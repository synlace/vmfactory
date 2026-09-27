#!/usr/bin/env python3
# PROTOTYPE — mocks/run/board.py
#
# The TUI shell over mocks/run/timeline.py (the pure scenario data).
# It replays the race board (scripts/vmf_ui.py layout, same column
# widths and state colors) and the race's say() event stream for five
# real outcome classes. No VMs boot; nothing here is production code.
#
# Arrow keys (or j/k) select a lane; the events pane rolls and filters
# to that lane's lines. The top of the cycle is "all".
#
# Run:    uv run --with rich python3 mocks/run/board.py
# Demo:   uv run --with rich python3 mocks/run/board.py --demo 2 [lane]
import sys
import time

from rich.console import Console
from rich.text import Text

import timeline

STATE_STYLE = {"plan": "cyan", "booting": "magenta", "promoting": "magenta",
               "pass": "green", "parked": "bright_black",
               "blocked": "yellow", "skipped": "yellow"}
ACTIVE_STATES = ("plan", "booting", "promoting")
W = (2, 10, 9, 40, 24, 9)
KEYS = "[↑/↓] lane  [space] step  [a] auto  [r] restart  " \
    "[1-5] scenario  [q] quit"


def render(cons, sc, i, frame, sel=None, say_tail=8):
    step, done = timeline.frame_at(sc, i)
    cons.clear()
    fr = timeline.FRAMES[frame % len(timeline.FRAMES)]
    busy = any(l[1] in ACTIVE_STATES for l in step["lanes"])
    head = Text("  ")
    head.append((fr if busy else " ") + " ", style="magenta")
    head.append("%s · " % sc["name"][:16], style="bold")
    head.append(step["stage"][:48], style="bright_black")
    chips = step["chips"] or (0, 0)
    if chips[0]:
        head.append("  %d skipped" % chips[0], style="yellow")
    if chips[1]:
        head.append("  %d llm" % chips[1], style="bright_black")
    head.append(" " * max(1, 102 - head.cell_len))
    head.append("%9s" % step["t"], style="bright_black")
    cons.print(head)
    for m, state, detail, cite, band in step["lanes"]:
        st = STATE_STYLE.get(state, "bright_black")
        act = state in ACTIVE_STATES
        line = Text("  > " if sel == m else "    ")
        line.append((fr + " ") if act else "  ", style="magenta")
        line.append("%-*s " % (W[1], m[:W[1]]), style="bold")
        line.append("%-*s " % (W[2], state[:W[2]]), style=st)
        line.append("%-*s " % (W[3], (detail or "")[:W[3]]),
                    style="cyan" if state == "pass"
                    else "bright_black" if state == "parked" else "")
        line.append("%-*s " % (W[4], (cite or "")[:W[4]]),
                    style="bright_black")
        line.append("%-*s" % (W[5], (band or "")[:W[5]]),
                    style="bright_black")
        cons.print(line)
    if step["note"]:
        cons.print(Text("    %s" % step["note"][:86],
                        style="bright_black"))
    cons.print()
    events = timeline.events_upto(sc, i)
    if sel:
        events = [e for e in events if e[1] == sel]
        cap = Text("── events · %s " % sel, style="bright_black")
        cap.append("· " if len(cap) < 70 else "")
        cap.append("[↑/↓ lane]", style="dim")
    else:
        cap = Text("── events ", style="bright_black")
        cap.append("[↑/↓ lane]", style="dim")
    cap.append(" " + "─" * max(2, 94 - cap.cell_len),
               style="bright_black")
    cons.print(cap)
    if not events:
        cons.print(Text("  (no events for this lane yet)",
                        style="dim"))
    for t, lane, text in events[-say_tail:]:
        row = Text("  %8s  " % t, style="bright_black")
        if not sel and lane != "*":
            row.append("[%s] " % lane, style="bright_black")
        style = "bright_black"
        if "pass" in text and "FAIL" not in text and "fail" not in text:
            style = "green"
        elif "FAIL" in text or "fail" in text or "Error" in text:
            style = "red"
        row.append(text, style=style)
        cons.print(row)
    if step["final"]:
        verdict, rc = step["final"]
        cons.print()
        cons.print(Text("  %s" % verdict, style="green" if rc == 0
                        else "red"))
    if done:
        cons.print()
        cons.print(Text("  · end of scenario · %s" % sc["title"],
                        style="dim"))
    cons.print()
    cons.print(Text(KEYS, style="dim"))


def demo(cons, n, lane=None):
    sc = timeline.SCENARIOS[n - 1]
    for i in range(len(sc["steps"])):
        render(cons, sc, i, i, sel=lane)
        time.sleep(0.35)
    print("scenario %d (%s%s) replayed; rc=%s"
          % (n, sc["name"], " · lane %s" % lane if lane else "",
             sc["steps"][-1]["final"][1]))


def main():
    cons = Console(width=102, soft_wrap=False, highlight=False)
    if "--demo" in sys.argv:
        k = sys.argv.index("--demo")
        n = int(sys.argv[k + 1]) if len(sys.argv) > k + 1 else 2
        lane = sys.argv[k + 2] if len(sys.argv) > k + 2 else None
        demo(cons, min(max(n, 1), len(timeline.SCENARIOS)), lane)
        return
    if not sys.stdin.isatty():
        print("run inside a terminal: uv run --with rich "
              "python3 mocks/run/board.py  (or --demo N [lane])")
        return
    import termios
    import tty
    cur, i, frame, auto, sel = 1, 0, 0, False, None
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        while True:
            sc = timeline.SCENARIOS[cur - 1]
            render(cons, sc, i, frame, sel=sel)
            if auto:
                time.sleep(0.7)
                _, done = timeline.frame_at(sc, i)
                frame += 1
                if not done:
                    i += 1
                    continue
                auto = False
            ch = sys.stdin.read(1)
            if ch in ("q", "\x03"):
                break
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                ch = {"[A": "up", "[B": "down"}.get(seq, "")
            if ch in "12345":
                cur, i, frame, auto, sel = int(ch), 0, 0, False, None
            elif ch == " ":
                _, done = timeline.frame_at(sc, i)
                frame += 1
                if not done:
                    i += 1
            elif ch == "a":
                auto = not auto
            elif ch == "r":
                i, frame, auto = 0, 0, False
            elif ch in ("up", "down", "j", "k"):
                lanes = [l[0] for l in
                         timeline.frame_at(sc, i)[0]["lanes"]]
                order = [None] + lanes
                pos = order.index(sel) if sel in order else 0
                if ch in ("down", "j"):
                    sel = order[min(pos + 1, len(order) - 1)]
                else:
                    sel = order[max(pos - 1, 0)]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print("scenarios: " + "; ".join("%d %s" % (n + 1, s["title"])
                                    for n, s in
                                    enumerate(timeline.SCENARIOS)))


if __name__ == "__main__":
    main()
