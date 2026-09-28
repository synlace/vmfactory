# ADR-0002: the event contract ships before the daemon

## Status

Accepted (2026-09-27).

## Context

The pack (README, ARCHITECTURE §3/§9) already decouples MVP-1 in-process
execution from the daemon transport. The port bootstrap must make that
concrete: the envelope and emitter are the first code, the transport is a
stub.

## Decision

The bootstrap commit ships `internal/events` (append-only, monotonically
sequenced envelope with candidate-level data and generic metrics) and
`internal/domain` types matching the openapi contract. `internal/api` and
`internal/daemon` exist as stubs so the dependency direction exists before the
transport does. The same emitter serves the CLI renderer now and SSE later;
no envelope field may encode transport shape.

## Consequences

- The CLI renderer grades rows by consuming the emitter, exactly as a future
  web UI would.
- Daemon/SSE work cannot change the envelope without an ADR.
