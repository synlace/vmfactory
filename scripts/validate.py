"""Validate a spec: schema, base pin resolution, role existence, file pins,
and Dockerfile directive subset. Errors are fatal; warnings are informational."""

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vmf_lib as lib


def main():
    parser = argparse.ArgumentParser(description="Validate a VM spec without building")
    parser.add_argument("spec", type=Path)
    args = parser.parse_args()

    errors = []
    spec = lib.load_yaml(args.spec)
    for err in lib.schema_errors(spec, "spec.schema.json"):
        errors.append(f"error: spec {args.spec.name}: schema {lib.json_path(err)}: {err.message}")

    images = lib.load_yaml(lib.ROOT / "images.yaml")
    base = None
    base_ref = (spec or {}).get("base")
    if base_ref:
        base = lib.resolve_base(base_ref, images, errors)
        if base is not None and not (u := base.get("url")):
            errors.append(f"error: images.yaml: pin {base_ref} has no url")
        if base is not None and not base.get("checksum"):
            errors.append(f"error: images.yaml: pin {base_ref} has no checksum")
        if base is not None and not base.get("user"):
            errors.append(f"error: images.yaml: pin {base_ref} has no user")

    for role in (spec or {}).get("roles", []):
        if not (lib.ROOT / "roles" / role).is_dir():
            errors.append(f"error: spec {args.spec.name}: role directory roles/{role} does not exist")

    for entry in (spec or {}).get("files", []):
        src = lib.ROOT / entry["src"]
        if not src.is_file():
            errors.append(f"error: spec {args.spec.name}: file src {src} does not exist")
            continue
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        expected = entry.get("sha256")
        if expected and digest != expected:
            errors.append(
                f"error: spec {args.spec.name}: file {entry['src']} hash {digest} "
                f"does not match sha256 pin {expected}"
            )
        if not entry["dest"].startswith("/"):
            errors.append(f"error: spec {args.spec.name}: file dest must be absolute: {entry['dest']}")
        print(f"ok: file {entry['src']} -> {entry['dest']}")

    provision = (spec or {}).get("provision")
    if provision:
        dockerfile = lib.ROOT / provision["dockerfile"]
        if not dockerfile.is_file():
            errors.append(f"error: spec {args.spec.name}: dockerfile {dockerfile} does not exist")
        else:
            parsed = lib.parse_dockerfile(dockerfile, images)
            errors.extend(parsed["errors"])
            if parsed["base"] is not None and base is not None:
                if parsed["base"].get("url") != base.get("url"):
                    errors.append(
                        f"error: spec {args.spec.name}: dockerfile FROM {parsed['base_ref']} "
                        f"resolves to a different pin than spec base {base_ref}"
                    )
            context = dockerfile.parent
            for copy in parsed["copies"]:
                if not (context / copy["src"]).exists():
                    errors.append(f"error: dockerfile: COPY source {context / copy['src']} does not exist")
            for warning in parsed["warnings"]:
                print(f"warning: {warning}")

    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        sys.exit(1)

    print(f"ok: spec {args.spec.name}: schema valid")
    print(f"ok: base {base_ref} resolves to pinned image {base['url']}")
    for role in spec.get("roles", []):
        print(f"ok: role {role} exists")
    if provision:
        print(f"ok: dockerfile {provision['dockerfile']} translates cleanly")


if __name__ == "__main__":
    main()
