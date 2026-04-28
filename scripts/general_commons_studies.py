#!/usr/bin/env python3
"""List TCIA-related General Commons studies under phs004225."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Any, Optional


ENDPOINT = "https://general.datacommons.cancer.gov/v1/graphql/"
TCIA_PHS = "phs004225"

DESIRED_STUDY_FIELDS = [
    "phs_accession",
    "study_acronym",
    "study_name",
    "study_description",
    "program_name",
]

COUNT_FIELDS = [
    "participantsCount",
    "samplesCount",
    "filesCount",
    "diagnosesCount",
    "treatmentsCount",
    "imagesCount",
    "genomicInfoCount",
    "proteomicsCount",
    "pdxCount",
    "multiplexMicroscopiesCount",
    "nonDICOMCTimagesCount",
    "nonDICOMMRimagesCount",
    "nonDICOMPETimagesCount",
    "nonDICOMpathologyImagesCount",
    "nonDICOMradiologyAllModalitiesCount",
]


def graphql(query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "tcia-query-skill/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))
    if "errors" in result:
        messages = "; ".join(error.get("message", str(error)) for error in result["errors"])
        raise RuntimeError(messages)
    return result["data"]


def query_fields(type_name: str) -> set[str]:
    if type_name == "Query":
        schema = graphql(
            """
            query RootQueryType {
              __schema {
                queryType { name }
              }
            }
            """
        )
        type_name = schema["__schema"]["queryType"]["name"]

    data = graphql(
        """
        query TypeFields($name: String!) {
          __type(name: $name) {
            fields { name }
          }
        }
        """,
        {"name": type_name},
    )
    type_info = data.get("__type") or {}
    return {field["name"] for field in type_info.get("fields") or []}


def list_studies(phs: str, first: int, limit: Optional[int] = None) -> list[dict[str, Any]]:
    available_fields = query_fields("Study")
    selected = [field for field in DESIRED_STUDY_FIELDS if field in available_fields]
    if not selected:
        selected = sorted(available_fields)[:5]
    field_block = "\n    ".join(selected)

    records: list[dict[str, Any]] = []
    offset = 0
    while True:
        page_size = first
        if limit is not None:
            remaining = limit - len(records)
            if remaining <= 0:
                break
            page_size = min(page_size, remaining)
        data = graphql(
            f"""
            query TCIAStudies($phs: [String], $first: Int, $offset: Int) {{
              studies(phs_accessions: $phs, first: $first, offset: $offset) {{
                {field_block}
              }}
            }}
            """,
            {"phs": [phs], "first": page_size, "offset": offset},
        )
        page = data.get("studies") or []
        records.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return records


def get_counts(phs: str) -> dict[str, int]:
    available_query_fields = query_fields("Query")
    selected = [field for field in COUNT_FIELDS if field in available_query_fields]
    count_lines = [f"{field}: {field}(phs_accession: $phs)" for field in selected]
    if not count_lines:
        return {}

    data = graphql(
        f"""
        query TCIACounts($phs: String!) {{
          {' '.join(count_lines)}
        }}
        """,
        {"phs": phs},
    )
    return data


def print_table(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No General Commons studies matched.")
        return
    keys = sorted({key for record in records for key in record.keys()})
    preferred = [key for key in DESIRED_STUDY_FIELDS if key in keys]
    remaining = [key for key in keys if key not in preferred]
    columns = preferred + remaining
    widths = {column: min(max(len(column), 18), 36) for column in columns}
    for record in records:
        for column in columns:
            widths[column] = min(max(widths[column], len(str(record.get(column, ""))) + 1), 48)

    print(" | ".join(column.ljust(widths[column]) for column in columns))
    print(" | ".join("-" * widths[column] for column in columns))
    for record in records:
        row = []
        for column in columns:
            value = str(record.get(column, "") or "")
            if len(value) > widths[column]:
                value = value[: widths[column] - 3] + "..."
            row.append(value.ljust(widths[column]))
        print(" | ".join(row))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phs", default=TCIA_PHS, help="GC phs accession to query.")
    parser.add_argument("--study-acronym", action="append", default=[], help="Filter by study_acronym.")
    parser.add_argument("--first", type=int, default=10000, help="GraphQL page size.")
    parser.add_argument("--limit", type=int, help="Maximum studies to fetch.")
    parser.add_argument("--counts", action="store_true", help="Also show node counts for the phs accession.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    records = list_studies(args.phs, args.first, args.limit)
    if args.study_acronym:
        wanted = {value.lower() for value in args.study_acronym}
        records = [record for record in records if str(record.get("study_acronym", "")).lower() in wanted]

    output: dict[str, Any] = {"phs_accession": args.phs, "studies": records}
    if args.counts:
        output["counts"] = get_counts(args.phs)

    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print_table(records)
        if args.counts:
            print("\nCounts:")
            for key, value in output["counts"].items():
                print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"General Commons query failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
