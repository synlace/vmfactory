#!/usr/bin/env python3
# PROTOTYPE — mocks/plan/plan.py
#
# The question this mock answers: "what would `vmf plan` show — the
# deliverable on paper, before anything boots?" The data is the real
# preview output of 2026-09-26/27 (paperclip with the web intent; mvt
# with none), re-rendered the way the preview would look once its
# plain writer moves onto rich: a spec table, one panel per plan lane,
# blocked lanes dimmed, the dry-run footer.
#
# The contract the real renderer must keep: every rendered line traces
# to a plan file or the spec; the floor/model split stays visible;
# the footer never claims anything booted.
#
# Run:    uv run --with rich python3 mocks/plan/plan.py [1-3] [--lane N]
# JSON:   uv run --with rich python3 mocks/plan/plan.py 1 --json
import json
import sys

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

W = 102

# ---------------------------------------------------------------------------
# 1 · paperclip · web intent — the measured preview of 2026-09-26
PAPERCLIP = {
    "name": "paperclip",
    "title": "web intent · build + pkg serve · no keep-alive survives",
    "argv": ["just plan https://github.com/paperclipai/paperclip",
             "    --intent \"Run paperclip's web server on port 3100,"
             " authenticated mode\""],
    "found": ["Dockerfile", "package.json", ".env.example", "README.md"],
    "spec": {"deliverable": "web", "src": "intent + scout",
             "serve": "http 0.0.0.0:3100 · path /",
             "auth": "required · authenticated mode per user intent",
             "env": [], "user": "non-root", "hold": 25},
    "lanes": [
        {"lane": 1, "method": "build", "kind": "dockerfile",
         "cost": "medium",
         "env": "HOST=0.0.0.0 · PORT=3100 · PAPERCLIP_HOME=/paperclip "
                "· PAPERCLIP_OPEN_ON_LISTEN=false · "
                "PAPERCLIPAI_VERSION=latest · BETTER_AUTH_SECRET="
                "<required secret, set at launch>",
         "checks": ["tcp:3100"], "have": (1, 1),
         "floor": "tcp + hold", "adds": "—", "notes": ""},
        {"lane": 2, "method": "direct", "kind": "install_script",
         "cost": "slow",
         "install": [
             "apt-get update && apt-get install -y "
             "--no-install-recommends ca-certificates curl gnupg",
             "mkdir -p /etc/apt/keyrings && curl -fsSL https://deb."
             "nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg "
             "--dearmor -o /etc/apt/keyrings/nodesource.gpg && echo "
             "'deb [signed-by=/etc/apt/keyrings/nodesource.gpg] "
             "https://deb.nodesource.com/node_24.x nodistro main' > "
             "/etc/apt/sources.list.d/nodesource.list && apt-get "
             "update && apt-get install -y --no-install-recommends "
             "nodejs",
             "groupadd --gid 10001 paperclip && useradd --create-home "
             "--shell /bin/bash --uid 10001 --gid 10001 paperclip && "
             "mkdir -p /paperclip /home/paperclip/workspace && chown "
             "-R paperclip:paperclip /paperclip /home/paperclip",
         ],
         "run": ["npx --yes paperclipai@latest onboard --yes --bind"],
         "user": "paperclip",
         "env": "PAPERCLIP_HOME=/paperclip · "
                "PAPERCLIP_OPEN_ON_LISTEN=false · HOST=0.0.0.0 · "
                "PORT=3100 · HOME=/home/paperclip · "
                "NPM_CONFIG_UPDATE_NOTIFIER=false",
         "checks": ["tcp:3100", "probe:/ → 2xx", "cmd:curl"],
         "have": (3, 3), "floor": "tcp + hold", "adds": "cmd · probe",
         "notes": "onboard --yes --bind lan is the unattended init; "
                  "no default credentials evidenced"},
    ],
    "blocked": [("compose", "no complete compose file in evidence"),
                ("prebuilt",
                 "no official container image ref in evidence")],
}

