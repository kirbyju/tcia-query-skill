#!/usr/bin/env python3
"""Checkpoint legacy specialized detail for retirement into the V2 audit artifact."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

try:
    from tcia_v2_staging import file_sha256, resolve_component
except ModuleNotFoundError:
    from scripts.tcia_v2_staging import file_sha256, resolve_component


CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_TABLES = {
    "nifti": (
        "derived_objects",
        "derived_object_references",
        "nifti_classification_rules",
        "nifti_file_characteristics",
        "nifti_dataset_review_issues",
        "metadata_quality_flags",
        "annotation_groups",
    ),
    "pathology": (
        "pathology_download_label_matches",
        "pathology_package_files",
        "pathdb_slide_crosswalk",
        "pathology_disparities",
    ),
}


SCHEMA = """
CREATE TABLE checkpoint_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE checkpoint_inventory (
    component TEXT NOT NULL,
    source_table TEXT NOT NULL,
    checkpoint_table TEXT NOT NULL UNIQUE,
    source_rows INTEGER NOT NULL,
    checkpoint_rows INTEGER NOT NULL,
    source_sql_sha256 TEXT NOT NULL,
    source_database_sha256 TEXT NOT NULL,
    PRIMARY KEY (component, source_table)
);
"""


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def source_digest(staging_db: Path, component: str) -> str:
    with closing(sqlite3.connect(staging_db)) as conn:
        row = conn.execute(
            "SELECT sqlite_sha256 FROM staging_sources WHERE component=?",
            (component,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"Staging ledger has no source digest for {component}")
    return str(row[0])


def build_checkpoint(
    out: Path,
    *,
    staging_db: Path,
    replace: bool = False,
) -> dict[str, Any]:
    if out.exists():
        if not replace:
            raise FileExistsError(f"Checkpoint exists: {out}; pass --replace")
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    copied: dict[str, int] = {}
    with closing(sqlite3.connect(out)) as conn:
        conn.executescript(SCHEMA)
        inventory_rows: list[tuple[Any, ...]] = []
        for component, tables in CHECKPOINT_TABLES.items():
            source_path = resolve_component(staging_db, component, verify_hash=False)
            digest = source_digest(staging_db, component)
            conn.execute("ATTACH DATABASE ? AS source", (str(source_path),))
            source_objects = {
                str(row[0]): str(row[1] or "")
                for row in conn.execute(
                    "SELECT name, sql FROM source.sqlite_master WHERE type='table'"
                )
            }
            missing = sorted(set(tables) - set(source_objects))
            if missing:
                raise RuntimeError(
                    f"{component} source is missing checkpoint tables: {', '.join(missing)}"
                )
            for table in tables:
                checkpoint_table = f"source_{component}__{table}"
                conn.execute(
                    f"CREATE TABLE {quote_identifier(checkpoint_table)} AS "
                    f"SELECT * FROM source.{quote_identifier(table)}"
                )
                source_rows = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM source.{quote_identifier(table)}"
                    ).fetchone()[0]
                )
                checkpoint_rows = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {quote_identifier(checkpoint_table)}"
                    ).fetchone()[0]
                )
                if source_rows != checkpoint_rows:
                    raise RuntimeError(
                        f"Checkpoint row mismatch for {component}.{table}: "
                        f"{source_rows} != {checkpoint_rows}"
                    )
                inventory_rows.append(
                    (
                        component,
                        table,
                        checkpoint_table,
                        source_rows,
                        checkpoint_rows,
                        hashlib.sha256(source_objects[table].encode("utf-8")).hexdigest(),
                        digest,
                    )
                )
                copied[f"{component}.{table}"] = checkpoint_rows
            conn.commit()
            conn.execute("DETACH DATABASE source")
        conn.executemany(
            "INSERT INTO checkpoint_inventory VALUES (?,?,?,?,?,?,?)",
            inventory_rows,
        )
        generated = dt.datetime.now(dt.timezone.utc).isoformat()
        fingerprint_payload = {
            f"{row[0]}.{row[1]}": {
                "rows": row[3],
                "schema": row[5],
                "source": row[6],
            }
            for row in inventory_rows
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        conn.executemany(
            "INSERT INTO checkpoint_meta VALUES (?, ?)",
            (
                ("artifact", "tcia_metadata_v2_legacy_detail_checkpoint"),
                ("schema_version", str(CHECKPOINT_SCHEMA_VERSION)),
                ("generated_at_utc", generated),
                ("checkpoint_fingerprint", fingerprint),
                (
                    "storage_contract",
                    "Exact source rows for specialized legacy detail; embedded in public_non_dicom_audit.",
                ),
            ),
        )
        conn.commit()
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    return {
        "path": str(out),
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_fingerprint": fingerprint,
        "tables": len(copied),
        "rows": sum(copied.values()),
        "counts": copied,
        "sqlite_bytes": out.stat().st_size,
        "sqlite_sha256": file_sha256(out),
        "integrity_check": integrity,
    }


def extract_checkpoint_from_audit(
    out: Path,
    *,
    audit_db: Path,
    replace: bool = False,
) -> dict[str, Any]:
    """Recover the immutable legacy-detail checkpoint from a V2 audit companion.

    This is the post-retirement path: routine builds no longer download or open
    the standalone NIfTI/pathology databases.  The last parity-validated exact
    source rows already live in the published audit companion and are copied
    byte-for-byte at the SQLite value level into a runner-local checkpoint.
    """
    if not audit_db.is_file():
        raise FileNotFoundError(f"Audit companion is missing: {audit_db}")
    if out.exists():
        if not replace:
            raise FileExistsError(f"Checkpoint exists: {out}; pass --replace")
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    required_tables = {
        "checkpoint_meta",
        "checkpoint_inventory",
        *(
            f"source_{component}__{table}"
            for component, tables in CHECKPOINT_TABLES.items()
            for table in tables
        ),
    }
    with closing(sqlite3.connect(out)) as conn:
        conn.execute("ATTACH DATABASE ? AS audit", (str(audit_db),))
        source_objects = {
            str(name): str(sql or "")
            for name, sql in conn.execute(
                "SELECT name, sql FROM audit.sqlite_master WHERE type='table'"
            )
        }
        missing = sorted(required_tables - set(source_objects))
        if missing:
            raise RuntimeError(
                "Audit companion is missing checkpoint tables: " + ", ".join(missing)
            )
        for table in sorted(required_tables):
            conn.execute(
                f"CREATE TABLE {quote_identifier(table)} AS "
                f"SELECT * FROM audit.{quote_identifier(table)}"
            )
        conn.commit()
        conn.execute("DETACH DATABASE audit")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        rows = int(
            conn.execute(
                "SELECT COALESCE(SUM(checkpoint_rows), 0) FROM checkpoint_inventory"
            ).fetchone()[0]
        )
        tables = int(conn.execute("SELECT COUNT(*) FROM checkpoint_inventory").fetchone()[0])
        fingerprint_row = conn.execute(
            "SELECT value FROM checkpoint_meta WHERE key='checkpoint_fingerprint'"
        ).fetchone()
    validation = validate_checkpoint(out)
    if not validation["ok"]:
        raise RuntimeError("Extracted checkpoint is invalid: " + "; ".join(validation["errors"]))
    return {
        "path": str(out),
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_fingerprint": str(fingerprint_row[0]) if fingerprint_row else "",
        "tables": tables,
        "rows": rows,
        "sqlite_bytes": out.stat().st_size,
        "sqlite_sha256": file_sha256(out),
        "integrity_check": integrity,
        "source_audit_sha256": file_sha256(audit_db),
    }


def validate_checkpoint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "ok": False,
            "errors": [f"missing checkpoint database: {path}"],
            "integrity_check": "unavailable",
            "counts": {"tables": 0, "rows": 0},
        }
    errors: list[str] = []
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            errors.append(f"integrity_check={integrity}")
        objects = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
        required = {"checkpoint_meta", "checkpoint_inventory"}
        required.update(
            f"source_{component}__{table}"
            for component, tables in CHECKPOINT_TABLES.items()
            for table in tables
        )
        missing = sorted(required - objects)
        if missing:
            errors.append("missing checkpoint objects: " + ", ".join(missing))
        mismatches: list[str] = []
        if "checkpoint_inventory" in objects:
            for component, source_table, checkpoint_table, source_rows in conn.execute(
                "SELECT component, source_table, checkpoint_table, source_rows "
                "FROM checkpoint_inventory"
            ):
                actual = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {quote_identifier(str(checkpoint_table))}"
                    ).fetchone()[0]
                )
                if actual != int(source_rows):
                    mismatches.append(f"{component}.{source_table}={actual}/{source_rows}")
        if mismatches:
            errors.append("checkpoint row mismatches: " + ", ".join(mismatches))
        counts = {
            "tables": int(
                conn.execute("SELECT COUNT(*) FROM checkpoint_inventory").fetchone()[0]
            )
            if "checkpoint_inventory" in objects
            else 0,
            "rows": int(
                conn.execute("SELECT COALESCE(SUM(checkpoint_rows), 0) FROM checkpoint_inventory").fetchone()[0]
            )
            if "checkpoint_inventory" in objects
            else 0,
        }
    return {
        "ok": not errors,
        "errors": errors,
        "integrity_check": integrity,
        "counts": counts,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--staging-db", required=True)
    build.add_argument("--out", required=True)
    build.add_argument("--replace", action="store_true")
    extract = sub.add_parser("extract-from-audit")
    extract.add_argument("--audit-db", required=True)
    extract.add_argument("--out", required=True)
    extract.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--db", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "build":
        result = build_checkpoint(
            Path(args.out),
            staging_db=Path(args.staging_db),
            replace=args.replace,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "extract-from-audit":
        result = extract_checkpoint_from_audit(
            Path(args.out),
            audit_db=Path(args.audit_db),
            replace=args.replace,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    result = validate_checkpoint(Path(args.db))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
