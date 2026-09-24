#!/usr/bin/env python3
# vmf_race.py — the staggered install-approach race.
#
# usage: vmf_race.py <src>
#
# The route scout (vmf_plan.py scout) supplies the approach table as a
# JSONL stream: one route per line, in emission order, plus a final
# summary. The race drains the file every tick, so the first scouted
# route boots while the scout still reads. When the scout yields
# nothing (off, failed, zero routes), the per-method fan-out plans on
# paper instead, and the enumerate stays as the last fallback:
#   scout           streaming routes; boots race as they arrive
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
#      VMF_RACE_SCOUT=0   — skip the scout, fan out directly
#      VMF_SCOUT_TURNS (3) VMF_SCOUT_FIRST (120) VMF_SCOUT_DEADLINE (240)
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
import vmf_ui

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
           "method": w.get("method") or "",
           "cost": w.get("cost") or "",
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


def scout_enabled():
    return os.environ.get("VMF_RACE_SCOUT", "1") != "0"


class ScoutFeed:
    # Streams scout emissions into the race: the scout subprocess
    # appends one JSON route per line to a JSONL file as it reads; the
    # race drains the file every tick, so the first scouted route can
    # boot while the scout still reads. The file (not the pipe) is the
    # contract.
    def __init__(self, src, log=None):
        self.out = os.path.join(RUNS, ".race-scout.jsonl")
        self.routes = 0
        self.summary = None
        self.summary_taken = False
        self._consumed = 0
        try:
            os.unlink(self.out)
        except OSError:
            pass
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"),
             "scout", src, self.out],
            stdout=log or subprocess.DEVNULL, stderr=subprocess.STDOUT)

    def alive(self):
        return self.proc.poll() is None

    def kill(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass

    def wait_first(self, timeout):
        # Block until the first route lands, the scout exits, or the
        # timeout lapses. Returns the drained routes (possibly several)
        # or None when the scout yielded nothing usable.
        end = time.time() + max(10, timeout)
        while time.time() < end:
            got = self.drain()
            if got:
                return got
            if not self.alive():
                return self.drain() or None
            time.sleep(1)
        return None

    def drain(self):
        out = []
        try:
            with open(self.out) as f:
                lines = f.read().splitlines()
        except OSError:
            return out
        while self._consumed < len(lines):
            ln = lines[self._consumed]
            self._consumed += 1
            try:
                j = json.loads(ln)
            except ValueError:
                continue
            if "summary" in j:
                self.summary = j["summary"]
                continue
            if j.get("kind"):
                out.append(j)
        self.routes += len(out)
        return out


def _route_allowed(r, i):
    # Streaming twin of apply_filters: kind, method, or number.
    handles = {r.get("kind"), r.get("method"), str(i)}
    only = {x.strip() for x in
            (os.environ.get("VMF_RACE_APPROACH") or "").split(",") if x.strip()}
    skip = {x.strip() for x in
            (os.environ.get("VMF_RACE_SKIP") or "").split(",") if x.strip()}
    if only and not (handles & only):
        return False
    if skip and (handles & skip):
        return False
    return True


def _keep_entry(i, r, logdir, base):
    cand = "%s-c%d" % (base, i)
    detail = r.get("image") or r.get("compose_file") or ""
    if not detail and isinstance(r.get("direct"), dict):
        cmd = r["direct"].get("command") or []
        detail = " ".join(str(x) for x in cmd)[:40]
    return {"i": i, "kind": r.get("kind"), "cand": cand,
            "lp": os.path.join(logdir, "%s.log" % cand),
            "image": r.get("image"), "ports": r.get("ports") or [],
            "compose_file": r.get("compose_file"),
            "method": r.get("method") or "", "cite": r.get("cite") or "",
            "cost": r.get("cost") or "", "detail": str(detail)[:40]}


def reattach_allowed():
    # --new (and --replace) force a fresh instance; the default
    # presents a healthy running instance of the same tree.
    return os.environ.get("VMF_RACE_NEW", "0") != "1"


def _conf_paths():
    # Instance confs: id-keyed dirs ($RUNS/<id>/conf) and the legacy
    # flat layout ($RUNS/<name>.conf).
    out = []
    try:
        for d in sorted(os.listdir(RUNS)):
            p = os.path.join(RUNS, d, "conf")
            if os.path.isfile(p):
                out.append(p)
            p = os.path.join(RUNS, d + ".conf")
            if os.path.isfile(p):
                out.append(p)
    except OSError:
        pass
    return out


def _conf_get(path, field):
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if line.startswith(field + "="):
                    return line[len(field) + 1:].strip()
    except OSError:
        pass
    return ""


def _tcp_alive(host, port, timeout=2.0):
    # Connect-only probe: a silent open counts alive (the verify's
    # check_tcp semantics); refused or timed out means dead.
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def find_reattach(src):
    # One healthy running instance of this tree presents itself
    # instead of a new boot. The conf KEY is the content identity the
    # race stamps on every boot; the tcp probe is the arbiter — a conf
    # alone proves nothing.
    k = bundle_key(src)
    if not k:
        return None
    for path in _conf_paths():
        if _conf_get(path, "KEY") != k:
            continue
        name = _conf_get(path, "NAME")
        pid = _conf_get(path, "PID")
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            continue
        target = _conf_get(path, "TARGET")
        m = re.match(r"tcp://([^:/]+):(\d+)", target)
        if not m or not _tcp_alive(m.group(1), m.group(2)):
            continue
        return name, target
    return None


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


def runner_cmd(kind, name, src, image, ports=None, compose_file=None,
               method=None):
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
    # Compose dev stacks boot even slower: ghost's first-run
    # migrations finished ~2 min AFTER a 660s window expired
    # (measured 13:26-13:39). 1200s covers image loads + migrations
    # with margin; the winner cache makes solved trees cheap again.
    env.setdefault("VMF_VERIFY_SECS", "1200" if kind == "compose" else "420")
    # The detached runner chains python subprocesses; without this the
    # verify's progress lines sit in the block buffer and a healthy
    # run looks wedged in the logs.
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.pop("VMF_RUN_INTENT", None)
    if kind in ("source_build", "install_script"):
        # Repo-install kinds run the gap-fill direct flow on the real
        # source dir; a synthetic compose would need an image the plan
        # does not carry (the "None" registry pull). A scouted route
        # replays its OWN plan (direct-<method>.json); the shared pkg
        # plan (direct.json) stays the unspecific fallback.
        env["VMF_PLAN_SKIP_COMPOSE"] = "1"
        if method and re.fullmatch(r"[a-z0-9_-]{1,24}", method) \
                and method != "pkg":
            env["VMF_PLAN_DIRECT"] = method
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
    _board_close()
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
_board_ref = {}


def _board_close():
    b = _board_ref.get("b")
    if b:
        try:
            b.close()
        except Exception:
            pass
        _board_ref["b"] = None
        vmf_status.set_quiet(False)
        vmf_status.line_closed()


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

    # Content identity for this tree: the reattach match AND the key
    # every runner stamps into its instance conf (VMF_BUNDLE_KEY).
    key = bundle_key(src)
    if key:
        os.environ["VMF_BUNDLE_KEY"] = key

    # Reattach first: a healthy running instance of this tree IS the
    # presentation — no boot, no race, no promotion. --new (or
    # --replace) forces a fresh instance instead.
    if reattach_allowed():
        inst = find_reattach(src)
        if inst:
            rname, target = inst
            say("already running: %s · %s (--new for a fresh instance)"
                % (rname, target))
            vmf_status.event(base, "pass", "already running: %s · %s"
                             % (rname, target), final=True)
            return 0

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
                     "compose_file": w.get("compose_file"),
                     "method": w.get("method") or "",
                     "cost": w.get("cost") or "",
                     "detail": w.get("image") or w.get("compose_file")
                     or "cached winner",
                     "cite": "", "cached": True}]
            say("winner cache: replay %s (%s)" % (
                w["approach"], w.get("url") or "same tree"))
            vmf_status.event(base, "replay", w["approach"])
            board = _open_board(base)
            if board:
                board.stage("replay " + (w.get("method") or w["approach"]))
                board.lane(w.get("method") or w["approach"], "plan",
                           w.get("image") or w.get("compose_file")
                           or "cached winner", "", "replay")
            rc = race(keep, src, base, board=board)
            _board_close()
            if rc == 0:
                return 0
            try:
                os.unlink(winner_path(src))
            except OSError:
                pass
            say("winner cache: replay failed; racing the full field")

    # The scout fast path: streaming routes race as they arrive. A
    # scout that yields nothing (off, transport failure, zero routes)
    # falls back to the fan-out below — never a new failure mode.
    if scout_enabled():
        feed = ScoutFeed(src, log=open(RACE_LOG, "a") if RACE_LOG else None)
        vmf_status.event(base, "scout", "reading repo + releases")
        say("scout: reading repo + releases")
        first = feed.wait_first(
            int(os.environ.get("VMF_SCOUT_FIRST") or 120))
        if first:
            say("scout: %d route(s) before the race; feed stays live"
                % len(first))
            vmf_status.event(base, "plan", "scout: %d route(s)"
                             % len(first))
            rc = race_scout(feed, first, src, base)
            if rc is not None:
                return rc
            say("scout: routes filtered out; falling back to fan-out")
        else:
            feed.kill()
            say("scout: no routes (%s); falling back to fan-out"
                % ("done" if not feed.alive() else "timeout"))
        if RACE_LOG:
            try:
                os.unlink(feed.out)
            except OSError:
                pass

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


