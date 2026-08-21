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
        self.assertIn("/v2/datasets/{short_title}/versions", paths)
        self.assertIn("/v2/release-history/v1-releases", paths)
        self.assertIn("/v2/controlled-access/datasets", paths)
        self.assertIn("/v2/controlled-access/{short_title}/files", paths)
        self.assertIn("/v2/dicom/annotation-downloads", paths)
        self.assertIn("/v2/clinical/datasets", paths)
        self.assertIn("/v2/clinical/{short_title}/subjects", paths)
        self.assertIn("/v2/clinical/{short_title}/facts", paths)
        self.assertIn("/v2/clinical/{short_title}/conflicts", paths)
        self.assertNotIn("/v2/nifti/datasets", paths)
        self.assertNotIn("/v2/pathology/datasets", paths)
        self.assertIn("/v1/datasets/search", paths)
        self.assertIn("/v1/snapshot", paths)

    def test_v2_bundle_uses_lightweight_manifest_info(self) -> None:
        class Service:
            def bundle_info(self):
                return {"v2_bundle": {"release_fingerprint": "test"}}

            def snapshot_info(self):
                raise AssertionError("V2 bundle endpoint must not recount SQLite views")

        app = create_app(Service())
        route = next(route for route in app.routes if route.path == "/v2/bundle")
        self.assertEqual(
            route.endpoint(), {"v2_bundle": {"release_fingerprint": "test"}}
        )


if __name__ == "__main__":
    unittest.main()
