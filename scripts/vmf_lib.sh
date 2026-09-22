# vmf_lib.sh — shared bash helpers for the vmf scripts.
#
# tool provisioning: the idiom "command -v X || nix shell nixpkgs#X -c X"
# used to be copied per call site; these helpers own it now.
#   vmf_tool <bin> [pkgs...]  → TOOL=(prefix… -c <bin>) or TOOL=(<bin>)
#   vmf_tools <pkgs...>       → TOOL=() when all present, else nix prefix + -c
#   vmf_run <pkgs...> -- <cmd…>  run cmd directly, nix shell pkgs if missing
# Forward-line parser (the "proto host guest" contract):
#   vmf_fwd_parse <line>  → VMF_FWD_PROTO VMF_FWD_BIND VMF_FWD_HOST VMF_FWD_GUEST
#                           (2-field lines are legacy tcp; rc 1 on junk)
# Source with: . "$(dirname "${BASH_SOURCE[0]}")/vmf_lib.sh"

vmf_tool() {
  local bin="$1"; shift
  if command -v "$bin" >/dev/null 2>&1; then
    TOOL=("$bin")
    return 0
  fi
  local pkgs=("$bin")
  [[ $# -gt 0 ]] && pkgs=("$@")
  TOOL=(nix shell "${pkgs[@]/#/nixpkgs#}" -c "$bin")
}

vmf_tools() {
  local p missing=0
  for p in "$@"; do
    command -v "$p" >/dev/null 2>&1 || missing=1
  done
  if [[ "$missing" == "0" ]]; then
    TOOL=()
    return 0
  fi
  TOOL=(nix shell "${@/#/nixpkgs#}" -c)
}

vmf_run() {
  local pkgs=()
  while [[ $# -gt 0 && "$1" != "--" ]]; do
    pkgs+=("$1"); shift
  done
  [[ $# -gt 0 ]] && shift
  local p missing=0
  for p in "${pkgs[@]}"; do
    command -v "$p" >/dev/null 2>&1 || missing=1
  done
  if [[ "$missing" == "0" ]]; then
    "$@"
  else
    nix shell "${pkgs[@]/#/nixpkgs#}" -c "$@"
  fi
}

vmf_fwd_parse() {
  # "host guest" (legacy tcp), "proto host guest", or
  # "proto bind host guest" → VMF_FWD_* (bind "" means all interfaces).
  VMF_FWD_PROTO=tcp
  VMF_FWD_BIND=""
  VMF_FWD_HOST=""
  VMF_FWD_GUEST=""
  local -a f
  read -r -a f <<<"$1"
  case ${#f[@]} in
    4) VMF_FWD_PROTO="${f[0]}"; VMF_FWD_BIND="${f[1]}"
       VMF_FWD_HOST="${f[2]}"; VMF_FWD_GUEST="${f[3]}" ;;
    3) VMF_FWD_PROTO="${f[0]}"; VMF_FWD_HOST="${f[1]}"; VMF_FWD_GUEST="${f[2]}" ;;
    2) VMF_FWD_HOST="${f[0]}"; VMF_FWD_GUEST="${f[1]}" ;;
    *) return 1 ;;
  esac
  if [[ "$VMF_FWD_BIND" == "0.0.0.0" ]]; then
    VMF_FWD_BIND=""
  fi
}

# True when a live VM holds the name (resolver + liveness by PID).
vmf_conf_running() {
  local pid
  vmf_instance_dir "$1" 2>/dev/null || return 1
  [[ -f "$VMF_INST_CONF" ]] || return 1
  pid=$(grep -oE '^PID=[0-9]+' "$VMF_INST_CONF" 2>/dev/null | cut -d= -f2)
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# Instance numbering: when a running VM holds the name, a new run takes
# the next free numbered name instead of replacing. --replace redeploys
# over the bare name; race children manage their own names.
vmf_number_instance() { # name [replace_flag] -> final name on stdout
  local base="$1" replace="${2:-0}" n cand
  [[ -n "$base" ]] || { printf '%s' "$base"; return 0; }
  # Handoffs keep their name: compose children, the verify re-exec
  # (VMF_VERIFY_TURN), and race children all boot under an owner's
  # name and must not number themselves away from it.
  [[ -n "${VMF_NAME:-}" || -n "${VMF_VERIFY_TURN:-}" || \
    "${VMF_RACE_CHILD:-0}" == "1" ]] && { printf '%s' "$base"; return 0; }
  [[ "$replace" == "1" ]] && { printf '%s' "$base"; return 0; }
  vmf_conf_running "$base" || { printf '%s' "$base"; return 0; }
  for n in 2 3 4 5 6 7 8 9; do
    cand="$base-$n"
    if ! vmf_conf_running "$cand"; then
      echo "lab: $base is running — booting instance $cand" >&2
      printf '%s' "$cand"
      return 0
    fi
  done
  printf '%s' "$base"
}

# Resolve a VM reference to its instance directory.
# Accepts a name (the current symlink holder), a full 12-hex id, or a
# unique id prefix. Falls back to the legacy flat layout (<name>.conf).
# Sets VMF_INST_DIR (empty for legacy) and VMF_INST_CONF (the conf path).
# Returns 1 when nothing matches; VMF_INST_ERR carries the user message.
vmf_instance_dir() {
  local ref="$1" runs="${2:-${VMF_RUNS:-$HOME/.vmf/runs}}" m d
  VMF_INST_DIR=""
  VMF_INST_CONF=""
  VMF_INST_ERR=""
  if [[ -L "$runs/$ref" ]]; then
    m=$(readlink "$runs/$ref")
    if [[ -f "$runs/$m/conf" ]]; then
      VMF_INST_DIR="$runs/$m"
      VMF_INST_CONF="$VMF_INST_DIR/conf"
      return 0
    fi
  fi
  if [[ "$ref" =~ ^[0-9a-f]{12}$ ]] && [[ -f "$runs/$ref/conf" ]]; then
    VMF_INST_DIR="$runs/$ref"
    VMF_INST_CONF="$VMF_INST_DIR/conf"
    return 0
  fi
  if [[ "$ref" =~ ^[0-9a-f]{2,11}$ ]]; then
    local -a hits=()
    for d in "$runs/$ref"*; do
      [[ -f "$d/conf" ]] && hits+=("$d")
    done
    if [[ ${#hits[@]} -eq 1 ]]; then
      VMF_INST_DIR="${hits[0]}"
      VMF_INST_CONF="$VMF_INST_DIR/conf"
      return 0
    elif [[ ${#hits[@]} -gt 1 ]]; then
      VMF_INST_ERR="'$ref' matches ${#hits[@]} VMs: $(printf '%s ' "${hits[@]##*/}")— be more specific"
      return 1
    fi
  fi
  if [[ -f "$runs/$ref.conf" ]]; then
    VMF_INST_CONF="$runs/$ref.conf"
    return 0
  fi
  VMF_INST_ERR="no VM or box '$ref'"
  return 1
}