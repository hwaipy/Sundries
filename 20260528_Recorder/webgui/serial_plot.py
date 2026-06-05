#!/usr/bin/env python3
"""Local serial -> web plot for debugging the MAX9814 on GPIO7.

Reads the ESP32 DEBUG_PLOT serial stream (lines "P <mean> <min> <max>" at
~100Hz, 12-bit ADC counts) and serves a live scrolling voltage plot at
http://127.0.0.1:8000 . No external Python deps beyond pyserial (already in
the PlatformIO penv).

Run:
  ~/.platformio/penv/bin/python3 webgui/serial_plot.py
  # options: --port /dev/cu.usbserial-A5069RR4  --baud 115200  --http 8000  --vref 3.3

Only one program can hold the serial port at a time, so close any
`pio device monitor` first.
"""
import argparse
import json
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import serial  # pyserial
except ImportError:
    sys.exit("pyserial not found. Run with the pio python:\n"
             "  ~/.platformio/penv/bin/python3 webgui/serial_plot.py")

ADC_MAX = 4095            # 12-bit
_subscribers: list[queue.Queue] = []
_subs_lock = threading.Lock()
_cfg = {"vref": 3.3}


def _broadcast(msg: str) -> None:
    with _subs_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(msg)
        except queue.Full:
            pass


