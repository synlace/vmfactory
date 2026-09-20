#!/usr/bin/env bash
# vmf run: run an OCI registry image as an ephemeral microVM.
# Docker-style flags; krunvm (libkrun) is the runtime engine.
#
# Digest discipline: the image digest is pinned on first use (TOFU) to
# ~/.vmf/oci-pins; drift is a hard error. `--rm` is the default: the VM
# is deleted when the process exits.
#
# SSH mode (default): on first use of an image, the script derives a
# local image that adds one layer on top of the pinned bytes — a static
# dropbear + dropbearkey + busybox bundle plus a guest init script. The
# derived image is cached under a content-addressed tag and rebuilt when
# the base digest or any payload file changes. The guest init starts
# dropbear on :22 (pubkey auth only) and supervises the image
# entrypoint, so `just ssh <name> <cmd>` reaches the running microVM.
# `--no-ssh` skips the derive step and boots the pinned bytes as-is.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: oci-run.sh [--rm] [--keep] [-d] [--name NAME] [--cpus N] [--no-ssh]
                  [-p HOST:GUEST] [-v HOST:GUEST] [-e K=V] IMAGE [COMMAND...]

  --rm            accepted, default behavior (VM deleted on exit)
  --keep          keep the microVM after exit (re-run reuses it)
  -d|--detach     start the VM in the background, return immediately
  --no-ssh        boot the pinned image as-is (no dropbear layer, no ssh)
  -p HOST:GUEST   publish host port to guest port (repeatable)
  -v HOST:GUEST   mount a host path into the guest (repeatable)
  -e K=V          environment for the guest process (repeatable)
  --name NAME     microVM name (default: derived from the image)
  --cpus N        vCPUs
  -i, -t          accepted and ignored (microVMs have no PTY)
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
ssh=1
keep=0
detach=0
image=""
cmd_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rm) shift ;;
    --keep) keep=1; shift ;;
    -d|--detach) detach=1; shift ;;
    --no-ssh) ssh=0; shift ;;
    -i|-t|-it|-ti|-itd|-dit) echo "note: '$1' ignored: microVMs have no PTY" >&2; shift ;;
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

RUNS_DIR="${VMF_RUNS:-$HOME/.vmf/runs}"
SSH_DIR="${VMF_SSH_DIR:-$HOME/.vmf/ssh}"
BUNDLE_DIR="${VMF_SSH_BUNDLE:-$HOME/.local/share/vmf/ssh-bundle}"
DERIVE_DIR="${VMF_DERIVE:-$HOME/.vmf/derive}"

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
# = image Env overridden by -e flags, cwd = WorkingDir, uid = User.
# One python pass writes the guest init inputs into the run dir.
mkdir -p "$RUNS_DIR/$name/auth"
cfg_blob=$("${SKOPEO[@]}" inspect --config "docker://$ref" 2>/dev/null || true)
envs_nul="$(printf '%s\0' "${envs[@]:-}" || true)"
VMF_ENVS="$envs_nul" python3 - "$cfg_blob" "$RUNS_DIR/$name" ${cmd_args[@]+"${cmd_args[@]}"} <<'PYEOF' >/dev/null
import json, os, shlex, sys

blob, rundir = sys.argv[1], sys.argv[2]
argv = sys.argv[3:]
config = (json.loads(blob).get("config") or {}) if blob else {}
if not argv:
    argv = list(config.get("Entrypoint") or []) + list(config.get("Cmd") or [])
if not argv:
    sys.stderr.write("error: image has no default command; pass one explicitly\n"); sys.exit(1)
env = dict(kv.split("=", 1) for kv in (config.get("Env") or []) if "=" in kv)
for kv in (os.environ.get("VMF_ENVS") or "").split("\x00"):
    if kv and "=" in kv:
        k, _, v = kv.partition("=")
        env[k] = v
