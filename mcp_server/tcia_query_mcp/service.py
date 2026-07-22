"""Read-only TCIA snapshot query service for the MCP server.

The service intentionally exposes typed operations over the documented agent
views instead of accepting arbitrary SQL from clients.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any


CONTROLLED_ACCESS_POLICY_URL = (
    "https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/"
)
ANNOTATION_LABELS = {
    "annotation",
    "annotations",
    "image annotation",
    "image annotations",
    "rtstruct",
    "seg",
    "sr",
    "segmentation",
    "segmentations",
    "classification",
    "classifications",
    "measurement",
    "measurements",
    "fiducial",
    "fiducials",
    "label",
    "labels",
    "radiotherapy structure set",
}
DEFAULT_LIMIT = 25
MAX_LIMIT = 200


class TciaServiceError(RuntimeError):
    """Base exception for expected TCIA MCP service failures."""


class SnapshotNotFoundError(TciaServiceError):
    """Raised when a configured SQLite snapshot path is unavailable."""


def default_skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def coerce_limit(value: Any, default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    if limit < 1:
        return default
    return min(limit, maximum)


def is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def parse_json_array(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return [item.strip() for item in value.split(";") if item.strip()]
        if isinstance(loaded, list):
            return loaded
        if loaded in (None, False, ""):
            return []
        return [loaded]
    return [value]


def normalize_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = dict(row)
    for key in (
        "download_types",
        "data_types",
        "file_types",
        "external_resources",
        "external_resource_labels",
        "controlled_download_titles",
        "controlled_license_labels",
        "controlled_download_ids",
        "controlled_download_urls",
        "version_downloads",
    ):
        if key in output:
            output[key] = parse_json_array(output[key])
    for key in (
        "hidden",
        "controlled_access",
        "noncommercial_license",
        "has_tcia_clinical_download",
        "has_external_clinical_resource",
    ):
        if key in output and output[key] in (0, 1):
            output[key] = bool(output[key])
    return output


def compact_dataset(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = normalize_row(row)
    return {
        "short_title": data.get("short_title"),
        "title": data.get("title"),
        "dataset_type": data.get("dataset_type"),
        "doi": data.get("doi"),
        "tcia_page": data.get("link"),
        "date_updated": data.get("date_updated"),
        "current_version_number": data.get("current_version_number"),
        "hidden": data.get("hidden"),
        "license_status": data.get("license_status"),
        "licenses": data.get("licenses"),
        "access_level": data.get("access_level"),
        "resolved_access_level": data.get("resolved_access_level", data.get("access_level")),
        "controlled_access": data.get("controlled_access"),
        "noncommercial_license": data.get("noncommercial_license"),
        "controlled_download_count": data.get("controlled_download_count"),
        "noncontrolled_download_count": data.get("noncontrolled_download_count"),
        "current_download_count": data.get("current_download_count"),
        "subjects": data.get("subjects"),
        "data_types": data.get("data_types"),
        "download_types": data.get("download_types"),
        "download_data_types": data.get("download_data_types"),
        "download_file_types": data.get("download_file_types"),
        "external_resources": data.get("external_resources"),
        "external_resource_labels": data.get("external_resource_labels"),
        "has_tcia_clinical_download": data.get("has_tcia_clinical_download"),
        "has_external_clinical_resource": data.get("has_external_clinical_resource"),
        "cancer_types": data.get("cancer_types"),
        "cancer_locations": data.get("cancer_locations"),
        "species": data.get("species"),
        "program": data.get("program"),
        "source_collections": data.get("source_collections"),
        "summary": data.get("summary"),
        "controlled_access_policy_url": data.get("resolved_controlled_access_policy_url")
        or data.get("controlled_access_policy_url"),
    }


def compact_dataset_version(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = normalize_row(row)
    return {
        "short_title": data.get("short_title"),
        "title": data.get("title"),
        "dataset_type": data.get("dataset_type"),
        "doi": data.get("doi"),
        "tcia_page": data.get("link"),
        "date_updated": data.get("date_updated"),
        "current_version_number": data.get("current_version_number"),
        "subjects": data.get("subjects"),
        "hidden": data.get("hidden"),
        "version_id": data.get("version_id"),
        "version_slug": data.get("version_slug"),
        "version_post_title": data.get("version_post_title"),
        "version_number": data.get("version_number"),
        "version_date": data.get("version_date"),
        "version_related_short_title": data.get("version_related_short_title"),
        "match_method": data.get("match_method"),
        "version_downloads": data.get("version_downloads"),
        "version_text": data.get("version_text"),
    }


def compact_v1_release(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = normalize_row(row)
    return {
        "short_title": data.get("short_title"),
        "title": data.get("title"),
        "dataset_type": data.get("dataset_type"),
        "doi": data.get("doi"),
        "tcia_page": data.get("link"),
        "date_updated": data.get("date_updated"),
        "current_version_number": data.get("current_version_number"),
        "subjects": data.get("subjects"),
        "hidden": data.get("hidden"),
        "v1_release_date": data.get("v1_release_date"),
        "v1_release_date_source": data.get("v1_release_date_source"),
        "version_id": data.get("version_id"),
        "version_slug": data.get("version_slug"),
        "version_post_title": data.get("version_post_title"),
        "version_related_short_title": data.get("version_related_short_title"),
        "match_method": data.get("match_method"),
    }


def compact_download(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = normalize_row(row)
    return {
        "short_title": data.get("short_title"),
        "dataset_title": data.get("title"),
        "dataset_type": data.get("dataset_type"),
        "download_id": data.get("download_id"),
        "download_title": data.get("download_title") or data.get("download_label"),
        "download_url": data.get("download_url"),
        "search_url": data.get("search_url"),
        "download_types": data.get("download_types"),
        "data_types": data.get("data_types"),
        "file_types": data.get("file_types"),
        "external_resources": data.get("external_resources"),
        "access_level": data.get("access_level"),
        "controlled_access": data.get("controlled_access"),
        "noncommercial_license": data.get("noncommercial_license"),
        "license_label": data.get("license_label"),
        "license_url": data.get("license_url"),
        "requirements_label": data.get("requirements_label"),
        "requirements_text": data.get("requirements_text"),
        "download_size": data.get("download_size"),
        "download_size_unit": data.get("download_size_unit"),
        "subjects": data.get("subjects"),
        "studies": data.get("studies"),
        "series": data.get("series"),
        "images": data.get("images"),
        "controlled_access_policy_url": data.get("controlled_access_policy_url"),
        "route_system": data.get("route_system"),
        "route_manifest_kind": data.get("route_manifest_kind"),
        "metadata_artifact_kind": data.get("metadata_artifact_kind"),
    }


def compact_controlled_file(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = normalize_row(row)
    return {
        "short_title": data.get("short_title"),
        "dataset_type": data.get("dataset_type"),
        "title": data.get("title"),
        "doi": data.get("doi"),
        "route_system": data.get("route_system"),
        "download_id": data.get("download_id"),
        "download_title": data.get("download_title") or data.get("download_label"),
        "access_level": data.get("access_level"),
        "controlled_access_policy_url": data.get("controlled_access_policy_url"),
        "license_label": data.get("license_label"),
        "drs_uri": data.get("drs_uri"),
        "file_id": data.get("file_id"),
        "file_name": data.get("file_name"),
        "file_type": data.get("file_type"),
        "file_format": data.get("file_format"),
        "file_size_bytes": data.get("file_size_bytes"),
        "study_name": data.get("study_name"),
        "study_accession": data.get("study_accession"),
        "participant_id": data.get("participant_id"),
        "patient_id": data.get("patient_id"),
        "patient_sex": data.get("patient_sex"),
        "diagnosis": data.get("diagnosis"),
        "image_modality": data.get("image_modality"),
        "modality": data.get("modality"),
        "body_part_examined": data.get("body_part_examined"),
        "study_instance_uid": data.get("study_instance_uid"),
        "series_instance_uid": data.get("series_instance_uid"),
        "series_description": data.get("series_description"),
        "manufacturer": data.get("manufacturer"),
        "source_manifest_url": data.get("source_manifest_url"),
        "source_metadata_url": data.get("source_metadata_url"),
    }


def compact_nifti_summary(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = normalize_row(row)
    return {
        "short_title": data.get("short_title"),
        "title": data.get("title"),
        "dataset_type": data.get("dataset_type"),
        "nifti_downloads": data.get("nifti_downloads"),
        "nifti_files": data.get("nifti_files"),
        "non_dicom_files": data.get("non_dicom_files"),
        "sidecar_files": data.get("sidecar_files"),
        "package_metadata_files": data.get("package_metadata_files"),
        "radiology_series_rows": data.get("radiology_series_rows"),
        "mr_files": data.get("mr_files"),
        "ct_files": data.get("ct_files"),
        "derived_radiology_rows": data.get("derived_radiology_rows"),
        "derived_objects": data.get("derived_objects"),
        "linked_derived_objects": data.get("linked_derived_objects"),
        "subject_ids": data.get("subject_ids"),
        "study_ids": data.get("study_ids"),
        "series_ids": data.get("series_ids"),
        "download_ids": data.get("download_ids"),
        "download_labels": data.get("download_labels"),
        "package_file_rows": data.get("package_file_rows"),
        "aspera_root_sums_rows": data.get("aspera_root_sums_rows"),
    }


def compact_nifti_file(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = normalize_row(row)
    return {
        "short_title": data.get("short_title"),
        "dataset_type": data.get("dataset_type"),
        "download_id": data.get("download_id") or data.get("download_ids"),
        "file_name": data.get("file_name"),
        "package_path": data.get("package_path"),
        "subject_id": data.get("subject_id") or data.get("PatientID"),
        "procedure_id": data.get("procedure_id"),
        "study_id": data.get("study_id"),
        "study_instance_uid": data.get("study_instance_uid") or data.get("StudyInstanceUID"),
        "study_id_source": data.get("study_id_source"),
        "series_id": data.get("series_id"),
        "series_instance_uid": data.get("series_instance_uid") or data.get("SeriesInstanceUID"),
        "series_id_source": data.get("series_id_source"),
        "source_doi": data.get("source_doi") or data.get("source_DOI"),
        "modality": data.get("modality") or data.get("Modality"),
        "body_part_examined": data.get("body_part_examined") or data.get("BodyPartExamined"),
        "study_date": data.get("study_date") or data.get("StudyDate"),
        "series_date": data.get("series_date") or data.get("SeriesDate"),
        "study_description": data.get("study_description") or data.get("StudyDescription"),
        "series_description": data.get("series_description") or data.get("SeriesDescription"),
        "manufacturer": data.get("manufacturer") or data.get("Manufacturer"),
        "manufacturer_model_name": data.get("manufacturer_model_name") or data.get("ManufacturerModelName"),
        "rows": data.get("rows") or data.get("Rows"),
        "columns": data.get("columns") or data.get("Columns"),
        "number_of_slices": data.get("number_of_slices"),
        "pixel_spacing_row_mm": data.get("pixel_spacing_row_mm") or data.get("PixelSpacing_row_mm"),
        "pixel_spacing_col_mm": data.get("pixel_spacing_col_mm") or data.get("PixelSpacing_col_mm"),
        "slice_thickness_mm": data.get("slice_thickness_mm") or data.get("SliceThickness"),
        "object_type": data.get("object_type"),
        "is_derived_object": data.get("is_derived_object"),
        "quality_flag_json": data.get("quality_flag_json"),
    }


def compact_nifti_derived(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = normalize_row(row)
    return {
        "short_title": data.get("short_title"),
        "dataset_type": data.get("dataset_type"),
        "derived_object_id": data.get("derived_object_id"),
        "file_name": data.get("file_name"),
        "package_path": data.get("package_path"),
        "file_ext": data.get("file_ext"),
        "derived_object_type": data.get("derived_object_type"),
        "segmentation_representation": data.get("segmentation_representation"),
        "segmentation_type": data.get("segmentation_type"),
        "total_segments": data.get("total_segments"),
        "algorithm_type": data.get("algorithm_type"),
        "algorithm_name": data.get("algorithm_name"),
        "referenced_series_id": data.get("referenced_series_id"),
        "referenced_non_dicom_file_id": data.get("referenced_non_dicom_file_id"),
        "referenced_file_name": data.get("referenced_file_name"),
        "referenced_package_path": data.get("referenced_package_path"),
        "reference_role": data.get("reference_role"),
        "inference_method": data.get("inference_method"),
        "confidence": data.get("confidence"),
        "evidence_json": data.get("evidence_json"),
    }


def compact_package_file(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = normalize_row(row)
    return {
        "short_title": data.get("short_title"),
        "dataset_type": data.get("dataset_type"),
        "download_id": data.get("download_id"),
        "download_title": data.get("download_title") or data.get("download_label"),
        "source_url": data.get("source_url"),
        "package_path": data.get("package_path"),
        "file_name": data.get("file_name"),
        "file_ext": data.get("file_ext"),
        "file_role": data.get("file_role"),
        "bytes": data.get("bytes"),
        "checksum": data.get("checksum"),
        "checksum_algorithm": data.get("checksum_algorithm"),
        "inventory_source": data.get("inventory_source"),
        "inventory_status": data.get("inventory_status"),
        "modified_time": data.get("modified_time"),
    }


def compact_pathology_summary(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = normalize_row(row)
    return {
        "short_title": data.get("short_title"),
        "dataset_type": data.get("dataset_type"),
        "download_records": data.get("download_records"),
        "downloads_with_pathdb_collection": data.get("downloads_with_pathdb_collection"),
        "pathdb_collection_slide_count": data.get("pathdb_collection_slide_count"),
        "pathdb_collection_patient_count": data.get("pathdb_collection_patient_count"),
        "open_noncommercial_downloads": data.get("open_noncommercial_downloads"),
        "package_inventory_status": data.get("package_inventory_status"),
        "package_file_rows": data.get("package_file_rows"),
        "file_object_rows": data.get("file_object_rows"),
        "download_ids": data.get("download_ids"),
        "download_titles": data.get("download_titles"),
    }


def compact_pathology_file_object(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = normalize_row(row)
    return {
        "short_title": data.get("short_title"),
        "dataset_type": data.get("dataset_type"),
        "download_id": data.get("download_id"),
        "file_name": data.get("file_name"),
        "file_ext": data.get("file_ext"),
        "package_path": data.get("package_path"),
        "file_role": data.get("file_role"),
        "bytes": data.get("bytes"),
        "checksum": data.get("checksum"),
        "checksum_algorithm": data.get("checksum_algorithm"),
        "object_modality": data.get("object_modality"),
        "image_format": data.get("image_format"),
        "is_wsi": data.get("is_wsi"),
        "is_micrograph": data.get("is_micrograph"),
        "is_codex": data.get("is_codex"),
        "is_metadata": data.get("is_metadata"),
        "source_table": data.get("source_table"),
        "source_row_id": data.get("source_row_id"),
    }


def contains_all_label_values(values: list[Any], requested: list[str]) -> bool:
    if not requested:
        return True
    lowered = {str(value).strip().lower() for value in values if str(value).strip()}
    return all(item.strip().lower() in lowered for item in requested)


def json_text_values(row: dict[str, Any], keys: list[str]) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = row.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value not in (None, ""):
            values.append(str(value))
    return values


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class TciaQueryService:
    """Snapshot-backed TCIA query facade."""

    def __init__(
        self,
        snapshot_db: str | os.PathLike[str] | None = None,
        controlled_db: str | os.PathLike[str] | None = None,
        nifti_db: str | os.PathLike[str] | None = None,
        pathology_db: str | os.PathLike[str] | None = None,
        skill_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.skill_root = Path(skill_root) if skill_root else default_skill_root()
        self.snapshot_db = Path(
            snapshot_db
            or os.environ.get("TCIA_SNAPSHOT_DB", "")
            or self.skill_root / "cache" / "tcia_snapshot.sqlite"
        )
        self.controlled_db = Path(
            controlled_db
            or os.environ.get("TCIA_CONTROLLED_ACCESS_METADATA_DB", "")
            or self.skill_root / "cache" / "controlled_access_metadata.sqlite"
        )
        self.nifti_db = Path(
            nifti_db
            or os.environ.get("TCIA_NIFTI_METADATA_DB", "")
            or self.skill_root / "cache" / "nifti_metadata.sqlite"
        )
        self.pathology_db = Path(
            pathology_db
            or os.environ.get("TCIA_PATHOLOGY_METADATA_DB", "")
            or self.skill_root / "cache" / "pathology_metadata.sqlite"
        )

    def _connect_snapshot(self) -> sqlite3.Connection:
        if not self.snapshot_db.exists():
            raise SnapshotNotFoundError(
                f"TCIA snapshot not found at {self.snapshot_db}. "
                "Run `python scripts/tcia_snapshot.py ensure` from the skill root."
            )
        conn = sqlite3.connect(self.snapshot_db)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_controlled(self) -> sqlite3.Connection:
        if not self.controlled_db.exists():
            raise SnapshotNotFoundError(
                f"Controlled-access metadata snapshot not found at {self.controlled_db}. "
                "Run `python scripts/tcia_controlled_access_metadata.py ensure` from the skill root."
            )
        conn = sqlite3.connect(self.controlled_db)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_nifti(self) -> sqlite3.Connection:
        if not self.nifti_db.exists():
            raise SnapshotNotFoundError(
                f"NIfTI metadata snapshot not found at {self.nifti_db}. "
                "Run `python scripts/tcia_nifti_metadata.py ensure` from the skill root."
            )
        conn = sqlite3.connect(self.nifti_db)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_pathology(self) -> sqlite3.Connection:
        if not self.pathology_db.exists():
            raise SnapshotNotFoundError(
                f"Pathology metadata snapshot not found at {self.pathology_db}. "
                "Run `python scripts/tcia_pathology_metadata.py ensure` from the skill root."
            )
        conn = sqlite3.connect(self.pathology_db)
        conn.row_factory = sqlite3.Row
        return conn

    def _object_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
            (name,),
        ).fetchone()
        return row is not None

    def _count_object(self, conn: sqlite3.Connection, name: str) -> int | None:
        if not self._object_exists(conn, name):
            return None
        return int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])

    def _columns(self, conn: sqlite3.Connection, name: str) -> set[str]:
        if not self._object_exists(conn, name):
            return set()
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({name})")}

    def _coalesce_expr(self, columns: set[str], preferred: list[str], default: str = "''") -> str:
        available = [column for column in preferred if column in columns]
        if not available:
            return default
        return "COALESCE(" + ", ".join(available + [default]) + ")"

    def _read_meta_table(self, conn: sqlite3.Connection, table: str) -> dict[str, Any]:
        if not self._object_exists(conn, table):
            return {}
        output: dict[str, Any] = {}
        for row in conn.execute(f"SELECT key, value FROM {table}"):
            try:
                output[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                output[row["key"]] = row["value"]
        return output

    def snapshot_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "snapshot_db": str(self.snapshot_db),
            "snapshot_exists": self.snapshot_db.exists(),
            "controlled_access_db": str(self.controlled_db),
            "controlled_access_db_exists": self.controlled_db.exists(),
            "nifti_metadata_db": str(self.nifti_db),
            "nifti_metadata_db_exists": self.nifti_db.exists(),
            "pathology_metadata_db": str(self.pathology_db),
            "pathology_metadata_db_exists": self.pathology_db.exists(),
            "controlled_access_policy_url": CONTROLLED_ACCESS_POLICY_URL,
        }
        if self.snapshot_db.exists():
            with self._connect_snapshot() as conn:
                meta = {}
                for row in conn.execute("SELECT key, value FROM snapshot_meta"):
                    try:
                        meta[row["key"]] = json.loads(row["value"])
                    except json.JSONDecodeError:
                        meta[row["key"]] = row["value"]
                info["snapshot_meta"] = meta
                info["counts"] = {
                    "visible_datasets": conn.execute(
                        "SELECT COUNT(*) FROM agent_datasets WHERE hidden = 0"
                    ).fetchone()[0],
                    "visible_current_downloads": conn.execute(
                        "SELECT COUNT(*) FROM agent_current_downloads WHERE hidden = 0"
                    ).fetchone()[0],
                    "visible_mixed_datasets": conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM agent_dataset_access_summary
                        WHERE hidden = 0 AND resolved_access_level = 'mixed'
                        """
                    ).fetchone()[0],
                    "visible_controlled_or_mixed_datasets": conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM agent_dataset_access_summary
                        WHERE hidden = 0
                          AND resolved_access_level IN ('controlled', 'mixed')
                        """
                    ).fetchone()[0],
                }
                info["counts"]["dataset_version_rows"] = self._count_object(
                    conn, "agent_dataset_versions"
                )
                info["counts"]["dataset_v1_release_rows"] = self._count_object(
                    conn, "agent_dataset_v1_releases"
                )
                info["capabilities"] = {
                    "release_history": self._object_exists(conn, "agent_dataset_versions")
                    and self._object_exists(conn, "agent_dataset_v1_releases"),
                    "external_resource_labels": "external_resource_labels"
                    in self._columns(conn, "agent_dataset_access_summary"),
                }
        if self.controlled_db.exists():
            with self._connect_controlled() as conn:
                info["controlled_access_counts"] = {
                    "datasets": conn.execute(
                        "SELECT COUNT(*) FROM agent_controlled_dataset_summary"
                    ).fetchone()[0],
                    "file_rows": conn.execute(
                        "SELECT COUNT(*) FROM agent_controlled_files"
                    ).fetchone()[0],
                }
        if self.nifti_db.exists():
            with self._connect_nifti() as conn:
                info["nifti_metadata"] = self._read_meta_table(conn, "harvest_meta")
                info["nifti_counts"] = {
                    "datasets": self._count_object(conn, "agent_nifti_dataset_summary")
                    if self._object_exists(conn, "agent_nifti_dataset_summary")
                    else conn.execute("SELECT COUNT(DISTINCT short_title) FROM nifti_downloads").fetchone()[0],
                    "downloads": self._count_object(conn, "nifti_downloads"),
                    "radiology_series_rows": self._count_object(conn, "radiology_series"),
                    "derived_objects": self._count_object(conn, "derived_objects"),
                    "non_dicom_files": self._count_object(conn, "non_dicom_files"),
                    "package_files": self._count_object(conn, "package_files"),
                    "aspera_root_sums_inventory": self._count_object(conn, "aspera_root_sums_inventory"),
                    "has_agent_views": self._object_exists(conn, "agent_nifti_dataset_summary"),
                }
        if self.pathology_db.exists():
            with self._connect_pathology() as conn:
                info["pathology_metadata"] = self._read_meta_table(conn, "pathology_meta")
                info["pathology_counts"] = {
                    "datasets": self._count_object(conn, "agent_pathology_dataset_summary")
                    if self._object_exists(conn, "agent_pathology_dataset_summary")
                    else conn.execute("SELECT COUNT(DISTINCT short_title) FROM pathology_downloads").fetchone()[0],
                    "downloads": self._count_object(conn, "pathology_downloads"),
                    "package_files": self._count_object(conn, "pathology_package_files"),
                    "file_objects": self._count_object(conn, "pathology_file_objects"),
                    "pathdb_slide_crosswalk": self._count_object(conn, "pathdb_slide_crosswalk"),
                    "disparities": self._count_object(conn, "pathology_disparities"),
                    "has_agent_views": self._object_exists(conn, "agent_pathology_dataset_summary"),
                }
        return info

    def search_datasets(self, **filters: Any) -> dict[str, Any]:
        include_hidden = is_truthy(filters.get("include_hidden"))
        limit = coerce_limit(filters.get("limit"))
        query = str(filters.get("query") or "").strip()
        dataset_type = str(filters.get("dataset_type") or "both").strip().lower()
        short_titles = [item.lower() for item in as_list(filters.get("short_titles"))]
        access_levels = [item.lower() for item in as_list(filters.get("access_levels"))]
        modalities = as_list(filters.get("modalities"))
        data_types = as_list(filters.get("data_types"))
        download_types = as_list(filters.get("download_types"))
        file_types = as_list(filters.get("file_types"))
        cancer_types = as_list(filters.get("cancer_types"))
        cancer_locations = as_list(filters.get("cancer_locations"))
        species = as_list(filters.get("species"))
        programs = as_list(filters.get("programs"))
        external_resources = as_list(filters.get("external_resources"))
        has_external_clinical_resource = filters.get("has_external_clinical_resource")
        doi = str(filters.get("doi") or "").strip().lower()

        sql = "SELECT * FROM agent_dataset_access_summary WHERE 1 = 1"
        params: list[Any] = []
        if not include_hidden:
            sql += " AND hidden = 0"
        if dataset_type in {"collection", "collections"}:
            sql += " AND source = 'collections'"
        elif dataset_type in {"analysis-result", "analysis-results", "analysis_result"}:
            sql += " AND source = 'analysis-results'"
        if short_titles:
            sql += f" AND lower(short_title) IN ({','.join('?' for _ in short_titles)})"
            params.extend(short_titles)
        if access_levels:
            sql += (
                f" AND lower(COALESCE(resolved_access_level, access_level, '')) "
                f"IN ({','.join('?' for _ in access_levels)})"
            )
            params.extend(access_levels)
        if doi:
            sql += " AND lower(COALESCE(doi, '')) = ?"
            params.append(doi)
        if query:
            like = f"%{query.lower()}%"
            sql += (
                " AND (lower(COALESCE(short_title, '')) LIKE ?"
                " OR lower(COALESCE(title, '')) LIKE ?"
                " OR lower(COALESCE(summary, '')) LIKE ?"
                " OR lower(COALESCE(abstract, '')) LIKE ?"
                " OR lower(COALESCE(detailed_description, '')) LIKE ?"
                " OR lower(COALESCE(doi, '')) LIKE ?)"
            )
            params.extend([like] * 6)

        with self._connect_snapshot() as conn:
            columns = self._columns(conn, "agent_dataset_access_summary")
            for column, requested in (
                ("download_data_types", modalities + data_types),
                ("download_types", download_types),
                ("download_file_types", file_types),
                ("cancer_types", cancer_types),
                ("cancer_locations", cancer_locations),
                ("species", species),
                ("program", programs),
                ("external_resources", external_resources),
            ):
                if column not in columns:
                    continue
                for value in requested:
                    sql += f" AND lower(COALESCE({column}, '')) LIKE ?"
                    params.append(f"%{value.lower()}%")
            if has_external_clinical_resource is not None:
                if "has_external_clinical_resource" not in columns:
                    raise TciaServiceError(
                        "This snapshot does not expose has_external_clinical_resource. "
                        "Refresh with `python scripts/tcia_snapshot.py ensure`."
                    )
                sql += " AND COALESCE(has_external_clinical_resource, 0) = ?"
                params.append(1 if is_truthy(has_external_clinical_resource) else 0)

            sql += " ORDER BY hidden, lower(short_title), source LIMIT ?"
            params.append(limit)
            rows = [compact_dataset(row) for row in conn.execute(sql, params).fetchall()]
        return {
            "datasets": rows,
            "count": len(rows),
            "limit": limit,
            "include_hidden": include_hidden,
            "note": "Hidden/staged/retired WordPress records are excluded unless include_hidden is true.",
        }

    def get_dataset(self, short_title: str, include_hidden: bool = False) -> dict[str, Any]:
        title = short_title.strip().lower()
        if not title:
            raise TciaServiceError("short_title is required")
        hidden_clause = "" if include_hidden else " AND hidden = 0"
        with self._connect_snapshot() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM agent_dataset_access_summary
                WHERE lower(short_title) = ?{hidden_clause}
                ORDER BY source
                """,
                (title,),
            ).fetchall()
            if not rows:
                raise TciaServiceError(f"No visible TCIA dataset found for short_title={short_title!r}")
            downloads = conn.execute(
                f"""
                SELECT *
                FROM agent_current_downloads
                WHERE lower(short_title) = ?{hidden_clause}
                ORDER BY download_id, download_title
                """,
                (title,),
            ).fetchall()
            related = conn.execute(
                """
                SELECT *
                FROM agent_dataset_access_summary
                WHERE hidden = 0
                  AND source = 'analysis-results'
                  AND lower(COALESCE(source_collections, '')) LIKE ?
                ORDER BY lower(short_title)
                LIMIT 25
                """,
                (f"%{title}%",),
            ).fetchall()
        return {
            "datasets": [compact_dataset(row) for row in rows],
            "current_downloads": [compact_download(row) for row in downloads],
            "related_analysis_results": [compact_dataset(row) for row in related],
            "caveats": [
                "TCIA publication status is based on visible WordPress Collection and Analysis Result records.",
                "Use download-level labels for modality, file type, and access routing decisions.",
            ],
        }

    def get_dataset_versions(
        self,
        short_title: str,
        include_hidden: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        title = short_title.strip().lower()
        if not title:
            raise TciaServiceError("short_title is required")
        row_limit = coerce_limit(limit, default=100, maximum=500)
        with self._connect_snapshot() as conn:
            if not self._object_exists(conn, "agent_dataset_versions"):
                return {
                    "short_title": short_title,
                    "available": False,
                    "versions": [],
                    "count": 0,
                    "note": (
                        "The loaded snapshot does not include agent_dataset_versions. "
                        "Refresh the release snapshot with `python scripts/tcia_snapshot.py ensure`."
                    ),
                }
            hidden_clause = "" if include_hidden else " AND hidden = 0"
            rows = conn.execute(
                f"""
                SELECT *
                FROM agent_dataset_versions
                WHERE lower(short_title) = ?{hidden_clause}
                ORDER BY
                  CASE WHEN trim(COALESCE(version_number, '')) = '' THEN 1 ELSE 0 END,
                  CAST(NULLIF(version_number, '') AS INTEGER),
                  version_date,
                  CAST(NULLIF(version_id, '') AS INTEGER)
                LIMIT ?
                """,
                (title, row_limit),
            ).fetchall()
        return {
            "short_title": short_title,
            "available": True,
            "versions": [compact_dataset_version(row) for row in rows],
            "count": len(rows),
            "limit": row_limit,
            "note": (
                "Version rows come from TCIA WordPress /api/v2/versions matched to current "
                "Collection or Analysis Result short titles."
            ),
        }

    def get_dataset_v1_releases(self, **filters: Any) -> dict[str, Any]:
        include_hidden = is_truthy(filters.get("include_hidden"))
        limit = coerce_limit(filters.get("limit"), default=50, maximum=500)
        short_titles = [item.lower() for item in as_list(filters.get("short_titles"))]
        dataset_type = str(filters.get("dataset_type") or "both").strip().lower()
        released_since = str(filters.get("released_since") or "").strip()
        released_before = str(filters.get("released_before") or "").strip()
        with self._connect_snapshot() as conn:
            if not self._object_exists(conn, "agent_dataset_v1_releases"):
                return {
                    "available": False,
                    "v1_releases": [],
                    "count": 0,
                    "note": (
                        "The loaded snapshot does not include agent_dataset_v1_releases. "
                        "Refresh the release snapshot with `python scripts/tcia_snapshot.py ensure`."
                    ),
                }
            sql = "SELECT * FROM agent_dataset_v1_releases WHERE 1 = 1"
            params: list[Any] = []
            if not include_hidden:
                sql += " AND hidden = 0"
            if short_titles:
                sql += f" AND lower(short_title) IN ({','.join('?' for _ in short_titles)})"
                params.extend(short_titles)
            if dataset_type in {"collection", "collections"}:
                sql += " AND source = 'collections'"
            elif dataset_type in {"analysis-result", "analysis-results", "analysis_result"}:
                sql += " AND source = 'analysis-results'"
            if released_since:
                sql += " AND COALESCE(v1_release_date, '') >= ?"
                params.append(released_since)
            if released_before:
                sql += " AND COALESCE(v1_release_date, '') < ?"
                params.append(released_before)
            sql += " ORDER BY v1_release_date, lower(short_title), source LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        return {
            "available": True,
            "v1_releases": [compact_v1_release(row) for row in rows],
            "count": len(rows),
            "limit": limit,
            "note": (
                "v1_release_date prefers matched WordPress version-1 rows and falls back to "
                "date_updated only for current records still on version 1."
            ),
        }

    def get_current_downloads(self, short_title: str, **filters: Any) -> dict[str, Any]:
        title = short_title.strip().lower()
        if not title:
            raise TciaServiceError("short_title is required")
        include_hidden = is_truthy(filters.get("include_hidden"))
        limit = coerce_limit(filters.get("limit"))
        access_levels = [item.lower() for item in as_list(filters.get("access_levels"))]
        modalities = as_list(filters.get("modalities"))
        data_types = as_list(filters.get("data_types"))
        download_types = as_list(filters.get("download_types"))
        file_types = as_list(filters.get("file_types"))
        requires_annotations = is_truthy(filters.get("requires_annotations"))

        sql = "SELECT * FROM agent_current_downloads WHERE lower(short_title) = ?"
        params: list[Any] = [title]
        if not include_hidden:
            sql += " AND hidden = 0"
        if access_levels:
            sql += f" AND lower(COALESCE(access_level, '')) IN ({','.join('?' for _ in access_levels)})"
            params.extend(access_levels)
        for column, requested in (
            ("data_types", modalities + data_types),
            ("download_types", download_types),
            ("file_types", file_types),
        ):
            for value in requested:
                sql += f" AND lower(COALESCE({column}, '')) LIKE ?"
                params.append(f"%{value.lower()}%")
        sql += " ORDER BY download_id, download_title LIMIT ?"
        params.append(limit)
        with self._connect_snapshot() as conn:
            rows = [compact_download(row) for row in conn.execute(sql, params).fetchall()]
        if requires_annotations:
            rows = [row for row in rows if self._download_has_annotation(row)]
        return {
            "short_title": short_title,
            "downloads": rows,
            "count": len(rows),
            "limit": limit,
        }

    def summarize_access(self, short_title: str, include_hidden: bool = False) -> dict[str, Any]:
        dataset_payload = self.get_dataset(short_title, include_hidden=include_hidden)
        dataset = dataset_payload["datasets"][0]
        downloads = dataset_payload["current_downloads"]
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in downloads:
            groups.setdefault(str(row.get("access_level") or "unknown"), []).append(row)
        notes = [
            "Creative Commons licenses are open access; Creative Commons NonCommercial licenses are open with a noncommercial-use restriction.",
            "Controlled/restricted downloads require the TCIA controlled-access workflow and must not be downloaded directly by this server.",
        ]
        if dataset.get("resolved_access_level") in {"controlled", "mixed"}:
            notes.append(f"Controlled-access policy: {CONTROLLED_ACCESS_POLICY_URL}")
        return {
            "dataset": dataset,
            "download_groups": groups,
            "controlled_access_policy_url": CONTROLLED_ACCESS_POLICY_URL,
            "notes": notes,
        }

    def find_controlled_access_datasets(self, **filters: Any) -> dict[str, Any]:
        limit = coerce_limit(filters.get("limit"))
        modalities = as_list(filters.get("modalities"))
        file_types = as_list(filters.get("file_types"))
        requires_annotations = is_truthy(filters.get("requires_annotations"))
        include_mixed = True if filters.get("include_mixed") is None else is_truthy(filters.get("include_mixed"))
        access_levels = ["controlled", "mixed"] if include_mixed else ["controlled"]
        with self._connect_snapshot() as conn:
            summaries = conn.execute(
                f"""
                SELECT *
                FROM agent_dataset_access_summary
                WHERE hidden = 0
                  AND resolved_access_level IN ({','.join('?' for _ in access_levels)})
                ORDER BY lower(short_title)
                """,
                access_levels,
            ).fetchall()
            downloads = conn.execute(
                """
                SELECT *
                FROM agent_current_downloads
                WHERE hidden = 0
                  AND access_level IN ('controlled', 'mixed')
                ORDER BY lower(short_title), download_id, download_title
                """
            ).fetchall()

        by_title: dict[str, list[dict[str, Any]]] = {}
        for row in downloads:
            compact = compact_download(row)
            by_title.setdefault(str(compact.get("short_title") or "").lower(), []).append(compact)

        matches: list[dict[str, Any]] = []
        for row in summaries:
            dataset = compact_dataset(row)
            title = str(dataset.get("short_title") or "").lower()
            evidence = by_title.get(title, [])
            labels: list[str] = []
            for download in evidence:
                labels.extend(json_text_values(download, ["download_types", "data_types", "file_types"]))
                labels.append(str(download.get("download_title") or ""))
            if modalities and not all(self._label_matches(labels, modality) for modality in modalities):
                continue
            if file_types and not all(self._label_matches(labels, file_type) for file_type in file_types):
                continue
            if requires_annotations and not self._labels_have_annotation(labels):
                continue
            matches.append({"dataset": dataset, "matching_downloads": evidence})
            if len(matches) >= limit:
                break
        return {
            "datasets": matches,
            "count": len(matches),
            "limit": limit,
            "controlled_access_policy_url": CONTROLLED_ACCESS_POLICY_URL,
            "note": "Controlled metadata does not grant access; use TCIA policy and authorized Data Retriever workflows.",
        }

    def get_controlled_access_files(self, short_title: str, **filters: Any) -> dict[str, Any]:
        title = short_title.strip().lower()
        if not title:
            raise TciaServiceError("short_title is required")
        limit = coerce_limit(filters.get("limit"), default=50, maximum=500)
        route_systems = [item.lower() for item in as_list(filters.get("route_systems"))]
        modalities = [item.upper() for item in as_list(filters.get("modalities"))]
        file_types = [item.lower() for item in as_list(filters.get("file_types"))]
        body_part = str(filters.get("body_part") or "").strip().lower()
        participant_id = str(filters.get("participant_id") or "").strip().lower()
        patient_id = str(filters.get("patient_id") or "").strip().lower()
        has_drs_uri = filters.get("has_drs_uri")

        sql = "SELECT * FROM agent_controlled_files WHERE lower(short_title) = ?"
        params: list[Any] = [title]
        if route_systems:
            sql += f" AND lower(COALESCE(route_system, '')) IN ({','.join('?' for _ in route_systems)})"
            params.extend(route_systems)
        if modalities:
            sql += (
                f" AND upper(COALESCE(modality, image_modality, '')) "
                f"IN ({','.join('?' for _ in modalities)})"
            )
            params.extend(modalities)
        for value in file_types:
            sql += " AND lower(COALESCE(file_type, file_format, file_ext, '')) LIKE ?"
            params.append(f"%{value}%")
        if body_part:
            sql += " AND lower(COALESCE(body_part_examined, '')) LIKE ?"
            params.append(f"%{body_part}%")
        if participant_id:
            sql += " AND lower(COALESCE(participant_id, '')) = ?"
            params.append(participant_id)
        if patient_id:
            sql += " AND lower(COALESCE(patient_id, '')) = ?"
            params.append(patient_id)
        if has_drs_uri is not None:
            sql += " AND COALESCE(drs_uri, '') " + ("<> ''" if is_truthy(has_drs_uri) else "= ''")
        sql += " ORDER BY route_system, participant_id, patient_id, series_instance_uid, file_name LIMIT ?"
        params.append(limit)

        with self._connect_controlled() as conn:
            rows = [compact_controlled_file(row) for row in conn.execute(sql, params).fetchall()]
        return {
            "short_title": short_title,
            "files": rows,
            "count": len(rows),
            "limit": limit,
            "controlled_access_policy_url": CONTROLLED_ACCESS_POLICY_URL,
            "warning": (
                "These are public metadata rows for controlled-access files. "
                "The MCP server does not grant authorization and must not download controlled data."
            ),
        }

    def find_dicom_annotations(self, **filters: Any) -> dict[str, Any]:
        include_hidden = is_truthy(filters.get("include_hidden"))
        limit = coerce_limit(filters.get("limit"))
        query = str(filters.get("query") or "").strip().lower()
        modalities = as_list(filters.get("modalities"))
        access_levels = [item.lower() for item in as_list(filters.get("access_levels"))]
        short_titles = [item.lower() for item in as_list(filters.get("short_titles"))]

        sql = "SELECT * FROM agent_current_downloads WHERE lower(COALESCE(file_types, '')) LIKE '%dicom%'"
        params: list[Any] = []
        if not include_hidden:
            sql += " AND hidden = 0"
        if short_titles:
            sql += f" AND lower(short_title) IN ({','.join('?' for _ in short_titles)})"
            params.extend(short_titles)
        if access_levels:
            sql += f" AND lower(COALESCE(access_level, '')) IN ({','.join('?' for _ in access_levels)})"
            params.extend(access_levels)
        for modality in modalities:
            sql += " AND lower(COALESCE(data_types, '')) LIKE ?"
            params.append(f"%{modality.lower()}%")
        if query:
            like = f"%{query}%"
            sql += (
                " AND (lower(COALESCE(short_title, '')) LIKE ?"
                " OR lower(COALESCE(title, '')) LIKE ?"
                " OR lower(COALESCE(download_title, '')) LIKE ?"
                " OR lower(COALESCE(description, '')) LIKE ?)"
            )
            params.extend([like] * 4)
        sql += " ORDER BY lower(short_title), download_id, download_title LIMIT ?"
        params.append(limit)
        with self._connect_snapshot() as conn:
            rows = [compact_download(row) for row in conn.execute(sql, params).fetchall()]
        rows = [row for row in rows if self._download_has_annotation(row)]
        return {
            "downloads": rows[:limit],
            "count": len(rows[:limit]),
            "limit": limit,
            "routing_note": (
                "For open/public DICOM, use IDC/idc-index after TCIA provenance and access are confirmed. "
                "For controlled DICOM, do not generate public IDC/NBIA viewer or download routes."
            ),
        }

    def _nifti_dataset_rows(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        if self._object_exists(conn, "agent_nifti_dataset_summary"):
            rows = conn.execute(
                "SELECT * FROM agent_nifti_dataset_summary ORDER BY lower(short_title)"
            ).fetchall()
            output = [dict(row) for row in rows]
        else:
            rows = conn.execute(
                """
                WITH download_summary AS (
                    SELECT parent_source, dataset_type, short_title, MAX(title) AS title,
                           COUNT(*) AS nifti_downloads,
                           group_concat(download_id, '; ') AS download_ids,
                           group_concat(COALESCE(NULLIF(download_title, ''), download_id), '; ') AS download_labels
                    FROM nifti_downloads
                    GROUP BY parent_source, dataset_type, short_title
                ),
                file_summary AS (
                    SELECT short_title,
                           COUNT(*) AS non_dicom_files,
                           SUM(CASE WHEN is_nifti THEN 1 ELSE 0 END) AS nifti_files,
                           SUM(CASE WHEN is_sidecar THEN 1 ELSE 0 END) AS sidecar_files,
                           SUM(CASE WHEN is_package_metadata THEN 1 ELSE 0 END) AS package_metadata_files
                    FROM non_dicom_files
                    GROUP BY short_title
                ),
                radiology_summary AS (
                    SELECT short_title,
                           COUNT(*) AS radiology_series_rows,
                           SUM(CASE WHEN upper(COALESCE(modality, '')) = 'MR' THEN 1 ELSE 0 END) AS mr_files,
                           SUM(CASE WHEN upper(COALESCE(modality, '')) = 'CT' THEN 1 ELSE 0 END) AS ct_files,
                           SUM(CASE WHEN is_derived_object THEN 1 ELSE 0 END) AS derived_radiology_rows,
                           COUNT(DISTINCT NULLIF(subject_id, '')) AS subject_ids,
                           COUNT(DISTINCT NULLIF(study_id, '')) AS study_ids,
                           COUNT(DISTINCT NULLIF(series_id, '')) AS series_ids
                    FROM radiology_series
                    GROUP BY short_title
                ),
                derived_summary AS (
                    SELECT d.short_title,
                           COUNT(*) AS derived_objects,
                           COUNT(DISTINCT dor.derived_object_id) AS linked_derived_objects
                    FROM derived_objects d
                    LEFT JOIN derived_object_references dor
                      ON dor.derived_object_id = d.derived_object_id
                    GROUP BY d.short_title
                )
                SELECT d.*, COALESCE(f.nifti_files, 0) AS nifti_files,
                       COALESCE(f.non_dicom_files, 0) AS non_dicom_files,
                       COALESCE(f.sidecar_files, 0) AS sidecar_files,
                       COALESCE(f.package_metadata_files, 0) AS package_metadata_files,
                       COALESCE(r.radiology_series_rows, 0) AS radiology_series_rows,
                       COALESCE(r.mr_files, 0) AS mr_files,
                       COALESCE(r.ct_files, 0) AS ct_files,
                       COALESCE(r.derived_radiology_rows, 0) AS derived_radiology_rows,
                       COALESCE(x.derived_objects, 0) AS derived_objects,
                       COALESCE(x.linked_derived_objects, 0) AS linked_derived_objects,
                       COALESCE(r.subject_ids, 0) AS subject_ids,
                       COALESCE(r.study_ids, 0) AS study_ids,
                       COALESCE(r.series_ids, 0) AS series_ids
                FROM download_summary d
                LEFT JOIN file_summary f ON lower(f.short_title) = lower(d.short_title)
                LEFT JOIN radiology_summary r ON lower(r.short_title) = lower(d.short_title)
                LEFT JOIN derived_summary x ON lower(x.short_title) = lower(d.short_title)
                ORDER BY lower(d.short_title)
                """
            ).fetchall()
            output = [dict(row) for row in rows]
        package_counts = {
            row["short_title"].lower(): row["rows"]
            for row in conn.execute(
                "SELECT short_title, COUNT(*) AS rows FROM package_files GROUP BY short_title"
            )
        }
        sums_counts = {
            row["short_title"].lower(): row["rows"]
            for row in conn.execute(
                "SELECT short_title, COUNT(*) AS rows FROM aspera_root_sums_inventory GROUP BY short_title"
            )
        }
        for row in output:
            title = str(row.get("short_title") or "").lower()
            row["package_file_rows"] = package_counts.get(title, 0)
            row["aspera_root_sums_rows"] = sums_counts.get(title, 0)
        return output

    def find_nifti_datasets(self, **filters: Any) -> dict[str, Any]:
        limit = coerce_limit(filters.get("limit"))
        short_titles = {item.lower() for item in as_list(filters.get("short_titles"))}
        modalities = {item.upper() for item in as_list(filters.get("modalities"))}
        requires_derived = is_truthy(filters.get("requires_derived_objects"))
        with self._connect_nifti() as conn:
            rows = [compact_nifti_summary(row) for row in self._nifti_dataset_rows(conn)]
        matches: list[dict[str, Any]] = []
        for row in rows:
            title = str(row.get("short_title") or "").lower()
            if short_titles and title not in short_titles:
                continue
            if modalities:
                ok = True
                for modality in modalities:
                    if modality == "MR" and safe_int(row.get("mr_files")) <= 0:
                        ok = False
                    elif modality == "CT" and safe_int(row.get("ct_files")) <= 0:
                        ok = False
                    elif modality not in {"MR", "CT"} and modality.lower() not in str(row).lower():
                        ok = False
                if not ok:
                    continue
            if requires_derived and safe_int(row.get("derived_objects")) <= 0:
                continue
            matches.append(row)
            if len(matches) >= limit:
                break
        return {
            "datasets": matches,
            "count": len(matches),
            "limit": limit,
            "note": "NIfTI metadata is a public, non-controlled optional sidecar. Confirm TCIA provenance and access in the base snapshot first.",
        }

    def get_nifti_files(self, short_title: str, **filters: Any) -> dict[str, Any]:
        title = short_title.strip().lower()
        if not title:
            raise TciaServiceError("short_title is required")
        limit = coerce_limit(filters.get("limit"), default=50, maximum=500)
        modalities = [item.upper() for item in as_list(filters.get("modalities"))]
        subject_id = str(filters.get("subject_id") or "").strip().lower()
        file_name_contains = str(filters.get("file_name_contains") or "").strip().lower()
        derived_only = is_truthy(filters.get("derived_only"))
        has_source_uids = filters.get("has_source_uids")
        with self._connect_nifti() as conn:
            table = "agent_nifti_files" if self._object_exists(conn, "agent_nifti_files") else "radiology_series"
            columns = self._columns(conn, table)
            modality_expr = self._coalesce_expr(columns, ["modality", "Modality"])
            subject_expr = self._coalesce_expr(columns, ["subject_id", "PatientID"])
            series_uid_expr = self._coalesce_expr(columns, ["series_instance_uid", "SeriesInstanceUID"])
            sql = f"SELECT * FROM {table} WHERE lower(short_title) = ?"
            params: list[Any] = [title]
            if modalities:
                sql += f" AND upper({modality_expr}) IN ({','.join('?' for _ in modalities)})"
                params.extend(modalities)
            if subject_id:
                sql += f" AND lower({subject_expr}) = ?"
                params.append(subject_id)
            if file_name_contains:
                sql += " AND lower(COALESCE(file_name, '')) LIKE ?"
                params.append(f"%{file_name_contains}%")
            if derived_only:
                sql += " AND COALESCE(is_derived_object, 0) = 1"
            if has_source_uids is not None:
                if is_truthy(has_source_uids):
                    sql += f" AND {series_uid_expr} <> ''"
                else:
                    sql += f" AND {series_uid_expr} = ''"
            sql += " ORDER BY subject_id, package_path, file_name LIMIT ?"
            params.append(limit)
            rows = [compact_nifti_file(row) for row in conn.execute(sql, params).fetchall()]
        return {"short_title": short_title, "files": rows, "count": len(rows), "limit": limit}

    def get_nifti_derived_objects(self, short_title: str, **filters: Any) -> dict[str, Any]:
        title = short_title.strip().lower()
        if not title:
            raise TciaServiceError("short_title is required")
        limit = coerce_limit(filters.get("limit"), default=50, maximum=500)
        linked_only = is_truthy(filters.get("linked_only"))
        file_name_contains = str(filters.get("file_name_contains") or "").strip().lower()
        confidence = str(filters.get("confidence") or "").strip().lower()
        with self._connect_nifti() as conn:
            if self._object_exists(conn, "agent_nifti_derived_objects"):
                sql = "SELECT * FROM agent_nifti_derived_objects WHERE lower(short_title) = ?"
                linked_clause = " AND COALESCE(referenced_file_name, referenced_series_id, '') <> ''"
            else:
                sql = """
                SELECT d.*, dor.referenced_non_dicom_file_id, dor.referenced_file_name,
                       dor.referenced_package_path, dor.reference_role,
                       dor.inference_method, dor.confidence, dor.evidence_json
                FROM derived_objects d
                LEFT JOIN derived_object_references dor
                  ON dor.derived_object_id = d.derived_object_id
                WHERE lower(d.short_title) = ?
                """
                linked_clause = " AND COALESCE(dor.referenced_file_name, d.referenced_series_id, '') <> ''"
            params: list[Any] = [title]
            if linked_only:
                sql += linked_clause
            if confidence:
                sql += " AND lower(COALESCE(confidence, '')) = ?"
                params.append(confidence)
            if file_name_contains:
                sql += " AND lower(COALESCE(file_name, '')) LIKE ?"
                params.append(f"%{file_name_contains}%")
            sql += " ORDER BY file_name, referenced_file_name LIMIT ?"
            params.append(limit)
            rows = [compact_nifti_derived(row) for row in conn.execute(sql, params).fetchall()]
        return {
            "short_title": short_title,
            "derived_objects": rows,
            "count": len(rows),
            "limit": limit,
            "note": "Derived-object source links are heuristic unless the inference method records an explicit source identifier.",
        }

    def get_nifti_package_files(self, short_title: str, **filters: Any) -> dict[str, Any]:
        title = short_title.strip().lower()
        if not title:
            raise TciaServiceError("short_title is required")
        limit = coerce_limit(filters.get("limit"), default=50, maximum=500)
        file_exts = [item.lower().lstrip(".") for item in as_list(filters.get("file_exts"))]
        file_name_contains = str(filters.get("file_name_contains") or "").strip().lower()
        metadata_candidates = filters.get("metadata_candidates")
        with self._connect_nifti() as conn:
            sql = "SELECT * FROM package_files WHERE lower(short_title) = ?"
            params: list[Any] = [title]
            if file_exts:
                sql += f" AND lower(COALESCE(file_ext, '')) IN ({','.join('?' for _ in file_exts)})"
                params.extend(file_exts)
            if file_name_contains:
                sql += " AND lower(COALESCE(file_name, package_path, '')) LIKE ?"
                params.append(f"%{file_name_contains}%")
            if metadata_candidates is not None:
                sql += " AND COALESCE(is_metadata_candidate, 0) = ?"
                params.append(1 if is_truthy(metadata_candidates) else 0)
            sql += " ORDER BY package_path, file_name LIMIT ?"
            params.append(limit)
            rows = [compact_package_file(row) for row in conn.execute(sql, params).fetchall()]
        return {
            "short_title": short_title,
            "package_files": rows,
            "count": len(rows),
            "limit": limit,
            "note": "Aspera package listings are inventory metadata only; browse before downloading large packages.",
        }

    def _pathology_dataset_rows(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        if self._object_exists(conn, "agent_pathology_dataset_summary"):
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM agent_pathology_dataset_summary ORDER BY lower(short_title)"
                )
            ]
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT s.*,
                       CASE
                         WHEN COALESCE(p.package_file_rows, 0) = 0
                          AND COALESCE(o.file_object_rows, 0) = 0 THEN 'not_imported'
                         WHEN COALESCE(p.package_file_rows, 0) = 0
                          AND COALESCE(o.file_object_rows, 0) > 0 THEN 'pathdb_file_objects_available'
                         WHEN COALESCE(p.package_file_rows, 0) > 0
                          AND COALESCE(o.file_object_rows, 0) = 0 THEN 'package_rows_imported'
                         ELSE 'normalized_file_rows_available'
                       END AS package_inventory_status,
                       COALESCE(p.package_file_rows, 0) AS package_file_rows,
                       COALESCE(o.file_object_rows, 0) AS file_object_rows
                FROM pathology_dataset_summary s
                LEFT JOIN (
                    SELECT short_title, COUNT(*) AS package_file_rows
                    FROM pathology_package_files
                    GROUP BY short_title
                ) p ON lower(p.short_title) = lower(s.short_title)
                LEFT JOIN (
                    SELECT short_title, COUNT(*) AS file_object_rows
                    FROM pathology_file_objects
                    GROUP BY short_title
                ) o ON lower(o.short_title) = lower(s.short_title)
                ORDER BY lower(s.short_title)
                """
            )
        ]

    def find_pathology_datasets(self, **filters: Any) -> dict[str, Any]:
        limit = coerce_limit(filters.get("limit"))
        short_titles = {item.lower() for item in as_list(filters.get("short_titles"))}
        package_inventory_status = str(filters.get("package_inventory_status") or "").strip().lower()
        with_package_inventory = filters.get("with_package_inventory")
        has_pathdb = filters.get("has_pathdb")
        with self._connect_pathology() as conn:
            rows = [compact_pathology_summary(row) for row in self._pathology_dataset_rows(conn)]
        matches: list[dict[str, Any]] = []
        for row in rows:
            title = str(row.get("short_title") or "").lower()
            if short_titles and title not in short_titles:
                continue
            if package_inventory_status and str(row.get("package_inventory_status") or "").lower() != package_inventory_status:
                continue
            if with_package_inventory is not None:
                has_inventory = safe_int(row.get("package_file_rows")) > 0 or safe_int(row.get("file_object_rows")) > 0
                if has_inventory != is_truthy(with_package_inventory):
                    continue
            if has_pathdb is not None:
                pathdb = safe_int(row.get("pathdb_collection_slide_count")) > 0
                if pathdb != is_truthy(has_pathdb):
                    continue
            matches.append(row)
            if len(matches) >= limit:
                break
        return {
            "datasets": matches,
            "count": len(matches),
            "limit": limit,
            "note": "Pathology Aspera packages are the original submitter-provided file copy; PathDB is enrichment/viewer metadata.",
        }

    def get_pathology_downloads(self, **filters: Any) -> dict[str, Any]:
        limit = coerce_limit(filters.get("limit"))
        short_titles = {item.lower() for item in as_list(filters.get("short_titles"))}
        with self._connect_pathology() as conn:
            table = "agent_pathology_downloads" if self._object_exists(conn, "agent_pathology_downloads") else "pathology_downloads"
            sql = f"SELECT * FROM {table} WHERE 1 = 1"
            params: list[Any] = []
            if short_titles:
                sql += f" AND lower(short_title) IN ({','.join('?' for _ in short_titles)})"
                params.extend(short_titles)
            sql += " ORDER BY lower(short_title), download_id LIMIT ?"
            params.append(limit)
            rows = [compact_download(row) for row in conn.execute(sql, params).fetchall()]
        return {"downloads": rows, "count": len(rows), "limit": limit}

    def get_pathology_package_files(self, short_title: str, **filters: Any) -> dict[str, Any]:
        title = short_title.strip().lower()
        if not title:
            raise TciaServiceError("short_title is required")
        limit = coerce_limit(filters.get("limit"), default=50, maximum=500)
        file_exts = [item.lower().lstrip(".") for item in as_list(filters.get("file_exts"))]
        file_roles = [item.lower() for item in as_list(filters.get("file_roles"))]
        download_id = str(filters.get("download_id") or "").strip()
        file_name_contains = str(filters.get("file_name_contains") or "").strip().lower()
        with self._connect_pathology() as conn:
            table = "agent_pathology_package_files" if self._object_exists(conn, "agent_pathology_package_files") else "pathology_package_files"
            sql = f"SELECT * FROM {table} WHERE lower(short_title) = ?"
            params: list[Any] = [title]
            if download_id:
                sql += " AND COALESCE(download_id, '') = ?"
                params.append(download_id)
            if file_exts:
                sql += f" AND lower(COALESCE(file_ext, '')) IN ({','.join('?' for _ in file_exts)})"
                params.extend(file_exts)
            if file_roles:
                sql += f" AND lower(COALESCE(file_role, '')) IN ({','.join('?' for _ in file_roles)})"
                params.extend(file_roles)
            if file_name_contains:
                sql += " AND lower(COALESCE(file_name, package_path, '')) LIKE ?"
                params.append(f"%{file_name_contains}%")
            sql += " ORDER BY package_path, file_name LIMIT ?"
            params.append(limit)
            rows = [compact_package_file(row) for row in conn.execute(sql, params).fetchall()]
        return {
            "short_title": short_title,
            "package_files": rows,
            "count": len(rows),
            "limit": limit,
            "note": "These rows are Aspera package inventory metadata, not a download action.",
        }

    def get_pathology_file_objects(self, short_title: str, **filters: Any) -> dict[str, Any]:
        title = short_title.strip().lower()
        if not title:
            raise TciaServiceError("short_title is required")
        limit = coerce_limit(filters.get("limit"), default=50, maximum=500)
        file_exts = [item.lower().lstrip(".") for item in as_list(filters.get("file_exts"))]
        file_roles = [item.lower() for item in as_list(filters.get("file_roles"))]
        file_name_contains = str(filters.get("file_name_contains") or "").strip().lower()
        with self._connect_pathology() as conn:
            table = "agent_pathology_file_objects" if self._object_exists(conn, "agent_pathology_file_objects") else "pathology_file_objects"
            sql = f"SELECT * FROM {table} WHERE lower(short_title) = ?"
            params: list[Any] = [title]
            if file_exts:
                sql += f" AND lower(COALESCE(file_ext, '')) IN ({','.join('?' for _ in file_exts)})"
                params.extend(file_exts)
            if file_roles:
                sql += f" AND lower(COALESCE(file_role, '')) IN ({','.join('?' for _ in file_roles)})"
                params.extend(file_roles)
            if file_name_contains:
                sql += " AND lower(COALESCE(file_name, package_path, '')) LIKE ?"
                params.append(f"%{file_name_contains}%")
            sql += " ORDER BY package_path, file_name LIMIT ?"
            params.append(limit)
            rows = [compact_pathology_file_object(row) for row in conn.execute(sql, params).fetchall()]
        return {"short_title": short_title, "file_objects": rows, "count": len(rows), "limit": limit}

    def get_pathology_disparities(self, **filters: Any) -> dict[str, Any]:
        limit = coerce_limit(filters.get("limit"))
        disparity_types = [item.lower() for item in as_list(filters.get("disparity_types"))]
        short_titles = {item.lower() for item in as_list(filters.get("short_titles"))}
        with self._connect_pathology() as conn:
            sql = "SELECT * FROM pathology_disparities WHERE 1 = 1"
            params: list[Any] = []
            if disparity_types:
                sql += f" AND lower(disparity_type) IN ({','.join('?' for _ in disparity_types)})"
                params.extend(disparity_types)
            if short_titles:
                sql += f" AND lower(COALESCE(short_title, '')) IN ({','.join('?' for _ in short_titles)})"
                params.extend(short_titles)
            sql += " ORDER BY severity, disparity_type, lower(COALESCE(short_title, pathdb_collection, '')) LIMIT ?"
            params.append(limit)
            rows = [normalize_row(row) for row in conn.execute(sql, params).fetchall()]
        return {"disparities": rows, "count": len(rows), "limit": limit}

    def _download_has_annotation(self, row: dict[str, Any]) -> bool:
        labels = json_text_values(row, ["download_types", "data_types", "file_types"])
        labels.append(str(row.get("download_title") or ""))
        return self._labels_have_annotation(labels)

    def _labels_have_annotation(self, labels: list[str]) -> bool:
        text = " | ".join(labels).lower()
        tokens = {label.lower() for label in labels}
        if tokens & ANNOTATION_LABELS:
            return True
        return any(re.search(rf"\b{re.escape(label)}\b", text) for label in ANNOTATION_LABELS)

    def _label_matches(self, labels: list[str], requested: str) -> bool:
        needle = requested.strip().lower()
        return any(needle == str(label).strip().lower() or needle in str(label).strip().lower() for label in labels)
