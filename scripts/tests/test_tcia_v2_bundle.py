import importlib.util
import gzip
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


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
            self.assertEqual(first["release_channel"], "stable")
            self.assertEqual(first["release_tag"], "tcia-metadata-v2-latest")

    def test_streamlined_candidate_has_nine_payloads_and_one_bundle_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            payload = BUNDLE.build_bundle_manifest(
                root,
                release_contract=BUNDLE.STREAMLINED_CANDIDATE_CONTRACT,
                release_channel="candidate",
                release_tag="tcia-metadata-v2-streamlined-candidate",
            )
            self.assertEqual(payload["asset_count"], 9)
            self.assertEqual(payload["release_channel"], "candidate")
            self.assertEqual(set(payload["components"]), set(BUNDLE.STREAMLINED_COMPONENTS))
            self.assertNotIn("nifti_metadata.sqlite.gz", payload["assets"])
            self.assertNotIn("pathology_metadata.sqlite.gz", payload["assets"])
            self.assertNotIn("clinical_qc_manual_review.csv", payload["assets"])
            self.assertNotIn("tcia_snapshot_manifest.json", payload["assets"])
            manifest = root / BUNDLE.BUNDLE_MANIFEST_ASSET
            manifest.write_text(json.dumps(payload))
            result = BUNDLE.validate_bundle(root, manifest)
            self.assertTrue(result["ok"], result["errors"])
            self.assertEqual(BUNDLE.validate_manifest_contract(payload), [])
            candidate = root / "candidate"
            materialized = BUNDLE.materialize_bundle(root, manifest, candidate)
            self.assertEqual(materialized["asset_count"], 10)
            self.assertEqual(
                sorted(path.name for path in candidate.iterdir()),
                sorted([*payload["assets"], BUNDLE.BUNDLE_MANIFEST_ASSET]),
            )

    def test_streamlined_contract_is_publishable_on_stable_channel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            payload = BUNDLE.build_bundle_manifest(
                root,
                release_contract=BUNDLE.STREAMLINED_RELEASE_CONTRACT,
                release_channel="stable",
                release_tag="tcia-metadata-v2-latest",
            )
            self.assertEqual(payload["asset_count"], 9)
            self.assertEqual(payload["release_contract"], "streamlined")
            self.assertEqual(payload["release_channel"], "stable")
            self.assertEqual(set(payload["components"]), set(BUNDLE.STREAMLINED_COMPONENTS))
            manifest = root / BUNDLE.BUNDLE_MANIFEST_ASSET
            manifest.write_text(json.dumps(payload))
            self.assertTrue(BUNDLE.validate_bundle(root, manifest)["ok"])

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

    def test_research_core_defers_file_grain_and_audit_artifacts(self):
        core = BUNDLE.assets_for_profile("research_core")
        self.assertIn("participant_inventory.sqlite.gz", core)
        self.assertIn("tcia_snapshot.sqlite.gz", core)
        self.assertNotIn("public_non_dicom_metadata.sqlite.gz", core)
        self.assertNotIn("public_non_dicom_audit.sqlite.gz", core)
        detail = BUNDLE.assets_for_profile("research_detail")
        self.assertIn("public_non_dicom_metadata.sqlite.gz", detail)
        self.assertIn("participant_inventory.sqlite.gz", detail)
        audit = BUNDLE.assets_for_profile("audit_support", include_dependencies=False)
        self.assertIn("public_non_dicom_audit.sqlite.gz", audit)
        self.assertIn("participant_inventory_audit.sqlite.gz", audit)

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
            self.assertEqual(len(result["assets"]), 7)

    def test_selected_v2_baseline_assets_are_manifest_and_release_pinned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            payload = BUNDLE.build_bundle_manifest(
                root,
                release_contract=BUNDLE.STREAMLINED_RELEASE_CONTRACT,
            )
            manifest_path = root / BUNDLE.BUNDLE_MANIFEST_ASSET
            manifest_path.write_text(json.dumps(payload))
            names = [
                "public_non_dicom_metadata.sqlite.gz",
                "public_non_dicom_audit.sqlite.gz",
            ]
            release_path = root / "release.json"
            release_path.write_text(
                json.dumps(
                    {
                        "tag_name": BUNDLE.DEFAULT_RELEASE_TAG,
                        "assets": [
                            {
                                "name": name,
                                "digest": "sha256:" + BUNDLE.file_sha256(root / name),
                            }
                            for name in names
                        ],
                    }
                )
            )
            result = BUNDLE.validate_selected_bundle_assets(
                root,
                manifest_path,
                names,
                release_json_path=release_path,
            )
            self.assertTrue(result["ok"], result["errors"])

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

    def test_download_to_path_streams_and_validates(self):
        class Response(io.BytesIO):
            def __init__(self, body: bytes):
                super().__init__(body)
                self.read_sizes: list[int] = []

            def read(self, size: int = -1) -> bytes:
                self.read_sizes.append(size)
                return super().read(size)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        body = b"streamed asset" * 200_000
        response = Response(body)
        details = {
            "bytes": len(body),
            "sha256": BUNDLE.hashlib.sha256(body).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            BUNDLE.urllib.request, "urlopen", return_value=response
        ):
            destination = Path(temporary) / "asset.gz"
            BUNDLE.download_to_path("https://example.invalid/asset.gz", destination, details, "asset.gz")
            self.assertEqual(destination.read_bytes(), body)
        self.assertTrue(response.read_sizes)
        self.assertEqual(set(response.read_sizes), {1024 * 1024})

    def test_download_to_path_removes_invalid_asset(self):
        body = b"invalid"
        details = {"bytes": len(body), "sha256": "0" * 64}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            BUNDLE.urllib.request, "urlopen", return_value=io.BytesIO(body)
        ):
            destination = Path(temporary) / "asset.gz"
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                BUNDLE.download_to_path(
                    "https://example.invalid/asset.gz", destination, details, "asset.gz"
                )
            self.assertFalse(destination.exists())

    def test_install_research_core_validates_and_installs_databases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            install_dir = root / "installed"
            assets.mkdir()
            self.create_bundle_files(assets)
            for component in ("snapshot", "participant_inventory"):
                details = BUNDLE.COMPONENTS[component]
                sqlite_path = root / f"{component}.sqlite"
                with closing(sqlite3.connect(sqlite_path)) as conn:
                    conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
                    conn.execute("INSERT INTO metadata VALUES ('component', ?)", (component,))
                    conn.commit()
                raw = sqlite_path.read_bytes()
                compressed = gzip.compress(raw)
                (assets / details["database"]).write_bytes(compressed)
                manifest = json.loads((assets / details["manifest"]).read_text())
                manifest["sqlite_sha256"] = BUNDLE.hashlib.sha256(raw).hexdigest()
                manifest["gzip_sha256"] = BUNDLE.hashlib.sha256(compressed).hexdigest()
                (assets / details["manifest"]).write_text(json.dumps(manifest))
            payload = BUNDLE.build_bundle_manifest(assets)
            bundle_body = json.dumps(payload).encode()

            def fake_fetch(url: str) -> bytes:
                name = url.rsplit("/", 1)[-1]
                if name == BUNDLE.BUNDLE_MANIFEST_ASSET:
                    return bundle_body
                return (assets / name).read_bytes()

            def fake_download(
                url: str, destination: Path, details: dict, asset: str
            ) -> None:
                destination.write_bytes((assets / url.rsplit("/", 1)[-1]).read_bytes())
                self.assertEqual(destination.stat().st_size, details["bytes"])
                self.assertEqual(BUNDLE.file_sha256(destination), details["sha256"])

            with mock.patch.object(BUNDLE, "fetch_bytes", side_effect=fake_fetch), mock.patch.object(
                BUNDLE, "download_to_path", side_effect=fake_download
            ), mock.patch.object(
                BUNDLE.gzip, "decompress", side_effect=AssertionError("must stream")
            ):
                result = BUNDLE.install_bundle(install_dir=install_dir)
            self.assertEqual(result["status"], "downloaded")
            self.assertTrue((install_dir / "tcia_snapshot.sqlite").is_file())
            self.assertTrue((install_dir / "participant_inventory.sqlite").is_file())
            self.assertTrue((install_dir / BUNDLE.BUNDLE_MANIFEST_ASSET).is_file())
            self.assertTrue((install_dir / BUNDLE.INSTALL_STATE_ASSET).is_file())
            with closing(sqlite3.connect(install_dir / "participant_inventory.sqlite")) as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_install_streamlined_core_uses_inline_component_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            install_dir = root / "installed"
            assets.mkdir()
            self.create_bundle_files(assets)
            for component in ("snapshot", "participant_inventory"):
                details = BUNDLE.COMPONENTS[component]
                sqlite_path = root / f"{component}.sqlite"
                with closing(sqlite3.connect(sqlite_path)) as conn:
                    conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
                    conn.execute("INSERT INTO metadata VALUES ('component', ?)", (component,))
                raw = sqlite_path.read_bytes()
                compressed = gzip.compress(raw)
                (assets / details["database"]).write_bytes(compressed)
                manifest = json.loads((assets / details["manifest"]).read_text())
                manifest["sqlite_sha256"] = BUNDLE.hashlib.sha256(raw).hexdigest()
                manifest["gzip_sha256"] = BUNDLE.hashlib.sha256(compressed).hexdigest()
                (assets / details["manifest"]).write_text(json.dumps(manifest))
            payload = BUNDLE.build_bundle_manifest(
                assets,
                release_contract=BUNDLE.STREAMLINED_CANDIDATE_CONTRACT,
                release_channel="candidate",
                release_tag="tcia-metadata-v2-streamlined-candidate",
            )
            bundle_body = json.dumps(payload).encode()

            def fake_fetch(url: str) -> bytes:
                name = url.rsplit("/", 1)[-1]
                if name == BUNDLE.BUNDLE_MANIFEST_ASSET:
                    return bundle_body
                return (assets / name).read_bytes()

            def fake_download(
                url: str, destination: Path, details: dict, asset: str
            ) -> None:
                destination.write_bytes((assets / url.rsplit("/", 1)[-1]).read_bytes())
                self.assertEqual(destination.stat().st_size, details["bytes"])
                self.assertEqual(BUNDLE.file_sha256(destination), details["sha256"])

            with mock.patch.object(BUNDLE, "fetch_bytes", side_effect=fake_fetch), mock.patch.object(
                BUNDLE, "download_to_path", side_effect=fake_download
            ), mock.patch.object(
                BUNDLE.gzip, "decompress", side_effect=AssertionError("must stream")
            ):
                result = BUNDLE.install_bundle(
                    tag="tcia-metadata-v2-streamlined-candidate",
                    install_dir=install_dir,
                )
            self.assertEqual(result["status"], "downloaded")
            self.assertTrue((install_dir / "tcia_snapshot.sqlite").is_file())
            self.assertTrue((install_dir / "participant_inventory.sqlite").is_file())
            self.assertFalse((install_dir / "tcia_snapshot_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
