import sqlite3
import unittest

from scripts.tcia_download_requirements import resolve_download, software_catalog
from scripts import tcia_snapshot as snapshot


class DownloadRequirementTests(unittest.TestCase):
    def test_declared_aspera_and_retriever_have_compact_stable_codes(self):
        cases = [
            (
                "IBM-Aspera-Connect plugin",
                "https://faspex.cancerimagingarchive.net/?context=EXAMPLE",
                "aspera",
                "aspera_package",
            ),
            (
                "TCIA Data Retriever",
                "https://www.cancerimagingarchive.net/data.csv",
                "tcia_data_retriever",
                "tabular_manifest",
            ),
            (
                "TCIA Data Retriever",
                "https://www.cancerimagingarchive.net/data.tcia",
                "tcia_data_retriever",
                "dicom_manifest",
            ),
        ]
        for label, url, code, method in cases:
            with self.subTest(label=label, url=url):
                self.assertEqual(
                    resolve_download(dict(requirements_label=label, download_url=url)),
                    dict(
                        software_requirement=code,
                        requirement_status="declared",
                        download_method=method,
                    ),
                )

    def test_missing_declaration_is_flagged_and_unknown_declaration_is_not_overridden(self):
        url = "https://faspex.cancerimagingarchive.net/?context=EXAMPLE"
        self.assertEqual(
            resolve_download({"download_url": url})["requirement_status"], "link_fallback"
        )
        actual = resolve_download(
            {"download_url": url, "requirements_label": "Unrecognized software"}
        )
        self.assertEqual(actual["software_requirement"], "unknown")
        self.assertEqual(actual["download_method"], "unsupported")

    def test_conflict_private_credentials_and_ambiguous_page_do_not_become_executable(self):
        for row in [
            dict(
                requirements_label="TCIA Data Retriever",
                download_url="https://faspex.cancerimagingarchive.net/?context=EXAMPLE",
            ),
            dict(requirements_label="Aspera", download_url="https://example.org/data.zip"),
            dict(download_url="https://user:secret@example.org/data.zip"),
            dict(download_url="https://example.org/download-page"),
        ]:
            self.assertEqual(resolve_download(row)["download_method"], "unsupported")
        self.assertEqual(
            resolve_download(
                dict(
                    requirements_label="TCIA Data Retriever",
                    download_url="https://faspex.cancerimagingarchive.net/",
                )
            )["requirement_status"],
            "conflict",
        )

    def test_snapshot_persists_normalized_fields_and_one_catalog(self):
        with sqlite3.connect(":memory:") as conn:
            snapshot.create_schema(conn)
            raw = {
                "id": "1",
                "download_requirements": {
                    "label": "TCIA Data Retriever",
                    "url": "https://example.org/help",
                    "text": "Download requires a retriever",
                },
                "download_url": "https://www.cancerimagingarchive.net/data.tcia",
                "data_license": {"label": "CC BY 4.0"},
            }
            row = snapshot.normalize_wordpress_download(
                raw, "collections", parent_short_title="EXAMPLE", is_current_version=True
            )
            snapshot.insert_download_row(conn, row, raw)
            names = [
                c[0] for c in conn.execute("SELECT * FROM agent_current_downloads").description
            ]
            stored = dict(
                zip(names, conn.execute("SELECT * FROM agent_current_downloads").fetchone())
            )
            self.assertEqual(stored["requirements_url"], "https://example.org/help")
            self.assertEqual(
                {k: stored[k] for k in resolve_download(stored)}, resolve_download(row)
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM download_software_requirements").fetchone()[0],
                len(software_catalog()),
            )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            snapshot.insert_meta(conn, {"schema_version": snapshot.SCHEMA_VERSION})
            self.assertEqual(snapshot.validate_snapshot_schema(conn)["schema_version"], 9)

    def test_incompatible_snapshot_version_is_rejected(self):
        with sqlite3.connect(":memory:") as conn:
            snapshot.create_schema(conn)
            snapshot.insert_meta(conn, {"schema_version": 8})
            with self.assertRaisesRegex(RuntimeError, "expected 9"):
                snapshot.validate_snapshot_schema(conn)

    def test_valid_codes_cannot_disagree_with_the_declared_requirement(self):
        with sqlite3.connect(":memory:") as conn:
            snapshot.create_schema(conn)
            raw = {
                "id": "1",
                "download_requirements": {"label": "TCIA Data Retriever"},
                "download_url": "https://www.cancerimagingarchive.net/data.tcia",
            }
            row = snapshot.normalize_wordpress_download(
                raw, "collections", parent_short_title="EXAMPLE", is_current_version=True
            )
            snapshot.insert_download_row(conn, row, raw)
            snapshot.insert_meta(conn, {"schema_version": snapshot.SCHEMA_VERSION})
            conn.execute("UPDATE wordpress_downloads SET software_requirement='aspera'")
            with self.assertRaisesRegex(RuntimeError, "disagree"):
                snapshot.validate_snapshot_schema(conn)

    def test_unrecognized_non_ascii_declaration_is_not_a_missing_declaration(self):
        actual = resolve_download(
            {
                "requirements_label": "工具",
                "download_url": "https://faspex.cancerimagingarchive.net/",
            }
        )
        self.assertEqual(actual["software_requirement"], "unknown")
        self.assertEqual(actual["requirement_status"], "unknown")
        self.assertEqual(actual["download_method"], "unsupported")


if __name__ == "__main__":
    unittest.main()
