#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_release_review_queue.py"
SPEC = importlib.util.spec_from_file_location("tcia_release_review_queue", SCRIPT)
queue = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = queue
SPEC.loader.exec_module(queue)


class ReleaseReviewQueueTest(unittest.TestCase):
    def create_public(self, path: Path, *, subject_id: str) -> None:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE public_non_dicom_assets ("
                "asset_id TEXT PRIMARY KEY,dataset_type TEXT,short_title TEXT,subject_id TEXT)"
            )
            conn.execute(
                "CREATE TABLE public_non_dicom_asset_participants ("
                "asset_participant_id TEXT PRIMARY KEY,asset_id TEXT,short_title TEXT)"
            )
            conn.execute(
                "INSERT INTO public_non_dicom_assets VALUES ('asset-1','Collection','DEMO',?)",
                (subject_id,),
            )
            conn.execute(
                "INSERT INTO public_non_dicom_asset_participants "
                "VALUES ('link-1','asset-1','DEMO')"
            )

    def create_participant(self, path: Path, *, display_id: str) -> None:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE participants ("
                "participant_key TEXT PRIMARY KEY,dataset_type TEXT,short_title TEXT,"
                "display_participant_id TEXT)"
            )
            conn.execute(
                "CREATE TABLE participant_identifiers ("
                "participant_identifier_id TEXT PRIMARY KEY,participant_key TEXT)"
            )
            conn.execute(
                "INSERT INTO participants VALUES ('participant-1','Collection','DEMO',?)",
                (display_id,),
            )
            conn.execute(
                "INSERT INTO participant_identifiers VALUES ('identifier-1','participant-1')"
            )

    def test_groups_public_and_participant_changes_by_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public_old = root / "public-old.sqlite"
            public_new = root / "public-new.sqlite"
            participant_old = root / "participant-old.sqlite"
            participant_new = root / "participant-new.sqlite"
            self.create_public(public_old, subject_id="demo")
            self.create_public(public_new, subject_id="DEMO")
            self.create_participant(participant_old, display_id="demo")
            self.create_participant(participant_new, display_id="DEMO")
            report = root / "report.json"
            changes = [
                {
                    "artifact": "public_non_dicom",
                    "table": "public_non_dicom_assets",
                    "primary_key": [["asset_id", "asset-1"]],
                    "change_kind": "modified",
                    "before_sha256": "a" * 64,
                    "after_sha256": "b" * 64,
                },
                {
                    "artifact": "participant_inventory",
                    "table": "participant_identifiers",
                    "primary_key": [["participant_identifier_id", "identifier-1"]],
                    "change_kind": "modified",
                    "before_sha256": "c" * 64,
                    "after_sha256": "d" * 64,
                },
            ]
            report.write_text(json.dumps({
                "report_sha256": "e" * 64,
                "unexplained_high_severity_changes": changes,
            }))
            payload = queue.build_queue(argparse.Namespace(
                report=str(report), public_new=str(public_new), public_old=str(public_old),
                participant_new=str(participant_new), participant_old=str(participant_old),
            ))
            self.assertEqual(payload["summary"], {
                "dataset_queue_count": 1,
                "dataset_scoped_change_count": 2,
                "blocking_unscoped_change_count": 0,
            })
            entry = payload["queue_entries"][0]
            self.assertEqual((entry["dataset_type"], entry["short_title"]), ("Collection", "DEMO"))
            self.assertEqual(entry["change_count"], 2)
            self.assertEqual(len(entry["changes_sha256"]), 64)

    def test_unscoped_or_non_dataset_artifact_remains_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / name for name in ("pn", "po", "in", "io")]
            self.create_public(paths[0], subject_id="x")
            self.create_public(paths[1], subject_id="x")
            self.create_participant(paths[2], display_id="x")
            self.create_participant(paths[3], display_id="x")
            change = {
                "artifact": "clinical",
                "table": "clinical_facts",
                "primary_key": [["fact_id", "fact-1"]],
                "change_kind": "added",
                "before_sha256": "",
                "after_sha256": "f" * 64,
            }
            report = root / "report.json"
            report.write_text(json.dumps({
                "report_sha256": "0" * 64,
                "unexplained_high_severity_changes": [change],
            }))
            payload = queue.build_queue(argparse.Namespace(
                report=str(report), public_new=str(paths[0]), public_old=str(paths[1]),
                participant_new=str(paths[2]), participant_old=str(paths[3]),
            ))
            self.assertEqual(payload["queue_entries"], [])
            self.assertEqual(payload["blocking_unscoped_change_count"], 1)
            self.assertEqual(payload["blocking_unscoped_changes"], [change])


if __name__ == "__main__":
    unittest.main()
