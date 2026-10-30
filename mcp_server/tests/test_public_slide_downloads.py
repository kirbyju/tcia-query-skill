import asyncio
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from mcp import Client
from mcp_server.tcia_query_mcp.models import Asset, AssetsResponse
from mcp_server.tcia_query_mcp.service import public_asset_download
from mcp_server.tcia_query_mcp.rest import create_app
from mcp_server.tcia_query_mcp.server import mcp
from mcp_server.tests import test_service as fixtures


def slide(**changes):
    row={'dataset_type':'Collection','short_title':'TCGA-BRCA','asset_granularity':'file',
         'source_system':'tcia_pathdb','media_kind':'whole_slide_image','file_format':'SVS',
         'source_url':'http://pathdb.cancerimagingarchive.net/index.php/system/files/wsi/example/slide.svs',
         'file_name':'source-slide-id','representation_provenance_class':'unknown'}
    row.update(changes);return row


class PublicSlideDownloadTests(unittest.TestCase):
    def test_descriptor_preserves_source_and_names_derived_representation(self):
        original=slide();row=public_asset_download(original)
        Asset.model_validate(row)
        self.assertEqual(row['source_url'],original['source_url'])
        self.assertEqual(row['file_name'],'source-slide-id')
        self.assertEqual(row['download_file_name'],'slide.svs')
        self.assertTrue(row['imageUrl'].startswith('https://pathdb.'))
        self.assertEqual(row['representation_provenance_class'],'unknown')
        self.assertEqual(row['download_representation'],'public_pathology_slide_copy')
        self.assertEqual(row['access_level'],'open')
        self.assertNotIn('imageUrl',original)

    def test_unknown_or_controlled_sources_never_gain_a_download_url(self):
        cases=[{'access_level':'controlled'},{'controlled_access':True},{'access_level':'unknown'},
               {'source_system':'other'},{'asset_granularity':'package'},{'file_format':'DICOM'},
               {'source_url':'https://pathdb.cancerimagingarchive.net/viewer/slide.svs'},
               {'source_url':'https://evil.example/system/files/wsi/slide.svs'},
               {'source_url':'https://pathdb.cancerimagingarchive.net/system/files/wsi/slide.svs?token=secret'},
               {'source_url':'https://pathdb.cancerimagingarchive.net/system/files/wsi/../slide.svs'},
               {'source_url':'https://pathdb.cancerimagingarchive.net/system/files/wsi/%2e%2e/slide.svs'},
               {'source_url':'https://pathdb.cancerimagingarchive.net:444/system/files/wsi/slide.svs'},
               {'source_url':'https://user:password@pathdb.cancerimagingarchive.net/system/files/wsi/slide.svs'},
               {'source_url':'https://pathdb.cancerimagingarchive.net/system/files/wsi/images.zip'}]
        for changes in cases:
            with self.subTest(changes=changes):self.assertNotIn('imageUrl',public_asset_download(slide(**changes)))

    def test_rest_and_mcp_expose_same_descriptor(self):
        fixture=fixtures.TciaQueryServiceTests();fixture.setUp();self.addCleanup(fixture.tmp.cleanup)
        with fixture.service._connect_public_non_dicom() as readonly:
            path=readonly.execute('PRAGMA database_list').fetchone()[2]
        with fixtures.connect(path) as conn:
            conn.execute("UPDATE agent_public_non_dicom_assets SET source_system='tcia_pathdb',media_kind='whole_slide_image',file_format='SVS',source_url=? WHERE asset_id='pnd-1'",(slide()['source_url'],))
        result=fixture.service.find_public_non_dicom_assets(short_titles=['TCGA-BRCA'])
        AssetsResponse.model_validate(result)
        expected=next(r for r in result['assets'] if r['asset_id']=='pnd-1')['imageUrl']
        response=TestClient(create_app(fixture.service)).get('/v2/public-non-dicom/assets',params={'short_titles':'TCGA-BRCA'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(next(r for r in response.json()['assets'] if r['asset_id']=='pnd-1')['imageUrl'],expected)
        async def run():
            async with Client(mcp) as client:
                with patch('mcp_server.tcia_query_mcp.server.service',return_value=fixture.service):
                    tool=await client.call_tool('find_public_non_dicom_assets',{'short_titles':['TCGA-BRCA']})
                self.assertFalse(tool.is_error)
                self.assertEqual(next(r for r in tool.structured_content['assets'] if r['asset_id']=='pnd-1')['imageUrl'],expected)
        asyncio.run(run())
