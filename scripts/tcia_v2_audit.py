#!/usr/bin/env python3
"""Split verbose V2 provenance into optional, joinable audit companions."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tcia_participant_inventory as participant_inventory
    import tcia_public_non_dicom_metadata as public_non_dicom
except ModuleNotFoundError:
    from scripts import tcia_participant_inventory as participant_inventory
    from scripts import tcia_public_non_dicom_metadata as public_non_dicom


AUDIT_SCHEMA_VERSION = 2
COMPACT_AUDIT_SCHEMA_VERSION = 3

COMPACT_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_types (
    entity_type_id INTEGER PRIMARY KEY,
    entity_table TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS fields (
    field_id INTEGER PRIMARY KEY,
    entity_type_id INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    empty_value_json TEXT NOT NULL,
    UNIQUE(entity_type_id, field_name)
);
CREATE TABLE IF NOT EXISTS entities (
    entity_key_id INTEGER PRIMARY KEY,
    entity_type_id INTEGER NOT NULL,
    entity_id TEXT NOT NULL,
    UNIQUE(entity_type_id, entity_id)
);
CREATE TABLE IF NOT EXISTS payloads (
    payload_id INTEGER PRIMARY KEY,
    payload_sha256 BLOB NOT NULL UNIQUE CHECK(length(payload_sha256) = 32),
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_payloads (
    entity_key_id INTEGER NOT NULL,
    field_id INTEGER NOT NULL,
    payload_id INTEGER NOT NULL,
    PRIMARY KEY(entity_key_id, field_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS provenance_fields (
    provenance_field_id INTEGER PRIMARY KEY,
    field_name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS provenance_source_payloads (
    source_payload_id INTEGER PRIMARY KEY,
    payload_sha256 BLOB NOT NULL UNIQUE CHECK(length(payload_sha256) = 32),
    source_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provenance_decision_payloads (
    decision_payload_id INTEGER PRIMARY KEY,
    payload_sha256 BLOB NOT NULL UNIQUE CHECK(length(payload_sha256) = 32),
    decision_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provenance_source_identifiers (
    source_identifier_id INTEGER PRIMARY KEY,
    identifier_json TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS entity_provenance_sources (
    entity_source_id INTEGER PRIMARY KEY,
    entity_key_id INTEGER NOT NULL,
    source_label TEXT NOT NULL,
    source_payload_id INTEGER NOT NULL,
    UNIQUE(entity_key_id, source_label)
);
CREATE TABLE IF NOT EXISTS field_provenance (
    entity_source_id INTEGER NOT NULL,
    provenance_field_id INTEGER NOT NULL,
    source_identifier_id INTEGER NOT NULL,
    decision_payload_id INTEGER NOT NULL,
    PRIMARY KEY(entity_source_id, provenance_field_id)
) WITHOUT ROWID;
CREATE VIEW IF NOT EXISTS agent_normalized_field_provenance AS
SELECT t.entity_table,
       e.entity_id,
       f.field_name,
       s.source_label,
       sp.source_json,
       si.identifier_json,
       dp.decision_json
FROM field_provenance fp
JOIN entity_provenance_sources s USING(entity_source_id)
JOIN provenance_source_payloads sp USING(source_payload_id)
JOIN provenance_source_identifiers si USING(source_identifier_id)
JOIN provenance_decision_payloads dp USING(decision_payload_id)
JOIN provenance_fields f USING(provenance_field_id)
JOIN entities e USING(entity_key_id)
JOIN entity_types t USING(entity_type_id);
CREATE VIEW IF NOT EXISTS agent_entity_payloads AS
SELECT t.entity_table,
       e.entity_id,
       f.field_name,
       lower(hex(p.payload_sha256)) AS payload_id,
       p.payload_json
FROM entity_payloads ep
JOIN entities e USING(entity_key_id)
JOIN entity_types t USING(entity_type_id)
JOIN fields f USING(field_id)
JOIN payloads p USING(payload_id);
"""

CONFIGS: dict[str, dict[str, Any]] = {
    "public_non_dicom": {
        "meta_table": "artifact_meta",
        "artifact": "public_non_dicom_audit",
        "main_manifest": public_non_dicom.build_manifest,
        "json_fields": {
            "public_non_dicom_assets": (
                "asset_id",
                {
                    "raw_values_json": "{}",
                    "provenance_json": "{}",
                    "quality_flag_json": "{}",
                    "geometry_details_json": "{}",
                },
            ),
            "public_non_dicom_locations": ("location_id", {"provenance_json": "{}"}),
            "public_non_dicom_geometry_assessments": (
                "geometry_assessment_id",
                {
                    "shape_json": "[]",
                    "spacing_json": "[]",
                    "origin_json": "[]",
                    "direction_json": "[]",
                    "checks_json": "{}",
                    "details_json": "{}",
                },
            ),
            "public_non_dicom_asset_relationships": (
                "relationship_id",
                {"evidence_json": "{}"},
            ),
            "public_non_dicom_asset_participants": (
                "asset_participant_id",
                {"evidence_json": "{}"},
            ),
            "public_non_dicom_review_issues": ("issue_id", {"evidence_json": "{}"}),
            "public_non_dicom_image_metadata": (
                "asset_id",
                {
                    "field_provenance_json": "{}",
                    "conflicting_values_json": "{}",
                    "quality_flag_json": "{}",
                },
            ),
            "public_non_dicom_dataset_metadata_notes": (
                "note_id",
                {"evidence_json": "{}"},
            ),
        },
        "audit_tables": (
            "public_non_dicom_crosswalk_decisions",
            "public_non_dicom_crosswalk_evidence",
        ),
    },
    "participant_inventory": {
        "meta_table": "participant_inventory_meta",
        "artifact": "participant_inventory_audit",
        "main_manifest": participant_inventory.build_manifest,
        "json_fields": {
            "participant_identifiers": (
                "participant_identifier_id",
                {"provenance_json": "{}"},
            ),
            "participant_assets": ("participant_asset_id", {"provenance_json": "{}"}),
            "participant_clinical_values": (
                "participant_clinical_value_id",
                {"provenance_json": "{}"},
            ),
            "dataset_assets_without_participant_crosswalk": (
                "dataset_asset_id",
                {"provenance_json": "{}"},
            ),
            "participant_link_issues": ("issue_id", {"evidence_json": "{}"}),
            "participant_identity_evidence": (
                "identity_evidence_id",
                {"evidence_json": "{}"},
            ),
            "participant_source_links": (
                "participant_source_link_id",
                {"evidence_json": "{}"},
            ),
        },
        "audit_tables": (),
    },
}


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def table_exists(conn: sqlite3.Connection, name: str, schema: str = "main") -> bool:
    return conn.execute(
        f"SELECT 1 FROM {quote_identifier(schema)}.sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gzip_database(
    source: Path,
    target: Path,
    *,
    compresslevel: int = 6,
    use_pigz: bool = True,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    pigz = shutil.which("pigz") if use_pigz else None
    if pigz:
        workers = max(1, min(2, (os.cpu_count() or 2) // 2))
        with target.open("wb") as output_handle:
            subprocess.run(
                [pigz, "-c", f"-{compresslevel}", "-p", str(workers), str(source)],
                stdout=output_handle,
                check=True,
            )
        return
    with source.open("rb") as input_handle, gzip.open(
        target, "wb", compresslevel=compresslevel
    ) as output_handle:
        shutil.copyfileobj(input_handle, output_handle)


def gzip_outputs(
    outputs: list[tuple[Path, Path]], *, compresslevel: int = 6
) -> dict[str, float]:
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, len(outputs))) as pool:
        futures = [
            pool.submit(gzip_database, source, target, compresslevel=compresslevel)
            for source, target in outputs
        ]
        for future in futures:
            future.result()
    return {"compression_seconds": round(time.perf_counter() - started, 3)}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def database_table_sizes(path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT name, SUM(pgsize) AS bytes FROM dbstat "
            "GROUP BY name ORDER BY bytes DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"name": str(name), "bytes": int(size)} for name, size in rows]


def configure_bulk_database(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "PRAGMA journal_mode=OFF;"
        "PRAGMA synchronous=OFF;"
        "PRAGMA temp_store=FILE;"
        "PRAGMA cache_size=-262144;"
    )


def create_research_projection(
    source_database: Path,
    research_database: Path,
    *,
    artifact: str,
    replace: bool = False,
) -> dict[str, Any]:
    if artifact not in CONFIGS:
        raise ValueError(f"Unsupported V2 artifact: {artifact}")
    if not source_database.is_file():
        raise FileNotFoundError(source_database)
    if research_database.exists():
        if not replace:
            raise FileExistsError(
                f"Research output exists: {research_database}; pass --replace"
            )
        research_database.unlink()
    research_database.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    config = CONFIGS[artifact]
    with sqlite3.connect(research_database) as conn:
        configure_bulk_database(conn)
        conn.execute(
            "ATTACH DATABASE ? AS source",
            (f"file:{source_database}?mode=ro",),
        )
        tables = list(
            conn.execute(
                "SELECT name, sql FROM source.sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
            )
        )
        for _, sql in tables:
            if sql:
                conn.execute(str(sql))
        audit_tables = set(config["audit_tables"])
        json_fields = config["json_fields"]
        copied: dict[str, int] = {}
        for table, _ in tables:
            table = str(table)
            if table in audit_tables:
                copied[table] = 0
                continue
            columns = [
                str(row[1])
                for row in conn.execute(
                    f"PRAGMA source.table_info({quote_identifier(table)})"
                )
            ]
            replacements = json_fields.get(table, ("", {}))[1]
            select_parts: list[str] = []
            params: list[Any] = []
            for column in columns:
                if column in replacements:
                    select_parts.append("?")
                    params.append(replacements[column])
                else:
                    select_parts.append(quote_identifier(column))
            column_sql = ", ".join(quote_identifier(column) for column in columns)
            select_sql = ", ".join(select_parts)
            conn.execute(
                f"INSERT INTO main.{quote_identifier(table)} ({column_sql}) "
                f"SELECT {select_sql} FROM source.{quote_identifier(table)}",
                params,
            )
            copied[table] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM main.{quote_identifier(table)}"
                ).fetchone()[0]
            )
        meta_table = str(config["meta_table"])
        conn.executemany(
            f"INSERT OR REPLACE INTO main.{quote_identifier(meta_table)} VALUES (?, ?)",
            (
                ("provenance_storage", "companion_audit_artifact"),
                ("audit_companion_asset", str(config["artifact"]) + ".sqlite.gz"),
                ("audit_schema_version", str(COMPACT_AUDIT_SCHEMA_VERSION)),
            ),
        )
        schema_objects = list(
            conn.execute(
                "SELECT type, name, sql FROM source.sqlite_master "
                "WHERE type IN ('index','trigger','view') AND sql IS NOT NULL "
                "ORDER BY CASE type WHEN 'index' THEN 0 WHEN 'trigger' THEN 1 ELSE 2 END, rowid"
            )
        )
        for _, _, sql in schema_objects:
            conn.execute(str(sql))
        conn.commit()
        conn.execute("DETACH DATABASE source")
        conn.execute("ANALYZE main")
        conn.commit()
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    return {
        "path": str(research_database),
        "integrity_check": integrity,
        "table_rows": copied,
        "sqlite_bytes": research_database.stat().st_size,
        "projection_seconds": round(time.perf_counter() - started, 3),
    }


