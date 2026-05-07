# SQLite Metadata Snapshots

The skill can use a local SQLite snapshot for routine TCIA discovery instead of querying every source live. The snapshot contains:

- TCIA WordPress Collections, Analysis Results, and Downloads.
- Normalized current WordPress download records and their multi-select labels.
- PathDB cohort-builder slide metadata, trimmed to TCIA query fields.
- DataCite records under the TCIA DOI prefix `10.7937`.

Generated snapshot files are intentionally not committed to the repository. GitHub Actions builds them twice daily and publishes changed content as release assets:

- `tcia_snapshot.sqlite.gz`
- `tcia_snapshot_manifest.json`

The workflow compares source-content hashes and skips release uploads when the source content has not changed.

## Local Cache

Default local cache paths:

```text
cache/tcia_snapshot.sqlite
cache/tcia_snapshot_manifest.json
```

Users or agents can override the SQLite path with:

```bash
export TCIA_SNAPSHOT_DB=/path/to/tcia_snapshot.sqlite
```

The helper scripts prefer the local snapshot when it exists. Use `--live` to bypass the snapshot when a user suspects stale or missing metadata.

## Build A Snapshot

From the skill root:

```bash
python scripts/tcia_snapshot.py build \
  --out cache/tcia_snapshot.sqlite \
  --gzip-out dist/tcia_snapshot.sqlite.gz \
  --manifest-out dist/tcia_snapshot_manifest.json
```

The generated SQLite database is suitable for local use. The gzipped artifact is suitable for GitHub Release distribution.

## Install Or Update From A Release

From the skill root, when release assets are available:

```bash
python scripts/tcia_snapshot.py ensure --repo <owner>/<repo>
```

The helper checks the release manifest content hash and downloads the snapshot only when the local cache is missing or changed.

## Query Behavior

These scripts prefer the SQLite snapshot when available:

- `scripts/tcia_wordpress_search.py`
- `scripts/pathdb_metadata.py`
- `scripts/datacite_tcia_dois.py`

Troubleshooting examples:

```bash
python scripts/tcia_wordpress_search.py --query breast --live
python scripts/pathdb_metadata.py --collection CPTAC-STAD --summary --live
python scripts/datacite_tcia_dois.py --query lung --live
```

## WordPress Download Tables

The snapshot keeps the original WordPress JSON in `wordpress_records.raw_json`, but download labels are also relational:

- `wordpress_downloads`: one row per nested current-version `collection_downloads` or `result_downloads` record, plus one row per global downloads endpoint record.
- `wordpress_download_labels`: one row per multi-select label from `download_type`, `data_type`, `file_type`, and `external_resources`.

Use the boolean `wordpress_downloads.is_current_version` column for normal user-facing discovery, for example `is_current_version IS TRUE`. Rows from the global downloads endpoint have `is_current_version = FALSE`; they are useful for troubleshooting but may include older release rows that are no longer part of the current Collection or Analysis Result version.

Do not rely only on a Collection or Analysis Result top-level `data_types` value for modality questions. Mixed datasets can expose labels such as `MR`, `CT`, `PT`, or `DICOM` only on the download records. For example:

```sql
SELECT DISTINCT d.parent_short_title, d.download_id, d.download_url
FROM wordpress_downloads d
JOIN wordpress_download_labels l
  ON l.download_row_id = d.download_row_id
WHERE d.is_current_version IS TRUE
  AND l.label_kind = 'data_type'
  AND l.label = 'MR';
```

## PathDB Columns

The snapshot keeps these PathDB columns:

```text
collection
patient_id
slide_id
wsiimage_url
species
cancer_type
cancer_location
data_format
modality
protocol
par
magnification
update
camic_id
```

Generate caMicroscope URLs dynamically from `camic_id`; do not store full repeated viewer URLs in the snapshot.
