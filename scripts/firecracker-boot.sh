#!/usr/bin/env bash
# Firecracker engine boot. Unlike the qemu engine this needs no buildah
# unshare for the run: the rootfs is a cached read-only squashfs of the
# derived image, per-run inputs travel on a small ext4 drive, and
# networking is a per-VM user network namespace with slirp4netns (the
# rootless podman stack): unshare -Urn gives CAP_NET_ADMIN over a
# loopback-only netns, slirp4netns creates tap0 inside it, and host
# port publishing goes through slirp4netns' add_hostfwd API (host-side
# binds stay in the host namespace, so `vmf ssh`/curl work normally).
#
# Guest addresses are static (10.0.2.15/24 gw 10.0.2.2), configured by
# the initramfs init — the same file the qemu engine uses, with a
# firecracker branch keyed on the presence of the inputs drive.
#
# Lifecycle: the inner process execs firecracker, so the recorded PID
# IS the VM process; killing it tears down guest, netns and slirp.
# Cleanup ownership uses the same conf-PID gate as the qemu engine.
#
# Env: VMF_NAME VMF_RUNDIR VMF_ASSETS_SQUASHFS VMF_KERNEL VMF_INITRAMFS
#      VMF_CONSOLE VMF_CPUS VMF_MEM VMF_TIMEOUT_SECS VMF_NET_MODE
set -euo pipefail

: "${VMF_NAME:?}" "${VMF_RUNDIR:?}" "${VMF_ASSETS_SQUASHFS:?}" "${VMF_KERNEL:?}" "${VMF_INITRAMFS:?}" "${VMF_CONSOLE:?}"
VMF_TIMEOUT_SECS="${VMF_TIMEOUT_SECS:-0}"
VMF_CONF="${VMF_CONF:-$VMF_RUNDIR.conf}"
VMF_NET_MODE="${VMF_NET_MODE:-open}"

self="$(readlink -f "${BASH_SOURCE[0]}")"
boot_json="$VMF_RUNDIR/boot.json"
guest_mac="06:00:AC:10:00:02"

cat > "$boot_json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "$VMF_KERNEL",
    "initrd_path": "$VMF_INITRAMFS",
    "boot_args": "console=ttyS0 reboot=k panic=-1"
  },
  "drives": [
    { "drive_id": "rootfs", "is_root_device": false, "is_read_only": true,
      "path_on_host": "$VMF_ASSETS_SQUASHFS" },
    { "drive_id": "inputs", "is_root_device": false, "is_read_only": true,
      "path_on_host": "$VMF_RUNDIR/inputs.ext4" }
  ],
  "network-interfaces": [
    { "iface_id": "0", "guest_mac": "$guest_mac", "host_dev_name": "tap0" }
  ],
  "machine-config": { "vcpu_count": ${VMF_CPUS:-2}, "mem_size_mib": ${VMF_MEM:-1024} }
}
EOF

python3 - "$boot_json" <<'PY'
import json, os, sys
cfg = json.load(open(sys.argv[1]))
if os.environ.get("VMF_NET_MODE") == "off":
    cfg.pop("network-interfaces", None)
data = os.environ.get("VMF_DATA_DRIVE")
if data:
    cfg["drives"].append({"drive_id": "data", "is_root_device": False,
                          "is_read_only": False,
                          "path_on_host": os.environ["VMF_DATA_DRIVE"]})
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
PY

fc_run() {
  local tcmd=()
  [[ "$VMF_TIMEOUT_SECS" -gt 0 ]] && tcmd=(timeout --signal=TERM "$VMF_TIMEOUT_SECS")
  # exec: the calling shell becomes the VM process, so the pid recorded
  # in fc.pid/state is the one to kill. Guest serial output flows
  # through the process stdio (firecracker has no -serial file option).
  exec "${tcmd[@]}" firecracker --api-sock "$VMF_RUNDIR/fc.sock" --config-file "$boot_json"
}

# --inner: runs inside the user netns. slirp4netns owns tap1 (created
# from outside); this side waits for it, builds a kernel bridge (br0)
# that enslaves tap1 plus firecracker's tap0 (created later by
# firecracker itself), records the pid, then execs firecracker so the
# pid stays the VM process for the recorded state.
if [[ "${1:-}" == "--inner" ]]; then
  # Signal the netns is live BEFORE slirp4netns attaches: otherwise the
  # outer can read /proc/<pid>/ns/net before unshare(2) completed and
  # setns into the wrong namespace (EPERM race).
  touch "$VMF_RUNDIR/netns-ready"
  i=0
  while [[ $i -lt 100 ]]; do
    ip link show tap1 >/dev/null 2>&1 && break
    i=$((i + 1)); sleep 0.1
  done
  ip link show tap1 >/dev/null 2>&1 || { echo "vmf-fc: tap1 never appeared" >&2; exit 1; }
  ip link add br0 type bridge
  ip link set tap1 master br0
  ip link set tap1 up
  ip link set br0 up
  (
    # Enslave firecracker's tap0 the moment it appears.
    while :; do
      if ip link show tap0 >/dev/null 2>&1; then
        ip link set tap0 master br0
        ip link set tap0 up
        break
      fi
      sleep 0.02
    done
  ) &
  echo $$ > "$VMF_RUNDIR/fc.pid"
  fc_run
