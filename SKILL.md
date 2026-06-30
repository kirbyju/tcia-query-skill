---
name: tcia-query-skill
description: Find, verify, cite, visualize, and route TCIA-published datasets and verified manuscripts about TCIA data across TCIA WordPress Collection and Analysis Result metadata, TCIA Publications EndNote XML, IDC/idc-index, Cancer Data Aggregator, General Commons, CTDC, PathDB, DataCite, and Aspera. Use when users ask to discover TCIA datasets or publications by cancer type, modality, body site, species, data type, access/license, DOI, program, clinical/supporting data, demographic/diagnosis enrichment, segmentation/annotation availability, browser preview/viewer URL, or download path, including public DICOM, controlled-access datasets, non-DICOM pathology, supporting files, and derived results.
---

# TCIA Query Skill

## Core Rule

Use the TCIA WordPress Collection Manager as the authority for whether a dataset is TCIA-published. A dataset is in scope only if it appears as a WordPress Collection or Analysis Result. For normal agent work, query the local SQLite snapshot as the fast discovery surface over WordPress, PathDB, and DataCite metadata. Downstream systems such as IDC, CDA, General Commons, CTDC, PathDB, Zenodo, and DataCite can enrich or route access, but they do not decide TCIA provenance.

Use TCIA's Publications page and EndNote export as the authority for peer-reviewed manuscripts written about TCIA datasets: `https://www.cancerimagingarchive.net/publications/` and `https://cancerimagingarchive.net/endnote/Pubs_basedon_TCIA.xml`. DataCite records describe TCIA dataset DOI metadata; they are not the verified bibliography of papers that used TCIA data. For questions about papers, manuscripts, publication impact, hypotheses studied, methods, or citation lists, load `references/publications.md` and use `scripts/tcia_publications.py`.

Exception for DOI-centered questions: start with DataCite for DOI metadata, citation metadata, versions, and DOI relationships, then use WordPress to confirm TCIA publication status, visibility, access/license, and user-facing dataset pages.

Ignore WordPress records where `hide_from_browse_table = "1"` by default. TCIA uses this flag for pre-release staging/review and for retired/outdated datasets that should not be casually rediscovered. Include hidden records only when the user explicitly says they are a TCIA staff member and explicitly asks to include hidden, staged, retired, or internal-review datasets.

When a downstream record is derived from a TCIA DOI but is not itself listed in WordPress, describe it as an external derived or related dataset, not as a TCIA-published dataset.

## Quick Workflow

0. Use the local SQLite metadata snapshot for routine discovery, release-history, and access/license metadata. Prefer the agent-facing views in `references/schema.md`: `agent_datasets`, `agent_current_downloads`, `agent_dataset_access_summary`, `agent_dataset_versions`, `agent_dataset_v1_releases`, `agent_pathdb_slides`, and `agent_datacite_dois`. Load `references/snapshots.md` for refresh behavior and `references/schema.md` for SQL details. If the environment cannot execute scripts or query SQLite, use the web-friendly release exports described in `references/mcp-and-web-llms.md`; do not switch to live WordPress API discovery.
1. Choose the starting source.
   - For peer-reviewed publications or manuscripts about TCIA data, use TCIA's EndNote XML export, not DataCite. Prefer `scripts/tcia_publications.py`, and load `references/publications.md`.
   - For DOI, citation, or version questions, start with DataCite metadata from the snapshot. Prefer `scripts/datacite_tcia_dois.py` for TCIA DOI prefix records.
   - For TCIA dataset release timelines, first-release dates, or version-history questions, use `agent_dataset_versions` and `agent_dataset_v1_releases` from the snapshot. These views are derived from the WordPress `/api/v2/versions` endpoint and match related datasets by exact short title plus a normalized punctuation/case-insensitive key. For v1 timelines, prefer `v1_release_date_source` values from the versions endpoint; treat `current_record_still_v1_date_updated` as a fallback only for datasets that are still version 1.
   - For subject-level clinical, demographic, diagnosis, treatment, or cross-commons data-availability enrichment, confirm the TCIA dataset in WordPress first, then use CDA from validated TCIA/IDC subject identifiers. Load `references/cda.md`.
   - For file-level public NIfTI questions, confirm TCIA provenance/access through the normal snapshot first, then load `references/nifti.md` and use the optional NIfTI SQLite release only if file-grain metadata are needed. Prefer `agent_nifti_dataset_summary`, `agent_nifti_downloads`, `agent_nifti_files`, and `agent_nifti_derived_objects`.
   - For controlled-access file, manifest, `drs_uri`, modality, or series-level metadata, confirm controlled status through the normal snapshot first, then load `references/controlled-access.md` and use the optional controlled-access SQLite release when file-grain public metadata are needed. Prefer `agent_controlled_dataset_summary`, `agent_controlled_downloads`, and `agent_controlled_files`.
   - For all other discovery and access questions, search WordPress snapshot records first for Collections and Analysis Results.
   - Prefer `scripts/tcia_wordpress_search.py` for lightweight snapshot searches.
   - If the snapshot is missing, run or tell the user to run `python scripts/tcia_snapshot.py ensure` from the skill root.
   - If a specific dataset appears absent after refreshing, say the snapshot may not include the newest TCIA metadata yet. The GitHub Action builds release snapshots at 7:17 AM and 7:17 PM America/New_York; ask the user to try again after the next scheduled run has had time to finish, then rerun `python scripts/tcia_snapshot.py ensure`.
   - Use `--include-hidden` only for explicit TCIA staff requests that ask for hidden/staged/retired records.
