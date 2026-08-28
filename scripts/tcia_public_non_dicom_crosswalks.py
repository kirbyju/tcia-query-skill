#!/usr/bin/env python3
"""Build reviewed public non-DICOM participant-to-file crosswalk rows.

This maintainer utility converts reviewer-approved filename/folder rules and
their supporting inventories into a flat, provenance-preserving CSV. It does
not inspect image pixels or rewrite submitter-provided identifiers.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = ROOT / "outputs" / "participant_crosswalk_sources"
DEFAULT_CURATION = ROOT / "references" / "public-non-dicom-crosswalk-curation-v1.json"
DEFAULT_OUT = ROOT / "references" / "public_non_dicom_crosswalks_v1.csv"
DEFAULT_MANIFEST = ROOT / "references" / "public_non_dicom_crosswalks_v1_provenance.json"

FIELDS = [
    "dataset_type", "short_title", "download_id", "subject_id", "raw_subject_id",
    "subject_id_namespace", "participant_link_status", "package_path", "file_name",
    "file_format", "media_kind", "imaging_domain", "modality", "object_role",
    "size_bytes", "source_system", "source_url", "crosswalk_source_url",
    "crosswalk_method", "crosswalk_confidence", "reviewer_note",
    "dicom_series_instance_uid", "raw_values_json", "provenance_json",
    "quality_flag_json",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_extension(name: str) -> str:
    return Path(name).suffix.lstrip(".").upper()


def row(**values: Any) -> dict[str, Any]:
    result = {field: "" for field in FIELDS}
    result.update(values)
    for field in ("raw_values_json", "provenance_json", "quality_flag_json"):
        value = result[field]
        if not isinstance(value, str):
            result[field] = json.dumps(value or {}, sort_keys=True, separators=(",", ":"))
    return result


def decisions_by_title(curation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["short_title"]: item for item in curation["decisions"]}


def explicit_mapping_rows(curation: dict[str, Any]) -> list[dict[str, Any]]:
    """Project curator-approved one-off mappings into the generated CSV."""
    output = []
    for decision in curation.get("decisions") or []:
        for mapping in decision.get("explicit_mappings") or []:
            package_path = str(mapping["package_path"])
            subject_id = str(mapping["subject_id"])
            output.append(row(
                dataset_type=decision["dataset_type"],
                short_title=decision["short_title"],
                download_id=str((decision.get("download_ids") or [""])[0]),
                subject_id=subject_id,
                raw_subject_id=subject_id,
                subject_id_namespace=f"tcia_dataset:{decision['short_title']}",
                participant_link_status="reviewed_source_crosswalk",
                package_path=package_path,
                file_name=Path(package_path).name,
                file_format=normalize_extension(package_path),
                media_kind="whole_slide_image",
                imaging_domain="pathology",
                modality="SM",
                object_role="source_image",
                source_system="tcia_aspera",
                crosswalk_source_url=mapping["crosswalk_source_url"],
                crosswalk_method="exact_pathdb_source_url_suffix",
                crosswalk_confidence="high",
                reviewer_note=decision["reviewer_note"],
                raw_values_json={"pathdb_subject_id": subject_id},
                provenance_json={"evidence_relation": "exact_pathdb_source_url_suffix"},
                quality_flag_json={},
            ))
    return output


def breast_rows(source_dir: Path, decision: dict[str, Any], workbook_json: dict[str, Any]) -> list[dict[str, Any]]:
    sheets = workbook_json["breast_clinical.xlsx"]
    values = next(sheet["values"] for sheet in sheets if sheet["name"].startswith("BrEaST-Lesions-USG"))
    headers = values[0]
    filename_fields = ["Image_filename", "Mask_tumor_filename", "Mask_other_filename"]
    mapping: dict[str, tuple[str, str]] = {}
    for values_row in values[1:]:
        record = dict(zip(headers, values_row))
        case_id = int(record["CaseID"])
        subject_id = f"case{case_id:03d}"
        for field in filename_fields:
            names = [name.strip() for name in str(record.get(field) or "").split("&") if name.strip()]
            for name in names:
                role = "segmentation" if field.startswith("Mask_") else "source_image"
                mapping[name.casefold()] = (subject_id, role)
    output = []
    archive = source_dir / "breast_images.zip"
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            if info.is_dir() or normalize_extension(info.filename) != "PNG":
                continue
            name = Path(info.filename).name
            if name.casefold() not in mapping:
                raise RuntimeError(f"Breast ZIP filename missing from clinical crosswalk: {name}")
            subject_id, role = mapping[name.casefold()]
            output.append(row(
                dataset_type="Collection", short_title="Breast-Lesions-USG", download_id="41273",
                subject_id=subject_id, raw_subject_id=subject_id.removeprefix("case"),
                subject_id_namespace="tcia_dataset:Breast-Lesions-USG",
                participant_link_status="reviewed_source_crosswalk", package_path=info.filename,
                file_name=name, file_format="PNG", media_kind="still_image",
                imaging_domain="radiology", modality="US", object_role=role,
                size_bytes=info.file_size, source_system="tcia_wordpress",
                source_url="https://www.cancerimagingarchive.net/wp-content/uploads/BrEaST-Lesions_USG-images_and_masks-Dec-15-2023.zip",
                crosswalk_source_url=decision["evidence_url"],
                crosswalk_method="clinical_filename_exact_match", crosswalk_confidence="high",
                reviewer_note=decision["reviewer_note"],
                raw_values_json={"clinical_case_id": int(subject_id[-3:])},
                provenance_json={"inventory": archive.name, "crosswalk": "breast_clinical.xlsx"},
            ))
    if len(output) != len(mapping):
        raise RuntimeError(f"Breast crosswalk mismatch: {len(output)} ZIP files versus {len(mapping)} workbook filenames")
    return output


def cdd_rows(decision: dict[str, Any], workbook_json: dict[str, Any]) -> list[dict[str, Any]]:
    values = next(sheet["values"] for sheet in workbook_json["cdd_annotations.xlsx"] if sheet["name"] == "all")
    headers = values[0]
    output = []
    for values_row in values[1:]:
        record = dict(zip(headers, values_row))
        image_name = str(record.get("Image_name") or "").strip()
        patient_id = str(int(record["Patient_ID"]))
        if not image_name:
            continue
        file_name = image_name + ".jpg"
        output.append(row(
            dataset_type="Collection", short_title="CDD-CESM", download_id="41789",
            subject_id=f"P{patient_id}", raw_subject_id=patient_id,
            subject_id_namespace="tcia_dataset:CDD-CESM",
            participant_link_status="reviewed_source_crosswalk", package_path=file_name,
            file_name=file_name, file_format="JPG", media_kind="still_image",
            imaging_domain="radiology", modality="MG", object_role="source_image",
            source_system="tcia_aspera",
            source_url="https://faspex.cancerimagingarchive.net/aspera/faspex/public/package",
            crosswalk_source_url=decision["evidence_url"],
            crosswalk_method="manual_annotation_image_name_to_patient_id", crosswalk_confidence="high",
            reviewer_note=decision["reviewer_note"], raw_values_json=record,
            provenance_json={"crosswalk": "cdd_annotations.xlsx", "sheet": "all", "inventory_basis": "metadata_declared_filename"},
            quality_flag_json={"package_path_not_live_browsed": True},
        ))
    return output


def aspera_rows(source_dir: Path, decision: dict[str, Any], *, kind: str) -> list[dict[str, Any]]:
    if kind == "capsule":
        file_path = source_dir / "Capsule-Endoscopy-SB-NET_52333_listing.csv"
        short_title, download_id, domain, modality = "Capsule-Endoscopy-SB-NET", "52333", "endoscopy", ""
    else:
        file_path = source_dir / "Pedi-Cranial-CT-Healthy_55262_listing.csv"
        short_title, download_id, domain, modality = "Pedi-Cranial-CT-Healthy", "55262", "radiology", "CT"
    output = []
    demographics = {}
    metadata_titles = set()
    if kind == "pedi":
        with (source_dir / "pedi_demographics.tsv").open(newline="") as handle:
            demographics = {int(item["Id"]): item for item in csv.DictReader(handle, delimiter="\t")}
        with (source_dir / "pedi_image_metadata.tsv").open(newline="") as handle:
            metadata_titles = {item["file_title"] for item in csv.DictReader(handle, delimiter="\t")}
    with file_path.open(newline="") as handle:
        for item in csv.reader(handle):
            if len(item) < 10 or item[2] != "symbolic_link":
                continue
            package_path, file_name = item[0].lstrip("/"), item[1]
            file_format = normalize_extension(file_name)
            if kind == "capsule":
                match = re.search(r"(?:^|/)(SBNET_\d{2})(?:/|_)", "/" + package_path, re.IGNORECASE)
                if not match:
                    raise RuntimeError(f"Capsule filename lacks patient folder: {package_path}")
                subject_id = match.group(1).upper()
                raw_subject_id = subject_id
                media = "video" if file_format == "MPG" else "still_image"
                role = "source_image"
                method = "aspera_patient_folder_and_filename"
                crosswalk_url = decision["evidence_url"]
            else:
                match = re.fullmatch(r"(\d{5})_CT\.mha", file_name, re.IGNORECASE)
                if not match:
                    raise RuntimeError(f"Pediatric filename does not match reviewed rule: {file_name}")
                padded = match.group(1)
                numeric = int(padded)
                if numeric not in demographics or f"{padded}_CT" not in metadata_titles:
                    raise RuntimeError(f"Pediatric ID lacks TSV evidence: {file_name}")
                subject_id, raw_subject_id = str(numeric), padded
                media, role = "image_volume", "source_image"
                method = "zero_padded_filename_to_demographics_id"
                crosswalk_url = decision["evidence_url"]
            output.append(row(
                dataset_type="Collection", short_title=short_title, download_id=download_id,
                subject_id=subject_id, raw_subject_id=raw_subject_id,
                subject_id_namespace=f"tcia_dataset:{short_title}",
                participant_link_status="reviewed_source_crosswalk", package_path=package_path,
                file_name=file_name, file_format=file_format, media_kind=media,
                imaging_domain=domain, modality=modality, object_role=role,
                size_bytes=int(item[3]), source_system="tcia_aspera", source_url="",
                crosswalk_source_url=crosswalk_url, crosswalk_method=method,
                crosswalk_confidence="high", reviewer_note=decision["reviewer_note"],
                raw_values_json={"aspera_entry_type": item[2], "aspera_target_path": item[6]},
                provenance_json={"inventory": file_path.name, "review_source": "reviewer_workbook"},
            ))
    return output


def prostate_rows(source_dir: Path, decisions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    packages = [
        ("prostate3t_mha.zip", "Prostate-3T", "43157"),
        ("prostate3t_nrrd.zip", "Prostate-3T", "43159"),
        ("prostatedx_nrrd.zip", "PROSTATE-DIAGNOSIS", "43169"),
        ("prostatedx_isbi.zip", "PROSTATE-DIAGNOSIS", "43171"),
        ("prostatedx_mha.zip", "PROSTATE-DIAGNOSIS", "43173"),
    ]
    output = []
    for archive_name, short_title, download_id in packages:
        archive = source_dir / archive_name
        decision = decisions[short_title]
        pattern = r"(Prostate3T-01-\d{4})" if short_title == "Prostate-3T" else r"(ProstateDx-01-\d{4})"
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                file_format = normalize_extension(info.filename)
                if info.is_dir() or file_format not in {"MHA", "NRRD"}:
                    continue
                file_name = Path(info.filename).name
                match = re.search(pattern, file_name)
                if not match:
                    raise RuntimeError(f"Prostate filename lacks reviewed Patient ID: {file_name}")
                output.append(row(
                    dataset_type="Collection", short_title=short_title, download_id=download_id,
                    subject_id=match.group(1), raw_subject_id=match.group(1),
                    subject_id_namespace=f"tcia_dataset:{short_title}",
                    participant_link_status="reviewed_source_crosswalk", package_path=info.filename,
                    file_name=file_name, file_format=file_format, media_kind="image_volume",
                    imaging_domain="imaging_annotation", modality="MR", object_role="segmentation",
                    size_bytes=info.file_size, source_system="tcia_wordpress", source_url="",
                    crosswalk_source_url=decision["evidence_url"],
                    crosswalk_method="filename_embedded_dicom_patient_id", crosswalk_confidence="high",
                    reviewer_note=decision["reviewer_note"],
                    raw_values_json={"archive": archive_name},
                    provenance_json={"inventory": archive_name, "review_source": "reviewer_workbook"},
                ))
    return output


def aurora_rows(source_dir: Path, decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve AURORA pathology files using the published naming schema/workbook."""

    listing_path = source_dir / "aurora_pathology_listing.csv"
    hla_path = source_dir / "aurora_hla_linkage.csv"
    usage_notes_url = decision["evidence_url"]
    clinical_url = decision["supporting_evidence_url"]
    he_pattern = re.compile(
        r"^AUR-(?P<patient>[A-Z0-9]{4})-"
        r"(?P<sample_short_code>[A-Z]+)(?P<biospecimen_ordinal>\d+)-"
        r"(?P<vial_identifier>[A-Z])-(?P<portion_identifier>\d+)-"
        r"(?P<subportion_identifier>\d+)-S(?P<slide_number>\d+)\."
        r"(?P<uuid>[0-9A-F-]+)\.svs$",
        re.IGNORECASE,
    )

    with hla_path.open(newline="") as handle:
        hla_rows = list(csv.DictReader(handle))
    hla_by_slide_token: dict[str, dict[str, str]] = {}
    for item in hla_rows:
        slide_id = str(item.get("Slide ID") or "").strip()
        match = re.fullmatch(r"HLAA_CK_(\d+-\d+)", slide_id, re.IGNORECASE)
        if not match:
            raise RuntimeError(f"AURORA HLA linkage has an unexpected Slide ID: {slide_id}")
        token = match.group(1)
        if token in hla_by_slide_token:
            raise RuntimeError(f"AURORA HLA linkage repeats slide token: {token}")
        hla_by_slide_token[token] = item

    output: list[dict[str, Any]] = []
    matched_hla_tokens: set[str] = set()
    with listing_path.open(newline="") as handle:
        for item in csv.DictReader(handle):
            package_path = str(item.get("package_path") or "").strip()
            file_name = str(item.get("file_name") or Path(package_path).name).strip()
            file_ext = str(item.get("file_ext") or Path(file_name).suffix).casefold()
            size = str(item.get("size_bytes") or "").strip()
            size_bytes = int(size) if size.isdigit() else ""
            if file_ext == ".svs":
                match = he_pattern.fullmatch(file_name)
                if not match:
                    raise RuntimeError(f"AURORA SVS filename does not match published schema: {file_name}")
                values = {key: value for key, value in match.groupdict().items()}
                patient_id = values["patient"].upper()
                raw_patient_id = f"AUR-{patient_id}"
                raw_values = {
                    "project_name": "AUR",
                    "patient_identifier": values["patient"].upper(),
                    "sample_short_code": values["sample_short_code"].upper(),
                    "biospecimen_ordinal": int(values["biospecimen_ordinal"]),
                    "vial_identifier": values["vial_identifier"].upper(),
                    "portion_identifier": int(values["portion_identifier"]),
                    "subportion_identifier": int(values["subportion_identifier"]),
                    "slide_number": int(values["slide_number"]),
                    "uuid": values["uuid"].upper(),
                }
                method = "published_filename_schema_patient_identifier"
                crosswalk_url = usage_notes_url
                provenance = {
                    "inventory": listing_path.name,
                    "naming_schema": "WordPress Usage Notes",
                }
            elif file_ext in {".tif", ".tiff"}:
                tokens = re.findall(r"(?<!\d)(\d{6}-\d)(?!\d)", file_name)
                matches = [hla_by_slide_token[token] for token in tokens if token in hla_by_slide_token]
                if len(matches) != 1:
                    raise RuntimeError(
                        f"AURORA TIFF filename has {len(matches)} HLA linkage matches: {file_name}"
                    )
                linkage = matches[0]
                matched_hla_tokens.add(str(linkage["Slide ID"]).split("HLAA_CK_", 1)[-1])
                raw_patient_id = str(linkage["Patient ID"]).strip().upper()
                if not re.fullmatch(r"AUR-[A-Z0-9]{4}", raw_patient_id):
                    raise RuntimeError(
                        f"AURORA HLA linkage has an unexpected Patient ID: {raw_patient_id}"
                    )
                patient_id = raw_patient_id.removeprefix("AUR-")
                raw_values = dict(linkage)
                method = "clinical_workbook_slide_id_to_patient_id"
                crosswalk_url = clinical_url
                provenance = {
                    "inventory": listing_path.name,
                    "crosswalk": hla_path.name,
                    "workbook_sheet": "5.HLA sample ID linkage",
                }
            else:
                continue

            output.append(row(
                dataset_type="Collection",
                short_title="AURORA-Metastatic-Breast-Multiomics",
                download_id="52487",
                subject_id=patient_id,
                raw_subject_id=raw_patient_id,
                subject_id_namespace="tcia_dataset:AURORA-Metastatic-Breast-Multiomics",
                participant_link_status="reviewed_source_crosswalk",
                package_path=package_path,
                file_name=file_name,
                file_format="TIFF" if file_ext in {".tif", ".tiff"} else "SVS",
                media_kind="whole_slide_image",
                imaging_domain="pathology",
                modality="SM",
                object_role="source_image",
                size_bytes=size_bytes,
                source_system="tcia_aspera",
                source_url="",
                crosswalk_source_url=crosswalk_url,
                crosswalk_method=method,
                crosswalk_confidence="high",
                reviewer_note=decision["reviewer_note"],
                raw_values_json=raw_values,
                provenance_json=provenance,
            ))

    if len(output) != 289:
        raise RuntimeError(f"AURORA crosswalk expected 289 image files, found {len(output)}")
    if matched_hla_tokens != set(hla_by_slide_token):
        missing = sorted(set(hla_by_slide_token) - matched_hla_tokens)
        raise RuntimeError(f"AURORA HLA linkage rows missing from TIFF inventory: {missing}")
    if len({item["subject_id"] for item in output}) != 54:
        raise RuntimeError("AURORA crosswalk expected pathology files for 54 patients")
    return output


