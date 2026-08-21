from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from mcp_server.tcia_query_mcp.service import TciaQueryService


def q(value):
    return json.dumps(value)


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_base_snapshot(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE snapshot_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE agent_datasets (
                source TEXT, dataset_type TEXT, id TEXT, slug TEXT, short_title TEXT,
                title TEXT, doi TEXT, link TEXT, date_updated TEXT, current_version_number TEXT,
                hidden INTEGER, license_status TEXT, licenses TEXT, controlled_access INTEGER,
                noncommercial_license INTEGER, access_level TEXT, controlled_access_policy_url TEXT,
                subjects INTEGER, data_types TEXT, download_types TEXT, download_data_types TEXT,
                download_file_types TEXT, external_resources TEXT, external_resource_labels TEXT,
                cancer_types TEXT, cancer_locations TEXT, species TEXT, program TEXT,
                has_tcia_clinical_download INTEGER, has_external_clinical_resource INTEGER,
                source_collections TEXT, summary TEXT, abstract TEXT, detailed_description TEXT
            );
            CREATE TABLE agent_dataset_access_summary (
                source TEXT, dataset_type TEXT, id TEXT, slug TEXT, short_title TEXT,
                title TEXT, doi TEXT, link TEXT, date_updated TEXT, current_version_number TEXT,
                hidden INTEGER, license_status TEXT, licenses TEXT, controlled_access INTEGER,
                noncommercial_license INTEGER, access_level TEXT, controlled_access_policy_url TEXT,
                subjects INTEGER, data_types TEXT, download_types TEXT, download_data_types TEXT,
                download_file_types TEXT, external_resources TEXT, external_resource_labels TEXT,
                cancer_types TEXT, cancer_locations TEXT, species TEXT, program TEXT,
                has_tcia_clinical_download INTEGER, has_external_clinical_resource INTEGER,
                source_collections TEXT, summary TEXT, abstract TEXT, detailed_description TEXT,
                current_download_count INTEGER, controlled_download_count INTEGER,
                noncontrolled_download_count INTEGER, open_noncommercial_download_count INTEGER,
                controlled_download_titles TEXT, controlled_license_labels TEXT,
                controlled_download_ids TEXT, controlled_download_urls TEXT,
                resolved_access_level TEXT, resolved_controlled_access_policy_url TEXT
            );
            CREATE TABLE agent_current_downloads (
                download_row_id TEXT, parent_source TEXT, dataset_type TEXT, parent_id TEXT,
                parent_slug TEXT, short_title TEXT, title TEXT, hidden INTEGER,
                download_id TEXT, download_slug TEXT, download_title TEXT, download_url TEXT,
                download_metadata TEXT, search_url TEXT, date_updated TEXT, collection_status TEXT,
                description TEXT, license_label TEXT, license_url TEXT, requirements_label TEXT,
                requirements_url TEXT, requirements_text TEXT, download_size TEXT,
                download_size_unit TEXT, subjects INTEGER, studies INTEGER, series INTEGER,
                images INTEGER, download_types TEXT, data_types TEXT, file_types TEXT,
                external_resources TEXT, controlled_access INTEGER, noncommercial_license INTEGER,
                access_level TEXT, controlled_access_policy_url TEXT, raw_json TEXT
            );
            CREATE TABLE agent_dataset_versions (
                source TEXT, dataset_type TEXT, id TEXT, slug TEXT, short_title TEXT,
                title TEXT, doi TEXT, link TEXT, date_updated TEXT, current_version_number TEXT,
                subjects INTEGER, hidden INTEGER, version_row_id TEXT, version_id TEXT,
                version_slug TEXT, version_post_title TEXT, version_number TEXT,
                version_date TEXT, version_related_short_title TEXT, match_method TEXT,
                version_downloads TEXT, version_text TEXT, version_normalized_json TEXT,
                version_raw_json TEXT
            );
            CREATE TABLE agent_dataset_v1_releases (
                source TEXT, dataset_type TEXT, id TEXT, slug TEXT, short_title TEXT,
                title TEXT, doi TEXT, link TEXT, date_updated TEXT, current_version_number TEXT,
                subjects INTEGER, hidden INTEGER, v1_release_date TEXT,
                v1_release_date_source TEXT, version_id TEXT, version_slug TEXT,
                version_post_title TEXT, version_related_short_title TEXT, match_method TEXT
            );
            """
        )
        conn.execute("INSERT INTO snapshot_meta VALUES (?, ?)", ("schema_version", q("test")))
        dataset = (
            "collections",
            "Collection",
            "1",
            "tcga-brca",
            "TCGA-BRCA",
            "Breast cancer collection",
            "10.7937/test",
            "https://example.org/tcga-brca",
            "2026-01-02",
            "2",
            0,
            "Open (Creative Commons)",
            "CC BY 4.0",
            0,
            0,
            "open",
            "",
            100,
            "MR; CT",
            "Radiology Images",
            "MR; CT; SEG",
            "DICOM; CSV",
            "Clinical; Genomics",
            q(["Clinical", "Genomics"]),
            "Breast Cancer",
            "Breast",
            "Human",
            "TCGA",
            1,
            1,
            "",
            "Breast MRI and CT data",
            "",
            "",
        )
        conn.execute(
            """
            INSERT INTO agent_datasets VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            dataset,
        )
        conn.execute(
            """
            INSERT INTO agent_dataset_access_summary VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 1, 0, '', '', '', '', 'open', ''
            )
            """,
            dataset,
        )
        conn.execute(
            """
            INSERT INTO agent_current_downloads VALUES (
              'drow1', 'collections', 'Collection', '1', 'tcga-brca', 'TCGA-BRCA',
              'Breast cancer collection', 0, 'download-1', 'download-1',
              'DICOM images and SEG labels', 'https://example.org/download.csv', '',
              'https://example.org/search', '2026-01-02', '', 'SEG annotations',
              'Creative Commons Attribution 4.0', 'https://creativecommons.org/licenses/by/4.0/',
              '', '', '', '1', 'GB', 100, 10, 20, 2000,
              '["Radiology Images", "Image Annotations"]',
              '["MR", "SEG"]',
              '["DICOM", "CSV"]',
              '["Clinical"]',
              0, 0, 'open', '', '{}'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO agent_dataset_versions VALUES (
              'collections', 'Collection', '1', 'tcga-brca', 'TCGA-BRCA',
              'Breast cancer collection', '10.7937/test', 'https://example.org/tcga-brca',
              '2026-01-02', '2', 100, 0, 'vrow1', '101', 'tcga-brca-v1',
              'TCGA-BRCA Version 1', '1', '2025-05-01', 'TCGA-BRCA',
              'exact_short_title', '[]', 'Initial release', '{}', '{}'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO agent_dataset_v1_releases VALUES (
              'collections', 'Collection', '1', 'tcga-brca', 'TCGA-BRCA',
              'Breast cancer collection', '10.7937/test', 'https://example.org/tcga-brca',
              '2026-01-02', '2', 100, 0, '2025-05-01',
              'versions_endpoint_exact_short_title', '101', 'tcga-brca-v1',
              'TCGA-BRCA Version 1', 'TCGA-BRCA', 'exact_short_title'
            )
            """
        )


