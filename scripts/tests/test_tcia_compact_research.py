"""Storage changes must preserve logical content and rebuildable source evidence."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import tcia_compact_research as compact
from scripts import tcia_controlled_access_metadata as controlled
from scripts import tcia_metadata_change_report as changes
from scripts import tcia_public_non_dicom_metadata as public
from scripts import tcia_v2_audit as audit


class CompactResearchTests(unittest.TestCase):
    def test_non_dicom_roundtrip_and_semantic_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, research = root / 'source.sqlite', root / 'research.sqlite'
            with sqlite3.connect(source) as conn:
                conn.row_factory = sqlite3.Row
                conn.executescript(public.SCHEMA)
                public.insert_vocab(conn)
                conn.execute("INSERT INTO artifact_meta VALUES ('schema_version','9')")
                for i, url in enumerate(['https://example.test/data', '', None]):
                    public.insert_asset(conn, {
                        'asset_id': str(i), 'dataset_type': 'Collection', 'short_title': 'TEST',
                        'asset_granularity': 'file', 'file_format': 'NIFTI',
                        'participant_link_status':'unavailable',
                        'media_kind': 'image_volume', 'spatial_dimensionality': '3D',
                        'temporal_dimensionality': 'static', 'imaging_domain': 'radiology',
                        'object_role': 'source_image', 'modality': 'MR',
                        'source_system': 'tcia_aspera', 'source_url': url,
                        'representation_provenance_class': 'submitted_original',
                        'geometry_status': 'not_checked', 'raw_values_json':'{}',
                        'provenance_json':'{}', 'quality_flag_json':'{}',
                    })
                    # Include a differing modality and missing dimensionality keys.
                    public.merge_image_metadata(conn, str(i),
                        {'file_format': 'NIFTI', 'modality': 'CT', 'object_role': 'source_image'},
                        value_role='source_raw', source_kind='test', source_locator='test',
                        inference_method='direct', confidence='high', priority=100)
                conn.execute("INSERT INTO public_non_dicom_locations(location_id,asset_id,managed_system,system_functions_json,access_url,representation_provenance_class) VALUES ('l0','0','tcia_aspera','[]','https://example.test/data','submitted_original')")
                # Duplicate locations must neither multiply assets nor make URL choice ambiguous.
                conn.execute("INSERT INTO public_non_dicom_locations(location_id,asset_id,managed_system,system_functions_json,access_url,representation_provenance_class) VALUES ('l1','0','tcia_aspera','[]','https://example.test/data','submitted_original')")
                conn.execute("INSERT INTO public_non_dicom_asset_participants(asset_participant_id,asset_id,short_title,subject_id,subject_id_namespace,raw_subject_id,link_status) VALUES ('ap0','0','TEST','P0','test','P0','source_identifier')")
            audit.create_research_projection(source, research, artifact='public_non_dicom')
            with sqlite3.connect(research) as conn:
                self.assertNotIn('source_url', {r[1] for r in conn.execute('pragma table_info(public_non_dicom_assets)')})
                self.assertEqual(conn.execute('select source_location_id,source_url_override from public_non_dicom_assets where asset_id="0"').fetchone(), ('l0', None))
                self.assertEqual(conn.execute('select asset_id,source_url from agent_public_non_dicom_assets order by asset_id').fetchall(), [('0','https://example.test/data'),('1',''),('2',None)])
                self.assertEqual(json.loads(conn.execute('select metadata_json from public_non_dicom_image_metadata limit 1').fetchone()[0]), {'modality': 'CT'})
                self.assertEqual(json.loads(conn.execute('select metadata_json from agent_public_non_dicom_image_metadata limit 1').fetchone()[0]), {'file_format':'NIFTI','modality':'CT','object_role':'source_image'})
                self.assertEqual(conn.execute('SELECT source_url FROM agent_public_non_dicom_asset_participants').fetchone()[0], 'https://example.test/data')
                self.assertEqual(conn.execute('pragma foreign_key_check').fetchall(), [])
                compact.expand_assembly(conn)
                self.assertFalse(compact.is_compact(conn))
                self.assertEqual(conn.execute('select source_url from public_non_dicom_assets order by asset_id').fetchall(), [('https://example.test/data',),('',),(None,)])
                self.assertIn('file_format', json.loads(conn.execute('select metadata_json from public_non_dicom_image_metadata limit 1').fetchone()[0]))
            audit.create_research_projection(research, root / 'second_generation.sqlite', artifact='public_non_dicom')
            with sqlite3.connect(root / 'second_generation.sqlite') as conn:
                self.assertEqual(conn.execute('SELECT source_url FROM agent_public_non_dicom_assets WHERE asset_id="0"').fetchone()[0], 'https://example.test/data')
            # A storage-only migration must not request semantic waivers; actual changes must.
            # These fixtures intentionally keep verbose provenance local for this comparison.
            with sqlite3.connect(source) as conn:
                for table, (key, fields) in audit.CONFIGS['public_non_dicom']['json_fields'].items():
                    for field, empty in fields.items():
                        conn.execute(f'UPDATE "{table}" SET "{field}"=?', (empty,))
            audit.create_research_projection(source, research, artifact='public_non_dicom', replace=True)
            _, _, _, high, _ = changes.compare_asset('public', research, source, max_items=20)
            self.assertEqual(high, [])
            with sqlite3.connect(research) as conn:
                conn.execute("UPDATE public_non_dicom_assets SET file_format='NRRD' WHERE asset_id='0'")
            _, _, _, high, _ = changes.compare_asset('public', research, source, max_items=20)
            self.assertTrue(high)

    def test_controlled_views_preserve_routes_and_identifiers(self):
        with sqlite3.connect(':memory:') as conn:
            conn.executescript(controlled.SCHEMA_SQL)
            for i, modality in enumerate(['CT','PT','']):
                controlled.insert_controlled_file(conn, {
                    'controlled_file_id':str(i), 'download_row_id':1,
                    'route_system':'ctdc', 'dataset_type':'Analysis Result',
                    'short_title':'TEST', 'source_collections':'Source',
                    'access_level':'controlled', 'controlled_access_policy_url':controlled.CONTROLLED_POLICY_URL,
                    'series_instance_uid':f'1.2.{i}' if modality else '',
                    'patient_id':'P1', 'participant_id':'alternate', 'modality':modality,
                    'image_modality':modality, 'study_instance_uid':'1.2',
                    'file_size_bytes':62202350, 'file_type':'SEG', 'image_count':'12',
                    'manifest_json':'{"retained":"in source table"}', 'metadata_json':'{}',
                })
            self.assertEqual(conn.execute('select count(*) from radiology_series').fetchone()[0],2)
            self.assertEqual(conn.execute('select radiology_id,subject_id,is_derived_object from radiology_series order by controlled_file_id').fetchall(), [(controlled.safe_id('radiology','0'),'P1',1),(controlled.safe_id('radiology','1'),'P1',1)])
            self.assertEqual(conn.execute('select collection_id,analysis_result_id,series_size_MB from idc_index limit 1').fetchone(), ('Source','TEST',controlled.mb_string(62202350)))
            self.assertEqual(conn.execute('select count(*) from idc_ct_index').fetchone()[0],1)
            self.assertEqual(conn.execute('select NumberOfSlices from idc_pt_index').fetchone()[0],'12')
            self.assertEqual(conn.execute('select count(*) from idc_contrast_index').fetchone()[0],0)
            self.assertNotIn('metadata_json', {r[1] for r in conn.execute('pragma table_info(controlled_files)')})
            for name in ['radiology_series','idc_index','idc_ct_index','idc_pt_index','idc_contrast_index','idc_series_links']:
                self.assertEqual(conn.execute('select type from sqlite_master where name=?',(name,)).fetchone()[0],'view')


if __name__ == '__main__':
    unittest.main()
