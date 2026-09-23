---
name: tcia-query-skill
description: Find and verify TCIA-published datasets and verified publications about TCIA data. Use for TCIA Collections or Analysis Results, participant and annotation availability, provenance, access and license questions, viewers, or official download routes.
license: Apache-2.0
---

# TCIA Query Skill

## Authority And Scope

TCIA WordPress Collection and Analysis Result records decide whether a dataset is TCIA-published. Use the release-backed SQLite snapshot or snapshot-backed MCP/REST service for normal discovery. IDC, CDA, General Commons, CTDC, PathDB, DataCite, Zenodo, and Aspera may enrich or route access; they do not establish TCIA publication.

For peer-reviewed papers about TCIA data, use TCIA's Publications EndNote export via `scripts/tcia_publications.py`; DataCite describes dataset DOI metadata, not the verified manuscript bibliography. For DOI-centered metadata, start with DataCite and confirm publication/access in WordPress.

## Check Skill Guidance Freshness

At the start of a new substantive TCIA task, when the installed scripts are
available, run:

```bash
python3 scripts/tcia_freshness.py check
```

This checks local skill-file integrity and compares the small
`skill_version.json` manifest with GitHub `main`. Successful remote checks are
cached for six hours. It does not inspect, install, or download metadata
artifacts, and it never updates the skill automatically. If it reports
`update_required`, ask the user to update the skill before making
freshness-sensitive claims. A failed network check makes skill freshness
unverified; it does not by itself require an artifact download.

## Choose The Data Surface

Choose the query surface by the agent's execution environment and the user's requested outcome:

| Environment or need | Preferred TCIA surface |
| --- | --- |
| MCP-capable agent doing an ordinary interactive query | Snapshot-backed MCP service |
| HTTP-capable agent, script, or application without MCP | REST service |
| Browser-only agent without MCP or programmable HTTP | Canonical public pages in `references/web-browser-llms.md` |
| Offline work, bulk analysis, custom SQL, pinned-release reproducibility, or operating a local server | Validated local artifacts |

This selection controls how the same release-backed evidence is accessed; it does not change TCIA publication authority, license rules, or provenance requirements. When MCP is selected, start with `get_snapshot_info`, use compact search tools, and follow only relevant candidates with detail tools. Do not download local artifacts merely to answer a routine query.

Only after the user chooses a local artifact workflow, run from the skill root:

```bash
python3 scripts/tcia_v2_bundle.py install --profile research_core
```

The bundle manifest is the release contract. The installer stages a complete generation and verifies gzip/SQLite hashes, SQLite integrity, and foreign keys before atomically switching it into service. Valid legacy flat installs migrate automatically. Install `research_detail` only for file-grain clinical, controlled-access, and public non-DICOM work; install `audit_support` only for verbose provenance/QC and the full correction registry. The compact correction digest, counts, and source-health status remain in the top manifest.

For freshness-sensitive remote answers, report the MCP/REST service's installed release fingerprint and timestamp. When the user needs confirmation that it is the latest published release, compare that fingerprint with the small current bundle manifest; do not download payload artifacts for this comparison. If the service is behind, report the deployment lag as an operator concern rather than asking the user to install local artifacts. If the comparison cannot be completed, distinguish a validated deployed generation from latest-release status instead of claiming current verification.

If local code is out of date, ask the user to update it. If remote freshness verification fails, label results offline/unverified and report the installed release fingerprint and timestamp. Do not silently switch to live WordPress discovery.

Publishing a validated release does not update a deployed MCP/REST process.
Operators still run the normal bundle install command and restart the services;
rollback selects a retained verified generation and also requires a restart.

## Query Workflow

1. Confirm the selected service or local bundle fingerprint and capabilities with `get_snapshot_info`, `/v2/bundle`, or the release manifest.
2. Use `search_datasets` for compact discovery. Follow a candidate with `get_dataset` for narrative, current downloads, license/access details, and related Analysis Results.
3. Use download-level labels for modality, file type, access, and route decisions. Split mixed datasets into open and controlled components.
4. Check related Analysis Results before saying a Collection lacks annotations, segmentations, labels, or ground truth.
5. Use `search_participants` for availability, `get_participant_assets` for drill-down, and `get_dataset_participant_coverage` before completeness claims. Participant identity is dataset-scoped; Collections and Analysis Results remain distinct.
6. Follow `next_cursor` while `has_more` is true. Keep the same filters and limit because cursors are release/component/file-generation-local and query-bound; restart after any artifact change.
7. Cite the TCIA page and DOI where available. State access/license caveats and distinguish verified, published, deployed, and unverified status.

