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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export VMF_SCRIPTS_DIR="$SCRIPT_DIR"

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
import json, os, shutil, subprocess, sys, yaml
src = sys.argv[1]
out = sys.argv[2]
NAMES = ("compose.yaml", "docker-compose.yaml", "compose.yml", "docker-compose.yml")
SKIP_DIRS = {".git", "node_modules", ".github", "__pycache__", ".idea", ".vscode"}

def meta(path):
    # Cheap per-candidate metadata for the menu and the hint matcher.
    try:
        doc = yaml.safe_load(open(path)) or {}
    except Exception as exc:
        return None, ["unparseable: %s" % exc], []
    name = doc.get("name") or ""
    svcs = doc.get("services") or {}
    ports = [str(pv) for s in svcs.values() for pv in (s.get("ports") or [])]
    return str(name), list(svcs), ports

def scan(root):
    # Every compose file in the first two directory levels (one project
    # per directory is the monorepo layout).
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel != "." and rel.count(os.sep) >= 2:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if dirpath == root:
            continue
        for cand in NAMES:
            if cand in filenames:
                name, svcs, ports = meta(os.path.join(dirpath, cand))
                hits.append({"dir": dirpath, "rel": rel, "file": cand,
                             "name": name, "services": svcs, "ports": ports})
                break
    return hits

def menu(hits, root):
    sys.stderr.write("vmf: several projects under %s:\n" % root)
    for h in sorted(hits, key=lambda h: h["rel"]):
        ps = " ".join(h["ports"][:4]) if h["ports"] else "-"
        sys.stderr.write("  vmf:   %-28s %-14s %d services  ports %s\n"
                         % (h["rel"], h["name"] or "-", len(h["services"]), ps))
    sys.stderr.write("  vmf: run one explicitly:\n")
    sys.stderr.write("  vmf:   vmf run <repo>/<relpath>\n")
    sys.stderr.write("  vmf:   vmf run <repo> --project <name-or-path>\n")
    sys.stderr.write("  vmf:   vmf run <repo> --intent \"which one to run\"\n")

hint = os.environ.get("VMF_COMPOSE_PROJECT", "").strip()
intent = os.environ.get("VMF_RUN_INTENT", "").strip()

def resolve(root, hits, hint):
    # A hint matches by relative path prefix, directory name, or compose
    # name. Unambiguous only when exactly one candidate survives.
    if hint:
        matched = [h for h in hits
                   if h["rel"] == hint
                   or h["rel"].startswith(hint.rstrip("/") + "/")
                   or os.path.basename(h["rel"]) == hint
                   or (h["name"] and h["name"] == hint)]
        if len(matched) != 1:
            if not matched:
                sys.stderr.write("error: no compose project matches '%s'\n" % hint)
            else:
                sys.stderr.write("error: hint '%s' matches several projects\n" % hint)
            menu(hits, root)
            sys.exit(1)
        return matched[0]
    if len(hits) == 1:
        return hits[0]
    menu(hits, root)
    sys.exit(2)

