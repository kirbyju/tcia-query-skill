"""Reported DICOM sizes and rounded labels are never exact ZIP payload assertions."""
import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from mcp import Client
from mcp_server.tests import test_service as fixtures
from mcp_server.tcia_query_mcp.models import ControlledFilesResponse
from mcp_server.tcia_query_mcp.rest import create_app
from mcp_server.tcia_query_mcp.server import mcp
from mcp_server.tcia_query_mcp.service import ServiceUnavailableError


class ControlledTransferSizeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TciaQueryServiceTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        with fixtures.connect(self.service.controlled_db) as c:
            for column in ['manifest_row_id TEXT','metadata_row_id TEXT','checksum TEXT','checksum_algorithm TEXT']:
                c.execute('ALTER TABLE agent_controlled_files ADD COLUMN '+column)
            c.executescript('''
                CREATE TABLE manifest_rows(manifest_row_id TEXT PRIMARY KEY,short_title TEXT,drs_uri TEXT,file_size_text TEXT,file_size_bytes INTEGER);
                CREATE TABLE metadata_rows(metadata_row_id TEXT PRIMARY KEY,short_title TEXT,file_size_bytes INTEGER);
                INSERT INTO manifest_rows VALUES('manifest-1','AAPM-RT-MAC','drs://nci-crdc.datacommons.io/file-1','19.71 KB',19710);
                INSERT INTO metadata_rows VALUES('metadata-1','AAPM-RT-MAC',4920514);
                UPDATE agent_controlled_files SET manifest_row_id='manifest-1',metadata_row_id='metadata-1',file_name='series.zip',file_size_bytes=4920514,
                    checksum='0123456789abcdef0123456789abcdef',checksum_algorithm='md5';
            ''')

    def file(self):
        result=self.service.get_controlled_access_files('AAPM-RT-MAC')
        ControlledFilesResponse.model_validate(result)
        return result['files'][0]

    def test_uncompressed_series_and_rounded_manifest_sizes_are_not_payload_bytes(self):
        row=self.file()
        self.assertEqual(row['file_size_bytes'],4920514)
        self.assertEqual(row['file_size_basis'],'series_metadata')
        self.assertEqual(row['download_size_label'],'19.71 KB')
        self.assertIsNone(row['download_size_bytes'])
        self.assertEqual(row['checksum'],'0123456789abcdef0123456789abcdef')
        self.assertEqual(row['checksum_algorithm'],'md5')

    def test_exact_manifest_byte_count_is_independent_of_series_size(self):
        for label in ['20181','20181 B','20181 bytes','0']:
            with self.subTest(label=label):
                with fixtures.connect(self.service.controlled_db) as c:
                    c.execute('UPDATE manifest_rows SET file_size_text=?',(label,))
                row=self.file()
                self.assertEqual(row['download_size_bytes'],int(label.split()[0]))
                self.assertEqual(row['file_size_bytes'],4920514)
                self.assertEqual(row['file_size_basis'],'series_metadata')

    def test_rounding_and_unit_base_never_become_exact(self):
        for label in ['24.39 MB','19.71 KB','24 MiB','1024.0','-1','unknown','']:
            with self.subTest(label=label):
                with fixtures.connect(self.service.controlled_db) as c:
                    c.execute('UPDATE manifest_rows SET file_size_text=?',(label,))
                self.assertIsNone(self.file()['download_size_bytes'])

    def test_manifest_only_reported_size_retains_its_basis(self):
        with fixtures.connect(self.service.controlled_db) as c:
            c.execute("UPDATE agent_controlled_files SET metadata_row_id=NULL,file_size_bytes=19710")
        self.assertEqual(self.file()['file_size_basis'],'manifest_rounded')
        with fixtures.connect(self.service.controlled_db) as c:
            c.execute("UPDATE manifest_rows SET file_size_text='20181',file_size_bytes=20181")
            c.execute('UPDATE agent_controlled_files SET file_size_bytes=20181')
        self.assertEqual(self.file()['file_size_basis'],'manifest_exact')
        self.assertEqual(self.file()['download_size_bytes'],20181)

    def test_series_only_metadata_never_invents_an_archive_size(self):
        with fixtures.connect(self.service.controlled_db) as c:
            c.execute('UPDATE agent_controlled_files SET manifest_row_id=NULL')
        row=self.file()
        self.assertIsNone(row['download_size_bytes'])
        self.assertIsNone(row['download_size_label'])
        self.assertEqual(row['file_size_basis'],'series_metadata')

    def test_missing_source_or_wrong_identity_fails_closed(self):
        for sql in ["UPDATE manifest_rows SET short_title='OTHER'",
                    "UPDATE manifest_rows SET drs_uri='drs://nci-crdc.datacommons.io/other'",
                    'DELETE FROM manifest_rows']:
            with self.subTest(sql=sql):
                with fixtures.connect(self.service.controlled_db) as c:
                    c.execute('DELETE FROM manifest_rows')
                    c.execute("INSERT INTO manifest_rows VALUES('manifest-1','AAPM-RT-MAC','drs://nci-crdc.datacommons.io/file-1','19.71 KB',19710)")
                    c.execute(sql)
                with self.assertRaises(ServiceUnavailableError):self.file()

    def test_rest_and_mcp_expose_the_same_safe_sizes(self):
        expected=ControlledFilesResponse.model_validate(self.service.get_controlled_access_files('AAPM-RT-MAC')).model_dump(mode='json')
        response=TestClient(create_app(self.service)).get('/v2/controlled-access/AAPM-RT-MAC/files')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),expected)
        async def run():
            async with Client(mcp) as client:
                with patch('mcp_server.tcia_query_mcp.server.service',return_value=self.service):
                    result=await client.call_tool('get_controlled_access_files',{'short_title':'AAPM-RT-MAC'})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content,expected)
        asyncio.run(run())
