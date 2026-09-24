#!/usr/bin/env python3
# vmf_plan.py — the vmf plan pipeline as a real module.
#
# Subcommands (the stages of a compose-mode run; the bash driver
# compose-run.sh invokes one per stage):
#
#   plan <src> <out>            scan/resolve/gap-fill/translate → plan.json
#                               (may also write direct.json beside out;
#                                exit 0 means continue with the plan,
#                                or stop when direct.json was written)
#   variant <plan.json> [v]     apply a build-arg "variant" override
#   refine <plan.json> <out> [phrase]
#                               --intent → refines.json (bounded overlay)
#   flatten <manifest.json> <compose.yaml> <ports.txt> [refines.json]
#                               plan + refines → flattened compose + ports
#   ports <plan.json> [refines.json]
#                               refined extra VM ports as "host<TAB>proto<TAB>reps" TSV
#   enumerate <src> <out.json> [--grounding]
#                               repo read set + one grounded call → the
#                               install-approach list (cheapest first)
#
# Contracts (schemas/plan.schema.json, schemas/refines.schema.json):
#   plan.json    {compose_file, project_dir, primary, services: [...]}
#   refines.json {replicas: {service: 2..12}}
#   direct.json  {base_image, install[], command[], env, needs_docker}
#   ports.txt    lines "proto host cport instance"
#
# Exit codes: 0 ok · 1 bad plan/evidence · 2 ambiguity/menu/not approved
#             · 3 model unconfigured (gap-fill needs an LLM)
#
# The model is an accelerator, never a dependency: menu resolution
# falls back to the enumerated menu, gap-fill failure is an honest
# exit 3, and refine ignores anything outside the bounded overlay.
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET

import vmf_llm

NAMES = ("compose.yaml", "docker-compose.yaml", "compose.yml", "docker-compose.yml")
SKIP_DIRS = {".git", "node_modules", ".github", "__pycache__", ".idea", ".vscode"}
# Prompt version: part of the cache keys, so improved prompts
# invalidate stale cached plans. v7 adds the memory_mb field to
# direct plans (an under-sized VM OOM-kills the app: dockerd,
# containerd and the app share one 1024 MB VM otherwise).
PROMPT_V = "7"


def _gapfill_bundle(root):
    # Deterministic, content-only evidence for the gap-fill prompt
    # (readme, Dockerfiles, manifests, systemd units) — the same bytes
    # the gap-fill cache key hashes. No LLM, no git, no compose read.
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
    return "\n".join("=== %s: %s ===\n%s" % (k, rp, t) for k, rp, t in inputs)


def winner_key(root):
    # The winner-cache key: the gap-fill bundle plus the root compose
    # files (the bundle never reads compose; a compose edit is
    # plan-relevant). Content-only — a HEAD move that changes nothing
    # plan-relevant keeps the key, so a solved fast-moving repo replays.
    bundle = _gapfill_bundle(root)
    parts = [bundle]
    for cand in sorted(NAMES):
        p = os.path.join(root, cand)
        if os.path.isfile(p):
            try:
                parts.append("=== compose: %s ===\n%s"
                             % (cand, open(p, errors="replace").read(8192)))
            except OSError:
                pass
    return hashlib.sha256(
        ("winner-v1\n%s\n" % PROMPT_V + "\n".join(parts)).encode()
    ).hexdigest()[:12]


def parse_llm_json(raw):
    return vmf_llm.parse_llm_json(raw)


def meta(path):
    # Cheap per-candidate metadata for the menu and the hint matcher.
    import yaml
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
    menu_json = [{"path": h["rel"], "name": h["name"],
                  "services": len(h["services"]), "ports": h["ports"][:8]}
                 for h in sorted(hits, key=lambda h: h["rel"])]
    prompt = ("Project menu (choose one path from this list only):\n%s\n\n"
              "User request: %s\n\n"
              "Reply with JSON: {\"project\": \"<path from the menu>\", "
              "\"ref\": \"<git branch or tag, or null>\", "
              "\"variant\": \"<build variant value, or null>\", "
              "\"why\": \"<max 8 words>\"}" % (json.dumps(menu_json), phrase))
    rc, out, err = vmf_llm.llm_call("intent", prompt)
    if rc != 0:
        # Deterministic fallback: the menu. The model is an accelerator,
        # never a dependency.
        sys.stderr.write(err)
        menu(hits, root)
        sys.exit(2)
    try:
        sel = vmf_llm.parse_llm_json(out)
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


