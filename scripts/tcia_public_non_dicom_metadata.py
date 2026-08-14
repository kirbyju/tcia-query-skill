#!/usr/bin/env python3
"""Build and query the TCIA public non-DICOM imaging metadata sidecar.

The builder preserves logical assets separately from their delivery/viewing
locations. It treats PathDB, Aspera, WordPress attachments, and AWS Open Data
as managed systems, not as mutually exclusive data categories.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from tcia_public_non_dicom_crosswalk_discovery import (
    build_candidate_index,
    match_path,
    wordpress_evidence,
)

from tcia_artifact_model import (
    CONTAINER_FORMATS,
    MANAGED_SYSTEMS,
    NON_DICOM_IMAGING_FORMATS,
    REPRESENTATION_PROVENANCE_CLASSES,
    SCHEMA_VERSION,
    SYSTEM_FUNCTIONS,
    default_representation_class,
    default_system_functions,
    format_from_path,
    imaging_domain,
    json_dumps,
    managed_system_for_url,
    media_kind,
    normalize_format,
    object_role,
    stable_id,
)


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_DB = SKILL_ROOT / "cache" / "tcia_snapshot.sqlite"
DEFAULT_NIFTI_DB = SKILL_ROOT / "cache" / "nifti_metadata.sqlite"
DEFAULT_PATHOLOGY_DB = SKILL_ROOT / "cache" / "pathology_metadata.sqlite"
DEFAULT_CROSSWALK_CSV = SKILL_ROOT / "references" / "public_non_dicom_crosswalks_v1.csv"
DEFAULT_CROSSWALK_CURATION = SKILL_ROOT / "references" / "public-non-dicom-crosswalk-curation-v1.json"
DEFAULT_DB = SKILL_ROOT / "cache" / "public_non_dicom_metadata.sqlite"
DEFAULT_MANIFEST = SKILL_ROOT / "cache" / "public_non_dicom_metadata_manifest.json"
DEFAULT_RELEASE_TAG = "tcia-metadata-v2-preview"
DEFAULT_REPOSITORY = "kirbyju/tcia-query-skill"
DB_ASSET = "public_non_dicom_metadata.sqlite.gz"
MANIFEST_ASSET = "public_non_dicom_metadata_manifest.json"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE artifact_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE managed_systems (
    managed_system TEXT PRIMARY KEY,
    manager_group TEXT NOT NULL,
    display_name TEXT NOT NULL
);

CREATE TABLE system_functions (
    system_function TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

CREATE TABLE public_non_dicom_assets (
    asset_id TEXT PRIMARY KEY,
    dataset_type TEXT NOT NULL,
    short_title TEXT NOT NULL,
    download_row_id INTEGER,
    download_id TEXT,
    subject_id TEXT,
    subject_id_namespace TEXT,
    participant_link_status TEXT NOT NULL DEFAULT 'unavailable',
    asset_granularity TEXT NOT NULL,
    asset_name TEXT,
    file_name TEXT,
    package_path TEXT,
    file_format TEXT NOT NULL,
    container_format TEXT,
    media_kind TEXT NOT NULL,
    spatial_dimensionality TEXT NOT NULL DEFAULT 'unknown',
    temporal_dimensionality TEXT NOT NULL DEFAULT 'unknown',
    imaging_domain TEXT NOT NULL,
    modality TEXT,
    object_role TEXT NOT NULL,
    size_bytes INTEGER,
    checksum TEXT,
    checksum_algorithm TEXT,
    representation_provenance_class TEXT NOT NULL,
    source_system TEXT NOT NULL,
    source_record_id TEXT,
    source_url TEXT,
    raw_values_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    quality_flag_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_system) REFERENCES managed_systems(managed_system)
);

CREATE TABLE public_non_dicom_locations (
    location_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    managed_system TEXT NOT NULL,
    system_functions_json TEXT NOT NULL,
    access_url TEXT,
    viewer_url TEXT,
    manifest_url TEXT,
    bucket TEXT,
    object_key TEXT,
    access_level TEXT NOT NULL DEFAULT 'open',
    availability_status TEXT NOT NULL DEFAULT 'observed',
    representation_provenance_class TEXT NOT NULL,
    equivalence_status TEXT NOT NULL DEFAULT 'unresolved',
    checksum TEXT,
    checksum_algorithm TEXT,
    observed_at TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (asset_id) REFERENCES public_non_dicom_assets(asset_id),
    FOREIGN KEY (managed_system) REFERENCES managed_systems(managed_system)
);

CREATE TABLE public_non_dicom_asset_relationships (
    relationship_id TEXT PRIMARY KEY,
    source_asset_id TEXT NOT NULL,
    target_asset_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    evidence_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    FOREIGN KEY (source_asset_id) REFERENCES public_non_dicom_assets(asset_id),
    FOREIGN KEY (target_asset_id) REFERENCES public_non_dicom_assets(asset_id)
);

CREATE TABLE public_non_dicom_asset_participants (
    asset_participant_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    short_title TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_id_namespace TEXT NOT NULL,
    raw_subject_id TEXT NOT NULL,
    participant_role TEXT NOT NULL DEFAULT 'depicted_subject',
    link_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (asset_id) REFERENCES public_non_dicom_assets(asset_id)
);

CREATE TABLE public_non_dicom_crosswalk_decisions (
    decision_id TEXT PRIMARY KEY,
    dataset_type TEXT NOT NULL,
    short_title TEXT NOT NULL,
    download_ids_json TEXT NOT NULL DEFAULT '[]',
    decision_status TEXT NOT NULL,
    resolution_type TEXT NOT NULL,
    reviewer_note TEXT NOT NULL DEFAULT '',
    evidence_url TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE public_non_dicom_crosswalk_evidence (
    crosswalk_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    short_title TEXT NOT NULL,
    raw_subject_id TEXT NOT NULL DEFAULT '',
    resolved_subject_id TEXT NOT NULL,
    mapping_method TEXT NOT NULL,
    confidence TEXT NOT NULL,
    evidence_url TEXT NOT NULL DEFAULT '',
    reviewer_note TEXT NOT NULL DEFAULT '',
    dicom_series_instance_uid TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (asset_id) REFERENCES public_non_dicom_assets(asset_id)
);

CREATE TABLE public_non_dicom_review_issues (
    issue_id TEXT PRIMARY KEY,
    short_title TEXT NOT NULL,
    issue_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    affected_assets INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_pnd_assets_dataset ON public_non_dicom_assets(short_title, dataset_type);
CREATE INDEX idx_pnd_assets_subject ON public_non_dicom_assets(short_title, subject_id);
CREATE INDEX idx_pnd_assets_format ON public_non_dicom_assets(file_format, media_kind);
CREATE INDEX idx_pnd_locations_asset ON public_non_dicom_locations(asset_id);
CREATE INDEX idx_pnd_locations_system ON public_non_dicom_locations(managed_system);
CREATE INDEX idx_pnd_asset_participants_asset ON public_non_dicom_asset_participants(asset_id);
CREATE INDEX idx_pnd_asset_participants_subject ON public_non_dicom_asset_participants(short_title, subject_id);
CREATE UNIQUE INDEX idx_pnd_asset_participants_unique
    ON public_non_dicom_asset_participants(asset_id, subject_id_namespace, subject_id);
CREATE INDEX idx_pnd_crosswalk_asset ON public_non_dicom_crosswalk_evidence(asset_id);
CREATE INDEX idx_pnd_crosswalk_subject ON public_non_dicom_crosswalk_evidence(short_title, resolved_subject_id);

CREATE VIEW agent_public_non_dicom_assets AS
SELECT
    a.*,
    (SELECT COUNT(*) FROM public_non_dicom_asset_participants ap WHERE ap.asset_id = a.asset_id)
      AS participant_link_count,
    (SELECT COUNT(*) FROM public_non_dicom_locations l WHERE l.asset_id = a.asset_id)
      AS location_count,
    (SELECT group_concat(DISTINCT l.managed_system)
       FROM public_non_dicom_locations l WHERE l.asset_id = a.asset_id)
      AS managed_systems
FROM public_non_dicom_assets a;

CREATE VIEW agent_public_non_dicom_asset_participants AS
SELECT
    ap.*,
    a.dataset_type,
    a.asset_granularity,
    a.file_name,
    a.package_path,
    a.file_format,
    a.media_kind,
    a.imaging_domain,
    a.modality,
    a.object_role,
    a.source_system,
    a.source_url
FROM public_non_dicom_asset_participants ap
JOIN public_non_dicom_assets a USING (asset_id);

CREATE VIEW agent_public_non_dicom_locations AS
SELECT
    l.*,
    a.dataset_type,
    a.short_title,
    a.subject_id,
    a.file_format,
    a.media_kind,
    a.imaging_domain,
    a.object_role,
    a.asset_name,
    a.file_name
FROM public_non_dicom_locations l
JOIN public_non_dicom_assets a USING (asset_id);

CREATE VIEW agent_public_non_dicom_dataset_summary AS
SELECT
    a.dataset_type,
    a.short_title,
    COUNT(*) AS asset_rows,
    SUM(CASE WHEN asset_granularity = 'file' THEN 1 ELSE 0 END) AS file_assets,
    SUM(CASE WHEN asset_granularity = 'download' THEN 1 ELSE 0 END) AS download_assets,
    (SELECT COUNT(DISTINCT ap.subject_id)
       FROM public_non_dicom_asset_participants ap
       JOIN public_non_dicom_assets a2 USING (asset_id)
      WHERE a2.dataset_type = a.dataset_type
        AND ap.short_title = a.short_title) AS participant_ids,
    group_concat(DISTINCT file_format) AS file_formats,
    group_concat(DISTINCT media_kind) AS media_kinds,
    group_concat(DISTINCT imaging_domain) AS imaging_domains,
    group_concat(DISTINCT modality) AS modalities,
    SUM(COALESCE(size_bytes, 0)) AS known_size_bytes
FROM public_non_dicom_assets a
GROUP BY a.dataset_type, a.short_title;

CREATE VIEW agent_public_non_dicom_participant_summary AS
SELECT
    a.dataset_type,
    a.short_title,
    ap.subject_id,
    ap.subject_id_namespace,
    ap.link_status AS participant_link_status,
    COUNT(*) AS asset_rows,
    SUM(CASE WHEN a.asset_granularity = 'file' THEN 1 ELSE 0 END) AS file_assets,
    group_concat(DISTINCT a.file_format) AS file_formats,
    group_concat(DISTINCT a.media_kind) AS media_kinds,
    group_concat(DISTINCT a.imaging_domain) AS imaging_domains,
    group_concat(DISTINCT a.modality) AS modalities,
    group_concat(DISTINCT a.object_role) AS object_roles,
    SUM(COALESCE(a.size_bytes, 0)) AS known_size_bytes
FROM public_non_dicom_asset_participants ap
JOIN public_non_dicom_assets a USING (asset_id)
GROUP BY a.dataset_type, a.short_title, ap.subject_id, ap.subject_id_namespace, ap.link_status;

CREATE VIEW agent_public_non_dicom_review_issues AS
SELECT * FROM public_non_dicom_review_issues;

CREATE VIEW agent_public_non_dicom_crosswalk_decisions AS
SELECT * FROM public_non_dicom_crosswalk_decisions;

CREATE VIEW agent_public_non_dicom_crosswalk_evidence AS
SELECT
    e.*,
    a.dataset_type,
    a.download_id,
    a.package_path,
    a.file_name,
    a.file_format,
    a.source_system,
    a.source_url
FROM public_non_dicom_crosswalk_evidence e
JOIN public_non_dicom_assets a USING (asset_id);
"""


