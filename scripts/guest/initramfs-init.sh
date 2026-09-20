#!/bin/sh
# Kernel initramfs init (PID 1) for the QEMU microVM engine. The kernel
# is a custom build with virtio, 9p and devpts built in, so no modules
# are needed. Duties: early mounts, static slirp networking, mount the
# container rootfs (9p tag vmf-root) and the per-run inputs (tag
# vmf-run), then hand over: derived images switch_root into /vmf/init.sh,
# plain images run the resolved entrypoint via chroot and power off.
set -u
BB=/bin/busybox

# Static slirp networking (QEMU user-mode net): guest 10.0.2.15, gateway
# 10.0.2.2, DNS 10.0.2.3. Host forwards arrive via the same net stack —
# no TSI, so guest binds behave like real kernel binds.
$BB ip link set lo up
$BB ip link set eth0 up
$BB ip addr add 10.0.2.15/24 dev eth0
$BB ip route add default via 10.0.2.2

$BB mkdir -p /root
$BB mount -t 9p -o trans=virtio,version=9p2000.L,msize=512000 vmf-root /root || {
  echo "vmf-initramfs: cannot mount root share" >&2
  $BB poweroff -f
}
# Mount the boot-time filesystems INTO the new root: mounts on the
# initramfs root itself become unreachable after switch_root.
$BB mkdir -p /root/proc /root/sys /root/dev /root/tmp
$BB mount -t proc proc /root/proc
$BB mount -t sysfs sysfs /root/sys
$BB mount -t devtmpfs devtmpfs /root/dev
$BB mkdir -p /root/dev/pts
$BB mount -t devpts devpts -o mode=620,ptmxmode=0666 /root/dev/pts
$BB mount -t tmpfs tmpfs /root/tmp
$BB mkdir -p /root/vmf-run
$BB mount -t 9p -o trans=virtio,version=9p2000.L,msize=512000 vmf-run /root/vmf-run || {
  echo "vmf-initramfs: cannot mount run-inputs share" >&2
  $BB poweroff -f
}

hn=$($BB cat /root/vmf-run/hostname 2>/dev/null)
[ -n "$hn" ] && $BB hostname "$hn"
[ -d /root/etc ] && echo "nameserver 10.0.2.3" > /root/etc/resolv.conf

if [ -x /root/vmf/init.sh ]; then
  exec $BB switch_root /root /vmf/init.sh
fi

# Plain mode (--no-ssh): no derived layer, so run the resolved entrypoint
# directly. busybox chroot execs the app as a child of PID 1; the app
# exit powers the VM off.
. /root/vmf-run/env
cd "$($BB cat /root/vmf-run/cwd)"
eval "set -- $(cat /root/vmf-run/argv.sh)"
( $BB chroot /root "$@" ) &
wait $!
$BB poweroff -f