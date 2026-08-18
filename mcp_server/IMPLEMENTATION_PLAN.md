# TCIA Query MCP/REST Implementation Plan

## Goals

Build a read-only server surface that lets MCP clients, hosted LLMs, scripts,
and small web apps query TCIA-published dataset metadata without installing the
Codex skill or falling back to live WordPress scraping.

## Architecture

Follow the IDC-REST-MCP pattern:

1. Keep TCIA query behavior in a protocol-independent service layer
   (`tcia_query_mcp/service.py`).
2. Expose normal HTTP endpoints through FastAPI (`tcia_query_mcp/rest.py`).
3. Expose LLM-facing tools and guidance resources through FastMCP
   (`tcia_query_mcp/server.py`).
4. Test the service layer directly, then add parity tests for REST/MCP as the
   server surface stabilizes.

## Data Sources And Release Contract

- Required V2 research core: `tcia_snapshot.sqlite`,
  `participant_inventory.sqlite`, and the top-level bundle manifest.
- Optional V2 detail: `public_non_dicom_metadata.sqlite`.
- Optional but supported: `controlled_access_metadata.sqlite`.
- Optional but supported: `nifti_metadata.sqlite`.
- Optional but supported: `pathology_metadata.sqlite`.
- Optional but supported: `clinical_metadata.sqlite`.

The base snapshot remains the TCIA provenance, visibility, release-history,
DOI, and access/license authority. Participant Inventory is the participant
identity and availability surface. Optional detail artifacts add file-grain
public non-DICOM, controlled-access, NIfTI, pathology, PathDB, Aspera inventory,
and patient-level clinical metadata. Public DICOM detail stays in IDC.

## Tool Families

- Snapshot/version: `get_snapshot_info`, `get_dataset_versions`,
  `get_dataset_v1_releases`.
- Dataset discovery: `search_datasets`, `get_dataset`.
- Participant discovery: `search_participants`, `get_participant`,
  `get_participant_assets`, `get_dataset_participant_coverage`,
  `find_participant_link_issues`.
- Public non-DICOM: `find_public_non_dicom_assets`.
- Download/access: `get_current_downloads`, `summarize_access`,
  `find_dicom_annotations`.
- Controlled access: `find_controlled_access_datasets`,
  `get_controlled_access_files`.
- NIfTI: `find_nifti_datasets`, `get_nifti_files`,
  `get_nifti_derived_objects`, `get_nifti_characteristics`,
  `find_nifti_review_issues`, `get_nifti_package_files`.
- Pathology/Aspera: `find_pathology_datasets`, `get_pathology_downloads`,
  `get_pathology_package_files`, `get_pathology_file_objects`,
  `get_pathology_disparities`.
- Clinical: `find_clinical_datasets`, `get_clinical_subjects`,
  `get_clinical_facts`, `get_clinical_conflicts`.

## Guardrails

- Do not expose arbitrary SQL, shell execution, or live WordPress scraping.
- Exclude hidden/staged/retired WordPress rows by default.
- Use WordPress Collection and Analysis Result rows as the TCIA publication
  authority.
- Use download-level labels for modality, file type, and access routing.
- Split mixed-access datasets and never imply that all files are open.
- Keep this MCP server read-only; it does not transfer controlled payloads. Client
  agents may use official TCIA Data Retriever manifests after an authorized user
  supplies their JSON-key path, following the skill's controlled-access guidance.
- Use optional SQLite sidecars only as public metadata surfaces; they do not
  grant authorization.
- Scope clinical identities by `(short_title, subject_id)`, retain source
  provenance/conflicts, and flag dataset-scope inferred values.
- Use `scripts/tcia_v2_bundle.py install` so the top-level release fingerprint,
  component hashes, decompressed SQLite hashes, and integrity checks are
  validated together. Keep individual `ensure` commands for compatibility.

## Deployment Shape

Run the Git checkout, virtual environment, pip cache, logs, and snapshots under
a configurable runtime root such as `/srv/tcia-query-mcp` or a larger mounted
data volume. Keep only OS-managed service files, reverse-proxy configuration,
TLS material, and firewall rules outside that runtime root.
