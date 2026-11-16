#!/usr/bin/env python3
"""Plan and verify a fail-closed selective TCIA V2 release.

The fast path may carry expensive component artifacts forward only when the
snapshot projection available to their builders is byte-for-byte equivalent to
the preceding release and no changed producer file can affect those builders.
DataCite and TCIA EndNote publication tables are intentionally outside that
projection because they are served directly from the base snapshot.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


PLAN_SCHEMA_VERSION = 1
CONTROLLED_PLAN_SCHEMA_VERSION = 1
CLINICAL_PLAN_SCHEMA_VERSION = 1
PARTICIPANT_PLAN_SCHEMA_VERSION = 1
FAST_MODE = "fast"
FULL_MODE = "full"
REUSE_MODE = "reuse"
REBUILD_MODE = "rebuild"

SNAPSHOT_ONLY_TABLES = {
    "datacite_dois",
    "tcia_publications",
    "tcia_publication_dataset_dois",
}

CARRIED_COMPONENTS = {
    "clinical",
    "controlled_access",
    "participant_inventory",
    "participant_inventory_audit",
    "public_non_dicom",
    "public_non_dicom_audit",
}

FAST_SAFE_EXACT_PATHS = {
    ".github/workflows/build-metadata-v2-preview.yml",
    ".github/workflows/update-snapshot.yml",
    "skill_version.json",
    "scripts/tcia_metadata_change_report.py",
    "scripts/tcia_selective_release.py",
    "scripts/tcia_snapshot.py",
    "scripts/tcia_v2_bundle.py",
    "scripts/tests/test_tcia_metadata_change_report.py",
    "scripts/tests/test_tcia_release_workflow_contract.py",
    "scripts/tests/test_tcia_selective_release.py",
    "scripts/tests/test_tcia_snapshot.py",
    "scripts/tests/test_tcia_v2_bundle.py",
}

CONTROLLED_ACCESS_PRODUCER_PATHS = {
    "requirements-build.lock",
    "scripts/tcia_controlled_access_metadata.py",
}

CLINICAL_PRODUCER_PATHS = {
    "requirements-build.lock",
    "references/reviewed_analysis_result_participants_v1.csv",
    "scripts/tcia_clinical_metadata.py",
    "scripts/tcia_dicom_source_scope.py",
}

PARTICIPANT_PRODUCER_PATHS = {
    "requirements-build.lock",
    "references/clinical_only_identity_repairs_v1.json",
    "references/reviewed-participant-issue-dispositions.json",
    "references/reviewed_analysis_result_source_collections_v1.csv",
    "references/reviewed_missing_participant_inventories_v1.json",
    "references/reviewed_participant_identity_v1.json",
    "scripts/tcia_artifact_model.py",
    "scripts/tcia_clinical_metadata.py",
    "scripts/tcia_clinical_only_repairs.py",
    "scripts/tcia_missing_inventory_repairs.py",
    "scripts/tcia_participant_groups.py",
    "scripts/tcia_participant_inventory.py",
    "scripts/tcia_reviewed_identity.py",
    "scripts/tcia_v2_audit.py",
    "scripts/tcia_v2_staging.py",
}

PARTICIPANT_MANIFEST_DEPENDENCIES = (
    "controlled_access",
    "clinical",
)

CONTROLLED_ACCESS_PROJECTION_COLUMNS = (
    "download_row_id", "parent_source", "dataset_type", "parent_id",
    "parent_slug", "short_title", "title", "doi", "source_collections",
    "download_id", "download_slug", "download_title", "download_url",
    "download_metadata", "search_url", "date_updated", "collection_status",
    "description", "license_label", "license_url", "requirements_label",
    "requirements_url", "requirements_text", "access_level",
    "controlled_access", "noncommercial_license", "download_size",
    "download_size_unit", "subjects", "studies", "series", "images",
    "download_types", "data_types", "file_types", "external_resources",
    "raw_json",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _encoded_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, bytes):
        return {"type": "blob", "value": value.hex()}
    if isinstance(value, int):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        return {"type": "real", "value": repr(value)}
    return {"type": "text", "value": str(value)}


def _snapshot_only_object(name: str, sql: str) -> bool:
    if name == "snapshot_meta" or name in SNAPSHOT_ONLY_TABLES:
        return True
    lowered = sql.casefold()
    return any(table.casefold() in lowered for table in SNAPSHOT_ONLY_TABLES)


def downstream_snapshot_fingerprint(path: Path) -> str:
    """Hash the exact snapshot schema/data visible to expensive builders."""
    uri = f"file:{path.resolve()}?mode=ro"
    digest = hashlib.sha256()
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"Snapshot quick_check failed for {path}: {quick_check}")
        objects = conn.execute(
            """
            SELECT type, name, COALESCE(sql, '') AS sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        included_tables: list[str] = []
        for row in objects:
            name = str(row["name"])
            sql = str(row["sql"] or "")
            if _snapshot_only_object(name, sql):
                continue
            digest.update(
                (canonical_json({"type": row["type"], "name": name, "sql": sql}) + "\n").encode(
                    "utf-8"
                )
            )
            if row["type"] == "table":
                included_tables.append(name)

        for table in included_tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            if not columns:
                raise RuntimeError(f"Snapshot table has no columns: {table}")
            order_by = ", ".join(str(index) for index in range(1, len(columns) + 1))
            digest.update((canonical_json({"table": table, "columns": [dict(row) for row in columns]}) + "\n").encode("utf-8"))
            for row in conn.execute(f"SELECT * FROM {quoted} ORDER BY {order_by}"):
                digest.update(
                    (canonical_json([_encoded_value(value) for value in row]) + "\n").encode(
                        "utf-8"
                    )
                )
    return digest.hexdigest()


