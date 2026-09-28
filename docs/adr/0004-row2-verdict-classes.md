# ADR-0004: row 2 verdicts grade plan artifacts, not boot-verify machinery

## Status

Accepted (2026-09-28). Errata to the recorded baseline row 2, per the
ADR-0001 authority rule.

## Context

The recorded row 2 (paperclip, explicit web intent) first wrote the expected
verdict as: "at least one runnable web candidate (`paperclipai … as
paperclip`, `tcp:3100` + `probe:/ → 2xx`)". Grading the row against the
pinned reference (`0f14d261233c`, gen `88c87a8424db`, warm cache, zero LLM)
shows the reproducible artifact state differs:

- the runnable candidate is the `build` lane (kind `dockerfile`) carrying
  `ports: [3100]` and the derived env — no `direct` command, no own checks
  (the fanout's build clamp returns no checks; see `vmf_plan.py`
  `clamp_method`);
- `tcp:3100` and the probe floor are the boot verify's machinery: the
  verify derives tcp from the plan's ports and fires a floor probe
  (`probe:/ → status < 400`) on the first port whenever the plan declares
  no probe of its own (`vmf_verify.py` check collection). The plan artifact
  itself carries none of it;
- the run-line wording `paperclipai … as paperclip` was an earlier plan
  roll's shape. The gen slots are single per method (`plan-<m>.json`), so a
  later same-spec roll overwrites the slot; the cached plan is the latest
  roll and carries no command. The wording is not reproducible from the
  artifact;
- compose is blocked with reason `no compose file in repo root` (the row
  first cited `no standalone compose file`, which is row 3's DVWA reason).

## Decision

1. Row 2's verdict class is corrected to the plan-level fact: at least one
   runnable web candidate whose `ports` include the serve port (3100); the
   boot verify floors `tcp:<port>` + `probe:/` + hold on that surface.
2. The compose blocked wording is corrected to `no compose file in repo
   root` (the fixture's tolerant `reason_contains: compose` already holds).
3. The grading rule is restated: verdicts grade what the plan artifacts
   record. Verify-time machinery (floors, holds, floor probes) is graded by
   the race rows, not by plan-file assertions.

## Consequences

- The reference grader asserts `runnable_serves_port` (ports-based) for row
  2 and passes at zero LLM spend against the pinned gen dir.
- The Go port implements the same split: the planner records ports and own
  checks; the verify derives the floor. A port that claims serve checks
  inside the plan file is over-claiming what the reference does.