def _flush_normalized_provenance(
    conn: sqlite3.Connection,
    sources: list[tuple[int, str, bytes, str]],
    decisions: list[tuple[int, str, str, str, bytes, str]],
) -> None:
    if not sources and not decisions:
        return
    conn.executemany(
        "INSERT INTO temp.provenance_source_stage VALUES (?,?,?,?)", sources
    )
    conn.executemany(
        "INSERT INTO temp.provenance_decision_stage VALUES (?,?,?,?,?,?)", decisions
    )
    conn.execute(
        "INSERT OR IGNORE INTO provenance_source_payloads(payload_sha256, source_json) "
        "SELECT payload_sha256, source_json FROM provenance_source_stage GROUP BY payload_sha256"
    )
    conn.execute(
        "INSERT OR IGNORE INTO entity_provenance_sources"
        "(entity_key_id, source_label, source_payload_id) "
        "SELECT s.entity_key_id, s.source_label, p.source_payload_id "
        "FROM provenance_source_stage s "
        "JOIN provenance_source_payloads p USING(payload_sha256)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO provenance_fields(field_name) "
        "SELECT field_name FROM provenance_decision_stage GROUP BY field_name"
    )
    conn.execute(
        "INSERT OR IGNORE INTO provenance_decision_payloads"
        "(payload_sha256, decision_json) "
        "SELECT payload_sha256, decision_json FROM provenance_decision_stage "
        "GROUP BY payload_sha256"
    )
    conn.execute(
        "INSERT OR IGNORE INTO provenance_source_identifiers(identifier_json) "
        "SELECT identifier_json FROM provenance_decision_stage GROUP BY identifier_json"
    )
    conn.execute(
        "INSERT OR REPLACE INTO field_provenance"
        "(entity_source_id, provenance_field_id, source_identifier_id, decision_payload_id) "
        "SELECT es.entity_source_id, f.provenance_field_id, si.source_identifier_id, "
        "p.decision_payload_id "
        "FROM provenance_decision_stage d "
        "JOIN entity_provenance_sources es "
        "  ON es.entity_key_id=d.entity_key_id AND es.source_label=d.source_label "
        "JOIN provenance_fields f USING(field_name) "
        "JOIN provenance_source_identifiers si USING(identifier_json) "
        "JOIN provenance_decision_payloads p USING(payload_sha256)"
    )
    conn.execute("DELETE FROM provenance_source_stage")
    conn.execute("DELETE FROM provenance_decision_stage")


