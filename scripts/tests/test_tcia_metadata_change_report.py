#!/usr/bin/env python3

from __future__ import annotations

import sqlite3
import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_metadata_change_report.py"
SPEC = importlib.util.spec_from_file_location("tcia_metadata_change_report", SCRIPT)
change_report = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = change_report
SPEC.loader.exec_module(change_report)
REGISTRY_SPEC = importlib.util.spec_from_file_location(
    "tcia_correction_registry_for_change_report",
    SCRIPT.parent / "tcia_correction_registry.py",
)
registry = importlib.util.module_from_spec(REGISTRY_SPEC)
assert REGISTRY_SPEC.loader is not None
sys.modules[REGISTRY_SPEC.name] = registry
REGISTRY_SPEC.loader.exec_module(registry)


class MetadataChangeReportTest(unittest.TestCase):
    def test_high_severity_key_must_equal_ordered_sqlite_primary_key(self) -> None:
        with sqlite3.connect(":memory:") as conn:
            conn.execute(
                "CREATE TABLE keyed (a TEXT,b TEXT,value TEXT,PRIMARY KEY (a,b))"
            )
            change_report.validate_declared_key(
                conn, change_report.TableSpec("keyed", ("a", "b"), "high")
            )
            with self.assertRaisesRegex(RuntimeError, "differs from SQLite primary key"):
                change_report.validate_declared_key(
                    conn, change_report.TableSpec("keyed", ("a",), "high")
                )
            with self.assertRaisesRegex(RuntimeError, "differs from SQLite primary key"):
                change_report.validate_declared_key(
                    conn, change_report.TableSpec("keyed", ("b", "a"), "high")
                )

    def test_independent_registry_build_times_are_nonsemantic_but_validation_results_are_semantic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.sqlite"
            new = root / "new.sqlite"
            report = root / "same.json"
            registry.build_registry(old, observed_at="2026-09-11T00:00:00Z")
            registry.build_registry(new, observed_at="2026-09-12T00:00:00Z")

            same = subprocess.run(
                [
                    sys.executable, str(SCRIPT),
                    "--correction-new", str(new),
                    "--correction-old", str(old),
                    "--json-out", str(report),
                    "--fail-on-unexplained-high",
                ],
                text=True, capture_output=True,
            )
            self.assertEqual(same.returncode, 0, same.stdout + same.stderr)
            baseline = json.loads(report.read_text())
            correction_rows = [
                row for row in baseline["comparisons"] if row["asset"] == "correction"
            ]
            self.assertTrue(correction_rows)
            self.assertTrue(all(
                row["added"] == row["removed"] == row["modified"] == 0
                for row in correction_rows
            ))
            baseline_digest = baseline["report_sha256"]
            repeated_report = root / "same-repeated.json"
            repeated = subprocess.run(
                [
                    sys.executable, str(SCRIPT),
                    "--correction-new", str(new),
                    "--correction-old", str(old),
                    "--json-out", str(repeated_report),
                    "--fail-on-unexplained-high",
                ],
                text=True, capture_output=True,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            self.assertEqual(
                json.loads(repeated_report.read_text())["report_sha256"],
                baseline_digest,
            )

            for column, value in (("status", "failed"), ("evidence_sha256", "f" * 64)):
                with self.subTest(column=column):
                    changed = root / f"changed-{column}.sqlite"
                    changed_report = root / f"changed-{column}.json"
                    shutil.copyfile(new, changed)
                    with sqlite3.connect(changed) as conn:
                        conn.execute(
                            f"UPDATE correction_validations SET {column}=?",
                            (value,),
                        )
                        conn.commit()
                    result = subprocess.run(
                        [
                            sys.executable, str(SCRIPT),
                            "--correction-new", str(changed),
                            "--correction-old", str(old),
                            "--json-out", str(changed_report),
                            "--fail-on-unexplained-high",
                        ],
                        text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    payload = json.loads(changed_report.read_text())
                    self.assertNotEqual(payload["report_sha256"], baseline_digest)
                    validation = next(
                        row for row in payload["comparisons"]
                        if row["asset"] == "correction"
                        and row["table"] == "correction_validations"
                    )
                    self.assertEqual(validation["modified"], 1)
                    self.assertTrue(any(
                        item.startswith("correction_registry.correction_validations:")
                        for item in payload["unexplained_high_severity"]
                    ))

    def test_report_digest_is_semantic_across_repeated_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "correction.sqlite"
            self._correction_meta(path, "2026-09-10T00:00:00Z", "verified_current")
            values = {f"{name}_{side}": None for name in change_report.PROFILES for side in ("new", "old")}
            values.update(correction_new=str(path), correction_old=str(path), max_items=20)
            first_args = argparse.Namespace(**values, generated_at_utc="2026-09-11T00:00:00+00:00")
            second_args = argparse.Namespace(**values, generated_at_utc="2026-09-12T00:00:00+00:00")
            first = change_report.build_report(first_args)[2]
            second = change_report.build_report(second_args)[2]
            self.assertNotEqual(first["generated_at_utc"], second["generated_at_utc"])
            self.assertEqual(first["report_sha256"], second["report_sha256"])

    def test_correction_baseline_requires_exact_bootstrap_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "correction.sqlite"
            report = root / "report.json"
            self._correction_meta(db, "2026-09-11T00:00:00Z", "verified_current")
            evidence = {
                "prior_published_bundle": {
                    "schema_version": 2,
                    "correction_component_absent": True,
                    "release_fingerprint": "a" * 64,
                    "manifest_sha256": "b" * 64,
                },
                "initial_registry": {
                    "decision_set_sha256": "same",
                    "source_health": "verified_current",
                },
                "authorization_scope": {
                    "allows_initial_registry_baseline": True,
                    "allows_metadata_row_changes": False,
                    "allows_future_missing_or_corrupt_registry": False,
                },
                "baseline_mode": {
                    "reason": "component_absent_in_verified_prior_contract",
                    "gating_disposition": "baseline_established_not_compared",
                },
            }
            evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
            import hashlib
            evidence_sha = hashlib.sha256(evidence_json.encode()).hexdigest()
            with sqlite3.connect(db) as conn:
                conn.executemany("INSERT INTO registry_meta VALUES (?,?)", [
                    ("bootstrap_evidence_json", evidence_json),
                    ("bootstrap_evidence_sha256", evidence_sha),
                    ("bootstrap_prior_release_fingerprint", "a" * 64),
                ])
                conn.execute(
                    """CREATE TABLE correction_validations (
                       validation_id TEXT PRIMARY KEY,rule_id TEXT,status TEXT,
                       evidence_sha256 TEXT,executed_at TEXT)"""
                )
                conn.execute(
                    "INSERT INTO correction_validations VALUES ('v','initial_registry_bootstrap','passed',?,'now')",
                    (evidence_sha,),
                )
                conn.commit()
            accepted = subprocess.run(
                [sys.executable, str(SCRIPT), "--correction-new", str(db),
                 "--json-out", str(report), "--fail-on-unexplained-high"],
                text=True, capture_output=True,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
            baseline = json.loads(report.read_text())["baseline_modes"]
            self.assertEqual(baseline[0]["gating_disposition"], "baseline_established_not_compared")
            old_public = root / "old-public.sqlite"
            new_public = root / "new-public.sqlite"
            self._public(old_public, [("old", "A", "same")])
            self._public(new_public, [("new", "A", "same")])
            unrelated = subprocess.run(
                [
                    sys.executable, str(SCRIPT), "--correction-new", str(db),
                    "--public-old", str(old_public), "--public-new", str(new_public),
                    "--json-out", str(report), "--fail-on-unexplained-high",
                ],
                text=True, capture_output=True,
            )
            self.assertEqual(unrelated.returncode, 2)
            self.assertEqual(len(json.loads(report.read_text())["baseline_modes"]), 1)
            self.assertIn("public_non_dicom.public_non_dicom_assets", unrelated.stdout)
            with sqlite3.connect(db) as conn:
                conn.execute("DELETE FROM registry_meta WHERE key='bootstrap_evidence_sha256'")
                conn.commit()
            rejected = subprocess.run(
                [sys.executable, str(SCRIPT), "--correction-new", str(db),
                 "--fail-on-unexplained-high"], text=True, capture_output=True,
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("hash-valid bootstrap evidence", rejected.stdout)

    def test_reports_new_dataset_and_screening_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_snapshot = root / "old-snapshot.sqlite"
            new_snapshot = root / "new-snapshot.sqlite"
            old_clinical = root / "old-clinical.sqlite"
            new_clinical = root / "new-clinical.sqlite"
            report = root / "report.md"
            self._snapshot(old_snapshot, include_new=False)
            self._snapshot(new_snapshot, include_new=True)
            self._clinical(old_clinical, include_review=False)
            self._clinical(new_clinical, include_review=True)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--snapshot-new",
                    str(new_snapshot),
                    "--snapshot-old",
                    str(old_snapshot),
                    "--clinical-new",
                    str(new_clinical),
                    "--clinical-old",
                    str(old_clinical),
                    "--markdown-out",
                    str(report),
                    "--github-actions",
                ],
                check=True,
                text=True,
                capture_output=True,
            )

            markdown = report.read_text()
            self.assertIn("Collection / NEW", markdown)
            self.assertIn("Clinical screening review queue", markdown)
            self.assertIn("`SCREEN`", markdown)
            self.assertIn("Breast Cancer", markdown)
            self.assertIn("Inferred rows applied", markdown)
            self.assertIn("Subjects suppressed", markdown)
            self.assertIn("Curated clinical screening resolutions", markdown)
            self.assertIn("`ACRIN-6698`", markdown)
            self.assertIn(
                "::warning title=TCIA metadata review::"
                "snapshot: agent_datasets added 1 row",
                result.stdout,
            )
            self.assertIn(
                "clinical screening review required: SCREEN",
                result.stdout,
            )
            self.assertIn(
                "clinical screening review resolved: ACRIN-6698",
                result.stdout,
            )

    def test_streaming_digest_detects_equal_count_substitution_and_modification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_public = root / "old-public.sqlite"
            new_public = root / "new-public.sqlite"
            report_json = root / "report.json"
            self._public(old_public, [("a", "A", "raw-a"), ("b", "B", "raw-b")])
            self._public(new_public, [("a", "A2", "raw-a"), ("c", "C", "raw-c")])
            result = subprocess.run(
                [
                    sys.executable, str(SCRIPT),
                    "--public-new", str(new_public),
                    "--public-old", str(old_public),
                    "--json-out", str(report_json),
                    "--fail-on-unexplained-high",
                ],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            payload = json.loads(report_json.read_text())
            assets = next(
                row for row in payload["comparisons"]
                if row["table"] == "public_non_dicom_assets"
            )
            self.assertEqual(assets["new"], assets["old"])
            self.assertEqual(
                (assets["added"], assets["removed"], assets["modified"]),
                (1, 1, 1),
            )
            self.assertEqual(len(payload["report_sha256"]), 64)
            self.assertTrue(payload["unexplained_high_severity"])

    def test_same_content_pk_substitution_reports_one_to_one_migration_but_stays_gated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.sqlite"
            new = root / "new.sqlite"
            report = root / "report.json"
            for path, fact_id in ((old, "legacy-id"), (new, "canonical-id")):
                with sqlite3.connect(path) as conn:
                    conn.execute(
                        "CREATE TABLE clinical_facts (fact_id TEXT PRIMARY KEY,payload TEXT)"
                    )
                    conn.execute(
                        "INSERT INTO clinical_facts VALUES (?, 'unchanged')", (fact_id,)
                    )
            result = subprocess.run(
                [
                    sys.executable, str(SCRIPT), "--clinical-new", str(new),
                    "--clinical-old", str(old), "--json-out", str(report),
                    "--fail-on-unexplained-high",
                ],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            payload = json.loads(report.read_text())
            self.assertEqual(len(payload["primary_key_migrations"]), 1)
            migration = payload["primary_key_migrations"][0]
            self.assertEqual(migration["old_primary_key"], [["fact_id", "legacy-id"]])
            self.assertEqual(migration["new_primary_key"], [["fact_id", "canonical-id"]])
            self.assertEqual(len(payload["semantic_changes"]), 2)
            self.assertEqual(len(payload["unexplained_high_severity"]), 2)

    def test_strict_gate_requires_exact_single_use_pk_kind_and_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.sqlite"
            new = root / "new.sqlite"
            explanations = root / "corrections.sqlite"
            report = root / "report.json"
            self._public(old, [("a", "A", "raw-a")])
            self._public(new, [("a", "A2", "raw-a")])
            registry.build_registry(explanations, observed_at="2026-09-11T00:00:00Z")
            spec = change_report.PROFILES["public"][0]
            with sqlite3.connect(old) as old_conn, sqlite3.connect(new) as new_conn:
                old_digest = next(change_report.keyed_row_digests(old_conn, spec))[1]
                new_digest = next(change_report.keyed_row_digests(new_conn, spec))[1]
            with sqlite3.connect(explanations) as conn:
                effect_id, revision_id = conn.execute(
                    """SELECT e.effect_id,e.revision_id FROM correction_effects e
                       JOIN correction_decisions d USING(revision_id)
                       JOIN correction_cases c ON c.current_revision_id=d.revision_id
                       WHERE d.status='approved' LIMIT 1"""
                ).fetchone()
                conn.execute(
                    """UPDATE correction_effects
                       SET artifact='public_non_dicom',entity_table='public_non_dicom_assets',
                           entity_id=?,effect_kind='modified',before_sha256=?,
                           after_sha256=?,effect_status='approved'
                       WHERE effect_id=?""",
                    (json.dumps([["asset_id", "a"]]), old_digest, new_digest, effect_id),
                )
                conn.commit()

            command = [
                sys.executable, str(SCRIPT), "--public-new", str(new),
                "--public-old", str(old), "--explanations-db", str(explanations),
                "--json-out", str(report), "--fail-on-unexplained-high",
            ]
            exact = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(exact.returncode, 0, exact.stdout + exact.stderr)
            payload = json.loads(report.read_text())
            self.assertEqual(payload["semantic_explanations"]["consumed_effect_ids"], [effect_id])

            with sqlite3.connect(explanations) as conn:
                conn.execute(
                    "UPDATE correction_effects SET entity_id=? WHERE effect_id=?",
                    (json.dumps([["wrong_key", "a"]]), effect_id),
                )
                conn.commit()
            partial = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(partial.returncode, 2)

            with sqlite3.connect(explanations) as conn:
                conn.execute(
                    "UPDATE correction_effects SET entity_id=? WHERE effect_id=?",
                    (json.dumps([["asset_id", "a"]]), effect_id),
                )
                values = conn.execute(
                    "SELECT * FROM correction_effects WHERE effect_id=?", (effect_id,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO correction_effects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("duplicate-effect", *values[1:]),
                )
                conn.commit()
            duplicate = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(duplicate.returncode, 2)
            self.assertTrue(json.loads(report.read_text())["semantic_explanations"]["duplicate_matches"])

    def test_correction_meta_ignores_build_time_but_gates_source_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.sqlite"
            same = root / "same.sqlite"
            changed = root / "changed.sqlite"
            self._correction_meta(old, "2026-09-10T00:00:00Z", "verified_current")
            self._correction_meta(same, "2026-09-11T00:00:00Z", "verified_current")
            self._correction_meta(changed, "2026-09-11T00:00:00Z", "degraded")
            unchanged = subprocess.run(
                [sys.executable, str(SCRIPT), "--correction-new", str(same),
                 "--correction-old", str(old), "--fail-on-unexplained-high"],
                text=True, capture_output=True,
            )
            self.assertEqual(unchanged.returncode, 0, unchanged.stdout + unchanged.stderr)
            degraded = subprocess.run(
                [sys.executable, str(SCRIPT), "--correction-new", str(changed),
                 "--correction-old", str(old), "--fail-on-unexplained-high"],
                text=True, capture_output=True,
            )
            self.assertEqual(degraded.returncode, 2)
            self.assertIn("correction_registry.registry_meta", degraded.stdout)

    @staticmethod
    def _public(path: Path, rows: list[tuple[str, str, str]]) -> None:
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE public_non_dicom_assets ("
            "asset_id TEXT PRIMARY KEY, modality TEXT, raw_values_json TEXT)"
        )
        conn.executemany("INSERT INTO public_non_dicom_assets VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()

    @staticmethod
    def _correction_meta(path: Path, generated_at: str, source_health: str) -> None:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE registry_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO registry_meta VALUES (?,?)",
            [
                ("generated_at_utc", generated_at),
                ("source_health", source_health),
                ("source_health_json", json.dumps({"status": source_health})),
                ("active_decision_set_sha256", "same"),
            ],
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _snapshot(path: Path, *, include_new: bool) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE agent_datasets (
                dataset_type TEXT, short_title TEXT, title TEXT
            );
            CREATE TABLE agent_current_downloads (
                dataset_type TEXT, short_title TEXT, download_id TEXT
            );
            CREATE TABLE agent_datacite_dois (doi TEXT);
            CREATE TABLE agent_pathdb_slides (slide_id TEXT);
            INSERT INTO agent_datasets VALUES
                ('Collection', 'OLD', 'Old Dataset');
            """
        )
        if include_new:
            conn.execute(
                "INSERT INTO agent_datasets VALUES ('Collection', 'NEW', 'New Dataset')"
            )
        conn.commit()
        conn.close()

    @staticmethod
    def _clinical(path: Path, *, include_review: bool) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE clinical_sources (source_id TEXT);
            CREATE TABLE clinical_downloads (source_id TEXT);
            CREATE TABLE clinical_idc_tables (
                collection_id TEXT, table_name TEXT
            );
            CREATE TABLE clinical_imaging_subjects (subject_key TEXT);
            CREATE TABLE clinical_rows (source_row_id TEXT);
            CREATE TABLE clinical_facts (fact_id TEXT PRIMARY KEY);
            CREATE TABLE clinical_subjects (subject_key TEXT PRIMARY KEY);
            CREATE TABLE clinical_dataset_inferences (
                short_title TEXT,
                concept TEXT,
                raw_value TEXT,
                review_required INTEGER,
                review_reason TEXT,
                review_evidence TEXT,
                screening_signal TEXT,
                candidate_subjects INTEGER,
                subjects_applied INTEGER,
                subjects_suppressed INTEGER
            );
            CREATE TABLE clinical_build_warnings (warning_id INTEGER);
            """
        )
        if include_review:
            conn.execute(
                """INSERT INTO clinical_dataset_inferences VALUES
                   ('SCREEN', 'primary_diagnosis', 'Breast Cancer', 1,
                    'screening_single_diagnosis_without_non_cancer',
                    '', 'title:Screening', 500, 0, 500)"""
            )
            conn.execute(
                """INSERT INTO clinical_dataset_inferences VALUES
                   ('ACRIN-6698', 'primary_diagnosis', 'Breast Cancer', 0,
                    'screening_review_resolved_confirmed_diagnosis',
                    'TCIA confirms invasive breast cancer enrollment.',
                    'detailed_description:screened', 385, 385, 0)"""
            )
        conn.commit()
        conn.close()


if __name__ == "__main__":
    unittest.main()
