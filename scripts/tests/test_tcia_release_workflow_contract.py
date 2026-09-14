import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build-metadata-v2-preview.yml"
SOURCE_WORKFLOW = ROOT / ".github" / "workflows" / "update-snapshot.yml"


class ReleaseWorkflowContractTests(unittest.TestCase):
    def test_correction_registry_is_verified_built_and_strictly_consumed(self) -> None:
        source = SOURCE_WORKFLOW.read_text(encoding="utf-8")
        downstream = WORKFLOW.read_text(encoding="utf-8")
        for marker in (
            "validate-selection",
            "previous/immutable-release.json",
            "tcia_correction_registry.py link-release",
            "--snapshot dist/tcia_snapshot.sqlite",
            "--snapshot-manifest dist/tcia_snapshot_manifest.json",
            "--gzip-out dist/tcia_correction_registry.sqlite.gz",
            "--manifest-out dist/tcia_correction_registry_manifest.json",
            "--explanations-db dist/tcia_correction_registry.sqlite",
            "--fail-on-unexplained-high",
        ):
            self.assertIn(marker, source)
        self.assertIn("--explanations-db cache/tcia_correction_registry.sqlite", downstream)
        self.assertIn("--fail-on-unexplained-high", downstream)

    def test_source_failure_uploads_nonrelease_diagnostics_without_release_input(self) -> None:
        source = SOURCE_WORKFLOW.read_text(encoding="utf-8")
        diagnostic_step = source.index("Upload non-release semantic-gate diagnostics")
        source_upload = source.index("Upload validated V2 source inputs")
        self.assertLess(diagnostic_step, source_upload)
        self.assertIn("Initialize diagnostics-only status", source)
        self.assertIn("Finalize diagnostics-only status", source)
        self.assertIn("if: ${{ always() }}", source)
        self.assertIn(
            "tcia-metadata-v2-diagnostics-${{ github.run_id }}-${{ github.run_attempt }}",
            source,
        )
        self.assertIn("dist/metadata_change_report.json", source)
        self.assertIn("dist/metadata_change_report.md", source)
        self.assertIn("dist/metadata_diagnostic_status.json", source)
        self.assertIn("if-no-files-found: error", source)
        self.assertIn("retention-days: 30", source)
        diagnostic_block = source[diagnostic_step:source_upload]
        self.assertNotIn("GH_TOKEN", diagnostic_block)
        self.assertNotIn(".sqlite.gz", diagnostic_block)
        self.assertNotIn("dist/*_manifest.json", diagnostic_block)
        self.assertNotIn("requirements-build.lock", diagnostic_block)
        self.assertNotIn("requirements-server.lock", diagnostic_block)
        self.assertNotIn("tcia-metadata-v2-source", diagnostic_block)
        validated_block = source[source_upload:]
        self.assertNotIn("tcia-metadata-v2-diagnostics", validated_block)
        self.assertNotIn("metadata_diagnostic_status.json", validated_block)

    def test_prior_release_api_failure_cannot_establish_correction_baseline(self) -> None:
        source = SOURCE_WORKFLOW.read_text(encoding="utf-8")
        marker = "Import only a verified prior correction registry"
        block = source[source.index(marker):source.index("Build and package the correction registry")]
        self.assertIn("if ! gh api", block)
        self.assertIn("Prior V2 release retrieval failed", block)
        self.assertIn("exit 1", block)
        self.assertNotIn("No prior V2 release exists", block)

    def test_source_first_registry_bootstrap_is_explicit_and_narrow(self) -> None:
        source = SOURCE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("record-initial-bootstrap", source)
        self.assertIn("--prior-manifest previous/tcia_metadata_v2_bundle_manifest.json", source)
        self.assertIn("--prior-release-json previous/release.json", source)
        self.assertIn("tcia_correction_registry_bootstrap_evidence.json", source)
        self.assertRegex(
            source,
            r"if \[ -s previous/tcia_metadata_v2_bundle_manifest\.json \] && \\\n+\s+! jq -e '.assets\[\"tcia_correction_registry\.sqlite\.gz\"\]'",
        )

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

    def test_deployment_lag_report_skips_when_assembly_never_produced_manifest(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        marker = "Report published versus deployed fingerprint lag"
        block = workflow[workflow.index(marker):]
        guard = 'if [ ! -s "$manifest_path" ]; then'
        manifest_read = "json.load(open(sys.argv[1]))"
        self.assertIn('manifest_path="release-dist/tcia_metadata_v2_bundle_manifest.json"', block)
        self.assertIn(guard, block)
        self.assertIn("no release manifest was assembled", block)
        self.assertIn(manifest_read, block)
        self.assertLess(block.index(guard), block.index(manifest_read))

    def test_staging_cross_component_checks_use_validated_bundle_generation(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        staging_step = workflow.index("Build the runner-local V2 staging ledger")
        next_step = workflow.index(
            "Regenerate the complete web export set", staging_step
        )
        block = workflow[staging_step:next_step]
        self.assertIn(
            "--baseline-bundle-manifest dist/tcia_metadata_v2_bundle_manifest.json",
            block,
        )

    def test_semantic_baselines_survive_until_exact_change_gate(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        staging_step = workflow.index("Build the runner-local V2 staging ledger")
        semantic_step = workflow.index("Enforce exact semantic change explanations")
        contract_tests = workflow.index("Run V2 contract tests", semantic_step)
        staging_block = workflow[staging_step:semantic_step]
        semantic_block = workflow[semantic_step:contract_tests]

        for baseline in (
            "cache/public_non_dicom_baseline.sqlite",
            "cache/participant_inventory_baseline.sqlite",
        ):
            with self.subTest(baseline=baseline):
                self.assertIn(baseline, semantic_block)
                self.assertNotIn(f"rm -f {baseline}", staging_block)
        self.assertLess(
            semantic_block.index("--fail-on-unexplained-high"),
            semantic_block.index("rm -f cache/public_non_dicom_baseline.sqlite"),
        )

    def test_geometry_refresh_is_atomic_and_strictly_scoped_at_gate(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        build_step = workflow.index("Build public non-DICOM metadata")
        semantic_step = workflow.index("Enforce exact semantic change explanations")
        block = workflow[build_step:semantic_step]
        build_command = block[:block.index("tcia_geometry_batch.py plan")]
        self.assertNotIn("--reset-geometry", build_command)
        self.assertIn("tcia_public_non_dicom_metadata.py import-geometry", block)
        semantic_block = workflow[semantic_step:]
        self.assertIn(
            "--geometry-refresh-report cache/reports/public_non_dicom_geometry_refresh.json",
            semantic_block,
        )


if __name__ == "__main__":
    unittest.main()
