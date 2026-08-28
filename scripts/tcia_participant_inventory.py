#!/usr/bin/env python3
"""Build a compact participant-centered inventory over TCIA metadata sidecars."""

from __future__ import annotations

import argparse
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
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tcia_artifact_model import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
from tcia_artifact_model import json_dumps, stable_id
try:
    from tcia_v2_staging import resolve_component as resolve_staging_component
except ImportError:
    from scripts.tcia_v2_staging import resolve_component as resolve_staging_component


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_DB = SKILL_ROOT / "cache" / "tcia_snapshot.sqlite"
DEFAULT_PUBLIC_DB = SKILL_ROOT / "cache" / "public_non_dicom_metadata.sqlite"
DEFAULT_CONTROLLED_DB = SKILL_ROOT / "cache" / "controlled_access_metadata.sqlite"
DEFAULT_CLINICAL_DB = SKILL_ROOT / "cache" / "clinical_metadata.sqlite"
DEFAULT_IDC_DB = SKILL_ROOT / "cache" / "idc_participant_projection.sqlite"
DEFAULT_DB = SKILL_ROOT / "cache" / "participant_inventory.sqlite"
DEFAULT_MANIFEST = SKILL_ROOT / "cache" / "participant_inventory_manifest.json"
DEFAULT_RELEASE_TAG = "tcia-metadata-v2-latest"
DEFAULT_REPOSITORY = "kirbyju/tcia-query-skill"
DB_ASSET = "participant_inventory.sqlite.gz"
MANIFEST_ASSET = "participant_inventory_manifest.json"
SCHEMA_VERSION = 7

GEOMETRY_CAPABLE_FORMATS = {"DICOM", "NIFTI", "MHA", "MHD", "NRRD"}
GEOMETRY_CHECKED_STATUSES = {
    "checked_regular",
    "checked_not_regular",
    "checked_grid_geometry",
    "checked_invalid_geometry",
}


DISPLAY_SOURCE_PRECEDENCE = {
    "tcia_wordpress": 0,
    "tcia_aspera": 1,
    "pathdb": 2,
    "crdc_ctdc": 3,
    "crdc_gc": 4,
    "crdc_idc": 5,
    "tcia_brats_challenge": 99,
}

BRATS_SHORT_TITLE = "RSNA-ASNR-MICCAI-BraTS-2021"
BCBM_SHORT_TITLE = "BCBM-RadioGenomics"
BRAIN_TR_GAMMAKNIFE_SHORT_TITLE = "Brain-TR-GammaKnife"
REMIND_SHORT_TITLE = "ReMIND"
TCGA_LGG_MASK_SHORT_TITLE = "TCGA-LGG-Mask"
CPTAC_GBM_CODEX_SHORT_TITLE = "CPTAC-Glioblastoma-CODEX"
TCGA_GBM_QI_SHORT_TITLE = "TCGA-GBM-QI-Radiogenomics"


def cptac_gbm_codex_clinical_projection(
    source: sqlite3.Connection,
) -> dict[str, list[str]]:
    """Map official workbook specimen/composite IDs to parent patients."""
    if not table_exists(source, "clinical_rows"):
        return {}
    projected: dict[str, set[str]] = defaultdict(set)
    for row in source.execute(
        """SELECT subject_id, row_json
           FROM clinical_rows
           WHERE short_title = ? AND source_id LIKE 'tcia-download:%'""",
        (CPTAC_GBM_CODEX_SHORT_TITLE,),
    ):
        values = json.loads(str(row["row_json"] or "{}"))
        upenn_id = str(values.get("UPENN-GBM_PatientID") or "").strip()
        cptac_id = str(values.get("CPTAC-GBM_PatientID") or "").strip()
        parent_value = upenn_id or cptac_id
        parents = [item.strip() for item in parent_value.split(",") if item.strip()]
        raw_id = str(row["subject_id"] or "").strip()
        if raw_id and parents:
            projected[raw_id].update(parents)
    return {key: sorted(values) for key, values in projected.items()}


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE participant_inventory_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE participants (
    participant_key TEXT PRIMARY KEY,
    dataset_type TEXT NOT NULL,
    short_title TEXT NOT NULL,
    display_participant_id TEXT NOT NULL,
    identity_scope TEXT NOT NULL DEFAULT 'dataset_scoped',
    within_dataset_identity_status TEXT NOT NULL DEFAULT 'single_namespace',
    identity_resolution_method TEXT NOT NULL DEFAULT 'source_identifier',
    cross_dataset_identity_status TEXT NOT NULL DEFAULT 'not_asserted'
);

CREATE TABLE participant_identifiers (
    participant_identifier_id TEXT PRIMARY KEY,
    participant_key TEXT NOT NULL,
    managed_system TEXT NOT NULL,
    identifier_namespace TEXT NOT NULL,
    raw_identifier TEXT NOT NULL,
    normalized_identifier TEXT NOT NULL,
    link_evidence TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (participant_key) REFERENCES participants(participant_key)
);

CREATE TABLE participant_assets (
    participant_asset_id TEXT PRIMARY KEY,
    participant_key TEXT NOT NULL,
    managed_system TEXT NOT NULL,
    source_artifact TEXT NOT NULL,
    access_level TEXT NOT NULL,
    data_domain TEXT NOT NULL,
    data_category TEXT,
    data_type TEXT,
    media_kind TEXT,
    modality TEXT,
    file_format TEXT,
    object_role TEXT,
    geometry_status TEXT,
    geometry_checked_count INTEGER,
    geometry_regular_count INTEGER,
    geometry_not_regular_count INTEGER,
    geometry_not_checked_count INTEGER,
    geometry_source TEXT,
    study_count INTEGER,
    series_count INTEGER,
    file_count INTEGER,
    known_size_bytes INTEGER,
    has_file_level_metadata INTEGER NOT NULL DEFAULT 0,
    detail_pointer TEXT,
    access_route TEXT,
    inventory_status TEXT NOT NULL DEFAULT 'known',
    source_version TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (participant_key) REFERENCES participants(participant_key)
);

CREATE TABLE participant_clinical_values (
    participant_clinical_value_id TEXT PRIMARY KEY,
    participant_key TEXT NOT NULL,
    concept TEXT NOT NULL,
    raw_field_name TEXT,
    raw_value TEXT,
    standardized_value TEXT,
    value_role TEXT NOT NULL,
    normalization_method TEXT,
    managed_system TEXT NOT NULL,
    source_artifact TEXT NOT NULL,
    source_url TEXT,
    confidence TEXT,
    review_status TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (participant_key) REFERENCES participants(participant_key)
);

