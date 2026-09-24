# Contract tests for the race winner cache (scripts/vmf_race.py).
#
# Run: uv run --with pyyaml --with jsonschema python -m unittest discover tests
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import vmf_plan  # noqa: E402
import vmf_race  # noqa: E402
import vmf_status  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmf-race-")
        self.addCleanup(shutil.rmtree, self.tmp)
        vmf_race.GEN = os.path.join(self.tmp, "generated")
        # main()/race() emit one-line status events; keep them out of
        # the real ~/.vmf/runs/.status.
        self.old_status_runs = vmf_status.RUNS
        vmf_status.RUNS = self.tmp
        self.addCleanup(setattr, vmf_status, "RUNS", self.old_status_runs)
        # main() must never spawn the scout subprocess (a real LLM
        # call); scout wiring is test_scout.py's job.
        self.old_scout_env = os.environ.pop("VMF_RACE_SCOUT", None)
        os.environ["VMF_RACE_SCOUT"] = "0"
        self.addCleanup(self._restore_scout_env)

    def _restore_scout_env(self):
        os.environ.pop("VMF_RACE_SCOUT", None)
        if self.old_scout_env is not None:
            os.environ["VMF_RACE_SCOUT"] = self.old_scout_env


class WinnerKey(unittest.TestCase):
    def _dir(self, content="hello", compose=None):
        d = tempfile.mkdtemp(prefix="vmf-src-")
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "README.md"), "w") as f:
            f.write(content)
        if compose is not None:
            with open(os.path.join(d, "docker-compose.yml"), "w") as f:
                f.write(compose)
        return d

    def test_same_content_same_key(self):
        self.assertEqual(vmf_plan.winner_key(self._dir()),
                         vmf_plan.winner_key(self._dir()))

    def test_content_change_moves_key(self):
        a = vmf_plan.winner_key(self._dir())
        b = vmf_plan.winner_key(self._dir(content="changed"))
        self.assertNotEqual(a, b)

    def test_compose_change_moves_key(self):
        a = vmf_plan.winner_key(self._dir(compose="services: {}"))
        b = vmf_plan.winner_key(self._dir(compose="services: {app: {image: x}}"))
        self.assertNotEqual(a, b)

    def test_git_head_moves_keep_key(self):
        # The fix vs the old tree_key: an upstream commit that changes
        # nothing plan-relevant must NOT rotate the winner cache key.
        d = self._dir()

        def git(*args):
            subprocess.run(["git", "-C", d, *args],
                           capture_output=True, check=True)
        git("init", "-q")
        git("add", ".")
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "init", "--allow-empty")
        k1 = vmf_plan.winner_key(d)
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "hourly upstream move", "--allow-empty")
        self.assertEqual(k1, vmf_plan.winner_key(d))

    def test_subcommand_prints_key(self):
        d = self._dir()
        py = sys.executable
        r = subprocess.run([py, os.path.join(SCRIPTS, "vmf_plan.py"),
                            "cache-key", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout.strip(), r"^[0-9a-f]{12}$")
        self.assertEqual(r.stdout.strip(), vmf_plan.winner_key(d))


class BundleKey(Tmp):
    def test_wrapper_matches_plan_key(self):
        src = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        with open(os.path.join(src, "package.json"), "w") as f:
            f.write('{"name": "app"}')
        self.assertEqual(vmf_race.bundle_key(src),
                         vmf_plan.winner_key(src))

    def test_wrapper_none_on_failure(self):
        self.assertIsNone(vmf_race.bundle_key(
            os.path.join(self.tmp, "does-not-exist")))


class WinnerRoundTrip(Tmp):
    def test_save_load_roundtrip(self):
        src = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        w = {"kind": "install_script", "image": "node:22",
             "ports": [8080], "compose_file": None}
        vmf_race.save_winner(src, w, "web-c1")
        p = vmf_race.winner_path(src)
        self.assertTrue(os.path.isfile(p))
        rec = vmf_race.load_winner(src)
        self.assertEqual(rec["approach"], "install_script")
        self.assertEqual(rec["ports"], [8080])
        self.assertEqual(rec["by"], "web-c1")
        self.assertIn("created", rec)

    def test_missing_cache_returns_none(self):
        src = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        self.assertIsNone(vmf_race.load_winner(src))

    def test_corrupt_cache_returns_none(self):
        src = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        os.makedirs(os.path.dirname(vmf_race.winner_path(src)), exist_ok=True)
        open(vmf_race.winner_path(src), "w").write("{broken")
        self.assertIsNone(vmf_race.load_winner(src))


class CacheAllowed(unittest.TestCase):
    def _clear(self):
        for k in ("VMF_RACE_MODE", "VMF_RACE_SKIP_CACHE",
                  "VMF_RACE_APPROACH", "VMF_RACE_SKIP"):
            os.environ.pop(k, None)

    def setUp(self):
        self._clear()
        self.addCleanup(self._clear)

    def test_default_yes(self):
        self.assertTrue(vmf_race.cache_allowed())

    def test_plan_mode_no(self):
        os.environ["VMF_RACE_MODE"] = "plan"
        self.assertFalse(vmf_race.cache_allowed())

    def test_skip_flag_no(self):
        os.environ["VMF_RACE_SKIP_CACHE"] = "1"
        self.assertFalse(vmf_race.cache_allowed())

    def test_filters_no(self):
        os.environ["VMF_RACE_APPROACH"] = "compose"
        self.assertFalse(vmf_race.cache_allowed())
        os.environ.pop("VMF_RACE_APPROACH")
        os.environ["VMF_RACE_SKIP"] = "compose"
        self.assertFalse(vmf_race.cache_allowed())


class Promotion(Tmp):
    def setUp(self):
        super().setUp()
        self.runs = tempfile.mkdtemp(prefix="vmf-runs-", dir=self.tmp)
        self.src = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        vmf_race.RUNS = self.runs
        self.stops = []
        self.boots = []
        self.old_stop, self.old_cmd = vmf_race.stop_vm, vmf_race.runner_cmd
        self.old_popen = vmf_race.subprocess.Popen
        self.old_key = vmf_race.bundle_key
        vmf_race.stop_vm = lambda name: self.stops.append(name)
        vmf_race.bundle_key = lambda src: "testkey"
        self.old_tries = os.environ.pop("VMF_PROMOTION_TRIES", None)
        self.old_wait = os.environ.pop("VMF_PROMOTION_WAIT", None)
        os.environ["VMF_PROMOTION_WAIT"] = "1"

    def tearDown(self):
        vmf_race.stop_vm = self.old_stop
        vmf_race.runner_cmd = self.old_cmd
        vmf_race.subprocess.Popen = self.old_popen
        vmf_race.bundle_key = self.old_key
        if self.old_tries is None:
            os.environ.pop("VMF_PROMOTION_TRIES", None)
        else:
            os.environ["VMF_PROMOTION_TRIES"] = self.old_tries
        if self.old_wait is None:
            os.environ.pop("VMF_PROMOTION_WAIT", None)
        else:
            os.environ["VMF_PROMOTION_WAIT"] = self.old_wait

    def _w(self):
        return {"i": 1, "kind": "install_script", "cand": "web-c1",
                "lp": os.path.join(self.runs, "web-c1.log"),
                "image": None, "ports": [], "compose_file": None}

    def _fake_boots(self, verdicts):
        # Each fake boot writes the next verdict marker on wait() —
        # like a detached runner chain would.
        vdir = self.runs

        def popen(cmd, env=None, stdout=None, stderr=None, **_kw):
            self.boots.append(cmd)
            idx = len(self.boots) - 1

            class P:
                def __init__(self, idx):
                    self.idx = idx
                def __enter__(self):
                    return self
                def __exit__(self, *_a):
                    return False
                def wait(self):
                    v = verdicts[min(self.idx, len(verdicts) - 1)]
                    open(os.path.join(vdir, "web.verdict"), "w").write(v)
                    return 0
            return P(idx)
        return popen

    def test_retry_recovers_transient_failure(self):
        os.environ["VMF_PROMOTION_TRIES"] = "2"
        vmf_race.subprocess.Popen = self._fake_boots(
            ["fail (probe timeout)", "pass"])
        rc = vmf_race.promote(self._w(), self.src, "web", "web-c1")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.boots), 2)
        self.assertIn("web", self.stops)
        rec = vmf_race.load_winner(self.src)
        self.assertEqual(rec["approach"], "install_script")

    def test_both_attempts_fail_exit_one(self):
        os.environ["VMF_PROMOTION_TRIES"] = "2"
        vmf_race.subprocess.Popen = self._fake_boots(
            ["fail (probe timeout)", "fail (probe timeout)"])
        rc = vmf_race.promote(self._w(), self.src, "web", "web-c1")
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.boots), 2)
        self.assertIsNone(vmf_race.load_winner(self.src))

    def test_stale_verdict_marker_never_satisfies_wait(self):
        # A leftover verdict from a rerun must be unlinked before the
        # first boot, or the wait loop returns instantly.
        open(os.path.join(self.runs, "web.verdict"), "w").write("fail (x)")
        os.environ["VMF_PROMOTION_TRIES"] = "1"
        popen = self._fake_boots(["pass"])
        vmf_race.subprocess.Popen = popen
        rc = vmf_race.promote(self._w(), self.src, "web", "web-c1")
        self.assertEqual(rc, 0)
        # The marker read after boot is the new one, not the stale one.
        with open(os.path.join(self.runs, "web.verdict")) as f:
            self.assertEqual(f.read().strip(), "pass")

    def test_symlink_repoint_verdict_found(self):
        # The pre-boot symlink points at an OLD instance with no
        # verdict, so verdict_path() freezes on the flat fallback; the
        # boot repoints the symlink to a new id dir and the chain
        # writes the verdict THERE. The wait loop re-resolves the path
        # every poll and finds it (regression: 900s spin, then the
        # retry killed the healthy instance).
        olddir = os.path.join(self.runs, "cafe00000000")
        newdir = os.path.join(self.runs, "cafe00011111")
        os.makedirs(olddir)
        os.makedirs(newdir)
        os.environ["VMF_PROMOTION_TRIES"] = "1"
        os.symlink("cafe00000000", os.path.join(self.runs, "web"))

        def popen(cmd, env=None, stdout=None, stderr=None, **_kw):
            # The boot repoints the symlink, then the chain writes the
            # verdict inside the new id dir (as oci-run's chain does).
            link = os.path.join(self.runs, "web")
            tmp = link + ".tmp"
            os.symlink("cafe00011111", tmp)
            os.replace(tmp, link)
            open(os.path.join(newdir, "verdict"), "w").write("pass")

            class P:
                def wait(self):
                    return 0
            return P()
        vmf_race.subprocess.Popen = popen
        rc = vmf_race.promote(self._w(), self.src, "web", "web-c1")
        self.assertEqual(rc, 0)

    def test_board_rides_through_promotion(self):
        # The winning lane goes promoting -> pass (canonical target) on
        # the board; the plain final render is suppressed while the
        # board owns the terminal, but the status file still records.
        os.environ["VMF_PROMOTION_TRIES"] = "1"
        vmf_race.subprocess.Popen = self._fake_boots(["pass"])
        rec = []

        class FakeBoard:
            def lane(self, *a):
                rec.append(("lane",) + a)
            def stage(self, *a):
                rec.append(("stage",) + a)
            def close(self):
                rec.append(("close",))
        board = FakeBoard()
        vmf_race._board_ref["b"] = board
        vmf_status.set_quiet(True)
        try:
            rc = vmf_race.promote(self._w(), self.src, "web", "web-c1",
                                  board)
            self.assertEqual(rc, 0)
        finally:
            vmf_race._board_ref.pop("b", None)
            vmf_status.set_quiet(False)
            vmf_status.line_closed()
        lanes = [r[2] for r in rec if r[0] == "lane"]
        self.assertEqual(lanes, ["promoting", "pass"])
        self.assertIn(("stage", "pass"), rec)
        self.assertIn(("close",), rec)
        with open(os.path.join(vmf_status.RUNS, ".status", "web")) as f:
            self.assertIn("pass", f.read())


