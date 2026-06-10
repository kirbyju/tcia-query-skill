# TCIA Routing Reference

## Authority And Keys

Use TCIA WordPress as the authoritative allowlist. For normal agent work, query the local SQLite snapshot and its agent-facing views first. For web-only environments without SQLite execution, use the release JSON/JSONL exports before any live API. The endpoints below are source inputs for the snapshot builder.

- Collections endpoint: `https://cancerimagingarchive.net/api/v1/collections/`
- Analysis Results endpoint: `https://cancerimagingarchive.net/api/v1/analysis-results/`
- Preferred v2 endpoints used by the snapshot builder:
  - `https://cancerimagingarchive.net/api/v2/collections`
  - `https://cancerimagingarchive.net/api/v2/analysis-results`

Exclude records where `hide_from_browse_table = "1"` unless the user explicitly says they are a TCIA staff member and asks to include hidden, staged, retired, or internal-review datasets. Hidden records may be pre-release staging pages for submitter review or retired/outdated datasets that TCIA does not want users to accidentally select.

Use WordPress license metadata to decide open versus controlled access. Do not use `collection_page_accessibility` or `result_page_accessibility`; those fields are being phased out. Creative Commons licenses mean open access. Creative Commons NonCommercial licenses are open access with a noncommercial-use restriction. If license text indicates NIH Controlled Data Access, TCIA Restricted, or another controlled/restricted license, alert users that the dataset is not open access and link to the TCIA NIH Controlled Data Access Policy before giving access, API-key, or TCIA Data Retriever instructions: `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/`.

Use these WordPress fields as cross-system keys:

- Collections: `collection_short_title`
- Analysis Results: `result_short_title`

Downstream field mappings:

| System | Matching field |
| --- | --- |
| General Commons | `study_acronym`, scoped to `phs004225` |
| Cancer Data Aggregator | `subject_id` or source-specific IDs exposed through `upstream_identifiers.*`; validate identifiers from TCIA/IDC before matching |
| PathDB cohort-builder CSV | `collection` |
| PathDB API collection list | `collectionName` |
| DataCite | TCIA DOI and related identifiers |
| IDC | Prefer DOI, collection/analysis metadata, and Series Instance UIDs from IDC, but keep WordPress as the provenance anchor |

## Discovery Process

For DOI, citation, or version questions, start with DataCite records in the SQLite snapshot, not WordPress. Use `agent_datacite_dois` or `scripts/datacite_tcia_dois.py`, then use `agent_datasets` to confirm TCIA publication, hidden/visible status, access/license, and dataset pages.

For peer-reviewed manuscripts written about TCIA data, start with TCIA's Publications EndNote XML export, not DataCite. Load `references/publications.md` and use `scripts/tcia_publications.py` to search title, abstract, keywords, journal, PMID, manuscript DOI, and linked TCIA dataset DOI values.

For non-DOI discovery:

1. Query `agent_datasets` for both WordPress Collections and Analysis Results.
2. Use `agent_current_downloads` and `agent_dataset_access_summary` when the answer depends on modalities, files, route labels, or access/license details.
3. Remove hidden records by default.
4. Filter locally so criteria can match custom fields, download labels, and flattened snapshot columns.
5. Use the snapshot's verbose-normalized text fields for abstracts/descriptions when needed.
6. Flag controlled access from license metadata only. Creative Commons means open; Creative Commons NonCommercial means open with noncommercial restriction; controlled/restricted license text means controlled access.
7. Enrich only the filtered candidate set through IDC, CDA, General Commons, PathDB, or DataCite.
8. If a candidate does not appear in WordPress, exclude it from TCIA-published results. If useful, mention it separately as related or derived.
9. If a named dataset is absent after refreshing the local snapshot, say the published snapshot may not include the newest TCIA metadata yet. Ask the user to try again after the next 7:17 AM or 7:17 PM America/New_York snapshot run has had time to finish, then rerun `python scripts/tcia_snapshot.py ensure`.

## Snapshot Querying

