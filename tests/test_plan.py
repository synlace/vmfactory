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
        # healthcheck rides through as an exec check named to its
        # container (the verify runner execs it inside that container)
        self.assertEqual(plan["checks"], [
            {"tcp": {"port": 8080}},
            {"exec": {"cmd": "curl -f http://localhost/ || exit 1",
                      "container": "web"}}])
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


class SynthChecks(unittest.TestCase):
    def test_ports_give_tcp_and_probe(self):
        out = vmf_plan._synth_checks([2368, 2369])
        self.assertEqual(out, [
            {"tcp": {"port": 2368}}, {"tcp": {"port": 2369}},
            {"probe": {"port": 2368, "path": "/", "expect_status_max": 399}}])

    def test_junk_ports_clamped(self):
        out = vmf_plan._synth_checks(["8080", "junk"])
        self.assertEqual([c["tcp"]["port"] for c in out[:1]], [8080])
        self.assertTrue(any("probe" in c for c in out))

    def test_command_only_gives_cmd_ladder(self):
        out = vmf_plan._synth_checks([], ["mvt", "android", "check"])
        self.assertEqual(out, [{"cmd": {"bin": "mvt",
                                        "probes": ["mvt --version",
                                                   "mvt --help"]}}])

    def test_keepalive_command_synthesizes_nothing(self):
        # A keep-alive command names no app; an unverified verdict beats
        # a vacuous `command -v sleep` pass.
        self.assertEqual(vmf_plan._synth_checks([], ["sleep", "100000000"]),
                         [])

    def test_declared_checks_win(self):
        out = vmf_plan._synth_checks([], [])
        self.assertEqual(out, [])

    def test_absolute_path_skipped(self):
        out = vmf_plan._synth_checks([], ["/usr/local/bin/app", "serve"])
        self.assertEqual(out, [])


class BindsAndProfiles(Tmp):
    """translate: relative bind mounts become clone-relative binds,

    profile-gated services drop out (with depends_on cleaned), and the
    flatten mounts them from /data/repo."""

    def _write(self, name, text):
        p = self.path(name)
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        open(p, "w").write(text)
        return p

    def test_binds_parsed_and_profiles_dropped(self):
        compose = self._write("compose.yaml", """
services:
  stripe:
    image: docker.io/stripe/stripe-cli:latest
    entrypoint: ['/entrypoint.sh']
    profiles: ['stripe']
    volumes: ['./docker/stripe/entrypoint.sh:/entrypoint.sh:ro']
  web:
    image: docker.io/library/nginx
    ports: ['8080:80']
    volumes:
      - ./docker/dev/Caddyfile:/etc/caddy/Caddyfile:ro
      - ./apps:/srv/apps:ro
      - shared:/mnt/shared
      - /var/run/x:/var/run/x
  db:
    image: docker.io/library/mariadb:11.8
    depends_on: [stripe]
""")
        self._write("docker/stripe/entrypoint.sh", "#!/bin/sh\n")
        os.makedirs(self.path("apps"))
        plan = vmf_plan.translate(compose, self.tmp, self.tmp)
        names = [s["name"] for s in plan["services"]]
        # The profile-gated opt-in service is not part of the stack.
        self.assertNotIn("stripe", names)
        web = next(s for s in plan["services"] if s["name"] == "web")
        self.assertEqual(web["binds"], [
            {"host": "docker/dev/Caddyfile", "container": "/etc/caddy/Caddyfile",
             "mode": "ro"},
            {"host": "apps", "container": "/srv/apps", "mode": "ro"}])
        # Named and absolute paths pass through untouched.
        self.assertNotIn("binds", next(
            s for s in plan["services"] if s["name"] == "db")) or None

    def test_healthcheck_lifts_container_check(self):
        compose = self._write("compose.yaml", """
services:
  web:
    image: docker.io/library/nginx
    ports: ['8080:80']
  db:
    image: docker.io/library/mysql:8
    healthcheck:
      test: ['CMD', 'mysqladmin', 'ping', '-h', 'localhost']
""")
        plan = vmf_plan.translate(compose, self.tmp, self.tmp)
        self.assertIn(
            {"exec": {"cmd": "mysqladmin ping -h localhost",
                      "container": "db"}},
            plan["checks"])

    def test_memory_mb_from_compose_evidence(self):
        # The ghost failure mode, turned into arithmetic: a 1G innodb
        # buffer pool plus a 500M log buffer inside a 1024 MB VM OOM-
        # kills mysqld. The plan sizes the VM from those flags.
        compose = self._write("compose.yaml", """
services:
  mysql:
    image: docker.io/library/mysql:8.4
    command: ['--innodb-buffer-pool-size=1G', '--innodb-log-buffer-size=500M']
  redis:
    image: docker.io/library/redis:7.4
    command: ['redis-server', '--loglevel', 'warning']
  web:
    image: docker.io/library/nginx
""")
        plan = vmf_plan.translate(compose, self.tmp, self.tmp)
        # floor 2048 + flags (1536 + 500) + 2 extra services (512) = 4584
        self.assertEqual(plan["memory_mb"], 4596)

    def test_memory_mb_floor_and_limit(self):
        compose = self._write("compose.yaml", """
services:
  db:
    image: docker.io/library/postgres:16
    command: ['postgres', '-c', 'shared_buffers=512MB']
    mem_limit: 3g
  app:
    image: docker.io/library/nginx
""")
        plan = vmf_plan.translate(compose, self.tmp, self.tmp)
        # floor 2048 vs limit 3072; flags 500... shared_buffers=512MB
        # = 512; +256 extra service → 2048+512+... = 3840
        self.assertEqual(plan["memory_mb"], 3840)

    def test_plain_stack_stays_at_floor(self):
        compose = self._write("compose.yaml", """
services:
  web:
    image: docker.io/library/nginx
    ports: ['8080:80']
""")
        plan = vmf_plan.translate(compose, self.tmp, self.tmp)
        self.assertEqual(plan["memory_mb"], 2048)

    def test_clamp_keeps_container(self):
        out = vmf_plan._clamp_checks(
            [{"exec": {"cmd": "redis-cli ping", "container": "redis"}},
             {"exec": {"cmd": "  true  ", "container": "  "}}])
        self.assertEqual(out, [
            {"exec": {"cmd": "redis-cli ping", "container": "redis"}},
            {"exec": {"cmd": "true"}}])

    def test_depends_on_dropped_profile_cleaned(self):
        compose = self._write("compose.yaml", """
services:
  stripe:
    image: docker.io/stripe/stripe-cli:latest
    profiles: ['stripe']
  web:
    image: docker.io/library/nginx
    depends_on: [stripe]
""")
        plan = vmf_plan.translate(compose, self.tmp, self.tmp)
        web = plan["services"][0]
        self.assertEqual(web["depends_on"], [])
        self.assertEqual(plan["primary"], "web")

    def test_flatten_mounts_binds_from_repo(self):
        compose = self._write("compose.yaml", """
services:
  web:
    image: docker.io/library/nginx
    ports: ['8080:80']
    volumes: ['./docker/ep.sh:/ep.sh:ro']
""")
        plan = vmf_plan.translate(compose, self.tmp, self.tmp)
        manifest = {"services": plan["services"], "tags": {"web": "localhost/x:0"}}
        out = self.path("flat.yaml")
        ports = self.path("ports.txt")
        with redirect_stdout(io.StringIO()):
            vmf_plan.flatten_cmd(
                self._manifest(manifest), out, ports)
        doc = yaml.safe_load(open(out))
        self.assertEqual(doc["services"]["web"]["volumes"],
                         ["/data/repo/docker/ep.sh:/ep.sh:ro"])

    def _manifest(self, plan):
        p = self.path("manifest.json")
        open(p, "w").write(json.dumps(plan))
        return p