2. Filter out `hide_from_browse_table = "1"` records unless the explicit TCIA staff exception applies.
3. Filter candidates by the user's criteria: cancer type, body site, modality, species, data type, access/license, DOI, program, supporting data, segmentations/annotations, or download need. For modality, file format, download-route, and access/license questions, prefer download-level metadata over top-level Collection or Analysis Result `data_types`; mixed datasets can have modality labels such as MR only on individual download records. For public DICOM series/file details after TCIA provenance and access are established, use IDC/idc-index rather than querying live WordPress.
4. Use `collection_short_title` or `result_short_title` as the cross-system key whenever possible.
5. For download questions, inspect `agent_current_downloads` plus WordPress `download_type`, `data_type`, and `file_type` together. These are multi-select labels, not a strict one-parent tree.
6. Route access with the matrix below.
7. Decide open versus controlled access from WordPress license metadata, not from collection/page accessibility fields. Creative Commons licenses mean open access; Creative Commons NonCommercial licenses are open access with a noncommercial-use restriction. If license text indicates NIH Controlled Data Access, TCIA Restricted, or another controlled/restricted license, alert the user that the dataset is not open access and link to the TCIA NIH Controlled Data Access Policy before giving download/API-key/Data Retriever guidance.
8. If `agent_dataset_access_summary.resolved_access_level = 'mixed'`, split open/noncontrolled downloads from controlled downloads. Do not imply that all files in a mixed dataset are open.
9. For visualization questions, decide open versus controlled access first. Controlled-access data cannot be previewed in a browser before download; open-access data should be returned as clickable viewer links, not opened by the agent.
10. For open-access download requests, ask whether the user wants the agent to download files directly in the current environment or create a portable TCIA Data Retriever CSV manifest when the requested data can be represented by DICOM Series Instance UIDs, direct `imageUrl` values, or `drs_uri` values. Do not directly download controlled data; for controlled data, provide the TCIA policy link and portable manifest guidance only.
11. Include citations and access caveats before recommending downloads or downstream analysis.

## Access Routing

