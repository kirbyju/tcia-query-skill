#!/usr/bin/env python3
"""Search TCIA WordPress Collection and Analysis Result metadata."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import textwrap
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Optional


BASE_URL = "https://cancerimagingarchive.net/api/v1/"

COLLECTION_FIELDS = [
    "id",
    "link",
    "title",
    "collection_title",
    "collection_short_title",
    "collection_doi",
    "collection_status",
    "collection_page_accessibility",
    "collection_summary",
    "detailed_description",
    "cancer_types",
    "cancer_locations",
    "data_types",
    "species",
    "subjects",
    "program",
    "date_updated",
    "supporting_data",
    "collection_download_info",
    "collection_downloads",
    "additional_resources",
]

ANALYSIS_FIELDS = [
    "id",
    "link",
    "title",
    "result_title",
    "result_short_title",
    "result_doi",
    "result_page_accessibility",
    "result_summary",
    "detailed_description",
    "collections",
    "cancer_types",
    "cancer_locations",
    "data_types",
    "species",
    "subjects",
    "program",
    "date_updated",
    "supporting_data",
    "result_download_info",
    "result_downloads",
    "additional_resources",
]


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def get_data(self) -> str:
        return " ".join(self.parts)


def strip_html(value: Any) -> str:
    text = stringify(value)
    parser = _Stripper()
    parser.feed(text)
    return re.sub(r"\s+", " ", html.unescape(parser.get_data() or text)).strip()


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "; ".join(stringify(item) for item in value if stringify(item))
    if isinstance(value, dict):
        if "rendered" in value:
            return stringify(value["rendered"])
        if "title" in value:
            return stringify(value["title"])
        if "name" in value:
            return stringify(value["name"])
        if "label" in value:
            return stringify(value["label"])
        return "; ".join(
            f"{key}: {stringify(item)}" for key, item in value.items() if stringify(item)
        )
    return str(value)


def fetch_all(endpoint: str, fields: list[str], per_page: int = 100) -> list[dict[str, Any]]:
    params = {"per_page": str(per_page), "_fields": ",".join(fields)}
    url = f"{BASE_URL}{endpoint}/?{urllib.parse.urlencode(params)}"
    records: list[dict[str, Any]] = []

    while url:
        request = urllib.request.Request(url, headers={"User-Agent": "tcia-query-skill/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            records.extend(json.loads(response.read().decode("utf-8")))
            link = response.headers.get("Link", "")
        url = next_link(link)
    return records


def next_link(link_header: str) -> Optional[str]:
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' not in section:
            continue
        match = re.search(r"<([^>]+)>", section)
        if match:
            return match.group(1)
    return None


def normalize(item: dict[str, Any], kind: str) -> dict[str, Any]:
    is_collection = kind == "collection"
    title_key = "collection_title" if is_collection else "result_title"
    short_key = "collection_short_title" if is_collection else "result_short_title"
    doi_key = "collection_doi" if is_collection else "result_doi"
    access_key = "collection_page_accessibility" if is_collection else "result_page_accessibility"
    summary_key = "collection_summary" if is_collection else "result_summary"
    downloads_key = "collection_downloads" if is_collection else "result_downloads"
    download_info_key = "collection_download_info" if is_collection else "result_download_info"

    title = strip_html(item.get(title_key)) or strip_html(item.get("title"))
    short_title = strip_html(item.get(short_key))
    doi = strip_html(item.get(doi_key))
    access = strip_html(item.get(access_key)) or strip_html(item.get("collection_status"))
    summary = strip_html(item.get(summary_key))

    record = {
        "type": "Collection" if is_collection else "Analysis Result",
        "short_title": short_title,
        "title": title,
        "doi": doi,
        "link": item.get("link", ""),
        "access": access,
        "subjects": strip_html(item.get("subjects")),
        "data_types": strip_html(item.get("data_types")),
        "cancer_types": strip_html(item.get("cancer_types")),
        "cancer_locations": strip_html(item.get("cancer_locations")),
        "species": strip_html(item.get("species")),
        "program": strip_html(item.get("program")),
        "date_updated": strip_html(item.get("date_updated")),
        "supporting_data": strip_html(item.get("supporting_data")),
        "download_info": strip_html(item.get(download_info_key)),
        "downloads": strip_html(item.get(downloads_key)),
        "summary": summary,
    }
    record["_search_text"] = " ".join(strip_html(value) for value in item.values()).lower()
    return record


def matches(record: dict[str, Any], query: Optional[str], short_titles: set[str]) -> bool:
    if short_titles and record["short_title"].lower() not in short_titles:
        return False
    if query:
        terms = [term.lower() for term in query.split() if term.strip()]
        haystack = " ".join(str(value).lower() for value in record.values())
        return all(term in haystack for term in terms)
    return True


def print_table(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No TCIA WordPress Collections or Analysis Results matched.")
        return

    headers = ["type", "short_title", "title", "access", "doi", "link"]
    widths = {
        "type": 15,
        "short_title": 22,
        "title": 44,
        "access": 18,
        "doi": 26,
        "link": 45,
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
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = parser.parse_args(argv)

    raw_records: list[dict[str, Any]] = []
    if args.type in {"both", "collections"}:
        raw_records.extend(normalize(item, "collection") for item in fetch_all("collections", COLLECTION_FIELDS))
    if args.type in {"both", "analysis-results"}:
        raw_records.extend(
            normalize(item, "analysis") for item in fetch_all("analysis-results", ANALYSIS_FIELDS)
        )

    short_titles = {title.lower() for title in args.short_title}
    records = [record for record in raw_records if matches(record, args.query, short_titles)]
    records = records[: args.limit]

    for record in records:
        record.pop("_search_text", None)

    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        print_table(records)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        print(f"Network or API error: {exc}", file=sys.stderr)
        raise SystemExit(2)
