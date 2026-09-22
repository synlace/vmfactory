#!/usr/bin/env python3
# vmf_llm.py — the single LLM seam: transport, three-phase propose flow
# (draft → context7 grounding → finalize), parser, and gate.
#
# Every model touchpoint in vmf goes through this module. The model is
# an accelerator, never a dependency: every helper degrades
# deterministically (empty grounding, honest exits) when the transport
# is missing or unreachable.
#
# Transport contracts (documented in llm.sh / context7.sh):
#   llm.sh --role R <prompt>   stdout = assistant text
#                              exit 0 ok · 2 usage · 3 unconfigured
#   context7.sh search <topic> stdout = one JSON object per line
#                              {"id","title","description","updated"}
#                              exit 4 on transport failure
#   context7.sh docs <lib> <topic>   stdout = doc text
#
# The three-phase flow shape (draft → ground → finalize):
#   1. draft: the model lists up to 3 topics whose CURRENT facts matter
#      (its recall of package names and install steps may be stale)
#   2. ground: fetch current docs per topic; every failure degrades
#      silently and the prompt says to state facts conservatively
#   3. finalize: strict-JSON plan parsed by parse_llm_json (fence- and
#      double-encoding-tolerant); the caller validates against the
#      bounded vocabulary
import json
import os
import subprocess


def _scripts():
    # Resolved per call (not at import): tests and callers redirect the
    # transport by setting VMF_SCRIPTS_DIR; the default is this dir.
    return os.environ.get("VMF_SCRIPTS_DIR") or os.path.dirname(os.path.abspath(__file__))


def run(cmd, timeout=90, env=None, stdin=None):
    # env REPLACES the environment at the subprocess level; callers pass
    # overrides, so merge them over the inherited environment here.
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=full_env, input=stdin)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 99, "", "timeout"


def llm_call(role, prompt, timeout=90, env=None):
    # The prompt rides stdin ("-"): grounded prompts (repo evidence +
    # doc facts) exceed the per-arg exec limit (E2BIG: "Argument list
    # too long") and silently killed the gap-fill before a plan existed.
    return run(["bash", os.path.join(_scripts(), "llm.sh"), "--role", role,
                "-"], timeout, env, stdin=prompt)


def c7_search(topic, timeout=30):
    rc, out, _ = run(["bash", os.path.join(_scripts(), "context7.sh"), "search", topic],
                     timeout)
    if rc != 0:
        return []
    try:
        return [json.loads(l) for l in out.strip().splitlines() if l.strip()]
    except Exception:
        return []


def c7_docs(lib, topic, timeout=40):
    rc, out, _ = run(["bash", os.path.join(_scripts(), "context7.sh"), "docs", lib, topic],
                     timeout)
    if rc != 0 or not out.strip():
        return ""
    return out


def ground(lookup, doc_cap=3000):
    # Phase 2: grounding. Returns (grounded_text, c7_ids); empty lookup
    # or dead transport degrades to a conservative-state-facts note.
    grounding = []
    c7_ids = []
    for topic in (lookup or [])[:3]:
        hits = c7_search(topic)
        if not hits:
            continue
        lib = hits[0]["id"]
        docs = c7_docs(lib, topic)
        if not docs:
            continue
        grounding.append("=== context7: %s (%s, updated %s) ===\n%s"
                         % (lib, topic, hits[0].get("updated", "?"),
                            docs[:doc_cap]))
        c7_ids.append("%s [%s]" % (lib, topic))
    grounded = ("\nGrounding - CURRENT documentation fetched for the lookup "
                "topics; prefer these facts over your recall:\n"
                + "\n".join(grounding)) if grounding else \
               ("\nGrounding: context7 unavailable for this run; state facts "
                "conservatively and prefer the evidence below.\n")
    return grounded, c7_ids


def grounding_note(c7_ids):
    return ("grounded via context7: " + ", ".join(c7_ids[:3])) if c7_ids \
        else "NOT grounded (context7 unavailable)"


def parse_llm_json(raw):
    # LLM payloads arrive wrapped in markdown fences, optionally tagged
    # ("```json ... ```"), and occasionally double-encoded as a JSON
    # string. Strip the fence, drop a language tag, then parse.
    raw = raw.strip()
    if raw.startswith("`") and raw.endswith("`"):
        raw = raw.strip("`").strip()
        if raw[:4].lower() == "json":
            raw = raw[4:].lstrip()
    j = json.loads(raw)
    if isinstance(j, str):
        j = json.loads(j)
    return j


def accepted(env_key="VMF_RUN_YES"):
    return os.environ.get(env_key) == "1"


def tty_ask(prompt, words=("y", "yes")):
    # stdin may be a pipe (heredoc); the controlling terminal is
    # reachable through /dev/tty. Raw fd I/O: buffered streams
    # misbehave on some ttys.
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
        os.write(fd, prompt.encode())
        buf = b""
        while not buf.endswith(b"\n"):
            c = os.read(fd, 1)
            if not c:
                break
            buf += c
        os.close(fd)
        return buf.decode().strip().lower() in words
    except OSError:
        return False


def tty_line(prompt):
    # One free-text line from the controlling terminal: the refine loop
    # feeds it back to the model. Returns the stripped line, or None
    # when there is no controlling terminal (decline, honestly).
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
        os.write(fd, prompt.encode())
        buf = b""
        while not buf.endswith(b"\n"):
            c = os.read(fd, 1)
            if not c:
                break
            buf += c
        os.close(fd)
        return buf.decode().strip()
    except OSError:
        return None


def gate(prompt, yes_env="VMF_RUN_YES"):
    # Interactive approval: --yes bypasses; a declined gate is an
    # honest exit 2 for the caller to translate.
    if accepted(yes_env):
        return True
    if tty_ask(prompt):
        return True
    return False