class CmdChecksClamp(unittest.TestCase):
    """The cmd check vocabulary: bin + probe ladder, clamped."""

    def test_full_cmd_check_clamped(self):
        out = vmf_plan._clamp_checks(
            [{"cmd": {"bin": "mvt",
                      "probes": ["mvt --version", "  ", "mvt -V", "x"],
                      "expect_exit": 2, "expect_out": r"v\d+"}}])
        self.assertEqual(out, [{"cmd": {"bin": "mvt",
                                        "probes": ["mvt --version", "mvt -V",
                                                   "x"],
                                        "expect_exit": 2,
                                        "expect_out": r"v\d+"}}])

    def test_bin_derived_from_first_probe(self):
        out = vmf_plan._clamp_checks([{"cmd": {"probes": ["mvt --version"]}}])
        self.assertEqual(out, [{"cmd": {"bin": "mvt",
                                        "probes": ["mvt --version"]}}])

    def test_bin_only_expands_ladder(self):
        out = vmf_plan._clamp_checks([{"cmd": {"bin": "mvt"}}])
        self.assertEqual(out, [{"cmd": {"bin": "mvt",
                                        "probes": ["mvt --version",
                                                   "mvt --help"]}}])

    def test_junk_dropped(self):
        out = vmf_plan._clamp_checks(
            [{"cmd": {}}, {"cmd": {"bin": ""}},
             {"cmd": {"probes": ["   "]}}, {"cmd": "junk"}])
        self.assertEqual(out, [])

    def test_bad_regex_omitted_check_kept(self):
        out = vmf_plan._clamp_checks(
            [{"cmd": {"bin": "mvt", "probes": ["mvt --version"],
                      "expect_out": "(bad"}}])
        self.assertEqual(out, [{"cmd": {"bin": "mvt",
                                        "probes": ["mvt --version"]}}])


class UserClamp(unittest.TestCase):
    """The plan's run account: created by the install list, dropped to
    by the guest init via setuidgid. Apps that refuse root need it."""

    def test_valid_names(self):
        self.assertEqual(vmf_plan._clamp_user("paperclip"), "paperclip")
        self.assertEqual(vmf_plan._clamp_user("App_2"), "app_2")
        self.assertEqual(vmf_plan._clamp_user("svc-worker-2"),
                         "svc-worker-2")
        self.assertEqual(vmf_plan._clamp_user("_svc"), "_svc")

    def test_junk_dropped(self):
        self.assertEqual(vmf_plan._clamp_user(""), "")
        self.assertEqual(vmf_plan._clamp_user(None), "")
        self.assertEqual(vmf_plan._clamp_user("1abc"), "")
        self.assertEqual(vmf_plan._clamp_user("has space"), "")
        self.assertEqual(vmf_plan._clamp_user("/etc/passwd"), "")
        self.assertEqual(vmf_plan._clamp_user("x" * 33), "")

    def test_direct_plan_carries_user(self):
        d = vmf_plan._clamp_direct(
            {"command": ["paperclipai", "run"], "user": "Paperclip"},
            [])
        self.assertEqual(d["user"], "paperclip")


