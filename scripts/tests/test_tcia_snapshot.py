import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_snapshot.py"
SPEC = importlib.util.spec_from_file_location("tcia_snapshot", SCRIPT)
SNAPSHOT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SNAPSHOT)


class SnapshotNormalizationTests(unittest.TestCase):
    def test_pathdb_whole_slide_modality_is_canonical(self):
        self.assertEqual(
            SNAPSHOT.canonical_pathdb_modality("Whole slide image"),
            "Whole Slide Image",
        )
        self.assertEqual(
            SNAPSHOT.canonical_pathdb_modality("  Whole   Slide Image "),
            "Whole Slide Image",
        )

    def test_pathdb_other_modality_is_not_reinterpreted(self):
        self.assertEqual(SNAPSHOT.canonical_pathdb_modality("SM"), "SM")


if __name__ == "__main__":
    unittest.main()
