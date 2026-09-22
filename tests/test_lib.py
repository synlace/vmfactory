# Contract tests for the shared bash helpers (scripts/vmf_lib.sh):
# tool provisioning and the forward-line parser.
#
# Run: uv run --with pyyaml --with jsonschema python -m unittest discover tests
import os
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
LIB = os.path.join(SCRIPTS, "vmf_lib.sh")


def run_bash(script):
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c",
         '. "%s"\n%s' % (LIB, script)],
        capture_output=True, text=True)


class FwdParse(unittest.TestCase):
    def _fields(self, line):
        proc = run_bash('vmf_fwd_parse %s; echo "$VMF_FWD_PROTO|$VMF_FWD_BIND|$VMF_FWD_HOST|$VMF_FWD_GUEST"'
                        % ("'%s'" % line if line else "''"))
        return proc

    def test_two_field_legacy_tcp(self):
        out = self._fields("8080 80").stdout.strip()
        self.assertEqual(out, "tcp||8080|80")

    def test_three_field_proto(self):
        out = self._fields("udp 53 53").stdout.strip()
        self.assertEqual(out, "udp||53|53")

    def test_four_field_bind(self):
        out = self._fields("tcp 127.0.0.1 4280 80").stdout.strip()
        self.assertEqual(out, "tcp|127.0.0.1|4280|80")

    def test_four_field_bind_all_collapses(self):
        out = self._fields("tcp 0.0.0.0 4280 80").stdout.strip()
        self.assertEqual(out, "tcp||4280|80")

    def test_blank_is_rc1(self):
        proc = self._fields("")
        self.assertNotEqual(proc.returncode, 0)

    def test_single_field_is_rc1(self):
        proc = self._fields("8080")
        self.assertNotEqual(proc.returncode, 0)


class ToolProvisioning(unittest.TestCase):
    def test_present_binary_uses_host(self):
        out = run_bash('vmf_tool bash; echo "${TOOL[*]}"').stdout.strip()
        self.assertEqual(out, "bash")

    def test_missing_binary_gets_nix_prefix(self):
        out = run_bash('vmf_tool no-such-bin-xyz-vmf; echo "${TOOL[*]}"').stdout.strip()
        self.assertEqual(out, "nix shell nixpkgs#no-such-bin-xyz-vmf -c no-such-bin-xyz-vmf")

    def test_tools_all_present_is_empty(self):
        out = run_bash('vmf_tools git curl; echo "${#TOOL[@]}"').stdout.strip()
        self.assertEqual(out, "0")

    def test_tools_missing_gets_prefix(self):
        out = run_bash('vmf_tools git no-such-bin-xyz-vmf; echo "${TOOL[*]}"').stdout.strip()
        self.assertEqual(out, "nix shell nixpkgs#git nixpkgs#no-such-bin-xyz-vmf -c")

    def test_run_present_pkgs(self):
        out = run_bash('vmf_run coreutils -- echo hi').stdout.strip()
        self.assertEqual(out, "hi")

    def test_run_pkg_differs_from_cmd(self):
        # pkg list and command are independent: coreutils provides the
        # env, sha256sum is the command.
        out = run_bash('vmf_run coreutils -- sha256sum /dev/null').stdout
        self.assertIn("e3b0c44298fc1c14", out)


class InstanceResolve(unittest.TestCase):
    """vmf_instance_dir: name symlink, full id, unique prefix, ambiguity,
    legacy flat layout."""

    def setUp(self):
        import tempfile
        self.runs = tempfile.mkdtemp(prefix="vmf-inst-")
        for ident, name in (("aabb11223344", "web"),
                            ("ccdd55667788", "web2")):
            d = os.path.join(self.runs, ident)
            os.makedirs(d)
            open(os.path.join(d, "conf"), "w").write("ID=%s\nNAME=%s\n" % (ident, name))
            os.symlink(ident, os.path.join(self.runs, name))

    def _resolve(self, ref):
        # set -e in the harness: the resolver's rc=1 must not kill the
        # script, so capture the rc explicitly.
        return run_bash(
            'export VMF_RUNS=%s; rc=0; vmf_instance_dir %s || rc=$?; '
            'printf "rc=$rc dir=${VMF_INST_DIR:-} conf=${VMF_INST_CONF:-} err=${VMF_INST_ERR:-}"'
            % (self.runs, ref)).stdout.strip()

    def test_name_via_symlink(self):
        self.assertIn("dir=%s/aabb11223344" % self.runs, self._resolve("web"))

    def test_full_id(self):
        self.assertIn("dir=%s/ccdd55667788" % self.runs, self._resolve("ccdd55667788"))

    def test_unique_prefix(self):
        self.assertIn("dir=%s/aabb11223344" % self.runs, self._resolve("aabb"))

    def test_no_match_rc1(self):
        out = self._resolve("zzzz")
        self.assertIn("rc=1", out)
        self.assertIn("no VM", out)

    def test_legacy_flat_conf(self):
        open(os.path.join(self.runs, "oldvm.conf"), "w").write("PORT=1\n")
        out = self._resolve("oldvm")
        self.assertIn("rc=0", out)
        self.assertIn("conf=%s/oldvm.conf" % self.runs, out)


class InstanceNumbering(unittest.TestCase):
    """vmf_number_instance: free name stays bare; a running holder
    numbers the next run; handoffs and --replace keep the name."""

    def _num(self, base, replace="0", extra_env=""):
        return run_bash(
            'export VMF_RUNS=%s; %s vmf_number_instance %s %s || true; '
            % (self.runs, extra_env, base, replace)).stdout.strip()

    def setUp(self):
        import tempfile, subprocess
        self.runs = tempfile.mkdtemp(prefix="vmf-num-")
        # A running legacy instance: conf + a live pid (this shell's).
        d = tempfile.mkdtemp(prefix="vmf-num-inst-", dir=self.runs)
        self.live_pid = str(subprocess.Popen(["sleep", "30"]).pid)
        open(os.path.join(d, "conf"), "w").write(
            "ID=aabb11223344\nNAME=web\nPID=%s\n" % self.live_pid)
        os.symlink(os.path.basename(d), os.path.join(self.runs, "web"))

    def tearDown(self):
        subprocess.run(["kill", self.live_pid], capture_output=True)

    def test_free_name_stays_bare(self):
        self.assertEqual(self._num("freename"), "freename")

    def test_running_name_numbers(self):
        out = self._num("web")
        self.assertEqual(out, "web-2")

    def test_replace_keeps_bare(self):
        self.assertEqual(self._num("web", "1"), "web")

    def test_race_child_keeps_name(self):
        out = run_bash(
            'export VMF_RUNS=%s VMF_RACE_CHILD=1; vmf_number_instance web 0'
            % self.runs).stdout.strip()
        self.assertEqual(out, "web")

    def test_handoff_env_keeps_name(self):
        out = run_bash(
            'export VMF_RUNS=%s VMF_NAME=web; vmf_number_instance web 0'
            % self.runs).stdout.strip()
        self.assertEqual(out, "web")


if __name__ == "__main__":
    unittest.main()