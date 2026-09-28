#!/usr/bin/env python3
# The Python reference grader over fixtures/acceptance.yaml.
#
# Grades the frozen reference surface against the recorded rows. Each
# row clones the pinned SHA (a moving tip re-plans and drifts; the pin
# reproduces the recorded gen dir, so a graded run replays the cache
# with zero LLM calls — a cache miss still grades, at bounded spend),
# runs one `preview`, and asserts the machine-readable verdict classes
# from the render, the content-keyed gen dir, and the run's stderr.
# The parity report is the diff between this output and the port
# grader's.
import json
import os
import re
import shutil
import subprocess
import sys
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vmf_plan  # noqa: E402

GEN_ROOT = os.path.expanduser("~/.vmf/generated")


def _clone(url, name, sha):
    # Pinned checkout: the dir name carries the pin, so a moved tip
    # never masquerades as the recorded content.
    d = os.path.join("/tmp/kilo",
                     "fixture-%s-%s" % (name, (sha or "head")[:9]))
    if not os.path.isdir(d):
        rc = subprocess.run(["git", "clone", url, d],
                            capture_output=True, text=True, timeout=600)
        if rc.returncode != 0:
            sys.stderr.write("grade: clone failed: %s\n"
                             % (rc.stderr or "").strip()[-160:])
            return None
    if sha:
        rc = subprocess.run(["git", "-C", d, "checkout", "-q", sha],
                            capture_output=True, text=True, timeout=120)
        if rc.returncode != 0:
            # A stale or shallow copy without the pin: re-clone.
            shutil.rmtree(d, ignore_errors=True)
            rc = subprocess.run(["git", "clone", url, d],
                                capture_output=True, text=True, timeout=600)
            if rc.returncode == 0:
                rc = subprocess.run(["git", "-C", d, "checkout", "-q", sha],
                                    capture_output=True, text=True,
                                    timeout=120)
        if rc.returncode != 0:
            sys.stderr.write("grade: pin %s unavailable: %s\n"
                             % (sha, (rc.stderr or "").strip()[-160:]))
            return None
    return d


def _approach_checks(a):
    return (a.get("checks") or (a.get("direct") or {}).get("checks") or [])


def _verdicts(runnables, blocked):
    return {
        "runnable": sorted(a.get("method") or "?" for a in runnables),
        "blocked": sorted(blocked),
    }


def _row_spec_h(gen, intent):
    # Mirror preview_cmd: plans and blocked files stamp the run's spec
    # fingerprint; a no-intent run stamps "". Read after the run so a
    # cold derive is judged by the spec it actually wrote.
    if not intent:
        return ""
    try:
        sp = json.load(open(os.path.join(gen, "spec.json")))
        if (sp.get("intent") or "").strip() == intent.strip() \
                and sp.get("deliverable"):
            return vmf_plan._spec_h(sp)
    except (OSError, ValueError):
        pass
    return None


def _blocked_from_gen(gen, sh):
    blocked = {}
    if not os.path.isdir(gen):
        return blocked
    for name in os.listdir(gen):
        if not name.endswith(".json.blocked"):
            continue
        m = name[len("plan-"):-len(".json.blocked")]
        try:
            b = json.load(open(os.path.join(gen, name)))
        except (OSError, ValueError):
            blocked[m] = "blocked"
            continue
        if (b.get("spec_h") or "") == sh:
            blocked[m] = str(b.get("why") or "blocked")
    return blocked


def _enrich_direct(gen, runnables):
    # Mirror preview_cmd: install_script/source_build approaches carry
    # their direct payload in plan-<m>.json, not in the approaches
    # file.
    for a in runnables:
        m = a.get("method")
        if m and a.get("kind") in ("install_script", "source_build"):
            pj = os.path.join(gen, "plan-%s.json" % m)
            try:
                a["direct"] = (json.load(open(pj)).get("approach")
                               or {}).get("direct") or {}
            except (OSError, ValueError):
                a["direct"] = {}


