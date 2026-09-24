# Contract tests for the route scout (vmf_plan.py scout) and the
# race's streaming feed (vmf_race.ScoutFeed).
#
# Run: uv run --with pyyaml --with jsonschema --with rich \
#        python -m unittest discover tests
import io
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
import vmf_race  # noqa: E402
import vmf_status  # noqa: E402
import vmf_ui  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmf-scout-")
        self.addCleanup(shutil.rmtree, self.tmp)
        os.environ["VMF_GENERATED"] = os.path.join(self.tmp, "generated")
        os.environ.pop("VMF_RUN_YES", None)
        self.calls = []
        self.old_call = vmf_llm.llm_call

    def tearDown(self):
        vmf_llm.llm_call = self.old_call
        os.environ.pop("VMF_GENERATED", None)
        os.environ.pop("VMF_RUN_YES", None)

    def _repo(self, files):
        d = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        for name, content in files.items():
            with open(os.path.join(d, name), "w") as f:
                f.write(content)
        return d

    def _canned_turns(self, turns):
        # turns: list of llm responses, consumed in order.
        seq = list(turns)

        def fake(role, prompt, timeout=90, env=None):
            self.calls.append(prompt)
            return 0, seq.pop(0) if seq else '{"routes": [], "more": false}', ""
        vmf_llm.llm_call = fake


class ScoutPlan(Tmp):
    def _turn1(self):
        return ('{"routes": ['
                '{"method": "prebuilt", "why": "official image", '
                '"cost": "fast", "cite": "docs: install", '
                '"image": "ghcr.io/gchq/cyberchef:10", "ports": [8080]},'
                '{"method": "release", "why": "static zip", '
                '"cost": "fast", "cite": "releases v10.19.2", '
                '"install": ["curl -L -o /tmp/a.zip <url>", '
                '"unzip /tmp/a.zip"], "command": ["node", "web/"], '
                '"ports": [8080]}], "more": true}')

    def _turn2(self):
        return ('{"routes": ['
                '{"method": "pkg", "why": "node stack", "cost": "slow", '
                '"install": ["apt-get install -y nodejs"], '
                '"command": ["npm", "start"], "ports": [8080]}], '
                '"more": false}')

    def test_routes_stream_in_emission_order(self):
        src = self._repo({"README.md": "# app\nnpm start\n",
                          "package.json": "{}"})
        out = os.path.join(self.tmp, "scout.jsonl")
        seq = [self._turn1(), self._turn2()]

        def fake(role, prompt, timeout=90, env=None):
            self.calls.append(prompt)
            return 0, seq.pop(0), ""
        vmf_llm.llm_call = fake
        rc = vmf_plan.scout_cmd(src, out)
        self.assertEqual(rc, 0)
        lines = [json.loads(l) for l in open(out) if l.strip()]
        self.assertEqual([x["method"] for x in lines[:2]],
                         ["prebuilt", "release"])
        self.assertEqual(lines[-1]["summary"]["llm"], 2)
        self.assertEqual(lines[-1]["summary"]["skipped"],
                         ["no compose file", "no root Dockerfile",
                          "no build manifest"])
        # release clamps to the install_script kind with a direct plan
        self.assertEqual(lines[1]["kind"], "install_script")
        self.assertIn("direct", lines[1])
        self.assertEqual(lines[1]["direct"]["command"],
                         ["node", "web/"])

    def test_second_run_replays_cache_zero_llm(self):
        src = self._repo({"README.md": "# app\nnpm start\n",
                          "package.json": "{}"})
        out = os.path.join(self.tmp, "scout.jsonl")

        def fake(role, prompt, timeout=90, env=None):
            self.calls.append(prompt)
            return 0, self._turn1(), ""
        vmf_llm.llm_call = fake
        vmf_plan.scout_cmd(src, out)
        n = len(self.calls)
        out2 = os.path.join(self.tmp, "scout2.jsonl")
        rc = vmf_plan.scout_cmd(src, out2)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), n)
        lines = [json.loads(l) for l in open(out2) if l.strip()]
        self.assertEqual(lines[0]["method"], "prebuilt")
        self.assertIn("summary", lines[-1])

    def test_unknown_method_and_portless_prebuilt_dropped(self):
        src = self._repo({"README.md": "# app\n"})
        out = os.path.join(self.tmp, "scout.jsonl")
        bad = ('{"routes": ['
               '{"method": "teleport", "why": "junk", "image": "x:1"},'
               '{"method": "prebuilt", "why": "no ports", '
               '"image": "a:1", "ports": []},'
               '{"method": "release", "why": "no command", '
               '"install": ["curl x"]}], "more": false}')
        vmf_llm.llm_call = lambda *a, **k: (0, bad, "")
        rc = vmf_plan.scout_cmd(src, out)
        self.assertEqual(rc, 1)
        routes = [json.loads(l) for l in open(out)
                  if l.strip() and "summary" not in l]
        self.assertEqual(routes, [])

    def test_transient_failure_not_cached(self):
        src = self._repo({"README.md": "# app\n"})
        out = os.path.join(self.tmp, "scout.jsonl")
        vmf_llm.llm_call = lambda *a, **k: (99, "", "timeout")
        rc = vmf_plan.scout_cmd(src, out)
        self.assertEqual(rc, 1)
        gen = os.path.join(self.tmp, "generated")
        keydir = os.path.join(gen, os.listdir(gen)[0])
        self.assertFalse(os.path.exists(os.path.join(keydir, "scout.json")))
        # ...and the summary still lands on the stream (honest close)
        self.assertIn("summary", open(out).read())

    def test_direct_cache_written_when_accepted(self):
        src = self._repo({"README.md": "# app\nnpm start\n",
                          "package.json": "{}"})
        os.environ["VMF_RUN_YES"] = "1"
        out = os.path.join(self.tmp, "scout.jsonl")
        vmf_llm.llm_call = lambda *a, **k: (0, self._turn1(), "")
        vmf_plan.scout_cmd(src, out)
        gen = os.path.join(self.tmp, "generated")
        hits = []
        for d in os.listdir(gen):
            p = os.path.join(gen, d, "direct-release.json")
            if os.path.isfile(p):
                j = json.load(open(p))
                self.assertIn("command", j)
                hits.append(p)
        self.assertEqual(len(hits), 1)

    def test_release_manifest_degrades_without_github(self):
        src = self._repo({"README.md": "# app\n"})
        self.assertEqual(vmf_plan._release_manifest(src), "")

    def test_install_hints_bounded(self):
        src = self._repo({"README.md":
                          "# app\n### Install\n" +
                          "curl -L foo | sh\n" * 40})
        hints = vmf_plan._install_hints(src)
        self.assertLessEqual(len(hints), 24)
        self.assertTrue(any("curl" in h for h in hints))


