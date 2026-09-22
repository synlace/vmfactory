#!/usr/bin/env python3
# vmf_race.py — the staggered install-approach race.
#
# usage: vmf_race.py <src>
#
# The enumeration (vmf_plan.py enumerate) supplies the approach table.
# Each candidate materializes as an oci-run invocation:
#   compose         oci-run.sh <src>            (real repo; compose found)
#   dockerfile      oci-run.sh <tmpdir>         (synthetic 1-service compose)
#   prebuilt_image  oci-run.sh <tmpdir>         (synthetic compose, image ref)
#   source_build    oci-run.sh <src> + VMF_PLAN_SKIP_COMPOSE=1 (gapfill direct)
# Candidates boot detached with ssh; the verify stage writes
# $RUNS_DIR/<name>.verdict. First pass wins; losers reaped; the winner
# reboots under the canonical name from the shared content-keyed data
# drive.
#
# Env: VMF_RACE_MODE=plan  — table only, boot nothing (exit 0/1)
#      VMF_RACE_APPROACH / VMF_RACE_SKIP — name or number filters (csv)
#      VMF_RACE_STAGGER (90) VMF_RACE_PARALLEL (2) VMF_RACE_DEADLINE (900)
#      VMF_RACE_CHILD=1    — candidate runners must not re-enter the race
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

RUNS = os.environ.get("VMF_RUNS") or os.path.expanduser("~/.vmf/runs")
SCRIPTS = os.environ.get("VMF_SCRIPTS_DIR") or os.path.dirname(
    os.path.abspath(__file__))
STAGGER = int(os.environ.get("VMF_RACE_STAGGER") or 90)
PARALLEL = int(os.environ.get("VMF_RACE_PARALLEL") or 2)
DEADLINE = int(os.environ.get("VMF_RACE_DEADLINE") or 900)


def _plan_interp():
    # vmf_plan's yaml import is a runtime-optional dependency (pyyaml);
    # mirror compose-run's provisioning: bare python3 when yaml imports,
    # else the nix-provisioned interpreter.
    if subprocess.run([sys.executable, "-c", "import yaml"],
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode == 0:
        return [sys.executable]
    return ["nix", "shell", "--impure", "--expr",
            "with import <nixpkgs> {}; python3.withPackages (p: [ p.pyyaml ])",
            "-c", "python3"]


PLAN_PY = _plan_interp()


def plan_stage(src, out):
    return subprocess.run(PLAN_PY + [
        os.path.join(SCRIPTS, "vmf_plan.py"), "plan", src, out],
        capture_output=True, text=True)


def say(msg):
    sys.stderr.write("race: %s\n" % msg)
    sys.stderr.flush()


def verdict_path(name):
    # Id-layout instance dir first (RUNS/<id>/verdict, name = symlink),
    # then the legacy flat marker (RUNS/<name>.verdict).
    p = os.path.join(RUNS, name, "verdict")
    if os.path.isfile(p):
        return p
    return os.path.join(RUNS, "%s.verdict" % name)


def conf_path(name):
    d = os.path.join(RUNS, name)
    if os.path.isfile(os.path.join(d, "conf")):
        return os.path.join(d, "conf")
    return os.path.join(RUNS, "%s.conf" % name)


def load_approaches(src):
    enum = os.path.join(RUNS, ".race-enum.json")
    rc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"),
         "enumerate", src, enum],
        capture_output=True, text=True)
    sys.stderr.write(rc.stderr)
    if rc.returncode != 0:
        return []
    try:
        with open(enum) as f:
            return json.load(f).get("approaches") or []
    except (OSError, ValueError):
        return []


def apply_filters(approaches):
    only = [x.strip() for x in
            (os.environ.get("VMF_RACE_APPROACH") or "").split(",") if x.strip()]
    skip = [x.strip() for x in
            (os.environ.get("VMF_RACE_SKIP") or "").split(",") if x.strip()]
    out = []
    for i, a in enumerate(approaches, 1):
        handles = {a["kind"], str(i)}
        if only and not (handles & set(only)):
            continue
        if skip and (handles & set(skip)):
            continue
        out.append(a)
    return out


