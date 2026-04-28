#!/usr/bin/env python3
"""Find DataCite works related to a DOI, defaulting to IsDerivedFrom."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from typing import Any, Optional


DATACITE_WORKS_URL = "https://api.datacite.org/works"


def fetch_related(doi: str, relation: str, rows: int) -> list[dict[str, Any]]:
    query = (
        "relatedIdentifiers.relatedIdentifierType:DOI AND "
        f"relatedIdentifiers.relatedIdentifier:{doi} AND "
        f"relatedIdentifiers.relationType:{relation}"
    )
    params = {"query": query, "page[size]": str(rows)}
    url = f"{DATACITE_WORKS_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "tcia-query-skill/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("data") or []


def normalize(work: dict[str, Any]) -> dict[str, Any]:
    attrs = work.get("attributes") or {}
    titles = attrs.get("titles") or []
    title = attrs.get("title") or ""
    if not title and titles:
        title = titles[0].get("title", "")
    publisher = attrs.get("publisher") or attrs.get("container-title")
    doi = attrs.get("doi") or work.get("id")
    url = attrs.get("url")
    related = attrs.get("relatedIdentifiers") or []
    return {
        "doi": doi,
        "title": title,
        "publisher": publisher,
        "url": url,
        "created": attrs.get("created"),
        "updated": attrs.get("updated"),
        "related_identifiers": related,
    }


def clean_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def print_table(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No related DataCite works found.")
        return
    print("DOI | Title | Publisher | URL")
    print("--- | --- | --- | ---")
    for record in records:
        print(
            f"{clean_cell(record.get('doi', ''))} | "
            f"{clean_cell(record.get('title', ''))} | "
            f"{clean_cell(record.get('publisher', ''))} | "
            f"{clean_cell(record.get('url', ''))}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("doi", help="Source DOI to look up.")
    parser.add_argument("--relation", default="IsDerivedFrom", help="DataCite relation type.")
    parser.add_argument("--rows", type=int, default=1000, help="Maximum rows to request.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    records = [normalize(work) for work in fetch_related(args.doi, args.relation, args.rows)]
    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        print_table(records)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"DataCite query failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
