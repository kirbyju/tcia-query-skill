"""Clinical and imaging criteria intersect before paging; no payload access."""
import asyncio
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from mcp import Client
from mcp_server.tcia_query_mcp.models import ParticipantsResponse
from mcp_server.tcia_query_mcp.rest import create_app
from mcp_server.tcia_query_mcp.server import mcp
from mcp_server.tcia_query_mcp.service import InvalidRequestError, ServiceUnavailableError
from mcp_server.tests import test_service as fixtures


class ParticipantClinicalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TciaQueryServiceTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.visible = {('collection', 'a'), ('collection', 'b')}
        self.service._visible_dataset_keys = lambda: self.visible
        with fixtures.connect(self.service.participant_db) as c:
            c.execute('DELETE FROM participant_assets')
            c.execute('DELETE FROM participant_identifiers')
            c.execute('DELETE FROM participants')
        with fixtures.connect(self.service.clinical_db) as c:
            c.execute('DELETE FROM agent_clinical_subjects')

    def person(self, key, *, title='A', identity=None, modality='CT', diagnosis='Carcinoma', site='Lung',
               diagnosis_inferred=0, site_inferred=0, scope='patient', site_scope=None,
               kind='Collection', extra_sources=None, clinical=True):
        identity = identity or key
        with fixtures.connect(self.service.participant_db) as c:
            c.execute('INSERT INTO participants(participant_key,dataset_type,short_title,display_participant_id) VALUES(?,?,?,?)',
                      (key,kind,title,identity))
            c.execute('INSERT INTO participant_assets(participant_asset_id,participant_key,managed_system,access_level,modality,file_format) VALUES(?,?,?,?,?,?)',
                      ('asset-'+key,key,'crdc_idc','open',modality,'DICOM'))
        if clinical:
            sources={'primary_diagnosis':{'evidence_scope':scope},
                     'primary_site':{'evidence_scope':site_scope or scope}, **(extra_sources or {})}
            self.clinical(title,identity,diagnosis,site,diagnosis_inferred,site_inferred,sources)

    def clinical(self, title, identity, diagnosis, site, dflag=0, sflag=0, sources=None):
        with fixtures.connect(self.service.clinical_db) as c:
            c.execute('INSERT INTO agent_clinical_subjects(short_title,subject_id,primary_diagnosis,primary_site,primary_diagnosis_is_inferred,primary_site_is_inferred,resolved_sources_json) VALUES(?,?,?,?,?,?,?)',
                      (title,identity,diagnosis,site,dflag,sflag,json.dumps(sources or {})))

    def search(self, **extra):
        return self.service.search_participants(modalities=['CT'], diagnosis='carcinoma', primary_site='lung', **extra)

    def test_combined_filters_page_after_matching_and_preserve_evidence(self):
        self.person('0-wrong',site='Liver')
        self.person('1-good',diagnosis='ADENOCARCINOMA',site='Upper lobe of right LUNG')
        self.person('2-wrong',diagnosis='Sarcoma')
        self.person('3-good',title='B')
        self.person('4-mr',modality='MR')
        first=self.search(limit=1)
        self.assertEqual([r['participant_key'] for r in first['participants']],['1-good'])
        self.assertTrue(first['has_more'])
        match=first['participants'][0]['clinical_match']
        self.assertEqual(match['primary_site'],'Upper lobe of right LUNG')
        self.assertIs(match['primary_diagnosis_is_inferred'],False)
        self.assertEqual(match['diagnosis_evidence_scope'],'patient')
        second=self.search(limit=1,cursor=first['next_cursor'])
        self.assertEqual([r['participant_key'] for r in second['participants']],['3-good'])
        self.assertFalse(second['has_more'])
        ParticipantsResponse.model_validate(first)
        self.assertEqual(first['clinical_query']['dataset_scope'],'Collection')
        self.assertEqual(first['clinical_query']['match_mode'],'case_insensitive_substring')

    def test_subject_identity_is_exact_and_dataset_scoped_and_never_combines_rows(self):
        self.person('a',identity='SAME',diagnosis='Carcinoma',site='Liver')
        self.person('b',title='B',identity='SAME',diagnosis='Sarcoma',site='Lung')
        self.person('c',identity='EXACT',clinical=False)
        self.clinical('A','exact','Carcinoma','Lung')
        self.assertEqual(self.search()['count'],0)

    def test_recorded_mode_checks_both_flags_scopes_and_patient_evidence(self):
        self.person('reported')
        self.person('inferred-diagnosis',diagnosis_inferred=1,scope='dataset')
        self.person('inferred-site',site_inferred=1,site_scope='dataset')
        self.person('unknown',diagnosis_inferred=None,site_inferred=None,scope='unknown')
        self.person('untrusted-false-flag',scope='dataset')
        self.person('inherited',scope='patient_inherited')
        self.person('no-site',site=None)
        self.assertEqual(self.search()['count'],6)
        found=self.search(include_inferred=False)
        self.assertEqual({r['participant_key'] for r in found['participants']},{'reported','inherited'})

    def test_no_selected_clinical_values_still_requires_patient_evidence(self):
        self.person('only-dataset',diagnosis_inferred=1,site_inferred=1,scope='dataset')
        self.person('recorded-age',diagnosis_inferred=1,site_inferred=1,scope='dataset',
                    extra_sources={'age':{'evidence_scope':'patient'}})
        self.person('no-clinical',clinical=False)
        result=self.service.search_participants(include_inferred=False)
        self.assertEqual([r['participant_key'] for r in result['participants']],['recorded-age'])
        self.assertEqual(self.service.search_participants()['count'],3)

    def test_text_is_literal_unicode_casefolded_and_missing_does_not_match(self):
        self.person('plain')
        self.person('symbols',diagnosis='Carcinoma 100%_[]',site='Straße')
        self.person('missing',diagnosis=None)
        found=self.service.search_participants(diagnosis='%_[]',primary_site='STRASSE')
        self.assertEqual([r['participant_key'] for r in found['participants']],['symbols'])
        self.assertEqual(self.service.search_participants(diagnosis="' OR 1=1 --")['count'],0)

    def test_hidden_and_analysis_result_records_do_not_enter_clinical_search(self):
        self.person('a')
        self.person('hidden',title='HIDDEN')
        self.person('ar',title='AR',kind='Analysis Result')
        self.visible.add(('analysis result','ar'))
        self.assertEqual([r['participant_key'] for r in self.search()['participants']],['a'])
        with self.assertRaises(InvalidRequestError):
            self.search(dataset_type='analysis result')

    def test_ambiguous_title_or_duplicate_subject_fails(self):
        self.person('a')
        self.visible.add(('analysis result','a'))
        with self.assertRaisesRegex(ServiceUnavailableError,'disambiguate'):
            self.search()
        self.visible.remove(('analysis result','a'))
        self.clinical('A','a','Carcinoma','Lung')
        with self.assertRaisesRegex(ServiceUnavailableError,'ambiguous'):
            self.search()

    def test_missing_clinical_does_not_change_availability_or_become_zero(self):
        self.person('a')
        self.service.clinical_db.unlink()
        self.assertEqual(self.service.search_participants()['count'],1)
        with self.assertRaises(ServiceUnavailableError):self.search()
        response=TestClient(create_app(self.service)).post('/v2/participants/search',json={'diagnosis':'carcinoma'})
        self.assertEqual(response.status_code,503)

    def test_invalid_json_and_wrong_flags_are_errors(self):
        self.person('a')
        with fixtures.connect(self.service.clinical_db) as c:
            c.execute("UPDATE agent_clinical_subjects SET resolved_sources_json='invalid'")
        for inferred in [True,False]:
            with self.assertRaises(ServiceUnavailableError):self.search(include_inferred=inferred)
        with fixtures.connect(self.service.clinical_db) as c:
            c.execute("UPDATE agent_clinical_subjects SET resolved_sources_json='{}',primary_site_is_inferred=9")
        with self.assertRaises(ServiceUnavailableError):self.search()

    def test_cursors_bind_clinical_filters_and_generation(self):
        self.person('a');self.person('b')
        cursor=self.search(limit=1)['next_cursor']
        with self.assertRaises(InvalidRequestError):
            self.service.search_participants(modalities=['CT'],diagnosis='other',primary_site='lung',limit=1,cursor=cursor)
        with fixtures.connect(self.service.clinical_db) as c:
            c.execute("UPDATE agent_clinical_subjects SET primary_site='Different'")
        with self.assertRaises(InvalidRequestError):self.search(limit=1,cursor=cursor)

    def test_metadata_drift_during_search_is_an_error(self):
        self.person('a')
        original=self.service._participant_summary_rows
        def drift(conn,keys):
            result=original(conn,keys)
            with fixtures.connect(self.service.clinical_db) as c:
                c.execute("UPDATE agent_clinical_subjects SET primary_site='Different'")
            return result
        with patch.object(self.service,'_participant_summary_rows',side_effect=drift):
            with self.assertRaisesRegex(ServiceUnavailableError,'changed'):
                self.search()

    def test_search_does_not_modify_any_database(self):
        self.person('a')
        paths=[self.service.snapshot_db,self.service.participant_db,self.service.clinical_db]
        before=[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        self.search();self.search(include_inferred=False)
        self.assertEqual(before,[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])

    def test_invalid_filters_and_extra_rest_parameters_are_rejected(self):
        for data in [{'diagnosis':''},{'primary_site':' '},{'diagnosis':'x'*201},{'include_inferred':'false'}]:
            with self.subTest(data=data),self.assertRaises(InvalidRequestError):
                self.service.search_participants(**data)
        client=TestClient(create_app(self.service))
        for data in [{'diagnosis':' '},{'include_inferred':'false'},{'credential':'secret'}]:
            self.assertEqual(client.post('/v2/participants/search',json=data).status_code,422)

    def test_rest_mcp_contracts_match(self):
        self.person('a')
        body={'modalities':['CT'],'diagnosis':'carcinoma','primary_site':'lung','include_inferred':False}
        expected=ParticipantsResponse.model_validate(self.service.search_participants(**body)).model_dump(mode='json')
        self.assertEqual(TestClient(create_app(self.service)).post('/v2/participants/search',json=body).json(),expected)
        async def run():
            async with Client(mcp) as client:
                with patch('mcp_server.tcia_query_mcp.server.service',return_value=self.service):
                    result=await client.call_tool('search_participants',body)
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content,expected)
        asyncio.run(run())
