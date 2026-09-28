# ADR-0005: not-runnable is the class; the compose disposition is
# checkout-state-dependent

## Status

Accepted (2026-09-28). Errata to the recorded baseline row 2, per the
ADR-0001 authority rule.

## Context

Grading the Go port against the pinned row 2 (paperclip at
`0f14d261233c`) exposed a provenance subtlety in the recorded compose
verdict. The recorded row says compose `blocked` with `no compose file
in repo root` (a cached verdict in gen `88c87a8424db`, written
2026-09-27 14:45). The pinned checkout carries **no compose file at any
depth**.

The resolution: the reference's winner key folds the bundle plus
root-level compose files only; the compose lane's prefilter also scans
the first two directory levels (`scan()`), which is **not** part of the
key. The recorded day's checkout had a nested compose file —
scan-visible, key-invisible — so the compose lane ran and the model
blocked it. The pinned tree has none, so the port's prefilter skips the
lane honestly (`no compose file`). Same key, different lane
dispositions: the recorded blocked verdict was checkout-state-dependent.

## Decision

1. The grading class for a lane that cannot run is **not runnable**:
   a durable blocked verdict with the recorded reason, **or** an
   honest prefilter skip. Both keep the lane off the runnable list;
   both are correct.
2. The port mirrors the reference exactly: the scan feeds the lane
   prefilter only and stays out of the content key (the reference's
   own comment: a solved fast-moving repo replays across commits that
   change nothing root-level).
3. Row 2's compose expectation grades the not-runnable class; the
   fixture's `reason_contains` applies when the lane blocked.

## Consequences

- The port grades row 2 without a compose model call when the pinned
  tree has no compose file — cheaper and equally honest.
- A future key change that folds scan results into the content hash is
  a deliberate cache-invalidation decision and needs its own ADR.