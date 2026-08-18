import importlib.util
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


model = load("tcia_artifact_model")
public = load("tcia_public_non_dicom_metadata")
participants = load("tcia_participant_inventory")
crosswalks = load("tcia_public_non_dicom_crosswalks")
discovery = load("tcia_public_non_dicom_crosswalk_discovery")


class VocabularyTests(unittest.TestCase):
    def test_crosswalk_path_matcher_is_delimiter_aware(self):
        index = discovery.build_candidate_index({"AQK", "C3N-00123", "13952", "12"})
        self.assertEqual(
            discovery.match_path("data/CBFB_MYH11/AQK/image_0.tif", index),
            [("AQK", "exact_path_component")],
        )
        self.assertIn(
            ("C3N-00123", "delimiter_bounded_path_token"),
            discovery.match_path("slides/C3N-00123-01.svs", index),
        )
        self.assertEqual(
            discovery.match_path("patches/13952/44009.png", index),
            [("13952", "exact_path_component")],
        )
        self.assertNotIn(
            ("12", "delimiter_bounded_path_token"),
            discovery.match_path("patches/12/44009.png", index),
        )

    def test_managed_system_routing(self):
        self.assertEqual(
            model.managed_system_for_url("https://faspex.cancerimagingarchive.net/?context=x"),
            "tcia_aspera",
        )
        self.assertEqual(
            model.managed_system_for_url("https://pathdb.cancerimagingarchive.net/slide/1"),
            "tcia_pathdb",
        )
        self.assertEqual(
            model.managed_system_for_url("https://example.s3.amazonaws.com/key"),
            "aws_open_data",
        )
        self.assertEqual(model.managed_system_for_url("", route_system="ctdc"), "crdc_ctdc")
        self.assertEqual(
            model.managed_system_for_url("", route_system="general_commons"), "crdc_gc"
        )

    def test_media_and_domain_are_independent_from_format(self):
        self.assertEqual(model.media_kind("MHA", ["CT"]), "image_volume")
        self.assertEqual(model.media_kind("PNG", ["US", "Segmentation"]), "still_image")
        self.assertEqual(model.media_kind("MPG", ["Capsule Endoscopy"]), "video")
        self.assertEqual(model.imaging_domain(["Other"], ["Capsule Endoscopy"]), "endoscopy")
        self.assertEqual(model.imaging_domain(["Radiology Images"], ["CT"]), "radiology")

    def test_original_is_not_assigned_to_unknown_wordpress_attachment(self):
        self.assertEqual(model.default_representation_class("tcia_aspera"), "submitted_original")
        self.assertEqual(model.default_representation_class("tcia_wordpress"), "unknown")


