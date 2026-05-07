#!/usr/bin/env python3
"""List TCIA DOI metadata from the local SQLite snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

import tcia_snapshot


DEFAULT_TCIA_PREFIX = "10.7937"


def matches(record: dict[str, Any], query: str | None) -> bool:
    if not query:
        return True
    haystack = " ".join(
        str(record.get(key, ""))
        for key in ("doi", "tcia_short_name", "title", "publisher", "url", "publication_year")
    ).lower()
    return all(term in haystack for term in query.lower().split())


def clean_cell(value: Any, width: int = 72) -> str:
    text = str(value or "").replace("|", "\\|").replace("\n", " ").strip()
    if len(text) > width:
        return text[: width - 3] + "..."
    return text


def print_table(records: list[dict[str, Any]]) -> None:
    if not records:
        print(
            "No DataCite DOI records matched. If you expected very recent metadata, "
            "try again after the next 7:17 AM or 7:17 PM America/New_York snapshot "
            "run has finished, then rerun `python scripts/tcia_snapshot.py ensure`."
        )
        return
    print("TCIA Short Name | DOI | Year | Title | URL")
    print("--- | --- | --- | --- | ---")
    for record in records:
        print(
            f"{clean_cell(record.get('tcia_short_name'), 32)} | "
            f"{clean_cell(record.get('doi'), 32)} | "
            f"{clean_cell(record.get('publication_year'), 8)} | "
            f"{clean_cell(record.get('title'))} | "
            f"{clean_cell(record.get('url'))}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doi", help="Fetch one exact DOI record.")
    parser.add_argument("--prefix", default=DEFAULT_TCIA_PREFIX, help="DOI prefix for TCIA DOI listing.")
    parser.add_argument("--query", help="Local text filter over DOI, TCIA short name, title, publisher, URL, and year.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum records to print; 0 means all matched records.")
    parser.add_argument(
        "--snapshot-db",
        help="Optional SQLite snapshot path. Defaults to TCIA_SNAPSHOT_DB or cache/tcia_snapshot.sqlite.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    if args.prefix != DEFAULT_TCIA_PREFIX:
        print("Only the TCIA DOI prefix 10.7937 is available from the local snapshot.", file=sys.stderr)
        return 1
    if not tcia_snapshot.snapshot_available(args.snapshot_db):
        print(
            f"No local TCIA snapshot found at {tcia_snapshot.snapshot_path(args.snapshot_db)}. "
            "Run `python scripts/tcia_snapshot.py ensure` from the skill root, then try again.",
            file=sys.stderr,
        )
        return 1

    records = tcia_snapshot.datacite_records_from_snapshot(
        doi=args.doi,
        prefix=args.prefix,
        query=args.query,
        path=args.snapshot_db,
    )

    records = [record for record in records if matches(record, args.query)]
    if args.limit > 0:
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
        print(f"DataCite DOI query failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
