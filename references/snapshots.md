# SQLite Metadata Snapshots

The skill uses a local SQLite snapshot for routine TCIA discovery instead of querying public APIs during end-user tasks. The snapshot contains:

- TCIA WordPress Collections, Analysis Results, and Downloads.
- Normalized current WordPress download records and their multi-select labels.
- PathDB cohort-builder slide metadata, trimmed to TCIA query fields.
- DataCite records under the TCIA DOI prefix `10.7937`.

Generated snapshot files are intentionally not committed to the repository. GitHub Actions builds them twice daily at 7:17 AM and 7:17 PM America/New_York and publishes changed content as release assets:

- `tcia_snapshot.sqlite.gz`
- `tcia_snapshot_manifest.json`
- `agent_datasets.jsonl`
- `agent_current_downloads.jsonl`
- `agent_datasets.jsonl.gz`
- `agent_current_downloads.jsonl.gz`

Large optional derived metadata assets may also be published on the same release tag. They are not part of `python scripts/tcia_snapshot.py ensure` and are not downloaded during skill install or normal snapshot refresh.

Optional NIfTI assets:

- `nifti_metadata.sqlite.gz`
- `nifti_metadata_manifest.json`

Optional pathology Aspera assets:

- `pathology_metadata.sqlite.gz`
- `pathology_metadata_manifest.json`

Optional controlled-access public manifest/spreadsheet assets:

- `controlled_access_metadata.sqlite.gz`
- `controlled_access_metadata_manifest.json`

The controlled-access SQLite is rebuilt by the scheduled workflow from the fresh base snapshot plus public WordPress manifest and metadata spreadsheet URLs. The workflow uploads it when its controlled metadata release fingerprint changes. NIfTI and pathology assets are larger/manual sidecars: the scheduled workflow checks their source download signatures and warns when they need a manual refresh.

The workflow validates that the SQLite file contains the documented `agent_*` views, writes web-friendly exports from those views, and compares a release fingerprint built from the source-content hash, schema version, SQLite hash, and export hashes. It skips release uploads only when the release fingerprint is unchanged.

Source fetches use bounded retry/backoff for transient network failures such as connection refusals, timeouts, rate limits, and 5xx responses. If any source is temporarily unavailable but a previous release snapshot exists, the workflow reuses that source's previous rows, emits a warning annotation in the build log and a warning entry in the manifest, and still refreshes the other sources. The build fails after retries only when a required source fails and no usable previous snapshot data is available for that source.

GitHub scheduled workflows can start late. If a user asks about a dataset that appears to be missing, say the published snapshot may not include the newest TCIA metadata yet and ask them to try again after the next scheduled snapshot run has had time to finish.

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

The helper scripts use the local snapshot. They do not fall back to live public APIs for normal end-user discovery.

The optional NIfTI SQLite uses separate cache paths and is downloaded only on demand:

```text
cache/nifti_metadata.sqlite
cache/nifti_metadata_manifest.json
```

Users or agents can override the NIfTI SQLite path with:

```bash
export TCIA_NIFTI_METADATA_DB=/path/to/nifti_metadata.sqlite
```

The optional pathology SQLite uses separate cache paths and is downloaded only on demand:

```text
cache/pathology_metadata.sqlite
cache/pathology_metadata_manifest.json
```

Users or agents can override the pathology SQLite path with:

```bash
export TCIA_PATHOLOGY_METADATA_DB=/path/to/pathology_metadata.sqlite
```

The optional controlled-access SQLite uses separate cache paths and is downloaded only on demand:

```text
cache/controlled_access_metadata.sqlite
cache/controlled_access_metadata_manifest.json
```

Users or agents can override the controlled-access SQLite path with:

```bash
export TCIA_CONTROLLED_ACCESS_METADATA_DB=/path/to/controlled_access_metadata.sqlite
```

## Refresh Local Metadata

End users do not need to reinstall the skill just to receive newer TCIA metadata. Skill code/instructions and snapshot data are separate.

- Reinstall or update the skill only when the skill instructions or scripts changed.
- Refresh metadata by updating the local SQLite snapshot:

```bash
python scripts/tcia_snapshot.py ensure
```

By default, `ensure` downloads release assets from `kirbyju/tcia-query-skill`. Use `--repo owner/name` only when testing a fork or alternate release source. The helper compares content hashes and schema versions, then replaces `cache/tcia_snapshot.sqlite` only when the published snapshot data or schema changed.

For NIfTI file-grain metadata, use the separate on-demand helper:

```bash
python scripts/tcia_nifti_metadata.py ensure
```

This downloads and verifies only the optional NIfTI release assets. It is not run by the base snapshot refresh command.

For pathology Aspera package/download metadata, use the separate on-demand helper:

```bash
python scripts/tcia_pathology_metadata.py ensure
```

This downloads and verifies only the optional pathology release assets. It is not run by the base snapshot refresh command.

For controlled-access file-grain public metadata, use the separate on-demand helper:

```bash
python scripts/tcia_controlled_access_metadata.py ensure
```

This downloads and verifies only the optional controlled-access release assets. It is not run by the base snapshot refresh command.

## Build A Snapshot

This section is for maintainers and developers improving the skill. End users trying to find or download TCIA data should use the published release snapshot through `ensure`, not live API queries.

From the skill root:

