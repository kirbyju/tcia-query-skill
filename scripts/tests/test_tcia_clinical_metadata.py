#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_clinical_metadata.py"
SPEC = importlib.util.spec_from_file_location("tcia_clinical_metadata", SCRIPT)
assert SPEC and SPEC.loader
CLINICAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLINICAL)


class ClinicalMetadataTest(unittest.TestCase):
    @staticmethod
    def _victre_archive(
        density: str, members: dict[str, str]
    ) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for seed, content in members.items():
                payload = content.encode("utf-8")
                info = tarfile.TarInfo(
                    f"{density}/SP/pcl_{seed}_crop.loc"
                )
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        return buffer.getvalue()

    def test_schema_v4_source_rows_are_reused_with_patient_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = root / "v4.sqlite"
            old = sqlite3.connect(previous)
            old.executescript(
                """
                CREATE TABLE clinical_sources (
                    source_id TEXT PRIMARY KEY, source_kind TEXT,
                    source_priority INTEGER, source_lineage TEXT,
                    short_title TEXT, source_url TEXT, source_date TEXT,
                    source_signature TEXT, artifact_sha256 TEXT,
                    artifact_bytes INTEGER, provenance_json TEXT
                );
                CREATE TABLE clinical_rows (
                    source_row_id TEXT PRIMARY KEY, source_id TEXT,
                    short_title TEXT, subject_id TEXT, subject_key TEXT,
                    table_name TEXT, row_number INTEGER, has_imaging INTEGER,
                    row_json TEXT, row_sha256 TEXT
                );
                CREATE TABLE clinical_facts (
                    fact_id TEXT PRIMARY KEY, source_row_id TEXT, source_id TEXT,
                    source_kind TEXT, source_priority INTEGER, short_title TEXT,
                    subject_id TEXT, subject_key TEXT, concept TEXT,
                    value_text TEXT, value_normalized TEXT, value_number REAL,
                    unit TEXT, original_column TEXT, provenance_json TEXT
                );
                CREATE TABLE clinical_build_warnings (
                    warning_id INTEGER PRIMARY KEY, source_id TEXT,
                    short_title TEXT, warning_type TEXT, warning_text TEXT
                );
                INSERT INTO clinical_sources VALUES
                    ('cda:test', 'cda', 200, 'cda:test', 'TEST', '', '',
                     'sig', '', NULL, '{}');
                INSERT INTO clinical_rows VALUES
                    ('row', 'cda:test', 'TEST', 'SUB-1', 'test:sub-1',
                     'subject', 1, 1, '{}', 'hash');
                INSERT INTO clinical_facts VALUES
                    ('fact', 'row', 'cda:test', 'cda', 200, 'TEST', 'SUB-1',
                     'test:sub-1', 'primary_site', 'Lung', 'lung', NULL,
                     NULL, 'primary_site', '{}');
                """
            )
            old.commit()
            old.close()

            conn = CLINICAL.init_db(root / "v5.sqlite", replace=True)
            self.assertTrue(
                CLINICAL.copy_source_from_previous(conn, previous, "cda:test")
            )
            fact = conn.execute(
                """SELECT evidence_scope, is_inferred
                   FROM clinical_facts WHERE fact_id = 'fact'"""
            ).fetchone()
            self.assertEqual(tuple(fact), ("patient", 0))
            conn.close()

    def test_wordpress_single_label_fallback_is_explicit_and_non_overwriting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.sqlite"
            snapshot_conn = sqlite3.connect(snapshot)
            snapshot_conn.executescript(
                """
                CREATE TABLE agent_datasets (
                    short_title TEXT,
                    title TEXT,
                    link TEXT,
                    date_updated TEXT,
                    dataset_type TEXT,
                    hidden INTEGER,
                    cancer_types TEXT,
                    cancer_locations TEXT,
                    summary TEXT,
                    abstract TEXT,
                    detailed_description TEXT
                );
                INSERT INTO agent_datasets VALUES
                    ('TEST', 'Test', 'https://example.test/test', '2026-01-01',
                     'Collection', 0, 'Lung Cancer', 'Lung', '', '', ''),
                    ('MULTI', 'Multi', '', '', 'Collection', 0,
                     'Cancer A; Cancer B', 'Abdomen; Pelvis', '', '', ''),
                    ('GENERIC', 'Generic', '', '', 'Collection', 0,
                     'Various', 'Various', '', '', ''),
                    ('SCREEN', 'Screening cohort', '', '', 'Collection', 0,
                     'Breast Cancer', 'Breast',
                     'Women undergoing screening for cancer detection', '', ''),
                    ('SCREEN-MIXED', 'Screening mixed cohort', '', '',
                     'Collection', 0, 'Breast Cancer; Non-Cancer', 'Breast',
                     'Screening cases with positive and negative results', '', ''),
                    ('ACRIN-6698', 'ACRIN 6698/I-SPY2 Breast DWI', '', '',
                     'Collection', 0, 'Breast Cancer', 'Breast',
                     '406 women with invasive breast cancer were prospectively enrolled after screening for eligibility.',
                     '', ''),
                    ('IvyGAP', 'Ivy Glioblastoma Atlas Project', '', '',
                     'Collection', 0, 'Glioblastoma', 'Brain',
                     'Gene-expression primary screen of confirmed tumors',
                     '', ''),
                    ('RESULT', 'Result', '', '', 'Analysis Result', 0,
                     'Brain Cancer', 'Brain', '', '', '');
                """
            )
            snapshot_conn.commit()
            snapshot_conn.close()

            conn = CLINICAL.init_db(root / "clinical.sqlite", replace=True)
            for subject_id in ("SUB-1", "SUB-2"):
                conn.execute(
                    """INSERT INTO clinical_imaging_subjects
                       VALUES (?, 'TEST', ?, 'test')""",
                    (f"test:{subject_id.lower()}", subject_id),
                )
            for subject_id in ("POSITIVE", "UNKNOWN"):
                conn.execute(
                    """INSERT INTO clinical_imaging_subjects
                       VALUES (?, 'SCREEN', ?, 'test')""",
                    (f"screen:{subject_id.lower()}", subject_id),
                )
            conn.execute(
                """INSERT INTO clinical_imaging_subjects
                   VALUES ('screen-mixed:unknown', 'SCREEN-MIXED',
                           'MIXED-UNKNOWN', 'test')"""
            )
            conn.execute(
                """INSERT INTO clinical_imaging_subjects
                   VALUES ('acrin6698:subject-1', 'ACRIN-6698',
                           'ACRIN-6698-SUBJECT-1', 'test')"""
            )
            conn.execute(
                """INSERT INTO clinical_imaging_subjects
                   VALUES ('ivygap:w1', 'IvyGAP', 'W1', 'test')"""
            )
            CLINICAL.insert_source(
                conn,
                source_id="idc:test",
                source_kind="idc_clinical",
                short_title="TEST",
                source_signature_value="test",
            )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="idc:test",
                source_kind="idc_clinical",
                short_title="TEST",
                subject_id="SUB-1",
                table_name="test",
                row_number=1,
                row={"dicom_patient_id": "SUB-1", "diagnosis": "Adenocarcinoma"},
                facts=[
                    (
                        "primary_diagnosis",
                        "Adenocarcinoma",
                        "diagnosis",
                        None,
                    )
                ],
                has_imaging=True,
            )
            CLINICAL.insert_source(
                conn,
                source_id="idc:screen",
                source_kind="idc_clinical",
                short_title="SCREEN",
                source_signature_value="screen",
            )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="idc:screen",
                source_kind="idc_clinical",
                short_title="SCREEN",
                subject_id="POSITIVE",
                table_name="screen",
                row_number=1,
                row={"subject_id": "POSITIVE", "diagnosis": "Breast carcinoma"},
                facts=[
                    (
                        "primary_diagnosis",
                        "Breast carcinoma",
                        "diagnosis",
                        None,
                    )
                ],
                has_imaging=True,
            )

            result = CLINICAL.apply_wordpress_dataset_inferences(conn, snapshot)
            CLINICAL.materialize_subjects(conn)

            self.assertEqual(result["collections"], 7)
            self.assertEqual(result["eligible_labels"], 7)
            self.assertEqual(result["screening_reviews_required"], 1)
            self.assertEqual(result["screening_reviews_resolved"], 2)
            subjects = {
                row["subject_id"]: row
                for row in conn.execute(
                    """SELECT * FROM agent_clinical_subjects
                       WHERE short_title = 'TEST'"""
                )
            }
            self.assertEqual(
                subjects["SUB-1"]["primary_diagnosis"], "Adenocarcinoma"
            )
            self.assertEqual(subjects["SUB-1"]["primary_site"], "Lung")
            self.assertEqual(subjects["SUB-2"]["primary_diagnosis"], "Lung Cancer")
            self.assertEqual(subjects["SUB-2"]["primary_site"], "Lung")
            self.assertEqual(
                subjects["SUB-1"]["primary_diagnosis_is_inferred"], 0
            )
            self.assertEqual(subjects["SUB-1"]["primary_site_is_inferred"], 1)
            self.assertEqual(
                subjects["SUB-2"]["primary_diagnosis_is_inferred"], 1
            )
            inferred_fact = conn.execute(
                """SELECT evidence_scope, is_inferred
                   FROM agent_clinical_facts
                   WHERE short_title = 'TEST' AND subject_id = 'SUB-2'
                     AND concept = 'primary_diagnosis'"""
            ).fetchone()
            self.assertEqual(tuple(inferred_fact), ("dataset", 1))
            audit = conn.execute(
                """SELECT eligibility_reason, candidate_subjects,
                          subjects_applied, subjects_suppressed
                   FROM agent_clinical_dataset_inferences
                   WHERE short_title = 'TEST'
                     AND concept = 'primary_diagnosis'"""
            ).fetchone()
            self.assertEqual(tuple(audit), ("eligible", 2, 1, 0))
            screening_audit = conn.execute(
                """SELECT eligibility_reason, review_required, review_reason,
                          screening_signal, candidate_subjects,
                          subjects_applied, subjects_suppressed
                   FROM agent_clinical_dataset_inferences
                   WHERE short_title = 'SCREEN'
                     AND concept = 'primary_diagnosis'"""
            ).fetchone()
            self.assertEqual(
                tuple(screening_audit),
                (
                    "screening_review_required",
                    1,
                    "screening_single_diagnosis_without_non_cancer",
                    "short_title:SCREEN",
                    2,
                    0,
                    1,
                ),
            )
            self.assertEqual(
                conn.execute(
                    """SELECT primary_diagnosis FROM agent_clinical_subjects
                       WHERE short_title = 'SCREEN'
                         AND subject_id = 'POSITIVE'"""
                ).fetchone()[0],
                "Breast carcinoma",
            )
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_facts
                       WHERE short_title = 'SCREEN'
                         AND source_kind = 'wordpress_dataset_inference'"""
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_build_warnings
                       WHERE short_title = 'SCREEN'
                         AND warning_type =
                             'screening_dataset_review_required'"""
                ).fetchone()[0],
                1,
            )
            acrin_audit = conn.execute(
                """SELECT eligibility_reason, review_required, review_reason,
                          review_evidence, subjects_applied
                   FROM agent_clinical_dataset_inferences
                   WHERE short_title = 'ACRIN-6698'
                     AND concept = 'primary_diagnosis'"""
            ).fetchone()
            self.assertEqual(acrin_audit["eligibility_reason"], "eligible")
            self.assertEqual(acrin_audit["review_required"], 0)
            self.assertEqual(
                acrin_audit["review_reason"],
                "screening_review_resolved_confirmed_diagnosis",
            )
            self.assertIn("406 women", acrin_audit["review_evidence"])
            self.assertEqual(acrin_audit["subjects_applied"], 1)
            acrin_subject = conn.execute(
                """SELECT primary_diagnosis, primary_site,
                          primary_diagnosis_is_inferred,
                          primary_site_is_inferred
                   FROM agent_clinical_subjects
                   WHERE short_title = 'ACRIN-6698'"""
            ).fetchone()
            self.assertEqual(
                tuple(acrin_subject),
                ("Breast Cancer", "Breast", 1, 1),
            )
            ivygap_subject = conn.execute(
                """SELECT primary_diagnosis, primary_site
                   FROM agent_clinical_subjects
                   WHERE short_title = 'IvyGAP'"""
            ).fetchone()
            self.assertEqual(tuple(ivygap_subject), ("Glioblastoma", "Brain"))
            ivygap_audit = conn.execute(
                """SELECT review_required, review_reason, review_evidence
                   FROM agent_clinical_dataset_inferences
                   WHERE short_title = 'IvyGAP'
                     AND concept = 'primary_diagnosis'"""
            ).fetchone()
            self.assertEqual(ivygap_audit["review_required"], 0)
            self.assertEqual(
                ivygap_audit["review_reason"],
                "screening_review_resolved_confirmed_diagnosis",
            )
            self.assertIn(
                "gene-expression experimental screens",
                ivygap_audit["review_evidence"],
            )
            self.assertEqual(
                conn.execute(
                    """SELECT subjects_suppressed
                       FROM agent_clinical_dataset_inferences
                       WHERE short_title = 'SCREEN'
                         AND concept = 'primary_site'"""
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    """SELECT subjects_suppressed
                       FROM agent_clinical_dataset_inferences
                       WHERE short_title = 'SCREEN-MIXED'
                         AND concept = 'primary_site'"""
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_build_warnings
                       WHERE short_title = 'SCREEN-MIXED'
                         AND warning_type =
                           'dataset_site_inference_suppressed_without_diagnosis'"""
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_facts
                       WHERE short_title IN
                             ('MULTI', 'GENERIC', 'SCREEN-MIXED', 'RESULT')"""
                ).fetchone()[0],
                0,
            )
            conn.close()

    def test_victre_patient_ground_truth_classifies_mixed_synthetic_cohort(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conn = CLINICAL.init_db(root / "clinical.sqlite", replace=True)
            index_rows = [
                {
                    "collection_id": "victre",
                    "PatientID": "101",
                    "PatientSex": "F",
                    "StudyDescription": "Simulated Digital Mammography",
                    "SeriesDescription": (
                        "mcgpu_image_pc_-101_crop.raw.gz-fatty_0000"
                    ),
                },
                {
                    "collection_id": "victre",
                    "PatientID": "202",
                    "PatientSex": "F",
                    "StudyDescription": "Simulated Digital Mammography",
                    "SeriesDescription": (
                        "mcgpu_image_pcl_202_crop.raw.gz-hetero_0000"
                    ),
                },
                {
                    "collection_id": "victre",
                    "PatientID": "303",
                    "PatientSex": "F",
                    "StudyDescription": "Simulated Digital Mammography",
                    "SeriesDescription": (
                        "mcgpu_image_pcl_-303_crop.raw.gz-dense_0000"
                    ),
                },
            ]
            artifacts = {
                "dense": self._victre_archive("dense", {"-303": ""}),
                "fatty": self._victre_archive("fatty", {}),
                "hetero": self._victre_archive(
                    "hetero",
                    {
                        "202": (
                            "1 2 3 0\n4 5 6 0\n7 8 9 1\n"
                            "10 11 12 1\n"
                        )
                    },
                ),
                "scattered": self._victre_archive("scattered", {}),
            }

            def fetcher(url: str, **_: object) -> bytes:
                if url == CLINICAL.VICTRE_LOCATION_README_URL:
                    return b"lesionFlag: 0 calcification, 1 mass"
                for density, archive_url in (
                    CLINICAL.VICTRE_LOCATION_ARCHIVE_URLS.items()
                ):
                    if url == archive_url:
                        return artifacts[density]
                raise AssertionError(url)

            result = CLINICAL.ingest_victre_external_clinical(
                conn,
                index_rows=index_rows,
                idc_version="v-test",
                fetcher=fetcher,
            )
            CLINICAL.materialize_subjects(conn)

            self.assertEqual(result["status"], "loaded")
            self.assertEqual(result["subjects"], 3)
            self.assertEqual(result["lesion_present_subjects"], 2)
            self.assertEqual(result["lesion_absent_subjects"], 1)
            self.assertEqual(result["cancer_subjects"], 1)
            self.assertEqual(result["non_cancer_subjects"], 1)
            self.assertEqual(result["unresolved_subject_ids"], ["303"])
            resolved = {
                row["subject_id"]: row
                for row in conn.execute(
                    """SELECT * FROM agent_clinical_subjects
                       WHERE short_title = 'VICTRE'"""
                )
            }
            self.assertEqual(resolved["101"]["primary_diagnosis"], "Non-Cancer")
            self.assertIsNone(resolved["101"]["primary_site"])
            self.assertEqual(resolved["202"]["primary_diagnosis"], "Breast Cancer")
            self.assertEqual(resolved["202"]["primary_site"], "Breast")
            self.assertIsNone(resolved["303"]["primary_diagnosis"])
            self.assertEqual(
                conn.execute(
                    """SELECT value_number FROM clinical_facts
                       WHERE short_title = 'VICTRE' AND subject_id = '202'
                         AND concept = 'microcalcification_count'"""
                ).fetchone()[0],
                2.0,
            )
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_build_warnings
                       WHERE warning_type = 'victre_signal_location_conflict'"""
                ).fetchone()[0],
                1,
            )
            conn.close()

    def test_victre_screening_resolution_never_uses_dataset_diagnosis(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.sqlite"
            snapshot_conn = sqlite3.connect(snapshot)
            snapshot_conn.executescript(
                """
                CREATE TABLE agent_datasets (
                    short_title TEXT, title TEXT, link TEXT, date_updated TEXT,
                    dataset_type TEXT, hidden INTEGER, cancer_types TEXT,
                    cancer_locations TEXT, summary TEXT, abstract TEXT,
                    detailed_description TEXT
                );
                INSERT INTO agent_datasets VALUES
                    ('VICTRE', 'VICTRE', '', '2026-01-01', 'Collection', 0,
                     'Breast Cancer', 'Breast',
                     'Virtual breast cancer screening trial', '', '');
                """
            )
            snapshot_conn.commit()
            snapshot_conn.close()
            conn = CLINICAL.init_db(root / "clinical.sqlite", replace=True)
            conn.execute(
                """INSERT INTO clinical_imaging_subjects VALUES
                   ('victre:unknown', 'VICTRE', 'UNKNOWN', 'test')"""
            )
            result = CLINICAL.apply_wordpress_dataset_inferences(conn, snapshot)
            audit = conn.execute(
                """SELECT eligibility_reason, review_required, review_reason,
                          subjects_applied, subjects_suppressed
                   FROM clinical_dataset_inferences
                   WHERE short_title = 'VICTRE'
                     AND concept = 'primary_diagnosis'"""
            ).fetchone()
            self.assertEqual(result["screening_reviews_resolved"], 1)
            self.assertEqual(
                tuple(audit),
                (
                    "screening_patient_level_only",
                    0,
                    "screening_review_resolved_patient_level_mixed_cohort",
                    0,
                    1,
                ),
            )
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_facts
                       WHERE short_title = 'VICTRE'"""
                ).fetchone()[0],
                0,
            )
            conn.close()

    def test_acrin_columns_map_to_precise_common_concepts(self) -> None:
        self.assertEqual(
            CLINICAL.concept_for_source_column("ACRIN-6698", "age"),
            "age_at_enrollment_years",
        )
        self.assertEqual(
            CLINICAL.concept_for_source_column("ACRIN-6698", "SBRgrade"),
            "grade",
        )
        self.assertEqual(
            CLINICAL.concept_for_source_column("ACRIN-6698", "pcr"),
            "response",
        )
        self.assertEqual(
            CLINICAL.choose_subject_column(
                ["I-SPY 2 Research ID", "TCIA PATIENT ID", "age"]
            ),
            "TCIA PATIENT ID",
        )
        self.assertIsNone(
            CLINICAL.choose_subject_column(
                [
                    "data dictionary for ACRIN_6698_Patient_Cohorts_ClinicalData file",
                    "Unnamed: 1",
                ]
            )
        )

    def test_ct_colonography_patient_histology_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(
                Path(directory) / "ctc.sqlite", replace=True
            )
            for subject_id in (
                "MALIGNANT",
                "ADENOMA",
                "NEGATIVE",
                "OTHER",
                "NO-SPREADSHEET",
            ):
                conn.execute(
                    """INSERT INTO clinical_imaging_subjects
                       VALUES (?, 'CT COLONOGRAPHY', ?, 'test')""",
                    (f"ctcolonography:{subject_id.lower()}", subject_id),
                )
            CLINICAL.insert_source(
                conn,
                source_id="tcia-download:ctc:test",
                source_kind="tcia_clinical_download",
                short_title="CT COLONOGRAPHY",
                source_signature_value="test",
            )
            for row_number, (subject_id, facts) in enumerate(
                [
                    (
                        "MALIGNANT",
                        [
                            (
                                "lesion_histology_code",
                                "1",
                                "LESION 1.5",
                                None,
                            )
                        ],
                    ),
                    (
                        "ADENOMA",
                        [
                            (
                                "lesion_histology_code",
                                "13",
                                "LESION 1.5",
                                None,
                            )
                        ],
                    ),
                    (
                        "NEGATIVE",
                        [
                            (
                                "screening_result",
                                "No polyp found",
                                "download_title",
                                None,
                            )
                        ],
                    ),
                    (
                        "OTHER",
                        [
                            (
                                "lesion_histology_code",
                                "88",
                                "LESION 1.5",
                                None,
                            ),
                            (
                                "screening_result",
                                "No polyp found",
                                "download_title",
                                None,
                            ),
                        ],
                    ),
                ],
                start=1,
            ):
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id="tcia-download:ctc:test",
                    source_kind="tcia_clinical_download",
                    short_title="CT COLONOGRAPHY",
                    subject_id=subject_id,
                    table_name="test",
                    row_number=row_number,
                    row={"subject_id": subject_id},
                    facts=facts,
                    has_imaging=True,
                )

            result = (
                CLINICAL.derive_ct_colonography_patient_diagnoses(conn)
            )
            CLINICAL.materialize_subjects(conn)
            self.assertEqual(result["spreadsheet_subjects"], 4)
            self.assertEqual(result["classified_subjects"], 3)
            self.assertEqual(result["malignant_subjects"], 1)
            self.assertEqual(result["adenoma_subjects"], 1)
            self.assertEqual(result["negative_screening_subjects"], 1)
            self.assertEqual(result["indeterminate_subjects"], 1)
            self.assertEqual(result["imaging_without_spreadsheet"], 1)
            diagnoses = dict(
                conn.execute(
                    """SELECT subject_id, primary_diagnosis
                       FROM agent_clinical_subjects
                       WHERE short_title = 'CT COLONOGRAPHY'"""
                )
            )
            self.assertEqual(diagnoses["MALIGNANT"], "Adenocarcinoma")
            self.assertEqual(diagnoses["ADENOMA"], "Tubular adenoma")
            self.assertEqual(diagnoses["NEGATIVE"], "Non-Cancer")
            self.assertIsNone(diagnoses["OTHER"])
            conn.close()

    def test_ct_colonography_workbook_fields_are_recognized(self) -> None:
        facts = CLINICAL.ct_colonography_workbook_facts(
            "CT COLONOGRAPHY",
            "Polyp Descriptions - Large 10mm",
            {
                "TCIA Number": "SUB-1",
                "LESION 1.5": 1,
                "2.5": 13,
            },
        )
        self.assertIn(
            ("lesion_histology", "Adenocarcinoma", "LESION 1.5", None),
            facts,
        )
        self.assertIn(
            ("lesion_histology_code", "13", "2.5", None),
            facts,
        )
        negative_facts = CLINICAL.ct_colonography_workbook_facts(
            "CT COLONOGRAPHY",
            "Polyp Descriptions - No polyp found",
            {"TCIA Patient ID": "SUB-2"},
        )
        self.assertEqual(
            negative_facts,
            [
                (
                    "screening_result",
                    "No polyp found",
                    "download_title",
                    None,
                )
            ],
        )
        self.assertEqual(
            CLINICAL.choose_subject_column(["TCIA Number", "LESION 1.5"]),
            "TCIA Number",
        )
        self.assertTrue(
            CLINICAL.is_clinical_download(
                {
                    "download_title": "Polyp Descriptions - No polyp found",
                    "download_types": '["Image Annotations"]',
                    "data_types": '["Classification"]',
                    "description": "",
                }
            )
        )

    def test_ea1141_patient_screening_and_pathology_derivation(self) -> None:
        self.assertEqual(
            CLINICAL.choose_subject_column(["AGE", "SUBJECT_DE"]),
            "SUBJECT_DE",
        )
        self.assertEqual(
            CLINICAL.normalize_official_subject_id("EA1141", "1180574"),
            "ea1141-1180574",
        )
        decoded = CLINICAL.ea1141_workbook_facts(
            "EA1141",
            {
                "SUBJECT_DE": "POSITIVE",
                "AGE": "55",
                "SEX": "1",
                "RACE": "5",
                "ETHNICITY": "2",
                "YEAR0_SENSSPEC_REFSTD": "1",
                "MRI_LESIONOUTCOME_YR0": "Invasive",
                "MRI_LESIONOUTCOMEDETAIL_YR0": (
                    "Invasive (infiltrating) ductal carcinoma"
                ),
                "MRI_COREPATH_GRADE_YR0": "3",
                "MRI_SURGPATH_GRADE_YR0": "2",
            },
        )
        self.assertIn(
            ("sex_at_birth", "Female", "SEX", None),
            decoded,
        )
        self.assertIn(("race", "White", "RACE", None), decoded)
        self.assertIn(
            ("ethnicity", "Not Hispanic/Latino", "ETHNICITY", None),
            decoded,
        )
        self.assertIn(
            (
                "age_at_enrollment_years",
                "55",
                "AGE",
                None,
            ),
            decoded,
        )
        self.assertIn(
            (
                "screening_result",
                "Positive",
                "YEAR0_SENSSPEC_REFSTD",
                None,
            ),
            decoded,
        )

        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(
                Path(directory) / "ea1141.sqlite", replace=True
            )
            for subject_id in (
                "POSITIVE",
                "NEGATIVE",
                "WITHDRAWN",
                "MISSING",
            ):
                conn.execute(
                    """INSERT INTO clinical_imaging_subjects
                       VALUES (?, 'EA1141', ?, 'test')""",
                    (f"ea1141:{subject_id.lower()}", subject_id),
                )
            CLINICAL.insert_source(
                conn,
                source_id="tcia-download:ea1141:test",
                source_kind="tcia_clinical_download",
                short_title="EA1141",
                source_signature_value="test",
            )
            screening = {
                "POSITIVE": "Positive",
                "NEGATIVE": "Negative",
                "WITHDRAWN": "Withdrawn",
                "MISSING": "Missing",
            }
            for row_number, (subject_id, value) in enumerate(
                screening.items(), start=1
            ):
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id="tcia-download:ea1141:test",
                    source_kind="tcia_clinical_download",
                    short_title="EA1141",
                    subject_id=subject_id,
                    table_name="12month",
                    row_number=row_number,
                    row={"SUBJECT_DE": subject_id},
                    facts=[
                        (
                            "screening_result",
                            value,
                            "YEAR0_SENSSPEC_REFSTD",
                            None,
                        )
                    ],
                    has_imaging=True,
                )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="tcia-download:ea1141:test",
                source_kind="tcia_clinical_download",
                short_title="EA1141",
                subject_id="POSITIVE",
                table_name="mri_lesions",
                row_number=1,
                row={"SUBJECT_DE": "POSITIVE"},
                facts=[
                    (
                        "lesion_outcome",
                        "Invasive",
                        "MRI_LESIONOUTCOME_YR0",
                        None,
                    ),
                    (
                        "lesion_outcome_detail",
                        "Invasive (infiltrating) ductal carcinoma",
                        "MRI_LESIONOUTCOMEDETAIL_YR0",
                        None,
                    ),
                    (
                        "tumor_grade_core_code",
                        "3",
                        "MRI_COREPATH_GRADE_YR0",
                        None,
                    ),
                    (
                        "tumor_grade_surgical_code",
                        "2",
                        "MRI_SURGPATH_GRADE_YR0",
                        None,
                    ),
                ],
                has_imaging=True,
            )

            result = CLINICAL.derive_ea1141_patient_diagnoses(conn)
            CLINICAL.materialize_subjects(conn)
            self.assertEqual(result["official_subjects"], 4)
            self.assertEqual(result["classified_subjects"], 2)
            self.assertEqual(result["positive_subjects"], 1)
            self.assertEqual(result["negative_subjects"], 1)
            self.assertEqual(result["withdrawn_subjects"], 1)
            self.assertEqual(result["missing_subjects"], 1)
            subjects = {
                row["subject_id"]: row
                for row in conn.execute(
                    """SELECT * FROM agent_clinical_subjects
                       WHERE short_title = 'EA1141'"""
                )
            }
            self.assertEqual(
                subjects["POSITIVE"]["primary_diagnosis"],
                "Invasive (infiltrating) ductal carcinoma",
            )
            self.assertEqual(subjects["POSITIVE"]["primary_site"], "Breast")
            self.assertEqual(subjects["POSITIVE"]["grade"], "Grade II")
            self.assertEqual(
                subjects["NEGATIVE"]["primary_diagnosis"],
                "Non-Cancer",
            )
            self.assertIsNone(subjects["WITHDRAWN"]["primary_diagnosis"])
            self.assertIsNone(subjects["MISSING"]["primary_diagnosis"])
            conn.close()

    def test_hnscc_official_patient_union_and_columns(self) -> None:
        csv_id = "TCIA Radiomics dummy ID of To_Submit_Final"
        self.assertEqual(
            CLINICAL.choose_subject_column([csv_id, "Gender"]),
            csv_id,
        )
        self.assertEqual(
            CLINICAL.concept_for_source_column(
                "HNSCC", "Cancer subsite of origin"
            ),
            "primary_site",
        )
        self.assertEqual(
            CLINICAL.concept_for_source_column(
                "HNSCC", "AJCC Stage (7th edition)"
            ),
            "stage",
        )
        self.assertEqual(
            CLINICAL.hnscc_workbook_facts(
                "HNSCC",
                {"TCIA PatientID": "HNSCC-01-0001", "Histology": "SCC"},
            ),
            [
                (
                    "primary_diagnosis",
                    "Head and Neck Squamous Cell Carcinoma",
                    "Histology",
                    None,
                )
            ],
        )
        self.assertFalse(
            CLINICAL.is_clinical_download(
                {
                    "download_title": (
                        "Oropharyngeal-Radiomics-Outcomes Images"
                    ),
                    "download_types": '["Radiology Images"]',
                    "data_types": '["CT","RTSTRUCT"]',
                    "file_types": '["DICOM"]',
                    "description": "",
                }
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(
                Path(directory) / "hnscc.sqlite", replace=True
            )
            sources = [
                (
                    "tcia-download:hnscc:atlas",
                    (
                        "https://www.cancerimagingarchive.net/wp-content/"
                        "uploads/HNSCC-MDA-Data_update_20240514.xlsx"
                    ),
                    range(1, 216),
                ),
                (
                    "tcia-download:hnscc:radiomics",
                    (
                        "https://www.cancerimagingarchive.net/wp-content/"
                        "uploads/"
                        "Radiomics_Outcome_Prediction_in_OPC_ASRM_corrected.csv"
                    ),
                    range(136, 628),
                ),
            ]
            for source_id, source_url, subjects in sources:
                CLINICAL.insert_source(
                    conn,
                    source_id=source_id,
                    source_kind="tcia_clinical_download",
                    short_title="HNSCC",
                    source_signature_value=source_id,
                    source_url=source_url,
                )
                for row_number, number in enumerate(subjects, start=1):
                    subject_id = f"HNSCC-01-{number:04d}"
                    CLINICAL.insert_row_and_facts(
                        conn,
                        source_id=source_id,
                        source_kind="tcia_clinical_download",
                        short_title="HNSCC",
                        subject_id=subject_id,
                        table_name="test",
                        row_number=row_number,
                        row={"subject_id": subject_id},
                        facts=[],
                        has_imaging=False,
                    )

            CLINICAL.insert_source(
                conn,
                source_id="dicom:hnscc:legacy-extra",
                source_kind="dicom",
                short_title="HNSCC",
                source_signature_value="legacy-extra",
            )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="dicom:hnscc:legacy-extra",
                source_kind="dicom",
                short_title="HNSCC",
                subject_id="HNSCC-01-9999",
                table_name="legacy.idc_index",
                row_number=1,
                row={"subject_id": "HNSCC-01-9999"},
                facts=[],
                has_imaging=True,
            )
            conn.execute(
                """INSERT INTO clinical_imaging_subjects
                   (subject_key, short_title, subject_id, imaging_source)
                   VALUES (?, 'HNSCC', 'HNSCC-01-9999', 'legacy_idc_index')""",
                (
                    conn.execute(
                        """SELECT subject_key FROM clinical_rows
                           WHERE subject_id = 'HNSCC-01-9999'"""
                    ).fetchone()[0],
                ),
            )

            result = CLINICAL.promote_hnscc_official_cohort(conn)
            self.assertEqual(result["atlas_subjects"], 215)
            self.assertEqual(result["radiomics_subjects"], 492)
            self.assertEqual(result["overlap_subjects"], 80)
            self.assertEqual(result["union_subjects"], 627)
            self.assertEqual(result["promoted_imaging_subjects"], 627)
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_imaging_subjects
                       WHERE short_title = 'HNSCC'"""
                ).fetchone()[0],
                627,
            )
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_rows
                       WHERE short_title = 'HNSCC' AND has_imaging = 1"""
                ).fetchone()[0],
                707,
            )
            self.assertEqual(
                conn.execute(
                    """SELECT has_imaging FROM clinical_rows
                       WHERE subject_id = 'HNSCC-01-9999'"""
                ).fetchone()[0],
                0,
            )
            conn.close()

    def test_hungarian_colorectal_icd10_and_pathdb_crosscheck(self) -> None:
        self.assertIsNone(CLINICAL.choose_subject_column(["ID", "Age"]))
        self.assertEqual(
            CLINICAL.choose_subject_column(
                ["ID", "Age"], "Hungarian-Colorectal-Screening"
            ),
            "ID",
        )
        malignant = CLINICAL.hungarian_colorectal_workbook_facts(
            "Hungarian-Colorectal-Screening",
            {"ID": "1", "ICD10_health_status": "C18800"},
        )
        self.assertIn(
            ("icd10_code", "C18800", "ICD10_health_status", None),
            malignant,
        )
        self.assertIn(
            (
                "primary_diagnosis",
                "Malignant neoplasm of colon",
                "ICD10_health_status",
                None,
            ),
            malignant,
        )
        benign = CLINICAL.hungarian_colorectal_workbook_facts(
            "Hungarian-Colorectal-Screening",
            {"ID": "2", "ICD10_health_status": "D12600"},
        )
        self.assertIn(
            (
                "screening_result",
                "Non-malignant finding",
                "ICD10_health_status",
                None,
            ),
            benign,
        )
        indeterminate = CLINICAL.hungarian_colorectal_workbook_facts(
            "Hungarian-Colorectal-Screening",
            {"ID": "3", "ICD10_health_status": "R8990"},
        )
        self.assertIn(
            (
                "screening_result",
                "Indeterminate",
                "ICD10_health_status",
                None,
            ),
            indeterminate,
        )
        self.assertFalse(
            any(fact[0] == "primary_diagnosis" for fact in indeterminate)
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.sqlite"
            snapshot_conn = sqlite3.connect(snapshot)
            snapshot_conn.execute(
                """CREATE TABLE agent_pathdb_slides (
                       collection TEXT, patient_id TEXT, slide_id TEXT
                   )"""
            )
            snapshot_conn.executemany(
                """INSERT INTO agent_pathdb_slides VALUES
                   ('Hungarian-Colorectal-Screening', ?, ?)""",
                [(str(number), f"slide-{number}") for number in range(1, 201)],
            )
            snapshot_conn.executemany(
                """INSERT INTO agent_pathdb_slides VALUES
                   ('Hungarian-Colorectal-Screening', ?, ?)""",
                [
                    (f"{number}_zoom_2", f"tile-{tile}")
                    for number in range(1, 201)
                    for tile in range(2)
                ],
            )
            snapshot_conn.commit()
            snapshot_conn.close()

            conn = CLINICAL.init_db(root / "clinical.sqlite", replace=True)
            source_id = "tcia-download:hungarian:test"
            CLINICAL.insert_source(
                conn,
                source_id=source_id,
                source_kind="tcia_clinical_download",
                short_title="Hungarian-Colorectal-Screening",
                source_signature_value="test",
            )
            categories = (
                ["C18"] * 35
                + ["C20"]
                + ["C76"] * 3
                + ["D12"] * 66
                + ["K52"] * 8
                + ["K62"] * 6
                + ["K63"] * 67
                + ["R89"] * 14
            )
            for number, category in enumerate(categories, start=1):
                mapping = CLINICAL.HUNGARIAN_COLORECTAL_ICD10[category]
                facts = [
                    ("icd10_category", category, "ICD10_health_status", None),
                    (
                        "screening_result",
                        mapping["screening_result"],
                        "ICD10_health_status",
                        None,
                    ),
                ]
                if mapping["diagnosis"]:
                    facts.append(
                        (
                            "primary_diagnosis",
                            mapping["diagnosis"],
                            "ICD10_health_status",
                            None,
                        )
                    )
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id=source_id,
                    source_kind="tcia_clinical_download",
                    short_title="Hungarian-Colorectal-Screening",
                    subject_id=str(number),
                    table_name="clinical.csv",
                    row_number=number,
                    row={"ID": number},
                    facts=facts,
                    has_imaging=False,
                )

            result = CLINICAL.promote_and_audit_hungarian_colorectal_cohort(
                conn, snapshot
            )
            self.assertEqual(result["official_subjects"], 200)
            self.assertEqual(result["pathdb_subjects"], 200)
            self.assertEqual(result["official_pathdb_overlap"], 200)
            self.assertEqual(result["malignant_subjects"], 39)
            self.assertEqual(result["nonmalignant_subjects"], 147)
            self.assertEqual(result["indeterminate_subjects"], 14)
            self.assertEqual(result["unmapped_subjects"], 0)
            self.assertEqual(result["promoted_imaging_subjects"], 200)
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_imaging_subjects
                       WHERE short_title =
                             'Hungarian-Colorectal-Screening'"""
                ).fetchone()[0],
                200,
            )
            warning_row = conn.execute(
                """SELECT warning_type FROM clinical_build_warnings
                   WHERE short_title = 'Hungarian-Colorectal-Screening'"""
            ).fetchone()
            self.assertEqual(
                warning_row[0],
                "hungarian_colorectal_icd10_indeterminate",
            )
            conn.close()

    def test_ivygap_allen_rows_match_tcia_manifest_subjects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.sqlite"
            snapshot_conn = sqlite3.connect(snapshot)
            snapshot_conn.executescript(
                """
                CREATE TABLE agent_datasets (
                    short_title TEXT, title TEXT, date_updated TEXT,
                    hidden INTEGER, dataset_type TEXT
                );
                INSERT INTO agent_datasets VALUES
                    ('IvyGAP', 'Ivy Glioblastoma Atlas Project',
                     '2026-03-26', 0, 'Collection');
                CREATE TABLE agent_current_downloads (
                    short_title TEXT, hidden INTEGER, download_url TEXT,
                    date_updated TEXT, download_id TEXT
                );
                INSERT INTO agent_current_downloads VALUES
                    ('IvyGAP', 0,
                     'https://example.test/GC_manifest_IvyGAP.csv',
                     '2026-03-26', 'manifest');
                """
            )
            snapshot_conn.commit()
            snapshot_conn.close()

            tumor_csv = b"""donor_id,tumor_name,molecular_subtype,extent_of_resection,surgery,mgmt_methylation,survival_days,egfr_amplification,initial_kps,age_in_years
