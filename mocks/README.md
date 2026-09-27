# mocks — what the terminal will look like

Prototypes. No VMs boot; nothing here is production code. Each
directory mocks one command, each mock names the real run it replays,
and every NOTES.md ends with a verdict line to fill in before the
mock is deleted.

## Layout

    run/    the race board (`just run`)
            board.py + timeline.py — rich TUI replay of five real
            outcome classes (uv run --with rich python3 mocks/run/board.py)
            rich1.html — board + rolling lane events, static frames
            rich.html — the first board mock, kept for the record
    plan/   `vmf plan` — the deliverable on paper
            plan.py — rich render of the preview (spec table, lane
            panels, blocked lanes, dry-run footer)
            plan1.html — the same three screens as static HTML

## Rules

- The data is real: every scenario replays a measured run or preview
  output, named in the NOTES.
- The mock previews the render, not the machinery: no scripts/ code
  imports from mocks/.
- When a mock's question is answered by the real surface, delete the
  mock and write the verdict line first.
