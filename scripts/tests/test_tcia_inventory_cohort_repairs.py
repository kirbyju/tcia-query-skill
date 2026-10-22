import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tcia_participant_inventory as inventory
import tcia_public_non_dicom_metadata as public
import tcia_reviewed_identity as identity


class InventoryCohortRepairsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = inventory.connect(self.root / 'inventory.sqlite')
        self.conn.executescript(inventory.SCHEMA)
        self.addCleanup(self.conn.close)

    def person(self, title, name, domain=None, version='current'):
        key = inventory.ensure_participant(
            self.conn, 'Collection', title, name, managed_system='tcia_wordpress',
            namespace=f'tcia_dataset:{title}', evidence='fixture', provenance={})
        if domain:
            inventory.add_asset(self.conn, {
                'participant_asset_id': name + title, 'participant_key': key,
                'managed_system': 'crdc_idc', 'source_artifact': 'idc_participant_projection',
                'access_level': 'open', 'data_domain': domain, 'source_version': version,
                'has_file_level_metadata': 0, 'inventory_status': 'known', 'provenance_json': '{}'})
        return key

    def test_nlst_inventory_retains_current_pathology_and_ct_but_not_clinical_only_or_legacy(self):
        clinical = self.root / 'clinical.sqlite'
        with sqlite3.connect(clinical) as c:
            c.execute('CREATE TABLE agent_clinical_all_subjects '
                      '(short_title, subject_id, source_kinds, has_imaging)')
            c.executemany('INSERT INTO agent_clinical_all_subjects VALUES (?,?,?,?)', [
                ('NLST', 'ct', 'idc_clinical', 1), ('NLST', 'path', 'idc_clinical', 0),
                ('NLST', 'clinical-only', 'idc_clinical', 0), ('NLST', 'old', 'idc_clinical', 1),
                ('Other', 'clinical-only', 'idc_clinical', 0)])
            c.execute('CREATE TABLE clinical_imaging_subjects (short_title,subject_id,imaging_source)')
            c.execute("INSERT INTO clinical_imaging_subjects VALUES ('NLST','historical','legacy_idc_index')")
            c.execute('CREATE TABLE agent_clinical_facts (short_title,subject_id,concept,value_text,'
                      'value_resolved,source_kind,source_url,original_column,evidence_scope,'
                      'is_inferred,qc_status,provenance_json,qc_excluded)')
            for subject in ['ct', 'clinical-only', 'historical']:
                c.execute('INSERT INTO agent_clinical_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          ('NLST', subject, 'sex', 'F', 'Female', 'idc_clinical', '', 'gender',
                           'patient', 0, '', '{}', 0))
        self.person('NLST', 'ct', 'radiology')
        self.person('NLST', 'path', 'pathology')
        self.person('NLST', 'old', 'radiology', version='legacy')
        inventory.ingest_clinical(self.conn, clinical, {'nlst': ('Collection', 'NLST'),
                                  'other': ('Collection', 'Other')}, include_clinical_values=True)
        ids = {r[0] for r in self.conn.execute("SELECT p.display_participant_id FROM participants p "
                "JOIN participant_assets a USING(participant_key) WHERE p.short_title='NLST' "
                "AND a.data_domain='clinical'")}
        self.assertEqual(ids, {'ct', 'path'})
        self.assertFalse(self.conn.execute("SELECT 1 FROM participants WHERE short_title='NLST' "
                                          "AND display_participant_id IN ('clinical-only','historical')").fetchall())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM participant_clinical_values').fetchone()[0], 1)
        self.assertTrue(self.conn.execute("SELECT 1 FROM participants WHERE short_title='Other'").fetchone())
        with sqlite3.connect(clinical) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM agent_clinical_all_subjects').fetchone()[0], 5)

    def cbis_source(self, patient='P_00038', path_patient='P_00038'):
        clinical = self.root / 'clinical.sqlite'
        with sqlite3.connect(clinical) as c:
            c.execute('CREATE TABLE clinical_rows (short_title,subject_id,table_name,row_json,row_sha256)')
            row = {'patient_id': patient, 'image_file_path': f'Calc-Test_{path_patient}_LEFT_CC/study/series/file.dcm'}
            payload = json.dumps(row)
            c.execute('INSERT INTO clinical_rows VALUES (?,?,?,?,?)', ('CBIS-DDSM', patient,
                      'cbis_ddsm_calc_case_description_test_set', payload,
                      hashlib.sha256(payload.encode()).hexdigest()))
        return clinical

    def test_cbis_merges_only_documented_case_ids_in_its_own_dataset_and_preserves_assets(self):
        clinical = self.cbis_source()
        canonical = self.person('CBIS-DDSM', 'P_00038', 'clinical')
        alias = self.person('CBIS-DDSM', 'Calc-Test_P_00038_LEFT_CC_1', 'radiology')
        self.person('CBIS-DDSM', 'P_00038_LEFT_MLO.dcm', 'radiology')
        self.person('CBIS-DDSM', 'unexpected_P_00038', 'radiology')
        self.person('CBIS-DDSM', 'P_99999_LEFT_CC.dcm', 'radiology')
        self.person('Other', 'Calc-Test_P_00038_LEFT_CC_1', 'radiology')
        raw_ids = self.conn.execute('SELECT COUNT(*) FROM participant_identifiers').fetchone()[0]
        assets = set(self.conn.execute('SELECT participant_asset_id FROM participant_assets'))
        result = identity.apply_cbis_ddsm_aliases(self.conn, clinical, api=inventory)
        self.assertEqual(result['cbis_ddsm_aliases'], 2)
        self.assertFalse(self.conn.execute('SELECT 1 FROM participants WHERE participant_key=?', (alias,)).fetchone())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM participant_assets WHERE participant_key=?',
                                          (canonical,)).fetchone()[0], 3)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM participant_identifiers').fetchone()[0], raw_ids)
        self.assertEqual(set(self.conn.execute('SELECT participant_asset_id FROM participant_assets')), assets)
        self.assertFalse(self.conn.execute('PRAGMA foreign_key_check').fetchall())
        self.assertEqual(identity.apply_cbis_ddsm_aliases(self.conn, clinical, api=inventory)['cbis_ddsm_aliases'], 0)
        self.conn.commit()
        path = self.root / 'inventory.sqlite'
        self.assertTrue(inventory.validate_database(path)['ok'])
        # Evidence for one alias must not authorize an unrelated identifier.
        self.conn.execute("UPDATE participant_identifiers SET normalized_identifier='unreviewed' "
                          "WHERE participant_key=? AND raw_identifier='Calc-Test_P_00038_LEFT_CC_1'",
                          (canonical,))
        self.conn.execute("DELETE FROM participant_identity_evidence WHERE "
                          "json_extract(evidence_json,'$.raw_identifier')='Calc-Test_P_00038_LEFT_CC_1'")
        self.conn.commit()
        self.assertFalse(inventory.validate_database(path)['ok'])

    def test_cbis_rejects_disagreement_with_the_official_image_path(self):
        clinical = self.cbis_source(path_patient='P_99999')
        self.person('CBIS-DDSM', 'P_00038')
        with self.assertRaisesRegex(RuntimeError, 'patient-specific image path'):
            identity.apply_cbis_ddsm_aliases(self.conn, clinical, api=inventory)

    def tompei_snapshot(self, url=None):
        snapshot = self.root / 'snapshot.sqlite'
        with sqlite3.connect(snapshot) as c:
            c.execute('CREATE TABLE agent_current_downloads '
                      '(download_row_id,dataset_type,short_title,download_id,download_url,'
                      'file_types,data_types,hidden,controlled_access)')
            c.execute('INSERT INTO agent_current_downloads VALUES (?,?,?,?,?,?,?,?,?)',
                      (99, 'Analysis Result', 'TOMPEI-CMMD', '50085', url or
                       'https://www.cancerimagingarchive.net/wp-content/uploads/TOMPEI-CMMD_v01_20250123.zip',
                       '["JSON","ZIP"]', '["Segmentation"]', 0, 0))
        return snapshot

    def public_conn(self):
        c = public.connect(self.root / 'public.sqlite')
        c.executescript(public.SCHEMA)
        public.insert_vocab(c)
        c.execute("INSERT INTO artifact_meta VALUES ('schema_version','8')")
        self.addCleanup(c.close)
        return c

    def test_exact_tompei_inventory_links_all_annotations_without_inventing_source_images(self):
        c = self.public_conn()
        snapshot = self.tompei_snapshot()
        self.assertEqual(public.ingest_tompei_cmmd_annotations(c, snapshot), {'files': 1385, 'participants': 1363})
        self.assertEqual(c.execute('SELECT COUNT(*) FROM public_non_dicom_assets').fetchone()[0], 1385)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM public_non_dicom_asset_participants').fetchone()[0], 1385)
        self.assertEqual(c.execute("SELECT DISTINCT imaging_domain,object_role,file_format,media_kind "
                                   "FROM public_non_dicom_assets").fetchall()[0][:],
                         ('imaging_annotation', 'segmentation', 'JSON', 'geometric_annotation'))
        self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
        c.commit()
        self.assertTrue(public.validate_database(self.root / 'public.sqlite')['ok'])
        public.ingest_tompei_cmmd_annotations(c, snapshot)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM public_non_dicom_assets').fetchone()[0], 1385)
        self.assertEqual(public.delete_refreshable_assets(c), 1385)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM public_non_dicom_asset_participants').fetchone()[0], 0)

    def test_tompei_rejects_changed_source_or_corrupt_inventory_before_writing(self):
        c = self.public_conn()
        with self.assertRaisesRegex(RuntimeError, 'differs from reviewed package'):
            public.ingest_tompei_cmmd_annotations(c, self.tompei_snapshot('https://example.org/new.zip'))
        (self.root / 'snapshot.sqlite').unlink()
        broken = self.root / 'broken.csv'
        broken.write_text('bad inventory')
        with self.assertRaisesRegex(RuntimeError, 'digest mismatch'):
            public.ingest_tompei_cmmd_annotations(c, self.tompei_snapshot(), inventory_path=broken)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM public_non_dicom_assets').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
