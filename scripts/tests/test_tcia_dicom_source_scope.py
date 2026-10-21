import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    pd = None

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import tcia_dicom_source_scope as scope
spec = importlib.util.spec_from_file_location("clinical_scope_test", SCRIPTS / "tcia_clinical_metadata.py")
clinical = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clinical)


class ManifestParserTests(unittest.TestCase):
    def test_manifest_format_and_duplicate_series(self):
        self.assertEqual(scope.parse_tcia_manifest(
            b"\xef\xbb\xbfmanifestVersion=3.0\r\nListOfSeriesToDownload=\r\n1.2.3\r\n1.2.3\r\n1.2.4\r\n"
        ), ["1.2.3", "1.2.4"])

    def test_invalid_selections_fail_closed(self):
        for data in (b"<html>error</html>", b"ListOfSeriesToDownload=\n",
                     b"ListOfSeriesToDownload=\nnot-a-uid", b"ListOfSeriesToDownload=\n1.2\nListOfSeriesToDownload="):
            with self.subTest(data=data), self.assertRaises(ValueError):
                scope.parse_tcia_manifest(data)


@unittest.skipIf(pd is None, "pandas is a pinned build dependency")
class SourceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot.sqlite"
        with sqlite3.connect(self.snapshot) as conn:
            conn.executescript("""
                CREATE TABLE agent_datasets (source TEXT, short_title TEXT, title TEXT,
                    link TEXT, hidden INTEGER, source_collections TEXT);
                INSERT INTO agent_datasets VALUES
                    ('collections','Source-A','Source A','https://example/a',0,''),
                    ('collections','Source-B','Source B','https://example/b',0,''),
                    ('analysis-results','Result','Result','https://example/result',0,'');
                CREATE TABLE agent_current_downloads (download_id TEXT, short_title TEXT,
                    parent_source TEXT, hidden INTEGER, download_url TEXT, search_url TEXT,
                    file_types TEXT, controlled_access INTEGER);
                INSERT INTO agent_current_downloads VALUES
                    ('101','Result','analysis-results',0,
                     'https://www.cancerimagingarchive.net/wp-content/uploads/cohort.tcia',
                     'https://portal.imaging.datacommons.cancer.gov/explore?shared_cart=opaque',
                     'DICOM',0);
            """)
        self.manifest = b"ListOfSeriesToDownload=\n1.2.3\n1.2.4\n"
        self.rows = [
            {"SeriesInstanceUID": "1.2.3", "collection_id": "source_a", "PatientID": "P-1"},
            {"SeriesInstanceUID": "1.2.4", "collection_id": "source_b", "PatientID": "P-2"},
            {"SeriesInstanceUID": "9.9.9", "collection_id": "source_a", "PatientID": "OUTSIDE"},
        ]

    def resolve(self, fetch=None):
        return scope.resolve_scopes(self.snapshot, index_frame=pd.DataFrame(self.rows),
            idc_version="v-test", fetch_manifest=fetch or (lambda url: self.manifest))

    def test_navigation_changes_do_not_change_scope_or_expand_selection(self):
        reports = []
        for search_url in (
            "https://nbia.cancerimagingarchive.net/nbia-search/?CollectionCriteria=Source-A",
            "https://portal.imaging.datacommons.cancer.gov/explore?filters=collection_id:source_a",
            "https://portal.imaging.datacommons.cancer.gov/explore?shared_cart=unrelated-cart-name",
        ):
            with sqlite3.connect(self.snapshot) as conn:
                conn.execute("UPDATE agent_current_downloads SET search_url=?", (search_url,))
            report = self.resolve()
            self.assertFalse(report["blocked"])
            reports.append(report["relationships"])
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])
        self.assertEqual([r["participant_ids"] for r in reports[0]], [["P-1"], ["P-2"]])
        self.assertNotIn("OUTSIDE", json.dumps(reports))

    def test_missing_conflicting_or_unknown_series_blocks_migrated_download(self):
        original = list(self.rows)
        for rows in (
            original[:1],
            original + [{"SeriesInstanceUID": "1.2.3", "collection_id": "source_a", "PatientID": "OTHER"}],
            [{**row, "collection_id": "unknown"} for row in original],
        ):
            with self.subTest(rows=rows):
                self.rows = rows
                result = self.resolve()
                self.assertTrue(result["blocked"])
                self.assertEqual(result["relationships"], [])

    def test_colliding_patient_namespaces_do_not_merge(self):
        self.rows[1]["PatientID"] = "P-1"
        result = self.resolve()
        self.assertTrue(result["blocked"])
        self.assertEqual(result["relationships"], [])
        self.assertEqual(result["downloads"][0]["status"], "ambiguous_patient_namespace")

    def test_index_lag_uses_exact_public_nbia_metadata_with_provenance(self):
        self.rows = self.rows[:1]
        def fallback(uids):
            self.assertEqual(uids, ["1.2.4"])
            return [{"SeriesInstanceUID": "1.2.4", "Collection": "Source-B", "PatientID": "P-2"}], ["a" * 64]
        report = scope.resolve_scopes(self.snapshot, index_frame=pd.DataFrame(self.rows),
            idc_version="v-test", fetch_manifest=lambda url: self.manifest, fetch_missing_series=fallback)
        self.assertFalse(report["blocked"])
        self.assertEqual(report["downloads"][0]["idc_missing_series"], 1)
        self.assertEqual(report["nbia_metadata_sha256"], ["a" * 64])
        self.assertEqual(len(report["relationships"][1]["downloads"][0]["nbia_mapping_sha256"]), 64)
        reordered = scope.resolve_scopes(self.snapshot, index_frame=pd.DataFrame(self.rows),
            idc_version="v-test", fetch_manifest=lambda url: self.manifest,
            fetch_missing_series=lambda uids: (fallback(uids)[0], ["b" * 64]))
        self.assertEqual(report["relationships"], reordered["relationships"])
        with sqlite3.connect(self.snapshot) as conn:
            conn.execute("UPDATE agent_current_downloads SET search_url='https://nbia.cancerimagingarchive.net/nbia-search/'")
        nbia_navigation = scope.resolve_scopes(self.snapshot, index_frame=pd.DataFrame(self.rows),
            idc_version="v-test", fetch_manifest=lambda url: self.manifest,
            fetch_missing_series=fallback)
        self.assertEqual(report["relationships"], nbia_navigation["relationships"])

    def test_nbia_cannot_override_ambiguous_idc_or_supply_unbound_outside_rows(self):
        self.rows = self.rows[:1]
        for rows, hashes in (
            ([{"SeriesInstanceUID": "9.9.9", "Collection": "Source-B", "PatientID": "P-2"}], ["a" * 64]),
            ([{"SeriesInstanceUID": "1.2.4", "Collection": "Source-B", "PatientID": "P-2"}], []),
        ):
            report = scope.resolve_scopes(self.snapshot, index_frame=pd.DataFrame(self.rows),
                idc_version="v-test", fetch_manifest=lambda url: self.manifest,
                fetch_missing_series=lambda uids: (rows, hashes))
            self.assertTrue(report["blocked"])
        self.rows.append({"SeriesInstanceUID": "1.2.3", "collection_id": "source_b", "PatientID": "OTHER"})
        def fallback(uids):
            self.assertEqual(uids, ["1.2.4"])
            return [{"SeriesInstanceUID": "1.2.4", "Collection": "Source-B", "PatientID": "P-2"}], ["a" * 64]
        self.assertTrue(scope.resolve_scopes(self.snapshot, index_frame=pd.DataFrame(self.rows),
            idc_version="v-test", fetch_manifest=lambda url: self.manifest,
            fetch_missing_series=fallback)["blocked"])

    def test_fetch_failure_is_sanitized_and_blocks_migrated_download(self):
        def fail(url):
            raise RuntimeError("https://secret.example/?token=credential")
        report = self.resolve(fail)
        self.assertTrue(report["blocked"])
        self.assertNotIn("credential", json.dumps(report))
        self.assertNotIn("P-1", json.dumps(scope.diagnostics_only(report)))

    def test_partial_target_scope_is_not_published_when_another_manifest_fails(self):
        with sqlite3.connect(self.snapshot) as conn:
            conn.execute("INSERT INTO agent_current_downloads VALUES "
                "('102','Result','analysis-results',0,"
                "'https://www.cancerimagingarchive.net/wp-content/uploads/missing.tcia',"
                "'https://nbia.cancerimagingarchive.net/nbia-search/','DICOM',0)")
        def fetch(url):
            if url.endswith('missing.tcia'):
                raise ValueError('unavailable')
            return self.manifest
        result = self.resolve(fetch)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["relationships"], [])

    def test_controlled_and_hidden_downloads_are_not_fetched(self):
        with sqlite3.connect(self.snapshot) as conn:
            conn.execute("UPDATE agent_current_downloads SET controlled_access=1")
        result = self.resolve(lambda url: self.fail("must not fetch controlled metadata"))
        self.assertEqual(result["downloads"], [])
        with sqlite3.connect(self.snapshot) as conn:
            conn.execute("UPDATE agent_current_downloads SET controlled_access=0,hidden=1")
        self.assertEqual(self.resolve()["downloads"], [])

    def test_inheritance_is_restricted_to_each_source_manifest_patients(self):
        result = self.resolve()
        conn = clinical.init_db(self.root / "clinical.sqlite", replace=True)
        self.addCleanup(conn.close)
        for title, patients in (("Source-A", ["P-1", "P-2", "OUTSIDE"]), ("Source-B", ["P-2", "P-1"])):
            source_id = "idc:" + title
            clinical.insert_source(conn, source_id=source_id, source_kind="idc_clinical",
                short_title=title, source_signature_value=source_id)
            for number, patient in enumerate(patients, 1):
                clinical.insert_row_and_facts(conn, source_id=source_id, source_kind="idc_clinical",
                    short_title=title, subject_id=patient, table_name="clinical", row_number=number,
                    row={"PatientID": patient}, facts=[("sex_at_birth", "Female", "sex", None)],
                    has_imaging=True)
        clinical.materialize_clinical_qc(conn)
        clinical.materialize_subjects(conn)
        summary = clinical.inherit_analysis_result_clinical_facts(conn, self.snapshot,
            reviewed_participants_csv=None, manifest_scope=result)
        self.assertEqual(summary["inherited_facts"], 2)
        facts = conn.execute("SELECT subject_id,provenance_json FROM clinical_facts WHERE short_title='Result'").fetchall()
        self.assertEqual({row[0] for row in facts}, {"P-1", "P-2"})
        for patient, raw in facts:
            provenance = json.loads(raw)
            self.assertEqual(provenance["inherited_from_short_title"], "Source-A" if patient == "P-1" else "Source-B")
            evidence = json.loads(provenance["relationship_evidence"])
            self.assertEqual(evidence["idc_version"], "v-test")
            self.assertEqual(len(evidence["downloads"][0]["manifest_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
