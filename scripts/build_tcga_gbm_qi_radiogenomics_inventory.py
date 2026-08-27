#!/usr/bin/env python3
"""Build a reviewed file/participant/DICOM crosswalk from the AIM ZIP."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT.parent / "codex-scratch" / "SupplementaryDataZIP_AIMfiles.zip"
DEFAULT_CSV = ROOT / "references" / "tcga_gbm_qi_radiogenomics_aim_inventory_v1.csv"
DEFAULT_JSON = ROOT / "references" / "tcga_gbm_qi_radiogenomics_aim_inventory_v1.json"
SOURCE_URL = "https://www.cancerimagingarchive.net/wp-content/uploads/SupplementaryDataZIP_AIMfiles.zip"
NS = "{gme://caCORE.caCORE/3.2/edu.northwestern.radiology.AIM}"
FIELDS = (
    "file_name",
    "size_bytes",
    "sha256",
    "patient_id",
    "annotation_uid",
    "annotation_name",
    "aim_version",
    "code_meaning",
    "code_value",
    "coding_scheme_designator",
    "study_instance_uid",
    "series_instance_uid",
    "sop_instance_uid",
    "study_date",
)


def required_one(root: ET.Element, path: str, attribute: str, file_name: str) -> str:
    values = {
        str(element.get(attribute) or "").strip()
        for element in root.findall(path)
        if str(element.get(attribute) or "").strip()
    }
    if len(values) != 1:
        raise RuntimeError(
            f"Expected one {attribute} in {file_name}, found {sorted(values)}"
        )
    return next(iter(values))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--provenance-out", type=Path, default=DEFAULT_JSON)
    args = parser.parse_args()

    source_bytes = args.source.read_bytes()
    rows: list[dict[str, object]] = []
    ignored_entries: list[str] = []
    with zipfile.ZipFile(io.BytesIO(source_bytes)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if info.filename.startswith("__MACOSX/"):
                ignored_entries.append(info.filename)
                continue
            if not info.filename.lower().endswith(".xml"):
                raise RuntimeError(f"Unexpected AIM package entry: {info.filename}")
            content = archive.read(info)
            root = ET.fromstring(content)
            rows.append(
                {
                    "file_name": info.filename,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "patient_id": required_one(root, f".//{NS}Patient", "patientID", info.filename),
                    "annotation_uid": str(root.get("uniqueIdentifier") or "").strip(),
                    "annotation_name": str(root.get("name") or "").strip(),
                    "aim_version": str(root.get("aimVersion") or "").strip(),
                    "code_meaning": str(root.get("codeMeaning") or "").strip(),
                    "code_value": str(root.get("codeValue") or "").strip(),
                    "coding_scheme_designator": str(root.get("codingSchemeDesignator") or "").strip(),
                    "study_instance_uid": required_one(root, f".//{NS}Study", "instanceUID", info.filename),
                    "series_instance_uid": required_one(root, f".//{NS}Series", "instanceUID", info.filename),
                    "sop_instance_uid": required_one(root, f".//{NS}Image", "sopInstanceUID", info.filename),
                    "study_date": required_one(root, f".//{NS}Study", "studyDate", info.filename),
                }
            )
    rows.sort(key=lambda row: str(row["file_name"]).casefold())
    patients = {str(row["patient_id"]) for row in rows}
    studies = {str(row["study_instance_uid"]) for row in rows}
    series = {str(row["series_instance_uid"]) for row in rows}
    sops = {str(row["sop_instance_uid"]) for row in rows}
    annotations = {str(row["annotation_uid"]) for row in rows}
    expected = (321, 55, 60, 111, 193, 321)
    actual = (
        len(rows), len(patients), len(studies), len(series), len(sops), len(annotations)
    )
    if actual != expected:
        raise RuntimeError(f"AIM inventory contract changed: {actual} != {expected}")

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    csv_bytes = stream.getvalue().encode("utf-8")
    args.out.write_bytes(csv_bytes)
    provenance = {
        "schema_version": 1,
        "short_title": "TCGA-GBM-QI-Radiogenomics",
        "dataset_type": "Analysis Result",
        "source_collection_short_title": "TCGA-GBM",
        "download_id": "45557",
        "source_url": SOURCE_URL,
        "source_zip_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "inventory_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "reviewed_at": "2026-08-27",
        "review_status": "reviewed_source_projection",
        "counts": {
            "xml_files": len(rows),
            "patients": len(patients),
            "studies": len(studies),
            "series": len(series),
            "sop_instances": len(sops),
            "annotation_uids": len(annotations),
        },
        "ignored_package_entries": ignored_entries,
        "mapping_note": "Each AIM XML directly supplies one TCGA PatientID and one DICOM Study, Series, and SOP Instance UID.",
    }
    args.provenance_out.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(provenance, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
