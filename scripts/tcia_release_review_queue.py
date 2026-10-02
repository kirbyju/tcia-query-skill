#!/usr/bin/env python3
"""Group unexplained semantic changes into dataset-scoped review work."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def change_digest(changes: list[dict[str, object]]) -> str:
    identities = sorted(canonical_json(change) for change in changes)
    return hashlib.sha256(canonical_json(identities).encode("utf-8")).hexdigest()


def primary_key(change: dict[str, object]) -> tuple[str, str]:
    values = change.get("primary_key")
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError("review queue supports one-column semantic primary keys")
    pair = values[0]
    if not isinstance(pair, list) or len(pair) != 2:
        raise ValueError("semantic primary key must be [column,value]")
    return str(pair[0]), str(pair[1])


def query_scopes(
    path: Path,
    *,
    artifact: str,
    table: str,
    key_column: str,
    key_value: str,
) -> set[tuple[str, str]]:
    if not path.is_file():
        return set()
    direct_public = {
        "public_non_dicom_assets",
        "public_non_dicom_crosswalk_decisions",
    }
    asset_child_public = {
        "public_non_dicom_asset_participants",
        "public_non_dicom_crosswalk_evidence",
        "public_non_dicom_image_metadata",
    }
    short_title_public = {
        "public_non_dicom_review_issues",
        "public_non_dicom_dataset_metadata_notes",
    }
    participant_children = {
        "participant_identifiers",
        "participant_assets",
        "participant_identity_evidence",
        "participant_source_links",
    }
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        if artifact == "public_non_dicom" and table in direct_public:
            columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
            if {"dataset_type", "short_title"}.issubset(columns):
                rows = conn.execute(
                    f"SELECT dataset_type,short_title FROM {table} WHERE {key_column}=?",
                    (key_value,),
                )
            elif "short_title" in columns:
                rows = conn.execute(
                    f"SELECT '',short_title FROM {table} WHERE {key_column}=?",
                    (key_value,),
                )
            else:
                return set()
        elif artifact == "public_non_dicom" and table in asset_child_public:
            rows = conn.execute(
                f"SELECT a.dataset_type,t.short_title FROM {table} t "
                "LEFT JOIN public_non_dicom_assets a ON a.asset_id=t.asset_id "
                f"WHERE t.{key_column}=?",
                (key_value,),
            )
        elif artifact == "public_non_dicom" and table in short_title_public:
            rows = conn.execute(
                f"SELECT DISTINCT a.dataset_type,t.short_title FROM {table} t "
                "LEFT JOIN public_non_dicom_assets a "
                "ON a.short_title=t.short_title "
                f"WHERE t.{key_column}=?",
                (key_value,),
            )
        elif artifact == "participant_inventory" and table == "participants":
            rows = conn.execute(
                "SELECT dataset_type,short_title FROM participants "
                f"WHERE {key_column}=?",
                (key_value,),
            )
        elif artifact == "participant_inventory" and table in participant_children:
            participant_column = (
                "analysis_result_participant_key"
                if table == "participant_source_links" else "participant_key"
            )
            rows = conn.execute(
                f"SELECT p.dataset_type,p.short_title FROM {table} t "
                f"JOIN participants p ON p.participant_key=t.{participant_column} "
                f"WHERE t.{key_column}=?",
                (key_value,),
            )
        elif artifact == "participant_inventory" and table in {
            "dataset_assets_without_participant_crosswalk", "participant_link_issues"
        }:
            rows = conn.execute(
                f"SELECT dataset_type,short_title FROM {table} WHERE {key_column}=?",
                (key_value,),
            )
        else:
            return set()
        return {
            (str(dataset_type or "").strip(), str(short_title or "").strip())
            for dataset_type, short_title in rows
            if str(short_title or "").strip()
        }


def resolve_scope(
    change: dict[str, object],
    databases: dict[tuple[str, str], Path],
) -> tuple[str, str] | None:
    artifact = str(change.get("artifact") or "")
    table = str(change.get("table") or "")
    kind = str(change.get("change_kind") or "")
    key_column, key_value = primary_key(change)
    preferred = "old" if kind == "removed" else "new"
    scopes = query_scopes(
        databases[(artifact, preferred)], artifact=artifact, table=table,
        key_column=key_column, key_value=key_value,
    )
    if not scopes:
        fallback = "new" if preferred == "old" else "old"
        scopes = query_scopes(
            databases[(artifact, fallback)], artifact=artifact, table=table,
            key_column=key_column, key_value=key_value,
        )
    if len(scopes) != 1:
        return None
    return next(iter(scopes))


def build_queue(args: argparse.Namespace) -> dict[str, object]:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    changes = report.get("unexplained_high_severity_changes")
    if not isinstance(changes, list):
        raise ValueError(
            "change report lacks structured unexplained_high_severity_changes"
        )
    databases = {
        ("public_non_dicom", "new"): Path(args.public_new),
        ("public_non_dicom", "old"): Path(args.public_old),
        ("participant_inventory", "new"): Path(args.participant_new),
        ("participant_inventory", "old"): Path(args.participant_old),
    }
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    blocking: list[dict[str, object]] = []
    for raw_change in changes:
        if not isinstance(raw_change, dict):
            raise ValueError("structured semantic change must be an object")
        change = dict(raw_change)
        artifact = str(change.get("artifact") or "")
        if artifact not in {"public_non_dicom", "participant_inventory"}:
            blocking.append(change)
            continue
        try:
            scope = resolve_scope(change, databases)
        except (KeyError, ValueError, sqlite3.Error):
            scope = None
        if scope is None:
            blocking.append(change)
        else:
            grouped[scope].append(change)

    entries: list[dict[str, object]] = []
    for (dataset_type, short_title), scoped_changes in sorted(grouped.items()):
        counts = Counter(
            f"{change['artifact']}.{change['table']}:{change['change_kind']}"
            for change in scoped_changes
        )
        entries.append({
            "dataset_type": dataset_type,
            "short_title": short_title,
            "status": "needs_review",
            "change_count": len(scoped_changes),
            "changes_sha256": change_digest(scoped_changes),
            "change_counts": dict(sorted(counts.items())),
            "release_disposition": (
                "blocking_until_exact_approval_or_reviewed_dataset_holdback"
            ),
        })
    payload: dict[str, object] = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_report_sha256": str(report.get("report_sha256") or ""),
        "queue_entries": entries,
        "blocking_unscoped_change_count": len(blocking),
        "blocking_unscoped_changes": blocking,
        "summary": {
            "dataset_queue_count": len(entries),
            "dataset_scoped_change_count": sum(int(item["change_count"]) for item in entries),
            "blocking_unscoped_change_count": len(blocking),
        },
    }
    payload["queue_sha256"] = hashlib.sha256(
        canonical_json({key: value for key, value in payload.items() if key != "generated_at_utc"}).encode("utf-8")
    ).hexdigest()
    return payload


def write_csv(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=(
            "dataset_type", "short_title", "status", "change_count",
            "changes_sha256", "release_disposition", "change_counts_json",
        ))
        writer.writeheader()
        for entry in payload["queue_entries"]:
            writer.writerow({
                **{key: entry[key] for key in writer.fieldnames if key != "change_counts_json"},
                "change_counts_json": canonical_json(entry["change_counts"]),
            })


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--public-new", required=True)
    parser.add_argument("--public-old", required=True)
    parser.add_argument("--participant-new", required=True)
    parser.add_argument("--participant-old", required=True)
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--csv-out", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = build_queue(args)
        json_out = Path(args.json_out)
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        write_csv(Path(args.csv_out), payload)
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"error: {exc}")
        return 1
    print(
        "dataset_queue_count="
        f"{payload['summary']['dataset_queue_count']} "
        "blocking_unscoped_change_count="
        f"{payload['summary']['blocking_unscoped_change_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
