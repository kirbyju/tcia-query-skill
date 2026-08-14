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
            participants.participant_key("Collection", "Dataset-A", "tcia_dataset:Dataset-A", "001"),
            participants.participant_key("Collection", "Dataset-B", "tcia_dataset:Dataset-B", "001"),
        )
        self.assertNotEqual(
            participants.participant_key("Collection", "Dataset-A", "tcia_dataset:Dataset-A", "001"),
            participants.participant_key("Collection", "Dataset-A", "crdc_gc:Dataset-A", "001"),
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
            conn.close()

    def test_reviewed_crosswalk_adds_file_evidence_and_resolves_download_flag(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot = base / "snapshot.sqlite"
            output = base / "public.sqlite"
            crosswalk_csv = base / "crosswalk.csv"
            curation = base / "curation.json"
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
            public.build_database(
                snapshot,
                output,
                nifti_db=None,
                pathology_db=None,
                include_pathdb_files=False,
                replace=True,
                crosswalk_csv=crosswalk_csv,
                crosswalk_curation=curation,
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
            conn.close()


if __name__ == "__main__":
    unittest.main()
