import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import tcia_public_non_dicom_metadata as public_metadata


class GeometryArtifactIntegrationTests(unittest.TestCase):
    def test_defaults_and_imported_assessment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_db = root / "geometry.sqlite"
            with sqlite3.connect(result_db) as conn:
                conn.execute(
                    """
                    CREATE TABLE geometry_assessments (
                      assessment_id INTEGER PRIMARY KEY,
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
                    "INSERT INTO geometry_assessments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        1,
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
                    "INSERT INTO geometry_assessments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        2,
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


if __name__ == "__main__":
    unittest.main()
