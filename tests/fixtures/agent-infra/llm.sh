#!/usr/bin/env bash
# Test stub: call 1 = the grounding draft; calls 2+ = the agent keeps
# emitting infra-diagnostics forever (the watchdog must abort it).
d="$(cd "$(dirname "$0")" && pwd)"
n=$(cat "$d/calls" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$d/calls"
if [[ "$n" == "1" ]]; then
  echo '{"lookup":["tls debugging"],"why":"current facts"}'
else
  echo '{"thought":"debug","cmd":"openssl s_client -connect registry-1.docker.io:443; getent hosts x","done":false,"plan":null}'
fi
