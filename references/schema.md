# SQLite Snapshot Schema

Use this reference when querying `cache/tcia_snapshot.sqlite` or a database selected by `TCIA_SNAPSHOT_DB`.

The GitHub Release web exports mirror the most useful agent-facing views for environments that cannot run SQLite. Use `agent_datasets.jsonl` or `agent_datasets.jsonl.gz` for `agent_dataset_access_summary`, `agent_current_downloads.jsonl` or `agent_current_downloads.jsonl.gz` for `agent_current_downloads`, `agent_dataset_versions.jsonl` or `.gz` for matched version history, and `agent_dataset_v1_releases.jsonl` or `.gz` for first-release dates. Prefer plain `.jsonl` for web LLM browse tools that cannot decompress gzip. Filter these generic JSONL tables for controlled/mixed access, modalities, DICOM annotation labels, download routes, or release dates instead of relying on prompt-specific precomputed exports.

The optional NIfTI file-grain SQLite is separate from `cache/tcia_snapshot.sqlite`. It is downloaded only when needed with `python scripts/tcia_nifti_metadata.py ensure`, defaults to `cache/nifti_metadata.sqlite`, and is documented in `references/nifti.md`.

The optional pathology Aspera SQLite is also separate from `cache/tcia_snapshot.sqlite`. It is downloaded only when needed with `python scripts/tcia_pathology_metadata.py ensure`, defaults to `cache/pathology_metadata.sqlite`, and is documented in `references/pathology.md`.

The optional controlled-access SQLite is also separate from `cache/tcia_snapshot.sqlite`. It is downloaded only when needed with `python scripts/tcia_controlled_access_metadata.py ensure`, defaults to `cache/controlled_access_metadata.sqlite`, and is documented in `references/controlled-access.md`.

## Agent-Facing Views

Prefer these views for normal discovery. They flatten common JSON fields and keep the base tables available as lower-level provenance.

### `agent_datasets`

One row per TCIA WordPress Collection or Analysis Result.

Key columns:

- `source`: `collections` or `analysis-results`.
- `dataset_type`: `Collection` or `Analysis Result`.
- `short_title`: WordPress short title; use this as the cross-system key.
- `short_title_key`: normalized short-title join key used internally for version history matching across punctuation/case differences.
- `title`, `doi`, `link`, `date_updated`, `hidden`.
- `current_version_number`: current Collection Manager version number, when exposed by WordPress.
- `license_status`, `licenses`, `controlled_access`, `noncommercial_license`.
- `access_level`: `open`, `open_noncommercial`, `controlled`, `mixed`, `review_needed`, or `unknown`.
- `controlled_access_policy_url`: populated when the dataset-level license is controlled.
- `subjects`, `data_types`, `download_types`, `download_data_types`, `download_file_types`.
- `cancer_types`, `cancer_locations`, `species`, `program`.

Default user-facing filter:

```sql
SELECT short_title, title, access_level, doi, link
FROM agent_datasets
WHERE hidden = 0
ORDER BY short_title;
```

### `agent_current_downloads`

One row per current nested `collection_downloads` or `result_downloads` record. This excludes historical/global WordPress download endpoint rows.

Key columns:

- `short_title`, `title`, `dataset_type`, `hidden`.
- `download_id`, `download_title`, `download_url`, `download_metadata`, `search_url`.
- `download_types`, `data_types`, `file_types`, `external_resources`: JSON arrays as text.
- `license_label`, `license_url`, `requirements_label`, `requirements_url`, `requirements_text`.
- `subjects`, `studies`, `series`, `images`, `download_size`, `download_size_unit`.
- `access_level`, `controlled_access`, `noncommercial_license`, `controlled_access_policy_url`.

Find current MR downloads:

```sql
SELECT DISTINCT d.short_title, d.download_title, d.access_level, d.download_url
FROM agent_current_downloads d
JOIN wordpress_download_labels l
  ON l.download_row_id = d.download_row_id
WHERE d.hidden = 0
  AND l.label_kind = 'data_type'
  AND l.label = 'MR';
```

### `agent_dataset_access_summary`

One row per dataset with download-level access counts. Use this view when `access_level` is `mixed` or when deciding whether a download answer must split open and controlled files.

Additional columns:

