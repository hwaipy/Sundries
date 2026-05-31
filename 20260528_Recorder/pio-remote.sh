#!/usr/bin/env bash
# Sync this project to OfficeMac and run `pio` over ssh.
#
# Usage:
#   ./pio-remote.sh run                 # compile
#   ./pio-remote.sh run -t upload       # compile + flash
#   ./pio-remote.sh run -t clean
#   ./pio-remote.sh monitor             # serial monitor (Ctrl-C to exit)
#   ./pio-remote.sh device list
#
# Source code lives locally; build artifacts stay on the remote.
set -euo pipefail

REMOTE_HOST="OfficeMac"
REMOTE_DIR="~/pio-projects/20260528_Recorder"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure remote dir exists.
ssh "$REMOTE_HOST" "mkdir -p $REMOTE_DIR"

# Push source. Exclude build output and editor cruft.
rsync -az --delete \
  --exclude '.pio/' \
  --exclude '.vscode/' \
  --exclude '.git/' \
  --exclude '*.swp' \
  "$LOCAL_DIR/" "$REMOTE_HOST:$REMOTE_DIR/"

# Forward all args to pio on the remote, with a login shell so PATH picks up ~/.platformio/penv/bin.
# Use -t for a TTY so `pio device monitor` works interactively.
exec ssh -t "$REMOTE_HOST" "zsh -lc 'cd $REMOTE_DIR && pio $*'"
