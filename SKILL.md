---
name: tcia-query-skill
description: Find, verify, cite, visualize, and route TCIA-published datasets across TCIA WordPress Collection and Analysis Result metadata, IDC/idc-index, General Commons, PathDB, DataCite, and Aspera. Use when users ask to discover TCIA datasets by cancer type, modality, body site, species, data type, access/license, DOI, program, clinical/supporting data, segmentation/annotation availability, browser preview/viewer URL, or download path, including public DICOM, controlled-access face datasets, non-DICOM pathology, supporting files, and derived results.
---

# TCIA Query Skill

## Core Rule

Use the TCIA WordPress Collection Manager as the authority for whether a dataset is TCIA-published. A dataset is in scope only if it appears as a WordPress Collection or Analysis Result. Downstream systems such as IDC, General Commons, PathDB, Zenodo, and DataCite can enrich or route access, but they do not decide TCIA provenance.

Ignore WordPress records where `hide_from_browse_table = "1"` by default. TCIA uses this flag for pre-release staging/review and for retired/outdated datasets that should not be casually rediscovered. Include hidden records only when the user explicitly says they are a TCIA staff member and explicitly asks to include hidden, staged, retired, or internal-review datasets.

When a downstream record is derived from a TCIA DOI but is not itself listed in WordPress, describe it as an external derived or related dataset, not as a TCIA-published dataset.

## Quick Workflow

1. Search WordPress first for Collections and Analysis Results.
   - Prefer `scripts/tcia_wordpress_search.py` for lightweight searches.
   - Use terse v2 results for broad discovery, then re-query candidates with `--verbose` when answering from abstracts, detailed descriptions, acknowledgements, methods, download notes, publications, or other long text.
   - Use `--include-hidden` only for explicit TCIA staff requests that ask for hidden/staged/retired records.
   - Or use `tcia_utils.wordpress.getCollections()` and `getAnalyses()` if packages are available.
2. Filter out `hide_from_browse_table = "1"` records unless the explicit TCIA staff exception applies.
3. Filter candidates by the user's criteria: cancer type, body site, modality, species, data type, access/license, DOI, program, supporting data, segmentations/annotations, or download need.
4. Use `collection_short_title` or `result_short_title` as the cross-system key whenever possible.
5. For download questions, inspect WordPress `download_type`, `data_type`, and `file_type` together. These are multi-select labels, not a strict one-parent tree.
6. Route access with the matrix below.
7. Decide open versus controlled access from WordPress license metadata, not from collection/page accessibility fields. Creative Commons licenses mean open access; Creative Commons NonCommercial licenses are open access with a noncommercial-use restriction. If license text indicates NIH Controlled Data Access, TCIA Restricted, or another controlled/restricted license, alert the user that the dataset is not open access and link to the TCIA NIH Controlled Data Access Policy before giving download/API-key/Data Retriever guidance.
8. For visualization questions, decide open versus controlled access first. Controlled-access data cannot be previewed in a browser before download; open-access DICOM should use IDC viewer capabilities.
9. Include citations and access caveats before recommending downloads or downstream analysis.

## Access Routing

