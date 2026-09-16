#!/usr/bin/env python3
"""Compare newly built TCIA SQLite assets with their published predecessors."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class TableSpec:
    name: str
    keys: tuple[str, ...] = ()
    severity: str = "review"
    where: str = ""
    nonsemantic_columns: tuple[str, ...] = ()


PROFILES = {
    "snapshot": (
        TableSpec("agent_datasets", ("dataset_type", "short_title")),
        TableSpec(
            "agent_current_downloads",
            ("dataset_type", "short_title", "download_id"),
        ),
        TableSpec("agent_datacite_dois", ("doi",)),
        TableSpec("agent_pathdb_slides"),
    ),
    "controlled": (
        TableSpec(
            "controlled_downloads",
            ("short_title", "download_id", "route_system"),
        ),
        TableSpec("controlled_files"),
        TableSpec("manifest_rows"),
        TableSpec("metadata_rows"),
        TableSpec("radiology_series"),
    ),
    "clinical": (
        TableSpec("clinical_sources", ("source_id",)),
        TableSpec("clinical_downloads", ("source_id",)),
        TableSpec("clinical_idc_tables", ("collection_id", "table_name")),
        TableSpec("clinical_imaging_subjects"),
        TableSpec("clinical_rows"),
        TableSpec("clinical_facts", ("fact_id",), "high"),
        TableSpec("clinical_subjects", ("subject_key",), "high"),
        TableSpec(
            "clinical_dataset_inferences", ("short_title", "concept")
        ),
        TableSpec("clinical_build_warnings"),
    ),
    "pathology": (
        TableSpec(
            "pathology_downloads", ("short_title", "download_id")
        ),
        TableSpec("pathology_package_files"),
        TableSpec("pathology_file_objects"),
        TableSpec("pathdb_slide_crosswalk"),
        TableSpec("pathology_disparities"),
    ),
    "public": (
        TableSpec("public_non_dicom_assets", ("asset_id",), "high"),
        TableSpec("public_non_dicom_asset_participants", ("asset_participant_id",), "high"),
        TableSpec("public_non_dicom_crosswalk_decisions", ("decision_id",), "high"),
        TableSpec("public_non_dicom_crosswalk_evidence", ("crosswalk_id",), "high"),
        TableSpec("public_non_dicom_review_issues", ("issue_id",)),
        TableSpec("public_non_dicom_image_metadata", ("asset_id",), "high"),
        TableSpec("public_non_dicom_dataset_metadata_notes", ("note_id",)),
    ),
    "participant": (
        TableSpec("participants", ("participant_key",), "high"),
        TableSpec("participant_identifiers", ("participant_identifier_id",), "high"),
        TableSpec("participant_assets", ("participant_asset_id",), "high"),
        TableSpec("dataset_assets_without_participant_crosswalk", ("dataset_asset_id",)),
        TableSpec("participant_link_issues", ("issue_id",)),
        TableSpec("participant_identity_evidence", ("identity_evidence_id",), "high"),
        TableSpec("participant_source_links", ("participant_source_link_id",), "high"),
    ),
    "correction": (
        TableSpec(
            "registry_meta", ("key",), "high",
            "key IN ('source_health','active_decision_set_sha256')",
        ),
        TableSpec("correction_observations", ("observation_id",)),
        TableSpec(
            "correction_cases", ("case_id",), "high",
            "current_revision_id IS NULL OR current_revision_id NOT IN "
            "(SELECT revision_id FROM correction_decisions WHERE source_kind='semantic_change_explanation')",
            nonsemantic_columns=("last_observed_at",),
        ),
        TableSpec("correction_proposals", ("proposal_id",)),
        TableSpec(
            "correction_decisions", ("revision_id",), "high",
            "source_kind!='semantic_change_explanation'",
        ),
        TableSpec(
            "correction_effects", ("effect_id",), "high",
            "revision_id NOT IN (SELECT revision_id FROM correction_decisions "
            "WHERE source_kind='semantic_change_explanation')",
        ),
        TableSpec(
            "correction_validations",
            ("validation_id",),
            "high",
            nonsemantic_columns=("executed_at", "observed_at_utc"),
        ),
        TableSpec("correction_releases", ("release_fingerprint",), "high"),
        TableSpec(
            "correction_release_revisions",
            ("release_fingerprint", "revision_id"),
            "high",
        ),
        TableSpec("correction_waivers", ("waiver_id",), "high"),
        TableSpec("agent_release_evidence_health", ("release_fingerprint",), "high"),
    ),
}

ARTIFACT_IDS = {
    "snapshot": "snapshot",
    "controlled": "controlled_access",
    "clinical": "clinical",
    "pathology": "pathology",
    "public": "public_non_dicom",
    "participant": "participant_inventory",
    "correction": "correction_registry",
}

GEOMETRY_SUMMARY_COLUMNS = {
    "geometry_status",
    "geometry_assessment_method",
    "geometry_assessment_source",
    "geometry_assessed_at_utc",
    "geometry_details_json",
}

PARTICIPANT_GEOMETRY_SUMMARY_COLUMNS = {
    "geometry_status",
    "geometry_checked_count",
    "geometry_regular_count",
    "geometry_not_regular_count",
    "geometry_not_checked_count",
}


def canonical_download_id(value: object) -> str:
    """Treat a scalar download ID and its singleton JSON-list form equally."""
    text = str(value or "")
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text
    if isinstance(decoded, list) and len(decoded) == 1:
        return str(decoded[0] or "")
    return text


def accepted_geometry_refreshes(
    high_changes: list[dict[str, object]],
    assets: list[tuple[str, Path, Path | None]],
    report_path: Path | None,
) -> list[dict[str, object]]:
    """Recognize only safe geometry invalidations for explicitly changed scopes."""
    if report_path is None:
        return []
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    records = payload.get("records")
    if not isinstance(records, list):
        raise RuntimeError("geometry refresh report records must be a list")
    changed_scopes = {
        (
            str(item.get("dataset_type") or ""),
            str(item.get("short_title") or ""),
            canonical_download_id(item.get("download_id")),
        )
        for item in records
        if isinstance(item, dict) and item.get("status") in {"new", "changed"}
    }
    public_asset = next((item for item in assets if item[0] == "public"), None)
    if not changed_scopes or public_asset is None or public_asset[2] is None:
        return []
    _, new_path, old_path = public_asset
    if not old_path.exists():
        return []
    accepted: list[dict[str, object]] = []
    with sqlite3.connect(new_path) as new, sqlite3.connect(old_path) as old:
        columns = [
            str(row[1])
            for row in new.execute("PRAGMA table_info(public_non_dicom_assets)")
        ]
        selected = ", ".join(quote_identifier(name) for name in columns)
        new_rows: dict[str, dict[str, object]] = {}
        old_rows: dict[str, dict[str, object]] = {}
        for scope in changed_scopes:
            parameters = tuple(scope[:2])
            for row in new.execute(
                f"SELECT {selected} FROM public_non_dicom_assets "
                "WHERE dataset_type=? AND short_title=?",
                parameters,
            ):
                record = dict(zip(columns, row))
                if canonical_download_id(record.get("download_id")) == scope[2]:
                    new_rows[str(record["asset_id"])] = record
            for row in old.execute(
                f"SELECT {selected} FROM public_non_dicom_assets "
                "WHERE dataset_type=? AND short_title=?",
                parameters,
            ):
                record = dict(zip(columns, row))
                if canonical_download_id(record.get("download_id")) == scope[2]:
                    old_rows[str(record["asset_id"])] = record
        for change in high_changes:
            if (
                change.get("artifact") != "public_non_dicom"
                or change.get("table") != "public_non_dicom_assets"
                or change.get("change_kind") != "modified"
            ):
                continue
            primary_key = change.get("primary_key")
            if not (
                isinstance(primary_key, list)
                and len(primary_key) == 1
                and isinstance(primary_key[0], list)
                and len(primary_key[0]) == 2
                and primary_key[0][0] == "asset_id"
            ):
                continue
            asset_id = str(primary_key[0][1])
            after = new_rows.get(asset_id)
            before = old_rows.get(asset_id)
            if after is None or before is None:
                continue
            changed_columns = {
                name for name in columns if canonical_value(before[name]) != canonical_value(after[name])
            }
            scope = tuple(
                canonical_download_id(after.get(name)) if name == "download_id"
                else str(after.get(name) or "")
                for name in ("dataset_type", "short_title", "download_id")
            )
            safe_after = (
                after.get("geometry_status") == "not_checked"
                and after.get("geometry_assessment_method") == "not_assessed"
                and (after.get("geometry_assessment_source") or "") == ""
                and after.get("geometry_assessed_at_utc") is None
                and after.get("geometry_details_json") == "{}"
            )
            assessed_before = str(before.get("geometry_status") or "").startswith("checked_") or before.get("geometry_status") == "mixed"
            if (
                scope in changed_scopes
                and changed_columns
                and changed_columns.issubset(GEOMETRY_SUMMARY_COLUMNS)
                and safe_after
                and assessed_before
            ):
                accepted.append({
                    **change,
                    "scope": list(scope),
                    "changed_columns": sorted(changed_columns),
                    "reason": "changed geometry scope safely invalidated pending HPC refresh",
                })
    participant_asset = next((item for item in assets if item[0] == "participant"), None)
    changed_datasets = {(scope[0], scope[1]) for scope in changed_scopes}
    if participant_asset is not None and participant_asset[2] is not None:
        _, new_path, old_path = participant_asset
        if old_path.exists():
            with sqlite3.connect(new_path) as new, sqlite3.connect(old_path) as old:
                columns = [
                    str(row[1])
                    for row in new.execute("PRAGMA table_info(participant_assets)")
                ]
                selected = ", ".join(f"a.{quote_identifier(name)}" for name in columns)
                for change in high_changes:
                    if (
                        change.get("artifact") != "participant_inventory"
                        or change.get("table") != "participant_assets"
                        or change.get("change_kind") != "modified"
                    ):
                        continue
                    primary_key = change.get("primary_key")
                    if not (
                        isinstance(primary_key, list)
                        and len(primary_key) == 1
                        and isinstance(primary_key[0], list)
                        and len(primary_key[0]) == 2
                        and primary_key[0][0] == "participant_asset_id"
                    ):
                        continue
                    participant_asset_id = str(primary_key[0][1])
                    query = (
                        f"SELECT {selected}, p.dataset_type, p.short_title "
                        "FROM participant_assets a JOIN participants p USING(participant_key) "
                        "WHERE a.participant_asset_id=?"
                    )
                    after_row = new.execute(query, (participant_asset_id,)).fetchone()
                    before_row = old.execute(query, (participant_asset_id,)).fetchone()
                    if after_row is None or before_row is None:
                        continue
                    after = dict(zip((*columns, "dataset_type", "short_title"), after_row))
                    before = dict(zip((*columns, "dataset_type", "short_title"), before_row))
                    changed_columns = {
                        name for name in columns
                        if canonical_value(before[name]) != canonical_value(after[name])
                    }
                    safe_after = (
                        after.get("geometry_status") == "not_checked"
                        and int(after.get("geometry_checked_count") or 0) == 0
                        and int(after.get("geometry_regular_count") or 0) == 0
                        and int(after.get("geometry_not_regular_count") or 0) == 0
                        and int(after.get("geometry_not_checked_count") or 0) > 0
                    )
                    assessed_before = int(before.get("geometry_checked_count") or 0) > 0
                    dataset = (
                        str(after.get("dataset_type") or ""),
                        str(after.get("short_title") or ""),
                    )
                    if (
                        dataset in changed_datasets
                        and changed_columns
                        and changed_columns.issubset(PARTICIPANT_GEOMETRY_SUMMARY_COLUMNS)
                        and safe_after
                        and assessed_before
                    ):
                        accepted.append({
                            **change,
                            "scope": [*dataset, "*"],
                            "changed_columns": sorted(changed_columns),
                            "reason": "participant geometry summary safely invalidated by changed public scope",
                        })
    return accepted


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def object_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    try:
        return {
            str(row[1])
            for row in conn.execute(
                f"PRAGMA table_info({quote_identifier(name)})"
            )
        }
    except sqlite3.Error:
        return set()


def validate_declared_key(conn: sqlite3.Connection, spec: TableSpec) -> None:
    if not spec.keys:
        raise RuntimeError(f"high-severity object has no declared key: {spec.name}")
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name=?", (spec.name,)
    ).fetchone()
    if row is None:
        return
    info = list(conn.execute(f"PRAGMA table_info({quote_identifier(spec.name)})"))
    columns = {str(item[1]) for item in info}
    if not set(spec.keys).issubset(columns):
        raise RuntimeError(f"declared key columns are absent from {spec.name}")
    physical = tuple(
        str(item[1]) for item in sorted(info, key=lambda item: int(item[5]) or 10**9)
        if int(item[5]) > 0
    )
    if str(row[0]) == "table" and physical != spec.keys:
        raise RuntimeError(
            f"configured key for {spec.name} differs from SQLite primary key: "
            f"configured={spec.keys!r}, sqlite={physical!r}"
        )
    selected = ",".join(quote_identifier(key) for key in spec.keys)
    where = f" WHERE {spec.where}" if spec.where else ""
    duplicate = conn.execute(
        f"SELECT 1 FROM {quote_identifier(spec.name)}{where} "
        f"GROUP BY {selected} HAVING COUNT(*)>1 LIMIT 1"
    ).fetchone()
    if duplicate:
        raise RuntimeError(f"declared key is not unique for {spec.name}")


def row_count(conn: sqlite3.Connection, spec: TableSpec) -> int | None:
    if not object_columns(conn, spec.name):
        return None
    where = f" WHERE {spec.where}" if spec.where else ""
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {quote_identifier(spec.name)}{where}"
        ).fetchone()[0]
    )


def key_rows(
    conn: sqlite3.Connection,
    spec: TableSpec,
    *,
    limit: int | None = None,
) -> list[tuple[str, ...]]:
    columns = object_columns(conn, spec.name)
    if not spec.keys or not set(spec.keys).issubset(columns):
        return []
    selected = ", ".join(quote_identifier(key) for key in spec.keys)
    sql = (
        f"SELECT DISTINCT {selected} FROM {quote_identifier(spec.name)}"
    )
    if spec.where:
        sql += f" WHERE {spec.where}"
    sql += f" ORDER BY {selected}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [
        tuple("" if value is None else str(value) for value in row)
        for row in conn.execute(sql)
    ]


def format_key(row: Iterable[str]) -> str:
    return " / ".join(value or "(blank)" for value in row)


def canonical_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    return value


def keyed_row_digests(
    conn: sqlite3.Connection, spec: TableSpec
) -> Iterable[tuple[tuple[str, ...], str]]:
    """Stream stable keys and complete row digests in key order."""
    columns = [
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({quote_identifier(spec.name)})")
        if str(row[1]) not in spec.nonsemantic_columns
    ]
    if not spec.keys or not set(spec.keys).issubset(columns):
        return
    selected = ", ".join(quote_identifier(name) for name in columns)
    ordering = ", ".join(quote_identifier(name) for name in spec.keys)
    sql = f"SELECT {selected} FROM {quote_identifier(spec.name)}"
    if spec.where:
        sql += f" WHERE {spec.where}"
    sql += f" ORDER BY {ordering}"
    for row in conn.execute(sql):
        values = dict(zip(columns, row))
        key = tuple("" if values[name] is None else str(values[name]) for name in spec.keys)
        payload = json.dumps(
            {name: canonical_value(values[name]) for name in columns},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        yield key, hashlib.sha256(payload).hexdigest()


def keyed_row_records(
    conn: sqlite3.Connection, spec: TableSpec
) -> Iterable[tuple[tuple[str, ...], str, str]]:
    """Stream key, full-row digest, and key-independent content digest."""
    columns = [
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({quote_identifier(spec.name)})")
        if str(row[1]) not in spec.nonsemantic_columns
    ]
    if not spec.keys or not set(spec.keys).issubset(columns):
        return
    selected = ", ".join(quote_identifier(name) for name in columns)
    ordering = ", ".join(quote_identifier(name) for name in spec.keys)
    sql = f"SELECT {selected} FROM {quote_identifier(spec.name)}"
    if spec.where:
        sql += f" WHERE {spec.where}"
    sql += f" ORDER BY {ordering}"
    for row in conn.execute(sql):
        values = dict(zip(columns, row))
        key = tuple("" if values[name] is None else str(values[name]) for name in spec.keys)
        full_payload = {
            name: canonical_value(values[name]) for name in columns
        }
        content_payload = {
            name: value for name, value in full_payload.items() if name not in spec.keys
        }
        yield (
            key,
            hashlib.sha256(json.dumps(
                full_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            hashlib.sha256(json.dumps(
                content_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
        )


def compare_keyed_rows(
    new: sqlite3.Connection, old: sqlite3.Connection, spec: TableSpec,
    *, max_items: int,
) -> tuple[
    int, int, int, list[tuple[str, tuple[str, ...]]],
    list[tuple[str, tuple[str, ...], str, str]], list[dict[str, object]],
]:
    new_rows = iter(keyed_row_records(new, spec))
    old_rows = iter(keyed_row_records(old, spec))
    new_item = next(new_rows, None)
    old_item = next(old_rows, None)
    added = removed = modified = 0
    examples: list[tuple[str, tuple[str, ...]]] = []
    changes: list[tuple[str, tuple[str, ...], str, str]] = []
    added_by_content: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    removed_by_content: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    while new_item is not None or old_item is not None:
        if old_item is None or (new_item is not None and new_item[0] < old_item[0]):
            added += 1
            if len(examples) < max_items:
                examples.append(("added", new_item[0]))
            changes.append(("added", new_item[0], "", new_item[1]))
            added_by_content.setdefault(new_item[2], []).append((new_item[0], new_item[1]))
            new_item = next(new_rows, None)
        elif new_item is None or old_item[0] < new_item[0]:
            removed += 1
            if len(examples) < max_items:
                examples.append(("removed", old_item[0]))
            changes.append(("removed", old_item[0], old_item[1], ""))
            removed_by_content.setdefault(old_item[2], []).append((old_item[0], old_item[1]))
            old_item = next(old_rows, None)
        else:
            if new_item[1] != old_item[1]:
                modified += 1
                if len(examples) < max_items:
                    examples.append(("modified", new_item[0]))
                changes.append(("modified", new_item[0], old_item[1], new_item[1]))
            new_item = next(new_rows, None)
            old_item = next(old_rows, None)
    migrations: list[dict[str, object]] = []
    for content_sha in sorted(set(added_by_content).intersection(removed_by_content)):
        added_rows = added_by_content[content_sha]
        removed_rows = removed_by_content[content_sha]
        # Ambiguous duplicate payloads are deliberately not paired.
        if len(added_rows) != 1 or len(removed_rows) != 1:
            continue
        old_key, old_digest = removed_rows[0]
        new_key, new_digest = added_rows[0]
        migrations.append({
            "old_primary_key": [
                [column, value] for column, value in zip(spec.keys, old_key)
            ],
            "new_primary_key": [
                [column, value] for column, value in zip(spec.keys, new_key)
            ],
            "old_row_sha256": old_digest,
            "new_row_sha256": new_digest,
            "non_primary_key_content_sha256": content_sha,
        })
    return added, removed, modified, examples, changes, migrations


def load_explanations(
    path: Path | None, *, allowed_artifacts: set[str],
) -> tuple[list[dict[str, object]], list[str]]:
    """Load only approved effects on current approved decision revisions."""
    if path is None:
        return [], []
    explanations: list[dict[str, object]] = []
    malformed: list[str] = []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            """SELECT e.effect_id,e.artifact,e.entity_table,e.entity_id,
                      e.effect_kind,e.before_sha256,e.after_sha256
                 FROM correction_effects e
                 JOIN correction_decisions d USING(revision_id)
                 JOIN correction_cases c
                   ON c.case_id=d.case_id AND c.current_revision_id=d.revision_id
                WHERE e.effect_status='approved'
                  AND d.status='approved' AND d.stale_status='current'
                ORDER BY e.effect_id"""
        )
        for effect_id, artifact, table, entity_id, kind, before, after in rows:
            if str(artifact) not in allowed_artifacts:
                continue
            try:
                primary_key = json.loads(str(entity_id))
                if (
                    not isinstance(primary_key, list)
                    or not primary_key
                    or any(
                        not isinstance(item, list)
                        or len(item) != 2
                        or not isinstance(item[0], str)
                        or not isinstance(item[1], str)
                        for item in primary_key
                    )
                ):
                    raise ValueError("primary key must be a non-empty ordered [name,value] list")
                if kind not in {"added", "removed", "modified"}:
                    raise ValueError("change kind is invalid")
                before = str(before or "")
                after = str(after or "")
                for label, value in (("before", before), ("after", after)):
                    if value and (len(value) != 64 or any(c not in "0123456789abcdef" for c in value.lower())):
                        raise ValueError(f"{label} SHA-256 is invalid")
                if kind == "added" and (before or not after):
                    raise ValueError("added effects require only an after SHA-256")
                if kind == "removed" and (not before or after):
                    raise ValueError("removed effects require only a before SHA-256")
                if kind == "modified" and (not before or not after or before == after):
                    raise ValueError("modified effects require distinct before/after SHA-256 values")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                malformed.append(f"{effect_id}: {exc}")
                continue
            explanations.append({
                "effect_id": str(effect_id), "artifact": str(artifact),
                "table": str(table), "primary_key": primary_key,
                "change_kind": str(kind), "before_sha256": before,
                "after_sha256": after,
            })
    return explanations, malformed


def explanation_identity(change: dict[str, object]) -> str:
    return json.dumps(
        {key: change[key] for key in (
            "artifact", "table", "primary_key", "change_kind",
            "before_sha256", "after_sha256",
        )}, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def screening_reviews(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    columns = object_columns(conn, "clinical_dataset_inferences")
    required = {
        "short_title",
        "concept",
        "review_required",
        "review_reason",
        "screening_signal",
        "raw_value",
        "candidate_subjects",
        "subjects_applied",
        "subjects_suppressed",
    }
    if not required.issubset(columns):
        return {}
    rows = conn.execute(
        """SELECT short_title, review_reason, screening_signal, raw_value,
                  candidate_subjects, subjects_applied, subjects_suppressed
           FROM clinical_dataset_inferences
           WHERE concept = 'primary_diagnosis' AND review_required = 1
           ORDER BY short_title"""
    )
    return {
        str(row[0]): {
            "reason": str(row[1] or ""),
            "signal": str(row[2] or ""),
            "label": str(row[3] or ""),
            "candidate_subjects": str(row[4] or 0),
            "subjects_applied": str(row[5] or 0),
            "subjects_suppressed": str(row[6] or 0),
        }
        for row in rows
    }


def screening_resolutions(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, str]]:
    columns = object_columns(conn, "clinical_dataset_inferences")
    required = {
        "short_title",
        "concept",
        "review_required",
        "review_reason",
        "review_evidence",
        "raw_value",
        "subjects_applied",
    }
    if not required.issubset(columns):
        return {}
    rows = conn.execute(
        """SELECT short_title, review_reason, review_evidence, raw_value,
                  subjects_applied
           FROM clinical_dataset_inferences
           WHERE concept = 'primary_diagnosis'
             AND review_required = 0
             AND review_reason LIKE 'screening_review_resolved_%'
           ORDER BY short_title"""
    )
    return {
        str(row[0]): {
            "reason": str(row[1] or ""),
            "evidence": str(row[2] or ""),
            "label": str(row[3] or ""),
            "subjects_applied": str(row[4] or 0),
        }
        for row in rows
    }


def github_escape(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def correction_baseline_mode(path: Path) -> dict[str, object]:
    """Load the exact bootstrap disposition from a validated registry."""
    with sqlite3.connect(path) as conn:
        meta = dict(conn.execute("SELECT key,value FROM registry_meta"))
        evidence_json = meta.get("bootstrap_evidence_json", "")
        evidence_sha = meta.get("bootstrap_evidence_sha256", "")
        if not evidence_json or hashlib.sha256(evidence_json.encode("utf-8")).hexdigest() != evidence_sha:
            raise RuntimeError("correction baseline lacks hash-valid bootstrap evidence")
        evidence = json.loads(evidence_json)
        baseline = evidence.get("baseline_mode")
        prior = evidence.get("prior_published_bundle") or {}
        initial = evidence.get("initial_registry") or {}
        scope = evidence.get("authorization_scope")
        if baseline != {
            "reason": "component_absent_in_verified_prior_contract",
            "gating_disposition": "baseline_established_not_compared",
        }:
            raise RuntimeError("correction baseline disposition is not exact")
        if scope != {
            "allows_initial_registry_baseline": True,
            "allows_metadata_row_changes": False,
            "allows_future_missing_or_corrupt_registry": False,
        }:
            raise RuntimeError("correction baseline authorization scope is not exact")
        if (
            prior.get("schema_version") != 2
            or prior.get("correction_component_absent") is not True
            or prior.get("release_fingerprint")
               != meta.get("bootstrap_prior_release_fingerprint")
            or initial.get("decision_set_sha256")
               != meta.get("active_decision_set_sha256")
            or initial.get("source_health") != "verified_current"
        ):
            raise RuntimeError("correction baseline evidence is inconsistent")
        validation = conn.execute(
            """SELECT status,evidence_sha256 FROM correction_validations
               WHERE rule_id='initial_registry_bootstrap'"""
        ).fetchall()
        if len(validation) != 1 or tuple(validation[0]) != ("passed", evidence_sha):
            raise RuntimeError("correction baseline validation is missing or inconsistent")
        prior_state_counts = {
            "correction_releases": conn.execute(
                "SELECT COUNT(*) FROM correction_releases"
            ).fetchone()[0],
            "correction_release_revisions": conn.execute(
                "SELECT COUNT(*) FROM correction_release_revisions"
            ).fetchone()[0],
            "consumed_effects": conn.execute(
                """SELECT COUNT(*) FROM correction_effects
                   WHERE effect_status='consumed' OR build_fingerprint!=''"""
            ).fetchone()[0],
        }
        if any(prior_state_counts.values()):
            raise RuntimeError(
                "correction baseline cannot authorize a missing prior registry after "
                "release linkage or semantic-effect consumption: "
                + json.dumps(prior_state_counts, sort_keys=True)
            )
        return {
            "component": "correction_registry",
            "prior_release_fingerprint": prior["release_fingerprint"],
            "prior_manifest_sha256": prior["manifest_sha256"],
            "evidence_sha256": evidence_sha,
            **baseline,
        }


def compare_asset(
    name: str,
    new_path: Path,
    old_path: Path | None,
    *,
    max_items: int,
) -> tuple[
    list[dict[str, object]], list[str], list[str],
    list[dict[str, object]], list[dict[str, object]],
]:
    new = sqlite3.connect(new_path)
    old = sqlite3.connect(old_path) if old_path and old_path.exists() else None
    rows: list[dict[str, object]] = []
    details: list[str] = []
    warnings: list[str] = []
    high_changes: list[dict[str, object]] = []
    primary_key_migrations: list[dict[str, object]] = []
    for spec in PROFILES[name]:
        new_count = row_count(new, spec)
        if new_count is None:
            continue
        old_count = row_count(old, spec) if old else None
        if spec.severity == "high":
            validate_declared_key(new, spec)
            if old_count is not None and old is not None:
                validate_declared_key(old, spec)
        old_display = 0 if old_count is None else old_count
        added = max(new_count - old_display, 0)
        removed = max(old_display - new_count, 0)
        modified = 0
        changed_keys: list[tuple[str, tuple[str, ...]]] = []
        semantic_changes: list[tuple[str, tuple[str, ...], str, str]] = []
        if spec.keys:
            if old is None or old_count is None:
                changed_keys = [("added", item) for item in key_rows(new, spec, limit=max_items)]
                added = new_count
            else:
                (
                    added, removed, modified, changed_keys, semantic_changes,
                    table_migrations,
                ) = compare_keyed_rows(new, old, spec, max_items=max_items)
                for migration in table_migrations:
                    primary_key_migrations.append({
                        "artifact": ARTIFACT_IDS[name],
                        "table": spec.name,
                        **migration,
                    })
        rows.append(
            {
                "asset": name,
                "table": spec.name,
                "old": old_count,
                "new": new_count,
                "added": added,
                "removed": removed,
                "modified": modified,
                "severity": spec.severity,
            }
        )
        if changed_keys:
            details.append(
                f"**{name} · {spec.name}**\n\n"
                + "\n".join(
                    f"- {kind}: `{format_key(row)}`" for kind, row in changed_keys
                )
            )
        if added or removed or modified:
            if added and not removed and not modified:
                warnings.append(
                    f"{name}: {spec.name} added {added:,} row"
                    f"{'s' if added != 1 else ''}"
                )
            else:
                warnings.append(
                    f"{name}: {spec.name} added {added:,}, removed {removed:,}, "
                    f"modified {modified:,} rows"
                )
            if spec.severity == "high" and old is not None:
                for kind, key, before, after in semantic_changes:
                    high_changes.append({
                        "artifact": ARTIFACT_IDS[name],
                        "table": spec.name,
                        "primary_key": [[column, value] for column, value in zip(spec.keys, key)],
                        "change_kind": kind,
                        "before_sha256": before,
                        "after_sha256": after,
                    })
    if old is None:
        warnings.append(
            f"{name}: no previous SQLite was available; report uses a new baseline"
        )
    new.close()
    if old:
        old.close()
    return rows, details, warnings, high_changes, primary_key_migrations


def build_report(args: argparse.Namespace) -> tuple[str, list[str], dict[str, object]]:
    assets: list[tuple[str, Path, Path | None]] = []
    for name in PROFILES:
        new_value = getattr(args, f"{name}_new")
        old_value = getattr(args, f"{name}_old")
        if new_value:
            assets.append(
                (
                    name,
                    Path(new_value),
                    Path(old_value) if old_value else None,
                )
            )
    if not assets:
        raise RuntimeError("Pass at least one --<asset>-new SQLite path")

    comparison_rows: list[dict[str, object]] = []
    detail_blocks: list[str] = []
    warnings: list[str] = []
    high_changes: list[dict[str, object]] = []
    primary_key_migrations: list[dict[str, object]] = []
    baseline_modes: list[dict[str, object]] = []
    for name, new_path, old_path in assets:
        rows, details, asset_warnings, asset_high, asset_migrations = compare_asset(
            name, new_path, old_path, max_items=args.max_items
        )
        comparison_rows.extend(rows)
        detail_blocks.extend(details)
        warnings.extend(asset_warnings)
        high_changes.extend(asset_high)
        primary_key_migrations.extend(asset_migrations)
        if name == "correction" and old_path is None:
            try:
                baseline_modes.append(correction_baseline_mode(new_path))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
                raise RuntimeError(f"invalid correction baseline: {exc}") from exc

    explanations, malformed_explanations = load_explanations(
        Path(args.explanations_db) if getattr(args, "explanations_db", None) else None,
        allowed_artifacts={ARTIFACT_IDS[name] for name, _, _ in assets},
    )
    by_identity: dict[str, list[dict[str, object]]] = {}
    for explanation in explanations:
        by_identity.setdefault(explanation_identity(explanation), []).append(explanation)
    consumed: list[str] = []
    duplicates: list[str] = []
    unexplained_changes: list[dict[str, object]] = []
    geometry_refreshes = accepted_geometry_refreshes(
        high_changes,
        assets,
        Path(args.geometry_refresh_report)
        if getattr(args, "geometry_refresh_report", None)
        else None,
    )
    accepted_geometry_identities = {
        explanation_identity(item) for item in geometry_refreshes
    }
    for change in high_changes:
        if explanation_identity(change) in accepted_geometry_identities:
            continue
        matches = by_identity.get(explanation_identity(change), [])
        if len(matches) == 1:
            consumed.append(str(matches[0]["effect_id"]))
        elif len(matches) > 1:
            duplicates.append(explanation_identity(change))
        else:
            unexplained_changes.append(change)
    unused = sorted(
        str(item["effect_id"]) for item in explanations
        if str(item["effect_id"]) not in set(consumed)
    )
    unexplained_high = [
        f"{item['artifact']}.{item['table']}: {item['change_kind']} "
        + "/".join(str(value) for _, value in item["primary_key"])
        for item in unexplained_changes
    ]

    review_rows: list[tuple[str, dict[str, str], bool]] = []
    resolution_rows: list[tuple[str, dict[str, str], bool]] = []
    clinical_asset = next(
        (asset for asset in assets if asset[0] == "clinical"), None
    )
    if clinical_asset:
        _, new_path, old_path = clinical_asset
        new_conn = sqlite3.connect(new_path)
        new_reviews = screening_reviews(new_conn)
        new_resolutions = screening_resolutions(new_conn)
        new_conn.close()
        old_reviews: dict[str, dict[str, str]] = {}
        old_resolutions: dict[str, dict[str, str]] = {}
        if old_path and old_path.exists():
            old_conn = sqlite3.connect(old_path)
            old_reviews = screening_reviews(old_conn)
            old_resolutions = screening_resolutions(old_conn)
            old_conn.close()
        for short_title, review in new_reviews.items():
            is_new = short_title not in old_reviews
            review_rows.append((short_title, review, is_new))
            if is_new:
                warnings.append(
                    "clinical screening review required: "
                    f"{short_title} ({review['label']}; {review['signal']})"
                )
        for short_title, resolution in new_resolutions.items():
            is_new = short_title not in old_resolutions
            resolution_rows.append((short_title, resolution, is_new))
            if is_new:
                warnings.append(
                    "clinical screening review resolved: "
                    f"{short_title} ({resolution['label']})"
                )

    lines = [
        "## TCIA SQLite change report",
        "",
        "Generated "
        + datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        + ". Additions are compared with the previously published release.",
        "",
        "| Asset | Table/view | Previous | New | Added | Removed | Modified | Severity |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in comparison_rows:
        previous = "baseline" if row["old"] is None else f"{row['old']:,}"
        lines.append(
            f"| {row['asset']} | `{row['table']}` | {previous} | "
            f"{row['new']:,} | {row['added']:,} | {row['removed']:,} | "
            f"{row['modified']:,} | {row['severity']} |"
        )
    if review_rows:
        lines.extend(
            [
                "",
                "### Clinical screening review queue",
                "",
                "| Dataset | New review | Cancer label | Signal | "
                "Imaging subjects | Inferred rows applied | "
                "Subjects suppressed |",
                "| --- | --- | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for short_title, review, is_new in review_rows:
            lines.append(
                f"| `{short_title}` | {'yes' if is_new else 'no'} | "
                f"{review['label']} | `{review['signal']}` | "
                f"{review['candidate_subjects']} | "
                f"{review['subjects_applied']} | "
                f"{review['subjects_suppressed']} |"
            )
    if resolution_rows:
        lines.extend(
            [
                "",
                "### Curated clinical screening resolutions",
                "",
                "| Dataset | New resolution | Cancer label | Resolution | "
                "Subjects inferred | Evidence |",
                "| --- | --- | --- | --- | ---: | --- |",
            ]
        )
        for short_title, resolution, is_new in resolution_rows:
            lines.append(
                f"| `{short_title}` | {'yes' if is_new else 'no'} | "
                f"{resolution['label']} | `{resolution['reason']}` | "
                f"{resolution['subjects_applied']} | "
                f"{resolution['evidence']} |"
            )
    if detail_blocks:
        lines.extend(["", "### Newly added identifiers", ""])
        lines.extend(block + "\n" for block in detail_blocks)
    if primary_key_migrations:
        lines.extend([
            "", "### One-to-one primary-key migrations", "",
            "Rows are paired only when every non-primary-key value is identical; "
            "these pairs remain gated semantic changes.", "",
        ])
        for migration in primary_key_migrations[:args.max_items]:
            old_key = format_key(value for _, value in migration["old_primary_key"])
            new_key = format_key(value for _, value in migration["new_primary_key"])
            lines.append(
                f"- `{migration['artifact']}.{migration['table']}`: "
                f"`{old_key}` -> `{new_key}`"
            )
    if baseline_modes:
        lines.extend(["", "### Component baseline modes", ""])
        for baseline in baseline_modes:
            lines.append(
                f"- `{baseline['component']}`: `{baseline['reason']}`; "
                f"`{baseline['gating_disposition']}`; prior "
                f"`{baseline['prior_release_fingerprint']}`"
            )
    if not warnings:
        lines.extend(["", "No monitored additions or new review flags."])
    if unexplained_high:
        lines.extend(["", "### Unexplained high-severity semantic changes", ""])
        lines.extend(f"- `{item}`" for item in unexplained_high)
    if geometry_refreshes:
        lines.extend(["", "### Accepted geometry refresh invalidations", ""])
        public_refreshes = sum(
            item.get("artifact") == "public_non_dicom"
            for item in geometry_refreshes
        )
        participant_refreshes = sum(
            item.get("artifact") == "participant_inventory"
            for item in geometry_refreshes
        )
        lines.append(
            f"- {public_refreshes:,} public asset rows and "
            f"{participant_refreshes:,} derived participant summary rows were "
            "safely reset in explicitly changed geometry scopes."
        )
    if malformed_explanations or duplicates or unused:
        lines.extend(["", "### Invalid or unused semantic explanations", ""])
        lines.extend(f"- malformed: `{item}`" for item in malformed_explanations)
        lines.extend(f"- duplicate match: `{item}`" for item in duplicates)
        lines.extend(f"- unused: `{item}`" for item in unused)
    semantic_payload: dict[str, object] = {
        "schema_version": 2,
        "comparisons": comparison_rows,
        "warnings": warnings,
        "unexplained_high_severity": unexplained_high,
        "semantic_changes": high_changes,
        "accepted_geometry_refreshes": geometry_refreshes,
        "primary_key_migrations": primary_key_migrations,
        "baseline_modes": baseline_modes,
        "semantic_explanations": {
            "consumed_effect_ids": sorted(consumed),
            "duplicate_matches": sorted(duplicates),
            "malformed_effects": sorted(malformed_explanations),
            "unused_effect_ids": unused,
        },
    }
    summary: dict[str, object] = {
        **semantic_payload,
        "generated_at_utc": getattr(args, "generated_at_utc", None)
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    summary["report_sha256"] = hashlib.sha256(
        json.dumps(semantic_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "\n".join(lines).rstrip() + "\n", warnings, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in PROFILES:
        parser.add_argument(f"--{name}-new")
        parser.add_argument(f"--{name}-old")
    parser.add_argument("--markdown-out")
    parser.add_argument("--json-out")
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--github-actions", action="store_true")
    parser.add_argument(
        "--geometry-refresh-report",
        help="Geometry seed comparison report used for strict safe-invalidation classification.",
    )
    parser.add_argument("--fail-on-unexplained-high", action="store_true")
    parser.add_argument(
        "--explanations-db",
        help="Correction registry containing exact approved semantic effects.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        markdown, warnings, summary = build_report(args)
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}")
        return 1
    print(markdown, end="")
    if args.markdown_out:
        path = Path(args.markdown_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.github_actions:
        for warning_text in warnings:
            print(
                "::warning title=TCIA metadata review::"
                + github_escape(warning_text)
            )
    explanation_errors = summary["semantic_explanations"]
    if args.fail_on_unexplained_high and (
        summary["unexplained_high_severity"]
        or explanation_errors["duplicate_matches"]
        or explanation_errors["malformed_effects"]
        or explanation_errors["unused_effect_ids"]
    ):
        print("error: semantic changes lack an exact one-to-one approved explanation")
        return 2
    eyeball = summary.get("report_sha256")
    print(f"report_sha256={eyeball}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
