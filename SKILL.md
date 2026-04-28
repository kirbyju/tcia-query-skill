---
name: tcia-query-skill
description: Find, verify, cite, and route TCIA-published datasets across TCIA WordPress Collection and Analysis Result metadata, IDC/idc-index, General Commons, PathDB, DataCite, and Aspera. Use when users ask to discover TCIA datasets by cancer type, modality, body site, species, data type, access/license, DOI, program, clinical/supporting data, segmentation/annotation availability, or download path, including public DICOM, controlled-access face datasets, non-DICOM pathology, supporting files, and derived results.
---

# TCIA Query Skill

## Core Rule

Use the TCIA WordPress Collection Manager as the authority for whether a dataset is TCIA-published. A dataset is in scope only if it appears as a WordPress Collection or Analysis Result. Downstream systems such as IDC, General Commons, PathDB, Zenodo, and DataCite can enrich or route access, but they do not decide TCIA provenance.

When a downstream record is derived from a TCIA DOI but is not itself listed in WordPress, describe it as an external derived or related dataset, not as a TCIA-published dataset.

## Quick Workflow

1. Search WordPress first for Collections and Analysis Results.
   - Prefer `scripts/tcia_wordpress_search.py` for lightweight searches.
   - Or use `tcia_utils.wordpress.getCollections()` and `getAnalyses()` if packages are available.
2. Filter candidates by the user's criteria: cancer type, body site, modality, species, data type, access/license, DOI, program, supporting data, segmentations/annotations, or download need.
3. Use `collection_short_title` or `result_short_title` as the cross-system key whenever possible.
4. Route access with the matrix below.
5. Include citations and access caveats before recommending downloads or downstream analysis.

## Access Routing

| Data need | Route |
| --- | --- |
| Public DICOM radiology or DICOM pathology | Use IDC and `idc-index`. If an IDC skill is available, use it for IDC-specific querying, visualization, and downloading. Keep the TCIA WordPress short title or DOI as the allowlist/provenance anchor. |
| Limited-access face datasets | Use General Commons metadata and access guidance. Scope GC queries to `phs004225` and match `study_acronym` to the WordPress short title. Do not promise file download without proper dbGaP/DAC authorization. |
| Limited-access NCTN trials or Biobank data | Use WordPress for current metadata and access statements. CTDC support is expected later; do not invent CTDC routing until TCIA data are available there. |
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
```

Use `idc-index` for public DICOM only after confirming that the dataset is TCIA-published through WordPress or is clearly an external derived dataset through DataCite relationships.

## Bundled Scripts

Run scripts from the skill root.

| Script | Purpose |
| --- | --- |
| `scripts/tcia_wordpress_search.py` | Search live TCIA WordPress Collection and Analysis Result metadata, with text or JSON output. |
| `scripts/general_commons_studies.py` | Query General Commons GraphQL for TCIA face dataset study acronyms under `phs004225` and optional node counts. |
| `scripts/datacite_related.py` | Find DataCite records that declare DOI relationships, such as Zenodo records derived from TCIA DOIs. |
| `scripts/pathdb_metadata.py` | Search or summarize PathDB non-DICOM histopathology slide metadata from the stable cohort-builder CSV. |

Examples:

```bash
python scripts/tcia_wordpress_search.py --query breast --limit 10
python scripts/tcia_wordpress_search.py --short-title TCGA-BRCA --json
python scripts/general_commons_studies.py --study-acronym TCGA-GBM --counts
python scripts/datacite_related.py 10.7937/TCIA.HMQ8-J677
python scripts/pathdb_metadata.py --collection CPTAC-STAD --summary
```

## General Commons

Use General Commons only for controlled-access TCIA face datasets unless the user explicitly asks for broader GC context. All TCIA data in General Commons are under `phs004225`; child `study_acronym` values should match WordPress `collection_short_title` or `result_short_title`.

Load `references/general-commons-graphql.md` when querying General Commons.

## DataCite Relationships

Use DataCite to explain DOI provenance and derived-data relationships. For example, an external Zenodo dataset may declare `IsDerivedFrom` a TCIA DOI. Such a record is relevant to the TCIA collection, but it remains an external derived record unless WordPress also lists it as a Collection or Analysis Result.

Load `references/datacite-relationships.md` when answering DOI, citation, version, or derived-result questions.

## Aspera Packages

Some non-DICOM data are distributed through IBM Aspera Faspex package links in WordPress. Do not try to reconstruct package URLs. Use the URL exposed by the TCIA dataset page or WordPress download metadata, then follow `references/aspera.md`.

## PathDB Metadata

For non-DICOM histopathology, load `references/pathdb.md`. Use WordPress first to confirm the dataset is TCIA-published, then use PathDB metadata to answer slide-level questions, including patient counts, slide counts, image URLs, cancer type/location, data formats, and companion radiology/genomics/proteomics flags.

## Answer Format

For discovery requests, prefer a compact ranked table:

| Dataset | Type | Why it matched | Access route | Access/license | DOI/citation | Notes |
| --- | --- | --- | --- | --- | --- | --- |

Include the TCIA page link, WordPress short title, and any caveats about controlled access, license, or external derived records. For download requests, estimate size/counts when available and recommend a small test download before bulk transfer.

## Guardrails

- Never present a dataset as TCIA-published unless it appears in WordPress Collections or Analysis Results.
- Do not broaden IDC, GC, or PathDB searches beyond WordPress short titles, TCIA DOIs, or explicit user-approved exploratory scope.
- Distinguish public, limited-access, and controlled-access data clearly.
- Do not provide medical, regulatory, or legal conclusions about data suitability. Report metadata, access terms, and citations.
- Verify current package/API behavior when the user asks for latest status, current availability, or exact download commands.
