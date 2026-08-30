#!/usr/bin/env python3
"""Plan, download, analyze, and merge TCIA non-IDC geometry batch jobs.

The planner reads the V2 public non-DICOM detail database and creates one job
per public download route.  Download URLs are deliberately retained only in a
mode-0600 private JSONL plan; the shareable CSV summary omits them.

Default geometry analysis is header-only for public non-DICOM volume formats:

* NIfTI: nibabel header/affine inspection without loading the data array.
* MHA/MHD/NRRD: SimpleITK ``ReadImageInformation()`` without reading pixels.

The DICOM parser remains available only for explicitly requested diagnostic
runs. DICOM rows are not part of the public-non-DICOM artifact; public DICOM
geometry belongs in IDC and idc-index.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import urlparse


DEFAULT_FORMATS = ("NIFTI", "MHA", "MHD", "NRRD")
FILE_SUFFIX_FORMATS = {
    ".nii": "NIFTI",
    ".nii.gz": "NIFTI",
    ".mha": "MHA",
    ".mhd": "MHD",
    ".nrrd": "NRRD",
    ".dcm": "DICOM",
    ".dicom": "DICOM",
}
ANALYZER_VERSION = "1"
VOLUME_SOP_CLASS_UIDS = {
    "1.2.840.10008.5.1.4.1.1.2",  # CT Image Storage
    "1.2.840.10008.5.1.4.1.1.4",  # MR Image Storage
    "1.2.840.10008.5.1.4.1.1.128",  # PET Image Storage
}


def canonical_download_ids(value: object) -> tuple[str, ...]:
    """Return stable download IDs from scalar, JSON-array, or delimited input."""
    text = str(value or "").strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        values = parsed
    elif parsed is not None and not isinstance(parsed, (dict, list)):
        values = [parsed]
    else:
        values = re.split(r"[,;]", text)
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


def stable_id(*parts: object, prefix: str = "job") -> str:
    payload = "\x1f".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return slug or "dataset"


def detect_route(url: str) -> str:
    lower = url.lower()
    if "faspex" in lower or "aspera" in lower:
        # TCIA's current Faspex 5 service accepts exact published links that
        # retain legacy-looking /aspera/faspex paths. URL path shape alone
        # therefore cannot identify the obsolete Faspex 4 plugin.
        return "aspera_faspex5_public_link"
    if lower.startswith(("https://", "http://")):
        return "http"
    return "unsupported"


def ensure_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def plan_jobs(
    db_path: Path,
    out_dir: Path,
    formats: Sequence[str],
    datasets: Sequence[str] = (),
) -> dict[str, Any]:
    selected_formats = tuple(sorted({item.upper() for item in formats}))
    if not selected_formats:
        raise ValueError("at least one format is required")
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = out_dir / "job_manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    with connect_readonly(db_path) as conn:
        table_names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {"public_non_dicom_assets", "public_non_dicom_locations"}
        missing = required - table_names
        if missing:
            raise ValueError(f"missing required V2 tables: {sorted(missing)}")

        placeholders = ",".join("?" for _ in selected_formats)
        params: list[object] = list(selected_formats)
        dataset_clause = ""
        if datasets:
            dataset_clause = " AND a.short_title IN ({})".format(
                ",".join("?" for _ in datasets)
            )
            params.extend(datasets)

        assets = list(
            conn.execute(
                f"""
                SELECT a.*
                FROM public_non_dicom_assets a
                WHERE upper(a.file_format) IN ({placeholders})
                  {dataset_clause}
                ORDER BY a.short_title, a.download_id, a.asset_granularity,
                         a.package_path, a.asset_id
                """,
                params,
            )
        )

        locations_by_asset: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in conn.execute(
            """
            SELECT * FROM public_non_dicom_locations
            WHERE lower(access_level) IN ('open', 'public', 'noncontrolled',
                                           'non-controlled')
              AND availability_status <> 'unavailable'
            ORDER BY asset_id, location_id
            """
        ):
            locations_by_asset[str(row["asset_id"])].append(row)

    # Download-grain rows are the authoritative routes.  File rows may encode
    # their download ID as a JSON list and often omit the repeated URL.
    route_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in assets:
        if str(row["asset_granularity"]) != "download":
            continue
        ids = canonical_download_ids(row["download_id"])
        if not ids:
            continue
        candidate_urls = [str(row["source_url"] or "")]
        candidate_urls.extend(
            str(location["access_url"] or location["manifest_url"] or "")
            for location in locations_by_asset.get(str(row["asset_id"]), [])
        )
        url = next((value for value in candidate_urls if value.startswith("http")), "")
        if not url:
            continue
        access_levels = {
            str(location["access_level"] or "").lower()
            for location in locations_by_asset.get(str(row["asset_id"]), [])
        }
        if access_levels and not access_levels <= {
            "open",
            "public",
            "noncontrolled",
            "non-controlled",
        }:
            continue
        for download_id in ids:
            route_by_key[(str(row["short_title"]), download_id)] = {
                "short_title": str(row["short_title"]),
                "dataset_type": str(row["dataset_type"]),
                "download_id": download_id,
                "source_url": url,
                "route_type": detect_route(url),
                "catalog_size_bytes": int(row["size_bytes"] or 0),
                "catalog_represented_file_count": int(
                    row["represented_file_count"] or 0
                ),
                "formats": {str(row["file_format"]).upper()},
                "route_asset_id": str(row["asset_id"]),
                "assets": [],
            }

    unmatched_assets: list[dict[str, str]] = []
    for row in assets:
        if str(row["asset_granularity"]) == "download":
            continue
        matched = False
        for download_id in canonical_download_ids(row["download_id"]):
            job = route_by_key.get((str(row["short_title"]), download_id))
            if job is None:
                continue
            matched = True
            job["formats"].add(str(row["file_format"]).upper())
            job["assets"].append(
                {
                    "asset_id": str(row["asset_id"]),
                    "asset_granularity": str(row["asset_granularity"]),
                    "file_format": str(row["file_format"]).upper(),
                    "file_name": str(row["file_name"] or ""),
                    "package_path": str(row["package_path"] or ""),
                    "object_role": str(row["object_role"] or ""),
                    "subject_id": str(row["subject_id"] or ""),
                    "represented_file_count": int(row["represented_file_count"] or 0),
                }
            )
        # Some reviewed file inventories carry the ID of the inventory TSV/CSV
        # rather than the payload download.  Use a fallback only when exactly
        # one public route in the same dataset advertises the same format.
        if not matched:
            fallback_jobs = [
                job
                for (short_title, _), job in route_by_key.items()
                if short_title == str(row["short_title"])
                and str(row["file_format"]).upper() in job["formats"]
            ]
            if len(fallback_jobs) == 1:
                matched = True
                fallback_jobs[0]["assets"].append(
                    {
                        "asset_id": str(row["asset_id"]),
                        "asset_granularity": str(row["asset_granularity"]),
                        "file_format": str(row["file_format"]).upper(),
                        "file_name": str(row["file_name"] or ""),
                        "package_path": str(row["package_path"] or ""),
                        "object_role": str(row["object_role"] or ""),
                        "subject_id": str(row["subject_id"] or ""),
                        "represented_file_count": int(row["represented_file_count"] or 0),
                        "route_mapping_method": "unique_dataset_format_download_route",
                        "source_download_ids": list(canonical_download_ids(row["download_id"])),
                    }
                )
        if not matched:
            unmatched_assets.append(
                {
                    "asset_id": str(row["asset_id"]),
                    "short_title": str(row["short_title"]),
                    "download_id": str(row["download_id"] or ""),
                }
            )

    jobs: list[dict[str, Any]] = []
    for job in route_by_key.values():
        job_id = stable_id(
            job["short_title"], job["download_id"], job["source_url"]
        )
        manifest_path = manifest_dir / f"{job_id}.jsonl.gz"
        with gzip.open(manifest_path, "wt", encoding="utf-8") as stream:
            for asset in job.pop("assets"):
                stream.write(json.dumps(asset, sort_keys=True) + "\n")
        job["job_id"] = job_id
        job["formats"] = sorted(job["formats"])
        job["asset_manifest"] = manifest_path.relative_to(out_dir).as_posix()
        job["asset_rows"] = sum(1 for _ in iter_jsonl(manifest_path))
        jobs.append(job)

    jobs.sort(key=lambda item: (item["short_title"].casefold(), item["download_id"]))
    for index, job in enumerate(jobs):
        job["array_index"] = index

    private_plan = out_dir / "jobs.private.jsonl"
    with private_plan.open("w", encoding="utf-8") as stream:
        for job in jobs:
            stream.write(json.dumps(job, sort_keys=True) + "\n")
    ensure_private(private_plan)

    summary_csv = out_dir / "jobs.csv"
    fields = [
        "array_index",
        "job_id",
        "dataset_type",
        "short_title",
        "download_id",
        "route_type",
        "formats",
        "asset_rows",
        "catalog_represented_file_count",
        "catalog_size_bytes",
    ]
    with summary_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for job in jobs:
            safe = dict(job)
            safe["formats"] = ";".join(job["formats"])
            writer.writerow({field: safe.get(field, "") for field in fields})

    unmatched_path = out_dir / "unmatched_assets.jsonl"
    with unmatched_path.open("w", encoding="utf-8") as stream:
        for row in unmatched_assets:
            stream.write(json.dumps(row, sort_keys=True) + "\n")

    summary = {
        "schema_version": 1,
        "created_at_utc": utc_now(),
        "source_db": str(db_path.resolve()),
        "formats": list(selected_formats),
        "job_count": len(jobs),
        "array_max": len(jobs) - 1,
        "route_counts": count_values(job["route_type"] for job in jobs),
        "catalog_size_bytes": sum(job["catalog_size_bytes"] for job in jobs),
        "catalog_represented_file_count": sum(
            job["catalog_represented_file_count"] for job in jobs
        ),
        "unmatched_asset_rows": len(unmatched_assets),
        "private_plan": str(private_plan.resolve()),
        "shareable_jobs_csv": str(summary_csv.resolve()),
    }
    (out_dir / "plan_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def load_job(plan_path: Path, index: int) -> dict[str, Any]:
    for current, row in enumerate(iter_jsonl(plan_path)):
        if current == index:
            row["_plan_dir"] = str(plan_path.resolve().parent)
            return row
    raise IndexError(f"array index {index} not found in {plan_path}")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def count_values(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for value in values:
        result[value] += 1
    return dict(sorted(result.items()))


def run_checked(command: Sequence[str], *, cwd: Path) -> None:
    completed = subprocess.run(list(command), cwd=cwd, check=False)
    if completed.returncode:
        raise RuntimeError(
            f"command failed with exit code {completed.returncode}: {command[0]}"
        )


def safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"unsafe ZIP member: {member.filename}")
            mode = member.external_attr >> 16
            if (mode & 0o170000) == 0o120000:
                raise ValueError(f"ZIP symlink is not allowed: {member.filename}")
        bundle.extractall(destination)


def safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"unsafe TAR member: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"TAR link is not allowed: {member.name}")
        bundle.extractall(destination)


def download_job(job: dict[str, Any], data_root: Path) -> Path:
    target = data_root / f"{safe_slug(job['short_title'])}--{job['job_id']}"
    target.mkdir(parents=True, exist_ok=True)
    marker = target / ".download_complete.json"
    if marker.is_file():
        return target

    route = job["route_type"]
    url = job["source_url"]
    if route in {"aspera_faspex4_public_link", "aspera_faspex5_public_link"}:
        if shutil.which("ascli") is None:
            raise RuntimeError("ascli is required for this Aspera job")
        # Accept the earlier planner label as a compatibility alias, but use
        # the Faspex 5 public-link workflow validated against TCIA's service.
        route = "aspera_faspex5_public_link"
        command = [
            "ascli",
            "faspex5",
            "packages",
            "receive",
            f"--url={url}",
        ]
        run_checked(command, cwd=target)
    elif route == "http":
        if shutil.which("curl") is None:
            raise RuntimeError("curl is required for HTTP downloads")
        parsed_name = Path(urlparse(url).path).name
        archive_name = parsed_name if parsed_name else "payload.zip"
        archive = target / archive_name
        curl_command = [
            "curl",
            "-L",
            "--fail",
            "--retry",
            "5",
        ]
        curl_help = subprocess.run(
            ["curl", "--help", "all"],
            check=False,
            capture_output=True,
            text=True,
        )
        if "--retry-all-errors" in curl_help.stdout:
            curl_command.append("--retry-all-errors")
        curl_command.extend(
            [
                "--continue-at",
                "-",
                "--output",
                str(archive),
                url,
            ]
        )
        run_checked(curl_command, cwd=target)
        extracted = target / "extracted"
        lower = archive.name.lower()
        if lower.endswith(".zip"):
            safe_extract_zip(archive, extracted)
        elif lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
            safe_extract_tar(archive, extracted)
    else:
        raise RuntimeError(f"unsupported route type: {route}")

    marker.write_text(
        json.dumps(
            {
                "job_id": job["job_id"],
                "completed_at_utc": utc_now(),
                "route_type": route,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def suffix_format(path: Path) -> str | None:
    lower = path.name.lower()
    if lower.endswith(".nii.gz"):
        return "NIFTI"
    return FILE_SUFFIX_FORMATS.get(path.suffix.lower())


def finite_list(values: Sequence[object]) -> list[float]:
    result = [float(value) for value in values]
    if not all(math.isfinite(value) for value in result):
        raise ValueError("geometry contains non-finite values")
    return result


def matrix_is_orthonormal(values: Sequence[float], dimension: int, tol: float = 1e-4) -> bool:
    if len(values) != dimension * dimension:
        return False
    rows = [values[i * dimension : (i + 1) * dimension] for i in range(dimension)]
    for i, row in enumerate(rows):
        norm = math.sqrt(sum(value * value for value in row))
        if abs(norm - 1.0) > tol:
            return False
        for j in range(i):
            dot = sum(a * b for a, b in zip(row, rows[j]))
            if abs(dot) > tol:
                return False
    return True


def analyze_nifti(path: Path) -> dict[str, Any]:
    try:
        import nibabel as nib  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise RuntimeError("nibabel and numpy are required for NIfTI analysis") from exc

    image = nib.load(str(path), mmap=True)
    header = image.header
    shape = [int(value) for value in image.shape]
    zooms = finite_list(header.get_zooms()[: len(shape)])
    affine = np.asarray(image.affine, dtype=float)
    finite_affine = bool(np.isfinite(affine).all())
    spatial_rank = min(3, len(shape))
    basis = affine[:spatial_rank, :spatial_rank]
    determinant = float(np.linalg.det(basis)) if spatial_rank else 0.0
    qform, qform_code = image.get_qform(coded=True)
    sform, sform_code = image.get_sform(coded=True)
    forms_consistent: bool | None = None
    if qform_code and sform_code and qform is not None and sform is not None:
        forms_consistent = bool(np.allclose(qform, sform, rtol=1e-5, atol=1e-4))
    checks = {
        "finite_affine": finite_affine,
        "positive_spacing": all(value > 0 for value in zooms[:spatial_rank]),
        "nonzero_spatial_extent": all(value > 0 for value in shape[:spatial_rank]),
        "nonsingular_spatial_affine": math.isfinite(determinant) and abs(determinant) > 1e-12,
        "qform_sform_consistent": forms_consistent,
    }
    valid = all(value is not False for value in checks.values())
    return {
        "geometry_status": "checked_grid_geometry" if valid else "checked_invalid_geometry",
        "dimension": len(shape),
        "shape": shape,
        "spacing": zooms,
        "origin": finite_list(affine[:spatial_rank, 3]),
        "direction": finite_list(basis.reshape(-1)),
        "checks": checks,
        "details": {
            "qform_code": int(qform_code or 0),
            "sform_code": int(sform_code or 0),
            "spatial_affine_determinant": determinant,
        },
    }


def analyze_itk_header(path: Path) -> dict[str, Any]:
    try:
        import SimpleITK as sitk  # type: ignore
    except ImportError as exc:
        raise RuntimeError("SimpleITK is required for MHA/MHD/NRRD analysis") from exc

    reader = sitk.ImageFileReader()
    reader.SetFileName(str(path))
    reader.ReadImageInformation()
    dimension = int(reader.GetDimension())
    shape = [int(value) for value in reader.GetSize()]
    spacing = finite_list(reader.GetSpacing())
    origin = finite_list(reader.GetOrigin())
    direction = finite_list(reader.GetDirection())
    checks = {
        "positive_spacing": all(value > 0 for value in spacing),
        "nonzero_extent": all(value > 0 for value in shape),
        "orthonormal_direction": matrix_is_orthonormal(direction, dimension),
    }
    valid = all(checks.values())
    return {
        "geometry_status": "checked_grid_geometry" if valid else "checked_invalid_geometry",
        "dimension": dimension,
        "shape": shape,
        "spacing": spacing,
        "origin": origin,
        "direction": direction,
        "checks": checks,
        "details": {"pixel_id": int(reader.GetPixelID())},
    }


def tuple_close(left: Sequence[float], right: Sequence[float], tol: float) -> bool:
    return len(left) == len(right) and all(abs(a - b) <= tol for a, b in zip(left, right))


def vector_cross(left: Sequence[float], right: Sequence[float]) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def dicom_series_geometry(instances: list[dict[str, Any]]) -> dict[str, Any]:
    excluded_count = sum(
        bool(instance["excluded_localizer_or_mip"]) for instance in instances
    )
    if excluded_count == len(instances):
        return {
            "geometry_status": "not_applicable_localizer_or_mip",
            "checks": {},
            "details": {"instance_count": len(instances)},
        }
    sop_classes = {instance["sop_class_uid"] for instance in instances}
    if not sop_classes or not sop_classes <= VOLUME_SOP_CLASS_UIDS:
        return {
            "geometry_status": "not_applicable_nonvolume_sop_class",
            "checks": {},
            "details": {
                "instance_count": len(instances),
                "sop_class_uids": sorted(sop_classes),
            },
        }
    if any(instance["number_of_frames"] > 1 for instance in instances):
        return {
            "geometry_status": "unsupported_multiframe",
            "checks": {},
            "details": {"instance_count": len(instances)},
        }
    complete = [
        item
        for item in instances
        if len(item["orientation"]) == 6
        and len(item["position"]) == 3
        and len(item["pixel_spacing"]) == 2
        and item["rows"] > 0
        and item["columns"] > 0
    ]
    if len(complete) != len(instances):
        return {
            "geometry_status": "missing_geometry",
            "checks": {},
            "details": {
                "instance_count": len(instances),
                "complete_geometry_instances": len(complete),
            },
        }
    if len(complete) < 3:
        return {
            "geometry_status": "insufficient_slices",
            "checks": {},
            "details": {"instance_count": len(complete)},
        }

    reference = complete[0]
    orientation = reference["orientation"]
    row_vector = orientation[:3]
    col_vector = orientation[3:]
    normal = vector_cross(row_vector, col_vector)
    magnitude = math.sqrt(dot(normal, normal))
    if magnitude > 0:
        normal = tuple(value / magnitude for value in normal)

    positions = sorted(dot(item["position"], normal) for item in complete)
    intervals = [right - left for left, right in zip(positions, positions[1:])]
    expected = (positions[-1] - positions[0]) / (len(positions) - 1)
    unique_positions = all(abs(value) > 1e-6 for value in intervals)
    uniform_spacing = bool(expected) and all(
        abs(value - expected) < 0.01 * abs(expected) for value in intervals
    )
    row_positions = [dot(item["position"], row_vector) for item in complete]
    col_positions = [dot(item["position"], col_vector) for item in complete]
    checks = {
        "no_localizer_or_mip_instances": excluded_count == 0,
        "single_orientation": all(
            tuple_close(item["orientation"], orientation, 1e-6) for item in complete
        ),
        "orthogonal_orientation": abs(magnitude - 1.0) <= 0.01,
        "unique_slice_positions": unique_positions,
        "consistent_in_plane_row": max(row_positions) - min(row_positions) < 0.1,
        "consistent_in_plane_col": max(col_positions) - min(col_positions) < 0.1,
        "consistent_pixel_spacing": all(
            tuple_close(item["pixel_spacing"], reference["pixel_spacing"], 1e-6)
            for item in complete
        ),
        "consistent_image_dimensions": all(
            item["rows"] == reference["rows"]
            and item["columns"] == reference["columns"]
            for item in complete
        ),
        "uniform_slice_spacing": uniform_spacing,
    }
    regular = all(checks.values())
    nearest_axis = max(abs(value) for value in normal) if magnitude else 0.0
    obliquity = math.degrees(math.acos(min(1.0, nearest_axis))) if magnitude else None
    return {
        "geometry_status": "checked_regular" if regular else "checked_not_regular",
        "dimension": 3,
        "shape": [reference["rows"], reference["columns"], len(complete)],
        "spacing": [*reference["pixel_spacing"], abs(expected)],
        "origin": reference["position"],
        "direction": [*row_vector, *col_vector, *normal],
        "checks": checks,
        "details": {
            "instance_count": len(complete),
            "expected_slice_spacing": expected,
            "obliquity_degrees": obliquity,
        },
    }


def read_dicom_header(path: Path) -> dict[str, Any] | None:
    try:
        import pydicom  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pydicom is required for DICOM analysis") from exc
    tags = [
        "SOPInstanceUID",
        "SeriesInstanceUID",
        "StudyInstanceUID",
        "SOPClassUID",
        "Modality",
        "ImagePositionPatient",
        "ImageOrientationPatient",
        "PixelSpacing",
        "Rows",
        "Columns",
        "NumberOfFrames",
        "ImageType",
    ]
    try:
        dataset = pydicom.dcmread(
            str(path), stop_before_pixels=True, specific_tags=tags, force=True
        )
    except Exception:
        return None
    series_uid = str(getattr(dataset, "SeriesInstanceUID", "") or "")
    sop_uid = str(getattr(dataset, "SOPInstanceUID", "") or "")
    if not series_uid or not sop_uid:
        return None
    image_type = [str(value).upper() for value in getattr(dataset, "ImageType", [])]
    return {
        "path": str(path),
        "series_uid": series_uid,
        "study_uid": str(getattr(dataset, "StudyInstanceUID", "") or ""),
        "sop_uid": sop_uid,
        "sop_class_uid": str(getattr(dataset, "SOPClassUID", "") or ""),
        "modality": str(getattr(dataset, "Modality", "") or ""),
        "orientation": finite_list(getattr(dataset, "ImageOrientationPatient", [])),
        "position": finite_list(getattr(dataset, "ImagePositionPatient", [])),
        "pixel_spacing": finite_list(getattr(dataset, "PixelSpacing", [])),
        "rows": int(getattr(dataset, "Rows", 0) or 0),
        "columns": int(getattr(dataset, "Columns", 0) or 0),
        "number_of_frames": int(getattr(dataset, "NumberOfFrames", 1) or 1),
        "excluded_localizer_or_mip": any(
            value == "LOCALIZER" or "MIP" in value for value in image_type
        ),
    }


def load_asset_manifest(job: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(job["asset_manifest"])
    if not path.is_absolute():
        path = Path(job.get("_plan_dir") or ".") / path
    return list(iter_jsonl(path)) if path.is_file() else []


def format_tokens(value: object) -> set[str]:
    return {
        token.strip().upper()
        for token in re.split(r"[,;]", str(value or ""))
        if token.strip()
    }


def map_asset_id(
    path: Path,
    root: Path,
    assets: list[dict[str, Any]],
    file_format: str,
) -> str:
    relative = path.relative_to(root).as_posix()
    matches: list[tuple[int, str]] = []
    for asset in assets:
        if file_format not in format_tokens(asset.get("file_format")):
            continue
        package_path = str(asset.get("package_path") or "").lstrip("/")
        path_with_boundaries = f"/{relative.strip('/')}/"
        package_with_boundaries = f"/{package_path.strip('/')}/"
        if package_path and package_with_boundaries in path_with_boundaries:
            matches.append((len(package_path), str(asset["asset_id"])))
    if matches:
        longest = max(length for length, _ in matches)
        asset_ids = {asset_id for length, asset_id in matches if length == longest}
        return next(iter(asset_ids)) if len(asset_ids) == 1 else ""
    download_assets = {
        str(asset["asset_id"])
        for asset in assets
        if str(asset.get("asset_granularity") or "") == "download"
        and file_format in format_tokens(asset.get("file_format"))
    }
    return next(iter(download_assets)) if len(download_assets) == 1 else ""


def assessment_base(job: dict[str, Any], local_path: str, file_format: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "analyzer": "tcia_geometry_batch",
        "analyzer_version": ANALYZER_VERSION,
        "assessed_at_utc": utc_now(),
        "job_id": job["job_id"],
        "dataset_type": job["dataset_type"],
        "short_title": job["short_title"],
        "download_id": job["download_id"],
        "asset_id": "",
        "local_relative_path": local_path,
        "file_format": file_format,
        "assessment_scope": "file",
        "series_instance_uid": "",
        "study_instance_uid": "",
        "geometry_status": "not_checked",
        "dimension": None,
        "shape": [],
        "spacing": [],
        "origin": [],
        "direction": [],
        "checks": {},
        "details": {},
        "error": "",
    }


def analyze_job(job: dict[str, Any], data_root: Path, results_dir: Path) -> Path:
    root = data_root / f"{safe_slug(job['short_title'])}--{job['job_id']}"
    complete_marker = root / ".download_complete.json"
    partial_marker = root / ".download_partial.json"
    if not complete_marker.is_file() and not partial_marker.is_file():
        raise RuntimeError(
            f"download is not complete or documented partial: {root}"
        )

    # Aspera packages may deliver ZIP archives without unpacking them.
    extraction_marker = root / ".archive_extraction_complete.json"
    if (
        job["route_type"] in {
            "aspera_faspex4_public_link",
            "aspera_faspex5_public_link",
        }
        and not extraction_marker.is_file()
    ):
        archives = sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
            and "extracted" not in path.relative_to(root).parts
            and path.name.lower().endswith(".zip")
        )
        for archive in archives:
            safe_extract_zip(archive, root / "extracted")
        extraction_marker.write_text(
            json.dumps(
                {
                    "completed_at_utc": utc_now(),
                    "archives": [
                        archive.relative_to(root).as_posix()
                        for archive in archives
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    results_dir.mkdir(parents=True, exist_ok=True)
    output = results_dir / f"{job['job_id']}.jsonl.gz"
    temp = output.with_suffix(output.suffix + ".part")
    assets = load_asset_manifest(job)
    selected_formats = set(job["formats"])
    dicom_instances: dict[str, list[dict[str, Any]]] = defaultdict(list)
    dicom_paths: dict[str, list[Path]] = defaultdict(list)

    with gzip.open(temp, "wt", encoding="utf-8") as stream:
        for path in sorted(candidate_files(root)):
            detected = suffix_format(path)
            if detected in {"NIFTI", "MHA", "MHD", "NRRD"}:
                if detected not in selected_formats:
                    continue
                row = assessment_base(
                    job, path.relative_to(root).as_posix(), detected
                )
                row["asset_id"] = map_asset_id(path, root, assets, detected)
                try:
                    result = (
                        analyze_nifti(path)
                        if detected == "NIFTI"
                        else analyze_itk_header(path)
                    )
                    row.update(result)
                except Exception as exc:
                    row["geometry_status"] = "read_error"
                    row["error"] = f"{type(exc).__name__}: {exc}"
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                continue

            if "DICOM" not in selected_formats:
                continue
            # Try likely DICOM files only.  Extensionless files are common;
            # archive/log/sidecar files are excluded to avoid wasteful probes.
            if path.suffix.lower() not in {"", ".dcm", ".dicom", ".ima"}:
                continue
            try:
                header = read_dicom_header(path)
            except Exception as exc:
                row = assessment_base(
                    job, path.relative_to(root).as_posix(), "DICOM"
                )
                row["geometry_status"] = "dependency_error"
                row["error"] = f"{type(exc).__name__}: {exc}"
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                break
            if header is None:
                continue
            dicom_instances[header["series_uid"]].append(header)
            dicom_paths[header["series_uid"]].append(path)

        for series_uid, instances in sorted(dicom_instances.items()):
            common_root = Path(os.path.commonpath([str(path) for path in dicom_paths[series_uid]]))
            local_path = (
                common_root.relative_to(root).as_posix()
                if common_root != root
                else "."
            )
            row = assessment_base(job, local_path, "DICOM")
            row["assessment_scope"] = "dicom_series"
            row["series_instance_uid"] = series_uid
            row["study_instance_uid"] = instances[0]["study_uid"]
            row["asset_id"] = map_asset_id(
                dicom_paths[series_uid][0], root, assets, "DICOM"
            )
            try:
                row.update(dicom_series_geometry(instances))
            except Exception as exc:
                row["geometry_status"] = "analysis_error"
                row["error"] = f"{type(exc).__name__}: {exc}"
            row["details"] = {
                **row.get("details", {}),
                "modality": instances[0]["modality"],
                "sop_class_uid": instances[0]["sop_class_uid"],
            }
            stream.write(json.dumps(row, sort_keys=True) + "\n")

    temp.replace(output)
    return output


def candidate_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or path.name.startswith(".download_"):
            continue
        if path.name.startswith("._") or "__MACOSX" in path.parts:
            continue
        if path.name.endswith((".jsonl.gz", ".part")):
            continue
        yield path


RESULT_COLUMNS = (
    "schema_version",
    "analyzer",
    "analyzer_version",
    "assessed_at_utc",
    "job_id",
    "dataset_type",
    "short_title",
    "download_id",
    "asset_id",
    "local_relative_path",
    "file_format",
    "assessment_scope",
    "series_instance_uid",
    "study_instance_uid",
    "geometry_status",
    "dimension",
    "shape_json",
    "spacing_json",
    "origin_json",
    "direction_json",
    "checks_json",
    "details_json",
    "error",
)


def merge_results(
    results_dir: Path, output_db: Path, baseline_db: Path | None = None
) -> dict[str, Any]:
    output_db.parent.mkdir(parents=True, exist_ok=True)
    temp = output_db.with_suffix(output_db.suffix + ".part")
    if temp.exists():
        temp.unlink()
    if baseline_db:
        if not baseline_db.is_file():
            raise FileNotFoundError(f"Geometry baseline not found: {baseline_db}")
        shutil.copy2(baseline_db, temp)
    conn = sqlite3.connect(temp)
    try:
        if baseline_db is None:
            conn.execute(
                """
            CREATE TABLE geometry_assessments (
                assessment_id INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                analyzer TEXT NOT NULL,
                analyzer_version TEXT NOT NULL,
                assessed_at_utc TEXT NOT NULL,
                job_id TEXT NOT NULL,
                dataset_type TEXT NOT NULL,
                short_title TEXT NOT NULL,
                download_id TEXT NOT NULL,
                asset_id TEXT,
                local_relative_path TEXT NOT NULL,
                file_format TEXT NOT NULL,
                assessment_scope TEXT NOT NULL,
                series_instance_uid TEXT,
                study_instance_uid TEXT,
                geometry_status TEXT NOT NULL,
                dimension INTEGER,
                shape_json TEXT NOT NULL,
                spacing_json TEXT NOT NULL,
                origin_json TEXT NOT NULL,
                direction_json TEXT NOT NULL,
                checks_json TEXT NOT NULL,
                details_json TEXT NOT NULL,
                error TEXT
            )
                """
            )
            conn.execute("CREATE INDEX idx_geometry_asset ON geometry_assessments(asset_id)")
            conn.execute(
                "CREATE INDEX idx_geometry_series ON geometry_assessments(series_instance_uid)"
            )
            conn.execute(
                "CREATE INDEX idx_geometry_dataset ON geometry_assessments(short_title, download_id)"
            )
        insert_sql = """
            INSERT INTO geometry_assessments (
                schema_version, analyzer, analyzer_version, assessed_at_utc,
                job_id, dataset_type, short_title, download_id, asset_id,
                local_relative_path, file_format, assessment_scope,
                series_instance_uid, study_instance_uid, geometry_status,
                dimension, shape_json, spacing_json, origin_json,
                direction_json, checks_json, details_json, error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        files = sorted(results_dir.glob("*.jsonl.gz"))
        replacement_jobs: set[str] = set()
        for path in files:
            for row in iter_jsonl(path):
                job_id = str(row.get("job_id") or "")
                if job_id:
                    replacement_jobs.add(job_id)
        if baseline_db and replacement_jobs:
            conn.executemany(
                "DELETE FROM geometry_assessments WHERE job_id=?",
                [(job_id,) for job_id in sorted(replacement_jobs)],
            )
        inserted_rows = 0
        for path in files:
            for row in iter_jsonl(path):
                conn.execute(
                    insert_sql,
                    (
                        row.get("schema_version", 1),
                        row.get("analyzer", ""),
                        row.get("analyzer_version", ""),
                        row.get("assessed_at_utc", ""),
                        row.get("job_id", ""),
                        row.get("dataset_type", ""),
                        row.get("short_title", ""),
                        row.get("download_id", ""),
                        row.get("asset_id") or None,
                        row.get("local_relative_path", ""),
                        row.get("file_format", ""),
                        row.get("assessment_scope", ""),
                        row.get("series_instance_uid") or None,
                        row.get("study_instance_uid") or None,
                        row.get("geometry_status", "not_checked"),
                        row.get("dimension"),
                        json.dumps(row.get("shape", []), separators=(",", ":")),
                        json.dumps(row.get("spacing", []), separators=(",", ":")),
                        json.dumps(row.get("origin", []), separators=(",", ":")),
                        json.dumps(row.get("direction", []), separators=(",", ":")),
                        json.dumps(row.get("checks", {}), sort_keys=True, separators=(",", ":")),
                        json.dumps(row.get("details", {}), sort_keys=True, separators=(",", ":")),
                        row.get("error") or None,
                    ),
                )
                inserted_rows += 1
        conn.execute(
            "DROP VIEW IF EXISTS agent_geometry_assessments"
        )
        conn.execute(
            """
            CREATE VIEW agent_geometry_assessments AS
            SELECT * FROM geometry_assessments
            """
        )
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        rows = int(conn.execute("SELECT COUNT(*) FROM geometry_assessments").fetchone()[0])
        statuses = dict(
            conn.execute(
                "SELECT geometry_status,COUNT(*) FROM geometry_assessments "
                "GROUP BY geometry_status ORDER BY geometry_status"
            )
        )
    finally:
        conn.close()
    temp.replace(output_db)
    summary = {
        "schema_version": 1,
        "created_at_utc": utc_now(),
        "result_files": len(files),
        "baseline_db": str(baseline_db.resolve()) if baseline_db else None,
        "replacement_jobs": len(replacement_jobs),
        "inserted_rows": inserted_rows,
        "assessment_rows": rows,
        "geometry_status_counts": statuses,
        "sqlite_integrity": integrity,
        "output_db": str(output_db.resolve()),
        "sha256": sha256_file(output_db),
    }
    output_db.with_suffix(".summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="create a private job plan from V2 detail")
    plan.add_argument("--db", type=Path, required=True)
    plan.add_argument("--out-dir", type=Path, required=True)
    plan.add_argument("--formats", default=",".join(DEFAULT_FORMATS))
    plan.add_argument("--dataset", action="append", default=[])

    download = sub.add_parser("download", help="download one SLURM array job")
    download.add_argument("--plan", type=Path, required=True)
    download.add_argument("--index", type=int, required=True)
    download.add_argument("--data-root", type=Path, required=True)

    analyze = sub.add_parser("analyze", help="analyze one downloaded array job")
    analyze.add_argument("--plan", type=Path, required=True)
    analyze.add_argument("--index", type=int, required=True)
    analyze.add_argument("--data-root", type=Path, required=True)
    analyze.add_argument("--results-dir", type=Path, required=True)

    merge = sub.add_parser("merge", help="merge per-job results into SQLite")
    merge.add_argument("--results-dir", type=Path, required=True)
    merge.add_argument("--out", type=Path, required=True)
    merge.add_argument(
        "--baseline-db",
        type=Path,
        help="Optional prior geometry SQLite; jobs present in results replace prior rows.",
    )
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "plan":
        summary = plan_jobs(
            args.db,
            args.out_dir,
            [item.strip() for item in args.formats.split(",") if item.strip()],
            args.dataset,
        )
    elif args.command == "download":
        job = load_job(args.plan, args.index)
        target = download_job(job, args.data_root)
        summary = {"job_id": job["job_id"], "download_dir": str(target.resolve())}
    elif args.command == "analyze":
        job = load_job(args.plan, args.index)
        output = analyze_job(job, args.data_root, args.results_dir)
        summary = {"job_id": job["job_id"], "result": str(output.resolve())}
    else:
        summary = merge_results(args.results_dir, args.out, args.baseline_db)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
