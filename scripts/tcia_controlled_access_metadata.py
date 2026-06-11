#!/usr/bin/env python3
"""Build a SQLite metadata file for TCIA controlled-access GC/CTDC downloads.

The source of truth for scope is the TCIA WordPress snapshot. For each visible
current controlled-access download routed through General Commons or CTDC, this
script reads the public download manifest and the public download metadata
spreadsheet URL exposed by WordPress. It does not use authenticated APIs and it
does not download controlled data.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_REPO = "kirbyju/tcia-query-skill"
DEFAULT_RELEASE_TAG = "tcia-snapshot-latest"
CONTROLLED_ASSET = "controlled_access_metadata.sqlite.gz"
CONTROLLED_MANIFEST_ASSET = "controlled_access_metadata_manifest.json"
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_DB = SKILL_ROOT / "cache" / "tcia_snapshot.sqlite"
DEFAULT_OUT = SKILL_ROOT / "cache" / "controlled_access_metadata.sqlite"
DEFAULT_ARTIFACT_DIR = SKILL_ROOT / "cache" / "controlled_access_source_artifacts"
DEFAULT_MANIFEST = SKILL_ROOT / "cache" / CONTROLLED_MANIFEST_ASSET
DEFAULT_GZIP = SKILL_ROOT / "cache" / CONTROLLED_ASSET

CONTROLLED_POLICY_URL = (
    "https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/"
)
DRS_PREFIX = "drs://nci-crdc.datacommons.io/"
USER_AGENT = "tcia-controlled-access-metadata/0.1"
UID_RE = re.compile(r"^(?:[0-2])(?:\.\d+)+$")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")

REQUIRED_TABLES = [
    "controlled_meta",
    "controlled_downloads",
    "wordpress_download_metadata",
    "wordpress_download_urls",
    "wordpress_search_filters",
    "source_artifacts",
    "manifest_rows",
    "metadata_rows",
    "controlled_files",
    "radiology_series",
    "idc_index",
    "idc_ct_index",
    "idc_pt_index",
    "idc_contrast_index",
    "idc_series_links",
    "controlled_metadata_exceptions",
]
COUNT_TABLES = [table for table in REQUIRED_TABLES if table != "controlled_meta"]

IDC_INDEX_COLUMNS = [
    "collection_id",
    "analysis_result_id",
    "PatientID",
    "SeriesInstanceUID",
    "StudyInstanceUID",
    "source_DOI",
    "PatientAge",
    "PatientSex",
    "StudyDate",
    "StudyDescription",
    "BodyPartExamined",
    "Modality",
    "SOPClassUID",
    "sop_class_name",
    "TransferSyntaxUID",
    "transfer_syntax_name",
    "Manufacturer",
    "ManufacturerModelName",
    "SeriesDate",
    "SeriesDescription",
    "SeriesNumber",
    "instanceCount",
    "license_short_name",
    "series_init_idc_version",
    "series_revised_idc_version",
    "aws_bucket",
    "crdc_series_uuid",
    "series_aws_url",
    "series_size_MB",
]

IDC_CT_COLUMNS = [
    "SeriesInstanceUID",
    "ImageType",
    "PixelSpacing_row_mm",
    "PixelSpacing_col_mm",
    "Rows",
    "Columns",
    "SliceThickness",
    "KVP",
    "ScanOptions",
    "ConvolutionKernel",
    "GantryDetectorTilt",
    "XRayTubeCurrent_min",
    "XRayTubeCurrent_max",
    "FilterType",
    "Exposure_min",
    "Exposure_max",
    "ExposureTime_min",
    "ExposureTime_max",
    "DataCollectionDiameter",
    "ReconstructionDiameter",
    "SpiralPitchFactor",
]

IDC_PT_COLUMNS = [
    "SeriesInstanceUID",
    "SeriesType",
    "Units",
    "DecayCorrection",
    "CorrectedImage",
    "RandomsCorrectionMethod",
    "ReconstructionMethod",
    "ActualFrameDuration",
    "ScatterCorrectionMethod",
    "AttenuationCorrectionMethod",
    "RadionuclideCodeMeaning",
    "RadionuclideTotalDose",
    "RadiopharmaceuticalStartTime",
    "Radiopharmaceutical",
    "PixelSpacing_row_mm",
    "PixelSpacing_col_mm",
    "Rows",
    "Columns",
    "SliceThickness",
    "NumberOfSlices",
    "NumberOfTimeSlices",
]

IDC_CONTRAST_COLUMNS = [
    "SeriesInstanceUID",
    "ContrastBolusAgent",
    "ContrastBolusIngredient",
    "ContrastBolusRoute",
]


SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS controlled_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS controlled_downloads (
    download_row_id INTEGER PRIMARY KEY,
    parent_source TEXT NOT NULL,
    dataset_type TEXT NOT NULL,
    parent_id TEXT,
    parent_slug TEXT,
    short_title TEXT NOT NULL,
    title TEXT,
    doi TEXT,
    source_collections TEXT,
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
    access_level TEXT,
    controlled_access INTEGER NOT NULL DEFAULT 1,
    noncommercial_license INTEGER NOT NULL DEFAULT 0,
    download_size TEXT,
    download_size_unit TEXT,
    subjects TEXT,
    studies TEXT,
    series TEXT,
    images TEXT,
    download_types TEXT,
    data_types TEXT,
    file_types TEXT,
    external_resources TEXT,
    route_system TEXT NOT NULL,
    route_manifest_kind TEXT,
    metadata_artifact_kind TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS source_artifacts (
    artifact_id TEXT PRIMARY KEY,
    download_row_id INTEGER NOT NULL,
    artifact_role TEXT NOT NULL,
    route_system TEXT NOT NULL,
    url TEXT NOT NULL,
    local_path TEXT,
    file_name TEXT,
    artifact_kind TEXT,
    bytes INTEGER,
    sha256 TEXT,
    status TEXT NOT NULL,
    error TEXT,
    fetched_at TEXT,
    FOREIGN KEY (download_row_id) REFERENCES controlled_downloads(download_row_id)
);

CREATE TABLE IF NOT EXISTS manifest_rows (
    manifest_row_id TEXT PRIMARY KEY,
    download_row_id INTEGER NOT NULL,
    artifact_id TEXT NOT NULL,
    route_system TEXT NOT NULL,
    short_title TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    drs_uri TEXT,
    file_id TEXT,
    file_name TEXT,
    file_type TEXT,
    file_format TEXT,
    file_size_text TEXT,
    file_size_bytes INTEGER,
    study_name TEXT,
    study_accession TEXT,
    participant_id TEXT,
    sample_id TEXT,
    study_data_type TEXT,
    image_modality TEXT,
    series_instance_uid TEXT,
    raw_json TEXT NOT NULL,
    FOREIGN KEY (download_row_id) REFERENCES controlled_downloads(download_row_id),
    FOREIGN KEY (artifact_id) REFERENCES source_artifacts(artifact_id)
);

CREATE TABLE IF NOT EXISTS metadata_rows (
    metadata_row_id TEXT PRIMARY KEY,
    download_row_id INTEGER NOT NULL,
    artifact_id TEXT NOT NULL,
    route_system TEXT NOT NULL,
    short_title TEXT NOT NULL,
    sheet_name TEXT,
    row_number INTEGER NOT NULL,
    patient_id TEXT,
    patient_name TEXT,
    patient_sex TEXT,
    ethnic_group TEXT,
    species_code TEXT,
    species_description TEXT,
    phantom TEXT,
    study_instance_uid TEXT,
    study_date TEXT,
    study_description TEXT,
    admitting_diagnosis_description TEXT,
    study_id TEXT,
    patient_age TEXT,
    longitudinal_temporal_event_type TEXT,
    longitudinal_temporal_offset_from_event TEXT,
    series_instance_uid TEXT,
    project TEXT,
    modality TEXT,
    protocol_name TEXT,
    series_date TEXT,
    series_description TEXT,
    body_part_examined TEXT,
    series_number TEXT,
    annotations_flag TEXT,
    manufacturer TEXT,
    manufacturer_model_name TEXT,
    software_versions TEXT,
    image_count TEXT,
    max_submission_timestamp TEXT,
    collection_uri TEXT,
    file_size_text TEXT,
    file_size_bytes INTEGER,
    date_released TEXT,
    license_name TEXT,
    license_uri TEXT,
    third_party_analysis TEXT,
    pixel_spacing_row_mm TEXT,
    pixel_spacing_col_mm TEXT,
    slice_thickness_mm TEXT,
    raw_json TEXT NOT NULL,
    FOREIGN KEY (download_row_id) REFERENCES controlled_downloads(download_row_id),
    FOREIGN KEY (artifact_id) REFERENCES source_artifacts(artifact_id)
);

CREATE TABLE IF NOT EXISTS controlled_files (
    controlled_file_id TEXT PRIMARY KEY,
    download_row_id INTEGER NOT NULL,
    manifest_row_id TEXT,
    metadata_row_id TEXT,
    route_system TEXT NOT NULL,
    dataset_type TEXT NOT NULL,
    short_title TEXT NOT NULL,
    title TEXT,
    doi TEXT,
    source_collections TEXT,
    download_id TEXT,
    download_title TEXT,
    access_level TEXT NOT NULL,
    controlled_access_policy_url TEXT NOT NULL,
    license_label TEXT,
    license_url TEXT,
    drs_uri TEXT,
    file_id TEXT,
    file_name TEXT,
    file_ext TEXT,
    file_type TEXT,
    file_format TEXT,
    file_size_bytes INTEGER,
    checksum TEXT,
    checksum_algorithm TEXT,
    study_name TEXT,
    study_accession TEXT,
    participant_id TEXT,
    sample_id TEXT,
    patient_id TEXT,
    patient_name TEXT,
    patient_age TEXT,
    patient_sex TEXT,
    race TEXT,
    ethnicity TEXT,
    species_code TEXT,
    species_description TEXT,
    is_phantom TEXT,
    diagnosis TEXT,
    study_data_type TEXT,
    image_modality TEXT,
    study_instance_uid TEXT,
    series_instance_uid TEXT,
    modality TEXT,
    body_part_examined TEXT,
    study_date TEXT,
    series_date TEXT,
    study_description TEXT,
    series_description TEXT,
    series_number TEXT,
    protocol_name TEXT,
    annotations_flag TEXT,
    manufacturer TEXT,
    manufacturer_model_name TEXT,
    software_versions TEXT,
    image_count TEXT,
    collection_uri TEXT,
    date_released TEXT,
    longitudinal_temporal_event_type TEXT,
    longitudinal_temporal_offset_from_event TEXT,
    third_party_analysis TEXT,
    pixel_spacing_row_mm TEXT,
    pixel_spacing_col_mm TEXT,
    slice_thickness_mm TEXT,
    source_manifest_url TEXT,
    source_metadata_url TEXT,
    manifest_json TEXT,
    metadata_json TEXT,
    quality_flag_json TEXT,
    FOREIGN KEY (download_row_id) REFERENCES controlled_downloads(download_row_id),
    FOREIGN KEY (manifest_row_id) REFERENCES manifest_rows(manifest_row_id),
    FOREIGN KEY (metadata_row_id) REFERENCES metadata_rows(metadata_row_id)
);

CREATE TABLE IF NOT EXISTS wordpress_download_metadata (
    metadata_id TEXT PRIMARY KEY,
    download_row_id INTEGER NOT NULL,
    route_system TEXT NOT NULL,
    short_title TEXT NOT NULL,
    download_id TEXT,
    metadata_key TEXT NOT NULL,
    metadata_text TEXT,
    metadata_json TEXT NOT NULL,
    is_empty INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (download_row_id) REFERENCES controlled_downloads(download_row_id)
);

CREATE TABLE IF NOT EXISTS wordpress_download_urls (
    url_metadata_id TEXT PRIMARY KEY,
    download_row_id INTEGER NOT NULL,
    route_system TEXT NOT NULL,
    short_title TEXT NOT NULL,
    download_id TEXT,
    source_key TEXT NOT NULL,
    url_role TEXT NOT NULL,
    url TEXT NOT NULL,
    url_host TEXT,
    url_path TEXT,
    FOREIGN KEY (download_row_id) REFERENCES controlled_downloads(download_row_id)
);

CREATE TABLE IF NOT EXISTS wordpress_search_filters (
    download_row_id INTEGER PRIMARY KEY,
    route_system TEXT NOT NULL,
    short_title TEXT NOT NULL,
    download_id TEXT,
    search_url TEXT,
    downstream_system TEXT,
    study_names TEXT,
    study_ids TEXT,
    filter_json TEXT,
    FOREIGN KEY (download_row_id) REFERENCES controlled_downloads(download_row_id)
);

CREATE TABLE IF NOT EXISTS radiology_series (
    radiology_id TEXT PRIMARY KEY,
    controlled_file_id TEXT NOT NULL,
    short_title TEXT NOT NULL,
    dataset_type TEXT NOT NULL,
    download_id TEXT,
    file_name TEXT,
    subject_id TEXT,
    procedure_id TEXT,
    study_id TEXT,
    study_id_source TEXT,
    series_id TEXT,
    series_id_source TEXT,
    source_doi TEXT,
    modality TEXT,
    body_part_examined TEXT,
    study_date TEXT,
    series_date TEXT,
    study_description TEXT,
    series_description TEXT,
    series_number TEXT,
    manufacturer TEXT,
    manufacturer_model_name TEXT,
    software_versions TEXT,
    image_type TEXT,
    object_type TEXT,
    rows TEXT,
    columns TEXT,
    number_of_slices TEXT,
    number_of_temporal_positions TEXT,
    pixel_spacing_row_mm TEXT,
    pixel_spacing_col_mm TEXT,
    slice_thickness_mm TEXT,
    spacing_between_slices_mm TEXT,
    orientation_or_affine TEXT,
    is_phantom TEXT,
    is_derived_object INTEGER NOT NULL DEFAULT 0,
    quality_flag_json TEXT,
    FOREIGN KEY (controlled_file_id) REFERENCES controlled_files(controlled_file_id)
);

CREATE TABLE IF NOT EXISTS idc_index (
    {", ".join(f'"{column}" TEXT' for column in IDC_INDEX_COLUMNS)}
);

CREATE TABLE IF NOT EXISTS idc_ct_index (
    {", ".join(f'"{column}" TEXT' for column in IDC_CT_COLUMNS)}
);

CREATE TABLE IF NOT EXISTS idc_pt_index (
    {", ".join(f'"{column}" TEXT' for column in IDC_PT_COLUMNS)}
);

CREATE TABLE IF NOT EXISTS idc_contrast_index (
    {", ".join(f'"{column}" TEXT' for column in IDC_CONTRAST_COLUMNS)}
);

CREATE TABLE IF NOT EXISTS idc_series_links (
    controlled_file_id TEXT PRIMARY KEY,
    SeriesInstanceUID TEXT,
    route_system TEXT NOT NULL,
    short_title TEXT NOT NULL,
    download_row_id INTEGER NOT NULL,
    drs_uri TEXT,
    file_id TEXT,
    FOREIGN KEY (controlled_file_id) REFERENCES controlled_files(controlled_file_id)
);

CREATE TABLE IF NOT EXISTS controlled_metadata_exceptions (
    exception_id TEXT PRIMARY KEY,
    severity TEXT NOT NULL DEFAULT 'review',
    exception_type TEXT NOT NULL,
    route_system TEXT,
    short_title TEXT,
    download_row_id INTEGER,
    artifact_id TEXT,
    manifest_row_id TEXT,
    metadata_row_id TEXT,
    message TEXT NOT NULL,
    evidence_json TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_controlled_downloads_route
  ON controlled_downloads(route_system);
CREATE INDEX IF NOT EXISTS idx_manifest_rows_download
  ON manifest_rows(download_row_id);
CREATE INDEX IF NOT EXISTS idx_manifest_rows_series
  ON manifest_rows(series_instance_uid);
CREATE INDEX IF NOT EXISTS idx_manifest_rows_file_id
  ON manifest_rows(file_id);
CREATE INDEX IF NOT EXISTS idx_metadata_rows_download
  ON metadata_rows(download_row_id);
CREATE INDEX IF NOT EXISTS idx_metadata_rows_series
  ON metadata_rows(series_instance_uid);
CREATE INDEX IF NOT EXISTS idx_controlled_files_route
  ON controlled_files(route_system);
CREATE INDEX IF NOT EXISTS idx_controlled_files_short_title
  ON controlled_files(short_title);
CREATE INDEX IF NOT EXISTS idx_controlled_files_drs
  ON controlled_files(drs_uri);
CREATE INDEX IF NOT EXISTS idx_controlled_files_series
  ON controlled_files(series_instance_uid);
CREATE INDEX IF NOT EXISTS idx_wordpress_metadata_download
  ON wordpress_download_metadata(download_row_id);
CREATE INDEX IF NOT EXISTS idx_wordpress_metadata_key
  ON wordpress_download_metadata(metadata_key);
CREATE INDEX IF NOT EXISTS idx_wordpress_urls_role
  ON wordpress_download_urls(url_role);
CREATE INDEX IF NOT EXISTS idx_radiology_series_series_id
  ON radiology_series(series_id);
CREATE INDEX IF NOT EXISTS idx_idc_index_series
  ON idc_index(SeriesInstanceUID);
CREATE INDEX IF NOT EXISTS idx_idc_links_series
  ON idc_series_links(SeriesInstanceUID);

CREATE VIEW IF NOT EXISTS controlled_dataset_summary AS
SELECT
    route_system,
    dataset_type,
    short_title,
    COUNT(*) AS controlled_file_rows,
    COUNT(DISTINCT NULLIF(participant_id, '')) AS participant_ids,
    COUNT(DISTINCT NULLIF(patient_id, '')) AS patient_ids,
    COUNT(DISTINCT NULLIF(study_instance_uid, '')) AS study_instance_uids,
    COUNT(DISTINCT NULLIF(series_instance_uid, '')) AS series_instance_uids,
    SUM(COALESCE(file_size_bytes, 0)) AS total_file_size_bytes,
    group_concat(DISTINCT download_id) AS download_ids,
    group_concat(DISTINCT download_title) AS download_titles
FROM controlled_files
GROUP BY route_system, dataset_type, short_title
ORDER BY route_system, lower(short_title);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def db_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    env_path = os.environ.get("TCIA_CONTROLLED_ACCESS_METADATA_DB")
    if env_path:
        return Path(env_path)
    return DEFAULT_OUT


def manifest_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    env_path = os.environ.get("TCIA_CONTROLLED_ACCESS_METADATA_MANIFEST")
    if env_path:
        return Path(env_path)
    return DEFAULT_MANIFEST


def connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    resolved = db_path(path)
    if not resolved.exists():
        raise RuntimeError(
            f"Controlled-access metadata SQLite not found at {resolved}. "
            "Run `python scripts/tcia_controlled_access_metadata.py ensure` first."
        )
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_bytes(
    url: str, timeout: int = 120, headers: dict[str, str] | None = None
) -> tuple[bytes, dict[str, str]]:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(), dict(response.headers.items())


def github_api_json(url: str) -> Any:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body, _headers = fetch_bytes(url, timeout=60, headers=headers)
    return json.loads(body.decode("utf-8"))


def release_assets(repo: str, tag: str) -> dict[str, Any]:
    release = github_api_json(
        f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    )
    return {asset["name"]: asset for asset in release.get("assets") or []}


def clean_value(value: Any) -> str:
    if value is None:
        return ""
    try:
        if value != value:
            return ""
    except Exception:
        pass
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def null_if_blank(value: Any) -> str | None:
    text = clean_value(value)
    return text or None


def first_value(row: dict[str, Any], *names: str) -> str:
    lower_map = {key.lower().strip(): key for key in row}
    for name in names:
        key = lower_map.get(name.lower().strip())
        if key is not None:
            value = clean_value(row.get(key))
            if value:
                return value
    return ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_url_filename(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name or hashlib.sha256(url.encode("utf-8")).hexdigest()
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def artifact_kind_from_url(url: str) -> str:
    lower = urllib.parse.urlparse(url).path.lower()
    if lower.endswith(".csv"):
        return "csv"
    if lower.endswith(".tsv"):
        return "tsv"
    if lower.endswith(".xlsx"):
        return "xlsx"
    if lower.endswith(".xls"):
        return "xls"
    if lower.endswith(".tcia"):
        return "tcia"
    return "unknown"


def route_system(download_url: str, download_title: str, short_title: str) -> str:
    lower_url = (download_url or "").lower()
    lower_title = (download_title or "").lower()
    if "gc_manifest_" in lower_url or "general.datacommons.cancer.gov" in lower_url:
        return "general_commons"
    if "drs_metadata_manifest" in lower_url or short_title.startswith("CMB-"):
        return "ctdc"
    if lower_url.endswith(".tcia"):
        return "legacy_tcia_manifest"
    if "restricted" in lower_url or "restricted" in lower_title:
        return "controlled_metadata_only"
    return "controlled_metadata_only"


def route_manifest_kind(download_url: str) -> str:
    kind = artifact_kind_from_url(download_url or "")
    if kind == "csv":
        return "data_retriever_csv"
    if kind == "tcia":
        return "legacy_tcia"
    if kind in {"xlsx", "xls"}:
        return "spreadsheet"
    return kind


def metadata_text(value: Any) -> str:
    if value in (None, False):
        return ""
    if isinstance(value, str):
        return clean_value(value)
    if isinstance(value, list):
        return "; ".join(clean_value(item) for item in value if clean_value(item))
    if isinstance(value, dict):
        pieces = []
        for key in ("label", "text", "url"):
            text = clean_value(value.get(key))
            if text:
                pieces.append(text)
        return "; ".join(pieces)
    return clean_value(value)


def is_empty_metadata(value: Any) -> bool:
    return value in (None, "", [], {}, False, [False])


def classify_wordpress_url(source_key: str, url: str) -> str:
    key = source_key.lower()
    path = urllib.parse.urlparse(url).path.lower()
    if key == "download url":
        return "download_manifest"
    if key == "download metadata":
        return "download_metadata"
    if key == "search url":
        return "downstream_search"
    if key == "license":
        return "license"
    if key == "download requirements":
        return "download_requirement"
    if path.endswith((".xlsx", ".xls", ".csv", ".tsv", ".tcia")):
        return "artifact"
    return "url"


def parse_search_filter(search_url: str, route: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(search_url)
    downstream = ""
    study_names: list[str] = []
    study_ids: list[str] = []
    filter_payload: Any = {}
    host = parsed.netloc.lower()
    if "general.datacommons.cancer.gov" in host:
        downstream = "general_commons"
        marker = "/data/"
        if marker in search_url:
            encoded = search_url.split(marker, 1)[1]
            try:
                filter_payload = json.loads(urllib.parse.unquote(encoded))
                if isinstance(filter_payload, dict):
                    study_names = [
                        clean_value(item)
                        for item in filter_payload.get("studies", [])
                        if clean_value(item)
                    ]
            except json.JSONDecodeError:
                filter_payload = {"unparsed": encoded}
    elif "clinical.datacommons.cancer.gov" in host:
        downstream = "ctdc"
        if "#/study/" in search_url:
            study_ids = [clean_value(search_url.rsplit("#/study/", 1)[1])]
            filter_payload = {"study_id": study_ids[0]}
    return {
        "downstream_system": downstream or route,
        "study_names": "; ".join(study_names),
        "study_ids": "; ".join(study_ids),
        "filter_json": json_dumps(filter_payload) if filter_payload else "",
    }


def parse_size_to_bytes(value: str) -> int | None:
    text = clean_value(value)
    if not text:
        return None
    normalized = text.replace(",", "").strip()
    match = re.match(r"^([0-9]*\.?[0-9]+)\s*([A-Za-z]+)?$", normalized)
    if not match:
        return int(float(normalized)) if normalized.replace(".", "", 1).isdigit() else None
    number = float(match.group(1))
    unit = (match.group(2) or "B").lower()
    multiplier = {
        "b": 1,
        "byte": 1,
        "bytes": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
        "tb": 1000**4,
        "tib": 1024**4,
    }.get(unit)
    if multiplier is None:
        return None
    return int(round(number * multiplier))


def as_series_uid(value: str) -> str:
    text = clean_value(value)
    if not text:
        return ""
    name = Path(text).name
    if name.lower().endswith(".zip"):
        name = name[:-4]
    return name if UID_RE.match(name) else ""


def drs_uri_from_file_id(file_id: str) -> str:
    value = clean_value(file_id)
    if not value:
        return ""
    if value.startswith("drs://"):
        return value
    if value.startswith("dg."):
        return DRS_PREFIX + value
    return ""


def file_id_from_drs_uri(drs_uri: str) -> str:
    value = clean_value(drs_uri)
    if value.startswith(DRS_PREFIX):
        return value.removeprefix(DRS_PREFIX)
    return ""


def safe_id(*parts: Any) -> str:
    joined = "|".join(clean_value(part) for part in parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]
    prefix = re.sub(r"[^A-Za-z0-9_.:-]+", "_", clean_value(parts[0]) or "row")
    return f"{prefix}:{digest}"


def fetch_artifact(url: str, out_dir: Path, no_network: bool) -> tuple[Path | None, str, str]:
    if not url:
        return None, "missing", "blank URL"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / normalize_url_filename(url)
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path, "cached", ""
    if no_network:
        return None, "skipped", "network disabled and artifact is not cached"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            out_path.write_bytes(response.read())
        return out_path, "fetched", ""
    except Exception as exc:  # noqa: BLE001 - stored as artifact exception.
        return None, "error", str(exc)


def local_ctdc_fallback(url: str) -> Path | None:
    name = normalize_url_filename(url)
    candidates = [
        Path("output/ctdc_tcia_biobank_manifests_2026-05-28/drs_manifests_with_metadata")
        / name,
        Path("output/ctdc_tcia_biobank_manifests_2026-05-28/source_tcia") / name,
        Path("output/ctdc_tcia_biobank_manifests_2026-05-28/source_digest") / name,
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def write_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO controlled_meta(key, value) VALUES (?, ?)",
        (key, json_dumps(value)),
    )


def rows_as_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params)]


def require_source_views(conn: sqlite3.Connection) -> None:
    required = {"agent_current_downloads", "agent_datasets", "snapshot_meta"}
    found = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM source.sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    missing = sorted(required - found)
    if missing:
        raise RuntimeError(f"source snapshot missing required views: {', '.join(missing)}")


def source_snapshot_meta(conn: sqlite3.Connection) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for row in conn.execute("SELECT key, value FROM source.snapshot_meta"):
        try:
            meta[row["key"]] = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            meta[row["key"]] = row["value"]
    return meta


def seed_downloads(conn: sqlite3.Connection, include_legacy: bool) -> int:
    rows = rows_as_dicts(
        conn,
        """
        SELECT
            d.download_row_id,
            d.parent_source,
            d.dataset_type,
            d.parent_id,
            d.parent_slug,
            d.short_title,
            d.title,
            a.doi,
            a.source_collections,
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
            d.access_level,
            d.controlled_access,
            d.noncommercial_license,
            d.download_size,
            d.download_size_unit,
            d.subjects,
            d.studies,
            d.series,
            d.images,
            d.download_types,
            d.data_types,
            d.file_types,
            d.external_resources,
            d.raw_json
        FROM source.agent_current_downloads d
        LEFT JOIN source.agent_datasets a
          ON a.source = d.parent_source
         AND a.short_title = d.short_title
        WHERE d.hidden = 0
          AND d.controlled_access = 1
        ORDER BY lower(d.short_title), d.download_id
        """,
    )
    selected = []
    for row in rows:
        route = route_system(row.get("download_url") or "", row.get("download_title") or "", row.get("short_title") or "")
        if not include_legacy and route not in {"general_commons", "ctdc"}:
            continue
        row["route_system"] = route
        row["route_manifest_kind"] = route_manifest_kind(row.get("download_url") or "")
        row["metadata_artifact_kind"] = artifact_kind_from_url(row.get("download_metadata") or "")
        selected.append(row)

    if not selected:
        return 0
    columns = list(selected[0].keys())
    placeholders = ", ".join(":" + column for column in columns)
    conn.executemany(
        f"INSERT INTO controlled_downloads ({', '.join(columns)}) VALUES ({placeholders})",
        selected,
    )
    return len(selected)


def seed_wordpress_download_metadata(conn: sqlite3.Connection) -> dict[str, int]:
    metadata_rows = 0
    url_rows = 0
    search_rows = 0
    for download in rows_as_dicts(conn, "SELECT * FROM controlled_downloads ORDER BY download_row_id"):
        try:
            raw = json.loads(download.get("raw_json") or "{}")
        except json.JSONDecodeError:
            raw = {}
        for key, value in raw.items():
            metadata_id = safe_id("wpmeta", download["download_row_id"], key)
            metadata_json = json_dumps(value)
            text = metadata_text(value)
            conn.execute(
                """
                INSERT OR REPLACE INTO wordpress_download_metadata
                (metadata_id, download_row_id, route_system, short_title, download_id,
                 metadata_key, metadata_text, metadata_json, is_empty)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metadata_id,
                    download["download_row_id"],
                    download["route_system"],
                    download["short_title"],
                    download.get("download_id") or "",
                    key,
                    text,
                    metadata_json,
                    1 if is_empty_metadata(value) else 0,
                ),
            )
            metadata_rows += 1

            for url in URL_RE.findall(metadata_json if not isinstance(value, str) else value):
                cleaned_url = url.rstrip("\\,.;)")
                parsed = urllib.parse.urlparse(cleaned_url)
                url_id = safe_id("wpurl", download["download_row_id"], key, cleaned_url)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO wordpress_download_urls
                    (url_metadata_id, download_row_id, route_system, short_title, download_id,
                     source_key, url_role, url, url_host, url_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        url_id,
                        download["download_row_id"],
                        download["route_system"],
                        download["short_title"],
                        download.get("download_id") or "",
                        key,
                        classify_wordpress_url(key, cleaned_url),
                        cleaned_url,
                        parsed.netloc,
                        parsed.path,
                    ),
                )
                url_rows += 1

        search_url = clean_value(download.get("search_url"))
        if search_url:
            parsed_filter = parse_search_filter(search_url, download["route_system"])
            conn.execute(
                """
                INSERT OR REPLACE INTO wordpress_search_filters
                (download_row_id, route_system, short_title, download_id, search_url,
                 downstream_system, study_names, study_ids, filter_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    download["download_row_id"],
                    download["route_system"],
                    download["short_title"],
                    download.get("download_id") or "",
                    search_url,
                    parsed_filter["downstream_system"],
                    parsed_filter["study_names"],
                    parsed_filter["study_ids"],
                    parsed_filter["filter_json"],
                ),
            )
            search_rows += 1
    return {
        "wordpress_download_metadata": metadata_rows,
        "wordpress_download_urls": url_rows,
        "wordpress_search_filters": search_rows,
    }


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    data = path.read_bytes()
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [dict(row) for row in reader]


def read_tcia_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), start=1
    ):
        line = raw_line.strip()
        if UID_RE.match(line):
            rows.append({"Series Instance UID": line, "line_number": line_number})
    return rows


def read_spreadsheet_rows(path: Path) -> list[tuple[str, int, dict[str, Any]]]:
    import pandas as pd

    sheets = pd.read_excel(path, sheet_name=None, dtype=str)
    output = []
    for sheet_name, df in sheets.items():
        df = df.fillna("")
        for index, row in df.iterrows():
            values = {str(key): clean_value(value) for key, value in row.to_dict().items()}
            if any(values.values()):
                output.append((str(sheet_name), int(index) + 2, values))
    return output


def insert_artifact(
    conn: sqlite3.Connection,
    download: dict[str, Any],
    artifact_role: str,
    url: str,
    path: Path | None,
    status: str,
    error: str,
    fetched_at: str,
) -> str:
    artifact_id = safe_id("artifact", download["download_row_id"], artifact_role, url)
    bytes_count = path.stat().st_size if path and path.exists() else None
    sha = file_sha256(path) if path and path.exists() else ""
    conn.execute(
        """
        INSERT OR REPLACE INTO source_artifacts
        (artifact_id, download_row_id, artifact_role, route_system, url, local_path,
         file_name, artifact_kind, bytes, sha256, status, error, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            download["download_row_id"],
            artifact_role,
            download["route_system"],
            url,
            str(path) if path else "",
            path.name if path else normalize_url_filename(url),
            artifact_kind_from_url(url),
            bytes_count,
            sha,
            status,
            error,
            fetched_at,
        ),
    )
    return artifact_id


