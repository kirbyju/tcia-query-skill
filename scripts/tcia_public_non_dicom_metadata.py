#!/usr/bin/env python3
"""Build and query the TCIA public non-IDC imaging metadata sidecar.

The builder preserves logical assets separately from their delivery/viewing
locations. It treats PathDB, Aspera, WordPress attachments, and AWS Open Data
as managed systems, not as mutually exclusive data categories. The historical
artifact name remains ``public_non_dicom_metadata``; narrowly scoped public
DICOM holdings that are distributed by TCIA but absent from IDC are retained
as explicit exceptions.
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
from contextlib import closing
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
DEFAULT_CLINICAL_DB = SKILL_ROOT / "cache" / "clinical_metadata.sqlite"
DEFAULT_CROSSWALK_CSV = SKILL_ROOT / "references" / "public_non_dicom_crosswalks_v1.csv"
DEFAULT_CROSSWALK_CURATION = SKILL_ROOT / "references" / "public-non-dicom-crosswalk-curation-v1.json"
DEFAULT_IMAGE_METADATA_CSV = SKILL_ROOT / "references" / "public_non_dicom_image_metadata_v1.csv"
DEFAULT_DB = SKILL_ROOT / "cache" / "public_non_dicom_metadata.sqlite"
DEFAULT_MANIFEST = SKILL_ROOT / "cache" / "public_non_dicom_metadata_manifest.json"
DEFAULT_RELEASE_TAG = "tcia-metadata-v2-latest"
DEFAULT_REPOSITORY = "kirbyju/tcia-query-skill"
DB_ASSET = "public_non_dicom_metadata.sqlite.gz"
MANIFEST_ASSET = "public_non_dicom_metadata_manifest.json"
SCHEMA_VERSION = 7


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
    represented_file_count INTEGER,
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

CREATE TABLE public_non_dicom_image_metadata (
    asset_id TEXT PRIMARY KEY,
    short_title TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    field_source_ids_json TEXT NOT NULL DEFAULT '{}',
    field_provenance_json TEXT NOT NULL DEFAULT '{}',
    conflicting_values_json TEXT NOT NULL DEFAULT '{}',
    quality_flag_json TEXT NOT NULL DEFAULT '{}',
    populated_field_count INTEGER NOT NULL DEFAULT 0,
    conflict_field_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (asset_id) REFERENCES public_non_dicom_assets(asset_id)
);

CREATE TABLE public_non_dicom_metadata_sources (
    source_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_locator TEXT NOT NULL
);

CREATE TABLE public_non_dicom_metadata_field_coverage (
    short_title TEXT NOT NULL,
    field_name TEXT NOT NULL,
    eligible_assets INTEGER NOT NULL,
    populated_assets INTEGER NOT NULL,
    source_raw_assets INTEGER NOT NULL DEFAULT 0,
    normalized_assets INTEGER NOT NULL DEFAULT 0,
    inferred_assets INTEGER NOT NULL DEFAULT 0,
    resolved_assets INTEGER NOT NULL DEFAULT 0,
    distinct_value_count INTEGER NOT NULL DEFAULT 0,
    example_values_json TEXT NOT NULL DEFAULT '[]',
    source_kinds_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (short_title, field_name)
);

CREATE TABLE public_non_dicom_dataset_metadata_notes (
    note_id TEXT PRIMARY KEY,
    short_title TEXT NOT NULL,
    field_name TEXT NOT NULL DEFAULT '',
    note_code TEXT NOT NULL,
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
CREATE INDEX idx_pnd_image_metadata_dataset ON public_non_dicom_image_metadata(short_title);
CREATE INDEX idx_pnd_metadata_notes_dataset ON public_non_dicom_dataset_metadata_notes(short_title, status);

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
    SUM(CASE WHEN asset_granularity IN ('file', 'participant_modality')
             THEN COALESCE(represented_file_count, 1) ELSE 0 END) AS represented_files,
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
    a.source_system,
    ap.subject_id,
    ap.subject_id_namespace,
    ap.link_status AS participant_link_status,
    COUNT(*) AS asset_rows,
    SUM(CASE WHEN a.asset_granularity = 'file' THEN 1 ELSE 0 END) AS file_assets,
    SUM(CASE WHEN a.asset_granularity IN ('file', 'participant_modality')
             THEN COALESCE(a.represented_file_count, 1) ELSE 0 END) AS represented_files,
    group_concat(DISTINCT a.file_format) AS file_formats,
    group_concat(DISTINCT a.media_kind) AS media_kinds,
    group_concat(DISTINCT a.imaging_domain) AS imaging_domains,
    group_concat(DISTINCT a.modality) AS modalities,
    group_concat(DISTINCT a.object_role) AS object_roles,
    MAX(NULLIF(a.source_url, '')) AS access_route,
    SUM(COALESCE(a.size_bytes, 0)) AS known_size_bytes
FROM public_non_dicom_asset_participants ap
JOIN public_non_dicom_assets a USING (asset_id)
GROUP BY a.dataset_type, a.short_title, a.source_system,
         ap.subject_id, ap.subject_id_namespace, ap.link_status;

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

CREATE VIEW agent_public_non_dicom_image_metadata AS
SELECT
    a.asset_id,
    a.dataset_type,
    a.short_title,
    a.subject_id,
    a.file_name,
    a.package_path,
    a.file_format,
    a.media_kind,
    a.object_role,
    json_extract(m.metadata_json, '$.modality') AS modality,
    json_extract(m.metadata_json, '$.body_part_examined') AS body_part_examined,
    json_extract(m.metadata_json, '$.study_description') AS study_description,
    json_extract(m.metadata_json, '$.series_description') AS series_description,
    json_extract(m.metadata_json, '$.manufacturer') AS manufacturer,
    json_extract(m.metadata_json, '$.manufacturer_model_name') AS manufacturer_model_name,
    json_extract(m.metadata_json, '$.magnetic_field_strength_t') AS magnetic_field_strength_t,
    json_extract(m.metadata_json, '$.study_datetime') AS study_datetime,
    json_extract(m.metadata_json, '$.acquisition_dimensionality') AS acquisition_dimensionality,
    json_extract(m.metadata_json, '$.scanner_site') AS scanner_site,
    json_extract(m.metadata_json, '$.sequence_class') AS sequence_class,
    json_extract(m.metadata_json, '$.sequence_tags') AS sequence_tags,
    json_extract(m.metadata_json, '$.slice_thickness_mm') AS slice_thickness_mm,
    json_extract(m.metadata_json, '$.spacing_between_slices_mm') AS spacing_between_slices_mm,
    json_extract(m.metadata_json, '$.repetition_time_ms') AS repetition_time_ms,
    json_extract(m.metadata_json, '$.echo_time_ms') AS echo_time_ms,
    json_extract(m.metadata_json, '$.inversion_time_ms') AS inversion_time_ms,
    json_extract(m.metadata_json, '$.pre_included') AS pre_included,
    json_extract(m.metadata_json, '$.post_included') AS post_included,
    json_extract(m.metadata_json, '$.t2_included') AS t2_included,
    json_extract(m.metadata_json, '$.flair_included') AS flair_included,
    json_extract(m.metadata_json, '$.sequences_present') AS sequences_present,
    json_extract(m.metadata_json, '$.rows') AS rows,
    json_extract(m.metadata_json, '$.columns') AS columns,
    json_extract(m.metadata_json, '$.number_of_slices') AS number_of_slices,
    json_extract(m.metadata_json, '$.pixel_spacing_mm') AS pixel_spacing_mm,
    json_extract(m.metadata_json, '$.pathology_protocol') AS pathology_protocol,
    json_extract(m.metadata_json, '$.magnification') AS magnification,
    m.metadata_json,
    m.field_source_ids_json,
    m.field_provenance_json,
    m.conflicting_values_json,
    m.quality_flag_json,
    m.populated_field_count,
    m.conflict_field_count
FROM public_non_dicom_image_metadata m
JOIN public_non_dicom_assets a USING (asset_id);

CREATE VIEW agent_public_non_dicom_metadata_field_coverage AS
SELECT * FROM public_non_dicom_metadata_field_coverage;

CREATE VIEW agent_public_non_dicom_dataset_metadata_notes AS
SELECT * FROM public_non_dicom_dataset_metadata_notes;
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


def row_value(row: sqlite3.Row, name: str, default: Any = "") -> Any:
    return row[name] if name in row.keys() else default


def nonempty_extension_rows(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, table):
        return {}
    names = [name for name in columns(conn, table) if name != "radiology_id"]
    if not names:
        return {}
    predicate = " OR ".join(f"NULLIF(trim(COALESCE({name}, '')), '') IS NOT NULL" for name in names)
    return {
        str(row["radiology_id"]): {name: row[name] for name in names}
        for row in conn.execute(f"SELECT * FROM {table} WHERE {predicate}")
    }


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


EMPTY_METADATA_VALUES = {"", "n/a", "na", "none", "null", "not available", "unknown"}


def meaningful_metadata_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in EMPTY_METADATA_VALUES
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def merge_image_metadata(
    conn: sqlite3.Connection,
    asset_id: str,
    values: dict[str, Any],
    *,
    value_role: str,
    source_kind: str,
    source_locator: str,
    inference_method: str,
    confidence: str,
    priority: int,
    evidence: dict[str, Any] | None = None,
    short_title: str = "",
    assume_new: bool = False,
) -> int:
    """Add standardized metadata while retaining conflicts and field provenance."""
    if assume_new:
        asset_short_title = short_title
        row = None
    else:
        row = conn.execute(
            """
            SELECT a.short_title AS asset_short_title, m.*
            FROM public_non_dicom_assets a
            LEFT JOIN public_non_dicom_image_metadata m USING(asset_id)
            WHERE a.asset_id = ?
            """,
            (asset_id,),
        ).fetchone()
        if row is None:
            return 0
        asset_short_title = str(row["asset_short_title"])
    metadata = json.loads(row["metadata_json"] or "{}") if row else {}
    field_sources = json.loads(row["field_source_ids_json"] or "{}") if row else {}
    provenance = json.loads(row["field_provenance_json"] or "{}") if row else {}
    provenance_sources = provenance.setdefault("_sources", {})
    conflicts = json.loads(row["conflicting_values_json"] or "{}") if row else {}
    quality = json.loads(row["quality_flag_json"] or "{}") if row else {}
    source_definition = {
        "source_kind": source_kind,
        "source_locator": source_locator,
        "inference_method": inference_method,
        "confidence": confidence,
        "priority": priority,
        "evidence": evidence or {},
    }
    source_id = hashlib.sha256(json_dumps(source_definition).encode("utf-8")).hexdigest()[:12]
    provenance_sources[source_id] = source_definition
    conn.execute(
        "INSERT OR IGNORE INTO public_non_dicom_metadata_sources VALUES (?, ?, ?)",
        (source_id, source_kind, source_locator),
    )
    changed = 0
    for field_name, raw_value in values.items():
        if not meaningful_metadata_value(raw_value):
            continue
        value = raw_value.strip() if isinstance(raw_value, str) else raw_value
        candidate = {
            "value_role": value_role,
            "source_kind": source_kind,
            "inference_method": inference_method,
            "confidence": confidence,
            "priority": priority,
            "source_id": source_id,
        }
        if field_name not in metadata:
            metadata[field_name] = value
            field_sources[field_name] = source_id
            provenance[field_name] = candidate
            changed += 1
            continue
        if json_dumps(metadata[field_name]) == json_dumps(value):
            existing = provenance.setdefault(field_name, candidate)
            field_sources.setdefault(field_name, str(existing.get("source_id") or source_id))
            sources = existing.setdefault("additional_sources", [])
            source_summary = {
                "source_id": source_id,
                "source_kind": source_kind,
                "confidence": confidence,
            }
            if source_summary not in sources and (
                existing.get("source_kind") != source_kind
                or existing.get("source_id") != source_id
            ):
                sources.append(source_summary)
            continue
        existing = provenance.get(field_name, {})
        conflict_rows = conflicts.setdefault(field_name, [])
        conflict_candidate = {"value": value, **candidate}
        if conflict_candidate not in conflict_rows:
            conflict_rows.append(conflict_candidate)
        if priority > int(existing.get("priority", 0)):
            previous = {"value": metadata[field_name], **existing}
            if previous not in conflict_rows:
                conflict_rows.append(previous)
            metadata[field_name] = value
            field_sources[field_name] = source_id
            provenance[field_name] = candidate
            changed += 1
        quality["metadata_conflict_fields"] = sorted(conflicts)
    conn.execute(
        """
        INSERT INTO public_non_dicom_image_metadata
          (asset_id, short_title, metadata_json, field_source_ids_json,
           field_provenance_json,
           conflicting_values_json, quality_flag_json, populated_field_count,
           conflict_field_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset_id) DO UPDATE SET
          metadata_json=excluded.metadata_json,
          field_source_ids_json=excluded.field_source_ids_json,
          field_provenance_json=excluded.field_provenance_json,
          conflicting_values_json=excluded.conflicting_values_json,
          quality_flag_json=excluded.quality_flag_json,
          populated_field_count=excluded.populated_field_count,
          conflict_field_count=excluded.conflict_field_count
        """,
        (
            asset_id,
            asset_short_title,
            json_dumps(metadata),
            json_dumps(field_sources),
            json_dumps(provenance),
            json_dumps(conflicts),
            json_dumps(quality),
            len(metadata),
            len(conflicts),
        ),
    )
    return changed


def add_dataset_metadata_note(
    conn: sqlite3.Connection,
    short_title: str,
    field_name: str,
    note_code: str,
    description: str,
    *,
    severity: str = "info",
    status: str = "manual_review",
    affected_assets: int = 0,
    evidence: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO public_non_dicom_dataset_metadata_notes
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stable_id("metadata_note", short_title, field_name, note_code),
            short_title,
            field_name,
            note_code,
            severity,
            status,
            affected_assets,
            description,
            json_dumps(evidence or {}),
        ),
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


RADIOLOGY_MODALITIES = {"CT", "MR", "MG", "DX", "CR", "US", "PET", "NM", "RTDOSE"}
MR_SEQUENCE_PATTERNS = {
    "T1CE": r"(?:^|[/_.-])(?:t1w?ce|t1gd|t1c)(?=$|[/_.-])",
    "T1": r"(?:^|[/_.-])t1w?(?=$|[/_.-])",
    "T2": r"(?:^|[/_.-])t2w?(?=$|[/_.-])",
    "FLAIR": r"(?:^|[/_.-])flair(?=$|[/_.-])",
    "DWI": r"(?:^|[/_.-])dwi(?=$|[/_.-])",
    "ADC": r"(?:^|[/_.-])adc(?=$|[/_.-])",
    "DSC": r"(?:^|[/_.-])dsc(?=$|[/_.-])",
    "SWI": r"(?:^|[/_.-])swi(?=$|[/_.-])",
}


def modalities_from_labels(values: Iterable[Any]) -> set[str]:
    found: set[str] = set()
    for value in values:
        label = str(value or "").strip().upper()
        if label in RADIOLOGY_MODALITIES:
            found.add(label)
        elif label == "CAPSULE ENDOSCOPY":
            found.add("ES")
    return found


def modalities_from_text(value: str) -> set[str]:
    text = str(value or "")
    full_name_patterns = {
        "CT": r"\bcomputed tomography\b",
        "MR": r"\bmagnetic resonance\b",
        "US": r"\b(?:ultrasound|ultrasonography)\b",
        "MG": r"\b(?:mammograph(?:y|ic)|mammogram)\b",
        "PET": r"\bpositron emission tomography\b",
        "NM": r"\bnuclear medicine\b",
    }
    found = {
        modality for modality, pattern in full_name_patterns.items()
        if re.search(pattern, text, re.IGNORECASE)
    }
    acronym_patterns = {
        "CT": r"\bCTs?\b",
        "MR": r"\b(?:MR|MRI)\b",
        "US": r"\bUS\b",
        "MG": r"\bMG\b",
        "PET": r"\bPET\b",
        "NM": r"\bNM\b",
    }
    found.update(
        modality for modality, pattern in acronym_patterns.items()
        if re.search(pattern, text)
    )
    return found


def filename_metadata(path: str) -> dict[str, Any]:
    lower = str(path or "").casefold()
    values: dict[str, Any] = {}
    sequences = [name for name, pattern in MR_SEQUENCE_PATTERNS.items() if re.search(pattern, lower)]
    if len(sequences) == 1:
        values.update({"modality": "MR", "sequence_name": sequences[0]})
    elif re.search(r"(?:^|[/_.-])ct(?=$|[/_.-])", lower):
        values["modality"] = "CT"
    elif re.search(r"(?:^|[/_.-])(?:us|ultrasound)(?=$|[/_.-])", lower):
        values["modality"] = "US"
    stains = {
        "H&E": r"(?:^|[/_.-])(?:h&e|h[_-]?e|he)(?=$|[/_.-])",
        "CD3": r"(?:^|[/_.-])cd3(?=$|[/_.-])",
        "CD8": r"(?:^|[/_.-])cd8(?=$|[/_.-])",
        "CD56": r"(?:^|[/_.-])cd56(?=$|[/_.-])",
        "CD68": r"(?:^|[/_.-])cd68(?=$|[/_.-])",
        "CD163": r"(?:^|[/_.-])cd163(?=$|[/_.-])",
        "PD-L1": r"(?:^|[/_.-])pd[_-]?l1(?=$|[/_.-])",
        "MHC-1": r"(?:^|[/_.-])mhc[_-]?1(?=$|[/_.-])",
    }
    matched_stains = [name for name, pattern in stains.items() if re.search(pattern, lower)]
    if len(matched_stains) == 1:
        values["pathology_protocol"] = matched_stains[0]
    return values


def compact_number(value: str) -> str:
    return value.rstrip("0").rstrip(".") if "." in value else value


def description_acquisition_candidates(value: str) -> dict[str, list[str]]:
    text = str(value or "")
    strengths = sorted({
        compact_number(match)
        for match in re.findall(r"(?<![\d.])(\d(?:\.\d+)?)\s*[- ]?(?:t|tesla)\b", text, re.IGNORECASE)
    })
    magnifications = sorted({
        f"{compact_number(match)}x"
        for match in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:x|×|[- ]fold)\b", text, re.IGNORECASE)
    })
    stains: list[str] = []
    stain_patterns = {
        "H&E": r"\b(?:H\s*&\s*E|hematoxylin\s+and\s+eosin)\b",
        "Giemsa": r"\b(?:May[- ]Gr[uü]nwald[- ]Giemsa|Jenner[- ]Giemsa|Giemsa)\b",
        "IHC": r"\b(?:immunohistochemistry|IHC)\b",
        "immunofluorescence": r"\bimmunofluorescence\b",
        "CODEX": r"\bCODEX\b",
    }
    for name, pattern in stain_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            stains.append(name)
    manufacturers: list[str] = []
    manufacturer_patterns = {
        "Siemens": r"\bSiemens\b",
        "Philips": r"\bPhilips\b",
        "GE": r"\b(?:GE Healthcare|GE Medical|General Electric)\b",
        "Canon": r"\bCanon\b",
        "Toshiba": r"\bToshiba\b",
    }
    for name, pattern in manufacturer_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            manufacturers.append(name)
    models = sorted({
        match.strip()
        for match in re.findall(
            r"\(([^,()]{2,80}),\s*(?:GE Healthcare|GE Medical|General Electric|Siemens|Philips|Canon|Toshiba)\b",
            text,
            re.IGNORECASE,
        )
    })
    return {
        "magnetic_field_strength_t": strengths,
        "magnification": magnifications,
        "pathology_protocol": stains,
        "manufacturer": manufacturers,
        "manufacturer_model_name": models,
    }


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
    with closing(connect(snapshot_db)) as source:
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
    with closing(connect(nifti_db)) as source:
        if not table_exists(source, "agent_nifti_files"):
            return 0
        downloads: dict[tuple[str, str], str] = {}
        if table_exists(source, "agent_nifti_downloads"):
            for row in source.execute("SELECT short_title, download_id, download_url FROM agent_nifti_downloads"):
                downloads[(str(row["short_title"]), str(row["download_id"] or ""))] = str(row["download_url"] or "")
        extensions: dict[str, dict[str, Any]] = {}
        for table in ("radiology_mr", "radiology_ct", "radiology_pet", "radiology_contrast"):
            for radiology_id, values in nonempty_extension_rows(source, table).items():
                extensions.setdefault(radiology_id, {}).update(values)
        for row in source.execute("SELECT * FROM agent_nifti_files ORDER BY short_title, package_path"):
            package_path = str(row["package_path"] or row["file_name"] or "")
            file_format = format_from_path(package_path) or "NIFTI"
            download_id = str(row["download_id"] or "")
            url = downloads.get((str(row["short_title"]), download_id), "")
            system = managed_system_for_url(url)
            asset_id = stable_id("asset", "nifti", row["short_title"], row["radiology_id"])
            derived = bool(row["is_derived_object"])
            role = "segmentation" if derived else "source_image"
            representation = default_representation_class(system)
            if (
                str(row["short_title"]).casefold()
                == "rsna-asnr-miccai-brats-2021".casefold()
                and re.search(r"/BraTS2021_(?:Training|Validation)Set/", package_path)
            ):
                representation = "standardized_representation"
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
                    "represented_file_count": 1,
                    "size_bytes": None,
                    "checksum": "",
                    "checksum_algorithm": "",
                    "representation_provenance_class": representation,
                    "source_system": system,
                    "source_record_id": row["radiology_id"],
                    "source_url": url,
                    "raw_values_json": json_dumps({"object_type": row["object_type"], "image_type": row["image_type"]}),
                    "provenance_json": json_dumps({"source_artifact": "nifti_metadata", "source_view": "agent_nifti_files"}),
                    "quality_flag_json": row["quality_flag_json"] or "{}",
                },
            )
            insert_location(conn, location_values(asset_id, url, provenance={"source_artifact": "nifti_metadata"}))
            metadata_values = {
                "modality": row_value(row, "modality"),
                "file_format": file_format,
                "media_kind": "image_volume",
                "spatial_dimensionality": "3D" if row_value(row, "number_of_slices") else "unknown",
                "temporal_dimensionality": "time_series" if row_value(row, "number_of_temporal_positions") else "static",
                "object_role": role,
                "body_part_examined": row_value(row, "body_part_examined"),
                "study_instance_uid": row_value(row, "study_instance_uid"),
                "series_instance_uid": row_value(row, "series_instance_uid"),
                "source_doi": row_value(row, "source_doi"),
                "study_date": row_value(row, "study_date"),
                "series_date": row_value(row, "series_date"),
                "study_description": row_value(row, "study_description"),
                "series_description": row_value(row, "series_description"),
                "series_number": row_value(row, "series_number"),
                "manufacturer": row_value(row, "manufacturer"),
                "manufacturer_model_name": row_value(row, "manufacturer_model_name"),
                "software_versions": row_value(row, "software_versions"),
                "image_type": row_value(row, "image_type"),
                "rows": row_value(row, "rows"),
                "columns": row_value(row, "columns"),
                "number_of_slices": row_value(row, "number_of_slices"),
                "number_of_temporal_positions": row_value(row, "number_of_temporal_positions"),
                "pixel_spacing_row_mm": row_value(row, "pixel_spacing_row_mm"),
                "pixel_spacing_col_mm": row_value(row, "pixel_spacing_col_mm"),
                "slice_thickness_mm": row_value(row, "slice_thickness_mm"),
                "spacing_between_slices_mm": row_value(row, "spacing_between_slices_mm"),
                "orientation_or_affine": row_value(row, "orientation_or_affine"),
            }
            metadata_values.update(extensions.get(str(row["radiology_id"]), {}))
            merge_image_metadata(
                conn,
                asset_id,
                metadata_values,
                value_role="normalized",
                source_kind="supporting_spreadsheet_or_package_metadata",
                source_locator="nifti_metadata.agent_nifti_files",
                inference_method="legacy_nifti_file_metadata_projection",
                confidence="high",
                priority=90,
                evidence={"radiology_id": row["radiology_id"]},
                short_title=str(row["short_title"]),
                assume_new=True,
            )
            count += 1
    return count


def metadata_number(value: Any) -> int | float | str | None:
    """Keep source precision while making workbook measurements queryable."""
    if not meaningful_metadata_value(value):
        return None
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def metadata_flag(value: Any) -> bool | None:
    text = str(value or "").strip().casefold()
    if text in {"1", "1.0", "true", "yes", "present"}:
        return True
    if text in {"0", "0.0", "false", "no", "absent"}:
        return False
    return None


def ingest_yale_brain_mets_workbook_metadata(
    conn: sqlite3.Connection, clinical_db: Path | None
) -> dict[str, int]:
    """Join Yale's official file/acquisition workbook rows to NIfTI assets."""
    counts = {
        "image_rows": 0,
        "matched_image_rows": 0,
        "unmatched_image_rows": 0,
        "matched_assets": 0,
        "acquisition_rows": 0,
        "metadata_values": 0,
    }
    if not clinical_db or not clinical_db.exists():
        return counts
    unmatched_examples: list[str] = []
    with closing(connect(clinical_db)) as source:
        if not table_exists(source, "clinical_rows") or not table_exists(
            source, "clinical_sources"
        ):
            return counts
        source_row = source.execute(
            """SELECT source_id, source_url, artifact_sha256
               FROM clinical_sources
               WHERE short_title = 'Yale-Brain-Mets-Longitudinal'
                 AND source_kind = 'tcia_clinical_download'
               ORDER BY source_priority DESC LIMIT 1"""
        ).fetchone()
        if source_row is None:
            return counts
        source_id = str(source_row["source_id"])
        source_url = str(source_row["source_url"] or "")
        acquisitions: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        for row in source.execute(
            """SELECT source_row_id, subject_id, row_json
               FROM clinical_rows
               WHERE source_id = ? AND table_name LIKE '%::Acquisition_data'""",
            (source_id,),
        ):
            values = json.loads(row["row_json"] or "{}")
            study_datetime = str(values.get("study_datetime") or "").strip()
            if not study_datetime:
                continue
            acquisitions[(str(row["subject_id"]), study_datetime)] = (
                str(row["source_row_id"]),
                values,
            )
            counts["acquisition_rows"] += 1

        assets_by_file: dict[str, list[str]] = {}
        for asset in conn.execute(
            """SELECT asset_id, file_name
               FROM public_non_dicom_assets
               WHERE short_title = 'Yale-Brain-Mets-Longitudinal'
                 AND asset_granularity = 'file'
                 AND NULLIF(trim(COALESCE(file_name, '')), '') IS NOT NULL"""
        ):
            assets_by_file.setdefault(str(asset["file_name"]).casefold(), []).append(
                str(asset["asset_id"])
            )

        for row in source.execute(
            """SELECT source_row_id, subject_id, row_json
               FROM clinical_rows
               WHERE source_id = ?
                 AND table_name LIKE '%::image_acquisition_parameters'""",
            (source_id,),
        ):
            counts["image_rows"] += 1
            values = json.loads(row["row_json"] or "{}")
            file_name = str(values.get("file_name") or "").strip()
            asset_ids = assets_by_file.get(file_name.casefold(), [])
            if not asset_ids:
                counts["unmatched_image_rows"] += 1
                if file_name and len(unmatched_examples) < 8:
                    unmatched_examples.append(file_name)
                continue
            counts["matched_image_rows"] += 1
            image_values = {
                "study_datetime": values.get("study_datetime"),
                "sequence_class": values.get("sequence_class"),
                "sequence_tags": values.get("sequence_tags"),
                "slice_thickness_mm": metadata_number(values.get("slice_thickness (mm)")),
                "spacing_between_slices_mm": metadata_number(
                    values.get("spacing_between_slices (mm)")
                ),
                "repetition_time_ms": metadata_number(values.get("repetition_time (ms)")),
                "echo_time_ms": metadata_number(values.get("echo_time (ms)")),
                "inversion_time_ms": metadata_number(values.get("inversion_time (ms)")),
            }
            study_key = (
                str(row["subject_id"]),
                str(values.get("study_datetime") or "").strip(),
            )
            acquisition = acquisitions.get(study_key)
            acquisition_values: dict[str, Any] = {}
            acquisition_row_id = ""
            if acquisition:
                acquisition_row_id, acquisition_row = acquisition
                sequence_flags = {
                    "PRE": metadata_flag(
                        acquisition_row.get("pre_included (1=present; 0=absent)")
                    ),
                    "POST": metadata_flag(
                        acquisition_row.get("post_included (1=present; 0=absent)")
                    ),
                    "T2": metadata_flag(
                        acquisition_row.get("t2_included (1=present; 0=absent)")
                    ),
                    "FLAIR": metadata_flag(
                        acquisition_row.get("flair_included (1=present; 0=absent)")
                    ),
                }
                acquisition_values = {
                    "manufacturer": acquisition_row.get("vendor"),
                    "manufacturer_model_name": acquisition_row.get("model"),
                    "magnetic_field_strength_t": metadata_number(
                        acquisition_row.get("field_strength (T)")
                    ),
                    "acquisition_dimensionality": acquisition_row.get("2D_3D_acquisition"),
                    "scanner_site": acquisition_row.get("scanner_site"),
                    "pre_included": sequence_flags["PRE"],
                    "post_included": sequence_flags["POST"],
                    "t2_included": sequence_flags["T2"],
                    "flair_included": sequence_flags["FLAIR"],
                    "sequences_present": [
                        name for name, present in sequence_flags.items() if present is True
                    ],
                }
            for asset_id in asset_ids:
                counts["metadata_values"] += merge_image_metadata(
                    conn,
                    asset_id,
                    image_values,
                    value_role="normalized",
                    source_kind="tcia_clinical_download",
                    source_locator=f"{source_url}::image_acquisition_parameters",
                    inference_method="exact_file_name_to_official_workbook_row",
                    confidence="high",
                    priority=110,
                    evidence={
                        "source_id": source_id,
                        "source_row_id": str(row["source_row_id"]),
                        "artifact_sha256": str(source_row["artifact_sha256"] or ""),
                    },
                )
                if acquisition_values:
                    counts["metadata_values"] += merge_image_metadata(
                        conn,
                        asset_id,
                        acquisition_values,
                        value_role="normalized",
                        source_kind="tcia_clinical_download",
                        source_locator=f"{source_url}::Acquisition_data",
                        inference_method="patient_and_study_datetime_to_official_workbook_row",
                        confidence="high",
                        priority=110,
                        evidence={
                            "source_id": source_id,
                            "source_row_id": acquisition_row_id,
                            "artifact_sha256": str(source_row["artifact_sha256"] or ""),
                        },
                    )
                counts["matched_assets"] += 1
    if counts["unmatched_image_rows"]:
        add_dataset_metadata_note(
            conn,
            "Yale-Brain-Mets-Longitudinal",
            "file_name",
            "official_workbook_file_not_in_public_inventory",
            "One or more official workbook file names did not match a current public NIfTI asset.",
            affected_assets=counts["unmatched_image_rows"],
            evidence={
                "matched_image_rows": counts["matched_image_rows"],
                "unmatched_image_rows": counts["unmatched_image_rows"],
                "unmatched_file_examples": unmatched_examples,
                "source_url": source_url,
            },
        )
    return counts


