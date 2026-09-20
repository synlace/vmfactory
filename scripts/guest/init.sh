#!/vmf/sh
# Guest init for vmfactory microVMs: starts dropbear (SSH server) in the
# background, then runs the image entrypoint as a supervised child.
# Injected into the derived image by oci-run.sh. Runtime inputs arrive on
# the read-only /vmf-run mount: authorized_keys, env, argv.sh, cwd, uid.
# Requires nothing from the image: all file tools come from /vmf/busybox.
set -u

BB=/vmf/busybox

# This init is PID 1 of the guest kernel (qemu engine): ANY exit panics
# the kernel ('Attempted to kill init!'). Power the VM off instead on
# every exit path. krunvm guests stop the VM on init exit, so they
# return the status normally. The engine file is usually present; when
# missing (fresh boot, read race), assume qemu and power off.
engine=$($BB cat /vmf-run/engine 2>/dev/null || true)
case "$engine" in
  krunvm) ;;
  firecracker) trap '$BB reboot -f' EXIT ;;
  *) trap '$BB poweroff -f' EXIT ;;
esac

$BB mkdir -p /run/dropbear /root/.ssh
# PTY support: the initramfs (qemu engine) mounts devpts with proper
# modes; init.krun (krunvm engine) mounts it with ptmxmode=000, so
# remount there. Never stack a second devpts instance on /dev/pts —
# that breaks the /dev/ptmx -> /dev/pts/N slave lookup.
if [ "$($BB cat /vmf-run/engine 2>/dev/null)" = "krunvm" ]; then
  $BB mount -o remount,mode=620,ptmxmode=0666 /dev/pts 2>/dev/null || true
fi

$BB cp /vmf-run/auth/authorized_keys /root/.ssh/authorized_keys
$BB chmod 700 /root/.ssh
$BB chmod 600 /root/.ssh/authorized_keys

# Image env (PATH etc.) + -e overrides, applied to the app below.
[ -r /vmf-run/env ] && [ -r /vmf-run/argv.sh ] && [ -r /vmf-run/cwd ] || {
  echo "vmf-init: /vmf-run inputs missing" >&2
  exit 2
}
. /vmf-run/env

# SSH server, engine-specific:
# - qemu / firecracker (real kernel): OpenSSH sshd — full PTY sessions.
#   Dropbear 2026.91 closes the pty master before TIOCSCTTY, which hangs
#   the controlling-tty setup (verified: TIOCSCTTY EIO with the master
#   closed, OK with it held), so sshd is the safe default here.
# - krunvm (libkrun): dropbear on :22, one-shot exec sessions only.
if [ "$($BB cat /vmf-run/engine 2>/dev/null)" = "krunvm" ]; then
  $BB mkdir -p /etc/ssh /var/empty /var/run
  [ -f /etc/ssh/ssh_host_ed25519_key ] || /vmf/ssh-keygen -A >/dev/null 2>&1
  /vmf/sshd -D -e &
else
  [ -d /etc/dropbear ] || $BB mkdir -p /etc/dropbear
  for t in ed25519 ecdsa rsa; do
    [ -f "/etc/dropbear/dropbear_${t}_host_key" ] || \
      /vmf/dropbearkey -t "$t" $([ "$t" = rsa ] && echo "-s 2048") \
        -f "/etc/dropbear/dropbear_${t}_host_key" >/dev/null 2>&1
  done
  /vmf/dropbear -EsF -p 22 &
fi

cd "$($BB cat /vmf-run/cwd)"
# Everything the init itself runs comes from /vmf or shell builtins:
# distroless bases carry no coreutils and PATH may not reach /bin.
eval "set -- $(/vmf/busybox cat /vmf-run/argv.sh)"

# Compose mode: the VM runs a docker daemon and a compose project from
# the data drive (/dev/vdc on firecracker, /dev/vdb on qemu — qemu's
# per-run inputs arrive over 9p, firecracker's on the inputs drive).
# dockerd uses --iptables=false: inter-container traffic rides the
# compose bridge, published ports go through the userland proxy, and
# the host reaches them via the usual slirp hostfwd.
mode=$($BB cat /vmf-run/mode 2>/dev/null || echo direct)
$BB echo "vmf-init: mode=$mode"
if [ "$mode" = "compose" ]; then
  $BB echo "vmf-init: compose branch"
  $BB mkdir -p /data
  dd=/dev/vdb
  $BB test -b /dev/vdc && dd=/dev/vdc
  $BB echo "vmf-init: mounting data drive $dd"
  $BB mount -t ext4 "$dd" /data || {
    echo "vmf-init: cannot mount data drive" >&2
    exit 3
  }
  export PATH="/data/docker/bin:$PATH"
  # dockerd needs a mounted cgroup hierarchy (v2 preferred; the devices
  # controller arrives via CGROUP_DEVICE/CGROUP_BPF in the kernel).
  $BB mkdir -p /sys/fs/cgroup
  $BB mount -t cgroup2 none /sys/fs/cgroup 2>/dev/null || true
  $BB mkdir -p /data/docker-data
  $BB mkdir -p /root/.docker/cli-plugins
  $BB ln -sf /data/docker/bin/docker-compose /root/.docker/cli-plugins/docker-compose
  dockerd --iptables=false --ip6tables=false \
    --data-root /data/docker-data --storage-driver=overlay2 \
    >/data/dockerd.log 2>&1 &
  dockerd_pid=$!
  i=0
  while [ "$i" -lt 300 ]; do
    $BB test -S /var/run/docker.sock && break
    i=$((i + 1))
    $BB sleep 0.1
  done
  if [ ! -S /var/run/docker.sock ]; then
    echo "vmf-init: dockerd did not come up; see /data/dockerd.log" >&2
    wait "$dockerd_pid"
    exit 3
  fi
  for f in /data/images/*.tar; do
    [ -e "$f" ] || continue
    $BB echo "vmf-init: docker load $f"
    docker load -i "$f" || $BB echo "vmf-init: docker load failed: $f" >&2
  done
  $BB echo "vmf-init: docker compose up"
  docker compose -f /data/compose.yaml up -d || {
    echo "vmf-init: compose up failed" >&2
    exit 3
  }
  # Supervise dockerd; if it dies the VM exits (compose containers die
  # with it). sshd keeps serving until then.
  wait "$dockerd_pid"
  exit 0
fi

[ $# -ge 1 ] || {
  echo "vmf-init: empty argv" >&2
  exit 2
}
uid="$($BB cat /vmf-run/uid 2>/dev/null || true)"

# Image USER (numeric uid[:gid]) or root.
if [ -n "$uid" ]; then
  $BB setuidgid "$uid" "$@" &
else
  "$@" &
fi
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null' INT TERM
wait "$pid"
rc=$?
# Real-kernel engines (qemu, firecracker): this init is PID 1, so the
# VM must be powered off explicitly (init cannot just exit). qemu
# powers off via ACPI; firecracker has no power-off, so it reboots
# (reboot=k makes firecracker exit on guest reboot). krunvm guests
# exit when init exits.
case "$($BB cat /vmf-run/engine 2>/dev/null)" in
  krunvm) ;;
  firecracker) $BB reboot -f ;;
  *) $BB poweroff -f ;;
esac
exit $rc