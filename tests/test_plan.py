# Contract tests for the vmf plan pipeline (scripts/vmf_plan.py).
#
# These run without qemu and without a model: translate/flatten/refine
# are pure functions over fixtures, and the LLM transport is a stub
# scripts/llm.sh (tests/fixtures/llm-stub.sh).
#
# Run: python3 -m unittest discover tests
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, SCRIPTS)

import vmf_plan  # noqa: E402

import jsonschema  # noqa: E402
import yaml  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmf-test-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def path(self, name):
        return os.path.join(self.tmp, name)


class NormImage(unittest.TestCase):
    def test_table(self):
        cases = {
            "nginx": "docker.io/library/nginx",
            "nginx:1.27": "docker.io/library/nginx:1.27",
            "user/repo": "docker.io/user/repo",
            "user/repo:tag": "docker.io/user/repo:tag",
            "docker.io/library/nginx": "docker.io/library/nginx",
            "ghcr.io/owner/app": "ghcr.io/owner/app",
            "localhost/vmf-compose/x-0:0": "localhost/vmf-compose/x-0:0",
            "registry:5000/a/b": "registry:5000/a/b",
            "nginx@sha256:aa": "docker.io/library/nginx@sha256:aa",
            "": "",
        }
        for img, want in cases.items():
            self.assertEqual(vmf_plan.norm_image(img), want, img)


class Resolve(Tmp):
    @staticmethod
    def _hit(rel):
        return {"dir": os.path.join("/x", rel), "rel": rel, "file": "compose.yaml",
                "name": "", "services": [], "ports": []}

    def test_single_hit(self):
        hits = [self._hit(".")]
        self.assertEqual(vmf_plan.resolve("/x", hits, ""), hits[0])

    def test_multi_hit_no_hint_exits_2(self):
        hits = [self._hit("a"), self._hit("b")]
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            vmf_plan.resolve("/x", hits, "")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("several projects", err.getvalue())

    def test_hint_no_match_exits_1(self):
        hits = [self._hit("a")]
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            vmf_plan.resolve("/x", hits, "zzz")
        self.assertEqual(cm.exception.code, 1)


class Replicas(unittest.TestCase):
    def test_bounds_and_names(self):
        names = {"web", "db"}
        payload = {"replicas": {"web": 5, "db": 1, "ghost": 4, "web2": 13, "x": "3"}}
        self.assertEqual(vmf_plan.extract_refines(payload, [{"name": "web"}, {"name": "db"}]),
                         {"replicas": {"web": 5}, "env": {}, "command": {}})

    def test_empty(self):
        self.assertEqual(vmf_plan.extract_refines({}, [{"name": "web"}]),
                         {"replicas": {}, "env": {}, "command": {}})

    def test_env_and_command_coercion(self):
        payload = {"env": {"web": {"A": 1, "B": None}, "ghost": {"X": "1"}},
                   "command": {"web": ["run", 2], "db": [], "ghost": ["x"]}}
        out = vmf_plan.extract_refines(payload, [{"name": "web"}, {"name": "db"}])
        self.assertEqual(out["env"], {"web": {"A": "1", "B": ""}})
        self.assertEqual(out["command"], {"web": ["run", "2"]})
        self.assertEqual(out["replicas"], {})

    def test_replicas_legacy_wrapper(self):
        self.assertEqual(vmf_plan.extract_replicas({"replicas": {"web": 5}}, {"web"}),
                         {"web": 5})


class ApplyVariant(Tmp):
    def test_case_insensitive_key(self):
        plan = {"services": [
            {"name": "a", "build": {"args": {"Variant": "old"}}},
            {"name": "b", "build": {"args": {"other": "x"}}},
        ]}
        self.assertEqual(vmf_plan.apply_variant(plan, "v2"), 1)
        self.assertEqual(plan["services"][0]["build"]["args"]["Variant"], "v2")


