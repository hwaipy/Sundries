#!/usr/bin/env python3
"""
Recorder WebGUI — Flask server with background sync + auto-transcription.

- Syncs mp3 from WebDAV mount to ~/Claude/Record/<device>/mp3/
- Auto-transcribes new mp3 files with mlx-whisper
- Serves a web UI for browsing recordings, playing audio, viewing transcripts
"""
import os, json, time, threading, glob, subprocess, shutil
import numpy as np
from pathlib import Path
from flask import Flask, jsonify, request, send_file, send_from_directory
from scipy.signal import stft, istft, butter, sosfiltfilt

# ── paths ──────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / "Claude" / "Record"
WEBDAV_SRC = Path("/Volumes/grayfog.chat/docker/Record/20260528_Recorder/server/data")
STATIC_DIR = Path(__file__).parent

app = Flask(__name__)

# ── sync worker ────────────────────────────────────────────────────────
SYNC_INTERVAL = 30  # seconds
WEBDAV_URL = "https://grayfog.chat:5056/docker/Record/20260528_Recorder/server/data"
WEBDAV_AUTH = "Recorder:h0lyelf"


def _sync_device(device_name):
    """Sync by probing sequential filenames via curl (bypasses macOS WebDAV mount)."""
    mp3_dst = DATA_DIR / device_name / "mp3"
    mp3_dst.mkdir(parents=True, exist_ok=True)
    base_url = f"{WEBDAV_URL}/{device_name}/mp3"

    local_files = sorted(f.name for f in mp3_dst.glob("*.mp3"))
    if local_files:
        last = local_files[-1]
        prefix = last[:9]
        num = int(last[9:13])
    else:
        import datetime
        prefix = datetime.date.today().strftime("%Y%m%d") + "_"
        num = -1

    import datetime
    copied = 0
    cur_date = datetime.datetime.strptime(prefix[:8], "%Y%m%d").date()
    today = datetime.date.today()
    cur_num = num + 1

    while cur_date <= today:
        cur_prefix = cur_date.strftime("%Y%m%d") + "_"
        miss = 0
        while miss < 5:
            fname = f"{cur_prefix}{cur_num:04d}.mp3"
            dst_file = mp3_dst / fname
            if dst_file.exists():
                miss = 0
                cur_num += 1
                continue
            url = f"{base_url}/{fname}"
            try:
                r = subprocess.run(
                    ["curl", "-sf", "-k", "-u", WEBDAV_AUTH,
                     "-o", str(dst_file), url],
                    capture_output=True, timeout=15)
                if r.returncode == 0 and dst_file.exists() and dst_file.stat().st_size > 0:
                    copied += 1
                    miss = 0
                else:
                    dst_file.unlink(missing_ok=True)
                    miss += 1
            except subprocess.TimeoutExpired:
                dst_file.unlink(missing_ok=True)
                miss += 1
            except Exception:
                miss += 1
            cur_num += 1
        # Move to next day
        cur_date += datetime.timedelta(days=1)
        cur_num = 0

    if copied:
        print(f"[sync] {device_name}: +{copied} files", flush=True)


def _discover_devices():
    """List esp32-* device dirs from WebDAV via PROPFIND."""
    import re
    try:
        r = subprocess.run(
            ["curl", "-sf", "-k", "-u", WEBDAV_AUTH, "-X", "PROPFIND",
             "-H", "Depth: 1", WEBDAV_URL + "/"],
            capture_output=True, timeout=15, text=True)
        if r.returncode == 0:
            return sorted(set(re.findall(r'esp32-[0-9a-f]+', r.stdout)))
    except Exception:
        pass
    return []


def sync_worker():
    """Sync new mp3 files via curl to WebDAV (auto-discovers devices)."""
    _cached_devices = []
    _last_discover = 0
    while True:
        try:
            now = time.time()
            if now - _last_discover > 300 or not _cached_devices:
                found = _discover_devices()
                if found:
                    if found != _cached_devices:
                        print(f"[sync] devices: {found}", flush=True)
                    _cached_devices = found
                _last_discover = now
            for d in _cached_devices:
                _sync_device(d)
        except Exception as e:
            print(f"[sync] error: {e}", flush=True)
        time.sleep(SYNC_INTERVAL)

