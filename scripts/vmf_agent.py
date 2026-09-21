#!/usr/bin/env python3
# vmf_agent.py — the agent session: drives a booted VM over ssh until the
# app works, then writes back a demonstrated spec.
#
# The inversion: instead of "model predicts a plan, boot, verify, revise
# blind", the agent works against the RUNNING system (install, start,
# curl, read the real errors) and its spec records what demonstrably
# worked. Every later run replays that spec deterministically; the
# verify stage is the acceptance test for the replay.
#
# usage: vmf_agent.py --vm NAME --image IMAGE --phrase PHRASE --out PATH
#                     [--turns N] [--budget S]
#
# Protocol: each turn the model replies ONE JSON object:
#   {"thought": "<one line>", "cmd": "<shell or null>",
#    "done": <bool>, "plan": {direct plan or null}}
# A shell command runs over ssh (output capped, both streams); "done"
# triggers the in-guest acceptance checks; failures feed back as
# evidence. Exit 0 spec written · 1 budget exhausted or VM lost ·
# 2 not approved · 3 no agent model.
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vmf_llm
import vmf_plan
import vmf_verify

CMD_TIMEOUT = 180
OUT_CAP = 3500
TRANSCRIPT_CAP = 12
# Infra-symptom watchdog: transport plumbing (TLS, proxies, DNS, CAs)
# belongs to the deterministic substrate, not the agent's turn budget.
# Repeated diagnostic turns on the same plumbing abort the session and
# hand the work to the honest fallback path.
INFRA_DIAG = re.compile(
    r"openssl|s_client|getent|_PROXY|certificate|ca-cert|registry-1"
    r"|tls:|SSL|no-curl|command -v", re.I)


def infra_diagnostic(cmd, out):
    return bool(INFRA_DIAG.search("%s %s" % (cmd, out)[:4000]))

SYSTEM_BRIEF = (
    "You drive a disposable microVM over ssh one-shot commands to make an "
    "app serve, then write back the exact recipe. Facts: you are root; "
    "the VM has a fresh base image, no plan yet; the app only needs to "
    "serve IN THE GUEST (host publishing happens on the replay boot). "
    "Batch work: one turn = one complete phase (a full script), not one "
    "micro-command; aim for under 6 turns. "
    "dockerd is ALREADY running in the VM (docker pull/run works, `docker` "
    "is on PATH). Prefer the host's image supply over in-VM pulls: to "
    "use an official image reply ONCE with "
    '{"thought":"...","cmd":null,"seed_images":["ghost:5"],"done":false}'
    " — the host pulls it with its own trust and loads it into this "
    "VM's docker while you think; then docker run it. Never pull the "
    "base image this VM already runs (e.g. you are inside ubuntu); go "
    "straight to the app's own official image, and list every image "
    "the plan needs in plan.images so the replay boot preloads them. "
    "Speed rules: prefer an official image "
    "or the app's release artifact (GitHub releases); "
    "build from source only when neither exists. If the plan's install "
    "or command uses "
    "docker, set needs_docker=true so the replay boot starts dockerd. "
    "Guest tools are minimal: /vmf/busybox provides coreutils + wget + "
    "httpd; install what the app needs. Make installs idempotent (they "
    "re-run on a fresh VM). When the app serves, reply "
    "done=true with the plan:\n"
    "or the app's release artifact (GitHub releases); build from source "
    "only when neither exists. If the plan's install or command uses "
    "docker, set needs_docker=true so the replay boot starts dockerd. "
    "Guest "
    "tools are minimal: /vmf/busybox provides coreutils + wget + httpd; "
    "install what the app needs. Make installs idempotent (they re-run "
    "on a fresh VM). When the app serves, reply "
    "done=true with the plan:\n"
    '{"install": ["<shell, idempotent, run once at boot>"], '
    '"command": ["<argv>"], "ports": [<guest tcp ports>], '
    '"checks": [{"probe": {"port": P, "path": "/", "expect_status": 200, '
    '"expect_contains": "<text that proves it>"}}], '
    '"images": ["<container images the plan needs>"], '
    '"env": {"K": "V"}, "needs_docker": <bool>, '
    '"memory_mb": <measured need, 1024-8192>, "notes": "<max 12 words>"}\n'
    "checks must reproduce what you verified (status and a distinctive "
    "body substring). Reply with ONE JSON object only.")


