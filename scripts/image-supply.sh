#!/usr/bin/env bash
# image-supply.sh <ref> <out.tar> — host-side image supply.
#
# The host owns the trust boundary: it pulls with its own CA pool and
# credentials, applies the same TOFU digest discipline as every other
# pull in this repo, and archives the image for the guest's `docker
# load` (the compose data-drive pattern, generalized). The guest never
# dials a registry.
#
# Exit 0 supplied · 1 pull/inspect failed · 2 pin drift.
set -euo pipefail
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=vmf_lib.sh
. "$SCRIPTS_DIR/vmf_lib.sh"
vmf_tool buildah
BUILD_BIN=("${TOOL[@]}")

ref="$1"
# Normalize short names: the host builders refuse them without a
# containers-registries.conf (buildah rc=125, "short-name ... did not
# resolve"). Same rules as the boot path's docker normalization.
case "$ref" in
  localhost/*|vmf-*) ;;
  */*) head="${ref%%/*}"
       [[ "$head" == *.* || "$head" == *:* ]] || ref="docker.io/$ref" ;;
  *) ref="docker.io/library/$ref" ;;
esac
out="$2"
PINS="${VMF_OCI_PINS:-$HOME/.vmf/oci-pins}"
mkdir -p "$(dirname "$PINS")" "$(dirname "$out")"

digest=$("${BUILD_BIN[@]}" inspect --type image "$ref" 2>/dev/null \
  | grep -oE 'sha256:[0-9a-f]{64}' | awk 'NR==1{v=$0} END{print v}') || digest=""
if [[ -z "$digest" ]]; then
  echo "supply: pulling $ref (host)..."
  "${BUILD_BIN[@]}" pull --retry 2 "$ref" >/dev/null
  digest=$("${BUILD_BIN[@]}" inspect --type image "$ref" 2>/dev/null \
    | grep -oE 'sha256:[0-9a-f]{64}' | awk 'NR==1{v=$0} END{print v}') || digest=""
fi
[[ -n "$digest" ]] || { echo "error: no digest for $ref" >&2; exit 1; }

# TOFU pin: freeze the first-seen digest; drift is a hard error.
old=$(awk -v r="$ref" '$1 == r {print $2; exit}' "$PINS" 2>/dev/null || true)
if [[ -n "$old" && "$old" != "$digest" ]]; then
  echo "error: pin drift for $ref: $old -> $digest" >&2
  exit 2
fi
[[ -n "$old" ]] || printf '%s %s\n' "$ref" "$digest" >> "$PINS"

"${BUILD_BIN[@]}" push "$ref" "docker-archive:$out:$ref" >/dev/null
echo "supply: $ref@$digest -> $out"
