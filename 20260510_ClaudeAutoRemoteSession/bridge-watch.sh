#!/bin/bash
# Deep snapshot of a single claude --remote-control process.
# Usage: ./bridge-watch.sh <pid> <label>
set -u
PID="${1:?usage: $0 <pid> <label>}"
LABEL="${2:?usage: $0 <pid> <label>}"
OUT="/tmp/bridge-watch-${PID}-${LABEL}.txt"

if [ ! -d "/proc/$PID" ]; then
  echo "pid $PID 不存在"; exit 1
fi

{
echo "=== bridge-watch pid=$PID label=$LABEL  @$(date -Iseconds) ==="
echo ""
echo "===== cmdline ====="
tr '\0' ' ' < /proc/$PID/cmdline; echo

echo ""
echo "===== session.json ====="
if [ -f "$HOME/.claude/sessions/$PID.json" ]; then
  python3 -m json.tool < "$HOME/.claude/sessions/$PID.json"
fi

echo ""
echo "===== /proc/$PID/fd (full list, sorted) ====="
ls -l /proc/$PID/fd/ 2>/dev/null | awk 'NR>1 {print "  fd=" $NF, "->", $(NF-1), $(NF-1), $(NF-2)}' | sort -t= -k2 -n | head -60

echo ""
echo "===== ss: tcp connections ====="
ss -tpn 2>/dev/null | grep "pid=$PID," | awk '{print "  ", $1, $2, $4, "->", $5}'

echo ""
echo "===== ss: unix connections (all states) ====="
ss -xpn 2>/dev/null | grep "pid=$PID," | awk '{print "  ", $1, $2, "fd=" $5, "<->", $6}'

echo ""
echo "===== /proc/$PID/net/unix (filtered for this pid's inodes) ====="
inodes=$(ls -l /proc/$PID/fd/ 2>/dev/null | grep -oP 'socket:\[\K[0-9]+' | sort -u | tr '\n' '|' | sed 's/|$//')
if [ -n "$inodes" ]; then
  grep -E "$inodes" /proc/net/unix 2>/dev/null | awk '{print "  flags="$3, "type="$5, "state="$6, "inode="$7, "path="$8}'
fi

echo ""
echo "===== abstract sockets bound by this pid (anything with @-prefix path) ====="
grep "^[0-9a-f]" /proc/net/unix 2>/dev/null | awk '$8 ~ /^@/ {print "  " $0}' | head

echo ""
echo "===== children (live + recently exited via pidfd) ====="
pgrep -P $PID 2>/dev/null | while read child; do
  cmd=$(tr '\0' ' ' < /proc/$child/cmdline 2>/dev/null | head -c 80)
  echo "  pid=$child $cmd"
done
for fd in $(ls /proc/$PID/fd/ 2>/dev/null); do
  target=$(readlink /proc/$PID/fd/$fd 2>/dev/null)
  if [[ "$target" == "anon_inode:[pidfd]" ]]; then
    refpid=$(awk '/^Pid:/{print $2}' /proc/$PID/fdinfo/$fd 2>/dev/null)
    refalive="dead"
    [ -d "/proc/$refpid" ] && refalive="alive"
    echo "  pidfd $fd -> $refpid ($refalive)"
  fi
done

echo ""
echo "===== status snippet (Threads, VmRSS) ====="
grep -E "^(State|Threads|VmRSS|voluntary_ctxt_switches|nonvoluntary_ctxt_switches):" /proc/$PID/status

echo ""
echo "===== /api/sessions row for this pid ====="
curl -s http://127.0.0.1:1880/api/sessions | python3 -c "
import sys, json
d = json.load(sys.stdin)
for s in d['sessions']:
    if s['proc_pid'] == $PID:
        for k in ('display_name', 'tmux_alive', 'proc_alive', 'app_status', 'bridge_connected', 'heartbeat_at'):
            print(f'  {k}: {s[k]!r}')
        break
"
} > "$OUT" 2>&1

echo "wrote $OUT ($(wc -l < "$OUT") lines)"
