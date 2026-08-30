#!/usr/bin/env python3
"""Build, validate, verify, and scope-filter canonical non-DICOM geometry seeds."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


SEED_SCHEMA_VERSION = 1
NON_DICOM_FORMATS = {"NIFTI", "MHA", "MHD", "NRRD"}
ASSESSMENT_COLUMNS = (
    "assessment_id", "schema_version", "analyzer", "analyzer_version",
    "assessed_at_utc", "job_id", "dataset_type", "short_title",
    "download_id", "asset_id", "local_relative_path", "file_format",
    "assessment_scope", "series_instance_uid", "study_instance_uid",
    "geometry_status", "dimension", "shape_json", "spacing_json",
    "origin_json", "direction_json", "checks_json", "details_json", "error",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def format_tokens(value: object) -> list[str]:
    return sorted(
        token for token in {
            item.strip().upper()
            for item in str(value or "").replace(",", ";").split(";")
            if item.strip()
        }
        if token in NON_DICOM_FORMATS
    )


def scope_key(row: dict[str, Any] | sqlite3.Row) -> tuple[str, str, str]:
    return tuple(str(row[name] or "") for name in ("dataset_type", "short_title", "download_id"))


def job_fingerprint(row: dict[str, Any] | sqlite3.Row) -> str:
    payload = {
        "dataset_type": str(row["dataset_type"] or ""),
        "short_title": str(row["short_title"] or ""),
        "download_id": str(row["download_id"] or ""),
        "route_type": str(row["route_type"] or ""),
        "formats": format_tokens(row["formats"]),
        "asset_rows": int(row["asset_rows"] or 0),
        "catalog_represented_file_count": int(row["catalog_represented_file_count"] or 0),
        "catalog_size_bytes": int(row["catalog_size_bytes"] or 0),
    }
    return hashlib.sha256(json_dump(payload).encode()).hexdigest()


def read_jobs(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    required = {
        "job_id", "dataset_type", "short_title", "download_id", "route_type",
        "formats", "asset_rows", "catalog_represented_file_count", "catalog_size_bytes",
    }
    if not rows or not required.issubset(rows[0]):
        raise RuntimeError(f"Geometry jobs CSV lacks required columns: {path}")
    return [row for row in rows if format_tokens(row["formats"])]


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE geometry_assessments (
            assessment_id INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL,
            analyzer TEXT NOT NULL, analyzer_version TEXT NOT NULL,
            assessed_at_utc TEXT NOT NULL, job_id TEXT NOT NULL,
            dataset_type TEXT NOT NULL, short_title TEXT NOT NULL,
            download_id TEXT NOT NULL, asset_id TEXT,
            local_relative_path TEXT NOT NULL, file_format TEXT NOT NULL,
            assessment_scope TEXT NOT NULL, series_instance_uid TEXT,
            study_instance_uid TEXT, geometry_status TEXT NOT NULL,
            dimension INTEGER, shape_json TEXT NOT NULL, spacing_json TEXT NOT NULL,
            origin_json TEXT NOT NULL, direction_json TEXT NOT NULL,
            checks_json TEXT NOT NULL, details_json TEXT NOT NULL, error TEXT
        );
        CREATE INDEX idx_geometry_asset ON geometry_assessments(asset_id);
        CREATE INDEX idx_geometry_series ON geometry_assessments(series_instance_uid);
        CREATE INDEX idx_geometry_dataset ON geometry_assessments(short_title, download_id);
        CREATE TABLE geometry_jobs (
            job_id TEXT PRIMARY KEY, dataset_type TEXT NOT NULL,
            short_title TEXT NOT NULL, download_id TEXT NOT NULL,
            route_type TEXT NOT NULL, formats_json TEXT NOT NULL,
            asset_rows INTEGER NOT NULL, catalog_represented_file_count INTEGER NOT NULL,
            catalog_size_bytes INTEGER NOT NULL, scope_fingerprint TEXT NOT NULL,
            assessment_rows INTEGER NOT NULL, seed_status TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_geometry_job_scope
        ON geometry_jobs(dataset_type, short_title, download_id);
        CREATE TABLE geometry_seed_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE VIEW agent_geometry_assessments AS SELECT * FROM geometry_assessments;
        PRAGMA user_version=1;
        """
    )


def sanitize_error(error: object, local_relative_path: str) -> str | None:
    value = str(error or "")
    if not value:
        return None
    marker = "Empty file:"
    if marker in value:
        return f"{value.split(marker, 1)[0]}{marker} {local_relative_path!r}"
    return value