| Data need | Route |
| --- | --- |
| Public DICOM radiology, DICOM pathology, or DICOM annotations/results | Use IDC and `idc-index` first. If an IDC skill is available, use it for IDC-specific querying, visualization, and downloading. Keep the TCIA WordPress short title, DOI, or Series Instance UIDs as the allowlist/provenance anchor. Use NBIA only as a fallback when the requested DICOM data cannot be found in IDC/idc-index; if fallback is needed, tell users to use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`. |
| Browser visualization before download | Controlled-access data cannot be previewed before download. For open/public DICOM in IDC, use OHIF v3 for radiology, VolView when a public S3 series folder/CRDC UUID is available, and SliM for SM slide microscopy. For open/public non-DICOM PathDB slides, use caMicroscope with the PathDB CSV `slide_id`. Load `references/visualization.md`. |
| Controlled-access face datasets | If license metadata indicates controlled/restricted access, alert that the dataset is controlled access and point to `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/`. Use General Commons metadata and access guidance. Scope GC queries to `phs004225` and match `study_acronym` to the WordPress short title. Do not promise file download without proper authorization. |
| Controlled-access NCTN trials or Biobank data | If license metadata indicates controlled/restricted access, alert that the dataset is controlled access and point to the TCIA NIH Controlled Data Access Policy. Use WordPress for current metadata and access statements. CTDC support is expected later; do not invent CTDC routing until TCIA data are available there. |
| Non-DICOM pathology | Use PathDB. Prefer the stable cohort-builder CSV for rich slide-level metadata, and match its `collection` field to the WordPress short title. The PathDB API collection list may use `collectionName`. |
| Spreadsheets, ZIP files, supporting files, manifests, and ancillary downloads | Use WordPress download metadata. If a download is an IBM Aspera Faspex package, see `references/aspera.md`. |
| DOI, citation, version, or derived-result relationships | Use WordPress citation fields and DataCite metadata. See `references/datacite-relationships.md`. |

Read `references/routing.md` for detailed routing and answer-format guidance.

## Tool Setup

Ask before installing packages. If the user allows package installation, use:

```bash
python -m pip install --upgrade tcia_utils idc-index
```

Use `tcia_utils` for TCIA-specific metadata and helper APIs:

```python
from tcia_utils import wordpress, datacite, pathdb

collections = wordpress.getCollections(format="df", removeHtml="yes")
analyses = wordpress.getAnalyses(format="df", removeHtml="yes")
downloads = wordpress.getDownloads(format="df", removeHtml="yes")
doi_records = datacite.getDoi()

# Default public-facing filter:
collections = collections[collections["hide_from_browse_table"].astype(str) != "1"]
analyses = analyses[analyses["hide_from_browse_table"].astype(str) != "1"]
```

For long-text evidence, use Collection Manager API v2 verbose mode (`v=1`). The bundled WordPress helper uses v2 by default and supports `--verbose`; use this when a user asks about abstracts, descriptions, methods, usage notes, acknowledgements, download notes, or other narrative fields. Do not rely on terse/default API output for these questions.

For broad WordPress searches, keep requests efficient. The bundled helper parallelizes independent Collection and Analysis Result endpoint calls plus v2 pagination with `--workers 4` by default; use `--workers 1` only when troubleshooting or when a sequential trace is needed. For manual API work, parallelize independent collection, analysis-result, or page requests modestly rather than fetching large result sets one page at a time.

For controlled-access evidence, inspect WordPress download/license metadata. Do not use `collection_page_accessibility` or `result_page_accessibility` to decide controlled access; those fields are being phased out. Creative Commons licenses mean open access. Creative Commons NonCommercial licenses are open access with a noncommercial-use restriction. The bundled WordPress helper emits `license_status`, `licenses`, `controlled_access`, `noncommercial_license`, and `controlled_access_policy` fields. When `controlled_access` is true, link users to `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/`, which contains current access-request, JSON API key, and TCIA Data Retriever configuration guidance.

Use `idc-index` for public DICOM only after confirming that the dataset is TCIA-published through WordPress or is clearly an external derived dataset through DataCite relationships.

For open-access/public DICOM downloads, prefer IDC/idc-index over NBIA. TCIA is phasing out NBIA, so do not use NBIA as the first route for public DICOM files. WordPress `.tcia` manifest files are still useful because they usually contain a few configuration lines followed by one DICOM Series Instance UID per row; extract those Series Instance UIDs and use them with idc-index. Use NBIA only as a fallback when IDC/idc-index cannot find the requested public DICOM series. If NBIA fallback is needed, tell users to use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`. For controlled-access DICOM, use WordPress license metadata plus General Commons under `phs004225`; do not imply IDC or NBIA public download.

For visualization before download, load `references/visualization.md`. Controlled-access data cannot be visualized in a browser before download regardless of file format. For open-access/public DICOM in IDC, use IDC/idc-index viewer support: OHIF v3 for radiology, SliM for DICOM slide microscopy (`SM`), and VolView only after mapping the series to its public S3 folder or `crdc_series_uuid`. For open/public non-DICOM PathDB slides, use caMicroscope viewer URLs built from the PathDB CSV `slide_id`.