class BuilderTests(unittest.TestCase):
    def test_yale_workbook_enriches_matching_nifti_asset(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            public_db = base / "public.sqlite"
            clinical_db = base / "clinical.sqlite"
            conn = public.connect(public_db)
            conn.executescript(public.SCHEMA)
            public.insert_vocab(conn)
            public.insert_asset(
                conn,
                {
                    "asset_id": "asset-yale-1",
                    "dataset_type": "Collection",
                    "short_title": "Yale-Brain-Mets-Longitudinal",
                    "subject_id": "YG_TEST",
                    "subject_id_namespace": "tcia_dataset:Yale-Brain-Mets-Longitudinal",
                    "participant_link_status": "dataset_scoped_source_identifier",
                    "asset_granularity": "file",
                    "asset_name": "YG_TEST_2020-01-02_03-04-05_FLAIR.nii.gz",
                    "file_name": "YG_TEST_2020-01-02_03-04-05_FLAIR.nii.gz",
                    "package_path": "Yale/YG_TEST/YG_TEST_2020-01-02_03-04-05_FLAIR.nii.gz",
                    "file_format": "NIFTI",
                    "media_kind": "image_volume",
                    "spatial_dimensionality": "3D",
                    "temporal_dimensionality": "static",
                    "imaging_domain": "radiology",
                    "modality": "MR",
                    "object_role": "source_image",
                    "representation_provenance_class": "submitted_original",
                    "source_system": "tcia_aspera",
                    "raw_values_json": "{}",
                    "provenance_json": "{}",
                    "quality_flag_json": "{}",
                },
            )
            conn.commit()
            conn.close()

            source = sqlite3.connect(clinical_db)
            source.executescript(
                """
                CREATE TABLE clinical_sources (
                    source_id TEXT, source_kind TEXT, source_priority INTEGER,
                    short_title TEXT, source_url TEXT, artifact_sha256 TEXT
                );
                CREATE TABLE clinical_rows (
                    source_row_id TEXT, source_id TEXT, subject_id TEXT,
                    table_name TEXT, row_json TEXT
                );
                """
            )
            source.execute(
                "INSERT INTO clinical_sources VALUES (?,?,?,?,?,?)",
                (
                    "official:yale",
                    "tcia_clinical_download",
                    400,
                    "Yale-Brain-Mets-Longitudinal",
                    "https://example.test/yale.xlsx",
                    "abc123",
                ),
            )
            source.executemany(
                "INSERT INTO clinical_rows VALUES (?,?,?,?,?)",
                [
                    (
                        "acq-row",
                        "official:yale",
                        "YG_TEST",
                        "yale.xlsx::Acquisition_data",
                        json.dumps(
                            {
                                "patient_id": "YG_TEST",
                                "study_datetime": "2020-01-02_03-04-05",
                                "vendor": "SIEMENS",
                                "model": "Verio",
                                "field_strength (T)": "3",
                                "2D_3D_acquisition": "2D",
                                "scanner_site": "Yale",
                                "pre_included (1=present; 0=absent)": "1",
                                "post_included (1=present; 0=absent)": "1",
                                "t2_included (1=present; 0=absent)": "0",
                                "flair_included (1=present; 0=absent)": "1",
                            }
                        ),
                    ),
                    (
                        "image-row",
                        "official:yale",
                        "YG_TEST",
                        "yale.xlsx::image_acquisition_parameters",
                        json.dumps(
                            {
                                "file_name": "YG_TEST_2020-01-02_03-04-05_FLAIR.nii.gz",
                                "study_datetime": "2020-01-02_03-04-05",
                                "sequence_class": "FLAIR",
                                "sequence_tags": "ax_flair_blade",
                                "slice_thickness (mm)": "5",
                                "spacing_between_slices (mm)": "5",
                                "repetition_time (ms)": "9000",
                                "echo_time (ms)": "94",
                                "inversion_time (ms)": "2500",
                            }
                        ),
                    ),
                ],
            )
            source.commit()
            source.close()

            conn = public.connect(public_db)
            counts = public.ingest_yale_brain_mets_workbook_metadata(
                conn, clinical_db
            )
            public.build_metadata_field_coverage(conn)
            row = conn.execute(
                """SELECT manufacturer, manufacturer_model_name,
                          magnetic_field_strength_t, sequence_class,
                          slice_thickness_mm, metadata_json,
                          field_provenance_json
                   FROM agent_public_non_dicom_image_metadata
                   WHERE asset_id = 'asset-yale-1'"""
            ).fetchone()
            self.assertEqual(row[:5], ("SIEMENS", "Verio", 3, "FLAIR", 5))
            metadata = json.loads(row[5])
            self.assertEqual(metadata["sequence_class"], "FLAIR")
            self.assertEqual(metadata["slice_thickness_mm"], 5)
            self.assertEqual(metadata["sequences_present"], ["PRE", "POST", "FLAIR"])
            provenance = json.loads(row[6])
            self.assertEqual(
                provenance["manufacturer"]["source_kind"],
                "tcia_clinical_download",
            )
            self.assertEqual(counts["matched_image_rows"], 1)
            self.assertEqual(counts["unmatched_image_rows"], 0)
            coverage = conn.execute(
                """SELECT populated_assets, normalized_assets
                   FROM agent_public_non_dicom_metadata_field_coverage
                   WHERE short_title = 'Yale-Brain-Mets-Longitudinal'
                     AND field_name = 'sequence_class'"""
            ).fetchone()
            self.assertEqual(tuple(coverage), (1, 1))
            conn.close()

    def test_reviewed_crosswalk_enriches_existing_aspera_and_pathdb_assets(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db = base / "public.sqlite"
            crosswalk_path = base / "crosswalk.csv"
            curation_path = base / "curation.json"
            package_path = "H&E/AUR-AD9G-TTP1-B-1-0-S1.example.svs"
            conn = public.connect(db)
            conn.executescript(public.SCHEMA)
            public.insert_vocab(conn)

            def asset(asset_id, granularity, system, subject_id="", source_url=""):
                public.insert_asset(conn, {
                    "asset_id": asset_id,
                    "dataset_type": "Collection",
                    "short_title": "AURORA-Metastatic-Breast-Multiomics",
                    "download_row_id": 1,
                    "download_id": "52487" if system == "tcia_aspera" else "",
                    "subject_id": subject_id,
                    "subject_id_namespace": "tcia_dataset:AURORA-Metastatic-Breast-Multiomics",
                    "participant_link_status": "unavailable" if not subject_id else "dataset_scoped_source_identifier",
                    "asset_granularity": granularity,
                    "asset_name": Path(package_path).name,
                    "file_name": Path(package_path).name,
                    "package_path": (
                        package_path
                        if system == "tcia_aspera" and granularity == "file"
                        else ""
                    ),
                    "file_format": "SVS",
                    "container_format": "",
                    "media_kind": "whole_slide_image",
                    "spatial_dimensionality": "2D",
                    "temporal_dimensionality": "static",
                    "imaging_domain": "pathology",
                    "modality": "SM",
                    "object_role": "whole_slide_image",
                    "representation_provenance_class": "unknown",
                    "source_system": system,
                    "source_record_id": asset_id,
                    "source_url": source_url,
                    "raw_values_json": json.dumps({"raw_patient_id": subject_id}),
                    "provenance_json": json.dumps({"original": system}),
                    "quality_flag_json": "{}",
                })

            asset("download", "download", "tcia_aspera")
            asset("package", "file", "tcia_aspera")
            asset(
                "pathdb", "file", "tcia_pathdb", "AUR-AD9G",
                f"https://pathdb.example/{package_path}",
            )
            public.insert_asset_participant(
                conn,
                asset_id="pathdb",
                short_title="AURORA-Metastatic-Breast-Multiomics",
                subject_id="AUR-AD9G",
                namespace="tcia_dataset:AURORA-Metastatic-Breast-Multiomics",
                raw_subject_id="AUR-AD9G",
                participant_role="depicted_subject",
                link_status="dataset_scoped_source_identifier",
            )
            conn.commit()

            crosswalk_row = crosswalks.row(
                dataset_type="Collection",
                short_title="AURORA-Metastatic-Breast-Multiomics",
                download_id="52487",
                subject_id="AD9G",
                raw_subject_id="AUR-AD9G",
                subject_id_namespace="tcia_dataset:AURORA-Metastatic-Breast-Multiomics",
                participant_link_status="reviewed_source_crosswalk",
                package_path=package_path,
                file_name=Path(package_path).name,
                file_format="SVS",
                media_kind="whole_slide_image",
                imaging_domain="pathology",
                modality="SM",
                object_role="whole_slide_image",
                source_system="tcia_aspera",
                crosswalk_source_url="https://example.test/usage-notes",
                crosswalk_method="published_filename_schema_patient_identifier",
                crosswalk_confidence="high",
                reviewer_note="reviewed",
            )
            with crosswalk_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=crosswalks.FIELDS)
                writer.writeheader()
                writer.writerow(crosswalk_row)
            curation_path.write_text(json.dumps({
                "reviewed_at": "2026-08-18",
                "review_source": "unit test",
                "decisions": [{
                    "dataset_type": "Collection",
                    "short_title": "AURORA-Metastatic-Breast-Multiomics",
                    "download_ids": ["52487"],
                    "decision_status": "resolved",
                    "resolution_type": "participant_crosswalk",
                    "reviewer_note": "reviewed",
                    "evidence_url": "https://example.test/usage-notes",
                }],
            }))
            result = public.ingest_reviewed_crosswalks(conn, crosswalk_path, curation_path)
            conn.commit()

            self.assertEqual(result["file_assets"], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM public_non_dicom_assets").fetchone()[0],
                3,
            )
            linked = conn.execute(
                "SELECT asset_id, subject_id, participant_link_status "
                "FROM public_non_dicom_assets WHERE asset_id IN ('package', 'pathdb') "
                "ORDER BY asset_id"
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in linked],
                [
                    ("package", "AD9G", "reviewed_source_crosswalk"),
                    ("pathdb", "AD9G", "reviewed_source_crosswalk"),
                ],
            )
            participant_links = conn.execute(
                    "SELECT subject_id FROM public_non_dicom_asset_participants "
                    "WHERE asset_id IN ('package', 'pathdb') ORDER BY asset_id"
                ).fetchall()
            self.assertEqual(
                [tuple(row) for row in participant_links],
                [("AD9G",), ("AD9G",)],
            )
            conn.close()

    def test_aurora_crosswalk_uses_usage_notes_and_hla_linkage(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            listing = ["package_path,file_name,file_ext,size_bytes"]
            for index in range(225):
                patient = f"{index % 54:04X}"
                uuid = f"{index:08X}-1111-1111-1111-111111111111"
                name = f"AUR-{patient}-TTP1-B-1-0-S1.{uuid}.svs"
                listing.append(f"H&E/{name},{name},.svs,10")
            linkage = ["Patient ID,BCR Sample barcode,Sample_Identifier,Slide ID"]
            for index in range(64):
                token = f"{120000 + index}-3"
                patient = f"{index % 54:04X}"
                name = f"JMB_HLAA_CK_HLADR_{token}-Image Export-{index:02d}_c1-3.tif"
                listing.append(f"HLA/{name},{name},.tif,20")
                linkage.append(
                    f"AUR-{patient},AUR-{patient}-TTM1-A,AUR_01_01_01,HLAA_CK_{token}"
                )
            (source / "aurora_pathology_listing.csv").write_text("\n".join(listing) + "\n")
            (source / "aurora_hla_linkage.csv").write_text("\n".join(linkage) + "\n")
            decision = {
                "evidence_url": "https://example.test/usage-notes",
                "supporting_evidence_url": "https://example.test/clinical.xlsx",
                "reviewer_note": "reviewed",
            }
            rows = crosswalks.aurora_rows(source, decision)
            self.assertEqual(len(rows), 289)
            self.assertEqual(len({item["subject_id"] for item in rows}), 54)
            self.assertEqual(rows[0]["subject_id"], "0000")
            self.assertEqual(rows[0]["raw_subject_id"], "AUR-0000")
            self.assertEqual(
                json.loads(rows[0]["raw_values_json"])["sample_short_code"],
                "TTP",
            )
            self.assertEqual(rows[-1]["file_format"], "TIFF")
            self.assertEqual(
                rows[-1]["crosswalk_method"],
                "clinical_workbook_slide_id_to_patient_id",
            )

    def build_snapshot(self, path: Path):
        import sqlite3

        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE agent_current_downloads (
              download_row_id INTEGER, dataset_type TEXT, short_title TEXT,
              download_id TEXT, download_title TEXT, title TEXT,
              download_url TEXT, download_size TEXT, download_size_unit TEXT,
              download_types TEXT, data_types TEXT, file_types TEXT,
              hidden INTEGER, controlled_access INTEGER
            );
            CREATE TABLE agent_pathdb_slides (
              collection TEXT, patient_id TEXT, slide_id TEXT, camic_id TEXT,
              camicroscope_url TEXT, wsiimage_url TEXT, data_format TEXT,
              modality TEXT, protocol TEXT, magnification TEXT
            );
            CREATE TABLE agent_datasets (dataset_type TEXT, short_title TEXT);
            """
        )
        rows = [
            (1, "Collection", "Pedi-Cranial-CT-Healthy", "55262", "Images", "Pedi CT",
             "https://faspex.cancerimagingarchive.net/?context=x", "3.2", "gb",
             '["Radiology Images"]', '["CT"]', '["MHA"]', 0, 0),
            (2, "Collection", "Breast-Lesions-USG", "2", "Images and masks", "US",
             "https://www.cancerimagingarchive.net/file.zip", "1", "gb",
             '["Radiology Images","Image Annotations"]', '["US","Segmentation"]',
             '["PNG","ZIP"]', 0, 0),
            (3, "Collection", "Capsule-Endoscopy-SB-NET", "3", "Excerpts", "Capsule",
             "https://faspex.cancerimagingarchive.net/?context=y", "1", "gb",
             '["Other"]', '["Capsule Endoscopy"]', '["JPG","MPG"]', 0, 0),
            (4, "Collection", "Public-DICOM", "4", "Images", "DICOM",
             "https://example.org/manifest.tcia", "1", "gb",
             '["Radiology Images"]', '["CT"]', '["DICOM"]', 0, 0),
        ]
        conn.executemany("INSERT INTO agent_current_downloads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany(
            "INSERT INTO agent_datasets VALUES (?, ?)",
            [(row[1], row[2]) for row in rows],
        )
        conn.commit()
        conn.close()

    def test_public_builder_represents_mha_png_video_and_excludes_dicom(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot = base / "snapshot.sqlite"
            output = base / "public.sqlite"
            self.build_snapshot(snapshot)
            result = public.build_database(
                snapshot,
                output,
                nifti_db=None,
                pathology_db=None,
                include_pathdb_files=False,
                replace=True,
            )
            self.assertEqual(result["counts"]["wordpress_download_assets"], 4)
            conn = sqlite3.connect(output)
            formats = {row[0] for row in conn.execute("SELECT file_format FROM public_non_dicom_assets")}
            self.assertEqual(formats, {"MHA", "PNG", "JPG", "MPG"})
            capsule = conn.execute(
                "SELECT media_kind, imaging_domain FROM public_non_dicom_assets WHERE file_format='MPG'"
            ).fetchone()
            self.assertEqual(capsule, ("video", "endoscopy"))
            issue = conn.execute(
                "SELECT description, evidence_json FROM public_non_dicom_review_issues "
                "WHERE short_title='Pedi-Cranial-CT-Healthy'"
            ).fetchone()
            self.assertIn("One or more", issue[0])
            evidence = __import__("json").loads(issue[1])
            self.assertEqual(evidence["unlinked_download_assets"], 1)
            self.assertEqual(evidence["linked_file_assets_same_dataset"], 0)
            self.assertEqual(
                evidence["coverage_context"], "no_linked_file_coverage_observed"
            )
            conn.close()

    def test_participant_keys_remain_dataset_scoped(self):
        self.assertNotEqual(
            participants.participant_key("Collection", "Dataset-A", "001"),
            participants.participant_key("Collection", "Dataset-B", "001"),
        )

    def test_brats_aspera_dicom_exception_recovers_dicom_only_participants(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot = base / "snapshot.sqlite"
            nifti = base / "nifti.sqlite"
            public_db = base / "public.sqlite"
            participant_db = base / "participants.sqlite"
            self.build_snapshot(snapshot)

            conn = sqlite3.connect(snapshot)
            conn.execute(
                "INSERT INTO agent_current_downloads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    5, "Analysis Result", "RSNA-ASNR-MICCAI-BraTS-2021", "46595",
                    "Challenge data both tasks", "BraTS 2021", "https://faspex.example/package",
                    "1", "tb", '["Radiology Images"]', '["MR"]',
                    '["DICOM","NIfTI"]', 0, 0,
                ),
            )
            conn.execute(
                "INSERT INTO agent_datasets VALUES (?, ?)",
                ("Analysis Result", "RSNA-ASNR-MICCAI-BraTS-2021"),
            )
            conn.commit()
            conn.close()

            conn = sqlite3.connect(nifti)
            conn.executescript(
                """
                CREATE TABLE agent_nifti_files (short_title TEXT, package_path TEXT);
                CREATE TABLE agent_nifti_downloads (
                  short_title TEXT, download_id TEXT, download_url TEXT
                );
                CREATE TABLE aspera_root_sums_inventory (
                  dataset_type TEXT, short_title TEXT, download_id TEXT,
                  package_path TEXT, file_ext TEXT, line_number INTEGER
                );
                """
            )
            conn.execute(
                "INSERT INTO agent_nifti_downloads VALUES (?,?,?)",
                (
                    "RSNA-ASNR-MICCAI-BraTS-2021", "46595",
                    "https://faspex.example/package",
                ),
            )
            paths = [
                "RSNA-ASNR-MICCAI-BraTS-2021/BraTS2021_TrainingSet_dcm/"
                "new-not-previously-in-TCIA/00794/T2w/Image-1.dcm",
                "RSNA-ASNR-MICCAI-BraTS-2021/BraTS2021_TrainingSet_dcm/"
                "new-not-previously-in-TCIA/00794/T2w/Image-2.dcm",
                "RSNA-ASNR-MICCAI-BraTS-2021/BraTS2021_TrainingSet_dcm/"
                "new-not-previously-in-TCIA/00794/FLAIR/Image-1.dcm",
                "RSNA-ASNR-MICCAI-BraTS-2021/BraTS2021_ValidationSet_dcm/"
                "new-not-previously-in-TCIA/00393/T1w/Image-1.dcm",
            ]
            conn.executemany(
                "INSERT INTO aspera_root_sums_inventory VALUES (?,?,?,?,?,?)",
                [
                    (
                        "Analysis Result", "RSNA-ASNR-MICCAI-BraTS-2021", "46595",
                        path, "dcm", index,
                    )
                    for index, path in enumerate(paths, 1)
                ],
            )
            conn.commit()
            conn.close()

            result = public.build_database(
                snapshot,
                public_db,
                nifti_db=nifti,
                pathology_db=None,
                include_pathdb_files=False,
                replace=True,
            )
            self.assertEqual(result["counts"]["aspera_public_dicom_exception_assets"], 3)

            conn = sqlite3.connect(public_db)
            rows = conn.execute(
                """
                SELECT subject_id, modality, represented_file_count,
                       representation_provenance_class, source_system
                FROM public_non_dicom_assets
                WHERE file_format='DICOM'
                ORDER BY subject_id, represented_file_count, asset_name
                """
            ).fetchall()
            self.assertEqual(
                rows,
                [
                    ("BraTS2021_00393", "MR", 1, "submitted_original", "tcia_aspera"),
                    ("BraTS2021_00794", "MR", 1, "submitted_original", "tcia_aspera"),
                    ("BraTS2021_00794", "MR", 2, "submitted_original", "tcia_aspera"),
                ],
            )
            conn.close()

            participants.build_database(
                participant_db,
                snapshot_db=snapshot,
                public_db=public_db,
                controlled_db=base / "missing-controlled.sqlite",
                clinical_db=base / "missing-clinical.sqlite",
                replace=True,
            )
            conn = sqlite3.connect(participant_db)
            participant_rows = conn.execute(
                """
                SELECT display_participant_id, has_public_dicom,
                       has_public_non_dicom, file_formats
                FROM agent_participant_search
                WHERE short_title='RSNA-ASNR-MICCAI-BraTS-2021'
                ORDER BY display_participant_id
                """
            ).fetchall()
            self.assertEqual(
                participant_rows,
                [
                    ("BraTS2021_00393", 1, 0, "DICOM"),
                    ("BraTS2021_00794", 1, 0, "DICOM"),
                ],
            )
            file_counts = dict(
                conn.execute(
                    """
                    SELECT p.display_participant_id, a.file_count
                    FROM participant_assets a JOIN participants p USING(participant_key)
                    WHERE p.short_title='RSNA-ASNR-MICCAI-BraTS-2021'
                    """
                )
            )
            self.assertEqual(file_counts, {"BraTS2021_00393": 1, "BraTS2021_00794": 3})
            conn.close()

    def test_participant_inventory_unifies_case_equivalent_same_dataset_identifiers(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot = base / "snapshot.sqlite"
            controlled = base / "controlled.sqlite"
            clinical = base / "clinical.sqlite"
            output = base / "participants.sqlite"

            conn = sqlite3.connect(snapshot)
            conn.execute("CREATE TABLE agent_datasets (dataset_type TEXT, short_title TEXT)")
            conn.executemany(
                "INSERT INTO agent_datasets VALUES (?, ?)",
                [
                    ("Collection", "AAPM-RT-MAC"),
                    ("Analysis Result", "Outcome-Result"),
                ],
            )
            conn.commit()
            conn.close()

            conn = sqlite3.connect(controlled)
            conn.execute(
                """
                CREATE TABLE agent_controlled_files (
                  dataset_type TEXT, short_title TEXT, route_system TEXT,
                  patient_id TEXT, participant_id TEXT, modality TEXT,
                  file_format TEXT, study_instance_uid TEXT,
                  series_instance_uid TEXT, file_size_bytes INTEGER, drs_uri TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO agent_controlled_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "Collection", "AAPM-RT-MAC", "general_commons",
                        "RTMAC-LIVE-008", "", "MR", "DICOM", "study-1",
                        "series-1", 10, "drs://example/1",
                    ),
                    (
                        "Collection", "Outcome-Result", "general_commons",
                        "CASE-001", "", "MR", "DICOM", "study-2",
                        "series-2", 20, "drs://example/2",
                    ),
                ],
            )
            conn.commit()
            conn.close()

            conn = sqlite3.connect(clinical)
            conn.execute(
                "CREATE TABLE agent_clinical_all_subjects "
                "(short_title TEXT, subject_id TEXT, source_kinds TEXT)"
            )
            conn.executemany(
                "INSERT INTO agent_clinical_all_subjects VALUES (?,?,?)",
                [
                    ("AAPM-RT-MAC", "RTMAC-LIVE-008", "tcia,idc"),
                    ("Outcome-Result", "CASE-001", "tcia"),
                ],
            )
            conn.execute(
                "CREATE TABLE clinical_imaging_subjects "
                "(short_title TEXT, subject_id TEXT, imaging_source TEXT)"
            )
            conn.executemany(
                "INSERT INTO clinical_imaging_subjects VALUES (?,?,?)",
                [
                    ("AAPM-RT-MAC", "rtmac-live-008", "idc_index"),
                    ("AAPM-RT-MAC", "IDC-ONLY-009", "idc_index"),
                ],
            )
            conn.commit()
            conn.close()

            result = participants.build_database(
                output,
                snapshot_db=snapshot,
                public_db=base / "missing-public.sqlite",
                controlled_db=controlled,
                clinical_db=clinical,
                replace=True,
            )
            self.assertEqual(result["counts"]["exact_cross_namespace_resolutions"], 1)
            self.assertEqual(result["counts"]["casefolded_identifier_resolutions"], 1)

            conn = sqlite3.connect(output)
            aapm = conn.execute(
                """
                SELECT dataset_type, display_participant_id,
                       within_dataset_identity_status, identity_resolution_method,
                       source_namespace_count, modalities
                FROM agent_participant_search
                WHERE short_title='AAPM-RT-MAC'
                  AND display_participant_id='RTMAC-LIVE-008'
                """
            ).fetchall()
            self.assertEqual(
                aapm,
                [(
                    "Collection", "RTMAC-LIVE-008", "resolved",
                    "casefolded_identifier_same_tcia_dataset", 2, "MR",
                )],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT raw_identifier FROM participant_identifiers "
                    "WHERE participant_key=(SELECT participant_key FROM participants "
                    "WHERE short_title='AAPM-RT-MAC' "
                    "AND display_participant_id='RTMAC-LIVE-008') "
                    "ORDER BY raw_identifier"
                ).fetchall(),
                [("RTMAC-LIVE-008",), ("RTMAC-LIVE-008",), ("rtmac-live-008",)],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM participant_identifiers "
                    "WHERE participant_key=(SELECT participant_key FROM participants "
                    "WHERE short_title='AAPM-RT-MAC' "
                    "AND display_participant_id='RTMAC-LIVE-008')"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM participant_assets "
                    "WHERE participant_key=(SELECT participant_key FROM participants "
                    "WHERE short_title='AAPM-RT-MAC' "
                    "AND display_participant_id='RTMAC-LIVE-008')"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT dataset_type FROM participants "
                    "WHERE short_title='Outcome-Result'"
                ).fetchone()[0],
                "Analysis Result",
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM participant_link_issues").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM participant_identity_evidence "
                    "WHERE resolution_method='exact_identifier_same_tcia_dataset'"
                ).fetchone()[0], 1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM participant_identity_evidence "
                    "WHERE resolution_method='casefolded_identifier_same_tcia_dataset'"
                ).fetchone()[0], 1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT has_clinical FROM agent_participant_search "
                    "WHERE display_participant_id='IDC-ONLY-009'"
                ).fetchone()[0], 0,
            )
            conn.close()

    def test_participant_key_casefolds_within_but_not_across_dataset_types(self):
        collection_upper = participants.participant_key("Collection", "TEST", "Case-001")
        collection_lower = participants.participant_key("Collection", "TEST", "case-001")
        analysis_lower = participants.participant_key("Analysis Result", "TEST", "case-001")
        self.assertEqual(collection_upper, collection_lower)
        self.assertNotEqual(collection_upper, analysis_lower)

    def test_display_identifier_uses_source_precedence_not_ingest_order(self):
        conn = participants.connect(Path(":memory:"))
        conn.executescript(participants.SCHEMA)
        key = participants.ensure_participant(
            conn, "Collection", "TEST", "case-001",
            managed_system="crdc_idc",
            namespace="tcia_dataset:TEST",
            evidence="idc_index_participant_projection",
            provenance={"source_artifact": "clinical_metadata"},
        )
        self.assertEqual(
            participants.ensure_participant(
                conn, "Collection", "TEST", "CASE-001",
                managed_system="tcia_wordpress",
                namespace="tcia_dataset:TEST",
                evidence="clinical_sidecar_dataset_scoped_identifier",
                provenance={"source_artifact": "clinical_metadata"},
            ),
            key,
        )
        participants.select_display_participant_ids(conn)
        self.assertEqual(
            conn.execute(
                "SELECT display_participant_id FROM participants WHERE participant_key=?",
                (key,),
            ).fetchone()[0],
            "CASE-001",
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(DISTINCT raw_identifier) FROM participant_identifiers"
            ).fetchone()[0],
            2,
        )
        conn.close()

    def test_participant_validator_rejects_case_equivalent_canonical_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "participants.sqlite"
            conn = participants.connect(output)
            conn.executescript(participants.SCHEMA)
            conn.executemany(
                "INSERT INTO participants VALUES (?,?,?,?,?,?,?,?)",
                [
                    ("p1", "Collection", "TEST", "Case-001", "dataset_scoped",
                     "single_namespace", "source_identifier", "not_asserted"),
                    ("p2", "Collection", "TEST", "case-001", "dataset_scoped",
                     "single_namespace", "source_identifier", "not_asserted"),
                ],
            )
            conn.commit()
            conn.close()
            result = participants.validate_database(output)
            self.assertFalse(result["ok"])
            self.assertTrue(
                any("case-equivalent canonical participant" in error for error in result["errors"])
            )

    def test_hancock_tma_slide_links_one_asset_to_multiple_participants(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot = base / "snapshot.sqlite"
            output = base / "public.sqlite"
            self.build_snapshot(snapshot)
            conn = sqlite3.connect(snapshot)
            conn.execute(
                "INSERT INTO agent_pathdb_slides VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "HANCOCK", "1, 74, 83", "InvasionFront_HE_block2", "99", "", "",
                    "SVS", "SM", "TMA invasion front", "40x",
                ),
            )
            conn.execute(
                "INSERT INTO agent_pathdb_slides VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "HANCOCK", "1", "PrimaryTumor_HE_001", "100", "", "",
                    "SVS", "SM", "primary tumor", "40x",
                ),
            )
            conn.commit()
            conn.close()
            public.build_database(
                snapshot,
                output,
                nifti_db=None,
                pathology_db=None,
                include_pathdb_files=True,
                replace=True,
            )
            conn = sqlite3.connect(output)
            tma = conn.execute(
                "SELECT asset_id, subject_id, participant_link_status FROM public_non_dicom_assets "
                "WHERE short_title='HANCOCK' AND asset_name='InvasionFront_HE_block2'"
            ).fetchone()
            self.assertEqual(tma[1:], ("", "multi_participant_source_list"))
            links = conn.execute(
                "SELECT subject_id, raw_subject_id, participant_role "
                "FROM public_non_dicom_asset_participants WHERE asset_id=? ORDER BY subject_id",
                (tma[0],),
            ).fetchall()
            self.assertEqual(
                links,
                [
                    ("patient001", "1", "tma_block_member"),
                    ("patient074", "74", "tma_block_member"),
                    ("patient083", "83", "tma_block_member"),
                ],
            )
            wsi = conn.execute(
                "SELECT subject_id FROM public_non_dicom_assets "
                "WHERE short_title='HANCOCK' AND asset_name='PrimaryTumor_HE_001'"
            ).fetchone()
            self.assertEqual(wsi[0], "patient001")
            slide_metadata = conn.execute(
                "SELECT modality, pathology_protocol, magnification, field_provenance_json "
                "FROM agent_public_non_dicom_image_metadata "
                "WHERE short_title='HANCOCK' AND file_name='PrimaryTumor_HE_001'"
            ).fetchone()
            self.assertEqual(slide_metadata[:3], ("SM", "primary tumor", "40x"))
            self.assertEqual(
                json.loads(slide_metadata[3])["magnification"]["source_kind"],
                "pathdb_slide_csv",
            )
            conn.close()

    def test_reviewed_crosswalk_adds_file_evidence_and_resolves_download_flag(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot = base / "snapshot.sqlite"
            output = base / "public.sqlite"
            crosswalk_csv = base / "crosswalk.csv"
            curation = base / "curation.json"
            image_metadata_csv = base / "image_metadata.csv"
            self.build_snapshot(snapshot)
            values = {field: "" for field in crosswalks.FIELDS}
            values.update({
                "dataset_type": "Collection",
                "short_title": "Breast-Lesions-USG",
                "download_id": "2",
                "subject_id": "case001",
                "raw_subject_id": "1",
                "subject_id_namespace": "tcia_dataset:Breast-Lesions-USG",
                "participant_link_status": "reviewed_source_crosswalk",
                "package_path": "images/case001.png",
                "file_name": "case001.png",
                "file_format": "PNG",
                "media_kind": "still_image",
                "imaging_domain": "radiology",
                "modality": "US",
                "object_role": "source_image",
                "source_system": "tcia_wordpress",
                "crosswalk_method": "clinical_filename_exact_match",
                "crosswalk_confidence": "high",
                "raw_values_json": "{}",
                "provenance_json": "{}",
                "quality_flag_json": "{}",
            })
            with crosswalk_csv.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=crosswalks.FIELDS)
                writer.writeheader()
                writer.writerow(values)
            curation.write_text(json.dumps({
                "reviewed_at": "2026-08-13",
                "review_source": "test",
                "decisions": [{
                    "dataset_type": "Collection",
                    "short_title": "Breast-Lesions-USG",
                    "download_ids": ["2"],
                    "decision_status": "resolved",
                    "resolution_type": "participant_crosswalk",
                    "reviewer_note": "test",
                    "evidence_url": "https://example.test/crosswalk.xlsx",
                }],
            }))
            with image_metadata_csv.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "dataset_type", "short_title", "download_id", "subject_id",
                    "file_name", "value_role", "source_kind", "source_url",
                    "source_file", "source_row", "inference_method", "confidence",
                    "priority", "metadata_json",
                ])
                writer.writeheader()
                writer.writerow({
                    "dataset_type": "Collection",
                    "short_title": "Breast-Lesions-USG",
                    "download_id": "2",
                    "subject_id": "case001",
                    "file_name": "case001.png",
                    "value_role": "normalized",
                    "source_kind": "supporting_spreadsheet",
                    "source_url": "https://example.test/metadata.xlsx",
                    "source_file": "metadata.xlsx",
                    "source_row": "2",
                    "inference_method": "image_filename_exact_match",
                    "confidence": "high",
                    "priority": "100",
                    "metadata_json": json.dumps({"pixel_spacing_mm": [0.1, 0.1]}),
                })
            public.build_database(
                snapshot,
                output,
                nifti_db=None,
                pathology_db=None,
                include_pathdb_files=False,
                replace=True,
                crosswalk_csv=crosswalk_csv,
                crosswalk_curation=curation,
                image_metadata_csv=image_metadata_csv,
            )
            conn = sqlite3.connect(output)
            self.assertEqual(
                conn.execute(
                    "SELECT participant_link_status FROM public_non_dicom_assets "
                    "WHERE short_title='Breast-Lesions-USG' AND asset_granularity='download'"
                ).fetchone()[0],
                "crosswalk_available_at_file_grain",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT resolved_subject_id, mapping_method FROM public_non_dicom_crosswalk_evidence"
                ).fetchone(),
                ("case001", "clinical_filename_exact_match"),
            )
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM public_non_dicom_review_issues WHERE short_title='Breast-Lesions-USG'"
            ).fetchone())
            metadata = conn.execute(
                "SELECT pixel_spacing_mm, field_provenance_json "
                "FROM agent_public_non_dicom_image_metadata "
                "WHERE short_title='Breast-Lesions-USG' AND file_name='case001.png'"
            ).fetchone()
            self.assertEqual(json.loads(metadata[0]), [0.1, 0.1])
            self.assertEqual(
                json.loads(metadata[1])["pixel_spacing_mm"]["source_kind"],
                "supporting_spreadsheet",
            )
            coverage = conn.execute(
                "SELECT populated_assets, normalized_assets "
                "FROM agent_public_non_dicom_metadata_field_coverage "
                "WHERE short_title='Breast-Lesions-USG' AND field_name='pixel_spacing_mm'"
            ).fetchone()
            self.assertEqual(coverage, (1, 1))
            conn.close()


if __name__ == "__main__":
    unittest.main()