Prefer direct SQL against the agent-facing views when the user asks for precise criteria, joins, or counts. Prefer `scripts/tcia_wordpress_search.py`, `scripts/pathdb_metadata.py`, and `scripts/datacite_tcia_dois.py` for lightweight command-line searches.

If the local SQLite file is missing, run or ask the user to run `python scripts/tcia_snapshot.py ensure`. End users do not need to reinstall the skill to update metadata; `ensure` refreshes the SQLite cache from the latest release snapshot when the content hash changed.

Live source API details are maintainer/developer context for `scripts/tcia_snapshot.py build`, not the normal end-user discovery path. If an agent cannot query SQLite, it should use the release exports documented in `snapshots.md` and `mcp-and-web-llms.md`, not live WordPress API calls.

## Access Route Details

Public DICOM radiology or DICOM pathology:

- Route open-access/public DICOM to IDC and `idc-index` first. This also applies to public DICOM annotation/result objects such as RTSTRUCT, SEG, SR, RTDOSE, RTPLAN, and other DICOM files.
- Use IDC-specific tooling for series selection, series/file metadata, visualization, licenses, citations, and downloads after TCIA provenance and access/license checks are established from the snapshot.
- Do not query live WordPress for DICOM series, modality, annotation, or file details during end-user tasks. The snapshot's WordPress fields identify TCIA publication/download/access metadata; IDC/idc-index is the preferred public DICOM detail source.
- Avoid duplicating the IDC skill. If available, use it after TCIA provenance is established.
- For browser visualization, load `visualization.md`. Use OHIF v3 for open radiology DICOM, SliM for open DICOM slide microscopy (`SM`), and VolView only after mapping the requested series to a public S3 folder or `crdc_series_uuid`. Return clickable links; do not install browser automation just to preview examples.
- Before downloading DICOM data, ask whether the user wants direct agent download in the current environment or a portable TCIA Data Retriever CSV manifest created from Series Instance UIDs.
- TCIA is phasing out NBIA. Do not use NBIA as the first route for public DICOM downloads.
- For new Data Retriever CSV manifests, `SeriesInstanceUID` means public DICOM: IDC/S3 first, then TCIA/NBIA v4 fallback only when needed.
- Use existing WordPress `.tcia` manifest files as Series Instance UID allowlists for IDC/idc-index lookups when helpful, but treat `.tcia` as a legacy input format.
- Use NBIA only as a fallback when requested public DICOM series cannot be found in IDC/idc-index, or when the user explicitly asks for NBIA after being warned that IDC is preferred.
- If NBIA fallback is needed, tell users to use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.
- Load `idc-dicom-downloads.md` for the TCIA-specific IDC download workflow.

Controlled-access face datasets:

- If license metadata indicates controlled/restricted access, alert users that the dataset is controlled access, not open access.
- Link to `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/` for current request, JSON API key, and TCIA Data Retriever configuration guidance.
- Do not construct public browser viewer links. Controlled-access data cannot be visualized in a browser before download regardless of file format.
- Route to General Commons.
- Scope all GC queries to `phs004225`.
- Match WordPress short title to GC `study_acronym`.
- Do not imply unauthenticated download.

Controlled-access NCTN trials or Biobank data:

- If license metadata indicates controlled/restricted access, alert users that the dataset is controlled access, not open access.
- Link to the TCIA NIH Controlled Data Access Policy for current access-request and API-key guidance.
- Do not construct public browser viewer links. Controlled-access data cannot be visualized in a browser before download regardless of file format.
- Use WordPress license metadata and dataset pages for now.
- CTDC is planned but should not be used until TCIA data and matching fields are confirmed there.

CDA subject enrichment:

