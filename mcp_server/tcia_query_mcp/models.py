"""Closed public V2 response contracts shared by REST and MCP."""

from __future__ import annotations

from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue


JsonScalar: TypeAlias = str | int | float | bool | None
JsonObject: TypeAlias = dict[str, JsonValue]
TextValues: TypeAlias = str | list[str] | None


class PublicModel(BaseModel):
    """Strict boundary model; additive data belongs in a named extension field."""

    model_config = ConfigDict(extra="forbid", strict=True)


class PageResponse(PublicModel):
    count: int = Field(ge=0)
    limit: int = Field(ge=1)
    has_more: bool
    truncated: bool
    next_cursor: str | None = None


class DatasetSummary(PublicModel):
    short_title: str | None = None
    title: str | None = None
    dataset_type: str | None = None
    doi: str | None = None
    tcia_page: str | None = None
    date_updated: str | None = None
    current_version_number: str | None = None
    license_status: str | None = None
    resolved_access_level: str | None = None
    current_download_count: int | None = None
    subjects: int | None = None
    download_data_types: TextValues = None
    download_file_types: TextValues = None
    external_resource_labels: list[str] = Field(default_factory=list)
    cancer_types: TextValues = None
    cancer_locations: TextValues = None
    species: TextValues = None
    source_collections: TextValues = None


class DatasetDetail(DatasetSummary):
    hidden: bool | None = None
    licenses: TextValues = None
    access_level: str | None = None
    controlled_access: bool | None = None
    noncommercial_license: bool | None = None
    controlled_download_count: int | None = None
    noncontrolled_download_count: int | None = None
    data_types: list[str] = Field(default_factory=list)
    download_types: list[str] = Field(default_factory=list)
    external_resources: list[str] = Field(default_factory=list)
    has_tcia_clinical_download: bool | None = None
    has_external_clinical_resource: bool | None = None
    program: str | None = None
    summary: str | None = None
    abstract: str | None = None
    detailed_description: str | None = None
    controlled_access_policy_url: str | None = None


class Download(PublicModel):
    short_title: str | None = None
    dataset_title: str | None = None
    dataset_type: str | None = None
    download_id: str | None = None
    download_title: str | None = None
    download_url: str | None = None
    search_url: str | None = None
    download_types: list[str] = Field(default_factory=list)
    data_types: list[str] = Field(default_factory=list)
    file_types: list[str] = Field(default_factory=list)
    external_resources: list[str] = Field(default_factory=list)
    access_level: str | None = None
    controlled_access: bool | None = None
    noncommercial_license: bool | None = None
    license_label: str | None = None
    license_url: str | None = None
    requirements_label: str | None = None
    requirements_text: str | None = None
    download_size: str | None = None
    download_size_unit: str | None = None
    subjects: int | None = None
    studies: int | None = None
    series: int | None = None
    images: int | None = None
    controlled_access_policy_url: str | None = None
    route_system: str | None = None
    route_manifest_kind: str | None = None
    metadata_artifact_kind: str | None = None


class Participant(PublicModel):
    participant_key: str
    dataset_type: str
    short_title: str
    display_participant_id: str
    identity_scope: str | None = None
    within_dataset_identity_status: str | None = None
    identity_resolution_method: str | None = None
    cross_dataset_identity_status: str | None = None
    source_namespace_count: int | None = None
    source_namespaces: list[str] = Field(default_factory=list)
    inventory_rows: int | None = None
    has_open_data: int | None = None
    has_controlled_data: int | None = None
    has_public_dicom: int | None = None
    has_public_non_dicom: int | None = None
    has_clinical: int | None = None
    data_domains: list[str] = Field(default_factory=list)
    data_categories: list[str] = Field(default_factory=list)
    data_types: list[str] = Field(default_factory=list)
    modalities: list[str] = Field(default_factory=list)
    file_formats: list[str] = Field(default_factory=list)
    geometry_statuses: list[str] = Field(default_factory=list)
    geometry_checked_count: int | None = None
    geometry_regular_count: int | None = None
    geometry_not_regular_count: int | None = None
    geometry_not_checked_count: int | None = None
    managed_systems: list[str] = Field(default_factory=list)


