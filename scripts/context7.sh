#!/usr/bin/env bash
# Context7 REST client for vmf's LLM spec'ing: up-to-date library docs
# ground the gap-filler's install plans (package names, install steps,
# prerequisites) instead of relying on model recall.
#
# Usage:
#   context7.sh search "<query>"        -> one JSON object per result:
#                                          {"id","title","description","updated"}
#   context7.sh docs <id> [topic] [tokens]
#                                       -> markdown docs for the library
#                                          (id like /astral-sh/uv or astral-sh/uv)
#
# Config (precedence: process env > ~/.vmf/env):
#   CONTEXT7_API_KEY   optional; higher rate limits (context7.com/settings)
#   VMF_CONTEXT7       off disables (default on)
#   VMF_CONTEXT7_TOKENS cap per fetch (default 3000)
#
# Exits 4 on transport/API failure; the caller degrades gracefully.
set -euo pipefail

ENV_FILE="${VMF_ENV_FILE:-$HOME/.vmf/env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi
[[ "${VMF_CONTEXT7:-on}" != "off" ]] || { echo "context7: disabled" >&2; exit 4; }

BASE="${VMF_CONTEXT7_BASE_URL:-https://context7.com}"
TOKENS="${VMF_CONTEXT7_TOKENS:-3000}"
auth=()
[[ -z "${CONTEXT7_API_KEY:-}" ]] || auth=(-H "Authorization: Bearer $CONTEXT7_API_KEY")

cmd="${1:-}"; shift || true
case "$cmd" in
  search)
    q="${1:?context7.sh search <query>}"
    resp=$(curl -sS --max-time 20 "${BASE}/api/v1/search" \
      --get --data-urlencode "query=$q" \
      "${auth[@]+"${auth[@]}"}" 2>&1) || { echo "context7: search failed: $resp" >&2; exit 4; }
    python3 - "$resp" <<'PY'
import json, sys
try:
    r = json.loads(sys.argv[1])
    for h in (r.get("results") or [])[:4]:
        print(json.dumps({"id": h.get("id"), "title": h.get("title"),
                          "description": (h.get("description") or "")[:140],
                          "updated": h.get("lastUpdateDate", "")}))
except Exception:
    sys.exit(1)
PY
    ;;
  docs)
    lib="${1:?context7.sh docs <id> [topic] [tokens]}"
    topic="${2:-}"
    tok="${3:-$TOKENS}"
    lib="${lib#/}"
    url="${BASE}/api/v1/${lib}"
    args=(--get)
    [[ -z "$topic" ]] || args+=(--data-urlencode "topic=$topic")
    resp=$(curl -sSL --max-time 25 "${args[@]}" \
      --data-urlencode "tokens=$tok" "$url" \
      "${auth[@]+"${auth[@]}"}" 2>&1) || { echo "context7: docs failed: $resp" >&2; exit 4; }
    case "$resp" in
      "Library "*not\ found*|*"Redirecting"*) echo "context7: library '$lib' not found" >&2; exit 4 ;;
    esac
    printf '%s' "$resp"
    ;;
  *)
    echo "usage: context7.sh search <query> | docs <id> [topic] [tokens]" >&2
    exit 2 ;;
esac