class ScoutFeed(Tmp):
    def setUp(self):
        super().setUp()
        self.runs = tempfile.mkdtemp(prefix="vmf-runs-", dir=self.tmp)
        self.old_runs = vmf_race.RUNS
        vmf_race.RUNS = self.runs
        self.addCleanup(setattr, vmf_race, "RUNS", self.old_runs)
        vmf_status.RUNS = self.tmp
        self.addCleanup(setattr, vmf_status, "RUNS", self.tmp)
        self.old_popen = vmf_race.subprocess.Popen
        self.addCleanup(setattr, vmf_race.subprocess, "Popen",
                        self.old_popen)

    def _feed(self, lines):
        # A fake scout subprocess: the jsonl stream is already written
        # when the feed drains; the process exits after first poll.
        def popen(cmd, stdout=None, stderr=None, **_kw):
            self.spawned = cmd
            out = cmd[-1]
            with open(out, "w") as f:
                f.write("\n".join(lines) + "\n")

            class P:
                def poll(self):
                    return 0
                def terminate(self):
                    pass
                def wait(self, timeout=None):
                    return 0
            return P()
        vmf_race.subprocess.Popen = popen

    def test_drain_reads_routes_and_summary(self):
        r1 = {"method": "prebuilt", "kind": "prebuilt_image",
              "cost": "fast", "ports": [8080], "image": "a:1",
              "cite": "docs"}
        self._feed([json.dumps(r1), json.dumps({"summary": {"llm": 1}})])
        feed = vmf_race.ScoutFeed(self.tmp)
        self.assertEqual(self.spawned[-3], "scout")
        self.assertEqual(self.spawned[-1], feed.out)
        got = feed.drain()
        self.assertEqual(len(got), 1)
        self.assertEqual(feed.routes, 1)
        self.assertEqual(feed.summary, {"llm": 1})
        self.assertFalse(feed.alive())
        self.assertEqual(feed.drain(), [])

    def test_wait_first_returns_routes_or_none(self):
        self._feed([json.dumps({"method": "release", "kind":
                                "install_script", "ports": [80]})])
        feed = vmf_race.ScoutFeed(self.tmp)
        got = feed.wait_first(5)
        self.assertEqual(len(got), 1)
        empty = tempfile.mkdtemp(dir=self.tmp)
        self._feed([])
        feed2 = vmf_race.ScoutFeed(empty)
        self.assertIsNone(feed2.wait_first(5))

    def test_keep_entry_shape(self):
        r = {"method": "release", "kind": "install_script", "cost": "fast",
             "ports": [8080], "direct": {"command": ["./cyberchef"]},
             "cite": "v10"}
        k = vmf_race._keep_entry(2, r, self.runs, "web")
        self.assertEqual(k["cand"], "web-c2")
        self.assertEqual(k["method"], "release")
        self.assertEqual(k["detail"], "./cyberchef")
        self.assertIn("c2.log", k["lp"])

    def test_route_allowed_matches_kind_and_number(self):
        self.assertTrue(vmf_race._route_allowed(
            {"kind": "install_script", "method": "release"}, 2))
        os.environ["VMF_RACE_APPROACH"] = "prebuilt_image"
        try:
            self.assertFalse(vmf_race._route_allowed(
                {"kind": "install_script", "method": "release"}, 2))
        finally:
            os.environ.pop("VMF_RACE_APPROACH")


