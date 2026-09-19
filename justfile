lab := "strix"
uvrun := "env -u VIRTUAL_ENV uv run"

default:
    @just --list

# List boxes, running state, and dedicated IP
list:
    #!/bin/sh
    for spec in specs/*.yaml; do
      name=$(basename "$spec" .yaml)
      if pgrep -f "qemu-system.*-name $name" >/dev/null 2>&1; then
        state=$( (printf 'info status\n'; sleep 1) | nc -N -U "build/$name/mon.sock" 2>/dev/null | grep -oE 'paused|running' | head -1)
        state=${state:-running}
      else
        state=stopped
      fi
      ip=$(awk '/network:/{f=1} f && /ip:/{print $2; exit}' "$spec")
      printf '%-24s %-10s %s\n' "$name" "$state" "${ip:--}"
    done
    # Ephemeral microVMs (krunvm), if any; names are plain single tokens
    if command -v krunvm >/dev/null 2>&1 && command -v buildah >/dev/null 2>&1; then
      buildah unshare -- krunvm list 2>/dev/null | grep -E '^[A-Za-z0-9_.-]+$'
    else
      nix shell nixpkgs#krunvm nixpkgs#buildah -c buildah unshare -- krunvm list 2>/dev/null | grep -E '^[A-Za-z0-9_.-]+$'
    fi | while IFS= read -r vm; do
      printf '%-24s %-10s %s\n' "$vm" microvm "-"
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

# Run an OCI image as an ephemeral microVM (docker-style flags)
run *args:
    sh scripts/oci-run.sh {{ args }}

# SSH into a running box; pass a command to run it remotely instead
ssh lab=lab *cmd:
    sh scripts/enter.sh {{ lab }} {{ cmd }}

# SSH into a running box (alias for ssh)
enter lab=lab *cmd:
    sh scripts/enter.sh {{ lab }} {{ cmd }}

# Run a command inside a running box (ssh) or microVM (agent exec)
exec name *cmd:
    #!/bin/sh
    if [ -f "build/{{ name }}/vm.conf" ]; then
      exec sh scripts/enter.sh "{{ name }}" {{ cmd }}
    fi
    exec sh scripts/oci-exec.sh "{{ name }}" {{ cmd }}

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

# Shut down a running box. ACPI first, then force after 10 seconds.
stop lab=lab:
    #!/bin/sh
    sock="build/{{ lab }}/mon.sock"
    [ -S "$sock" ] && { echo system_powerdown | nc -N -U "$sock" >/dev/null 2>&1 || true; }
    i=0
    while [ "$i" -lt 10 ]; do
      pgrep -f "qemu-system.*-name {{ lab }}" >/dev/null 2>&1 || { echo "box {{ lab }} stopped"; exit 0; }
      i=$((i + 1))
      sleep 1
    done
    pkill -f "qemu-system.*-name {{ lab }}" || true
    echo "box {{ lab }} force stopped"

# Snapshot a box disk (box should be stopped for a consistent checkpoint)
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

# Remove generated build output for a box
clean lab:
    rm -rf build/{{ lab }}

# Remove all generated build output
clean-all:
    rm -rf build
