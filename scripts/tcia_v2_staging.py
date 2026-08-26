#!/usr/bin/env python3
"""Build and validate the runner-local source ledger for TCIA Metadata V2."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


STAGING_SCHEMA_VERSION = 3
COMPONENT_ORDER = (
    "snapshot",
    "public_non_dicom_baseline",
    "public_non_dicom_audit_baseline",
    "controlled_access",
    "clinical",
    "idc_participants",
)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE staging_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE staging_sources (
    component TEXT PRIMARY KEY,
    database_file TEXT NOT NULL,
    database_bytes INTEGER NOT NULL,
    sqlite_sha256 TEXT NOT NULL,
    manifest_file TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    component_schema_version TEXT,
    component_release_fingerprint TEXT,
    source_release_tag TEXT,
    source_release_asset_id INTEGER,
    source_release_gzip_sha256 TEXT,
    integrity_check TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE staging_object_inventory (
    component TEXT NOT NULL,
    object_name TEXT NOT NULL,
    object_type TEXT NOT NULL,
    sql_sha256 TEXT NOT NULL,
    row_count INTEGER,
    PRIMARY KEY (component, object_name),
    FOREIGN KEY (component) REFERENCES staging_sources(component)
);

CREATE VIEW agent_staging_sources AS
SELECT component, database_file, database_bytes, sqlite_sha256, manifest_file,
       component_schema_version, component_release_fingerprint,
       source_release_tag, integrity_check, required
FROM staging_sources;
"""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return payload


def source_release_details(path: Path | None) -> tuple[str, dict[str, dict[str, Any]]]:
    if path is None:
        return "", {}
    payload = load_json(path)
    assets = {
        str(asset.get("name")): asset
        for asset in payload.get("assets") or []
        if asset.get("name")
    }
    return str(payload.get("tag_name") or ""), assets


def inventory_objects(conn: sqlite3.Connection) -> list[tuple[str, str, str, int | None]]:
    rows: list[tuple[str, str, str, int | None]] = []
    objects = conn.execute(
        "SELECT name, type, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table', 'view') ORDER BY name"
    ).fetchall()
    for name, object_type, sql in objects:
        row_count: int | None = None
        if object_type == "table":
            row_count = int(
                conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(str(name))}").fetchone()[0]
            )
        rows.append(
            (
                str(name),
                str(object_type),
                hashlib.sha256(str(sql).encode("utf-8")).hexdigest(),
                row_count,
            )
        )
    return rows


def build_staging_database(
    out: Path,
    *,
    components: dict[str, tuple[Path, Path]],
    source_release_json: Path | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    missing_components = sorted(set(COMPONENT_ORDER) - set(components))
    if missing_components:
        raise RuntimeError("Missing staging components: " + ", ".join(missing_components))
    if out.exists():
        if not replace:
            raise FileExistsError(f"Staging database exists: {out}; pass --replace")
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    release_tag, release_assets = source_release_details(source_release_json)
    source_rows: list[tuple[Any, ...]] = []
    object_rows: list[tuple[Any, ...]] = []
    ordered_components = COMPONENT_ORDER + tuple(
        sorted(set(components) - set(COMPONENT_ORDER))
    )
    for component in ordered_components:
        database, manifest_path = components[component]
        if not database.is_file():
            raise FileNotFoundError(f"Missing {component} database: {database}")
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Missing {component} manifest: {manifest_path}")
        manifest = load_json(manifest_path)
        sqlite_digest = file_sha256(database)
        expected_sqlite_digest = str(manifest.get("sqlite_sha256") or "")
        if not expected_sqlite_digest:
            raise RuntimeError(f"{manifest_path.name} has no sqlite_sha256")
        if sqlite_digest != expected_sqlite_digest:
            raise RuntimeError(f"{database.name} does not match {manifest_path.name} sqlite_sha256")
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as source:
            integrity = str(source.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"{component} integrity_check={integrity}")
            for object_name, object_type, sql_digest, row_count in inventory_objects(source):
                object_rows.append(
                    (component, object_name, object_type, sql_digest, row_count)
                )
        release_asset = release_assets.get(database.name + ".gz") or {}
        release_digest = str(release_asset.get("digest") or "").removeprefix("sha256:")
        source_rows.append(
            (
                component,
                database.name,
                database.stat().st_size,
                sqlite_digest,
                manifest_path.name,
                file_sha256(manifest_path),
                str(manifest.get("schema_version") or ""),
                str(manifest.get("release_fingerprint") or ""),
                release_tag,
                release_asset.get("id"),
                release_digest,
                integrity,
                1,
            )
        )
    generated = dt.datetime.now(dt.timezone.utc).isoformat()
    fingerprint_payload = {
        row[0]: {"sqlite_sha256": row[3], "manifest_sha256": row[5]}
        for row in source_rows
    }
    fingerprint = hashlib.sha256(canonical_json(fingerprint_payload).encode("utf-8")).hexdigest()
    with closing(sqlite3.connect(out)) as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO staging_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            source_rows,
        )
        conn.executemany(
            "INSERT INTO staging_object_inventory VALUES (?,?,?,?,?)",
            object_rows,
        )
        conn.executemany(
            "INSERT INTO staging_meta VALUES (?, ?)",
            (
                ("artifact", "tcia_metadata_v2_staging"),
                ("schema_version", str(STAGING_SCHEMA_VERSION)),
                ("generated_at_utc", generated),
                ("source_release_tag", release_tag),
                ("source_fingerprint", fingerprint),
                (
                    "storage_contract",
                    "runner_local_ledger_with_path_independent_audit_checkpoint",
                ),
            ),
        )
        conn.commit()
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    return {
        "path": str(out),
        "schema_version": STAGING_SCHEMA_VERSION,
        "source_fingerprint": fingerprint,
        "components": len(source_rows),
        "objects": len(object_rows),
        "integrity_check": integrity,
    }