- `current_download_count`.
- `controlled_download_count`.
- `noncontrolled_download_count`.
- `open_noncommercial_download_count`.
- `controlled_download_titles`.
- `controlled_license_labels`.
- `controlled_download_ids`.
- `controlled_download_urls`.
- `resolved_access_level`: download-aware access level.
- `resolved_controlled_access_policy_url`.

Mixed access rule:

- `resolved_access_level = 'mixed'` means some current downloads are controlled and others are not.
- Do not imply the whole dataset is open.
- Separate open/noncontrolled downloads from controlled downloads in the answer.
- Do not directly download controlled data. For controlled downloads, provide the TCIA NIH Controlled Data Access Policy link and, when useful, portable TCIA Data Retriever manifest guidance only.

Inspect a mixed dataset:

```sql
SELECT
  short_title,
  resolved_access_level,
  controlled_download_count,
  noncontrolled_download_count,
  controlled_download_titles,
  resolved_controlled_access_policy_url
FROM agent_dataset_access_summary
WHERE hidden = 0
  AND resolved_access_level = 'mixed';
```

### `agent_dataset_versions`

One row per current TCIA dataset matched to a `/api/v2/versions` record. The match uses exact short titles when possible and a normalized fallback key that strips punctuation and case. This matters for historical records where the version endpoint may use legacy related short titles such as `CT-IMAGES-IN-COVID-19` while the current Collection page uses `CT Images in COVID-19`.

Key columns:

- Dataset columns: `source`, `dataset_type`, `short_title`, `title`, `doi`, `link`, `date_updated`, `current_version_number`, `subjects`, `hidden`.
- Version columns: `version_id`, `version_slug`, `version_post_title`, `version_number`, `version_date`, `version_related_short_title`.
- `match_method`: `exact_short_title` or `normalized_short_title`.
- `version_downloads`, `version_text`, `version_normalized_json`, `version_raw_json`.

Find the version history for a dataset:

```sql
SELECT short_title, version_number, version_date, version_post_title, match_method
FROM agent_dataset_versions
WHERE hidden = 0
  AND short_title = 'CT Images in COVID-19'
ORDER BY CAST(NULLIF(version_number, '') AS INTEGER), version_date;
```

### `agent_dataset_v1_releases`

One row per current TCIA dataset with the best available version 1 release date. The view prefers matched `/api/v2/versions` rows with `version_number = '1'`. If no matched v1 row exists and the current dataset is still version 1, it falls back to the current record's `date_updated`.

Key columns:

- Dataset columns: `source`, `dataset_type`, `short_title`, `title`, `doi`, `link`, `date_updated`, `current_version_number`, `subjects`, `hidden`.
- `v1_release_date`: best available first-release date.
- `v1_release_date_source`: `versions_endpoint_exact_short_title`, `versions_endpoint_normalized_short_title`, `current_record_still_v1_date_updated`, or blank when no v1 date can be inferred.
- Version provenance: `version_id`, `version_slug`, `version_post_title`, `version_related_short_title`, `match_method`.

Find visible datasets first released since a given date:

```sql
SELECT dataset_type,
       short_title,
       title,
       v1_release_date,
       v1_release_date_source,
       subjects,
       link
FROM agent_dataset_v1_releases
WHERE hidden = 0
  AND v1_release_date >= '2025-01-01'
ORDER BY v1_release_date, lower(short_title);
```

### `agent_pathdb_slides`

One row per PathDB cohort-builder slide row.

Key columns:

- `collection`: TCIA WordPress short title.
- `patient_id`, `slide_id`, `camic_id`.
- `camicroscope_url`: built from `camic_id`.
- `wsiimage_url`, `species`, `cancer_type`, `cancer_location`, `data_format`, `modality`.

For caMicroscope URLs, use `camic_id`. The URL parameter is named `slideId`, but it expects the numeric `camic_id`, not the CSV `slide_id`.

```sql
SELECT collection, patient_id, slide_id, camic_id, camicroscope_url
FROM agent_pathdb_slides
WHERE collection = 'CPTAC-STAD'
LIMIT 5;
```

### `agent_datacite_dois`

One row per DataCite record under the TCIA DOI prefix `10.7937`.

Key columns:

- `doi`, `tcia_short_name`, `title`, `publisher`, `publication_year`, `version`, `state`, `url`.

## Base Tables

