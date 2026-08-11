import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "tcia_skill_version.py"
SPEC = importlib.util.spec_from_file_location("tcia_skill_version", SCRIPT)
VERSIONING = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(VERSIONING)
sys.modules["tcia_skill_version"] = VERSIONING

FRESHNESS_SCRIPT = Path(__file__).resolve().parents[1] / "tcia_freshness.py"
FRESHNESS_SPEC = importlib.util.spec_from_file_location("tcia_freshness", FRESHNESS_SCRIPT)
FRESHNESS = importlib.util.module_from_spec(FRESHNESS_SPEC)
assert FRESHNESS_SPEC.loader is not None
FRESHNESS_SPEC.loader.exec_module(FRESHNESS)


class SkillVersionManifestTests(unittest.TestCase):
    def test_manifest_detects_changed_operational_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "SKILL.md").write_text("current\n", encoding="utf-8")
            (root / "README.md").write_text("docs\n", encoding="utf-8")
            (root / "agents").mkdir()
            (root / "agents" / "openai.yaml").write_text("name: TCIA\n", encoding="utf-8")

            manifest = VERSIONING.build_manifest("2026.08.10.1", root)
            self.assertTrue(VERSIONING.validate_manifest(manifest, root)["ok"])

            (root / "SKILL.md").write_text("changed\n", encoding="utf-8")
            result = VERSIONING.validate_manifest(manifest, root)
            self.assertFalse(result["ok"])
            self.assertEqual(result["changed"], ["SKILL.md"])

    def test_manifest_detects_untracked_operational_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "SKILL.md").write_text("current\n", encoding="utf-8")
            manifest = VERSIONING.build_manifest("2026.08.10.1", root)
            (root / "references").mkdir()
            (root / "references" / "new.md").write_text("new\n", encoding="utf-8")

            result = VERSIONING.validate_manifest(manifest, root)
            self.assertFalse(result["ok"])
            self.assertEqual(result["untracked"], ["references/new.md"])

    def test_freshness_requires_remote_and_local_manifests_to_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "SKILL.md").write_text("current\n", encoding="utf-8")
            manifest = VERSIONING.build_manifest("2026.08.10.1", root)
            manifest_path = root / "skill_version.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with (
                mock.patch.object(FRESHNESS, "DEFAULT_MANIFEST", manifest_path),
                mock.patch.object(FRESHNESS, "SKILL_ROOT", root),
                mock.patch.object(FRESHNESS, "remote_skill_manifest", return_value=manifest),
            ):
                result = FRESHNESS.check_skill("owner/repo", "main")
            self.assertTrue(result["current"])
            self.assertEqual(result["status"], "current")

            remote = dict(manifest)
            remote["skill_version"] = "2026.08.11.1"
            with (
                mock.patch.object(FRESHNESS, "DEFAULT_MANIFEST", manifest_path),
                mock.patch.object(FRESHNESS, "SKILL_ROOT", root),
                mock.patch.object(FRESHNESS, "remote_skill_manifest", return_value=remote),
            ):
                result = FRESHNESS.check_skill("owner/repo", "main")
            self.assertFalse(result["current"])
            self.assertEqual(result["status"], "update_required")


if __name__ == "__main__":
    unittest.main()
