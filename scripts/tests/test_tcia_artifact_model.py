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
audit = load("tcia_v2_audit")
crosswalks = load("tcia_public_non_dicom_crosswalks")
discovery = load("tcia_public_non_dicom_crosswalk_discovery")


class VocabularyTests(unittest.TestCase):
    def test_refresh_replaces_wordpress_downloads_routed_through_aspera(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.sqlite"
            conn = public.connect(path)
            conn.executescript(public.SCHEMA)
            public.insert_vocab(conn)
            required = (
                "asset_id,dataset_type,short_title,asset_granularity,file_format,"
                "media_kind,spatial_dimensionality,temporal_dimensionality,"
                "imaging_domain,object_role,representation_provenance_class,"
                "source_system,source_record_id"
            )
            conn.executemany(
                f"INSERT INTO public_non_dicom_assets ({required}) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("aspera_download", "Collection", "A", "download", "NRRD", "image_volume", "3d", "none", "radiology", "segmentation", "submitted_original", "tcia_aspera", "1"),
                    ("wordpress_download", "Collection", "B", "download", "ZIP", "archive", "unknown", "unknown", "other", "package", "unknown", "tcia_wordpress", "2"),
                    ("aspera_file", "Collection", "A", "file", "NRRD", "image_volume", "3d", "none", "radiology", "segmentation", "submitted_original", "tcia_aspera", "3"),
                    ("pathdb_file", "Collection", "C", "file", "SVS", "whole_slide_image", "2d", "none", "pathology", "source_image", "submitted_original", "tcia_pathdb", "4"),
                ],
            )
            self.assertEqual(public.delete_refreshable_assets(conn), 3)
            self.assertEqual(
                conn.execute(
                    "SELECT group_concat(asset_id) FROM public_non_dicom_assets"
                ).fetchone()[0],
                "aspera_file",
            )
            conn.close()

    def test_explicit_reviewed_mapping_generates_reproducible_crosswalk_row(self):
        rows = crosswalks.explicit_mapping_rows({
            "decisions": [{
                "dataset_type": "Collection",
                "short_title": "CPTAC-LUAD",
                "download_ids": ["44839"],
                "reviewer_note": "Exact PathDB path.",
                "explicit_mappings": [{
                    "subject_id": "11LU035",
                    "package_path": "LUAD/example_D1_D1.svs",
                    "crosswalk_source_url": "https://pathdb.example/LUAD/example_D1_D1.svs",
                }],
            }],
        })
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject_id"], "11LU035")
        self.assertEqual(rows[0]["file_format"], "SVS")
        self.assertEqual(rows[0]["crosswalk_method"], "exact_pathdb_source_url_suffix")
        self.assertEqual(
            json.loads(rows[0]["provenance_json"])["evidence_relation"],
            "exact_pathdb_source_url_suffix",
        )

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
        self.assertEqual(model.normalize_format("mat"), "MATLAB")
        self.assertEqual(model.normalize_format(".qptiff"), "TIFF")
        self.assertEqual(model.format_from_path("slide.qptiff"), "TIFF")
        self.assertEqual(model.media_kind("MATLAB", ["Segmentation"]), "image_volume")
        self.assertEqual(model.media_kind("PNG", ["US", "Segmentation"]), "still_image")
        self.assertEqual(model.media_kind("MPG", ["Capsule Endoscopy"]), "video")
        self.assertEqual(model.imaging_domain(["Other"], ["Capsule Endoscopy"]), "endoscopy")
        self.assertEqual(model.imaging_domain(["Radiology Images"], ["CT"]), "radiology")

    def test_cptac_codex_workbook_projection_helpers(self):
        self.assertEqual(
            public.codex_workbook_file_key("WSI_CODEX__A__B.ome.tiff"),
            "wsi_codex__a__b",
        )
        self.assertEqual(
            public.codex_workbook_file_key("WSI_HE__A__B.qptiff"),
            "wsi_he__a__b",
        )
        self.assertEqual(
            public.codex_workbook_participants(
                {
                    "CPTAC-GBM_PatientID": "C3L-01046, C3N-01197, C3L-02041",
                    "UPENN-GBM_PatientID": "",
                }
            ),
            ["C3L-01046", "C3N-01197", "C3L-02041"],
        )
        self.assertEqual(
            public.codex_workbook_participants(
                {
                    "CPTAC-GBM_PatientID": "",
                    "UPENN-GBM_PatientID": "C1230738",
                }
            ),
            ["C1230738"],
        )

    def test_participant_inventory_projects_codex_specimens_to_patients(self):
        import sqlite3

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE clinical_rows(short_title TEXT, source_id TEXT, "
            "subject_id TEXT, row_json TEXT)"
        )
        connection.executemany(
            "INSERT INTO clinical_rows VALUES (?, 'tcia-download:test', ?, ?)",
            [
                (
                    "CPTAC-Glioblastoma-CODEX",
                    "C1230738-TP",
                    json.dumps(
                        {
                            "UPENN-GBM_PatientID": "C1230738",
                            "CPTAC-GBM_PatientID": "",
                        }
                    ),
                ),
                (
                    "CPTAC-Glioblastoma-CODEX",
                    "C3L-01046, C3N-01197, C3L-02041",
                    json.dumps(
                        {
                            "UPENN-GBM_PatientID": "",
                            "CPTAC-GBM_PatientID": (
                                "C3L-01046, C3N-01197, C3L-02041"
                            ),
                        }
                    ),
                ),
            ],
        )
        self.assertEqual(
            participants.cptac_gbm_codex_clinical_projection(connection),
            {
                "C1230738-TP": ["C1230738"],
                "C3L-01046, C3N-01197, C3L-02041": [
                    "C3L-01046",
                    "C3L-02041",
                    "C3N-01197",
                ],
            },
        )

    def test_original_is_not_assigned_to_unknown_wordpress_attachment(self):
        self.assertEqual(model.default_representation_class("tcia_aspera"), "submitted_original")
        self.assertEqual(model.default_representation_class("tcia_wordpress"), "unknown")


