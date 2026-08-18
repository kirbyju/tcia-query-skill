from __future__ import annotations

import unittest

from mcp_server.tcia_query_mcp.rest import create_app


class RestV2ContractTests(unittest.TestCase):
    def test_v2_is_documented_default_and_v1_remains_available(self) -> None:
        app = create_app()
        self.assertEqual(app.docs_url, "/v2/docs")
        paths = app.openapi()["paths"]
        self.assertIn("/v2/bundle", paths)
        self.assertIn("/v2/datasets/search", paths)
        self.assertIn("/v2/datasets/{short_title}", paths)
        self.assertIn("/v2/datasets/{short_title}/downloads", paths)
        self.assertIn("/v2/participants/search", paths)
        self.assertIn("/v2/participants/{participant_key}/assets", paths)
        self.assertIn("/v2/datasets/{short_title}/participant-coverage", paths)
        self.assertIn("/v2/public-non-dicom/assets", paths)
        self.assertIn("/v1/datasets/search", paths)
        self.assertIn("/v1/snapshot", paths)


if __name__ == "__main__":
    unittest.main()
