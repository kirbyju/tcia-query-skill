#!/usr/bin/env python3
"""Search or summarize PathDB non-DICOM histopathology snapshot metadata."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

import tcia_snapshot


SUMMARY_COLUMNS = [
    "collection",
    "patients",
    "slides",
    "data_format",
    "modality",
    "cancer_type",
    "cancer_location",
    "species",
    "last_update",
]


def shorten(value: Any, width: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def print_table(records: list[dict[str, Any]], columns: list[str]) -> None:
    if not records:
        print(
            "No PathDB metadata rows matched. If you expected very recent metadata, "
            "try again after the next 7:17 AM or 7:17 PM America/New_York snapshot "
            "run has finished, then rerun `python scripts/tcia_snapshot.py ensure`."
        )
        return

    widths = {column: min(max(len(column), 10), 32) for column in columns}
    for record in records:
        for column in columns:
            widths[column] = min(max(widths[column], len(str(record.get(column, ""))) + 1), 40)

    print(" | ".join(column.ljust(widths[column]) for column in columns))
    print(" | ".join("-" * widths[column] for column in columns))
    for record in records:
        print(" | ".join(shorten(record.get(column, ""), widths[column]).ljust(widths[column]) for column in columns))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", action="append", default=[], help="Exact PathDB collection/TCIA short title.")
    parser.add_argument("--doi", action="append", default=[], help="Exact collection DOI.")
    parser.add_argument("--query", help="Text search across all CSV fields.")
    parser.add_argument("--summary", action="store_true", help="Summarize by collection.")
    parser.add_argument("--limit", type=int, default=25, help="Maximum records to print.")
    parser.add_argument(
        "--snapshot-db",
        help="Optional SQLite snapshot path. Defaults to TCIA_SNAPSHOT_DB or cache/tcia_snapshot.sqlite.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    collections = {value.lower() for value in args.collection}
    dois = {value.lower() for value in args.doi}
    if not tcia_snapshot.snapshot_available(args.snapshot_db):
        print(
            f"No local TCIA snapshot found at {tcia_snapshot.snapshot_path(args.snapshot_db)}. "
            "Run `python scripts/tcia_snapshot.py ensure` from the skill root, then try again.",
            file=sys.stderr,
        )
        return 1

    filtered = tcia_snapshot.pathdb_rows_from_snapshot(
        query=args.query,
        collections=collections,
        dois=dois,
        path=args.snapshot_db,
    )

    if args.summary:
        records = tcia_snapshot.summarize_pathdb_rows(filtered)
        columns = SUMMARY_COLUMNS
    else:
        records = filtered
        columns = [
            "collection",
            "patient_id",
            "slide_id",
            "camic_id",
            "data_format",
            "modality",
            "cancer_type",
            "cancer_location",
            "wsiimage_url",
            "camicroscope_url",
        ]

    records = records[: args.limit]
    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        print_table(records, columns)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"PathDB metadata query failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
