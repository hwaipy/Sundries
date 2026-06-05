#!/usr/bin/env python3
"""Decode recorder .bin captures and transcribe speech with faster-whisper.

Pipeline: raw LE uint16 ADC @ SRC_RATE  ->  DC-block high-pass  ->  peak
normalize  ->  resample to 16 kHz float32  ->  Whisper (built-in Silero VAD
to skip silence)  ->  timestamped txt / json / srt next to the .bin.

Examples:
  analysis/venv/bin/python analysis/stt.py --list
  analysis/venv/bin/python analysis/stt.py                 # latest file, small model
  analysis/venv/bin/python analysis/stt.py --model medium --board esp32-xxxx --date 20260605
  analysis/venv/bin/python analysis/stt.py --max-seconds 120 --wav   # quick test + dump wav
"""
import argparse
import json
import os
import sys
import wave

import numpy as np
from scipy.signal import butter, resample_poly, sosfilt

SRC_RATE = 10000      # recorder ADC rate (Hz); overridden by meta.json if present
DST_RATE = 16000      # Whisper expects 16 kHz
DEFAULT_DATA = os.path.join(os.path.dirname(__file__), "..", "server", "data")


def list_boards(data_dir):
    out = []
    if os.path.isdir(data_dir):
        for name in sorted(os.listdir(data_dir)):
            bdir = os.path.join(data_dir, name)
            bins = sorted(f for f in os.listdir(bdir) if f.endswith(".bin")) if os.path.isdir(bdir) else []
            if bins:
                out.append((name, bins))
    return out


def pick_bin(data_dir, board, date):
    boards = list_boards(data_dir)
    if not boards:
        sys.exit(f"no data under {data_dir}")
    if board is None:
        board = boards[0][0]
    match = dict(boards).get(board)
    if not match:
        sys.exit(f"board {board} not found; have: {[b for b, _ in boards]}")
    fname = f"{date}.bin" if date else match[-1]
    if fname not in match:
        sys.exit(f"{fname} not found for {board}; have: {match}")
    return board, os.path.join(data_dir, board, fname)


def read_rate(data_dir, board):
    try:
        with open(os.path.join(data_dir, board, "meta.json")) as f:
            r = json.load(f).get("sample_rate_hz")
            return int(r) if r else SRC_RATE
    except Exception:
        return SRC_RATE


def load_audio(bin_path, src_rate, max_seconds=None):
    raw = np.fromfile(bin_path, dtype="<u2")
    if max_seconds:
        raw = raw[: int(max_seconds * src_rate)]
    if raw.size == 0:
        sys.exit("empty capture")
    x = raw.astype(np.float32)
    # DC-block / rumble high-pass at 80 Hz (removes the ~1.25V bias & AGC drift).
    sos = butter(2, 80.0, btype="highpass", fs=src_rate, output="sos")
    x = sosfilt(sos, x).astype(np.float32)
    # Peak normalize to 0.95 (use 99.9th pct to ignore rare spikes).
    peak = np.percentile(np.abs(x), 99.9)
    if peak > 0:
        x = np.clip(x / peak * 0.95, -1.0, 1.0).astype(np.float32)
    # Resample src_rate -> 16k. 16000/10000 = 8/5; otherwise fall back to ratio.
    from math import gcd
    g = gcd(DST_RATE, src_rate)
    x = resample_poly(x, DST_RATE // g, src_rate // g).astype(np.float32)
    return x


def write_wav(path, audio16k):
    pcm = (np.clip(audio16k, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(DST_RATE)
        w.writeframes(pcm.tobytes())


def fmt_ts(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--board", default=None)
    ap.add_argument("--date", default=None, help="YYYYMMDD; default = latest file")
    ap.add_argument("--model", default="small",
                    help="tiny|base|small|medium|large-v3 (bigger = more accurate, slower)")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute", default="int8", help="int8|int8_float32|float32")
    ap.add_argument("--max-seconds", type=float, default=None, help="only first N seconds (quick test)")
    ap.add_argument("--language", default=None, help="force language (e.g. zh, en); default auto-detect")
    ap.add_argument("--wav", action="store_true", help="also dump the cleaned 16k wav next to outputs")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    data_dir = os.path.abspath(args.data)

    if args.list:
        for b, bins in list_boards(data_dir):
            print(b, "->", ", ".join(bins))
        return

    board, bin_path = pick_bin(data_dir, args.board, args.date)
    src_rate = read_rate(data_dir, board)
    print(f"[stt] board={board} file={os.path.basename(bin_path)} src_rate={src_rate}Hz", flush=True)

    audio = load_audio(bin_path, src_rate, args.max_seconds)
    dur = len(audio) / DST_RATE
    print(f"[stt] decoded {dur:.1f}s of audio ({len(audio)} samples @ {DST_RATE}Hz)", flush=True)

    stem = os.path.splitext(bin_path)[0]
    if args.wav:
        write_wav(stem + ".clean16k.wav", audio)
        print(f"[stt] wrote {stem}.clean16k.wav", flush=True)

    from faster_whisper import WhisperModel
    print(f"[stt] loading model '{args.model}' ({args.device}/{args.compute}); first run downloads it ...", flush=True)
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute)

    segments, info = model.transcribe(
        audio,
        language=args.language,          # None -> auto-detect (mixed zh/en ok)
        task="transcribe",
        beam_size=5,
        vad_filter=True,                 # built-in Silero VAD drops silence
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    print(f"[stt] detected language={info.language} (p={info.language_probability:.2f})", flush=True)

    rows = []
    print("\n----- transcript -----")
    for seg in segments:
        line = f"[{fmt_ts(seg.start)} -> {fmt_ts(seg.end)}] {seg.text.strip()}"
        print(line, flush=True)
        rows.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip()})
    print("----- end -----\n")

    if not rows:
        print("[stt] no speech detected (VAD found only silence/noise).", flush=True)

    with open(stem + ".transcript.json", "w") as f:
        json.dump({"board": board, "file": os.path.basename(bin_path),
                   "language": info.language, "segments": rows}, f, ensure_ascii=False, indent=2)
    with open(stem + ".transcript.txt", "w") as f:
        for r in rows:
            f.write(f"[{fmt_ts(r['start'])}] {r['text']}\n")
    with open(stem + ".transcript.srt", "w") as f:
        for i, r in enumerate(rows, 1):
            f.write(f"{i}\n{fmt_ts(r['start']).replace('.', ',')} --> "
                    f"{fmt_ts(r['end']).replace('.', ',')}\n{r['text']}\n\n")
    print(f"[stt] wrote {stem}.transcript.(json|txt|srt)  ({len(rows)} segments)", flush=True)


if __name__ == "__main__":
    main()