SYSTEM_DISPLAY = {
    "tcia_wordpress": ("TCIA", "TCIA WordPress"),
    "tcia_aspera": ("TCIA", "TCIA Aspera"),
    "tcia_pathdb": ("TCIA", "TCIA PathDB"),
    "aws_open_data": ("AWS", "AWS Open Data"),
    "crdc_idc": ("NCI CRDC", "Imaging Data Commons"),
    "crdc_gc": ("NCI CRDC", "General Commons"),
    "crdc_ctdc": ("NCI CRDC", "Clinical and Translational Data Commons"),
}

FUNCTION_DESCRIPTIONS = {
    "publication_catalog": "Publishes TCIA dataset identity, provenance, and user-facing metadata.",
    "discovery_index": "Provides normalized metadata for searching or filtering.",
    "distribution_endpoint": "Provides or routes retrieval of data objects.",
    "viewer": "Provides an interactive data viewer.",
    "controlled_access_broker": "Routes authorization-aware access to controlled data.",
}


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def parse_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        loaded = json.loads(text)
        if isinstance(loaded, list):
            return [str(item).strip() for item in loaded if str(item).strip()]
    except json.JSONDecodeError:
        pass
    separator = ";" if ";" in text else ","
    return [item.strip() for item in text.split(separator) if item.strip()]


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone() is not None


def columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")}