class BaseImageClamp(unittest.TestCase):
    """A direct plan's base_image is ONE oci ref: the fat-base ready
    marker line carries a timestamp, and a model echo of it died at
    derive on buildah's "invalid reference format"."""

    def test_timestamp_dropped(self):
        self.assertEqual(vmf_plan._clamp_base_image(
            "localhost/vmf-fat-base:1 2026-09-23T09:47:02+01:00"),
            "localhost/vmf-fat-base:1")

    def test_plain_ref_unchanged(self):
        self.assertEqual(vmf_plan._clamp_base_image("python:3.12-slim"),
                         "python:3.12-slim")

    def test_empty(self):
        self.assertEqual(vmf_plan._clamp_base_image(None), "")
        self.assertEqual(vmf_plan._clamp_base_image("  "), "")

    def test_direct_plan_base_clamped(self):
        d = vmf_plan._clamp_direct(
            {"base_image": "localhost/vmf-fat-base:1 2026-09-23",
             "command": ["mvt", "version"]}, [])
        self.assertEqual(d["base_image"], "localhost/vmf-fat-base:1")


class FatBaseRef(Tmp):
    """fat_base_ref: the ready marker is "<ref> <timestamp>" on one
    line; only the ref token may reach the plan and derive."""

    def setUp(self):
        super().setUp()
        self.old_home = os.environ["HOME"]
        self.old_base = os.environ.pop("VMF_BASE_IMAGE", None)
        os.environ["HOME"] = self.tmp

    def tearDown(self):
        os.environ["HOME"] = self.old_home
        if self.old_base is not None:
            os.environ["VMF_BASE_IMAGE"] = self.old_base
        else:
            os.environ.pop("VMF_BASE_IMAGE", None)
        super().tearDown()

    def _ready(self, text):
        d = os.path.join(self.tmp, ".local", "share", "vmf")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "fat-base.ready"), "w").write(text)

    def test_marker_timestamp_stripped(self):
        self._ready("localhost/vmf-fat-base:1 2026-09-23T09:47:02+01:00\n")
        self.assertEqual(vmf_plan.fat_base_ref(),
                         "localhost/vmf-fat-base:1")

    def test_env_wins_and_strips(self):
        os.environ["VMF_BASE_IMAGE"] = ("localhost/x:1 "
                                        "2026-09-23T09:47:02+01:00")
        self.assertEqual(vmf_plan.fat_base_ref(), "localhost/x:1")

    def test_missing_marker_empty(self):
        self.assertEqual(vmf_plan.fat_base_ref(), "")
# ---- target-state spec (intent → deliverable contract) ----

import vmf_llm  # noqa: E402


class _Repo(Tmp):
    # A repo fixture builder shared by the fan-out/preview test classes.
    def _mkrepo(self, files):
        d = tempfile.mkdtemp(prefix="vmf-src-", dir=self.tmp)
        for name, content in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(content)
        return d

SPEC_WEB = {"deliverable": "web",
            "serve": {"proto": "http", "port": 3100, "path": "/"},
            "auth": {"required": True, "note": "first-run admin"},
            "env_required": ["BETTER_AUTH_SECRET"],
            "user": "non-root", "hold": 25,
            "why": "paperclip serves web"}


class SpecClamp(Tmp):
    def test_clamp_spec_full(self):
        s = vmf_plan._clamp_spec(dict(SPEC_WEB),
                                 intent="run the web server on port 3100")
        self.assertEqual(s["deliverable"], "web")
        self.assertEqual(s["serve"]["port"], 3100)
        self.assertEqual(s["serve"]["path"], "/")
        self.assertTrue(s["auth"]["required"])
        self.assertEqual(s["env_required"], ["BETTER_AUTH_SECRET"])
        self.assertEqual(s["user"], "non-root")
        self.assertEqual(s["hold"], 25)
        self.assertEqual(s["intent"], "run the web server on port 3100")

    def test_intent_port_wins(self):
        s = vmf_plan._clamp_spec({"deliverable": "web",
                                  "serve": {"port": 9999}},
                                 intent="serve on port 4242")
        self.assertEqual(s["serve"]["port"], 4242)

    def test_junk_degrades(self):
        self.assertIsNone(vmf_plan._clamp_spec("junk"))
        s = vmf_plan._clamp_spec({"deliverable": "app",
                                  "serve": {"proto": "gopher", "path": "x"},
                                  "env_required": ["lower", "OK_KEY"],
                                  "user": "1bad", "hold": 9999})
        self.assertEqual(s["deliverable"], "")
        self.assertEqual(s["serve"]["proto"], "http")
        self.assertEqual(s["serve"]["path"], "/")
        self.assertEqual(s["env_required"], ["OK_KEY"])
        self.assertEqual(s["user"], "")
        self.assertEqual(s["hold"], 120)

    def test_is_keepalive(self):
        self.assertTrue(vmf_plan._is_keepalive(["sleep", "100000000"]))
        self.assertTrue(vmf_plan._is_keepalive(["tail", "-f", "/x"]))
        self.assertFalse(vmf_plan._is_keepalive(["node", "server.js"]))
        self.assertFalse(vmf_plan._is_keepalive([]))


