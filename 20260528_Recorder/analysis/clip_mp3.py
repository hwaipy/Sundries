#!/usr/bin/env python3
"""Grab the next N seconds of incoming recorder data and save it as an MP3.

Snapshots the board's .bin size(s), waits N seconds (so you can speak), then
reads only the newly-appended samples, cleans them (DC high-pass + peak
normalize), resamples 10k->16k (10k isn't a legal MP3 rate), and encodes MP3.

  analysis/venv/bin/python analysis/clip_mp3.py [--seconds 60] [--board id]
"""
import argparse
import glob
import os
import time

import numpy as np
import lameenc
from scipy.signal import butter, resample_poly, sosfilt

DATA = os.path.join(os.path.dirname(__file__), "..", "server", "data")
SRC_RATE, DST_RATE = 10000, 16000


def sizes(board_dir):
    return {p: os.path.getsize(p) for p in sorted(glob.glob(os.path.join(board_dir, "*.bin")))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--board", default=None)
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--out", default=None)
    ap.add_argument("--bitrate", type=int, default=96)
    args = ap.parse_args()

    data = os.path.abspath(args.data)
    board = args.board or sorted(os.listdir(data))[0]
    bdir = os.path.join(data, board)

    before = sizes(bdir)
    print(f"[clip] board={board}  recording next {args.seconds:.0f}s ... speak now", flush=True)
    time.sleep(args.seconds)
    after = sizes(bdir)

    chunks = []
    for p in sorted(after):
        start = before.get(p, 0)
        if after[p] > start:
            with open(p, "rb") as f:
                f.seek(start - start % 2)
                chunks.append(f.read())
    raw = b"".join(chunks)
    n = len(raw) // 2
    if n == 0:
        print("[clip] no new data arrived (board offline / uploads failing?)")
        return
    print(f"[clip] captured {n} samples = {n / SRC_RATE:.1f}s of audio", flush=True)

    x = np.frombuffer(raw[: n * 2], dtype="<u2").astype(np.float32)
    sos = butter(2, 80.0, btype="highpass", fs=SRC_RATE, output="sos")
    x = sosfilt(sos, x).astype(np.float32)
    peak = np.percentile(np.abs(x), 99.9)
    if peak > 0:
        x = np.clip(x / peak * 0.95, -1.0, 1.0)
    x = resample_poly(x, 8, 5)  # 10k -> 16k
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()

    enc = lameenc.Encoder()
    enc.set_in_sample_rate(DST_RATE)
    enc.set_channels(1)
    enc.set_bit_rate(args.bitrate)
    enc.silence()
    mp3 = enc.encode(pcm) + enc.flush()

    out = args.out or os.path.join(os.path.dirname(__file__), "clips",
                                   f"{board}_{time.strftime('%Y%m%d_%H%M%S')}.mp3")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(mp3)
    print(f"[clip] wrote {out}  ({len(mp3) / 1024:.0f} KiB, {DST_RATE}Hz mono mp3)", flush=True)


if __name__ == "__main__":
    main()