def _store_generic_payloads(
    conn: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    field: str,
    empty_value: str,
) -> int:
    entity_type_id = int(
        conn.execute(
            "SELECT entity_type_id FROM entity_types WHERE entity_table=?", (table,)
        ).fetchone()[0]
    )
    field_id = int(
        conn.execute(
            "SELECT field_id FROM fields WHERE entity_type_id=? AND field_name=?",
            (entity_type_id, field),
        ).fetchone()[0]
    )
    conn.execute("DROP TABLE IF EXISTS temp.payload_stage")
    conn.execute(
        "CREATE TEMP TABLE payload_stage("
        "entity_id TEXT PRIMARY KEY, payload_sha256 BLOB NOT NULL, payload_json TEXT NOT NULL)"
    )
    conn.execute(
        f"INSERT INTO payload_stage "
        f"SELECT CAST({quote_identifier(id_column)} AS TEXT), "
        f"sha256_blob({quote_identifier(field)}), {quote_identifier(field)} "
        f"FROM source.{quote_identifier(table)} "
        f"WHERE COALESCE({quote_identifier(field)}, '') NOT IN ('', ?, 'null')",
        (empty_value,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO payloads(payload_sha256, payload_json) "
        "SELECT payload_sha256, payload_json FROM payload_stage GROUP BY payload_sha256"
    )
    conn.execute(
        "INSERT INTO entity_payloads(entity_key_id, field_id, payload_id) "
        "SELECT e.entity_key_id, ?, p.payload_id FROM payload_stage s "
        "JOIN entities e ON e.entity_type_id=? AND e.entity_id=s.entity_id "
        "JOIN payloads p USING(payload_sha256)",
        (field_id, entity_type_id),
    )
    count = int(conn.execute("SELECT COUNT(*) FROM payload_stage").fetchone()[0])
    conn.execute("DROP TABLE payload_stage")
    return count


def _store_normalized_field_provenance(
    conn: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    field: str,
    empty_value: str,
    batch_size: int = 5000,
) -> dict[str, int]:
    entity_type_id = int(
        conn.execute(
            "SELECT entity_type_id FROM entity_types WHERE entity_table=?", (table,)
        ).fetchone()[0]
    )
    conn.executescript(
        "CREATE TEMP TABLE provenance_source_stage("
        "entity_key_id INTEGER NOT NULL, source_label TEXT NOT NULL, "
        "payload_sha256 BLOB NOT NULL, source_json TEXT NOT NULL);"
        "CREATE TEMP TABLE provenance_decision_stage("
        "entity_key_id INTEGER NOT NULL, source_label TEXT NOT NULL, "
        "field_name TEXT NOT NULL, identifier_json TEXT NOT NULL, "
        "payload_sha256 BLOB NOT NULL, "
        "decision_json TEXT NOT NULL);"
    )
    sources: list[tuple[int, str, bytes, str]] = []
    decisions: list[tuple[int, str, str, str, bytes, str]] = []
    fallback: list[tuple[int, str]] = []
    documents = 0
    source_links = 0
    decision_links = 0
    query = (
        f"SELECT e.entity_key_id, s.{quote_identifier(field)} "
        f"FROM source.{quote_identifier(table)} s "
        f"JOIN entities e ON e.entity_type_id=? "
        f" AND e.entity_id=CAST(s.{quote_identifier(id_column)} AS TEXT) "
        f"WHERE COALESCE(s.{quote_identifier(field)}, '') NOT IN ('', ?, 'null')"
    )
    for entity_key_id, payload_json in conn.execute(
        query, (entity_type_id, empty_value)
    ):
        try:
            document = json.loads(str(payload_json))
            source_map = document.pop("_sources")
            if not isinstance(document, dict) or not isinstance(source_map, dict):
                raise ValueError("field provenance is not an object")
            parsed_decisions: list[tuple[int, str, str, str, bytes, str]] = []
            for metadata_field, decision in document.items():
                if not isinstance(decision, dict):
                    raise ValueError("field decision is not an object")
                decision_body = dict(decision)
                source_identifier = decision_body.pop("source_id", "")
                source_label = str(source_identifier)
                if not source_label or source_label not in source_map:
                    raise ValueError("field decision has no matching source")
                decision_json = canonical_json(decision_body)
                parsed_decisions.append(
                    (
                        int(entity_key_id),
                        source_label,
                        str(metadata_field),
                        canonical_json(source_identifier),
                        hashlib.sha256(decision_json.encode("utf-8")).digest(),
                        decision_json,
                    )
                )
            for source_label, source_value in source_map.items():
                source_json = canonical_json(source_value)
                sources.append(
                    (
                        int(entity_key_id),
                        str(source_label),
                        hashlib.sha256(source_json.encode("utf-8")).digest(),
                        source_json,
                    )
                )
            decisions.extend(parsed_decisions)
            documents += 1
            source_links += len(source_map)
            decision_links += len(parsed_decisions)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            fallback.append((int(entity_key_id), str(payload_json)))
        if documents and documents % batch_size == 0:
            _flush_normalized_provenance(conn, sources, decisions)
            sources.clear()
            decisions.clear()
    _flush_normalized_provenance(conn, sources, decisions)
    conn.execute("DROP TABLE provenance_source_stage")
    conn.execute("DROP TABLE provenance_decision_stage")
    if fallback:
        field_id = int(
            conn.execute(
                "SELECT field_id FROM fields WHERE entity_type_id=? AND field_name=?",
                (entity_type_id, field),
            ).fetchone()[0]
        )
        conn.execute(
            "CREATE TEMP TABLE provenance_fallback("
            "entity_key_id INTEGER PRIMARY KEY, payload_sha256 BLOB, payload_json TEXT)"
        )
        conn.executemany(
            "INSERT INTO provenance_fallback VALUES (?,?,?)",
            (
                (entity_id, hashlib.sha256(value.encode("utf-8")).digest(), value)
                for entity_id, value in fallback
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO payloads(payload_sha256, payload_json) "
            "SELECT payload_sha256, payload_json FROM provenance_fallback "
            "GROUP BY payload_sha256"
        )
        conn.execute(
            "INSERT INTO entity_payloads(entity_key_id, field_id, payload_id) "
            "SELECT f.entity_key_id, ?, p.payload_id FROM provenance_fallback f "
            "JOIN payloads p USING(payload_sha256)",
            (field_id,),
        )
        conn.execute("DROP TABLE provenance_fallback")
    return {
        "documents": documents,
        "source_links": source_links,
        "decision_links": decision_links,
        "fallback_documents": len(fallback),
    }


def reconstruct_field_provenance(
    conn: sqlite3.Connection, *, entity_table: str, entity_id: str
) -> dict[str, Any] | None:
    source_rows = list(
        conn.execute(
            "SELECT s.source_label, p.source_json "
            "FROM entity_provenance_sources s "
            "JOIN provenance_source_payloads p USING(source_payload_id) "
            "JOIN entities e USING(entity_key_id) "
            "JOIN entity_types t USING(entity_type_id) "
            "WHERE t.entity_table=? AND e.entity_id=? ORDER BY s.source_label",
            (entity_table, entity_id),
        )
    )
    rows = list(
        conn.execute(
            "SELECT field_name, source_label, source_json, identifier_json, decision_json "
            "FROM agent_normalized_field_provenance "
            "WHERE entity_table=? AND entity_id=? ORDER BY field_name, source_label",
            (entity_table, entity_id),
        )
    )
    if not source_rows and not rows:
        row = conn.execute(
            "SELECT payload_json FROM agent_entity_payloads "
            "WHERE entity_table=? AND entity_id=? AND field_name='field_provenance_json'",
            (entity_table, entity_id),
        ).fetchone()
        return json.loads(str(row[0])) if row else None
    result: dict[str, Any] = {
        "_sources": {
            str(source_label): json.loads(str(source_json))
            for source_label, source_json in source_rows
        }
    }
    for metadata_field, source_label, source_json, identifier_json, decision_json in rows:
        decision = json.loads(str(decision_json))
        decision["source_id"] = json.loads(str(identifier_json))
        result[str(metadata_field)] = decision
    return result


def verify_field_provenance_reconstruction(
    source_database: Path,
    audit_database: Path,
    *,
    sample_size: int = 1000,
) -> dict[str, Any]:
    errors: list[str] = []
    with sqlite3.connect(source_database) as source, sqlite3.connect(
        audit_database
    ) as audit_conn:
        source_counts = source.execute(
            "SELECT COUNT(*), "
            "SUM((SELECT COUNT(*) FROM json_each(m.field_provenance_json) "
            "     WHERE key <> '_sources')), "
            "SUM((SELECT COUNT(*) FROM json_each("
            "     json_extract(m.field_provenance_json, '$._sources')))) "
            "FROM public_non_dicom_image_metadata m "
            "WHERE field_provenance_json NOT IN ('', '{}', 'null')"
        ).fetchone()
        fallback_documents = int(
            audit_conn.execute(
                "SELECT COUNT(*) FROM agent_entity_payloads "
                "WHERE entity_table='public_non_dicom_image_metadata' "
                "AND field_name='field_provenance_json'"
            ).fetchone()[0]
        )
        normalized_documents = int(
            audit_conn.execute(
                "SELECT COUNT(DISTINCT entity_key_id) FROM entity_provenance_sources"
            ).fetchone()[0]
        )
        audit_counts = (
            normalized_documents + fallback_documents,
            int(audit_conn.execute("SELECT COUNT(*) FROM field_provenance").fetchone()[0]),
            int(
                audit_conn.execute(
                    "SELECT COUNT(*) FROM entity_provenance_sources"
                ).fetchone()[0]
            ),
        )
        expected_counts = tuple(int(value or 0) for value in source_counts)
        if expected_counts != audit_counts:
            errors.append(
                f"aggregate reconstruction mismatch: source={expected_counts}, audit={audit_counts}"
            )
        total = expected_counts[0]
        step = max(1, total // max(1, sample_size))
        sampled = list(
            source.execute(
                "SELECT asset_id, field_provenance_json "
                "FROM public_non_dicom_image_metadata "
                "WHERE field_provenance_json NOT IN ('', '{}', 'null') "
                "AND rowid % ? = 0 ORDER BY rowid LIMIT ?",
                (step, sample_size),
            )
        )
        mismatches: list[str] = []
        for entity_id, payload_json in sampled:
            expected = json.loads(str(payload_json))
            actual = reconstruct_field_provenance(
                audit_conn,
                entity_table="public_non_dicom_image_metadata",
                entity_id=str(entity_id),
            )
            if actual != expected:
                mismatches.append(str(entity_id))
                if len(mismatches) >= 20:
                    break
        if mismatches:
            errors.append(
                "sample reconstruction mismatches: " + ", ".join(mismatches)
            )
    return {
        "ok": not errors,
        "errors": errors,
        "source_counts": {
            "documents": expected_counts[0],
            "field_decisions": expected_counts[1],
            "source_links": expected_counts[2],
        },
        "audit_counts": {
            "documents": audit_counts[0],
            "field_decisions": audit_counts[1],
            "source_links": audit_counts[2],
        },
        "sampled_documents": len(sampled),
    }


def materialize_assembly_from_companions(
    research_database: Path,
    audit_database: Path,
    out: Path,
    *,
    artifact: str = "public_non_dicom",
    replace: bool = False,
) -> dict[str, Any]:
    """Reconstitute a lossless assembly from published V2 companions.

    The resulting database is an internal build input, not another release
    artifact.  It lets routine builds refresh unified V2 data without
    downloading the retired standalone NIfTI or pathology databases.
    """
    if artifact not in CONFIGS:
        raise ValueError(f"Unsupported V2 artifact: {artifact}")
    if not research_database.is_file() or not audit_database.is_file():
        raise FileNotFoundError("Both research and audit companion databases are required")
    if out.exists():
        if not replace:
            raise FileExistsError(f"Assembly exists: {out}; pass --replace")
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(research_database, out)
    config = CONFIGS[artifact]
    restored_fields = 0
    restored_tables: dict[str, int] = {}
    with sqlite3.connect(out) as conn:
        configure_bulk_database(conn)
        conn.execute("ATTACH DATABASE ? AS audit", (str(audit_database),))
        audit_objects = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM audit.sqlite_master WHERE type IN ('table','view')"
            )
        }
        if "agent_entity_payloads" not in audit_objects:
            raise RuntimeError("Audit companion has no agent_entity_payloads view")
        conn.execute(
            "CREATE TEMP TABLE audit_payload_stage("
            "entity_table TEXT NOT NULL, entity_id TEXT NOT NULL, "
            "field_name TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "PRIMARY KEY(entity_table, entity_id, field_name)) WITHOUT ROWID"
        )
        conn.execute(
            "INSERT INTO audit_payload_stage "
            "SELECT entity_table, entity_id, field_name, payload_json "
            "FROM audit.agent_entity_payloads"
        )
        main_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table'"
            )
        }
        for table in config["audit_tables"]:
            if table not in audit_objects:
                raise RuntimeError(f"Audit companion is missing {table}")
            conn.execute(f"DELETE FROM main.{quote_identifier(table)}")
            conn.execute(
                f"INSERT INTO main.{quote_identifier(table)} "
                f"SELECT * FROM audit.{quote_identifier(table)}"
            )
            restored_tables[table] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM main.{quote_identifier(table)}"
                ).fetchone()[0]
            )
        for table, (identifier, fields) in config["json_fields"].items():
            # The published research companion may use the previous additive
            # schema. New tables and JSON columns are created by the subsequent
            # artifact build, so they cannot be restored during baseline
            # materialization until they exist.
            if table not in main_tables:
                continue
            table_columns = {
                str(row[1])
                for row in conn.execute(
                    f"PRAGMA main.table_info({quote_identifier(table)})"
                )
            }
            if identifier not in table_columns:
                continue
            for field in fields:
                if field == "field_provenance_json" or field not in table_columns:
                    continue
                before = conn.total_changes
                conn.execute(
                    f"UPDATE main.{quote_identifier(table)} AS target "
                    f"SET {quote_identifier(field)} = ("
                    "SELECT payload_json FROM audit_payload_stage payload "
                    "WHERE payload.entity_table=? "
                    f"AND payload.entity_id=CAST(target.{quote_identifier(identifier)} AS TEXT) "
                    "AND payload.field_name=? LIMIT 1) "
                    "WHERE EXISTS (SELECT 1 FROM audit_payload_stage payload "
                    "WHERE payload.entity_table=? "
                    f"AND payload.entity_id=CAST(target.{quote_identifier(identifier)} AS TEXT) "
                    "AND payload.field_name=?)",
                    (table, field, table, field),
                )
                restored_fields += conn.total_changes - before
        conn.commit()
        conn.execute("DROP TABLE audit_payload_stage")
        conn.execute("DETACH DATABASE audit")

    # Compact audit v3 normalizes image field provenance into relational
    # dictionaries. Stream both ordered relations once instead of performing
    # one lookup set per asset.
    reconstructed = 0
    with sqlite3.connect(audit_database) as audit_conn, sqlite3.connect(out) as conn:
        fallback_rows = list(
            audit_conn.execute(
                "SELECT e.entity_id, p.payload_json "
                "FROM entity_payloads ep "
                "JOIN entities e USING(entity_key_id) "
                "JOIN entity_types t USING(entity_type_id) "
                "JOIN fields f USING(field_id) "
                "JOIN payloads p USING(payload_id) "
                "WHERE t.entity_table='public_non_dicom_image_metadata' "
                "AND f.field_name='field_provenance_json'"
            )
        )
        conn.executemany(
            "UPDATE public_non_dicom_image_metadata "
            "SET field_provenance_json=? WHERE asset_id=?",
            ((str(payload), str(entity_id)) for entity_id, payload in fallback_rows),
        )
        reconstructed += len(fallback_rows)

        source_cursor = audit_conn.execute(
            "SELECT s.entity_key_id, e.entity_id, s.source_label, p.source_json "
            "FROM entity_provenance_sources s "
            "INDEXED BY sqlite_autoindex_entity_provenance_sources_1 "
            "CROSS JOIN entities e ON e.entity_key_id=s.entity_key_id "
            "CROSS JOIN entity_types t ON t.entity_type_id=e.entity_type_id "
            "JOIN provenance_source_payloads p USING(source_payload_id) "
            "WHERE t.entity_table='public_non_dicom_image_metadata' "
            "ORDER BY s.entity_key_id, s.source_label"
        )
        decision_cursor = audit_conn.execute(
            "SELECT s.entity_key_id, e.entity_id, f.field_name, s.source_label, "
            "       i.identifier_json, p.decision_json "
            "FROM entity_provenance_sources s "
            "INDEXED BY sqlite_autoindex_entity_provenance_sources_1 "
            "CROSS JOIN entities e ON e.entity_key_id=s.entity_key_id "
            "CROSS JOIN entity_types t ON t.entity_type_id=e.entity_type_id "
            "JOIN field_provenance fp USING(entity_source_id) "
            "JOIN provenance_fields f USING(provenance_field_id) "
            "JOIN provenance_source_identifiers i USING(source_identifier_id) "
            "JOIN provenance_decision_payloads p USING(decision_payload_id) "
            "WHERE t.entity_table='public_non_dicom_image_metadata' "
            "ORDER BY s.entity_key_id, s.source_label, fp.provenance_field_id"
        )
        source_row = source_cursor.fetchone()
        decision_row = decision_cursor.fetchone()
        batch: list[tuple[str, str]] = []
        while source_row is not None or decision_row is not None:
            candidates = [
                int(row[0]) for row in (source_row, decision_row) if row is not None
            ]
            entity_key_id = min(candidates)
            entity_id = str(
                source_row[1]
                if source_row is not None and int(source_row[0]) == entity_key_id
                else decision_row[1]
            )
            value: dict[str, Any] = {"_sources": {}}
            while source_row is not None and int(source_row[0]) == entity_key_id:
                value["_sources"][str(source_row[2])] = json.loads(str(source_row[3]))
                source_row = source_cursor.fetchone()
            while decision_row is not None and int(decision_row[0]) == entity_key_id:
                decision = json.loads(str(decision_row[5]))
                decision["source_id"] = json.loads(str(decision_row[4]))
                value[str(decision_row[2])] = decision
                decision_row = decision_cursor.fetchone()
            batch.append((canonical_json(value), entity_id))
            reconstructed += 1
            if len(batch) >= 10000:
                conn.executemany(
                    "UPDATE public_non_dicom_image_metadata "
                    "SET field_provenance_json=? WHERE asset_id=?",
                    batch,
                )
                conn.commit()
                batch.clear()
        if batch:
            conn.executemany(
                "UPDATE public_non_dicom_image_metadata "
                "SET field_provenance_json=? WHERE asset_id=?",
                batch,
            )
        conn.execute(
            "DELETE FROM artifact_meta WHERE key IN "
            "('provenance_storage','audit_companion_asset','audit_schema_version')"
        )
        conn.commit()
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    return {
        "path": str(out),
        "artifact": artifact,
        "integrity_check": integrity,
        "restored_json_fields": restored_fields + reconstructed,
        "reconstructed_field_provenance": reconstructed,
        "restored_audit_tables": restored_tables,
        "research_sha256": file_sha256(research_database),
        "audit_sha256": file_sha256(audit_database),
        "sqlite_sha256": file_sha256(out),
    }