class SpecDerive(Tmp):
    def setUp(self):
        super().setUp()
        os.environ["VMF_GENERATED"] = os.path.join(self.tmp, "gen")
        os.environ.pop("VMF_RUN_INTENT", None)
        os.environ.pop("VMF_PLAN_SPEC", None)

    def tearDown(self):
        os.environ.pop("VMF_GENERATED", None)
        os.environ.pop("VMF_RUN_INTENT", None)
        os.environ.pop("VMF_PLAN_SPEC", None)
        super().tearDown()

    def test_no_intent_no_spec_no_calls(self):
        self.calls = []
        old = vmf_llm.llm_call
        vmf_llm.llm_call = \
            lambda *a, **k: self.calls.append(a) or (0, "", "")
        try:
            s = vmf_plan.load_or_derive_spec(self.tmp, self.path("gen"))
        finally:
            vmf_llm.llm_call = old
        self.assertIsNone(s)
        self.assertEqual(self.calls, [])

    def test_derive_persists_and_reuses(self):
        self.calls = []
        old = vmf_llm.llm_call
        vmf_llm.llm_call = lambda *a, **k: self.calls.append(a) \
            or (0, json.dumps(SPEC_WEB), "")
        try:
            gen = self.path("gen")
            os.environ["VMF_RUN_INTENT"] = "run the web server on port 3100"
            s = vmf_plan.load_or_derive_spec(self.tmp, gen)
            self.assertEqual(s["deliverable"], "web")
            n = len(self.calls)
            s2 = vmf_plan.load_or_derive_spec(self.tmp, gen)
            self.assertEqual(s2["deliverable"], "web")
            self.assertEqual(len(self.calls), n)
        finally:
            vmf_llm.llm_call = old

    def test_transport_down_falls_back_deterministic(self):
        root = self.path("repo")
        os.makedirs(root)
        with open(os.path.join(root, "compose.yml"), "w") as f:
            f.write("services:\n  web:\n    image: nginx\n"
                    "    ports: ['8080:80']\n")
        old = vmf_llm.llm_call
        vmf_llm.llm_call = lambda *a, **k: (1, "", "down")
        try:
            s = vmf_plan.derive_spec(root, "run the web server on port 3100")
        finally:
            vmf_llm.llm_call = old
        self.assertEqual(s["deliverable"], "web")
        self.assertEqual(s["serve"]["port"], 3100)
        self.assertEqual(s["why"], "deterministic fallback")

    def test_fallback_cli_without_anything(self):
        root = self.path("repo2")
        os.makedirs(root)
        s = vmf_plan._fallback_spec(root, "")
        self.assertEqual(s["deliverable"], "cli")

    def test_spec_block_shape(self):
        b = vmf_plan.spec_block(dict(SPEC_WEB))
        self.assertIn("deliverable: web", b)
        self.assertIn("http://0.0.0.0:3100", b)
        self.assertIn("keep-alive", b)
        self.assertEqual(vmf_plan.spec_block(None), "")
        self.assertEqual(vmf_plan.spec_block({"deliverable": ""}), "")


class FanoutSpec(_Repo):
    def setUp(self):
        super().setUp()
        os.environ["VMF_GENERATED"] = os.path.join(self.tmp, "gen")
        os.environ.pop("VMF_RUN_INTENT", None)
        os.environ.pop("VMF_PLAN_SPEC", None)
        self.calls = []
        self.old = (vmf_llm.llm_call, vmf_llm.ground)
        vmf_llm.ground = lambda lookup, doc_cap=3000: ("", [])

    def tearDown(self):
        (vmf_llm.llm_call, vmf_llm.ground) = self.old
        os.environ.pop("VMF_GENERATED", None)
        os.environ.pop("VMF_RUN_INTENT", None)
        os.environ.pop("VMF_PLAN_SPEC", None)
        super().tearDown()

    def _stub(self, responder):
        def fake(role, prompt, timeout=90, env=None):
            self.calls.append(prompt)
            return 0, responder(prompt), ""
        vmf_llm.llm_call = fake

    def _pkg(self, body):
        def responder(prompt):
            if prompt.startswith("Derive the target state"):
                return json.dumps(SPEC_WEB)
            if "runtime packages" in prompt:
                return json.dumps(body)
            return json.dumps({"status": "blocked", "why": "nope"})
        return responder

    def test_intent_reaches_planner_prompts(self):
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        self._stub(self._pkg({"status": "plan",
                              "install": ["npm i -g paperclipai"],
                              "command": ["paperclipai", "run"],
                              "ports": [3100],
                              "checks": [{"probe": {"port": 3100,
                                                    "path": "/"}}],
                              "user": "pc", "notes": "web tool"}))
        os.environ["VMF_RUN_INTENT"] = "run the web server on port 3100"
        out = self.path("fa.json")
        rc = vmf_plan.fanout_cmd(src, out)
        self.assertEqual(rc, 0)
        pkg = [p for p in self.calls if "runtime packages" in p]
        self.assertTrue(pkg)
        self.assertIn("deliverable: web", pkg[0])
        self.assertIn("0.0.0.0:3100", pkg[0])
        self.assertNotIn('set command to ["sleep", "100000000"]', pkg[0])

    def test_keepalive_plan_blocked_under_web(self):
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        self._stub(self._pkg({"status": "plan",
                              "install": ["npm i -g paperclipai"],
                              "command": ["sleep", "100000000"],
                              "notes": "cli"}))
        os.environ["VMF_RUN_INTENT"] = "run the web server on port 3100"
        out = self.path("fa.json")
        rc = vmf_plan.fanout_cmd(src, out)
        self.assertEqual(rc, 1)
        gen = os.path.join(self.tmp, "gen",
                           os.listdir(os.path.join(self.tmp, "gen"))[0])
        b = json.load(open(os.path.join(gen, "plan-pkg.json.blocked")))
        self.assertEqual(b["why"], "plan ignores the web target")
        self.assertTrue(b.get("spec_h"))

    def test_same_spec_reruns_free(self):
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        self._stub(self._pkg({"status": "plan",
                              "install": ["npm i -g x"],
                              "command": ["x", "run"], "ports": [3100],
                              "checks": [{"probe": {"port": 3100,
                                                    "path": "/"}}],
                              "notes": "n"}))
        os.environ["VMF_RUN_INTENT"] = "run the web server on port 3100"
        out = self.path("fa.json")
        vmf_plan.fanout_cmd(src, out)
        n = len(self.calls)
        rc = vmf_plan.fanout_cmd(src, out)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), n)

    def test_spec_change_replans(self):
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        self._stub(self._pkg({"status": "plan",
                              "install": ["npm i -g x"],
                              "command": ["x", "run"], "ports": [3100],
                              "checks": [{"probe": {"port": 3100,
                                                    "path": "/"}}],
                              "notes": "n"}))
        os.environ["VMF_RUN_INTENT"] = "run the web server on port 3100"
        out = self.path("fa.json")
        vmf_plan.fanout_cmd(src, out)
        n = len(self.calls)
        os.environ["VMF_RUN_INTENT"] = "run the web server on port 3200"
        rc = vmf_plan.fanout_cmd(src, out)
        self.assertEqual(rc, 0)
        self.assertGreater(len(self.calls), n)
        gen = os.path.join(self.tmp, "gen")
        keydir = os.path.join(gen, os.listdir(gen)[0])
        doc = json.load(open(os.path.join(keydir, "plan-pkg.json")))
        self.assertEqual(doc["approach"]["ports"], [3100])


