#!/usr/bin/env python3
"""Create a TCIA Data Retriever CSV manifest."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Iterable


UID_RE = re.compile(r"\b\d+(?:\.\d+)+\b")
MIN_UID_LENGTH = 10
MAX_UID_LENGTH = 64


def split_lines(values: list[str], files: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        output.extend(value.replace(",", "\n").splitlines())
    for path in files:
        output.extend(Path(path).read_text(encoding="utf-8", errors="replace").splitlines())
    return [value.strip() for value in output if value.strip()]


def uid_candidates(text: str) -> Iterable[str]:
    for match in UID_RE.finditer(text):
        uid = match.group(0)
        if MIN_UID_LENGTH <= len(uid) <= MAX_UID_LENGTH:
            yield uid


def unique(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def collect_series_uids(values: list[str], files: list[str]) -> list[str]:
    return unique(uid for text in split_lines(values, files) for uid in uid_candidates(text))


def collect_plain_values(values: list[str], files: list[str]) -> list[str]:
    return unique(split_lines(values, files))


def build_rows(args: argparse.Namespace) -> tuple[list[str], list[list[str]]]:
    series_uids = collect_series_uids(args.series_uid, args.uids_file)
    image_urls = collect_plain_values(args.image_url, args.image_urls_file)
    drs_uris = collect_plain_values(args.drs_uri, args.drs_uris_file)

    modes = sum(bool(values) for values in (series_uids, image_urls, drs_uris))
    if modes != 1:
        raise ValueError(
            "Provide exactly one input type: Series Instance UIDs, image URLs, or DRS URIs."
        )

    if series_uids:
        return ["SeriesInstanceUID"], [[uid] for uid in series_uids]
    if image_urls:
        return ["imageUrl"], [[url] for url in image_urls]
    return ["drs_uri"], [[uri] for uri in drs_uris]


def write_csv(path: str | None, header: list[str], rows: list[list[str]]) -> None:
    if path:
        output = Path(path)
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        print(f"Wrote {len(rows)} row(s) to {output}")
    else:
        writer = csv.writer(sys.stdout)
        writer.writerow(header)
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--series-uid",
        action="append",
        default=[],
        help="DICOM Series Instance UID. Repeat or provide comma/newline-separated text.",
    )
    parser.add_argument(
        "--uids-file",
        action="append",
        default=[],
        help="File containing DICOM Series Instance UIDs.",
    )
    parser.add_argument(
        "--image-url",
        action="append",
        default=[],
        help="Direct image/file URL for Data Retriever's imageUrl column.",
    )
    parser.add_argument(
        "--image-urls-file",
        action="append",
        default=[],
        help="File containing direct image/file URLs, one per line.",
    )
    parser.add_argument(
        "--drs-uri",
        "--file-id",
        dest="drs_uri",
        action="append",
        default=[],
        help="DRS URI or General Commons file ID for Data Retriever's drs_uri column.",
    )
    parser.add_argument(
        "--drs-uris-file",
        "--file-ids-file",
        dest="drs_uris_file",
        action="append",
        default=[],
        help="File containing DRS URIs or General Commons file IDs, one per line.",
    )
    parser.add_argument(
        "--out",
        help="Output .csv path. If omitted, print CSV to stdout.",
    )
    args = parser.parse_args(argv)

    try:
        header, rows = build_rows(args)
    except ValueError as exc:
        print(f"Could not create CSV manifest: {exc}", file=sys.stderr)
        return 2

    write_csv(args.out, header, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