def gapfill(root, plan_out):
    # Compose-less repo: collect deterministic evidence (readme,
    # Dockerfiles, manifests, systemd units), ask the gap-fill model for
    # a strict-JSON plan, render compose.yaml HERE (the model never
    # writes YAML), and gate behind an approval. Approved proposals
    # cache by input hash; later runs replay.
    bundle = _gapfill_bundle(root)
    if not bundle.strip():
        sys.stderr.write("error: no compose file and nothing to infer from "
                         "(no README/Dockerfile/manifests)\n")
        sys.exit(1)
    key = hashlib.sha256((PROMPT_V + "\n" + bundle).encode()).hexdigest()[:12]
    gen = os.path.join(os.path.expanduser("~"), ".vmf", "generated", key)
    cache = os.path.join(gen, "compose.yaml")
    runtime = os.environ.get("VMF_RUN_RUNTIME", "auto").strip().lower()
    if runtime not in ("auto", "direct", "docker"):
        runtime = "auto"
    # The race's source_build candidate forces the direct path: a
    # compose-mode plan here would race the compose candidate against
    # itself (and the gap-fill model must not invent one).
    if runtime == "auto" and os.environ.get("VMF_PLAN_SKIP_COMPOSE") == "1":
        runtime = "direct"
    # The cache respects the requested runtime: a docker-mode cache hit
    # must not hijack a --runtime direct run and vice versa.
    if runtime in ("auto", "docker") and os.path.isfile(cache):
        sys.stderr.write("gap-filler: cache hit %s\n" % cache)
        shutil.copy(cache, os.path.join(root, "compose.yaml"))
        return
    dcache = os.path.join(gen, "direct.json")
    # Sidecar for the verify-revision loop: the gap-fill cache key is
    # content-derived (repo evidence hash), so compose-run carries the
    # cache dir to oci-run instead of recomputing it.
    sidecar = os.path.join(os.path.dirname(plan_out), "direct.json.cache")
    open(sidecar, "w").write(gen + "\n")
    if runtime in ("auto", "direct") and os.path.isfile(dcache):
        sys.stderr.write("gap-filler: cache hit %s (direct)\n" % dcache)
        shutil.copy(dcache, os.path.join(os.path.dirname(plan_out), "direct.json"))
        sys.exit(0)

    def gate(text):
        sys.stderr.write(text + "  grounding: %s\n"
                         % vmf_llm.grounding_note(c7_ids))

    def save(obj_text, path):
        os.makedirs(gen, exist_ok=True)
        open(path, "w").write(obj_text)
        open(path + ".meta.json", "w").write(json.dumps(
            {"model": os.environ.get("VMF_GAPFILL_MODEL") or os.environ.get("VMF_LLM_MODEL", ""),
             "notes": planj.get("notes", ""), "created": "",
             "context7": c7_ids, "refined": turns > 0, "turns": turns}, indent=2))
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
    rc, out, err = vmf_llm.llm_call("gapfill", draft_prompt, timeout=480)
    lookup = []
    if rc == 0:
        try:
            dj = vmf_llm.parse_llm_json(out)
            lookup = [str(x) for x in (dj.get("lookup") or [])[:3]]
        except Exception:
            lookup = []
    # Phase 2: grounding. Fetch current docs per lookup topic via
    # Context7; every failure degrades silently.
    grounded, c7_ids = vmf_llm.ground(lookup)

    prompt = (
        "Decide how to run this repository inside a disposable microVM "
        "(the VM is the sandbox). Use ONLY the evidence below; never "
        "invent versions, ports, or env values.\n"
        "Reply with ONE JSON object:\n"
        '{"mode": "direct" | "docker",\n'
        ' "base_image": "<oci ref like python:3.12-slim, only for direct>",\n'
        ' "install": ["<shell commands run once at boot in the VM>"],\n'
        ' "command": ["<argv that starts the app>"],\n'
        ' "ports": [<guest tcp ports the app listens on, e.g. 8080>],\n'
        ' "checks": [{"probe": {"port": 8080, "path": "/", '
        '"expect_status": 200, "expect_contains": "<optional text>"}}],\n'
        ' "env": {"K": "V"},\n'
        ' "needs_docker": <true when the app itself shells out to docker>,\n'
        ' "memory_mb": <int vm ram, 1024-8192>,\n'
        ' "services": [<docker shape, only for docker: {"name", "image" or '
        '"build": {"context", "dockerfile", "args"}, "command", "ports", '
        '"env"}>],\n'
        ' "notes": "<max 12 words>"}\n'
        "mode=direct when the evidence shows a direct install path "
        "(README install steps, pyproject.toml/package.json etc.) and no "
        "multi-service dependencies; mode=docker when the repo is "
        "container-native (compose, Dockerfile-only, multi-service). For "
        "direct, install the app from the repo source (staged at "
        "/workspace) unless the README pins an external package. "
        + base_image_note() + " List "
        "every guest TCP port the app will listen on (from the README "
        "evidence or the app's defaults); the host publishes each one "
        "1:1. If the app itself needs a docker daemon at runtime "
        "(sandbox builders, container-based tools), set needs_docker=true "
        "with mode=direct (vmf starts dockerd in the VM); if the app IS "
        "container-native, prefer mode=docker instead. Declare success "
        "checks for direct mode: one probe per HTTP-serving port whose "
        "status (and, when a specific content proves it, body text) says "
        "the app works; tcp checks for declared ports are added "
        "automatically. At most 4 checks. Size the VM: modern CLIs and "
        "TUIs often need more than the 1024 MB default, and "
        "needs_docker=true needs at least 2048 (dockerd, containerd and "
        "the app share the VM).\n"
        + grounded +
        "Evidence:\n" + bundle[:32768])

    def finalize(feedback=None):
        p = prompt
        if feedback:
            p += ("\nThe user reviewed the previous proposal and says: "
                  "\"%s\"\nRevise the plan accordingly." % feedback)
        rc, o, err = vmf_llm.llm_call("gapfill", p, timeout=480)
        if rc != 0:
            sys.stderr.write(err or "")
            sys.stderr.write("error: gap-filler call failed (rc=%s); "
                             "no compose file in the repo to plan from\n"
                             % rc)
            sys.exit(3)
        try:
            pj = vmf_llm.parse_llm_json(o)
        except Exception:
            sys.stderr.write("error: gap-filler returned unparseable JSON:\n%s\n"
                             % o[:400])
            sys.exit(1)
        if runtime == "direct":
            pj["mode"] = "direct"
        elif runtime == "docker":
            pj["mode"] = "docker"
        return pj

    def render(pj):
        # → (proposal text, payload for diff, save text, save path)
        if pj.get("mode") == "direct":
            # Direct mode: base image + boot-time install + argv. The VM is
            # the sandbox; no docker. The bash layer turns this into a plain
            # image run with a staged repo tar and install script.
            base = (pj.get("base_image") or "").strip()
            cmd = pj.get("command") or []
            if not base or not cmd:
                sys.stderr.write("error: gap-fill direct plan lacks base_image or command\n")
                sys.exit(1)
            clamped_ports = _clamp_ports(pj.get("ports"))
            clamped_checks = _clamp_checks(pj.get("checks")) \
                or _synth_checks(clamped_ports, cmd)
            dtext = json.dumps({"base_image": base,
                                "install": [str(x) for x in (pj.get("install") or [])][:20],
                                "command": [str(x) for x in cmd][:16],
                                "ports": clamped_ports,
                                "checks": clamped_checks,
                                "images": _clamp_images(pj.get("images")),
                                "env": {str(k): str(v) for k, v in (pj.get("env") or {}).items()},
                                "needs_docker": bool(pj.get("needs_docker")),
                                "memory_mb": _clamp_memory(pj.get("memory_mb"),
                                                           pj.get("needs_docker")),
                                "notes": pj.get("notes", "")}, indent=2)
            proposal = ("gap-filler: proposal (%s)\nmode: direct\nbase: %s\ninstall:\n%s\ncommand: %s\n"
                        % ((pj.get("notes") or "-")[:60], base,
                           "\n".join("  - %s" % i for i in json.loads(dtext)["install"]) or "  - (none)",
                           cmd))
            return proposal, json.loads(dtext), dtext, dcache
        comp = {"services": {}}
        for s in pj.get("services") or []:
            e = {}
            b = s.get("build") or {}
            if b.get("dockerfile") or b.get("context"):
                ctx = b.get("context") or "."
                # The context must name a real directory (relative to the
                # repo root or absolute); invented paths ("/workspace")
                # collapse to the root — the whole repo is the context.
                cand = ctx if os.path.isabs(ctx) \
                    else os.path.join(root, ctx)
                if not os.path.isdir(cand):
                    sys.stderr.write("gap-filler: build context '%s' does "
                                     "not exist; using the repo root\n" % ctx)
                    ctx = "."
                be = {"context": ctx,
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
        rendered = yaml_safe_dump(comp, None, sort_keys=False)
        proposal = "gap-filler: proposal (%s)\n%s" \
            % ((pj.get("notes") or "-")[:60], rendered)
        return proposal, comp, rendered, cache

    # The gate is a loop, not a binary: a = boot this plan, n = abort,
    # free text = refine (the model revises; the diff shows the change).
    # Capped at 3 turns; --yes skips the loop entirely.
    planj = finalize()
    prev = None
    turns = 0
    while True:
        text, payload, save_text, save_path = render(planj)
        note = _diff_note(prev, payload)
        gate(text + ("\n%s" % note if note else ""))
        if vmf_llm.accepted():
            break
        line = vmf_llm.tty_line(
            "gap-filler: refine? [a = boot this plan, n = abort, or type a change] ")
        if line is None or line == "" or line.lower() in ("n", "no"):
            sys.stderr.write("gap-filler: not approved; rerun with --yes to accept\n")
            sys.exit(2)
        if line.lower() in ("a", "y", "yes", "proceed"):
            break
        if turns >= 3:
            sys.stderr.write("gap-filler: refinement cap reached; "
                             "booting the last proposal\n")
            break
        prev = payload
        turns += 1
        planj = finalize(feedback=line)
    save(save_text, save_path)
    if save_path == dcache:
        shutil.copy(dcache, os.path.join(os.path.dirname(plan_out), "direct.json"))
        sys.stderr.write("gap-filler: approved; cached %s\n" % dcache)
        sys.exit(0)
    shutil.copy(cache, os.path.join(root, "compose.yaml"))
    sys.stderr.write("gap-filler: approved; cached %s\n" % cache)


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


_DB_SIBLING_PREFIXES = ("mongo", "postgres", "postgis", "mysql",
                        "mariadb", "redis")


def _db_sibling(svcs, self_name):
    # Trigger detection only: a declared db sibling makes the missing
    # env_file synthesizable. The VALUES come from the grounded call.
    for n, svc in svcs.items():
        if n == self_name:
            continue
        img = str(svc.get("image") or "")
        base = img.rsplit(":", 1)[0].rsplit("/", 1)[-1].lower()
        if base.startswith(_DB_SIBLING_PREFIXES):
            return n, svc
    return None, None


def _svc_env(svc):
    # The sibling's declared environment as a plain dict (creds the
    # prompt shows and the URI may carry).
    eraw = svc.get("environment") or {}
    if isinstance(eraw, dict):
        return {str(k): ("" if v is None else str(v)) for k, v in eraw.items()}
    out = {}
    if isinstance(eraw, list):
        for kv in eraw:
            if isinstance(kv, str):
                k, _, v = kv.partition("=")
                out[k] = v
            else:
                out.update({str(k): ("" if v is None else str(v))
                            for k, v in kv.items()})
    return out


_DB_URI_SCHEMES = ("mongodb", "postgres", "postgresql", "mysql", "redis")


def _clamp_synth_value(v, sib, sibport, sibenv):
    # The compose topology is authoritative for host, port, and
    # credentials — but only for db connection schemes. App URLs
    # (http://...) pass through untouched. srv URIs pass through too:
    # srv cannot carry a port.
    from urllib.parse import quote
    v = "".join(c for c in v if ord(c) >= 32 and ord(c) != 127).strip()
    if len(v) > 300:
        return ""
    scheme = v.split("://", 1)[0].lower()
    if not scheme.startswith(_DB_URI_SCHEMES) or "+srv" in scheme:
        return v[:300]
    m = re.match(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.]*://)"
                 r"(?:(?P<userinfo>[^@/]*)@)?"
                 r"(?P<host>[^/:?]+)(?P<port>:\d+)?"
                 r"(?P<rest>[/?].*)?$", v)
    if not m:
        return v[:300]
    d = m.groupdict()
    host = d["host"]
    if host != sib:
        d["host"] = sib
        d["port"] = ":%d" % sibport if d["port"] or host else ""
    # Declared credentials win: when the sibling names a user and a
    # password pair, they REPLACE the model's userinfo or supply one it
    # omitted (README placeholders and atlas templates are the drift).
    userinfo = ""
    if sibenv:
        ukey = next((k for k in sibenv
                     if re.search(r"USER|USERNAME|LOGIN", k, re.I)
                     and sibenv[k]), None)
        pkey = next((k for k in sibenv
                     if re.search(r"PASSWORD|PASS\b|SECRET", k, re.I)
                     and sibenv[k]), None)
        if ukey and pkey:
            userinfo = "%s:%s" % (quote(sibenv[ukey], safe=""),
                                  quote(sibenv[pkey], safe=""))
    if userinfo:
        userinfo += "@"
    out = "%s%s%s%s%s" % (d["scheme"], userinfo,
                          d["host"], d["port"], d["rest"] or "")
    # Official-image contract: MONGO_INITDB_ROOT_USERNAME creates the
    # root user in the admin database, so a mongo URI carrying those
    # credentials must target authSource=admin when it says nothing.
    if (userinfo and d["scheme"].startswith("mongodb://")
            and "MONGO_INITDB_ROOT_USERNAME" in sibenv
            and "authSource" not in out and "?" not in out):
        out += "?authSource=admin"
    return out


