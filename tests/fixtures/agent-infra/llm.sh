#!/usr/bin/env bash
# Test stub: the agent keeps emitting infra-diagnostics forever (the
# watchdog must abort it).
echo '{"thought":"debug","cmd":"openssl s_client -connect registry-1.docker.io:443; getent hosts x","done":false,"plan":null}'