def ldct_rows(source_dir: Path, decision: dict[str, Any], workbook_json: dict[str, Any]) -> list[dict[str, Any]]:
    workbook_names = {
        "CR_Abdomen_v4 2026_07_21.zip": "CR_Abdomen_v4_2026_07_21__Abdomen_v11 2026.xlsx",
        "CR_Chest_v4 2026_07_21.zip": "CR_Chest_v4_2026_07_21__Chest_v11 2026.xlsx",
        "CR_Head_v4 2026_07_21.zip": "CR_Head_v4_2026_07_21__Neuro_v11 2026.xlsx",
    }
    mapping = {}
    for nested_name, workbook_name in workbook_names.items():
        values = workbook_json[workbook_name][0]["values"]
        headers = values[1]
        for values_row in values[2:]:
            record = dict(zip(headers, values_row))
            snapshot = str(record.get("Snapshot_Filename") or "").strip()
            case = str(record.get("Case") or "").strip()
            series_uid = str(record.get("SeriesUID") or "").strip()
            if snapshot:
                mapping[(nested_name, Path(snapshot).stem.casefold())] = (case, series_uid, record)
    output = []
    outer_path = source_dir / "ldct_clinical.zip"
    with zipfile.ZipFile(outer_path) as outer:
        for nested_name in workbook_names:
            with zipfile.ZipFile(io.BytesIO(outer.read(nested_name))) as nested:
                for info in nested.infolist():
                    if info.is_dir() or normalize_extension(info.filename) not in {"JPG", "JPEG"}:
                        continue
                    key = (nested_name, Path(info.filename).stem.casefold())
                    if key not in mapping:
                        raise RuntimeError(f"LDCT JPG lacks companion workbook row: {nested_name}!/{info.filename}")
                    case, series_uid, record = mapping[key]
                    output.append(row(
                        dataset_type="Collection", short_title="LDCT-and-Projection-data", download_id="42661",
                        subject_id=case, raw_subject_id=case,
                        subject_id_namespace="tcia_dataset:LDCT-and-Projection-data",
                        participant_link_status="reviewed_source_crosswalk",
                        package_path=f"{nested_name}!/{info.filename}", file_name=Path(info.filename).name,
                        file_format="JPG", media_kind="still_image", imaging_domain="other_imaging",
                        modality="CT", object_role="annotation_snapshot", size_bytes=info.file_size,
                        source_system="tcia_wordpress",
                        source_url="https://www.cancerimagingarchive.net/wp-content/uploads/LDCT-and-Projection-Data_CR_HeadChestAbdomen_v4.zip",
                        crosswalk_source_url=decision["evidence_url"],
                        crosswalk_method="companion_workbook_snapshot_filename_to_case_and_series_uid",
                        crosswalk_confidence="high", reviewer_note=decision["reviewer_note"],
                        dicom_series_instance_uid=series_uid, raw_values_json=record,
                        provenance_json={"inventory": outer_path.name, "nested_archive": nested_name},
                        quality_flag_json={"case_id_requires_series_uid_bridge_for_dicom_patient_join": True},
                    ))
    return output


