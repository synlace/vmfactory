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
scripts/           generate.py, validate.py, boot.sh, enter.sh, vmf_lib.py,
                   vmf_plan.py (the plan pipeline), vmf_llm.py (the LLM seam),
                   vmf_verify.py (the verify runner)
build/             generated output (gitignored)
```

## Quickstart

The most grounded mode is a repo: the plan comes from the project's
own evidence (README, manifests), not from model priors.

```sh
just run https://github.com/org/repo       # plan -> boot -> verify
just run https://github.com/org/repo --intent "Run 3 instances"
```

A plain image with `--intent` is EXPERIMENTAL — the plan is conjured
from model priors, so version-sensitive apps are safer as a repo, or
as their official image (the propose route now prefers it):

```sh
just run --rm ubuntu --intent "nginx on 1337 and 1338"   # experimental
```

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

## Compose runs from git URLs

`vmf run github.com/org/repo` works without a scheme (gitlab.com and
bitbucket.org too); `https://`, `git@`, and `file://` URLs also work,
and a plain local directory path runs the same pipeline. A URL with
extra path segments selects the project inside the repo
(`github.com/org/repo/apps/upper`; the clone URL stays the repo root).

### Input classification (first line of every run)

Every run names what vmf thinks the input is, from cheap facts only —
URL shape, file magic (ISO 9660 / qcow2 / tar members), directory
layout. No clone, no registry, no model:

```
input: git-url https://github.com/gchq/cyberchef
input: image alpine (assumed)
input: iso /path/win11.iso (6.2 GB, iso "CCCOMA_X64FRE_EN-US_DV9")
input: ambiguous './nginx' (no such path; matches image ref pattern)
       resolve: --as dir|image          ← exit 2, never a silent guess
```

