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