CREATE TABLE dataset_assets_without_participant_crosswalk (
    dataset_asset_id TEXT PRIMARY KEY,
    dataset_type TEXT NOT NULL,
    short_title TEXT NOT NULL,
    managed_system TEXT NOT NULL,
    access_level TEXT NOT NULL,
    data_domain TEXT NOT NULL,
    media_kind TEXT,
    modality TEXT,
    file_format TEXT,
    object_role TEXT,
    asset_count INTEGER NOT NULL,
    explanation TEXT NOT NULL,
    detail_pointer TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE participant_inventory_sources (
    source_name TEXT PRIMARY KEY,
    source_path TEXT,
    present INTEGER NOT NULL,
    source_sha256 TEXT,
    imported_rows INTEGER NOT NULL DEFAULT 0,
    coverage_note TEXT NOT NULL
);

CREATE TABLE participant_link_issues (
    issue_id TEXT PRIMARY KEY,
    dataset_type TEXT NOT NULL,
    short_title TEXT NOT NULL,
    raw_identifier TEXT,
    issue_code TEXT NOT NULL,
    status TEXT NOT NULL,
    description TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE participant_identity_evidence (
    identity_evidence_id TEXT PRIMARY KEY,
    participant_key TEXT NOT NULL,
    resolution_scope TEXT NOT NULL,
    resolution_method TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence TEXT NOT NULL,
    description TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (participant_key) REFERENCES participants(participant_key)
);

CREATE INDEX idx_pi_participants_dataset ON participants(short_title, display_participant_id);
CREATE INDEX idx_pi_participants_short_title_nocase ON participants(short_title COLLATE NOCASE);
CREATE INDEX idx_pi_participants_display_id_nocase ON participants(display_participant_id COLLATE NOCASE);
CREATE INDEX idx_pi_identifiers_raw ON participant_identifiers(identifier_namespace, raw_identifier);
CREATE INDEX idx_pi_identifiers_participant ON participant_identifiers(participant_key);
CREATE INDEX idx_pi_identifiers_raw_nocase ON participant_identifiers(raw_identifier COLLATE NOCASE);
CREATE INDEX idx_pi_identifiers_normalized_nocase ON participant_identifiers(normalized_identifier COLLATE NOCASE);
CREATE INDEX idx_pi_assets_participant ON participant_assets(participant_key);
CREATE INDEX idx_pi_assets_access ON participant_assets(access_level, data_domain);
CREATE INDEX idx_pi_clinical_participant ON participant_clinical_values(participant_key, concept);

CREATE VIEW agent_participants AS
SELECT
    p.*,
    (SELECT COUNT(DISTINCT i.identifier_namespace)
     FROM participant_identifiers i
     WHERE i.participant_key = p.participant_key) AS source_namespace_count,
    (SELECT group_concat(DISTINCT i.identifier_namespace)
     FROM participant_identifiers i
     WHERE i.participant_key = p.participant_key) AS source_namespaces,
    COUNT(DISTINCT a.participant_asset_id) AS inventory_rows,
    MAX(CASE WHEN a.access_level = 'open' AND COALESCE(a.source_version, '') <> 'legacy'
             THEN 1 ELSE 0 END) AS has_open_data,
    MAX(CASE WHEN a.access_level = 'controlled' AND COALESCE(a.source_version, '') <> 'legacy'
             THEN 1 ELSE 0 END) AS has_controlled_data,
    MAX(CASE WHEN a.access_level = 'open'
                  AND COALESCE(a.source_version, '') <> 'legacy'
                  AND instr(upper(COALESCE(a.file_format, '')), 'DICOM') > 0
             THEN 1 ELSE 0 END) AS has_public_dicom,
    MAX(CASE WHEN COALESCE(a.source_version, '') <> 'legacy'
                  AND a.source_artifact = 'public_non_dicom_metadata'
                  AND (COALESCE(a.file_format, '') = ''
                       OR instr(upper(a.file_format), 'DICOM') = 0
                       OR instr(upper(a.file_format), 'NIFTI') > 0)
             THEN 1 ELSE 0 END) AS has_public_non_dicom,
    MAX(CASE WHEN a.data_domain = 'clinical' AND COALESCE(a.source_version, '') <> 'legacy'
             THEN 1 ELSE 0 END) AS has_clinical,
    group_concat(DISTINCT CASE WHEN COALESCE(a.source_version, '') <> 'legacy'
                               THEN NULLIF(a.data_domain, '') END) AS data_domains,
    group_concat(DISTINCT CASE WHEN COALESCE(a.source_version, '') <> 'legacy'
                               THEN NULLIF(a.data_category, '') END) AS data_categories,
    group_concat(DISTINCT CASE WHEN COALESCE(a.source_version, '') <> 'legacy'
                               THEN NULLIF(a.data_type, '') END) AS data_types,
    group_concat(DISTINCT CASE WHEN COALESCE(a.source_version, '') <> 'legacy'
                               THEN NULLIF(a.modality, '') END) AS modalities,
    group_concat(DISTINCT CASE WHEN COALESCE(a.source_version, '') <> 'legacy'
                               THEN NULLIF(a.file_format, '') END) AS file_formats,
    group_concat(DISTINCT CASE WHEN COALESCE(a.source_version, '') <> 'legacy'
                               THEN NULLIF(a.geometry_status, '') END) AS geometry_statuses,
    SUM(CASE WHEN COALESCE(a.source_version, '') <> 'legacy'
             THEN COALESCE(a.geometry_checked_count, 0) ELSE 0 END)
      AS geometry_checked_count,
    SUM(CASE WHEN COALESCE(a.source_version, '') <> 'legacy'
             THEN COALESCE(a.geometry_regular_count, 0) ELSE 0 END)
      AS geometry_regular_count,
    SUM(CASE WHEN COALESCE(a.source_version, '') <> 'legacy'
             THEN COALESCE(a.geometry_not_regular_count, 0) ELSE 0 END)
      AS geometry_not_regular_count,
    SUM(CASE WHEN COALESCE(a.source_version, '') <> 'legacy'
             THEN COALESCE(a.geometry_not_checked_count, 0) ELSE 0 END)
      AS geometry_not_checked_count,
    group_concat(DISTINCT a.managed_system) AS managed_systems
FROM participants p
LEFT JOIN participant_assets a USING(participant_key)
GROUP BY p.participant_key;

CREATE VIEW agent_participant_assets AS
SELECT a.*, p.dataset_type, p.short_title, p.display_participant_id
FROM participant_assets a
JOIN participants p USING(participant_key);

CREATE VIEW agent_participant_identifiers AS
SELECT i.*, p.dataset_type, p.short_title, p.display_participant_id
FROM participant_identifiers i
JOIN participants p USING(participant_key);

CREATE VIEW agent_participant_clinical_values AS
SELECT v.*, p.dataset_type, p.short_title, p.display_participant_id
FROM participant_clinical_values v
JOIN participants p USING(participant_key);

CREATE VIEW agent_dataset_assets_without_participant_crosswalk AS
SELECT * FROM dataset_assets_without_participant_crosswalk;

CREATE VIEW agent_participant_link_issues AS
SELECT * FROM participant_link_issues;

CREATE VIEW agent_participant_identity_evidence AS
SELECT e.*, p.dataset_type, p.short_title, p.display_participant_id
FROM participant_identity_evidence e
JOIN participants p USING(participant_key);

CREATE VIEW agent_participant_search AS
SELECT * FROM agent_participants;
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.create_function(
        "casefold",
        1,
        lambda value: str(value or "").strip().casefold(),
        deterministic=True,
    )
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone() is not None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def participant_key(dataset_type: str, short_title: str, raw_identifier: str) -> str:
    """Return one key for a case-equivalent identifier within one TCIA dataset."""
    canonical_identifier = str(raw_identifier or "").strip().casefold()
    return stable_id("participant", dataset_type, short_title, canonical_identifier)


def select_display_participant_ids(conn: sqlite3.Connection) -> int:
    """Select one deterministic source spelling without discarding any identifier."""
    identifiers: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT participant_identifier_id, participant_key, managed_system,
               identifier_namespace, raw_identifier, normalized_identifier
        FROM participant_identifiers
        ORDER BY participant_key, participant_identifier_id
        """
    ):
        identifiers[str(row["participant_key"])].append(row)

    updates: list[tuple[str, str]] = []
    for key, rows in identifiers.items():
        selected = min(
            rows,
            key=lambda row: (
                DISPLAY_SOURCE_PRECEDENCE.get(str(row["managed_system"]), 100),
                -sum(character.isupper() for character in str(row["normalized_identifier"])),
                str(row["normalized_identifier"]).casefold(),
                str(row["normalized_identifier"]),
                str(row["identifier_namespace"]),
                str(row["participant_identifier_id"]),
            ),
        )
        updates.append((str(selected["normalized_identifier"]), key))
    conn.executemany(
        "UPDATE participants SET display_participant_id = ? WHERE participant_key = ?",
        updates,
    )
    return len(updates)


def load_dataset_types(snapshot_db: Path) -> dict[str, tuple[str, str]]:
    """Map a TCIA short title to its authoritative WordPress dataset identity."""
    if not snapshot_db.exists():
        raise FileNotFoundError(f"Base TCIA snapshot does not exist: {snapshot_db}")
    with closing(connect(snapshot_db)) as source:
        if not table_exists(source, "agent_datasets"):
            raise RuntimeError("Base TCIA snapshot is missing agent_datasets")
        rows = source.execute(
            "SELECT DISTINCT dataset_type, short_title FROM agent_datasets "
            "WHERE COALESCE(short_title, '') <> ''"
        ).fetchall()
    result: dict[str, tuple[str, str]] = {}
    for row in rows:
        short_title = str(row["short_title"]).strip()
        dataset_type = str(row["dataset_type"] or "Collection").strip()
        key = short_title.casefold()
        previous = result.get(key)
        identity = (dataset_type, short_title)
        if previous and previous != identity:
            raise RuntimeError(
                "Ambiguous WordPress dataset identity for short title "
                f"{short_title!r}: {previous[0]!r} and {dataset_type!r}"
            )
        result[key] = identity
    return result


def resolve_dataset_identity(
    dataset_types: dict[str, tuple[str, str]], short_title: str, fallback_type: str
) -> tuple[str, str]:
    supplied = str(short_title or "").strip()
    return dataset_types.get(
        supplied.casefold(),
        (str(fallback_type or "Collection").strip() or "Collection", supplied),
    )


def ensure_participant(
    conn: sqlite3.Connection,
    dataset_type: str,
    short_title: str,
    raw_identifier: str,
    *,
    managed_system: str,
    namespace: str,
    evidence: str,
    provenance: dict[str, Any],
) -> str:
    normalized_identifier = str(raw_identifier or "").strip()
    key = participant_key(dataset_type, short_title, normalized_identifier)
    conn.execute(
        """
        INSERT OR IGNORE INTO participants
        VALUES (?, ?, ?, ?, 'dataset_scoped', 'single_namespace',
                'source_identifier', 'not_asserted')
        """,
        (key, dataset_type, short_title, normalized_identifier),
    )
    identifier_id = stable_id("pid", key, managed_system, namespace, raw_identifier)
    conn.execute(
        """
        INSERT OR IGNORE INTO participant_identifiers
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identifier_id,
            key,
            managed_system,
            namespace,
            raw_identifier,
            normalized_identifier,
            evidence,
            json_dumps(provenance),
        ),
    )
    return key


def split_tokens(value: object) -> list[str]:
    return [
        token.strip()
        for token in re.split(r"[,;]", str(value or ""))
        if token.strip()
    ]


def derived_data_categories(data_domain: object, object_role: object) -> str:
    domains = {token.casefold() for token in split_tokens(data_domain)}
    roles = {token.casefold() for token in split_tokens(object_role)}
    categories: list[str] = []
    if "imaging_annotation" in domains or roles & {
        "segmentation",
        "annotation",
        "annotation_snapshot",
        "aim_segmentation_annotation",
    }:
        categories.append("Annotations/Segmentations")
    mapping = (
        ("radiology", "Radiology"),
        ("pathology", "Pathology"),
        ("clinical", "Clinical Data"),
        ("other_imaging", "Other Imaging"),
        ("endoscopy", "Other Imaging"),
    )
    categories.extend(label for token, label in mapping if token in domains)
    return ";".join(dict.fromkeys(categories))


def derived_data_types(
    data_domain: object,
    media_kind: object,
    modality: object,
    object_role: object,
) -> str:
    values: list[str] = split_tokens(modality)
    roles = {token.casefold() for token in split_tokens(object_role)}
    if "segmentation" in roles:
        values.append("Segmentation")
    if roles & {"annotation", "annotation_snapshot", "aim_segmentation_annotation"}:
        values.append("Annotation")
    media_labels = {
        "whole_slide_image": "Whole Slide Image",
        "still_image": "Still Image",
        "video": "Video",
        "spectral_or_array": "Spectral/Array Data",
        "tabular": "Tabular Data",
    }
    for token in split_tokens(media_kind):
        label = media_labels.get(token.casefold())
        if label:
            values.append(label)
    if "clinical" in {token.casefold() for token in split_tokens(data_domain)}:
        values.append("Clinical Data")
    return ";".join(dict.fromkeys(value for value in values if value))


def default_geometry_status(file_format: object, data_domain: object) -> str:
    if "clinical" in {token.casefold() for token in split_tokens(data_domain)}:
        return "not_applicable"
    formats = {token.upper() for token in split_tokens(file_format)}
    return "not_checked" if formats & GEOMETRY_CAPABLE_FORMATS else "not_applicable"