Use base tables when the views do not expose a needed detail.

- `snapshot_meta`: schema version, source hashes, source URLs, generated timestamp, and counts.
- `wordpress_records`: WordPress Collections, Analysis Results, and global Downloads endpoint rows. `raw_json` stores source JSON. `normalized_json` is populated for Collections and Analysis Results.
- `wordpress_downloads`: normalized nested current download records plus global Downloads endpoint rows. Use `is_current_version = 1` for normal user-facing downloads.
- `wordpress_download_labels`: one row per `download_type`, `data_type`, `file_type`, and `external_resource` label.
- `wordpress_versions`: normalized `/api/v2/versions` rows, expanded to one row per related Collection or Analysis Result short title.
- `pathdb_rows`: trimmed PathDB cohort-builder slide metadata.
- `pathdb_collection_summary`: collection-level PathDB patient/slide summaries.
- `datacite_dois`: TCIA DOI prefix records from DataCite.

## Optional Pathology SQLite

Use `cache/pathology_metadata.sqlite` only after the base snapshot has confirmed TCIA provenance, visibility, and access/license metadata. Important tables:

- `pathology_downloads`: visible, non-controlled, current pathology Aspera download records selected from Collection Manager/WordPress metadata.
- `pathology_download_label_matches`: label/title evidence for why a download was selected as pathology-related.
- `pathology_package_files`: imported Aspera package browse or `.sums` inventory rows. This table may be empty before package inventories are imported.
- `pathology_file_objects`: normalized file rows derived from imported Aspera package inventories.
- `pathdb_slide_crosswalk`: PathDB rows matched by exact TCIA/PathDB collection short title for enrichment and discrepancy review. These rows do not define the Aspera package inventory.
- `pathology_disparities`: curator-facing rows for PathDB/download scope mismatches, multiple-download cases, and future package-file reconciliation issues.

Prefer the agent-facing views for normal use:

- `agent_pathology_downloads`: current pathology Aspera download records with `download_label` fallback text.
- `agent_pathology_dataset_summary`: dataset-level scope plus `package_inventory_status`.
- `agent_pathology_package_files`: imported package inventory rows when available.
- `agent_pathology_file_objects`: normalized file objects from imported Aspera package rows.

`package_inventory_status = 'normalized_file_rows_available'` means imported Aspera package rows have been normalized into file objects. `package_inventory_status = 'not_imported'` means Aspera package inventory rows are not available for that dataset in the release. `pathdb_file_objects_available` can appear only in locally built or legacy SQLite files that opted into PathDB file-object seeding.

Common pathology summary:

```sql
SELECT short_title, download_records, pathdb_collection_slide_count,
       package_inventory_status, open_noncommercial_downloads
FROM agent_pathology_dataset_summary
ORDER BY lower(short_title);
```

## Optional Controlled-Access SQLite

Use `cache/controlled_access_metadata.sqlite` only after the base snapshot has confirmed TCIA provenance and controlled/restricted access. The data are public metadata extracted from WordPress-controlled download records, public manifests, public spreadsheet metadata URLs, and WordPress `download_metadata` fields. The SQLite does not grant file access and must not be used to download controlled data directly.

Important tables:

- `controlled_downloads`: current visible WordPress controlled download records routed to `general_commons` or `ctdc`.
- `wordpress_download_metadata`: key/value expansion of WordPress `download_metadata`, including nested objects serialized as JSON.
- `wordpress_download_urls`: URLs extracted from download metadata and classified as manifest, metadata spreadsheet, data dictionary, supporting docs, or other.
- `wordpress_search_filters`: parsed downstream search URL filters such as study names or IDs.
- `source_artifacts`: fetched public manifest/spreadsheet artifacts, hashes, and fetch status.
- `manifest_rows`: public manifest rows, including `drs_uri`, `file_id`, file name/type/size, study, participant, sample, and series identifiers when available.
- `metadata_rows`: public spreadsheet rows with patient, study, series, modality, manufacturer, protocol, pixel spacing, release, species, phantom, and longitudinal metadata.
- `controlled_files`: normalized file-grain rows combining manifest rows and spreadsheet rows by download and Series Instance UID where possible.
- `radiology_series`: radiology-oriented rows aligned to the public NIfTI/pathology metadata model.
- `idc_index`, `idc_ct_index`, `idc_pt_index`, `idc_contrast_index`, and `idc_series_links`: IDC-parquet-shaped indexes for controlled-access metadata discovery only.
- `controlled_metadata_exceptions`: review rows for manifest rows without DRS URIs, rows without Series Instance UIDs, and spreadsheet series that did not match a manifest row.

