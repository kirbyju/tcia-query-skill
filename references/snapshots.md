# SQLite Metadata Snapshots

For the default V2 bundle contract, load `references/artifact-model-v2.md`.
After successful scheduled base-snapshot
updates, the V2 workflow copies the base snapshot and optional sidecars,
regenerates all eight web exports, builds the two V2 research databases and
their optional audit companions, and publishes the complete profile-based,
hash-pinned bundle only when its fingerprint changes. The moving stable tag is
`tcia-metadata-v2-latest`; immutable tags preserve reproducible releases and
`tcia-metadata-v2-preview` remains available for explicit candidates. The
workflow leaves `tcia-snapshot-latest` and its May 2026-compatible contracts
unchanged. Publication does not itself deploy or restart MCP/REST.

The skill uses a local SQLite snapshot for routine TCIA discovery instead of querying public APIs during end-user tasks. The snapshot contains:

- TCIA WordPress Collections, Analysis Results, and Downloads.
- Normalized current WordPress download records and their multi-select labels.
- TCIA WordPress version records from `/api/v2/versions`, matched back to datasets with exact and normalized short-title keys.
- PathDB cohort-builder slide metadata, trimmed to TCIA query fields.
- DataCite records under the TCIA DOI prefix `10.7937`.

Generated snapshot files are intentionally not committed to the repository. GitHub Actions builds them twice daily at 7:17 AM and 7:17 PM America/New_York and publishes changed content as release assets:

- `tcia_snapshot.sqlite.gz`
- `tcia_snapshot_manifest.json`
- `agent_datasets.jsonl`
- `agent_current_downloads.jsonl`
- `agent_dataset_versions.jsonl`
- `agent_dataset_v1_releases.jsonl`
- `agent_datasets.jsonl.gz`
- `agent_current_downloads.jsonl.gz`
- `agent_dataset_versions.jsonl.gz`
- `agent_dataset_v1_releases.jsonl.gz`

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

Optional patient-level clinical assets:

- `clinical_metadata.sqlite.gz`
- `clinical_metadata_manifest.json`

The controlled-access SQLite is rebuilt by the scheduled workflow from the fresh base snapshot plus public WordPress manifest and metadata spreadsheet URLs. The clinical SQLite uses a hybrid refresh: unchanged direct official artifacts and same-version IDC clinical tables are reused, while a new clinical schema or IDC version triggers complete reprocessing so revised column mappings take effect. Small TCIA-linked external clinical sources, currently Allen Institute IvyGAP tumor details and FDA DIDSR VICTRE lesion-location archives, are fetched on every scheduled build and matched to current TCIA/IDC imaging identifiers so source additions change the release fingerprint and appear in the metadata change report. The prior strict CDA/DICOM seed is carried forward. Single-label WordPress Collection diagnosis/site fallbacks are recomputed locally from every fresh base snapshot. The workflow uploads either sidecar only when its release fingerprint changes. NIfTI and pathology assets are larger/manual sidecars: the scheduled workflow checks their source download signatures and optional SQLite schema versions, then warns when they need a manual refresh.

The imaging-subject allowlist is built from every IDC collection that maps
unambiguously to a visible TCIA short title, including collections without an
IDC clinical table. This permits official TCIA spreadsheets such as the ACRIN
6664 polyp files to link to imaging subjects while leaving participants absent
from those spreadsheets unclassified. For controlled HNSCC imaging, which is
not present in IDC's public index, the allowlist additionally accepts the two
official public patient tables only when their audited counts are exactly
492 and 215 subjects, 80 overlapping, and 627 in the union. This identifies
the TCIA cohort but does not grant controlled-image access.

The workflow validates that the SQLite file contains the documented `agent_*` views, writes web-friendly exports from those views, and compares a release fingerprint built from the source-content hash, schema version, SQLite hash, and export hashes. It skips release uploads only when the release fingerprint is unchanged.

Each scheduled or manual run also compares the newly built base,
controlled-access, and clinical SQLite files with their previously published
copies. `scripts/tcia_metadata_change_report.py` writes a Markdown table of
monitored row additions/removals and representative new identifiers to the
GitHub Actions job summary. New additions and newly appearing clinical
screening-review flags also emit warning annotations. Manual pathology refreshes
include the pathology SQLite in the same report.

GitHub's standard workflow notification email reports run status and links to
the workflow run; arbitrary job-summary content is not guaranteed to appear in
the email body. Enable successful Actions email notifications if every
scheduled run should produce an email, then use its run link to open the change
report and annotations.

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

