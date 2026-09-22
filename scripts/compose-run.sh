#!/usr/bin/env bash
# Compose mode: run a docker-compose project inside ONE microVM.
#
# Pipeline:
#   1. plan: scan/resolve/gap-fill/translate the compose file into
#      plan.json — owned by scripts/vmf_plan.py (services, image|build,
#      ports, env, depends_on)
#   2. refine: an unconsumed --intent becomes refines.json (bounded
#      replicas overlay) — vmf_plan.py refine
#   3. host-side builds/pulls through buildah — pinned digests only
#   4. flatten: plan + refines → flattened compose file (image tags
#      only: no builds, no in-VM pulls) and ports.txt — vmf_plan.py
#   5. docker-archive every service image onto a data drive (ext4)
#      together with the pinned static docker bundle
#   6. exec oci-run.sh with the primary service's local tag, plus
#      VMF_MODE=compose + VMF_DATA_DRIVE — the normal run path
#      (derive layer, ssh, state, teardown) applies unchanged; the
#      guest init swaps entrypoint supervision for dockerd + compose up
#
# In-VM networking: dockerd runs with --iptables=false; inter-container
# traffic rides the compose bridge (kernel VETH+BRIDGE), published
# ports bind in the VM and use docker's userland proxy, and the host
# reaches them through the same slirp hostfwd as any other VM.
# The data drive is cached by content (plan + image digests + bundle).
set -euo pipefail
: "${VMF_COMPOSE_SRC:?}" "${VMF_NAME:?}" "${VMF_COMPOSE_SLUG:?}"
RUNS_DIR="${VMF_RUNS:-$HOME/.vmf/runs}"
DOCKER_BUNDLE="${VMF_DOCKER_BUNDLE:-$HOME/.local/share/vmf/docker-bundle}"
COMPOSE_CACHE="${VMF_COMPOSE_CACHE:-$HOME/.vmf/compose}"
PINS="${VMF_OCI_PINS:-$HOME/.vmf/oci-pins}"
VMF_COMPOSE_RUNID="${VMF_COMPOSE_RUNID:-0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export VMF_SCRIPTS_DIR="$SCRIPT_DIR"
# shellcheck source=vmf_lib.sh
. "$SCRIPT_DIR/vmf_lib.sh"

# OCI reference components must be lowercase with single separators
# ("OpenStock" from a repo name is not a valid tag fragment).
ref_component() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' \
    | sed -e 's/[^a-z0-9._-]/-/g' -e 's/[-._][-._]*/-/g' -e 's/^[-._]*//' -e 's/[-._]*$//'
}
tag_for() { printf 'localhost/vmf-compose/%s-%s:%s' "$(ref_component "$VMF_COMPOSE_SLUG")" "$(ref_component "$1")" "$VMF_COMPOSE_RUNID"; }

vmf_tool buildah krunvm buildah
BUILD_BIN=("${TOOL[@]}")
if command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" 2>/dev/null; then
  PY=(python3)
else
  PY=(nix shell --impure --expr 'with import <nixpkgs> {}; python3.withPackages (p: [ p.pyyaml ])' -c python3)
fi

plan_tmp=$(mktemp -d)
data_tmp="$plan_tmp/data"
# EXIT alone does not fire on SIGTERM (timeout kills), so trap INT and
# TERM too — a killed run must not leak a ~800 MB staging dir per
# attempt.
trap 'rm -rf "$plan_tmp"' EXIT INT TERM
if [[ "${VMF_COMPOSE_KEEPPLAN:-0}" == "1" ]]; then
  # Keep the working dir for diagnosis instead of cleaning it up.
  trap 'echo "compose: kept working dir: $plan_tmp"' EXIT INT TERM
fi

# --- plan: scan / resolve / gap-fill / translate --------------------------
"${PY[@]}" "$SCRIPT_DIR/vmf_plan.py" plan "$VMF_COMPOSE_SRC" "$plan_tmp/plan.json"

# Explicit build-arg "variant" override (env-supplied).
if [[ -n "${VMF_VARIANT_OVERRIDE:-}" ]]; then
  "${PY[@]}" "$SCRIPT_DIR/vmf_plan.py" variant "$plan_tmp/plan.json" "$VMF_VARIANT_OVERRIDE"
fi

