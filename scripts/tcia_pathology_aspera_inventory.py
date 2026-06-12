#!/usr/bin/env python3
"""Browse public TCIA pathology Aspera packages and write a file inventory TSV."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tcia_pathology_metadata import (
    DEFAULT_SNAPSHOT_DB,
    extension_for_name,
    normalize_package_path,
    pathology_download_filter_sql,
)


OUTPUT_COLUMNS = [
    "download_row_id",
    "short_title",
    "download_id",
    "download_title",
    "download_url",
    "package_path",
    "file_name",
    "file_ext",
    "entry_type",
    "bytes",
    "checksum",
    "checksum_algorithm",
    "modified_time",
    "inventory_source",
    "inventory_status",
    "browsed_at",
    "row_json",
]

CHECKSUM_MANIFEST_TOKENS = (
    ".sums",
    ".md5",
    ".sha1",
    ".sha256",
    ".sha512",
    "checksum",
    "checksums",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_ascli_csv(text: str) -> list[dict[str, Any]]:
    lines = [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("Items:")
    ]
    if not lines:
        return []
    rows = list(csv.reader(lines))
    header = [value.strip().lower() for value in rows[0]]
    known_headers = {"path", "name", "type", "entry_type", "bytes", "size", "modified_at"}
    if known_headers.intersection(header):
        return [dict(row) for row in csv.DictReader(lines)]

    fieldnames = ["path", "name", "entry_type", "bytes", "modified_time", "permissions"]
    target_fieldnames = [
        "target_path",
        "target_name",
        "target_entry_type",
        "target_bytes",
        "target_modified_time",
        "target_permissions",
    ]
    output: list[dict[str, Any]] = []
    for values in rows:
        record: dict[str, Any] = {}
        for index, value in enumerate(values[:6]):
            record[fieldnames[index]] = value
        for index, value in enumerate(values[6:12]):
            record[target_fieldnames[index]] = value
        for index, value in enumerate(values[12:], 12):
            record[f"extra_{index}"] = value
        output.append(record)
    return output


def row_value(row: dict[str, Any], *names: str) -> str:
    normalized = {str(key).strip().lower(): value for key, value in row.items()}
    for name in names:
        value = normalized.get(name.lower())
        if value in (None, ""):
            continue
        text = str(value)
        if text.strip().lower() in {"<empty string>", "<null>"}:
            continue
        return text
    return ""


def build_browse_query(offset_paging: bool, page_limit: int) -> dict[str, Any] | None:
    if not offset_paging:
        return None
    return {"paging": False, "limit": page_limit}


def safe_slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("_")


def exception_summary(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout
        if detail:
            return detail[-2000:]
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timed out after {exc.timeout} seconds: {' '.join(str(part) for part in exc.cmd)}"
    return str(exc)


def run_ascli_command(command: list[str], timeout: int, retries: int, retry_sleep: float) -> subprocess.CompletedProcess[str]:
    last_exc: subprocess.SubprocessError | OSError | None = None
    for attempt in range(retries + 1):
        try:
            return subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            last_exc = exc
            if attempt >= retries:
                break
            if retry_sleep:
                time.sleep(retry_sleep * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError("ascli command did not run")


def browse_package_path(
    ascli: str,
    url: str,
    package_path: str,
    timeout: int,
    query: dict[str, Any] | None = None,
    retries: int = 0,
    retry_sleep: float = 0.0,
) -> list[dict[str, Any]]:
    command = [
        ascli,
        "--format=csv",
        "faspex5",
        "packages",
        "browse",
        f"--url={url}",
    ]
    if query:
        command.append(f"--query=@json:{json.dumps(query, separators=(',', ':'))}")
    if package_path:
        command.append(package_path)
    completed = run_ascli_command(command, timeout, retries, retry_sleep)
    return parse_ascli_csv(completed.stdout)


def browse_package_recursive(
    ascli: str,
    url: str,
    timeout: int,
    query: dict[str, Any] | None = None,
    retries: int = 0,
    retry_sleep: float = 0.0,
) -> list[dict[str, Any]]:
    recursive_query = {"recursive": True, **(query or {})}
    command = [
        ascli,
        "--format=csv",
        "faspex5",
        "packages",
        "browse",
        f"--url={url}",
        f"--query=@json:{json.dumps(recursive_query, separators=(',', ':'))}",
    ]
    completed = run_ascli_command(command, timeout, retries, retry_sleep)
    return parse_ascli_csv(completed.stdout)


def receive_package_file(
    ascli: str,
    url: str,
    package_path: str,
    out_dir: Path,
    timeout: int,
    retries: int,
    retry_sleep: float,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_name = Path(package_path).name
    before = {
        path.resolve()
        for path in out_dir.rglob(expected_name)
        if path.is_file()
    }
    command = [
        ascli,
        "faspex5",
        "packages",
        "receive",
        f"--url={url}",
        package_path,
        f"--to-folder={out_dir}",
    ]
    run_ascli_command(command, timeout, retries, retry_sleep)
    candidates = [
        path
        for path in out_dir.rglob(expected_name)
        if path.is_file() and path.resolve() not in before
    ]
    if not candidates:
        candidates = [path for path in out_dir.rglob(expected_name) if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"ascli did not produce expected file: {package_path}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def checksum_manifest_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in rows:
        entry_type = row_value(row, "entry_type", "type").lower()
        target_type = row_value(row, "target_entry_type", "target_type").lower()
        if entry_type not in {"file", "symbolic_link", "symlink"} and target_type != "file":
            continue
        name_or_path = row_value(row, "path", "target_path", "name", "target_name")
        lower_name = name_or_path.lower()
        if any(token in lower_name for token in CHECKSUM_MANIFEST_TOKENS):
            matches.append(row)
    return matches


def guess_algorithm(checksum: str) -> str:
    length_map = {
        32: "md5",
        40: "sha1",
        64: "sha256",
        128: "sha512",
    }
    return length_map.get(len(checksum), "")


def parse_sums_file(path: Path) -> list[dict[str, Any]]:
    checksum_re = re.compile(r"^[A-Fa-f0-9]{16,128}$")
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            pieces = line.split(maxsplit=1)
            if len(pieces) != 2:
                continue
            checksum, package_path = pieces
            package_path = package_path.lstrip("*")
            if not checksum_re.match(checksum):
                continue
            package_path = normalize_package_path(package_path)
            if not package_path:
                continue
            entries.append(
                {
                    "line_number": line_number,
                    "checksum": checksum,
                    "checksum_algorithm": guess_algorithm(checksum),
                    "package_path": package_path,
                    "file_name": Path(package_path).name,
                    "file_ext": extension_for_name(package_path),
                    "raw_line": line,
                }
            )
    return entries


def load_download_rows(snapshot_db: Path, collection: str | None, download_id: str | None, limit: int | None) -> list[dict[str, Any]]:
    conn = sqlite3.connect(snapshot_db)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute(pathology_download_filter_sql(""))]
    conn.close()
    if collection:
        rows = [row for row in rows if row["short_title"] == collection]
    if download_id:
        rows = [row for row in rows if str(row.get("download_id") or "") == str(download_id)]
    if limit is not None:
        rows = rows[:limit]
    return rows


def download_key(download: dict[str, Any]) -> str:
    return f"{download['short_title']}\t{download.get('download_id') or ''}"


def result_key(result: dict[str, Any]) -> str:
    return f"{result.get('short_title') or ''}\t{result.get('download_id') or ''}"


def load_resume_results(summary_path: Path | None) -> list[dict[str, Any]]:
    if not summary_path or not summary_path.exists():
        return []
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    results = payload.get("results") or []
    return [dict(result) for result in results if isinstance(result, dict)]


def write_summary(summary_path: Path | None, downloads: int, summaries: list[dict[str, Any]], out: Path) -> None:
    if not summary_path:
        return
    summary = {
        "downloads": downloads,
        "completed_downloads": len(summaries),
        "files": sum(int(item.get("files") or 0) for item in summaries),
        "directories": sum(int(item.get("directories") or 0) for item in summaries),
        "errors": [error for item in summaries for error in item.get("errors", [])],
        "results": summaries,
        "out": str(out.resolve()),
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_row(download: dict[str, Any], entry: dict[str, Any], browsed_at: str) -> dict[str, Any]:
    package_path = normalize_package_path(row_value(entry, "path"))
    file_name = row_value(entry, "name") or Path(package_path).name
    bytes_value = row_value(entry, "bytes", "size") or row_value(entry, "target_bytes")
    modified_time = row_value(entry, "modified_time", "modified_at") or row_value(
        entry,
        "target_modified_time",
        "target_modified_at",
    )
    return {
        "download_row_id": download["download_row_id"],
        "short_title": download["short_title"],
        "download_id": download.get("download_id") or "",
        "download_title": download.get("download_title") or "",
        "download_url": download["download_url"],
        "package_path": package_path,
        "file_name": file_name,
        "file_ext": extension_for_name(file_name),
        "entry_type": row_value(entry, "entry_type", "type"),
        "bytes": bytes_value,
        "checksum": "",
        "checksum_algorithm": "",
        "modified_time": modified_time,
        "inventory_source": "ascli browse",
        "inventory_status": "available",
        "browsed_at": browsed_at,
        "row_json": json.dumps(entry, ensure_ascii=False, sort_keys=True),
    }


def sums_entry_row(
    download: dict[str, Any],
    entry: dict[str, Any],
    sums_package_path: str,
    browsed_at: str,
) -> dict[str, Any]:
    package_path = normalize_package_path(entry["package_path"])
    file_name = entry.get("file_name") or Path(package_path).name
    return {
        "download_row_id": download["download_row_id"],
        "short_title": download["short_title"],
        "download_id": download.get("download_id") or "",
        "download_title": download.get("download_title") or "",
        "download_url": download["download_url"],
        "package_path": package_path,
        "file_name": file_name,
        "file_ext": entry.get("file_ext") or extension_for_name(file_name),
        "entry_type": "file",
        "bytes": "",
        "checksum": entry.get("checksum") or "",
        "checksum_algorithm": entry.get("checksum_algorithm") or "",
        "modified_time": "",
        "inventory_source": "ascli checksum manifest",
        "inventory_status": "available",
        "browsed_at": browsed_at,
        "row_json": json.dumps(
            {
                "sums_package_path": sums_package_path,
                "line_number": entry.get("line_number"),
                "raw_line": entry.get("raw_line"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def try_browse_download_from_sums(
    ascli: str,
    download: dict[str, Any],
    writer: csv.DictWriter,
    timeout: int,
    query: dict[str, Any] | None,
    retries: int,
    retry_sleep: float,
    sums_cache_dir: Path,
    fail_fast: bool,
) -> dict[str, Any] | None:
    url = download["download_url"]
    browsed_at = utc_now()
    try:
        root_entries = browse_package_path(
            ascli,
            url,
            "",
            timeout,
            query,
            retries,
            retry_sleep,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        error = {
            "short_title": download["short_title"],
            "download_id": download.get("download_id") or "",
            "package_path": "",
            "method": "root_sums_probe",
            "error": exception_summary(exc),
        }
        if fail_fast:
            raise RuntimeError(json.dumps(error, sort_keys=True)) from exc
        return None

    candidates = checksum_manifest_rows(root_entries)
    if not candidates:
        return None

    files = 0
    errors: list[dict[str, Any]] = []
    cache_slug = safe_slug(
        f"{download['short_title']}_{download.get('download_id') or download['download_row_id']}"
    )
    cache_dir = sums_cache_dir / cache_slug
    for candidate in candidates:
        candidate_path = normalize_package_path(row_value(candidate, "path", "target_path"))
        if not candidate_path:
            continue
        package_path = "/" + candidate_path
        try:
            local_path = receive_package_file(
                ascli,
                url,
                package_path,
                cache_dir,
                timeout,
                retries,
                retry_sleep,
            )
            parsed_entries = parse_sums_file(local_path)
            if not parsed_entries:
                raise ValueError(f"checksum manifest had no parseable rows: {local_path}")
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            error = {
                "short_title": download["short_title"],
                "download_id": download.get("download_id") or "",
                "package_path": package_path,
                "method": "root_sums",
                "error": exception_summary(exc),
            }
            errors.append(error)
            if fail_fast:
                raise RuntimeError(json.dumps(error, sort_keys=True)) from exc
            continue

        writer.writerow(file_row(download, candidate, browsed_at))
        files += 1
        for entry in parsed_entries:
            writer.writerow(sums_entry_row(download, entry, package_path, browsed_at))
            files += 1

    if not files:
        return None
    directories = sum(
        1
        for entry in root_entries
        if row_value(entry, "entry_type", "type").lower() in {"directory", "folder"}
    )
    return {
        "short_title": download["short_title"],
        "download_id": download.get("download_id") or "",
        "method": "root_sums",
        "files": files,
        "directories": directories,
        "checksum_manifests": len(candidates),
        "errors": errors,
    }


def browse_download(
    ascli: str,
    download: dict[str, Any],
    writer: csv.DictWriter,
    timeout: int,
    max_depth: int,
    sleep_seconds: float,
    workers: int,
    query: dict[str, Any] | None,
    prefer_sums: bool,
    sums_cache_dir: Path,
    retries: int,
    retry_sleep: float,
    recursive: bool,
    fallback_staged: bool,
    fail_fast: bool,
) -> dict[str, Any]:
    url = download["download_url"]
    browsed_at = utc_now()
    if prefer_sums:
        sums_result = try_browse_download_from_sums(
            ascli,
            download,
            writer,
            timeout,
            query,
            retries,
            retry_sleep,
            sums_cache_dir,
            fail_fast,
        )
        if sums_result:
            return sums_result

    if recursive:
        try:
            entries = browse_package_recursive(ascli, url, timeout, query, retries, retry_sleep)
            files = 0
            directories = 0
            for entry in entries:
                entry_type = row_value(entry, "entry_type", "type").lower()
                target_type = row_value(entry, "target_entry_type", "target_type").lower()
                if entry_type in {"directory", "folder"} or target_type in {"directory", "folder"}:
                    directories += 1
                    continue
                if entry_type in {"file", "symbolic_link", "symlink"} or target_type == "file":
                    writer.writerow(file_row(download, entry, browsed_at))
                    files += 1
            return {
                "short_title": download["short_title"],
                "download_id": download.get("download_id") or "",
                "method": "recursive",
                "files": files,
                "directories": directories,
                "errors": [],
            }
        except (subprocess.SubprocessError, OSError) as exc:
            error = {
                "short_title": download["short_title"],
                "download_id": download.get("download_id") or "",
                "package_path": "",
                "method": "recursive",
                "error": exception_summary(exc),
            }
            if fail_fast:
                raise RuntimeError(json.dumps(error, sort_keys=True)) from exc
            if not fallback_staged:
                return {
                    "short_title": download["short_title"],
                    "download_id": download.get("download_id") or "",
                    "method": "recursive",
                    "files": 0,
                    "directories": 0,
                    "errors": [error],
                }
            print(
                f"  recursive browse failed; falling back to staged: {exception_summary(exc)}",
                file=sys.stderr,
                flush=True,
            )

    queue: deque[tuple[str, int]] = deque([("", 0)])
    visited: set[str] = set()
    active: dict[Any, tuple[str, int]] = {}
    files = 0
    directories = 0
    errors: list[dict[str, Any]] = []
    worker_count = max(1, workers)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        while queue or active:
            while queue and len(active) < worker_count:
                package_path, depth = queue.popleft()
                if package_path in visited:
                    continue
                visited.add(package_path)
                future = executor.submit(
                    browse_package_path,
                    ascli,
                    url,
                    package_path,
                    timeout,
                    query,
                    retries,
                    retry_sleep,
                )
                active[future] = (package_path, depth)
            if not active:
                continue
            done, _pending = wait(active.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                package_path, depth = active.pop(future)
                try:
                    entries = future.result()
                except (subprocess.SubprocessError, OSError) as exc:
                    error = {
                        "short_title": download["short_title"],
                        "download_id": download.get("download_id") or "",
                        "package_path": package_path,
                        "error": exception_summary(exc),
                    }
                    errors.append(error)
                    if fail_fast:
                        raise RuntimeError(json.dumps(error, sort_keys=True)) from exc
                    continue
                for entry in entries:
                    entry_path = row_value(entry, "path")
                    entry_type = row_value(entry, "entry_type", "type").lower()
                    target_type = row_value(entry, "target_entry_type", "target_type").lower()
                    if entry_type in {"directory", "folder"} or target_type in {"directory", "folder"}:
                        directories += 1
                        if depth < max_depth:
                            next_path = entry_path or row_value(entry, "target_path")
                            if next_path and next_path not in visited:
                                queue.append((next_path, depth + 1))
                        continue
                    if entry_type in {"file", "symbolic_link", "symlink"} or target_type == "file":
                        writer.writerow(file_row(download, entry, browsed_at))
                        files += 1
                        if files % 5000 == 0:
                            print(
                                f"  {download['short_title']} {download.get('download_id') or ''}: "
                                f"{files} files, {directories} directories queued/listed",
                                file=sys.stderr,
                                flush=True,
                            )
                if sleep_seconds:
                    time.sleep(sleep_seconds)
    return {
        "short_title": download["short_title"],
        "download_id": download.get("download_id") or "",
        "method": "staged",
        "files": files,
        "directories": directories,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-db", default=str(DEFAULT_SNAPSHOT_DB), help="Source TCIA snapshot DB.")
    parser.add_argument("--out", required=True, help="Output TSV path.")
    parser.add_argument("--ascli", default="ascli", help="ascli executable path.")
    parser.add_argument("--collection", help="Filter by TCIA short title.")
    parser.add_argument("--download-id", help="Filter by Collection Manager download ID.")
    parser.add_argument("--limit", type=int, help="Maximum downloads to browse.")
    parser.add_argument("--max-depth", type=int, default=12, help="Maximum package directory depth.")
    parser.add_argument("--timeout", type=int, default=180, help="Per-browse timeout in seconds.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between browse calls.")
    parser.add_argument("--workers", type=int, default=1, help="Maximum concurrent Aspera browse calls per package.")
    parser.add_argument("--retries", type=int, default=2, help="Retry failed Aspera browse/receive calls this many times.")
    parser.add_argument("--retry-sleep", type=float, default=2.0, help="Base seconds to sleep between retries.")
    parser.add_argument(
        "--no-prefer-sums",
        action="store_true",
        help="Do not prefer root checksum manifests before recursive/staged browsing.",
    )
    parser.add_argument(
        "--sums-cache-dir",
        help="Directory for downloaded Aspera checksum manifests. Defaults next to --out.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Use one recursive Aspera package browse per download before staged browsing.",
    )
    parser.add_argument(
        "--no-fallback-staged",
        action="store_true",
        help="Do not fall back to staged directory browsing when recursive browse fails.",
    )
    parser.add_argument(
        "--offset-paging",
        action="store_true",
        help="Use Faspex offset/limit paging for browse calls instead of iteration-token paging.",
    )
    parser.add_argument("--page-limit", type=int, default=1000, help="Rows per offset-paged browse request.")
    parser.add_argument("--summary-out", help="Optional JSON summary path.")
    parser.add_argument("--resume", action="store_true", help="Append to an existing TSV and skip successful downloads from --summary-out.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on the first package browse error.")
    args = parser.parse_args(argv)

    downloads = load_download_rows(
        Path(args.snapshot_db),
        args.collection,
        args.download_id,
        args.limit,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    summary_path = Path(args.summary_out) if args.summary_out else None
    summaries = load_resume_results(summary_path) if args.resume else []
    completed = {
        result_key(result)
        for result in summaries
        if not result.get("errors")
    }
    mode = "a" if args.resume and out.exists() else "w"
    write_header = mode == "w" or not out.exists() or out.stat().st_size == 0
    query = build_browse_query(args.offset_paging, args.page_limit)
    sums_cache_dir = Path(args.sums_cache_dir) if args.sums_cache_dir else out.parent / "pathology_sums_manifests"
    with out.open(mode, encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_COLUMNS, delimiter="\t")
        if write_header:
            writer.writeheader()
        for index, download in enumerate(downloads, 1):
            if download_key(download) in completed:
                print(
                    f"[{index}/{len(downloads)}] skip {download['short_title']} "
                    f"{download.get('download_id') or ''}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            print(
                f"[{index}/{len(downloads)}] {download['short_title']} "
                f"{download.get('download_id') or ''}",
                file=sys.stderr,
                flush=True,
            )
            result = browse_download(
                args.ascli,
                download,
                writer,
                args.timeout,
                args.max_depth,
                args.sleep,
                args.workers,
                query,
                not args.no_prefer_sums,
                sums_cache_dir,
                args.retries,
                args.retry_sleep,
                args.recursive,
                not args.no_fallback_staged,
                args.fail_fast,
            )
            summaries.append(result)
            stream.flush()
            print(
                f"  files={result['files']} directories={result['directories']} "
                f"errors={len(result['errors'])}",
                file=sys.stderr,
                flush=True,
            )
            write_summary(summary_path, len(downloads), summaries, out)

    summary = {
        "downloads": len(downloads),
        "completed_downloads": len(summaries),
        "files": sum(item["files"] for item in summaries),
        "directories": sum(item["directories"] for item in summaries),
        "errors": [error for item in summaries for error in item["errors"]],
        "results": summaries,
        "out": str(out.resolve()),
    }
    if args.summary_out:
        write_summary(summary_path, len(downloads), summaries, out)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not summary["errors"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
