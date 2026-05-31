#!/usr/bin/env python3
"""Ingest server for ESP32 recorder boards.

Each board POSTs batches of raw samples to /ingest/<board_id>. The body is
opaque binary (the firmware sends little-endian uint16 ADC samples). Data is
appended per board into DATA_DIR/<board_id>/<UTC-date>.bin, with a meta.json
alongside summarizing what has been received.

Stdlib only. Run:  python3 ingest_server.py [--port 8123] [--data ./data]
"""
import argparse
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_BODY = 8 * 1024 * 1024  # 8 MiB per batch, generous upper bound

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


class Handler(BaseHTTPRequestHandler):
    server_version = "RecorderIngest/1.0"

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter, single-line
        print(f"{self.address_string()} {fmt % args}", flush=True)

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

        return self._json(200, {"ok": True, "stored": len(body), "file": os.path.basename(bin_path)})

    def do_GET(self):
        if self.path not in ("/", "/stats"):
            return self._json(404, {"error": "not found"})
        boards = []
        if os.path.isdir(DATA_DIR):
            for name in sorted(os.listdir(DATA_DIR)):
                meta_path = os.path.join(DATA_DIR, name, "meta.json")
                if os.path.isfile(meta_path):
                    boards.append(load_meta(meta_path))
        return self._json(200, {"server_time": utc_now(), "boards": boards})


def _to_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return s


def main():
    global DATA_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("INGEST_PORT", 8123)))
    ap.add_argument("--data", default=os.environ.get("INGEST_DATA", "./data"))
    args = ap.parse_args()
    DATA_DIR = os.path.abspath(args.data)
    os.makedirs(DATA_DIR, exist_ok=True)

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[ingest] listening on 127.0.0.1:{args.port}  data={DATA_DIR}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[ingest] shutting down", flush=True)


if __name__ == "__main__":
    main()
