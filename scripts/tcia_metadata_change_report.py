#!/usr/bin/env python3
"""Compare newly built TCIA SQLite assets with their published predecessors."""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class TableSpec:
    name: str
    keys: tuple[str, ...] = ()


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
        TableSpec("clinical_facts"),
        TableSpec("clinical_subjects"),
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
}


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


def row_count(conn: sqlite3.Connection, name: str) -> int | None:
    if not object_columns(conn, name):
        return None
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {quote_identifier(name)}"
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
        f"SELECT DISTINCT {selected} FROM {quote_identifier(spec.name)} "
        f"ORDER BY {selected}"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [
        tuple("" if value is None else str(value) for value in row)
        for row in conn.execute(sql)
    ]


def format_key(row: Iterable[str]) -> str:
    return " / ".join(value or "(blank)" for value in row)


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


def compare_asset(
    name: str,
    new_path: Path,
    old_path: Path | None,
    *,
    max_items: int,
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    new = sqlite3.connect(new_path)
    old = sqlite3.connect(old_path) if old_path and old_path.exists() else None
    rows: list[dict[str, object]] = []
    details: list[str] = []
    warnings: list[str] = []
    for spec in PROFILES[name]:
        new_count = row_count(new, spec.name)
        if new_count is None:
            continue
        old_count = row_count(old, spec.name) if old else None
        old_display = 0 if old_count is None else old_count
        added = max(new_count - old_display, 0)
        removed = max(old_display - new_count, 0)
        new_keys: list[tuple[str, ...]] = []
        if spec.keys:
            if old is None or old_count is None:
                new_keys = key_rows(new, spec, limit=max_items)
                added = new_count
            elif new_count + old_count <= 250_000:
                new_set = set(key_rows(new, spec))
                old_set = set(key_rows(old, spec))
                added = len(new_set - old_set)
                removed = len(old_set - new_set)
                new_keys = sorted(new_set - old_set)[:max_items]
        rows.append(
            {
                "asset": name,
                "table": spec.name,
                "old": old_count,
                "new": new_count,
                "added": added,
                "removed": removed,
            }
        )
        if new_keys:
            details.append(
                f"**{name} · {spec.name}**\n\n"
                + "\n".join(f"- `{format_key(row)}`" for row in new_keys)
            )
        if added:
            warnings.append(
                f"{name}: {spec.name} added {added:,} row"
                f"{'s' if added != 1 else ''}"
            )
    if old is None:
        warnings.append(
            f"{name}: no previous SQLite was available; report uses a new baseline"
        )
    new.close()
    if old:
        old.close()
    return rows, details, warnings


def build_report(args: argparse.Namespace) -> tuple[str, list[str]]:
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
    for name, new_path, old_path in assets:
        rows, details, asset_warnings = compare_asset(
            name, new_path, old_path, max_items=args.max_items
        )
        comparison_rows.extend(rows)
        detail_blocks.extend(details)
        warnings.extend(asset_warnings)

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
        "| Asset | Table/view | Previous | New | Added | Removed |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in comparison_rows:
        previous = "baseline" if row["old"] is None else f"{row['old']:,}"
        lines.append(
            f"| {row['asset']} | `{row['table']}` | {previous} | "
            f"{row['new']:,} | {row['added']:,} | {row['removed']:,} |"
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
    if not warnings:
        lines.extend(["", "No monitored additions or new review flags."])
    return "\n".join(lines).rstrip() + "\n", warnings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in PROFILES:
        parser.add_argument(f"--{name}-new")
        parser.add_argument(f"--{name}-old")
    parser.add_argument("--markdown-out")
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--github-actions", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        markdown, warnings = build_report(args)
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}")
        return 1
    print(markdown, end="")
    if args.markdown_out:
        path = Path(args.markdown_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    if args.github_actions:
        for warning_text in warnings:
            print(
                "::warning title=TCIA metadata review::"
                + github_escape(warning_text)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
