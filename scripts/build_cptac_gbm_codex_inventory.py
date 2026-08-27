#!/usr/bin/env python3
"""Build the reviewed CPTAC-Glioblastoma-CODEX V2 package reference."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "cache" / "tcia-metadata-v2-latest" / "pathology_metadata.sqlite"
DEFAULT_CSV = ROOT / "references" / "cptac_gbm_codex_inventory_v1.csv"
DEFAULT_JSON = ROOT / "references" / "cptac_gbm_codex_inventory_v1.json"
SHORT_TITLE = "CPTAC-Glioblastoma-CODEX"
FIELDS = (
    "non_dicom_file_id",
    "download_row_id",
    "download_id",
    "file_name",
    "file_ext",
    "package_path",
    "file_role",
    "bytes",
    "checksum",
    "checksum_algorithm",
    "object_modality",
    "image_format",
    "is_wsi",
    "is_codex",
    "source_table",
    "source_row_id",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--provenance-out", type=Path, default=DEFAULT_JSON)
    args = parser.parse_args()

    connection = sqlite3.connect(args.source)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"""SELECT {', '.join(FIELDS)}
                FROM agent_pathology_file_objects
                WHERE short_title = ? AND COALESCE(is_metadata, 0) = 0
                ORDER BY package_path""",
            (SHORT_TITLE,),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != 52:
        raise RuntimeError(f"Expected 52 package rows, found {len(rows)}")
    if len({str(row['package_path']) for row in rows}) != 52:
        raise RuntimeError("Package paths are not unique")

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row[field] if row[field] is not None else "" for field in FIELDS})
    csv_bytes = stream.getvalue().encode("utf-8")
    args.out.write_bytes(csv_bytes)

    extension_counts: dict[str, int] = {}
    for row in rows:
        extension = str(row["file_ext"] or "").lower()
        extension_counts[extension] = extension_counts.get(extension, 0) + 1
    provenance = {
        "schema_version": 1,
        "short_title": SHORT_TITLE,
        "dataset_type": "Analysis Result",
        "download_id": "48969",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "reviewed_at": "2026-08-27",
        "review_status": "reviewed_source_projection",
        "source_artifact": "legacy pathology_metadata package inventory",
        "source_view": "agent_pathology_file_objects",
        "source_scope": "short_title=CPTAC-Glioblastoma-CODEX and is_metadata=0",
        "migration_note": "This pinned 52-row reference removes the runtime dependency on the retired pathology artifact.",
        "row_count": len(rows),
        "unique_package_paths": len({str(row["package_path"]) for row in rows}),
        "extension_counts": dict(sorted(extension_counts.items())),
        "inventory_sha256": hashlib.sha256(csv_bytes).hexdigest(),
    }
    args.provenance_out.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(provenance, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