def build_compact_audit_database(
    source_database: Path,
    audit_database: Path,
    *,
    artifact: str,
    staging_database: Path | None = None,
    checkpoint_database: Path | None = None,
    consume_checkpoint: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    if artifact not in CONFIGS:
        raise ValueError(f"Unsupported V2 artifact: {artifact}")
    if not source_database.is_file():
        raise FileNotFoundError(source_database)
    if audit_database.exists():
        if not replace:
            raise FileExistsError(f"Audit output exists: {audit_database}; pass --replace")
        audit_database.unlink()
    audit_database.parent.mkdir(parents=True, exist_ok=True)
    if checkpoint_database is not None:
        if artifact != "public_non_dicom":
            raise ValueError("The legacy-detail checkpoint belongs to public non-DICOM audit")
        if not checkpoint_database.is_file():
            raise FileNotFoundError(checkpoint_database)
        if consume_checkpoint:
            os.replace(checkpoint_database, audit_database)
        else:
            shutil.copy2(checkpoint_database, audit_database)
    started = time.perf_counter()
    config = CONFIGS[artifact]
    counts: dict[str, Any] = {}
    with sqlite3.connect(audit_database) as conn:
        configure_bulk_database(conn)
        conn.create_function(
            "sha256_blob",
            1,
            lambda value: hashlib.sha256(str(value).encode("utf-8")).digest(),
            deterministic=True,
        )
        conn.executescript(COMPACT_AUDIT_SCHEMA)
        conn.execute(
            "ATTACH DATABASE ? AS source", (f"file:{source_database}?mode=ro",)
        )
        for table, (id_column, fields) in config["json_fields"].items():
            if not table_exists(conn, table, "source"):
                continue
            conn.execute(
                "INSERT INTO entity_types(entity_table) VALUES (?)", (table,)
            )
            entity_type_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.executemany(
                "INSERT INTO fields(entity_type_id, field_name, empty_value_json) "
                "VALUES (?, ?, ?)",
                ((entity_type_id, field, empty) for field, empty in fields.items()),
            )
            conn.execute(
                f"INSERT INTO entities(entity_type_id, entity_id) "
                f"SELECT ?, CAST({quote_identifier(id_column)} AS TEXT) "
                f"FROM source.{quote_identifier(table)}",
                (entity_type_id,),
            )
            for field, empty_value in fields.items():
                if (
                    artifact == "public_non_dicom"
                    and table == "public_non_dicom_image_metadata"
                    and field == "field_provenance_json"
                ):
                    counts[f"{table}.{field}"] = _store_normalized_field_provenance(
                        conn,
                        table=table,
                        id_column=id_column,
                        field=field,
                        empty_value=empty_value,
                    )
                else:
                    counts[f"{table}.{field}"] = _store_generic_payloads(
                        conn,
                        table=table,
                        id_column=id_column,
                        field=field,
                        empty_value=empty_value,
                    )
        for table in config["audit_tables"]:
            if not table_exists(conn, table, "source"):
                continue
            sql_row = conn.execute(
                "SELECT sql FROM source.sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if sql_row and sql_row[0]:
                conn.execute(str(sql_row[0]))
                conn.execute(
                    f"INSERT INTO main.{quote_identifier(table)} "
                    f"SELECT * FROM source.{quote_identifier(table)}"
                )
                counts[table] = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM main.{quote_identifier(table)}"
                    ).fetchone()[0]
                )
        staging_fingerprint = ""
        if staging_database is not None:
            if not staging_database.is_file():
                raise FileNotFoundError(staging_database)
            conn.execute(
                "ATTACH DATABASE ? AS staging",
                (f"file:{staging_database}?mode=ro",),
            )
            for table in ("staging_meta", "staging_sources", "staging_object_inventory"):
                if not table_exists(conn, table, "staging"):
                    raise RuntimeError(f"Staging ledger is missing: {table}")
                conn.execute(
                    f"CREATE TABLE main.{quote_identifier(table)} AS "
                    f"SELECT * FROM staging.{quote_identifier(table)}"
                )
            row = conn.execute(
                "SELECT value FROM staging.staging_meta WHERE key='source_fingerprint'"
            ).fetchone()
            staging_fingerprint = str(row[0]) if row else ""
        checkpoint_fingerprint = ""
        if table_exists(conn, "checkpoint_meta"):
            row = conn.execute(
                "SELECT value FROM checkpoint_meta WHERE key='checkpoint_fingerprint'"
            ).fetchone()
            checkpoint_fingerprint = str(row[0]) if row else ""
        audit_meta = {
            "artifact_family": "tcia-metadata-v2",
            "artifact": config["artifact"],
            "schema_version": str(COMPACT_AUDIT_SCHEMA_VERSION),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "research_artifact": artifact,
            "research_database_asset": source_database.name + ".gz",
            "join_contract": "Join integer audit entities to stable source entity identifiers through agent_entity_payloads or agent_normalized_field_provenance.",
            "normalized_field_provenance_contract": "Reconstruct canonical field_provenance_json from source and decision payload dictionaries without loss of parsed values.",
            "counts": canonical_json(counts),
        }
        if staging_fingerprint:
            audit_meta["staging_source_fingerprint"] = staging_fingerprint
        if checkpoint_fingerprint:
            audit_meta["legacy_detail_checkpoint_fingerprint"] = checkpoint_fingerprint
            audit_meta["legacy_detail_checkpoint_contract"] = (
                "Exact specialized NIfTI and pathology source rows retained in source_* tables."
            )
        conn.executemany(
            "INSERT OR REPLACE INTO audit_meta VALUES (?, ?)", audit_meta.items()
        )
        conn.commit()
        conn.execute("ANALYZE main")
        conn.commit()
        integrity = str(conn.execute("PRAGMA main.integrity_check").fetchone()[0])
    validation = validate_audit_database(audit_database, artifact=artifact)
    return {
        "path": str(audit_database),
        "schema_version": COMPACT_AUDIT_SCHEMA_VERSION,
        "integrity_check": integrity,
        "audit_validation": validation,
        "counts": counts,
        "sqlite_bytes": audit_database.stat().st_size,
        "audit_build_seconds": round(time.perf_counter() - started, 3),
    }


