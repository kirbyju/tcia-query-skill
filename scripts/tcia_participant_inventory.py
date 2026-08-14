#!/usr/bin/env python3
"""Build a compact participant-centered inventory over TCIA metadata sidecars."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tcia_artifact_model import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
from tcia_artifact_model import json_dumps, stable_id


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC_DB = SKILL_ROOT / "cache" / "public_non_dicom_metadata.sqlite"
DEFAULT_CONTROLLED_DB = SKILL_ROOT / "cache" / "controlled_access_metadata.sqlite"
DEFAULT_CLINICAL_DB = SKILL_ROOT / "cache" / "clinical_metadata.sqlite"
DEFAULT_DB = SKILL_ROOT / "cache" / "participant_inventory.sqlite"
DEFAULT_MANIFEST = SKILL_ROOT / "cache" / "participant_inventory_manifest.json"
DEFAULT_RELEASE_TAG = "tcia-metadata-v2-preview"
DEFAULT_REPOSITORY = "kirbyju/tcia-query-skill"
DB_ASSET = "participant_inventory.sqlite.gz"
MANIFEST_ASSET = "participant_inventory_manifest.json"
SCHEMA_VERSION = 3


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
    media_kind TEXT,
    modality TEXT,
    file_format TEXT,
    object_role TEXT,
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

CREATE INDEX idx_pi_participants_dataset ON participants(short_title, display_participant_id);
CREATE INDEX idx_pi_identifiers_raw ON participant_identifiers(identifier_namespace, raw_identifier);
CREATE INDEX idx_pi_assets_participant ON participant_assets(participant_key);
CREATE INDEX idx_pi_assets_access ON participant_assets(access_level, data_domain);
CREATE INDEX idx_pi_clinical_participant ON participant_clinical_values(participant_key, concept);

CREATE VIEW agent_participants AS
SELECT
    p.*,
    COUNT(DISTINCT a.participant_asset_id) AS inventory_rows,
    MAX(CASE WHEN a.access_level = 'open' THEN 1 ELSE 0 END) AS has_open_data,
    MAX(CASE WHEN a.access_level = 'controlled' THEN 1 ELSE 0 END) AS has_controlled_data,
    MAX(CASE WHEN a.managed_system = 'crdc_idc' THEN 1 ELSE 0 END) AS has_public_dicom,
    MAX(CASE WHEN a.source_artifact = 'public_non_dicom_metadata' THEN 1 ELSE 0 END) AS has_public_non_dicom,
    MAX(CASE WHEN a.source_artifact = 'clinical_metadata' THEN 1 ELSE 0 END) AS has_clinical,
    group_concat(DISTINCT a.data_domain) AS data_domains,
    group_concat(DISTINCT a.modality) AS modalities,
    group_concat(DISTINCT a.file_format) AS file_formats,
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
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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


def participant_key(
    dataset_type: str, short_title: str, identifier_namespace: str, raw_identifier: str
) -> str:
    return stable_id(
        "participant", dataset_type, short_title, identifier_namespace, raw_identifier
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
    key = participant_key(dataset_type, short_title, namespace, raw_identifier)
    conn.execute(
        "INSERT OR IGNORE INTO participants VALUES (?, ?, ?, ?, 'dataset_scoped', 'not_asserted')",
        (key, dataset_type, short_title, raw_identifier),
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
            raw_identifier.strip(),
            evidence,
            json_dumps(provenance),
        ),
    )
    return key


def add_asset(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
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


def ingest_public_non_dicom(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        record_source(conn, "public_non_dicom_metadata", path, False, 0, "Optional source not installed.")
        return 0
    imported = 0
    with connect(path) as source:
        if not table_exists(source, "agent_public_non_dicom_participant_summary"):
            record_source(conn, "public_non_dicom_metadata", path, True, 0, "Required participant view missing.")
            return 0
        for row in source.execute("SELECT * FROM agent_public_non_dicom_participant_summary"):
            raw_id = str(row["subject_id"] or "").strip()
            if not raw_id:
                continue
            key = ensure_participant(
                conn,
                str(row["dataset_type"] or "Collection"),
                str(row["short_title"]),
                raw_id,
                managed_system="tcia_wordpress",
                namespace=f"tcia_dataset:{row['short_title']}",
                evidence=str(row["participant_link_status"] or "dataset_scoped_source_identifier"),
                provenance={"source_artifact": "public_non_dicom_metadata"},
            )
            asset_id = stable_id("pa", key, "public_non_dicom", row["file_formats"], row["object_roles"])
            add_asset(conn, {
                "participant_asset_id": asset_id,
                "participant_key": key,
                "managed_system": "tcia_wordpress",
                "source_artifact": "public_non_dicom_metadata",
                "access_level": "open",
                "data_domain": row["imaging_domains"] or "other_imaging",
                "media_kind": row["media_kinds"] or "",
                "modality": row["modalities"] or "",
                "file_format": row["file_formats"] or "",
                "object_role": row["object_roles"] or "",
                "study_count": None,
                "series_count": None,
                "file_count": row["file_assets"] or row["asset_rows"],
                "known_size_bytes": row["known_size_bytes"] or 0,
                "has_file_level_metadata": 1,
                "detail_pointer": f"agent_public_non_dicom_asset_participants:{row['short_title']}:{raw_id}",
                "access_route": "",
                "inventory_status": "known",
                "source_version": "schema-v3",
                "provenance_json": json_dumps({"source_view": "agent_public_non_dicom_participant_summary"}),
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
    return imported


def ingest_controlled(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        record_source(conn, "controlled_access_metadata", path, False, 0, "Optional detailed controlled metadata not installed.")
        return 0
    imported = 0
    with connect(path) as source:
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
            key = ensure_participant(
                conn,
                str(row["dataset_type"] or "Collection"),
                str(row["short_title"]),
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


def ingest_clinical(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        record_source(conn, "clinical_metadata", path, False, 0, "Optional clinical metadata not installed.")
        return 0
    imported = 0
    with connect(path) as source:
        if table_exists(source, "agent_clinical_all_subjects"):
            for row in source.execute("SELECT short_title, subject_id, source_kinds FROM agent_clinical_all_subjects"):
                raw_id = str(row["subject_id"] or "").strip()
                if not raw_id:
                    continue
                key = ensure_participant(
                    conn, "Collection", str(row["short_title"]), raw_id,
                    managed_system="tcia_wordpress",
                    namespace=f"tcia_dataset:{row['short_title']}",
                    evidence="clinical_sidecar_dataset_scoped_identifier",
                    provenance={"source_artifact": "clinical_metadata", "source_kinds": row["source_kinds"]},
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
                    "provenance_json": json_dumps({"source_view": "agent_clinical_all_subjects"}),
                })
                imported += 1
        if table_exists(source, "clinical_imaging_subjects"):
            for row in source.execute(
                "SELECT short_title, subject_id, imaging_source FROM clinical_imaging_subjects WHERE imaging_source LIKE '%idc_index%'"
            ):
                raw_id = str(row["subject_id"] or "").strip()
                if not raw_id:
                    continue
                key = ensure_participant(
                    conn, "Collection", str(row["short_title"]), raw_id,
                    managed_system="crdc_idc",
                    namespace=f"tcia_dataset:{row['short_title']}",
                    evidence="idc_index_participant_projection",
                    provenance={"source_artifact": "clinical_metadata", "imaging_source": row["imaging_source"]},
                )
                add_asset(conn, {
                    "participant_asset_id": stable_id("pa", key, "crdc_idc", "public_dicom"),
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
                    "detail_pointer": f"crdc_idc:{row['short_title']}:{raw_id}",
                    "access_route": "",
                    "inventory_status": "participant_presence_known_query_idc_for_detail",
                    "source_version": "",
                    "provenance_json": json_dumps({"source_table": "clinical_imaging_subjects", "imaging_source": row["imaging_source"]}),
                })
        if table_exists(source, "agent_clinical_facts"):
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
                key = participant_key(
                    "Collection", str(row["short_title"]),
                    f"tcia_dataset:{row['short_title']}", raw_id,
                )
                if conn.execute("SELECT 1 FROM participants WHERE participant_key = ?", (key,)).fetchone() is None:
                    key = ensure_participant(
                        conn, "Collection", str(row["short_title"]), raw_id,
                        managed_system="tcia_wordpress",
                        namespace=f"tcia_dataset:{row['short_title']}",
                        evidence="clinical_fact_dataset_scoped_identifier",
                        provenance={"source_artifact": "clinical_metadata"},
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
                value_id = stable_id(
                    "clinical_value", key, row["concept"], row["source_kind"],
                    row["source_url"], row["original_column"], raw_value, standardized,
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO participant_clinical_values
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'tcia_wordpress',
                            'clinical_metadata', ?, ?, ?, ?)
                    """,
                    (
                        value_id, key, row["concept"], row["original_column"], raw_value,
                        standardized, role, method, row["source_url"] or "",
                        "inferred" if row["is_inferred"] else "source_supported",
                        row["qc_status"] or "",
                        row["provenance_json"] or json_dumps({
                            "source_kind": row["source_kind"], "evidence_scope": row["evidence_scope"]
                        }),
                    ),
                )
    record_source(conn, "clinical_metadata", path, True, imported, "Clinical availability and the compact IDC participant projection imported; IDC remains authoritative for DICOM detail.")
    return imported