def create_controlled_db(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE controlled_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE agent_controlled_dataset_summary (
                short_title TEXT, dataset_type TEXT, title TEXT
            );
            CREATE TABLE agent_controlled_files (
                short_title TEXT, dataset_type TEXT, title TEXT, doi TEXT, route_system TEXT,
                download_id TEXT, download_title TEXT, access_level TEXT,
                controlled_access_policy_url TEXT, license_label TEXT, drs_uri TEXT,
                file_id TEXT, file_name TEXT, file_type TEXT, file_format TEXT,
                file_size_bytes INTEGER, study_name TEXT, study_accession TEXT,
                participant_id TEXT, patient_id TEXT, patient_sex TEXT, diagnosis TEXT,
                image_modality TEXT, modality TEXT, body_part_examined TEXT,
                study_instance_uid TEXT, series_instance_uid TEXT, series_description TEXT,
                manufacturer TEXT, source_manifest_url TEXT, source_metadata_url TEXT
            );
            INSERT INTO agent_controlled_dataset_summary VALUES
              ('AAPM-RT-MAC', 'Collection', 'Controlled RT collection');
            INSERT INTO agent_controlled_files VALUES (
              'AAPM-RT-MAC', 'Collection', 'Controlled RT collection', '10.7937/controlled',
              'ctdc', '99', 'CTDC manifest', 'controlled',
              'https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/',
              'NIH Controlled Data Access', 'drs://nci-crdc.datacommons.io/file-1',
              'file-1', 'rtstruct.dcm', 'DICOM', 'DICOM', 1234, 'Study', 'phs002192',
              'P1', 'Patient1', 'F', 'diagnosis', 'RTSTRUCT', 'RTSTRUCT', 'HEAD',
              '1.2.3', '4.5.6', 'RTSTRUCT', 'Test', 'https://example.org/manifest.csv',
              'https://example.org/metadata.csv'
            );
            """
        )


def create_nifti_db(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE harvest_meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO harvest_meta VALUES ('schema_version', '"test"');
            CREATE TABLE nifti_downloads (short_title TEXT);
            CREATE TABLE radiology_series (short_title TEXT);
            CREATE TABLE derived_objects (short_title TEXT);
            CREATE TABLE non_dicom_files (short_title TEXT);
            CREATE TABLE aspera_root_sums_inventory (short_title TEXT);
            CREATE TABLE agent_nifti_dataset_summary (
                parent_source TEXT, dataset_type TEXT, short_title TEXT, title TEXT,
                nifti_downloads INTEGER, nifti_files INTEGER, non_dicom_files INTEGER,
                sidecar_files INTEGER, package_metadata_files INTEGER,
                radiology_series_rows INTEGER, mr_files INTEGER, ct_files INTEGER,
                derived_radiology_rows INTEGER, derived_objects INTEGER,
                linked_derived_objects INTEGER, subject_ids INTEGER, study_ids INTEGER,
                series_ids INTEGER, download_ids TEXT, download_labels TEXT
            );
            CREATE TABLE agent_nifti_files (
                short_title TEXT, dataset_type TEXT, download_ids TEXT, download_id TEXT,
                file_name TEXT, package_path TEXT, subject_id TEXT, procedure_id TEXT,
                study_id TEXT, study_instance_uid TEXT, study_id_source TEXT, series_id TEXT,
                series_instance_uid TEXT, series_id_source TEXT, source_doi TEXT, modality TEXT,
                body_part_examined TEXT, study_date TEXT, series_date TEXT,
                study_description TEXT, series_description TEXT, series_number TEXT,
                manufacturer TEXT, manufacturer_model_name TEXT, software_versions TEXT,
                image_type TEXT, object_type TEXT, rows INTEGER, columns INTEGER,
                number_of_slices INTEGER, number_of_temporal_positions INTEGER,
                pixel_spacing_row_mm REAL, pixel_spacing_col_mm REAL,
                slice_thickness_mm REAL, spacing_between_slices_mm REAL,
                orientation_or_affine TEXT, is_phantom INTEGER, is_derived_object INTEGER,
                quality_flag_json TEXT
            );
            CREATE TABLE agent_nifti_derived_objects (
                derived_object_id TEXT, non_dicom_file_id TEXT, radiology_id TEXT,
                short_title TEXT, dataset_type TEXT, file_name TEXT, package_path TEXT,
                file_ext TEXT, source_nifti_volume_id TEXT,
                source_dicom_series_instance_uid TEXT, source_dicom_study_instance_uid TEXT,
                derived_object_type TEXT,
                segmentation_representation TEXT, segmentation_type TEXT, total_segments INTEGER,
                algorithm_type TEXT, algorithm_name TEXT, source_non_dicom_file_id TEXT,
                source_nifti_volume_file_name TEXT, source_nifti_volume_package_path TEXT,
                reference_role TEXT, inference_method TEXT, confidence TEXT, evidence_json TEXT
            );
            CREATE TABLE agent_nifti_characteristics (
                short_title TEXT, dataset_type TEXT, download_ids TEXT, subject_id TEXT,
                file_name TEXT, package_path TEXT, object_role TEXT,
                associated_imaging_modality TEXT, imaging_modality_relationship TEXT,
                study_id TEXT, study_id_source TEXT, study_date TEXT, series_date TEXT,
                series_id TEXT, series_description TEXT, file_metadata_sources TEXT,
                segmentation_representation TEXT, source_nifti_volume_file_name TEXT,
                source_nifti_volume_id TEXT, source_dataset_short_title TEXT,
                source_access_level TEXT, source_dicom_series_instance_uid TEXT,
                source_dicom_study_instance_uid TEXT,
                alternate_dicom_seg_series_instance_uid TEXT,
                alternate_dicom_seg_study_instance_uid TEXT,
                alternate_dicom_representation_count INTEGER,
                reference_inference_method TEXT, reference_confidence TEXT,
                source_reference_count INTEGER, classification_source TEXT,
                classification_confidence TEXT, wordpress_download_id TEXT,
                wordpress_download_types TEXT, wordpress_data_types TEXT,
                wordpress_file_types TEXT
            );
            CREATE TABLE agent_nifti_review_issues (
                review_issue_id TEXT, short_title TEXT, issue_code TEXT, severity TEXT,
                status TEXT, affected_files INTEGER, review_scope TEXT,
                description TEXT, evidence_json TEXT
            );
            CREATE TABLE package_files (
                short_title TEXT, download_id TEXT, download_title TEXT, package_path TEXT,
                file_name TEXT, file_ext TEXT, bytes INTEGER, checksum TEXT,
                checksum_algorithm TEXT, modified_time TEXT, is_metadata_candidate INTEGER
            );
            INSERT INTO agent_nifti_dataset_summary VALUES
              ('collections', 'Collection', 'BCBM-RadioGenomics', 'BCBM', 1, 2, 3, 1, 1, 2, 2, 0, 1, 1, 1, 2, 2, 2, '7', 'NIfTI package');
            INSERT INTO agent_nifti_files VALUES
              ('BCBM-RadioGenomics', 'Collection', '7', '7', 'image.nii.gz', 'sub/image.nii.gz',
               'S1', '', 'Study1', '1.2', 'source_metadata', 'Series1', '3.4',
               'source_metadata', '10.7937/nifti', 'MR', 'BRAIN', '', '', '', '',
               '', '', '', '', '', '', 10, 10, 20, 1, 1.0, 1.0, 2.0, 2.0, '', 0, 0, '{}');
            INSERT INTO agent_nifti_derived_objects VALUES
              ('derived-1', 'file-mask', 'rad-mask', 'BCBM-RadioGenomics', 'Collection',
               'mask.nii.gz', 'sub/mask.nii.gz', 'gz', 'rad-image', '', '',
               'segmentation', 'labelmap', 'tumor', 1, 'manual', 'test', 'file-image',
               'image.nii.gz', 'sub/image.nii.gz', 'source_image', 'filename', 'medium', '{}');
            INSERT INTO agent_nifti_characteristics VALUES
              ('BCBM-RadioGenomics', 'Collection', '7', 'S1', 'mask.nii.gz',
               'sub/mask.nii.gz', 'segmentation', 'MR',
               'associated_with_source_nifti_volume', 'Study1', 'source_metadata', '', '',
               'SeriesMask', 'Tumor mask', 'source_metadata', 'labelmap', 'image.nii.gz',
               'rad-image', 'BCBM-RadioGenomics', 'open', '', '', '', '', 0,
               'filename', 'medium', 1, 'dataset_review', 'high', '7',
               'Image Annotations', 'MR; Segmentation', 'NIfTI');
            INSERT INTO agent_nifti_review_issues VALUES
              ('review-1', 'BCBM-RadioGenomics', 'confirm_source', 'warning',
               'manual_review', 1, 'source_relationship', 'Confirm source volume.', '{}');
            INSERT INTO agent_nifti_review_issues VALUES
              ('review-2', 'BCBM-RadioGenomics', 'old_issue', 'info',
               'resolved', 1, 'inventory', 'Previously resolved.', '{}');
            INSERT INTO package_files VALUES
              ('BCBM-RadioGenomics', '7', 'NIfTI package', 'sub/image.nii.gz',
               'image.nii.gz', 'gz', 100, '', '', '', 0);
            """
        )


