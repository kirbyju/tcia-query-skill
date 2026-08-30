import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import tcia_geometry_seed as seed


class GeometrySeedTests(unittest.TestCase):
    def test_release_workflow_verifies_filters_and_imports_seed(self):
        workflow = (SCRIPTS.parent / ".github/workflows/build-metadata-v2-preview.yml").read_text()
        self.assertIn("tcia_geometry_seed.py verify-release", workflow)
        self.assertIn("tcia_geometry_seed.py compare", workflow)
        self.assertIn("--reset-geometry", workflow)
        self.assertIn("public_non_dicom_metadata.py import-geometry", workflow)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.sqlite"
        with sqlite3.connect(self.source) as conn:
            seed.create_schema(conn)
            conn.execute("DROP TABLE geometry_jobs")
            conn.execute("DROP TABLE geometry_seed_meta")
            values = [
                1, 1, "test", "1", "2026-01-01T00:00:00Z", "job-a",
                "Collection", "Demo", "1", None, "case.nii.gz", "NIFTI",
                "file", None, None, "checked_grid_geometry", 3, "[]", "[]",
                "[]", "[]", "{}", "{}",
                "ImageFileError: Empty file: '/scratch/private/case.nii.gz'",
            ]
            conn.execute(
                f"INSERT INTO geometry_assessments VALUES ({','.join('?' for _ in values)})",
                values,
            )
            values[0] = 2
            values[10] = "series"
            values[11] = "DICOM"
            values[-1] = None
            conn.execute(
                f"INSERT INTO geometry_assessments VALUES ({','.join('?' for _ in values)})",
                values,
            )
        self.jobs = self.root / "jobs.csv"
        self.write_jobs([
            {
                "array_index": "0", "job_id": "job-a", "dataset_type": "Collection",
                "short_title": "Demo", "download_id": "1", "route_type": "http",
                "formats": "DICOM;NIFTI", "asset_rows": "1",
                "catalog_represented_file_count": "1", "catalog_size_bytes": "10",
            }
        ])

    def tearDown(self):
        self.temp.cleanup()

    def write_jobs(self, rows):
        with self.jobs.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader(); writer.writerows(rows)

    def test_build_excludes_dicom_and_sanitizes_absolute_error(self):
        out = self.root / "seed.sqlite"
        archive = self.root / "seed.sqlite.gz"
        manifest = self.root / "manifest.json"
        result = seed.build_seed(
            self.source, self.jobs, [], out, archive, manifest, "test-tag", True
        )
        self.assertTrue(result["validation"]["ok"])
        self.assertEqual(result["manifest"]["assessment_rows"], 1)
        with sqlite3.connect(out) as conn:
            row = conn.execute(
                "SELECT file_format,error FROM geometry_assessments"
            ).fetchone()
            formats = conn.execute("SELECT formats_json FROM geometry_jobs").fetchone()[0]
        self.assertEqual(row[0], "NIFTI")
        self.assertNotIn("/scratch/", row[1])
        self.assertEqual(json.loads(formats), ["NIFTI"])
        verified = self.root / "verified.sqlite"
        self.assertTrue(
            seed.verify_release(manifest, archive, verified, None)["validation"]["ok"]
        )

    def test_compare_filters_changed_new_and_removed_jobs(self):
        built = self.root / "seed.sqlite"
        seed.build_seed(self.source, self.jobs, [], built, None, None, "test-tag", True)
        self.write_jobs([
            {
                "array_index": "0", "job_id": "job-a", "dataset_type": "Collection",
                "short_title": "Demo", "download_id": "1", "route_type": "http",
                "formats": "NIFTI", "asset_rows": "1",
                "catalog_represented_file_count": "1", "catalog_size_bytes": "11",
            },
            {
                "array_index": "1", "job_id": "job-b", "dataset_type": "Collection",
                "short_title": "New", "download_id": "2", "route_type": "http",
                "formats": "NIFTI", "asset_rows": "1",
                "catalog_represented_file_count": "1", "catalog_size_bytes": "5",
            },
        ])
        filtered = self.root / "filtered.sqlite"
        report = seed.compare_scope(
            built, self.jobs, filtered, self.root / "report.json", None, None, None
        )
        self.assertEqual(report["status_counts"], {"changed": 1, "new": 1})
        self.assertTrue(report["refresh_required"])
        with sqlite3.connect(filtered) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM geometry_assessments").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
