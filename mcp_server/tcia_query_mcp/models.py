"""Shared public response contracts for REST OpenAPI and MCP structured output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PublicResponse(BaseModel):
    """Typed top-level contract that permits versioned additive fields."""

    model_config = ConfigDict(extra="allow")


class PageResponse(PublicResponse):
    count: int = Field(ge=0)
    limit: int = Field(ge=1)
    has_more: bool
    truncated: bool
    next_cursor: str | None = None


class DatasetSummary(PublicResponse):
    short_title: str | None = None
    title: str | None = None
    dataset_type: str | None = None
    doi: str | None = None
    tcia_page: str | None = None
    resolved_access_level: str | None = None
    subjects: int | None = None
    download_data_types: str | list[Any] | None = None
    download_file_types: str | list[Any] | None = None
    external_resource_labels: list[Any] = Field(default_factory=list)


class DatasetSearchResponse(PageResponse):
    datasets: list[DatasetSummary]
    note: str


class DatasetDetailResponse(PublicResponse):
    datasets: list[dict[str, Any]]
    current_downloads: list[dict[str, Any]]
    related_analysis_results: list[dict[str, Any]]
    caveats: list[str]


class DownloadsResponse(PageResponse):
    short_title: str
    downloads: list[dict[str, Any]]


class ParticipantsResponse(PageResponse):
    participants: list[dict[str, Any]]


class AssetsResponse(PageResponse):
    assets: list[dict[str, Any]]


class HealthResponse(PublicResponse):
    status: str


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    code: str
    retryable: bool