def add_asset(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    values = dict(values)
    values.setdefault(
        "data_category",
        derived_data_categories(values.get("data_domain"), values.get("object_role")),
    )
    values.setdefault(
        "data_type",
        derived_data_types(
            values.get("data_domain"),
            values.get("media_kind"),
            values.get("modality"),
            values.get("object_role"),
        ),
    )
    values.setdefault(
        "geometry_status",
        default_geometry_status(values.get("file_format"), values.get("data_domain")),
    )
    values.setdefault("geometry_checked_count", 0)
    values.setdefault("geometry_regular_count", 0)
    values.setdefault("geometry_not_regular_count", 0)
    values.setdefault(
        "geometry_not_checked_count",
        (
            int(values.get("file_count") or values.get("series_count") or 1)
            if values["geometry_status"] == "not_checked"
            else 0
        ),
    )
    values.setdefault("geometry_source", "not_assessed")
    names = [row[1] for row in conn.execute("PRAGMA table_info(participant_assets)")]
    conn.execute(
        f"INSERT OR IGNORE INTO participant_assets ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
        [values.get(name) for name in names],
    )


def record_source(
    conn: sqlite3.Connection, name: str, path: Path, present: bool, rows: int, note: str
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO participant_inventory_sources VALUES (?, ?, ?, ?, ?, ?)",
        (name, str(path), int(present), file_sha256(path) if present else "", rows, note),
    )


def ingest_public_non_dicom(
    conn: sqlite3.Connection,
    path: Path,
    dataset_types: dict[str, tuple[str, str]],
    *,
    audit_path: Path | None = None,
) -> int:
    if not path.exists():
        record_source(conn, "public_non_dicom_metadata", path, False, 0, "Optional source not installed.")
        return 0
    imported = 0
    with closing(connect(path)) as source:
        if not table_exists(source, "agent_public_non_dicom_participant_summary"):
            record_source(conn, "public_non_dicom_metadata", path, True, 0, "Required participant view missing.")
            return 0
        public_schema_version = ""
        if table_exists(source, "artifact_meta"):
            row = source.execute(
                "SELECT value FROM artifact_meta WHERE key='schema_version'"
            ).fetchone()
            if row:
                public_schema_version = str(row[0])
        public_asset_columns = {
            item[1] for item in source.execute("PRAGMA table_info(public_non_dicom_assets)")
        }
        has_geometry_assessments = table_exists(
            source, "public_non_dicom_geometry_assessments"
        )
        participant_summary_sql = (
            """
            WITH geometry AS (
              SELECT
                a.dataset_type,
                a.short_title,
                a.source_system,
                ap.subject_id,
                ap.subject_id_namespace,
                ap.link_status AS participant_link_status,
                group_concat(DISTINCT a.geometry_status) AS geometry_statuses,
                COUNT(g.geometry_assessment_id) FILTER (
                    WHERE g.geometry_status LIKE 'checked_%'
                )
                    AS geometry_checked_count,
                COUNT(g.geometry_assessment_id) FILTER (
                    WHERE g.geometry_status IN ('checked_regular','checked_grid_geometry')
                )
                    AS geometry_regular_count,
                COUNT(g.geometry_assessment_id) FILTER (
                    WHERE g.geometry_status IN ('checked_not_regular','checked_invalid_geometry')
                )
                    AS geometry_not_regular_count,
                COUNT(DISTINCT CASE WHEN a.geometry_status='not_checked'
                                    THEN a.asset_id END)
                    AS geometry_not_checked_count
              FROM public_non_dicom_asset_participants ap
              JOIN public_non_dicom_assets a USING(asset_id)
              LEFT JOIN public_non_dicom_geometry_assessments g USING(asset_id)
              GROUP BY a.dataset_type, a.short_title, a.source_system,
                       ap.subject_id, ap.subject_id_namespace, ap.link_status
            )
            SELECT s.*, g.geometry_statuses, g.geometry_checked_count,
                   g.geometry_regular_count, g.geometry_not_regular_count,
                   g.geometry_not_checked_count
            FROM agent_public_non_dicom_participant_summary s
            LEFT JOIN geometry g
              ON g.dataset_type=s.dataset_type
             AND g.short_title=s.short_title
             AND g.source_system=s.source_system
             AND g.subject_id=s.subject_id
             AND g.subject_id_namespace=s.subject_id_namespace
             AND g.participant_link_status=s.participant_link_status
            """
            if "geometry_status" in public_asset_columns and has_geometry_assessments
            else """
            SELECT s.*, 'not_checked' AS geometry_statuses,
                   0 AS geometry_checked_count,
                   0 AS geometry_regular_count,
                   0 AS geometry_not_regular_count,
                   COALESCE(s.represented_files, s.file_assets, s.asset_rows, 0)
                     AS geometry_not_checked_count
            FROM agent_public_non_dicom_participant_summary s
            """
        )
        for row in source.execute(participant_summary_sql):
            raw_id = str(row["subject_id"] or "").strip()
            if not raw_id:
                continue
            source_system = (
                str(row["source_system"] or "tcia_wordpress")
                if "source_system" in row.keys()
                else "tcia_wordpress"
            )
            dataset_type, short_title = resolve_dataset_identity(
                dataset_types, str(row["short_title"]), str(row["dataset_type"] or "Collection")
            )
            key = ensure_participant(
                conn,
                dataset_type,
                short_title,
                raw_id,
                managed_system=source_system,
                namespace=f"tcia_dataset:{row['short_title']}",
                evidence=str(row["participant_link_status"] or "dataset_scoped_source_identifier"),
                provenance={"source_artifact": "public_non_dicom_metadata"},
            )
            asset_id = stable_id(
                "pa", key, "public_non_dicom", source_system,
                row["file_formats"], row["object_roles"],
            )
            add_asset(conn, {
                "participant_asset_id": asset_id,
                "participant_key": key,
                "managed_system": source_system,
                "source_artifact": "public_non_dicom_metadata",
                "access_level": "open",
                "data_domain": row["imaging_domains"] or "other_imaging",
                "media_kind": row["media_kinds"] or "",
                "modality": row["modalities"] or "",
                "file_format": row["file_formats"] or "",
                "object_role": row["object_roles"] or "",
                "geometry_status": row["geometry_statuses"] or "not_checked",
                "geometry_checked_count": row["geometry_checked_count"] or 0,
                "geometry_regular_count": row["geometry_regular_count"] or 0,
                "geometry_not_regular_count": row["geometry_not_regular_count"] or 0,
                "geometry_not_checked_count": row["geometry_not_checked_count"] or 0,
                "geometry_source": "public_non_dicom_metadata",
                "study_count": None,
                "series_count": None,
                "file_count": (
                    row["represented_files"]
                    if "represented_files" in row.keys() and row["represented_files"]
                    else row["file_assets"] or row["asset_rows"]
                ),
                "known_size_bytes": row["known_size_bytes"] or 0,
                "has_file_level_metadata": 1,
                "detail_pointer": f"agent_public_non_dicom_asset_participants:{row['short_title']}:{raw_id}",
                "access_route": (
                    row["access_route"]
                    if "access_route" in row.keys() and row["access_route"]
                    else ""
                ),
                "inventory_status": "known",
                "source_version": (
                    f"schema-v{public_schema_version}" if public_schema_version else ""
                ),
                "provenance_json": json_dumps({
                    "source_view": "agent_public_non_dicom_participant_summary",
                    "managed_system": source_system,
                }),
            })
            imported += 1
        for row in source.execute(
            """
            SELECT dataset_type, short_title, source_system, imaging_domain, media_kind,
                   modality, file_format, object_role, COUNT(*) AS asset_count
            FROM public_non_dicom_assets
            WHERE participant_link_status IN ('dataset_only', 'unavailable')
            GROUP BY dataset_type, short_title, source_system, imaging_domain,
                     media_kind, modality, file_format, object_role
            """
        ):
            dataset_asset_id = stable_id(
                "dataset_asset", row["dataset_type"], row["short_title"], row["source_system"],
                row["file_format"], row["object_role"],
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO dataset_assets_without_participant_crosswalk
                VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_asset_id, row["dataset_type"], row["short_title"], row["source_system"],
                    row["imaging_domain"], row["media_kind"], row["modality"], row["file_format"],
                    row["object_role"], row["asset_count"],
                    "Data availability is known, but no source-supported participant-to-file crosswalk is available.",
                    f"agent_public_non_dicom_assets:{row['short_title']}",
                    json_dumps({"source_artifact": "public_non_dicom_metadata"}),
                ),
            )
    record_source(conn, "public_non_dicom_metadata", path, True, imported, "Participant-linked public non-DICOM summaries imported; unlinked dataset assets retained separately.")
    crosswalk_path = audit_path if audit_path and audit_path.is_file() else path
    crosswalk_source = (
        "public_non_dicom_audit" if crosswalk_path != path else "public_non_dicom_metadata"
    )
    aliases = 0
    with closing(connect(crosswalk_path)) as source:
        if table_exists(source, "public_non_dicom_crosswalk_evidence"):
            rows = source.execute(
                """
                SELECT DISTINCT resolved_subject_id AS subject_id, raw_subject_id,
                       provenance_json AS evidence_json
                FROM public_non_dicom_crosswalk_evidence
                WHERE lower(short_title) = lower(?)
                  AND mapping_method = 'official_tcia_brats2021_workbook'
                  AND raw_subject_id GLOB 'BraTS2021_[0-9][0-9][0-9][0-9][0-9]'
                """,
                (BRATS_SHORT_TITLE,),
            )
        elif table_exists(source, "public_non_dicom_asset_participants"):
            # Compatibility for unsplit source databases used by local builders.
            rows = source.execute(
                """
                SELECT DISTINCT subject_id, raw_subject_id, evidence_json
                FROM public_non_dicom_asset_participants
                WHERE lower(short_title) = lower(?)
                  AND raw_subject_id GLOB 'BraTS2021_[0-9][0-9][0-9][0-9][0-9]'
                """,
                (BRATS_SHORT_TITLE,),
            )
        else:
            rows = ()
        for row in rows:
            resolved_id = str(row["subject_id"] or "").strip()
            challenge_id = str(row["raw_subject_id"] or "").strip()
            if challenge_id == resolved_id:
                continue
            evidence = json.loads(str(row["evidence_json"] or "{}"))
            dataset_type, short_title = resolve_dataset_identity(
                dataset_types, BRATS_SHORT_TITLE, "Analysis Result"
            )
            key = participant_key(dataset_type, short_title, resolved_id)
            if not conn.execute(
                "SELECT 1 FROM participants WHERE participant_key = ?", (key,)
            ).fetchone():
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO participant_identifiers
                VALUES (?, ?, 'tcia_brats_challenge', 'challenge:BraTS2021',
                        ?, ?, 'official_tcia_brats2021_workbook', ?)
                """,
                (
                    stable_id("pid", key, "challenge:BraTS2021", challenge_id),
                    key,
                    challenge_id,
                    challenge_id,
                    json_dumps({
                        "source_artifact": crosswalk_source,
                        "crosswalk": evidence,
                    }),
                ),
            )
            aliases += 1
    if audit_path is not None:
        record_source(
            conn,
            "public_non_dicom_audit",
            audit_path,
            audit_path.is_file(),
            aliases,
            "Reviewed BraTS challenge-to-source identity evidence imported.",
        )
    return imported


def apply_brats_source_collection_links(conn: sqlite3.Connection) -> dict[str, int]:
    """Record explicit Analysis Result-to-source Collection identity evidence."""
    counts = {"linked_source_collection": 0, "source_participant_not_current": 0}
    rows = conn.execute(
        """
        SELECT i.participant_key, i.raw_identifier, i.provenance_json
        FROM participant_identifiers i
        JOIN participants p USING(participant_key)
        WHERE p.dataset_type = 'Analysis Result'
          AND lower(p.short_title) = lower(?)
          AND i.identifier_namespace = 'challenge:BraTS2021'
        ORDER BY i.raw_identifier
        """,
        (BRATS_SHORT_TITLE,),
    ).fetchall()
    for row in rows:
        provenance = json.loads(row["provenance_json"] or "{}")
        crosswalk = provenance.get("crosswalk") or {}
        source_collection = str(
            crosswalk.get("source_collection_short_title") or ""
        ).strip()
        source_patient_id = str(
            crosswalk.get("resolved_source_patient_id") or ""
        ).strip()
        if not source_collection or not source_patient_id:
            continue
        source_key = participant_key("Collection", source_collection, source_patient_id)
        source_present = bool(
            conn.execute(
                "SELECT 1 FROM participants WHERE participant_key = ?", (source_key,)
            ).fetchone()
        )
        status = (
            "linked_source_collection"
            if source_present
            else "source_participant_not_current"
        )
        counts[status] += 1
        conn.execute(
            "UPDATE participants SET cross_dataset_identity_status = ? WHERE participant_key = ?",
            (status, row["participant_key"]),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO participant_identity_evidence
            VALUES (?, ?, 'cross_dataset', 'official_tcia_brats2021_workbook',
                    ?, 'high', ?, ?)
            """,
            (
                stable_id(
                    "identity_evidence", row["participant_key"], source_collection,
                    source_patient_id,
                ),
                row["participant_key"],
                "resolved" if source_present else "source_participant_not_current",
                (
                    "Official TCIA BraTS workbook links this Analysis Result participant "
                    f"to {source_collection}/{source_patient_id}."
                ),
                json_dumps({
                    **crosswalk,
                    "source_collection_participant_key": source_key,
                    "source_collection_participant_present": source_present,
                }),
            ),
        )
        if not source_present:
            conn.execute(
                """
                INSERT OR REPLACE INTO participant_link_issues
                VALUES (?, 'Analysis Result', ?, ?,
                        'brats_source_collection_participant_not_current', 'open', ?, ?)
                """,
                (
                    stable_id("participant_issue", BRATS_SHORT_TITLE, row["raw_identifier"]),
                    BRATS_SHORT_TITLE,
                    row["raw_identifier"],
                    f"The official workbook names {source_collection}/{source_patient_id}, "
                    "but that Collection participant is absent from the current inventory.",
                    json_dumps(crosswalk),
                ),
            )
    return counts