def create_pathology_db(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE pathology_meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO pathology_meta VALUES ('schema_version', '"test"');
            CREATE TABLE pathology_downloads (short_title TEXT);
            CREATE TABLE pathology_package_files (short_title TEXT);
            CREATE TABLE pathology_file_objects (short_title TEXT);
            CREATE TABLE pathdb_slide_crosswalk (short_title TEXT);
            CREATE TABLE agent_pathology_dataset_summary (
                parent_source TEXT, dataset_type TEXT, short_title TEXT, download_records INTEGER,
                downloads_with_pathdb_collection INTEGER, pathdb_collection_slide_count INTEGER,
                pathdb_collection_patient_count INTEGER, open_noncommercial_downloads INTEGER,
                package_inventory_status TEXT, package_file_rows INTEGER, file_object_rows INTEGER,
                download_ids TEXT, download_titles TEXT
            );
            CREATE TABLE agent_pathology_downloads (
                short_title TEXT, dataset_type TEXT, title TEXT, download_id TEXT,
                download_label TEXT, download_title TEXT, download_url TEXT, search_url TEXT,
                download_types TEXT, data_types TEXT, file_types TEXT, external_resources TEXT,
                access_level TEXT, controlled_access INTEGER, noncommercial_license INTEGER,
                license_label TEXT, license_url TEXT, requirements_label TEXT,
                requirements_text TEXT, download_size TEXT, download_size_unit TEXT,
                subjects INTEGER, studies INTEGER, series INTEGER, images INTEGER
            );
            CREATE TABLE agent_pathology_package_files (
                short_title TEXT, dataset_type TEXT, download_id TEXT, download_label TEXT,
                download_title TEXT, source_url TEXT, package_path TEXT, file_name TEXT,
                file_ext TEXT, file_role TEXT, bytes INTEGER, checksum TEXT,
                checksum_algorithm TEXT, modified_time TEXT, inventory_source TEXT,
                inventory_status TEXT
            );
            CREATE TABLE agent_pathology_file_objects (
                short_title TEXT, dataset_type TEXT, download_id TEXT, file_name TEXT,
                file_ext TEXT, package_path TEXT, file_group_id TEXT, file_role TEXT,
                bytes INTEGER, checksum TEXT, checksum_algorithm TEXT, object_modality TEXT,
                image_format TEXT, is_wsi INTEGER, is_micrograph INTEGER, is_codex INTEGER,
                is_metadata INTEGER, source_table TEXT, source_row_id TEXT
            );
            CREATE TABLE pathology_disparities (
                short_title TEXT, disparity_type TEXT, severity TEXT, pathdb_collection TEXT,
                message TEXT, evidence_json TEXT
            );
            INSERT INTO agent_pathology_dataset_summary VALUES
              ('collections', 'Collection', 'CPTAC-STAD', 1, 1, 10, 5, 0,
               'normalized_file_rows_available', 2, 2, '8', 'Aspera package');
            INSERT INTO agent_pathology_downloads VALUES
              ('CPTAC-STAD', 'Collection', 'CPTAC STAD', '8', 'Aspera package',
               'Aspera package', 'https://example.org/faspex', '', '["Pathology Images"]',
               '["Whole Slide Image"]', '["SVS"]', '[]', 'open', 0, 0, 'CC BY 4.0',
               '', '', '', '10', 'GB', 5, 0, 0, 0);
            INSERT INTO agent_pathology_package_files VALUES
              ('CPTAC-STAD', 'Collection', '8', 'Aspera package', 'Aspera package',
               'https://example.org/faspex', 'slide.svs', 'slide.svs', 'svs', 'wsi',
               200, '', '', '', 'aspera', 'ok');
            INSERT INTO agent_pathology_file_objects VALUES
              ('CPTAC-STAD', 'Collection', '8', 'slide.svs', 'svs', 'slide.svs',
               'g1', 'wsi', 200, '', '', 'SM', 'SVS', 1, 0, 0, 0, 'package', 'row1');
            INSERT INTO pathology_disparities VALUES
              ('CPTAC-STAD', 'package_without_pathdb', 'info', 'CPTAC-STAD',
               'test message', '{}');
            """
        )


def create_clinical_db(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE clinical_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO clinical_meta VALUES ('schema_version', '13');

            CREATE TABLE agent_clinical_dataset_summary (
              short_title TEXT, subjects INTEGER, all_source_subjects INTEGER,
              clinical_only_subjects INTEGER, subjects_with_conflicts INTEGER,
              subjects_with_inferred_diagnosis INTEGER, subjects_with_inferred_site INTEGER,
              source_kinds TEXT, concepts INTEGER
            );
            INSERT INTO agent_clinical_dataset_summary VALUES
              ('TCGA-BRCA', 1, 2, 1, 1, 0, 0,
               'idc_clinical,tcia_clinical_download', 2);

            CREATE TABLE agent_clinical_subjects (
              subject_key TEXT, short_title TEXT, subject_id TEXT, source_kinds TEXT,
              source_count INTEGER, conflict_count INTEGER, has_imaging INTEGER,
              sex_at_birth TEXT, race TEXT, ethnicity TEXT, age_at_diagnosis TEXT,
              age_at_enrollment_years TEXT, age_at_imaging_years TEXT,
              primary_diagnosis TEXT, primary_site TEXT, stage TEXT, grade TEXT,
              vital_status TEXT, days_to_death TEXT, days_to_last_followup TEXT,
              overall_survival_days TEXT, progression_free_survival_days TEXT,
              recurrence TEXT, progression TEXT, response TEXT, screening_result TEXT,
              primary_diagnosis_is_inferred INTEGER, primary_site_is_inferred INTEGER,
              resolved_values_json TEXT, resolved_sources_json TEXT, conflicts_json TEXT
            );
            INSERT INTO agent_clinical_subjects VALUES
              ('tcgabrcca:brca-1', 'TCGA-BRCA', 'BRCA-1', '["idc_clinical","tcia_clinical_download"]',
               2, 1, 1, 'Female', 'White', NULL, '55', NULL, NULL,
               'Breast Cancer', 'Breast', NULL, NULL, 'Alive', NULL, '800', '800', NULL,
               NULL, NULL, 'pCR', NULL, 0, 0,
               '{"primary_diagnosis":"Breast Cancer","response":"pCR"}',
               '{"response":{"source_kind":"tcia_clinical_download","priority":400}}',
               '{"response":["Yes","pCR"]}');
            CREATE TABLE agent_clinical_all_subjects AS SELECT * FROM agent_clinical_subjects;
            INSERT INTO agent_clinical_all_subjects VALUES
              ('tcgabrcca:brca-2', 'TCGA-BRCA', 'BRCA-2', '["tcia_clinical_download"]',
               1, 0, 0, 'Female', NULL, NULL, NULL, NULL, NULL,
               'Breast Cancer', 'Breast', NULL, NULL, NULL, NULL, NULL, NULL, NULL,
               NULL, NULL, NULL, NULL, 0, 0,
               '{"primary_diagnosis":"Breast Cancer"}', '{}', '{}');

            CREATE TABLE agent_clinical_facts (
              short_title TEXT, subject_id TEXT, concept TEXT, value_text TEXT,
              value_number REAL, unit TEXT, source_kind TEXT, source_priority INTEGER,
              source_url TEXT, source_date TEXT, original_column TEXT,
              evidence_scope TEXT, is_inferred INTEGER, provenance_json TEXT
            );
            INSERT INTO agent_clinical_facts VALUES
              ('TCGA-BRCA', 'BRCA-1', 'response', 'pCR', NULL, NULL,
               'tcia_clinical_download', 400, 'https://example.org/clinical.csv',
               '2026-01-01', 'pcr', 'patient', 0, '{"row_number":2}'),
              ('TCGA-BRCA', 'BRCA-1', 'response', 'Yes', NULL, NULL,
               'idc_clinical', 300, 'https://example.org/idc', 'v24',
               'response', 'patient', 0, '{"row_number":2}');

            CREATE TABLE agent_clinical_conflicts (
              short_title TEXT, subject_id TEXT, concept TEXT, distinct_values INTEGER,
              values_seen TEXT, source_kinds TEXT
            );
            INSERT INTO agent_clinical_conflicts VALUES
              ('TCGA-BRCA', 'BRCA-1', 'response', 2, 'Yes,pCR',
               'idc_clinical,tcia_clinical_download');
            """
        )