The optional patient-level clinical SQLite also uses separate on-demand paths:

```text
cache/clinical_metadata.sqlite
cache/clinical_metadata_manifest.json
```

Users or agents can override the clinical SQLite path with:

```bash
export TCIA_CLINICAL_METADATA_DB=/path/to/clinical_metadata.sqlite
```

## Refresh Local Metadata

End users do not need to reinstall the skill just to receive newer TCIA metadata. Skill code/instructions and snapshot data are separate.

- Reinstall or update the skill only when the skill instructions or scripts changed.
- Verify skill code, then refresh the manifest-pinned V2 research core:

```bash
python scripts/tcia_freshness.py check
python scripts/tcia_v2_bundle.py install --profile research_core
```

The freshness helper first fetches `skill_version.json` from GitHub `main` and compares its version plus per-file hashes with the installed `SKILL.md`, agent metadata, references, scripts, and MCP files. If code differs, it exits with `update_required` and does not overwrite the installed skill. This deliberate stop prevents an old script from silently operating against a newer artifact schema and preserves the user's authority over skill installation or replacement.

When the skill is current, the bundle installer compares the remote V2
fingerprint with the installed state, verifies the top-level and component
contracts, and stages changed assets. It replaces installed files only after
compressed and decompressed hashes plus SQLite integrity checks pass. Run this
preflight at the start of each discovery task; having a local database is not
evidence that it is still the newest published artifact.

Install only the additional profiles needed for the task:

```bash
python scripts/tcia_v2_bundle.py install --profile research_detail
python scripts/tcia_v2_bundle.py install --profile audit_support
```

The individual `python scripts/tcia_snapshot.py ensure`, sidecar `ensure`, and
`tcia_freshness.py ensure` commands remain available for legacy compatibility,
maintainers, and troubleshooting. New integrations should start with the V2
bundle contract so components cannot drift across releases.

If GitHub cannot be reached, freshness is unverified. Do not claim that cached results are current. Report the local manifest's `generated_at_utc` and ask whether the user wants to continue offline with potentially outdated metadata.

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

For patient-level clinical metadata, use:

```bash
python scripts/tcia_clinical_metadata.py ensure
```

This downloads and verifies only the optional clinical release assets. It is
not run by the base snapshot refresh command.

## Skill Code Versioning

`skill_version.json` is the machine-readable code/instruction manifest. It includes a human-readable skill version, update timestamp, and SHA-256 hash for each operational skill file. After changing `SKILL.md`, agent metadata, references, scripts, or MCP files, bump and regenerate it:

```bash
python scripts/tcia_skill_version.py generate --version YYYY.MM.DD.N
python scripts/tcia_skill_version.py check
```

The validation workflow checks this manifest on pushes and pull requests. A mismatch catches partial installations and repository changes made without regenerating the manifest. Maintainers should still bump the human-readable version for every operational change. Installed copies compare their local manifest with the current GitHub `main` manifest; they report an update requirement but never modify themselves automatically.

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
| `agent_dataset_versions.jsonl` | Plain-text matched version-history export for web LLMs and browse tools that cannot decompress gzip. |
| `agent_dataset_v1_releases.jsonl` | Plain-text first-release date export for web LLMs and browse tools that cannot decompress gzip. |
| `agent_datasets.jsonl.gz` | General flattened dataset/access discovery from `agent_dataset_access_summary`. |
| `agent_current_downloads.jsonl.gz` | Current WordPress download records from `agent_current_downloads`. |
| `agent_dataset_versions.jsonl.gz` | Matched `/api/v2/versions` records from `agent_dataset_versions`. |
| `agent_dataset_v1_releases.jsonl.gz` | Best available dataset v1 release dates from `agent_dataset_v1_releases`. |

Direct release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/tcia_snapshot.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/tcia_snapshot_manifest.json`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_datasets.jsonl`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_current_downloads.jsonl`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_dataset_versions.jsonl`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_dataset_v1_releases.jsonl`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_datasets.jsonl.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_current_downloads.jsonl.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_dataset_versions.jsonl.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_dataset_v1_releases.jsonl.gz`

Optional NIfTI release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/nifti_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/nifti_metadata_manifest.json`

Optional pathology release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/pathology_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/pathology_metadata_manifest.json`

Optional controlled-access release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/controlled_access_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/controlled_access_metadata_manifest.json`

Optional patient-level clinical release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/clinical_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/clinical_metadata_manifest.json`