class CacheBeforeEnumerate(Tmp):
    # The rekey contract: a winner-cache hit replays without ever
    # calling load_approaches (the enumerate LLM call).
    def setUp(self):
        super().setUp()
        self.runs = tempfile.mkdtemp(prefix="vmf-runs-", dir=self.tmp)
        self.src = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        with open(os.path.join(self.src, "package.json"), "w") as f:
            f.write('{"name": "app"}')
        vmf_race.RUNS = self.runs
        self.old = (vmf_race.bundle_key, vmf_race.load_winner,
                    vmf_race.load_approaches, vmf_race.race)
        self.raced = []
        self.enum_calls = []
        vmf_race.bundle_key = lambda src: "abc123def456"
        vmf_race.load_winner = lambda src: {
            "approach": "install_script", "image": None, "ports": [],
            "compose_file": None}
        vmf_race.load_approaches = self._enum
        vmf_race.race = lambda keep, src, base, feed=None, board=None: (
            self.raced.append((keep, base)) or 0)
        self.env_backup = {}
        for k in ("VMF_RACE_MODE", "VMF_RACE_SKIP_CACHE",
                  "VMF_RACE_APPROACH", "VMF_RACE_SKIP", "VMF_NAME",
                  "VMF_RACE_SCOUT"):
            self.env_backup[k] = os.environ.pop(k, None)
        os.environ["VMF_RACE_SCOUT"] = "0"

    def tearDown(self):
        (vmf_race.bundle_key, vmf_race.load_winner,
         vmf_race.load_approaches, vmf_race.race) = self.old
        for k, v in self.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _enum(self, src):
        self.enum_calls.append(src)
        return []

    def test_cache_hit_skips_enumerate(self):
        rc = vmf_race.main(["race", self.src])
        self.assertEqual(rc, 0)
        self.assertEqual(self.enum_calls, [])
        self.assertEqual(len(self.raced), 1)
        self.assertTrue(self.raced[0][0][0].get("cached"))

    def test_cache_miss_runs_enumerate(self):
        vmf_race.load_winner = lambda src: None
        rc = vmf_race.main(["race", self.src])
        self.assertEqual(rc, 1)
        self.assertEqual(self.enum_calls, [self.src])
        self.assertEqual(self.raced, [])