def _synth_env_for(compose_path, svcs, self_name, have_env):
    # The trigger is deterministic (env_file declared and missing, no
    # .env.example, a db sibling exists). The CONTENT is repo knowledge:
    # one grounded call reads the README and the compose and proposes the
    # documented env values. The clamp pins keys and rewrites hosts and
    # ports to the topology. No model → empty result; the satisfiability
    # check then reports the gap honestly.
    sib, sibsvc = _db_sibling(svcs, self_name)
    if not sib:
        return {}, {}
    sibport = _declared_port(sibsvc) or 0
    sibenv = _svc_env(sibsvc)
    readme = ""
    for cand in ("README.md", "README.rst", "readme.md", "README"):
        p = os.path.join(os.path.dirname(compose_path), cand)
        if os.path.isfile(p):
            with open(p, errors="replace") as f:
                readme = f.read(8192)
            break
    with open(compose_path, errors="replace") as f:
        compose_text = f.read(8192)
    prompt = (
        "The docker-compose file declares env_file: .env but the repo "
        "ships none (no .env.example either). Produce the env values the "
        "app needs.\n"
        "Sibling service '%s' is reachable in the compose network at host "
        "'%s' port %d with declared environment %s.\n"
        'Reply ONE JSON object: {"env": {"<KEY>": "<value>"}}\n'
        "Rules: at most 6; use the EXACT key names the README's env "
        "block documents; connection strings use host '%s' and port %d, "
        "carry the sibling's declared credentials, and follow the shape "
        "the README documents; database name may be the app name '%s'.\n\n"
        "===== docker-compose =====\n%s\n===== README =====\n%s\n"
        % (sib, sib, sibport, json.dumps(sibenv, sort_keys=True),
           sib, sibport, self_name, compose_text, readme))
    role = os.environ.get("VMF_SYNTH_ROLE", "gapfill")
    # One retry: an empty or unparseable reply is usually a transient
    # model hiccup, and a silent synth gap costs a whole failed build.
    dj = None
    for _attempt in (1, 2):
        rc, o, e = vmf_llm.llm_call(role, prompt, timeout=90)
        if rc == 0:
            try:
                dj = vmf_llm.parse_llm_json(o)
                if dj.get("env"):
                    break
            except Exception:
                dj = None
    if not dj:
        return {}, {}
    out = {}
    meta = {}
    # NODE_ENV never comes from env recovery: it flips npm install (dev
    # dependencies) and next build behavior — runtime build policy is
    # the compose/Dockerfile's business, not the synth's.
    deny = {"NODE_ENV"}
    for k, v in (dj.get("env") or {}).items():
        k = str(k).upper()
        if not re.match(r"^[A-Z_][A-Z0-9_]{0,63}$", k) or k in have_env:
            continue
        if k in deny:
            continue
        v = _clamp_synth_value(str(v), sib, sibport, sibenv)
        if not v or len(out) >= 6:
            continue
        out[k] = v
        meta[k] = {"service": sib, "host": sib, "port": sibport}
    return out, meta


def _declared_port(svc):
    # First declared host port from the raw compose service ports.
    for pv in svc.get("ports") or []:
        pv = str(pv)
        h = pv.split(":")[0].split("/")[0]
        if h.isdigit():
            return int(h)
    return None


def translate(compose_path, src, root):
    # compose file → plan dict. `src` is the project dir (env_file and
    # build contexts resolve there); `root` is the clone root.
    import yaml
    doc = yaml.safe_load(open(compose_path))
    svcs = doc.get("services") or {}
    if not svcs:
        sys.stderr.write("error: compose file has no services\n")
        sys.exit(1)
    plan = {"compose_file": os.path.basename(compose_path), "services": [],
            "project_dir": os.path.relpath(src, root), "checks": []}
    dropped = set()
    for name, s in svcs.items():
        prof = s.get("profiles") or []
        if prof:
            # Profile-gated services are opt-in by compose semantics; the
            # default stack never starts them. Booting one anyway wastes
            # an image load and can break the stack on its extras.
            sys.stderr.write(
                "note: service '%s' is profile-gated (%s); skipped\n"
                % (name, ",".join(str(p) for p in prof)))
            dropped.add(name)
            continue
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
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                # compose "HOST:CONTAINER": the VM side listens on HOST and a
                # socat forward bridges HOST -> the container port; the
                # slirp hostfwd maps host:HOST -> vm:HOST.
                ports.append({"host": int(parts[0]),
                              "cport": int(parts[1]), "proto": proto})
            elif (len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit()
                  and (parts[0] == "" or _is_ipv4(parts[0]))):
                # compose "[ADDR:]HOST:CONTAINER": an empty or 0.0.0.0
                # address binds all host interfaces, an IPv4 literal binds
                # that one ("127.0.0.1:4280:80" -> localhost only).
                bind = parts[0] or "0.0.0.0"
                ports.append({"host": int(parts[1]),
                              "cport": int(parts[2]), "proto": proto,
                              "bind": bind})
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
        # Relative bind mounts: the guest has no clone on disk. The plan
        # records them clone-relative; the stage copies the sources into
        # /data/repo and the flatten mounts from there. Named volumes
        # (bare names) and absolute host paths pass through untouched.
        binds = []
        for v in s.get("volumes") or []:
            if isinstance(v, dict):
                hp = str(v.get("source") or "")
                cp = str(v.get("target") or "")
                mode = "ro" if v.get("read_only") else ""
            else:
                parts = str(v).split(":")
                if len(parts) < 2:
                    continue
                hp, cp = parts[0], parts[1]
                mode = parts[2] if len(parts) > 2 else ""
            if hp.startswith("./") or hp.startswith("../"):
                rel = os.path.relpath(
                    os.path.normpath(os.path.join(src, hp)), root)
            elif hp and not hp.startswith(("/", "$")) and "/" in hp:
                # A bare relative path ("docker/stripe/x.sh") with a
                # slash is a host path, not a named volume.
                rel = os.path.relpath(
                    os.path.normpath(os.path.join(src, hp)), root)
            else:
                continue
            if any(b["host"] == rel for b in binds):
                continue
            binds.append({"host": rel, "container": cp, "mode": mode})
        if binds:
            e["binds"] = binds
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
        # Build-time visibility for the synthesized vars: the app may read
        # them during the image build (Next.js page-data collection does).
        env_file_missing = bool(s.get("env_file")) and not os.path.isfile(
            os.path.join(src, ".env.example"))
        synth_env, synth_from = {}, {}
        if env_file_missing:
            synth_env, synth_from = _synth_env_for(compose_path, svcs, name, env)
        if synth_env:
            env.update(synth_env)
            e["synth_env"] = synth_env
            if synth_from:
                e["synth_from"] = synth_from
            sys.stderr.write("note: %s: synthesized %s from the repo docs\n"
                             % (name, ", ".join(sorted(synth_env))))
        elif env_file_missing:
            # Honest gap: the satisfiability check reports this instead of
            # letting the build or runtime fail with an obscure error.
            e["env_unresolved"] = True
            sys.stderr.write("warning: %s: env unresolvable (env_file "
                             "missing, no .env.example, no db sibling or "
                             "no model)\n" % name)
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
        e["depends_on"] = list((s.get("depends_on") or {}).keys()
                               if isinstance(s.get("depends_on"), dict)
                               else s.get("depends_on") or [])
        plan["services"].append(e)
        # Checks derive only from declared facts — never invented: a
        # published tcp port is a connect check; a compose healthcheck
        # is recorded verbatim as an exec check. The verify runner
        # (separate stage) executes these; nothing probes on a guess.
        for p in ports:
            if p["proto"] == "tcp":
                plan["checks"].append({"tcp": {"port": p["host"]}})
        hc = s.get("healthcheck")
        if isinstance(hc, dict):
            test = hc.get("test")
            if isinstance(test, list) and test:
                cmd = " ".join(str(x) for x in test)
                if str(test[0]) in ("CMD", "CMD-SHELL") and len(test) > 1:
                    cmd = str(test[1])
                checks_entry = {"exec": {"cmd": cmd}}
                if checks_entry not in plan["checks"]:
                    plan["checks"].append(checks_entry)
    # Profile-gated services are gone: depends_on must not reference
    # them (compose would refuse "service depends on undefined service").
    if dropped:
        for e in plan["services"]:
            e["depends_on"] = [d for d in e["depends_on"]
                               if d not in dropped]
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
    # Two services may publish the same host port; keep the first check.
    seen = set()
    unique = []
    for c in plan["checks"]:
        key = json.dumps(c, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    plan["checks"] = unique
    return plan


def apply_variant(plan, value):
    # A build arg whose key matches "variant" (case-insensitive) gets
    # the requested value in every built service.
    n = 0
    for e in plan["services"]:
        a = (e.get("build") or {}).get("args")
        if a:
            for k in list(a):
                if k.lower() == "variant":
                    a[k] = value
                    n += 1
    return n


def extract_refines(payload, plan_services):
    # The bounded --intent overlay for resolved plans. The phrase is
    # unbounded free text; the model may only fill these three fields,
    # only for services from the plan. Everything else is ignored.
    names = {e["name"] for e in plan_services}
    reps = {}
    for k, v in (payload.get("replicas") or {}).items():
        k = str(k)
        if k in names and isinstance(v, int) and 2 <= v <= 12:
            reps[k] = v
    env = {}
    for k, v in (payload.get("env") or {}).items():
        k = str(k)
        if k in names and isinstance(v, dict):
            env[k] = {str(ek): ("" if ev is None else str(ev))
                      for ek, ev in v.items()}
    cmd = {}
    for k, v in (payload.get("command") or {}).items():
        k = str(k)
        if k in names and isinstance(v, list) and v:
            cmd[k] = [str(x) for x in v[:16]]
    return {"replicas": reps, "env": env, "command": cmd}


def extract_replicas(payload, names):
    # Legacy shape: replicas-only validation (tests cover the full
    # overlay through extract_refines).
    return extract_refines(payload, [{"name": n} for n in names])["replicas"]


def _diff_note(prev, curr):
    # Gate refinement: show only what changed since the last proposal.
    if prev is None:
        return ""
    changed = [k for k in sorted(set(prev) | set(curr))
               if prev.get(k) != curr.get(k)]
    if not changed:
        return "  diff vs previous: (no change)"
    return "  diff vs previous: %s changed" % ", ".join(changed)


def yaml_safe_dump(data, path_or_file, **kw):
    # pyyaml is a runtime-optional dependency: plan/flatten need it, but
    # classify/profile/ports/refine must work with bare python3.
    import yaml
    return yaml.safe_dump(data, path_or_file, **kw)


def plan_cmd(src_arg, out):
    src = src_arg
    intent = os.environ.get("VMF_RUN_INTENT", "").strip()
    hint = os.environ.get("VMF_COMPOSE_PROJECT", "").strip()

    # Root compose wins; otherwise every subdirectory compose is a candidate.
    # VMF_PLAN_SKIP_COMPOSE=1 (the race's source_build candidate) forces
    # the direct/gapfill path even when a compose file exists.
    # VMF_COMPOSE_HINT_FILE (the enumeration's compose_file, e.g. a dev
    # compose variant) outranks the standard-name scan: the model read
    # the dev script and named the file — the substrate honors it.
    candidates = []
    if os.environ.get("VMF_PLAN_SKIP_COMPOSE", "") != "1":
        cf_hint = os.environ.get("VMF_COMPOSE_HINT_FILE", "").strip()
        if cf_hint and "/" not in cf_hint \
                and os.path.isfile(os.path.join(src, cf_hint)):
            name_, svcs_, ports_ = meta(os.path.join(src, cf_hint))
            candidates.append({"dir": src, "rel": ".", "file": cf_hint,
                               "name": name_, "services": svcs_,
                               "ports": ports_})
        else:
            for cand in NAMES:
                p = os.path.join(src, cand)
                if os.path.isfile(p):
                    name_, svcs_, ports_ = meta(p)
                    candidates.append({"dir": src, "rel": ".", "file": cand,
                                       "name": name_, "services": svcs_,
                                       "ports": ports_})
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

    if not candidates:
        gapfill(src_arg, out)
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
                         % (os.path.relpath(compose_dir, src_arg), resolved["file"]))
    sel_variant = None
    if sel:
        ref = (sel.get("ref") or "").strip()
        sel_variant = (sel.get("variant") or "").strip()
        sys.stderr.write("intent: lab=%s (%s, %d services) ref=%s variant=%s [llm: %s]\n"
                         % (resolved["rel"], resolved["name"] or "-",
                            len(resolved["services"]), ref or "default", sel_variant or "-",
                            (sel.get("why") or "")[:40]))
        url = os.environ.get("VMF_COMPOSE_URL")
        if ref and url:
            sys.stderr.write("intent: re-cloning %s @ %s\n" % (url, ref))
            shutil.rmtree(src, ignore_errors=True)
            rc = subprocess.run(["git", "clone", "--depth", "1", "--branch", ref,
                                 url, src_arg]).returncode
            if rc != 0 or not os.path.isfile(compose_path):
                sys.stderr.write("error: ref %s has no compose at %s\n" % (ref, resolved["rel"]))
                sys.exit(1)

    plan = translate(compose_path, src, src_arg)
    # The intent-selected build variant applies here, in the process that
    # owns the plan (an os.environ set in a child never reaches the bash
    # parent — the old heredoc dropped it silently).
    if sel_variant:
        n = apply_variant(plan, sel_variant)
        if n:
            sys.stderr.write("intent: VARIANT=%s applied to %d service(s)\n" % (sel_variant, n))
    json.dump(plan, open(out, "w"))