# Fallback tools: /vmf/bin holds a busybox symlink per applet. Append it
# to PATH so images without coreutils (distroless) still run `ls` etc.
# while real image binaries keep priority. The guest init inherits this
# PATH, so dropbear ssh sessions get the same fallback.
p = env.get("PATH", "")
env["PATH"] = (p + ":/vmf/bin") if p else "/vmf/bin:/usr/bin:/bin"

user = (config.get("User") or "").strip()
uid = ""
if user:
    u, _, g = user.partition(":")
    if u.isdigit() and (not g or g.isdigit()):
        uid = f"{u}:{g or u}"
    else:
        sys.stderr.write(f"warning: image USER {user!r} is not numeric; running as root\n")

with open(os.path.join(rundir, "env"), "w") as f:
    for k, v in env.items():
        f.write(f"{k}={shlex.quote(v)}\n")
with open(os.path.join(rundir, "argv.sh"), "w") as f:
    f.write(" ".join(shlex.quote(a) for a in argv) + "\n")
with open(os.path.join(rundir, "cwd"), "w") as f:
    f.write(config.get("WorkingDir") or "/")
with open(os.path.join(rundir, "uid"), "w") as f:
    f.write(uid)
PYEOF

# Client keypair (TOFU, per host user).
if [[ ! -f "$SSH_DIR/id_ed25519" ]]; then
  mkdir -p "$SSH_DIR"
  ssh-keygen -q -t ed25519 -N '' -f "$SSH_DIR/id_ed25519"
  echo "note: ssh client key generated: $SSH_DIR/id_ed25519"
fi

# Host port for guest 22: stable from the name, probing upward if busy.
user_ssh_port=""
for p in "${ports[@]:-}"; do
  [[ "$p" == *:22 ]] && user_ssh_port="${p%%:*}" && break
done
if [[ -z "$user_ssh_port" ]]; then
  ssh_port=$(python3 - "$name" <<'PY'
import socket, sys, zlib
p = 20000 + zlib.crc32(sys.argv[1].encode()) % 10000
for _ in range(100):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); s.close(); print(p); break
    except OSError:
        s.close(); p += 1
PY
)
  ports+=("$ssh_port:22")
else
  ssh_port="$user_ssh_port"
fi

