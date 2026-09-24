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
#      VMF_RACE_STAGGER (10) VMF_RACE_PARALLEL (2) VMF_RACE_DEADLINE (2700)
#      VMF_RACE_CHILD=1    — candidate runners must not re-enter the race
#      VMF_RACE_SKIP_CACHE=1 — ignore the winner cache for this run
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vmf_status

RUNS = os.environ.get("VMF_RUNS") or os.path.expanduser("~/.vmf/runs")
GEN = os.environ.get("VMF_GENERATED") \
    or os.path.join(os.path.expanduser("~"), ".vmf", "generated")
SCRIPTS = os.environ.get("VMF_SCRIPTS_DIR") or os.path.dirname(
    os.path.abspath(__file__))
STAGGER = int(os.environ.get("VMF_RACE_STAGGER") or 10)
PARALLEL = int(os.environ.get("VMF_RACE_PARALLEL") or 2)
DEADLINE = int(os.environ.get("VMF_RACE_DEADLINE") or 2700)


def bundle_key(src):
    # The winner cache keys on the plan-relevant content bundle
    # (vmf_plan.py cache-key: gap-fill evidence + root compose files +
    # prompt version), not on git HEAD. Fast-moving repos change HEAD
    # hourly; a solved tree must keep replaying its winner.
    try:
        r = subprocess.run(PLAN_PY + [
            os.path.join(SCRIPTS, "vmf_plan.py"), "cache-key", src],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    k = (r.stdout or "").strip()
    if r.returncode != 0 or not re.fullmatch(r"[0-9a-f]{12}", k):
        say("warning: cache-key failed (%s); winner cache disabled"
            % (r.stderr or "").strip()[-120:])
        return None
    return k


def winner_path(src):
    k = bundle_key(src)
    if not k:
        return None
    return os.path.join(GEN, k, "winner.json")


def load_winner(src):
    p = winner_path(src)
    if not p:
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_winner(src, w, cand):
    k = bundle_key(src)
    if not k:
        return
    rec = {"approach": w["kind"], "image": w.get("image"),
           "ports": w.get("ports") or [],
           "compose_file": w.get("compose_file"),
           "by": cand, "url": os.environ.get("VMF_COMPOSE_URL", ""),
           "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    d = os.path.join(GEN, k)
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "winner.json.tmp")
        with open(tmp, "w") as f:
            json.dump(rec, f, indent=2)
        os.replace(tmp, os.path.join(d, "winner.json"))
    except OSError as e:
        say("warning: winner cache write failed: %s" % e)
        return
    say("winner cache: %s" % os.path.join(d, "winner.json"))


def cache_allowed():
    # Plan-table mode and explicit filters want the full field, not a
    # one-candidate replay; VMF_RACE_SKIP_CACHE bypasses for testing.
    if os.environ.get("VMF_RACE_MODE") == "plan":
        return False
    if os.environ.get("VMF_RACE_SKIP_CACHE") == "1":
        return False
    if os.environ.get("VMF_RACE_APPROACH") or os.environ.get("VMF_RACE_SKIP"):
        return False
    return True


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


def plan_stage(src, out, compose_file=None):
    # The prune-phase plan must see the same compose-file hint the boot
    # would get, or the prune judges a compose variant the model never
    # named (ghost's docker/dev-url-testing vs compose.dev.yaml).
    env = dict(os.environ)
    if compose_file:
        env["VMF_COMPOSE_HINT_FILE"] = compose_file
    return subprocess.run(PLAN_PY + [
        os.path.join(SCRIPTS, "vmf_plan.py"), "plan", src, out],
        capture_output=True, text=True, env=env)


RACE_LOG = None
# Plan-only mode keeps the old firehose (the table IS the artifact);
# run mode sends the firehose to the race log and the terminal gets
# the one status line (VMF_LOUD=1 mirrors the firehose for debugging).
SAY_STDERR = True


def say(msg):
    if RACE_LOG:
        try:
            with open(RACE_LOG, "a") as f:
                f.write("race: %s\n" % msg)
        except OSError:
            pass
    if SAY_STDERR or os.environ.get("VMF_LOUD") == "1":
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


def fanout_approaches(src):
    # The per-method fan-out plans on paper first (blocked methods
    # never boot). Returns approaches or None when the fan-out is off
    # or yields nothing runnable (the enumerate stays as fallback).
    if os.environ.get("VMF_RACE_FANOUT", "1") == "0":
        return None
    out = os.path.join(RUNS, ".race-fanout.json")
    try:
        rc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"),
             "fanout", src, out], capture_output=True, text=True,
            timeout=1200)
    except (OSError, subprocess.SubprocessError) as e:
        say("plan fan-out failed (%s); falling back to enumerate" % e)
        return None
    sys.stderr.write(rc.stderr or "")
    if rc.returncode != 0:
        say("plan fan-out: nothing runnable; falling back to enumerate")
        return None
    try:
        with open(out) as f:
            fa = json.load(f).get("approaches") or []
    except (OSError, ValueError):
        return None
    return fa or None