class BuilderTests(unittest.TestCase):
    def test_remind_sums_inventory_projects_file_level_crosswalk(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source_db = base / "nifti.sqlite"
            output_db = base / "public.sqlite"
            source = sqlite3.connect(source_db)
            source.executescript(
                """
                CREATE TABLE aspera_root_sums_inventory (
                  dataset_type TEXT, short_title TEXT, download_id TEXT,
                  line_number INTEGER, checksum TEXT, algorithm TEXT,
                  package_path TEXT, file_name TEXT, file_ext TEXT
                );
                CREATE TABLE agent_nifti_downloads (
                  short_title TEXT, download_id TEXT, download_url TEXT
                );
                """
            )
            source.executemany(
                "INSERT INTO aspera_root_sums_inventory VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "Collection", "ReMIND", "43725", 1,
                        public.EMPTY_FILE_MD5, "md5",
                        "ReMIND_NRRD_Seg_Sep_2023/ReMIND-001/"
                        "ReMIND-001-preop-SEG-tumor-MR-3D_AX_T1_postcontrast.nrrd",
                        "ReMIND-001-preop-SEG-tumor-MR-3D_AX_T1_postcontrast.nrrd",
                        ".nrrd",
                    ),
                    (
                        "Collection", "ReMIND", "43725", 2,
                        public.EMPTY_FILE_MD5, "md5",
                        "ReMIND_NRRD_Seg_Sep_2023/ReMIND-032/"
                        "ReMIND-032-intraop-SEG-tumor_residual-MR-2D_AX_T2_BLADE.nrrd",
                        "ReMIND-032-intraop-SEG-tumor_residual-MR-2D_AX_T2_BLADE.nrrd",
                        ".nrrd",
                    ),
                    (
                        "Collection", "ReMIND", "43725", 3, "abc", "md5",
                        "ReMIND_NRRD_Seg_Sep_2023/not-a-reviewed-path.nrrd",
                        "not-a-reviewed-path.nrrd", ".nrrd",
                    ),
                ],
            )
            source.execute(
                "INSERT INTO agent_nifti_downloads VALUES (?,?,?)",
                ("ReMIND", "43725", "https://example.test/remind-aspera"),
            )
            source.commit()
            source.close()

            conn = sqlite3.connect(output_db)
            conn.row_factory = sqlite3.Row
            conn.executescript(public.SCHEMA)
            public.insert_vocab(conn)
            self.assertEqual(
                public.ingest_remind_nrrd_inventory(
                    conn, source_db, inventory_path=None
                ),
                2,
            )
            rows = conn.execute(
                """
                SELECT subject_id, file_format, object_role, checksum,
                       json_extract(raw_values_json, '$.segment_label'),
                       json_extract(quality_flag_json, '$.checksum')
                FROM public_non_dicom_assets
                ORDER BY subject_id
                """
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    ("ReMIND-001", "NRRD", "segmentation", "", "tumor",
                     "invalid_empty_file_placeholder"),
                    ("ReMIND-032", "NRRD", "segmentation", "", "tumor_residual",
                     "invalid_empty_file_placeholder"),
                ],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_asset_participants"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(public.sync_scalar_asset_participants(conn), 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_crosswalk_evidence "
                    "WHERE mapping_method='reviewed_remind_package_subject_folder_and_filename'"
                ).fetchone()[0],
                2,
            )
            conn.close()

    def test_reviewed_remind_reference_is_hash_pinned_and_complete(self):
        rows, provenance = public.load_remind_nrrd_reference(
            public.DEFAULT_REMIND_NRRD_INVENTORY
        )
        self.assertEqual(len(rows), 356)
        self.assertEqual(provenance["participant_count"], 114)
        self.assertEqual(
            len(
                {
                    public.REMIND_NRRD_PATH.search(row["package_path"]).group("subject")
                    for row in rows
                    if public.REMIND_NRRD_PATH.search(row["package_path"])
                }
            ),
            114,
        )

    def test_legacy_idc_evidence_does_not_claim_current_public_dicom(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "participants.sqlite"
            conn = sqlite3.connect(database)
            conn.executescript(participants.SCHEMA)
            conn.execute(
                "INSERT INTO participants VALUES (?,?,?,?,?,?,?,?)",
                (
                    "participant-key", "Collection", "Brain-TR-GammaKnife", "GK_103",
                    "dataset_scoped", "single_namespace", "source_identifier", "not_asserted",
                ),
            )
            conn.execute(
                """
                INSERT INTO participant_assets (
                    participant_asset_id, participant_key, managed_system, source_artifact,
                    access_level, data_domain, media_kind, modality, file_format, object_role,
                    has_file_level_metadata, inventory_status, source_version, provenance_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "legacy-idc", "participant-key", "crdc_idc", "clinical_metadata",
                    "open", "radiology", "dicom_series", "", "DICOM",
                    "source_image_or_annotation", 0,
                    "historical_participant_presence_query_idc_or_tcia_for_detail",
                    "legacy", "{}",
                ),
            )
            row = conn.execute(
                "SELECT has_open_data, has_public_dicom, data_domains, file_formats "
                "FROM agent_participants WHERE participant_key='participant-key'"
            ).fetchone()
            self.assertEqual(row, (0, 0, None, None))
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM participant_assets WHERE participant_key='participant-key'"
                ).fetchone()[0],
                1,
            )
            conn.close()

    def test_reviewed_readme_and_pathdb_contract_links_partial_dataset(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            curation = Path(directory) / "curation.json"
            curation.write_text(json.dumps({
                "reviewed_at": "2026-08-18",
                "review_source": "unit test",
                "decisions": [{
                    "dataset_type": "Collection",
                    "short_title": "C-NMC 2019",
                    "download_ids": ["41841"],
                    "decision_status": "resolved",
                    "resolution_type": "participant_pathdb_contract",
                    "reviewed_at": "2026-08-20",
                    "reviewer_note": "Published README contract.",
                    "evidence_url": "https://example.test/readme.pdf",
                    "pathdb_non_participant_ids": ["NA_final"],
                    "path_rules": [{
                        "name": "training",
                        "pattern": "^C-NMC_training_data/fold_(?P<fold>[0-2])/(?P<class_name>all|hem)/UID_(?P<subject_prefix>H?)(?P<subject_number>[0-9]+)_[0-9]+_[0-9]+_(?P=class_name)[.]bmp$",
                        "participant_id_template": "{subject_prefix}{subject_number}_training_fold{fold}_{class_name}",
                    }],
                    "pathdb_file_rules": [{
                        "name": "prelim",
                        "pattern": "^C-NMC_test_prelim_phase_data/(?P<file_stem>[0-9]+)[.]bmp$",
                        "pathdb_url_marker": "/converted/C-NMC_Leukemia/",
                        "pathdb_relative_path_template": "C-NMC_test_prelim_phase_data/{file_stem}.tiff",
                        "excluded_subject_ids": ["NA_final"],
                    }],
                    "source_unavailable_rules": [{
                        "name": "final",
                        "pattern": "^C-NMC_test_final_phase_data/[0-9]+[.]bmp$",
                        "reason": "final_test_participant_mapping_not_published",
                    }],
                }],
            }))
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.executescript(public.SCHEMA)
            public.insert_vocab(conn)

            def add_asset(asset_id, path, system, subject_id="", source_url=""):
                public.insert_asset(conn, {
                    "asset_id": asset_id,
                    "dataset_type": "Collection",
                    "short_title": "C-NMC 2019",
                    "download_id": "41841" if system == "tcia_aspera" else "",
                    "subject_id": subject_id,
                    "subject_id_namespace": "tcia_dataset:C-NMC 2019" if subject_id else "",
                    "participant_link_status": (
                        "dataset_scoped_source_identifier" if subject_id else "unavailable"
                    ),
                    "asset_granularity": "file",
                    "asset_name": Path(path).name,
                    "file_name": Path(path).name,
                    "package_path": path if system == "tcia_aspera" else "",
                    "file_format": "BMP" if system == "tcia_aspera" else "TIFF",
                    "media_kind": "still_image",
                    "spatial_dimensionality": "2D",
                    "temporal_dimensionality": "static",
                    "imaging_domain": "pathology",
                    "modality": "SM",
                    "object_role": "source_image",
                    "representation_provenance_class": "unknown",
                    "source_system": system,
                    "source_url": source_url,
                    "raw_values_json": "{}",
                    "provenance_json": "{}",
                    "quality_flag_json": "{}",
                })

            add_asset(
                "pathdb-training", "training.tiff", "tcia_pathdb",
                "11_training_fold0_all", "https://pathdb.example/training.tiff",
            )
            add_asset(
                "pathdb-prelim", "prelim.tiff", "tcia_pathdb",
                "53_prelim_all",
                "https://pathdb.example/converted/C-NMC_Leukemia/"
                "C-NMC_test_prelim_phase_data/1.tiff",
            )
            add_asset(
                "pathdb-final", "final.tiff", "tcia_pathdb", "NA_final",
                "https://pathdb.example/converted/C-NMC_Leukemia/"
                "C-NMC_test_final_phase_data/1.tiff",
            )
            add_asset(
                "training", "C-NMC_training_data/fold_0/all/UID_11_10_1_all.bmp",
                "tcia_aspera",
            )
            add_asset(
                "prelim", "C-NMC_test_prelim_phase_data/1.bmp", "tcia_aspera",
            )
            add_asset(
                "final", "C-NMC_test_final_phase_data/1.bmp", "tcia_aspera",
            )

            result = public.apply_reviewed_pathdb_contracts(conn, curation)
            self.assertEqual(result, {
                "decisions": 1,
                "file_assets": 2,
                "evidence_rows": 2,
                "source_confirmed_unavailable_assets": 1,
                "pathdb_non_participant_assets": 1,
                "unmatched_assets": 0,
            })
            self.assertEqual(
                [tuple(row) for row in conn.execute(
                    "SELECT asset_id, subject_id, participant_link_status "
                    "FROM public_non_dicom_assets "
                    "WHERE asset_id IN ('training', 'prelim', 'final') ORDER BY asset_id"
                )],
                [
                    ("final", "", "source_confirmed_unavailable"),
                    ("prelim", "53_prelim_all", "reviewed_source_crosswalk"),
                    ("training", "11_training_fold0_all", "reviewed_source_crosswalk"),
                ],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_crosswalk_evidence"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                tuple(conn.execute(
                    "SELECT subject_id, participant_link_status, "
                    "json_extract(raw_values_json, '$.pathdb_non_participant_label') "
                    "FROM public_non_dicom_assets WHERE asset_id='pathdb-final'"
                ).fetchone()),
                ("", "source_confirmed_unavailable", "NA_final"),
            )
            conn.close()

    def test_reviewed_hancock_contract_links_core_wsi_and_shared_tma_block(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            curation = Path(directory) / "curation.json"
            curation.write_text(json.dumps({
                "reviewed_at": "2026-08-20",
                "review_source": "unit test",
                "decisions": [{
                    "dataset_type": "Collection",
                    "short_title": "HANCOCK",
                    "download_ids": ["52636"],
                    "decision_status": "resolved",
                    "resolution_type": "participant_pathdb_contract",
                    "reviewer_note": "Reviewed HANCOCK contract.",
                    "evidence_url": "https://example.test/hancock",
                    "path_rules": [
                        {
                            "name": "core",
                            "pattern": "^Images/TMA_Cores/[^/]+/[^/]*_patient(?P<subject_number>[0-9]{3})[.]png$",
                            "participant_id_template": "patient{subject_number}",
                        },
                        {
                            "name": "wsi",
                            "pattern": "^Images/WSI_[^/]+/(?:LymphNode|PrimaryTumor)_HE_(?P<subject_number>[0-9]{3})(?:_a)?[.]svs$",
                            "participant_id_template": "patient{subject_number}",
                        },
                    ],
                    "pathdb_asset_rules": [{
                        "name": "tma",
                        "pattern": "^Images/TMA_(?:InvasionFront|TumorCenter)/[^/]+/(?P<pathdb_asset_name>(?:InvasionFront|TumorCenter)_[^/]+_block[0-9]+)[.]svs$",
                        "pathdb_asset_name_template": "{pathdb_asset_name}",
                    }],
                }],
            }))
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.executescript(public.SCHEMA)
            public.insert_vocab(conn)

            def add_asset(asset_id, path, system, subject_id="", asset_name=""):
                public.insert_asset(conn, {
                    "asset_id": asset_id,
                    "dataset_type": "Collection",
                    "short_title": "HANCOCK",
                    "download_id": "52636" if system == "tcia_aspera" else "",
                    "subject_id": subject_id,
                    "subject_id_namespace": "tcia_dataset:HANCOCK" if subject_id else "",
                    "participant_link_status": (
                        "dataset_scoped_source_identifier" if subject_id else "unavailable"
                    ),
                    "asset_granularity": "file",
                    "asset_name": asset_name or Path(path).stem,
                    "file_name": Path(path).name,
                    "package_path": path if system == "tcia_aspera" else "",
                    "file_format": Path(path).suffix.lstrip(".").upper(),
                    "media_kind": "whole_slide_image" if path.endswith(".svs") else "still_image",
                    "spatial_dimensionality": "2D",
                    "temporal_dimensionality": "static",
                    "imaging_domain": "pathology",
                    "modality": "SM",
                    "object_role": "source_image",
                    "representation_provenance_class": "unknown",
                    "source_system": system,
                    "raw_values_json": "{}",
                    "provenance_json": "{}",
                    "quality_flag_json": "{}",
                })

            add_asset("pathdb-core", "core.png", "tcia_pathdb", "patient001")
            add_asset("pathdb-wsi", "wsi.svs", "tcia_pathdb", "patient036")
            add_asset(
                "pathdb-tma", "tma.svs", "tcia_pathdb", "",
                "InvasionFront_CD3_block2",
            )
            for raw_id in ("1", "74", "83"):
                public.insert_asset_participant(
                    conn,
                    asset_id="pathdb-tma",
                    short_title="HANCOCK",
                    subject_id=f"patient{int(raw_id):03d}",
                    namespace="tcia_dataset:HANCOCK",
                    raw_subject_id=raw_id,
                    participant_role="tma_block_member",
                    link_status="multi_participant_source_list",
                    evidence={"source": "PathDB"},
                )
            add_asset(
                "core", "Images/TMA_Cores/tma_tumorcenter_CD3/"
                "TumorCenter_CD3_block2_x3_y9_patient001.png", "tcia_aspera",
            )
            add_asset(
                "wsi", "Images/WSI_PrimaryTumor_Hypopharynx/"
                "PrimaryTumor_HE_036_a.svs", "tcia_aspera",
            )
            add_asset(
                "tma", "Images/TMA_InvasionFront/CD3/"
                "InvasionFront_CD3_block2.svs", "tcia_aspera",
            )

            result = public.apply_reviewed_pathdb_contracts(conn, curation)
            self.assertEqual(result, {
                "decisions": 1,
                "file_assets": 3,
                "evidence_rows": 5,
                "source_confirmed_unavailable_assets": 0,
                "pathdb_non_participant_assets": 0,
                "unmatched_assets": 0,
            })
            self.assertEqual(
                [tuple(row) for row in conn.execute(
                    "SELECT asset_id, subject_id, participant_link_status "
                    "FROM public_non_dicom_assets "
                    "WHERE asset_id IN ('core', 'wsi', 'tma') ORDER BY asset_id"
                )],
                [
                    ("core", "patient001", "reviewed_source_crosswalk"),
                    ("tma", "", "reviewed_source_crosswalk"),
                    ("wsi", "patient036", "reviewed_source_crosswalk"),
                ],
            )
            self.assertEqual(
                [tuple(row) for row in conn.execute(
                    "SELECT subject_id, raw_subject_id, participant_role "
                    "FROM public_non_dicom_asset_participants "
                    "WHERE asset_id='tma' ORDER BY subject_id"
                )],
                [
                    ("patient001", "1", "tma_block_member"),
                    ("patient074", "74", "tma_block_member"),
                    ("patient083", "83", "tma_block_member"),
                ],
            )
            conn.close()

    def test_reviewed_wordpress_path_contract_links_only_patient_paths(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            clinical_db = base / "clinical.sqlite"
            pathology_db = base / "pathology.sqlite"
            curation = base / "curation.json"
            clinical = sqlite3.connect(clinical_db)
            clinical.executescript(
                """
                CREATE TABLE clinical_subjects (short_title TEXT, subject_id TEXT);
                INSERT INTO clinical_subjects VALUES ('DLBCL-Morphology', '13952');
                CREATE VIEW agent_clinical_all_subjects AS
                SELECT short_title, subject_id FROM clinical_subjects;
                """
            )
            clinical.commit()
            clinical.close()
            pathology = sqlite3.connect(pathology_db)
            pathology.executescript(
                """
                CREATE TABLE agent_pathology_file_objects (
                    dataset_type TEXT, short_title TEXT, is_metadata INTEGER,
                    package_path TEXT, file_name TEXT, image_format TEXT,
                    file_ext TEXT, download_id TEXT, bytes INTEGER
                );
                INSERT INTO agent_pathology_file_objects VALUES
                  ('Collection', 'DLBCL-Morphology', 0,
                   'DLBCL-Morph/Cells/13952/44009/1.npy', '1.npy', 'NPY', '.npy', '42421', 8),
                  ('Collection', 'DLBCL-Morphology', 0,
                   'DLBCL-Morph/Cells/13952/44009/2.npy', '2.npy', 'NPY', '.npy', '42421', 12),
                  ('Collection', 'DLBCL-Morphology', 1,
                   'DLBCL-Morph/Cells/13952/44009/metadata.npy', 'metadata.npy', 'NPY', '.npy', '42421', 4),
                  ('Collection', 'DLBCL-Morphology', 0,
                   'DLBCL-Morph/Cells/99999/44010/1.npy', '1.npy', 'NPY', '.npy', '42421', 8);
                CREATE TABLE agent_pathology_downloads (
                    short_title TEXT, download_id TEXT, download_url TEXT
                );
                INSERT INTO agent_pathology_downloads VALUES
                  ('DLBCL-Morphology', '42421', 'https://example.test/dlbcl-package');
                """
            )
            pathology.commit()
            pathology.close()
            curation.write_text(json.dumps({
                "reviewed_at": "2026-08-18",
                "review_source": "unit test",
                "decisions": [{
                    "dataset_type": "Collection",
                    "short_title": "DLBCL-Morphology",
                    "download_ids": ["42421"],
                    "decision_status": "resolved",
                    "resolution_type": "participant_path_contract",
                    "reviewed_at": "2026-08-20",
                    "reviewer_note": "WordPress directory contract.",
                    "evidence_url": "https://example.test/dlbcl",
                    "identifier_source": "clinical_metadata.agent_clinical_all_subjects",
                    "identifier_source_url": "https://example.test/clinical.csv",
                    "path_rules": [
                        {
                            "name": "wsi",
                            "pattern": "^DLBCL-Morph/WSI/(?P<participant_id>[0-9]+)(?:_[0-9]+)?[.]svs$",
                        },
                        {
                            "name": "patch",
                            "pattern": "^DLBCL-Morph/Patches/[^/]+/(?P<participant_id>[0-9]+)/[^/]+[.]png$",
                        },
                    ],
                    "compact_path_rules": [{
                        "name": "cells",
                        "pattern": "^DLBCL-Morph/Cells/(?P<participant_id>[0-9]+)/[^/]+/[^/]+[.]npy$",
                        "source_file_format": "NPY",
                        "include_metadata": True,
                        "asset_group": "cell_shape_arrays",
                        "asset_label": "cell-shape arrays",
                        "media_kind": "spectral_or_array",
                        "spatial_dimensionality": "unknown",
                        "temporal_dimensionality": "static",
                        "imaging_domain": "pathology",
                        "modality": "",
                        "object_role": "cell_shape_array",
                        "representation_provenance_class": "derived_asset",
                    }],
                }],
            }))

            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.executescript(public.SCHEMA)
            public.insert_vocab(conn)
            common = {
                "dataset_type": "Collection",
                "short_title": "DLBCL-Morphology",
                "download_id": "42421",
                "subject_id": "",
                "subject_id_namespace": "",
                "participant_link_status": "unavailable",
                "asset_granularity": "file",
                "media_kind": "whole_slide_image",
                "spatial_dimensionality": "2D",
                "temporal_dimensionality": "static",
                "imaging_domain": "pathology",
                "modality": "SM",
                "object_role": "source_image",
                "representation_provenance_class": "submitted_original",
                "source_system": "tcia_aspera",
                "raw_values_json": "{}",
                "provenance_json": "{}",
                "quality_flag_json": "{}",
            }
            for asset_id, path, file_format in (
                ("wsi", "DLBCL-Morph/WSI/13952_0.svs", "SVS"),
                ("patch", "DLBCL-Morph/Patches/BCL2/13952/44009.png", "PNG"),
                ("tma", "DLBCL-Morph/TMA/BCL2/44009.svs", "SVS"),
            ):
                file_name = path.rsplit("/", 1)[-1]
                public.insert_asset(conn, {
                    **common,
                    "asset_id": asset_id,
                    "asset_name": file_name,
                    "file_name": file_name,
                    "package_path": path,
                    "file_format": file_format,
                })

            result = public.apply_reviewed_path_contracts(
                conn, clinical_db, curation, pathology_db=pathology_db
            )
            self.assertEqual(result, {
                "decisions": 1,
                "file_assets": 2,
                "compact_assets": 1,
                "represented_files": 3,
                "evidence_rows": 3,
                "unmatched_assets": 1,
                "unmatched_source_files": 1,
            })
            self.assertEqual(
                tuple(conn.execute(
                    "SELECT participant_link_status, subject_id "
                    "FROM public_non_dicom_assets WHERE asset_id='wsi'"
                ).fetchone()),
                ("reviewed_source_crosswalk", "13952"),
            )
            self.assertEqual(
                tuple(conn.execute(
                    "SELECT participant_link_status, subject_id "
                    "FROM public_non_dicom_assets WHERE asset_id='patch'"
                ).fetchone()),
                ("reviewed_source_crosswalk", "13952"),
            )
            self.assertEqual(
                tuple(conn.execute(
                    "SELECT participant_link_status, subject_id "
                    "FROM public_non_dicom_assets WHERE asset_id='tma'"
                ).fetchone()),
                ("unavailable", ""),
            )
            self.assertEqual(
                tuple(conn.execute(
                    "SELECT asset_granularity, subject_id, represented_file_count, size_bytes "
                    "FROM public_non_dicom_assets WHERE file_format='NPY'"
                ).fetchone()),
                ("participant_file_group", "13952", 3, 24),
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM public_non_dicom_crosswalk_evidence").fetchone()[0],
                3,
            )
            conn.close()

    def test_download_parent_resolves_from_json_array_file_association(self):
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(public.SCHEMA)
        public.insert_vocab(conn)
        common = {
            "dataset_type": "Collection",
            "short_title": "Example-NIfTI",
            "media_kind": "image_volume",
            "spatial_dimensionality": "3D",
            "temporal_dimensionality": "static",
            "imaging_domain": "radiology",
            "modality": "MR",
            "object_role": "segmentation",
            "representation_provenance_class": "submitted_original",
            "source_system": "tcia_aspera",
            "raw_values_json": "{}",
            "provenance_json": "{}",
            "quality_flag_json": "{}",
        }
        public.insert_asset(conn, {
            **common,
            "asset_id": "download-nifti",
            "download_id": "51716",
            "subject_id": "",
            "subject_id_namespace": "",
            "participant_link_status": "dataset_only",
            "asset_granularity": "download",
            "asset_name": "NIfTI package",
            "file_name": "",
            "package_path": "",
            "file_format": "NIFTI",
        })
        public.insert_asset(conn, {
            **common,
            "asset_id": "file-nifti",
            "download_id": '["51716"]',
            "subject_id": "EXAMPLE-001",
            "subject_id_namespace": "tcia_dataset:Example-NIfTI",
            "participant_link_status": "dataset_scoped_source_identifier",
            "asset_granularity": "file",
            "asset_name": "EXAMPLE-001_seg.nii.gz",
            "file_name": "EXAMPLE-001_seg.nii.gz",
            "package_path": "Example-NIfTI/EXAMPLE-001_seg.nii.gz",
            "file_format": "NIFTI",
        })
        public.sync_scalar_asset_participants(conn)

        self.assertEqual(public.mark_downloads_with_linked_file_grain(conn), 1)
        status, flags = conn.execute(
            "SELECT participant_link_status, quality_flag_json "
            "FROM public_non_dicom_assets WHERE asset_id='download-nifti'"
        ).fetchone()
        self.assertEqual(status, "crosswalk_available_at_file_grain")
        self.assertEqual(
            json.loads(flags)["participant_inventory"],
            "crosswalk_available_at_file_grain",
        )
        conn.close()

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
                    "reviewed_at": "2026-08-20",
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
            self.assertEqual(
                conn.execute(
                    "SELECT reviewed_at FROM public_non_dicom_crosswalk_evidence"
                ).fetchone()[0],
                "2026-08-20",
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
              hidden INTEGER, controlled_access INTEGER, subjects INTEGER, images INTEGER
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
             '["Radiology Images"]', '["CT"]', '["MHA"]', 0, 0, 10, 100),
            (2, "Collection", "Breast-Lesions-USG", "2", "Images and masks", "US",
             "https://www.cancerimagingarchive.net/file.zip", "1", "gb",
             '["Radiology Images","Image Annotations"]', '["US","Segmentation"]',
             '["PNG","ZIP"]', 0, 0, 20, 200),
            (3, "Collection", "Capsule-Endoscopy-SB-NET", "3", "Excerpts", "Capsule",
             "https://faspex.cancerimagingarchive.net/?context=y", "1", "gb",
             '["Other"]', '["Capsule Endoscopy"]', '["JPG","MPG"]', 0, 0, 30, 300),
            (4, "Collection", "Public-DICOM", "4", "Images", "DICOM",
             "https://example.org/manifest.tcia", "1", "gb",
             '["Radiology Images"]', '["CT"]', '["DICOM"]', 0, 0, 40, 400),
        ]
        conn.executemany("INSERT INTO agent_current_downloads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
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
            self.assertEqual(
                conn.execute(
                    "SELECT represented_file_count FROM public_non_dicom_assets "
                    "WHERE short_title='Pedi-Cranial-CT-Healthy'"
                ).fetchone()[0],
                100,
            )
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

    def test_tcga_lgg_mask_reviewed_file_and_participant_coverage(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot = base / "snapshot.sqlite"
            output = base / "public.sqlite"
            self.build_snapshot(snapshot)
            conn = sqlite3.connect(snapshot)
            rows = [
                (
                    101, "Analysis Result", "TCGA-LGG-Mask", "45749",
                    "VASARI information", "TCGA-LGG-Mask",
                    "https://www.cancerimagingarchive.net/wp-content/uploads/TCGA_vasari_INFO.csv",
                    "12.3", "kb", "[]", '["Other"]', '["CSV"]',
                    0, 0, 188, 1,
                ),
                (
                    102, "Analysis Result", "TCGA-LGG-Mask", "45751",
                    "VASARI MR feature key", "TCGA-LGG-Mask",
                    "https://www.cancerimagingarchive.net/wp-content/uploads/VASARI_MR_featurekey.pdf",
                    "111.35", "kb", "[]", '["Other"]', '["PDF"]',
                    0, 0, 0, 1,
                ),
                (
                    103, "Analysis Result", "TCGA-LGG-Mask", "45753",
                    "Matlab Segmentations", "TCGA-LGG-Mask",
                    "https://www.cancerimagingarchive.net/wp-content/uploads/TCGA_LGG_masks.zip",
                    "2.08", "mb", "[]", '["Segmentation"]',
                    '["MATLAB","ZIP"]', 0, 0, 108, 406,
                ),
            ]
            conn.executemany(
                "INSERT INTO agent_current_downloads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.execute(
                "INSERT INTO agent_datasets VALUES ('Analysis Result', 'TCGA-LGG-Mask')"
            )
            conn.commit()
            conn.close()
            result = public.build_database(
                snapshot,
                output,
                nifti_db=None,
                pathology_db=None,
                include_pathdb_files=False,
                replace=True,
            )
            self.assertEqual(result["counts"]["tcga_lgg_mask_mask_files"], 406)
            self.assertEqual(result["counts"]["tcga_lgg_mask_mask_participants"], 108)
            self.assertEqual(result["counts"]["tcga_lgg_mask_vasari_participants"], 188)
            conn = sqlite3.connect(output)
            self.assertEqual(
                conn.execute(
                    "SELECT subject_id FROM public_non_dicom_asset_participants "
                    "WHERE raw_subject_id='TCGA-EZ-7264A'"
                ).fetchone()[0],
                "TCGA-EZ-7264",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(DISTINCT json_extract(metadata_json, "
                    "'$.source_series_instance_uid')) "
                    "FROM public_non_dicom_image_metadata "
                    "WHERE short_title='TCGA-LGG-Mask'"
                ).fetchone()[0],
                406,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM public_non_dicom_asset_relationships "
                    "WHERE relationship_type='interpreted_by'"
                ).fetchone()[0],
                1,
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
            public_audit_db = base / "public-audit.sqlite"
            participant_db = base / "participants.sqlite"
            self.build_snapshot(snapshot)

            conn = sqlite3.connect(snapshot)
            conn.execute(
                "INSERT INTO agent_current_downloads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    5, "Analysis Result", "RSNA-ASNR-MICCAI-BraTS-2021", "46595",
                    "Challenge data both tasks", "BraTS 2021", "https://faspex.example/package",
                    "1", "tb", '["Radiology Images"]', '["MR"]',
                    '["DICOM","NIfTI"]', 0, 0, 0, 0,
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
                "UCSF-PDGM/00000/T1w/Image-1.dcm",
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
            self.assertEqual(result["counts"]["aspera_public_dicom_exception_assets"], 4)

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
                    ("UCSF-PDGM-0057", "MR", 1, "submitted_original", "tcia_aspera"),
                ],
            )
            crosswalk = conn.execute(
                """
                SELECT raw_subject_id, resolved_subject_id, mapping_method
                FROM public_non_dicom_crosswalk_evidence
                """
            ).fetchone()
            self.assertEqual(
                crosswalk,
                ("BraTS2021_00000", "UCSF-PDGM-0057", "official_tcia_brats2021_workbook"),
            )
            conn.close()

            audit.split_database(
                public_db,
                public_audit_db,
                artifact="public_non_dicom",
                replace=True,
            )

            participants.build_database(
                participant_db,
                snapshot_db=snapshot,
                public_db=public_db,
                public_audit_db=public_audit_db,
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
                    ("UCSF-PDGM-0057", 1, 0, "DICOM"),
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
            self.assertEqual(
                file_counts,
                {
                    "BraTS2021_00393": 1,
                    "BraTS2021_00794": 3,
                    "UCSF-PDGM-0057": 1,
                },
            )
            alias = conn.execute(
                """
                SELECT display_participant_id, raw_identifier, link_evidence
                FROM agent_participant_identifiers
                WHERE identifier_namespace='challenge:BraTS2021'
                  AND raw_identifier='BraTS2021_00000'
                """
            ).fetchone()
            self.assertEqual(
                alias,
                (
                    "UCSF-PDGM-0057",
                    "BraTS2021_00000",
                    "official_tcia_brats2021_workbook",
                ),
            )
            issue = conn.execute(
                """
                SELECT issue_code FROM participant_link_issues
                WHERE raw_identifier='BraTS2021_00000'
                """
            ).fetchone()
            self.assertEqual(
                issue,
                ("brats_source_collection_participant_not_current",),
            )
            conn.close()

    def test_participant_inventory_unifies_case_equivalent_same_dataset_identifiers(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot = base / "snapshot.sqlite"
            controlled = base / "controlled.sqlite"
            clinical = base / "clinical.sqlite"
            idc_projection = base / "idc_participants.sqlite"
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
            conn.commit()
            conn.close()

            conn = sqlite3.connect(idc_projection)
            conn.execute(
                """
                CREATE TABLE agent_idc_dataset_participants (
                  dataset_type TEXT, short_title TEXT, participant_id TEXT,
                  source_collection_ids_json TEXT,
                  source_analysis_result_ids_json TEXT,
                  study_count INTEGER, series_count INTEGER, modalities TEXT,
                  source_dois TEXT, idc_version TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO agent_idc_dataset_participants VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "Collection", "AAPM-RT-MAC", "rtmac-live-008",
                        '["aapm_rt_mac"]', "[]", 1, 2, "CT;MR", "", "v24",
                    ),
                    (
                        "Collection", "AAPM-RT-MAC", "IDC-ONLY-009",
                        '["aapm_rt_mac"]', "[]", 1, 1, "CT", "", "v24",
                    ),
                    (
                        "Analysis Result", "Outcome-Result", "CASE-002",
                        '["aapm_rt_mac"]', '["outcome_result"]', 1, 1, "SEG", "", "v24",
                    ),
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
                idc_db=idc_projection,
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
                    "casefolded_identifier_same_tcia_dataset", 2, "MR,CT;MR",
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
            self.assertEqual(
                conn.execute(
                    "SELECT dataset_type, modality, series_count "
                    "FROM agent_participant_assets "
                    "WHERE short_title='Outcome-Result' "
                    "AND display_participant_id='CASE-002'"
                ).fetchone(),
                ("Analysis Result", "SEG", 1),
            )
            conn.close()
            regression = participants.validate_database(
                output, minimum_analysis_result_participants=3
            )
            self.assertFalse(regression["ok"])
            self.assertTrue(
                any("analysis_result_participants coverage regression" in error
                    for error in regression["errors"])
            )

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
