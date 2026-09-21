#!/usr/bin/env bash
# Test stub: the verify revision model reply (one call, fixed JSON).
echo '{"install":["apt-get update","apt-get install -y nginx"],"command":["nginx","-g","daemon off;"],"ports":[1337,99999,1338],"checks":[{"probe":{"port":1337,"path":"/","expect_status":200,"expect_contains":"Welcome"}},{"probe":{"port":"junk"}},{"log":{"match":"x"}}],"env":{},"needs_docker":false,"memory_mb":2048,"notes":"revised"}'
