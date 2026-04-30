#!/usr/bin/env python3
"""Construct IDC browser viewer URLs for already-verified open DICOM data."""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import quote


OHIF_V3_BASE = "https://viewer.imaging.datacommons.cancer.gov/v3/viewer/"
SLIM_BASE = "https://viewer.imaging.datacommons.cancer.gov/slim/studies"
VOLVIEW_BASE = "https://volview.kitware.app/"


def split_values(values: list[str] | None) -> list[str]:
    """Split repeated and comma-separated CLI values while preserving order."""
    output: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        for item in value.split(","):
            item = item.strip()
            if item and item not in seen:
                output.append(item)
                seen.add(item)
    return output


def clean_s3_url(url: str) -> str:
    url = url.strip()
    if not url.startswith("s3://"):
        raise ValueError(f"VolView URLs must use s3:// paths, got: {url}")
    while url.endswith("/*"):
        url = url[:-2]
    return url.rstrip("/")


def build_ohif_v3_url(study_uids: list[str], series_uids: list[str]) -> str:
    if not study_uids:
        raise ValueError("OHIF v3 requires --study-uid")

    studies = quote(",".join(study_uids), safe=".,")
    url = f"{OHIF_V3_BASE}?StudyInstanceUIDs={studies}"
    if series_uids:
        series = quote(",".join(series_uids), safe=".,")
        url = f"{url}&SeriesInstanceUIDs={series}"
    return url


def build_slim_url(study_uids: list[str], series_uids: list[str]) -> str:
    if len(study_uids) != 1:
        raise ValueError("SliM requires exactly one --study-uid")
    if len(series_uids) != 1:
        raise ValueError("SliM requires exactly one --series-uid")

    study = quote(study_uids[0], safe=".")
    series = quote(series_uids[0], safe=".")
    return f"{SLIM_BASE}/{study}/series/{series}"


def build_volview_url(s3_urls: list[str]) -> str:
    if not s3_urls:
        raise ValueError("VolView requires --s3-url or --crdc-series-uuid")
    cleaned = [clean_s3_url(url) for url in s3_urls]
    return f"{VOLVIEW_BASE}?urls=[{','.join(cleaned)}]"


def output_url(url: str, args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps({"viewer": args.viewer, "url": url}, indent=2))
    else:
        print(url)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Construct browser viewer URLs for open-access DICOM data already "
            "confirmed as TCIA-published and present in IDC. This helper does "
            "not query IDC or verify access/license status."
        )
    )
    parser.add_argument(
        "viewer",
        choices=("ohif-v3", "slim", "volview"),
        help="Viewer URL type to construct.",
    )
    parser.add_argument(
        "--study-uid",
        action="append",
        default=[],
        help="DICOM StudyInstanceUID. Repeat or comma-separate for OHIF v3.",
    )
    parser.add_argument(
        "--series-uid",
        action="append",
        default=[],
        help="DICOM SeriesInstanceUID. Required for SliM; optional for OHIF v3 study-level URLs.",
    )
    parser.add_argument(
        "--s3-url",
        action="append",
        default=[],
        help="S3 series folder URL for VolView, such as s3://idc-open-data/<crdc_series_uuid>.",
    )
    parser.add_argument(
        "--crdc-series-uuid",
        action="append",
        default=[],
        help="CRDC series UUID for VolView. Repeat or comma-separate for multiple series.",
    )
    parser.add_argument(
        "--bucket",
        default="idc-open-data",
        help="S3 bucket used with --crdc-series-uuid. Default: idc-open-data.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a bare URL.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    study_uids = split_values(args.study_uid)
    series_uids = split_values(args.series_uid)
    crdc_series_uuids = split_values(args.crdc_series_uuid)
    s3_urls = split_values(args.s3_url)
    s3_urls.extend(f"s3://{args.bucket.strip('/')}/{uuid}" for uuid in crdc_series_uuids)

    try:
        if args.viewer == "ohif-v3":
            url = build_ohif_v3_url(study_uids, series_uids)
        elif args.viewer == "slim":
            url = build_slim_url(study_uids, series_uids)
        else:
            url = build_volview_url(s3_urls)
    except ValueError as exc:
        parser.error(str(exc))
        return 2

    output_url(url, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
