#!/usr/bin/env bash
# Host-side expose poller for firecracker microVMs.
#
# The guest expose daemon (embedded in scripts/guest/init.sh) writes
# /vmf/ports-live.txt: one line per discovered listener
# ("proto guest_port target ...", target "vm" = VM-local listener,
# otherwise "ip:port" from a guest socat bridge). This poller reads the
# table over ssh and turns new (proto, guest_port) pairs into slirp
# hostfwd entries (127.0.0.1:host_port -> 10.0.2.15:guest_port, 1:1)
# through slirp4netns' API socket. Published state lands in
# $VMF_RUNDIR/ports-live.txt ("proto host_port guest_port target status")
# for `vmf ps` / `vmf url`.
#
# Boot-time -p forwards stay in the hostfwd file (firecracker-boot.sh);
# this poller seeds its table from them and handles only auto-discovered
# ports. Exits when the VM process dies or ssh keeps failing.
set -uo pipefail
# shellcheck source=vmf_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmf_lib.sh"
: "${VMF_NAME:?}" "${VMF_RUNDIR:?}" "${VMF_CONF:?}"
SSH_DIR="${VMF_SSH_DIR:-$HOME/.vmf/ssh}"
table="$VMF_RUNDIR/ports-live.txt"
deadline=$(( $(date +%s) + 86400 ))

# The state conf (PORT=...) is written just before the boot starts.
port=""
for _ in $(seq 1 120); do
  port=$(grep -oE '^PORT=[0-9]+' "$VMF_CONF" 2>/dev/null | cut -d= -f2)
  [[ -n "$port" ]] && break
  sleep 0.5
done
[[ -n "$port" ]] || { echo "vmf-expose: no ssh port in state; giving up"; exit 0; }

# Seed from the boot-time hostfwd entries (already published); a
# restarted poller must not duplicate existing lines. Guest ports
# covered by a boot hostfwd (possibly remapped) need no dynamic entry.
covered="$VMF_RUNDIR/.expose-covered"
: > "$covered"
while IFS= read -r line; do
  vmf_fwd_parse "$line" || continue
  [[ "$VMF_FWD_GUEST" == "22" ]] && target="ssh" || target="-"
  printf '%s\n' "$VMF_FWD_PROTO $VMF_FWD_GUEST" >> "$covered"
  grep -qE "^$VMF_FWD_PROTO $VMF_FWD_HOST " "$table" 2>/dev/null && continue
  printf '%s %s %s %s published\n' "$VMF_FWD_PROTO" "$VMF_FWD_HOST" "$VMF_FWD_GUEST" "$target" >> "$table"
done < "$VMF_RUNDIR/hostfwd"

ssh_opts=(-i "$SSH_DIR/id_ed25519"
  -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no
  -o ConnectTimeout=3 -o LogLevel=ERROR -o BatchMode=yes)

fails=0
echo "vmf-expose: poller up (ssh port $port)"
# fc.pid appears a second or two after the boot starts (the inner
# process writes it after bridge setup); never treat a missing pid as
# a dead VM before it exists.
pid=""
for _ in $(seq 1 240); do
  [[ -f "$VMF_RUNDIR/fc.pid" ]] && { pid=$(cat "$VMF_RUNDIR/fc.pid" 2>/dev/null); break; }
  sleep 0.5
done
while (( $(date +%s) < deadline )); do
  if [[ -f "$VMF_RUNDIR/fc.pid" ]]; then
    pid=$(cat "$VMF_RUNDIR/fc.pid" 2>/dev/null || true)
  fi
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    echo "vmf-expose: VM gone; poller exits"
    exit 0
  fi
  [[ -S "$VMF_RUNDIR/slirp-api.sock" ]] || { sleep 2; continue; }
  if ! live=$(ssh "${ssh_opts[@]}" -p "$port" root@127.0.0.1 \
        cat /vmf/ports-live.txt 2>/dev/null); then
    fails=$((fails + 1))
    (( fails > 40 )) && { echo "vmf-expose: ssh keeps failing; poller exits"; exit 0; }
    sleep 3
    continue
  fi
  fails=0
  while read -r proto gport rest; do
    [[ -n "$proto" && -n "$gport" ]] || continue
    [[ "$gport" == "22" ]] && continue
    grep -qxF "$proto $gport" "$covered" 2>/dev/null && continue
    grep -qE "^$proto $gport " "$table" 2>/dev/null && continue
    resp=$(printf '{"execute":"add_hostfwd","arguments":{"proto":"%s","host_addr":"127.0.0.1","host_port":%s,"guest_addr":"10.0.2.15","guest_port":%s}}' \
      "$proto" "$gport" "$gport" | timeout 5 nc -U "$VMF_RUNDIR/slirp-api.sock" 2>/dev/null || true)
    if [[ "$resp" == *'"return"'* ]]; then
      printf '%s %s %s %s published\n' "$proto" "$gport" "$gport" "$rest" >> "$table"
      echo "vmf-expose: published $proto $gport -> $rest"
    else
      printf '%s %s %s %s conflict\n' "$proto" "$gport" "$gport" "$rest" >> "$table"
      echo "vmf-expose: hostfwd $proto $gport failed: ${resp:-no response}"
    fi
  done <<<"$live"
  sleep 3
done
echo "vmf-expose: poller deadline reached"