def project_database_v3(
    source_database: Path,
    research_database: Path,
    audit_database: Path,
    *,
    artifact: str,
    staging_database: Path | None = None,
    checkpoint_database: Path | None = None,
    consume_checkpoint: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    if artifact != "public_non_dicom":
        raise ValueError(
            "Compact audit schema 3 is currently scoped to public_non_dicom"
        )
    started = time.perf_counter()
    audit_result = build_compact_audit_database(
        source_database,
        audit_database,
        artifact=artifact,
        staging_database=staging_database,
        checkpoint_database=checkpoint_database,
        consume_checkpoint=consume_checkpoint,
        replace=replace,
    )
    research_result = create_research_projection(
        source_database,
        research_database,
        artifact=artifact,
        replace=replace,
    )
    with sqlite3.connect(audit_database) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO audit_meta VALUES (?, ?)",
            ("research_database_asset", research_database.name + ".gz"),
        )
        conn.commit()
    audit_result["largest_tables"] = database_table_sizes(audit_database)
    research_result["largest_tables"] = database_table_sizes(research_database)
    return {
        "artifact": artifact,
        "schema_version": COMPACT_AUDIT_SCHEMA_VERSION,
        "source_database": str(source_database),
        "source_sqlite_bytes": source_database.stat().st_size,
        "research": research_result,
        "audit": audit_result,
        "total_projection_seconds": round(time.perf_counter() - started, 3),
    }