def validate_staging_database(path: Path, *, verify_sources: bool = False) -> dict[str, Any]:
    if not path.is_file():
        return {
            "ok": False,
            "errors": [f"missing staging database: {path}"],
            "integrity_check": "unavailable",
            "counts": {"components": 0, "objects": 0},
        }
    errors: list[str] = []
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            errors.append(f"integrity_check={integrity}")
        objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        for required in (
            "staging_meta",
            "staging_sources",
            "staging_object_inventory",
            "agent_staging_sources",
        ):
            if required not in objects:
                errors.append(f"missing staging object: {required}")
        meta = dict(conn.execute("SELECT key, value FROM staging_meta")) if "staging_meta" in objects else {}
        if meta.get("schema_version") != str(STAGING_SCHEMA_VERSION):
            errors.append("unexpected staging schema version")
        rows = list(conn.execute("SELECT * FROM staging_sources")) if "staging_sources" in objects else []
        names = {str(row["component"]) for row in rows}
        missing = sorted(set(COMPONENT_ORDER) - names)
        if missing:
            errors.append("missing components: " + ", ".join(missing))
        if verify_sources:
            for row in rows:
                source = path.parent / str(row["database_file"])
                if not source.is_file():
                    errors.append(f"missing staged source: {row['component']}={source}")
                elif file_sha256(source) != row["sqlite_sha256"]:
                    errors.append(f"staged source hash mismatch: {row['component']}")
        orphan_objects = 0
        if "staging_object_inventory" in objects and "staging_sources" in objects:
            orphan_objects = int(
                conn.execute(
                    "SELECT COUNT(*) FROM staging_object_inventory o "
                    "LEFT JOIN staging_sources s USING(component) WHERE s.component IS NULL"
                ).fetchone()[0]
            )
            if orphan_objects:
                errors.append(f"orphan staging objects: {orphan_objects}")
        counts = {
            "components": len(rows),
            "objects": int(conn.execute("SELECT COUNT(*) FROM staging_object_inventory").fetchone()[0])
            if "staging_object_inventory" in objects
            else 0,
        }
    return {
        "ok": not errors,
        "errors": errors,
        "integrity_check": integrity,
        "counts": counts,
    }


def resolve_component(path: Path, component: str, *, verify_hash: bool = True) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"Staging ledger is missing: {path}")
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        row = conn.execute(
            "SELECT database_file, sqlite_sha256 FROM staging_sources WHERE component=?",
            (component,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"Staging ledger has no component: {component}")
    database = path.parent / str(row[0])
    if not database.is_file():
        raise FileNotFoundError(f"Staged {component} database is missing: {database}")
    if verify_hash and file_sha256(database) != row[1]:
        raise RuntimeError(f"Staged {component} database hash mismatch: {database}")
    return database


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    for component in COMPONENT_ORDER:
        build.add_argument(f"--{component.replace('_', '-')}-db", required=True)
        build.add_argument(f"--{component.replace('_', '-')}-manifest", required=True)
    build.add_argument("--source-release-json")
    build.add_argument("--out", required=True)
    build.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--db", required=True)
    validate.add_argument("--verify-sources", action="store_true")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--db", required=True)
    resolve.add_argument("--component", choices=COMPONENT_ORDER, required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "build":
        components = {
            component: (
                Path(getattr(args, f"{component}_db")),
                Path(getattr(args, f"{component}_manifest")),
            )
            for component in COMPONENT_ORDER
        }
        result = build_staging_database(
            Path(args.out),
            components=components,
            source_release_json=Path(args.source_release_json) if args.source_release_json else None,
            replace=args.replace,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "validate":
        result = validate_staging_database(Path(args.db), verify_sources=args.verify_sources)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    print(resolve_component(Path(args.db), args.component))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
