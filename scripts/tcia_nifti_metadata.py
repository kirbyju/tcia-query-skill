#!/usr/bin/env python3
"""Download and query the optional TCIA NIfTI metadata SQLite database."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = 1
DEFAULT_REPO = "kirbyju/tcia-query-skill"
DEFAULT_RELEASE_TAG = "tcia-snapshot-latest"
NIFTI_ASSET = "nifti_metadata.sqlite.gz"
NIFTI_MANIFEST_ASSET = "nifti_metadata_manifest.json"
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = SKILL_ROOT / "cache" / "nifti_metadata.sqlite"
DEFAULT_MANIFEST_PATH = SKILL_ROOT / "cache" / NIFTI_MANIFEST_ASSET
USER_AGENT = "tcia-nifti-metadata/1.0"

REQUIRED_TABLES = [
    "nifti_downloads",
    "candidate_downloads",
    "package_files",
    "harvested_files",
    "tabular_sheets",
    "tabular_rows",
    "normalized_series_rows",
    "aspera_root_sums_inventory",
    "metadata_quality_flags",
    "nifti_file_series",
    "non_dicom_files",
    "radiology_series",
    "radiology_mr",
    "radiology_ct",
    "radiology_pet",
    "radiology_contrast",
    "derived_objects",
    "derived_object_references",
    "annotation_groups",
]

NIFTI_DOWNLOAD_SIGNATURE_COLUMNS = [
    "parent_source",
    "dataset_type",
    "short_title",
    "title",
    "download_id",
    "download_title",
    "download_url",
    "download_size",
    "download_size_unit",
    "subjects",
    "studies",
    "series",
    "images",
    "download_types",
    "data_types",
    "file_types",
    "license_label",
    "access_level",
]


def db_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    env_path = os.environ.get("TCIA_NIFTI_METADATA_DB")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


def manifest_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    env_path = os.environ.get("TCIA_NIFTI_METADATA_MANIFEST")
    if env_path:
        return Path(env_path)
    return DEFAULT_MANIFEST_PATH


def connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    resolved = db_path(path)
    if not resolved.exists():
        raise RuntimeError(
            f"NIfTI metadata SQLite not found at {resolved}. "
            "Run `python scripts/tcia_nifti_metadata.py ensure` first."
        )
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    return conn


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fetch_bytes(
    url: str, timeout: int = 120, headers: dict[str, str] | None = None
) -> tuple[bytes, dict[str, str]]:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(), dict(response.headers.items())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def github_api_json(url: str) -> Any:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body, _headers = fetch_bytes(url, timeout=60, headers=headers)
    return json.loads(body.decode("utf-8"))


def release_assets(repo: str, tag: str) -> dict[str, Any]:
    release = github_api_json(
        f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    )
    return {asset["name"]: asset for asset in release.get("assets") or []}


def rows_as_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params)]


def get_harvest_meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM harvest_meta")}
    except sqlite3.Error:
        return {}


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in REQUIRED_TABLES:
        try:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:
            counts[table] = -1
    return counts


def nifti_download_rows_from_nifti_db(conn: sqlite3.Connection) -> list[dict[str, str]]:
    columns = ", ".join(NIFTI_DOWNLOAD_SIGNATURE_COLUMNS)
    return rows_as_dicts(
        conn,
        f"""
        SELECT {columns}
        FROM nifti_downloads
        ORDER BY lower(short_title), download_id, download_title, download_url
        """,
    )


def nifti_download_rows_from_snapshot(conn: sqlite3.Connection) -> list[dict[str, str]]:
    columns = ", ".join(f"d.{column}" for column in NIFTI_DOWNLOAD_SIGNATURE_COLUMNS)
    return rows_as_dicts(
        conn,
        f"""
        SELECT DISTINCT {columns}
        FROM agent_current_downloads d
        JOIN wordpress_download_labels l
          ON l.download_row_id = d.download_row_id
        WHERE d.hidden = 0
          AND d.controlled_access = 0
          AND l.label_kind = 'file_type'
          AND lower(l.label) = 'nifti'
        ORDER BY lower(d.short_title), d.download_id, d.download_title, d.download_url
        """,
    )


def download_signature(rows: list[dict[str, Any]]) -> str:
    normalized = [
        {key: str(row.get(key) or "") for key in NIFTI_DOWNLOAD_SIGNATURE_COLUMNS}
        for row in rows
    ]
    return hashlib.sha256(json_dumps(normalized).encode("utf-8")).hexdigest()


def release_fingerprint(manifest: dict[str, Any]) -> str:
    payload = {
        "schema_version": manifest.get("schema_version"),
        "sqlite_sha256": manifest.get("sqlite_sha256"),
        "gzip_sha256": manifest.get("gzip_sha256"),
        "nifti_download_signature": manifest.get("nifti_download_signature"),
        "table_counts": manifest.get("table_counts"),
    }
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def build_manifest(
    sqlite_path: Path,
    gzip_path: Path | None = None,
    snapshot_db: Path | None = None,
) -> dict[str, Any]:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    meta = get_harvest_meta(conn)
    counts = table_counts(conn)
    nifti_rows = nifti_download_rows_from_nifti_db(conn)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()

    manifest: dict[str, Any] = {
        "asset": NIFTI_ASSET,
        "schema_version": SCHEMA_VERSION,
        "sqlite_sha256": file_sha256(sqlite_path),
        "sqlite_bytes": sqlite_path.stat().st_size,
        "table_counts": counts,
        "harvest_meta": meta,
        "nifti_download_count": len(nifti_rows),
        "nifti_download_signature": download_signature(nifti_rows),
        "sqlite_integrity_check": integrity,
    }
    if gzip_path and gzip_path.exists():
        manifest["gzip_sha256"] = file_sha256(gzip_path)
        manifest["gzip_bytes"] = gzip_path.stat().st_size
    if snapshot_db and snapshot_db.exists():
        snapshot_conn = sqlite3.connect(snapshot_db)
        snapshot_conn.row_factory = sqlite3.Row
        snapshot_rows = nifti_download_rows_from_snapshot(snapshot_conn)
        snapshot_conn.close()
        manifest["source_snapshot_nifti_download_count"] = len(snapshot_rows)
        manifest["source_snapshot_nifti_download_signature"] = download_signature(snapshot_rows)
        manifest["source_snapshot_matches_harvest"] = (
            manifest["source_snapshot_nifti_download_signature"]
            == manifest["nifti_download_signature"]
        )
    manifest["release_fingerprint"] = release_fingerprint(manifest)
    return manifest


def local_manifest_current(local_db: Path, local_manifest: Path, remote_manifest: dict[str, Any]) -> bool:
    if not local_db.exists() or not local_manifest.exists():
        return False
    try:
        current_manifest = json.loads(local_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if current_manifest.get("release_fingerprint") != remote_manifest.get("release_fingerprint"):
        return False
    expected_sqlite = remote_manifest.get("sqlite_sha256")
    return not expected_sqlite or file_sha256(local_db) == expected_sqlite


def ensure_release_nifti(repo: str, tag: str, local_db: Path, local_manifest: Path) -> dict[str, Any]:
    assets = release_assets(repo, tag)
    missing = [name for name in (NIFTI_ASSET, NIFTI_MANIFEST_ASSET) if name not in assets]
    if missing:
        raise RuntimeError(f"Release {repo}@{tag} is missing NIfTI assets: {', '.join(missing)}")

    manifest_body, _headers = fetch_bytes(assets[NIFTI_MANIFEST_ASSET]["browser_download_url"])
    remote_manifest = json.loads(manifest_body.decode("utf-8"))
    if local_manifest_current(local_db, local_manifest, remote_manifest):
        local_manifest.parent.mkdir(parents=True, exist_ok=True)
        local_manifest.write_bytes(manifest_body)
        return {"status": "unchanged", "manifest": remote_manifest}

    compressed, _headers = fetch_bytes(assets[NIFTI_ASSET]["browser_download_url"], timeout=300)
    expected_gzip = remote_manifest.get("gzip_sha256")
    actual_gzip = hashlib.sha256(compressed).hexdigest()
    if expected_gzip and actual_gzip != expected_gzip:
        raise RuntimeError("Downloaded NIfTI metadata gzip SHA-256 does not match manifest.")

    local_db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=str(local_db.parent)) as tmp:
        tmp_path = Path(tmp.name)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as gz:
            shutil.copyfileobj(gz, tmp)
    expected_sqlite = remote_manifest.get("sqlite_sha256")
    actual_sqlite = file_sha256(tmp_path)
    if expected_sqlite and actual_sqlite != expected_sqlite:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded NIfTI metadata SQLite SHA-256 does not match manifest.")
    tmp_path.replace(local_db)
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    local_manifest.write_bytes(manifest_body)
    return {"status": "downloaded", "manifest": remote_manifest}


def validate_db(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    missing = [table for table in REQUIRED_TABLES if table not in tables]
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    counts = table_counts(conn)
    conn.close()
    return {"integrity_check": integrity, "missing_tables": missing, "table_counts": counts}


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("No rows.")
        return
    widths = {
        column: max(len(column), *(len(str(row.get(column, ""))) for row in rows))
        for column in columns
    }
    print(" | ".join(column.ljust(widths[column]) for column in columns))
    print(" | ".join("-" * widths[column] for column in columns))
    for row in rows:
        print(" | ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))


def command_info(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    counts = table_counts(conn)
    meta = get_harvest_meta(conn)
    conn.close()
    manifest: dict[str, Any] = {}
    candidate_manifest = manifest_path(args.manifest)
    if candidate_manifest.exists():
        manifest = json.loads(candidate_manifest.read_text(encoding="utf-8"))
    payload = {
        "db": str(db_path(args.db)),
        "manifest": str(candidate_manifest),
        "table_counts": counts,
        "harvest_meta": meta,
        "release_fingerprint": manifest.get("release_fingerprint", ""),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"DB: {payload['db']}")
        print(f"Manifest: {payload['manifest']}")
        if payload["release_fingerprint"]:
            print(f"Release fingerprint: {payload['release_fingerprint']}")
        for key in ("radiology_series", "derived_objects", "derived_object_references"):
            print(f"{key}: {counts.get(key, 0)}")
    return 0


def command_datasets(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    sql = """
        SELECT
          r.short_title,
          COUNT(*) AS nifti_files,
          SUM(CASE WHEN r.modality = 'MR' THEN 1 ELSE 0 END) AS mr_files,
          SUM(CASE WHEN r.modality = 'CT' THEN 1 ELSE 0 END) AS ct_files,
          SUM(CASE WHEN r.is_derived_object THEN 1 ELSE 0 END) AS derived_objects,
          COUNT(DISTINCT dor.derived_object_id) AS linked_derived_objects
        FROM radiology_series r
        LEFT JOIN derived_object_references dor
          ON dor.derived_radiology_id = r.radiology_id
        GROUP BY r.short_title
        ORDER BY lower(r.short_title)
        LIMIT ?
    """
    rows = rows_as_dicts(conn, sql, (args.limit,))
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(
            rows,
            [
                "short_title",
                "nifti_files",
                "mr_files",
                "ct_files",
                "derived_objects",
                "linked_derived_objects",
            ],
        )
    return 0


def command_files(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    where = ["1 = 1"]
    params: list[Any] = []
    if args.collection:
        where.append("r.short_title = ?")
        params.append(args.collection)
    if args.modality:
        where.append("r.modality = ?")
        params.append(args.modality)
    if args.derived:
        where.append("r.is_derived_object = 1")
    if args.source:
        where.append("r.is_derived_object = 0")
    params.append(args.limit)
    rows = rows_as_dicts(
        conn,
        f"""
        SELECT r.short_title, r.file_name, r.modality, r.subject_id,
               r.series_id, r.is_derived_object, r.package_path
        FROM radiology_series r
        WHERE {' AND '.join(where)}
        ORDER BY lower(r.short_title), r.file_name, r.package_path
        LIMIT ?
        """,
        tuple(params),
    )
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(rows, ["short_title", "file_name", "modality", "subject_id", "is_derived_object"])
    return 0


def command_derived(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    where = ["1 = 1"]
    params: list[Any] = []
    if args.collection:
        where.append("d.short_title = ?")
        params.append(args.collection)
    params.append(args.limit)
    if args.with_sources:
        sql = f"""
            SELECT d.short_title, d.file_name AS derived_file,
                   d.segmentation_representation, dor.referenced_file_name,
                   dor.confidence, dor.inference_method
            FROM derived_objects d
            LEFT JOIN derived_object_references dor
              ON dor.derived_object_id = d.derived_object_id
            WHERE {' AND '.join(where)}
            ORDER BY lower(d.short_title), d.file_name, dor.referenced_file_name
            LIMIT ?
        """
        columns = [
            "short_title",
            "derived_file",
            "segmentation_representation",
            "referenced_file_name",
            "confidence",
            "inference_method",
        ]
    else:
        sql = f"""
            SELECT d.short_title, d.file_name, d.derived_object_type,
                   d.segmentation_representation, d.referenced_series_id
            FROM derived_objects d
            WHERE {' AND '.join(where)}
            ORDER BY lower(d.short_title), d.file_name
            LIMIT ?
        """
        columns = [
            "short_title",
            "file_name",
            "derived_object_type",
            "segmentation_representation",
            "referenced_series_id",
        ]
    rows = rows_as_dicts(conn, sql, tuple(params))
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(rows, columns)
    return 0


def command_drift_check(args: argparse.Namespace) -> int:
    snapshot_conn = sqlite3.connect(args.snapshot_db)
    snapshot_conn.row_factory = sqlite3.Row
    current_rows = nifti_download_rows_from_snapshot(snapshot_conn)
    snapshot_conn.close()
    current_signature = download_signature(current_rows)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    release_signature = manifest.get("nifti_download_signature")
    payload = {
        "status": "unchanged" if current_signature == release_signature else "changed",
        "current_nifti_download_count": len(current_rows),
        "release_nifti_download_count": manifest.get("nifti_download_count"),
        "current_nifti_download_signature": current_signature,
        "release_nifti_download_signature": release_signature,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"NIfTI download signature {payload['status']}: "
            f"current={payload['current_nifti_download_count']} "
            f"release={payload['release_nifti_download_count']}"
        )
    return 0 if payload["status"] == "unchanged" else 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure = subparsers.add_parser("ensure", help="Download optional NIfTI SQLite release assets.")
    ensure.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository owner/name.")
    ensure.add_argument("--tag", default=DEFAULT_RELEASE_TAG, help="Release tag.")
    ensure.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Local SQLite output path.")
    ensure.add_argument(
        "--manifest-out", default=str(DEFAULT_MANIFEST_PATH), help="Local manifest output path."
    )

    info = subparsers.add_parser("info", help="Show local NIfTI metadata DB status.")
    info.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Local SQLite path.")
    info.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Local manifest path.")
    info.add_argument("--json", action="store_true", help="Emit JSON.")

    manifest = subparsers.add_parser("manifest", help="Write a release manifest for a NIfTI DB.")
    manifest.add_argument("--db", required=True, help="SQLite path.")
    manifest.add_argument("--gzip", help="Gzipped SQLite path.")
    manifest.add_argument("--snapshot-db", help="Optional TCIA snapshot DB for source signature check.")
    manifest.add_argument("--out", required=True, help="Manifest JSON output path.")

    validate = subparsers.add_parser("validate", help="Validate a local NIfTI metadata SQLite DB.")
    validate.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path.")
    validate.add_argument("--json", action="store_true", help="Emit JSON.")

    drift = subparsers.add_parser(
        "drift-check",
        help="Compare current snapshot NIfTI download records with a NIfTI release manifest.",
    )
    drift.add_argument("--snapshot-db", required=True, help="Current TCIA snapshot SQLite path.")
    drift.add_argument("--manifest", required=True, help="NIfTI metadata manifest path.")
    drift.add_argument("--json", action="store_true", help="Emit JSON.")

    datasets = subparsers.add_parser("datasets", help="Summarize NIfTI rows by dataset.")
    datasets.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path.")
    datasets.add_argument("--limit", type=int, default=100, help="Maximum rows.")
    datasets.add_argument("--json", action="store_true", help="Emit JSON.")

    files = subparsers.add_parser("files", help="List NIfTI file/series rows.")
    files.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path.")
    files.add_argument("--collection", help="Filter by TCIA short title.")
    files.add_argument("--modality", help="Filter by modality, such as MR or CT.")
    files.add_argument("--derived", action="store_true", help="Show only derived-object rows.")
    files.add_argument("--source", action="store_true", help="Show only source-image rows.")
    files.add_argument("--limit", type=int, default=20, help="Maximum rows.")
    files.add_argument("--json", action="store_true", help="Emit JSON.")

    derived = subparsers.add_parser("derived", help="List derived objects and optional source links.")
    derived.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path.")
    derived.add_argument("--collection", help="Filter by TCIA short title.")
    derived.add_argument("--with-sources", action="store_true", help="Include source-file links.")
    derived.add_argument("--limit", type=int, default=20, help="Maximum rows.")
    derived.add_argument("--json", action="store_true", help="Emit JSON.")

    args = parser.parse_args(argv)

    if args.command == "ensure":
        result = ensure_release_nifti(
            args.repo, args.tag, Path(args.db), Path(args.manifest_out)
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "info":
        return command_info(args)
    if args.command == "manifest":
        payload = build_manifest(
            Path(args.db),
            Path(args.gzip) if args.gzip else None,
            Path(args.snapshot_db) if args.snapshot_db else None,
        )
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.command == "validate":
        payload = validate_db(Path(args.db))
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"integrity_check: {payload['integrity_check']}")
            print(f"missing_tables: {', '.join(payload['missing_tables']) or 'none'}")
        return 0 if payload["integrity_check"] == "ok" and not payload["missing_tables"] else 1
    if args.command == "drift-check":
        return command_drift_check(args)
    if args.command == "datasets":
        return command_datasets(args)
    if args.command == "files":
        return command_files(args)
    if args.command == "derived":
        return command_derived(args)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, sqlite3.Error, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
