lab := "strix"
uvrun := "env -u VIRTUAL_ENV uv run"

# Recipe arguments arrive as positional parameters ($0 = recipe name):
# "$@" forwards them verbatim, so quoted values survive
# (--intent "Run 3 instances").
set positional-arguments

default:
    @just --list

# List boxes, running state, and dedicated IP
list:
    #!/bin/sh
    for spec in specs/*.yaml; do
      name=$(basename "$spec" .yaml)
      # Box liveness = its QEMU monitor socket answering. A pgrep on
      # "-name $name" would also match a microVM of the same name.
      state=$( (printf 'info status\n'; sleep 1) | nc -N -U "build/$name/mon.sock" 2>/dev/null | grep -oE 'paused|running' | head -1)
      state=${state:-stopped}
      ip=$(awk '/network:/{f=1} f && /ip:/{print $2; exit}' "$spec")
      printf '%-24s %-10s %s\n' "$name" "$state" "${ip:--}"
    done
    # Ephemeral microVMs, from run state files (engine-aware)
    for conf in "$HOME"/.vmf/runs/*.conf; do
      [ -f "$conf" ] || continue
      name=$(basename "$conf" .conf)
      . "$conf"
      engine=${ENGINE:-krunvm}
      if [ "$engine" = "qemu" ] || [ "$engine" = "firecracker" ]; then
        rundir=${RUNDIR:-$HOME/.vmf/runs/$name}
        pid=${PID:-}
        if [ -z "$pid" ]; then
          for f in qemu.pid fc.pid; do
            if [ -f "$rundir/$f" ]; then pid=$(cat "$rundir/$f"); break; fi
          done
        fi
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then state=running; else state=stopped; fi
      else
        if command -v krunvm >/dev/null 2>&1 && command -v buildah >/dev/null 2>&1; then
          KR=1
        else
          KR=0
        fi
        if [ "$KR" = "1" ] && buildah unshare -- krunvm list 2>/dev/null | grep -qx -- "$name"; then
          state=running
        else
          state=stopped
        fi
      fi
      printf '%-24s %-10s %s\n' "$name" "microvm/$engine" "ssh port ${PORT:--} ($state)"
    done

# Validate a spec: schema, base pin, roles, Dockerfile subset
validate lab=lab:
    {{ uvrun }} scripts/validate.py specs/{{ lab }}.yaml

# Generate the Packer template, playbook, seeds, and provision script
generate lab=lab:
    {{ uvrun }} scripts/generate.py specs/{{ lab }}.yaml

# Export a spec to a standalone Dockerfile (derived; do not edit)
dockerfile lab=lab:
    {{ uvrun }} scripts/export_dockerfile.py specs/{{ lab }}.yaml

# Build a box image with Packer on QEMU/KVM
build lab=lab: (generate lab)
    rm -rf build/{{ lab }}/image
    cd build/{{ lab }} && packer init . && packer build box.pkr.hcl

# Boot a built box on the virbr0 network with a dedicated IP
boot lab=lab:
    sh scripts/boot.sh {{ lab }}

# Run an OCI image as an ephemeral microVM (docker-style flags); ssh in
# with `just ssh <name>` afterwards. "$@" + positional-arguments keeps
# quoting intact (--intent "Run 3 instances" reaches oci-run as one arg).
run *args:
    exec sh scripts/oci-run.sh "$@"

# SSH into a running box (qemu) or microVM (dropbear); pass a command to
# run it remotely instead. Docker-style leading flags are accepted/ignored.
ssh *args:
    exec sh scripts/ssh.sh "$@"

# SSH into a running box or microVM (alias for ssh)
enter *args:
    exec sh scripts/ssh.sh "$@"

# Run a command inside a running box or microVM (same router as ssh);
# docker-style leading flags accepted and ignored
exec *args:
    exec sh scripts/ssh.sh "$@"

# Print the dedicated IP of a box, declared in the spec
ip lab=lab:
    {{ uvrun }} python -c "import yaml; spec = yaml.safe_load(open('specs/{{ lab }}.yaml')); print(spec['network']['ip'])"

# Pause a running box (CPU halted, RAM kept in host memory; ssh freezes)
pause lab=lab:
    echo stop | nc -N -U "build/{{ lab }}/mon.sock" >/dev/null

# Resume a paused box (TCP connections survive the pause)
resume lab=lab:
    echo cont | nc -N -U "build/{{ lab }}/mon.sock" >/dev/null

# Show the box's QEMU status and current snapshot list
status lab=lab:
    #!/bin/sh
    (printf 'info status\n'; sleep 1) | nc -N -U "build/{{ lab }}/mon.sock" 2>/dev/null | tail -2
    qemu-img snapshot -l "build/{{ lab }}/image/vmf-{{ lab }}" 2>/dev/null | head -6

# Shut down a box (ACPI first, then force after 10 seconds) or a microVM
# (engine-aware: qemu pid kill + container cleanup, or krunvm tree).
stop lab=lab:
    sh scripts/stop.sh {{ lab }}

snap label lab=lab:
    qemu-img snapshot -c "{{ label }}" "build/{{ lab }}/image/vmf-{{ lab }}"

# List snapshots of a box disk
snaps lab=lab:
    qemu-img snapshot -l "build/{{ lab }}/image/vmf-{{ lab }}"

# Restore a snapshot; the box must be stopped
revert label lab=lab:
    #!/bin/sh
    if pgrep -f "qemu-system.*-name {{ lab }}" >/dev/null 2>&1; then
      echo "error: box {{ lab }} is running; run: just stop {{ lab }}" >&2
      exit 1
    fi
    qemu-img snapshot -a "{{ label }}" "build/{{ lab }}/image/vmf-{{ lab }}"
    echo "ok: reverted {{ lab }} to {{ label }}"

# Inventory and remove run artifacts: terminated VMs, data drives, plan
# caches, race artifacts. Dry run + gate by default; --yes executes.
clean *args:
    #!/bin/sh
    exec bash scripts/vmf_clean.sh "$@"

# Delete winner-cache entries (forces a fresh race next run; for tests).
# --tree H limits to one tree prefix; --plans also clears plan caches.
cache-clean *args:
    #!/bin/sh
    exec bash scripts/vmf_clean.sh --yes --cache "$@"

# Build the fat base once: node 22 + python3 + pip + sqlite3 + nginx,
# frozen host-side. The gap-fill prefers it when the marker exists.
fat-base *args:
    #!/bin/sh
    exec bash scripts/build-fat-base.sh "$@"

# Docker-ps view of VM instances: id, ref, approach, status, target.
# --all shows race candidates; --json for scripts.
ps *args:
    #!/bin/sh
    exec python3 scripts/vmf_ps.py "$@"

# One-time host bridge stack for ip mode: bridge, tap pool, dnsmasq.
# Needs sudo once; boots never do (they claim a free tap).
lab-init:
    #!/bin/sh
    exec bash scripts/vmf_net.sh init

# Bridge stack summary
net-status:
    #!/bin/sh
    exec bash scripts/vmf_net.sh status

# Remove generated build output for a box
clean-lab lab:
    rm -rf build/{{ lab }}

# Remove all generated build output
clean-all:
    rm -rf build
