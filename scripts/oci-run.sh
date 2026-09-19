#!/usr/bin/env bash
# vmf run: run an OCI registry image as an ephemeral microVM.
# Docker-style flags; krunvm (libkrun) is the runtime engine.
#
# Digest discipline: the image digest is pinned on first use (TOFU) to
# ~/.vmf/oci-pins; drift is a hard error. `--rm` is the default: the VM
# is deleted when the process exits.
#
# Agent mode (default, when the static agent binary exists): the guest
# init is the vmf-agent, which execs the app and exposes a live exec
# channel on the agent port, so `just exec <name> <cmd>` reaches the
# running VM (real process space, like docker exec).
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: oci-run.sh [--rm] [--keep] [--name NAME] [--cpus N]
                  [-p HOST:GUEST] [-e K=V] IMAGE [COMMAND...]

  --rm            accepted, default behavior (VM deleted on exit)
  --keep          keep the microVM after exit (re-run reuses it)
  -p HOST:GUEST   publish host port to guest port (repeatable)
  -v HOST:GUEST   mount a host path into the guest (repeatable)
  -e K=V          environment for the guest process (repeatable)
  --name NAME     microVM name (default: derived from the image)
  --cpus N        vCPUs
  --no-agent      do not boot the exec agent (plain init)
  --agent-port N  host port for the exec channel (default 47770)
  IMAGE           OCI reference (registry/repo[:tag]); digest-pinned
  COMMAND...      optional command to run inside (default: image entrypoint)
EOF
  exit 2
}

