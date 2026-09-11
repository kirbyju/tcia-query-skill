from __future__ import annotations

import importlib.util
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
spec = importlib.util.spec_from_file_location(
    "tcia_correction_registry", SCRIPTS / "tcia_correction_registry.py"
)
registry = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(registry)


class CorrectionIdentityTests(unittest.TestCase):
    def test_release_package_is_deterministic_hash_pinned_and_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "registry.sqlite"
            first_gzip = root / "first.sqlite.gz"
            second_gzip = root / "second.sqlite.gz"
            first_manifest = root / "first.json"
            second_manifest = root / "second.json"
            registry.build_registry(db, observed_at="2026-09-11T12:00:00Z")
            first = registry.package_registry(
                db, gzip_out=first_gzip, manifest_out=first_manifest
            )
            second = registry.package_registry(
                db, gzip_out=second_gzip, manifest_out=second_manifest
            )
            self.assertEqual(first_gzip.read_bytes(), second_gzip.read_bytes())
            self.assertEqual(first["gzip_sha256"], registry.file_sha256(first_gzip))
            self.assertEqual(first["sqlite_sha256"], registry.file_sha256(db))
            self.assertEqual(first["decision_set_summary"], second["decision_set_summary"])
            self.assertEqual(first["storage_contract"]["profile"], "audit_support")
            unpacked = root / "unpacked.sqlite"
            unpacked.write_bytes(gzip.decompress(first_gzip.read_bytes()))
            self.assertTrue(registry.validate_database(unpacked)["ok"])

    def test_stable_promotion_waivers_are_exact_current_and_single_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "registry.sqlite"
            registry.build_registry(db, observed_at="2026-09-11T12:00:00Z")
            with sqlite3.connect(db) as conn:
                validation_id = "release-validation"
                conn.execute(
                    "INSERT INTO correction_validations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (validation_id, None, "row-change", "v1", "high", "failed",
                     "{}", "{}", "a" * 64, "2026-09-11T11:00:00Z", "", "test"),
                )
                conn.commit()
            blocked = registry.validate_promotion_waivers(
                db, at="2026-09-11T12:00:00Z"
            )
            self.assertFalse(blocked["ok"])

            waiver = registry.add_waiver(
                db, rule_id="row-change", owner="release-owner", reason="reviewed",
                scope={"validation_id": validation_id, "evidence_sha256": "a" * 64},
                created_at="2026-09-11T11:30:00Z",
                expires_at="2026-09-12T11:30:00Z",
            )
            allowed = registry.validate_promotion_waivers(
                db, at="2026-09-11T12:00:00Z"
            )
            self.assertTrue(allowed["ok"], allowed["errors"])
            self.assertEqual(allowed["used_waiver_ids"], [waiver["waiver_id"]])

            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE correction_waivers SET status='revoked' WHERE waiver_id=?",
                    (waiver["waiver_id"],),
                )
                conn.commit()
            revoked = registry.validate_promotion_waivers(
                db, at="2026-09-11T12:00:00Z"
            )
            self.assertFalse(revoked["ok"])

            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE correction_waivers SET status='active',expires_at='2026-09-11T11:45:00Z' WHERE waiver_id=?",
                    (waiver["waiver_id"],),
                )
                conn.commit()
            expired = registry.validate_promotion_waivers(
                db, at="2026-09-11T12:00:00Z"
            )
            self.assertFalse(expired["ok"])
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE correction_waivers SET expires_at=?,scope_json='{}' WHERE waiver_id=?",
                    ("2026-09-12T11:30:00Z", waiver["waiver_id"]),
                )
                conn.commit()
            malformed = registry.validate_promotion_waivers(
                db, at="2026-09-11T12:00:00Z"
            )
            self.assertFalse(malformed["ok"])
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE correction_waivers SET scope_json=? WHERE waiver_id=?",
                    (json.dumps({"validation_id": "wrong", "evidence_sha256": "a" * 64}),
                     waiver["waiver_id"]),
                )
                conn.commit()
            unused = registry.validate_promotion_waivers(
                db, at="2026-09-11T12:00:00Z"
            )
            self.assertFalse(unused["ok"])

    def test_promotion_waiver_chronology_and_scope_matrix(self) -> None:
        instant = "2026-09-11T12:00:00Z"
        exact_scope = {
            "validation_id": "release-validation",
            "evidence_sha256": "a" * 64,
        }

        def evaluate(
            *,
            created_at: str = "2026-09-11T11:30:00Z",
            expires_at: str = "2026-09-12T11:30:00Z",
            status: str = "active",
            scope: dict[str, str] | None = None,
            duplicate: bool = False,
        ) -> dict[str, object]:
            with tempfile.TemporaryDirectory() as directory:
                db = Path(directory) / "registry.sqlite"
                registry.build_registry(db, observed_at=instant)
                with sqlite3.connect(db) as conn:
                    conn.execute(
                        "INSERT INTO correction_validations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            "release-validation", None, "row-change", "v1", "high",
                            "failed", "{}", "{}", "a" * 64,
                            "2026-09-11T11:00:00Z", "", "test",
                        ),
                    )
                    conn.commit()
                waiver = registry.add_waiver(
                    db,
                    rule_id="row-change",
                    owner="release-owner",
                    reason="reviewed",
                    scope=exact_scope,
                    created_at="2026-09-11T11:30:00Z",
                    expires_at="2026-09-12T11:30:00Z",
                )
                with sqlite3.connect(db) as conn:
                    conn.execute(
                        """UPDATE correction_waivers
                           SET created_at=?,expires_at=?,status=?,scope_json=?
                           WHERE waiver_id=?""",
                        (
                            created_at,
                            expires_at,
                            status,
                            json.dumps(exact_scope if scope is None else scope),
                            waiver["waiver_id"],
                        ),
                    )
                    conn.commit()
                if duplicate:
                    registry.add_waiver(
                        db,
                        rule_id="row-change",
                        owner="second-release-owner",
                        reason="independent duplicate",
                        scope=exact_scope,
                        created_at="2026-09-11T11:45:00Z",
                        expires_at="2026-09-12T11:45:00Z",
                    )
                return registry.validate_promotion_waivers(db, at=instant)

        valid_cases = {
            "already active": {},
            "created exactly at validation instant": {"created_at": instant},
        }
        for label, kwargs in valid_cases.items():
            with self.subTest(label=label):
                result = evaluate(**kwargs)
                self.assertTrue(result["ok"], result["errors"])
                self.assertEqual(len(result["used_waiver_ids"]), 1)

        invalid_cases = {
            "future created": {
                "created_at": "2026-09-11T12:00:00.000001Z",
                "expires_at": "2026-09-12T12:00:00Z",
                "error": "not active yet",
            },
            "expiry equals instant": {"expires_at": instant, "error": "expired"},
            "past expiry": {
                "expires_at": "2026-09-11T11:59:59Z",
                "error": "expired",
            },
            "reversed chronology": {
                "created_at": "2026-09-11T11:30:00Z",
                "expires_at": "2026-09-11T11:00:00Z",
                "error": "expiry must be after creation",
            },
            "malformed creation": {"created_at": "not-a-time", "error": "malformed"},
            "malformed expiry": {"expires_at": "not-a-time", "error": "malformed"},
            "missing expiry": {"expires_at": "", "error": "malformed"},
            "revoked": {"status": "revoked", "error": "no active exact waiver"},
            "empty scope": {"scope": {}, "error": "malformed"},
            "wrong validation scope": {
                "scope": {"validation_id": "wrong", "evidence_sha256": "a" * 64},
                "error": "unused",
            },
            "wrong evidence scope": {
                "scope": {"validation_id": "release-validation", "evidence_sha256": "b" * 64},
                "error": "unused",
            },
            "duplicate exact scope": {"duplicate": True, "error": "duplicate"},
        }
        for label, settings in invalid_cases.items():
            with self.subTest(label=label):
                kwargs = dict(settings)
                expected_error = str(kwargs.pop("error"))
                result = evaluate(**kwargs)
                self.assertFalse(result["ok"])
                self.assertTrue(
                    any(expected_error in error for error in result["errors"]),
                    result["errors"],
                )

    def test_declarative_semantic_explanation_stays_consumed_across_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "registry.sqlite"
            declarations = root / "explanations.json"
            declarations.write_text(json.dumps({
                "schema_version": 1,
                "explanations": [{
                    "artifact": "public_non_dicom",
                    "entity_table": "public_non_dicom_assets",
                    "primary_key": [["asset_id", "asset-1"]],
                    "change_kind": "modified",
                    "before_sha256": "a" * 64,
                    "after_sha256": "b" * 64,
                    "reviewer": "release-reviewer",
                    "approved_at": "2026-09-11T11:00:00Z",
                    "rationale": "Reviewed exact before and after rows.",
                }],
            }))
            registry.build_registry(
                db, observed_at="2026-09-11T12:00:00Z",
                semantic_explanations=declarations,
            )
            with sqlite3.connect(db) as conn:
                effect_id = conn.execute(
                    "SELECT effect_id FROM correction_effects WHERE effect_status='approved'"
                ).fetchone()[0]
            report = root / "report.json"
            report.write_text(json.dumps({
                "report_sha256": "c" * 64,
                "unexplained_high_severity": [],
                "semantic_explanations": {
                    "consumed_effect_ids": [effect_id],
                    "duplicate_matches": [], "malformed_effects": [],
                    "unused_effect_ids": [],
                },
            }))
            consumed = registry.consume_semantic_explanations(db, report)
            self.assertEqual(consumed["consumed"], 1)
            registry.build_registry(
                db, observed_at="2026-09-11T13:00:00Z", replace=True,
                semantic_explanations=declarations,
            )
            with sqlite3.connect(db) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT effect_status,build_fingerprint FROM correction_effects WHERE effect_id=?",
                        (effect_id,),
                    ).fetchone(),
                    ("consumed", "c" * 64),
                )

    def base_record(self) -> dict[str, object]:
        scope = {
            "dataset_type": "Collection",
            "short_title": "TEST",
            "decision_type": "clinical_normalization",
            "target": "field:value",
        }
        record: dict[str, object] = {
            "case_id": registry.stable_id("case", scope),
            "status": "approved",
            "reviewer": "reviewer",
            "approver": "",
            "reviewed_at": "2026-09-11T00:00:00Z",
            "approved_at": "2026-09-11T00:00:00Z",
            "rationale": "Reviewed exact source mapping.",
            "resolution": {"value": "A"},
            "scope": scope,
            "negative_scope": {"must_not": ["overwrite_raw"]},
            "expected_effects": [{"field": "value_resolved"}],
            "evidence_observation_ids": [registry.stable_id("observation", "source")],
            "supersedes_revision_id": None,
            "stale_status": "current",
            "source_kind": "clinical_normalization",
            "policy_version": "v1",
        }
        record["decision_id"] = registry.decision_identity(
            str(record["source_kind"]), scope
        )
        record["revision_id"] = registry.revision_identity(record)
        return record

    def test_revision_changes_for_every_reviewed_semantic_dimension(self) -> None:
        base = self.base_record()
        original = base["revision_id"]
        mutations = {
            "status": "rejected",
            "reviewer": "second-reviewer",
            "rationale": "Different rationale.",
            "resolution": {"value": "B"},
            "scope": {**base["scope"], "target": "other"},
            "negative_scope": {"must_not": ["apply_elsewhere"]},
            "expected_effects": [{"field": "other"}],
            "evidence_observation_ids": [registry.stable_id("observation", "other")],
            "stale_status": "stale_source_changed",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = dict(base)
                changed[field] = value
                self.assertNotEqual(registry.revision_identity(changed), original)

    def test_decision_id_is_logical_while_scope_changes_identity(self) -> None:
        record = self.base_record()
        changed = dict(record)
        changed["rationale"] = "New rationale"
        self.assertEqual(
            registry.decision_identity(str(record["source_kind"]), record["scope"]),
            registry.decision_identity(str(changed["source_kind"]), changed["scope"]),
        )
        changed_scope = {**record["scope"], "target": "different"}
        self.assertNotEqual(
            registry.decision_identity(str(record["source_kind"]), record["scope"]),
            registry.decision_identity(str(record["source_kind"]), changed_scope),
        )

    def test_schema_rejects_invalid_or_tampered_record(self) -> None:
        record = self.base_record()
        self.assertEqual(registry.validate_decision_record(record), [])
        record["resolution"] = {"value": "tampered"}
        self.assertIn(
            "revision_id does not match revision content",
            registry.validate_decision_record(record),
        )


class RegistryBuildTests(unittest.TestCase):
    def test_staged_refresh_preserves_history_and_failed_build_preserves_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "registry.sqlite"
            registry.build_registry(out, observed_at="2026-09-11T12:00:00Z")
            registry.link_release(
                out, release_fingerprint="f" * 64, release_tag="release-1",
                source_health="verified_current", observed_at="2026-09-11T12:10:00Z",
                change_report_sha256="a" * 64,
            )
            waiver = registry.add_waiver(
                out, rule_id="bounded", owner="curator", reason="test",
                scope={"dataset": "TEST"}, created_at="2026-09-11T12:15:00Z",
                expires_at="2026-09-12T12:15:00Z",
            )
            with sqlite3.connect(out) as conn:
                original_revision = conn.execute(
                    "SELECT revision_id FROM agent_active_corrections ORDER BY revision_id LIMIT 1"
                ).fetchone()[0]
            stale = registry.mark_revision_stale(
                out, original_revision, stale_status="stale_source_changed",
                executed_at="2026-09-11T12:20:00Z",
            )
            self.assertTrue(stale["ok"])
            with sqlite3.connect(out) as conn:
                before = {
                    table: {row[0] for row in conn.execute(f"SELECT {column} FROM {table}")}
                    for table, column in (
                        ("correction_observations", "observation_id"),
                        ("correction_decisions", "revision_id"),
                        ("correction_effects", "effect_id"),
                        ("correction_validations", "validation_id"),
                        ("correction_releases", "release_fingerprint"),
                        ("correction_release_revisions", "release_fingerprint || ':' || revision_id"),
                        ("correction_waivers", "waiver_id"),
                    )
                }
                pointers = dict(conn.execute(
                    "SELECT case_id,current_revision_id FROM correction_cases"
                ))
            refreshed = registry.build_registry(
                out, observed_at="2026-09-11T13:00:00Z", replace=True
            )
            self.assertTrue(refreshed["ok"], refreshed["errors"])
            with sqlite3.connect(out) as conn:
                for table, ids in before.items():
                    column = {
                        "correction_observations": "observation_id",
                        "correction_decisions": "revision_id",
                        "correction_effects": "effect_id",
                        "correction_validations": "validation_id",
                        "correction_releases": "release_fingerprint",
                        "correction_release_revisions": "release_fingerprint || ':' || revision_id",
                        "correction_waivers": "waiver_id",
                    }[table]
                    self.assertTrue(ids.issubset({row[0] for row in conn.execute(f"SELECT {column} FROM {table}")}))
                self.assertEqual(
                    {case: revision for case, revision in conn.execute(
                        "SELECT case_id,current_revision_id FROM correction_cases"
                    ) if case in pointers},
                    pointers,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM correction_waivers WHERE waiver_id=?", (waiver["waiver_id"],)).fetchone()[0], 1
                )
            prior_bytes = out.read_bytes()
            with self.assertRaises(FileNotFoundError):
                registry.build_registry(
                    out, clinical_module=root / "missing.py",
                    observed_at="2026-09-11T14:00:00Z", replace=True,
                )
            self.assertEqual(out.read_bytes(), prior_bytes)

    def test_reviewed_revision_supersedes_without_deleting_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.sqlite"
            with registry.create_database(path) as conn:
                kwargs = {
                    "source_kind": "clinical_normalization",
                    "dataset_type": "Collection",
                    "short_title": "TEST",
                    "decision_type": "display_mapping",
                    "target": "raw:A",
                    "status": "approved",
                    "reviewer": "curator",
                    "reviewed_at": "2026-09-10T00:00:00Z",
                    "rationale": "Initial reviewed mapping.",
                    "resolution": {"value": "A"},
                    "expected_effects": [{
                        "artifact": "clinical", "entity_table": "clinical_facts",
                        "field_name": "value_resolved", "effect_kind": "resolve",
                        "after": "A",
                    }],
                    "evidence": [{"source_record_id": "source", "excerpt": "A means A."}],
                }
                first = registry.make_reviewed_decision(conn, **kwargs)
                second = registry.make_reviewed_decision(
                    conn, **{
                        **kwargs,
                        "reviewed_at": "2026-09-11T00:00:00Z",
                        "rationale": "Updated reviewed mapping.",
                        "resolution": {"value": "Alpha"},
                        "evidence": [{"source_record_id": "source-v2", "excerpt": "A means Alpha."}],
                        "supersedes_revision_id": first,
                    }
                )
                conn.commit()
                self.assertNotEqual(first, second)
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM correction_decisions WHERE revision_id=?", (first,)
                    ).fetchone()[0],
                    "approved",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT current_revision_id FROM correction_cases"
                    ).fetchone()[0],
                    second,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT supersedes_revision_id FROM correction_decisions WHERE revision_id=?",
                        (second,),
                    ).fetchone()[0],
                    first,
                )

    def test_wordpress_clues_are_field_level_and_proposal_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.sqlite"
            out = root / "registry.sqlite"
            conn = sqlite3.connect(snapshot)
            conn.executescript(
                """
                CREATE TABLE agent_datasets (
                    dataset_type TEXT, short_title TEXT, id TEXT, link TEXT,
                    date_updated TEXT, hidden INTEGER, summary TEXT, abstract TEXT,
                    detailed_description TEXT, source_collections TEXT, raw_json TEXT
                );
                CREATE TABLE agent_current_downloads (
                    dataset_type TEXT, short_title TEXT, download_id TEXT,
                    download_url TEXT, date_updated TEXT, description TEXT,
                    download_title TEXT, subjects TEXT, studies TEXT, series TEXT,
                    images TEXT, license_label TEXT, requirements_text TEXT,
                    raw_json TEXT
                );
                """
            )
            raw = json.dumps({
                "usage_notes": "Folder name is the Patient ID.",
                "geometry_note": "Volumes contain 3D voxel spacing.",
            }, separators=(",", ":"))
            conn.execute(
                "INSERT INTO agent_datasets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("Analysis Result", "AR", "17", "https://example.test/ar",
                 "2026-09-10", 0, "A subset of SOURCE with 12 patients.", "",
                 "No patient-level mapping is published.", "SOURCE", raw),
            )
            conn.execute(
                "INSERT INTO agent_datasets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("Collection", "HIDDEN", "18", "https://example.test/hidden",
                 "2026-09-10", 1, "Patient ID is in the folder.", "", "", "", raw),
            )
            conn.execute(
                "INSERT INTO agent_current_downloads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("Analysis Result", "AR", "99", "https://example.test/file",
                 "2026-09-10", "Controlled access; 12 image files.",
                 "Patient files", "12", "", "", "12", "Restricted", "Authorization required", raw),
            )
            conn.commit()
            original_raw = conn.execute("SELECT raw_json FROM agent_datasets").fetchone()[0]
            conn.close()

            result = registry.build_registry(
                out, snapshot=snapshot,
                curation=root / "missing-curation.json",
                source_links=root / "missing-links.csv",
                clinical_module=ROOT / "scripts/tcia_clinical_metadata.py",
                observed_at="2026-09-11T12:00:00Z", replace=True,
            )
            self.assertTrue(result["ok"], result["errors"])
            with sqlite3.connect(out) as built:
                clue_classes = {
                    row[0] for row in built.execute(
                        "SELECT DISTINCT clue_class FROM correction_observations WHERE detector='wordpress_field_clue'"
                    )
                }
                self.assertTrue(
                    {"identifier_naming", "source_relationship", "count_statement",
                     "access_constraint", "geometry", "unavailability"}.issubset(clue_classes)
                )
                self.assertEqual(
                    built.execute("SELECT MIN(proposal_only) FROM correction_proposals").fetchone()[0], 1
                )
                self.assertEqual(
                    built.execute(
                        "SELECT COUNT(*) FROM correction_decisions d JOIN correction_observations o "
                        "ON instr(d.evidence_observation_ids_json,o.observation_id)>0 "
                        "WHERE o.detector='wordpress_field_clue' AND d.status='approved'"
                    ).fetchone()[0], 0
                )
                self.assertEqual(
                    built.execute(
                        "SELECT COUNT(*) FROM correction_observations WHERE short_title='HIDDEN'"
                    ).fetchone()[0],
                    0,
                )
            with sqlite3.connect(snapshot) as source:
                self.assertEqual(source.execute("SELECT raw_json FROM agent_datasets").fetchone()[0], original_raw)

    def test_real_migrations_cover_public_source_links_and_clinical_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "registry.sqlite"
            result = registry.build_registry(out, observed_at="2026-09-11T12:00:00Z")
            self.assertTrue(result["ok"], result["errors"])
            with sqlite3.connect(out) as conn:
                kinds = dict(conn.execute(
                    "SELECT source_kind,COUNT(*) FROM correction_decisions GROUP BY source_kind"
                ))
                self.assertGreater(kinds.get("public_non_dicom_crosswalk", 0), 0)
                self.assertGreater(kinds.get("analysis_result_source_link", 0), 0)
                self.assertGreater(kinds.get("clinical_normalization", 0), 100)
                constants = {
                    json.loads(row[0]).get("clinical_policy_constant")
                    for row in conn.execute(
                        "SELECT scope_json FROM correction_decisions WHERE source_kind='clinical_normalization'"
                    )
                }
                self.assertEqual(constants, set(registry.CLINICAL_DECISION_CONSTANTS))
                self.assertGreater(
                    conn.execute("SELECT COUNT(*) FROM agent_active_corrections").fetchone()[0], 100
                )
                self.assertGreater(
                    conn.execute("SELECT COUNT(*) FROM agent_field_resolution_trace").fetchone()[0], 100
                )
                self.assertGreater(
                    conn.execute("SELECT COUNT(*) FROM correction_assertions").fetchone()[0], 0
                )
                migration_counts = json.loads(dict(conn.execute(
                    "SELECT key,value FROM registry_meta"
                ))["migration_counts"])
                self.assertGreater(migration_counts["declarative_assertions"], 0)

    def test_schema_document_is_valid_json_and_requires_review_fields(self) -> None:
        schema = json.loads(
            (ROOT / "references/correction-registry-v1.schema.json").read_text()
        )
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertTrue(
            {"reviewer", "reviewed_at", "negative_scope", "expected_effects",
             "evidence_observation_ids", "stale_status"}.issubset(schema["required"])
        )

    def test_snapshot_source_health_preserves_fallback_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "snapshot-manifest.json"
            manifest.write_text(json.dumps({
                "generated_at_utc": "2026-09-11T12:00:00Z",
                "source_status": {
                    "wordpress_collections": "live",
                    "wordpress_analysis_results": "fallback_snapshot",
                },
                "warnings": [{"source": "wordpress_analysis_results", "rows": 29}],
            }))
            health = registry.snapshot_source_health(manifest)
            self.assertEqual(health["status"], "degraded")
            self.assertEqual(
                health["sources"]["wordpress_analysis_results"], "fallback_snapshot"
            )
            self.assertEqual(len(health["manifest_sha256"]), 64)

    def test_release_link_waiver_and_staleness_are_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "registry.sqlite"
            registry.build_registry(out, observed_at="2026-09-11T12:00:00Z")
            linked = registry.link_release(
                out, release_fingerprint="f" * 64,
                release_tag="test-release", source_health="verified_current",
                observed_at="2026-09-11T12:30:00Z",
                change_report_sha256="a" * 64,
            )
            self.assertTrue(linked["ok"], linked["errors"])
            waiver = registry.add_waiver(
                out, rule_id="temporary-source-health", owner="curator",
                reason="Bounded review exception", scope={"source": "wordpress"},
                created_at="2026-09-11T12:30:00Z",
                expires_at="2026-09-12T12:30:00Z",
            )
            self.assertTrue(waiver["ok"], waiver["errors"])
            with sqlite3.connect(out) as conn:
                revision_id = conn.execute(
                    "SELECT revision_id FROM agent_active_corrections ORDER BY revision_id LIMIT 1"
                ).fetchone()[0]
                health = conn.execute(
                    "SELECT source_health,change_report_sha256 FROM agent_release_evidence_health"
                ).fetchone()
                self.assertEqual(health, ("verified_current", "a" * 64))
            stale = registry.mark_revision_stale(
                out, revision_id, stale_status="stale_source_changed",
                executed_at="2026-09-11T13:00:00Z",
            )
            self.assertTrue(stale["ok"], stale["errors"])
            with sqlite3.connect(out) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT status,stale_status FROM correction_decisions WHERE revision_id=?",
                        (revision_id,),
                    ).fetchone(),
                    ("approved", "current"),
                )
                current = conn.execute(
                    "SELECT current_revision_id FROM correction_cases "
                    "WHERE current_revision_id<>? AND stale_status='stale_source_changed' LIMIT 1",
                    (revision_id,),
                ).fetchone()[0]
                self.assertEqual(
                    conn.execute(
                        "SELECT status,stale_status,supersedes_revision_id "
                        "FROM correction_decisions WHERE revision_id=?",
                        (current,),
                    ).fetchone(),
                    ("stale", "stale_source_changed", revision_id),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM agent_correction_queue WHERE current_revision_id=?",
                        (current,),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT change_kind,current_revision_id "
                        "FROM agent_correction_changes_since_release "
                        "WHERE released_revision_id=?",
                        (revision_id,),
                    ).fetchone(),
                    ("stale", current),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT stale_revision_count FROM agent_release_evidence_health"
                    ).fetchone()[0],
                    1,
                )

    def test_release_identity_is_immutable_and_exact_relink_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "registry.sqlite"
            registry.build_registry(out, observed_at="2026-09-11T12:00:00Z")
            base = dict(
                release_fingerprint="e" * 64, release_tag="release-1",
                source_health="verified_current", change_report_sha256="b" * 64,
            )
            registry.link_release(out, observed_at="2026-09-11T12:30:00Z", **base)
            with sqlite3.connect(out) as conn:
                before = (
                    conn.execute("SELECT * FROM correction_releases").fetchall(),
                    conn.execute("SELECT * FROM correction_release_revisions ORDER BY 1,2").fetchall(),
                )
            registry.link_release(out, observed_at="2026-09-11T13:30:00Z", **base)
            with sqlite3.connect(out) as conn:
                self.assertEqual(conn.execute("SELECT * FROM correction_releases").fetchall(), before[0])
                self.assertEqual(conn.execute("SELECT * FROM correction_release_revisions ORDER BY 1,2").fetchall(), before[1])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_release_evidence_health").fetchone()[0], 1)
            for changed in (
                {"release_tag": "release-2"},
                {"source_health": "degraded"},
                {"change_report_sha256": "c" * 64},
            ):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    registry.link_release(
                        out, observed_at="2026-09-11T14:00:00Z", **{**base, **changed}
                    )
            with sqlite3.connect(out) as conn:
                # A changed active membership for the same fingerprint must fail;
                # use the public helper to append the additional reviewed decision.
                registry.make_reviewed_decision(
                    conn, source_kind="clinical_normalization", dataset_type="Collection",
                    short_title="EXTRA", decision_type="display_mapping", target="raw:X",
                    status="approved", reviewer="curator",
                    reviewed_at="2026-09-11T13:45:00Z", rationale="Reviewed extra mapping.",
                    resolution={"value": "X"},
                    expected_effects=[{
                        "artifact": "clinical", "entity_table": "clinical_facts",
                        "field_name": "value_resolved", "effect_kind": "resolve", "after": "X",
                    }],
                    evidence=[{"source_record_id": "extra-source", "excerpt": "X means X."}],
                )
                conn.commit()
            with self.assertRaises(ValueError):
                registry.link_release(out, observed_at="2026-09-11T14:00:00Z", **base)
            with sqlite3.connect(out) as conn:
                self.assertEqual(conn.execute("SELECT * FROM correction_releases").fetchall(), before[0])
                self.assertEqual(conn.execute("SELECT * FROM correction_release_revisions ORDER BY 1,2").fetchall(), before[1])

    def test_clinical_policy_inventory_independently_covers_production_families(self) -> None:
        expected_families = {
            "concept_code_normalization", "canonical_display_normalization",
            "nlst_morphology_decode", "nlst_topography_decode", "nlst_screening_decode",
            "dataset_value_decode", "source_column_concept_override",
            "source_column_unit_override", "screening_diagnosis_resolution",
            "permanent_screening_review", "subject_column_override",
            "reviewed_cohort_pattern", "hungarian_colorectal_decode",
            "ct_colonography_workbook_decode", "ct_colonography_patient_histology",
            "ea1141_workbook_decode", "ea1141_patient_diagnosis",
            "hnscc_workbook_decode", "hnscc_official_cohort_promotion",
            "official_source_transform_versioning",
        }
        self.assertEqual(set(registry.CLINICAL_POLICY_INVENTORY), expected_families)
        classifications = {value[0] for value in registry.CLINICAL_POLICY_INVENTORY.values()}
        self.assertEqual(classifications, {"registry_covered", "procedural_with_evidence"})
        inventory_constants = {
            constant for _, constants in registry.CLINICAL_POLICY_INVENTORY.values()
            for constant in constants
        }
        self.assertEqual(inventory_constants, set(registry.CLINICAL_DECISION_CONSTANTS))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "registry.sqlite"
            registry.build_registry(out, observed_at="2026-09-11T12:00:00Z")
            with sqlite3.connect(out) as conn:
                for constant, expected_field in (
                    ("CT_COLONOGRAPHY_HISTOLOGY", "lesion_histology/primary_diagnosis"),
                    ("CT_COLONOGRAPHY_NONMALIGNANT_SEVERITY", "primary_diagnosis"),
                    ("EA1141_RACE", "race"), ("EA1141_ETHNICITY", "ethnicity"),
                    ("EA1141_GRADE", "grade"),
                    ("EA1141_HANDLED_COLUMNS", "official_workbook_column_handling"),
                    ("HNSCC_HANDLED_COLUMNS", "official_workbook_column_handling"),
                    ("OFFICIAL_SOURCE_TRANSFORM_VERSIONS", "transform_version"),
                ):
                    rows = conn.execute(
                        """SELECT d.evidence_observation_ids_json,e.field_name
                           FROM correction_decisions d JOIN correction_effects e USING(revision_id)
                           WHERE json_extract(d.scope_json,'$.clinical_policy_constant')=?""",
                        (constant,),
                    ).fetchall()
                    self.assertTrue(rows, constant)
                    self.assertTrue(all(json.loads(row[0]) for row in rows), constant)
                    self.assertTrue(all(row[1] == expected_field for row in rows), constant)
                procedural = conn.execute(
                    """SELECT json_extract(d.scope_json,'$.transformation_family'),
                              d.evidence_observation_ids_json,e.field_name
                       FROM correction_decisions d JOIN correction_effects e USING(revision_id)
                       WHERE d.source_kind='clinical_derivation_policy'
                       ORDER BY 1"""
                ).fetchall()
                self.assertEqual(
                    {row[0] for row in procedural},
                    {"ct_colonography_patient_histology", "ea1141_patient_diagnosis",
                     "hnscc_official_cohort_promotion"},
                )
                self.assertTrue(all(json.loads(row[1]) and row[2] for row in procedural))


if __name__ == "__main__":
    unittest.main()
