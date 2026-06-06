#!/usr/bin/env python3
"""Ingest + viewer server for ESP32 recorder boards.

Ingest (default port 8123):
  POST /ingest/<board_id>  append opaque binary (LE uint16 ADC samples) to
                           DATA_DIR/<board_id>/<UTC-date>.bin, update meta.json
  GET  /stats              JSON status of all boards

Viewer / web GUI (default --web-port 8124), refreshed at 1Hz by the page:
  GET  /                   dashboard: pick any device, live waveform
  GET  /api/boards         per-device metadata (one row per board)
  GET  /api/samples?board=<id>&secs=<n>  recent samples, min/max-decimated

Stdlib only. Run:
  python3 ingest_server.py [--host 0.0.0.0] [--port 8123] [--web-port 8124] [--data ./data]
"""
import argparse
import array
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DATE_BIN_RE = re.compile(r"^\d{8}\.bin$")
MAX_BODY = 8 * 1024 * 1024  # 8 MiB per batch, generous upper bound

# Per-minute MP3 snapshots: a background thread slices each board's raw .bin
# into fixed-length windows, DC-removes + scales them to 16-bit PCM, and pipes
# them through `lame` into DATA_DIR/<board>/mp3/<day>_<index>.mp3. Slicing is by
# sample count (gapless, exactly MP3_SECONDS each) with a cursor persisted in
# meta.json, so restarts resume instead of re-encoding.
MP3_ENABLE = True
MP3_SECONDS = 60      # audio seconds per file
MP3_BITRATE = 64      # kbps, mono
MP3_GAIN = 16         # 12-bit ADC span -> 16-bit PCM span

DATA_DIR = "./data"
_locks_guard = threading.Lock()
_board_locks: dict[str, threading.Lock] = {}