Kinds: git-url, dir, image, image-tar, compose-file, dockerfile, iso,
ova, disk, box, tarball, bundle. Only git-url, dir, and image are
runnable today; the rest refuse with exit 3 ("planned but not built
yet") instead of degrading. `--as KIND` resolves ambiguity explicitly.
An unknown file is a hard error with the supported list — the
classifier never silently downgrades to qemu defaults.

### Evidence profiles

Kinds with requirements (iso, ova, disk, image) resolve a VM profile
through a three-tier ladder: evidence first (Windows `install.wim`
label, OVF-declared RAM/vCPU), then `scripts/formats.yaml` (the
user-tunable table), then — only when both miss — a stated default.
The model may select a table row; it never invents values:

```
profile: windows-install (ram 6144, disk 64G, firmware uefi, display webvnc)
         [evidence: iso label "CCCOMA_X64FRE_EN-US_DV9", 6.2 GB]
profile: default-linux (ram 2048, disk 8G, display webvnc)
         [default (unrecognized iso "MYSTERY_42"); override: --ram/--disk]
```

The plan pipeline lives in `scripts/vmf_plan.py` (plan / variant /
refine / flatten / ports / classify / profile) with file contracts in
`schemas/plan.schema.json` and `schemas/refines.schema.json`; tests in
`tests/` run without qemu or a model
(`uv run --with pyyaml --with jsonschema --with rich python -m unittest discover tests`).

Multi-project repos (one compose per lab) never guess: every candidate
compose file under the root or the first two directory levels gets a
menu (name, services, ports) and `vmf` refuses to boot until you pick
one by path hint, `--project <name-or-path>`, or `--intent "<phrase>"`.

`--intent` sends the menu plus your phrase to a small LLM that returns
a pointer ({project, ref, variant}); the pointer must be one of the
menu entries or the run refuses. `ref` re-clones that branch or tag;
`variant` overrides any `variant` build arg.

## Compose-less repos (gap-filler)

A repo with no compose file anywhere (strix-style: README + a
Dockerfile) runs through the gap-filler. It collects deterministic
evidence (README, Dockerfiles, manifest.yaml, systemd units, package
files), asks `VMF_GAPFILL_MODEL` for a strict-JSON plan, renders the
plan itself (the model never writes YAML or files), and shows the
proposal. The gate is a loop, not a binary: `a` boots the plan, `n`
aborts, and free text is a refinement — the model revises the plan
(capped at 3 turns) and the diff shows what changed. Refined plans
replace the cache for that input; `--yes` skips the loop for
scripting. Approved plans cache under `~/.vmf/generated/<input-hash>/`
— the same repo replays without a model call. Without a key the
gap-filler fails with a clear message; nothing is ever generated
silently.

Spec'ing is grounded: the gap-filler runs in three phases. A draft
call states the plan and names the topics whose CURRENT facts matter;
`scripts/context7.sh` then fetches up-to-date library docs for each
topic (Context7 REST API, `CONTEXT7_API_KEY` optional, `VMF_CONTEXT7`
off to disable); the final plan prompt carries those doc blocks, and
the gate prints the provenance line (`grounding: grounded via
context7: /astral-sh/uv [...], ...`). Package names, install steps,
and prerequisites come from current docs, not model recall. Degraded
runs say `NOT grounded` on the gate; provenance lands in the plan's
`.meta.json`.

Two run shapes come out of the gap-filler:

- `mode: docker` — the compose path (build the repo image, dockerd in
  the VM, compose up). Container-native repos and multi-service apps.
- `mode: direct` — the VM is the sandbox. The plan carries a base
  image, boot-time install commands, and the argv. The host stages the
  repo tar and install script on the per-run inputs; the guest unpacks
  to /workspace, runs the install, then execs the app. No docker,
  fast boot. If the app itself needs a docker daemon at runtime (its
  plan sets `needs_docker`), the static docker bundle is staged too
  and dockerd starts in the VM (vfs storage) before the app.
  `--runtime direct|docker|auto` forces a shape; auto lets the model
  pick from the evidence. Install failures print their last lines on
  the console. The install runs at every boot (the tmpfs overlay
  resets); the approval gate is the supply-chain review point.

Large images: host `mke2fs -d` cannot populate files beyond 2GiB (32-
bit counter in its populate path), so docker archives over 2GiB are
split into <2GiB parts on the host and the guest concatenates them
into `docker load`.

## Intent on a plain image

EXPERIMENTAL: a repo, or the app's official image, carries more
evidence than a phrase. The propose route now prefers the official
container image (host-supplied, digest-pinned) when one exists; source
installs are the fallback.

`--intent` also works without a repo:
`vmf run ubuntu --intent "Latest version of kilocode CLI"`. The same
draft -> context7 -> finalize flow produces a setup plan (install
commands, argv, env, `needs_docker`, `memory_mb`) for the FIXED base
image; the gate reviews it (package names and installer URLs visible,
grounding provenance printed); the plan caches by image+phrase and
sizes the VM memory when the app needs more than 1024 MB. The VM then
boots, runs the install non-interactively (DEBIAN_FRONTEND, stdin
closed), and execs the app — interactive CLIs paint their TUI on the
console in foreground runs, or use `-d` + `vmf ssh`. "Latest" resolves
at install time inside the VM; the image is digest-pinned as usual.
The plan also declares success checks (a probe per HTTP-serving port);
the verify stage below runs them after boot.

## Verify: checks, verdict, self-healing revision

Detached runs with an intent (direct plan) verify themselves after
boot. Every declared guest port gets a deterministic tcp check — a
declared fact, never a model opinion. The plan may also declare probe
checks (HTTP status, and body text when the content proves success)
and exec checks (a command that exits 0 inside the guest). log and rfb
checks print `SKIP` until their runners arrive.

The runner polls until each check passes or the deadline expires
(120s; `VMF_VERIFY_SECS`), then prints a verdict line:
`verdict: 6/6 checks pass`. This closes the "up but broken" gap: a VM
that boots and publishes ports but serves the wrong content fails its
verdict with evidence (expected vs actual status, body head, exit
codes).

A failed verdict triggers repair, cheapest first. When the VM is
alive, the agent fixes the app in place over ssh and rewrites the
spec — no reboot; only a port change reboots. A dead VM falls to the
bounded blind revise (one model turn from the evidence), then a
reboot. The revised plan writes back into the intent cache, so the
same image+phrase replays the fix instead of the failure.

The plan itself is proposed first (one grounded model call), and the
full agent session is the deep fallback — a dedicated plan VM the
agent drives, kept alive with its transcript when a session ends
unresolved, so the next run resumes from real state instead of paying
from zero.

Detached runs are the verify scope: foreground runs hold the console,
and compose-mode runs carry no plan in the rundir, so their verdict
arrives with the orchestrator slice. `VMF_VERIFY=0` skips the stage.

`--intent` also refines a resolved plan when the run has one project
(or a cached gap-fill plan): the phrase is unbounded free text, and it
resolves into a bounded, validated overlay — replicas ("Run 5
instances" scales a service to N), per-service env (`"set env
GREETING=hello on cyberchef"`), and per-service command overrides.
The base plan and its cache stay untouched; the flatten step applies
the overlay deterministically: numbered instances (`cyberchef-2`...),
distinct static IPs, VM ports offset per instance (8080-8084), merged
environment, and the extra ports ride the boot-time hostfwd on both
engines. Anything outside the vocabulary (unknown service, count
outside 2-12) is ignored. An intent that maps to nothing observable
prints `intent: no refinement matched` — never a silent guess.
Interactive runs get the a / n / free-text overlay gate (same loop as
the proposal gates); detached and `--yes` runs apply the validated
overlay as-is.

The compose file is found at the repo root or in a unique subdirectory
(two levels deep). The translation is deterministic (no LLM on the
happy path): services, image|build (context, dockerfile, args), ports
(TCP/UDP), expose, env (mapping and list forms), env_file, command,
entrypoint, depends_on, network aliases. Known gaps (a compose file
relying on them still boots, but those parts are inert): volumes
(named volumes and bind mounts are dropped), custom network isolation
(everything lands on one static-IP project network), healthchecks,
replicas.

Because the guest has no docker embedded DNS (its resolver DNAT needs
iptables, which the module-less microVM kernel does not ship), each
service gets a static IP (172.31.100.10+) on the project network and
every other service resolves names and aliases through generated
`extra_hosts` entries. Container restarts keep their IP.

Compose-declared privileged ports (e.g. 80:80) auto-remap on the host
side when slirp cannot bind (unprivileged port or already taken); the
guest keeps the declared port and `vmf url NAME 80` still resolves.

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

- Scripts share `scripts/vmf_lib.sh`: `vmf_tool`/`vmf_tools`/`vmf_run`
  own the "command missing → nix shell fallback" idiom (never copy it
  per call site), and `vmf_fwd_parse` owns the
  "proto host guest" forward-line contract (2-field lines are legacy
  tcp).
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
