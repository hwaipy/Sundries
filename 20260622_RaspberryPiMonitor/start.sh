#!/bin/bash
export TZ=CST-8
cd /data
python3 receiver.py 1023 /data/recordings 1 >> receiver.log 2>&1 &
echo "[$(date)] Receiver started, PID=$!"
python3 webgui.py 1024 /data/recordings >> webgui.log 2>&1 &
echo "[$(date)] WebGUI started, PID=$!"