| Data need | Route |
| --- | --- |
| Public DICOM radiology, DICOM pathology, or DICOM annotations/results | Use IDC and `idc-index` first. If an IDC skill is available, use it for IDC-specific querying, visualization, and downloading. Keep the TCIA WordPress short title, DOI, or Series Instance UIDs as the allowlist/provenance anchor. Use NBIA only as a fallback when the requested DICOM data cannot be found in IDC/idc-index; if fallback is needed, tell users to use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`. |
| Public non-DICOM NIfTI file-grain metadata | Use WordPress snapshot metadata first to confirm the dataset/download is visible and non-controlled, then use `references/nifti.md` and `scripts/tcia_nifti_metadata.py`. The NIfTI SQLite is optional and downloaded on demand with `python scripts/tcia_nifti_metadata.py ensure`; it is not installed or refreshed automatically with the base skill snapshot. Prefer the optional SQLite `agent_nifti_*` views for direct SQL. |
| Browser visualization before download | Controlled-access data cannot be previewed before download. For open/public DICOM in IDC, use OHIF v3 for radiology, VolView when a public S3 series folder/CRDC UUID is available, and SliM for SM slide microscopy. For open/public non-DICOM PathDB slides, use caMicroscope with the PathDB CSV `camic_id`. Load `references/visualization.md`. |
| Controlled-access face datasets | If license metadata indicates controlled/restricted access, alert that the dataset is controlled access and point to `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/`. For Biobank controlled-access face data, use the current WordPress dataset pages/download metadata and optional controlled-access SQLite for CTDC manifest, `drs_uri`, metadata, download, and viewer links; users must request access to dbGaP study `phs002192`. For non-Biobank face datasets, use General Commons only when WordPress or GC metadata indicate that route; scope GC queries to `phs004225` and match `study_acronym` to the WordPress short title. Do not directly download controlled data; provide policy and TCIA Data Retriever manifest guidance for later authorized use. |
| Controlled-access NCTN trials or Biobank data | If license metadata indicates controlled/restricted access, alert that the dataset is controlled access and point to the TCIA NIH Controlled Data Access Policy. For Biobank controlled-access face data, route through CTDC using the manifests and download/view links now exposed on the relevant WordPress pages, and use the optional controlled-access SQLite when file-grain public metadata are needed; tell users they must request dbGaP access to `phs002192`. For NCTN trials or other controlled datasets, use WordPress for current metadata and access statements unless WordPress identifies a downstream route. |
| Subject-level clinical/demographic/diagnosis enrichment or cross-commons availability | Use CDA after WordPress provenance is established. Prefer `cdapython` for harmonized subject and file summaries across IDC, GDC, PDC, GC, ICDC, and related upstream identifiers. Use CDA to enrich TCIA/IDC cohorts, not to decide TCIA publication, replace official WordPress downloads, or authorize controlled data access. Load `references/cda.md`. |
| Non-DICOM pathology | Use PathDB. Prefer the stable cohort-builder CSV for rich slide-level metadata, and match its `collection` field to the WordPress short title. The PathDB API collection list may use `collectionName`. |
| Spreadsheets, ZIP files, supporting files, manifests, and ancillary downloads | Use WordPress download metadata. If a download is an IBM Aspera Faspex package, see `references/aspera.md`. |
| Peer-reviewed manuscripts about TCIA datasets | Use TCIA Publications and the EndNote XML export. Search title, abstract, keywords, journal, PMID, manuscript DOI, and linked TCIA dataset DOIs. Load `references/publications.md`. |
| DOI, citation, version, or derived-result relationships | Start with DataCite metadata and relationships, then use WordPress for TCIA publication/visibility, access/license, and user-facing pages. See `references/datacite-relationships.md`. |
| TCIA release timeline or first-release date | Use `agent_dataset_versions` and `agent_dataset_v1_releases` from the snapshot. These come from the WordPress versions endpoint and preserve exact/normalized match provenance. |

Read `references/routing.md` for detailed routing and answer-format guidance.

## Tool Setup

Do not assume optional Python packages are installed. Before writing custom code for TCIA, IDC, DICOM, or CDA operations, check whether the appropriate package is available in the same Python environment that will run the task:

```bash
python -c "import importlib.util as u; print({p: u.find_spec(p) is not None for p in ['tcia_utils','idc_index','pydicom','cdapython']})"
```

Ask before installing packages. If the user allows package installation, install them in the active local agent environment:

```bash
python -m pip install --upgrade tcia_utils idc-index pydicom cdapython
```

Prefer these package APIs over custom implementations where possible:

- `tcia_utils`: TCIA-specific helper APIs when maintaining the snapshot or doing explicit source-system checks.
- `idc-index`: IDC metadata lookup, public DICOM download, viewer URLs, cloud-storage URLs, and Series Instance UID workflows.
- `pydicom`: local DICOM header/metadata inspection. Do not hand-parse DICOM files when `pydicom` can be installed or is already available.
- `cdapython`: CDA subject/file summaries and harmonized cross-CRDC enrichment. Prefer it over direct `cda-client` or handwritten CDA REST calls.

The bundled standard-library scripts support snapshot refresh/querying, Data Retriever CSV creation, legacy manifest parsing, and viewer URL construction. They do not replace `idc-index` for IDC workflows, `pydicom` for local DICOM parsing, or `cdapython` for CDA workflows. Live source API details belong to `scripts/tcia_snapshot.py build` and the maintainer guidance in `references/snapshots.md`, not normal end-user discovery.

For controlled-access evidence, inspect WordPress download/license metadata through `agent_datasets`, `agent_current_downloads`, and `agent_dataset_access_summary`. When file-grain controlled metadata are needed, use the optional controlled-access SQLite release from `scripts/tcia_controlled_access_metadata.py ensure`; it is derived from public WordPress manifest and metadata spreadsheet URLs and does not require a user account. Do not use `collection_page_accessibility` or `result_page_accessibility` to decide controlled access; those fields are being phased out. Creative Commons licenses mean open access. Creative Commons NonCommercial licenses are open access with a noncommercial-use restriction. When `controlled_access` is true or `resolved_access_level` is `controlled` or `mixed`, link users to `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/`, which contains current access-request, JSON API key, and TCIA Data Retriever configuration guidance. Do not directly download controlled data.

Use `idc-index` for public DICOM only after confirming that the dataset is TCIA-published through WordPress or is clearly an external derived dataset through DataCite relationships.

For open-access/public DICOM downloads, prefer IDC/idc-index over NBIA. TCIA is phasing out NBIA, so do not use NBIA as the first route for public DICOM files. Existing WordPress `.tcia` manifest files are still useful as legacy inputs because they usually contain a few configuration lines followed by one DICOM Series Instance UID per row; extract those Series Instance UIDs and use them with idc-index. For new portable manifests, write CSV files for TCIA Data Retriever instead of `.tcia` files. Use NBIA only as a fallback when IDC/idc-index cannot find the requested public DICOM series. If NBIA fallback is needed, tell users to use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`. For controlled-access DICOM, use WordPress license metadata plus the downstream route identified by current WordPress metadata: CTDC for Biobank controlled-access face data, or General Commons under `phs004225` only when WordPress/GC metadata indicate that route. Do not imply IDC or NBIA public download and do not directly download controlled data.

