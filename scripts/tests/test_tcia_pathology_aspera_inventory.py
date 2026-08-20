import base64
import csv
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from tcia_pathology_aspera_inventory import (  # noqa: E402
    FaspexPublicClient,
    OUTPUT_COLUMNS,
    file_row,
)


class FaspexPublicInventoryTests(unittest.TestCase):
    def test_public_context_decode_and_config_values(self):
        payload = {"resource": "packages", "type": "external_download_package", "id": "1305"}
        context = base64.b64encode(json.dumps(payload).encode()).decode()

        self.assertEqual(FaspexPublicClient._decode_context(context), payload)
        self.assertEqual(FaspexPublicClient._config_value("window.x={client_id: 'abc'}", "client_id"), "abc")

    def test_paged_public_browse_follows_iteration_token(self):
        client = FaspexPublicClient(
            api_url="https://example.test/api/v5",
            auth_url="https://example.test/auth",
            client_id="client",
            redirect_uri="https://example.test/token",
            context="context",
            package_id="1305",
            timeout=30,
        )
        client._authorization = "Bearer test"
        client._pagination_browsing = True
        calls = []

        def fake_request(url, *, data=None, authorization=""):
            calls.append((url, data, authorization))
            if "iteration_token=" not in url:
                return ({"items": [{"path": "/ML_dataset/001/a.jpg", "type": "file"}]}, {"x-aspera-next-iteration-token": "next"})
            return ({"items": [{"path": "/ML_dataset/001/labels.csv", "type": "file"}]}, {})

        with mock.patch.object(client, "_request_json", side_effect=fake_request):
            rows = client.browse("/ML_dataset/001")

        self.assertEqual(
            [row["path"] for row in rows],
            ["/ML_dataset/001/a.jpg", "/ML_dataset/001/labels.csv"],
        )
        self.assertTrue(all(row["_inventory_source"] == "faspex public API" for row in rows))
        self.assertTrue(calls[0][0].endswith("/files/received/page"))
        self.assertTrue(calls[1][0].endswith("/files/received/page?iteration_token=next"))
        self.assertEqual(calls[0][1], {"path": "/ML_dataset/001", "filters": {}})

    def test_file_row_preserves_public_api_provenance_and_mtime(self):
        download = {
            "download_row_id": 1,
            "short_title": "Hungarian-Colorectal-Screening",
            "download_id": "55034",
            "download_title": "HunCRC patch dataset",
            "download_url": "https://example.test/?context=redacted",
        }
        entry = {
            "path": "/ML_dataset/zoom_1_partA/001/a.jpg",
            "basename": "a.jpg",
            "type": "file",
            "size": 42,
            "mtime": "2026-06-24T15:16:00Z",
            "_inventory_source": "faspex public API",
        }

        row = file_row(download, entry, "2026-08-20T00:00:00Z")

        self.assertEqual(row["file_name"], "a.jpg")
        self.assertEqual(row["file_ext"], ".jpg")
        self.assertEqual(row["modified_time"], "2026-06-24T15:16:00Z")
        self.assertEqual(row["inventory_source"], "faspex public API")
        self.assertNotIn("_inventory_source", json.loads(row["row_json"]))
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=OUTPUT_COLUMNS, delimiter="\t")
        writer.writerow(row)


if __name__ == "__main__":
    unittest.main()
