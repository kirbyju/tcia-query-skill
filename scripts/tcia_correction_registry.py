#!/usr/bin/env python3
"""Build and validate an additive TCIA derived-metadata correction registry.

The registry never rewrites source rows.  It records immutable observations,
proposal-only machine clues, reviewed decision revisions, expected/observed
effects, validations, release linkage, and explicitly scoped waivers.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
DETECTOR_VERSION = "wordpress-clues-v1"
DEFAULT_CURATION = ROOT / "references/public-non-dicom-crosswalk-curation-v1.json"
DEFAULT_SOURCE_LINKS = ROOT / "references/reviewed_analysis_result_source_collections_v1.csv"
DEFAULT_CLINICAL_MODULE = ROOT / "scripts/tcia_clinical_metadata.py"
DEFAULT_ASSERTIONS = ROOT / "references/correction-assertions-v1.json"
DEFAULT_SEMANTIC_EXPLANATIONS = ROOT / "references/correction-semantic-explanations-v1.json"
LEGACY_POLICY_REVIEWED_AT = "2026-09-02T00:00:00Z"

DECISION_STATUSES = {
    "proposed", "needs_review", "approved", "rejected", "superseded",
    "withdrawn", "stale",
}
STALE_STATUSES = {"current", "stale_source_changed", "evidence_unreachable"}
CLUE_PATTERNS = {
    "identifier_naming": re.compile(
        r"(?:file\s*name|naming|folder|director(?:y|ies)|patient\s*id|"
        r"subject\s*id|case\s*id|per\s+(?:patient|subject|case))", re.I
    ),
    "source_relationship": re.compile(
        r"(?:source\s+collection|derived\s+from|subset\s+of|selected\s+from|"
        r"based\s+on|cohort\s+from)", re.I
    ),
    "count_statement": re.compile(
        r"\b\d[\d,]*\s+(?:patients?|subjects?|cases?|studies?|series|images?|files?)\b",
        re.I,
    ),
    "access_constraint": re.compile(
        r"(?:controlled\s+access|restricted|dbgap|authorization|required\s+agreement|"
        r"data\s+usage\s+agreement|noncommercial)", re.I
    ),
    "geometry": re.compile(
        r"(?:voxel|spacing|slice\s+thickness|orientation|affine|3[- ]?d|volume|"
        r"geometry|dimensions?)", re.I
    ),
    "unavailability": re.compile(
        r"(?:not\s+(?:available|provided|published)|unavailable|withheld|"
        r"cannot\s+be\s+mapped|no\s+patient[- ]level)", re.I
    ),
}

CLINICAL_DECISION_CONSTANTS = (
    "DATASET_SPECIFIC_CONCEPT_CODES",
    "CANONICAL_DISPLAY_VALUES",
    "NLST_ICDO3_MORPHOLOGY_LABELS",
    "NLST_ICDO3_TOPOGRAPHY_LABELS",
    "NLST_CANCER_SCREEN_LABELS",
    "DATASET_SPECIFIC_CLINICAL_VALUE_LABELS",
    "SOURCE_COLUMN_CONCEPT_OVERRIDES",
    "SOURCE_COLUMN_UNIT_OVERRIDES",
    "CURATED_SCREENING_DIAGNOSIS_RESOLUTIONS",
    "PERMANENT_SCREENING_REVIEW_DATASETS",
    "SUBJECT_COLUMN_OVERRIDES",
    "REVIEWED_OFFICIAL_COHORT_PATTERNS",
    "HUNGARIAN_COLORECTAL_ICD10",
    "CT_COLONOGRAPHY_HISTOLOGY",
    "CT_COLONOGRAPHY_NONMALIGNANT_SEVERITY",
    "EA1141_RACE",
    "EA1141_ETHNICITY",
    "EA1141_GRADE",
    "EA1141_HANDLED_COLUMNS",
    "HNSCC_HANDLED_COLUMNS",
    "OFFICIAL_SOURCE_TRANSFORM_VERSIONS",
)

# This inventory is intentionally independent of CLINICAL_DECISION_CONSTANTS:
# coverage tests compare the two so a production transform cannot be added to
# the importer merely by extending the list that the test itself trusts.
CLINICAL_POLICY_INVENTORY = {
    "concept_code_normalization": ("registry_covered", ("DATASET_SPECIFIC_CONCEPT_CODES",)),
    "canonical_display_normalization": ("registry_covered", ("CANONICAL_DISPLAY_VALUES",)),
    "nlst_morphology_decode": ("registry_covered", ("NLST_ICDO3_MORPHOLOGY_LABELS",)),
    "nlst_topography_decode": ("registry_covered", ("NLST_ICDO3_TOPOGRAPHY_LABELS",)),
    "nlst_screening_decode": ("registry_covered", ("NLST_CANCER_SCREEN_LABELS",)),
    "dataset_value_decode": ("registry_covered", ("DATASET_SPECIFIC_CLINICAL_VALUE_LABELS",)),
    "source_column_concept_override": ("registry_covered", ("SOURCE_COLUMN_CONCEPT_OVERRIDES",)),
    "source_column_unit_override": ("registry_covered", ("SOURCE_COLUMN_UNIT_OVERRIDES",)),
    "screening_diagnosis_resolution": ("registry_covered", ("CURATED_SCREENING_DIAGNOSIS_RESOLUTIONS",)),
    "permanent_screening_review": ("registry_covered", ("PERMANENT_SCREENING_REVIEW_DATASETS",)),
    "subject_column_override": ("registry_covered", ("SUBJECT_COLUMN_OVERRIDES",)),
    "reviewed_cohort_pattern": ("registry_covered", ("REVIEWED_OFFICIAL_COHORT_PATTERNS",)),
    "hungarian_colorectal_decode": ("registry_covered", ("HUNGARIAN_COLORECTAL_ICD10",)),
    "ct_colonography_workbook_decode": ("registry_covered", ("CT_COLONOGRAPHY_HISTOLOGY",)),
    "ct_colonography_patient_histology": ("registry_covered", ("CT_COLONOGRAPHY_HISTOLOGY", "CT_COLONOGRAPHY_NONMALIGNANT_SEVERITY")),
    "ea1141_workbook_decode": ("registry_covered", ("EA1141_RACE", "EA1141_ETHNICITY", "EA1141_HANDLED_COLUMNS")),
    "ea1141_patient_diagnosis": ("registry_covered", ("EA1141_GRADE", "EA1141_HANDLED_COLUMNS")),
    "hnscc_workbook_decode": ("registry_covered", ("HNSCC_HANDLED_COLUMNS",)),
    "hnscc_official_cohort_promotion": ("procedural_with_evidence", ("HNSCC_HANDLED_COLUMNS", "REVIEWED_OFFICIAL_COHORT_PATTERNS")),
    "official_source_transform_versioning": ("registry_covered", ("OFFICIAL_SOURCE_TRANSFORM_VERSIONS",)),
}

CLINICAL_PROCEDURAL_POLICIES = (
    {
        "family": "ct_colonography_patient_histology",
        "short_title": "CT COLONOGRAPHY",
        "function": "derive_ct_colonography_patient_diagnoses",
        "resolution": {
            "malignant_codes": list(range(1, 10)),
            "indeterminate_codes": [88, 98],
            "nonmalignant_precedence_constant": "CT_COLONOGRAPHY_NONMALIGNANT_SEVERITY",
            "negative_screening_value": "Non-Cancer",
        },
        "fields": "primary_diagnosis/primary_site",
    },
    {
        "family": "ea1141_patient_diagnosis",
        "short_title": "EA1141",
        "function": "derive_ea1141_patient_diagnoses",
        "resolution": {
            "screening_classes": ["positive", "negative", "withdrawn", "missing"],
            "malignant_outcomes": ["invasive", "dcis"],
            "grade_constant": "EA1141_GRADE",
            "negative_screening_value": "Non-Cancer",
        },
        "fields": "primary_diagnosis/grade",
    },
    {
        "family": "hnscc_official_cohort_promotion",
        "short_title": "HNSCC",
        "function": "promote_hnscc_official_cohort",
        "resolution": {
            "identity_pattern": "HNSCC-\\d{2}-\\d{4}",
            "required_union_size": 627,
            "promotion": "tcia_official_clinical_union",
        },
        "fields": "participant_link_status",
    },
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE registry_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE correction_observations (
    observation_id TEXT PRIMARY KEY,
    source_system TEXT NOT NULL,
    dataset_type TEXT NOT NULL DEFAULT '',
    short_title TEXT NOT NULL DEFAULT '',
    source_record_type TEXT NOT NULL,
    source_record_id TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_field TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    excerpt_sha256 TEXT NOT NULL,
    raw_record_sha256 TEXT NOT NULL,
    source_updated_at TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    detector TEXT NOT NULL,
    detector_version TEXT NOT NULL,
    clue_class TEXT NOT NULL,
    machine_confidence TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE correction_cases (
    case_id TEXT PRIMARY KEY,
    case_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    dataset_type TEXT NOT NULL DEFAULT '',
    short_title TEXT NOT NULL DEFAULT '',
    affected_artifact TEXT NOT NULL,
    affected_scope_json TEXT NOT NULL DEFAULT '{}',
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    next_action TEXT NOT NULL DEFAULT '',
    current_revision_id TEXT,
    stale_status TEXT NOT NULL DEFAULT 'current'
);

CREATE TABLE correction_proposals (
    proposal_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    observation_ids_json TEXT NOT NULL,
    proposed_resolution_json TEXT NOT NULL,
    confidence TEXT NOT NULL,
    proposal_only INTEGER NOT NULL CHECK (proposal_only IN (0, 1)),
    automation_eligible INTEGER NOT NULL DEFAULT 0 CHECK (automation_eligible IN (0, 1)),
    rationale TEXT NOT NULL,
    negative_scope_json TEXT NOT NULL DEFAULT '{}',
    expected_effects_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    producer TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES correction_cases(case_id)
);

CREATE TABLE correction_decisions (
    revision_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    approver TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL,
    approved_at TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL,
    resolution_json TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    negative_scope_json TEXT NOT NULL,
    expected_effects_json TEXT NOT NULL,
    evidence_observation_ids_json TEXT NOT NULL,
    supersedes_revision_id TEXT,
    stale_status TEXT NOT NULL DEFAULT 'current',
    source_kind TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    UNIQUE (decision_id, revision_id),
    FOREIGN KEY (case_id) REFERENCES correction_cases(case_id),
    FOREIGN KEY (supersedes_revision_id) REFERENCES correction_decisions(revision_id)
);

CREATE TABLE correction_effects (
    effect_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL,
    artifact TEXT NOT NULL,
    entity_table TEXT NOT NULL,
    entity_id TEXT NOT NULL DEFAULT '',
    field_name TEXT NOT NULL DEFAULT '',
    effect_kind TEXT NOT NULL,
    before_sha256 TEXT NOT NULL DEFAULT '',
    after_sha256 TEXT NOT NULL DEFAULT '',
    before_value_json TEXT NOT NULL DEFAULT 'null',
    after_value_json TEXT NOT NULL DEFAULT 'null',
    effect_status TEXT NOT NULL DEFAULT 'expected',
    build_fingerprint TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (revision_id) REFERENCES correction_decisions(revision_id)
);

CREATE TABLE correction_validations (
    validation_id TEXT PRIMARY KEY,
    revision_id TEXT,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    actual_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL DEFAULT '',
    executed_at TEXT NOT NULL,
    producer_commit TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (revision_id) REFERENCES correction_decisions(revision_id)
);

CREATE TABLE correction_releases (
    release_fingerprint TEXT PRIMARY KEY,
    release_tag TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    decision_set_sha256 TEXT NOT NULL,
    source_health TEXT NOT NULL,
    change_report_sha256 TEXT NOT NULL DEFAULT ''
);

CREATE TABLE correction_release_revisions (
    release_fingerprint TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    PRIMARY KEY (release_fingerprint, revision_id),
    FOREIGN KEY (release_fingerprint) REFERENCES correction_releases(release_fingerprint),
    FOREIGN KEY (revision_id) REFERENCES correction_decisions(revision_id)
);

CREATE TABLE correction_waivers (
    waiver_id TEXT PRIMARY KEY,
    case_id TEXT,
    rule_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    reason TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES correction_cases(case_id)
);

CREATE TABLE correction_assertions (
    assertion_id TEXT PRIMARY KEY,
    revision_id TEXT,
    artifact TEXT NOT NULL,
    query_name TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    evidence_observation_ids_json TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    FOREIGN KEY (revision_id) REFERENCES correction_decisions(revision_id)
);

CREATE INDEX idx_observations_dataset ON correction_observations(short_title, clue_class);
CREATE INDEX idx_cases_queue ON correction_cases(status, severity, stale_status);
CREATE INDEX idx_decisions_logical ON correction_decisions(decision_id, reviewed_at);
CREATE INDEX idx_effects_revision ON correction_effects(revision_id, artifact, entity_table);

CREATE VIEW agent_correction_queue AS
SELECT c.case_id, c.case_type, c.severity, c.status, c.dataset_type,
       c.short_title, c.affected_artifact, c.affected_scope_json,
       c.first_observed_at, c.last_observed_at, c.owner, c.next_action,
       c.current_revision_id, c.stale_status,
       COUNT(DISTINCT p.proposal_id) AS proposal_count,
       COUNT(DISTINCT o.observation_id) AS observation_count
FROM correction_cases c
LEFT JOIN correction_proposals p USING(case_id)
LEFT JOIN json_each(COALESCE(p.observation_ids_json, '[]')) po
LEFT JOIN correction_observations o ON o.observation_id=po.value
WHERE c.status IN ('proposed','needs_review','stale') OR c.stale_status<>'current'
GROUP BY c.case_id;

CREATE VIEW agent_active_corrections AS
SELECT d.decision_id, d.revision_id, d.case_id, c.case_type, c.dataset_type,
       c.short_title, c.affected_artifact, d.status, d.reviewer, d.approver,
       d.reviewed_at, d.approved_at, d.rationale, d.resolution_json,
       d.scope_json, d.negative_scope_json, d.expected_effects_json,
       d.evidence_observation_ids_json, d.stale_status, d.source_kind,
       d.policy_version
FROM correction_decisions d
JOIN correction_cases c USING(case_id)
WHERE d.status='approved' AND d.stale_status='current'
  AND c.current_revision_id=d.revision_id;

CREATE VIEW agent_field_resolution_trace AS
SELECT e.effect_id, d.decision_id, e.revision_id, c.dataset_type,
       c.short_title, e.artifact, e.entity_table, e.entity_id, e.field_name,
       e.effect_kind, e.before_value_json AS source_or_previous_value_json,
       e.after_value_json AS selected_value_json, e.effect_status,
       d.rationale, d.resolution_json, d.negative_scope_json,
       d.evidence_observation_ids_json, d.reviewer, d.reviewed_at,
       d.stale_status
FROM correction_effects e
JOIN correction_decisions d USING(revision_id)
JOIN correction_cases c USING(case_id);

CREATE VIEW agent_release_evidence_health AS
SELECT r.release_fingerprint, r.release_tag, r.source_health,
       r.decision_set_sha256, r.change_report_sha256,
       r.first_seen_at, r.last_seen_at,
       COUNT(rr.revision_id) AS active_revision_count,
       SUM(CASE WHEN c.current_revision_id<>rr.revision_id
                     OR c.stale_status<>'current' THEN 1 ELSE 0 END) AS stale_revision_count,
       (SELECT COUNT(*) FROM correction_cases c
         WHERE c.status IN ('proposed','needs_review','stale')
           AND c.severity IN ('high','critical')) AS unresolved_high_severity_cases,
       (SELECT COUNT(*) FROM correction_waivers w
         WHERE w.status='active' AND w.expires_at>r.last_seen_at) AS active_waivers
FROM correction_releases r
LEFT JOIN correction_release_revisions rr USING(release_fingerprint)
LEFT JOIN correction_decisions d USING(revision_id)
LEFT JOIN correction_cases c ON c.case_id=d.case_id
GROUP BY r.release_fingerprint;

CREATE VIEW agent_correction_changes_since_release AS
WITH releases AS (
  SELECT release_fingerprint, release_tag FROM correction_releases
), released AS (
  SELECT r.release_fingerprint, r.release_tag, d.decision_id,
         rr.revision_id AS released_revision_id
  FROM correction_releases r
  JOIN correction_release_revisions rr USING(release_fingerprint)
  JOIN correction_decisions d USING(revision_id)
), current_revisions AS (
  SELECT d.decision_id, d.revision_id AS current_revision_id, d.case_id,
         d.status, d.stale_status
  FROM correction_decisions d
  JOIN correction_cases c
    ON c.current_revision_id=d.revision_id AND c.case_id=d.case_id
)
SELECT x.release_fingerprint, x.release_tag, cur.decision_id,
       old.released_revision_id, cur.current_revision_id,
       c.dataset_type, c.short_title, c.case_type, cur.status,
       cur.stale_status,
       CASE WHEN old.released_revision_id IS NULL THEN 'added'
            WHEN cur.status IN ('withdrawn','rejected') THEN 'withdrawn'
            WHEN cur.status='stale' THEN 'stale'
            ELSE 'revised' END AS change_kind,
       COUNT(e.effect_id) AS effect_count
FROM releases x
CROSS JOIN current_revisions cur
JOIN correction_cases c USING(case_id)
LEFT JOIN released old
  ON old.release_fingerprint=x.release_fingerprint
 AND old.decision_id=cur.decision_id
LEFT JOIN correction_effects e ON e.revision_id=cur.current_revision_id
WHERE old.released_revision_id IS NULL
   OR old.released_revision_id<>cur.current_revision_id
GROUP BY x.release_fingerprint, x.release_tag, cur.decision_id,
         old.released_revision_id, cur.current_revision_id;
"""


def canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): canonicalize(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [canonicalize(item) for item in value]
        return sorted(items, key=lambda item: canonical_json(item)) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, re.Pattern):
        return {"pattern": value.pattern, "flags": value.flags}
    if isinstance(value, Path):
        return str(value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    payload = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def stable_id(prefix: str, *values: Any) -> str:
    return f"{prefix}_{digest(list(values))[:24]}"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def split_evidence(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return []
    return [item.strip() for item in re.split(r"(?<=[.!?])\s+|\s*[;|]\s*", cleaned) if item.strip()]


def validate_decision_record(record: dict[str, Any]) -> list[str]:
    required = {
        "decision_id", "revision_id", "case_id", "status", "reviewer",
        "reviewed_at", "rationale", "resolution", "scope", "negative_scope",
        "expected_effects", "evidence_observation_ids", "stale_status",
        "source_kind", "policy_version",
    }
    errors = [f"missing {name}" for name in sorted(required - set(record))]
    if record.get("status") not in DECISION_STATUSES:
        errors.append("invalid status")
    if record.get("stale_status") not in STALE_STATUSES:
        errors.append("invalid stale_status")
    for name in ("resolution", "scope", "negative_scope"):
        if name in record and not isinstance(record[name], dict):
            errors.append(f"{name} must be an object")
    for name in ("expected_effects", "evidence_observation_ids"):
        if name in record and not isinstance(record[name], list):
            errors.append(f"{name} must be an array")
        elif name in record and not record[name]:
            errors.append(f"{name} must not be empty")
    for name in ("reviewer", "reviewed_at", "rationale", "source_kind", "policy_version"):
        if not str(record.get(name) or "").strip():
            errors.append(f"{name} must not be empty")
    if record.get("status") == "approved" and not str(record.get("approved_at") or ""):
        errors.append("approved decisions require approved_at")
    if not record.get("negative_scope"):
        errors.append("negative_scope must not be empty")
    identity = decision_identity(record.get("source_kind", ""), record.get("scope", {}))
    if record.get("decision_id") and record["decision_id"] != identity:
        errors.append("decision_id does not match logical identity")
    revision = revision_identity(record)
    if record.get("revision_id") and record["revision_id"] != revision:
        errors.append("revision_id does not match revision content")
    return errors


def decision_identity(source_kind: str, scope: dict[str, Any]) -> str:
    logical = {
        "source_kind": source_kind,
        "dataset_type": scope.get("dataset_type", ""),
        "short_title": scope.get("short_title", ""),
        "decision_type": scope.get("decision_type", ""),
        "target": scope.get("target", ""),
    }
    return stable_id("decision", logical)


def revision_identity(record: dict[str, Any]) -> str:
    payload = {
        "decision_id": record.get("decision_id", ""),
        "status": record.get("status", ""),
        "reviewer": record.get("reviewer", ""),
        "approver": record.get("approver", ""),
        "reviewed_at": record.get("reviewed_at", ""),
        "approved_at": record.get("approved_at", ""),
        "rationale": record.get("rationale", ""),
        "resolution": record.get("resolution", {}),
        "scope": record.get("scope", {}),
        "negative_scope": record.get("negative_scope", {}),
        "expected_effects": record.get("expected_effects", []),
        "evidence_observation_ids": record.get("evidence_observation_ids", []),
        "supersedes_revision_id": record.get("supersedes_revision_id"),
        "stale_status": record.get("stale_status", "current"),
        "source_kind": record.get("source_kind", ""),
        "policy_version": record.get("policy_version", ""),
    }
    return stable_id("revision", payload)


def create_database(path: Path, *, replace: bool = False) -> sqlite3.Connection:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite registry in place: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


HISTORY_TABLES = (
    "registry_meta",
    "correction_observations",
    "correction_cases",
    "correction_proposals",
    "correction_decisions",
    "correction_effects",
    "correction_validations",
    "correction_waivers",
    "correction_assertions",
)


def copy_prior_registry(conn: sqlite3.Connection, prior_path: Path) -> None:
    """Copy an existing registry into a fresh staged schema without mutation."""
    conn.execute("ATTACH DATABASE ? AS prior", (f"file:{prior_path}?mode=ro",))
    try:
        prior_tables = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM prior.sqlite_master WHERE type='table'"
            )
        }
        conn.execute("BEGIN")
        conn.execute("PRAGMA defer_foreign_keys=ON")
        for table in HISTORY_TABLES:
            if table not in prior_tables:
                continue
            current_columns = [str(row[1]) for row in conn.execute(f"PRAGMA main.table_info({table})")]
            prior_columns = {str(row[1]) for row in conn.execute(f"PRAGMA prior.table_info({table})")}
            columns = [name for name in current_columns if name in prior_columns]
            quoted = ",".join(f'"{name}"' for name in columns)
            conn.execute(
                f'INSERT OR IGNORE INTO main."{table}" ({quoted}) '
                f'SELECT {quoted} FROM prior."{table}"'
            )
        if "correction_releases" in prior_tables:
            conn.execute(
                "INSERT OR IGNORE INTO correction_releases SELECT * FROM prior.correction_releases"
            )
            conn.execute(
                "INSERT OR IGNORE INTO correction_release_revisions "
                "SELECT * FROM prior.correction_release_revisions"
            )
        elif "correction_release_links" in prior_tables:
            conflicts = conn.execute(
                """SELECT release_fingerprint FROM prior.correction_release_links
                   GROUP BY release_fingerprint
                   HAVING COUNT(DISTINCT release_tag)>1
                       OR COUNT(DISTINCT decision_set_sha256)>1
                       OR COUNT(DISTINCT source_health)>1
                       OR COUNT(DISTINCT change_report_sha256)>1"""
            ).fetchall()
            if conflicts:
                raise ValueError("prior registry has ambiguous release fingerprint identities")
            conn.execute(
                """INSERT INTO correction_releases
                   SELECT release_fingerprint, MIN(release_tag), MIN(first_seen_at),
                          MAX(last_seen_at), MIN(decision_set_sha256),
                          MIN(source_health), MIN(change_report_sha256)
                   FROM prior.correction_release_links GROUP BY release_fingerprint"""
            )
            conn.execute(
                """INSERT INTO correction_release_revisions
                   SELECT release_fingerprint,revision_id
                   FROM prior.correction_release_links"""
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("DETACH DATABASE prior")


def insert_observation(
    conn: sqlite3.Connection, *, source_system: str, dataset_type: str,
    short_title: str, source_record_type: str, source_record_id: str,
    source_url: str, source_field: str, excerpt: str, raw_record_sha256: str,
    source_updated_at: str, observed_at: str, detector: str,
    detector_version: str, clue_class: str, machine_confidence: str,
    payload: dict[str, Any] | None = None,
) -> str:
    excerpt = re.sub(r"\s+", " ", excerpt).strip()[:1200]
    excerpt_sha = digest(excerpt.encode("utf-8"))
    observation_id = stable_id(
        "observation", source_system, source_record_type, source_record_id,
        source_field, excerpt_sha, raw_record_sha256, detector_version,
    )
    conn.execute(
        "INSERT OR IGNORE INTO correction_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (observation_id, source_system, dataset_type, short_title,
         source_record_type, source_record_id, source_url, source_field,
         excerpt, excerpt_sha, raw_record_sha256, source_updated_at,
         observed_at, detector, detector_version, clue_class,
         machine_confidence, canonical_json(payload or {})),
    )
    return observation_id


