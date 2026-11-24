#!/usr/bin/env python3
"""Create missing mailbox directories, preserving all registration and task files."""
import argparse
import json
import re
from pathlib import Path


def project_ids(root):
    try:
        registry = json.loads((root / "projects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Project registry is unreadable or not valid JSON") from None
    projects = registry.get("projects") if isinstance(registry, dict) else None
    if not isinstance(projects, list) or not all(isinstance(p, dict) for p in projects):
        raise ValueError("Project registry is a malformed registry: projects must be a list of objects")
    ids = [p.get("id") for p in projects]
    if not all(isinstance(x, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", x) for x in ids) or len(ids) != len(set(ids)):
        raise ValueError("Project IDs must be unique safe directory names")
    return ids


def ensure(root):
    root = root.expanduser().resolve()
    for filename in ("AGENTS.md", "README.md", "projects.json"):
        if not (root / filename).is_file():
            raise ValueError(f"Missing required mailbox file: {filename}")
    ids = project_ids(root)
    targets = [root / name for name in ("sessions", "projects", "epics", "discovery", "templates")]
    targets += [root / kind / key for kind in ("inbox", "outbox", "archive", "tasks") for key in ids]
    targets += [root / kind / "orchestrator" for kind in ("inbox", "outbox", "archive")]
    for path in targets:
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Directory escapes the mailbox root: {path.relative_to(root)}")
        if path.exists() and not path.is_dir():
            raise ValueError(f"Expected a directory: {path.relative_to(root)}")
    created = []
    for path in targets:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path.relative_to(root)))
    return {"root": str(root), "projects": ids, "created_directories": created}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.home() / "harness-mailbox")
    args = parser.parse_args()
    try:
        report = ensure(args.root)
    except ValueError as error:
        raise SystemExit(f"ensure_structure: {error}")
    except OSError:
        raise SystemExit("ensure_structure: the mailbox directories could not be created")
    print(json.dumps(report, indent=2))