class ScoutWiring(Tmp):
    # main() tries the scout first; the fan-out stays the fallback.
    def setUp(self):
        super().setUp()
        self.runs = tempfile.mkdtemp(prefix="vmf-runs-", dir=self.tmp)
        self.src = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        vmf_race.RUNS = self.runs
        vmf_status.RUNS = self.tmp
        self.addCleanup(setattr, vmf_status, "RUNS", self.tmp)
        self.env_backup = {}
        for k in ("VMF_RACE_MODE", "VMF_RACE_SKIP_CACHE",
                  "VMF_RACE_APPROACH", "VMF_RACE_SKIP", "VMF_NAME",
                  "VMF_RACE_SCOUT", "VMF_LOUD", "VMF_UI"):
            self.env_backup[k] = os.environ.pop(k, None)
        self.addCleanup(self._restore_env)
        self.old = (vmf_race.ScoutFeed, vmf_race.load_approaches,
                    vmf_race.race, vmf_race.bundle_key,
                    vmf_race.load_winner)
        self.addCleanup(self._restore)
        self.raced = []
        self.enum_calls = []

    def _restore_env(self):
        for k, v in self.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _restore(self):
        (vmf_race.ScoutFeed, vmf_race.load_approaches,
         vmf_race.race, vmf_race.bundle_key,
         vmf_race.load_winner) = self.old

    def test_scout_first_feeds_race(self):
        routes = [{"method": "prebuilt", "kind": "prebuilt_image",
                   "cost": "fast", "ports": [8080], "image": "a:1",
                   "cite": "docs"}]

        class FakeFeed:
            def __init__(self, src, log=None):
                FakeFeed.spawned = True
            def wait_first(self, t):
                return routes
            def alive(self):
                return False
            def drain(self):
                return []
            def kill(self):
                pass
        vmf_race.ScoutFeed = FakeFeed
        vmf_race.bundle_key = lambda src: "abc123def456"
        vmf_race.load_winner = lambda src: None
        vmf_race.load_approaches = lambda src: (
            self.enum_calls.append(src) or [])
        vmf_race.race = lambda keep, src, base, feed=None, board=None: (
            self.raced.append((keep, feed)) or 0)
        rc = vmf_race.main(["race", self.src])
        self.assertEqual(rc, 0)
        self.assertEqual(self.enum_calls, [])
        self.assertEqual(len(self.raced), 1)
        self.assertEqual(self.raced[0][0][0]["method"], "prebuilt")
        self.assertIsInstance(self.raced[0][1], FakeFeed)

    def test_scout_zero_routes_falls_back(self):
        class FakeFeed:
            out = ""
            def __init__(self, src, log=None):
                pass
            def wait_first(self, t):
                return None
            def alive(self):
                return False
            def drain(self):
                return []
            def kill(self):
                pass
        vmf_race.ScoutFeed = FakeFeed
        vmf_race.bundle_key = lambda src: "abc123def456"
        vmf_race.load_winner = lambda src: None
        vmf_race.load_approaches = lambda src: (
            self.enum_calls.append(src) or [])
        vmf_race.race = lambda keep, src, base, feed=None, board=None: (
            self.raced.append(keep) or 0)
        rc = vmf_race.main(["race", self.src])
        # The fallback table is empty here, so the run fails honestly,
        # but the scout fell through to the fan-out.
        self.assertEqual(rc, 1)
        self.assertEqual(self.enum_calls, [self.src])
        self.assertEqual(self.raced, [])

    def test_scout_disabled_skips_feed(self):
        os.environ["VMF_RACE_SCOUT"] = "0"

        class Boom:
            def __init__(self, *a, **k):
                raise AssertionError("scout must not spawn")
        vmf_race.ScoutFeed = Boom
        vmf_race.bundle_key = lambda src: "abc123def456"
        vmf_race.load_winner = lambda src: None
        vmf_race.load_approaches = lambda src: [
            {"kind": "install_script", "evidence": "x"}]
        vmf_race.race = lambda keep, src, base, feed=None, board=None: 0
        rc = vmf_race.main(["race", self.src])
        self.assertEqual(rc, 0)


