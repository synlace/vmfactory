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


if __name__ == "__main__":
    unittest.main()