def load_approaches(src):
    fa = fanout_approaches(src)
    if fa:
        return fa
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


def satisfiable(kind, src, image, ports, log, compose_file=None):
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
        rc = plan_stage(psrc, out, compose_file)
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


def runner_cmd(kind, name, src, image, ports=None, compose_file=None):
    # Candidates re-enter oci-run as children; the marker env stops the
    # race from re-entering. --yes/--ssh ride the runner args.
    env = dict(os.environ)
    env["VMF_RACE_CHILD"] = "1"
    env["VMF_APPROACH"] = kind
    env["VMF_NAME"] = name
    env["VMF_RUN_YES"] = "1"
    env["VMF_RUN_DETACH"] = "1"
    env["VMF_RUN_SSH"] = "1"
    # Candidates pay boot + in-guest install latency; the default
    # verify deadline (built for fast images) expires mid-install.
    env.setdefault("VMF_VERIFY_SECS", "420")
    env.pop("VMF_RUN_INTENT", None)
    if kind in ("source_build", "install_script"):
        # Repo-install kinds run the gap-fill direct flow on the real
        # source dir; a synthetic compose would need an image the plan
        # does not carry (the "None" registry pull).
        env["VMF_PLAN_SKIP_COMPOSE"] = "1"
        args = [src]
    elif kind == "compose":
        # The enumeration may name the compose file the dev script uses
        # (a dev variant like compose.dev.yaml); the plan honors it.
        if compose_file:
            env["VMF_COMPOSE_HINT_FILE"] = compose_file
        args = [src]
        say("spawn %s: VMF_COMPOSE_HINT_FILE=%r" % (
            name, env.get("VMF_COMPOSE_HINT_FILE")))
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
    vmf_status.clear(_die_base)
    vmf_status.event(_die_base, "fail", "killed (signal %d)" % signum,
                     final=True)
    sys.exit(128 + signum)


running_state = {}
_die_base = ""


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: vmf_race.py <src>\n")
        return 2
    src = os.path.abspath(argv[1])
    base = os.environ.get("VMF_NAME") or os.path.basename(src)
    plan_only = os.environ.get("VMF_RACE_MODE") == "plan"
    global RACE_LOG, SAY_STDERR, _die_base
    if plan_only:
        SAY_STDERR = True
    else:
        SAY_STDERR = False
        _die_base = base
        logdir = os.path.join(RUNS, "race-logs")
        os.makedirs(logdir, exist_ok=True)
        RACE_LOG = os.path.join(logdir, "%s.race.log" % base)
        vmf_status.begin(base)
        vmf_status.event(base, "plan", "evidence bundle")

    if plan_only:
        # Satisfiability without booting: pruned candidates print and
        # any survivor means the run would race.
        approaches = apply_filters(load_approaches(src))
        logdir = os.path.join(RUNS, "race-logs")
        os.makedirs(logdir, exist_ok=True)
        alive = 0
        for i, a in enumerate(approaches, 1):
            cand = "%s-c%d" % (base, i)
            lp = os.path.join(logdir, "%s.log" % cand)
            with open(lp, "w") as log:
                ok = satisfiable(a["kind"], src, a.get("image"), a.get("ports") or [], log, a.get("compose_file"))
            if ok:
                alive += 1
            else:
                say("%d %s .. pruned (%s)" % (i, a["kind"], lp))
        say("plan only: %d/%d approach(es) satisfiable; nothing booted"
            % (alive, len(approaches)))
        return 0 if alive else 1

    # Winner cache FIRST: a hit replays as one candidate and costs no
    # enumerate call, no gap-fill, no LLM. The check must precede
    # load_approaches or every replay still pays the enumeration.
    if cache_allowed():
        w = load_winner(src)
        if w:
            logdir = os.path.join(RUNS, "race-logs")
            os.makedirs(logdir, exist_ok=True)
            cand = "%s-c1" % base
            keep = [{"i": 1, "kind": w["approach"], "cand": cand,
                     "lp": os.path.join(logdir, "%s.log" % cand),
                     "image": w.get("image"), "ports": w.get("ports") or [],
                     "compose_file": w.get("compose_file"), "cached": True}]
            say("winner cache: replay %s (%s)" % (
                w["approach"], w.get("url") or "same tree"))
            vmf_status.event(base, "replay", w["approach"])
            rc = race(keep, src, base)
            if rc == 0:
                return 0
            try:
                os.unlink(winner_path(src))
            except OSError:
                pass
            say("winner cache: replay failed; racing the full field")

    approaches = apply_filters(load_approaches(src))
    if not approaches:
        say("no approach allowed; nothing to race")
        vmf_status.event(base, "fail", "no approach allowed", final=True)
        return 1

    # Satisfiability prune before any boot.
    keep = []
    logdir = os.path.join(RUNS, "race-logs")
    os.makedirs(logdir, exist_ok=True)
    for i, a in enumerate(approaches, 1):
        cand = "%s-c%d" % (base, i)
        lp = os.path.join(logdir, "%s.log" % cand)
        with open(lp, "w") as log:
            ok = satisfiable(a["kind"], src, a.get("image"), a.get("ports") or [], log, a.get("compose_file"))
        if ok:
            keep.append({"i": i, "kind": a["kind"], "cand": cand, "lp": lp,
                         "image": a.get("image"), "ports": a.get("ports") or [],
                         "compose_file": a.get("compose_file")})
        else:
            say("%d %s .. pruned (%s)" % (i, a["kind"], lp))
    if not keep:
        say("all approaches pruned at plan time")
        vmf_status.event(base, "fail", "all approaches pruned", final=True)
        return 1

    return race(keep, src, base)



