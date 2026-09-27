# vmfactory

vmfactory is a local-first execution utility that turns heterogeneous software targets into reproducible, isolated virtual-machine executions.

## Status

Frozen 2026-09-27. Errata-only: changes arrive as ADRs, each carrying a measured scenario this pack grades wrong. No measured scenario, no edit.

The first supported target type is a Git repository. Future target types may include ISOs, APKs, OVAs, QCOW2 images, container images, and other executable or bootable artefacts.

The long-term user experience is intentionally small:

```bash
vmf plan https://github.com/example/project
vmf run https://github.com/example/project
```

Behind those commands, vmfactory performs workflows composed from independently meaningful atomic operations.

## Current handover scope

This handover specifies the first vertical slice:

```text
vmf plan <target>
```

The planning workflow:

1. resolves the target;
2. inspects the target into a bounded evidence bundle;
3. derives an `ExecutionSpec` when one is justified by user intent and/or evidence;
4. generates candidate execution approaches per method;
5. validates candidates deterministically;
6. persists the resulting plan and candidate verdicts;
7. streams progress using a transport-independent event envelope.

The goal is not to specify the entire future vmfactory API. The goal is to define enough stable architecture and API contract to begin a clean implementation of the planning MVP.

## Proposed implementation stack

- **Core:** Go
- **CLI:** Go
- **API:** HTTP/JSON
- **Live updates:** Server-Sent Events (SSE) when running through the daemon
- **Local state:** SQLite
- **Large artefacts:** content-addressed filesystem store
- **Future web UI:** TypeScript + React

The CLI is short-lived. The same core workflow/event interfaces should support two execution modes:

- **MVP-1:** in-process CLI execution
- **Later slice:** local daemon + HTTP API + SSE using the same contracts

The daemon should be viewed as a transport/process wrapper around the core, not as the owner of the domain model.

## Core decisions

- **No fabricated intent.** If the user does not pass `--intent`, intent is absent.
- **ExecutionSpec is first-class.** It defines what success means and is hashed as `spec_hash`.
- **Models propose; deterministic mechanisms dispose.**
- **AI is an accelerator, never a runtime dependency.**
- **Candidate outcomes are distinct:** `runnable`, `blocked`, and `transient_error`.
- **Blocked verdicts may be cached; transient failures are not negative-cache entries.**
- **Plan acceptance is semantic, not merely schema-valid.**
- **Workflow YAML is documentation for v1, not a runtime DAG engine.**

## Repository handover files

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system purpose, concepts, decisions, cache semantics, and planning flow.
- [`api/openapi.yaml`](api/openapi.yaml) — API contract for the planning MVP.
- [`api/schemas/`](api/schemas/) — human-readable schema notes/examples.
- [`workflows/plan-v1.yaml`](workflows/plan-v1.yaml) — documentary workflow definition.

## Planning acceptance scenarios

The first implementation should be judged against four explicit scenarios:

1. **Minimal viable CLI target** — a repository with a straightforward CLI entry point.
2. **Web-intent target** — a target where explicit user intent should produce a web deliverable spec.
3. **Blocked sidecar candidate** — a candidate that looks superficially viable but is rejected by deterministic validation.
4. **Cached replay** — the same evidence/spec pair reuses cached candidate verdicts and records zero fresh model calls for replayed results.

These scenarios are more important than broad schema coverage.

## Core principle

The API exposes meaningful operations and durable resources. A workflow composes those operations.

`run` and `plan` should not become large monolithic code paths. They are compositions over reusable primitives.


## Port-completion requirements

The planning MVP is not considered port-complete until the Go implementation also has:

- method-specific CandidateSpec schemas;
- deterministic clamp rules equivalent to the measured Python planner behaviour;
- documented CLI exit codes;
- optional bounded external enrichment with provenance and safe degradation;
- pinned acceptance fixtures with expected verdicts.

These are port-spec requirements, not new architectural scope.


## CLI contract

See [`CLI.md`](CLI.md) for the normative command-line and exit-code contract.