Do not query live WordPress to find DICOM series, modality, annotation, or file details during end-user tasks. WordPress snapshot metadata identifies TCIA publication status, user-facing downloads, and access/license terms; IDC/idc-index is the preferred source for public DICOM series/file metadata after those checks.

For visualization before download, load `references/visualization.md`. Controlled-access data cannot be visualized in a browser before download regardless of file format. For open-access/public DICOM in IDC, use IDC/idc-index viewer support: OHIF v3 for radiology, SliM for DICOM slide microscopy (`SM`), and VolView only after mapping the series to its public S3 folder or `crdc_series_uuid`. For open/public non-DICOM PathDB slides, use caMicroscope viewer URLs built from the PathDB CSV `camic_id`; the viewer URL parameter is named `slideId`, but it expects the numeric `camic_id`, not the CSV `slide_id` or `patient_id`. Return these as links for the user to open in their regular browser; do not install Playwright or browser automation just to preview example data.

Before performing any data download, ask how the user wants to proceed:

- Direct agent download: use Python tooling such as `idc-index` in the current environment when the user wants the agent to download files locally.
- Portable manifest: create or return a CSV manifest for TCIA Data Retriever when the data can be represented by DICOM Series Instance UIDs, direct `imageUrl` values, or `drs_uri` values. This lets the user save the manifest, use it on another computer, or share it with a collaborator.

If the user chooses a manifest and WordPress already provides a current CSV/TSV/XLSX manifest link, prefer the official WordPress manifest. Treat `.tcia` as a legacy NBIA-era format: read existing `.tcia` files when needed, but do not write new `.tcia` files unless the user explicitly asks for the legacy format. If the agent has validated Series Instance UIDs, image URLs, or DRS URIs and needs to create a manifest locally, use `scripts/tcia_create_data_retriever_csv.py`.

TCIA Data Retriever routes spreadsheet manifests by column headers:

- `SeriesInstanceUID` is the preferred header for public DICOM Series Instance UID manifests; `Series UID` is also recognized. Data Retriever treats this as DICOM and attempts IDC/S3 lookup first, with TCIA/NBIA v4 as backup for public data that is not found in IDC. Do not use this route for controlled-access DICOM; use General Commons guidance instead.
- `imageUrl` is the preferred header for direct public file URLs; `wsiimage_url` is also recognized. In TCIA workflows, use this for public PathDB/non-DICOM pathology files.
- `drs_uri` is the preferred header for controlled-access files when official WordPress, CTDC, or General Commons manifests provide DRS URIs; `File ID`/`file_id` are also recognized and bare IDs are interpreted as `drs://nci-crdc.datacommons.io/<file-id>`. Always warn that authorization and TCIA Data Retriever API-key configuration may be required.

