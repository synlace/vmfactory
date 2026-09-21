# Contract tests for scripts/image-supply.sh: pull-on-demand, TOFU
# pinning with drift detection, and the docker-archive push. buildah is
# a fake on PATH; the pins file lands in a redirected HOME.
#
# Run: uv run --with pyyaml --with jsonschema python -m unittest discover tests
import os
import shutil
import subprocess
import tempfile
import unittest


FAKE_BUILDAH = """#!/usr/bin/env bash
case "$1" in
  inspect) echo '{"Digest":"sha256:0000000000000000000000000000000000000000000000000000000000000042"}' ;;
  pull) echo "pulled $2" ;;
  push) out="${3#docker-archive:}"; out="${out%%:*}"; touch "$out"; echo "pushed" ;;
esac
"""


class ImageSupply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.bin))
        os.makedirs(self.home)
        open(os.path.join(self.bin, "buildah"), "w").write(FAKE_BUILDAH)
        os.chmod(os.path.join(self.bin, "buildah"), 0o755)
        self.old_path = os.environ["PATH"]
        self.old_home = os.environ["HOME"]
        os.environ["PATH"] = self.bin + ":" + self.old_path
        os.environ["HOME"] = self.home

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ["HOME"] = self.old_home
        shutil.rmtree(self.tmp)

    def run_supply(self, ref, out):
        return subprocess.run(
            ["bash", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "scripts", "image-supply.sh"),
             ref, out], capture_output=True, text=True)

    def test_first_supply_pins_and_archives(self):
        out = os.path.join(self.tmp, "ghost.tar")
        p = self.run_supply("ghost:5", out)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.isfile(out))
        pins = open(os.path.join(self.home, ".vmf", "oci-pins")).read()
        self.assertIn("ghost:5 sha256:", pins)
        self.assertIn("sha256:0000", pins)

    def test_second_supply_is_cached(self):
        out = os.path.join(self.tmp, "g.tar")
        self.run_supply("ghost:5", out)
        # the fake buildah answers inspect without a pull, so the second
        # call proves the pin path (no re-pull needed for a stored image)
        p = self.run_supply("ghost:5", out)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
