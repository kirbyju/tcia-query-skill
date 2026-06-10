#!/usr/bin/env python3
"""Build, download, and query optional TCIA pathology Aspera metadata SQLite."""

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = 1
DEFAULT_REPO = "kirbyju/tcia-query-skill"
DEFAULT_RELEASE_TAG = "tcia-snapshot-latest"
PATHOLOGY_ASSET = "pathology_metadata.sqlite.gz"
PATHOLOGY_MANIFEST_ASSET = "pathology_metadata_manifest.json"
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = SKILL_ROOT / "cache" / "pathology_metadata.sqlite"
DEFAULT_MANIFEST_PATH = SKILL_ROOT / "cache" / PATHOLOGY_MANIFEST_ASSET
DEFAULT_SNAPSHOT_DB = SKILL_ROOT / "cache" / "tcia_snapshot.sqlite"
USER_AGENT = "tcia-pathology-metadata/1.0"

REQUIRED_TABLES = [
    "pathology_meta",
    "pathology_downloads",
    "pathology_download_label_matches",
    "pathology_package_files",
    "pathology_file_objects",
    "pathdb_slide_crosswalk",
    "pathology_disparities",
]

PATHOLOGY_DOWNLOAD_SIGNATURE_COLUMNS = [
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

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pathology_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pathology_downloads (
    download_row_id INTEGER PRIMARY KEY,
    parent_source TEXT NOT NULL,
    dataset_type TEXT NOT NULL,
    parent_id TEXT,
    parent_slug TEXT,
    short_title TEXT NOT NULL,
    title TEXT,
    download_id TEXT,
    download_slug TEXT,
    download_title TEXT,
    download_url TEXT NOT NULL,
    date_updated TEXT,
    collection_status TEXT,
    description TEXT,
    license_label TEXT,
    license_url TEXT,
    requirements_label TEXT,
    requirements_url TEXT,
    requirements_text TEXT,
    access_level TEXT,
    noncommercial_license INTEGER NOT NULL DEFAULT 0,
    controlled_access INTEGER NOT NULL DEFAULT 0,
    download_size TEXT,
    download_size_unit TEXT,
    subjects TEXT,
    studies TEXT,
    series TEXT,
    images TEXT,
    download_types TEXT,
    data_types TEXT,
    file_types TEXT,
    external_resources TEXT,
    pathology_match_reasons TEXT,
    pathdb_collection_slide_count INTEGER NOT NULL DEFAULT 0,
    pathdb_collection_patient_count INTEGER NOT NULL DEFAULT 0,
    has_pathdb_rows INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS pathology_download_label_matches (
    download_row_id INTEGER NOT NULL,
    label_kind TEXT NOT NULL,
    label TEXT NOT NULL,
    match_reason TEXT NOT NULL,
    PRIMARY KEY (download_row_id, label_kind, label),
    FOREIGN KEY (download_row_id) REFERENCES pathology_downloads(download_row_id)
);

CREATE TABLE IF NOT EXISTS pathology_package_files (
    package_file_id TEXT PRIMARY KEY,
    download_row_id INTEGER NOT NULL,
    parent_source TEXT NOT NULL,
    dataset_type TEXT NOT NULL,
    short_title TEXT NOT NULL,
    download_id TEXT,
    download_title TEXT,
    source_url TEXT NOT NULL,
    package_path TEXT NOT NULL,
    file_name TEXT,
    file_ext TEXT,
    file_role TEXT,
    bytes INTEGER,
    checksum TEXT,
    checksum_algorithm TEXT,
    modified_time TEXT,
    inventory_source TEXT,
    inventory_status TEXT NOT NULL DEFAULT 'pending',
    row_json TEXT,
    browsed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (download_row_id) REFERENCES pathology_downloads(download_row_id)
);

CREATE TABLE IF NOT EXISTS pathology_file_objects (
    non_dicom_file_id TEXT PRIMARY KEY,
    package_file_id TEXT,
    download_row_id INTEGER NOT NULL,
    parent_source TEXT NOT NULL,
    dataset_type TEXT NOT NULL,
    short_title TEXT NOT NULL,
    download_id TEXT,
    file_name TEXT,
    file_ext TEXT,
    package_path TEXT,
    file_group_id TEXT,
    file_role TEXT,
    bytes INTEGER,
    checksum TEXT,
    checksum_algorithm TEXT,
    object_modality TEXT,
    image_format TEXT,
    is_wsi INTEGER NOT NULL DEFAULT 0,
    is_micrograph INTEGER NOT NULL DEFAULT 0,
    is_codex INTEGER NOT NULL DEFAULT 0,
    is_metadata INTEGER NOT NULL DEFAULT 0,
    source_table TEXT,
    source_row_id TEXT,
    quality_flag_json TEXT,
    FOREIGN KEY (download_row_id) REFERENCES pathology_downloads(download_row_id),
    FOREIGN KEY (package_file_id) REFERENCES pathology_package_files(package_file_id)
);

CREATE TABLE IF NOT EXISTS pathdb_slide_crosswalk (
    crosswalk_id TEXT PRIMARY KEY,
    short_title TEXT NOT NULL,
    pathdb_collection TEXT NOT NULL,
    patient_id TEXT,
    slide_id TEXT,
    camic_id TEXT,
    camicroscope_url TEXT,
    wsiimage_url TEXT,
    species TEXT,
    cancer_type TEXT,
    cancer_location TEXT,
    data_format TEXT,
    modality TEXT,
    protocol TEXT,
    par TEXT,
    magnification TEXT,
    pathdb_update TEXT,
    download_row_id INTEGER,
    download_id TEXT,
    package_file_id TEXT,
    non_dicom_file_id TEXT,
    package_path TEXT,
    file_name TEXT,
    match_status TEXT NOT NULL DEFAULT 'collection_only',
    match_method TEXT,
    match_confidence TEXT,
    evidence_json TEXT,
    FOREIGN KEY (download_row_id) REFERENCES pathology_downloads(download_row_id),
    FOREIGN KEY (package_file_id) REFERENCES pathology_package_files(package_file_id),
    FOREIGN KEY (non_dicom_file_id) REFERENCES pathology_file_objects(non_dicom_file_id)
);

CREATE TABLE IF NOT EXISTS pathology_disparities (
    disparity_id TEXT PRIMARY KEY,
    disparity_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'review',
    short_title TEXT,
    download_id TEXT,
    download_row_id INTEGER,
    package_path TEXT,
    file_name TEXT,
    pathdb_collection TEXT,
    pathdb_slide_id TEXT,
    message TEXT NOT NULL,
    evidence_json TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pathology_downloads_short_title
  ON pathology_downloads(short_title);
CREATE INDEX IF NOT EXISTS idx_pathology_downloads_download
  ON pathology_downloads(download_id);
CREATE INDEX IF NOT EXISTS idx_pathology_package_files_download
  ON pathology_package_files(download_row_id);
CREATE INDEX IF NOT EXISTS idx_pathology_package_files_path
  ON pathology_package_files(short_title, package_path);
CREATE INDEX IF NOT EXISTS idx_pathology_file_objects_download
  ON pathology_file_objects(download_row_id);
CREATE INDEX IF NOT EXISTS idx_pathology_file_objects_group
  ON pathology_file_objects(file_group_id);
CREATE INDEX IF NOT EXISTS idx_pathdb_crosswalk_collection
  ON pathdb_slide_crosswalk(pathdb_collection);
CREATE INDEX IF NOT EXISTS idx_pathdb_crosswalk_slide
  ON pathdb_slide_crosswalk(slide_id);
CREATE INDEX IF NOT EXISTS idx_pathdb_crosswalk_status
  ON pathdb_slide_crosswalk(match_status);
CREATE INDEX IF NOT EXISTS idx_pathology_disparities_type
  ON pathology_disparities(disparity_type, status);

CREATE VIEW IF NOT EXISTS pathology_dataset_summary AS
SELECT
    parent_source,
    dataset_type,
    short_title,
    COUNT(*) AS download_records,
    SUM(CASE WHEN has_pathdb_rows THEN 1 ELSE 0 END) AS downloads_with_pathdb_collection,
    MAX(pathdb_collection_slide_count) AS pathdb_collection_slide_count,
    MAX(pathdb_collection_patient_count) AS pathdb_collection_patient_count,
    SUM(CASE WHEN noncommercial_license THEN 1 ELSE 0 END) AS open_noncommercial_downloads,
    group_concat(download_id, '; ') AS download_ids,
    group_concat(download_title, '; ') AS download_titles
FROM pathology_downloads
GROUP BY parent_source, dataset_type, short_title
ORDER BY lower(short_title);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    env_path = os.environ.get("TCIA_PATHOLOGY_METADATA_DB")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


def manifest_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    env_path = os.environ.get("TCIA_PATHOLOGY_METADATA_MANIFEST")
    if env_path:
        return Path(env_path)
    return DEFAULT_MANIFEST_PATH


def connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    resolved = db_path(path)
    if not resolved.exists():
        raise RuntimeError(
            f"Pathology metadata SQLite not found at {resolved}. "
            "Run `python scripts/tcia_pathology_metadata.py ensure` first."
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


def rows_as_dicts(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params)]


def get_pathology_meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM pathology_meta")}
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


def pathology_download_filter_sql(prefix: str = "") -> str:
    return f"""
        WITH label_matches AS (
            SELECT
                l.download_row_id,
                l.label_kind,
                l.label,
                CASE
                    WHEN lower(l.label) LIKE '%pathology%' THEN 'pathology_label'
                    WHEN lower(l.label) LIKE '%histopathology%' THEN 'histopathology_label'
                    WHEN lower(l.label) LIKE '%whole slide%' THEN 'whole_slide_label'
                    WHEN lower(l.label) LIKE '%photomicrograph%' THEN 'photomicrograph_label'
                    WHEN lower(l.label) LIKE '%single-cell%' THEN 'single_cell_label'
                    WHEN lower(l.label) LIKE '%single cell%' THEN 'single_cell_label'
                    WHEN lower(l.label) LIKE '%codex%' THEN 'codex_label'
                    WHEN lower(l.label) LIKE '%immunofluorescence%' THEN 'immunofluorescence_label'
                    WHEN lower(l.label) = 'svs' THEN 'svs_file_type'
                    ELSE 'other_pathology_hint'
                END AS match_reason
            FROM {prefix}wordpress_download_labels l
            WHERE lower(l.label) LIKE '%pathology%'
               OR lower(l.label) LIKE '%histopathology%'
               OR lower(l.label) LIKE '%whole slide%'
               OR lower(l.label) LIKE '%photomicrograph%'
               OR lower(l.label) LIKE '%single-cell%'
               OR lower(l.label) LIKE '%single cell%'
               OR lower(l.label) LIKE '%codex%'
               OR lower(l.label) LIKE '%immunofluorescence%'
               OR lower(l.label) = 'svs'
        ),
        title_matches AS (
            SELECT
                d.download_row_id,
                'download_title' AS label_kind,
                d.download_title AS label,
                CASE
                    WHEN lower(d.download_title) LIKE '%patholog%' THEN 'pathology_title'
                    WHEN lower(d.download_title) LIKE '%tissue slide%' THEN 'tissue_slide_title'
                    WHEN lower(d.download_title) LIKE '%slide image%' THEN 'slide_image_title'
                    ELSE 'slide_title'
                END AS match_reason
            FROM {prefix}agent_current_downloads d
            WHERE lower(d.download_title) LIKE '%patholog%'
               OR lower(d.download_title) LIKE '%tissue slide%'
               OR lower(d.download_title) LIKE '%slide image%'
        ),
        matches AS (
            SELECT * FROM label_matches
            UNION
            SELECT * FROM title_matches
        ),
        pathdb_summary AS (
            SELECT
                lower(collection) AS collection_key,
                COUNT(*) AS slide_count,
                COUNT(DISTINCT NULLIF(patient_id, '')) AS patient_count
            FROM {prefix}agent_pathdb_slides
            GROUP BY lower(collection)
        ),
        download_reasons AS (
            SELECT
                download_row_id,
                group_concat(match_reason || ':' || label_kind || '=' || label, '; ') AS reasons
            FROM matches
            GROUP BY download_row_id
        )
        SELECT
            d.download_row_id,
            d.parent_source,
            d.dataset_type,
            d.parent_id,
            d.parent_slug,
            d.short_title,
            d.title,
            d.download_id,
            d.download_slug,
            d.download_title,
            d.download_url,
            d.date_updated,
            d.collection_status,
            d.description,
            d.license_label,
            d.license_url,
            d.requirements_label,
            d.requirements_url,
            d.requirements_text,
            d.access_level,
            d.noncommercial_license,
            d.controlled_access,
            d.download_size,
            d.download_size_unit,
            d.subjects,
            d.studies,
            d.series,
            d.images,
            d.download_types,
            d.data_types,
            d.file_types,
            d.external_resources,
            r.reasons AS pathology_match_reasons,
            COALESCE(p.slide_count, 0) AS pathdb_collection_slide_count,
            COALESCE(p.patient_count, 0) AS pathdb_collection_patient_count,
            CASE WHEN p.slide_count IS NULL THEN 0 ELSE 1 END AS has_pathdb_rows,
            d.raw_json
        FROM {prefix}agent_current_downloads d
        JOIN download_reasons r ON r.download_row_id = d.download_row_id
        LEFT JOIN pathdb_summary p ON p.collection_key = lower(d.short_title)
        WHERE d.hidden = 0
          AND d.controlled_access = 0
          AND lower(d.download_url) LIKE '%faspex%'
        ORDER BY lower(d.short_title), d.download_id, d.download_title
    """


def pathology_download_rows_from_metadata_db(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = ", ".join(PATHOLOGY_DOWNLOAD_SIGNATURE_COLUMNS)
    return rows_as_dicts(
        conn,
        f"""
        SELECT {columns}
        FROM pathology_downloads
        ORDER BY lower(short_title), download_id, download_title, download_url
        """,
    )


def pathology_download_rows_from_snapshot(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = ", ".join(PATHOLOGY_DOWNLOAD_SIGNATURE_COLUMNS)
    return rows_as_dicts(
        conn,
        f"""
        SELECT {columns}
        FROM ({pathology_download_filter_sql("")})
        ORDER BY lower(short_title), download_id, download_title, download_url
        """,
    )


def download_signature(rows: list[dict[str, Any]]) -> str:
    normalized = [
        {key: str(row.get(key) or "") for key in PATHOLOGY_DOWNLOAD_SIGNATURE_COLUMNS}
        for row in rows
    ]
    return hashlib.sha256(json_dumps(normalized).encode("utf-8")).hexdigest()


def release_fingerprint(manifest: dict[str, Any]) -> str:
    payload = {
        "schema_version": manifest.get("schema_version"),
        "sqlite_sha256": manifest.get("sqlite_sha256"),
        "gzip_sha256": manifest.get("gzip_sha256"),
        "pathology_download_signature": manifest.get("pathology_download_signature"),
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
    meta = get_pathology_meta(conn)
    counts = table_counts(conn)
    rows = pathology_download_rows_from_metadata_db(conn)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()

    manifest: dict[str, Any] = {
        "asset": PATHOLOGY_ASSET,
        "schema_version": SCHEMA_VERSION,
        "sqlite_sha256": file_sha256(sqlite_path),
        "sqlite_bytes": sqlite_path.stat().st_size,
        "table_counts": counts,
        "pathology_meta": meta,
        "pathology_download_count": len(rows),
        "pathology_download_signature": download_signature(rows),
        "sqlite_integrity_check": integrity,
    }
    if gzip_path and gzip_path.exists():
        manifest["gzip_sha256"] = file_sha256(gzip_path)
        manifest["gzip_bytes"] = gzip_path.stat().st_size
    if snapshot_db and snapshot_db.exists():
        snapshot_conn = sqlite3.connect(snapshot_db)
        snapshot_conn.row_factory = sqlite3.Row
        snapshot_rows = pathology_download_rows_from_snapshot(snapshot_conn)
        snapshot_conn.close()
        manifest["source_snapshot_pathology_download_count"] = len(snapshot_rows)
        manifest["source_snapshot_pathology_download_signature"] = download_signature(snapshot_rows)
        manifest["source_snapshot_matches_metadata"] = (
            manifest["source_snapshot_pathology_download_signature"]
            == manifest["pathology_download_signature"]
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


def ensure_release_pathology(repo: str, tag: str, local_db: Path, local_manifest: Path) -> dict[str, Any]:
    assets = release_assets(repo, tag)
    missing = [
        name for name in (PATHOLOGY_ASSET, PATHOLOGY_MANIFEST_ASSET) if name not in assets
    ]
    if missing:
        raise RuntimeError(
            f"Release {repo}@{tag} is missing pathology metadata assets: {', '.join(missing)}"
        )

    manifest_body, _headers = fetch_bytes(assets[PATHOLOGY_MANIFEST_ASSET]["browser_download_url"])
    remote_manifest = json.loads(manifest_body.decode("utf-8"))
    if local_manifest_current(local_db, local_manifest, remote_manifest):
        local_manifest.parent.mkdir(parents=True, exist_ok=True)
        local_manifest.write_bytes(manifest_body)
        return {"status": "unchanged", "manifest": remote_manifest}

    compressed, _headers = fetch_bytes(assets[PATHOLOGY_ASSET]["browser_download_url"], timeout=300)
    expected_gzip = remote_manifest.get("gzip_sha256")
    actual_gzip = hashlib.sha256(compressed).hexdigest()
    if expected_gzip and actual_gzip != expected_gzip:
        raise RuntimeError("Downloaded pathology metadata gzip SHA-256 does not match manifest.")

    local_db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=str(local_db.parent)) as tmp:
        tmp_path = Path(tmp.name)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as gz:
            shutil.copyfileobj(gz, tmp)
    expected_sqlite = remote_manifest.get("sqlite_sha256")
    actual_sqlite = file_sha256(tmp_path)
    if expected_sqlite and actual_sqlite != expected_sqlite:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded pathology metadata SQLite SHA-256 does not match manifest.")
    tmp_path.replace(local_db)
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    local_manifest.write_bytes(manifest_body)
    return {"status": "downloaded", "manifest": remote_manifest}


def require_source_views(conn: sqlite3.Connection) -> None:
    required = {
        "agent_current_downloads",
        "wordpress_download_labels",
        "agent_pathdb_slides",
        "snapshot_meta",
    }
    found = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM source.sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    missing = sorted(required - found)
    if missing:
        raise RuntimeError(f"source snapshot is missing required tables/views: {', '.join(missing)}")


def write_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO pathology_meta(key, value) VALUES (?, ?)",
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
    )


def snapshot_meta(conn: sqlite3.Connection) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for key, value in conn.execute("SELECT key, value FROM source.snapshot_meta"):
        try:
            meta[key] = json.loads(value)
        except json.JSONDecodeError:
            meta[key] = value
    return meta


def seed_downloads(conn: sqlite3.Connection) -> int:
    rows = [dict(row) for row in conn.execute(pathology_download_filter_sql("source."))]
    if not rows:
        return 0
    columns = list(rows[0].keys())
    placeholders = ", ".join(":" + column for column in columns)
    conn.executemany(
        f"""
        INSERT INTO pathology_downloads ({", ".join(columns)})
        VALUES ({placeholders})
        """,
        rows,
    )
    return len(rows)


def seed_label_matches(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        INSERT INTO pathology_download_label_matches
        (download_row_id, label_kind, label, match_reason)
        WITH label_matches AS (
            SELECT
                l.download_row_id,
                l.label_kind,
                l.label,
                CASE
                    WHEN lower(l.label) LIKE '%pathology%' THEN 'pathology_label'
                    WHEN lower(l.label) LIKE '%histopathology%' THEN 'histopathology_label'
                    WHEN lower(l.label) LIKE '%whole slide%' THEN 'whole_slide_label'
                    WHEN lower(l.label) LIKE '%photomicrograph%' THEN 'photomicrograph_label'
                    WHEN lower(l.label) LIKE '%single-cell%' THEN 'single_cell_label'
                    WHEN lower(l.label) LIKE '%single cell%' THEN 'single_cell_label'
                    WHEN lower(l.label) LIKE '%codex%' THEN 'codex_label'
                    WHEN lower(l.label) LIKE '%immunofluorescence%' THEN 'immunofluorescence_label'
                    WHEN lower(l.label) = 'svs' THEN 'svs_file_type'
                    ELSE 'other_pathology_hint'
                END AS match_reason
            FROM source.wordpress_download_labels l
            JOIN pathology_downloads d ON d.download_row_id = l.download_row_id
            WHERE lower(l.label) LIKE '%pathology%'
               OR lower(l.label) LIKE '%histopathology%'
               OR lower(l.label) LIKE '%whole slide%'
               OR lower(l.label) LIKE '%photomicrograph%'
               OR lower(l.label) LIKE '%single-cell%'
               OR lower(l.label) LIKE '%single cell%'
               OR lower(l.label) LIKE '%codex%'
               OR lower(l.label) LIKE '%immunofluorescence%'
               OR lower(l.label) = 'svs'
        ),
        title_matches AS (
            SELECT
                d.download_row_id,
                'download_title' AS label_kind,
                d.download_title AS label,
                CASE
                    WHEN lower(d.download_title) LIKE '%patholog%' THEN 'pathology_title'
                    WHEN lower(d.download_title) LIKE '%tissue slide%' THEN 'tissue_slide_title'
                    WHEN lower(d.download_title) LIKE '%slide image%' THEN 'slide_image_title'
                    ELSE 'slide_title'
                END AS match_reason
            FROM pathology_downloads d
            WHERE lower(d.download_title) LIKE '%patholog%'
               OR lower(d.download_title) LIKE '%tissue slide%'
               OR lower(d.download_title) LIKE '%slide image%'
        )
        SELECT DISTINCT download_row_id, label_kind, label, match_reason FROM label_matches
        UNION
        SELECT DISTINCT download_row_id, label_kind, label, match_reason FROM title_matches
        """
    )
    return int(conn.execute("SELECT COUNT(*) FROM pathology_download_label_matches").fetchone()[0])


def seed_pathdb_crosswalk(conn: sqlite3.Connection, created_at: str) -> int:
    conn.execute(
        """
        INSERT INTO pathdb_slide_crosswalk
        (crosswalk_id, short_title, pathdb_collection, patient_id, slide_id, camic_id,
         camicroscope_url, wsiimage_url, species, cancer_type, cancer_location,
         data_format, modality, protocol, par, magnification, pathdb_update,
         match_status, match_method, match_confidence, evidence_json)
        WITH pathdb_candidates AS (
            SELECT
                p.*,
                row_number() OVER (
                    ORDER BY
                        lower(p.collection),
                        COALESCE(NULLIF(p.camic_id, ''), ''),
                        COALESCE(NULLIF(p.slide_id, ''), ''),
                        COALESCE(NULLIF(p.patient_id, ''), ''),
                        COALESCE(NULLIF(p.wsiimage_url, ''), '')
                ) AS candidate_number
            FROM source.agent_pathdb_slides p
            JOIN (
                SELECT DISTINCT short_title
                FROM pathology_downloads
            ) d ON lower(d.short_title) = lower(p.collection)
        )
        SELECT
            'pathdb:' || lower(p.collection) || ':' || p.candidate_number,
            d.short_title,
            p.collection,
            p.patient_id,
            p.slide_id,
            p.camic_id,
            p.camicroscope_url,
            p.wsiimage_url,
            p.species,
            p.cancer_type,
            p.cancer_location,
            p.data_format,
            p.modality,
            p.protocol,
            p.par,
            p.magnification,
            p."update",
            'collection_only',
            'pathdb.collection = pathology_downloads.short_title',
            'low',
            json_object(
                'note', 'PathDB row is matched to the TCIA short title only; it is not yet tied to a Collection Manager download file.',
                'seeded_at', ?
            )
        FROM pathdb_candidates p
        JOIN (
            SELECT DISTINCT short_title
            FROM pathology_downloads
        ) d ON lower(d.short_title) = lower(p.collection)
        """,
        (created_at,),
    )
    return int(conn.execute("SELECT COUNT(*) FROM pathdb_slide_crosswalk").fetchone()[0])


def seed_disparities(conn: sqlite3.Connection, created_at: str) -> int:
    conn.execute(
        """
        INSERT INTO pathology_disparities
        (disparity_id, disparity_type, severity, short_title, download_id, download_row_id,
         message, evidence_json, created_at)
        SELECT
            'download_without_pathdb:' || download_row_id,
            'download_without_pathdb_collection_match',
            'review',
            short_title,
            download_id,
            download_row_id,
            'Collection Manager pathology Aspera download has no same-short-title PathDB rows.',
            json_object(
                'download_title', download_title,
                'download_url', download_url,
                'pathology_match_reasons', pathology_match_reasons
            ),
            ?
        FROM pathology_downloads
        WHERE has_pathdb_rows = 0
        """,
        (created_at,),
    )
    conn.execute(
        """
        INSERT INTO pathology_disparities
        (disparity_id, disparity_type, severity, pathdb_collection, message, evidence_json, created_at)
        SELECT
            'pathdb_without_download:' || lower(p.collection),
            'pathdb_collection_without_pathology_aspera_download_match',
            'review',
            p.collection,
            'PathDB collection has no exact same-short-title public pathology Aspera download in Collection Manager.',
            json_object(
                'pathdb_rows', COUNT(*),
                'pathdb_patients', COUNT(DISTINCT NULLIF(p.patient_id, '')),
                'pathdb_slides', COUNT(DISTINCT NULLIF(p.slide_id, '')),
                'data_formats', group_concat(DISTINCT p.data_format)
            ),
            ?
        FROM source.agent_pathdb_slides p
        LEFT JOIN pathology_downloads d
          ON lower(d.short_title) = lower(p.collection)
        WHERE d.short_title IS NULL
        GROUP BY p.collection
        """,
        (created_at,),
    )
    conn.execute(
        """
        INSERT INTO pathology_disparities
        (disparity_id, disparity_type, severity, short_title, message, evidence_json, created_at)
        SELECT
            'multiple_downloads:' || lower(short_title),
            'multiple_pathology_aspera_downloads_for_short_title',
            'review',
            short_title,
            'Collection Manager short title has multiple public pathology Aspera downloads; file-level PathDB reconciliation should not assume collection-level uniqueness.',
            json_object(
                'download_records', COUNT(*),
                'downloads', group_concat(download_id || ': ' || download_title, ' | ')
            ),
            ?
        FROM pathology_downloads
        GROUP BY short_title
        HAVING COUNT(*) > 1
        """,
        (created_at,),
    )
    return int(conn.execute("SELECT COUNT(*) FROM pathology_disparities").fetchone()[0])


def build_pathology_db(snapshot_db: Path, out_path: Path, replace: bool = False) -> dict[str, Any]:
    if not snapshot_db.exists():
        raise RuntimeError(f"source snapshot not found: {snapshot_db}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and not replace:
        raise RuntimeError(f"output already exists: {out_path}. Use --replace to rebuild it.")
    if replace and out_path.exists():
        out_path.unlink()
        for suffix in ("-shm", "-wal", "-journal"):
            sidecar = Path(str(out_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

    conn = sqlite3.connect(out_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("ATTACH DATABASE ? AS source", (str(snapshot_db),))
    require_source_views(conn)
    conn.executescript(SCHEMA_SQL)
    created_at = utc_now()
    downloads = seed_downloads(conn)
    label_matches = seed_label_matches(conn)
    pathdb_rows = seed_pathdb_crosswalk(conn, created_at)
    disparities = seed_disparities(conn, created_at)
    write_meta(conn, "created_at", created_at)
    write_meta(conn, "source_snapshot_db", str(snapshot_db.resolve()))
    write_meta(conn, "source_snapshot_meta", snapshot_meta(conn))
    write_meta(
        conn,
        "scope_note",
        "Visible, non-controlled TCIA Collection Manager current downloads routed through Aspera and matched to pathology/WSI-related labels.",
    )
    write_meta(
        conn,
        "pathology_package_policy",
        "Public pathology Aspera packages are treated as the authoritative file copy and should be preserved as-is, including image files, metadata, JSON, CSV, sidecars, package documentation, and other non-image files.",
    )
    write_meta(
        conn,
        "pathdb_note",
        "PathDB rows are seeded as collection_only crosswalk candidates, not file-level matches.",
    )
    write_meta(
        conn,
        "table_counts",
        {
            "pathology_downloads": downloads,
            "pathology_download_label_matches": label_matches,
            "pathdb_slide_crosswalk": pathdb_rows,
            "pathology_disparities": disparities,
        },
    )
    conn.commit()
    conn.close()
    return {
        "sqlite": str(out_path.resolve()),
        "pathology_downloads": downloads,
        "pathology_download_label_matches": label_matches,
        "pathdb_slide_crosswalk_rows": pathdb_rows,
        "pathology_disparities": disparities,
    }


def gzip_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as source, gzip.open(dst, "wb") as target:
        shutil.copyfileobj(source, target)


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
    meta = get_pathology_meta(conn)
    conn.close()
    manifest: dict[str, Any] = {}
    candidate_manifest = manifest_path(args.manifest)
    if candidate_manifest.exists():
        manifest = json.loads(candidate_manifest.read_text(encoding="utf-8"))
    payload = {
        "db": str(db_path(args.db)),
        "manifest": str(candidate_manifest),
        "table_counts": counts,
        "pathology_meta": meta,
        "release_fingerprint": manifest.get("release_fingerprint", ""),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"DB: {payload['db']}")
        print(f"Manifest: {payload['manifest']}")
        if payload["release_fingerprint"]:
            print(f"Release fingerprint: {payload['release_fingerprint']}")
        for key in ("pathology_downloads", "pathology_package_files", "pathdb_slide_crosswalk", "pathology_disparities"):
            print(f"{key}: {counts.get(key, 0)}")
    return 0


def command_datasets(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rows = rows_as_dicts(
        conn,
        """
        SELECT short_title, dataset_type, download_records,
               pathdb_collection_slide_count, pathdb_collection_patient_count,
               open_noncommercial_downloads, download_ids
        FROM pathology_dataset_summary
        ORDER BY lower(short_title)
        LIMIT ?
        """,
        (args.limit,),
    )
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(
            rows,
            [
                "short_title",
                "dataset_type",
                "download_records",
                "pathdb_collection_slide_count",
                "pathdb_collection_patient_count",
                "open_noncommercial_downloads",
                "download_ids",
            ],
        )
    return 0


def command_downloads(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    where = ["1 = 1"]
    params: list[Any] = []
    if args.collection:
        where.append("short_title = ?")
        params.append(args.collection)
    if args.noncommercial:
        where.append("noncommercial_license = 1")
    params.append(args.limit)
    rows = rows_as_dicts(
        conn,
        f"""
        SELECT short_title, download_id, download_title, access_level,
               license_label, download_size, download_size_unit, has_pathdb_rows
        FROM pathology_downloads
        WHERE {' AND '.join(where)}
        ORDER BY lower(short_title), download_id, download_title
        LIMIT ?
        """,
        tuple(params),
    )
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(
            rows,
            [
                "short_title",
                "download_id",
                "download_title",
                "access_level",
                "license_label",
                "has_pathdb_rows",
            ],
        )
    return 0


def command_pathdb(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    where = ["1 = 1"]
    params: list[Any] = []
    if args.collection:
        where.append("short_title = ? OR pathdb_collection = ?")
        params.extend([args.collection, args.collection])
    params.append(args.limit)
    rows = rows_as_dicts(
        conn,
        f"""
        SELECT short_title, pathdb_collection, patient_id, slide_id, camic_id,
               wsiimage_url, data_format, modality, match_status
        FROM pathdb_slide_crosswalk
        WHERE {' AND '.join(where)}
        ORDER BY lower(pathdb_collection), patient_id, slide_id, camic_id
        LIMIT ?
        """,
        tuple(params),
    )
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(
            rows,
            [
                "short_title",
                "pathdb_collection",
                "patient_id",
                "slide_id",
                "camic_id",
                "data_format",
                "match_status",
            ],
        )
    return 0


def command_files(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    where = ["1 = 1"]
    params: list[Any] = []
    if args.collection:
        where.append("short_title = ?")
        params.append(args.collection)
    if args.file_ext:
        where.append("lower(file_ext) = lower(?)")
        params.append(args.file_ext)
    params.append(args.limit)
    rows = rows_as_dicts(
        conn,
        f"""
        SELECT short_title, download_id, file_name, file_ext, file_role,
               bytes, package_path, inventory_status
        FROM pathology_package_files
        WHERE {' AND '.join(where)}
        ORDER BY lower(short_title), download_id, package_path
        LIMIT ?
        """,
        tuple(params),
    )
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(rows, ["short_title", "download_id", "file_name", "file_ext", "file_role", "inventory_status"])
    return 0


def command_disparities(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    where = ["1 = 1"]
    params: list[Any] = []
    if args.type:
        where.append("disparity_type = ?")
        params.append(args.type)
    if args.collection:
        where.append("(short_title = ? OR pathdb_collection = ?)")
        params.extend([args.collection, args.collection])
    params.append(args.limit)
    rows = rows_as_dicts(
        conn,
        f"""
        SELECT disparity_type, severity, short_title, download_id,
               pathdb_collection, message, status
        FROM pathology_disparities
        WHERE {' AND '.join(where)}
        ORDER BY severity, disparity_type, lower(COALESCE(short_title, pathdb_collection, ''))
        LIMIT ?
        """,
        tuple(params),
    )
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(
            rows,
            [
                "disparity_type",
                "severity",
                "short_title",
                "download_id",
                "pathdb_collection",
                "message",
                "status",
            ],
        )
    return 0


def command_drift_check(args: argparse.Namespace) -> int:
    snapshot_conn = sqlite3.connect(args.snapshot_db)
    snapshot_conn.row_factory = sqlite3.Row
    current_rows = pathology_download_rows_from_snapshot(snapshot_conn)
    snapshot_conn.close()
    current_signature = download_signature(current_rows)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    release_signature = manifest.get("pathology_download_signature")
    payload = {
        "status": "unchanged" if current_signature == release_signature else "changed",
        "current_pathology_download_count": len(current_rows),
        "release_pathology_download_count": manifest.get("pathology_download_count"),
        "current_pathology_download_signature": current_signature,
        "release_pathology_download_signature": release_signature,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Pathology download signature {payload['status']}: "
            f"current={payload['current_pathology_download_count']} "
            f"release={payload['release_pathology_download_count']}"
        )
    return 0 if payload["status"] == "unchanged" else 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure = subparsers.add_parser("ensure", help="Download optional pathology SQLite release assets.")
    ensure.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository owner/name.")
    ensure.add_argument("--tag", default=DEFAULT_RELEASE_TAG, help="Release tag.")
    ensure.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Local SQLite output path.")
    ensure.add_argument(
        "--manifest-out", default=str(DEFAULT_MANIFEST_PATH), help="Local manifest output path."
    )

    info = subparsers.add_parser("info", help="Show local pathology metadata DB status.")
    info.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Local SQLite path.")
    info.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Local manifest path.")
    info.add_argument("--json", action="store_true", help="Emit JSON.")

    build = subparsers.add_parser("build", help="Build pathology metadata SQLite from a TCIA snapshot.")
    build.add_argument("--snapshot-db", default=str(DEFAULT_SNAPSHOT_DB), help="Source TCIA snapshot DB.")
    build.add_argument("--out", default=str(DEFAULT_DB_PATH), help="SQLite output path.")
    build.add_argument("--replace", action="store_true", help="Replace existing output DB.")
    build.add_argument("--gzip-out", help="Optional gzipped SQLite output path.")
    build.add_argument("--manifest-out", help="Optional manifest JSON output path.")

    manifest = subparsers.add_parser("manifest", help="Write a release manifest for a pathology DB.")
    manifest.add_argument("--db", required=True, help="SQLite path.")
    manifest.add_argument("--gzip", help="Gzipped SQLite path.")
    manifest.add_argument("--snapshot-db", help="Optional TCIA snapshot DB for source signature check.")
    manifest.add_argument("--out", required=True, help="Manifest JSON output path.")

    validate = subparsers.add_parser("validate", help="Validate a local pathology metadata SQLite DB.")
    validate.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path.")
    validate.add_argument("--json", action="store_true", help="Emit JSON.")

    drift = subparsers.add_parser(
        "drift-check",
        help="Compare current snapshot pathology downloads with a pathology release manifest.",
    )
    drift.add_argument("--snapshot-db", required=True, help="Current TCIA snapshot SQLite path.")
    drift.add_argument("--manifest", required=True, help="Pathology metadata manifest path.")
    drift.add_argument("--json", action="store_true", help="Emit JSON.")

    datasets = subparsers.add_parser("datasets", help="Summarize pathology metadata by dataset.")
    datasets.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path.")
    datasets.add_argument("--limit", type=int, default=100, help="Maximum rows.")
    datasets.add_argument("--json", action="store_true", help="Emit JSON.")

    downloads = subparsers.add_parser("downloads", help="List pathology Aspera download rows.")
    downloads.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path.")
    downloads.add_argument("--collection", help="Filter by TCIA short title.")
    downloads.add_argument("--noncommercial", action="store_true", help="Show only CC NonCommercial rows.")
    downloads.add_argument("--limit", type=int, default=50, help="Maximum rows.")
    downloads.add_argument("--json", action="store_true", help="Emit JSON.")

    files = subparsers.add_parser("files", help="List imported pathology package file rows.")
    files.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path.")
    files.add_argument("--collection", help="Filter by TCIA short title.")
    files.add_argument("--file-ext", help="Filter by extension, such as .svs.")
    files.add_argument("--limit", type=int, default=50, help="Maximum rows.")
    files.add_argument("--json", action="store_true", help="Emit JSON.")

    pathdb = subparsers.add_parser("pathdb", help="List PathDB crosswalk rows.")
    pathdb.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path.")
    pathdb.add_argument("--collection", help="Filter by TCIA or PathDB collection.")
    pathdb.add_argument("--limit", type=int, default=50, help="Maximum rows.")
    pathdb.add_argument("--json", action="store_true", help="Emit JSON.")

    disparities = subparsers.add_parser("disparities", help="List PathDB/package disparity rows.")
    disparities.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path.")
    disparities.add_argument("--collection", help="Filter by TCIA or PathDB collection.")
    disparities.add_argument("--type", help="Filter by disparity_type.")
    disparities.add_argument("--limit", type=int, default=50, help="Maximum rows.")
    disparities.add_argument("--json", action="store_true", help="Emit JSON.")

    args = parser.parse_args(argv)

    if args.command == "ensure":
        result = ensure_release_pathology(
            args.repo, args.tag, Path(args.db), Path(args.manifest_out)
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "info":
        return command_info(args)
    if args.command == "build":
        payload = build_pathology_db(Path(args.snapshot_db), Path(args.out), args.replace)
        if args.gzip_out:
            gzip_file(Path(args.out), Path(args.gzip_out))
            payload["gzip"] = str(Path(args.gzip_out).resolve())
        if args.manifest_out:
            manifest_payload = build_manifest(
                Path(args.out),
                Path(args.gzip_out) if args.gzip_out else None,
                Path(args.snapshot_db),
            )
            out = Path(args.manifest_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            payload["manifest"] = str(out.resolve())
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
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
    if args.command == "downloads":
        return command_downloads(args)
    if args.command == "files":
        return command_files(args)
    if args.command == "pathdb":
        return command_pathdb(args)
    if args.command == "disparities":
        return command_disparities(args)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, sqlite3.Error, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
