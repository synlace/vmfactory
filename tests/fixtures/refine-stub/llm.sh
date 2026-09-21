#!/usr/bin/env bash
# Test stub for the overlay gate loop: call 1 = initial overlay, call 2 = revised.
d="$(cd "$(dirname "$0")" && pwd)"
n=$(cat "$d/calls" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$d/calls"
if [[ "$n" == "1" ]]; then
  echo '{"replicas":{"web":3},"env":{"web":{"GREETING":"hi"}}}'
else
  echo '{"replicas":{"web":5},"env":{},"command":{}}'
fi
