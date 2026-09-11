#!/usr/bin/env python3
"""Verify that installed TCIA query skill files match the public main branch."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from tcia_skill_version import DEFAULT_MANIFEST, SKILL_ROOT, load_manifest, validate_manifest


DEFAULT_REPO = "kirbyju/tcia-query-skill"
DEFAULT_REF = "main"
REMOTE_VERSION_FILE = "skill_version.json"
DEFAULT_CACHE_FILE = SKILL_ROOT / "cache" / "tcia_freshness_receipt.json"
DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 30.0
RECEIPT_SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def retry_delay(exc: BaseException, attempt: int) -> float | None:
    headers: Any = {}
    if isinstance(exc, urllib.error.HTTPError):
        headers = exc.headers or {}
        rate_limited = exc.code == 429 or (
            exc.code == 403 and str(headers.get("X-RateLimit-Remaining") or "") == "0"
        )
        if exc.code < 500 and not rate_limited:
            return None
    elif not isinstance(
        exc,
        (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            json.JSONDecodeError,
        ),
    ):
        return None
    delay = float(min(2**attempt, 4))
    retry_after = str(headers.get("Retry-After") or "").strip()
    if retry_after.isdigit():
        delay = float(retry_after)
    else:
        reset = str(headers.get("X-RateLimit-Reset") or "").strip()
        if reset.isdigit():
            delay = max(float(reset) - time.time(), 0.0)
    return min(delay, MAX_RETRY_DELAY_SECONDS)


def github_json(
    url: str,
    *,
    etag: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
) -> tuple[dict[str, Any] | None, str, bool]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tcia-query-skill-freshness/1.0",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(max(retries, 0) + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return (
                    json.loads(response.read().decode("utf-8")),
                    str(response.headers.get("ETag") or ""),
                    False,
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return None, etag, True
            delay = retry_delay(exc, attempt)
            if delay is None or attempt >= retries:
                raise
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            json.JSONDecodeError,
        ) as exc:
            delay = retry_delay(exc, attempt)
            if delay is None or attempt >= retries:
                raise
        time.sleep(delay)
    raise RuntimeError("unreachable GitHub retry state")


def remote_skill_manifest(
    repo: str,
    ref: str,
    *,
    etag: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
) -> tuple[dict[str, Any] | None, str, bool]:
    quoted_path = urllib.parse.quote(REMOTE_VERSION_FILE)
    quoted_ref = urllib.parse.quote(ref)
    payload, response_etag, not_modified = github_json(
        f"https://api.github.com/repos/{repo}/contents/{quoted_path}?ref={quoted_ref}",
        etag=etag,
        timeout=timeout,
        retries=retries,
    )
    if not_modified:
        return None, response_etag, True
    assert payload is not None
    encoded = payload.get("content")
    if not encoded:
        raise RuntimeError(f"Remote {REMOTE_VERSION_FILE} has no content.")
    return json.loads(base64.b64decode(encoded).decode("utf-8")), response_etag, False


def load_receipt(path: Path, repo: str, ref: str) -> dict[str, Any] | None:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    checksum = receipt.pop("receipt_sha256", "")
    valid = (
        receipt.get("schema_version") == RECEIPT_SCHEMA_VERSION
        and receipt.get("repository") == repo
        and receipt.get("ref") == ref
        and checksum == payload_sha256(receipt)
        and receipt.get("remote_manifest_sha256")
        == payload_sha256(receipt.get("remote_manifest"))
    )
    receipt["receipt_sha256"] = checksum
    return receipt if valid else None


def write_receipt(path: Path, payload: dict[str, Any]) -> None:
    body = dict(payload)
    body["receipt_sha256"] = payload_sha256(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def receipt_is_fresh(receipt: dict[str, Any], ttl_seconds: int, at: dt.datetime) -> bool:
    try:
        checked = dt.datetime.fromisoformat(
            str(receipt["checked_at_utc"]).replace("Z", "+00:00")
        )
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=dt.timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    age = (at - checked.astimezone(dt.timezone.utc)).total_seconds()
    return 0 <= age < max(ttl_seconds, 0)


def check_skill(
    repo: str,
    ref: str,
    *,
    cache_file: Path = DEFAULT_CACHE_FILE,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    use_cache: bool = True,
) -> dict[str, Any]:
    local = load_manifest(DEFAULT_MANIFEST)
    integrity = validate_manifest(local, SKILL_ROOT)
    at = now_utc()
    receipt = load_receipt(cache_file, repo, ref) if use_cache else None
    remote_check = "live"
    if receipt and receipt_is_fresh(receipt, ttl_seconds, at):
        remote = receipt["remote_manifest"]
        checked_at = receipt["checked_at_utc"]
        remote_check = "cached"
    else:
        etag = str((receipt or {}).get("etag") or "")
        remote_result = remote_skill_manifest(
            repo, ref, etag=etag, timeout=timeout, retries=retries
        )
        # Preserve compatibility with callers/tests that replace the historical
        # helper with a direct manifest-returning function.
        if isinstance(remote_result, tuple):
            fetched, response_etag, not_modified = remote_result
        else:
            fetched, response_etag, not_modified = remote_result, "", False
        if not_modified:
            if not receipt:
                raise RuntimeError("GitHub returned not-modified without a valid cache receipt")
            remote = receipt["remote_manifest"]
            remote_check = "conditional_not_modified"
        else:
            if fetched is None:
                raise RuntimeError("GitHub freshness response contained no manifest")
            remote = fetched
        checked_at = at.isoformat()
        if use_cache:
            write_receipt(
                cache_file,
                {
                    "schema_version": RECEIPT_SCHEMA_VERSION,
                    "repository": repo,
                    "ref": ref,
                    "checked_at_utc": checked_at,
                    "etag": response_etag,
                    "remote_manifest": remote,
                    "remote_manifest_sha256": payload_sha256(remote),
                },
            )
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
        "remote_check": remote_check,
        "remote_checked_at_utc": checked_at,
        "cache_ttl_seconds": ttl_seconds,
        "update_url": f"https://github.com/{repo}/tree/{ref}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Verify that installed skill files match GitHub.")
    check.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository owner/name.")
    check.add_argument("--ref", default=DEFAULT_REF, help="Git ref for skill code.")
    check.add_argument(
        "--cache-file",
        default=os.environ.get("TCIA_FRESHNESS_CACHE_FILE", str(DEFAULT_CACHE_FILE)),
    )
    check.add_argument(
        "--ttl-seconds",
        type=int,
        default=int(os.environ.get("TCIA_FRESHNESS_TTL_SECONDS", DEFAULT_TTL_SECONDS)),
    )
    check.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("TCIA_FRESHNESS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
    )
    check.add_argument(
        "--retries",
        type=int,
        default=int(os.environ.get("TCIA_FRESHNESS_RETRIES", DEFAULT_RETRIES)),
    )
    check.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    skill = check_skill(
        args.repo,
        args.ref,
        cache_file=Path(args.cache_file),
        ttl_seconds=args.ttl_seconds,
        timeout=args.timeout,
        retries=args.retries,
        use_cache=not args.no_cache,
    )
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