def add_same_text_cross_namespace_issues(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT p.dataset_type, p.short_title, i.raw_identifier,
               COUNT(DISTINCT i.identifier_namespace) AS namespace_count,
               group_concat(DISTINCT i.identifier_namespace) AS namespaces,
               group_concat(DISTINCT i.managed_system) AS managed_systems
        FROM participant_identifiers i
        JOIN participants p USING(participant_key)
        GROUP BY p.dataset_type, p.short_title, i.raw_identifier
        HAVING COUNT(DISTINCT i.identifier_namespace) > 1
        """
    ).fetchall()
    conn.executemany(
        """
        INSERT OR IGNORE INTO participant_link_issues
        VALUES (?, ?, ?, ?, 'same_text_cross_namespace', 'review_required', ?, ?)
        """,
        [
            (
                stable_id("link_issue", row["dataset_type"], row["short_title"], row["raw_identifier"]),
                row["dataset_type"], row["short_title"], row["raw_identifier"],
                "The same identifier text occurs in multiple source namespaces; no participant equivalence was asserted without an explicit crosswalk.",
                json_dumps({
                    "namespace_count": row["namespace_count"],
                    "namespaces": row["namespaces"],
                    "managed_systems": row["managed_systems"],
                }),
            )
            for row in rows
        ],
    )
    return len(rows)


def build_database(
    out: Path,
    *,
    public_db: Path,
    controlled_db: Path,
    clinical_db: Path,
    replace: bool,
) -> dict[str, Any]:
    if out.exists():
        if not replace:
            raise FileExistsError(f"Output exists: {out}; pass --replace")
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    with connect(out) as conn:
        conn.executescript(SCHEMA)
        counts = {
            "public_non_dicom": ingest_public_non_dicom(conn, public_db),
            "controlled": ingest_controlled(conn, controlled_db),
            "clinical": ingest_clinical(conn, clinical_db),
        }
        counts["same_text_cross_namespace_issues"] = add_same_text_cross_namespace_issues(conn)
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "artifact_model_schema_version": MODEL_SCHEMA_VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "identity_rule": "Participants are dataset-scoped; cross-dataset identity is never asserted from a repeated bare identifier.",
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


def validate_database(path: Path) -> dict[str, Any]:
    required = {
        "participants", "participant_identifiers", "participant_assets",
        "participant_clinical_values",
        "dataset_assets_without_participant_crosswalk", "participant_inventory_sources",
        "agent_participants", "agent_participant_assets", "agent_participant_identifiers",
        "agent_participant_clinical_values",
    }
    errors: list[str] = []
    with connect(path) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"integrity_check={integrity}")
        objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        missing = sorted(required - objects)
        if missing:
            errors.append("missing objects: " + ", ".join(missing))
        cross_dataset = conn.execute(
            "SELECT COUNT(*) FROM participants WHERE cross_dataset_identity_status <> 'not_asserted'"
        ).fetchone()[0]
        if cross_dataset:
            errors.append(f"unexpected asserted cross-dataset identities: {cross_dataset}")
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
    build.add_argument("--public-db", default=str(DEFAULT_PUBLIC_DB))
    build.add_argument("--controlled-db", default=str(DEFAULT_CONTROLLED_DB))
    build.add_argument("--clinical-db", default=str(DEFAULT_CLINICAL_DB))
    build.add_argument("--out", default=str(DEFAULT_DB))
    build.add_argument("--gzip-out")
    build.add_argument("--manifest-out")
    build.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--db", default=str(DEFAULT_DB))
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
        result = build_database(
            out,
            public_db=Path(args.public_db),
            controlled_db=Path(args.controlled_db),
            clinical_db=Path(args.clinical_db),
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
    if args.command == "ensure":
        print(json.dumps(
            ensure_release(args.repo, args.tag, Path(args.db), Path(args.manifest_out)),
            indent=2,
            sort_keys=True,
        ))
        return 0
    with connect(Path(args.db)) as conn:
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
                sql += " AND display_participant_id = ?"
                params.append(args.participant)
            sql += " ORDER BY lower(short_title), display_participant_id LIMIT ?"
            params.append(args.limit)
            for row in conn.execute(sql, params):
                print(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
