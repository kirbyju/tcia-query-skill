import sqlite3
import sys
import tempfile
import unittest
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import tcia_public_non_dicom_metadata as public_metadata


class GeometryArtifactIntegrationTests(unittest.TestCase):
    def test_reset_geometry_surface_clears_evidence_and_resets_all_assets(self):
        with sqlite3.connect(":memory:") as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript(public_metadata.SCHEMA)
            public_metadata.insert_vocab(conn)
            for asset_id, file_format in (("nifti", "NIFTI"), ("csv", "CSV")):
                conn.execute(
                    """INSERT INTO public_non_dicom_assets (
                    asset_id,dataset_type,short_title,download_id,
                    participant_link_status,asset_granularity,file_format,
                    media_kind,spatial_dimensionality,temporal_dimensionality,
                    imaging_domain,object_role,representation_provenance_class,
                    source_system,raw_values_json,provenance_json,quality_flag_json,
                    geometry_status,geometry_assessment_method,geometry_assessment_source,
                    geometry_details_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (asset_id, "Collection", "Demo", "1", "unavailable", "file",
                     file_format, "image_volume", "unknown", "unknown", "radiology",
                     "source_image", "submitted_original", "tcia_aspera", "{}", "{}",
                     "{}", "checked_grid_geometry", "old", "old", "{}"),
                )
            counts = public_metadata.reset_geometry_surface(conn)
            self.assertEqual(counts["asset_statuses_reset"], 2)
            statuses = dict(conn.execute(
                "SELECT asset_id,geometry_status FROM public_non_dicom_assets"
            ))
            self.assertEqual(statuses, {"nifti": "not_checked", "csv": "not_applicable"})

    def test_legacy_public_dicom_assets_and_dependents_are_purged(self):
        with sqlite3.connect(":memory:") as conn:
            conn.executescript(public_metadata.SCHEMA)
            public_metadata.insert_vocab(conn)
            for asset_id, file_format in (
                ("legacy-dicom", "DICOM"),
                ("keep-nifti", "NIFTI"),
            ):
                conn.execute(
                    """
                    INSERT INTO public_non_dicom_assets (
                      asset_id,dataset_type,short_title,download_id,
                      participant_link_status,asset_granularity,file_format,
                      media_kind,spatial_dimensionality,temporal_dimensionality,
                      imaging_domain,object_role,representation_provenance_class,
                      source_system,raw_values_json,provenance_json,quality_flag_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        asset_id, "Analysis Result", "Demo", "1", "unavailable",
                        "file", file_format, "image_volume", "3d", "none",
                        "radiology", "source_image", "submitted_original",
                        "tcia_aspera", "{}", "{}", "{}",
                    ),
                )
                conn.execute(
                    "INSERT INTO public_non_dicom_locations "
                    "(location_id,asset_id,managed_system,system_functions_json,"
                    "access_url,access_level,representation_provenance_class,"
                    "provenance_json) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        f"location-{asset_id}", asset_id, "tcia_aspera",
                        '["distribution_endpoint"]', "https://example.invalid",
                        "open", "submitted_original", "{}",
                    ),
                )
            self.assertEqual(public_metadata.delete_public_dicom_assets(conn), 1)
            self.assertEqual(
                conn.execute(
                    "SELECT group_concat(asset_id) FROM public_non_dicom_assets"
                ).fetchone()[0],
                "keep-nifti",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT group_concat(asset_id) FROM public_non_dicom_locations"
                ).fetchone()[0],
                "keep-nifti",
            )

    def test_documented_partial_job_coverage_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            coverage_path = Path(directory) / "job.coverage.json"
            coverage_path.write_text(
                json.dumps(
                    {
                        "job_id": "job-partial",
                        "short_title": "Demo",
                        "download_status": "partial_source_broken",
                        "complete": False,
                        "analyzable": True,
                        "inventory_non_directory_entries": 4391,
                        "downloaded_files": 4390,
                        "file_count_coverage": 4390 / 4391,
                        "missing_paths": ["/Demo/missing.nii.gz"],
                        "source_error": {"http_code": 500},
                    }
                )
            )
            with sqlite3.connect(":memory:") as conn:
                conn.row_factory = sqlite3.Row
                conn.executescript(public_metadata.SCHEMA)
                public_metadata.insert_vocab(conn)
                conn.execute(
                    """
                    INSERT INTO public_non_dicom_assets (
                      asset_id,dataset_type,short_title,download_id,
                      participant_link_status,asset_granularity,file_format,
                      media_kind,spatial_dimensionality,temporal_dimensionality,
                      imaging_domain,object_role,representation_provenance_class,
                      source_system,raw_values_json,provenance_json,quality_flag_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "asset-demo", "Collection", "Demo", "1", "unavailable",
                        "download", "NIFTI", "image_volume", "unknown", "unknown",
                        "radiology", "source_image", "submitted_original",
                        "tcia_aspera", "{}", "{}", "{}",
                    ),
                )
                self.assertEqual(
                    public_metadata.ingest_geometry_coverage(
                        conn, [coverage_path]
                    ),
                    1,
                )
                row = conn.execute(
                    "SELECT * FROM agent_public_non_dicom_geometry_job_coverage"
                ).fetchone()
                self.assertEqual(row["downloaded_files"], 4390)
                self.assertEqual(row["complete"], 0)

    def test_defaults_and_imported_assessment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_db = root / "geometry.sqlite"
            with sqlite3.connect(result_db) as conn:
                conn.execute(
                    """
                    CREATE TABLE geometry_assessments (
                      assessment_id INTEGER PRIMARY KEY,
                      short_title TEXT, download_id TEXT,
                      asset_id TEXT, geometry_status TEXT, analyzer TEXT,
                      analyzer_version TEXT, assessed_at_utc TEXT,
                      checks_json TEXT, details_json TEXT, error TEXT,
                      local_relative_path TEXT, file_format TEXT,
                      assessment_scope TEXT, series_instance_uid TEXT,
                      study_instance_uid TEXT, dimension INTEGER,
                      shape_json TEXT, spacing_json TEXT, origin_json TEXT,
                      direction_json TEXT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO geometry_assessments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        1,
                        "Demo",
                        "1",
                        "asset-volume",
                        "checked_grid_geometry",
                        "test",
                        "1",
                        "2026-01-01T00:00:00Z",
                        '{"finite_affine":true}',
                        "{}",
                        "",
                        "case.nii.gz",
                        "NIFTI",
                        "file",
                        "",
                        "",
                        3,
                        "[10,10,10]",
                        "[1,1,1]",
                        "[0,0,0]",
                        "[1,0,0,0,1,0,0,0,1]",
                    ),
                )
                conn.execute(
                    "INSERT INTO geometry_assessments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        2,
                        "Demo",
                        "1",
                        "asset-volume",
                        "checked_invalid_geometry",
                        "test",
                        "1",
                        "2026-01-01T00:01:00Z",
                        '{"finite_affine":false}',
                        "{}",
                        "invalid affine",
                        "case-2.nii.gz",
                        "NIFTI",
                        "file",
                        "",
                        "",
                        3,
                        "[10,10,10]",
                        "[1,1,1]",
                        "[0,0,0]",
                        "[0,0,0,0,0,0,0,0,0]",
                    ),
                )

            with sqlite3.connect(":memory:") as conn:
                conn.row_factory = sqlite3.Row
                conn.executescript(public_metadata.SCHEMA)
                public_metadata.insert_vocab(conn)
                base = {
                    "dataset_type": "Collection",
                    "short_title": "Demo",
                    "download_row_id": 1,
                    "download_id": "1",
                    "subject_id": "CASE-1",
                    "subject_id_namespace": "tcia_dataset:Demo",
                    "participant_link_status": "source_supported",
                    "asset_granularity": "file",
                    "asset_name": "",
                    "package_path": "",
                    "container_format": "",
                    "media_kind": "image_volume",
                    "spatial_dimensionality": "unknown",
                    "temporal_dimensionality": "unknown",
                    "imaging_domain": "radiology",
                    "modality": "MR",
                    "object_role": "source_image",
                    "represented_file_count": 1,
                    "size_bytes": 1,
                    "checksum": "",
                    "checksum_algorithm": "",
                    "representation_provenance_class": "submitted_original",
                    "source_system": "tcia_wordpress",
                    "source_record_id": "",
                    "source_url": "",
                    "raw_values_json": "{}",
                    "provenance_json": "{}",
                    "quality_flag_json": "{}",
                }
                public_metadata.insert_asset(
                    conn,
                    {
                        **base,
                        "asset_id": "asset-volume",
                        "file_name": "case.nii.gz",
                        "file_format": "NIFTI",
                    },
                )
                public_metadata.insert_asset(
                    conn,
                    {
                        **base,
                        "asset_id": "asset-image",
                        "file_name": "case.png",
                        "file_format": "PNG",
                        "media_kind": "still_image",
                    },
                )
                self.assertEqual(public_metadata.initialize_geometry_statuses(conn), 2)
                self.assertEqual(
                    conn.execute(
                        "SELECT geometry_status FROM public_non_dicom_assets "
                        "WHERE asset_id='asset-volume'"
                    ).fetchone()[0],
                    "not_checked",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT geometry_status FROM public_non_dicom_assets "
                        "WHERE asset_id='asset-image'"
                    ).fetchone()[0],
                    "not_applicable",
                )
                counts = public_metadata.ingest_geometry_results(conn, result_db)
                self.assertEqual(counts["matched_assets"], 1)
                self.assertEqual(counts["matched_rows"], 2)
                self.assertEqual(
                    conn.execute(
                        "SELECT geometry_status FROM public_non_dicom_assets "
                        "WHERE asset_id='asset-volume'"
                    ).fetchone()[0],
                    "mixed",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM public_non_dicom_geometry_assessments"
                    ).fetchone()[0],
                    2,
                )

    def test_unique_fallbacks_preserve_rows_without_source_asset_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            result_db = Path(directory) / "geometry.sqlite"
            with sqlite3.connect(result_db) as source:
                source.execute(
                    """
                    CREATE TABLE geometry_assessments (
                      assessment_id INTEGER PRIMARY KEY,
                      short_title TEXT, download_id TEXT, asset_id TEXT,
                      geometry_status TEXT, analyzer TEXT, analyzer_version TEXT,
                      assessed_at_utc TEXT, checks_json TEXT, details_json TEXT,
                      error TEXT, local_relative_path TEXT, file_format TEXT,
                      assessment_scope TEXT, series_instance_uid TEXT,
                      study_instance_uid TEXT, dimension INTEGER,
                      shape_json TEXT, spacing_json TEXT, origin_json TEXT,
                      direction_json TEXT
                    )
                    """
                )
                base = (
                    "", "checked_grid_geometry", "test", "1",
                    "2026-01-01T00:00:00Z", "{}", "{}", "",
                )
                geometry = ("file", "", "", 3, "[]", "[]", "[]", "[]")
                source.execute(
                    "INSERT INTO geometry_assessments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (1, "Aggregate", "10", *base, "extracted/case.nii.gz", "NIFTI", *geometry),
                )
                source.execute(
                    "INSERT INTO geometry_assessments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        2, "Series", "20", *base,
                        "wrapper/Series/package/case/T1w", "DICOM",
                        "dicom_series", "1.2.3", "1.2", 3,
                        "[]", "[]", "[]", "[]",
                    ),
                )

            with sqlite3.connect(":memory:") as conn:
                conn.row_factory = sqlite3.Row
                conn.executescript(public_metadata.SCHEMA)
                public_metadata.insert_vocab(conn)
                values = {
                    "download_row_id": None,
                    "subject_id": "",
                    "subject_id_namespace": "",
                    "participant_link_status": "unavailable",
                    "asset_name": "",
                    "container_format": "",
                    "media_kind": "image_volume",
                    "spatial_dimensionality": "unknown",
                    "temporal_dimensionality": "unknown",
                    "imaging_domain": "radiology",
                    "modality": "MR",
                    "object_role": "source_image",
                    "represented_file_count": 1,
                    "size_bytes": 1,
                    "checksum": "",
                    "checksum_algorithm": "",
                    "representation_provenance_class": "submitted_original",
                    "source_system": "tcia_aspera",
                    "source_record_id": "",
                    "source_url": "",
                    "raw_values_json": "{}",
                    "provenance_json": "{}",
                    "quality_flag_json": "{}",
                }
                public_metadata.insert_asset(
                    conn,
                    {
                        **values,
                        "asset_id": "asset-download",
                        "dataset_type": "Collection",
                        "short_title": "Aggregate",
                        "download_id": "10",
                        "asset_granularity": "download",
                        "file_name": "",
                        "package_path": "",
                        "file_format": "NIFTI",
                    },
                )
                public_metadata.insert_asset(
                    conn,
                    {
                        **values,
                        "asset_id": "asset-series",
                        "dataset_type": "Collection",
                        "short_title": "Series",
                        "download_id": "20",
                        "asset_granularity": "participant_modality",
                        "file_name": "",
                        "package_path": "Series/package/case/T1w",
                        "file_format": "DICOM",
                    },
                )
                public_metadata.initialize_geometry_statuses(conn)
                counts = public_metadata.ingest_geometry_results(conn, result_db)
                self.assertEqual(counts["unmatched_rows"], 0)
                self.assertEqual(counts["excluded_dicom_rows"], 1)
                self.assertEqual(counts["download_asset_mapped_rows"], 1)
                self.assertEqual(counts["package_path_mapped_rows"], 0)
                self.assertEqual(counts["matched_rows"], 1)


if __name__ == "__main__":
    unittest.main()
