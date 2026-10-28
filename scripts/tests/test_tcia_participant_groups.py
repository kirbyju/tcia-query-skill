"""Curated index tests focus on identity safety and reproducible counts."""
import sqlite3
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tcia_participant_inventory as inventory
from tcia_participant_groups import build_groups, validate_groups


class ParticipantGroupTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(inventory.SCHEMA)
        self.addCleanup(self.db.close)
        self.species = {}

    def person(self, title, identifier='1', kind='Collection', species='Human'):
        self.species[(kind, title)] = species
        return inventory.ensure_participant(self.db, kind, title, identifier,
            managed_system='tcia_wordpress', namespace='fixture', evidence='fixture', provenance={})

    def equivalent(self, a, b, key, confidence='high'):
        self.db.execute('INSERT INTO participant_equivalences VALUES (?,?,?,?,?,?,?)',
                        (key, a, b, 'official_collection_overlap_declaration_and_exact_shared_namespace',
                         confidence, 'reviewed.json', '{}'))

    def source(self, result, collection, title, key='source'):
        self.db.execute('INSERT INTO participant_source_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (key, result, title, collection, '1', 'linked_source_collection',
                         'official_reviewed_finite_participant_crosswalk', 'high', 'reviewed.json', key, '', '{}'))

    def test_transitive_consolidation_and_identical_bare_ids_remain_separate(self):
        a, b, c = self.person('A'), self.person('B'), self.person('Unrelated')
        result = self.person('Derived', kind='Analysis Result')
        self.equivalent(a,b,'eq'); self.source(result,b,'B')
        self.assertEqual(build_groups(self.db,self.species)['participant_groups'],2)
        ids = dict(self.db.execute('SELECT participant_key,participant_group_id FROM participant_group_members'))
        self.assertEqual(ids[a],ids[b]); self.assertEqual(ids[a],ids[result]); self.assertNotEqual(ids[a],ids[c])
        self.assertEqual(validate_groups(self.db),[])
        before = list(self.db.execute('SELECT * FROM participant_groups ORDER BY 1'))
        build_groups(self.db,self.species)
        self.assertEqual(before,list(self.db.execute('SELECT * FROM participant_groups ORDER BY 1')))

    def test_conflicting_transitive_component_is_rejected_in_full(self):
        a, a2, b = self.person('A','1'),self.person('A','2'),self.person('B')
        self.equivalent(a,b,'one');self.equivalent(a2,b,'two')
        self.assertEqual(build_groups(self.db,self.species)['participant_groups'],3)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM participant_group_link_review WHERE disposition='rejected'").fetchone()[0],2)
        self.assertEqual(validate_groups(self.db),[])

    def test_unreviewed_edges_and_wrong_endpoint_scopes_are_rejected(self):
        a,b,r = self.person('A'),self.person('B'),self.person('Derived',kind='Analysis Result')
        self.equivalent(a,b,'low',confidence='low');self.equivalent(a,r,'wrong-scope')
        self.assertEqual(build_groups(self.db,self.species)['participant_groups'],3)

    def test_categories_keep_nonhuman_and_mixed_groups_separate(self):
        self.person('Human');self.person('Animal',species='Mouse')
        self.person('Phantom',species='Physical Phantom')
        self.person('Pseudo-PHI-DICOM-Data');self.person('Unknown',species='')
        a=self.person('MixedA');b=self.person('MixedB',species='Mouse');self.equivalent(a,b,'mixed')
        build_groups(self.db,self.species)
        counts=dict(self.db.execute('SELECT subject_category,COUNT(*) FROM participant_groups GROUP BY 1'))
        self.assertEqual(counts,dict(human=1,animal=1,phantom=1,benchmark=1,unclassified=2))

    def test_staff_dispositions_preserve_source_status(self):
        a=self.person('A');r=self.person('Derived',kind='Analysis Result');self.source(r,a,'A')
        for issue_id,title,identifier,status,code in [
            ('resolved','A','1','resolved','source_collection_match_unresolved'),
            ('mapped','Derived','1','open','brats_source_collection_participant_not_current'),
            ('unresolved','A','1','open','ambiguous_source_collection_match'),
            ('explained','TCGA-GBM-QI-Radiogenomics','TCGA-06-0118','open','source_collection_participant_not_current')]:
            if issue_id=='explained':self.person(title,identifier,kind='Analysis Result')
            self.db.execute('INSERT INTO participant_link_issues VALUES (?,?,?,?,?,?,?,?)',
                            (issue_id,'Analysis Result' if title!='A' else 'Collection',title,identifier,code,status,'fixture','{}'))
        build_groups(self.db,self.species)
        self.assertEqual(dict(self.db.execute('SELECT issue_id,disposition FROM participant_issue_dispositions')),
                         dict(resolved='source_resolved',mapped='resolved_by_reviewed_mapping',unresolved='actionable',explained='explained_annotation_only'))
        self.assertEqual(self.db.execute("SELECT status FROM participant_link_issues WHERE issue_id='mapped'").fetchone()[0],'open')

    def test_validation_detects_missing_members_and_wrong_counts(self):
        a=self.person('A');build_groups(self.db,self.species)
        self.db.execute('DELETE FROM participant_group_members WHERE participant_key=?',(a,))
        self.assertEqual(len(validate_groups(self.db)),2)

    def test_accepted_unknown_brats_source_retains_unresolved_evidence_without_merge(self):
        key = self.person('RSNA-ASNR-MICCAI-BraTS-2021', 'BraTS2021_00051', kind='Analysis Result')
        self.db.execute('INSERT INTO participant_link_issues VALUES (?,?,?,?,?,?,?,?)',
                        ('unknown-brats','Analysis Result','RSNA-ASNR-MICCAI-BraTS-2021',
                         'BraTS2021_00051','brats_source_collection_participant_not_current',
                         'open','Original workbook source endpoint absent','{}'))
        build_groups(self.db,self.species)
        self.assertEqual(self.db.execute('SELECT disposition FROM participant_issue_dispositions').fetchone()[0],
                         'accepted_unknown_source_identity')
        self.assertEqual(self.db.execute('SELECT status FROM participant_link_issues').fetchone()[0],'open')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM participant_group_members WHERE participant_key=?',(key,)).fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM participant_source_links').fetchone()[0],0)

    def test_explicit_external_tcga_source_anchor_deduplicates_results_without_fake_collection(self):
        a = self.person('TIL-WSI-TCGA', 'TCGA-AA-0001', kind='Analysis Result')
        b = self.person('Pan-Cancer-Nuclei-Seg', 'TCGA-AA-0001', kind='Analysis Result')
        unrelated = self.person('Unrelated', 'TCGA-AA-0001', kind='Analysis Result')
        anchors = {('GDC_TCGA', 'TCGA-PAAD', 'TCGA-AA-0001'): [a, b]}
        result = build_groups(self.db, self.species, tcga_source_groups=anchors)
        self.assertEqual(result['participant_groups'], 2)
        groups = dict(self.db.execute('SELECT participant_key,participant_group_id FROM participant_group_members'))
        self.assertEqual(groups[a], groups[b])
        self.assertNotEqual(groups[a], groups[unrelated])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM participants WHERE dataset_type='Collection'").fetchone()[0], 0)
        self.assertEqual(validate_groups(self.db), [])

    def test_external_tcga_anchor_rejects_bare_id_or_wrong_participant(self):
        a = self.person('A', '1', kind='Analysis Result')
        b = self.person('B', '1', kind='Analysis Result')
        result = build_groups(self.db, self.species,
            tcga_source_groups={('GDC_TCGA', 'TCGA-PAAD', '1'): [a, b]})
        self.assertEqual(result['participant_groups'], 2)
        self.assertEqual(result['rejected_group_links'], 1)
        result = build_groups(self.db, self.species,
            tcga_source_groups={('GDC_TCGA', 'TCGA-PAAD', 'TCGA-AA-0001'): [a, b]})
        self.assertEqual(result['participant_groups'], 2)


if __name__=='__main__':unittest.main()
