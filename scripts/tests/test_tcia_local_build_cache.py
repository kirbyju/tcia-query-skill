#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_local_build_cache.py"
SPEC = importlib.util.spec_from_file_location("tcia_local_build_cache", SCRIPT)
assert SPEC and SPEC.loader
CACHE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CACHE)


class LocalBuildCacheTests(unittest.TestCase):
    def test_restores_only_exact_input_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            output = root / "result.sqlite"
            checkpoints = root / "checkpoints"
            source.write_text('{"generation": 1}\n', encoding="utf-8")
            output.write_bytes(b"validated-output")
            saved = CACHE.save_checkpoint(
                checkpoints,
                "public-non-dicom",
                [source],
                ["release_contract=streamlined"],
                [output],
            )
            self.assertTrue(saved["saved"])
            output.write_bytes(b"damaged-local-output")
            restored = CACHE.restore_checkpoint(
                checkpoints,
                "public-non-dicom",
                [source],
                ["release_contract=streamlined"],
            )
            self.assertTrue(restored["hit"])
            self.assertEqual(output.read_bytes(), b"validated-output")

            source.write_text('{"generation": 2}\n', encoding="utf-8")
            missed = CACHE.restore_checkpoint(
                checkpoints,
                "public-non-dicom",
                [source],
                ["release_contract=streamlined"],
            )
            self.assertFalse(missed["hit"])

    def test_refuses_corrupted_checkpoint_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            output = root / "result.sqlite"
            checkpoints = root / "checkpoints"
            source.write_text("source\n", encoding="utf-8")
            output.write_bytes(b"validated-output")
            saved = CACHE.save_checkpoint(
                checkpoints, "participant", [source], [], [output]
            )
            checkpoint = CACHE.checkpoint_path(
                checkpoints, "participant", saved["fingerprint"]
            )
            stored = checkpoint / "files" / saved["outputs"][0]["stored_name"]
            stored.write_bytes(b"corrupt")
            with self.assertRaisesRegex(RuntimeError, "size changed"):
                CACHE.restore_checkpoint(
                    checkpoints, "participant", [source], []
                )


if __name__ == "__main__":
    unittest.main()
