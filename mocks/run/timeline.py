#!/usr/bin/env python3
# PROTOTYPE — mocks/run/timeline.py
#
# The question this mock answers: "what does `just run` actually look
# like, step by step, for each outcome class?" The data below is the
# observed choreography of the race board (scripts/vmf_ui.py) and the
# race's say() event lines, replayed from real runs — no VMs boot.
#
# Pure data + one pure accessor. No I/O. The TUI shell (run.py) renders
# these snapshots; nothing here touches a terminal.
#
# Step fields:
#   t      clock label ("t+0:41")
#   stage  board head stage text
#   lanes  [(method, state, detail, cite, band)] in row order
#   chips  (skipped, llm) head chips, or None to keep the previous
#   note   the board note line, or "" to keep the previous
#   say    plain event lines printed this step (the race's say stream)
#   final  (verdict line, exit rc) — present only on the last step
#   cands  candidate → lane map for event attribution ({"c1": "pkg"})
import re

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Runner-kind words inside event lines map to the board's short method
# words (same table as scripts/vmf_ui.py).
DISPLAY_METHOD = {"prebuilt_image": "prebuilt", "dockerfile": "build",
                  "source_build": "source", "compose": "compose",
                  "install_script": "pkg"}


def _s(t, stage, lanes, say=(), chips=None, note=None, final=None):
    return {"t": t, "stage": stage, "lanes": lanes, "say": list(say),
            "chips": chips, "note": note, "final": final}


# ---------------------------------------------------------------------------
# 1 · service target won by the prebuilt lane (cyberchef, 34 min ago)
SERVICE = {
    "name": "cyberchef",
    "title": "service target · prebuilt lane wins · target tcp://",
    "cands": {"cyberchef-c1": "prebuilt"},
    "steps": [
        _s("t+0:04", "clone", [], ["scout: reading repo + releases"]),
        _s("t+0:09", "scan", [
            ("prebuilt", "plan", "guest: docker run -p 8080:8080",
             "ghcr.io/gchq/cyberchef", "fast/T0"),
            ("compose", "skipped", "no compose file", "", ""),
            ("build", "blocked", "no evidence for the method", "", ""),
            ("pkg", "blocked", "no evidence for the method", "", ""),
        ], ["scout: 3 route(s) before the race; feed stays live"],
            chips=(2, 4)),
        _s("t+0:41", "boot prebuilt", [
            ("prebuilt", "booting", "guest: docker run -p 8080:8080",
             "ghcr.io/gchq/cyberchef", "fast/T0"),
            ("compose", "skipped", "no compose file", "", ""),
            ("build", "blocked", "no evidence for the method", "", ""),
            ("pkg", "blocked", "no evidence for the method", "", ""),
        ], ["1 prebuilt_image .. started (cyberchef-c1)"]),
        _s("t+2:38", "boot prebuilt", [
            ("prebuilt", "booting", "guest: docker run -p 8080:8080",
             "ghcr.io/gchq/cyberchef", "fast/T0"),
            ("compose", "skipped", "no compose file", "", ""),
            ("build", "blocked", "no evidence for the method", "", ""),
            ("pkg", "blocked", "no evidence for the method", "", ""),
        ], ["log cyberchef-c1: verify: 3 checks, deadline 420s",
            "log cyberchef-c1: check tcp:8080 pass · check tcp:8081 pass "
            "· check probe:8080 pass"]),
        _s("t+3:02", "boot prebuilt", [
            ("prebuilt", "pass", "", "ghcr.io/gchq/cyberchef", "fast/T0"),
            ("compose", "skipped", "no compose file", "", ""),
            ("build", "blocked", "no evidence for the method", "", ""),
            ("pkg", "blocked", "no evidence for the method", "", ""),
        ], ["1 prebuilt_image .. pass", "winner 1 prebuilt_image; reaping losers"]),
        _s("t+3:10", "promote · booting canonical", [
            ("prebuilt", "promoting", "canonical · winner drive",
             "ghcr.io/gchq/cyberchef", "fast/T0"),
            ("compose", "skipped", "no compose file", "", ""),
            ("build", "blocked", "no evidence for the method", "", ""),
            ("pkg", "blocked", "no evidence for the method", "", ""),
        ], ["promotion: booting cyberchef (~2-4 min)"]),
        _s("t+5:26", "pass", [
            ("prebuilt", "pass", "tcp://127.0.0.1:36485",
             "ghcr.io/gchq/cyberchef", "fast/T0"),
            ("compose", "skipped", "no compose file", "", ""),
            ("build", "blocked", "no evidence for the method", "", ""),
            ("pkg", "blocked", "no evidence for the method", "", ""),
        ], ["target: tcp://127.0.0.1:36485"],
            final=("cyberchef  pass     tcp://127.0.0.1:36485        t+5:26", 0)),
    ],
}


