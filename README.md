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
just run --keep image                              # keep the VM for reuse
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
  prefixing the guest command with `/usr/bin/env` — needs an explicit
  command; krunvm starts guests with a clean environment and does not read
  the image's default command, so the script resolves ENTRYPOINT/CMD from
  the OCI config blob (skopeo) when no command is given.
- Privileged ports fail rootless when unmapped: a guest bind with no `-p`
  mapping translates to a direct host bind under your uid, so binding
  below 1024 fails (`listen() ... Permission denied`). With `-p 8080:80`
  the host side binds 8080 (unprivileged OK) and the guest bind is
  virtualized by TSI — mapped ports work even for guest port 80.
- Known krunvm 0.2.4 quirk: guest commands containing shell quotes get
  mangled in transit; keep commands quote-free (use `env` style).

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
