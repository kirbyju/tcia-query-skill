import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build-metadata-v2-preview.yml"


class ReleaseWorkflowContractTests(unittest.TestCase):
    def test_ci_and_operator_runtime_use_same_exact_hash_locked_versions(self) -> None:
        runtime = (ROOT / "mcp_server" / "requirements.txt").read_text(encoding="utf-8")
        requirements = [
            line.strip()
            for line in runtime.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(requirements)
        self.assertTrue(all("==" in requirement for requirement in requirements))
        self.assertEqual(
            [
                line.strip()
                for line in (ROOT / "requirements-ci.txt").read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ],
            ["--require-hashes", "-r requirements-server.lock"],
        )
        lock = (ROOT / "requirements-server.lock").read_text(encoding="utf-8")
        for requirement in requirements:
            name, version = requirement.split("==", 1)
            locked_name = name.split("[", 1)[0]
            self.assertRegex(lock, rf"(?m)^{re.escape(locked_name)}=={re.escape(version)}(?: |$)")
        self.assertIn("--hash=sha256:", lock)

    def test_producer_workflows_install_hash_locked_build_dependencies(self) -> None:
        for relative in (
            ".github/workflows/build-metadata-v2-preview.yml",
            ".github/workflows/update-snapshot.yml",
        ):
            workflow = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(
                "python -m pip install --require-hashes --requirement requirements-build.lock",
                workflow,
            )
            self.assertNotIn("pip install --requirement requirements-build.txt", workflow)
        build_lock = (ROOT / "requirements-build.lock").read_text(encoding="utf-8")
        self.assertIn("--hash=sha256:", build_lock)

    def test_workflow_run_uses_one_triggering_producer_sha_everywhere(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "PRODUCER_SHA: ${{ github.event.workflow_run.head_sha }}", workflow
        )
        self.assertIn('test "$(git rev-parse HEAD)" = "$PRODUCER_SHA"', workflow)
        self.assertIn('--producer-commit "$PRODUCER_SHA"', workflow)
        self.assertGreaterEqual(workflow.count('--target "$PRODUCER_SHA"'), 2)
        self.assertIn('-f sha="$PRODUCER_SHA"', workflow)
        self.assertNotIn("$GITHUB_SHA", workflow)

    def test_immutable_and_moving_tags_are_resolved_and_verified(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertGreaterEqual(workflow.count("resolve_tag_sha()"), 2)
        self.assertGreaterEqual(
            workflow.count('test "$(resolve_tag_sha "$tag")" = "$PRODUCER_SHA"'),
            2,
        )
        self.assertRegex(
            workflow,
            re.compile(
                r"gh release create \"\$tag\" \"\$publish_dir\"/\*.*?"
                r"--target \"\$PRODUCER_SHA\"",
                re.DOTALL,
            ),
        )

    def test_moving_alias_has_validated_backup_and_failure_restoration(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        required = (
            'backup_dir="previous-v2-alias"',
            'gh release download "$tag" --dir "$backup_dir/assets"',
            '--manifest "$backup_dir/assets/tcia_metadata_v2_bundle_manifest.json"',
            "restore_alias()",
            "trap on_alias_error ERR",
            'sha="$old_tag_sha"',
            "V2 alias restoration failed",
            "trap - ERR",
        )
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, workflow)
        self.assertLess(
            workflow.index("trap on_alias_error ERR"),
            workflow.index('gh release upload "$tag" "${payload_assets[@]}" --clobber'),
        )


if __name__ == "__main__":
    unittest.main()
