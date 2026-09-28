# ADR-0003: the model seam ports as a contract over the measured bash scripts

## Status

Accepted (2026-09-27).

## Context

The measured Python planner's LLM seam is two bash scripts (`scripts/llm.sh`,
`scripts/context7.sh`) with ported degrade behaviour: bounded calls, fallback
spec, base approaches, transient failures never cached, junk enrichment
dropped. The pack (§8) requires "bounded calls" and "accelerator, never a
dependency".

## Decision

`internal/model` calls the existing bash seam first (exec the scripts), so the
port inherits the measured transport and degrade paths with zero new
transport code. The seam contract in Go: one bounded call per role, structured
output only, deterministic clamps after every call, fallback paths exercised
by tests with forced transport failure. A native Go transport may replace the
exec later; the contract is the ADR's substance, not the exec.

## Consequences

- Parity with the reference planner's model behaviour is testable without
  reimplementing providers.
- A seam outage degrades exactly as the reference degrades.
- Replacing the transport requires no domain change (the seam contract is the
  boundary).