def variant_cmd(plan_path, value):
    plan = json.load(open(plan_path))
    if value is None:
        value = os.environ.get("VMF_VARIANT_OVERRIDE", "")
    n = apply_variant(plan, value)
    json.dump(plan, open(plan_path, "w"))
    if n:
        print("intent: VARIANT=%s applied to %d service(s)" % (value, n))


def refine_cmd(plan_path, refines_out, phrase):
    plan = json.load(open(plan_path))
    if not phrase:
        phrase = os.environ.get("VMF_RUN_INTENT", "")
    svcs = [{"name": e["name"],
             "ports": ["%d/%s" % (p["host"], p["proto"]) for p in e["ports"]]}
            for e in plan["services"]]
    prompt = (
        "A run plan already exists with these services:\n%s\n\n"
        "The user's intent for this run: %s\n\n"
        "Adjust the plan within this vocabulary — reply ONE JSON object:\n"
        '{"replicas": {"<service from the list>": <2-12>}, '
        '"env": {"<service>": {"K": "V"}}, '
        '"command": {"<service>": ["<argv>"]}}\n'
        "Set only what the intent asks for; omit fields you leave "
        "unchanged. Unknown services or other fields are ignored; "
        "reply {} when nothing applies. Never invent service names."
        % (json.dumps(svcs), phrase))

    def finalize(feedback=None):
        p = prompt
        if feedback:
            p += ("\nThe user reviewed the proposed overlay and says: "
                  "\"%s\"\nRevise the overlay accordingly." % feedback)
        rc, out, err = vmf_llm.llm_call("intent", p)
        if rc != 0:
            return {"replicas": {}, "env": {}, "command": {}}
        try:
            return extract_refines(vmf_llm.parse_llm_json(out), plan["services"])
        except Exception:
            return {"replicas": {}, "env": {}, "command": {}}

    # The overlay gate: interactive runs get the a / n / free-text loop
    # (same UX as the proposal gates). Detached/scripted runs (--yes or
    # no controlling terminal) apply the validated overlay as-is — the
    # vocabulary bounds it, the gate cannot block a -d run.
    overlay = finalize()
    prev = None
    turns = 0
    while True:
        if not (overlay["replicas"] or overlay["env"] or overlay["command"]):
            if turns == 0:
                sys.stderr.write("intent: no refinement matched\n")
            break
        note = _diff_note(prev, overlay)
        for k, v in sorted(overlay["replicas"].items()):
            sys.stderr.write("intent: scaling %s to %d instances "
                             "(extra ports on the VM side)\n" % (k, v))
        for k, v in sorted(overlay["env"].items()):
            sys.stderr.write("intent: env on %s: %s\n"
                             % (k, " ".join("%s=%s" % kv for kv in sorted(v.items()))))
        for k, v in sorted(overlay["command"].items()):
            sys.stderr.write("intent: command for %s: %s\n"
                             % (k, " ".join(v)))
        if note:
            sys.stderr.write("%s\n" % note)
        if vmf_llm.accepted():
            break
        line = vmf_llm.tty_line(
            "intent: apply overlay? [a = apply, n = ignore, or type a change] ")
        if line is None:
            break  # non-interactive: apply as-is
        if line == "" or line.lower() in ("n", "no"):
            sys.stderr.write("intent: refinement declined; running the plan unchanged\n")
            overlay = {"replicas": {}, "env": {}, "command": {}}
            break
        if line.lower() in ("a", "y", "yes", "proceed"):
            break
        if turns >= 3:
            sys.stderr.write("intent: refinement cap reached; "
                             "applying the last overlay\n")
            break
        prev = overlay
        turns += 1
        overlay = finalize(feedback=line)
    json.dump(overlay, open(refines_out, "w"))


def load_refines(path):
    if path and os.path.isfile(path):
        return json.load(open(path)).get("replicas") or {}
    return {}


