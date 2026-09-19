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
just run --rm -e FOO=bar image cmd                 # command + env (quote-free)
just run --keep image                              # keep the VM, then ssh in
just ssh image                                     # interactive shell (PTY)
just ssh image 'ls /proc'                          # one-shot, docker-exec semantics
just ssh -it image true                            # docker-style flags accepted/ignored
```

- Runtime: krunvm (libkrun) inside a `buildah unshare` user namespace.
  Falls back to `nix shell nixpkgs#krunvm nixpkgs#buildah` when not on PATH.
- `--rm` is the default: the microVM is deleted when the process exits.
  Ctrl-C in a terminal tears down the whole tree.
- Digest pinning: first run records the digest (TOFU) in `~/.vmf/oci-pins`;
  later runs fail loudly on drift. `VMF_OCI_PINS` overrides the pin file.
- Unprefixed image names get docker.io normalization (`alpine` ->
  `docker.io/library/alpine`).
- `-p` uses krunvm port mapping (host:guest), rootless. `-e` is applied by
  the guest init before the app starts — krunvm starts guests with a clean
  environment and ignores the image's default command, so ENTRYPOINT/CMD/
  ENV/USER/WORKDIR are resolved from the OCI config blob (skopeo) and
  handed to the init via a per-run mount. Quoting survives: argv travels
  in a file, not through the krunvm command line.
- Privileged ports fail rootless when unmapped: a guest bind with no `-p`
  mapping translates to a direct host bind under your uid, so binding
  below 1024 fails (`listen() ... Permission denied`). With `-p 8080:80`
  the host side binds 8080 (unprivileged OK) and the guest bind is
  virtualized by TSI — mapped ports work even for guest port 80.

## SSH into microVMs (dropbear layer)

`just ssh <name>` reaches both box types: qemu boxes (via `enter.sh`,
real sshd from the cloud image) and krunvm microVMs (via a derived
dropbear image). For microVMs:

- On first use of an image, `scripts/derive.sh` commits one extra layer
  on top of the pinned bytes: a static musl `dropbear` + `dropbearkey` +
  `busybox` bundle (`~/.local/share/vmf/ssh-bundle`) and
  `scripts/guest/init.sh`, plus `/etc/passwd` + `/etc/shells` fixups so
  every base behaves identically (distroless gets a root entry; root's
  shell is forced to `/vmf/sh` — dropbear rejects shells missing from
  `/etc/shells`, like alpine's `/bin/ash`).
- The derived image is cached under a content-addressed tag
  (`vmf-ssh:<base-hash>-<payload-hash>`: base pinned ref + bundle +
  init + derive script) in the buildah store, recorded in
  `~/.vmf/derive/`; any payload change or base digest change rebuilds
  it. The pinned bytes themselves are never modified.
- The guest init (`/vmf/init.sh`) starts dropbear on guest port 22
  (pubkey-only, host keys generated in-guest at boot) and supervises the
  image entrypoint with signal forwarding; the VM exits when the
  entrypoint exits (libkrun's init.krun is PID 1 and reaps).
- Auth: one client keypair per host user (`~/.vmf/ssh/id_ed25519`,
  TOFU-generated on first run); its public half is mounted into the VM
  and installed as root's `authorized_keys`. Host key checking is
  `accept-new` into `~/.vmf/ssh/known_hosts`.
- The ssh host port is stable per VM name (hash, 20000-29999) and stored
  in `~/.vmf/runs/<name>.conf`, which `just ssh` reads; `just list`
  shows it.
- One-shot commands are the contract for microVMs: `just ssh <name> cmd`
  gives docker-exec semantics (live process space, exit code, scp -O
  works — it is exec-based). The derived layer also symlinks every
  busybox applet into `/vmf/bin` and appends that directory to PATH, so
  even distroless images run plain `ls`, `grep`, `ps` etc. (real image
  binaries keep priority). Interactive PTY shells are impossible
  inside libkrun microVMs: opening a pts slave device returns EIO while
  the master is held (verified with an in-guest probe: `open(/dev/ptmx)`
  and `TIOCGPTN` succeed, `open(/dev/pts/N)` fails). `just ssh` strips
  `-t` style flags with a notice. For interactive shells, boot a qemu
  box (`just boot <lab>`), which runs real sshd with a full PTY.
- `--no-ssh` boots the pinned image as-is: no derive, no ssh, entrypoint
  straight through krunvm (krunvm then mangles quoted args — the ssh
  path passes argv through files, so quoting is safe there).

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
