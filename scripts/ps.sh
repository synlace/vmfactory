#!/usr/bin/env bash
# vmf ps: microVMs known to the run state, with engine, state and ssh port.
set -euo pipefail
json=0
args=()
for a in "$@"; do
  case "$a" in
    --json) json=1 ;;
    *) args+=("$a") ;;
  esac
done
[[ ${#args[@]} -eq 0 ]] || { echo "usage: ps [--json]" >&2; exit 2; }

RUNS_DIR="${VMF_RUNS:-$HOME/.vmf/runs}"

declare -a rows=()
for conf in "$RUNS_DIR"/*.conf; do
  [[ -f "$conf" ]] || continue
  name="$(basename "$conf" .conf)"
  # shellcheck source=/dev/null
  source "$conf"
  ENGINE="${ENGINE:-krunvm}"
  PORT="${PORT:-}"
  IMAGE="${IMAGE:-}"
  rundir="${RUNDIR:-$RUNS_DIR/$name}"
  state="unknown"
  case "$ENGINE" in
    qemu|firecracker)
      pid="${PID:-}"
      if [[ -z "$pid" ]]; then
        for f in qemu.pid fc.pid; do
          [[ -f "$rundir/$f" ]] && { pid="$(cat "$rundir/$f" 2>/dev/null || true)"; break; }
        done
      fi
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then state=running; else state=stopped; fi
      ;;
    krunvm)
      if command -v krunvm >/dev/null 2>&1 && command -v buildah >/dev/null 2>&1; then
        if buildah unshare -- krunvm list 2>/dev/null | grep -qx -- "$name"; then
          state=running
        else
          state=stopped
        fi
      fi
      ;;
  esac
  # Published ports: boot-time -p plus auto-discovered (expose poller).
  ports_str=""
  if [[ -f "$rundir/ports-live.txt" ]]; then
    ports_str=$(awk '{printf "%s%s/%s", sep, $1, $2; sep=","}' "$rundir/ports-live.txt")
  fi
  [[ -n "$ports_str" ]] || ports_str="-"
  rows+=("$name|$ENGINE|$state|$PORT|$IMAGE|$ports_str")
done

if [[ $json -eq 1 ]]; then
  python3 - "${rows[@]+"${rows[@]}"}" <<'PY'
import json, sys
out = []
for r in sys.argv[1:]:
    name, engine, state, port, image, ports = r.split("|", 5)
    out.append({"name": name, "engine": engine, "state": state,
                "ssh_port": int(port) if port else None, "image": image,
                "ports": [p for p in ports.split(",") if p] if ports != "-" else []})
print(json.dumps(out, indent=2))
PY
else
  printf '%-20s %-12s %-9s %-8s %-14s %s\n' NAME ENGINE STATE SSH_PORT IMAGE PORTS
  for r in "${rows[@]+"${rows[@]}"}"; do
    IFS='|' read -r n e s p i t <<<"$r"
    printf '%-20s %-12s %-9s %-8s %-14s %s\n' "$n" "$e" "$s" "$p" "$i" "$t"
  done
fi
