#!/usr/bin/env python3
# Image-intent planner: turn a natural-language intent against a PLAIN
# base image ("just run ubuntu --intent 'Latest version of kilocode
# CLI'") into a direct-mode install plan. Same three phases as the repo
# gap-filler: draft (which topics need CURRENT facts) -> context7
# grounding -> finalize. The base image is the caller's (already
# normalized/pinned by oci-run); the model never picks it.
#
# Output: the direct plan JSON on --out. Gate: interactive y/N or --yes
# (provenance line included). Cache: ~/.vmf/generated/<hash>/direct.json
# keyed by image+phrase; a cache hit replays without any LLM call.
#
# Exit codes: 0 planned, 2 not approved, 3 unconfigured/unreachable.
import hashlib
import json
import os
import subprocess
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))


def run(cmd, timeout=90):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 99, "", "timeout"


def llm_call(prompt):
    return run(["bash", os.path.join(SCRIPTS, "llm.sh"),
                "--role", "intent", prompt])


def parse_json(raw):
    raw = raw.strip().strip("`")
    j = json.loads(raw)
    if isinstance(j, str):
        j = json.loads(j)
    return j


def main():
    args = sys.argv[1:]
    image = args[0]
    phrase = args[1]
    out = args[2] if len(args) > 2 else "direct.json"
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    llm = os.path.join(SCRIPTS, "llm.sh")
    ctx7 = os.path.join(SCRIPTS, "context7.sh")

    key = hashlib.sha256(("image-intent-v2\n%s\n%s" % (image, phrase))
                         .encode()).hexdigest()[:12]
    gen = os.path.join(os.path.expanduser("~"), ".vmf", "generated",
                       "img-" + key)
    dcache = os.path.join(gen, "direct.json")
    if os.path.isfile(dcache):
        sys.stderr.write("intent: cache hit %s\n" % dcache)
        open(out, "w").write(open(dcache).read())
        return 0

    # Phase 1: draft - which topics need CURRENT docs.
    draft = ("A user wants to run an intent inside a disposable microVM "
             "based on the image '%s'. Intent: %s\n"
             "List up to 3 topics whose CURRENT facts matter (package "
             "names on registries, install steps, prerequisites). Reply "
             'ONE JSON object: {"lookup": ["<doc topic>"], '
             '"why": "<max 8 words>"}' % (image, phrase))
    rc, o, e = llm_call(draft)
    lookup = []
    if rc == 0:
        try:
            dj = parse_json(o)
            lookup = [str(x) for x in (dj.get("lookup") or [])[:3]]
        except Exception:
            lookup = []

    grounding, c7_ids = [], []
    for topic in lookup:
        rc, o, _ = run(["bash", os.path.join(SCRIPTS, "context7.sh"),
                        "search", topic], timeout=30)
        if rc != 0:
            continue
        try:
            hits = [json.loads(l) for l in o.strip().splitlines() if l.strip()]
        except Exception:
            continue
        if not hits:
            continue
        lib = hits[0]["id"]
        rc, o, _ = run(["bash", os.path.join(SCRIPTS, "context7.sh"),
                        "docs", lib, topic], timeout=40)
        if rc != 0 or not o.strip():
            continue
        grounding.append("=== context7: %s (%s, updated %s) ===\n%s"
                         % (lib, topic, hits[0].get("updated", "?"), o[:2500]))
        c7_ids.append("%s [%s]" % (lib, topic))
    grounded = ("\nGrounding - CURRENT documentation; prefer these facts "
                "over your recall:\n" + "\n".join(grounding)) if grounding \
        else ("\nGrounding: context7 unavailable; state facts "
              "conservatively.\n")

    final = (
        "Plan how to fulfil this intent inside a disposable microVM whose "
        "base image is FIXED: %s (already chosen - do not pick another). "
        "The VM boots, runs the install commands once (network available, "
        "root shell), then execs the command as PID 1. The image family "
        "matters for the package manager (ubuntu/debian: apt; alpine: "
        "apk). Include every prerequisite (e.g. 'pip install uv' before "
        "'uv sync'; curl/repos before npm). Size the VM: modern CLIs and "
        "TUIs often need more than the 1024 MB default. Reply with ONE "
        "JSON object:\n"
        '{"install": ["<shell commands run once at boot>"], '
        '"command": ["<argv that starts the app>"], '
        '"env": {"K": "V"}, "needs_docker": <bool>, '
        '"memory_mb": <int vm ram, 1024-8192>, '
        '"notes": "<max 12 words>"}\n'
        "Intent: %s\n%s"
        % (image, phrase, grounded))
    rc, o, e = llm_call(final)
    if rc != 0:
        sys.stderr.write(e)
        sys.stderr.write("error: --intent needs a reachable model\n")
        return 3
    try:
        plan = parse_json(o)
    except Exception:
        sys.stderr.write("error: --intent returned unparseable JSON:\n%s\n"
                         % o[:400])
        return 1
    install = [str(x) for x in (plan.get("install") or [])][:20]
    cmd = [str(x) for x in (plan.get("command") or [])][:16]
    if not cmd:
        sys.stderr.write("error: the intent produced no command\n")
        return 1
    plan_text = json.dumps({"base_image": "",  # caller's image; unset
                            "install": install, "command": cmd,
                            "env": {str(k): str(v)
                                    for k, v in (plan.get("env") or {}).items()},
                            "needs_docker": bool(plan.get("needs_docker")),
                            "memory_mb": int(plan.get("memory_mb") or 1024),
                            "notes": plan.get("notes", "")}, indent=2)
    g = ("grounded via context7: " + ", ".join(c7_ids[:3])) if c7_ids \
        else "NOT grounded (context7 unavailable)"
    accept = os.environ.get("VMF_RUN_YES") == "1"
    sys.stderr.write("%s\n  grounding: %s\n" % (proposal_head(plan_text), g))
    if not accept:
        # Prompt on the controlling terminal (stdin may be a pipe); raw
        # fd I/O - buffered streams misbehave on some ttys.
        import os as _os
        try:
            fd = _os.open("/dev/tty", _os.O_RDWR)
            _os.write(fd, b"intent: boot with this plan? [y/N] ")
            buf = b""
            while not buf.endswith(b"\n"):
                c = _os.read(fd, 1)
                if not c:
                    break
                buf += c
            _os.close(fd)
            accept = buf.decode().strip().lower() in ("y", "yes")
        except OSError:
            accept = False
    if not accept:
        sys.stderr.write("intent: not approved; rerun with --yes to accept\n")
        return 2
    os.makedirs(gen, exist_ok=True)
    open(dcache, "w").write(plan_text)
    open(dcache + ".meta.json", "w").write(json.dumps(
        {"model": os.environ.get("VMF_INTENT_MODEL") or os.environ.get("VMF_LLM_MODEL", ""),
         "intent": phrase, "context7": c7_ids, "created": ""}, indent=2))
    open(out, "w").write(plan_text)
    sys.stderr.write("intent: approved; cached %s\n" % dcache)
    return 0


def proposal_head(text):
    j = json.loads(text)
    lines = ["  install:"]
    lines += ["    - %s" % i for i in j["install"]] or ["    - (none)"]
    lines.append("  command: %s" % j["command"])
    lines.append("  env: %s" % (j["env"] or "-"))
    lines.append("  needs_docker: %s" % j["needs_docker"])
    lines.append("  notes: %s" % (j["notes"] or "-"))
    return "intent: setup plan\n" + "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())