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
checkpoint = load("tcia_v2_checkpoint")
public = load("tcia_public_non_dicom_metadata")
audit = load("tcia_v2_audit")


class V2CheckpointTests(unittest.TestCase):
    def create_component(self, root: Path, component: str) -> tuple[Path, Path]:
        database = root / f"{component}.sqlite"
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("CREATE TABLE base_rows (source_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO base_rows VALUES (?)", (component,))
            for table in checkpoint.CHECKPOINT_TABLES.get(component, ()):
                conn.execute(f'CREATE TABLE "{table}" (row_id TEXT, value TEXT)')
                conn.execute(
                    f'INSERT INTO "{table}" VALUES (?, ?)',
                    (f"{component}-{table}", "raw-value"),
                )
            conn.commit()
        manifest = root / f"{component}_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sqlite_sha256": staging.file_sha256(database),
                    "release_fingerprint": f"fingerprint-{component}",
                }
            )
        )
        return database, manifest

    def test_checkpoint_is_exact_and_can_seed_public_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = {
                component: self.create_component(root, component)
                for component in staging.COMPONENT_ORDER
            }
            components.update(
                {
                    component: self.create_component(root, component)
                    for component in checkpoint.CHECKPOINT_TABLES
                }
            )
            ledger = root / "staging.sqlite"
            staging.build_staging_database(ledger, components=components, replace=True)
            checkpoint_db = root / "checkpoint.sqlite"
            result = checkpoint.build_checkpoint(
                checkpoint_db, staging_db=ledger, replace=True
            )
            self.assertEqual(result["tables"], 11)
            self.assertEqual(result["rows"], 11)
            validation = checkpoint.validate_checkpoint(checkpoint_db)
            self.assertTrue(validation["ok"], validation["errors"])

            research = root / "public.sqlite"
            audit_db = root / "audit.sqlite"
            with closing(sqlite3.connect(research)) as conn:
                conn.executescript(public.SCHEMA)
                public.insert_vocab(conn)
                conn.execute("INSERT INTO artifact_meta VALUES ('schema_version', '7')")
                conn.commit()
            split = audit.split_database(
                research,
                audit_db,
                artifact="public_non_dicom",
                staging_database=ledger,
                checkpoint_database=checkpoint_db,
                consume_checkpoint=True,
                replace=True,
            )
            self.assertTrue(split["audit_validation"]["ok"])
            self.assertFalse(checkpoint_db.exists())
            with closing(sqlite3.connect(audit_db)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM source_nifti__derived_objects"
                    ).fetchone()[0],
                    1,
                )
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT value FROM audit_meta "
                        "WHERE key='legacy_detail_checkpoint_fingerprint'"
                    ).fetchone()
                )

            extracted = root / "extracted.sqlite"
            extraction = checkpoint.extract_checkpoint_from_audit(
                extracted, audit_db=audit_db, replace=True
            )
            self.assertEqual(extraction["tables"], 11)
            self.assertEqual(extraction["rows"], 11)
            self.assertTrue(checkpoint.validate_checkpoint(extracted)["ok"])


if __name__ == "__main__":
    unittest.main()
