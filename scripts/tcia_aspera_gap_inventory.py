#!/usr/bin/env python3
"""Live-browse TCIA Aspera packages that still lack NIfTI file inventories."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import tcia_nifti_metadata_harvest as harvest


def safe_slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("_")


def candidate_local_dir(files_dir: Path, row: sqlite3.Row) -> Path:
    return files_dir / safe_slug(row["short_title"] or "dataset") / str(row["download_id"] or "download")


def write_rows_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def row_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    nifti = 0
    metadata = 0
    directories = 0
    for row in rows:
        path = harvest.package_row_path(row)
        if harvest.is_plausible_nifti_file(path):
            nifti += 1
        if harvest.aspera_file_is_metadata(path):
            metadata += 1
        if harvest.package_row_is_directory(row):
            directories += 1
    return {"rows": len(rows), "nifti_rows": nifti, "metadata_candidate_rows": metadata, "directories": directories}


def browseable_package_path(row: dict[str, str]) -> str:
    for key in ("raw_0", "Path", "path", "Name", "name"):
        value = str(row.get(key) or "").strip()
        if value.startswith("/") and "view, edit, delete" not in value.lower():
            return value
    path = harvest.package_row_path(row)
    if path and harvest.package_row_is_directory(row) and not path.startswith("/"):
        return f"/{path}"
    return path


def browse_root(
    ascli: str,
    url: str,
    timeout: int,
    query: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    return harvest.browse_aspera_path(ascli, url, "", timeout, query)


def browse_recursive(
    ascli: str,
    url: str,
    timeout: int,
    query: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    return harvest.browse_aspera_package(ascli, url, timeout, query)


def browse_staged(
    ascli: str,
    url: str,
    timeout: int,
    max_dirs: int,
    max_depth: int,
    initial_root_rows: list[dict[str, str]] | None = None,
    seed_paths: list[str] | None = None,
    verbose_paths: bool = False,
    query: dict[str, Any] | None = None,
) -> tuple[list[dict[str, str]], bool, list[dict[str, Any]]]:
    output: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    seeds = [path.strip() for path in seed_paths or [] if path.strip()]
    queue: list[tuple[str, int]] = [(path, 0) for path in seeds] if seeds else [("", 0)]
    seen_dirs = set(seeds) if seeds else {""}
    browsed_dirs = 0

    while queue and browsed_dirs < max_dirs:
        package_path, depth = queue.pop(0)
        try:
            if verbose_paths:
                print(f"  browse {package_path or '<root>'}", flush=True)
            if package_path == "" and initial_root_rows is not None:
                rows = initial_root_rows
            else:
                rows = harvest.browse_aspera_path(ascli, url, package_path, timeout, query)
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic script.
            errors.append({"package_path": package_path, "depth": depth, "error": str(exc)})
            continue
        browsed_dirs += 1
        for row in rows:
            if package_path:
                row = {**row, "_browsed_path": package_path}
            output.append(row)
            path = browseable_package_path(row)
            if (
                not path
                or not harvest.package_row_is_directory(row)
                or path in seen_dirs
                or depth >= max_depth
            ):
                continue
            seen_dirs.add(path)
            queue.append((path, depth + 1))

    complete = not queue and not errors
    if queue:
        errors.append({"package_path": "", "depth": 0, "error": f"stopped with {len(queue)} directories still queued"})
    return output, complete, errors


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def candidate_rows(
    conn: sqlite3.Connection,
    download_ids: list[str],
    include_yale: bool,
    include_sums_covered: bool,
) -> list[sqlite3.Row]:
    filters = ["c.route = 'aspera'", "c.candidate_kind = 'nifti_aspera_package'"]
    params: list[Any] = []
    if download_ids:
        filters.append(f"c.download_id IN ({','.join('?' for _ in download_ids)})")
        params.extend(download_ids)
    else:
        filters.append(
            """
            NOT EXISTS (
              SELECT 1
              FROM package_files p
              WHERE p.download_row_id = c.download_row_id
                AND p.file_ext IN ('.nii', '.nii.gz')
            )
            """
        )
        if not include_sums_covered:
            filters.append(
                """
                NOT EXISTS (
                  SELECT 1
                  FROM aspera_root_sums_inventory s
                  WHERE s.short_title = c.short_title
                    AND s.download_id = c.download_id
                    AND s.file_ext IN ('.nii', '.nii.gz')
                )
                """
            )
        if not include_yale:
            filters.append("c.short_title != 'Yale-Brain-Mets-Longitudinal'")

    where = " AND ".join(f"({item})" for item in filters)
    return list(
        conn.execute(
            f"""
            SELECT c.*
            FROM candidate_downloads c
            WHERE {where}
            ORDER BY c.short_title, c.download_id
            """,
            params,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("outputs/nifti_metadata/nifti_metadata.sqlite"))
    parser.add_argument("--files-dir", type=Path, default=Path("outputs/nifti_metadata/files"))
    parser.add_argument("--out-dir", type=Path, default=Path("outputs/nifti_metadata/aspera_gap_inventory"))
    parser.add_argument("--ascli", default="/Users/kirbyju/.rbenv/shims/ascli")
    parser.add_argument("--download-id", action="append", default=[])
    parser.add_argument("--include-yale", action="store_true")
    parser.add_argument("--include-sums-covered", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-recursive",
        action="store_true",
        help="Skip recursive package browse and use staged directory browsing directly.",
    )
    parser.add_argument(
        "--no-fallback-staged",
        action="store_true",
        help="Do not fall back to staged directory browsing when recursive browse fails.",
    )
    parser.add_argument("--root-timeout", type=int, default=120)
    parser.add_argument("--recursive-timeout", type=int, default=900)
    parser.add_argument("--staged-timeout", type=int, default=180)
    parser.add_argument("--staged-max-dirs", type=int, default=10000)
    parser.add_argument("--staged-max-depth", type=int, default=8)
    parser.add_argument(
        "--offset-paging",
        action="store_true",
        help="Use offset/limit paging instead of Faspex iteration-token paging for folder browse calls.",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=500,
        help="Rows per offset-paged browse request when --offset-paging is used.",
    )
    parser.add_argument(
        "--seed-path",
        action="append",
        default=[],
        help="Start staged browsing from a known package path instead of discovering from root.",
    )
    parser.add_argument(
        "--seed-path-file",
        type=Path,
        help="Text file containing one seed package path per line.",
    )
    parser.add_argument("--verbose-paths", action="store_true")
    args = parser.parse_args()

    conn = connect(args.db)
    candidates = candidate_rows(
        conn,
        [str(value) for value in args.download_id],
        args.include_yale,
        args.include_sums_covered,
    )
    if args.dry_run:
        for row in candidates:
            print(f"{row['short_title']}\t{row['download_id']}\t{row['download_url']}")
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    seed_paths = [str(value) for value in args.seed_path]
    if args.seed_path_file:
        seed_paths.extend(
            line.strip()
            for line in args.seed_path_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    browse_query = {"paging": False, "limit": args.page_limit} if args.offset_paging else None
    summaries: list[dict[str, Any]] = []
    for index, row in enumerate(candidates, 1):
        label = f"{row['short_title']} {row['download_id']}"
        print(f"[{index}/{len(candidates)}] {label}", flush=True)
        slug = safe_slug(f"{row['short_title']}_{row['download_id']}")
        row_dir = args.out_dir / slug
        row_dir.mkdir(parents=True, exist_ok=True)
        summary: dict[str, Any] = {
            "short_title": row["short_title"],
            "download_id": row["download_id"],
            "download_row_id": row["download_row_id"],
            "download_url": row["download_url"],
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "status": "started",
        }
        rows: list[dict[str, str]] = []
        root_rows: list[dict[str, str]] = []
        try:
            if seed_paths:
                summary["root"] = {"rows": 0, "nifti_rows": 0, "metadata_candidate_rows": 0, "directories": 0}
            else:
                root_rows = browse_root(args.ascli, row["download_url"], args.root_timeout, browse_query)
                write_rows_csv(row_dir / "root_browse.csv", root_rows)
                summary["root"] = row_counts(root_rows)
        except Exception as exc:  # noqa: BLE001
            summary["root_error"] = str(exc)

        try:
            if args.skip_recursive:
                raise RuntimeError("recursive browse skipped by --skip-recursive")
            rows = browse_recursive(args.ascli, row["download_url"], args.recursive_timeout, browse_query)
            write_rows_csv(row_dir / "recursive_listing.csv", rows)
            summary["status"] = "ok"
            summary["method"] = "recursive"
            summary["listing"] = row_counts(rows)
        except Exception as recursive_exc:  # noqa: BLE001
            summary["recursive_error"] = str(recursive_exc)
            if args.no_fallback_staged:
                summary["status"] = "error"
                summary["method"] = "recursive"
                summary["listing"] = row_counts(rows)
            else:
                rows, complete, errors = browse_staged(
                    args.ascli,
                    row["download_url"],
                    args.staged_timeout,
                    args.staged_max_dirs,
                    args.staged_max_depth,
                    root_rows or None,
                    seed_paths,
                    args.verbose_paths,
                    browse_query,
                )
                write_rows_csv(row_dir / "staged_listing.csv", rows)
                summary["status"] = "ok" if complete else "partial"
                summary["method"] = "staged"
                summary["listing"] = row_counts(rows)
                summary["staged_complete"] = complete
                summary["staged_errors"] = errors

        if summary["status"] == "ok":
            cache_listing = candidate_local_dir(args.files_dir, row) / "aspera_package_listing.csv"
            write_rows_csv(cache_listing, rows)
            summary["cache_listing"] = str(cache_listing)
        summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        (row_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        summaries.append(summary)

    summary_tsv = args.out_dir / "summary.tsv"
    with summary_tsv.open("w", newline="", encoding="utf-8") as stream:
        fieldnames = [
            "short_title",
            "download_id",
            "status",
            "method",
            "rows",
            "nifti_rows",
            "metadata_candidate_rows",
            "directories",
            "cache_listing",
            "recursive_error",
        ]
        writer = csv.DictWriter(stream, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        for summary in summaries:
            listing = summary.get("listing", {})
            writer.writerow(
                {
                    "short_title": summary.get("short_title", ""),
                    "download_id": summary.get("download_id", ""),
                    "status": summary.get("status", ""),
                    "method": summary.get("method", ""),
                    "rows": listing.get("rows", ""),
                    "nifti_rows": listing.get("nifti_rows", ""),
                    "metadata_candidate_rows": listing.get("metadata_candidate_rows", ""),
                    "directories": listing.get("directories", ""),
                    "cache_listing": summary.get("cache_listing", ""),
                    "recursive_error": summary.get("recursive_error", ""),
                }
            )
    print(f"wrote {summary_tsv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