def reader_thread(port: str, baud: int) -> None:
    """Read the serial port forever, reconnecting on error."""
    while True:
        try:
            s = serial.Serial()
            s.port = port
            s.baudrate = baud
            s.timeout = 0.5
            s.dtr = False   # don't reset the board on connect
            s.rts = False
            s.open()
            print(f"[serial] opened {port} @ {baud}", flush=True)
            _broadcast(json.dumps({"status": "connected", "port": port}))
            while True:
                raw = s.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("P "):
                    continue
                parts = line.split()
                if len(parts) != 4:
                    continue
                try:
                    mean, mn, mx = int(parts[1]), int(parts[2]), int(parts[3])
                except ValueError:
                    continue
                _broadcast(json.dumps({"mean": mean, "min": mn, "max": mx}))
        except serial.SerialException as e:
            print(f"[serial] {e}; retrying in 2s", flush=True)
            _broadcast(json.dumps({"status": "disconnected", "error": str(e)}))
            threading.Event().wait(2.0)
        except Exception as e:  # keep the thread alive
            print(f"[serial] unexpected: {e}; retrying in 2s", flush=True)
            threading.Event().wait(2.0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = PAGE.replace("__VREF__", str(_cfg["vref"])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q: queue.Queue = queue.Queue(maxsize=1000)
            with _subs_lock:
                _subscribers.append(q)
            try:
                while True:
                    msg = q.get()
                    self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with _subs_lock:
                    if q in _subscribers:
                        _subscribers.remove(q)
            return
        self.send_response(404)
        self.end_headers()


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>GPIO7 voltage</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background:#0d1117; color:#e6edf3; font:14px/1.4 -apple-system,system-ui,sans-serif; }
  header { padding:12px 18px; display:flex; gap:24px; align-items:baseline; flex-wrap:wrap; border-bottom:1px solid #21262d; }
  h1 { font-size:15px; margin:0; font-weight:600; color:#8b949e; }
  .big { font-size:34px; font-weight:700; font-variant-numeric:tabular-nums; }
  .v { color:#58a6ff; } .pp { color:#3fb950; } .raw { color:#8b949e; font-size:13px; }
  .stat { font-size:12px; color:#6e7681; }
  #dot { display:inline-block; width:9px; height:9px; border-radius:50%; background:#f85149; margin-right:5px; }
  #dot.on { background:#3fb950; }
  canvas { display:block; width:100%; height:calc(100vh - 64px); }
  .hint { font-size:12px; color:#6e7681; }
</style>
</head>
<body>
<header>
  <h1><span id="dot"></span>GPIO7 / MAX9814</h1>
  <div>voltage <span class="big v" id="volt">--</span></div>
  <div>p-p <span class="big pp" id="pp">--</span></div>
  <div class="raw">raw mean <span id="rawmean">--</span> / 4095 &nbsp; min <span id="rmin">--</span> max <span id="rmax">--</span></div>
  <div class="stat" id="rate">0 Hz</div>
  <div class="hint">y-axis 0&ndash;__VREF__V · blue=voltage · band=min/max · dashed=~1.25V bias</div>
</header>
<canvas id="c"></canvas>
<script>
const VREF = __VREF__, ADC_MAX = 4095, BIAS = 1.25;
const N = 1000;                       // points kept (~10s at 100Hz)
const buf = new Array(N).fill(null);  // {mean,min,max} or null
let head = 0, lastMsg = 0, msgCount = 0, rateShown = 0;

const cv = document.getElementById('c'), ctx = cv.getContext('2d');
function fit(){ const r = devicePixelRatio||1;
  cv.width = cv.clientWidth*r; cv.height = cv.clientHeight*r; ctx.setTransform(r,0,0,r,0,0); }
addEventListener('resize', fit); fit();

const toV = x => x/ADC_MAX*VREF;
function push(d){ buf[head]=d; head=(head+1)%N;
  document.getElementById('volt').textContent = toV(d.mean).toFixed(3)+' V';
  document.getElementById('pp').textContent = (toV(d.max-d.min)*1000).toFixed(0)+' mV';
  document.getElementById('rawmean').textContent = d.mean;
  document.getElementById('rmin').textContent = d.min;
  document.getElementById('rmax').textContent = d.max;
  msgCount++; }

function draw(){
  const w = cv.clientWidth, h = cv.clientHeight;
  ctx.clearRect(0,0,w,h);
  const y = v => h - (v/VREF)*h;
  // grid
  ctx.strokeStyle='#21262d'; ctx.fillStyle='#6e7681'; ctx.lineWidth=1; ctx.font='11px sans-serif';
  for(let i=0;i<=VREF*2;i++){ const v=i/2; const yy=y(v);
    ctx.beginPath(); ctx.moveTo(40,yy); ctx.lineTo(w,yy); ctx.stroke();
    ctx.fillText(v.toFixed(1)+'V', 4, yy-2); }
  // bias reference
  ctx.strokeStyle='#8b949e'; ctx.setLineDash([5,4]); ctx.beginPath();
  ctx.moveTo(40,y(BIAS)); ctx.lineTo(w,y(BIAS)); ctx.stroke(); ctx.setLineDash([]);
  const x = i => 40 + (w-40)*i/(N-1);
  // min/max band
  ctx.beginPath(); let started=false;
  for(let i=0;i<N;i++){ const d=buf[(head+i)%N]; if(!d){started=false;continue;}
    const xx=x(i); if(!started){ctx.moveTo(xx,y(toV(d.max)));started=true;} else ctx.lineTo(xx,y(toV(d.max))); }
  for(let i=N-1;i>=0;i--){ const d=buf[(head+i)%N]; if(!d)continue; ctx.lineTo(x(i),y(toV(d.min))); }
  ctx.fillStyle='rgba(88,166,255,0.15)'; ctx.fill();
  // mean line
  ctx.strokeStyle='#58a6ff'; ctx.lineWidth=1.5; ctx.beginPath(); started=false;
  for(let i=0;i<N;i++){ const d=buf[(head+i)%N]; if(!d){started=false;continue;}
    const xx=x(i), yy=y(toV(d.mean)); if(!started){ctx.moveTo(xx,yy);started=true;} else ctx.lineTo(xx,yy); }
  ctx.stroke();
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);

// rate counter
setInterval(()=>{ document.getElementById('rate').textContent = msgCount+' Hz'; msgCount=0; }, 1000);
setInterval(()=>{ const dot=document.getElementById('dot');
  dot.classList.toggle('on', (performance.now()-lastMsg) < 1500); }, 500);

const es = new EventSource('/stream');
es.onmessage = e => { const d=JSON.parse(e.data);
  if(d.status){ console.log('status', d); return; }
  lastMsg = performance.now(); push(d); };
es.onerror = () => { document.getElementById('dot').classList.remove('on'); };
</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/cu.usbserial-A5069RR4")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--http", type=int, default=8000)
    ap.add_argument("--vref", type=float, default=3.3,
                    help="ADC full-scale volts for the y-axis (approx, 11dB atten ~3.1-3.3)")
    args = ap.parse_args()
    _cfg["vref"] = args.vref

    t = threading.Thread(target=reader_thread, args=(args.port, args.baud), daemon=True)
    t.start()

    httpd = ThreadingHTTPServer(("127.0.0.1", args.http), Handler)
    url = f"http://127.0.0.1:{args.http}"
    print(f"[web] open {url}  (serial {args.port} @ {args.baud})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] bye", flush=True)


if __name__ == "__main__":
    main()
