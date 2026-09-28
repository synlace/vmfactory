# ADR-0001: the recorded baseline states measured facts, not pack aspirations

## Status

Accepted (2026-09-27). First use of the freeze errata discipline.

## Context

The recorded baseline row 1 (mvt, minimal CLI target) was first written with
`Expected ExecutionSpec: deliverable: cli`. The pack's Scenario A (ARCHITECTURE
§17) reads: "A simple CLI-oriented repository produces a CLI ExecutionSpec and
at least one runnable candidate."

The measured reference behaviour differs. The Python reference at commit
`607f9f7`, PROMPT_V 14, run live at the freeze commit (`0f751be`), produced:

- no ExecutionSpec (no intent; the spec row printed `(none — pass --intent)`);
- three runnable candidates (dockerfile, pkg, source), each carrying a
  `cmd:mvt` check naming the tool binary;
- prebuilt blocked (`no official image ref in evidence`);
- exit 0.

The pack's derive rule is explicit elsewhere: "If the user does not pass
intent, intent is absent" and "the system must not invent one" (§6.3). A cli
spec with no intent would fabricate a deliverable contract — the measured
surface correctly wrote nothing.

## Decision

1. Row 1 is corrected to the measured fact: `none` spec, `(none)` row,
   verdicts as measured.
2. The baseline's authority rule is restated: expected verdicts are recorded
   reference facts, not aspirations. When a pack expectation and a measurement
   disagree, the row states the measurement and cites the ADR.
3. Pack errata proposed (not yet applied, errata channel): Scenario A should
   read that a minimal CLI repository produces a CLI deliverable *outcome* —
   either an explicit `cli` spec when the evidence or intent justifies one, or
   the honest no-spec row with `cmd`-grade candidates. Scenario A as written
   grades a correct port wrong.

## Consequences

- The Go port grades row 1 against the corrected row: no spec, `cmd`-grade
  runnables, prebuilt blocked, exit 0.
- Any future baseline correction follows this pattern: measurement first, ADR
  cited in the row, pack errata proposed separately.
