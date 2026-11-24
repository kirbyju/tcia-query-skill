from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from mcp import Client

from scripts.tcia_locator_indexes import PATHDB_URL_INDEX_SQL, CONTROLLED_FILE_ID_INDEX_SQL
from mcp_server.tcia_query_mcp.models import FileDOIsResponse
from mcp_server.tcia_query_mcp.rest import create_app
from mcp_server.tcia_query_mcp.server import mcp
from mcp_server.tcia_query_mcp.service import TciaQueryService, InvalidRequestError


class FileDOIResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.base, self.controlled, self.public = (root/name for name in ('base.sqlite','controlled.sqlite','public.sqlite'))
        with closing(sqlite3.connect(self.base)) as conn, conn:
            conn.executescript('''
                CREATE TABLE agent_datasets(dataset_type, short_title, title, doi, link, hidden);
                INSERT INTO agent_datasets VALUES ('Collection','A','Dataset A','10.1234/a','https://example.org/a',0),
                 ('Analysis Result','B','Dataset B','10.1234/b','https://example.org/b',0),
                 ('Collection','Hidden','Hidden','10.1234/hidden','https://example.org/hidden',1);
            ''')
        with closing(sqlite3.connect(self.controlled)) as conn, conn:
            conn.executescript('''
                CREATE TABLE controlled_files(drs_uri,file_id,series_instance_uid,dataset_type,short_title,doi);
                CREATE INDEX idx_controlled_files_drs ON controlled_files(drs_uri);
                CREATE INDEX idx_controlled_files_series ON controlled_files(series_instance_uid);
                INSERT INTO controlled_files VALUES
                 ('drs://example.org/one','one','1.2.3','Collection','A','10.1234/a'),
                 ('drs://example.org/duplicate','dup','1.2.4','Collection','A','10.1234/a'),
                 ('drs://example.org/duplicate','dup','1.2.4','Analysis Result','B','10.1234/b'),
                 ('drs://example.org/hidden','hidden','1.2.5','Collection','Hidden','10.1234/hidden');
            ''')
            conn.execute(CONTROLLED_FILE_ID_INDEX_SQL)
        with closing(sqlite3.connect(self.public)) as conn, conn:
            conn.executescript('''
                CREATE TABLE public_non_dicom_assets(asset_id TEXT PRIMARY KEY,source_system,asset_granularity,dataset_type,short_title);
                INSERT INTO public_non_dicom_assets VALUES
                 ('a1','tcia_pathdb','file','Collection','A'),
                 ('a2','tcia_pathdb','file','Collection','A'),
                 ('b','tcia_aspera','file','Analysis Result','B'),
                 ('package','tcia_pathdb','package','Collection','A');
            ''')
            conn.executescript("""
                CREATE TABLE public_non_dicom_locations(asset_id,managed_system,access_url);
                INSERT INTO public_non_dicom_locations VALUES
                ('a1','tcia_pathdb','http://pathdb.cancerimagingarchive.net/system/files/wsi/a.tif'),
                ('a2','tcia_pathdb','https://pathdb.cancerimagingarchive.net/system/files/wsi/a.tif'),
                ('b','tcia_pathdb','http://pathdb.cancerimagingarchive.net/system/files/wsi/a.tif'),
                ('package','tcia_pathdb','http://pathdb.cancerimagingarchive.net/system/files/wsi/package.zip');
            """)
            conn.execute(PATHDB_URL_INDEX_SQL)
        self.service = TciaQueryService(snapshot_db=self.base,controlled_db=self.controlled,public_non_dicom_db=self.public,bundle_manifest=root/'missing.json')

    def request(self,kind='drs_uri',value='drs://example.org/one',id='one'):
        return {'id':id,'kind':kind,'value':value}

    def test_exact_keys_duplicates_hidden_and_no_filename_guessing(self):
        locators=[self.request(), self.request('file_id','one','id'),self.request('series_instance_uid','1.2.3','uid'),
                  self.request(value='drs://example.org/hidden',id='hidden'), self.request(value='drs://example.org/absent',id='absent'),
                  self.request(value='drs://example.org/duplicate',id='ambiguous')]
        result=self.service.resolve_file_dois(locators)
        FileDOIsResponse.model_validate(result)
        self.assertEqual([r['id'] for r in result['results']],[r['id'] for r in locators])
        self.assertEqual([r['status'] for r in result['results']],['matched','matched','matched','unmatched','unmatched','ambiguous'])
        self.assertEqual(result['results'][0]['matches'][0]['doi'],'10.1234/a')
        self.assertEqual(len(result['results'][-1]['matches']),2)

    def test_conflicting_publication_identities_are_not_overwritten(self):
        with closing(sqlite3.connect(self.base)) as conn, conn:
            conn.execute("INSERT INTO agent_datasets VALUES ('Collection','A','Other identity','10.1234/other','https://example.org/other',0)")
        item=self.service.resolve_file_dois([self.request()])['results'][0]
        self.assertEqual(item['status'],'ambiguous')
        self.assertEqual({match['doi'] for match in item['matches']},{'10.1234/a','10.1234/other'})

    def test_public_url_matches_scheme_variants_and_deduplicates_dataset(self):
        result=self.service.resolve_file_dois([self.request('image_url','https://pathdb.cancerimagingarchive.net/system/files/wsi/a.tif')])['results'][0]
        self.assertEqual(result['status'],'matched')
        self.assertEqual(len(result['matches']),1)
        self.assertEqual(result['matches'][0]['doi'],'10.1234/a')
        result=self.service.resolve_file_dois([self.request('image_url','https://pathdb.cancerimagingarchive.net/system/files/wsi/package.zip')])['results'][0]
        self.assertEqual(result['status'],'unmatched')

    def test_missing_index_is_unavailable_instead_of_a_scan_or_false_zero(self):
        with closing(sqlite3.connect(self.public)) as conn, conn:conn.execute('DROP INDEX idx_pnd_pathdb_file_url')
        result=self.service.resolve_file_dois([self.request(),self.request('image_url','https://pathdb.cancerimagingarchive.net/system/files/wsi/a.tif','url')])
        self.assertEqual([r['status'] for r in result['results']],['matched','unavailable'])
        self.assertNotIn(str(self.public),json.dumps(result))

    def test_missing_and_wrong_component_and_inconsistent_doi(self):
        with closing(sqlite3.connect(self.controlled)) as conn, conn:
            conn.execute("UPDATE controlled_files SET doi='10.1234/wrong' WHERE file_id='one'")
        self.assertEqual(self.service.resolve_file_dois([self.request()])['results'][0]['status'],'unavailable')
        self.controlled.unlink()
        self.assertEqual(self.service.resolve_file_dois([self.request()])['results'][0]['status'],'unavailable')
        with closing(sqlite3.connect(self.controlled)) as conn, conn:conn.execute('CREATE TABLE unrelated(x)')
        self.assertEqual(self.service.resolve_file_dois([self.request()])['results'][0]['status'],'unavailable')

    def test_missing_base_returns_503_without_a_filesystem_path(self):
        self.base.unlink()
        response=TestClient(create_app(self.service)).post('/v2/files/resolve-dois',json={'locators':[self.request()]})
        self.assertEqual(response.status_code,503)
        self.assertNotIn(str(self.base),response.text)

    def test_malformed_locators_are_invalid_requests(self):
        for kind,value in [('drs_uri','drs://[broken/path'),('series_instance_uid','1..2'),('series_instance_uid','.1.2')]:
            with self.subTest(kind=kind,value=value),self.assertRaises(InvalidRequestError):
                self.service.resolve_file_dois([self.request(kind,value)])

    def test_rejects_credentials_and_invalid_batches_without_echo(self):
        client=TestClient(create_app(self.service))
        for url in ['https://u:secret@pathdb.cancerimagingarchive.net/a.tif','https://pathdb.cancerimagingarchive.net/a.tif?token=secret']:
            response=client.post('/v2/files/resolve-dois',json={'locators':[self.request('image_url',url)]})
            self.assertEqual(response.status_code,422)
            self.assertNotIn('secret',response.text)
            self.assertEqual(response.headers['content-type'],'application/problem+json')
        for body in [{'locators':[]},{'locators':[self.request()]*201},{'locators':[self.request(),self.request()]},{'locators':[self.request()],'unknown':True}]:
            self.assertEqual(client.post('/v2/files/resolve-dois',json=body).status_code,422)

    def test_rest_mcp_same_closed_readonly_contract(self):
        locators=[self.request()]
        expected=self.service.resolve_file_dois(locators)
        response=TestClient(create_app(self.service)).post('/v2/files/resolve-dois',json={'locators':locators})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),expected)
        async def run():
            async with Client(mcp) as client:
                with patch('mcp_server.tcia_query_mcp.server.service',return_value=self.service):
                    result=await client.call_tool('resolve_file_dois',{'locators':locators})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content,expected)
        asyncio.run(run())
        with self.service._connect_controlled() as conn:
            with self.assertRaises(sqlite3.OperationalError):conn.execute("DELETE FROM controlled_files")

    def test_indexes_are_used(self):
        with closing(sqlite3.connect(self.public)) as conn, conn:
            plan=conn.execute("EXPLAIN QUERY PLAN SELECT asset_id FROM public_non_dicom_locations WHERE managed_system='tcia_pathdb' AND access_url IS NOT NULL AND access_url=?",('http://pathdb.cancerimagingarchive.net/system/files/wsi/a.tif',)).fetchall()
            self.assertIn('idx_pnd_pathdb_file_url',str(plan))
        with closing(sqlite3.connect(self.controlled)) as conn, conn:
            plan=conn.execute('EXPLAIN QUERY PLAN SELECT short_title FROM controlled_files WHERE file_id=?',('one',)).fetchall()
            self.assertIn('idx_controlled_files_file_id',str(plan))
