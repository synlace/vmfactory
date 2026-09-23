#!/usr/bin/env bash
# The host bridge stack for ip mode: bridge vmfbr0 (192.168.42.1/24),
# a user-owned tap pool, and dnsmasq for DHCP + DNS. `init` needs sudo
# once (just lab-init); boots never run privileged — they claim a free
# tap from the pool and the guest DHCPs on the bridge subnet.
#   vmf-net.sh init      one-time setup (bridge, taps, dnsmasq)
#   vmf-net.sh status    human summary
#   vmf-net.sh ready     rc 0 when ip mode is usable (silent)
#   vmf-net.sh claim MAC print a free tap name (or fail)
set -euo pipefail
# shellcheck source=vmf_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmf_lib.sh"

NET_DIR="${VMF_NET_DIR:-$HOME/.vmf/net}"
BRIDGE="${VMF_BRIDGE:-vmfbr0}"
SUBNET="${VMF_NET_SUBNET:-192.168.42.1/24}"
POOL="${VMF_NET_POOL:-4}"
LEASES="$NET_DIR/dnsmasq.leases"
PIDF="$NET_DIR/dnsmasq.pid"

dnsmasq_bin() {
  # Resolve once through nix; callers cache the path.
  nix build nixpkgs#dnsmasq --print-out-paths --no-link | tail -1
}

tap_name() { printf 'tap_vmf%d\n' "$1"; }

tap_busy() { # tap name -> rc0 when a qemu process uses it
  # The carrier semantics: a tap is "busy" only while a process holds
  # it open. A dead VM's conf mention does not block the pool.
  grep -aq "ifname=$1" /proc/[0-9]*/cmdline 2>/dev/null
}

dnsmasq_alive() {
  # dnsmasq runs as root: kill -0 hits EPERM from the user account, so
  # liveness goes through ps instead.
  [[ -f "$PIDF" ]] || return 1
  local p
  p=$(cat "$PIDF" 2>/dev/null)
  [[ -n "$p" ]] && ps -p "$p" -o args= 2>/dev/null | grep -q dnsmasq
}

# iptables hole for the bridge traffic: br_netfilter routes bridged
# frames through the host's chains (kube-router drops them otherwise),
# and guests need NAT for outbound internet.
fw_rule() { # chain spec...
  sudo iptables -C "$@" 2>/dev/null || sudo iptables -I "$@"
}

net_fw() {
  local base
  base=$(echo "${SUBNET%/*}" | cut -d. -f1-3)
  fw_rule INPUT 1 -i "$BRIDGE" -j ACCEPT
  fw_rule FORWARD 1 -i "$BRIDGE" -j ACCEPT
  fw_rule FORWARD 1 -o "$BRIDGE" -j ACCEPT
  sudo iptables -t nat -C POSTROUTING -s "$base.0/24" \
    -o "${WAN_IF:-eno1}" -j MASQUERADE 2>/dev/null || \
    sudo iptables -t nat -A POSTROUTING -s "$base.0/24" \
      -o "${WAN_IF:-eno1}" -j MASQUERADE
  [[ "$(sysctl -n net.ipv4.ip_forward 2>/dev/null)" == "1" ]] || \
    sudo sysctl -w net.ipv4.ip_forward=1 >/dev/null
}