class ParticipantIdentifier(PublicModel):
    participant_identifier_id: str | None = None
    participant_key: str
    dataset_type: str | None = None
    short_title: str | None = None
    display_participant_id: str | None = None
    managed_system: str | None = None
    identifier_namespace: str | None = None
    raw_identifier: str | None = None
    normalized_identifier: str | None = None
    link_evidence: str | None = None
    provenance_json: JsonObject = Field(default_factory=dict)


class Asset(PublicModel):
    participant_asset_id: str | None = None
    asset_id: str | None = None
    participant_key: str | None = None
    display_participant_id: str | None = None
    dataset_type: str | None = None
    short_title: str | None = None
    download_id: str | None = None
    managed_system: str | None = None
    managed_systems: list[str] = Field(default_factory=list)
    source_system: str | None = None
    source_artifact: str | None = None
    source_version: str | None = None
    source_url: str | None = None
    access_level: str | None = None
    access_route: str | None = None
    data_domain: str | None = None
    data_category: str | None = None
    data_type: str | None = None
    modality: str | None = None
    imaging_domain: str | None = None
    file_format: str | None = None
    media_kind: str | None = None
    object_role: str | None = None
    geometry_status: str | None = None
    file_name: str | None = None
    package_path: str | None = None
    asset_name: str | None = None
    asset_granularity: str | None = None
    subject_id: str | None = None
    study_count: int | None = None
    series_count: int | None = None
    file_count: int | None = None
    location_count: int | None = None
    participant_link_count: int | None = None
    known_size_bytes: int | None = None
    has_file_level_metadata: int | None = None
    detail_pointer: str | None = None
    inventory_status: str | None = None
    provenance_json: JsonObject = Field(default_factory=dict)


class DatasetVersion(PublicModel):
    short_title: str | None = None
    title: str | None = None
    dataset_type: str | None = None
    doi: str | None = None
    tcia_page: str | None = None
    date_updated: str | None = None
    current_version_number: str | None = None
    subjects: int | None = None
    hidden: bool | None = None
    version_id: str | None = None
    version_slug: str | None = None
    version_post_title: str | None = None
    version_number: str | None = None
    version_date: str | None = None
    version_related_short_title: str | None = None
    match_method: str | None = None
    version_downloads: list[JsonObject] = Field(default_factory=list)
    version_text: str | None = None


class V1Release(PublicModel):
    short_title: str | None = None
    title: str | None = None
    dataset_type: str | None = None
    doi: str | None = None
    tcia_page: str | None = None
    date_updated: str | None = None
    current_version_number: str | None = None
    subjects: int | None = None
    hidden: bool | None = None
    v1_release_date: str | None = None
    v1_release_date_source: str | None = None
    version_id: str | None = None
    version_slug: str | None = None
    version_post_title: str | None = None
    version_related_short_title: str | None = None
    match_method: str | None = None


class ParticipantLinkIssue(PublicModel):
    issue_id: str | None = None
    dataset_type: str | None = None
    short_title: str | None = None
    raw_identifier: str | None = None
    issue_code: str | None = None
    status: str | None = None
    description: str | None = None
    evidence_json: JsonObject = Field(default_factory=dict)


class UnlinkedDatasetAsset(PublicModel):
    dataset_asset_id: str | None = None
    dataset_type: str | None = None
    short_title: str | None = None
    managed_system: str | None = None
    access_level: str | None = None
    data_domain: str | None = None
    media_kind: str | None = None
    modality: str | None = None
    file_format: str | None = None
    object_role: str | None = None
    asset_count: int | None = None
    detail_pointer: str | None = None
    explanation: str | None = None
    provenance_json: JsonObject = Field(default_factory=dict)


class InventorySource(PublicModel):
    source_name: str
    present: int | None = None
    imported_rows: int | None = None
    source_sha256: str | None = None
    coverage_note: str | None = None


class ControlledDataset(PublicModel):
    dataset: DatasetSummary
    matching_downloads: list[Download]


class ControlledFile(PublicModel):
    short_title: str | None = None
    dataset_type: str | None = None
    title: str | None = None
    doi: str | None = None
    route_system: str | None = None
    download_id: str | None = None
    download_title: str | None = None
    access_level: str | None = None
    controlled_access_policy_url: str | None = None
    license_label: str | None = None
    drs_uri: str | None = None
    file_id: str | None = None
    file_name: str | None = None
    file_type: str | None = None
    file_format: str | None = None
    file_size_bytes: int | None = None
    study_name: str | None = None
    study_accession: str | None = None
    participant_id: str | None = None
    patient_id: str | None = None
    patient_sex: str | None = None
    diagnosis: str | None = None
    image_modality: str | None = None
    modality: str | None = None
    body_part_examined: str | None = None
    study_instance_uid: str | None = None
    series_instance_uid: str | None = None
    series_description: str | None = None
    manufacturer: str | None = None
    source_manifest_url: str | None = None
    source_metadata_url: str | None = None