# ── denoise worker ────────────────────────────────────────────────────
DN_SR = 11025
DN_NP = 512
DN_HOP = 128
DN_WIN = 'hann'
DN_FSENV = DN_SR / DN_HOP
DN_WINDOW = 40      # files around target to consider for auto-reference
DN_REF_RATIO = 0.2  # pick quietest 20% as reference
_denoise_status = {"current": None, "queue": 0}
_rms_cache = {}     # filepath -> rms (cached to avoid re-decoding)


def _dn_decode(path):
    p = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', str(path),
         '-ar', str(DN_SR), '-ac', '1', '-f', 'f32le', '-'],
        capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.float32).astype(np.float64)


def _dn_rms(path):
    """Get RMS of a file, cached."""
    key = str(path)
    if key not in _rms_cache:
        x = _dn_decode(path)
        _rms_cache[key] = float(np.sqrt(np.mean(x ** 2)))
    return _rms_cache[key]


def _dn_build_profile_from_files(files):
    """Build noise profile from a list of mp3 file paths."""
    acc, cnt = None, 0
    for f in files:
        x = _dn_decode(f)
        _, _, Z = stft(x, DN_SR, window=DN_WIN, nperseg=DN_NP, noverlap=DN_NP - DN_HOP)
        P = (np.abs(Z) ** 2).sum(1)
        acc = P if acc is None else acc + P
        cnt += (np.abs(Z) ** 2).shape[1]
    if acc is None:
        return None
    return acc / cnt


def _dn_auto_ref(all_files, target_idx):
    """Pick quietest files in a window around target_idx as reference."""
    half = DN_WINDOW // 2
    lo = max(0, target_idx - half)
    hi = min(len(all_files), target_idx + half + 1)
    window = all_files[lo:hi]
    # Compute RMS for window
    rms_list = [(f, _dn_rms(f)) for f in window]
    rms_list.sort(key=lambda x: x[1])
    n_ref = max(3, int(len(rms_list) * DN_REF_RATIO))
    return [f for f, _ in rms_list[:n_ref]]


def _dn_pulse_track(P):
    Pm = P - P.mean()
    nf = len(P)
    F = np.fft.rfft(Pm)
    fr = np.fft.rfftfreq(nf, 1 / DN_FSENV)
    band = (fr > 3.4) & (fr < 4.6)
    if not band.any():
        return np.zeros(nf), 4.0
    f0 = fr[band][np.argmax(np.abs(F[band]))]
    mask = np.zeros_like(F)
    for k in range(1, 13):
        sel = np.abs(fr - k * f0) < 0.28
        mask[sel] = F[sel]
    return np.fft.irfft(mask, n=nf), f0


def _dn_enhance(x, NPSD, ov=2.4, beta=0.06, alpha_dd=0.98):
    SQN = np.sqrt(NPSD)
    STOT = NPSD.sum()
    _, _, Z = stft(x, DN_SR, window=DN_WIN, nperseg=DN_NP, noverlap=DN_NP - DN_HOP)
    mag = np.abs(Z); ph = np.angle(Z); Y = mag ** 2
    nb, nf = Z.shape
    c, _ = _dn_pulse_track(Y.sum(0))
    s = np.clip((STOT + c) / STOT, 0.1, 12.0)
    noise = NPSD[:, None] * s[None, :]
    gamma = np.minimum(Y / (ov * noise), 1e3)
    Gp = np.ones(nb); gp = np.ones(nb); Go = np.empty_like(Y)
    floor = beta * SQN
    for l in range(nf):
        g = gamma[:, l]
        xi = np.maximum(alpha_dd * (Gp ** 2) * gp + (1 - alpha_dd) * np.maximum(g - 1, 0), 1e-3)
        v = xi / (1 + xi) * g
        ei = np.where(v < 0.1, -2.31 * np.log10(v) - 0.6,
             np.where(v > 1, np.exp(-v) / v * (1 - 1 / v + 2 / v ** 2),
                      -np.log(v) - 0.57722 + v - v * v / 4))
        G = np.minimum(xi / (1 + xi) * np.exp(0.5 * ei), 1.0)
        cm = np.maximum(G * mag[:, l], floor)
        Go[:, l] = cm / np.maximum(mag[:, l], 1e-9)
        Gp = Go[:, l]; gp = g
    _, y = istft(Go * mag * np.exp(1j * ph), DN_SR, window=DN_WIN,
                 nperseg=DN_NP, noverlap=DN_NP - DN_HOP)
    return y


