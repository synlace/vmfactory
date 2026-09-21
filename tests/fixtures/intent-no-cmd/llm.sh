#!/usr/bin/env bash
d="$(cd "$(dirname "$0")" && pwd)"
n=$(cat "$d/calls" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$d/calls"
if [[ "$n" == "1" ]]; then
  echo '{"lookup":[],"why":"none needed"}'
else
  echo '{"install":[],"command":[],"ports":[],"env":{},"needs_docker":false,"memory_mb":1024,"notes":"no cmd"}'
fi