def normalize_manifest_row(row: dict[str, Any]) -> dict[str, Any]:
    file_id = first_value(row, "File ID", "file_id", "Access", "data_file_uuid")
    drs_uri = first_value(row, "drs_uri", "DRS URI", "Drs URI") or drs_uri_from_file_id(file_id)
    if not file_id:
        file_id = file_id_from_drs_uri(drs_uri)
    file_name = first_value(row, "File Name", "file_name", "CTDC File Name", "data_file_name")
    series_uid = (
        first_value(
            row,
            "Series Instance UID",
            "TCIA Series Instance UID",
            "SeriesInstanceUID",
            "Series UID",
        )
        or as_series_uid(file_name)
    )
    size_text = first_value(row, "File Size", "Size", "CTDC Size", "data_file_size")
    return {
        "drs_uri": drs_uri,
        "file_id": file_id,
        "file_name": file_name,
        "file_type": first_value(row, "File Type", "file_type", "data_file_type"),
        "file_format": first_value(row, "Format", "File Format", "data_file_format"),
        "file_size_text": size_text,
        "file_size_bytes": parse_size_to_bytes(size_text),
        "study_name": first_value(row, "Study Name", "study_name"),
        "study_accession": first_value(row, "Accession", "Study Accession", "CTDC Study Accession", "study_accession"),
        "participant_id": first_value(row, "Participant ID", "participant_id", "Patient ID", "PatientID"),
        "sample_id": first_value(row, "Sample ID", "sample_id", "Specimen Record ID"),
        "study_data_type": first_value(row, "Study Data Type", "study_data_type"),
        "image_modality": first_value(row, "Image Modality", "Modality", "image_modality"),
        "series_instance_uid": series_uid,
    }