def path_is_fast_safe(path: str) -> bool:
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        return False
    if normalized in FAST_SAFE_EXACT_PATHS:
        return True
    if normalized.endswith(".md"):
        return True
    return normalized.startswith(("mcp_server/", "evals/"))


def prior_health_reuse_errors(
    manifest: dict[str, Any], components_to_check: Iterable[str] = CARRIED_COMPONENTS,
) -> list[str]:
    components = ((manifest.get("source_health") or {}).get("components") or {})
    errors: list[str] = []
    now = dt.datetime.now(dt.timezone.utc)
    for component in sorted(components_to_check):
        health = components.get(component)
        if not isinstance(health, dict) or health.get("status") != "healthy":
            errors.append(f"prior {component} source health is not reusable")
            continue
        sources = health.get("sources")
        if not isinstance(sources, dict) or not sources:
            errors.append(f"prior {component} source health has no sources")
            continue
        for name, details in sources.items():
            if not isinstance(details, dict) or details.get("status") != "healthy":
                errors.append(f"prior source is not healthy: {name}")
                continue
            if details.get("freshness") == "verified_stale":
                try:
                    observed = dt.datetime.fromisoformat(
                        str(details.get("last_successful_at_utc") or "").replace("Z", "+00:00")
                    )
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=dt.timezone.utc)
                    max_age = int(details.get("max_age_seconds"))
                except (TypeError, ValueError):
                    errors.append(f"prior verified-stale evidence is malformed: {name}")
                    continue
                age_seconds = int((now - observed.astimezone(dt.timezone.utc)).total_seconds())
                if age_seconds < 0 or age_seconds > max_age:
                    errors.append(f"prior verified-stale evidence expired: {name}")
    return errors


def controlled_access_projection_fingerprint(path: Path) -> str:
    """Hash WordPress rows that define the controlled-access build scope."""
    uri = f"file:{path.resolve()}?mode=ro"
    digest = hashlib.sha256()
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"Snapshot quick_check failed for {path}: {quick_check}")
        required = {"agent_current_downloads", "agent_datasets"}
        present = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(
                "Controlled-access projection views are missing: " + ", ".join(missing)
            )
        select_columns = []
        for column in CONTROLLED_ACCESS_PROJECTION_COLUMNS:
            if column in {"doi", "source_collections"}:
                select_columns.append(f"a.{column} AS {column}")
            else:
                select_columns.append(f"d.{column} AS {column}")
        sql = f"""
            SELECT {', '.join(select_columns)}
            FROM agent_current_downloads d
            LEFT JOIN agent_datasets a
              ON a.source = d.parent_source
             AND a.short_title = d.short_title
            WHERE d.hidden = 0
              AND d.controlled_access = 1
            ORDER BY lower(d.short_title), d.download_id, d.download_row_id
        """
        digest.update(
            (canonical_json({"columns": CONTROLLED_ACCESS_PROJECTION_COLUMNS}) + "\n").encode(
                "utf-8"
            )
        )
        for row in conn.execute(sql):
            digest.update(
                (canonical_json([_encoded_value(value) for value in row]) + "\n").encode(
                    "utf-8"
                )
            )
    return digest.hexdigest()