# Tranche bands by method cost. The static map is the default; a plan
# entry's cost word (fast/medium/slow/slowest, from the fan-out)
# refines it. Band 0 boots first; a failed band escalates to the next.
KIND_BAND = {"prebuilt_image": 0, "compose": 1, "dockerfile": 1,
             "install_script": 2, "source_build": 3}
COST_BAND = {"fast": 0, "medium": 1, "slow": 2, "slowest": 3}
# Seconds a band owns before escalation (0 = rest of the race deadline).
BAND_SECS = (480, 720, 900, 0)


def band_of(k):
    c = (k.get("cost") or "").strip().lower()
    if c in COST_BAND:
        return COST_BAND[c]
    return KIND_BAND.get(k.get("kind"), 3)


def tranche_mode():
    return (os.environ.get("VMF_RACE_TRANCHE") or "all").strip().lower() \
        == "tranches"



def race(keep, src, base):
    # The staggered boot loop, the crown, and the promotion. Keep
    # entries carry i/kind/cand/lp/image/ports/compose_file.
    running = {}
    verdicts = {}
    winner = None
    started = time.time()
    last_event = started
    # Tranche bands: cheapest methods first; a band that exhausts its
    # verdicts escalates to the next. "all" (default) races everything
    # at once, exactly as before the bands existed.
    bands = [[] for _ in range(4)]
    for k in keep:
        bands[band_of(k)].append(k)
    if tranche_mode():
        say("tranches: " + " | ".join(
            ("T%d %s" % (i, ",".join(x["kind"] for x in b))) if b
            else ("T%d -" % i)
            for i, b in enumerate(bands)))
        groups = [(i, b) for i, b in enumerate(bands) if b]
    else:
        groups = [(0, list(keep))]
    queue = []
    band_no = 0
    band_start = 0.0
    # Reruns reuse candidate names: stale verdict markers would poison
    # the poll (and the crown).
    for k in keep:
        for p in (os.path.join(RUNS, "%s.verdict" % k["cand"]),
                  os.path.join(RUNS, k["cand"], "verdict")):
            try:
                os.unlink(p)
            except OSError:
                pass
    while queue or running or groups:
        now = time.time()
        # Stagger: the next candidate starts STAGGER seconds after the
        # last race event (a start, a verdict, or a failure). Cheapest
        # boots first; usually it wins alone.
        if (queue and len(running) < PARALLEL
                and now - last_event >= STAGGER):
            k = queue.pop(0)
            cmd, env = runner_cmd(k["kind"], k["cand"], src, k["image"], k["ports"], k.get("compose_file"))
            log = open(k["lp"], "a")
            log.write("\n===== boot =====\n")
            proc = subprocess.Popen(cmd, env=env, stdout=log,
                                    stderr=subprocess.STDOUT)
            band = band_of(k)
            running[k["cand"]] = dict(k, proc=proc, log=log, born=now,
                                      band=band)
            running_state[k["cand"]] = running[k["cand"]]
            say("%d %s .. started (%s)" % (k["i"], k["kind"], k["cand"]))
            vmf_status.event(base, "T%d" % band,
                             "c%d %s boot" % (k["i"], k["kind"]))
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
                try:
                    with open(r["log"].name, errors="replace") as f:
                        tail = [l.rstrip() for l in f.readlines()[-3:]]
                    say("%s last: %s" % (cand, " | ".join(t[-90:] for t in tail
                                                         if t.strip()) or "(no output)"))
                except OSError:
                    say("%s last: (log unreadable)" % cand)
            elif rc == 0:
                # Runner done without a verdict: brief grace for the
                # marker write, then judge.
                if "done_at" not in r:
                    r["done_at"] = now
                elif now - r["done_at"] > 30:
                    verdicts[cand] = "fail (no verdict after boot)"
            if cand in verdicts:
                say("%d %s .. %s" % (r["i"], r["kind"], verdicts[cand]))
                vmf_status.event(base, "T%d" % r.get("band", 3),
                                 "c%d %s %s" % (r["i"], r["kind"],
                                                verdicts[cand][:24]))
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
        # Band deadline: the band owns its slice of the clock; hanging
        # candidates (and unstarted queue entries) yield to the next
        # band when it lapses.
        band_secs = BAND_SECS[band_no] if tranche_mode() else 0
        if band_secs and now - band_start > band_secs:
            for cand, r in running.items():
                verdicts.setdefault(cand, "fail (band T%d deadline)" % band_no)
                r["log"].close()
                r["proc"].terminate()
                running_state.pop(cand, None)
            for k in queue:
                verdicts.setdefault(
                    k["cand"], "fail (band T%d deadline; not started)" % band_no)
            running.clear()
            queue = []
        if not queue and not running and groups:
            if winner:
                break
            nxt_no, nxt = groups.pop(0)
            if tranche_mode() and nxt and band_start:
                say("tranche T%d failed; escalate to T%d" % (band_no, nxt_no))
            band_no, queue = nxt_no, list(nxt)
            band_start = time.time()
            if tranche_mode() and queue:
                say("tranche T%d: %d candidate(s) (%s)" % (
                    band_no, len(queue),
                    ",".join(x["kind"] for x in queue)))
            continue
        if not queue and not running and not groups:
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
        vmf_status.event(base, "fail", "no working service", final=True)
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
    return promote(w, src, base, winner)



