"""MCP adapter for the TCIA query service.

This layer is intentionally thin: it exposes hand-authored, read-only MCP tools
over :mod:`tcia_query_mcp.service` and keeps the TCIA snapshot/query logic out
of the protocol adapter.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from . import __version__
from .service import CONTROLLED_ACCESS_POLICY_URL, TciaQueryService, TciaServiceError


SERVER_NAME = "tcia-query-mcp"
SERVER_TITLE = "TCIA Query MCP"

INSTRUCTIONS = """\
This server exposes read-only TCIA-published dataset metadata backed by the
TCIA query skill SQLite release snapshots. Use WordPress Collection and
Analysis Result records as the TCIA provenance authority. Hidden, staged, and
retired records are excluded unless a TCIA staff workflow explicitly asks for
them.

Work this way:
1. Start with get_snapshot_info to confirm which SQLite snapshots and optional
   sidecars are loaded.
2. Use search_datasets and get_dataset for TCIA provenance, license/access
   status, current download labels, external-resource labels, and related
   Analysis Results.
3. Use download-level tools for modality, file type, route, and controlled
   access decisions. Split mixed-access datasets into open and controlled
   downloads.
4. Use the optional sidecar tools only after confirming TCIA provenance in the
   base snapshot: controlled-access files, public NIfTI metadata, and pathology
   Aspera/PathDB metadata.
5. Do not directly download controlled data. Return policy and manifest/DRS
   guidance only.
"""

GUIDE = f"""\
# Querying TCIA With This MCP Server

TCIA provenance comes from visible WordPress Collection and Analysis Result
records in the base snapshot. Downstream resources such as IDC, CDA, General
Commons, CTDC, PathDB, DataCite, and Aspera enrich or route access; they do not
decide whether something is TCIA-published.

Recommended workflow:

1. Call `get_snapshot_info` and confirm the base snapshot plus any needed
   optional sidecar exists.
2. Use `search_datasets` for broad discovery. Use `get_dataset` for one short
   title, including current downloads and related visible Analysis Results.
3. Use `summarize_access` before any download guidance. Creative Commons is
   open access, Creative Commons NonCommercial is open with a noncommercial
   restriction, and controlled/restricted licenses require the TCIA policy:
   {CONTROLLED_ACCESS_POLICY_URL}
4. For public DICOM downloads or viewers, use IDC/idc-index after TCIA
   provenance and access are confirmed. Do not generate public viewer/download
   routes for controlled-access data.
5. Use `get_dataset_versions` and `get_dataset_v1_releases` for release-history
   questions when the loaded base snapshot includes those views.
6. Use `get_controlled_access_files` only for public file-grain metadata about
   controlled-access records. It does not grant authorization.
7. Use the NIfTI tools for public non-DICOM NIfTI file-grain metadata.
8. Use the pathology tools for PathDB/Aspera metadata. PathDB is optimized for
   metadata/viewers and may use converted files; Aspera packages are the
   original submitter-provided route.
