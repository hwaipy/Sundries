#!/bin/bash
# Pi side: capture video+audio from USB camera, stream MPEG-TS over TCP to receiver.
# First sends device serial as header line, then the MPEG-TS stream.
# Usage: ./sender.sh [RECEIVER_HOST] [RECEIVER_PORT]

RECEIVER_HOST="${1:-grayfog.chat}"
RECEIVER_PORT="${2:-8223}"
VIDEO_DEV="/dev/video0"
AUDIO_DEV="hw:3,0"
DEVICE_ID=$(cat /sys/firmware/devicetree/base/serial-number 2>/dev/null | tr -d '\0')
if [ -z "$DEVICE_ID" ]; then
    DEVICE_ID=$(cat /sys/class/net/eth0/address 2>/dev/null | tr -d ':')
fi
echo "Device ID: $DEVICE_ID"

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Streaming to ${RECEIVER_HOST}:${RECEIVER_PORT} ..."
    exec 3<>/dev/tcp/"$RECEIVER_HOST"/"$RECEIVER_PORT" 2>/dev/null
    if [ $? -ne 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Connection failed, retrying in 5s..."
        sleep 5
        continue
    fi
    printf '%s\n' "$DEVICE_ID" >&3
    ffmpeg -nostdin -y \
        -f v4l2 -framerate 10 -video_size 640x480 -input_format yuyv422 -i "$VIDEO_DEV" \
        -f alsa -ac 1 -ar 16000 -i "$AUDIO_DEV" \
        -vf "drawtext=text='%{localtime}':fontsize=18:fontcolor=white:borderw=1:bordercolor=black:x=10:y=10" \
        -pix_fmt yuv420p -c:v libx264 -preset ultrafast -tune zerolatency -g 100 -b:v 400k \
        -c:a aac -b:a 64k -ar 44100 -ac 1 \
        -f mpegts pipe:1 >&3 2>/dev/null
    exec 3>&-
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Disconnected, retrying in 5s..."
    sleep 5
done