def _open_board(base):
    # The board owns the terminal when it can (TTY, rich importable,
    # not VMF_LOUD); status files stay the source of truth either way.
    if os.environ.get("VMF_LOUD") == "1" or not vmf_ui.available():
        return None
    board = vmf_ui.Board(base)
    if not board.ok:
        return None
    vmf_status.set_quiet(True)
    _board_ref["b"] = board
    return board


def race_scout(feed, first, src, base):
    # The scout fast path: the first scouted route(s) enter the race
    # immediately; the feed keeps streaming later routes into the boot
    # loop, so a route the scout finds at t+30s still boots at t+35s.
    # The rich board (when the terminal allows it) renders the lane
    # view; status files and the race log stay the plain source of
    # truth. Scout routes are structurally pruned at clamp time (image,
    # ports, command), so no plan-stage prune here: the scout IS the
    # plan, and the verify arbitrates honestly.
    logdir = os.path.join(RUNS, "race-logs")
    os.makedirs(logdir, exist_ok=True)
    keep = []
    for r in first:
        i = len(keep) + 1
        if not _route_allowed(r, i):
            say("%d %s .. filtered" % (i, r.get("kind")))
            continue
        keep.append(_keep_entry(i, r, logdir, base))
    if not keep:
        feed.kill()
        return None
    board = _open_board(base)
    if board:
        board.stage("scout · reading repo + releases")
        for k in keep:
            board.lane(k["method"] or k["kind"], "plan",
                       k["detail"], k["cite"],
                       _band_label(k, band_of(k)))
    if not tranche_mode():
        say("scout: %d route(s) in flight; the scout keeps reading"
            % len(keep))
    rc = race(keep, src, base, feed=feed, board=board)
    _board_close()
    return rc



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