def flatten_cmd(manifest_path, compose_out, ports_out, refines_path=None):
    m = json.load(open(manifest_path))
    svcs = {}
    fwd = []
    # Intent refinement (e.g. "Run 5 instances"): the base service keeps
    # its name and declared VM ports; extra instances get a numbered name,
    # their own static IP, and VM ports offset by the instance index. The
    # docker image is shared - one archive, N containers.
    refines, env_over, cmd_over = {}, {}, {}
    rf_path = refines_path or os.environ.get("VMF_PLAN_REFINES", "")
    if rf_path and os.path.isfile(rf_path):
        rf = json.load(open(rf_path))
        refines = rf.get("replicas") or {}
        env_over = rf.get("env") or {}
        cmd_over = rf.get("command") or {}
    SUB = "172.31.100"
    expanded = []
    for e in m["services"]:
        reps = int(refines.get(e["name"]) or 1)
        for i in range(reps):
            iname = e["name"] if i == 0 else "%s-%d" % (e["name"], i + 1)
            expanded.append((iname, e, i))
    names = [n for n, _, _ in expanded]
    ip = {n: "%s.%d" % (SUB, 10 + i) for i, n in enumerate(names)}
    alias_ip = {}
    for e in m["services"]:
        for a in e.get("aliases") or []:
            alias_ip[a] = ip[e["name"]]
    for iname, e, i in expanded:
        n = e["name"]
        tag = m["tags"].get(n) or e.get("image")
        entry = {"image": tag}
        for b in e.get("binds") or []:
            # The stage carries the repo files the compose file binds;
            # the guest mounts them from the data drive.
            entry.setdefault("volumes", []).append(
                "/data/repo/%s:%s:%s" % (b["host"], b["container"],
                                         b.get("mode") or "rw"))
        if cmd_over.get(n):
            entry["command"] = cmd_over[n]
        elif e.get("command"):
            entry["command"] = e["command"]
        if e.get("entrypoint"):
            entry["entrypoint"] = e["entrypoint"]
        if e.get("expose"):
            entry["expose"] = e["expose"]
        for p in e["ports"]:
            fwd.append("%s %d %d %s" % (p["proto"], p["host"] + i, p["cport"], iname))
        merged_env = dict(e["env"])
        if env_over.get(n):
            merged_env.update(env_over[n])
        if merged_env:
            entry["environment"] = merged_env
        if e["depends_on"]:
            entry["depends_on"] = list(e["depends_on"])
        entry["networks"] = {"default": {"ipv4_address": ip[iname]}}
        hosts = {o: ip[o] for o in names if o != iname}
        for a, t in alias_ip.items():
            if t != ip[iname]:
                hosts[a] = t
        entry["extra_hosts"] = ["%s=%s" % (k, v) for k, v in sorted(hosts.items())]
        svcs[iname] = entry
    doc = {"services": svcs}
    doc["networks"] = {"default": {"ipam": {"config": [{"subnet": SUB + ".0/24"}]}}}
    yaml_safe_dump(doc, open(compose_out, "w"), sort_keys=False)
    open(ports_out, "w").write("\n".join(fwd) + ("\n" if fwd else ""))


def ports_cmd(plan_path, refines_path=None):
    plan = json.load(open(plan_path))
    reps = load_refines(refines_path or os.environ.get("VMF_PLAN_REFINES", ""))
    for e in plan["services"]:
        r = int(reps.get(e["name"]) or 1)
        if r > 1 and e["ports"]:
            p = e["ports"][0]
            print("%s\t%s\t%d" % (p["host"], p["proto"], r))


def usage():
    sys.stderr.write(
        "usage: vmf_plan.py <command> ...\n"
        "  plan <src> <plan.json>\n"
        "  variant <plan.json> [value]\n"
        "  refine <plan.json> <refines.json> [phrase]\n"
        "  flatten <manifest.json> <compose.yaml> <ports.txt> [refines.json]\n"
        "  ports <plan.json> [refines.json]\n"
        "  classify <input> [--as KIND]    what vmf thinks the input is\n"
        "  profile <kind> <path-or-ref>    evidence-based VM profile\n"
        "  intent <image> <phrase> <out>   plain-image --intent setup plan\n"
        "  cache-key <src>                 winner-cache key (bundle+compose)\n")


KINDS = ("git-url", "dir", "image", "image-tar", "compose-file", "dockerfile",
         "iso", "ova", "disk", "box", "tarball", "bundle")
URL_SCHEME_RE = re.compile(r"^(https?://|git@|file://)")
ARCHIVE_EXTS = (".tar.gz", ".tgz", ".tar.bz2", ".tar")
DISK_EXTS = (".qcow2", ".qcow", ".vmdk", ".vdi", ".vhd", ".vhdx", ".raw", ".img")
MEDIA_EXTS = (".iso", ".ova")
WINDOWS_ISO_RE = re.compile(r"(?i)win|server|cccoma|dv\d")
KNOWN_LINUX_RE = re.compile(
    r"(?i)ubuntu|debian|fedora|alpine|arch|centos|rocky|almalinux|nixos|kali")

# Degraded-mode profile rows (used when formats.yaml is unreadable).
# scripts/formats.yaml is the canonical, user-tunable table.
BUILTIN_PROFILES = {
    "windows-install": {"ram": 6144, "disk": "64G", "display": "webvnc", "firmware": "uefi"},
    "linux-live": {"ram": 2048, "disk": "8G", "display": "webvnc"},
    "default-linux": {"ram": 2048, "disk": "8G", "display": "webvnc"},
    "unknown-disk": {"ram": 1024, "disk": "as-is", "display": "none"},
    "image-default": {"ram": 1024, "disk": "4G", "display": "none"},
    "box-default": {"ram": 1024, "disk": "10G", "display": "none"},
}


def _human_size(n):
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n


def _is_ipv4(text):
    # Dotted quad with decimal octets 0..255; no socket import needed.
    parts = text.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or not 0 <= int(p) <= 255:
            return False
    return True


def _iso_evidence(path):
    # ISO 9660: primary volume descriptor at sector 16 (2048-byte
    # sectors); "CD001" at offset +1, 32-byte volume id at +40.
    try:
        with open(path, "rb") as f:
            f.seek(32769)
            if f.read(5) != b"CD001":
                return None
            f.seek(32808)
            label = f.read(32).decode("ascii", "replace").rstrip("\x00").strip()
        return label, os.path.getsize(path)
    except OSError:
        return None


def _tar_members(path):
    try:
        with tarfile.open(path) as t:
            return t.getnames()[:60]
    except (tarfile.TarError, OSError, EOFError):
        return None


def _ovf_requirements(path):
    # OVA is self-describing: the .ovf declares memory (ResourceType 4)
    # and vCPU (ResourceType 3) as VirtualQuantity. Zero guessing.
    root = None
    try:
        with tarfile.open(path) as t:
            for m in t.getmembers():
                if m.name.endswith(".ovf"):
                    root = ET.fromstring(t.extractfile(m).read())
                    break
    except (tarfile.TarError, OSError, EOFError, ET.ParseError):
        return None
    if root is None:
        return None
    ram = cpus = None
    for item in root.iter():
        if item.tag.endswith("Item"):
            rt = vq = None
            for el in item:
                if el.tag.endswith("ResourceType"):
                    rt = (el.text or "").strip()
                elif el.tag.endswith("VirtualQuantity"):
                    vq = (el.text or "").strip()
            if rt == "3" and (vq or "").isdigit():
                cpus = int(vq)
            if rt == "4" and (vq or "").isdigit():
                ram = int(vq)
    if ram is None:
        return None
    ram = max(256, min(8192, ram))
    return ram, cpus


def classify(inp, as_kind=None):
    """Decision table: input arg → (kind, detail). Cheap facts only —
    no clone, no registry, no LLM. Raises SystemExit(2) on ambiguity."""
    if as_kind is not None:
        if as_kind not in KINDS:
            sys.stderr.write("error: unknown --as kind '%s'\n" % as_kind)
            sys.exit(2)
        return as_kind, "%s (--as)" % inp
    if re.match(URL_SCHEME_RE, inp):
        low = inp.lower()
        if low.endswith(".iso"):
            return "iso", inp
        if low.endswith(".ova"):
            return "ova", inp
        if low.endswith(ARCHIVE_EXTS):
            return "tarball", inp
        return "git-url", inp
    path = os.path.expanduser(inp)
    if os.path.isdir(path):
        names = set(os.listdir(path))
        if "config.json" in names and "rootfs" in names:
            return "bundle", "%s/ (config.json + rootfs)" % inp.rstrip("/")
        for cand in NAMES:
            if cand in names:
                return "dir", "%s (%s)" % (inp, cand)
        return "dir", "%s (no compose; gap-fill evidence)" % inp
    if os.path.isfile(path):
        size = os.path.getsize(path)
        iso = _iso_evidence(path)
        if iso:
            return "iso", '%s (%s, iso "%s")' % (inp, _human_size(size), iso[0])
        with open(path, "rb") as f:
            head = f.read(4)
        if head == b"QFI\xfb\xfb":
            return "disk", "%s (qcow2, %s)" % (inp, _human_size(size))
        members = _tar_members(path)
        if members is not None:
            if any(m.endswith(".ovf") for m in members):
                return "ova", "%s (%s, OVF)" % (inp, _human_size(size))
            if "metadata.json" in members:
                return "box", "%s (vagrant)" % inp
            if "manifest.json" in members or "oci-layout" in members:
                return "image-tar", "%s (docker-archive, %s)" % (inp, _human_size(size))
            if inp.lower().endswith((".tar.gz", ".tgz", ".tar.bz2")):
                return "tarball", "%s (%s)" % (inp, _human_size(size))
            return "image-tar", "%s (%s)" % (inp, _human_size(size))
        if os.path.basename(inp) in NAMES:
            import yaml
            try:
                doc = yaml.safe_load(open(path)) or {}
                n = len(doc.get("services") or {})
            except Exception:
                n = 0
            return "compose-file", "%s (%d services)" % (inp, n)
        base = os.path.basename(inp)
        if base.startswith("Dockerfile"):
            first = ""
            for line in open(path, errors="replace"):
                if line.startswith("FROM"):
                    parts = line.split()
                    first = parts[1] if len(parts) > 1 else ""
                    break
            return "dockerfile", "%s (base: %s)" % (inp, first or "?")
        if base.lower().endswith(DISK_EXTS):
            return "disk", "%s (%s, %s)" % (inp, os.path.splitext(base)[1][1:],
                                            _human_size(size))
        return "unknown", "%s (no magic match)" % inp
    # Neither a URL nor an existing path.
    looks_path = inp.startswith(("./", "../", "/")) or \
        inp.lower().endswith(MEDIA_EXTS + DISK_EXTS + ARCHIVE_EXTS + (".box",))
    if looks_path:
        if inp.lower().endswith(MEDIA_EXTS + DISK_EXTS + (".box",)):
            sys.stderr.write("error: no such path '%s'\n" % inp)
            sys.stderr.write("       supported: %s\n" % " ".join(KINDS))
        else:
            sys.stderr.write("input: ambiguous '%s' (no such path; matches image ref pattern)\n" % inp)
            sys.stderr.write("       resolve: --as dir|image\n")
        sys.exit(2)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", inp):
        return "image", "%s (assumed)" % inp
    return "image", inp


