#!/usr/bin/env bash
# Stop a running box (ACPI then force) or a microVM (engine-aware).
# Shared by the justfile stop recipe and the vmf CLI.
set -euo pipefail
name="${1:?usage: stop.sh <name|id>}"
RUNS_DIR="${VMF_RUNS:-$HOME/.vmf/runs}"
# shellcheck source=vmf_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmf_lib.sh"

# Name (symlink holder), id, or id-prefix; legacy flat layout falls out
# of the same resolver. Docker-style: the id is the instance key, the
# name hands over on replace.
if ! vmf_instance_dir "$name"; then
  if pgrep -f "qemu-system.*-name $name" >/dev/null 2>&1; then
    (printf 'system_powerdown\n'; sleep 5) | nc -N -U "build/$name/mon.sock" >/dev/null 2>&1 || true
    sleep 5
    if pgrep -f "qemu-system.*-name $name" >/dev/null 2>&1; then
      pkill -f "qemu-system.*-name $name" 2>/dev/null || true
    fi
    echo "box $name stopped"
    exit 0
  fi
  echo "error: ${VMF_INST_ERR:-no running VM or box '$name'}" >&2
  exit 1
fi
conf="$VMF_INST_CONF"
inst_dir="$VMF_INST_DIR"
inst_id=""
[[ -n "$inst_dir" ]] && inst_id=$(basename "$inst_dir")

# shellcheck source=/dev/null
source "$conf"
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
# Instance state: the id dir, plus the name symlink ONLY when it still
# points here (a newer instance may already hold the name). Legacy flat
# files come out with the same sweep.
rm -rf "${inst_dir:-$RUNS_DIR/$name}" "$RUNS_DIR/$name".[0-9]* \
       "$RUNS_DIR/$name.conf" "$RUNS_DIR/$name.log"
if [[ -n "$inst_id" ]] && [[ "$(readlink "$RUNS_DIR/$name" 2>/dev/null)" == "$inst_id" ]]; then
  rm -f "$RUNS_DIR/$name"
fi
echo "microVM $name stopped"
exit 0