# ---------------------------------------------------------------------------
# 2 · CLI tool won by the pkg lane (mvt, today — the first CLI win)
CLI = {
    "name": "mvt",
    "title": "CLI tool · keep-alive plan · cmd check ladder · target cli://",
    "cands": {"mvt-c1": "pkg", "mvt-c2": "source"},
    "steps": [
        _s("t+0:04", "clone", [], ["scout: reading repo + releases"]),
        _s("t+0:38", "scan", [
            ("pkg", "plan", "cli: mvt", "README.md Installation:", "fast/T0"),
            ("source", "plan", "cli: mvt", "Makefile install target:",
             "medium/T1"),
        ], ["scout: dropped build .. no ports to verify",
            "scout: 2 route(s) in flight; the scout keeps reading"],
            chips=(1, 1),
            note="skipped · no compose file"),
        _s("t+1:05", "boot install_script", [
            ("pkg", "booting", "sleep 100000000",
             "README.md Installation:", "fast/T0"),
            ("source", "plan", "cli: mvt", "Makefile install target:",
             "medium/T1"),
        ], ["1 install_script .. started (mvt-c1)"]),
        _s("t+1:20", "boot install_script", [
            ("pkg", "booting", "sleep 100000000",
             "README.md Installation:", "fast/T0"),
            ("source", "booting", "uv venv; pip install .",
             "Makefile install target:", "medium/T1"),
        ], ["2 source_build .. started (mvt-c2)"]),
        _s("t+3:30", "boot install_script", [
            ("pkg", "booting", "sleep 100000000",
             "README.md Installation:", "fast/T0"),
            ("source", "booting", "uv venv; pip install .",
             "Makefile install target:", "medium/T1"),
        ], ["log mvt-c1: verify: 1 checks, deadline 420s",
            "log mvt-c1: check cmd:mvt FAIL not installed (command -v exit 127)"]),
        _s("t+9:40", "boot install_script", [
            ("pkg", "booting", "sleep 100000000",
             "README.md Installation:", "fast/T0"),
            ("source", "booting", "uv venv; pip install .",
             "Makefile install target:", "medium/T1"),
        ], ["log mvt-c1: verify: waiting (387s/420s): cmd:mvt",
            "log mvt-c2: verify: waiting (237s/420s): cmd:mvt"]),
        _s("t+11:50", "boot install_script", [
            ("pkg", "booting", "sleep 100000000",
             "README.md Installation:", "fast/T0"),
            ("source", "booting", "uv venv; pip install .",
             "Makefile install target:", "medium/T1"),
        ], ["log mvt-c1: check cmd:mvt pass",
            "log mvt-c2: verdict: 0/1 checks pass; 1 failed",
            "2 source_build .. fail (0/1 checks pass; 1 failed)"]),
        _s("t+12:02", "boot install_script", [
            ("pkg", "pass", "", "README.md Installation:", "fast/T0"),
            ("source", "parked", "0/1 checks pass; 1 failed",
             "Makefile install target:", "medium/T1"),
        ], ["1 install_script .. pass",
            "winner 1 install_script; reaping losers"]),
        _s("t+12:10", "promote · booting canonical", [
            ("pkg", "promoting", "canonical · winner drive",
             "README.md Installation:", "fast/T0"),
            ("source", "parked", "0/1 checks pass; 1 failed",
             "Makefile install target:", "medium/T1"),
        ], ["promotion: booting mvt (~2-4 min)"]),
        _s("t+13:05", "pass", [
            ("pkg", "pass", "cli://mvt", "README.md Installation:",
             "fast/T0"),
            ("source", "parked", "0/1 checks pass; 1 failed",
             "Makefile install target:", "medium/T1"),
        ], ["target: cli://mvt"],
            final=("mvt          pass     cli://mvt                      t+13:05", 0)),
    ],
}


