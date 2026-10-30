"""Compact download requirements shared by artifact builders and query services.

This module uses only the standard library. It never reads a manifest, contacts
an upstream host, checks installed software, authenticates, or transfers data.
"""

from __future__ import annotations

import re
import ipaddress
from pathlib import PurePosixPath
from urllib.parse import urlsplit

CONTRACT_VERSION = 1
SOFTWARE = {
    "none": {
        "label": "No additional download software",
        "help_url": "",
        "instructions": "Download the published file under its license.",
    },
    "tcia_data_retriever": {
        "label": "Requires TCIA Data Retriever",
        "help_url": "https://github.com/TCIA/data-retriever/releases",
        "instructions": "Open the manifest with TCIA Data Retriever. TCIA Python tools can also process supported manifest formats. Controlled downloads additionally require approval and credentials.",
    },
    "aspera": {
        "label": "Requires Aspera",
        "help_url": "https://github.com/kirbyju/TCIA_Notebooks/blob/main/TCIA_Aspera_CLI_Downloads.ipynb",
        "instructions": "Install a supported Aspera transfer client where the files will be received. Python package transfers need Aspera CLI and its transfer engine in the active runtime; a browser installation on another computer is not available in a hosted notebook.",
    },
    "unknown": {
        "label": "Download requirements need review",
        "help_url": "",
        "instructions": "Follow the official dataset download instructions; a supported transfer route has not been established.",
    },
}
METHODS = {"dicom_manifest", "tabular_manifest", "aspera_package", "direct_file", "unsupported"}
STATUSES = {"declared", "link_fallback", "unknown", "conflict"}
FILE_SUFFIXES = {
    ".zip",
    ".gz",
    ".tar",
    ".csv",
    ".tsv",
    ".xls",
    ".xlsx",
    ".pdf",
    ".json",
    ".txt",
    ".nii",
    ".dcm",
    ".tif",
    ".tiff",
    ".svs",
    ".png",
    ".jpg",
    ".jpeg",
    ".mha",
    ".mhd",
    ".nrrd",
    ".mp4",
    ".h5",
    ".mat",
    ".xml",
    ".parquet",
    ".7z",
}


def software_catalog(codes=None):
    """Return one help definition per code, not a copy on every download row."""
    return {code: dict(value) for code, value in SOFTWARE.items() if codes is None or code in codes}


def _label(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def resolve_download(row):
    """Resolve declared requirements first; clearly mark a URL-only fallback.

    Unknown declarations are not discarded in favor of a link guess. Conflicting
    declarations and known URL types have no executable method. Access and
    authorization remain separate fields; software never grants permission.
    """
    declared = bool(str(row.get("requirements_label") or "").strip())
    label = _label(row.get("requirements_label"))
    code = {
        "tcia data retriever": "tcia_data_retriever",
        "nbia data retriever": "tcia_data_retriever",
        "requires tcia data retriever": "tcia_data_retriever",
        "download requires the tcia data retriever": "tcia_data_retriever",
        "aspera": "aspera",
        "requires aspera": "aspera",
        "ibm aspera connect plugin": "aspera",
        "ibm aspera connect": "aspera",
        "aspera connect": "aspera",
        "aspera connect plugin": "aspera",
        "none": "none",
        "no requirements": "none",
        "no additional software": "none",
        "no software required": "none",
        "web browser": "none",
    }.get(label, "unknown")
    status = "declared" if code != "unknown" else "unknown"
    method = "unsupported"
    try:
        url = urlsplit(str(row.get("download_url") or ""))
        valid_url = (
            url.scheme == "https"
            and url.hostname
            and not url.username
            and not url.password
            and not url.fragment
            and url.port in {None, 443}
        )
        host = (url.hostname or "").casefold()
        if host == "localhost" or "." not in host or host.endswith((".local", ".internal", ".lan")):
            valid_url = False
        try:
            if not ipaddress.ip_address(host).is_global:
                valid_url = False
        except ValueError:
            pass
        suffix = PurePosixPath(url.path).suffix.casefold()
        aspera = url.hostname == "faspex.cancerimagingarchive.net"
    except ValueError:
        valid_url, aspera, suffix = False, False, ""
    if not valid_url:
        return {
            "software_requirement": code,
            "requirement_status": status,
            "download_method": method,
        }
    if declared and code == "unknown":
        return {
            "software_requirement": code,
            "requirement_status": "unknown",
            "download_method": method,
        }
    if aspera:
        if code not in {"unknown", "aspera"}:
            status = "conflict"
        else:
            code, method = "aspera", "aspera_package"
    elif code == "aspera":
        status = "conflict"
    elif suffix == ".tcia":
        if code not in {"unknown", "tcia_data_retriever"}:
            status = "conflict"
        else:
            code, method = "tcia_data_retriever", "dicom_manifest"
    elif code == "tcia_data_retriever":
        if suffix in {".csv", ".tsv"}:
            method = "tabular_manifest"
        else:
            status = "conflict"
    elif suffix in FILE_SUFFIXES:
        code, method = "none", "direct_file"
    if method == "direct_file" and (
        row.get("controlled_access") or row.get("access_level") == "controlled"
    ):
        method = "unsupported"
        if not declared:
            code, status = "unknown", "unknown"
    if not declared and method != "unsupported":
        status = "link_fallback"
    return {"software_requirement": code, "requirement_status": status, "download_method": method}
