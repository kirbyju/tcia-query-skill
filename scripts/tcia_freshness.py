#!/usr/bin/env python3
"""Verify that installed TCIA query skill files match the public main branch."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from tcia_skill_version import DEFAULT_MANIFEST, SKILL_ROOT, load_manifest, validate_manifest


DEFAULT_REPO = "kirbyju/tcia-query-skill"
DEFAULT_REF = "main"
REMOTE_VERSION_FILE = "skill_version.json"


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Verify that installed skill files match GitHub.")
    check.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository owner/name.")
    check.add_argument("--ref", default=DEFAULT_REF, help="Git ref for skill code.")
    args = parser.parse_args()

    skill = check_skill(args.repo, args.ref)
    print(json.dumps({"skill": skill}, indent=2, sort_keys=True))
    return 0 if skill["current"] else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        print(f"Freshness verification network error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"TCIA freshness error: {exc}", file=sys.stderr)
        raise SystemExit(2)
