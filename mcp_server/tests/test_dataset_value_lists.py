import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from mcp import Client
from pydantic import ValidationError
from mcp_server.tests.test_service import connect
from mcp_server.tests import test_service as fixtures
from mcp_server.tcia_query_mcp.models import DatasetSummary, DatasetDetail, Download
from mcp_server.tcia_query_mcp.rest import create_app
from mcp_server.tcia_query_mcp.server import mcp
from mcp_server.tcia_query_mcp.service import (
    compact_dataset,
    full_dataset,
    compact_download,
)

SUMMARY_FIELDS = (
    "data_categories",
    "download_data_types",
    "download_file_types",
    "external_resource_labels",
    "cancer_types",
    "cancer_locations",
    "species",
    "source_collections",
)
DETAIL_FIELDS = SUMMARY_FIELDS + (
    "licenses",
    "program",
    "data_types",
    "download_types",
    "external_resources",
)
DOWNLOAD_FIELDS = ("download_types", "data_types", "file_types", "external_resources")


class DatasetValueListsTests(unittest.TestCase):
    def test_summary_single_multiple_empty_and_structured_values_are_lists(self):
        raw = {
            "short_title": "EXAMPLE",
            "download_types": '["Radiology Images", "Pathology Images"]',
            "download_data_types": "CT; MR; PT; Whole Slide Image",
            "download_file_types": "DICOM; JSON; SVS",
            "external_resource_labels": ["Clinical"],
            "cancer_types": "Head, Neck; Lung",
            "cancer_locations": "Esophagus",
            "species": None,
            "source_collections": "",
        }
        result = compact_dataset(raw)
        DatasetSummary.model_validate(result)
        self.assertTrue(
            all(isinstance(result[field], list) for field in SUMMARY_FIELDS)
        )
        self.assertEqual(result["cancer_types"], ["Head, Neck", "Lung"])
        self.assertEqual(result["cancer_locations"], ["Esophagus"])
        self.assertEqual(result["species"], [])
        self.assertEqual(result["source_collections"], [])
        self.assertEqual(
            result["download_data_types"], ["CT", "MR", "PT", "Whole Slide Image"]
        )
        self.assertEqual(raw["download_data_types"], "CT; MR; PT; Whole Slide Image")

    def test_detail_and_download_fields_use_same_contract_without_splitting_prose(self):
        raw = {
            "short_title": "EXAMPLE",
            "licenses": "CC BY 4.0; Restricted",
            "program": "One; Two",
            "data_types": '["CT", "Whole Slide Image"]',
            "file_types": ["SVS"],
            "download_types": ["Radiology Images; supporting material"],
            "external_resources": None,
            "title": "Title; with comma, intact",
            "abstract": "Words; stay together, too",
            "link": "https://example.org/a;b",
        }
        detail = full_dataset(raw)
        DatasetDetail.model_validate(detail)
        self.assertTrue(all(isinstance(detail[field], list) for field in DETAIL_FIELDS))
        self.assertEqual(detail["licenses"], ["CC BY 4.0", "Restricted"])
        self.assertEqual(detail["program"], ["One", "Two"])
        self.assertEqual(
            detail["download_types"], ["Radiology Images; supporting material"]
        )
        self.assertEqual(detail["title"], raw["title"])
        self.assertEqual(detail["abstract"], raw["abstract"])
        self.assertEqual(detail["tcia_page"], raw["link"])
        row = compact_download(raw)
        Download.model_validate(row)
        self.assertTrue(all(isinstance(row[field], list) for field in DOWNLOAD_FIELDS))
        self.assertEqual(row["data_types"], ["CT", "Whole Slide Image"])

    def test_missing_fields_are_empty_lists_and_models_reject_string_regressions(self):
        summary = compact_dataset({})
        detail = full_dataset({})
        download = compact_download({})
        self.assertTrue(all(summary[field] == [] for field in SUMMARY_FIELDS))
        self.assertTrue(all(detail[field] == [] for field in DETAIL_FIELDS))
        self.assertTrue(all(download[field] == [] for field in DOWNLOAD_FIELDS))
        for field in SUMMARY_FIELDS:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                DatasetSummary.model_validate({field: "CT; MR"})
        for field in ("licenses", "program"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                DatasetDetail.model_validate({field: "One; Two"})

    def test_real_search_detail_access_and_plan_respond_consistently(self):
        fixture = fixtures.TciaQueryServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tmp.cleanup)
        path = fixture.snapshot
        with connect(path) as conn:
            conn.execute(
                "UPDATE agent_dataset_access_summary SET download_data_types='CT; MR',download_file_types='DICOM; SVS',cancer_types='Head, Neck; Lung',cancer_locations='One; Two',species='Human',program='One; Two',licenses='CC BY 4.0; Restricted',source_collections='A; B' WHERE short_title='TCGA-BRCA'"
            )
        service = fixture.service
        client = TestClient(create_app(service))
        search = client.post(
            "/v2/datasets/search", json={"short_titles": ["TCGA-BRCA"]}
        )
        self.assertEqual(search.status_code, 200, search.text)
        row = search.json()["datasets"][0]
        self.assertEqual(row["download_data_types"], ["CT", "MR"])
        self.assertEqual(row["cancer_types"], ["Head, Neck", "Lung"])
        detail = client.get("/v2/datasets/TCGA-BRCA")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["datasets"][0]["licenses"], ["CC BY 4.0", "Restricted"]
        )
        access = client.get("/v2/datasets/TCGA-BRCA/access")
        self.assertEqual(access.status_code, 200)
        self.assertTrue(
            all(
                isinstance(access.json()["dataset"][field], list)
                for field in SUMMARY_FIELDS
            )
        )
        plan = client.get("/v2/datasets/TCGA-BRCA/download-plan")
        self.assertEqual(plan.status_code, 200)
        self.assertTrue(
            all(
                isinstance(row[field], list)
                for row in plan.json()["downloads"]
                for field in DOWNLOAD_FIELDS
            )
        )

        async def run():
            async with Client(mcp) as mc:
                with patch(
                    "mcp_server.tcia_query_mcp.server.service", return_value=service
                ):
                    result = await mc.call_tool(
                        "search_datasets", {"short_titles": ["TCGA-BRCA"]}
                    )
                self.assertFalse(result.is_error)
                self.assertEqual(
                    result.structured_content["datasets"], search.json()["datasets"]
                )

        asyncio.run(run())
        with connect(path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT cancer_types FROM agent_dataset_access_summary WHERE short_title='TCGA-BRCA'"
                ).fetchone()[0],
                "Head, Neck; Lung",
            )
