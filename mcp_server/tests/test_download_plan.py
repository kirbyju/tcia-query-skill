import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from mcp import Client
from mcp_server.tcia_query_mcp.models import DownloadPlanResponse, DatasetSearchResponse
from mcp_server.tcia_query_mcp.rest import create_app
from mcp_server.tcia_query_mcp.server import mcp
from mcp_server.tcia_query_mcp.service import TciaQueryService, InvalidRequestError
from mcp_server.tests.test_service import create_base_snapshot, connect
from mcp_server.tests import test_service as service_fixtures


class DownloadPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "snapshot.sqlite"
        create_base_snapshot(self.path)
        self.service = TciaQueryService(snapshot_db=self.path)

    def test_search_exposes_existing_download_categories_without_detail_lookup(self):
        fixture = service_fixtures.TciaQueryServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tmp.cleanup)
        result = fixture.service.search_datasets(short_titles=["TCGA-BRCA"])
        DatasetSearchResponse.model_validate(result)
        self.assertIn("data_categories", result["datasets"][0])
        self.assertTrue(result["datasets"][0]["data_categories"])

    def test_plan_rest_matches_service_and_contains_one_help_catalog(self):
        plan = self.service.get_download_plan("TCGA-BRCA")
        DownloadPlanResponse.model_validate(plan)
        self.assertEqual(plan["scope"]["selected_groups"], len(plan["downloads"]))
        self.assertEqual(
            set(plan["software"]), {r["software_requirement"] for r in plan["downloads"]}
        )
        response = TestClient(create_app(self.service)).get("/v2/datasets/TCGA-BRCA/download-plan")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), plan)
        self.assertIn("requirements_url", plan["downloads"][0])
        self.assertNotIn("instructions", plan["downloads"][0])

    def test_plan_validates_selection_and_preserves_empty_scope(self):
        with self.assertRaises(InvalidRequestError):
            self.service.get_download_plan("TCGA-BRCA", download_ids=["not-real"])
        with self.assertRaises(InvalidRequestError):
            self.service.get_download_plan("TCGA-BRCA", download_ids=[])
        with self.assertRaises(InvalidRequestError):
            self.service.get_download_plan("TCGA-BRCA", access_levels=["invalid"])
        plan = self.service.get_download_plan("TCGA-BRCA", access_levels=["controlled"])
        self.assertEqual(plan["downloads"], [])
        self.assertEqual(plan["scope"]["selected_groups"], 0)
        self.assertGreater(plan["scope"]["inventory_groups"], 0)

    def test_shared_title_requires_explicit_dataset_type(self):
        with connect(self.path) as conn:
            conn.execute(
                "INSERT INTO agent_dataset_access_summary SELECT * FROM agent_dataset_access_summary WHERE short_title='TCGA-BRCA'"
            )
            conn.execute(
                "UPDATE agent_dataset_access_summary SET dataset_type='Analysis Result',source='analysis-results' WHERE rowid=(SELECT MAX(rowid) FROM agent_dataset_access_summary)"
            )
        with self.assertRaises(InvalidRequestError):
            self.service.get_download_plan("TCGA-BRCA")
        plan = self.service.get_download_plan("TCGA-BRCA", dataset_type="analysis_result")
        self.assertEqual(plan["dataset_type"], "Analysis Result")
        self.assertEqual(plan["downloads"], [])

    def test_bad_selector_gets_problem_envelope(self):
        response = TestClient(create_app(self.service)).get(
            "/v2/datasets/TCGA-BRCA/download-plan?dataset_type=wrong"
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("detail", response.json())

    def test_mcp_tool_has_same_structured_result(self):
        async def run():
            async with Client(mcp) as client:
                with patch("mcp_server.tcia_query_mcp.server.service", return_value=self.service):
                    response = await client.call_tool(
                        "get_download_plan", {"short_title": "TCGA-BRCA"}
                    )
                self.assertFalse(response.is_error)
                self.assertEqual(
                    response.structured_content, self.service.get_download_plan("TCGA-BRCA")
                )

        import asyncio

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
