import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tcia_clinical_only_repairs as repairs
import tcia_clinical_metadata as clinical
import tcia_participant_inventory as inventory
import tcia_public_non_dicom_metadata as public
import tcia_reviewed_identity as identity


class ClinicalOnlyRepairsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_finite_crosswalk_has_reviewed_dataset_scopes_and_no_arbitrary_id_rules(self):
        data = identity.load_reference(repairs.IDENTITY_REFERENCE)
        from collections import Counter
        self.assertEqual(Counter(r['kind'] for r in data['records']),
                         {'within_dataset_alias': 1413, 'collection_equivalence': 266})
        spinal = [r for r in data['records'] if r['left']['short_title']=='Spinal-Multiple-Myeloma-SEG']
        self.assertEqual(len(spinal), 10)
        self.assertTrue(all(r['evidence']['series_uid'] for r in spinal))
        self.assertFalse(any(r['left']['short_title']=='CFB-GBM' and r['left']['identifier']=='89'
                             for r in data['records']))

    def test_finite_alias_validator_requires_evidence_for_that_raw_identifier(self):
        path = self.root / 'inventory.sqlite'
        c = inventory.connect(path);self.addCleanup(c.close);c.executescript(inventory.SCHEMA)
        row = next(r for r in identity.load_reference(repairs.IDENTITY_REFERENCE)['records']
                   if r['left']['short_title']=='CDD-CESM')
        for side in ['left','right']:
            e=row[side]
            inventory.ensure_participant(c,e['dataset_type'],e['short_title'],e['identifier'],
                managed_system='tcia_wordpress',namespace='fixture',evidence='fixture',provenance={})
        ref = self.root / repairs.IDENTITY_REFERENCE.name
        ref.write_text(json.dumps({'schema_version':1,'records':[row],'source_sets':{}}))
        self.assertEqual(identity.apply(c,aliases=True,api=inventory,reference=ref)['aliases'],1)
        c.commit();self.assertTrue(inventory.validate_database(path)['ok'])
        c.execute("UPDATE participant_identity_evidence SET evidence_json=json_set(evidence_json,"
                  "'$.left.identifier','wrong')")
        c.commit();self.assertFalse(inventory.validate_database(path)['ok'])

    def test_conflicting_finite_alias_targets_fail_before_any_merge(self):
        row=copy.deepcopy(next(r for r in identity.load_reference(repairs.IDENTITY_REFERENCE)['records']
                               if r['kind']=='within_dataset_alias'))
        other=copy.deepcopy(row);other['right']['identifier']='different-participant'
        path=self.root/'conflicting.json'
        path.write_text(json.dumps({'schema_version':1,'records':[row,other],'source_sets':{}}))
        with self.assertRaisesRegex(ValueError,'Ambiguous reviewed alias target'):
            identity.load_reference(path)

    def test_acrin_equivalence_groups_only_the_exact_source_asserted_pair(self):
        from tcia_participant_groups import build_groups
        c=inventory.connect(self.root/'groups.sqlite');self.addCleanup(c.close)
        c.executescript(inventory.SCHEMA)
        row=next(r for r in identity.load_reference(repairs.IDENTITY_REFERENCE)['records']
                 if r['kind']=='collection_equivalence')
        for side in ['left','right']:
            e=row[side]
            inventory.ensure_participant(c,e['dataset_type'],e['short_title'],e['identifier'],
                managed_system='tcia_wordpress',namespace='fixture',evidence='fixture',provenance={})
        reference=self.root/repairs.IDENTITY_REFERENCE.name
        reference.write_text(json.dumps({'schema_version':1,'records':[row],'source_sets':{}}))
        identity.apply(c,aliases=False,api=inventory,reference=reference)
        species={('Collection','ISPY2'):'Human',('Collection','ACRIN-6698'):'Human'}
        self.assertEqual(build_groups(c,species)['participant_groups'],1)
        c.execute("UPDATE participant_equivalences SET evidence_json=json_set(evidence_json,"
                  "'$.evidence.source_fields.i_spy_2_research_id','wrong')")
        self.assertEqual(build_groups(c,species)['participant_groups'],2)
        self.assertEqual(c.execute('SELECT reason FROM participant_group_link_review').fetchone()[0],
                         'unsupported_acrin_ispy2_crosswalk_scope')

    def test_ucsd_directory_coverage_has_study_counts_but_no_fabricated_file_counts(self):
        meta=json.loads((repairs.REFERENCES/'ucsd_bmets_study_inventory_v1.json').read_text())
        snapshot=self.root/'snapshot.sqlite'
        with sqlite3.connect(snapshot) as c:
            c.execute('CREATE TABLE agent_current_downloads '
                      '(download_row_id,short_title,download_id,download_url,hidden,controlled_access)')
            c.execute('INSERT INTO agent_current_downloads VALUES (?,?,?,?,0,0)',
                      (1,meta['short_title'],meta['download_id'],meta['source_url']))
        public_path=self.root/'public.sqlite'
        with public.connect(public_path) as c:
            c.executescript(public.SCHEMA);public.insert_vocab(c)
            self.assertEqual(repairs.ingest_ucsd_bmets_studies(c,snapshot,api=public),
                             {'studies':1065,'participants':300})
            c.commit()
        with inventory.connect(self.root/'inventory.sqlite') as c:
            c.executescript(inventory.SCHEMA)
            inventory.ingest_public_non_dicom(c,public_path,
                {'ucsdbmetslongitudinal':('Collection',meta['short_title'])})
            row=c.execute('SELECT COUNT(*),SUM(study_count),SUM(file_count),'
                          'SUM(has_file_level_metadata) FROM participant_assets').fetchone()
            self.assertEqual(tuple(row),(300,1065,None,0))

    def test_hsi_mismatched_asset_batch_is_rejected_before_reassignment(self):
        import csv
        with (repairs.REFERENCES/'histology_hsi_participant_links_v1.csv').open() as f:
            row=next(csv.DictReader(f))
        with public.connect(self.root/'public.sqlite') as c:
            c.executescript(public.SCHEMA);public.insert_vocab(c)
            public.insert_asset(c,{'asset_id':row['asset_id'],'dataset_type':'Collection',
                'short_title':'HistologyHSI-BC-Recurrence','asset_granularity':'file',
                'package_path':'wrong/path','file_format':row['file_format'],
                'subject_id':row['old_subject_id'],'participant_link_status':'unavailable',
                'media_kind':'whole_slide_image','spatial_dimensionality':'2d',
                'temporal_dimensionality':'static','imaging_domain':'pathology',
                'object_role':'source_image','representation_provenance_class':'unknown',
                'source_system':'tcia_aspera','raw_values_json':'{}','provenance_json':'{}',
                'quality_flag_json':'{}'})
            with self.assertRaisesRegex(RuntimeError,'asset identity mismatch'):
                repairs.repair_histology_hsi_links(c,api=public)
            self.assertEqual(c.execute('SELECT subject_id FROM public_non_dicom_assets').fetchone()[0],
                             row['old_subject_id'])

    def test_ldct_rekeys_reused_scanner_rows_per_case_and_preserves_original_values(self):
        c=clinical.init_db(self.root/'clinical.sqlite',replace=False);self.addCleanup(c.close)
        clinical.insert_source(c,source_id='fixture',source_kind='idc_clinical',
                               short_title='LDCT-and-Projection-data',source_signature_value='fixture')
        for number,case in [(2,'L004'),(3,'L030')]:
            c.execute('INSERT INTO clinical_imaging_subjects VALUES (?,?,?,?)',
                      ('ldctandprojectiondata:'+case.lower(),'LDCT-and-Projection-data',case,'idc_index'))
            clinical.insert_row_and_facts(c,source_id='fixture',source_kind='idc_clinical',
                short_title='LDCT-and-Projection-data',subject_id='GE',
                table_name='ldct_and_projection_data_abdomen_v9',row_number=number,
                row={'abdomen_case':case,'dicom_patient_id':'GE','abdomen_complete_diagnosis':'original'},
                facts=[('primary_diagnosis','original','abdomen_complete_diagnosis',None)])
        originals={r[0] for r in c.execute('SELECT row_json FROM clinical_rows')}
        result=repairs.repair_ldct_case_ownership(c,api=clinical)
        self.assertEqual(result,{'rows_rekeyed':2,'corrected_participants':2})
        self.assertEqual({r[0] for r in c.execute('SELECT subject_id FROM clinical_rows')},{'L004','L030'})
        self.assertEqual({r[0] for r in c.execute('SELECT row_json FROM clinical_rows')},originals)
        self.assertEqual({tuple(r) for r in c.execute('SELECT subject_id,value_text FROM clinical_facts')},
                         {('L004','original'),('L030','original')})
        self.assertEqual(repairs.repair_ldct_case_ownership(c,api=clinical)['rows_rekeyed'],0)
        self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())


if __name__=='__main__': unittest.main()
