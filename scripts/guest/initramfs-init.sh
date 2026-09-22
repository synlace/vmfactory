#!/bin/sh
# Kernel initramfs init (PID 1) for the QEMU microVM engine. The kernel
# is a custom build with virtio, 9p and devpts built in, so no modules
# are needed. Duties: early mounts, static slirp networking, mount the
# container rootfs (9p tag vmf-root) and the per-run inputs (tag
# vmf-run), then hand over: derived images switch_root into /vmf/init.sh,
# plain images run the resolved entrypoint via chroot and power off.
set -u
export PATH=/bin:/sbin
BB=/bin/busybox
# Device nodes early: the firecracker branch below needs /dev/vda[vb],
# and the initramfs /dev is otherwise empty (devtmpfs automount covers
# real root filesystems, not the initramfs).
$BB mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
# Engine probe: firecracker's rootfs is a squashfs on /dev/vda; qemu has
# no block rootfs (9p shares), though a qemu compose run still has a
# block device on vda (the ext4 data drive). Probe the filesystem, not
# the device node. Firecracker exits on guest reboot (reboot=k) and has
# no ACPI poweroff -> reboot -f there; qemu powers off normally.
fc=0
$BB mkdir -p /ro /up /root
if $BB test -b /dev/vda && $BB mount -t squashfs -o ro /dev/vda /ro 2>/dev/null; then
  fc=1
fi
if [ "$fc" = 1 ]; then
  pw() { $BB reboot -f; }
else
  pw() { $BB poweroff -f; }
fi

# Firecracker branch (fc=1 from the probe above): the squashfs root is
# already mounted on /ro; add the tmpfs overlay and the inputs drive.
# qemu branch: 9p shares; a compose data drive on vda stays for the
# guest init to mount.
if [ "$fc" = 1 ]; then
  $BB mount -t tmpfs tmpfs /up
  $BB mkdir -p /up/up /up/work
  $BB mount -t overlay overlay \
    -o lowerdir=/ro,upperdir=/up/up,workdir=/up/work /root || {
    echo "vmf-initramfs: cannot mount overlay rootfs" >&2
    pw
  }
  $BB mkdir -p /root/vmf-run
  $BB mount -t ext4 -o ro /dev/vdb /root/vmf-run || {
    echo "vmf-initramfs: cannot mount inputs drive" >&2
    pw
  }
else
  # qemu engine: 9p root share. cache=loose is required for writeable
  # MAP_SHARED mmap on 9p (cache=none returns EINVAL). Databases like
  # LMDB (OpenLDAP slapd) mmap their files and fail to open without it.
  # Coherence is safe: the host writes to the shares only before the
  # guest boots.
  $BB mkdir -p /root
  $BB mount -t 9p -o trans=virtio,version=9p2000.L,msize=512000,cache=loose vmf-root /root || {
    echo "vmf-initramfs: cannot mount root share" >&2
    pw
  }
  $BB mkdir -p /root/vmf-run
  $BB mount -t 9p -o trans=virtio,version=9p2000.L,msize=512000,cache=loose vmf-run /root/vmf-run || {
    echo "vmf-initramfs: cannot mount run-inputs share" >&2
    pw
  }
fi

hn=$($BB cat /root/vmf-run/hostname 2>/dev/null)
[ -n "$hn" ] && $BB hostname "$hn"
# Docker parity: docker makes the container hostname resolvable via
# /etc/hosts. Without this, apps that resolve their own hostname (apache
# ServerName, slapd, postfix...) log warnings or fail at startup.
[ -n "$hn" ] && [ -d /root/etc ] && \
  [ "$($BB cat /root/vmf-run/net 2>/dev/null)" != "ip" ] && \
  echo "10.0.2.15 $hn" >> /root/etc/hosts

# Networking (both engines). Default: static slirp (guest 10.0.2.15,
# gateway 10.0.2.2, DNS 10.0.2.3) — host forwards arrive via the same
# net stack, no TSI, so guest binds behave like real kernel binds.
# net=ip (rundir marker): bridge mode — DHCP on eth0 from the host's
# dnsmasq; the VM gets a routable host-subnet address, so the host
# reaches it directly and no hostfwd exists.
if $BB ip link show eth0 >/dev/null 2>&1; then
  $BB ip link set lo up
  $BB ip link set eth0 up
  if [ "$($BB cat /root/vmf-run/net 2>/dev/null)" = "ip" ]; then
    # udhcpc applies leases through a helper script (the default
    # script is absent in the initramfs); bound/renew both re-add.
    # Only busybox applets exist here, so the helper uses full paths.
    $BB mkdir -p /tmp
    printf '%s\n' \
      '#!/bin/sh' \
      'BB=/bin/busybox' \
      'case "$1" in' \
      '  deconfig) $BB ip addr flush dev "$interface" ;;' \
      '  bound|renew)' \
      '    [ -n "$ip" ] && $BB ip addr flush dev "$interface"' \
      '    [ -n "$ip" ] && $BB ip addr add "$ip/$mask" dev "$interface"' \
      '    [ -n "$router" ] && $BB ip route add default via "$router"' \
      '    [ -n "$dns" ] && $BB echo "nameserver $dns" > /root/etc/resolv.conf 2>/dev/null' \
      '    ;;' \
      'esac' > /tmp/udhcpc.sh
    $BB chmod +x /tmp/udhcpc.sh
    echo "net: ip mode; dhcp on eth0 (tap pool)..." > /dev/console
    $BB udhcpc -i eth0 -q -n -t 10 -T 3 -s /tmp/udhcpc.sh 2>&1 | $BB tail -3
    gip=$($BB ip -4 addr show eth0 | $BB grep -oE 'inet [0-9.]+' | $BB cut -d' ' -f2)
    echo "net: leased address ${gip:-NONE}" > /dev/console
    [ -n "$hn" ] && [ -n "$gip" ] && $BB echo "$gip $hn" >> /root/etc/hosts
    [ -d /root/etc ] && [ ! -s /root/etc/resolv.conf ] && \
      $BB echo "nameserver 192.168.42.1" > /root/etc/resolv.conf
  else
    $BB ip addr add 10.0.2.15/24 dev eth0
    $BB ip route add default via 10.0.2.2
    [ -d /root/etc ] && echo "nameserver 10.0.2.3" > /root/etc/resolv.conf
  fi
fi

# Mount the boot-time filesystems INTO the new root: mounts on the
# initramfs root itself become unreachable after switch_root.
$BB mkdir -p /root/proc /root/sys /root/dev /root/tmp
$BB mount -t proc proc /root/proc
$BB mount -t sysfs sysfs /root/sys
$BB mount -t devtmpfs devtmpfs /root/dev
$BB mkdir -p /root/dev/pts
$BB mount -t devpts devpts -o mode=620,ptmxmode=0666 /root/dev/pts
$BB mount -t tmpfs tmpfs /root/tmp

if [ -x /root/vmf/init.sh ]; then
  exec $BB switch_root /root /vmf/init.sh
fi

# Plain mode (--no-ssh): no derived layer, so run the resolved entrypoint
# directly. busybox chroot execs the app as a child of PID 1; the app
# exit powers the VM off.
. /root/vmf-run/env
cd "$($BB cat /root/vmf-run/cwd)"
eval "set -- $($BB cat /root/vmf-run/argv.sh)"
( $BB chroot /root "$@" ) &
wait $!
$BB pw