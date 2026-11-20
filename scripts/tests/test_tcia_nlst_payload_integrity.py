import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("nlst_public", SCRIPTS / "tcia_public_non_dicom_metadata.py")
public = importlib.util.module_from_spec(spec)
spec.loader.exec_module(public)


class KnownPayloadIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(public.SCHEMA)
        public.insert_vocab(self.conn)
        self.evidence = json.loads((public.SKILL_ROOT / "references/nlst_known_payload_integrity_v1.json").read_text())

    def tearDown(self):
        self.conn.close()

    def insert(self, asset_id, path, dataset="NLST-New-lesion-LongCT", checksum="", algorithm=""):
        self.conn.execute(
            "INSERT INTO public_non_dicom_assets "
            "(asset_id,dataset_type,short_title,asset_granularity,file_format,media_kind,"
            "imaging_domain,object_role,representation_provenance_class,source_system,"
            "source_record_id,package_path,checksum,checksum_algorithm,geometry_status,quality_flag_json) "
            "VALUES (?, 'Analysis Result', ?, 'file', 'NIfTI', 'image_volume', 'radiology', "
            "'source_image', 'derived_asset', 'tcia_wordpress', ?, ?, ?, ?, 'checked_grid_geometry', ?) ",
            (asset_id, dataset, asset_id, path, checksum, algorithm, '{"existing_flag": true}'),
        )

    def flags(self, asset_id):
        return json.loads(self.conn.execute("SELECT quality_flag_json FROM agent_public_non_dicom_assets WHERE asset_id=?", (asset_id,)).fetchone()[0])

    def test_exact_paths_only_preserve_geometry_and_existing_flags(self):
        self.assertEqual(len(self.evidence["files"]), 13)
        self.assertEqual(len({r["package_path"] for r in self.evidence["files"]}), 13)
        for i, item in enumerate(self.evidence["files"]):
            self.assertEqual(len(item["sha256"]), 64)
            self.assertGreater(item["missing_uncompressed_bytes"], 0)
            self.insert(str(i), "/" + item["package_path"])
        path = self.evidence["files"][0]["package_path"]
        self.insert("same_name_other_path", path.replace("/2-59865.nii.gz", "/resampled/2-59865.nii.gz"))
        self.insert("other_dataset", path, dataset="other")
        self.assertEqual(public.apply_known_payload_integrity(self.conn), 13)
        for i in range(13):
            flags = self.flags(str(i))
            self.assertTrue(flags["existing_flag"])
            self.assertIsNone(flags["reviewed_payload_integrity"]["current_bytes_match_audit"])
            self.assertEqual(flags["reviewed_payload_integrity"]["status"], "known_truncated_at_audit")
        for name in ["same_name_other_path", "other_dataset"]:
            self.assertEqual(self.flags(name), {"existing_flag": True})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM public_non_dicom_assets WHERE geometry_status='checked_grid_geometry'").fetchone()[0], 15)
        self.assertEqual(public.apply_known_payload_integrity(self.conn), 0)

    def test_replacement_bytes_require_recheck_and_refresh_old_flag(self):
        item = self.evidence["files"][0]
        self.insert("match", item["package_path"], checksum=item["sha256"], algorithm="SHA-256")
        public.apply_known_payload_integrity(self.conn)
        self.assertTrue(self.flags("match")["reviewed_payload_integrity"]["current_bytes_match_audit"])
        self.conn.execute("UPDATE public_non_dicom_assets SET checksum=?", ("0" * 64,))
        public.apply_known_payload_integrity(self.conn)
        flag = self.flags("match")["reviewed_payload_integrity"]
        self.assertEqual(flag["status"], "source_changed_requires_recheck")
        self.assertFalse(flag["current_bytes_match_audit"])
        self.conn.execute("UPDATE public_non_dicom_assets SET package_path='unrelated.nii.gz'")
        public.apply_known_payload_integrity(self.conn)
        self.assertEqual(self.flags("match"), {"existing_flag": True})


if __name__ == "__main__":
    unittest.main()