def _dn_notch_50hz(x, sr=DN_SR, harmonics=10, Q=5):
    """Remove 50Hz mains hum and its harmonics with notch filters."""
    from scipy.signal import iirnotch
    y = x.copy()
    for k in range(1, harmonics + 1):
        f0 = 50.0 * k
        if f0 >= sr / 2:
            break
        b, a = iirnotch(f0, Q, sr)
        y = sosfiltfilt(np.array([np.concatenate([b, a])]), y)
    return y


def _dn_process_file(mp3_in, mp3_out, NPSD):
    import scipy.io.wavfile as wav
    hp = butter(4, 90.0 / (DN_SR / 2), btype='high', output='sos')
    x = _dn_decode(mp3_in)
    x = sosfiltfilt(hp, x)
    y = _dn_enhance(x, NPSD)
    # Remove 50Hz mains hum harmonics (electrical noise)
    y = _dn_notch_50hz(y)
    y = y / (np.abs(y).max() + 1e-9) * 0.97 * 32767
    tmp = str(mp3_out) + ".tmp.wav"
    wav.write(tmp, DN_SR, y.astype(np.int16))
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', tmp,
                    '-ar', str(DN_SR), '-ac', '1', '-b:a', '64k', str(mp3_out)],
                   check=True)
    os.remove(tmp)


def denoise_worker():
    """Watch for mp3 files without denoised versions and denoise them.
    Uses auto-reference: picks quietest 20% of nearby files as noise profile."""
    time.sleep(5)
    while True:
        try:
            for device_dir in sorted(DATA_DIR.glob("esp32-*")):
                mp3_dir = device_dir / "mp3"
                dn_dir = device_dir / "denoised"
                if not mp3_dir.is_dir():
                    continue
                dn_dir.mkdir(exist_ok=True)

                dev = device_dir.name
                all_files = sorted(mp3_dir.glob("*.mp3"))
                if len(all_files) < 5:
                    continue

                # Find files needing denoise
                pending_idx = []
                for i, mp3 in enumerate(all_files):
                    if not (dn_dir / mp3.name).exists():
                        pending_idx.append(i)

                _denoise_status["queue"] = len(pending_idx)
                last_ref_key = None
                NPSD = None
                for idx in pending_idx:
                    mp3 = all_files[idx]
                    _denoise_status["current"] = mp3.name

                    # Build auto-reference profile (reuse if window unchanged)
                    half = DN_WINDOW // 2
                    ref_key = (max(0, idx - half), min(len(all_files), idx + half + 1))
                    if ref_key != last_ref_key:
                        ref_files = _dn_auto_ref(all_files, idx)
                        NPSD = _dn_build_profile_from_files(ref_files)
                        last_ref_key = ref_key
                        if NPSD is None:
                            continue

                    print(f"[denoise] {dev}/{mp3.name}", flush=True)
                    try:
                        _dn_process_file(mp3, dn_dir / mp3.name, NPSD)
                    except Exception as e:
                        print(f"[denoise] error on {mp3.name}: {e}", flush=True)
                    _denoise_status["queue"] = _denoise_status["queue"] - 1

            _denoise_status["current"] = None
            _denoise_status["queue"] = 0
        except Exception as e:
            print(f"[denoise] error: {e}", flush=True)
            _denoise_status["current"] = None
        time.sleep(15)

