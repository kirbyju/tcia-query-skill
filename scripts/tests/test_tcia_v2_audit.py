import importlib.util
import sqlite3
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


public = load("tcia_public_non_dicom_metadata")
participants = load("tcia_participant_inventory")
audit = load("tcia_v2_audit")


class V2AuditSplitTests(unittest.TestCase):
    def test_participant_audit_embeds_clinical_qc_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "participant.sqlite"
            audit_database = root / "participant_audit.sqlite"
            clinical_qc = root / "clinical_qc.csv"
            clinical_qc.write_text(
                "short_title,subject_id,review_note\nTEST,P1,check value\n",
                encoding="utf-8",
            )
            with sqlite3.connect(database) as conn:
                conn.executescript(participants.SCHEMA)
                conn.execute(
                    "INSERT INTO participant_inventory_meta VALUES ('schema_version', '6')"
                )
            result = audit.split_database(
                database,
                audit_database,
                artifact="participant_inventory",
                clinical_qc_csv=clinical_qc,
                replace=True,
            )
            self.assertTrue(result["audit_validation"]["ok"])
            with sqlite3.connect(audit_database) as conn:
                source_row, row_json = conn.execute(
                    "SELECT source_row_number, row_json FROM clinical_qc_manual_review"
                ).fetchone()
                self.assertEqual(source_row, 2)
                self.assertEqual(__import__("json").loads(row_json)["subject_id"], "P1")

    def test_public_research_artifact_keeps_root_source_and_moves_verbose_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "public_non_dicom_metadata.sqlite"
            audit_database = root / "public_non_dicom_audit.sqlite"
            with sqlite3.connect(database) as conn:
                conn.executescript(public.SCHEMA)
                public.insert_vocab(conn)
                public.insert_asset(
                    conn,
                    {
                        "asset_id": "asset-1",
                        "dataset_type": "Collection",
                        "short_title": "TEST",
                        "download_id": "1",
                        "subject_id": "P1",
                        "subject_id_namespace": "tcia_dataset:TEST",
                        "participant_link_status": "source_identifier",
                        "asset_granularity": "file",
                        "asset_name": "image.nii.gz",
                        "file_name": "image.nii.gz",
                        "package_path": "P1/image.nii.gz",
                        "file_format": "NIFTI",
                        "media_kind": "image_volume",
                        "spatial_dimensionality": "3D",
                        "temporal_dimensionality": "static",
                        "imaging_domain": "radiology",
                        "modality": "MR",
                        "object_role": "source_image",
                        "representation_provenance_class": "submitted_original",
                        "source_system": "tcia_aspera",
                        "source_record_id": "row-1",
                        "source_url": "https://example.test/package",
                        "raw_values_json": '{"source_column":"raw"}',
                        "provenance_json": '{"source_artifact":"nifti_metadata"}',
                        "quality_flag_json": '{"reviewed":true}',
                    },
                )
                public.insert_location(
                    conn,
                    {
                        "location_id": "location-1",
                        "asset_id": "asset-1",
                        "managed_system": "tcia_aspera",
                        "system_functions_json": '["distribution_endpoint"]',
                        "access_url": "https://example.test/package",
                        "access_level": "open",
                        "availability_status": "observed",
                        "representation_provenance_class": "submitted_original",
                        "equivalence_status": "unresolved",
                        "provenance_json": '{"source_artifact":"nifti_metadata"}',
                    },
                )
                public.insert_asset_participant(
                    conn,
                    asset_id="asset-1",
                    short_title="TEST",
                    subject_id="P1",
                    namespace="tcia_dataset:TEST",
                    raw_subject_id="P1",
                    participant_role="depicted_subject",
                    link_status="source_identifier",
                    evidence={"mapping_method": "source_identifier"},
                )
                public.merge_image_metadata(
                    conn,
                    "asset-1",
                    {"modality": "MR"},
                    value_role="source_raw",
                    source_kind="nifti_metadata",
                    source_locator="agent_nifti_files",
                    inference_method="direct",
                    confidence="high",
                    priority=100,
                    short_title="TEST",
                    assume_new=True,
                )
                conn.execute(
                    "INSERT INTO public_non_dicom_crosswalk_evidence VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "crosswalk-1", "asset-1", "TEST", "P1", "P1",
                        "source_identifier", "high", "https://example.test", "reviewed",
                        "", "2026-08-17", '{"source_file":"crosswalk.csv"}',
                    ),
                )
                conn.execute("INSERT INTO artifact_meta VALUES ('schema_version', '5')")
                conn.commit()
            result = audit.split_database(
                database,
                audit_database,
                artifact="public_non_dicom",
                replace=True,
            )
            self.assertEqual(result["integrity_check"], "ok")
            self.assertTrue(public.validate_database(database)["ok"])
            with sqlite3.connect(database) as conn:
                row = conn.execute(
                    "SELECT source_system, source_url, raw_values_json, provenance_json, "
                    "quality_flag_json FROM public_non_dicom_assets"
                ).fetchone()
                self.assertEqual(row[:2], ("tcia_aspera", "https://example.test/package"))
                self.assertEqual(row[2:], ("{}", "{}", "{}"))
                source_map = __import__("json").loads(
                    conn.execute(
                        "SELECT field_source_ids_json FROM public_non_dicom_image_metadata"
                    ).fetchone()[0]
                )
                self.assertIn("modality", source_map)
                self.assertEqual(
                    conn.execute(
                        "SELECT source_kind, source_locator "
                        "FROM public_non_dicom_metadata_sources WHERE source_id=?",
                        (source_map["modality"],),
                    ).fetchone(),
                    ("nifti_metadata", "agent_nifti_files"),
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM public_non_dicom_crosswalk_evidence").fetchone()[0],
                    0,
                )
            with sqlite3.connect(audit_database) as conn:
                fields = set(conn.execute("SELECT field_name FROM agent_entity_payloads"))
                self.assertIn(("provenance_json",), fields)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM public_non_dicom_crosswalk_evidence").fetchone()[0],
                    1,
                )

    def test_participant_inventory_defers_clinical_facts_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.sqlite"
            clinical = root / "clinical.sqlite"
            compact = root / "participant.sqlite"
            embedded = root / "participant-embedded.sqlite"
            with sqlite3.connect(snapshot) as conn:
                conn.execute("CREATE TABLE agent_datasets (dataset_type TEXT, short_title TEXT)")
                conn.execute("INSERT INTO agent_datasets VALUES ('Collection', 'TEST')")
            with sqlite3.connect(clinical) as conn:
                conn.execute(
                    "CREATE TABLE agent_clinical_all_subjects "
                    "(short_title TEXT, subject_id TEXT, source_kinds TEXT, has_imaging INTEGER)"
                )
                conn.execute("INSERT INTO agent_clinical_all_subjects VALUES ('TEST', 'P1', '[\"source\"]', 1)")
                conn.execute(
                    "CREATE TABLE agent_clinical_facts ("
                    "short_title TEXT, subject_id TEXT, concept TEXT, value_text TEXT, "
                    "value_resolved TEXT, source_kind TEXT, source_url TEXT, original_column TEXT, "
                    "evidence_scope TEXT, is_inferred INTEGER, qc_status TEXT, provenance_json TEXT, "
                    "qc_excluded INTEGER)"
                )
                conn.execute(
                    "INSERT INTO agent_clinical_facts VALUES "
                    "('TEST','P1','sex_at_birth','F','Female','source','https://example.test',"
                    "'sex','patient',0,'accepted','{\"row\":1}',0)"
                )
            common = {
                "snapshot_db": snapshot,
                "public_db": root / "missing-public.sqlite",
                "controlled_db": root / "missing-controlled.sqlite",
                "clinical_db": clinical,
                "replace": True,
            }
            participants.build_database(compact, **common)
            participants.build_database(embedded, include_clinical_values=True, **common)
            with sqlite3.connect(compact) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM participant_clinical_values").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM participant_inventory_meta "
                        "WHERE key='clinical_values_storage'"
                    ).fetchone()[0],
                    "clinical_metadata_detail_artifact",
                )
            with sqlite3.connect(embedded) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM participant_clinical_values").fetchone()[0], 1)

    def test_participant_provenance_moves_to_joinable_audit_companion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "participant_inventory.sqlite"
            audit_database = root / "participant_inventory_audit.sqlite"
            participant_key = participants.participant_key("Collection", "TEST", "P1")
            with sqlite3.connect(database) as conn:
                conn.executescript(participants.SCHEMA)
                conn.execute(
                    "INSERT INTO participants VALUES "
                    "(?,'Collection','TEST','P1','dataset_scoped','single_namespace',"
                    "'source_identifier','not_asserted')",
                    (participant_key,),
                )
                conn.execute(
                    "INSERT INTO participant_identifiers VALUES "
                    "('pid1',?,'tcia_wordpress','tcia_dataset:TEST','P1','P1',"
                    "'source_identifier','{\"source_artifact\":\"clinical_metadata\"}')",
                    (participant_key,),
                )
                conn.execute(
                    "INSERT INTO participant_inventory_meta VALUES ('schema_version',?)",
                    (str(participants.SCHEMA_VERSION),),
                )
            audit.split_database(
                database,
                audit_database,
                artifact="participant_inventory",
                replace=True,
            )
            validation = participants.validate_database(database)
            self.assertTrue(validation["ok"], validation["errors"])
            with sqlite3.connect(database) as conn:
                self.assertEqual(
                    conn.execute("SELECT provenance_json FROM participant_identifiers").fetchone()[0],
                    "{}",
                )
            with sqlite3.connect(audit_database) as conn:
                row = conn.execute(
                    "SELECT entity_table, entity_id, field_name, payload_json "
                    "FROM agent_entity_payloads"
                ).fetchone()
                self.assertEqual(row[:3], ("participant_identifiers", "pid1", "provenance_json"))
                self.assertIn("clinical_metadata", row[3])

    def test_compact_v3_projects_without_mutating_source_and_reconstructs_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "assembly.sqlite"
            research = root / "research.sqlite"
            audit_database = root / "audit.sqlite"
            with sqlite3.connect(source) as conn:
                conn.executescript(public.SCHEMA)
                public.insert_vocab(conn)
                for index in (1, 2):
                    asset_id = f"asset-{index}"
                    public.insert_asset(
                        conn,
                        {
                            "asset_id": asset_id,
                            "dataset_type": "Collection",
                            "short_title": "TEST",
                            "download_id": str(index),
                            "subject_id": f"P{index}",
                            "subject_id_namespace": "tcia_dataset:TEST",
                            "participant_link_status": "unavailable",
                            "asset_granularity": "file",
                            "asset_name": f"image-{index}.nii.gz",
                            "file_name": f"image-{index}.nii.gz",
                            "package_path": f"P{index}/image-{index}.nii.gz",
                            "file_format": "NIFTI",
                            "media_kind": "image_volume",
                            "spatial_dimensionality": "3D",
                            "temporal_dimensionality": "static",
                            "imaging_domain": "radiology",
                            "modality": "MR",
                            "object_role": "source_image",
                            "representation_provenance_class": "submitted_original",
                            "source_system": "tcia_aspera",
                            "source_record_id": f"row-{index}",
                            "source_url": "https://example.test/package",
                            "raw_values_json": '{"source_column":"raw"}',
                            "provenance_json": '{"source_artifact":"nifti_metadata"}',
                            "quality_flag_json": "{}",
                        },
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM public_non_dicom_assets WHERE asset_id=?",
                            (asset_id,),
                        ).fetchone()[0],
                        1,
                    )
                    public.merge_image_metadata(
                        conn,
                        asset_id,
                        {"modality": "MR", "file_format": "NIFTI"},
                        value_role="source_raw",
                        source_kind="nifti_metadata",
                        source_locator="agent_nifti_files",
                        inference_method="direct",
                        confidence="high",
                        priority=100,
                        short_title="TEST",
                        assume_new=True,
                    )
                conn.execute("INSERT INTO artifact_meta VALUES ('schema_version', '5')")
                row = conn.execute(
                    "SELECT asset_id, field_provenance_json "
                    "FROM public_non_dicom_image_metadata ORDER BY asset_id LIMIT 1"
                ).fetchone()
                numeric_source = __import__("json").loads(row[1])
                old_label, source_value = next(iter(numeric_source["_sources"].items()))
                numeric_source["_sources"] = {
                    "1": source_value,
                    "unused-source": {"source_kind": "supplemental"},
                }
                for key, decision in numeric_source.items():
                    if key != "_sources" and decision.get("source_id") == old_label:
                        decision["source_id"] = 1
                conn.execute(
                    "UPDATE public_non_dicom_image_metadata "
                    "SET field_provenance_json=? WHERE asset_id=?",
                    (__import__("json").dumps(numeric_source, sort_keys=True), row[0]),
                )
                original = {
                    row[0]: __import__("json").loads(row[1])
                    for row in conn.execute(
                        "SELECT asset_id, field_provenance_json "
                        "FROM public_non_dicom_image_metadata"
                    )
                }
                conn.commit()
            source_size = source.stat().st_size
            result = audit.project_database_v3(
                source,
                research,
                audit_database,
                artifact="public_non_dicom",
                replace=True,
            )
            self.assertEqual(result["schema_version"], 3)
            self.assertTrue(result["audit"]["audit_validation"]["ok"])
            self.assertTrue(public.validate_database(research)["ok"])
            self.assertEqual(source.stat().st_size, source_size)
            with sqlite3.connect(source) as conn:
                self.assertNotEqual(
                    conn.execute(
                        "SELECT field_provenance_json "
                        "FROM public_non_dicom_image_metadata LIMIT 1"
                    ).fetchone()[0],
                    "{}",
                )
            with sqlite3.connect(research) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT field_provenance_json "
                        "FROM public_non_dicom_image_metadata LIMIT 1"
                    ).fetchone()[0],
                    "{}",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT SUM(length(field_provenance_json)) "
                        "FROM public_non_dicom_image_metadata"
                    ).fetchone()[0],
                    4,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM artifact_meta WHERE key='audit_schema_version'"
                    ).fetchone()[0],
                    "3",
                )
            with sqlite3.connect(audit_database) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM audit_meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    "3",
                )
                for asset_id, expected in original.items():
                    self.assertEqual(
                        audit.reconstruct_field_provenance(
                            conn,
                            entity_table="public_non_dicom_image_metadata",
                            entity_id=asset_id,
                        ),
                        expected,
                    )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type='index' AND name='idx_entity_payloads_entity'"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM provenance_decision_payloads"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM field_provenance").fetchone()[0],
                    4,
                )
                self.assertTrue(
                    all(
                        row[0] == 32
                        for row in conn.execute(
                            "SELECT length(payload_sha256) FROM payloads"
                        )
                    )
                )
            reconstruction = audit.verify_field_provenance_reconstruction(
                source, audit_database, sample_size=2
            )
            self.assertTrue(reconstruction["ok"], reconstruction["errors"])
            self.assertEqual(reconstruction["sampled_documents"], 2)
            manifest = audit.build_audit_manifest(
                audit_database, None, artifact="public_non_dicom"
            )
            self.assertEqual(manifest["schema_version"], 3)
            materialized = root / "materialized.sqlite"
            json_fields = audit.CONFIGS["public_non_dicom"]["json_fields"]
            asset_identifier, asset_fields = json_fields["public_non_dicom_assets"]
            original_asset_fields = dict(asset_fields)
            json_fields["public_non_dicom_assets"] = (
                asset_identifier,
                {**asset_fields, "future_geometry_json": "{}"},
            )
            json_fields["future_geometry_assessments"] = (
                "geometry_assessment_id",
                {"details_json": "{}"},
            )
            try:
                materialization = audit.materialize_assembly_from_companions(
                    research,
                    audit_database,
                    materialized,
                    replace=True,
                )
            finally:
                json_fields["public_non_dicom_assets"] = (
                    asset_identifier,
                    original_asset_fields,
                )
                del json_fields["future_geometry_assessments"]
            self.assertEqual(materialization["integrity_check"], "ok")
            self.assertEqual(
                materialization["reconstructed_field_provenance"], 2
            )
            with sqlite3.connect(materialized) as conn:
                self.assertEqual(
                    {
                        row[0]: __import__("json").loads(row[1])
                        for row in conn.execute(
                            "SELECT asset_id, field_provenance_json "
                            "FROM public_non_dicom_image_metadata"
                        )
                    },
                    original,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT raw_values_json, provenance_json "
                        "FROM public_non_dicom_assets ORDER BY asset_id LIMIT 1"
                    ).fetchone(),
                    ('{"source_column":"raw"}', '{"source_artifact":"nifti_metadata"}'),
                )


if __name__ == "__main__":
    unittest.main()
