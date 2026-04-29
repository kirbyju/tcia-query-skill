# TCIA Routing Reference

## Authority And Keys

Use TCIA WordPress as the authoritative allowlist:

- Collections endpoint: `https://cancerimagingarchive.net/api/v1/collections/`
- Analysis Results endpoint: `https://cancerimagingarchive.net/api/v1/analysis-results/`
- Preferred v2 endpoints for broad search and verbose mode:
  - `https://cancerimagingarchive.net/api/v2/collections`
  - `https://cancerimagingarchive.net/api/v2/analysis-results`

Exclude records where `hide_from_browse_table = "1"` unless the user explicitly says they are a TCIA staff member and asks to include hidden, staged, retired, or internal-review datasets. Hidden records may be pre-release staging pages for submitter review or retired/outdated datasets that TCIA does not want users to accidentally select.

Use v2 verbose mode (`v=1`) when the answer depends on long text such as abstracts, detailed descriptions, acknowledgements, methods, usage notes, download information, publications, or external resources. V2 defaults to terse mode, which truncates fields containing many characters.

Use WordPress license metadata to decide open versus controlled access. Do not use `collection_page_accessibility` or `result_page_accessibility`; those fields are being phased out. Creative Commons licenses mean open access. Creative Commons NonCommercial licenses are open access with a noncommercial-use restriction. If license text indicates NIH Controlled Data Access, TCIA Restricted, or another controlled/restricted license, alert users that the dataset is not open access and link to the TCIA NIH Controlled Data Access Policy before giving access, API-key, or TCIA Data Retriever instructions: `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/`.

Use these WordPress fields as cross-system keys:

- Collections: `collection_short_title`
- Analysis Results: `result_short_title`

Downstream field mappings:

| System | Matching field |
| --- | --- |
| General Commons | `study_acronym`, scoped to `phs004225` |
| PathDB cohort-builder CSV | `collection` |
| PathDB API collection list | `collectionName` |
| DataCite | TCIA DOI and related identifiers |
| IDC | Prefer DOI and collection/analysis metadata from IDC, but keep WordPress as the provenance anchor |

## Discovery Process

1. Query both WordPress Collections and Analysis Results. Use terse v2 output for initial broad search.
2. Normalize title, short title, DOI, data types, cancer types, body locations, species, license text/status, page URL, `hide_from_browse_table`, and summary text.
3. Remove hidden records by default.
4. Filter locally when possible so criteria can match custom fields, not just WordPress full-text search.
5. Re-query matching WordPress candidates with verbose mode before answering from abstracts/descriptions or quoting/paraphrasing narrative fields.
6. Flag controlled access from license metadata only. Creative Commons means open; Creative Commons NonCommercial means open with noncommercial restriction; controlled/restricted license text means controlled access.
7. Enrich only the filtered candidate set through IDC, General Commons, PathDB, or DataCite.
8. If a candidate does not appear in WordPress, exclude it from TCIA-published results. If useful, mention it separately as related or derived.

## Access Route Details

Public DICOM radiology or DICOM pathology:

- Route to IDC and `idc-index`.
- Use IDC-specific tooling for series selection, visualization, licenses, citations, and downloads.
- Avoid duplicating the IDC skill. If available, use it after TCIA provenance is established.

Controlled-access face datasets:

- If license metadata indicates controlled/restricted access, alert users that the dataset is controlled access, not open access.
- Link to `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/` for current request, JSON API key, and TCIA Data Retriever configuration guidance.
- Route to General Commons.
- Scope all GC queries to `phs004225`.
- Match WordPress short title to GC `study_acronym`.
- Do not imply unauthenticated download.

Controlled-access NCTN trials or Biobank data:

- If license metadata indicates controlled/restricted access, alert users that the dataset is controlled access, not open access.
- Link to the TCIA NIH Controlled Data Access Policy for current access-request and API-key guidance.
- Use WordPress license metadata and dataset pages for now.
- CTDC is planned but should not be used until TCIA data and matching fields are confirmed there.

Non-DICOM pathology:

- Route to PathDB.
- Prefer the stable PathDB cohort-builder CSV for rich slide-level metadata.
- Match WordPress short title to CSV `collection`; the PathDB API collection list may use `collectionName`.
- Use `tcia_utils.pathdb` if installed.
- Load `pathdb.md` for the stable CSV URL, columns, and helper script.

Supporting files:

- Use WordPress download metadata and dataset page links.
- For IBM Aspera Faspex package links, see `aspera.md`.

DOI/citation:

- Use WordPress citation fields first.
- Use DataCite to inspect DOI metadata, related identifiers, versions, and external derived records.

## Download Label Interpretation

Use the WordPress downloads endpoint, nested `collection_downloads`, or nested `result_downloads` when answering file/download questions. Interpret these fields together:

| Field | Meaning |
| --- | --- |
| `download_type` | Broad category label, such as Radiology Images, Image Annotations, Clinical Data, Pathology Images, or Other |
| `data_type` | More specific content/modality/metadata label, such as CT, MR, RTSTRUCT, SEG, Segmentation, Demographic, Protocol, Histopathology, or Whole Slide Image |
| `file_type` | Physical or logical file format, such as DICOM, CSV, TSV, XLSX, ZIP, NIfTI, SVS, JSON, or PDF |

For Collections, use `collection_downloads` as the actual dataset download records. For Analysis Results, use `result_downloads` as the actual result file records. Source collection metadata explains provenance only; do not present source collection downloads as if they are the Analysis Result files. If an Analysis Result lacks `result_downloads`, verify against the current WordPress API or page and state that result-file metadata is unavailable instead of substituting `collection_downloads`.

`download_type` is intended as the parent category, but all three fields are multi-select. Mixed parent categories are normal when one download record represents a combined TCIA Data Retriever manifest or package. Examples:

- A single `.tcia` manifest may include source radiology images plus annotation DICOM objects, such as CT/MR with RTSTRUCT or SEG.
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
| Access/license | Open Creative Commons, Open Creative Commons NonCommercial, controlled/restricted, or license-review-needed |
| DOI/citation | Link DOI when present |
| Notes | Include caveats, related external results, size/counts, or next step |

For exact dataset questions, give a short prose summary first, then a table of access routes and citations.

## Common Caveats

- WordPress metadata can contain HTML; strip tags before quoting or matching.
- WordPress `hide_from_browse_table = "1"` means hidden. Treat hidden records as out of scope for public user-facing discovery unless the explicit TCIA staff exception applies.
- WordPress API v2 terse mode can truncate long fields. Use `v=1` or `scripts/tcia_wordpress_search.py --verbose` for full abstracts/descriptions before making content-based claims.
- Controlled-access metadata can be visible even when file downloads require approval. Determine controlled status from license metadata, then link to the TCIA NIH Controlled Data Access Policy for current request, JSON API key, and TCIA Data Retriever configuration steps.
- WordPress download metadata may contain nested objects or media IDs. Prefer the `tcia_utils.wordpress.getDownloads()` helper if package installation is allowed.
- DataCite relationships are about DOI provenance. They do not automatically make an external Zenodo or IDC record a TCIA-published dataset.
- Controlled-access metadata can be public even when file access is restricted.
