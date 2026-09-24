# Contract tests for the one-line status writer (scripts/vmf_status.py).
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import vmf_race  # noqa: E402
import vmf_status  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmf-status-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.old_runs = vmf_status.RUNS
        vmf_status.RUNS = self.tmp
        self.old_env = os.environ.pop("VMF_STATUS", None)
        # Tests are never a TTY: default mode is log.
        os.environ.pop("VMF_STATUS", None)

    def tearDown(self):
        vmf_status.RUNS = self.old_runs
        if self.old_env is None:
            os.environ.pop("VMF_STATUS", None)
        else:
            os.environ["VMF_STATUS"] = self.old_env

    def status_path(self, name):
        return os.path.join(self.tmp, ".status", name)


class Fmt(Tmp):
    def test_line_shape(self):
        t0 = time.time() - 75
        line = vmf_status.fmt("ghost-4", "boot", "c1 prebuilt boot", t0)
        self.assertRegex(line, r"^ghost-4\s+boot\s+c1 prebuilt boot\s+t\+\d+:\d+")

    def test_detail_clamped(self):
        line = vmf_status.fmt("n", "boot", "x" * 80, time.time())
        self.assertIn("x" * 40 + " ", line)
        self.assertNotIn("x" * 41, line)


class Event(Tmp):
    def test_writes_status_file(self):
        vmf_status.begin("web")
        vmf_status.event("web", "boot", "booting")
        p = os.path.join(self.tmp, ".status", "web")
        self.assertTrue(os.path.isfile(p))
        self.assertIn("web", open(p).read())
        self.assertIn("boot", open(p).read())

    def test_log_mode_final_only(self):
        # Non-TTY (log mode): intermediate events render nothing; the
        # final event prints one clean line with a newline.
        err = io.StringIO()
        with redirect_stderr(err):
            vmf_status.event("web", "boot", "booting")
            self.assertEqual(err.getvalue(), "")
            vmf_status.event("web", "pass", "http://x", final=True)
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("pass", err.getvalue())

    def test_tty_mode_rewrites_without_newline(self):
        os.environ["VMF_STATUS"] = "tty"
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                vmf_status.event("web", "boot", "booting")
            self.assertEqual(err.getvalue().count("\n"), 0)
            self.assertTrue(err.getvalue().startswith("\r"))
            with redirect_stderr(err):
                vmf_status.event("web", "pass", "ok", final=True)
            # The final event terminates the open CR line first, so the
            # verdict lands on its own line (2 newlines total).
            self.assertEqual(err.getvalue().count("\n"), 2)
            self.assertIn("\nweb", err.getvalue())
        finally:
            del os.environ["VMF_STATUS"]
            vmf_status.line_closed()

    def test_off_mode_silent(self):
        os.environ["VMF_STATUS"] = "off"
        try:
            out = io.StringIO()
            with redirect_stderr(out):
                vmf_status.event("web", "fail", "boom", final=True)
            self.assertEqual(out.getvalue(), "")
            # The status file still lands (watch keeps working).
            self.assertTrue(os.path.isfile(
                os.path.join(self.tmp, ".status", "web")))
        finally:
            del os.environ["VMF_STATUS"]


class RaceSay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmf-say-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.old = (vmf_race.RACE_LOG, vmf_race.SAY_STDERR)
        vmf_race.RACE_LOG = os.path.join(self.tmp, "race.log")
        vmf_race.SAY_STDERR = False

    def tearDown(self):
        vmf_race.RACE_LOG = None
        vmf_race.SAY_STDERR = True

    def test_say_goes_to_race_log_not_stderr(self):
        err = io.StringIO()
        with redirect_stderr(err):
            vmf_race.say("hello")
        self.assertEqual(err.getvalue(), "")
        with open(os.path.join(self.tmp, "race.log")) as f:
            self.assertEqual(f.read(), "race: hello\n")

    def test_loud_mirrors_to_stderr(self):
        os.environ["VMF_LOUD"] = "1"
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                vmf_race.say("hello")
            self.assertIn("race: hello", err.getvalue())
        finally:
            os.environ.pop("VMF_LOUD", None)


if __name__ == "__main__":
    unittest.main()