def write_rows(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(materialized)
    return len(materialized)


def build(source_dir: Path, curation_path: Path, out: Path, manifest_path: Path) -> dict[str, Any]:
    curation = json.loads(curation_path.read_text())
    decisions = decisions_by_title(curation)
    workbook_json_path = source_dir / "crosswalk_workbooks.json"
    workbook_json = json.loads(workbook_json_path.read_text())
    rows = []
    rows.extend(breast_rows(source_dir, decisions["Breast-Lesions-USG"], workbook_json))
    rows.extend(cdd_rows(decisions["CDD-CESM"], workbook_json))
    rows.extend(aspera_rows(source_dir, decisions["Capsule-Endoscopy-SB-NET"], kind="capsule"))
    rows.extend(aspera_rows(source_dir, decisions["Pedi-Cranial-CT-Healthy"], kind="pedi"))
    rows.extend(ldct_rows(source_dir, decisions["LDCT-and-Projection-data"], workbook_json))
    rows.extend(prostate_rows(source_dir, decisions))
    rows.extend(aurora_rows(source_dir, decisions["AURORA-Metastatic-Breast-Multiomics"]))
    rows.extend(explicit_mapping_rows(curation))
    rows.sort(key=lambda item: (item["short_title"].casefold(), item["subject_id"], item["package_path"]))
    count = write_rows(out, rows)
    source_files = sorted({
        "breast_images.zip", "breast_clinical.xlsx", "cdd_annotations.xlsx",
        "Capsule-Endoscopy-SB-NET_52333_listing.csv", "Pedi-Cranial-CT-Healthy_55262_listing.csv",
        "pedi_demographics.tsv", "pedi_image_metadata.tsv", "ldct_clinical.zip",
        "prostate3t_mha.zip", "prostate3t_nrrd.zip", "prostatedx_nrrd.zip",
        "prostatedx_isbi.zip", "prostatedx_mha.zip", "crosswalk_workbooks.json",
        "aurora_pathology_listing.csv", "aurora_hla_linkage.csv", "aurora_clinical.xlsx",
    })
    counts: dict[str, dict[str, int]] = {}
    for item in rows:
        entry = counts.setdefault(item["short_title"], {"files": 0, "participants": 0})
        entry["files"] += 1
    for title in counts:
        counts[title]["participants"] = len({item["subject_id"] for item in rows if item["short_title"] == title})
    manifest = {
        "schema_version": 1,
        "reviewed_at": max(
            [str(curation["reviewed_at"])]
            + [str(item.get("reviewed_at")) for item in curation["decisions"] if item.get("reviewed_at")]
        ),
        "row_count": count,
        "crosswalk_csv": {"path": str(out), "sha256": sha256(out), "bytes": out.stat().st_size},
        "curation": {"path": str(curation_path), "sha256": sha256(curation_path)},
        "source_files": [
            {"name": name, "sha256": sha256(source_dir / name), "bytes": (source_dir / name).stat().st_size}
            for name in source_files
        ],
        "dataset_counts": counts,
        "acknowledged_without_crosswalk": ["Bone-Marrow-Cytomorphology_MLL_Helmholtz_Fraunhofer"],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    result.add_argument("--curation", type=Path, default=DEFAULT_CURATION)
    result.add_argument("--out", type=Path, default=DEFAULT_OUT)
    result.add_argument("--manifest-out", type=Path, default=DEFAULT_MANIFEST)
    return result


if __name__ == "__main__":
    args = parser().parse_args()
    print(json.dumps(build(args.source_dir, args.curation, args.out, args.manifest_out), indent=2))