def _band_label(k, band):
    # Lane label: cost/T-band when the cost word exists, else just the
    # band. Legacy and replayed entries may have no cost word.
    c = (k.get("cost") or "").strip().lower()
    return "%s/T%d" % (c, band) if c in COST_BAND else "T%d" % band


def tranche_mode():
    return (os.environ.get("VMF_RACE_TRANCHE") or "all").strip().lower() \
        == "tranches"



def race(keep, src, base, feed=None, board=None):
    # The staggered boot loop, the crown, and the promotion. Keep
    # entries carry i/kind/cand/lp/image/ports/compose_file; scout
    # entries add method/cite/cost/detail. A live feed streams more
    # keep entries in as the scout emits them; the board (when active)
    # renders the same events as lanes.
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
        queue = []
    else:
        # all-mode: the queue holds everything from the start; the
        # scout feed appends behind it in emission order. (A group-pop
        # snapshot would drop feed entries drained before the first
        # pop.)
        groups = []
        queue = list(keep)
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
    while queue or running or groups or (feed and feed.alive()):
        now = time.time()
        # The feed drains first: a freshly scouted route joins the
        # queue in emission order, behind whatever is already queued.
        if feed:
            logdir = os.path.join(RUNS, "race-logs")
            for r in feed.drain():
                i = len(keep) + 1
                if not _route_allowed(r, i):
                    say("%d %s .. filtered" % (i, r.get("kind")))
                    if board:
                        board.lane(r.get("method") or "?", "parked",
                                   "filtered out")
                    continue
                k = _keep_entry(i, r, logdir, base)
                keep.append(k)
                queue.append(k)
                # Reruns reuse candidate names: a drained entry must
                # clear stale verdict markers like the initial keep.
                for p in (os.path.join(RUNS, "%s.verdict" % k["cand"]),
                          os.path.join(RUNS, k["cand"], "verdict")):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
                say("%d %s .. scouted (%s)" % (
                    i, r.get("kind"), r.get("cite")
                    or r.get("evidence") or ""))
                vmf_status.event(base, "plan", "scout: %s route"
                                 % (k["method"] or k["kind"]))
                if board:
                    board.lane(k["method"] or k["kind"], "plan",
                               k["detail"], k["cite"],
                               _band_label(k, band_of(k)))
            if not feed.alive() and not feed.summary_taken:
                feed.summary_taken = True
                s = feed.summary or {}
                if board:
                    board.chips(skipped=len(s.get("skipped") or []),
                                llm=s.get("llm") or 0)
                    if s.get("skipped"):
                        board.note("skipped · %s"
                                   % " · ".join(s["skipped"]))
                say("scout: done (%d route(s), %d llm call(s))"
                    % (feed.routes, s.get("llm") or 0))
        # Stagger: the next candidate starts STAGGER seconds after the
        # last race event (a start, a verdict, or a failure). Cheapest
        # boots first; usually it wins alone.
        if (queue and len(running) < PARALLEL
                and now - last_event >= STAGGER):
            k = queue.pop(0)
            cmd, env = runner_cmd(k["kind"], k["cand"], src, k["image"],
                                  k["ports"], k.get("compose_file"),
                                  k.get("method"))
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
            if board:
                board.lane(k["method"] or k["kind"], "booting",
                           k.get("detail"), k.get("cite"),
                           _band_label(k, band))
                board.stage(("scout + boot " if feed and feed.alive()
                             else "boot ") + (k["method"] or k["kind"]))
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
                if board:
                    ok = verdicts[cand] == "pass"
                    board.lane(r.get("method") or r["kind"],
                               "pass" if ok else "parked",
                               "" if ok else verdicts[cand][:40],
                               r.get("cite"))
                    if winner:
                        board.stage("pass")
                    elif not (feed and feed.alive()):
                        board.stage("boot")
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
            if feed and feed.alive():
                # The scout still reads; wait for its routes instead of
                # declaring the field empty.
                time.sleep(2)
                continue
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
        if board:
            board.stage("fail")
            board.close()
            vmf_status.set_quiet(False)
            vmf_status.line_closed()
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
    # The board rides through promotion: the winning lane goes
    # pass -> promoting -> pass (canonical target), and the board's
    # last line is the verdict. promote() closes it.
    return promote(w, src, base, winner, board)