BRATS_DICOM_PATH = re.compile(
    r"^(?P<root>.+/BraTS2021_(?P<cohort>Training|Validation)Set_dcm/"
    r"(?P<source_group>[^/]+)/(?P<raw_subject_id>\d{5})/(?P<modality>[^/]+))/"
    r"(?P<file_name>[^/]+\.dcm)$",
    re.IGNORECASE,
)


def ingest_aspera_public_dicom_exceptions(
    conn: sqlite3.Connection, nifti_db: Path
) -> int:
    """Import compact participant/modality summaries for public DICOM absent from IDC.

    The legacy NIfTI sidecar intentionally retains every row from package-level
    ``.sums`` inventories. BraTS 2021 uses that inventory for parallel NIfTI and
    DICOM trees, including nine participants that occur only in the DICOM tree.
    Store one logical asset per participant/modality rather than duplicating
    hundreds of thousands of DICOM-instance rows.
    """
    if not nifti_db.exists():
        return 0
    with closing(connect(nifti_db)) as source:
        if not table_exists(source, "aspera_root_sums_inventory"):
            return 0
        downloads: dict[tuple[str, str], str] = {}
        if table_exists(source, "agent_nifti_downloads"):
            for row in source.execute(
                "SELECT short_title, download_id, download_url FROM agent_nifti_downloads"
            ):
                downloads[(str(row["short_title"]), str(row["download_id"] or ""))] = str(
                    row["download_url"] or ""
                )
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        for row in source.execute(
            """
            SELECT dataset_type, short_title, download_id, package_path
            FROM aspera_root_sums_inventory
            WHERE lower(short_title) = lower('RSNA-ASNR-MICCAI-BraTS-2021')
              AND lower(ltrim(file_ext, '.')) = 'dcm'
            ORDER BY line_number
            """
        ):
            package_path = str(row["package_path"] or "")
            match = BRATS_DICOM_PATH.match(package_path)
            if not match:
                continue
            raw_subject_id = match.group("raw_subject_id")
            subject_id = f"BraTS2021_{raw_subject_id}"
            key = (
                str(row["dataset_type"] or "Analysis Result"),
                str(row["short_title"]),
                str(row["download_id"] or ""),
                match.group("cohort").title(),
                match.group("source_group"),
                raw_subject_id,
                match.group("modality"),
                match.group("root"),
            )
            item = grouped.setdefault(
                key,
                {
                    "subject_id": subject_id,
                    "file_count": 0,
                },
            )
            item["file_count"] += 1

        for key, item in grouped.items():
            (
                dataset_type,
                short_title,
                download_id,
                cohort,
                source_group,
                raw_subject_id,
                modality,
                package_root,
            ) = key
            subject_id = str(item["subject_id"])
            file_count = int(item["file_count"])
            url = downloads.get((short_title, download_id), "")
            asset_id = stable_id(
                "asset",
                "aspera_public_dicom_exception",
                short_title,
                cohort,
                source_group,
                raw_subject_id,
                modality,
            )
            insert_asset(
                conn,
                {
                    "asset_id": asset_id,
                    "dataset_type": dataset_type,
                    "short_title": short_title,
                    "download_row_id": None,
                    "download_id": download_id,
                    "subject_id": subject_id,
                    "subject_id_namespace": f"tcia_dataset:{short_title}",
                    "participant_link_status": "dataset_scoped_source_identifier",
                    "asset_granularity": "participant_modality",
                    "asset_name": f"{subject_id} {modality} DICOM instances",
                    "file_name": "",
                    "package_path": package_root,
                    "file_format": "DICOM",
                    "container_format": "",
                    "media_kind": "dicom_instance_collection",
                    "spatial_dimensionality": "unknown",
                    "temporal_dimensionality": "unknown",
                    "imaging_domain": "radiology",
                    "modality": "MR",
                    "object_role": "source_image",
                    "represented_file_count": file_count,
                    "size_bytes": None,
                    "checksum": "",
                    "checksum_algorithm": "",
                    "representation_provenance_class": "submitted_original",
                    "source_system": "tcia_aspera",
                    "source_record_id": (
                        f"{download_id}:BraTS2021_{cohort}Set_dcm:"
                        f"{source_group}:{raw_subject_id}:{modality}"
                    ),
                    "source_url": url,
                    "raw_values_json": json_dumps(
                        {
                            "challenge_cohort": cohort,
                            "source_group": source_group,
                            "raw_subject_folder": raw_subject_id,
                            "brats_sequence_folder": modality,
                            "represented_dicom_instances": file_count,
                        }
                    ),
                    "provenance_json": json_dumps(
                        {
                            "source_artifact": "nifti_metadata",
                            "source_table": "aspera_root_sums_inventory",
                            "inventory_scope": "public_aspera_dicom_not_in_idc",
                            "identifier_method": "BraTS DICOM folder normalized to challenge ID",
                        }
                    ),
                    "quality_flag_json": json_dumps(
                        {
                            "idc_availability": "not_observed",
                            "file_detail_pointer": "aspera_root_sums_inventory",
                        }
                    ),
                },
            )
            insert_location(
                conn,
                location_values(
                    asset_id,
                    url,
                    representation_class="submitted_original",
                    provenance={
                        "source_artifact": "nifti_metadata",
                        "source_table": "aspera_root_sums_inventory",
                    },
                ),
            )
            merge_image_metadata(
                conn,
                asset_id,
                {
                    "modality": "MR",
                    "file_format": "DICOM",
                    "media_kind": "dicom_instance_collection",
                    "spatial_dimensionality": "unknown",
                    "temporal_dimensionality": "unknown",
                    "object_role": "source_image",
                    "sequence_name": filename_metadata(package_root).get("sequence_name", ""),
                },
                value_role="normalized",
                source_kind="aspera_package_inventory",
                source_locator="nifti_metadata.aspera_root_sums_inventory",
                inference_method="brats_dicom_folder_projection",
                confidence="high",
                priority=90,
                evidence={"package_root": package_root},
                short_title=short_title,
                assume_new=True,
            )
    return len(grouped)


