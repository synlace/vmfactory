#!/usr/bin/env bash
# vmf_clean.sh — inventory and remove run artifacts.
#
# usage: vmf_clean.sh [--yes] [--vms] [--drives] [--plans] [--race] [--pins]
#                     [--keep-age AGE]
#
# Groups:
#   vms     terminated VMs: conf + console log + rundirs + ssh host keys
#   drives  compose data drives (content-keyed ext4; a drive mounted by
#           a running VM is never a candidate)
#   plans   generated plan caches (~/.vmf/generated)
#   race    race artifacts: logs, verdict markers, enum/plan scratch
#   pins    registry pin store (listed always, removed only with --pins)
#
# Default: dry run + the gate (a = all, n = abort, or a group list).
# --yes executes the selected groups (all except pins without group
# flags). --keep-age AGE keeps items younger than AGE (30m, 12h, 2d).
# Running VMs are never touched.
set -u
RUNS_DIR="${VMF_RUNS:-$HOME/.vmf/runs}"
COMPOSE_CACHE="${VMF_COMPOSE_CACHE:-$HOME/.vmf/compose}"
GEN_DIR="${VMF_GENERATED:-$HOME/.vmf/generated}"
PINS="${VMF_OCI_PINS:-$HOME/.vmf/oci-pins}"
SSH_DIR="${VMF_SSH_DIR:-$HOME/.vmf/ssh}"

GROUP_NAMES="vms drives plans race"
yes_flag=0
want=""
want_pins=0
keep_age=0

usage() {
  cat <<'EOF'
usage: just clean [--yes] [--vms] [--drives] [--plans] [--race] [--pins]
                  [--keep-age AGE]

Groups:
  vms     terminated VMs: conf, console log, rundirs
  drives  compose data drives (a drive held by a running VM is kept)
  plans   generated plan caches (~/.vmf/generated)
  race    race artifacts: logs, verdict markers, enum/plan scratch
  pins    registry pin store (listed always; removed only with --pins)

Default: dry run + the gate (a = all, n = abort, or a group list).
--yes executes the selected groups (all except pins without flags).
--keep-age AGE keeps items younger than AGE (30m, 12h, 2d).
Running VMs are never touched.
EOF
  exit 2
}

