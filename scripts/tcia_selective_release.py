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
FAST_MODE = "fast"
FULL_MODE = "full"

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


def prior_health_reuse_errors(manifest: dict[str, Any]) -> list[str]:
    components = ((manifest.get("source_health") or {}).get("components") or {})
    errors: list[str] = []
    now = dt.datetime.now(dt.timezone.utc)
    for component in sorted(CARRIED_COMPONENTS):
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
    if not reasons:
        manifest = json.loads(previous_manifest.read_text(encoding="utf-8"))
        previous_release_fingerprint = str(manifest.get("release_fingerprint") or "")
        previous_commit = str((manifest.get("producer") or {}).get("commit") or "")
        if not previous_release_fingerprint:
            reasons.append("previous bundle manifest has no release fingerprint")
        if len(previous_commit) != 40:
            reasons.append("previous bundle manifest has no exact producer commit")
        reasons.extend(prior_health_reuse_errors(manifest))

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
