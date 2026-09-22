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
#      VMF_DETACH VMF_KEEP VMF_CPUS VMF_MEM (hostfwd file: $VMF_RUNDIR/hostfwd)
#
# Teardown ownership: VMF_RUNDIR is unique per run, so its removal can
# never race a replace-run's writes. The shared state conf is deleted
# only while it still points at THIS qemu process (PID=...): a
# replace-run rewrites the conf for the new VM, and when this subshell
# wakes up the PID no longer matches, so it leaves the new state alone.
set -euo pipefail
# shellcheck source=vmf_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmf_lib.sh"

: "${VMF_IMAGE_REF:?}" "${VMF_NAME:?}" "${VMF_RUNDIR:?}" "${VMF_ASSETS:?}" "${VMF_CONSOLE:?}"
VMF_CONF="${VMF_CONF:-$VMF_RUNDIR.conf}"
VMF_NET_MODE="${VMF_NET_MODE:-open}"
VMF_TIMEOUT_SECS="${VMF_TIMEOUT_SECS:-0}"
VMF_DISK_BLOCKS="${VMF_DISK_BLOCKS:-0}"

# Disk cap: cap single-file writes done through the 9p share by capping
# qemu's file-size rlimit (the virtfs backend writes in this process).
# SIGXFSZ is ignored so a write beyond the cap returns EFBIG to the
# guest (disk-full semantics) instead of killing the VM.
if [[ "$VMF_DISK_BLOCKS" -gt 0 ]]; then
  trap '' XFSZ
  ulimit -f "$VMF_DISK_BLOCKS"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
while IFS= read -r line; do
  # 2 fields (legacy): "hport gport" tcp; 3 fields: "proto hport gport";
  # 4 fields: "proto bind hport gport" (bind "" or 0.0.0.0 = all).
  vmf_fwd_parse "$line" || continue
  hostfwd="$hostfwd,hostfwd=$VMF_FWD_PROTO:$VMF_FWD_BIND:$VMF_FWD_HOST-:$VMF_FWD_GUEST"
done < "$VMF_RUNDIR/hostfwd"

serial=(-serial "file:$VMF_CONSOLE")
if [[ "${VMF_DETACH:-0}" != "1" ]]; then
  serial=(-serial stdio)
fi

# Network policy: open = default slirp; restricted = a Landlock ruleset
# denies every outbound connect() of the qemu process (guest-initiated
# internet access, host services via the slirp gateway, the slirp DNS
# resolver's host sockets). Bind/listen stay allowed, so published ports
# and ssh (inbound hostfwd) keep working. DNS is intentionally dead in
# this mode: no outbound includes name resolution. off = no NIC at all
# (also means no ssh). ip = the bridge pool: a free tap, guest DHCP, the
# host reaches the VM by address — no hostfwd, no bumps. Falls back to
# slirp with a note when the stack (lab-init) is absent or exhausted.
# Net args: slirp (default), off, restricted, or the bridge pool.
# ip mode = a free tap + guest DHCP: the host reaches the VM by address,
# no hostfwd, no bumps. Requested with --net ip; auto-detected for plain
# runs when the lab-init stack is ready, silent slirp fallback otherwise.
net_args=()
ip_nic() {
  local macs mac tap
  macs=$(od -An -tx1 -N3 /dev/urandom)
  mac=$(printf '52:54:00:%02x:%02x:%02x' \
    "0x$(echo $macs | awk '{print $1}')" \
    "0x$(echo $macs | awk '{print $2}')" \
    "0x$(echo $macs | awk '{print $3}')") || return 1
  tap=$(bash "$SCRIPT_DIR/vmf_net.sh" claim "$mac") || return 1
  printf 'ip\n' > "$VMF_RUNDIR/net"
  printf 'TAP=%s\nMAC=%s\n' "$tap" "$mac" >> "$VMF_CONF"
  net_args=(-netdev "tap,id=net0,ifname=$tap,script=no,downscript=no,vnet_hdr=off" \
            -device "virtio-net-pci,netdev=net0,mac=$mac")
}
case "$VMF_NET_MODE" in
  off)        net_args=(-nic none) ;;
  restricted) net_args=(-nic "user,model=virtio-net-pci$hostfwd") ;;
  ip)
    if ! ip_nic; then
      echo "note: --net ip requested but the stack is not ready; slirp hostfwd" >&2
      net_args=(-nic "user,model=virtio-net-pci$hostfwd")
    fi ;;
  *)
    if ip_nic 2>/dev/null; then
      echo "net: ip mode (bridge pool; the VM has a routable address)"
    else
      net_args=(-nic "user,model=virtio-net-pci$hostfwd")
    fi ;;
esac

# Optional run timeout: timeout execs qemu in place, so the recorded
# pid stays the VM process and TERM tears the VM down through the
# normal wait+cleanup path.
vm() {
  cmd=()
  [[ "$VMF_NET_MODE" == "restricted" ]] && \
    cmd=(python3 "$SCRIPT_DIR/landlock-net-deny.py")
  if [[ "$VMF_TIMEOUT_SECS" -gt 0 ]]; then
    cmd+=(timeout --signal=TERM "$VMF_TIMEOUT_SECS")
  fi
  cmd+=("${qemu[@]}")
  "${cmd[@]}"
}

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
  ${VMF_DATA_DRIVE:+-drive "file=$VMF_DATA_DRIVE,if=virtio,format=raw"}
  "${net_args[@]}"
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

# ip mode: poll the dnsmasq lease file for this VM's MAC and record the
# assigned address in the conf — ssh, verify, and ps resolve IP= first.
lease_wait() {
  local mac ip i leases
  [[ -f "$VMF_RUNDIR/net" ]] || return 0
  mac=$(grep -oE '^MAC=.*' "$VMF_CONF" 2>/dev/null | cut -d= -f2)
  [[ -n "$mac" ]] || return 0
  leases="${VMF_NET_DIR:-$HOME/.vmf/net}/dnsmasq.leases"
  for i in $(seq 1 15); do
    ip=$(awk -v m="$mac" '$2==m {print $3; exit}' "$leases" 2>/dev/null)
    if [[ -n "$ip" ]]; then
      printf 'IP=%s\n' "$ip" >> "$VMF_CONF"
      return 0
    fi
    sleep 2
  done
  echo "warning: no DHCP lease for $mac after 30s" >&2
}

if [[ "${VMF_DETACH:-0}" == "1" ]]; then
  # The detached subshell owns the VM lifecycle (container cleanup +
  # state removal); the outer script must NOT clean up on exit. qemu
  # shares the subshell's process group, so it survives the outer
  # script's exit inside the unshare mount namespace.
  (
    vm </dev/null >>"$VMF_CONSOLE" 2>&1 &
    qpid=$!
    record_state "$qpid"
    lease_wait
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
vm &
qpid=$!
record_state "$qpid"
lease_wait
trap 'kill "$qpid" 2>/dev/null || true' INT TERM
rc=0
wait "$qpid" || rc=$?
trap - INT TERM
cleanup "$qpid"
exit $rc
