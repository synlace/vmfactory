# vmfactory

Declarative VM factory for a KVM host: spec in, built image out, boot, ssh in.
"Ubuntu + strix" is instance one, not the design. The boot recipe is lifted
from AnyCTF (TAP pool, name-derived MAC, monitor socket); the CTF layers
(states, flags, solver) were left behind.

## Layout

```
images.yaml        pinned base images (url + checksum + user + FROM refs)
schemas/           spec JSON schema
specs/*.yaml       one box per spec
docker/            Dockerfiles consumed via provision.dockerfile
roles/             shared Ansible roles (platform first: common, docker, pipx)
scripts/           generate.py, validate.py, boot.sh, enter.sh, vmf_lib.py
build/             generated output (gitignored)
```

## Quickstart

```sh
just validate strix     # schema + pins + roles + Dockerfile subset
just build strix        # generate -> packer build (pinned noble + ansible)
just boot strix         # qemu + TAP + static IP from the spec
just ssh strix          # ssh in (user/password from the build seed)
just ssh strix "uname -a"   # or run one command remotely
VMF_SSH_USER=root just ssh strix   # override the ssh user (needs root login enabled)
just stop strix
just snap strix clean   # qemu-img checkpoint
just revert strix clean # must be stopped
```

## Spec keys

| Key | Meaning |
|---|---|
| `name` | box name; drives MAC, hostname, build dir |
| `base` | key into images.yaml |
| `cpu`, `memory_gb`, `disk_gb` | resources |
| `network.ip`, `.gateway` | static address baked into the boot seed |
| `roles` | Ansible roles, applied in order (platform first) |
| `provision.dockerfile` | Dockerfile translated into the build, after roles |
| `files` | Files baked into the image: `src` (repo-root relative), `dest` (absolute), optional `owner`/`group`/`mode`/`sha256` |

`files` entries are staged by Packer file provisioners and installed by
`provision.sh` with the declared owner/group/mode. A `sha256` pin is
checked at `just validate` time; a mismatch fails before any build.

## Dockerfile support (subset)

Translated: `RUN` (shell form), `COPY`/`ADD` (single src), `ENV`, `WORKDIR`,
`USER root`. Ignored with a warning: `EXPOSE`, `CMD`, `ENTRYPOINT`,
`HEALTHCHECK`, `LABEL`, `VOLUME`. Rejected: multi-stage (`FROM` x2, `--from`),
exec-form `RUN`, digest refs, unknown directives, `USER != root`.

`FROM` must resolve to a pin in images.yaml. Docker tags float; pins do not.
A checksum mismatch fails the build loudly — refresh pins deliberately.

One source of truth per tool: if a Dockerfile exists, reference it from the
spec; do not also hand-write an Ansible role for the same thing.

## Exports (derived, never edited)

```sh
just dockerfile strix   # spec -> build/strix/Dockerfile (standalone)
docker build -f build/strix/Dockerfile .
```

The export merges role package lists (`roles/*/defaults/main.yml`
`vmf_packages`) with the referenced provision Dockerfile's content.
Roles without `vmf_packages` get a note, not a guess. `COPY` in the
referenced Dockerfile blocks the export (context paths do not carry over).

## Ephemeral OCI runs (krunvm backend)

```sh
just run --rm -p 3002:3000 bkimminich/juice-shop   # run an OCI image in a microVM
just run --rm -d -p 8080:80 nginx                  # detached: prompt returns at once
just run --rm -e FOO=bar image cmd                 # command + env (quote-free)
just run --keep image                              # keep the VM, then ssh in
just ssh image                                     # one-shot exec (PTY-free)
just ssh image 'ls /proc'                          # one-shot, docker-exec semantics
just stop image                                    # stop + delete a microVM
just list                                          # shows microVMs + ssh ports
```

- Runtime engines (`--engine qemu|firecracker|krunvm`, default qemu;
  `VMF_ENGINE` env or a context engine):
  - **qemu** (default): a real kernel built on demand from nixpkgs
    (`pkgs.linux` with virtio/9p/devpts forced built-in, cached in
    `~/.local/share/vmf/microvm`), booted with direct kernel + a tiny
    initramfs. The container rootfs (derived image) is shared read-write
    over 9p; networking is QEMU user-mode slirp with `hostfwd` for `-p`
    (port collisions fail LOUDLY at boot, no silent misbinds). Real
    kernel means real `/dev/pts`: interactive ssh shells and sftp work.
    Falls back to `nix shell nixpkgs#qemu nixpkgs#buildah` when not on
    PATH.
  - **firecracker**: sandbox-grade engine. The derived image is packed
    into a cached read-only squashfs (per base+payload, like the derive
    layer); guest writes land on a tmpfs overlay (RAM-bounded, wiped on
    stop — `--rm` semantics become literal). Per-run inputs travel on a
    small read-only ext4 drive. Networking is the rootless podman
    stack: a user netns per VM (`unshare -Urn`), `slirp4netns` for the
    tap, and `-p` forwarding via slirp4netns' add_hostfwd API with
    `--disable-host-loopback` (guest cannot reach host services by
    design). Same guest kernel + initramfs as qemu; devices enumerate
    over firecracker's ACPI tables. Needs `nix shell` on first use for
    the firecracker toolchain (firecracker, slirp4netns, mksquashfs,
    e2fsprogs).
  - **krunvm** (legacy): libkrun microVM inside a `buildah unshare`
    user namespace. Faster boot, but libkrun's TSI network stack
    virtualizes privileged guest binds (breaking vhost-based apache —
    see the apache shim note) and cannot open pts devices (no PTY
    sessions). Kept for comparison; one-shot ssh still works.