def copy_assessments(
    source: sqlite3.Connection, destination: sqlite3.Connection,
    allowed_jobs: set[str] | None = None,
) -> int:
    source.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in ASSESSMENT_COLUMNS)
    sql = f"INSERT INTO geometry_assessments ({','.join(ASSESSMENT_COLUMNS)}) VALUES ({placeholders})"
    count = 0
    for row in source.execute("SELECT * FROM geometry_assessments ORDER BY assessment_id"):
        if str(row["file_format"] or "").upper() not in NON_DICOM_FORMATS:
            continue
        if allowed_jobs is not None and str(row["job_id"]) not in allowed_jobs:
            continue
        values = [row[name] for name in ASSESSMENT_COLUMNS]
        values[-1] = sanitize_error(row["error"], str(row["local_relative_path"] or ""))
        destination.execute(sql, values)
        count += 1
    return count


def populate_jobs(
    conn: sqlite3.Connection, jobs: Iterable[dict[str, str]],
    allowed_jobs: set[str] | None = None,
) -> int:
    inserted = 0
    for row in jobs:
        job_id = str(row["job_id"])
        if allowed_jobs is not None and job_id not in allowed_jobs:
            continue
        count = int(conn.execute(
            "SELECT COUNT(*) FROM geometry_assessments WHERE job_id=?", (job_id,)
        ).fetchone()[0])
        conn.execute(
            "INSERT INTO geometry_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, row["dataset_type"], row["short_title"], row["download_id"],
                row["route_type"], json_dump(format_tokens(row["formats"])),
                int(row["asset_rows"] or 0), int(row["catalog_represented_file_count"] or 0),
                int(row["catalog_size_bytes"] or 0), job_fingerprint(row), count,
                "assessed" if count else "no_assessments",
            ),
        )
        inserted += 1
    return inserted


def seed_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "assessment_rows": int(conn.execute("SELECT COUNT(*) FROM geometry_assessments").fetchone()[0]),
        "jobs": int(conn.execute("SELECT COUNT(*) FROM geometry_jobs").fetchone()[0]),
        "datasets": int(conn.execute("SELECT COUNT(DISTINCT short_title) FROM geometry_jobs").fetchone()[0]),
        "file_format_counts": dict(conn.execute(
            "SELECT file_format,COUNT(*) FROM geometry_assessments GROUP BY file_format ORDER BY file_format"
        )),
        "geometry_status_counts": dict(conn.execute(
            "SELECT geometry_status,COUNT(*) FROM geometry_assessments GROUP BY geometry_status ORDER BY geometry_status"
        )),
    }


