import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    pd = None


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


projection = load("tcia_idc_participant_projection")


class IDCParticipantProjectionTests(unittest.TestCase):
    @unittest.skipIf(pd is None, "pandas is installed with the idc-index build dependency")
    def test_projects_collection_and_analysis_result_memberships(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.sqlite"
            output = root / "idc.sqlite"
            with sqlite3.connect(snapshot) as conn:
                conn.execute(
                    "CREATE TABLE agent_datasets "
                    "(dataset_type TEXT, short_title TEXT, hidden INTEGER)"
                )
                conn.executemany(
                    "INSERT INTO agent_datasets VALUES (?,?,?)",
                    [
                        ("Collection", "Source-Collection", 0),
                        ("Analysis Result", "Tumor-Annotations", 0),
                        ("Analysis Result", "Hidden-Result", 1),
                    ],
                )
            frame = pd.DataFrame(
                [
                    {
                        "collection_id": "source_collection",
                        "analysis_result_id": "tumor_annotations",
                        "PatientID": "CASE-001",
                        "StudyInstanceUID": "study-1",
                        "SeriesInstanceUID": "series-1",
                        "Modality": "SEG",
                        "source_DOI": "10.example/idc",
                    },
                    {
                        "collection_id": "source_collection",
                        "analysis_result_id": "tumor_annotations",
                        "PatientID": "CASE-001",
                        "StudyInstanceUID": "study-1",
                        "SeriesInstanceUID": "series-2",
                        "Modality": "CT",
                        "source_DOI": "10.example/idc",
                    },
                    {
                        "collection_id": "source_collection",
                        "analysis_result_id": "hidden_result",
                        "PatientID": "CASE-002",
                        "StudyInstanceUID": "study-2",
                        "SeriesInstanceUID": "series-3",
                        "Modality": "SEG",
                        "source_DOI": "",
                    },
                ]
            )
            geometry = pd.DataFrame(
                [
                    {
                        "SeriesInstanceUID": "series-1",
                        "regularly_spaced_3d_volume": False,
                    },
                    {
                        "SeriesInstanceUID": "series-2",
                        "regularly_spaced_3d_volume": True,
                    },
                ]
            )
            result = projection.build_database(
                output,
                snapshot_db=snapshot,
                replace=True,
                index_frame=frame,
                geometry_frame=geometry,
                source_version="v-test",
            )
            self.assertEqual(
                result["counts"]["analysis_result_participant_memberships"], 1
            )
            validation = projection.validate_database(output)
            self.assertTrue(validation["ok"], validation["errors"])
            regression = projection.validate_database(
                output, minimum_analysis_result_memberships=2
            )
            self.assertFalse(regression["ok"])
            self.assertIn("analysis_result_participants coverage regression", regression["errors"][0])
            with sqlite3.connect(output) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT participant_id, study_count, series_count, modalities, "
                        "source_analysis_result_ids_json "
                        "FROM idc_dataset_participants "
                        "WHERE dataset_type='Analysis Result'"
                    ).fetchone(),
                    (
                        "CASE-001",
                        1,
                        2,
                        "CT;SEG",
                        '["tumor_annotations"]',
                    ),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT geometry_statuses, geometry_eligible_series_count, "
                        "geometry_checked_series_count, "
                        "regularly_spaced_volume_series_count, "
                        "non_regular_volume_series_count, "
                        "geometry_not_checked_series_count "
                        "FROM idc_dataset_participants "
                        "WHERE dataset_type='Analysis Result'"
                    ).fetchone(),
                    ("checked_not_regular;checked_regular", 2, 2, 1, 1, 0),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT source_dataset_id, reason FROM idc_unmatched_datasets "
                        "WHERE dataset_type='Analysis Result'"
                    ).fetchone(),
                    ("hidden_result", "not_visible_in_tcia_wordpress_snapshot"),
                )


if __name__ == "__main__":
    unittest.main()