- Use CDA after WordPress provenance is established and after TCIA/IDC subject identifiers have been validated.
- Prefer `cdapython` over direct `cda-client` or handwritten CDA REST calls. Load `cda.md`.
- Good CDA questions include: which subjects in this TCIA/IDC cohort have GDC/PDC/GC/IDC data, what harmonized demographics/diagnoses/treatments are available, what file categories/formats/access levels exist, and what upstream identifiers can route users into source commons.
- For a known subject list, use `match_from_file` against `subject_id` when identifiers are already in CDA subject-id form. If raw DICOM `PatientID` or WordPress spreadsheet IDs do not match, inspect CDA `columns(table="upstream_identifiers")` and use upstream identifier columns instead of broad wildcard matching.
- For cohort summaries, use `summarize_subjects()` for demographics/diagnosis-style counts and `summarize_files()` for file categories, formats, data sources, and open/controlled file counts.
- For row-level enrichment, use `get_subject_data(add_columns="upstream_identifiers.*", include_external_refs=True)` and add targeted tables such as `observation.*` or `treatment.*` only when the user needs those details.
- CDA file `access` values help triage open versus controlled source files, but they do not authorize downloads. Route file access to the source commons and keep TCIA license metadata for TCIA download guidance.

Non-DICOM pathology:

- Route slide-level metadata questions to PathDB.
- Prefer the stable PathDB cohort-builder CSV for rich slide-level metadata.
- Match WordPress short title to CSV `collection`; the PathDB API collection list may use `collectionName`.
- For public pathology Aspera download/package scope, PathDB coverage gaps, and package-file reconciliation, use `references/pathology.md` and `scripts/tcia_pathology_metadata.py` after WordPress provenance/access is confirmed.
- For open/public PathDB slides, construct caMicroscope browser viewer URLs from CSV `camic_id`: `https://pathdb.cancerimagingarchive.net/caMicroscope/apps/mini/viewer.html?mode=pathdb&slideId=<camic_id>`. The URL parameter is named `slideId`, but it must use numeric `camic_id`, not CSV `slide_id` or `patient_id`.
- Use `tcia_utils.pathdb` if installed.
- Load `pathdb.md` for the stable CSV URL, columns, and helper script.

Supporting files:

- Use WordPress download metadata and dataset page links.
- For IBM Aspera Faspex package links, see `aspera.md`.

DOI/citation:

- Start with DataCite to inspect DOI metadata, related identifiers, versions, and external derived records.
- Use WordPress after DataCite to confirm visible TCIA Collection/Analysis Result status, access/license, and dataset page/download routing.

Peer-reviewed publications:

- Start with TCIA Publications EndNote XML: `https://cancerimagingarchive.net/endnote/Pubs_basedon_TCIA.xml`.
- Use `scripts/tcia_publications.py` for verified papers written about TCIA datasets.
- Use linked TCIA dataset DOI values from `remote-database-name` to connect papers back to WordPress/DataCite dataset records when dataset metadata or access routes are needed.
- Do not treat DataCite dataset DOI records as the bibliography of papers that used TCIA data.

## Download Label Interpretation

Use `agent_current_downloads` when answering file/download questions. If you need lower-level detail, use `wordpress_downloads` joined to `wordpress_download_labels` with `is_current_version IS TRUE` for current-version user-facing downloads. Interpret these fields together:

| Field | Meaning |
| --- | --- |
| `download_type` | Broad category label, such as Radiology Images, Image Annotations, Clinical Data, Pathology Images, or Other |
| `data_type` | More specific content/modality/metadata label, such as CT, MR, RTSTRUCT, SEG, Segmentation, Demographic, Protocol, Histopathology, or Whole Slide Image |
| `file_type` | Physical or logical file format, such as DICOM, CSV, TSV, XLSX, ZIP, NIfTI, SVS, JSON, or PDF |

For Collections, use `collection_downloads` as the actual dataset download records. For Analysis Results, use `result_downloads` as the actual result file records. Source collection metadata explains provenance only; do not present source collection downloads as if they are the Analysis Result files. If an Analysis Result lacks `result_downloads`, say that result-file metadata is unavailable in the current snapshot instead of substituting `collection_downloads`. Do not rely only on top-level `data_types` for modality filtering; mixed collections can have modality labels only on individual download records.

`download_type` is intended as the parent category, but all three fields are multi-select. Mixed parent categories are normal when one download record represents a combined TCIA Data Retriever manifest or package. Examples:

