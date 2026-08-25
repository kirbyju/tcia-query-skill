from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_nifti_metadata_harvest.py"
SPEC = importlib.util.spec_from_file_location("tcia_nifti_metadata_harvest", SCRIPT)
assert SPEC and SPEC.loader
HARVEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARVEST)


class ReviewedPathIdentityTests(unittest.TestCase):
    def test_bcbm_scan_suffix_is_projected_to_patient_grain(self) -> None:
        for scan_id, patient_id in (
            ("BCBM-RadioGenomics-0-0", "BCBM-RadioGenomics-0"),
            ("BCBM-RadioGenomics-101-0", "BCBM-RadioGenomics-101"),
        ):
            package_path = f"BCBM/{scan_id}/{scan_id}_image_ss_n4.nii.gz"
            self.assertEqual(
                HARVEST.infer_patient_id_from_path(
                    "BCBM-RadioGenomics", package_path
                ),
                (patient_id, "bcbm_reviewed_scan_suffix"),
            )
            self.assertEqual(
                HARVEST.bcbm_patient_and_scan_id(
                    "BCBM-RadioGenomics", package_path
                ),
                (patient_id, scan_id),
            )


class CtOrgCharacteristicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        HARVEST.create_output_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO nifti_downloads
            (download_row_id, parent_source, dataset_type, short_title, title,
             download_id, download_title, download_types, data_types, file_types)
            VALUES
            (1, 'collections', 'Collection', 'CT-ORG', 'CT volumes',
             '42077', 'Images', '["Radiology Images"]',
             '["Segmentation", "CT"]', '["NIfTI"]')
            """
        )
        rows = []
        for case_number in (0, 1):
            subject_id = f"CT-ORG-{case_number}"
            study_id = HARVEST.stable_hash_id("study", "CT-ORG", subject_id)
            for role in ("volume", "labels"):
                radiology_id = f"rad_{role}_{case_number}"
                rows.append(
                    (
                        radiology_id,
                        "CT-ORG",
                        "Collection",
                        '["42077"]',
                        f"{role}-{case_number}.nii.gz",
                        f"CT-ORG/OrganSegmentations/{role}-{case_number}.nii.gz",
                        subject_id,
                        study_id,
                        "synthetic_from_subject_id",
                        radiology_id,
                        "synthetic_from_file_path",
                        "CT",
                        "segmentation" if role == "labels" else "NIfTI image",
                        1 if role == "labels" else 0,
                    )
                )
        self.conn.executemany(
            """
            INSERT INTO radiology_series
            (radiology_id, short_title, dataset_type, download_ids, file_name,
             package_path, subject_id, study_id, study_id_source, series_id,
             series_id_source, modality, object_type, is_derived_object)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        for case_number in (0, 1):
            derived_id = f"der_{case_number}"
            label_id = f"rad_labels_{case_number}"
            volume_id = f"rad_volume_{case_number}"
            self.conn.execute(
                """
                INSERT INTO derived_objects
                (derived_object_id, radiology_id, short_title, dataset_type, file_name,
                 package_path, file_ext, source_nifti_volume_id,
                 derived_object_type, segmentation_representation)
                VALUES (?, ?, 'CT-ORG', 'Collection', ?, ?, '.nii.gz', ?,
                        'segmentation', 'labelmap')
                """,
                (
                    derived_id,
                    label_id,
                    f"labels-{case_number}.nii.gz",
                    f"CT-ORG/OrganSegmentations/labels-{case_number}.nii.gz",
                    volume_id,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO derived_object_references
                (derived_object_reference_id, derived_object_id, derived_radiology_id,
                 source_nifti_volume_id, source_nifti_volume_file_name,
                 reference_role, inference_method, confidence)
                VALUES (?, ?, ?, ?, ?, 'source_image',
                        'label_volume_filename_pair', 'high')
                """,
                (
                    f"ref_{case_number}",
                    derived_id,
                    label_id,
                    volume_id,
                    f"volume-{case_number}.nii.gz",
                ),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_ct_org_characteristics_separate_images_and_segmentations(self) -> None:
        self.assertEqual(HARVEST.build_ct_org_file_characteristics(self.conn), 4)
        summary = self.conn.execute(
            "SELECT * FROM agent_nifti_characteristics_summary WHERE short_title = 'CT-ORG'"
        ).fetchone()
        self.assertEqual(summary["ct_source_image_files"], 2)
        self.assertEqual(summary["ct_associated_segmentations"], 2)
        self.assertEqual(summary["study_ids"], 2)

    def test_ct_org_segmentation_uses_wordpress_semantics_and_source_modality(self) -> None:
        HARVEST.build_ct_org_file_characteristics(self.conn)
        row = self.conn.execute(
            """
            SELECT * FROM agent_nifti_characteristics
            WHERE file_name = 'labels-0.nii.gz'
            """
        ).fetchone()
        self.assertEqual(row["object_role"], "segmentation")
        self.assertEqual(row["associated_imaging_modality"], "CT")
        self.assertEqual(
            row["imaging_modality_relationship"], "associated_with_source_nifti_volume"
        )
        self.assertEqual(set(json.loads(row["wordpress_data_types"])), {"CT", "Segmentation"})
        self.assertEqual(row["source_nifti_volume_file_name"], "volume-0.nii.gz")
        self.assertIsNone(row["source_dicom_series_instance_uid"])
        self.assertEqual(
            row["study_id"], HARVEST.stable_hash_id("study", "CT-ORG", "CT-ORG-0")
        )
        self.assertEqual(row["study_id_source"], "synthetic_from_subject_id")

    def test_wordpress_labels_are_stored_once_and_joined_into_the_view(self) -> None:
        HARVEST.build_ct_org_file_characteristics(self.conn)
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(nifti_file_characteristics)")
        }
        self.assertFalse(
            {"file_format", "wordpress_download_types", "wordpress_data_types", "wordpress_file_types"}
            & columns
        )
        rule_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(nifti_classification_rules)")
        }
        self.assertNotIn("file_format", rule_columns)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nifti_classification_rules").fetchone()[0],
            1,
        )
        view_row = self.conn.execute(
            "SELECT wordpress_file_types FROM agent_nifti_characteristics LIMIT 1"
        ).fetchone()
        self.assertEqual(view_row["wordpress_file_types"], '["NIfTI"]')


class BcbmRadiogenomicsCharacteristicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        HARVEST.create_output_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO nifti_downloads
            (download_row_id, parent_source, dataset_type, short_title, title,
             download_id, download_title, download_types, data_types, file_types)
            VALUES
            (1, 'analysis-results', 'Analysis Result', 'BCBM-RadioGenomics',
             'BCBM images and masks', '50097', 'Images and Segmentations',
             '["Radiology Images"]', '["MR", "Segmentation"]', '["NIfTI"]')
            """
        )
        study_id = "study_existing_parent_path_session"
        self.conn.executemany(
            """
            INSERT INTO radiology_series
            (radiology_id, short_title, dataset_type, download_ids, file_name,
             package_path, subject_id, study_id, study_id_source, series_id,
             series_id_source, modality, object_type, is_derived_object)
            VALUES (?, 'BCBM-RadioGenomics', 'Analysis Result', '["50097"]', ?, ?,
                    'BCBM-RadioGenomics-0-0', ?, 'synthetic_from_parent_path', ?,
                    'synthetic_from_file_path', 'MR', ?, ?)
            """,
            [
                (
                    "rad_image",
                    "BCBM-RadioGenomics-0-0_image_ss_n4.nii.gz",
                    "BCBM-RadioGenomics-0-0/BCBM-RadioGenomics-0-0_image_ss_n4.nii.gz",
                    study_id,
                    "rad_image",
                    "NIfTI image",
                    0,
                ),
                (
                    "rad_mask_1",
                    "BCBM-RadioGenomics-0-0_mask_tumor-1.nii.gz",
                    "BCBM-RadioGenomics-0-0/BCBM-RadioGenomics-0-0_mask_tumor-1.nii.gz",
                    study_id,
                    "rad_mask_1",
                    "segmentation",
                    1,
                ),
                (
                    "rad_mask_2",
                    "BCBM-RadioGenomics-0-0_mask_tumor-2.nii.gz",
                    "BCBM-RadioGenomics-0-0/BCBM-RadioGenomics-0-0_mask_tumor-2.nii.gz",
                    study_id,
                    "rad_mask_2",
                    "segmentation",
                    1,
                ),
            ],
        )
        for suffix in ("1", "2"):
            derived_id = f"der_{suffix}"
            radiology_id = f"rad_mask_{suffix}"
            self.conn.execute(
                """
                INSERT INTO derived_objects
                (derived_object_id, radiology_id, short_title, derived_object_type,
                 segmentation_representation)
                VALUES (?, ?, 'BCBM-RadioGenomics', 'segmentation', 'binary_mask')
                """,
                (derived_id, radiology_id),
            )
            self.conn.execute(
                """
                INSERT INTO derived_object_references
                (derived_object_reference_id, derived_object_id, derived_radiology_id,
                 source_nifti_volume_id, source_nifti_volume_file_name, reference_role,
                 inference_method, confidence)
                VALUES (?, ?, ?, 'rad_image',
                        'BCBM-RadioGenomics-0-0_image_ss_n4.nii.gz', 'source_image',
                        'mask_prefix_same_folder', 'high')
                """,
                (f"ref_{suffix}", derived_id, radiology_id),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_bcbm_rule_classifies_images_and_linked_binary_masks(self) -> None:
        self.assertEqual(
            HARVEST.build_bcbm_radiogenomics_file_characteristics(self.conn), 3
        )
        summary = self.conn.execute(
            """
            SELECT * FROM agent_nifti_characteristics_summary
            WHERE short_title = 'BCBM-RadioGenomics'
            """
        ).fetchone()
        self.assertEqual(summary["mr_source_image_files"], 1)
        self.assertEqual(summary["mr_associated_segmentations"], 2)
        self.assertEqual(summary["study_ids"], 1)
        mask = self.conn.execute(
            """
            SELECT * FROM agent_nifti_characteristics
            WHERE file_name = 'BCBM-RadioGenomics-0-0_mask_tumor-1.nii.gz'
            """
        ).fetchone()
        self.assertEqual(mask["object_role"], "segmentation")
        self.assertEqual(mask["associated_imaging_modality"], "MR")
        self.assertEqual(mask["study_id"], "study_existing_parent_path_session")
        self.assertEqual(mask["source_nifti_volume_id"], "rad_image")
        self.assertEqual(set(json.loads(mask["wordpress_data_types"])), {"MR", "Segmentation"})


class VestibularSchwannomaMcRc2CharacteristicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        HARVEST.create_output_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO nifti_downloads
            (download_row_id, parent_source, dataset_type, short_title, title,
             download_id, download_title, download_types, data_types, file_types)
            VALUES
            (1, 'analysis-results', 'Analysis Result',
             'Vestibular-Schwannoma-MC-RC2', 'VS longitudinal MRI', '52880',
             'Images and Segmentations', '["Radiology Images", "Image Annotations"]',
             '["MR", "Segmentation"]', '["NIfTI"]')
            """
        )
        subject_id = "VS_MC_RC2_001"
        rows = []
        for date, scan_types in (
            ("1991-04-28", ("T1", "T1C", "T1C_seg")),
            ("1993-06-01", ("T1C", "T1C_seg")),
        ):
            study_id = HARVEST.stable_hash_id(
                "study", "Vestibular-Schwannoma-MC-RC2", subject_id, date
            )
            for scan_type in scan_types:
                radiology_id = f"rad_{date}_{scan_type}"
                file_name = f"{subject_id}_{date}_{scan_type}.nii.gz"
                is_segmentation = scan_type.endswith("_seg")
                rows.append(
                    (
                        radiology_id,
                        file_name,
                        file_name,
                        subject_id,
                        study_id,
                        date,
                        radiology_id,
                        "segmentation" if is_segmentation else "NIfTI image",
                        1 if is_segmentation else 0,
                    )
                )
        self.conn.executemany(
            """
            INSERT INTO radiology_series
            (radiology_id, short_title, dataset_type, download_ids, file_name,
             package_path, subject_id, study_id, study_id_source, study_date,
             series_id, series_id_source, modality, object_type, is_derived_object)
            VALUES (?, 'Vestibular-Schwannoma-MC-RC2', 'Analysis Result', '["52880"]',
                    ?, ?, ?, ?, 'synthetic_from_subject_id_and_study_date', ?, ?,
                    'synthetic_from_file_path', 'MR', ?, ?)
            """,
            rows,
        )
        for date in ("1991-04-28", "1993-06-01"):
            derived_id = f"der_{date}"
            derived_radiology_id = f"rad_{date}_T1C_seg"
            source_radiology_id = f"rad_{date}_T1C"
            source_file_name = f"{subject_id}_{date}_T1C.nii.gz"
            self.conn.execute(
                """
                INSERT INTO derived_objects
                (derived_object_id, radiology_id, short_title, derived_object_type,
                 segmentation_representation, source_nifti_volume_id)
                VALUES (?, ?, 'Vestibular-Schwannoma-MC-RC2', 'segmentation',
                        'segmentation_file', ?)
                """,
                (derived_id, derived_radiology_id, source_radiology_id),
            )
            self.conn.execute(
                """
                INSERT INTO derived_object_references
                (derived_object_reference_id, derived_object_id, derived_radiology_id,
                 source_nifti_volume_id, source_nifti_volume_file_name,
                 reference_role, inference_method, confidence)
                VALUES (?, ?, ?, ?, ?, 'source_image',
                        'trailing_modality_segmentation_pair', 'high')
                """,
                (
                    f"ref_{date}",
                    derived_id,
                    derived_radiology_id,
                    source_radiology_id,
                    source_file_name,
                ),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_longitudinal_sessions_are_distinct_studies(self) -> None:
        self.assertEqual(HARVEST.build_vs_mc_rc2_file_characteristics(self.conn), 5)
        summary = self.conn.execute(
            """
            SELECT * FROM agent_nifti_characteristics_summary
            WHERE short_title = 'Vestibular-Schwannoma-MC-RC2'
            """
        ).fetchone()
        self.assertEqual(summary["mr_source_image_files"], 3)
        self.assertEqual(summary["mr_associated_segmentations"], 2)
        self.assertEqual(summary["study_ids"], 2)
        studies = self.conn.execute(
            """
            SELECT study_date, COUNT(DISTINCT study_id) AS studies
            FROM radiology_series
            WHERE short_title = 'Vestibular-Schwannoma-MC-RC2'
            GROUP BY study_date
            ORDER BY study_date
            """
        ).fetchall()
        self.assertEqual([row["studies"] for row in studies], [1, 1])
        self.assertNotEqual(
            self.conn.execute(
                "SELECT study_id FROM radiology_series WHERE study_date = '1991-04-28' LIMIT 1"
            ).fetchone()[0],
            self.conn.execute(
                "SELECT study_id FROM radiology_series WHERE study_date = '1993-06-01' LIMIT 1"
            ).fetchone()[0],
        )


class NlstNewLesionLongCtCharacteristicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        HARVEST.create_output_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO nifti_downloads
            (download_row_id, parent_source, dataset_type, short_title, title,
             download_id, download_title, data_types, file_types)
            VALUES (1, 'analysis-results', 'Analysis Result',
                    'NLST-New-lesion-LongCT', 'Longitudinal CT', '51634',
                    'CT and point annotation images', '["Fiducial", "CT"]', '["NIfTI"]')
            """
        )
        rows = [
            ("rad_ct_a", "file_ct_a", "2-11111.nii.gz", "pkg/100/date-a/2-11111.nii.gz", "source_image", 0),
            ("rad_ct_b", "file_ct_b", "3-22222.nii.gz", "pkg/100/date-b/3-22222.nii.gz", "source_image", 0),
            ("rad_point", "file_point", "point_1.nii.gz", "pkg/100/date-b/point_1.nii.gz", "annotation", 1),
            ("rad_reg", "file_reg", "2-11111_3-22222.nii.gz", "pkg/100/register_Byr0_FUyr1/2-11111_3-22222.nii.gz", "derived image", 1),
            ("rad_transfer", "file_transfer", "long-name.nii.gz", "100/date-a/long-name.nii.gz", "source_image", 0),
        ]
        for radiology_id, file_id, file_name, package_path, object_type, is_derived in rows:
            inventory = '["package_files"]' if radiology_id != "rad_transfer" else "[]"
            self.conn.execute(
                """
                INSERT INTO non_dicom_files
                (non_dicom_file_id, short_title, package_path, file_name, inventory_sources,
                 metadata_sources, is_nifti)
                VALUES (?, 'NLST-New-lesion-LongCT', ?, ?, ?, '[]', 1)
                """,
                (file_id, package_path, file_name, inventory),
            )
            self.conn.execute(
                """
                INSERT INTO radiology_series
                (radiology_id, non_dicom_file_id, short_title, dataset_type, download_ids,
                 subject_id, file_name, package_path, modality, object_type,
                 is_derived_object, study_id, study_id_source, series_id,
                 series_id_source, series_description)
                VALUES (?, ?, 'NLST-New-lesion-LongCT', 'Analysis Result', '["51634"]',
                        '100', ?, ?, 'CT', ?, ?, 'study', 'source_metadata_crosswalk', ?,
                        'synthetic_from_file_path', ?)
                """,
                (
                    radiology_id,
                    file_id,
                    file_name,
                    package_path,
                    object_type,
                    is_derived,
                    radiology_id,
                    "Point fiducial annotation" if radiology_id == "rad_point" else (
                        "Longitudinal registered CT volume" if radiology_id == "rad_reg" else "CT source volume"
                    ),
                ),
            )
        for derived_id, radiology_id, derived_type in (
            ("der_point", "rad_point", "annotation"),
            ("der_reg", "rad_reg", "derived_image"),
        ):
            self.conn.execute(
                """
                INSERT INTO derived_objects
                (derived_object_id, radiology_id, short_title, derived_object_type)
                VALUES (?, ?, 'NLST-New-lesion-LongCT', ?)
                """,
                (derived_id, radiology_id, derived_type),
            )
        for reference_id, derived_id, derived_radiology_id, source_id in (
            ("ref_point", "der_point", "rad_point", "rad_ct_b"),
            ("ref_reg_a", "der_reg", "rad_reg", "rad_ct_a"),
            ("ref_reg_b", "der_reg", "rad_reg", "rad_ct_b"),
        ):
            self.conn.execute(
                """
                INSERT INTO derived_object_references
                (derived_object_reference_id, derived_object_id, derived_radiology_id,
                 source_nifti_volume_id, source_nifti_volume_file_name,
                 reference_role, inference_method, confidence)
                VALUES (?, ?, ?, ?, ?, 'source_image', 'test', 'high')
                """,
                (reference_id, derived_id, derived_radiology_id, source_id, source_id),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_package_scope_roles_and_unambiguous_preferred_source(self) -> None:
        self.assertEqual(HARVEST.build_nlst_new_lesion_longct_file_characteristics(self.conn), 4)
        summary = self.conn.execute(
            "SELECT * FROM agent_nifti_characteristics_summary WHERE short_title = 'NLST-New-lesion-LongCT'"
        ).fetchone()
        self.assertEqual(summary["characterized_files"], 4)
        self.assertEqual(summary["source_image_files"], 2)
        self.assertEqual(summary["derived_image_files"], 1)
        self.assertEqual(summary["fiducial_annotation_files"], 1)
        registered = self.conn.execute(
            "SELECT * FROM agent_nifti_characteristics WHERE radiology_id = 'rad_reg'"
        ).fetchone()
        self.assertEqual(registered["source_reference_count"], 2)
        self.assertIsNone(registered["source_nifti_volume_id"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM agent_nifti_characteristics WHERE radiology_id = 'rad_reg'"
            ).fetchone()[0],
            1,
        )


class RadiomicFeatureStandardsCharacteristicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        HARVEST.create_output_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO nifti_downloads
            (download_row_id, parent_source, dataset_type, short_title, title,
             download_id, download_title, data_types, file_types)
            VALUES (1, 'analysis-results', 'Analysis Result',
                    'Radiomic-Feature-Standards', 'Radiomic standards', '46067',
                    'Segmentation', '["Segmentation"]', '["NIfTI", "ZIP"]')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO nifti_file_series
            (short_title, dataset_type, download_ids, download_titles, file_name,
             file_ext, package_path, inventory_sources, metadata_sources,
             source_row_count)
            VALUES ('Radiomic-Feature-Standards', 'Analysis Result', '["46067"]',
                    '["Segmentation"]', ?, '.nii', ?, '["package_files"]', '[]', 1)
            """,
            [
                (
                    "LIDC_IDRI_0314_alg01_run1.nii",
                    "package/LIDC_IDRI-0314/LIDC_IDRI_0314_alg01_run1.nii",
                ),
                (
                    "SEG-Phantom-100.0-1.0-1.0-1.0-9.0-0.0-100.0-10.0-0.0-0.0.nii",
                    "package/SEG-Phantom-100.0-1.0-1.0-1.0-9.0-0.0-100.0-10.0-0.0-0.0.nii",
                ),
            ],
        )
        self.assertEqual(HARVEST.apply_radiomic_feature_standards_mapping(self.conn), 2)
        HARVEST.build_canonical_non_dicom_layer(self.conn)
        self.assertEqual(
            HARVEST.apply_radiomic_feature_standards_external_source_references(self.conn), 4
        )
        self.assertEqual(
            HARVEST.build_radiomic_feature_standards_file_characteristics(self.conn), 2
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_exact_source_ct_and_alternate_dicom_seg_are_distinct(self) -> None:
        rows = self.conn.execute(
            """
            SELECT * FROM agent_nifti_characteristics
            WHERE short_title = 'Radiomic-Feature-Standards'
            ORDER BY subject_id
            """
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["object_role"] for row in rows}, {"segmentation"})
        self.assertEqual({row["associated_imaging_modality"] for row in rows}, {"CT"})
        self.assertEqual({row["source_reference_count"] for row in rows}, {1})
        self.assertTrue(all(row["source_dicom_series_instance_uid"] for row in rows))
        self.assertTrue(all(row["alternate_dicom_seg_series_instance_uid"] for row in rows))
        self.assertTrue(
            all(
                row["source_dicom_series_instance_uid"]
                != row["alternate_dicom_seg_series_instance_uid"]
                for row in rows
            )
        )
        self.assertTrue(
            all(
                row["source_dicom_study_instance_uid"]
                == row["alternate_dicom_seg_study_instance_uid"]
                for row in rows
            )
        )


class HealthyTotalBodyCtsCharacteristicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.controlled_db = Path(self.temp_dir.name) / "controlled.sqlite"
        controlled = sqlite3.connect(self.controlled_db)
        controlled.execute(
            """
            CREATE TABLE agent_controlled_files (
                short_title TEXT, patient_id TEXT, study_instance_uid TEXT,
                series_instance_uid TEXT, series_description TEXT
            )
            """
        )
        controlled.executemany(
            """
            INSERT INTO agent_controlled_files VALUES
            ('Healthy-Total-Body-CTs', ?, ?, ?, ?)
            """,
            [
                ("Healthy-Total-Body-CTs-001", "study_001", "ct_90min_001", "CT 90min"),
                ("Healthy-Total-Body-CTs-031", "study_031", "ct_90min_031", "CT 90min"),
            ],
        )
        controlled.commit()
        controlled.close()

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        HARVEST.create_output_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO nifti_downloads
            (download_row_id, parent_source, dataset_type, short_title, title,
             download_id, download_title, data_types, file_types, license_label,
             access_level)
            VALUES (1, 'collections', 'Collection', 'Healthy-Total-Body-CTs',
                    'Healthy CT', '42741', 'Segmentations', '["Segmentation"]',
                    '["NIfTI", "ZIP"]', 'CC BY 4.0', 'open')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO nifti_file_series
            (short_title, dataset_type, download_ids, download_titles, file_name,
             file_ext, package_path, inventory_sources, metadata_sources,
             source_row_count)
            VALUES ('Healthy-Total-Body-CTs', 'Collection', '["42741"]',
                    '["Segmentations"]', ?, '.nii.gz', ?, '["package_files"]', '[]', 1)
            """,
            [
                ("Healthy-Total-Body-CTs-001.nii.gz", "package/Healthy-Total-Body-CTs-001.nii.gz"),
                ("Healthy-Total-Body-CTs-014.nii.gz", "package/Healthy-Total-Body-CTs-014.nii.gz"),
            ],
        )
        self.assertEqual(
            HARVEST.apply_healthy_total_body_cts_mapping(self.conn, self.controlled_db), 2
        )
        HARVEST.build_canonical_non_dicom_layer(self.conn)
        self.assertEqual(
            HARVEST.apply_healthy_total_body_cts_external_source_references(
                self.conn, self.controlled_db
            ),
            2,
        )
        self.assertEqual(HARVEST.build_healthy_total_body_cts_file_characteristics(self.conn), 2)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_exact_and_manual_review_controlled_sources_stay_distinct(self) -> None:
        exact = self.conn.execute(
            """
            SELECT * FROM agent_nifti_characteristics
            WHERE subject_id = 'Healthy-Total-Body-CTs-001'
            """
        ).fetchone()
        unresolved = self.conn.execute(
            """
            SELECT * FROM agent_nifti_characteristics
            WHERE subject_id = 'Healthy-Total-Body-CTs-014'
            """
        ).fetchone()
        self.assertEqual(exact["source_access_level"], "controlled")
        self.assertEqual(exact["source_dicom_series_instance_uid"], "ct_90min_001")
        self.assertEqual(exact["imaging_modality_relationship"], "associated_with_source_dicom_series")
        self.assertEqual(unresolved["source_access_level"], "controlled")
        self.assertEqual(unresolved["source_dicom_series_instance_uid"], "")
        self.assertEqual(
            unresolved["imaging_modality_relationship"],
            "associated_with_controlled_source_dataset",
        )
        self.assertEqual(unresolved["classification_confidence"], "medium")
        flag = self.conn.execute(
            "SELECT quality_flag_json FROM radiology_series WHERE subject_id = 'Healthy-Total-Body-CTs-014'"
        ).fetchone()[0]
        self.assertEqual(json.loads(flag)["status"], "manual_review")


class RemainingDatasetReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        HARVEST.create_output_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_breast_filename_recovers_subject_and_visit(self) -> None:
        self.conn.execute(
            """
            INSERT INTO nifti_file_series
            (short_title, dataset_type, download_ids, download_titles, file_name,
             file_ext, package_path, inventory_sources, metadata_sources, source_row_count)
            VALUES ('BreastDCEDL_ISPY2', 'Analysis Result', '["52648"]',
                    '["Images and Segmentations"]',
                    'ISPY2-100899_spy2_vis1_dce_aqc_3.nii.gz', '.nii.gz',
                    'ISPY2-100899_spy2_vis1_dce_aqc_3.nii.gz',
                    '["package_files"]', '[]', 1)
            """
        )
        self.assertEqual(HARVEST.apply_remaining_dataset_mappings(self.conn), 1)
        row = self.conn.execute(
            "SELECT * FROM nifti_file_series WHERE short_title = 'BreastDCEDL_ISPY2'"
        ).fetchone()
        self.assertEqual(row["PatientID"], "ISPY2-100899")
        self.assertEqual(row["StudyDescription"], "visit_1")
        self.assertEqual(row["SeriesDescription"], "DCE AQC temporal volume 3")
        study_id, source = HARVEST.infer_study_id(row)
        self.assertTrue(study_id)
        self.assertEqual(source, "synthetic_from_subject_id_and_visit")

    def test_dro_rule_pairs_segmentation_with_exact_nifti_source(self) -> None:
        self.conn.execute(
            """
            INSERT INTO nifti_downloads
            (download_row_id, parent_source, dataset_type, short_title, download_id,
             download_title, data_types, file_types, access_level)
            VALUES (1, 'collections', 'Collection', 'DRO-Toolkit', '43679',
                    'Images and Segmentations', '["CT", "Segmentation"]', '["NIfTI"]', 'open')
            """
        )
        for role in ("SERIES", "SEG"):
            radiology_id = role.lower()
            self.conn.execute(
                """
                INSERT INTO radiology_series
                (radiology_id, short_title, dataset_type, download_ids, file_name,
                 package_path, subject_id, study_id, study_id_source, series_id,
                 series_id_source, modality, object_type, is_derived_object)
                VALUES (?, 'DRO-Toolkit', 'Collection', '["43679"]', ?, ?, 'Phantom-X',
                        'study_x', 'synthetic_from_subject_id', ?, 'synthetic_from_file_path',
                        'CT', ?, ?)
                """,
                (
                    radiology_id,
                    f"{role}-Phantom-X.nii",
                    f"DRO/{role}-Phantom-X.nii",
                    radiology_id,
                    "segmentation" if role == "SEG" else "NIfTI image",
                    1 if role == "SEG" else 0,
                ),
            )
        self.conn.execute(
            """
            INSERT INTO derived_objects
            (derived_object_id, radiology_id, short_title, dataset_type, file_name,
             package_path, derived_object_type, segmentation_representation)
            VALUES ('derived_seg', 'seg', 'DRO-Toolkit', 'Collection',
                    'SEG-Phantom-X.nii', 'DRO/SEG-Phantom-X.nii',
                    'segmentation', 'labelmap')
            """
        )
        self.conn.execute(
            """
            INSERT INTO derived_object_references
            (derived_object_reference_id, derived_object_id, derived_radiology_id,
             source_nifti_volume_id, source_nifti_volume_file_name, reference_role,
             inference_method, confidence)
            VALUES ('ref', 'derived_seg', 'seg', 'series', 'SERIES-Phantom-X.nii',
                    'source_image', 'dro_seg_series_exact_configuration_pair', 'high')
            """
        )
        self.assertEqual(HARVEST.build_remaining_reviewed_file_characteristics(self.conn), 2)
        segmentation = self.conn.execute(
            "SELECT * FROM agent_nifti_characteristics WHERE radiology_id = 'seg'"
        ).fetchone()
        self.assertEqual(segmentation["object_role"], "segmentation")
        self.assertEqual(segmentation["associated_imaging_modality"], "CT")
        self.assertEqual(
            segmentation["imaging_modality_relationship"],
            "associated_with_source_nifti_volume",
        )

    def test_review_issue_schema_is_compact_dataset_grain(self) -> None:
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(nifti_dataset_review_issues)")
        }
        self.assertIn("affected_files", columns)
        self.assertNotIn("radiology_id", columns)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name='agent_nifti_review_issues'"
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
