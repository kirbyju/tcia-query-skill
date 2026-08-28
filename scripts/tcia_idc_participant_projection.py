#!/usr/bin/env python3
"""Build a compact IDC participant projection for TCIA V2 staging."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
DEFAULT_SNAPSHOT_DB = Path(__file__).resolve().parents[1] / "cache" / "tcia_snapshot.sqlite"
DEFAULT_DB = Path(__file__).resolve().parents[1] / "cache" / "idc_participant_projection.sqlite"
DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[1] / "cache" / "idc_participant_projection_manifest.json"
)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE idc_participant_projection_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE idc_dataset_participants (
    dataset_type TEXT NOT NULL,
    short_title TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    source_collection_ids_json TEXT NOT NULL DEFAULT '[]',
    source_analysis_result_ids_json TEXT NOT NULL DEFAULT '[]',
    study_count INTEGER NOT NULL DEFAULT 0,
    series_count INTEGER NOT NULL DEFAULT 0,
    modalities TEXT NOT NULL DEFAULT '',
    object_roles TEXT NOT NULL DEFAULT '',
    source_dois TEXT NOT NULL DEFAULT '',
    geometry_statuses TEXT NOT NULL DEFAULT '',
    geometry_eligible_series_count INTEGER NOT NULL DEFAULT 0,
    geometry_checked_series_count INTEGER NOT NULL DEFAULT 0,
    regularly_spaced_volume_series_count INTEGER NOT NULL DEFAULT 0,
    non_regular_volume_series_count INTEGER NOT NULL DEFAULT 0,
    geometry_not_checked_series_count INTEGER NOT NULL DEFAULT 0,
    idc_version TEXT NOT NULL,
    PRIMARY KEY (dataset_type, short_title, participant_id)
) WITHOUT ROWID;

CREATE TABLE idc_unmatched_datasets (
    dataset_type TEXT NOT NULL,
    source_dataset_id TEXT NOT NULL,
    participant_count INTEGER NOT NULL,
    series_count INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (dataset_type, source_dataset_id)
) WITHOUT ROWID;

CREATE VIEW agent_idc_dataset_participants AS
SELECT * FROM idc_dataset_participants;
"""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_dataset_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "none", "<na>"} else text


def visible_dataset_maps(snapshot_db: Path) -> dict[str, dict[str, str]]:
    if not snapshot_db.is_file():
        raise FileNotFoundError(snapshot_db)
    result: dict[str, dict[str, str]] = {"Collection": {}, "Analysis Result": {}}
    ambiguous: dict[str, set[str]] = {"Collection": set(), "Analysis Result": set()}
    with closing(sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT dataset_type, short_title FROM agent_datasets "
            "WHERE hidden = 0 AND COALESCE(short_title, '') <> ''"
        ).fetchall()
    for dataset_type, short_title in rows:
        kind = str(dataset_type)
        if kind not in result:
            continue
        key = normalize_dataset_name(short_title)
        title = str(short_title).strip()
        previous = result[kind].get(key)
        if previous and previous != title:
            ambiguous[kind].add(key)
        else:
            result[kind][key] = title
    for kind, keys in ambiguous.items():
        for key in keys:
            result[kind].pop(key, None)
    return result


def idc_version(client: Any) -> str:
    getter = getattr(client, "get_idc_version", None)
    version = clean_text(getter() if callable(getter) else "")
    data_dir = clean_text(getattr(client, "indices_data_dir", ""))
    release = Path(data_dir).name if data_dir else ""
    if release and release != version:
        return f"{version}@{release}" if version else release
    return version or "unknown"


def unique_text_json(values: Any) -> str:
    return canonical_json(sorted({clean_text(value) for value in values if clean_text(value)}))


def unique_text_list(values: Any) -> str:
    return ";".join(sorted({clean_text(value) for value in values if clean_text(value)}))


def idc_object_role(modality: Any) -> str:
    value = clean_text(modality).upper()
    if value == "SEG":
        return "segmentation"
    if value in {"ANN", "RTSTRUCT", "PR", "KO"}:
        return "annotation"
    if value == "SR":
        return "measurement_report"
    if value in {"RTDOSE", "RTPLAN", "REG", "RWV"}:
        return "derived_object"
    return "source_image"


