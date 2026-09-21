# Contract tests for the verify runner (scripts/vmf_verify.py): check
# derivation from direct plans, the tcp/probe/exec runners against real
# local transports, hostfwd remapping, and the evidence-driven revise
# flow with its gate and cache write-back.
#
# The exec transport is overridden with VMF_VERIFY_SSH templates (the
# test seam); probe/tcp speak to real localhost sockets; the revise
# model reply is a fixture stub under tests/fixtures/verify-stub, and
# HOME is redirected so the revised-plan cache lands in the sandbox.
#
# Run: uv run --with pyyaml --with jsonschema python -m unittest discover tests
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, SCRIPTS)

import vmf_llm  # noqa: E402
import vmf_plan  # noqa: E402
import vmf_verify  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    return s, s.getsockname()[1]


class HttpStub:
    # One server, route table per test: path -> (status, body).
    routes = {}

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                st, body = outer.routes.get(self.path, (404, "not found"))
                self.send_response(st)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_port
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class TcpChecks(unittest.TestCase):
    def test_open_port_passes(self):
        s, port = free_port()
        s.listen(1)
        try:
            ok, ev = vmf_verify.check_tcp(port, {}, None)
        finally:
            s.close()
        self.assertTrue(ok)
        self.assertIsNone(ev)

    def test_dead_port_fails_with_evidence(self):
        s, port = free_port()
        s.close()
        ok, ev = vmf_verify.check_tcp(port, {}, None)
        self.assertFalse(ok)
        self.assertEqual(ev["check"], "tcp:%d" % port)

    def test_hostfwd_remaps_guest_to_host(self):
        s, host_port = free_port()
        s.listen(1)
        try:
            ok, _ = vmf_verify.check_tcp(1337, {1337: host_port}, None)
        finally:
            s.close()
        self.assertTrue(ok)


class ProbeChecks(unittest.TestCase):
    def setUp(self):
        self.stub = HttpStub()
        self.stub.start()

    def tearDown(self):
        self.stub.stop()

    def test_status_and_contains_pass(self):
        self.stub.routes = {"/": (200, "hello vmf")}
        spec = {"probe": {"port": self.stub.port, "path": "/",
                          "expect_status": 200, "expect_contains": "hello"}}
        ok, ev = vmf_verify.check_probe(spec, {}, "x")
        self.assertTrue(ok)
        self.assertIsNone(ev)

    def test_wrong_status_fails(self):
        self.stub.routes = {"/": (200, "hello vmf")}
        spec = {"probe": {"port": self.stub.port, "expect_status": 403}}
        ok, ev = vmf_verify.check_probe(spec, {}, "x")
        self.assertFalse(ok)
        self.assertEqual(ev["expected"], "403")
        self.assertIn("status 200", ev["actual"])

    def test_contains_miss_fails(self):
        self.stub.routes = {"/": (200, "hello vmf")}
        spec = {"probe": {"port": self.stub.port,
                          "expect_contains": "absent"}}
        ok, ev = vmf_verify.check_probe(spec, {}, "x")
        self.assertFalse(ok)
        self.assertIn("hello vmf", ev["actual"])

    def test_connection_refused_is_evidence(self):
        s, port = free_port()
        s.close()
        spec = {"probe": {"port": port}}
        ok, ev = vmf_verify.check_probe(spec, {}, "x")
        self.assertFalse(ok)
        self.assertIn("fetch failed", ev["actual"])


class ExecChecks(unittest.TestCase):
    def test_exit_zero_passes(self):
        os.environ["VMF_VERIFY_SSH"] = "echo ran {cmd}"
        try:
            ok, ev = vmf_verify.check_exec("curl localhost", "vm", None)
        finally:
            del os.environ["VMF_VERIFY_SSH"]
        self.assertTrue(ok)
        self.assertIsNone(ev)

    def test_nonzero_fails_with_output(self):
        os.environ["VMF_VERIFY_SSH"] = "echo boom >&2; exit 3"
        try:
            ok, ev = vmf_verify.check_exec("x", "vm", None)
        finally:
            del os.environ["VMF_VERIFY_SSH"]
        self.assertFalse(ok)
        self.assertEqual(ev["expected"], "exit 0")
        self.assertIn("boom", ev["actual"])