class PreviewRender(_Repo):
    def setUp(self):
        super().setUp()
        os.environ["VMF_GENERATED"] = os.path.join(self.tmp, "gen")
        os.environ.pop("VMF_RUN_INTENT", None)
        os.environ.pop("VMF_PLAN_SPEC", None)
        self.old = (vmf_llm.llm_call, vmf_llm.ground)
        vmf_llm.ground = lambda lookup, doc_cap=3000: ("", [])

    def tearDown(self):
        (vmf_llm.llm_call, vmf_llm.ground) = self.old
        os.environ.pop("VMF_GENERATED", None)
        os.environ.pop("VMF_RUN_INTENT", None)
        os.environ.pop("VMF_PLAN_SPEC", None)
        super().tearDown()

    def _stub(self, responder):
        def fake(role, prompt, timeout=90, env=None):
            return 0, responder(prompt), ""
        vmf_llm.llm_call = fake

    def _pkg(self, body):
        def responder(prompt):
            if prompt.startswith("Derive the target state"):
                return json.dumps(SPEC_WEB)
            if "runtime packages" in prompt:
                return json.dumps(body)
            return json.dumps({"status": "blocked", "why": "nope"})
        return responder

    def test_renders_spec_and_plans(self):
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        self._stub(self._pkg({"status": "plan",
                              "install": ["npm i -g paperclipai",
                                          "useradd -m pc"],
                              "command": ["paperclipai", "run"],
                              "ports": [3100],
                              "checks": [{"probe": {"port": 3100,
                                                    "path": "/"}}],
                              "user": "pc", "notes": "web tool"}))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = vmf_plan.preview_cmd(
                src, intent="run the web server on port 3100")
        self.assertEqual(rc, 0)
        o = buf.getvalue()
        self.assertIn("deliverable  web", o)
        self.assertIn("0.0.0.0:3100", o)
        self.assertIn("plan 1  direct", o)
        self.assertIn("paperclipai run", o)
        self.assertIn("as pc", o)
        self.assertIn("probe:/", o)
        self.assertIn("dry run", o)
        # The plan landed in the race's cache: a run right after replays.
        gen = os.path.join(self.tmp, "gen",
                           os.listdir(os.path.join(self.tmp, "gen"))[0])
        self.assertTrue(os.path.isfile(os.path.join(gen, "plan-pkg.json")))
        self.assertTrue(os.path.isfile(os.path.join(gen, "spec.json")))

    def test_exit_1_when_nothing_serves(self):
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        self._stub(self._pkg({"status": "plan",
                              "install": ["npm i -g x"],
                              "command": ["sleep", "100000000"],
                              "notes": "cli"}))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = vmf_plan.preview_cmd(
                src, intent="run the web server on port 3100")
        self.assertEqual(rc, 1)
        self.assertIn("plan ignores the web target", buf.getvalue())

    def test_json_and_lane(self):
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        self._stub(self._pkg({"status": "plan",
                              "install": ["npm i -g x"],
                              "command": ["x", "run"], "ports": [3100],
                              "checks": [{"probe": {"port": 3100,
                                                    "path": "/"}}],
                              "notes": "n"}))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = vmf_plan.preview_cmd(
                src, intent="run the web server on port 3100", as_json=True)
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["spec"]["deliverable"], "web")
        self.assertEqual(doc["approaches"][0]["method"], "pkg")
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc = vmf_plan.preview_cmd(
                src, intent="run the web server on port 3100", lane=1)
        self.assertEqual(rc, 0)
        self.assertIn("plan 1 ", buf2.getvalue())

# ---- the rich render (terminals; plain stays the fallback) ----

