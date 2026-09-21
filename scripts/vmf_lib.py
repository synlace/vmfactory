"""Shared helpers: YAML/schema loading and the Dockerfile subset parser."""

from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"

# Directives translated into the image build. Everything else is rejected
# (runtime-only directives) or warned about; see parse_dockerfile().
TRANSLATED = {"FROM", "RUN", "COPY", "ADD", "ENV", "WORKDIR", "USER"}
IGNORED = {
    "EXPOSE", "VOLUME", "LABEL", "STOPSIGNAL", "HEALTHCHECK",
    "ENTRYPOINT", "CMD", "ONBUILD", "SHELL", "MAINTAINER",
}


def load_yaml(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def json_path(err):
    return "/".join(str(p) for p in err.path) or "<root>"


def schema_errors(doc, schema_name):
    validator = Draft202012Validator(load_yaml(SCHEMAS / schema_name))
    return sorted(validator.iter_errors(doc), key=lambda e: list(e.path))


def shquote(value):
    """Single-quote a value for POSIX sh."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _logical_lines(text):
    """Join backslash continuations, drop full-line comments and blanks."""
    pending = ""
    for raw in text.splitlines():
        pending += raw
        if pending.rstrip().endswith("\\"):
            pending = pending.rstrip()[:-1] + " "
            continue
        line = pending.strip()
        pending = ""
        if line and not line.startswith("#"):
            yield line


def resolve_base(ref, images, errors):
    """Map a Dockerfile FROM ref (or images.yaml key) to a pinned entry."""
    if "@" in ref:
        errors.append(f"error: dockerfile: digest ref {ref} not supported; pin the image in images.yaml")
        return None
    if ref in images:
        return images[ref]
    for entry in images.values():
        if ref in (entry.get("matches") or []):
            return entry
    errors.append(
        f"error: dockerfile: FROM {ref} has no pin in images.yaml; "
        f"add a pin (url + checksum + user) before building"
    )
    return None


def parse_dockerfile(path, images):
    """Parse the supported subset of a Dockerfile.

    Returns a dict with base (entry or None), runs, copies [(src, dest)],
    envs [(key, value)], warnings, errors. Base resolution goes through
    images.yaml; runtime-only directives produce warnings, not image content.
    """
    result = {"base": None, "base_ref": None, "runs": [], "copies": [],
              "envs": [], "warnings": [], "errors": []}
    from_seen = False
    workdir = None

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        result["errors"].append(f"error: dockerfile: cannot read {path}: {exc}")
        return result

    for lineno, line in enumerate(_logical_lines(text), start=1):
        word = line.split(None, 1)[0].upper()
        rest = line[len(line.split(None, 1)[0]):].strip()
        loc = f"error: dockerfile:{lineno}: "

        if word == "FROM":
            if from_seen:
                result["errors"].append(
                    loc + "multi-stage builds are not supported; one FROM per Dockerfile"
                )
                continue
            if rest.startswith("--platform="):
                flag, _, remainder = rest.partition(" ")
                rest = remainder.strip()
                result["warnings"].append(f"dockerfile:{lineno}: --platform ignored")
            if not rest:
                result["errors"].append(loc + "FROM requires an image ref")
                continue
            entry = resolve_base(rest, images, result["errors"])
            result["base_ref"] = rest
            if entry is not None:
                result["base"] = entry
            from_seen = True

        elif word in ("RUN",):
            if rest.startswith("["):
                result["errors"].append(
                    loc + "exec-form RUN ([\"...\"], ...) not supported; use shell form"
                )
                continue
            result["runs"].append({"dir": workdir, "cmd": rest})

        elif word in ("COPY", "ADD"):
            parts = rest.split()
            flags = [p for p in parts if p.startswith("--")]
            args = [p for p in parts if not p.startswith("--")]
            if any(f.startswith("--from") for f in flags):
                result["errors"].append(
                    loc + "--from (multi-stage) not supported"
                )
                continue
            if flags:
                result["warnings"].append(
                    f"dockerfile:{lineno}: flags {', '.join(flags)} ignored"
                )
            if len(args) != 2:
                result["errors"].append(
                    loc + "exactly one source and one destination supported"
                )
                continue
            src, dest = args
            result["copies"].append({"src": src, "dest": dest})

        elif word == "ENV":
            if " " in rest and "=" not in rest.split(" ", 1)[0]:
                key, _, value = rest.partition(" ")
                result["envs"].append((key, value.strip()))
            else:
                for pair in rest.split():
                    key, _, value = pair.partition("=")
                    result["envs"].append((key, value))

        elif word == "WORKDIR":
            workdir = rest if rest.startswith("/") else f"{workdir or '/'}/{rest}".replace("//", "/")

        elif word == "USER":
            if rest not in ("root", "0"):
                result["errors"].append(
                    loc + f"USER {rest} not supported; only root"
                )

        elif word in IGNORED:
            result["warnings"].append(
                f"dockerfile:{lineno}: {word} is runtime-only and was ignored"
            )

        else:
            result["errors"].append(
                loc + f"directive {word} not in supported subset "
                f"({', '.join(sorted(TRANSLATED))})"
            )

    if not from_seen:
        result["errors"].append("error: dockerfile: no FROM directive")
    return result
