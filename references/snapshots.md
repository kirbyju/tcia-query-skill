# SQLite Metadata Snapshots

The skill can use a local SQLite snapshot for routine TCIA discovery instead of querying every source live. The snapshot contains:

- TCIA WordPress Collections, Analysis Results, and Downloads.
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
