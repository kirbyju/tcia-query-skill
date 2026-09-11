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
    ) -> None:
        research, research_manifest = components["public_non_dicom_baseline"]
        research.unlink()
        with closing(sqlite3.connect(research)) as conn:
            conn.execute(
                "CREATE TABLE public_non_dicom_assets "
                "(asset_id TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute("INSERT INTO public_non_dicom_assets VALUES ('asset-1', 'ok')")
            conn.commit()
        research_payload = json.loads(research_manifest.read_text())
        research_payload["sqlite_sha256"] = staging.file_sha256(research)
        research_manifest.write_text(json.dumps(research_payload))

        audit_database, audit_manifest = components["public_non_dicom_audit_baseline"]
        audit_database.unlink()
        with closing(sqlite3.connect(audit_database)) as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "CREATE TABLE public_non_dicom_crosswalk_evidence ("
                "crosswalk_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, "
                f"FOREIGN KEY(asset_id) REFERENCES {foreign_parent_table}(asset_id))"
            )
            conn.execute(
                "INSERT INTO public_non_dicom_crosswalk_evidence VALUES (?, ?)",
                ("crosswalk-1", orphan_asset_id or "asset-1"),
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
        audit_payload["sqlite_sha256"] = staging.file_sha256(audit_database)
        audit_manifest.write_text(json.dumps(audit_payload))

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
            self.create_legacy_public_cross_component_pair(components)
            ledger = root / "staging.sqlite"
            result = staging.build_staging_database(
                ledger, components=components, replace=True
            )
            self.assertEqual(
                result["cross_component_foreign_keys"],
                [{
                    "child_component": "public_non_dicom_audit_baseline",
                    "child_table": "public_non_dicom_crosswalk_evidence",
                    "child_column": "asset_id",
                    "parent_component": "public_non_dicom_baseline",
                    "parent_table": "public_non_dicom_assets",
                    "parent_column": "asset_id",
                    "verified_rows": 1,
                }],
            )
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
            self.create_legacy_public_cross_component_pair(
                components, orphan_asset_id="missing-asset"
            )
            with self.assertRaisesRegex(
                RuntimeError, "cross-component foreign-key orphans.*count=1"
            ):
                staging.build_staging_database(
                    root / "staging.sqlite", components=components, replace=True
                )

    def test_build_rejects_unrecognized_orphan_alongside_valid_cross_component_fk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            self.create_legacy_public_cross_component_pair(
                components, unrelated_orphan=True
            )
            with self.assertRaisesRegex(
                RuntimeError, "unrecognized foreign_key_check violations"
            ):
                staging.build_staging_database(
                    root / "staging.sqlite", components=components, replace=True
                )

    def test_build_rejects_local_shadow_for_declared_cross_component_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            self.create_legacy_public_cross_component_pair(
                components, local_shadow_parent=True
            )
            with self.assertRaisesRegex(
                RuntimeError, "cross-component parent unexpectedly exists locally"
            ):
                staging.build_staging_database(
                    root / "staging.sqlite", components=components, replace=True
                )

    def test_build_rejects_redirected_cross_component_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            self.create_legacy_public_cross_component_pair(
                components, foreign_parent_table="redirected_assets"
            )
            with self.assertRaisesRegex(
                RuntimeError, "cross-component foreign-key declaration mismatch"
            ):
                staging.build_staging_database(
                    root / "staging.sqlite", components=components, replace=True
                )

    def test_build_rejects_cross_component_parent_manifest_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            self.create_legacy_public_cross_component_pair(components)
            _database, parent_manifest = components["public_non_dicom_baseline"]
            payload = json.loads(parent_manifest.read_text())
            payload["sqlite_sha256"] = "0" * 64
            parent_manifest.write_text(json.dumps(payload))
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                staging.build_staging_database(
                    root / "staging.sqlite", components=components, replace=True
                )


if __name__ == "__main__":
    unittest.main()