def ingest_controlled(
    conn: sqlite3.Connection,
    path: Path,
    dataset_types: dict[str, tuple[str, str]],
) -> int:
    if not path.exists():
        record_source(conn, "controlled_access_metadata", path, False, 0, "Optional detailed controlled metadata not installed.")
        return 0
    imported = 0
    with closing(connect(path)) as source:
        if not table_exists(source, "agent_controlled_files"):
            record_source(conn, "controlled_access_metadata", path, True, 0, "Required controlled file view missing.")
            return 0
        rows = source.execute(
            """
            SELECT
              dataset_type, short_title, route_system,
              COALESCE(NULLIF(trim(patient_id), ''), NULLIF(trim(participant_id), '')) AS subject_id,
              group_concat(DISTINCT modality) AS modalities,
              group_concat(DISTINCT file_format) AS file_formats,
              COUNT(DISTINCT NULLIF(study_instance_uid, '')) AS studies,
              COUNT(DISTINCT NULLIF(series_instance_uid, '')) AS series,
              COUNT(*) AS files,
              SUM(COALESCE(file_size_bytes, 0)) AS known_size_bytes,
              MAX(CASE WHEN COALESCE(drs_uri, '') <> '' THEN drs_uri ELSE '' END) AS access_route
            FROM agent_controlled_files
            WHERE COALESCE(NULLIF(trim(patient_id), ''), NULLIF(trim(participant_id), '')) IS NOT NULL
            GROUP BY dataset_type, short_title, route_system,
                     COALESCE(NULLIF(trim(patient_id), ''), NULLIF(trim(participant_id), ''))
            """
        )
        for row in rows:
            raw_id = str(row["subject_id"])
            route = str(row["route_system"] or "")
            system = "crdc_ctdc" if route == "ctdc" else "crdc_gc"
            dataset_type, short_title = resolve_dataset_identity(
                dataset_types, str(row["short_title"]), str(row["dataset_type"] or "Collection")
            )
            key = ensure_participant(
                conn,
                dataset_type,
                short_title,
                raw_id,
                managed_system=system,
                namespace=f"{system}:{row['short_title']}",
                evidence="dataset_scoped_controlled_manifest_identifier",
                provenance={"source_artifact": "controlled_access_metadata", "route_system": route},
            )
            add_asset(conn, {
                "participant_asset_id": stable_id("pa", key, system, "controlled"),
                "participant_key": key,
                "managed_system": system,
                "source_artifact": "controlled_access_metadata",
                "access_level": "controlled",
                "data_domain": "radiology" if row["modalities"] else "controlled_data",
                "media_kind": "",
                "modality": row["modalities"] or "",
                "file_format": row["file_formats"] or "",
                "object_role": "",
                "study_count": row["studies"],
                "series_count": row["series"],
                "file_count": row["files"],
                "known_size_bytes": row["known_size_bytes"] or 0,
                "has_file_level_metadata": 1,
                "detail_pointer": f"agent_controlled_files:{row['short_title']}:{raw_id}",
                "access_route": row["access_route"] or "",
                "inventory_status": "known_metadata_authorization_required",
                "source_version": "",
                "provenance_json": json_dumps({"source_view": "agent_controlled_files", "route_system": route}),
            })
            imported += 1
    record_source(conn, "controlled_access_metadata", path, True, imported, "Public metadata about controlled holdings imported; no access rights are implied.")
    return imported