def grade_row(row, results):
    rid = row["id"]
    url = row["url"]
    sha = row.get("sha")
    intent = row.get("intent")
    src_row = row.get("repeat_of")
    clone_name = src_row or rid
    ok = []

    def good(name, cond, detail=""):
        ok.append((name, bool(cond), detail))

    d = _clone(url, clone_name, sha)
    if not d:
        good("clone", False, url)
        results[rid] = {"ok": ok}
        return
    root = d
    gen = os.path.join(GEN_ROOT, vmf_plan.winner_key(root))
    env = dict(os.environ)
    if intent:
        env["VMF_RUN_INTENT"] = intent
    else:
        env.pop("VMF_RUN_INTENT", None)
    t0 = time.time()
    p = subprocess.run(
        [sys.executable, os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "vmf_plan.py"), "preview", root, "--plain"],
        capture_output=True, text=True, timeout=1800, env=env)
    out, err, rc = p.stdout, p.stderr, p.returncode
    wall = time.time() - t0
    results[rid] = {"ok": ok, "rc": rc, "gen": gen,
                    "wall": wall, "runnables": [], "blocked": {}}

    good("clone", True)
    exp = row.get("expect") or {}
    # The plain render prints at column 0: "spec    deliverable  web",
    # "spec    (none — ...)", sub-fields indented eight spaces.
    m_spec = re.search(r"^spec    deliverable  (\w+)", out, re.M)
    m_none = re.search(r"^spec    \(none", out, re.M)
    spec = None
    if m_spec:
        spec = {"deliverable": m_spec.group(1)}
    want_spec = exp.get("spec")
    if want_spec is None:
        good("spec none", bool(m_none), str(spec))
    else:
        good("spec deliverable",
             spec and spec.get("deliverable")
             == want_spec.get("deliverable"),
             str(spec and spec.get("deliverable")))
        if want_spec.get("serve_port"):
            m_port = re.search(r"0\.0\.0\.0:(\d+)", out)
            good("serve port",
                 m_port and int(m_port.group(1)) == want_spec["serve_port"],
                 m_port.group(1) if m_port else "absent")
        if want_spec.get("user"):
            good("spec user",
                 re.search(r"^ {8}user\s+%s$" % want_spec["user"],
                           out, re.M) is not None)
        if want_spec.get("provenance_fresh"):
            good("provenance fresh",
                 "deterministic fallback" not in out)
    runnables = []
    try:
        runnables = json.load(open(os.path.join(
            gen, ".preview-approaches.json"))).get("approaches") or []
    except (OSError, ValueError):
        pass
    _enrich_direct(gen, runnables)
    sh = _row_spec_h(gen, intent)
    blocked = {} if sh is None else _blocked_from_gen(gen, sh)
    results[rid]["runnables"] = runnables
    results[rid]["blocked"] = blocked
    good("exit", rc == (exp.get("exit") or 0), "rc=%s" % rc)
    good("min runnable", len(runnables) >= (exp.get("min_runnable") or 1),
         "runnable=%d" % len(runnables))
    if exp.get("runnable_with_cmd_check"):
        good("cmd check in runnables",
             any(any(isinstance(c, dict) and "cmd" in c
                     for c in _approach_checks(a)) for a in runnables))
    if exp.get("runnable_serves_port"):
        # The plan-level fact: the fanout records the serve port in
        # the candidate's ports. The boot verify floors tcp:<port> +
        # probe:/ on that surface (vmf_verify), so ports — not a
        # checks list — is what the plan artifact honestly carries.
        port = (want_spec or {}).get("serve_port") or 0
        good("serve port in runnables",
             any(port in (a.get("ports") or []) for a in runnables),
             "port=%s" % port)
    if exp.get("runnable_keepalive_forbidden"):
        bad = [a.get("method") for a in runnables
               if vmf_plan._is_keepalive(
                   (a.get("direct") or {}).get("command")
                   or a.get("command") or [])]
        good("no keep-alive runnables", not bad, ",".join(bad))
    for b in exp.get("blocked_contains") or []:
        m, frag = b["method"], b["reason_contains"]
        good("blocked %s" % m, m in blocked and frag in blocked[m],
             blocked.get(m, "absent"))
    fresh = re.findall(r"(\d+) llm call", err)
    n_fresh = int(fresh[-1]) if fresh else None
    results[rid]["fresh_llm"] = n_fresh
    if src_row:
        prev = results.get(src_row) or {}
        good("verdicts match repeat",
             prev.get("runnables") is not None
             and _verdicts(runnables, blocked)
             == _verdicts(prev.get("runnables") or [],
                          prev.get("blocked") or {}))
        if exp.get("fresh_model_calls") == 0:
            good("0 fresh calls", n_fresh == 0, "llm=%s" % n_fresh)


def main():
    fx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "fixtures", "acceptance.yaml")
    with open(fx) as f:
        doc = yaml.safe_load(f)
    results = {}
    npass = nfail = 0
    for row in doc["rows"]:
        grade_row(row, results)
        ok = results[row["id"]]["ok"]
        fails = [c for c in ok if not c[1]]
        npass += len(ok) - len(fails)
        nfail += len(fails)
        r = results[row["id"]]
        print("row %s: %s (%d checks, wall %.0fs, llm %s)"
              % (row["id"], "PASS" if not fails else "FAIL", len(ok),
                 r.get("wall") or 0,
                 r.get("fresh_llm") if r.get("fresh_llm") is not None
                 else "?"))
        for name, cond, detail in ok:
            print("   %s %s%s" % ("ok " if cond else "FAIL", name,
                                  (" — %s" % detail) if detail else ""))
    print("parity: %d ok, %d failed" % (npass, nfail))
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