# ---------------------------------------------------------------------------
# 3 · honest fail: the polluted base ref died at derive (mvt, this morning)
FAIL = {
    "name": "mvt",
    "title": "honest fail · runner exit 125 (invalid reference format) · no winner",
    "cands": {"mvt-c1": "pkg", "mvt-c2": "source"},
    "steps": [
        _s("t+0:04", "clone", [], ["scout: reading repo + releases"]),
        _s("t+0:09", "scan", [
            ("pkg", "plan", "mvt version", "README.md Installation:",
             "fast/T0"),
            ("source", "plan", "uv run mvt", "Makefile install target:",
             "medium/T1"),
        ], ["scout: cache replay 2 route(s), 1 skipped, 0 llm call(s)"],
            chips=(1, 1),
            note="skipped · no compose file"),
        _s("t+0:14", "boot install_script", [
            ("pkg", "booting", "mvt version", "README.md Installation:",
             "fast/T0"),
            ("source", "plan", "uv run mvt", "Makefile install target:",
             "medium/T1"),
        ], ["1 install_script .. started (mvt-c1)"]),
        _s("t+0:26", "boot install_script", [
            ("pkg", "booting", "mvt version", "README.md Installation:",
             "fast/T0"),
            ("source", "plan", "uv run mvt", "Makefile install target:",
             "medium/T1"),
        ], ["mvt-c1 last: Error: parsing reference "
            '"localhost/vmf-fat-base:1 2026-09-23…" invalid reference format']),
        _s("t+0:28", "boot", [
            ("pkg", "parked", "fail (runner exit 125)",
             "README.md Installation:", "fast/T0"),
            ("source", "parked", "unverified (plan declares no checks)",
             "Makefile install target:", "medium/T1"),
        ], ["1 install_script .. fail (runner exit 125)"]),
        _s("t+0:29", "fail", [
            ("pkg", "parked", "fail (runner exit 125)",
             "README.md Installation:", "fast/T0"),
            ("source", "parked", "unverified (plan declares no checks)",
             "Makefile install target:", "medium/T1"),
        ], ["no approach produced a working service",
            "rerun with --approach <name|number> to retry one approach"],
            final=("mvt          fail     no working service             t+0:29", 1)),
    ],
}


# ---------------------------------------------------------------------------
# 4 · verify-fail repair loop: OOM evidence raises the memory floor, reboot passes
REPAIR = {
    "name": "ghost",
    "title": "repair loop · OOM evidence · memory floor 5120 · revised plan cached",
    "cands": {"ghost-c1": "pkg"},
    "steps": [
        _s("t+0:04", "clone", [], ["scout: reading repo + releases"]),
        _s("t+0:12", "scan", [
            ("pkg", "plan", "node server.js", "README.md Installation:",
             "fast/T0"),
        ], ["scout: 1 route(s) before the race"], chips=(0, 2)),
        _s("t+0:40", "boot install_script", [
            ("pkg", "booting", "node server.js", "README.md Installation:",
             "fast/T0"),
        ], ["1 install_script .. started (ghost-c1)"]),
        _s("t+3:10", "boot install_script", [
            ("pkg", "booting", "node server.js", "README.md Installation:",
             "fast/T0"),
        ], ["log ghost-c1: check probe:2368 FAIL status 502",
            "log ghost-c1: check app-alive FAIL python was OOM-killed "
            "(anon-rss 3.6GB); plan memory_mb was 4096"]),
        _s("t+3:40", "boot install_script", [
            ("pkg", "booting", "node server.js", "README.md Installation:",
             "fast/T0"),
        ], ["log ghost-c1: verify: revising the plan from check evidence...",
            "log ghost-c1: agent: grounded /tryghost/ghost "
            "[memory requirements]",
            "log ghost-c1: verify: memory floor 5120 "
            "(measured rss 3.6GB + 1024 headroom)",
            "log ghost-c1: verify: revised plan cached (verify_revised)"]),
        _s("t+4:05", "boot install_script", [
            ("pkg", "booting", "node server.js (revised)",
             "README.md Installation:", "fast/T0"),
        ], ["log ghost-c1: verify: rebooting with the revised plan "
            "(memory_mb 5120)"]),
        _s("t+7:20", "boot install_script", [
            ("pkg", "booting", "node server.js (revised)",
             "README.md Installation:", "fast/T0"),
        ], ["log ghost-c1: check probe:2368 pass"]),
        _s("t+7:30", "boot install_script", [
            ("pkg", "pass", "", "README.md Installation:", "fast/T0"),
        ], ["1 install_script .. pass", "winner 1 install_script; reaping losers"]),
        _s("t+9:05", "pass", [
            ("pkg", "pass", "http://192.168.42.181:2368/",
             "README.md Installation:", "fast/T0"),
        ], ["target: http://192.168.42.181:2368/"],
            final=("ghost         pass     http://192.168.42.181:2368/    t+9:05", 0)),
    ],
}


