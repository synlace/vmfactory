#!/usr/bin/env bash
# Test stub: call 1 = draft, call 2 = plan v1, call 3 = revised plan v2.
d="$(cd "$(dirname "$0")" && pwd)"
n=$(cat "$d/calls" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$d/calls"
case "$n" in
  1) echo '{"mode":"direct","lookup":[]}' ;;
  2) echo '{"mode":"direct","base_image":"python:3.12-slim","install":["pip install app"],"command":["app","--port","8080"],"env":{},"needs_docker":false,"memory_mb":4096,"notes":"v1"}' ;;
  *) echo '{"mode":"direct","base_image":"python:3.12-slim","install":["pip install app"],"command":["app","--port","9000"],"env":{},"needs_docker":true,"memory_mb":1024,"notes":"v2"}' ;;
esac