def classify_cmd(inp, as_kind=None):
    kind, detail = classify(inp, as_kind)
    sys.stderr.write("input: %s %s\n" % (kind, detail))
    if kind == "unknown":
        sys.stderr.write("       supported: %s\n" % " ".join(KINDS))
        return 2
    print(kind)
    return 0


def load_formats():
    # formats.yaml is the user-tunable profile table; the builtin rows
    # above are the degraded fallback when pyyaml is absent.
    p = os.environ.get("VMF_FORMATS") or \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "formats.yaml")
    try:
        import yaml
        return yaml.safe_load(open(p)) or {}
    except Exception:
        return {}


def profile_cmd(kind, path):
    prof = dict(BUILTIN_PROFILES)
    prof.update(load_formats())
    name = None
    row = None
    why = None
    if kind == "iso" and path:
        ev = _iso_evidence(os.path.expanduser(path))
        if ev:
            label, sz = ev
            if WINDOWS_ISO_RE.search(label):
                name, row = "windows-install", prof["windows-install"]
            elif KNOWN_LINUX_RE.search(label):
                name, row = "linux-live", prof["linux-live"]
            else:
                name, row = "default-linux", prof["default-linux"]
            if name == "default-linux":
                why = 'default (unrecognized iso "%s"); override: --ram/--disk' % label
            else:
                why = 'evidence: iso label "%s", %s' % (label, _human_size(sz))
        else:
            name, row = "default-linux", prof["default-linux"]
            why = "table (no iso evidence read)"
    elif kind == "ova" and path:
        req = _ovf_requirements(os.path.expanduser(path))
        if req:
            ram, cpus = req
            row = {"ram": ram, "disk": "as-is"}
            if cpus:
                row["cpus"] = cpus
            name = "from-ovf"
            why = "evidence: OVF declares ram%s" % (", cpus" if cpus else "")
        else:
            name, row = "unknown-disk", prof["unknown-disk"]
            why = "default (no OVF requirements parsed)"
    elif kind == "disk":
        name, row = "unknown-disk", prof["unknown-disk"]
    elif kind == "image":
        name, row = "image-default", prof["image-default"]
    elif kind == "box":
        name, row = "box-default", prof["box-default"]
    else:
        return 0
    parts = ["ram %s" % row.get("ram", "?"), "disk %s" % row.get("disk", "?")]
    if row.get("cpus"):
        parts.append("cpus %s" % row["cpus"])
    if row.get("firmware"):
        parts.append("firmware %s" % row["firmware"])
    display = row.get("display") or "none"
    if display != "none":
        parts.append("display %s" % display)
    sys.stderr.write("profile: %s (%s)" % (name, ", ".join(parts)))
    if why:
        sys.stderr.write("  [%s]" % why)
    sys.stderr.write("\n")
    print("%s %s" % (name, row.get("ram", "")))
    return 0


def fat_base_ref():
    # The fat base tag: VMF_BASE_IMAGE wins; else the ready marker that
    # scripts/build-fat-base.sh writes after a successful host build.
    ref = (os.environ.get("VMF_BASE_IMAGE") or "").strip()
    if ref:
        return ref
    try:
        with open(os.path.join(
                os.path.expanduser("~"),
                ".local", "share", "vmf", "fat-base.ready")) as f:
            return f.read().split("\n")[0].strip()
    except OSError:
        return ""


def base_image_note():
    # Vocabulary for the gap-fill: with a fat base present, plans stop
    # provisioning the common runtimes (the apt/npm window disappears).
    fat = fat_base_ref()
    if fat:
        return (
            'Prefer the fat base "%s" when the evidence matches its '
            "runtime set (node 22, python3, pip, sqlite3, nginx, git, "
            "curl, ca-certificates preinstalled): set base_image to it "
            "and never install those packages. Otherwise use a stock "
            "oci ref: the base image is minimal, so include "
            "prerequisite installs in the install list "
            "(e.g. 'pip install uv' before 'uv sync')." % fat)
    return (
        "The base image is minimal: include prerequisite installs in "
        "the install list (e.g. 'pip install uv' before 'uv sync').")


def _synth_checks(ports, command=None):
    # Every plan carries a verify step. Ports → tcp per port + one
    # lenient HTTP probe on the first port. No ports but a command →
    # one exec check proving the entry binary exists on PATH (the
    # "ssh in and check the binary" shape for non-serving artifacts).
    # Deterministic: same plan in, same checks out.
    cp = _clamp_ports(ports)
    out = [{"tcp": {"port": p}} for p in cp]
    if cp:
        out.append({"probe": {"port": cp[0], "path": "/",
                              "expect_status_max": 399}})
    if not out and command:
        a0 = str(command[0]).strip() if command else ""
        if a0 and "/" not in a0:
            out.append({"exec": {"cmd": "sh -c 'command -v %s'" % a0}})
    return out


def _clamp_ports(raw):
    # Direct-plan ports: guest tcp ports the app listens on; the host
    # publishes each 1:1. Junk is dropped, not guessed.
    ports = []
    for x in (raw or [])[:8]:
        s = str(x).strip()
        if s.lstrip("-").isdigit() and 1 <= int(s) <= 65535:
            p = int(s)
            if p not in ports:
                ports.append(p)
    return ports


def _clamp_images(raw):
    # Direct-plan image refs the HOST supplies (pull with the host's
    # trust, pin TOFU, archive for the guest's docker load). Junk is
    # dropped, not guessed. Short names are normalized the same way the
    # boot path does (oci-run): the host builders refuse them (no
    # containers-registries.conf — buildah rc=125).
    out = []
    for x in (raw or [])[:4]:
        s = str(x).strip()
        if s and " " not in s and "\n" not in s and len(s) < 200:
            if s.startswith("localhost/") or s.startswith("vmf-"):
                pass
            elif "/" in s:
                head = s.split("/", 1)[0]
                if "." not in head and ":" not in head:
                    s = "docker.io/" + s
            else:
                s = "docker.io/library/" + s
            if s not in out:
                out.append(s)
    return out


def _clamp_memory(raw, needs_docker):
    # Direct-plan VM sizing. needs_docker=true brings dockerd +
    # containerd + the app into one VM (measured: ~150 MB of daemons
    # before the app runs), so the floor is 2048; without docker the
    # default stays 1024. Junk degrades to the default, never a guess.
    try:
        mb = int(raw)
    except (TypeError, ValueError):
        mb = 0
    if mb < 1024:
        mb = 1024
    if mb > 8192:
        mb = 8192
    if needs_docker and mb < 2048:
        mb = 2048
    return mb


def _clamp_checks(raw):
    # Model-declared success checks for direct plans. Bounded vocabulary:
    # probe and exec only — tcp checks are derived from declared ports
    # (a declared fact, never a model opinion), and log/rfb arrive later
    # with their runners. Junk is dropped, not guessed.
    out, dropped = [], 0
    for c in (raw or [])[:6]:
        if isinstance(c, dict) and isinstance(c.get("probe"), dict):
            p = c["probe"]
            try:
                port = int(p.get("port"))
            except (TypeError, ValueError):
                dropped += 1
                continue
            if not (1 <= port <= 65535):
                dropped += 1
                continue
            e = {"probe": {"port": port}}
            path = p.get("path")
            if isinstance(path, str) and path.startswith("/"):
                e["probe"]["path"] = path[:200]
            st = p.get("expect_status")
            if isinstance(st, int) and 100 <= st <= 599:
                e["probe"]["expect_status"] = st
            ec = p.get("expect_contains")
            if isinstance(ec, str) and ec:
                e["probe"]["expect_contains"] = ec[:200]
            if e not in out:
                out.append(e)
        elif isinstance(c, dict) and isinstance(c.get("exec"), dict):
            cmd = c["exec"].get("cmd")
            if isinstance(cmd, str) and cmd.strip():
                e = {"exec": {"cmd": cmd.strip()[:300]}}
                if e not in out:
                    out.append(e)
            else:
                dropped += 1
        elif isinstance(c, dict) and c:
            dropped += 1
    if dropped:
        sys.stderr.write("note: %d check(s) dropped (probe|exec only)\n" % dropped)
    return out


def proposal_head(text):
    j = json.loads(text)
    lines = ["  install:"]
    lines += ["    - %s" % i for i in j["install"]] or ["    - (none)"]
    lines.append("  command: %s" % j["command"])
    if j.get("ports"):
        lines.append("  ports: %s" % " ".join(str(p) for p in j["ports"]))
    for c in j.get("checks") or []:
        if "probe" in c:
            p = c["probe"]
            d = "http %d%s -> %s" % (p["port"], p.get("path", "/"),
                                     p.get("expect_status", 200))
            if p.get("expect_contains"):
                d += " (%s)" % p["expect_contains"]
            lines.append("  check: %s" % d)
        elif "exec" in c:
            lines.append("  check: exec %s" % c["exec"]["cmd"])
    lines.append("  env: %s" % (j["env"] or "-"))
    lines.append("  needs_docker: %s" % j["needs_docker"])
    lines.append("  notes: %s" % (j["notes"] or "-"))
    return "intent: setup plan\n" + "\n".join(lines)