class FakeBoot:
    # A runner replacement: first poll() writes the verdict marker and
    # reports the runner exited (0 on pass, 1 on fail) — the shape the
    # race loop's poll path expects.
    def __init__(self, runs_dir, cand, verdict):
        self.runs_dir, self.cand, self.verdict = runs_dir, cand, verdict
        self.polled = 0

    def poll(self):
        self.polled += 1
        if self.polled == 1:
            open(os.path.join(self.runs_dir, "%s.verdict" % self.cand),
                 "w").write(self.verdict)
            return 0 if self.verdict == "pass" else 1
        return 1

    def terminate(self):
        pass


class Tranches(Tmp):
    def setUp(self):
        super().setUp()
        self.runs = tempfile.mkdtemp(prefix="vmf-runs-", dir=self.tmp)
        self.src = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        vmf_race.RUNS = self.runs
        self.boots = []
        self.stops = []
        self.old = (vmf_race.runner_cmd, vmf_race.stop_vm,
                    vmf_race.subprocess.Popen, vmf_race.promote,
                    vmf_race.bundle_key, vmf_race.STAGGER,
                    vmf_race.DEADLINE, vmf_race.PARALLEL)
        vmf_race.stop_vm = lambda name: self.stops.append(name)
        vmf_race.promote = lambda w, src, base, winner, board=None: 0
        vmf_race.bundle_key = lambda src: "testkey"
        vmf_race.STAGGER = 0
        vmf_race.DEADLINE = 600
        self.env_backup = {}
        for k in ("VMF_RACE_TRANCHE",):
            self.env_backup[k] = os.environ.pop(k, None)

    def tearDown(self):
        (vmf_race.runner_cmd, vmf_race.stop_vm,
         vmf_race.subprocess.Popen, vmf_race.promote,
         vmf_race.bundle_key, vmf_race.STAGGER,
         vmf_race.DEADLINE, vmf_race.PARALLEL) = self.old
        for k, v in self.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _keep(self, entries):
        out = []
        for i, (kind, verdict) in enumerate(entries, 1):
            cand = "x-c%d" % i
            out.append({"i": i, "kind": kind, "cand": cand,
                        "lp": os.path.join(self.runs, "%s.log" % cand),
                        "image": None, "ports": [], "compose_file": None,
                        "verdict": verdict})
        return out

    def _popen_for(self, keep):
        by_cand = {k["cand"]: k for k in keep}

        def popen(cmd, env=None, stdout=None, stderr=None, **_kw):
            name = env["VMF_NAME"]
            self.boots.append(name)
            fake = FakeBoot(self.runs, name, by_cand[name]["verdict"])

            class P:
                def __init__(self):
                    self.fake = fake
                def poll(self):
                    return fake.poll()
                def terminate(self):
                    pass
            return P()
        return popen

    def test_cheap_band_boots_before_expensive(self):
        os.environ["VMF_RACE_TRANCHE"] = "tranches"
        keep = self._keep([("install_script", "pass"),   # T2
                           ("prebuilt_image", "pass")])  # T0
        vmf_race.subprocess.Popen = self._popen_for(keep)
        rc = vmf_race.race(keep, self.src, "x")
        self.assertEqual(rc, 0)
        self.assertEqual(self.boots, ["x-c2"])  # c1 never booted

    def test_escalation_after_band_failure(self):
        os.environ["VMF_RACE_TRANCHE"] = "tranches"
        keep = self._keep([("prebuilt_image", "fail (probe timeout)"),  # T0
                           ("install_script", "pass")])                 # T2
        vmf_race.subprocess.Popen = self._popen_for(keep)
        rc = vmf_race.race(keep, self.src, "x")
        self.assertEqual(rc, 0)
        self.assertEqual(self.boots, ["x-c1", "x-c2"])

    def test_all_mode_boots_everything_staggered(self):
        keep = self._keep([("install_script", "fail"),   # T2
                           ("prebuilt_image", "pass")])  # T0
        vmf_race.subprocess.Popen = self._popen_for(keep)
        rc = vmf_race.race(keep, self.src, "x")
        self.assertEqual(rc, 0)
        # all-mode: both boot (queue order), the pass crowns.
        self.assertEqual(self.boots, ["x-c1", "x-c2"])

    def test_cost_word_refines_band(self):
        self.assertEqual(vmf_race.band_of(
            {"kind": "install_script", "cost": "fast"}), 0)
        self.assertEqual(vmf_race.band_of({"kind": "compose"}), 1)
        self.assertEqual(vmf_race.band_of({"kind": "weird"}), 3)


