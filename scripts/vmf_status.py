#!/usr/bin/env python3
# vmf_status.py — the one-line status writer.
#
# One run owns one terminal line: <name> <stage> <detail> t+<mm:ss>.
# Modes (VMF_STATUS=tty|log|off; default: stderr-isatty):
#   tty  the line rewrites in place (CR); a final event ends the line
#   log  nothing until the final event — one clean line for pipes/tee
#   off  nothing ever (cron/CI)
# The status file (~/.vmf/runs/.status/<name>) updates in every mode,
# so `just watch` renders one line per run regardless of the mode.
import os
import sys
import time

RUNS = os.environ.get("VMF_RUNS") or \
    os.path.join(os.path.expanduser("~"), ".vmf", "runs")
# Settled lines stay visible to watch for this long.
WATCH_TTL = 600
# The rich board (vmf_ui) owns the terminal while active; the CR line
# goes quiet until the board closes. Status files keep updating — the
# board renders events, the files stay the source of truth.
_QUIET = [False]


def set_quiet(q):
    _QUIET[0] = bool(q)


def status_dir():
    return os.path.join(RUNS, ".status")


def _t0(name):
    try:
        with open(os.path.join(status_dir(), "%s.t0" % name)) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return time.time()


def fmt(name, stage, detail, t0):
    el = max(0, int(time.time() - t0))
    return "%-12s %-8s %-40s t+%d:%02d" % (
        (name or "")[:12], (stage or "")[:8], (detail or "")[:40],
        el // 60, el % 60)


def _mode():
    m = os.environ.get("VMF_STATUS")
    if m in ("tty", "log", "off"):
        return m
    return "tty" if sys.stderr.isatty() else "log"


def _render(text, final):
    m = _mode()
    if _QUIET[0] and not final:
        return
    if m == "off" or (m != "tty" and not final):
        return
    if m == "tty" and not final:
        sys.stderr.write("\r%-100s" % text[:100])
        sys.stderr.flush()
        return
    sys.stderr.write(text[:100] + "\n")
    sys.stderr.flush()


def event(name, stage, detail="", final=False):
    d = status_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    text = fmt(name, stage, detail, _t0(name))
    try:
        tmp = os.path.join(d, "%s.tmp" % name)
        with open(tmp, "w") as f:
            f.write(text + "\n")
        os.replace(tmp, os.path.join(d, name))
    except OSError:
        pass
    _render(text, final)
    return text


def begin(name):
    d = status_dir()
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "%s.t0" % name), "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def clear(name):
    for suffix in ("", ".t0"):
        try:
            os.unlink(os.path.join(status_dir(), name + suffix))
        except OSError:
            pass


def watch(poll=2.0, _sleep=time.sleep):
    d = status_dir()
    try:
        while True:
            rows = []
            now = time.time()
            try:
                names = sorted(os.listdir(d))
            except OSError:
                names = []
            for n in names:
                if n.endswith(".t0"):
                    continue
                p = os.path.join(d, n)
                try:
                    if now - os.path.getmtime(p) > WATCH_TTL:
                        continue
                    with open(p) as f:
                        rows.append(f.read().rstrip("\n"))
                except OSError:
                    continue
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.write("\n".join(rows) + ("\n" if rows else
                                                "(no active runs)\n"))
            sys.stdout.flush()
            _sleep(poll)
    except KeyboardInterrupt:
        return 0


def main(argv):
    if len(argv) >= 3 and argv[1] == "begin":
        begin(argv[2])
        return 0
    if len(argv) >= 4 and argv[1] == "event":
        event(argv[2], argv[3], argv[4] if len(argv) > 4 else "")
        return 0
    if len(argv) >= 4 and argv[1] == "final":
        event(argv[2], argv[3], argv[4] if len(argv) > 4 else "", final=True)
        return 0
    if len(argv) >= 2 and argv[1] == "watch":
        return watch()
    sys.stderr.write(
        "usage: vmf_status.py begin <name> | event <name> <stage> [detail]"
        " | final <name> <stage> [detail] | watch\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