def upsert_machine_case_and_proposal(
    conn: sqlite3.Connection, *, observation_id: str, dataset_type: str,
    short_title: str, clue_class: str, observed_at: str,
) -> None:
    case_id = stable_id("case", "wordpress_clue", dataset_type, short_title, clue_class)
    proposal_id = stable_id("proposal", case_id, observation_id, DETECTOR_VERSION)
    existing_proposal = conn.execute(
        "SELECT 1 FROM correction_proposals WHERE proposal_id=?", (proposal_id,)
    ).fetchone()
    existing_case = conn.execute(
        "SELECT current_revision_id FROM correction_cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if existing_case:
        if existing_case[0] and not existing_proposal:
            conn.execute(
                """UPDATE correction_cases SET last_observed_at=?, status='stale',
                          stale_status='stale_source_changed',
                          next_action='re-review changed evidence'
                   WHERE case_id=?""",
                (observed_at, case_id),
            )
        else:
            conn.execute(
                "UPDATE correction_cases SET last_observed_at=? WHERE case_id=?",
                (observed_at, case_id),
            )
    else:
        conn.execute(
            "INSERT INTO correction_cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, clue_class, "review", "needs_review", dataset_type,
             short_title, "source_observation", canonical_json({"source": "wordpress"}),
             observed_at, observed_at, "", "human_review", None, "current"),
        )
    conn.execute(
        "INSERT OR IGNORE INTO correction_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (proposal_id, case_id, canonical_json([observation_id]),
         canonical_json({"action": "review_authoritative_clue", "clue_class": clue_class}),
         "machine_candidate", 1, 0,
         "Machine-extracted WordPress clue; no derived correction is authorized.",
         canonical_json({"must_not": ["change_derived_values_without_review"]}),
         "[]", observed_at, DETECTOR_VERSION),
    )


def record_fields(record: sqlite3.Row, columns: set[str]) -> Iterable[tuple[str, Any]]:
    for field in (
        "summary", "abstract", "detailed_description", "source_collections",
        "description", "download_title", "title", "subjects", "studies",
        "series", "images", "license_label", "requirements_text",
    ):
        if field in columns and record[field] not in (None, ""):
            yield field, record[field]
    if "raw_json" in columns and record["raw_json"]:
        try:
            raw = json.loads(record["raw_json"])
        except (TypeError, json.JSONDecodeError):
            return
        stack: list[tuple[str, Any]] = [("raw_json", raw)]
        while stack:
            pointer, value = stack.pop()
            if isinstance(value, dict):
                for key, item in value.items():
                    key_text = str(key)
                    next_pointer = f"{pointer}/{key_text}"
                    if isinstance(item, (dict, list)):
                        stack.append((next_pointer, item))
                    elif re.search(r"usage|description|source.?collection|note|download|name|count|access|geometry", key_text, re.I):
                        yield next_pointer, item
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, (dict, list)):
                        stack.append((f"{pointer}/{index}", item))


