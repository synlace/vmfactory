#!/usr/bin/env bash
# Test stub: call 1 = draft (topics), call 2 = finalized plan JSON.
d="$(cd "$(dirname "$0")" && pwd)"
n=$(cat "$d/calls" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$d/calls"
if [[ "$n" == "1" ]]; then
  echo '{"lookup":["kilo code cli"],"why":"current pkg facts"}'
else
  echo '{"install":["apt-get update","npm i -g kilo"],"command":["kilo","serve"],"ports":[1337],"env":{"A":"B"},"needs_docker":false,"memory_mb":2048,"notes":"ok"}'
fi
