#!/usr/bin/env python3
"""Split verbose V2 provenance into optional, joinable audit companions."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
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
                },
            ),
            "public_non_dicom_locations": ("location_id", {"provenance_json": "{}"}),
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


def gzip_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_handle, gzip.open(
        target, "wb", compresslevel=6
    ) as output_handle:
        shutil.copyfileobj(input_handle, output_handle)


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
        for table in sorted(objects):
            if table.startswith("public_non_dicom_crosswalk_"):
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table)}"
                ).fetchone()[0]
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
    return {"ok": not errors, "errors": errors, "integrity_check": integrity, "counts": counts}


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
        "schema_version": AUDIT_SCHEMA_VERSION,
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
    split.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--artifact", choices=sorted(CONFIGS))
    validate.add_argument("--db", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "validate":
        result = validate_audit_database(Path(args.db), artifact=args.artifact)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    database = Path(args.db)
    audit_database = Path(args.audit_out)
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
    if research_gzip:
        gzip_database(database, research_gzip)
    if audit_gzip:
        gzip_database(audit_database, audit_gzip)
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
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