def ingest_wordpress_observations(
    conn: sqlite3.Connection, snapshot_path: Path, *, observed_at: str,
) -> dict[str, int]:
    counts = {name: 0 for name in CLUE_PATTERNS}
    with closing(sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        for table, record_type in (("agent_datasets", "dataset"), ("agent_current_downloads", "download")):
            exists = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            columns = {str(row[1]) for row in source.execute(f"PRAGMA table_info({table})")}
            order = [name for name in ("dataset_type", "short_title", "download_id") if name in columns]
            sql = f"SELECT * FROM {table}"
            if "hidden" in columns:
                sql += " WHERE hidden=0"
            if order:
                sql += " ORDER BY " + ",".join(order)
            for row in source.execute(sql):
                dataset_type = str(row["dataset_type"] or "") if "dataset_type" in columns else ""
                short_title = str(row["short_title"] or row["parent_short_title"] or "") if "short_title" in columns else str(row["parent_short_title"] or "") if "parent_short_title" in columns else ""
                source_id = str(row["download_id"] or "") if "download_id" in columns else str(row["id"] or row["slug"] or short_title) if "id" in columns else short_title
                source_url = str(row["link"] or "") if "link" in columns else str(row["download_url"] or "") if "download_url" in columns else ""
                updated = str(row["date_updated"] or "") if "date_updated" in columns else ""
                raw_text = str(row["raw_json"] or "") if "raw_json" in columns else canonical_json(dict(row))
                raw_sha = digest(raw_text.encode("utf-8"))
                for field, value in record_fields(row, columns):
                    for excerpt in split_evidence(str(value)):
                        for clue_class, pattern in CLUE_PATTERNS.items():
                            if not pattern.search(excerpt):
                                continue
                            observation_id = insert_observation(
                                conn, source_system="tcia_wordpress",
                                dataset_type=dataset_type, short_title=short_title,
                                source_record_type=record_type, source_record_id=source_id,
                                source_url=source_url, source_field=field, excerpt=excerpt,
                                raw_record_sha256=raw_sha, source_updated_at=updated,
                                observed_at=observed_at, detector="wordpress_field_clue",
                                detector_version=DETECTOR_VERSION, clue_class=clue_class,
                                machine_confidence="candidate",
                                payload={"proposal_only": True},
                            )
                            upsert_machine_case_and_proposal(
                                conn, observation_id=observation_id,
                                dataset_type=dataset_type, short_title=short_title,
                                clue_class=clue_class, observed_at=observed_at,
                            )
                            counts[clue_class] += 1
    return counts


def make_reviewed_decision(
    conn: sqlite3.Connection, *, source_kind: str, dataset_type: str,
    short_title: str, decision_type: str, target: str, status: str,
    reviewer: str, reviewed_at: str, rationale: str, resolution: dict[str, Any],
    scope_extra: dict[str, Any] | None = None,
    negative_scope: dict[str, Any] | None = None,
    expected_effects: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    policy_version: str = "v1", supersedes_revision_id: str | None = None,
) -> str:
    scope = {
        "dataset_type": dataset_type, "short_title": short_title,
        "decision_type": decision_type, "target": target,
        **(scope_extra or {}),
    }
    observation_ids: list[str] = []
    for item in evidence or []:
        raw = canonical_json(item)
        observation_ids.append(insert_observation(
            conn, source_system=str(item.get("source_system") or "reviewed_source"),
            dataset_type=dataset_type, short_title=short_title,
            source_record_type=str(item.get("source_record_type") or "reviewed_evidence"),
            source_record_id=str(item.get("source_record_id") or target),
            source_url=str(item.get("source_url") or ""),
            source_field=str(item.get("source_field") or "reviewed_decision"),
            excerpt=str(item.get("excerpt") or rationale),
            raw_record_sha256=digest(raw.encode("utf-8")),
            source_updated_at=str(item.get("source_updated_at") or ""),
            observed_at=reviewed_at, detector="reviewed_import",
            detector_version=policy_version, clue_class=decision_type,
            machine_confidence="reviewed", payload=item,
        ))
    case_id = stable_id("case", source_kind, scope)
    decision_id = decision_identity(source_kind, scope)
    record = {
        "decision_id": decision_id, "case_id": case_id,
        "status": status, "reviewer": reviewer, "approver": "",
        "reviewed_at": reviewed_at,
        "approved_at": reviewed_at if status == "approved" else "",
        "rationale": rationale, "resolution": resolution, "scope": scope,
        "negative_scope": negative_scope or {"must_not": ["overwrite_raw_source_values"]},
        "expected_effects": expected_effects or [],
        "evidence_observation_ids": sorted(observation_ids),
        "supersedes_revision_id": supersedes_revision_id,
        "stale_status": "current", "source_kind": source_kind,
        "policy_version": policy_version,
    }
    record["revision_id"] = revision_identity(record)
    errors = validate_decision_record(record)
    if errors:
        raise ValueError("Invalid decision: " + "; ".join(errors))
    case_values = (
        case_id, decision_type, "review", status, dataset_type, short_title,
        str(scope.get("affected_artifact") or "derived_metadata"), canonical_json(scope),
        reviewed_at, reviewed_at, reviewer, "", record["revision_id"], "current",
    )
    if conn.execute("SELECT 1 FROM correction_cases WHERE case_id=?", (case_id,)).fetchone():
        if supersedes_revision_id:
            conn.execute(
                """UPDATE correction_cases SET status=?, last_observed_at=?, owner=?,
                          current_revision_id=?, stale_status='current'
                   WHERE case_id=?""",
                (status, reviewed_at, reviewer, record["revision_id"], case_id),
            )
    else:
        conn.execute("INSERT INTO correction_cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", case_values)
    conn.execute(
        """INSERT OR IGNORE INTO correction_decisions VALUES
           (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (record["revision_id"], decision_id, case_id, status, reviewer, "",
         reviewed_at, record["approved_at"], rationale, canonical_json(resolution),
         canonical_json(scope), canonical_json(record["negative_scope"]),
         canonical_json(record["expected_effects"]),
         canonical_json(record["evidence_observation_ids"]), supersedes_revision_id,
         "current", source_kind, policy_version),
    )
    if supersedes_revision_id:
        previous = conn.execute(
            "SELECT decision_id FROM correction_decisions WHERE revision_id=?",
            (supersedes_revision_id,),
        ).fetchone()
        if not previous or str(previous[0]) != decision_id:
            raise ValueError("superseded revision must exist and share decision_id")
    for index, effect in enumerate(record["expected_effects"]):
        effect_id = stable_id("effect", record["revision_id"], index, effect)
        conn.execute(
            "INSERT OR IGNORE INTO correction_effects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (effect_id, record["revision_id"], effect.get("artifact", "derived_metadata"),
             effect.get("entity_table", ""), effect.get("entity_id", ""),
             effect.get("field_name", ""), effect.get("effect_kind", "resolve"),
             "", digest(effect.get("after")), "null",
             canonical_json(effect.get("after")), "expected", ""),
        )
    return record["revision_id"]


def mark_revision_stale(
    path: Path, revision_id: str, *, stale_status: str,
    executed_at: str | None = None,
) -> dict[str, Any]:
    if stale_status not in STALE_STATUSES - {"current"}:
        raise ValueError("stale_status must identify changed or unreachable evidence")
    when = executed_at or utc_now()
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM correction_decisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown revision: {revision_id}")
        case_id = str(row["case_id"])
        record = {
            "decision_id": row["decision_id"], "case_id": case_id,
            "status": "stale", "reviewer": row["reviewer"],
            "approver": row["approver"], "reviewed_at": when,
            "approved_at": "", "rationale": row["rationale"],
            "resolution": json.loads(row["resolution_json"]),
            "scope": json.loads(row["scope_json"]),
            "negative_scope": json.loads(row["negative_scope_json"]),
            "expected_effects": json.loads(row["expected_effects_json"]),
            "evidence_observation_ids": json.loads(row["evidence_observation_ids_json"]),
            "supersedes_revision_id": revision_id,
            "stale_status": stale_status, "source_kind": row["source_kind"],
            "policy_version": row["policy_version"],
        }
        stale_revision_id = revision_identity(record)
        record["revision_id"] = stale_revision_id
        errors = validate_decision_record(record)
        if errors:
            raise ValueError("Invalid stale revision: " + "; ".join(errors))
        conn.execute(
            "INSERT INTO correction_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (stale_revision_id, row["decision_id"], case_id, "stale",
             row["reviewer"], row["approver"], when, "", row["rationale"],
             row["resolution_json"], row["scope_json"], row["negative_scope_json"],
             row["expected_effects_json"], row["evidence_observation_ids_json"],
             revision_id, stale_status, row["source_kind"], row["policy_version"]),
        )
        conn.execute(
            "UPDATE correction_cases SET status='stale', stale_status=?, current_revision_id=?, next_action='re-review changed evidence' WHERE case_id=?",
            (stale_status, stale_revision_id, case_id),
        )
        for effect in conn.execute(
            "SELECT * FROM correction_effects WHERE revision_id=?", (revision_id,)
        ).fetchall():
            effect_id = stable_id("effect", stale_revision_id, effect["effect_id"])
            conn.execute(
                "INSERT INTO correction_effects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (effect_id, stale_revision_id, effect["artifact"], effect["entity_table"],
                 effect["entity_id"], effect["field_name"], effect["effect_kind"],
                 effect["before_sha256"], effect["after_sha256"],
                 effect["before_value_json"], effect["after_value_json"], "stale", ""),
            )
        validation_id = stable_id("validation", stale_revision_id, "stale", stale_status, when)
        conn.execute(
            "INSERT INTO correction_validations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (validation_id, stale_revision_id, "evidence_currency", "v1", "high", "failed",
             canonical_json({"stale_status": "current"}),
             canonical_json({"stale_status": stale_status}), "", when, "",
             "Governing evidence changed or became unreachable."),
        )
        conn.commit()
    return validate_database(path)


def migrate_semantic_explanations(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("explanations"), list):
        raise ValueError("semantic explanation input must use schema_version 1")
    count = 0
    for item in payload["explanations"]:
        required = {
            "artifact", "entity_table", "primary_key", "change_kind",
            "before_sha256", "after_sha256", "reviewer", "approved_at", "rationale",
        }
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError("semantic explanation is missing required fields")
        primary_key = item["primary_key"]
        if (
            not isinstance(primary_key, list) or not primary_key
            or any(not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(v, str) for v in pair) for pair in primary_key)
        ):
            raise ValueError("semantic explanation primary_key must be an ordered [name,value] list")
        kind = str(item["change_kind"])
        before = str(item["before_sha256"])
        after = str(item["after_sha256"])
        if kind not in {"added", "removed", "modified"}:
            raise ValueError("semantic explanation change_kind is invalid")
        for value in (before, after):
            if value and (len(value) != 64 or any(c not in "0123456789abcdef" for c in value.lower())):
                raise ValueError("semantic explanation has an invalid SHA-256")
        if (kind == "added" and (before or not after)) or (kind == "removed" and (not before or after)) or (kind == "modified" and (not before or not after or before == after)):
            raise ValueError("semantic explanation before/after SHA-256 values disagree with change_kind")
        target = canonical_json({
            "artifact": item["artifact"], "table": item["entity_table"],
            "primary_key": primary_key, "change_kind": kind,
        })
        revision_id = make_reviewed_decision(
            conn, source_kind="semantic_change_explanation",
            dataset_type="release", short_title=str(item["artifact"]),
            decision_type="semantic_change", target=target, status="approved",
            reviewer=str(item["reviewer"]), reviewed_at=str(item["approved_at"]),
            rationale=str(item["rationale"]),
            resolution={"approved_change": target},
            expected_effects=[{
                "artifact": str(item["artifact"]),
                "entity_table": str(item["entity_table"]),
                "entity_id": canonical_json(primary_key),
                "effect_kind": kind,
                "after": {"sha256": after},
            }],
            evidence=[{
                "source_record_id": str(item.get("evidence_id") or target),
                "excerpt": str(item.get("evidence") or item["rationale"]),
            }],
            policy_version="semantic-explanations-v1",
        )
        effect = conn.execute(
            "SELECT effect_id FROM correction_effects WHERE revision_id=?", (revision_id,)
        ).fetchone()
        conn.execute(
            """UPDATE correction_effects SET artifact=?,entity_table=?,entity_id=?,
                      effect_kind=?,before_sha256=?,after_sha256=?,effect_status='approved'
               WHERE effect_id=? AND effect_status!='consumed'""",
            (str(item["artifact"]), str(item["entity_table"]), canonical_json(primary_key),
             kind, before, after, str(effect[0])),
        )
        count += 1
    return count


def consume_semantic_explanations(path: Path, report_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    explanations = report.get("semantic_explanations") or {}
    if report.get("unexplained_high_severity") or any(
        explanations.get(key) for key in ("duplicate_matches", "malformed_effects", "unused_effect_ids")
    ):
        raise ValueError("cannot consume explanations from a failing semantic report")
    report_sha = str(report.get("report_sha256") or "")
    effect_ids = list(explanations.get("consumed_effect_ids") or [])
    if len(report_sha) != 64 or len(effect_ids) != len(set(effect_ids)):
        raise ValueError("semantic report identity or consumed effect IDs are invalid")
    with closing(sqlite3.connect(path)) as conn:
        for effect_id in effect_ids:
            cursor = conn.execute(
                """UPDATE correction_effects
                   SET effect_status='consumed',build_fingerprint=?
                   WHERE effect_id=? AND effect_status='approved'""",
                (report_sha, str(effect_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"semantic explanation was not uniquely consumable: {effect_id}")
        conn.commit()
    return {"consumed": len(effect_ids), "report_sha256": report_sha, **validate_database(path)}


def link_release(
    path: Path, *, release_fingerprint: str, release_tag: str,
    source_health: str, observed_at: str, change_report_sha256: str = "",
) -> dict[str, Any]:
    if source_health not in {"verified_current", "degraded", "unverified"}:
        raise ValueError("invalid source_health")
    with closing(sqlite3.connect(path)) as conn:
        active = [str(row[0]) for row in conn.execute(
            """SELECT revision_id FROM agent_active_corrections
               WHERE source_kind!='semantic_change_explanation' ORDER BY revision_id"""
        )]
        decision_sha = digest(active)
        expected_header = (release_tag, decision_sha, source_health, change_report_sha256)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """SELECT release_tag,decision_set_sha256,source_health,
                          change_report_sha256 FROM correction_releases
                   WHERE release_fingerprint=?""",
                (release_fingerprint,),
            ).fetchone()
            if existing:
                existing_members = [str(row[0]) for row in conn.execute(
                    """SELECT revision_id FROM correction_release_revisions
                       WHERE release_fingerprint=? ORDER BY revision_id""",
                    (release_fingerprint,),
                )]
                if tuple(existing) != expected_header or existing_members != active:
                    raise ValueError(
                        "release fingerprint already has a different immutable evidence identity"
                    )
            else:
                conn.execute(
                    "INSERT INTO correction_releases VALUES (?,?,?,?,?,?,?)",
                    (release_fingerprint, release_tag, observed_at, observed_at,
                     decision_sha, source_health, change_report_sha256),
                )
                conn.executemany(
                    "INSERT INTO correction_release_revisions VALUES (?,?)",
                    [(release_fingerprint, revision_id) for revision_id in active],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return validate_database(path)


def add_waiver(
    path: Path, *, rule_id: str, owner: str, reason: str,
    scope: dict[str, Any], created_at: str, expires_at: str,
    case_id: str | None = None,
) -> dict[str, Any]:
    if not rule_id or not owner or not reason or not created_at or not expires_at:
        raise ValueError("waiver rule, owner, reason, creation, and expiry are required")
    if not isinstance(scope, dict) or not scope:
        raise ValueError("waiver scope must be a non-empty object")
    if parse_utc(expires_at) <= parse_utc(created_at):
        raise ValueError("waiver expiry must be after creation")
    waiver_id = stable_id(
        "waiver", rule_id, owner, reason, scope, created_at, expires_at, case_id
    )
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO correction_waivers VALUES (?,?,?,?,?,?,?,?,?)",
            (waiver_id, case_id, rule_id, owner, reason, canonical_json(scope),
             created_at, expires_at, "active"),
        )
        conn.commit()
    return {"waiver_id": waiver_id, **validate_database(path)}


def migrate_public_curation(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    reviewed_at_default = str(payload.get("reviewed_at") or LEGACY_POLICY_REVIEWED_AT)
    if len(reviewed_at_default) == 10:
        reviewed_at_default += "T00:00:00Z"
    reviewer = str(payload.get("review_source") or "legacy_public_non_dicom_review")
    count = 0
    for item in payload.get("decisions") or []:
        reviewed_at = str(item.get("reviewed_at") or reviewed_at_default)
        if len(reviewed_at) == 10:
            reviewed_at += "T00:00:00Z"
        resolution_type = str(item.get("resolution_type") or "reviewed_resolution")
        status = "approved" if item.get("decision_status") in {"resolved", "acknowledged"} else "needs_review"
        rationale = str(item.get("reviewer_note") or "Imported reviewed public non-DICOM decision.")
        expected = [{
            "artifact": "public_non_dicom", "entity_table": "public_non_dicom_assets",
            "entity_id": str(item.get("short_title") or ""),
            "field_name": "participant_link_status",
            "effect_kind": "resolve",
            "after": resolution_type,
        }]
        evidence = [{
            "source_system": "tcia_wordpress" if item.get("evidence_url") else "reviewed_curation",
            "source_record_type": "curation_decision",
            "source_record_id": f"{item.get('short_title','')}:{','.join(str(v) for v in item.get('download_ids') or [])}",
            "source_url": item.get("evidence_url") or item.get("supporting_evidence_url") or "",
            "excerpt": rationale, "curation_record": item,
        }]
        make_reviewed_decision(
            conn, source_kind="public_non_dicom_crosswalk",
            dataset_type=str(item.get("dataset_type") or ""),
            short_title=str(item.get("short_title") or ""),
            decision_type=resolution_type,
            target=",".join(str(v) for v in item.get("download_ids") or []),
            status=status, reviewer=reviewer, reviewed_at=reviewed_at,
            rationale=rationale, resolution=canonicalize(item),
            scope_extra={"download_ids": item.get("download_ids") or [], "affected_artifact": "public_non_dicom"},
            negative_scope={"must_not": ["invent_unpublished_participant_links", "overwrite_raw_subject_ids"]},
            expected_effects=expected, evidence=evidence, policy_version="public-crosswalk-v1",
        )
        count += 1
    return count


def migrate_source_links(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            reviewed_at = str(row.get("reviewed_at") or LEGACY_POLICY_REVIEWED_AT)
            if len(reviewed_at) == 10:
                reviewed_at += "T00:00:00Z"
            short_title = str(row.get("analysis_result_short_title") or "")
            source_title = str(row.get("source_collection_short_title") or "")
            make_reviewed_decision(
                conn, source_kind="analysis_result_source_link",
                dataset_type="Analysis Result", short_title=short_title,
                decision_type="source_collection_relationship", target=source_title,
                status="approved" if row.get("review_status") == "reviewed" else "needs_review",
                reviewer="legacy_reviewed_source_relationships", reviewed_at=reviewed_at,
                rationale=str(row.get("evidence_pointer") or "Reviewed Analysis Result source Collection relationship."),
                resolution={"source_collection_short_title": source_title, "review_status": row.get("review_status")},
                scope_extra={"affected_artifact": "participant_inventory"},
                negative_scope={"must_not": ["copy_unmatched_collection_participants", "infer_links_from_equal_bare_ids"]},
                expected_effects=[{
                    "artifact": "participant_inventory", "entity_table": "participant_source_links",
                    "entity_id": f"{short_title}:{source_title}", "field_name": "link_status",
                    "effect_kind": "link", "after": "reviewed_source_relationship",
                }],
                evidence=[{
                    "source_system": "tcia_wordpress", "source_record_type": "analysis_result_source_relationship",
                    "source_record_id": short_title, "source_url": row.get("evidence_url") or "",
                    "source_field": row.get("evidence_source") or "reviewed_relationship",
                    "excerpt": row.get("evidence_pointer") or "",
                }], policy_version="analysis-result-source-links-v1",
            )
            count += 1
    return count


def load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("tcia_clinical_metadata_registry_source", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load clinical policy module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def migrate_clinical_decisions(conn: sqlite3.Connection, module_path: Path) -> dict[str, int]:
    module = load_module(module_path)
    counts: dict[str, int] = {}
    family_by_constant = {
        constant: sorted(
            family for family, (_, constants) in CLINICAL_POLICY_INVENTORY.items()
            if constant in constants
        )
        for constant in CLINICAL_DECISION_CONSTANTS
    }
    field_by_constant = {
        "CT_COLONOGRAPHY_HISTOLOGY": "lesion_histology/primary_diagnosis",
        "CT_COLONOGRAPHY_NONMALIGNANT_SEVERITY": "primary_diagnosis",
        "EA1141_RACE": "race",
        "EA1141_ETHNICITY": "ethnicity",
        "EA1141_GRADE": "grade",
        "EA1141_HANDLED_COLUMNS": "official_workbook_column_handling",
        "HNSCC_HANDLED_COLUMNS": "official_workbook_column_handling",
        "OFFICIAL_SOURCE_TRANSFORM_VERSIONS": "transform_version",
    }
    for constant in CLINICAL_DECISION_CONSTANTS:
        value = getattr(module, constant, None)
        if value is None:
            counts[constant] = 0
            continue
        if isinstance(value, dict):
            items = list(value.items())
        elif isinstance(value, (set, frozenset, list, tuple)):
            items = [(item, {"membership": True}) for item in value]
        else:
            items = [(constant, value)]
        for key, resolution in items:
            key_json = canonicalize(key)
            target = canonical_json(key_json)
            short_title = ""
            if isinstance(key, tuple) and key:
                short_title = str(key[0])
            elif constant in {"CURATED_SCREENING_DIAGNOSIS_RESOLUTIONS", "PERMANENT_SCREENING_REVIEW_DATASETS", "SUBJECT_COLUMN_OVERRIDES", "REVIEWED_OFFICIAL_COHORT_PATTERNS"}:
                short_title = str(key)
            elif constant.startswith("EA1141_"):
                short_title = "EA1141"
            elif constant.startswith("CT_COLONOGRAPHY_"):
                short_title = "CT COLONOGRAPHY"
            elif constant.startswith("HNSCC_"):
                short_title = "HNSCC"
            elif constant == "OFFICIAL_SOURCE_TRANSFORM_VERSIONS":
                short_title = str(key)
            rationale = f"Imported reviewed clinical normalization policy {constant}[{target}]."
            if isinstance(resolution, dict) and resolution.get("review_evidence"):
                rationale = str(resolution["review_evidence"])
            make_reviewed_decision(
                conn, source_kind="clinical_normalization",
                dataset_type="Collection" if short_title else "",
                short_title=short_title, decision_type=constant.casefold(), target=target,
                status="approved", reviewer="repository_reviewed_clinical_policy",
                reviewed_at=LEGACY_POLICY_REVIEWED_AT, rationale=rationale,
                resolution={
                    "constant": constant, "key": key_json,
                    "value": canonicalize(resolution),
                    "transformation_families": family_by_constant[constant],
                },
                scope_extra={
                    "affected_artifact": "clinical",
                    "clinical_policy_constant": constant,
                    "transformation_families": family_by_constant[constant],
                },
                negative_scope={"must_not": ["overwrite_clinical_rows.row_json", "overwrite_clinical_facts.value_text", "apply_outside_declared_scope"]},
                expected_effects=[{
                    "artifact": "clinical", "entity_table": "clinical_facts",
                    "entity_id": short_title or target,
                    "field_name": field_by_constant.get(constant, "value_resolved"),
                    "effect_kind": "normalize_or_resolve", "after": canonicalize(resolution),
                }],
                evidence=[{
                    "source_system": "repository_policy", "source_record_type": "python_constant",
                    "source_record_id": constant, "source_field": target,
                    "excerpt": rationale, "module": module_path.name,
                }], policy_version="clinical-normalization-v1",
            )
        counts[constant] = len(items)
    module_sha256 = digest(module_path.read_bytes())
    for policy in CLINICAL_PROCEDURAL_POLICIES:
        family = str(policy["family"])
        function = str(policy["function"])
        short_title = str(policy["short_title"])
        rationale = (
            f"Reviewed production clinical derivation {function} for {short_title}; "
            "raw clinical rows and facts remain unchanged."
        )
        make_reviewed_decision(
            conn, source_kind="clinical_derivation_policy",
            dataset_type="Collection", short_title=short_title,
            decision_type=family, target=function, status="approved",
            reviewer="repository_reviewed_clinical_policy",
            reviewed_at=LEGACY_POLICY_REVIEWED_AT, rationale=rationale,
            resolution={
                "family": family, "function": function,
                "algorithm": canonicalize(policy["resolution"]),
                "module_sha256": module_sha256,
            },
            scope_extra={
                "affected_artifact": "clinical",
                "transformation_family": family,
            },
            negative_scope={
                "must_not": [
                    "overwrite_clinical_rows.row_json",
                    "overwrite_clinical_facts.value_text",
                    "classify_explicitly_indeterminate_subjects",
                    "apply_outside_declared_dataset",
                ]
            },
            expected_effects=[{
                "artifact": "clinical", "entity_table": "clinical_facts",
                "entity_id": short_title, "field_name": policy["fields"],
                "effect_kind": "derive_reviewed_patient_facts",
                "after": {"family": family, "policy_version": "clinical-derivation-v1"},
            }],
            evidence=[{
                "source_system": "repository_policy",
                "source_record_type": "python_function",
                "source_record_id": function,
                "source_field": f"scripts/tcia_clinical_metadata.py::{function}",
                "excerpt": rationale,
                "module_sha256": module_sha256,
            }],
            policy_version="clinical-derivation-v1",
        )
    counts["procedural_policies"] = len(CLINICAL_PROCEDURAL_POLICIES)
    return counts


def migrate_assertions(conn: sqlite3.Connection, path: Path) -> int:
    """Project evidence-linked declarative checks into the lifecycle registry."""
    if not path.exists():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    policy_version = str(payload.get("policy_version") or "correction-assertions-v1")
    observed_at = str(payload.get("reviewed_at") or LEGACY_POLICY_REVIEWED_AT)
    if len(observed_at) == 10:
        observed_at += "T00:00:00Z"
    count = 0
    for group_name, group in sorted((payload.get("assertions") or {}).items()):
        evidence_ids: list[str] = []
        for evidence_path in group.get("evidence") or []:
            resolved = ROOT / str(evidence_path)
            evidence_ids.append(insert_observation(
                conn, source_system="repository_assertion_evidence",
                dataset_type="", short_title="",
                source_record_type="declarative_assertion_evidence",
                source_record_id=str(evidence_path), source_url="",
                source_field=group_name,
                excerpt=f"Evidence source for assertion group {group_name}: {evidence_path}",
                raw_record_sha256=digest(resolved.read_bytes()) if resolved.exists() else "",
                source_updated_at="", observed_at=observed_at,
                detector="reviewed_assertion_import", detector_version=policy_version,
                clue_class="validation_assertion", machine_confidence="reviewed",
                payload={"path": str(evidence_path), "exists": resolved.exists()},
            ))
        for query_name, expected in sorted((group.get("expected") or {}).items()):
            assertion_id = stable_id(
                "assertion", policy_version, group_name, query_name, expected,
                sorted(evidence_ids),
            )
            conn.execute(
                "INSERT OR IGNORE INTO correction_assertions VALUES (?,?,?,?,?,?,?,?,?)",
                (assertion_id, None, "public_non_dicom", str(query_name),
                 canonical_json(expected), canonical_json(sorted(evidence_ids)),
                 str(group.get("severity") or "high"), "active", policy_version),
            )
            count += 1
    return count


def decision_set_digest(conn: sqlite3.Connection) -> str:
    rows = [str(row[0]) for row in conn.execute(
        """SELECT revision_id FROM agent_active_corrections
           WHERE source_kind!='semantic_change_explanation' ORDER BY revision_id"""
    )]
    return digest(rows)


def snapshot_source_health(manifest_path: Path | None) -> dict[str, Any]:
    if manifest_path is None or not manifest_path.exists():
        return {"status": "unverified", "sources": {}, "warnings": []}
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    statuses = payload.get("source_status") or {}
    warnings = payload.get("warnings") or []
    if not statuses:
        status = "unverified"
    elif all(value == "live" for value in statuses.values()):
        status = "verified_current"
    else:
        status = "degraded"
    return {
        "status": status, "sources": statuses, "warnings": warnings,
        "manifest_sha256": digest(manifest_path.read_bytes()),
        "generated_at_utc": payload.get("generated_at_utc", ""),
    }


def validate_database(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    counts: dict[str, int] = {}
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            errors.append(f"integrity_check={integrity}")
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            errors.append(f"foreign_key_check={len(foreign_key_errors)}")
        objects = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
        required = {
            "correction_observations", "correction_cases", "correction_proposals",
            "correction_decisions", "correction_effects", "correction_validations",
            "correction_releases", "correction_release_revisions",
            "correction_waivers", "correction_assertions",
            "agent_correction_queue", "agent_active_corrections",
            "agent_field_resolution_trace", "agent_release_evidence_health",
            "agent_correction_changes_since_release",
        }
        missing = sorted(required - objects)
        if missing:
            errors.append("missing registry objects: " + ", ".join(missing))
        for table in sorted(name for name in required if name.startswith("correction_") and name in objects):
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if "correction_decisions" in objects:
            for row in conn.execute("SELECT * FROM correction_decisions"):
                record = {
                    "revision_id": row["revision_id"], "decision_id": row["decision_id"],
                    "case_id": row["case_id"], "status": row["status"],
                    "reviewer": row["reviewer"], "approver": row["approver"],
                    "reviewed_at": row["reviewed_at"], "approved_at": row["approved_at"],
                    "rationale": row["rationale"], "resolution": json.loads(row["resolution_json"]),
                    "scope": json.loads(row["scope_json"]),
                    "negative_scope": json.loads(row["negative_scope_json"]),
                    "expected_effects": json.loads(row["expected_effects_json"]),
                    "evidence_observation_ids": json.loads(row["evidence_observation_ids_json"]),
                    "supersedes_revision_id": row["supersedes_revision_id"],
                    "stale_status": row["stale_status"], "source_kind": row["source_kind"],
                    "policy_version": row["policy_version"],
                }
                errors.extend(f"{row['revision_id']}: {item}" for item in validate_decision_record(record))
                for observation_id in record["evidence_observation_ids"]:
                    if not conn.execute(
                        "SELECT 1 FROM correction_observations WHERE observation_id=?",
                        (observation_id,),
                    ).fetchone():
                        errors.append(
                            f"{row['revision_id']}: missing evidence observation {observation_id}"
                        )
            machine_approved = int(conn.execute(
                """SELECT COUNT(*) FROM correction_decisions d
                   JOIN correction_observations o
                     ON instr(d.evidence_observation_ids_json, o.observation_id)>0
                   WHERE d.status='approved' AND o.machine_confidence='candidate'"""
            ).fetchone()[0])
            if machine_approved:
                errors.append(f"machine candidate observations approve {machine_approved} decision links")
        if "correction_cases" in objects:
            bad_current = int(conn.execute(
                """SELECT COUNT(*) FROM correction_cases c
                   LEFT JOIN correction_decisions d
                     ON d.revision_id=c.current_revision_id AND d.case_id=c.case_id
                   WHERE c.current_revision_id IS NOT NULL AND d.revision_id IS NULL"""
            ).fetchone()[0])
            if bad_current:
                errors.append(f"invalid case current revisions: {bad_current}")
        if "correction_proposals" in objects:
            for proposal_id, evidence_json in conn.execute(
                "SELECT proposal_id,observation_ids_json FROM correction_proposals"
            ):
                for observation_id in json.loads(str(evidence_json)):
                    if not conn.execute(
                        "SELECT 1 FROM correction_observations WHERE observation_id=?",
                        (observation_id,),
                    ).fetchone():
                        errors.append(f"{proposal_id}: missing observation {observation_id}")
            known_observations = {
                str(row[0]) for row in conn.execute(
                    "SELECT observation_id FROM correction_observations"
                )
            }
            for revision_id, evidence_json in conn.execute(
                "SELECT revision_id,evidence_observation_ids_json FROM correction_decisions"
            ):
                missing_evidence = sorted(
                    set(json.loads(evidence_json)) - known_observations
                )
                if missing_evidence:
                    errors.append(
                        f"{revision_id}: missing evidence observations: "
                        + ", ".join(missing_evidence)
                    )
            bad_current = int(conn.execute(
                """SELECT COUNT(*) FROM correction_cases c
                   LEFT JOIN correction_decisions d
                     ON d.revision_id=c.current_revision_id AND d.case_id=c.case_id
                   WHERE c.current_revision_id IS NOT NULL AND d.revision_id IS NULL"""
            ).fetchone()[0])
            if bad_current:
                errors.append(f"invalid current decision revisions: {bad_current}")
        orphan_effects = int(conn.execute(
            "SELECT COUNT(*) FROM correction_effects e LEFT JOIN correction_decisions d USING(revision_id) WHERE d.revision_id IS NULL"
        ).fetchone()[0]) if "correction_effects" in objects else 0
        if orphan_effects:
            errors.append(f"orphan correction effects: {orphan_effects}")
        if {"correction_releases", "correction_release_revisions"}.issubset(objects):
            for release_fingerprint, expected_digest in conn.execute(
                "SELECT release_fingerprint,decision_set_sha256 FROM correction_releases"
            ):
                members = [str(row[0]) for row in conn.execute(
                    """SELECT revision_id FROM correction_release_revisions
                       WHERE release_fingerprint=? ORDER BY revision_id""",
                    (release_fingerprint,),
                )]
                if digest(members) != expected_digest:
                    errors.append(
                        f"{release_fingerprint}: release membership digest mismatch"
                    )
        active_digest = decision_set_digest(conn) if "agent_active_corrections" in objects else ""
    return {"ok": not errors, "errors": errors, "integrity_check": integrity,
            "counts": counts, "active_decision_set_sha256": active_digest}


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def validate_promotion_waivers(path: Path, *, at: str) -> dict[str, Any]:
    """Require exact, expiring, single-use waivers for failed release validations."""
    errors: list[str] = []
    used: list[str] = []
    instant = parse_utc(at)
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        failures = {
            str(row["validation_id"]): row
            for row in conn.execute(
                """SELECT v.*,d.case_id FROM correction_validations v
                   LEFT JOIN correction_decisions d USING(revision_id)
                   WHERE v.status!='passed' AND v.severity IN ('high','critical')
                     AND (
                       (v.revision_id IS NOT NULL AND EXISTS (
                          SELECT 1 FROM agent_active_corrections a
                          WHERE a.revision_id=v.revision_id
                       ))
                       OR
                       (v.revision_id IS NULL AND v.executed_at=(
                          SELECT MAX(v2.executed_at) FROM correction_validations v2
                          WHERE v2.revision_id IS NULL AND v2.rule_id=v.rule_id
                       ))
                     )"""
            )
        }
        matched: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT * FROM correction_waivers WHERE status='active' ORDER BY waiver_id"
        ):
            waiver_id = str(row["waiver_id"])
            try:
                scope = json.loads(str(row["scope_json"]))
                if not isinstance(scope, dict) or set(scope) != {"validation_id", "evidence_sha256"}:
                    raise ValueError("scope must contain exactly validation_id and evidence_sha256")
                expires = parse_utc(str(row["expires_at"]))
                created = parse_utc(str(row["created_at"]))
                if expires <= created:
                    raise ValueError("expiry must be after creation")
                if expires <= instant:
                    raise ValueError("waiver is expired")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                errors.append(f"{waiver_id}: malformed active waiver: {exc}")
                continue
            validation_id = str(scope["validation_id"])
            failure = failures.get(validation_id)
            if (
                failure is None
                or str(row["rule_id"]) != str(failure["rule_id"])
                or str(scope["evidence_sha256"]) != str(failure["evidence_sha256"])
                or (row["case_id"] is not None and str(row["case_id"]) != str(failure["case_id"]))
            ):
                errors.append(f"{waiver_id}: active waiver is unused")
                continue
            matched.setdefault(validation_id, []).append(waiver_id)
        for validation_id, waiver_ids in matched.items():
            if len(waiver_ids) != 1:
                errors.append(f"{validation_id}: duplicate waiver consumption")
            else:
                used.extend(waiver_ids)
        for validation_id in sorted(set(failures) - set(matched)):
            errors.append(f"{validation_id}: failed validation has no active exact waiver")
    return {"ok": not errors, "errors": errors, "used_waiver_ids": sorted(used)}


def package_registry(
    path: Path, *, gzip_out: Path, manifest_out: Path, gzip_level: int = 6,
) -> dict[str, Any]:
    """Write deterministic, hash-bound release artifacts for a valid registry."""
    result = validate_database(path)
    if not result["ok"]:
        raise RuntimeError("Invalid correction registry: " + "; ".join(result["errors"]))
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        meta = dict(conn.execute("SELECT key,value FROM registry_meta"))
        active = int(conn.execute(
            "SELECT COUNT(*) FROM agent_active_corrections WHERE source_kind!='semantic_change_explanation'"
        ).fetchone()[0])
        cases = int(conn.execute("SELECT COUNT(*) FROM correction_cases").fetchone()[0])
        pending = int(conn.execute(
            "SELECT COUNT(*) FROM correction_cases WHERE status IN ('review','needs_review','proposed')"
        ).fetchone()[0])
        failed = int(conn.execute(
            "SELECT COUNT(*) FROM correction_validations WHERE status != 'passed'"
        ).fetchone()[0])
    waiver_health = validate_promotion_waivers(
        path, at=meta.get("generated_at_utc") or utc_now()
    )
    if not waiver_health["ok"]:
        raise RuntimeError(
            "Correction registry is not eligible for stable promotion: "
            + "; ".join(waiver_health["errors"])
        )
    source_health = json.loads(meta.get("source_health_json") or "{}")
    source_status = source_health.get("sources") or {"registry": "validated_component"}
    gzip_out.parent.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as source, gzip_out.open("wb") as raw:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, compresslevel=gzip_level, mtime=0
        ) as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
    sqlite_sha = file_sha256(path)
    gzip_sha = file_sha256(gzip_out)
    decision_sha = str(result["active_decision_set_sha256"])
    manifest = {
        "artifact": "tcia_correction_registry",
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": meta.get("generated_at_utc", ""),
        "sqlite_sha256": sqlite_sha,
        "gzip_sha256": gzip_sha,
        "sqlite_bytes": path.stat().st_size,
        "gzip_bytes": gzip_out.stat().st_size,
        "release_fingerprint": hashlib.sha256(
            canonical_json({
                "artifact": "tcia_correction_registry",
                "schema_version": SCHEMA_VERSION,
                "sqlite_sha256": sqlite_sha,
                "decision_set_sha256": decision_sha,
            }).encode("utf-8")
        ).hexdigest(),
        "decision_set_summary": {
            "sha256": decision_sha,
            "status": str(source_health.get("status") or "unverified"),
            "counts": {
                "active_revisions": active,
                "cases": cases,
                "failed_validations": failed,
                "pending_cases": pending,
                "used_waivers": len(waiver_health["used_waiver_ids"]),
            },
        },
        "source_status": source_status,
        "provenance": {
            "snapshot_sha256": meta.get("snapshot_sha256", ""),
            "snapshot_manifest_sha256": source_health.get("manifest_sha256", ""),
            "prior_release_links": "immutable releases recorded before this build only",
        },
        "storage_contract": {
            "database": gzip_out.name,
            "compression": "gzip-mtime-0",
            "profile": "audit_support",
        },
    }
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return manifest


def build_registry(
    out: Path, *, snapshot: Path | None = None, curation: Path = DEFAULT_CURATION,
    source_links: Path = DEFAULT_SOURCE_LINKS,
    clinical_module: Path = DEFAULT_CLINICAL_MODULE,
    assertions: Path = DEFAULT_ASSERTIONS,
    semantic_explanations: Path = DEFAULT_SEMANTIC_EXPLANATIONS,
    snapshot_manifest: Path | None = None,
    observed_at: str | None = None, replace: bool = False,
    gzip_out: Path | None = None, manifest_out: Path | None = None,
    gzip_level: int = 6,
) -> dict[str, Any]:
    generated = observed_at or utc_now()
    if out.exists() and not replace:
        raise FileExistsError(f"Registry exists: {out}; pass --replace for staged refresh")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{out.name}.", dir=out.parent) as directory:
        staged = Path(directory) / out.name
        with closing(create_database(staged)) as conn:
            if out.exists():
                copy_prior_registry(conn, out)
            public_count = migrate_public_curation(conn, curation)
            source_link_count = migrate_source_links(conn, source_links)
            clinical_counts = migrate_clinical_decisions(conn, clinical_module)
            assertion_count = migrate_assertions(conn, assertions)
            semantic_explanation_count = migrate_semantic_explanations(
                conn, semantic_explanations
            )
            clue_counts = ingest_wordpress_observations(conn, snapshot, observed_at=generated) if snapshot else {}
            source_health = snapshot_source_health(snapshot_manifest)
            meta = {
            "artifact": "tcia_correction_registry", "schema_version": str(SCHEMA_VERSION),
            "generated_at_utc": generated,
            "public_curation_sha256": digest(curation.read_bytes()) if curation.exists() else "",
            "source_links_sha256": digest(source_links.read_bytes()) if source_links.exists() else "",
            "clinical_policy_sha256": digest(clinical_module.read_bytes()),
            "assertions_sha256": digest(assertions.read_bytes()) if assertions.exists() else "",
            "semantic_explanations_sha256": digest(semantic_explanations.read_bytes()) if semantic_explanations.exists() else "",
            "snapshot_sha256": digest(snapshot.read_bytes()) if snapshot else "",
            "source_health": str(source_health["status"]),
            "source_health_json": canonical_json(source_health),
            "migration_counts": canonical_json({
                "public_crosswalk_decisions": public_count,
                "analysis_result_source_links": source_link_count,
                "clinical_decisions": clinical_counts,
                "declarative_assertions": assertion_count,
                "semantic_explanations": semantic_explanation_count,
                "wordpress_clues": clue_counts,
            }),
            "clinical_policy_inventory": canonical_json({
                family: {"classification": classification, "constants": list(constants)}
                for family, (classification, constants) in sorted(CLINICAL_POLICY_INVENTORY.items())
            }),
            }
            conn.executemany(
                """INSERT INTO registry_meta VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                meta.items(),
            )
            active_digest = decision_set_digest(conn)
            conn.execute(
                """INSERT INTO registry_meta VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                ("active_decision_set_sha256", active_digest),
            )
            validation_id = stable_id("validation", "registry_integrity", active_digest)
            conn.execute(
                "INSERT OR IGNORE INTO correction_validations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (validation_id, None, "registry_integrity", "v1", "critical", "passed",
                 canonical_json({"errors": 0}), canonical_json({"errors": 0}), active_digest,
                 generated, "", "Registry was built from validated immutable decision revisions."),
            )
            conn.commit()
        result = validate_database(staged)
        if not result["ok"]:
            raise RuntimeError("Invalid correction registry: " + "; ".join(result["errors"]))
        os.replace(staged, out)
    result["path"] = str(out)
    if bool(gzip_out) != bool(manifest_out):
        raise ValueError("--gzip-out and --manifest-out must be provided together")
    if gzip_out and manifest_out:
        result["manifest"] = package_registry(
            out, gzip_out=gzip_out, manifest_out=manifest_out, gzip_level=gzip_level
        )
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--snapshot", type=Path)
    build.add_argument("--snapshot-manifest", type=Path)
    build.add_argument("--curation", type=Path, default=DEFAULT_CURATION)
    build.add_argument("--source-links", type=Path, default=DEFAULT_SOURCE_LINKS)
    build.add_argument("--clinical-module", type=Path, default=DEFAULT_CLINICAL_MODULE)
    build.add_argument("--assertions", type=Path, default=DEFAULT_ASSERTIONS)
    build.add_argument(
        "--semantic-explanations", type=Path, default=DEFAULT_SEMANTIC_EXPLANATIONS
    )
    build.add_argument("--observed-at")
    build.add_argument("--gzip-out", type=Path)
    build.add_argument("--manifest-out", type=Path)
    build.add_argument("--gzip-level", type=int, default=6)
    build.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--db", type=Path, required=True)
    package = sub.add_parser("package")
    package.add_argument("--db", type=Path, required=True)
    package.add_argument("--gzip-out", type=Path, required=True)
    package.add_argument("--manifest-out", type=Path, required=True)
    package.add_argument("--gzip-level", type=int, default=6)
    consume = sub.add_parser("consume-explanations")
    consume.add_argument("--db", type=Path, required=True)
    consume.add_argument("--report", type=Path, required=True)
    stale = sub.add_parser("mark-stale")
    stale.add_argument("--db", type=Path, required=True)
    stale.add_argument("--revision-id", required=True)
    stale.add_argument(
        "--stale-status",
        choices=("stale_source_changed", "evidence_unreachable"),
        required=True,
    )
    stale.add_argument("--executed-at")
    release = sub.add_parser("link-release")
    release.add_argument("--db", type=Path, required=True)
    release.add_argument("--release-fingerprint", required=True)
    release.add_argument("--release-tag", required=True)
    release.add_argument(
        "--source-health",
        choices=("verified_current", "degraded", "unverified"),
        required=True,
    )
    release.add_argument("--observed-at", required=True)
    release.add_argument("--change-report-sha256", default="")
    waiver = sub.add_parser("add-waiver")
    waiver.add_argument("--db", type=Path, required=True)
    waiver.add_argument("--rule-id", required=True)
    waiver.add_argument("--owner", required=True)
    waiver.add_argument("--reason", required=True)
    waiver.add_argument("--scope-json", required=True)
    waiver.add_argument("--created-at", required=True)
    waiver.add_argument("--expires-at", required=True)
    waiver.add_argument("--case-id")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "build":
            result = build_registry(
                args.out, snapshot=args.snapshot, curation=args.curation,
                source_links=args.source_links, clinical_module=args.clinical_module,
                assertions=args.assertions,
                semantic_explanations=args.semantic_explanations,
                snapshot_manifest=args.snapshot_manifest,
                observed_at=args.observed_at, replace=args.replace,
                gzip_out=args.gzip_out, manifest_out=args.manifest_out,
                gzip_level=args.gzip_level,
            )
        elif args.command == "validate":
            result = validate_database(args.db)
        elif args.command == "package":
            result = {
                "ok": True,
                "manifest": package_registry(
                    args.db, gzip_out=args.gzip_out,
                    manifest_out=args.manifest_out, gzip_level=args.gzip_level,
                ),
            }
        elif args.command == "consume-explanations":
            result = consume_semantic_explanations(args.db, args.report)
        elif args.command == "mark-stale":
            result = mark_revision_stale(
                args.db, args.revision_id, stale_status=args.stale_status,
                executed_at=args.executed_at,
            )
        elif args.command == "link-release":
            result = link_release(
                args.db, release_fingerprint=args.release_fingerprint,
                release_tag=args.release_tag, source_health=args.source_health,
                observed_at=args.observed_at,
                change_report_sha256=args.change_report_sha256,
            )
        else:
            result = add_waiver(
                args.db, rule_id=args.rule_id, owner=args.owner,
                reason=args.reason, scope=json.loads(args.scope_json),
                created_at=args.created_at, expires_at=args.expires_at,
                case_id=args.case_id,
            )
    except (KeyError, OSError, ValueError, RuntimeError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
