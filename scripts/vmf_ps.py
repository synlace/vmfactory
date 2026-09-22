#!/usr/bin/env python3
# `just ps`: the docker-ps view of vmf instances.
#   ps [--all]     — default hides race candidates (<name>-c<n>)
#   ps --json      — machine-readable rows
# One row per instance dir (~/.vmf/runs/<12-hex-id>/conf) or legacy
# conf. NAMES shows the symlink holders; the id is never re-used, so a
# replaced name keeps its old row (Exited) until cleaned.
import glob
import json
import os
import re
import sys
import time

RUNS = os.environ.get("VMF_RUNS") or os.path.expanduser("~/.vmf/runs")
CAND = re.compile(r"-c[0-9]+$")


def conf_name(conf):
    for line in open(conf, errors="replace"):
        if line.startswith("NAME="):
            return line.split("=", 1)[1].strip()
    base = os.path.basename(conf)
    return base[:-5] if base.endswith(".conf") else base


def rel_age(ts):
    s = int(time.time() - ts)
    if s < 60:
        return "%ds ago" % s
    if s < 3600:
        return "%d min ago" % (s // 60)
    if s < 86400:
        return "%d hours ago" % (s // 3600)
    return "%d days ago" % (s // 86400)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def verdict_of(d, name):
    for p in (os.path.join(d, "verdict"),
              os.path.join(RUNS, "%s.verdict" % name)):
        if os.path.isfile(p):
            return open(p).read().strip() or "pass"
    return ""


def load_conf(conf):
    out = {}
    try:
        for line in open(conf, errors="replace"):
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def rows():
    # name -> id holders (the symlink is the name handover).
    holders = {}
    for link in glob.glob(os.path.join(RUNS, "*")):
        if os.path.islink(link):
            holders[os.path.basename(link)] = os.readlink(link)
    out = []
    # Symlinked names also match the glob; dedupe on the real path so
    # each instance renders once.
    seen = set()
    confs = glob.glob(os.path.join(RUNS, "*", "conf")) \
        + glob.glob(os.path.join(RUNS, "*.conf"))
    confs = [c for c in confs
             if os.path.realpath(c) not in seen and not seen.add(os.path.realpath(c))]
    for conf in sorted(confs, key=lambda p: os.path.getmtime(p)):
        c = load_conf(conf)
        d = os.path.dirname(conf)
        ident = c.get("ID", "")
        if not ident:
            ident = "—"
        name = c.get("NAME") or conf_name(conf)
        ref = c.get("SRC") or c.get("IMAGE") or "—"
        approach = c.get("APPROACH") or "—"
        created = rel_age(os.path.getmtime(conf))
        names = [name]
        if os.path.isdir(d):
            linked = sorted(n for n, i in holders.items()
                            if i == os.path.basename(d))
            names = linked or names
        pid = 0
        try:
            pid = int(c.get("PID", "0") or 0)
        except ValueError:
            pass
        if pid and alive(pid):
            status = "Up " + rel_age(os.path.getmtime(conf)).replace(" ago", "")
        else:
            status = "Exited"
        v = verdict_of(d, name)
        if v:
            status += " (%s)" % v.split(" ")[0]
        target = c.get("TARGET") or ""
        out.append(dict(id=ident if ident != name else "—",
                        ref=ref, approach=approach, created=created,
                        status=status, target=target, names=",".join(names),
                        mtime=os.path.getmtime(conf),
                        candidate=any(CAND.match(n) for n in names)))
    return out


def main(argv):
    show_all = "--all" in argv
    as_json = "--json" in argv
    data = [r for r in rows() if show_all or not r["candidate"]]
    if as_json:
        for r in data:
            r.pop("mtime", None)
        print(json.dumps(data, indent=2))
        return 0
    if not data:
        print("no VM instances")
        return 0
    w = (13, 30, 15, 13, 26, 34, 16)
    print("%-*s %-30s %-15s %-13s %-26s %-34s %s"
          % (w[0], "VM ID", "IMAGE/LAB", "APPROACH", "CREATED",
             "STATUS", "TARGET", "NAMES"))
    for r in data:
        t = r["target"]
        if len(t) > w[5]:
            t = t[:w[5] - 1] + "…"
        ref = r["ref"]
        if len(ref) > w[1]:
            ref = ref[:w[1] - 1] + "…"
        print("%-*s %-30s %-15s %-13s %-26s %-34s %s"
              % (w[0], r["id"][:12], ref, r["approach"], r["created"],
                 r["status"][:w[4]], t, r["names"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
