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
Usage: oci-run.sh [--rm] [--keep] [-d] [--name NAME] [--cpus N] [--memory MB]
                  [--engine qemu|krunvm] [--no-ssh]
                  [-p HOST:GUEST] [-v HOST:GUEST] [-e K=V] IMAGE [COMMAND...]

  --rm            accepted, default behavior (VM deleted on exit)
  --keep          keep the microVM after exit (re-run reuses it)
  -d|--detach     start the VM in the background, return immediately
  --no-ssh        boot the pinned image as-is (no dropbear layer, no ssh)
  --engine E      microVM engine: qemu (default) or krunvm (legacy)
  --memory MB     guest memory (default 1024)
  --net MODE      guest network: open (default) | restricted (no outbound
                  connections; published ports and ssh still work) | off
                  (no network device at all, no ssh)
  --timeout DUR   power the VM off after DUR (45s, 30m, 2h); for
                  untrusted jobs
  --disk-cap SZ   cap single-file guest writes at SZ (500M, 10G); writes
                  beyond fail with EFBIG (disk-full semantics)
  -p HOST:GUEST   publish host port to guest port (repeatable; suffix
                  /udp for UDP, e.g. 53:53/udp)
  --expose MODE   port exposure: all (default: auto-publish every port
                  the guest discovers, TCP+UDP) | declared (only -p /
                  compose-declared ports) | none. An explicit -p makes
                  declared the default
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
expose=""
name=""
cpus=""
mem=""
netmode=""
timeout_spec=""
diskcap=""
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
    --engine) [[ $# -ge 2 ]] || usage; ENGINE="$2"; shift 2 ;;
    --memory) [[ $# -ge 2 ]] || usage; mem="$2"; shift 2 ;;
    --net) [[ $# -ge 2 ]] || usage; netmode="$2"; shift 2 ;;
    --timeout) [[ $# -ge 2 ]] || usage; timeout_spec="$2"; shift 2 ;;
    --disk-cap) [[ $# -ge 2 ]] || usage; diskcap="$2"; shift 2 ;;
    -i|-t|-it|-ti|-itd|-dit) echo "note: '$1' ignored: microVMs have no PTY" >&2; shift ;;
    -p|--publish) [[ $# -ge 2 ]] || usage; ports+=("$2"); shift 2 ;;
    --expose) [[ $# -ge 2 ]] || usage; expose="$2"; shift 2 ;;
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
# Port exposure mode: "all" auto-publishes every port the guest finds
# (compose-declared plus EXPOSEd container ports, TCP+UDP); "declared"
# publishes only explicit -p / compose-declared ports; "none" publishes
# nothing. An explicit -p makes declared the default.
case "${expose:-}" in
  ""|all|declared|none) ;;
  *) echo "error: --expose must be all|declared|none" >&2; exit 2 ;;
esac
expose_mode="${expose:-all}"
if [[ -z "$expose" && ${#ports[@]} -gt 0 ]]; then
  expose_mode=declared
fi
# Compose mode: a git URL or a directory containing a compose file.
# The pipeline lives in compose-run.sh; it hands back to this script
# with a local image tag and VMF_MODE=compose + VMF_DATA_DRIVE set.
if [[ "$image" =~ ^(https?://|git@) ]]; then
  repo_src="$RUNS_DIR/.compose-src.$$"
  rm -rf "$repo_src"
  mkdir -p "$repo_src"
  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 "$image" "$repo_src" 2>&1 | tail -1
  else
    nix shell nixpkgs#git -c git clone --depth 1 "$image" "$repo_src" 2>&1 | tail -1
  fi
  export VMF_COMPOSE_SRC="$repo_src"
  name="${name:-$(basename "${image%%.git}")}"
elif [[ -d "$image" ]] && { [[ -f "$image/compose.yaml" || -f "$image/compose.yml" || -f "$image/docker-compose.yaml" || -f "$image/docker-compose.yml" ]]; }; then
  export VMF_COMPOSE_SRC="$(cd "$image" && pwd)"
  name="${name:-$(basename "$image")}"
fi
if [[ -z "${VMF_MODE:-}" && -n "${VMF_COMPOSE_SRC:-}" ]]; then
  export VMF_NAME="$name" VMF_COMPOSE_SLUG="$name"
  export VMF_RUN_DETACH="${detach:-0}" VMF_RUN_KEEP="${keep:-0}"
  export VMF_RUN_MEM="${mem:-1024}" VMF_RUN_NETMODE="${netmode:-open}"
  export VMF_RUN_TIMEOUT_SECS="${timeout_secs:-0}" VMF_RUN_CPUS="${cpus:-2}"
  export VMF_RUN_ENGINE="$ENGINE" VMF_RUN_EXPOSE="$expose_mode"
  exec bash "$(cd "$(dirname "$0")" && pwd)/compose-run.sh"
fi

# Docker-compatible normalization: unprefixed names imply docker.io
# ("alpine" -> docker.io/library/alpine, "user/repo" -> docker.io/user/repo).
# Local buildah tags (compose handoff) pass through untouched.
is_local_tag=0
case "$image" in
  vmf-compose/*|vmf/*|localhost/vmf-compose/*) is_local_tag=1 ;;
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
# Flag (--engine) wins over env; default qemu.
ENGINE="${ENGINE:-${VMF_ENGINE:-qemu}}"
case "$ENGINE" in
  qemu|krunvm|firecracker) ;;
  *) echo "error: engine '$ENGINE' is planned but not built yet (qemu|krunvm available)" >&2; exit 3 ;;
esac
MICROVM_DIR="${VMF_MICROVM:-$HOME/.local/share/vmf/microvm}"

# P0 guardrails: parse network mode, run timeout, and disk cap. The
# sandbox flags are opt-in today; the repo-run feature (P3) forces
# restricted + timeout for un-audited code.
case "$netmode" in
  ""|open|restricted|off) ;;
  *) echo "error: --net must be open|restricted|off" >&2; exit 2 ;;
esac
timeout_secs=0
if [[ -n "$timeout_spec" ]]; then
  timeout_secs=$(python3 - "$timeout_spec" <<'PY'
import re, sys
m = re.fullmatch(r"(\d+)([smh]?)", sys.argv[1])
if not m:
    print(-1)
else:
    print(int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)])
PY
)
  [[ "$timeout_secs" -gt 0 ]] || { echo "error: bad --timeout '$timeout_spec' (45s|30m|2h)" >&2; exit 2; }
fi
disk_blocks=0
if [[ -n "$diskcap" ]]; then
  disk_blocks=$(python3 - "$diskcap" <<'PY'
import re, sys
m = re.fullmatch(r"(\d+)([KMG]?)", sys.argv[1])
mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
if not m:
    print(-1)
else:
    print((int(m.group(1)) * mult[m.group(2)]) // 512)
PY
)
  [[ "$disk_blocks" -gt 0 ]] || { echo "error: bad --disk-cap '$diskcap' (500M|10G)" >&2; exit 2; }
fi

# Per-run input directory, unique per run (<name>.<run-pid>). A
# replace-run of the same name kills the old VM, and the old teardown
# deletes ITS run dir without ever racing the new run's writes on
# shared paths (the old race emptied /vmf-run mid-boot and killed init).

runid=$$
rundir="$RUNS_DIR/$name.$runid"

# Kernel + initramfs for the microVM engines. The kernel is a stock
# nixpkgs build with virtio/9p/devpts/block/squashfs/overlay/ext4 forced
# built-in (no modules) — one kernel serves the qemu engine (9p rootfs)
# and the firecracker engine (squashfs root + ext4 inputs, mmio via
# firecracker's ACPI tables). Both cached by content markers; rebuilds
# happen only when inputs change.
ensure_microvm_assets() {
  mkdir -p "$MICROVM_DIR"
  local kexpr='let pkgs = import <nixpkgs> {}; k = pkgs.lib.kernel; in
    pkgs.linux.override { ignoreConfigErrors = true; structuredExtraConfig = {
      VIRTIO = k.yes; VIRTIO_PCI = k.yes; VIRTIO_MMIO = k.yes; VIRTIO_NET = k.yes;
      VIRTIO_BLK = k.yes;
      NET_9P = k.yes; "9P_FS" = k.yes; NET_9P_VIRTIO = k.yes;
      DEVPTS_FS = k.yes; TMPFS = k.yes; DEVTMPFS = k.yes; DEVTMPFS_MOUNT = k.yes;
      SERIAL_8250 = k.yes; SERIAL_8250_CONSOLE = k.yes; UNIX = k.yes;
      BINFMT_ELF = k.yes; BINFMT_SCRIPT = k.yes;
      SQUASHFS = k.yes; OVERLAY_FS = k.yes; EXT4_FS = k.yes; }; }'
  local kpath
  kpath=$(nix build --impure --no-link --print-out-paths --expr "$kexpr" | tail -1)
  if [[ ! -f "$MICROVM_DIR/vmlinuz" || "$(<"$MICROVM_DIR/kernel-marker" 2>/dev/null)" != "$kpath" ]]; then
    rm -f "$MICROVM_DIR/vmlinuz"
    cp "$kpath/bzImage" "$MICROVM_DIR/vmlinuz"
    printf '%s\n' "$kpath" > "$MICROVM_DIR/kernel-marker"
  fi
  # Firecracker's x86 loader wants the uncompressed ELF kernel; nixpkgs
  # ships only the bzImage. Extract once per kernel marker.
  if [[ ! -f "$MICROVM_DIR/vmlinux" ]]; then
    echo "extracting vmlinux (firecracker ELF kernel)..."
    if command -v zstd >/dev/null 2>&1; then
      python3 "$(cd "$(dirname "$0")" && pwd)/extract-vmlinux.py" \
        "$MICROVM_DIR/vmlinuz" "$MICROVM_DIR/vmlinux"
    else
      nix shell nixpkgs#zstd -c python3 "$(cd "$(dirname "$0")" && pwd)/extract-vmlinux.py" \
        "$MICROVM_DIR/vmlinuz" "$MICROVM_DIR/vmlinux"
    fi
  fi
  local h stage
  h=$(cat "$(cd "$(dirname "$0")" && pwd)/guest/initramfs-init.sh" "$BUNDLE_DIR/busybox" | sha256sum | cut -c1-8)
  if [[ ! -f "$MICROVM_DIR/initramfs.cpio.gz" || "$(<"$MICROVM_DIR/initramfs-marker" 2>/dev/null)" != "$h" ]]; then
    stage=$(mktemp -d)
    mkdir -p "$stage/bin"
    cp "$BUNDLE_DIR/busybox" "$stage/bin/busybox"
    chmod 755 "$stage/bin/busybox"
    ln -s busybox "$stage/bin/sh"
    cp "$(cd "$(dirname "$0")" && pwd)/guest/initramfs-init.sh" "$stage/init"
    chmod 755 "$stage/init"
    if command -v cpio >/dev/null 2>&1; then
      (cd "$stage" && find . | cpio -o -H newc | gzip -1) > "$MICROVM_DIR/initramfs.cpio.gz"
    else
      nix shell nixpkgs#cpio nixpkgs#gzip -c sh -c "cd '$stage' && find . | cpio -o -H newc | gzip -1" \
        > "$MICROVM_DIR/initramfs.cpio.gz"
    fi
    rm -rf "$stage"
    printf '%s\n' "$h" > "$MICROVM_DIR/initramfs-marker"
  fi
}

# Docker-compatible normalization: unprefixed names imply docker.io
# ("alpine" -> docker.io/library/alpine, "user/repo" -> docker.io/user/repo).
# Local buildah tags (compose handoff) pass through untouched.
is_local_tag=0
case "$image" in
  vmf-compose/*|vmf/*|localhost/vmf-compose/*) is_local_tag=1 ;;
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
# Flag (--engine) wins over env; default qemu.
ENGINE="${ENGINE:-${VMF_ENGINE:-qemu}}"
case "$ENGINE" in
  qemu|krunvm|firecracker) ;;
  *) echo "error: engine '$ENGINE' is planned but not built yet (qemu|krunvm available)" >&2; exit 3 ;;
esac
MICROVM_DIR="${VMF_MICROVM:-$HOME/.local/share/vmf/microvm}"

# P0 guardrails: parse network mode, run timeout, and disk cap. The
# sandbox flags are opt-in today; the repo-run feature (P3) forces
# restricted + timeout for un-audited code.
case "$netmode" in
  ""|open|restricted|off) ;;
  *) echo "error: --net must be open|restricted|off" >&2; exit 2 ;;
esac
timeout_secs=0
if [[ -n "$timeout_spec" ]]; then
  timeout_secs=$(python3 - "$timeout_spec" <<'PY'
import re, sys
m = re.fullmatch(r"(\d+)([smh]?)", sys.argv[1])
if not m:
    print(-1)
else:
    print(int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)])
PY
)
  [[ "$timeout_secs" -gt 0 ]] || { echo "error: bad --timeout '$timeout_spec' (45s|30m|2h)" >&2; exit 2; }
fi
disk_blocks=0
if [[ -n "$diskcap" ]]; then
  disk_blocks=$(python3 - "$diskcap" <<'PY'
import re, sys
m = re.fullmatch(r"(\d+)([KMG]?)", sys.argv[1])
mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
if not m:
    print(-1)
else:
    print((int(m.group(1)) * mult[m.group(2)]) // 512)
PY
)
  [[ "$disk_blocks" -gt 0 ]] || { echo "error: bad --disk-cap '$diskcap' (500M|10G)" >&2; exit 2; }
fi

# Per-run input directory, unique per run (<name>.<run-pid>). A
# replace-run of the same name kills the old VM, and the old teardown
# deletes ITS run dir without ever racing the new run's writes on
# shared paths (the old race emptied /vmf-run mid-boot and killed init).

runid=$$
rundir="$RUNS_DIR/$name.$runid"

# Kernel + initramfs for the microVM engines. The kernel is a stock
# nixpkgs build with virtio/9p/devpts/block/squashfs/overlay/ext4 forced
# built-in (no modules) — one kernel serves the qemu engine (9p rootfs)
# and the firecracker engine (squashfs root + ext4 inputs, mmio via
# firecracker's ACPI tables). Both cached by content markers; rebuilds
# happen only when inputs change.
ensure_microvm_assets() {
  mkdir -p "$MICROVM_DIR"
  local kexpr='let pkgs = import <nixpkgs> {}; k = pkgs.lib.kernel; in
    pkgs.linux.override { ignoreConfigErrors = true; structuredExtraConfig = {
      VIRTIO = k.yes; VIRTIO_PCI = k.yes; VIRTIO_MMIO = k.yes; VIRTIO_NET = k.yes;
      VIRTIO_BLK = k.yes;
      NET_9P = k.yes; "9P_FS" = k.yes; NET_9P_VIRTIO = k.yes;
      DEVPTS_FS = k.yes; TMPFS = k.yes; DEVTMPFS = k.yes; DEVTMPFS_MOUNT = k.yes;
      SERIAL_8250 = k.yes; SERIAL_8250_CONSOLE = k.yes; UNIX = k.yes;
      BINFMT_ELF = k.yes; BINFMT_SCRIPT = k.yes;
      SQUASHFS = k.yes; OVERLAY_FS = k.yes; EXT4_FS = k.yes; }; }'
  local kpath
  kpath=$(nix build --impure --no-link --print-out-paths --expr "$kexpr" | tail -1)
  if [[ ! -f "$MICROVM_DIR/vmlinuz" || "$(<"$MICROVM_DIR/kernel-marker" 2>/dev/null)" != "$kpath" ]]; then
    rm -f "$MICROVM_DIR/vmlinuz"
    cp "$kpath/bzImage" "$MICROVM_DIR/vmlinuz"
    printf '%s\n' "$kpath" > "$MICROVM_DIR/kernel-marker"
  fi
  # Firecracker's x86 loader wants the uncompressed ELF kernel; nixpkgs
  # ships only the bzImage. Extract once per kernel marker.
  if [[ ! -f "$MICROVM_DIR/vmlinux" ]]; then
    echo "extracting vmlinux (firecracker ELF kernel)..."
    if command -v zstd >/dev/null 2>&1; then
      python3 "$(cd "$(dirname "$0")" && pwd)/extract-vmlinux.py" \
        "$MICROVM_DIR/vmlinuz" "$MICROVM_DIR/vmlinux"
    else
      nix shell nixpkgs#zstd -c python3 "$(cd "$(dirname "$0")" && pwd)/extract-vmlinux.py" \
        "$MICROVM_DIR/vmlinuz" "$MICROVM_DIR/vmlinux"
    fi
  fi
  local h stage
  h=$(cat "$(cd "$(dirname "$0")" && pwd)/guest/initramfs-init.sh" "$BUNDLE_DIR/busybox" | sha256sum | cut -c1-8)
  if [[ ! -f "$MICROVM_DIR/initramfs.cpio.gz" || "$(<"$MICROVM_DIR/initramfs-marker" 2>/dev/null)" != "$h" ]]; then
    stage=$(mktemp -d)
    mkdir -p "$stage/bin"
    cp "$BUNDLE_DIR/busybox" "$stage/bin/busybox"
    chmod 755 "$stage/bin/busybox"
    ln -s busybox "$stage/bin/sh"
    cp "$(cd "$(dirname "$0")" && pwd)/guest/initramfs-init.sh" "$stage/init"
    chmod 755 "$stage/init"
    if command -v cpio >/dev/null 2>&1; then
      (cd "$stage" && find . | cpio -o -H newc | gzip -1) > "$MICROVM_DIR/initramfs.cpio.gz"
    else
      nix shell nixpkgs#cpio nixpkgs#gzip -c sh -c "cd '$stage' && find . | cpio -o -H newc | gzip -1" \
        > "$MICROVM_DIR/initramfs.cpio.gz"
    fi
    rm -rf "$stage"
    printf '%s\n' "$h" > "$MICROVM_DIR/initramfs-marker"
  fi
}


# Digest pin: TOFU on first run; drift is a hard error afterwards.
pins="${VMF_OCI_PINS:-$HOME/.vmf/oci-pins}"
mkdir -p "$(dirname "$pins")"
if command -v skopeo >/dev/null 2>&1; then
  SKOPEO=(skopeo)
else
  SKOPEO=(nix shell nixpkgs#skopeo -c skopeo)
fi
digest=""
ref="$image"
if [[ "$is_local_tag" -eq 1 ]]; then
  # Local buildah tag (compose handoff): its digest was pinned by
  # compose-run.sh at build time; no registry round-trip here.
  ref="$image"
else
  digest=$("${SKOPEO[@]}" inspect --format '{{.Digest}}' "docker://$image" 2>/dev/null || true)
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
fi

# Resolve the app argv + env from the OCI config blob: krunvm ignores the
# image's ENTRYPOINT/CMD, so argv = Entrypoint+Cmd (docker semantics), env
# = image Env overridden by -e flags, cwd = WorkingDir, uid = User.
# One python pass writes the guest init inputs into the run dir.
# Config blob (Env/Cmd/WorkingDir/User): local buildah tags must be read
# through the containers-storage transport (docker:// would query the
# registry and silently fail, losing image Env -> guest PATH falls back).
# vmf-ssh:* tags normalize to localhost/vmf-ssh:* in the store.
cfg_blob=""
for tref in "containers-storage:$ref" "containers-storage:localhost/$ref" "docker://$ref"; do
  cfg_blob=$("${SKOPEO[@]}" inspect --config "$tref" 2>/dev/null) && break
  cfg_blob=""
done
# NUL-separated override list travels as a file: environment variables
# cannot carry NUL bytes, so env-var transport would silently merge
# multiple -e flags into one entry.
mkdir -p "$rundir"
printf '%s\0' "${envs[@]:-}" > "$rundir/envs-nul" 2>/dev/null || : > "$rundir/envs-nul"
VMF_ENVS_FILE="$rundir/envs-nul" python3 - "$cfg_blob" "$rundir" ${cmd_args[@]+"${cmd_args[@]}"} <<'PYEOF' >/dev/null
import json, os, shlex, sys

blob, rundir = sys.argv[1], sys.argv[2]
argv = sys.argv[3:]
config = (json.loads(blob).get("config") or {}) if blob else {}
if not argv:
    argv = list(config.get("Entrypoint") or []) + list(config.get("Cmd") or [])
if not argv:
    # Compose mode manages services via dockerd; no entrypoint needed.
    if os.environ.get("VMF_MODE") == "compose":
        argv = ["true"]
    else:
        sys.stderr.write("error: image has no default command; pass one explicitly\n"); sys.exit(1)
env = dict(kv.split("=", 1) for kv in (config.get("Env") or []) if "=" in kv)
override_path = os.environ.get("VMF_ENVS_FILE")
if override_path and os.path.exists(override_path):
    with open(override_path, "rb") as f:
        for kv in f.read().split(b"\x00"):
            kv = kv.decode("utf-8", "replace").strip()
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

# 'export' is required: the guest init sources this file, and shell
# variables set by sourcing are invisible to the child processes
# (sshd, the app) unless exported. grafana was the first image to hit
# this — its run.sh depends on the image's own GF_PATHS_* env.
with open(os.path.join(rundir, "env"), "w") as f:
    for k, v in env.items():
        f.write(f"export {k}={shlex.quote(v)}\n")
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
    "$BUNDLE_DIR/sshd" "$BUNDLE_DIR/ssh-keygen" "$BUNDLE_DIR/sshd-session" \
    "$BUNDLE_DIR/sshd-auth" "$BUNDLE_DIR/moduli" \
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
  mkdir -p "$rundir/auth"
  cp "$SSH_DIR/id_ed25519.pub" "$rundir/auth/authorized_keys"
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
[[ "$ssh" -eq 1 ]] && create_args+=(-v "$rundir:/vmf-run")
[[ -n "$cpus" ]] && create_args+=(--cpus "$cpus")

# Stop and replace a stale VM of the same name BEFORE writing state:
# the old teardown would otherwise wipe this run's state files.
if [[ "$ENGINE" == "qemu" ]]; then
  pkill -f "qemu-system.*-name $name " 2>/dev/null || true
elif [[ "$ENGINE" == "firecracker" ]]; then
  oldpid=""
  [[ -f "$RUNS_DIR/$name.conf" ]] && oldpid=$(grep -oE '^PID=[0-9]+' "$RUNS_DIR/$name.conf" | cut -d= -f2 || true)
  [[ -n "$oldpid" ]] && kill "$oldpid" 2>/dev/null || true
else
  pkill -f "krunvm start ${name} --" 2>/dev/null || true
fi
sleep 0.5

# State for `just ssh <name>`.
cat > "$RUNS_DIR/$name.conf" <<EOF
PORT=$ssh_port
IMAGE=$image
PIN=${digest:-}
DERIVED=${DERIVED_TAG:-}
ENGINE=$ENGINE
RUNDIR=$rundir
RUN=$runid
EOF

# Per-run guest inputs shared into the VM (hostname for the kernel UTS,
# engine marker for the guest init's power-off behavior, port forwards
# for slirp hostfwd).
printf '%s\n' "$name" > "$rundir/hostname"
printf '%s\n' "$ENGINE" > "$rundir/engine"
printf '%s\n' "${VMF_MODE:-direct}" > "$rundir/mode"
printf '%s\n' "$expose_mode" > "$rundir/expose"
: > "$rundir/hostfwd"
for p in "${ports[@]:-}"; do
  [[ -n "$p" && "$expose_mode" != "none" ]] || continue
  proto=tcp; spec="$p"
  if [[ "$spec" == */udp ]]; then proto=udp; spec="${spec%/udp}"
  elif [[ "$spec" == */tcp ]]; then spec="${spec%/tcp}"; fi
  printf '%s %s %s\n' "$proto" "${spec%%:*}" "${spec##*:}" >> "$rundir/hostfwd"
done

if [[ "$ENGINE" == "qemu" ]]; then
  ensure_microvm_assets
  for v in "${volumes[@]:-}"; do
    [[ -n "$v" ]] && echo "warning: -v not supported by the qemu engine yet; ignored: $v" >&2
  done
  rm -f "$RUNS_DIR/$name.log"
  echo "creating microVM '$name' from $create_ref (qemu engine)..."
  if command -v buildah >/dev/null 2>&1; then
    BUILD_BIN=(buildah)
  else
    BUILD_BIN=(nix shell nixpkgs#krunvm nixpkgs#buildah -c buildah)
  fi
  VMF_IMAGE_REF="$create_ref" VMF_NAME="$name" VMF_RUNDIR="$rundir" \
  VMF_CONF="$RUNS_DIR/$name.conf" VMF_ASSETS="$MICROVM_DIR" VMF_CONSOLE="$RUNS_DIR/$name.log" \
  VMF_NET_MODE="${netmode:-open}" VMF_TIMEOUT_SECS="$timeout_secs" \
  VMF_DISK_BLOCKS="$disk_blocks" \
  VMF_DETACH="$detach" VMF_KEEP="$keep" VMF_CPUS="${cpus:-2}" VMF_MEM="${mem:-1024}" \
  "${BUILD_BIN[@]}" unshare -- bash "$(cd "$(dirname "$0")" && pwd)/qemu-boot.sh"
  if [[ "$detach" -eq 1 ]]; then
    echo "microVM '$name' detached; console log: $RUNS_DIR/$name.log"
    echo "ssh: just ssh $name   stop: just stop $name"
  fi
  exit 0
fi

if [[ "$ENGINE" == "firecracker" ]]; then
  ensure_microvm_assets
  # Firecracker toolchain (binary + slirp4netns + mksquashfs + mke2fs).
  if command -v firecracker >/dev/null 2>&1 && command -v slirp4netns >/dev/null 2>&1 \
     && command -v mksquashfs >/dev/null 2>&1 && command -v mke2fs >/dev/null 2>&1; then
    FC=(firecracker)
  else
    echo "fetching firecracker toolchain via nix shell..."
    FC=(nix shell nixpkgs#firecracker nixpkgs#slirp4netns nixpkgs#squashfsTools nixpkgs#e2fsprogs nixpkgs#iproute2 nixpkgs#netcat -c)
  fi
  if [[ "$netmode" == "restricted" ]]; then
    echo "warning: --net restricted is not wired for firecracker yet; using open" >&2
    netmode="open"
  fi
  # Read-only squashfs of the derived image, cached by the derive tag:
  # assembly is per base+payload, not per run.
  squash="$MICROVM_DIR/fc/$DERIVED_TAG.squashfs"
  mkdir -p "$MICROVM_DIR/fc"
  if [[ ! -f "$squash" ]]; then
    echo "assembling firecracker rootfs (squashfs of $create_ref)..."
    # The squashfs must be owned by the host user; mksquashfs runs inside
    # the buildah unshare but the result is readable by everyone.
    squash_stage=$(mktemp -d)
    if command -v buildah >/dev/null 2>&1; then
      BUILD_BIN=(buildah)
    else
      BUILD_BIN=(nix shell nixpkgs#krunvm nixpkgs#buildah -c buildah)
    fi
    if command -v mksquashfs >/dev/null 2>&1; then
      SQ=(mksquashfs)
    else
      SQ=(nix shell nixpkgs#squashfsTools -c mksquashfs)
    fi
    VMF_IMAGE_REF="$create_ref" VMF_OUT="$squash_stage/rootfs.squashfs" \
      "${BUILD_BIN[@]}" unshare -- bash -c '
      set -euo pipefail
      ctr=$(buildah from "$VMF_IMAGE_REF")
      rootfs=$(buildah mount "$ctr")
      trap "buildah rm $ctr >/dev/null 2>&1 || true" EXIT
      if command -v mksquashfs >/dev/null 2>&1; then
        mksquashfs "$rootfs" "$VMF_OUT" -noappend -no-exports -quiet >/dev/null
      else
        nix shell nixpkgs#squashfsTools -c mksquashfs "$rootfs" "$VMF_OUT" -noappend -no-exports -quiet >/dev/null
      fi
    '
    mv "$squash_stage/rootfs.squashfs" "$squash"
    rm -rf "$squash_stage"
  else
    echo "firecracker rootfs cache hit: $squash"
  fi
  # Per-run inputs drive: small ext4 built WITHOUT a mount (mke2fs -d),
  # same files the 9p share carries for qemu.
  stage="$rundir/inputs"
  mkdir -p "$stage/auth"
  cp "$SSH_DIR/id_ed25519.pub" "$stage/auth/authorized_keys" 2>/dev/null || true
  for f in hostname engine mode expose hostfwd env argv.sh cwd uid; do
    [[ -f "$rundir/$f" ]] && cp "$rundir/$f" "$stage/$f"
  done
  if command -v mke2fs >/dev/null 2>&1; then
    mke2fs -q -F -t ext4 -d "$stage" "$rundir/inputs.ext4" 16M
  else
    nix shell nixpkgs#e2fsprogs -c mke2fs -q -F -t ext4 -d "$stage" "$rundir/inputs.ext4" 16M
  fi
  rm -rf "$stage"
  rm -f "$RUNS_DIR/$name.log"
  echo "creating microVM '$name' from $create_ref (firecracker engine)..."
  # Host-side expose poller: reads the guest's discovered port table
  # over ssh and adds slirp hostfwd entries for new ports through the
  # slirp API socket. Dynamic publishing is firecracker-only (qemu's
  # user-net hostfwd is fixed at boot). It starts before the boot and
  # waits for the VM to come up on its own.
  if [[ "$ssh" -eq 1 && "${netmode:-open}" != "off" ]]; then
    VMF_NAME="$name" VMF_RUNDIR="$rundir" VMF_CONF="$RUNS_DIR/$name.conf" \
      nohup bash "$(cd "$(dirname "$0")" && pwd)/expose-poller.sh" \
      >>"$rundir/expose-host.log" 2>&1 &
    disown
  fi
  FCB=("${FC[@]}")
  VMF_NAME="$name" VMF_RUNDIR="$rundir" VMF_ASSETS_SQUASHFS="$squash" \
  VMF_KERNEL="$MICROVM_DIR/vmlinux" VMF_INITRAMFS="$MICROVM_DIR/initramfs.cpio.gz" \
  VMF_CONF="$RUNS_DIR/$name.conf" VMF_CONSOLE="$RUNS_DIR/$name.log" \
  VMF_NET_MODE="${netmode:-open}" VMF_TIMEOUT_SECS="$timeout_secs" \
  VMF_DETACH="$detach" VMF_KEEP="$keep" VMF_CPUS="${cpus:-2}" VMF_MEM="${mem:-1024}" \
  "${FCB[@]}" bash "$(cd "$(dirname "$0")" && pwd)/firecracker-boot.sh"
  if [[ "$detach" -eq 1 ]]; then
    echo "microVM '$name' detached; console log: $RUNS_DIR/$name.log"
    echo "ssh: just ssh $name   stop: just stop $name"
  fi
  exit 0
fi

# Runs are disposable: a stale VM of the same name is replaced.
if [[ -n "$netmode" || "$timeout_secs" -gt 0 || "$disk_blocks" -gt 0 ]]; then
  echo "warning: --net/--timeout/--disk-cap apply to the qemu engine only; ignored on krunvm" >&2
fi
krun delete "$name" >/dev/null 2>&1 || true
echo "creating microVM '$name' from $create_ref (pulls on first use)..."
krun create "$create_ref" "${create_args[@]}"

cleanup() {
  if [[ "$keep" -eq 0 ]]; then
    krun delete "$name" >/dev/null 2>&1 || true
    rm -rf "$rundir" "$RUNS_DIR/$name.log"
    grep -q "^RUN=$runid$" "$RUNS_DIR/$name.conf" 2>/dev/null && rm -f "$RUNS_DIR/$name.conf"
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
    mapfile -t argv < <(eval "printf '%s\n' $(cat "$rundir/argv.sh")")
    start_cmd=(krun start "$name" -- ${argv[@]+"${argv[@]}"})
  fi
  (
    "${start_cmd[@]}" </dev/null >>"$console_log" 2>&1
    if [[ "$keep" -eq 0 ]]; then
      krun delete "$name" >/dev/null 2>&1 || true
      rm -rf "$rundir" "$console_log"
      grep -q "^RUN=$runid$" "$RUNS_DIR/$name.conf" 2>/dev/null && rm -f "$RUNS_DIR/$name.conf"
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
  argv_file="$rundir/argv.sh"
  mapfile -t argv < <(eval "printf '%s\n' $(cat "$argv_file")")
  set +e
  krun start "$name" -- ${argv[@]+"${argv[@]}"}
  status=$?
  set -e
fi
exit $status