class ClinicalDataset(PublicModel):
    short_title: str
    dataset_types: list[str]
    subjects: int | None = None
    all_source_subjects: int | None = None
    clinical_only_subjects: int | None = None
    subjects_with_conflicts: int | None = None
    subjects_with_inferred_diagnosis: int | None = None
    subjects_with_inferred_site: int | None = None
    source_kinds: list[str] = Field(default_factory=list)
    concepts: int | None = None


class ClinicalResolvedSource(PublicModel):
    source_kind: str | None = None
    priority: int | None = None


class ClinicalSubject(PublicModel):
    subject_key: str
    short_title: str
    subject_id: str
    source_kinds: list[str] = Field(default_factory=list)
    source_count: int | None = None
    conflict_count: int | None = None
    has_imaging: bool | None = None
    sex_at_birth: str | None = None
    race: str | None = None
    ethnicity: str | None = None
    age_at_diagnosis: str | None = None
    age_at_enrollment_years: str | None = None
    age_at_imaging_years: str | None = None
    primary_diagnosis: str | None = None
    primary_site: str | None = None
    stage: str | None = None
    grade: str | None = None
    vital_status: str | None = None
    days_to_death: str | None = None
    days_to_last_followup: str | None = None
    overall_survival_days: str | None = None
    progression_free_survival_days: str | None = None
    recurrence: str | None = None
    progression: str | None = None
    response: str | None = None
    screening_result: str | None = None
    primary_diagnosis_is_inferred: bool | None = None
    primary_site_is_inferred: bool | None = None
    resolved_values: dict[str, JsonScalar] = Field(default_factory=dict)
    resolved_sources: dict[str, ClinicalResolvedSource] = Field(default_factory=dict)
    conflicts: dict[str, list[str]] = Field(default_factory=dict)


class ClinicalFact(PublicModel):
    short_title: str
    subject_id: str
    concept: str
    value_text: str | None = None
    value_number: float | int | None = None
    unit: str | None = None
    source_kind: str | None = None
    source_priority: int | None = None
    source_url: str | None = None
    source_date: str | None = None
    original_column: str | None = None
    evidence_scope: str | None = None
    is_inferred: bool | None = None
    provenance: JsonObject = Field(default_factory=dict)


class ClinicalConflict(PublicModel):
    short_title: str
    subject_id: str
    concept: str
    distinct_values: int | None = None
    values_seen: list[str] = Field(default_factory=list)
    source_kinds: list[str] = Field(default_factory=list)


class BundleProducer(PublicModel):
    commit: str | None = None
    repository: str | None = None
    skill_version: str | None = None


class BundleComponent(PublicModel):
    database_asset: str
    schema_version: str | int
    sqlite_sha256: str
    gzip_sha256: str | None = None
    manifest_asset: str | None = None
    source_manifest: str | None = None
    profile: str | None = None
    release_fingerprint: str | None = None
    storage_contract: str | JsonObject | None = None
    provenance: JsonObject | None = None


class BundleProfile(PublicModel):
    assets: list[str]
    depends_on: list[str] = Field(default_factory=list)


class BundleManifest(PublicModel):
    artifact: str
    schema_version: int
    release_channel: str | None = None
    release_tag: str | None = None
    release_contract: str | None = None
    release_fingerprint: str
    generated_at_utc: str | None = None
    producer: BundleProducer | None = None
    components: dict[str, BundleComponent]
    profiles: dict[str, BundleProfile]


class BundleInstall(PublicModel):
    artifact: str
    installed_assets: list[str]
    installed_profile: str
    release_fingerprint: str
    installed_at_utc: str | None = None
    release_channel: str | None = None
    release_tag: str | None = None


class Capabilities(PublicModel):
    participant_search: bool
    public_non_dicom_detail: bool
    controlled_access_detail: bool | None = None
    clinical_detail: bool | None = None
    audit_support: bool | None = None
    bundle_manifest: bool
    install_state: bool
    public_dicom_authority: str
    publication_authority: str