def normalize_metadata_row(row: dict[str, Any]) -> dict[str, Any]:
    size_text = first_value(row, "File Size", "file_size")
    pixel_spacing = first_value(row, "Pixel Spacing(mm)", "Pixel Spacing")
    pixel_row = first_value(row, "Pixel Spacing(mm)- Row", "PixelSpacing_row_mm")
    pixel_col = first_value(row, "Pixel Spacing(mm)- Col", "PixelSpacing_col_mm")
    if pixel_spacing and not pixel_row:
        parts = re.split(r"[\\\\,;| ]+", pixel_spacing)
        if parts:
            pixel_row = parts[0]
        if len(parts) > 1:
            pixel_col = parts[1]
    return {
        "patient_id": first_value(row, "Patient ID", "PatientID"),
        "patient_name": first_value(row, "Patient Name", "PatientName"),
        "patient_sex": first_value(row, "Patient Sex", "PatientSex"),
        "ethnic_group": first_value(row, "Ethnic Group"),
        "species_code": first_value(row, "Species Code"),
        "species_description": first_value(row, "Species Description"),
        "phantom": first_value(row, "Phantom"),
        "study_instance_uid": first_value(row, "Study Instance UID", "StudyInstanceUID"),
        "study_date": first_value(row, "Study Date", "StudyDate"),
        "study_description": first_value(row, "Study Description", "StudyDescription"),
        "admitting_diagnosis_description": first_value(row, "Admitting Diagnosis Description"),
        "study_id": first_value(row, "Study ID"),
        "patient_age": first_value(row, "Patient Age", "PatientAge"),
        "longitudinal_temporal_event_type": first_value(row, "Longitudinal Temporal Event Type"),
        "longitudinal_temporal_offset_from_event": first_value(row, "Longitudinal Temporal Offset From Event"),
        "series_instance_uid": first_value(row, "Series Instance UID", "SeriesInstanceUID", "Series UID"),
        "project": first_value(row, "Project"),
        "modality": first_value(row, "Modality"),
        "protocol_name": first_value(row, "Protocol Name"),
        "series_date": first_value(row, "Series Date", "SeriesDate"),
        "series_description": first_value(row, "Series Description", "SeriesDescription"),
        "body_part_examined": first_value(row, "Body Part Examined", "BodyPartExamined"),
        "series_number": first_value(row, "Series Number", "SeriesNumber"),
        "annotations_flag": first_value(row, "Annotations Flag"),
        "manufacturer": first_value(row, "Manufacturer"),
        "manufacturer_model_name": first_value(row, "Manufacturer Model Name", "ManufacturerModelName"),
        "software_versions": first_value(row, "Software Versions"),
        "image_count": first_value(row, "Image Count", "instanceCount"),
        "max_submission_timestamp": first_value(row, "Max Submission Timestamp"),
        "collection_uri": first_value(row, "Collection URI"),
        "file_size_text": size_text,
        "file_size_bytes": parse_size_to_bytes(size_text),
        "date_released": first_value(row, "Date Released"),
        "license_name": first_value(row, "License Name"),
        "license_uri": first_value(row, "License URI"),
        "third_party_analysis": first_value(row, "Third Party Analysis"),
        "pixel_spacing_row_mm": pixel_row,
        "pixel_spacing_col_mm": pixel_col,
        "slice_thickness_mm": first_value(row, "Slice Thickness(mm)", "SliceThickness"),
    }