# 2 · mvt · no intent — the CLI class; the spec row says so
MVT = {
    "name": "mvt",
    "title": "no intent · spec row names it · CLI keep-alive plans",
    "argv": ["just plan https://github.com/mvt-project/mvt"],
    "found": ["package.json", "pyproject.toml", "Makefile", "README.md"],
    "spec": None,
    "lanes": [
        {"lane": 1, "method": "direct", "kind": "install_script",
         "cost": "slow",
         "install": ["pip install mvt"],
         "run": ["sleep 100000000"], "keep": True,
         "env": "", "checks": ["cmd:mvt"], "have": (1, 1),
         "floor": "hold", "adds": "cmd",
         "notes": "CLI tool: keep-alive so the VM survives the verify"},
        {"lane": 2, "method": "direct", "kind": "source_build",
         "cost": "slowest",
         "base": "python:3.12-slim",
         "install": ["git clone --depth 1 <repo> /src",
                     "pip install /src"],
         "run": ["sleep 100000000"], "keep": True,
         "env": "", "checks": ["cmd:mvt"], "have": (1, 1),
         "floor": "hold", "adds": "cmd", "notes": ""},
    ],
    "blocked": [("compose", "no compose file"),
                ("prebuilt", "no official image"),
                ("build", "no root Dockerfile")],
}

# 3 · paperclip · --spec override — the human correction point
OVERRIDE = dict(PAPERCLIP, **{
    "name": "paperclip (--spec)",
    "title": "the user corrects the contract; plans regenerate from it",
    "argv": ["just plan https://github.com/paperclipai/paperclip",
             "    --spec '{\"deliverable\": \"web\","
             " \"serve\": {\"port\": 3100}}'"],
    "spec": {"deliverable": "web", "src": "user (--spec override)",
             "serve": "http 0.0.0.0:3100 · path /",
             "auth": None, "env": [], "user": None, "hold": 25},
    "lanes": [PAPERCLIP["lanes"][1]],
    "blocked": [("compose", "no complete compose file in evidence"),
                ("prebuilt",
                 "no official container image ref in evidence")],
})

SCENARIOS = [PAPERCLIP, MVT, OVERRIDE]


def _chunks(text, cap):
    # Word-wrap one value into display chunks.
    out, cur = [], []
    for w in str(text).split():
        if cur and len(" ".join(cur)) + 1 + len(w) > cap:
            out.append(" ".join(cur))
            cur = []
        cur.append(w)
    if cur:
        out.append(" ".join(cur))
    return out


def _kv(label, text, style="", pad=12):
    # "key      value" rows; the value word-wraps, continuation rows
    # carry a blank label. The cap leaves room for the panel borders
    # and the pad, so no row ever overflows its frame.
    rows = []
    cap = W - 4 - pad - 6
    for i, chunk in enumerate(_chunks(text, cap) or [""]):
        t = Text("  ")
        t.append("%-*s " % (pad, label if not i else ""),
                 style="bright_black")
        t.append(chunk, style=style)
        rows.append(t)
    return rows


def _lane_panel(l):
    pad = 12
    rows = []
    def row(label, value, style=""):
        if value:
            rows.extend(_kv(label, value, style, pad=pad))
    row("image", l.get("image"), "cyan")
    row("compose", l.get("compose"), "cyan")
    row("base", l.get("base"), "cyan")
    inst = l.get("install") or []
    for i, c in enumerate(inst[:3]):
        row("install" if not i else "", c)
    if len(inst) > 3:
        row("", "… +%d more" % (len(inst) - 3), "bright_black")
    if l.get("run"):
        t = Text("  ")
        t.append("%-*s " % (pad, "run"), style="bright_black")
        t.append(" ".join(l["run"]))
        if l.get("keep"):
            t.append("    keep-alive", style="yellow")
        if l.get("user"):
            t.append("    as %s" % l["user"], style="magenta")
        rows.append(t)
    row("env", l.get("env"), "cyan")
    ck = Text("  ")
    ck.append("%-*s " % (pad, "checks"), style="bright_black")
    for i, c in enumerate(l["checks"]):
        if i:
            ck.append(" · ", style="bright_black")
        ck.append(c, style="green")
    ck.append(" · hold %ss" % 25, style="bright_black")
    rows.append(ck)
    v = Text("  ")
    v.append("%-*s " % (pad, "verdict"), style="bright_black")
    v.append("winner needs %d/%d checks" % l["have"], style="bold")
    v.append(" · floor: %s" % l["floor"], style="bright_black")
    v.append(" · model adds: %s" % l["adds"], style="bright_black")
    rows.append(v)
    row("notes", l.get("notes"), "bright_black")
    return Panel(Group(*rows),
                 title="plan %d · %s · %s · %s"
                       % (l["lane"], l["method"], l["kind"], l["cost"]),
                 border_style="dim", padding=(0, 1))


