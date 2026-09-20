#!/usr/bin/env bash
# vmf ssh: a shell or one-shot command in a running box or microVM.
#
# Routing:
#   build/<name>/vm.conf exists -> qemu box, via scripts/enter.sh (real sshd)
#   ~/.vmf/runs/<name>.conf     -> microVM, engine-aware: qemu VMs get real
#                                  sshd (interactive PTY works); krunvm VMs
#                                  get dropbear (one-shot only)
#
# Docker-style leading flags (-it, -i, -t, -d, --rm) are accepted and
# ignored: ssh supports interactive PTY natively, the flags exist only for
# docker-habit compatibility.
set -euo pipefail

# Docker-habit flags are ignored; other value-less flags (e.g. ssh's own
# -tt, -v) pass through to ssh.
ssh_opts=()
while [[ $# -gt 0 && "$1" == -* ]]; do
  case "$1" in
    -i|-t|-it|-ti|-d|-dit|-itd|--rm) shift ;;
    --) shift; break ;;
    *) ssh_opts+=("$1"); shift ;;
  esac
done
[[ $# -gt 0 ]] || { echo "usage: ssh.sh <name> [command...]" >&2; exit 2; }
name="$1"; shift

if [[ -f "build/$name/vm.conf" ]]; then
  exec sh scripts/enter.sh "$name" "$@"
fi

# Interactive handling is engine-specific and happens below: qemu VMs
# allow interactive PTY shells, krunvm VMs reject them with guidance.

RUNS_DIR="${VMF_RUNS:-$HOME/.vmf/runs}"
conf="$RUNS_DIR/$name.conf"
[[ -f "$conf" ]] || {
  echo "error: no running microVM '$name'; start one with: just run --keep <image>" >&2
  exit 1
}
# shellcheck disable=SC1090
source "$conf"
port="${PORT:?state file lacks PORT}"
ENGINE="${ENGINE:-krunvm}"

if [[ "$ENGINE" == "qemu" ]]; then
  # Real kernel: PTY sessions work, so interactive ssh is allowed. The
  # qemu pid (state file or pidfile) tells us whether the VM is alive.
  pid="${PID:-}"
  if [[ -z "$pid" && -f "$RUNS_DIR/$name/qemu.pid" ]]; then
    pid=$(cat "$RUNS_DIR/$name/qemu.pid")
  fi
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    echo "error: microVM '$name' is not running (stale state file); start it again" >&2
    exit 1
  fi
else
  if command -v krunvm >/dev/null 2>&1 && command -v buildah >/dev/null 2>&1; then
    krun() { buildah unshare -- krunvm "$@"; }
  else
    krun() { nix shell nixpkgs#krunvm nixpkgs#buildah -c buildah unshare -- krunvm "$@"; }
  fi
  if ! krun list | grep -qx -- "$name"; then
    echo "error: microVM '$name' is not running (stale state file); start it again" >&2
    exit 1
  fi

  # libkrun guests cannot open pts slave devices (open() returns EIO while
  # the master is held), so PTY-based ssh sessions are impossible there.
  # Strip -t style flags with a notice; one-shot commands are the contract.
  # Interactive shells belong to qemu boxes (vmf boot -> just ssh <box>).
  if [[ $# -eq 0 ]]; then
    echo "error: interactive ssh is not supported on krunvm microVMs (no PTY in libkrun)" >&2
    echo "run a command instead: just ssh $name '<cmd>'" >&2
    echo "for an interactive shell, boot a box: just boot <lab> && just ssh <lab>" >&2
    exit 1
  fi
  clean_opts=()
  for o in ${ssh_opts[@]+"${ssh_opts[@]}"}; do
    case "$o" in
      *t*) echo "note: '$o' ignored: krunvm microVMs have no PTY (libkrun); the command runs without a terminal" >&2 ;;
      *) clean_opts+=("$o") ;;
    esac
  done
  ssh_opts=()
  if [[ ${#clean_opts[@]} -gt 0 ]]; then
    ssh_opts=("${clean_opts[@]}")
  fi
fi

SSH_DIR="${VMF_SSH_DIR:-$HOME/.vmf/ssh}"
[[ -f "$SSH_DIR/id_ed25519" ]] || {
  echo "error: ssh client key missing: $SSH_DIR/id_ed25519 (run 'just run --keep <image>' once)" >&2
  exit 1
}
exec ssh ${ssh_opts[@]+"${ssh_opts[@]}"} \
  -i "$SSH_DIR/id_ed25519" \
  -o UserKnownHostsFile=/dev/null \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout=5 \
  -o LogLevel=ERROR \
  -p "$port" root@127.0.0.1 "$@"