import hashlib
import importlib.util
import json
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


staging = load("tcia_v2_staging")
public = load("tcia_public_non_dicom_metadata")
audit = load("tcia_v2_audit")


class V2StagingTests(unittest.TestCase):
    def create_component(self, root: Path, component: str) -> tuple[Path, Path]:
        database = root / f"{component}.sqlite"
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("CREATE TABLE source_rows (source_id TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO source_rows VALUES (?, ?)", (component, "value"))
            conn.execute("CREATE VIEW agent_source_rows AS SELECT * FROM source_rows")
            conn.commit()
        manifest = root / f"{component}_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "sqlite_sha256": staging.file_sha256(database),
                    "release_fingerprint": f"fingerprint-{component}",
                }
            )
        )
        return database, manifest

    def create_legacy_public_cross_component_pair(
        self,
        components: dict[str, tuple[Path, Path]],
        *,
        orphan_asset_id: str | None = None,
        unrelated_orphan: bool = False,
        local_shadow_parent: bool = False,
        foreign_parent_table: str = "public_non_dicom_assets",
        short_title_mismatch: bool = False,
        parent_has_primary_key: bool = True,
    ) -> Path:
        research, research_manifest = components["public_non_dicom_baseline"]
        research.unlink()
        with closing(sqlite3.connect(research)) as conn:
            conn.execute(
                "CREATE TABLE public_non_dicom_assets "
                f"(asset_id TEXT {'PRIMARY KEY' if parent_has_primary_key else ''}, "
                "short_title TEXT NOT NULL, value TEXT)"
            )
            conn.execute(
                "INSERT INTO public_non_dicom_assets VALUES ('asset-1', 'TEST', 'ok')"
            )
            conn.execute("CREATE TABLE artifact_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT INTO artifact_meta VALUES (?, ?)",
                (
                    ("provenance_storage", "companion_audit_artifact"),
                    ("audit_companion_asset", "public_non_dicom_audit.sqlite.gz"),
                    ("audit_schema_version", "3"),
                ),
            )
            conn.commit()
        research_payload = json.loads(research_manifest.read_text())
        research_payload.update({
            "schema_version": 8,
            "sqlite_sha256": staging.file_sha256(research),
            "gzip_sha256": hashlib.sha256(b"research-gzip").hexdigest(),
            "database_asset": "public_non_dicom_metadata.sqlite.gz",
            "profile": "research_detail",
            "provenance": {
                "provenance_storage": "companion_audit_artifact",
                "audit_companion_asset": "public_non_dicom_audit.sqlite.gz",
                "audit_schema_version": "3",
            },
            "storage_contract": None,
        })
        research_manifest.write_text(json.dumps(research_payload))

        audit_database, audit_manifest = components["public_non_dicom_audit_baseline"]
        audit_database.unlink()
        with closing(sqlite3.connect(audit_database)) as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "CREATE TABLE public_non_dicom_crosswalk_evidence ("
                "crosswalk_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, "
                "short_title TEXT NOT NULL, "
                f"FOREIGN KEY(asset_id) REFERENCES {foreign_parent_table}(asset_id))"
            )
            conn.execute(
                "INSERT INTO public_non_dicom_crosswalk_evidence VALUES (?, ?, ?)",
                (
                    "crosswalk-1",
                    orphan_asset_id or "asset-1",
                    "OTHER" if short_title_mismatch else "TEST",
                ),
            )
            conn.execute("CREATE TABLE audit_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT INTO audit_meta VALUES (?, ?)",
                (
                    ("research_artifact", "public_non_dicom"),
                    ("research_database_asset", "public_non_dicom_metadata.sqlite.gz"),
                    ("schema_version", "3"),
                ),
            )
            if local_shadow_parent:
                conn.execute(
                    "CREATE TABLE public_non_dicom_assets (asset_id TEXT PRIMARY KEY)"
                )
                conn.execute("INSERT INTO public_non_dicom_assets VALUES ('asset-1')")
            if unrelated_orphan:
                conn.execute("CREATE TABLE local_parents (id INTEGER PRIMARY KEY)")
                conn.execute(
                    "CREATE TABLE local_children "
                    "(parent_id INTEGER REFERENCES local_parents(id))"
                )
                conn.execute("INSERT INTO local_children VALUES (42)")
            conn.commit()
        audit_payload = json.loads(audit_manifest.read_text())
        audit_payload.update({
            "schema_version": 3,
            "sqlite_sha256": staging.file_sha256(audit_database),
            "gzip_sha256": hashlib.sha256(b"audit-gzip").hexdigest(),
            "database_asset": "public_non_dicom_audit.sqlite.gz",
            "profile": "audit_support",
            "storage_contract": None,
        })
        audit_manifest.write_text(json.dumps(audit_payload))
        bundle = {
            "artifact": "tcia_metadata_v2_bundle",
            "schema_version": 2,
            "release_channel": "stable",
            "release_tag": "tcia-metadata-v2-latest",
            "release_contract": "streamlined",
            "source": {"repository": "example/repository", "release_tag": "source"},
            "producer": {"commit": "a" * 40},
            "assets": {
                research_payload["database_asset"]: {
                    "sha256": research_payload["gzip_sha256"], "bytes": 1
                },
                audit_payload["database_asset"]: {
                    "sha256": audit_payload["gzip_sha256"], "bytes": 1
                },
            },
            "components": {
                "public_non_dicom": research_payload,
                "public_non_dicom_audit": audit_payload,
            },
            "profiles": {
                "audit_support": {
                    "assets": [
                        research_payload["database_asset"],
                        audit_payload["database_asset"],
                    ],
                    "depends_on": ["research_core", "research_detail"],
                }
            },
        }
        fingerprint_payload = {
            "artifact": bundle["artifact"],
            "schema_version": bundle["schema_version"],
            "release_channel": bundle["release_channel"],
            "release_tag": bundle["release_tag"],
            "release_contract": bundle["release_contract"],
            "source": bundle["source"],
            "producer": bundle["producer"],
            "assets": {
                name: details["sha256"]
                for name, details in sorted(bundle["assets"].items())
            },
        }
        bundle["release_fingerprint"] = hashlib.sha256(
            staging.canonical_json(fingerprint_payload).encode()
        ).hexdigest()
        bundle_path = research.parent / "baseline_bundle_manifest.json"
        bundle_path.write_text(json.dumps(bundle))
        return bundle_path

    def test_build_validate_and_resolve_runner_local_staging_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            ledger = root / "tcia_metadata_staging.sqlite"
            result = staging.build_staging_database(
                ledger, components=components, replace=True
            )
            self.assertEqual(result["components"], 6)
            validation = staging.validate_staging_database(
                ledger, verify_sources=True
            )
            self.assertTrue(validation["ok"], validation["errors"])
            self.assertEqual(
                staging.resolve_component(ledger, "public_non_dicom_baseline"),
                components["public_non_dicom_baseline"][0],
            )
            with closing(sqlite3.connect(ledger)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT row_count FROM staging_object_inventory "
                        "WHERE component='clinical' AND object_name='source_rows'"
                    ).fetchone()[0],
                    1,
                )

    def test_public_audit_embeds_path_independent_staging_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            ledger = root / "tcia_metadata_staging.sqlite"
            staging.build_staging_database(ledger, components=components, replace=True)
            research = root / "public.sqlite"
            companion = root / "public_audit.sqlite"
            with closing(sqlite3.connect(research)) as conn:
                conn.executescript(public.SCHEMA)
                public.insert_vocab(conn)
                conn.execute("INSERT INTO artifact_meta VALUES ('schema_version', '7')")
                conn.commit()
            result = audit.split_database(
                research,
                companion,
                artifact="public_non_dicom",
                staging_database=ledger,
                replace=True,
            )
            self.assertTrue(result["audit_validation"]["ok"])
            with closing(sqlite3.connect(companion)) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM staging_sources").fetchone()[0],
                    6,
                )
                database_files = {
                    row[0] for row in conn.execute("SELECT database_file FROM staging_sources")
                }
                self.assertTrue(all("/" not in name for name in database_files))
                fingerprint = conn.execute(
                    "SELECT value FROM audit_meta WHERE key='staging_source_fingerprint'"
                ).fetchone()[0]
                self.assertEqual(len(fingerprint), hashlib.sha256().digest_size * 2)

    def test_build_rejects_declared_foreign_key_violations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            database, manifest = components["snapshot"]
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
                conn.execute(
                    "CREATE TABLE children (parent_id INTEGER REFERENCES parents(id))"
                )
                conn.execute("INSERT INTO children VALUES (42)")
                conn.commit()
            payload = json.loads(manifest.read_text())
            payload["sqlite_sha256"] = staging.file_sha256(database)
            manifest.write_text(json.dumps(payload))
            with self.assertRaisesRegex(RuntimeError, "foreign_key_check"):
                staging.build_staging_database(
                    root / "staging.sqlite", components=components, replace=True
                )

    def test_build_validates_legacy_cross_component_foreign_key_against_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            bundle_manifest = self.create_legacy_public_cross_component_pair(components)
            ledger = root / "staging.sqlite"
            result = staging.build_staging_database(
                ledger,
                components=components,
                baseline_bundle_manifest_path=bundle_manifest,
                replace=True,
            )
            self.assertEqual(len(result["cross_component_foreign_keys"]), 1)
            check = result["cross_component_foreign_keys"][0]
            self.assertEqual(check["child_rows"], 1)
            self.assertEqual(check["matched_rows"], 1)
            self.assertEqual(check["distinct_child_keys"], 1)
            self.assertEqual(check["matched_distinct_keys"], 1)
            self.assertEqual(check["orphan_rows"], 0)
            self.assertEqual(check["coherence_mismatches"], 0)
            self.assertEqual(len(check["bundle_release_fingerprint"]), 64)
            with closing(sqlite3.connect(ledger)) as conn:
                recorded = json.loads(
                    conn.execute(
                        "SELECT value FROM staging_meta "
                        "WHERE key='cross_component_foreign_keys'"
                    ).fetchone()[0]
                )
            self.assertEqual(recorded, result["cross_component_foreign_keys"])

    def test_build_rejects_legacy_cross_component_orphan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            bundle_manifest = self.create_legacy_public_cross_component_pair(
                components, orphan_asset_id="missing-asset"
            )
            with self.assertRaisesRegex(
                RuntimeError, "cross-component foreign-key orphans.*count=1"
            ):
                staging.build_staging_database(
                    root / "staging.sqlite",
                    components=components,
                    baseline_bundle_manifest_path=bundle_manifest,
                    replace=True,
                )

    def test_build_rejects_unrecognized_orphan_alongside_valid_cross_component_fk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            bundle_manifest = self.create_legacy_public_cross_component_pair(
                components, unrelated_orphan=True
            )
            with self.assertRaisesRegex(
                RuntimeError, "unrecognized foreign_key_check violations"
            ):
                staging.build_staging_database(
                    root / "staging.sqlite",
                    components=components,
                    baseline_bundle_manifest_path=bundle_manifest,
                    replace=True,
                )

    def test_build_rejects_local_shadow_for_declared_cross_component_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            bundle_manifest = self.create_legacy_public_cross_component_pair(
                components, local_shadow_parent=True
            )
            with self.assertRaisesRegex(
                RuntimeError, "cross-component parent unexpectedly exists locally"
            ):
                staging.build_staging_database(
                    root / "staging.sqlite",
                    components=components,
                    baseline_bundle_manifest_path=bundle_manifest,
                    replace=True,
                )

    def test_build_rejects_redirected_cross_component_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            bundle_manifest = self.create_legacy_public_cross_component_pair(
                components, foreign_parent_table="redirected_assets"
            )
            with self.assertRaisesRegex(
                RuntimeError, "cross-component foreign-key declaration mismatch"
            ):
                staging.build_staging_database(
                    root / "staging.sqlite",
                    components=components,
                    baseline_bundle_manifest_path=bundle_manifest,
                    replace=True,
                )

    def test_build_rejects_cross_component_parent_manifest_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            bundle_manifest = self.create_legacy_public_cross_component_pair(components)
            _database, parent_manifest = components["public_non_dicom_baseline"]
            payload = json.loads(parent_manifest.read_text())
            payload["sqlite_sha256"] = "0" * 64
            parent_manifest.write_text(json.dumps(payload))
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                staging.build_staging_database(
                    root / "staging.sqlite",
                    components=components,
                    baseline_bundle_manifest_path=bundle_manifest,
                    replace=True,
                )

    def test_build_rejects_cross_component_pair_from_different_bundle_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            bundle_manifest = self.create_legacy_public_cross_component_pair(components)
            _database, audit_manifest = components["public_non_dicom_audit_baseline"]
            payload = json.loads(audit_manifest.read_text())
            payload["release_fingerprint"] = "f" * 64
            audit_manifest.write_text(json.dumps(payload))
            with self.assertRaisesRegex(
                RuntimeError, "child manifest does not match pinned bundle component"
            ):
                staging.build_staging_database(
                    root / "staging.sqlite",
                    components=components,
                    baseline_bundle_manifest_path=bundle_manifest,
                    replace=True,
                )

    def test_build_rejects_tampered_parent_even_with_updated_free_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            bundle_manifest = self.create_legacy_public_cross_component_pair(components)
            parent_database, parent_manifest = components["public_non_dicom_baseline"]
            with closing(sqlite3.connect(parent_database)) as conn:
                conn.execute(
                    "UPDATE public_non_dicom_assets SET value='tampered' "
                    "WHERE asset_id='asset-1'"
                )
                conn.commit()
            payload = json.loads(parent_manifest.read_text())
            payload["sqlite_sha256"] = staging.file_sha256(parent_database)
            parent_manifest.write_text(json.dumps(payload))
            with self.assertRaisesRegex(
                RuntimeError, "parent manifest does not match pinned bundle component"
            ):
                staging.build_staging_database(
                    root / "staging.sqlite",
                    components=components,
                    baseline_bundle_manifest_path=bundle_manifest,
                    replace=True,
                )

    def test_build_rejects_cross_component_parent_key_schema_defect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            bundle_manifest = self.create_legacy_public_cross_component_pair(
                components, parent_has_primary_key=False
            )
            with self.assertRaisesRegex(RuntimeError, "parent key is not a primary key"):
                staging.build_staging_database(
                    root / "staging.sqlite",
                    components=components,
                    baseline_bundle_manifest_path=bundle_manifest,
                    replace=True,
                )

    def test_build_rejects_cross_component_short_title_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            bundle_manifest = self.create_legacy_public_cross_component_pair(
                components, short_title_mismatch=True
            )
            with self.assertRaisesRegex(
                RuntimeError, "cross-component coherence mismatches.*count=1"
            ):
                staging.build_staging_database(
                    root / "staging.sqlite",
                    components=components,
                    baseline_bundle_manifest_path=bundle_manifest,
                    replace=True,
                )


if __name__ == "__main__":
    unittest.main()
