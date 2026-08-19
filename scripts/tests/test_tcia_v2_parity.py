import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
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
parity = load("tcia_v2_parity")
artifact_model = load("tcia_artifact_model")


class V2ParityTests(unittest.TestCase):
    def test_projection_can_pass_while_retirement_remains_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nifti = root / "nifti.sqlite"
            pathology = root / "pathology.sqlite"
            unified = root / "public.sqlite"
            with closing(sqlite3.connect(nifti)) as conn:
                conn.execute("CREATE TABLE files (short_title TEXT, radiology_id TEXT)")
                conn.execute("INSERT INTO files VALUES ('TEST-NIFTI', 'r1')")
                conn.execute("CREATE VIEW agent_nifti_files AS SELECT * FROM files")
                for table in {
                    value
                    for tables in parity.SPECIALIZED_CAPABILITIES["nifti"].values()
                    for value in tables
                }:
                    conn.execute(f'CREATE TABLE "{table}" (value TEXT)')
                conn.commit()
            with closing(sqlite3.connect(pathology)) as conn:
                conn.execute(
                    "CREATE TABLE objects (non_dicom_file_id TEXT, image_format TEXT, "
                    "file_ext TEXT, is_metadata INTEGER)"
                )
                conn.execute("INSERT INTO objects VALUES ('p1', 'SVS', '.svs', 0)")
                conn.execute(
                    "CREATE VIEW agent_pathology_file_objects AS SELECT * FROM objects"
                )
                for table in {
                    value
                    for tables in parity.SPECIALIZED_CAPABILITIES["pathology"].values()
                    for value in tables
                }:
                    conn.execute(f'CREATE TABLE "{table}" (value TEXT)')
                conn.commit()
            with closing(sqlite3.connect(unified)) as conn:
                conn.executescript(public.SCHEMA)
                public.insert_vocab(conn)
                for asset_id, title, source_record, file_format, domain in (
                    (
                        artifact_model.stable_id("asset", "nifti", "TEST-NIFTI", "r1"),
                        "TEST-NIFTI",
                        "r1",
                        "NIFTI",
                        "radiology",
                    ),
                    (
                        artifact_model.stable_id("asset", "pathology_package", "p1"),
                        "TEST-PATH",
                        "p1",
                        "SVS",
                        "pathology",
                    ),
                ):
                    public.insert_asset(
                        conn,
                        {
                            "asset_id": asset_id,
                            "dataset_type": "Collection",
                            "short_title": title,
                            "participant_link_status": "unavailable",
                            "asset_granularity": "file",
                            "asset_name": source_record,
                            "file_name": source_record,
                            "package_path": source_record,
                            "file_format": file_format,
                            "media_kind": "image_volume" if domain == "radiology" else "whole_slide_image",
                            "spatial_dimensionality": "3D" if domain == "radiology" else "2D",
                            "temporal_dimensionality": "static",
                            "imaging_domain": domain,
                            "modality": "MR" if domain == "radiology" else "SM",
                            "object_role": "source_image",
                            "representation_provenance_class": "submitted_original",
                            "source_system": "tcia_aspera",
                            "source_record_id": source_record,
                            "source_url": "https://example.test",
                            "raw_values_json": "{}",
                            "provenance_json": "{}",
                            "quality_flag_json": "{}",
                        },
                    )
                conn.commit()
            result = parity.analyze_parity(
                public_db=unified,
                nifti_db=nifti,
                pathology_db=pathology,
            )
            self.assertTrue(result["projection_ok"], result)
            self.assertFalse(result["retirement_ready"])
            self.assertEqual(
                result["decision"],
                "retain_legacy_detail_assets_until_specialized_capabilities_are_checkpointed",
            )


if __name__ == "__main__":
    unittest.main()
