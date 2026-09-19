#!/vmf/sh
# Guest init for vmfactory microVMs: starts dropbear (SSH server) in the
# background, then runs the image entrypoint as a supervised child.
# Injected into the derived image by oci-run.sh. Runtime inputs arrive on
# the read-only /vmf-run mount: authorized_keys, env, argv.sh, cwd, uid.
# Requires nothing from the image: all file tools come from /vmf/busybox.
set -u

BB=/vmf/busybox

$BB mkdir -p /run/dropbear /root/.ssh
# PTY support: libkrun's init.krun already mounts devpts at /dev/pts with
# ptmxmode=000. Remount it with usable modes; do NOT stack a second devpts
# instance on the same path (a stacked instance breaks the /dev/ptmx ->
# /dev/pts/N slave lookup with "No such file").
$BB mount -o remount,mode=620,ptmxmode=0666 /dev/pts 2>/dev/null || true

$BB cp /vmf-run/auth/authorized_keys /root/.ssh/authorized_keys
$BB chmod 700 /root/.ssh
$BB chmod 600 /root/.ssh/authorized_keys

# Image env (PATH etc.) + -e overrides, applied to the app below.
[ -r /vmf-run/env ] && [ -r /vmf-run/argv.sh ] && [ -r /vmf-run/cwd ] || {
  echo "vmf-init: /vmf-run inputs missing" >&2
  exit 2
}
. /vmf-run/env

# SSH server: -E stderr logging (no syslogd in the guest), -s no password
# auth (pubkey only), -F foreground. Host keys are generated eagerly on
# first boot: -R lazy generation fails here.
[ -d /etc/dropbear ] || $BB mkdir -p /etc/dropbear
for t in ed25519 ecdsa rsa; do
  [ -f "/etc/dropbear/dropbear_${t}_host_key" ] || \
    /vmf/dropbearkey -t "$t" $([ "$t" = rsa ] && echo "-s 2048") \
      -f "/etc/dropbear/dropbear_${t}_host_key" >/dev/null 2>&1
done
/vmf/dropbear -EsF -p 22 &

cd "$($BB cat /vmf-run/cwd)"
# Everything the init itself runs comes from /vmf or shell builtins:
# distroless bases carry no coreutils and PATH may not reach /bin.
eval "set -- $(/vmf/busybox cat /vmf-run/argv.sh)"
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
# Exit immediately with the app's status: init.krun (the real PID 1) reaps
# the dropbear child, and the VM must terminate with the app for --rm.
exit $rc