class Translate(Tmp):
    def _write(self, name, text):
        p = self.path(name)
        open(p, "w").write(text)
        return p

    def test_fixture_to_schema(self):
        compose = self._write("compose.yaml", """
services:
  web:
    image: nginx
    ports: ["8080:80", "53:53/udp", "9000"]
    environment:
      FOO: bar
      EMPTY:
    env_file: [extra.env]
    networks:
      default:
        aliases: [app]
    depends_on: [db]
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost/ || exit 1"]
  db:
    image: mariadb:11.8
    expose: ["3306"]
  built:
    build: ./svc
    command: ["sleep", "1"]
""")
        self._write("extra.env", "K1=V1\n# comment\nBAD\nK2=V2\n")
        os.mkdir(self.path("svc"))
        plan = vmf_plan.translate(compose, self.tmp, self.tmp)
        self.assertEqual(plan["primary"], "web")
        self.assertEqual(plan["project_dir"], ".")
        web = plan["services"][0]
        self.assertEqual(web["image"], "docker.io/library/nginx")
        self.assertEqual(web["ports"], [
            {"host": 8080, "cport": 80, "proto": "tcp"},
            {"host": 53, "cport": 53, "proto": "udp"}])
        self.assertEqual(web["env"], {"FOO": "bar", "EMPTY": "", "K1": "V1", "K2": "V2"})
        self.assertEqual(web["aliases"], ["app"])
        self.assertEqual(web["depends_on"], ["db"])
        db = plan["services"][1]
        self.assertEqual(db["image"], "docker.io/library/mariadb:11.8")
        self.assertEqual(db["expose"], ["3306/tcp"])
        self.assertEqual(plan["services"][2]["build"],
                         {"context": "./svc", "dockerfile": "Dockerfile"})
        self.assertEqual(plan["services"][2]["build"],
                         {"context": "./svc", "dockerfile": "Dockerfile"})
        # checks lift: published tcp ports get connect checks, the
        # healthcheck rides through verbatim as an exec check
        self.assertEqual(plan["checks"], [
            {"tcp": {"port": 8080}},
            {"exec": {"cmd": "curl -f http://localhost/ || exit 1"}}])
        schema = json.load(open(os.path.join(SCRIPTS, "..", "schemas", "plan.schema.json")))
        jsonschema.validate(plan, schema)

    def test_no_services(self):
        compose = self._write("compose.yaml", "services: {}\n")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            vmf_plan.translate(compose, self.tmp, self.tmp)
        self.assertEqual(cm.exception.code, 1)

    def test_every_dep_exits_1(self):
        compose = self._write("compose.yaml", """
services:
  a: {image: nginx, depends_on: [b]}
  b: {image: nginx, depends_on: [a]}
""")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            vmf_plan.translate(compose, self.tmp, self.tmp)
        self.assertEqual(cm.exception.code, 1)


class Flatten(Tmp):
    def _manifest(self):
        return {"services": [
            {"name": "web", "image": "docker.io/library/nginx:1.27",
             "ports": [{"host": 8080, "cport": 80, "proto": "tcp"}],
             "env": {"FOO": "bar"}, "depends_on": ["db"],
             "aliases": ["app"]},
            {"name": "db", "image": "docker.io/library/mariadb:11.8",
             "ports": [], "env": {}, "depends_on": []}],
            "tags": {"web": "localhost/vmf-compose/x-web:0"}}

    def test_single_instance(self):
        m = self.path("manifest.json")
        json.dump(self._manifest(), open(m, "w"))
        vmf_plan.flatten_cmd(m, self.path("compose.yaml"), self.path("ports.txt"))
        doc = yaml.safe_load(open(self.path("compose.yaml")))
        self.assertEqual(sorted(doc["services"]), ["db", "web"])
        self.assertEqual(doc["services"]["web"]["image"], "localhost/vmf-compose/x-web:0")
        self.assertEqual(doc["services"]["web"]["networks"]["default"]["ipv4_address"],
                         "172.31.100.10")
        # db sees the base instance and its alias (sorted by hostname)
        self.assertEqual(doc["services"]["db"]["extra_hosts"],
                         ["app=172.31.100.10", "web=172.31.100.10"])
        self.assertEqual(open(self.path("ports.txt")).read(), "tcp 8080 80 web\n")

    def test_replicas(self):
        m = self.path("manifest.json")
        r = self.path("refines.json")
        json.dump(self._manifest(), open(m, "w"))
        json.dump({"replicas": {"web": 3}}, open(r, "w"))
        vmf_plan.flatten_cmd(m, self.path("compose.yaml"), self.path("ports.txt"), r)
        doc = yaml.safe_load(open(self.path("compose.yaml")))
        self.assertEqual(sorted(doc["services"]), ["db", "web", "web-2", "web-3"])
        self.assertEqual(doc["services"]["web-2"]["networks"]["default"]["ipv4_address"],
                         "172.31.100.11")
        self.assertEqual(doc["services"]["web-3"]["networks"]["default"]["ipv4_address"],
                         "172.31.100.12")
        # every instance carries every other instance's IP
        self.assertIn("web-2=172.31.100.11", doc["services"]["web"]["extra_hosts"])
        self.assertIn("web=172.31.100.10", doc["services"]["web-2"]["extra_hosts"])
        self.assertIn("web-3=172.31.100.12", doc["services"]["web"]["extra_hosts"])
        # aliases point at the base instance; peers of web see the alias too
        self.assertIn("app=172.31.100.10", doc["services"]["db"]["extra_hosts"])
        self.assertIn("app=172.31.100.10", doc["services"]["web-2"]["extra_hosts"])
        self.assertEqual(open(self.path("ports.txt")).read(),
                         "tcp 8080 80 web\n"
                         "tcp 8081 80 web-2\n"
                         "tcp 8082 80 web-3\n")
        schema = json.load(open(os.path.join(SCRIPTS, "..", "schemas", "refines.schema.json")))
        jsonschema.validate({"replicas": {"web": 3}, "env": {}, "command": {}}, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"replicas": {"web": 13}, "env": {}, "command": {}}, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"replicas": {"web": 1}, "env": {}, "command": {}}, schema)

    def test_udp_ports(self):
        m = self.path("manifest.json")
        mdata = self._manifest()
        mdata["services"][0]["ports"].append({"host": 53, "cport": 53, "proto": "udp"})
        json.dump(mdata, open(m, "w"))
        vmf_plan.flatten_cmd(m, self.path("compose.yaml"), self.path("ports.txt"))
        text = open(self.path("ports.txt")).read()
        self.assertIn("tcp 8080 80 web\n", text)
        self.assertIn("udp 53 53 web\n", text)

    def test_env_and_command_overlay(self):
        m = self.path("manifest.json")
        r = self.path("refines.json")
        mdata = self._manifest()
        mdata["services"][0]["env"] = {"A": "1"}
        mdata["services"][0]["command"] = ["old", "cmd"]
        json.dump(mdata, open(m, "w"))
        json.dump({"replicas": {"web": 2},
                   "env": {"web": {"B": "2"}, "ghost": {"X": "1"}},
                   "command": {"web": ["new", "cmd"], "ghost": ["x"]}},
                  open(r, "w"))
        vmf_plan.flatten_cmd(m, self.path("compose.yaml"), self.path("ports.txt"), r)
        doc = yaml.safe_load(open(self.path("compose.yaml")))
        for iname in ("web", "web-2"):
            self.assertEqual(doc["services"][iname]["command"], ["new", "cmd"])
            self.assertEqual(doc["services"][iname]["environment"], {"A": "1", "B": "2"})
        self.assertEqual(doc["services"]["db"].get("environment", {}), {})
        self.assertEqual(open(self.path("ports.txt")).read(),
                         "tcp 8080 80 web\ntcp 8081 80 web-2\n")