fi

owns_state() {
  local fpid="$1" confpid=""
  [[ -f "$VMF_CONF" ]] || return 1
  confpid=$(grep -oE '^PID=[0-9]+' "$VMF_CONF" 2>/dev/null | cut -d= -f2 || true)
  [[ -n "$confpid" && "$confpid" == "$fpid" ]]
}

cleanup() {
  local fpid="$1"
  if [[ "${VMF_KEEP:-0}" != "1" ]]; then
    rm -rf "$VMF_RUNDIR"
    if owns_state "$fpid"; then
      rm -f "$VMF_CONF"
    fi
  fi
}

# Orchestrated net boot: netns (unshare) + tap (slirp4netns) + hostfwd
# (API) + firecracker inside. The unshare process waits for its child,
# so its pid is a stable netns-owner handle for slirp4netns.
net_boot() {
  # Guest serial flows through firecracker's stdio -> console log.
  unshare -Urn --propagation private bash "$self" --inner >>"$VMF_CONSOLE" 2>&1 &
  nspid=$!
  i=0
  while [[ $i -lt 100 ]]; do
    [[ -f "$VMF_RUNDIR/netns-ready" ]] && break
    if ! kill -0 "$nspid" 2>/dev/null; then
      echo "vmf-fc: netns process exited before signaling readiness" >&2
      return 1
    fi
    i=$((i + 1)); sleep 0.1
  done
  [[ -f "$VMF_RUNDIR/netns-ready" ]] || { echo "vmf-fc: netns never became ready" >&2; return 1; }
  slirp4netns --ready-fd=3 --api-sock "$VMF_RUNDIR/slirp-api.sock" \
    --disable-host-loopback "$nspid" tap1 3>"$VMF_RUNDIR/slirp-ready" \
    >/dev/null 2>&1 &
  slirp_pid=$!
  i=0
  while [[ $i -lt 100 ]]; do
    [[ -s "$VMF_RUNDIR/slirp-ready" ]] && break
    if ! kill -0 "$slirp_pid" 2>/dev/null; then
      echo "vmf-fc: slirp4netns exited early" >&2
      return 1
    fi
    i=$((i + 1)); sleep 0.1
  done
  [[ -s "$VMF_RUNDIR/slirp-ready" ]] || { echo "vmf-fc: slirp4netns not ready" >&2; kill "$slirp_pid" 2>/dev/null || true; return 1; }
  while IFS= read -r pair; do
    [[ -n "$pair" ]] || continue
    hport="${pair%% *}"; gport="${pair##* }"
    resp=$(printf '{"execute":"add_hostfwd","arguments":{"proto":"tcp","host_addr":"127.0.0.1","host_port":%s,"guest_addr":"10.0.2.15","guest_port":%s}}' \
      "$hport" "$gport" | timeout 5 nc -U "$VMF_RUNDIR/slirp-api.sock" || true)
    [[ "$resp" == *'"return"'* ]] || { echo "vmf-fc: hostfwd $hport failed: $resp" >&2; }
  done < "$VMF_RUNDIR/hostfwd"
  wait "$nspid" || true
  kill "$slirp_pid" 2>/dev/null || true
  wait "$slirp_pid" 2>/dev/null || true
}

orchestrate() {
  local fpid=""
  if [[ "$VMF_NET_MODE" == "off" ]]; then
    fc_run </dev/null >>"$VMF_CONSOLE" 2>&1 &
    fpid=$!
    wait "$fpid" 2>/dev/null || true
    cleanup "$fpid"
  else
    net_boot </dev/null >>"$VMF_CONSOLE" 2>&1 || true
    if [[ -f "$VMF_RUNDIR/fc.pid" ]]; then
      fpid="$(cat "$VMF_RUNDIR/fc.pid")"
      printf 'PID=%s\nCTR=\n' "$fpid" >> "$VMF_CONF"
    fi
    cleanup "${fpid:-0}"
  fi
}

if [[ "${VMF_DETACH:-0}" == "1" ]]; then
  (
    orchestrate
  ) >/dev/null 2>&1 &
  disown
  sleep 1
  if [[ ! -f "$VMF_RUNDIR/fc.pid" ]]; then
    echo "warning: firecracker pid missing after boot; check console log: $VMF_CONSOLE" >&2
  fi
  exit 0
fi

orchestrate