def ingest_artifacts(conn: sqlite3.Connection, artifact_dir: Path, no_network: bool) -> dict[str, int]:
    fetched_at = utc_now()
    counts = {
        "download_artifacts": 0,
        "metadata_artifacts": 0,
        "manifest_rows": 0,
        "metadata_rows": 0,
        "artifact_errors": 0,
    }
    downloads = rows_as_dicts(conn, "SELECT * FROM controlled_downloads ORDER BY download_row_id")
    for download in downloads:
        for role, url_key in (("download_url", "download_url"), ("download_metadata", "download_metadata")):
            url = clean_value(download.get(url_key))
            if not url:
                continue
            path, status, error = fetch_artifact(
                url,
                artifact_dir / download["route_system"] / role,
                no_network=no_network,
            )
            if path is None and download["route_system"] == "ctdc":
                fallback = local_ctdc_fallback(url)
                if fallback is not None:
                    path = fallback
                    status = "local_fallback"
                    error = ""
            artifact_id = insert_artifact(
                conn, download, role, url, path, status, error, fetched_at
            )
            if error:
                counts["artifact_errors"] += 1
                insert_exception(
                    conn,
                    "artifact_fetch_failed",
                    "warning",
                    download,
                    f"Could not fetch public {role} artifact.",
                    {"url": url, "status": status, "error": error},
                    artifact_id=artifact_id,
                )
                continue
            if path is None:
                continue

            kind = artifact_kind_from_url(url)
            if role == "download_url" and kind in {"csv", "tsv", "tcia"}:
                rows = read_tcia_rows(path) if kind == "tcia" else read_csv_rows(path)
                for idx, row in enumerate(rows, start=1):
                    normalized = normalize_manifest_row(row)
                    manifest_row_id = safe_id("manifest", download["download_row_id"], artifact_id, idx)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO manifest_rows
                        (manifest_row_id, download_row_id, artifact_id, route_system, short_title,
                         row_number, drs_uri, file_id, file_name, file_type, file_format,
                         file_size_text, file_size_bytes, study_name, study_accession,
                         participant_id, sample_id, study_data_type, image_modality,
                         series_instance_uid, raw_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            manifest_row_id,
                            download["download_row_id"],
                            artifact_id,
                            download["route_system"],
                            download["short_title"],
                            idx,
                            normalized["drs_uri"],
                            normalized["file_id"],
                            normalized["file_name"],
                            normalized["file_type"],
                            normalized["file_format"],
                            normalized["file_size_text"],
                            normalized["file_size_bytes"],
                            normalized["study_name"],
                            normalized["study_accession"],
                            normalized["participant_id"],
                            normalized["sample_id"],
                            normalized["study_data_type"],
                            normalized["image_modality"],
                            normalized["series_instance_uid"],
                            json_dumps({str(k): clean_value(v) for k, v in row.items()}),
                        ),
                    )
                counts["download_artifacts"] += 1
                counts["manifest_rows"] += len(rows)
            elif role == "download_metadata" and kind in {"xlsx", "xls"}:
                rows = read_spreadsheet_rows(path)
                for sheet_name, row_number, row in rows:
                    normalized = normalize_metadata_row(row)
                    metadata_row_id = safe_id(
                        "metadata", download["download_row_id"], artifact_id, sheet_name, row_number
                    )
                    columns = [
                        "metadata_row_id",
                        "download_row_id",
                        "artifact_id",
                        "route_system",
                        "short_title",
                        "sheet_name",
                        "row_number",
                        *normalized.keys(),
                        "raw_json",
                    ]
                    values = {
                        "metadata_row_id": metadata_row_id,
                        "download_row_id": download["download_row_id"],
                        "artifact_id": artifact_id,
                        "route_system": download["route_system"],
                        "short_title": download["short_title"],
                        "sheet_name": sheet_name,
                        "row_number": row_number,
                        **normalized,
                        "raw_json": json_dumps(row),
                    }
                    conn.execute(
                        f"""
                        INSERT OR REPLACE INTO metadata_rows
                        ({", ".join(columns)})
                        VALUES ({", ".join(":" + column for column in columns)})
                        """,
                        values,
                    )
                counts["metadata_artifacts"] += 1
                counts["metadata_rows"] += len(rows)
    return counts