When an environment has no SQLite execution path, prefer these generic release exports before considering any live API. Use plain `.jsonl` for web LLM browse tools that cannot decompress gzip, and `.jsonl.gz` for local or connector tools that can. They are intentionally table-shaped rather than prompt-specific precomputed answer files. For MCP guidance, see `references/mcp-and-web-llms.md`.

## WordPress Version Tables

The base snapshot includes `/api/v2/versions` records in `wordpress_versions`, expanded to one row per related Collection or Analysis Result short title. Version records are matched to current datasets in `agent_dataset_versions` using exact short titles when possible and a normalized key that strips punctuation and case when legacy version records differ from current Collection Manager short titles.

Use `agent_dataset_v1_releases` for first-release timelines. It prefers matched version 1 rows from `/api/v2/versions`; only when no matched v1 row exists and the current dataset is still version 1 does it fall back to the current Collection or Analysis Result `date_updated` value. That fallback is useful for still-v1 records but should not be used to reconstruct older v1 release dates for datasets now on later versions.

## Optional Patient-Level Clinical SQLite

The clinical SQLite combines direct official public TCIA clinical artifacts,
public TCIA-linked external clinical sources with manifest-validated
identities, IDC collection clinical tables, strict TCIA-matched CDA
enrichment, and sparse DICOM fallback values. It preserves raw source rows,
IDC dictionaries, imaging linkage, and conflicting facts while materializing
convenient resolved subject rows with direct TCIA > linked external > IDC
clinical > CDA > DICOM precedence. When the corresponding patient-level fact
is absent, one non-generic WordPress Collection cancer type/location may
provide a lower-priority diagnosis/site fallback marked as a dataset-scope
inference.

Bootstrap CDA/DICOM locally from the existing prototype, publish the resulting
clinical assets once, and let scheduled GitHub Actions reuse that seed.
Scheduled runs refresh the base snapshot, reuse direct artifacts whose
Collection Manager signature is unchanged, reuse IDC tables when the IDC
version is unchanged, fully rebuild IDC clinical tables on a version change,
fetch small linked external sources on every run, and carry prior CDA/DICOM
sources forward. Manual workflow inputs can force
either direct official artifacts or IDC clinical tables to be fetched again.
WordPress dataset-scope inference is inexpensive and is always regenerated
from the fresh base snapshot rather than reused from the previous sidecar.
Collections containing `screen*` with one otherwise-eligible diagnosis label
and no non-cancer designation are placed in the clinical review queue. Their
Collection-level diagnosis and site fallbacks are suppressed, while any
patient-level source facts continue through normal precedence.

Do not perform the full CDA harvest twice daily. Refresh the local seed after
relevant CDA releases or material IDC subject-inventory changes, then upload
the refreshed clinical assets. See `references/clinical.md`.

## Optional NIfTI SQLite

The NIfTI SQLite is a file-grain metadata layer mined from visible, non-controlled current NIfTI download records, companion spreadsheets, root `.sums` files, and accepted Aspera listings. It is too large and too specialized to bundle with the normal snapshot refresh.

Use `references/nifti.md` for table details and examples. The scheduled GitHub Action should not rebuild the NIfTI database by default. Instead, it compares the current snapshot's visible non-controlled NIfTI download signature and expected optional SQLite schema version against `nifti_metadata_manifest.json`, then emits a warning if a manual NIfTI refresh is needed. A refresh should use the fresh base snapshot so WordPress download titles and `agent_nifti_*` views are current.

## Optional Pathology SQLite

The pathology SQLite is a package/download metadata layer for visible, non-controlled current pathology Aspera download records. It contains Collection Manager download scope, PathDB crosswalk rows for enrichment/disparity review, curator-facing disparity rows, and package/file-object tables populated from Aspera browse output or root `.sums` inventories. `normalized_file_rows_available` means imported Aspera package rows have been normalized. `not_imported` means Aspera package inventory rows are not available for that dataset in the release. The legacy `pathdb_file_objects_available` status can appear only in locally built or older SQLite files that opted into PathDB file-object seeding.

Use `references/pathology.md` for table details and examples. The scheduled GitHub Action validates pathology metadata from the fresh snapshot but does not publish a pathology release by default. Publishing is tied to a manual workflow dispatch that refreshes the Aspera package inventory with `scripts/tcia_pathology_aspera_inventory.py` and passes it with `--package-inventory-tsv`.

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
