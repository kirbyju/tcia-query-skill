#!/usr/bin/env python3
"""Fetch, parse, and search TCIA's verified EndNote publication library."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional


PUBLICATIONS_URL = "https://cancerimagingarchive.net/endnote/Pubs_basedon_TCIA.xml"
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_PATH = SKILL_ROOT / "cache" / "Pubs_basedon_TCIA.xml"
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s;,]+", re.IGNORECASE)


def collapse_ws(text: str) -> str:
    return " ".join((text or "").split())


def element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return collapse_ws("".join(element.itertext()))


def first_text(record: ET.Element, *paths: str) -> str:
    for path in paths:
        text = element_text(record.find(path))
        if text:
            return text
    return ""


def all_text(record: ET.Element, path: str) -> list[str]:
    return [text for text in (element_text(elem) for elem in record.findall(path)) if text]


def normalize_doi(doi: str) -> str:
    return doi.strip().rstrip(".").lower()


def extract_dois(text: str) -> list[str]:
    seen: set[str] = set()
    dois: list[str] = []
    for match in DOI_RE.findall(text or ""):
        doi = normalize_doi(match)
        if doi not in seen:
            seen.add(doi)
            dois.append(doi)
    return dois


def parse_record(record: ET.Element) -> dict[str, Any]:
    remote_database_name = first_text(record, "remote-database-name")
    authors = all_text(record, "contributors/authors/author")
    keywords = all_text(record, "keywords/keyword")
    journal = first_text(record, "titles/secondary-title", "periodical/full-title")
    doi = normalize_doi(first_text(record, "electronic-resource-num"))
    accession_num = first_text(record, "accession-num")
    pmid = accession_num if accession_num.isdigit() else ""
    return {
        "rec_number": first_text(record, "rec-number"),
        "ref_type": record.find("ref-type").get("name", "") if record.find("ref-type") is not None else "",
        "title": first_text(record, "titles/title"),
        "authors": authors,
        "first_author": authors[0] if authors else "",
        "journal": journal,
        "year": first_text(record, "dates/year"),
        "doi": doi,
        "pmid": pmid,
        "accession_num": accession_num,
        "keywords": keywords,
        "abstract": first_text(record, "abstract"),
        "notes": first_text(record, "notes"),
        "linked_tcia_dataset_dois": extract_dois(remote_database_name),
        "remote_database_name": remote_database_name,
    }


def read_publications_xml(path: Path) -> list[dict[str, Any]]:
    root = ET.parse(path).getroot()
    return [parse_record(record) for record in root.findall("./records/record")]


def fetch_publications(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "tcia-query-skill/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        path.write_bytes(response.read())


def publication_path(args: argparse.Namespace) -> Path:
    return Path(args.xml) if args.xml else DEFAULT_CACHE_PATH


def matches(record: dict[str, Any], args: argparse.Namespace) -> bool:
    if args.dataset_doi:
        linked = {normalize_doi(doi) for doi in record["linked_tcia_dataset_dois"]}
        wanted = {normalize_doi(doi) for doi in args.dataset_doi}
        if linked.isdisjoint(wanted):
            return False
    if args.keyword:
        keywords = {keyword.lower() for keyword in record["keywords"]}
        if not all(keyword.lower() in keywords for keyword in args.keyword):
            return False
    if args.from_year or args.to_year:
        try:
            year = int(record.get("year") or "0")
        except ValueError:
            return False
        if args.from_year and year < args.from_year:
            return False
        if args.to_year and year > args.to_year:
            return False
    if args.query:
        haystack = " ".join(
            [
                record.get("title", ""),
                record.get("journal", ""),
                record.get("doi", ""),
                record.get("pmid", ""),
                record.get("accession_num", ""),
                record.get("abstract", ""),
                record.get("notes", ""),
                " ".join(record.get("authors", [])),
                " ".join(record.get("keywords", [])),
                " ".join(record.get("linked_tcia_dataset_dois", [])),
            ]
        ).lower()
        if not all(term in haystack for term in args.query.lower().split()):
            return False
    return True


def sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(record: dict[str, Any]) -> tuple[int, str]:
        try:
            year = int(record.get("year") or "0")
        except ValueError:
            year = 0
        return (-year, record.get("title", ""))

    return sorted(records, key=key)


def clean_cell(value: Any, width: int = 72) -> str:
    if isinstance(value, list):
        text = "; ".join(str(item) for item in value)
    else:
        text = str(value or "")
    text = text.replace("|", "\\|").replace("\n", " ").strip()
    if len(text) > width:
        return text[: width - 3] + "..."
    return text


def print_table(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No TCIA publication records matched.")
        return
    print("Year | Title | Journal | DOI | PMID | Linked TCIA dataset DOI(s)")
    print("--- | --- | --- | --- | --- | ---")
    for record in records:
        print(
            f"{clean_cell(record.get('year'), 8)} | "
            f"{clean_cell(record.get('title'))} | "
            f"{clean_cell(record.get('journal'), 36)} | "
            f"{clean_cell(record.get('doi'), 36)} | "
            f"{clean_cell(record.get('pmid'), 16)} | "
            f"{clean_cell(record.get('linked_tcia_dataset_dois'), 48)}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", help="Path to a local Pubs_basedon_TCIA.xml file.")
    parser.add_argument("--fetch", action="store_true", help="Download the current EndNote XML before parsing.")
    parser.add_argument("--source-url", default=PUBLICATIONS_URL, help="EndNote XML URL to fetch.")
    parser.add_argument("--query", help="Text query over title, abstract, keywords, journal, authors, DOI, PMID, and notes.")
    parser.add_argument("--dataset-doi", action="append", default=[], help="Filter by linked TCIA dataset DOI.")
    parser.add_argument("--keyword", action="append", default=[], help="Filter by exact EndNote keyword; can be repeated.")
    parser.add_argument("--from-year", type=int, help="Minimum publication year.")
    parser.add_argument("--to-year", type=int, help="Maximum publication year.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum records to print; 0 means all matched records.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    path = publication_path(args)
    if args.fetch or not path.exists():
        fetch_publications(args.source_url, path)
    if not path.exists():
        print(f"No EndNote XML file found at {path}. Run with --fetch or pass --xml.", file=sys.stderr)
        return 1

    records = [record for record in read_publications_xml(path) if matches(record, args)]
    records = sort_records(records)
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
        print(f"TCIA publication query failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
