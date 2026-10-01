import importlib.util
import io
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_controlled_access_metadata.py"
SPEC = importlib.util.spec_from_file_location("tcia_controlled_access_metadata", SCRIPT)
CONTROLLED = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CONTROLLED)


class ControlledAccessSourceHealthTests(unittest.TestCase):
    def test_fetch_artifact_retries_transient_url_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            response = io.BytesIO(b"manifest")
            with mock.patch.object(
                CONTROLLED.urllib.request,
                "urlopen",
                side_effect=[urllib.error.URLError("connection refused"), response],
            ) as urlopen, mock.patch.object(CONTROLLED.time, "sleep") as sleep:
                path, status, error = CONTROLLED.fetch_artifact(
                    "https://example.test/manifest.csv",
                    Path(directory),
                    no_network=False,
                )

            self.assertEqual(status, "fetched")
            self.assertEqual(error, "")
            self.assertEqual(path.read_bytes(), b"manifest")
            self.assertEqual(urlopen.call_count, 2)
            sleep.assert_called_once_with(1)

    def test_fetch_artifact_rejects_declared_oversize_without_partial_file(self) -> None:
        class Response(io.BytesIO):
            headers = {"Content-Length": "9"}

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                CONTROLLED.urllib.request, "urlopen", return_value=Response(b"oversize")
            ):
                path, status, error = CONTROLLED.fetch_artifact(
                    "https://example.test/manifest.csv",
                    Path(directory),
                    no_network=False,
                    attempts=1,
                    max_bytes=8,
                )

            self.assertIsNone(path)
            self.assertEqual(status, "error")
            self.assertIn("Content-Length 9 exceeds 8-byte limit", error)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_fetch_artifact_rejects_stream_over_limit_and_cleans_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                CONTROLLED.urllib.request, "urlopen", return_value=io.BytesIO(b"123456789")
            ):
                path, status, error = CONTROLLED.fetch_artifact(
                    "https://example.test/manifest.csv",
                    Path(directory),
                    no_network=False,
                    attempts=1,
                    max_bytes=8,
                )

            self.assertIsNone(path)
            self.assertEqual(status, "error")
            self.assertIn("exceeded 8-byte download limit", error)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_fetch_artifact_enforces_total_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                CONTROLLED.urllib.request, "urlopen", return_value=io.BytesIO(b"manifest")
            ), mock.patch.object(
                CONTROLLED.time, "monotonic", side_effect=[0, 0, 0, 6]
            ):
                path, status, error = CONTROLLED.fetch_artifact(
                    "https://example.test/manifest.csv",
                    Path(directory),
                    no_network=False,
                    attempts=1,
                    total_timeout_seconds=5,
                )

            self.assertIsNone(path)
            self.assertEqual(status, "error")
            self.assertIn("exceeded 5s total deadline", error)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_validation_reports_failed_source_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controlled.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE source_artifacts(error TEXT)")
            conn.execute("INSERT INTO source_artifacts VALUES ('connection refused')")
            for name in CONTROLLED.REQUIRED_TABLES:
                if name not in {"source_artifacts", "controlled_meta"}:
                    conn.execute(f'CREATE TABLE "{name}" (value TEXT)')
            conn.execute("CREATE TABLE controlled_meta(key TEXT, value TEXT)")
            for name in CONTROLLED.REQUIRED_VIEWS:
                conn.execute(f'CREATE VIEW "{name}" AS SELECT 1 AS value')
            conn.commit()
            conn.close()

            result = CONTROLLED.validate_db(path)

            self.assertEqual(result["integrity_check"], "ok")
            self.assertEqual(result["artifact_errors"], 1)


if __name__ == "__main__":
    unittest.main()
