#!/usr/bin/env bash
# QEMU microVM engine boot. Runs INSIDE `buildah unshare` (called by
# oci-run.sh) so the mounted image rootfs is visible to the 9p export.
# The container rootfs is the derived image (base + dropbear layer) or
# the pinned base for --no-ssh; per-run inputs live on a second share.
# Real kernel, real network stack: guest binds are ordinary kernel binds
# and host forwards use slirp hostfwd, so docker-style port publishing
# and ssh with a full PTY work.
#
# Env: VMF_IMAGE_REF VMF_NAME VMF_RUNDIR VMF_ASSETS VMF_CONSOLE
#      VMF_DETACH VMF_KEEP VMF_CPUS VMF_MEM VMF_HOSTFWD (file: "host guest" lines)
set -euo pipefail

: "${VMF_IMAGE_REF:?}" "${VMF_NAME:?}" "${VMF_RUNDIR:?}" "${VMF_ASSETS:?}" "${VMF_CONSOLE:?}"

ctr=$(buildah from "$VMF_IMAGE_REF")
rootfs=$(buildah mount "$ctr")
cleanup() {
  buildah rm "$ctr" >/dev/null 2>&1 || true
  if [[ "${VMF_KEEP:-0}" != "1" ]]; then
    rm -rf "$VMF_RUNDIR" "$VMF_RUNDIR.conf"
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

if [[ "${VMF_DETACH:-0}" == "1" ]]; then
  # The detached subshell owns the VM lifecycle (container cleanup +
  # state removal); the outer script must NOT clean up on exit.
  (
    "${qemu[@]}" </dev/null >>"$VMF_CONSOLE" 2>&1
    cleanup
  ) >/dev/null 2>&1 &
  disown
  sleep 1
  if [[ -f "$VMF_RUNDIR/qemu.pid" ]]; then
    printf 'PID=%s\nCTR=%s\n' "$(cat "$VMF_RUNDIR/qemu.pid")" "$ctr" \
      >> "$VMF_RUNDIR.conf"
  fi
  exit 0
fi

trap cleanup EXIT
"${qemu[@]}"
