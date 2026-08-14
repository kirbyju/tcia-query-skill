#!/usr/bin/env python3
"""Generate and validate the version/hash manifest for installed skill files."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = SKILL_ROOT / "skill_version.json"
INCLUDED_TOP_LEVEL_FILES = ("README.md", "SKILL.md")
INCLUDED_SUFFIXES = {".csv", ".json", ".md", ".py", ".txt", ".yaml", ".yml"}
INCLUDED_DIRECTORIES = ("agents", "mcp_server", "references", "scripts")
EXCLUDED_NAMES = {"__pycache__", ".DS_Store"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_skill_files(root: Path = SKILL_ROOT) -> Iterable[Path]:
    for name in INCLUDED_TOP_LEVEL_FILES:
        path = root / name
        if path.is_file():
            yield path
    for directory in INCLUDED_DIRECTORIES:
        base = root / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in INCLUDED_SUFFIXES:
                continue
            if any(part in EXCLUDED_NAMES for part in path.parts):
                continue
            yield path


def current_file_hashes(root: Path = SKILL_ROOT) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in sorted(set(iter_skill_files(root)))
    }


def build_manifest(version: str, root: Path = SKILL_ROOT) -> dict[str, Any]:
    if not version.strip():
        raise ValueError("A nonempty skill version is required.")
    return {
        "schema_version": SCHEMA_VERSION,
        "skill_version": version.strip(),
        "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "files": current_file_hashes(root),
    }


def validate_manifest(manifest: dict[str, Any], root: Path = SKILL_ROOT) -> dict[str, Any]:
    expected = manifest.get("files")
    if not isinstance(expected, dict):
        return {"ok": False, "error": "Manifest files must be an object."}
    actual = current_file_hashes(root)
    expected_names = set(expected)
    actual_names = set(actual)
    missing = sorted(expected_names - actual_names)
    untracked = sorted(actual_names - expected_names)
    changed = sorted(
        name for name in expected_names & actual_names if expected.get(name) != actual.get(name)
    )
    schema_ok = manifest.get("schema_version") == SCHEMA_VERSION
    version_ok = bool(str(manifest.get("skill_version") or "").strip())
    return {
        "ok": schema_ok and version_ok and not missing and not untracked and not changed,
        "schema_ok": schema_ok,
        "version_ok": version_ok,
        "skill_version": manifest.get("skill_version"),
        "missing": missing,
        "untracked": untracked,
        "changed": changed,
    }


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="Write a new skill version manifest.")
    generate.add_argument("--version", required=True, help="Human-readable monotonically bumped version.")
    generate.add_argument("--out", default=str(DEFAULT_MANIFEST), help="Manifest output path.")
    check = subparsers.add_parser("check", help="Validate installed files against the manifest.")
    check.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Manifest path.")
    args = parser.parse_args()

    if args.command == "generate":
        manifest = build_manifest(args.version)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": "generated", "path": str(out), **manifest}, indent=2))
        return 0

    result = validate_manifest(load_manifest(Path(args.manifest)))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