def _norm_phrase(phrase):
    # Cache-key hygiene: "Run 5 instances" and "run  5  instances" are
    # one plan. Casefold + collapse whitespace before hashing.
    return " ".join(phrase.split()).casefold()


def ground_for_app(image, phrase, role="intent"):
    # One draft + context7 grounding pass for an app name. Shared by the
    # propose path, the agent session, and the revise loop: the doc
    # facts (version matrices, official images, ports) replace stale
    # priors. Returns (grounded_text, c7_ids) — the grounded text is ""
    # when nothing grounded.
    draft = ("A user wants to run an intent inside a disposable microVM "
             "based on the image '%s'. Intent: %s\n"
             "List up to 3 topics whose CURRENT facts matter (supported "
             "versions, official container images, install steps, "
             "prerequisites). Reply "
             'ONE JSON object: {"lookup": ["<doc topic>"], '
             '"why": "<max 8 words>"}' % (image, phrase))
    rc, o, e = vmf_llm.llm_call(role, draft)
    lookup = []
    if rc == 0:
        try:
            dj = vmf_llm.parse_llm_json(o)
            lookup = [str(x) for x in (dj.get("lookup") or [])[:3]]
        except Exception:
            lookup = []
    return vmf_llm.ground(lookup, doc_cap=2500)


def intent_cache_dir(image, phrase):
    # The verify-revision loop recomputes this path to write a revised
    # plan back; the formula is the intent cache key itself.
    # v5 adds the checks field to direct plans (the verify runner needs
    # the model's own definition of success; tcp checks derive from
    # declared ports either way). v6 routes to official images first:
    # the host supplies them, so version-sensitive stacks (Ghost and
    # node) stop being built from source on a bare OS.
    key = hashlib.sha256(("image-intent-v6\n%s\n%s"
                          % (image, _norm_phrase(phrase)))
                         .encode()).hexdigest()[:12]
    return os.path.join(os.path.expanduser("~"), ".vmf", "generated",
                        "img-" + key)


def intent_cmd(image, phrase, out):
    # --intent on a plain image: the phrase becomes a direct-mode setup
    # plan (install + argv) through the three-phase flow. The base
    # image is the caller's; the model never picks it.
    # Cache: ~/.vmf/generated/img-<hash(image+phrase)>/direct.json; a
    # hit replays without any LLM call.
    # Exit codes: 0 planned · 1 bad output · 2 not approved · 3 unreachable
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    gen = intent_cache_dir(image, phrase)
    dcache = os.path.join(gen, "direct.json")
    if os.path.isfile(dcache):
        # Cache hygiene: a spec that failed its replay verdict
        # repeatedly is rejected here — a fresh grounded plan replaces
        # it instead of the same death loop.
        meta = {}
        try:
            meta = json.load(open(dcache + ".meta.json"))
        except (OSError, ValueError):
            meta = {}
        if meta.get("failed", 0) >= 2:
            sys.stderr.write(
                "intent: cache rejected (%d failed replays); fresh plan\n"
                % meta.get("failed", 0))
        else:
            sys.stderr.write("intent: cache hit %s\n" % dcache)
            open(out, "w").write(open(dcache).read())
            return 0

    # Phase 1: draft + grounding — shared with the agent session and
    # the revise loop.
    grounded, c7_ids = ground_for_app(image, phrase)

    final = (
        "Plan how to fulfil this intent inside a disposable microVM whose "
        "base image is FIXED: %s (already chosen - do not pick another). "
        "The VM boots, runs the install commands once (network available, "
        "root shell), then execs the command as PID 1. "
        "ROUTE FIRST: when the grounded facts name an official container "
        "image for the app, the plan USES it — set needs_docker=true and "
        "images=[\"<ref>\"]; install only docker setup steps (the host "
        "supplies the image) and make command/docker run the app. "
        "Source-install the app only when no official image exists. "
        "The image family "
        "matters for the package manager (ubuntu/debian: apt; alpine: "
        "apk). Include every prerequisite (e.g. 'pip install uv' before "
        "'uv sync'; curl/repos before npm). List every guest TCP port the "
        "app will listen on (from the intent phrase or the app's default "
        "config); the host publishes each one 1:1. Declare success "
        "checks: one probe per HTTP-serving port whose status (and, when "
        "a specific content proves it, body text) says the app works; "
        "tcp checks for declared ports are added automatically. For a "
        "non-serving artifact (CLI, binary, library build) declare an "
        "exec check that proves it runs: a command executed in the VM "
        "via ssh, judged by exit code. At most "
        "4 checks. Size the VM: modern "
        "CLIs and TUIs often need more than the 1024 MB default, and "
        "needs_docker=true needs at least 2048 (dockerd, containerd and "
        "the app share the VM). Reply "
        "with ONE JSON object:\n"
        '{"install": ["<shell commands run once at boot>"], '
        '"command": ["<argv that starts the app>"], '
        '"ports": [<guest tcp port, e.g. 1337>], '
        '"checks": [{"probe": {"port": 1337, "path": "/", '
        '"expect_status": 200, "expect_contains": "<optional text>"}}], '
        '"images": ["<container image the app needs; the host pulls it>"], '
        '"env": {"K": "V"}, "needs_docker": <bool>, '
        '"memory_mb": <int vm ram, 1024-8192>, '
        '"notes": "<max 12 words>"}\n'
        "Intent: %s\n%s"
        % (image, phrase, grounded))

    def finalize(feedback=None):
        p = final
        if feedback:
            p += ("\nThe user reviewed the previous proposal and says: "
                  "\"%s\"\nRevise the plan accordingly." % feedback)
        rc, o, e = vmf_llm.llm_call("intent", p)
        if rc != 0:
            sys.stderr.write(e)
            sys.stderr.write("error: --intent needs a reachable model\n")
            sys.exit(3)
        try:
            return vmf_llm.parse_llm_json(o)
        except Exception:
            sys.stderr.write("error: --intent returned unparseable JSON:\n%s\n"
                             % o[:400])
            sys.exit(1)

    # The gate is a loop, not a binary: a = proceed, n = abort, free
    # text = refine (the model revises; the diff shows what changed).
    # Capped at 3 turns; --yes skips the loop entirely.
    plan = finalize()
    prev = None
    turns = 0
    while True:
        install = [str(x) for x in (plan.get("install") or [])][:20]
        cmd = [str(x) for x in (plan.get("command") or [])][:16]
        if not cmd:
            sys.stderr.write("error: the intent produced no command\n")
            return 1
        payload = {"install": install, "command": cmd,
                   "ports": _clamp_ports(plan.get("ports")),
                   "checks": _clamp_checks(plan.get("checks")),
                   "images": _clamp_images(plan.get("images")),
                   "env": {str(k): str(v)
                           for k, v in (plan.get("env") or {}).items()},
                   "needs_docker": bool(plan.get("needs_docker")),
                   "memory_mb": _clamp_memory(plan.get("memory_mb"),
                                              plan.get("needs_docker"))}
        plan_text = json.dumps({"base_image": "",  # caller's image; unset
                                **payload, "notes": plan.get("notes", "")},
                               indent=2)
        note = _diff_note(prev, payload)
        sys.stderr.write("%s%s\n  grounding: %s\n"
                         % (proposal_head(plan_text),
                            ("\n%s" % note) if note else "",
                            vmf_llm.grounding_note(c7_ids)))
        if vmf_llm.accepted():
            break
        line = vmf_llm.tty_line(
            "intent: refine? [a = proceed, n = abort, or type a change] ")
        if line is None or line == "" or line.lower() in ("n", "no"):
            sys.stderr.write("intent: not approved; rerun with --yes to accept\n")
            return 2
        if line.lower() in ("a", "y", "yes", "proceed"):
            break
        if turns >= 3:
            sys.stderr.write("intent: refinement cap reached; "
                             "proceeding with the last proposal\n")
            break
        prev = payload
        turns += 1
        plan = finalize(feedback=line)
    os.makedirs(gen, exist_ok=True)
    open(dcache, "w").write(plan_text)
    open(dcache + ".meta.json", "w").write(json.dumps(
        {"model": os.environ.get("VMF_INTENT_MODEL") or os.environ.get("VMF_LLM_MODEL", ""),
         "intent": phrase, "context7": c7_ids, "created": "",
         "refined": turns > 0, "turns": turns}, indent=2))
    open(out, "w").write(plan_text)
    sys.stderr.write("intent: approved; cached %s\n" % dcache)
    return 0


REPO_READ_FILES = (
    "README.md", "README.rst", "readme.md", "README",
    "AGENTS.md", "CLAUDE.md",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "Dockerfile", "install.sh", "setup.sh",
    "package.json", "Makefile", "requirements.txt", "pyproject.toml",
    "setup.py", "go.mod", "Cargo.toml", "Gemfile", "pom.xml",
    "build.gradle", "CMakeLists.txt", ".env.example",
)
APPROACH_KINDS = ("compose", "dockerfile", "prebuilt_image",
                  "install_script", "source_build")
