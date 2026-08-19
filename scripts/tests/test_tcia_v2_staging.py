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
                staging.resolve_component(ledger, "pathology"),
                components["pathology"][0],
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


if __name__ == "__main__":
    unittest.main()