- A single Data Retriever manifest may include source radiology images plus annotation DICOM objects, such as CT/MR with RTSTRUCT or SEG.
- A single ZIP/supporting package may include image annotations plus files that are best categorized as Other, such as acquisition protocol details.

Do not collapse mixed labels into one category. Report the mixed content and choose routes based on the files the user wants:

- DICOM radiology images and DICOM annotation objects can usually be explored through IDC after WordPress provenance and license checks.
- Non-DICOM annotation files, spreadsheets, ZIP packages, and supporting files usually route through WordPress download links, Aspera packages, or the dataset page.
- Non-DICOM pathology image labels should be checked against PathDB guidance when slide-level metadata or image URLs are needed.

If a global downloads endpoint row has no `download_type`, `data_type`, `file_type`, title, or URL, treat it as a metadata anomaly and ignore it for normal discovery. Prefer downloads nested under a visible Collection or Analysis Result for user-facing answers.

## Recommended Response Fields

For search/discovery:

| Field | Notes |
| --- | --- |
| Dataset | Use WordPress title and short title |
| Type | Collection or Analysis Result |
| Match reason | Cite the matching cancer type, modality, data type, body site, DOI, etc. |
| Access route | IDC, General Commons, PathDB, WordPress downloads, Aspera, or DataCite |
| Visualization route | None for controlled access; OHIF v3, SliM, or VolView for open/public DICOM in IDC; caMicroscope for open/public PathDB slides |
| Download delivery | Direct agent download or portable Data Retriever CSV manifest when DICOM Series Instance UIDs are available |
| Access/license | Open Creative Commons, Open Creative Commons NonCommercial, controlled/restricted, or license-review-needed |
| DOI/citation | Link DOI when present |
| Notes | Include caveats, related external results, size/counts, or next step |

For exact dataset questions, give a short prose summary first, then a table of access routes and citations.

## Common Caveats

- WordPress metadata can contain HTML; strip tags before quoting or matching.
- WordPress `hide_from_browse_table = "1"` means hidden. Treat hidden records as out of scope for public user-facing discovery unless the explicit TCIA staff exception applies.
- The snapshot is built from verbose WordPress source metadata. If a very recent field is absent, ask the user to try again after the next scheduled snapshot run and refresh with `python scripts/tcia_snapshot.py ensure`.
- Controlled-access metadata can be visible even when file downloads require approval. Determine controlled status from license metadata, then link to the TCIA NIH Controlled Data Access Policy for current request, JSON API key, and TCIA Data Retriever configuration steps.
- Controlled-access data cannot be previewed through public browser viewers before download. Report metadata and access guidance instead of constructing OHIF, SliM, VolView, IDC, NBIA, PathDB, or other public viewer URLs.
- Visualization answers should provide links for users to open in their own browser. Do not install Playwright or other browser automation just to demonstrate viewer links.
- For open-access/public DICOM downloads, prefer IDC/idc-index. Existing TCIA `.tcia` manifests can be parsed for Series Instance UID allowlists, but NBIA should be fallback-only for public DICOM. New portable Data Retriever manifests should be CSV/TSV/XLSX-compatible, not `.tcia`, unless the user explicitly asks for the legacy NBIA-era format. If NBIA fallback is needed, use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`. For controlled-access DICOM, use WordPress license metadata and General Commons under `phs004225`; do not imply public IDC/NBIA download.
- For new Data Retriever CSV manifests, route by one preferred header only: `SeriesInstanceUID` for public DICOM, `imageUrl` for PathDB/direct public files, or `drs_uri` for General Commons controlled-access files. Avoid mixed-route manifests because Data Retriever applies header precedence.
- WordPress download metadata may contain nested objects or media IDs. Prefer the snapshot views and `wordpress_downloads` tables for normal tasks; source API helper packages are maintainer/developer tools.
- DataCite relationships are about DOI provenance. They do not automatically make an external Zenodo or IDC record a TCIA-published dataset. WordPress remains the publication/visibility authority after DataCite discovery.
- TCIA Publications EndNote XML is the bibliography authority for manuscripts written about TCIA datasets. DataCite remains the dataset DOI authority.
- Controlled-access metadata can be public even when file access is restricted.
