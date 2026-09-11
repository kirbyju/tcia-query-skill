---
name: tcia-query-skill
description: Find, verify, cite, visualize, and route TCIA-published datasets and verified manuscripts about TCIA data. Use for TCIA discovery by disease, modality, body site, species, data type, access/license, DOI, program, clinical/supporting data, annotation availability, participants, viewers, or download routes across TCIA WordPress, IDC, CRDC commons, PathDB, DataCite, and Aspera.
---

# TCIA Query Skill

## Authority And Scope

TCIA WordPress Collection and Analysis Result records decide whether a dataset is TCIA-published. Use the release-backed SQLite snapshot or snapshot-backed MCP/REST service for normal discovery. IDC, CDA, General Commons, CTDC, PathDB, DataCite, Zenodo, and Aspera may enrich or route access; they do not establish TCIA publication.

Only visible WordPress records belong in public answers. The public service never exposes hidden, staged, or retired records. Maintainers investigating raw source state must use the explicit local CLI workflow in `references/maintainer-operations.md`.

For peer-reviewed papers about TCIA data, use TCIA's Publications EndNote export via `scripts/tcia_publications.py`; DataCite describes dataset DOI metadata, not the verified manuscript bibliography. For DOI-centered metadata, start with DataCite and confirm publication/access in WordPress.

## Before A Freshness-Sensitive Query

From the skill root:

```bash
python3 scripts/tcia_freshness.py check
python3 scripts/tcia_v2_bundle.py install --profile research_core
```

The bundle manifest is the release contract. The installer stages a complete generation and verifies gzip/SQLite hashes, SQLite integrity, and foreign keys before atomically switching it into service. Valid legacy flat installs migrate automatically. Install `research_detail` only for file-grain clinical, controlled-access, and public non-DICOM work; install `audit_support` only for verbose provenance/QC and the full correction registry. The compact correction digest, counts, and source-health status remain in the top manifest.

If code is out of date, ask the user to update it. If remote freshness verification fails, label results offline/unverified and report the installed release fingerprint and timestamp. Do not silently switch to live WordPress discovery. Web-only agents should follow `references/mcp-and-web-llms.md`.

Publishing a validated release does not update a deployed MCP/REST process.
Operators still run the normal bundle install command and restart the services;
rollback selects a retained verified generation and also requires a restart.

## Query Workflow

1. Confirm bundle fingerprint and capabilities with `get_snapshot_info` or the V2 manifest.
2. Use `search_datasets` for compact discovery. Follow a candidate with `get_dataset` for narrative, current downloads, license/access details, and related Analysis Results.
3. Use download-level labels for modality, file type, access, and route decisions. Split mixed datasets into open and controlled components.
4. Check related visible Analysis Results before saying a Collection lacks annotations, segmentations, labels, or ground truth.
5. Use `search_participants` for availability, `get_participant_assets` for drill-down, and `get_dataset_participant_coverage` before completeness claims. Participant identity is dataset-scoped; Collections and Analysis Results remain distinct.
6. Follow `next_cursor` while `has_more` is true. Keep the same filters and limit because cursors are release/component/file-generation-local and query-bound; restart after any artifact change.
7. Cite the TCIA page and DOI where available. State access/license caveats and distinguish verified, published, deployed, and unverified status.

## Route To The Right Reference

| Request | Load and use |
| --- | --- |
| Snapshot schema, SQL, releases, freshness | `references/schema.md`, `references/snapshots.md` |
| V2 bundle, Participant Inventory, public non-DICOM | `references/artifact-model-v2.md` |
| MCP, REST, or web-only use | `references/mcp-and-web-llms.md` |
| Agent/server upgrade compatibility | `references/api-upgrade-notes.md` |
| Maintainer builds, raw hidden-state checks, operations | `references/maintainer-operations.md` |
| Publications and verified manuscripts | `references/publications.md` |
| Public DICOM and annotations | `references/idc-dicom-downloads.md` |
| Participant-level clinical facts | `references/clinical.md` |
| Controlled access or authorized retrieval | `references/controlled-access.md` |
| NIfTI or public non-DICOM imaging | `references/nifti.md`, `references/artifact-model-v2.md` |
| Pathology, PathDB, or Aspera packages | `references/pathdb.md`, `references/aspera.md` |
| Viewer links | `references/visualization.md` |
| CDA enrichment | `references/cda.md` |
| General Commons | `references/general-commons-graphql.md` |
| DOI versions/relationships | `references/datacite-relationships.md` |

## Access And Download Rules

- Creative Commons is open. Creative Commons NonCommercial is open with a noncommercial restriction. Controlled/restricted licenses require the current TCIA policy and authorized route.
- Public DICOM detail and download should use IDC/idc-index first. NBIA v4 is a fallback only when IDC lacks the requested series or the user explicitly requests it after the preference is explained.
- Controlled-access metadata and manifests do not grant authorization. Never make public viewer or download links for controlled data.
- Before transferring payloads, ask whether the user wants a direct download in the active environment or a portable manifest/file list.
- For controlled downloads, require an explicit transfer request and a user-specified path to their valid JSON key. Use only official TCIA/CRDC manifests and the official TCIA Data Retriever. Never print, copy, embed, upload, or send credential contents elsewhere.
- New Data Retriever tabular manifests use exactly one route column: `SeriesInstanceUID`, `imageUrl`, or `drs_uri`. Do not create legacy `.tcia` files.
- WordPress-provided Aspera URLs must not be reconstructed. When PathDB and Aspera both exist, explain that PathDB copies may be transformed for viewing while Aspera packages represent submitter-provided files.

## Domain Guardrails

- Preserve dataset/source provenance. A downstream record derived from a TCIA DOI remains external unless WordPress lists it as a Collection or Analysis Result.
- License metadata, not generic page visibility, determines access. Clearly distinguish open, noncommercial, mixed, and controlled/restricted states.
- Public DICOM detail remains in IDC. Use the V2 public non-DICOM artifact for NIfTI, MHA/MHD, NRRD, images/video, pathology, and reviewed IDC-missing exceptions. Do not infer annotation-to-source relationships without evidence.
- Clinical identity is `(short_title, subject_id)`. Preserve fact provenance, inference flags, source precedence, and conflicts; load `references/clinical.md` before patient-level claims.
- Use CDA only for enrichment after validating TCIA/IDC identifiers. Do not use it to claim TCIA publication, official clinical completeness, or access rights.
- Do not broaden downstream searches beyond validated TCIA short titles, DOIs, or participant identifiers without explicit exploratory scope.
- Viewer routing: OHIF for public radiology, SliM for public slide microscopy, and VolView only after mapping to a public S3 path or CRDC series UUID. VolView is not UID-based.
- Do not install browser automation merely to show a viewer example; return a link unless browser interaction was requested.
- Do not make medical, legal, regulatory, or suitability conclusions. Report metadata, evidence, uncertainty, and access terms.

## Answer Shape

For discovery, prefer a compact ranked table with dataset, type, match reason, access/route, license, DOI, and caveats. For a specific dataset, include the TCIA page, current download-level evidence, related Analysis Results, and access route.

For recency, distinguish first release from last update and include date provenance. Treat `current_record_still_v1_date_updated` as an estimate. For freshness-sensitive work, report the verified skill version and bundle timestamp; otherwise label the result offline/unverified.
