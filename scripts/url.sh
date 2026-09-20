#!/usr/bin/env bash
# vmf url: reach addresses for published ports of a running microVM.
#
#   vmf url NAME            list every published port with its address
#   vmf url NAME PORT       print just the address for one port
#
# Ports come from the expose poller's table ($RUNDIR/ports-live.txt):
# boot-time -p forwards plus auto-discovered ports (--expose all).
# Published addresses bind on the host's 127.0.0.1.
set -euo pipefail
[[ $# -ge 1 ]] || { echo "usage: url NAME [PORT]" >&2; exit 2; }
name="$1"; want_port="${2:-}"

RUNS_DIR="${VMF_RUNS:-$HOME/.vmf/runs}"
conf="$RUNS_DIR/$name.conf"
[[ -f "$conf" ]] || { echo "error: no running microVM '$name'" >&2; exit 1; }
# shellcheck disable=SC1090
source "$conf"
rundir="${RUNDIR:-$RUNS_DIR/$name}"
table="$rundir/ports-live.txt"
[[ -f "$table" ]] || { echo "error: no published ports for '$name'" >&2; exit 1; }

label_target() { # proto host_port guest_port target status
  case "$4" in
    ssh) echo "127.0.0.1:$2 (ssh)" ;;
    vm)  echo "127.0.0.1:$2 ($1)" ;;
    -)   echo "127.0.0.1:$2 ($1)" ;;
    *)   echo "127.0.0.1:$2 ($1 -> $4)" ;;
  esac
}

if [[ -n "$want_port" ]]; then
  # Match either the host port or the guest port (a remapped privileged
  # port publishes on a different host number).
  line=$(grep -E "^[a-z]+ $want_port ([0-9]+|-|ssh) " "$table" | head -1) || true
  [[ -n "$line" ]] || line=$(grep -E "^[a-z]+ [0-9]+ $want_port " "$table" | head -1) || true
  [[ -n "$line" ]] || { echo "error: port $want_port not published on '$name'" >&2; exit 1; }
  read -r proto hport gport target status <<<"$line"
  [[ "$status" == published ]] || { echo "error: port $want_port could not be published (${status})" >&2; exit 1; }
  echo "127.0.0.1:$hport"
else
  while read -r proto hport gport target status; do
    label_target "$proto" "$hport" "$gport" "$target" "$status"
  done < "$table"
fi