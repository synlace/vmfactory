#!/usr/bin/env bash
# Test stub: the agent model transcript. Call 1 = the grounding draft
# (topics), call 2 = seed an image (the host supplies it), call 3 = a
# shell command turn, call 4+ = done.
d="$(cd "$(dirname "$0")" && pwd)"
n=$(cat "$d/calls" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$d/calls"
case "$n" in
  1) echo '{"lookup":["busybox httpd usage","static file serving"],"why":"current facts"}' ;;
  2) echo '{"thought":"need the app image","cmd":null,"seed_images":["ghost:5"],"done":false,"plan":null}' ;;
  3) echo '{"thought":"write the index","cmd":"mkdir -p /srv && printf agent-made > /srv/index.html && (nohup /vmf/busybox httpd -f -p 8021 -h /srv &)","done":false,"plan":null}' ;;
  *) echo '{"thought":"serving","cmd":null,"done":true,"plan":{"install":["mkdir -p /srv","printf agent-made > /srv/index.html"],"command":["/vmf/busybox","httpd","-f","-p","8021","-h","/srv"],"ports":[8021],"checks":[{"probe":{"port":8021,"path":"/","expect_status":200,"expect_contains":"agent-made"}}],"images":["ghost:5"],"env":{},"needs_docker":true,"memory_mb":1024,"notes":"agent-made spec"}}' ;;
esac
