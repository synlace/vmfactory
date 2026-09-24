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

import vmf_race  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmf-race-")
        self.addCleanup(shutil.rmtree, self.tmp)
        vmf_race.GEN = os.path.join(self.tmp, "generated")


class TreeKey(Tmp):
    def _dir(self, name=None, content="hello"):
        d = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        if name:
            n = os.path.join(d, name)
            os.makedirs(n)
            d = n
        with open(os.path.join(d, "f.txt"), "w") as f:
            f.write(content)
        return d

    def test_same_content_same_key(self):
        self.assertEqual(vmf_race.tree_key(self._dir()),
                         vmf_race.tree_key(self._dir()))

    def test_content_change_moves_key(self):
        a = vmf_race.tree_key(self._dir())
        b = vmf_race.tree_key(self._dir(content="changed"))
        self.assertNotEqual(a, b)

    def test_git_head_and_url_drive_key(self):
        d = self._dir()
        def git(*args):
            subprocess.run(["git", "-C", d, *args],
                           capture_output=True, check=True)
        git("init", "-q")
        git("add", ".")
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "init")
        git("remote", "add", "origin", "https://example.com/app.git")
        k1 = vmf_race.tree_key(d)
        # Same tree state: the key is stable.
        self.assertEqual(k1, vmf_race.tree_key(d))
        # A new commit moves the key even though the files match
        # relpath+size (git identity is the primary signal).
        with open(os.path.join(d, "g.txt"), "w") as f:
            f.write("x")
        git("add", ".")
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "second")
        self.assertNotEqual(k1, vmf_race.tree_key(d))


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
        self.old_key = vmf_race.tree_key
        vmf_race.stop_vm = lambda name: self.stops.append(name)
        vmf_race.tree_key = lambda src: "testkey"
        self.old_tries = os.environ.pop("VMF_PROMOTION_TRIES", None)
        self.old_wait = os.environ.pop("VMF_PROMOTION_WAIT", None)
        os.environ["VMF_PROMOTION_WAIT"] = "1"

    def tearDown(self):
        vmf_race.stop_vm = self.old_stop
        vmf_race.runner_cmd = self.old_cmd
        vmf_race.subprocess.Popen = self.old_popen
        vmf_race.tree_key = self.old_key
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


if __name__ == "__main__":
    unittest.main()