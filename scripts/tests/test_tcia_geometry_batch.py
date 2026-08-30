import gzip
import json
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.tcia_geometry_batch import (
    canonical_download_ids,
    dicom_series_geometry,
    detect_route,
    download_job,
    matrix_is_orthonormal,
    merge_results,
    load_asset_manifest,
    load_job,
    map_asset_id,
    plan_jobs,
)


class GeometryBatchTests(unittest.TestCase):
    def test_canonical_download_ids(self):
        self.assertEqual(canonical_download_ids('["2", "1"]'), ("1", "2"))
        self.assertEqual(canonical_download_ids("7;8"), ("7", "8"))
        self.assertEqual(canonical_download_ids(9), ("9",))

    def test_matrix_orthonormal(self):
        self.assertTrue(matrix_is_orthonormal([1, 0, 0, 1], 2))
        self.assertFalse(matrix_is_orthonormal([1, 0, 1, 0], 2))

    def test_legacy_looking_faspex_link_uses_current_tcia_service(self):
        self.assertEqual(
            detect_route("https://example.org/aspera/faspex/public/package?context=x"),
            "aspera_faspex5_public_link",
        )
        self.assertEqual(
            detect_route("https://faspex.example.org/?context=x"),
            "aspera_faspex5_public_link",
        )

    def test_faspex_download_routes_use_faspex5_public_link_workflow(self):
        base = {
            "short_title": "Demo",
            "job_id": "job_1",
            "source_url": "https://example.org/?context=x",
        }
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "scripts.tcia_geometry_batch.shutil.which", return_value="/usr/bin/ascli"
        ), mock.patch("scripts.tcia_geometry_batch.run_checked") as run:
            root = Path(temp_dir)
            download_job(
                {**base, "route_type": "aspera_faspex4_public_link"}, root
            )
            self.assertEqual(
                run.call_args.args[0],
                ["ascli", "faspex5", "packages", "receive", "--url=https://example.org/?context=x"],
            )
            target = root / "Demo--job_1"
            (target / ".download_complete.json").unlink()
            download_job(
                {**base, "route_type": "aspera_faspex5_public_link"}, root
            )
            self.assertEqual(
                run.call_args.args[0],
                ["ascli", "faspex5", "packages", "receive", "--url=https://example.org/?context=x"],
            )

    def test_map_asset_id_uses_unique_suffix_or_download_asset(self):
        root = Path("/tmp/job")
        suffix_asset = {
            "asset_id": "asset-series",
            "asset_granularity": "participant_modality",
            "package_path": "Demo/package/case/T1w",
            "file_format": "DICOM",
        }
        self.assertEqual(
            map_asset_id(
                root / "wrapper/Demo/package/case/T1w/1.dcm",
                root,
                [suffix_asset],
                "DICOM",
            ),
            "asset-series",
        )
        self.assertEqual(
            map_asset_id(
                root / "extracted/case.nii.gz",
                root,
                [
                    {
                        "asset_id": "asset-download",
                        "asset_granularity": "download",
                        "package_path": "",
                        "file_format": "NIFTI",
                    }
                ],
                "NIFTI",
            ),
            "asset-download",
        )

    def test_regular_dicom_series(self):
        instances = []
        for index in range(3):
            instances.append(
                {
                    "orientation": [1, 0, 0, 0, 1, 0],
                    "position": [0, 0, float(index)],
                    "pixel_spacing": [1, 1],
                    "rows": 8,
                    "columns": 8,
                    "number_of_frames": 1,
                    "sop_class_uid": "1.2.840.10008.5.1.4.1.1.4",
                    "excluded_localizer_or_mip": False,
                }
            )
        result = dicom_series_geometry(instances)
        self.assertEqual(result["geometry_status"], "checked_regular")
        self.assertTrue(result["checks"]["uniform_slice_spacing"])

    def test_plan_excludes_non_open_location_and_hides_urls_from_csv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "detail.sqlite"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE public_non_dicom_assets (
                  asset_id TEXT, dataset_type TEXT, short_title TEXT,
                  download_row_id INTEGER, download_id TEXT, subject_id TEXT,
                  subject_id_namespace TEXT, participant_link_status TEXT,
                  asset_granularity TEXT, asset_name TEXT, file_name TEXT,
                  package_path TEXT, file_format TEXT, container_format TEXT,
                  media_kind TEXT, spatial_dimensionality TEXT,
                  temporal_dimensionality TEXT, imaging_domain TEXT,
                  modality TEXT, object_role TEXT, represented_file_count INTEGER,
                  size_bytes INTEGER, checksum TEXT, checksum_algorithm TEXT,
                  representation_provenance_class TEXT, source_system TEXT,
                  source_record_id TEXT, source_url TEXT, raw_values_json TEXT,
                  provenance_json TEXT, quality_flag_json TEXT
                );
                CREATE TABLE public_non_dicom_locations (
                  location_id TEXT, asset_id TEXT, managed_system TEXT,
                  system_functions_json TEXT, access_url TEXT, viewer_url TEXT,
                  manifest_url TEXT, bucket TEXT, object_key TEXT,
                  access_level TEXT, availability_status TEXT,
                  representation_provenance_class TEXT, equivalence_status TEXT,
                  checksum TEXT, checksum_algorithm TEXT, observed_at TEXT,
                  provenance_json TEXT
                );
                """
            )
            base = [
                "Collection", "Demo", None, "", "", "", "unavailable", "download",
                "", "", "", "NIFTI", "ZIP", "image_volume", "unknown", "unknown",
                "radiology", "MR", "source_image", 2, 100, "", "", "submitted_original",
                "tcia_wordpress", "", "https://example.org/demo.zip", "{}", "{}", "{}",
            ]
            conn.execute(
                "INSERT INTO public_non_dicom_assets VALUES (" + ",".join("?" for _ in range(31)) + ")",
                ["download-open", *base[:3], "11", *base[4:]],
            )
            conn.execute(
                "INSERT INTO public_non_dicom_locations VALUES (" + ",".join("?" for _ in range(17)) + ")",
                ["loc", "download-open", "tcia_wordpress", "[]", "https://example.org/demo.zip", "", "", "", "", "open", "observed", "submitted_original", "unresolved", "", "", "", "{}"],
            )
            conn.commit()
            conn.close()
            summary = plan_jobs(db, root / "plan", ["NIFTI"])
            self.assertEqual(summary["job_count"], 1)
            csv_text = (root / "plan" / "jobs.csv").read_text()
            self.assertNotIn("https://", csv_text)
            private_text = (root / "plan" / "jobs.private.jsonl").read_text()
            self.assertIn("https://example.org/demo.zip", private_text)
            moved = root / "moved-plan"
            shutil.move(root / "plan", moved)
            job = load_job(moved / "jobs.private.jsonl", 0)
            self.assertTrue((moved / job["asset_manifest"]).is_file())
            self.assertEqual(load_asset_manifest(job), [])

    def test_merge_results_creates_integrity_checked_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            results = root / "results"
            results.mkdir()
            row = {
                "schema_version": 1,
                "analyzer": "test",
                "analyzer_version": "1",
                "assessed_at_utc": "2026-01-01T00:00:00Z",
                "job_id": "job_1",
                "dataset_type": "Collection",
                "short_title": "Demo",
                "download_id": "1",
                "asset_id": "asset_1",
                "local_relative_path": "image.nii.gz",
                "file_format": "NIFTI",
                "assessment_scope": "file",
                "series_instance_uid": "",
                "study_instance_uid": "",
                "geometry_status": "checked_grid_geometry",
                "dimension": 3,
                "shape": [2, 2, 2],
                "spacing": [1, 1, 1],
                "origin": [0, 0, 0],
                "direction": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                "checks": {"finite_affine": True},
                "details": {},
                "error": "",
            }
            with gzip.open(results / "job_1.jsonl.gz", "wt") as stream:
                stream.write(json.dumps(row) + "\n")
            output = root / "geometry.sqlite"
            summary = merge_results(results, output)
            self.assertEqual(summary["assessment_rows"], 1)
            self.assertEqual(summary["sqlite_integrity"], "ok")
            with sqlite3.connect(output) as conn:
                status = conn.execute(
                    "SELECT geometry_status FROM geometry_assessments"
                ).fetchone()[0]
            self.assertEqual(status, "checked_grid_geometry")


if __name__ == "__main__":
    unittest.main()