class PortsCmd(Tmp):
    def _plan(self, ports):
        p = self.path("plan.json")
        json.dump({"services": [{"name": "web", "ports": ports}]}, open(p, "w"))
        return p

    def test_tsv(self):
        r = self.path("refines.json")
        json.dump({"replicas": {"web": 5}}, open(r, "w"))
        out = io.StringIO()
        with redirect_stdout(out):
            vmf_plan.ports_cmd(self._plan([{"host": 8080, "cport": 80, "proto": "tcp"}]), r)
        self.assertEqual(out.getvalue(), "8080\ttcp\t5\n")

    def test_udp_and_no_refines(self):
        out = io.StringIO()
        with redirect_stdout(out):
            vmf_plan.ports_cmd(self._plan([{"host": 53, "cport": 53, "proto": "udp"}]), None)
        self.assertEqual(out.getvalue(), "")


class RefineViaStub(Tmp):
    """refine end-to-end with a stub llm.sh — no model, no network."""

    def _run(self, stub, payload_plan):
        plan = self.path("plan.json")
        json.dump(payload_plan, open(plan, "w"))
        out = self.path("refines.json")
        env = dict(os.environ, VMF_SCRIPTS_DIR=os.path.join(FIXTURES, stub))
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"),
                               "refine", plan, out],
                              capture_output=True, text=True, env=env)
        return proc, out

    def test_stub_scales_and_ignores_outsiders(self):
        proc, out = self._run("llm-stub",
                              {"services": [{"name": "web", "ports": []},
                                            {"name": "db", "ports": []}]})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.load(open(out)),
                         {"replicas": {"web": 5}, "env": {}, "command": {}})
        self.assertIn("intent: scaling web to 5 instances", proc.stderr)
        self.assertNotIn("db", json.load(open(out))["replicas"])

    def test_backtick_wrapped_payload(self):
        proc, out = self._run("llm-backtick",
                              {"services": [{"name": "web", "ports": []}]})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.load(open(out)),
                         {"replicas": {"web": 5}, "env": {}, "command": {}})

    def test_no_match_line(self):
        proc, out = self._run("llm-empty",
                              {"services": [{"name": "web", "ports": []}]})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.load(open(out)),
                         {"replicas": {}, "env": {}, "command": {}})
        self.assertIn("intent: no refinement matched", proc.stderr)


class Cli(unittest.TestCase):
    def test_unknown_subcommand_exit_2(self):
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"), "nope"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage:", proc.stderr)

    def test_no_args_exit_2(self):
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "vmf_plan.py")],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_plan_missing_source_exit_1(self):
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"),
                               "plan", "/nonexistent-vmf-src",
                               os.path.join(tempfile.mkdtemp(), "plan.json")],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no compose file", proc.stderr)