# ── transcription worker (GPUClaw via SSH) ───────────────────────────
GPUCLAW_HOST = "GPUClaw"
GPUCLAW_STT = "~/stt_batch.sh"
GPUCLAW_TMP = "/tmp/stt_work"
_transcribe_status = {"current": None, "queue": 0}


def _stt_batch(file_list):
    """Upload files to GPUClaw, run STT, return {filename: segments}."""
    subprocess.run(["ssh", GPUCLAW_HOST, f"mkdir -p {GPUCLAW_TMP}"],
                   capture_output=True, timeout=10)
    files_to_upload = [str(f) for f in file_list]
    subprocess.run(
        ["scp", "-q"] + files_to_upload + [f"{GPUCLAW_HOST}:{GPUCLAW_TMP}/"],
        capture_output=True, timeout=120)

    remote_paths = [f"{GPUCLAW_TMP}/{Path(f).name}" for f in file_list]
    input_str = "\n".join(remote_paths) + "\n"
    proc = subprocess.run(
        ["ssh", GPUCLAW_HOST, GPUCLAW_STT],
        input=input_str, capture_output=True, text=True, timeout=600)

    results = {}
    for line in proc.stdout.strip().split("\n"):
        line = line.strip()
        if not line or line in ("LOADING", "READY"):
            continue
        try:
            obj = json.loads(line)
            if "error" not in obj:
                results[obj["file"]] = obj["segments"]
            else:
                print(f"[stt] error on {obj['file']}: {obj['error']}", flush=True)
        except json.JSONDecodeError:
            continue

    rm_paths = " ".join(remote_paths)
    subprocess.run(["ssh", GPUCLAW_HOST, f"rm -f {rm_paths}"],
                   capture_output=True, timeout=10)
    return results


def transcribe_worker():
    """Transcribe both raw and denoised audio via GPUClaw.
    - transcripts-raw/  <- from raw mp3
    - transcripts/      <- from denoised mp3 (when available)
    Raw is prioritized first for all existing files."""
    time.sleep(10)
    while True:
        try:
            # Collect pending: (audio_file, output_json, device, label)
            pending = []
            for device_dir in sorted(DATA_DIR.glob("esp32-*")):
                mp3_dir = device_dir / "mp3"
                dn_dir = device_dir / "denoised"
                tr_raw_dir = device_dir / "transcripts-raw"
                tr_dn_dir = device_dir / "transcripts"
                if not mp3_dir.is_dir():
                    continue
                tr_raw_dir.mkdir(exist_ok=True)
                tr_dn_dir.mkdir(exist_ok=True)
                dev = device_dir.name

                for mp3 in sorted(mp3_dir.glob("*.mp3")):
                    stem = mp3.stem
                    # Raw transcript needed?
                    tr_raw = tr_raw_dir / (stem + ".json")
                    if not tr_raw.exists():
                        pending.append((mp3, tr_raw, dev, "raw"))
                    # Denoised transcript needed?
                    dn_file = dn_dir / mp3.name
                    tr_dn = tr_dn_dir / (stem + ".json")
                    if dn_file.exists() and not tr_dn.exists():
                        pending.append((dn_file, tr_dn, dev, "dn"))

            _transcribe_status["queue"] = len(pending)
            if not pending:
                _transcribe_status["current"] = None
                time.sleep(15)
                continue

            BATCH = 20
            for i in range(0, len(pending), BATCH):
                batch = pending[i:i + BATCH]
                _transcribe_status["current"] = f"STT {batch[0][3]} batch"
                _transcribe_status["queue"] = len(pending) - i
                print(f"[transcribe] GPUClaw batch: {len(batch)} files ({batch[0][3]})", flush=True)

                results = _stt_batch([b[0] for b in batch])

                for audio_file, tr_path, dev, label in batch:
                    segs = results.get(audio_file.name, [])
                    with open(tr_path, "w", encoding="utf-8") as f:
                        json.dump(segs, f, ensure_ascii=False)
                    print(f"[transcribe] {dev}/{audio_file.stem} ({label}): {len(segs)} seg", flush=True)

            _transcribe_status["current"] = None
            _transcribe_status["queue"] = 0
        except Exception as e:
            print(f"[transcribe] error: {e}", flush=True)
            _transcribe_status["current"] = None
        time.sleep(3)

