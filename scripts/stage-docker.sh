#!/usr/bin/env bash
# Stage the docker-in-VM bundle: static dockerd + docker + containerd +
# runc + docker-proxy + the compose plugin, from Docker's official
# static tarball plus the compose release binary. Downloads are TOFU
# pinned (sha256 recorded on first fetch; drift is a hard error).
# Output: $VMF_DOCKER_BUNDLE (default ~/.local/share/vmf/docker-bundle)
# with bin/{dockerd,docker,...} and bin/docker-compose.
set -euo pipefail
out="${VMF_DOCKER_BUNDLE:-$HOME/.local/share/vmf/docker-bundle}"
pins_dir="$out"
mkdir -p "$out"

DOCKER_VER="${DOCKER_VERSION:-27.5.1}"
COMPOSE_VER="${COMPOSE_VERSION:-2.38.1}"
tgz_url="https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VER}.tgz"
compose_url="https://github.com/docker/compose/releases/download/v${COMPOSE_VER}/docker-compose-linux-x86_64"

fetch_pinned() { # url dest pinfile
  local url="$1" dest="$2" pin="$3"
  if [[ -f "$dest" ]]; then
    echo "cache hit: $dest"
    return 0
  fi
  local want got
  if [[ -f "$pin" ]]; then
    want="$(cat "$pin")"
    echo "fetching (pin verified): $url"
  else
    echo "fetching (TOFU: recording sha256): $url"
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dest.tmp"
  else
    nix shell nixpkgs#curl -c curl -fsSL "$url" -o "$dest.tmp"
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    got="$(sha256sum "$dest.tmp" | cut -d' ' -f1)"
  else
    got="$(nix shell nixpkgs#coreutils -c sha256sum "$dest.tmp" | cut -d' ' -f1)"
  fi
  if [[ -f "$pin" && "$got" != "$want" ]]; then
    echo "error: digest drift for $url: pinned $want got $got" >&2
    rm -f "$dest.tmp"
    exit 1
  fi
  printf '%s\n' "$got" > "$pin"
  mv "$dest.tmp" "$dest"
}

fetch_pinned "$tgz_url" "$out/docker.tgz" "$pins_dir/docker.tgz.sha256"

# socat: the VM-side port forwards (published host ports -> container
# IPs) ride a static socat instead of docker-proxy, which needs the
# iptables-free userland path that misbehaves on this guest.
if [[ ! -f "$out/bin/socat" ]]; then
  echo "building static socat..."
  nix build --impure --no-link --print-out-paths \
    --expr 'let pkgs = import <nixpkgs> {}; in pkgs.pkgsStatic.socat' \
    > "$out/.socat-path"
  cp "$(cat "$out/.socat-path")/bin/socat" "$out/bin/socat"
fi

# iptables: dockerd's embedded DNS resolver (127.0.0.11 DNAT per
# container) invokes the iptables binary even with --iptables=false;
# without it every user-defined network loses name resolution.
if [[ ! -f "$out/bin/iptables" ]]; then
  echo "building static iptables..."
  nix build --impure --no-link --print-out-paths \
    --expr 'let pkgs = import <nixpkgs> {}; in pkgs.pkgsStatic.iptables' \
    | tail -1 > "$out/.iptables-path"
  cp "$(cat "$out/.iptables-path")"/bin/* "$out/bin/"
fi
fetch_pinned "$compose_url" "$out/docker-compose" "$pins_dir/docker-compose.sha256"

if [[ ! -f "$out/bin/dockerd" ]]; then
  mkdir -p "$out/bin"
  tar -xzf "$out/docker.tgz" -C "$out" --strip-components=1 docker
  mv "$out"/{dockerd,docker,containerd,containerd-shim-runc-v2,runc,docker-init,docker-proxy} "$out/bin/" 2>/dev/null || \
    mv "$out"/dockerd "$out"/docker "$out"/containerd "$out"/runc "$out"/docker-init "$out"/docker-proxy "$out/bin/"
  rm -f "$out/ctr" "$out/containerd-shim" 2>/dev/null || true
  mv "$out/docker-compose" "$out/bin/docker-compose"
  chmod 755 "$out/bin/"*
  echo "docker bundle staged: $out"
else
  [[ -f "$out/bin/docker-compose" ]] || mv "$out/docker-compose" "$out/bin/docker-compose"
  echo "docker bundle cache hit: $out"
fi
"$out/bin/dockerd" --version
"$out/bin/docker-compose" version --short