try:
    import rich.console as _rich_console_mod
    _HAVE_RICH = True
except ImportError:
    _HAVE_RICH = False


RICH_APPROACHES = [{
    "kind": "install_script", "evidence": "fanout: pkg paperclip",
    "cost": "slow", "method": "pkg", "ports": [3100],
    "install": ["npm i -g paperclipai", "useradd -m pc"],
    "checks": [{"probe": {"port": 3100, "path": "/"}}],
    "env": {"PAPERCLIP_HOME": "/paperclip"},
    "direct": {"base_image": "node:24-trixie-slim",
               "command": ["paperclipai", "run"], "user": "pc",
               "checks": [{"probe": {"port": 3100, "path": "/"}}],
               "env": {"PAPERCLIP_HOME": "/paperclip"},
               "notes": "web tool"},
}]


@unittest.skipUnless(_HAVE_RICH, "rich not installed")
class PreviewRich(unittest.TestCase):
    def test_rich_matches_plain_words(self):
        cons = _rich_console_mod.Console(
            file=io.StringIO(), width=102, highlight=False,
            soft_wrap=False)
        vmf_plan.render_preview_rich(
            cons, dict(SPEC_WEB), RICH_APPROACHES,
            {"compose": "no complete compose file"},
            ["Dockerfile", "package.json"],
            ["just plan x", "    --intent \"web server on port 3100\""])
        out = cons.file.getvalue()
        for w in ("vmf plan", "scout", "deliverable", "web",
                  "0.0.0.0:3100", "auth", "required", "user",
                  "non-root", "hold", "25s", "lane 1", "✗ compose",
                  "plan 1", "direct", "install_script", "slow",
                  "npm i -g paperclipai", "paperclipai run", "as pc",
                  "PAPERCLIP_HOME=/paperclip", "tcp:3100", "probe:/",
                  "winner needs 2/2 checks", "floor: tcp + hold",
                  "model adds: probe", "web tool",
                  "dry run — nothing booted"):
            self.assertIn(w, out, w)

    def test_lane_filter_and_cli_class(self):
        cons = _rich_console_mod.Console(
            file=io.StringIO(), width=102, highlight=False,
            soft_wrap=False)
        cli = [{"kind": "install_script", "cost": "slow", "method": "pkg",
                "ports": [], "install": ["pip install mvt"],
                "direct": {"command": ["sleep", "100000000"],
                           "checks": [{"cmd": {"bin": "mvt",
                                               "probes": ["mvt --version"]}}],
                           "notes": "cli"}}]
        vmf_plan.render_preview_rich(
            cons, None, cli, {}, ["package.json"],
            ["just plan mvt"], lane=1)
        out = cons.file.getvalue()
        self.assertIn("(none — pass --intent", out)
        self.assertIn("keep-alive", out)
        self.assertIn("cmd:mvt", out)
        self.assertNotIn("plan 1 · direct · install_script · slowest",
                         out)

    def test_console_gate(self):
        # Piped stdout is plain even with rich installed; VMF_PLAN_PLAIN
        # forces plain even on a tty.
        self.assertIsNone(vmf_plan._preview_console())
        old = sys.stdout

        class FakeTty:
            def isatty(self):
                return True
        sys.stdout = FakeTty()
        try:
            os.environ["VMF_PLAN_PLAIN"] = "1"
            self.assertIsNone(vmf_plan._preview_console())
            os.environ.pop("VMF_PLAN_PLAIN", None)
            cons = vmf_plan._preview_console()
            if _HAVE_RICH:
                self.assertIsNotNone(cons)
            else:
                self.assertIsNone(cons)
        finally:
            sys.stdout = old
            os.environ.pop("VMF_PLAN_PLAIN", None)
# ---- render fixes from the DVWA preview (2026-09-27) ----

