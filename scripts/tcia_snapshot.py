#!/usr/bin/env python3
"""Build, download, and query a local TCIA metadata SQLite snapshot."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import html
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = 4
DEFAULT_REPO = "kirbyju/tcia-query-skill"
DEFAULT_RELEASE_TAG = "tcia-snapshot-latest"
SNAPSHOT_ASSET = "tcia_snapshot.sqlite.gz"
MANIFEST_ASSET = "tcia_snapshot_manifest.json"
AGENT_DATASETS_EXPORT = "agent_datasets.jsonl.gz"
AGENT_DOWNLOADS_EXPORT = "agent_current_downloads.jsonl.gz"
WEB_EXPORT_ASSETS = [
    AGENT_DATASETS_EXPORT,
    AGENT_DOWNLOADS_EXPORT,
]
REQUIRED_AGENT_VIEWS = [
    "agent_datasets",
    "agent_current_downloads",
    "agent_dataset_access_summary",
    "agent_pathdb_slides",
    "agent_datacite_dois",
]
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = SKILL_ROOT / "cache" / "tcia_snapshot.sqlite"
DEFAULT_MANIFEST_PATH = SKILL_ROOT / "cache" / MANIFEST_ASSET

BASE_WORDPRESS_URL = "https://cancerimagingarchive.net/api/v2"
DATACITE_DOIS_URL = "https://api.datacite.org/dois"
DEFAULT_TCIA_PREFIX = "10.7937"
PATHDB_CSV_URL = (
    "https://pathdb.cancerimagingarchive.net/system/files/collectionmetadata/202401/"
    "cohort_builder_v1_01-16-2024.csv"
)
CAMICROSCOPE_VIEWER_BASE = (
    "https://pathdb.cancerimagingarchive.net/caMicroscope/apps/mini/viewer.html"
)
CONTROLLED_ACCESS_POLICY_URL = (
    "https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/"
)
USER_AGENT = "tcia-query-skill/1.0"

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
FALSEY_TEXT = {"", "false", "none", "null"}

PATHDB_COLUMNS = [
    "collection",
    "patient_id",
    "slide_id",
    "wsiimage_url",
    "species",
    "cancer_type",
    "cancer_location",
    "data_format",
    "modality",
    "protocol",
    "par",
    "magnification",
    "update",
    "camic_id",
]


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def get_data(self) -> str:
        return " ".join(self.parts)


def snapshot_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    env_path = os.environ.get("TCIA_SNAPSHOT_DB")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


def manifest_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    env_path = os.environ.get("TCIA_SNAPSHOT_MANIFEST")
    if env_path:
        return Path(env_path)
    return DEFAULT_MANIFEST_PATH


def snapshot_available(path: str | os.PathLike[str] | None = None) -> bool:
    candidate = snapshot_path(path)
    return candidate.exists() and candidate.stat().st_size > 0


def connect_snapshot(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(snapshot_path(path))
    conn.row_factory = sqlite3.Row
    return conn


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "" if value is False else "True"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
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


def strip_html(value: Any) -> str:
    text = stringify(value)
    parser = _Stripper()
    parser.feed(text)
    return re.sub(r"\s+", " ", html.unescape(parser.get_data() or text)).strip()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def clean_text(value: Any) -> str:
    text = strip_html(value)
    return "" if text.lower() in FALSEY_TEXT else text


def scalar_field(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in item:
            text = clean_text(item.get(key))
            if text:
                return text
    return ""


def label_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, list):
        labels: list[str] = []
        for item in value:
            labels.extend(label_list(item))
        return unique_list(labels)
    if isinstance(value, dict):
        for key in ("label", "title", "name"):
            if key in value:
                return label_list(value.get(key))
        return []
    text = clean_text(value)
    if not text:
        return []
    return [text]


def labels_field(item: dict[str, Any], *keys: str) -> list[str]:
    labels: list[str] = []
    for key in keys:
        if key in item:
            labels.extend(label_list(item.get(key)))
    return unique_list(labels)


def license_field(item: dict[str, Any]) -> tuple[str, str]:
    value = item.get("license")
    if value is None:
        value = item.get("data_license")
    if isinstance(value, dict):
        return clean_text(value.get("label")), clean_text(value.get("url"))
    return clean_text(value), ""


def requirements_field(item: dict[str, Any]) -> tuple[str, str, str]:
    value = item.get("download requirements")
    if value is None:
        value = item.get("download_requirements")
    if not isinstance(value, dict):
        return clean_text(value), "", ""
    return (
        clean_text(value.get("label")),
        clean_text(value.get("url")),
        clean_text(value.get("text")),
    )


def unique_list(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = clean_text(value)
        marker = clean.lower()
        if clean and marker not in seen:
            seen.add(marker)
            output.append(clean)
    return output


def fetch_bytes(url: str, timeout: int = 90) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        headers = {key.lower(): value for key, value in response.headers.items()}
        return response.read(), headers


def fetch_json(url: str, timeout: int = 90) -> tuple[Any, dict[str, str]]:
    body, headers = fetch_bytes(url, timeout=timeout)
    return json.loads(body.decode("utf-8")), headers


def wordpress_url(endpoint: str, page: int) -> str:
    params = {"per_page": "100", "page": str(page), "v": "1"}
    return f"{BASE_WORDPRESS_URL}/{endpoint}/?{urllib.parse.urlencode(params)}"


def fetch_wordpress_endpoint(endpoint: str) -> list[dict[str, Any]]:
    payload, _ = fetch_json(wordpress_url(endpoint, 1))
    if not isinstance(payload, dict) or "results" not in payload:
        raise RuntimeError(f"Unexpected WordPress response for {endpoint}")

    records = list(payload.get("results") or [])
    total_pages = int(payload.get("total_pages") or 1)
    for page in range(2, total_pages + 1):
        page_payload, _ = fetch_json(wordpress_url(endpoint, page))
        records.extend(page_payload.get("results") or [])
    return sorted(records, key=lambda record: (str(record.get("id") or ""), str(record.get("slug") or "")))


def datacite_url(page: int, page_size: int, prefix: str = DEFAULT_TCIA_PREFIX) -> str:
    params = {
        "prefix": prefix,
        "page[number]": str(page),
        "page[size]": str(page_size),
    }
    return f"{DATACITE_DOIS_URL}?{urllib.parse.urlencode(params)}"


def fetch_datacite_prefix(prefix: str = DEFAULT_TCIA_PREFIX) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page = 1
    page_size = 100
    while True:
        payload, _ = fetch_json(datacite_url(page, page_size, prefix))
        page_records = payload.get("data") or []
        records.extend(page_records)
        total = int((payload.get("meta") or {}).get("total") or 0)
        if not page_records or len(records) >= total:
            break
        page += 1
    return sorted(records, key=lambda record: datacite_doi(record).lower())


def fetch_pathdb_rows(url: str = PATHDB_CSV_URL) -> list[dict[str, str]]:
    body, _ = fetch_bytes(url)
    text = body.decode("utf-8-sig", errors="replace")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        rows.append({column: row.get(column, "") for column in PATHDB_COLUMNS})
    return sorted(
        rows,
        key=lambda row: (
            row.get("collection", ""),
            row.get("patient_id", ""),
            row.get("slide_id", ""),
            row.get("camic_id", ""),
        ),
    )


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
    matches = re.findall(
        r"(?:^|[;\n]\s*)(?:data[_ ]?)?license:\s*([^;\n]+)",
        text,
        flags=re.IGNORECASE,
    )
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


def normalize_wordpress_download(
    item: dict[str, Any],
    parent_source: str,
    parent_id: str = "",
    parent_slug: str = "",
    parent_short_title: str = "",
    parent_title: str = "",
    parent_hidden: int = 0,
    is_current_version: bool = False,
) -> dict[str, Any]:
    download_types = labels_field(item, "download type", "download_type")
    data_types = labels_field(item, "data type", "data_type")
    file_types = labels_field(item, "file type", "file_type")
    external_resources = labels_field(item, "external_resources")
    license_label, license_url = license_field(item)
    requirements_label, requirements_url, requirements_text = requirements_field(item)
    controlled_access = is_controlled_access_from_licenses([license_label] if license_label else [])
    noncommercial_license = has_noncommercial_license([license_label] if license_label else [])

    return {
        "parent_source": parent_source,
        "parent_id": parent_id,
        "parent_slug": parent_slug,
        "parent_short_title": parent_short_title,
        "parent_title": parent_title,
        "parent_hidden": int(parent_hidden),
        "is_current_version": bool(is_current_version),
        "download_id": str(item.get("id") or ""),
        "download_slug": scalar_field(item, "slug"),
        "download_title": scalar_field(item, "download_title", "download title", "title"),
        "download_url": scalar_field(item, "download url", "download_url", "download_file"),
        "download_metadata": scalar_field(item, "download metadata", "download_metadata"),
        "search_url": scalar_field(item, "search url", "search_url"),
        "date_updated": scalar_field(item, "date updated", "date_updated"),
        "collection_status": scalar_field(item, "collection status", "collection_status"),
        "description": strip_html(item.get("description")),
        "license_label": license_label,
        "license_url": license_url,
        "requirements_label": requirements_label,
        "requirements_url": requirements_url,
        "requirements_text": requirements_text,
        "download_size": scalar_field(item, "download size", "download_size"),
        "download_size_unit": scalar_field(item, "download size unit", "download_size_unit"),
        "subjects": scalar_field(item, "subjects"),
        "studies": scalar_field(item, "studies", "study_count"),
        "series": scalar_field(item, "series", "series_count"),
        "images": scalar_field(item, "images", "image_count"),
        "download_types": download_types,
        "data_types": data_types,
        "file_types": file_types,
        "external_resources": external_resources,
        "controlled_access": controlled_access,
        "noncommercial_license": noncommercial_license,
    }


def compact_download(download: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": download.get("download_id", ""),
        "title": download.get("download_title", ""),
        "download_types": download.get("download_types", []),
        "data_types": download.get("data_types", []),
        "file_types": download.get("file_types", []),
        "url": download.get("download_url", ""),
        "license": download.get("license_label", ""),
        "controlled_access": download.get("controlled_access", False),
        "subjects": download.get("subjects", ""),
        "studies": download.get("studies", ""),
        "series": download.get("series", ""),
        "images": download.get("images", ""),
    }


def aggregate_download_labels(downloads: list[dict[str, Any]], key: str) -> str:
    labels: list[str] = []
    for download in downloads:
        labels.extend(download.get(key, []))
    return "; ".join(unique_list(labels))


def has_clinical_download(downloads: list[dict[str, Any]]) -> bool:
    clinical_labels = {"clinical data", "demographic", "diagnosis", "treatment", "follow-up"}
    for download in downloads:
        labels = [
            *(download.get("download_types", []) or []),
            *(download.get("data_types", []) or []),
        ]
        if any(label.lower() in clinical_labels for label in labels):
            return True
    return False


def normalize_wordpress_record(item: dict[str, Any], kind: str) -> dict[str, Any]:
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
    hidden_raw = strip_html(item.get("hide_from_browse_table"))
    hidden = hidden_raw.lower() not in {"", "0", "false", "no", "none"}
    licenses = collect_license_texts(item.get(download_info_key), item.get(downloads_key))
    controlled_access = is_controlled_access_from_licenses(licenses)
    noncommercial_license = has_noncommercial_license(licenses)
    license_status = classify_license_status(licenses, controlled_access, noncommercial_license)
    current_downloads = [
        normalize_wordpress_download(
            download,
            parent_source="collections" if is_collection else "analysis-results",
            parent_id=str(item.get("id") or ""),
            parent_slug=strip_html(item.get("slug")),
            parent_short_title=short_title,
            parent_title=title,
            parent_hidden=hidden,
            is_current_version=True,
        )
        for download in item.get(downloads_key) or []
        if isinstance(download, dict)
    ]
    external_resources = strip_html(item.get("external_resources") or item.get("additional_resources"))

    return {
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
        "download_types": aggregate_download_labels(current_downloads, "download_types"),
        "download_data_types": aggregate_download_labels(current_downloads, "data_types"),
        "download_file_types": aggregate_download_labels(current_downloads, "file_types"),
        "has_tcia_clinical_download": has_clinical_download(current_downloads),
        "has_external_clinical_resource": "clinical" in external_resources.lower(),
        "current_downloads": [compact_download(download) for download in current_downloads],
        "external_resources": external_resources,
        "summary": strip_html(item.get(summary_key)),
        "abstract": strip_html(item.get(abstract_key)),
        "detailed_description": strip_html(item.get("detailed_description")),
        "hidden": hidden,
        "hide_from_browse_table": hidden_raw,
        "controlled_access": controlled_access,
        "noncommercial_license": noncommercial_license,
        "controlled_access_policy": CONTROLLED_ACCESS_POLICY_URL if controlled_access else "",
    }


def hidden_value(record: dict[str, Any]) -> int:
    text = strip_html(record.get("hide_from_browse_table")).lower()
    return 0 if text in {"", "0", "false", "no", "none"} else 1


def first_title(attrs: dict[str, Any], title_type: str | None = None) -> str:
    for title in attrs.get("titles") or []:
        if title_type is None and not title.get("titleType"):
            return title.get("title", "")
        if title_type is not None and title.get("titleType") == title_type:
            return title.get("title", "")
    return ""


def datacite_short_name(attrs: dict[str, Any]) -> str:
    for identifier in attrs.get("identifiers") or []:
        if str(identifier.get("identifierType", "")).lower() == "tcia short name":
            return identifier.get("identifier", "")
    return first_title(attrs, "AlternativeTitle")


def datacite_doi(work: dict[str, Any]) -> str:
    attrs = work.get("attributes") or {}
    return attrs.get("doi") or work.get("id", "")


def normalize_datacite(work: dict[str, Any]) -> dict[str, Any]:
    attrs = work.get("attributes") or {}
    return {
        "doi": datacite_doi(work),
        "tcia_short_name": datacite_short_name(attrs),
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


def camicroscope_url(camic_id: str) -> str:
    camic_id = (camic_id or "").strip()
    if not camic_id:
        return ""
    return f"{CAMICROSCOPE_VIEWER_BASE}?mode=pathdb&slideId={urllib.parse.quote(camic_id, safe='')}"


def canonical_content_hash(
    wordpress_sources: dict[str, list[dict[str, Any]]],
    pathdb_rows: list[dict[str, str]],
    datacite_records: list[dict[str, Any]],
) -> str:
    digest = hashlib.sha256()
    for label in sorted(wordpress_sources):
        digest.update(f"wordpress:{label}\n".encode("utf-8"))
        for record in wordpress_sources[label]:
            digest.update(json_dumps(record).encode("utf-8"))
            digest.update(b"\n")
    digest.update(b"pathdb\n")
    for row in pathdb_rows:
        digest.update(json_dumps(row).encode("utf-8"))
        digest.update(b"\n")
    digest.update(b"datacite\n")
    for record in datacite_records:
        digest.update(json_dumps(record).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def create_schema(conn: sqlite3.Connection) -> None:
    quoted_pathdb = ",\n            ".join(f"{quote_identifier(column)} TEXT" for column in PATHDB_COLUMNS)
    camicroscope_base = CAMICROSCOPE_VIEWER_BASE.replace("'", "''")
    policy_url = CONTROLLED_ACCESS_POLICY_URL.replace("'", "''")
    conn.executescript(
        f"""
        CREATE TABLE snapshot_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE wordpress_records (
            source TEXT NOT NULL,
            id TEXT,
            slug TEXT,
            short_title TEXT,
            doi TEXT,
            title TEXT,
            link TEXT,
            date_updated TEXT,
            hidden INTEGER NOT NULL DEFAULT 0,
            normalized_json TEXT,
            raw_json TEXT NOT NULL,
            search_text TEXT NOT NULL
        );

        CREATE TABLE wordpress_downloads (
            download_row_id INTEGER PRIMARY KEY,
            parent_source TEXT NOT NULL,
            parent_id TEXT,
            parent_slug TEXT,
            parent_short_title TEXT,
            parent_title TEXT,
            parent_hidden INTEGER NOT NULL DEFAULT 0,
            is_current_version INTEGER NOT NULL DEFAULT 0,
            download_id TEXT,
            download_slug TEXT,
            download_title TEXT,
            download_url TEXT,
            download_metadata TEXT,
            search_url TEXT,
            date_updated TEXT,
            collection_status TEXT,
            description TEXT,
            license_label TEXT,
            license_url TEXT,
            requirements_label TEXT,
            requirements_url TEXT,
            requirements_text TEXT,
            download_size TEXT,
            download_size_unit TEXT,
            subjects TEXT,
            studies TEXT,
            series TEXT,
            images TEXT,
            download_types_json TEXT NOT NULL,
            data_types_json TEXT NOT NULL,
            file_types_json TEXT NOT NULL,
            external_resources_json TEXT NOT NULL,
            controlled_access INTEGER NOT NULL DEFAULT 0,
            noncommercial_license INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL,
            search_text TEXT NOT NULL
        );

        CREATE TABLE wordpress_download_labels (
            download_row_id INTEGER NOT NULL,
            label_kind TEXT NOT NULL,
            label TEXT NOT NULL,
            FOREIGN KEY(download_row_id) REFERENCES wordpress_downloads(download_row_id)
        );

        CREATE TABLE pathdb_rows (
            {quoted_pathdb}
        );

        CREATE TABLE pathdb_collection_summary (
            collection TEXT PRIMARY KEY,
            patients INTEGER NOT NULL,
            slides INTEGER NOT NULL,
            cancer_type TEXT,
            cancer_location TEXT,
            modality TEXT,
            data_format TEXT,
            species TEXT,
            last_update TEXT
        );

        CREATE TABLE datacite_dois (
            doi TEXT,
            tcia_short_name TEXT,
            title TEXT,
            publisher TEXT,
            publication_year TEXT,
            version TEXT,
            state TEXT,
            created TEXT,
            updated TEXT,
            url TEXT,
            normalized_json TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            search_text TEXT NOT NULL
        );

        CREATE VIEW agent_datasets AS
        SELECT
            source,
            CASE
                WHEN source = 'collections' THEN 'Collection'
                WHEN source = 'analysis-results' THEN 'Analysis Result'
                ELSE source
            END AS dataset_type,
            id,
            slug,
            short_title,
            title,
            doi,
            link,
            date_updated,
            hidden,
            json_extract(normalized_json, '$.license_status') AS license_status,
            json_extract(normalized_json, '$.licenses') AS licenses,
            CAST(COALESCE(json_extract(normalized_json, '$.controlled_access'), 0) AS INTEGER) AS controlled_access,
            CAST(COALESCE(json_extract(normalized_json, '$.noncommercial_license'), 0) AS INTEGER) AS noncommercial_license,
            CASE
                WHEN json_extract(normalized_json, '$.license_status') = 'Mixed open/controlled' THEN 'mixed'
                WHEN COALESCE(json_extract(normalized_json, '$.controlled_access'), 0) THEN 'controlled'
                WHEN COALESCE(json_extract(normalized_json, '$.noncommercial_license'), 0) THEN 'open_noncommercial'
                WHEN json_extract(normalized_json, '$.license_status') = 'Open (Creative Commons)' THEN 'open'
                WHEN json_extract(normalized_json, '$.license_status') = 'Unknown' THEN 'unknown'
                ELSE 'review_needed'
            END AS access_level,
            CASE
                WHEN COALESCE(json_extract(normalized_json, '$.controlled_access'), 0) THEN '{policy_url}'
                ELSE ''
            END AS controlled_access_policy_url,
            json_extract(normalized_json, '$.subjects') AS subjects,
            json_extract(normalized_json, '$.data_types') AS data_types,
            json_extract(normalized_json, '$.download_types') AS download_types,
            json_extract(normalized_json, '$.download_data_types') AS download_data_types,
            json_extract(normalized_json, '$.download_file_types') AS download_file_types,
            json_extract(normalized_json, '$.cancer_types') AS cancer_types,
            json_extract(normalized_json, '$.cancer_locations') AS cancer_locations,
            json_extract(normalized_json, '$.species') AS species,
            json_extract(normalized_json, '$.program') AS program,
            json_extract(normalized_json, '$.has_tcia_clinical_download') AS has_tcia_clinical_download,
            json_extract(normalized_json, '$.has_external_clinical_resource') AS has_external_clinical_resource,
            json_extract(normalized_json, '$.source_collections') AS source_collections,
            json_extract(normalized_json, '$.summary') AS summary,
            json_extract(normalized_json, '$.abstract') AS abstract,
            json_extract(normalized_json, '$.detailed_description') AS detailed_description,
            normalized_json,
            raw_json
        FROM wordpress_records
        WHERE source IN ('collections', 'analysis-results')
          AND normalized_json IS NOT NULL;

        CREATE VIEW agent_current_downloads AS
        SELECT
            d.download_row_id,
            d.parent_source,
            CASE
                WHEN d.parent_source = 'collections' THEN 'Collection'
                WHEN d.parent_source = 'analysis-results' THEN 'Analysis Result'
                ELSE d.parent_source
            END AS dataset_type,
            d.parent_id,
            d.parent_slug,
            d.parent_short_title AS short_title,
            d.parent_title AS title,
            d.parent_hidden AS hidden,
            d.download_id,
            d.download_slug,
            d.download_title,
            d.download_url,
            d.download_metadata,
            d.search_url,
            d.date_updated,
            d.collection_status,
            d.description,
            d.license_label,
            d.license_url,
            d.requirements_label,
            d.requirements_url,
            d.requirements_text,
            d.download_size,
            d.download_size_unit,
            d.subjects,
            d.studies,
            d.series,
            d.images,
            d.download_types_json AS download_types,
            d.data_types_json AS data_types,
            d.file_types_json AS file_types,
            d.external_resources_json AS external_resources,
            d.controlled_access,
            d.noncommercial_license,
            CASE
                WHEN d.controlled_access THEN 'controlled'
                WHEN d.noncommercial_license THEN 'open_noncommercial'
                WHEN lower(d.license_label) LIKE '%creative commons%'
                  OR lower(d.license_label) LIKE '%cc by%'
                  OR lower(d.license_label) LIKE '%cc-by%' THEN 'open'
                WHEN trim(COALESCE(d.license_label, '')) = '' THEN 'unknown'
                ELSE 'review_needed'
            END AS access_level,
            CASE WHEN d.controlled_access THEN '{policy_url}' ELSE '' END AS controlled_access_policy_url,
            d.raw_json
        FROM wordpress_downloads d
        WHERE d.is_current_version = 1
          AND d.parent_source IN ('collections', 'analysis-results');

        CREATE VIEW agent_dataset_access_summary AS
        WITH download_summary AS (
            SELECT
                parent_source,
                parent_short_title,
                COUNT(*) AS current_download_count,
                SUM(CASE WHEN controlled_access THEN 1 ELSE 0 END) AS controlled_download_count,
                SUM(CASE WHEN NOT controlled_access THEN 1 ELSE 0 END) AS noncontrolled_download_count,
                SUM(CASE WHEN NOT controlled_access AND noncommercial_license THEN 1 ELSE 0 END) AS open_noncommercial_download_count,
                group_concat(CASE WHEN controlled_access THEN NULLIF(download_title, '') END, '; ') AS controlled_download_titles,
                group_concat(CASE WHEN controlled_access THEN NULLIF(license_label, '') END, '; ') AS controlled_license_labels,
                group_concat(CASE WHEN controlled_access THEN NULLIF(download_id, '') END, '; ') AS controlled_download_ids,
                group_concat(CASE WHEN controlled_access THEN NULLIF(download_url, '') END, '; ') AS controlled_download_urls
            FROM wordpress_downloads
            WHERE is_current_version = 1
              AND parent_source IN ('collections', 'analysis-results')
            GROUP BY parent_source, parent_short_title
        ),
        summary AS (
            SELECT
                d.*,
                COALESCE(s.current_download_count, 0) AS current_download_count,
                COALESCE(s.controlled_download_count, 0) AS controlled_download_count,
                COALESCE(s.noncontrolled_download_count, 0) AS noncontrolled_download_count,
                COALESCE(s.open_noncommercial_download_count, 0) AS open_noncommercial_download_count,
                COALESCE(s.controlled_download_titles, '') AS controlled_download_titles,
                COALESCE(s.controlled_license_labels, '') AS controlled_license_labels,
                COALESCE(s.controlled_download_ids, '') AS controlled_download_ids,
                COALESCE(s.controlled_download_urls, '') AS controlled_download_urls
            FROM agent_datasets d
            LEFT JOIN download_summary s
              ON s.parent_source = d.source
             AND s.parent_short_title = d.short_title
        )
        SELECT
            *,
            CASE
                WHEN controlled_download_count > 0 AND noncontrolled_download_count > 0 THEN 'mixed'
                WHEN controlled_download_count > 0 OR controlled_access THEN 'controlled'
                WHEN noncommercial_license OR open_noncommercial_download_count > 0 THEN 'open_noncommercial'
                WHEN license_status = 'Open (Creative Commons)' THEN 'open'
                WHEN license_status = 'Unknown' THEN 'unknown'
                ELSE 'review_needed'
            END AS resolved_access_level,
            CASE
                WHEN controlled_download_count > 0 OR controlled_access THEN '{policy_url}'
                ELSE ''
            END AS resolved_controlled_access_policy_url
        FROM summary;

        CREATE VIEW agent_pathdb_slides AS
        SELECT
            collection,
            patient_id,
            slide_id,
            camic_id,
            CASE
                WHEN trim(COALESCE(camic_id, '')) = '' THEN ''
                ELSE '{camicroscope_base}?mode=pathdb&slideId=' || trim(camic_id)
            END AS camicroscope_url,
            wsiimage_url,
            species,
            cancer_type,
            cancer_location,
            data_format,
            modality,
            protocol,
            par,
            magnification,
            "update"
        FROM pathdb_rows;

        CREATE VIEW agent_datacite_dois AS
        SELECT
            doi,
            tcia_short_name,
            title,
            publisher,
            publication_year,
            version,
            state,
            created,
            updated,
            url,
            normalized_json,
            raw_json
        FROM datacite_dois;
        """
    )


def insert_meta(conn: sqlite3.Connection, meta: dict[str, Any]) -> None:
    conn.executemany(
        "INSERT INTO snapshot_meta (key, value) VALUES (?, ?)",
        [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in sorted(meta.items())],
    )


def insert_wordpress(
    conn: sqlite3.Connection,
    source: str,
    records: list[dict[str, Any]],
) -> None:
    rows = []
    for record in records:
        normalized: dict[str, Any] | None = None
        if source == "collections":
            normalized = normalize_wordpress_record(record, "collection")
        elif source == "analysis-results":
            normalized = normalize_wordpress_record(record, "analysis")

        if normalized:
            short_title = normalized.get("short_title", "")
            doi = normalized.get("doi", "")
            title = normalized.get("title", "")
            link = normalized.get("link", "")
            normalized_json = json_dumps(normalized)
        else:
            short_title = strip_html(record.get("slug"))
            doi = ""
            title = strip_html(record.get("download_title") or record.get("title"))
            link = strip_html(record.get("download_url") or record.get("download_file"))
            normalized_json = None

        search_text = " ".join(strip_html(value) for value in record.values()).lower()
        rows.append(
            (
                source,
                str(record.get("id") or ""),
                str(record.get("slug") or ""),
                short_title,
                doi,
                title,
                link,
                strip_html(record.get("date_updated")),
                hidden_value(record),
                normalized_json,
                json_dumps(record),
                search_text,
            )
        )

    conn.executemany(
        """
        INSERT INTO wordpress_records
        (source, id, slug, short_title, doi, title, link, date_updated, hidden,
         normalized_json, raw_json, search_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def insert_download_row(
    conn: sqlite3.Connection,
    download: dict[str, Any],
    raw_record: dict[str, Any],
) -> None:
    search_values = [
        download.get("parent_source", ""),
        download.get("parent_short_title", ""),
        download.get("parent_title", ""),
        download.get("download_id", ""),
        download.get("download_slug", ""),
        download.get("download_title", ""),
        download.get("download_url", ""),
        download.get("license_label", ""),
        download.get("requirements_label", ""),
        download.get("description", ""),
        *(download.get("download_types", []) or []),
        *(download.get("data_types", []) or []),
        *(download.get("file_types", []) or []),
        *(download.get("external_resources", []) or []),
    ]
    cursor = conn.execute(
        """
        INSERT INTO wordpress_downloads
        (parent_source, parent_id, parent_slug, parent_short_title, parent_title,
         parent_hidden, is_current_version, download_id, download_slug,
         download_title, download_url, download_metadata, search_url, date_updated,
         collection_status, description, license_label, license_url,
         requirements_label, requirements_url, requirements_text, download_size,
         download_size_unit, subjects, studies, series, images, download_types_json,
         data_types_json, file_types_json, external_resources_json, controlled_access,
         noncommercial_license, raw_json, search_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            download.get("parent_source", ""),
            download.get("parent_id", ""),
            download.get("parent_slug", ""),
            download.get("parent_short_title", ""),
            download.get("parent_title", ""),
            int(download.get("parent_hidden") or 0),
            1 if download.get("is_current_version") else 0,
            download.get("download_id", ""),
            download.get("download_slug", ""),
            download.get("download_title", ""),
            download.get("download_url", ""),
            download.get("download_metadata", ""),
            download.get("search_url", ""),
            download.get("date_updated", ""),
            download.get("collection_status", ""),
            download.get("description", ""),
            download.get("license_label", ""),
            download.get("license_url", ""),
            download.get("requirements_label", ""),
            download.get("requirements_url", ""),
            download.get("requirements_text", ""),
            download.get("download_size", ""),
            download.get("download_size_unit", ""),
            download.get("subjects", ""),
            download.get("studies", ""),
            download.get("series", ""),
            download.get("images", ""),
            json_dumps(download.get("download_types", [])),
            json_dumps(download.get("data_types", [])),
            json_dumps(download.get("file_types", [])),
            json_dumps(download.get("external_resources", [])),
            1 if download.get("controlled_access") else 0,
            1 if download.get("noncommercial_license") else 0,
            json_dumps(raw_record),
            " ".join(str(value).lower() for value in search_values if value),
        ),
    )
    row_id = int(cursor.lastrowid)
    label_rows = []
    for label_kind, labels_key in (
        ("download_type", "download_types"),
        ("data_type", "data_types"),
        ("file_type", "file_types"),
        ("external_resource", "external_resources"),
    ):
        for label in download.get(labels_key, []) or []:
            label_rows.append((row_id, label_kind, label))
    if label_rows:
        conn.executemany(
            """
            INSERT INTO wordpress_download_labels
            (download_row_id, label_kind, label)
            VALUES (?, ?, ?)
            """,
            label_rows,
        )


def insert_wordpress_downloads(
    conn: sqlite3.Connection,
    collections: list[dict[str, Any]],
    analysis_results: list[dict[str, Any]],
    downloads: list[dict[str, Any]],
) -> None:
    for parent, parent_source, short_key, title_key, downloads_key in [
        (collection, "collections", "collection_short_title", "collection_title", "collection_downloads")
        for collection in collections
    ] + [
        (analysis, "analysis-results", "result_short_title", "result_title", "result_downloads")
        for analysis in analysis_results
    ]:
        parent_short_title = strip_html(parent.get(short_key))
        parent_title = strip_html(parent.get(title_key)) or strip_html(parent.get("title"))
        parent_hidden = hidden_value(parent)
        for raw_download in parent.get(downloads_key) or []:
            if not isinstance(raw_download, dict):
                continue
            normalized = normalize_wordpress_download(
                raw_download,
                parent_source=parent_source,
                parent_id=str(parent.get("id") or ""),
                parent_slug=strip_html(parent.get("slug")),
                parent_short_title=parent_short_title,
                parent_title=parent_title,
                parent_hidden=parent_hidden,
                is_current_version=True,
            )
            insert_download_row(conn, normalized, raw_download)

    for raw_download in downloads:
        normalized = normalize_wordpress_download(
            raw_download,
            parent_source="downloads",
            is_current_version=False,
        )
        insert_download_row(conn, normalized, raw_download)


def insert_pathdb(conn: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    placeholders = ", ".join("?" for _ in PATHDB_COLUMNS)
    columns = ", ".join(quote_identifier(column) for column in PATHDB_COLUMNS)
    conn.executemany(
        f"INSERT INTO pathdb_rows ({columns}) VALUES ({placeholders})",
        [tuple(row.get(column, "") for column in PATHDB_COLUMNS) for row in rows],
    )

    summaries: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        summaries.setdefault(row.get("collection", ""), []).append(row)

    conn.executemany(
        """
        INSERT INTO pathdb_collection_summary
        (collection, patients, slides, cancer_type, cancer_location, modality,
         data_format, species, last_update)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                collection,
                len({row.get("patient_id", "") for row in group if row.get("patient_id")}),
                len({row.get("slide_id", "") for row in group if row.get("slide_id")}),
                unique_join({row.get("cancer_type", "") for row in group}),
                unique_join({row.get("cancer_location", "") for row in group}),
                unique_join({row.get("modality", "") for row in group}),
                unique_join({row.get("data_format", "") for row in group}),
                unique_join({row.get("species", "") for row in group}),
                max((row.get("update", "") for row in group), default=""),
            )
            for collection, group in sorted(summaries.items())
        ],
    )