For new CSV manifests, create a single-route file with exactly one of the preferred route headers: `SeriesInstanceUID`, `imageUrl`, or `drs_uri`. Do not mix route headers in one manifest. Data Retriever checks for Series UID columns first, and in direct-file spreadsheets `drs_uri`/`File ID` takes precedence over `imageUrl`, so mixed-route CSVs can be ambiguous. For DICOM UID manifests, prefer a simple one-column CSV headed `SeriesInstanceUID`.

## WordPress Download Labels

WordPress download metadata uses three related multi-select fields:

- `download_type`: broad category, such as Radiology Images, Image Annotations, Clinical Data, Pathology Images, or Other.
- `data_type`: modality, annotation, clinical, pathology, or content label, such as CT, MR, RTSTRUCT, SEG, Segmentation, Demographic, Protocol, or Whole Slide Image.
- `file_type`: file format, such as DICOM, CSV, TSV, XLSX, ZIP, NIfTI, SVS, JSON, or PDF.

Treat `download_type` as the broad parent category, but do not require each download to have exactly one parent. TCIA often publishes one download record for a Data Retriever manifest that contains mixed content, such as CT or MR images plus RTSTRUCT or SEG annotations. TCIA may also publish one ZIP or supporting package that legitimately combines categories, such as image annotations plus protocol/acquisition details labeled as Other.

When routing downloads, preserve all labels and explain mixed labels plainly. Do not infer the route from a single `data_type`; use the combined `download_type`, `data_type`, `file_type`, title, description, license, requirements, and WordPress dataset context. Ignore blank or orphaned download rows in normal public discovery unless they are attached to a visible Collection or Analysis Result and contain a real title, URL, or file.

For Collections, `collection_downloads` are the dataset's download records. For Analysis Results, `result_downloads` are the Analysis Result's actual downloadable files. Do not treat source collection download records as the Analysis Result files; source collections only explain what data were used to create the result. If `result_downloads` are missing or empty for an Analysis Result, say that the result file metadata is unavailable in the current snapshot and, if the user expects a recent change, ask them to try again after the next scheduled snapshot run has completed.

In local SQLite snapshots, current-version nested `collection_downloads` and `result_downloads` are normalized into `wordpress_downloads` and `wordpress_download_labels`. Use the boolean `is_current_version` column for user-facing dataset downloads, for example `is_current_version IS TRUE`. The global WordPress downloads endpoint is also stored there with `is_current_version = FALSE`; use those rows for troubleshooting, not routine discovery, because they can include historical or orphaned endpoint records.

## Bundled Scripts

Run scripts from the skill root.

| Script | Purpose |
| --- | --- |
| `scripts/tcia_wordpress_search.py` | Search local SQLite TCIA WordPress Collection and Analysis Result snapshot metadata, with text or JSON output. |
| `scripts/tcia_snapshot.py` | Build, inspect, validate, or download the local SQLite metadata snapshot, and export web-friendly release files. |
| `scripts/tcia_nifti_metadata.py` | Download, validate, summarize, and query the optional release SQLite for visible non-controlled TCIA NIfTI file-grain metadata. |
| `scripts/tcia_controlled_access_metadata.py` | Build/download, validate, summarize, and query the optional release SQLite for public controlled-access WordPress manifests, metadata spreadsheets, `drs_uri` rows, and IDC-shaped radiology indexes. |
| `scripts/tcia_manifest_series_uids.py` | Extract DICOM Series Instance UIDs from a legacy TCIA `.tcia` manifest path or URL for IDC/idc-index lookup. |
| `scripts/tcia_create_data_retriever_csv.py` | Create a TCIA Data Retriever CSV manifest from validated DICOM Series Instance UIDs, direct `imageUrl` values, or `drs_uri` values. |
| `scripts/idc_viewer_urls.py` | Construct OHIF v3, SliM, or VolView URLs after TCIA provenance, license, and IDC presence are already verified. |
| `scripts/general_commons_studies.py` | Query General Commons GraphQL for TCIA face dataset study acronyms under `phs004225` and optional node counts. |
| `scripts/datacite_tcia_dois.py` | List TCIA DOI metadata from DataCite using prefix `10.7937`, or fetch one exact DOI. |
| `scripts/datacite_related.py` | Developer/explicit-current-check helper for external DataCite records that declare DOI relationships, such as Zenodo records derived from TCIA DOIs. |
| `scripts/tcia_publications.py` | Fetch, parse, and search TCIA's verified EndNote publication library for papers written about TCIA datasets. |
| `scripts/pathdb_metadata.py` | Search or summarize PathDB non-DICOM histopathology slide metadata and derived caMicroscope viewer URLs from the stable cohort-builder CSV. |