Prefer the agent-facing views for normal use:

- `agent_controlled_downloads`: scoped controlled downloads with `download_label` fallback text and policy URL.
- `agent_controlled_files`: normalized file rows with lower-snake-case identifiers such as `series_instance_uid`, `study_instance_uid`, `drs_uri`, and `file_id`.
- `agent_controlled_dataset_summary`: route/dataset summary with policy URL.

Common controlled-access summary:

```sql
SELECT route_system, short_title, controlled_file_rows,
       participant_ids, patient_ids, series_instance_uids
FROM agent_controlled_dataset_summary
ORDER BY route_system, lower(short_title);
```

Find controlled CTDC PET rows with DRS URIs:

```sql
SELECT short_title, file_name, modality, participant_id,
       series_instance_uid, drs_uri
FROM controlled_files
WHERE route_system = 'ctdc'
  AND upper(COALESCE(modality, image_modality, '')) = 'PT'
  AND COALESCE(drs_uri, '') <> ''
LIMIT 25;
```

## Optional NIfTI SQLite

Use `cache/nifti_metadata.sqlite` only after the base snapshot has confirmed TCIA provenance, visibility, and access/license metadata. Prefer these agent-facing views:

- `agent_nifti_downloads`: WordPress NIfTI download provenance with `download_label` fallback text.
- `agent_nifti_dataset_summary`: all NIfTI download scope plus file/radiology/derived-object counts.
- `agent_nifti_files`: canonical radiology file/series rows with lower-snake-case aliases such as `series_instance_uid` and `study_instance_uid` when source UIDs exist.
- `agent_nifti_derived_objects`: segmentation/derived object rows and best-effort source references.

Common NIfTI summary:

```sql
SELECT short_title, nifti_downloads, nifti_files,
       radiology_series_rows, mr_files, ct_files
FROM agent_nifti_dataset_summary
ORDER BY lower(short_title);
```

## Common Joins

Join datasets to current downloads:

```sql
SELECT d.short_title, d.title, w.download_title, w.access_level, w.download_url
FROM agent_datasets d
JOIN agent_current_downloads w
  ON w.parent_source = d.source
 AND w.short_title = d.short_title
WHERE d.hidden = 0;
```

Join datasets to PathDB slides:

```sql
SELECT d.short_title, d.title, p.patient_id, p.slide_id, p.camicroscope_url
FROM agent_datasets d
JOIN agent_pathdb_slides p
  ON lower(p.collection) = lower(d.short_title)
WHERE d.hidden = 0;
```

Find controlled or mixed-access datasets with CT, PET, and annotation/result labels:

```sql
SELECT DISTINCT s.short_title, s.title, s.dataset_type,
       s.resolved_access_level, s.download_data_types, s.download_types, s.link
FROM agent_dataset_access_summary s
WHERE s.hidden = 0
  AND s.resolved_access_level IN ('controlled', 'mixed')
  AND EXISTS (
    SELECT 1 FROM agent_current_downloads d, json_each(d.data_types) x
    WHERE d.short_title = s.short_title AND d.hidden = 0
      AND lower(x.value) IN ('ct', 'computed tomography')
  )
  AND EXISTS (
    SELECT 1 FROM agent_current_downloads d, json_each(d.data_types) x
    WHERE d.short_title = s.short_title AND d.hidden = 0
      AND lower(x.value) IN ('pt', 'pet')
  )
  AND (
    EXISTS (
      SELECT 1 FROM agent_current_downloads d, json_each(d.data_types) x
      WHERE d.short_title = s.short_title AND d.hidden = 0
        AND lower(x.value) IN ('seg', 'segmentation', 'rtstruct', 'sr', 'annotation', 'annotations')
    )
    OR EXISTS (
      SELECT 1 FROM agent_current_downloads d, json_each(d.download_types) x
      WHERE d.short_title = s.short_title AND d.hidden = 0
        AND lower(x.value) LIKE '%annotation%'
    )
  )
ORDER BY s.short_title;
```
