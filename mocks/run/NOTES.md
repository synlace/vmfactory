# PROTOTYPE — mocks/run

The question: what does `just run` actually look like, step by step,
for each outcome class — without booting VMs?

## Run

    uv run --with rich python3 mocks/run/board.py

Keys: `[1-5]` scenario · `[space]` step · `[a]` auto-play · `[r]`
restart · `[q]` quit. `↑`/`↓` (or `j`/`k`) select a lane; the events
pane rolls and filters to that lane's lines ("all" is the top of the
cycle; the selected row wears a `>` cursor). Non-interactive:
`--demo N [lane]`.

## Scenarios (all from real runs of 2026-09-23..25)

1. **cyberchef** — service target, prebuilt lane wins, target
   `tcp://127.0.0.1:36485`.
2. **mvt (current)** — CLI tool: keep-alive plan (`sleep 100000000`),
   `cmd:mvt` reports not-installed while pip installs, then passes;
   target `cli://mvt`; source lane parks at its 420s deadline.
3. **mvt (this morning)** — honest fail: the polluted base ref
   (`fat-base:1 <timestamp>`) died at derive, runner exit 125, no
   winner, exit 1.
4. **ghost** — verify-fail repair loop: OOM evidence raises the memory
   floor to 5120, the revised plan reboots and passes.
5. **mvt (the old shape)** — the CLI ran as the app, the VM tore down
   at boot, runner exit 3, "no working service".

## Finding

The board only moves on stage and verdict events. The whole verify
window — the interesting part, checks failing and passing — is a
silent stretch on the board; the check lines live in the runner logs
(`~/.vmf/runs/race-logs/<cand>.log`). The mock shows them as `log`
prefixed event lines to make that explicit.

## Verdict

(fill in before deleting: does the mock match what you see, and is any
scenario's shape wrong?)
