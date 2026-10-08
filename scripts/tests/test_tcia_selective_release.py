import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import tcia_selective_release as SELECTIVE


def build_snapshot(path: Path, *, core_value: str = "core", citation_count: int = 1) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE snapshot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE wordpress_records (id TEXT PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE datacite_dois (doi TEXT PRIMARY KEY, citation_count INTEGER NOT NULL);
            CREATE TABLE tcia_publications (rec_number TEXT PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE tcia_publication_dataset_dois (
                rec_number TEXT NOT NULL, dataset_doi TEXT NOT NULL,
                PRIMARY KEY (rec_number, dataset_doi)
            );
            CREATE VIEW agent_datasets AS SELECT id, title FROM wordpress_records;
            CREATE VIEW agent_datacite_dois AS SELECT * FROM datacite_dois;
            CREATE VIEW agent_tcia_publications AS SELECT * FROM tcia_publications;
            """
        )
        conn.execute("INSERT INTO snapshot_meta VALUES ('generated_at_utc', 'now')")
        conn.execute("INSERT INTO wordpress_records VALUES ('1', ?)", (core_value,))
        conn.execute("INSERT INTO datacite_dois VALUES ('10.1/example', ?)", (citation_count,))
        conn.execute("INSERT INTO tcia_publications VALUES ('1', 'verified use')")
        conn.execute("INSERT INTO tcia_publication_dataset_dois VALUES ('1', '10.1/example')")


def reusable_manifest() -> dict:
    components = {}
    for component in SELECTIVE.CARRIED_COMPONENTS:
        source = f"{component}.component"
        components[component] = {
            "status": "healthy",
            "sources": {source: {"status": "healthy", "mode": "validated_component"}},
            "degraded_sources": [],
            "unknown_sources": [],
            "warning_count": 0,
            "warnings": [],
            "warning_summary": {},
        }
    return {
        "release_fingerprint": "a" * 64,
        "producer": {"commit": "b" * 40},
        "source_health": {"components": components},
    }


class SelectiveReleaseTests(unittest.TestCase):
    def test_datacite_and_publication_changes_do_not_change_downstream_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = root / "before.sqlite"
            after = root / "after.sqlite"
            build_snapshot(before, citation_count=1)
            build_snapshot(after, citation_count=99)
            with sqlite3.connect(after) as conn:
                conn.execute("UPDATE tcia_publications SET title='new verified use'")
            self.assertEqual(
                SELECTIVE.downstream_snapshot_fingerprint(before),
                SELECTIVE.downstream_snapshot_fingerprint(after),
            )

    def test_core_snapshot_change_forces_a_distinct_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = root / "before.sqlite"
            after = root / "after.sqlite"
            build_snapshot(before, core_value="before")
            build_snapshot(after, core_value="after")
            self.assertNotEqual(
                SELECTIVE.downstream_snapshot_fingerprint(before),
                SELECTIVE.downstream_snapshot_fingerprint(after),
            )

    def test_path_classifier_is_conservative(self) -> None:
        self.assertTrue(SELECTIVE.path_is_fast_safe("scripts/tcia_snapshot.py"))
        self.assertTrue(SELECTIVE.path_is_fast_safe("scripts/tcia_v2_bundle.py"))
        self.assertTrue(SELECTIVE.path_is_fast_safe("mcp_server/tcia_query_mcp/service.py"))
        self.assertTrue(SELECTIVE.path_is_fast_safe("references/publications.md"))
        self.assertFalse(SELECTIVE.path_is_fast_safe("scripts/tcia_participant_inventory.py"))
        self.assertFalse(SELECTIVE.path_is_fast_safe("references/public_non_dicom_crosswalks_v1.csv"))
        self.assertFalse(SELECTIVE.path_is_fast_safe("requirements-build.lock"))

    def test_plan_allows_equal_projection_and_safe_code_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current.sqlite"
            previous = root / "previous.sqlite"
            manifest = root / "manifest.json"
            build_snapshot(current, citation_count=2)
            build_snapshot(previous, citation_count=1)
            manifest.write_text(json.dumps(reusable_manifest()), encoding="utf-8")
            plan = SELECTIVE.build_plan(
                current,
                previous,
                manifest,
                root,
                "c" * 40,
                changed_paths=["scripts/tcia_snapshot.py", "references/publications.md"],
            )
            self.assertEqual(plan["mode"], "fast")
            self.assertTrue(plan["eligible"])

    def test_plan_falls_back_for_expensive_builder_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current.sqlite"
            previous = root / "previous.sqlite"
            manifest = root / "manifest.json"
            build_snapshot(current)
            build_snapshot(previous)
            manifest.write_text(json.dumps(reusable_manifest()), encoding="utf-8")
            plan = SELECTIVE.build_plan(
                current,
                previous,
                manifest,
                root,
                "c" * 40,
                changed_paths=["scripts/tcia_participant_inventory.py"],
            )
            self.assertEqual(plan["mode"], "full")
            self.assertEqual(plan["disqualifying_paths"], ["scripts/tcia_participant_inventory.py"])

    def test_operator_refresh_request_forces_full_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current.sqlite"
            previous = root / "previous.sqlite"
            manifest = root / "manifest.json"
            build_snapshot(current)
            build_snapshot(previous)
            manifest.write_text(json.dumps(reusable_manifest()), encoding="utf-8")
            plan = SELECTIVE.build_plan(
                current,
                previous,
                manifest,
                root,
                "c" * 40,
                changed_paths=[],
                force_full_reasons=["operator requested clinical refresh"],
            )
            self.assertEqual(plan["mode"], "full")
            self.assertIn("operator requested clinical refresh", plan["reasons"])

    def test_verify_rejects_tampered_changed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current.sqlite"
            previous = root / "previous.sqlite"
            manifest = root / "manifest.json"
            plan_path = root / "plan.json"
            build_snapshot(current)
            build_snapshot(previous)
            manifest.write_text(json.dumps(reusable_manifest()), encoding="utf-8")
            plan = SELECTIVE.build_plan(
                current,
                previous,
                manifest,
                root,
                "c" * 40,
                changed_paths=["scripts/tcia_snapshot.py"],
            )
            plan["changed_paths"] = []
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed paths mismatch"):
                SELECTIVE.verify_plan(
                    plan_path,
                    current,
                    manifest,
                    "c" * 40,
                    root,
                    changed_paths=["scripts/tcia_snapshot.py"],
                )


if __name__ == "__main__":
    unittest.main()
