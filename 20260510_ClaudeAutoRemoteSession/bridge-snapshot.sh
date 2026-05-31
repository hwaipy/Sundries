#!/bin/bash
# Snapshot all `claude --remote-control` processes' bridge-relevant state.
# Usage: ./bridge-snapshot.sh <label>
# Writes /tmp/bridge-snapshot-<label>.txt
set -u
LABEL="${1:-default}"
OUT="/tmp/bridge-snapshot-${LABEL}.txt"

{
echo "=== bridge snapshot @ $(date -Iseconds) label=$LABEL ==="
for pid in $(pgrep -f "claude --remote-control" | sort -n); do
  echo ""
  echo "--- pid $pid ---"
  cmdline=$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null | head -c 200)
  echo "  cmdline: $cmdline"

  if [ -f "$HOME/.claude/sessions/$pid.json" ]; then
    python3 - "$HOME/.claude/sessions/$pid.json" <<'PY'
import json, sys, time
d = json.load(open(sys.argv[1]))
now = time.time()
ua = d.get("updatedAt", 0) / 1000
print(f"  status:       {d.get('status')}")
print(f"  bridge_id:    {d.get('bridgeSessionId')}")
print(f"  heartbeat:    {int(now-ua)}s ago" if ua else "  heartbeat:    never")
PY
  else
    echo "  session.json: missing"
  fi

  echo "  unix sockets:"
  ss -xpn 2>/dev/null | grep "pid=$pid," | awk '{printf "    fd=%s  local=%s  peer=%s\n", $0, $5, $6}' | head -10
  if [ -z "$(ss -xpn 2>/dev/null | grep "pid=$pid,")" ]; then
    echo "    (none)"
  fi

  echo "  open file descriptors (non-/dev/pts, non-anon):"
  ls -l /proc/$pid/fd/ 2>/dev/null | awk '!/pts|anon_inode|urandom|cmdline|stat|tty/ {print "    " $NF " -> " $(NF-2) " " $(NF-1) " " $NF}' | grep -E "socket|claude|remote|rpc" | head -10
done
echo ""
echo "=== /api/sessions current view ==="
curl -s http://127.0.0.1:1880/api/sessions 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for s in d['sessions']:
        print(f\"  pid={s['proc_pid']:>6}  bridge={'Y' if s['bridge_connected'] else 'N'}  status={s['app_status']}  hb={s['heartbeat_at']}  name={s['display_name'][:40]}\")
except Exception as e:
    print(f'  (parse error: {e})')
"
} > "$OUT" 2>&1

echo "wrote $OUT  ($(wc -l < "$OUT") lines)"
