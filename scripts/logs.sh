#!/usr/bin/env bash
# vmf logs: console log of a microVM (qemu writes it detached; krunvm too).
set -euo pipefail
follow=0
n=25
name=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--follow) follow=1; shift ;;
    -n) n="$2"; shift 2 ;;
    -*) echo "usage: logs NAME [-f] [-n N]" >&2; exit 2 ;;
    *) [[ -z "$name" ]] && name="$1" || { echo "usage: logs NAME [-f] [-n N]" >&2; exit 2; }; shift ;;
  esac
done
[[ -n "$name" ]] || { echo "usage: logs NAME [-f] [-n N]" >&2; exit 2; }

RUNS_DIR="${VMF_RUNS:-$HOME/.vmf/runs}"
log="$RUNS_DIR/$name.log"
[[ -f "$log" ]] || { echo "error: no console log for '$name' ($log)" >&2; exit 1; }

if [[ $follow -eq 1 ]]; then
  exec tail -F -n "$n" "$log"
fi
tail -n "$n" "$log"