1,W1-1-2,Classical,Complete,primary,No,105,Yes,100,66 yrs
2,W22-1-1,"Classical, Neural",Complete,primary,Yes,,Yes,90,52 yrs
3,W22-2-1,Neural,Complete,recurrent,,,Yes,90,52 yrs
4,W27-2-1,Classical,Complete,recurrent,No,72,,90,64 yrs
"""
            manifest_csv = b"""Participant ID,File ID
W1,file-1
W22,file-2
"""
            original_fetch = CLINICAL.fetch_url

            def fake_fetch(url: str, *, timeout: int, max_bytes: int) -> bytes:
                del timeout, max_bytes
                return (
                    manifest_csv
                    if "GC_manifest" in url
                    else tumor_csv
                )

            CLINICAL.fetch_url = fake_fetch
            try:
                conn = CLINICAL.init_db(
                    root / "clinical.sqlite", replace=True
                )
                result = CLINICAL.ingest_ivygap_external_clinical(
                    conn,
                    snapshot,
                    no_fetch=False,
                    timeout=30,
                    max_bytes=1024 * 1024,
                )
            finally:
                CLINICAL.fetch_url = original_fetch

            self.assertEqual(result["status"], "loaded")
            self.assertEqual(result["allen_tumor_rows"], 4)
            self.assertEqual(result["allen_subjects"], 3)
            self.assertEqual(result["tcia_manifest_subjects"], 2)
            self.assertEqual(result["matched_tumor_rows"], 3)
            self.assertEqual(result["matched_subjects"], 2)
            self.assertEqual(result["external_only_subjects"], ["W27"])
            self.assertEqual(result["manifest_only_subjects"], [])
            self.assertEqual(result["multiple_tumor_subjects"], ["W22"])
            self.assertEqual(result["promoted_imaging_subjects"], 2)
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_rows
                       WHERE short_title = 'IvyGAP'"""
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_rows
                       WHERE short_title = 'IvyGAP'
                         AND subject_id = 'W27'"""
                ).fetchone()[0],
                0,
            )
            ages = dict(
                conn.execute(
                    """SELECT subject_id, value_number FROM clinical_facts
                       WHERE short_title = 'IvyGAP'
                         AND concept = 'age_at_diagnosis'"""
                )
            )
            self.assertEqual(ages, {"W1": 66.0, "W22": 52.0})
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(DISTINCT value_normalized)
                       FROM clinical_facts
                       WHERE short_title = 'IvyGAP'
                         AND subject_id = 'W22'
                         AND concept = 'molecular_subtype'"""
                ).fetchone()[0],
                2,
            )
            conn.close()

    def test_shared_idc_table_is_partitioned_by_imaging_collection(self) -> None:
        class FakeIDCClient:
            clinical_index = CLINICAL.SimpleFrame(
                ["collection_id", "short_table_name", "column", "column_label", "values"],
                [
                    {
                        "collection_id": collection,
                        "short_table_name": "shared_qa",
                        "column": column,
                        "column_label": label,
                        "values": [],
                    }
                    for collection in ("test", "test2")
                    for column, label in (
                        ("dicom_patient_id", "idc_provenance_dicom_patient_id"),
                        ("grade", "Grade"),
                    )
                ],
            )
            index = CLINICAL.SimpleFrame(
                ["collection_id", "PatientID"],
                [
                    {"collection_id": "test", "PatientID": "SUB-1"},
                    {"collection_id": "test2", "PatientID": "SUB-2"},
                    {
                        "collection_id": "imaging_only",
                        "PatientID": "IMG-1",
                    },
                ],
            )

            @staticmethod
            def get_idc_version() -> str:
                return "v-test"

            @staticmethod
            def fetch_index(name: str) -> None:
                pass

            @staticmethod
            def get_clinical_table(name: str):
                return CLINICAL.SimpleFrame(
                    ["dicom_patient_id", "grade"],
                    [
                        {"dicom_patient_id": "SUB-1", "grade": "1"},
                        {"dicom_patient_id": "SUB-2", "grade": "2"},
                    ],
                )

        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(Path(directory) / "shared.sqlite", replace=True)
            result = CLINICAL.ingest_idc_clinical(
                conn,
                allowed_short_titles={"TEST", "TEST2", "IMAGING-ONLY"},
                previous_db=None,
                refresh=False,
                no_fetch=False,
                client=FakeIDCClient(),
            )
            self.assertEqual(result["tables"], 2)
            self.assertEqual(result["rows"], 2)
            counts = dict(
                conn.execute(
                    """SELECT short_title, row_count
                       FROM clinical_idc_tables ORDER BY short_title"""
                )
            )
            self.assertEqual(counts, {"TEST": 1, "TEST2": 1})
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_imaging_subjects
                       WHERE short_title = 'IMAGING-ONLY'"""
                ).fetchone()[0],
                1,
            )
            conn.close()

    def test_idc_dictionary_lineage_and_image_linked_view(self) -> None:
        class FakeIDCClient:
            def __init__(self) -> None:
                self.clinical_index = CLINICAL.SimpleFrame(
                    [
                        "collection_id",
                        "short_table_name",
                        "column",
                        "column_label",
                        "values",
                    ],
                    [
                        {
                            "collection_id": "test",
                            "short_table_name": "test_prsn",
                            "column": "dicom_patient_id",
                            "column_label": "idc_provenance_dicom_patient_id",
                            "values": [],
                        },
                        {
                            "collection_id": "test",
                            "short_table_name": "test_prsn",
                            "column": "gender",
                            "column_label": "Gender",
                            "values": [
                                {"option_code": "1", "option_description": "Male"},
                                {"option_code": "2", "option_description": "Female"},
                            ],
                        },
                    ],
                )
                self.index = CLINICAL.SimpleFrame(
                    ["collection_id", "PatientID"],
                    [{"collection_id": "test", "PatientID": "SUB-1"}],
                )

            @staticmethod
            def get_idc_version() -> str:
                return "v-test"

            @staticmethod
            def fetch_index(name: str) -> None:
                if name != "clinical_index":
                    raise AssertionError(name)

            @staticmethod
            def get_clinical_table(name: str):
                if name != "test_prsn":
                    raise AssertionError(name)
                return CLINICAL.SimpleFrame(
                    ["dicom_patient_id", "gender"],
                    [
                        {"dicom_patient_id": "SUB-1", "gender": 2},
                        {"dicom_patient_id": "SUB-2", "gender": 1},
                    ],
                )

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "idc.sqlite"
            conn = CLINICAL.init_db(db, replace=True)
            result = CLINICAL.ingest_idc_clinical(
                conn,
                allowed_short_titles={"TEST"},
                previous_db=None,
                refresh=False,
                no_fetch=False,
                client=FakeIDCClient(),
            )
            CLINICAL.materialize_subjects(conn)
            self.assertEqual(result["status"], "loaded")
            self.assertEqual(result["rows"], 2)
            self.assertEqual(result["subjects_with_imaging"], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_clinical_subjects"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_clinical_all_subjects"
                ).fetchone()[0],
                2,
            )
            subject = conn.execute(
                """SELECT sex_at_birth, has_imaging
                   FROM agent_clinical_subjects WHERE subject_id = 'SUB-1'"""
            ).fetchone()
            self.assertEqual(tuple(subject), ("Female", 1))
            source = conn.execute(
                """SELECT source_priority, source_lineage,
                          length(artifact_sha256)
                   FROM clinical_sources WHERE source_kind = 'idc_clinical'"""
            ).fetchone()
            self.assertEqual(
                tuple(source),
                (300, "tcia-official-clinical:test", 64),
            )
            conn.close()

    def test_precedence_conflicts_and_incremental_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            official = root / "clinical.csv"
            official.write_text(
                "PatientID,Sex,Vital Status,Overall Survival Days\n"
                "SUB-1,F,Dead,400\n",
                encoding="utf-8",
            )
            snapshot = root / "snapshot.sqlite"
            self._make_snapshot(snapshot, official.as_uri())
            seed = root / "legacy.sqlite"
            self._make_seed(seed)
            first = root / "clinical.sqlite"
            first_gzip = root / "clinical.sqlite.gz"
            first_manifest = root / "clinical_manifest.json"

            self._run(
                "build",
                "--snapshot-db",
                str(snapshot),
                "--legacy-seed-db",
                str(seed),
                "--out",
                str(first),
                "--gzip-out",
                str(first_gzip),
                "--manifest-out",
                str(first_manifest),
                "--no-fetch-idc-clinical",
                "--replace",
            )
            self._run("validate", "--db", str(first))

            conn = sqlite3.connect(first)
            conn.row_factory = sqlite3.Row
            subject = conn.execute(
                """SELECT * FROM agent_clinical_subjects
                   WHERE short_title = 'TEST' AND subject_id = 'SUB-1'"""
            ).fetchone()
            self.assertIsNotNone(subject)
            self.assertEqual(subject["sex_at_birth"], "F")
            self.assertEqual(subject["vital_status"], "Dead")
            self.assertEqual(subject["overall_survival_days"], "400")
            self.assertGreaterEqual(subject["conflict_count"], 2)
            priorities = dict(
                conn.execute(
                    """SELECT source_kind, MAX(source_priority)
                       FROM clinical_facts GROUP BY source_kind"""
                )
            )
            self.assertEqual(
                priorities,
                {"cda": 200, "dicom": 100, "tcia_clinical_download": 400},
            )
            summary_subjects = conn.execute(
                """SELECT subjects FROM agent_clinical_dataset_summary
                   WHERE short_title = 'TEST'"""
            ).fetchone()[0]
            self.assertEqual(summary_subjects, 1)
            conn.close()

            # Reuse must not need the original artifact when the Collection
            # Manager signature is unchanged.
            official.unlink()
            second = root / "clinical-second.sqlite"
            second_manifest = root / "clinical-second-manifest.json"
            self._run(
                "build",
                "--snapshot-db",
                str(snapshot),
                "--previous-db",
                str(first),
                "--out",
                str(second),
                "--manifest-out",
                str(second_manifest),
                "--no-fetch-idc-clinical",
                "--replace",
            )
            conn = sqlite3.connect(second)
            status = conn.execute(
                "SELECT ingest_status FROM clinical_downloads"
            ).fetchone()[0]
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_clinical_subjects"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(status, "reused")
            self.assertEqual(count, 1)
            self.assertEqual(
                json.loads(first_manifest.read_text())["release_fingerprint"],
                json.loads(second_manifest.read_text())["release_fingerprint"],
            )

    def _run(self, *arguments: str) -> None:
        subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            check=True,
            text=True,
            capture_output=True,
        )

    @staticmethod
    def _make_snapshot(path: Path, url: str) -> None:
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE agent_current_downloads (
                hidden INTEGER,
                short_title TEXT,
                dataset_type TEXT,
                title TEXT,
                download_id TEXT,
                download_title TEXT,
                download_url TEXT,
                date_updated TEXT,
                file_types TEXT,
                download_types TEXT,
                data_types TEXT,
                access_level TEXT,
                controlled_access INTEGER,
                description TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO agent_current_downloads VALUES
               (0, 'TEST', 'Collection', 'Test collection', '1',
                'Clinical data', ?, '2026-01-01', '["CSV"]',
                '["Clinical Data"]', '["Demographic"]', 'open', 0,
                'Official patient clinical spreadsheet')""",
            (url,),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _make_seed(path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE dataset (
                dataset_id TEXT PRIMARY KEY,
                tcia_short_title TEXT NOT NULL
            );
            CREATE TABLE subject (
                subject_id TEXT PRIMARY KEY,
                dataset_id TEXT,
                cda_subject_id TEXT,
                source_subject_value TEXT,
                ethnicity TEXT,
                ncbi_taxonomy_id TEXT,
                race TEXT,
                sex_at_birth TEXT,
                vital_status TEXT,
                cda_row_json TEXT,
                source_json TEXT
            );
            CREATE TABLE diagnosis (
                diagnosis_id TEXT PRIMARY KEY,
                subject_id TEXT,
                age_at_diagnosis TEXT,
                metastasis_anatomic_site TEXT,
                primary_diagnosis TEXT,
                primary_site TEXT,
                cda_row_json TEXT
            );
            CREATE TABLE source_subjects (
                source_subject_id TEXT PRIMARY KEY,
                source_db_id TEXT,
                source_table TEXT,
                source_kind TEXT,
                source_column TEXT,
                source_value TEXT,
                normalized_source_value TEXT,
                short_title TEXT,
                dataset_type TEXT,
                id_kind TEXT,
                evidence_json TEXT
            );
            INSERT INTO dataset VALUES ('TEST', 'TEST');
            INSERT INTO subject VALUES
                ('CDA.SUB-1', 'TEST', 'CDA.SUB-1', 'SUB-1', '', '', '',
                 'M', 'Alive', '{}', '{}');
            INSERT INTO diagnosis VALUES
                ('DX-1', 'CDA.SUB-1', '48', '', 'Example cancer',
                 'Example site', '{}');
            INSERT INTO source_subjects VALUES
                ('DICOM-1', 'IDC', 'idc_index', 'idc_index', 'PatientID',
                 'SUB-1', 'sub-1', 'TEST', 'Collection', 'PatientID',
                 '{"short_title":"TEST","context":{"PatientSex":"O","PatientAge":"050Y"}}');
            """
        )
        conn.commit()
        conn.close()


if __name__ == "__main__":
    unittest.main()
