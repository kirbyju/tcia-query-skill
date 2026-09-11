import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_snapshot.py"
SPEC = importlib.util.spec_from_file_location("tcia_snapshot", SCRIPT)
SNAPSHOT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SNAPSHOT)


class SnapshotNormalizationTests(unittest.TestCase):
    def test_pathdb_whole_slide_modality_is_canonical(self):
        self.assertEqual(
            SNAPSHOT.canonical_pathdb_modality("Whole slide image"),
            "Whole Slide Image",
        )
        self.assertEqual(
            SNAPSHOT.canonical_pathdb_modality("  Whole   Slide Image "),
            "Whole Slide Image",
        )

    def test_pathdb_other_modality_is_not_reinterpreted(self):
        self.assertEqual(SNAPSHOT.canonical_pathdb_modality("SM"), "SM")

    def test_public_views_and_exports_exclude_hidden_builder_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "snapshot.sqlite"
            exports = root / "exports"
            with sqlite3.connect(db) as conn:
                SNAPSHOT.create_schema(conn)
                for hidden, short_title in ((0, "VISIBLE"), (1, "HIDDEN")):
                    normalized = {
                        "short_title": short_title,
                        "title": short_title.title(),
                    }
                    conn.execute(
                        """INSERT INTO wordpress_records
                           (source, id, slug, short_title, short_title_key, doi,
                            title, link, date_updated, hidden, normalized_json,
                            raw_json, search_text)
                           VALUES ('collections', ?, ?, ?, ?, '', ?, '', '', ?, ?, '{}', ?)""",
                        (
                            short_title,
                            short_title.lower(),
                            short_title,
                            SNAPSHOT.short_title_key(short_title),
                            short_title.title(),
                            hidden,
                            json.dumps(normalized),
                            short_title.lower(),
                        ),
                    )
                SNAPSHOT.insert_meta(conn, {"schema_version": SNAPSHOT.SCHEMA_VERSION})
                conn.commit()

            with sqlite3.connect(db) as conn:
                self.assertEqual(
                    conn.execute("SELECT short_title FROM agent_datasets").fetchall(),
                    [("VISIBLE",)],
                )
            exported = SNAPSHOT.export_web_artifacts(db, exports)
            self.assertEqual(exported[SNAPSHOT.AGENT_DATASETS_JSONL]["rows"], 1)
            rows = [
                json.loads(line)
                for line in (exports / SNAPSHOT.AGENT_DATASETS_JSONL)
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual([row["short_title"] for row in rows], ["VISIBLE"])


if __name__ == "__main__":
    unittest.main()