def _expose_ports(src):
    # EXPOSE lines from the root Dockerfile: the dockerfile candidate's
    # declared surface (deterministic; the compose kind scrapes its own).
    ports = []
    p = os.path.join(src, "Dockerfile")
    if os.path.isfile(p):
        for line in open(p, errors="replace"):
            m = re.match(r"^\s*EXPOSE\s+(\d+)", line)
            if m and int(m.group(1)) not in ports:
                ports.append(int(m.group(1)))
    return ports[:8]


def synth_dir(kind, src, image, ports):
    # Synthetic single-service compose dir for the dockerfile and
    # prebuilt_image kinds; the compose kind uses the real repo dir.
    # Ports come from the Dockerfile's EXPOSE lines, falling back to the
    # enumeration's documented ports.
    d = tempfile.mkdtemp(prefix="vmf-race-%s-" % kind)
    ports = _expose_ports(src) or ports or []
    svc = {"image": image} if kind != "dockerfile" else \
        {"build": {"context": src, "dockerfile": "Dockerfile"}}
    doc = {"services": {"app": dict(
        svc, ports=["%d:%d" % (p, p) for p in ports], environment={})}}
    with open(os.path.join(d, "docker-compose.yml"), "w") as f:
        json.dump(doc, f, indent=2)
    return d


def satisfiable(kind, src, image, ports, log):
    # Prune before boot: the plan stage must succeed, no env gap, and at
    # least one declared tcp port for the verify arbiter.
    if kind == "prebuilt_image" and not image:
        log.write("pruned: no image ref from the enumeration\n")
        return False
    if kind in ("dockerfile", "prebuilt_image") \
            and not _expose_ports(src) and not ports:
        log.write("pruned: no declared tcp ports to verify "
                  "(no EXPOSE lines, none documented)\n")
        return False
    if kind in ("compose", "dockerfile", "prebuilt_image"):
        psrc = src
        if kind != "compose":
            psrc = synth_dir(kind, src, image, ports)
        out = os.path.join(RUNS, ".race-plan.json")
        rc = plan_stage(psrc, out)
        if rc.returncode != 0:
            log.write("pruned: plan stage failed\n%s" % rc.stderr[-1500:])
            return False
        try:
            with open(out) as f:
                plan = json.load(f)
        except (OSError, ValueError):
            log.write("pruned: unreadable plan\n")
            return False
        for s in plan.get("services") or []:
            if s.get("env_unresolved"):
                log.write("pruned: env unresolvable (env_file missing, "
                          "no .env.example, no db sibling or no model)\n")
                return False
        primary = plan.get("primary")
        for s in plan.get("services") or []:
            if s.get("name") != primary:
                continue
            if not any(pp.get("proto", "tcp") != "udp"
                       for pp in s.get("ports") or []):
                log.write("pruned: no declared tcp ports to verify\n")
                return False
        return True
    # source_build / install_script: the gapfill flow answers honestly
    # on its own; nothing to prune cheaply here.
    return True


def runner_cmd(kind, name, src, image, ports=None):
    # Candidates re-enter oci-run as children; the marker env stops the
    # race from re-entering. --yes/--ssh ride the runner args.
    env = dict(os.environ)
    env["VMF_RACE_CHILD"] = "1"
    env["VMF_APPROACH"] = kind
    env["VMF_NAME"] = name
    env["VMF_RUN_YES"] = "1"
    env["VMF_RUN_DETACH"] = "1"
    env["VMF_RUN_SSH"] = "1"
    env.pop("VMF_RUN_INTENT", None)
    if kind == "source_build":
        env["VMF_PLAN_SKIP_COMPOSE"] = "1"
        args = [src]
    elif kind == "compose":
        args = [src]
    else:
        args = [synth_dir(kind, src, image, ports)]
    return ["bash", os.path.join(SCRIPTS, "oci-run.sh"), "--yes",
            "--name", name] + args, env


