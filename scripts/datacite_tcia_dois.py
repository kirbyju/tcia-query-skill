#!/usr/bin/env python3
"""List TCIA DOI metadata from the DataCite public API."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from typing import Any, Optional


DATACITE_DOIS_URL = "https://api.datacite.org/dois"
DEFAULT_TCIA_PREFIX = "10.7937"
USER_AGENT = "tcia-query-skill/1.0 (https://github.com/kirbyju/tcia-query-skill)"


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def doi_url(params: dict[str, str]) -> str:
    return f"{DATACITE_DOIS_URL}?{urllib.parse.urlencode(params)}"


def fetch_doi(doi: str) -> dict[str, Any]:
    encoded = urllib.parse.quote(doi.strip(), safe="")
    payload = fetch_json(f"{DATACITE_DOIS_URL}/{encoded}")
    return payload.get("data") or {}


def fetch_prefix(prefix: str, max_records: int, page_size: int) -> list[dict[str, Any]]:
    url = doi_url({"prefix": prefix, "page[size]": str(page_size)})
    records: list[dict[str, Any]] = []

    while url:
        payload = fetch_json(url)
        records.extend(payload.get("data") or [])
        if max_records > 0 and len(records) >= max_records:
            return records[:max_records]
        url = (payload.get("links") or {}).get("next")

    return records


def first_title(attrs: dict[str, Any], title_type: str | None = None) -> str:
    for title in attrs.get("titles") or []:
        if title_type is None and not title.get("titleType"):
            return title.get("title", "")
        if title_type is not None and title.get("titleType") == title_type:
            return title.get("title", "")
    return ""


def tcia_short_name(attrs: dict[str, Any]) -> str:
    for identifier in attrs.get("identifiers") or []:
        if str(identifier.get("identifierType", "")).lower() == "tcia short name":
            return identifier.get("identifier", "")
    return first_title(attrs, "AlternativeTitle")


def normalize(work: dict[str, Any]) -> dict[str, Any]:
    attrs = work.get("attributes") or {}
    return {
        "doi": attrs.get("doi") or work.get("id", ""),
        "tcia_short_name": tcia_short_name(attrs),
        "title": first_title(attrs) or attrs.get("title") or first_title(attrs, "AlternativeTitle"),
        "publisher": attrs.get("publisher", ""),
        "publication_year": attrs.get("publicationYear", ""),
        "version": attrs.get("version", ""),
        "url": attrs.get("url", ""),
        "state": attrs.get("state", ""),
        "created": attrs.get("created", ""),
        "updated": attrs.get("updated", ""),
        "rights": attrs.get("rightsList") or [],
        "related_identifiers": attrs.get("relatedIdentifiers") or [],
    }


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
        print("No DataCite DOI records matched.")
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
    parser.add_argument("--max-records", type=int, default=1000, help="Maximum DataCite records to fetch; 0 means all.")
    parser.add_argument("--page-size", type=int, default=100, help="DataCite page size.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum records to print; 0 means all matched records.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    if args.doi:
        records = [normalize(fetch_doi(args.doi))]
    else:
        records = [normalize(work) for work in fetch_prefix(args.prefix, args.max_records, args.page_size)]

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
