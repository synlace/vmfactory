#!/usr/bin/env python3
# vmf_verify.py — the verify runner: plan checks run against the booted
# VM, evidence feeds back, and a failed verdict triggers one bounded
# plan revision. This is the "up but broken" detector: the VM boots,
# the ports publish, and these checks prove the app actually serves.
#
# Commands:
#   run <plan.json> --name N --hostfwd F [--deadline S] [--evidence-out P]
#       Runs every runnable check until it passes or the deadline
#       expires. Exit 0 all pass · 1 failures · 2 ssh never came up.
#       log/rfb checks are skipped with a note (their runners arrive
#       with webvnc and the compose-log plumbing).
#   revise <plan.json> <out.json> --image I --phrase P [--cache DIR]
#       Feeds the failed-check evidence to the model, validates the
#       revised plan against the same bounded vocabulary, gates it
#       (VMF_RUN_YES or the terminal), then writes it beside the run
#       and back into the intent cache. Exit 0 revised · 1 model or
#       validation failure · 2 declined.
#
# Check derivation (direct plans): every declared guest port gets a
# deterministic tcp check — a declared fact, never a model opinion. The
# model's own checks[] (probe/exec) carry the success definition.
import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vmf_llm
import vmf_plan

POLL = 2.0
EXEC_TIMEOUT = int(os.environ.get("VMF_VERIFY_EXEC_TIMEOUT", "30"))


def exec_transport(name, cmd):
    # One-shot exec inside the guest. Default: scripts/ssh.sh batch
    # mode. VMF_VERIFY_SSH overrides with a shell template ({cmd} is
    # substituted shell-quoted) — the test seam.
    override = os.environ.get("VMF_VERIFY_SSH")
    if override:
        return ["bash", "-c", override.replace("{cmd}", shlex.quote(cmd))]
    return ["bash", os.path.join(scripts(), "ssh.sh"), name, "--", cmd]


def scripts():
    return os.environ.get("VMF_SCRIPTS_DIR") or \
        os.path.dirname(os.path.abspath(__file__))


def load_hostfwd(path):
    # hostfwd lines: "proto hport gport" (or legacy "hport gport" tcp).
    # The check speaks against the PUBLISHED host port; the plan speaks
    # guest ports (intent publishes 1:1, compose may remap).
    fwd = {}
    try:
        for line in open(path):
            parts = line.split()
            if len(parts) == 3:
                fwd[int(parts[2])] = int(parts[1])
            elif len(parts) == 2:
                fwd[int(parts[1])] = int(parts[0])
    except OSError:
        pass
    return fwd


def derive_checks(plan, fwd):
    # tcp for every declared port (deterministic) + the model's
    # probe/exec checks (clamped). Returns (runnable, skipped) where a
    # runnable check is (kind, port_or_cmd, spec_dict).
    runnable, skipped = [], []
    for p in vmf_plan._clamp_ports(plan.get("ports")):
        runnable.append(("tcp", p, {"tcp": {"port": p}}))
    for c in vmf_plan._clamp_checks(plan.get("checks")):
        if "probe" in c:
            runnable.append(("probe", c["probe"]["port"], c))
        elif "exec" in c:
            runnable.append(("exec", None, c))
    for c in plan.get("checks") or []:
        if isinstance(c, dict) and ("log" in c or "rfb" in c):
            skipped.append(c)
    # The model may also have declared a tcp check; declared ports
    # already carry one, so anything else is a duplicate — drop it.
    return runnable, skipped


def check_tcp(port, fwd, _spec):
    hp = fwd.get(port, port)
    try:
        s = socket.create_connection(("127.0.0.1", hp), timeout=2)
        s.close()
        return True, None
    except OSError as e:
        return False, {"check": "tcp:%d" % port, "expected": "connect",
                       "actual": str(e)}


def check_probe(spec, fwd, _name):
    p = spec["probe"]
    hp = fwd.get(p["port"], p["port"])
    url = "http://127.0.0.1:%d%s" % (hp, p.get("path", "/"))
    want = p.get("expect_status", 200)
    try:
        r = urllib.request.urlopen(url, timeout=5)
        status, body = r.status, r.read(8192).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        body = (e.read(8192) or b"").decode("utf-8", "replace")
    except Exception as e:
        return False, {"check": "probe:%d" % p["port"], "expected": str(want),
                       "actual": "http fetch failed: %s" % e}
    ok = status == want
    if ok and p.get("expect_contains"):
        ok = p["expect_contains"] in body
    ev = None
    if not ok:
        ev = {"check": "probe:%d" % p["port"],
              "expected": "%s%s" % (want, (" containing %r"
                                           % p["expect_contains"])
                                    if p.get("expect_contains") else ""),
              "actual": "status %d; body: %s" % (status, body[:120])}
    return ok, ev


