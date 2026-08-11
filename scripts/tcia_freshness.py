#!/usr/bin/env python3
"""Verify installed skill code and refresh checksum-verified TCIA release artifacts."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from tcia_skill_version import DEFAULT_MANIFEST, SKILL_ROOT, load_manifest, validate_manifest


DEFAULT_REPO = "kirbyju/tcia-query-skill"
DEFAULT_REF = "main"
DEFAULT_RELEASE_TAG = "tcia-snapshot-latest"
REMOTE_VERSION_FILE = "skill_version.json"
SIDECAR_SCRIPTS = {
    "clinical": "tcia_clinical_metadata.py",
    "controlled": "tcia_controlled_access_metadata.py",
    "nifti": "tcia_nifti_metadata.py",
    "pathology": "tcia_pathology_metadata.py",
}
SIDECAR_DATABASES = {
    "clinical": SKILL_ROOT / "cache" / "clinical_metadata.sqlite",
    "controlled": SKILL_ROOT / "cache" / "controlled_access_metadata.sqlite",
    "nifti": SKILL_ROOT / "cache" / "nifti_metadata.sqlite",
    "pathology": SKILL_ROOT / "cache" / "pathology_metadata.sqlite",
}


def github_json(url: str) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tcia-query-skill-freshness/1.0",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def remote_skill_manifest(repo: str, ref: str) -> dict[str, Any]:
    quoted_path = urllib.parse.quote(REMOTE_VERSION_FILE)
    quoted_ref = urllib.parse.quote(ref)
    payload = github_json(
        f"https://api.github.com/repos/{repo}/contents/{quoted_path}?ref={quoted_ref}"
    )
    encoded = payload.get("content")
    if not encoded:
        raise RuntimeError(f"Remote {REMOTE_VERSION_FILE} has no content.")
    return json.loads(base64.b64decode(encoded).decode("utf-8"))


def check_skill(repo: str, ref: str) -> dict[str, Any]:
    local = load_manifest(DEFAULT_MANIFEST)
    integrity = validate_manifest(local, SKILL_ROOT)
    remote = remote_skill_manifest(repo, ref)
    same_version = local.get("skill_version") == remote.get("skill_version")
    same_files = local.get("files") == remote.get("files")
    current = bool(integrity.get("ok") and same_version and same_files)
    return {
        "status": "current" if current else "update_required",
        "current": current,
        "repository": repo,
        "ref": ref,
        "local_version": local.get("skill_version"),
        "remote_version": remote.get("skill_version"),
        "local_integrity": integrity,
        "remote_updated_at_utc": remote.get("updated_at_utc"),
        "update_url": f"https://github.com/{repo}/tree/{ref}",
    }


def run_ensure(script_name: str, repo: str, tag: str) -> dict[str, Any]:
    command = [
        sys.executable,
        str(SKILL_ROOT / "scripts" / script_name),
        "ensure",
        "--repo",
        repo,
        "--tag",
        tag,
    ]
    completed = subprocess.run(command, cwd=SKILL_ROOT, text=True, capture_output=True)
    result: dict[str, Any] = {
        "script": script_name,
        "returncode": completed.returncode,
    }
    stdout = completed.stdout.strip()
    if stdout:
        try:
            result["result"] = json.loads(stdout)
        except json.JSONDecodeError:
            result["stdout"] = stdout
    if completed.stderr.strip():
        result["stderr"] = completed.stderr.strip()
    if completed.returncode != 0:
        raise RuntimeError(json.dumps(result, indent=2))
    return result


def requested_sidecars(explicit: list[str], installed: bool) -> list[str]:
    requested = set(explicit)
    if installed:
        requested.update(name for name, path in SIDECAR_DATABASES.items() if path.exists())
    return sorted(requested)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Verify that installed skill files match GitHub.")
    ensure = subparsers.add_parser(
        "ensure",
        help="Verify skill files, then refresh the base snapshot and requested sidecars.",
    )
    for command in (check, ensure):
        command.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository owner/name.")
        command.add_argument("--ref", default=DEFAULT_REF, help="Git ref for skill code.")
    ensure.add_argument("--tag", default=DEFAULT_RELEASE_TAG, help="Release artifact tag.")
    ensure.add_argument(
        "--sidecar",
        action="append",
        choices=sorted(SIDECAR_SCRIPTS),
        default=[],
        help="Also refresh one optional metadata sidecar; repeat as needed.",
    )
    ensure.add_argument(
        "--installed-sidecars",
        action="store_true",
        help="Also refresh optional sidecars whose local SQLite files already exist.",
    )
    args = parser.parse_args()

    skill = check_skill(args.repo, args.ref)
    if args.command == "check" or not skill["current"]:
        print(json.dumps({"skill": skill}, indent=2, sort_keys=True))
        return 0 if skill["current"] else 3

    artifacts = {
        "base": run_ensure("tcia_snapshot.py", args.repo, args.tag),
        "sidecars": {},
    }
    for name in requested_sidecars(args.sidecar, args.installed_sidecars):
        artifacts["sidecars"][name] = run_ensure(SIDECAR_SCRIPTS[name], args.repo, args.tag)
    print(json.dumps({"skill": skill, "artifacts": artifacts}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        print(f"Freshness verification network error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"TCIA freshness error: {exc}", file=sys.stderr)
        raise SystemExit(2)
