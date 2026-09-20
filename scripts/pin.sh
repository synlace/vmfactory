#!/usr/bin/env bash
# vmf pin: digest pins (TOFU). 'vmf pin ls' shows what bytes are trusted;
# verification against the registry lives in oci-run.sh at run time.
set -euo pipefail
json=0
sub="${1:-ls}"
shift || true
case "$sub" in
  ls) ;;
  -h|--help|help|"")
    echo "usage: pin ls [--json]" >&2
    exit 0 ;;
  *) echo "error: unknown pin subcommand '$sub'" >&2; exit 2 ;;
esac
for a in "$@"; do
  case "$a" in
    --json) json=1 ;;
    *) echo "usage: pin ls [--json]" >&2; exit 2 ;;
  esac
done

pins="${VMF_OCI_PINS:-$HOME/.vmf/oci-pins}"
[[ -f "$pins" ]] || { echo "no pins recorded yet"; exit 0; }

if [[ $json -eq 1 ]]; then
  python3 - "$pins" <<'PY'
import json, sys
out = []
for line in open(sys.argv[1]):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    parts = line.split(None, 1)
    if len(parts) == 2:
        out.append({"image": parts[0], "digest": parts[1]})
print(json.dumps(out, indent=2))
PY
else
  while read -r image digest; do
    [[ -n "$image" ]] || continue
    printf '%-52s %s\n' "$image" "$digest"
  done < "$pins"
fi
