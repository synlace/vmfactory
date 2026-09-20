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
  # Data drive by engine: firecracker = vdc (vda squashfs, vdb inputs);
  # qemu = vda (the only block device; 9p carries root+inputs).
  case "$($BB cat /vmf-run/engine 2>/dev/null)" in
    qemu) dd=/dev/vda ;;
    *) dd=/dev/vdb; $BB test -b /dev/vdc && dd=/dev/vdc ;;
  esac
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
  # Split archives (host mke2fs cannot populate >2GiB files): each
  # image lives as <name>.tar.part-NN and must be concatenated.
  for f in /data/images/*.tar.part-00; do
    [ -e "$f" ] || continue
    base="${f%.part-00}"
    $BB echo "vmf-init: docker load $base (split)"
    cat "$base".part-* | docker load || $BB echo "vmf-init: docker load failed: $base" >&2
  done
  $BB echo "vmf-init: docker compose up"
  docker-compose -f /data/compose.yaml up -d || {
    echo "vmf-init: compose up failed" >&2
    exit 3
  }
  # Port exposure. docker-proxy misbehaves on this guest (binds :: only,
  # drops data), so published ports use socat bridges in the VM instead:
  # slirp hostfwd lands on the VM address, socat bridges to the
  # container IP. The daemon is detached (its log is /vmf/expose.log,
  # readable over ssh) so init-context quirks cannot block it.
  expose_mode=$($BB cat /vmf-run/expose 2>/dev/null || echo all)
  if [ "$expose_mode" != "none" ]; then
    cat > /vmf/expose.sh <<'EXPOSEEOF'
#!/vmf/sh
# vmf expose daemon (guest side). Discovers listeners every 2s and:
#   compose mode : socat per declared (ports.txt) +, with expose=all,
#                  EXPOSEd container port -> container IP (TCP+UDP)
#   direct mode  : records VM-local listeners only; the host poller adds
#                  slirp hostfwd entries that land on them directly
# Writes /vmf/ports-live.txt ("proto guest_port target ...") which the
# host-side poller (scripts/expose-poller.sh) turns into slirp hostfwd.
set -u
BB=/vmf/busybox
mode=$($BB cat /vmf-run/mode 2>/dev/null || echo direct)
expose=$($BB cat /vmf-run/expose 2>/dev/null || echo all)
[ "$expose" != "none" ] || exit 0
SOCAT=/data/docker/bin/socat
pids=/vmf/expose.pids
live=/vmf/ports-live.txt
desired=/vmf/expose.desired
: > "$pids"

while :; do
  : > "$desired.tmp"
  if [ "$mode" = "compose" ] && [ -x "$SOCAT" ] && [ -f /data/ports.txt ]; then
    PATH=/data/docker/bin:$PATH
    # Declared forwards: "proto vm_port container_port service".
    while read -r proto hport cport svc; do
      [ -n "${proto:-}" ] || continue
      cid=$(docker ps -q --filter "label=com.docker.compose.service=$svc" | $BB head -1)
      [ -n "$cid" ] || continue
      cip=$(docker inspect "$cid" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null)
      [ -n "$cip" ] || continue
      $BB echo "$proto $hport $cip $cport $svc" >> "$desired.tmp"
    done < /data/ports.txt
    # Auto mode: every other EXPOSEd container port, forwarded 1:1.
    if [ "$expose" = "all" ]; then
      for cid in $(docker ps -q); do
        svc=$(docker inspect "$cid" --format '{{index .Config.Labels "com.docker.compose.service"}}' 2>/dev/null)
        cip=$(docker inspect "$cid" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null)
        [ -n "$cip" ] || continue
        for pp in $(docker inspect "$cid" --format '{{range $p, $_ := .Config.ExposedPorts}}{{$p}} {{end}}' 2>/dev/null); do
          cport=${pp%/*}; proto=${pp#*/}
          [ "$proto" = "tcp" ] || [ "$proto" = "udp" ] || continue
          $BB grep -qE "^[a-z]+ [0-9]+ [0-9.]+ $cport " "$desired.tmp" && continue
          $BB echo "$proto $cport $cip $cport ${svc:-container}" >> "$desired.tmp"
        done
      done
    fi
  elif [ "$expose" = "all" ]; then
    # Direct mode: VM-local listeners. slirp hostfwd reaches 0.0.0.0 and
    # 10.0.2.15 (0F02000A little-endian) only; skip sshd + ephemeral.
    for f in /proc/net/tcp /proc/net/tcp6; do
      [ -r "$f" ] || continue
      $BB awk 'function h2d(h,   n, i) {
        n = 0
        for (i = 1; i <= length(h); i++)
          n = n * 16 + index("0123456789abcdef", tolower(substr(h, i, 1))) - 1
        return n
      }
      $4 == "0A" {
        split($2, a, ":"); port = h2d(a[2]);
        if (port >= 32768 && port <= 60999) next;
        if (port == 22) next;
        if (length(a[1]) == 8 && a[1] != "00000000" && a[1] != "0F02000A") next;
        if (length(a[1]) == 32 && a[1] != "00000000000000000000000000000000") next;
        print "tcp " port " vm - -"
      }' "$f"
    done
    for f in /proc/net/udp /proc/net/udp6; do
      [ -r "$f" ] || continue
      $BB awk 'function h2d(h,   n, i) {
        n = 0
        for (i = 1; i <= length(h); i++)
          n = n * 16 + index("0123456789abcdef", tolower(substr(h, i, 1))) - 1
        return n
      }
      {
        split($2, a, ":"); port = h2d(a[2]);
        if (port >= 32768 && port <= 60999) next;
        if (port == 67 || port == 68) next;
        if (length(a[1]) == 8 && a[1] != "00000000" && a[1] != "0F02000A") next;
        if (length(a[1]) == 32 && a[1] != "00000000000000000000000000000000") next;
        print "udp " port " vm - -"
      }' "$f"
    done
  fi
  sort -u "$desired.tmp" > "$desired.new" 2>/dev/null
  mv "$desired.new" "$desired"
  # Reconcile socat forwards: first service to claim a (proto, port)
  # keeps it; late claims on a taken port are conflicts.
  : > "$pids.new"
  while read -r proto lport cip cport svc; do
    [ -n "${proto:-}" ] || continue
    [ "${cip:-}" = "vm" ] && continue
    [ -n "$cip" ] || continue
    key="$proto:$lport"
    old=""
    [ -f "$pids" ] && old=$($BB grep -E "^$key " "$pids" | $BB head -1)
    oldpid=""; oldcip=""; oldsvc=""
    if [ -n "$old" ]; then
      oldpid=$($BB echo "$old" | cut -d' ' -f2)
      oldcip=$($BB echo "$old" | cut -d' ' -f3)
      oldsvc=$($BB echo "$old" | cut -d' ' -f4)
    fi
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null && [ "$oldsvc" != "$svc" ]; then
      $BB echo "vmf-expose: port conflict $proto/$lport: keeping $oldsvc, skipping $svc"
      $BB echo "$key $oldpid $oldcip $oldsvc" >> "$pids.new"
      continue
    fi
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null && [ "$oldcip" = "$cip" ]; then
      $BB echo "$key $oldpid $cip $svc" >> "$pids.new"
      continue
    fi
    [ -n "$oldpid" ] && kill "$oldpid" 2>/dev/null
    if [ "$proto" = "udp" ]; then
      "$SOCAT" -T60 "UDP4-RECVFROM:$lport,fork,reuseaddr" "UDP4-SENDTO:$cip:$cport" >/dev/null 2>&1 &
    else
      "$SOCAT" "TCP4-LISTEN:$lport,fork,reuseaddr" "TCP4:$cip:$cport" >/dev/null 2>&1 &
    fi
    $BB echo "$key $! $cip $svc" >> "$pids.new"
    $BB echo "vmf-expose: $proto $lport -> $cip:$cport ($svc)"
  done < "$desired"
  mv "$pids.new" "$pids"
  # Port table the host poller turns into slirp hostfwd.
  : > "$live.tmp"
  while read -r proto lport cip cport svc; do
    [ -n "${proto:-}" ] || continue
    if [ "${cip:-}" = "vm" ]; then
      $BB echo "$proto $lport vm" >> "$live.tmp"
    elif [ -n "$cip" ]; then
      $BB echo "$proto $lport $cip:$cport $svc" >> "$live.tmp"
    fi
  done < "$desired"
  sort -u "$live.tmp" > "$live.new" 2>/dev/null
  mv "$live.new" "$live"
  $BB sleep 2
done
EXPOSEEOF
    $BB sh /vmf/expose.sh >/vmf/expose.log 2>&1 &
    $BB echo "vmf-init: expose daemon started (expose=$expose_mode)"
  fi
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