class PlanRenderFixes(_Repo):
    def test_shell_payload_not_keepalive(self):
        # A bare shell is a keep-alive; a shell carrying a serving
        # payload is the app (measured: DVWA apache2ctl under bash -c).
        self.assertFalse(vmf_plan._is_keepalive(
            ["bash", "-c",
             "service mariadb start && exec apache2ctl -DFOREGROUND"]))
        self.assertFalse(vmf_plan._is_keepalive(
            ["sh", "-c", "exec node server.js"]))
        self.assertTrue(vmf_plan._is_keepalive(["bash"]))
        self.assertTrue(vmf_plan._is_keepalive(
            ["bash", "-c", "sleep 100000000"]))

    def test_approaches_carry_notes(self):
        os.environ["VMF_GENERATED"] = os.path.join(self.tmp, "gen")
        self.addCleanup(os.environ.pop, "VMF_GENERATED", None)
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        old = (vmf_llm.llm_call, vmf_llm.ground)
        vmf_llm.ground = lambda lookup, doc_cap=3000: ("", [])
        vmf_llm.llm_call = lambda *a, **k: (
            0, json.dumps({"status": "plan", "install": ["npm i -g x"],
                           "command": ["x", "run"], "ports": [3100],
                           "checks": [{"probe": {"port": 3100,
                                                 "path": "/"}}],
                           "notes": "needs a db sidecar"}), "")
        try:
            out = self.path("fa.json")
            rc = vmf_plan.fanout_cmd(src, out)
        finally:
            (vmf_llm.llm_call, vmf_llm.ground) = old
        self.assertEqual(rc, 0)
        doc = json.load(open(out))
        self.assertEqual(doc["approaches"][0]["notes"],
                         "needs a db sidecar")

    def test_root_user_not_tagged(self):
        if not _HAVE_RICH:
            self.skipTest("rich not installed")
        ap = dict(RICH_APPROACHES[0])
        ap = {**ap, "direct": {**ap["direct"], "user": "root",
                               "command": ["apache2ctl", "-DFOREGROUND"]}}
        cons = _rich_console_mod.Console(
            file=io.StringIO(), width=102, highlight=False,
            soft_wrap=False)
        vmf_plan.render_preview_rich(
            cons, None, [ap], {}, ["Dockerfile"], ["just plan x"])
        out = cons.file.getvalue()
        self.assertIn("apache2ctl -DFOREGROUND", out)
        self.assertNotIn("as root", out)
    def test_notes_cap_120(self):
        # The 40-char board column truncated panels mid-word
        # (measured: "login veri"); notes now carry the whole story.
        os.environ["VMF_GENERATED"] = os.path.join(self.tmp, "gen")
        self.addCleanup(os.environ.pop, "VMF_GENERATED", None)
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        long_notes = "DB seeded via setup.php POST; login verified " \
            "with admin/password; expect index after auth"
        old = (vmf_llm.llm_call, vmf_llm.ground)
        vmf_llm.ground = lambda lookup, doc_cap=3000: ("", [])
        vmf_llm.llm_call = lambda *a, **k: (
            0, json.dumps({"status": "plan", "install": ["npm i -g x"],
                           "command": ["x", "run"], "ports": [3100],
                           "checks": [{"probe": {"port": 3100,
                                                 "path": "/"}}],
                           "notes": long_notes}), "")
        try:
            out = self.path("fa.json")
            rc = vmf_plan.fanout_cmd(src, out)
        finally:
            (vmf_llm.llm_call, vmf_llm.ground) = old
        self.assertEqual(rc, 0)
        doc = json.load(open(out))
        self.assertEqual(doc["approaches"][0]["notes"], long_notes)

    def test_env_shape_teaches_literal_values(self):
        # The old shape taught "V — explanation" and the model copied
        # it into real values (measured: DVWA's sidecar password was
        # prose). No prompt may carry that shape again.
        os.environ["VMF_GENERATED"] = os.path.join(self.tmp, "gen")
        self.addCleanup(os.environ.pop, "VMF_GENERATED", None)
        src = self._mkrepo({"README.md": "# app\n",
                            "Dockerfile": "FROM node:24\n",
                            "package.json": '{"name": "pc"}'})
        prompts = []
        old = (vmf_llm.llm_call, vmf_llm.ground)
        vmf_llm.ground = lambda lookup, doc_cap=3000: ("", [])
        vmf_llm.llm_call = lambda role, prompt, timeout=90, env=None: \
            prompts.append(prompt) or (
                0, json.dumps({"status": "blocked", "why": "nope"}), "")
        try:
            vmf_plan.fanout_cmd(src, self.path("fa.json"))
        finally:
            (vmf_llm.llm_call, vmf_llm.ground) = old
        self.assertTrue(prompts)
        for p in prompts:
            self.assertNotIn('"V — ', p)
            self.assertNotIn('"K": "V —', p)
# ---- live plan assembly (streaming fanout + rich board) ----

class FanoutStreaming(_Repo):
    def setUp(self):
        super().setUp()
        os.environ["VMF_GENERATED"] = os.path.join(self.tmp, "gen")
        self.addCleanup(os.environ.pop, "VMF_GENERATED", None)

    def test_progress_events_and_immediate_writes(self):
        os.environ["VMF_RUN_INTENT"] = "run the web server on port 3100"
        self.addCleanup(os.environ.pop, "VMF_RUN_INTENT", None)
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        old = (vmf_llm.llm_call, vmf_llm.ground)
        vmf_llm.ground = lambda lookup, doc_cap=3000: ("", [])

        def fake(role, prompt, timeout=90, env=None):
            if prompt.startswith("Derive the target state"):
                return 1, "", "down"
            if "runtime packages" in prompt:
                return 0, json.dumps(
                    {"status": "plan", "install": ["npm i -g x"],
                     "command": ["x", "run"], "ports": [3100],
                     "checks": [{"probe": {"port": 3100, "path": "/"}}],
                     "notes": "n"}), ""
            return 0, json.dumps({"status": "blocked",
                                  "why": "no image"}), ""
        vmf_llm.llm_call = fake
        events = []
        try:
            out = self.path("fa.json")
            rc = vmf_plan.fanout_cmd(src, out,
                                     progress=lambda ev, *a:
                                     events.append((ev,) + a))
        finally:
            (vmf_llm.llm_call, vmf_llm.ground) = old
        self.assertEqual(rc, 0)
        evs = [e[0] for e in events]
        self.assertIn("grounding", evs)
        self.assertIn("grounded", evs)
        self.assertIn("spec", evs)
        self.assertIn("done", evs)
        methods = {e[1]: e[2] for e in events if e[0] == "method"}
        self.assertEqual(methods.get("pkg"), "plan")
        self.assertEqual(methods.get("prebuilt"), "blocked")
        self.assertEqual(methods.get("source"), None)
        skipped = [e for e in events if e[0] == "skipped"]
        self.assertEqual(sorted(s[1] for s in skipped), ["build", "compose", "source"])
        doc = json.load(open(out))
        self.assertEqual(doc["approaches"][0]["method"], "pkg")

    def test_plain_path_streams_per_method(self):
        # Without a progress callback the stderr lines land as each
        # call returns (the plain terminal sees the same assembly).
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        old = (vmf_llm.llm_call, vmf_llm.ground)
        vmf_llm.ground = lambda lookup, doc_cap=3000: ("", [])
        vmf_llm.llm_call = lambda *a, **k: (
            0, json.dumps({"status": "plan", "install": ["npm i -g x"],
                           "command": ["x", "run"], "ports": [3100],
                           "checks": [{"probe": {"port": 3100,
                                                 "path": "/"}}],
                           "notes": "n"}), "")
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                rc = vmf_plan.fanout_cmd(src, self.path("fa.json"))
        finally:
            (vmf_llm.llm_call, vmf_llm.ground) = old
        self.assertEqual(rc, 0)
        o = err.getvalue()
        self.assertIn("fanout: pkg       .. plan", o)
        self.assertIn("fanout: prebuilt  .. blocked", o)
        self.assertIn("fanout: %d runnable" % 1, o)