class RunnerMethod(Tmp):
    def test_release_sets_direct_plan(self):
        cmd, env = vmf_race.runner_cmd("install_script", "web-c1",
                                       self.tmp, None, [8080],
                                       method="release")
        self.assertEqual(env.get("VMF_PLAN_DIRECT"), "release")

    def test_pkg_keeps_shared_plan(self):
        cmd, env = vmf_race.runner_cmd("install_script", "web-c1",
                                       self.tmp, None, [8080],
                                       method="pkg")
        self.assertNotIn("VMF_PLAN_DIRECT", env)

    def test_junk_method_ignored(self):
        cmd, env = vmf_race.runner_cmd("install_script", "web-c1",
                                       self.tmp, None, [8080],
                                       method="../evil")
        self.assertNotIn("VMF_PLAN_DIRECT", env)


class Board(unittest.TestCase):
    def test_lane_transitions_and_chips(self):
        if not vmf_ui._RICH:
            self.skipTest("rich not installed")
        b = vmf_ui.Board("web", file=io.StringIO())
        self.assertTrue(b.ok)
        b.stage("scout · reading repo + releases")
        b.lane("prebuilt", "plan", "docker run -p 8080:8080",
               "ghcr.io/gchq/cyberchef", "fast/T0")
        b.lane("release", "plan", "unzip release", "v10.19.2", "fast/T0")
        b.chips(skipped=2, llm=2)
        b.note("skipped · no compose file · no build manifest")
        b.lane("prebuilt", "booting", "docker run -p 8080:8080",
               "ghcr.io/gchq/cyberchef", "fast/T0")
        b.lane("prebuilt", "pass", "tcp://192.168.42.191:8080")
        b.lane("release", "parked", "fail (probe timeout)")
        self.assertEqual(b.lanes["prebuilt"]["state"], "pass")
        self.assertEqual(b.lanes["release"]["state"], "parked")
        self.assertEqual(b.skipped, 2)
        b.close()
        self.assertFalse(b.ok)

    def test_missing_rich_degrades(self):
        old = vmf_ui._RICH
        vmf_ui._RICH = False
        try:
            b = vmf_ui.Board("web")
            self.assertTrue(b.dead)
            self.assertFalse(b.ok)
            b.lane("x", "plan")
            b.close()
        finally:
            vmf_ui._RICH = old


class QuietStatus(unittest.TestCase):
    def test_quiet_suppresses_nonfinal_render(self):
        buf = io.StringIO()
        old_env = os.environ.pop("VMF_STATUS", None)
        os.environ["VMF_STATUS"] = "tty"
        old = sys.stderr
        sys.stderr = buf
        try:
            vmf_status.set_quiet(True)
            vmf_status._render("web plan x t+0:01", final=False)
            self.assertEqual(buf.getvalue(), "")
            vmf_status._render("web pass t+1:00", final=True)
            self.assertIn("pass", buf.getvalue())
            vmf_status.set_quiet(False)
            vmf_status._render("web plan y t+0:02", final=False)
            self.assertIn("plan y", buf.getvalue())
        finally:
            sys.stderr = old
            vmf_status.set_quiet(False)
            if old_env is None:
                os.environ.pop("VMF_STATUS", None)
            else:
                os.environ["VMF_STATUS"] = old_env


if __name__ == "__main__":
    unittest.main()
