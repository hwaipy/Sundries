#!/usr/bin/env bash
# Read serial from the remote board for N seconds (default 6).
# Usage: ./serial-read.sh [seconds]
set -euo pipefail
SECS="${1:-6}"
REMOTE_HOST="OfficeMac"
PORT="/dev/cu.usbserial-A5069RR4"
ssh "$REMOTE_HOST" "~/.platformio/penv/bin/python -c '
import serial, time, sys
s = serial.Serial()
s.port = \"$PORT\"
s.baudrate = 115200
s.timeout = 0.5
s.dtr = False          # avoid triggering the auto-reset/boot circuit
s.rts = False
s.open()
end = time.time() + $SECS
while time.time() < end:
    line = s.readline()
    if not line:
        continue
    try:
        sys.stdout.write(line.decode().rstrip() + chr(10))
    except UnicodeDecodeError:
        sys.stdout.write(repr(line) + chr(10))
    sys.stdout.flush()
'"