# ── API routes ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(STATIC_DIR / "index.html")


@app.route("/api/devices")
def api_devices():
    devices = []
    for d in sorted(DATA_DIR.glob("esp32-*")):
        mp3_dir = d / "mp3"
        count = len(list(mp3_dir.glob("*.mp3"))) if mp3_dir.is_dir() else 0
        devices.append({"id": d.name, "count": count})
    return jsonify(devices)


@app.route("/api/recordings/<device>")
def api_recordings(device):
    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 100))

    mp3_dir = DATA_DIR / device / "mp3"
    dn_dir = DATA_DIR / device / "denoised"
    tr_dn_dir = DATA_DIR / device / "transcripts"
    tr_raw_dir = DATA_DIR / device / "transcripts-raw"
    if not mp3_dir.is_dir():
        return jsonify({"items": [], "total": 0})

    # List all mp3 files, sort by mtime descending
    files = list(mp3_dir.glob("*.mp3"))
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    total = len(files)
    page = files[offset:offset + limit]

    def _load_tr(d, stem):
        if not d.is_dir():
            return None
        p = d / (stem + ".json")
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            return None

    items = []
    for f in page:
        st = f.stat()
        is_denoised = dn_dir.is_dir() and (dn_dir / f.name).exists()
        tr_dn = _load_tr(tr_dn_dir, f.stem)
        tr_raw = _load_tr(tr_raw_dir, f.stem)
        items.append({
            "filename": f.name,
            "mtime": st.st_mtime,
            "size": st.st_size,
            "is_denoised": is_denoised,
            "transcript": tr_dn,
            "transcript_raw": tr_raw,
        })

    return jsonify({"items": items, "total": total})


@app.route("/audio/<device>/<filename>")
def serve_audio(device, filename):
    # Prefer denoised version if available
    dn_path = DATA_DIR / device / "denoised" / filename
    if dn_path.exists():
        return send_file(dn_path, mimetype="audio/mpeg")
    raw_path = DATA_DIR / device / "mp3" / filename
    if raw_path.exists():
        return send_file(raw_path, mimetype="audio/mpeg")
    return "Not found", 404


@app.route("/audio-raw/<device>/<filename>")
def serve_audio_raw(device, filename):
    raw_path = DATA_DIR / device / "mp3" / filename
    if raw_path.exists():
        return send_file(raw_path, mimetype="audio/mpeg")
    return "Not found", 404


@app.route("/api/status")
def api_status():
    return jsonify({
        "denoise": {
            "current": _denoise_status["current"],
            "queue": _denoise_status["queue"],
        },
        "transcribe": {
            "current": _transcribe_status["current"],
            "queue": _transcribe_status["queue"],
        },
        "webdav_mounted": WEBDAV_SRC.exists(),
    })


# ── main ───────────────────────────────────────────────────────────────
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    t_sync = threading.Thread(target=sync_worker, daemon=True)
    t_sync.start()
    t_dn = threading.Thread(target=denoise_worker, daemon=True)
    t_dn.start()
    t_trans = threading.Thread(target=transcribe_worker, daemon=True)
    t_trans.start()

    print(f"[webgui] data dir: {DATA_DIR}", flush=True)
    print(f"[webgui] webdav:   {WEBDAV_SRC}", flush=True)
    print(f"[webgui] http://localhost:9200", flush=True)
    app.run(host="0.0.0.0", port=9200, debug=False)


if __name__ == "__main__":
    main()
