#!/usr/bin/env bash
# Stage the static guest ssh bundle into $VMF_SSH_BUNDLE (default
# ~/.local/share/vmf/ssh-bundle). Everything is a static musl build from
# nixpkgs:
#   busybox, dropbear + dropbearkey (krunvm engine)
#   openssh suite (qemu engine): sshd, ssh-keygen, sshd-session,
#     sshd-auth, moduli, plus the compiled-in libexec path record
# openssh is overridden with --disable-utmp --disable-utmpx --disable-wtmp
# --disable-wtmpx: musl's paths.h points _PATH_{UTMP,WTMP} at
# /dev/null/{utmp,wtmp}, so without those flags sshd logs
# 'wtmp_write: problem writing /dev/null/wtmp' on every session.
# Re-run this script when nixpkgs or the override flags change; oci-run.sh
# hashes the bundle contents into the derive tag, so the next run
# re-derives affected images automatically.
set -euo pipefail
out="${VMF_SSH_BUNDLE:-$HOME/.local/share/vmf/ssh-bundle}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

build() {
  nix build --no-link --print-out-paths --impure --expr "let pkgs = import <nixpkgs> {}; in $1"
}

echo "building busybox..."
bb=$(build 'pkgs.pkgsStatic.busybox')
echo "building dropbear..."
db=$(build 'pkgs.pkgsStatic.dropbear')
echo "building openssh (utmp/wtmp disabled)..."
# openssh has extra outputs (man); keep the one that carries the binaries.
sshd_all=$(build 'pkgs.pkgsStatic.openssh.overrideAttrs (old: {
  configureFlags = old.configureFlags ++ [
    "--disable-utmp" "--disable-utmpx" "--disable-wtmp" "--disable-wtmpx"
  ];
})')
sshd=$(echo "$sshd_all" | grep -v -- '-man$' | head -n1)

mkdir -p "$out"
chmod u+w "$out"/* 2>/dev/null || true
cp "$bb/bin/busybox" "$out/busybox"
cp "$db/bin/dropbear" "$out/dropbear"
cp "$db/bin/dropbearkey" "$out/dropbearkey"
cp "$sshd/bin/sshd" "$out/sshd"
cp "$sshd/bin/ssh-keygen" "$out/ssh-keygen"
cp "$sshd/libexec/sshd-session" "$out/sshd-session"
cp "$sshd/libexec/sshd-auth" "$out/sshd-auth"
cp "$sshd/etc/ssh/moduli" "$out/moduli"
printf '%s/libexec\n' "$sshd" > "$out/sshd-libexec-path"
chmod 755 "$out/busybox" "$out/dropbear" "$out/dropbearkey" \
  "$out/sshd" "$out/ssh-keygen" "$out/sshd-session" "$out/sshd-auth"
echo "bundle staged: $out"
echo "openssh: $sshd"
