# PROTOTYPE — mocks/plan

The question: what would `vmf plan` show — the deliverable on paper,
before anything boots — once its plain writer moves onto rich?

## Run

    uv run --with rich python3 mocks/plan/plan.py [1-3] [--lane N]

`1` paperclip with the web intent · `2` mvt with no intent (the CLI
class) · `3` the `--spec` override (the human correction point).
`--lane N` renders one panel only; `--json` dumps the machine shape.
Static render, no key loop: a plan preview is one screen.

## HTML (plan1.html)

Static frames for the three scenarios, then `4 · interactive replay`:
the plan assembling the way mocks/run/rich1.html replays the race
board — clone → read → spec derive → grounding → the four per-method
calls landing as they finish, each lane row flipping from `waiting`
to `plan` or `blocked` the moment its call returns. Replay and lane
buttons (all/prebuilt/compose/build/pkg/spec) plus arrow keys filter
the event log. Data: the real stderr lines of the 2026-09-26 paperclip
preview; clock labels approximate the measured ~3-minute wall.

## Scenarios (all from real preview output of 2026-09-26/27)

1. **paperclip · web intent** — the intent that crowned
   `cli://paperclipai` three runs in a row now names the target in a
   spec block; the build and pkg lanes serve (`paperclipai … as
   paperclip`, `probe:/ → 2xx`), and no keep-alive plan survives the
   clamp ("plan ignores the web target").
2. **mvt · no intent** — the spec row says `(none — pass --intent)`
   instead of guessing; the plans keep the honest keep-alive shape
   (`sleep 100000000 · keep-alive`, `cmd:mvt`).
3. **paperclip · --spec override** — the provenance line reads
   `← user (--spec override)`; plans regenerate from the corrected
   contract.

## Contract the real renderer must keep

- Every rendered line traces to a plan file or the spec. No
  interpretations on screen.
- The floor/model split stays visible on every plan
  (`floor: tcp + hold · model adds: cmd · probe`).
- The footer never claims anything booted: `dry run — nothing
  booted · just run <src> --intent … executes`.

## Shape notes

- Spec block: pad-12 key column; provenance under `deliverable`;
  absent rows are absent lines (no placeholders).
- One rich Panel per plan lane (full width, dim border); values word-
  wrap with a blank-label continuation; `as <user>` in magenta,
  `keep-alive` in yellow, checks green.
- Blocked lanes are `✗ method — why` rows under the lane table.

## Verdict

The real surface shipped (2026-09-27): scripts/vmf_plan.py renders the
mock's shape through rich when stdout is a terminal, and keeps the
plain writer for pipes, tests, and --plain. The mock matched: spec
table, lane table, panel per plan, blocked rows, dry-run footer, and
the floor/model split all aired on a real cached paperclip preview.
Keep the mock until the replay idea (section 4) lands in the real
surface; delete both after that.
