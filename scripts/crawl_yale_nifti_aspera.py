#!/usr/bin/env python3
"""Crawl the Yale-Brain-Mets-Longitudinal NIfTI Aspera package with paged browse."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import tcia_nifti_metadata_harvest as harvest


YALE_PACKAGE_ROOT = "/Yale-Brain-Mets-Longitudinal"


def package_path(row: dict[str, str]) -> str:
    path = harvest.package_row_path(row)
    return f"/{path.lstrip('/')}" if path else ""


def is_directory(row: dict[str, str]) -> bool:
    return harvest.package_row_is_directory(row)


def browse(
    ascli: str,
    url: str,
    path: str,
    timeout: int,
    query: dict[str, Any],
    retries: int,
    retry_sleep: float,
) -> list[dict[str, str]]:
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return harvest.browse_aspera_path(ascli, url, path, timeout, query)
        except (subprocess.SubprocessError, OSError) as exc:
            last_exc = exc
            if attempt >= retries:
                break
            if retry_sleep:
                time.sleep(retry_sleep * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"browse failed without exception: {path}")


def browse_many(
    label: str,
    ascli: str,
    url: str,
    paths: list[str],
    timeout: int,
    query: dict[str, Any],
    workers: int,
    retries: int,
    retry_sleep: float,
    progress_every: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    pending = list(paths)
    active: dict[Any, str] = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        while pending or active:
            while pending and len(active) < max(1, workers):
                path = pending.pop(0)
                future = executor.submit(
                    browse,
                    ascli,
                    url,
                    path,
                    timeout,
                    query,
                    retries,
                    retry_sleep,
                )
                active[future] = path
            done, _ = wait(active.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                path = active.pop(future)
                completed += 1
                try:
                    browsed_rows = future.result()
                    rows.extend({**row, "_browsed_path": path} for row in browsed_rows)
                except Exception as exc:  # noqa: BLE001 - maintainer crawl should keep going.
                    errors.append({"path": path, "error": str(exc)})
                if completed % progress_every == 0 or completed == len(paths):
                    print(
                        f"{label}: {completed}/{len(paths)} browsed, "
                        f"rows={len(rows)}, errors={len(errors)}",
                        file=sys.stderr,
                        flush=True,
                    )
    return rows, errors


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="TCIA Faspex package URL.")
    parser.add_argument("--out", required=True, type=Path, help="Output Aspera listing CSV.")
    parser.add_argument("--summary-out", required=True, type=Path, help="Output summary JSON.")
    parser.add_argument("--ascli", default="ascli")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--page-limit", type=int, default=1000)
    parser.add_argument("--progress-every", type=int, default=100)
    args = parser.parse_args()

    query = {"paging": False, "limit": args.page_limit}
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    all_rows: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    root_rows = browse(args.ascli, args.url, "", args.timeout, query, args.retries, args.retry_sleep)
    all_rows.extend(root_rows)
    top_rows = browse(
        args.ascli,
        args.url,
        YALE_PACKAGE_ROOT,
        args.timeout,
        query,
        args.retries,
        args.retry_sleep,
    )
    all_rows.extend({**row, "_browsed_path": YALE_PACKAGE_ROOT} for row in top_rows)
    patient_paths = [package_path(row) for row in top_rows if is_directory(row)]
    patient_paths = [path for path in patient_paths if path]
    print(f"patients: {len(patient_paths)}", file=sys.stderr, flush=True)

    patient_rows, patient_errors = browse_many(
        "patient dirs",
        args.ascli,
        args.url,
        patient_paths,
        args.timeout,
        query,
        args.workers,
        args.retries,
        args.retry_sleep,
        args.progress_every,
    )
    all_rows.extend(patient_rows)
    errors.extend(patient_errors)
    date_paths = [package_path(row) for row in patient_rows if is_directory(row)]
    date_paths = [path for path in date_paths if path]
    print(f"date dirs: {len(date_paths)}", file=sys.stderr, flush=True)

    date_rows, date_errors = browse_many(
        "date dirs",
        args.ascli,
        args.url,
        date_paths,
        args.timeout,
        query,
        args.workers,
        args.retries,
        args.retry_sleep,
        args.progress_every,
    )
    all_rows.extend(date_rows)
    errors.extend(date_errors)

    write_rows(args.out, all_rows)
    nifti_rows = [
        row for row in all_rows if harvest.is_plausible_nifti_file(harvest.package_row_path(row))
    ]
    summary = {
        "started_at": started,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rows": len(all_rows),
        "nifti_rows": len(nifti_rows),
        "directories": sum(1 for row in all_rows if is_directory(row)),
        "patient_dirs": len(patient_paths),
        "date_dirs": len(date_paths),
        "errors": errors,
        "out": str(args.out),
    }
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