def split_database(
    database: Path,
    audit_database: Path,
    *,
    artifact: str,
    staging_database: Path | None = None,
    checkpoint_database: Path | None = None,
    consume_checkpoint: bool = False,
    clinical_qc_csv: Path | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    if artifact not in CONFIGS:
        raise ValueError(f"Unsupported V2 artifact: {artifact}")
    if not database.is_file():
        raise FileNotFoundError(database)
    if audit_database.exists():
        if not replace:
            raise FileExistsError(f"Audit output exists: {audit_database}; pass --replace")
        audit_database.unlink()
    audit_database.parent.mkdir(parents=True, exist_ok=True)
    if checkpoint_database is not None:
        if artifact != "public_non_dicom":
            raise ValueError("The legacy-detail checkpoint belongs to public non-DICOM audit")
        if not checkpoint_database.is_file():
            raise FileNotFoundError(checkpoint_database)
        if consume_checkpoint:
            os.replace(checkpoint_database, audit_database)
        else:
            shutil.copy2(checkpoint_database, audit_database)
    config = CONFIGS[artifact]
    counts: dict[str, int] = {}
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.create_function(
            "sha256_text",
            1,
            lambda value: hashlib.sha256(str(value).encode("utf-8")).hexdigest(),
            deterministic=True,
        )
        meta_table = str(config["meta_table"])
        existing = conn.execute(
            f"SELECT value FROM {quote_identifier(meta_table)} WHERE key='provenance_storage'"
        ).fetchone()
        if existing and existing[0] == "companion_audit_artifact":
            raise RuntimeError(f"{database} is already split into research and audit artifacts")
        conn.execute("ATTACH DATABASE ? AS audit", (str(audit_database),))
        conn.executescript(
            """
            CREATE TABLE audit.audit_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE audit.payloads (
                payload_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE audit.entity_payloads (
                entity_table TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                field_name TEXT NOT NULL,
                payload_id TEXT NOT NULL,
                PRIMARY KEY (entity_table, entity_id, field_name)
            );
            CREATE INDEX audit.idx_entity_payloads_entity
                ON entity_payloads(entity_table, entity_id);
            CREATE VIEW audit.agent_entity_payloads AS
            SELECT e.entity_table, e.entity_id, e.field_name, e.payload_id,
                   p.payload_json
            FROM entity_payloads e
            JOIN payloads p USING(payload_id);
            """
        )
        staging_fingerprint = ""
        if staging_database is not None:
            if artifact != "public_non_dicom":
                raise ValueError("The staging checkpoint belongs to the public non-DICOM audit artifact")
            if not staging_database.is_file():
                raise FileNotFoundError(staging_database)
            conn.execute("ATTACH DATABASE ? AS staging", (str(staging_database),))
            required_staging = (
                "staging_meta",
                "staging_sources",
                "staging_object_inventory",
            )
            missing_staging = [
                table for table in required_staging if not table_exists(conn, table, "staging")
            ]
            if missing_staging:
                raise RuntimeError(
                    "Staging ledger is missing: " + ", ".join(missing_staging)
                )
            for table in required_staging:
                conn.execute(
                    f"CREATE TABLE audit.{quote_identifier(table)} AS "
                    f"SELECT * FROM staging.{quote_identifier(table)}"
                )
                counts[table] = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM audit.{quote_identifier(table)}"
                    ).fetchone()[0]
                )
            row = conn.execute(
                "SELECT value FROM staging.staging_meta WHERE key='source_fingerprint'"
            ).fetchone()
            staging_fingerprint = str(row[0]) if row else ""
        checkpoint_fingerprint = ""
        if table_exists(conn, "checkpoint_meta", "audit"):
            row = conn.execute(
                "SELECT value FROM audit.checkpoint_meta "
                "WHERE key='checkpoint_fingerprint'"
            ).fetchone()
            checkpoint_fingerprint = str(row[0]) if row else ""
        if clinical_qc_csv is not None:
            if artifact != "participant_inventory":
                raise ValueError("Clinical QC rows belong to Participant Inventory audit")
            if not clinical_qc_csv.is_file():
                raise FileNotFoundError(clinical_qc_csv)
            conn.execute(
                "CREATE TABLE audit.clinical_qc_manual_review ("
                "source_row_number INTEGER PRIMARY KEY, row_json TEXT NOT NULL)"
            )
            with clinical_qc_csv.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                rows = [
                    (
                        index,
                        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    )
                    for index, row in enumerate(reader, start=2)
                ]
            conn.executemany(
                "INSERT INTO audit.clinical_qc_manual_review VALUES (?, ?)", rows
            )
            counts["clinical_qc_manual_review"] = len(rows)
        for table, (id_column, fields) in config["json_fields"].items():
            if not table_exists(conn, table):
                continue
            for field, empty_value in fields.items():
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO audit.payloads (payload_id, payload_json)
                    SELECT sha256_text({quote_identifier(field)}), {quote_identifier(field)}
                    FROM main.{quote_identifier(table)}
                    WHERE COALESCE({quote_identifier(field)}, '') NOT IN ('', ?, 'null')
                    """,
                    (empty_value,),
                )
                before = conn.total_changes
                conn.execute(
                    f"""
                    INSERT INTO audit.entity_payloads
                      (entity_table, entity_id, field_name, payload_id)
                    SELECT ?, CAST({quote_identifier(id_column)} AS TEXT), ?,
                           sha256_text({quote_identifier(field)})
                    FROM main.{quote_identifier(table)}
                    WHERE COALESCE({quote_identifier(field)}, '') NOT IN ('', ?, 'null')
                    """,
                    (table, field, empty_value),
                )
                counts[f"{table}.{field}"] = conn.total_changes - before
                conn.execute(
                    f"UPDATE main.{quote_identifier(table)} SET {quote_identifier(field)}=?",
                    (empty_value,),
                )
        for table in config["audit_tables"]:
            if not table_exists(conn, table):
                continue
            conn.execute(
                f"CREATE TABLE audit.{quote_identifier(table)} AS "
                f"SELECT * FROM main.{quote_identifier(table)}"
            )
            row_count = conn.execute(
                f"SELECT COUNT(*) FROM audit.{quote_identifier(table)}"
            ).fetchone()[0]
            counts[table] = int(row_count)
            conn.execute(f"DELETE FROM main.{quote_identifier(table)}")
        generated = datetime.now(timezone.utc).isoformat()
        audit_meta = {
            "artifact_family": "tcia-metadata-v2",
            "artifact": config["artifact"],
            "schema_version": AUDIT_SCHEMA_VERSION,
            "generated_at_utc": generated,
            "research_artifact": artifact,
            "research_database_asset": database.name + ".gz",
            "join_contract": "Join entity_payloads.entity_id to the stable primary identifier in entity_table.",
            "counts": json.dumps(counts, sort_keys=True, separators=(",", ":")),
        }
        if staging_fingerprint:
            audit_meta["staging_source_fingerprint"] = staging_fingerprint
            audit_meta["staging_checkpoint_contract"] = (
                "Path-independent source and schema ledger; build inputs remain external."
            )
        if checkpoint_fingerprint:
            audit_meta["legacy_detail_checkpoint_fingerprint"] = checkpoint_fingerprint
            audit_meta["legacy_detail_checkpoint_contract"] = (
                "Exact specialized NIfTI and pathology source rows retained in source_* tables."
            )
        conn.executemany(
            "INSERT INTO audit.audit_meta VALUES (?, ?)", audit_meta.items()
        )
        main_meta = {
            "provenance_storage": "companion_audit_artifact",
            "audit_companion_asset": audit_database.name + ".gz",
            "audit_schema_version": str(AUDIT_SCHEMA_VERSION),
        }
        conn.executemany(
            f"INSERT OR REPLACE INTO main.{quote_identifier(meta_table)} VALUES (?, ?)",
            main_meta.items(),
        )
        conn.commit()
        if staging_database is not None:
            conn.execute("DETACH DATABASE staging")
        conn.execute("DETACH DATABASE audit")
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    audit_validation = validate_audit_database(audit_database, artifact=artifact)
    return {
        "artifact": artifact,
        "research_database": str(database),
        "audit_database": str(audit_database),
        "integrity_check": integrity,
        "audit_validation": audit_validation,
        "counts": counts,
    }


def validate_audit_database(path: Path, *, artifact: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    counts: dict[str, int] = {}
    with sqlite3.connect(path) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"integrity_check={integrity}")
        objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        for required in ("audit_meta", "payloads", "entity_payloads", "agent_entity_payloads"):
            if required not in objects:
                errors.append(f"missing table: {required}")
        meta = dict(conn.execute("SELECT key, value FROM audit_meta")) if "audit_meta" in objects else {}
        if artifact and meta.get("research_artifact") != artifact:
            errors.append("audit research artifact does not match")
        schema_version = int(meta.get("schema_version", AUDIT_SCHEMA_VERSION))
        if schema_version == COMPACT_AUDIT_SCHEMA_VERSION:
            compact_required = {
                "entity_types",
                "fields",
                "entities",
                "provenance_fields",
                "provenance_source_payloads",
                "provenance_source_identifiers",
                "provenance_decision_payloads",
                "entity_provenance_sources",
                "field_provenance",
                "agent_normalized_field_provenance",
            }
            missing_compact = sorted(compact_required - objects)
            if missing_compact:
                errors.append("missing compact audit objects: " + ", ".join(missing_compact))
        if "entity_payloads" in objects:
            counts["entity_payloads"] = conn.execute(
                "SELECT COUNT(*) FROM entity_payloads"
            ).fetchone()[0]
            orphan_payloads = conn.execute(
                "SELECT COUNT(*) FROM entity_payloads e "
                "LEFT JOIN payloads p USING(payload_id) WHERE p.payload_id IS NULL"
            ).fetchone()[0]
            if orphan_payloads:
                errors.append(f"orphan payload references: {orphan_payloads}")
        if "payloads" in objects:
            counts["unique_payloads"] = conn.execute(
                "SELECT COUNT(*) FROM payloads"
            ).fetchone()[0]
        if schema_version == COMPACT_AUDIT_SCHEMA_VERSION and "field_provenance" in objects:
            counts["normalized_field_decisions"] = int(
                conn.execute("SELECT COUNT(*) FROM field_provenance").fetchone()[0]
            )
            counts["normalized_entity_sources"] = int(
                conn.execute("SELECT COUNT(*) FROM entity_provenance_sources").fetchone()[0]
            )
            counts["unique_source_payloads"] = int(
                conn.execute("SELECT COUNT(*) FROM provenance_source_payloads").fetchone()[0]
            )
            counts["unique_decision_payloads"] = int(
                conn.execute("SELECT COUNT(*) FROM provenance_decision_payloads").fetchone()[0]
            )
            counts["unique_source_identifiers"] = int(
                conn.execute("SELECT COUNT(*) FROM provenance_source_identifiers").fetchone()[0]
            )
            orphan_sources = int(
                conn.execute(
                    "SELECT COUNT(*) FROM entity_provenance_sources s "
                    "LEFT JOIN entities e USING(entity_key_id) "
                    "LEFT JOIN provenance_source_payloads p USING(source_payload_id) "
                    "WHERE e.entity_key_id IS NULL OR p.source_payload_id IS NULL"
                ).fetchone()[0]
            )
            orphan_decisions = int(
                conn.execute(
                    "SELECT COUNT(*) FROM field_provenance f "
                    "LEFT JOIN entity_provenance_sources s USING(entity_source_id) "
                    "LEFT JOIN provenance_fields n USING(provenance_field_id) "
                    "LEFT JOIN provenance_source_identifiers i USING(source_identifier_id) "
                    "LEFT JOIN provenance_decision_payloads p USING(decision_payload_id) "
                    "WHERE s.entity_source_id IS NULL OR n.provenance_field_id IS NULL "
                    "OR i.source_identifier_id IS NULL OR p.decision_payload_id IS NULL"
                ).fetchone()[0]
            )
            if orphan_sources or orphan_decisions:
                errors.append(
                    f"orphan normalized provenance references: "
                    f"sources={orphan_sources}, decisions={orphan_decisions}"
                )
        for table in sorted(objects):
            if table.startswith("public_non_dicom_crosswalk_"):
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table)}"
                ).fetchone()[0]
        if (
            artifact == "public_non_dicom"
            and counts.get("public_non_dicom_crosswalk_evidence", 0) > 1000
        ):
            brats_reviewed = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT raw_subject_id) "
                    "FROM public_non_dicom_crosswalk_evidence "
                    "WHERE short_title='RSNA-ASNR-MICCAI-BraTS-2021' "
                    "AND mapping_method='official_tcia_brats2021_workbook'"
                ).fetchone()[0]
            )
            remind_participants = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT resolved_subject_id) "
                    "FROM public_non_dicom_crosswalk_evidence "
                    "WHERE short_title='ReMIND' "
                    "AND mapping_method='reviewed_remind_package_subject_folder_and_filename'"
                ).fetchone()[0]
            )
            remind_files = int(
                conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_crosswalk_evidence "
                    "WHERE short_title='ReMIND' "
                    "AND mapping_method='reviewed_remind_package_subject_folder_and_filename'"
                ).fetchone()[0]
            )
            counts.update(
                {
                    "brats_reviewed_source_crosswalks": brats_reviewed,
                    "remind_reviewed_participants": remind_participants,
                    "remind_reviewed_files": remind_files,
                }
            )
            for name, actual, expected in (
                ("brats_reviewed_source_crosswalks", brats_reviewed, 1066),
                ("remind_reviewed_participants", remind_participants, 114),
                ("remind_reviewed_files", remind_files, 356),
            ):
                if actual != expected:
                    errors.append(
                        f"public non-DICOM audit coverage regression: "
                        f"{name}={actual} != {expected}"
                    )
        staging_objects = {
            "staging_meta", "staging_sources", "staging_object_inventory"
        }
        present_staging = staging_objects & objects
        if present_staging and present_staging != staging_objects:
            errors.append(
                "incomplete staging checkpoint: "
                + ", ".join(sorted(staging_objects - present_staging))
            )
        if present_staging == staging_objects:
            counts["staging_sources"] = conn.execute(
                "SELECT COUNT(*) FROM staging_sources"
            ).fetchone()[0]
            counts["staging_objects"] = conn.execute(
                "SELECT COUNT(*) FROM staging_object_inventory"
            ).fetchone()[0]
        checkpoint_objects = {"checkpoint_meta", "checkpoint_inventory"}
        present_checkpoint = checkpoint_objects & objects
        if present_checkpoint and present_checkpoint != checkpoint_objects:
            errors.append(
                "incomplete legacy-detail checkpoint: "
                + ", ".join(sorted(checkpoint_objects - present_checkpoint))
            )
        if present_checkpoint == checkpoint_objects:
            checkpoint_rows = list(
                conn.execute(
                    "SELECT checkpoint_table, checkpoint_rows FROM checkpoint_inventory"
                )
            )
            counts["checkpoint_tables"] = len(checkpoint_rows)
            counts["checkpoint_rows"] = sum(int(row[1]) for row in checkpoint_rows)
            for table, expected_rows in checkpoint_rows:
                if str(table) not in objects:
                    errors.append(f"missing checkpoint table: {table}")
                    continue
                actual_rows = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {quote_identifier(str(table))}"
                    ).fetchone()[0]
                )
                if actual_rows != int(expected_rows):
                    errors.append(
                        f"checkpoint row mismatch: {table}={actual_rows}/{expected_rows}"
                    )
        if "clinical_qc_manual_review" in objects:
            counts["clinical_qc_manual_review"] = conn.execute(
                "SELECT COUNT(*) FROM clinical_qc_manual_review"
            ).fetchone()[0]
    return {
        "ok": not errors,
        "errors": errors,
        "integrity_check": integrity,
        "schema_version": schema_version,
        "counts": counts,
    }


def build_audit_manifest(
    audit_database: Path,
    audit_gzip: Path | None,
    *,
    artifact: str,
) -> dict[str, Any]:
    validation = validate_audit_database(audit_database, artifact=artifact)
    if not validation["ok"]:
        raise RuntimeError("Cannot manifest invalid audit database: " + "; ".join(validation["errors"]))
    audit_artifact = str(CONFIGS[artifact]["artifact"])
    result: dict[str, Any] = {
        "artifact_family": "tcia-metadata-v2",
        "artifact": audit_artifact,
        "asset": audit_database.name + ".gz",
        "schema_version": validation["schema_version"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "research_artifact": artifact,
        "sqlite_bytes": audit_database.stat().st_size,
        "sqlite_sha256": file_sha256(audit_database),
        "counts": validation["counts"],
    }
    if audit_gzip:
        result["gzip_bytes"] = audit_gzip.stat().st_size
        result["gzip_sha256"] = file_sha256(audit_gzip)
    fingerprint = {
        key: result[key]
        for key in ("artifact", "schema_version", "research_artifact", "sqlite_sha256", "counts")
    }
    result["release_fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    split = sub.add_parser("split")
    split.add_argument("--artifact", choices=sorted(CONFIGS), required=True)
    split.add_argument("--db", required=True)
    split.add_argument("--audit-out", required=True)
    split.add_argument("--research-gzip-out")
    split.add_argument("--research-manifest-out")
    split.add_argument("--audit-gzip-out")
    split.add_argument("--audit-manifest-out")
    split.add_argument(
        "--staging-db",
        help="Embed the path-independent runner staging ledger in public non-DICOM audit output.",
    )
    split.add_argument(
        "--checkpoint-db",
        help="Seed public non-DICOM audit with exact specialized legacy-detail source rows.",
    )
    split.add_argument(
        "--consume-checkpoint",
        action="store_true",
        help="Move the runner-local checkpoint into the audit output to avoid a multi-GB copy.",
    )
    split.add_argument(
        "--clinical-qc-csv",
        help="Embed the clinical manual-review CSV losslessly in Participant Inventory audit.",
    )
    split.add_argument("--gzip-level", type=int, choices=range(1, 10), default=6)
    split.add_argument("--metrics-out")
    split.add_argument("--replace", action="store_true")
    project = sub.add_parser(
        "project-v3",
        help="Project an assembly database directly into compact research and audit outputs.",
    )
    project.add_argument("--artifact", choices=("public_non_dicom",), required=True)
    project.add_argument("--db", required=True, help="Read-only assembly database.")
    project.add_argument("--research-out", required=True)
    project.add_argument("--audit-out", required=True)
    project.add_argument("--research-gzip-out")
    project.add_argument("--research-manifest-out")
    project.add_argument("--audit-gzip-out")
    project.add_argument("--audit-manifest-out")
    project.add_argument("--staging-db")
    project.add_argument("--checkpoint-db")
    project.add_argument("--consume-checkpoint", action="store_true")
    project.add_argument("--gzip-level", type=int, choices=range(1, 10), default=3)
    project.add_argument("--metrics-out")
    project.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--artifact", choices=sorted(CONFIGS))
    validate.add_argument("--db", required=True)
    reconstruction = sub.add_parser("verify-reconstruction")
    reconstruction.add_argument("--source-db", required=True)
    reconstruction.add_argument("--audit-db", required=True)
    reconstruction.add_argument("--sample-size", type=int, default=1000)
    reconstruction.add_argument("--out")
    materialize = sub.add_parser(
        "materialize-assembly",
        help="Reconstitute a build assembly from manifest-pinned V2 companions.",
    )
    materialize.add_argument("--artifact", choices=("public_non_dicom",), required=True)
    materialize.add_argument("--research-db", required=True)
    materialize.add_argument("--audit-db", required=True)
    materialize.add_argument("--out", required=True)
    materialize.add_argument("--replace", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "validate":
        result = validate_audit_database(Path(args.db), artifact=args.artifact)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command == "verify-reconstruction":
        result = verify_field_provenance_reconstruction(
            Path(args.source_db),
            Path(args.audit_db),
            sample_size=args.sample_size,
        )
        if args.out:
            output = Path(args.out)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command == "materialize-assembly":
        result = materialize_assembly_from_companions(
            Path(args.research_db),
            Path(args.audit_db),
            Path(args.out),
            artifact=args.artifact,
            replace=args.replace,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["integrity_check"] == "ok" else 1
    source_database = Path(args.db)
    audit_database = Path(args.audit_out)
    if args.command == "project-v3":
        database = Path(args.research_out)
        result = project_database_v3(
            source_database,
            database,
            audit_database,
            artifact=args.artifact,
            staging_database=Path(args.staging_db) if args.staging_db else None,
            checkpoint_database=Path(args.checkpoint_db) if args.checkpoint_db else None,
            consume_checkpoint=args.consume_checkpoint,
            replace=args.replace,
        )
    else:
        database = source_database
        result = split_database(
            database,
            audit_database,
            artifact=args.artifact,
            staging_database=Path(args.staging_db) if args.staging_db else None,
            checkpoint_database=Path(args.checkpoint_db) if args.checkpoint_db else None,
            consume_checkpoint=args.consume_checkpoint,
            clinical_qc_csv=Path(args.clinical_qc_csv) if args.clinical_qc_csv else None,
            replace=args.replace,
        )
    research_gzip = Path(args.research_gzip_out) if args.research_gzip_out else None
    audit_gzip = Path(args.audit_gzip_out) if args.audit_gzip_out else None
    gzip_jobs = []
    if research_gzip:
        gzip_jobs.append((database, research_gzip))
    if audit_gzip:
        gzip_jobs.append((audit_database, audit_gzip))
    if gzip_jobs and args.command == "project-v3":
        result["compression"] = gzip_outputs(
            gzip_jobs, compresslevel=args.gzip_level
        )
    elif gzip_jobs:
        compression_started = time.perf_counter()
        for source, target in gzip_jobs:
            gzip_database(
                source,
                target,
                compresslevel=args.gzip_level,
                use_pigz=False,
            )
        result["compression"] = {
            "compression_seconds": round(time.perf_counter() - compression_started, 3)
        }
    if args.research_manifest_out:
        manifest = CONFIGS[args.artifact]["main_manifest"](database, research_gzip)
        Path(args.research_manifest_out).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        result["research_manifest"] = manifest
    if args.audit_manifest_out:
        manifest = build_audit_manifest(
            audit_database,
            audit_gzip,
            artifact=args.artifact,
        )
        Path(args.audit_manifest_out).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        result["audit_manifest"] = manifest
    result["output_sizes"] = {
        "research_sqlite_bytes": database.stat().st_size,
        "audit_sqlite_bytes": audit_database.stat().st_size,
        "research_gzip_bytes": research_gzip.stat().st_size if research_gzip else None,
        "audit_gzip_bytes": audit_gzip.stat().st_size if audit_gzip else None,
    }
    if getattr(args, "metrics_out", None):
        metrics_path = Path(args.metrics_out)
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