def check_exec(cmd, name, spec):
    try:
        p = subprocess.run(exec_transport(name, cmd), capture_output=True,
                           text=True, timeout=EXEC_TIMEOUT)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return False, {"check": "exec", "expected": "exit 0 within %ds"
                       % EXEC_TIMEOUT, "actual": "timeout"}
    if rc == 0:
        return True, None
    return False, {"check": "exec", "expected": "exit 0",
                   "actual": "exit %d: %s" % (rc,
                                              ((out + err).strip()[:200]))}


def wait_ssh(name, deadline):
    # Readiness gate: the guest starts sshd before the install runs, so
    # ssh-up alone is not success — but a VM that never answers ssh is
    # unreachable and no revision can fix that from outside.
    while time.time() < deadline:
        rc, _, _ = vmf_llm.run(exec_transport(name, "echo vmf-verify-ready"),
                               timeout=20)
        if rc == 0:
            return True
        time.sleep(POLL)
    return False


def run_cmd(args):
    plan = json.load(open(args.plan))
    fwd = load_hostfwd(args.hostfwd) if args.hostfwd else {}
    runnable, skipped = derive_checks(plan, fwd)
    for c in skipped:
        verb = "log" if "log" in c else "rfb"
        port = c.get(verb, {}).get("port", "")
        print("check %s:%s SKIP (runner not built yet)" % (verb, port))
    if not runnable:
        print("verify: no runnable checks (plan declares no ports)")
        print("verdict: 0/0 checks pass (nothing to verify)")
        return 0
    deadline = time.time() + args.deadline
    print("verify: %d checks, deadline %ds" % (len(runnable), args.deadline))
    if not wait_ssh(args.name, deadline):
        print("verify: ssh never came up; no verdict possible")
        return 2
    results = [False] * len(runnable)
    failed = {}
    while time.time() < deadline:
        for i, (kind, port, spec) in enumerate(runnable):
            if results[i]:
                continue
            if kind == "tcp":
                ok, ev = check_tcp(port, fwd, spec)
            elif kind == "probe":
                ok, ev = check_probe(spec, fwd, args.name)
            else:
                ok, ev = check_exec(spec["exec"]["cmd"], args.name, spec)
            label = spec_key(kind, port, spec)
            if ok:
                results[i] = True
                print("check %s pass" % label)
            elif ev:
                ev["check"] = label
                if label not in failed:
                    print("check %s FAIL expected %s, got %s"
                          % (label, ev["expected"], ev["actual"]))
                failed[label] = ev
        if all(results):
            break
        time.sleep(POLL)
    evidence = list(failed.values())
    passed = sum(results)
    if evidence:
        out = json.dumps(evidence, indent=2)
        if args.evidence_out:
            open(args.evidence_out, "w").write(out)
    skipped_note = " (%d skipped)" % len(skipped) if skipped else ""
    if passed == len(runnable):
        print("verdict: %d/%d checks pass%s"
              % (passed, len(runnable), skipped_note))
        return 0
    print("verdict: %d/%d checks pass%s; %d failed"
          % (passed, len(runnable), skipped_note,
             len(runnable) - passed))
    return 1


def spec_key(kind, port, spec):
    if kind == "tcp":
        return "tcp:%d" % port
    if kind == "probe":
        p = spec["probe"]
        return "probe:%d%s" % (p["port"], p.get("path", "/"))
    return "exec:%s" % spec["exec"]["cmd"][:40]


