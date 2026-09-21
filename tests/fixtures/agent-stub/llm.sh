#!/usr/bin/env bash
# Test stub: the agent model transcript. Call 1 = a shell command turn,
# call 2 = done with a plan; calls 3+ = done again (instruction rounds).
d="$(cd "$(dirname "$0")" && pwd)"
n=$(cat "$d/calls" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$d/calls"
case "$n" in
  1) echo '{"thought":"write the index","cmd":"mkdir -p /srv && printf agent-made > /srv/index.html && (nohup /vmf/busybox httpd -f -p 8021 -h /srv &)","done":false,"plan":null}' ;;
  *) echo '{"thought":"serving","cmd":null,"done":true,"plan":{"install":["mkdir -p /srv","printf agent-made > /srv/index.html"],"command":["/vmf/busybox","httpd","-f","-p","8021","-h","/srv"],"ports":[8021],"checks":[{"probe":{"port":8021,"path":"/","expect_status":200,"expect_contains":"agent-made"}}],"env":{},"needs_docker":false,"memory_mb":1024,"notes":"agent-made spec"}}' ;;
esac
