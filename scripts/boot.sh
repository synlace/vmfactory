#!/bin/sh
# Boot a built box on the virbr0 network with a dedicated IP.
# Usage: scripts/boot.sh <name>   (AnyCTF boot recipe, vmf-adapted)
set -eu

name="${1:?usage: boot.sh <name>}"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

. "build/$name/vm.conf"
disk="build/$name/$DISK"
if [ ! -f "$disk" ]; then
  echo "error: $disk missing; run: just build $name" >&2
  exit 1
fi

mac=$(printf '%s' "$name" | md5sum | awk '{print "52:54:00:" substr($1,1,2) ":" substr($1,3,2) ":" substr($1,5,2)}')

tap=
for i in 0 1 2 3 4 5 6 7; do
  dev=tap-ctf-$i
  if ! ip link show "$dev" >/dev/null 2>&1; then
    sudo ip tuntap add dev "$dev" mode tap || continue
    sudo ip link set "$dev" master virbr0 2>/dev/null || true
    sudo ip link set "$dev" up 2>/dev/null || true
  fi
  if ! pgrep -f "ifname=$dev," >/dev/null 2>&1 && ip -br link show "$dev" 2>/dev/null | grep -q NO-CARRIER; then
    tap=$dev
    break
  fi
done
if [ -z "$tap" ]; then
  echo "error: no free tap-ctf device; stop a box first" >&2
  exit 1
fi

cd_args=""
if [ -f "build/$name/$BOOT_ISO" ]; then
  cd_args="-cdrom build/$name/$BOOT_ISO"
fi

echo "booting $name: $DISK tap=$tap mac=$mac ip=$IP"

# Rebuilds and snapshot reverts produce new host keys on the same IP.
# These boxes are disposable, so reset the per-box known_hosts each boot;
# enter.sh re-adds the key via StrictHostKeyChecking=accept-new.
rm -f "build/$name/known_hosts" "build/$name/mon.sock"

exec qemu-system-x86_64 -m "$MEM_MB" -smp "$CPU" -accel kvm -name "$name" \
  -drive file="$disk",if=virtio,format=qcow2 \
  $cd_args \
  -netdev tap,ifname=$tap,script=no,downscript=no,id=n0 \
  -device virtio-net,netdev=n0,mac="$mac" \
  -display none \
  -monitor "unix:build/$name/mon.sock,server,nowait"
