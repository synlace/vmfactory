# CLI contract

This file is the normative source for vmfactory CLI behaviour and exit codes.
Other documents should reference it rather than redefine the table.

## Planning

```bash
vmf plan <target> [--intent <text>] [--spec <path>]
```

`--intent` is optional.

If absent, the planner must not synthesize an intent string.

`--spec` supplies a human-authored ExecutionSpec override. The override is
validated and clamped using the same bounded ExecutionSpec contract as a derived
spec. Where fields are supplied by the override, they take precedence over
derived values.

## Exit codes

```text
0  usable plan produced
1  planning completed but no usable candidate exists
2  usage error or ambiguous/invalid invocation
3  required model capability unavailable or unconfigured
```

These codes are deliberately coarse for shell automation.

## Rendering

The CLI should render the shared event envelope whether execution is:

- in-process; or
- via daemon + SSE.

The renderer should not depend on transport-specific event shapes.