def create_participant_db(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE participant_inventory_meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO participant_inventory_meta VALUES ('schema_version', '6');
            CREATE TABLE participants (
                participant_key TEXT PRIMARY KEY, dataset_type TEXT, short_title TEXT,
                display_participant_id TEXT, identity_scope TEXT,
                within_dataset_identity_status TEXT, identity_resolution_method TEXT,
                cross_dataset_identity_status TEXT
            );
            INSERT INTO participants VALUES (
                'participant-1', 'Collection', 'TCGA-BRCA', 'BRCA-1', 'dataset_scoped',
                'resolved', 'source_identifier', 'not_asserted'
            ), (
                'participant-2', 'Collection', 'TCGA-BRCA', 'BRCA-2', 'dataset_scoped',
                'resolved', 'source_identifier', 'not_asserted'
            );
            CREATE TABLE participant_identifiers (
                participant_identifier_id TEXT PRIMARY KEY, participant_key TEXT,
                managed_system TEXT, identifier_namespace TEXT, raw_identifier TEXT,
                normalized_identifier TEXT, link_evidence TEXT, provenance_json TEXT
            );
            CREATE INDEX idx_test_identifiers_participant
                ON participant_identifiers(participant_key);
            INSERT INTO participant_identifiers VALUES
              ('identifier-1', 'participant-1', 'tcia_wordpress', 'tcia_subject_id',
               'BRCA-1', 'BRCA-1', 'source_identifier', '{}'),
              ('identifier-2', 'participant-1', 'crdc_idc', 'idc_patient_id',
               'brca-1', 'BRCA-1', 'casefolded_identifier_same_tcia_dataset', '{}');
            CREATE TABLE participant_assets (
                participant_asset_id TEXT PRIMARY KEY, participant_key TEXT,
                managed_system TEXT, source_artifact TEXT, access_level TEXT,
                data_domain TEXT, media_kind TEXT, modality TEXT, file_format TEXT,
                object_role TEXT, study_count INTEGER, series_count INTEGER,
                file_count INTEGER, known_size_bytes INTEGER,
                has_file_level_metadata INTEGER, detail_pointer TEXT, access_route TEXT,
                inventory_status TEXT, source_version TEXT, provenance_json TEXT
            );
            CREATE INDEX idx_test_assets_participant ON participant_assets(participant_key);
            INSERT INTO participant_assets VALUES
              ('base-asset-1', 'participant-1', 'crdc_idc', 'idc_metadata', 'open',
               'radiology', 'volume', 'MR', 'DICOM', 'source_image', 1, 2, 20,
               1000, 1, 'idc', 'https://example.org/idc', 'known', 'v24', '{}'),
              ('base-asset-2', 'participant-1', 'tcia_aspera',
               'public_non_dicom_metadata', 'open', 'radiology', 'volume', 'CT;SEG;SR',
               'NIfTI', 'source_image', 1, 1, 1, 500, 1, 'public_non_dicom',
               'https://example.org/nifti', 'known', 'v2', '{}'),
              ('base-asset-3', 'participant-1', 'tcia_wordpress',
               'clinical_metadata', 'controlled', 'clinical', 'table', NULL, 'CSV',
               'clinical_data', NULL, NULL, 1, 50, 1, 'clinical',
               'https://example.org/clinical', 'known', 'v2', '{}');
            CREATE TABLE agent_participant_search (
                participant_key TEXT, dataset_type TEXT, short_title TEXT,
                display_participant_id TEXT, source_namespace_count INTEGER,
                source_namespaces TEXT, inventory_rows INTEGER, has_open_data INTEGER,
                has_controlled_data INTEGER, has_public_dicom INTEGER,
                has_public_non_dicom INTEGER, has_clinical INTEGER, data_domains TEXT,
                modalities TEXT, file_formats TEXT, managed_systems TEXT
            );
            INSERT INTO agent_participant_search VALUES (
                'participant-1', 'Collection', 'TCGA-BRCA', 'BRCA-1', 2,
                'tcia_subject_id,idc_patient_id', 3, 1, 1, 1, 1, 1,
                'radiology,clinical', 'MR,CT', 'DICOM,NIfTI', 'tcia_wordpress,crdc_idc'
            );
            CREATE TABLE agent_participant_identifiers (
                participant_identifier_id TEXT, participant_key TEXT, managed_system TEXT,
                identifier_namespace TEXT, raw_identifier TEXT, normalized_identifier TEXT,
                link_evidence TEXT, provenance_json TEXT, dataset_type TEXT,
                short_title TEXT, display_participant_id TEXT
            );
            INSERT INTO agent_participant_identifiers VALUES (
                'identifier-1', 'participant-1', 'tcia_wordpress', 'tcia_subject_id',
                'BRCA-1', 'BRCA-1', 'source_identifier', '{}',
                'Collection', 'TCGA-BRCA', 'BRCA-1'
            );
            CREATE TABLE agent_participant_assets (
                participant_asset_id TEXT, participant_key TEXT, managed_system TEXT,
                source_artifact TEXT, access_level TEXT, data_domain TEXT, media_kind TEXT,
                modality TEXT, file_format TEXT, object_role TEXT, study_count INTEGER,
                series_count INTEGER, file_count INTEGER, known_size_bytes INTEGER,
                has_file_level_metadata INTEGER, detail_pointer TEXT, access_route TEXT,
                inventory_status TEXT, source_version TEXT, provenance_json TEXT,
                dataset_type TEXT, short_title TEXT, display_participant_id TEXT
            );
            INSERT INTO agent_participant_assets VALUES (
                'asset-1', 'participant-1', 'crdc_idc', 'idc_metadata', 'open',
                'radiology', 'volume', 'MR', 'DICOM', 'source_image', 1, 2, 20,
                1000, 1, 'idc', 'https://example.org/idc', 'known', 'v24', '{}',
                'Collection', 'TCGA-BRCA', 'BRCA-1'
            );
            CREATE TABLE agent_dataset_assets_without_participant_crosswalk (
                dataset_asset_id TEXT, dataset_type TEXT, short_title TEXT,
                managed_system TEXT, access_level TEXT, data_domain TEXT, media_kind TEXT,
                modality TEXT, file_format TEXT, object_role TEXT, asset_count INTEGER,
                explanation TEXT, detail_pointer TEXT, provenance_json TEXT
            );
            INSERT INTO agent_dataset_assets_without_participant_crosswalk VALUES (
                'unlinked-1', 'Collection', 'TCGA-BRCA', 'tcia_aspera', 'open',
                'radiology', 'volume', 'MR', 'MHA', 'source_image', 2,
                'No participant crosswalk.', 'public_non_dicom', '{}'
            );
            CREATE TABLE agent_participant_link_issues (
                issue_id TEXT, dataset_type TEXT, short_title TEXT, raw_identifier TEXT,
                issue_code TEXT, status TEXT, description TEXT, evidence_json TEXT
            );
            INSERT INTO agent_participant_link_issues VALUES (
                'issue-1', 'Collection', 'TCGA-BRCA', '', 'missing_crosswalk',
                'review_required', 'Participant mapping is unavailable.', '{}'
            );
            CREATE TABLE participant_inventory_sources (
                source_name TEXT, source_path TEXT, present INTEGER, source_sha256 TEXT,
                imported_rows INTEGER, coverage_note TEXT
            );
            INSERT INTO participant_inventory_sources VALUES (
                'public_non_dicom_metadata', '/tmp/public.sqlite', 1, 'abc', 1,
                'Participant-linked rows imported.'
            );
            """
        )


def create_public_non_dicom_db(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE artifact_meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO artifact_meta VALUES ('schema_version', '7');
            CREATE TABLE agent_public_non_dicom_dataset_summary (short_title TEXT);
            INSERT INTO agent_public_non_dicom_dataset_summary VALUES ('TCGA-BRCA');
            CREATE TABLE agent_public_non_dicom_assets (
                asset_id TEXT, dataset_type TEXT, short_title TEXT, subject_id TEXT,
                asset_name TEXT, asset_granularity TEXT, download_id TEXT,
                file_name TEXT, package_path TEXT, file_format TEXT, media_kind TEXT,
                imaging_domain TEXT, modality TEXT, object_role TEXT, source_system TEXT,
                source_url TEXT, participant_link_count INTEGER, location_count INTEGER,
                managed_systems TEXT
            );
            INSERT INTO agent_public_non_dicom_assets VALUES (
                'pnd-1', 'Collection', 'TCGA-BRCA', 'BRCA-1', 'volume', 'file',
                '7', 'image.nii.gz', 'BRCA-1/image.nii.gz', 'NIfTI', 'volume',
                'radiology', 'MR', 'source_image', 'tcia_aspera',
                'https://example.org/package', 1, 1, 'tcia_aspera'
            );
            CREATE TABLE agent_public_non_dicom_asset_participants (
                asset_id TEXT, subject_id TEXT
            );
            INSERT INTO agent_public_non_dicom_asset_participants VALUES ('pnd-1', 'BRCA-1');
            CREATE TABLE agent_public_non_dicom_review_issues (issue_id TEXT);
            """
        )


class TciaQueryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.snapshot = root / "tcia_snapshot.sqlite"
        self.controlled = root / "controlled_access_metadata.sqlite"
        self.nifti = root / "nifti_metadata.sqlite"
        self.pathology = root / "pathology_metadata.sqlite"
        self.clinical = root / "clinical_metadata.sqlite"
        self.participants = root / "participant_inventory.sqlite"
        self.public_non_dicom = root / "public_non_dicom_metadata.sqlite"
        self.bundle_manifest = root / "tcia_metadata_v2_bundle_manifest.json"
        self.bundle_install_state = root / "tcia_metadata_v2_install.json"
        create_base_snapshot(self.snapshot)
        create_controlled_db(self.controlled)
        create_nifti_db(self.nifti)
        create_pathology_db(self.pathology)
        create_clinical_db(self.clinical)
        create_participant_db(self.participants)
        create_public_non_dicom_db(self.public_non_dicom)
        self.bundle_manifest.write_text(json.dumps({
            "artifact": "tcia_metadata_v2_bundle",
            "schema_version": 2,
            "release_channel": "stable",
            "release_tag": "tcia-metadata-v2-latest",
            "release_fingerprint": "test-fingerprint",
            "producer": {"commit": "test"},
            "components": {},
            "profiles": {
                "research_detail": {
                    "assets": [
                        "tcia_snapshot.sqlite.gz",
                        "participant_inventory.sqlite.gz",
                        "public_non_dicom_metadata.sqlite.gz",
                        "controlled_access_metadata.sqlite.gz",
                        "clinical_metadata.sqlite.gz",
                    ]
                }
            },
        }))
        self.bundle_install_state.write_text(json.dumps({
            "installed_profile": "research_detail",
            "installed_assets": [
                "tcia_snapshot.sqlite.gz",
                "participant_inventory.sqlite.gz",
                "public_non_dicom_metadata.sqlite.gz",
                "controlled_access_metadata.sqlite.gz",
                "clinical_metadata.sqlite.gz",
            ],
        }))
        self.service = TciaQueryService(
            snapshot_db=self.snapshot,
            controlled_db=self.controlled,
            nifti_db=self.nifti,
            pathology_db=self.pathology,
            clinical_db=self.clinical,
            participant_db=self.participants,
            public_non_dicom_db=self.public_non_dicom,
            bundle_manifest=self.bundle_manifest,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_v2_configuration_does_not_fall_back_to_legacy_cache_databases(self) -> None:
        root = Path(self.tmp.name)
        skill_root = root / "skill"
        cache = skill_root / "cache"
        cache.mkdir(parents=True)
        (cache / "nifti_metadata.sqlite").touch()
        (cache / "pathology_metadata.sqlite").touch()
        v2_root = root / "v2"
        v2_root.mkdir()

        with patch.dict(
            os.environ,
            {
                "TCIA_V2_INSTALL_DIR": str(v2_root),
                "TCIA_NIFTI_METADATA_DB": "",
                "TCIA_PATHOLOGY_METADATA_DB": "",
            },
        ):
            service = TciaQueryService(skill_root=skill_root)

        self.assertEqual(
            service.nifti_db,
            v2_root / ".legacy-not-configured" / "nifti_metadata.sqlite",
        )
        self.assertEqual(
            service.pathology_db,
            v2_root / ".legacy-not-configured" / "pathology_metadata.sqlite",
        )
        self.assertFalse(service.nifti_db.exists())
        self.assertFalse(service.pathology_db.exists())

    def test_snapshot_info_reports_sidecars_and_new_capabilities(self) -> None:
        info = self.service.snapshot_info()
        self.assertTrue(info["snapshot_exists"])
        self.assertTrue(info["controlled_access_db_exists"])
        self.assertTrue(info["nifti_metadata_db_exists"])
        self.assertTrue(info["pathology_metadata_db_exists"])
        self.assertTrue(info["clinical_metadata_db_exists"])
        self.assertTrue(info["participant_inventory_db_exists"])
        self.assertTrue(info["public_non_dicom_metadata_db_exists"])
        self.assertEqual(info["v2_bundle"]["release_channel"], "stable")
        self.assertEqual(info["participant_counts"]["participants"], 1)
        self.assertEqual(info["clinical_counts"]["image_linked_subjects"], 1)
        self.assertTrue(info["capabilities"]["release_history"])
        self.assertTrue(info["capabilities"]["external_resource_labels"])

    def test_bundle_info_uses_manifest_without_sqlite_recounts(self) -> None:
        for name in (
            "_connect_snapshot",
            "_connect_controlled",
            "_connect_nifti",
            "_connect_pathology",
            "_connect_clinical",
            "_connect_participants",
            "_connect_public_non_dicom",
        ):
            setattr(self.service, name, lambda: self.fail("bundle_info opened SQLite"))
        info = self.service.bundle_info()
        self.assertEqual(info["v2_bundle"]["release_fingerprint"], "test-fingerprint")
        self.assertTrue(info["v2_capabilities"]["participant_search"])
        self.assertTrue(info["v2_capabilities"]["public_non_dicom_detail"])
        self.assertNotIn("participant_counts", info)

    def test_streamlined_defaults_ignore_legacy_files_in_v2_directory(self) -> None:
        root = Path(self.tmp.name) / "isolated-skill"
        v2_root = root / "cache" / "tcia-metadata-v2-latest"
        v2_root.mkdir(parents=True)
        (v2_root / "nifti_metadata.sqlite").touch()
        (v2_root / "pathology_metadata.sqlite").touch()
        with patch.dict(os.environ, {"TCIA_V2_INSTALL_DIR": str(v2_root)}, clear=False):
            service = TciaQueryService(skill_root=root)
        self.assertEqual(
            service.nifti_db,
            v2_root / ".legacy-not-configured" / "nifti_metadata.sqlite",
        )
        self.assertEqual(
            service.pathology_db,
            v2_root / ".legacy-not-configured" / "pathology_metadata.sqlite",
        )

    def test_search_datasets_filters_external_clinical_resource(self) -> None:
        result = self.service.search_datasets(
            external_resources=["Clinical"],
            has_external_clinical_resource=True,
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["datasets"][0]["short_title"], "TCGA-BRCA")
        self.assertEqual(result["datasets"][0]["external_resource_labels"], ["Clinical", "Genomics"])

    def test_get_dataset_includes_downloads_and_related_fields(self) -> None:
        result = self.service.get_dataset("TCGA-BRCA")
        self.assertEqual(result["datasets"][0]["current_version_number"], "2")
        self.assertEqual(result["current_downloads"][0]["data_types"], ["MR", "SEG"])

    def test_release_history_tools(self) -> None:
        versions = self.service.get_dataset_versions("TCGA-BRCA")
        self.assertTrue(versions["available"])
        self.assertEqual(versions["versions"][0]["version_number"], "1")
        releases = self.service.get_dataset_v1_releases(released_since="2025-01-01")
        self.assertEqual(releases["v1_releases"][0]["v1_release_date"], "2025-05-01")

    def test_controlled_access_sidecar_query(self) -> None:
        result = self.service.get_controlled_access_files(
            "AAPM-RT-MAC",
            modalities=["RTSTRUCT"],
            has_drs_uri=True,
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["files"][0]["drs_uri"], "drs://nci-crdc.datacommons.io/file-1")

    def test_nifti_sidecar_queries(self) -> None:
        datasets = self.service.find_nifti_datasets(modalities=["MR"])
        self.assertEqual(datasets["count"], 1)
        files = self.service.get_nifti_files("BCBM-RadioGenomics", modalities=["MR"])
        self.assertEqual(files["files"][0]["series_instance_uid"], "3.4")
        derived = self.service.get_nifti_derived_objects("BCBM-RadioGenomics", linked_only=True)
        self.assertEqual(derived["count"], 1)
        self.assertEqual(
            derived["derived_objects"][0]["source_nifti_volume_file_name"],
            "image.nii.gz",
        )
        characteristics = self.service.get_nifti_characteristics(
            "BCBM-RadioGenomics",
            object_roles=["segmentation"],
            associated_imaging_modalities=["MR"],
            has_source_reference=True,
        )
        self.assertEqual(characteristics["count"], 1)
        self.assertEqual(characteristics["characteristics"][0]["source_access_level"], "open")
        review = self.service.find_nifti_review_issues(short_titles=["BCBM-RadioGenomics"])
        self.assertEqual(review["count"], 1)
        self.assertEqual(review["review_issues"][0]["status"], "manual_review")
        resolved = self.service.find_nifti_review_issues(statuses=["resolved"])
        self.assertEqual(resolved["count"], 1)
        package_files = self.service.get_nifti_package_files("BCBM-RadioGenomics", file_exts=["gz"])
        self.assertEqual(package_files["count"], 1)

    def test_pathology_sidecar_queries(self) -> None:
        datasets = self.service.find_pathology_datasets(has_pathdb=True)
        self.assertEqual(datasets["count"], 1)
        downloads = self.service.get_pathology_downloads(short_titles=["CPTAC-STAD"])
        self.assertEqual(downloads["count"], 1)
        package_files = self.service.get_pathology_package_files("CPTAC-STAD", file_exts=["svs"])
        self.assertEqual(package_files["count"], 1)
        files = self.service.get_pathology_file_objects("CPTAC-STAD", file_roles=["wsi"])
        self.assertEqual(files["count"], 1)
        disparities = self.service.get_pathology_disparities(short_titles=["CPTAC-STAD"])
        self.assertEqual(disparities["count"], 1)

    def test_clinical_sidecar_queries(self) -> None:
        datasets = self.service.find_clinical_datasets(
            source_kinds=["tcia_clinical_download"],
            concepts=["response"],
            has_conflicts=True,
            has_clinical_only_subjects=True,
        )
        self.assertEqual(datasets["count"], 1)
        self.assertEqual(datasets["datasets"][0]["concepts"], 2)

        subjects = self.service.get_clinical_subjects("TCGA-BRCA", subject_ids=["BRCA-1"])
        self.assertEqual(subjects["count"], 1)
        self.assertTrue(subjects["subjects"][0]["has_imaging"])
        self.assertEqual(subjects["subjects"][0]["resolved_values"]["response"], "pCR")

        all_subjects = self.service.get_clinical_subjects(
            "TCGA-BRCA", include_clinical_only=True
        )
        self.assertEqual(all_subjects["count"], 2)

        facts = self.service.get_clinical_facts(
            "TCGA-BRCA", subject_id="BRCA-1", concepts=["response"]
        )
        self.assertEqual(facts["count"], 2)
        self.assertEqual(facts["facts"][0]["source_priority"], 400)
        self.assertEqual(facts["facts"][0]["provenance"], {"row_number": 2})

        conflicts = self.service.get_clinical_conflicts(
            "TCGA-BRCA", subject_id="BRCA-1"
        )
        self.assertEqual(conflicts["count"], 1)
        self.assertEqual(conflicts["conflicts"][0]["values_seen"], ["Yes", "pCR"])

    def test_v2_participant_queries_preserve_identity_and_coverage(self) -> None:
        participants = self.service.search_participants(
            query="brca-1", modalities=["MR"], access_levels=["open"]
        )
        self.assertEqual(participants["count"], 1)
        self.assertEqual(participants["participants"][0]["source_namespaces"], [
            "tcia_subject_id", "idc_patient_id"
        ])
        ct_participants = self.service.search_participants(
            query="brca-1", modalities=["CT"], access_levels=["open"]
        )
        self.assertEqual(ct_participants["count"], 1)
        detail = self.service.get_participant(
            short_title="TCGA-BRCA", participant_id="brca-1"
        )
        self.assertEqual(detail["participant"]["participant_key"], "participant-1")
        assets = self.service.get_participant_assets(
            "participant-1", data_domains=["radiology"]
        )
        self.assertEqual(assets["assets"][0]["file_format"], "DICOM")
        coverage = self.service.get_dataset_participant_coverage("TCGA-BRCA")
        self.assertEqual(coverage["participant_count"], 2)
        self.assertFalse(coverage["coverage_complete"])
        self.assertEqual(len(coverage["unlinked_dataset_assets"]), 1)
        issues = self.service.find_participant_link_issues(statuses=["review_required"])
        self.assertEqual(issues["count"], 1)

    def test_compatibility_participant_queries_use_normalized_modalities(self) -> None:
        with connect(self.participants) as conn:
            conn.execute(
                "UPDATE agent_participant_search SET modalities = 'CT;SEG;SR' "
                "WHERE participant_key = 'participant-1'"
            )
            conn.execute("DROP TABLE participant_assets")
            conn.execute("DROP TABLE participants")

        participants = self.service.search_participants(
            query="brca-1", modalities=["CT"], access_levels=["open"]
        )
        self.assertEqual(participants["count"], 1)
        coverage = self.service.get_dataset_participant_coverage("TCGA-BRCA")
        self.assertEqual(coverage["participant_count"], 1)

    def test_v2_public_non_dicom_query(self) -> None:
        result = self.service.find_public_non_dicom_assets(
            short_titles=["TCGA-BRCA"], participant_id="brca-1", file_formats=["NIfTI"]
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["assets"][0]["file_name"], "image.nii.gz")
        self.assertEqual(result["public_dicom_detail_route"], "IDC/idc-index")


if __name__ == "__main__":
    unittest.main()
