# SQLite Snapshot Schema

Use this reference when querying `cache/tcia_snapshot.sqlite` or a database selected by `TCIA_SNAPSHOT_DB`.

## Agent-Facing Views

Prefer these views for normal discovery. They flatten common JSON fields and keep the base tables available as lower-level provenance.

### `agent_datasets`

One row per TCIA WordPress Collection or Analysis Result.

Key columns:

- `source`: `collections` or `analysis-results`.
- `dataset_type`: `Collection` or `Analysis Result`.
- `short_title`: WordPress short title; use this as the cross-system key.
- `title`, `doi`, `link`, `date_updated`, `hidden`.
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
- `pathdb_rows`: trimmed PathDB cohort-builder slide metadata.
- `pathdb_collection_summary`: collection-level PathDB patient/slide summaries.
- `datacite_dois`: TCIA DOI prefix records from DataCite.

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

