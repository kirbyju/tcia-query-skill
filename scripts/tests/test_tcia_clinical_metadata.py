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
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_clinical_metadata.py"
SPEC = importlib.util.spec_from_file_location("tcia_clinical_metadata", SCRIPT)
assert SPEC and SPEC.loader
CLINICAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLINICAL)


class ClinicalMetadataTest(unittest.TestCase):
    def test_remind_dictionary_and_reviewed_identifiers(self) -> None:
        self.assertEqual(
            CLINICAL.choose_subject_column(
                ["Case Number", "Age"], "ReMIND"
            ),
            "Case Number",
        )
        self.assertEqual(
            CLINICAL.choose_subject_column(
                ["unique_pt_id", "Course #"], "Brain-TR-GammaKnife"
            ),
            "unique_pt_id",
        )
        self.assertEqual(
            CLINICAL.official_subject_id_mapping("ReMIND", "1"),
            ("ReMIND-001", "dataset_prefix_zero_pad_3"),
        )
        self.assertEqual(
            CLINICAL.official_subject_id_mapping("Brain-TR-GammaKnife", "103.0"),
            ("GK_103", "dataset_prefix_zero_pad_3"),
        )
        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(Path(directory) / "remind.sqlite", replace=True)
            CLINICAL.insert_source(
                conn,
                source_id="official:remind",
                source_kind="tcia_clinical_download",
                short_title="ReMIND",
                source_signature_value="test",
            )
            frame = CLINICAL.SimpleFrame(
                ["Case Number", "Number associated with the case identifier"],
                [
                    {
                        "Case Number": "Age",
                        "Number associated with the case identifier": "Age at surgery",
                    },
                    {
                        "Case Number": "Histopathology",
                        "Number associated with the case identifier": "WHO tumor type",
                    },
                ],
            )
            inserted = CLINICAL.ingest_official_source_dictionary(
                conn,
                source_id="official:remind",
                short_title="ReMIND",
                table_name="clinical.xlsx::ReMIND Data Dictionary",
                frame=frame,
            )
            self.assertEqual(inserted, 3)
            fields = conn.execute(
                "SELECT field_name FROM clinical_source_dictionary ORDER BY rowid"
            ).fetchall()
            self.assertEqual(
                [row[0] for row in fields],
                ["Case Number", "Age", "Histopathology"],
            )
            conn.close()

    def test_brain_tr_gammaknife_course_and_lesion_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(Path(directory) / "brain-tr.sqlite", replace=True)
            CLINICAL.insert_source(
                conn,
                source_id="official:brain-tr",
                source_kind="tcia_clinical_download",
                short_title="Brain-TR-GammaKnife",
                source_signature_value="test",
            )
            rows = [
                (
                    "clinical.xlsx::course_level",
                    {"unique_pt_id": "103", "Course #": "1"},
                    "radiotherapy_course",
                    "",
                ),
                (
                    "clinical.xlsx::lesion_level",
                    {
                        "unique_pt_id": "103",
                        "Treatment Course": "1",
                        "Lesion #": "2",
                        "mri_type": "stable",
                        "Lesion Name in NRRD files": "GK.103_1_LFrontal",
                    },
                    "lesion_followup_outcome",
                    "GK.103_1_LFrontal",
                ),
            ]
            for row_number, (table_name, values, _, _) in enumerate(rows, 2):
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id="official:brain-tr",
                    source_kind="tcia_clinical_download",
                    short_title="Brain-TR-GammaKnife",
                    subject_id="GK_103",
                    table_name=table_name,
                    row_number=row_number,
                    row=values,
                    facts=[],
                )
                self.assertEqual(
                    CLINICAL.ingest_brain_tr_gammaknife_observation(
                        conn,
                        source_id="official:brain-tr",
                        short_title="Brain-TR-GammaKnife",
                        subject_id="GK_103",
                        table_name=table_name,
                        row_number=row_number,
                        row=values,
                    ),
                    1,
                )
            observations = conn.execute(
                """SELECT observation_type, file_name
                   FROM clinical_longitudinal_observations
                   ORDER BY observation_type"""
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in observations],
                [(row[2], row[3]) for row in sorted(rows, key=lambda item: item[2])],
            )
            conn.close()

    def test_bcbm_scan_and_radiomics_rows_preserve_native_grain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(
                Path(directory) / "bcbm.sqlite", replace=True
            )
            clinical_row = {
                "ID      ": "BCBM-RadioGenomics-76-0",
                "Age": "52",
                "Year": "2018",
                "Manufacturer": "GE MEDICAL SYSTEMS",
            }
            radiomics_row = {
                "FilenamePrefix": "BCBM-RadioGenomics-76-0",
                "Segmentation_Name": "mask_tumor",
                "original_shape_Elongation": "0.5",
            }
            for source_id in ("official:bcbm:clinical", "official:bcbm:radiomics"):
                CLINICAL.insert_source(
                    conn,
                    source_id=source_id,
                    source_kind="tcia_clinical_download",
                    short_title="BCBM-RadioGenomics",
                    source_signature_value="test",
                )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="official:bcbm:clinical",
                source_kind="tcia_clinical_download",
                short_title="BCBM-RadioGenomics",
                subject_id="BCBM-RadioGenomics-76",
                table_name="clinical.xlsx::Clinical+Genetics",
                row_number=2,
                row=clinical_row,
                facts=[],
            )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="official:bcbm:radiomics",
                source_kind="tcia_clinical_download",
                short_title="BCBM-RadioGenomics",
                subject_id="BCBM-RadioGenomics-76",
                table_name="radiomics.xlsx::merged_orig",
                row_number=2,
                row=radiomics_row,
                facts=[],
            )
            self.assertEqual(
                CLINICAL.ingest_bcbm_longitudinal_observation(
                    conn,
                    source_id="official:bcbm:clinical",
                    short_title="BCBM-RadioGenomics",
                    subject_id="BCBM-RadioGenomics-76",
                    table_name="clinical.xlsx::Clinical+Genetics",
                    row_number=2,
                    row=clinical_row,
                ),
                1,
            )
            self.assertEqual(
                CLINICAL.ingest_bcbm_longitudinal_observation(
                    conn,
                    source_id="official:bcbm:radiomics",
                    short_title="BCBM-RadioGenomics",
                    subject_id="BCBM-RadioGenomics-76",
                    table_name="radiomics.xlsx::merged_orig",
                    row_number=2,
                    row=radiomics_row,
                ),
                1,
            )
            observations = conn.execute(
                """SELECT observation_type, study_datetime, file_name
                   FROM clinical_longitudinal_observations
                   ORDER BY observation_type"""
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in observations],
                [
                    (
                        "scanner_clinical_scan",
                        "2018",
                        "BCBM-RadioGenomics-76-0_image_ss_n4.nii.gz",
                    ),
                    (
                        "segmentation_radiomics",
                        "",
                        "BCBM-RadioGenomics-76-0_mask_tumor.nii.gz",
                    ),
                ],
            )
            conn.close()

    def test_yale_longitudinal_ages_resolve_to_baseline_without_false_conflict(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(
                Path(directory) / "yale-age.sqlite", replace=True
            )
            CLINICAL.insert_source(
                conn,
                source_id="official:yale",
                source_kind="tcia_clinical_download",
                short_title="Yale-Brain-Mets-Longitudinal",
                source_signature_value="test",
            )
            for row_number, age in enumerate(("72", "71"), 2):
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id="official:yale",
                    source_kind="tcia_clinical_download",
                    short_title="Yale-Brain-Mets-Longitudinal",
                    subject_id="YG_TEST",
                    table_name="clinical.xlsx::Clinical_data",
                    row_number=row_number,
                    row={"patient_id": "YG_TEST", "age_at_Imaging (years)": age},
                    facts=[
                        ("age_at_imaging_years", age, "age_at_Imaging (years)", "years")
                    ],
                )
            CLINICAL.materialize_subjects(conn)
            subject = conn.execute(
                """SELECT age_at_imaging_years, conflict_count
                   FROM agent_clinical_all_subjects"""
            ).fetchone()
            self.assertEqual(tuple(subject), ("71", 0))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM agent_clinical_conflicts").fetchone()[0],
                0,
            )
            conn.close()

    def test_yale_brain_mets_dictionary_and_longitudinal_columns_are_exposed(
        self,
    ) -> None:
        mappings = {
            "age_at_Imaging (years)": ("age_at_imaging_years", "years"),
        }
        for column, expected in mappings.items():
            self.assertEqual(
                (
                    CLINICAL.concept_for_source_column(
                        "Yale-Brain-Mets-Longitudinal", column
                    ),
                    CLINICAL.unit_for_source_column(
                        "Yale-Brain-Mets-Longitudinal", column
                    ),
                ),
                expected,
            )

        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(
                Path(directory) / "yale-dictionary.sqlite", replace=True
            )
            CLINICAL.insert_source(
                conn,
                source_id="official:yale",
                source_kind="tcia_clinical_download",
                short_title="Yale-Brain-Mets-Longitudinal",
                source_signature_value="test",
            )
            frame = CLINICAL.SimpleFrame(
                ["Data Collection Name", "Data Descriptor /Metadata Name"],
                [
                    {
                        "Data Collection Name": "Patient ID",
                        "Data Descriptor /Metadata Name": "Patient Identification Number",
                    },
                    {
                        "Data Collection Name": "Study-Datetime",
                        "Data Descriptor /Metadata Name": "Anonymized Study Date and Time",
                    },
                    {
                        "Data Collection Name": "Age at Study",
                        "Data Descriptor /Metadata Name": "Age at Imaging",
                    },
                    {
                        "Data Collection Name": "Sex",
                        "Data Descriptor /Metadata Name": "Sex at Birth",
                    },
                ],
            )
            inserted = CLINICAL.ingest_official_source_dictionary(
                conn,
                source_id="official:yale",
                short_title="Yale-Brain-Mets-Longitudinal",
                table_name="clinical.xlsx::Data Dictionary",
                frame=frame,
            )
            self.assertEqual(inserted, 4)
            rows = conn.execute(
                """SELECT field_name, field_description
                   FROM agent_clinical_source_dictionary
                   ORDER BY field_name"""
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    ("age_at_Imaging (years)", "Age at Imaging"),
                    ("patient_id", "Patient Identification Number"),
                    ("sex", "Sex at Birth"),
                    ("study_datetime", "Anonymized Study Date and Time"),
                ],
            )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="official:yale",
                source_kind="tcia_clinical_download",
                short_title="Yale-Brain-Mets-Longitudinal",
                subject_id="YG_TEST",
                table_name="clinical.xlsx::image_acquisition_parameters",
                row_number=2,
                row={
                    "patient_id": "YG_TEST",
                    "study_datetime": "2020-01-02_03-04-05",
                    "file_name": "YG_TEST_FLAIR.nii.gz",
                    "sequence_class": "FLAIR",
                    "slice_thickness (mm)": "5",
                },
                facts=[],
            )
            inserted_observation = CLINICAL.ingest_yale_longitudinal_observation(
                conn,
                source_id="official:yale",
                short_title="Yale-Brain-Mets-Longitudinal",
                subject_id="YG_TEST",
                table_name="clinical.xlsx::image_acquisition_parameters",
                row_number=2,
                row={
                    "patient_id": "YG_TEST",
                    "study_datetime": "2020-01-02_03-04-05",
                    "file_name": "YG_TEST_FLAIR.nii.gz",
                    "sequence_class": "FLAIR",
                    "slice_thickness (mm)": "5",
                },
            )
            self.assertEqual(inserted_observation, 1)
            observation = conn.execute(
                """SELECT observation_type, study_datetime, file_name,
                          sequence_class, slice_thickness_mm
                   FROM agent_clinical_longitudinal_observations"""
            ).fetchone()
            self.assertEqual(
                tuple(observation),
                (
                    "image_acquisition",
                    "2020-01-02_03-04-05",
                    "YG_TEST_FLAIR.nii.gz",
                    "FLAIR",
                    "5",
                ),
            )
            conn.close()

    def test_reviewed_bare_id_mappings_are_dataset_scoped_and_auditable(
        self,
    ) -> None:
        self.assertIsNone(CLINICAL.choose_subject_column(["ID", "Age"]))
        for short_title in (
            "HEAD-NECK-RADIOMICS-HN1",
            "TOMPEI-CMMD",
            "UCSD-PTGBM",
            "UCSD-VS-Longitudinal",
            "UCSF-PDGM",
            "UPENN-GBM",
            "BCBM-RadioGenomics",
        ):
            self.assertEqual(
                CLINICAL.choose_subject_column(["ID", "Age"], short_title),
                "ID",
            )
        self.assertEqual(
            CLINICAL.choose_subject_column(
                ["ID1", "classification"], "TOMPEI-CMMD"
            ),
            "ID1",
        )

        mappings = {
            ("UCSF-PDGM", "UCSF-PDGM-004"): (
                "UCSF-PDGM-0004",
                "zero_pad_4_strip_followup_suffix",
            ),
            ("UCSF-PDGM", "UCSF-PDGM-0391_FU016d"): (
                "UCSF-PDGM-0391",
                "zero_pad_4_strip_followup_suffix",
            ),
            ("UPENN-GBM", "UPENN-GBM-00001_11"): (
                "UPENN-GBM-00001",
                "strip_scan_suffix",
            ),
            ("UCSD-PTGBM", "UCSD-PTGBM-0001_02"): (
                "UCSD-PTGBM-0001",
                "strip_scan_suffix",
            ),
            ("UCSD-VS-Longitudinal", "VS_0001_03"): (
                "VS_0001",
                "strip_visit_suffix",
            ),
            ("HEAD-NECK-RADIOMICS-HN1", "HN1004"): (
                "HN1004",
                "dataset_specific_exact_id",
            ),
            ("BCBM-RadioGenomics", "BCBM-RadioGenomics-76-0"): (
                "BCBM-RadioGenomics-76",
                "strip_scan_suffix",
            ),
        }
        for (short_title, source_id), expected in mappings.items():
            self.assertEqual(
                CLINICAL.official_subject_id_mapping(short_title, source_id),
                expected,
            )

        self.assertEqual(
            CLINICAL.concept_for_source_column("UCSF-PDGM", "Age at MRI"),
            "age_at_imaging_years",
        )
        self.assertEqual(
            CLINICAL.concept_for_source_column(
                "UPENN-GBM", "Survival_from_surgery_days_UPDATED"
            ),
            "overall_survival_days",
        )

    def test_official_scan_id_ingest_preserves_raw_id_and_mapping_provenance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(
                Path(directory) / "clinical.sqlite", replace=True
            )
            source_row = conn.execute(
                """SELECT 'UPENN-GBM' AS short_title,
                          'Collection' AS dataset_type,
                          'test' AS download_id,
                          'Clinical Data' AS download_title,
                          'https://example.test/upenn.csv' AS download_url,
                          '2026-08-07' AS date_updated,
                          '[\"CSV\"]' AS file_types,
                          '[\"Clinical Data\"]' AS download_types,
                          '[\"Demographic\"]' AS data_types,
                          'open' AS access_level,
                          0 AS controlled_access"""
            ).fetchone()
            csv_bytes = (
                "ID,Gender,Age_at_scan_years,MGMT\n"
                "UPENN-GBM-00001_11,F,63,Methylated\n"
                "UPENN-GBM-00001_21,F,64,Methylated\n"
            ).encode("utf-8")
            rows, subjects = CLINICAL.ingest_official_bytes(
                conn,
                source_row,
                source_id="tcia-download:upenn:test",
                signature="test",
                data=csv_bytes,
            )
            self.assertEqual((rows, subjects), (2, 1))
            stored = conn.execute(
                """SELECT subject_id, row_json FROM clinical_rows
                   ORDER BY row_number LIMIT 1"""
            ).fetchone()
            self.assertEqual(stored["subject_id"], "UPENN-GBM-00001")
            self.assertEqual(
                json.loads(stored["row_json"])["ID"],
                "UPENN-GBM-00001_11",
            )
            fact = conn.execute(
                """SELECT f.provenance_json FROM clinical_facts f
                   JOIN clinical_rows r USING (source_row_id)
                   WHERE f.concept = 'age_at_imaging_years'
                   ORDER BY r.row_number LIMIT 1"""
            ).fetchone()
            provenance = json.loads(fact["provenance_json"])
            self.assertEqual(
                provenance["source_subject_id"], "UPENN-GBM-00001_11"
            )
            self.assertEqual(
                provenance["subject_id_mapping_method"], "strip_scan_suffix"
            )
            conn.close()

    def test_reviewed_official_cohort_promotes_only_exact_published_count(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.sqlite"
            snapshot_conn = sqlite3.connect(snapshot)
            snapshot_conn.execute(
                """CREATE TABLE agent_datasets (
                       short_title TEXT, subjects TEXT, hidden INTEGER,
                       dataset_type TEXT
                   )"""
            )
            snapshot_conn.execute(
                """INSERT INTO agent_datasets VALUES
                   ('UCSD-VS-Longitudinal', '2', 0, 'Collection')"""
            )
            snapshot_conn.commit()
            snapshot_conn.close()

            conn = CLINICAL.init_db(root / "clinical.sqlite", replace=True)
            CLINICAL.insert_source(
                conn,
                source_id="official:vs",
                source_kind="tcia_clinical_download",
                short_title="UCSD-VS-Longitudinal",
                source_signature_value="test",
            )
            for row_number, subject_id in enumerate(("VS_0001", "VS_0002"), 1):
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id="official:vs",
                    source_kind="tcia_clinical_download",
                    short_title="UCSD-VS-Longitudinal",
                    subject_id=subject_id,
                    table_name="clinical.tsv",
                    row_number=row_number,
                    row={"ID": f"{subject_id}_01"},
                    facts=[("sex_at_birth", "F", "Sex at birth", None)],
                )
            result = CLINICAL.promote_reviewed_official_id_cohorts(
                conn, snapshot
            )["UCSD-VS-Longitudinal"]
            self.assertEqual(result["status"], "promoted")
            self.assertEqual(result["promoted_imaging_subjects"], 2)
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_rows
                       WHERE has_imaging = 1"""
                ).fetchone()[0],
                2,
            )
            conn.close()

    def test_tompei_package_crosswalk_promotes_only_package_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.sqlite"
            snapshot_conn = sqlite3.connect(snapshot)
            snapshot_conn.executescript(
                """
                CREATE TABLE agent_datasets (
                    short_title TEXT, subjects TEXT, hidden INTEGER,
                    dataset_type TEXT
                );
                INSERT INTO agent_datasets VALUES
                    ('TOMPEI-CMMD', '3', 0, 'Analysis Result');
                CREATE TABLE agent_current_downloads (
                    short_title TEXT, hidden INTEGER, download_id TEXT,
                    download_url TEXT, subjects TEXT, file_types TEXT,
                    data_types TEXT
                );
                INSERT INTO agent_current_downloads VALUES
                    ('TOMPEI-CMMD', 0, 'package',
                     'https://example.test/tompei.zip', '3',
                     '["ZIP","JSON"]', '["Segmentation"]');
                """
            )
            snapshot_conn.close()

            conn = CLINICAL.init_db(root / "clinical.sqlite", replace=True)
            CLINICAL.insert_source(
                conn,
                source_id="official:tompei",
                source_kind="tcia_clinical_download",
                short_title="TOMPEI-CMMD",
                source_signature_value="test",
            )
            for row_number, subject_id in enumerate(
                ("D1-0001", "D1-0002", "D2-0001", "D2-0002"), 1
            ):
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id="official:tompei",
                    source_kind="tcia_clinical_download",
                    short_title="TOMPEI-CMMD",
                    subject_id=subject_id,
                    table_name="clinical.xlsx",
                    row_number=row_number,
                    row={"ID": subject_id},
                    facts=[("screening_result", "Benign", "classification", None)],
                )
            archive_bytes = io.BytesIO()
            with zipfile.ZipFile(archive_bytes, "w") as archive:
                for subject_id in ("D1-0001", "D1-0002", "D2-0001"):
                    archive.writestr(
                        f"TOMPEI/{subject_id}_MLO_L_AnnotationFile.json",
                        "{}",
                    )
            original_fetch = CLINICAL.fetch_url
            CLINICAL.fetch_url = lambda *args, **kwargs: archive_bytes.getvalue()
            try:
                result = CLINICAL.promote_tompei_cmmd_package_cohort(
                    conn,
                    snapshot,
                    no_fetch=False,
                    timeout=5,
                    max_bytes=1_000_000,
                )
            finally:
                CLINICAL.fetch_url = original_fetch
            self.assertEqual(result["status"], "promoted")
            self.assertEqual(result["package_subjects"], 3)
            self.assertEqual(result["clinical_ids_outside_package"], 1)
            self.assertEqual(result["promoted_imaging_subjects"], 3)
            conn.close()

    def test_tcga_breast_result_prefers_full_bcr_patient_barcode(self) -> None:
        self.assertEqual(
            CLINICAL.choose_subject_column(
                ["patient_id", "bcr_patient_barcode", "vital_status"],
                "TCGA-Breast-Radiogenomics",
            ),
            "bcr_patient_barcode",
        )

    def test_analysis_result_inherits_collection_facts_by_exact_patient_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.sqlite"
            snapshot_conn = sqlite3.connect(snapshot)
            snapshot_conn.executescript(
                """
                CREATE TABLE agent_datasets (
                    source TEXT, short_title TEXT, title TEXT, link TEXT,
                    hidden INTEGER, source_collections TEXT
                );
                INSERT INTO agent_datasets VALUES
                    ('collections', 'TCGA-GBM', 'TCGA Glioblastoma',
                     'https://example.test/tcga-gbm', 0, ''),
                    ('analysis-results', 'GBM-RESULT', 'Derived GBM result',
                     'https://example.test/gbm-result', 0,
                     'Original images and patients from TCGA-GBM');
                CREATE TABLE agent_current_downloads (
                    short_title TEXT, parent_source TEXT, hidden INTEGER
                );
                """
            )
            snapshot_conn.close()

            conn = CLINICAL.init_db(root / "clinical.sqlite", replace=True)
            for source_id, source_kind, short_title in (
                ("official:gbm", "tcia_clinical_download", "TCGA-GBM"),
                ("dicom:result", "dicom", "GBM-RESULT"),
            ):
                CLINICAL.insert_source(
                    conn,
                    source_id=source_id,
                    source_kind=source_kind,
                    short_title=short_title,
                    source_signature_value=source_id,
                )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="official:gbm",
                source_kind="tcia_clinical_download",
                short_title="TCGA-GBM",
                subject_id="TCGA-01-0001",
                table_name="clinical.csv",
                row_number=1,
                row={"PatientID": "TCGA-01-0001"},
                facts=[
                    ("sex_at_birth", "Female", "sex", None),
                    ("primary_diagnosis", "Glioblastoma", "diagnosis", None),
                    ("primary_site", "Brain", "site", None),
                    (
                        "age_at_imaging_years",
                        "55",
                        "PatientAge",
                        "years",
                    ),
                ],
                has_imaging=True,
            )
            for row_number, subject_id in enumerate(
                ("TCGA-01-0001", "TCGA-01-9999"), start=1
            ):
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id="dicom:result",
                    source_kind="dicom",
                    short_title="GBM-RESULT",
                    subject_id=subject_id,
                    table_name="legacy.idc_index",
                    row_number=row_number,
                    row={"PatientID": subject_id, "PatientSex": "M"},
                    facts=[("sex_at_birth", "M", "PatientSex", None)],
                    has_imaging=True,
                )
                conn.execute(
                    """INSERT INTO clinical_imaging_subjects
                       (subject_key, short_title, subject_id, imaging_source)
                       VALUES (?, 'GBM-RESULT', ?, 'dicom')""",
                    (f"gbmresult:{subject_id.lower()}", subject_id),
                )

            CLINICAL.materialize_clinical_qc(conn)
            CLINICAL.materialize_subjects(conn)
            result = CLINICAL.inherit_analysis_result_clinical_facts(
                conn, snapshot
            )
            CLINICAL.materialize_clinical_qc(conn)
            CLINICAL.materialize_subjects(conn)

            self.assertEqual(result["relationships"], 1)
            self.assertEqual(result["matched_subjects"], 1)
            self.assertEqual(result["inherited_facts"], 3)
            relationship = conn.execute(
                """SELECT target_short_title, source_short_title,
                          target_subjects, matched_subjects, inherited_facts,
                          status
                   FROM agent_clinical_dataset_relationships"""
            ).fetchone()
            self.assertEqual(
                tuple(relationship),
                ("GBM-RESULT", "TCGA-GBM", 2, 1, 3, "matched"),
            )
            matched = conn.execute(
                """SELECT sex_at_birth, primary_diagnosis, primary_site,
                          age_at_imaging_years
                   FROM agent_clinical_subjects
                   WHERE short_title = 'GBM-RESULT'
                     AND subject_id = 'TCGA-01-0001'"""
            ).fetchone()
            self.assertEqual(
                tuple(matched), ("Male", "Glioblastoma", "Brain", None)
            )
            unmatched = conn.execute(
                """SELECT primary_diagnosis, primary_site
                   FROM agent_clinical_subjects
                   WHERE short_title = 'GBM-RESULT'
                     AND subject_id = 'TCGA-01-9999'"""
            ).fetchone()
            self.assertEqual(tuple(unmatched), (None, None))
            inherited = conn.execute(
                """SELECT evidence_scope, source_priority, provenance_json
                   FROM clinical_facts
                   WHERE short_title = 'GBM-RESULT'
                     AND concept = 'primary_diagnosis'
                     AND source_kind =
                         'tcia_collection_subject_inheritance'"""
            ).fetchone()
            self.assertEqual(tuple(inherited)[:2], ("patient_inherited", 75))
            provenance = json.loads(inherited["provenance_json"])
            self.assertEqual(
                provenance["inherited_from_short_title"], "TCGA-GBM"
            )
            self.assertEqual(
                provenance["inherited_from_subject_id"], "TCGA-01-0001"
            )
            conn.close()

    def test_reviewed_dataset_zero_ages_are_excluded_as_missing(self) -> None:
        datasets = (
            "CMB-AML",
            "CMB-CRC",
            "CMB-GEC",
            "CMB-LCA",
            "CMB-MEL",
            "CMB-MML",
            "CMB-PCA",
            "GLIS-RT",
            "Head-Neck Cetuximab",
        )
        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(
                Path(directory) / "reviewed-zero.sqlite", replace=True
            )
            for row_number, short_title in enumerate(datasets, start=1):
                source_id = f"dicom:{CLINICAL.normalize_name(short_title)}"
                CLINICAL.insert_source(
                    conn,
                    source_id=source_id,
                    source_kind="dicom",
                    short_title=short_title,
                    source_signature_value=source_id,
                )
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id=source_id,
                    source_kind="dicom",
                    short_title=short_title,
                    subject_id=f"SUB-{row_number}",
                    table_name="legacy.idc_index",
                    row_number=1,
                    row={"PatientAge": "0"},
                    facts=[
                        ("age_at_imaging_years", "0", "PatientAge", "years")
                    ],
                    has_imaging=True,
                )

            counts = CLINICAL.materialize_clinical_qc(conn)
            self.assertEqual(counts, {"auto_exclude": len(datasets)})
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_facts
                       WHERE qc_status = 'excluded_dataset_zero_age_missing'"""
                ).fetchone()[0],
                len(datasets),
            )
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_qc_findings
                       WHERE rule_id = 'dicom_age_zero_review'"""
                ).fetchone()[0],
                0,
            )
            conn.close()

    def test_acrin_fmiso_official_age_resolves_zero_age_review(self) -> None:
        self.assertEqual(
            CLINICAL.choose_subject_column(["cn", "entryage"], "ACRIN-FMISO-Brain"),
            "cn",
        )
        self.assertEqual(
            CLINICAL.normalize_official_subject_id("ACRIN-FMISO-Brain", "36.0"),
            "ACRIN-FMISO-Brain-036",
        )
        self.assertEqual(
            CLINICAL.concept_for_source_column(
                "ACRIN-FMISO-Brain", "entryage"
            ),
            "age_at_enrollment_years",
        )
        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(
                Path(directory) / "acrin-fmiso.sqlite", replace=True
            )
            for source_id, source_kind in (
                ("official:fmiso", "tcia_clinical_download"),
                ("dicom:fmiso", "dicom"),
            ):
                CLINICAL.insert_source(
                    conn,
                    source_id=source_id,
                    source_kind=source_kind,
                    short_title="ACRIN-FMISO-Brain",
                    source_signature_value=source_id,
                )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="official:fmiso",
                source_kind="tcia_clinical_download",
                short_title="ACRIN-FMISO-Brain",
                subject_id="ACRIN-FMISO-Brain-036",
                table_name="A0.csv",
                row_number=1,
                row={"cn": "36", "entryage": "64"},
                facts=[
                    ("age_at_enrollment_years", "64", "entryage", "years")
                ],
                has_imaging=True,
            )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="dicom:fmiso",
                source_kind="dicom",
                short_title="ACRIN-FMISO-Brain",
                subject_id="ACRIN-FMISO-Brain-036",
                table_name="legacy.idc_index",
                row_number=1,
                row={"PatientAge": "0"},
                facts=[
                    ("age_at_imaging_years", "0", "PatientAge", "years")
                ],
                has_imaging=True,
            )
            counts = CLINICAL.materialize_clinical_qc(conn)
            CLINICAL.materialize_subjects(conn)
            self.assertEqual(counts, {"auto_exclude": 1})
            subject = conn.execute(
                """SELECT age_at_enrollment_years, age_at_imaging_years
                   FROM clinical_subjects"""
            ).fetchone()
            self.assertEqual(tuple(subject), ("64", None))
            self.assertEqual(
                conn.execute(
                    """SELECT rule_id FROM clinical_qc_findings"""
                ).fetchone()[0],
                "acrin_fmiso_dicom_zero_age_missing",
            )
            conn.close()

    def test_acrin_fmiso_a0_csv_ingests_cn_and_entryage(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("A0.csv", "cn,entryage\n36,64\n44,48\n")
        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(
                Path(directory) / "acrin-fmiso-ingest.sqlite", replace=True
            )
            for subject_id in (
                "ACRIN-FMISO-Brain-036",
                "ACRIN-FMISO-Brain-044",
            ):
                conn.execute(
                    """INSERT INTO clinical_imaging_subjects
                       (subject_key, short_title, subject_id, imaging_source)
                       VALUES (?, ?, ?, 'dicom')""",
                    (
                        f"acrinfmisobrain:{subject_id.lower()}",
                        "ACRIN-FMISO-Brain",
                        subject_id,
                    ),
                )
            row = {
                "short_title": "ACRIN-FMISO-Brain",
                "download_url": "https://example.test/clinical.zip",
                "download_title": "ACRIN FMISO clinical data",
                "date_updated": "2026-01-01",
                "download_id": "6684",
                "file_types": "CSV",
            }
            loaded_rows, subjects = CLINICAL.ingest_official_bytes(
                conn,
                row,
                source_id="official:fmiso",
                signature="test",
                data=buffer.getvalue(),
            )
            self.assertEqual((loaded_rows, subjects), (2, 2))
            self.assertEqual(
                [tuple(item) for item in conn.execute(
                    """SELECT subject_id, concept, value_text
                       FROM clinical_facts ORDER BY subject_id"""
                )],
                [
                    (
                        "ACRIN-FMISO-Brain-036",
                        "age_at_enrollment_years",
                        "64",
                    ),
                    (
                        "ACRIN-FMISO-Brain-044",
                        "age_at_enrollment_years",
                        "48",
                    ),
                ],
            )
            conn.close()

    def test_qc_decodes_audited_codes_and_classifies_ingestion_warnings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conn = CLINICAL.init_db(
                Path(directory) / "codes-warnings.sqlite", replace=True
            )
            CLINICAL.insert_source(
                conn,
                source_id="official:colorectal",
                source_kind="tcia_clinical_download",
                short_title="Colorectal-Liver-Metastases",
                source_signature_value="official",
            )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="official:colorectal",
                source_kind="tcia_clinical_download",
                short_title="Colorectal-Liver-Metastases",
                subject_id="CRLM-1",
                table_name="Clinical.xlsx",
                row_number=1,
                row={"Sex": "2", "Vital status": "1", "Age": "57"},
                facts=[
                    ("sex_at_birth", "2", "Sex", None),
                    ("vital_status", "1", "Vital status", None),
                    ("age_at_treatment_years", "57", "Age", "years"),
                ],
                has_imaging=True,
            )
            CLINICAL.warning(
                conn,
                "subject_column_not_found",
                "No subject identifier in Clinical.xlsx::Data Dictionary; "
                "columns=['Data Category', 'Description']",
                source_id="official:colorectal",
                short_title="Colorectal-Liver-Metastases",
            )
            CLINICAL.warning(
                conn,
                "subject_column_not_found",
                "No subject identifier in A0.csv; columns=['cn', 'entryage']",
                source_id="official:colorectal",
                short_title="Colorectal-Liver-Metastases",
            )

            counts = CLINICAL.materialize_clinical_qc(conn)
            CLINICAL.materialize_subjects(conn)
            self.assertEqual(
                counts,
                {"accepted_skip": 1, "auto_normalize": 2, "manual_review": 1},
            )
            subject = conn.execute(
                """SELECT sex_at_birth, vital_status, age_at_treatment_years,
                          age_at_imaging_years FROM clinical_subjects"""
            ).fetchone()
            self.assertEqual(tuple(subject), ("Female", "Dead", "57", None))
            dispositions = dict(
                conn.execute(
                    """SELECT rule_id, disposition FROM clinical_qc_findings
                       WHERE rule_id LIKE '%table'
                          OR rule_id = 'subject_identifier_mapping_required'"""
                )
            )
            self.assertEqual(
                dispositions,
                {
                    "expected_non_patient_reference_table": "accepted_skip",
                    "subject_identifier_mapping_required": "manual_review",
                },
            )
            conn.close()

    def test_hnc_imrt_zero_age_is_preserved_but_excluded_as_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "hnc-imrt.sqlite"
            conn = CLINICAL.init_db(db_path, replace=True)
            CLINICAL.insert_source(
                conn,
                source_id="dicom:hncimrt7033",
                source_kind="dicom",
                short_title="HNC-IMRT-70-33",
                source_signature_value="dicom",
            )
            for row_number, (subject_id, age) in enumerate(
                (("HNC_001", "0"), ("HNC_002", "55")),
                start=1,
            ):
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id="dicom:hncimrt7033",
                    source_kind="dicom",
                    short_title="HNC-IMRT-70-33",
                    subject_id=subject_id,
                    table_name="legacy.idc_index",
                    row_number=row_number,
                    row={"PatientID": subject_id, "PatientAge": age},
                    facts=[
                        ("age_at_imaging_years", age, "PatientAge", "years")
                    ],
                    has_imaging=True,
                )

            counts = CLINICAL.materialize_clinical_qc(conn)
            CLINICAL.materialize_subjects(conn)
            conn.commit()

            self.assertEqual(counts, {"auto_exclude": 1})
            zero = conn.execute(
                """SELECT value_text, qc_excluded, qc_status
                   FROM clinical_facts WHERE subject_id = 'HNC_001'"""
            ).fetchone()
            self.assertEqual(
                tuple(zero),
                ("0", 1, "excluded_hnc_imrt_zero_age_missing"),
            )
            self.assertIsNone(
                conn.execute(
                    """SELECT age_at_imaging_years FROM clinical_subjects
                       WHERE subject_id = 'HNC_001'"""
                ).fetchone()
            )
            self.assertEqual(
                conn.execute(
                    """SELECT age_at_imaging_years FROM clinical_subjects
                       WHERE subject_id = 'HNC_002'"""
                ).fetchone()[0],
                "55",
            )
            finding = conn.execute(
                """SELECT rule_id, disposition FROM clinical_qc_findings"""
            ).fetchone()
            self.assertEqual(
                tuple(finding),
                ("hnc_imrt_dicom_zero_age_missing", "auto_exclude"),
            )
            conn.close()

    def test_radcure_qc_prefers_official_age_and_flags_sex_conflict(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "radcure.sqlite"
            conn = CLINICAL.init_db(db_path, replace=True)
            CLINICAL.insert_source(
                conn,
                source_id="official:radcure",
                source_kind="tcia_clinical_download",
                short_title="RADCURE",
                source_signature_value="official",
            )
            CLINICAL.insert_source(
                conn,
                source_id="dicom:radcure",
                source_kind="dicom",
                short_title="RADCURE",
                source_signature_value="dicom",
            )
            for row_number, (subject_id, official_age, dicom_age) in enumerate(
                (("RADCURE-1", "72.3", "0"), ("RADCURE-2", "60.1", "60")),
                start=1,
            ):
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id="official:radcure",
                    source_kind="tcia_clinical_download",
                    short_title="RADCURE",
                    subject_id=subject_id,
                    table_name="RADCURE_Clinical.xlsx",
                    row_number=row_number,
                    row={
                        "PatientID": subject_id,
                        "Age": official_age,
                        "Sex": "Female",
                    },
                    facts=[
                        ("age_at_imaging_years", official_age, "Age", None),
                        ("sex_at_birth", "Female", "Sex", None),
                    ],
                    has_imaging=True,
                )
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id="dicom:radcure",
                    source_kind="dicom",
                    short_title="RADCURE",
                    subject_id=subject_id,
                    table_name="legacy.idc_index",
                    row_number=row_number,
                    row={
                        "PatientID": subject_id,
                        "PatientAge": dicom_age,
                        "PatientSex": "M" if row_number == 1 else "F",
                    },
                    facts=[
                        (
                            "age_at_imaging_years",
                            dicom_age,
                            "PatientAge",
                            "years",
                        ),
                        (
                            "sex_at_birth",
                            "M" if row_number == 1 else "F",
                            "PatientSex",
                            None,
                        ),
                    ],
                    has_imaging=True,
                )

            counts = CLINICAL.materialize_clinical_qc(conn)
            CLINICAL.materialize_subjects(conn)
            conn.commit()

            self.assertEqual(counts, {"auto_exclude": 2, "manual_review": 1})
            subjects = conn.execute(
                """SELECT subject_id, age_at_imaging_years, sex_at_birth
                   FROM clinical_subjects ORDER BY subject_id"""
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in subjects],
                [
                    ("RADCURE-1", "72.3", "Female"),
                    ("RADCURE-2", "60.1", "Female"),
                ],
            )
            excluded = conn.execute(
                """SELECT COUNT(*) FROM clinical_facts
                   WHERE source_kind = 'dicom'
                     AND concept = 'age_at_imaging_years'
                     AND qc_excluded = 1
                     AND qc_status = 'superseded_by_official_radcure_age'"""
            ).fetchone()[0]
            self.assertEqual(excluded, 2)
            findings = conn.execute(
                """SELECT rule_id, disposition, original_value, resolved_value
                   FROM clinical_qc_findings ORDER BY rule_id, original_value"""
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in findings],
                [
                    (
                        "radcure_dicom_age_superseded",
                        "auto_exclude",
                        "0",
                        "72.3",
                    ),
                    (
                        "radcure_dicom_age_superseded",
                        "auto_exclude",
                        "60",
                        "60.1",
                    ),
                    (
                        "radcure_sex_source_conflict",
                        "manual_review",
                        "M",
                        "Female",
                    ),
                ],
            )
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM agent_clinical_conflicts
                       WHERE concept = 'age_at_imaging_years'"""
                ).fetchone()[0],
                0,
            )
            review_csv = Path(directory) / "radcure-review.csv"
            exported = CLINICAL.export_qc_findings(
                db_path, review_csv, short_title="RADCURE"
            )
            self.assertEqual(exported["rows"], 1)
            self.assertIn(
                "radcure_sex_source_conflict", review_csv.read_text()
            )
            conn.close()

    def test_age_qc_preserves_source_values_and_cleans_resolved_subjects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "clinical.sqlite"
            conn = CLINICAL.init_db(db_path, replace=True)
            CLINICAL.insert_source(
                conn,
                source_id="official:crowds",
                source_kind="tcia_clinical_download",
                short_title="Crowds-Cure-2017",
                source_signature_value="crowds",
            )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="official:crowds",
                source_kind="tcia_clinical_download",
                short_title="Crowds-Cure-2017",
                subject_id="TCGA-TEST",
                table_name="ccc2017clinical.csv",
                row_number=1,
                row={"PatientID": "TCGA-TEST", "age_at_diagnosis": "32364"},
                facts=[
                    ("age_at_diagnosis", "32364", "age_at_diagnosis", None)
                ],
            )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="official:crowds",
                source_kind="tcia_clinical_download",
                short_title="Crowds-Cure-2017",
                subject_id="TCGA-MISSING",
                table_name="ccc2017clinical.csv",
                row_number=2,
                row={"PatientID": "TCGA-MISSING", "age_at_diagnosis": "--"},
                facts=[("age_at_diagnosis", "--", "age_at_diagnosis", None)],
            )
            CLINICAL.insert_source(
                conn,
                source_id="dicom:headneck",
                source_kind="dicom",
                short_title="Head-Neck-PET-CT",
                source_signature_value="dicom",
            )
            for row_number, (subject_id, value) in enumerate(
                (("PLACEHOLDER", "999"), ("ZERO", "0"), ("VALID", "45")),
                start=1,
            ):
                CLINICAL.insert_row_and_facts(
                    conn,
                    source_id="dicom:headneck",
                    source_kind="dicom",
                    short_title="Head-Neck-PET-CT",
                    subject_id=subject_id,
                    table_name="legacy.idc_index",
                    row_number=row_number,
                    row={"PatientID": subject_id, "PatientAge": value},
                    facts=[
                        ("age_at_imaging_years", value, "PatientAge", "years")
                    ],
                    has_imaging=True,
                )
            CLINICAL.insert_row_and_facts(
                conn,
                source_id="dicom:headneck",
                source_kind="dicom",
                short_title="Head-Neck-PET-CT",
                subject_id="Explanations",
                table_name="clinical.xlsx::Excluded",
                row_number=12,
                row={"Patient #": "Explanations", "Sex": "curation note"},
                facts=[("sex_at_birth", "curation note", "Sex", None)],
            )

            counts = CLINICAL.materialize_clinical_qc(conn)
            CLINICAL.materialize_subjects(conn)
            conn.commit()

            crowds = conn.execute(
                """SELECT value_text, value_resolved, value_number, unit,
                          qc_excluded, qc_status
                   FROM clinical_facts
                   WHERE subject_id = 'TCGA-TEST'"""
            ).fetchone()
            self.assertEqual(crowds["value_text"], "32364")
            self.assertEqual(crowds["value_resolved"], "88.61")
            self.assertAlmostEqual(crowds["value_number"], 32364 / 365.25)
            self.assertEqual(crowds["unit"], "years")
            self.assertEqual(crowds["qc_excluded"], 0)
            self.assertEqual(
                crowds["qc_status"], "normalized_age_days_to_years"
            )
            self.assertEqual(
                conn.execute(
                    """SELECT age_at_diagnosis FROM clinical_subjects
                       WHERE subject_id = 'TCGA-TEST'"""
                ).fetchone()[0],
                "88.61",
            )
            self.assertEqual(
                conn.execute(
                    """SELECT age_at_imaging_years FROM clinical_subjects
                       WHERE subject_id = 'VALID'"""
                ).fetchone()[0],
                "45",
            )
            excluded_subjects = {
                row[0]
                for row in conn.execute(
                    """SELECT subject_id FROM clinical_subjects
                       WHERE subject_id IN
                           ('TCGA-MISSING', 'PLACEHOLDER', 'ZERO',
                            'Explanations')"""
                )
            }
            self.assertEqual(excluded_subjects, set())
            self.assertEqual(counts["manual_review"], 1)
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_qc_findings
                       WHERE disposition = 'auto_normalize'"""
                ).fetchone()[0],
                1,
            )

            csv_path = root / "manual-review.csv"
            exported = CLINICAL.export_qc_findings(db_path, csv_path)
            self.assertEqual(exported["rows"], 1)
            self.assertIn("dicom_age_zero_review", csv_path.read_text())
            conn.close()

    def test_sex_codes_follow_dataset_specific_dictionaries(self) -> None:
        self.assertEqual(
            CLINICAL.decode_dataset_concept(
                "HCC-TACE-Seg", "sex_at_birth", "1.0"
            ),
            "Male",
        )
        self.assertEqual(
            CLINICAL.decode_dataset_concept(
                "EA1141", "sex_at_birth", "1"
            ),
            "Female",
        )
        self.assertEqual(
            CLINICAL.decode_dataset_concept(
                "UNMAPPED", "sex_at_birth", "1"
            ),
            "1",
        )

    def test_idc_dictionary_matches_integral_float_codes_and_label_fallback(
        self,
    ) -> None:
        mapping = CLINICAL.idc_value_mapping(
            [
                {"option_code": "1.0", "option_description": "Male"},
                {"option_code": "2.0", "option_description": "Female"},
            ]
        )
        self.assertEqual(mapping["1"], "Male")
        self.assertEqual(mapping["2"], "Female")
        label_mapping = CLINICAL.idc_value_mapping(
            [
                {"option_code": "1", "option_description": None},
                {"option_code": "2", "option_description": None},
            ],
            "Sex: 1=Male, 2=Female",
        )
        self.assertEqual(label_mapping, {"1": "Male", "2": "Female"})

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
                     'Virtual breast phantom trial', '', '');
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

    def test_permanent_screening_review_survives_wordpress_text_changes(
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
                    ('EA1141', 'EA1141', '', '2026-01-01', 'Collection', 0,
                     'Breast Cancer', 'Breast', 'Updated page text', '', '');
                """
            )
            snapshot_conn.commit()
            snapshot_conn.close()
            conn = CLINICAL.init_db(root / "clinical.sqlite", replace=True)
            conn.execute(
                """INSERT INTO clinical_imaging_subjects VALUES
                   ('ea1141:unknown', 'EA1141', 'UNKNOWN', 'test')"""
            )
            result = CLINICAL.apply_wordpress_dataset_inferences(conn, snapshot)
            audit = conn.execute(
                """SELECT eligibility_reason, review_required,
                          screening_signal, subjects_applied
                   FROM clinical_dataset_inferences
                   WHERE short_title = 'EA1141'
                     AND concept = 'primary_site'"""
            ).fetchone()
            self.assertEqual(result["screening_reviews_required"], 1)
            self.assertEqual(
                tuple(audit),
                (
                    "screening_review_required",
                    1,
                    "curated:permanent_screening_review",
                    0,
                ),
            )
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM clinical_facts
                       WHERE short_title = 'EA1141'"""
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
                                {"option_code": "1.0", "option_description": "Male"},
                                {"option_code": "2.0", "option_description": "Female"},
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
            self.assertEqual(subject["sex_at_birth"], "Female")
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
        try:
            subprocess.run(
                [sys.executable, str(SCRIPT), *arguments],
                check=True,
                text=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            self.fail(
                f"command failed with {exc.returncode}: "
                f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
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
