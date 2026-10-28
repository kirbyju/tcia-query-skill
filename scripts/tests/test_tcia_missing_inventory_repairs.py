import csv
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tcia_missing_inventory_repairs as repairs
import tcia_participant_inventory as inventory


class MissingInventoryRepairsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'subjects.csv'
        self.source.write_text('PatientID,ReferencedSeriesInstanceUID\nP1,1.2.3\nP2,1.2.4\n')
        self.reference = self.root / 'review.json'
        self.csv = self.root / 'review.csv'
        self.metadata = {
            'reviewed_at': '2026-10-25',
            'source_files': {'subjects.csv': {
                'url': 'https://www.cancerimagingarchive.net/wp-content/uploads/subjects.csv',
                'sha256': repairs.digest(self.source), 'size': self.source.stat().st_size}},
            'recipes': [{'dataset_type': 'Collection', 'short_title': 'Restricted',
                'download_id': '10', 'kind': 'csv', 'column': 'PatientID',
                'encoding': 'utf-8-sig', 'source_files': ['subjects.csv'], 'species': 'Human',
                'bindings': [{'short_title': 'Restricted', 'download_id': '10',
                    'field': 'download_url',
                    'url': 'https://www.cancerimagingarchive.net/wp-content/uploads/subjects.csv'}]}]}
        self.reference.write_text(json.dumps(self.metadata))
        repairs.regenerate(self.root, reference=self.reference, inventory=self.csv)
        self.snapshot = self.root / 'snapshot.sqlite'
        with closing(sqlite3.connect(self.snapshot)) as conn, conn:
            conn.execute('CREATE TABLE agent_current_downloads '
                         '(dataset_type,short_title,download_id,hidden,download_url,access_level,controlled_access)')
            conn.execute('INSERT INTO agent_current_downloads VALUES (?,?,?,?,?,?,?)',
                ('Collection', 'Restricted', '10', 0, self.metadata['source_files']['subjects.csv']['url'], 'controlled', 1))
        self.conn = inventory.connect(self.root / 'inventory.sqlite')
        self.addCleanup(self.conn.close)
        self.conn.executescript(inventory.SCHEMA)

    def test_controlled_membership_never_claims_images_or_open_access(self):
        kwargs = dict(api=inventory, reference=self.reference, inventory=self.csv)
        self.assertEqual(repairs.ingest(self.conn, self.snapshot, **kwargs), 2)
        repairs.ingest(self.conn, self.snapshot, **kwargs)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM participants').fetchone()[0], 2)
        rows = list(self.conn.execute('SELECT * FROM participant_assets'))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row['access_level'], 'controlled')
            self.assertEqual(row['inventory_status'], 'known_metadata_authorization_required')
            self.assertEqual(row['data_domain'], 'participant_metadata')
            self.assertEqual(row['object_role'], 'metadata_only')
            self.assertIsNone(row['file_count'])
            self.assertIsNone(row['series_count'])
            self.assertEqual(row['has_file_level_metadata'], 0)
        self.assertFalse(self.conn.execute('PRAGMA foreign_key_check').fetchall())

    def test_current_download_change_rejects_entire_scope_before_import(self):
        with closing(sqlite3.connect(self.snapshot)) as conn, conn:
            conn.execute("UPDATE agent_current_downloads SET download_url='https://example.org/new.csv'")
        with self.assertRaisesRegex(ValueError, 'Current download differs'):
            repairs.ingest(self.conn, self.snapshot, api=inventory, reference=self.reference, inventory=self.csv)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM participants').fetchone()[0], 0)

    def test_hidden_scope_is_not_imported(self):
        with closing(sqlite3.connect(self.snapshot)) as conn, conn:
            conn.execute('UPDATE agent_current_downloads SET hidden=1')
        self.assertEqual(repairs.ingest(self.conn, self.snapshot, api=inventory,
                        reference=self.reference, inventory=self.csv), 0)

    def test_missing_download_catalog_cannot_activate_reviewed_rows(self):
        with closing(sqlite3.connect(self.snapshot)) as conn, conn:
            conn.execute('DROP TABLE agent_current_downloads')
        self.assertEqual(repairs.ingest(self.conn, self.snapshot, api=inventory,
                        reference=self.reference, inventory=self.csv), 0)

    def test_bare_matching_ids_do_not_authorize_source_collection_links(self):
        metadata = json.loads(self.reference.read_text())
        metadata['recipes'][0]['source_collection'] = 'Unrelated'
        self.reference.write_text(json.dumps(metadata))
        repairs.regenerate(self.root, reference=self.reference, inventory=self.csv)
        repairs.ingest(self.conn, self.snapshot, api=inventory,
                       reference=self.reference, inventory=self.csv)
        with self.assertRaisesRegex(ValueError, 'verified parent metadata membership'):
            repairs.link_sources(self.conn, self.snapshot, api=inventory,
                                 reference=self.reference, inventory=self.csv)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM participant_source_links').fetchone()[0], 0)

    def test_inventory_and_source_tampering_fail_closed(self):
        self.csv.write_text(self.csv.read_text().replace('P1', 'P9'))
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            repairs.load(self.reference, self.csv)
        self.source.write_text('PatientID\nP9\n')
        with self.assertRaisesRegex(ValueError, 'source digest mismatch'):
            repairs.regenerate(self.root, reference=self.reference, inventory=self.csv)

    def test_generated_inventory_uses_git_stable_lf_line_endings(self):
        self.assertNotIn(b'\r\n', self.csv.read_bytes())
        metadata = json.loads(self.reference.read_text())
        self.assertEqual(metadata['inventory_sha256'], repairs.digest(self.csv))

    def test_parent_membership_requires_exact_referenced_series(self):
        (self.root / 'parent.tcia').write_text('downloadServerUrl=https://example.org\n1.2.3\n')
        recipe = {'kind': 'referenced_manifest', 'source_files': ['subjects.csv', 'parent.tcia']}
        members = repairs.recipe_members(recipe, self.root)
        self.assertEqual([r[0] for r in members], ['P1'])
        self.assertIn('ReferencedSeriesInstanceUID:1.2.3', members[0][1])

    def test_til_paginated_data_is_not_treated_as_complete_index(self):
        (self.root / 'til.json').write_text(json.dumps({'imageGrid': {'paginate': True, 'values': []}}))
        with self.assertRaisesRegex(ValueError, 'paginated index'):
            repairs.recipe_members({'kind': 'til_browser', 'source_files': ['til.json']}, self.root)

    def test_midi_mapping_requires_bijection_and_exact_curated_patient_set(self):
        try:
            from openpyxl import Workbook
        except ImportError:
            self.skipTest('Optional workbook fixture requires openpyxl')
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Metadata'
        sheet.append(['Patient ID'])
        sheet.append(['MIDI_1'])
        workbook.save(self.root / 'curated.xlsx')
        mapping = self.root / 'mapping.csv'
        mapping.write_text('id_old,id_new\nSYNTHETIC_1,MIDI_1\n')
        recipe = {'kind': 'midi_mapping', 'source_files': ['mapping.csv', 'curated.xlsx']}
        self.assertEqual(repairs.recipe_members(recipe, self.root)[0][0], 'MIDI_1')
        mapping.write_text('id_old,id_new\nSYNTHETIC_1,MIDI_1\nSYNTHETIC_2,MIDI_1\n')
        with self.assertRaisesRegex(ValueError, 'one-to-one'):
            repairs.recipe_members(recipe, self.root)
        mapping.write_text('id_old,id_new\nSYNTHETIC_1,MIDI_2\n')
        with self.assertRaisesRegex(ValueError, 'curated image metadata'):
            repairs.recipe_members(recipe, self.root)

    def test_checked_in_inventory_keeps_partial_cohorts_and_benchmark_pairs_explicit(self):
        metadata, rows = repairs.load()
        counts = metadata['participants_by_dataset']
        self.assertEqual(len(counts), 32)
        self.assertEqual(counts['MIDI-B-Test-MIDI-B-Validation'], 538)
        self.assertEqual(counts['AREN0532'], 542)
        self.assertEqual(counts['S0819'], 1281)
        self.assertEqual(counts['TIL-WSI-TCGA'], 4820)
        self.assertEqual(counts['Pan-Cancer-Nuclei-Seg'], 4635)
        self.assertEqual(counts['MRI-DIR'], 9)
        self.assertNotIn('NSCLC-Cetuximab', counts)
        # Bare equal identifiers in unrelated scopes are never source links.
        self.assertFalse(any(r['source_collection'] for r in rows if r['short_title'] == 'TIL-WSI-TCGA'))

    def test_til_variant_selection_does_not_union_changed_cohorts(self):
        data = {'range_only': True, 'files': [
            {'name': 'v1/brca/TCGA-AA-0001-01Z-00-DX1.png'},
            {'name': 'v2/brca/TCGA-AA-0002-01Z-00-DX1.png'}]}
        (self.root / 'directory.json').write_text(json.dumps(data))
        recipe = {'kind': 'til_zip_directory', 'source_files': ['directory.json'],
                  'archive_variant': 'v1', 'map_file_count': 1,
                  'tcga_projects': {'TCGA-AA-0001': 'TCGA-BRCA'}}
        self.assertEqual([m[0] for m in repairs.recipe_members(recipe, self.root)], ['TCGA-AA-0001'])
        recipe['tcga_projects']['TCGA-AA-0001'] = 'TCGA-LUAD'
        with self.assertRaisesRegex(ValueError, 'case/project mapping mismatch'):
            repairs.recipe_members(recipe, self.root)

    def test_explicit_all_folder_policy_excludes_only_reviewed_absent_cases(self):
        data = {'range_only': True, 'files': [
            {'name': 'v1/brca/TCGA-AA-0001-01Z-00-DX1.png'},
            {'name': 'v2/brca/TCGA-AA-0001-01Z-00-DX1.png'},
            {'name': 'v2/brca/TCGA-AA-0002-01Z-00-DX1.png'},
            {'name': 'v2/brca/TCGA-AA-0003-01Z-00-DX1.png'}]}
        (self.root / 'directory.json').write_text(json.dumps(data))
        recipe = {'kind': 'til_zip_directory', 'source_files': ['directory.json'],
                  'archive_variants': ['v1', 'v2'], 'map_file_counts': {'v1': 1, 'v2': 3},
                  'tcga_projects': {'TCGA-AA-0001': 'TCGA-BRCA', 'TCGA-AA-0002': 'TCGA-BRCA'},
                  'excluded_participants': {'TCGA-AA-0003': 'TCGA-BRCA'},
                  'idc_presence_review': {'authority': 'user-approved finite test policy'}}
        members = repairs.recipe_members(recipe, self.root)
        self.assertEqual({pid for pid, _ in members}, {'TCGA-AA-0001', 'TCGA-AA-0002'})
        self.assertIn(('TCGA-AA-0002', 'zip-entry:v2/brca/TCGA-AA-0002-01Z-00-DX1.png'), members)
        recipe['excluded_participants']['TCGA-AA-0003'] = 'TCGA-LUAD'
        with self.assertRaisesRegex(ValueError, 'case/project mapping mismatch'):
            repairs.recipe_members(recipe, self.root)
        recipe['excluded_participants']['TCGA-AA-0003'] = 'TCGA-BRCA'
        recipe.pop('idc_presence_review')
        with self.assertRaisesRegex(ValueError, 'exclusions lack'):
            repairs.recipe_members(recipe, self.root)

    def test_reviewed_til_policy_retains_exclusion_evidence_not_membership(self):
        metadata, rows = repairs.load()
        recipe = next(r for r in metadata['recipes'] if r['short_title'] == 'TIL-WSI-TCGA')
        included = {r['participant_id'] for r in rows if r['short_title'] == 'TIL-WSI-TCGA'}
        self.assertEqual(len(included), 4820)
        self.assertEqual(len(recipe['excluded_participants']), 10)
        self.assertFalse(included & set(recipe['excluded_participants']))
        self.assertEqual(included, set(recipe['tcga_current_source_ids']))
        self.assertEqual(included, set(recipe['tcga_projects']))

    def test_approved_mri_dir_policy_overrides_source_human_label_per_identity(self):
        metadata, _ = repairs.load()
        recipe = next(r for r in metadata['recipes'] if r['short_title'] == 'MRI-DIR')
        with closing(sqlite3.connect(self.snapshot)) as conn, conn:
            conn.execute('ALTER TABLE agent_current_downloads ADD COLUMN download_metadata')
            conn.execute('INSERT INTO agent_current_downloads '
                         '(dataset_type,short_title,download_id,hidden,download_url,access_level,controlled_access,download_metadata) '
                         'VALUES (?,?,?,?,?,?,?,?)', ('Collection','MRI-DIR','43705',0,'','open',0,recipe['bindings'][0]['url']))
            conn.execute('CREATE TABLE agent_datasets (dataset_type,short_title,species)')
            conn.execute("INSERT INTO agent_datasets VALUES ('Collection','MRI-DIR','Human')")
        repairs.ingest(self.conn, self.snapshot, api=inventory)
        inventory.build_participant_groups(self.conn, self.snapshot)
        counts = {r['species']: r['participant_count'] for r in self.conn.execute('SELECT * FROM agent_participant_summary')}
        self.assertEqual(counts, {'Computational Phantom': 8, 'Physical Phantom': 1})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM participants').fetchone()[0], 9)
        with closing(sqlite3.connect(self.snapshot)) as conn:
            self.assertEqual(conn.execute('SELECT species FROM agent_datasets').fetchone()[0], 'Human')


if __name__ == '__main__':
    unittest.main()
