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
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=vmf_lib.sh
. "$SCRIPTS_DIR/vmf_lib.sh"

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
  --project P     compose project inside a multi-project repo: path
                  relative to the repo root, directory name, or the
                  compose file's name field
  --intent TEXT   natural-language project/version pick; resolves to a
                  pointer from the candidate menu through an LLM (needs
                  VMF_LLM_API_KEY in ~/.vmf/env; degrades to the menu)
  --yes           accept LLM proposals without the interactive gate
                  (gap-filler compose generation)
  --expose MODE   port exposure: all (default: auto-publish every port
                  the guest discovers, TCP+UDP) | declared (only -p /
                  compose-declared ports) | none. An explicit -p makes
                  declared the default
  -v HOST:GUEST   mount a host path into the guest (repeatable)
  -e K=V          environment for the guest process (repeatable)
  --name NAME     microVM name (default: derived from the image)
  --cpus N        vCPUs
  --as KIND       resolve input-kind ambiguity explicitly; every run
                  prints the classified kind as its first line
                  (git-url dir image image-tar compose-file dockerfile
                   iso ova disk box tarball bundle)
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
runtime=""
yes_flag=0
project_hint=""
intent=""
input_as=""
name=""
cpus=""
mem=""
netmode=""
replace_flag=0
timeout_spec=""
diskcap=""
ssh=1
keep=0
detach=0
plan_mode=0
approach_list=""
skip_list=""
image=""
cmd_args=()
# Needed by the compose handoff, which runs before the later defaults.
RUNS_DIR="${VMF_RUNS:-$HOME/.vmf/runs}"
ENGINE="${ENGINE:-${VMF_ENGINE:-qemu}}"
# The verify stage re-runs this script with the ORIGINAL argv to boot
# the revised plan. The argv must survive the exec chain (pass 1 execs
# compose-run, which execs pass 3 as a new process): plain shell
# variables do not survive `exec env`, and re-capturing per pass would
# replay pass 3's own argv (the resolved image + command) instead of
# the user's input. Base64 of NUL-separated args carries it losslessly;
# exported only on the first pass.
if [[ -z "${VMF_ORIG_ARGS_B64:-}" ]]; then
  export VMF_ORIG_ARGS_B64=$(printf '%s\0' "$@" | base64 -w0)