## WordPress Download Labels

WordPress download metadata uses three related multi-select fields:

- `download_type`: broad category, such as Radiology Images, Image Annotations, Clinical Data, Pathology Images, or Other.
- `data_type`: modality, annotation, clinical, pathology, or content label, such as CT, MR, RTSTRUCT, SEG, Segmentation, Demographic, Protocol, or Whole Slide Image.
- `file_type`: file format, such as DICOM, CSV, TSV, XLSX, ZIP, NIfTI, SVS, JSON, or PDF.

Treat `download_type` as the broad parent category, but do not require each download to have exactly one parent. TCIA often publishes one download record for a Data Retriever manifest that contains mixed content, such as CT or MR images plus RTSTRUCT or SEG annotations. TCIA may also publish one ZIP or supporting package that legitimately combines categories, such as image annotations plus protocol/acquisition details labeled as Other.

When routing downloads, preserve all labels and explain mixed labels plainly. Do not infer the route from a single `data_type`; use the combined `download_type`, `data_type`, `file_type`, title, description, license, requirements, and WordPress dataset context. Ignore blank or orphaned download rows in normal public discovery unless they are attached to a visible Collection or Analysis Result and contain a real title, URL, or file.

For Collections, `collection_downloads` are the dataset's download records. For Analysis Results, `result_downloads` are the Analysis Result's actual downloadable files. Do not treat source collection download records as the Analysis Result files; source collections only explain what data were used to create the result. If `result_downloads` are missing or empty for an Analysis Result, say that the result file metadata is unavailable and verify with the current WordPress API or page before giving download instructions.

## Bundled Scripts

Run scripts from the skill root.

| Script | Purpose |
| --- | --- |
| `scripts/tcia_wordpress_search.py` | Search live TCIA WordPress Collection and Analysis Result metadata, with text or JSON output. |
| `scripts/tcia_manifest_series_uids.py` | Extract DICOM Series Instance UIDs from a TCIA `.tcia` manifest path or URL for IDC/idc-index lookup. |
| `scripts/idc_viewer_urls.py` | Construct OHIF v3, SliM, or VolView URLs after TCIA provenance, license, and IDC presence are already verified. |
| `scripts/general_commons_studies.py` | Query General Commons GraphQL for TCIA face dataset study acronyms under `phs004225` and optional node counts. |
| `scripts/datacite_related.py` | Find DataCite records that declare DOI relationships, such as Zenodo records derived from TCIA DOIs. |
| `scripts/pathdb_metadata.py` | Search or summarize PathDB non-DICOM histopathology slide metadata and derived caMicroscope viewer URLs from the stable cohort-builder CSV. |

Examples:

```bash
python scripts/tcia_wordpress_search.py --query breast --limit 10
python scripts/tcia_wordpress_search.py --short-title TCGA-BRCA --verbose --json
python scripts/tcia_wordpress_search.py --short-title TCGA-BRCA --json
python scripts/tcia_wordpress_search.py --query lung --workers 6 --limit 10
python scripts/tcia_wordpress_search.py --query retired --include-hidden
python scripts/tcia_manifest_series_uids.py ./manifest.tcia --out series_uids.txt
python scripts/idc_viewer_urls.py ohif-v3 --study-uid <StudyInstanceUID> --series-uid <SeriesInstanceUID>
python scripts/idc_viewer_urls.py slim --study-uid <StudyInstanceUID> --series-uid <SeriesInstanceUID>
python scripts/idc_viewer_urls.py volview --crdc-series-uuid <crdc_series_uuid>
python scripts/general_commons_studies.py --study-acronym TCGA-GBM --counts
python scripts/datacite_related.py 10.7937/TCIA.HMQ8-J677
python scripts/pathdb_metadata.py --collection CPTAC-STAD --summary
```

## General Commons

Use General Commons only for controlled-access TCIA face datasets unless the user explicitly asks for broader GC context. All TCIA data in General Commons are under `phs004225`; child `study_acronym` values should match WordPress `collection_short_title` or `result_short_title`.

Load `references/general-commons-graphql.md` when querying General Commons.

## Controlled Access

