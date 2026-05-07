#!/usr/bin/env python3
"""Search local TCIA WordPress Collection and Analysis Result snapshot metadata."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from typing import Any, Optional

import tcia_snapshot


def print_table(records: list[dict[str, Any]]) -> None:
    if not records:
        print(
            "No TCIA WordPress Collections or Analysis Results matched. "
            "If you expected a very recent dataset, try again after the next "
            "7:17 AM or 7:17 PM America/New_York snapshot run has finished, "
            "then rerun `python scripts/tcia_snapshot.py ensure`."
        )
        return

    headers = ["type", "short_title", "title", "license_status", "doi", "link"]
    if any(record.get("controlled_access") for record in records):
        headers.append("controlled_access")
    if any(record.get("hidden") for record in records):
        headers.append("hidden")
    widths = {
        "type": 15,
        "short_title": 22,
        "title": 44,
        "license_status": 32,
        "doi": 26,
        "link": 45,
        "controlled_access": 17,
        "hidden": 8,
    }

    print(" | ".join(label.replace("_", " ").title().ljust(widths[label]) for label in headers))
    print(" | ".join("-" * widths[label] for label in headers))
    for record in records:
        cells = []
        for label in headers:
            value = str(record.get(label, "") or "")
            value = textwrap.shorten(value, width=widths[label], placeholder="...")
            cells.append(value.ljust(widths[label]))
        print(" | ".join(cells))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", help="Local text search across WordPress metadata.")
    parser.add_argument(
        "--short-title",
        action="append",
        default=[],
        help="Exact collection_short_title or result_short_title. Repeatable.",
    )
    parser.add_argument(
        "--type",
        choices=["both", "collections", "analysis-results"],
        default="both",
        help="Metadata type to search.",
    )
    parser.add_argument("--limit", type=int, default=25, help="Maximum records to display.")
    parser.add_argument(
        "--snapshot-db",
        help="Optional SQLite snapshot path. Defaults to TCIA_SNAPSHOT_DB or cache/tcia_snapshot.sqlite.",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help=(
            "Include WordPress records where hide_from_browse_table is 1. "
            "Use only for explicit TCIA staff requests involving hidden, staged, or retired datasets."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = parser.parse_args(argv)

    if not tcia_snapshot.snapshot_available(args.snapshot_db):
        print(
            f"No local TCIA snapshot found at {tcia_snapshot.snapshot_path(args.snapshot_db)}. "
            "Run `python scripts/tcia_snapshot.py ensure` from the skill root, then try again.",
            file=sys.stderr,
        )
        return 1
    short_titles = {title.lower() for title in args.short_title}
    records = tcia_snapshot.search_wordpress_records(
        query=args.query,
        short_titles=short_titles,
        type_filter=args.type,
        include_hidden=args.include_hidden,
        path=args.snapshot_db,
    )
    records = records[: args.limit]

    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        print_table(records)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"TCIA WordPress snapshot search failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
