#!/usr/bin/env bash
# Compose mode: run a docker-compose project inside ONE microVM.
#
# Pipeline:
#   1. translate the compose file: services, image|build, ports, env,
#      depends_on (python + pyyaml)
#   2. host-side builds/pulls through buildah — pinned digests only
#   3. docker-archive every service image onto a data drive (ext4)
#      together with the pinned static docker bundle and a flattened
#      compose file (image tags only: no builds, no in-VM pulls)
#   4. exec oci-run.sh with the primary service's local tag, plus
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

tag_for() { printf 'localhost/vmf-compose/%s-%s:%s' "$VMF_COMPOSE_SLUG" "$1" "$VMF_COMPOSE_RUNID"; }

if command -v buildah >/dev/null 2>&1; then
  BUILD_BIN=(buildah)
else
  BUILD_BIN=(nix shell nixpkgs#krunvm nixpkgs#buildah -c buildah)
fi
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

"${PY[@]}" - "$VMF_COMPOSE_SRC" "$plan_tmp/plan.json" <<'PYEOF'
import json, os, sys, yaml
src = sys.argv[1]
out = sys.argv[2]
for cand in ("compose.yaml", "docker-compose.yaml", "compose.yml", "docker-compose.yml"):
    p = os.path.join(src, cand)
    if os.path.isfile(p):
        break
else:
    sys.stderr.write("error: no compose file in %s\n" % src)
    sys.exit(1)
doc = yaml.safe_load(open(p))
svcs = doc.get("services") or {}
if not svcs:
    sys.stderr.write("error: compose file has no services\n")
    sys.exit(1)
plan = {"compose_file": os.path.basename(p), "services": []}
for name, s in svcs.items():
    e = {"name": name}
    if s.get("build"):
        b = s["build"]
        if isinstance(b, str):
            e["build"] = {"context": b, "dockerfile": "Dockerfile"}
        else:
            e["build"] = {"context": b.get("context") or ".",
                          "dockerfile": b.get("dockerfile") or "Dockerfile"}
        if "image" in s:
            e["tag"] = s["image"]
    elif "image" in s:
        e["image"] = s["image"]
    else:
        sys.stderr.write("error: service '%s' has neither build nor image\n" % name)
        sys.exit(1)
    ports = []
    for pv in s.get("ports") or []:
        pv = str(pv)
        parts = pv.split(":")
        if len(parts) >= 2:
            ports.append({"host": int(parts[0]), "guest": int(parts[-1])})
        else:
            sys.stderr.write("warning: port '%s' for %s is guest-only; not published\n" % (pv, name))
    e["ports"] = ports
    env = {}
    for kv in s.get("environment") or []:
        if isinstance(kv, str):
            k, _, v = kv.partition("=")
            env[k] = v
        else:
            env.update({k: ("" if v is None else str(v)) for k, v in kv.items()})
    if s.get("env_file"):
        for f in s["env_file"]:
            fp = os.path.join(src, f)
            if os.path.isfile(fp):
                for line in open(fp):
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        env[k] = v
            else:
                sys.stderr.write("warning: env_file %s not found; skipped\n" % f)
    e["env"] = env
    e["depends_on"] = list((s.get("depends_on") or {}).keys()
                           if isinstance(s.get("depends_on"), dict)
                           else s.get("depends_on") or [])
    plan["services"].append(e)
if len(plan["services"]) > 12:
    sys.stderr.write("error: more than 12 services not supported\n")
    sys.exit(1)
# primary: the service no other service depends on
deps = {d for e in plan["services"] for d in e["depends_on"]}
prim = [e for e in plan["services"] if e["name"] not in deps]
if not prim:
    sys.stderr.write("error: every service is a dependency; cannot pick a primary\n")
    sys.exit(1)
plan["primary"] = prim[0]["name"]
json.dump(plan, open(out, "w"))
PYEOF

if command -v jq >/dev/null 2>&1; then
  JQ=(jq -r)
else
  JQ=(nix shell nixpkgs#jq -c jq -r)
fi
svc_count=$("${JQ[@]}" '.services | length' "$plan_tmp/plan.json")
primary=$("${JQ[@]}" -r '.primary' "$plan_tmp/plan.json")
echo "compose: $svc_count services (primary: $primary)"

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
    digest=$("${BUILD_BIN[@]}" inspect --type image "$image" | grep -oE 'sha256:[0-9a-f]{64}' | head -1)
    pin_image "$image" "$digest"
    SVC_DIGEST["$name"]="$digest"
  else
    dockerfile="$plan_tmp/df-$name"
    ctx="$VMF_COMPOSE_SRC/$rest"
    echo "compose: building $name (context: ${rest#./})..."
    "${BUILD_BIN[@]}" build -t "$tag" -f "$ctx/$(${JQ[@]} -r --arg n "$name" '.services[] | select(.name==$n) | .build.dockerfile' "$plan_tmp/plan.json")" "$ctx" >/dev/null
    digest=$("${BUILD_BIN[@]}" inspect --type image "$tag" | grep -oE 'sha256:[0-9a-f]{64}' | head -1)
    pin_image "$tag" "$digest"
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

# --- flattened compose file (no builds, local tags only) ----------------
"${PY[@]}" - "$plan_tmp/manifest.json" "$plan_tmp/compose.yaml" <<'PYEOF'
import json, sys, yaml
m = json.load(open(sys.argv[1]))
svcs = {}
for e in m["services"]:
    n = e["name"]
    tag = m["tags"].get(n) or e.get("image")
    entry = {"image": tag}
    if e["ports"]:
        entry["ports"] = ["%d:%d" % (p["host"], p["guest"]) for p in e["ports"]]
    if e["env"]:
        entry["environment"] = e["env"]
    if e["depends_on"]:
        entry["depends_on"] = e["depends_on"]
    svcs[n] = entry
yaml.safe_dump({"services": svcs}, open(sys.argv[2], "w"), sort_keys=False)
PYEOF

# --- data drive ----------------------------------------------------------
stage="$plan_tmp/data"
mkdir -p "$stage/images" "$stage/docker/bin"
cp "$plan_tmp/compose.yaml" "$stage/compose.yaml"
cp "$DOCKER_BUNDLE"/bin/* "$stage/docker/bin/"
while IFS=$'\t' read -r name kind rest; do
  # Built services push their local tag; pulled services push by digest
  # ref (the store only has the upstream name). Either way the tar
  # carries the guest-side tag_for() name, which compose references.
  if [[ "${SVC_KINDS[$name]:-}" == "image" ]]; then
    ref="${SVC_IMAGES[$name]:-}@${SVC_DIGEST[$name]:-}"
  else
    ref="${SVC_TAGS[$name]:-}"
  fi
  [[ -n "$ref" && "$ref" != "@" ]] || { echo "compose: internal error: no ref for $name" >&2; exit 1; }
  echo "compose: archiving $name -> data drive..."
  "${BUILD_BIN[@]}" push "$ref" "docker-archive:$stage/images/$name.tar:$(tag_for "$name")" >/dev/null
done < <("${JQ[@]}" -r '.services[] | [.name, (if .image then "image" else "build" end), (.image // .build.context)] | @tsv' "$plan_tmp/plan.json")

size_kb=$(du -sk "$stage" | cut -f1)
fs_size=$(( size_kb + size_kb / 4 + 65536 ))
h=$(printf 'layout-v2\n' | cat - "$plan_tmp/manifest.json" "$stage/compose.yaml" "$DOCKER_BUNDLE/docker.tgz.sha256" "$DOCKER_BUNDLE/docker-compose.sha256" | sha256sum | cut -c1-12)
mkdir -p "$COMPOSE_CACHE"
drive="$COMPOSE_CACHE/${VMF_NAME}-$h.ext4"
if [[ ! -f "$drive" ]]; then
  echo "compose: building data drive ($(( fs_size / 1024 )) MiB)..."
  if command -v mke2fs >/dev/null 2>&1; then
    mke2fs -q -F -t ext4 -d "$stage" "$drive.tmp" $(( fs_size * 1024 / 1024 ))
  else
    nix shell nixpkgs#e2fsprogs -c mke2fs -q -F -t ext4 -d "$stage" "$drive.tmp" $(( fs_size * 1024 / 1024 ))
  fi
  mv "$drive.tmp" "$drive"
else
  echo "compose: data drive cache hit: $drive"
fi

# --- hand over to the normal run path ------------------------------------
primary_tag="${SVC_TAGS[$primary]}"
[[ -n "$primary_tag" ]] || primary_tag="${SVC_IMAGES[$primary]}"
ports_args=()
while IFS=$'\t' read -r h g; do
  ports_args+=(-p "$h:$g")
done < <("${JQ[@]}" -r '.services[] | .ports[]? | [(.host|tostring), (.guest|tostring)] | @tsv' "$plan_tmp/plan.json")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${VMF_COMPOSE_NOEXEC:-0}" == "1" ]]; then
  echo "compose: noexec; primary=$primary_tag data=$drive ports=${ports_args[*]:-none}"
  exit 0
fi
# Reconstruct the original run flags for the second oci-run pass: the
# first pass parsed them; without this the VM boots on defaults.
run_args=(--name "$VMF_NAME" --engine "${VMF_RUN_ENGINE:-qemu}")
[[ "${VMF_RUN_DETACH:-0}" == "1" ]] && run_args+=(-d)
[[ "${VMF_RUN_KEEP:-0}" == "1" ]] && run_args+=(--keep)
run_args+=(--memory "${VMF_RUN_MEM:-1024}" --cpus "${VMF_RUN_CPUS:-2}" --net "${VMF_RUN_NETMODE:-open}")
[[ "${VMF_RUN_TIMEOUT_SECS:-0}" -gt 0 ]] && run_args+=(--timeout "${VMF_RUN_TIMEOUT_SECS}s")
exec env -u VMF_COMPOSE_SRC VMF_MODE=compose VMF_DATA_DRIVE="$drive" \
  "$SCRIPT_DIR/oci-run.sh" ${run_args[@]+"${run_args[@]}"} ${ports_args[@]+"${ports_args[@]}"} "$primary_tag"