vmf_tool jq
JQ=("${TOOL[@]}" -r)
vmf_tool awk gawk
AWK=("${TOOL[@]}")
# Gap-fill direct mode: the VM is the sandbox. Run the base image with
# the resolved argv; the repo tar and the install script ride the
# per-run inputs (the guest installs at boot, before the app).
if [[ -f "$plan_tmp/direct.json" ]]; then
  base=$("${JQ[@]}" -r '.base_image' "$plan_tmp/direct.json")
  mapfile -t gcmd < <("${JQ[@]}" -r '.command[]' "$plan_tmp/direct.json")
  ginst=$("${JQ[@]}" -r '.install | join("\n")' "$plan_tmp/direct.json")
  genv_args=()
  while IFS=$'\t' read -r k v; do
    [[ -n "$k" ]] && genv_args+=(-e "$k=$v")
  done < <("${JQ[@]}" -r '(.env // {}) | to_entries[] | [(.key|tostring), (.value|tostring)] | @tsv' \
    "$plan_tmp/direct.json")
  gneed_docker=$("${JQ[@]}" -r '.needs_docker // false' "$plan_tmp/direct.json")
  # Plan sizing: the gap-fill plan sizes the VM (memory_mb; the clamp
  # floor is 2048 when needs_docker). The user's explicit --memory
  # always wins — pass 1 exports VMF_RUN_MEM_EXPLICIT for it.
  if [[ "${VMF_RUN_MEM_EXPLICIT:-0}" != "1" ]]; then
    gmem=$("${JQ[@]}" -r '.memory_mb // 0' "$plan_tmp/direct.json")
    if [[ "$gmem" =~ ^[0-9]+$ && "$gmem" -gt 0 ]]; then
      export VMF_RUN_MEM="$gmem"
    fi
  fi
  # The verify stage reads the plan here (plan_tmp does not survive
  # into the rundir) and writes a revised plan back to the gap-fill
  # cache (the key is content-derived; the sidecar carries it).
  docker_env=()
  # jq prints the literal string "false"; a bare ${var:+} expansion
  # would stage dockerd for needs_docker=false and starve the app.
  [[ "$gneed_docker" == "true" ]] && docker_env=(VMF_WANT_DOCKER=1)
  verify_env=()
  [[ -f "$plan_tmp/direct.json" ]] && verify_env+=(VMF_VERIFY_PLAN="$plan_tmp/direct.json")
  [[ -f "$plan_tmp/direct.json.cache" ]] && \
    verify_env+=(VMF_VERIFY_CACHE="$(cat "$plan_tmp/direct.json.cache")")
  # Direct plans carry guest tcp ports (PROMPT_V 5); the host publishes
  # each 1:1 via the boot-time -p list (qemu has no dynamic hostfwd).
  gports_args=()
  while IFS= read -r gp; do
    [[ -n "$gp" ]] && gports_args+=(-p "$gp:$gp")
  done < <("${JQ[@]}" -r '.ports[]?' "$plan_tmp/direct.json")
  echo "gap-fill direct: base=$base command=${gcmd[*]} ports=${gports_args[*]:-none} needs_docker=$gneed_docker mem=${VMF_RUN_MEM:-1024}"
  exec env -u VMF_MODE -u VMF_COMPOSE_SRC \
    VMF_REPO_DIR="$VMF_COMPOSE_SRC" VMF_INSTALL_CMD="$ginst" \
    ${docker_env[@]+"${docker_env[@]}"} \
    ${verify_env[@]+"${verify_env[@]}"} \
    "$SCRIPT_DIR/oci-run.sh" \
    ${genv_args[@]+"${genv_args[@]}"} \
    ${gports_args[@]+"${gports_args[@]}"} \
    "$base" -- ${gcmd[@]+"${gcmd[@]}"}
fi

svc_count=$("${JQ[@]}" '.services | length' "$plan_tmp/plan.json")
primary=$("${JQ[@]}" -r '.primary' "$plan_tmp/plan.json")
PROJ="$VMF_COMPOSE_SRC/$("${JQ[@]}" -r '.project_dir // "."' "$plan_tmp/plan.json")"
echo "compose: $svc_count services (primary: $primary)"

# Intent refinement: an unconsumed --intent phrase (single-project run)
# becomes a bounded, validated plan overlay - e.g. "Run 5 instances"
# scales a service. The base plan (and its cache) stays untouched; the
# flatten step applies the overlay deterministically.
export VMF_PLAN_REFINES="$plan_tmp/refines.json"
if [[ -n "${VMF_RUN_INTENT:-}" && ! -f "$VMF_PLAN_REFINES" ]]; then
  "${PY[@]}" "$SCRIPT_DIR/vmf_plan.py" refine "$plan_tmp/plan.json" "$VMF_PLAN_REFINES" "$VMF_RUN_INTENT"
fi