def ingest_pathology_packages(conn: sqlite3.Connection, pathology_db: Path) -> int:
    if not pathology_db.exists():
        return 0
    count = 0
    with closing(connect(pathology_db)) as source:
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
            merge_image_metadata(
                conn,
                asset_id,
                {
                    "modality": row["object_modality"] or "SM",
                    "image_format": row["image_format"],
                    "file_format": file_format,
                    "media_kind": "whole_slide_image" if row["is_wsi"] else "still_image",
                    "spatial_dimensionality": "2D",
                    "temporal_dimensionality": "static",
                    "object_role": row["file_role"] or "source_image",
                    "is_whole_slide_image": bool(row["is_wsi"]),
                    "is_micrograph": bool(row["is_micrograph"]),
                    "is_codex": bool(row["is_codex"]),
                },
                value_role="normalized",
                source_kind="pathology_package_inventory",
                source_locator="pathology_metadata.agent_pathology_file_objects",
                inference_method="pathology_file_inventory_projection",
                confidence="high",
                priority=90,
                evidence={"source_row_id": row["source_row_id"]},
                short_title=str(row["short_title"]),
                assume_new=True,
            )
            count += 1
    return count


def ingest_pathdb(conn: sqlite3.Connection, snapshot_db: Path, include_files: bool) -> int:
    if not include_files:
        return 0
    count = 0
    with closing(connect(snapshot_db)) as source:
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
                    "modality": row["modality"],
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
            merge_image_metadata(
                conn,
                asset_id,
                {
                    "modality": row["modality"] or "SM",
                    "pathology_protocol": row["protocol"],
                    "magnification": row["magnification"],
                    "species": row_value(row, "species"),
                    "cancer_type": row_value(row, "cancer_type"),
                    "cancer_location": row_value(row, "cancer_location"),
                    "image_format": row["data_format"],
                    "file_format": file_format,
                    "media_kind": "whole_slide_image",
                    "spatial_dimensionality": "2D",
                    "temporal_dimensionality": "static",
                    "object_role": "source_image",
                },
                value_role="source_raw",
                source_kind="pathdb_slide_csv",
                source_locator="tcia_snapshot.agent_pathdb_slides",
                inference_method="direct_slide_row",
                confidence="high",
                priority=100,
                evidence={"slide_id": row["slide_id"], "camic_id": row["camic_id"]},
                short_title=str(row["collection"]),
                assume_new=True,
            )
            if not meaningful_metadata_value(row["modality"]):
                merge_image_metadata(
                    conn,
                    asset_id,
                    {"modality": "SM"},
                    value_role="normalized",
                    source_kind="v2_asset_classification",
                    source_locator="tcia_snapshot.agent_pathdb_slides",
                    inference_method="pathology_whole_slide_modality_projection",
                    confidence="high",
                    priority=90,
                    evidence={"imaging_domain": "pathology", "media_kind": "whole_slide_image"},
                    short_title=str(row["collection"]),
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
            existing_assets = conn.execute(
                """
                SELECT * FROM public_non_dicom_assets
                WHERE dataset_type = ? AND short_title = ?
                  AND COALESCE(download_id, '') = COALESCE(?, '')
                  AND file_format = ?
                  AND source_system = ?
                  AND asset_granularity = 'file'
                  AND participant_link_status IN ('dataset_only', 'unavailable')
                  AND (
                    (COALESCE(package_path, '') <> '' AND package_path = ?)
                    OR (COALESCE(package_path, '') = '' AND file_name = ?)
                  )
                """,
                (
                    source_row["dataset_type"], source_row["short_title"],
                    source_row["download_id"], source_row["file_format"], source_system,
                    source_row["package_path"], source_row["file_name"],
                ),
            ).fetchall()
            if len(existing_assets) > 1:
                raise RuntimeError(
                    "Reviewed crosswalk ambiguously matches existing file assets: "
                    f"{source_row['short_title']} {source_row['package_path']}"
                )
            asset_id = (
                str(existing_assets[0]["asset_id"])
                if existing_assets
                else stable_id(
                    "asset", "reviewed_crosswalk", source_row["dataset_type"],
                    source_row["short_title"], source_row["download_id"],
                    source_row["package_path"], source_row["subject_id"],
                )
            )
            crosswalk_id = stable_id("crosswalk", asset_id, source_row["crosswalk_method"], reviewed_at)
            size_bytes = int(source_row["size_bytes"]) if str(source_row["size_bytes"] or "").isdigit() else None
            asset_values = {
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
                }
            if existing_assets:
                existing = existing_assets[0]
                try:
                    existing_provenance = json.loads(existing["provenance_json"] or "{}")
                except json.JSONDecodeError:
                    existing_provenance = {}
                existing_provenance["reviewed_crosswalk"] = json.loads(
                    asset_values["provenance_json"]
                )
                asset_values["provenance_json"] = json_dumps(existing_provenance)
                assignments = [
                    name for name in asset_values
                    if name not in {"asset_id", "download_row_id", "source_record_id"}
                ]
                conn.execute(
                    f"UPDATE public_non_dicom_assets SET "
                    f"{', '.join(f'{name} = ?' for name in assignments)} WHERE asset_id = ?",
                    [asset_values[name] for name in assignments] + [asset_id],
                )
            else:
                insert_asset(conn, asset_values)
            conn.execute(
                "DELETE FROM public_non_dicom_asset_participants WHERE asset_id = ?",
                (asset_id,),
            )
            insert_asset_participant(
                conn,
                asset_id=asset_id,
                short_title=source_row["short_title"],
                subject_id=source_row["subject_id"],
                namespace=source_row["subject_id_namespace"],
                raw_subject_id=source_row["raw_subject_id"],
                participant_role="depicted_subject",
                link_status="reviewed_source_crosswalk",
                evidence={
                    "crosswalk_id": crosswalk_id,
                    "mapping_method": source_row["crosswalk_method"],
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
            try:
                crosswalk_raw = json.loads(source_row["raw_values_json"] or "{}")
            except json.JSONDecodeError:
                crosswalk_raw = {}
            raw_metadata_mapping = {
                "Type": "acquisition_type",
                "Side": "laterality",
                "View": "view_position",
                "Machine": "equipment_code",
                "Pixel_size": "pixel_size_source_value",
                "pixel_spacing": "pixel_spacing_mm",
                "direction_cosines": "direction_cosines",
                "origin": "origin_mm",
                "pixel_type": "pixel_type",
                "rescale_slope": "rescale_slope",
                "rescale_intercept": "rescale_intercept",
                "contrast_used": "contrast_used",
            }
            mapped_metadata = {
                target: crosswalk_raw.get(source)
                for source, target in raw_metadata_mapping.items()
                if meaningful_metadata_value(crosswalk_raw.get(source))
            }
            mapped_metadata.update({
                "modality": source_row["modality"],
                "file_format": source_row["file_format"],
                "media_kind": source_row["media_kind"],
                "spatial_dimensionality": "3D" if source_row["media_kind"] == "image_volume" else "2D",
                "temporal_dimensionality": "video" if source_row["media_kind"] == "video" else "static",
                "object_role": source_row["object_role"],
            })
            merge_image_metadata(
                conn,
                asset_id,
                mapped_metadata,
                value_role="source_raw",
                source_kind="supporting_spreadsheet",
                source_locator=source_row["crosswalk_source_url"],
                inference_method="reviewed_crosswalk_source_row_projection",
                confidence=source_row["crosswalk_confidence"] or "high",
                priority=100,
                evidence={"crosswalk_id": crosswalk_id},
                short_title=str(source_row["short_title"]),
                assume_new=True,
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

            pathdb_matches = conn.execute(
                """
                SELECT * FROM public_non_dicom_assets
                WHERE dataset_type = ? AND short_title = ?
                  AND source_system = 'tcia_pathdb'
                  AND COALESCE(source_url, '') <> ''
                  AND lower(source_url) LIKE '%' || lower(?)
                """,
                (
                    source_row["dataset_type"], source_row["short_title"],
                    source_row["package_path"],
                ),
            ).fetchall()
            if len(pathdb_matches) > 1:
                raise RuntimeError(
                    "Reviewed crosswalk ambiguously matches PathDB assets: "
                    f"{source_row['short_title']} {source_row['package_path']}"
                )
            if pathdb_matches:
                pathdb_asset = pathdb_matches[0]
                try:
                    pathdb_provenance = json.loads(pathdb_asset["provenance_json"] or "{}")
                except json.JSONDecodeError:
                    pathdb_provenance = {}
                pathdb_provenance["reviewed_crosswalk_projection"] = {
                    "crosswalk_id": crosswalk_id,
                    "mapping_method": "exact_pathdb_source_url_suffix",
                }
                conn.execute(
                    """
                    UPDATE public_non_dicom_assets
                    SET subject_id = ?, subject_id_namespace = ?,
                        participant_link_status = 'reviewed_source_crosswalk',
                        provenance_json = ?
                    WHERE asset_id = ?
                    """,
                    (
                        source_row["subject_id"], source_row["subject_id_namespace"],
                        json_dumps(pathdb_provenance), pathdb_asset["asset_id"],
                    ),
                )
                conn.execute(
                    "DELETE FROM public_non_dicom_asset_participants WHERE asset_id = ?",
                    (pathdb_asset["asset_id"],),
                )
                insert_asset_participant(
                    conn,
                    asset_id=str(pathdb_asset["asset_id"]),
                    short_title=source_row["short_title"],
                    subject_id=source_row["subject_id"],
                    namespace=source_row["subject_id_namespace"],
                    raw_subject_id=source_row["raw_subject_id"],
                    participant_role="depicted_subject",
                    link_status="reviewed_source_crosswalk",
                    evidence={
                        "crosswalk_id": crosswalk_id,
                        "mapping_method": "exact_pathdb_source_url_suffix",
                    },
                )

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


def ingest_curated_image_metadata(
    conn: sqlite3.Connection, metadata_csv: Path | None
) -> dict[str, int]:
    if not metadata_csv or not metadata_csv.exists():
        return {"rows": 0, "matched_rows": 0, "matched_assets": 0, "unmatched_rows": 0}
    counts = {"rows": 0, "matched_rows": 0, "matched_assets": 0, "unmatched_rows": 0}
    unmatched_by_dataset: dict[str, list[dict[str, str]]] = {}
    with metadata_csv.open(newline="", encoding="utf-8-sig") as handle:
        for source_row in csv.DictReader(handle):
            counts["rows"] += 1
            short_title = str(source_row.get("short_title") or "").strip()
            file_name = str(source_row.get("file_name") or "").strip()
            subject_id = str(source_row.get("subject_id") or "").strip()
            download_id = str(source_row.get("download_id") or "").strip()
            clauses = ["short_title = ?", "asset_granularity <> 'download'"]
            params: list[Any] = [short_title]
            if file_name:
                clauses.append("lower(file_name) = lower(?)")
                params.append(file_name)
            if subject_id and not file_name:
                clauses.append("subject_id = ?")
                params.append(subject_id)
            if download_id:
                clauses.append("(',' || replace(COALESCE(download_id,''), ';', ',') || ',') LIKE ?")
                params.append(f"%,{download_id},%")
            assets = conn.execute(
                "SELECT asset_id FROM public_non_dicom_assets WHERE " + " AND ".join(clauses),
                params,
            ).fetchall()
            if not assets:
                counts["unmatched_rows"] += 1
                unmatched_by_dataset.setdefault(short_title, []).append({
                    "file_name": file_name,
                    "subject_id": subject_id,
                    "download_id": download_id,
                })
                continue
            try:
                values = json.loads(source_row.get("metadata_json") or "{}")
            except json.JSONDecodeError:
                values = {}
            counts["matched_rows"] += 1
            for asset in assets:
                merge_image_metadata(
                    conn,
                    asset["asset_id"],
                    values,
                    value_role=str(source_row.get("value_role") or "source_raw"),
                    source_kind=str(source_row.get("source_kind") or "supporting_spreadsheet"),
                    source_locator=str(source_row.get("source_url") or source_row.get("source_file") or metadata_csv),
                    inference_method=str(source_row.get("inference_method") or "file_name_to_spreadsheet_row"),
                    confidence=str(source_row.get("confidence") or "high"),
                    priority=int(source_row.get("priority") or 100),
                    evidence={
                        "source_file": source_row.get("source_file") or "",
                        "source_row": source_row.get("source_row") or "",
                        "curation_file": str(metadata_csv),
                    },
                )
                counts["matched_assets"] += 1
    for short_title, rows in unmatched_by_dataset.items():
        add_dataset_metadata_note(
            conn,
            short_title,
            "",
            "curated_spreadsheet_rows_unmatched",
            "One or more curated supporting-spreadsheet metadata rows did not match a current file asset.",
            severity="warning",
            affected_assets=len(rows),
            evidence={"examples": rows[:10], "source_file": str(metadata_csv)},
        )
    return counts


def seed_core_asset_metadata(conn: sqlite3.Connection) -> int:
    changed = 0
    for row in conn.execute(
        """
        SELECT asset_id, modality, file_format, media_kind, spatial_dimensionality,
               temporal_dimensionality, object_role, source_system, source_record_id
        FROM public_non_dicom_assets
        WHERE asset_granularity <> 'download'
          AND NOT EXISTS (
            SELECT 1 FROM public_non_dicom_image_metadata m
            WHERE m.asset_id=public_non_dicom_assets.asset_id
          )
        """
    ):
        changed += merge_image_metadata(
            conn,
            row["asset_id"],
            {
                "modality": row["modality"],
                "file_format": row["file_format"],
                "media_kind": row["media_kind"],
                "spatial_dimensionality": row["spatial_dimensionality"],
                "temporal_dimensionality": row["temporal_dimensionality"],
                "object_role": row["object_role"],
            },
            value_role="normalized",
            source_kind="v2_asset_record",
            source_locator=f"{row['source_system']}:{row['source_record_id'] or ''}",
            inference_method="asset_classification_projection",
            confidence="high",
            priority=85,
        )
    return changed


def enrich_from_wordpress_and_filenames(conn: sqlite3.Connection, snapshot_db: Path) -> dict[str, int]:
    counts = {"filename_assets": 0, "wordpress_label_assets": 0, "description_assets": 0}
    download_context: dict[tuple[str, str], dict[str, Any]] = {}
    dataset_download_modalities: dict[str, set[str]] = {}
    dataset_text: dict[str, str] = {}
    with closing(connect(snapshot_db)) as source:
        download_columns = columns(source, "agent_current_downloads")
        for row in source.execute("SELECT * FROM agent_current_downloads WHERE hidden = 0 AND controlled_access = 0"):
            short_title = str(row["short_title"])
            download_id = str(row["download_id"] or "")
            data_types = parse_list(row["data_types"] if "data_types" in download_columns else "")
            modalities = modalities_from_labels(data_types)
            download_context[(short_title, download_id)] = {
                "modalities": modalities,
                "data_types": data_types,
                "download_title": row_value(row, "download_title"),
                "description": row_value(row, "description"),
            }
            dataset_download_modalities.setdefault(short_title, set()).update(modalities)
        if table_exists(source, "agent_datasets"):
            dataset_columns = columns(source, "agent_datasets")
            text_fields = [
                name for name in ("title", "summary", "abstract", "detailed_description")
                if name in dataset_columns
            ]
            if text_fields:
                dataset_query = "SELECT * FROM agent_datasets"
                if "hidden" in dataset_columns:
                    dataset_query += " WHERE hidden = 0"
                for row in source.execute(dataset_query):
                    dataset_text[str(row["short_title"])] = " ".join(
                        str(row[name] or "") for name in text_fields
                    )
    assets = conn.execute(
        """
        SELECT a.*,
               COALESCE(m.metadata_json, '{}') AS image_metadata_json
        FROM public_non_dicom_assets a
        LEFT JOIN public_non_dicom_image_metadata m USING (asset_id)
        WHERE a.asset_granularity <> 'download'
          AND a.source_system <> 'tcia_pathdb'
        ORDER BY lower(a.short_title), a.asset_id
        """
    ).fetchall()
    assets_by_dataset: dict[str, list[sqlite3.Row]] = {}
    for asset in assets:
        assets_by_dataset.setdefault(str(asset["short_title"]), []).append(asset)
        path_values = filename_metadata(str(asset["package_path"] or asset["file_name"] or ""))
        if path_values:
            merge_image_metadata(
                conn,
                asset["asset_id"],
                path_values,
                value_role="inferred",
                source_kind="structured_filename",
                source_locator=str(asset["package_path"] or asset["file_name"] or ""),
                inference_method="delimiter_bounded_filename_token",
                confidence="medium",
                priority=70,
            )
            counts["filename_assets"] += 1
        current = json.loads(asset["image_metadata_json"] or "{}")
        if meaningful_metadata_value(current.get("modality")) or meaningful_metadata_value(path_values.get("modality")):
            continue
        download_ids = parse_list(str(asset["download_id"] or "").replace(",", ";"))
        modalities: set[str] = set()
        for download_id in download_ids:
            modalities.update(download_context.get((asset["short_title"], download_id), {}).get("modalities", set()))
        if not modalities:
            modalities = dataset_download_modalities.get(str(asset["short_title"]), set())
        if len(modalities) == 1:
            merge_image_metadata(
                conn,
                asset["asset_id"],
                {"modality": next(iter(modalities))},
                value_role="inferred",
                source_kind="wordpress_download_label",
                source_locator=f"agent_current_downloads:{asset['short_title']}:{asset['download_id'] or ''}",
                inference_method="single_unambiguous_download_modality",
                confidence="medium",
                priority=60,
            )
            counts["wordpress_label_assets"] += 1
    for short_title, dataset_assets in assets_by_dataset.items():
        text = dataset_text.get(short_title, "")
        title_modalities = modalities_from_text(short_title)
        description_modalities = modalities_from_text(text)
        text_modalities = title_modalities | description_modalities
        preferred_text_modalities = (
            title_modalities if len(title_modalities) == 1
            else text_modalities if len(text_modalities) == 1
            else set()
        )
        candidates = description_acquisition_candidates(text)
        blank_modality_assets = []
        resolved_modalities: set[str] = set()
        for asset in dataset_assets:
            row = conn.execute(
                "SELECT metadata_json FROM public_non_dicom_image_metadata WHERE asset_id=?",
                (asset["asset_id"],),
            ).fetchone()
            values = json.loads(row["metadata_json"] or "{}") if row else {}
            if not meaningful_metadata_value(values.get("modality")):
                blank_modality_assets.append(asset)
            if len(preferred_text_modalities) == 1 and not meaningful_metadata_value(values.get("modality")):
                merge_image_metadata(
                    conn,
                    asset["asset_id"],
                    {"modality": next(iter(preferred_text_modalities))},
                    value_role="inferred",
                    source_kind="wordpress_dataset_description",
                    source_locator=f"agent_datasets:{short_title}",
                    inference_method=(
                        "single_modality_in_dataset_title"
                        if len(title_modalities) == 1
                        else "single_modality_in_dataset_description"
                    ),
                    confidence="medium",
                    priority=50,
                )
                values["modality"] = next(iter(preferred_text_modalities))
                counts["description_assets"] += 1
            if meaningful_metadata_value(values.get("modality")):
                resolved_modalities.add(str(values["modality"]))
            if values.get("modality") == "MR" and len(candidates["magnetic_field_strength_t"]) == 1:
                merge_image_metadata(
                    conn,
                    asset["asset_id"],
                    {"magnetic_field_strength_t": candidates["magnetic_field_strength_t"][0]},
                    value_role="inferred",
                    source_kind="wordpress_dataset_description",
                    source_locator=f"agent_datasets:{short_title}",
                    inference_method="single_field_strength_in_dataset_description",
                    confidence="medium",
                    priority=45,
                )
            if asset["imaging_domain"] == "pathology":
                for field_name in ("magnification", "pathology_protocol"):
                    if len(candidates[field_name]) == 1:
                        merge_image_metadata(
                            conn,
                            asset["asset_id"],
                            {field_name: candidates[field_name][0]},
                            value_role="inferred",
                            source_kind="wordpress_dataset_description",
                            source_locator=f"agent_datasets:{short_title}",
                            inference_method=f"single_{field_name}_in_dataset_description",
                            confidence="medium",
                            priority=45,
                        )
            if re.search(r"\b(?:all|each|acquired|scanned|digitized)\b", text, re.IGNORECASE):
                equipment_values = {
                    field_name: candidates[field_name][0]
                    for field_name in ("manufacturer", "manufacturer_model_name")
                    if len(candidates[field_name]) == 1
                }
                if equipment_values:
                    merge_image_metadata(
                        conn,
                        asset["asset_id"],
                        equipment_values,
                        value_role="inferred",
                        source_kind="wordpress_dataset_description",
                        source_locator=f"agent_datasets:{short_title}",
                        inference_method="single_scanner_in_uniform_acquisition_description",
                        confidence="low",
                        priority=40,
                    )
        if len(text_modalities) > 1 and not preferred_text_modalities and blank_modality_assets:
            add_dataset_metadata_note(
                conn,
                short_title,
                "modality",
                "mixed_modalities_not_assigned",
                "The dataset description mentions multiple modalities, so modality was not propagated to unclassified files.",
                severity="warning",
                affected_assets=len(blank_modality_assets),
                evidence={"candidate_modalities": sorted(text_modalities)},
            )
        for field_name, values in candidates.items():
            relevant = (
                field_name in {"manufacturer", "manufacturer_model_name"}
                or (field_name == "magnetic_field_strength_t" and "MR" in resolved_modalities)
                or (field_name in {"magnification", "pathology_protocol"} and any(
                    asset["imaging_domain"] == "pathology" for asset in dataset_assets
                ))
            )
            if relevant and len(values) > 1:
                add_dataset_metadata_note(
                    conn,
                    short_title,
                    field_name,
                    "multiple_description_values_not_assigned",
                    f"The WordPress description contains multiple candidate values for {field_name}; no dataset-wide value was assigned.",
                    affected_assets=len(dataset_assets),
                    evidence={"candidate_values": values},
                )
    return counts


def build_metadata_field_coverage(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM public_non_dicom_metadata_field_coverage")
    eligible = {
        str(row["short_title"]): int(row["asset_count"])
        for row in conn.execute(
            "SELECT short_title, COUNT(*) AS asset_count FROM public_non_dicom_image_metadata GROUP BY short_title"
        )
    }
    aggregates: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT short_title, metadata_json, field_provenance_json FROM public_non_dicom_image_metadata"
    ):
        short_title = str(row["short_title"])
        metadata = json.loads(row["metadata_json"] or "{}")
        provenance = json.loads(row["field_provenance_json"] or "{}")
        for field_name, value in metadata.items():
            key = (short_title, field_name)
            item = aggregates.setdefault(
                key,
                {
                    "populated": 0,
                    "roles": {role: 0 for role in ("source_raw", "normalized", "inferred", "resolved")},
                    "distinct_hashes": set(),
                    "examples": [],
                    "source_kinds": set(),
                },
            )
            item["populated"] += 1
            field_provenance = provenance.get(field_name) or {}
            value_role = str(field_provenance.get("value_role") or "")
            if value_role in item["roles"]:
                item["roles"][value_role] += 1
            source_kind = str(field_provenance.get("source_kind") or "")
            if source_kind:
                item["source_kinds"].add(source_kind)
            serialized = json_dumps(value)
            value_hash = hashlib.sha256(serialized.encode("utf-8")).digest()[:16]
            if value_hash not in item["distinct_hashes"]:
                item["distinct_hashes"].add(value_hash)
                if len(item["examples"]) < 8:
                    item["examples"].append(value)
    conn.executemany(
        """
        INSERT INTO public_non_dicom_metadata_field_coverage VALUES
          (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                short_title,
                field_name,
                eligible[short_title],
                item["populated"],
                item["roles"]["source_raw"],
                item["roles"]["normalized"],
                item["roles"]["inferred"],
                item["roles"]["resolved"],
                len(item["distinct_hashes"]),
                json_dumps(item["examples"]),
                json_dumps(sorted(item["source_kinds"])),
            )
            for (short_title, field_name), item in sorted(aggregates.items())
        ],
    )
    return len(aggregates)


def add_metadata_assessment_notes(conn: sqlite3.Connection, nifti_db: Path | None) -> int:
    before = conn.total_changes
    for row in conn.execute(
        """
        SELECT d.short_title, COUNT(*) AS download_assets
        FROM public_non_dicom_assets d
        WHERE d.asset_granularity='download'
          AND NOT EXISTS (
            SELECT 1 FROM public_non_dicom_assets f
            WHERE f.short_title=d.short_title AND f.asset_granularity<>'download'
          )
        GROUP BY d.short_title
        """
    ):
        add_dataset_metadata_note(
            conn,
            row["short_title"],
            "",
            "file_level_inventory_unavailable",
            "Only download-level non-DICOM declarations are available, so image metadata cannot yet be assigned to individual files.",
            affected_assets=row["download_assets"],
        )
    for row in conn.execute(
        """
        SELECT a.short_title, COUNT(*) AS affected
        FROM public_non_dicom_assets a
        LEFT JOIN public_non_dicom_image_metadata m USING(asset_id)
        WHERE a.asset_granularity<>'download'
          AND NULLIF(trim(COALESCE(json_extract(m.metadata_json,'$.modality'),'')),'') IS NULL
        GROUP BY a.short_title
        """
    ):
        add_dataset_metadata_note(
            conn,
            row["short_title"],
            "modality",
            "file_modality_unresolved",
            "One or more file assets still lack a defensible modality after spreadsheet, filename, label, and description inference.",
            severity="warning",
            affected_assets=row["affected"],
        )
    for row in conn.execute(
        """
        SELECT short_title, COUNT(*) AS affected
        FROM public_non_dicom_image_metadata
        WHERE conflicting_values_json <> '{}'
        GROUP BY short_title
        """
    ):
        add_dataset_metadata_note(
            conn,
            row["short_title"],
            "",
            "asset_metadata_value_conflicts",
            "Some assets have conflicting metadata values from different evidence sources; the selected value and alternatives are preserved for review.",
            severity="warning",
            affected_assets=row["affected"],
        )
    for row in conn.execute(
        """
        SELECT short_title, COUNT(*) AS affected,
               json_group_array(DISTINCT json_extract(metadata_json,'$.equipment_code')) AS codes
        FROM public_non_dicom_image_metadata
        WHERE json_extract(metadata_json,'$.equipment_code') IS NOT NULL
        GROUP BY short_title
        """
    ):
        add_dataset_metadata_note(
            conn,
            row["short_title"],
            "equipment_code",
            "equipment_code_dictionary_needed",
            "The supporting spreadsheet supplies equipment codes, but they are not promoted to manufacturer/model without a source dictionary.",
            affected_assets=row["affected"],
            evidence={"codes": json.loads(row["codes"] or "[]")},
        )
    for row in conn.execute(
        """
        SELECT m.short_title, COUNT(*) AS affected
        FROM public_non_dicom_image_metadata m, json_each(m.field_provenance_json) p
        WHERE p.key <> '_sources'
          AND json_extract(p.value,'$.confidence')='low'
        GROUP BY m.short_title
        """
    ):
        add_dataset_metadata_note(
            conn,
            row["short_title"],
            "",
            "low_confidence_description_inference",
            "At least one value was inferred conservatively from acquisition wording in the WordPress description and should be reviewed.",
            affected_assets=row["affected"],
        )
    if nifti_db and nifti_db.exists():
        with closing(connect(nifti_db)) as source:
            if table_exists(source, "normalized_series_rows"):
                candidate_fields = [
                    name for name in (
                        "Manufacturer", "ManufacturerModelName", "MagneticFieldStrength",
                        "ScanningSequence", "SequenceVariant", "MRAcquisitionType",
                        "EchoTime", "RepetitionTime", "FlipAngle", "InversionTime",
                        "ReceiveCoilName", "SequenceName", "DiffusionBValue", "Rows",
                        "Columns", "SliceThickness", "KVP", "ConvolutionKernel",
                        "XRayTubeCurrent_min", "XRayTubeCurrent_max", "SpiralPitchFactor",
                    ) if name in columns(source, "normalized_series_rows")
                ]
                for field_name in candidate_fields:
                    for row in source.execute(
                        f"""
                        SELECT short_title, COUNT(*) AS source_rows,
                               COUNT(DISTINCT {field_name}) AS distinct_values,
                               group_concat(DISTINCT source_file_name) AS source_files
                        FROM normalized_series_rows
                        WHERE NULLIF(trim(COALESCE({field_name},'')),'') IS NOT NULL
                          AND NULLIF(trim(COALESCE(nifti_file,'')),'') IS NULL
                        GROUP BY short_title
                        """
                    ):
                        add_dataset_metadata_note(
                            conn,
                            row["short_title"],
                            field_name,
                            "spreadsheet_values_not_file_mapped",
                            f"Supporting spreadsheets contain {field_name} values, but the rows are not mapped to individual NIfTI files.",
                            affected_assets=row["source_rows"],
                            evidence={
                                "distinct_values": row["distinct_values"],
                                "source_files": parse_list(row["source_files"]),
                            },
                        )
    return conn.total_changes - before


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
    clinical_db: Path | None = None,
    crosswalk_csv: Path | None = None,
    crosswalk_curation: Path | None = None,
    image_metadata_csv: Path | None = None,
) -> dict[str, Any]:
    if not snapshot_db.exists():
        raise FileNotFoundError(f"Base snapshot not found: {snapshot_db}")
    if out.exists():
        if not replace:
            raise FileExistsError(f"Output exists: {out}; pass --replace")
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(out)) as conn:
        conn.executescript(SCHEMA)
        insert_vocab(conn)
        counts = {
            "wordpress_download_assets": ingest_wordpress(conn, snapshot_db),
            "nifti_file_assets": ingest_nifti(conn, nifti_db) if nifti_db else 0,
            "aspera_public_dicom_exception_assets": (
                ingest_aspera_public_dicom_exceptions(conn, nifti_db) if nifti_db else 0
            ),
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
        curated_metadata = ingest_curated_image_metadata(conn, image_metadata_csv)
        counts.update({f"curated_image_metadata_{key}": value for key, value in curated_metadata.items()})
        yale_metadata = ingest_yale_brain_mets_workbook_metadata(conn, clinical_db)
        counts.update({f"yale_workbook_{key}": value for key, value in yale_metadata.items()})
        counts["core_image_metadata_values"] = seed_core_asset_metadata(conn)
        inferred_metadata = enrich_from_wordpress_and_filenames(conn, snapshot_db)
        counts.update({f"inferred_image_metadata_{key}": value for key, value in inferred_metadata.items()})
        counts["metadata_field_coverage_rows"] = build_metadata_field_coverage(conn)
        counts["metadata_assessment_note_changes"] = add_metadata_assessment_notes(conn, nifti_db)
        counts["asset_participant_links_projected"] = sync_scalar_asset_participants(conn)
        add_review_issues(conn)
        generated = datetime.now(timezone.utc).isoformat()
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": generated,
            "source_snapshot": source_meta(snapshot_db),
            "source_nifti": source_meta(nifti_db) if nifti_db else {"enabled": False},
            "source_pathology": source_meta(pathology_db) if pathology_db else {"enabled": False},
            "source_clinical": source_meta(clinical_db) if clinical_db else {"enabled": False},
            "source_crosswalk_csv": source_meta(crosswalk_csv) if crosswalk_csv else {"enabled": False},
            "source_crosswalk_curation": source_meta(crosswalk_curation) if crosswalk_curation else {"enabled": False},
            "source_image_metadata_csv": source_meta(image_metadata_csv) if image_metadata_csv else {"enabled": False},
            "include_pathdb_files": include_pathdb_files,
            "ingest_counts": counts,
        }
        conn.executemany(
            "INSERT INTO artifact_meta VALUES (?, ?)",
            [(key, json_dumps(value) if not isinstance(value, str) else value) for key, value in metadata.items()],
        )
        conn.commit()
        conn.execute("ANALYZE")
        conn.commit()
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
        counts["image_metadata_assets"] = conn.execute(
            "SELECT COUNT(*) FROM public_non_dicom_image_metadata"
        ).fetchone()[0]
        counts["dataset_metadata_notes"] = conn.execute(
            "SELECT COUNT(*) FROM public_non_dicom_dataset_metadata_notes"
        ).fetchone()[0]
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
        "public_non_dicom_image_metadata",
        "public_non_dicom_metadata_sources",
        "public_non_dicom_metadata_field_coverage",
        "public_non_dicom_dataset_metadata_notes",
        "agent_public_non_dicom_assets",
        "agent_public_non_dicom_locations",
        "agent_public_non_dicom_asset_participants",
        "agent_public_non_dicom_dataset_summary",
        "agent_public_non_dicom_participant_summary",
        "agent_public_non_dicom_crosswalk_decisions",
        "agent_public_non_dicom_crosswalk_evidence",
        "agent_public_non_dicom_image_metadata",
        "agent_public_non_dicom_metadata_field_coverage",
        "agent_public_non_dicom_dataset_metadata_notes",
    }
    errors: list[str] = []
    with closing(connect(path)) as conn:
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
        orphan_image_metadata = conn.execute(
            """SELECT COUNT(*) FROM public_non_dicom_image_metadata m
               LEFT JOIN public_non_dicom_assets a USING(asset_id) WHERE a.asset_id IS NULL"""
        ).fetchone()[0]
        if orphan_image_metadata:
            errors.append(f"orphan image metadata rows: {orphan_image_metadata}")
        orphan_field_sources = conn.execute(
            """
            SELECT COUNT(*)
            FROM public_non_dicom_image_metadata m,
                 json_each(m.field_source_ids_json) f
            LEFT JOIN public_non_dicom_metadata_sources s
                   ON s.source_id = f.value
            WHERE s.source_id IS NULL
            """
        ).fetchone()[0]
        if orphan_field_sources:
            errors.append(f"orphan image metadata field-source references: {orphan_field_sources}")
        counts = {
            "assets": conn.execute("SELECT COUNT(*) FROM public_non_dicom_assets").fetchone()[0],
            "locations": conn.execute("SELECT COUNT(*) FROM public_non_dicom_locations").fetchone()[0],
            "datasets": conn.execute("SELECT COUNT(*) FROM agent_public_non_dicom_dataset_summary").fetchone()[0],
            "participants": conn.execute("SELECT COUNT(*) FROM agent_public_non_dicom_participant_summary").fetchone()[0],
            "asset_participant_links": conn.execute(
                "SELECT COUNT(*) FROM public_non_dicom_asset_participants"
            ).fetchone()[0],
            "crosswalk_evidence": conn.execute("SELECT COUNT(*) FROM public_non_dicom_crosswalk_evidence").fetchone()[0],
            "image_metadata_assets": conn.execute("SELECT COUNT(*) FROM public_non_dicom_image_metadata").fetchone()[0],
            "metadata_field_coverage_rows": conn.execute("SELECT COUNT(*) FROM public_non_dicom_metadata_field_coverage").fetchone()[0],
            "dataset_metadata_notes": conn.execute("SELECT COUNT(*) FROM public_non_dicom_dataset_metadata_notes").fetchone()[0],
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
    with closing(connect(db)) as conn:
        artifact_meta = dict(
            conn.execute(
                "SELECT key, value FROM artifact_meta WHERE key IN "
                "('provenance_storage', 'audit_companion_asset', 'audit_schema_version')"
            )
        )
    if artifact_meta:
        manifest["provenance"] = artifact_meta
    if gzip_path:
        manifest.update({
            "gzip_bytes": gzip_path.stat().st_size,
            "gzip_sha256": file_sha256(gzip_path),
        })
    fingerprint_payload = {
        key: manifest[key]
        for key in ("artifact", "schema_version", "sqlite_sha256", "counts")
    }
    if "provenance" in manifest:
        fingerprint_payload["provenance"] = manifest["provenance"]
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
    with closing(connect(path)) as conn:
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM artifact_meta")}
        counts = {
            "assets": conn.execute("SELECT COUNT(*) FROM public_non_dicom_assets").fetchone()[0],
            "locations": conn.execute("SELECT COUNT(*) FROM public_non_dicom_locations").fetchone()[0],
            "datasets": conn.execute("SELECT COUNT(*) FROM agent_public_non_dicom_dataset_summary").fetchone()[0],
            "participants": conn.execute("SELECT COUNT(*) FROM agent_public_non_dicom_participant_summary").fetchone()[0],
            "review_issues": conn.execute("SELECT COUNT(*) FROM public_non_dicom_review_issues").fetchone()[0],
            "image_metadata_assets": conn.execute("SELECT COUNT(*) FROM public_non_dicom_image_metadata").fetchone()[0],
            "metadata_field_coverage_rows": conn.execute("SELECT COUNT(*) FROM public_non_dicom_metadata_field_coverage").fetchone()[0],
            "dataset_metadata_notes": conn.execute("SELECT COUNT(*) FROM public_non_dicom_dataset_metadata_notes").fetchone()[0],
        }
    return {"path": str(path), "meta": meta, "counts": counts}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--snapshot-db", default=str(DEFAULT_SNAPSHOT_DB))
    build.add_argument("--nifti-db", default=str(DEFAULT_NIFTI_DB))
    build.add_argument("--pathology-db", default=str(DEFAULT_PATHOLOGY_DB))
    build.add_argument("--clinical-db", default=str(DEFAULT_CLINICAL_DB))
    build.add_argument("--crosswalk-csv", default=str(DEFAULT_CROSSWALK_CSV))
    build.add_argument("--crosswalk-curation", default=str(DEFAULT_CROSSWALK_CURATION))
    build.add_argument("--image-metadata-csv", default=str(DEFAULT_IMAGE_METADATA_CSV))
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
            clinical_db=Path(args.clinical_db) if args.clinical_db else None,
            crosswalk_csv=Path(args.crosswalk_csv) if args.crosswalk_csv else None,
            crosswalk_curation=Path(args.crosswalk_curation) if args.crosswalk_curation else None,
            image_metadata_csv=Path(args.image_metadata_csv) if args.image_metadata_csv else None,
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
    with closing(connect(Path(args.db))) as conn:
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