def show_cmd(cmd):
    # Terminal display: multi-line commands shown in full (bounded),
    # so the session reads like a transcript, not a cut-off.
    shown = str(cmd)
    if len(shown) > 400:
        shown = shown[:400] + " …(+%d chars)" % (len(shown) - 400)
    print("agent: $ %s" % shown)


def show_result(rc, out, err):
    lines = [l for l in ((out or "") + (err or "")).strip().splitlines()
             if l.strip()]
    head = lines[0][:100] if lines else ""
    tail = (" (%d lines)" % len(lines)) if len(lines) > 1 else ""
    print("agent:   → rc=%d %s%s" % (rc, head, tail if head else
                                     "(no output)"))


def scripts():
    return os.environ.get("VMF_SCRIPTS_DIR") or \
        os.path.dirname(os.path.abspath(__file__))


def ssh_exec(name, cmd, timeout=CMD_TIMEOUT):
    # One-shot exec in the guest. Default: scripts/ssh.sh batch mode.
    # VMF_AGENT_SSH overrides with a shell template ({cmd} is substituted
    # shell-quoted) — the test seam.
    override = os.environ.get("VMF_AGENT_SSH")
    if override:
        full = ["bash", "-c", override.replace("{cmd}", shlex.quote(cmd))]
    else:
        full = ["bash", os.path.join(scripts(), "ssh.sh"), name, "--", cmd]
    try:
        p = subprocess.run(full, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 99, "", "timeout"


def vm_alive(name):
    rc, _, _ = ssh_exec(name, "echo alive", timeout=20)
    return rc == 0


def guest_tcp_listening(name, port):
    # /proc/net/tcp LISTEN state for a guest port — shell-independent,
    # no nc/curl needed; h2d avoids gawk-only strtonum.
    script = ("/vmf/busybox awk 'function h2d(h,   n, i) { n = 0; "
              "for (i = 1; i <= length(h); i++) n = n * 16 + "
              "index(\"0123456789abcdef\", tolower(substr(h, i, 1))) - 1; "
              "return n } "
              "$4 == \"0A\" { split($2, a, \":\"); "
              "if (h2d(a[2]) == %d) { found = 1 } } "
              "END { exit found ? 0 : 1 }' /proc/net/tcp /proc/net/tcp6 "
              "2>/dev/null" % port)
    rc, _, _ = ssh_exec(name, script, timeout=30)
    return rc == 0


def guest_probe(name, port, path, want, contains):
    # busybox wget: -S writes the status line to stderr; the body to
    # stdout. Status parse from the first HTTP/ line.
    url = "http://127.0.0.1:%d%s" % (port, path or "/")
    rc, out, err = ssh_exec(name, "/vmf/busybox wget -S -q -O - '%s'" % url,
                            timeout=45)
    status = None
    for line in (err or "").splitlines():
        if "HTTP/" in line:
            try:
                status = int(line.strip().split()[1])
            except (IndexError, ValueError):
                pass
            break
    body = out or ""
    if status != want:
        return False, ("probe %s -> status %s (wanted %s)"
                       % (url, status, want))
    if contains and contains not in body:
        return False, ("probe %s -> %r not in body" % (url, contains[:60]))
    return True, None


def guest_acceptance(name, plan):
    # Deterministic in-guest replay of the plan's checks: tcp via the
    # proc listener table, probe via busybox wget, exec verbatim.
    results, failures = [], []
    for p in vmf_plan._clamp_ports(plan.get("ports")):
        ok = guest_tcp_listening(name, p)
        results.append(ok)
        if not ok:
            failures.append("tcp %d: no listener in the guest" % p)
    for c in vmf_plan._clamp_checks(plan.get("checks")):
        if "probe" in c:
            p = c["probe"]
            ok, msg = guest_probe(name, p["port"], p.get("path", "/"),
                                  p.get("expect_status", 200),
                                  p.get("expect_contains"))
            results.append(ok)
            if not ok:
                failures.append(msg)
        elif "exec" in c:
            rc, out, err = ssh_exec(name, c["exec"]["cmd"], timeout=120)
            results.append(rc == 0)
            if rc != 0:
                failures.append("exec failed rc=%d: %s"
                                % (rc, ((out + err).strip()[:200])))
    return all(results) and bool(results), failures


def transcript_block(turns):
    lines = []
    for t in turns[-TRANSCRIPT_CAP:]:
        out = (t["out"] or "").strip()[:OUT_CAP]
        lines.append("$ %s\n%s" % (t["cmd"], out if out else "(no output)"))
    return "\n".join(lines)


def agent_turn(image, phrase, turns, note="", brief=None, facts=None):
    prompt = (
        (brief or SYSTEM_BRIEF) +
        ("Grounded facts from context7 (authoritative — supported "
         "versions, official images, ports override your priors):\n%s\n"
         % facts if facts else "") +
        "Base image: %s\nIntent: %s\n%s\n"
        "Command transcript so far (last %d):\n%s\n"
        "Reply with ONE JSON object: "
        '{"thought": "...", "cmd": "<shell or null>", "done": false, '
        '"plan": null} — or done=true with the plan filled in.'
        % (image, phrase, note, len(turns[-TRANSCRIPT_CAP:]),
           transcript_block(turns) or "(nothing yet)"))
    rc, o, err = vmf_llm.llm_call("agent", prompt, timeout=240,
                                  env={"VMF_LLM_TIMEOUT": "220"})
    if rc != 0:
        sys.stderr.write((err or "") + "\nerror: agent needs a reachable "
                                      "model (VMF_AGENT_MODEL)\n")
        return None
    try:
        return vmf_llm.parse_llm_json(o)
    except Exception:
        sys.stderr.write("error: agent reply unparseable:\n%s\n" % o[:400])
        return None


def validate_spec(raw, plan):
    install = [str(x) for x in (raw.get("install") or [])][:20]
    cmd = [str(x) for x in (raw.get("command") or [])][:16]
    if not cmd:
        return None
    return {"base_image": plan.get("base_image", ""),
            "install": install,
            "command": cmd,
            "ports": vmf_plan._clamp_ports(raw.get("ports")),
            "checks": vmf_plan._clamp_checks(raw.get("checks")),
            "images": vmf_plan._clamp_images(raw.get("images")),
            "env": {str(k): str(v)
                    for k, v in (raw.get("env") or {}).items()},
            "needs_docker": bool(raw.get("needs_docker")),
            "memory_mb": vmf_plan._clamp_memory(raw.get("memory_mb"),
                                                raw.get("needs_docker")),
            "notes": str(raw.get("notes") or "")[:80]}


def seed_images(args, refs):
    # Host-side supply: pull with the host's trust, pin TOFU, archive
    # into the agent VM's inputs, then `docker load` in the guest. The
    # guest never dials a registry.
    for i, ref in enumerate(refs[:4]):
        out = os.path.join(args.rundir, "images-seed-%d.tar" % i)
        override = os.environ.get("VMF_AGENT_PULL")
        if override:
            sh = override.replace("{ref}", shlex.quote(ref)) \
                .replace("{path}", shlex.quote(out))
            rc = subprocess.run(["bash", "-c", sh],
                                capture_output=True, text=True,
                                timeout=900).returncode
        else:
            try:
                rc = subprocess.run(
                    ["bash", os.path.join(scripts(), "image-supply.sh"),
                     ref, out], capture_output=True, text=True,
                    timeout=900).returncode
            except subprocess.TimeoutExpired:
                rc = 99
        if rc != 0:
            return False, "host supply failed for %s (rc=%d)" % (ref, rc)
        lrc, lo, le = ssh_exec(
            args.vm, "docker load -i /vmf-run/%s" % os.path.basename(out),
            timeout=300)
        if lrc != 0:
            return False, "guest load failed for %s: %s" % (
                ref, (lo + le).strip()[:200])
    return True, ""


def agent_model_configured():
    # llm.sh sources ~/.vmf/env at call time, so a process env var is
    # not the only way a model arrives — check both.
    for k in ("VMF_AGENT_MODEL", "VMF_GAPFILL_MODEL", "VMF_LLM_MODEL"):
        if os.environ.get(k):
            return True
    env_file = os.environ.get(
        "VMF_ENV_FILE",
        os.path.join(os.path.expanduser("~"), ".vmf", "env"))
    try:
        text = open(env_file, errors="replace").read()
    except OSError:
        return False
    for k in ("VMF_AGENT_MODEL", "VMF_GAPFILL_MODEL", "VMF_LLM_MODEL",
              "VMF_LLM_API_KEY", "OPENROUTER_API_KEY"):
        if re.search(r"^\s*(export\s+)?%s=" % k, text, re.M):
            return True
    return False


REPAIR_BRIEF = (
    "You REPAIR a RUNNING microVM whose app failed its post-boot checks. "
    "The VM is LIVE (ssh works) — fix the app in place, do not reboot. "
    "The current plan's install already ran on this VM. The failure "
    "evidence and the current plan are in the transcript below. Fix "
    "the app live (ssh commands) until the declared checks pass, then "
    "reply done with the FULL corrected plan: its install[] must ALSO "
    "reproduce the fix on a FRESH VM (idempotent — include the original "
    "install steps plus the repair steps), command[] must start the "
    "app. Batch work: one turn = one complete phase. Reply ONE JSON "
    "object per turn.\n")


def save_transcript(path, turns):
    try:
        json.dump(turns, open(path, "w"))
    except OSError:
        pass


def load_transcript(path):
    try:
        t = json.load(open(path))
        return t if isinstance(t, list) else []
    except (OSError, ValueError):
        return []


def agent_cmd(args):
    if not agent_model_configured():
        sys.stderr.write("agent: no agent model configured (VMF_AGENT_MODEL)\n")
        return 3
    deadline = time.time() + args.budget
    turns, note = [], ""
    if getattr(args, "resume", None) and os.path.isfile(args.resume):
        turns = load_transcript(args.resume)
        if turns:
            sys.stderr.write("agent: resumed %d prior turns\n" % len(turns))
    if getattr(args, "context", None) and os.path.isfile(args.context):
        ctx = json.load(open(args.context))
        turns.insert(0, {"cmd": "(current plan + failure evidence)",
                         "out": json.dumps(ctx)[:OUT_CAP * 2]})
    spec = None
    turn_no = 0
    infra_streak = 0
    seed_state = {"thread": None, "refs": [], "result": {}}
    # Grounding before the first turn: the doc facts (supported
    # versions, official images, ports) replace stale priors — the
    # Ghost/node class of failure becomes un-navigable.
    facts = ""
    try:
        grounded, c7_ids = vmf_plan.ground_for_app(args.image, args.phrase,
                                                   role="agent")
        if grounded.strip():
            facts = grounded
            sys.stderr.write("agent: grounded %s\n"
                             % vmf_llm.grounding_note(c7_ids))
    except Exception as e:
        sys.stderr.write("agent: grounding unavailable (%s); priors\n" % e)

    def persist():
        if getattr(args, "resume", None):
            save_transcript(args.resume, turns[-40:])

    def collect_seed():
        if seed_state["thread"] is not None:
            seed_state["thread"].join()
            seed_state["thread"] = None
            ok, msg = seed_state["result"].get("v", (True, ""))
            refs = ", ".join(seed_state["refs"])
            if ok:
                print("agent:   → images loaded: %s" % refs)
                turns.append({"cmd": "(host image supply)",
                              "out": "loaded %s" % refs})
            else:
                print("agent:   → supply failed: %s" % msg)
                turns.append({"cmd": "(host image supply)", "out": msg})
                return msg
        return ""

    while turn_no < args.turns and time.time() < deadline:
        turn_no += 1
        fail_note = collect_seed()
        if fail_note:
            note = fail_note
        reply = agent_turn(args.image, args.phrase, turns, note,
                           brief=REPAIR_BRIEF if getattr(
                               args, "repair", False) else None,
                           facts=facts or None)
        if reply is None:
            return 1
        persist()
        if reply.get("seed_images") and seed_state["thread"] is None:
            seed_state["refs"] = [str(x)
                                  for x in reply["seed_images"]][:4]
            if seed_state["refs"]:
                print("agent:   → host supplying %s (pulls while the "
                      "model thinks)" % ", ".join(seed_state["refs"]))

                def _seed():
                    seed_state["result"]["v"] = seed_images(
                        args, seed_state["refs"])
                seed_state["thread"] = threading.Thread(target=_seed)
                seed_state["thread"].start()
        cmd = reply.get("cmd")
        if cmd:
            if seed_state["thread"] is not None:
                fail_note = collect_seed()
                if fail_note:
                    note = fail_note
            rc, out, err = ssh_exec(args.vm, str(cmd)[:2000])
            turns.append({"cmd": str(cmd)[:300],
                          "out": ("rc=%d\n%s" % (rc, (out + err)[-OUT_CAP:]))})
            show_cmd(cmd)
            show_result(rc, out, err)
            persist()
            if infra_diagnostic(str(cmd), (out or "") + (err or "")):
                infra_streak += 1
            else:
                infra_streak = 0
            if infra_streak >= 3:
                sys.stderr.write(
                    "agent: %d consecutive plumbing turns (TLS/proxy/DNS "
                    "diagnostics); the substrate should own this — "
                    "aborting to the propose path\n" % infra_streak)
                return 1
            if not vm_alive(args.vm) and not reply.get("done"):
                sys.stderr.write("agent: VM lost (crash or OOM); aborting\n")
                return 1
        if reply.get("done"):
            raw_plan = reply.get("plan")
            if not isinstance(raw_plan, dict):
                note = ("Your done plan was not a JSON object (got %s). "
                        "Reply the plan as ONE object with install, "
                        "command, ports, checks, images, env, "
                        "needs_docker, memory_mb, notes."
                        % type(raw_plan).__name__)
                turns.append({"cmd": "(plan format)",
                              "out": note[:OUT_CAP]})
                print("agent:   → plan was %s, not an object; re-asking"
                      % type(raw_plan).__name__)
                continue
            spec = validate_spec(raw_plan, {})
            if spec is None:
                note = ("Your done had no command; a plan needs a "
                        "non-empty command array.")
                continue
            if not spec["install"] and sum(
                    1 for t in turns
                    if t.get("out", "").startswith("rc=0")) >= 2:
                note = ("Your install list is EMPTY, but this session "
                        "built state with shell commands (users, "
                        "packages, files). The replay VM starts FRESH "
                        "from the base image: move every state-building "
                        "step into install[] (idempotent), then reply "
                        "done again.")
                turns.append({"cmd": "(replay check)",
                              "out": note[:OUT_CAP]})
                print("agent:   → empty install cannot replay; re-asking")
                continue
            runnable_ports = vmf_plan._clamp_ports(raw_plan.get("ports"))
            runnable_checks = vmf_plan._clamp_checks(raw_plan.get("checks"))
            if not runnable_ports and not runnable_checks:
                note = ("The plan declares no ports and no checks; the "
                        "acceptance has nothing to verify. Declare the "
                        "guest tcp port(s) the app serves and one probe "
                        "per HTTP port (status + distinctive body text), "
                        "then reply done again.")
                turns.append({"cmd": "(acceptance)",
                              "out": note[:OUT_CAP]})
                print("agent:   → plan declares no checks; re-asking")
                continue
            ok, failures = guest_acceptance(args.vm, spec)
            if ok:
                break
            note = ("The deterministic in-guest checks FAILED:\n- %s\n"
                    "Fix the app live, then redeclare done with the plan."
                    % "\n- ".join(failures[:5]))
            turns.append({"cmd": "(acceptance checks)",
                          "out": note[:OUT_CAP]})
            print("agent: checks failed (%d); iterating" % len(failures))
            spec = None
        else:
            note = ""
    if spec is None:
        sys.stderr.write("agent: budget exhausted without a working spec\n")
        return 1
    # The gate: a = cache and finish, n = abort, free text = one more
    # agent instruction (capped at 2 extra rounds).
    extra = 0
    while True:
        text = json.dumps(spec, indent=2)
        sys.stderr.write("%s\n" % vmf_plan.proposal_head(text))
        if vmf_llm.accepted():
            break
        line = vmf_llm.tty_line(
            "agent: refine? [a = accept, n = abort, or type an instruction] ")
        if line is None or line == "" or line.lower() in ("n", "no"):
            sys.stderr.write("agent: not approved\n")
            return 2
        if line.lower() in ("a", "y", "yes", "proceed"):
            break
        if extra >= 2:
            sys.stderr.write("agent: refinement cap reached; keeping\n")
            break
        extra += 1
        turns.append({"cmd": "(user instruction)", "out": line})
        reply = agent_turn(args.image, args.phrase, turns,
                           "The user instructs: %s\nApply it live "
                           "(run commands), then reply done with the "
                           "updated plan." % line,
                           facts=facts or None)
        if reply is None:
            return 1
        if reply.get("cmd"):
            rc, out, err = ssh_exec(args.vm, str(reply["cmd"])[:2000])
            turns.append({"cmd": str(reply["cmd"])[:300],
                          "out": ("rc=%d\n%s" % (rc, (out + err)[-OUT_CAP:]))})
            show_cmd(reply["cmd"])
            show_result(rc, out, err)
        if reply.get("done"):
            cand = validate_spec(reply.get("plan") or {}, spec)
            if cand is not None:
                ok, failures = guest_acceptance(args.vm, cand)
                if ok:
                    spec = cand
                    continue
                note = ("checks failed:\n- %s" % "\n- ".join(failures[:5]))
                turns.append({"cmd": "(acceptance)", "out": note[:OUT_CAP]})
                continue
        sys.stderr.write("agent: instruction round did not yield a "
                         "verified plan; keeping the last spec\n")
        break
    vmf_verify.write_evidence(args.out, spec)
    # The cache is the payoff: the same (image, phrase) replays this
    # demonstrated spec deterministically instead of paying for a new
    # session.
    gen = vmf_plan.intent_cache_dir(args.image, args.phrase)
    try:
        os.makedirs(gen, exist_ok=True)
        open(os.path.join(gen, "direct.json"), "w").write(
            json.dumps(spec, indent=2))
        open(os.path.join(gen, "direct.json.meta.json"), "w").write(
            json.dumps({"model": os.environ.get("VMF_AGENT_MODEL", ""),
                        "intent": args.phrase, "agent": True,
                        "created": ""}, indent=2))
        sys.stderr.write("agent: spec cached %s\n" % gen)
    except OSError as e:
        sys.stderr.write("warning: cache write failed: %s\n" % e)
    sys.stderr.write("agent: spec written %s\n" % args.out)
    return 0


def main(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--vm", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--phrase", required=True)
    ap.add_argument("--rundir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--turns", type=int,
                    default=int(os.environ.get("VMF_AGENT_TURNS", "16")))
    ap.add_argument("--budget", type=int,
                    default=int(os.environ.get("VMF_AGENT_BUDGET", "900")))
    ap.add_argument("--repair", action="store_true")
    ap.add_argument("--context")
    ap.add_argument("--resume")
    a = ap.parse_args(argv[1:])
    return agent_cmd(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