class Reattach(Tmp):
    def setUp(self):
        super().setUp()
        self.runs = tempfile.mkdtemp(prefix="vmf-runs-", dir=self.tmp)
        self.src = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        with open(os.path.join(self.src, "package.json"), "w") as f:
            f.write('{"name": "app"}')
        vmf_race.RUNS = self.runs
        vmf_status.RUNS = self.runs
        self.old_key = vmf_race.bundle_key
        vmf_race.bundle_key = lambda src: "reattachkey"
        self.socks = []
        self.env_backup = {}
        for k in ("VMF_RACE_MODE", "VMF_RACE_SKIP_CACHE", "VMF_RACE_NEW",
                  "VMF_RACE_APPROACH", "VMF_RACE_SKIP", "VMF_NAME",
                  "VMF_BUNDLE_KEY"):
            self.env_backup[k] = os.environ.pop(k, None)

    def tearDown(self):
        vmf_race.bundle_key = self.old_key
        for s in self.socks:
            s.close()
        for k, v in self.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _port(self):
        import socket as sockmod
        s = sockmod.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.socks.append(s)
        return s.getsockname()[1]

    def _conf(self, name, key="reattachkey", pid=None, target=None):
        d = os.path.join(self.runs, "deadbeefcafe")
        os.makedirs(d, exist_ok=True)
        lines = ["ID=deadbeefcafe", "NAME=%s" % name,
                 "KEY=%s" % key, "PID=%s" % (pid if pid else os.getpid())]
        if target:
            lines.append("TARGET=%s" % target)
        with open(os.path.join(d, "conf"), "w") as f:
            f.write("\n".join(lines) + "\n")

    def test_healthy_instance_presents(self):
        port = self._port()
        self._conf("cyberchef-2", target="tcp://127.0.0.1:%d" % port)
        self.assertEqual(vmf_race.find_reattach(self.src),
                         ("cyberchef-2", "tcp://127.0.0.1:%d" % port))

    def test_key_mismatch_skips(self):
        port = self._port()
        self._conf("other", key="othertree", target="tcp://127.0.0.1:%d" % port)
        self.assertIsNone(vmf_race.find_reattach(self.src))

    def test_dead_pid_skips(self):
        port = self._port()
        self._conf("cyberchef-2", pid=999999,
                   target="tcp://127.0.0.1:%d" % port)
        self.assertIsNone(vmf_race.find_reattach(self.src))

    def test_dead_target_skips(self):
        port = self._port()
        self.socks[-1].close()
        self.socks.pop()
        self._conf("cyberchef-2", target="tcp://127.0.0.1:%d" % port)
        self.assertIsNone(vmf_race.find_reattach(self.src))

    def test_new_flag_disables(self):
        os.environ["VMF_RACE_NEW"] = "1"
        self.assertFalse(vmf_race.reattach_allowed())

    def test_main_reattaches_without_race(self):
        port = self._port()
        self._conf("cyberchef-2", target="tcp://127.0.0.1:%d" % port)
        self.raced = []
        old = (vmf_race.race, vmf_race.load_approaches)
        vmf_race.race = lambda *a, **k: self.raced.append(a) or 0
        vmf_race.load_approaches = lambda src: self.fail("must not plan")
        try:
            rc = vmf_race.main(["race", self.src])
            self.assertEqual(rc, 0)
            self.assertEqual(self.raced, [])
            self.assertEqual(os.environ.get("VMF_BUNDLE_KEY"), "reattachkey")
            st = os.path.join(self.runs, ".status")
            self.assertTrue(any(
                "already running: cyberchef-2" in open(os.path.join(st, f)).read()
                for f in os.listdir(st)))
        finally:
            (vmf_race.race, vmf_race.load_approaches) = old
            os.environ.pop("VMF_BUNDLE_KEY", None)


if __name__ == "__main__":
    unittest.main()