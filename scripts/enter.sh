#!/bin/sh
# SSH into a running box. Usage: scripts/enter.sh <name> [command...]
set -eu

name="${1:?usage: enter.sh <name> [command...]}"
shift || true
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

. "build/$name/vm.conf"
user="${VMF_SSH_USER:-$SSH_USER}"

if ! pgrep -f "qemu-system.*-name $name" >/dev/null 2>&1; then
  echo "error: box $name is not running; run: just boot $name" >&2
  exit 1
fi

kh="build/$name/known_hosts"

# Rebuilds and snapshot reverts put new host keys on a fixed IP. Refresh
# the cached key from the live host before connecting (boot.sh already
# resets known_hosts on every boot; this covers changes without a boot).
if scanned=$(ssh-keyscan -T 3 "$IP" 2>/dev/null) && [ -n "$scanned" ]; then
  ssh-keygen -R "$IP" -f "$kh" >/dev/null 2>&1 || true
  printf '%s\n' "$scanned" >> "$kh"
fi

exec sshpass -p "$SSH_PASS" \
  ssh -o StrictHostKeyChecking=accept-new \
      -o UserKnownHostsFile="$kh" \
      "$user@$IP" "$@"