def promote(w, src, base, winner, board=None):
    # Boot the winning candidate's spec under the canonical name; a
    # transient failure (partial boot, slow verdict) retries once. The
    # verdict marker must be unlinked before EVERY attempt: a stale
    # marker from a rerun would satisfy the wait loop instantly. With
    # a board, the winning lane rides through: pass (candidate
    # verdict) -> promoting (canonical boot from the winner data
    # drive) -> pass with the canonical target; the board's last line
    # IS the verdict.
    method = w.get("method") or w["kind"]
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
    if board:
        board.lane(method, "promoting", "canonical · winner drive",
                   w.get("cite"), _band_label(w, band_of(w)))
        board.stage("promote · booting canonical")
    promote_tries = int(os.environ.get("VMF_PROMOTION_TRIES") or "2")
    status = "fail (no verdict)"
    for attempt in range(1, promote_tries + 1):
        if attempt > 1:
            say("promotion: retry %d/%d (fresh boot under %s)"
                % (attempt, promote_tries, base))
            if board:
                board.lane(method, "promoting",
                           "canonical · retry %d" % attempt,
                           w.get("cite"), _band_label(w, band_of(w)))
        cmd, env = runner_cmd(w["kind"], base, src, w["image"],
                              w["ports"], w.get("compose_file"),
                              w.get("method"))
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
        t_promote = time.time()
        # The detached runner returns before its verify chain finishes: the
        # verdict marker (and the rendered target) land minutes later. Wait
        # for the marker instead of defaulting to pass on a missing verdict.
        # The wait must outlast the winner's verify window (a compose
        # candidate verifies for 1200s; a 900s wait would call it dead).
        vw = 1200 if w["kind"] == "compose" else 420
        deadline = time.time() + max(
            int(os.environ.get("VMF_PROMOTION_WAIT") or "900"), vw + 120)
        while not os.path.isfile(vpath) and time.time() < deadline:
            time.sleep(2)
            if board:
                el = int(time.time() - t_promote)
                board.stage("promote · booting canonical · %d:%02d"
                            % (el // 60, el % 60))
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
        if board:
            board.lane(method, "pass", target or "canonical",
                       w.get("cite"), _band_label(w, band_of(w)))
            board.stage("pass")
        vmf_status.event(base, "pass", target or "pass", final=True)
        _board_close()
    else:
        if board:
            board.lane(method, "parked", status[:40], w.get("cite"),
                       _band_label(w, band_of(w)))
            board.stage("fail")
            _board_close()
        vmf_status.event(base, "fail", status[:40], final=True)
    return 0 if status == "pass" else 1




if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _die)
    signal.signal(signal.SIGINT, _die)
    sys.exit(main(sys.argv))
