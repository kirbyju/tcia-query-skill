import importlib.util
import gzip
import io
import json
import sqlite3
import shutil
import subprocess
import sys
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
            sqlite_path = root / f".{component}.sqlite"
            with closing(sqlite3.connect(sqlite_path)) as conn:
                conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO metadata VALUES ('component', ?)", (component,))
                conn.commit()
            raw = sqlite_path.read_bytes()
            database.write_bytes(gzip.compress(raw, mtime=0))
            sqlite_path.unlink()
            manifest = {
                "schema_version": 3,
                "sqlite_sha256": BUNDLE.hashlib.sha256(raw).hexdigest(),
                "gzip_sha256": BUNDLE.file_sha256(database),
                "release_fingerprint": "fingerprint-" + component,
            }
            if component == "snapshot":
                manifest["web_exports"] = web_exports
            (root / details["manifest"]).write_text(json.dumps(manifest))
        for name in BUNDLE.EXTRA_ASSETS:
            (root / name).write_text("review\n")

    def create_installable_bundle(self, root: Path) -> tuple[Path, dict]:
        assets = root / "assets"
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
            manifest_path = assets / details["manifest"]
            manifest = json.loads(manifest_path.read_text())
            manifest["sqlite_sha256"] = BUNDLE.hashlib.sha256(raw).hexdigest()
            manifest["gzip_sha256"] = BUNDLE.hashlib.sha256(compressed).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
        return assets, BUNDLE.build_bundle_manifest(assets)

    def install_from_assets(
        self, assets: Path, payload: dict, install_dir: Path, *, profile: str = "research_core"
    ) -> dict:
        bundle_body = json.dumps(payload).encode()

        def fake_fetch(url: str, **_kwargs) -> bytes:
            name = url.rsplit("/", 1)[-1]
            return bundle_body if name == BUNDLE.BUNDLE_MANIFEST_ASSET else (assets / name).read_bytes()

        def fake_download(
            url: str, destination: Path, details: dict, asset: str, **_kwargs
        ) -> None:
            destination.write_bytes((assets / url.rsplit("/", 1)[-1]).read_bytes())
            self.assertEqual(destination.stat().st_size, details["bytes"])
            self.assertEqual(BUNDLE.file_sha256(destination), details["sha256"])

        with mock.patch.object(BUNDLE, "fetch_bytes", side_effect=fake_fetch), mock.patch.object(
            BUNDLE, "download_to_path", side_effect=fake_download
        ):
            return BUNDLE.install_bundle(install_dir=install_dir, profile=profile)

    def test_complete_bundle_manifest_is_stable_and_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            first = BUNDLE.build_bundle_manifest(root)
            second = BUNDLE.build_bundle_manifest(root)
            self.assertEqual(first["release_fingerprint"], second["release_fingerprint"])
            self.assertEqual(first["asset_count"], 25)
            manifest = root / BUNDLE.BUNDLE_MANIFEST_ASSET
            manifest.write_text(json.dumps(first))
            result = BUNDLE.validate_bundle(root, manifest)
            self.assertTrue(result["ok"], result["errors"])
            self.assertEqual(first["release_channel"], "stable")
            self.assertEqual(first["release_tag"], "tcia-metadata-v2-latest")

    def test_correction_summary_is_compact_and_fingerprinted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            manifest_path = root / BUNDLE.COMPONENTS["correction_registry"]["manifest"]
            component = json.loads(manifest_path.read_text())
            component["decision_set_summary"] = {
                "sha256": "a" * 64,
                "status": "verified_current",
                "counts": {"active_revisions": 3, "failed_validations": 0},
            }
            component["source_status"] = {"snapshot": "live"}
            manifest_path.write_text(json.dumps(component))
            first = BUNDLE.build_bundle_manifest(root)
            self.assertEqual(
                first["decision_sets"]["correction_registry"],
                component["decision_set_summary"],
            )
            self.assertEqual(
                first["source_health"]["components"]["correction_registry"]["status"],
                "healthy",
            )
            component["decision_set_summary"]["counts"]["active_revisions"] = 4
            manifest_path.write_text(json.dumps(component))
            second = BUNDLE.build_bundle_manifest(root)
            self.assertNotEqual(first["release_fingerprint"], second["release_fingerprint"])

    def test_schema_two_manifest_remains_install_contract_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            payload = BUNDLE.build_bundle_manifest(root)
            payload["schema_version"] = 2
            payload.pop("source_health")
            payload.pop("decision_sets")
            correction = payload["components"].pop("correction_registry")
            for name in (correction["database_asset"], correction["manifest_asset"]):
                payload["assets"].pop(name)
            payload["asset_count"] = len(payload["assets"])
            for profile in BUNDLE.PROFILE_ORDER:
                payload["profiles"][profile]["assets"] = BUNDLE.assets_for_profile_schema(
                    profile, 2, payload["release_contract"]
                )
            fingerprint_payload = {
                "artifact": payload["artifact"],
                "schema_version": 2,
                "release_channel": payload["release_channel"],
                "release_tag": payload["release_tag"],
                "release_contract": payload["release_contract"],
                "source": {
                    "repository": payload["source"]["repository"],
                    "release_tag": payload["source"]["release_tag"],
                },
                "producer": payload["producer"],
                "assets": {
                    name: details["sha256"]
                    for name, details in sorted(payload["assets"].items())
                },
            }
            payload["release_fingerprint"] = BUNDLE.hashlib.sha256(
                BUNDLE.canonical_json(fingerprint_payload).encode()
            ).hexdigest()
            self.assertEqual(BUNDLE.validate_manifest_contract(payload), [])

    def test_schema_two_streamlined_manifest_remains_compatible_without_correction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            payload = BUNDLE.build_bundle_manifest(
                root, release_contract=BUNDLE.STREAMLINED_RELEASE_CONTRACT
            )
            payload["schema_version"] = 2
            payload.pop("source_health")
            payload.pop("decision_sets")
            payload["components"].pop("correction_registry")
            payload["assets"].pop("tcia_correction_registry.sqlite.gz")
            payload["asset_count"] = len(payload["assets"])
            for profile in BUNDLE.PROFILE_ORDER:
                payload["profiles"][profile]["assets"] = BUNDLE.assets_for_profile_schema(
                    profile, 2, payload["release_contract"]
                )
            source = payload["source"]
            fingerprint_payload = {
                "artifact": payload["artifact"], "schema_version": 2,
                "release_channel": payload["release_channel"],
                "release_tag": payload["release_tag"],
                "release_contract": payload["release_contract"],
                "source": {"repository": source["repository"], "release_tag": source["release_tag"]},
                "producer": payload["producer"],
                "assets": {name: details["sha256"] for name, details in sorted(payload["assets"].items())},
            }
            payload["release_fingerprint"] = BUNDLE.hashlib.sha256(
                BUNDLE.canonical_json(fingerprint_payload).encode()
            ).hexdigest()
            self.assertEqual(BUNDLE.validate_manifest_contract(payload), [])

    def test_streamlined_candidate_has_ten_payloads_and_one_bundle_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            payload = BUNDLE.build_bundle_manifest(
                root,
                release_contract=BUNDLE.STREAMLINED_CANDIDATE_CONTRACT,
                release_channel="candidate",
                release_tag="tcia-metadata-v2-streamlined-candidate",
            )
            self.assertEqual(payload["asset_count"], 10)
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
            self.assertEqual(materialized["asset_count"], 11)
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
            self.assertEqual(payload["asset_count"], 10)
            self.assertEqual(payload["release_contract"], "streamlined")
            self.assertEqual(payload["release_channel"], "stable")
            self.assertEqual(set(payload["components"]), set(BUNDLE.STREAMLINED_COMPONENTS))
            self.assertTrue(
                all("manifest_asset" not in item for item in payload["components"].values())
            )
            self.assertTrue(
                all("source_manifest" in item for item in payload["components"].values())
            )
            manifest = root / BUNDLE.BUNDLE_MANIFEST_ASSET
            manifest.write_text(json.dumps(payload))
            self.assertTrue(BUNDLE.validate_bundle(root, manifest)["ok"])

    def test_stable_bundle_rejects_degraded_source_without_scoped_waiver(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            snapshot_manifest_path = root / BUNDLE.COMPONENTS["snapshot"]["manifest"]
            snapshot_manifest = json.loads(snapshot_manifest_path.read_text())
            snapshot_manifest["source_status"] = {"wordpress_collections": "fallback_snapshot"}
            snapshot_manifest["warnings"] = [
                {"source": "wordpress_collections", "message": "reused prior rows"}
            ]
            snapshot_manifest_path.write_text(json.dumps(snapshot_manifest))
            with self.assertRaisesRegex(RuntimeError, "degraded or unknown authoritative sources"):
                BUNDLE.build_bundle_manifest(root)

    def test_stable_bundle_fails_closed_for_every_unknown_source_mode(self):
        for mode in ("disabled", "pending", "misspelled-live"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.create_bundle_files(root)
                clinical_path = root / BUNDLE.COMPONENTS["clinical"]["manifest"]
                clinical = json.loads(clinical_path.read_text())
                clinical["source_status"] = {"idc_clinical": mode}
                clinical_path.write_text(json.dumps(clinical))
                with self.assertRaisesRegex(RuntimeError, "unknown authoritative sources"):
                    BUNDLE.build_bundle_manifest(root)

    def test_warning_without_source_mode_fails_stable_promotion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            clinical_path = root / BUNDLE.COMPONENTS["clinical"]["manifest"]
            clinical = json.loads(clinical_path.read_text())
            clinical["warnings"] = [{"source": "new_source", "message": "not classified"}]
            clinical_path.write_text(json.dumps(clinical))
            with self.assertRaisesRegex(RuntimeError, "degraded or unknown authoritative sources"):
                BUNDLE.build_bundle_manifest(root)

    def test_unknown_source_summary_is_explicit_and_structurally_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            clinical_path = root / BUNDLE.COMPONENTS["clinical"]["manifest"]
            clinical = json.loads(clinical_path.read_text())
            clinical["source_status"] = {"idc_clinical": "pending"}
            clinical_path.write_text(json.dumps(clinical))
            waiver_path = root / "waiver.json"
            waiver_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "waivers": [
                            {
                                "source": "clinical.idc_clinical",
                                "reason": "bounded upstream transition",
                                "approved_by": "release-manager",
                                "expires_at_utc": "2099-01-01T00:00:00Z",
                            }
                        ],
                    }
                )
            )
            payload = BUNDLE.build_bundle_manifest(root, source_health_waiver=waiver_path)
            health = payload["source_health"]
            self.assertEqual(health["status"], "unknown")
            self.assertEqual(health["unknown_sources"], ["clinical.idc_clinical"])
            self.assertEqual(health["unwaived_unknown_sources"], [])
            self.assertEqual(BUNDLE.validate_manifest_contract(payload), [])
            health["unknown_sources"] = []
            self.assertTrue(
                any("unknown_sources" in error for error in BUNDLE.validate_manifest_contract(payload))
            )

    def test_scoped_unexpired_waiver_is_recorded_in_top_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            snapshot_manifest_path = root / BUNDLE.COMPONENTS["snapshot"]["manifest"]
            snapshot_manifest = json.loads(snapshot_manifest_path.read_text())
            snapshot_manifest["source_status"] = {"wordpress_collections": "fallback_snapshot"}
            snapshot_manifest_path.write_text(json.dumps(snapshot_manifest))
            waiver_path = root / "waiver.json"
            waiver_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "waivers": [
                            {
                                "source": "snapshot.wordpress_collections",
                                "reason": "upstream maintenance window",
                                "approved_by": "release-manager",
                                "expires_at_utc": "2099-01-01T00:00:00Z",
                            }
                        ],
                    }
                )
            )
            payload = BUNDLE.build_bundle_manifest(
                root, source_health_waiver=waiver_path
            )
            self.assertEqual(payload["source_health"]["status"], "degraded")
            self.assertEqual(payload["source_health"]["unwaived_degraded_sources"], [])
            self.assertEqual(
                payload["source_health"]["waivers"][0]["source"],
                "snapshot.wordpress_collections",
            )

    def test_expired_waiver_rejects_stable_promotion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            snapshot_manifest_path = root / BUNDLE.COMPONENTS["snapshot"]["manifest"]
            snapshot_manifest = json.loads(snapshot_manifest_path.read_text())
            snapshot_manifest["source_status"] = {
                "wordpress_collections": "fallback_snapshot"
            }
            snapshot_manifest_path.write_text(json.dumps(snapshot_manifest))
            waiver_path = root / "waiver.json"
            waiver_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "waivers": [
                            {
                                "source": "snapshot.wordpress_collections",
                                "reason": "expired maintenance window",
                                "approved_by": "release-manager",
                                "expires_at_utc": "2020-01-01T00:00:00Z",
                            }
                        ],
                    }
                )
            )
            with self.assertRaisesRegex(RuntimeError, "Expired source-health waiver"):
                BUNDLE.build_bundle_manifest(root, source_health_waiver=waiver_path)

    def test_compact_decision_set_summary_is_exposed_and_fingerprinted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            snapshot_manifest_path = root / BUNDLE.COMPONENTS["snapshot"]["manifest"]
            snapshot_manifest = json.loads(snapshot_manifest_path.read_text())
            snapshot_manifest["decision_set_summary"] = {
                "sha256": "a" * 64,
                "status": "ready",
                "counts": {"accepted": 7, "rejected": 2},
            }
            snapshot_manifest_path.write_text(json.dumps(snapshot_manifest))
            payload = BUNDLE.build_bundle_manifest(root)
            self.assertEqual(
                payload["decision_sets"]["snapshot"],
                snapshot_manifest["decision_set_summary"],
            )
            self.assertEqual(
                payload["source_health"]["components"]["snapshot"]["status"],
                "healthy",
            )
            self.assertEqual(BUNDLE.validate_manifest_contract(payload), [])
            payload["decision_sets"]["snapshot"]["counts"]["accepted"] += 1
            self.assertTrue(
                any(
                    "fingerprint mismatch" in error
                    for error in BUNDLE.validate_manifest_contract(payload)
                )
            )

    def test_manifest_contract_rejects_dangling_asset_pointer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            payload = BUNDLE.build_bundle_manifest(root)
            payload["components"]["snapshot"]["manifest_asset"] = "missing.json"
            self.assertTrue(
                any("not a published asset" in error for error in BUNDLE.validate_manifest_contract(payload))
            )

    def test_producer_version_changes_bundle_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            first = BUNDLE.build_bundle_manifest(root, producer_commit="abc", producer_skill_version="1")
            second = BUNDLE.build_bundle_manifest(root, producer_commit="def", producer_skill_version="2")
            self.assertNotEqual(first["release_fingerprint"], second["release_fingerprint"])

    def test_immutable_release_tag_is_derived_from_date_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            payload = BUNDLE.build_bundle_manifest(root)
            self.assertEqual(
                BUNDLE.immutable_release_tag(payload),
                "tcia-metadata-v2-"
                + payload["generated_at_utc"][:10].replace("-", ".")
                + "-"
                + payload["release_fingerprint"][:12],
            )

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
        self.assertNotIn("tcia_correction_registry.sqlite.gz", detail)
        audit = BUNDLE.assets_for_profile("audit_support", include_dependencies=False)
        self.assertIn("public_non_dicom_audit.sqlite.gz", audit)
        self.assertIn("participant_inventory_audit.sqlite.gz", audit)
        self.assertIn("tcia_correction_registry.sqlite.gz", audit)

    def test_synthetic_research_detail_and_audit_support_installs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets, payload = self.create_installable_bundle(root)
            detail_profile = self.install_from_assets(
                assets, payload, root / "detail-profile-install", profile="research_detail"
            )
            self.assertEqual(detail_profile["profile"], "research_detail")
            self.assertNotIn(
                "tcia_correction_registry.sqlite.gz", detail_profile["downloaded_assets"]
            )
            with mock.patch.object(BUNDLE, "fetch_bytes", return_value=json.dumps(payload).encode()), \
                 mock.patch.object(BUNDLE, "download_to_path", side_effect=lambda url, destination, details, asset, **kwargs: destination.write_bytes((assets / url.rsplit('/', 1)[-1]).read_bytes())):
                audit = BUNDLE.install_bundle(
                    install_dir=root / "audit-install", profile="audit_support"
                )
            self.assertEqual(audit["profile"], "audit_support")
            self.assertTrue(
                (root / "audit-install" / "tcia_correction_registry.sqlite").is_file()
            )

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
            self.assertTrue(result["ok"], result["errors"])
            self.assertEqual(result["release_id"], 42)
            self.assertEqual(len(result["assets"]), 9)

    def test_validate_source_cli_returns_nonzero_with_structured_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            release = root / "release.json"
            release.write_text(json.dumps({"assets": []}))
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "validate-source",
                    "--asset-dir",
                    str(root),
                    "--source-release-json",
                    str(release),
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["errors"])

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
                                "size": (root / name).stat().st_size,
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

    def test_legacy_schema_two_baseline_allows_only_canonical_manifest_omissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_bundle_files(root)
            payload = BUNDLE.build_bundle_manifest(
                root, release_contract=BUNDLE.STREAMLINED_RELEASE_CONTRACT
            )
            payload["schema_version"] = 2
            payload.pop("source_health")
            payload.pop("decision_sets")
            correction = payload["components"].pop("correction_registry")
            payload["assets"].pop(correction["database_asset"], None)
            for component_name, component in payload["components"].items():
                component.pop("source_manifest", None)
                component["manifest_asset"] = BUNDLE.COMPONENTS[component_name]["manifest"]
            payload["asset_count"] = len(payload["assets"])
            for profile in BUNDLE.PROFILE_ORDER:
                payload["profiles"][profile]["assets"] = BUNDLE.assets_for_profile_schema(
                    profile, 2, payload["release_contract"]
                )
            source = payload["source"]
            fingerprint_payload = {
                "artifact": payload["artifact"],
                "schema_version": 2,
                "release_channel": payload["release_channel"],
                "release_tag": payload["release_tag"],
                "release_contract": payload["release_contract"],
                "source": {
                    "repository": source["repository"],
                    "release_tag": source["release_tag"],
                },
                "producer": payload["producer"],
                "assets": {
                    name: details["sha256"]
                    for name, details in sorted(payload["assets"].items())
                },
            }
            payload["release_fingerprint"] = BUNDLE.hashlib.sha256(
                BUNDLE.canonical_json(fingerprint_payload).encode()
            ).hexdigest()
            manifest_path = root / BUNDLE.BUNDLE_MANIFEST_ASSET
            manifest_path.write_text(json.dumps(payload))
            names = [
                "public_non_dicom_metadata.sqlite.gz",
                "public_non_dicom_audit.sqlite.gz",
                "participant_inventory.sqlite.gz",
            ]
            release_path = root / "release.json"
            release_path.write_text(json.dumps({
                "tag_name": BUNDLE.DEFAULT_RELEASE_TAG,
                "assets": [
                    {
                        "name": name,
                        "digest": "sha256:" + BUNDLE.file_sha256(root / name),
                        "size": (root / name).stat().st_size,
                    }
                    for name in names
                ],
            }))

            result = BUNDLE.validate_selected_bundle_assets(
                root, manifest_path, names, release_json_path=release_path
            )
            self.assertTrue(result["ok"], result["errors"])
            self.assertEqual(
                len(result["legacy_schema2_component_manifest_omissions"]), 7
            )

            cases = []
            schema_three = json.loads(json.dumps(payload))
            schema_three["schema_version"] = 3
            cases.append(("schema three", schema_three))
            noncanonical = json.loads(json.dumps(payload))
            noncanonical["components"]["clinical"]["manifest_asset"] = "arbitrary.json"
            cases.append(("noncanonical pointer", noncanonical))
            missing_selected = json.loads(json.dumps(payload))
            missing_selected["assets"].pop(names[0])
            cases.append(("missing selected asset", missing_selected))
            for label, invalid in cases:
                with self.subTest(label=label):
                    manifest_path.write_text(json.dumps(invalid))
                    rejected = BUNDLE.validate_selected_bundle_assets(
                        root, manifest_path, names, release_json_path=release_path
                    )
                    self.assertFalse(rejected["ok"], rejected)

            manifest_path.write_text(json.dumps(payload))
            release = json.loads(release_path.read_text())
            release["assets"][0]["digest"] = "sha256:" + "0" * 64
            release_path.write_text(json.dumps(release))
            rejected = BUNDLE.validate_selected_bundle_assets(
                root, manifest_path, names, release_json_path=release_path
            )
            self.assertFalse(rejected["ok"], rejected)
            self.assertTrue(any("digest mismatch" in error for error in rejected["errors"]))

            for label, release_mutation, expected_error in (
                ("empty release", {"tag_name": BUNDLE.DEFAULT_RELEASE_TAG, "assets": []},
                 "captured release does not contain"),
                ("wrong remote size", {
                    **release,
                    "assets": [
                        {**asset, "size": asset["size"] + 1}
                        if index == 0 else asset
                        for index, asset in enumerate(release["assets"])
                    ],
                }, "captured release byte-size mismatch"),
            ):
                with self.subTest(label=label):
                    release_path.write_text(json.dumps(release_mutation))
                    rejected = BUNDLE.validate_selected_bundle_assets(
                        root, manifest_path, names, release_json_path=release_path
                    )
                    self.assertFalse(rejected["ok"], rejected)
                    self.assertTrue(
                        any(expected_error in error for error in rejected["errors"]),
                        rejected["errors"],
                    )

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
            self.assertEqual(result["asset_count"], 26)

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
                    "https://example.invalid/asset.gz",
                    destination,
                    details,
                    "asset.gz",
                    retries=0,
                )
            self.assertFalse(destination.exists())

    def test_download_retries_complete_transfer_after_midstream_failure(self):
        class FailingResponse(io.BytesIO):
            def __init__(self, body: bytes):
                super().__init__(body)
                self.calls = 0

            def read(self, size: int = -1) -> bytes:
                self.calls += 1
                if self.calls == 2:
                    raise BUNDLE.urllib.error.URLError("connection reset")
                return super().read(min(size, 4))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        body = b"complete-transfer"
        details = {"bytes": len(body), "sha256": BUNDLE.hashlib.sha256(body).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            BUNDLE.urllib.request,
            "urlopen",
            side_effect=[FailingResponse(body), io.BytesIO(body)],
        ) as urlopen, mock.patch.object(BUNDLE.time, "sleep"):
            destination = Path(temporary) / "asset.gz"
            BUNDLE.download_to_path(
                "https://example.invalid/asset.gz",
                destination,
                details,
                "asset.gz",
                retries=1,
            )
            self.assertEqual(destination.read_bytes(), body)
            self.assertEqual(urlopen.call_count, 2)

    def test_fetch_retries_truncated_content_length_and_rate_limits(self):
        class Response(io.BytesIO):
            def __init__(self, body: bytes, content_length: int | None = None):
                super().__init__(body)
                self.headers = (
                    {"Content-Length": str(content_length)}
                    if content_length is not None
                    else {}
                )

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        body = b"manifest"
        rate_limit_headers = {"Retry-After": "0"}
        rate_limited = BUNDLE.urllib.error.HTTPError(
            "https://example.invalid", 429, "rate limited", rate_limit_headers, None
        )
        forbidden_headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}
        forbidden = BUNDLE.urllib.error.HTTPError(
            "https://example.invalid", 403, "rate limited", forbidden_headers, None
        )
        with mock.patch.object(
            BUNDLE.urllib.request,
            "urlopen",
            side_effect=[Response(b"short", len(body)), rate_limited, forbidden, Response(body)],
        ) as urlopen, mock.patch.object(BUNDLE.time, "sleep"):
            self.assertEqual(
                BUNDLE.fetch_bytes("https://example.invalid", retries=3), body
            )
            self.assertEqual(urlopen.call_count, 4)

    def test_download_retries_checksum_failure_from_zero(self):
        body = b"correct"
        details = {"bytes": len(body), "sha256": BUNDLE.hashlib.sha256(body).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            BUNDLE.urllib.request,
            "urlopen",
            side_effect=[io.BytesIO(b"badbad"), io.BytesIO(body)],
        ) as urlopen, mock.patch.object(BUNDLE.time, "sleep"):
            destination = Path(temporary) / "asset.gz"
            BUNDLE.download_to_path(
                "https://example.invalid/asset.gz",
                destination,
                details,
                "asset.gz",
                retries=1,
            )
            self.assertEqual(destination.read_bytes(), body)
            self.assertEqual(urlopen.call_count, 2)

    def test_prune_is_receipt_aware_and_preserves_unmanaged_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_dir = root / "installed"
            assets = root / "assets"
            install_dir.mkdir()
            assets.mkdir()
            self.create_bundle_files(assets)
            manifest = BUNDLE.build_bundle_manifest(assets)
            selected = manifest["profiles"]["research_detail"]["assets"]
            (install_dir / BUNDLE.BUNDLE_MANIFEST_ASSET).write_text(json.dumps(manifest))
            (install_dir / BUNDLE.INSTALL_STATE_ASSET).write_text(
                json.dumps(
                    {
                        "artifact": "tcia_metadata_v2_install",
                        "release_fingerprint": manifest["release_fingerprint"],
                        "installed_profile": "research_detail",
                        "installed_assets": selected,
                    }
                )
            )
            (install_dir / "tcia_snapshot.sqlite").write_bytes(b"active")
            (install_dir / "pathology_metadata.sqlite").write_bytes(b"legacy")
            (install_dir / "operator-notes.txt").write_text("preserve me")
            generation_root = install_dir / BUNDLE.GENERATIONS_DIRNAME
            generation_root.mkdir()
            abandoned = generation_root / ".tcia-v2-stage-abandoned"
            abandoned.mkdir()
            (abandoned / "partial.sqlite.gz").write_bytes(b"partial")

            dry_run = BUNDLE.prune_install(
                install_dir,
                stale_stage_hours=0,
            )
            self.assertTrue(dry_run["dry_run"])
            self.assertTrue((install_dir / "pathology_metadata.sqlite").exists())
            self.assertGreater(dry_run["stale_bytes"], 0)

            applied = BUNDLE.prune_install(
                install_dir,
                apply=True,
                stale_stage_hours=0,
            )
            self.assertEqual(applied["status"], "pruned")
            self.assertFalse((install_dir / "pathology_metadata.sqlite").exists())
            self.assertFalse(abandoned.exists())
            self.assertTrue((install_dir / "tcia_snapshot.sqlite").exists())
            self.assertTrue((install_dir / "operator-notes.txt").exists())

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

            def fake_fetch(url: str, **_kwargs) -> bytes:
                name = url.rsplit("/", 1)[-1]
                if name == BUNDLE.BUNDLE_MANIFEST_ASSET:
                    return bundle_body
                return (assets / name).read_bytes()

            def fake_download(
                url: str, destination: Path, details: dict, asset: str, **_kwargs
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

            def fake_fetch(url: str, **_kwargs) -> bytes:
                name = url.rsplit("/", 1)[-1]
                if name == BUNDLE.BUNDLE_MANIFEST_ASSET:
                    return bundle_body
                return (assets / name).read_bytes()

            def fake_download(
                url: str, destination: Path, details: dict, asset: str, **_kwargs
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

    def test_install_automatically_migrates_flat_layout_to_atomic_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            install_dir = root / "installed"
            assets.mkdir()
            install_dir.mkdir()
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
                component_manifest = json.loads((assets / details["manifest"]).read_text())
                component_manifest["sqlite_sha256"] = BUNDLE.hashlib.sha256(raw).hexdigest()
                component_manifest["gzip_sha256"] = BUNDLE.hashlib.sha256(compressed).hexdigest()
                (assets / details["manifest"]).write_text(json.dumps(component_manifest))
            payload = BUNDLE.build_bundle_manifest(assets)
            selected = payload["profiles"]["research_core"]["assets"]
            for asset in selected:
                destination = install_dir / BUNDLE.installed_asset_name(asset)
                if asset.endswith(".sqlite.gz"):
                    destination.write_bytes(gzip.decompress((assets / asset).read_bytes()))
                else:
                    destination.write_bytes((assets / asset).read_bytes())
            (install_dir / BUNDLE.BUNDLE_MANIFEST_ASSET).write_text(json.dumps(payload))
            (install_dir / BUNDLE.INSTALL_STATE_ASSET).write_text(
                json.dumps(
                    {
                        "artifact": "tcia_metadata_v2_install",
                        "release_tag": BUNDLE.DEFAULT_RELEASE_TAG,
                        "release_fingerprint": payload["release_fingerprint"],
                        "installed_profile": "research_core",
                        "installed_assets": selected,
                        "installed_at_utc": "2026-09-01T00:00:00+00:00",
                    }
                )
            )

            with mock.patch.object(
                BUNDLE, "fetch_bytes", return_value=json.dumps(payload).encode()
            ):
                result = BUNDLE.install_bundle(install_dir=install_dir)
            self.assertTrue(result["migrated_legacy_install"])
            self.assertTrue((install_dir / BUNDLE.CURRENT_POINTER).is_symlink())
            self.assertTrue((install_dir / "tcia_snapshot.sqlite").is_symlink())
            self.assertTrue((install_dir / "tcia_snapshot.sqlite").is_file())
            generations = [
                path
                for path in (install_dir / BUNDLE.GENERATIONS_DIRNAME).iterdir()
                if path.is_dir()
            ]
            self.assertEqual(len(generations), 1)
            self.assertEqual(
                BUNDLE.active_generation_dir(install_dir).resolve(),
                generations[0].resolve(),
            )
            prior = install_dir / BUNDLE.GENERATIONS_DIRNAME / "prior-generation"
            shutil.copytree(generations[0], prior)
            rollback = BUNDLE.rollback_install(install_dir)
            self.assertEqual(rollback["status"], "rolled_back")
            self.assertEqual(BUNDLE.active_generation_dir(install_dir).resolve(), prior.resolve())

    def test_receipt_and_compatibility_names_cannot_escape_install_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets, payload = self.create_installable_bundle(root)
            install_dir = root / "installed"
            self.install_from_assets(assets, payload, install_dir)
            generation = BUNDLE.active_generation_dir(install_dir)
            receipt_path = generation / BUNDLE.INSTALL_STATE_ASSET
            original = receipt_path.read_text()
            victim = root / "victim"
            victim.write_text("preserve")
            for bad_name in ("../victim", str(victim), "nested/victim", "nested\\victim"):
                with self.subTest(name=bad_name):
                    receipt = json.loads(original)
                    receipt["installed_assets"] = [bad_name]
                    receipt_path.write_text(json.dumps(receipt))
                    with self.assertRaisesRegex(RuntimeError, "receipt assets disagree|Unsafe"):
                        BUNDLE._verify_generation(generation)
                    self.assertEqual(victim.read_text(), "preserve")
            receipt_path.write_text(original)
            with self.assertRaisesRegex(RuntimeError, "managed asset allowlist"):
                BUNDLE._ensure_compatibility_links(install_dir, {"../victim"})
            self.assertEqual(victim.read_text(), "preserve")

    def test_symlinked_install_topology_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            marker = outside / "marker"
            marker.write_text("preserve")

            install_with_generation_link = root / "generation-link-install"
            install_with_generation_link.mkdir()
            (install_with_generation_link / BUNDLE.GENERATIONS_DIRNAME).symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
                BUNDLE._generation_root(install_with_generation_link)

            install_with_escaping_current = root / "escaping-current-install"
            install_with_escaping_current.mkdir()
            (install_with_escaping_current / BUNDLE.CURRENT_POINTER).symlink_to("../outside")
            with self.assertRaisesRegex(RuntimeError, "escapes generations"):
                BUNDLE.active_generation_dir(install_with_escaping_current)

            install_with_child_link = root / "child-link-install"
            install_with_child_link.mkdir()
            generation_root = install_with_child_link / BUNDLE.GENERATIONS_DIRNAME
            generation_root.mkdir()
            (generation_root / "linked-generation").symlink_to(outside, target_is_directory=True)
            (install_with_child_link / BUNDLE.CURRENT_POINTER).symlink_to(
                f"{BUNDLE.GENERATIONS_DIRNAME}/linked-generation"
            )
            with self.assertRaisesRegex(RuntimeError, "not a real directory"):
                BUNDLE.active_generation_dir(install_with_child_link)
            self.assertEqual(marker.read_text(), "preserve")

    def test_generation_rejects_symlinked_managed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets, payload = self.create_installable_bundle(root)
            install_dir = root / "installed"
            self.install_from_assets(assets, payload, install_dir)
            generation = BUNDLE.active_generation_dir(install_dir)
            managed = generation / "tcia_snapshot.sqlite"
            victim = root / "victim.sqlite"
            victim.write_bytes(managed.read_bytes())
            managed.unlink()
            managed.symlink_to(victim)
            with self.assertRaisesRegex(RuntimeError, "regular file|file set"):
                BUNDLE._verify_generation(generation)
            self.assertTrue(victim.is_file())

    def test_first_install_recovers_if_current_switch_is_interrupted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets, payload = self.create_installable_bundle(root)
            install_dir = root / "installed"
            original_atomic_symlink = BUNDLE._atomic_symlink

            def interrupt_current(target: str, link: Path) -> None:
                if link.name == BUNDLE.CURRENT_POINTER:
                    raise RuntimeError("injected current switch interruption")
                original_atomic_symlink(target, link)

            bundle_body = json.dumps(payload).encode()

            def fake_fetch(url: str, **_kwargs) -> bytes:
                name = url.rsplit("/", 1)[-1]
                return bundle_body if name == BUNDLE.BUNDLE_MANIFEST_ASSET else (assets / name).read_bytes()

            def fake_download(
                url: str, destination: Path, _details: dict, _asset: str, **_kwargs
            ) -> None:
                destination.write_bytes((assets / url.rsplit("/", 1)[-1]).read_bytes())

            with mock.patch.object(BUNDLE, "fetch_bytes", side_effect=fake_fetch), mock.patch.object(
                BUNDLE, "download_to_path", side_effect=fake_download
            ), mock.patch.object(BUNDLE, "_atomic_symlink", side_effect=interrupt_current):
                with self.assertRaisesRegex(RuntimeError, "injected current switch"):
                    BUNDLE.install_bundle(install_dir=install_dir)

            self.assertFalse((install_dir / BUNDLE.CURRENT_POINTER).exists())
            self.assertTrue((install_dir / "tcia_snapshot.sqlite").is_symlink())
            self.assertFalse((install_dir / "tcia_snapshot.sqlite").exists())
            recovered = self.install_from_assets(assets, payload, install_dir)
            self.assertEqual(recovered["status"], "unchanged")
            self.assertTrue((install_dir / BUNDLE.CURRENT_POINTER).is_symlink())
            self.assertTrue((install_dir / "tcia_snapshot.sqlite").is_file())


if __name__ == "__main__":
    unittest.main()