APPROACH_COST = {"compose": "fast", "dockerfile": "slow",
                 "prebuilt_image": "fast", "install_script": "slowest",
                 "source_build": "slowest"}


def _fmt_bytes(n):
    if n >= 1048576:
        return "%.1f MB" % (n / 1048576)
    if n >= 1024:
        return "%.1f KB" % (n / 1024)
    return "%d B" % n


def read_repo_files(src, cap_bytes=40960):
    # Bounded read set from the clone root, cheapest-evidence order.
    found = []
    skipped = 0
    total = 0
    for rel in REPO_READ_FILES:
        p = os.path.join(src, rel)
        if not os.path.isfile(p):
            continue
        sz = os.path.getsize(p)
        if total + sz > cap_bytes:
            skipped += 1
            continue
        with open(p, "rb") as f:
            content = f.read(cap_bytes - total).decode("utf-8", "replace")
        found.append((rel, sz, content))
        total += sz
    return found, skipped, total


def print_reading_phase(found, skipped, src, verbose=False):
    # The visible reading phase: what was read, in what size, and which
    # watchlist files are absent. --grounding adds numbered excerpts.
    watchlist = ("README.md", "AGENTS.md", "docker-compose.yml",
                 "Dockerfile", ".env.example")
    shown = set()
    for rel, sz, content in found:
        sys.stderr.write("reading: %-24s %8s\n" % (rel, _fmt_bytes(sz)))
        shown.add(rel)
        if verbose:
            for i, line in enumerate(content.splitlines()[:8], 1):
                if line.strip():
                    sys.stderr.write("  %4d: %s\n" % (i, line[:100]))
    for rel in watchlist:
        if rel in shown:
            continue
        if os.path.isfile(os.path.join(src, rel)):
            continue  # found but over the byte cap; the skip note covers it
        sys.stderr.write("reading: %-24s      --\n" % rel)
    if skipped:
        sys.stderr.write("reading: %d file(s) skipped (byte cap)\n" % skipped)


def _base_approaches(found):
    # Deterministic enumeration from file presence: the fallback when the
    # model is unavailable, and the sanity floor when it answers.
    names = {rel for rel, _, _ in found}
    out = []
    if names & {"docker-compose.yml", "docker-compose.yaml",
                "compose.yml", "compose.yaml"}:
        out.append({"kind": "compose",
                    "evidence": "compose file in the repo root",
                    "cost": "fast"})
    if "Dockerfile" in names:
        out.append({"kind": "dockerfile",
                    "evidence": "Dockerfile in the repo root",
                    "cost": "slow"})
    if names & {"package.json", "Makefile", "requirements.txt",
                "pyproject.toml", "go.mod", "Cargo.toml", "Gemfile",
                "pom.xml", "build.gradle", "CMakeLists.txt"}:
        out.append({"kind": "source_build",
                    "evidence": "build manifest in the repo root",
                    "cost": "slowest"})
    return out


def _clamp_image_ref(raw):
    # A container image reference: registry/repo[:tag][@digest]. Bare
    # library names are allowed; junk is not.
    s = str(raw or "").strip()
    if not s or len(s) > 200 or any(c in s for c in " \t\n\"'"):
        return ""
    return s


def _clamp_int_ports(raw):
    # Serving ports the docs declare: 1-8 distinct ints.
    out = []
    for x in (raw or [])[:8]:
        try:
            p = int(str(x).strip())
        except ValueError:
            continue
        if 1 <= p <= 65535 and p not in out:
            out.append(p)
    return out[:8]


def _clamp_compose_file(raw, src):
    # A compose file the model identified in the repo ROOT (a dev
    # compose variant like compose.dev.yaml). Basename only; it must
    # exist; the substrate never trusts a path with separators.
    s = str(raw or "").strip()
    if not s or "/" in s or "\\" in s or not s.endswith((".yaml", ".yml")):
        return ""
    if len(s) > 120 or not os.path.isfile(os.path.join(src, s)):
        return ""
    return s


def enumerate_cmd(src, out, verbose=False):
    found, skipped, total = read_repo_files(src)
    print_reading_phase(found, skipped, src, verbose=verbose)
    base = _base_approaches(found)

    prompt = (
        "You plan disposable microVM runs. These are the first files of a "
        "cloned repository. List EVERY way to install and run this "
        "application, cheapest first.\n"
        'Reply ONE JSON object: {"approaches": [{"kind": "compose|'
        'dockerfile|prebuilt_image|install_script|source_build", '
        '"evidence": "<file: reason, max 12 words>", '
        '"cost": "fast|slow|slowest", '
        '"image": "<container ref, prebuilt_image only>", '
        '"compose_file": "<compose file the dev script uses, '
        'compose kind only>", '
        '"ports": [<int serving ports, when the docs state them>]}]}\n'
        "Rules: kinds from the vocabulary only; at most 4; cheapest first; "
        "evidence names a real file; for prebuilt_image the ref is exact "
        "(e.g. ghcr.io/org/app:latest); ports only when documented.\n\n")
    for rel, _, content in found:
        prompt += "===== %s =====\n%s\n" % (rel, content)
    role = os.environ.get("VMF_ENUMERATE_ROLE", "gapfill")
    rc, o, e = vmf_llm.llm_call(role, prompt, timeout=480)
    got = []
    if rc == 0:
        try:
            dj = vmf_llm.parse_llm_json(o)
            for a in (dj.get("approaches") or [])[:4]:
                if not isinstance(a, dict):
                    continue
                kind = a.get("kind")
                if kind not in APPROACH_KINDS:
                    continue
                entry = {"kind": kind,
                         "evidence": str(a.get("evidence") or kind)[:120],
                         "cost": a.get("cost") if a.get("cost")
                                 in ("fast", "slow", "slowest")
                                 else APPROACH_COST[kind]}
                img = _clamp_image_ref(a.get("image"))
                if img and kind == "prebuilt_image":
                    entry["image"] = img
                cf = _clamp_compose_file(a.get("compose_file"), src)
                if cf and kind == "compose":
                    entry["compose_file"] = cf
                ports = _clamp_int_ports(a.get("ports"))
                if ports:
                    entry["ports"] = ports
                got.append(entry)
        except Exception:
            got = []
    # Clamp: dedupe by kind, first occurrence wins (the model's order);
    # the substrate owns cost — the model's cost hint is advisory only,
    # so every entry carries the canonical cost and the table re-sorts.
    merged = []
    seen = set()
    for a in got + base:
        if a["kind"] in seen:
            continue
        seen.add(a["kind"])
        a["cost"] = APPROACH_COST[a["kind"]]
        merged.append(a)
    rank = {"fast": 0, "slow": 1, "slowest": 2}
    merged.sort(key=lambda a: rank[a["cost"]])
    if rc != 0:
        sys.stderr.write("enumerate: model unavailable; deterministic "
                         "enumeration (%s)\n" % (e.strip() or "no LLM"))
    sys.stderr.write("grounded call: %d file(s), %s context, %d call(s)\n"
                     % (len(found), _fmt_bytes(total), 1 if rc == 0 else 0))
    doc = {"approaches": merged}
    with open(out, "w") as f:
        json.dump(doc, f, indent=2)
    for i, a in enumerate(merged, 1):
        sys.stderr.write("plan: %d %-14s %-40s %s\n"
                         % (i, a["kind"], a["evidence"], a["cost"]))
    if not merged:
        sys.stderr.write("error: no install approach found in %s\n" % src)
        return 1
    return 0


def main(argv):
    if len(argv) < 2:
        usage()
        return 2
    cmd, rest = argv[1], argv[2:]
    if cmd == "plan" and len(rest) >= 2:
        plan_cmd(rest[0], rest[1])
    elif cmd == "enumerate" and len(rest) >= 2:
        verbose = "--grounding" in rest
        rest = [a for a in rest if a != "--grounding"]
        return enumerate_cmd(rest[0], rest[1], verbose=verbose)
    elif cmd == "variant" and len(rest) >= 1:
        variant_cmd(rest[0], rest[1] if len(rest) > 1 else None)
    elif cmd == "refine" and len(rest) >= 2:
        refine_cmd(rest[0], rest[1], rest[2] if len(rest) > 2 else None)
    elif cmd == "flatten" and len(rest) >= 3:
        flatten_cmd(rest[0], rest[1], rest[2], rest[3] if len(rest) > 3 else None)
    elif cmd == "ports" and len(rest) >= 1:
        ports_cmd(rest[0], rest[1] if len(rest) > 1 else None)
    elif cmd == "classify" and len(rest) >= 1:
        as_kind = None
        if len(rest) >= 3 and rest[1] == "--as":
            as_kind = rest[2]
        return classify_cmd(rest[0], as_kind)
    elif cmd == "profile" and len(rest) >= 2:
        return profile_cmd(rest[0], rest[1])
    elif cmd == "intent" and len(rest) >= 3:
        return intent_cmd(rest[0], rest[1], rest[2])
    elif cmd == "cache-key" and len(rest) >= 1:
        # No yaml import, no LLM: the race reads this before the
        # enumerate and must not pay the interpreter's yaml dance.
        if not os.path.isdir(rest[0]):
            sys.stderr.write("error: no such directory: %s\n" % rest[0])
            return 2
        print(winner_key(rest[0]))
        return 0
    else:
        usage()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))