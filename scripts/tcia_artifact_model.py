#!/usr/bin/env python3
"""Shared vocabulary and deterministic helpers for TCIA metadata artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


SCHEMA_VERSION = 3

MANAGED_SYSTEMS = {
    "tcia_wordpress",
    "tcia_aspera",
    "tcia_pathdb",
    "aws_open_data",
    "crdc_idc",
    "crdc_gc",
    "crdc_ctdc",
}

SYSTEM_FUNCTIONS = {
    "publication_catalog",
    "discovery_index",
    "distribution_endpoint",
    "viewer",
    "controlled_access_broker",
}

REPRESENTATION_PROVENANCE_CLASSES = {
    "submitted_original",
    "exact_replica",
    "standardized_representation",
    "derived_asset",
    "metadata_only",
    "unknown",
}

VALUE_ROLES = {"source_raw", "normalized", "harmonized", "inferred", "resolved"}

NON_DICOM_IMAGING_FORMATS = {
    "BMP",
    "DAT",
    "ENVI",
    "HDF5",
    "JPG",
    "JPEG",
    "MHA",
    "MHD",
    "MP4",
    "MPG",
    "MPEG",
    "MRXS",
    "NDPI",
    "NIFTI",
    "NRRD",
    "PNG",
    "SVS",
    "TIFF",
}

CONTAINER_FORMATS = {"ZIP", "TAR", "GZ", "TGZ"}

VIDEO_FORMATS = {"AVI", "MP4", "MPG", "MPEG", "MOV", "WEBM"}
VOLUME_FORMATS = {"MHA", "MHD", "NIFTI", "NRRD"}
WHOLE_SLIDE_FORMATS = {"MRXS", "NDPI", "SVS"}
STILL_IMAGE_FORMATS = {"BMP", "JPG", "JPEG", "PNG", "TIFF"}


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, *parts: object) -> str:
    normalized = "\x1f".join(str(part or "").strip() for part in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def normalize_format(value: object) -> str:
    text = str(value or "").strip().upper().lstrip(".")
    aliases = {
        "NII": "NIFTI",
        "NII.GZ": "NIFTI",
        "JPE": "JPEG",
        "TIF": "TIFF",
    }
    return aliases.get(text, text)


def format_from_path(value: object) -> str:
    lower = str(value or "").strip().lower()
    if lower.endswith(".nii.gz"):
        return "NIFTI"
    suffix = Path(lower).suffix.lstrip(".")
    return normalize_format(suffix)


def media_kind(file_format: str, data_types: list[str] | None = None) -> str:
    normalized = normalize_format(file_format)
    labels = {str(value).strip().casefold() for value in (data_types or [])}
    if normalized in VIDEO_FORMATS:
        return "video"
    if normalized in WHOLE_SLIDE_FORMATS or "whole slide image" in labels:
        return "whole_slide_image"
    if normalized in VOLUME_FORMATS:
        return "image_volume"
    if normalized in STILL_IMAGE_FORMATS:
        return "still_image"
    if normalized in {"ENVI", "HDF5"}:
        return "spectral_or_array"
    return "unknown"


def imaging_domain(download_types: list[str], data_types: list[str]) -> str:
    labels = {str(value).strip().casefold() for value in [*download_types, *data_types]}
    if any("capsule" in value or "endoscop" in value for value in labels):
        return "endoscopy"
    if any(
        token in value
        for value in labels
        for token in ("pathology", "histopath", "whole slide", "photomicrograph", "micrograph")
    ):
        return "pathology"
    if any(
        value in {"ct", "mr", "mg", "dx", "cr", "us", "pet", "nm", "radiology images"}
        for value in labels
    ):
        return "radiology"
    if any("segmentation" in value or "annotation" in value for value in labels):
        return "imaging_annotation"
    return "other_imaging"


def object_role(download_types: list[str], data_types: list[str], name: str = "") -> str:
    labels = {str(value).strip().casefold() for value in [*download_types, *data_types]}
    lower_name = str(name or "").casefold()
    if any("segmentation" in value for value in labels) or any(
        token in lower_name for token in ("mask", "label", "seg")
    ):
        return "segmentation"
    if any("annotation" in value for value in labels):
        return "annotation"
    if any("radiology images" in value or "pathology images" in value for value in labels):
        return "source_image"
    return "unknown"


def managed_system_for_url(url: object, *, route_system: str = "") -> str:
    route = str(route_system or "").strip().casefold().replace("-", "_")
    if route in {"ctdc", "crdc_ctdc"}:
        return "crdc_ctdc"
    if route in {"general_commons", "gc", "crdc_gc"}:
        return "crdc_gc"
    host = (urlparse(str(url or "")).hostname or "").casefold()
    if "faspex" in host or "aspera" in host:
        return "tcia_aspera"
    if "pathdb" in host:
        return "tcia_pathdb"
    if host.endswith("amazonaws.com") or host.endswith("aws.amazon.com"):
        return "aws_open_data"
    if "imagingdatacommons" in host or host.endswith("idc.google"):
        return "crdc_idc"
    return "tcia_wordpress"


def default_system_functions(system: str, *, viewer_url: str = "") -> list[str]:
    if system == "tcia_wordpress":
        values = ["publication_catalog", "distribution_endpoint"]
    elif system == "tcia_aspera":
        values = ["distribution_endpoint"]
    elif system == "tcia_pathdb":
        values = ["discovery_index", "distribution_endpoint", "viewer"]
    elif system == "aws_open_data":
        values = ["distribution_endpoint"]
    elif system == "crdc_idc":
        values = ["discovery_index", "distribution_endpoint", "viewer"]
    elif system in {"crdc_gc", "crdc_ctdc"}:
        values = ["controlled_access_broker", "discovery_index", "distribution_endpoint"]
    else:
        values = ["distribution_endpoint"]
    if viewer_url and "viewer" not in values:
        values.append("viewer")
    return sorted(values)


def default_representation_class(system: str, *, is_metadata: bool = False) -> str:
    if is_metadata:
        return "metadata_only"
    if system == "tcia_aspera":
        return "submitted_original"
    return "unknown"


def validate_vocab(value: str, allowed: set[str], field: str) -> None:
    if value not in allowed:
        raise ValueError(f"Unsupported {field} {value!r}; expected one of {sorted(allowed)}")