Examples:

```bash
python scripts/tcia_snapshot.py ensure
python scripts/tcia_snapshot.py info
python scripts/tcia_snapshot.py build --out cache/tcia_snapshot.sqlite --gzip-out dist/tcia_snapshot.sqlite.gz --manifest-out dist/tcia_snapshot_manifest.json --exports-dir dist
python scripts/tcia_snapshot.py validate --db cache/tcia_snapshot.sqlite
python scripts/tcia_nifti_metadata.py ensure
python scripts/tcia_nifti_metadata.py datasets --limit 20
python scripts/tcia_nifti_metadata.py derived --collection BCBM-RadioGenomics --with-sources
python scripts/tcia_controlled_access_metadata.py ensure
python scripts/tcia_controlled_access_metadata.py datasets --limit 20
python scripts/tcia_controlled_access_metadata.py files --collection CMB-MEL --limit 10
python scripts/tcia_wordpress_search.py --query breast --limit 10
python scripts/tcia_wordpress_search.py --short-title TCGA-BRCA --json
python scripts/tcia_wordpress_search.py --query retired --include-hidden
python scripts/tcia_manifest_series_uids.py ./legacy_manifest.tcia --out series_uids.txt
python scripts/tcia_create_data_retriever_csv.py --uids-file series_uids.txt --out manifest.csv
python scripts/idc_viewer_urls.py ohif-v3 --study-uid <StudyInstanceUID> --series-uid <SeriesInstanceUID>
python scripts/idc_viewer_urls.py slim --study-uid <StudyInstanceUID> --series-uid <SeriesInstanceUID>
python scripts/idc_viewer_urls.py volview --crdc-series-uuid <crdc_series_uuid>
python scripts/general_commons_studies.py --study-acronym TCGA-GBM --counts
python scripts/datacite_tcia_dois.py --query breast --limit 10
python scripts/datacite_tcia_dois.py --doi 10.7937/4qad-4280 --json
python scripts/tcia_publications.py --query radiogenomics --limit 10
python scripts/tcia_publications.py --dataset-doi 10.7937/K9/TCIA.2016.RNYFUYE9 --json
python scripts/pathdb_metadata.py --collection CPTAC-STAD --summary
```

## General Commons

Use direct General Commons GraphQL only when WordPress or GC metadata route a controlled-access TCIA face dataset to General Commons, unless the user explicitly asks for broader GC context. All TCIA data in General Commons are under `phs004225`; child `study_acronym` values should match WordPress `collection_short_title` or `result_short_title`. Biobank controlled-access face data now route through CTDC from the current WordPress manifests/links and require dbGaP access to `phs002192`, not GC `phs004225`.

Load `references/general-commons-graphql.md` when querying General Commons.

## Cancer Data Aggregator

Use CDA when a user asks whether TCIA/IDC subjects have additional harmonized clinical, demographic, diagnosis, treatment, genomic, proteomic, General Commons, or cross-CRDC file metadata. Load `references/cda.md`.

Use CDA after WordPress confirms TCIA publication and after subject identifiers are validated through TCIA/IDC metadata. CDA can enrich a cohort, summarize which subjects have IDC/GDC/PDC/GC/ICDC data, and expose upstream identifiers, but WordPress remains the TCIA publication/download authority and source systems remain the access authorities.

## Controlled Access

Load `references/controlled-access.md` when a user asks about controlled-access datasets, face data with controlled/restricted licenses, NCTN trials, Biobank data, API-key access, or configuring TCIA Data Retriever for restricted downloads. Always point users to the TCIA NIH Controlled Data Access Policy page for current instructions: `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/`. For Biobank controlled-access face data, tell users to request dbGaP access to `phs002192` and use the current WordPress CTDC manifests/download/view links after authorization.

