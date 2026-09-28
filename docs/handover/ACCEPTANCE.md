# Planning MVP acceptance scenarios

These scenarios define the behavioural bar for the first implementation.

The fixture names below are the expected first grading targets for the Go port.

Before implementation is declared complete, each fixture MUST be recorded with:

```text
repository URL
commit SHA
invocation
expected ExecutionSpec
expected candidate verdicts
expected exit code
```

Do not grade against a moving branch tip.

## Recorded baseline (2026-09-27)

The rows below record what the Python reference actually did (2026-09-27, at
commit 607f9f7, PROMPT_V 14). Each row grades the Go `plan` surface against the
measured reference behaviour. Expected verdicts are the recorded reference
facts, not aspirations.

| # | Scenario | Repository | SHA | Invocation | Expected ExecutionSpec | Expected candidate verdicts | Exit |
|---|----------|------------|-----|------------|------------------------|-----------------------------|------|
| 1 | minimal CLI target | https://github.com/mvt-project/mvt | 9c3db579ee73 | `just plan <repo>` | **none** (no intent; the spec row says `(none — pass --intent)`; nothing fabricated — corrected from an aspirational `cli`, see ADR-0001) | at least one runnable `native`/`dockerfile` candidate with a `cmd` check naming the tool binary; keep-alive commands tagged but never counted as serving; prebuilt blocked (no official image ref) | 0 |
| 2 | explicit web intent | https://github.com/paperclipai/paperclip | 0f14d261233c | `just plan <repo> --intent "Run paperclip's web server on port 3100, authenticated mode"` | `deliverable: web`, serve `http 0.0.0.0:3100 path /`, auth required, `user: non-root`, provenance `intent + scout` | at least one runnable web candidate whose `ports` include the serve port (build lane, kind `dockerfile`, no own checks — see ADR-0004); the boot verify floors `tcp:3100` + `probe:/ → 2xx` + hold on that surface; keep-alive candidates blocked (`plan ignores the web target`); prebuilt blocked (no official image ref); compose not runnable (`no compose file in repo root` when the checkout carries a nested compose file, honest prefilter skip when the pinned tree has none — ADR-0005) | 0 |
| 3 | blocked sidecar | https://github.com/digininja/DVWA | b496a5d3de6b | `just plan <repo>` | no spec (no intent; the spec row says `(none — pass --intent)`) | prebuilt `blocked` with durable reason `official image needs a MariaDB sidecar` (cacheable); compose `blocked` `no standalone compose file`; `dockerfile` and `native` runnable; source skipped | 0 |
| 4 | cached replay | https://github.com/digininja/DVWA | b496a5d3de6b | repeat of row 3 | identical spec hashes | plan provenance `mixed` or `cached_replay`; replayed candidates `cache_hit: true`, `0` fresh model calls; a prior transient (compose `bad output shape`) retried, then cached as a durable `blocked` | 0 |

Recording rule for the Go port: before completion is declared, the port must
reproduce these rows (same invocation, same SHA or a re-pinned commit, same
expected ExecutionSpec fields, same verdict classes, same exit code) through
its own grading harness.

## 1. mvt — minimal CLI target

Fixture:

```text
mvt
```

Expected:

- target resolves;
- bounded inspection is produced;
- no fabricated user intent appears;
- ExecutionSpec resolves to a CLI deliverable;
- at least one runnable candidate is produced;
- the candidate includes a command/exec-style verification check;
- plan is persisted;
- CLI exits `0`.

## 2. paperclip — explicit web intent

Fixture:

```text
paperclip
```

Invocation concept:

```bash
vmf plan <paperclip-repo> --intent "serve the web application"
```

Expected:

- intent is recorded as explicit user input;
- ExecutionSpec is `deliverable: web`;
- serve contract resolves to port `3100`;
- at least one runnable web candidate exists;
- a keep-alive-only candidate is not accepted as satisfying the web deliverable;
- CLI exits `0`.

## 3. DVWA prebuilt — blocked sidecar candidate

Fixture:

```text
DVWA
```

Expected:

- prebuilt/sidecar-style candidate is generated or evaluated;
- deterministic validation rejects the sidecar candidate;
- candidate verdict is `blocked`;
- the block reason is persisted;
- blocked verdict is cacheable;
- the overall plan may still succeed if another candidate is runnable.

The expected qualitative block reason is:

```text
sidecar
```

meaning the candidate does not itself satisfy the declared deliverable.

## 4. DVWA replay — cached replay

Repeat the DVWA planning request without changing target evidence or ExecutionSpec.

Expected:

- target/inspection/spec hashes match previous values;
- per-method cache entries are reused;
- plan provenance is `cached_replay` or `mixed` as appropriate;
- replayed candidate provenance records `cache_hit: true`;
- replayed entries make `0` fresh LLM calls;
- transient failures from a prior run are retried rather than replayed as permanent negatives.

## Exit-code assertions

Use the normative table in [`CLI.md`](CLI.md).
