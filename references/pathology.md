# TCIA Pathology Aspera Metadata

TCIA's WordPress/Collection Manager snapshot is the authority for which non-DICOM pathology Aspera downloads are current, visible, non-controlled, and TCIA-published. The optional pathology SQLite release adds a download-level scope table, Aspera-derived package inventory tables, and a PathDB crosswalk for enrichment/disparity review.

Use this reference when a user asks about public TCIA pathology Aspera packages, package/download scope, PathDB coverage gaps, or curator-facing PathDB/package disparities.

## Optional Release Asset

The pathology SQLite is intentionally not downloaded when the skill is installed or when the normal TCIA snapshot is refreshed. Users or agents that need pathology Aspera package/download metadata can download it on demand:

```bash
python scripts/tcia_pathology_metadata.py ensure
```

Default local cache paths:

```text
cache/pathology_metadata.sqlite
cache/pathology_metadata_manifest.json
```

Override the SQLite path with:

```bash
export TCIA_PATHOLOGY_METADATA_DB=/path/to/pathology_metadata.sqlite
```

Release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/pathology_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/pathology_metadata_manifest.json`

## When To Use

Use the normal TCIA snapshot first to confirm TCIA provenance, visibility, access level, and user-facing download URLs. Then use the pathology SQLite for pathology Aspera package scope and PathDB reconciliation details.

Good pathology SQLite use cases:

- List visible, non-controlled TCIA pathology Aspera download records.
- Identify pathology Aspera downloads with PathDB coverage.
- Inspect Aspera-derived package file rows and normalized file objects.
- Review PathDB collections that do not exactly match Collection Manager short titles.
- Review Collection Manager pathology Aspera downloads that have no exact PathDB collection match.
- Inspect package-file rows after Aspera browse or root `.sums` inventories have been imported.

Do not use this SQLite to authorize controlled data, replace WordPress licensing metadata, or infer clinical truth. Hidden and controlled downloads are excluded by design.

## Helper Script

Run from the skill root:

```bash
python scripts/tcia_pathology_metadata.py ensure
python scripts/tcia_pathology_metadata.py info
python scripts/tcia_pathology_metadata.py datasets --limit 20
python scripts/tcia_pathology_metadata.py downloads --collection CPTAC-CCRCC
python scripts/tcia_pathology_metadata.py pathdb --collection CPTAC-STAD --limit 10
python scripts/tcia_pathology_metadata.py disparities
```

The helper downloads and verifies `pathology_metadata.sqlite.gz` only when `ensure` is run. Query commands expect the local SQLite to exist and will ask the user to run `ensure` if it does not.

Maintainer commands for a download-scope-only build without package inventory:

```bash
python scripts/tcia_pathology_metadata.py build \
  --snapshot-db dist/tcia_snapshot.sqlite \
  --out outputs/pathology_metadata/pathology_metadata.sqlite \
  --gzip-out outputs/pathology_metadata/pathology_metadata.sqlite.gz \
  --manifest-out outputs/pathology_metadata/pathology_metadata_manifest.json \
  --replace \
  --no-pathdb-file-objects
python scripts/tcia_pathology_metadata.py validate --db outputs/pathology_metadata/pathology_metadata.sqlite
python scripts/tcia_pathology_metadata.py drift-check \
  --snapshot-db dist/tcia_snapshot.sqlite \
  --manifest previous/pathology_metadata_manifest.json
```

Maintainer commands for an Aspera package inventory refresh:

```bash
python scripts/tcia_pathology_aspera_inventory.py \
  --snapshot-db dist/tcia_snapshot.sqlite \
  --out outputs/pathology_metadata/pathology_aspera_inventory.tsv \
  --summary-out outputs/pathology_metadata/pathology_aspera_inventory_summary.json \
  --sums-cache-dir outputs/pathology_metadata/pathology_sums_manifests \
  --offset-paging \
  --page-limit 1000 \
  --timeout 300 \
  --workers 1 \
  --retries 2 \
  --retry-sleep 2
python scripts/tcia_pathology_metadata.py build \
  --snapshot-db dist/tcia_snapshot.sqlite \
  --out outputs/pathology_metadata/pathology_metadata.sqlite \
  --package-inventory-tsv outputs/pathology_metadata/pathology_aspera_inventory.tsv \
  --gzip-out outputs/pathology_metadata/pathology_metadata.sqlite.gz \
  --manifest-out outputs/pathology_metadata/pathology_metadata_manifest.json \
  --replace \
  --no-pathdb-file-objects
