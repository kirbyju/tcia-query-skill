#!/usr/bin/env python3
"""Save and restore hash-bound checkpoints for local V2 build verification.

This utility is deliberately local-only. It does not publish artifacts or
relax any release gate. Maintainers use it around expensive component build
blocks, then rerun the semantic, bundle, and publication validations normally.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_ROOT = Path("cache/local-v2-checkpoints")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_input(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"checkpoint input does not exist: {path}")
    if path.is_file():
        return {
            "path": str(path),
            "kind": "file",
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
    if not path.is_dir():
        raise RuntimeError(f"checkpoint input is not a file or directory: {path}")
    files = []
    ignored_names = {".DS_Store", "__pycache__", ".pytest_cache"}
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path)
        if any(part in ignored_names for part in relative.parts):
            continue
        if child.suffix in {".pyc", ".pyo"}:
            continue
        files.append(
            {
                "path": relative.as_posix(),
                "bytes": child.stat().st_size,
                "sha256": file_sha256(child),
            }
        )
    return {"path": str(path), "kind": "directory", "files": files}


def checkpoint_identity(
    stage: str, inputs: list[Path], values: list[str]
) -> tuple[str, dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", stage):
        raise ValueError("stage must contain only letters, numbers, dot, dash, or underscore")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "inputs": [describe_input(path) for path in inputs],
        "values": sorted(values),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), payload


def checkpoint_path(root: Path, stage: str, fingerprint: str) -> Path:
    return root / stage / fingerprint


def load_manifest(path: Path) -> dict[str, Any]:
    manifest_path = path / "checkpoint.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"checkpoint manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("checkpoint schema version is unsupported")
    return manifest


def verify_checkpoint(path: Path, fingerprint: str) -> dict[str, Any]:
    manifest = load_manifest(path)
    if manifest.get("fingerprint") != fingerprint:
        raise RuntimeError("checkpoint fingerprint does not match its directory")
    for output in manifest.get("outputs") or []:
        stored = path / "files" / str(output["stored_name"])
        if not stored.is_file():
            raise RuntimeError(f"checkpoint output is missing: {stored}")
        if stored.stat().st_size != int(output["bytes"]):
            raise RuntimeError(f"checkpoint output size changed: {stored}")
        if file_sha256(stored) != output["sha256"]:
            raise RuntimeError(f"checkpoint output hash changed: {stored}")
    return manifest


def save_checkpoint(
    root: Path,
    stage: str,
    inputs: list[Path],
    values: list[str],
    outputs: list[Path],
) -> dict[str, Any]:
    fingerprint, identity = checkpoint_identity(stage, inputs, values)
    destination = checkpoint_path(root, stage, fingerprint)
    if destination.exists():
        manifest = verify_checkpoint(destination, fingerprint)
        return {"saved": False, "reused": True, **manifest}
    if not outputs:
        raise ValueError("at least one checkpoint output is required")
    for output in outputs:
        if not output.is_file():
            raise FileNotFoundError(f"checkpoint output is not a file: {output}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{fingerprint[:12]}-", dir=destination.parent)
    )
    try:
        files_dir = temporary / "files"
        files_dir.mkdir()
        output_records = []
        for index, output in enumerate(outputs):
            stored_name = f"{index:03d}-{output.name}"
            stored = files_dir / stored_name
            shutil.copy2(output, stored)
            output_records.append(
                {
                    "path": str(output),
                    "stored_name": stored_name,
                    "bytes": stored.stat().st_size,
                    "sha256": file_sha256(stored),
                }
            )
        manifest = {
            **identity,
            "fingerprint": fingerprint,
            "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "outputs": output_records,
        }
        (temporary / "checkpoint.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"saved": True, "reused": False, **manifest}


def restore_checkpoint(
    root: Path, stage: str, inputs: list[Path], values: list[str]
) -> dict[str, Any]:
    fingerprint, _ = checkpoint_identity(stage, inputs, values)
    source = checkpoint_path(root, stage, fingerprint)
    if not source.is_dir():
        return {"hit": False, "stage": stage, "fingerprint": fingerprint}
    manifest = verify_checkpoint(source, fingerprint)
    for output in manifest["outputs"]:
        target = Path(output["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        stored = source / "files" / output["stored_name"]
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.", dir=target.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
        try:
            shutil.copy2(stored, temporary)
            if file_sha256(temporary) != output["sha256"]:
                raise RuntimeError(f"restored output hash changed: {target}")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return {"hit": True, **manifest}


def add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--input", type=Path, action="append", default=[])
    parser.add_argument(
        "--value",
        action="append",
        default=[],
        help="Explicit non-file setting included in the checkpoint fingerprint.",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    save_parser = subparsers.add_parser("save")
    add_identity_arguments(save_parser)
    save_parser.add_argument("--output", type=Path, action="append", required=True)
    restore_parser = subparsers.add_parser("restore")
    add_identity_arguments(restore_parser)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "save":
            result = save_checkpoint(
                args.root, args.stage, args.input, args.value, args.output
            )
        else:
            result = restore_checkpoint(
                args.root, args.stage, args.input, args.value
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if args.command == "save" or result["hit"] else 3
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
