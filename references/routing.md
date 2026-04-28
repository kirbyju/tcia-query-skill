# TCIA Routing Reference

## Authority And Keys

Use TCIA WordPress as the authoritative allowlist:

- Collections endpoint: `https://cancerimagingarchive.net/api/v1/collections/`
- Analysis Results endpoint: `https://cancerimagingarchive.net/api/v1/analysis-results/`

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

1. Query both WordPress Collections and Analysis Results.
2. Normalize title, short title, DOI, data types, cancer types, body locations, species, access status, page URL, and summary text.
3. Filter locally when possible so criteria can match custom fields, not just WordPress full-text search.
4. Enrich only the filtered candidate set through IDC, General Commons, PathDB, or DataCite.
5. If a candidate does not appear in WordPress, exclude it from TCIA-published results. If useful, mention it separately as related or derived.

## Access Route Details

Public DICOM radiology or DICOM pathology:

- Route to IDC and `idc-index`.
- Use IDC-specific tooling for series selection, visualization, licenses, citations, and downloads.
- Avoid duplicating the IDC skill. If available, use it after TCIA provenance is established.

Limited-access face datasets:

- Route to General Commons.
- Scope all GC queries to `phs004225`.
- Match WordPress short title to GC `study_acronym`.
- Describe dbGaP/DAC authorization and SB-CGC access. Do not imply unauthenticated download.

Limited-access NCTN trials or Biobank data:

- Use WordPress access information and dataset pages for now.
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

## Recommended Response Fields

For search/discovery:

| Field | Notes |
| --- | --- |
| Dataset | Use WordPress title and short title |
| Type | Collection or Analysis Result |
| Match reason | Cite the matching cancer type, modality, data type, body site, DOI, etc. |
| Access route | IDC, General Commons, PathDB, WordPress downloads, Aspera, or DataCite |
| Access/license | Public, limited, controlled, license text when known |
| DOI/citation | Link DOI when present |
| Notes | Include caveats, related external results, size/counts, or next step |

For exact dataset questions, give a short prose summary first, then a table of access routes and citations.

## Common Caveats

- WordPress metadata can contain HTML; strip tags before quoting or matching.
- WordPress download metadata may contain nested objects or media IDs. Prefer the `tcia_utils.wordpress.getDownloads()` helper if package installation is allowed.
- DataCite relationships are about DOI provenance. They do not automatically make an external Zenodo or IDC record a TCIA-published dataset.
- Controlled-access metadata can be public even when file access is restricted.
