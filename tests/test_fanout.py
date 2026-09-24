# Contract tests for the per-method plan fan-out (vmf_plan.py fanout).
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import vmf_llm  # noqa: E402
import vmf_plan  # noqa: E402


def _canned(method):
    # Canned planner outputs per method keyword in the prompt.
    if "official container image" in method:
        return json.dumps({"status": "plan", "image": "ghost:5",
                           "ports": [2368], "notes": "official image"})
    if "compose stack" in method:
        return json.dumps({"status": "blocked",
                           "why": "overlay compose is not standalone"})
    if "Dockerfile host-side" in method:
        return json.dumps({"status": "blocked", "why": "no root Dockerfile"})
    if "runtime packages" in method:
        return json.dumps({"status": "plan",
                           "install": ["apt-get install -y nodejs"],
                           "command": ["npm", "start"], "ports": [2368],
                           "checks": [{"probe": {"port": 2368}}],
                           "needs_docker": True, "memory_mb": 2048,
                           "notes": "node stack"})
    return json.dumps({"status": "blocked", "why": "nothing buildable"})


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmf-fanout-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.gen = os.path.join(self.tmp, "generated")
        os.environ["VMF_GENERATED"] = self.gen
        # Non-interactive: the pkg plan must NOT write the gap-fill
        # direct.json (the interactive gate stays shut).
        os.environ.pop("VMF_RUN_YES", None)
        self.old = (vmf_llm.llm_call, vmf_llm.ground)
        self.calls = []
        self.grounds = []

        def fake_call(role, prompt, timeout=90, env=None):
            self.calls.append(prompt)
            return 0, _canned(prompt), ""

        def fake_ground(lookup, doc_cap=3000):
            self.grounds.append(list(lookup))
            return "\ngrounded\n", ["ctx/lib [topic]"]

        vmf_llm.llm_call = fake_call
        vmf_llm.ground = fake_ground

    def tearDown(self):
        (vmf_llm.llm_call, vmf_llm.ground) = self.old
        os.environ.pop("VMF_GENERATED", None)
        os.environ.pop("VMF_RUN_YES", None)

    def _repo(self, files):
        d = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        for name, content in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(content)
        return d


