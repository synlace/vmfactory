#!/usr/bin/env bash
# build-fat-base.sh — freeze the common runtimes into one host-local
# image, built ONCE. The gap-fill prefers it (scripts/vmf_plan.py reads
# the ready marker), so direct plans stop emitting apt/npm provisioning
# and boots skip the package window entirely.
#
# Contents: node 22 + npm, python3 + pip, sqlite3, nginx, git, curl,
# ca-certificates. No compilers, no databases — keep it lean.
#
# Idempotent: an existing ready marker plus a resolvable tag skips the
# build. VMF_FAT_FROM overrides the seed image; VMF_BASE_IMAGE the tag.
# Rebuild with --force.
#
# usage: build-fat-base.sh [--force]
set -euo pipefail

TAG="${VMF_BASE_IMAGE:-localhost/vmf-fat-base:1}"
FROM="${VMF_FAT_FROM:-node:22-bookworm-slim}"
READY="$HOME/.local/share/vmf/fat-base.ready"

mkdir -p "$(dirname "$READY")"

storage_has() { # tag -> rc
  local probe
  for probe in "containers-storage:$1" "containers-storage:localhost/$1"; do
    if skopeo inspect --format '{{.Digest}}' "$probe" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

if [[ "${1:-}" != "--force" && -f "$READY" ]] \
    && storage_has "$TAG"; then
  echo "fat base: ready ($(head -1 "$READY"))"
  exit 0
fi

# buildah is the canonical builder (the substrate's engine); docker is
# the fallback when only a docker runtime exists.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/Dockerfile" <<'EOF'
FROM {{FROM}}
RUN apt-get update && DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-install-recommends \
      python3 python3-pip sqlite3 nginx-light git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
EOF
sed -i "s|{{FROM}}|$FROM|" "$tmp/Dockerfile"

if command -v buildah >/dev/null 2>&1; then
  buildah bud --layers -t "$TAG" "$tmp"
elif command -v docker >/dev/null 2>&1; then
  docker build -t "$TAG" "$tmp"
  # Mirror into containers-storage so skopeo/krunvm resolve it without
  # a registry round-trip.
  skopeo copy --dest-precompute-digests "docker-daemon:$TAG" \
    "containers-storage:$TAG" 2>/dev/null || true
else
  echo "error: need buildah or docker on the host to build the fat base" >&2
  exit 1
fi

printf '%s %s\n' "$TAG" "$(date -Iseconds)" > "$READY"
echo "fat base: built $TAG (from $FROM); marker: $READY"