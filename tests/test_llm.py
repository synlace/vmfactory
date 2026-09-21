# Contract tests for the single LLM seam (scripts/vmf_llm.py) and the
# intent subcommand it powers (vmf_plan.py intent, the former
# plan-image.py flow).
#
# The LLM transport and context7 are stubs under tests/fixtures; HOME
# is redirected so the intent cache lands in the test sandbox.
#
# Run: uv run --with pyyaml --with jsonschema python -m unittest discover tests
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, SCRIPTS)

import vmf_llm  # noqa: E402
import vmf_plan  # noqa: E402


class ParseLlmJson(unittest.TestCase):
    def test_variants(self):
        cases = [
            ('{"a": 1}', {"a": 1}),
            ('```\n{"a": 1}\n```', {"a": 1}),
            ('```json\n{"a": 1}\n```', {"a": 1}),
            ('```JSON\n{"a": 1}\n```', {"a": 1}),
            ('  {"a": 1}  ', {"a": 1}),
            ('"{\\"a\\": 1}"', {"a": 1}),
        ]
        for raw, want in cases:
            self.assertEqual(vmf_llm.parse_llm_json(raw), want, raw)

    def test_garbage_raises(self):
        with self.assertRaises(Exception):
            vmf_llm.parse_llm_json("not json at all")


class GroundWithStubs(unittest.TestCase):
    def setUp(self):
        self.old_scripts = os.environ.get("VMF_SCRIPTS_DIR")
        os.environ["VMF_SCRIPTS_DIR"] = os.path.join(FIXTURES, "c7-stub")
        self.addCleanup(self._restore_scripts)

    def _restore_scripts(self):
        if self.old_scripts is None:
            os.environ.pop("VMF_SCRIPTS_DIR", None)
        else:
            os.environ["VMF_SCRIPTS_DIR"] = self.old_scripts

    def test_grounded(self):
        grounded, ids = vmf_llm.ground(["kilo code cli"])
        self.assertIn("context7: stub/lib", grounded)
        self.assertEqual(ids, ["stub/lib [kilo code cli]"])

    def test_degrades_when_transport_dead(self):
        os.environ["VMF_SCRIPTS_DIR"] = os.path.join(FIXTURES, "empty-stub")
        grounded, ids = vmf_llm.ground(["topic"])
        self.assertIn("state facts", grounded)
        self.assertEqual(ids, [])

    def test_note(self):
        self.assertIn("grounded via context7", vmf_llm.grounding_note(["x [t]"]))
        self.assertIn("NOT grounded", vmf_llm.grounding_note([]))