def source_meta(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "bytes": path.stat().st_size,
        "sha256": file_sha256(path),
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def insert_vocab(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO managed_systems VALUES (?, ?, ?)",
        [(name, *SYSTEM_DISPLAY[name]) for name in sorted(MANAGED_SYSTEMS)],
    )
    conn.executemany(
        "INSERT INTO system_functions VALUES (?, ?)",
        [(name, FUNCTION_DESCRIPTIONS[name]) for name in sorted(SYSTEM_FUNCTIONS)],
    )


def insert_asset(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    names = [row[1] for row in conn.execute("PRAGMA table_info(public_non_dicom_assets)")]
    conn.execute(
        f"INSERT OR IGNORE INTO public_non_dicom_assets ({', '.join(names)}) "
        f"VALUES ({', '.join('?' for _ in names)})",
        [values.get(name) for name in names],
    )


def insert_location(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    names = [row[1] for row in conn.execute("PRAGMA table_info(public_non_dicom_locations)")]
    conn.execute(
        f"INSERT OR IGNORE INTO public_non_dicom_locations ({', '.join(names)}) "
        f"VALUES ({', '.join('?' for _ in names)})",
        [values.get(name) for name in names],
    )


def insert_asset_participant(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    short_title: str,
    subject_id: str,
    namespace: str,
    raw_subject_id: str,
    participant_role: str,
    link_status: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO public_non_dicom_asset_participants
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stable_id("asset_participant", asset_id, namespace, subject_id),
            asset_id,
            short_title,
            subject_id,
            namespace,
            raw_subject_id,
            participant_role,
            link_status,
            json_dumps(evidence or {}),
        ),
    )


def sync_scalar_asset_participants(conn: sqlite3.Connection) -> int:
    """Project ordinary one-subject asset fields into the canonical junction."""
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO public_non_dicom_asset_participants
        SELECT
            'asset_participant_scalar_' || asset_id,
            asset_id,
            short_title,
            subject_id,
            COALESCE(NULLIF(subject_id_namespace, ''), 'tcia_dataset:' || short_title),
            subject_id,
            'depicted_subject',
            participant_link_status,
            json_object('projection', 'scalar_asset_subject', 'source_system', source_system)
        FROM public_non_dicom_assets
        WHERE COALESCE(trim(subject_id), '') <> ''
        """
    )
    return conn.total_changes - before


def hancock_subject_id(raw_subject_id: str) -> str:
    """Resolve HANCOCK's numeric PathDB IDs to its patientNNN identifier form."""
    value = raw_subject_id.strip()
    if value.isdigit():
        return f"patient{int(value):03d}"
    return value


def location_values(
    asset_id: str,
    url: str,
    *,
    viewer_url: str = "",
    route_system: str = "",
    representation_class: str = "",
    checksum: str = "",
    checksum_algorithm: str = "",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    system = managed_system_for_url(url or viewer_url, route_system=route_system)
    representation = representation_class or default_representation_class(system)
    return {
        "location_id": stable_id("loc", asset_id, system, url, viewer_url),
        "asset_id": asset_id,
        "managed_system": system,
        "system_functions_json": json_dumps(default_system_functions(system, viewer_url=viewer_url)),
        "access_url": url,
        "viewer_url": viewer_url,
        "manifest_url": "",
        "bucket": "",
        "object_key": "",
        "access_level": "open",
        "availability_status": "observed",
        "representation_provenance_class": representation,
        "equivalence_status": "byte_identical" if checksum else "unresolved",
        "checksum": checksum,
        "checksum_algorithm": checksum_algorithm,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "provenance_json": json_dumps(provenance or {}),
    }


def download_size_bytes(value: Any, unit: Any) -> int | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    multiplier = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
    }.get(str(unit or "").strip().casefold())
    return int(number * multiplier) if multiplier else None


def ingest_wordpress(conn: sqlite3.Connection, snapshot_db: Path) -> int:
    count = 0
    with connect(snapshot_db) as source:
        for row in source.execute(
            """
            SELECT * FROM agent_current_downloads
            WHERE hidden = 0 AND controlled_access = 0
            ORDER BY lower(short_title), download_row_id
            """
        ):
            file_types = [normalize_format(value) for value in parse_list(row["file_types"])]
            imaging_formats = sorted(set(file_types) & NON_DICOM_IMAGING_FORMATS)
            if not imaging_formats:
                continue
            download_types = parse_list(row["download_types"])
            data_types = parse_list(row["data_types"])
            containers = sorted(set(file_types) & CONTAINER_FORMATS)
            for file_format in imaging_formats:
                asset_id = stable_id(
                    "asset", "wordpress_download", row["dataset_type"], row["short_title"],
                    row["download_row_id"], file_format,
                )
                system = managed_system_for_url(row["download_url"])
                representation = default_representation_class(system)
                insert_asset(
                    conn,
                    {
                        "asset_id": asset_id,
                        "dataset_type": row["dataset_type"],
                        "short_title": row["short_title"],
                        "download_row_id": row["download_row_id"],
                        "download_id": row["download_id"],
                        "subject_id": "",
                        "subject_id_namespace": "",
                        "participant_link_status": "dataset_only",
                        "asset_granularity": "download",
                        "asset_name": row["download_title"] or row["title"],
                        "file_name": "",
                        "package_path": "",
                        "file_format": file_format,
                        "container_format": ";".join(containers),
                        "media_kind": media_kind(file_format, data_types),
                        "spatial_dimensionality": "unknown",
                        "temporal_dimensionality": "video" if media_kind(file_format, data_types) == "video" else "unknown",
                        "imaging_domain": imaging_domain(download_types, data_types),
                        "modality": ";".join(
                            value for value in data_types if value.upper() in {"CT", "MR", "MG", "DX", "CR", "US", "PET", "NM"}
                        ),
                        "object_role": object_role(download_types, data_types, row["download_title"]),
                        "size_bytes": download_size_bytes(row["download_size"], row["download_size_unit"]),
                        "checksum": "",
                        "checksum_algorithm": "",
                        "representation_provenance_class": representation,
                        "source_system": system,
                        "source_record_id": str(row["download_row_id"]),
                        "source_url": row["download_url"],
                        "raw_values_json": json_dumps({
                            "download_types": download_types,
                            "data_types": data_types,
                            "file_types": parse_list(row["file_types"]),
                        }),
                        "provenance_json": json_dumps({
                            "source_table": "agent_current_downloads",
                            "download_row_id": row["download_row_id"],
                            "classification": "current visible public non-DICOM imaging download",
                        }),
                        "quality_flag_json": json_dumps({"participant_inventory": "not_available_at_download_grain"}),
                    },
                )
                insert_location(
                    conn,
                    location_values(
                        asset_id,
                        row["download_url"] or "",
                        representation_class=representation,
                        provenance={"source_table": "agent_current_downloads"},
                    ),
                )
                count += 1
    return count


def ingest_nifti(conn: sqlite3.Connection, nifti_db: Path) -> int:
    if not nifti_db.exists():
        return 0
    count = 0
    with connect(nifti_db) as source:
        if not table_exists(source, "agent_nifti_files"):
            return 0
        downloads: dict[tuple[str, str], str] = {}
        if table_exists(source, "agent_nifti_downloads"):
            for row in source.execute("SELECT short_title, download_id, download_url FROM agent_nifti_downloads"):
                downloads[(str(row["short_title"]), str(row["download_id"] or ""))] = str(row["download_url"] or "")
        for row in source.execute("SELECT * FROM agent_nifti_files ORDER BY short_title, package_path"):
            package_path = str(row["package_path"] or row["file_name"] or "")
            file_format = format_from_path(package_path) or "NIFTI"
            download_id = str(row["download_id"] or "")
            url = downloads.get((str(row["short_title"]), download_id), "")
            system = managed_system_for_url(url)
            asset_id = stable_id("asset", "nifti", row["short_title"], row["radiology_id"])
            derived = bool(row["is_derived_object"])
            role = "segmentation" if derived else "source_image"
            insert_asset(
                conn,
                {
                    "asset_id": asset_id,
                    "dataset_type": row["dataset_type"] or "Collection",
                    "short_title": row["short_title"],
                    "download_row_id": None,
                    "download_id": download_id,
                    "subject_id": row["subject_id"] or "",
                    "subject_id_namespace": f"tcia_dataset:{row['short_title']}",
                    "participant_link_status": "dataset_scoped_source_identifier" if row["subject_id"] else "unavailable",
                    "asset_granularity": "file",
                    "asset_name": row["file_name"],
                    "file_name": row["file_name"],
                    "package_path": package_path,
                    "file_format": file_format,
                    "container_format": "",
                    "media_kind": "image_volume",
                    "spatial_dimensionality": "3D" if row["number_of_slices"] else "unknown",
                    "temporal_dimensionality": "time_series" if row["number_of_temporal_positions"] else "static",
                    "imaging_domain": "radiology",
                    "modality": row["modality"] or "",
                    "object_role": role,
                    "size_bytes": None,
                    "checksum": "",
                    "checksum_algorithm": "",
                    "representation_provenance_class": default_representation_class(system),
                    "source_system": system,
                    "source_record_id": row["radiology_id"],
                    "source_url": url,
                    "raw_values_json": json_dumps({"object_type": row["object_type"], "image_type": row["image_type"]}),
                    "provenance_json": json_dumps({"source_artifact": "nifti_metadata", "source_view": "agent_nifti_files"}),
                    "quality_flag_json": row["quality_flag_json"] or "{}",
                },
            )
            insert_location(conn, location_values(asset_id, url, provenance={"source_artifact": "nifti_metadata"}))
            count += 1
    return count


def ingest_pathology_packages(conn: sqlite3.Connection, pathology_db: Path) -> int:
    if not pathology_db.exists():
        return 0
    count = 0
    with connect(pathology_db) as source:
        if not table_exists(source, "agent_pathology_file_objects"):
            return 0
        urls: dict[tuple[str, str], str] = {}
        if table_exists(source, "agent_pathology_downloads"):
            for row in source.execute("SELECT short_title, download_id, download_url FROM agent_pathology_downloads"):
                urls[(str(row["short_title"]), str(row["download_id"] or ""))] = str(row["download_url"] or "")
        for row in source.execute(
            "SELECT * FROM agent_pathology_file_objects WHERE COALESCE(is_metadata, 0) = 0 ORDER BY short_title, package_path"
        ):
            file_format = normalize_format(row["image_format"] or row["file_ext"])
            if file_format not in NON_DICOM_IMAGING_FORMATS:
                continue
            url = urls.get((str(row["short_title"]), str(row["download_id"] or "")), "")
            system = managed_system_for_url(url)
            asset_id = stable_id("asset", "pathology_package", row["non_dicom_file_id"])
            insert_asset(
                conn,
                {
                    "asset_id": asset_id,
                    "dataset_type": row["dataset_type"] or "Collection",
                    "short_title": row["short_title"],
                    "download_row_id": row["download_row_id"],
                    "download_id": row["download_id"],
                    "subject_id": "",
                    "subject_id_namespace": "",
                    "participant_link_status": "unavailable",
                    "asset_granularity": "file",
                    "asset_name": row["file_name"],
                    "file_name": row["file_name"],
                    "package_path": row["package_path"],
                    "file_format": file_format,
                    "container_format": "",
                    "media_kind": "whole_slide_image" if row["is_wsi"] else "still_image",
                    "spatial_dimensionality": "2D",
                    "temporal_dimensionality": "static",
                    "imaging_domain": "pathology",
                    "modality": row["object_modality"] or "",
                    "object_role": row["file_role"] or "source_image",
                    "size_bytes": row["bytes"],
                    "checksum": row["checksum"] or "",
                    "checksum_algorithm": row["checksum_algorithm"] or "",
                    "representation_provenance_class": default_representation_class(system),
                    "source_system": system,
                    "source_record_id": row["non_dicom_file_id"],
                    "source_url": url,
                    "raw_values_json": json_dumps({"source_table": row["source_table"], "source_row_id": row["source_row_id"]}),
                    "provenance_json": json_dumps({"source_artifact": "pathology_metadata", "source_view": "agent_pathology_file_objects"}),
                    "quality_flag_json": "{}",
                },
            )
            insert_location(
                conn,
                location_values(
                    asset_id,
                    url,
                    checksum=row["checksum"] or "",
                    checksum_algorithm=row["checksum_algorithm"] or "",
                    provenance={"source_artifact": "pathology_metadata"},
                ),
            )
            count += 1
    return count


def ingest_pathdb(conn: sqlite3.Connection, snapshot_db: Path, include_files: bool) -> int:
    if not include_files:
        return 0
    count = 0
    with connect(snapshot_db) as source:
        if not table_exists(source, "agent_pathdb_slides"):
            return 0
        for row in source.execute("SELECT * FROM agent_pathdb_slides ORDER BY collection, patient_id, slide_id"):
            url = str(row["wsiimage_url"] or "")
            viewer_url = str(row["camicroscope_url"] or "")
            file_format = normalize_format(row["data_format"] or format_from_path(url))
            if not file_format:
                file_format = "UNKNOWN"
            asset_id = stable_id("asset", "pathdb", row["collection"], row["slide_id"], row["camic_id"])
            is_hancock_tma_block = (
                row["collection"] == "HANCOCK"
                and re.match(r"^(?:InvasionFront|TumorCenter)_.+_block\d+$", str(row["slide_id"] or ""))
            )
            raw_subject_ids = (
                re.findall(r"\d+", str(row["patient_id"] or ""))
                if is_hancock_tma_block
                else parse_list(row["patient_id"])
            )
            resolved_subject_ids = [
                hancock_subject_id(item) if row["collection"] == "HANCOCK" else item
                for item in raw_subject_ids
            ]
            scalar_subject_id = resolved_subject_ids[0] if len(resolved_subject_ids) == 1 else ""
            if len(resolved_subject_ids) > 1:
                link_status = "multi_participant_source_list"
            elif resolved_subject_ids:
                link_status = "dataset_scoped_source_identifier"
            else:
                link_status = "unavailable"
            insert_asset(
                conn,
                {
                    "asset_id": asset_id,
                    "dataset_type": "Collection",
                    "short_title": row["collection"],
                    "download_row_id": None,
                    "download_id": "",
                    "subject_id": scalar_subject_id,
                    "subject_id_namespace": f"tcia_dataset:{row['collection']}",
                    "participant_link_status": link_status,
                    "asset_granularity": "file",
                    "asset_name": row["slide_id"],
                    "file_name": row["slide_id"],
                    "package_path": "",
                    "file_format": file_format,
                    "container_format": "",
                    "media_kind": "whole_slide_image",
                    "spatial_dimensionality": "2D",
                    "temporal_dimensionality": "static",
                    "imaging_domain": "pathology",
                    "modality": row["modality"] or "SM",
                    "object_role": "source_image",
                    "size_bytes": None,
                    "checksum": "",
                    "checksum_algorithm": "",
                    "representation_provenance_class": "unknown",
                    "source_system": "tcia_pathdb",
                    "source_record_id": row["slide_id"],
                    "source_url": url,
                    "raw_values_json": json_dumps({
                        "protocol": row["protocol"], "magnification": row["magnification"],
                        "camic_id": row["camic_id"], "raw_patient_id": row["patient_id"] or "",
                    }),
                    "provenance_json": json_dumps({"source_artifact": "tcia_snapshot", "source_view": "agent_pathdb_slides"}),
                    "quality_flag_json": json_dumps({"equivalence_to_submitter_package": "unresolved"}),
                },
            )
            for raw_subject_id, resolved_subject_id in zip(raw_subject_ids, resolved_subject_ids):
                insert_asset_participant(
                    conn,
                    asset_id=asset_id,
                    short_title=row["collection"],
                    subject_id=resolved_subject_id,
                    namespace=f"tcia_dataset:{row['collection']}",
                    raw_subject_id=raw_subject_id,
                    participant_role=("tma_block_member" if is_hancock_tma_block else "depicted_subject"),
                    link_status=link_status,
                    evidence={
                        "source_artifact": "tcia_snapshot",
                        "source_view": "agent_pathdb_slides",
                        "mapping_method": (
                            "hancock_numeric_to_patient_token"
                            if row["collection"] == "HANCOCK" and raw_subject_id != resolved_subject_id
                            else "source_identifier"
                        ),
                    },
                )
            insert_location(
                conn,
                location_values(
                    asset_id,
                    url,
                    viewer_url=viewer_url,
                    representation_class="unknown",
                    provenance={"source_artifact": "tcia_snapshot", "source_view": "agent_pathdb_slides"},
                ),
            )
            count += 1
            if count % 10000 == 0:
                conn.commit()
    return count


def ingest_reviewed_crosswalks(
    conn: sqlite3.Connection,
    crosswalk_csv: Path,
    curation_path: Path,
) -> dict[str, int]:
    if not crosswalk_csv.exists() or not curation_path.exists():
        return {"decisions": 0, "file_assets": 0, "evidence_rows": 0}

    curation = json.loads(curation_path.read_text())
    reviewed_at = str(curation.get("reviewed_at") or "")
    decisions = curation.get("decisions") or []
    for decision in decisions:
        download_ids = [str(value) for value in decision.get("download_ids") or []]
        decision_id = stable_id(
            "crosswalk_decision", decision["dataset_type"], decision["short_title"],
            decision.get("resolution_type"), *download_ids,
        )
        conn.execute(
            """
            INSERT INTO public_non_dicom_crosswalk_decisions
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                decision["dataset_type"],
                decision["short_title"],
                json_dumps(download_ids),
                decision["decision_status"],
                decision["resolution_type"],
                decision.get("reviewer_note") or "",
                decision.get("evidence_url") or "",
                reviewed_at,
                json_dumps({"source_file": str(curation_path), "review_source": curation.get("review_source")}),
            ),
        )
        if decision["resolution_type"] == "source_confirmed_unavailable":
            placeholders = ",".join("?" for _ in download_ids)
            clause = f"AND COALESCE(download_id, '') IN ({placeholders})" if download_ids else ""
            conn.execute(
                f"""
                UPDATE public_non_dicom_assets
                SET participant_link_status = 'source_confirmed_unavailable',
                    quality_flag_json = json_set(
                        COALESCE(NULLIF(quality_flag_json, ''), '{{}}'),
                        '$.participant_inventory', 'source_confirmed_unavailable',
                        '$.crosswalk_decision_id', ?
                    )
                WHERE dataset_type = ? AND short_title = ? {clause}
                """,
                (decision_id, decision["dataset_type"], decision["short_title"], *download_ids),
            )

    download_lookup: dict[tuple[str, str, str, str], sqlite3.Row] = {}
    for item in conn.execute(
        """
        SELECT * FROM public_non_dicom_assets
        WHERE asset_granularity = 'download'
        ORDER BY short_title, download_id, file_format
        """
    ):
        download_lookup[(item["dataset_type"], item["short_title"], str(item["download_id"] or ""), item["file_format"])] = item

    assets = 0
    evidence_rows = 0
    with crosswalk_csv.open(newline="") as handle:
        for source_row in csv.DictReader(handle):
            key = (
                source_row["dataset_type"], source_row["short_title"],
                str(source_row["download_id"] or ""), source_row["file_format"],
            )
            download = download_lookup.get(key)
            if download is None:
                raise RuntimeError(f"Reviewed crosswalk has no matching public download asset: {key}")
            source_url = str(download["source_url"] or source_row["source_url"] or "")
            source_system = str(download["source_system"] or source_row["source_system"] or "tcia_wordpress")
            asset_id = stable_id(
                "asset", "reviewed_crosswalk", source_row["dataset_type"], source_row["short_title"],
                source_row["download_id"], source_row["package_path"], source_row["subject_id"],
            )
            crosswalk_id = stable_id("crosswalk", asset_id, source_row["crosswalk_method"], reviewed_at)
            size_bytes = int(source_row["size_bytes"]) if str(source_row["size_bytes"] or "").isdigit() else None
            insert_asset(
                conn,
                {
                    "asset_id": asset_id,
                    "dataset_type": source_row["dataset_type"],
                    "short_title": source_row["short_title"],
                    "download_row_id": download["download_row_id"],
                    "download_id": source_row["download_id"],
                    "subject_id": source_row["subject_id"],
                    "subject_id_namespace": source_row["subject_id_namespace"],
                    "participant_link_status": source_row["participant_link_status"],
                    "asset_granularity": "file",
                    "asset_name": source_row["file_name"],
                    "file_name": source_row["file_name"],
                    "package_path": source_row["package_path"],
                    "file_format": source_row["file_format"],
                    "container_format": download["container_format"],
                    "media_kind": source_row["media_kind"],
                    "spatial_dimensionality": "3D" if source_row["media_kind"] == "image_volume" else "2D",
                    "temporal_dimensionality": "video" if source_row["media_kind"] == "video" else "static",
                    "imaging_domain": source_row["imaging_domain"],
                    "modality": source_row["modality"],
                    "object_role": source_row["object_role"],
                    "size_bytes": size_bytes,
                    "checksum": "",
                    "checksum_algorithm": "",
                    "representation_provenance_class": download["representation_provenance_class"],
                    "source_system": source_system,
                    "source_record_id": crosswalk_id,
                    "source_url": source_url,
                    "raw_values_json": source_row["raw_values_json"] or "{}",
                    "provenance_json": json_dumps({
                        "source_artifact": "public_non_dicom_crosswalks_v1",
                        "crosswalk_id": crosswalk_id,
                        "crosswalk_source_url": source_row["crosswalk_source_url"],
                        "mapping_method": source_row["crosswalk_method"],
                        "source_provenance": json.loads(source_row["provenance_json"] or "{}"),
                    }),
                    "quality_flag_json": source_row["quality_flag_json"] or "{}",
                },
            )
            insert_location(
                conn,
                location_values(
                    asset_id,
                    source_url,
                    representation_class=download["representation_provenance_class"],
                    provenance={"source_artifact": "public_non_dicom_crosswalks_v1", "crosswalk_id": crosswalk_id},
                ),
            )
            conn.execute(
                """
                INSERT INTO public_non_dicom_crosswalk_evidence
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    crosswalk_id,
                    asset_id,
                    source_row["short_title"],
                    source_row["raw_subject_id"],
                    source_row["subject_id"],
                    source_row["crosswalk_method"],
                    source_row["crosswalk_confidence"],
                    source_row["crosswalk_source_url"],
                    source_row["reviewer_note"],
                    source_row["dicom_series_instance_uid"],
                    reviewed_at,
                    source_row["provenance_json"] or "{}",
                ),
            )
            assets += 1
            evidence_rows += 1

    conn.execute(
        """
        UPDATE public_non_dicom_assets AS d
        SET participant_link_status = 'crosswalk_available_at_file_grain',
            quality_flag_json = json_set(
                COALESCE(NULLIF(d.quality_flag_json, ''), '{}'),
                '$.participant_inventory', 'crosswalk_available_at_file_grain'
            )
        WHERE d.asset_granularity = 'download'
          AND EXISTS (
              SELECT 1
              FROM public_non_dicom_assets f
              JOIN public_non_dicom_crosswalk_evidence e ON e.asset_id = f.asset_id
              WHERE f.dataset_type = d.dataset_type
                AND f.short_title = d.short_title
                AND COALESCE(f.download_id, '') = COALESCE(d.download_id, '')
                AND f.file_format = d.file_format
          )
        """
    )
    return {"decisions": len(decisions), "file_assets": assets, "evidence_rows": evidence_rows}


def apply_automated_pathdb_crosswalks(
    conn: sqlite3.Connection,
    snapshot_db: Path,
) -> dict[str, int]:
    """Link complete Aspera inventories to PathDB dataset-scoped subjects.

    This is deliberately stricter than candidate discovery: every file in a
    download must have exactly one safe identifier match. Numeric identifiers
    are accepted only as a complete path component or filename stem.
    """
    candidates: dict[str, set[str]] = {}
    for row in conn.execute(
        """
        SELECT short_title, subject_id
        FROM public_non_dicom_assets
        WHERE source_system = 'tcia_pathdb'
          AND COALESCE(subject_id, '') <> ''
        GROUP BY short_title, subject_id
        """
    ):
        candidates.setdefault(row["short_title"], set()).add(row["subject_id"])

    wordpress: dict[str, tuple[str, str]] = {}
    with sqlite3.connect(snapshot_db) as source:
        source.row_factory = sqlite3.Row
        try:
            rows = source.execute(
                """
                SELECT short_title, link,
                       COALESCE(summary, '') || ' ' || COALESCE(abstract, '') || ' ' ||
                       COALESCE(detailed_description, '') AS text
                FROM agent_datasets WHERE hidden = 0
                """
            )
            for row in rows:
                wordpress[row["short_title"]] = (row["link"], wordpress_evidence(row["text"]))
        except sqlite3.OperationalError:
            # Minimal unit-test snapshots need only agent_current_downloads.
            pass

    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in conn.execute(
        """
        SELECT * FROM public_non_dicom_assets
        WHERE asset_granularity = 'file'
          AND source_system = 'tcia_aspera'
          AND participant_link_status IN ('dataset_only', 'unavailable')
        ORDER BY lower(short_title), download_id, package_path
        """
    ):
        key = (row["dataset_type"], row["short_title"], str(row["download_id"] or ""))
        groups.setdefault(key, []).append(row)

    accepted_downloads = accepted_datasets = evidence_rows = linked_assets = 0
    accepted_titles: set[str] = set()
    generated_at = datetime.now(timezone.utc).isoformat()
    for (dataset_type, title, download_id), assets in groups.items():
        title_candidates = candidates.get(title, set())
        if not title_candidates:
            continue
        candidate_index = build_candidate_index(title_candidates)
        matches: list[tuple[sqlite3.Row, str, str]] = []
        valid = True
        for asset in assets:
            path = str(asset["package_path"] or asset["file_name"] or "")
            found = match_path(path, candidate_index)
            if len(found) > 1:
                found.sort(key=lambda item: len(item[0]), reverse=True)
                longest = found[0][0].casefold()
                if all(item[0].casefold() in longest for item in found[1:]):
                    found = found[:1]
            if len(found) != 1:
                valid = False
                break
            subject_id, method = found[0]
            if subject_id.isdigit() and method not in {"exact_path_component", "exact_filename_stem"}:
                valid = False
                break
            matches.append((asset, subject_id, method))
        if not valid or len(matches) != len(assets):
            continue

        evidence_url, naming_text = wordpress.get(title, ("", ""))
        decision_id = stable_id(
            "crosswalk_decision", dataset_type, title,
            "automated_pathdb_identifier_match", download_id,
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO public_non_dicom_crosswalk_decisions
            VALUES (?, ?, ?, ?, 'accepted_automated', 'participant_crosswalk', ?, ?, ?, ?)
            """,
            (
                decision_id, dataset_type, title, json_dumps([download_id]),
                "Complete metadata-only match between Aspera paths and PathDB dataset-scoped identifiers; no ambiguous paths.",
                evidence_url, generated_at,
                json_dumps({
                    "automation": "exact_or_delimiter_bounded_pathdb_identifier_match",
                    "human_reviewed": False,
                    "matched_files": len(matches),
                    "matched_participants": len({item[1] for item in matches}),
                    "wordpress_naming_evidence": naming_text,
                }),
            ),
        )
        for asset, subject_id, method in matches:
            crosswalk_id = stable_id("crosswalk", asset["asset_id"], method, "pathdb_dataset_identifier")
            conn.execute(
                """
                UPDATE public_non_dicom_assets
                SET subject_id = ?, raw_values_json = json_set(
                        COALESCE(NULLIF(raw_values_json, ''), '{}'),
                        '$.automated_crosswalk.raw_subject_id', ?,
                        '$.automated_crosswalk.pathdb_identifier_match', 1
                    ),
                    subject_id_namespace = ?,
                    participant_link_status = 'automated_source_crosswalk',
                    quality_flag_json = json_set(
                        COALESCE(NULLIF(quality_flag_json, ''), '{}'),
                        '$.participant_inventory', 'automated_source_crosswalk',
                        '$.crosswalk_decision_id', ?
                    )
                WHERE asset_id = ?
                """,
                (subject_id, subject_id, f"tcia_dataset:{title}", decision_id, asset["asset_id"]),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
                VALUES (?, ?, ?, ?, ?, ?, 'high', ?, ?, '', ?, ?)
                """,
                (
                    crosswalk_id, asset["asset_id"], title, subject_id, subject_id,
                    f"automated_{method}_to_pathdb_dataset_identifier", evidence_url,
                    "Automated metadata-only match; complete download coverage and zero ambiguous paths.",
                    generated_at,
                    json_dumps({
                        "decision_id": decision_id,
                        "human_reviewed": False,
                        "identifier_source_system": "tcia_pathdb",
                        "wordpress_naming_evidence": naming_text,
                    }),
                ),
            )
        conn.execute(
            """
            UPDATE public_non_dicom_assets
            SET participant_link_status = 'crosswalk_available_at_file_grain',
                quality_flag_json = json_set(
                    COALESCE(NULLIF(quality_flag_json, ''), '{}'),
                    '$.participant_inventory', 'crosswalk_available_at_file_grain',
                    '$.crosswalk_decision_id', ?
                )
            WHERE asset_granularity = 'download'
              AND dataset_type = ? AND short_title = ? AND COALESCE(download_id, '') = ?
            """,
            (decision_id, dataset_type, title, download_id),
        )
        accepted_downloads += 1
        accepted_titles.add(title)
        linked_assets += len(matches)
        evidence_rows += len(matches)
    accepted_datasets = len(accepted_titles)
    return {
        "downloads": accepted_downloads,
        "datasets": accepted_datasets,
        "file_assets": linked_assets,
        "evidence_rows": evidence_rows,
    }


def add_review_issues(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        WITH unlinked AS (
          SELECT
            short_title,
            COUNT(*) AS affected,
            SUM(CASE WHEN asset_granularity = 'download' THEN 1 ELSE 0 END)
              AS unlinked_download_assets,
            SUM(CASE WHEN asset_granularity = 'file' THEN 1 ELSE 0 END)
              AS unlinked_file_assets
          FROM public_non_dicom_assets
          WHERE participant_link_status IN ('dataset_only', 'unavailable')
          GROUP BY short_title
        ),
        linked AS (
          SELECT
            short_title,
            COUNT(*) AS linked_file_assets,
            COUNT(DISTINCT subject_id) AS linked_participants
          FROM public_non_dicom_assets
          WHERE asset_granularity = 'file'
            AND NULLIF(trim(subject_id), '') IS NOT NULL
          GROUP BY short_title
        )
        SELECT
          u.*,
          COALESCE(l.linked_file_assets, 0) AS linked_file_assets,
          COALESCE(l.linked_participants, 0) AS linked_participants
        FROM unlinked u
        LEFT JOIN linked l USING (short_title)
        """
    ).fetchall()
    conn.executemany(
        """
        INSERT INTO public_non_dicom_review_issues
        VALUES (?, ?, 'participant_file_crosswalk_unavailable', 'info', 'manual_review', ?, ?, ?)
        """,
        [
            (
                stable_id("issue", row["short_title"], "participant_file_crosswalk_unavailable"),
                row["short_title"],
                row["affected"],
                (
                    "One or more public non-DICOM assets lack a source-supported participant-to-file "
                    "crosswalk. Participant-linked coverage elsewhere in the dataset does not establish "
                    "equivalence for these unlinked assets."
                ),
                json_dumps({
                    "unlinked_assets": row["affected"],
                    "unlinked_download_assets": row["unlinked_download_assets"],
                    "unlinked_file_assets": row["unlinked_file_assets"],
                    "linked_file_assets_same_dataset": row["linked_file_assets"],
                    "linked_participants_same_dataset": row["linked_participants"],
                    "coverage_context": (
                        "linked_coverage_also_present"
                        if row["linked_file_assets"]
                        else "no_linked_file_coverage_observed"
                    ),
                }),
            )
            for row in rows
        ],
    )


def build_database(
    snapshot_db: Path,
    out: Path,
    *,
    nifti_db: Path | None,
    pathology_db: Path | None,
    include_pathdb_files: bool,
    replace: bool,
    crosswalk_csv: Path | None = None,
    crosswalk_curation: Path | None = None,
) -> dict[str, Any]:
    if not snapshot_db.exists():
        raise FileNotFoundError(f"Base snapshot not found: {snapshot_db}")
    if out.exists():
        if not replace:
            raise FileExistsError(f"Output exists: {out}; pass --replace")
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    with connect(out) as conn:
        conn.executescript(SCHEMA)
        insert_vocab(conn)
        counts = {
            "wordpress_download_assets": ingest_wordpress(conn, snapshot_db),
            "nifti_file_assets": ingest_nifti(conn, nifti_db) if nifti_db else 0,
            "pathology_package_assets": ingest_pathology_packages(conn, pathology_db) if pathology_db else 0,
            "pathdb_file_assets": ingest_pathdb(conn, snapshot_db, include_pathdb_files),
        }
        reviewed = (
            ingest_reviewed_crosswalks(conn, crosswalk_csv, crosswalk_curation)
            if crosswalk_csv and crosswalk_curation
            else {"decisions": 0, "file_assets": 0, "evidence_rows": 0}
        )
        counts.update({f"reviewed_crosswalk_{key}": value for key, value in reviewed.items()})
        automated = apply_automated_pathdb_crosswalks(conn, snapshot_db)
        counts.update({f"automated_crosswalk_{key}": value for key, value in automated.items()})
        counts["asset_participant_links_projected"] = sync_scalar_asset_participants(conn)
        add_review_issues(conn)
        generated = datetime.now(timezone.utc).isoformat()
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": generated,
            "source_snapshot": source_meta(snapshot_db),
            "source_nifti": source_meta(nifti_db) if nifti_db else {"enabled": False},
            "source_pathology": source_meta(pathology_db) if pathology_db else {"enabled": False},
            "source_crosswalk_csv": source_meta(crosswalk_csv) if crosswalk_csv else {"enabled": False},
            "source_crosswalk_curation": source_meta(crosswalk_curation) if crosswalk_curation else {"enabled": False},
            "include_pathdb_files": include_pathdb_files,
            "ingest_counts": counts,
        }
        conn.executemany(
            "INSERT INTO artifact_meta VALUES (?, ?)",
            [(key, json_dumps(value) if not isinstance(value, str) else value) for key, value in metadata.items()],
        )
        conn.commit()
        conn.execute("ANALYZE")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        counts["asset_rows"] = conn.execute("SELECT COUNT(*) FROM public_non_dicom_assets").fetchone()[0]
        counts["location_rows"] = conn.execute("SELECT COUNT(*) FROM public_non_dicom_locations").fetchone()[0]
        counts["asset_participant_rows"] = conn.execute(
            "SELECT COUNT(*) FROM public_non_dicom_asset_participants"
        ).fetchone()[0]
        counts["participant_summaries"] = conn.execute(
            "SELECT COUNT(*) FROM agent_public_non_dicom_participant_summary"
        ).fetchone()[0]
        counts["review_issues"] = conn.execute("SELECT COUNT(*) FROM public_non_dicom_review_issues").fetchone()[0]
    return {"path": str(out), "schema_version": SCHEMA_VERSION, "integrity_check": integrity, "counts": counts}


def validate_database(path: Path) -> dict[str, Any]:
    required = {
        "public_non_dicom_assets",
        "public_non_dicom_locations",
        "public_non_dicom_asset_relationships",
        "public_non_dicom_asset_participants",
        "public_non_dicom_crosswalk_decisions",
        "public_non_dicom_crosswalk_evidence",
        "public_non_dicom_review_issues",
        "agent_public_non_dicom_assets",
        "agent_public_non_dicom_locations",
        "agent_public_non_dicom_asset_participants",
        "agent_public_non_dicom_dataset_summary",
        "agent_public_non_dicom_participant_summary",
        "agent_public_non_dicom_crosswalk_decisions",
        "agent_public_non_dicom_crosswalk_evidence",
    }
    errors: list[str] = []
    with connect(path) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"integrity_check={integrity}")
        objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        missing = sorted(required - objects)
        if missing:
            errors.append(f"missing objects: {', '.join(missing)}")
        for row in conn.execute("SELECT DISTINCT source_system FROM public_non_dicom_assets"):
            if row[0] not in MANAGED_SYSTEMS:
                errors.append(f"invalid managed system: {row[0]}")
        for row in conn.execute("SELECT DISTINCT representation_provenance_class FROM public_non_dicom_assets"):
            if row[0] not in REPRESENTATION_PROVENANCE_CLASSES:
                errors.append(f"invalid representation class: {row[0]}")
        orphan_locations = conn.execute(
            """SELECT COUNT(*) FROM public_non_dicom_locations l
               LEFT JOIN public_non_dicom_assets a USING(asset_id) WHERE a.asset_id IS NULL"""
        ).fetchone()[0]
        if orphan_locations:
            errors.append(f"orphan locations: {orphan_locations}")
        orphan_participant_links = conn.execute(
            """SELECT COUNT(*) FROM public_non_dicom_asset_participants ap
               LEFT JOIN public_non_dicom_assets a USING(asset_id) WHERE a.asset_id IS NULL"""
        ).fetchone()[0]
        if orphan_participant_links:
            errors.append(f"orphan asset participant links: {orphan_participant_links}")
        counts = {
            "assets": conn.execute("SELECT COUNT(*) FROM public_non_dicom_assets").fetchone()[0],
            "locations": conn.execute("SELECT COUNT(*) FROM public_non_dicom_locations").fetchone()[0],
            "datasets": conn.execute("SELECT COUNT(*) FROM agent_public_non_dicom_dataset_summary").fetchone()[0],
            "participants": conn.execute("SELECT COUNT(*) FROM agent_public_non_dicom_participant_summary").fetchone()[0],
            "asset_participant_links": conn.execute(
                "SELECT COUNT(*) FROM public_non_dicom_asset_participants"
            ).fetchone()[0],
            "crosswalk_evidence": conn.execute("SELECT COUNT(*) FROM public_non_dicom_crosswalk_evidence").fetchone()[0],
        }
    return {"ok": not errors, "errors": errors, "integrity_check": integrity, "counts": counts}


def gzip_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_handle, gzip.open(target, "wb", compresslevel=6) as output_handle:
        shutil.copyfileobj(input_handle, output_handle)


def build_manifest(db: Path, gzip_path: Path | None = None) -> dict[str, Any]:
    validation = validate_database(db)
    if not validation["ok"]:
        raise RuntimeError("Cannot manifest invalid database: " + "; ".join(validation["errors"]))
    manifest: dict[str, Any] = {
        "artifact_family": "tcia-metadata-v2",
        "artifact": "public_non_dicom_metadata",
        "asset": DB_ASSET,
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sqlite_bytes": db.stat().st_size,
        "sqlite_sha256": file_sha256(db),
        "counts": validation["counts"],
    }
    if gzip_path:
        manifest.update({
            "gzip_bytes": gzip_path.stat().st_size,
            "gzip_sha256": file_sha256(gzip_path),
        })
    fingerprint_payload = {key: manifest[key] for key in ("artifact", "schema_version", "sqlite_sha256", "counts")}
    manifest["release_fingerprint"] = hashlib.sha256(json_dumps(fingerprint_payload).encode()).hexdigest()
    return manifest


def github_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "tcia-query-skill"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "tcia-query-skill"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def ensure_release(repo: str, tag: str, db: Path, manifest_path: Path) -> dict[str, Any]:
    release = github_json(f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}")
    assets = {asset["name"]: asset for asset in release.get("assets") or []}
    missing = [name for name in (DB_ASSET, MANIFEST_ASSET) if name not in assets]
    if missing:
        raise RuntimeError(f"Release {repo}@{tag} is missing: {', '.join(missing)}")
    manifest_body = fetch_bytes(assets[MANIFEST_ASSET]["browser_download_url"])
    remote_manifest = json.loads(manifest_body)
    if db.exists() and manifest_path.exists():
        try:
            local = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            local = {}
        if local.get("release_fingerprint") == remote_manifest.get("release_fingerprint") and file_sha256(db) == remote_manifest.get("sqlite_sha256"):
            manifest_path.write_bytes(manifest_body)
            return {"status": "unchanged", "manifest": remote_manifest}
    compressed = fetch_bytes(assets[DB_ASSET]["browser_download_url"])
    if hashlib.sha256(compressed).hexdigest() != remote_manifest.get("gzip_sha256"):
        raise RuntimeError("Downloaded public non-DICOM gzip SHA-256 mismatch")
    raw = gzip.decompress(compressed)
    if hashlib.sha256(raw).hexdigest() != remote_manifest.get("sqlite_sha256"):
        raise RuntimeError("Downloaded public non-DICOM SQLite SHA-256 mismatch")
    db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=db.parent, delete=False) as handle:
        handle.write(raw)
        temporary = Path(handle.name)
    os.replace(temporary, db)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_body)
    return {"status": "downloaded", "manifest": remote_manifest}


def info(path: Path) -> dict[str, Any]:
    with connect(path) as conn:
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM artifact_meta")}
        counts = {
            "assets": conn.execute("SELECT COUNT(*) FROM public_non_dicom_assets").fetchone()[0],
            "locations": conn.execute("SELECT COUNT(*) FROM public_non_dicom_locations").fetchone()[0],
            "datasets": conn.execute("SELECT COUNT(*) FROM agent_public_non_dicom_dataset_summary").fetchone()[0],
            "participants": conn.execute("SELECT COUNT(*) FROM agent_public_non_dicom_participant_summary").fetchone()[0],
            "review_issues": conn.execute("SELECT COUNT(*) FROM public_non_dicom_review_issues").fetchone()[0],
        }
    return {"path": str(path), "meta": meta, "counts": counts}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--snapshot-db", default=str(DEFAULT_SNAPSHOT_DB))
    build.add_argument("--nifti-db", default=str(DEFAULT_NIFTI_DB))
    build.add_argument("--pathology-db", default=str(DEFAULT_PATHOLOGY_DB))
    build.add_argument("--crosswalk-csv", default=str(DEFAULT_CROSSWALK_CSV))
    build.add_argument("--crosswalk-curation", default=str(DEFAULT_CROSSWALK_CURATION))
    build.add_argument("--out", default=str(DEFAULT_DB))
    build.add_argument("--gzip-out")
    build.add_argument("--manifest-out")
    build.add_argument(
        "--no-pathdb-files",
        action="store_true",
        help="Skip PathDB file rows for a faster development build. Full release builds include them.",
    )
    build.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--db", default=str(DEFAULT_DB))
    manifest = sub.add_parser("manifest")
    manifest.add_argument("--db", default=str(DEFAULT_DB))
    manifest.add_argument("--gzip")
    manifest.add_argument("--out", required=True)
    ensure = sub.add_parser("ensure")
    ensure.add_argument("--repo", default=DEFAULT_REPOSITORY)
    ensure.add_argument("--tag", default=DEFAULT_RELEASE_TAG)
    ensure.add_argument("--db", default=str(DEFAULT_DB))
    ensure.add_argument("--manifest-out", default=str(DEFAULT_MANIFEST))
    information = sub.add_parser("info")
    information.add_argument("--db", default=str(DEFAULT_DB))
    datasets = sub.add_parser("datasets")
    datasets.add_argument("--db", default=str(DEFAULT_DB))
    datasets.add_argument("--limit", type=int, default=50)
    files = sub.add_parser("files")
    files.add_argument("--db", default=str(DEFAULT_DB))
    files.add_argument("--collection", required=True)
    files.add_argument("--participant")
    files.add_argument("--limit", type=int, default=100)
    return root


def print_rows(rows: Iterable[sqlite3.Row]) -> None:
    for row in rows:
        print(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))


def main() -> int:
    args = parser().parse_args()
    if args.command == "build":
        out = Path(args.out)
        result = build_database(
            Path(args.snapshot_db), out,
            nifti_db=Path(args.nifti_db) if args.nifti_db else None,
            pathology_db=Path(args.pathology_db) if args.pathology_db else None,
            crosswalk_csv=Path(args.crosswalk_csv) if args.crosswalk_csv else None,
            crosswalk_curation=Path(args.crosswalk_curation) if args.crosswalk_curation else None,
            include_pathdb_files=not args.no_pathdb_files,
            replace=args.replace,
        )
        if args.gzip_out:
            gzip_database(out, Path(args.gzip_out))
        if args.manifest_out:
            manifest = build_manifest(out, Path(args.gzip_out) if args.gzip_out else None)
            Path(args.manifest_out).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            result["manifest"] = manifest
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "validate":
        result = validate_database(Path(args.db))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command == "manifest":
        result = build_manifest(Path(args.db), Path(args.gzip) if args.gzip else None)
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "ensure":
        print(json.dumps(ensure_release(args.repo, args.tag, Path(args.db), Path(args.manifest_out)), indent=2, sort_keys=True))
        return 0
    if args.command == "info":
        print(json.dumps(info(Path(args.db)), indent=2, sort_keys=True))
        return 0
    with connect(Path(args.db)) as conn:
        if args.command == "datasets":
            print_rows(conn.execute(
                "SELECT * FROM agent_public_non_dicom_dataset_summary ORDER BY lower(short_title) LIMIT ?",
                (args.limit,),
            ))
        elif args.command == "files":
            sql = "SELECT * FROM agent_public_non_dicom_assets WHERE lower(short_title) = lower(?)"
            params: list[Any] = [args.collection]
            if args.participant:
                sql += " AND subject_id = ?"
                params.append(args.participant)
            sql += " ORDER BY asset_granularity DESC, package_path, file_name LIMIT ?"
            params.append(args.limit)
            print_rows(conn.execute(sql, params))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