cmd_init() {
  sudo ip link add "$BRIDGE" type bridge 2>/dev/null || true
  sudo ip addr add "${SUBNET%/*}/24" dev "$BRIDGE" 2>/dev/null || true
  sudo ip link set "$BRIDGE" up 2>/dev/null || true
  local i base
  base=$(echo "${SUBNET%/*}" | cut -d. -f1-3)
  # Owner by uid, never $USER: an init under sudo (or with $USER unset)
  # must not mint root-owned taps — qemu is unprivileged and TUNSETIFF
  # on a persistent tap only succeeds for the owner uid.
  local uid
  uid=$(id -u)
  for i in $(seq 0 $((POOL - 1))); do
    local t
    t=$(tap_name "$i")
    if ip -o link show "$t" >/dev/null 2>&1; then
      # Existing tap with the wrong owner (a previous sudo init): replace.
      if ! ip tuntap show | grep -q "^$t:.* user $uid$"; then
        echo "net: replacing $t (owner $(ip tuntap show | grep "^$t:" \
          | sed 's/.* user //;s/ .*//') != $uid)" >&2
        sudo ip tuntap del dev "$t" mode tap 2>/dev/null || true
      fi
    fi
    if ! ip -o link show "$t" >/dev/null 2>&1; then
      sudo ip tuntap add dev "$t" mode tap user "$uid" 2>/dev/null || true
    fi
    sudo ip link set "$t" master "$BRIDGE" 2>/dev/null || true
    sudo ip link set "$t" up 2>/dev/null || true
  done
  net_fw
  mkdir -p "$NET_DIR"
  [[ -f "$LEASES" ]] || touch "$LEASES"
  if ! dnsmasq_alive; then
    local d
    d="$(dnsmasq_bin)/bin/dnsmasq"
    sudo nohup "$d" --keep-in-foreground --bind-interfaces \
      --interface="$BRIDGE" --except-interface=lo \
      --dhcp-range="$base.100,$base.200,12h" \
      --dhcp-leasefile="$LEASES" --pid-file="$PIDF" \
      </dev/null >"$NET_DIR/dnsmasq.log" 2>&1 &
    echo $! > "$NET_DIR/launch.pid"
    # Watchdog: a dnsmasq that dies at startup (bind conflict, bad
    # config) must fail the init loudly, not leave "stopped" silently.
    local tries=0
    while ! dnsmasq_alive && (( tries < 10 )); do
      sleep 0.5
      tries=$((tries + 1))
    done
    if ! dnsmasq_alive; then
      echo "net: dnsmasq did not come up; last log lines:" >&2
      tail -5 "$NET_DIR/dnsmasq.log" >&2 2>/dev/null || true
      return 1
    fi
  fi
  cmd_status
}

cmd_status() {
  local taps n=0 i
  ip -o link show "$BRIDGE" >/dev/null 2>&1 \
    && printf 'net: bridge %s (%s admin-up; carrier on first open tap)\n' \
      "$BRIDGE" "$(ip -o link show "$BRIDGE" | grep -q UP && echo yes || echo no)" \
    || printf 'net: bridge %s MISSING\n' "$BRIDGE"
  for i in $(seq 0 $((POOL - 1))); do
    ip -o link show "$(tap_name "$i")" >/dev/null 2>&1 && n=$((n + 1))
  done
  printf 'net: taps %d/%d, dnsmasq %s\n' \
    "$n" "$POOL" "$(dnsmasq_alive && echo running || echo stopped)"
  printf 'net: dhcp range %s.100-%s.200, leases: %s\n' \
    "$(echo "${SUBNET%/*}" | cut -d. -f1-3)" \
    "$(echo "${SUBNET%/*}" | cut -d. -f1-3)" \
    "$([[ -f "$LEASES" ]] && wc -l < "$LEASES" || echo 0)"
}

cmd_ready() {
  ip -o link show "$BRIDGE" >/dev/null 2>&1 || return 1
  dnsmasq_alive || return 1
  local i
  for i in $(seq 0 $((POOL - 1))); do
    tap_busy "$(tap_name "$i")" || return 0
  done
  return 1
}

cmd_claim() {
  local mac="${1:-}" i t
  [[ -n "$mac" ]] || { echo "usage: vmf-net.sh claim <mac>" >&2; return 2; }
  for i in $(seq 0 $((POOL - 1))); do
    t=$(tap_name "$i")
    # Existence first: after a host reboot the pool is gone, and handing
    # out a phantom name makes qemu die on /dev/net/tun (unprivileged).
    ip -o link show "$t" >/dev/null 2>&1 || continue
    if ! tap_busy "$t"; then
      printf '%s\n' "$t"
      return 0
    fi
  done
  echo "net: no free tap in the pool ($POOL); run 'just lab-init' or raise VMF_NET_POOL" >&2
  return 1
}

cmd="${1:-status}"
case "$cmd" in
  init) cmd_init ;;
  status) cmd_status ;;
  ready) cmd_ready ;;
  claim) shift; cmd_claim "${1:-}" ;;
  *) echo "usage: vmf-net.sh init|status|ready|claim <mac>" >&2; exit 2 ;;
esac