```

## Core Tables

Prefer the `agent_*` views for routine querying:

| View | Use |
| --- | --- |
| `agent_pathology_downloads` | Collection Manager pathology Aspera download scope with `download_label` fallback text. |
| `agent_pathology_dataset_summary` | Dataset summary, PathDB coverage, and `package_inventory_status`. |
| `agent_pathology_package_files` | Imported Aspera package inventory rows when available. |
| `agent_pathology_file_objects` | Normalized file objects from imported Aspera rows. |

| Table | Use |
| --- | --- |
| `pathology_downloads` | Collection Manager pathology Aspera download scope: visible, non-controlled, current download records. |
| `pathology_download_label_matches` | Labels/title hints explaining why each download was selected as pathology-related. |
| `pathology_package_files` | Raw Aspera package inventory rows after browse or `.sums` import. This table is empty until inventories are imported. |
| `pathology_file_objects` | Normalized file records from imported Aspera package rows. |
| `pathdb_slide_crosswalk` | PathDB rows matched to TCIA short titles for enrichment and discrepancy review. These rows do not define the Aspera package inventory. |
| `pathology_disparities` | Curator-facing scope/reconciliation rows, such as PathDB collections without exact Collection Manager short-title matches. |

## Common Queries

List pathology Aspera downloads:

```sql
SELECT short_title, download_id, download_title, access_level, license_label
FROM pathology_downloads
ORDER BY lower(short_title), download_id;
```

Summarize datasets and PathDB coverage:

```sql
SELECT
  short_title,
  download_records,
  pathdb_collection_slide_count,
  pathdb_collection_patient_count,
  package_inventory_status,
  open_noncommercial_downloads
FROM agent_pathology_dataset_summary
ORDER BY lower(short_title);
```

Review disparities:

```sql
SELECT disparity_type, short_title, download_id, pathdb_collection, message
FROM pathology_disparities
ORDER BY disparity_type, lower(COALESCE(short_title, pathdb_collection, ''));
```

Inspect file objects:

```sql
SELECT short_title, download_id, file_name, file_ext, file_role, source_table, package_path
FROM agent_pathology_file_objects
ORDER BY lower(short_title), download_id, package_path
LIMIT 50;
```

## Interpretation Notes

Collection Manager/WordPress download metadata decides scope, access level, license, and provenance. The Aspera package is the authoritative file copy for these pathology downloads. PathDB is best-effort enrichment for additional metadata and browser visualization of representative/sample images, but PathDB does not decide whether a package is TCIA-published and may not cover every package file.

The release build should pass `--no-pathdb-file-objects` so `pathology_file_objects` is populated only from Aspera package inventory rows. When Aspera browse output or root `.sums` inventories are imported, matching datasets move to `normalized_file_rows_available`. `not_imported` means Aspera package inventory rows were not available for that dataset in the release. The legacy `pathdb_file_objects_available` status can appear only in locally built or older SQLite files that opted into PathDB file-object seeding.

`pathdb_slide_crosswalk.match_status = 'collection_only'` means the row is matched by PathDB collection/TCIA short title only.

Recommended future file-level `match_status` values:

- `exact_url`
- `exact_filename`
- `filename_plus_collection`
- `slide_id_pattern`
- `collection_only`
- `no_pathdb_match`
- `pathdb_row_without_download_file`
- `ambiguous_multiple_matches`

For package preservation and future access workflows, TCIA expects public pathology Aspera packages to be treated as authoritative and preserved as-is, including image files, metadata, JSON, CSV, sidecars, package documentation, and other non-image files. Keep using Collection Manager download URLs and the optional pathology SQLite for scope/reconciliation until any future access transition is complete.

## Refresh Strategy

Scheduled workflow should:

1. Build the normal TCIA WordPress/PathDB/DataCite snapshot.
2. Build `pathology_metadata.sqlite` from the fresh snapshot without PathDB file-object seeding.
3. Validate the pathology SQLite and write `pathology_metadata_manifest.json`.
4. Skip pathology release publishing unless a manual workflow dispatch explicitly refreshed the Aspera package inventory.

Run `scripts/tcia_pathology_aspera_inventory.py` manually when maintainers want to add or refresh Aspera package inventory rows. The script prefers root checksum manifests such as `.sums` files and uses offset-paged browse calls for packages that must be walked. That live package browse can be slow for large directory trees, so publishing the optional pathology release is intentionally manual.
