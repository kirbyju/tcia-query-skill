#!/usr/bin/env python3
"""Shallow-browse TCIA Aspera package roots for checksum/summary files."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any


SUMMARY_TOKENS = (
    "sums",
    "checksum",
    "md5",
    "sha1",
    "sha256",
    "sha512",
    "manifest",
)


def safe_slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("_")


def row_value(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value:
            return value
    return ""


def candidate_rows(conn: sqlite3.Connection, failed_only: bool) -> list[sqlite3.Row]:
    status_sql = """
        SELECT h.status
        FROM harvested_files h
        WHERE h.download_row_id = c.download_row_id
          AND h.source_kind IN ('aspera_listing', 'aspera_interactive_listing', 'aspera_browse')
        ORDER BY
          CASE h.status WHEN 'ok' THEN 0 WHEN 'error' THEN 1 ELSE 2 END,
          h.file_id DESC
        LIMIT 1
    """
    where = "c.route = 'aspera'"
    if failed_only:
        where += f" AND COALESCE(({status_sql}), 'not_attempted') != 'ok'"
    return list(
        conn.execute(
            f"""
            SELECT DISTINCT
              c.download_row_id,
              c.short_title,
              c.download_id,
              c.download_title,
              c.download_url,
              COALESCE(({status_sql}), 'not_attempted') AS previous_status
            FROM candidate_downloads c
            WHERE {where}
            ORDER BY c.short_title, c.download_id
            """
        )
    )


def parse_ascli_csv(text: str) -> list[dict[str, str]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    rows = list(csv.reader(lines))
    header = [value.strip().lower() for value in rows[0]]
    known_headers = {"path", "name", "type", "entry_type", "bytes", "size", "modified_at"}
    if known_headers.intersection(header):
        return [dict(row) for row in csv.DictReader(lines)]

    fieldnames = ["path", "name", "entry_type", "bytes", "modified_at", "permissions"]
    output: list[dict[str, str]] = []
    for row in rows:
        record = {
            fieldnames[index] if index < len(fieldnames) else f"extra_{index}": value
            for index, value in enumerate(row)
        }
        output.append(record)
    return output


def checksum_like_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in rows:
        haystack = " ".join(str(value or "") for value in row.values()).lower()
        if not any(token in haystack for token in SUMMARY_TOKENS):
            continue
        matches.append(
            {
                "name_or_path": row_value(row, "path", "Path", "name", "Name", "file", "File"),
                "row": row,
            }
        )
    return matches


def browse_package_path(
    ascli: str, url: str, package_path: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    command = [
        ascli,
        "--format=csv",
        "faspex5",
        "packages",
        "browse",
        f"--url={url}",
    ]
    if package_path:
        command.append(package_path)
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)


def receive_package_file(
    ascli: str, url: str, package_path: str, out_dir: Path, timeout: int
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    before = {
        path.resolve()
        for path in out_dir.rglob(Path(package_path).name)
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
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except Exception:
        if before:
            return max((Path(path) for path in before), key=lambda path: path.stat().st_mtime)
        raise
    candidates = [
        path
        for path in out_dir.rglob(Path(package_path).name)
        if path.is_file() and path.resolve() not in before
    ]
    if not candidates:
        candidates = [path for path in out_dir.rglob(Path(package_path).name) if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"ascli did not produce expected file: {package_path}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def guess_algorithm(checksum: str) -> str:
    length_map = {
        32: "md5",
        40: "sha1",
        64: "sha256",
        128: "sha512",
    }
    return length_map.get(len(checksum), "")


def extension_for_name(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".nii.gz"):
        return ".nii.gz"
    return Path(name).suffix.lower()


def parse_sums_file(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    checksum_re = re.compile(r"^[A-Fa-f0-9]{16,128}$")
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            pieces = line.split(maxsplit=1)
            if len(pieces) != 2:
                entries.append(
                    {
                        "line_number": line_number,
                        "checksum": "",
                        "algorithm": "",
                        "package_path": "",
                        "file_name": "",
                        "file_ext": "",
                        "raw_line": line,
                    }
                )
                continue
            checksum, package_path = pieces
            package_path = package_path.lstrip("*")
            if not checksum_re.match(checksum):
                checksum, package_path = "", line
            entries.append(
                {
                    "line_number": line_number,
                    "checksum": checksum,
                    "algorithm": guess_algorithm(checksum),
                    "package_path": package_path,
                    "file_name": Path(package_path).name,
                    "file_ext": extension_for_name(package_path),
                    "raw_line": line,
                }
            )
    return entries


def write_sums_inventory(out_dir: Path, results: list[dict[str, Any]]) -> int:
    rows: list[list[Any]] = []
    for result in results:
        for match in result.get("summary_matches", []):
            local_path = match.get("local_path")
            if not local_path:
                continue
            for entry in parse_sums_file(Path(local_path)):
                rows.append(
                    [
                        result.get("short_title", ""),
                        result.get("download_id", ""),
                        result.get("download_title", ""),
                        match.get("name_or_path", ""),
                        local_path,
                        entry["line_number"],
                        entry["checksum"],
                        entry["algorithm"],
                        entry["package_path"],
                        entry["file_name"],
                        entry["file_ext"],
                        entry["raw_line"],
                    ]
                )
    with (out_dir / "sums_inventory.tsv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(
            [
                "short_title",
                "download_id",
                "download_title",
                "sums_package_path",
                "local_sums_path",
                "line_number",
                "checksum",
                "algorithm",
                "package_path",
                "file_name",
                "file_ext",
                "raw_line",
            ]
        )
        writer.writerows(rows)
    return len(rows)


def write_summary(out_dir: Path, results: list[dict[str, Any]]) -> None:
    (out_dir / "summary.json").write_text(
        json.dumps(results, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    with (out_dir / "summary.tsv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(
            [
                "short_title",
                "download_id",
                "download_title",
                "previous_status",
                "root_status",
                "root_rows",
                "summary_match_count",
                "summary_matches",
                "root_browse_csv",
                "error",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    result.get("short_title", ""),
                    result.get("download_id", ""),
                    result.get("download_title", ""),
                    result.get("previous_status", ""),
                    result.get("root_status", ""),
                    result.get("root_rows", ""),
                    len(result.get("summary_matches", [])),
                    "; ".join(
                        match.get("name_or_path", "")
                        for match in result.get("summary_matches", [])
                    ),
                    result.get("root_browse_csv", ""),
                    result.get("error", ""),
                ]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("outputs/nifti_metadata/nifti_metadata.sqlite"),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("outputs/nifti_metadata/aspera_root_sums_check"),
    )
    parser.add_argument("--ascli", default="/Users/kirbyju/.rbenv/shims/ascli")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument(
        "--all-aspera",
        action="store_true",
        help="Check all Aspera candidates instead of only non-ok candidates.",
    )
    parser.add_argument(
        "--download-matches",
        action="store_true",
        help="Download root checksum/summary files found by the shallow browse.",
    )
    parser.add_argument(
        "--descend-root-dirs",
        action="store_true",
        help="Also browse immediate root directories for checksum/summary files.",
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    candidates = candidate_rows(conn, failed_only=not args.all_aspera)

    results: list[dict[str, Any]] = []
    for row in candidates:
        slug = safe_slug(f"{row['short_title']}_{row['download_id']}")
        root_csv = args.out_dir / f"{slug}_root_browse.csv"
        result: dict[str, Any] = dict(row)
        result["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        result["root_browse_csv"] = str(root_csv)
        try:
            completed = browse_package_path(args.ascli, row["download_url"], "", args.timeout)
            root_csv.write_text(completed.stdout, encoding="utf-8")
            parsed = parse_ascli_csv(completed.stdout)
            summary_matches = checksum_like_rows(parsed)
            child_browses: list[dict[str, Any]] = []
            if args.descend_root_dirs:
                root_dirs = [
                    entry.get("path", "")
                    for entry in parsed
                    if entry.get("entry_type", "").strip().lower()
                    in {"directory", "dir", "folder"}
                    and entry.get("path", "")
                ]
                for root_dir in root_dirs:
                    child_slug = safe_slug(root_dir.strip("/") or "root")
                    child_csv = args.out_dir / f"{slug}_dir_{child_slug}_browse.csv"
                    child: dict[str, Any] = {
                        "package_path": root_dir,
                        "browse_csv": str(child_csv),
                    }
                    try:
                        child_completed = browse_package_path(
                            args.ascli,
                            row["download_url"],
                            root_dir,
                            args.timeout,
                        )
                        child_csv.write_text(child_completed.stdout, encoding="utf-8")
                        child_rows = parse_ascli_csv(child_completed.stdout)
                        child_matches = checksum_like_rows(child_rows)
                        for match in child_matches:
                            match["browse_path"] = root_dir
                        summary_matches.extend(child_matches)
                        child.update(
                            {
                                "status": "ok",
                                "rows": len(child_rows),
                                "summary_match_count": len(child_matches),
                                "summary_matches": child_matches,
                            }
                        )
                    except Exception as exc:  # noqa: BLE001 - diagnostic script should keep going.
                        child.update({"status": "error", "error": str(exc)})
                    child_browses.append(child)
            result.update(
                {
                    "root_status": "ok",
                    "root_rows": len(parsed),
                    "summary_matches": summary_matches,
                    "child_browses": child_browses,
                    "stderr": completed.stderr[-1000:],
                }
            )
            if args.download_matches:
                sums_dir = args.out_dir / "sums_files" / slug
                for match in result["summary_matches"]:
                    package_path = match.get("name_or_path")
                    if not package_path:
                        match["download_status"] = "skipped"
                        match["download_error"] = "missing package path"
                        continue
                    try:
                        local_path = receive_package_file(
                            args.ascli,
                            row["download_url"],
                            package_path,
                            sums_dir,
                            args.timeout,
                        )
                        match["download_status"] = "ok"
                        match["local_path"] = str(local_path)
                    except Exception as exc:  # noqa: BLE001 - diagnostic script should keep going.
                        match["download_status"] = "error"
                        match["download_error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - diagnostic script should keep going.
            result.update({"root_status": "error", "error": str(exc)})
        (args.out_dir / f"{slug}_summary.json").write_text(
            json.dumps(result, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        results.append(result)

    write_summary(args.out_dir, results)
    inventory_count = write_sums_inventory(args.out_dir, results)
    match_count = sum(len(result.get("summary_matches", [])) for result in results)
    print(f"wrote {args.out_dir / 'summary.tsv'}")
    print(f"wrote {args.out_dir / 'sums_inventory.tsv'}")
    print(f"checked {len(results)} Aspera candidates")
    print(f"checksum/summary-like root matches: {match_count}")
    print(f"sums inventory rows: {inventory_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