## Route To The Right Reference

| Request | Load and use |
| --- | --- |
| Snapshot schema, SQL, releases, freshness | `references/schema.md`, `references/snapshots.md` |
| Release bundle, Participant Inventory, public non-DICOM | `references/artifact-model.md` |
| Browser-only or web-search use | `references/web-browser-llms.md` |
| MCP/REST tools, protocol, deployment, or compatibility | `mcp_server/README.md`, `references/api-upgrade-notes.md` |
| Agent/server upgrade compatibility | `references/api-upgrade-notes.md` |
| Maintainer builds, raw-source checks, operations | `references/maintainer-operations.md` |
| Publications and verified manuscripts | `references/publications.md` |
| Public DICOM and annotations | `references/idc-public-dicom.md` |
| Public DICOM missing from IDC or explicit NBIA request | `references/nbia-public-dicom-fallback.md` |
| Participant-level clinical facts | `references/clinical.md` |
| Controlled access or authorized retrieval | `references/controlled-access.md` |
| NIfTI or public non-DICOM imaging | `references/nifti.md`, `references/artifact-model.md` |
| Pathology, PathDB, or Aspera packages | `references/pathology.md`, `references/pathdb-public-pathology.md`, `references/aspera.md` |
| Viewer links | `references/visualization.md` |
| CDA enrichment | `references/cda.md` |
| General Commons | `references/general-commons-graphql.md` |
| DOI versions/relationships | `references/datacite-doi-relationships.md` |

## Access And Download Rules

- Creative Commons is open. Creative Commons NonCommercial is open with a noncommercial restriction. Controlled/restricted licenses require the current TCIA policy and authorized route.
- For public DICOM detail, preview, and retrieval planning, load `references/idc-public-dicom.md` and choose IDC MCP, REST, or local `idc-index` by environment. NBIA v4 is a fallback only when IDC lacks the requested series or the user explicitly requests it after the preference is explained.
- Controlled-access metadata and manifests do not grant authorization. Never make public viewer or download links for controlled data.
- Before transferring payloads, ask whether the user wants a direct download in the active environment or a portable manifest/file list.
- For controlled downloads, require an explicit transfer request and a user-specified path to their valid JSON key. Use only official TCIA/CRDC manifests and the official TCIA Data Retriever. Never print, copy, embed, upload, or send credential contents elsewhere.
- New Data Retriever tabular manifests use exactly one route column: `SeriesInstanceUID`, `imageUrl`, or `drs_uri`. Do not create legacy `.tcia` files.
- WordPress-provided Aspera URLs must not be reconstructed. When PathDB and Aspera both exist, explain that PathDB copies may be transformed for viewing while Aspera packages represent submitter-provided files.

## Domain Guardrails

- Preserve dataset/source provenance. A downstream record derived from a TCIA DOI remains external unless WordPress lists it as a Collection or Analysis Result.
- License metadata, not generic page visibility, determines access. Clearly distinguish open, noncommercial, mixed, and controlled/restricted states.
- Public DICOM detail remains in IDC. Use the public non-DICOM artifact for NIfTI, MHA/MHD, NRRD, images/video, pathology, and reviewed IDC-missing exceptions. Do not infer annotation-to-source relationships without evidence.
- Clinical identity is `(short_title, subject_id)`. Preserve fact provenance, inference flags, source precedence, and conflicts; load `references/clinical.md` before patient-level claims.
- Use CDA only for enrichment after validating TCIA/IDC identifiers. Do not use it to claim TCIA publication, official clinical completeness, or access rights.
- Do not broaden downstream searches beyond validated TCIA short titles, DOIs, or participant identifiers without explicit exploratory scope.
- Viewer routing: use `references/visualization.md`. For IDC, request a current viewer URL from the selected IDC surface rather than copying viewer products or URL formats into this skill; PathDB preview rules remain TCIA-specific.
- Do not install browser automation merely to show a viewer example; return a link unless browser interaction was requested.
- Do not make medical, legal, regulatory, or suitability conclusions. Report metadata, evidence, uncertainty, and access terms.

## Answer Shape

For discovery, prefer a compact ranked table with dataset, type, match reason, access/route, license, DOI, and caveats. For a specific dataset, include the TCIA page, current download-level evidence, related Analysis Results, and access route.

For recency, distinguish first release from last update and include date provenance. Treat `current_record_still_v1_date_updated` as an estimate. For freshness-sensitive work, report the verified skill version and bundle timestamp; otherwise label the result offline/unverified.