# --- host-side builds/pulls (pinned digests) ----------------------------
mkdir -p "$(dirname "$PINS")"
pin_image() { # name digest
  grep -qxF "$1 $2" "$PINS" 2>/dev/null || printf '%s %s\n' "$1" "$2" >> "$PINS"
}

declare -A SVC_TAGS=() SVC_DIGEST=() SVC_IMAGES=() SVC_KINDS=()
while IFS=$'\t' read -r name kind rest; do
  tag="$(tag_for "$name")"
  SVC_KINDS["$name"]="$kind"
  if [[ "$kind" == "image" ]]; then
    image="$rest"
    SVC_IMAGES["$name"]="$image"
    echo "compose: pulling $name ($image)..."
    "${BUILD_BIN[@]}" pull "$image"
    # Alias the pulled image to the guest-side local tag so the run
    # path (derive, squashfs) can address it like a built image.
    "${BUILD_BIN[@]}" tag "$image" "$tag"
    digest=$("${BUILD_BIN[@]}" inspect --type image "$image" \
      | grep -oE 'sha256:[0-9a-f]{64}' | awk 'NR==1{v=$0} END{print v}')
    pin_image "$image" "$digest"
    SVC_DIGEST["$name"]="$digest"
  else
    dockerfile="$plan_tmp/df-$name"
    # Build contexts resolve against the project dir (a monorepo compose
    # file may live in a subdirectory of the clone).
    ctx="$PROJ/$rest"
    echo "compose: building $name (context: ${rest#./})..."
    # Patch unqualified FROM images in a generated copy: buildah refuses
    # short names without a registry (FROM php@sha256:... etc.).
    "${AWK[@]}" '
      /^[[:space:]]*FROM[[:space:]]/ {
        out = $1; i = 2
        while (i <= NF && $i ~ /^--/) { out = out " " $i; i++ }
        if (i <= NF) {
          img = $i; t = img
          sub(/@.*/, "", t); sub(/:.*/, "", t)
          if (t != "scratch" && t !~ /[\/.]/) img = "docker.io/library/" img
          else {
            f1 = t; sub(/\/.*/, "", f1)
            if (t ~ /\// && f1 !~ /\./ && f1 != "localhost") img = "docker.io/" img
          }
          out = out " " img; i++
        }
        while (i <= NF) { out = out " " $i; i++ }
        print out; next
      }
      { print }
    ' "$ctx/$(${JQ[@]} -r --arg n "$name" '.services[] | select(.name==$n) | .build.dockerfile' "$plan_tmp/plan.json")" > "$dockerfile"
    # COPY --from=<name> images: buildah resolves them from local storage
    # only. Pre-pull every external ref; refs that name a local build
    # stage (FROM ... AS <stage>) must not be pulled.
    stage_names=$("${AWK[@]}" 'tolower($0) ~ /^[[:space:]]*from[[:space:]]/ {
      for (i = 1; i <= NF; i++) if (tolower($i) == "as" && i < NF) print $(i+1)
    }' "$dockerfile" | tr '[:upper:]' '[:lower:]')
    seen_from=""
    while IFS= read -r ref; do
      [[ -z "$ref" || "$ref" == \$* ]] && continue
      lr="$(printf '%s' "$ref" | tr '[:upper:]' '[:lower:]')"
      skip=0
      while IFS= read -r s; do [[ -n "$s" && "$s" == "$lr" ]] && skip=1; done <<< "$stage_names"
      [[ "$skip" == 1 ]] && continue
      case "$seen_from" in *"|$lr|"*) continue ;; esac
      seen_from="$seen_from|$lr|"
      t="${lr%%@*}"; t="${t%%:*}"
      if [[ "$t" != "scratch" && "$t" != *"/"* && "$t" != *"."* ]]; then
        lr="docker.io/library/$lr"
      elif [[ "$lr" == *"/"* ]]; then
        f1="${lr%%/*}"
        case "$f1" in localhost|*.*|*:*) ;; *) lr="docker.io/$lr" ;; esac
      fi
      echo "compose: pulling stage image ($lr)..."
      "${BUILD_BIN[@]}" pull "$lr" || true
    done < <("${AWK[@]}" '{
      line = $0
      while (match(line, /--[Ff][Rr][Oo][Mm]=[A-Za-z0-9_.$@:\/-]+/)) {
        print substr(line, RSTART + 7, RLENGTH - 7)
        line = substr(line, RSTART + RLENGTH)
      }
    }' "$dockerfile")
    # Synthesized env (missing env_file) must be visible to the build:
    # some apps read it while building (Next.js page-data collection).
    # The generated copy declares ARG/ENV right after the last FROM, so
    # the app stage carries them; values must be substitution-free.
    "${JQ[@]}" -r --arg n "$name" \
      '.services[] | select(.name==$n) | ((.synth_env // {}) | to_entries[]) | [(.key|tostring), (.value|tostring)] | @tsv' \
      "$plan_tmp/plan.json" > "$plan_tmp/synth-$name.tsv"
    if [[ -s "$plan_tmp/synth-$name.tsv" ]]; then
      while IFS=$'\t' read -r k v; do
        [[ -n "$k" ]] || continue
        printf 'ARG %s\nENV %s="%s"\n' "$k" "$k" "$v" >> "$plan_tmp/synth-$name.block"
      done < "$plan_tmp/synth-$name.tsv"
      # Last FROM line of the dockerfile (the app stage), then splice the
      # ARG/ENV block after it.
      last_from=$("${AWK[@]}" 'tolower($0) ~ /^[[:space:]]*from[[:space:]]/ { last = NR }
        END { print last + 0 }' "$dockerfile")
      if [[ "${last_from:-0}" -gt 0 ]]; then
        "${AWK[@]}" -v bf="$plan_tmp/synth-$name.block" -v at="$last_from" '
          { print; if (FNR == at) { while ((getline l < bf) > 0) print l } }
        ' "$dockerfile" > "$dockerfile.tmp" && mv "$dockerfile.tmp" "$dockerfile"
        echo "compose: build env injected at FROM line $last_from"
      fi
    fi
    build_args=()
    "${BUILD_BIN[@]}" build -t "$tag" -f "$dockerfile" \
      ${build_args[@]+"${build_args[@]}"} "$ctx" >/dev/null
    # Digest of the built image. The reader must drain the full inspect
    # output: head -1 closes the pipe early and SIGPIPEs grep (141) when
    # the output carries several sha256 matches, which pipefail turns
    # into a silent script exit.
    digest=$("${BUILD_BIN[@]}" inspect --type image "$tag" \
      | grep -oE 'sha256:[0-9a-f]{64}' | awk 'NR==1{v=$0} END{print v}')
    SVC_DIGEST["$name"]="$digest"
  fi
  SVC_TAGS["$name"]="$tag"
done < <("${JQ[@]}" -r '.services[] | [.name, (if .image then "image" else "build" end), (.image // (.build.context | sub("^\\./"; "")))] | @tsv' "$plan_tmp/plan.json")

# manifest for the flattened compose generation
digests_json=$(printf '{%s}' "$(for k in "${!SVC_DIGEST[@]}"; do printf '"%s":"%s",' "$k" "${SVC_DIGEST[$k]}"; done | sed 's/,$//')")
tags_json=$(printf '{%s}' "$(for k in "${!SVC_TAGS[@]}"; do printf '"%s":"%s",' "$k" "${SVC_TAGS[$k]}"; done | sed 's/,$//')")
"${JQ[@]}" -n --slurpfile plan "$plan_tmp/plan.json" \
  --argjson digests "$digests_json" --argjson tags "$tags_json" \
  '$plan[0] + {digests: $digests, tags: $tags}' > "$plan_tmp/manifest.json"

# --- flattened compose file (no builds, local tags only) ------------------
"${PY[@]}" "$SCRIPT_DIR/vmf_plan.py" flatten "$plan_tmp/manifest.json" \
  "$plan_tmp/compose.yaml" "$plan_tmp/ports.txt" "$VMF_PLAN_REFINES"
# --- data drive ----------------------------------------------------------
stage="$plan_tmp/data"
mkdir -p "$stage/images" "$stage/docker/bin"
cp "$plan_tmp/compose.yaml" "$stage/compose.yaml"
cp "$plan_tmp/ports.txt" "$stage/ports.txt"
cp "$DOCKER_BUNDLE"/bin/* "$stage/docker/bin/"
while IFS=$'\t' read -r name kind rest; do
  # Built services push their local tag; pulled services push by digest
  # ref (the store only has the upstream name). Either way the tar
  # carries the guest-side tag_for() name, which compose references.
  if [[ "${SVC_KINDS[$name]:-}" == "image" ]]; then
    ref="${SVC_IMAGES[$name]}"
    # Only append the digest when the ref does not carry one already
    # (compose files may pin their own @sha256).
    [[ "$ref" == *@sha256:* ]] || ref="${ref}@${SVC_DIGEST[$name]:-}"
  else
    ref="${SVC_TAGS[$name]:-}"
  fi
  [[ -n "$ref" && "$ref" != "@" ]] || { echo "compose: internal error: no ref for $name" >&2; exit 1; }
  echo "compose: archiving $name -> data drive..."
  "${BUILD_BIN[@]}" push "$ref" "docker-archive:$stage/images/$name.tar:$(tag_for "$name")" >/dev/null
done < <("${JQ[@]}" -r '.services[] | [.name, (if .image then "image" else "build" end), (.image // .build.context)] | @tsv' "$plan_tmp/plan.json")

# mke2fs -d populate cannot write non-sparse files beyond 2GiB (its
# byte counter is 32-bit; verified on e2fsprogs 1.47.3). Split big
# docker archives into <2GiB parts; the guest cats them into
# `docker load`.
for tar in "$stage"/images/*.tar; do
  [[ -f "$tar" ]] || continue
  sz=$(stat -c%s "$tar")
  if (( sz > 2000000000 )); then
    mv "$tar" "$tar.full"
    split -b 1800M -d -a 2 "$tar.full" "$tar.part-"
    rm -f "$tar.full"
    echo "compose: split $(basename "$tar") into $((sz / 1800000000 + 1)) parts (>2GiB populate limit)"
  fi
done

size_kb=$(du -sk "$stage" | cut -f1)
# Docker storage needs headroom: the images tars decompress into
# docker-data on the same drive. Sparse file: host disk usage grows
# with real use.
fs_size=$(( size_kb * 4 + 4194304 ))
h=$(printf 'layout-v3\n' | cat - "$plan_tmp/manifest.json" "$stage/compose.yaml" "$DOCKER_BUNDLE/docker.tgz.sha256" "$DOCKER_BUNDLE/docker-compose.sha256" | sha256sum | cut -c1-12)
mkdir -p "$COMPOSE_CACHE"
drive="$COMPOSE_CACHE/${VMF_NAME}-$h.ext4"
if [[ ! -f "$drive" ]]; then
  echo "compose: building data drive ($(( fs_size / 1024 )) MiB sparse)..."
  vmf_run util-linux -- truncate -s $(( fs_size / 1024 ))M "$drive.tmp"
  vmf_run e2fsprogs -- mke2fs -q -F -t ext4 -b 4096 -d "$stage" "$drive.tmp"
  mv "$drive.tmp" "$drive"
else
  echo "compose: data drive cache hit: $drive"
fi

# --- hand over to the normal run path ------------------------------------
primary_tag="${SVC_TAGS[$primary]}"
[[ -n "$primary_tag" ]] || primary_tag="${SVC_IMAGES[$primary]}"
ports_args=()
while IFS=$'\t' read -r h proto bind; do
  spec="$h:$h"
  [[ "$bind" != "0.0.0.0" && -n "$bind" ]] && spec="$bind:$h:$h"
  if [[ "$proto" == "udp" ]]; then
    ports_args+=(-p "$spec/udp")
  else
    ports_args+=(-p "$spec")
  fi
done < <("${JQ[@]}" -r '.services[] | .ports[]? | [(.host|tostring), .proto, (.bind // "0.0.0.0")] | @tsv' "$plan_tmp/plan.json")
# Intent-refined instances: extra VM ports are offsets of the service's
# first declared port; qemu publishes hostfwd only at boot, so they
# must ride the boot-time -p list (firecracker's poller would cover
# them, but qemu has no dynamic hostfwd).
if [[ -f "$VMF_PLAN_REFINES" ]]; then
  while IFS=$'\t' read -r h proto reps; do
    [[ -n "$h" ]] || continue
    for (( i=1; i<reps; i++ )); do
      hp=$((h + i))
      if [[ "$proto" == "udp" ]]; then
        ports_args+=(-p "$hp:$hp/udp")
      else
        ports_args+=(-p "$hp:$hp")
      fi
    done
  done < <("${PY[@]}" "$SCRIPT_DIR/vmf_plan.py" ports "$plan_tmp/plan.json" "$VMF_PLAN_REFINES")
fi

# Hand back to the boot driver: the first oci-run pass parsed the run
# flags and exported them (VMF_RUN_*); this pass inherits that env, so
# only the ports and the primary image travel as arguments.
if [[ "${VMF_COMPOSE_NOEXEC:-0}" == "1" ]]; then
  echo "compose: noexec; primary=$primary_tag data=$drive ports=${ports_args[*]:-none}"
  exit 0
fi
exec env -u VMF_COMPOSE_SRC VMF_MODE=compose VMF_DATA_DRIVE="$drive" \
  "$SCRIPT_DIR/oci-run.sh" ${ports_args[@]+"${ports_args[@]}"} "$primary_tag"