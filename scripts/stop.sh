#!/usr/bin/env bash
# Stop a running box (ACPI then force) or a microVM (engine-aware).
# Shared by the justfile stop recipe and the vmf CLI.
set -euo pipefail
name="${1:?usage: stop.sh <name>}"
RUNS_DIR="${VMF_RUNS:-$HOME/.vmf/runs}"
# shellcheck source=vmf_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmf_lib.sh"

if [[ -f "$RUNS_DIR/$name.conf" ]]; then
  # shellcheck source=/dev/null
  source "$RUNS_DIR/$name.conf"
  ENGINE="${ENGINE:-krunvm}"
  if [[ "$ENGINE" == "qemu" || "$ENGINE" == "firecracker" ]]; then
    rundir="${RUNDIR:-$RUNS_DIR/$name}"
    pid="${PID:-}"
    if [[ -z "$pid" ]]; then
      for f in qemu.pid fc.pid; do
        [[ -f "$rundir/$f" ]] && { pid=$(cat "$rundir/$f"); break; }
      done
    fi
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
    vmf_tool buildah
    [[ -n "${CTR:-}" ]] && "${TOOL[@]}" unshare -- buildah rm "$CTR" >/dev/null 2>&1 || true
    # firecracker: the slirp4netns process exits with the netns owner
  else
    pkill -f "krunvm start $name --" 2>/dev/null || true
    sleep 0.5
    vmf_run krunvm buildah -- buildah unshare -- krunvm delete "$name" >/dev/null 2>&1 || true
  fi
  rm -rf "$RUNS_DIR/$name" "$RUNS_DIR/$name".[0-9]* \
         "$RUNS_DIR/$name.conf" "$RUNS_DIR/$name.log"
  echo "microVM $name stopped"
  exit 0
fi

# Not a microVM: try a qemu box.
if pgrep -f "qemu-system.*-name $name" >/dev/null 2>&1; then
  (printf 'system_powerdown\n'; sleep 5) | nc -N -U "build/$name/mon.sock" >/dev/null 2>&1 || true
  sleep 5
  if pgrep -f "qemu-system.*-name $name" >/dev/null 2>&1; then
    pkill -f "qemu-system.*-name $name" 2>/dev/null || true
  fi
  echo "box $name stopped"
  exit 0
fi

echo "error: no running VM or box '$name'" >&2
exit 1
