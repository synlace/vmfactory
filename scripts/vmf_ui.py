#!/usr/bin/env python3
# vmf_ui.py — the rich TTY board for a run (the scout-lanes view).
#
# One run owns one board:
#   <spinner> <name> · <stage>  <chips>                    t+<mm:ss>
#     <spinner> <method>  <state>  <detail>  <cite>  <band>
#   <scan note>
#
# Writers stay plain: status files (vmf_status) and race logs carry the
# same events as one-line records. The board is a terminal-only
# rendering of those events, never a second source of truth. It
# activates only when rich imports, stderr is a TTY, and VMF_UI is not
# "off"; every failure degrades to no board (exactly the old output).
#
# Lane states map 1:1 to what the rows say — the head keeps only the
# counts the rows cannot show (skipped, llm):
#   plan     cyan     a scouted route holds a runnable plan
#   booting  magenta  the candidate's runner chain is up
#   pass     green    the verify arbiter passed the candidate
#   parked   dim      the candidate lost (failed, pruned, filtered)
#   blocked  yellow   the scan found no evidence for the method
import os
import sys
import time

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text
    _RICH = True
except Exception:
    _RICH = False

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# The board speaks the scout's short method vocabulary; runner kinds
# (the fallback identity on replays) map to the same short words. The
# detail line carries the specifics either way.
DISPLAY_METHOD = {"prebuilt_image": "prebuilt", "dockerfile": "build",
                  "source_build": "source", "compose": "compose",
                  "install_script": "pkg"}


def _display(text):
    # Shorten runner-kind words inside arbitrary stage strings
    # ("boot prebuilt_image" -> "boot prebuilt"); scout method words
    # pass through untouched.
    out = text
    for kind, short in DISPLAY_METHOD.items():
        if kind != short:
            out = out.replace(kind, short)
    return out
STATE_STYLE = {"plan": "cyan", "booting": "magenta", "pass": "green",
               "parked": "bright_black", "blocked": "yellow",
               "skipped": "yellow"}
# States that own the head spinner (work is in flight).
ACTIVE_STATES = ("plan", "booting")


def available():
    return _RICH and sys.stderr.isatty() and \
        os.environ.get("VMF_UI", "").strip().lower() != "off"


def _frame():
    return FRAMES[int(time.time() * 8) % len(FRAMES)]


def _clock(t0):
    el = max(0, int(time.time() - t0))
    return "t+%d:%02d" % (el // 60, el % 60)


class _SelfRender:
    # rich's Live re-renders the last object passed to update(); a
    # static Group would freeze the braille frame and the clock. This
    # wrapper re-renders the board on every auto-refresh tick.
    def __init__(self, board):
        self.board = board

    def __rich_console__(self, console, options):
        try:
            yield self.board._render()
        except Exception:
            yield Text("")


class Board:
    # Live lane board. `file` is injectable for tests; the default
    # stderr matches every other writer (say, status events).
    def __init__(self, name, t0=None, file=None):
        self.name = (name or "")[:16]
        self.t0 = t0 or time.time()
        self._stage_text = "scan"
        self.skipped = 0
        self.llm = 0
        self.note_text = ""
        self.lanes = {}
        self.order = []
        self.dead = False
        self._live = None
        if not available() and file is None:
            self.dead = True
            return
        try:
            console = Console(file=file or sys.stderr, highlight=False,
                              soft_wrap=False)
            self._live = Live(console=console, auto_refresh=True,
                              refresh_per_second=6, transient=False)
            self._live.start()
            self._live.update(_SelfRender(self))
        except Exception:
            self._live = None
            self.dead = True

    @property
    def ok(self):
        return not self.dead and self._live is not None

    def _render(self):
        fr = _frame()
        busy = any(L["state"] in ACTIVE_STATES for L in self.lanes.values())
        head = Text()
        head.append(fr if busy else " ", style="magenta")
        head.append(" %s · " % self.name, style="bold")
        head.append(self._stage_text, style="bright_black")
        if self.skipped:
            head.append("  %d skipped" % self.skipped, style="yellow")
        if self.llm:
            head.append("  %d llm" % self.llm, style="bright_black")
        ht = Table.grid(padding=(0, 1))
        ht.add_column(ratio=1, justify="left")
        ht.add_column(width=9, justify="right")
        ht.add_row(head, Text(_clock(self.t0), style="bright_black"))
        lt = Table.grid(padding=(0, 1))
        for w in (1, 10, 9, 40, 24, 9):
            lt.add_column(width=w, justify="left")
        for m in self.order:
            L = self.lanes[m]
            act = L["state"] == "booting"
            st = STATE_STYLE.get(L["state"], "bright_black")
            lt.add_row(
                Text(fr if act else " ", style="magenta"),
                Text("%-10s" % m[:10], style="bold"),
                Text("%-9s" % L["state"][:9], style=st),
                Text("%-40s" % (L["detail"] or "")[:40],
                     style="cyan" if L["state"] == "pass"
                     else "bright_black" if L["state"] == "parked" else ""),
                Text("%-24s" % (L["cite"] or "")[:24], style="bright_black"),
                Text("%-9s" % (L["band"] or "")[:9], style="bright_black"))
        parts = [ht, lt]
        if self.note_text:
            parts.append(Text("  %s" % self.note_text[:90],
                              style="bright_black"))
        return Group(*parts)

    def _push(self):
        if self.ok:
            try:
                self._live.update(_SelfRender(self), refresh=True)
            except Exception:
                self.dead = True
                try:
                    self._live.stop()
                except Exception:
                    pass
                self._live = None

    def stage(self, text, note=""):
        self._stage_text = _display(text or "")[:48]
        if note:
            self.note_text = note
        self._push()

    def lane(self, method, state, detail="", cite="", band=""):
        m = _display((method or "?").strip())[:10]
        if m not in self.lanes:
            self.lanes[m] = {}
            self.order.append(m)
        self.lanes[m].update({"state": (state or "plan")[:9],
                              "detail": _display((detail or ""))[:40],
                              "cite": (cite or "")[:24],
                              "band": (band or "")[:9]})
        self._push()

    def chips(self, skipped=None, llm=None):
        if skipped is not None:
            self.skipped = int(skipped)
        if llm is not None:
            self.llm = int(llm)
        self._push()

    def note(self, text):
        self.note_text = (text or "")[:90]
        self._push()

    def close(self):
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None
