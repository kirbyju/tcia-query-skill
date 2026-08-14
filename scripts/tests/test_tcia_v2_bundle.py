import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPT_DIR / "tcia_v2_bundle.py"
SPEC = importlib.util.spec_from_file_location("tcia_v2_bundle", SCRIPT)
BUNDLE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(BUNDLE)


class V2BundleTests(unittest.TestCase):
    def create_bundle_files(self, root: Path) -> None:
        web_exports = {}
        for name in BUNDLE.WEB_EXPORT_ASSETS:
            path = root / name
            path.write_bytes((name + "\n").encode())
            web_exports[name] = {"sha256": BUNDLE.file_sha256(path)}
        for component, details in BUNDLE.COMPONENTS.items():
            database = root / details["database"]
            database.write_bytes((component + " database").encode())
            manifest = {
                "schema_version": 3,
                "sqlite_sha256": "sqlite-" + component,
                "gzip_sha256": BUNDLE.file_sha256(database),
                "release_fingerprint": "fingerprint-" + component,
            }
            if component == "snapshot":
                manifest["web_exports"] = web_exports
            (root / details["manifest"]).write_text(json.dumps(manifest))
        for name in BUNDLE.EXTRA_ASSETS:
            (root / name).write_text("review\n")

    def test_complete_bundle_manifest_is_stable_and_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            first = BUNDLE.build_bundle_manifest(root)
            second = BUNDLE.build_bundle_manifest(root)
            self.assertEqual(first["release_fingerprint"], second["release_fingerprint"])
            self.assertEqual(first["asset_count"], 23)
            manifest = root / BUNDLE.BUNDLE_MANIFEST_ASSET
            manifest.write_text(json.dumps(first))
            result = BUNDLE.validate_bundle(root, manifest)
            self.assertTrue(result["ok"], result["errors"])

    def test_producer_version_changes_bundle_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            first = BUNDLE.build_bundle_manifest(root, producer_commit="abc", producer_skill_version="1")
            second = BUNDLE.build_bundle_manifest(root, producer_commit="def", producer_skill_version="2")
            self.assertNotEqual(first["release_fingerprint"], second["release_fingerprint"])

    def test_changed_asset_fails_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            payload = BUNDLE.build_bundle_manifest(root)
            manifest = root / BUNDLE.BUNDLE_MANIFEST_ASSET
            manifest.write_text(json.dumps(payload))
            (root / "agent_datasets.jsonl").write_text("changed\n")
            result = BUNDLE.validate_bundle(root, manifest)
            self.assertFalse(result["ok"])
            self.assertTrue(any("agent_datasets.jsonl" in error for error in result["errors"]))

    def test_exports_command_surface_uses_all_eight_exports(self):
        self.assertEqual(len(BUNDLE.WEB_EXPORT_ASSETS), 8)
        self.assertIn("agent_dataset_versions.jsonl", BUNDLE.expected_payload_assets())
        self.assertIn("agent_dataset_v1_releases.jsonl.gz", BUNDLE.expected_payload_assets())

    def test_v2_built_participant_inventory_assets_have_v2_provenance(self):
        self.assertEqual(BUNDLE.asset_source("participant_inventory.sqlite.gz"), "v2_build")
        self.assertEqual(BUNDLE.asset_source("participant_inventory_manifest.json"), "v2_build")

    def test_source_release_copy_is_digest_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            assets = []
            for index, name in enumerate(BUNDLE.source_copy_assets(), start=1):
                assets.append({
                    "id": index,
                    "name": name,
                    "digest": "sha256:" + BUNDLE.file_sha256(root / name),
                })
            release = {
                "id": 42,
                "tag_name": BUNDLE.DEFAULT_SOURCE_TAG,
                "target_commitish": "main",
                "published_at": "2026-05-07T00:10:47Z",
                "updated_at": "2026-08-14T12:15:00Z",
                "assets": assets,
            }
            release_path = root / "source_release.json"
            release_path.write_text(json.dumps(release))
            result = BUNDLE.validate_source_release(root, release_path)
            self.assertEqual(result["release_id"], 42)
            self.assertEqual(len(result["assets"]), 11)

    def test_published_release_digests_match_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            payload = BUNDLE.build_bundle_manifest(root)
            manifest_path = root / BUNDLE.BUNDLE_MANIFEST_ASSET
            manifest_path.write_text(json.dumps(payload))
            assets = [
                {
                    "name": name,
                    "size": details["bytes"],
                    "digest": "sha256:" + details["sha256"],
                }
                for name, details in payload["assets"].items()
            ]
            assets.append({
                "name": BUNDLE.BUNDLE_MANIFEST_ASSET,
                "size": manifest_path.stat().st_size,
                "digest": "sha256:" + BUNDLE.file_sha256(manifest_path),
            })
            release_path = root / "published_release.json"
            release_path.write_text(json.dumps({"tag_name": BUNDLE.DEFAULT_RELEASE_TAG, "assets": assets}))
            result = BUNDLE.validate_published_release(manifest_path, release_path)
            self.assertTrue(result["ok"], result["errors"])
            self.assertEqual(result["asset_count"], 24)

    def test_changed_assets_excludes_unchanged_large_sidecars(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            previous = BUNDLE.build_bundle_manifest(root)
            previous_path = root / "previous.json"
            previous_path.write_text(json.dumps(previous))
            (root / "agent_datasets.jsonl").write_text("new datasets\n")
            snapshot_manifest = json.loads((root / "tcia_snapshot_manifest.json").read_text())
            snapshot_manifest["web_exports"]["agent_datasets.jsonl"]["sha256"] = BUNDLE.file_sha256(
                root / "agent_datasets.jsonl"
            )
            (root / "tcia_snapshot_manifest.json").write_text(json.dumps(snapshot_manifest))
            snapshot_manifest["gzip_sha256"] = BUNDLE.file_sha256(root / "tcia_snapshot.sqlite.gz")
            (root / "tcia_snapshot_manifest.json").write_text(json.dumps(snapshot_manifest))
            current = BUNDLE.build_bundle_manifest(root)
            current_path = root / "current.json"
            current_path.write_text(json.dumps(current))
            changed = BUNDLE.changed_payload_assets(current_path, previous_path)
            self.assertIn("agent_datasets.jsonl", changed)
            self.assertIn("tcia_snapshot_manifest.json", changed)
            self.assertNotIn("pathology_metadata.sqlite.gz", changed)


if __name__ == "__main__":
    unittest.main()