- `--rm` is the default: the microVM is deleted when the process exits.
  Ctrl-C in a terminal tears down the whole tree.
- Digest pinning: first run records the digest (TOFU) in `~/.vmf/oci-pins`;
  later runs fail loudly on drift. `VMF_OCI_PINS` overrides the pin file.
- Unprefixed image names get docker.io normalization (`alpine` ->
  `docker.io/library/alpine`).
- `-p HOST:GUEST` publishes ports docker-style. `-e` is applied by the
  guest init before the app starts — ENTRYPOINT/CMD/ENV/USER/WORKDIR are
  resolved from the OCI config blob (skopeo) and handed to the init via
  a per-run share. Quoting survives: argv travels in files.
- Privileged guest ports: qemu binds them inside the guest (guest root),
  so `-p 8080:80` works natively. The krunvm engine virtualizes them via
  TSI (see the apache note below); an UNMAPPED privileged bind under
  krunvm fails with `Permission denied`.

### Sandbox flags (untrusted-code guardrails)

Opt-in today; the planned `vmf run <repo-url>` feature will force a
sandboxed combination automatically when it runs un-audited code.

- `--net restricted`: a Landlock ruleset (kernel 6.7+, x86_64) denies
  every outbound `connect()` of the qemu process. The guest cannot reach
  the internet or host services through the slirp gateway — outbound
  TCP dies with an ICMP unreachable, which slirp converts cleanly.
  Published ports (`-p`) and `just ssh` are inbound and keep working.
  DNS still resolves because slirp's resolver uses UDP, which Landlock
  does not govern yet; a determined guest can exfiltrate over UDP.
  Full network closure needs the per-VM netns work (planned).
- `--net off`: no network device at all (also no ssh, no `-p`).
- `--timeout 30m` (also `45s`, `2h`): the VM is powered off after the
  duration and the run state self-cleans; for untrusted jobs that must
  not outlive their welcome.
- `--disk-cap 10G` (also `500M`): caps single-file guest writes through
  the 9p root share at the storage layer (qemu's `RLIMIT_FSIZE`; SIGXFSZ
  is ignored so the write fails with EFBIG instead of killing the VM).
  With the loose 9p cache, in-guest writes beyond the cap may appear to
  succeed (data sits in guest RAM, writeback fails) — the host disk
  stays capped either way. Guest `/tmp` is tmpfs (RAM), so it is not
  covered by this cap.
- The sandbox flags apply to the qemu engine; the krunvm engine warns
  and ignores them.

## The `vmf` CLI

The `vmf` command wraps the same scripts with context management and
json output. Install: `ln -sf "$PWD/bin/vmf" ~/.local/bin/vmf`.

```
vmf run --rm -d -p 3000:3000 grafana/grafana   # same flags as just run
vmf ps                    # name, engine, state, ssh port; --json too
vmf ssh grafana           # interactive shell; one-shot with a command
vmf logs grafana -f       # console log
vmf stop grafana
vmf pin ls                # digest pins (TOFU); --json too
vmf context ls|use|current|add|remove
```

Contexts pick where verbs run and how. `default` is implicit (local,
qemu engine, the global `~/.vmf/runs` state). Other local contexts get
their own engine (`--engine qemu|krunvm`) and their own state directory
(`~/.vmf/contexts/<name>/runs`), so names cannot collide across
contexts. Provider contexts (`--type provider --url ...`) are
config-first: verbs on them fail with a clear error until a cloud
backend exists — the CLI surface is already provider-shaped.

The sandbox flags (`--net restricted`, `--timeout`, `--disk-cap`) carry
over unchanged: `vmf run` is `scripts/oci-run.sh`, and going through
`vmf` instead of `just` also avoids the justfile quote-flattening when
passing guest commands.

## SSH into microVMs (derived ssh server)

`just ssh <name>` reaches both box types: qemu boxes (via `enter.sh`,
real sshd from the cloud image) and microVMs (via a derived image with
an in-guest ssh server). For microVMs:

- On first use of an image, `scripts/derive.sh` commits one extra layer
  on top of the pinned bytes: a static musl bundle
  (`~/.local/share/vmf/ssh-bundle`: busybox + dropbear for krunvm,
  OpenSSH sshd + ssh-keygen for qemu; stage or update it with
  `scripts/stage-bundle.sh`, which builds everything from nixpkgs with
  sshd's utmp/wtmp logging disabled) plus `scripts/guest/init.sh`,
  plus `/etc/passwd` + `/etc/shells` fixups so every base behaves
  identically (distroless gets a root entry; root's shell is forced to
  `/vmf/sh`).
- The guest init starts the ssh server on guest port 22 (pubkey-only,
  host keys generated in-guest at boot) and supervises the image
  entrypoint with signal forwarding. qemu VMs power off when the
  entrypoint exits; krunvm VMs exit when the init exits.
- Auth: one client keypair per host user (`~/.vmf/ssh/id_ed25519`,
  TOFU-generated on first run); its public half is mounted into the VM
  and installed as root's `authorized_keys`. MicroVM host keys are
  ephemeral, so `just ssh` uses `StrictHostKeyChecking=no`.
- The ssh host port is stable per VM name (hash, 20000-29999) and stored
  in `~/.vmf/runs/<name>.conf`, which `just ssh` reads; `just list`
  shows it with engine and state.
- qemu engine: interactive shells and sftp work (`Subsystem sftp
  internal-sftp`; plain `scp` works too). krunvm engine: one-shot
  commands are the contract (libkrun cannot open pts devices); `just
  ssh` strips `-t` style flags with a notice there. The derived layer
  symlinks every busybox applet into `/vmf/bin` and appends it to PATH,
  so even distroless images run plain `ls`, `grep`, `ps` etc.
- `--no-ssh` boots the pinned image as-is: no derive, no ssh,
  entrypoint through the engine directly.
- Apache CGI images on the krunvm engine: TSI port mapping virtualizes
  guest binds on privileged ports, and `<VirtualHost *:80>` sections
  never match in the running daemon (`NameVirtualHost *:80 has no
  VirtualHosts` on every re-parse), so vhost-declared ScriptAliases
  vanish and `/cgi-bin/*` 404s. The derive step detects Debian apache
  layouts (`/etc/apache2` + `/usr/lib/cgi-bin`) and re-declares the
  cgi-bin mapping at main-server level (`conf.d/zzz-vmf-cgi.conf`),
  where directives apply. The qemu engine does not have this problem
  (real kernel binds); nginx-style single-default-server images are
  unaffected either way.

## Port exposure (firecracker engine)

`vmf run` needs no `-p` knowledge: the guest discovers its own listeners
and the host publishes them 1:1 (`--expose all`, the default).

- Modes: `--expose all` (default; every port the guest finds, TCP+UDP)
  | `declared` (only `-p` / compose-declared ports; also the default
  once any explicit `-p` is present) | `none` (no publishing).
- Compose projects: declared `ports:` map the VM port to the container
  port through a static socat bridge; `expose:`-only listeners and
  EXPOSEd image ports are auto-forwarded at their own port number.
- Plumbing: a detached guest daemon (`/vmf/expose.sh`, log at
  `/vmf/expose.log`) probes listeners every 2 s and writes
  `/vmf/ports-live.txt`; a host-side poller (`scripts/expose-poller.sh`,
  log at `<rundir>/expose-host.log`) reads it over ssh and adds slirp
  `add_hostfwd` entries (127.0.0.1:port -> 10.0.2.15:port). The first
  service to claim a (proto, port) keeps it; later claims on the same
  port are recorded as conflicts. Host-port collisions (another VM or
  host service) show up as `conflict` in the table.
- `vmf ps` shows the published ports; `vmf url NAME [PORT]` prints the
  reach address (`127.0.0.1:<port>`).
- Limits: dynamic hostfwd is firecracker-only (qemu user-net hostfwd is
  fixed at boot; the guest daemon still runs, so declared socat bridges
  work there). UDP through slirp4netns' `add_hostfwd` is one-way:
  host->guest datagrams arrive, but libslirp drops the guest->host
  return path (verified with slirp4netns 1.3.3 / libslirp 4.9.1). TCP
  request/reply works fully. For UDP services, prefer a declared
  forward and note the one-way behavior; dynamic UDP publishing is
  best-effort until the engine moves to a slirp port driver or pasta.
- Auto mode never publishes ports below 1024 except ssh (bind needs
  privileges), skips the ephemeral range (32768-60999), and skips
  listener sockets bound to loopback only.

## Conventions

- Roles build the platform (common, docker, pipx); Dockerfiles add tools.
- The build user comes from the base pin (`user:`); password `vmf-build`
  (cloud-init, packer ssh, and `just enter` all use it).
- Boxes get a static IP from the spec via a runtime seed ISO
  (`boot-seed.iso`); the Packer build itself uses user-mode DHCP.
- Host keys change on rebuild and snapshot revert (same IP, new disk).
  `boot.sh` resets the per-box `known_hosts` each boot, and `enter.sh`
  clears a stale key and retries once if one slips through.
- MAC = `52:54:00` + md5(name); IP pool is the AnyCTF virbr0 NAT
  (192.168.124.0/24); TAP devices `tap-ctf-0..7`.
- Snapshots: `qemu-img snapshot -c <label>`; rebuild with Packer is the
  source of truth, snapshots are disposable.
