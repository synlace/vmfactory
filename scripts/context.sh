#!/usr/bin/env bash
# vmf context management. Contexts are simple conf files:
#   ~/.vmf/contexts/<name>.conf   TYPE=local|provider ENGINE=... URL=...
#   ~/.vmf/context                name of the active context
# The default context is implicit (local, engine unset -> qemu, global
# state dir) and does not need a file.
set -euo pipefail

VMF_HOME="${VMF_HOME:-$HOME/.vmf}"
CTX_DIR="$VMF_HOME/contexts"
CTX_FILE="$VMF_HOME/context"
mkdir -p "$CTX_DIR"

usage() {
  cat >&2 <<'EOF'
Usage: vmf context <subcommand>

  ls [--json]                     list contexts (active marked *)
  current                         print the active context name
  use NAME                        set the active context
  add NAME [--type T] [--engine E] [--url U] [--use]
      types: local (default) | provider (config-only for now)
      engines (local): qemu (default) | krunvm | firecracker (planned)
  remove NAME                     delete a context (not the active one)
EOF
  exit 2
}

current_name() {
  if [[ -n "${VMF_CONTEXT:-}" ]]; then echo "$VMF_CONTEXT"
  elif [[ -f "$CTX_FILE" ]]; then cat "$CTX_FILE"
  else echo default
  fi
}

json_out=0
sub="${1:-}"; [[ $# -gt 0 ]] && shift

case "$sub" in
  ls)
    [[ "${1:-}" == "--json" ]] && json_out=1
    cur="$(current_name)"
    names=(default)
    for f in "$CTX_DIR"/*.conf; do
      [[ -f "$f" ]] || continue
      names+=("$(basename "$f" .conf)")
    done
    if [[ $json_out -eq 1 ]]; then
      python3 - "$cur" "${names[@]}" <<'PY'
import json, os, sys
cur, names = sys.argv[1], sys.argv[2:]
home = os.path.expanduser("~/.vmf")
out = []
for n in names:
    t, e, u = "local", "", ""
    if n != "default":
        p = os.path.join(home, "contexts", n + ".conf")
        if os.path.exists(p):
            for line in open(p):
                k, _, v = line.strip().partition("=")
                if k == "TYPE": t = v
                elif k == "ENGINE": e = v
                elif k == "URL": u = v
    out.append({"name": n, "active": n == cur, "type": t,
                "engine": e or ("qemu" if t == "local" else ""),
                "url": u})
print(json.dumps(out, indent=2))
PY
    else
      for n in "${names[@]}"; do
        mark=" "
        [[ "$n" == "$cur" ]] && mark="*"
        t="local"; e=""; u=""
        if [[ "$n" != "default" && -f "$CTX_DIR/$n.conf" ]]; then
          # shellcheck source=/dev/null
          source "$CTX_DIR/$n.conf"
          t="${TYPE:-local}"; e="${ENGINE:-}"; u="${URL:-}"
        fi
        printf '%s %-16s %-9s %-12s %s\n' "$mark" "$n" "$t" "${e:--}" "$u"
      done
    fi
    ;;
  current) current_name ;;
  use)
    [[ $# -ge 1 ]] || usage
    name="$1"
    if [[ "$name" != "default" && ! -f "$CTX_DIR/$name.conf" ]]; then
      echo "error: no context '$name'; create one: vmf context add $name" >&2
      exit 3
    fi
    printf '%s\n' "$name" > "$CTX_FILE"
    echo "context: $name"
    ;;
  add)
    name=""; ctype="local"; cengine=""; curl=""; use_now=0
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --type) ctype="$2"; shift 2 ;;
        --engine) cengine="$2"; shift 2 ;;
        --url) curl="$2"; shift 2 ;;
        --use) use_now=1; shift ;;
        -*) echo "error: unknown flag $1" >&2; usage ;;
        *) [[ -z "$name" ]] && name="$1" || { echo "error: extra arg $1" >&2; usage; }; shift ;;
      esac
    done
    [[ -n "$name" ]] || usage
    [[ "$name" != "default" ]] || { echo "error: 'default' is implicit" >&2; exit 3; }
    case "$ctype" in local|provider) ;; *) echo "error: --type must be local|provider" >&2; exit 2 ;; esac
    case "$cengine" in ""|qemu|krunvm|firecracker) ;; *) echo "error: --engine must be qemu|krunvm|firecracker" >&2; exit 2 ;; esac
    [[ "$ctype" == "provider" && -z "$curl" ]] && { echo "error: provider contexts need --url" >&2; exit 2; }
    [[ -f "$CTX_DIR/$name.conf" ]] && { echo "error: context '$name' exists" >&2; exit 3; }
    {
      printf 'TYPE=%s\n' "$ctype"
      [[ -n "$cengine" ]] && printf 'ENGINE=%s\n' "$cengine"
      [[ -n "$curl" ]] && printf 'URL=%s\n' "$curl"
    } > "$CTX_DIR/$name.conf"
    echo "context added: $name ($ctype${cengine:+, $cengine})"
    if [[ $use_now -eq 1 ]]; then
      printf '%s\n' "$name" > "$CTX_FILE"
      echo "context: $name"
    fi
    ;;
  remove)
    [[ $# -ge 1 ]] || usage
    name="$1"
    [[ "$name" != "default" ]] || { echo "error: 'default' cannot be removed" >&2; exit 3; }
    [[ -f "$CTX_DIR/$name.conf" ]] || { echo "error: no context '$name'" >&2; exit 3; }
    [[ "$(current_name)" != "$name" ]] || { echo "error: context '$name' is active; switch first: vmf context use default" >&2; exit 3; }
    rm -f "$CTX_DIR/$name.conf"
    echo "context removed: $name"
    ;;
  ""|-h|--help|help) usage ;;
  *) echo "error: unknown subcommand '$sub'" >&2; usage ;;
esac
