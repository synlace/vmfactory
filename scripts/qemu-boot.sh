#!/usr/bin/env bash
# QEMU microVM engine boot. Runs INSIDE `buildah unshare` (called by
# oci-run.sh) so the mounted image rootfs is visible to the 9p export.
# The container rootfs is the derived image (base + dropbear layer) or
# the pinned base for --no-ssh; per-run inputs live on a second share.
# Real kernel, real network stack: guest binds are ordinary kernel binds
# and host forwards use slirp hostfwd, so docker-style port publishing
# and ssh with a full PTY work.
#
# Env: VMF_IMAGE_REF VMF_NAME VMF_RUNDIR VMF_CONF VMF_ASSETS VMF_CONSOLE
#      VMF_DETACH VMF_KEEP VMF_CPUS VMF_MEM VMF_HOSTFWD (file: "host guest" lines)
#
# Teardown ownership: VMF_RUNDIR is unique per run, so its removal can
# never race a replace-run's writes. The shared state conf is deleted
# only while it still points at THIS qemu process (PID=...): a
# replace-run rewrites the conf for the new VM, and when this subshell
# wakes up the PID no longer matches, so it leaves the new state alone.
set -euo pipefail

: "${VMF_IMAGE_REF:?}" "${VMF_NAME:?}" "${VMF_RUNDIR:?}" "${VMF_ASSETS:?}" "${VMF_CONSOLE:?}"
VMF_CONF="${VMF_CONF:-$VMF_RUNDIR.conf}"

ctr=$(buildah from "$VMF_IMAGE_REF")
rootfs=$(buildah mount "$ctr")

# Conf ownership: PID=<qpid> must be the latest PID recorded in the
# shared state conf. A conf without our PID (rewritten by a replace-run)
# means the new run owns the state now.
owns_state() {
  local qpid="$1" confpid=""
  [[ -f "$VMF_CONF" ]] || return 1
  confpid=$(grep -oE '^PID=[0-9]+' "$VMF_CONF" 2>/dev/null | cut -d= -f2 || true)
  [[ -n "$confpid" && "$confpid" == "$qpid" ]]
}

cleanup() {
  local qpid="$1"
  buildah rm "$ctr" >/dev/null 2>&1 || true
  if [[ "${VMF_KEEP:-0}" != "1" ]]; then
    rm -rf "$VMF_RUNDIR"
    if owns_state "$qpid"; then
      rm -f "$VMF_CONF"
    fi
  fi
}

hostfwd=""
while IFS= read -r pair; do
  [[ -n "$pair" ]] || continue
  hport="${pair%% *}"; gport="${pair##* }"
  hostfwd="$hostfwd,hostfwd=tcp::$hport-:$gport"
done < "$VMF_RUNDIR/hostfwd"

serial=(-serial "file:$VMF_CONSOLE")
if [[ "${VMF_DETACH:-0}" != "1" ]]; then
  serial=(-serial stdio)
fi

qemu=(qemu-system-x86_64
  -machine pc,accel=kvm
  -cpu host
  -name "$VMF_NAME"
  -smp "${VMF_CPUS:-2}"
  -m "${VMF_MEM:-1024}"
  -kernel "$VMF_ASSETS/vmlinuz"
  -initrd "$VMF_ASSETS/initramfs.cpio.gz"
  -append "console=ttyS0"
  -fsdev "local,id=fs0,path=$rootfs,security_model=none"
  -device "virtio-9p-pci,fsdev=fs0,mount_tag=vmf-root"
  -fsdev "local,id=fs1,path=$VMF_RUNDIR,security_model=none"
  -device "virtio-9p-pci,fsdev=fs1,mount_tag=vmf-run"
  -nic "user,model=virtio-net-pci$hostfwd"
  -pidfile "$VMF_RUNDIR/qemu.pid"
  -no-reboot
  -display none
  -monitor none
  -nodefaults
  "${serial[@]}")

record_state() {
  local qpid="$1"
  printf 'PID=%s\nCTR=%s\n' "$qpid" "$ctr" >> "$VMF_CONF"
}

if [[ "${VMF_DETACH:-0}" == "1" ]]; then
  # The detached subshell owns the VM lifecycle (container cleanup +
  # state removal); the outer script must NOT clean up on exit. qemu
  # shares the subshell's process group, so it survives the outer
  # script's exit inside the unshare mount namespace.
  (
    "${qemu[@]}" </dev/null >>"$VMF_CONSOLE" 2>&1 &
    qpid=$!
    record_state "$qpid"
    wait "$qpid" 2>/dev/null || true
    cleanup "$qpid"
  ) >/dev/null 2>&1 &
  disown
  sleep 1
  if [[ ! -f "$VMF_RUNDIR/qemu.pid" ]]; then
    echo "warning: qemu pidfile missing after boot; check console log: $VMF_CONSOLE" >&2
  fi
  exit 0
fi

# Foreground: without job control the backgrounded qemu stays in this
# shell's foreground process group, so Ctrl-C still reaches qemu
# directly and the tty is readable for -serial stdio.
"${qemu[@]}" &
qpid=$!
record_state "$qpid"
trap 'kill "$qpid" 2>/dev/null || true' INT TERM
rc=0
wait "$qpid" || rc=$?
trap - INT TERM
cleanup "$qpid"
exit $rc