def insert_datacite(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    rows = []
    for record in records:
        normalized = normalize_datacite(record)
        rows.append(
            (
                normalized.get("doi", ""),
                normalized.get("tcia_short_name", ""),
                normalized.get("title", ""),
                normalized.get("publisher", ""),
                str(normalized.get("publication_year", "")),
                normalized.get("version", ""),
                normalized.get("state", ""),
                normalized.get("created", ""),
                normalized.get("updated", ""),
                normalized.get("url", ""),
                json_dumps(normalized),
                json_dumps(record),
                " ".join(str(value) for value in normalized.values()).lower(),
            )
        )
    conn.executemany(
        """
        INSERT INTO datacite_dois
        (doi, tcia_short_name, title, publisher, publication_year, version,
         state, created, updated, url, normalized_json, raw_json, search_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def add_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX idx_wordpress_source_short_title ON wordpress_records(source, short_title);
        CREATE INDEX idx_wordpress_doi ON wordpress_records(doi);
        CREATE INDEX idx_wordpress_hidden ON wordpress_records(hidden);
        CREATE INDEX idx_wordpress_search_source ON wordpress_records(source);
        CREATE INDEX idx_wordpress_downloads_parent ON wordpress_downloads(parent_source, parent_short_title);
        CREATE INDEX idx_wordpress_downloads_id ON wordpress_downloads(download_id);
        CREATE INDEX idx_wordpress_downloads_current ON wordpress_downloads(is_current_version);
        CREATE INDEX idx_wordpress_downloads_hidden ON wordpress_downloads(parent_hidden);
        CREATE INDEX idx_wordpress_download_labels_kind_label ON wordpress_download_labels(label_kind, label COLLATE NOCASE);
        CREATE INDEX idx_wordpress_download_labels_row ON wordpress_download_labels(download_row_id);
        CREATE INDEX idx_pathdb_collection ON pathdb_rows(collection);
        CREATE INDEX idx_pathdb_patient ON pathdb_rows(patient_id);
        CREATE INDEX idx_pathdb_slide ON pathdb_rows(slide_id);
        CREATE INDEX idx_datacite_doi ON datacite_dois(doi);
        CREATE INDEX idx_datacite_short_name ON datacite_dois(tcia_short_name);
        """
    )


def unique_join(values: set[str], max_items: int = 12) -> str:
    clean = sorted(value for value in values if value)
    if len(clean) > max_items:
        return "; ".join(clean[:max_items]) + f"; +{len(clean) - max_items} more"
    return "; ".join(clean)


def build_snapshot(
    out_path: Path,
    gzip_out: Path | None = None,
    manifest_out: Path | None = None,
    exports_dir: Path | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    started = time.time()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    def log(message: str) -> None:
        if not quiet:
            print(message, file=sys.stderr)

    log("Fetching WordPress collections...")
    collections = fetch_wordpress_endpoint("collections")
    log(f"  {len(collections)} records")
    log("Fetching WordPress analysis-results...")
    analysis_results = fetch_wordpress_endpoint("analysis-results")
    log(f"  {len(analysis_results)} records")
    log("Fetching WordPress downloads...")
    downloads = fetch_wordpress_endpoint("downloads")
    log(f"  {len(downloads)} records")
    log("Fetching PathDB cohort-builder CSV...")
    pathdb_rows = fetch_pathdb_rows()
    log(f"  {len(pathdb_rows)} rows")
    log("Fetching DataCite DOI records...")
    datacite_records = fetch_datacite_prefix()
    log(f"  {len(datacite_records)} records")

    wordpress_sources = {
        "collections": collections,
        "analysis-results": analysis_results,
        "downloads": downloads,
    }
    content_sha256 = canonical_content_hash(wordpress_sources, pathdb_rows, datacite_records)
    current_collection_downloads = sum(
        len(collection.get("collection_downloads") or []) for collection in collections
    )
    current_analysis_result_downloads = sum(
        len(result.get("result_downloads") or []) for result in analysis_results
    )
    counts = {
        "wordpress_collections": len(collections),
        "wordpress_analysis_results": len(analysis_results),
        "wordpress_download_endpoint_records": len(downloads),
        "wordpress_current_collection_downloads": current_collection_downloads,
        "wordpress_current_analysis_result_downloads": current_analysis_result_downloads,
        "wordpress_downloads": (
            len(downloads) + current_collection_downloads + current_analysis_result_downloads
        ),
        "pathdb_rows": len(pathdb_rows),
        "datacite_dois": len(datacite_records),
    }

    conn = sqlite3.connect(out_path)
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")
        create_schema(conn)
        insert_wordpress(conn, "collections", collections)
        insert_wordpress(conn, "analysis-results", analysis_results)
        insert_wordpress(conn, "downloads", downloads)
        insert_wordpress_downloads(conn, collections, analysis_results, downloads)
        insert_pathdb(conn, pathdb_rows)
        insert_datacite(conn, datacite_records)
        add_indexes(conn)
        insert_meta(
            conn,
            {
                "schema_version": SCHEMA_VERSION,
                "content_sha256": content_sha256,
                "wordpress_base_url": BASE_WORDPRESS_URL,
                "wordpress_mode": "v2 verbose v=1",
                "pathdb_csv_url": PATHDB_CSV_URL,
                "datacite_prefix": DEFAULT_TCIA_PREFIX,
                "counts": counts,
            },
        )
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

    db_sha256 = file_sha256(out_path)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "content_sha256": content_sha256,
        "sqlite_sha256": db_sha256,
        "sqlite_bytes": out_path.stat().st_size,
        "counts": counts,
        "sources": {
            "wordpress_base_url": BASE_WORDPRESS_URL,
            "pathdb_csv_url": PATHDB_CSV_URL,
            "datacite_prefix": DEFAULT_TCIA_PREFIX,
        },
        "elapsed_seconds": round(time.time() - started, 3),
    }

    if exports_dir:
        manifest["web_exports"] = export_web_artifacts(out_path, exports_dir)
    if gzip_out:
        gzip_out.parent.mkdir(parents=True, exist_ok=True)
        gzip_deterministic(out_path, gzip_out)
        manifest["gzip_sha256"] = file_sha256(gzip_out)
        manifest["gzip_bytes"] = gzip_out.stat().st_size
    manifest["release_fingerprint"] = snapshot_release_fingerprint(manifest)
    if manifest_out:
        manifest_out.parent.mkdir(parents=True, exist_ok=True)
        manifest_out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def gzip_deterministic(source: Path, target: Path) -> None:
    with source.open("rb") as src, target.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            shutil.copyfileobj(src, gz)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rows_as_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = old_factory


def write_json(path: Path, payload: Any) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows = len(payload) if isinstance(payload, list) else 1
    return {"bytes": path.stat().st_size, "sha256": file_sha256(path), "rows": rows}


def write_jsonl_gz(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            for row in rows:
                line = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                gz.write(line.encode("utf-8"))
                gz.write(b"\n")
    return {"bytes": path.stat().st_size, "sha256": file_sha256(path), "rows": len(rows)}


def export_web_artifacts(db_path: Path, exports_dir: Path) -> dict[str, dict[str, Any]]:
    exports_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        validate_snapshot_schema(conn)
        datasets = rows_as_dicts(
            conn,
            """
            SELECT *
            FROM agent_dataset_access_summary
            ORDER BY hidden, lower(short_title), source
            """,
        )
        downloads = rows_as_dicts(
            conn,
            """
            SELECT *
            FROM agent_current_downloads
            ORDER BY hidden, lower(short_title), download_id, download_slug
            """,
        )

    return {
        AGENT_DATASETS_EXPORT: write_jsonl_gz(exports_dir / AGENT_DATASETS_EXPORT, datasets),
        AGENT_DOWNLOADS_EXPORT: write_jsonl_gz(exports_dir / AGENT_DOWNLOADS_EXPORT, downloads),
    }


def snapshot_release_fingerprint(manifest: dict[str, Any]) -> str:
    payload = {
        "schema_version": manifest.get("schema_version"),
        "content_sha256": manifest.get("content_sha256"),
        "sqlite_sha256": manifest.get("sqlite_sha256"),
        "web_exports": {
            name: details.get("sha256")
            for name, details in sorted((manifest.get("web_exports") or {}).items())
        },
    }
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def validate_snapshot_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    objects = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }
    missing_views = [view for view in REQUIRED_AGENT_VIEWS if objects.get(view) != "view"]
    if missing_views:
        raise RuntimeError(f"Snapshot is missing required agent views: {', '.join(missing_views)}")
    for view in REQUIRED_AGENT_VIEWS:
        conn.execute(f"SELECT * FROM {quote_identifier(view)} LIMIT 1").fetchall()
    meta_rows = conn.execute("SELECT key, value FROM snapshot_meta").fetchall()
    meta = {}
    for key, value in meta_rows:
        try:
            meta[key] = json.loads(value)
        except json.JSONDecodeError:
            meta[key] = value
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"Snapshot schema_version is {meta.get('schema_version')}; expected {SCHEMA_VERSION}."
        )
    return {
        "schema_version": meta.get("schema_version"),
        "views": REQUIRED_AGENT_VIEWS,
    }