class DatasetSearchResponse(PageResponse):
    datasets: list[DatasetSummary]
    note: str


class DatasetDetailResponse(PublicModel):
    datasets: list[DatasetDetail]
    current_downloads: list[Download]
    related_analysis_results: list[DatasetSummary]
    caveats: list[str]


class DownloadsResponse(PageResponse):
    short_title: str
    downloads: list[Download]


class ParticipantsResponse(PageResponse):
    participants: list[Participant]
    identity_scope: str
    public_dicom_detail_route: str


class AssetsResponse(PageResponse):
    assets: list[Asset]
    participant_key: str | None = None
    public_dicom_detail_route: str | None = None
    scope: str | None = None
    annotation_roles: list[str] = Field(default_factory=list)
    annotation_relationship_scope: str | None = None


class BundleResponse(PublicModel):
    v2_bundle_manifest_exists: bool
    v2_install_state_exists: bool
    count_policy: str
    v2_bundle: BundleManifest | None = None
    v2_bundle_error: str | None = None
    v2_install: BundleInstall | None = None
    v2_install_state_error: str | None = None
    v2_capabilities: Capabilities


class HealthResponse(PublicModel):
    status: str
    release_fingerprint: str | None = None
    release_tag: str | None = None
    installed_profile: str | None = None
    capabilities: Capabilities | None = None


class ParticipantDetailResponse(PublicModel):
    participant: Participant
    identifiers: list[ParticipantIdentifier]
    count: int = Field(ge=0)
    identity_scope: str


class CoverageResponse(PublicModel):
    short_title: str
    dataset_type: str | None = None
    participant_count: int = Field(ge=0)
    unlinked_dataset_assets: list[UnlinkedDatasetAsset]
    participant_link_issues: list[ParticipantLinkIssue]
    sources: list[InventorySource]
    coverage_complete: bool


class LinkIssuesResponse(PublicModel):
    participant_link_issues: list[ParticipantLinkIssue]
    count: int = Field(ge=0)
    limit: int = Field(ge=1)


class DatasetVersionsResponse(PublicModel):
    short_title: str
    available: bool
    versions: list[DatasetVersion]
    count: int = Field(ge=0)
    limit: int | None = Field(default=None, ge=1)
    note: str


class V1ReleasesResponse(PageResponse):
    available: bool
    v1_releases: list[V1Release]
    note: str


class AccessSummaryResponse(PublicModel):
    dataset: DatasetDetail
    download_groups: dict[str, list[Download]]
    controlled_access_policy_url: str
    notes: list[str]


class ControlledDatasetsResponse(PublicModel):
    datasets: list[ControlledDataset]
    count: int = Field(ge=0)
    limit: int = Field(ge=1)
    controlled_access_policy_url: str
    note: str


class ControlledFilesResponse(PublicModel):
    short_title: str
    files: list[ControlledFile]
    count: int = Field(ge=0)
    limit: int = Field(ge=1)
    controlled_access_policy_url: str
    warning: str


class DicomAnnotationsResponse(PublicModel):
    downloads: list[Download]
    count: int = Field(ge=0)
    limit: int = Field(ge=1)
    routing_note: str
    scope: str
    public_dicom_annotation_detail: dict[str, str]


class ClinicalDatasetsResponse(PublicModel):
    datasets: list[ClinicalDataset]
    count: int = Field(ge=0)
    limit: int = Field(ge=1)
    note: str


class ClinicalSubjectsResponse(PublicModel):
    short_title: str
    dataset_types: list[str]
    subjects: list[ClinicalSubject]
    count: int = Field(ge=0)
    limit: int = Field(ge=1)
    includes_clinical_only_subjects: bool
    identity_scope: str


class ClinicalFactsResponse(PublicModel):
    short_title: str
    dataset_types: list[str]
    facts: list[ClinicalFact]
    count: int = Field(ge=0)
    limit: int = Field(ge=1)
    note: str


class ClinicalConflictsResponse(PublicModel):
    short_title: str
    dataset_types: list[str]
    conflicts: list[ClinicalConflict]
    count: int = Field(ge=0)
    limit: int = Field(ge=1)
    note: str


class ProblemDetail(PublicModel):
    type: str
    title: str
    status: int
    detail: str
    code: str
    retryable: bool