# Static ssh bundle: build once from nixpkgs (musl-static, libc-free).
if [[ "$ssh" -eq 1 ]] && [[ ! -x "$BUNDLE_DIR/dropbear" || ! -x "$BUNDLE_DIR/busybox" ]]; then
  echo "building static ssh bundle (dropbear + busybox, one-time)..."
  mapfile -t outs < <(nix build nixpkgs#pkgsStatic.dropbear nixpkgs#pkgsStatic.busybox --print-out-paths)
  mkdir -p "$BUNDLE_DIR"
  cp "${outs[0]}/bin/dropbear" "$BUNDLE_DIR/dropbear"
  cp "${outs[0]}/bin/dropbearkey" "$BUNDLE_DIR/dropbearkey"
  cp "${outs[1]}/bin/busybox" "$BUNDLE_DIR/busybox"
fi

# Derive the ssh-enabled image from the pinned bytes: add one layer with
# the dropbear/busybox bundle and the guest init. Cached per pinned ref.
DERIVED_TAG=""
if [[ "$ssh" -eq 1 ]]; then
  # Content-addressed tag: base digest + bundle/init content. Any change
  # to the guest payload busts the derive cache.
  content=$(cat "$BUNDLE_DIR/dropbear" "$BUNDLE_DIR/dropbearkey" "$BUNDLE_DIR/busybox" \
    "$(cd "$(dirname "$0")" && pwd)/guest/init.sh" \
    "$(cd "$(dirname "$0")" && pwd)/derive.sh" | sha256sum | cut -c1-8)
  DERIVED_TAG="v$(printf '%s' "$ref" | cksum | cut -d' ' -f1 | cut -c1-10)-$content"
  DERIVED="vmf-ssh:$DERIVED_TAG"
  if command -v buildah >/dev/null 2>&1; then
    BUILD_BIN=(buildah)
  else
    BUILD_BIN=(nix shell nixpkgs#krunvm nixpkgs#buildah -c buildah)
  fi
  VMF_REF="$ref" VMF_DERIVED="$DERIVED" VMF_TAG="$DERIVED_TAG" \
  VMF_BUNDLE="$BUNDLE_DIR" VMF_INIT="$(cd "$(dirname "$0")" && pwd)/guest/init.sh" \
  VMF_DERIVE_DIR="$DERIVE_DIR" \
  "${BUILD_BIN[@]}" unshare -- bash "$(cd "$(dirname "$0")" && pwd)/derive.sh"
  cp "$SSH_DIR/id_ed25519.pub" "$RUNS_DIR/$name/auth/authorized_keys"
  create_ref="$DERIVED"
else
  create_ref="$ref"
fi

create_args=(--name "$name")
for p in "${ports[@]:-}"; do
  [[ -n "$p" ]] && create_args+=(--port "$p")
done
for v in "${volumes[@]:-}"; do
  [[ -n "$v" ]] && create_args+=(-v "$v")
done
[[ "$ssh" -eq 1 ]] && create_args+=(-v "$RUNS_DIR/$name:/vmf-run")
[[ -n "$cpus" ]] && create_args+=(--cpus "$cpus")

# State for `just ssh <name>`.
cat > "$RUNS_DIR/$name.conf" <<EOF
PORT=$ssh_port
IMAGE=$image
PIN=${digest:-}
DERIVED=${DERIVED_TAG:-}
EOF

# Runs are disposable: a stale VM of the same name is replaced.
krun delete "$name" >/dev/null 2>&1 || true
echo "creating microVM '$name' from $create_ref (pulls on first use)..."
krun create "$create_ref" "${create_args[@]}"

cleanup() {
  if [[ "$keep" -eq 0 ]]; then
    krun delete "$name" >/dev/null 2>&1 || true
    rm -rf "$RUNS_DIR/$name" "$RUNS_DIR/$name.conf" "$RUNS_DIR/$name.log"
  else
    echo "note: kept microVM '$name' (krunvm delete $name to remove)"
  fi
}
trap cleanup EXIT

if [[ "$detach" -eq 1 ]]; then
  # Detached: a background subshell owns the VM lifecycle. When the
  # entrypoint exits, the subshell tears the VM down (--rm) exactly like
  # the foreground path would.
  trap - EXIT
  console_log="$RUNS_DIR/$name.log"
  if [[ "$ssh" -eq 1 ]]; then
    start_cmd=(krun start "$name" -- /vmf/init.sh)
  else
    mapfile -t argv < <(eval "printf '%s\n' $(cat "$RUNS_DIR/$name/argv.sh")")
    start_cmd=(krun start "$name" -- ${argv[@]+"${argv[@]}"})
  fi
  (
    "${start_cmd[@]}" </dev/null >>"$console_log" 2>&1
    if [[ "$keep" -eq 0 ]]; then
      krun delete "$name" >/dev/null 2>&1 || true
      rm -rf "$RUNS_DIR/$name" "$RUNS_DIR/$name.conf" "$console_log"
    fi
  ) >/dev/null 2>&1 &
  disown
  echo "microVM '$name' detached; console log: $console_log"
  echo "ssh: just ssh $name   stop: just stop $name"
  exit 0
fi

if [[ "$ssh" -eq 1 ]]; then
  echo "microVM '$name': ssh with 'just ssh $name' (pubkey, port $ssh_port)"
  set +e
  krun start "$name" -- /vmf/init.sh
  status=$?
  set -e
else
  # Plain mode: argv straight through krunvm (no derive, no ssh).
  argv_file="$RUNS_DIR/$name/argv.sh"
  mapfile -t argv < <(eval "printf '%s\n' $(cat "$argv_file")")
  set +e
  krun start "$name" -- ${argv[@]+"${argv[@]}"}
  status=$?
  set -e
fi
exit $status