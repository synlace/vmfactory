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
#       The deadline default (180s) must outlast the app's own boot:
#       an OOM kill lands inside the window, becomes evidence, and
#       drives the memory revision (measured RSS + 1024 MB headroom).
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
import re
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
    # hostfwd lines: "proto bind hport gport" (bind-aware, matches
    # vmf_fwd_parse), "proto hport gport", or legacy "hport gport" tcp.
    # The check speaks against the PUBLISHED host port; the plan speaks
    # guest ports (intent publishes 1:1, compose may remap).
    fwd = {}
    try:
        for line in open(path):
            parts = line.split()
            if len(parts) == 4:
                fwd[int(parts[3])] = int(parts[2])
            elif len(parts) == 3:
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
    ports = plan.get("ports")
    if ports is None and plan.get("services"):
        # Compose-shaped plan: the primary service's declared guest
        # ports are the app's surface; tcp only (udp is not probed).
        primary = plan.get("primary")
        for s in plan.get("services") or []:
            if s.get("name") != primary:
                continue
            ports = [pp.get("host") for pp in s.get("ports") or []
                     if pp.get("proto", "tcp") != "udp"]
            break
    clamped = vmf_plan._clamp_ports(ports)
    for p in clamped:
        runnable.append(("tcp", p, {"tcp": {"port": p}}))
    # Lenient http probe on the first tcp surface (status < 400 covers
    # 200 and auth redirects) when the plan declared no checks of its
    # own. tcp alone proved shallow: a published port with a dying app
    # accepts the connection and dies after.
    probe_pool = [pp.get("host") if isinstance(pp, dict) else pp
                  for pp in (ports or [])
                  if not (isinstance(pp, dict) and pp.get("proto") == "udp")]
    probe_c = vmf_plan._clamp_ports(probe_pool)
    if not plan.get("checks") and probe_c:
        first = min(probe_c)
        runnable.append(("probe", first,
                         {"probe": {"port": first, "path": "/",
                                    "expect_status_max": 399}}))
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
    except OSError as e:
        return False, {"check": "tcp:%d" % port, "expected": "connect",
                       "actual": str(e)}
    # slirp accepts the host connection before the guest one, so a bare
    # connect proves only the publish. The guest refusing (dead app,
    # loopback bind) arrives as RST/EOF right after: read briefly and
    # fail on it. A silent open socket means the guest accepted.
    try:
        s.settimeout(1.5)
        data = s.recv(16)
        if data == b"":
            s.close()
            return False, {"check": "tcp:%d" % port, "expected": "connect",
                           "actual": "connection closed by guest "
                                     "(nothing listens on the guest side)"}
    except ConnectionResetError:
        s.close()
        return False, {"check": "tcp:%d" % port, "expected": "connect",
                       "actual": "connection reset by guest"}
    except OSError:
        pass
    s.close()
    return True, None