def revise_cmd(args):
    plan = json.load(open(args.plan))
    try:
        evidence = json.load(open(args.evidence))
    except (OSError, ValueError):
        evidence = []
    if not evidence:
        print("verify: no failure evidence; nothing to revise", file=sys.stderr)
        return 1
    prompt = (
        "A disposable microVM plan failed its post-boot checks. The VM "
        "boots and the declared ports publish, but the app does not "
        "serve what the checks expect.\n"
        "Current plan (JSON):\n%s\n"
        "Failed-check evidence (JSON):\n%s\n"
        "Revise the plan so the checks pass. Rules: keep the same base "
        "image family; the install commands run on a FRESH VM from the "
        "same base image, so make them complete and idempotent; keep "
        "ports unless the evidence proves they are wrong; declare every "
        "guest tcp port the revised app listens on. Do not weaken a "
        "check to match the observed failure unless the evidence proves "
        "the check itself was wrong; prefer fixing the app config. "
        "Reply with ONE JSON object, same shape:\n"
        '{"install": [...], "command": [...], "ports": [...], '
        '"checks": [{"probe": {"port": N, "path": "/", '
        '"expect_status": 200, "expect_contains": "..."}}], '
        '"env": {"K": "V"}, "needs_docker": <bool>, '
        '"memory_mb": <int>, "notes": "<max 12 words>"}'
        % (json.dumps(plan, indent=2), json.dumps(evidence, indent=2)))
    rc, o, err = vmf_llm.llm_call("intent", prompt)
    if rc != 0:
        sys.stderr.write(err or "")
        sys.stderr.write("error: verify revision needs a reachable model\n")
        return 1
    try:
        j = vmf_llm.parse_llm_json(o)
    except Exception:
        sys.stderr.write("error: revision returned unparseable JSON:\n%s\n"
                         % o[:400])
        return 1
    install = [str(x) for x in (j.get("install") or [])][:20]
    cmd = [str(x) for x in (j.get("command") or [])][:16]
    if not cmd:
        sys.stderr.write("error: the revision produced no command\n")
        return 1
    revised = {"base_image": plan.get("base_image", ""),
               "install": install,
               "command": cmd,
               "ports": vmf_plan._clamp_ports(j.get("ports")),
               "checks": vmf_plan._clamp_checks(j.get("checks")),
               "env": {str(k): str(v)
                       for k, v in (j.get("env") or {}).items()},
               "needs_docker": bool(j.get("needs_docker")),
               "memory_mb": int(j.get("memory_mb")
                                or plan.get("memory_mb") or 1024),
               "notes": j.get("notes", "")}
    note = vmf_plan._diff_note(plan, revised)
    text = json.dumps(revised, indent=2)
    sys.stderr.write("verify: revised proposal\n%s\n%s\n"
                     % (text, ("\n" + note) if note else ""))
    if not vmf_llm.gate("verify: apply the revised plan? [y/N] "):
        sys.stderr.write("verify: revision declined; the failed "
                         "verdict stands\n")
        return 2
    open(args.out, "w").write(text)
    if args.image and args.phrase:
        # Self-healing cache: the same (image, phrase) replays the
        # revised plan instead of the one that failed its checks.
        gen = args.cache or vmf_plan.intent_cache_dir(args.image, args.phrase)
        try:
            os.makedirs(gen, exist_ok=True)
            open(os.path.join(gen, "direct.json"), "w").write(text)
            meta_path = os.path.join(gen, "direct.json.meta.json")
            meta = {}
            if os.path.isfile(meta_path):
                try:
                    meta = json.load(open(meta_path))
                except ValueError:
                    meta = {}
            meta["verify_revised"] = True
            open(meta_path, "w").write(json.dumps(meta, indent=2))
            sys.stderr.write("verify: revised plan cached %s\n" % gen)
        except OSError as e:
            sys.stderr.write("warning: cache write failed: %s\n" % e)
    return 0


def main(argv):
    ap = argparse.ArgumentParser(add_help=False)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("plan")
    r.add_argument("--name", required=True)
    r.add_argument("--hostfwd")
    r.add_argument("--deadline", type=int,
                   default=int(os.environ.get("VMF_VERIFY_SECS", "120")))
    r.add_argument("--evidence-out")
    v = sub.add_parser("revise")
    v.add_argument("plan")
    v.add_argument("out")
    v.add_argument("evidence", nargs="?", default="-")
    v.add_argument("--image")
    v.add_argument("--phrase")
    v.add_argument("--cache")
    a = ap.parse_args(argv[1:])
    if a.cmd == "run":
        return run_cmd(a)
    if a.cmd == "revise":
        a.evidence = a.evidence if a.evidence != "-" else \
            os.path.join(os.path.dirname(os.path.abspath(a.plan)),
                         "verify-evidence.json")
        return revise_cmd(a)
    ap.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