def resolve_intent(root, hits, phrase):
    # The LLM picks a pointer from the enumerated menu. Anything outside
    # the menu is a hard error, never a boot.
    llm = os.path.join(os.environ["VMF_SCRIPTS_DIR"], "llm.sh")
    menu_json = [{"path": h["rel"], "name": h["name"],
                  "services": len(h["services"]), "ports": h["ports"][:8]}
                 for h in sorted(hits, key=lambda h: h["rel"])]
    prompt = ("Project menu (choose one path from this list only):\n%s\n\n"
              "User request: %s\n\n"
              "Reply with JSON: {\"project\": \"<path from the menu>\", "
              "\"ref\": \"<git branch or tag, or null>\", "
              "\"variant\": \"<build variant value, or null>\", "
              "\"why\": \"<max 8 words>\"}" % (json.dumps(menu_json), phrase))
    proc = subprocess.run(["bash", llm, "--role", "intent", prompt],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        # Deterministic fallback: the menu. The model is an accelerator,
        # never a dependency.
        sys.stderr.write(proc.stderr)
        menu(hits, root)
        sys.exit(2)
    try:
        sel = json.loads(proc.stdout.strip().strip("`"))
        if isinstance(sel, str):
            sel = json.loads(sel)
    except Exception:
        sys.stderr.write("error: --intent returned unparseable output; run without --intent\n")
        sys.exit(1)
    want = sel.get("project")
    for h in hits:
        if h["rel"] == want or (h["name"] and h["name"] == want):
            return h, sel
    sys.stderr.write("error: llm picked '%s' which is not a menu project; refusing\n"
                     % want)
    menu(hits, root)
    sys.exit(1)

# Root compose wins; otherwise every subdirectory compose is a candidate.
candidates = []
for cand in NAMES:
    p = os.path.join(src, cand)
    if os.path.isfile(p):
        name_, svcs_, ports_ = meta(p)
        candidates.append({"dir": src, "rel": ".", "file": cand,
                           "name": name_, "services": svcs_, "ports": ports_})
        break
if not candidates:
    candidates = scan(src)

sel = None
if candidates:
    if intent and not hint:
        resolved, sel = resolve_intent(src, candidates, intent)
    else:
        resolved = resolve(src, candidates, hint)
else:
    resolved = None

def gapfill(root):
    # Compose-less repo: collect deterministic evidence (readme,
    # Dockerfiles, manifests, systemd units, package files), ask the
    # gap-fill model for a strict-JSON plan, render compose.yaml HERE
    # (the model never writes YAML), and gate behind an approval.
    # Approved proposals cache by input hash; later runs replay.
    import hashlib
    inputs = []
    for rn in ("README.md", "README.rst", "README.txt"):
        if os.path.isfile(os.path.join(root, rn)):
            inputs.append(("readme", rn,
                           open(os.path.join(root, rn), errors="replace").read(8192)))
            break
    dfs = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel != "." and rel.count(os.sep) >= 2:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.startswith("Dockerfile") and len(dfs) < 3:
                dfs.append(os.path.join(dirpath, f))
    for p in dfs:
        rel = os.path.relpath(p, root)
        inputs.append(("dockerfile", rel, open(p, errors="replace").read(4096)))
    for f, cap in (("manifest.yaml", 4096), ("pyproject.toml", 4096),
                   ("requirements.txt", 2048), ("package.json", 4096),
                   ("go.mod", 2048), ("Cargo.toml", 2048), ("Gemfile", 2048),
                   ("Makefile", 4096)):
        p = os.path.join(root, f)
        if os.path.isfile(p):
            inputs.append(("manifest", f, open(p, errors="replace").read(4096)))
    units = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel != "." and rel.count(os.sep) >= 2:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".service") and len(units) < 3:
                units.append(os.path.join(dirpath, f))
    for p in units:
        rel = os.path.relpath(p, root)
        inputs.append(("systemd-unit", rel, open(p, errors="replace").read(4096)))
    bundle = "\n".join("=== %s: %s ===\n%s" % (k, rp, t) for k, rp, t in inputs)
    if not bundle.strip():
        sys.stderr.write("error: no compose file and nothing to infer from "
                         "(no README/Dockerfile/manifests)\n")
        sys.exit(1)
    # Prompt version: part of the cache key, so improved prompts
    # invalidate stale cached plans.
    PROMPT_V = "4"
    key = hashlib.sha256((PROMPT_V + "\n" + bundle).encode()).hexdigest()[:12]
    gen = os.path.join(os.path.expanduser("~"), ".vmf", "generated", key)
    cache = os.path.join(gen, "compose.yaml")
    runtime = os.environ.get("VMF_RUN_RUNTIME", "auto").strip().lower()
    if runtime not in ("auto", "direct", "docker"):
        runtime = "auto"
    # The cache respects the requested runtime: a docker-mode cache hit
    # must not hijack a --runtime direct run and vice versa.
    if runtime in ("auto", "docker") and os.path.isfile(cache):
        sys.stderr.write("gap-filler: cache hit %s\n" % cache)
        shutil.copy(cache, os.path.join(root, "compose.yaml"))
        return
    dcache = os.path.join(gen, "direct.json")
    if runtime in ("auto", "direct") and os.path.isfile(dcache):
        sys.stderr.write("gap-filler: cache hit %s (direct)\n" % dcache)
        shutil.copy(dcache, os.path.join(os.path.dirname(out), "direct.json"))
        sys.exit(0)
    def gate(text):
        g = ("grounded via context7: " + ", ".join(c7_ids[:3])) if c7_ids \
            else "NOT grounded (context7 unavailable)"
        sys.stderr.write(text + "  grounding: %s\n" % g)
        accept = os.environ.get("VMF_RUN_YES") == "1"
        if not accept and sys.stdin.isatty():
            sys.stderr.write("gap-filler: boot with this plan? [y/N] ")
            try:
                accept = input().strip().lower() in ("y", "yes")
            except EOFError:
                accept = False
        if not accept:
            sys.stderr.write("gap-filler: not approved; rerun with --yes to accept\n")
            sys.exit(2)
        return True
    def save(obj_text, path):
        os.makedirs(gen, exist_ok=True)
        open(path, "w").write(obj_text)
        open(path + ".meta.json", "w").write(json.dumps(
            {"model": os.environ.get("VMF_GAPFILL_MODEL") or os.environ.get("VMF_LLM_MODEL", ""),
             "notes": planj.get("notes", ""), "created": "",
             "context7": c7_ids}, indent=2))
    llm = os.path.join(os.environ["VMF_SCRIPTS_DIR"], "llm.sh")
    ctx7 = os.path.join(os.environ["VMF_SCRIPTS_DIR"], "context7.sh")
    # Phase 1: draft. The model states what it plans and which topics it
    # needs verified against CURRENT docs - its recall of package names
    # and install steps may be stale.
    draft_prompt = (
        "Draft a run plan for this repository inside a disposable microVM "
        "(the VM is the sandbox). Use ONLY the evidence below, but ALSO "
        "list up to 3 topics whose CURRENT facts matter (package names, "
        "install steps, prerequisites) so they can be verified against "
        "up-to-date docs. Reply with ONE JSON object:\n"
        '{"mode": "direct" | "docker", '
        '"lookup": ["<doc topic, e.g. <tool> install on linux>"], '
        '"why": "<max 8 words>"}\n'
        "Evidence:\n" + bundle[:32768])
    proc = subprocess.run(["bash", llm, "--role", "gapfill", draft_prompt],
                          capture_output=True, text=True)
    lookup = []
    if proc.returncode == 0:
        try:
            dj = json.loads(proc.stdout.strip().strip("`"))
            if isinstance(dj, str):
                dj = json.loads(dj)
            lookup = [str(x) for x in (dj.get("lookup") or [])[:3]]
        except Exception:
            lookup = []
    # Phase 2: grounding. Fetch current docs per lookup topic via
    # Context7; every failure degrades silently.
    grounding = []
    c7_ids = []
    for topic in lookup:
        s = subprocess.run(["bash", ctx7, "search", topic],
                           capture_output=True, text=True)
        if s.returncode != 0:
            continue
        try:
            hits = [json.loads(l) for l in s.stdout.strip().splitlines() if l.strip()]
        except Exception:
            continue
        if not hits:
            continue
        lib = hits[0]["id"]
        d = subprocess.run(["bash", ctx7, "docs", lib, topic],
                           capture_output=True, text=True)
        if d.returncode != 0 or not d.stdout.strip():
            continue
        grounding.append("=== context7: %s (%s, updated %s) ===\n%s"
                         % (lib, topic, hits[0].get("updated", "?"),
                            d.stdout[:3000]))
        c7_ids.append("%s [%s]" % (lib, topic))
    grounded = ("\nGrounding - CURRENT documentation fetched for the lookup "
                "topics; prefer these facts over your recall:\n"
                + "\n".join(grounding)) if grounding else \
               ("\nGrounding: context7 unavailable for this run; state facts "
                "conservatively and prefer the evidence below.\n")

    prompt = (
        "Decide how to run this repository inside a disposable microVM "
        "(the VM is the sandbox). Use ONLY the evidence below; never "
        "invent versions, ports, or env values.\n"
        "Reply with ONE JSON object:\n"
        '{"mode": "direct" | "docker",\n'
        ' "base_image": "<oci ref like python:3.12-slim, only for direct>",\n'
        ' "install": ["<shell commands run once at boot in the VM>"],\n'
        ' "command": ["<argv that starts the app>"],\n'
        ' "env": {"K": "V"},\n'
        ' "needs_docker": <true when the app itself shells out to docker>,\n'
        ' "services": [<docker shape, only for docker: {"name", "image" or '
        '"build": {"context", "dockerfile", "args"}, "command", "ports", '
        '"env"}>],\n'
        ' "notes": "<max 12 words>"}\n'
        "mode=direct when the evidence shows a direct install path "
        "(README install steps, pyproject.toml/package.json etc.) and no "
        "multi-service dependencies; mode=docker when the repo is "
        "container-native (compose, Dockerfile-only, multi-service). For "
        "direct, install the app from the repo source (staged at "
        "/workspace) unless the README pins an external package. The "
        "base image is minimal: include prerequisite installs in the "
        "install list (e.g. 'pip install uv' before 'uv sync'). Leave "
        "ports empty when unknown - vmf auto-publishes what the app "
        "listens on. If the app itself needs a docker daemon at runtime "
        "(sandbox builders, container-based tools), set needs_docker=true "
        "with mode=direct (vmf starts dockerd in the VM); if the app IS "
        "container-native, prefer mode=docker instead.\n"
        + grounded +
        "Evidence:\n" + bundle[:32768])
    proc = subprocess.run(["bash", os.path.join(os.environ["VMF_SCRIPTS_DIR"], "llm.sh"),
                           "--role", "gapfill", prompt],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.stderr.write("error: gap-filler needs a model (VMF_GAPFILL_MODEL); "
                         "the repo has no compose file\n")
        sys.exit(3)
    try:
        planj = json.loads(proc.stdout.strip().strip("`"))
        if isinstance(planj, str):
            planj = json.loads(planj)
    except Exception:
        sys.stderr.write("error: gap-filler returned unparseable JSON:\n%s\n"
                         % proc.stdout[:400])
        sys.exit(1)
    if runtime == "direct":
        planj["mode"] = "direct"
    elif runtime == "docker":
        planj["mode"] = "docker"
    if planj.get("mode") == "direct":
        # Direct mode: base image + boot-time install + argv. The VM is
        # the sandbox; no docker. The bash layer turns this into a plain
        # image run with a staged repo tar and install script.
        base = (planj.get("base_image") or "").strip()
        cmd = planj.get("command") or []
        if not base or not cmd:
            sys.stderr.write("error: gap-fill direct plan lacks base_image or command\n")
            sys.exit(1)
        dtext = json.dumps({"base_image": base,
                            "install": [str(x) for x in (planj.get("install") or [])][:20],
                            "command": [str(x) for x in cmd][:16],
                            "env": {str(k): str(v) for k, v in (planj.get("env") or {}).items()},
                            "needs_docker": bool(planj.get("needs_docker")),
                            "notes": planj.get("notes", "")}, indent=2)
        proposal = ("gap-filler: proposal (%s)\nmode: direct\nbase: %s\ninstall:\n%s\ncommand: %s\n"
                    % ((planj.get("notes") or "-")[:60], base,
                       "\n".join("  - %s" % i for i in json.loads(dtext)["install"]) or "  - (none)",
                       cmd))
        gate(proposal)
        save(dtext, dcache)
        shutil.copy(dcache, os.path.join(os.path.dirname(out), "direct.json"))
        sys.stderr.write("gap-filler: approved; cached %s\n" % dcache)
        sys.exit(0)
    comp = {"services": {}}
    for s in planj.get("services") or []:
        e = {}
        b = s.get("build") or {}
        if b.get("dockerfile") or b.get("context"):
            be = {"context": b.get("context") or ".",
                  "dockerfile": b.get("dockerfile") or "Dockerfile"}
            if b.get("args"):
                be["args"] = {str(k): str(v) for k, v in b["args"].items()}
            e["build"] = be
        elif s.get("image"):
            e["image"] = str(s["image"])
        else:
            continue
        if s.get("command"):
            e["command"] = s["command"]
        if s.get("ports"):
            e["ports"] = [str(p) for p in s["ports"]]
        if s.get("env"):
            e["environment"] = {str(k): str(v) for k, v in s["env"].items()}
        comp["services"][str(s["name"])] = e
    if not comp["services"]:
        sys.stderr.write("error: gap-filler proposed no usable services\n")
        sys.exit(1)
    rendered = yaml.safe_dump(comp, sort_keys=False)
    gate("gap-filler: proposal (%s)\n%s"
         % ((planj.get("notes") or "-")[:60], rendered))
    save(rendered, cache)
    shutil.copy(cache, os.path.join(root, "compose.yaml"))
    sys.stderr.write("gap-filler: approved; cached %s\n" % cache)

if not candidates:
    gapfill(sys.argv[1])
    for cand in NAMES:
        p = os.path.join(src, cand)
        if os.path.isfile(p):
            name_, svcs_, ports_ = meta(p)
            candidates.append({"dir": src, "rel": ".", "file": cand,
                               "name": name_, "services": svcs_, "ports": ports_})
            break
    resolved = candidates[0] if candidates else None
if not candidates:
    sys.stderr.write("error: no compose file in %s (root or first two dir levels)\n" % src)
    sys.exit(1)

compose_dir = resolved["dir"]
compose_path = os.path.join(compose_dir, resolved["file"])
if compose_dir != src:
    src = compose_dir
    sys.stderr.write("compose: using %s/%s\n"
                     % (os.path.relpath(compose_dir, sys.argv[1]), resolved["file"]))
if sel:
    ref = (sel.get("ref") or "").strip()
    variant = (sel.get("variant") or "").strip()
    sys.stderr.write("intent: lab=%s (%s, %d services) ref=%s variant=%s [llm: %s]\n"
                     % (resolved["rel"], resolved["name"] or "-",
                        len(resolved["services"]), ref or "default", variant or "-",
                        (sel.get("why") or "")[:40]))
    url = os.environ.get("VMF_COMPOSE_URL")
    if ref and url:
        sys.stderr.write("intent: re-cloning %s @ %s\n" % (url, ref))
        shutil.rmtree(src, ignore_errors=True)
        rc = subprocess.run(["git", "clone", "--depth", "1", "--branch", ref,
                             url, sys.argv[1]]).returncode
        if rc != 0 or not os.path.isfile(compose_path):
            sys.stderr.write("error: ref %s has no compose at %s\n" % (ref, resolved["rel"]))
            sys.exit(1)
    if variant:
        os.environ["VMF_VARIANT_OVERRIDE"] = variant

doc = yaml.safe_load(open(compose_path))
svcs = doc.get("services") or {}

def norm_image(img):
    # Docker-style normalization: unprefixed names imply docker.io
    # ("mariadb:11.8" -> docker.io/library/mariadb:11.8, "user/repo" ->
    # docker.io/user/repo). A registry prefix only counts before the
    # first slash; localhost/local buildah tags pass through.
    if not img:
        return img
    base = img.split("@", 1)[0]
    first, slash, rest = base.partition("/")
    if slash and (first == "localhost" or "." in first or ":" in first):
        return img
    if slash:
        return "docker.io/" + img
    return "docker.io/library/" + img

if not svcs:
    sys.stderr.write("error: compose file has no services\n")
    sys.exit(1)
plan = {"compose_file": os.path.basename(compose_path), "services": [],
        "project_dir": os.path.relpath(src, sys.argv[1])}
for name, s in svcs.items():
    e = {"name": name}
    if s.get("command"):
        e["command"] = s["command"]
    if s.get("entrypoint"):
        e["entrypoint"] = s["entrypoint"]
    if s.get("build"):
        b = s["build"]
        if isinstance(b, str):
            e["build"] = {"context": b, "dockerfile": "Dockerfile"}
        else:
            e["build"] = {"context": b.get("context") or ".",
                          "dockerfile": b.get("dockerfile") or "Dockerfile"}
            args = b.get("args") or {}
            if isinstance(args, dict) and args:
                e["build"]["args"] = {str(k): ("" if v is None else str(v))
                                      for k, v in args.items()}
        if "image" in s:
            e["tag"] = norm_image(str(s["image"]))
    elif "image" in s:
        e["image"] = norm_image(str(s["image"]))
    else:
        sys.stderr.write("error: service '%s' has neither build nor image\n" % name)
        sys.exit(1)
    ports = []
    for pv in s.get("ports") or []:
        pv = str(pv)
        # Proto suffix ("53:53/udp"); tcp is the default.
        proto = "tcp"
        if "/" in pv:
            pv, suffix = pv.rsplit("/", 1)
            if suffix == "udp":
                proto = "udp"
            elif suffix != "tcp":
                sys.stderr.write("warning: port '%s' for %s: unknown proto; skipped\n" % (pv, name))
                continue
        parts = pv.split(":")
        if len(parts) >= 2 and parts[0].isdigit() and parts[-1].isdigit():
            # compose "HOST:CONTAINER": the VM side listens on HOST and a
            # socat forward bridges HOST -> the container port; the
            # slirp hostfwd maps host:HOST -> vm:HOST.
            ports.append({"host": int(parts[0]),
                          "cport": int(parts[-1]), "proto": proto})
        else:
            sys.stderr.write("warning: port '%s' for %s is guest-only or an unsupported form; not published\n" % (pv, name))
    e["ports"] = ports
    # Guest-only listeners (no host mapping): kept so the guest expose
    # daemon can auto-forward them from Config.ExposedPorts.
    exp = []
    for pv in s.get("expose") or []:
        pv = str(pv)
        if "/" not in pv:
            pv = pv + "/tcp"
        exp.append(pv)
    if exp:
        e["expose"] = exp
    env_raw = s.get("environment")
    env = {}
    if isinstance(env_raw, dict):
        env = {str(k): ("" if v is None else str(v)) for k, v in env_raw.items()}
    elif isinstance(env_raw, list):
        for kv in env_raw:
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
    # Network aliases declared on the service's networks: kept so the
    # flattened compose can point every other service's extra_hosts at
    # them (the guest has no docker embedded DNS; see the flatten step).
    aliases = []
    nets = s.get("networks")
    if isinstance(nets, dict):
        for nv in nets.values():
            if isinstance(nv, dict):
                aliases += list(nv.get("aliases") or [])
    elif isinstance(nets, list):
        aliases += [str(a) for a in nets]
    if aliases:
        e["aliases"] = aliases
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

# Intent variant override: a build arg whose key matches "variant"
# (case-insensitive) gets the requested value in every built service.
if [[ -n "${VMF_VARIANT_OVERRIDE:-}" ]]; then
  VMF_VARIANT_OVERRIDE="$VMF_VARIANT_OVERRIDE" "${PY[@]}" - "$plan_tmp/plan.json" <<'PYV'
import json, os, sys
p = json.load(open(sys.argv[1]))
v = os.environ["VMF_VARIANT_OVERRIDE"]
n = 0
for e in p["services"]:
    a = (e.get("build") or {}).get("args")
    if a:
        for k in list(a):
            if k.lower() == "variant":
                a[k] = v
                n += 1
json.dump(p, open(sys.argv[1], "w"))
if n:
    print("intent: VARIANT=%s applied to %d service(s)" % (v, n))
PYV
fi

if command -v jq >/dev/null 2>&1; then
  JQ=(jq -r)
else
  JQ=(nix shell nixpkgs#jq -c jq -r)
fi
if command -v awk >/dev/null 2>&1; then
  AWK=(awk)
else
  AWK=(nix shell nixpkgs#gawk -c awk)
fi
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
  echo "gap-fill direct: base=$base command=${gcmd[*]} needs_docker=$gneed_docker"
  exec env -u VMF_MODE -u VMF_COMPOSE_SRC \
    VMF_REPO_DIR="$VMF_COMPOSE_SRC" VMF_INSTALL_CMD="$ginst" \
    ${gneed_docker:+VMF_WANT_DOCKER=1} \
    "$SCRIPT_DIR/oci-run.sh" \
    ${genv_args[@]+"${genv_args[@]}"} ${run_args[@]+"${run_args[@]}"} \
    "$base" ${gcmd[@]+"${gcmd[@]}"}
fi

svc_count=$("${JQ[@]}" '.services | length' "$plan_tmp/plan.json")
primary=$("${JQ[@]}" -r '.primary' "$plan_tmp/plan.json")
PROJ="$VMF_COMPOSE_SRC/$("${JQ[@]}" -r '.project_dir // "."' "$plan_tmp/plan.json")"
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
    build_args=()
    while IFS=$'\t' read -r k v; do
      [[ -n "$k" ]] && build_args+=(--build-arg "$k=$v")
    done < <("${JQ[@]}" -r --arg n "$name" \
      '.services[] | select(.name==$n) | ((.build.args // {}) | to_entries[]) | [(.key|tostring), (.value|tostring)] | @tsv' \
      "$plan_tmp/plan.json")
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

# --- flattened compose file (no builds, local tags only) ----------------
"${PY[@]}" - "$plan_tmp/manifest.json" "$plan_tmp/compose.yaml" "$plan_tmp/ports.txt" <<'PYEOF'
import json, sys, yaml
m = json.load(open(sys.argv[1]))
svcs = {}
fwd = []
# The guest dockerd's embedded DNS (127.0.0.11) does not work here (its
# resolver DNAT needs iptables; the microVM kernel has no netfilter
# modules and dockerd runs --iptables=false). Give every service a
# static IP on the project network and point every other service's
# extra_hosts at the names + aliases. Deterministic, no resolver.
SUB = "172.31.100"
names = [e["name"] for e in m["services"]]
ip = {n: "%s.%d" % (SUB, 10 + i) for i, n in enumerate(names)}
alias_ip = {}
for e in m["services"]:
    for a in e.get("aliases") or []:
        alias_ip[a] = ip[e["name"]]
for e in m["services"]:
    n = e["name"]
    tag = m["tags"].get(n) or e.get("image")
    entry = {"image": tag}
    if e.get("command"):
        entry["command"] = e["command"]
    if e.get("entrypoint"):
        entry["entrypoint"] = e["entrypoint"]
    if e.get("expose"):
        entry["expose"] = e["expose"]
    # ports are NOT published by docker: the guest expose daemon runs
    # socat forwards from ports.txt, exposing them on the VM address.
    for p in e["ports"]:
        fwd.append("%s %d %d %s" % (p["proto"], p["host"], p["cport"], n))
    if e["env"]:
        entry["environment"] = e["env"]
    if e["depends_on"]:
        entry["depends_on"] = e["depends_on"]
    entry["networks"] = {"default": {"ipv4_address": ip[n]}}
    hosts = {o: ip[o] for o in names if o != n}
    for a, t in alias_ip.items():
        if t != ip[n]:
            hosts[a] = t
    entry["extra_hosts"] = ["%s=%s" % (k, v) for k, v in sorted(hosts.items())]
    svcs[n] = entry
doc = {"services": svcs}
doc["networks"] = {"default": {"ipam": {"config": [{"subnet": SUB + ".0/24"}]}}}
yaml.safe_dump(doc, open(sys.argv[2], "w"), sort_keys=False)
open(sys.argv[3], "w").write("\n".join(fwd) + ("\n" if fwd else ""))
PYEOF
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
  if command -v truncate >/dev/null 2>&1; then
    truncate -s $(( fs_size / 1024 ))M "$drive.tmp"
  else
    nix shell nixpkgs#util-linux -c truncate -s $(( fs_size / 1024 ))M "$drive.tmp"
  fi
  if command -v mke2fs >/dev/null 2>&1; then
    mke2fs -q -F -t ext4 -b 4096 -d "$stage" "$drive.tmp"
  else
    nix shell nixpkgs#e2fsprogs -c mke2fs -q -F -t ext4 -b 4096 -d "$stage" "$drive.tmp"
  fi
  mv "$drive.tmp" "$drive"
else
  echo "compose: data drive cache hit: $drive"
fi

# --- hand over to the normal run path ------------------------------------
primary_tag="${SVC_TAGS[$primary]}"
[[ -n "$primary_tag" ]] || primary_tag="${SVC_IMAGES[$primary]}"
ports_args=()
while IFS=$'\t' read -r h proto; do
  if [[ "$proto" == "udp" ]]; then
    ports_args+=(-p "$h:$h/udp")
  else
    ports_args+=(-p "$h:$h")
  fi
done < <("${JQ[@]}" -r '.services[] | .ports[]? | [(.host|tostring), .proto] | @tsv' "$plan_tmp/plan.json")

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
[[ -n "${VMF_RUN_EXPOSE:-}" ]] && run_args+=(--expose "$VMF_RUN_EXPOSE")
exec env -u VMF_COMPOSE_SRC VMF_MODE=compose VMF_DATA_DRIVE="$drive" \
  "$SCRIPT_DIR/oci-run.sh" ${run_args[@]+"${run_args[@]}"} ${ports_args[@]+"${ports_args[@]}"} "$primary_tag"
