#!/usr/bin/env bash
# Derive a local ssh-enabled image from pinned OCI bytes. Runs inside
# `buildah unshare` (called by oci-run.sh). Adds exactly one layer on top
# of the pinned image: /vmf containing the static dropbear + busybox
# bundle and the guest init script, plus /etc/passwd fixups for distroless
# bases (dropbear needs a root entry with an existing shell).
# Idempotent: cached under the content-addressed tag vmf-ssh:$VMF_TAG in the
# buildah store; rebuilt only when that tag is absent (e.g. digest drift
# after a deliberate pin refresh).
set -euo pipefail
: "${VMF_REF:?}" "${VMF_DERIVED:?}" "${VMF_TAG:?}" "${VMF_BUNDLE:?}" "${VMF_INIT:?}" "${VMF_DERIVE_DIR:?}"

if buildah inspect --type image "$VMF_DERIVED" >/dev/null 2>&1; then
  echo "derive cache hit: $VMF_DERIVED"
  exit 0
fi
echo "deriving $VMF_DERIVED (base $VMF_REF): one layer with dropbear + busybox + init..."
ctr=$(buildah from "$VMF_REF")
rootfs=$(buildah mount "$ctr")
trap 'buildah rm "$ctr" >/dev/null 2>&1 || true' EXIT

mkdir -p "$rootfs/vmf" "$rootfs/vmf/bin" "$rootfs/root"
cp "$VMF_BUNDLE/dropbear" "$rootfs/vmf/dropbear"
cp "$VMF_BUNDLE/dropbearkey" "$rootfs/vmf/dropbearkey"
cp "$VMF_BUNDLE/busybox" "$rootfs/vmf/busybox"
cp "$VMF_INIT" "$rootfs/vmf/init.sh"
ln -sf busybox "$rootfs/vmf/sh"
chmod 755 "$rootfs/vmf/dropbear" "$rootfs/vmf/busybox" "$rootfs/vmf/init.sh"

# OpenSSH suite for the qemu engine (real kernel): sshd + its split
# session binary (sshd looks for sshd-session at its compiled-in store
# path, so the file is placed there) and ssh-keygen for host keys.
cp "$VMF_BUNDLE/sshd" "$rootfs/vmf/sshd"
cp "$VMF_BUNDLE/ssh-keygen" "$rootfs/vmf/ssh-keygen"
chmod 755 "$rootfs/vmf/sshd" "$rootfs/vmf/ssh-keygen"
libexec=$(cat "$VMF_BUNDLE/sshd-libexec-path")
mkdir -p "$rootfs$libexec"
cp "$VMF_BUNDLE/sshd-session" "$rootfs$libexec/sshd-session"
cp "$VMF_BUNDLE/sshd-auth" "$rootfs$libexec/sshd-auth"
chmod 755 "$rootfs$libexec/sshd-session" "$rootfs$libexec/sshd-auth"
mkdir -p "$rootfs/etc/ssh"
cp "$VMF_BUNDLE/moduli" "$rootfs/etc/ssh/moduli"
mkdir -p "$rootfs/var/empty"
cat > "$rootfs/etc/ssh/sshd_config" <<'EOF'
Port 22
ListenAddress 0.0.0.0
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
AuthorizedKeysFile .ssh/authorized_keys
PidFile /var/run/sshd.pid
Subsystem sftp internal-sftp
EOF

# Fallback tool symlinks: busybox dispatches on argv[0], so one symlink
# per applet gives every image (including distroless) a full coreutils
# set under /vmf/bin. The directory is APPENDED to PATH by oci-run.sh,
# so real image binaries always win where they exist.
while read -r applet; do
  [ -n "$applet" ] || continue
  ln -sf /vmf/busybox "$rootfs/vmf/bin/$applet"
done < <("$VMF_BUNDLE/busybox" --list)

# /etc/passwd: distroless bases carry none; dropbear requires a root entry
# with a shell it accepts. Root's shell is forced to /vmf/sh (busybox) so
# every base behaves identically (alpine's /bin/ash gets rejected by
# dropbear's shell validation).
passwd="$rootfs/etc/passwd"
root_shell_fix='root:x:0:0:root:/root:/vmf/sh'
if [ -f "$passwd" ]; then
  if ! grep -q '^root:' "$passwd"; then
    printf '%s\n' "$root_shell_fix" >> "$passwd"
  else
    awk -F: -v OFS=: '$1=="root"{$7="/vmf/sh"} {print}' "$passwd" > "$passwd.new"
    mv "$passwd.new" "$passwd"
  fi
else
  mkdir -p "$(dirname "$passwd")"
  printf '%s\n' "$root_shell_fix" > "$passwd"
fi

# Dropbear rejects shells not listed in /etc/shells (when that file
# exists). Register /vmf/sh there.
shells="$rootfs/etc/shells"
if [ -f "$shells" ]; then
  grep -qx '/vmf/sh' "$shells" || printf '%s\n' '/vmf/sh' >> "$shells"
else
  printf '%s\n' '/bin/sh' '/vmf/sh' > "$shells"
fi

# OpenSSH privilege separation needs the sshd user to exist.
if grep -q '^sshd:' "$passwd"; then
  :
else
  printf 'sshd:x:74:65534:privsep:/var/empty:/vmf/sh\n' >> "$passwd"
fi

# Apache vhost shim: under krunvm's TSI port mapping, <VirtualHost *:80>
# sections never match in the running daemon ("NameVirtualHost *:80 has
# no VirtualHosts" at every re-parse, while a fresh -S parse shows them),
# so vhost-declared ScriptAliases silently vanish and CGI paths 404.
# Main-server directives DO apply, so re-declare the standard Debian
# cgi-bin mapping at main level when the image uses that layout.
if [ -f "$rootfs/etc/apache2/apache2.conf" ] && [ -d "$rootfs/etc/apache2/conf.d" ] \
   && [ -d "$rootfs/usr/lib/cgi-bin" ] && [ ! -f "$rootfs/etc/apache2/conf.d/zzz-vmf-cgi.conf" ]; then
  cat > "$rootfs/etc/apache2/conf.d/zzz-vmf-cgi.conf" <<'EOF'
# Added by vmfactory: main-server fallback for the vhost ScriptAlias.
# krunvm's TSI virtual bind drops <VirtualHost *:80> matching; this
# re-declares the Debian cgi-bin convention at main-server level.
ScriptAlias /cgi-bin/ /usr/lib/cgi-bin/
<Directory "/usr/lib/cgi-bin">
    AllowOverride None
    Options +ExecCGI -MultiViews +SymLinksIfOwnerMatch
    Order allow,deny
    allow from all
</Directory>
EOF
fi

buildah commit "$ctr" "$VMF_DERIVED" >/dev/null
mkdir -p "$VMF_DERIVE_DIR"
printf 'base=%s\nderived=%s\ncreated=%s\n' "$VMF_REF" "$VMF_DERIVED" "$(date -Is)" \
  > "$VMF_DERIVE_DIR/$VMF_TAG.conf"
echo "derived image ready: $VMF_DERIVED"