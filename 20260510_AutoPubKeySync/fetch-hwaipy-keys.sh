#!/usr/bin/env bash
# Fetch GitHub public keys for `hwaipy` and push them into ~/.ssh/authorized_keys
# on every host listed in ~/.ssh/config_shared. Idempotent: existing keys are skipped.
# Appends a summary to ~/.ssh/push-keys.log

set -uo pipefail

# Auto-attach to a running ssh-agent if SSH_AUTH_SOCK isn't set.
# gnome-keyring exposes one at /run/user/<uid>/gcr/ssh on Ubuntu.
if [ -z "${SSH_AUTH_SOCK:-}" ]; then
  for sock in "/run/user/$(id -u)/gcr/ssh" "/run/user/$(id -u)/keyring/ssh"; do
    if [ -S "$sock" ]; then
      export SSH_AUTH_SOCK="$sock"
      echo "[agent] using $SSH_AUTH_SOCK"
      break
    fi
  done
fi

GITHUB_USER="hwaipy"
KEYS_URL="https://github.com/${GITHUB_USER}.keys"
SSH_CONFIG="$HOME/.ssh/config_shared"
LOG_FILE="$HOME/.ssh/push-keys.log"

# 1. Download keys
echo "[fetch] $KEYS_URL"
if ! KEYS=$(curl -fsSL --max-time 30 "$KEYS_URL"); then
  echo "[fetch] failed" >&2
  exit 1
fi
KEY_COUNT=$(printf "%s\n" "$KEYS" | grep -c "^ssh-" || true)
if [ "$KEY_COUNT" -eq 0 ]; then
  echo "[fetch] no ssh-* lines in response" >&2
  exit 1
fi
echo "[fetch] $KEY_COUNT keys"

# 2. Extract host aliases from config_shared (skip wildcards)
HOSTS=$(awk '/^Host / { for (i=2; i<=NF; i++) if ($i !~ /[*?]/) print $i }' "$SSH_CONFIG")
HOST_COUNT=$(printf "%s\n" "$HOSTS" | grep -c .)

# 3. Log header
TS="$(date -Iseconds)"
{
  echo "=== ssh-key push @ $TS ==="
  echo "Source: $KEYS_URL ($KEY_COUNT keys)"
  echo "Hosts:  $HOST_COUNT (from $SSH_CONFIG)"
} >> "$LOG_FILE"

# Common ssh options
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)

push_unix() {
  local host="$1"
  ssh "${SSH_OPTS[@]}" "$host" bash -s <<REMOTE 2>&1
set -u
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
added=0; existed=0
while IFS= read -r key; do
  [ -z "\$key" ] && continue
  if grep -qxF "\$key" ~/.ssh/authorized_keys; then
    existed=\$((existed+1))
  else
    printf '%s\n' "\$key" >> ~/.ssh/authorized_keys
    added=\$((added+1))
  fi
done <<KEYS
$KEYS
KEYS
echo "RESULT added=\$added existed=\$existed"
REMOTE
}

push_windows() {
  local host="$1"
  # base64 the keys to dodge PowerShell parsing quirks over stdin/CLI.
  local keys_b64
  keys_b64=$(printf '%s' "$KEYS" | base64 -w0)
  # Build the full script then ship it via -EncodedCommand (UTF-16LE base64).
  # `powershell -Command -` over stdin chokes on multi-line foreach blocks;
  # EncodedCommand bypasses both stdin parsing and shell-quoting entirely.
  local ps_script
  ps_script=$(cat <<PWSH
\$keys = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$keys_b64'))
\$dir  = Join-Path \$env:USERPROFILE '.ssh'
\$file = Join-Path \$dir 'authorized_keys'
if (-not (Test-Path \$dir))  { New-Item -ItemType Directory -Path \$dir  -Force | Out-Null }
if (-not (Test-Path \$file)) { New-Item -ItemType File      -Path \$file -Force | Out-Null }
\$existing = @(Get-Content \$file -ErrorAction SilentlyContinue)
\$added = 0; \$existed = 0
foreach (\$k in (\$keys -split [char]10)) {
  \$k = \$k.Trim()
  if (-not \$k) { continue }
  if (\$existing -contains \$k) { \$existed++ } else { Add-Content -Path \$file -Value \$k; \$added++ }
}
Write-Output "RESULT added=\$added existed=\$existed"
PWSH
)
  local enc
  enc=$(printf '%s' "$ps_script" | iconv -t UTF-16LE | base64 -w0)
  ssh -n "${SSH_OPTS[@]}" "$host" "powershell -NoProfile -EncodedCommand $enc" 2>&1
}

# Probe what's on the other end. Echoes "type|err": type=unix|windows|unreachable
probe_os() {
  local host="$1"
  local out rc
  out=$(ssh -n "${SSH_OPTS[@]}" "$host" "uname -s" 2>&1)
  rc=$?
  out=$(printf "%s" "$out" | head -1 | tr -d '\r')
  if [ $rc -ne 0 ] && echo "$out" | grep -qE 'Permission denied|Connection (refused|reset|closed)|No route to host|timed out|denied \(publickey'; then
    echo "unreachable|$out"
    return
  fi
  case "$out" in
    Linux|Darwin|FreeBSD|OpenBSD|NetBSD|DragonFly|*BSD|MINGW*|CYGWIN*) echo "unix|" ;;
    *) echo "windows|" ;;
  esac
}

# 4. Push to each host
ok=0; fail=0
while IFS= read -r host; do
  [ -z "$host" ] && continue
  printf "[push] %-12s ... " "$host"

  probe=$(probe_os "$host")
  os="${probe%%|*}"
  probe_err="${probe#*|}"

  if [ "$os" = "unreachable" ]; then
    msg="FAIL [unreach] $probe_err"
    fail=$((fail+1))
  else
    if [ "$os" = "unix" ]; then
      result=$(push_unix "$host"); rc=$?
    else
      result=$(push_windows "$host"); rc=$?
    fi
    summary=$(printf "%s\n" "$result" | grep "^RESULT " | tail -n1 || true)
    if [ $rc -eq 0 ] && [ -n "$summary" ]; then
      msg="OK [$os] ${summary#RESULT }"
      ok=$((ok+1))
    else
      err=$(printf "%s" "$result" | tr '\n' ' ' | cut -c1-200)
      msg="FAIL [$os] $err"
      fail=$((fail+1))
    fi
  fi

  echo "$msg"
  printf "  %-12s %s\n" "$host" "$msg" >> "$LOG_FILE"
done <<< "$HOSTS"

# 5. Summary footer
{
  echo "Summary: ok=$ok fail=$fail"
  echo
} >> "$LOG_FILE"

echo
echo "Summary: ok=$ok fail=$fail"
echo "Log: $LOG_FILE"