def validate_snapshot_file(db_path: Path, exports_dir: Path | None = None) -> dict[str, Any]:
    if not db_path.exists():
        raise RuntimeError(f"Snapshot not found: {db_path}")
    with sqlite3.connect(db_path) as conn:
        result = validate_snapshot_schema(conn)
    if exports_dir:
        missing_exports = [name for name in WEB_EXPORT_ASSETS if not (exports_dir / name).exists()]
        if missing_exports:
            raise RuntimeError(f"Missing web export assets: {', '.join(missing_exports)}")
        result["web_exports"] = {
            name: {"bytes": (exports_dir / name).stat().st_size, "sha256": file_sha256(exports_dir / name)}
            for name in WEB_EXPORT_ASSETS
        }
    return result


def get_snapshot_meta(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    if not snapshot_available(path):
        return {}
    with connect_snapshot(path) as conn:
        rows = conn.execute("SELECT key, value FROM snapshot_meta").fetchall()
    output: dict[str, Any] = {}
    for row in rows:
        try:
            output[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            output[row["key"]] = row["value"]
    return output


def terms_match(record: dict[str, Any], query: str | None) -> bool:
    if not query:
        return True
    terms = [term.lower() for term in query.split() if term.strip()]
    haystack = " ".join(str(value).lower() for value in record.values())
    return all(term in haystack for term in terms)


def search_wordpress_records(
    query: str | None = None,
    short_titles: set[str] | None = None,
    type_filter: str = "both",
    include_hidden: bool = False,
    path: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    if not snapshot_available(path):
        return []
    sources = []
    if type_filter in {"both", "collections"}:
        sources.append("collections")
    if type_filter in {"both", "analysis-results"}:
        sources.append("analysis-results")
    short_titles = {title.lower() for title in (short_titles or set())}

    placeholders = ", ".join("?" for _ in sources)
    sql = (
        "SELECT normalized_json FROM wordpress_records "
        f"WHERE source IN ({placeholders}) AND normalized_json IS NOT NULL"
    )
    params: list[Any] = list(sources)
    if not include_hidden:
        sql += " AND hidden = 0"
    if short_titles:
        sql += f" AND lower(short_title) IN ({', '.join('?' for _ in short_titles)})"
        params.extend(sorted(short_titles))

    with connect_snapshot(path) as conn:
        rows = conn.execute(sql, params).fetchall()
    records = [json.loads(row["normalized_json"]) for row in rows]
    records = [record for record in records if terms_match(record, query)]
    return sorted(records, key=lambda record: (record.get("type", ""), record.get("short_title", "")))


def wordpress_downloads_from_snapshot(
    parent_short_titles: set[str] | None = None,
    label_filters: dict[str, set[str]] | None = None,
    current_only: bool = True,
    include_hidden: bool = False,
    path: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    if not snapshot_available(path):
        return []
    requested = {value.lower() for value in (parent_short_titles or set())}
    sql = "SELECT d.* FROM wordpress_downloads d WHERE 1 = 1"
    params: list[Any] = []
    if current_only:
        sql += " AND d.is_current_version IS TRUE"
    if not include_hidden:
        sql += " AND d.parent_hidden = 0"
    if requested:
        sql += f" AND lower(d.parent_short_title) IN ({', '.join('?' for _ in requested)})"
        params.extend(sorted(requested))
    for label_kind, labels in sorted((label_filters or {}).items()):
        clean_labels = {label.lower() for label in labels if label}
        if not clean_labels:
            continue
        placeholders = ", ".join("?" for _ in clean_labels)
        sql += (
            " AND EXISTS ("
            "SELECT 1 FROM wordpress_download_labels l "
            "WHERE l.download_row_id = d.download_row_id "
            f"AND l.label_kind = ? AND lower(l.label) IN ({placeholders})"
            ")"
        )
        params.append(label_kind)
        params.extend(sorted(clean_labels))
    sql += " ORDER BY d.parent_source, d.parent_short_title, d.download_id, d.download_slug"

    try:
        with connect_snapshot(path) as conn:
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []

    for row in rows:
        row["is_current_version"] = bool(row.get("is_current_version"))
        row["parent_hidden"] = bool(row.get("parent_hidden"))
        row["controlled_access"] = bool(row.get("controlled_access"))
        row["noncommercial_license"] = bool(row.get("noncommercial_license"))
        for column in (
            "download_types_json",
            "data_types_json",
            "file_types_json",
            "external_resources_json",
        ):
            output_key = column.removesuffix("_json")
            row[output_key] = json.loads(row.pop(column) or "[]")
        row["raw_json"] = json.loads(row["raw_json"])
    return rows


def datacite_records_from_snapshot(
    doi: str | None = None,
    prefix: str = DEFAULT_TCIA_PREFIX,
    query: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    if prefix != DEFAULT_TCIA_PREFIX or not snapshot_available(path):
        return []
    sql = "SELECT normalized_json FROM datacite_dois"
    params: list[Any] = []
    if doi:
        sql += " WHERE lower(doi) = ?"
        params.append(doi.lower())
    with connect_snapshot(path) as conn:
        rows = conn.execute(sql, params).fetchall()
    records = [json.loads(row["normalized_json"]) for row in rows]
    records = [record for record in records if terms_match(record, query)]
    return sorted(records, key=lambda record: str(record.get("doi", "")).lower())


def pathdb_collections_for_dois(dois: set[str], path: str | os.PathLike[str] | None = None) -> set[str]:
    if not dois:
        return set()
    placeholders = ", ".join("?" for _ in dois)
    sql = (
        "SELECT short_title FROM wordpress_records "
        f"WHERE lower(doi) IN ({placeholders}) AND source IN ('collections', 'analysis-results')"
    )
    with connect_snapshot(path) as conn:
        return {
            row["short_title"].lower()
            for row in conn.execute(sql, sorted(dois)).fetchall()
            if row["short_title"]
        }


def pathdb_rows_from_snapshot(
    query: str | None = None,
    collections: set[str] | None = None,
    dois: set[str] | None = None,
    path: str | os.PathLike[str] | None = None,
) -> list[dict[str, str]]:
    if not snapshot_available(path):
        return []
    requested = {value.lower() for value in (collections or set())}
    requested.update(pathdb_collections_for_dois({value.lower() for value in (dois or set())}, path))

    columns = ", ".join(quote_identifier(column) for column in PATHDB_COLUMNS)
    sql = f"SELECT {columns} FROM pathdb_rows"
    params: list[Any] = []
    if requested:
        sql += f" WHERE lower(collection) IN ({', '.join('?' for _ in requested)})"
        params.extend(sorted(requested))
    with connect_snapshot(path) as conn:
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

    for row in rows:
        row["camicroscope_url"] = camicroscope_url(row.get("camic_id", ""))
    if query:
        terms = [term.lower() for term in query.split() if term.strip()]
        rows = [
            row
            for row in rows
            if all(term in " ".join(str(value).lower() for value in row.values()) for term in terms)
        ]
    return rows


def summarize_pathdb_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row.get("collection", ""), []).append(row)
    return [
        {
            "collection": collection,
            "patients": len({row.get("patient_id", "") for row in group if row.get("patient_id")}),
            "slides": len({row.get("slide_id", "") for row in group if row.get("slide_id")}),
            "data_format": unique_join({row.get("data_format", "") for row in group}, 4),
            "modality": unique_join({row.get("modality", "") for row in group}, 4),
            "cancer_type": unique_join({row.get("cancer_type", "") for row in group}, 4),
            "cancer_location": unique_join({row.get("cancer_location", "") for row in group}, 4),
            "species": unique_join({row.get("species", "") for row in group}, 4),
            "last_update": max((row.get("update", "") for row in group), default=""),
        }
        for collection, group in sorted(grouped.items())
    ]


def github_repo_from_env_or_default() -> str:
    env_repo = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("TCIA_SNAPSHOT_REPOSITORY")
    if env_repo:
        return env_repo
    return DEFAULT_REPO


def github_api_json(url: str) -> Any:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def download_release_snapshot(
    repo: str,
    tag: str,
    db_path: Path,
    manifest_out: Path,
) -> dict[str, Any]:
    release = github_api_json(f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}")
    assets = {asset["name"]: asset for asset in release.get("assets") or []}
    if SNAPSHOT_ASSET not in assets or MANIFEST_ASSET not in assets:
        raise RuntimeError(f"Release {repo}@{tag} does not contain snapshot assets.")

    manifest_body, _ = fetch_bytes(assets[MANIFEST_ASSET]["browser_download_url"])
    manifest = json.loads(manifest_body.decode("utf-8"))
    current = get_snapshot_meta(db_path)
    if (
        current.get("content_sha256") == manifest.get("content_sha256")
        and current.get("schema_version") == manifest.get("schema_version")
        and db_path.exists()
    ):
        manifest_out.parent.mkdir(parents=True, exist_ok=True)
        manifest_out.write_bytes(manifest_body)
        return {"status": "unchanged", "manifest": manifest}

    compressed, _ = fetch_bytes(assets[SNAPSHOT_ASSET]["browser_download_url"], timeout=180)
    expected_gzip = manifest.get("gzip_sha256")
    actual_gzip = hashlib.sha256(compressed).hexdigest()
    if expected_gzip and actual_gzip != expected_gzip:
        raise RuntimeError("Downloaded snapshot gzip SHA-256 does not match manifest.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=str(db_path.parent)) as tmp:
        tmp_path = Path(tmp.name)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as gz:
            shutil.copyfileobj(gz, tmp)
    expected_sqlite = manifest.get("sqlite_sha256")
    actual_sqlite = file_sha256(tmp_path)
    if expected_sqlite and actual_sqlite != expected_sqlite:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded snapshot SQLite SHA-256 does not match manifest.")
    tmp_path.replace(db_path)
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_bytes(manifest_body)
    return {"status": "downloaded", "manifest": manifest}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build a SQLite snapshot from live public sources.")
    build.add_argument("--out", default=str(DEFAULT_DB_PATH), help="SQLite output path.")
    build.add_argument("--gzip-out", help="Optional deterministic gzip output path.")
    build.add_argument("--manifest-out", help="Optional manifest JSON output path.")
    build.add_argument("--exports-dir", help="Optional directory for web-friendly JSON/JSONL release exports.")
    build.add_argument("--quiet", action="store_true", help="Suppress fetch progress messages.")

    info = subparsers.add_parser("info", help="Print local snapshot metadata.")
    info.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite snapshot path.")

    validate = subparsers.add_parser("validate", help="Validate a built SQLite snapshot and optional exports.")
    validate.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite snapshot path.")
    validate.add_argument("--exports-dir", help="Optional directory containing web-friendly exports.")

    ensure = subparsers.add_parser("ensure", help="Download the latest release snapshot if missing or changed.")
    ensure.add_argument(
        "--repo",
        default=None,
        help=f"GitHub repository in owner/name form. Defaults to {DEFAULT_REPO}.",
    )
    ensure.add_argument("--tag", default=DEFAULT_RELEASE_TAG, help="Snapshot release tag.")
    ensure.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite snapshot path.")
    ensure.add_argument("--manifest-out", default=str(DEFAULT_MANIFEST_PATH), help="Manifest output path.")

    args = parser.parse_args(argv)
    if args.command == "build":
        manifest = build_snapshot(
            Path(args.out),
            gzip_out=Path(args.gzip_out) if args.gzip_out else None,
            manifest_out=Path(args.manifest_out) if args.manifest_out else None,
            exports_dir=Path(args.exports_dir) if args.exports_dir else None,
            quiet=args.quiet,
        )
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    if args.command == "info":
        meta = get_snapshot_meta(args.db)
        if not meta:
            print(f"No snapshot found at {snapshot_path(args.db)}")
            return 1
        print(json.dumps(meta, indent=2, sort_keys=True))
        return 0
    if args.command == "validate":
        result = validate_snapshot_file(
            Path(args.db),
            exports_dir=Path(args.exports_dir) if args.exports_dir else None,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "ensure":
        repo = args.repo or github_repo_from_env_or_default()
        result = download_release_snapshot(repo, args.tag, Path(args.db), Path(args.manifest_out))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        print(f"Network or API error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"TCIA snapshot error: {exc}", file=sys.stderr)
        raise SystemExit(2)
