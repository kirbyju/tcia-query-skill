import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tcia_participant_inventory as inventory
from tcia_participant_groups import build_groups, validate_groups


class ManifestParticipantLinksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.clinical = self.base / "clinical.sqlite"
        self.evidence = json.dumps({"policy_version": 1, "idc_version": "v24@test", "downloads": [
            {"download_id": "1", "manifest_sha256": "a" * 64,
             "series_set_sha256": "b" * 64, "requested_series": 1}
        ]})
        self.provenance = {
            "relationship_id": "relation", "relationship_source": "tcia_manifest_series_crosswalk",
            "relationship_evidence": self.evidence,
            "inherited_from_short_title": "Parent", "inherited_from_subject_id": "001",
        }
        with sqlite3.connect(self.clinical) as c:
            c.execute("CREATE TABLE clinical_dataset_relationships (relationship_id TEXT, "
                      "target_short_title TEXT, source_short_title TEXT, relationship_source TEXT, "
                      "relationship_evidence TEXT)")
            c.execute("INSERT INTO clinical_dataset_relationships VALUES (?,?,?,?,?)",
                      ("relation", "Derived", "Parent", "tcia_manifest_series_crosswalk", self.evidence))
            c.execute("CREATE TABLE clinical_facts (fact_id TEXT,short_title TEXT,subject_id TEXT,"
                      "source_kind TEXT,provenance_json TEXT)")
        self.add_fact("one", self.provenance)

    def add_fact(self, fact_id, provenance):
        with sqlite3.connect(self.clinical) as c:
            c.execute("INSERT INTO clinical_facts VALUES (?,?,?,?,?)",
                      (fact_id, "Derived", "001", "tcia_collection_subject_inheritance", json.dumps(provenance)))

    def test_duplicate_concepts_produce_one_patient_crosswalk(self):
        self.add_fact("two", self.provenance)
        self.assertEqual(len(inventory.manifest_inherited_source_links(self.clinical)), 1)

    def test_broad_legacy_relationship_is_not_promoted(self):
        with sqlite3.connect(self.clinical) as c:
            c.execute("UPDATE clinical_dataset_relationships SET relationship_source='wordpress_source_collections'")
        self.assertEqual(inventory.manifest_inherited_source_links(self.clinical), [])

    def test_mismatched_provenance_is_rejected(self):
        for field, value in [("inherited_from_short_title", "Other"),
                             ("inherited_from_subject_id", "002"),
                             ("relationship_evidence", "{}"),
                             ("relationship_id", "missing")]:
            with self.subTest(field=field), sqlite3.connect(self.clinical) as c:
                c.execute("UPDATE clinical_facts SET provenance_json=?",
                          (json.dumps({**self.provenance, field: value}),))
            with self.assertRaisesRegex(RuntimeError, "inconsistent provenance"):
                inventory.manifest_inherited_source_links(self.clinical)

    def test_missing_manifest_digest_is_rejected(self):
        evidence = json.dumps({"policy_version": 1, "idc_version": "test", "downloads": [{}]})
        with sqlite3.connect(self.clinical) as c:
            c.execute("UPDATE clinical_dataset_relationships SET relationship_evidence=?", (evidence,))
            c.execute("UPDATE clinical_facts SET provenance_json=?",
                      (json.dumps({**self.provenance, "relationship_evidence": evidence}),))
        with self.assertRaisesRegex(RuntimeError, "lacks download evidence"):
            inventory.manifest_inherited_source_links(self.clinical)

    def test_same_patient_id_in_two_source_collections_is_rejected(self):
        with sqlite3.connect(self.clinical) as c:
            c.execute("INSERT INTO clinical_dataset_relationships VALUES (?,?,?,?,?)",
                      ("other", "Derived", "Other", "tcia_manifest_series_crosswalk", self.evidence))
        self.add_fact("other", {**self.provenance, "relationship_id": "other",
                                "inherited_from_short_title": "Other"})
        with self.assertRaisesRegex(RuntimeError, "ambiguous source identity"):
            inventory.manifest_inherited_source_links(self.clinical)

    def test_verified_scope_links_only_selected_patient_and_collection(self):
        c = inventory.connect(self.base / "inventory.sqlite")
        self.addCleanup(c.close)
        c.executescript(inventory.SCHEMA)
        types = {}
        for kind, title, patient in [
            ("Analysis Result", "Derived", "001"), ("Analysis Result", "Derived", "002"),
            ("Collection", "Parent", "001"), ("Collection", "Unrelated", "001"),
        ]:
            types[title.casefold()] = (kind, title)
            inventory.ensure_participant(c, kind, title, patient, managed_system="test",
                                         namespace=title, evidence="test", provenance={})
        inventory.select_display_participant_ids(c)
        with patch.object(inventory, "wordpress_analysis_result_relationships", return_value=[]):
            result = inventory.apply_analysis_result_source_collection_links(
                c, snapshot_db=self.base / "snapshot.sqlite", idc_db=self.base / "missing.sqlite",
                dataset_types=types, clinical_db=self.clinical,
                reviewed_relationships=self.base / "missing.csv",
            )
        self.assertEqual(result["linked_source_collection"], 1)
        rows = c.execute("SELECT source_collection_short_title,source_participant_id,"
                         "resolution_method FROM participant_source_links").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("Parent", "001", "manifest_series_patient_crosswalk")])
        self.assertEqual(c.execute("SELECT cross_dataset_identity_status FROM participants "
                                  "WHERE short_title='Derived' AND display_participant_id='002'").fetchone()[0],
                         "not_asserted")
        groups = build_groups(c, {identity: "Human" for identity in types.values()})
        self.assertEqual(groups["participant_groups"], 3)
        self.assertEqual(groups["rejected_group_links"], 0)
        self.assertEqual(validate_groups(c), [])


if __name__ == "__main__":
    unittest.main()