def board_lock(board_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _board_locks.get(board_id)
        if lock is None:
            lock = threading.Lock()
            _board_locks[board_id] = lock
        return lock


def board_dir(board_id: str) -> str:
    return os.path.join(DATA_DIR, board_id)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_meta(path: str) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_meta(path: str, meta: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, path)


def _to_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return s


# ----------------------------- data reading (viewer) -----------------------------

def list_boards() -> list:
    boards = []
    if os.path.isdir(DATA_DIR):
        for name in sorted(os.listdir(DATA_DIR)):
            meta_path = os.path.join(DATA_DIR, name, "meta.json")
            if os.path.isfile(meta_path):
                boards.append(load_meta(meta_path))
    return boards


def latest_bin(board_id: str):
    bdir = board_dir(board_id)
    if not os.path.isdir(bdir):
        return None
    bins = sorted(n for n in os.listdir(bdir) if DATE_BIN_RE.match(n))
    return os.path.join(bdir, bins[-1]) if bins else None


def read_recent(board_id: str, secs: float, buckets: int = 800) -> dict:
    """Read the tail (~secs) of the board's latest .bin and min/max-decimate it
    into <=buckets [min,max] pairs for an envelope plot."""
    meta = load_meta(os.path.join(board_dir(board_id), "meta.json"))
    rate = meta.get("sample_rate_hz") or 10000
    if not isinstance(rate, int) or rate <= 0:
        rate = 10000
    out = {"board": board_id, "rate": rate, "secs": secs, "count": 0,
           "buckets": [], "min": None, "max": None, "mean": None}
    path = latest_bin(board_id)
    if not path:
        return out

    want_bytes = int(secs * rate) * 2
    size = os.path.getsize(path)
    start = max(0, size - want_bytes)
    start -= start % 2  # align to sample boundary
    with open(path, "rb") as f:
        f.seek(start)
        raw = f.read()
    count = len(raw) // 2
    if count == 0:
        return out
    samples = struct.unpack("<%dH" % count, raw[:count * 2])

    out["count"] = count
    out["min"] = min(samples)
    out["max"] = max(samples)
    out["mean"] = sum(samples) // count

    step = max(1, (count + buckets - 1) // buckets)
    bk = []
    for i in range(0, count, step):
        chunk = samples[i:i + step]
        bk.append([min(chunk), max(chunk)])
    out["buckets"] = bk
    return out


# ----------------------------- per-minute MP3 snapshots -----------------------------

def list_bins(board_id: str):
    bdir = board_dir(board_id)
    if not os.path.isdir(bdir):
        return bdir, []
    return bdir, sorted(n for n in os.listdir(bdir) if DATE_BIN_RE.match(n))


def adc_slice_to_pcm(raw: bytes) -> bytes:
    """Raw little-endian uint16 ADC samples -> signed 16-bit mono PCM, with the
    per-slice DC bias removed and the 12-bit span scaled toward 16-bit."""
    a = array.array("H")
    a.frombytes(raw[: (len(raw) // 2) * 2])
    if sys.byteorder == "big":
        a.byteswap()
    n = len(a)
    if n == 0:
        return b""
    mean = sum(a) // n
    out = array.array("h", bytes(2 * n))
    for i in range(n):
        v = (a[i] - mean) * MP3_GAIN
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out[i] = v
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def encode_mp3(pcm: bytes, rate: int, out_path: str) -> bool:
    """Pipe raw PCM through lame. lame auto-resamples non-MPEG rates (e.g. the
    10kHz capture -> 11.025kHz) so playback speed/pitch stay correct."""
    tmp = out_path + ".tmp"
    cmd = ["lame", "-r", "-s", f"{rate / 1000:g}",
           "--signed", "--bitwidth", "16", "--little-endian",
           "-m", "m", "-b", str(MP3_BITRATE), "--quiet", "-", tmp]
    try:
        p = subprocess.run(cmd, input=pcm,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return False
    if p.returncode != 0:
        print(f"[mp3] lame rc={p.returncode}: "
              f"{p.stderr.decode(errors='replace').strip()}", flush=True)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    os.replace(tmp, out_path)
    return True


def mp3_step(board_id: str) -> bool:
    """Encode at most one pending MP3 window for the board, advancing a cursor in
    meta.json. Returns True if it did work (encoded, or skipped to a newer .bin),
    False once caught up. The heavy lame call runs outside the per-board lock."""
    meta_path = os.path.join(board_dir(board_id), "meta.json")
    with board_lock(board_id):
        meta = load_meta(meta_path)
        rate = meta.get("sample_rate_hz") or 10000
        if not isinstance(rate, int) or rate <= 0:
            rate = 10000
        slice_bytes = rate * MP3_SECONDS * 2
        bdir, bins = list_bins(board_id)
        if not bins:
            return False
        cur = meta.get("mp3_bin")
        off = meta.get("mp3_offset", 0)
        if cur not in bins:           # unset or pointing at a rotated-away file
            cur, off = bins[-1], 0
        path = os.path.join(bdir, cur)
        size = os.path.getsize(path)
        if size - off < slice_bytes:  # not a full window yet in the cursor's .bin
            newer = [b for b in bins if b > cur]
            if newer:                 # day rolled over: skip the <1min tail, advance
                meta["mp3_bin"], meta["mp3_offset"] = newer[0], 0
                save_meta(meta_path, meta)
                return True
            if meta.get("mp3_bin") != cur or meta.get("mp3_offset") != off:
                meta["mp3_bin"], meta["mp3_offset"] = cur, off
                save_meta(meta_path, meta)
            return False
        index = off // slice_bytes
        enc_bin, enc_off = cur, off

    with open(path, "rb") as f:       # append-only region below `size`: safe unlocked
        f.seek(enc_off)
        raw = f.read(slice_bytes)
    pcm = adc_slice_to_pcm(raw)
    mp3_dir = os.path.join(bdir, "mp3")
    os.makedirs(mp3_dir, exist_ok=True)
    out_path = os.path.join(mp3_dir, f"{enc_bin[:-4]}_{index:04d}.mp3")
    if not encode_mp3(pcm, rate, out_path):
        return False                  # leave the cursor put; retry next tick

    with board_lock(board_id):
        meta = load_meta(meta_path)   # reload to keep concurrent ingest updates
        meta["mp3_bin"] = enc_bin
        meta["mp3_offset"] = enc_off + slice_bytes
        meta["mp3_last"] = os.path.basename(out_path)
        meta["mp3_count"] = meta.get("mp3_count", 0) + 1
        save_meta(meta_path, meta)
    print(f"[mp3] {board_id} -> {os.path.basename(out_path)} "
          f"({len(pcm) // 2} samples)", flush=True)
    return True


def mp3_worker(interval: float = 5.0) -> None:
    """Drain every board's pending MP3 windows, then sleep. Also backfills any
    accumulated unencoded audio (e.g. after the server was down)."""
    while True:
        try:
            boards = ([n for n in os.listdir(DATA_DIR)
                       if os.path.isfile(os.path.join(DATA_DIR, n, "meta.json"))]
                      if os.path.isdir(DATA_DIR) else [])
            for b in boards:
                guard = 0
                while mp3_step(b) and guard < 100000:
                    guard += 1
        except Exception as e:  # never let the worker thread die
            print(f"[mp3] worker error: {e}", flush=True)
        time.sleep(interval)


# ----------------------------- ingest server (POST) -----------------------------

class IngestHandler(BaseHTTPRequestHandler):
    server_version = "RecorderIngest/1.1"

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[ingest] {self.address_string()} {fmt % args}", flush=True)

    def do_POST(self):
        m = re.match(r"^/ingest/([^/]+)$", self.path)
        if not m:
            return self._json(404, {"error": "use POST /ingest/<board_id>"})
        board_id = m.group(1)
        if not BOARD_ID_RE.match(board_id):
            return self._json(400, {"error": "invalid board_id"})

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY:
            return self._json(400, {"error": "bad Content-Length"})
        body = self.rfile.read(length)
        if len(body) != length:
            return self._json(400, {"error": "short body"})

        seq = self.headers.get("X-Seq")
        rate = self.headers.get("X-Sample-Rate")

        bdir = board_dir(board_id)
        with board_lock(board_id):
            os.makedirs(bdir, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            bin_path = os.path.join(bdir, f"{day}.bin")
            with open(bin_path, "ab") as f:
                f.write(body)

            meta_path = os.path.join(bdir, "meta.json")
            meta = load_meta(meta_path)
            now = utc_now()
            meta.setdefault("board_id", board_id)
            meta.setdefault("first_seen", now)
            meta["last_seen"] = now
            meta["total_bytes"] = meta.get("total_bytes", 0) + len(body)
            meta["total_batches"] = meta.get("total_batches", 0) + 1
            if rate is not None:
                meta["sample_rate_hz"] = _to_int(rate)
            if seq is not None:
                meta["last_seq"] = _to_int(seq)
            save_meta(meta_path, meta)

        return self._json(200, {"ok": True, "stored": len(body),
                                "file": os.path.basename(bin_path)})

    def do_GET(self):
        if self.path in ("/", "/stats"):
            return self._json(200, {"server_time": utc_now(), "boards": list_boards()})
        return self._json(404, {"error": "not found"})


# ----------------------------- viewer server (GET) -----------------------------

class WebHandler(BaseHTTPRequestHandler):
    server_version = "RecorderViewer/1.0"

    def log_message(self, fmt, *args):
        pass  # quiet; the dashboard polls 1Hz per client

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/" or path.startswith("/index"):
            return self._html(PAGE.encode())
        if path == "/api/boards":
            return self._json(200, {"server_time": utc_now(), "boards": list_boards()})
        if path == "/api/samples":
            board = (qs.get("board") or [""])[0]
            if not BOARD_ID_RE.match(board):
                return self._json(400, {"error": "invalid board"})
            try:
                secs = float((qs.get("secs") or ["1"])[0])
            except ValueError:
                secs = 1.0
            secs = max(0.05, min(secs, 10.0))
            try:
                return self._json(200, read_recent(board, secs))
            except OSError as e:
                return self._json(500, {"error": str(e)})
        return self._json(404, {"error": "not found"})


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recorder viewer</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#0d1117; color:#e6edf3; font:14px/1.4 -apple-system,system-ui,sans-serif; height:100vh; display:flex; }
  #side { width:260px; flex:none; border-right:1px solid #21262d; overflow-y:auto; padding:10px; }
  #side h2 { font-size:12px; text-transform:uppercase; color:#6e7681; letter-spacing:.05em; margin:6px 4px; }
  .dev { padding:9px 10px; border-radius:8px; cursor:pointer; margin-bottom:6px; border:1px solid #21262d; background:#161b22; }
  .dev:hover { border-color:#30363d; }
  .dev.sel { border-color:#1f6feb; background:#172033; }
  .dev .id { font-weight:600; font-size:13px; word-break:break-all; }
  .dev .sub { font-size:11px; color:#8b949e; margin-top:3px; }
  .dev .age { font-size:11px; }
  .live { color:#3fb950; } .stale { color:#d29922; } .dead { color:#f85149; }
  #main { flex:1; display:flex; flex-direction:column; min-width:0; }
  header { padding:12px 18px; border-bottom:1px solid #21262d; display:flex; gap:22px; align-items:baseline; flex-wrap:wrap; }
  h1 { font-size:14px; margin:0; color:#8b949e; font-weight:600; }
  .big { font-size:30px; font-weight:700; font-variant-numeric:tabular-nums; }
  .v{color:#58a6ff;} .pp{color:#3fb950;} .muted{color:#6e7681; font-size:12px;}
  canvas { flex:1; width:100%; display:block; }
  .hint { font-size:11px; color:#6e7681; }
</style>
</head>
<body>
<div id="side"><h2>Devices</h2><div id="devs"></div></div>
<div id="main">
  <header>
    <h1 id="title">no device</h1>
    <div>V <span class="big v" id="volt">--</span></div>
    <div>p-p <span class="big pp" id="pp">--</span></div>
    <div class="muted" id="meta"></div>
    <div class="hint">1&nbsp;Hz refresh · y 0&ndash;3.3V · band = min/max envelope of last 1s</div>
  </header>
  <canvas id="c"></canvas>
</div>
<script>
const VREF=3.3, ADC_MAX=4095, BIAS=1.25, REFRESH_MS=1000, WINDOW_S=1.0;
let boards=[], cur=null, samples=null;
const toV = x => x/ADC_MAX*VREF;

const cv=document.getElementById('c'), ctx=cv.getContext('2d');
function fit(){ const r=devicePixelRatio||1; cv.width=cv.clientWidth*r; cv.height=cv.clientHeight*r; ctx.setTransform(r,0,0,r,0,0); }
addEventListener('resize',()=>{fit();draw();});

function ageClass(iso){ if(!iso) return ['dead','?']; const s=(Date.now()-Date.parse(iso))/1000;
  const txt = s<60? s.toFixed(0)+'s' : s<3600? (s/60).toFixed(0)+'m' : (s/3600).toFixed(1)+'h';
  return [s<6?'live':s<60?'stale':'dead', txt+' ago']; }

function renderDevs(){
  const el=document.getElementById('devs'); el.innerHTML='';
  if(!boards.length){ el.innerHTML='<div class="muted" style="padding:8px">no data yet</div>'; return; }
  boards.forEach(b=>{
    const [cls,age]=ageClass(b.last_seen);
    const d=document.createElement('div'); d.className='dev'+(b.board_id===cur?' sel':'');
    d.innerHTML=`<div class="id">${b.board_id}</div>
      <div class="sub">${(b.sample_rate_hz||'?')}Hz · ${((b.total_bytes||0)/1024).toFixed(0)} KiB · seq ${b.last_seq??'?'}</div>
      <div class="age ${cls}">● ${age}</div>`;
    d.onclick=()=>{ cur=b.board_id; renderDevs(); pull(); };
    el.appendChild(d);
  });
}

async function refreshBoards(){
  try{ const r=await fetch('/api/boards',{cache:'no-store'}); boards=(await r.json()).boards||[]; }
  catch(e){ boards=[]; }
  if(!cur && boards.length) cur=boards[0].board_id;
  renderDevs();
  const b=boards.find(x=>x.board_id===cur);
  document.getElementById('title').textContent = cur || 'no device';
  document.getElementById('meta').textContent = b ?
    `batches ${b.total_batches||0} · ${((b.total_bytes||0)/1024).toFixed(0)} KiB · last_seq ${b.last_seq??'?'}` : '';
}

async function pull(){
  if(!cur){ samples=null; draw(); return; }
  try{ const r=await fetch(`/api/samples?board=${encodeURIComponent(cur)}&secs=${WINDOW_S}`,{cache:'no-store'});
       samples=await r.json(); }
  catch(e){ samples=null; }
  if(samples && samples.mean!=null){
    document.getElementById('volt').textContent=toV(samples.mean).toFixed(3)+' V';
    document.getElementById('pp').textContent=(toV(samples.max-samples.min)*1000).toFixed(0)+' mV';
  } else { document.getElementById('volt').textContent='--'; document.getElementById('pp').textContent='--'; }
  draw();
}

function draw(){
  const w=cv.clientWidth, h=cv.clientHeight; ctx.clearRect(0,0,w,h);
  const L=42, y=v=>h-(v/VREF)*h;
  ctx.strokeStyle='#21262d'; ctx.fillStyle='#6e7681'; ctx.lineWidth=1; ctx.font='11px sans-serif';
  for(let i=0;i<=VREF*2;i++){ const v=i/2, yy=y(v); ctx.beginPath(); ctx.moveTo(L,yy); ctx.lineTo(w,yy); ctx.stroke(); ctx.fillText(v.toFixed(1)+'V',4,yy-2); }
  ctx.strokeStyle='#8b949e'; ctx.setLineDash([5,4]); ctx.beginPath(); ctx.moveTo(L,y(BIAS)); ctx.lineTo(w,y(BIAS)); ctx.stroke(); ctx.setLineDash([]);
  if(!samples || !samples.buckets || !samples.buckets.length){
    ctx.fillStyle='#6e7681'; ctx.fillText('no samples', L+10, 20); return; }
  const bk=samples.buckets, n=bk.length, x=i=>L+(w-L)*i/Math.max(1,n-1);
  ctx.beginPath(); for(let i=0;i<n;i++){ const xx=x(i), yy=y(toV(bk[i][1])); i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy); }
  for(let i=n-1;i>=0;i--){ ctx.lineTo(x(i),y(toV(bk[i][0]))); }
  ctx.closePath(); ctx.fillStyle='rgba(88,166,255,0.25)'; ctx.fill();
  ctx.strokeStyle='#58a6ff'; ctx.lineWidth=1; ctx.beginPath();
  for(let i=0;i<n;i++){ const mid=(bk[i][0]+bk[i][1])/2, xx=x(i), yy=y(toV(mid)); i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy); }
  ctx.stroke();
}

fit();
refreshBoards().then(pull);
setInterval(async()=>{ await refreshBoards(); await pull(); }, REFRESH_MS);
</script>
</body>
</html>"""


def main():
    global DATA_DIR, MP3_ENABLE, MP3_SECONDS, MP3_BITRATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("INGEST_HOST", "0.0.0.0"),
                    help="bind address (0.0.0.0 so the frpc container can reach it)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("INGEST_PORT", 8123)))
    ap.add_argument("--web-port", type=int, default=int(os.environ.get("INGEST_WEB_PORT", 8124)),
                    help="viewer/dashboard port (0 to disable)")
    ap.add_argument("--data", default=os.environ.get("INGEST_DATA", "./data"))
    ap.add_argument("--no-mp3", action="store_true",
                    default=os.environ.get("INGEST_NO_MP3", "") not in ("", "0", "false"),
                    help="disable the per-minute MP3 snapshots")
    ap.add_argument("--mp3-seconds", type=int,
                    default=int(os.environ.get("INGEST_MP3_SECONDS", MP3_SECONDS)),
                    help="audio seconds per MP3 file")
    ap.add_argument("--mp3-bitrate", type=int,
                    default=int(os.environ.get("INGEST_MP3_BITRATE", MP3_BITRATE)),
                    help="MP3 bitrate in kbps (mono)")
    args = ap.parse_args()
    DATA_DIR = os.path.abspath(args.data)
    os.makedirs(DATA_DIR, exist_ok=True)
    MP3_ENABLE = not args.no_mp3
    MP3_SECONDS = max(1, args.mp3_seconds)
    MP3_BITRATE = args.mp3_bitrate

    if MP3_ENABLE:
        if shutil.which("lame"):
            threading.Thread(target=mp3_worker, daemon=True).start()
            print(f"[mp3] enabled: {MP3_SECONDS}s/file @ {MP3_BITRATE}kbps "
                  f"-> <board>/mp3/", flush=True)
        else:
            print("[mp3] DISABLED: 'lame' not found on PATH (apt-get install lame)",
                  flush=True)

    if args.web_port:
        web = ThreadingHTTPServer((args.host, args.web_port), WebHandler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        print(f"[viewer] listening on {args.host}:{args.web_port}", flush=True)

    httpd = ThreadingHTTPServer((args.host, args.port), IngestHandler)
    print(f"[ingest] listening on {args.host}:{args.port}  data={DATA_DIR}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[ingest] shutting down", flush=True)


if __name__ == "__main__":
    main()
