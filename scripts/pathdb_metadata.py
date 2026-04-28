#!/usr/bin/env python3
"""Search or summarize PathDB non-DICOM histopathology metadata."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
from collections import defaultdict
from typing import Any, Optional


COHORT_BUILDER_CSV_URL = (
    "https://pathdb.cancerimagingarchive.net/system/files/collectionmetadata/202401/"
    "cohort_builder_v1_01-16-2024.csv"
)

SUMMARY_COLUMNS = [
    "collection",
    "collection_doi",
    "patients",
    "slides",
    "data_format",
    "modality",
    "cancer_type",
    "cancer_location",
    "species",
    "has_radiology",
    "has_genomics",
    "has_proteomics",
    "last_update",
]


def fetch_rows(url: str) -> list[dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": "tcia-query-skill/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        text = response.read().decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def unique_join(values: set[str], max_items: int = 4) -> str:
    clean = sorted(value for value in values if value)
    if len(clean) > max_items:
        return "; ".join(clean[:max_items]) + f"; +{len(clean) - max_items} more"
    return "; ".join(clean)


def summarize(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row.get("collection", "")].append(row)

    summaries: list[dict[str, Any]] = []
    for collection, group in sorted(grouped.items()):
        summaries.append(
            {
                "collection": collection,
                "collection_doi": unique_join({row.get("collection_doi", "") for row in group}, 2),
                "patients": len({row.get("patient_id", "") for row in group if row.get("patient_id")}),
                "slides": len({row.get("slide_id", "") for row in group if row.get("slide_id")}),
                "data_format": unique_join({row.get("data_format", "") for row in group}),
                "modality": unique_join({row.get("modality", "") for row in group}),
                "cancer_type": unique_join({row.get("cancer_type", "") for row in group}),
                "cancer_location": unique_join({row.get("cancer_location", "") for row in group}),
                "species": unique_join({row.get("species", "") for row in group}),
                "has_radiology": unique_join({row.get("has_radiology", "") for row in group}, 2),
                "has_genomics": unique_join({row.get("has_genomics", "") for row in group}, 2),
                "has_proteomics": unique_join({row.get("has_proteomics", "") for row in group}, 2),
                "last_update": max((row.get("update", "") for row in group), default=""),
            }
        )
    return summaries


def matches(
    row: dict[str, str],
    query: Optional[str],
    collections: set[str],
    dois: set[str],
) -> bool:
    if collections and row.get("collection", "").lower() not in collections:
        return False
    if dois and row.get("collection_doi", "").lower() not in dois:
        return False
    if query:
        haystack = " ".join(str(value).lower() for value in row.values())
        return all(term in haystack for term in query.lower().split())
    return True


def shorten(value: Any, width: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def print_table(records: list[dict[str, Any]], columns: list[str]) -> None:
    if not records:
        print("No PathDB metadata rows matched.")
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
    parser.add_argument("--url", default=COHORT_BUILDER_CSV_URL, help="PathDB cohort-builder CSV URL.")
    parser.add_argument("--collection", action="append", default=[], help="Exact PathDB collection/TCIA short title.")
    parser.add_argument("--doi", action="append", default=[], help="Exact collection DOI.")
    parser.add_argument("--query", help="Text search across all CSV fields.")
    parser.add_argument("--summary", action="store_true", help="Summarize by collection.")
    parser.add_argument("--limit", type=int, default=25, help="Maximum records to print.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    rows = fetch_rows(args.url)
    collections = {value.lower() for value in args.collection}
    dois = {value.lower() for value in args.doi}
    filtered = [row for row in rows if matches(row, args.query, collections, dois)]

    if args.summary:
        records = summarize(filtered)
        columns = SUMMARY_COLUMNS
    else:
        records = filtered
        columns = [
            "collection",
            "collection_doi",
            "patient_id",
            "slide_id",
            "data_format",
            "modality",
            "cancer_type",
            "cancer_location",
            "wsiimage_url",
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