def insert_exception(
    conn: sqlite3.Connection,
    exception_type: str,
    severity: str,
    download: dict[str, Any] | None,
    message: str,
    evidence: dict[str, Any],
    artifact_id: str = "",
    manifest_row_id: str = "",
    metadata_row_id: str = "",
) -> None:
    created_at = utc_now()
    short_title = clean_value(download.get("short_title")) if download else ""
    download_row_id = download.get("download_row_id") if download else None
    route = clean_value(download.get("route_system")) if download else ""
    exception_id = safe_id(
        "exception",
        exception_type,
        route,
        short_title,
        download_row_id,
        artifact_id,
        manifest_row_id,
        metadata_row_id,
        message,
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO controlled_metadata_exceptions
        (exception_id, severity, exception_type, route_system, short_title,
         download_row_id, artifact_id, manifest_row_id, metadata_row_id,
         message, evidence_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            exception_id,
            severity,
            exception_type,
            route,
            short_title,
            download_row_id,
            artifact_id,
            manifest_row_id,
            metadata_row_id,
            message,
            json_dumps(evidence),
            created_at,
        ),
    )


def metadata_by_download_and_series(conn: sqlite3.Connection) -> dict[int, dict[str, dict[str, Any]]]:
    output: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows_as_dicts(
        conn,
        """
        SELECT *
        FROM metadata_rows
        WHERE COALESCE(series_instance_uid, '') <> ''
        ORDER BY download_row_id, row_number
        """,
    ):
        output.setdefault(int(row["download_row_id"]), {}).setdefault(
            row["series_instance_uid"], row
        )
    return output


def manifest_by_download_and_series(conn: sqlite3.Connection) -> dict[int, dict[str, dict[str, Any]]]:
    output: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows_as_dicts(
        conn,
        """
        SELECT *
        FROM manifest_rows
        WHERE COALESCE(series_instance_uid, '') <> ''
        ORDER BY download_row_id, row_number
        """,
    ):
        output.setdefault(int(row["download_row_id"]), {}).setdefault(
            row["series_instance_uid"], row
        )
    return output


def choose_file_size(manifest: dict[str, Any] | None, metadata: dict[str, Any] | None) -> int | None:
    for row in (metadata, manifest):
        if row and row.get("file_size_bytes") is not None:
            return int(row["file_size_bytes"])
    return None


