#!/usr/bin/env bash
# Test stub that answers like a wrapped-in-backticks LLM payload.
printf '\x60\x60\x60json\n{"replicas": {"web": 5}}\n\x60\x60\x60\n'