def check_probe(spec, fwd, _name):
    p = spec["probe"]
    hp = fwd.get(p["port"], p["port"])
    url = "http://127.0.0.1:%d%s" % (hp, p.get("path", "/"))
    want = p.get("expect_status")
    want_max = p.get("expect_status_max")
    try:
        r = urllib.request.urlopen(url, timeout=5)
        status, body = r.status, r.read(8192).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        body = (e.read(8192) or b"").decode("utf-8", "replace")
    except Exception as e:
        return False, {"check": "probe:%d" % p["port"],
                       "expected": str(want or want_max),
                       "actual": "http fetch failed: %s" % e}
    if want is not None:
        ok = status == want
        desc = str(want)
    elif want_max is not None:
        ok = 100 <= status <= want_max
        desc = "status 100..%d" % want_max
    else:
        ok = status == 200
        desc = "200"
    if ok and p.get("expect_contains"):
        ok = p["expect_contains"] in body
    ev = None
    if not ok:
        ev = {"check": "probe:%d" % p["port"],
              "expected": "%s%s" % (desc, (" containing %r"
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


CRASH_PATTERNS = (
    r"install\.sh FAILED", r"error: unrecognized arguments: .*",
    r"Traceback \(most recent call last\)",
    r"TypeError: ", r"ReferenceError: ",
    r"SyntaxError: .*", r"ModuleNotFoundError: .*",
    r"Cannot find module", r"npm error", r"ELIFECYCLE",
    r"Address already in use", r"permission denied",
    r"command not found: .*", r"not found: .*",
    r"user \S+ does not exist", r"No such file or directory",
    r"FATAL:", r"EADDRINUSE",
)


def crash_evidence(console):
    # A guest app that dies at boot leaves its error in the console log
    # before the power-down. The exact line is evidence: it tells the
    # revision what the app actually rejected (e.g. serve.py's usage
    # names the flags it accepts).
    if not console or not os.path.isfile(console):
        return None
    try:
        text = open(console, errors="replace").read()
    except OSError:
        return None
    last = None
    for line in text.splitlines():
        line = line.strip()
        for pat in CRASH_PATTERNS:
            m = re.search(pat, line)
            if m:
                last = line[:200]
                break
    if not last:
        return None
    return {"check": "app-alive", "expected": "app running",
            "actual": "app exited during boot: %s" % last}


def oom_evidence(console, plan):
    # A dead app leaves a kernel OOM report in the console log. Extract
    # the victim and the resident size; the revision needs both to size
    # the VM honestly (measured need, not a guess).
    if not console or not os.path.isfile(console):
        return None
    try:
        text = open(console, errors="replace").read()
    except OSError:
        return None
    m = re.search(
        r"Out of memory: Killed process \d+ \((\S+)\).*?"
        r"anon-rss:(\d+)kB", text, re.S)
    if not m:
        return None
    return {"check": "app-alive",
            "expected": "running within the verify deadline",
            "actual": "%s was OOM-killed (anon-rss %.1fGB); plan memory_mb was %d"
                      % (m.group(1), int(m.group(2)) / 1048576.0,
                         plan.get("memory_mb", 1024))}


def wait_ssh(name, deadline):
    # Readiness gate: the guest starts sshd before the install runs, so
    # ssh-up alone is not success — but a VM that never answers ssh is
    # unreachable and no revision can fix that from outside. A torn-down
    # VM (conf gone) is provably dead: stop polling.
    while time.time() < deadline:
        rc, _, err = vmf_llm.run(exec_transport(
            name, "echo vmf-verify-ready"), timeout=20)
        if rc == 0:
            return True
        if "no running microVM" in (err or ""):
            return False
        time.sleep(POLL)
    return False


def write_evidence(path, evidence):
    # The VM's teardown may remove the rundir mid-verify (--rm runs);
    # recreate the parent so the evidence always lands.
    d = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return
    open(path, "w").write(json.dumps(evidence, indent=2))


def run_cmd(args):
    plan = json.load(open(args.plan))
    fwd = load_hostfwd(args.hostfwd) if args.hostfwd else {}
    runnable, skipped = derive_checks(plan, fwd)
    for c in skipped:
        verb = "log" if "log" in c else "rfb"
        port = c.get(verb, {}).get("port", "")
        print("check %s:%s SKIP (runner not built yet)" % (verb, port))
    if not runnable:
        # A plan that declares nothing cannot be replay-verified: "pass"
        # would be vacuous (the race would crown an unverifiable
        # candidate). Honest exit: unverified, not passed.
        print("verify: no runnable checks (plan declares no ports)")
        print("verdict: unverified (nothing to verify)")
        return 2
    deadline = time.time() + args.deadline
    print("verify: %d checks, deadline %ds" % (len(runnable), args.deadline))
    if not wait_ssh(args.name, deadline):
        ev = oom_evidence(args.console, plan)
        if not ev:
            ev = crash_evidence(args.console)
        if ev:
            evidence = [ev]
            if args.evidence_out:
                write_evidence(args.evidence_out, evidence)
            print("verdict: %s" % ev["actual"])
            return 1
        print("verify: ssh never came up; no verdict possible")
        return 2
    results = [False] * len(runnable)
    failed = {}
    last_progress = time.time()
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
        if time.time() - last_progress >= 30:
            waiting = ", ".join(spec_key(k, p, s)
                                for (k, p, s), r in zip(runnable, results)
                                if not r)
            print("verify: waiting (%ds/%ds): %s"
                  % (int(deadline - time.time()), args.deadline, waiting))
            last_progress = time.time()
        time.sleep(POLL)
    evidence = list(failed.values())
    ev = oom_evidence(args.console, plan)
    if not ev:
        ev = crash_evidence(args.console)
    if ev:
        evidence.append(ev)
        print("verify: %s (see console log)" % ev["actual"])
    passed = sum(results)
    if evidence:
        if args.evidence_out:
            write_evidence(args.evidence_out, evidence)
    skipped_note = " (%d skipped)" % len(skipped) if skipped else ""
    if passed == len(runnable):
        print("verdict: %d/%d checks pass%s"
              % (passed, len(runnable), skipped_note))
        if args.target_out:
            url = render_target(runnable, fwd)
            if url:
                try:
                    open(args.target_out, "w").write(url + "\n")
                except OSError:
                    pass
        return 0
    print("verdict: %d/%d checks pass%s; %d failed"
          % (passed, len(runnable), skipped_note,
             len(runnable) - passed))
    return 1


def render_target(runnable, fwd):
    # The user-facing deliverable: the first web surface with its
    # PUBLISHED host port (the bump is visible, never a surprise).
    for kind, _port, spec in runnable:
        if kind != "probe":
            continue
        p = spec["probe"]
        hp = fwd.get(p["port"], p["port"])
        return "http://127.0.0.1:%d%s" % (hp, p.get("path", "/"))
    for kind, _port, spec in runnable:
        if kind != "tcp":
            continue
        hp = fwd.get(spec["tcp"]["port"], spec["tcp"]["port"])
        return "tcp://127.0.0.1:%d" % hp
    return None


def spec_key(kind, port, spec):
    if kind == "tcp":
        return "tcp:%d" % port
    if kind == "probe":
        p = spec["probe"]
        return "probe:%d%s" % (p["port"], p.get("path", "/"))
    return "exec:%s" % spec["exec"]["cmd"][:40]


def oom_floor_mb(evidence):
    # Deterministic memory enforcement: when the evidence records an
    # OOM kill, the revised VM needs the measured resident size plus
    # 1024 MB of headroom (the model proposes app fixes; the driver
    # owns this arithmetic). Rounded up to whole GB, capped by the
    # _clamp_memory ceiling.
    for ev in evidence or []:
        m = re.search(r"anon-rss (\d+\.?\d*)GB",
                      str(ev.get("actual", "")))
        if m:
            rss_gb = int(float(m.group(1)) + 0.999)
            return (rss_gb + 1) * 1024
    return 0


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
        "guest tcp port the revised app listens on. When the evidence "
        "shows the app was OOM-killed, raise memory_mb to fit the "
        "measured resident size plus 1024 MB of headroom (at most 8192). "
        "If the grounded facts name an official container image for the "
        "app, prefer it (needs_docker=true and images=[ref]) over "
        "rebuilding from source. Do not weaken a "
        "check to match the observed failure unless the evidence proves "
        "the check itself was wrong; prefer fixing the app config. "
        "Reply with ONE JSON object, same shape:\n"
        '{"install": [...], "command": [...], "ports": [...], '
        '"checks": [{"probe": {"port": N, "path": "/", '
        '"expect_status": 200, "expect_contains": "..."}}], '
        '"images": ["<container images the plan needs>"], '
        '"env": {"K": "V"}, "needs_docker": <bool>, '
        '"memory_mb": <int>, "notes": "<max 12 words>"}'
        % (json.dumps(plan, indent=2), json.dumps(evidence, indent=2)))
    # Grounding for the revise: the doc facts (version matrices,
    # official images) sit beside the console evidence — the revise
    # stops re-picking the incompatible version the traceback names.
    facts = ""
    if args.image and args.phrase:
        try:
            grounded, c7_ids = vmf_plan.ground_for_app(args.image,
                                                       args.phrase)
            if grounded.strip():
                facts = ("Grounded facts from context7 (authoritative — "
                         "supported versions, official images):\n%s\n"
                         % grounded)
                sys.stderr.write("verify: revise grounded %s\n"
                                 % vmf_llm.grounding_note(c7_ids))
        except Exception as e:
            sys.stderr.write("verify: revise grounding unavailable (%s)\n"
                             % e)
    prompt = facts + prompt
    rc, o, err = vmf_llm.llm_call("intent", prompt, timeout=200,
                                  env={"VMF_LLM_TIMEOUT": "180"})
    if rc != 0:
        # One retry: a stalled stream (curl 28 with a partial body) is a
        # provider hiccup, not a missing model — the evidence is worth
        # one more attempt before the verdict stands.
        rc, o, err = vmf_llm.llm_call("intent", prompt, timeout=200,
                                      env={"VMF_LLM_TIMEOUT": "180"})
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
               "images": vmf_plan._clamp_images(j.get("images")),
               "env": {str(k): str(v)
                       for k, v in (j.get("env") or {}).items()},
               "needs_docker": bool(j.get("needs_docker")),
               "memory_mb": vmf_plan._clamp_memory(
                   j.get("memory_mb") or plan.get("memory_mb"),
                   j.get("needs_docker", plan.get("needs_docker"))),
               "notes": j.get("notes", "")}
    floor = oom_floor_mb(evidence)
    if floor and revised["memory_mb"] < floor:
        sys.stderr.write("verify: memory floor %d MB from the measured "
                         "RSS (plan proposed %d)\n"
                         % (floor, revised["memory_mb"]))
        revised["memory_mb"] = floor
        revised["memory_mb"] = vmf_plan._clamp_memory(
            revised["memory_mb"], revised["needs_docker"])
        revised["notes"] = (revised["notes"] or "")[:60]
    note = vmf_plan._diff_note(plan, revised)
    text = json.dumps(revised, indent=2)
    sys.stderr.write("verify: revised proposal\n%s\n%s\n"
                     % (text, ("\n" + note) if note else ""))
    if not vmf_llm.gate("verify: apply the revised plan? [y/N] "):
        sys.stderr.write("verify: revision declined; the failed "
                         "verdict stands\n")
        return 2
    d = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(d, exist_ok=True)
    open(args.out, "w").write(text)
    # Self-healing cache: the same (image, phrase) or the same gap-fill
    # cache replays the revised plan instead of the one that failed.
    gen = args.cache
    if not gen and args.image and args.phrase:
        gen = vmf_plan.intent_cache_dir(args.image, args.phrase)
    if gen:
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
    r.add_argument("--console")
    r.add_argument("--deadline", type=int,
                   default=int(os.environ.get("VMF_VERIFY_SECS", "300")))
    r.add_argument("--evidence-out")
    r.add_argument("--target-out")
    v = sub.add_parser("revise")
    v.add_argument("plan")
    v.add_argument("out")
    v.add_argument("--evidence", default="-")
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
