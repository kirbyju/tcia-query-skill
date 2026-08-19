#!/usr/bin/env python3
"""Measure whether unified V2 outputs can retire legacy NIfTI/pathology artifacts."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

try:
    import tcia_public_non_dicom_metadata as public_non_dicom
    from tcia_artifact_model import normalize_format, stable_id
except ModuleNotFoundError:
    from scripts import tcia_public_non_dicom_metadata as public_non_dicom
    from scripts.tcia_artifact_model import normalize_format, stable_id


SPECIALIZED_CAPABILITIES = {
    "nifti": {
        "derived_object_links": ("derived_objects", "derived_object_references"),
        "reviewed_characteristics": (
            "nifti_classification_rules",
            "nifti_file_characteristics",
        ),
        "review_issues": ("nifti_dataset_review_issues", "metadata_quality_flags"),
        "annotation_groups": ("annotation_groups",),
    },
    "pathology": {
        "download_label_matches": ("pathology_download_label_matches",),
        "complete_package_inventory": ("pathology_package_files",),
        "pathdb_slide_crosswalk": ("pathdb_slide_crosswalk",),
        "disparity_qc": ("pathology_disparities",),
    },
}


def object_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def expected_nifti_assets(conn: sqlite3.Connection) -> set[str]:
    if "agent_nifti_files" not in object_names(conn):
        return set()
    return {
        stable_id("asset", "nifti", str(short_title), str(radiology_id))
        for short_title, radiology_id in conn.execute(
            "SELECT short_title, radiology_id FROM agent_nifti_files"
        )
    }


def expected_pathology_assets(conn: sqlite3.Connection) -> set[str]:
    if "agent_pathology_file_objects" not in object_names(conn):
        return set()
    expected: set[str] = set()
    for row in conn.execute(
        "SELECT non_dicom_file_id, image_format, file_ext, is_metadata "
        "FROM agent_pathology_file_objects"
    ):
        if int(row[3] or 0):
            continue
        file_format = normalize_format(row[1] or row[2])
        if file_format not in public_non_dicom.NON_DICOM_IMAGING_FORMATS:
            continue
        expected.add(stable_id("asset", "pathology_package", str(row[0])))
    return expected


def capability_report(
    source: sqlite3.Connection,
    *,
    component: str,
    audit: sqlite3.Connection | None,
) -> dict[str, Any]:
    source_objects = object_names(source)
    audit_objects = object_names(audit) if audit is not None else set()
    capabilities: dict[str, Any] = {}
    for capability, tables in SPECIALIZED_CAPABILITIES[component].items():
        source_tables = sorted(set(tables) & source_objects)
        checkpoint_tables = [
            f"source_{component}__{table}" for table in source_tables
        ]
        missing_checkpoint = sorted(set(checkpoint_tables) - audit_objects)
        source_row_counts = {
            table: int(
                source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in source_tables
        }
        checkpoint_row_counts = {
            table: int(
                audit.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in checkpoint_tables
            if audit is not None and table in audit_objects
        }
        count_mismatches = {
            source_table: {
                "source_rows": source_row_counts[source_table],
                "checkpoint_rows": checkpoint_row_counts.get(
                    f"source_{component}__{source_table}"
                ),
            }
            for source_table in source_tables
            if checkpoint_row_counts.get(f"source_{component}__{source_table}")
            != source_row_counts[source_table]
        }
        capabilities[capability] = {
            "source_tables": source_tables,
            "source_row_counts": source_row_counts,
            "checkpoint_tables": sorted(set(checkpoint_tables) & audit_objects),
            "checkpoint_row_counts": checkpoint_row_counts,
            "retained": (
                bool(source_tables) and not missing_checkpoint and not count_mismatches
            ),
            "missing_checkpoint_tables": missing_checkpoint,
            "count_mismatches": count_mismatches,
        }
    return capabilities


def analyze_parity(
    *,
    public_db: Path,
    nifti_db: Path,
    pathology_db: Path,
    audit_db: Path | None = None,
) -> dict[str, Any]:
    with closing(connect_readonly(public_db)) as public_conn, closing(
        connect_readonly(nifti_db)
    ) as nifti_conn, closing(connect_readonly(pathology_db)) as pathology_conn:
        public_ids = {
            str(row[0])
            for row in public_conn.execute("SELECT asset_id FROM public_non_dicom_assets")
        }
        expected = {
            "nifti": expected_nifti_assets(nifti_conn),
            "pathology": expected_pathology_assets(pathology_conn),
        }
        with ExitStack() as stack:
            audit_conn = (
                stack.enter_context(closing(connect_readonly(audit_db)))
                if audit_db and audit_db.is_file()
                else None
            )
            components: dict[str, Any] = {}
            for component, source_conn in (
                ("nifti", nifti_conn),
                ("pathology", pathology_conn),
            ):
                missing = sorted(expected[component] - public_ids)
                represented = len(expected[component]) - len(missing)
                capabilities = capability_report(
                    source_conn, component=component, audit=audit_conn
                )
                components[component] = {
                    "expected_projected_assets": len(expected[component]),
                    "represented_projected_assets": represented,
                    "missing_projected_asset_ids": missing[:100],
                    "missing_projected_asset_count": len(missing),
                    "specialized_capabilities": capabilities,
                    "retirement_ready": (
                        not missing
                        and all(details["retained"] for details in capabilities.values())
                    ),
                }
    projection_ok = all(
        details["missing_projected_asset_count"] == 0 for details in components.values()
    )
    retirement_ready = projection_ok and all(
        details["retirement_ready"] for details in components.values()
    )
    return {
        "artifact": "tcia_metadata_v2_legacy_detail_parity",
        "projection_ok": projection_ok,
        "retirement_ready": retirement_ready,
        "components": components,
        "decision": (
            "legacy_nifti_and_pathology_assets_may_be_removed_from_future_v2_contract"
            if retirement_ready
            else "retain_legacy_detail_assets_until_specialized_capabilities_are_checkpointed"
        ),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--public-db", required=True)
    root.add_argument("--nifti-db", required=True)
    root.add_argument("--pathology-db", required=True)
    root.add_argument("--audit-db")
    root.add_argument("--out")
    root.add_argument("--require-retirement-ready", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    result = analyze_parity(
        public_db=Path(args.public_db),
        nifti_db=Path(args.nifti_db),
        pathology_db=Path(args.pathology_db),
        audit_db=Path(args.audit_db) if args.audit_db else None,
    )
    body = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(body, encoding="utf-8")
    print(body, end="")
    if not result["projection_ok"]:
        return 1
    if args.require_retirement_ready and not result["retirement_ready"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
