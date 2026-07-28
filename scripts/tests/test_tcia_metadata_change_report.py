#!/usr/bin/env python3

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_metadata_change_report.py"


class MetadataChangeReportTest(unittest.TestCase):
    def test_reports_new_dataset_and_screening_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_snapshot = root / "old-snapshot.sqlite"
            new_snapshot = root / "new-snapshot.sqlite"
            old_clinical = root / "old-clinical.sqlite"
            new_clinical = root / "new-clinical.sqlite"
            report = root / "report.md"
            self._snapshot(old_snapshot, include_new=False)
            self._snapshot(new_snapshot, include_new=True)
            self._clinical(old_clinical, include_review=False)
            self._clinical(new_clinical, include_review=True)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--snapshot-new",
                    str(new_snapshot),
                    "--snapshot-old",
                    str(old_snapshot),
                    "--clinical-new",
                    str(new_clinical),
                    "--clinical-old",
                    str(old_clinical),
                    "--markdown-out",
                    str(report),
                    "--github-actions",
                ],
                check=True,
                text=True,
                capture_output=True,
            )

            markdown = report.read_text()
            self.assertIn("Collection / NEW", markdown)
            self.assertIn("Clinical screening review queue", markdown)
            self.assertIn("`SCREEN`", markdown)
            self.assertIn("Breast Cancer", markdown)
            self.assertIn("Inferred rows applied", markdown)
            self.assertIn("Subjects suppressed", markdown)
            self.assertIn("Curated clinical screening resolutions", markdown)
            self.assertIn("`ACRIN-6698`", markdown)
            self.assertIn(
                "::warning title=TCIA metadata review::"
                "snapshot: agent_datasets added 1 row",
                result.stdout,
            )
            self.assertIn(
                "clinical screening review required: SCREEN",
                result.stdout,
            )
            self.assertIn(
                "clinical screening review resolved: ACRIN-6698",
                result.stdout,
            )

    @staticmethod
    def _snapshot(path: Path, *, include_new: bool) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE agent_datasets (
                dataset_type TEXT, short_title TEXT, title TEXT
            );
            CREATE TABLE agent_current_downloads (
                dataset_type TEXT, short_title TEXT, download_id TEXT
            );
            CREATE TABLE agent_datacite_dois (doi TEXT);
            CREATE TABLE agent_pathdb_slides (slide_id TEXT);
            INSERT INTO agent_datasets VALUES
                ('Collection', 'OLD', 'Old Dataset');
            """
        )
        if include_new:
            conn.execute(
                "INSERT INTO agent_datasets VALUES ('Collection', 'NEW', 'New Dataset')"
            )
        conn.commit()
        conn.close()

    @staticmethod
    def _clinical(path: Path, *, include_review: bool) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE clinical_sources (source_id TEXT);
            CREATE TABLE clinical_downloads (source_id TEXT);
            CREATE TABLE clinical_idc_tables (
                collection_id TEXT, table_name TEXT
            );
            CREATE TABLE clinical_imaging_subjects (subject_key TEXT);
            CREATE TABLE clinical_rows (source_row_id TEXT);
            CREATE TABLE clinical_facts (fact_id TEXT);
            CREATE TABLE clinical_subjects (subject_key TEXT);
            CREATE TABLE clinical_dataset_inferences (
                short_title TEXT,
                concept TEXT,
                raw_value TEXT,
                review_required INTEGER,
                review_reason TEXT,
                review_evidence TEXT,
                screening_signal TEXT,
                candidate_subjects INTEGER,
                subjects_applied INTEGER,
                subjects_suppressed INTEGER
            );
            CREATE TABLE clinical_build_warnings (warning_id INTEGER);
            """
        )
        if include_review:
            conn.execute(
                """INSERT INTO clinical_dataset_inferences VALUES
                   ('SCREEN', 'primary_diagnosis', 'Breast Cancer', 1,
                    'screening_single_diagnosis_without_non_cancer',
                    '', 'title:Screening', 500, 0, 500)"""
            )
            conn.execute(
                """INSERT INTO clinical_dataset_inferences VALUES
                   ('ACRIN-6698', 'primary_diagnosis', 'Breast Cancer', 0,
                    'screening_review_resolved_confirmed_diagnosis',
                    'TCIA confirms invasive breast cancer enrollment.',
                    'detailed_description:screened', 385, 385, 0)"""
            )
        conn.commit()
        conn.close()


if __name__ == "__main__":
    unittest.main()