ports=()
volumes=()
envs=()
name=""
cpus=""
agent=1
agent_port="47770"
keep=0
image=""
cmd_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rm) shift ;;
    --keep) keep=1; shift ;;
    --no-agent) agent=0; shift ;;
    --agent-port) [[ $# -ge 2 ]] || usage; agent_port="$2"; shift 2 ;;
    -p|--publish) [[ $# -ge 2 ]] || usage; ports+=("$2"); shift 2 ;;
    --volume|-v) [[ $# -ge 2 ]] || usage; volumes+=("$2"); shift 2 ;;
    -e|--env) [[ $# -ge 2 ]] || usage; envs+=("$2"); shift 2 ;;
    --name) [[ $# -ge 2 ]] || usage; name="$2"; shift 2 ;;
    --cpus) [[ $# -ge 2 ]] || usage; cpus="$2"; shift 2 ;;
    -h|--help) usage ;;
    --) shift; cmd_args+=("$@"); break ;;
    -*) echo "error: unknown flag $1" >&2; exit 2 ;;
    *) if [[ -z "$image" ]]; then image="$1"; shift; else cmd_args+=("$1"); shift; fi ;;
  esac
done
[[ -n "$image" ]] || usage

# Docker-compatible normalization: unprefixed names imply docker.io
# ("alpine" -> docker.io/library/alpine, "user/repo" -> docker.io/user/repo).
case "$image" in
  localhost/*|*.*/*|*:*/*) ;;                      # explicit registry present
  */*) image="docker.io/$image" ;;
  *) image="docker.io/library/$image" ;;
esac

if command -v krunvm >/dev/null 2>&1 && command -v buildah >/dev/null 2>&1; then
  krun() { buildah unshare -- krunvm "$@"; }
else
  krun() { nix shell nixpkgs#krunvm nixpkgs#buildah -c buildah unshare -- krunvm "$@"; }
fi

if [[ -z "$name" ]]; then
  name="$(basename "${image%%:*}")"
fi

# Digest pin: TOFU on first run; drift is a hard error afterwards.
pins="${VMF_OCI_PINS:-$HOME/.vmf/oci-pins}"
mkdir -p "$(dirname "$pins")"
if command -v skopeo >/dev/null 2>&1; then
  SKOPEO=(skopeo)
else
  SKOPEO=(nix shell nixpkgs#skopeo -c skopeo)
fi
digest=$("${SKOPEO[@]}" inspect --format '{{.Digest}}' "docker://$image" 2>/dev/null || true)
ref="$image"
if [[ -n "$digest" ]]; then
  pinned=$(awk -v img="$image" '$1 == img {print $2}' "$pins" 2>/dev/null || true)
  if [[ -n "$pinned" && "$pinned" != "$digest" ]]; then
    echo "error: digest drift for $image: pinned $pinned, registry $digest" >&2
    echo "update the pin in $pins deliberately, then retry" >&2
    exit 1
  fi
  if [[ -z "$pinned" ]]; then
    printf '%s %s\n' "$image" "$digest" >> "$pins"
    echo "note: digest pin recorded (TOFU): $image@$digest"
  fi
  ref="$image@$digest"
fi

# Resolve the app argv + env from the OCI config blob: krunvm ignores the
# image's ENTRYPOINT/CMD, so argv = Entrypoint+Cmd (docker semantics), env
# = image Env overridden by -e flags, cwd = WorkingDir. One python pass
# emits the agent config (and the argv used by plain mode).
cfg_blob=$("${SKOPEO[@]}" inspect --config "docker://$image" 2>/dev/null || true)
envs_nul="$(printf '%s\0' "${envs[@]:-}" || true)"
resolved_json=$(
  VMF_ENVS="$envs_nul" python3 - "$cfg_blob" ${cmd_args[@]+"${cmd_args[@]}"} <<'PYEOF'
import json, os, sys
blob = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else {}
config = blob.get("config") or {}
argv = sys.argv[2:]
if not argv:
    argv = list(config.get("Entrypoint") or []) + list(config.get("Cmd") or [])
if not argv:
    sys.stderr.write("error: image has no default command; pass one explicitly\n")
    sys.exit(1)
env = dict(kv.split("=", 1) for kv in (config.get("Env") or []) if "=" in kv)
for kv in (os.environ.get("VMF_ENVS") or "").split("\x00"):
    if kv and "=" in kv:
        k, _, v = kv.partition("=")
        env[k] = v
json.dump({"argv": argv, "env": env,
           "cwd": config.get("WorkingDir") or "/",
           "listen": "0.0.0.0:7777"}, sys.stdout)
PYEOF
)

create_args=(--name "$name")
for p in "${ports[@]:-}"; do
  [[ -n "$p" ]] && create_args+=(--port "$p")
done
for v in "${volumes[@]:-}"; do
  [[ -n "$v" ]] && create_args+=(-v "$v")
done
[[ -n "$cpus" ]] && create_args+=(--cpus "$cpus")

AGENT_DIR="${VMF_AGENT_DIR:-$HOME/.local/share/vmf/agent}"
use_agent=0
if [[ "$agent" -eq 1 && -x "$AGENT_DIR/vmf-agent" ]]; then
  use_agent=1
  create_args+=(-v "$AGENT_DIR:/vmf-agent" --port "${agent_port}:7777")
  printf '%s' "$resolved_json" > "$AGENT_DIR/config.json"
fi

# Runs are disposable: a stale VM of the same name is replaced.
krun delete "$name" >/dev/null 2>&1 || true
echo "creating microVM '$name' from $ref (pulls the image on first use)..."
krun create "$ref" "${create_args[@]}"

cleanup() {
  if [[ "$keep" -eq 0 ]]; then
    krun delete "$name" >/dev/null 2>&1 || true
  else
    echo "note: kept microVM '$name' (krunvm delete $name to remove)"
  fi
}
trap cleanup EXIT

if [[ "$use_agent" -eq 1 ]]; then
  set +e
  krun start "$name" -- /vmf-agent/vmf-agent /vmf-agent/config.json
  status=$?
  set -e
else
  mapfile -t argv < <(printf '%s' "$resolved_json" | python3 -c '
import json, sys
for a in json.load(sys.stdin):
    print(a)
')
  set +e
  krun start "$name" -- ${argv[@]+"${argv[@]}"}
  status=$?
  set -e
fi
exit $status