```bash
python scripts/tcia_snapshot.py build \
  --out cache/tcia_snapshot.sqlite \
  --gzip-out dist/tcia_snapshot.sqlite.gz \
  --manifest-out dist/tcia_snapshot_manifest.json \
  --exports-dir dist \
  --fallback-db previous/tcia_snapshot.sqlite
python scripts/tcia_snapshot.py validate --db cache/tcia_snapshot.sqlite
python scripts/tcia_snapshot.py validate --db dist/tcia_snapshot.sqlite --exports-dir dist
```

The generated SQLite database is suitable for local use. The gzipped artifact and web exports are suitable for GitHub Release distribution. The optional `--fallback-db` is only used for source-level fallback when a live source cannot be fetched; omit it for strict local rebuilds. The validation command fails if required agent-facing views are absent, which prevents documentation/schema drift such as publishing a base-table-only SQLite file.

## Query Behavior

These scripts query the SQLite snapshot:

- `scripts/tcia_wordpress_search.py`
- `scripts/pathdb_metadata.py`
- `scripts/datacite_tcia_dois.py`

Examples:

```bash
python scripts/tcia_wordpress_search.py --query breast
python scripts/pathdb_metadata.py --collection CPTAC-STAD --summary
python scripts/datacite_tcia_dois.py --query lung
```

For direct SQL, prefer the views documented in `references/schema.md`: `agent_datasets`, `agent_current_downloads`, `agent_dataset_access_summary`, `agent_pathdb_slides`, and `agent_datacite_dois`.

## Web-Friendly Release Exports

These assets are generated from the same agent-facing views for hosted/web LLMs that can fetch files from GitHub Releases but cannot install a skill or execute SQLite:

| Asset | Use |
| --- | --- |
| `agent_datasets.jsonl` | Plain-text dataset/access export for web LLMs and browse tools that cannot decompress gzip. |
| `agent_current_downloads.jsonl` | Plain-text current WordPress download export for web LLMs and browse tools that cannot decompress gzip. |
| `agent_datasets.jsonl.gz` | General flattened dataset/access discovery from `agent_dataset_access_summary`. |
| `agent_current_downloads.jsonl.gz` | Current WordPress download records from `agent_current_downloads`. |

Direct release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/tcia_snapshot.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/tcia_snapshot_manifest.json`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_datasets.jsonl`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_current_downloads.jsonl`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_datasets.jsonl.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_current_downloads.jsonl.gz`

Optional NIfTI release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/nifti_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/nifti_metadata_manifest.json`

Optional pathology release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/pathology_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/pathology_metadata_manifest.json`

Optional controlled-access release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/controlled_access_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/controlled_access_metadata_manifest.json`

When an environment has no SQLite execution path, prefer these generic release exports before considering any live API. Use plain `.jsonl` for web LLM browse tools that cannot decompress gzip, and `.jsonl.gz` for local or connector tools that can. They are intentionally table-shaped rather than prompt-specific precomputed answer files. For MCP guidance, see `references/mcp-and-web-llms.md`.

## Optional NIfTI SQLite

The NIfTI SQLite is a file-grain metadata layer mined from visible, non-controlled current NIfTI download records, companion spreadsheets, root `.sums` files, and accepted Aspera listings. It is too large and too specialized to bundle with the normal snapshot refresh.

Use `references/nifti.md` for table details and examples. The scheduled GitHub Action should not rebuild the NIfTI database by default. Instead, it compares the current snapshot's visible non-controlled NIfTI download signature against `nifti_metadata_manifest.json` and emits a warning if a manual NIfTI refresh is needed.

## Optional Pathology SQLite

The pathology SQLite is a package/download metadata layer for visible, non-controlled current pathology Aspera download records. It contains Collection Manager download scope, PathDB crosswalk rows, curator-facing disparity rows, and placeholder package/file tables that can be populated from Aspera browse output or root `.sums` inventories.

Use `references/pathology.md` for table details and examples. The scheduled GitHub Action should not rebuild the pathology database by default. Instead, it compares the current snapshot's visible non-controlled pathology Aspera download signature against `pathology_metadata_manifest.json` and emits a warning if a manual pathology refresh is needed.

## Optional Controlled-Access SQLite

The controlled-access SQLite is a file-grain metadata layer for visible current WordPress controlled/restricted download records routed through General Commons or CTDC. It ingests public WordPress manifest URLs, public metadata spreadsheet URLs, and WordPress `download_metadata` fields; it does not use authenticated GraphQL APIs and does not download controlled files.

Important tables include:

- `controlled_downloads`: scoped current WordPress controlled download records with route classification.
- `wordpress_download_metadata`, `wordpress_download_urls`, and `wordpress_search_filters`: public WordPress metadata fields and extracted URLs used to find manifests/spreadsheets.
- `manifest_rows`: public manifest rows, including `drs_uri` and file IDs when available.
- `metadata_rows`: public spreadsheet rows with TCIA/IDC-shaped radiology metadata.
- `controlled_files`: normalized file-grain rows combining manifest and spreadsheet metadata.
- `radiology_series`, `idc_index`, `idc_ct_index`, `idc_pt_index`, and `idc_series_links`: IDC-parquet-shaped radiology views for controlled metadata discovery only.

Use `references/controlled-access.md` for policy guidance and examples. The scheduled GitHub Action rebuilds this database from the freshly built base snapshot and uploads `controlled_access_metadata.sqlite.gz` plus `controlled_access_metadata_manifest.json` when the controlled metadata fingerprint changes.

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
