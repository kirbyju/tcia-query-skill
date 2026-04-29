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
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any, Optional


BASE_URL = "https://cancerimagingarchive.net/api/"
CONTROLLED_ACCESS_POLICY_URL = (
    "https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/"
)
CONTROLLED_LICENSE_TERMS = [
    "controlled access",
    "nih controlled",
    "tcia restricted",
    "restricted",
    "data usage agreement",
    "dbgap",
]
CREATIVE_COMMONS_TERMS = ["creative commons", "cc by", "cc-by"]
NONCOMMERCIAL_TERMS = ["noncommercial", "non-commercial", "cc by-nc", "cc-by-nc"]

COLLECTION_FIELDS = [
    "id",
    "slug",
    "url",
    "link",
    "title",
    "collection_title",
    "collection_short_title",
    "collection_doi",
    "collection_status",
    "collection_summary",
    "collection_abstract",
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
    "external_resources",
    "additional_resources",
    "hide_from_browse_table",
]

ANALYSIS_FIELDS = [
    "id",
    "slug",
    "url",
    "link",
    "title",
    "result_title",
    "result_short_title",
    "result_doi",
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
    "external_resources",
    "additional_resources",
    "hide_from_browse_table",
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


def fetch_all(
    endpoint: str,
    fields: list[str],
    api_version: str = "v2",
    per_page: int = 100,
    verbose: bool = False,
    search: Optional[str] = None,
    workers: int = 4,
) -> list[dict[str, Any]]:
    field_param = "fields" if api_version == "v2" else "_fields"
    params = {"per_page": str(per_page), field_param: ",".join(fields)}
    if search:
        params["search"] = search
    if api_version == "v2" and verbose:
        params["v"] = "1"
    url = f"{BASE_URL}{api_version}/{endpoint}/?{urllib.parse.urlencode(params)}"
    records: list[dict[str, Any]] = []

    payload, link = fetch_json(url)
    if isinstance(payload, dict) and "results" in payload:
        records.extend(payload["results"])
        total_pages = int(payload.get("total_pages") or 1)
        page_urls = [
            v2_page_url(api_version, endpoint, params, page)
            for page in range(2, total_pages + 1)
        ]
        if page_urls:
            if workers <= 1:
                for page_url in page_urls:
                    page_payload, _ = fetch_json(page_url)
                    records.extend(page_payload.get("results", []))
            else:
                with ThreadPoolExecutor(max_workers=min(workers, len(page_urls))) as executor:
                    for page_payload, _ in executor.map(fetch_json, page_urls):
                        records.extend(page_payload.get("results", []))
        return records

    while True:
        records.extend(payload)
        url = next_link(link)
        if not url:
            break
        payload, link = fetch_json(url)
    return records


def fetch_json(url: str) -> tuple[Any, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "tcia-query-skill/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
        link = response.headers.get("Link", "")
    return payload, link


def v2_page_url(
    api_version: str,
    endpoint: str,
    params: dict[str, str],
    page: int,
) -> str:
    next_params = dict(params)
    next_params["page"] = str(page)
    return f"{BASE_URL}{api_version}/{endpoint}/?{urllib.parse.urlencode(next_params)}"


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
    summary_key = "collection_summary" if is_collection else "result_summary"
    abstract_key = "collection_abstract" if is_collection else "result_abstract"
    downloads_key = "collection_downloads" if is_collection else "result_downloads"
    download_info_key = "collection_download_info" if is_collection else "result_download_info"

    title = strip_html(item.get(title_key)) or strip_html(item.get("title"))
    short_title = strip_html(item.get(short_key))
    doi = strip_html(item.get(doi_key))
    summary = strip_html(item.get(summary_key))
    abstract = strip_html(item.get(abstract_key))
    description = strip_html(item.get("detailed_description"))
    hidden_raw = strip_html(item.get("hide_from_browse_table"))
    hidden = hidden_raw.lower() not in {"", "0", "false", "no", "none"}
    licenses = collect_license_texts(item.get(download_info_key), item.get(downloads_key))
    controlled_access = is_controlled_access_from_licenses(licenses)
    noncommercial_license = has_noncommercial_license(licenses)
    license_status = classify_license_status(licenses, controlled_access, noncommercial_license)

    record = {
        "type": "Collection" if is_collection else "Analysis Result",
        "short_title": short_title,
        "title": title,
        "doi": doi,
        "link": item.get("link", "") or item.get("url", ""),
        "license_status": license_status,
        "licenses": "; ".join(licenses),
        "subjects": strip_html(item.get("subjects")),
        "data_types": strip_html(item.get("data_types")),
        "cancer_types": strip_html(item.get("cancer_types")),
        "cancer_locations": strip_html(item.get("cancer_locations")),
        "species": strip_html(item.get("species")),
        "program": strip_html(item.get("program")),
        "date_updated": strip_html(item.get("date_updated")),
        "supporting_data": strip_html(item.get("supporting_data")),
        "source_collections": "" if is_collection else strip_html(item.get("collections")),
        "download_info": strip_html(item.get(download_info_key)),
        "downloads": strip_html(item.get(downloads_key)),
        "external_resources": strip_html(item.get("external_resources") or item.get("additional_resources")),
        "summary": summary,
        "abstract": abstract,
        "detailed_description": description,
        "hidden": hidden,
        "hide_from_browse_table": hidden_raw,
        "controlled_access": controlled_access,
        "noncommercial_license": noncommercial_license,
        "controlled_access_policy": CONTROLLED_ACCESS_POLICY_URL if controlled_access else "",
    }
    record["_search_text"] = " ".join(strip_html(value) for value in item.values()).lower()
    return record


def collect_license_texts(*values: Any) -> list[str]:
    licenses: list[str] = []

    def walk(value: Any, key: str = "") -> None:
        if value is None:
            return
        if "license" in key.lower():
            keyed_text = strip_html(value)
            if keyed_text and keyed_text.lower() not in {"false", "none", "null"}:
                licenses.append(keyed_text)
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                walk(sub_value, str(sub_key))
            return
        if isinstance(value, list):
            for item in value:
                walk(item, key)
            return
        text_value = strip_html(value)
        licenses.extend(extract_flattened_licenses(text_value))

    for value in values:
        walk(value)

    deduped: list[str] = []
    seen: set[str] = set()
    for license_text in licenses:
        marker = license_text.lower()
        if marker not in seen:
            seen.add(marker)
            deduped.append(license_text)
    return deduped


def extract_flattened_licenses(text: str) -> list[str]:
    if "license" not in text.lower():
        return []
    matches = re.findall(r"(?:^|[;\n]\s*)(?:data[_ ]?)?license:\s*([^;\n]+)", text, flags=re.IGNORECASE)
    return [match.strip() for match in matches if match.strip().lower() not in {"false", "none", "null"}]


def is_creative_commons_license(license_text: str) -> bool:
    lower = license_text.lower()
    return any(term in lower for term in CREATIVE_COMMONS_TERMS)


def is_controlled_access_from_licenses(licenses: list[str]) -> bool:
    for license_text in licenses:
        lower = license_text.lower()
        if is_creative_commons_license(license_text):
            continue
        if any(term in lower for term in CONTROLLED_LICENSE_TERMS):
            return True
    return False


def has_noncommercial_license(licenses: list[str]) -> bool:
    return any(term in license_text.lower() for license_text in licenses for term in NONCOMMERCIAL_TERMS)


def classify_license_status(
    licenses: list[str],
    controlled_access: bool,
    noncommercial_license: bool,
) -> str:
    if controlled_access and any(is_creative_commons_license(license_text) for license_text in licenses):
        return "Mixed open/controlled"
    if controlled_access:
        return "Controlled"
    if noncommercial_license:
        return "Open (Creative Commons NonCommercial)"
    if licenses and all(is_creative_commons_license(license_text) for license_text in licenses):
        return "Open (Creative Commons)"
    if licenses:
        return "License review needed"
    return "Unknown"


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
    parser.add_argument(
        "--api-version",
        choices=["v1", "v2"],
        default="v2",
        help="Collection Manager API version. v2 supports verbose mode.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Use v2 verbose mode (v=1) to retrieve full long-text fields.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel fetch worker budget for v2 endpoint and page requests. Use 1 for sequential requests.",
    )
    parser.add_argument("--limit", type=int, default=25, help="Maximum records to display.")
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

    api_search = args.query
    if not api_search and len(args.short_title) == 1:
        api_search = args.short_title[0]
    jobs: list[tuple[str, str, list[str]]] = []
    if args.type in {"both", "collections"}:
        jobs.append(("collection", "collections", COLLECTION_FIELDS))
    if args.type in {"both", "analysis-results"}:
        jobs.append(("analysis", "analysis-results", ANALYSIS_FIELDS))

    page_workers = max(1, args.workers // max(1, len(jobs))) if len(jobs) > 1 else max(1, args.workers)

    def load_job(job: tuple[str, str, list[str]]) -> list[dict[str, Any]]:
        kind, endpoint, fields = job
        return [
            normalize(item, kind)
            for item in fetch_all(
                endpoint,
                fields,
                api_version=args.api_version,
                verbose=args.verbose,
                search=api_search,
                workers=page_workers,
            )
        ]

    raw_records: list[dict[str, Any]] = []
    if len(jobs) > 1 and args.workers > 1:
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            for records in executor.map(load_job, jobs):
                raw_records.extend(records)
    else:
        for job in jobs:
            raw_records.extend(load_job(job))

    short_titles = {title.lower() for title in args.short_title}
    if not args.include_hidden:
        raw_records = [record for record in raw_records if not record.get("hidden")]
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
