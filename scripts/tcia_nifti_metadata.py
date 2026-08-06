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


SCHEMA_VERSION = 4
DEFAULT_REPO = "kirbyju/tcia-query-skill"
DEFAULT_RELEASE_TAG = "tcia-snapshot-latest"
NIFTI_ASSET = "nifti_metadata.sqlite.gz"
NIFTI_MANIFEST_ASSET = "nifti_metadata_manifest.json"
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = SKILL_ROOT / "cache" / "nifti_metadata.sqlite"
DEFAULT_MANIFEST_PATH = SKILL_ROOT / "cache" / NIFTI_MANIFEST_ASSET
USER_AGENT = "tcia-nifti-metadata/1.0"

REQUIRED_TABLES = [
    "harvest_meta",
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
    "nifti_classification_rules",
    "nifti_file_characteristics",
    "nifti_dataset_review_issues",
    "annotation_groups",
]

REQUIRED_VIEWS = [
    "agent_nifti_downloads",
    "agent_nifti_dataset_summary",
    "agent_nifti_files",
    "agent_nifti_derived_objects",
    "agent_nifti_characteristics",
    "agent_nifti_characteristics_summary",
    "agent_nifti_review_issues",
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
    required = REQUIRED_TABLES + REQUIRED_VIEWS
    missing = [table for table in required if table not in tables]
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    counts = table_counts(conn)
    semantic_errors: list[str] = []
    ct_org_files = conn.execute(
        "SELECT COUNT(*) FROM radiology_series WHERE short_title = 'CT-ORG'"
    ).fetchone()[0]
    if ct_org_files and {
        "nifti_file_characteristics",
        "agent_nifti_characteristics_summary",
    }.issubset(tables):
        ct_org_characteristics = conn.execute(
            """
            SELECT COUNT(*)
            FROM nifti_file_characteristics c
            JOIN nifti_classification_rules rule USING (classification_rule_id)
            WHERE rule.short_title = 'CT-ORG'
            """
        ).fetchone()[0]
        if ct_org_characteristics != ct_org_files:
            semantic_errors.append(
                f"CT-ORG characteristics coverage is {ct_org_characteristics}/{ct_org_files}"
            )
        summary = conn.execute(
            "SELECT * FROM agent_nifti_characteristics_summary WHERE short_title = 'CT-ORG'"
        ).fetchone()
        ct_org_subjects = conn.execute(
            """
            SELECT COUNT(DISTINCT NULLIF(subject_id, ''))
            FROM radiology_series
            WHERE short_title = 'CT-ORG'
            """
        ).fetchone()[0]
        if not summary or (
            summary["ct_source_image_files"],
            summary["ct_associated_segmentations"],
            summary["study_ids"],
        ) != (ct_org_subjects, ct_org_subjects, ct_org_subjects):
            semantic_errors.append(
                "CT-ORG expected one CT image, one segmentation, and one canonical study per subject"
            )
    bcbm_files = conn.execute(
        "SELECT COUNT(*) FROM radiology_series WHERE short_title = 'BCBM-RadioGenomics'"
    ).fetchone()[0]
    if bcbm_files and {
        "nifti_file_characteristics",
        "agent_nifti_characteristics_summary",
    }.issubset(tables):
        bcbm_characteristics = conn.execute(
            """
            SELECT COUNT(*)
            FROM nifti_file_characteristics c
            JOIN nifti_classification_rules rule USING (classification_rule_id)
            WHERE rule.short_title = 'BCBM-RadioGenomics'
            """
        ).fetchone()[0]
        if bcbm_characteristics != bcbm_files:
            semantic_errors.append(
                "BCBM-RadioGenomics characteristics coverage is "
                f"{bcbm_characteristics}/{bcbm_files}"
            )
        expected = conn.execute(
            """
            SELECT
              SUM(CASE WHEN is_derived_object = 0 THEN 1 ELSE 0 END) AS source_images,
              SUM(CASE WHEN object_type = 'segmentation' THEN 1 ELSE 0 END) AS segmentations,
              COUNT(DISTINCT NULLIF(study_id, '')) AS studies
            FROM radiology_series
            WHERE short_title = 'BCBM-RadioGenomics'
            """
        ).fetchone()
        summary = conn.execute(
            """
            SELECT * FROM agent_nifti_characteristics_summary
            WHERE short_title = 'BCBM-RadioGenomics'
            """
        ).fetchone()
        unlinked_segmentations = conn.execute(
            """
            SELECT COUNT(*)
            FROM agent_nifti_characteristics
            WHERE short_title = 'BCBM-RadioGenomics'
              AND object_role = 'segmentation'
              AND source_nifti_volume_id IS NULL
            """
        ).fetchone()[0]
        if not summary or (
            summary["mr_source_image_files"],
            summary["mr_associated_segmentations"],
            summary["study_ids"],
        ) != (expected["source_images"], expected["segmentations"], expected["studies"]):
            semantic_errors.append(
                "BCBM-RadioGenomics reviewed MR image, segmentation, or study counts do not match"
            )
        if unlinked_segmentations:
            semantic_errors.append(
                f"BCBM-RadioGenomics has {unlinked_segmentations} unlinked reviewed segmentations"
            )
    vs_title = "Vestibular-Schwannoma-MC-RC2"
    vs_files = conn.execute(
        "SELECT COUNT(*) FROM radiology_series WHERE short_title = ?", (vs_title,)
    ).fetchone()[0]
    if vs_files and {
        "nifti_file_characteristics",
        "agent_nifti_characteristics_summary",
    }.issubset(tables):
        vs_characteristics = conn.execute(
            """
            SELECT COUNT(*)
            FROM nifti_file_characteristics c
            JOIN nifti_classification_rules rule USING (classification_rule_id)
            WHERE rule.short_title = ?
            """,
            (vs_title,),
        ).fetchone()[0]
        if vs_characteristics != vs_files:
            semantic_errors.append(
                f"{vs_title} characteristics coverage is {vs_characteristics}/{vs_files}"
            )
        expected = conn.execute(
            """
            SELECT
              SUM(CASE WHEN is_derived_object = 0 THEN 1 ELSE 0 END) AS source_images,
              SUM(CASE WHEN object_type = 'segmentation' THEN 1 ELSE 0 END) AS segmentations,
              COUNT(DISTINCT NULLIF(subject_id || '|' || study_date, '|')) AS subject_dates,
              COUNT(DISTINCT NULLIF(study_id, '')) AS studies
            FROM radiology_series
            WHERE short_title = ?
            """,
            (vs_title,),
        ).fetchone()
        summary = conn.execute(
            """
            SELECT * FROM agent_nifti_characteristics_summary
            WHERE short_title = ?
            """,
            (vs_title,),
        ).fetchone()
        cross_study_links = conn.execute(
            """
            SELECT COUNT(*)
            FROM derived_object_references dor
            JOIN radiology_series derived
              ON derived.radiology_id = dor.derived_radiology_id
            JOIN radiology_series source
              ON source.radiology_id = dor.source_nifti_volume_id
            WHERE derived.short_title = ?
              AND derived.study_id <> source.study_id
            """,
            (vs_title,),
        ).fetchone()[0]
        if not summary or (
            summary["mr_source_image_files"],
            summary["mr_associated_segmentations"],
            summary["study_ids"],
        ) != (expected["source_images"], expected["segmentations"], expected["studies"]):
            semantic_errors.append(
                f"{vs_title} reviewed MR image, segmentation, or study counts do not match"
            )
        if expected["studies"] != expected["subject_dates"]:
            semantic_errors.append(
                f"{vs_title} expected one canonical study per subject/date imaging session"
            )
        if cross_study_links:
            semantic_errors.append(
                f"{vs_title} has {cross_study_links} segmentation links crossing study sessions"
            )
    nlst_title = "NLST-New-lesion-LongCT"
    nlst_package_files = conn.execute(
        """
        SELECT COUNT(*)
        FROM radiology_series r
        JOIN non_dicom_files f USING (non_dicom_file_id)
        WHERE r.short_title = ?
          AND EXISTS (
              SELECT 1 FROM json_each(COALESCE(f.inventory_sources, '[]'))
              WHERE value = 'package_files'
          )
        """,
        (nlst_title,),
    ).fetchone()[0]
    if nlst_package_files and "agent_nifti_characteristics" in tables:
        summary = conn.execute(
            "SELECT * FROM agent_nifti_characteristics_summary WHERE short_title = ?",
            (nlst_title,),
        ).fetchone()
        expected_roles = conn.execute(
            """
            SELECT
              SUM(CASE WHEN object_role = 'source_image' THEN 1 ELSE 0 END),
              SUM(CASE WHEN object_role = 'derived_image' THEN 1 ELSE 0 END),
              SUM(CASE WHEN object_role = 'fiducial_annotation' THEN 1 ELSE 0 END)
            FROM agent_nifti_characteristics
            WHERE short_title = ?
            """,
            (nlst_title,),
        ).fetchone()
        if not summary or summary["characterized_files"] != nlst_package_files:
            semantic_errors.append(
                f"{nlst_title} reviewed coverage does not match the current package inventory"
            )
        elif tuple(expected_roles) != (242, 363, 145):
            semantic_errors.append(
                f"{nlst_title} expected 242 source CT, 363 transformed CT, and 145 fiducial files"
            )
        bad_references = conn.execute(
            """
            SELECT COUNT(*)
            FROM agent_nifti_characteristics
            WHERE short_title = ?
              AND (
                (object_role = 'fiducial_annotation' AND source_reference_count <> 1)
                OR (series_description = 'Resampled CT volume' AND source_reference_count <> 1)
                OR (series_description = 'Longitudinal registered CT volume'
                    AND (source_reference_count <> 2 OR source_nifti_volume_id IS NOT NULL))
              )
            """,
            (nlst_title,),
        ).fetchone()[0]
        if bad_references:
            semantic_errors.append(
                f"{nlst_title} has {bad_references} reviewed files with unexpected source links"
            )
    rfs_title = "Radiomic-Feature-Standards"
    rfs_files = conn.execute(
        "SELECT COUNT(*) FROM radiology_series WHERE short_title = ?", (rfs_title,)
    ).fetchone()[0]
    if rfs_files and "agent_nifti_characteristics" in tables:
        summary = conn.execute(
            "SELECT * FROM agent_nifti_characteristics_summary WHERE short_title = ?",
            (rfs_title,),
        ).fetchone()
        bad_rows = conn.execute(
            """
            SELECT COUNT(*)
            FROM agent_nifti_characteristics
            WHERE short_title = ?
              AND (
                object_role <> 'segmentation'
                OR associated_imaging_modality <> 'CT'
                OR imaging_modality_relationship <> 'associated_with_source_dicom_series'
                OR source_reference_count <> 1
                OR source_dataset_short_title NOT IN ('LIDC-IDRI', 'DRO-Toolkit')
                OR COALESCE(source_dicom_series_instance_uid, '') = ''
                OR COALESCE(source_dicom_study_instance_uid, '') = ''
                OR alternate_dicom_representation_count <> 1
                OR COALESCE(alternate_dicom_seg_series_instance_uid, '') = ''
                OR alternate_dicom_seg_study_instance_uid <> source_dicom_study_instance_uid
              )
            """,
            (rfs_title,),
        ).fetchone()[0]
        if not summary or (
            summary["characterized_files"],
            summary["segmentation_files"],
            summary["ct_associated_segmentations"],
            summary["study_ids"],
        ) != (13, 13, 13, 13):
            semantic_errors.append(
                f"{rfs_title} expected 13 CT-associated NIfTI segmentations in 13 source studies"
            )
        if bad_rows:
            semantic_errors.append(
                f"{rfs_title} has {bad_rows} rows without exact CT-source and alternate-SEG provenance"
            )
    healthy_title = "Healthy-Total-Body-CTs"
    healthy_files = conn.execute(
        "SELECT COUNT(*) FROM radiology_series WHERE short_title = ?", (healthy_title,)
    ).fetchone()[0]
    if healthy_files and "agent_nifti_characteristics" in tables:
        summary = conn.execute(
            "SELECT * FROM agent_nifti_characteristics_summary WHERE short_title = ?",
            (healthy_title,),
        ).fetchone()
        provenance = conn.execute(
            """
            SELECT
              SUM(CASE WHEN COALESCE(source_dicom_series_instance_uid, '') <> '' THEN 1 ELSE 0 END)
                AS exact_sources,
              SUM(CASE WHEN COALESCE(source_dicom_series_instance_uid, '') = '' THEN 1 ELSE 0 END)
                AS unresolved_sources,
              SUM(CASE WHEN source_access_level = 'controlled' THEN 1 ELSE 0 END)
                AS controlled_sources,
              SUM(CASE WHEN source_reference_count = 1 THEN 1 ELSE 0 END)
                AS one_source_relationship
            FROM agent_nifti_characteristics
            WHERE short_title = ?
            """,
            (healthy_title,),
        ).fetchone()
        manual_flags = conn.execute(
            """
            SELECT COUNT(*)
            FROM radiology_series
            WHERE short_title = ?
              AND quality_flag_json LIKE '%unmatched_controlled_source_subject%'
            """,
            (healthy_title,),
        ).fetchone()[0]
        if not summary or (
            summary["characterized_files"],
            summary["segmentation_files"],
            summary["ct_associated_segmentations"],
            summary["study_ids"],
        ) != (30, 30, 30, 30):
            semantic_errors.append(
                f"{healthy_title} expected 30 CT-associated NIfTI segmentations in 30 studies"
            )
        if tuple(provenance) != (26, 4, 30, 30) or manual_flags != 4:
            semantic_errors.append(
                f"{healthy_title} expected 26 exact controlled CT links and 4 manual-review links"
            )
    if {"agent_nifti_characteristics", "nifti_classification_rules"}.issubset(tables):
        dataset_coverage = conn.execute(
            """
            SELECT
              (SELECT COUNT(DISTINCT short_title) FROM radiology_series) AS datasets,
              (SELECT COUNT(DISTINCT short_title) FROM nifti_classification_rules) AS reviewed_datasets,
              (SELECT COUNT(*) FROM nifti_classification_rules) AS rules
            """
        ).fetchone()
        expected_characteristics = conn.execute(
            """
            SELECT COUNT(*)
            FROM radiology_series r
            JOIN non_dicom_files f USING (non_dicom_file_id)
            WHERE r.short_title <> 'NLST-New-lesion-LongCT'
               OR EXISTS (
                   SELECT 1 FROM json_each(COALESCE(f.inventory_sources, '[]'))
                   WHERE value = 'package_files'
               )
            """
        ).fetchone()[0]
        actual_characteristics = conn.execute(
            "SELECT COUNT(*) FROM nifti_file_characteristics"
        ).fetchone()[0]
        missing_reviewed_datasets = [
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT r.short_title
                FROM radiology_series r
                LEFT JOIN nifti_classification_rules rule ON rule.short_title = r.short_title
                WHERE rule.short_title IS NULL
                ORDER BY lower(r.short_title)
                """
            )
        ]
        invalid_characteristics = conn.execute(
            """
            SELECT COUNT(*)
            FROM nifti_file_characteristics
            WHERE COALESCE(object_role, '') = ''
               OR COALESCE(associated_imaging_modality, '') = ''
               OR associated_imaging_modality IN ('SEG', 'RTSTRUCT', 'RTDOSE', 'NIfTI')
            """
        ).fetchone()[0]
        if dataset_coverage and (
            dataset_coverage["datasets"] != dataset_coverage["reviewed_datasets"]
            or dataset_coverage["reviewed_datasets"] != dataset_coverage["rules"]
            or missing_reviewed_datasets
        ):
            semantic_errors.append(
                "reviewed characteristics do not have exactly one rule for every NIfTI dataset: "
                + ", ".join(missing_reviewed_datasets)
            )
        if actual_characteristics != expected_characteristics:
            semantic_errors.append(
                f"reviewed characteristics coverage is {actual_characteristics}/{expected_characteristics} current files"
            )
        if invalid_characteristics:
            semantic_errors.append(
                f"reviewed characteristics contain {invalid_characteristics} blank or DICOM/file-format modalities"
            )
    if "nifti_dataset_review_issues" in tables:
        invalid_issues = conn.execute(
            """
            SELECT COUNT(*) FROM nifti_dataset_review_issues
            WHERE COALESCE(short_title, '') = ''
               OR COALESCE(issue_code, '') = ''
               OR status NOT IN ('manual_review', 'accepted', 'resolved')
               OR affected_files < 0
            """
        ).fetchone()[0]
        if invalid_issues:
            semantic_errors.append(f"nifti review-issues table contains {invalid_issues} invalid rows")
    conn.close()
    return {
        "integrity_check": integrity,
        "missing_tables": missing,
        "semantic_errors": semantic_errors,
        "table_counts": counts,
    }


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
        "source_snapshot_matches_harvest": manifest.get("source_snapshot_matches_harvest"),
        "schema_version": meta.get("schema_version") or manifest.get("schema_version", ""),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"DB: {payload['db']}")
        print(f"Manifest: {payload['manifest']}")
        if payload["schema_version"]:
            print(f"Schema version: {payload['schema_version']}")
        if payload["release_fingerprint"]:
            print(f"Release fingerprint: {payload['release_fingerprint']}")
        if payload["source_snapshot_matches_harvest"] is not None:
            print(
                "Source snapshot compatibility: "
                f"{'yes' if payload['source_snapshot_matches_harvest'] else 'no'}"
            )
        for key in ("radiology_series", "derived_objects", "derived_object_references"):
            print(f"{key}: {counts.get(key, 0)}")
    return 0


def command_datasets(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    sql = """
        SELECT
          short_title,
          nifti_downloads,
          nifti_files,
          radiology_series_rows,
          mr_files,
          ct_files,
          derived_objects,
          linked_derived_objects
        FROM agent_nifti_dataset_summary
        ORDER BY lower(short_title)
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
                "nifti_downloads",
                "nifti_files",
                "radiology_series_rows",
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
               r.series_id, r.series_instance_uid, r.is_derived_object, r.package_path
        FROM agent_nifti_files r
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
                   d.segmentation_representation, dor.source_nifti_volume_file_name,
                   dor.source_dicom_series_instance_uid,
                   dor.confidence, dor.inference_method
            FROM derived_objects d
            LEFT JOIN derived_object_references dor
              ON dor.derived_object_id = d.derived_object_id
            WHERE {' AND '.join(where)}
            ORDER BY lower(d.short_title), d.file_name, dor.source_nifti_volume_file_name
            LIMIT ?
        """
        columns = [
            "short_title",
            "derived_file",
            "segmentation_representation",
            "source_nifti_volume_file_name",
            "source_dicom_series_instance_uid",
            "confidence",
            "inference_method",
        ]
    else:
        sql = f"""
            SELECT d.short_title, d.file_name, d.derived_object_type,
                   d.segmentation_representation, d.source_nifti_volume_id,
                   d.source_dicom_series_instance_uid
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
            "source_nifti_volume_id",
            "source_dicom_series_instance_uid",
        ]
    rows = rows_as_dicts(conn, sql, tuple(params))
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(rows, columns)
    return 0


def command_characteristics(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    where = ["1 = 1"]
    params: list[Any] = []
    if args.collection:
        where.append("short_title = ?")
        params.append(args.collection)
    if args.modality:
        where.append("associated_imaging_modality = ?")
        params.append(args.modality)
    if args.role:
        where.append("object_role = ?")
        params.append(args.role)
    params.append(args.limit)
    rows = rows_as_dicts(
        conn,
        f"""
        SELECT short_title, subject_id, file_name, object_role,
               associated_imaging_modality, imaging_modality_relationship,
               study_id, study_id_source, study_date, series_description,
               file_metadata_sources,
               segmentation_representation,
               source_nifti_volume_id, source_nifti_volume_file_name,
               source_dataset_short_title, source_access_level,
               source_dicom_series_instance_uid, source_dicom_study_instance_uid,
               source_reference_count,
               alternate_dicom_seg_series_instance_uid,
               alternate_dicom_seg_study_instance_uid,
               alternate_dicom_representation_count,
               classification_source,
               classification_confidence
        FROM agent_nifti_characteristics
        WHERE {' AND '.join(where)}
        ORDER BY lower(short_title), subject_id, object_role DESC, file_name
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
                "subject_id",
                "file_name",
                "object_role",
                "associated_imaging_modality",
                "study_id",
                "study_date",
                "series_description",
                "source_nifti_volume_file_name",
                "source_dicom_series_instance_uid",
                "classification_confidence",
            ],
        )
    return 0


def command_drift_check(args: argparse.Namespace) -> int:
    snapshot_conn = sqlite3.connect(args.snapshot_db)
    snapshot_conn.row_factory = sqlite3.Row
    current_rows = nifti_download_rows_from_snapshot(snapshot_conn)
    snapshot_conn.close()
    current_signature = download_signature(current_rows)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    release_signature = manifest.get("nifti_download_signature")
    release_schema_version = manifest.get("schema_version")
    signature_changed = current_signature != release_signature
    schema_changed = release_schema_version != SCHEMA_VERSION
    payload = {
        "status": "unchanged" if not signature_changed and not schema_changed else "changed",
        "current_nifti_download_count": len(current_rows),
        "release_nifti_download_count": manifest.get("nifti_download_count"),
        "current_nifti_download_signature": current_signature,
        "release_nifti_download_signature": release_signature,
        "current_schema_version": SCHEMA_VERSION,
        "release_schema_version": release_schema_version,
        "signature_changed": signature_changed,
        "schema_changed": schema_changed,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"NIfTI download signature {payload['status']}: "
            f"current={payload['current_nifti_download_count']} "
            f"release={payload['release_nifti_download_count']} "
            f"schema={payload['release_schema_version']}->{payload['current_schema_version']}"
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

    characteristics = subparsers.add_parser(
        "characteristics", help="List reviewed query-facing NIfTI file characteristics."
    )
    characteristics.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path.")
    characteristics.add_argument("--collection", help="Filter by TCIA short title.")
    characteristics.add_argument("--modality", help="Filter by associated imaging modality.")
    characteristics.add_argument(
        "--role", choices=("source_image", "segmentation"), help="Filter by object role."
    )
    characteristics.add_argument("--limit", type=int, default=20, help="Maximum rows.")
    characteristics.add_argument("--json", action="store_true", help="Emit JSON.")

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
            print(f"semantic_errors: {', '.join(payload['semantic_errors']) or 'none'}")
        return (
            0
            if payload["integrity_check"] == "ok"
            and not payload["missing_tables"]
            and not payload["semantic_errors"]
            else 1
        )
    if args.command == "drift-check":
        return command_drift_check(args)
    if args.command == "datasets":
        return command_datasets(args)
    if args.command == "files":
        return command_files(args)
    if args.command == "derived":
        return command_derived(args)
    if args.command == "characteristics":
        return command_characteristics(args)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, sqlite3.Error, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
