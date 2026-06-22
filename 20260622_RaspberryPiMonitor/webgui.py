#!/usr/bin/env python3
"""
Web GUI for Pi Monitor: live view + history playback.
Usage: python webgui.py [PORT] [DATA_DIR]
  e.g. python webgui.py 1025 /data/recordings
"""

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs

LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 1025
DATA_DIR = sys.argv[2] if len(sys.argv) > 2 else "/data/recordings"

# Latest snapshot for live view (updated by background thread)
_snapshot_lock = threading.Lock()
_snapshot_data = b""
_snapshot_time = 0

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pi Monitor</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, sans-serif; background: #111; color: #eee; }
.tabs { display: flex; background: #222; }
.tab { padding: 12px 24px; cursor: pointer; border-bottom: 2px solid transparent; }
.tab.active { border-bottom-color: #4af; color: #4af; }
.panel { display: none; padding: 16px; }
.panel.active { display: block; }
#live-img { max-width: 100%; border-radius: 4px; }
#live-status { color: #888; font-size: 13px; margin-top: 8px; }
.video-list { list-style: none; }
.video-item {
    padding: 10px 14px; margin: 4px 0; background: #1a1a1a; border-radius: 6px;
    cursor: pointer; display: flex; justify-content: space-between; align-items: center;
}
.video-item:hover { background: #252525; }
.video-name { font-family: monospace; font-size: 14px; }
.video-size { color: #888; font-size: 13px; }
.video-device { color: #666; font-size: 12px; margin-bottom: 8px; }
#load-more {
    display: block; margin: 16px auto; padding: 10px 32px;
    background: #333; color: #ccc; border: none; border-radius: 6px; cursor: pointer;
}
#load-more:hover { background: #444; }
/* Fullscreen overlay */
.overlay {
    display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.95); z-index: 1000; justify-content: center; align-items: center;
}
.overlay.show { display: flex; }
.overlay video { max-width: 95%; max-height: 90%; border-radius: 4px; }
.overlay .close-btn {
    position: fixed; top: 16px; right: 20px; font-size: 32px; color: #fff;
    cursor: pointer; z-index: 1001; line-height: 1;
}
.overlay .close-btn:hover { color: #f66; }
</style>
</head>
<body>
<div class="tabs">
    <div class="tab active" onclick="switchTab('live')">Live</div>
    <div class="tab" onclick="switchTab('history')">History</div>
</div>
<div id="panel-live" class="panel active">
    <img id="live-img" alt="Live">
    <div id="live-status">Connecting...</div>
</div>
<div id="panel-history" class="panel">
    <ul id="video-list" class="video-list"></ul>
    <button id="load-more" onclick="loadMore()">Load more</button>
</div>
<div class="overlay" id="overlay" onclick="closeOverlay(event)">
    <span class="close-btn" onclick="closeOverlay()">&times;</span>
    <video id="overlay-video" controls autoplay></video>
</div>
<script>
function switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
    if (name === 'history' && !historyLoaded) { historyOffset = 0; loadMore(); historyLoaded = true; }
}

// Live view
let liveImg = document.getElementById('live-img');
let liveStatus = document.getElementById('live-status');
function refreshLive() {
    let img = new Image();
    let t = Date.now();
    img.onload = function() {
        liveImg.src = img.src;
        liveStatus.textContent = 'Live - ' + new Date().toLocaleTimeString();
        setTimeout(refreshLive, 500);
    };
    img.onerror = function() {
        liveStatus.textContent = 'No signal';
        setTimeout(refreshLive, 2000);
    };
    img.src = '/api/live.jpg?t=' + t;
}
refreshLive();

// History
let historyLoaded = false;
let historyOffset = 0;
const PAGE_SIZE = 20;
function loadMore() {
    fetch('/api/videos?offset=' + historyOffset + '&limit=' + PAGE_SIZE)
        .then(r => r.json())
        .then(data => {
            let ul = document.getElementById('video-list');
            let currentDevice = '';
            data.files.forEach(f => {
                if (f.device !== currentDevice) {
                    currentDevice = f.device;
                    let dh = document.createElement('li');
                    dh.className = 'video-device';
                    dh.textContent = 'Device: ' + currentDevice;
                    ul.appendChild(dh);
                }
                let li = document.createElement('li');
                li.className = 'video-item';
                li.innerHTML = '<span class="video-name">' + f.name + '</span><span class="video-size">' + f.size + '</span>';
                li.onclick = () => openVideo('/videos/' + f.device + '/' + f.name);
                ul.appendChild(li);
            });
            historyOffset += data.files.length;
            if (!data.has_more) document.getElementById('load-more').style.display = 'none';
        });
}
function openVideo(url) {
    let ov = document.getElementById('overlay');
    let vid = document.getElementById('overlay-video');
    vid.src = url;
    ov.classList.add('show');
}
function closeOverlay(e) {
    if (e && e.target !== document.getElementById('overlay') && e.target !== document.querySelector('.close-btn')) return;
    let ov = document.getElementById('overlay');
    let vid = document.getElementById('overlay-video');
    vid.pause(); vid.src = '';
    ov.classList.remove('show');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeOverlay(); });
</script>
</body>
</html>"""


def update_snapshot():
    """Background thread: read live.jpg written by receiver's persistent ffmpeg."""
    global _snapshot_data, _snapshot_time
    live_jpg = os.path.join(DATA_DIR, "live.jpg")
    last_mtime = 0
    while True:
        try:
            # Check for live_*.jpg files (written by receiver)
            target = live_jpg
            if os.path.islink(target):
                target = os.path.realpath(target)
            if not os.path.exists(target):
                # Find any live_*.jpg
                for f in os.listdir(DATA_DIR):
                    if f.startswith("live_") and f.endswith(".jpg"):
                        target = os.path.join(DATA_DIR, f)
                        break
            if os.path.exists(target):
                mt = os.path.getmtime(target)
                if mt != last_mtime:
                    with open(target, "rb") as f:
                        data = f.read()
                    if len(data) > 500:
                        with _snapshot_lock:
                            _snapshot_data = data
                            _snapshot_time = time.time()
                        last_mtime = mt
        except Exception:
            pass
        time.sleep(0.3)


def list_videos(offset=0, limit=20):
    """List MP4 files across all devices, newest first."""
    all_files = []
    if not os.path.exists(DATA_DIR):
        return [], False
    for device in sorted(os.listdir(DATA_DIR)):
        dpath = os.path.join(DATA_DIR, device)
        if not os.path.isdir(dpath):
            continue
        for f in sorted(os.listdir(dpath), reverse=True):
            if f.endswith(".mp4"):
                fpath = os.path.join(dpath, f)
                sz = os.path.getsize(fpath)
                if sz < 1024:
                    size_str = f"{sz} B"
                elif sz < 1024 * 1024:
                    size_str = f"{sz / 1024:.0f} KB"
                else:
                    size_str = f"{sz / (1024 * 1024):.1f} MB"
                all_files.append({"name": f, "device": device, "size": size_str})
    sliced = all_files[offset:offset + limit]
    has_more = (offset + limit) < len(all_files)
    return sliced, has_more


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress request logs

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            body = HTML_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/live.jpg":
            with _snapshot_lock:
                data = _snapshot_data
                age = time.time() - _snapshot_time if _snapshot_time else 999
            if data and age < 10:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(503, "No live data")

        elif path == "/api/videos":
            qs = parse_qs(parsed.query)
            offset = int(qs.get("offset", [0])[0])
            limit = int(qs.get("limit", [20])[0])
            files, has_more = list_videos(offset, limit)
            resp = json.dumps({"files": files, "has_more": has_more}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        elif path.startswith("/videos/"):
            # /videos/<device>/<filename>
            parts = path[len("/videos/"):].split("/", 1)
            if len(parts) == 2:
                device, fname = parts
                # Sanitize
                device = os.path.basename(device)
                fname = os.path.basename(fname)
                fpath = os.path.join(DATA_DIR, device, fname)
                if os.path.isfile(fpath) and fname.endswith(".mp4"):
                    fsize = os.path.getsize(fpath)
                    # Support Range requests for video seeking
                    range_header = self.headers.get("Range")
                    if range_header:
                        start, end = 0, fsize - 1
                        range_spec = range_header.replace("bytes=", "")
                        parts_r = range_spec.split("-")
                        start = int(parts_r[0]) if parts_r[0] else 0
                        end = int(parts_r[1]) if parts_r[1] else fsize - 1
                        length = end - start + 1
                        self.send_response(206)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
                        self.send_header("Content-Length", str(length))
                        self.send_header("Content-Type", "video/mp4")
                        self.send_header("Accept-Ranges", "bytes")
                        self.end_headers()
                        with open(fpath, "rb") as f:
                            f.seek(start)
                            remaining = length
                            while remaining > 0:
                                chunk = f.read(min(65536, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", "video/mp4")
                        self.send_header("Content-Length", str(fsize))
                        self.send_header("Accept-Ranges", "bytes")
                        self.end_headers()
                        with open(fpath, "rb") as f:
                            while True:
                                chunk = f.read(65536)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                else:
                    self.send_error(404)
            else:
                self.send_error(404)
        else:
            self.send_error(404)


def main():
    # Start snapshot updater
    t = threading.Thread(target=update_snapshot, daemon=True)
    t.start()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Web GUI on :{LISTEN_PORT}", flush=True)
    print(f"  Data dir: {DATA_DIR}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
        server.server_close()


if __name__ == "__main__":
    main()
