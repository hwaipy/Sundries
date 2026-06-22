#!/usr/bin/env python3
"""
TCP receiver: accepts MPEG-TS stream from Pi sender, saves to time-segmented .ts files.
First line from sender is the device ID, used as subdirectory name.
Also maintains a live JPEG snapshot via a persistent ffmpeg process.

Usage: python receiver.py [PORT] [DATA_DIR] [SEGMENT_MINUTES]
  e.g. python receiver.py 1023 /data/recordings 1
"""

import socket
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 1023
DATA_DIR = sys.argv[2] if len(sys.argv) > 2 else "/data/recordings"
SEGMENT_MINUTES = int(sys.argv[3]) if len(sys.argv) > 3 else 1

# Live snapshot: receiver writes JPEG frames here; webgui reads them.
LIVE_JPG = os.path.join(DATA_DIR, "live.jpg")


def ts_to_mp4(ts_path):
    """Remux .ts to .mp4. Try copy first, fall back to re-encode if needed."""
    mp4_path = ts_path.rsplit(".", 1)[0] + ".mp4"
    ts_size = os.path.getsize(ts_path) if os.path.exists(ts_path) else 0
    if ts_size < 10000:
        # Too small (< 10KB), likely incomplete fragment, just delete
        os.remove(ts_path)
        print(f"  [remux] skipped tiny segment ({ts_size}B)", flush=True)
        return
    try:
        # Try 1: stream copy (fast)
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-y",
             "-analyzeduration", "10000000", "-probesize", "5000000",
             "-i", ts_path, "-c", "copy", mp4_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        if result.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
            os.remove(ts_path)
            print(f"  [remux] {os.path.basename(mp4_path)}", flush=True)
            return
        # Try 2: re-encode (slower but handles broken headers)
        if os.path.exists(mp4_path):
            os.remove(mp4_path)
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-y",
             "-analyzeduration", "10000000", "-probesize", "5000000",
             "-i", ts_path,
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
             "-c:a", "aac", "-b:a", "64k",
             mp4_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
        if result.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
            os.remove(ts_path)
            print(f"  [remux] {os.path.basename(mp4_path)} (re-encoded)", flush=True)
        else:
            if os.path.exists(mp4_path) and os.path.getsize(mp4_path) == 0:
                os.remove(mp4_path)
            print(f"  [remux] failed, keeping .ts", flush=True)
    except Exception as e:
        if os.path.exists(mp4_path) and os.path.getsize(mp4_path) == 0:
            os.remove(mp4_path)
        print(f"  [remux] error: {e}", flush=True)


def handle_client(conn, addr):
    tag = f"{addr[0]}:{addr[1]}"
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Connected: {tag}", flush=True)

    # Read device ID from first line
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(1024)
        if not chunk:
            conn.close()
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] No device ID received, closing", flush=True)
            return
        buf += chunk
    newline_pos = buf.index(b"\n")
    device_id = buf[:newline_pos].decode(errors="replace").strip()
    remaining = buf[newline_pos + 1:]

    device_id = "".join(c for c in device_id if c.isalnum() or c in "-_")
    if not device_id:
        device_id = "unknown"

    device_dir = os.path.join(DATA_DIR, device_id)
    os.makedirs(device_dir, exist_ok=True)
    print(f"  [device] {device_id} -> {device_dir}", flush=True)

    # Start persistent ffmpeg for live snapshot: reads MPEG-TS from stdin,
    # outputs JPEG at 2fps, overwriting live.jpg each time.
    live_jpg = os.path.join(DATA_DIR, f"live_{device_id}.jpg")
    snap_proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-f", "mpegts", "-i", "pipe:0",
         "-vf", "fps=2", "-q:v", "8", "-update", "1", "-y", live_jpg],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Symlink for default live view
    try:
        tmp = LIVE_JPG + ".tmp"
        os.symlink(live_jpg, tmp)
        os.replace(tmp, LIVE_JPG)
    except OSError:
        pass

    segment_sec = SEGMENT_MINUTES * 60
    total_bytes = 0
    fp = None
    seg_start = 0

    def write_data(data):
        nonlocal fp, seg_start
        now = time.time()
        if fp is None or (now - seg_start) >= segment_sec:
            if fp:
                old_path = fp.name
                fp.close()
                print(f"  [seg] Closed segment", flush=True)
                threading.Thread(target=ts_to_mp4, args=(old_path,), daemon=True).start()
            fname = datetime.now().strftime("%Y%m%d_%H%M%S") + ".ts"
            fpath = os.path.join(device_dir, fname)
            fp = open(fpath, "wb")
            seg_start = now
            print(f"  [seg] Writing: {fname}", flush=True)
        fp.write(data)
        fp.flush()

    try:
        if remaining:
            write_data(remaining)
            try:
                snap_proc.stdin.write(remaining)
            except OSError:
                pass
            total_bytes += len(remaining)

        while True:
            data = conn.recv(65536)
            if not data:
                break
            write_data(data)
            try:
                snap_proc.stdin.write(data)
            except OSError:
                pass
            total_bytes += len(data)
    except (ConnectionResetError, OSError) as e:
        print(f"  [recv] {e}", flush=True)
    finally:
        if fp:
            old_path = fp.name
            fp.close()
            threading.Thread(target=ts_to_mp4, args=(old_path,), daemon=True).start()
        try:
            snap_proc.stdin.close()
        except OSError:
            pass
        snap_proc.wait()
        conn.close()
        mb = total_bytes / (1024 * 1024)
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Disconnected: {tag} device={device_id} ({mb:.1f} MB)", flush=True)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", LISTEN_PORT))
    srv.listen(2)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Listening on :{LISTEN_PORT}", flush=True)
    print(f"  Data dir: {os.path.abspath(DATA_DIR)}", flush=True)
    print(f"  Segment length: {SEGMENT_MINUTES} min", flush=True)

    try:
        while True:
            conn, addr = srv.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
        srv.close()


if __name__ == "__main__":
    main()
