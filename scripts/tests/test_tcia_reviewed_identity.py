import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tcia_participant_inventory as api
import tcia_reviewed_identity as reviewed


class ReviewedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(api.SCHEMA)
        self.addCleanup(self.db.close)

    def person(self, title, identifier, kind='Collection'):
        return api.ensure_participant(self.db, kind, title, identifier,
            managed_system='tcia_wordpress', namespace='test', evidence='fixture', provenance={})

    def reference(self, row):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'reviewed.json'
        path.write_text(json.dumps(dict(schema_version=1, records=[row], source_sets={})))
        return path

    def row(self, kind='collection_equivalence'):
        return dict(kind=kind, left=dict(dataset_type='Collection', short_title='A', identifier='1'),
                    right=dict(dataset_type='Collection', short_title='B', identifier='2'),
                    method='official_fixture_crosswalk', evidence=dict(url='https://example.org/crosswalk'))

    def test_complete_reference_has_expected_finite_scope(self):
        rows = reviewed.load_reference()['records']
        from collections import Counter
        self.assertEqual(Counter(r['kind'] for r in rows),
                         dict(source_link=883, replace_source_links=45, within_dataset_alias=26, collection_equivalence=119))

    def test_collection_equivalence_requires_both_endpoints(self):
        row=self.row(); self.person('A','1')
        counts=reviewed.apply(self.db, aliases=False, api=api, reference=self.reference(row))
        self.assertEqual(counts['missing_endpoints'],1)
        self.assertEqual(self.db.execute('select count(*) from participant_equivalences').fetchone()[0],0)
        self.person('B','2')
        counts=reviewed.apply(self.db, aliases=False, api=api, reference=self.reference(row))
        self.assertEqual(counts['collection_equivalences'],1)
        self.assertEqual(self.db.execute('select count(*) from participant_source_links').fetchone()[0],0)

    def test_alias_preserves_raw_identifier_and_assets(self):
        row=self.row('within_dataset_alias'); row['right']['short_title']='A'
        a=self.person('A','1'); self.person('A','2')
        before=self.db.execute('select count(*) from participant_identifiers').fetchone()[0]
        reviewed.apply(self.db, aliases=True, api=api, reference=self.reference(row))
        self.assertEqual(self.db.execute('select count(*) from participants').fetchone()[0],1)
        self.assertEqual(self.db.execute('select count(*) from participant_identifiers').fetchone()[0],before)
        self.assertFalse(self.db.execute('pragma foreign_key_check').fetchall())

    def test_alias_cannot_cross_scope(self):
        with self.assertRaises(ValueError): reviewed.load_reference(self.reference(self.row('within_dataset_alias')))

    def test_source_link_cannot_mislabel_collection_as_result(self):
        with self.assertRaises(ValueError): reviewed.load_reference(self.reference(self.row('source_link')))


if __name__=='__main__': unittest.main()