def promote(w, src, base, winner):
    # Boot the winning candidate's spec under the canonical name; a
    # transient failure (partial boot, slow verdict) retries once. The
    # verdict marker must be unlinked before EVERY attempt: a stale
    # marker from a rerun would satisfy the wait loop instantly.
    say("promotion: stopping %s" % winner)
    stop_vm(winner)
    # Slirp winners must release their host ports before the canonical
    # boot claims them (qemu teardown can lag the stop). ip-mode winners
    # own an address instead: nothing to free, skip the wait.
    winner_ip_mode = False
    try:
        with open(conf_path(winner)) as f:
            winner_ip_mode = any(line.startswith("TAP=") for line in f)
    except OSError:
        pass
    if not winner_ip_mode and w.get("ports"):
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
    promote_tries = int(os.environ.get("VMF_PROMOTION_TRIES") or "2")
    status = "fail (no verdict)"
    for attempt in range(1, promote_tries + 1):
        if attempt > 1:
            say("promotion: retry %d/%d (fresh boot under %s)"
                % (attempt, promote_tries, base))
        cmd, env = runner_cmd(w["kind"], base, src, w["image"],
                              w["ports"], w.get("compose_file"))
        env["VMF_NAME"] = base
        say("promotion: booting %s (~2-4 min; tail: ~/.vmf/runs/%s/log)"
            % (base, base))
        vmf_status.event(base, "promote", "booting")
        log = open(w["lp"], "a")
        log.write("\n===== promotion (%s)%s =====\n"
                  % (base, "" if attempt == 1
                     else " retry %d" % attempt))
        vpath = verdict_path(base)
        try:
            os.unlink(vpath)
        except OSError:
            pass
        proc = subprocess.Popen(cmd, env=env, stdout=log,
                                stderr=subprocess.STDOUT)
        proc.wait()
        # The detached runner returns before its verify chain finishes: the
        # verdict marker (and the rendered target) land minutes later. Wait
        # for the marker instead of defaulting to pass on a missing verdict.
        deadline = time.time() + int(os.environ.get("VMF_PROMOTION_WAIT", "900"))
        while not os.path.isfile(vpath) and time.time() < deadline:
            time.sleep(2)
        status = "fail (no verdict)"
        if os.path.isfile(vpath):
            with open(vpath) as f:
                status = f.read().strip() or "pass"
        log.close()
        say("promotion %s: %s" % (base, status))
        if status == "pass":
            break
        if attempt < promote_tries:
            stop_vm(base)
            time.sleep(2)
    # The rendered deliverable from the promoted instance's conf: the
    # bump (or the future per-VM IP) is visible at the end of the run.
    target = ""
    try:
        with open(conf_path(base)) as f:
            for line in f:
                if line.startswith("TARGET="):
                    target = line.split("=", 1)[1].strip()
                    say("target: %s" % target)
                    break
    except OSError:
        pass
    if status == "pass":
        save_winner(src, w, winner)
        vmf_status.event(base, "pass", target or "pass", final=True)
    else:
        vmf_status.event(base, "fail", status[:40], final=True)
    return 0 if status == "pass" else 1




if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _die)
    signal.signal(signal.SIGINT, _die)
    sys.exit(main(sys.argv))