class FanoutPlan(Tmp):
    def test_plans_and_blocks_per_method(self):
        src = self._repo_with_compose_variant()
        out = os.path.join(self.tmp, "fanout.json")
        rc = vmf_plan.fanout_cmd(src, out)
        self.assertEqual(rc, 0)
        doc = json.load(open(out))
        kinds = {a["kind"] for a in doc["approaches"]}
        self.assertEqual(kinds, {"prebuilt_image", "install_script"})
        # blocked compose is on disk as a negative cache entry under
        # the bundle-key dir; build/source were skipped pre-filter and
        # write nothing
        gen = os.path.join(self.tmp, "generated")
        keydir = os.path.join(gen, os.listdir(gen)[0])
        blocks = [f for f in os.listdir(keydir) if f.endswith(".blocked")]
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0].startswith("plan-compose."))
        pre = json.load(open(os.path.join(keydir, "plan-prebuilt.json")))
        self.assertEqual(pre["approach"]["image"],
                         "docker.io/library/ghost:5")

    def test_second_run_costs_zero_llm_calls(self):
        src = self._repo_with_compose_variant()
        out = os.path.join(self.tmp, "fanout.json")
        vmf_plan.fanout_cmd(src, out)
        self.assertGreater(len(self.calls), 0)
        n = len(self.calls)
        rc = vmf_plan.fanout_cmd(src, out)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), n)

    def test_cache_hit_skips_grounding_too(self):
        src = self._repo_with_compose_variant()
        out = os.path.join(self.tmp, "fanout.json")
        vmf_plan.fanout_cmd(src, out)
        self.assertEqual(len(self.grounds), 1)
        vmf_plan.fanout_cmd(src, out)
        self.assertEqual(len(self.grounds), 1)

    def test_prefilters_skip_slots(self):
        # No compose file, no Dockerfile, no Makefile: only prebuilt
        # and pkg slots plan.
        src = self._src({"README.md": "# app\nrun with node index.js\n",
                         "package.json": '{"name": "a", "scripts": {}}'})
        out = os.path.join(self.tmp, "fanout.json")
        rc = vmf_plan.fanout_cmd(src, out)
        self.assertEqual(rc, 0)
        doc = json.load(open(out))
        kinds = [a["kind"] for a in doc["approaches"]]
        self.assertEqual(kinds, ["prebuilt_image", "install_script"])
        gen = os.path.join(self.tmp, "generated")
        for keydir in os.listdir(gen):
            kd = os.path.join(gen, keydir)
            self.assertFalse(os.path.exists(os.path.join(
                kd, "plan-compose.json")),
                "compose slot must not plan without a compose file")

    def test_invented_ports_clamped(self):
        src = self._src({"README.md": "# app\n",
                         "package.json": "{}"})
        # A planner inventing a port out of range loses it.
        old = vmf_llm.llm_call
        vmf_llm.llm_call = lambda role, prompt, timeout=90, env=None: (
            0, json.dumps({"status": "plan", "image": "a:1",
                           "ports": [99999]}), "")
        try:
            out = os.path.join(self.tmp, "fanout.json")
            vmf_plan.fanout_cmd(src, out)
        finally:
            vmf_llm.llm_call = old
        doc = json.load(open(out))
        pre = next(a for a in doc["approaches"] if a["kind"] == "prebuilt_image")
        self.assertNotIn(99999, pre.get("ports") or [])

    def test_interactive_gate_writes_no_direct(self):
        src = self._repo_with_compose_variant()
        out = os.path.join(self.tmp, "fanout.json")
        vmf_plan.fanout_cmd(src, out)
        gen = os.path.join(self.tmp, "generated")
        for keydir in os.listdir(gen):
            self.assertFalse(os.path.exists(os.path.join(
                gen, keydir, "direct.json")))

    def test_race_context_caches_pkg_as_gapfill_direct(self):
        src = self._repo_with_compose_variant()
        os.environ["VMF_RUN_YES"] = "1"
        out = os.path.join(self.tmp, "fanout.json")
        rc = vmf_plan.fanout_cmd(src, out)
        self.assertEqual(rc, 0)
        directs = []
        for d in os.listdir(os.path.join(self.tmp, "generated")):
            p = os.path.join(self.tmp, "generated", d, "direct.json")
            if os.path.isfile(p):
                direct = json.load(open(p))
                self.assertIn("command", direct)
                directs.append(d)
        self.assertEqual(len(directs), 1)

    def test_transient_failure_retries_next_run(self):
        # A parse failure is not the model's verdict: no .blocked file,
        # and the next run replans the method.
        src = self._repo_with_compose_variant()
        old = vmf_llm.llm_call
        vmf_llm.llm_call = lambda role, prompt, timeout=90, env=None: (
            0, "this is not json at all", "")
        try:
            out = os.path.join(self.tmp, "fanout.json")
            rc = vmf_plan.fanout_cmd(src, out)
        finally:
            vmf_llm.llm_call = old
        self.assertEqual(rc, 1)
        gen = os.path.join(self.tmp, "generated")
        keydir = os.path.join(gen, os.listdir(gen)[0])
        self.assertEqual([f for f in os.listdir(keydir)
                          if f.endswith(".blocked")], [])
        vmf_plan.fanout_cmd(src, out)
        self.assertGreater(len(self.calls), 0)

    def _src(self, files):
        return self._repo(files)

    def _repo(self, files):
        return self._mk(files)

    def _mk(self, files):
        return self._mkrepo(files)

    def _mkrepo(self, files):
        d = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        for name, content in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(content)
        return d

    def _repo_with_compose_variant(self):
        return self._mkrepo({
            "README.md": "# ghost\nnpm install; npm start\n",
            "package.json": '{"name": "ghost", "scripts": {"start": "node index.js"}}',
            "compose.dev.sqlite.yaml": "services:\n  mysql:\n    profiles: ['mysql']\n",
        })


def pre_image(plan_doc):
    return plan_doc["approach"]["image"]


if __name__ == "__main__":
    unittest.main()