def build_controlled_access_plan(
    current_db: Path,
    previous_db: Path | None,
    previous_manifest: Path | None,
    repository_root: Path,
    current_commit: str,
    *,
    changed_paths: Iterable[str] | None = None,
    force_rebuild_reasons: Iterable[str] = (),
) -> dict[str, Any]:
    reasons = [str(reason) for reason in force_rebuild_reasons if str(reason).strip()]
    current_fingerprint = controlled_access_projection_fingerprint(current_db)
    previous_fingerprint = ""
    previous_commit = ""
    previous_release_fingerprint = ""
    manifest: dict[str, Any] = {}

    if not previous_db or not previous_db.is_file():
        reasons.append("verified previous snapshot is unavailable")
    else:
        previous_fingerprint = controlled_access_projection_fingerprint(previous_db)
        if previous_fingerprint != current_fingerprint:
            reasons.append("WordPress controlled-access projection changed")

    if not previous_manifest or not previous_manifest.is_file():
        reasons.append("verified previous bundle manifest is unavailable")
    else:
        manifest = json.loads(previous_manifest.read_text(encoding="utf-8"))
        previous_release_fingerprint = str(manifest.get("release_fingerprint") or "")
        previous_commit = str((manifest.get("producer") or {}).get("commit") or "")
        if not previous_release_fingerprint:
            reasons.append("previous bundle manifest has no release fingerprint")
        if len(previous_commit) != 40:
            reasons.append("previous bundle manifest has no exact producer commit")
        reasons.extend(prior_health_reuse_errors(manifest, {"controlled_access"}))

    paths: list[str] = []
    producer_paths: list[str] = []
    if len(previous_commit) == 40:
        try:
            paths = sorted(
                set(changed_paths)
                if changed_paths is not None
                else git_changed_paths(repository_root, previous_commit, current_commit)
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            reasons.append(f"controlled-access producer impact could not be established: {exc}")
        else:
            producer_paths = [
                path for path in paths if path in CONTROLLED_ACCESS_PRODUCER_PATHS
            ]
            if producer_paths:
                reasons.append("controlled-access producer files changed")

    mode = REBUILD_MODE if reasons else REUSE_MODE
    return {
        "artifact": "tcia_controlled_access_release_plan",
        "schema_version": CONTROLLED_PLAN_SCHEMA_VERSION,
        "mode": mode,
        "eligible": mode == REUSE_MODE,
        "current_producer_commit": current_commit,
        "previous_producer_commit": previous_commit,
        "previous_release_fingerprint": previous_release_fingerprint,
        "wordpress_projection_fingerprint": {
            "current": current_fingerprint,
            "previous": previous_fingerprint,
            "algorithm": "sha256-controlled-wordpress-projection-v1",
        },
        "changed_paths": paths,
        "controlled_producer_paths": producer_paths,
        "reasons": reasons or ["WordPress controlled-access scope and producer are unchanged"],
    }


def _manifest_release_fingerprint(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    value = str(payload.get("release_fingerprint") or "")
    return value if len(value) == 64 else ""


def _component_release_fingerprint(manifest: dict[str, Any], component: str) -> str:
    value = str(((manifest.get("components") or {}).get(component) or {}).get(
        "release_fingerprint"
    ) or "")
    return value if len(value) == 64 else ""


def _manifest_age_error(
    manifest: dict[str, Any], *, max_age_hours: int, now: dt.datetime | None = None,
) -> str:
    try:
        generated = dt.datetime.fromisoformat(
            str(manifest.get("generated_at_utc") or "").replace("Z", "+00:00")
        )
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return "previous bundle manifest has no valid generation time"
    observed = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    age_seconds = (observed - generated.astimezone(dt.timezone.utc)).total_seconds()
    if age_seconds < 0:
        return "previous bundle manifest generation time is in the future"
    if age_seconds > max_age_hours * 3600:
        return f"previous clinical observation is older than {max_age_hours} hours"
    return ""


def build_clinical_plan(
    current_db: Path,
    previous_db: Path | None,
    current_controlled_manifest: Path | None,
    previous_manifest: Path | None,
    repository_root: Path,
    current_commit: str,
    *,
    changed_paths: Iterable[str] | None = None,
    force_rebuild_reasons: Iterable[str] = (),
    max_prior_age_hours: int = 26,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Plan bounded clinical reuse between authoritative scheduled observations."""
    reasons = [str(reason) for reason in force_rebuild_reasons if str(reason).strip()]
    current_fingerprint = downstream_snapshot_fingerprint(current_db)
    previous_fingerprint = ""
    previous_commit = ""
    previous_release_fingerprint = ""
    current_controlled_fingerprint = ""
    previous_controlled_fingerprint = ""
    manifest: dict[str, Any] = {}

    if not previous_db or not previous_db.is_file():
        reasons.append("verified previous snapshot is unavailable")
    else:
        previous_fingerprint = downstream_snapshot_fingerprint(previous_db)
        if previous_fingerprint != current_fingerprint:
            reasons.append("clinical snapshot projection changed")

    if not current_controlled_manifest or not current_controlled_manifest.is_file():
        reasons.append("current controlled-access component manifest is unavailable")
    else:
        current_controlled_fingerprint = _manifest_release_fingerprint(
            current_controlled_manifest
        )
        if not current_controlled_fingerprint:
            reasons.append("current controlled-access component fingerprint is invalid")

    if not previous_manifest or not previous_manifest.is_file():
        reasons.append("verified previous bundle manifest is unavailable")
    else:
        manifest = json.loads(previous_manifest.read_text(encoding="utf-8"))
        previous_release_fingerprint = str(manifest.get("release_fingerprint") or "")
        previous_commit = str((manifest.get("producer") or {}).get("commit") or "")
        previous_controlled_fingerprint = _component_release_fingerprint(
            manifest, "controlled_access"
        )
        if not previous_release_fingerprint:
            reasons.append("previous bundle manifest has no release fingerprint")
        if len(previous_commit) != 40:
            reasons.append("previous bundle manifest has no exact producer commit")
        if not previous_controlled_fingerprint:
            reasons.append("previous controlled-access component fingerprint is invalid")
        elif (
            current_controlled_fingerprint
            and current_controlled_fingerprint != previous_controlled_fingerprint
        ):
            reasons.append("controlled-access dependency changed")
        reasons.extend(prior_health_reuse_errors(manifest, {"clinical"}))
        age_error = _manifest_age_error(
            manifest, max_age_hours=max_prior_age_hours, now=now
        )
        if age_error:
            reasons.append(age_error)

    paths: list[str] = []
    producer_paths: list[str] = []
    if len(previous_commit) == 40:
        try:
            paths = sorted(
                set(changed_paths)
                if changed_paths is not None
                else git_changed_paths(repository_root, previous_commit, current_commit)
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            reasons.append(f"clinical producer impact could not be established: {exc}")
        else:
            producer_paths = [path for path in paths if path in CLINICAL_PRODUCER_PATHS]
            if producer_paths:
                reasons.append("clinical producer files changed")

    mode = REBUILD_MODE if reasons else REUSE_MODE
    return {
        "artifact": "tcia_clinical_release_plan",
        "schema_version": CLINICAL_PLAN_SCHEMA_VERSION,
        "mode": mode,
        "eligible": mode == REUSE_MODE,
        "current_producer_commit": current_commit,
        "previous_producer_commit": previous_commit,
        "previous_release_fingerprint": previous_release_fingerprint,
        "max_prior_age_hours": max_prior_age_hours,
        "snapshot_projection_fingerprint": {
            "current": current_fingerprint,
            "previous": previous_fingerprint,
            "algorithm": "sha256-canonical-sqlite-v1",
        },
        "controlled_access_release_fingerprint": {
            "current": current_controlled_fingerprint,
            "previous": previous_controlled_fingerprint,
        },
        "changed_paths": paths,
        "clinical_producer_paths": producer_paths,
        "reasons": reasons or [
            "clinical dependencies are unchanged within the bounded observation window"
        ],
    }


def idc_participant_projection_fingerprint(path: Path) -> str:
    """Hash IDC participant content while excluding observation timestamps."""
    digest = hashlib.sha256()
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(f"IDC participant projection quick_check failed: {path}")
        required = {
            "idc_participant_projection_meta",
            "idc_dataset_participants",
            "idc_unmatched_datasets",
        }
        present = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(
                "IDC participant projection tables are missing: " + ", ".join(missing)
            )
        meta = {
            str(key): str(value)
            for key, value in conn.execute(
                "SELECT key, value FROM idc_participant_projection_meta "
                "WHERE key <> 'generated_at_utc' ORDER BY key"
            )
        }
        digest.update((canonical_json({"meta": meta}) + "\n").encode("utf-8"))
        for table in ("idc_dataset_participants", "idc_unmatched_datasets"):
            quoted = '"' + table.replace('"', '""') + '"'
            columns = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            order_by = ", ".join(str(index) for index in range(1, len(columns) + 1))
            digest.update(
                (canonical_json({"table": table, "columns": [dict(row) for row in columns]}) + "\n").encode("utf-8")
            )
            for row in conn.execute(f"SELECT * FROM {quoted} ORDER BY {order_by}"):
                digest.update(
                    (canonical_json([_encoded_value(value) for value in row]) + "\n").encode(
                        "utf-8"
                    )
                )
    return digest.hexdigest()


def filtered_sqlite_fingerprint(
    path: Path,
    *,
    excluded_tables: set[str] | None = None,
    included_tables: set[str] | None = None,
) -> str:
    """Hash selected SQLite schema and rows without transient build metadata."""
    excluded = excluded_tables or set()
    digest = hashlib.sha256()
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(f"SQLite dependency quick_check failed: {path}")
        objects = conn.execute(
            "SELECT type, name, COALESCE(sql, '') AS sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        tables: list[str] = []
        for row in objects:
            name = str(row["name"])
            if name in excluded:
                continue
            if included_tables is not None and name not in included_tables:
                continue
            digest.update(
                (canonical_json({"type": row["type"], "name": name, "sql": row["sql"]}) + "\n").encode(
                    "utf-8"
                )
            )
            if row["type"] == "table":
                tables.append(name)
        if included_tables is not None and set(tables) != included_tables:
            missing = sorted(included_tables - set(tables))
            raise RuntimeError("SQLite dependency tables are missing: " + ", ".join(missing))
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            order_by = ", ".join(str(index) for index in range(1, len(columns) + 1))
            digest.update(
                (canonical_json({"table": table, "columns": [dict(row) for row in columns]}) + "\n").encode(
                    "utf-8"
                )
            )
            for row in conn.execute(f"SELECT * FROM {quoted} ORDER BY {order_by}"):
                digest.update(
                    (canonical_json([_encoded_value(value) for value in row]) + "\n").encode(
                        "utf-8"
                    )
                )
    return digest.hexdigest()


def participant_snapshot_projection_fingerprint(path: Path) -> str:
    """Hash only visible WordPress rows consulted by Participant Inventory."""
    digest = hashlib.sha256()
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(f"Participant snapshot quick_check failed: {path}")
        for name in ("agent_datasets", "agent_current_downloads"):
            quoted = '"' + name.replace('"', '""') + '"'
            columns = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            if not columns:
                raise RuntimeError(f"Participant snapshot view is missing: {name}")
            column_names = {str(row["name"]) for row in columns}
            visible = " WHERE COALESCE(hidden, 0) = 0" if "hidden" in column_names else ""
            order_by = ", ".join(str(index) for index in range(1, len(columns) + 1))
            digest.update(
                (canonical_json({"object": name, "columns": [dict(row) for row in columns]}) + "\n").encode(
                    "utf-8"
                )
            )
            for row in conn.execute(f"SELECT * FROM {quoted}{visible} ORDER BY {order_by}"):
                digest.update(
                    (canonical_json([_encoded_value(value) for value in row]) + "\n").encode(
                        "utf-8"
                    )
                )
    return digest.hexdigest()


def participant_dependency_fingerprint(
    component_manifests: dict[str, Path],
    snapshot_db: Path,
    public_db: Path,
    public_audit_db: Path,
    idc_db: Path,
) -> tuple[str, dict[str, str]]:
    fingerprints = {
        "snapshot_projection": participant_snapshot_projection_fingerprint(snapshot_db),
        "public_non_dicom_projection": filtered_sqlite_fingerprint(
            public_db, excluded_tables={"artifact_meta"}
        ),
        "public_non_dicom_audit_projection": filtered_sqlite_fingerprint(
            public_audit_db,
            included_tables={"public_non_dicom_crosswalk_evidence"},
        ),
    }
    for component in PARTICIPANT_MANIFEST_DEPENDENCIES:
        path = component_manifests.get(component)
        if path is None or not path.is_file():
            raise RuntimeError(f"participant dependency manifest is unavailable: {component}")
        fingerprint = _manifest_release_fingerprint(path)
        if not fingerprint:
            raise RuntimeError(f"participant dependency fingerprint is invalid: {component}")
        fingerprints[component] = fingerprint
    fingerprints["idc_participant_projection"] = idc_participant_projection_fingerprint(
        idc_db
    )
    return (
        hashlib.sha256(canonical_json(fingerprints).encode("utf-8")).hexdigest(),
        fingerprints,
    )


def _participant_previous_dependency_fingerprint(path: Path) -> str:
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(f"Previous participant inventory quick_check failed: {path}")
        row = conn.execute(
            "SELECT value FROM participant_inventory_meta "
            "WHERE key='dependency_fingerprint'"
        ).fetchone()
    value = str(row[0] if row else "")
    return value if len(value) == 64 else ""


def build_participant_plan(
    component_manifests: dict[str, Path],
    snapshot_db: Path,
    public_db: Path,
    public_audit_db: Path,
    idc_db: Path,
    previous_participant_db: Path | None,
    previous_manifest: Path | None,
    repository_root: Path,
    current_commit: str,
    *,
    changed_paths: Iterable[str] | None = None,
    force_rebuild_reasons: Iterable[str] = (),
) -> dict[str, Any]:
    reasons = [str(reason) for reason in force_rebuild_reasons if str(reason).strip()]
    current_fingerprint, dependencies = participant_dependency_fingerprint(
        component_manifests, snapshot_db, public_db, public_audit_db, idc_db
    )
    previous_fingerprint = ""
    previous_commit = ""
    previous_release_fingerprint = ""
    manifest: dict[str, Any] = {}

    if not previous_participant_db or not previous_participant_db.is_file():
        reasons.append("verified previous participant inventory is unavailable")
    else:
        previous_fingerprint = _participant_previous_dependency_fingerprint(
            previous_participant_db
        )
        if not previous_fingerprint:
            reasons.append("previous participant dependency fingerprint is unavailable")
        elif previous_fingerprint != current_fingerprint:
            reasons.append("participant dependencies changed")

    if not previous_manifest or not previous_manifest.is_file():
        reasons.append("verified previous bundle manifest is unavailable")
    else:
        manifest = json.loads(previous_manifest.read_text(encoding="utf-8"))
        previous_release_fingerprint = str(manifest.get("release_fingerprint") or "")
        previous_commit = str((manifest.get("producer") or {}).get("commit") or "")
        if not previous_release_fingerprint:
            reasons.append("previous bundle manifest has no release fingerprint")
        if len(previous_commit) != 40:
            reasons.append("previous bundle manifest has no exact producer commit")
        reasons.extend(
            prior_health_reuse_errors(
                manifest, {"participant_inventory", "participant_inventory_audit"}
            )
        )

    paths: list[str] = []
    producer_paths: list[str] = []
    if len(previous_commit) == 40:
        try:
            paths = sorted(
                set(changed_paths)
                if changed_paths is not None
                else git_changed_paths(repository_root, previous_commit, current_commit)
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            reasons.append(f"participant producer impact could not be established: {exc}")
        else:
            producer_paths = [path for path in paths if path in PARTICIPANT_PRODUCER_PATHS]
            if producer_paths:
                reasons.append("participant producer files changed")

    mode = REBUILD_MODE if reasons else REUSE_MODE
    return {
        "artifact": "tcia_participant_release_plan",
        "schema_version": PARTICIPANT_PLAN_SCHEMA_VERSION,
        "mode": mode,
        "eligible": mode == REUSE_MODE,
        "current_producer_commit": current_commit,
        "previous_producer_commit": previous_commit,
        "previous_release_fingerprint": previous_release_fingerprint,
        "dependency_fingerprint": {
            "current": current_fingerprint,
            "previous": previous_fingerprint,
            "algorithm": "sha256-participant-dependencies-v1",
        },
        "dependencies": dependencies,
        "changed_paths": paths,
        "participant_producer_paths": producer_paths,
        "reasons": reasons or ["participant dependencies and producer are unchanged"],
    }


def git_changed_paths(repository_root: Path, previous_commit: str, current_commit: str) -> list[str]:
    for commit in (previous_commit, current_commit):
        subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=repository_root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{previous_commit}..{current_commit}"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def requires_manifest_refresh(snapshot_db: Path) -> bool:
    """Unversioned external manifest bytes are not proved by snapshot parity."""
    with closing(sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_current_downloads)")}
        if not {"parent_source", "hidden", "download_url"}.issubset(columns):
            return False
        return conn.execute(
            "SELECT 1 FROM agent_current_downloads WHERE parent_source='analysis-results' "
            "AND hidden=0 AND lower(download_url) LIKE '%.tcia%' LIMIT 1"
        ).fetchone() is not None


def build_plan(
    current_db: Path,
    previous_db: Path | None,
    previous_manifest: Path | None,
    repository_root: Path,
    current_commit: str,
    *,
    changed_paths: Iterable[str] | None = None,
    force_full_reasons: Iterable[str] = (),
) -> dict[str, Any]:
    reasons: list[str] = [str(reason) for reason in force_full_reasons if str(reason).strip()]
    current_fingerprint = downstream_snapshot_fingerprint(current_db)
    previous_fingerprint = ""
    previous_release_fingerprint = ""
    previous_commit = ""

    if not previous_db or not previous_db.is_file():
        reasons.append("verified previous snapshot is unavailable")
    if not previous_manifest or not previous_manifest.is_file():
        reasons.append("verified previous bundle manifest is unavailable")

    manifest: dict[str, Any] = {}
    if previous_manifest and previous_manifest.is_file():
        manifest = json.loads(previous_manifest.read_text(encoding="utf-8"))
        previous_release_fingerprint = str(manifest.get("release_fingerprint") or "")
        previous_commit = str((manifest.get("producer") or {}).get("commit") or "")
        if not previous_release_fingerprint:
            reasons.append("previous bundle manifest has no release fingerprint")
        if len(previous_commit) != 40:
            reasons.append("previous bundle manifest has no exact producer commit")
        reasons.extend(prior_health_reuse_errors(manifest))

    if requires_manifest_refresh(current_db):
        reasons.append("external DICOM manifest content requires fresh source resolution")

    if previous_db and previous_db.is_file():
        previous_fingerprint = downstream_snapshot_fingerprint(previous_db)
        if previous_fingerprint != current_fingerprint:
            reasons.append("downstream snapshot projection changed")

    paths: list[str] = []
    disqualifying_paths: list[str] = []
    if previous_commit and len(previous_commit) == 40:
        try:
            paths = sorted(
                set(changed_paths)
                if changed_paths is not None
                else git_changed_paths(repository_root, previous_commit, current_commit)
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            reasons.append(f"producer code impact could not be established: {exc}")
        else:
            disqualifying_paths = [path for path in paths if not path_is_fast_safe(path)]
            if disqualifying_paths:
                reasons.append("changed producer files can affect expensive components")

    mode = FAST_MODE if not reasons else FULL_MODE
    return {
        "artifact": "tcia_selective_release_plan",
        "schema_version": PLAN_SCHEMA_VERSION,
        "mode": mode,
        "eligible": mode == FAST_MODE,
        "current_producer_commit": current_commit,
        "previous_producer_commit": previous_commit,
        "previous_release_fingerprint": previous_release_fingerprint,
        "downstream_snapshot_fingerprint": {
            "current": current_fingerprint,
            "previous": previous_fingerprint,
            "algorithm": "sha256-canonical-sqlite-v1",
        },
        "changed_paths": paths,
        "disqualifying_paths": disqualifying_paths,
        "reasons": reasons or ["all expensive-component inputs are unchanged"],
    }


def verify_plan(
    plan_path: Path,
    current_db: Path,
    previous_manifest: Path,
    expected_producer_commit: str,
    repository_root: Path,
    *,
    changed_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if plan.get("artifact") != "tcia_selective_release_plan":
        errors.append("unknown selective release plan artifact")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        errors.append("unsupported selective release plan schema")
    if plan.get("mode") not in {FAST_MODE, FULL_MODE}:
        errors.append("invalid selective release mode")
    if plan.get("eligible") != (plan.get("mode") == FAST_MODE):
        errors.append("selective release eligibility disagrees with mode")
    if plan.get("current_producer_commit") != expected_producer_commit:
        errors.append("selective release plan producer commit mismatch")
    current = downstream_snapshot_fingerprint(current_db)
    expected_current = (plan.get("downstream_snapshot_fingerprint") or {}).get("current")
    if current != expected_current:
        errors.append("selective release plan snapshot fingerprint mismatch")
    previous = json.loads(previous_manifest.read_text(encoding="utf-8"))
    if previous.get("release_fingerprint") != plan.get("previous_release_fingerprint"):
        errors.append("selective release plan prior release fingerprint mismatch")
    previous_commit = str((previous.get("producer") or {}).get("commit") or "")
    if previous_commit != plan.get("previous_producer_commit"):
        errors.append("selective release plan prior producer commit mismatch")
    try:
        actual_paths = sorted(
            set(changed_paths)
            if changed_paths is not None
            else git_changed_paths(repository_root, previous_commit, expected_producer_commit)
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        errors.append(f"selective release producer paths could not be verified: {exc}")
        actual_paths = []
    if actual_paths != plan.get("changed_paths"):
        errors.append("selective release changed paths mismatch")
    actual_disqualifying = [path for path in actual_paths if not path_is_fast_safe(path)]
    if actual_disqualifying != plan.get("disqualifying_paths"):
        errors.append("selective release disqualifying paths mismatch")
    if plan.get("mode") == FAST_MODE:
        if requires_manifest_refresh(current_db):
            errors.append("fast plan cannot prove unchanged external DICOM manifests")
        projection = plan.get("downstream_snapshot_fingerprint") or {}
        if projection.get("current") != projection.get("previous"):
            errors.append("fast plan has unequal downstream snapshot fingerprints")
        if plan.get("disqualifying_paths"):
            errors.append("fast plan contains disqualifying producer paths")
    if errors:
        raise RuntimeError("; ".join(errors))
    return {"ok": True, "mode": plan["mode"], "snapshot_fingerprint": current}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--current-db", type=Path, required=True)
    plan.add_argument("--previous-db", type=Path)
    plan.add_argument("--previous-manifest", type=Path)
    plan.add_argument("--repository-root", type=Path, default=Path.cwd())
    plan.add_argument("--current-commit", required=True)
    plan.add_argument("--force-full-reason", action="append", default=[])
    plan.add_argument("--out", type=Path, required=True)

    controlled = subparsers.add_parser("plan-controlled")
    controlled.add_argument("--current-db", type=Path, required=True)
    controlled.add_argument("--previous-db", type=Path)
    controlled.add_argument("--previous-manifest", type=Path)
    controlled.add_argument("--repository-root", type=Path, default=Path.cwd())
    controlled.add_argument("--current-commit", required=True)
    controlled.add_argument("--force-rebuild-reason", action="append", default=[])
    controlled.add_argument("--out", type=Path, required=True)

    clinical = subparsers.add_parser("plan-clinical")
    clinical.add_argument("--current-db", type=Path, required=True)
    clinical.add_argument("--previous-db", type=Path)
    clinical.add_argument("--current-controlled-manifest", type=Path)
    clinical.add_argument("--previous-manifest", type=Path)
    clinical.add_argument("--repository-root", type=Path, default=Path.cwd())
    clinical.add_argument("--current-commit", required=True)
    clinical.add_argument("--force-rebuild-reason", action="append", default=[])
    clinical.add_argument("--max-prior-age-hours", type=int, default=26)
    clinical.add_argument("--out", type=Path, required=True)

    participant = subparsers.add_parser("plan-participant")
    for component in PARTICIPANT_MANIFEST_DEPENDENCIES:
        participant.add_argument(
            "--" + component.replace("_", "-") + "-manifest",
            dest=component + "_manifest",
            type=Path,
            required=True,
        )
    participant.add_argument("--snapshot-db", type=Path, required=True)
    participant.add_argument("--public-non-dicom-db", type=Path, required=True)
    participant.add_argument("--public-non-dicom-audit-db", type=Path, required=True)
    participant.add_argument("--idc-db", type=Path, required=True)
    participant.add_argument("--previous-participant-db", type=Path)
    participant.add_argument("--previous-manifest", type=Path)
    participant.add_argument("--repository-root", type=Path, default=Path.cwd())
    participant.add_argument("--current-commit", required=True)
    participant.add_argument("--force-rebuild-reason", action="append", default=[])
    participant.add_argument("--out", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--current-db", type=Path, required=True)
    verify.add_argument("--previous-manifest", type=Path, required=True)
    verify.add_argument("--expected-producer-commit", required=True)
    verify.add_argument("--repository-root", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "plan":
        payload = build_plan(
            args.current_db,
            args.previous_db,
            args.previous_manifest,
            args.repository_root,
            args.current_commit,
            force_full_reasons=args.force_full_reason,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    elif args.command == "plan-controlled":
        payload = build_controlled_access_plan(
            args.current_db,
            args.previous_db,
            args.previous_manifest,
            args.repository_root,
            args.current_commit,
            force_rebuild_reasons=args.force_rebuild_reason,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    elif args.command == "plan-clinical":
        payload = build_clinical_plan(
            args.current_db,
            args.previous_db,
            args.current_controlled_manifest,
            args.previous_manifest,
            args.repository_root,
            args.current_commit,
            force_rebuild_reasons=args.force_rebuild_reason,
            max_prior_age_hours=args.max_prior_age_hours,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command == "plan-participant":
        payload = build_participant_plan(
            {
                component: getattr(args, component + "_manifest")
                for component in PARTICIPANT_MANIFEST_DEPENDENCIES
            },
            args.snapshot_db,
            args.public_non_dicom_db,
            args.public_non_dicom_audit_db,
            args.idc_db,
            args.previous_participant_db,
            args.previous_manifest,
            args.repository_root,
            args.current_commit,
            force_rebuild_reasons=args.force_rebuild_reason,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    else:
        payload = verify_plan(
            args.plan,
            args.current_db,
            args.previous_manifest,
            args.expected_producer_commit,
            args.repository_root,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
