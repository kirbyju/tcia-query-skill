from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from mcp_server.tcia_query_mcp.rest import (
    RETIRED_SIDECAR_DETAIL,
    V1_DEPRECATION,
    V1_SUCCESSOR,
    V1_SUNSET,
    create_app,
)
from mcp_server.tcia_query_mcp.service import NotFoundError


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
        self.assertNotIn("/v1/datasets/search", paths)
        self.assertNotIn("/v1/snapshot", paths)
        route_paths = {route.path for route in app.routes}
        self.assertIn("/v1/datasets/search", route_paths)
        self.assertIn("/v1/snapshot", route_paths)
        response = TestClient(app).get("/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["deprecation"], V1_DEPRECATION)
        self.assertEqual(response.headers["sunset"], V1_SUNSET)
        self.assertEqual(response.headers["link"], V1_SUCCESSOR)
        self.assertIn("/v2/live", paths)
        self.assertIn("/v2/ready", paths)
        self.assertNotIn("/v2/health", paths)

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

    def test_v2_openapi_exposes_data_facets_and_geometry_filters(self) -> None:
        schema = create_app().openapi()
        participant_properties = schema["components"]["schemas"][
            "SearchParticipantsRequest"
        ]["properties"]
        for name in (
            "data_categories",
            "data_types",
            "file_formats",
            "geometry_statuses",
        ):
            self.assertIn(name, participant_properties)

        asset_parameters = {
            item["name"]
            for item in schema["paths"]["/v2/participants/{participant_key}/assets"][
                "get"
            ]["parameters"]
        }
        self.assertTrue(
            {"data_categories", "data_types", "file_formats", "geometry_statuses"}
            <= asset_parameters
        )
        public_parameters = {
            item["name"]
            for item in schema["paths"]["/v2/public-non-dicom/assets"]["get"][
                "parameters"
            ]
        }
        self.assertTrue({"modalities", "geometry_statuses"} <= public_parameters)

    def test_openapi_has_shared_response_and_problem_schemas(self) -> None:
        schema = create_app().openapi()
        operation = schema["paths"]["/v2/datasets/search"]["post"]
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DatasetSearchResponse",
        )
        self.assertIn("ProblemDetail", schema["components"]["schemas"])
        self.assertIn(
            "application/problem+json",
            operation["responses"]["422"]["content"],
        )
        self.assertNotIn(
            "include_hidden",
            schema["components"]["schemas"]["SearchDatasetsRequest"]["properties"],
        )
        self.assertNotIn("PublicResponse", schema["components"]["schemas"])
        dataset_fields = schema["components"]["schemas"]["DatasetSummary"]["properties"]
        self.assertFalse(
            schema["components"]["schemas"]["DatasetSearchResponse"]["additionalProperties"]
        )
        self.assertFalse(
            schema["components"]["schemas"]["DatasetSummary"]["additionalProperties"]
        )
        self.assertFalse(
            schema["components"]["schemas"]["ControlledFile"]["additionalProperties"]
        )
        self.assertTrue(
            {"date_updated", "license_status", "current_download_count", "cancer_types"}
            <= set(dataset_fields)
        )
        bundle_ref = schema["paths"]["/v2/bundle"]["get"]["responses"]["200"]
        self.assertEqual(
            bundle_ref["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/BundleResponse",
        )

    def test_service_errors_use_problem_json(self) -> None:
        class Service:
            def get_dataset(self, short_title):
                raise NotFoundError(f"missing {short_title}")

        response = TestClient(create_app(Service())).get("/v2/datasets/NOPE")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertEqual(response.json()["code"], "not_found")
        self.assertFalse(response.json()["retryable"])

    def test_v1_errors_keep_deprecation_headers(self) -> None:
        class Service:
            def get_dataset(self, short_title):
                raise NotFoundError(f"missing {short_title}")

        response = TestClient(create_app(Service())).get("/v1/datasets/NOPE")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["deprecation"], V1_DEPRECATION)
        self.assertEqual(response.headers["sunset"], V1_SUNSET)
        self.assertEqual(response.headers["link"], V1_SUCCESSOR)

    def test_retired_v1_sidecars_are_stable_gone_responses(self) -> None:
        paths = (
            "/v1/nifti/datasets",
            "/v1/nifti/STALE/files",
            "/v1/nifti/STALE/derived-objects",
            "/v1/nifti/STALE/characteristics",
            "/v1/nifti/review-issues",
            "/v1/nifti/STALE/package-files",
            "/v1/pathology/datasets",
            "/v1/pathology/downloads",
            "/v1/pathology/STALE/package-files",
            "/v1/pathology/STALE/files",
            "/v1/pathology/disparities",
        )
        app = create_app(object())
        client = TestClient(app)
        self.assertTrue(set(paths).isdisjoint(app.openapi()["paths"]))
        for path in paths:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 410)
                self.assertEqual(response.headers["content-type"], "application/problem+json")
                self.assertEqual(response.headers["deprecation"], V1_DEPRECATION)
                self.assertEqual(response.headers["sunset"], V1_SUNSET)
                self.assertEqual(response.headers["link"], V1_SUCCESSOR)
                self.assertEqual(response.json()["code"], "retired_surface")
                self.assertEqual(response.json()["detail"], RETIRED_SIDECAR_DETAIL)


if __name__ == "__main__":
    unittest.main()