age_secs() { # 30m | 12h | 2d
  local n="${1%[mhd]}" unit="${1##*[0-9]}"
  case "$unit" in
    m) echo $((n * 60)) ;;
    h) echo $((n * 3600)) ;;
    d) echo $((n * 86400)) ;;
    *) echo 0 ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) yes_flag=1; shift ;;
    --vms) want="$want vms"; shift ;;
    --drives) want="$want drives"; shift ;;
    --plans) want="$want plans"; shift ;;
    --race) want="$want race"; shift ;;
    --pins) want_pins=1; shift ;;
    --keep-age) [[ $# -ge 2 ]] || usage; keep_age=$(age_secs "$2"); shift 2 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done
want="${want# }"

fmt_size() {
  awk -v b="$1" 'BEGIN {
    if (b >= 1073741824) printf "%.1f GB", b / 1073741824
    else if (b >= 1048576) printf "%.1f MB", b / 1048576
    else if (b >= 1024) printf "%.1f KB", b / 1024
    else printf "%d B", b
  }'
}

age_of() { # seconds since mtime
  echo $(( $(date +%s) - $(stat -c %Y "$1") ))
}

too_young() { # path
  (( keep_age > 0 )) && (( $(age_of "$1") < keep_age ))
}

# A VM conf is running when its qemu/fc pid answers kill -0.
vm_running() { # name
  local conf="$RUNS_DIR/$1.conf"
  [[ -f "$conf" ]] || return 1
  local pid engine rundir f
  # shellcheck source=/dev/null
  . "$conf" 2>/dev/null
  engine=${ENGINE:-}
  rundir=${RUNDIR:-}
  pid=${PID:-}
  if [[ -z "$pid" && -n "$rundir" ]]; then
    for f in qemu.pid fc.pid; do
      [[ -f "$rundir/$f" ]] && { pid=$(cat "$rundir/$f"); break; }
    done
  fi
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && { VM_PID=$pid; return 0; }
  return 1
}

# rundirs: <name>.<pid> directories under RUNS_DIR; orphans have no conf.
# Returns: name<TAB>kind<TAB>path
scan_vms() {
  local conf name engine path p running
  for conf in "$RUNS_DIR"/*.conf; do
    [[ -f "$conf" ]] || continue
    name=$(basename "$conf" .conf)
    running=stopped
    if vm_running "$name"; then running=running; fi
    printf '%s\t%s\t%s\n' "$name" "$running" "$conf"
  done
  for path in "$RUNS_DIR"/*.*; do
    [[ -d "$path" ]] || continue
    name=$(basename "$path"); p="${name##*.}"
    [[ "$p" =~ ^[0-9]+$ ]] || continue
    name="${name%.*}"
    [[ -f "$RUNS_DIR/$name.conf" ]] && continue
    printf '%s\torphan\t%s\n' "$name" "$path"
  done
}

# Aggregate per name: one line per VM (a stopped VM's rundirs and an
# orphan's many rundirs collapse to a single row with a count).
scan_vms_agg() {
  scan_vms | awk -F'\t' -v OFS='\t' '
    $2 == "running" { print; next }
    { paths[$1] = paths[$1] "\t" $3; cnt[$1]++; kind[$1] = $2; size[$1]++ }
    END {
      for (n in cnt)
        if (cnt[n] > 1) print n, kind[n], n " (" cnt[n] " rundirs)"
        else print n, kind[n], n
    }'
}

drive_in_use() { # drive basename -> owner name if a running VM holds it
  local conf name pid drive
  for conf in "$RUNS_DIR"/*.conf; do
    [[ -f "$conf" ]] || continue
    name=$(basename "$conf" .conf)
    vm_running "$name" || continue
    pid=${VM_PID:-}
    [[ -n "$pid" ]] || continue
    drive=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null \
      | grep -oE '[A-Za-z0-9_.-]+\.ext4' | head -1)
    [[ "$drive" == "$1" ]] && { printf '%s' "$name"; return 0; }
  done
  return 1
}

drive_map() { # drive basename -> owner name
  local name="$1"
  # Old name-keyed drives ("cyberchef-<hash>.ext4") carry their owner in
  # the filename; content-keyed drives stay unattributed unless a conf
  # references them.
  if [[ "$name" =~ ^([A-Za-z0-9_.-]+)-[0-9a-f]{12}\.ext4$ ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
  else
    local conf best=""
    for conf in "$RUNS_DIR"/*.conf; do
      [[ -f "$conf" ]] || continue
      grep -q -- "$name" "$conf" 2>/dev/null || continue
      best=$(basename "$conf" .conf)
    done
    printf '%s' "$best"
  fi
}

# --- inventory ------------------------------------------------------------
declare -a ACT_VMS=() ACT_DRIVES=() ACT_PLANS=() ACT_RACE=()
sz_vms=0 sz_drives=0 sz_plans=0 sz_race=0
running_list=""

inv_vms() {
  local name kind label d sz
  while IFS=$'\t' read -r name kind label; do
    if [[ "$kind" == "running" ]]; then
      running_list="$running_list $name"
      continue
    fi
    sz=0
    # conf + every artifact that starts "<name>." (rundirs, console log)
    for d in "$RUNS_DIR/$name.conf" "$RUNS_DIR/$name".*; do
      [[ -e "$d" ]] && sz=$(( sz + $(du -sb "$d" 2>/dev/null | cut -f1) ))
    done
    if too_young "$RUNS_DIR/$name.conf" 2>/dev/null; then
      printf '  %-20s %-9s %10s (kept: younger than --keep-age)\n' \
        "$name" "$kind" "$(fmt_size "$sz")"
      continue
    fi
    printf '  %-20s %-9s %10s\n' "$name" "$kind" "$(fmt_size "$sz")"
    ACT_VMS+=("$name")
    sz_vms=$(( sz_vms + sz ))
  done < <(scan_vms_agg | sort)
  [[ -n "$running_list" ]] && \
    printf '  kept (running):%s\n' "$running_list"
  return 0
}

inv_drives() {
  local d name sz owner keep
  for d in "$COMPOSE_CACHE"/*.ext4; do
    [[ -f "$d" ]] || continue
    if too_young "$d"; then keep=" kept"; else keep=""; fi
    name=$(basename "$d")
    sz=$(stat -c %b "$d" | awk '{print $1 * 512}')
    holder=$(drive_in_use "$name")
    if [[ -n "$holder" ]]; then
      printf '  %-32s %10s  kept (%s running)\n' "$name" "$(fmt_size "$sz")" "$holder"
      continue
    fi
    owner=$(drive_map "$name")
    printf '  %-32s %10s  %s%s\n' "$name" "$(fmt_size "$sz")" \
      "$([[ -n "$owner" ]] && printf '(%s)' "$owner" || printf '(unattributed)')" "$keep"
    if [[ -z "$keep" ]]; then
      ACT_DRIVES+=("$d")
      sz_drives=$(( sz_drives + sz ))
    fi
  done
}

inv_plans() {
  local d sz
  for d in "$GEN_DIR"/*/; do
    [[ -d "$d" ]] || continue
    if too_young "$d"; then continue; fi
    sz=$(du -sb "$d" 2>/dev/null | cut -f1)
    printf '  %-16s %10s  age %sd\n' "$(basename "$d")" "$(fmt_size "$sz")" \
      "$(( $(age_of "$d") / 86400 ))"
    ACT_PLANS+=("$d")
    sz_plans=$(( sz_plans + sz ))
  done
}

inv_race() {
  local p sz
  for p in "$RUNS_DIR"/race-logs "$RUNS_DIR"/*.verdict \
           "$RUNS_DIR"/.race-enum.json "$RUNS_DIR"/.race-plan.json; do
    [[ -e "$p" ]] || continue
    if too_young "$p"; then continue; fi
    sz=$(du -sb "$p" 2>/dev/null | cut -f1)
    printf '  %-16s %10s\n' "$(basename "$p")" "$(fmt_size "$sz")"
    ACT_RACE+=("$p")
    sz_race=$(( sz_race + sz ))
  done
}

inv_pins() {
  [[ -f "$PINS" ]] || return 0
  printf '  %-16s %10s  %s pin(s)\n' "$(basename "$PINS")" \
    "$(fmt_size "$(stat -c %s "$PINS")")" "$(wc -l < "$PINS")"
}

echo "clean: scanning $HOME/.vmf"
inv_vms
inv_drives
inv_plans
inv_race
inv_pins
total=$(( sz_vms + sz_drives + sz_plans + sz_race ))
echo "clean: dry run — vms $(fmt_size "$sz_vms"), drives $(fmt_size "$sz_drives"), plans $(fmt_size "$sz_plans"), race $(fmt_size "$sz_race")"

# --- selection ------------------------------------------------------------
chosen=""
if [[ "$yes_flag" == "1" ]]; then
  chosen="$want"
  [[ -n "$chosen" ]] || chosen="$GROUP_NAMES"
else
  if [[ -t 0 ]]; then
    printf 'gate: what to remove? [a = all above, n = abort, or list: %s] ' \
      "$(printf '%s,' "$GROUP_NAMES" | sed 's/,$//')"
    read -r ans
    case "$ans" in
      a|A) chosen="$GROUP_NAMES" ;;
      ""|n|N) echo "clean: abort; nothing deleted"; exit 0 ;;
      *) chosen="$ans" ;;
    esac
  else
    echo "clean: no tty and no --yes; nothing deleted"
    exit 0
  fi
fi
chosen=$(printf '%s' "$chosen" | tr ',' ' ' | tr -s ' ')
for g in $chosen; do
  case " $GROUP_NAMES " in *" $g "*) ;; *) echo "clean: unknown group '$g'; abort"; exit 2 ;; esac
done
if [[ "$want_pins" == "1" ]]; then
  chosen="$chosen pins"
fi

# --- execute ----------------------------------------------------------------
reclaimed=0
for g in vms drives plans race; do
  case " $chosen " in *" $g "*) ;; *) continue ;; esac
  n=0
  case "$g" in
    vms)
      for name in "${ACT_VMS[@]:-}"; do
        [[ -n "$name" ]] || continue
        rm -f "$RUNS_DIR/$name.conf" "$RUNS_DIR/$name.log"
        rm -rf "$RUNS_DIR/$name" "$RUNS_DIR/$name".* 2>/dev/null
        n=$((n+1))
      done
      ;;
    drives)
      for d in "${ACT_DRIVES[@]:-}"; do
        [[ -n "$d" ]] || continue
        rm -f "$d" "$d.tmp"
        n=$((n+1))
      done
      ;;
    plans)
      for d in "${ACT_PLANS[@]:-}"; do
        [[ -n "$d" ]] || continue
        rm -rf "$d"
        n=$((n+1))
      done
      ;;
    race)
      for p in "${ACT_RACE[@]:-}"; do
        [[ -n "$p" ]] || continue
        rm -rf "$p"
        n=$((n+1))
      done
      ;;
  esac
  eval "sz=\$sz_$g"
  printf 'clean: removing %d %s .. ok (%s)\n' "$n" "$g" "$(fmt_size "$sz")"
  reclaimed=$(( reclaimed + sz ))
done
if [[ "$want_pins" == "1" && -f "$PINS" ]]; then
  rm -f "$PINS"
  echo "clean: removing the pin store .. ok"
fi
[[ "$chosen" == "" ]] && { echo "clean: nothing selected"; exit 0; }
echo "clean: reclaimed $(fmt_size "$reclaimed")"
