# SQLite Metadata Snapshots

The skill uses a local SQLite snapshot for routine TCIA discovery instead of querying public APIs during end-user tasks. The snapshot contains:

- TCIA WordPress Collections, Analysis Results, and Downloads.
- Normalized current WordPress download records and their multi-select labels.
- PathDB cohort-builder slide metadata, trimmed to TCIA query fields.
- DataCite records under the TCIA DOI prefix `10.7937`.

Generated snapshot files are intentionally not committed to the repository. GitHub Actions builds them twice daily at 7:17 AM and 7:17 PM America/New_York and publishes changed content as release assets:

- `tcia_snapshot.sqlite.gz`
- `tcia_snapshot_manifest.json`
- `agent_datasets.jsonl.gz`
- `agent_current_downloads.jsonl.gz`
- `controlled_access_datasets.json`
- `dicom_annotation_index.json`

The workflow validates that the SQLite file contains the documented `agent_*` views, writes web-friendly exports from those views, and compares a release fingerprint built from the source-content hash, schema version, SQLite hash, and export hashes. It skips release uploads only when the release fingerprint is unchanged.

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

## Refresh Local Metadata

End users do not need to reinstall the skill just to receive newer TCIA metadata. Skill code/instructions and snapshot data are separate.

- Reinstall or update the skill only when the skill instructions or scripts changed.
- Refresh metadata by updating the local SQLite snapshot:

```bash
python scripts/tcia_snapshot.py ensure
```

By default, `ensure` downloads release assets from `kirbyju/tcia-query-skill`. Use `--repo owner/name` only when testing a fork or alternate release source. The helper compares content hashes and schema versions, then replaces `cache/tcia_snapshot.sqlite` only when the published snapshot data or schema changed.

## Build A Snapshot

This section is for maintainers and developers improving the skill. End users trying to find or download TCIA data should use the published release snapshot through `ensure`, not live API queries.

From the skill root:

```bash
python scripts/tcia_snapshot.py build \
  --out cache/tcia_snapshot.sqlite \
  --gzip-out dist/tcia_snapshot.sqlite.gz \
  --manifest-out dist/tcia_snapshot_manifest.json \
  --exports-dir dist
python scripts/tcia_snapshot.py validate --db cache/tcia_snapshot.sqlite
python scripts/tcia_snapshot.py validate --db dist/tcia_snapshot.sqlite --exports-dir dist
```

The generated SQLite database is suitable for local use. The gzipped artifact and web exports are suitable for GitHub Release distribution. The validation command fails if required agent-facing views are absent, which prevents documentation/schema drift such as publishing a base-table-only SQLite file.

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
| `agent_datasets.jsonl.gz` | General flattened dataset/access discovery from `agent_dataset_access_summary`. |
| `agent_current_downloads.jsonl.gz` | Current WordPress download records from `agent_current_downloads`. |
| `controlled_access_datasets.json` | Visible controlled or mixed-access datasets with controlled-download summary fields. |
| `dicom_annotation_index.json` | Visible DICOM annotation/result download records. |

When an environment has no SQLite execution path, prefer these release exports before considering any live API. For MCP guidance, see `references/mcp-and-web-llms.md`.

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