class DeriveChecks(unittest.TestCase):
    def test_tcp_from_ports_plus_model_checks(self):
        plan = {"ports": [1337, 1338],
                "checks": [{"probe": {"port": 1337, "expect_status": 200}},
                           {"exec": {"cmd": "true"}},
                           {"log": {"match": "up"}},
                           {"rfb": {"port": 5900}}]}
        runnable, skipped = vmf_verify.derive_checks(plan, {})
        kinds = [k for k, _, _ in runnable]
        self.assertEqual(kinds, ["tcp", "tcp", "probe", "exec"])
        self.assertEqual(len(skipped), 2)

    def test_junk_ports_and_checks_dropped(self):
        plan = {"ports": ["junk", 99999, 8080],
                "checks": [{"probe": {"port": "nope"}},
                           {"exec": {"cmd": "   "}}]}
        runnable, skipped = vmf_verify.derive_checks(plan, {})
        self.assertEqual([(k, p) for k, p, _ in runnable],
                         [("tcp", 8080)])
        self.assertEqual(skipped, [])


class RunFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_ssh = os.environ.get("VMF_VERIFY_SSH")
        os.environ["VMF_VERIFY_SSH"] = "true"

    def tearDown(self):
        if self.old_ssh is None:
            os.environ.pop("VMF_VERIFY_SSH", None)
        else:
            os.environ["VMF_VERIFY_SSH"] = self.old_ssh
        shutil.rmtree(self.tmp)

    def write_plan(self, plan):
        p = os.path.join(self.tmp, "direct.json")
        json.dump(plan, open(p, "w"))
        return p

    def args(self, plan, deadline=6, evidence_out=None):
        class A:
            pass
        a = A()
        a.plan = self.write_plan(plan)
        a.name = "vm-test"
        a.hostfwd = None
        a.deadline = deadline
        a.evidence_out = evidence_out
        return a

    def test_all_pass(self):
        s, port = free_port()
        s.listen(1)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = vmf_verify.run_cmd(self.args(
                    {"ports": [port],
                     "checks": [{"exec": {"cmd": "echo hi"}}]}))
        finally:
            s.close()
        self.assertEqual(rc, 0)
        self.assertIn("verdict: 2/2 checks pass", out.getvalue())

    def test_fail_writes_evidence(self):
        s, port = free_port()
        s.close()
        ev_path = os.path.join(self.tmp, "ev.json")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = vmf_verify.run_cmd(
                self.args({"ports": [port]}, deadline=3,
                           evidence_out=ev_path))
        self.assertEqual(rc, 1)
        self.assertIn("0/1 checks pass", out.getvalue())
        self.assertIn("1 failed", out.getvalue())
        ev = json.load(open(ev_path))
        self.assertEqual(ev[0]["check"], "tcp:%d" % port)

    def test_ssh_unreachable(self):
        os.environ["VMF_VERIFY_SSH"] = "exit 255"
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = vmf_verify.run_cmd(
                    self.args({"ports": [1337]}, deadline=2))
        finally:
            if self.old_ssh is None:
                os.environ["VMF_VERIFY_SSH"] = "true"
            else:
                os.environ["VMF_VERIFY_SSH"] = self.old_ssh
        self.assertEqual(rc, 2)
        self.assertIn("ssh never came up", out.getvalue())

    def test_no_runnable_checks(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = vmf_verify.run_cmd(self.args({"ports": []}))
        self.assertEqual(rc, 0)
        self.assertIn("nothing to verify", out.getvalue())

    def test_log_and_rfb_skipped(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = vmf_verify.run_cmd(self.args(
                {"ports": [], "checks": [{"log": {"match": "up"}},
                                          {"rfb": {"port": 5900}}]}))
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("check log: SKIP", text)
        self.assertIn("check rfb:5900 SKIP", text)


class ReviseFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_home = os.environ["HOME"]
        self.old_scripts = os.environ.get("VMF_SCRIPTS_DIR")
        self.old_yes = os.environ.pop("VMF_RUN_YES", None)
        os.environ["HOME"] = self.tmp
        os.environ["VMF_SCRIPTS_DIR"] = os.path.join(FIXTURES, "verify-stub")

    def tearDown(self):
        os.environ["HOME"] = self.old_home
        if self.old_scripts is None:
            os.environ.pop("VMF_SCRIPTS_DIR", None)
        else:
            os.environ["VMF_SCRIPTS_DIR"] = self.old_scripts
        if self.old_yes is not None:
            os.environ["VMF_RUN_YES"] = self.old_yes
        shutil.rmtree(self.tmp)

    def write(self, name, data):
        p = os.path.join(self.tmp, name)
        json.dump(data, open(p, "w"))
        return p

    def args(self, plan_path, out, image="ubuntu", phrase="nginx"):
        class A:
            pass
        a = A()
        a.plan = plan_path
        a.out = out
        a.evidence = self.write("ev.json", [
            {"check": "probe:1337", "expected": "200", "actual": "403"}])
        a.image = image
        a.phrase = phrase
        a.cache = None
        return a

    def test_revised_plan_validated_and_cached(self):
        os.environ["VMF_RUN_YES"] = "1"
        plan = self.write("direct.json",
                          {"base_image": "", "install": ["x"],
                           "command": ["nginx"], "ports": [1337],
                           "env": {}, "needs_docker": False,
                           "memory_mb": 1024})
        out = os.path.join(self.tmp, "revised.json")
        err = io.StringIO()
        with redirect_stderr(err):
            rc = vmf_verify.revise_cmd(self.args(plan, out))
        self.assertEqual(rc, 0)
        revised = json.load(open(out))
        self.assertEqual(revised["ports"], [1337, 1338])
        self.assertEqual(revised["command"], ["nginx", "-g", "daemon off;"])
        self.assertEqual(len(revised["checks"]), 1)
        self.assertEqual(revised["checks"][0]["probe"]["port"], 1337)
        self.assertIn("check(s) dropped", err.getvalue())
        gen = vmf_plan.intent_cache_dir("ubuntu", "nginx")
        self.assertEqual(json.load(open(os.path.join(gen, "direct.json"))),
                         revised)
        meta = json.load(open(os.path.join(gen, "direct.json.meta.json")))
        self.assertTrue(meta["verify_revised"])

    def test_declined_gate_writes_nothing(self):
        plan = self.write("direct.json", {"base_image": "",
                                          "install": ["x"],
                                          "command": ["nginx"],
                                          "ports": [1337], "env": {},
                                          "needs_docker": False,
                                          "memory_mb": 1024})
        out = os.path.join(self.tmp, "revised.json")
        real_ask = vmf_llm.tty_ask
        vmf_llm.tty_ask = lambda *a, **k: False
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                rc = vmf_verify.revise_cmd(self.args(plan, out))
        finally:
            vmf_llm.tty_ask = real_ask
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(out))
        self.assertIn("declined", err.getvalue())

    def test_empty_evidence_refuses(self):
        plan = self.write("direct.json", {"base_image": "",
                                          "install": ["x"],
                                          "command": ["nginx"],
                                          "ports": [1337], "env": {},
                                          "needs_docker": False,
                                          "memory_mb": 1024})
        a = self.args(plan, os.path.join(self.tmp, "out.json"))
        a.evidence = self.write("empty.json", [])
        err = io.StringIO()
        with redirect_stderr(err):
            rc = vmf_verify.revise_cmd(a)
        self.assertEqual(rc, 1)
        self.assertIn("no failure evidence", err.getvalue())


if __name__ == "__main__":
    unittest.main()
