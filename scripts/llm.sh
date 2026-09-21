#!/usr/bin/env bash
# Minimal OpenAI-compatible chat client for vmf's LLM assists
# (--intent pointer resolution, the compose gap-filler).
#
# Configuration (precedence: process env > ~/.vmf/env):
#   VMF_LLM_API_KEY / OPENROUTER_API_KEY   bearer token
#   VMF_LLM_BASE_URL   (default https://openrouter.ai/api/v1)
#   VMF_LLM_MODEL      default model when the caller passes no role
#   VMF_INTENT_MODEL   small/fast model for pointer resolution
#   VMF_GAPFILL_MODEL  stronger model for compose generation
#
# Usage: llm.sh [--role intent|gapfill] [--model ID] PROMPT_JSON...
# Prints the assistant message content to stdout. Exits 3 with a clear
# message when unconfigured; 4 on transport/HTTP failure.
set -euo pipefail

role=""; model=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) [[ $# -ge 2 ]] || { echo "llm.sh: --role needs a value" >&2; exit 2; }
            role="$2"; shift 2 ;;
    --model) [[ $# -ge 2 ]] || { echo "llm.sh: --model needs a value" >&2; exit 2; }
             model="$2"; shift 2 ;;
    *) break ;;
  esac
done
[[ $# -ge 1 ]] || { echo "llm.sh: no prompt given" >&2; exit 2; }
prompt="$1"

ENV_FILE="${VMF_ENV_FILE:-$HOME/.vmf/env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi

key="${VMF_LLM_API_KEY:-${OPENROUTER_API_KEY:-}}"
if [[ -z "$key" ]]; then
  echo "llm.sh: no API key; set VMF_LLM_API_KEY in ~/.vmf/env (https://openrouter.ai/keys)" >&2
  exit 3
fi
base="${VMF_LLM_BASE_URL:-https://openrouter.ai/api/v1}"
model="${model:-${VMF_INTENT_MODEL:-${VMF_GAPFILL_MODEL:-${VMF_LLM_MODEL:-}}}}"
case "${role:-}" in
  intent)  model="${model:-${VMF_INTENT_MODEL:-${VMF_LLM_MODEL:-z-ai/glm-5.3-flash}}}" ;;
  gapfill) model="${model:-${VMF_GAPFILL_MODEL:-${VMF_LLM_MODEL:-z-ai/glm-5.3-flash}}}" ;;
  agent)   model="${model:-${VMF_AGENT_MODEL:-${VMF_GAPFILL_MODEL:-${VMF_LLM_MODEL:-z-ai/glm-5.3-flash}}}}" ;;
  *)       model="${model:-${VMF_LLM_MODEL:-z-ai/glm-5.3-flash}}" ;;
esac

# The caller passes the user-side prompt; the system prompt is fixed and
# terse: strict JSON, nothing else.
body=$(python3 - "$model" "$prompt" <<'PY'
import json, sys
print(json.dumps({
    "model": sys.argv[1],
    "messages": [
        {"role": "system", "content":
         "You are a config resolver for the vmf CLI. Reply with ONE JSON "
         "object and nothing else. Never invent values outside the "
         "allowed sets the user message gives you."},
        {"role": "user", "content": sys.argv[2]},
    ],
    "temperature": 0,
}))
PY
)

resp=$(curl -sS --max-time "${VMF_LLM_TIMEOUT:-60}" "$base/chat/completions" \
  -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
  -d "$body" 2>&1) || { echo "llm.sh: request failed: $resp" >&2; exit 3; }

python3 - "$resp" <<'PY' || { echo "llm.sh: unexpected API response" >&2; exit 3; }
import json, sys
try:
    r = json.loads(sys.argv[1])
    print(r["choices"][0]["message"]["content"])
except Exception:
    sys.exit(1)
PY