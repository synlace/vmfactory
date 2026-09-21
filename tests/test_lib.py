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


if __name__ == "__main__":
    unittest.main()