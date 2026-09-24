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
if [[ $# -lt 1 ]]; then
  echo "llm.sh: no prompt given" >&2
  exit 2
fi
# "-" reads the prompt from stdin: grounded prompts exceed the per-arg
# exec limit (E2BIG).
if [[ "$1" == "-" ]]; then
  prompt=$(cat)
else
  prompt="$1"
fi

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

# Role-aware curl ceiling: the gapfill/agent prompts carry repo
# evidence and doc excerpts; a 60s default times them out mid-flight
# and the caller reports "needs a model" instead of a timeout.
case "${role:-}" in
  gapfill) timeout_default=420 ;;
  agent)   timeout_default=220 ;;
  *)             timeout_default=60 ;;
esac

# The caller passes the user-side prompt; the system prompt is fixed and
# terse: strict JSON, nothing else. Reasoning effort follows the task
# class: PLANNING calls (enumerate, gap-fill, revise — the gapfill role)
# get high effort: a wrong plan costs a full boot cycle. The in-guest
# repair agent gets medium (iterative turns over a live VM). Intent
# stays lean (bounded-vocab pointer work). Env overrides per role;
# VMF_REASONING=off disables everywhere.
# The prompt rides STDIN (the -c script keeps argv for the model only):
# grounded prompts (repo evidence + context7 docs) exceed the per-arg
# exec limit (E2BIG: "Argument list too long").
body=$(printf '%s' "$prompt" | VMF_ROLE="$role" python3 -c '
import json, os, sys
model = sys.argv[1]
prompt = sys.stdin.read()
role = os.environ.get("VMF_ROLE", "")
effort = {"gapfill": "high", "agent": "medium"}.get(role)
if os.environ.get("VMF_REASONING", "").strip().lower() == "off":
    effort = None
override = {"intent": "VMF_INTENT_REASONING",
            "gapfill": "VMF_GAPFILL_REASONING",
            "agent": "VMF_AGENT_REASONING"}.get(role)
if override:
    v = os.environ.get(override, "").strip().lower()
    effort = None if v in ("", "off", "none") else v
msg = {
    "model": model,
    "messages": [
        {"role": "system", "content":
         "You are a config resolver for the vmf CLI. Reply with ONE JSON "
         "object and nothing else. Never invent values outside the "
         "allowed sets the user message gives you."},
        {"role": "user", "content": prompt},
    ],
    "temperature": 0,
}
if effort:
    msg["reasoning"] = {"effort": effort}
print(json.dumps(msg))
' "$model")

# The body rides stdin as well (-d @-): the same E2BIG ceiling.
resp=$(printf '%s' "$body" | curl -sS --max-time "${VMF_LLM_TIMEOUT:-$timeout_default}" "$base/chat/completions" \
  -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
  -d @- 2>&1) || { echo "llm.sh: request failed: $resp" >&2; exit 3; }

# The response rides stdin too: a grounded repair answer can exceed
# the per-arg exec limit just like the prompt.
printf '%s' "$resp" | python3 -c '
import json, sys
try:
    r = json.loads(sys.stdin.read())
    print(r["choices"][0]["message"]["content"])
except Exception:
    sys.exit(1)
' || { echo "llm.sh: unexpected API response" >&2; exit 3; }