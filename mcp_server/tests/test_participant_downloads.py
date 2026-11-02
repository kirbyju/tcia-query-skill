import asyncio
import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from mcp import Client
from mcp_server.tcia_query_mcp.idc_metadata import (
    IdcMetadataClient,
    MetadataUnavailable,
)
from mcp_server.tcia_query_mcp.models import ParticipantDownloadPlanResponse
from mcp_server.tcia_query_mcp.rest import create_app
from mcp_server.tcia_query_mcp.server import mcp
from mcp_server.tcia_query_mcp.service import (
    InvalidRequestError,
    ServiceUnavailableError,
)
from mcp_server.tests import test_service as fixtures

VERSION = {
    "idc_version": "v25",
    "idc_index_data_version": "25.0.5",
    "api_version": "3.0.0b7",
    "build": "fixture",
}


def series(patient="BRCA-1", uid="1.2.3", doi="10.1234/example", modality="CT"):
    return dict(
        collection_id="verified_collection",
        analysis_result_id=None,
        source_DOI=doi,
        PatientID=patient,
        StudyInstanceUID="1.2",
        SeriesInstanceUID=uid,
        Modality=modality,
        instanceCount=7,
        series_size_MB=1.25,
        license_short_name="CC BY 4.0",
    )


class ProviderTests(unittest.TestCase):
    def test_queries_bind_parameters_and_cache_versioned_metadata(self):
        calls = []
        patient = "patient' OR 1=1 --"

        def request(path, payload, timeout):
            calls.append((path, payload))
            if path.endswith("version"):
                return dict(VERSION)
            self.assertNotIn(patient, payload["sql"])
            self.assertIn(patient, payload["params"])
            return {
                "rows": [series(patient=patient)],
                "row_count": 1,
                "truncated": False,
            }

        client = IdcMetadataClient(request)
        first = client.resolve(
            "10.1234/example", [patient], "CT", collection_ids=["verified_collection"]
        )
        first["rows"][0]["PatientID"] = "changed"
        second = client.resolve(
            "10.1234/example", [patient], "CT", collection_ids=["verified_collection"]
        )
        self.assertEqual(second["rows"][0]["PatientID"], patient)
        self.assertEqual(len(calls), 4)

    def test_provider_failure_truncation_and_wrong_scope_never_become_zero_matches(
        self,
    ):
        cases = [
            {"rows": [], "row_count": 0, "truncated": True},
            {"rows": [series(patient="other")], "row_count": 1, "truncated": False},
            {"rows": [series(doi="10.1234/other")], "row_count": 1, "truncated": False},
            {"rows": [series(uid="not-a-uid")], "row_count": 1, "truncated": False},
            {"rows": [], "row_count": 1, "truncated": False},
        ]
        for result in cases:
            with self.subTest(result=result):
                client = IdcMetadataClient(
                    lambda path, payload, timeout: (
                        dict(VERSION) if path.endswith("version") else result
                    )
                )
                with self.assertRaises(MetadataUnavailable):
                    client.resolve("10.1234/example", ["BRCA-1"], "CT")

        def broken(*args):
            raise OSError("private diagnostic")

        with self.assertRaisesRegex(MetadataUnavailable, "unavailable"):
            IdcMetadataClient(broken).resolve("10.1234/example", ["BRCA-1"], "CT")

    def test_version_change_is_rejected(self):
        versions = iter([VERSION, dict(VERSION, idc_index_data_version="25.0.6")])
        client = IdcMetadataClient(
            lambda path, payload, timeout: (
                next(versions)
                if path.endswith("version")
                else {"rows": [], "row_count": 0, "truncated": False}
            )
        )
        with self.assertRaisesRegex(MetadataUnavailable, "version changed"):
            client.resolve("10.1234/example", ["BRCA-1"], "CT")

    def test_exact_page_boundary_is_followed_with_keyset_pagination(self):
        rows = [series(uid="1.2." + str(i).zfill(4)) for i in range(1001)]
        pages = iter([rows[:1000], rows[1000:]])

        def request(path, payload, timeout):
            if path.endswith("version"):
                return dict(VERSION)
            page = next(pages)
            return {"rows": page, "row_count": len(page), "truncated": False}

        self.assertEqual(
            len(
                IdcMetadataClient(request).resolve("10.1234/example", ["BRCA-1"], "CT")[
                    "rows"
                ]
            ),
            1001,
        )


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TciaQueryServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tmp.cleanup)
        self.service = self.fixture.service
        with fixtures.connect(self.service.participant_db) as conn:
            conn.execute(
                "INSERT INTO agent_participant_search SELECT * FROM agent_participant_search LIMIT 1"
            )
            conn.execute(
                "UPDATE agent_participant_search SET participant_key='participant-2',display_participant_id='BRCA-2' WHERE rowid=(SELECT max(rowid) FROM agent_participant_search)"
            )
            for n in (1, 2):
                conn.execute(
                    "INSERT INTO agent_participant_identifiers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"idc-{n}",
                        f"participant-{n}",
                        "crdc_idc",
                        "tcia_dataset:TCGA-BRCA",
                        f"BRCA-{n}",
                        f"BRCA-{n}",
                        "idc_index_participant_projection",
                        "{}",
                        "Collection",
                        "TCGA-BRCA",
                        f"BRCA-{n}",
                    ),
                )
        self.calls = []
        self.service._idc_metadata = self

    def resolve(self, doi, ids, modality, **kwargs):
        self.calls.append((doi, ids, modality, kwargs))
        return {
            "rows": [series(doi=doi, modality=modality)] if "BRCA-1" in ids else [],
            "version": dict(VERSION),
        }

    def plan(self, keys=None, **kwargs):
        return self.service.get_participant_download_plan(
            keys or ["participant-1", "participant-2"],
            modality=kwargs.get("modality", "CT"),
            file_format=kwargs.get("file_format", "DICOM"),
        )

    def test_server_resolves_identities_and_reports_zero_matches(self):
        plan = self.plan()
        ParticipantDownloadPlanResponse.model_validate(plan)
        self.assertTrue(plan["complete"])
        self.assertEqual(
            [r["state"] for r in plan["coverage"]], ["matched", "no_matches"]
        )
        self.assertEqual(plan["rows"][0]["participant_key"], "participant-1")
        self.assertEqual(plan["rows"][0]["size_bytes"], 1250000)
        self.assertEqual(plan["providers"]["IDC"], VERSION)
        self.assertEqual(self.calls[0][1], ["BRCA-1", "BRCA-2"])

    def test_missing_verified_identifier_is_unresolved_not_a_guessed_patient(self):
        with fixtures.connect(self.service.participant_db) as conn:
            conn.execute(
                "DELETE FROM agent_participant_identifiers WHERE managed_system='crdc_idc'"
            )
        result = self.plan()
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["unresolved"]), 2)
        self.assertFalse(self.calls)

    def test_ambiguous_provider_identifiers_remain_unresolved(self):
        with fixtures.connect(self.service.participant_db) as conn:
            conn.execute(
                "UPDATE agent_participant_identifiers SET raw_identifier='BRCA-1' WHERE managed_system='crdc_idc'"
            )
        result = self.plan()
        self.assertFalse(result["complete"])
        self.assertFalse(self.calls)
        self.assertEqual(
            [r["state"] for r in result["coverage"]], ["unresolved", "unresolved"]
        )

    def test_wrong_provider_scope_and_upstream_failure_are_service_errors(self):
        with patch.object(
            self.service._idc_metadata,
            "resolve",
            return_value={"rows": [series(patient="other")], "version": VERSION},
        ):
            with self.assertRaisesRegex(ServiceUnavailableError, "out-of-scope"):
                self.plan()
        with patch.object(
            self.service._idc_metadata,
            "resolve",
            side_effect=MetadataUnavailable("Unavailable"),
        ):
            response = TestClient(create_app(self.service)).post(
                "/v2/participants/download-plan",
                json={
                    "participant_keys": ["participant-1"],
                    "modality": "CT",
                    "file_format": "DICOM",
                },
            )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["code"], "service_unavailable")

    def test_non_dicom_uses_shared_public_routes_without_idc(self):
        with fixtures.connect(self.service.public_non_dicom_db) as conn:
            conn.execute(
                "UPDATE agent_public_non_dicom_assets SET source_system='tcia_pathdb',media_kind='whole_slide_image',"
                "file_format='SVS',modality='Whole Slide Image',subject_id='BRCA-1',subject_id_namespace='tcia_dataset:TCGA-BRCA',"
                "asset_granularity='file',source_url='https://pathdb.cancerimagingarchive.net/system/files/wsi/example/a.svs' "
                "WHERE asset_id='pnd-1'"
            )
        result = self.plan(
            ["participant-1"], modality="Whole Slide Image", file_format="non-DICOM"
        )
        ParticipantDownloadPlanResponse.model_validate(result)
        self.assertFalse(self.calls)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["provider"], "TCIA")
        self.assertTrue(result["rows"][0]["python_supported"])

    def test_rest_and_mcp_match_and_do_not_accept_credentials_or_raw_identifiers(self):
        expected = self.plan(["participant-1"])
        body = {
            "participant_keys": ["participant-1"],
            "modality": "CT",
            "file_format": "DICOM",
        }
        client = TestClient(create_app(self.service))
        self.assertEqual(
            client.post("/v2/participants/download-plan", json=body).json(), expected
        )
        self.assertEqual(
            client.post(
                "/v2/participants/download-plan", json={**body, "credential": "secret"}
            ).status_code,
            422,
        )

        async def run():
            async with Client(mcp) as client:
                with patch(
                    "mcp_server.tcia_query_mcp.server.service",
                    return_value=self.service,
                ):
                    result = await client.call_tool(
                        "get_participant_download_plan", body
                    )
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content, expected)

        asyncio.run(run())

    def test_missing_components_and_invalid_provider_counts_are_unavailable(self):
        with patch.object(
            self.service, "_file_generation", side_effect=FileNotFoundError
        ):
            with self.assertRaises(ServiceUnavailableError):
                self.plan()
        bad = series(doi=self.service.get_dataset("TCGA-BRCA")["datasets"][0]["doi"])
        bad["instanceCount"] = -1
        with patch.object(
            self.service._idc_metadata,
            "resolve",
            return_value={"rows": [bad], "version": VERSION},
        ):
            with self.assertRaisesRegex(ServiceUnavailableError, "contract"):
                self.plan()

    def test_limits_invalid_keys_and_snapshot_switch_are_not_silent(self):
        with self.assertRaises(InvalidRequestError):
            self.service.get_participant_download_plan(
                [], modality="CT", file_format="DICOM"
            )
        with self.assertRaises(InvalidRequestError):
            self.plan(["x"] * 101)
        original = self.service._file_generation
        calls = []

        def generation(path):
            calls.append(path)
            return original(path) if len(calls) <= 2 else ("changed",)

        with patch.object(self.service, "_file_generation", side_effect=generation):
            with self.assertRaisesRegex(ServiceUnavailableError, "changed"):
                self.plan()


if __name__ == "__main__":
    unittest.main()