class IntentCmd(unittest.TestCase):
    """The plain-image intent flow, end to end, with stub transports."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmf-intent-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fixture = os.path.join(FIXTURES, "intent-stub")

    def _plan(self, home, image, phrase, out, extra_env=None):
        env = dict(os.environ,
                   VMF_SCRIPTS_DIR=self.fixture,
                   HOME=home)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"),
             "intent", image, phrase, out],
            capture_output=True, text=True, env=env)

    def test_plan_gate_yes_and_cache(self):
        calls = os.path.join(self.fixture, "calls")
        open(calls, "w").write("0")
        home = os.path.join(self.tmp, "home")
        os.mkdir(home)
        out = os.path.join(self.tmp, "direct.json")
        proc = self._plan(home, "ubuntu", "install the kilo cli", out,
                          {"VMF_RUN_YES": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = json.load(open(out))
        self.assertEqual(plan["command"], ["kilo", "serve"])
        self.assertEqual(plan["install"], ["apt-get update", "npm i -g kilo"])
        self.assertEqual(plan["ports"], [1337])
        self.assertEqual(plan["memory_mb"], 2048)
        self.assertEqual(plan["base_image"], "")
        # cache exists under the redirected HOME
        import hashlib
        norm = " ".join("install the kilo cli".split()).casefold()
        key = hashlib.sha256(("image-intent-v4\nubuntu\n%s" % norm)
                             .encode()).hexdigest()[:12]
        self.assertTrue(os.path.isfile(os.path.join(
            home, ".vmf", "generated", "img-" + key, "direct.json")))
        # second run: cache hit, stub not called again
        n = int(open(calls).read())
        proc2 = self._plan(home, "ubuntu", "install the kilo cli", out,
                           {"VMF_RUN_YES": "1"})
        self.assertEqual(proc2.returncode, 0)
        self.assertIn("cache hit", proc2.stderr)
        self.assertEqual(int(open(calls).read()), n)

    def test_no_command_exit_1(self):
        fixture = os.path.join(FIXTURES, "intent-no-cmd")
        calls = os.path.join(fixture, "calls")
        open(calls, "w").write("0")
        out = os.path.join(self.tmp, "direct2.json")
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"),
             "intent", "ubuntu", "do a thing", out],
            capture_output=True, text=True,
            env=dict(os.environ, VMF_SCRIPTS_DIR=fixture, VMF_RUN_YES="1"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no command", proc.stderr)

    def test_not_approved_exit_2(self):
        # no tty in the test sandbox -> gate declines -> honest exit 2
        fixture = os.path.join(FIXTURES, "intent-stub")
        open(os.path.join(fixture, "calls"), "w").write("0")
        out = os.path.join(self.tmp, "direct3.json")
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"),
             "intent", "ubuntu", "other phrase", out],
            capture_output=True, text=True,
            env=dict(os.environ, VMF_SCRIPTS_DIR=fixture))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not approved", proc.stderr)


class RefineLoop(unittest.TestCase):
    """The a / n / free-text gate loop, with the tty and HOME scripted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmf-refine-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self._orig_tty_line = vmf_llm.tty_line
        self._env_backup = {k: os.environ.get(k)
                            for k in ("VMF_SCRIPTS_DIR", "HOME", "VMF_RUN_YES")}

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        vmf_llm.tty_line = self._orig_tty_line

    def _script(self, lines, scripts="gapfill-stub"):
        vmf_llm.tty_line = lambda prompt: lines.pop(0)
        os.environ["VMF_SCRIPTS_DIR"] = os.path.join(FIXTURES, scripts)
        os.environ["HOME"] = self.tmp
        # stub call counters persist between runs; reset them
        open(os.path.join(FIXTURES, scripts, "calls"), "w").write("0")

    def _repo(self, name):
        root = os.path.join(self.tmp, name)
        os.mkdir(root)
        open(os.path.join(root, "README.md"), "w").write(
            "# app\nInstall: pip install app\nRun: app --port 8080\n")
        return root

    def test_free_text_refines_then_accepts(self):
        root = self._repo("repo")
        self._script(["use port 9000 instead", "a"])
        err = io.StringIO()
        with redirect_stderr(err):
            try:
                vmf_plan.gapfill(root, os.path.join(self.tmp, "plan.json"))
            except SystemExit as e:
                self.assertEqual(e.code, 0)
        out = os.path.join(self.tmp, "direct.json")
        plan = json.load(open(out))
        self.assertEqual(plan["command"], ["app", "--port", "9000"])
        self.assertIn("diff vs previous: command, notes changed", err.getvalue())
        import glob as _glob
        metas = _glob.glob(os.path.join(self.tmp, ".vmf", "generated",
                                        "*", "direct.json.meta.json"))
        self.assertEqual(len(metas), 1)
        meta = json.load(open(metas[0]))
        self.assertTrue(meta["refined"])
        self.assertEqual(meta["turns"], 1)

    def test_abort_still_exit_2(self):
        root = self._repo("repo2")
        self._script(["n"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            vmf_plan.gapfill(root, os.path.join(self.tmp, "plan.json"))
        self.assertEqual(cm.exception.code, 2)

    def test_overlay_loop_refines_then_applies(self):
        plan = os.path.join(self.tmp, "plan.json")
        json.dump({"services": [{"name": "web", "ports": []},
                                {"name": "db", "ports": []}]}, open(plan, "w"))
        out = os.path.join(self.tmp, "refines.json")
        self._script(["also raise it to 5 instances", "a"], scripts="refine-stub")
        err = io.StringIO()
        with redirect_stderr(err):
            vmf_plan.refine_cmd(plan, out, "run several instances with a greeting")
        overlay = json.load(open(out))
        self.assertEqual(overlay["replicas"], {"web": 5})
        self.assertEqual(overlay["env"], {})
        self.assertIn("scaling web to 3", err.getvalue())
        self.assertIn("diff vs previous", err.getvalue())

    def test_declined_overlay_continues_empty(self):
        plan = os.path.join(self.tmp, "plan2.json")
        json.dump({"services": [{"name": "web", "ports": []}]}, open(plan, "w"))
        out = os.path.join(self.tmp, "refines2.json")
        self._script(["n"], scripts="refine-stub")
        err = io.StringIO()
        with redirect_stderr(err):
            vmf_plan.refine_cmd(plan, out, "scale it")
        self.assertEqual(json.load(open(out)), {"replicas": {}, "env": {}, "command": {}})
        self.assertIn("refinement declined", err.getvalue())

    def test_diff_note(self):
        self.assertEqual(vmf_plan._diff_note(None, {"a": 1}), "")
        self.assertIn("a changed", vmf_plan._diff_note({"a": 1}, {"a": 2}))
        self.assertIn("no change", vmf_plan._diff_note({"a": 1}, {"a": 1}))


if __name__ == "__main__":
    unittest.main()