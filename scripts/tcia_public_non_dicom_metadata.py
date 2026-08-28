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
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from tcia_v2_staging import resolve_component as resolve_staging_component
except ModuleNotFoundError:
    from scripts.tcia_v2_staging import resolve_component as resolve_staging_component

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
DEFAULT_CLINICAL_DB = SKILL_ROOT / "cache" / "clinical_metadata.sqlite"
DEFAULT_CROSSWALK_CSV = SKILL_ROOT / "references" / "public_non_dicom_crosswalks_v1.csv"
DEFAULT_CROSSWALK_CURATION = SKILL_ROOT / "references" / "public-non-dicom-crosswalk-curation-v1.json"
DEFAULT_BRATS_CROSSWALK_CSV = SKILL_ROOT / "references" / "brats2021_tcia_crosswalk_v1.csv"
DEFAULT_BRATS_CROSSWALK_PROVENANCE = SKILL_ROOT / "references" / "brats2021_tcia_crosswalk_v1.json"
DEFAULT_IMAGE_METADATA_CSV = SKILL_ROOT / "references" / "public_non_dicom_image_metadata_v1.csv"
DEFAULT_REMIND_NRRD_INVENTORY = SKILL_ROOT / "references" / "remind_nrrd_inventory_v1.sums"
DEFAULT_REMIND_NRRD_PROVENANCE = SKILL_ROOT / "references" / "remind_nrrd_inventory_v1.json"
DEFAULT_TCGA_LGG_MASK_INVENTORY = SKILL_ROOT / "references" / "tcga_lgg_mask_inventory_v1.csv"
DEFAULT_TCGA_LGG_MASK_VASARI = SKILL_ROOT / "references" / "tcga_lgg_mask_vasari_participants_v1.csv"
DEFAULT_TCGA_LGG_MASK_PROVENANCE = SKILL_ROOT / "references" / "tcga_lgg_mask_inventory_v1.json"
DEFAULT_CPTAC_GBM_CODEX_INVENTORY = SKILL_ROOT / "references" / "cptac_gbm_codex_inventory_v1.csv"
DEFAULT_CPTAC_GBM_CODEX_PROVENANCE = SKILL_ROOT / "references" / "cptac_gbm_codex_inventory_v1.json"
DEFAULT_TCGA_GBM_QI_AIM_INVENTORY = SKILL_ROOT / "references" / "tcga_gbm_qi_radiogenomics_aim_inventory_v1.csv"
DEFAULT_TCGA_GBM_QI_AIM_PROVENANCE = SKILL_ROOT / "references" / "tcga_gbm_qi_radiogenomics_aim_inventory_v1.json"
DEFAULT_REVIEWED_PARTICIPANT_INVENTORY = SKILL_ROOT / "references" / "reviewed_analysis_result_participants_v1.csv"
DEFAULT_REVIEWED_PARTICIPANT_PROVENANCE = SKILL_ROOT / "references" / "reviewed_analysis_result_participants_v1.json"
DEFAULT_DB = SKILL_ROOT / "cache" / "public_non_dicom_metadata.sqlite"
DEFAULT_MANIFEST = SKILL_ROOT / "cache" / "public_non_dicom_metadata_manifest.json"
DEFAULT_RELEASE_TAG = "tcia-metadata-v2-latest"
DEFAULT_REPOSITORY = "kirbyju/tcia-query-skill"
DB_ASSET = "public_non_dicom_metadata.sqlite.gz"
MANIFEST_ASSET = "public_non_dicom_metadata_manifest.json"
SCHEMA_VERSION = 7

BRATS_SHORT_TITLE = "RSNA-ASNR-MICCAI-BraTS-2021"
BCBM_SHORT_TITLE = "BCBM-RadioGenomics"
REMIND_SHORT_TITLE = "ReMIND"
TCGA_LGG_MASK_SHORT_TITLE = "TCGA-LGG-Mask"
CPTAC_GBM_CODEX_SHORT_TITLE = "CPTAC-Glioblastoma-CODEX"
TCGA_GBM_QI_SHORT_TITLE = "TCGA-GBM-QI-Radiogenomics"
BCBM_SCAN_ID = re.compile(r"^(BCBM-RadioGenomics-\d+)-(\d+)$", re.IGNORECASE)


