import importlib.util
import io
import json
import sqlite3
import sys
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
    def test_wordpress_fetch_retains_hidden_records_across_pages(self):
        visible = {"id": 1, "collection_short_title": "VISIBLE", "hide_from_browse_table": "No"}
        hidden = {"id": 2, "collection_short_title": "HIDDEN", "hide_from_browse_table": "Yes"}
        with mock.patch.object(SNAPSHOT, "fetch_json", side_effect=[
            ({"results": [visible], "total_pages": 2, "include_hidden": True}, {}),
            ({"results": [hidden], "total_pages": 2, "include_hidden": True}, {}),
        ]) as fetch:
            records = SNAPSHOT.fetch_wordpress_endpoint("collections")
        self.assertEqual(records, [visible, hidden])
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [
            f"{SNAPSHOT.BASE_WORDPRESS_URL}/collections/?per_page=100&page={page}&v=1&include_hidden=1"
            for page in (1, 2)
        ])

    def test_wordpress_fetch_rejects_filtered_or_malformed_mode_on_any_page(self):
        for endpoint in ("collections", "analysis-results", "collections/licenses"):
            for page in (1, 2):
                for mode in (False, "true", None):
                    with self.subTest(endpoint=endpoint, page=page, mode=mode):
                        responses = [({"results": [], "total_pages": 2, "include_hidden": True}, {})
                                     for _ in range(2)]
                        responses[page - 1][0]["include_hidden"] = mode
                        with mock.patch.object(SNAPSHOT, "fetch_json", side_effect=responses):
                            with self.assertRaisesRegex(RuntimeError, f"page {page} did not honor"):
                                SNAPSHOT.fetch_wordpress_endpoint(endpoint)

    def test_wordpress_fetch_supports_legacy_mode_and_unaffected_endpoints(self):
        for endpoint in ("collections", "analysis-results", "collections/licenses", "downloads", "versions"):
            with self.subTest(endpoint=endpoint):
                with mock.patch.object(SNAPSHOT, "fetch_json", return_value=({"results": []}, {})):
                    self.assertEqual(SNAPSHOT.fetch_wordpress_endpoint(endpoint), [])
                self.assertEqual("include_hidden=1" in SNAPSHOT.wordpress_url(endpoint, 1),
                                 endpoint not in {"downloads", "versions"})

    def test_v25_empty_arrays_and_hidden_curator_lookup_use_real_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "snapshot.sqlite"
            records = [
                {"id": ident, "result_short_title": title, "hide_from_browse_table": flag,
                 "related_collections": [], "related_analysis_results": [],
                 "citations": [], "collection_downloads": [], "result_downloads": []}
                for ident, title, flag in ((1, "VISIBLE", "No"), (2, "HIDDEN", "Yes"))
            ]
            with sqlite3.connect(db) as conn:
                SNAPSHOT.create_schema(conn)
                SNAPSHOT.insert_wordpress(conn, "analysis-results", records)
                SNAPSHOT.insert_wordpress_downloads(conn, [], records, [])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM wordpress_downloads").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT short_title FROM agent_datasets").fetchall(), [("VISIBLE",)])
            self.assertEqual([r["short_title"] for r in SNAPSHOT.search_wordpress_records(path=db)], ["VISIBLE"])
            selected = SNAPSHOT.search_wordpress_records(short_titles={"HIDDEN"}, include_hidden=True, path=db)
            self.assertEqual(len(selected), 1)
            self.assertTrue(selected[0]["hidden"])
            self.assertEqual(selected[0]["hide_from_browse_table"], "Yes")
            self.assertEqual(selected[0]["current_downloads"], [])

            spec = importlib.util.spec_from_file_location("curator_search", SCRIPT.with_name("tcia_wordpress_search.py"))
            search = importlib.util.module_from_spec(spec)
            with mock.patch.dict(sys.modules, {"tcia_snapshot": SNAPSHOT}):
                spec.loader.exec_module(search)
            for include_hidden in (False, True):
                with self.subTest(include_hidden=include_hidden):
                    args = ["--snapshot-db", str(db), "--short-title", "HIDDEN", "--json"]
                    if include_hidden:
                        args.append("--include-hidden")
                    with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                         mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                        self.assertEqual(search.main(args), 0)
                    self.assertEqual(len(json.loads(stdout.getvalue())), int(include_hidden))
                    self.assertEqual("Curator review" in stderr.getvalue(), include_hidden)

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

    def test_datacite_normalization_preserves_creators_and_event_metrics(self):
        record = self.datacite_record("10.7937/example")
        record["attributes"].update(
            {
                "creators": [{"name": "Doe, Jane", "nameType": "Personal"}],
                "publisher": "The Cancer Imaging Archive",
                "publicationYear": 2025,
                "types": {"resourceType": "Collection", "resourceTypeGeneral": "Dataset"},
                "citationCount": 7,
                "viewCount": 11,
                "downloadCount": 13,
                "referenceCount": 17,
            }
        )
        normalized = SNAPSHOT.normalize_datacite(record)
        self.assertEqual(normalized["creators"][0]["name"], "Doe, Jane")
        self.assertEqual(normalized["citation_count"], 7)
        self.assertEqual(normalized["download_count"], 13)

    def test_publications_xml_is_verified_use_and_links_dataset_dois(self):
        records = SNAPSHOT.parse_publications_xml(
            b"""<?xml version='1.0' encoding='UTF-8'?>
            <xml><records><record>
              <rec-number>42</rec-number><ref-type name='Journal Article'/>
              <contributors><authors><author>Doe, Jane</author></authors></contributors>
              <titles><title>Analyzing TCIA</title><secondary-title>Imaging Journal</secondary-title></titles>
              <dates><year>2026</year></dates><electronic-resource-num>10.1000/article</electronic-resource-num>
              <accession-num>123456</accession-num>
              <remote-database-name>TCIA dataset 10.7937/EXAMPLE.</remote-database-name>
            </record></records></xml>"""
        )
        self.assertEqual(records[0]["authors"], ["Doe, Jane"])
        self.assertEqual(records[0]["pmid"], "123456")
        self.assertEqual(records[0]["linked_tcia_dataset_dois"], ["10.7937/example"])

    def test_dataset_impact_view_combines_datacite_and_verified_use(self):
        with sqlite3.connect(":memory:") as conn:
            SNAPSHOT.create_schema(conn)
            normalized = {"short_title": "EXAMPLE", "title": "Example"}
            conn.execute(
                """INSERT INTO wordpress_records
                   (source,id,slug,short_title,short_title_key,doi,title,link,date_updated,
                    hidden,normalized_json,raw_json,search_text)
                   VALUES ('collections','1','example','EXAMPLE','example','10.7937/example',
                           'Example','https://example.org','','0',?,'{}','example')""",
                (json.dumps(normalized),),
            )
            record = self.datacite_record("10.7937/example")
            record["attributes"].update({"citationCount": 3, "viewCount": 4})
            SNAPSHOT.insert_datacite(conn, [record])
            SNAPSHOT.insert_publications(
                conn,
                [{
                    "rec_number": "1", "ref_type": "Journal Article", "title": "Study",
                    "authors": ["Doe, Jane"], "first_author": "Doe, Jane", "journal": "J",
                    "year": "2026", "doi": "10.1000/study", "pmid": "", "accession_num": "",
                    "keywords": [], "abstract": "", "notes": "",
                    "linked_tcia_dataset_dois": ["10.7937/example"],
                    "remote_database_name": "10.7937/example",
                }],
            )
            row = conn.execute("SELECT * FROM agent_dataset_impact").fetchone()
            self.assertEqual(row[8], 3)
            self.assertEqual(row[12], 1)

    def test_snapshot_build_includes_datacite_and_verified_use_sources(self):
        publication = {
            "rec_number": "1", "ref_type": "Journal Article", "title": "Study",
            "authors": ["Doe, Jane"], "first_author": "Doe, Jane", "journal": "J",
            "year": "2026", "doi": "10.1000/study", "pmid": "", "accession_num": "",
            "keywords": [], "abstract": "", "notes": "",
            "linked_tcia_dataset_dois": ["10.7937/example"],
            "remote_database_name": "10.7937/example",
        }
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "snapshot.sqlite"
            with (
                mock.patch.object(SNAPSHOT, "fetch_wordpress_endpoint", return_value=[]),
                mock.patch.object(SNAPSHOT, "fetch_pathdb_rows", return_value=[]),
                mock.patch.object(
                    SNAPSHOT, "fetch_datacite_prefix",
                    return_value=[self.datacite_record("10.7937/example")],
                ),
                mock.patch.object(SNAPSHOT, "fetch_publication_records", return_value=[publication]),
            ):
                manifest = SNAPSHOT.build_snapshot(db, quiet=True)
            self.assertEqual(manifest["schema_version"], 9)
            self.assertEqual(manifest["counts"]["datacite_dois"], 1)
            self.assertEqual(manifest["counts"]["tcia_verified_analytical_publications"], 1)
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_tcia_publications").fetchone()[0], 1)

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
