#!/usr/bin/env bash
# vmf exec: run a command inside a RUNNING microVM via the guest agent.
# Docker-exec semantics: the agent spawns the process inside the live VM's
# kernel, so the caller sees the real process table, not a sibling VM.
#
# Usage: oci-exec.sh <name> [COMMAND...]   (agent port: VMF_AGENT_PORT, 47770)
set -euo pipefail

name="${1:?usage: oci-exec.sh <name> [command...]}"
shift || true
port="${VMF_AGENT_PORT:-47770}"

if command -v krunvm >/dev/null 2>&1 && command -v buildah >/dev/null 2>&1; then
  krun() { buildah unshare -- krunvm "$@"; }
else
  krun() { nix shell nixpkgs#krunvm nixpkgs#buildah -c buildah unshare -- krunvm "$@"; }
fi

krun list | grep -qx "$name" || {
  echo "error: no microVM '$name' running; start one with: just run --keep <image>" >&2
  exit 1
}

python3 - "$port" ${@+"$@"} <<'PYEOF'
import json, socket, sys

port = int(sys.argv[1])
argv = sys.argv[2:] or ["sh", "-l"]

s = socket.create_connection(("127.0.0.1", port), timeout=30)
s.sendall((json.dumps({"argv": argv}) + "\n").encode())

chunks = []
try:
    while True:
        d = s.recv(65536)
        if not d:
            break
        chunks.append(d)
finally:
    s.close()

buf = b"".join(chunks)
marker = b"VMF-EXIT "
idx = buf.rfind(marker)
code = 1
if idx >= 0:
    body, tail = buf[:idx], buf[idx:].decode(errors="replace").strip()
    parts = tail.split()
    if len(parts) >= 2:
        code = int(parts[1])
else:
    body = buf
sys.stdout.buffer.write(body)
sys.stdout.flush()
sys.exit(code)
PYEOF