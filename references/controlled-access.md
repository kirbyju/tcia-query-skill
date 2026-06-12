# Controlled Access

Use this reference whenever WordPress license metadata indicates a TCIA dataset is controlled/restricted, or when a user asks about controlled-access data.

## Policy Link

Always point users to the latest TCIA policy page:

```text
https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/
```

That page contains current instructions for requesting access to controlled datasets. It also explains how approved users can create a JSON API key and configure the TCIA Data Retriever to use that key.

## When To Alert Users

Use license metadata as the source of truth. Do not use `collection_page_accessibility` or `result_page_accessibility` to classify controlled access; those fields are being phased out.

Alert users when license metadata indicates NIH Controlled Data Access, TCIA Restricted, controlled access, restricted access, dbGaP, DAC approval, data usage agreements, or other authorization requirements.

Do not alert as controlled access when license metadata is Creative Commons. Creative Commons licenses mean open access. Creative Commons NonCommercial licenses are open access with a noncommercial-use restriction; call out the noncommercial restriction, but do not send users to controlled-access request instructions unless another license in the same dataset/download metadata is controlled/restricted.

## Required User-Facing Guidance

Before download/API instructions, say clearly that:

- The dataset is controlled access, not open access.
- The decision came from license metadata, not the deprecated collection/page accessibility field.
- Metadata may be visible even when files cannot be downloaded without authorization.
- The user should review the TCIA NIH Controlled Data Access Policy page for current request, approval, API key, and TCIA Data Retriever configuration instructions.
- The agent should not directly download controlled data. Provide policy guidance and, when useful, a portable manifest for later authorized use with TCIA Data Retriever.

Do not invent approval requirements, timelines, or eligibility rules. Link to the policy page and summarize only what has been verified from current TCIA pages or WordPress metadata.

## Optional Controlled-Access SQLite

Use `scripts/tcia_controlled_access_metadata.py` when the user needs public file-grain metadata for controlled-access TCIA downloads, including:

- `drs_uri` values or file IDs for TCIA Data Retriever manifest guidance.
- Public manifest rows from WordPress download URLs.
- Public metadata spreadsheet rows from WordPress `download_metadata` fields.
- Route classification for General Commons versus CTDC.
- IDC-parquet-shaped radiology metadata columns for controlled datasets.
- Review rows explaining manifest/spreadsheet mismatches.

Fetch it on demand:

```bash
python scripts/tcia_controlled_access_metadata.py ensure
python scripts/tcia_controlled_access_metadata.py datasets --limit 20
python scripts/tcia_controlled_access_metadata.py files --collection CMB-MEL --limit 10
```

The SQLite defaults to `cache/controlled_access_metadata.sqlite` and is distributed as:

- `controlled_access_metadata.sqlite.gz`
- `controlled_access_metadata_manifest.json`

The data source is public metadata only. The builder reads current controlled/restricted WordPress download records from the base snapshot, follows public manifest/spreadsheet URLs attached to those records, and stores normalized rows. It does not require a General Commons or CTDC account, does not use the user's TCIA Data Retriever JSON API key, and does not download controlled files.

Important tables:

- `agent_controlled_downloads`: scoped controlled downloads with a stable `download_label` and policy URL.
- `agent_controlled_files`: normalized file-grain rows with lower-snake-case fields such as `series_instance_uid`, `study_instance_uid`, `drs_uri`, and `file_id`.
- `agent_controlled_dataset_summary`: route/dataset summary with policy URL.
- `controlled_downloads`: scoped WordPress download records and route system.
- `wordpress_download_metadata`, `wordpress_download_urls`, `wordpress_search_filters`: WordPress-provided metadata fields and extracted URLs.
- `manifest_rows`: public manifest rows with `drs_uri`, file ID, file name, size, study, participant, sample, and series fields when present.
- `metadata_rows`: public spreadsheet metadata rows.
- `controlled_files`: normalized file-grain rows combining manifest and spreadsheet metadata.
- `radiology_series` and `idc_*` tables: IDC-parquet-shaped indexes for controlled metadata discovery only.
- `controlled_metadata_exceptions`: review rows for incomplete or unmatched metadata.

Useful examples:

```sql
SELECT route_system, short_title, controlled_file_rows,
       participant_ids, patient_ids, series_instance_uids
FROM agent_controlled_dataset_summary
ORDER BY route_system, lower(short_title);
```

```sql
SELECT short_title, file_name, modality, participant_id,
       series_instance_uid, drs_uri
FROM agent_controlled_files
WHERE route_system = 'ctdc'
  AND COALESCE(drs_uri, '') <> ''
LIMIT 25;
```

Do not treat `idc_index` rows in this SQLite as open IDC availability. They are shaped like IDC metadata for alignment with other TCIA optional metadata layers, but access remains controlled and no public IDC/NBIA download or viewer route should be offered.

## Routing Notes

- Controlled-access face datasets: route access questions to the policy page. For Biobank controlled-access face data, use the current WordPress CTDC manifests/download/view links and tell users to request dbGaP study `phs002192`; use the controlled-access SQLite for public manifest/spreadsheet metadata when available. For non-Biobank face datasets, use General Commons metadata for `phs004225` only when WordPress or GC metadata indicate that route.
- NCTN trials or Biobank data: use WordPress license metadata and current TCIA access statements. Biobank controlled-access face data are now available in CTDC through the relevant WordPress manifests/links and require dbGaP study `phs002192`; for other controlled datasets, do not invent CTDC routing unless WordPress identifies it. Use the controlled-access SQLite when WordPress identifies a controlled download route and file-grain public metadata are needed.
- Public subsets of a mixed collection can be described separately from controlled/restricted subsets. For mixed datasets, use `agent_dataset_access_summary` to identify controlled download titles, licenses, IDs, and URLs before answering.
- Web-only agents that cannot query SQLite should filter `agent_datasets.jsonl` from the latest release for `resolved_access_level` values such as `controlled` and `mixed` before attempting any live API lookup. Use `agent_datasets.jsonl.gz` only when the host can decompress gzip.
