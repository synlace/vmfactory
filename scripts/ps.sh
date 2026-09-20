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
    qemu)
      pid="${PID:-}"
      [[ -z "$pid" && -f "$rundir/qemu.pid" ]] && pid="$(cat "$rundir/qemu.pid" 2>/dev/null || true)"
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
  rows+=("$name|$ENGINE|$state|$PORT|$IMAGE")
done

if [[ $json -eq 1 ]]; then
  python3 - "${rows[@]+"${rows[@]}"}" <<'PY'
import json, sys
out = []
for r in sys.argv[1:]:
    name, engine, state, port, image = r.split("|", 4)
    out.append({"name": name, "engine": engine, "state": state,
                "ssh_port": int(port) if port else None, "image": image})
print(json.dumps(out, indent=2))
PY
else
  printf '%-20s %-8s %-9s %-8s %s\n' NAME ENGINE STATE SSH_PORT IMAGE
  for r in "${rows[@]+"${rows[@]}"}"; do
    IFS='|' read -r n e s p i <<<"$r"
    printf '%-20s %-8s %-9s %-8s %s\n' "$n" "$e" "$s" "$p" "$i"
  done
fi