def validate_seed(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            errors.append(f"integrity_check={integrity}")
        objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        missing = {"geometry_assessments", "geometry_jobs", "geometry_seed_meta"} - objects
        if missing:
            errors.append("missing objects: " + ", ".join(sorted(missing)))
            return {"ok": False, "errors": errors, "integrity": integrity}
        dicom = int(conn.execute(
            "SELECT COUNT(*) FROM geometry_assessments WHERE upper(file_format)='DICOM'"
        ).fetchone()[0])
        if dicom:
            errors.append(f"DICOM assessments are forbidden: {dicom}")
        absolute_errors = int(conn.execute(
            "SELECT COUNT(*) FROM geometry_assessments WHERE error LIKE '%/scratch/%' OR error LIKE '%/Users/%'"
        ).fetchone()[0])
        if absolute_errors:
            errors.append(f"errors expose absolute paths: {absolute_errors}")
        orphan_jobs = int(conn.execute(
            "SELECT COUNT(*) FROM geometry_assessments a LEFT JOIN geometry_jobs j USING(job_id) WHERE j.job_id IS NULL"
        ).fetchone()[0])
        if orphan_jobs:
            errors.append(f"assessment rows without job ledger entries: {orphan_jobs}")
        bad_counts = int(conn.execute(
            """SELECT COUNT(*) FROM geometry_jobs j WHERE assessment_rows !=
               (SELECT COUNT(*) FROM geometry_assessments a WHERE a.job_id=j.job_id)"""
        ).fetchone()[0])
        if bad_counts:
            errors.append(f"job assessment count mismatches: {bad_counts}")
        for table, columns in (("geometry_assessments", ("shape_json", "spacing_json", "origin_json", "direction_json", "checks_json", "details_json")), ("geometry_jobs", ("formats_json",))):
            for column in columns:
                invalid = int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE NOT json_valid({column})").fetchone()[0])
                if invalid:
                    errors.append(f"invalid JSON in {table}.{column}: {invalid}")
        counts = seed_counts(conn)
    return {"ok": not errors, "errors": errors, "integrity": integrity, "counts": counts}


def write_gzip(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as inp, destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as out:
            shutil.copyfileobj(inp, out)


def build_seed(
    source_db: Path, jobs_csv: Path, coverage_paths: list[Path], out: Path,
    gzip_out: Path | None, manifest_out: Path | None, release_tag: str, replace: bool,
) -> dict[str, Any]:
    if out.exists() and not replace:
        raise FileExistsError(f"Output exists: {out}; pass --replace")
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_suffix(out.suffix + ".part")
    if temp.exists():
        temp.unlink()
    jobs = read_jobs(jobs_csv)
    with closing(sqlite3.connect(source_db)) as source, closing(sqlite3.connect(temp)) as conn:
        create_schema(conn)
        rows = copy_assessments(source, conn)
        job_count = populate_jobs(conn, jobs)
        meta = {
            "seed_schema_version": str(SEED_SCHEMA_VERSION),
            "created_at_utc": utc_now(),
            "source_geometry_sha256": sha256_file(source_db),
            "source_jobs_sha256": sha256_file(jobs_csv),
            "scope": "public_non_dicom",
            "dicom_policy": "excluded_use_idc_index",
        }
        conn.executemany("INSERT INTO geometry_seed_meta VALUES (?,?)", meta.items())
        conn.commit()
        conn.execute("VACUUM")
    os.replace(temp, out)
    validation = validate_seed(out)
    if not validation["ok"]:
        raise RuntimeError("Invalid geometry seed: " + "; ".join(validation["errors"]))
    if gzip_out:
        write_gzip(out, gzip_out)
    coverage = [
        {"file": path.name, "sha256": sha256_file(path), "bytes": path.stat().st_size,
         "job_id": json.loads(path.read_text())["job_id"]}
        for path in coverage_paths
    ]
    manifest = {
        "schema_version": 1, "component": "public_non_dicom_geometry_seed",
        "release_tag": release_tag, "asset_name": gzip_out.name if gzip_out else None,
        "created_at_utc": utc_now(), "sqlite_bytes": out.stat().st_size,
        "sqlite_sha256": sha256_file(out),
        "gzip_bytes": gzip_out.stat().st_size if gzip_out else None,
        "gzip_sha256": sha256_file(gzip_out) if gzip_out else None,
        "source_jobs_sha256": sha256_file(jobs_csv), "coverage": coverage,
        **validation["counts"],
    }
    if manifest_out:
        manifest_out.parent.mkdir(parents=True, exist_ok=True)
        manifest_out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {"output_db": str(out), "job_rows": job_count, "manifest": manifest, "validation": validation}


def verify_release(manifest_path: Path, gzip_path: Path, out: Path, coverage_dir: Path | None) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text())
    if gzip_path.name != manifest["asset_name"]:
        raise RuntimeError("Geometry release asset name does not match manifest")
    if gzip_path.stat().st_size != manifest["gzip_bytes"] or sha256_file(gzip_path) != manifest["gzip_sha256"]:
        raise RuntimeError("Geometry release gzip size or SHA-256 mismatch")
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(gzip_path, "rb") as inp, tempfile.NamedTemporaryFile(dir=out.parent, delete=False) as handle:
        shutil.copyfileobj(inp, handle)
        temporary = Path(handle.name)
    if temporary.stat().st_size != manifest["sqlite_bytes"] or sha256_file(temporary) != manifest["sqlite_sha256"]:
        temporary.unlink()
        raise RuntimeError("Geometry release SQLite size or SHA-256 mismatch")
    os.replace(temporary, out)
    if coverage_dir:
        for item in manifest.get("coverage", []):
            path = coverage_dir / item["file"]
            if not path.is_file() or path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
                raise RuntimeError(f"Geometry coverage sidecar mismatch: {path}")
    validation = validate_seed(out)
    if not validation["ok"]:
        raise RuntimeError("Invalid released geometry seed: " + "; ".join(validation["errors"]))
    return {"output_db": str(out), "manifest": str(manifest_path), "validation": validation}


def compare_scope(
    seed_db: Path, jobs_csv: Path, out: Path, report_json: Path,
    report_csv: Path | None, coverage_dir: Path | None,
    filtered_coverage_dir: Path | None,
) -> dict[str, Any]:
    current_rows = read_jobs(jobs_csv)
    current = {scope_key(row): row for row in current_rows}
    with closing(sqlite3.connect(seed_db)) as source:
        source.row_factory = sqlite3.Row
        seed_rows = [dict(row) for row in source.execute("SELECT * FROM geometry_jobs")]
        seeded = {scope_key(row): row for row in seed_rows}
        records: list[dict[str, Any]] = []
        unchanged: set[str] = set()
        for key in sorted(set(current) | set(seeded)):
            current_row, seed_row = current.get(key), seeded.get(key)
            if current_row is None:
                status = "removed"
            elif seed_row is None:
                status = "new"
            elif current_row["job_id"] == seed_row["job_id"] and job_fingerprint(current_row) == seed_row["scope_fingerprint"]:
                status = "unchanged"
                unchanged.add(str(seed_row["job_id"]))
            else:
                status = "changed"
            records.append({
                "status": status, "dataset_type": key[0], "short_title": key[1],
                "download_id": key[2],
                "seed_job_id": seed_row["job_id"] if seed_row else "",
                "current_job_id": current_row["job_id"] if current_row else "",
            })
        temp = out.with_suffix(out.suffix + ".part")
        if temp.exists():
            temp.unlink()
        with closing(sqlite3.connect(temp)) as destination:
            create_schema(destination)
            copy_assessments(source, destination, unchanged)
            populate_jobs(destination, current_rows, unchanged)
            destination.executemany("INSERT INTO geometry_seed_meta VALUES (?,?)", (
                ("seed_schema_version", str(SEED_SCHEMA_VERSION)),
                ("filtered_at_utc", utc_now()),
                ("source_seed_sha256", sha256_file(seed_db)),
                ("scope", "public_non_dicom_current_unchanged_jobs"),
            ))
            destination.commit()
            destination.execute("VACUUM")
        os.replace(temp, out)
    if filtered_coverage_dir:
        filtered_coverage_dir.mkdir(parents=True, exist_ok=True)
        for old in filtered_coverage_dir.glob("*.json"):
            old.unlink()
        if coverage_dir:
            for path in coverage_dir.glob("*.json"):
                payload = json.loads(path.read_text())
                if str(payload.get("job_id")) in unchanged:
                    shutil.copy2(path, filtered_coverage_dir / path.name)
    counts = dict(Counter(row["status"] for row in records))
    report = {
        "created_at_utc": utc_now(), "seed_db_sha256": sha256_file(seed_db),
        "current_jobs_sha256": sha256_file(jobs_csv), "status_counts": counts,
        "refresh_required": bool(counts.get("new") or counts.get("changed")),
        "records": records, "filtered_seed": str(out),
    }
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if report_csv:
        report_csv.parent.mkdir(parents=True, exist_ok=True)
        with report_csv.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]) if records else ["status"])
            writer.writeheader(); writer.writerows(records)
    if report["refresh_required"] and os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=Non-DICOM geometry refresh required::{counts.get('new', 0)} new and {counts.get('changed', 0)} changed download scopes require HPC analysis")
    return report


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--source-db", type=Path, required=True)
    build.add_argument("--jobs-csv", type=Path, required=True)
    build.add_argument("--coverage", type=Path, action="append", default=[])
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--gzip-out", type=Path)
    build.add_argument("--manifest-out", type=Path)
    build.add_argument("--release-tag", required=True)
    build.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--db", type=Path, required=True)
    verify = sub.add_parser("verify-release")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--gzip", type=Path, required=True)
    verify.add_argument("--out", type=Path, required=True)
    verify.add_argument("--coverage-dir", type=Path)
    compare = sub.add_parser("compare")
    compare.add_argument("--seed-db", type=Path, required=True)
    compare.add_argument("--jobs-csv", type=Path, required=True)
    compare.add_argument("--out", type=Path, required=True)
    compare.add_argument("--report-json", type=Path, required=True)
    compare.add_argument("--report-csv", type=Path)
    compare.add_argument("--coverage-dir", type=Path)
    compare.add_argument("--filtered-coverage-dir", type=Path)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "build":
        result = build_seed(args.source_db, args.jobs_csv, args.coverage, args.out, args.gzip_out, args.manifest_out, args.release_tag, args.replace)
    elif args.command == "validate":
        result = validate_seed(args.db)
    elif args.command == "verify-release":
        result = verify_release(args.manifest, args.gzip, args.out, args.coverage_dir)
    else:
        result = compare_scope(args.seed_db, args.jobs_csv, args.out, args.report_json, args.report_csv, args.coverage_dir, args.filtered_coverage_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok", result.get("validation", {"ok": True}).get("ok", True)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