Use the optional controlled-access SQLite only after the normal snapshot confirms TCIA provenance and controlled/restricted access. It is distributed as release assets `controlled_access_metadata.sqlite.gz` and `controlled_access_metadata_manifest.json`, and fetched on demand by `python scripts/tcia_controlled_access_metadata.py ensure`.

## IDC DICOM Downloads

Load `references/idc-dicom-downloads.md` when a user asks to download public TCIA DICOM data, including radiology images, DICOM pathology, RTSTRUCT, SEG, SR, radiotherapy objects, or other DICOM annotation/result files. Use IDC/idc-index first. Use NBIA only as a fallback when the requested DICOM series cannot be found in IDC/idc-index, or when the user explicitly asks for NBIA after being told IDC is preferred. If NBIA fallback is needed, use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.

## Visualization

Load `references/visualization.md` when a user asks to preview, visualize, open, inspect, or get a browser viewer link for TCIA data. Never generate public viewer links for controlled-access datasets. For open-access/public DICOM, use IDC/idc-index and the IDC skill when available; prefer OHIF v3 for radiology, SliM for `SM`, and VolView only when a public S3 series URL or CRDC series UUID has been identified. Return viewer URLs as links for the user to open; do not install Playwright or browser automation just to display examples.

## DataCite Relationships

Use snapshot DataCite records first for DOI metadata, citations, and versions. For explicit current external DOI relationship checks, an external Zenodo dataset may declare `IsDerivedFrom` a TCIA DOI. Such a record is relevant to the TCIA collection, but it remains an external derived record unless WordPress also lists it as a Collection or Analysis Result.

Load `references/datacite-relationships.md` when answering DOI, citation, version, or derived-result questions.

## Publications About TCIA Data

Load `references/publications.md` when a user asks for peer-reviewed manuscripts, papers, publication lists, citation counts, research hypotheses, methods, or downstream scientific uses of TCIA data. Use TCIA's verified Publications EndNote XML export at `https://cancerimagingarchive.net/endnote/Pubs_basedon_TCIA.xml` as the authority. Do not substitute DataCite dataset DOI records or WordPress dataset pages for the manuscript bibliography.

## Web LLMs And MCP

Load `references/mcp-and-web-llms.md` when a user asks how to use this skill from web-based LLMs, remote MCP connectors, custom agents that cannot install skills, or environments that cannot run Python/SQLite locally. Prefer the published release snapshot and static JSON/JSONL exports over live public APIs. A remote MCP server should expose typed read-only TCIA search tools backed by the same snapshot, not arbitrary live WordPress scraping.

## Aspera Packages

Some non-DICOM data are distributed through IBM Aspera Faspex package links in WordPress. Do not try to reconstruct package URLs. Use the URL exposed by the TCIA dataset page or WordPress download metadata, then follow `references/aspera.md`. For non-DICOM pathology that is available both through Aspera and PathDB, explain that Aspera packages are the original submitter-provided data, while PathDB copies may be converted or reformatted so they work properly in TCIA's browser-based pathology viewer. Recommend the Aspera copy for real analyses when the user needs the exact files submitted to TCIA.

## NIfTI Metadata

Load `references/nifti.md` when a user asks for TCIA NIfTI file counts, NIfTI filenames/package paths, NIfTI modality/acquisition metadata, or NIfTI segmentation/source-image relationships. Use the normal snapshot first for TCIA provenance, visibility, access/license, and user-facing download URLs. Use the optional NIfTI SQLite only for file-grain public non-controlled NIfTI metadata.

Do not download the optional NIfTI SQLite unless the user needs NIfTI file-grain metadata or explicitly asks to refresh it. It is distributed as release assets `nifti_metadata.sqlite.gz` and `nifti_metadata_manifest.json`, and fetched on demand by `python scripts/tcia_nifti_metadata.py ensure`.

## PathDB Metadata

For non-DICOM histopathology, load `references/pathdb.md`. Use WordPress first to confirm the dataset is TCIA-published, then use PathDB metadata to answer slide-level questions, including patient counts, slide counts, image URLs, caMicroscope viewer URLs, cancer type/location, data formats, and companion radiology/genomics/proteomics flags. For public pathology Aspera package scope, Aspera-derived package file inventory, or PathDB/package reconciliation, use the optional pathology SQLite and prefer `agent_pathology_dataset_summary`, `agent_pathology_downloads`, `agent_pathology_file_objects`, and `agent_pathology_package_files`. For caMicroscope URLs, use CSV `camic_id`, not `slide_id`. If the same pathology data are also distributed as an Aspera package, distinguish the routes: PathDB is optimized for metadata and browser viewing and may use converted or reformatted files; Aspera is the original submitter-provided copy and is preferred for analyses requiring exact source files.

