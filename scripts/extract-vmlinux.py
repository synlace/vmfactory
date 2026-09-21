#!/usr/bin/env python3
# Extract the uncompressed vmlinux ELF from a bzImage. Firecracker's x86
# loader wants the ELF; nixpkgs kernel outputs ship only the compressed
# bzImage. Tries the usual kernel payload compressors (zstd/gzip/xz/
# bzip2/lzma) at every magic offset and accepts the first stream that
# decompresses to a plausible ELF.
import bz2
import gzip
import lzma
import shutil
import subprocess
import sys

data = open(sys.argv[1], "rb").read()
magics = [(b"\x28\xb5\x2f\xfd", "zstd"), (b"\x1f\x8b\x08", "gzip"),
          (b"\xfd7zXZ\x00", "xz"), (b"BZh", "bzip2"),
          (b"\x5d\x00\x00", "lzma")]
for magic, kind in magics:
    off = data.find(magic)
    while off != -1:
        payload = data[off:]
        try:
            if kind == "gzip":
                out = gzip.decompress(payload)
            elif kind == "xz":
                out = lzma.decompress(payload)
            elif kind == "bzip2":
                out = bz2.decompress(payload)
            elif kind == "lzma":
                out = lzma.decompress(payload, format=lzma.FORMAT_ALONE)
            elif kind == "zstd":
                exe = shutil.which("zstd")
                if not exe:
                    raise RuntimeError("zstd binary not on PATH")
                # The payload is followed by other bzImage sections, so
                # zstd may exit nonzero after emitting the frame — keep
                # whatever it decoded and validate the ELF below.
                p = subprocess.run([exe, "-dc"], input=payload,
                                   capture_output=True)
                out = p.stdout
            if out[:4] == b"\x7fELF" and len(out) > 1024 * 1024:
                open(sys.argv[2], "wb").write(out)
                print("extracted %s payload: %d bytes" % (kind, len(out)))
                sys.exit(0)
        except Exception:
            pass
        off = data.find(magic, off + 1)
sys.stderr.write("error: no ELF payload found in %s\n" % sys.argv[1])
sys.exit(1)