# ---------------------------------------------------------------------------
# 5 · the old CLI death: the plan ran the CLI as the app, the VM tore down
OLDCLI = {
    "name": "mvt",
    "title": "the old shape · CLI ran as the app · VM died · runner exit 3",
    "cands": {"mvt-c1": "pkg", "mvt-c2": "source"},
    "steps": [
        _s("t+0:04", "clone", [], ["scout: reading repo + releases"]),
        _s("t+0:12", "scan", [
            ("pkg", "plan", "mvt", "README.md Installation:", "fast/T0"),
            ("source", "plan", "uv run mvt", "Makefile install target:",
             "medium/T1"),
        ], ["scout: 2 route(s) before the race"], chips=(1, 1),
            note="skipped · no compose file"),
        _s("t+4:03", "boot install_script", [
            ("pkg", "booting", "mvt", "README.md Installation:", "fast/T0"),
            ("source", "plan", "uv run mvt", "Makefile install target:",
             "medium/T1"),
        ], ["1 install_script .. started (mvt-c1)"]),
        _s("t+7:16", "boot", [
            ("pkg", "parked", "fail (runner exit 3)",
             "README.md Installation:", "fast/T0"),
            ("source", "parked", "unverified (plan declares no checks)",
             "Makefile install target:", "medium/T1"),
        ], ["mvt-c1 last: vmf-init: install.sh FAILED; the app exited "
            "during boot",
            "1 install_script .. fail (runner exit 3)"]),
        _s("t+7:46", "fail", [
            ("pkg", "parked", "fail (runner exit 3)",
             "README.md Installation:", "fast/T0"),
            ("source", "parked", "unverified (plan declares no checks)",
             "Makefile install target:", "medium/T1"),
        ], ["no approach produced a working service"],
            final=("mvt          fail     no working service             t+7:46", 1)),
    ],
}


SCENARIOS = [SERVICE, CLI, FAIL, REPAIR, OLDCLI]


def frame_at(scenario, i):
    # Pure accessor: the step at index i, with chips/note carried
    # forward when a step leaves them unset. Returns (step, done).
    steps = scenario["steps"]
    i = max(0, min(i, len(steps) - 1))
    step = steps[i]
    if step["chips"] is None and i > 0:
        step = dict(step, chips=steps[i - 1]["chips"])
    if step["note"] is None and i > 0:
        step = dict(step, note=steps[i - 1]["note"])
    return step, i >= len(steps) - 1


def event_lane(scenario, line):
    # Which board lane owns an event line. Three carriers: an explicit
    # runner-log prefix ("log mvt-c1:" / "mvt-c1 last:"), a numbered
    # candidate event ("1 install_script .. started"), or the race
    # itself ("*"). Pure: same line in, same lane out.
    cands = scenario.get("cands") or {}
    for cand, lane in cands.items():
        if line.startswith("log %s:" % cand) or \
                line.startswith("%s last:" % cand):
            return lane
    m = re.match(r"^\d+\s+(\S+)\s+\.\.", line)
    if m:
        return DISPLAY_METHOD.get(m.group(1), "*")
    return "*"


def events_upto(scenario, i):
    # The rolling event history through step i: [(t, lane, line)].
    out = []
    for s in scenario["steps"][:max(0, i) + 1]:
        for line in s["say"]:
            out.append((s["t"], event_lane(scenario, line), line))
    return out