def ingest_clinical(
    conn: sqlite3.Connection,
    path: Path,
    dataset_types: dict[str, tuple[str, str]],
    *,
    include_clinical_values: bool = False,
) -> int:
    if not path.exists():
        record_source(conn, "clinical_metadata", path, False, 0, "Optional clinical metadata not installed.")
        return 0
    imported = 0
    with closing(connect(path)) as source:
        codex_projection = cptac_gbm_codex_clinical_projection(source)
        if table_exists(source, "agent_clinical_all_subjects"):
            for row in source.execute("SELECT short_title, subject_id, source_kinds FROM agent_clinical_all_subjects"):
                raw_id = str(row["subject_id"] or "").strip()
                if not raw_id:
                    continue
                dataset_type, short_title = resolve_dataset_identity(
                    dataset_types, str(row["short_title"]), "Collection"
                )
                resolved_ids = (
                    codex_projection.get(raw_id, [raw_id])
                    if short_title == CPTAC_GBM_CODEX_SHORT_TITLE
                    else [raw_id]
                )
                for resolved_id in resolved_ids:
                    evidence = (
                        "official_cptac_gbm_codex_workbook_parent_patient"
                        if resolved_id != raw_id or len(resolved_ids) > 1
                        else "clinical_sidecar_dataset_scoped_identifier"
                    )
                    key = ensure_participant(
                        conn, dataset_type, short_title, resolved_id,
                        managed_system="tcia_wordpress",
                        namespace=f"tcia_dataset:{row['short_title']}",
                        evidence=evidence,
                        provenance={
                            "source_artifact": "clinical_metadata",
                            "source_kinds": row["source_kinds"],
                            "raw_subject_id": raw_id,
                        },
                    )
                    add_asset(conn, {
                        "participant_asset_id": stable_id("pa", key, "clinical"),
                        "participant_key": key,
                        "managed_system": "tcia_wordpress",
                        "source_artifact": "clinical_metadata",
                        "access_level": "open",
                        "data_domain": "clinical",
                        "media_kind": "",
                        "modality": "",
                        "file_format": "",
                        "object_role": "metadata_only",
                        "study_count": None,
                        "series_count": None,
                        "file_count": None,
                        "known_size_bytes": None,
                        "has_file_level_metadata": 0,
                        "detail_pointer": f"agent_clinical_facts:{row['short_title']}:{raw_id}",
                        "access_route": "",
                        "inventory_status": "known",
                        "source_version": "",
                        "provenance_json": json_dumps({
                            "source_view": "agent_clinical_all_subjects",
                            "raw_subject_id": raw_id,
                            "identity_projection": evidence,
                        }),
                    })
                    imported += 1
        # The CODEX workbook rows describe published image files but were not
        # promoted into agent_clinical_all_subjects by the legacy has_imaging
        # gate. Its explicit parent-patient fields are nevertheless valid
        # participant-level clinical/file metadata for all 12 patients.
        for raw_id, resolved_ids in codex_projection.items():
            dataset_type, short_title = resolve_dataset_identity(
                dataset_types, CPTAC_GBM_CODEX_SHORT_TITLE, "Analysis Result"
            )
            for resolved_id in resolved_ids:
                key = ensure_participant(
                    conn, dataset_type, short_title, resolved_id,
                    managed_system="tcia_wordpress",
                    namespace=f"tcia_dataset:{CPTAC_GBM_CODEX_SHORT_TITLE}",
                    evidence="official_cptac_gbm_codex_workbook_parent_patient",
                    provenance={
                        "source_artifact": "clinical_metadata",
                        "raw_subject_id": raw_id,
                        "projection": "official_workbook_parent_patient",
                    },
                )
                add_asset(
                    conn,
                    {
                        "participant_asset_id": stable_id("pa", key, "clinical"),
                        "participant_key": key,
                        "managed_system": "tcia_wordpress",
                        "source_artifact": "clinical_metadata",
                        "access_level": "open",
                        "data_domain": "clinical",
                        "media_kind": "",
                        "modality": "",
                        "file_format": "XLSX",
                        "object_role": "metadata_only",
                        "study_count": None,
                        "series_count": None,
                        "file_count": None,
                        "known_size_bytes": None,
                        "has_file_level_metadata": 1,
                        "detail_pointer": (
                            f"clinical_rows:{CPTAC_GBM_CODEX_SHORT_TITLE}:{raw_id}"
                        ),
                        "access_route": "",
                        "inventory_status": "known",
                        "source_version": "",
                        "provenance_json": json_dumps(
                            {
                                "source_view": "clinical_rows",
                                "raw_subject_id": raw_id,
                                "identity_projection": (
                                    "official_cptac_gbm_codex_workbook_parent_patient"
                                ),
                            }
                        ),
                    },
                )
        if include_clinical_values and table_exists(source, "agent_clinical_facts"):
            for row in source.execute(
                """
                SELECT short_title, subject_id, concept, value_text, value_resolved,
                       source_kind, source_url, original_column, evidence_scope,
                       is_inferred, qc_status, provenance_json
                FROM agent_clinical_facts
                WHERE COALESCE(qc_excluded, 0) = 0
                """
            ):
                raw_id = str(row["subject_id"] or "").strip()
                if not raw_id:
                    continue
                dataset_type, short_title = resolve_dataset_identity(
                    dataset_types, str(row["short_title"]), "Collection"
                )
                raw_value = str(row["value_text"] or "")
                standardized = str(row["value_resolved"] or "")
                if row["is_inferred"]:
                    role = "inferred"
                    method = "source-recorded inference"
                elif standardized and standardized != raw_value:
                    role = "harmonized"
                    method = "clinical sidecar concept mapping and source precedence"
                elif standardized:
                    role = "normalized"
                    method = "clinical sidecar normalization"
                else:
                    role = "source_raw"
                    method = "preserved without standardized value"
                resolved_ids = (
                    codex_projection.get(raw_id, [raw_id])
                    if short_title == CPTAC_GBM_CODEX_SHORT_TITLE
                    else [raw_id]
                )
                for resolved_id in resolved_ids:
                    key = participant_key(dataset_type, short_title, resolved_id)
                    if conn.execute(
                        "SELECT 1 FROM participants WHERE participant_key = ?", (key,)
                    ).fetchone() is None:
                        key = ensure_participant(
                            conn, dataset_type, short_title, resolved_id,
                            managed_system="tcia_wordpress",
                            namespace=f"tcia_dataset:{row['short_title']}",
                            evidence=(
                                "official_cptac_gbm_codex_workbook_parent_patient"
                                if resolved_id != raw_id or len(resolved_ids) > 1
                                else "clinical_fact_dataset_scoped_identifier"
                            ),
                            provenance={
                                "source_artifact": "clinical_metadata",
                                "raw_subject_id": raw_id,
                            },
                        )
                    value_id = stable_id(
                        "clinical_value", key, raw_id, row["concept"],
                        row["source_kind"], row["source_url"],
                        row["original_column"], raw_value, standardized,
                    )
                    try:
                        value_provenance = json.loads(
                            str(row["provenance_json"] or "{}")
                        )
                    except json.JSONDecodeError:
                        value_provenance = {}
                    value_provenance.update(
                        {
                            "source_kind": row["source_kind"],
                            "evidence_scope": row["evidence_scope"],
                            "raw_subject_id": raw_id,
                            "resolved_participant_id": resolved_id,
                        }
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO participant_clinical_values
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'tcia_wordpress',
                                'clinical_metadata', ?, ?, ?, ?)
                        """,
                        (
                            value_id, key, row["concept"], row["original_column"],
                            raw_value, standardized, role, method,
                            row["source_url"] or "",
                            "inferred" if row["is_inferred"] else "source_supported",
                            row["qc_status"] or "", json_dumps(value_provenance),
                        ),
                    )
        # Preserve previously observed IDC Collection membership only when the
        # clinical artifact explicitly marks it as legacy. Current IDC presence
        # is supplied exclusively by the direct build-time projection above.
        if table_exists(source, "clinical_imaging_subjects"):
            for row in source.execute(
                """
                SELECT short_title, subject_id, imaging_source
                FROM clinical_imaging_subjects
                WHERE imaging_source = 'legacy_idc_index'
                """
            ):
                raw_id = str(row["subject_id"] or "").strip()
                if not raw_id:
                    continue
                dataset_type, short_title = resolve_dataset_identity(
                    dataset_types, str(row["short_title"]), "Collection"
                )
                if dataset_type != "Collection":
                    continue
                key = ensure_participant(
                    conn,
                    dataset_type,
                    short_title,
                    raw_id,
                    managed_system="crdc_idc",
                    namespace=f"tcia_dataset:{short_title}",
                    evidence="legacy_idc_index_participant_projection",
                    provenance={
                        "source_artifact": "clinical_metadata",
                        "imaging_source": row["imaging_source"],
                        "current_idc_presence": False,
                    },
                )
                add_asset(
                    conn,
                    {
                        "participant_asset_id": stable_id(
                            "pa", key, "crdc_idc", "public_dicom"
                        ),
                        "participant_key": key,
                        "managed_system": "crdc_idc",
                        "source_artifact": "clinical_metadata",
                        "access_level": "open",
                        "data_domain": "radiology",
                        "media_kind": "dicom_series",
                        "modality": "",
                        "file_format": "DICOM",
                        "object_role": "source_image_or_annotation",
                        "study_count": None,
                        "series_count": None,
                        "file_count": None,
                        "known_size_bytes": None,
                        "has_file_level_metadata": 0,
                        "detail_pointer": f"crdc_idc_legacy:{short_title}:{raw_id}",
                        "access_route": "",
                        "inventory_status": (
                            "historical_participant_presence_query_idc_or_tcia_for_detail"
                        ),
                        "source_version": "legacy",
                        "provenance_json": json_dumps(
                            {
                                "source_table": "clinical_imaging_subjects",
                                "imaging_source": row["imaging_source"],
                                "current_idc_presence": False,
                            }
                        ),
                    },
                )
                imported += 1
    record_source(conn, "clinical_metadata", path, True, imported, "Clinical availability imported. Current public-DICOM presence comes directly from IDC; explicitly legacy IDC Collection memberships are retained as historical compatibility evidence.")
    return imported


def ingest_idc_participants(
    conn: sqlite3.Connection,
    path: Path,
    dataset_types: dict[str, tuple[str, str]],
) -> int:
    if not path.exists():
        record_source(
            conn,
            "idc_participant_projection",
            path,
            False,
            0,
            "Build-time IDC participant projection not installed.",
        )
        return 0
    imported = 0
    with closing(connect(path)) as source:
        if not table_exists(source, "agent_idc_dataset_participants"):
            raise RuntimeError(
                "IDC participant projection is missing agent_idc_dataset_participants"
            )
        idc_columns = {
            item[1] for item in source.execute("PRAGMA table_info(agent_idc_dataset_participants)")
        }
        object_roles_sql = (
            "object_roles"
            if "object_roles" in idc_columns
            else "'source_image_or_annotation' AS object_roles"
        )
        geometry_sql = (
            """geometry_statuses, geometry_eligible_series_count,
                   geometry_checked_series_count,
                   regularly_spaced_volume_series_count,
                   non_regular_volume_series_count,
                   geometry_not_checked_series_count"""
            if "geometry_statuses" in idc_columns
            else """'not_checked' AS geometry_statuses,
                   0 AS geometry_eligible_series_count,
                   0 AS geometry_checked_series_count,
                   0 AS regularly_spaced_volume_series_count,
                   0 AS non_regular_volume_series_count,
                   series_count AS geometry_not_checked_series_count"""
        )
        for row in source.execute(
            f"""
            SELECT dataset_type, short_title, participant_id,
                   source_collection_ids_json,
                   source_analysis_result_ids_json,
                   study_count, series_count, modalities, {object_roles_sql}, source_dois,
                   {geometry_sql},
                   idc_version
            FROM agent_idc_dataset_participants
            """
        ):
            raw_id = str(row["participant_id"] or "").strip()
            supplied_type = str(row["dataset_type"] or "Collection").strip()
            dataset_type, short_title = resolve_dataset_identity(
                dataset_types, str(row["short_title"]), supplied_type
            )
            if not raw_id or dataset_type != supplied_type:
                continue
            key = ensure_participant(
                conn,
                dataset_type,
                short_title,
                raw_id,
                managed_system="crdc_idc",
                namespace=f"tcia_dataset:{short_title}",
                evidence="idc_index_participant_projection",
                provenance={
                    "source_artifact": "idc_participant_projection",
                    "source_collection_ids": json.loads(
                        row["source_collection_ids_json"] or "[]"
                    ),
                    "source_analysis_result_ids": json.loads(
                        row["source_analysis_result_ids_json"] or "[]"
                    ),
                    "source_dois": row["source_dois"] or "",
                    "idc_version": row["idc_version"] or "",
                },
            )
            add_asset(
                conn,
                {
                    "participant_asset_id": stable_id(
                        "pa", key, "crdc_idc", "public_dicom"
                    ),
                    "participant_key": key,
                    "managed_system": "crdc_idc",
                    "source_artifact": "idc_participant_projection",
                    "access_level": "open",
                    "data_domain": "radiology",
                    "media_kind": "",
                    "modality": row["modalities"] or "",
                    "file_format": "DICOM",
                    "object_role": row["object_roles"] or "",
                    "geometry_status": row["geometry_statuses"] or "not_checked",
                    "geometry_checked_count": row["geometry_checked_series_count"] or 0,
                    "geometry_regular_count": row["regularly_spaced_volume_series_count"] or 0,
                    "geometry_not_regular_count": row["non_regular_volume_series_count"] or 0,
                    "geometry_not_checked_count": row["geometry_not_checked_series_count"] or 0,
                    "geometry_source": (
                        "idc-index-data:volume_geometry_index"
                        if "geometry_statuses" in idc_columns
                        else "not_assessed"
                    ),
                    "study_count": row["study_count"],
                    "series_count": row["series_count"],
                    "file_count": None,
                    "known_size_bytes": None,
                    "has_file_level_metadata": 0,
                    "detail_pointer": (
                        f"crdc_idc:{dataset_type}:{short_title}:{raw_id}"
                    ),
                    "access_route": "",
                    "inventory_status": (
                        "participant_presence_known_query_idc_for_detail"
                    ),
                    "source_version": row["idc_version"] or "",
                    "provenance_json": json_dumps(
                        {
                            "source_view": "agent_idc_dataset_participants",
                            "source_collection_ids": json.loads(
                                row["source_collection_ids_json"] or "[]"
                            ),
                            "source_analysis_result_ids": json.loads(
                                row["source_analysis_result_ids_json"] or "[]"
                            ),
                        }
                    ),
                },
            )
            imported += 1
    record_source(
        conn,
        "idc_participant_projection",
        path,
        True,
        imported,
        "Compact Collection and Analysis Result participant presence imported; IDC remains authoritative for public DICOM detail.",
    )
    return imported


def resolve_within_dataset_identifiers(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT p.participant_key, p.dataset_type, p.short_title,
               p.display_participant_id,
               COUNT(DISTINCT i.identifier_namespace) AS namespace_count,
               COUNT(DISTINCT i.normalized_identifier) AS spelling_count
        FROM participant_identifiers i
        JOIN participants p USING(participant_key)
        WHERE i.link_evidence <> 'official_tcia_brats2021_workbook'
        GROUP BY p.participant_key, p.dataset_type, p.short_title,
                 p.display_participant_id
        HAVING COUNT(DISTINCT i.identifier_namespace) > 1
            OR COUNT(DISTINCT i.normalized_identifier) > 1
        """
    ).fetchall()
    target_keys = {str(row["participant_key"]) for row in rows}
    identifier_provenance: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for item in conn.execute(
        """
        SELECT participant_key, identifier_namespace, managed_system, normalized_identifier
        FROM participant_identifiers
        ORDER BY participant_key, identifier_namespace, managed_system
        """
    ):
        key = str(item["participant_key"])
        if key in target_keys:
            identifier_provenance[key].add(
                (
                    str(item["identifier_namespace"]),
                    str(item["managed_system"]),
                    str(item["normalized_identifier"]),
                )
            )
    resolution_counts = {
        "exact_cross_namespace_resolutions": 0,
        "casefolded_identifier_resolutions": 0,
    }
    for row in rows:
        provenance = identifier_provenance[str(row["participant_key"])]
        namespaces = sorted({namespace for namespace, _, _ in provenance})
        managed_systems = sorted({system for _, system, _ in provenance})
        identifier_spellings = sorted({identifier for _, _, identifier in provenance})
        exact_text_match = len(identifier_spellings) == 1
        resolution_method = (
            "exact_identifier_same_tcia_dataset"
            if exact_text_match
            else "casefolded_identifier_same_tcia_dataset"
        )
        count_key = (
            "exact_cross_namespace_resolutions"
            if exact_text_match
            else "casefolded_identifier_resolutions"
        )
        resolution_counts[count_key] += 1
        source_scope = (
            "multiple source namespaces"
            if row["namespace_count"] > 1
            else "multiple managed-system records in one source namespace"
        )
        description = (
            f"Exact identifier text from {source_scope} was linked"
            if exact_text_match
            else f"Case-equivalent identifier spellings from {source_scope} were linked"
        )
        conn.execute(
            """
            UPDATE participants
            SET within_dataset_identity_status = 'resolved',
                identity_resolution_method = ?
            WHERE participant_key = ?
            """,
            (resolution_method, row["participant_key"]),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO participant_identity_evidence
            VALUES (?, ?, 'within_dataset', ?,
                    'resolved', 'high', ?, ?)
            """,
            (
                stable_id(
                    "identity_evidence",
                    row["participant_key"],
                    resolution_method,
                    *namespaces,
                ),
                row["participant_key"],
                resolution_method,
                f"{description} because every record resolves to the same "
                "authoritative TCIA dataset.",
                json_dumps({
                    "dataset_type": row["dataset_type"],
                    "short_title": row["short_title"],
                    "display_participant_id": row["display_participant_id"],
                    "identifier_spellings": identifier_spellings,
                    "namespace_count": row["namespace_count"],
                    "spelling_count": row["spelling_count"],
                    "namespaces": namespaces,
                    "managed_systems": managed_systems,
                }),
            ),
        )
    return resolution_counts


def build_database(
    out: Path,
    *,
    snapshot_db: Path,
    public_db: Path,
    controlled_db: Path,
    clinical_db: Path,
    replace: bool,
    public_audit_db: Path | None = None,
    idc_db: Path = DEFAULT_IDC_DB,
    include_clinical_values: bool = False,
) -> dict[str, Any]:
    dataset_types = load_dataset_types(snapshot_db)
    if out.exists():
        if not replace:
            raise FileExistsError(f"Output exists: {out}; pass --replace")
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(out)) as conn:
        conn.executescript(SCHEMA)
        record_source(
            conn,
            "tcia_snapshot",
            snapshot_db,
            True,
            len(dataset_types),
            "Authoritative WordPress dataset type and short-title identities imported.",
        )
        counts = {
            "public_non_dicom": ingest_public_non_dicom(
                conn, public_db, dataset_types, audit_path=public_audit_db
            ),
            "controlled": ingest_controlled(conn, controlled_db, dataset_types),
            "idc_participants": ingest_idc_participants(conn, idc_db, dataset_types),
            "clinical": ingest_clinical(
                conn,
                clinical_db,
                dataset_types,
                include_clinical_values=include_clinical_values,
            ),
        }
        brats_links = apply_brats_source_collection_links(conn)
        counts.update({f"brats_{key}": value for key, value in brats_links.items()})
        counts["display_identifiers_selected"] = select_display_participant_ids(conn)
        resolution_counts = resolve_within_dataset_identifiers(conn)
        counts.update(resolution_counts)
        counts["within_dataset_identity_resolutions"] = sum(resolution_counts.values())
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "artifact_model_schema_version": MODEL_SCHEMA_VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "identity_rule": (
                "Case-equivalent identifiers from sources attached to the same authoritative "
                "TCIA dataset resolve to one participant while every source spelling is "
                "preserved; cross-dataset identity is never asserted from a repeated bare "
                "identifier."
            ),
            "display_identifier_rule": (
                "Prefer TCIA-origin identifier spelling, then other managed-system sources, "
                "with deterministic case and lexical tie-breakers."
            ),
            "clinical_values_storage": (
                "embedded" if include_clinical_values else "clinical_metadata_detail_artifact"
            ),
        }
        conn.executemany(
            "INSERT INTO participant_inventory_meta VALUES (?, ?)",
            [(key, str(value)) for key, value in metadata.items()],
        )
        conn.commit()
        conn.execute("ANALYZE")
        counts.update({
            "participants": conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0],
            "participant_assets": conn.execute("SELECT COUNT(*) FROM participant_assets").fetchone()[0],
            "identifiers": conn.execute("SELECT COUNT(*) FROM participant_identifiers").fetchone()[0],
            "unlinked_dataset_assets": conn.execute("SELECT COUNT(*) FROM dataset_assets_without_participant_crosswalk").fetchone()[0],
        })
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    return {"path": str(out), "schema_version": SCHEMA_VERSION, "integrity_check": integrity, "counts": counts}


def validate_database(
    path: Path,
    *,
    minimum_collection_participants: int = 0,
    minimum_collections: int = 0,
    minimum_analysis_result_participants: int = 0,
    minimum_analysis_results: int = 0,
) -> dict[str, Any]:
    required = {
        "participants", "participant_identifiers", "participant_assets",
        "participant_clinical_values", "participant_identity_evidence",
        "dataset_assets_without_participant_crosswalk", "participant_inventory_sources",
        "agent_participants", "agent_participant_assets", "agent_participant_identifiers",
        "agent_participant_clinical_values", "agent_participant_identity_evidence",
        "agent_participant_search",
    }
    errors: list[str] = []
    with closing(connect(path)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"integrity_check={integrity}")
        objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        missing = sorted(required - objects)
        if missing:
            errors.append("missing objects: " + ", ".join(missing))
        cross_dataset = conn.execute(
            """
            SELECT COUNT(*) FROM participants p
            WHERE p.cross_dataset_identity_status <> 'not_asserted'
              AND NOT EXISTS (
                SELECT 1 FROM participant_identity_evidence e
                WHERE e.participant_key = p.participant_key
                  AND e.resolution_scope = 'cross_dataset'
              )
            """
        ).fetchone()[0]
        if cross_dataset:
            errors.append(f"cross-dataset identities without evidence: {cross_dataset}")
        duplicate_participants = conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT dataset_type, short_title, casefold(display_participant_id)
              FROM participants
              GROUP BY dataset_type, short_title, casefold(display_participant_id)
              HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        if duplicate_participants:
            errors.append(
                "case-equivalent canonical participant display identifiers within one dataset: "
                f"{duplicate_participants}"
            )
        canonical_key_mismatches = sum(
            1
            for row in conn.execute(
                "SELECT participant_key, dataset_type, short_title, display_participant_id "
                "FROM participants"
            )
            if str(row["participant_key"])
            != participant_key(
                str(row["dataset_type"]),
                str(row["short_title"]),
                str(row["display_participant_id"]),
            )
        )
        if canonical_key_mismatches:
            errors.append(
                "participant keys inconsistent with dataset-scoped casefold identity rule: "
                f"{canonical_key_mismatches}"
            )
        identifier_case_mismatches = conn.execute(
            """
            SELECT COUNT(*)
            FROM participant_identifiers i
            JOIN participants p USING(participant_key)
            WHERE casefold(i.normalized_identifier) <> casefold(p.display_participant_id)
              AND i.link_evidence <> 'official_tcia_brats2021_workbook'
            """
        ).fetchone()[0]
        if identifier_case_mismatches:
            errors.append(
                "participant identifiers outside their canonical casefold group: "
                f"{identifier_case_mismatches}"
            )
        unresolved_multiple_namespaces = conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT p.participant_key
              FROM participants p
              JOIN participant_identifiers i USING(participant_key)
              GROUP BY p.participant_key, p.within_dataset_identity_status
              HAVING COUNT(DISTINCT i.identifier_namespace) > 1
                 AND p.within_dataset_identity_status <> 'resolved'
                 AND NOT EXISTS (
                   SELECT 1 FROM participant_identity_evidence e
                   WHERE e.participant_key = p.participant_key
                     AND e.resolution_method = 'official_tcia_brats2021_workbook'
                 )
            )
            """
        ).fetchone()[0]
        if unresolved_multiple_namespaces:
            errors.append(
                "multi-namespace participants without resolved identity evidence: "
                f"{unresolved_multiple_namespaces}"
            )
        orphan_assets = conn.execute(
            "SELECT COUNT(*) FROM participant_assets a LEFT JOIN participants p USING(participant_key) WHERE p.participant_key IS NULL"
        ).fetchone()[0]
        if orphan_assets:
            errors.append(f"orphan participant assets: {orphan_assets}")
        counts = {
            "participants": conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0],
            "participant_assets": conn.execute("SELECT COUNT(*) FROM participant_assets").fetchone()[0],
            "unlinked_dataset_assets": conn.execute("SELECT COUNT(*) FROM dataset_assets_without_participant_crosswalk").fetchone()[0],
            "clinical_values": conn.execute("SELECT COUNT(*) FROM participant_clinical_values").fetchone()[0],
            "identity_resolutions": conn.execute(
                "SELECT COUNT(*) FROM participant_identity_evidence WHERE status = 'resolved'"
            ).fetchone()[0],
            "collection_participants": conn.execute(
                "SELECT COUNT(*) FROM participants WHERE dataset_type='Collection'"
            ).fetchone()[0],
            "collections": conn.execute(
                "SELECT COUNT(DISTINCT short_title) FROM participants "
                "WHERE dataset_type='Collection'"
            ).fetchone()[0],
            "analysis_result_participants": conn.execute(
                "SELECT COUNT(*) FROM participants WHERE dataset_type='Analysis Result'"
            ).fetchone()[0],
            "analysis_results": conn.execute(
                "SELECT COUNT(DISTINCT short_title) FROM participants "
                "WHERE dataset_type='Analysis Result'"
            ).fetchone()[0],
        }
        if counts["participants"] > 100000:
            brats_counts = {
                "brats_participants": conn.execute(
                    "SELECT COUNT(*) FROM participants WHERE dataset_type='Analysis Result' AND short_title=?",
                    (BRATS_SHORT_TITLE,),
                ).fetchone()[0],
                "brats_source_display_ids": conn.execute(
                    "SELECT COUNT(*) FROM participants WHERE dataset_type='Analysis Result' AND short_title=? AND display_participant_id NOT LIKE 'BraTS2021_%'",
                    (BRATS_SHORT_TITLE,),
                ).fetchone()[0],
                "brats_challenge_display_ids": conn.execute(
                    "SELECT COUNT(*) FROM participants WHERE dataset_type='Analysis Result' AND short_title=? AND display_participant_id LIKE 'BraTS2021_%'",
                    (BRATS_SHORT_TITLE,),
                ).fetchone()[0],
                "brats_alternate_challenge_aliases": conn.execute(
                    "SELECT COUNT(*) FROM participant_identifiers i JOIN participants p USING(participant_key) WHERE p.short_title=? AND i.identifier_namespace='challenge:BraTS2021'",
                    (BRATS_SHORT_TITLE,),
                ).fetchone()[0],
                "brats_linked_source_collection": conn.execute(
                    "SELECT COUNT(*) FROM participants WHERE short_title=? AND cross_dataset_identity_status='linked_source_collection'",
                    (BRATS_SHORT_TITLE,),
                ).fetchone()[0],
                "brats_source_participant_not_current": conn.execute(
                    "SELECT COUNT(*) FROM participants WHERE short_title=? AND cross_dataset_identity_status='source_participant_not_current'",
                    (BRATS_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(brats_counts)
            expected_brats = {
                "brats_participants": 1479,
                "brats_source_display_ids": 1066,
                "brats_challenge_display_ids": 413,
                "brats_alternate_challenge_aliases": 1066,
                "brats_linked_source_collection": 1060,
                "brats_source_participant_not_current": 6,
            }
            for name, expected in expected_brats.items():
                if brats_counts[name] != expected:
                    errors.append(
                        f"BraTS participant coverage regression: {name}={brats_counts[name]} != {expected}"
                    )
            bcbm_counts = {
                "bcbm_participants": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title = ?",
                    (BCBM_SHORT_TITLE,),
                ).fetchone()[0],
                "bcbm_with_public_non_dicom": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title = ? AND has_public_non_dicom = 1",
                    (BCBM_SHORT_TITLE,),
                ).fetchone()[0],
                "bcbm_with_clinical": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title = ? AND has_clinical = 1",
                    (BCBM_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(bcbm_counts)
            for name, expected in {
                "bcbm_participants": 165,
                "bcbm_with_public_non_dicom": 165,
                "bcbm_with_clinical": 165,
            }.items():
                if bcbm_counts[name] != expected:
                    errors.append(
                        f"BCBM participant coverage regression: {name}={bcbm_counts[name]} != {expected}"
                    )
            tcga_gbm_qi_counts = {
                "tcga_gbm_qi_participants": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title=?",
                    ("TCGA-GBM-QI-Radiogenomics",),
                ).fetchone()[0],
                "tcga_gbm_qi_with_public_non_dicom": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title=? "
                    "AND has_public_non_dicom=1",
                    ("TCGA-GBM-QI-Radiogenomics",),
                ).fetchone()[0],
                "tcga_gbm_qi_unlinked_xml_assets": conn.execute(
                    "SELECT COUNT(*) FROM dataset_assets_without_participant_crosswalk "
                    "WHERE short_title=? AND upper(file_format)='XML'",
                    ("TCGA-GBM-QI-Radiogenomics",),
                ).fetchone()[0],
            }
            counts.update(tcga_gbm_qi_counts)
            for name, expected in {
                "tcga_gbm_qi_participants": 55,
                "tcga_gbm_qi_with_public_non_dicom": 55,
                "tcga_gbm_qi_unlinked_xml_assets": 0,
            }.items():
                if tcga_gbm_qi_counts[name] != expected:
                    errors.append(
                        "TCGA-GBM-QI-Radiogenomics participant coverage regression: "
                        f"{name}={tcga_gbm_qi_counts[name]} != {expected}"
                    )
            reviewed_analysis_result_expected = {
                "DICOM-Glioma-SEG": 167,
                "ISBI-MR-Prostate-2013": 80,
                "LIDC-annot-NLST501": 501,
                "MRQy-Quality-Measures": 233,
                "TCGA-KIRC-Radiogenomics": 103,
                "TCGA-OV-Proteogenomics": 20,
                "TCGA-OV-Radiogenomics": 93,
            }
            for short_title, expected in reviewed_analysis_result_expected.items():
                participant_count = conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title=?",
                    (short_title,),
                ).fetchone()[0]
                public_count = conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title=? "
                    "AND has_public_non_dicom=1",
                    (short_title,),
                ).fetchone()[0]
                key = re.sub(r"[^a-z0-9]+", "_", short_title.casefold()).strip("_")
                counts[f"{key}_participants"] = participant_count
                counts[f"{key}_with_public_non_dicom"] = public_count
                if participant_count != expected or public_count != expected:
                    errors.append(
                        f"{short_title} participant coverage regression: "
                        f"participants={participant_count}, public_non_dicom={public_count}, "
                        f"expected={expected}"
                    )
            cptac_codex_counts = {
                "cptac_gbm_codex_participants": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title=?",
                    (CPTAC_GBM_CODEX_SHORT_TITLE,),
                ).fetchone()[0],
                "cptac_gbm_codex_with_public_non_dicom": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title=? "
                    "AND has_public_non_dicom=1",
                    (CPTAC_GBM_CODEX_SHORT_TITLE,),
                ).fetchone()[0],
                "cptac_gbm_codex_with_clinical": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title=? "
                    "AND has_clinical=1",
                    (CPTAC_GBM_CODEX_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(cptac_codex_counts)
            for name, expected in {
                "cptac_gbm_codex_participants": 12,
                "cptac_gbm_codex_with_public_non_dicom": 12,
                "cptac_gbm_codex_with_clinical": 12,
            }.items():
                if cptac_codex_counts[name] != expected:
                    errors.append(
                        "CPTAC-Glioblastoma-CODEX participant coverage regression: "
                        f"{name}={cptac_codex_counts[name]} != {expected}"
                    )
            brain_tr_counts = {
                "brain_tr_gammaknife_participants": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title = ?",
                    (BRAIN_TR_GAMMAKNIFE_SHORT_TITLE,),
                ).fetchone()[0],
                "brain_tr_gammaknife_with_controlled": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title = ? AND has_controlled_data = 1",
                    (BRAIN_TR_GAMMAKNIFE_SHORT_TITLE,),
                ).fetchone()[0],
                "brain_tr_gammaknife_with_clinical": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title = ? AND has_clinical = 1",
                    (BRAIN_TR_GAMMAKNIFE_SHORT_TITLE,),
                ).fetchone()[0],
                "brain_tr_gammaknife_with_current_public_dicom": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title = ? AND has_public_dicom = 1",
                    (BRAIN_TR_GAMMAKNIFE_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(brain_tr_counts)
            for name, expected in {
                "brain_tr_gammaknife_participants": 47,
                "brain_tr_gammaknife_with_controlled": 47,
                "brain_tr_gammaknife_with_clinical": 47,
                "brain_tr_gammaknife_with_current_public_dicom": 0,
            }.items():
                if brain_tr_counts[name] != expected:
                    errors.append(
                        "Brain-TR-GammaKnife participant coverage regression: "
                        f"{name}={brain_tr_counts[name]} != {expected}"
                    )
            remind_counts = {
                "remind_participants": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title = ?",
                    (REMIND_SHORT_TITLE,),
                ).fetchone()[0],
                "remind_with_public_dicom": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title = ? AND has_public_dicom = 1",
                    (REMIND_SHORT_TITLE,),
                ).fetchone()[0],
                "remind_with_clinical": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title = ? AND has_clinical = 1",
                    (REMIND_SHORT_TITLE,),
                ).fetchone()[0],
                "remind_with_public_non_dicom": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants "
                    "WHERE short_title = ? AND has_public_non_dicom = 1",
                    (REMIND_SHORT_TITLE,),
                ).fetchone()[0],
                "remind_unlinked_public_nrrd_assets": conn.execute(
                    "SELECT COUNT(*) FROM dataset_assets_without_participant_crosswalk "
                    "WHERE short_title = ? AND upper(file_format) = 'NRRD'",
                    (REMIND_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(remind_counts)
            for name, expected in {
                "remind_participants": 114,
                "remind_with_public_dicom": 114,
                "remind_with_clinical": 114,
                "remind_with_public_non_dicom": 114,
                "remind_unlinked_public_nrrd_assets": 0,
            }.items():
                if remind_counts[name] != expected:
                    errors.append(
                        f"ReMIND participant coverage regression: {name}={remind_counts[name]} != {expected}"
                    )
            tcga_lgg_mask_counts = {
                "tcga_lgg_mask_participants": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants WHERE short_title = ?",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_lgg_mask_with_public_non_dicom": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants "
                    "WHERE short_title = ? AND has_public_non_dicom = 1",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_lgg_mask_with_clinical": conn.execute(
                    "SELECT COUNT(*) FROM agent_participants "
                    "WHERE short_title = ? AND has_clinical = 1",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_lgg_mask_matlab_files": conn.execute(
                    # The participant summary counts the one shared VASARI CSV
                    # once for each of 188 participants. Subtract that one
                    # shared-file link per participant to recover mask files.
                    "SELECT COALESCE(SUM(file_count), 0) - COUNT(*) "
                    "FROM agent_participant_assets "
                    "WHERE short_title = ? "
                    "AND source_artifact = 'public_non_dicom_metadata'",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_lgg_mask_matlab_participants": conn.execute(
                    "SELECT COUNT(DISTINCT participant_key) FROM agent_participant_assets "
                    "WHERE short_title = ? "
                    "AND instr(',' || upper(file_format) || ',', ',MATLAB,') > 0 "
                    "AND instr(',' || object_role || ',', ',segmentation,') > 0",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(tcga_lgg_mask_counts)
            for name, expected in {
                "tcga_lgg_mask_participants": 188,
                "tcga_lgg_mask_with_public_non_dicom": 188,
                "tcga_lgg_mask_with_clinical": 188,
                "tcga_lgg_mask_matlab_files": 406,
                "tcga_lgg_mask_matlab_participants": 108,
            }.items():
                if tcga_lgg_mask_counts[name] != expected:
                    errors.append(
                        "TCGA-LGG-Mask participant coverage regression: "
                        f"{name}={tcga_lgg_mask_counts[name]} != {expected}"
                    )
        thresholds = {
            "collection_participants": minimum_collection_participants,
            "collections": minimum_collections,
            "analysis_result_participants": minimum_analysis_result_participants,
            "analysis_results": minimum_analysis_results,
        }
        for name, minimum in thresholds.items():
            if counts[name] < minimum:
                errors.append(
                    f"{name} coverage regression: {counts[name]} < required {minimum}"
                )
    return {"ok": not errors, "errors": errors, "integrity_check": integrity, "counts": counts}


def gzip_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_handle, gzip.open(target, "wb", compresslevel=6) as output_handle:
        shutil.copyfileobj(input_handle, output_handle)


def build_manifest(db: Path, gzip_path: Path | None = None) -> dict[str, Any]:
    validation = validate_database(db)
    if not validation["ok"]:
        raise RuntimeError("Cannot manifest invalid database: " + "; ".join(validation["errors"]))
    result: dict[str, Any] = {
        "artifact_family": "tcia-metadata-v2",
        "artifact": "participant_inventory",
        "asset": "participant_inventory.sqlite.gz",
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sqlite_bytes": db.stat().st_size,
        "sqlite_sha256": file_sha256(db),
        "counts": validation["counts"],
    }
    with closing(connect(db)) as conn:
        build_contract = dict(
            conn.execute(
                "SELECT key, value FROM participant_inventory_meta WHERE key IN "
                "('clinical_values_storage', 'provenance_storage', "
                "'audit_companion_asset', 'audit_schema_version')"
            )
        )
    if build_contract:
        result["storage_contract"] = build_contract
    if gzip_path:
        result["gzip_bytes"] = gzip_path.stat().st_size
        result["gzip_sha256"] = file_sha256(gzip_path)
    result["release_fingerprint"] = hashlib.sha256(json_dumps(result).encode()).hexdigest()
    return result


def github_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "tcia-query-skill"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "tcia-query-skill"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def ensure_release(repo: str, tag: str, db: Path, manifest_path: Path) -> dict[str, Any]:
    release = github_json(
        f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    )
    assets = {asset["name"]: asset for asset in release.get("assets") or []}
    missing = [name for name in (DB_ASSET, MANIFEST_ASSET) if name not in assets]
    if missing:
        raise RuntimeError(f"Release {repo}@{tag} is missing: {', '.join(missing)}")
    manifest_body = fetch_bytes(assets[MANIFEST_ASSET]["browser_download_url"])
    remote = json.loads(manifest_body)
    if db.exists() and manifest_path.exists():
        try:
            local = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            local = {}
        if (
            local.get("release_fingerprint") == remote.get("release_fingerprint")
            and file_sha256(db) == remote.get("sqlite_sha256")
        ):
            manifest_path.write_bytes(manifest_body)
            return {"status": "unchanged", "manifest": remote}
    compressed = fetch_bytes(assets[DB_ASSET]["browser_download_url"])
    if hashlib.sha256(compressed).hexdigest() != remote.get("gzip_sha256"):
        raise RuntimeError("Downloaded Participant Inventory gzip SHA-256 mismatch")
    raw = gzip.decompress(compressed)
    if hashlib.sha256(raw).hexdigest() != remote.get("sqlite_sha256"):
        raise RuntimeError("Downloaded Participant Inventory SQLite SHA-256 mismatch")
    db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=db.parent, delete=False) as handle:
        handle.write(raw)
        temporary = Path(handle.name)
    os.replace(temporary, db)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_body)
    return {"status": "downloaded", "manifest": remote}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--snapshot-db", default=str(DEFAULT_SNAPSHOT_DB))
    build.add_argument("--public-db", default=str(DEFAULT_PUBLIC_DB))
    build.add_argument("--public-audit-db")
    build.add_argument("--controlled-db", default=str(DEFAULT_CONTROLLED_DB))
    build.add_argument("--clinical-db", default=str(DEFAULT_CLINICAL_DB))
    build.add_argument("--idc-db", default=str(DEFAULT_IDC_DB))
    build.add_argument(
        "--staging-db",
        help=(
            "Resolve snapshot, controlled-access, clinical, and IDC participant "
            "inputs from the canonical runner-local V2 staging ledger."
        ),
    )
    build.add_argument("--out", default=str(DEFAULT_DB))
    build.add_argument("--gzip-out")
    build.add_argument("--manifest-out")
    build.add_argument("--replace", action="store_true")
    build.add_argument(
        "--embed-clinical-values",
        action="store_true",
        help="Embed all accepted clinical facts for compatibility; research-core builds defer them to clinical_metadata.",
    )
    validate = sub.add_parser("validate")
    validate.add_argument("--db", default=str(DEFAULT_DB))
    validate.add_argument("--min-collection-participants", type=int, default=0)
    validate.add_argument("--min-collections", type=int, default=0)
    validate.add_argument("--min-analysis-result-participants", type=int, default=0)
    validate.add_argument("--min-analysis-results", type=int, default=0)
    info = sub.add_parser("info")
    info.add_argument("--db", default=str(DEFAULT_DB))
    ensure = sub.add_parser("ensure")
    ensure.add_argument("--repo", default=DEFAULT_REPOSITORY)
    ensure.add_argument("--tag", default=DEFAULT_RELEASE_TAG)
    ensure.add_argument("--db", default=str(DEFAULT_DB))
    ensure.add_argument("--manifest-out", default=str(DEFAULT_MANIFEST))
    participants = sub.add_parser("participants")
    participants.add_argument("--db", default=str(DEFAULT_DB))
    participants.add_argument("--collection")
    participants.add_argument("--participant")
    participants.add_argument("--limit", type=int, default=100)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "build":
        out = Path(args.out)
        staging_db = Path(args.staging_db) if args.staging_db else None
        snapshot_db = Path(args.snapshot_db)
        controlled_db = Path(args.controlled_db)
        clinical_db = Path(args.clinical_db)
        idc_db = Path(args.idc_db)
        if staging_db:
            snapshot_db = resolve_staging_component(staging_db, "snapshot", verify_hash=False)
            controlled_db = resolve_staging_component(
                staging_db, "controlled_access", verify_hash=False
            )
            clinical_db = resolve_staging_component(staging_db, "clinical", verify_hash=False)
            idc_db = resolve_staging_component(
                staging_db, "idc_participants", verify_hash=False
            )
        result = build_database(
            out,
            snapshot_db=snapshot_db,
            public_db=Path(args.public_db),
            public_audit_db=(Path(args.public_audit_db) if args.public_audit_db else None),
            controlled_db=controlled_db,
            clinical_db=clinical_db,
            idc_db=idc_db,
            replace=args.replace,
            include_clinical_values=args.embed_clinical_values,
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
        result = validate_database(
            Path(args.db),
            minimum_collection_participants=args.min_collection_participants,
            minimum_collections=args.min_collections,
            minimum_analysis_result_participants=args.min_analysis_result_participants,
            minimum_analysis_results=args.min_analysis_results,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command == "ensure":
        print(json.dumps(
            ensure_release(args.repo, args.tag, Path(args.db), Path(args.manifest_out)),
            indent=2,
            sort_keys=True,
        ))
        return 0
    with closing(connect(Path(args.db))) as conn:
        if args.command == "info":
            result = {
                "meta": {row["key"]: row["value"] for row in conn.execute("SELECT * FROM participant_inventory_meta")},
                "sources": [dict(row) for row in conn.execute("SELECT * FROM participant_inventory_sources ORDER BY source_name")],
                "validation": validate_database(Path(args.db)),
            }
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            sql = "SELECT * FROM agent_participants WHERE 1 = 1"
            params: list[Any] = []
            if args.collection:
                sql += " AND lower(short_title) = lower(?)"
                params.append(args.collection)
            if args.participant:
                sql += (
                    " AND (casefold(display_participant_id) = casefold(?) OR EXISTS ("
                    "SELECT 1 FROM participant_identifiers i "
                    "WHERE i.participant_key = agent_participants.participant_key "
                    "AND casefold(i.raw_identifier) = casefold(?)))"
                )
                params.extend([args.participant, args.participant])
            sql += " ORDER BY lower(short_title), display_participant_id LIMIT ?"
            params.append(args.limit)
            for row in conn.execute(sql, params):
                print(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