def merge_controlled_file(
    download: dict[str, Any],
    manifest: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    manifest_json = manifest.get("raw_json") if manifest else ""
    metadata_json = metadata.get("raw_json") if metadata else ""
    manifest_payload = json.loads(manifest_json) if manifest_json else {}
    metadata_payload = json.loads(metadata_json) if metadata_json else {}

    file_id = clean_value((manifest or {}).get("file_id"))
    drs_uri = clean_value((manifest or {}).get("drs_uri")) or drs_uri_from_file_id(file_id)
    if not file_id:
        file_id = file_id_from_drs_uri(drs_uri)
    series_uid = clean_value((metadata or {}).get("series_instance_uid")) or clean_value(
        (manifest or {}).get("series_instance_uid")
    )
    file_name = clean_value((manifest or {}).get("file_name")) or (
        f"{series_uid}.zip" if series_uid else ""
    )
    metadata_url = clean_value(download.get("download_metadata"))
    manifest_url = clean_value(download.get("download_url"))
    file_ext = Path(file_name).suffix.lower().lstrip(".") if file_name else ""
    modality = clean_value((metadata or {}).get("modality")) or clean_value(
        (manifest or {}).get("image_modality")
    )
    image_modality = clean_value((manifest or {}).get("image_modality")) or modality
    participant_id = clean_value((manifest or {}).get("participant_id"))
    patient_id = (
        clean_value((metadata or {}).get("patient_id"))
        or first_value(manifest_payload, "Patient ID", "PatientID")
        or participant_id
    )
    diagnosis = (
        first_value(manifest_payload, "CTDC Diagnosis", "Diagnosis")
        or clean_value((metadata or {}).get("admitting_diagnosis_description"))
    )
    controlled_file_id = safe_id(
        "controlled_file",
        download["route_system"],
        download["download_row_id"],
        drs_uri,
        file_id,
        series_uid,
        file_name,
        (manifest or {}).get("manifest_row_id", ""),
        (metadata or {}).get("metadata_row_id", ""),
    )
    return {
        "controlled_file_id": controlled_file_id,
        "download_row_id": download["download_row_id"],
        "manifest_row_id": null_if_blank((manifest or {}).get("manifest_row_id")),
        "metadata_row_id": null_if_blank((metadata or {}).get("metadata_row_id")),
        "route_system": download["route_system"],
        "dataset_type": download["dataset_type"],
        "short_title": download["short_title"],
        "title": download["title"],
        "doi": download.get("doi") or clean_value((metadata or {}).get("collection_uri")),
        "source_collections": download.get("source_collections") or "",
        "download_id": download.get("download_id") or "",
        "download_title": download.get("download_title") or "",
        "access_level": "controlled",
        "controlled_access_policy_url": CONTROLLED_POLICY_URL,
        "license_label": download.get("license_label") or clean_value((metadata or {}).get("license_name")),
        "license_url": download.get("license_url") or clean_value((metadata or {}).get("license_uri")),
        "drs_uri": drs_uri,
        "file_id": file_id,
        "file_name": file_name,
        "file_ext": file_ext,
        "file_type": clean_value((manifest or {}).get("file_type")),
        "file_format": clean_value((manifest or {}).get("file_format")),
        "file_size_bytes": choose_file_size(manifest, metadata),
        "checksum": first_value(manifest_payload, "data_file_checksum_value", "md5sum"),
        "checksum_algorithm": first_value(manifest_payload, "data_file_checksum_type"),
        "study_name": clean_value((manifest or {}).get("study_name")),
        "study_accession": clean_value((manifest or {}).get("study_accession")),
        "participant_id": participant_id,
        "sample_id": clean_value((manifest or {}).get("sample_id")),
        "patient_id": patient_id,
        "patient_name": clean_value((metadata or {}).get("patient_name")) or first_value(manifest_payload, "Patient Name"),
        "patient_age": clean_value((metadata or {}).get("patient_age")) or first_value(manifest_payload, "Patient Age"),
        "patient_sex": clean_value((metadata or {}).get("patient_sex")) or first_value(manifest_payload, "Patient Sex"),
        "race": first_value(manifest_payload, "Race"),
        "ethnicity": clean_value((metadata or {}).get("ethnic_group")) or first_value(manifest_payload, "Ethnicity", "Ethnic Group"),
        "species_code": clean_value((metadata or {}).get("species_code")) or first_value(manifest_payload, "Species Code"),
        "species_description": clean_value((metadata or {}).get("species_description")) or first_value(manifest_payload, "Species Description"),
        "is_phantom": clean_value((metadata or {}).get("phantom")) or first_value(manifest_payload, "Phantom"),
        "diagnosis": diagnosis,
        "study_data_type": clean_value((manifest or {}).get("study_data_type")),
        "image_modality": image_modality,
        "study_instance_uid": clean_value((metadata or {}).get("study_instance_uid")) or first_value(manifest_payload, "Study Instance UID", "StudyInstanceUID"),
        "series_instance_uid": series_uid,
        "modality": modality,
        "body_part_examined": clean_value((metadata or {}).get("body_part_examined")) or first_value(manifest_payload, "Body Part Examined", "BodyPartExamined"),
        "study_date": clean_value((metadata or {}).get("study_date")) or first_value(manifest_payload, "Study Date", "StudyDate"),
        "series_date": clean_value((metadata or {}).get("series_date")) or first_value(manifest_payload, "Series Date", "SeriesDate"),
        "study_description": clean_value((metadata or {}).get("study_description")) or first_value(manifest_payload, "Study Description", "StudyDescription"),
        "series_description": clean_value((metadata or {}).get("series_description")) or first_value(manifest_payload, "Series Description", "SeriesDescription"),
        "series_number": clean_value((metadata or {}).get("series_number")) or first_value(manifest_payload, "Series Number", "SeriesNumber"),
        "protocol_name": clean_value((metadata or {}).get("protocol_name")) or first_value(manifest_payload, "Protocol Name"),
        "annotations_flag": clean_value((metadata or {}).get("annotations_flag")) or first_value(manifest_payload, "Annotations Flag"),
        "manufacturer": clean_value((metadata or {}).get("manufacturer")) or first_value(manifest_payload, "Manufacturer"),
        "manufacturer_model_name": clean_value((metadata or {}).get("manufacturer_model_name")) or first_value(manifest_payload, "Manufacturer Model Name", "ManufacturerModelName"),
        "software_versions": clean_value((metadata or {}).get("software_versions")) or first_value(manifest_payload, "Software Versions"),
        "image_count": clean_value((metadata or {}).get("image_count")) or first_value(manifest_payload, "Image Count", "instanceCount"),
        "collection_uri": clean_value((metadata or {}).get("collection_uri")) or first_value(manifest_payload, "Collection URI"),
        "date_released": clean_value((metadata or {}).get("date_released")) or first_value(manifest_payload, "Date Released"),
        "longitudinal_temporal_event_type": clean_value((metadata or {}).get("longitudinal_temporal_event_type")) or first_value(manifest_payload, "Longitudinal Temporal Event Type"),
        "longitudinal_temporal_offset_from_event": clean_value((metadata or {}).get("longitudinal_temporal_offset_from_event")) or first_value(manifest_payload, "Longitudinal Temporal Offset From Event"),
        "third_party_analysis": clean_value((metadata or {}).get("third_party_analysis")) or first_value(manifest_payload, "Third Party Analysis"),
        "pixel_spacing_row_mm": clean_value((metadata or {}).get("pixel_spacing_row_mm")) or first_value(manifest_payload, "Pixel Spacing(mm)- Row", "PixelSpacing_row_mm"),
        "pixel_spacing_col_mm": clean_value((metadata or {}).get("pixel_spacing_col_mm")) or first_value(manifest_payload, "Pixel Spacing(mm)- Col", "PixelSpacing_col_mm"),
        "slice_thickness_mm": clean_value((metadata or {}).get("slice_thickness_mm")) or first_value(manifest_payload, "Slice Thickness(mm)", "SliceThickness"),
        "source_manifest_url": manifest_url,
        "source_metadata_url": metadata_url,
        "manifest_json": manifest_json,
        "metadata_json": metadata_json,
        "quality_flag_json": "",
    }


def populate_controlled_files(conn: sqlite3.Connection) -> int:
    metadata_index = metadata_by_download_and_series(conn)
    manifest_index = manifest_by_download_and_series(conn)
    inserted = 0
    downloads = rows_as_dicts(conn, "SELECT * FROM controlled_downloads ORDER BY download_row_id")
    for download in downloads:
        download_id = int(download["download_row_id"])
        manifests = rows_as_dicts(
            conn,
            "SELECT * FROM manifest_rows WHERE download_row_id = ? ORDER BY row_number",
            (download_id,),
        )
        metadata_for_download = metadata_index.get(download_id, {})
        matched_metadata_ids = set()

        for manifest in manifests:
            series_uid = clean_value(manifest.get("series_instance_uid"))
            metadata = metadata_for_download.get(series_uid) if series_uid else None
            if metadata:
                matched_metadata_ids.add(metadata["metadata_row_id"])
            row = merge_controlled_file(download, manifest, metadata)
            insert_controlled_file(conn, row)
            inserted += 1

        for metadata in rows_as_dicts(
            conn,
            "SELECT * FROM metadata_rows WHERE download_row_id = ? ORDER BY row_number",
            (download_id,),
        ):
            if metadata["metadata_row_id"] in matched_metadata_ids:
                continue
            series_uid = clean_value(metadata.get("series_instance_uid"))
            if series_uid and manifest_index.get(download_id, {}).get(series_uid):
                continue
            row = merge_controlled_file(download, None, metadata)
            insert_controlled_file(conn, row)
            inserted += 1

    return inserted


def insert_controlled_file(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    columns = list(row.keys())
    conn.execute(
        f"""
        INSERT OR REPLACE INTO controlled_files
        ({", ".join(columns)})
        VALUES ({", ".join(":" + column for column in columns)})
        """,
        row,
    )


def mb_string(bytes_count: int | None) -> str:
    if bytes_count is None:
        return ""
    return f"{bytes_count / 1000 / 1000:.6g}"


def collection_or_analysis(row: dict[str, Any]) -> tuple[str, str]:
    if row["dataset_type"] == "Analysis Result":
        source_collection = clean_value(row.get("source_collections"))
        return source_collection, row["short_title"]
    return row["short_title"], ""


def populate_normalized_indexes(conn: sqlite3.Connection) -> dict[str, int]:
    rows = rows_as_dicts(conn, "SELECT * FROM controlled_files ORDER BY controlled_file_id")
    for row in rows:
        if not clean_value(row.get("series_instance_uid")):
            continue
        radiology = {
            "radiology_id": safe_id("radiology", row["controlled_file_id"]),
            "controlled_file_id": row["controlled_file_id"],
            "short_title": row["short_title"],
            "dataset_type": row["dataset_type"],
            "download_id": row["download_id"],
            "file_name": row["file_name"],
            "subject_id": row["patient_id"] or row["participant_id"],
            "procedure_id": "",
            "study_id": row["study_instance_uid"],
            "study_id_source": "StudyInstanceUID" if row["study_instance_uid"] else "",
            "series_id": row["series_instance_uid"],
            "series_id_source": "SeriesInstanceUID",
            "source_doi": row["doi"],
            "modality": row["modality"] or row["image_modality"],
            "body_part_examined": row["body_part_examined"],
            "study_date": row["study_date"],
            "series_date": row["series_date"],
            "study_description": row["study_description"],
            "series_description": row["series_description"],
            "series_number": row["series_number"],
            "manufacturer": row["manufacturer"],
            "manufacturer_model_name": row["manufacturer_model_name"],
            "software_versions": row["software_versions"],
            "image_type": "",
            "object_type": row["file_type"] or row["study_data_type"],
            "rows": "",
            "columns": "",
            "number_of_slices": row["image_count"],
            "number_of_temporal_positions": "",
            "pixel_spacing_row_mm": row["pixel_spacing_row_mm"],
            "pixel_spacing_col_mm": row["pixel_spacing_col_mm"],
            "slice_thickness_mm": row["slice_thickness_mm"],
            "spacing_between_slices_mm": "",
            "orientation_or_affine": "",
            "is_phantom": row["is_phantom"],
            "is_derived_object": 1 if (row["file_type"] or "").upper() in {"SEG", "RTSTRUCT", "SR"} else 0,
            "quality_flag_json": row["quality_flag_json"],
        }
        conn.execute(
            f"""
            INSERT OR REPLACE INTO radiology_series
            ({", ".join(radiology.keys())})
            VALUES ({", ".join(":" + column for column in radiology)})
            """,
            radiology,
        )

        collection_id, analysis_result_id = collection_or_analysis(row)
        idc_row = {
            "collection_id": collection_id,
            "analysis_result_id": analysis_result_id,
            "PatientID": row["patient_id"] or row["participant_id"],
            "SeriesInstanceUID": row["series_instance_uid"],
            "StudyInstanceUID": row["study_instance_uid"],
            "source_DOI": row["doi"],
            "PatientAge": row["patient_age"],
            "PatientSex": row["patient_sex"],
            "StudyDate": row["study_date"],
            "StudyDescription": row["study_description"],
            "BodyPartExamined": row["body_part_examined"],
            "Modality": row["modality"] or row["image_modality"],
            "SOPClassUID": "",
            "sop_class_name": "",
            "TransferSyntaxUID": "",
            "transfer_syntax_name": "",
            "Manufacturer": row["manufacturer"],
            "ManufacturerModelName": row["manufacturer_model_name"],
            "SeriesDate": row["series_date"],
            "SeriesDescription": row["series_description"],
            "SeriesNumber": row["series_number"],
            "instanceCount": row["image_count"],
            "license_short_name": row["license_label"],
            "series_init_idc_version": "",
            "series_revised_idc_version": "",
            "aws_bucket": "",
            "crdc_series_uuid": "",
            "series_aws_url": "",
            "series_size_MB": mb_string(row["file_size_bytes"]),
        }
        conn.execute(
            f"""
            INSERT INTO idc_index
            ({", ".join(IDC_INDEX_COLUMNS)})
            VALUES ({", ".join(":" + column for column in IDC_INDEX_COLUMNS)})
            """,
            idc_row,
        )
        link = {
            "controlled_file_id": row["controlled_file_id"],
            "SeriesInstanceUID": row["series_instance_uid"],
            "route_system": row["route_system"],
            "short_title": row["short_title"],
            "download_row_id": row["download_row_id"],
            "drs_uri": row["drs_uri"],
            "file_id": row["file_id"],
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO idc_series_links
            (controlled_file_id, SeriesInstanceUID, route_system, short_title,
             download_row_id, drs_uri, file_id)
            VALUES (:controlled_file_id, :SeriesInstanceUID, :route_system, :short_title,
                    :download_row_id, :drs_uri, :file_id)
            """,
            link,
        )
        modality = (idc_row["Modality"] or "").upper()
        if modality == "CT":
            ct_row = {column: "" for column in IDC_CT_COLUMNS}
            ct_row.update(
                {
                    "SeriesInstanceUID": row["series_instance_uid"],
                    "PixelSpacing_row_mm": row["pixel_spacing_row_mm"],
                    "PixelSpacing_col_mm": row["pixel_spacing_col_mm"],
                    "SliceThickness": row["slice_thickness_mm"],
                }
            )
            conn.execute(
                f"""
                INSERT INTO idc_ct_index
                ({", ".join(IDC_CT_COLUMNS)})
                VALUES ({", ".join(":" + column for column in IDC_CT_COLUMNS)})
                """,
                ct_row,
            )
        elif modality == "PT":
            pt_row = {column: "" for column in IDC_PT_COLUMNS}
            pt_row.update(
                {
                    "SeriesInstanceUID": row["series_instance_uid"],
                    "PixelSpacing_row_mm": row["pixel_spacing_row_mm"],
                    "PixelSpacing_col_mm": row["pixel_spacing_col_mm"],
                    "SliceThickness": row["slice_thickness_mm"],
                    "NumberOfSlices": row["image_count"],
                }
            )
            conn.execute(
                f"""
                INSERT INTO idc_pt_index
                ({", ".join(IDC_PT_COLUMNS)})
                VALUES ({", ".join(":" + column for column in IDC_PT_COLUMNS)})
                """,
                pt_row,
            )

    return {
        "radiology_series": conn.execute("SELECT COUNT(*) FROM radiology_series").fetchone()[0],
        "idc_index": conn.execute("SELECT COUNT(*) FROM idc_index").fetchone()[0],
        "idc_ct_index": conn.execute("SELECT COUNT(*) FROM idc_ct_index").fetchone()[0],
        "idc_pt_index": conn.execute("SELECT COUNT(*) FROM idc_pt_index").fetchone()[0],
    }


def seed_exceptions(conn: sqlite3.Connection) -> int:
    created_at = utc_now()
    conn.execute(
        """
        INSERT OR REPLACE INTO controlled_metadata_exceptions
        (exception_id, severity, exception_type, route_system, short_title,
         download_row_id, manifest_row_id, message, evidence_json, created_at)
        SELECT
            'manifest_without_drs:' || manifest_row_id,
            'warning',
            'manifest_row_without_drs_uri',
            route_system,
            short_title,
            download_row_id,
            manifest_row_id,
            'Manifest row lacks a drs_uri and no DRS URI could be derived from File ID.',
            raw_json,
            ?
        FROM manifest_rows
        WHERE COALESCE(drs_uri, '') = ''
        """,
        (created_at,),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO controlled_metadata_exceptions
        (exception_id, severity, exception_type, route_system, short_title,
         download_row_id, manifest_row_id, message, evidence_json, created_at)
        SELECT
            'manifest_without_series:' || manifest_row_id,
            'review',
            'manifest_row_without_series_uid',
            route_system,
            short_title,
            download_row_id,
            manifest_row_id,
            'Manifest row lacks Series Instance UID metadata and file name does not look like a series UID zip.',
            raw_json,
            ?
        FROM manifest_rows
        WHERE COALESCE(series_instance_uid, '') = ''
        """,
        (created_at,),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO controlled_metadata_exceptions
        (exception_id, severity, exception_type, route_system, short_title,
         download_row_id, metadata_row_id, message, evidence_json, created_at)
        SELECT
            'metadata_without_manifest:' || m.metadata_row_id,
            'review',
            'metadata_series_without_manifest_row',
            m.route_system,
            m.short_title,
            m.download_row_id,
            m.metadata_row_id,
            'Metadata spreadsheet row has no matching manifest row for the same download and Series Instance UID.',
            m.raw_json,
            ?
        FROM metadata_rows m
        LEFT JOIN manifest_rows r
          ON r.download_row_id = m.download_row_id
         AND r.series_instance_uid = m.series_instance_uid
        WHERE COALESCE(m.series_instance_uid, '') <> ''
          AND r.manifest_row_id IS NULL
        """,
        (created_at,),
    )
    return conn.execute("SELECT COUNT(*) FROM controlled_metadata_exceptions").fetchone()[0]


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in COUNT_TABLES
    }


def table_signature(
    conn: sqlite3.Connection,
    table: str,
    order_by: str,
    exclude_columns: set[str] | None = None,
) -> str:
    exclude = exclude_columns or set()
    columns = [
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})")
        if row[1] not in exclude
    ]
    digest = hashlib.sha256()
    digest.update(json_dumps({"table": table, "columns": columns}).encode("utf-8"))
    for row in conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_by}"
    ):
        payload = {column: row[index] for index, column in enumerate(columns)}
        digest.update(json_dumps(payload).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def release_fingerprint(payload: dict[str, Any]) -> str:
    stable_payload = {
        "schema_version": payload.get("schema_version"),
        "controlled_download_signature": payload.get("controlled_download_signature"),
        "controlled_file_signature": payload.get("controlled_file_signature"),
        "manifest_row_signature": payload.get("manifest_row_signature"),
        "metadata_row_signature": payload.get("metadata_row_signature"),
        "wordpress_metadata_signature": payload.get("wordpress_metadata_signature"),
        "wordpress_url_signature": payload.get("wordpress_url_signature"),
        "table_counts": payload.get("table_counts"),
        "integrity_check": payload.get("integrity_check"),
        "no_network": payload.get("no_network"),
        "include_legacy": payload.get("include_legacy"),
    }
    return hashlib.sha256(json_dumps(stable_payload).encode("utf-8")).hexdigest()


def get_controlled_meta(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        rows = conn.execute("SELECT key, value FROM controlled_meta")
    except sqlite3.Error:
        return {}
    meta: dict[str, Any] = {}
    for key, value in rows:
        try:
            meta[key] = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            meta[key] = value
    return meta


def validate_db(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(path)
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    missing = sorted(set(REQUIRED_TABLES) - existing)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    counts = table_counts(conn) if not missing else {}
    conn.close()
    return {"integrity_check": integrity, "missing_tables": missing, "table_counts": counts}


def gzip_sqlite(sqlite_path: Path, gzip_path: Path) -> None:
    gzip_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite_path.open("rb") as src, gzip.open(gzip_path, "wb") as dst:
        shutil.copyfileobj(src, dst)


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_manifest_from_db(
    sqlite_path: Path,
    gzip_path: Path | None = None,
    include_legacy: bool = False,
    no_network: bool = False,
) -> dict[str, Any]:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    counts = table_counts(conn)
    signatures = {
        "controlled_download_signature": table_signature(
            conn, "controlled_downloads", "download_row_id"
        ),
        "controlled_file_signature": table_signature(
            conn, "controlled_files", "controlled_file_id"
        ),
        "manifest_row_signature": table_signature(conn, "manifest_rows", "manifest_row_id"),
        "metadata_row_signature": table_signature(conn, "metadata_rows", "metadata_row_id"),
        "wordpress_metadata_signature": table_signature(
            conn, "wordpress_download_metadata", "metadata_id"
        ),
        "wordpress_url_signature": table_signature(
            conn, "wordpress_download_urls", "url_metadata_id"
        ),
    }
    meta = get_controlled_meta(conn)
    conn.close()
    payload: dict[str, Any] = {
        "asset": CONTROLLED_ASSET,
        "sqlite_asset": sqlite_path.name,
        "created_at": utc_now(),
        "schema_version": SCHEMA_VERSION,
        "sqlite_path": str(sqlite_path.resolve()),
        "sqlite_sha256": file_sha256(sqlite_path),
        "sqlite_bytes": sqlite_path.stat().st_size,
        "gzip_path": str(gzip_path.resolve()) if gzip_path else "",
        "gzip_sha256": file_sha256(gzip_path) if gzip_path and gzip_path.exists() else "",
        "gzip_bytes": gzip_path.stat().st_size if gzip_path and gzip_path.exists() else 0,
        "integrity_check": integrity,
        "controlled_meta": meta,
        "table_counts": counts,
        "no_network": bool(no_network),
        "include_legacy": bool(include_legacy),
        **signatures,
    }
    payload["release_fingerprint"] = release_fingerprint(payload)
    return payload


def local_manifest_current(local_db: Path, local_manifest: Path, remote_manifest: dict[str, Any]) -> bool:
    if not local_db.exists() or not local_manifest.exists():
        return False
    try:
        current_manifest = json.loads(local_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    remote_fingerprint = remote_manifest.get("release_fingerprint")
    if remote_fingerprint:
        if current_manifest.get("release_fingerprint") != remote_fingerprint:
            return False
    elif current_manifest.get("sqlite_sha256") != remote_manifest.get("sqlite_sha256"):
        return False
    expected_sqlite = remote_manifest.get("sqlite_sha256")
    return not expected_sqlite or file_sha256(local_db) == expected_sqlite


def ensure_release_controlled(
    repo: str, tag: str, local_db: Path, local_manifest: Path
) -> dict[str, Any]:
    assets = release_assets(repo, tag)
    missing = [
        name for name in (CONTROLLED_ASSET, CONTROLLED_MANIFEST_ASSET) if name not in assets
    ]
    if missing:
        raise RuntimeError(
            f"Release {repo}@{tag} is missing controlled-access metadata assets: "
            + ", ".join(missing)
        )

    manifest_body, _headers = fetch_bytes(
        assets[CONTROLLED_MANIFEST_ASSET]["browser_download_url"]
    )
    remote_manifest = json.loads(manifest_body.decode("utf-8"))
    if local_manifest_current(local_db, local_manifest, remote_manifest):
        local_manifest.parent.mkdir(parents=True, exist_ok=True)
        local_manifest.write_bytes(manifest_body)
        return {"status": "unchanged", "manifest": remote_manifest}

    compressed, _headers = fetch_bytes(
        assets[CONTROLLED_ASSET]["browser_download_url"], timeout=600
    )
    expected_gzip = remote_manifest.get("gzip_sha256")
    actual_gzip = hashlib.sha256(compressed).hexdigest()
    if expected_gzip and actual_gzip != expected_gzip:
        raise RuntimeError("Downloaded controlled-access metadata gzip SHA-256 does not match manifest.")

    local_db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=str(local_db.parent)) as tmp:
        tmp_path = Path(tmp.name)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as gz:
            shutil.copyfileobj(gz, tmp)
    expected_sqlite = remote_manifest.get("sqlite_sha256")
    actual_sqlite = file_sha256(tmp_path)
    if expected_sqlite and actual_sqlite != expected_sqlite:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded controlled-access metadata SQLite SHA-256 does not match manifest.")
    tmp_path.replace(local_db)
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    local_manifest.write_bytes(manifest_body)
    return {"status": "downloaded", "manifest": remote_manifest}


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("No rows.")
        return
    widths = {
        column: max(len(column), *(len(str(row.get(column, ""))) for row in rows))
        for column in columns
    }
    print(" | ".join(column.ljust(widths[column]) for column in columns))
    print(" | ".join("-" * widths[column] for column in columns))
    for row in rows:
        print(" | ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))


def command_info(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    counts = table_counts(conn)
    meta = get_controlled_meta(conn)
    conn.close()
    manifest: dict[str, Any] = {}
    candidate_manifest = manifest_path(args.manifest)
    if candidate_manifest.exists():
        manifest = json.loads(candidate_manifest.read_text(encoding="utf-8"))
    payload = {
        "db": str(db_path(args.db)),
        "manifest": str(candidate_manifest),
        "table_counts": counts,
        "controlled_meta": meta,
        "release_fingerprint": manifest.get("release_fingerprint", ""),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"DB: {payload['db']}")
        print(f"Manifest: {payload['manifest']}")
        if payload["release_fingerprint"]:
            print(f"Release fingerprint: {payload['release_fingerprint']}")
        for key in (
            "controlled_downloads",
            "controlled_files",
            "manifest_rows",
            "metadata_rows",
            "wordpress_download_metadata",
            "idc_index",
        ):
            print(f"{key}: {counts.get(key, 0)}")
    return 0


def command_datasets(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rows = rows_as_dicts(
        conn,
        """
        SELECT route_system, dataset_type, short_title, controlled_file_rows,
               participant_ids, patient_ids, study_instance_uids,
               series_instance_uids, total_file_size_bytes, download_ids
        FROM controlled_dataset_summary
        ORDER BY route_system, lower(short_title)
        LIMIT ?
        """,
        (args.limit,),
    )
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(
            rows,
            [
                "route_system",
                "short_title",
                "controlled_file_rows",
                "participant_ids",
                "patient_ids",
                "series_instance_uids",
            ],
        )
    return 0


def command_downloads(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    where = ["1 = 1"]
    params: list[Any] = []
    if args.collection:
        where.append("short_title = ?")
        params.append(args.collection)
    if args.route_system:
        where.append("route_system = ?")
        params.append(args.route_system)
    params.append(args.limit)
    rows = rows_as_dicts(
        conn,
        f"""
        SELECT route_system, short_title, download_id, download_title,
               route_manifest_kind, metadata_artifact_kind, download_url
        FROM controlled_downloads
        WHERE {' AND '.join(where)}
        ORDER BY route_system, lower(short_title), download_id
        LIMIT ?
        """,
        tuple(params),
    )
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(
            rows,
            [
                "route_system",
                "short_title",
                "download_id",
                "download_title",
                "route_manifest_kind",
            ],
        )
    return 0


def command_files(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    where = ["1 = 1"]
    params: list[Any] = []
    if args.collection:
        where.append("short_title = ?")
        params.append(args.collection)
    if args.route_system:
        where.append("route_system = ?")
        params.append(args.route_system)
    if args.modality:
        where.append("upper(COALESCE(modality, image_modality, '')) = upper(?)")
        params.append(args.modality)
    params.append(args.limit)
    rows = rows_as_dicts(
        conn,
        f"""
        SELECT route_system, short_title, file_name, modality, participant_id,
               patient_id, series_instance_uid, drs_uri
        FROM controlled_files
        WHERE {' AND '.join(where)}
        ORDER BY route_system, lower(short_title), file_name, series_instance_uid
        LIMIT ?
        """,
        tuple(params),
    )
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(
            rows,
            [
                "route_system",
                "short_title",
                "file_name",
                "modality",
                "participant_id",
                "series_instance_uid",
            ],
        )
    return 0


def build(args: argparse.Namespace) -> dict[str, Any]:
    snapshot_path = Path(args.snapshot_db)
    if not snapshot_path.exists():
        raise RuntimeError(f"source snapshot not found: {snapshot_path}")
    out_path = Path(args.out)
    artifact_dir = Path(args.artifact_dir)
    if out_path.exists() and not args.replace:
        raise RuntimeError(f"output already exists: {out_path}; use --replace")
    if args.replace and out_path.exists():
        out_path.unlink()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(out_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(out_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("ATTACH DATABASE ? AS source", (str(snapshot_path),))
    require_source_views(conn)
    conn.executescript(SCHEMA_SQL)

    created_at = utc_now()
    downloads = seed_downloads(conn, include_legacy=args.include_legacy)
    wordpress_counts = seed_wordpress_download_metadata(conn)
    artifact_counts = ingest_artifacts(conn, artifact_dir, no_network=args.no_network)
    controlled_files = populate_controlled_files(conn)
    index_counts = populate_normalized_indexes(conn)
    exceptions = seed_exceptions(conn)

    counts = table_counts(conn)
    write_meta(conn, "created_at", created_at)
    write_meta(conn, "source_snapshot_db", str(snapshot_path.resolve()))
    write_meta(conn, "source_snapshot_meta", source_snapshot_meta(conn))
    write_meta(conn, "scope_note", "Visible current TCIA controlled-access WordPress downloads routed through General Commons or CTDC. Source rows come from public WordPress download manifests and download metadata spreadsheets only; no authenticated data download or GraphQL harvest is performed.")
    write_meta(conn, "controlled_access_policy_url", CONTROLLED_POLICY_URL)
    write_meta(conn, "table_counts", counts)
    conn.commit()
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    signatures = {
        "controlled_download_signature": table_signature(
            conn, "controlled_downloads", "download_row_id"
        ),
        "controlled_file_signature": table_signature(
            conn, "controlled_files", "controlled_file_id"
        ),
        "manifest_row_signature": table_signature(conn, "manifest_rows", "manifest_row_id"),
        "metadata_row_signature": table_signature(conn, "metadata_rows", "metadata_row_id"),
        "wordpress_metadata_signature": table_signature(
            conn, "wordpress_download_metadata", "metadata_id"
        ),
        "wordpress_url_signature": table_signature(
            conn, "wordpress_download_urls", "url_metadata_id"
        ),
    }
    conn.close()

    gzip_path = Path(args.gzip_out) if args.gzip_out else None
    if gzip_path:
        gzip_sqlite(out_path, gzip_path)

    manifest_payload = {
        "asset": CONTROLLED_ASSET,
        "sqlite_asset": out_path.name,
        "created_at": created_at,
        "schema_version": SCHEMA_VERSION,
        "sqlite_path": str(out_path.resolve()),
        "sqlite_sha256": file_sha256(out_path),
        "sqlite_bytes": out_path.stat().st_size,
        "gzip_path": str(gzip_path.resolve()) if gzip_path else "",
        "gzip_sha256": file_sha256(gzip_path) if gzip_path and gzip_path.exists() else "",
        "gzip_bytes": gzip_path.stat().st_size if gzip_path and gzip_path.exists() else 0,
        "source_snapshot_db": str(snapshot_path.resolve()),
        "artifact_dir": str(artifact_dir.resolve()),
        "integrity_check": integrity,
        "downloads_seeded": downloads,
        "wordpress_counts": wordpress_counts,
        "controlled_files_inserted": controlled_files,
        "artifact_counts": artifact_counts,
        "index_counts": index_counts,
        "exception_count": exceptions,
        "table_counts": counts,
        "no_network": bool(args.no_network),
        "include_legacy": bool(args.include_legacy),
        **signatures,
    }
    manifest_payload["release_fingerprint"] = release_fingerprint(manifest_payload)
    if args.manifest_out:
        write_manifest(Path(args.manifest_out), manifest_payload)
    return manifest_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure = subparsers.add_parser(
        "ensure", help="Download optional controlled-access SQLite release assets."
    )
    ensure.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository owner/name.")
    ensure.add_argument("--tag", default=DEFAULT_RELEASE_TAG, help="Release tag.")
    ensure.add_argument("--db", default=str(DEFAULT_OUT), help="Local SQLite output path.")
    ensure.add_argument(
        "--manifest-out", default=str(DEFAULT_MANIFEST), help="Local manifest output path."
    )

    info = subparsers.add_parser("info", help="Show local controlled-access metadata DB status.")
    info.add_argument("--db", default=str(DEFAULT_OUT), help="Local SQLite path.")
    info.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Local manifest path.")
    info.add_argument("--json", action="store_true", help="Emit JSON.")

    build_parser = subparsers.add_parser(
        "build", help="Build controlled-access metadata SQLite from a TCIA snapshot."
    )
    build_parser.add_argument("--snapshot-db", type=Path, default=DEFAULT_SNAPSHOT_DB)
    build_parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    build_parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    build_parser.add_argument("--manifest-out", type=Path, default=DEFAULT_MANIFEST)
    build_parser.add_argument("--gzip-out", type=Path, default=DEFAULT_GZIP)
    build_parser.add_argument("--replace", action="store_true")
    build_parser.add_argument(
        "--no-network", action="store_true", help="Use cached/local artifacts only."
    )
    build_parser.add_argument(
        "--include-legacy",
        action="store_true",
        help="Also ingest controlled legacy .tcia downloads with metadata spreadsheets.",
    )

    manifest = subparsers.add_parser(
        "manifest", help="Write a release manifest for a controlled-access DB."
    )
    manifest.add_argument("--db", required=True, help="SQLite path.")
    manifest.add_argument("--gzip", help="Gzipped SQLite path.")
    manifest.add_argument("--out", required=True, help="Manifest JSON output path.")

    validate = subparsers.add_parser(
        "validate", help="Validate a local controlled-access metadata SQLite DB."
    )
    validate.add_argument("--db", default=str(DEFAULT_OUT), help="SQLite path.")
    validate.add_argument("--json", action="store_true", help="Emit JSON.")

    datasets = subparsers.add_parser(
        "datasets", help="Summarize controlled-access metadata by dataset."
    )
    datasets.add_argument("--db", default=str(DEFAULT_OUT), help="SQLite path.")
    datasets.add_argument("--limit", type=int, default=100, help="Maximum rows.")
    datasets.add_argument("--json", action="store_true", help="Emit JSON.")

    downloads = subparsers.add_parser("downloads", help="List controlled download rows.")
    downloads.add_argument("--db", default=str(DEFAULT_OUT), help="SQLite path.")
    downloads.add_argument("--collection", help="Filter by TCIA short title.")
    downloads.add_argument(
        "--route-system", choices=["general_commons", "ctdc", "legacy_tcia_manifest"]
    )
    downloads.add_argument("--limit", type=int, default=50, help="Maximum rows.")
    downloads.add_argument("--json", action="store_true", help="Emit JSON.")

    files = subparsers.add_parser("files", help="List normalized controlled file rows.")
    files.add_argument("--db", default=str(DEFAULT_OUT), help="SQLite path.")
    files.add_argument("--collection", help="Filter by TCIA short title.")
    files.add_argument(
        "--route-system", choices=["general_commons", "ctdc", "legacy_tcia_manifest"]
    )
    files.add_argument("--modality", help="Filter by DICOM modality, such as CT, MR, or PT.")
    files.add_argument("--limit", type=int, default=50, help="Maximum rows.")
    files.add_argument("--json", action="store_true", help="Emit JSON.")

    args = parser.parse_args(argv)

    if args.command == "ensure":
        result = ensure_release_controlled(
            args.repo, args.tag, Path(args.db), Path(args.manifest_out)
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "info":
        return command_info(args)
    if args.command == "build":
        summary = build(args)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    if args.command == "manifest":
        payload = build_manifest_from_db(
            Path(args.db),
            Path(args.gzip) if args.gzip else None,
            include_legacy=False,
            no_network=False,
        )
        write_manifest(Path(args.out), payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.command == "validate":
        payload = validate_db(Path(args.db))
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"integrity_check: {payload['integrity_check']}")
            print(f"missing_tables: {', '.join(payload['missing_tables']) or 'none'}")
        return 0 if payload["integrity_check"] == "ok" and not payload["missing_tables"] else 1
    if args.command == "datasets":
        return command_datasets(args)
    if args.command == "downloads":
        return command_downloads(args)
    if args.command == "files":
        return command_files(args)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, sqlite3.Error, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
