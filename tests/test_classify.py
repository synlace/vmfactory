# Contract tests for the classifier (vmf_plan.py classify) and the
# evidence profile ladder (profile + formats.yaml).
#
# Run: uv run --with pyyaml --with jsonschema python -m unittest discover tests
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import vmf_plan  # noqa: E402


def run_classify(*args):
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"), "classify", *args],
        capture_output=True, text=True)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmf-cla-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def _mk(self, name, data, mode="w"):
        p = self.path(name)
        if mode == "w":
            open(p, "w").write(data)
        else:
            open(p, "wb").write(data)
        return p


class ClassifyUrls(Tmp):
    def test_table(self):
        cases = {
            "https://github.com/gchq/cyberchef": "git-url",
            "git@github.com:owner/repo.git": "git-url",
            "file:///home/user/repo.git": "git-url",
            "https://example.com/app-1.4.tar.gz": "tarball",
            "https://example.com/app.tgz": "tarball",
            "https://example.com/win11.iso": "iso",
            "https://example.com/app.ova": "ova",
        }
        for inp, want in cases.items():
            kind, _ = vmf_plan.classify(inp)
            self.assertEqual(kind, want, inp)

    def test_bare_word_is_assumed_image(self):
        kind, detail = vmf_plan.classify("ubuntu")
        self.assertEqual((kind, detail), ("image", "ubuntu (assumed)"))

    def test_ref_shaped_image(self):
        self.assertEqual(vmf_plan.classify("docker.io/library/nginx:1.27")[0], "image")
        self.assertEqual(vmf_plan.classify("user/repo:tag")[0], "image")

    def test_missing_pathlike_is_ambiguous(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            vmf_plan.classify("./nginx")
        self.assertEqual(cm.exception.code, 2)

    def test_missing_media_path(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            vmf_plan.classify(os.path.join(self.tmp, "win11.iso"))
        self.assertEqual(cm.exception.code, 2)

    def test_as_override(self):
        self.assertEqual(vmf_plan.classify("./nginx", "image")[0], "image")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            vmf_plan.classify("./nginx", "warp-drive")


class ClassifyPaths(Tmp):
    def _tar(self, name, members):
        import tarfile
        p = self.path(name)
        with tarfile.open(p, "w") as t:
            for mname, blob in members:
                info = tarfile.TarInfo(mname)
                data = blob.encode()
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        return p

    def test_dir_kinds(self):
        with_compose = self.path("proj")
        os.mkdir(with_compose)
        open(os.path.join(with_compose, "compose.yaml"), "w").write("services: {}\n")
        self.assertEqual(vmf_plan.classify(with_compose)[0], "dir")
        self.assertIn("(compose.yaml)", vmf_plan.classify(with_compose)[1])
        plain = self.path("src")
        os.mkdir(plain)
        self.assertEqual(vmf_plan.classify(plain)[1], "%s (no compose; gap-fill evidence)" % plain)
        bundle = self.path("bnd")
        os.mkdir(bundle)
        open(os.path.join(bundle, "config.json"), "w").write("{}")
        os.mkdir(os.path.join(bundle, "rootfs"))
        self.assertEqual(vmf_plan.classify(bundle)[0], "bundle")

    def test_dockerfile(self):
        p = self._mk("Dockerfile", "FROM node:22-alpine\nRUN echo hi\n")
        kind, detail = vmf_plan.classify(p)
        self.assertEqual(kind, "dockerfile")
        self.assertIn("base: node:22-alpine", detail)

    def test_compose_file_arg(self):
        p = self._mk("docker-compose.yml",
                     "services:\n  a:\n    image: nginx\n  b:\n    image: redis\n")
        kind, detail = vmf_plan.classify(p)
        self.assertEqual(kind, "compose-file")
        self.assertIn("(2 services)", detail)

    def test_iso_magic_and_label(self):
        buf = bytearray(65536)
        buf[32769:32774] = b"CD001"
        label = b"CCCOMA_X64FRE_EN-US_DV9"
        buf[32808:32808 + len(label)] = label
        p = self._mk("win11.iso", bytes(buf), "wb")
        kind, detail = vmf_plan.classify(p)
        self.assertEqual(kind, "iso")
        self.assertIn('iso "CCCOMA_X64FRE_EN-US_DV9"', detail)

    def test_qcow2_magic(self):
        p = self._mk("disk.qcow2", b"QFI\xfb\xfb" + b"\x00" * 4096, "wb")
        self.assertEqual(vmf_plan.classify(p)[0], "disk")

    def test_tar_kinds(self):
        ova = self._tar("app.ova", [("app.ovf", "<x/>"), ("disk.vmdk", "")])
        self.assertEqual(vmf_plan.classify(ova)[0], "ova")
        box = self._tar("x.box", [("metadata.json", "{}"), ("disk.img", "")])
        self.assertEqual(vmf_plan.classify(box)[0], "box")
        dtar = self._tar("app.tar", [("manifest.json", "[]"), ("blobs/x", "")])
        self.assertEqual(vmf_plan.classify(dtar)[0], "image-tar")
        rtar = self._tar("repo.tar.gz", [("src/main.c", "")])
        self.assertEqual(vmf_plan.classify(rtar)[0], "tarball")
        bare = self._tar("stuff.tar", [("a.txt", "")])
        self.assertEqual(vmf_plan.classify(bare)[0], "image-tar")

    def test_unknown_exit_2(self):
        p = self._mk("firmware.bin", b"\xde\xad\xbe\xef" * 64, "wb")
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"), "classify", p],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no magic match", proc.stderr)
        self.assertIn("supported:", proc.stderr)


def OVF_XML():
    return """<?xml version="1.0"?>
<Envelope>
  <VirtualSystem>
    <Item><ResourceType>3</ResourceType><VirtualQuantity>2</VirtualQuantity></Item>
    <Item><ResourceType>4</ResourceType><VirtualQuantity>2048</VirtualQuantity></Item>
  </VirtualSystem>
</Envelope>"""


class Profile(Tmp):
    def _write_formats(self, text):
        p = self.path("formats.yaml")
        open(p, "w").write(text)
        return p

    def test_iso_windows_from_label(self):
        buf = bytearray(65536)
        buf[32769:32774] = b"CD001"
        label = b"CCCOMA_X64FRE_EN-US_DV9"
        buf[32808:32808 + len(label)] = label
        p = self._mk("win11.iso", bytes(buf), "wb")
        err = io.StringIO()
        with redirect_stderr(err):
            vmf_plan.profile_cmd("iso", p)
        self.assertIn("profile: windows-install", err.getvalue())
        self.assertIn("ram 6144", err.getvalue())
        self.assertIn("firmware uefi", err.getvalue())
        self.assertIn("display webvnc", err.getvalue())

    def test_iso_unrecognized_states_default(self):
        buf = bytearray(65536)
        buf[32769:32774] = b"CD001"
        label = b"SOMETHING_ODD_42"
        buf[32808:32808 + len(label)] = label
        p = self._mk("mystery.iso", bytes(buf), "wb")
        err = io.StringIO()
        with redirect_stderr(err):
            vmf_plan.profile_cmd("iso", p)
        self.assertIn("profile: default-linux", err.getvalue())
        self.assertIn("unrecognized", err.getvalue())
        self.assertIn("--ram/--disk", err.getvalue())

    def test_ova_from_ovf(self):
        import tarfile
        p = self.path("app.ova")
        data = OVF_XML().encode()
        with tarfile.open(p, "w") as t:
            info = tarfile.TarInfo("app.ovf")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        err = io.StringIO()
        with redirect_stderr(err):
            vmf_plan.profile_cmd("ova", p)
        self.assertIn("profile: from-ovf", err.getvalue())
        self.assertIn("ram 2048", err.getvalue())
        self.assertIn("cpus 2", err.getvalue())
        self.assertIn("OVF", err.getvalue())

    def test_formats_override(self):
        fmt = self._write_formats("image-default:\n  ram: 2048\n  disk: 8G\n")
        env = dict(os.environ, VMF_FORMATS=fmt)
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "vmf_plan.py"), "profile", "image", "nginx"],
            capture_output=True, text=True, env=env)
        self.assertIn("profile: image-default (ram 2048", proc.stderr)
        self.assertEqual(proc.stdout.strip(), "image-default 2048")

    def test_git_url_no_profile(self):
        rc = vmf_plan.profile_cmd("git-url", "https://example.com/x")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()