def codex_workbook_file_key(value: str) -> str:
    """Return the extension-free key shared by the workbook and PathDB."""
    name = Path(str(value or "").strip()).name.casefold()
    for suffix in (".ome.tiff", ".ome.tif", ".qptiff", ".tiff", ".tif"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def codex_workbook_participants(values: dict[str, Any]) -> list[str]:
    """Project a CODEX workbook row to its parent patient identifier(s)."""
    upenn_id = str(values.get("UPENN-GBM_PatientID") or "").strip()
    cptac_id = str(values.get("CPTAC-GBM_PatientID") or "").strip()
    source = upenn_id or cptac_id
    return [item.strip() for item in source.split(",") if item.strip()]


def load_cptac_gbm_codex_inventory(
    inventory_path: Path = DEFAULT_CPTAC_GBM_CODEX_INVENTORY,
    provenance_path: Path = DEFAULT_CPTAC_GBM_CODEX_PROVENANCE,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not inventory_path.is_file() or not provenance_path.is_file():
        raise FileNotFoundError(
            "CPTAC-Glioblastoma-CODEX reviewed inventory references are required: "
            f"{inventory_path} and {provenance_path}"
        )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    actual_sha256 = file_sha256(inventory_path)
    expected_sha256 = str(provenance.get("inventory_sha256") or "")
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            "CPTAC-Glioblastoma-CODEX inventory digest mismatch: "
            f"{actual_sha256} != {expected_sha256}"
        )
    with inventory_path.open(newline="", encoding="utf-8") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if len(rows) != int(provenance.get("row_count") or 0):
        raise RuntimeError("CPTAC-Glioblastoma-CODEX inventory row-count mismatch")
    return rows, provenance


def load_tcga_gbm_qi_aim_inventory(
    inventory_path: Path = DEFAULT_TCGA_GBM_QI_AIM_INVENTORY,
    provenance_path: Path = DEFAULT_TCGA_GBM_QI_AIM_PROVENANCE,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not inventory_path.is_file() or not provenance_path.is_file():
        raise FileNotFoundError(
            "TCGA-GBM-QI-Radiogenomics reviewed AIM references are required: "
            f"{inventory_path} and {provenance_path}"
        )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    expected_sha256 = str(provenance.get("inventory_sha256") or "")
    actual_sha256 = file_sha256(inventory_path)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            "TCGA-GBM-QI-Radiogenomics AIM inventory digest mismatch: "
            f"{actual_sha256} != {expected_sha256}"
        )
    with inventory_path.open(newline="", encoding="utf-8") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    counts = provenance.get("counts") or {}
    expected = (
        int(counts.get("xml_files") or 0),
        int(counts.get("patients") or 0),
        int(counts.get("studies") or 0),
        int(counts.get("series") or 0),
        int(counts.get("sop_instances") or 0),
        int(counts.get("annotation_uids") or 0),
    )
    actual = (
        len(rows),
        len({row["patient_id"] for row in rows}),
        len({row["study_instance_uid"] for row in rows}),
        len({row["series_instance_uid"] for row in rows}),
        len({row["sop_instance_uid"] for row in rows}),
        len({row["annotation_uid"] for row in rows}),
    )
    if actual != expected or actual != (321, 55, 60, 111, 193, 321):
        raise RuntimeError(
            f"TCGA-GBM-QI-Radiogenomics AIM inventory mismatch: {actual} != {expected}"
        )
    return rows, provenance


def bcbm_patient_and_scan_id(package_path: str) -> tuple[str, str]:
    for part in re.split(r"[/\\]+", str(package_path or "")):
        match = BCBM_SCAN_ID.fullmatch(part.strip())
        if match:
            return match.group(1), part.strip()
    return "", ""


def load_brats2021_crosswalk(
    csv_path: Path = DEFAULT_BRATS_CROSSWALK_CSV,
    provenance_path: Path = DEFAULT_BRATS_CROSSWALK_PROVENANCE,
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """Load the reviewed, hash-pinned projection of the official TCIA workbook."""
    if not csv_path.exists() or not provenance_path.exists():
        raise FileNotFoundError(
            "BraTS 2021 crosswalk references are required: "
            f"{csv_path} and {provenance_path}"
        )
    metadata = json.loads(provenance_path.read_text())
    rows: dict[str, dict[str, str]] = {}
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            challenge_id = str(row.get("challenge_id") or "").strip()
            if not re.fullmatch(r"BraTS2021_\d{5}", challenge_id):
                raise ValueError(f"Invalid BraTS challenge identifier: {challenge_id!r}")
            if challenge_id in rows:
                raise ValueError(f"Duplicate BraTS challenge identifier: {challenge_id}")
            rows[challenge_id] = {key: str(value or "").strip() for key, value in row.items()}
    expected = int(metadata.get("row_count") or 0)
    if expected and len(rows) != expected:
        raise ValueError(f"BraTS crosswalk row-count mismatch: {len(rows)} != {expected}")
    return rows, metadata


def brats_participant_projection(
    challenge_id: str,
    crosswalk: dict[str, dict[str, str]],
    provenance: dict[str, Any],
) -> tuple[str, str, str, dict[str, Any]]:
    row = crosswalk.get(challenge_id)
    if not row:
        raise ValueError(f"BraTS asset is absent from the reviewed crosswalk: {challenge_id}")
    source_collection = row.get("source_collection_short_title", "")
    resolved = row.get("resolved_source_patient_id", "")
    mapped = bool(source_collection and resolved)
    subject_id = resolved if mapped else challenge_id
    namespace = (
        f"tcia_collection:{source_collection}"
        if mapped
        else f"tcia_dataset:{BRATS_SHORT_TITLE}"
    )
    link_status = "reviewed_source_crosswalk" if mapped else "dataset_scoped_source_identifier"
    evidence = {
        "challenge_id": challenge_id,
        "workbook_row": int(row["workbook_row"]),
        "source_group": row.get("source_group", ""),
        "raw_tcia_patient_id": row.get("raw_tcia_patient_id", ""),
        "source_collection_short_title": source_collection,
        "resolved_source_patient_id": resolved,
        "mapping_status": row.get("mapping_status", ""),
        "evidence_url": provenance.get("source_url", ""),
        "evidence_sha256": provenance.get("source_sha256", ""),
    }
    return subject_id, namespace, link_status, evidence


def insert_brats_crosswalk_evidence(
    conn: sqlite3.Connection,
    asset_id: str,
    challenge_id: str,
    subject_id: str,
    evidence: dict[str, Any],
) -> None:
    if evidence.get("mapping_status") != "source_collection_identifier":
        return
    conn.execute(
        """
        INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
        VALUES (?, ?, ?, ?, ?, 'official_tcia_brats2021_workbook',
                'high', ?, ?, '', ?, ?)
        """,
        (
            stable_id("crosswalk", asset_id, challenge_id, subject_id),
            asset_id,
            BRATS_SHORT_TITLE,
            challenge_id,
            subject_id,
            evidence.get("evidence_url", ""),
            "Original TCIA Collection PatientID normalized without discarding the BraTS alias.",
            str(evidence.get("reviewed_at") or "2026-08-25"),
            json_dumps(evidence),
        ),
    )


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
    SUM(CASE WHEN asset_granularity IN ('file', 'participant_modality', 'participant_file_group')
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
    SUM(CASE WHEN a.asset_granularity IN ('file', 'participant_modality', 'participant_file_group')
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
          AND NOT EXISTS (
              SELECT 1
              FROM public_non_dicom_asset_participants existing
              WHERE existing.asset_id = public_non_dicom_assets.asset_id
                AND existing.subject_id = public_non_dicom_assets.subject_id
          )
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


def published_count(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def refresh_wordpress_aggregate_counts(
    conn: sqlite3.Connection, snapshot_db: Path
) -> int:
    """Apply published image counts to existing WordPress download assets."""
    before = conn.total_changes
    with closing(connect(snapshot_db)) as source:
        for row in source.execute(
            """
            SELECT dataset_type, short_title, download_id, images
            FROM agent_current_downloads
            WHERE hidden = 0 AND controlled_access = 0
              AND NULLIF(trim(COALESCE(download_id, '')), '') IS NOT NULL
            """
        ):
            count = published_count(row["images"])
            if count is None:
                continue
            conn.execute(
                """
                UPDATE public_non_dicom_assets
                SET represented_file_count = ?
                WHERE dataset_type = ? AND short_title = ? AND download_id = ?
                  AND asset_granularity = 'download'
                """,
                (count, row["dataset_type"], row["short_title"], str(row["download_id"])),
            )
    return conn.total_changes - before


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
                        "represented_file_count": published_count(row["images"]),
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
                            "published_subject_count": row["subjects"],
                            "published_image_count": row["images"],
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


def ingest_nifti(
    conn: sqlite3.Connection,
    nifti_db: Path,
    brats_crosswalk: dict[str, dict[str, str]],
    brats_provenance: dict[str, Any],
) -> int:
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
            raw_subject_id = str(row["subject_id"] or "").strip()
            subject_id = raw_subject_id
            namespace = f"tcia_dataset:{row['short_title']}"
            link_status = "dataset_scoped_source_identifier" if subject_id else "unavailable"
            brats_evidence: dict[str, Any] = {}
            if str(row["short_title"]).casefold() == BRATS_SHORT_TITLE.casefold() and raw_subject_id:
                subject_id, namespace, link_status, brats_evidence = brats_participant_projection(
                    raw_subject_id, brats_crosswalk, brats_provenance
                )
            bcbm_evidence: dict[str, Any] = {}
            if str(row["short_title"]).casefold() == BCBM_SHORT_TITLE.casefold():
                bcbm_patient_id, bcbm_scan_id = bcbm_patient_and_scan_id(package_path)
                if bcbm_patient_id:
                    raw_subject_id = bcbm_scan_id
                    subject_id = bcbm_patient_id
                    namespace = f"tcia_collection:{BCBM_SHORT_TITLE}"
                    link_status = "reviewed_patient_scan_projection"
                    bcbm_evidence = {
                        "source_scan_id": bcbm_scan_id,
                        "resolved_patient_id": bcbm_patient_id,
                        "mapping_method": "strip_final_numeric_scan_suffix",
                    }
            insert_asset(
                conn,
                {
                    "asset_id": asset_id,
                    "dataset_type": row["dataset_type"] or "Collection",
                    "short_title": row["short_title"],
                    "download_row_id": None,
                    "download_id": download_id,
                    "subject_id": subject_id,
                    "subject_id_namespace": namespace,
                    "participant_link_status": link_status,
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
                    "raw_values_json": json_dumps({"object_type": row["object_type"], "image_type": row["image_type"], "brats_crosswalk": brats_evidence, "bcbm_scan_projection": bcbm_evidence}),
                    "provenance_json": json_dumps({"source_artifact": "nifti_metadata", "source_view": "agent_nifti_files", "brats_crosswalk": brats_evidence, "bcbm_scan_projection": bcbm_evidence}),
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
                "procedure_id": bcbm_evidence.get("source_scan_id", ""),
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
            if brats_evidence:
                insert_asset_participant(
                    conn,
                    asset_id=asset_id,
                    short_title=str(row["short_title"]),
                    subject_id=subject_id,
                    namespace=namespace,
                    raw_subject_id=raw_subject_id,
                    participant_role="depicted_subject",
                    link_status=link_status,
                    evidence=brats_evidence,
                )
                insert_brats_crosswalk_evidence(
                    conn, asset_id, raw_subject_id, subject_id, brats_evidence
                )
            if bcbm_evidence:
                insert_asset_participant(
                    conn,
                    asset_id=asset_id,
                    short_title=str(row["short_title"]),
                    subject_id=subject_id,
                    namespace=namespace,
                    raw_subject_id=raw_subject_id,
                    participant_role="depicted_subject",
                    link_status=link_status,
                    evidence=bcbm_evidence,
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


def normalized_bcbm_filename(value: str) -> str:
    """Comparison key for reviewed punctuation-only BCBM name differences."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def bcbm_row_values(row_json: str) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = json.loads(row_json or "{}")
    return raw, {re.sub(r"[^a-z0-9]", "", str(key).casefold()): value for key, value in raw.items()}


def bcbm_pixel_spacing(value: Any) -> tuple[int | float | str | None, int | float | str | None]:
    numbers = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", str(value or ""))
    if len(numbers) < 2:
        return None, None
    return metadata_number(numbers[0]), metadata_number(numbers[1])


def ingest_bcbm_workbook_metadata(
    conn: sqlite3.Connection, clinical_db: Path | None
) -> dict[str, int]:
    """Join official BCBM scan metadata and radiomics to public NIfTI files."""
    counts = {
        "clinical_scan_rows": 0,
        "clinical_scan_assets_matched": 0,
        "radiomics_rows": 0,
        "radiomics_exact_matches": 0,
        "radiomics_normalized_matches": 0,
        "radiomics_unmatched_source_rows": 0,
        "radiomics_ambiguous_rows": 0,
        "radiomics_matched_assets": 0,
        "radiomics_feature_values": 0,
    }
    if not clinical_db or not clinical_db.exists():
        return counts
    assets_by_file: dict[str, list[str]] = defaultdict(list)
    assets_by_normalized_file: dict[str, list[str]] = defaultdict(list)
    for asset in conn.execute(
        """SELECT asset_id, file_name
           FROM public_non_dicom_assets
           WHERE short_title = ? AND asset_granularity = 'file'""",
        (BCBM_SHORT_TITLE,),
    ):
        file_name = str(asset["file_name"] or "")
        if not file_name:
            continue
        assets_by_file[file_name.casefold()].append(str(asset["asset_id"]))
        assets_by_normalized_file[normalized_bcbm_filename(file_name)].append(
            str(asset["asset_id"])
        )
    if not assets_by_file:
        return counts

    unmatched_examples: list[str] = []
    ambiguous_examples: list[str] = []
    matched_radiomics_assets: set[str] = set()
    with closing(connect(clinical_db)) as source:
        if not table_exists(source, "clinical_rows") or not table_exists(
            source, "clinical_sources"
        ):
            return counts
        rows = source.execute(
            """SELECT r.source_row_id, r.table_name, r.row_number, r.row_json,
                      r.row_sha256, s.source_id, s.source_url, s.artifact_sha256
               FROM clinical_rows r
               JOIN clinical_sources s USING(source_id)
               WHERE r.short_title = ?
                 AND s.source_kind = 'tcia_clinical_download'
               ORDER BY r.source_id, r.row_number""",
            (BCBM_SHORT_TITLE,),
        )
        for row in rows:
            raw, values = bcbm_row_values(str(row["row_json"] or "{}"))
            sheet_name = str(row["table_name"] or "").rsplit("::", 1)[-1].casefold()
            evidence = {
                "source_id": str(row["source_id"]),
                "source_row_id": str(row["source_row_id"]),
                "source_row_number": int(row["row_number"]),
                "row_sha256": str(row["row_sha256"] or ""),
                "artifact_sha256": str(row["artifact_sha256"] or ""),
            }
            source_locator = f"{row['source_url']}::{sheet_name}"
            if sheet_name == "clinical+genetics".casefold():
                counts["clinical_scan_rows"] += 1
                scan_id = str(values.get("id") or "").strip()
                file_name = f"{scan_id}_image_ss_n4.nii.gz"
                asset_ids = assets_by_file.get(file_name.casefold(), [])
                spacing_row, spacing_col = bcbm_pixel_spacing(values.get("pixelspacing"))
                for asset_id in asset_ids:
                    merge_image_metadata(
                        conn,
                        asset_id,
                        {
                            "procedure_id": scan_id,
                            "age_at_imaging_years": metadata_number(values.get("age")),
                            "acquisition_year": metadata_number(values.get("year")),
                            "pixel_spacing_row_mm": spacing_row,
                            "pixel_spacing_col_mm": spacing_col,
                            "manufacturer": values.get("manufacturer"),
                            "magnetic_field_strength_t": metadata_number(
                                values.get("magneticfieldstrengthid")
                            ),
                        },
                        value_role="normalized",
                        source_kind="tcia_clinical_download",
                        source_locator=source_locator,
                        inference_method="exact_scan_id_to_source_image_filename",
                        confidence="high",
                        priority=110,
                        evidence=evidence,
                    )
                    counts["clinical_scan_assets_matched"] += 1
                continue
            if sheet_name != "merged_orig".casefold():
                continue
            counts["radiomics_rows"] += 1
            prefix = str(values.get("filenameprefix") or "").strip()
            segmentation_name = str(values.get("segmentationname") or "").strip()
            file_name = f"{prefix}_{segmentation_name}.nii.gz"
            asset_ids = assets_by_file.get(file_name.casefold(), [])
            match_method = "exact_file_name"
            if len(asset_ids) == 1:
                counts["radiomics_exact_matches"] += 1
            else:
                asset_ids = assets_by_normalized_file.get(
                    normalized_bcbm_filename(file_name), []
                )
                match_method = "reviewed_punctuation_normalized_unique_file_name"
                if len(asset_ids) == 1:
                    counts["radiomics_normalized_matches"] += 1
                elif len(asset_ids) > 1:
                    counts["radiomics_ambiguous_rows"] += 1
                    if len(ambiguous_examples) < 8:
                        ambiguous_examples.append(file_name)
                    continue
                else:
                    counts["radiomics_unmatched_source_rows"] += 1
                    if len(unmatched_examples) < 8:
                        unmatched_examples.append(file_name)
                    continue
            features = {
                str(key): value
                for key, value in raw.items()
                if re.sub(r"[^a-z0-9]", "", str(key).casefold())
                not in {"filenameprefix", "segmentationname"}
                and meaningful_metadata_value(value)
            }
            for asset_id in asset_ids:
                merge_image_metadata(
                    conn,
                    asset_id,
                    {
                        "radiomics_filename_prefix": prefix,
                        "radiomics_segmentation_name": segmentation_name,
                        "radiomics_feature_count": len(features),
                        "radiomics_features": features,
                    },
                    value_role="source_raw",
                    source_kind="tcia_clinical_download",
                    source_locator=source_locator,
                    inference_method=match_method,
                    confidence="high",
                    priority=110,
                    evidence={**evidence, "constructed_file_name": file_name},
                )
                matched_radiomics_assets.add(asset_id)
                counts["radiomics_feature_values"] += len(features)
    counts["radiomics_matched_assets"] = len(matched_radiomics_assets)
    if counts["radiomics_unmatched_source_rows"] or counts["radiomics_ambiguous_rows"]:
        add_dataset_metadata_note(
            conn,
            BCBM_SHORT_TITLE,
            "radiomics_features",
            "official_radiomics_row_not_in_public_segmentation_inventory",
            "Official radiomics rows without a unique current public segmentation are retained as source-side QC exceptions.",
            severity="info",
            status="known_source_inventory_gap",
            affected_assets=counts["radiomics_unmatched_source_rows"] + counts["radiomics_ambiguous_rows"],
            evidence={
                **counts,
                "unmatched_file_examples": unmatched_examples,
                "ambiguous_file_examples": ambiguous_examples,
            },
        )
    return counts


BRATS_DICOM_PATH = re.compile(
    r"^(?P<root>.+/BraTS2021_(?P<cohort>Training|Validation)Set_dcm/"
    r"(?P<source_group>[^/]+)/(?P<raw_subject_id>\d{5})/(?P<modality>[^/]+))/"
    r"(?P<file_name>[^/]+\.dcm)$",
    re.IGNORECASE,
)

REMIND_NRRD_PATH = re.compile(
    r"(?:^|/)(?P<subject>ReMIND-\d{3})/"
    r"(?P=subject)-(?P<phase>preop|intraop)-SEG-"
    r"(?P<label>tumor_target|tumor_residual|previous_resection_cavity|"
    r"tumor|cerebrum|ventricles)-MR-(?P<sequence>.+)\.nrrd$",
    re.IGNORECASE,
)
EMPTY_FILE_MD5 = "d41d8cd98f00b204e9800998ecf8427e"


def ingest_aspera_public_dicom_exceptions(
    conn: sqlite3.Connection,
    nifti_db: Path,
    brats_crosswalk: dict[str, dict[str, str]],
    brats_provenance: dict[str, Any],
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
            challenge_id = str(item["subject_id"])
            subject_id, namespace, link_status, brats_evidence = brats_participant_projection(
                challenge_id, brats_crosswalk, brats_provenance
            )
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
                    "subject_id_namespace": namespace,
                    "participant_link_status": link_status,
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
                            "brats_crosswalk": brats_evidence,
                        }
                    ),
                    "provenance_json": json_dumps(
                        {
                            "source_artifact": "nifti_metadata",
                            "source_table": "aspera_root_sums_inventory",
                            "inventory_scope": "public_aspera_dicom_not_in_idc",
                            "identifier_method": "BraTS DICOM folder normalized to challenge ID",
                            "brats_crosswalk": brats_evidence,
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
            insert_asset_participant(
                conn,
                asset_id=asset_id,
                short_title=short_title,
                subject_id=subject_id,
                namespace=namespace,
                raw_subject_id=challenge_id,
                participant_role="depicted_subject",
                link_status=link_status,
                evidence=brats_evidence,
            )
            insert_brats_crosswalk_evidence(
                conn, asset_id, challenge_id, subject_id, brats_evidence
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


def load_remind_nrrd_reference(
    inventory_path: Path,
    provenance_path: Path = DEFAULT_REMIND_NRRD_PROVENANCE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not inventory_path.is_file() or not provenance_path.is_file():
        return [], {}
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    expected_digest = str(provenance.get("source_sha256") or "")
    actual_digest = file_sha256(inventory_path)
    if not expected_digest or actual_digest != expected_digest:
        raise RuntimeError(
            f"ReMIND inventory digest mismatch: {actual_digest} != {expected_digest}"
        )
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        inventory_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise RuntimeError(f"Malformed ReMIND inventory line {line_number}")
        checksum, package_path = parts
        rows.append(
            {
                "dataset_type": str(provenance.get("dataset_type") or "Collection"),
                "short_title": str(provenance.get("short_title") or REMIND_SHORT_TITLE),
                "download_id": str(provenance.get("aspera_inventory_id") or ""),
                "line_number": line_number,
                "checksum": checksum,
                "algorithm": "md5",
                "file_name": Path(package_path).name,
                "package_path": package_path,
                "file_ext": Path(package_path).suffix,
            }
        )
    expected_rows = int(provenance.get("row_count") or 0)
    if expected_rows and len(rows) != expected_rows:
        raise RuntimeError(
            f"ReMIND inventory row mismatch: {len(rows)} != {expected_rows}"
        )
    return rows, provenance


def ingest_remind_nrrd_inventory(
    conn: sqlite3.Connection,
    nifti_db: Path | None = None,
    *,
    inventory_path: Path | None = DEFAULT_REMIND_NRRD_INVENTORY,
    provenance_path: Path = DEFAULT_REMIND_NRRD_PROVENANCE,
) -> int:
    """Project the reviewed ReMIND Aspera ``.sums`` paths at file grain.

    TCIA's download aggregate reports 113 subjects/images because 113 subjects
    have a preoperative whole-tumor segmentation. The package itself contains
    356 NRRD segmentations across all 114 ReMIND subjects and several segment
    classes. The subject folder and filename repeat the exact Collection
    PatientID, so the mapping is deterministic and source supported.

    The package's March 2024 ``.sums`` file assigns the empty-file MD5 to every
    path even though the downloaded NRRDs are nonempty. Preserve that source
    value in provenance, but never expose it as a verified file checksum.
    """
    inventory_rows: Iterable[sqlite3.Row | dict[str, Any]] = []
    downloads: dict[tuple[str, str], str] = {}
    source_artifact = "public_non_dicom_reference"
    source_locator = "references/remind_nrrd_inventory_v1.sums"
    reference_provenance: dict[str, Any] = {}
    if inventory_path and inventory_path.is_file():
        inventory_rows, reference_provenance = load_remind_nrrd_reference(
            inventory_path, provenance_path
        )
        aggregate = conn.execute(
            "SELECT source_url FROM public_non_dicom_assets "
            "WHERE short_title=? AND file_format='NRRD' "
            "AND asset_granularity='download' ORDER BY asset_id LIMIT 1",
            (REMIND_SHORT_TITLE,),
        ).fetchone()
        if aggregate:
            downloads[(REMIND_SHORT_TITLE, str(reference_provenance.get("aspera_inventory_id") or ""))] = str(
                aggregate[0] or ""
            )
    elif nifti_db and nifti_db.exists():
        source_artifact = "nifti_metadata"
        source_locator = "nifti_metadata.aspera_root_sums_inventory"
        with closing(connect(nifti_db)) as source:
            if not table_exists(source, "aspera_root_sums_inventory"):
                return 0
            if table_exists(source, "agent_nifti_downloads"):
                for row in source.execute(
                    "SELECT short_title, download_id, download_url FROM agent_nifti_downloads"
                ):
                    downloads[(str(row["short_title"]), str(row["download_id"] or ""))] = str(
                        row["download_url"] or ""
                    )
            inventory_columns = columns(source, "aspera_root_sums_inventory")
            if not {"short_title", "package_path", "file_ext"}.issubset(inventory_columns):
                return 0
            optional = {
                "dataset_type": "'Collection'",
                "download_id": "''",
                "line_number": "0",
                "checksum": "''",
                "algorithm": "''",
                "file_name": "''",
            }
            select_fields = [
                name if name in inventory_columns else f"{fallback} AS {name}"
                for name, fallback in optional.items()
            ] + ["short_title", "package_path", "file_ext"]
            inventory_rows = list(
                source.execute(
                    f"""
                    SELECT {', '.join(select_fields)}
                    FROM aspera_root_sums_inventory
                    WHERE lower(short_title) = lower(?)
                      AND lower(ltrim(file_ext, '.')) = 'nrrd'
                    ORDER BY line_number
                    """,
                    (REMIND_SHORT_TITLE,),
                )
            )
    else:
        return 0
    imported = 0
    for row in inventory_rows:
            package_path = str(row["package_path"] or "")
            match = REMIND_NRRD_PATH.search(package_path)
            if not match:
                continue
            subject_id = match.group("subject")
            phase = match.group("phase").lower()
            segment_label = match.group("label").lower()
            sequence_name = match.group("sequence")
            download_id = str(row["download_id"] or "")
            source_url = downloads.get((str(row["short_title"]), download_id), "")
            raw_checksum = str(row["checksum"] or "").lower()
            checksum_is_placeholder = raw_checksum == EMPTY_FILE_MD5
            checksum = "" if checksum_is_placeholder else raw_checksum
            checksum_algorithm = "" if checksum_is_placeholder else str(row["algorithm"] or "")
            asset_id = stable_id(
                "asset", "remind_nrrd_package_path", download_id, package_path
            )
            mapping_method = "reviewed_remind_package_subject_folder_and_filename"
            evidence = {
                "download_id": download_id,
                "package_path": package_path,
                "source_line_number": row["line_number"],
                "mapping_method": mapping_method,
                "subject_folder_matches_filename": True,
                "raw_source_checksum": raw_checksum,
                "source_checksum_status": (
                    "invalid_empty_file_placeholder" if checksum_is_placeholder else "reported"
                ),
            }
            insert_asset(conn, {
                "asset_id": asset_id,
                "dataset_type": str(row["dataset_type"] or "Collection"),
                "short_title": str(row["short_title"]),
                "download_row_id": None,
                "download_id": download_id,
                "subject_id": subject_id,
                "subject_id_namespace": f"tcia_collection:{REMIND_SHORT_TITLE}",
                "participant_link_status": "reviewed_source_crosswalk",
                "asset_granularity": "file",
                "asset_name": str(row["file_name"] or Path(package_path).name),
                "file_name": str(row["file_name"] or Path(package_path).name),
                "package_path": package_path,
                "file_format": "NRRD",
                "container_format": "",
                "media_kind": "segmentation_volume",
                "spatial_dimensionality": "3d",
                "temporal_dimensionality": "none",
                "imaging_domain": "radiology",
                "modality": "MR",
                "object_role": "segmentation",
                "represented_file_count": 1,
                "size_bytes": None,
                "checksum": checksum,
                "checksum_algorithm": checksum_algorithm,
                "representation_provenance_class": "submitted_original",
                "source_system": "tcia_aspera",
                "source_record_id": f"{download_id}:sums:{row['line_number']}",
                "source_url": source_url,
                "raw_values_json": json_dumps({
                    "phase": phase,
                    "segment_label": segment_label,
                    "mr_sequence_token": sequence_name,
                    "raw_source_checksum": raw_checksum,
                }),
                "provenance_json": json_dumps({
                    "source_artifact": source_artifact,
                    "source_locator": source_locator,
                    "reference_provenance": reference_provenance,
                    **evidence,
                }),
                "quality_flag_json": json_dumps({
                    "participant_inventory": "reviewed_source_crosswalk",
                    "checksum": (
                        "invalid_empty_file_placeholder" if checksum_is_placeholder else "source_reported"
                    ),
                }),
            })
            insert_asset_participant(
                conn,
                asset_id=asset_id,
                short_title=REMIND_SHORT_TITLE,
                subject_id=subject_id,
                namespace=f"tcia_collection:{REMIND_SHORT_TITLE}",
                raw_subject_id=subject_id,
                participant_role="depicted_subject",
                link_status="reviewed_source_crosswalk",
                evidence=evidence,
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
                VALUES (?, ?, ?, ?, ?, ?, 'high', ?, ?, '', ?, ?)
                """,
                (
                    stable_id("crosswalk", asset_id, subject_id, mapping_method),
                    asset_id, REMIND_SHORT_TITLE, subject_id, subject_id,
                    mapping_method, source_url,
                    "Subject folder and filename repeat the exact ReMIND Collection PatientID.",
                    "2026-08-25", json_dumps(evidence),
                ),
            )
            if source_url:
                insert_location(
                    conn,
                    location_values(
                        asset_id,
                        source_url,
                        representation_class="submitted_original",
                        provenance={
                            "source_artifact": source_artifact,
                            "source_locator": source_locator,
                            "package_path": package_path,
                        },
                    ),
                )
            merge_image_metadata(
                conn,
                asset_id,
                {
                    "modality": "MR",
                    "file_format": "NRRD",
                    "media_kind": "segmentation_volume",
                    "spatial_dimensionality": "3d",
                    "temporal_dimensionality": "none",
                    "object_role": "segmentation",
                    "acquisition_phase": phase,
                    "segmentation_label": segment_label,
                    "sequence_name": sequence_name,
                },
                value_role="source_raw",
                source_kind="aspera_package_inventory",
                source_locator=source_locator,
                inference_method=mapping_method,
                confidence="high",
                priority=95,
                evidence=evidence,
                short_title=REMIND_SHORT_TITLE,
                assume_new=True,
            )
            imported += 1
    return imported


def ingest_tcga_gbm_qi_aim_inventory(
    conn: sqlite3.Connection,
    snapshot_db: Path,
    *,
    inventory_path: Path = DEFAULT_TCGA_GBM_QI_AIM_INVENTORY,
    provenance_path: Path = DEFAULT_TCGA_GBM_QI_AIM_PROVENANCE,
) -> dict[str, int]:
    """Project all AIM XML annotations to TCGA patients and source DICOM."""
    rows, provenance = load_tcga_gbm_qi_aim_inventory(
        inventory_path, provenance_path
    )
    with closing(connect(snapshot_db)) as source:
        dataset_present = source.execute(
            """SELECT 1 FROM agent_current_downloads
               WHERE lower(short_title) = lower(?) AND hidden = 0 LIMIT 1""",
            (TCGA_GBM_QI_SHORT_TITLE,),
        ).fetchone()
        download = source.execute(
            """SELECT * FROM agent_current_downloads
               WHERE lower(short_title) = lower(?) AND download_id = '45557'
                 AND hidden = 0 AND controlled_access = 0""",
            (TCGA_GBM_QI_SHORT_TITLE,),
        ).fetchone()
    if download is None:
        if dataset_present is None:
            return {
                "xml_files": 0,
                "participants": 0,
                "studies": 0,
                "series": 0,
                "sop_instances": 0,
            }
        raise RuntimeError(
            "TCGA-GBM-QI-Radiogenomics AIM download 45557 is absent from the current snapshot"
        )
    source_url = str(download["download_url"] or provenance.get("source_url") or "")
    reviewed_at = str(provenance.get("reviewed_at") or "2026-08-27")
    namespace = "tcia_collection:TCGA-GBM"
    parent_id = stable_id("asset", "tcga_gbm_qi_aim_download", "45557")
    insert_asset(
        conn,
        {
            "asset_id": parent_id,
            "dataset_type": "Analysis Result",
            "short_title": TCGA_GBM_QI_SHORT_TITLE,
            "download_row_id": download["download_row_id"],
            "download_id": "45557",
            "subject_id": "",
            "subject_id_namespace": "",
            "participant_link_status": "crosswalk_available_at_file_grain",
            "asset_granularity": "download",
            "asset_name": download["download_title"],
            "file_name": Path(urllib.parse.urlparse(source_url).path).name,
            "package_path": "",
            "file_format": "XML",
            "container_format": "ZIP",
            "media_kind": "geometric_annotation",
            "spatial_dimensionality": "2D",
            "temporal_dimensionality": "static",
            "imaging_domain": "imaging_annotation",
            "modality": "MR",
            "object_role": "aim_segmentation_annotation",
            "represented_file_count": len(rows),
            "size_bytes": download_size_bytes(
                download["download_size"], download["download_size_unit"]
            ),
            "checksum": str(provenance.get("source_zip_sha256") or ""),
            "checksum_algorithm": "sha256",
            "representation_provenance_class": "derived_asset",
            "source_system": "tcia_wordpress",
            "source_record_id": "tcga-gbm-qi-aim-download:45557",
            "source_url": source_url,
            "raw_values_json": json_dumps(
                {
                    "source_collection": "TCGA-GBM",
                    "aim_version": "TCGA",
                    "ignored_package_entries": provenance.get(
                        "ignored_package_entries"
                    ) or [],
                }
            ),
            "provenance_json": json_dumps(
                {
                    "source_artifact": "tcga_gbm_qi_radiogenomics_aim_inventory_v1",
                    "reference_provenance": provenance,
                }
            ),
            "quality_flag_json": json_dumps(
                {
                    "participant_inventory": "crosswalk_available_at_file_grain",
                    "represented_file_count_source": "reviewed_zip_inventory",
                }
            ),
        },
    )
    insert_location(
        conn,
        location_values(
            parent_id,
            source_url,
            checksum=str(provenance.get("source_zip_sha256") or ""),
            checksum_algorithm="sha256",
            representation_class="derived_asset",
            provenance={"download_id": "45557", "container_format": "ZIP"},
        ),
    )
    decision_id = stable_id(
        "crosswalk_decision", "Analysis Result", TCGA_GBM_QI_SHORT_TITLE,
        "aim_patient_and_dicom_uid_projection", "45557",
    )
    conn.execute(
        """INSERT OR REPLACE INTO public_non_dicom_crosswalk_decisions
           VALUES (?, 'Analysis Result', ?, '["45557"]', 'resolved',
                   'aim_patient_and_dicom_uid_projection', ?, ?, ?, ?)""",
        (
            decision_id,
            TCGA_GBM_QI_SHORT_TITLE,
            "Every AIM XML directly supplies one TCGA PatientID and one referenced DICOM Study, Series, and SOP Instance UID.",
            source_url,
            reviewed_at,
            json_dumps(
                {
                    "source_file": str(provenance_path),
                    "source_zip_sha256": provenance.get("source_zip_sha256"),
                    "source_collection": "TCGA-GBM",
                }
            ),
        ),
    )
    patients: set[str] = set()
    studies: set[str] = set()
    series: set[str] = set()
    sops: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        patient_id = str(row["patient_id"])
        study_uid = str(row["study_instance_uid"])
        series_uid = str(row["series_instance_uid"])
        sop_uid = str(row["sop_instance_uid"])
        asset_id = stable_id(
            "asset", "tcga_gbm_qi_aim_xml", "45557", row["file_name"]
        )
        evidence = {
            "source_file": str(inventory_path),
            "source_row": row_number,
            "patient_id": patient_id,
            "study_instance_uid": study_uid,
            "series_instance_uid": series_uid,
            "sop_instance_uid": sop_uid,
            "annotation_uid": row["annotation_uid"],
            "reviewed_at": reviewed_at,
        }
        insert_asset(
            conn,
            {
                "asset_id": asset_id,
                "dataset_type": "Analysis Result",
                "short_title": TCGA_GBM_QI_SHORT_TITLE,
                "download_row_id": download["download_row_id"],
                "download_id": "45557",
                "subject_id": patient_id,
                "subject_id_namespace": namespace,
                "participant_link_status": "reviewed_source_crosswalk",
                "asset_granularity": "file",
                "asset_name": row["annotation_name"] or row["file_name"],
                "file_name": row["file_name"],
                "package_path": row["file_name"],
                "file_format": "XML",
                "container_format": "ZIP",
                "media_kind": "geometric_annotation",
                "spatial_dimensionality": "2D",
                "temporal_dimensionality": "static",
                "imaging_domain": "imaging_annotation",
                "modality": "MR",
                "object_role": "aim_segmentation_annotation",
                "represented_file_count": 1,
                "size_bytes": int(row["size_bytes"]),
                "checksum": row["sha256"],
                "checksum_algorithm": "sha256",
                "representation_provenance_class": "derived_asset",
                "source_system": "tcia_wordpress",
                "source_record_id": f"tcga-gbm-qi-aim-inventory:{row_number}",
                "source_url": source_url,
                "raw_values_json": json_dumps(
                    {
                        "source_collection": "TCGA-GBM",
                        "annotation_uid": row["annotation_uid"],
                        "aim_version": row["aim_version"],
                        "code_meaning": row["code_meaning"],
                        "code_value": row["code_value"],
                        "coding_scheme_designator": row[
                            "coding_scheme_designator"
                        ],
                        "study_instance_uid": study_uid,
                        "series_instance_uid": series_uid,
                        "sop_instance_uid": sop_uid,
                        "study_date": row["study_date"],
                    }
                ),
                "provenance_json": json_dumps(
                    {
                        "source_artifact": "tcga_gbm_qi_radiogenomics_aim_inventory_v1",
                        "reference_provenance": provenance,
                        **evidence,
                    }
                ),
                "quality_flag_json": json_dumps(
                    {
                        "participant_inventory": "reviewed_source_crosswalk",
                        "source_dicom_link": "aim_embedded_uids",
                    }
                ),
            },
        )
        insert_asset_participant(
            conn,
            asset_id=asset_id,
            short_title=TCGA_GBM_QI_SHORT_TITLE,
            subject_id=patient_id,
            namespace=namespace,
            raw_subject_id=patient_id,
            participant_role="annotated_subject",
            link_status="reviewed_source_crosswalk",
            evidence=evidence,
        )
        insert_location(
            conn,
            location_values(
                asset_id,
                source_url,
                representation_class="derived_asset",
                provenance={"package_path": row["file_name"], **evidence},
            ),
        )
        conn.execute(
            """INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
               VALUES (?, ?, ?, ?, ?, ?, 'high', ?, ?, ?, ?, ?)""",
            (
                stable_id(
                    "crosswalk", asset_id, patient_id, series_uid,
                    "aim_embedded_patient_and_dicom_uids",
                ),
                asset_id,
                TCGA_GBM_QI_SHORT_TITLE,
                patient_id,
                patient_id,
                "aim_embedded_patient_and_dicom_uids",
                source_url,
                "The AIM XML directly records the TCGA PatientID and referenced DICOM Study, Series, and SOP Instance UIDs.",
                series_uid,
                reviewed_at,
                json_dumps(evidence),
            ),
        )
        merge_image_metadata(
            conn,
            asset_id,
            {
                "file_format": "XML",
                "media_kind": "geometric_annotation",
                "object_role": "aim_segmentation_annotation",
                "modality": "MR",
                "aim_version": row["aim_version"],
                "annotation_uid": row["annotation_uid"],
                "study_instance_uid": study_uid,
                "series_instance_uid": series_uid,
                "sop_instance_uid": sop_uid,
            },
            value_role="source_raw",
            source_kind="reviewed_aim_inventory",
            source_locator=str(inventory_path),
            inference_method="direct_aim_xml_attributes",
            confidence="high",
            priority=110,
            evidence=evidence,
            short_title=TCGA_GBM_QI_SHORT_TITLE,
            assume_new=True,
        )
        patients.add(patient_id)
        studies.add(study_uid)
        series.add(series_uid)
        sops.add(sop_uid)

    counts = {
        "xml_files": len(rows),
        "participants": len(patients),
        "studies": len(studies),
        "series": len(series),
        "sop_instances": len(sops),
    }
    if counts != {
        "xml_files": 321,
        "participants": 55,
        "studies": 60,
        "series": 111,
        "sop_instances": 193,
    }:
        raise RuntimeError(f"TCGA-GBM-QI-Radiogenomics ingest mismatch: {counts}")
    add_dataset_metadata_note(
        conn,
        TCGA_GBM_QI_SHORT_TITLE,
        "aim_annotations",
        "aim_patient_and_dicom_uid_inventory_reviewed",
        "The AIM ZIP contains 321 XML annotations for 55 TCGA-GBM patients, linked directly to 60 studies, 111 series, and 193 SOP instances.",
        severity="info",
        status="resolved",
        affected_assets=len(rows),
        evidence={"source_url": source_url, **counts},
    )
    return counts


def ingest_tcga_lgg_mask_inventory(
    conn: sqlite3.Connection,
    snapshot_db: Path,
    *,
    mask_inventory_path: Path = DEFAULT_TCGA_LGG_MASK_INVENTORY,
    vasari_inventory_path: Path = DEFAULT_TCGA_LGG_MASK_VASARI,
    provenance_path: Path = DEFAULT_TCGA_LGG_MASK_PROVENANCE,
) -> dict[str, int]:
    """Project reviewed MATLAB masks and VASARI annotations at file grain."""
    counts = {
        "mask_files": 0,
        "mask_participants": 0,
        "mask_series_links": 0,
        "vasari_assets": 0,
        "vasari_participants": 0,
        "vasari_complete_rows": 0,
        "feature_key_assets": 0,
    }
    required = (mask_inventory_path, vasari_inventory_path, provenance_path)
    if not all(path.is_file() for path in required):
        return counts
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    expected = provenance.get("counts") or {}
    reference_files = provenance.get("reference_files") or {}
    for key, path in (
        ("mask_inventory", mask_inventory_path),
        ("vasari_participants", vasari_inventory_path),
    ):
        expected_sha256 = str((reference_files.get(key) or {}).get("sha256") or "")
        if expected_sha256 and file_sha256(path) != expected_sha256:
            raise RuntimeError(f"TCGA-LGG-Mask reference hash mismatch: {path}")

    with closing(connect(snapshot_db)) as source:
        download_rows = {
            str(row["download_id"]): row
            for row in source.execute(
                """SELECT * FROM agent_current_downloads
                   WHERE lower(short_title) = lower(?) AND hidden = 0
                     AND controlled_access = 0 AND download_id IN ('45749', '45751', '45753')""",
                (TCGA_LGG_MASK_SHORT_TITLE,),
            )
        }
    if not download_rows:
        return counts
    missing_downloads = sorted({"45749", "45751", "45753"} - set(download_rows))
    if missing_downloads:
        raise RuntimeError(
            "TCGA-LGG-Mask current public downloads missing from snapshot: "
            + ", ".join(missing_downloads)
        )

    mask_download = download_rows["45753"]
    mask_parent = conn.execute(
        """SELECT * FROM public_non_dicom_assets
           WHERE lower(short_title) = lower(?) AND download_id = '45753'
             AND asset_granularity = 'download' AND file_format = 'MATLAB'""",
        (TCGA_LGG_MASK_SHORT_TITLE,),
    ).fetchone()
    if mask_parent is None:
        raise RuntimeError("TCGA-LGG-Mask MATLAB download declaration was not imported")

    reviewed_at = str(provenance.get("reviewed_at") or "2026-08-27")
    page_url = "https://www.cancerimagingarchive.net/analysis-result/tcga-lgg-mask/"
    mask_decision_id = stable_id(
        "crosswalk_decision", "Analysis Result", TCGA_LGG_MASK_SHORT_TITLE,
        "participant_and_source_series_crosswalk", "45753",
    )
    conn.execute(
        """INSERT OR REPLACE INTO public_non_dicom_crosswalk_decisions
           VALUES (?, 'Analysis Result', ?, '["45753"]', 'resolved',
                   'participant_and_source_series_crosswalk', ?, ?, ?, ?)""",
        (
            mask_decision_id,
            TCGA_LGG_MASK_SHORT_TITLE,
            "Patient folders provide TCGA-LGG PatientIDs and every MATLAB filename exactly matches one Series Instance UID in both the published manifest and DICOM digest.",
            page_url,
            reviewed_at,
            json_dumps({"source_file": str(provenance_path), "review_status": provenance.get("review_status")}),
        ),
    )

    mask_subjects: set[str] = set()
    mask_series: set[str] = set()
    with mask_inventory_path.open(newline="", encoding="utf-8") as handle:
        mask_rows = list(csv.DictReader(handle))
    for row_number, row in enumerate(mask_rows, start=2):
        subject_id = str(row["subject_id"]).strip()
        raw_subject_id = str(row["raw_subject_id"]).strip()
        series_uid = str(row["series_instance_uid"]).strip()
        package_path = str(row["package_path"]).strip()
        asset_id = stable_id(
            "asset", "tcga_lgg_mask", "45753", package_path, subject_id
        )
        evidence = {
            "source_file": str(mask_inventory_path),
            "source_row": row_number,
            "series_instance_uid": series_uid,
            "study_instance_uid": row["study_instance_uid"],
            "dicom_image_count": int(row["dicom_image_count"]),
            "reviewed_at": reviewed_at,
        }
        insert_asset(
            conn,
            {
                "asset_id": asset_id,
                "dataset_type": "Analysis Result",
                "short_title": TCGA_LGG_MASK_SHORT_TITLE,
                "download_row_id": mask_parent["download_row_id"],
                "download_id": "45753",
                "subject_id": subject_id,
                "subject_id_namespace": f"tcia_dataset:{TCGA_LGG_MASK_SHORT_TITLE}",
                "participant_link_status": "reviewed_source_crosswalk",
                "asset_granularity": "file",
                "asset_name": row["file_name"],
                "file_name": row["file_name"],
                "package_path": package_path,
                "file_format": "MATLAB",
                "container_format": "ZIP",
                "media_kind": "image_volume",
                "spatial_dimensionality": "3D",
                "temporal_dimensionality": "static",
                "imaging_domain": "imaging_annotation",
                "modality": "MR",
                "object_role": "segmentation",
                "represented_file_count": 1,
                "size_bytes": int(row["size_bytes"]),
                "checksum": row["sha256"],
                "checksum_algorithm": "sha256",
                "representation_provenance_class": "derived_asset",
                "source_system": "tcia_wordpress",
                "source_record_id": f"tcga-lgg-mask-inventory:{row_number}",
                "source_url": mask_download["download_url"],
                "raw_values_json": json_dumps(
                    {
                        "source_collection": "TCGA-LGG",
                        "study_instance_uid": row["study_instance_uid"],
                        "series_instance_uid": series_uid,
                        "dicom_image_count": int(row["dicom_image_count"]),
                        "series_description": row["series_description"],
                        "protocol_name": row["protocol_name"],
                    }
                ),
                "provenance_json": json_dumps(
                    {
                        "source_artifact": "tcga_lgg_mask_inventory_v1",
                        "reference_provenance": provenance,
                        **evidence,
                    }
                ),
                "quality_flag_json": json_dumps(
                    {
                        "participant_inventory": "reviewed_source_crosswalk",
                        "source_series_link": "exact_manifest_and_digest_match",
                    }
                ),
            },
        )
        insert_asset_participant(
            conn,
            asset_id=asset_id,
            short_title=TCGA_LGG_MASK_SHORT_TITLE,
            subject_id=subject_id,
            namespace=f"tcia_dataset:{TCGA_LGG_MASK_SHORT_TITLE}",
            raw_subject_id=raw_subject_id,
            participant_role="depicted_subject",
            link_status="reviewed_source_crosswalk",
            evidence=evidence,
        )
        insert_location(
            conn,
            location_values(
                asset_id,
                str(mask_download["download_url"] or ""),
                representation_class="derived_asset",
                provenance={"package_path": package_path, **evidence},
            ),
        )
        crosswalk_id = stable_id(
            "crosswalk", asset_id, subject_id, series_uid,
            "package_patient_and_exact_series_uid",
        )
        conn.execute(
            """INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
               VALUES (?, ?, ?, ?, ?, ?, 'high', ?, ?, ?, ?, ?)""",
            (
                crosswalk_id,
                asset_id,
                TCGA_LGG_MASK_SHORT_TITLE,
                raw_subject_id,
                subject_id,
                "package_patient_and_exact_series_uid",
                str((provenance.get("source_files") or {}).get("series_manifest_csv", {}).get("url") or page_url),
                "Folder PatientID and filename Series Instance UID exactly match the official corresponding-image digest.",
                series_uid,
                reviewed_at,
                json_dumps(evidence),
            ),
        )
        merge_image_metadata(
            conn,
            asset_id,
            {
                "modality": "MR",
                "file_format": "MATLAB",
                "media_kind": "image_volume",
                "spatial_dimensionality": "3D",
                "temporal_dimensionality": "static",
                "object_role": "segmentation",
                "source_collection": "TCGA-LGG",
                "source_study_instance_uid": row["study_instance_uid"],
                "source_series_instance_uid": series_uid,
                "source_dicom_image_count": int(row["dicom_image_count"]),
                "source_series_description": row["series_description"],
                "source_protocol_name": row["protocol_name"],
            },
            value_role="source_raw",
            source_kind="reviewed_manifest_and_dicom_digest",
            source_locator=str(mask_inventory_path),
            inference_method="package_patient_and_exact_series_uid",
            confidence="high",
            priority=110,
            evidence=evidence,
            short_title=TCGA_LGG_MASK_SHORT_TITLE,
            assume_new=True,
        )
        mask_subjects.add(subject_id)
        mask_series.add(series_uid)

    vasari_download = download_rows["45749"]
    vasari_asset_id = stable_id(
        "asset", "tcga_lgg_mask_vasari", "45749", vasari_download["download_url"]
    )
    vasari_sha256 = str(
        ((provenance.get("source_files") or {}).get("vasari_csv") or {}).get("sha256") or ""
    )
    insert_asset(
        conn,
        {
            "asset_id": vasari_asset_id,
            "dataset_type": "Analysis Result",
            "short_title": TCGA_LGG_MASK_SHORT_TITLE,
            "download_row_id": vasari_download["download_row_id"],
            "download_id": "45749",
            "subject_id": "",
            "subject_id_namespace": "",
            "participant_link_status": "reviewed_source_crosswalk",
            "asset_granularity": "file",
            "asset_name": vasari_download["download_title"],
            "file_name": Path(urllib.parse.urlparse(vasari_download["download_url"]).path).name,
            "package_path": "",
            "file_format": "CSV",
            "container_format": "",
            "media_kind": "tabular",
            "spatial_dimensionality": "not_applicable",
            "temporal_dimensionality": "static",
            "imaging_domain": "imaging_annotation",
            "modality": "MR",
            "object_role": "qualitative_image_annotation_table",
            "represented_file_count": 1,
            "size_bytes": download_size_bytes(
                vasari_download["download_size"], vasari_download["download_size_unit"]
            ),
            "checksum": vasari_sha256,
            "checksum_algorithm": "sha256" if vasari_sha256 else "",
            "representation_provenance_class": "derived_asset",
            "source_system": "tcia_wordpress",
            "source_record_id": "tcga-lgg-mask-vasari:45749",
            "source_url": vasari_download["download_url"],
            "raw_values_json": json_dumps(
                {"feature_columns": [f"f{index}" for index in range(1, 26)] + ["f29"]}
            ),
            "provenance_json": json_dumps(
                {"source_artifact": "tcga_lgg_mask_vasari_participants_v1", "reference_provenance": provenance}
            ),
            "quality_flag_json": json_dumps(
                {"complete_numeric_rows": 178, "participant_rows": 188}
            ),
        },
    )
    insert_location(
        conn,
        location_values(
            vasari_asset_id,
            str(vasari_download["download_url"] or ""),
            representation_class="derived_asset",
            provenance={"source_record_id": "tcga-lgg-mask-vasari:45749"},
        ),
    )
    vasari_subjects: set[str] = set()
    complete_rows = 0
    with vasari_inventory_path.open(newline="", encoding="utf-8") as handle:
        vasari_rows = list(csv.DictReader(handle))
    for row in vasari_rows:
        raw_subject_id = str(row["raw_subject_id"]).strip()
        subject_id = str(row["subject_id"]).strip()
        complete_numeric = int(row["complete_numeric"])
        evidence = {
            "source_file": str(vasari_inventory_path),
            "source_row": int(row["source_row"]),
            "complete_numeric": bool(complete_numeric),
            "non_numeric_or_missing_fields": json.loads(
                row["non_numeric_or_missing_fields_json"] or "[]"
            ),
            "normalization_method": (
                "curator_reviewed_typo_alias"
                if raw_subject_id.upper() == "TCGA-EZ-7264A"
                else "case_normalization"
                if raw_subject_id != subject_id
                else "identity"
            ),
        }
        insert_asset_participant(
            conn,
            asset_id=vasari_asset_id,
            short_title=TCGA_LGG_MASK_SHORT_TITLE,
            subject_id=subject_id,
            namespace=f"tcia_dataset:{TCGA_LGG_MASK_SHORT_TITLE}",
            raw_subject_id=raw_subject_id,
            participant_role="annotated_subject",
            link_status="reviewed_source_crosswalk",
            evidence=evidence,
        )
        conn.execute(
            """INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
               VALUES (?, ?, ?, ?, ?, ?, 'high', ?, ?, '', ?, ?)""",
            (
                stable_id("crosswalk", vasari_asset_id, raw_subject_id, subject_id),
                vasari_asset_id,
                TCGA_LGG_MASK_SHORT_TITLE,
                raw_subject_id,
                subject_id,
                evidence["normalization_method"],
                str(vasari_download["download_url"] or ""),
                "VASARI row PatientID retained raw and linked to the reviewed Analysis Result participant identity.",
                reviewed_at,
                json_dumps(evidence),
            ),
        )
        vasari_subjects.add(subject_id)
        complete_rows += complete_numeric
    merge_image_metadata(
        conn,
        vasari_asset_id,
        {
            "file_format": "CSV",
            "media_kind": "tabular",
            "object_role": "qualitative_image_annotation_table",
            "participant_count": len(vasari_subjects),
            "complete_numeric_rows": complete_rows,
            "vasari_feature_count": 26,
        },
        value_role="source_raw",
        source_kind="reviewed_vasari_inventory",
        source_locator=str(vasari_inventory_path),
        inference_method="participant_row_inventory",
        confidence="high",
        priority=110,
        evidence={"reference_provenance": provenance},
        short_title=TCGA_LGG_MASK_SHORT_TITLE,
        assume_new=True,
    )

    feature_download = download_rows["45751"]
    feature_asset_id = stable_id(
        "asset", "tcga_lgg_mask_feature_key", "45751", feature_download["download_url"]
    )
    feature_sha256 = str(
        ((provenance.get("source_files") or {}).get("feature_key_pdf") or {}).get("sha256") or ""
    )
    insert_asset(
        conn,
        {
            "asset_id": feature_asset_id,
            "dataset_type": "Analysis Result",
            "short_title": TCGA_LGG_MASK_SHORT_TITLE,
            "download_row_id": feature_download["download_row_id"],
            "download_id": "45751",
            "subject_id": "",
            "subject_id_namespace": "",
            "participant_link_status": "dataset_only",
            "asset_granularity": "file",
            "asset_name": feature_download["download_title"],
            "file_name": Path(urllib.parse.urlparse(feature_download["download_url"]).path).name,
            "package_path": "",
            "file_format": "PDF",
            "container_format": "",
            "media_kind": "document",
            "spatial_dimensionality": "not_applicable",
            "temporal_dimensionality": "static",
            "imaging_domain": "imaging_annotation",
            "modality": "MR",
            "object_role": "annotation_feature_dictionary",
            "represented_file_count": 1,
            "size_bytes": download_size_bytes(
                feature_download["download_size"], feature_download["download_size_unit"]
            ),
            "checksum": feature_sha256,
            "checksum_algorithm": "sha256" if feature_sha256 else "",
            "representation_provenance_class": "metadata_only",
            "source_system": "tcia_wordpress",
            "source_record_id": "tcga-lgg-mask-feature-key:45751",
            "source_url": feature_download["download_url"],
            "raw_values_json": json_dumps({"defined_features": "F1-F30"}),
            "provenance_json": json_dumps(
                {"source_artifact": "VASARI_MR_featurekey.pdf", "reference_provenance": provenance}
            ),
            "quality_flag_json": "{}",
        },
    )
    insert_location(
        conn,
        location_values(
            feature_asset_id,
            str(feature_download["download_url"] or ""),
            representation_class="metadata_only",
            provenance={"source_record_id": "tcga-lgg-mask-feature-key:45751"},
        ),
    )
    conn.execute(
        """INSERT OR REPLACE INTO public_non_dicom_asset_relationships
           VALUES (?, ?, ?, 'interpreted_by', 'source_supported', ?, 'curator_reviewed')""",
        (
            stable_id("relationship", vasari_asset_id, feature_asset_id, "interpreted_by"),
            vasari_asset_id,
            feature_asset_id,
            json_dumps(
                {"description": "The PDF defines the VASARI feature names and categorical score codes.", "reviewed_at": reviewed_at}
            ),
        ),
    )
    annotation_decision_id = stable_id(
        "crosswalk_decision", "Analysis Result", TCGA_LGG_MASK_SHORT_TITLE,
        "shared_annotation_participants", "45749", "45751",
    )
    conn.execute(
        """INSERT OR REPLACE INTO public_non_dicom_crosswalk_decisions
           VALUES (?, 'Analysis Result', ?, '["45749","45751"]', 'resolved',
                   'shared_annotation_participants', ?, ?, ?, ?)""",
        (
            annotation_decision_id,
            TCGA_LGG_MASK_SHORT_TITLE,
            "The VASARI CSV contains 188 participant rows; mixed-case source spellings are normalized and TCGA-EZ-7264A is retained as a curator-reviewed alias of TCGA-EZ-7264. The PDF is companion feature documentation.",
            page_url,
            reviewed_at,
            json_dumps({"source_file": str(provenance_path), "review_status": provenance.get("review_status")}),
        ),
    )
    add_dataset_metadata_note(
        conn,
        TCGA_LGG_MASK_SHORT_TITLE,
        "vasari_scores",
        "vasari_feature_key_and_completeness_reviewed",
        "The shared CSV has 188 participant rows and 178 complete numeric rows across f1-f25 and f29; the linked PDF defines VASARI F1-F30.",
        severity="info",
        status="resolved",
        affected_assets=1,
        evidence={"vasari_asset_id": vasari_asset_id, "feature_key_asset_id": feature_asset_id},
    )

    counts.update(
        {
            "mask_files": len(mask_rows),
            "mask_participants": len(mask_subjects),
            "mask_series_links": len(mask_series),
            "vasari_assets": 1,
            "vasari_participants": len(vasari_subjects),
            "vasari_complete_rows": complete_rows,
            "feature_key_assets": 1,
        }
    )
    expected_counts = {
        "mask_files": int(expected.get("mask_files") or 0),
        "mask_participants": int(expected.get("mask_subjects") or 0),
        "mask_series_links": int(expected.get("series_instance_uids") or 0),
        "vasari_assets": 1,
        "vasari_participants": int(expected.get("vasari_subjects") or 0),
        "vasari_complete_rows": int(expected.get("vasari_complete_numeric_rows") or 0),
        "feature_key_assets": 1,
    }
    if counts != expected_counts:
        raise RuntimeError(
            f"TCGA-LGG-Mask reviewed inventory mismatch: {counts} != {expected_counts}"
        )
    return counts


def ingest_reviewed_analysis_result_participants(
    conn: sqlite3.Connection,
    snapshot_db: Path,
    *,
    inventory_path: Path = DEFAULT_REVIEWED_PARTICIPANT_INVENTORY,
    provenance_path: Path = DEFAULT_REVIEWED_PARTICIPANT_PROVENANCE,
) -> dict[str, int]:
    """Link official Analysis Result tables/packages to their stated subjects."""
    counts = {
        "inventory_rows": 0,
        "datasets": 0,
        "downloads": 0,
        "participants": 0,
        "asset_participant_links": 0,
        "crosswalk_evidence_rows": 0,
    }
    if not inventory_path.is_file() or not provenance_path.is_file():
        return counts
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    expected_hash = str(provenance.get("inventory_sha256") or "")
    actual_hash = file_sha256(inventory_path)
    if not expected_hash or actual_hash != expected_hash:
        raise RuntimeError(
            "Reviewed Analysis Result participant inventory digest mismatch: "
            f"{actual_hash} != {expected_hash or 'missing'}"
        )
    with inventory_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    counts["inventory_rows"] = len(rows)
    expected_rows = int(((provenance.get("counts") or {}).get("membership_rows") or 0))
    if expected_rows != len(rows):
        raise RuntimeError(
            "Reviewed Analysis Result participant row-count mismatch: "
            f"{len(rows)} != {expected_rows}"
        )

    with closing(connect(snapshot_db)) as source:
        active_downloads = {
            (str(row["short_title"]), str(row["download_id"])): dict(row)
            for row in source.execute(
                """SELECT * FROM agent_current_downloads
                   WHERE hidden = 0 AND controlled_access = 0"""
            )
        }
    applicable = [
        row for row in rows
        if (str(row["short_title"]), str(row["download_id"])) in active_downloads
    ]
    if not applicable:
        return counts

    source_files = provenance.get("source_files") or {}
    reviewed_at = str(provenance.get("reviewed_at") or "")
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in applicable:
        grouped[(row["dataset_type"], row["short_title"], row["download_id"])].append(row)

    participants: set[tuple[str, str]] = set()
    datasets: set[str] = set()
    for (dataset_type, short_title, download_id), membership_rows in sorted(grouped.items()):
        assets = conn.execute(
            """SELECT asset_id FROM public_non_dicom_assets
               WHERE dataset_type=? AND short_title=? AND download_id=?
                 AND asset_granularity='download'
               ORDER BY asset_id""",
            (dataset_type, short_title, download_id),
        ).fetchall()
        if not assets:
            download = active_downloads[(short_title, download_id)]
            source_names = sorted({row["source_file"] for row in membership_rows})
            if len(source_names) != 1:
                raise RuntimeError(
                    "Reviewed participant inventory metadata asset has multiple source files for "
                    f"{short_title} download {download_id}: {source_names}"
                )
            source_name = source_names[0]
            source_record = source_files.get(source_name) or {}
            suffix = Path(source_name).suffix.casefold()
            file_format = {
                ".csv": "CSV",
                ".xlsx": "XLSX",
                ".json": "JSON",
                ".txt": "TXT",
                ".zip": "JSON",
            }.get(suffix, "OTHER")
            container_format = "ZIP" if suffix == ".zip" else ""
            source_url = str(source_record.get("url") or download["download_url"] or "")
            asset_id = stable_id(
                "asset", "reviewed_participant_source", dataset_type, short_title,
                download_id, source_name,
            )
            insert_asset(
                conn,
                {
                    "asset_id": asset_id,
                    "dataset_type": dataset_type,
                    "short_title": short_title,
                    "download_row_id": download["download_row_id"],
                    "download_id": download_id,
                    "subject_id": "",
                    "subject_id_namespace": "",
                    "participant_link_status": "reviewed_source_inventory",
                    "asset_granularity": "file",
                    "asset_name": download["download_title"] or source_name,
                    "file_name": Path(urllib.parse.urlparse(source_url).path).name or source_name,
                    "package_path": "",
                    "file_format": file_format,
                    "container_format": container_format,
                    "media_kind": "tabular" if file_format in {"CSV", "XLSX"} else "document",
                    "spatial_dimensionality": "not_applicable",
                    "temporal_dimensionality": "static",
                    "imaging_domain": "imaging_annotation",
                    "modality": "",
                    "object_role": "participant_metadata_table",
                    "represented_file_count": 1,
                    "size_bytes": int(source_record.get("size_bytes") or 0) or None,
                    "checksum": str(source_record.get("sha256") or ""),
                    "checksum_algorithm": "sha256",
                    "representation_provenance_class": "metadata_only",
                    "source_system": "tcia_wordpress",
                    "source_record_id": f"reviewed-participant-inventory:{download_id}",
                    "source_url": source_url,
                    "raw_values_json": json_dumps({
                        "published_file_types": parse_list(download["file_types"]),
                        "published_subject_count": download["subjects"],
                    }),
                    "provenance_json": json_dumps({
                        "source_artifact": "reviewed_analysis_result_participants_v1",
                        "source_file": source_name,
                        "source_sha256": source_record.get("sha256") or "",
                        "reviewed_at": reviewed_at,
                    }),
                    "quality_flag_json": json_dumps({
                        "participant_inventory": "reviewed_source_inventory"
                    }),
                },
            )
            insert_location(
                conn,
                location_values(
                    asset_id,
                    source_url,
                    representation_class="metadata_only",
                    provenance={
                        "source_artifact": "reviewed_analysis_result_participants_v1",
                        "download_id": download_id,
                    },
                ),
            )
            assets = conn.execute(
                "SELECT asset_id FROM public_non_dicom_assets WHERE asset_id=?",
                (asset_id,),
            ).fetchall()
        datasets.add(short_title)
        decision_id = stable_id(
            "crosswalk_decision", dataset_type, short_title,
            "official_source_participant_inventory", download_id,
        )
        source_names = sorted({row["source_file"] for row in membership_rows})
        evidence_urls = sorted({
            str((source_files.get(name) or {}).get("url") or "")
            for name in source_names
            if str((source_files.get(name) or {}).get("url") or "")
        })
        conn.execute(
            """INSERT OR REPLACE INTO public_non_dicom_crosswalk_decisions
               VALUES (?, ?, ?, ?, 'resolved', 'official_source_participant_inventory',
                       ?, ?, ?, ?)""",
            (
                decision_id, dataset_type, short_title, json_dumps([download_id]),
                "Participant identifiers are read directly from the official TCIA download; raw values and source locators are retained.",
                evidence_urls[0] if len(evidence_urls) == 1 else "",
                reviewed_at,
                json_dumps({
                    "source_files": source_names,
                    "source_hashes": {
                        name: str((source_files.get(name) or {}).get("sha256") or "")
                        for name in source_names
                    },
                    "participant_count": len({row["participant_id"] for row in membership_rows}),
                }),
            ),
        )
        for asset in assets:
            asset_id = str(asset["asset_id"])
            conn.execute(
                """UPDATE public_non_dicom_assets
                   SET participant_link_status='reviewed_source_inventory',
                       quality_flag_json=json_set(
                           COALESCE(NULLIF(quality_flag_json, ''), '{}'),
                           '$.participant_inventory', 'reviewed_source_inventory',
                           '$.crosswalk_decision_id', ?
                       )
                   WHERE asset_id=?""",
                (decision_id, asset_id),
            )
            for row in membership_rows:
                subject_id = str(row["participant_id"]).strip()
                raw_subject_id = str(row["raw_participant_id"]).strip()
                evidence = {
                    "decision_id": decision_id,
                    "source_file": row["source_file"],
                    "source_locator": row["source_locator"],
                    "source_sha256": str(
                        (source_files.get(row["source_file"]) or {}).get("sha256") or ""
                    ),
                    "reviewed_at": reviewed_at,
                }
                insert_asset_participant(
                    conn,
                    asset_id=asset_id,
                    short_title=short_title,
                    subject_id=subject_id,
                    namespace=f"tcia_dataset:{short_title}",
                    raw_subject_id=raw_subject_id,
                    participant_role=str(row["participant_role"]),
                    link_status="reviewed_source_inventory",
                    evidence=evidence,
                )
                conn.execute(
                    """INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
                       VALUES (?, ?, ?, ?, ?, 'official_source_participant_identifier',
                               'high', ?, ?, '', ?, ?)""",
                    (
                        stable_id("crosswalk", asset_id, subject_id, "official_source_participant_identifier"),
                        asset_id, short_title, raw_subject_id, subject_id,
                        str((source_files.get(row["source_file"]) or {}).get("url") or ""),
                        "Identifier copied from the official TCIA source file.",
                        reviewed_at, json_dumps(evidence),
                    ),
                )
                participants.add((short_title, subject_id))
                counts["asset_participant_links"] += 1
                counts["crosswalk_evidence_rows"] += 1
    counts["datasets"] = len(datasets)
    counts["downloads"] = len(grouped)
    counts["participants"] = len(participants)
    return counts


def apply_reviewed_remind_nrrd_aggregate(conn: sqlite3.Connection) -> int:
    """Restore package-grain ReMIND counts after WordPress count refresh.

    WordPress's 113-image value describes whole-tumor segmentations, while the
    reviewed package inventory contains 356 NRRD files across 114 subjects.
    The original WordPress values remain in the download asset raw provenance.
    """
    file_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM public_non_dicom_assets "
            "WHERE short_title = ? AND file_format = 'NRRD' "
            "AND asset_granularity = 'file'",
            (REMIND_SHORT_TITLE,),
        ).fetchone()[0]
    )
    if not file_count:
        return 0
    before = conn.total_changes
    conn.execute(
        "UPDATE public_non_dicom_assets "
        "SET represented_file_count = ?, "
        "quality_flag_json = json_set("
        "COALESCE(NULLIF(quality_flag_json, ''), '{}'), "
        "'$.represented_file_count_source', 'reviewed_package_inventory') "
        "WHERE short_title = ? AND file_format = 'NRRD' "
        "AND asset_granularity = 'download'",
        (file_count, REMIND_SHORT_TITLE),
    )
    return conn.total_changes - before


def ingest_cptac_gbm_codex_inventory(
    conn: sqlite3.Connection,
    inventory_path: Path = DEFAULT_CPTAC_GBM_CODEX_INVENTORY,
    provenance_path: Path = DEFAULT_CPTAC_GBM_CODEX_PROVENANCE,
) -> int:
    """Ingest the reviewed 52-file package inventory without a legacy sidecar."""
    rows, provenance = load_cptac_gbm_codex_inventory(
        inventory_path, provenance_path
    )
    aggregate = conn.execute(
        """SELECT source_url, download_row_id FROM public_non_dicom_assets
           WHERE short_title = ? AND download_id = '48969'
             AND asset_granularity = 'download'
           ORDER BY asset_id LIMIT 1""",
        (CPTAC_GBM_CODEX_SHORT_TITLE,),
    ).fetchone()
    if aggregate is None:
        return 0
    source_url = str(aggregate[0] or "")
    system = managed_system_for_url(source_url)
    for row in rows:
        file_format = normalize_format(row.get("image_format") or row.get("file_ext"))
        asset_id = stable_id("asset", "pathology_package", row["non_dicom_file_id"])
        insert_asset(
            conn,
            {
                "asset_id": asset_id,
                "dataset_type": "Analysis Result",
                "short_title": CPTAC_GBM_CODEX_SHORT_TITLE,
                "download_row_id": aggregate["download_row_id"],
                "download_id": "48969",
                "subject_id": "",
                "subject_id_namespace": "",
                "participant_link_status": "unavailable",
                "asset_granularity": "file",
                "asset_name": row["file_name"],
                "file_name": row["file_name"],
                "package_path": row["package_path"],
                "file_format": file_format,
                "container_format": "",
                "media_kind": "whole_slide_image" if int(row.get("is_wsi") or 0) else "still_image",
                "spatial_dimensionality": "2D",
                "temporal_dimensionality": "static",
                "imaging_domain": "pathology",
                "modality": row.get("object_modality") or "",
                "object_role": row.get("file_role") or "source_image",
                "represented_file_count": 1,
                "size_bytes": int(row["bytes"]) if row.get("bytes") else None,
                "checksum": row.get("checksum") or "",
                "checksum_algorithm": row.get("checksum_algorithm") or "",
                "representation_provenance_class": default_representation_class(system),
                "source_system": system,
                "source_record_id": row["non_dicom_file_id"],
                "source_url": source_url,
                "raw_values_json": json_dumps(
                    {
                        "source_table": row.get("source_table") or "pathology_package_files",
                        "source_row_id": row.get("source_row_id") or "",
                        "source_file_ext": row.get("file_ext") or "",
                    }
                ),
                "provenance_json": json_dumps(
                    {
                        "source_artifact": "reviewed_cptac_gbm_codex_inventory",
                        "source_locator": str(inventory_path),
                        "reference_provenance": provenance,
                    }
                ),
                "quality_flag_json": "{}",
            },
        )
        if source_url:
            insert_location(
                conn,
                location_values(
                    asset_id,
                    source_url,
                    checksum=row.get("checksum") or "",
                    checksum_algorithm=row.get("checksum_algorithm") or "",
                    provenance={
                        "source_artifact": "reviewed_cptac_gbm_codex_inventory",
                        "package_path": row["package_path"],
                    },
                ),
            )
    if len(rows) != 52:
        raise RuntimeError(f"Expected 52 reviewed CODEX files, found {len(rows)}")
    return len(rows)


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


def apply_cptac_gbm_codex_workbook_crosswalk(
    conn: sqlite3.Connection,
    clinical_db: Path | None,
    *,
    require_pathdb: bool = True,
) -> dict[str, int]:
    """Resolve CODEX specimen labels and files to the 12 published patients.

    The official TCIA workbook supplies exact WSI/cropped filenames, the
    dataset-specific specimen or composite identifier, and either the parent
    UPENN patient ID or CPTAC patient ID. This projection is applied to both
    submitted package files and PathDB records. Raw specimen/timepoint labels
    remain in asset and crosswalk provenance.
    """
    empty = {
        "workbook_rows": 0,
        "workbook_files": 0,
        "participants": 0,
        "matched_assets": 0,
        "aspera_assets": 0,
        "pathdb_assets": 0,
        "evidence_rows": 0,
        "representation_links": 0,
    }
    if not clinical_db or not clinical_db.is_file():
        return empty
    if conn.execute(
        "SELECT 1 FROM public_non_dicom_assets WHERE short_title=? LIMIT 1",
        (CPTAC_GBM_CODEX_SHORT_TITLE,),
    ).fetchone() is None:
        return empty

    source_url = ""
    artifact_sha256 = ""
    reviewed_at = "2026-08-27"
    by_file: dict[str, dict[str, Any]] = {}
    participant_ids: set[str] = set()
    with closing(connect(clinical_db)) as clinical:
        if not table_exists(clinical, "clinical_rows"):
            return empty
        source = clinical.execute(
            """SELECT source_id, source_url, artifact_sha256
               FROM clinical_sources
               WHERE short_title = ? AND source_kind = 'tcia_clinical_download'
               ORDER BY source_priority DESC LIMIT 1""",
            (CPTAC_GBM_CODEX_SHORT_TITLE,),
        ).fetchone()
        if source is None:
            return empty
        source_url = str(source["source_url"] or "")
        artifact_sha256 = str(source["artifact_sha256"] or "")
        rows = clinical.execute(
            """SELECT source_row_id, subject_id, table_name, row_number, row_json,
                      row_sha256
               FROM clinical_rows
               WHERE source_id = ? ORDER BY table_name, row_number""",
            (source["source_id"],),
        ).fetchall()
        for row in rows:
            values = json.loads(str(row["row_json"] or "{}"))
            file_name = str(
                values.get("CPTAC-Glioblastoma-CODEX_WSI_filename")
                or values.get("CPTAC-Glioblastoma-CODEX_Cropped_filename")
                or ""
            ).strip()
            participants = codex_workbook_participants(values)
            if not file_name or not participants:
                raise RuntimeError(
                    "CPTAC-Glioblastoma-CODEX workbook row lacks a file or parent patient: "
                    f"{row['source_row_id']}"
                )
            key = codex_workbook_file_key(file_name)
            if key in by_file:
                raise RuntimeError(f"Duplicate CODEX workbook filename key: {key}")
            raw_subject_id = str(
                values.get("CPTAC-Glioblastoma-CODEX_PatientID")
                or row["subject_id"]
                or ""
            ).strip()
            by_file[key] = {
                "source_row_id": str(row["source_row_id"]),
                "source_row_number": int(row["row_number"]),
                "source_row_sha256": str(row["row_sha256"] or ""),
                "table_name": str(row["table_name"] or ""),
                "file_name": file_name,
                "raw_subject_id": raw_subject_id,
                "participants": participants,
                "image_type": values.get("Image_type"),
                "sample_type": values.get("Sample_type"),
                "slide_setup": values.get("Slide_setup"),
                "number_of_samples_on_slide": metadata_number(
                    values.get("Number_of_samples_on_slide")
                ),
            }
            participant_ids.update(participants)

    if len(by_file) != 52 or len(participant_ids) != 12:
        raise RuntimeError(
            "CPTAC-Glioblastoma-CODEX workbook contract changed: "
            f"files={len(by_file)} participants={len(participant_ids)}"
        )

    assets = conn.execute(
        """SELECT * FROM public_non_dicom_assets
           WHERE short_title = ? AND asset_granularity = 'file'
             AND source_system IN ('tcia_aspera', 'tcia_pathdb')
           ORDER BY source_system, asset_name""",
        (CPTAC_GBM_CODEX_SHORT_TITLE,),
    ).fetchall()
    matched_by_key: dict[str, dict[str, str]] = defaultdict(dict)
    counts = dict(empty)
    counts.update(
        {
            "workbook_rows": len(by_file),
            "workbook_files": len(by_file),
            "participants": len(participant_ids),
        }
    )
    mapping_method = "official_cptac_gbm_codex_workbook_filename_patient_projection"
    for asset in assets:
        key = codex_workbook_file_key(
            str(asset["file_name"] or asset["asset_name"] or "")
        )
        evidence_row = by_file.get(key)
        if evidence_row is None:
            raise RuntimeError(
                "CODEX public file is absent from the official workbook: "
                f"{asset['source_system']}:{asset['file_name'] or asset['asset_name']}"
            )
        participants = list(evidence_row["participants"])
        scalar = participants[0] if len(participants) == 1 else ""
        namespace = f"tcia_dataset:{CPTAC_GBM_CODEX_SHORT_TITLE}"
        provenance = json.loads(str(asset["provenance_json"] or "{}"))
        provenance["official_workbook_projection"] = {
            "source_url": source_url,
            "artifact_sha256": artifact_sha256,
            "source_row_id": evidence_row["source_row_id"],
            "source_file_name": evidence_row["file_name"],
            "raw_subject_id": evidence_row["raw_subject_id"],
            "resolved_patient_ids": participants,
            "mapping_method": mapping_method,
        }
        conn.execute(
            """UPDATE public_non_dicom_assets
               SET dataset_type = 'Analysis Result',
                   subject_id = ?, subject_id_namespace = ?,
                   participant_link_status = 'reviewed_source_crosswalk',
                   provenance_json = ?,
                   quality_flag_json = json_set(
                       COALESCE(NULLIF(quality_flag_json, ''), '{}'),
                       '$.participant_inventory', 'reviewed_source_crosswalk',
                       '$.raw_specimen_or_composite_id_preserved', 1
                   )
               WHERE asset_id = ?""",
            (scalar, namespace, json_dumps(provenance), asset["asset_id"]),
        )
        conn.execute(
            "DELETE FROM public_non_dicom_asset_participants WHERE asset_id = ?",
            (asset["asset_id"],),
        )
        for participant_id in participants:
            evidence = {
                "source_url": source_url,
                "artifact_sha256": artifact_sha256,
                "source_row_id": evidence_row["source_row_id"],
                "source_row_number": evidence_row["source_row_number"],
                "source_row_sha256": evidence_row["source_row_sha256"],
                "source_file_name": evidence_row["file_name"],
                "raw_subject_id": evidence_row["raw_subject_id"],
                "mapping_method": mapping_method,
            }
            insert_asset_participant(
                conn,
                asset_id=str(asset["asset_id"]),
                short_title=CPTAC_GBM_CODEX_SHORT_TITLE,
                subject_id=participant_id,
                namespace=namespace,
                raw_subject_id=str(evidence_row["raw_subject_id"]),
                participant_role="depicted_subject",
                link_status="reviewed_source_crosswalk",
                evidence=evidence,
            )
            conn.execute(
                """INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
                   VALUES (?, ?, ?, ?, ?, ?, 'high', ?, ?, '', ?, ?)""",
                (
                    stable_id(
                        "crosswalk", asset["asset_id"],
                        evidence_row["raw_subject_id"], participant_id, mapping_method,
                    ),
                    asset["asset_id"], CPTAC_GBM_CODEX_SHORT_TITLE,
                    evidence_row["raw_subject_id"], participant_id, mapping_method,
                    source_url,
                    "Exact official-workbook filename and parent patient fields resolve the specimen, timepoint, or composite slide label.",
                    reviewed_at, json_dumps(evidence),
                ),
            )
            counts["evidence_rows"] += 1
        merge_image_metadata(
            conn,
            str(asset["asset_id"]),
            {
                "image_type": evidence_row["image_type"],
                "sample_type": evidence_row["sample_type"],
                "slide_setup": evidence_row["slide_setup"],
                "number_of_samples_on_slide": evidence_row[
                    "number_of_samples_on_slide"
                ],
            },
            value_role="source_raw",
            source_kind="tcia_clinical_download",
            source_locator=f"{source_url}::{evidence_row['table_name'].rsplit('::', 1)[-1]}",
            inference_method="exact_official_workbook_filename",
            confidence="high",
            priority=110,
            evidence={
                "source_row_id": evidence_row["source_row_id"],
                "artifact_sha256": artifact_sha256,
            },
        )
        system = str(asset["source_system"])
        matched_by_key[key][system] = str(asset["asset_id"])
        counts["matched_assets"] += 1
        counts["aspera_assets" if system == "tcia_aspera" else "pathdb_assets"] += 1

    for key, systems in matched_by_key.items():
        expected_systems = (
            {"tcia_aspera", "tcia_pathdb"} if require_pathdb else {"tcia_aspera"}
        )
        if set(systems) != expected_systems:
            raise RuntimeError(f"CODEX file lacks an Aspera/PathDB pair: {key}")
        if not require_pathdb:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO public_non_dicom_asset_relationships
               VALUES (?, ?, ?, 'managed_representation_correspondence',
                       'source_supported', ?, 'curator_reviewed')""",
            (
                stable_id(
                    "relationship", systems["tcia_aspera"], systems["tcia_pathdb"],
                    "managed_representation_correspondence",
                ),
                systems["tcia_aspera"], systems["tcia_pathdb"],
                json_dumps(
                    {
                        "workbook_file_key": key,
                        "source_url": source_url,
                        "note": "The exact published filename links the managed records; byte identity is not asserted.",
                    }
                ),
            ),
        )
        counts["representation_links"] += 1

    expected_assets = len(by_file) * (2 if require_pathdb else 1)
    if counts["matched_assets"] != expected_assets:
        raise RuntimeError(
            "CPTAC-Glioblastoma-CODEX file coverage mismatch: "
            f"{counts['matched_assets']} != {expected_assets}"
        )
    add_dataset_metadata_note(
        conn,
        CPTAC_GBM_CODEX_SHORT_TITLE,
        "participant_identity",
        "official_workbook_specimen_to_patient_projection",
        "The official workbook resolves specimen/timepoint labels and composite slides to 12 parent patients across all 52 submitted package files and 52 PathDB records.",
        severity="info",
        status="resolved",
        affected_assets=counts["matched_assets"],
        evidence={
            "source_url": source_url,
            "artifact_sha256": artifact_sha256,
            "workbook_rows": len(by_file),
            "participant_count": len(participant_ids),
            "byte_identity_asserted": False,
        },
    )
    return counts


def ingest_reviewed_crosswalks(
    conn: sqlite3.Connection,
    crosswalk_csv: Path,
    curation_path: Path,
) -> dict[str, int]:
    if not crosswalk_csv.exists() or not curation_path.exists():
        return {"decisions": 0, "file_assets": 0, "evidence_rows": 0}

    curation = json.loads(curation_path.read_text())
    default_reviewed_at = str(curation.get("reviewed_at") or "")
    decisions = curation.get("decisions") or []
    decision_review_dates: dict[tuple[str, str, str], str] = {}
    for decision in decisions:
        download_ids = [str(value) for value in decision.get("download_ids") or []]
        decision_reviewed_at = str(decision.get("reviewed_at") or default_reviewed_at)
        for download_id in download_ids or [""]:
            decision_review_dates[
                (str(decision["dataset_type"]), str(decision["short_title"]), download_id)
            ] = decision_reviewed_at
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
                decision_reviewed_at,
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
            reviewed_at = decision_review_dates.get(
                (
                    source_row["dataset_type"], source_row["short_title"],
                    str(source_row["download_id"] or ""),
                ),
                default_reviewed_at,
            )
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


def apply_reviewed_path_contracts(
    conn: sqlite3.Connection,
    clinical_db: Path | None,
    curation_path: Path | None,
    pathology_db: Path | None = None,
) -> dict[str, int]:
    """Apply curator-approved path rules against official clinical IDs.

    These mappings are reviewed source contracts, not heuristic discovery.
    Each extracted identifier must resolve uniquely, case-insensitively, to an
    identifier in the dataset's official clinical participant list. Assets not
    covered by a rule remain unmodified and continue to surface for review.
    """
    empty = {
        "decisions": 0,
        "file_assets": 0,
        "compact_assets": 0,
        "represented_files": 0,
        "evidence_rows": 0,
        "unmatched_assets": 0,
        "unmatched_source_files": 0,
    }
    if not clinical_db or not clinical_db.exists() or not curation_path or not curation_path.exists():
        return empty

    curation = json.loads(curation_path.read_text())
    default_reviewed_at = str(curation.get("reviewed_at") or "")
    decisions = [
        item for item in curation.get("decisions") or []
        if item.get("resolution_type") == "participant_path_contract"
    ]
    if not decisions:
        return empty

    matched_assets = compact_assets = represented_files = 0
    evidence_rows = unmatched_assets = unmatched_source_files = 0
    with closing(connect(clinical_db)) as clinical:
        if not table_exists(clinical, "agent_clinical_all_subjects"):
            return empty
        for decision in decisions:
            dataset_type = str(decision["dataset_type"])
            short_title = str(decision["short_title"])
            download_ids = [str(value) for value in decision.get("download_ids") or []]
            reviewed_at = str(decision.get("reviewed_at") or default_reviewed_at)
            rules = [
                (str(rule["name"]), re.compile(str(rule["pattern"]), re.IGNORECASE))
                for rule in decision.get("path_rules") or []
            ]
            compact_rules = [
                (rule, re.compile(str(rule["pattern"]), re.IGNORECASE))
                for rule in decision.get("compact_path_rules") or []
            ]
            identifiers: dict[str, set[str]] = defaultdict(set)
            for row in clinical.execute(
                """
                SELECT subject_id
                FROM agent_clinical_all_subjects
                WHERE short_title = ? AND NULLIF(trim(subject_id), '') IS NOT NULL
                """,
                (short_title,),
            ):
                raw_identifier = str(row["subject_id"]).strip()
                identifiers[raw_identifier.casefold()].add(raw_identifier)

            placeholders = ",".join("?" for _ in download_ids)
            download_clause = (
                f"AND COALESCE(download_id, '') IN ({placeholders})" if download_ids else ""
            )
            assets = conn.execute(
                f"""
                SELECT *
                FROM public_non_dicom_assets
                WHERE dataset_type = ? AND short_title = ?
                  AND asset_granularity = 'file'
                  AND participant_link_status IN ('dataset_only', 'unavailable')
                  {download_clause}
                ORDER BY package_path, file_name
                """,
                (dataset_type, short_title, *download_ids),
            ).fetchall()
            decision_id = stable_id(
                "crosswalk_decision", dataset_type, short_title,
                decision["resolution_type"], *download_ids,
            )
            decision_matched = decision_unmatched = 0
            decision_compact_assets = decision_represented_files = 0
            decision_unmatched_source_files = 0
            for asset in assets:
                path = str(asset["package_path"] or asset["file_name"] or "")
                matches: set[tuple[str, str]] = set()
                for rule_name, pattern in rules:
                    match = pattern.fullmatch(path)
                    if not match:
                        continue
                    extracted = str(match.groupdict().get("participant_id") or "").strip()
                    canonical = identifiers.get(extracted.casefold(), set())
                    if len(canonical) == 1:
                        matches.add((rule_name, next(iter(canonical))))
                resolved_ids = {item[1] for item in matches}
                if len(resolved_ids) != 1:
                    decision_unmatched += 1
                    continue
                subject_id = next(iter(resolved_ids))
                mapping_methods = sorted(item[0] for item in matches if item[1] == subject_id)
                mapping_method = "reviewed_wordpress_path_contract:" + "+".join(mapping_methods)
                crosswalk_id = stable_id("crosswalk", asset["asset_id"], mapping_method, reviewed_at)
                conn.execute(
                    """
                    UPDATE public_non_dicom_assets
                    SET subject_id = ?, subject_id_namespace = ?,
                        participant_link_status = 'reviewed_source_crosswalk',
                        raw_values_json = json_set(
                            COALESCE(NULLIF(raw_values_json, ''), '{}'),
                            '$.reviewed_path_contract.raw_subject_id', ?
                        ),
                        quality_flag_json = json_set(
                            COALESCE(NULLIF(quality_flag_json, ''), '{}'),
                            '$.participant_inventory', 'reviewed_source_crosswalk',
                            '$.crosswalk_decision_id', ?
                        )
                    WHERE asset_id = ?
                    """,
                    (
                        subject_id, f"tcia_dataset:{short_title}", subject_id,
                        decision_id, asset["asset_id"],
                    ),
                )
                conn.execute(
                    "DELETE FROM public_non_dicom_asset_participants WHERE asset_id = ?",
                    (asset["asset_id"],),
                )
                insert_asset_participant(
                    conn,
                    asset_id=str(asset["asset_id"]),
                    short_title=short_title,
                    subject_id=subject_id,
                    namespace=f"tcia_dataset:{short_title}",
                    raw_subject_id=subject_id,
                    participant_role="depicted_subject",
                    link_status="reviewed_source_crosswalk",
                    evidence={"decision_id": decision_id, "mapping_method": mapping_method},
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
                    VALUES (?, ?, ?, ?, ?, ?, 'high', ?, ?, '', ?, ?)
                    """,
                    (
                        crosswalk_id, asset["asset_id"], short_title, subject_id, subject_id,
                        mapping_method, decision.get("evidence_url") or "",
                        decision.get("reviewer_note") or "", reviewed_at,
                        json_dumps({
                            "decision_id": decision_id,
                            "identifier_source": decision.get("identifier_source") or "",
                            "identifier_source_url": decision.get("identifier_source_url") or "",
                            "package_path": path,
                        }),
                    ),
                )
                decision_matched += 1
            decision_unmatched += len(assets) - decision_matched - decision_unmatched

            if compact_rules and pathology_db and pathology_db.exists():
                grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
                with closing(connect(pathology_db)) as pathology:
                    if table_exists(pathology, "agent_pathology_file_objects"):
                        urls: dict[tuple[str, str], str] = {}
                        if table_exists(pathology, "agent_pathology_downloads"):
                            for row in pathology.execute(
                                "SELECT short_title, download_id, download_url FROM agent_pathology_downloads"
                            ):
                                urls[(str(row["short_title"]), str(row["download_id"] or ""))] = str(
                                    row["download_url"] or ""
                                )
                        source_rows = pathology.execute(
                            """
                            SELECT * FROM agent_pathology_file_objects
                            WHERE dataset_type = ? AND short_title = ?
                            ORDER BY package_path
                            """,
                            (dataset_type, short_title),
                        )
                        for source_row in source_rows:
                            source_download_id = str(source_row["download_id"] or "")
                            if download_ids and source_download_id not in download_ids:
                                continue
                            path = str(source_row["package_path"] or source_row["file_name"] or "")
                            source_format = normalize_format(
                                source_row["image_format"] or source_row["file_ext"]
                            )
                            source_matches: set[tuple[str, str, str]] = set()
                            matched_rule: dict[str, Any] | None = None
                            for rule, pattern in compact_rules:
                                if source_row["is_metadata"] and not rule.get("include_metadata"):
                                    continue
                                if source_format != normalize_format(rule.get("source_file_format")):
                                    continue
                                match = pattern.fullmatch(path)
                                if not match:
                                    continue
                                extracted = str(match.groupdict().get("participant_id") or "").strip()
                                canonical = identifiers.get(extracted.casefold(), set())
                                if len(canonical) == 1:
                                    subject_id = next(iter(canonical))
                                    source_matches.add((str(rule["name"]), subject_id, extracted))
                                    matched_rule = rule
                            resolved_ids = {item[1] for item in source_matches}
                            if len(resolved_ids) != 1 or matched_rule is None:
                                if any(pattern.fullmatch(path) for _, pattern in compact_rules):
                                    decision_unmatched_source_files += 1
                                continue
                            subject_id = next(iter(resolved_ids))
                            rule_name = sorted(item[0] for item in source_matches if item[1] == subject_id)[0]
                            group_key = (
                                source_download_id,
                                subject_id,
                                str(matched_rule.get("asset_group") or rule_name),
                            )
                            group = grouped.setdefault(group_key, {
                                "rule": matched_rule,
                                "rule_name": rule_name,
                                "file_count": 0,
                                "size_bytes": 0,
                                "known_size_count": 0,
                                "sample_path": path,
                                "source_url": urls.get((short_title, source_download_id), ""),
                            })
                            group["file_count"] += 1
                            if source_row["bytes"] is not None:
                                group["size_bytes"] += int(source_row["bytes"])
                                group["known_size_count"] += 1

                for (source_download_id, subject_id, asset_group), group in sorted(grouped.items()):
                    rule = group["rule"]
                    mapping_method = "reviewed_wordpress_path_contract:" + group["rule_name"]
                    asset_id = stable_id(
                        "asset", "reviewed_compact_path_group", dataset_type, short_title,
                        source_download_id, subject_id, asset_group,
                    )
                    source_url = str(group["source_url"])
                    represented_count = int(group["file_count"])
                    insert_asset(conn, {
                        "asset_id": asset_id,
                        "dataset_type": dataset_type,
                        "short_title": short_title,
                        "download_id": source_download_id,
                        "subject_id": subject_id,
                        "subject_id_namespace": f"tcia_dataset:{short_title}",
                        "participant_link_status": "reviewed_source_crosswalk",
                        "asset_granularity": "participant_file_group",
                        "asset_name": f"{subject_id} {rule.get('asset_label') or asset_group}",
                        "file_name": "",
                        "package_path": str(group["sample_path"]).rsplit("/", 2)[0] + "/",
                        "file_format": normalize_format(rule.get("source_file_format")),
                        "container_format": "",
                        "media_kind": rule.get("media_kind") or "unknown",
                        "spatial_dimensionality": rule.get("spatial_dimensionality") or "unknown",
                        "temporal_dimensionality": rule.get("temporal_dimensionality") or "unknown",
                        "imaging_domain": rule.get("imaging_domain") or "unknown",
                        "modality": rule.get("modality") or "",
                        "object_role": rule.get("object_role") or "derived_asset",
                        "represented_file_count": represented_count,
                        "size_bytes": group["size_bytes"] if group["known_size_count"] == represented_count else None,
                        "checksum": "",
                        "checksum_algorithm": "",
                        "representation_provenance_class": (
                            rule.get("representation_provenance_class") or "derived_asset"
                        ),
                        "source_system": managed_system_for_url(source_url),
                        "source_record_id": f"{source_download_id}:{subject_id}:{asset_group}",
                        "source_url": source_url,
                        "raw_values_json": json_dumps({
                            "represented_file_count": represented_count,
                            "sample_package_path": group["sample_path"],
                            "source_file_format": normalize_format(rule.get("source_file_format")),
                        }),
                        "provenance_json": json_dumps({
                            "source_artifact": "pathology_metadata",
                            "source_view": "agent_pathology_file_objects",
                            "projection": "reviewed_compact_path_group",
                            "decision_id": decision_id,
                            "mapping_method": mapping_method,
                        }),
                        "quality_flag_json": json_dumps({
                            "participant_inventory": "reviewed_source_crosswalk",
                            "file_detail_pointer": "pathology_metadata.agent_pathology_file_objects",
                        }),
                    })
                    insert_asset_participant(
                        conn,
                        asset_id=asset_id,
                        short_title=short_title,
                        subject_id=subject_id,
                        namespace=f"tcia_dataset:{short_title}",
                        raw_subject_id=subject_id,
                        participant_role="depicted_subject",
                        link_status="reviewed_source_crosswalk",
                        evidence={"decision_id": decision_id, "mapping_method": mapping_method},
                    )
                    if source_url:
                        insert_location(conn, location_values(
                            asset_id,
                            source_url,
                            provenance={
                                "source_artifact": "pathology_metadata",
                                "projection": "reviewed_compact_path_group",
                            },
                        ))
                    crosswalk_id = stable_id("crosswalk", asset_id, mapping_method, reviewed_at)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
                        VALUES (?, ?, ?, ?, ?, ?, 'high', ?, ?, '', ?, ?)
                        """,
                        (
                            crosswalk_id, asset_id, short_title, subject_id, subject_id,
                            mapping_method, decision.get("evidence_url") or "",
                            decision.get("reviewer_note") or "", reviewed_at,
                            json_dumps({
                                "decision_id": decision_id,
                                "identifier_source": decision.get("identifier_source") or "",
                                "identifier_source_url": decision.get("identifier_source_url") or "",
                                "sample_package_path": group["sample_path"],
                                "represented_file_count": represented_count,
                            }),
                        ),
                    )
                    decision_compact_assets += 1
                    decision_represented_files += represented_count
                    evidence_rows += 1
            matched_assets += decision_matched
            compact_assets += decision_compact_assets
            represented_files += decision_represented_files
            evidence_rows += decision_matched
            unmatched_assets += decision_unmatched
            unmatched_source_files += decision_unmatched_source_files
            conn.execute(
                """
                INSERT OR REPLACE INTO public_non_dicom_crosswalk_decisions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id, dataset_type, short_title, json_dumps(download_ids),
                    decision["decision_status"], decision["resolution_type"],
                    decision.get("reviewer_note") or "", decision.get("evidence_url") or "",
                    reviewed_at,
                    json_dumps({
                        "source_file": str(curation_path),
                        "review_source": curation.get("review_source"),
                        "identifier_source": decision.get("identifier_source") or "",
                        "identifier_source_url": decision.get("identifier_source_url") or "",
                        "path_rules": decision.get("path_rules") or [],
                        "compact_path_rules": decision.get("compact_path_rules") or [],
                        "matched_assets": decision_matched,
                        "compact_assets": decision_compact_assets,
                        "represented_files": decision_represented_files,
                        "unmatched_assets": decision_unmatched,
                        "unmatched_source_files": decision_unmatched_source_files,
                    }),
                ),
            )
    return {
        "decisions": len(decisions),
        "file_assets": matched_assets,
        "compact_assets": compact_assets,
        "represented_files": represented_files,
        "evidence_rows": evidence_rows,
        "unmatched_assets": unmatched_assets,
        "unmatched_source_files": unmatched_source_files,
    }


def apply_reviewed_pathdb_contracts(
    conn: sqlite3.Connection,
    curation_path: Path | None,
) -> dict[str, int]:
    """Apply reviewed filename and converted-PathDB participant contracts.

    These contracts are useful when a published README defines a filename
    subject token, or when a renamed split can be joined to PathDB by an exact
    relative path while preserving the distinction between the Aspera original
    and PathDB's converted representation.
    """
    empty = {
        "decisions": 0,
        "file_assets": 0,
        "evidence_rows": 0,
        "source_confirmed_unavailable_assets": 0,
        "pathdb_non_participant_assets": 0,
        "unmatched_assets": 0,
    }
    if not curation_path or not curation_path.exists():
        return empty

    curation = json.loads(curation_path.read_text())
    default_reviewed_at = str(curation.get("reviewed_at") or "")
    decisions = [
        item for item in curation.get("decisions") or []
        if item.get("resolution_type") == "participant_pathdb_contract"
    ]
    if not decisions:
        return empty

    matched_assets = evidence_rows = unavailable_assets = 0
    pathdb_non_participant_assets = unmatched_assets = 0
    for decision in decisions:
        dataset_type = str(decision["dataset_type"])
        short_title = str(decision["short_title"])
        download_ids = [str(value) for value in decision.get("download_ids") or []]
        reviewed_at = str(decision.get("reviewed_at") or default_reviewed_at)
        decision_id = stable_id(
            "crosswalk_decision", dataset_type, short_title,
            decision["resolution_type"], *download_ids,
        )

        non_participant_ids = {
            str(value).casefold() for value in decision.get("pathdb_non_participant_ids") or []
        }
        decision_pathdb_non_participants = 0
        if non_participant_ids:
            for pathdb_asset in conn.execute(
                """
                SELECT asset_id, subject_id
                FROM public_non_dicom_assets
                WHERE dataset_type = ? AND short_title = ?
                  AND source_system = 'tcia_pathdb'
                  AND NULLIF(trim(COALESCE(subject_id, '')), '') IS NOT NULL
                """,
                (dataset_type, short_title),
            ).fetchall():
                raw_subject_id = str(pathdb_asset["subject_id"])
                if raw_subject_id.casefold() not in non_participant_ids:
                    continue
                conn.execute(
                    """
                    UPDATE public_non_dicom_assets
                    SET subject_id = '', subject_id_namespace = '',
                        participant_link_status = 'source_confirmed_unavailable',
                        raw_values_json = json_set(
                            COALESCE(NULLIF(raw_values_json, ''), '{}'),
                            '$.pathdb_non_participant_label', ?
                        ),
                        quality_flag_json = json_set(
                            COALESCE(NULLIF(quality_flag_json, ''), '{}'),
                            '$.participant_inventory', 'source_confirmed_unavailable',
                            '$.crosswalk_decision_id', ?,
                            '$.source_unavailable_reason', 'pathdb_placeholder_is_not_participant'
                        )
                    WHERE asset_id = ?
                    """,
                    (raw_subject_id, decision_id, pathdb_asset["asset_id"]),
                )
                conn.execute(
                    "DELETE FROM public_non_dicom_asset_participants WHERE asset_id = ?",
                    (pathdb_asset["asset_id"],),
                )
                decision_pathdb_non_participants += 1

        identifiers: dict[str, set[str]] = defaultdict(set)
        for row in conn.execute(
            """
            SELECT DISTINCT subject_id FROM (
                SELECT a.subject_id AS subject_id
                FROM public_non_dicom_assets a
                WHERE a.dataset_type = ? AND a.short_title = ?
                  AND a.source_system = 'tcia_pathdb'
                UNION ALL
                SELECT ap.subject_id AS subject_id
                FROM public_non_dicom_asset_participants ap
                JOIN public_non_dicom_assets a USING (asset_id)
                WHERE a.dataset_type = ? AND a.short_title = ?
                  AND a.source_system = 'tcia_pathdb'
            )
            WHERE NULLIF(trim(COALESCE(subject_id, '')), '') IS NOT NULL
            """,
            (dataset_type, short_title, dataset_type, short_title),
        ):
            subject_id = str(row["subject_id"]).strip()
            identifiers[subject_id.casefold()].add(subject_id)

        path_rules = [
            (rule, re.compile(str(rule["pattern"]), re.IGNORECASE))
            for rule in decision.get("path_rules") or []
        ]
        pathdb_rules = [
            (rule, re.compile(str(rule["pattern"]), re.IGNORECASE))
            for rule in decision.get("pathdb_file_rules") or []
        ]
        pathdb_asset_rules = [
            (rule, re.compile(str(rule["pattern"]), re.IGNORECASE))
            for rule in decision.get("pathdb_asset_rules") or []
        ]
        unavailable_rules = [
            (rule, re.compile(str(rule["pattern"]), re.IGNORECASE))
            for rule in decision.get("source_unavailable_rules") or []
        ]

        pathdb_relative: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        if pathdb_rules:
            pathdb_assets = conn.execute(
                """
                SELECT asset_id, subject_id, source_url
                FROM public_non_dicom_assets
                WHERE dataset_type = ? AND short_title = ?
                  AND source_system = 'tcia_pathdb'
                  AND NULLIF(trim(COALESCE(subject_id, '')), '') IS NOT NULL
                  AND NULLIF(trim(COALESCE(source_url, '')), '') IS NOT NULL
                """,
                (dataset_type, short_title),
            ).fetchall()
            for rule, _ in pathdb_rules:
                rule_name = str(rule["name"])
                marker = str(rule["pathdb_url_marker"]).casefold()
                for pathdb_asset in pathdb_assets:
                    url = str(pathdb_asset["source_url"] or "")
                    position = url.casefold().find(marker)
                    if position < 0:
                        continue
                    relative_path = url[position + len(marker):].lstrip("/").casefold()
                    pathdb_relative[(rule_name, relative_path)].append(pathdb_asset)

        pathdb_named_assets: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        if pathdb_asset_rules:
            pathdb_assets = conn.execute(
                """
                SELECT asset_id, asset_name, file_name, subject_id
                FROM public_non_dicom_assets
                WHERE dataset_type = ? AND short_title = ?
                  AND source_system = 'tcia_pathdb'
                """,
                (dataset_type, short_title),
            ).fetchall()
            for rule, _ in pathdb_asset_rules:
                rule_name = str(rule["name"])
                for pathdb_asset in pathdb_assets:
                    asset_name = str(
                        pathdb_asset["asset_name"] or pathdb_asset["file_name"] or ""
                    ).strip()
                    if asset_name:
                        pathdb_named_assets[(rule_name, asset_name.casefold())].append(pathdb_asset)

        placeholders = ",".join("?" for _ in download_ids)
        download_clause = (
            f"AND COALESCE(download_id, '') IN ({placeholders})" if download_ids else ""
        )
        assets = conn.execute(
            f"""
            SELECT *
            FROM public_non_dicom_assets
            WHERE dataset_type = ? AND short_title = ?
              AND source_system = 'tcia_aspera'
              AND asset_granularity = 'file'
              AND participant_link_status IN ('dataset_only', 'unavailable')
              {download_clause}
            ORDER BY package_path, file_name
            """,
            (dataset_type, short_title, *download_ids),
        ).fetchall()

        decision_matched = decision_evidence = decision_unavailable = decision_unmatched = 0
        for asset in assets:
            path = str(asset["package_path"] or asset["file_name"] or "")
            unavailable_rule = next(
                (rule for rule, pattern in unavailable_rules if pattern.fullmatch(path)),
                None,
            )
            if unavailable_rule is not None:
                conn.execute(
                    """
                    UPDATE public_non_dicom_assets
                    SET participant_link_status = 'source_confirmed_unavailable',
                        quality_flag_json = json_set(
                            COALESCE(NULLIF(quality_flag_json, ''), '{}'),
                            '$.participant_inventory', 'source_confirmed_unavailable',
                            '$.crosswalk_decision_id', ?,
                            '$.source_unavailable_reason', ?
                        )
                    WHERE asset_id = ?
                    """,
                    (
                        decision_id,
                        unavailable_rule.get("reason") or "participant_file_mapping_not_published",
                        asset["asset_id"],
                    ),
                )
                decision_unavailable += 1
                continue

            named_matches: list[tuple[dict[str, Any], sqlite3.Row, dict[str, str]]] = []
            for rule, pattern in pathdb_asset_rules:
                match = pattern.fullmatch(path)
                if not match:
                    continue
                values = {key: str(value or "") for key, value in match.groupdict().items()}
                pathdb_asset_name = str(rule["pathdb_asset_name_template"]).format_map(values)
                pathdb_matches = pathdb_named_assets.get(
                    (str(rule["name"]), pathdb_asset_name.casefold()),
                    [],
                )
                if len(pathdb_matches) == 1:
                    named_matches.append((rule, pathdb_matches[0], values))

            named_asset_ids = {str(item[1]["asset_id"]) for item in named_matches}
            if named_matches and len(named_asset_ids) != 1:
                decision_unmatched += 1
                continue
            if len(named_asset_ids) == 1:
                rule, pathdb_asset, values = sorted(
                    named_matches,
                    key=lambda item: str(item[0]["name"]),
                )[0]
                participant_rows = conn.execute(
                    """
                    SELECT subject_id, raw_subject_id, participant_role
                    FROM public_non_dicom_asset_participants
                    WHERE asset_id = ?
                    ORDER BY subject_id, raw_subject_id
                    """,
                    (pathdb_asset["asset_id"],),
                ).fetchall()
                if not participant_rows and str(pathdb_asset["subject_id"] or "").strip():
                    participant_rows = [{
                        "subject_id": str(pathdb_asset["subject_id"]),
                        "raw_subject_id": str(pathdb_asset["subject_id"]),
                        "participant_role": "depicted_subject",
                    }]
                resolved_ids = {str(row["subject_id"]) for row in participant_rows}
                if not resolved_ids:
                    decision_unmatched += 1
                    continue

                rule_name = str(rule["name"])
                mapping_method = f"reviewed_pathdb_asset_contract:{rule_name}"
                scalar_subject_id = next(iter(resolved_ids)) if len(resolved_ids) == 1 else ""
                conn.execute(
                    """
                    UPDATE public_non_dicom_assets
                    SET subject_id = ?, subject_id_namespace = ?,
                        participant_link_status = 'reviewed_source_crosswalk',
                        raw_values_json = json_set(
                            COALESCE(NULLIF(raw_values_json, ''), '{}'),
                            '$.reviewed_pathdb_asset_contract.pathdb_asset_id', ?,
                            '$.reviewed_pathdb_asset_contract.pathdb_asset_name', ?,
                            '$.reviewed_pathdb_asset_contract.participant_count', ?
                        ),
                        quality_flag_json = json_set(
                            COALESCE(NULLIF(quality_flag_json, ''), '{}'),
                            '$.participant_inventory', 'reviewed_source_crosswalk',
                            '$.crosswalk_decision_id', ?,
                            '$.representation_equivalence', 'not_asserted'
                        )
                    WHERE asset_id = ?
                    """,
                    (
                        scalar_subject_id, f"tcia_dataset:{short_title}",
                        pathdb_asset["asset_id"],
                        pathdb_asset["asset_name"] or pathdb_asset["file_name"],
                        len(resolved_ids), decision_id, asset["asset_id"],
                    ),
                )
                conn.execute(
                    "DELETE FROM public_non_dicom_asset_participants WHERE asset_id = ?",
                    (asset["asset_id"],),
                )
                for participant_row in participant_rows:
                    subject_id = str(participant_row["subject_id"])
                    raw_subject_id = str(participant_row["raw_subject_id"] or subject_id)
                    participant_role = str(
                        participant_row["participant_role"] or "depicted_subject"
                    )
                    insert_asset_participant(
                        conn,
                        asset_id=str(asset["asset_id"]),
                        short_title=short_title,
                        subject_id=subject_id,
                        namespace=f"tcia_dataset:{short_title}",
                        raw_subject_id=raw_subject_id,
                        participant_role=participant_role,
                        link_status="reviewed_source_crosswalk",
                        evidence={
                            "decision_id": decision_id,
                            "mapping_method": mapping_method,
                            "pathdb_asset_id": str(pathdb_asset["asset_id"]),
                        },
                    )
                    crosswalk_id = stable_id(
                        "crosswalk", asset["asset_id"], subject_id, mapping_method, reviewed_at,
                    )
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
                        VALUES (?, ?, ?, ?, ?, ?, 'high', ?, ?, '', ?, ?)
                        """,
                        (
                            crosswalk_id, asset["asset_id"], short_title, raw_subject_id,
                            subject_id, mapping_method, decision.get("evidence_url") or "",
                            decision.get("reviewer_note") or "", reviewed_at,
                            json_dumps({
                                "decision_id": decision_id,
                                "package_path": path,
                                "path_groups": values,
                                "pathdb_asset_id": str(pathdb_asset["asset_id"]),
                                "pathdb_asset_name": str(
                                    pathdb_asset["asset_name"] or pathdb_asset["file_name"]
                                ),
                                "representation_equivalence": "not_asserted",
                            }),
                        ),
                    )
                    decision_evidence += 1
                decision_matched += 1
                continue

            matches: list[tuple[str, str, str, dict[str, Any]]] = []
            for rule, pattern in path_rules:
                match = pattern.fullmatch(path)
                if not match:
                    continue
                values = {key: str(value or "") for key, value in match.groupdict().items()}
                extracted = str(rule["participant_id_template"]).format_map(values)
                canonical = identifiers.get(extracted.casefold(), set())
                if len(canonical) == 1:
                    matches.append((
                        str(rule["name"]), next(iter(canonical)), extracted,
                        {
                            "package_path": path,
                            "extracted_subject_id": extracted,
                            "path_groups": values,
                        },
                    ))

            for rule, pattern in pathdb_rules:
                match = pattern.fullmatch(path)
                if not match:
                    continue
                values = {key: str(value or "") for key, value in match.groupdict().items()}
                relative_path = str(rule["pathdb_relative_path_template"]).format_map(values)
                pathdb_matches = pathdb_relative.get(
                    (str(rule["name"]), relative_path.casefold()),
                    [],
                )
                resolved = {str(row["subject_id"]) for row in pathdb_matches}
                if len(pathdb_matches) == 1 and len(resolved) == 1:
                    subject_id = next(iter(resolved))
                    if subject_id.casefold() not in {
                        str(value).casefold() for value in rule.get("excluded_subject_ids") or []
                    }:
                        matches.append((
                            str(rule["name"]), subject_id, subject_id,
                            {
                                "package_path": path,
                                "pathdb_relative_path": relative_path,
                                "pathdb_asset_id": str(pathdb_matches[0]["asset_id"]),
                                "pathdb_source_url": str(pathdb_matches[0]["source_url"]),
                            },
                        ))

            resolved_ids = {item[1] for item in matches}
            if len(resolved_ids) != 1:
                decision_unmatched += 1
                continue
            subject_id = next(iter(resolved_ids))
            selected = sorted(
                (item for item in matches if item[1] == subject_id),
                key=lambda item: item[0],
            )
            raw_subject_id = selected[0][2]
            rule_names = sorted({item[0] for item in selected})
            mapping_method = "reviewed_pdf_path_contract:" + "+".join(rule_names)
            crosswalk_id = stable_id("crosswalk", asset["asset_id"], mapping_method, reviewed_at)
            conn.execute(
                """
                UPDATE public_non_dicom_assets
                SET subject_id = ?, subject_id_namespace = ?,
                    participant_link_status = 'reviewed_source_crosswalk',
                    raw_values_json = json_set(
                        COALESCE(NULLIF(raw_values_json, ''), '{}'),
                        '$.reviewed_path_contract.raw_subject_id', ?
                    ),
                    quality_flag_json = json_set(
                        COALESCE(NULLIF(quality_flag_json, ''), '{}'),
                        '$.participant_inventory', 'reviewed_source_crosswalk',
                        '$.crosswalk_decision_id', ?
                    )
                WHERE asset_id = ?
                """,
                (
                    subject_id, f"tcia_dataset:{short_title}", raw_subject_id,
                    decision_id, asset["asset_id"],
                ),
            )
            conn.execute(
                "DELETE FROM public_non_dicom_asset_participants WHERE asset_id = ?",
                (asset["asset_id"],),
            )
            insert_asset_participant(
                conn,
                asset_id=str(asset["asset_id"]),
                short_title=short_title,
                subject_id=subject_id,
                namespace=f"tcia_dataset:{short_title}",
                raw_subject_id=raw_subject_id,
                participant_role="depicted_subject",
                link_status="reviewed_source_crosswalk",
                evidence={"decision_id": decision_id, "mapping_method": mapping_method},
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO public_non_dicom_crosswalk_evidence
                VALUES (?, ?, ?, ?, ?, ?, 'high', ?, ?, '', ?, ?)
                """,
                (
                    crosswalk_id, asset["asset_id"], short_title, raw_subject_id,
                    subject_id, mapping_method, decision.get("evidence_url") or "",
                    decision.get("reviewer_note") or "", reviewed_at,
                    json_dumps({
                        "decision_id": decision_id,
                        "matching_evidence": [item[3] for item in selected],
                        "representation_equivalence": "not_asserted",
                    }),
                ),
            )
            decision_matched += 1
            decision_evidence += 1

        matched_assets += decision_matched
        evidence_rows += decision_evidence
        unavailable_assets += decision_unavailable
        pathdb_non_participant_assets += decision_pathdb_non_participants
        unmatched_assets += decision_unmatched
        conn.execute(
            """
            INSERT OR REPLACE INTO public_non_dicom_crosswalk_decisions
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id, dataset_type, short_title, json_dumps(download_ids),
                decision["decision_status"], decision["resolution_type"],
                decision.get("reviewer_note") or "", decision.get("evidence_url") or "",
                reviewed_at,
                json_dumps({
                    "source_file": str(curation_path),
                    "review_source": curation.get("review_source"),
                    "path_rules": decision.get("path_rules") or [],
                    "pathdb_file_rules": decision.get("pathdb_file_rules") or [],
                    "pathdb_asset_rules": decision.get("pathdb_asset_rules") or [],
                    "source_unavailable_rules": decision.get("source_unavailable_rules") or [],
                    "pathdb_non_participant_ids": decision.get("pathdb_non_participant_ids") or [],
                    "matched_assets": decision_matched,
                    "source_confirmed_unavailable_assets": decision_unavailable,
                    "pathdb_non_participant_assets": decision_pathdb_non_participants,
                    "unmatched_assets": decision_unmatched,
                }),
            ),
        )

    return {
        "decisions": len(decisions),
        "file_assets": matched_assets,
        "evidence_rows": evidence_rows,
        "source_confirmed_unavailable_assets": unavailable_assets,
        "pathdb_non_participant_assets": pathdb_non_participant_assets,
        "unmatched_assets": unmatched_assets,
    }


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


def refresh_participant_crosswalk_review_issues(conn: sqlite3.Connection) -> int:
    before = conn.total_changes
    conn.execute(
        "DELETE FROM public_non_dicom_review_issues "
        "WHERE issue_code = 'participant_file_crosswalk_unavailable'"
    )
    add_review_issues(conn)
    return conn.total_changes - before


def mark_downloads_with_linked_file_grain(conn: sqlite3.Connection) -> int:
    """Resolve parent download placeholders when linked child files exist.

    NIfTI file metadata can retain ``download_id`` as a JSON array because one
    file record may be associated with more than one WordPress download.  The
    WordPress parent asset stores one scalar download ID.  Compare both forms
    so a dataset-level parent is not reported as an unlinked asset when the
    participant crosswalk is already available on its file rows.
    """
    before = conn.total_changes
    conn.execute(
        """
        UPDATE public_non_dicom_assets AS d
        SET participant_link_status = 'crosswalk_available_at_file_grain',
            quality_flag_json = json_set(
                COALESCE(NULLIF(d.quality_flag_json, ''), '{}'),
                '$.participant_inventory', 'crosswalk_available_at_file_grain'
            )
        WHERE d.asset_granularity = 'download'
          AND d.participant_link_status IN ('dataset_only', 'unavailable')
          AND NULLIF(trim(d.download_id), '') IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM public_non_dicom_assets f
              JOIN public_non_dicom_asset_participants ap
                ON ap.asset_id = f.asset_id
              WHERE f.dataset_type = d.dataset_type
                AND f.short_title = d.short_title
                AND f.file_format = d.file_format
                AND f.asset_granularity = 'file'
                AND (
                    COALESCE(f.download_id, '') = COALESCE(d.download_id, '')
                    OR EXISTS (
                        SELECT 1
                        FROM json_each(
                            CASE
                                WHEN json_valid(f.download_id) THEN f.download_id
                                ELSE '[]'
                            END
                        ) AS linked_download
                        WHERE CAST(linked_download.value AS TEXT) = CAST(d.download_id AS TEXT)
                    )
                )
          )
        """
    )
    return conn.total_changes - before


def delete_refreshable_assets(conn: sqlite3.Connection) -> int:
    """Remove source scopes that are rebuilt from current native V2 inputs."""
    conn.execute("DROP TABLE IF EXISTS temp.refresh_asset_ids")
    conn.execute("CREATE TEMP TABLE refresh_asset_ids(asset_id TEXT PRIMARY KEY)")
    conn.execute(
        "INSERT INTO refresh_asset_ids "
        "SELECT asset_id FROM public_non_dicom_assets "
        "WHERE (source_system IN ('tcia_wordpress', 'tcia_aspera') "
        "       AND asset_granularity='download') "
        "   OR source_record_id LIKE 'tcga-lgg-mask-%' "
        "   OR source_record_id LIKE 'tcga-lgg-mask-inventory:%' "
        "   OR source_record_id LIKE 'reviewed-participant-inventory:%' "
        "   OR source_system='tcia_pathdb'"
    )
    count = int(conn.execute("SELECT COUNT(*) FROM refresh_asset_ids").fetchone()[0])
    conn.execute(
        "DELETE FROM public_non_dicom_asset_relationships "
        "WHERE source_asset_id IN (SELECT asset_id FROM refresh_asset_ids) "
        "   OR target_asset_id IN (SELECT asset_id FROM refresh_asset_ids)"
    )
    for table in (
        "public_non_dicom_locations",
        "public_non_dicom_asset_participants",
        "public_non_dicom_crosswalk_evidence",
        "public_non_dicom_image_metadata",
    ):
        conn.execute(
            f"DELETE FROM {table} WHERE asset_id IN (SELECT asset_id FROM refresh_asset_ids)"
        )
    conn.execute(
        "DELETE FROM public_non_dicom_assets "
        "WHERE asset_id IN (SELECT asset_id FROM refresh_asset_ids)"
    )
    conn.execute("DROP TABLE refresh_asset_ids")
    return count


def apply_brats_crosswalk_to_existing_assets(
    conn: sqlite3.Connection,
    crosswalk: dict[str, dict[str, str]],
    provenance: dict[str, Any],
) -> int:
    """Replay the reviewed BraTS identity projection onto migrated V2 rows."""
    rows = conn.execute(
        """
        SELECT a.asset_id, a.subject_id, a.raw_values_json, a.provenance_json,
               COALESCE(
                 (SELECT ap.raw_subject_id
                  FROM public_non_dicom_asset_participants ap
                  WHERE ap.asset_id=a.asset_id AND ap.raw_subject_id LIKE 'BraTS2021_%'
                  LIMIT 1),
                 a.subject_id
               ) AS challenge_id
        FROM public_non_dicom_assets a
        WHERE lower(a.short_title)=lower(?)
          AND COALESCE(
                (SELECT ap.raw_subject_id
                 FROM public_non_dicom_asset_participants ap
                 WHERE ap.asset_id=a.asset_id AND ap.raw_subject_id LIKE 'BraTS2021_%'
                 LIMIT 1),
                a.subject_id
              ) LIKE 'BraTS2021_%'
        ORDER BY a.asset_id
        """,
        (BRATS_SHORT_TITLE,),
    ).fetchall()
    for row in rows:
        challenge_id = str(row["challenge_id"])
        subject_id, namespace, link_status, evidence = brats_participant_projection(
            challenge_id, crosswalk, provenance
        )
        try:
            raw_values = json.loads(row["raw_values_json"] or "{}")
        except json.JSONDecodeError:
            raw_values = {}
        try:
            provenance_values = json.loads(row["provenance_json"] or "{}")
        except json.JSONDecodeError:
            provenance_values = {}
        raw_values["brats_crosswalk"] = evidence
        provenance_values["brats_crosswalk"] = evidence
        conn.execute(
            """
            UPDATE public_non_dicom_assets
            SET subject_id=?, subject_id_namespace=?, participant_link_status=?,
                raw_values_json=?, provenance_json=?
            WHERE asset_id=?
            """,
            (
                subject_id,
                namespace,
                link_status,
                json_dumps(raw_values),
                json_dumps(provenance_values),
                row["asset_id"],
            ),
        )
        conn.execute(
            "DELETE FROM public_non_dicom_asset_participants WHERE asset_id=?",
            (row["asset_id"],),
        )
        insert_asset_participant(
            conn,
            asset_id=str(row["asset_id"]),
            short_title=BRATS_SHORT_TITLE,
            subject_id=subject_id,
            namespace=namespace,
            raw_subject_id=challenge_id,
            participant_role="depicted_subject",
            link_status=link_status,
            evidence=evidence,
        )
        insert_brats_crosswalk_evidence(
            conn, str(row["asset_id"]), challenge_id, subject_id, evidence
        )
    return len(rows)


def apply_bcbm_projection_to_existing_assets(conn: sqlite3.Connection) -> int:
    """Replay the reviewed BCBM scan-to-patient projection onto migrated rows."""
    rows = conn.execute(
        """SELECT asset_id, package_path, raw_values_json, provenance_json
           FROM public_non_dicom_assets
           WHERE lower(short_title)=lower(?) AND asset_granularity='file'
           ORDER BY asset_id""",
        (BCBM_SHORT_TITLE,),
    ).fetchall()
    projected = 0
    for row in rows:
        patient_id, scan_id = bcbm_patient_and_scan_id(str(row["package_path"] or ""))
        if not patient_id:
            continue
        evidence = {
            "source_scan_id": scan_id,
            "resolved_patient_id": patient_id,
            "mapping_method": "strip_final_numeric_scan_suffix",
        }
        try:
            raw_values = json.loads(row["raw_values_json"] or "{}")
        except json.JSONDecodeError:
            raw_values = {}
        try:
            provenance_values = json.loads(row["provenance_json"] or "{}")
        except json.JSONDecodeError:
            provenance_values = {}
        raw_values["bcbm_scan_projection"] = evidence
        provenance_values["bcbm_scan_projection"] = evidence
        namespace = f"tcia_collection:{BCBM_SHORT_TITLE}"
        conn.execute(
            """UPDATE public_non_dicom_assets
               SET subject_id=?, subject_id_namespace=?,
                   participant_link_status='reviewed_patient_scan_projection',
                   raw_values_json=?, provenance_json=?
               WHERE asset_id=?""",
            (
                patient_id,
                namespace,
                json_dumps(raw_values),
                json_dumps(provenance_values),
                row["asset_id"],
            ),
        )
        conn.execute(
            "DELETE FROM public_non_dicom_asset_participants WHERE asset_id=?",
            (row["asset_id"],),
        )
        insert_asset_participant(
            conn,
            asset_id=str(row["asset_id"]),
            short_title=BCBM_SHORT_TITLE,
            subject_id=patient_id,
            namespace=namespace,
            raw_subject_id=scan_id,
            participant_role="depicted_subject",
            link_status="reviewed_patient_scan_projection",
            evidence=evidence,
        )
        merge_image_metadata(
            conn,
            str(row["asset_id"]),
            {"procedure_id": scan_id},
            value_role="normalized",
            source_kind="reviewed_package_path_contract",
            source_locator="BCBM-RadioGenomics package path",
            inference_method="strip_final_numeric_scan_suffix",
            confidence="high",
            priority=110,
            evidence=evidence,
            short_title=BCBM_SHORT_TITLE,
        )
        projected += 1
    return projected


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
    remind_nrrd_inventory: Path | None = None,
    remind_nrrd_provenance: Path = DEFAULT_REMIND_NRRD_PROVENANCE,
    tcga_lgg_mask_inventory: Path = DEFAULT_TCGA_LGG_MASK_INVENTORY,
    tcga_lgg_mask_vasari: Path = DEFAULT_TCGA_LGG_MASK_VASARI,
    tcga_lgg_mask_provenance: Path = DEFAULT_TCGA_LGG_MASK_PROVENANCE,
    reviewed_participant_inventory: Path = DEFAULT_REVIEWED_PARTICIPANT_INVENTORY,
    reviewed_participant_provenance: Path = DEFAULT_REVIEWED_PARTICIPANT_PROVENANCE,
    staging_db: Path | None = None,
    baseline_db: Path | None = None,
) -> dict[str, Any]:
    if not snapshot_db.exists():
        raise FileNotFoundError(f"Base snapshot not found: {snapshot_db}")
    if baseline_db and not baseline_db.is_file():
        raise FileNotFoundError(f"V2 baseline not found: {baseline_db}")
    baseline_source_meta = source_meta(baseline_db) if baseline_db else {"enabled": False}
    in_place_baseline = bool(
        baseline_db and baseline_db.resolve() == out.resolve()
    )
    if out.exists():
        if not replace:
            raise FileExistsError(f"Output exists: {out}; pass --replace")
        if not in_place_baseline:
            out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    if baseline_db and not in_place_baseline:
        shutil.copy2(baseline_db, out)
    brats_crosswalk, brats_provenance = load_brats2021_crosswalk()
    with closing(connect(out)) as conn:
        if baseline_db:
            schema_row = conn.execute(
                "SELECT value FROM artifact_meta WHERE key='schema_version'"
            ).fetchone()
            if not schema_row or int(schema_row[0]) != SCHEMA_VERSION:
                raise RuntimeError("V2 baseline schema does not match the current builder")
            conn.execute("DELETE FROM artifact_meta")
            refreshed_assets = delete_refreshable_assets(conn)
            conn.execute("DELETE FROM public_non_dicom_crosswalk_decisions")
            conn.execute("DELETE FROM public_non_dicom_crosswalk_evidence")
            conn.execute("DELETE FROM public_non_dicom_review_issues")
            conn.execute("DELETE FROM public_non_dicom_dataset_metadata_notes")
            conn.execute("DELETE FROM public_non_dicom_metadata_field_coverage")
        else:
            conn.executescript(SCHEMA)
            insert_vocab(conn)
            refreshed_assets = 0
        counts = {
            "baseline_refreshable_assets_removed": refreshed_assets,
            "wordpress_download_assets": ingest_wordpress(conn, snapshot_db),
            "nifti_file_assets": ingest_nifti(
                conn, nifti_db, brats_crosswalk, brats_provenance
            ) if nifti_db else 0,
            "aspera_public_dicom_exception_assets": (
                ingest_aspera_public_dicom_exceptions(
                    conn, nifti_db, brats_crosswalk, brats_provenance
                ) if nifti_db else 0
            ),
            "remind_nrrd_file_assets": ingest_remind_nrrd_inventory(
                conn,
                nifti_db,
                inventory_path=remind_nrrd_inventory,
                provenance_path=remind_nrrd_provenance,
            ),
            "cptac_gbm_codex_reviewed_file_assets": ingest_cptac_gbm_codex_inventory(conn),
            "pathology_package_assets": ingest_pathology_packages(conn, pathology_db) if pathology_db else 0,
            "pathdb_file_assets": ingest_pathdb(conn, snapshot_db, include_pathdb_files),
        }
        codex_crosswalk = apply_cptac_gbm_codex_workbook_crosswalk(
            conn, clinical_db, require_pathdb=include_pathdb_files
        )
        counts.update(
            {f"cptac_gbm_codex_{key}": value for key, value in codex_crosswalk.items()}
        )
        if baseline_db:
            counts["brats_existing_assets_reprojected"] = apply_brats_crosswalk_to_existing_assets(
                conn, brats_crosswalk, brats_provenance
            )
            counts["bcbm_existing_assets_reprojected"] = apply_bcbm_projection_to_existing_assets(conn)
        counts["wordpress_aggregate_counts_refreshed"] = refresh_wordpress_aggregate_counts(
            conn, snapshot_db
        )
        counts["remind_nrrd_aggregate_counts_reviewed"] = (
            apply_reviewed_remind_nrrd_aggregate(conn)
        )
        reviewed = (
            ingest_reviewed_crosswalks(conn, crosswalk_csv, crosswalk_curation)
            if crosswalk_csv and crosswalk_curation
            else {"decisions": 0, "file_assets": 0, "evidence_rows": 0}
        )
        counts.update({f"reviewed_crosswalk_{key}": value for key, value in reviewed.items()})
        tcga_gbm_qi_aim = ingest_tcga_gbm_qi_aim_inventory(conn, snapshot_db)
        counts.update(
            {f"tcga_gbm_qi_aim_{key}": value for key, value in tcga_gbm_qi_aim.items()}
        )
        tcga_lgg_mask = ingest_tcga_lgg_mask_inventory(
            conn,
            snapshot_db,
            mask_inventory_path=tcga_lgg_mask_inventory,
            vasari_inventory_path=tcga_lgg_mask_vasari,
            provenance_path=tcga_lgg_mask_provenance,
        )
        counts.update(
            {f"tcga_lgg_mask_{key}": value for key, value in tcga_lgg_mask.items()}
        )
        reviewed_participants = ingest_reviewed_analysis_result_participants(
            conn,
            snapshot_db,
            inventory_path=reviewed_participant_inventory,
            provenance_path=reviewed_participant_provenance,
        )
        counts.update({
            f"reviewed_analysis_result_participants_{key}": value
            for key, value in reviewed_participants.items()
        })
        path_contracts = apply_reviewed_path_contracts(
            conn, clinical_db, crosswalk_curation, pathology_db=pathology_db
        )
        counts.update({f"reviewed_path_contract_{key}": value for key, value in path_contracts.items()})
        pathdb_contracts = apply_reviewed_pathdb_contracts(conn, crosswalk_curation)
        counts.update({
            f"reviewed_pathdb_contract_{key}": value
            for key, value in pathdb_contracts.items()
        })
        automated = apply_automated_pathdb_crosswalks(conn, snapshot_db)
        counts.update({f"automated_crosswalk_{key}": value for key, value in automated.items()})
        curated_metadata = ingest_curated_image_metadata(conn, image_metadata_csv)
        counts.update({f"curated_image_metadata_{key}": value for key, value in curated_metadata.items()})
        yale_metadata = ingest_yale_brain_mets_workbook_metadata(conn, clinical_db)
        counts.update({f"yale_workbook_{key}": value for key, value in yale_metadata.items()})
        bcbm_metadata = ingest_bcbm_workbook_metadata(conn, clinical_db)
        counts.update({f"bcbm_workbook_{key}": value for key, value in bcbm_metadata.items()})
        counts["core_image_metadata_values"] = seed_core_asset_metadata(conn)
        inferred_metadata = enrich_from_wordpress_and_filenames(conn, snapshot_db)
        counts.update({f"inferred_image_metadata_{key}": value for key, value in inferred_metadata.items()})
        counts["metadata_field_coverage_rows"] = build_metadata_field_coverage(conn)
        counts["metadata_assessment_note_changes"] = add_metadata_assessment_notes(conn, nifti_db)
        counts["asset_participant_links_projected"] = sync_scalar_asset_participants(conn)
        counts["download_assets_with_file_grain_crosswalk"] = (
            mark_downloads_with_linked_file_grain(conn)
        )
        counts["participant_crosswalk_review_issue_changes"] = (
            refresh_participant_crosswalk_review_issues(conn)
        )
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
            "source_remind_nrrd_inventory": source_meta(remind_nrrd_inventory) if remind_nrrd_inventory else {"enabled": False},
            "source_remind_nrrd_provenance": source_meta(remind_nrrd_provenance),
            "source_tcga_lgg_mask_inventory": source_meta(tcga_lgg_mask_inventory),
            "source_tcga_lgg_mask_vasari": source_meta(tcga_lgg_mask_vasari),
            "source_tcga_lgg_mask_provenance": source_meta(tcga_lgg_mask_provenance),
            "source_reviewed_participant_inventory": source_meta(reviewed_participant_inventory),
            "source_reviewed_participant_provenance": source_meta(reviewed_participant_provenance),
            "source_tcga_gbm_qi_aim_inventory": source_meta(DEFAULT_TCGA_GBM_QI_AIM_INVENTORY),
            "source_tcga_gbm_qi_aim_provenance": source_meta(DEFAULT_TCGA_GBM_QI_AIM_PROVENANCE),
            "source_cptac_gbm_codex_inventory": source_meta(DEFAULT_CPTAC_GBM_CODEX_INVENTORY),
            "source_cptac_gbm_codex_provenance": source_meta(DEFAULT_CPTAC_GBM_CODEX_PROVENANCE),
            "source_staging_ledger": source_meta(staging_db) if staging_db else {"enabled": False},
            "source_public_non_dicom_baseline": baseline_source_meta,
            "source_brats_crosswalk_csv": source_meta(DEFAULT_BRATS_CROSSWALK_CSV),
            "source_brats_crosswalk_provenance": source_meta(DEFAULT_BRATS_CROSSWALK_PROVENANCE),
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
        artifact_meta = (
            dict(conn.execute("SELECT key, value FROM artifact_meta"))
            if "artifact_meta" in objects
            else {}
        )
        provenance_in_companion = (
            artifact_meta.get("provenance_storage") == "companion_audit_artifact"
        )
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
        if counts["assets"] > 100000:
            brats_counts = {
                "brats_participants": conn.execute(
                    "SELECT COUNT(DISTINCT subject_id) FROM public_non_dicom_asset_participants WHERE short_title = ?",
                    (BRATS_SHORT_TITLE,),
                ).fetchone()[0],
                "brats_challenge_aliases": conn.execute(
                    "SELECT COUNT(DISTINCT raw_subject_id) FROM public_non_dicom_asset_participants WHERE short_title = ?",
                    (BRATS_SHORT_TITLE,),
                ).fetchone()[0],
                "brats_source_collection_identifiers": conn.execute(
                    "SELECT COUNT(DISTINCT subject_id) FROM public_non_dicom_asset_participants WHERE short_title = ? AND subject_id NOT LIKE 'BraTS2021_%'",
                    (BRATS_SHORT_TITLE,),
                ).fetchone()[0],
                "brats_challenge_only_identifiers": conn.execute(
                    "SELECT COUNT(DISTINCT subject_id) FROM public_non_dicom_asset_participants WHERE short_title = ? AND subject_id LIKE 'BraTS2021_%'",
                    (BRATS_SHORT_TITLE,),
                ).fetchone()[0],
                "brats_reviewed_source_crosswalks": conn.execute(
                    "SELECT COUNT(DISTINCT raw_subject_id) FROM public_non_dicom_crosswalk_evidence WHERE short_title = ? AND mapping_method = 'official_tcia_brats2021_workbook'",
                    (BRATS_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(brats_counts)
            expected_brats = {
                "brats_participants": 1479,
                "brats_challenge_aliases": 1479,
                "brats_source_collection_identifiers": 1066,
                "brats_challenge_only_identifiers": 413,
                "brats_reviewed_source_crosswalks": 1066,
            }
            for name, expected in expected_brats.items():
                if name == "brats_reviewed_source_crosswalks" and provenance_in_companion:
                    continue
                if brats_counts[name] != expected:
                    errors.append(
                        f"BraTS crosswalk coverage regression: {name}={brats_counts[name]} != {expected}"
                    )
            for short_title, expected in {
                "DICOM-Glioma-SEG": 167,
                "ISBI-MR-Prostate-2013": 80,
                "LIDC-annot-NLST501": 501,
                "MRQy-Quality-Measures": 233,
                "TCGA-KIRC-Radiogenomics": 103,
                "TCGA-OV-Proteogenomics": 20,
                "TCGA-OV-Radiogenomics": 93,
            }.items():
                actual = conn.execute(
                    "SELECT COUNT(DISTINCT subject_id) "
                    "FROM public_non_dicom_asset_participants WHERE short_title=?",
                    (short_title,),
                ).fetchone()[0]
                key = re.sub(r"[^a-z0-9]+", "_", short_title.casefold()).strip("_")
                counts[f"{key}_participants"] = actual
                if actual != expected:
                    errors.append(
                        f"{short_title} participant coverage regression: "
                        f"{actual} != {expected}"
                    )
            bcbm_counts = {
                "bcbm_participants": conn.execute(
                    "SELECT COUNT(DISTINCT subject_id) FROM public_non_dicom_asset_participants WHERE short_title = ?",
                    (BCBM_SHORT_TITLE,),
                ).fetchone()[0],
                "bcbm_scan_identifiers": conn.execute(
                    "SELECT COUNT(DISTINCT raw_subject_id) FROM public_non_dicom_asset_participants WHERE short_title = ?",
                    (BCBM_SHORT_TITLE,),
                ).fetchone()[0],
                "bcbm_files": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_assets WHERE short_title = ? AND asset_granularity = 'file'",
                    (BCBM_SHORT_TITLE,),
                ).fetchone()[0],
                "bcbm_source_images": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_assets WHERE short_title = ? AND asset_granularity = 'file' AND object_role = 'source_image'",
                    (BCBM_SHORT_TITLE,),
                ).fetchone()[0],
                "bcbm_segmentations": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_assets WHERE short_title = ? AND asset_granularity = 'file' AND object_role = 'segmentation'",
                    (BCBM_SHORT_TITLE,),
                ).fetchone()[0],
                "bcbm_radiomics_assets": conn.execute(
                    """SELECT COUNT(*) FROM public_non_dicom_image_metadata
                       WHERE short_title = ?
                         AND json_extract(metadata_json, '$.radiomics_feature_count') = 107""",
                    (BCBM_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(bcbm_counts)
            expected_bcbm = {
                "bcbm_participants": 165,
                "bcbm_scan_identifiers": 268,
                "bcbm_files": 3089,
                "bcbm_source_images": 268,
                "bcbm_segmentations": 2821,
                "bcbm_radiomics_assets": 2821,
            }
            for name, expected in expected_bcbm.items():
                if bcbm_counts[name] != expected:
                    errors.append(
                        f"BCBM coverage regression: {name}={bcbm_counts[name]} != {expected}"
                    )
            tcga_gbm_qi_counts = {
                "tcga_gbm_qi_aim_files": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_assets WHERE short_title=? "
                    "AND asset_granularity='file' AND file_format='XML' "
                    "AND object_role='aim_segmentation_annotation'",
                    (TCGA_GBM_QI_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_gbm_qi_participants": conn.execute(
                    "SELECT COUNT(DISTINCT subject_id) FROM public_non_dicom_asset_participants "
                    "WHERE short_title=?",
                    (TCGA_GBM_QI_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_gbm_qi_series": conn.execute(
                    "SELECT COUNT(DISTINCT json_extract(raw_values_json, '$.series_instance_uid')) "
                    "FROM public_non_dicom_assets WHERE short_title=? "
                    "AND asset_granularity='file'",
                    (TCGA_GBM_QI_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_gbm_qi_sop_instances": conn.execute(
                    "SELECT COUNT(DISTINCT json_extract(raw_values_json, '$.sop_instance_uid')) "
                    "FROM public_non_dicom_assets WHERE short_title=? "
                    "AND asset_granularity='file'",
                    (TCGA_GBM_QI_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(tcga_gbm_qi_counts)
            for name, expected in {
                "tcga_gbm_qi_aim_files": 321,
                "tcga_gbm_qi_participants": 55,
                "tcga_gbm_qi_series": 111,
                "tcga_gbm_qi_sop_instances": 193,
            }.items():
                if provenance_in_companion and name in {
                    "tcga_gbm_qi_series",
                    "tcga_gbm_qi_sop_instances",
                }:
                    # These identifiers are intentionally retained in the audit
                    # companion's raw_values_json payloads, while the compact
                    # research artifact stores the documented empty placeholder.
                    continue
                if tcga_gbm_qi_counts[name] != expected:
                    errors.append(
                        "TCGA-GBM-QI-Radiogenomics coverage regression: "
                        f"{name}={tcga_gbm_qi_counts[name]} != {expected}"
                    )
            include_pathdb = json.loads(
                artifact_meta.get("include_pathdb_files", "true")
            )
            cptac_codex_counts = {
                "cptac_gbm_codex_aspera_files": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_assets WHERE short_title=? "
                    "AND asset_granularity='file' AND source_system='tcia_aspera'",
                    (CPTAC_GBM_CODEX_SHORT_TITLE,),
                ).fetchone()[0],
                "cptac_gbm_codex_pathdb_files": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_assets WHERE short_title=? "
                    "AND asset_granularity='file' AND source_system='tcia_pathdb'",
                    (CPTAC_GBM_CODEX_SHORT_TITLE,),
                ).fetchone()[0],
                "cptac_gbm_codex_participants": conn.execute(
                    "SELECT COUNT(DISTINCT subject_id) FROM public_non_dicom_asset_participants "
                    "WHERE short_title=?",
                    (CPTAC_GBM_CODEX_SHORT_TITLE,),
                ).fetchone()[0],
                "cptac_gbm_codex_representation_links": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_asset_relationships r "
                    "JOIN public_non_dicom_assets a ON a.asset_id=r.source_asset_id "
                    "WHERE a.short_title=? "
                    "AND r.relationship_type='managed_representation_correspondence'",
                    (CPTAC_GBM_CODEX_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(cptac_codex_counts)
            expected_cptac = {
                "cptac_gbm_codex_aspera_files": 52,
                "cptac_gbm_codex_pathdb_files": 52 if include_pathdb else 0,
                "cptac_gbm_codex_participants": 12,
                "cptac_gbm_codex_representation_links": 52 if include_pathdb else 0,
            }
            for name, expected in expected_cptac.items():
                if cptac_codex_counts[name] != expected:
                    errors.append(
                        "CPTAC-Glioblastoma-CODEX coverage regression: "
                        f"{name}={cptac_codex_counts[name]} != {expected}"
                    )
            remind_counts = {
                "remind_nrrd_download_assets": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_assets "
                    "WHERE short_title = ? AND file_format = 'NRRD' AND asset_granularity = 'download'",
                    (REMIND_SHORT_TITLE,),
                ).fetchone()[0],
                "remind_nrrd_represented_files": conn.execute(
                    "SELECT COALESCE(SUM(represented_file_count), 0) FROM public_non_dicom_assets "
                    "WHERE short_title = ? AND file_format = 'NRRD' AND asset_granularity = 'download'",
                    (REMIND_SHORT_TITLE,),
                ).fetchone()[0],
                "remind_nrrd_file_assets": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_assets "
                    "WHERE short_title = ? AND file_format = 'NRRD' AND asset_granularity = 'file'",
                    (REMIND_SHORT_TITLE,),
                ).fetchone()[0],
                "remind_nrrd_participants": conn.execute(
                    "SELECT COUNT(DISTINCT ap.subject_id) FROM public_non_dicom_asset_participants ap "
                    "JOIN public_non_dicom_assets a USING(asset_id) "
                    "WHERE a.short_title = ? AND a.file_format = 'NRRD' AND a.asset_granularity = 'file'",
                    (REMIND_SHORT_TITLE,),
                ).fetchone()[0],
                "remind_nrrd_whole_tumor_participants": conn.execute(
                    "SELECT COUNT(DISTINCT a.subject_id) FROM public_non_dicom_assets a "
                    "JOIN public_non_dicom_image_metadata m USING(asset_id) "
                    "WHERE a.short_title = ? AND a.file_format = 'NRRD' "
                    "AND a.asset_granularity = 'file' "
                    "AND json_extract(m.metadata_json, '$.segmentation_label') = 'tumor'",
                    (REMIND_SHORT_TITLE,),
                ).fetchone()[0],
                "remind_nrrd_participant_links": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_asset_participants ap "
                    "JOIN public_non_dicom_assets a USING(asset_id) "
                    "WHERE a.short_title = ? AND a.file_format = 'NRRD'",
                    (REMIND_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(remind_counts)
            for name, expected in {
                "remind_nrrd_download_assets": 1,
                "remind_nrrd_represented_files": 356,
                "remind_nrrd_file_assets": 356,
                "remind_nrrd_participants": 114,
                "remind_nrrd_whole_tumor_participants": 113,
                "remind_nrrd_participant_links": 356,
            }.items():
                if remind_counts[name] != expected:
                    errors.append(
                        f"ReMIND NRRD coverage regression: {name}={remind_counts[name]} != {expected}"
                    )
            tcga_lgg_mask_counts = {
                "tcga_lgg_mask_files": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_assets "
                    "WHERE short_title = ? AND asset_granularity = 'file' "
                    "AND file_format = 'MATLAB' AND object_role = 'segmentation'",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_lgg_mask_participants": conn.execute(
                    "SELECT COUNT(DISTINCT ap.subject_id) "
                    "FROM public_non_dicom_asset_participants ap "
                    "JOIN public_non_dicom_assets a USING(asset_id) "
                    "WHERE a.short_title = ? AND a.file_format = 'MATLAB' "
                    "AND a.asset_granularity = 'file'",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_lgg_mask_source_series": conn.execute(
                    "SELECT COUNT(DISTINCT json_extract(m.metadata_json, '$.source_series_instance_uid')) "
                    "FROM public_non_dicom_image_metadata m "
                    "JOIN public_non_dicom_assets a USING(asset_id) "
                    "WHERE a.short_title = ? AND a.file_format = 'MATLAB'",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_lgg_mask_vasari_assets": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_assets "
                    "WHERE short_title = ? AND object_role = 'qualitative_image_annotation_table'",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_lgg_mask_vasari_participants": conn.execute(
                    "SELECT COUNT(DISTINCT ap.subject_id) "
                    "FROM public_non_dicom_asset_participants ap "
                    "JOIN public_non_dicom_assets a USING(asset_id) "
                    "WHERE a.short_title = ? "
                    "AND a.object_role = 'qualitative_image_annotation_table'",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_lgg_mask_vasari_complete_rows": conn.execute(
                    "SELECT COALESCE(MAX(json_extract(m.metadata_json, '$.complete_numeric_rows')), 0) "
                    "FROM public_non_dicom_image_metadata m "
                    "JOIN public_non_dicom_assets a USING(asset_id) "
                    "WHERE a.short_title = ? "
                    "AND a.object_role = 'qualitative_image_annotation_table'",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_lgg_mask_feature_keys": conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_assets "
                    "WHERE short_title = ? AND object_role = 'annotation_feature_dictionary'",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
                "tcga_lgg_mask_union_participants": conn.execute(
                    "SELECT COUNT(DISTINCT subject_id) FROM public_non_dicom_asset_participants "
                    "WHERE short_title = ?",
                    (TCGA_LGG_MASK_SHORT_TITLE,),
                ).fetchone()[0],
            }
            counts.update(tcga_lgg_mask_counts)
            for name, expected in {
                "tcga_lgg_mask_files": 406,
                "tcga_lgg_mask_participants": 108,
                "tcga_lgg_mask_source_series": 406,
                "tcga_lgg_mask_vasari_assets": 1,
                "tcga_lgg_mask_vasari_participants": 188,
                "tcga_lgg_mask_vasari_complete_rows": 178,
                "tcga_lgg_mask_feature_keys": 1,
                "tcga_lgg_mask_union_participants": 188,
            }.items():
                if tcga_lgg_mask_counts[name] != expected:
                    errors.append(
                        "TCGA-LGG-Mask coverage regression: "
                        f"{name}={tcga_lgg_mask_counts[name]} != {expected}"
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
    build.add_argument(
        "--nifti-db",
        default="",
        help="Migration-only legacy source; routine V2 builds use --baseline-db.",
    )
    build.add_argument(
        "--pathology-db",
        default="",
        help="Migration-only legacy source; routine V2 builds use --baseline-db.",
    )
    build.add_argument("--clinical-db", default=str(DEFAULT_CLINICAL_DB))
    build.add_argument(
        "--staging-db",
        help=(
            "Resolve the current snapshot/clinical inputs and the manifest-pinned "
            "unified V2 baseline from a runner-local staging ledger."
        ),
    )
    build.add_argument(
        "--baseline-db",
        help="Losslessly materialized V2 public non-DICOM assembly used after legacy retirement.",
    )
    build.add_argument("--crosswalk-csv", default=str(DEFAULT_CROSSWALK_CSV))
    build.add_argument("--crosswalk-curation", default=str(DEFAULT_CROSSWALK_CURATION))
    build.add_argument("--image-metadata-csv", default=str(DEFAULT_IMAGE_METADATA_CSV))
    build.add_argument("--remind-nrrd-inventory", default=str(DEFAULT_REMIND_NRRD_INVENTORY))
    build.add_argument("--remind-nrrd-provenance", default=str(DEFAULT_REMIND_NRRD_PROVENANCE))
    build.add_argument("--tcga-lgg-mask-inventory", default=str(DEFAULT_TCGA_LGG_MASK_INVENTORY))
    build.add_argument("--tcga-lgg-mask-vasari", default=str(DEFAULT_TCGA_LGG_MASK_VASARI))
    build.add_argument("--tcga-lgg-mask-provenance", default=str(DEFAULT_TCGA_LGG_MASK_PROVENANCE))
    build.add_argument(
        "--reviewed-participant-inventory",
        default=str(DEFAULT_REVIEWED_PARTICIPANT_INVENTORY),
    )
    build.add_argument(
        "--reviewed-participant-provenance",
        default=str(DEFAULT_REVIEWED_PARTICIPANT_PROVENANCE),
    )
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
        staging_db = Path(args.staging_db) if args.staging_db else None
        if staging_db:
            snapshot_db = resolve_staging_component(staging_db, "snapshot", verify_hash=False)
            clinical_db = resolve_staging_component(staging_db, "clinical", verify_hash=False)
            baseline_db = Path(args.baseline_db) if args.baseline_db else resolve_staging_component(
                staging_db, "public_non_dicom_baseline", verify_hash=False
            )
            nifti_db = None
            pathology_db = None
        else:
            snapshot_db = Path(args.snapshot_db)
            nifti_db = Path(args.nifti_db) if args.nifti_db else None
            pathology_db = Path(args.pathology_db) if args.pathology_db else None
            clinical_db = Path(args.clinical_db) if args.clinical_db else None
            baseline_db = Path(args.baseline_db) if args.baseline_db else None
        result = build_database(
            snapshot_db, out,
            nifti_db=nifti_db,
            pathology_db=pathology_db,
            clinical_db=clinical_db,
            crosswalk_csv=Path(args.crosswalk_csv) if args.crosswalk_csv else None,
            crosswalk_curation=Path(args.crosswalk_curation) if args.crosswalk_curation else None,
            image_metadata_csv=Path(args.image_metadata_csv) if args.image_metadata_csv else None,
            remind_nrrd_inventory=Path(args.remind_nrrd_inventory) if args.remind_nrrd_inventory else None,
            remind_nrrd_provenance=Path(args.remind_nrrd_provenance),
            tcga_lgg_mask_inventory=Path(args.tcga_lgg_mask_inventory),
            tcga_lgg_mask_vasari=Path(args.tcga_lgg_mask_vasari),
            tcga_lgg_mask_provenance=Path(args.tcga_lgg_mask_provenance),
            reviewed_participant_inventory=Path(args.reviewed_participant_inventory),
            reviewed_participant_provenance=Path(args.reviewed_participant_provenance),
            include_pathdb_files=not args.no_pathdb_files,
            replace=args.replace,
            staging_db=staging_db,
            baseline_db=baseline_db,
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
