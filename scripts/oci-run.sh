#!/usr/bin/env bash
# vmf run: run an OCI registry image as an ephemeral microVM.
# Docker-style flags; krunvm (libkrun) is the runtime engine.
#
# Usage: scripts/oci-run.sh [--rm] [--keep] [--name NAME] [--cpus N] [--mem MB]
#                           [-p HOST:GUEST]... [-e K=V]... IMAGE [COMMAND...]
#
# Digest discipline: the image's digest is pinned on first use (TOFU) to
# ~/.vmf/oci-pins. Later runs demand the pin; a drift is a hard error.
# `--rm` is the default: the microVM is deleted when the process exits.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: oci-run.sh [--rm] [--keep] [--name NAME] [--cpus N] [--mem MB]
                  [-p HOST:GUEST]... [-e K=V]... IMAGE [COMMAND...]

  --rm            accepted, default behavior (VM deleted on exit)
  --keep          keep the microVM after exit (re-run reuses it)
  -p HOST:GUEST   publish host port to guest port (repeatable)
  -e K=V          environment for the guest process (repeatable)
  --name NAME     microVM name (default: derived from the image)
  --cpus / --mem  resources (defaults: krunvm's)
  IMAGE           OCI reference (registry/repo[:tag]); digest-pinned
  COMMAND...      optional command to run inside (default: image entrypoint)
EOF
  exit 2
}

ports=()
envs=()
name=""
keep=0
cpus=""
mem=""
image=""
cmd_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rm) shift ;;
    --keep) keep=1; shift ;;
    -p|--publish|--port) [[ $# -ge 2 ]] || usage; ports+=("$2"); shift 2 ;;
    -e|--env) [[ $# -ge 2 ]] || usage; envs+=("$2"); shift 2 ;;
    --name) [[ $# -ge 2 ]] || usage; name="$2"; shift 2 ;;
    --cpus) [[ $# -ge 2 ]] || usage; cpus="$2"; shift 2 ;;
    --mem) [[ $# -ge 2 ]] || usage; mem="$2"; shift 2 ;;
    -h|--help) usage ;;
    --) shift; cmd_args+=("$@"); break ;;
    -*) if [[ -z "$image" ]]; then echo "error: unknown flag $1" >&2; usage; else cmd_args+=("$1"); shift; fi ;;
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

# krunvm needs the container storage userns wrap; prefer system install.
if command -v krunvm >/dev/null 2>&1 && command -v buildah >/dev/null 2>&1; then
  krun() { buildah unshare -- krunvm "$@"; }
else
  krun() { nix shell nixpkgs#krunvm nixpkgs#buildah -c buildah unshare -- krunvm "$@"; }
fi

if [[ -z "$name" ]]; then
  name="${image%%@*}"
  name="${name%%:*}"
  name="$(basename "$name")"
  name="${name//[^a-zA-Z0-9_.-]/-}"
fi

# Digest pin: TOFU on first run; drift is a hard error afterwards.
pins="${VMF_OCI_PINS:-$HOME/.vmf/oci-pins}"
mkdir -p "$(dirname "$pins")"
ref="$image"
if command -v skopeo >/dev/null 2>&1; then
  SKOPEO=(skopeo)
else
  SKOPEO=(nix shell nixpkgs#skopeo -c skopeo)
fi
digest=$("${SKOPEO[@]}" inspect --format '{{.Digest}}' "docker://$image" 2>/dev/null || true)
if [[ -n "$digest" ]]; then
  pinned=$(awk -v img="$image" '$1 == img {print $2}' "$pins" 2>/dev/null || true)
  if [[ -n "$pinned" && "$pinned" != "$digest" ]]; then
    echo "error: digest drift for $image" >&2
    echo "  pinned:   $pinned" >&2
    echo "  registry: $digest" >&2
    echo "  update the pin in $pins deliberately, then retry" >&2
    exit 1
  fi
  if [[ -z "$pinned" ]]; then
    printf '%s %s\n' "$image" "$digest" >> "$pins"
    echo "note: digest pin recorded (TOFU): $image@$digest"
  fi
  ref="$image@$digest"
fi

create_args=(--name "$name")
for p in "${ports[@]:-}"; do
  [[ -n "$p" ]] && create_args+=(--port "$p")
done
[[ -n "$cpus" ]] && create_args+=(--cpus "$cpus")
[[ -n "$mem" ]] && create_args+=(--mem "$mem")

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

# krunvm does not apply the image's default ENTRYPOINT/CMD; resolve them
# from the OCI config blob when no command is given (docker semantics:
# CMD becomes the args of ENTRYPOINT).
if [[ ${#cmd_args[@]} -eq 0 ]]; then
  default_cmd=$("${SKOPEO[@]}" inspect --config "docker://$ref" 2>/dev/null | python3 -c '
import json, sys
cfg = json.load(sys.stdin).get("config") or {}
cmd = (cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or [])
if cmd:
    print("\n".join(cmd))
' 2>/dev/null || true)
  if [[ -n "$default_cmd" ]]; then
    mapfile -t resolved <<< "$default_cmd"
    cmd_args=("${resolved[@]}")
  else
    echo "error: image has no default command; pass one (e.g. $0 $image node app.js)" >&2
    exit 1
  fi
fi

# krunvm starts guests with a clean environment; -e is applied by prefixing
# the guest command with /usr/bin/env, which requires an explicit command.
start_args=()
if [[ ${#envs[@]} -gt 0 ]]; then
  start_args=("/usr/bin/env" "${envs[@]}")
fi
start_args+=("${cmd_args[@]}")
# `--` keeps dash-leading guest args (e.g. nginx's -g) out of krunvm's own
# option parser; krunvm start passes everything after it to the guest.
set +e
krun start "$name" -- ${start_args[@]+"${start_args[@]}"}
status=$?
set -e
if [[ $status -ne 0 ]]; then
  echo "note: guest exited with status $status. If this is 'listen() ... Permission denied'" >&2
  echo "on a port below 1024: rootless microVMs cannot bind privileged ports." >&2
  echo "Use a high guest port or an unprivileged image variant (e.g. nginxinc/nginx-unprivileged)." >&2
fi
exit $status