fi

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
    --project) [[ $# -ge 2 ]] || usage; project_hint="$2"; shift 2 ;;
    --yes) yes_flag=1; shift ;;
    --runtime) [[ $# -ge 2 ]] || usage; runtime="$2"; shift 2 ;;
    --intent) [[ $# -ge 2 ]] || usage; intent="$2"; shift 2 ;;
    --expose) [[ $# -ge 2 ]] || usage; expose="$2"; shift 2 ;;
    --volume|-v) [[ $# -ge 2 ]] || usage; volumes+=("$2"); shift 2 ;;
    -e|--env) [[ $# -ge 2 ]] || usage; envs+=("$2"); shift 2 ;;
    --name) [[ $# -ge 2 ]] || usage; name="$2"; shift 2 ;;
    --replace) replace_flag=1; shift ;;
  --cpus) [[ $# -ge 2 ]] || usage; cpus="$2"; shift 2 ;;
  --as) [[ $# -ge 2 ]] || usage; input_as="$2"; shift 2 ;;
  --plan) plan_mode=1; shift ;;
  --approach) [[ $# -ge 2 ]] || usage; approach_list="$2"; shift 2 ;;
  --skip) [[ $# -ge 2 ]] || usage; skip_list="$2"; shift 2 ;;
  -h|--help) usage ;;
    --) shift; cmd_args+=("$@"); break ;;
    -*) echo "error: unknown flag $1" >&2; exit 2 ;;
    *) if [[ -z "$image" ]]; then image="$1"; shift; else cmd_args+=("$1"); shift; fi ;;
  esac
done
[[ -n "$image" ]] || usage
# Explicit-memory marker: from the RAW flag, before the handoff
# defaults turn the inherited VMF_RUN_MEM into a fake "--memory".
if [[ -n "${mem:-}" ]]; then export VMF_RUN_MEM_EXPLICIT=1; fi
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
# Bare git-host URLs (docker-CLI convenience): github.com/org/repo[.git]
# etc. No OCI registry lives at these hosts, so the mapping is safe.
if [[ "$image" =~ ^(github\.com|gitlab\.com|bitbucket\.org)/[^/]+/[^/]+$ ]]; then
  image="https://$image"
fi
# Handoff defaults: the first oci-run pass parsed the run flags and
# exported them as VMF_RUN_*; this pass prefers its own flags, then
# the handoff env, then built-ins. Applied BEFORE the classifier so
# the evidence-profile default never overrides a plan-sized
# VMF_RUN_MEM on the gap-fill pass-2 handoff.
if [[ -z "$netmode" ]]; then netmode="${VMF_RUN_NETMODE:-}"; fi
if [[ -z "$mem" ]]; then mem="${VMF_RUN_MEM:-}"; fi
if [[ -z "$cpus" ]]; then cpus="${VMF_RUN_CPUS:-}"; fi
if [[ "${VMF_RUN_DETACH:-0}" == "1" ]]; then detach=1; fi
if [[ "${VMF_RUN_KEEP:-0}" == "1" ]]; then keep=1; fi
if [[ "${VMF_RUN_SSH:-1}" == "0" ]]; then ssh=0; fi
if [[ -z "$timeout_spec" ]]; then timeout_spec="${VMF_RUN_TIMEOUT_SPEC:-}"; fi
if [[ -z "$diskcap" ]]; then diskcap="${VMF_RUN_DISKCAP:-}"; fi
expose_mode="${VMF_RUN_EXPOSE:-$expose_mode}"
case "$netmode" in
  ""|open|restricted|off|ip) ;;
  *) echo "error: --net must be open|restricted|off|ip" >&2; exit 2 ;;
esac
# Classifier: state what vmf thinks the input is — the first line of
# every run. Cheap facts only (path magic, URL shape); never a guess.
# Skipped on the compose/direct handoff passes (VMF_MODE/VMF_COMPOSE_SRC
# set by the first pass).
export VMF_SCRIPTS_DIR="${VMF_SCRIPTS_DIR:-$SCRIPTS_DIR}"
if [[ -z "${VMF_MODE:-}" && -z "${VMF_COMPOSE_SRC:-}" ]]; then
  kind=$(python3 "$VMF_SCRIPTS_DIR/vmf_plan.py" classify "$image" \
    ${input_as:+"--as" "$input_as"}) || exit $?
  # Compose runs take no command: positionals after the input are a
  # mangled flag (lost quotes) or a misuse — say so instead of
  # silently dropping them (checked before the clone, so nothing runs).
  if [[ "$kind" != "image" && ${#cmd_args[@]} -gt 0 ]]; then
    echo "error: unexpected arguments after the input: ${cmd_args[*]}" >&2
    echo "       multi-word values need quoting, e.g. --intent \"Run 3 instances\"" >&2
    exit 2
  fi
  case "$kind" in
    git-url|dir|image) ;;
    *) echo "error: '$kind' runs are planned but not built yet (supported: git-url, dir, image)" >&2; exit 3 ;;
  esac
  # Evidence profile for plain images: states the default instead of
  # implying it. User flags (--memory) always win. Skipped when an
  # --intent plan may size the VM itself (memory_mb).
  if [[ "$kind" == "image" && -z "$mem" && -z "$intent" ]]; then
    prof=$(python3 "$VMF_SCRIPTS_DIR/vmf_plan.py" profile "$kind" "$image") || prof=""
    mem="${prof##* }"
    [[ "$mem" =~ ^[0-9]+$ ]] || mem=1024
  fi
fi
# Compose mode: a git URL or a directory containing a compose file.
# The pipeline lives in compose-run.sh; it hands back to this script
# with a local image tag and VMF_MODE=compose + VMF_DATA_DRIVE set.
clone_url="$image"; proj_hint="${project_hint:-}"
# Path hints: a URL with segments beyond host/org/repo selects the
# project inside the repo (github.com/org/repo/apps/upper), matching
# the compose-spec url#path convention.
case "$image" in
  https?://*)
    p="${image#*://}"; p="${p%%#*}"; p="${p%.git}"
    if [[ "$p" == */*/*/* ]]; then
      rest="${p#*/*/*/}"
      clone_url="https://${p%/$rest}"
      proj_hint="${proj_hint:-$rest}"
    fi ;;
  git@*)
    p="${image#git@}"; h="${p%%:*}"; r="${p#*:}"; r="${r%.git}"
    if [[ "$r" == */*/* ]]; then
      rest="${r#*/*/}"
      clone_url="git@$h:${r%/$rest}.git"
      proj_hint="${proj_hint:-$rest}"
    fi ;;
  file://*)
    # file:///path/to/repo.git/rest: the hint starts after ".git/".
    p="${image#file://}"; p="${p%%#*}"
    case "$p" in
      *.git/*)
        clone_url="file://${p%%.git/*}.git"
        proj_hint="${proj_hint:-${p#*.git/}}" ;;
    esac ;;
esac
if [[ "$image" =~ ^(https?://|git@|file://) ]]; then
  repo_src="$RUNS_DIR/.compose-src.$$"
  rm -rf "$repo_src"
  mkdir -p "$repo_src"
  vmf_run git -- git clone --depth 1 "$clone_url" "$repo_src" 2>&1 | tail -1
  export VMF_COMPOSE_SRC="$repo_src" VMF_COMPOSE_URL="$clone_url"
  name="${name:-$(basename "${clone_url%%.git}")}"
  [[ -z "$proj_hint" ]] || export VMF_COMPOSE_PROJECT="$proj_hint"
  [[ -z "$intent" ]] || export VMF_RUN_INTENT="$intent"
  [[ "$yes_flag" -eq 0 ]] || export VMF_RUN_YES=1
  [[ -z "$runtime" ]] || export VMF_RUN_RUNTIME="$runtime"
elif [[ -d "$image" ]]; then  # Any local directory: compose-run.sh locates the compose file (root,
  # then a unique subdirectory) and errors clearly when there is none.
  export VMF_COMPOSE_SRC="$(cd "$image" && pwd)"
  name="${name:-$(basename "$image")}"
  [[ -z "$proj_hint" ]] || export VMF_COMPOSE_PROJECT="$proj_hint"
  [[ -z "$intent" ]] || export VMF_RUN_INTENT="$intent"
  [[ "$yes_flag" -eq 0 ]] || export VMF_RUN_YES=1
  [[ -z "$runtime" ]] || export VMF_RUN_RUNTIME="$runtime"
fi
# Instance numbering: a running lab keeps the bare name; this run
# takes a numbered instance unless --replace. Race children exempt.
numbered=$(vmf_number_instance "$name" "$replace_flag")
[[ "$numbered" == "$name" ]] || name="$numbered"
if [[ -z "${VMF_MODE:-}" && -n "${VMF_COMPOSE_SRC:-}" ]]; then
  export VMF_NAME="$name" VMF_COMPOSE_SLUG="$name"
  export VMF_RUN_DETACH="${detach:-0}" VMF_RUN_KEEP="${keep:-0}"
  export VMF_RUN_MEM="${mem:-1024}" VMF_RUN_NETMODE="${netmode:-open}"
  export VMF_RUN_TIMEOUT_SPEC="$timeout_spec" VMF_RUN_DISKCAP="$diskcap"
  export VMF_RUN_CPUS="${cpus:-2}" VMF_RUN_SSH="$ssh"
  export VMF_RUN_ENGINE="$ENGINE" VMF_RUN_EXPOSE="$expose_mode"
  if [[ "${VMF_RACE_CHILD:-0}" == "1" ]]; then
    # A race candidate: run the plan+boot chain directly.
    exec bash "$(cd "$(dirname "$0")" && pwd)/compose-run.sh"
  fi
  # Repo sources race their install approaches; --plan prints the
  # satisfiable table and boots nothing.
  if [[ "$plan_mode" -eq 1 ]]; then
    export VMF_RACE_MODE=plan
  fi
  [[ -z "$approach_list" ]] || export VMF_RACE_APPROACH="$approach_list"
  [[ -z "$skip_list" ]] || export VMF_RACE_SKIP="$skip_list"
  exec python3 "$(cd "$(dirname "$0")" && pwd)/vmf_race.py" "$VMF_COMPOSE_SRC"
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

# Both engines are needed: buildah wraps, krunvm runs. vmf_tools wants
# every package present before using the host binaries.
vmf_tools krunvm buildah
krun() { "${TOOL[@]}" buildah unshare -- krunvm "$@"; }

if [[ -z "$name" ]]; then
  name="${VMF_NAME:-$(basename "${image%%:*}")}"
fi
numbered=$(vmf_number_instance "$name" "$replace_flag")
[[ "$numbered" == "$name" ]] || name="$numbered"

RUNS_DIR="${VMF_RUNS:-$HOME/.vmf/runs}"
SSH_DIR="${VMF_SSH_DIR:-$HOME/.vmf/ssh}"
BUNDLE_DIR="${VMF_SSH_BUNDLE:-$HOME/.local/share/vmf/ssh-bundle}"
DERIVE_DIR="${VMF_DERIVE:-$HOME/.vmf/derive}"
# Flag (--engine) wins over env; default qemu.
ENGINE="${ENGINE:-${VMF_RUN_ENGINE:-${VMF_ENGINE:-qemu}}}"
case "$ENGINE" in
  qemu|krunvm|firecracker) ;;
  *) echo "error: engine '$ENGINE' is planned but not built yet (qemu|krunvm available)" >&2; exit 3 ;;
esac
MICROVM_DIR="${VMF_MICROVM:-$HOME/.local/share/vmf/microvm}"

# P0 guardrails: parse network mode, run timeout, and disk cap. The
# sandbox flags are opt-in today; the repo-run feature (P3) forces
# restricted + timeout for un-audited code.
# The VMF_RUN_* handoff defaults are applied further up (before the
# classifier): the profile default must not override a plan-sized
# VMF_RUN_MEM on the gap-fill pass-2 handoff.
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
# Instance id: a stable opaque key (docker-container-id style). All of
# this run's state lives in ~/.vmf/runs/<id>/; the name is a symlink
# that hands over to a newer instance on re-promotion. The id is never
# re-used, so replaced instances keep their evidence until cleaned.
inst_id=$(od -An -tx1 -N6 /dev/urandom | tr -d ' \n')
while [[ -e "$RUNS_DIR/$inst_id" ]]; do
  inst_id=$(od -An -tx1 -N6 /dev/urandom | tr -d ' \n')
done
inst_dir="$RUNS_DIR/$inst_id"
rundir="$inst_dir/rundir"

# Kernel + initramfs for the microVM engines. The kernel is a stock
# nixpkgs build with every needed driver forced built-in (no modules):
# virtio/9p/devpts/block/squashfs/overlay/ext4 for the engines, veth +
# bridge + cgroup v2 for docker-in-VM, the netfilter family for
# dockerd's embedded DNS resolver (127.0.0.11 DNAT) and container
# networking, and IKCONFIG so /proc/config.gz stays inspectable. One
# kernel serves the qemu engine (9p rootfs) and the firecracker engine
# (squashfs root + ext4 inputs, mmio via firecracker's ACPI tables).
# Both cached by content markers; rebuilds happen only when inputs
# change.
ensure_microvm_assets() {
  mkdir -p "$MICROVM_DIR"
  local kexpr='let pkgs = import <nixpkgs> {}; k = pkgs.lib.kernel; in
    pkgs.linux.override { ignoreConfigErrors = true; structuredExtraConfig = {
      VIRTIO = k.yes; VIRTIO_PCI = k.yes; VIRTIO_MMIO = k.yes; VIRTIO_NET = k.yes;
      VIRTIO_BLK = k.yes;
      NET_9P = k.yes; "9P_FS" = k.yes; NET_9P_VIRTIO = k.yes;
      DEVPTS_FS = k.yes; TMPFS = k.yes; DEVTMPFS = k.yes; DEVTMPFS_MOUNT = k.yes;
      SERIAL_8250 = k.yes; SERIAL_8250_CONSOLE = k.yes; UNIX = k.yes;
      PACKET = k.yes; INET = k.yes;
      BINFMT_ELF = k.yes; BINFMT_SCRIPT = k.yes;
      SQUASHFS = k.yes; OVERLAY_FS = k.yes; EXT4_FS = k.yes;
      VETH = k.yes; BRIDGE = k.yes; BRIDGE_NETFILTER = k.yes;
      IKCONFIG = k.yes; IKCONFIG_PROC = k.yes;
      CGROUP_DEVICE = k.yes; CGROUP_BPF = k.yes;
      NETFILTER = k.yes; NF_CONNTRACK = k.yes;
      NF_TABLES = k.yes; NF_TABLES_INET = k.yes; NF_TABLES_IPV4 = k.yes;
      NFT_CT = k.yes; NFT_NAT = k.yes; NFT_MASQ = k.yes; NFT_REJECT = k.yes; NFT_COUNTER = k.yes;
      IP_NF_IPTABLES = k.yes; IP_NF_FILTER = k.yes; IP_NF_NAT = k.yes;
      IP_NF_TARGET_MASQUERADE = k.yes; IP_NF_TARGET_REDIRECT = k.yes; IP_NF_MANGLE = k.yes;
      NETFILTER_XTABLES = k.yes; NETFILTER_XT_NAT = k.yes;
      NETFILTER_XT_MATCH_ADDRTYPE = k.yes; NETFILTER_XT_MATCH_CONNTRACK = k.yes;
      NETFILTER_XT_MATCH_COMMENT = k.yes; NETFILTER_XT_MATCH_MULTIPORT = k.yes;
      NETFILTER_XT_MATCH_STATE = k.yes; NETFILTER_XT_TARGET_MASQUERADE = k.yes;
      NETFILTER_XT_TARGET_REDIRECT = k.yes; NETFILTER_XT_TARGET_DNAT = k.yes;
      NF_NAT = k.yes; NF_NAT_MASQUERADE = k.yes; NF_DEFRAG_IPV4 = k.yes; }; }'
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
    vmf_run zstd -- python3 "$SCRIPTS_DIR/extract-vmlinux.py" \
      "$MICROVM_DIR/vmlinuz" "$MICROVM_DIR/vmlinux"
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
    vmf_run cpio gzip -- sh -c "cd '$stage' && find . | cpio -o -H newc | gzip -1" \
      > "$MICROVM_DIR/initramfs.cpio.gz"
    rm -rf "$stage"
    printf '%s\n' "$h" > "$MICROVM_DIR/initramfs-marker"
  fi
}


# Digest pin: TOFU on first run; drift is a hard error afterwards.
pins="${VMF_OCI_PINS:-$HOME/.vmf/oci-pins}"
mkdir -p "$(dirname "$pins")"
vmf_tool skopeo
SKOPEO=("${TOOL[@]}")
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
# Image-intent mode: --intent on a plain image (no repo, no command).
# The intent becomes a direct-mode setup plan (install + argv) via the
# draft -> context7 -> finalize flow; the gate reviews it. The VM is
# the sandbox; the plan rides the per-run inputs like the repo gap-fill.
if [[ -n "$intent" && -z "${VMF_COMPOSE_SRC:-}" && ${#cmd_args[@]} -eq 0 ]]; then
  [[ "$yes_flag" -eq 0 ]] || export VMF_RUN_YES=1
  mkdir -p "$rundir"
  intent_plan="$rundir/intent-direct.json"
  intent_rc=0
  echo "note: --intent on a plain image is experimental; a repo or an official image input is more reliable"
  # Propose FIRST: one grounded model call, seconds. The agent wakes
  # only when the boot's checks fail (the repair path in the verify
  # stage) or when the propose path itself fails — most intents never
  # pay for a session.
  if [[ "${VMF_INTENT_MODE:-auto}" != "agent" ]]; then
    if python3 "$VMF_SCRIPTS_DIR/vmf_plan.py" intent "$image" "$intent" \
        "$intent_plan"; then
      echo "intent: propose path (the agent wakes only if checks fail)"
    else
      intent_rc=1
    fi
  fi
  if [[ "$intent_rc" != 0 && "${VMF_INTENT_MODE:-auto}" != "plan" ]]; then
    # Agent session (deep fallback): boot a dedicated plan VM, drive it
    # over ssh, write back the demonstrated spec. The transcript
    # persists: a failed session keeps its plan VM and resumes on the
    # next run instead of paying from zero.
    agent_vm="${name}-planagent"
    agent_rundir=""
    agent_transcript=""
    if vmf_instance_dir "$agent_vm"; then
      agent_rundir=$(grep -oE '^RUNDIR=.*' "$VMF_INST_CONF" 2>/dev/null \
        | cut -d= -f2- || true)
      agent_transcript="$VMF_INST_DIR/transcript.json"
    fi
    echo "intent: agent session on plan VM '$agent_vm'"
    agent_alive=0
    if [[ -n "$agent_rundir" && -n "$agent_transcript" && -f "$agent_transcript" ]] && \
        bash "$SCRIPTS_DIR/ssh.sh" "$agent_vm" -- echo ok >/dev/null 2>&1; then
      agent_alive=1
      echo "intent: resuming plan VM '$agent_vm' (prior session kept)"
    else
      ( VMF_WANT_DOCKER=1 bash "$0" "$image" -d --yes --name "$agent_vm" \
          --memory "${VMF_AGENT_MEM:-2048}" \
          ${netmode:+--net "$netmode"} \
          ${timeout_spec:+--timeout "$timeout_spec"} \
          ${diskcap:+--disk-cap "$diskcap"} \
          -- /vmf/busybox sleep 100000 ) >>"$RUNS_DIR/$agent_vm.log" 2>&1 || true
      agent_rundir=""
      if vmf_instance_dir "$agent_vm"; then
        agent_rundir=$(grep -oE '^RUNDIR=.*' "$VMF_INST_CONF" 2>/dev/null \
          | cut -d= -f2- || true)
        agent_transcript="$VMF_INST_DIR/transcript.json"
      fi
      rm -f "${agent_transcript:-/nonexistent}" 2>/dev/null || true
    fi
    if python3 "$VMF_SCRIPTS_DIR/vmf_agent.py" --vm "$agent_vm" \
        --image "$image" --phrase "$intent" \
        ${agent_rundir:+--rundir "$agent_rundir"} \
        --resume "$agent_transcript" \
        --out "$intent_plan"; then
      intent_rc=0
      bash "$SCRIPTS_DIR/stop.sh" "$agent_vm" >/dev/null 2>&1 || true
      rm -f "$agent_transcript"
    else
      intent_rc=$?
      # Keep the plan VM and its transcript: the next run resumes from
      # real state instead of paying from zero.
      echo "note: agent session ended rc=$intent_rc; plan VM kept for resume"
    fi
    case "$intent_rc" in
      0) ;;
      2) echo "error: agent plan not approved" >&2; exit 2 ;;
      *) echo "error: agent session failed (rc=$intent_rc)" >&2; exit 1 ;;
    esac
  fi
  if [[ "$intent_rc" != 0 ]]; then
    echo "error: intent planning failed (no model configured?)" >&2
    exit 2
  fi
  mapfile -t cmd_args < <(python3 -c "import json,sys;[print(x) for x in json.load(open(sys.argv[1]))['command']]" "$intent_plan")
    while IFS=$'\t' read -r k v; do
      [[ -n "$k" ]] && envs+=("$k=$v")
    done < <(python3 - "$intent_plan" <<'PY'
import json, sys
for k, v in json.load(open(sys.argv[1])).get("env", {}).items():
    print("%s\t%s" % (k, v))
PY
)
    # Direct plans declare the guest tcp ports the app listens on; the
    # host publishes each 1:1. qemu forwards only at boot, so they ride
    # the -p list.
    while IFS= read -r gp; do
      [[ -n "$gp" ]] && ports+=("$gp:$gp")
    done < <(python3 -c "import json,sys;[print(x) for x in json.load(open(sys.argv[1])).get('ports',[])]" "$intent_plan")
    if [[ "$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('needs_docker',False))" "$intent_plan")" == "True" ]]; then
      VMF_WANT_DOCKER=1
    fi
    VMF_INSTALL_CMD="$(python3 -c "import json,sys;print('\n'.join(json.load(open(sys.argv[1])).get('install',[])))" "$intent_plan")"
    export VMF_INSTALL_CMD
    # Plan sizing: raise the VM memory when the plan asks for more and
    # the user gave no explicit --memory (an explicit flag always wins).
    pmb="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('memory_mb',0))" "$intent_plan")"
    if [[ -n "$pmb" && "$pmb" -gt 0 && -z "${mem:-}" ]]; then
      mem="$pmb"
    fi
    echo "intent: plan applied to $image (${#cmd_args[@]}-arg command)"
    # Host-side image supply (row 3): direct plans declare images[]; the
    # host pulls with its own trust, pins TOFU, and archives for the
    # guest's docker load at init — the guest never dials a registry.
    n=0
    while IFS= read -r imgref; do
      [[ -n "$imgref" ]] || continue
      n=$((n + 1))
      echo "intent: supplying image $imgref from the host..."
      bash "$SCRIPTS_DIR/image-supply.sh" "$imgref" \
        "$rundir/images-$n.tar" || {
        echo "error: image supply failed for $imgref" >&2
        exit 1
      }
    done < <(python3 -c "import json,sys;[print(x) for x in json.load(open(sys.argv[1])).get('images',[])]" "$intent_plan")
fi
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
# socat joins it for the guest loopback bridge (a guest app bound to
# 127.0.0.1 is unreachable through slirp hostfwd; the bridge exposes it
# on the VM address the forward actually targets).
if [[ "$ssh" -eq 1 ]] && [[ ! -x "$BUNDLE_DIR/dropbear" || ! -x "$BUNDLE_DIR/busybox" || ! -x "$BUNDLE_DIR/socat" ]]; then
  echo "building static ssh bundle (dropbear + busybox + socat, one-time)..."
  mapfile -t outs < <(nix build nixpkgs#pkgsStatic.dropbear nixpkgs#pkgsStatic.busybox nixpkgs#pkgsStatic.socat --print-out-paths)
  mkdir -p "$BUNDLE_DIR"
  cp "${outs[0]}/bin/dropbear" "$BUNDLE_DIR/dropbear"
  cp "${outs[0]}/bin/dropbearkey" "$BUNDLE_DIR/dropbearkey"
  cp "${outs[1]}/bin/busybox" "$BUNDLE_DIR/busybox"
  [[ -x "${outs[2]}/bin/socat" ]] && cp "${outs[2]}/bin/socat" "$BUNDLE_DIR/socat"
fi

# Derive the ssh-enabled image from the pinned bytes: add one layer with
# the dropbear/busybox bundle and the guest init. Cached per pinned ref.
DERIVED_TAG=""
if [[ "$ssh" -eq 1 ]]; then
  # Content-addressed tag: base digest + bundle/init content. Any change
  # to the guest payload busts the derive cache.
  bundle_hash_files=("$BUNDLE_DIR/dropbear" "$BUNDLE_DIR/dropbearkey" \
    "$BUNDLE_DIR/busybox" "$BUNDLE_DIR/sshd" "$BUNDLE_DIR/ssh-keygen" \
    "$BUNDLE_DIR/sshd-session" "$BUNDLE_DIR/sshd-auth" "$BUNDLE_DIR/moduli" \
    "$(cd "$(dirname "$0")" && pwd)/guest/init.sh" \
    "$(cd "$(dirname "$0")" && pwd)/guest/expose.sh" \
    "$(cd "$(dirname "$0")" && pwd)/derive.sh")
  [[ -f "$BUNDLE_DIR/socat" ]] && bundle_hash_files+=("$BUNDLE_DIR/socat")
  content=$(cat "${bundle_hash_files[@]}" | sha256sum | cut -c1-8)
  DERIVED_TAG="v$(printf '%s' "$ref" | cksum | cut -d' ' -f1 | cut -c1-10)-$content"
  DERIVED="vmf-ssh:$DERIVED_TAG"
  vmf_tool buildah krunvm buildah
  BUILD_BIN=("${TOOL[@]}")
  VMF_REF="$ref" VMF_DERIVED="$DERIVED" VMF_TAG="$DERIVED_TAG" \
  VMF_BUNDLE="$BUNDLE_DIR" VMF_INIT="$(cd "$(dirname "$0")" && pwd)/guest/init.sh" \
  VMF_EXPOSE="$(cd "$(dirname "$0")" && pwd)/guest/expose.sh" \
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
  oldpid=""
  if vmf_instance_dir "$name"; then
    [[ -f "$VMF_INST_CONF" ]] && oldpid=$(grep -oE '^PID=[0-9]+' "$VMF_INST_CONF" | cut -d= -f2 || true)
  fi
  [[ -n "$oldpid" ]] && kill "$oldpid" 2>/dev/null || true
else
  pkill -f "krunvm start ${name} --" 2>/dev/null || true
fi
sleep 0.5

# State for `just ssh <name>` and `just ps`: one conf per instance,
# inside the id-keyed dir. SRC records the user input, APPROACH the
# install approach (race winner or direct), TARGET the rendered
# deliverable the verify stage fills in after a pass.
mkdir -p "$inst_dir"
cat > "$inst_dir/conf" <<EOF
ID=$inst_id
NAME=$name
SRC=$image
APPROACH=${VMF_APPROACH:-image}
PORT=$ssh_port
IMAGE=$image
PIN=${digest:-}
DERIVED=${DERIVED_TAG:-}
ENGINE=$ENGINE
RUNDIR=$rundir
RUN=$runid
EOF
# The name resolves to the current holder; a replace-run hands it over.
ln -sfn "$inst_id" "$RUNS_DIR/$name"

# Per-run guest inputs shared into the VM (hostname for the kernel UTS,
# engine marker for the guest init's power-off behavior, port forwards
# for slirp hostfwd).
printf '%s\n' "$name" > "$rundir/hostname"
printf '%s\n' "$ENGINE" > "$rundir/engine"
printf '%s\n' "${VMF_MODE:-direct}" > "$rundir/mode"
printf '%s\n' "$expose_mode" > "$rundir/expose"
# Host CA trust: the host already verifies what it trusts (registry TLS,
# interception CAs); the guest inherits the same anchors so its own TLS
# (dockerd, package fetches) verifies instead of failing.
if [[ -f /etc/ssl/certs/ca-certificates.crt ]]; then
  cp /etc/ssl/certs/ca-certificates.crt "$rundir/ca-bundle.crt"
fi
# Gap-fill direct mode: a one-shot install script and the repo tar ride
# the per-run inputs; the guest stages the repo at /workspace, runs the
# install at boot, then execs the app. The VM is the sandbox.
if [[ -n "${VMF_INSTALL_CMD:-}" ]]; then
  printf '%s\n' "$VMF_INSTALL_CMD" > "$rundir/install.sh"
  chmod 755 "$rundir/install.sh"
fi
# Every boot carries the repo: the direct flow untars it to /workspace
# for the install, and an in-guest compose build resolves its contexts
# against it. Callers that only know the compose source get it here.
[[ -n "${VMF_REPO_DIR:-}" ]] || VMF_REPO_DIR="${VMF_COMPOSE_SRC:-}"
if [[ -n "$VMF_REPO_DIR" && -d "$VMF_REPO_DIR" ]]; then
  echo "staging repo ($VMF_REPO_DIR) into the inputs..."
  tar -czf "$rundir/repo.tar.gz" -C "$VMF_REPO_DIR" \
    --exclude=.git --exclude=node_modules --exclude=__pycache__ \
    --exclude=.venv --exclude=dist .
fi
# Direct mode with an in-VM docker runtime (gap-fill needs_docker):
# stage the static docker bundle; the guest untars it and starts
# dockerd before the app.
if [[ "${VMF_WANT_DOCKER:-0}" == "1" ]]; then
  DB_DIR="${VMF_DOCKER_BUNDLE:-$HOME/.local/share/vmf/docker-bundle}"
  echo "staging docker bundle into the inputs..."
  tar -czf "$rundir/docker-bundle.tar.gz" -C "$DB_DIR" bin
  # The plan's container images: the host pulls them with its own
  # trust and stages the tars; the guest loads them before install.sh
  # and never dials a registry (the in-guest dockerd has no CA story
  # for docker.io — the pull died with a TLS verify error).
  n=0
  while IFS= read -r ref; do
    [[ -n "$ref" ]] || continue
    [[ "$n" -lt 4 ]] || break
    if bash "$SCRIPTS_DIR/image-supply.sh" "$ref" \
        "$rundir/images-seed-$n.tar" >/dev/null 2>&1; then
      echo "staged image: $ref"
      n=$((n + 1))
    else
      echo "note: host supply failed for $ref; the guest will pull it itself" >&2
    fi
  done < <(python3 -c "
import json, os, sys

def norm(ref):
    if ref.startswith('localhost/') or ref.startswith('vmf-'):
        return ref
    if '/' in ref:
        head = ref.split('/', 1)[0]
        return ref if ('.' in head or ':' in head) else 'docker.io/' + ref
    return 'docker.io/library/' + ref

p = os.environ.get('VMF_VERIFY_PLAN') or ''
try:
    plan = json.load(open(p))
except Exception:
    sys.exit(0)
for r in (plan.get('images') or [])[:4]:
    print(norm(r))" 2>/dev/null)
fi
# Host port pick: 1:1 when the unprivileged slirp bind can take it,
# otherwise a stable high port (hash of name+want, probed upward). The
# guest keeps the declared port; only the host-side bind moves.
pick_host_port() { # name want -> usable host port
  python3 - "$1" "$2" <<'PPY'
import socket, sys, zlib
name, want = sys.argv[1], int(sys.argv[2])
def usable(p):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); s.close(); return True
    except OSError:
        return False
if usable(want):
    print(want); sys.exit(0)
p = 30000 + zlib.crc32(("%s:%d" % (name, want)).encode()) % 10000
for _ in range(100):
    if usable(p):
        print(p); break
    p += 1
else:
    print(want)
PPY
}
: > "$rundir/hostfwd"
for p in "${ports[@]:-}"; do
  [[ -n "$p" && "$expose_mode" != "none" ]] || continue
  proto=tcp; spec="$p"
  if [[ "$spec" == */udp ]]; then proto=udp; spec="${spec%/udp}"
  elif [[ "$spec" == */tcp ]]; then spec="${spec%/tcp}"; fi
  hport="${spec%%:*}"; gport="${spec##*:}"
  bind=""
  case "$spec" in *:*:*) bind="${spec%%:*}"; hport="${spec#*:}"; hport="${hport%%:*}" ;; esac
  rport=$(pick_host_port "$name" "$hport")
  [[ "$rport" != "$hport" ]] && \
    echo "note: host port $hport unavailable; '$name' port $gport published on ${bind:-127.0.0.1}:$rport" >&2
  printf '%s %s %s %s\n' "$proto" "${bind:-0.0.0.0}" "$rport" "$gport" >> "$rundir/hostfwd"
done

# Verify stage (qemu/firecracker, detached runs): run the plan's checks
# against the booted VM, print the verdict, and on failure feed the
# evidence to one bounded plan revision, then reboot with the revised
# plan. Compose-mode runs carry no plan.json in the rundir, so their
# verdict arrives with the orchestrator slice.
vmf_verify_stage() {
  [[ "$detach" -eq 1 && "$ssh" -eq 1 ]] || return 0
  [[ "${VMF_VERIFY:-1}" == "1" ]] || return 0
  local vplan=""
  # The intent cache is the stable plan source: a --rm teardown of a
  # fast-dying VM removes the rundir before this stage runs, so the
  # rundir copies are not enough for boot-time deaths.
  local cache_plan=""
  if [[ -n "$intent" ]]; then
    cache_plan=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import vmf_plan; print(vmf_plan.intent_cache_dir(sys.argv[2], sys.argv[3]) + "/direct.json")' \
      "$VMF_SCRIPTS_DIR" "$image" "$intent" 2>/dev/null || true)
  fi
  for f in "${VMF_VERIFY_PLAN:-}" "$rundir/intent-direct.json" \
           "$rundir/direct.json" "$cache_plan"; do
    [[ -n "$f" && -f "$f" ]] && { vplan="$f"; break; }
  done
  [[ -n "$vplan" ]] || return 0
  local turn="${VMF_VERIFY_TURN:-1}" vrc=0 probe_host="127.0.0.1"
  # ip mode: the lease_wait in the detached boot subshell lands IP=
  # asynchronously; the verify must not probe loopback before it does.
  if grep -q '^TAP=' "$inst_dir/conf" 2>/dev/null; then
    local i
    for i in $(seq 1 20); do
      grep -q '^IP=' "$inst_dir/conf" 2>/dev/null && break
      sleep 2
    done
  fi
  # The conf may lack IP= (slirp boots): the no-match grep must not
  # kill the stage under set -e + pipefail.
  probe_host=$(grep -oE '^IP=.*' "$inst_dir/conf" 2>/dev/null | cut -d= -f2- || true)
  probe_host="${probe_host:-127.0.0.1}"
  # Evidence lands FLAT under $RUNS_DIR (like the legacy verdict
  # markers): the race's loser reap rm -rf's the whole instance dir,
  # and a failed candidate's evidence must survive it.
  python3 "$SCRIPTS_DIR/vmf_verify.py" run "$vplan" --name "$name" \
    --hostfwd "$rundir/hostfwd" --console "$inst_dir/log" \
    --evidence-out "$RUNS_DIR/$name.verify-evidence.json" \
    --probe-host "$probe_host" \
    --target-out "$rundir/target" || vrc=$?
  # Verdict marker for the race coordinator: the final state of this
  # stage per VM. Recursions overwrite; the last writer wins.
  _verdict() {
    printf '%s\n' "$1" > "$inst_dir/verdict"
    # One-line status: standalone runs settle their line here (race
    # children skip — the coordinator owns the run's line).
    if [[ -z "${VMF_RACE_CHILD:-}" ]]; then
      if [[ "$1" == "pass" && -f "$rundir/target" ]]; then
        python3 "$SCRIPTS_DIR/vmf_status.py" final "$name" pass \
          "$(head -1 "$rundir/target")" 2>/dev/null || true
      else
        python3 "$SCRIPTS_DIR/vmf_status.py" final "$name" fail \
          "${1:0:40}" 2>/dev/null || true
      fi
    fi
  }
  if [[ "$vrc" -eq 0 ]]; then
    _verdict pass
    # The rendered deliverable rides the conf; `just ps` and the race
    # promotion print it, and the bump never stays silent again.
    if [[ -f "$rundir/target" ]]; then
      local tgt
      tgt=$(head -1 "$rundir/target")
      if grep -q '^TARGET=' "$inst_dir/conf" 2>/dev/null; then
        sed -i "s|^TARGET=.*|TARGET=$tgt|" "$inst_dir/conf"
      else
        printf 'TARGET=%s\n' "$tgt" >> "$inst_dir/conf"
      fi
      echo "target: $tgt"
    fi
    rm -f "$inst_dir/transcript.json"
    return 0
  fi
  if [[ "$vrc" -eq 2 ]]; then
    _verdict "unverified (plan declares no checks)"
    return 0
  fi
  if [[ "$vrc" -eq 3 ]]; then
    _verdict "fail (ssh never came up)"
    return 0
  fi
  if [[ "$vrc" -ne 1 ]]; then
    _verdict "fail (verify infra error rc=$vrc)"
    return 0
  fi
  if [[ "$turn" -ge "${VMF_VERIFY_TURNS:-2}" ]]; then
    _verdict fail
    # Cache hygiene: record the failed replay on the intent cache —
    # a fresh grounded plan then replaces a repeatedly-dead spec.
    if [[ -n "$intent" ]]; then
      python3 - "$image" "$intent" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ.get("VMF_SCRIPTS_DIR") or ".")
import vmf_plan
gen = vmf_plan.intent_cache_dir(sys.argv[1], sys.argv[2])
p = os.path.join(gen, "direct.json.meta.json")
try:
    meta = json.load(open(p))
except (OSError, ValueError):
    meta = {}
meta["failed"] = int(meta.get("failed", 0)) + 1
os.makedirs(gen, exist_ok=True)
open(p, "w").write(json.dumps(meta, indent=2))
PY
    fi
    return 0
  fi
  # Repair-first (the inversion): when the VM is alive, the agent fixes
  # the app in place and rewrites the spec — no reboot, seconds of a
  # small model instead of minutes of a rebuild.
  # Compose-mode OOM repair first: deterministic, no agent. The stack
  # OOM-killed a container (measured anon-rss in the evidence); the
  # repair is a bigger VM, applied by compose-run on the child re-run
  # via VMF_PLAN_MEM_FLOOR.
  if python3 -c 'import json,sys
p = json.load(open(sys.argv[1]))
sys.exit(0 if p.get("services") and not p.get("command") else 1)' \
      "$vplan" 2>/dev/null; then
    local floor
    floor=$(python3 -c '
import json, os, sys
sys.path.insert(0, os.environ.get("VMF_SCRIPTS_DIR") or ".")
import vmf_verify
try:
    ev = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    ev = []
print(vmf_verify.oom_floor_mb(ev) or 0)' "$rundir/verify-evidence.json" 2>/dev/null || echo 0)
    if [[ "$floor" =~ ^[0-9]+$ && "$floor" -gt 0 ]]; then
      echo "verify: compose OOM repair: rebooting with a ${floor}MB floor..."
      bash "$SCRIPTS_DIR/stop.sh" "$name" >/dev/null 2>&1 || true
      child_args=()
      if [[ -n "${VMF_ORIG_ARGS_B64:-}" ]]; then
        while IFS= read -r -d '' a; do
          child_args+=("$a")
        done < <(printf '%s' "$VMF_ORIG_ARGS_B64" | base64 -d)
      fi
      VMF_VERIFY_TURN=$((turn + 1)) VMF_RUN_YES=1 VMF_PLAN_MEM_FLOOR="$floor" \
        bash "$0" ${child_args[@]+"${child_args[@]}"} --name "$name"
      exit $?
    fi
  fi
  # Repair-first (the inversion): when the VM is alive, the agent fixes
  # the app in place and rewrites the spec — no reboot, seconds of a
  # small model instead of minutes of a rebuild.
  if bash "$SCRIPTS_DIR/ssh.sh" "$name" -- echo ok >/dev/null 2>&1; then
    if [[ "${VMF_INTENT_MODE:-auto}" != "plan" ]] && \
        python3 "$VMF_SCRIPTS_DIR/vmf_agent.py" --vm "$name" \
        --rundir "$rundir" --out "$inst_dir/repaired.json" \
        --repair --context "$rundir/verify-evidence.json" \
        --resume "$inst_dir/transcript.json" \
        --image "$image" --phrase "$intent"; then
      if python3 -c 'import json,sys
a, b = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
sys.exit(0 if a.get("ports") == b.get("ports") else 1)' \
          "$vplan" "$inst_dir/repaired.json"; then
        cp "$inst_dir/repaired.json" "$vplan" 2>/dev/null || true
        echo "verify: repaired in place; re-checking..."
        VMF_VERIFY_TURN=$((turn + 1)) vmf_verify_stage
        return
      fi
      # The repair changed the published ports: reboot with the new
      # spec (the child re-run reads the updated intent cache).
      echo "verify: repair changed the ports; rebooting..."
      bash "$SCRIPTS_DIR/stop.sh" "$name" >/dev/null 2>&1 || true
      child_args=()
      if [[ -n "${VMF_ORIG_ARGS_B64:-}" ]]; then
        while IFS= read -r -d '' a; do
          child_args+=("$a")
        done < <(printf '%s' "$VMF_ORIG_ARGS_B64" | base64 -d)
      fi
      VMF_VERIFY_TURN=$((turn + 1)) VMF_RUN_YES=1 bash "$0" \
        ${child_args[@]+"${child_args[@]}"} --name "$name"
      exit $?
    fi
  fi
  # The revision needs a cache target: intent runs recompute the cache
  # from image+phrase; gap-fill runs carry it in VMF_VERIFY_CACHE
  # (content-derived key, sidecar from the gap-filler).
  local cache_args=()
  if [[ -n "${VMF_VERIFY_CACHE:-}" ]]; then
    cache_args=(--cache "$VMF_VERIFY_CACHE")
  elif [[ -n "$intent" ]]; then
    cache_args=(--image "$image" --phrase "$intent")
  else
    echo "verify: checks failed; auto-revision needs an --intent run (phrase)"
    _verdict fail
    return 0
  fi
  echo "verify: revising the plan from check evidence..."
  # Stable path: the VM's teardown removes the rundir (--rm) while the
  # loop still runs; $RUNS_DIR survives the whole stage.
  local revised="$RUNS_DIR/$name.verify-revised.json"
  rm -f "$revised"
  if ! VMF_RUN_YES=1 python3 "$SCRIPTS_DIR/vmf_verify.py" revise "$vplan" \
      "$revised" --evidence "$rundir/verify-evidence.json" \
      ${cache_args[@]+"${cache_args[@]}"}; then
    echo "verify: revision not applied; the failed verdict stands"
    return 0
  fi
  echo "verify: turn $((turn + 1)): rebooting with the revised plan..."
  # A CHILD process, not exec: the rundir name embeds the shell PID
  # ($$), and exec keeps the PID — the old VM's teardown would then
  # delete this run's rundir mid-boot. A child gets a fresh PID, a
  # fresh rundir, and the old teardown race disappears. The child
  # replays the FIRST pass's argv (decoded from VMF_ORIG_ARGS_B64), so
  # the revised plan is re-planned from the user's original input.
  # --name "$name" rides last: in a race the outer argv carries the
  # canonical (held) name, and the revise must boot the candidate or
  # the race loses its verdict.
  child_args=()
  if [[ -n "${VMF_ORIG_ARGS_B64:-}" ]]; then
    while IFS= read -r -d '' a; do
      child_args+=("$a")
    done < <(printf '%s' "$VMF_ORIG_ARGS_B64" | base64 -d)
  fi
  VMF_VERIFY_TURN=$((turn + 1)) VMF_RUN_YES=1 bash "$0" \
    ${child_args[@]+"${child_args[@]}"} --name "$name"
  exit $?
}

if [[ "$ENGINE" == "qemu" ]]; then
  ensure_microvm_assets
  for v in "${volumes[@]:-}"; do
    [[ -n "$v" ]] && echo "warning: -v not supported by the qemu engine yet; ignored: $v" >&2
  done
  rm -f "$inst_dir/log"
  echo "creating microVM '$name' from $create_ref (qemu engine)..."
  vmf_tool buildah krunvm buildah
  BUILD_BIN=("${TOOL[@]}")
  VMF_IMAGE_REF="$create_ref" VMF_NAME="$name" VMF_RUNDIR="$rundir" \
  VMF_CONF="$inst_dir/conf" VMF_ASSETS="$MICROVM_DIR" VMF_CONSOLE="$inst_dir/log" \
  VMF_NET_MODE="${netmode:-open}" VMF_TIMEOUT_SECS="$timeout_secs" \
  VMF_DISK_BLOCKS="$disk_blocks" \
  VMF_DETACH="$detach" VMF_KEEP="$keep" VMF_CPUS="${cpus:-2}" VMF_MEM="${mem:-1024}" \
  "${BUILD_BIN[@]}" unshare -- bash "$(cd "$(dirname "$0")" && pwd)/qemu-boot.sh"
  if [[ "$detach" -eq 1 ]]; then
    echo "microVM '$name' detached; console log: $inst_dir/log"
    echo "ssh: just ssh $name   stop: just stop $name"
    # One-line status for standalone boots (race children skip: the
    # coordinator owns the run's line).
    if [[ -z "${VMF_RACE_CHILD:-}" ]]; then
      python3 "$SCRIPTS_DIR/vmf_status.py" begin "$name" 2>/dev/null || true
      python3 "$SCRIPTS_DIR/vmf_status.py" event "$name" boot "booting" 2>/dev/null || true
    fi
    vmf_verify_stage
  fi
  exit 0
fi

if [[ "$ENGINE" == "firecracker" ]]; then
  ensure_microvm_assets
  # Firecracker toolchain (binary + slirp4netns + mksquashfs + mke2fs).
  FC_PKGS=(firecracker slirp4netns squashfsTools e2fsprogs iproute2 netcat)
  vmf_tools "${FC_PKGS[@]}"
  if [[ ${#TOOL[@]} -gt 0 ]]; then
    echo "fetching firecracker toolchain via nix shell..."
  fi
  FC=("${TOOL[@]}")
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
    vmf_tool buildah krunvm buildah
    BUILD_BIN=("${TOOL[@]}")
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
  # Gap-fill direct mode artifacts (written before the engine branches):
  # copy them into the ext4 inputs; qemu reads the rundir over 9p.
  for f in install.sh repo.tar.gz docker-bundle.tar.gz; do
    [[ -f "$rundir/$f" ]] && cp "$rundir/$f" "$stage/$f"
  done
  input_extra=0
  [[ -f "$rundir/repo.tar.gz" ]] && \
    input_extra=$(($(stat -c%s "$rundir/repo.tar.gz") / 1024 / 1024 + 8))
  [[ -f "$rundir/docker-bundle.tar.gz" ]] && \
    input_extra=$(($input_extra + $(stat -c%s "$rundir/docker-bundle.tar.gz") / 1024 / 1024 + 16))
  input_size=$(( 16 + input_extra ))M
  vmf_run e2fsprogs -- mke2fs -q -F -t ext4 -b 4096 -d "$stage" "$rundir/inputs.ext4" "$input_size"
  rm -rf "$stage"
  rm -f "$inst_dir/log"
  echo "creating microVM '$name' from $create_ref (firecracker engine)..."
  # Host-side expose poller: reads the guest's discovered port table
  # over ssh and adds slirp hostfwd entries for new ports through the
  # slirp API socket. Dynamic publishing is firecracker-only (qemu's
  # user-net hostfwd is fixed at boot). It starts before the boot and
  # waits for the VM to come up on its own.
  if [[ "$ssh" -eq 1 && "${netmode:-open}" != "off" ]]; then
    VMF_NAME="$name" VMF_RUNDIR="$rundir" VMF_CONF="$inst_dir/conf" \
      nohup bash "$(cd "$(dirname "$0")" && pwd)/expose-poller.sh" \
      >>"$rundir/expose-host.log" 2>&1 &
    disown
  fi
  FCB=("${FC[@]}")
  VMF_NAME="$name" VMF_RUNDIR="$rundir" VMF_ASSETS_SQUASHFS="$squash" \
  VMF_KERNEL="$MICROVM_DIR/vmlinux" VMF_INITRAMFS="$MICROVM_DIR/initramfs.cpio.gz" \
  VMF_CONF="$inst_dir/conf" VMF_CONSOLE="$inst_dir/log" \
  VMF_NET_MODE="${netmode:-open}" VMF_TIMEOUT_SECS="$timeout_secs" \
  VMF_DETACH="$detach" VMF_KEEP="$keep" VMF_CPUS="${cpus:-2}" VMF_MEM="${mem:-1024}" \
  "${FCB[@]}" bash "$(cd "$(dirname "$0")" && pwd)/firecracker-boot.sh"
  if [[ "$detach" -eq 1 ]]; then
    echo "microVM '$name' detached; console log: $inst_dir/log"
    echo "ssh: just ssh $name   stop: just stop $name"
    vmf_verify_stage
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
    rm -rf "$rundir" "$inst_dir/log"
    if grep -q "^RUN=$runid$" "$inst_dir/conf" 2>/dev/null; then rm -rf "$inst_dir"; [[ "$(readlink "$RUNS_DIR/$name" 2>/dev/null)" == "$inst_id" ]] && rm -f "$RUNS_DIR/$name"; fi
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
  console_log="$inst_dir/log"
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
      if grep -q "^RUN=$runid$" "$inst_dir/conf" 2>/dev/null; then rm -rf "$inst_dir"; [[ "$(readlink "$RUNS_DIR/$name" 2>/dev/null)" == "$inst_id" ]] && rm -f "$RUNS_DIR/$name"; fi
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