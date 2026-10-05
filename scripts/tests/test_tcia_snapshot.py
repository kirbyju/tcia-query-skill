import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_snapshot.py"
SPEC = importlib.util.spec_from_file_location("tcia_snapshot", SCRIPT)
SNAPSHOT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SNAPSHOT)


class SnapshotNormalizationTests(unittest.TestCase):
    @staticmethod
    def datacite_record(doi: str) -> dict:
        return {
            "id": doi,
            "attributes": {
                "doi": doi,
                "identifiers": [],
                "titles": [{"title": doi}],
            },
        }

    def test_datacite_fetch_follows_cursor_and_validates_complete_unique_rows(self):
        first_url = SNAPSHOT.datacite_url("1", 1000)
        next_url = (
            "https://api.datacite.org/dois?prefix=10.7937&"
            "page%5Bcursor%5D=opaque&page%5Bsize%5D=1000"
        )
        responses = {
            first_url: {
                "data": [self.datacite_record("10.7937/second")],
                "meta": {"total": 2},
                "links": {"next": next_url},
            },
            next_url: {
                "data": [self.datacite_record("10.7937/first")],
                "meta": {"total": 2},
                "links": {"next": None},
            },
        }

        with mock.patch.object(
            SNAPSHOT,
            "fetch_json",
            side_effect=lambda url: (responses[url], {}),
        ) as fetch:
            records = SNAPSHOT.fetch_datacite_prefix()

        self.assertEqual(
            [SNAPSHOT.datacite_doi(record) for record in records],
            ["10.7937/first", "10.7937/second"],
        )
        self.assertEqual(
            [call.args[0] for call in fetch.call_args_list],
            [first_url, next_url],
        )

    def test_datacite_fetch_rejects_duplicate_dois(self):
        payload = {
            "data": [
                self.datacite_record("10.7937/DUPLICATE"),
                self.datacite_record("10.7937/duplicate"),
            ],
            "meta": {"total": 2},
            "links": {"next": None},
        }
        with mock.patch.object(SNAPSHOT, "fetch_json", return_value=(payload, {})):
            with self.assertRaisesRegex(RuntimeError, "duplicate DOI records"):
                SNAPSHOT.fetch_datacite_prefix()

    def test_datacite_fetch_rejects_incomplete_result(self):
        payload = {
            "data": [self.datacite_record("10.7937/only")],
            "meta": {"total": 2},
            "links": {"next": None},
        }
        with mock.patch.object(SNAPSHOT, "fetch_json", return_value=(payload, {})):
            with self.assertRaisesRegex(RuntimeError, "1 rows but advertised 2"):
                SNAPSHOT.fetch_datacite_prefix()

    def test_datacite_fetch_rejects_next_url_outside_prefix(self):
        payload = {
            "data": [self.datacite_record("10.7937/only")],
            "meta": {"total": 2},
            "links": {
                "next": (
                    "https://api.datacite.org/dois?prefix=10.9999&"
                    "page%5Bcursor%5D=opaque&page%5Bsize%5D=1000"
                )
            },
        }
        with mock.patch.object(SNAPSHOT, "fetch_json", return_value=(payload, {})):
            with self.assertRaisesRegex(RuntimeError, "unexpected next URL"):
                SNAPSHOT.fetch_datacite_prefix()

    def test_datacite_table_enforces_case_insensitive_doi_uniqueness(self):
        with sqlite3.connect(":memory:") as conn:
            SNAPSHOT.create_schema(conn)
            SNAPSHOT.insert_datacite(conn, [self.datacite_record("10.7937/Example")])
            with self.assertRaises(sqlite3.IntegrityError):
                SNAPSHOT.insert_datacite(conn, [self.datacite_record("10.7937/example")])

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
