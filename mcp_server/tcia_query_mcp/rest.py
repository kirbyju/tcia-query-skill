"""FastAPI REST adapter for the TCIA query service."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .service import TciaQueryService, TciaServiceError

API_PREFIX = "/v1"


class SearchDatasetsRequest(BaseModel):
    query: str | None = None
    short_titles: list[str] | None = None
    dataset_type: str = "both"
    access_levels: list[str] | None = None
    modalities: list[str] | None = None
    data_types: list[str] | None = None
    download_types: list[str] | None = None
    file_types: list[str] | None = None
    cancer_types: list[str] | None = None
    cancer_locations: list[str] | None = None
    species: list[str] | None = None
    programs: list[str] | None = None
    external_resources: list[str] | None = None
    has_external_clinical_resource: bool | None = None
    doi: str | None = None
    include_hidden: bool = False
    limit: int = Field(default=25, ge=1, le=200)


def _service_from_app(app: FastAPI) -> TciaQueryService:
    return app.state.tcia_service


def create_app(service: TciaQueryService | None = None) -> FastAPI:
    """Create the REST API over a shared TCIA query service."""

    app = FastAPI(
        title="TCIA Query API",
        version=__version__,
        summary="Read-only REST API for TCIA query skill SQLite release snapshots.",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    app.state.tcia_service = service or TciaQueryService()

    @app.exception_handler(TciaServiceError)
    async def _service_error_handler(_request, exc: TciaServiceError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def S() -> TciaQueryService:
        return _service_from_app(app)

    @app.get("/")
    def root() -> dict[str, str]:
        return {
            "name": "TCIA Query API",
            "version": __version__,
            "docs": f"{API_PREFIX}/docs",
            "health": f"{API_PREFIX}/health",
        }

    @app.get(f"{API_PREFIX}/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(f"{API_PREFIX}/snapshot")
    def snapshot_info() -> dict[str, Any]:
        return S().snapshot_info()

    @app.post(f"{API_PREFIX}/datasets/search")
    def search_datasets(request: SearchDatasetsRequest) -> dict[str, Any]:
        return S().search_datasets(**request.model_dump())

    @app.get(f"{API_PREFIX}/datasets/{{short_title}}")
    def get_dataset(short_title: str, include_hidden: bool = False) -> dict[str, Any]:
        return S().get_dataset(short_title=short_title, include_hidden=include_hidden)

    @app.get(f"{API_PREFIX}/datasets/{{short_title}}/downloads")
    def get_current_downloads(
        short_title: str,
        access_levels: Annotated[list[str] | None, Query()] = None,
        modalities: Annotated[list[str] | None, Query()] = None,
        data_types: Annotated[list[str] | None, Query()] = None,
        download_types: Annotated[list[str] | None, Query()] = None,
        file_types: Annotated[list[str] | None, Query()] = None,
        requires_annotations: bool = False,
        include_hidden: bool = False,
        limit: int = Query(default=25, ge=1, le=200),
    ) -> dict[str, Any]:
        return S().get_current_downloads(
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

    @app.get(f"{API_PREFIX}/datasets/{{short_title}}/access")
    def summarize_access(short_title: str, include_hidden: bool = False) -> dict[str, Any]:
        return S().summarize_access(short_title=short_title, include_hidden=include_hidden)

    @app.get(f"{API_PREFIX}/datasets/{{short_title}}/versions")
    def get_dataset_versions(
        short_title: str,
        include_hidden: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_dataset_versions(
            short_title=short_title,
            include_hidden=include_hidden,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/release-history/v1-releases")
    def get_dataset_v1_releases(
        short_titles: Annotated[list[str] | None, Query()] = None,
        dataset_type: str = "both",
        released_since: str | None = None,
        released_before: str | None = None,
        include_hidden: bool = False,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_dataset_v1_releases(
            short_titles=short_titles,
            dataset_type=dataset_type,
            released_since=released_since,
            released_before=released_before,
            include_hidden=include_hidden,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/controlled-access/datasets")
    def find_controlled_access_datasets(
        modalities: Annotated[list[str] | None, Query()] = None,
        file_types: Annotated[list[str] | None, Query()] = None,
        requires_annotations: bool = False,
        include_mixed: bool = True,
        limit: int = Query(default=25, ge=1, le=200),
    ) -> dict[str, Any]:
        return S().find_controlled_access_datasets(
            modalities=modalities,
            file_types=file_types,
            requires_annotations=requires_annotations,
            include_mixed=include_mixed,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/controlled-access/{{short_title}}/files")
    def get_controlled_access_files(
        short_title: str,
        route_systems: Annotated[list[str] | None, Query()] = None,
        modalities: Annotated[list[str] | None, Query()] = None,
        file_types: Annotated[list[str] | None, Query()] = None,
        body_part: str | None = None,
        participant_id: str | None = None,
        patient_id: str | None = None,
        has_drs_uri: bool | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_controlled_access_files(
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

    @app.get(f"{API_PREFIX}/dicom/annotations")
    def find_dicom_annotations(
        query: str | None = None,
        short_titles: Annotated[list[str] | None, Query()] = None,
        modalities: Annotated[list[str] | None, Query()] = None,
        access_levels: Annotated[list[str] | None, Query()] = None,
        include_hidden: bool = False,
        limit: int = Query(default=25, ge=1, le=200),
    ) -> dict[str, Any]:
        return S().find_dicom_annotations(
            query=query,
            short_titles=short_titles,
            modalities=modalities,
            access_levels=access_levels,
            include_hidden=include_hidden,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/nifti/datasets")
    def find_nifti_datasets(
        short_titles: Annotated[list[str] | None, Query()] = None,
        modalities: Annotated[list[str] | None, Query()] = None,
        requires_derived_objects: bool = False,
        limit: int = Query(default=25, ge=1, le=200),
    ) -> dict[str, Any]:
        return S().find_nifti_datasets(
            short_titles=short_titles,
            modalities=modalities,
            requires_derived_objects=requires_derived_objects,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/nifti/{{short_title}}/files")
    def get_nifti_files(
        short_title: str,
        modalities: Annotated[list[str] | None, Query()] = None,
        subject_id: str | None = None,
        file_name_contains: str | None = None,
        derived_only: bool = False,
        has_source_uids: bool | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_nifti_files(
            short_title=short_title,
            modalities=modalities,
            subject_id=subject_id,
            file_name_contains=file_name_contains,
            derived_only=derived_only,
            has_source_uids=has_source_uids,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/nifti/{{short_title}}/derived-objects")
    def get_nifti_derived_objects(
        short_title: str,
        linked_only: bool = False,
        file_name_contains: str | None = None,
        confidence: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_nifti_derived_objects(
            short_title=short_title,
            linked_only=linked_only,
            file_name_contains=file_name_contains,
            confidence=confidence,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/nifti/{{short_title}}/characteristics")
    def get_nifti_characteristics(
        short_title: str,
        object_roles: Annotated[list[str] | None, Query()] = None,
        associated_imaging_modalities: Annotated[list[str] | None, Query()] = None,
        source_access_levels: Annotated[list[str] | None, Query()] = None,
        subject_id: str | None = None,
        file_name_contains: str | None = None,
        has_source_reference: bool | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_nifti_characteristics(
            short_title=short_title,
            object_roles=object_roles,
            associated_imaging_modalities=associated_imaging_modalities,
            source_access_levels=source_access_levels,
            subject_id=subject_id,
            file_name_contains=file_name_contains,
            has_source_reference=has_source_reference,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/nifti/review-issues")
    def find_nifti_review_issues(
        short_titles: Annotated[list[str] | None, Query()] = None,
        statuses: Annotated[list[str] | None, Query()] = None,
        severities: Annotated[list[str] | None, Query()] = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().find_nifti_review_issues(
            short_titles=short_titles,
            statuses=statuses,
            severities=severities,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/nifti/{{short_title}}/package-files")
    def get_nifti_package_files(
        short_title: str,
        file_exts: Annotated[list[str] | None, Query()] = None,
        file_name_contains: str | None = None,
        metadata_candidates: bool | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_nifti_package_files(
            short_title=short_title,
            file_exts=file_exts,
            file_name_contains=file_name_contains,
            metadata_candidates=metadata_candidates,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/pathology/datasets")
    def find_pathology_datasets(
        short_titles: Annotated[list[str] | None, Query()] = None,
        package_inventory_status: str | None = None,
        with_package_inventory: bool | None = None,
        has_pathdb: bool | None = None,
        limit: int = Query(default=25, ge=1, le=200),
    ) -> dict[str, Any]:
        return S().find_pathology_datasets(
            short_titles=short_titles,
            package_inventory_status=package_inventory_status,
            with_package_inventory=with_package_inventory,
            has_pathdb=has_pathdb,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/pathology/downloads")
    def get_pathology_downloads(
        short_titles: Annotated[list[str] | None, Query()] = None,
        limit: int = Query(default=25, ge=1, le=200),
    ) -> dict[str, Any]:
        return S().get_pathology_downloads(short_titles=short_titles, limit=limit)

    @app.get(f"{API_PREFIX}/pathology/{{short_title}}/package-files")
    def get_pathology_package_files(
        short_title: str,
        file_exts: Annotated[list[str] | None, Query()] = None,
        file_roles: Annotated[list[str] | None, Query()] = None,
        download_id: str | None = None,
        file_name_contains: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_pathology_package_files(
            short_title=short_title,
            file_exts=file_exts,
            file_roles=file_roles,
            download_id=download_id,
            file_name_contains=file_name_contains,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/pathology/{{short_title}}/files")
    def get_pathology_file_objects(
        short_title: str,
        file_exts: Annotated[list[str] | None, Query()] = None,
        file_roles: Annotated[list[str] | None, Query()] = None,
        file_name_contains: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_pathology_file_objects(
            short_title=short_title,
            file_exts=file_exts,
            file_roles=file_roles,
            file_name_contains=file_name_contains,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/pathology/disparities")
    def get_pathology_disparities(
        short_titles: Annotated[list[str] | None, Query()] = None,
        disparity_types: Annotated[list[str] | None, Query()] = None,
        limit: int = Query(default=25, ge=1, le=200),
    ) -> dict[str, Any]:
        return S().get_pathology_disparities(
            short_titles=short_titles,
            disparity_types=disparity_types,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/clinical/datasets")
    def find_clinical_datasets(
        short_titles: Annotated[list[str] | None, Query()] = None,
        source_kinds: Annotated[list[str] | None, Query()] = None,
        concepts: Annotated[list[str] | None, Query()] = None,
        has_conflicts: bool | None = None,
        has_clinical_only_subjects: bool | None = None,
        limit: int = Query(default=25, ge=1, le=200),
    ) -> dict[str, Any]:
        return S().find_clinical_datasets(
            short_titles=short_titles,
            source_kinds=source_kinds,
            concepts=concepts,
            has_conflicts=has_conflicts,
            has_clinical_only_subjects=has_clinical_only_subjects,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/clinical/{{short_title}}/subjects")
    def get_clinical_subjects(
        short_title: str,
        subject_ids: Annotated[list[str] | None, Query()] = None,
        include_clinical_only: bool = False,
        has_conflicts: bool | None = None,
        include_inferred: bool = True,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_clinical_subjects(
            short_title=short_title,
            subject_ids=subject_ids,
            include_clinical_only=include_clinical_only,
            has_conflicts=has_conflicts,
            include_inferred=include_inferred,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/clinical/{{short_title}}/facts")
    def get_clinical_facts(
        short_title: str,
        subject_id: str | None = None,
        concepts: Annotated[list[str] | None, Query()] = None,
        source_kinds: Annotated[list[str] | None, Query()] = None,
        inferred: bool | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_clinical_facts(
            short_title=short_title,
            subject_id=subject_id,
            concepts=concepts,
            source_kinds=source_kinds,
            inferred=inferred,
            limit=limit,
        )

    @app.get(f"{API_PREFIX}/clinical/{{short_title}}/conflicts")
    def get_clinical_conflicts(
        short_title: str,
        subject_id: str | None = None,
        concepts: Annotated[list[str] | None, Query()] = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return S().get_clinical_conflicts(
            short_title=short_title,
            subject_id=subject_id,
            concepts=concepts,
            limit=limit,
        )

    return app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the TCIA query REST API.")
    parser.add_argument("--snapshot-db", help="Path to tcia_snapshot.sqlite.")
    parser.add_argument("--controlled-db", help="Path to controlled_access_metadata.sqlite.")
    parser.add_argument("--nifti-db", help="Path to nifti_metadata.sqlite.")
    parser.add_argument("--pathology-db", help="Path to pathology_metadata.sqlite.")
    parser.add_argument("--clinical-db", help="Path to clinical_metadata.sqlite.")
    parser.add_argument("--skill-root", type=Path, help="Skill repository root.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    service = TciaQueryService(
        snapshot_db=args.snapshot_db,
        controlled_db=args.controlled_db,
        nifti_db=args.nifti_db,
        pathology_db=args.pathology_db,
        clinical_db=args.clinical_db,
        skill_root=args.skill_root,
    )
    app = create_app(service)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