## Answer Format

For discovery requests, prefer a compact ranked table:

| Dataset | Type | Why it matched | Access route | Access/license | DOI/citation | Notes |
| --- | --- | --- | --- | --- | --- | --- |

Include the TCIA page link, WordPress short title, and any caveats about controlled access, license, or external derived records. For download requests, estimate size/counts when available and recommend a small test download before bulk transfer.

## Guardrails

- Never present a dataset as TCIA-published unless it appears in WordPress Collections or Analysis Results.
- Ignore WordPress `hide_from_browse_table = "1"` records unless the user explicitly identifies as TCIA staff and asks to include hidden/staged/retired/internal-review datasets.
- Do not use live WordPress API calls for normal end-user discovery. Use the SQLite snapshot, its release exports, or a snapshot-backed MCP server. Live source APIs are for snapshot maintainers and explicit current-source troubleshooting.
- Do not use DataCite DOI records as the authority for peer-reviewed manuscripts written about TCIA data. Use TCIA's Publications EndNote XML export for manuscript bibliography questions.
- Use license metadata, not WordPress collection/page accessibility fields, to decide controlled access. Creative Commons means open access; Creative Commons NonCommercial is open with a noncommercial-use restriction; controlled/restricted license text requires the controlled-access alert and policy link.
- Do not use NBIA as the first route for open-access/public DICOM downloads. Prefer IDC/idc-index, using existing WordPress `.tcia` manifests as Series Instance UID allowlists when helpful. If NBIA fallback is needed, use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`. For controlled-access DICOM, use WordPress license metadata plus the downstream route identified by current WordPress metadata, such as CTDC for Biobank controlled-access face data or General Commons for non-Biobank face data routed there, instead of public download routes.
- Do not construct browser viewer URLs for controlled-access data. There is no pre-download browser visualization route for controlled-access TCIA data regardless of file format.
- Do not install Playwright or other browser automation just to show visualization examples. Provide viewer links for users to open in their own browser unless they explicitly ask for browser automation.
- Before downloading data, ask whether the user wants direct agent download in the active environment or a portable manifest/file list when the requested data support that workflow. For new TCIA Data Retriever manifests, write CSV/TSV/XLSX-compatible files rather than legacy `.tcia` files.
- For new TCIA Data Retriever CSV manifests, use one route header only: `SeriesInstanceUID` for public DICOM through IDC first/NBIA fallback, `imageUrl` for PathDB/direct public files, or `drs_uri` for controlled-access files when official WordPress, CTDC, or General Commons manifests provide DRS URIs.
- For non-DICOM pathology available in both PathDB and Aspera, do not imply the files are necessarily byte-identical. PathDB may convert or reformat slides for browser-based viewing; Aspera packages should be described as the original submitter-provided data and preferred for analyses that require exact source files.
- For Biobank controlled-access face data, route through CTDC using the current WordPress manifests/download/view links and tell users to request dbGaP study `phs002192`; treat this as current routing, not a future placeholder. Use the optional controlled-access SQLite for public CTDC manifest/spreadsheet metadata when file-grain details are needed.
- Do not present VolView as UID-based. Map Study/Series UIDs through IDC/idc-index to a public S3 series folder or `crdc_series_uuid` before constructing a VolView URL.
- Do not broaden IDC, CDA, GC, or PathDB searches beyond WordPress short titles, TCIA DOIs, subject identifiers from validated TCIA/IDC cohorts, or explicit user-approved exploratory scope.
- Do not use CDA to claim TCIA publication, official TCIA clinical spreadsheet completeness, or controlled-data access rights. Use CDA as harmonized discovery/enrichment metadata and route users back to TCIA, IDC, GDC, PDC, GC, or other source systems for authoritative files and access controls.
- Distinguish open Creative Commons, open Creative Commons NonCommercial, mixed open/controlled, and controlled/restricted license statuses clearly.
- Do not provide medical, regulatory, or legal conclusions about data suitability. Report metadata, access terms, and citations.
- Verify current package/API behavior when the user asks for latest status, current availability, or exact download commands.