def stop_vm(name):
    subprocess.run(["bash", os.path.join(SCRIPTS, "stop.sh"), name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _die(signum, _frame):
    # Killed races must not orphan candidate VMs: reap everything this
    # race started, then exit.
    say("killed (signal %d); reaping candidates" % signum)
    for cand in list(running_state):
        r = running_state[cand]
        try:
            r["log"].close()
        except Exception:
            pass
        r["proc"].terminate()
        stop_vm(cand)
    sys.exit(128 + signum)


running_state = {}


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: vmf_race.py <src>\n")
        return 2
    src = os.path.abspath(argv[1])
    base = os.environ.get("VMF_NAME") or os.path.basename(src)
    plan_only = os.environ.get("VMF_RACE_MODE") == "plan"

    approaches = apply_filters(load_approaches(src))
    if plan_only:
        # Satisfiability without booting: pruned candidates print and
        # any survivor means the run would race.
        logdir = os.path.join(RUNS, "race-logs")
        os.makedirs(logdir, exist_ok=True)
        alive = 0
        for i, a in enumerate(approaches, 1):
            cand = "%s-c%d" % (base, i)
            lp = os.path.join(logdir, "%s.log" % cand)
            with open(lp, "w") as log:
                ok = satisfiable(a["kind"], src, a.get("image"), a.get("ports") or [], log)
            if ok:
                alive += 1
            else:
                say("%d %s .. pruned (%s)" % (i, a["kind"], lp))
        say("plan only: %d/%d approach(es) satisfiable; nothing booted"
            % (alive, len(approaches)))
        return 0 if alive else 1
    if not approaches:
        say("no approach allowed; nothing to race")
        return 1

    # Satisfiability prune before any boot.
    keep = []
    logdir = os.path.join(RUNS, "race-logs")
    os.makedirs(logdir, exist_ok=True)
    for i, a in enumerate(approaches, 1):
        cand = "%s-c%d" % (base, i)
        lp = os.path.join(logdir, "%s.log" % cand)
        with open(lp, "w") as log:
            ok = satisfiable(a["kind"], src, a.get("image"), a.get("ports") or [], log)
        if ok:
            keep.append({"i": i, "kind": a["kind"], "cand": cand, "lp": lp,
                         "image": a.get("image"), "ports": a.get("ports") or []})
        else:
            say("%d %s .. pruned (%s)" % (i, a["kind"], lp))
    if not keep:
        say("all approaches pruned at plan time")
        return 1

    running = {}
    verdicts = {}
    winner = None
    started = time.time()
    last_event = started
    queue = list(keep)
    # Reruns reuse candidate names: stale verdict markers would poison
    # the poll (and the crown).
    for k in keep:
        for p in (os.path.join(RUNS, "%s.verdict" % k["cand"]),
                  os.path.join(RUNS, k["cand"], "verdict")):
            try:
                os.unlink(p)
            except OSError:
                pass
    while queue or running:
        now = time.time()
        # Stagger: the next candidate starts STAGGER seconds after the
        # last race event (a start, a verdict, or a failure). Cheapest
        # boots first; usually it wins alone.
        if (queue and len(running) < PARALLEL
                and now - last_event >= STAGGER):
            k = queue.pop(0)
            cmd, env = runner_cmd(k["kind"], k["cand"], src, k["image"], k["ports"])
            log = open(k["lp"], "a")
            log.write("\n===== boot =====\n")
            proc = subprocess.Popen(cmd, env=env, stdout=log,
                                    stderr=subprocess.STDOUT)
            running[k["cand"]] = dict(k, proc=proc, log=log, born=now)
            running_state[k["cand"]] = running[k["cand"]]
            say("%d %s .. started (%s)" % (k["i"], k["kind"], k["cand"]))
            last_event = now
        # Poll verdicts and dead runners.
        for cand in list(running):
            r = running[cand]
            vpath = verdict_path(cand)
            rc = r["proc"].poll()
            if os.path.isfile(vpath):
                with open(vpath) as f:
                    status = f.read().strip() or "fail"
                if status == "pass" and r["proc"].poll() is None:
                    # Provisional: the runner's chain may still be
                    # repairing or rebooting behind the verdict marker.
                    # The crown waits for the runner to exit, then the
                    # file's last write wins.
                    continue
                verdicts[cand] = status
                if status == "pass":
                    winner = cand
            elif rc is not None and rc != 0:
                verdicts[cand] = "fail (runner exit %d)" % rc
            elif rc == 0:
                # Runner done without a verdict: brief grace for the
                # marker write, then judge.
                if "done_at" not in r:
                    r["done_at"] = now
                elif now - r["done_at"] > 30:
                    verdicts[cand] = "fail (no verdict after boot)"
            if cand in verdicts:
                say("%d %s .. %s" % (r["i"], r["kind"], verdicts[cand]))
                r["log"].close()
                r["proc"].terminate()
                del running[cand]
                running_state.pop(cand, None)
                last_event = now
                if winner:
                    break
            elif rc == 0 and not os.path.isfile(vpath):
                # Boot finished; the verify stage may still be running
                # (the child re-runs) — grace window before judging.
                r.setdefault("grace", now + 120)
                if now > r["grace"]:
                    verdicts[cand] = "fail (no verdict after boot)"
        if winner:
            break
        if time.time() - started > DEADLINE:
            for cand, r in running.items():
                verdicts.setdefault(cand, "fail (race deadline)")
                r["log"].close()
                r["proc"].terminate()
            say("race deadline reached")
            break
        time.sleep(2)

    for k in keep:
        if k["cand"] != winner:
            stop_vm(k["cand"])
    if not winner:
        say("no approach produced a working service")
        for cand, status in verdicts.items():
            say("%s: %s (log: %s)" % (
                cand, status, os.path.join(RUNS, "race-logs",
                                           "%s.log" % cand)))
        say("rerun with --approach <name|number> to retry one approach")
        return 1

    w = next(k for k in keep if k["cand"] == winner)
    say("winner %d %s; reaping losers" % (w["i"], w["kind"]))
    # Kill the losers' runner chains FIRST (a candidate that is still
    # building can boot its VM after the reap otherwise), then stop any
    # VM that already rose, and re-stop after a settle for stragglers.
    for k in keep:
        if k["cand"] != winner and k["cand"] in running_state:
            r = running_state[k["cand"]]
            say("reap: stopping %s (%s)" % (k["cand"], k["kind"]))
            try:
                r["proc"].terminate()
                r["log"].close()
            except Exception:
                pass
    time.sleep(2)
    for k in keep:
        if k["cand"] != winner:
            stop_vm(k["cand"])
    time.sleep(4)
    for k in keep:
        if k["cand"] != winner:
            stop_vm(k["cand"])

    # Promotion: boot the canonical name from the winner's data drive.
    say("promotion: stopping %s" % winner)
    stop_vm(winner)
    # Let the reaped VMs release their host ports before the canonical
    # boot claims them: wait until the winner's first port is actually
    # free (qemu teardown can lag the stop), then a short settle.
    if w.get("ports"):
        deadline = time.time() + 20
        waited = False
        while time.time() < deadline:
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", w["ports"][0]))
                s.close()
                break
            except OSError:
                if not waited:
                    waited = True
                    say("promotion: waiting for port %d to free" % w["ports"][0])
                s.close()
                time.sleep(1)
    time.sleep(2)
    cmd, env = runner_cmd(w["kind"], base, src, w["image"], w["ports"])
    env["VMF_NAME"] = base
    say("promotion: booting %s (~2-4 min; tail: ~/.vmf/runs/%s/log)" % (base, base))
    log = open(w["lp"], "a")
    log.write("\n===== promotion (%s) =====\n" % base)
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
    proc.wait()
    # The detached runner returns before its verify chain finishes: the
    # verdict marker (and the rendered target) land minutes later. Wait
    # for the marker instead of defaulting to pass on a missing verdict.
    vpath = verdict_path(base)
    deadline = time.time() + int(os.environ.get("VMF_PROMOTION_WAIT", "900"))
    while not os.path.isfile(vpath) and time.time() < deadline:
        time.sleep(2)
    status = "fail (no verdict)"
    if os.path.isfile(vpath):
        with open(vpath) as f:
            status = f.read().strip() or "pass"
    log.close()
    say("promotion %s: %s" % (base, status))
    # The rendered deliverable from the promoted instance's conf: the
    # bump (or the future per-VM IP) is visible at the end of the run.
    try:
        with open(conf_path(base)) as f:
            for line in f:
                if line.startswith("TARGET="):
                    say("target: %s" % line.split("=", 1)[1].strip())
                    break
    except OSError:
        pass
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _die)
    signal.signal(signal.SIGINT, _die)
    sys.exit(main(sys.argv))