class ClampImages(unittest.TestCase):
    def test_keeps_clean_refs(self):
        self.assertEqual(vmf_plan._clamp_images(["ghost:5", " nginx "]),
                         ["ghost:5", "nginx"])

    def test_drops_junk_and_caps(self):
        self.assertEqual(vmf_plan._clamp_images(
            ["", "a b", "x\ny", "ok", "ok", 42]), ["ok"])
        self.assertEqual(len(vmf_plan._clamp_images(
            ["a%d" % i for i in range(10)])), 4)


class ClampMemory(unittest.TestCase):
    def test_default_and_bounds(self):
        self.assertEqual(vmf_plan._clamp_memory(None, False), 1024)
        self.assertEqual(vmf_plan._clamp_memory(2048, False), 2048)
        self.assertEqual(vmf_plan._clamp_memory(99999, False), 8192)

    def test_needs_docker_floor(self):
        # dockerd + containerd + the app share the VM; the plan floor
        # is deterministic, not a model opinion.
        self.assertEqual(vmf_plan._clamp_memory(1024, True), 2048)
        self.assertEqual(vmf_plan._clamp_memory(4096, True), 4096)


class ClampComposeFile(unittest.TestCase):
    """The enumeration's compose_file: basename, yaml, must exist."""

    def setUp(self):
        import tempfile
        self.src = tempfile.mkdtemp(prefix="vmf-cf-")
        open(os.path.join(self.src, "compose.dev.yaml"), "w").write("services: {}")

    def test_valid_root_file(self):
        self.assertEqual(vmf_plan._clamp_compose_file("compose.dev.yaml", self.src),
                         "compose.dev.yaml")

    def test_rejects_missing_file(self):
        self.assertEqual(vmf_plan._clamp_compose_file("nope.yaml", self.src), "")

    def test_rejects_paths(self):
        self.assertEqual(
            vmf_plan._clamp_compose_file("docker/dev/compose.yaml", self.src), "")

    def test_rejects_non_yaml(self):
        self.assertEqual(vmf_plan._clamp_compose_file("package.json", self.src), "")

    def test_junk_degrades_to_default(self):
        self.assertEqual(vmf_plan._clamp_memory("4G", False), 1024)
        self.assertEqual(vmf_plan._clamp_memory("junk", True), 2048)


class FatBase(unittest.TestCase):
    """fat_base_ref reads the env override, then the ready marker;
    base_image_note switches the gap-fill vocabulary accordingly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmf-fat-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self._old = {k: os.environ.get(k)
                     for k in ("HOME", "VMF_BASE_IMAGE")}
        os.environ["HOME"] = self.tmp
        os.environ.pop("VMF_BASE_IMAGE", None)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _marker(self, text):
        d = os.path.join(self.tmp, ".local", "share", "vmf")
        os.makedirs(d)
        open(os.path.join(d, "fat-base.ready"), "w").write(text)

    def test_env_override_wins(self):
        self._marker("localhost/from-marker:1")
        os.environ["VMF_BASE_IMAGE"] = "localhost/from-env:2"
        self.assertEqual(vmf_plan.fat_base_ref(), "localhost/from-env:2")

    def test_marker_read(self):
        self._marker("localhost/vmf-fat-base:1\n2026-09-23T09:00:00")
        self.assertEqual(vmf_plan.fat_base_ref(), "localhost/vmf-fat-base:1")

    def test_absent_is_empty(self):
        self.assertEqual(vmf_plan.fat_base_ref(), "")

    def test_note_prefers_fat(self):
        os.environ["VMF_BASE_IMAGE"] = "localhost/vmf-fat-base:1"
        note = vmf_plan.base_image_note()
        self.assertIn("localhost/vmf-fat-base:1", note)
        self.assertIn("never install those packages", note)

    def test_note_without_fat_is_minimal(self):
        note = vmf_plan.base_image_note()
        self.assertIn("minimal", note)
        self.assertNotIn("never install", note)


if __name__ == "__main__":
    unittest.main()

class ClampImages(unittest.TestCase):
    def test_short_name_normalized(self):
        self.assertEqual(
            vmf_plan._clamp_images(["ghost:5"]),
            ["docker.io/library/ghost:5"])

    def test_user_repo_qualified(self):
        self.assertEqual(
            vmf_plan._clamp_images(["bitnami/redis:7"]),
            ["docker.io/bitnami/redis:7"])

    def test_explicit_registry_kept(self):
        self.assertEqual(
            vmf_plan._clamp_images(["ghcr.io/app/web:1"]),
            ["ghcr.io/app/web:1"])

    def test_local_tags_kept(self):
        self.assertEqual(
            vmf_plan._clamp_images(["localhost/vmf-fat-base:1"]),
            ["localhost/vmf-fat-base:1"])