@unittest.skipUnless(_HAVE_RICH, "rich not installed")
class LiveBoard(unittest.TestCase):
    def _board(self):
        b = vmf_plan._PlanLive("dvwa")
        b.feed("stage", "read")
        b.feed("found", ["Dockerfile", "compose.yml"])
        b.feed("spec_wait")
        b.feed("spec", dict(SPEC_WEB))
        b.feed("skipped", "source", "no build manifest")
        b.feed("grounding")
        b.feed("method", "prebuilt", "plan", "Guest MariaDB backs image")
        b.feed("method", "compose", "blocked", "no standalone compose")
        b.feed("method", "build", "blocked", "needs a sidecar")
        b.feed("method", "pkg", "plan", "DB seeded via database.sql")
        b.feed("done", 2, 2)
        return b

    def test_renders_states_and_events(self):
        cons = _rich_console_mod.Console(
            file=io.StringIO(), width=100, highlight=False,
            soft_wrap=False)
        b = self._board()
        cons.print(b)
        out = cons.file.getvalue()
        for w in ("dvwa", "dry run", "deliverable", "web", "0.0.0.0:3100",
                  "prebuilt", "plan", "Guest MariaDB backs image",
                  "compose", "blocked", "source", "skipped",
                  "grounding", "[prebuilt]", "[compose]", "[pkg]", "6 llm"):
            self.assertIn(w, out, w)
        self.assertNotIn("waiting", out)

    def test_waiting_rows_spin(self):
        cons = _rich_console_mod.Console(
            file=io.StringIO(), width=100, highlight=False,
            soft_wrap=False)
        b = vmf_plan._PlanLive("dvwa")
        b.feed("stage", "read")
        cons.print(b)
        f1 = cons.file.getvalue()
        cons.file = io.StringIO()
        cons.print(b)
        f2 = cons.file.getvalue()
        self.assertIn("waiting", f2)
        self.assertNotEqual(
            [l for l in f1.splitlines() if "pkg" in l],
            [l for l in f2.splitlines() if "pkg" in l])
# ---- clean live surface (quiet stderr + transient board) ----

class FanoutQuiet(FanoutStreaming):
    def test_quiet_silences_stderr_keeps_events(self):
        os.environ["VMF_RUN_INTENT"] = "run the web server on port 3100"
        self.addCleanup(os.environ.pop, "VMF_RUN_INTENT", None)
        src = self._mkrepo({"package.json": '{"name": "pc"}'})
        old = (vmf_llm.llm_call, vmf_llm.ground)
        vmf_llm.ground = lambda lookup, doc_cap=3000: ("", [])
        vmf_llm.llm_call = lambda *a, **k: (
            0, json.dumps({"status": "plan", "install": ["npm i -g x"],
                           "command": ["x", "run"], "ports": [3100],
                           "checks": [{"probe": {"port": 3100,
                                                 "path": "/"}}],
                           "notes": "n"}), "")
        events = []
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                rc = vmf_plan.fanout_cmd(
                    src, self.path("fa.json"),
                    progress=lambda ev, *a: events.append((ev,) + a),
                    quiet=True)
        finally:
            (vmf_llm.llm_call, vmf_llm.ground) = old
        self.assertEqual(rc, 0)
        self.assertNotIn("fanout:", err.getvalue())
        evs = [e[0] for e in events]
        self.assertIn("method", evs)
        self.assertIn("done", evs)


@unittest.skipUnless(_HAVE_RICH, "rich not installed")
class TallyFooter(unittest.TestCase):
    def test_footer_carries_tally(self):
        cons = _rich_console_mod.Console(
            file=io.StringIO(), width=102, highlight=False,
            soft_wrap=False)
        vmf_plan.render_preview_rich(
            cons, None, RICH_APPROACHES, {}, ["package.json"],
            ["just plan x"], tally=(2, 3, 0))
        out = cons.file.getvalue()
        self.assertIn("2 runnable, 3 blocked/skipped · 0 llm", out)
        self.assertIn("dry run — nothing booted", out)

    def test_footer_without_tally_unchanged(self):
        cons = _rich_console_mod.Console(
            file=io.StringIO(), width=102, highlight=False,
            soft_wrap=False)
        vmf_plan.render_preview_rich(
            cons, None, RICH_APPROACHES, {}, ["package.json"],
            ["just plan x"])
        out = cons.file.getvalue()
        self.assertNotIn("runnable,", out)