Load `references/controlled-access.md` when a user asks about controlled-access datasets, face data with controlled/restricted licenses, NCTN trials, Biobank data, API-key access, or configuring TCIA Data Retriever for restricted downloads. Always point users to the TCIA NIH Controlled Data Access Policy page for current instructions: `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/`.

## IDC DICOM Downloads

Load `references/idc-dicom-downloads.md` when a user asks to download public TCIA DICOM data, including radiology images, DICOM pathology, RTSTRUCT, SEG, SR, radiotherapy objects, or other DICOM annotation/result files. Use IDC/idc-index first. Use NBIA only as a fallback when the requested DICOM series cannot be found in IDC/idc-index, or when the user explicitly asks for NBIA after being told IDC is preferred. If NBIA fallback is needed, use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.

## Visualization

Load `references/visualization.md` when a user asks to preview, visualize, open, inspect, or launch a browser viewer for TCIA data. Never generate public viewer links for controlled-access datasets. For open-access/public DICOM, use IDC/idc-index and the IDC skill when available; prefer OHIF v3 for radiology, SliM for `SM`, and VolView only when a public S3 series URL or CRDC series UUID has been identified.

## DataCite Relationships

Use DataCite to explain DOI provenance and derived-data relationships. For example, an external Zenodo dataset may declare `IsDerivedFrom` a TCIA DOI. Such a record is relevant to the TCIA collection, but it remains an external derived record unless WordPress also lists it as a Collection or Analysis Result.

Load `references/datacite-relationships.md` when answering DOI, citation, version, or derived-result questions.

## Aspera Packages

Some non-DICOM data are distributed through IBM Aspera Faspex package links in WordPress. Do not try to reconstruct package URLs. Use the URL exposed by the TCIA dataset page or WordPress download metadata, then follow `references/aspera.md`.

## PathDB Metadata

For non-DICOM histopathology, load `references/pathdb.md`. Use WordPress first to confirm the dataset is TCIA-published, then use PathDB metadata to answer slide-level questions, including patient counts, slide counts, image URLs, caMicroscope viewer URLs, cancer type/location, data formats, and companion radiology/genomics/proteomics flags.

## Answer Format

For discovery requests, prefer a compact ranked table:

| Dataset | Type | Why it matched | Access route | Access/license | DOI/citation | Notes |
| --- | --- | --- | --- | --- | --- | --- |

Include the TCIA page link, WordPress short title, and any caveats about controlled access, license, or external derived records. For download requests, estimate size/counts when available and recommend a small test download before bulk transfer.

## Guardrails

- Never present a dataset as TCIA-published unless it appears in WordPress Collections or Analysis Results.
- Ignore WordPress `hide_from_browse_table = "1"` records unless the user explicitly identifies as TCIA staff and asks to include hidden/staged/retired/internal-review datasets.
- Use license metadata, not WordPress collection/page accessibility fields, to decide controlled access. Creative Commons means open access; Creative Commons NonCommercial is open with a noncommercial-use restriction; controlled/restricted license text requires the controlled-access alert and policy link.
- Do not use NBIA as the first route for open-access/public DICOM downloads. Prefer IDC/idc-index, using WordPress `.tcia` manifests as Series Instance UID allowlists when helpful. If NBIA fallback is needed, use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`. For controlled-access DICOM, use General Commons metadata and TCIA controlled-access guidance instead of public download routes.
- Do not construct browser viewer URLs for controlled-access data. There is no pre-download browser visualization route for controlled-access TCIA data regardless of file format.
- Do not present VolView as UID-based. Map Study/Series UIDs through IDC/idc-index to a public S3 series folder or `crdc_series_uuid` before constructing a VolView URL.
- Do not broaden IDC, GC, or PathDB searches beyond WordPress short titles, TCIA DOIs, or explicit user-approved exploratory scope.
- Distinguish open Creative Commons, open Creative Commons NonCommercial, mixed open/controlled, and controlled/restricted license statuses clearly.
- Do not provide medical, regulatory, or legal conclusions about data suitability. Report metadata, access terms, and citations.
- Verify current package/API behavior when the user asks for latest status, current availability, or exact download commands.
