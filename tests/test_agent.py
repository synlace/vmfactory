# Contract tests for the agent session (scripts/vmf_agent.py): the ssh
# transport seam, the in-guest acceptance checks against stub transports,
# spec validation, and the full loop with a scripted model under
# tests/fixtures/agent-stub.
#
# Run: uv run --with pyyaml --with jsonschema python -m unittest discover tests
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, SCRIPTS)

import vmf_agent  # noqa: E402


class Args:
    def __init__(self, tmp):
        self.vm = "agent-vm"
        self.image = "python:3.12-slim"
        self.phrase = "static server on 8021"
        self.out = os.path.join(tmp, "direct.json")
        self.turns = 8
        self.budget = 120


def env_setup(tmp, fixture="agent-stub", yes="1"):
    saved = {k: os.environ.get(k) for k in
             ("VMF_SCRIPTS_DIR", "VMF_AGENT_SSH", "VMF_RUN_YES", "HOME",
              "VMF_AGENT_MODEL")}
    os.environ["HOME"] = tmp
    os.environ["VMF_SCRIPTS_DIR"] = os.path.join(FIXTURES, fixture)
    os.environ["VMF_RUN_YES"] = yes
    os.environ["VMF_AGENT_MODEL"] = "stub"
    open(os.path.join(FIXTURES, fixture, "calls"), "w").write("0")
    return saved


def env_restore(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


GUEST_STUB = (
    "if [[ {cmd} == *wget* ]]; then "
    "printf '  HTTP/1.1 200 OK\\n' >&2; echo agent-made; "
    "exit 0; "
    "else exit 0; fi")


class GuestChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = {k: os.environ.get(k) for k in ("VMF_AGENT_SSH",)}
        os.environ["VMF_AGENT_SSH"] = GUEST_STUB

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    def test_tcp_listening(self):
        self.assertTrue(vmf_agent.guest_tcp_listening("vm", 8021))

    def test_probe_status_and_body(self):
        ok, msg = vmf_agent.guest_probe("vm", 8021, "/", 200, "agent-made")
        self.assertTrue(ok)
        self.assertIsNone(msg)

    def test_probe_wrong_status(self):
        ok, msg = vmf_agent.guest_probe("vm", 8021, "/", 404, None)
        self.assertFalse(ok)
        self.assertIn("status 200", msg)

    def test_probe_contains_miss(self):
        ok, msg = vmf_agent.guest_probe("vm", 8021, "/", 200, "absent")
        self.assertFalse(ok)
        self.assertIn("not in body", msg)

    def test_guest_acceptance_full_plan(self):
        plan = {"ports": [8021],
                "checks": [{"probe": {"port": 8021, "expect_status": 200,
                                      "expect_contains": "agent-made"}}]}
        ok, failures = vmf_agent.guest_acceptance("vm", plan)
        self.assertTrue(ok)
        self.assertEqual(failures, [])


class ValidateSpec(unittest.TestCase):
    def test_clamps(self):
        raw = {"command": ["srv"], "ports": ["junk", 99999, 8021],
               "checks": [{"probe": {"port": "x"}}, {"exec": {"cmd": "true"}}],
               "memory_mb": "4G", "needs_docker": True}
        spec = vmf_agent.validate_spec(raw, {})
        self.assertEqual(spec["ports"], [8021])
        self.assertEqual(len(spec["checks"]), 1)
        self.assertEqual(spec["memory_mb"], 2048)

    def test_no_command_refused(self):
        self.assertIsNone(vmf_agent.validate_spec({"command": []}, {}))


class AgentLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = env_setup(self.tmp)

    def tearDown(self):
        env_restore(self.saved)
        shutil.rmtree(self.tmp)

    def test_full_loop_writes_spec(self):
        os.environ["VMF_AGENT_SSH"] = GUEST_STUB
        a = Args(self.tmp)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = vmf_agent.agent_cmd(a)
        self.assertEqual(rc, 0, err.getvalue())
        spec = json.load(open(a.out))
        self.assertEqual(spec["command"],
                         ["/vmf/busybox", "httpd", "-f", "-p", "8021",
                          "-h", "/srv"])
        self.assertEqual(spec["ports"], [8021])
        self.assertIn("agent: spec written", err.getvalue())
        # the payoff: the demonstrated spec lands in the intent cache
        gen = vmf_agent.vmf_plan.intent_cache_dir(a.image, a.phrase)
        cached = json.load(open(os.path.join(gen, "direct.json")))
        self.assertEqual(cached["command"], spec["command"])
        meta = json.load(open(os.path.join(gen, "direct.json.meta.json")))
        self.assertTrue(meta["agent"])

    def test_no_model_configured(self):
        saved = {k: os.environ.pop(k, None)
                 for k in ("VMF_AGENT_MODEL", "VMF_GAPFILL_MODEL",
                           "VMF_LLM_MODEL")}
        try:
            a = Args(self.tmp)
            err = io.StringIO()
            with redirect_stderr(err):
                rc = vmf_agent.agent_cmd(a)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        self.assertEqual(rc, 3)
        self.assertIn("no agent model", err.getvalue())

    def test_vm_lost_aborts(self):
        os.environ["VMF_AGENT_SSH"] = "exit 255"
        a = Args(self.tmp)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = vmf_agent.agent_cmd(a)
        self.assertEqual(rc, 1)
        self.assertIn("VM lost", err.getvalue())


if __name__ == "__main__":
    unittest.main()
