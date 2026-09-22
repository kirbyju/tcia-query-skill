import csv
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "evals" / "prompts.csv"
RUBRIC = ROOT / "evals" / "answer-rubric.schema.json"


class SkillEvalContractTests(unittest.TestCase):
    def _prompt_rows(self):
        with PROMPTS.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_prompt_set_has_unique_positive_and_negative_cases(self):
        rows = self._prompt_rows()

        self.assertGreaterEqual(len(rows), 10)
        self.assertEqual(
            set(rows[0]),
            {"id", "should_trigger", "prompt", "required_behaviors", "forbidden_behaviors"},
        )
        ids = [row["id"] for row in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("true", {row["should_trigger"] for row in rows})
        self.assertIn("false", {row["should_trigger"] for row in rows})
        for row in rows:
            self.assertIn(row["should_trigger"], {"true", "false"})
            self.assertTrue(row["prompt"].strip())
            self.assertTrue(row["required_behaviors"].strip())
            self.assertTrue(row["forbidden_behaviors"].strip())

    def test_freshness_cases_separate_skill_checks_from_artifact_updates(self):
        rows = {row["id"]: row for row in self._prompt_rows()}
        self.assertEqual(rows["skill-guidance-freshness"]["should_trigger"], "true")
        self.assertIn(
            "Do not install, refresh, or download metadata artifacts",
            rows["skill-guidance-freshness"]["forbidden_behaviors"],
        )
        self.assertIn(
            "operator concern",
            rows["remote-service-lag"]["required_behaviors"],
        )
        self.assertIn(
            "install the smallest adequate manifest-pinned profile",
            rows["explicit-local-artifacts"]["required_behaviors"],
        )

    def test_rubric_schema_has_stable_scoring_fields(self):
        schema = json.loads(RUBRIC.read_text(encoding="utf-8"))
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), {"overall_pass", "score", "checks"})
        check_ids = schema["properties"]["checks"]["items"]["properties"]["id"]["enum"]
        self.assertEqual(len(check_ids), len(set(check_ids)))
        self.assertIn("publication-authority", check_ids)
        self.assertIn("access-and-authorization", check_ids)
        self.assertIn("freshness-state", check_ids)


if __name__ == "__main__":
    unittest.main()