def project_frame(
    index_frame: Any,
    snapshot_db: Path,
    version: str,
    geometry_frame: Any | None = None,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    required = {
        "collection_id",
        "analysis_result_id",
        "PatientID",
        "StudyInstanceUID",
        "SeriesInstanceUID",
        "Modality",
        "source_DOI",
    }
    missing = sorted(required - set(index_frame.columns))
    if missing:
        raise RuntimeError("IDC index is missing required columns: " + ", ".join(missing))
    dataset_maps = visible_dataset_maps(snapshot_db)
    geometry_by_series: dict[str, str] = {}
    if geometry_frame is not None:
        geometry_required = {"SeriesInstanceUID", "regularly_spaced_3d_volume"}
        geometry_missing = sorted(geometry_required - set(geometry_frame.columns))
        if geometry_missing:
            raise RuntimeError(
                "IDC volume geometry index is missing required columns: "
                + ", ".join(geometry_missing)
            )
        for _, row in geometry_frame.loc[
            :, ["SeriesInstanceUID", "regularly_spaced_3d_volume"]
        ].iterrows():
            series_uid = clean_text(row["SeriesInstanceUID"])
            if not series_uid:
                continue
            regular = row["regularly_spaced_3d_volume"]
            if regular is None or clean_text(regular).casefold() in {"", "nan", "<na>"}:
                status = "checked_indeterminate"
            else:
                status = "checked_regular" if bool(regular) else "checked_not_regular"
            previous = geometry_by_series.get(series_uid)
            if previous and previous != status:
                raise RuntimeError(
                    f"Conflicting IDC geometry rows for SeriesInstanceUID {series_uid}"
                )
            geometry_by_series[series_uid] = status
    contexts: list[Any] = []
    unmatched_rows: list[tuple[Any, ...]] = []
    for dataset_type, source_column in (
        ("Collection", "collection_id"),
        ("Analysis Result", "analysis_result_id"),
    ):
        columns = [
            "collection_id",
            "analysis_result_id",
            "PatientID",
            "StudyInstanceUID",
            "SeriesInstanceUID",
            "Modality",
            "source_DOI",
        ]
        frame = index_frame.loc[:, columns].copy()
        frame["source_dataset_id"] = frame[source_column].map(clean_text)
        frame["participant_id"] = frame["PatientID"].map(clean_text)
        frame["object_role"] = frame["Modality"].map(idc_object_role)
        frame["geometry_status"] = frame["SeriesInstanceUID"].map(
            lambda value: geometry_by_series.get(clean_text(value), "not_in_geometry_index_scope")
            if geometry_frame is not None
            else "not_checked"
        )
        frame = frame[
            (frame["source_dataset_id"] != "") & (frame["participant_id"] != "")
        ].copy()
        frame["short_title"] = frame["source_dataset_id"].map(
            lambda value: dataset_maps[dataset_type].get(normalize_dataset_name(value), "")
        )
        unmatched = frame[frame["short_title"] == ""]
        if not unmatched.empty:
            summary = unmatched.groupby("source_dataset_id", sort=True).agg(
                participant_count=("participant_id", "nunique"),
                series_count=("SeriesInstanceUID", "nunique"),
            )
            unmatched_rows.extend(
                (
                    dataset_type,
                    str(source_id),
                    int(row.participant_count),
                    int(row.series_count),
                    "not_visible_in_tcia_wordpress_snapshot",
                )
                for source_id, row in summary.iterrows()
            )
        matched = frame[frame["short_title"] != ""].copy()
        matched["dataset_type"] = dataset_type
        contexts.append(matched)
    if not contexts:
        return [], unmatched_rows
    import pandas as pd

    combined = pd.concat(contexts, ignore_index=True)
    grouped = combined.groupby(
        ["dataset_type", "short_title", "participant_id"], sort=True, dropna=False
    ).agg(
        source_collection_ids_json=("collection_id", unique_text_json),
        source_analysis_result_ids_json=("analysis_result_id", unique_text_json),
        study_count=("StudyInstanceUID", "nunique"),
        series_count=("SeriesInstanceUID", "nunique"),
        modalities=("Modality", unique_text_list),
        object_roles=("object_role", unique_text_list),
        source_dois=("source_DOI", unique_text_list),
        geometry_statuses=("geometry_status", unique_text_list),
        geometry_eligible_series_count=(
            "geometry_status",
            lambda values: int(sum(str(value).startswith("checked_") for value in values)),
        ),
        geometry_checked_series_count=(
            "geometry_status",
            lambda values: int(
                sum(value in {"checked_regular", "checked_not_regular"} for value in values)
            ),
        ),
        regularly_spaced_volume_series_count=(
            "geometry_status", lambda values: int(sum(value == "checked_regular" for value in values))
        ),
        non_regular_volume_series_count=(
            "geometry_status", lambda values: int(sum(value == "checked_not_regular" for value in values))
        ),
        geometry_not_checked_series_count=(
            "geometry_status",
            lambda values: int(sum(value == "not_checked" for value in values)),
        ),
    )
    rows = [
        (
            str(dataset_type),
            str(short_title),
            str(participant_id),
            str(row.source_collection_ids_json),
            str(row.source_analysis_result_ids_json),
            int(row.study_count),
            int(row.series_count),
            str(row.modalities),
            str(row.object_roles),
            str(row.source_dois),
            str(row.geometry_statuses),
            int(row.geometry_eligible_series_count),
            int(row.geometry_checked_series_count),
            int(row.regularly_spaced_volume_series_count),
            int(row.non_regular_volume_series_count),
            int(row.geometry_not_checked_series_count),
            version,
        )
        for (dataset_type, short_title, participant_id), row in grouped.iterrows()
    ]
    return rows, unmatched_rows


def build_database(
    out: Path,
    *,
    snapshot_db: Path,
    replace: bool = False,
    index_frame: Any | None = None,
    geometry_frame: Any | None = None,
    source_version: str = "",
) -> dict[str, Any]:
    if out.exists():
        if not replace:
            raise FileExistsError(f"Output exists: {out}; pass --replace")
        out.unlink()
    if index_frame is None:
        try:
            from idc_index import index
        except ImportError as exc:
            raise RuntimeError(
                "Building the IDC projection requires idc-index; install idc-index>=0.12.3"
            ) from exc
        client = index.IDCClient()
        index_frame = client.index
        client.fetch_index("volume_geometry_index")
        geometry_frame = getattr(client, "volume_geometry_index", None)
        if geometry_frame is None:
            raise RuntimeError("idc-index did not load volume_geometry_index")
        source_version = idc_version(client)
    source_version = source_version or "unknown"
    rows, unmatched = project_frame(
        index_frame, snapshot_db, source_version, geometry_frame=geometry_frame
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(out)) as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO idc_dataset_participants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.executemany("INSERT INTO idc_unmatched_datasets VALUES (?,?,?,?,?)", unmatched)
        collection_participants = int(
            conn.execute(
                "SELECT COUNT(*) FROM idc_dataset_participants WHERE dataset_type='Collection'"
            ).fetchone()[0]
        )
        analysis_participants = int(
            conn.execute(
                "SELECT COUNT(*) FROM idc_dataset_participants WHERE dataset_type='Analysis Result'"
            ).fetchone()[0]
        )
        metadata = {
            "artifact": "idc_participant_projection",
            "schema_version": str(SCHEMA_VERSION),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "idc_version": source_version,
            "authority": "IDC public DICOM index",
            "scope": "visible TCIA WordPress Collections and Analysis Results",
            "geometry_source": (
                "idc-index-data volume_geometry_index"
                if geometry_frame is not None
                else "not_checked"
            ),
        }
        conn.executemany("INSERT INTO idc_participant_projection_meta VALUES (?,?)", metadata.items())
        conn.commit()
        conn.execute("ANALYZE")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    return {
        "path": str(out),
        "schema_version": SCHEMA_VERSION,
        "idc_version": source_version,
        "integrity_check": integrity,
        "counts": {
            "participants": len(rows),
            "collection_participant_memberships": collection_participants,
            "analysis_result_participant_memberships": analysis_participants,
            "unmatched_datasets": len(unmatched),
        },
    }


def validate_database(
    path: Path,
    *,
    minimum_collection_memberships: int = 0,
    minimum_collections: int = 0,
    minimum_analysis_result_memberships: int = 0,
    minimum_analysis_results: int = 0,
) -> dict[str, Any]:
    errors: list[str] = []
    if not path.is_file():
        return {"ok": False, "errors": [f"missing database: {path}"]}
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            errors.append(f"integrity_check={integrity}")
        objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        required = {
            "idc_participant_projection_meta",
            "idc_dataset_participants",
            "idc_unmatched_datasets",
            "agent_idc_dataset_participants",
        }
        if required - objects:
            errors.append("missing objects: " + ", ".join(sorted(required - objects)))
        invalid = int(
            conn.execute(
                "SELECT COUNT(*) FROM idc_dataset_participants "
                "WHERE dataset_type NOT IN ('Collection','Analysis Result') "
                "OR short_title='' OR participant_id='' OR study_count < 0 OR series_count < 0 "
                "OR geometry_checked_series_count > geometry_eligible_series_count "
                "OR regularly_spaced_volume_series_count + non_regular_volume_series_count "
                "   > geometry_checked_series_count"
            ).fetchone()[0]
        )
        if invalid:
            errors.append(f"invalid participant projection rows: {invalid}")
        counts = {
            "participants": int(conn.execute("SELECT COUNT(*) FROM idc_dataset_participants").fetchone()[0]),
            "collections": int(conn.execute("SELECT COUNT(DISTINCT short_title) FROM idc_dataset_participants WHERE dataset_type='Collection'").fetchone()[0]),
            "analysis_results": int(conn.execute("SELECT COUNT(DISTINCT short_title) FROM idc_dataset_participants WHERE dataset_type='Analysis Result'").fetchone()[0]),
            "unmatched_datasets": int(conn.execute("SELECT COUNT(*) FROM idc_unmatched_datasets").fetchone()[0]),
        }
        thresholds = {
            "participants": minimum_collection_memberships,
            "collections": minimum_collections,
            "analysis_result_participants": minimum_analysis_result_memberships,
            "analysis_results": minimum_analysis_results,
        }
        actual = {
            "participants": int(
                conn.execute(
                    "SELECT COUNT(*) FROM idc_dataset_participants "
                    "WHERE dataset_type='Collection'"
                ).fetchone()[0]
            ),
            "collections": counts["collections"],
            "analysis_result_participants": int(
                conn.execute(
                    "SELECT COUNT(*) FROM idc_dataset_participants "
                    "WHERE dataset_type='Analysis Result'"
                ).fetchone()[0]
            ),
            "analysis_results": counts["analysis_results"],
        }
        for name, minimum in thresholds.items():
            if actual[name] < minimum:
                errors.append(
                    f"{name} coverage regression: {actual[name]} < required {minimum}"
                )
    return {"ok": not errors, "errors": errors, "integrity_check": integrity, "counts": counts}


def build_manifest(path: Path) -> dict[str, Any]:
    validation = validate_database(path)
    if not validation["ok"]:
        raise RuntimeError("Cannot manifest invalid database: " + "; ".join(validation["errors"]))
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        meta = dict(conn.execute("SELECT key, value FROM idc_participant_projection_meta"))
    manifest: dict[str, Any] = {
        "artifact_family": "tcia-metadata-v2-staging",
        "artifact": "idc_participant_projection",
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": meta.get("generated_at_utc", ""),
        "idc_version": meta.get("idc_version", ""),
        "sqlite_bytes": path.stat().st_size,
        "sqlite_sha256": file_sha256(path),
        "counts": validation["counts"],
    }
    manifest["release_fingerprint"] = hashlib.sha256(
        canonical_json(manifest).encode("utf-8")
    ).hexdigest()
    return manifest


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--snapshot-db", default=str(DEFAULT_SNAPSHOT_DB))
    build.add_argument("--out", default=str(DEFAULT_DB))
    build.add_argument("--manifest-out", default=str(DEFAULT_MANIFEST))
    build.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--db", default=str(DEFAULT_DB))
    validate.add_argument("--min-collection-memberships", type=int, default=0)
    validate.add_argument("--min-collections", type=int, default=0)
    validate.add_argument("--min-analysis-result-memberships", type=int, default=0)
    validate.add_argument("--min-analysis-results", type=int, default=0)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "build":
        out = Path(args.out)
        result = build_database(
            out, snapshot_db=Path(args.snapshot_db), replace=args.replace
        )
        manifest = build_manifest(out)
        manifest_path = Path(args.manifest_out)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        result["manifest"] = str(manifest_path)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    result = validate_database(
        Path(args.db),
        minimum_collection_memberships=args.min_collection_memberships,
        minimum_collections=args.min_collections,
        minimum_analysis_result_memberships=args.min_analysis_result_memberships,
        minimum_analysis_results=args.min_analysis_results,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