"""


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _transport_security() -> TransportSecuritySettings:
    """Configure FastMCP host/origin protection for hosted HTTP deployments."""

    if _env_bool("TCIA_MCP_DNS_REBINDING_PROTECTION", default=False):
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_csv_env("TCIA_MCP_ALLOWED_HOSTS") or ["127.0.0.1", "localhost"],
            allowed_origins=_csv_env("TCIA_MCP_ALLOWED_ORIGINS")
            or ["http://127.0.0.1", "http://localhost"],
        )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


mcp = FastMCP(
    "TCIA Query",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    transport_security=_transport_security(),
)
mcp._mcp_server.version = __version__

_service: TciaQueryService | None = None


def configure_service(
    *,
    snapshot_db: str | os.PathLike[str] | None = None,
    controlled_db: str | os.PathLike[str] | None = None,
    nifti_db: str | os.PathLike[str] | None = None,
    pathology_db: str | os.PathLike[str] | None = None,
    skill_root: str | os.PathLike[str] | None = None,
) -> TciaQueryService:
    """Replace the process-wide service used by MCP tools."""

    global _service
    _service = TciaQueryService(
        snapshot_db=snapshot_db,
        controlled_db=controlled_db,
        nifti_db=nifti_db,
        pathology_db=pathology_db,
        skill_root=skill_root,
    )
    return _service


def service() -> TciaQueryService:
    global _service
    if _service is None:
        _service = TciaQueryService()
    return _service


def guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Convert expected service exceptions into clean MCP tool errors."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except TciaServiceError as exc:
            raise ToolError(str(exc)) from None

    return wrapper


@mcp.tool()
@guard
def get_snapshot_info() -> dict:
    """Return configured snapshot paths, optional sidecar availability, row counts, and feature flags."""

    return service().snapshot_info()


@mcp.tool()
@guard
def search_datasets(
    query: str | None = None,
    short_titles: list[str] | None = None,
    dataset_type: str = "both",
    access_levels: list[str] | None = None,
    modalities: list[str] | None = None,
    data_types: list[str] | None = None,
    download_types: list[str] | None = None,
    file_types: list[str] | None = None,
    cancer_types: list[str] | None = None,
    cancer_locations: list[str] | None = None,
    species: list[str] | None = None,
    programs: list[str] | None = None,
    external_resources: list[str] | None = None,
    has_external_clinical_resource: bool | None = None,
    doi: str | None = None,
    include_hidden: bool = False,
    limit: int = 25,
) -> dict:
    """Search visible TCIA WordPress Collections and Analysis Results.

    Use this for TCIA provenance and discovery. Filter by download-level modality/file labels
    when the user asks about data formats, and by top-level external_resources when the user asks
    about curated external Clinical, Genomics, Proteomics, Software, or Image Analyses resources.
    """

    return service().search_datasets(
        query=query,
        short_titles=short_titles,
        dataset_type=dataset_type,
        access_levels=access_levels,
        modalities=modalities,
        data_types=data_types,
        download_types=download_types,
        file_types=file_types,
        cancer_types=cancer_types,
        cancer_locations=cancer_locations,
        species=species,
        programs=programs,
        external_resources=external_resources,
        has_external_clinical_resource=has_external_clinical_resource,
        doi=doi,
        include_hidden=include_hidden,
        limit=limit,
    )


@mcp.tool()
@guard
def get_dataset(short_title: str, include_hidden: bool = False) -> dict:
    """Return one TCIA dataset by short title, including access, current downloads, and related results."""

    return service().get_dataset(short_title=short_title, include_hidden=include_hidden)


@mcp.tool()
@guard
def get_dataset_versions(short_title: str, include_hidden: bool = False, limit: int = 100) -> dict:
    """Return WordPress version-history rows matched to one TCIA dataset short title."""

    return service().get_dataset_versions(
        short_title=short_title,
        include_hidden=include_hidden,
        limit=limit,
    )


@mcp.tool()
@guard
def get_dataset_v1_releases(
    short_titles: list[str] | None = None,
    dataset_type: str = "both",
    released_since: str | None = None,
    released_before: str | None = None,
    include_hidden: bool = False,
    limit: int = 50,
) -> dict:
    """Return best-available first-release dates from agent_dataset_v1_releases."""

    return service().get_dataset_v1_releases(
        short_titles=short_titles,
        dataset_type=dataset_type,
        released_since=released_since,
        released_before=released_before,
        include_hidden=include_hidden,
        limit=limit,
    )


@mcp.tool()
@guard
def get_current_downloads(
    short_title: str,
    access_levels: list[str] | None = None,
    modalities: list[str] | None = None,
    data_types: list[str] | None = None,
    download_types: list[str] | None = None,
    file_types: list[str] | None = None,
    requires_annotations: bool = False,
    include_hidden: bool = False,
    limit: int = 25,
) -> dict:
    """Return current WordPress download rows for one dataset.

    Use this before download advice because TCIA modality, file type, and access labels live at
    download level and mixed datasets can contain both open and controlled files.
    """

    return service().get_current_downloads(
        short_title=short_title,
        access_levels=access_levels,
        modalities=modalities,
        data_types=data_types,
        download_types=download_types,
        file_types=file_types,
        requires_annotations=requires_annotations,
        include_hidden=include_hidden,
        limit=limit,
    )


@mcp.tool()
@guard
def summarize_access(short_title: str, include_hidden: bool = False) -> dict:
    """Summarize dataset/download access and split open, noncommercial, controlled, and mixed routes."""

    return service().summarize_access(short_title=short_title, include_hidden=include_hidden)


@mcp.tool()
@guard
def find_controlled_access_datasets(
    modalities: list[str] | None = None,
    file_types: list[str] | None = None,
    requires_annotations: bool = False,
    include_mixed: bool = True,
    limit: int = 25,
) -> dict:
    """Find controlled or mixed-access TCIA datasets from the base WordPress snapshot."""

    return service().find_controlled_access_datasets(
        modalities=modalities,
        file_types=file_types,
        requires_annotations=requires_annotations,
        include_mixed=include_mixed,
        limit=limit,
    )


@mcp.tool()
@guard
def get_controlled_access_files(
    short_title: str,
    route_systems: list[str] | None = None,
    modalities: list[str] | None = None,
    file_types: list[str] | None = None,
    body_part: str | None = None,
    participant_id: str | None = None,
    patient_id: str | None = None,
    has_drs_uri: bool | None = None,
    limit: int = 50,
) -> dict:
    """Query public file-grain metadata for controlled-access downloads.

    This returns metadata only. It does not grant authorization and must not be used to directly
    download controlled files.
    """

    return service().get_controlled_access_files(
        short_title=short_title,
        route_systems=route_systems,
        modalities=modalities,
        file_types=file_types,
        body_part=body_part,
        participant_id=participant_id,
        patient_id=patient_id,
        has_drs_uri=has_drs_uri,
        limit=limit,
    )


@mcp.tool()
@guard
def find_dicom_annotations(
    query: str | None = None,
    short_titles: list[str] | None = None,
    modalities: list[str] | None = None,
    access_levels: list[str] | None = None,
    include_hidden: bool = False,
    limit: int = 25,
) -> dict:
    """Find TCIA DICOM annotation/result downloads with provenance and access caveats."""

    return service().find_dicom_annotations(
        query=query,
        short_titles=short_titles,
        modalities=modalities,
        access_levels=access_levels,
        include_hidden=include_hidden,
        limit=limit,
    )


@mcp.tool()
@guard
def find_nifti_datasets(
    short_titles: list[str] | None = None,
    modalities: list[str] | None = None,
    requires_derived_objects: bool = False,
    limit: int = 25,
) -> dict:
    """Summarize datasets represented in the optional public NIfTI SQLite sidecar."""

    return service().find_nifti_datasets(
        short_titles=short_titles,
        modalities=modalities,
        requires_derived_objects=requires_derived_objects,
        limit=limit,
    )


@mcp.tool()
@guard
def get_nifti_files(
    short_title: str,
    modalities: list[str] | None = None,
    subject_id: str | None = None,
    file_name_contains: str | None = None,
    derived_only: bool = False,
    has_source_uids: bool | None = None,
    limit: int = 50,
) -> dict:
    """Return public NIfTI radiology file/series rows from the optional NIfTI sidecar."""

    return service().get_nifti_files(
        short_title=short_title,
        modalities=modalities,
        subject_id=subject_id,
        file_name_contains=file_name_contains,
        derived_only=derived_only,
        has_source_uids=has_source_uids,
        limit=limit,
    )


@mcp.tool()
@guard
def get_nifti_derived_objects(
    short_title: str,
    linked_only: bool = False,
    file_name_contains: str | None = None,
    confidence: str | None = None,
    limit: int = 50,
) -> dict:
    """Return probable NIfTI segmentation/derived objects and source-image references."""

    return service().get_nifti_derived_objects(
        short_title=short_title,
        linked_only=linked_only,
        file_name_contains=file_name_contains,
        confidence=confidence,
        limit=limit,
    )


@mcp.tool()
@guard
def get_nifti_package_files(
    short_title: str,
    file_exts: list[str] | None = None,
    file_name_contains: str | None = None,
    metadata_candidates: bool | None = None,
    limit: int = 50,
) -> dict:
    """Return NIfTI Aspera package file inventory rows from the optional NIfTI sidecar."""

    return service().get_nifti_package_files(
        short_title=short_title,
        file_exts=file_exts,
        file_name_contains=file_name_contains,
        metadata_candidates=metadata_candidates,
        limit=limit,
    )


@mcp.tool()
@guard
def find_pathology_datasets(
    short_titles: list[str] | None = None,
    package_inventory_status: str | None = None,
    with_package_inventory: bool | None = None,
    has_pathdb: bool | None = None,
    limit: int = 25,
) -> dict:
    """Summarize datasets in the optional pathology/Aspera SQLite sidecar."""

    return service().find_pathology_datasets(
        short_titles=short_titles,
        package_inventory_status=package_inventory_status,
        with_package_inventory=with_package_inventory,
        has_pathdb=has_pathdb,
        limit=limit,
    )


@mcp.tool()
@guard
def get_pathology_downloads(short_titles: list[str] | None = None, limit: int = 25) -> dict:
    """Return pathology Aspera download scope rows from the optional pathology sidecar."""

    return service().get_pathology_downloads(short_titles=short_titles, limit=limit)


@mcp.tool()
@guard
def get_pathology_package_files(
    short_title: str,
    file_exts: list[str] | None = None,
    file_roles: list[str] | None = None,
    download_id: str | None = None,
    file_name_contains: str | None = None,
    limit: int = 50,
) -> dict:
    """Return pathology Aspera package inventory rows for one TCIA dataset."""

    return service().get_pathology_package_files(
        short_title=short_title,
        file_exts=file_exts,
        file_roles=file_roles,
        download_id=download_id,
        file_name_contains=file_name_contains,
        limit=limit,
    )


@mcp.tool()
@guard
def get_pathology_file_objects(
    short_title: str,
    file_exts: list[str] | None = None,
    file_roles: list[str] | None = None,
    file_name_contains: str | None = None,
    limit: int = 50,
) -> dict:
    """Return normalized pathology file objects from PathDB/package-derived metadata."""

    return service().get_pathology_file_objects(
        short_title=short_title,
        file_exts=file_exts,
        file_roles=file_roles,
        file_name_contains=file_name_contains,
        limit=limit,
    )


@mcp.tool()
@guard
def get_pathology_disparities(
    short_titles: list[str] | None = None,
    disparity_types: list[str] | None = None,
    limit: int = 25,
) -> dict:
    """Return PathDB/package/download scope disparity rows from the optional pathology sidecar."""

    return service().get_pathology_disparities(
        short_titles=short_titles,
        disparity_types=disparity_types,
        limit=limit,
    )


@mcp.resource("tcia://guide", mime_type="text/markdown")
def guide_resource() -> str:
    """How to query TCIA with these tools."""

    return GUIDE


@mcp.resource("tcia://snapshot/info", mime_type="application/json")
def snapshot_info_resource() -> str:
    """Snapshot paths, metadata, row counts, and feature flags."""

    return json.dumps(service().snapshot_info(), indent=2, sort_keys=True)


def http_app(server: FastMCP | None = None):
    """Return the Starlette app for FastMCP streamable HTTP transport."""

    server = server or mcp
    app = server.streamable_http_app()
    configured = server.settings.streamable_http_path
    base = configured.rstrip("/")
    spellings = [base, f"{base}/"] if base else ["/"]
    found = {r.path: r for r in app.router.routes if isinstance(r, Route) and r.path in spellings}
    if found:
        endpoint = next(iter(found.values())).endpoint
        for path in spellings:
            if path not in found:
                app.router.routes.append(Route(path, endpoint=endpoint, name="mcp_alt_path"))
        app.router.redirect_slashes = False
    return app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the TCIA query MCP server.")
    parser.add_argument("--snapshot-db", help="Path to tcia_snapshot.sqlite.")
    parser.add_argument("--controlled-db", help="Path to controlled_access_metadata.sqlite.")
    parser.add_argument("--nifti-db", help="Path to nifti_metadata.sqlite.")
    parser.add_argument("--pathology-db", help="Path to pathology_metadata.sqlite.")
    parser.add_argument("--skill-root", type=Path, help="Skill repository root.")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--http", action="store_true", help="Alias for --transport http.")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host.")
    parser.add_argument("--port", type=int, default=8765, help="HTTP bind port.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    configure_service(
        snapshot_db=args.snapshot_db,
        controlled_db=args.controlled_db,
        nifti_db=args.nifti_db,
        pathology_db=args.pathology_db,
        skill_root=args.skill_root,
    )
    transport = "http" if args.http else args.transport
    if transport == "stdio":
        mcp.run(transport="stdio")
        return 0

    import uvicorn

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    uvicorn.run(http_app(), host=args.host, port=args.port, log_level=mcp.settings.log_level.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