def render(cons, sc, lane=None, as_json=False):
    if as_json:
        cons.print(json.dumps(
            {"argv": sc["argv"], "spec": sc["spec"],
             "approaches": sc["lanes"], "blocked": sc["blocked"]},
            indent=2))
        return
    cons.print()
    head = Text("  ")
    head.append("vmf plan", style="bold")
    for i, a in enumerate(sc["argv"]):
        head.append("\n    " + a if i else "  " + a,
                    style="cyan" if i else "bold")
    cons.print(head)
    cons.print()
    cons.print(Text("  scout   " + " · ".join(sc["found"]),
                    style="bright_black"))
    s = sc["spec"]
    if s:
        for t in _kv("deliverable", s["deliverable"], "bold green"):
            cons.print(t)
        tail = Text("               ")
        tail.append("← %s " % s["src"], style="bright_black")
        tail.append("(--spec to override)", style="bright_black")
        cons.print(tail)
        if s.get("serve"):
            for t in _kv("serve", s["serve"], "cyan"):
                cons.print(t)
        if s.get("auth"):
            for t in _kv("auth", s["auth"], "yellow"):
                cons.print(t)
        if s.get("env"):
            for t in _kv("env", " · ".join(s["env"]), "cyan"):
                cons.print(t)
        if s.get("user"):
            for t in _kv("user", s["user"], "magenta"):
                cons.print(t)
        for t in _kv("hold", "%ss" % s.get("hold", 25)):
            cons.print(t)
    else:
        cons.print(Text("  spec    (none — pass --intent to declare "
                        "the target)", style="yellow"))
    cons.print()
    lanes = sc["lanes"]
    if lane is None:
        t = Table(box=None, pad_edge=False, padding=(0, 1),
                  show_header=False)
        for width in (9, 10, 8, 14, 7, 6):
            t.add_column(width=width)
        for l in lanes:
            t.add_row("lane %d" % l["lane"], l["method"], l["cost"],
                      l["kind"], "%d chk" % len(l["checks"]),
                      "%d/%d" % l["have"])
        cons.print(t)
        for m, why in sc["blocked"]:
            row = Text("        ✗ ")
            row.append("%-9s " % m, style="red")
            row.append(why, style="bright_black")
            cons.print(row)
        cons.print()
    for l in lanes:
        if lane is not None and l["lane"] != lane:
            continue
        cons.print(_lane_panel(l))
    foot = Text("  ")
    foot.append("dry run — nothing booted", style="bold yellow")
    foot.append("  ·  `just run <src> --intent …` executes",
                style="bright_black")
    cons.print(foot)
    cons.print()


def main():
    cons = Console(width=W, soft_wrap=False, highlight=False)
    n, lane, as_json = 1, None, False
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("1", "2", "3"):
            n = int(a)
        elif a == "--lane" and i + 1 < len(argv):
            lane = int(argv[i + 1])
            i += 1
        elif a == "--json":
            as_json = True
        elif a in ("-h", "--help"):
            print("usage: plan.py [1-3] [--lane N] [--json]")
            return
        i += 1
    sc = SCENARIOS[min(max(n, 1), len(SCENARIOS)) - 1]
    render(cons, sc, lane=lane, as_json=as_json)
    if not sys.stdin.isatty():
        print("scenario %d (%s)" % (n, sc["title"]))


if __name__ == "__main__":
    main()
