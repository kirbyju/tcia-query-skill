#!/usr/bin/env python3
"""Build reviewed TCGA-LGG-Mask reference inventories from official files."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import openpyxl


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MASK_OUT = ROOT / "references" / "tcga_lgg_mask_inventory_v1.csv"
DEFAULT_VASARI_OUT = ROOT / "references" / "tcga_lgg_mask_vasari_participants_v1.csv"
DEFAULT_PROVENANCE_OUT = ROOT / "references" / "tcga_lgg_mask_inventory_v1.json"

FILES = {
    "mask_zip": "ROIdata-TCGA-LGG-20170131.zip",
    "clinical_csv": "TCGA_clinical_INFO.csv",
    "vasari_csv": "TCGA_vasari_INFO.csv",
    "feature_key_pdf": "VASARI_MR_featurekey.pdf",
    "series_manifest_csv": "GC_manifest_TCGA-LGG-MASK-TCGA-LGG-doiJNLP-LxVyYE8d.csv",
    "dicom_digest_xlsx": "doiJNLP-LxVyYE8d-nbia-digest.xlsx",
}

SOURCE_URLS = {
    "mask_zip": "https://www.cancerimagingarchive.net/wp-content/uploads/ROIdata-TCGA-LGG-20170131.zip",
    "clinical_csv": "https://www.cancerimagingarchive.net/wp-content/uploads/TCGA_clinical_INFO.csv",
    "vasari_csv": "https://www.cancerimagingarchive.net/wp-content/uploads/TCGA_vasari_INFO.csv",
    "feature_key_pdf": "https://www.cancerimagingarchive.net/wp-content/uploads/VASARI_MR_featurekey.pdf",
    "series_manifest_csv": "https://www.cancerimagingarchive.net/wp-content/uploads/GC_manifest_TCGA-LGG-MASK-TCGA-LGG-doiJNLP-LxVyYE8d.csv",
    "dicom_digest_xlsx": "https://www.cancerimagingarchive.net/wp-content/uploads/doiJNLP-LxVyYE8d-nbia-digest.xlsx",
}

MASK_FIELDS = [
    "subject_id",
    "raw_subject_id",
    "package_path",
    "file_name",
    "size_bytes",
    "sha256",
    "study_instance_uid",
    "series_instance_uid",
    "dicom_image_count",
    "series_description",
    "protocol_name",
]

VASARI_FIELDS = [
    "source_row",
    "raw_subject_id",
    "subject_id",
    "complete_numeric",
    "non_numeric_or_missing_fields_json",
]


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_vasari_subject(raw_subject_id: str) -> str:
    value = raw_subject_id.strip().upper()
    if value == "TCGA-EZ-7264A":
        return "TCGA-EZ-7264"
    return value


def read_digest(path: Path) -> dict[str, dict[str, Any]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook["Metadata"]
    rows = sheet.iter_rows(values_only=True)
    header = list(next(rows))
    column = {str(name): index for index, name in enumerate(header)}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = str(row[column["Series Instance UID"]] or "").strip()
        if not uid:
            continue
        if uid in result:
            raise RuntimeError(f"duplicate Series Instance UID in digest: {uid}")
        result[uid] = {
            "subject_id": str(row[column["Patient ID"]] or "").strip(),
            "study_instance_uid": str(row[column["Study Instance UID"]] or "").strip(),
            "dicom_image_count": int(row[column["Image Count"]] or 0),
            "series_description": str(row[column["Series Description"]] or "").strip(),
            "protocol_name": str(row[column["Protocol Name"]] or "").strip(),
        }
    return result


def read_manifest_uids(path: Path) -> set[str]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    return {
        str(row.get("TCIA Series Instance UID") or "").strip()
        for row in rows
        if str(row.get("TCIA Series Instance UID") or "").strip()
    }


def build_mask_rows(source_dir: Path) -> list[dict[str, Any]]:
    digest = read_digest(source_dir / FILES["dicom_digest_xlsx"])
    manifest_uids = read_manifest_uids(source_dir / FILES["series_manifest_csv"])
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(source_dir / FILES["mask_zip"]) as archive:
        names = sorted(name for name in archive.namelist() if name.lower().endswith(".mat"))
        for name in names:
            parts = Path(name).parts
            raw_subject_id = parts[-2]
            file_name = parts[-1]
            uid = Path(file_name).stem
            if uid not in manifest_uids or uid not in digest:
                raise RuntimeError(f"mask UID is absent from manifest or digest: {uid}")
            metadata = digest[uid]
            if metadata["subject_id"] != raw_subject_id:
                raise RuntimeError(
                    f"ZIP/digest patient mismatch for {uid}: "
                    f"{raw_subject_id} != {metadata['subject_id']}"
                )
            payload = archive.read(name)
            rows.append(
                {
                    "subject_id": raw_subject_id.upper(),
                    "raw_subject_id": raw_subject_id,
                    "package_path": name,
                    "file_name": file_name,
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "study_instance_uid": metadata["study_instance_uid"],
                    "series_instance_uid": uid,
                    "dicom_image_count": metadata["dicom_image_count"],
                    "series_description": metadata["series_description"],
                    "protocol_name": metadata["protocol_name"],
                }
            )
    zip_uids = {str(row["series_instance_uid"]) for row in rows}
    if zip_uids != manifest_uids or zip_uids != set(digest):
        raise RuntimeError("ZIP, manifest, and digest Series Instance UID sets differ")
    if len(rows) != 406 or len({row["subject_id"] for row in rows}) != 108:
        raise RuntimeError("expected 406 masks across 108 subjects")
    return rows


def build_vasari_rows(source_dir: Path) -> list[dict[str, Any]]:
    path = source_dir / FILES["vasari_csv"]
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.reader(stream))
    header = rows[0]
    feature_names = header[1:]
    result: list[dict[str, Any]] = []
    for source_row, values in enumerate(rows[1:], start=2):
        raw_subject_id = values[0].strip()
        feature_values = values[1 : len(feature_names) + 1]
        problematic = [
            feature_names[index]
            for index, value in enumerate(feature_values)
            if not value.strip().isdigit()
        ]
        result.append(
            {
                "source_row": source_row,
                "raw_subject_id": raw_subject_id,
                "subject_id": normalized_vasari_subject(raw_subject_id),
                "complete_numeric": int(not problematic),
                "non_numeric_or_missing_fields_json": json.dumps(
                    problematic, ensure_ascii=False, separators=(",", ":")
                ),
            }
        )
    if len(result) != 188 or len({row["subject_id"] for row in result}) != 188:
        raise RuntimeError("expected 188 canonical VASARI participants")
    if sum(int(row["complete_numeric"]) for row in result) != 178:
        raise RuntimeError("expected 178 complete numeric VASARI rows")
    return result


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def file_metadata(path: Path, url: str) -> dict[str, Any]:
    return {
        "name": path.name,
        "url": url,
        "bytes": path.stat().st_size,
        "sha256": sha256_path(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("--mask-out", type=Path, default=DEFAULT_MASK_OUT)
    parser.add_argument("--vasari-out", type=Path, default=DEFAULT_VASARI_OUT)
    parser.add_argument("--provenance-out", type=Path, default=DEFAULT_PROVENANCE_OUT)
    args = parser.parse_args()

    missing = [name for name in FILES.values() if not (args.source_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(f"missing source files: {missing}")

    mask_rows = build_mask_rows(args.source_dir)
    vasari_rows = build_vasari_rows(args.source_dir)
    write_csv(args.mask_out, MASK_FIELDS, mask_rows)
    write_csv(args.vasari_out, VASARI_FIELDS, vasari_rows)

    clinical_path = args.source_dir / FILES["clinical_csv"]
    with clinical_path.open(newline="", encoding="utf-8-sig") as stream:
        clinical_rows = list(csv.reader(stream))
    clinical_subjects = {row[0].strip().upper() for row in clinical_rows[1:] if row}
    if len(clinical_rows) - 1 != 188 or len(clinical_subjects) != 188:
        raise RuntimeError("expected 188 clinical rows and subjects")

    provenance = {
        "schema_version": 1,
        "reviewed_at": "2026-08-27",
        "dataset_type": "Analysis Result",
        "short_title": "TCGA-LGG-Mask",
        "review_status": "curator_reviewed",
        "review_decisions": {
            "segmentation_subject_count": 108,
            "vasari_alias": {
                "raw_subject_id": "TCGA-EZ-7264A",
                "canonical_subject_id": "TCGA-EZ-7264",
                "reason": "curator-confirmed_likely_typo",
            },
        },
        "counts": {
            "mask_files": len(mask_rows),
            "mask_subjects": len({row["subject_id"] for row in mask_rows}),
            "series_instance_uids": len({row["series_instance_uid"] for row in mask_rows}),
            "clinical_rows": len(clinical_rows) - 1,
            "clinical_subjects": len(clinical_subjects),
            "vasari_rows": len(vasari_rows),
            "vasari_subjects": len({row["subject_id"] for row in vasari_rows}),
            "vasari_complete_numeric_rows": sum(
                int(row["complete_numeric"]) for row in vasari_rows
            ),
        },
        "source_files": {
            key: file_metadata(args.source_dir / name, SOURCE_URLS[key])
            for key, name in FILES.items()
        },
        "reference_files": {
            "mask_inventory": file_metadata(
                args.mask_out, f"references/{args.mask_out.name}"
            ),
            "vasari_participants": file_metadata(
                args.vasari_out, f"references/{args.vasari_out.name}"
            ),
        },
    }
    args.provenance_out.write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(provenance["counts"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
