from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_diagnostic_status.py"
SPEC = importlib.util.spec_from_file_location("tcia_diagnostic_status", SCRIPT)
status = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(status)


class DiagnosticStatusTests(unittest.TestCase):
    def test_pre_report_failure_exit2_and_success_phases_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "status.json"
            initialized = status.initialize(sentinel, run_id="123", run_attempt="2")
            self.assertEqual(initialized["artifact_role"], "diagnostics_only_nonvalidated")
            self.assertFalse(initialized["eligible_as_release_input"])
            serialized = sentinel.read_text()
            for secret_name in ("GH_TOKEN", "credential", "password", "secret"):
                self.assertNotIn(secret_name, serialized)

            finalized = status.finalize(
                sentinel,
                job_status="failure",
                report_json=root / "missing.json",
                report_markdown=root / "missing.md",
            )
            self.assertEqual(finalized["stage"], "failed_before_semantic_report")
            self.assertEqual(finalized["disposition"], "pre_report_failure")
            self.assertEqual(finalized["job_status_before_diagnostic_upload"], "failure")

            pre_report = status.update(
                sentinel,
                stage="semantic_report_failed",
                disposition="failed_before_report_serialization",
                exit_code=1,
                report_json=root / "missing.json",
                report_markdown=root / "missing.md",
            )
            self.assertFalse(pre_report["report_json_present"])
            self.assertFalse(pre_report["report_markdown_present"])

            report_json = root / "report.json"
            report_markdown = root / "report.md"
            report_json.write_text("{}\n")
            report_markdown.write_text("report\n")
            gated = status.update(
                sentinel,
                stage="semantic_report_failed",
                disposition="unexplained_high_rejected",
                exit_code=2,
                report_json=report_json,
                report_markdown=report_markdown,
            )
            self.assertTrue(gated["report_json_present"])
            self.assertTrue(gated["report_markdown_present"])
            gated_final = status.finalize(
                sentinel,
                job_status="failure",
                report_json=report_json,
                report_markdown=report_markdown,
            )
            self.assertEqual(gated_final["disposition"], "unexplained_high_rejected")
            self.assertEqual(gated_final["exit_code"], 2)

            complete = status.update(
                sentinel,
                stage="source_artifacts_validated",
                disposition="success",
                exit_code=0,
                report_json=report_json,
                report_markdown=report_markdown,
            )
            self.assertEqual(complete["disposition"], "success")
            self.assertTrue(json.loads(sentinel.read_text())["diagnostics_only"])

    def test_tampered_release_role_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "status.json"
            status.initialize(sentinel, run_id="1", run_attempt="1")
            payload = json.loads(sentinel.read_text())
            payload["eligible_as_release_input"] = True
            sentinel.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "invalid artifact role"):
                status.update(
                    sentinel,
                    stage="semantic_report_started",
                    disposition="pending",
                    exit_code=None,
                    report_json=None,
                    report_markdown=None,
                )
            with self.assertRaisesRegex(ValueError, "invalid artifact role"):
                status.finalize(
                    sentinel,
                    job_status="failure",
                    report_json=None,
                    report_markdown=None,
                )


if __name__ == "__main__":
    unittest.main()
