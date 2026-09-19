#!/usr/bin/env bash
# vmf exec router: run a command inside a running box or microVM.
# Docker-style leading flags are accepted and ignored (-it, -i, -t, -ti,
# -d, --rm) — interactive tty/pty is not supported over the agent channel;
# one-shot commands are the contract.
set -euo pipefail

name=""
rest=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -it|-i|-t|-ti|-d|--interactive|--tty|--rm) shift ;;
    -h|--help) echo "usage: exec <name> [cmd...]" >&2; exit 0 ;;
    -*) echo "error: unknown flag $1" >&2; exit 2 ;;
    *) if [[ -z "$name" ]]; then name="$1"; shift; else rest+=("$1"); shift; fi ;;
  esac
done
[[ -n "$name" ]] || { echo "error: no target; usage: exec <name> [cmd...]" >&2; exit 2; }

dir="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "build/$name/vm.conf" ]]; then
  exec sh "$dir/enter.sh" "$name" ${rest[@]+"${rest[@]}"}
fi
exec sh "$dir/oci-exec.sh" "$name" ${rest[@]+"${rest[@]}"}