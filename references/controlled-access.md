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
- Possession of a manifest does not grant access. The user must obtain the required dbGaP authorization and manually create their own JSON API key through the process linked from the policy page.
- An agent may invoke TCIA Data Retriever for the user only after the user explicitly requests the controlled download and specifies the readable filesystem path to their own valid JSON key.

Do not invent approval requirements, timelines, or eligibility rules. Link to the policy page and summarize only what has been verified from current TCIA pages or WordPress metadata.

## Authorized Agent Download

Use this workflow when the user asks the agent to download controlled TCIA data:

1. Confirm TCIA provenance and controlled status from current WordPress snapshot license/download metadata.
2. Link `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/` and state that the user must request permission through dbGaP or the access process specified there.
3. Require the user to explicitly state the path to a JSON API-key file they created manually after approval. Do not solicit the key value in chat.
4. Confirm the credential file exists, is readable, parses as a JSON object, and contains a non-empty `api_key`. A `key_id` may also be present. Report only validation status and key names; never print values.
5. Warn if the credential file is broadly readable. Recommend restrictive permissions such as `chmod 600 /path/to/credentials.json` on Unix-like systems, but do not change permissions outside the task scope without authorization.
6. Use an official current WordPress/CRDC manifest. Controlled CSV manifests use `drs_uri`, `File ID`, or `file_id`; keep the original manifest as provenance. If the user asks for a subset, filter by explicit participant, file, or series identifiers and preserve the header and selected source rows in a new scoped manifest.
7. Confirm the exact manifest scope and destination from the request. For a clearly specified small subset and workspace destination, proceed without re-asking. Estimate published size when available.
8. Locate or install the official TCIA Data Retriever only with user approval. Verify CLI support with the installed build or current official documentation.
9. Invoke the CLI by passing the credential **path**, never the key value. Do not copy the credential into the workspace, generated manifests, scripts, logs, environment variables, shell history, or response text.
10. Run the transfer only against the route encoded by the official manifest. Never send the credential to IDC, NBIA, Aspera, arbitrary DRS hosts, or URLs constructed by the agent.
11. Report destination, selected identifiers, counts/sizes, command exit status, and failures without exposing credentials. Preserve partial downloads for resume unless the user asks to remove them.

Current official CLI pattern:

```bash
TCIA_Data_Retriever --cli \
  --input /path/to/official-or-scoped-manifest.csv \
  --output /path/to/destination \
  --auth /path/to/credentials.json \
  --skip-existing
```

The short options `-i` and `-o` are also supported. Use `--server-friendly` for a conservative transfer, `--directory-mode classic` when predictable Collection/Patient/Study/Series folders are useful, and `--verbose` for status. Do not add `--accept-data-policy` unless the user has explicitly asked to accept the policy non-interactively after reviewing it. Avoid `--debug` or persistent logs when they are unnecessary for the task.

The Linux notebook [`TCIA_Linux_Data_Retriever_App.ipynb`](https://github.com/kirbyju/TCIA_Notebooks/blob/main/TCIA_Linux_Data_Retriever_App.ipynb), section 5, demonstrates the same pattern: obtain dbGaP access, download an official CRDC CSV manifest, create a JSON API key manually, and pass its path with `--auth`.

If the user has not supplied a valid credential path, prepare or identify the official manifest and give the policy/CLI steps without attempting the payload transfer. Authentication or authorization failure does not prove the key is malformed; it may be expired, unauthorized for that dbGaP study, or rejected by the routed CRDC service.

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
- Web-only agents that cannot query SQLite may decompress and filter `agent_datasets.jsonl.gz` from the latest V2 release for `resolved_access_level` values such as `controlled` and `mixed`. If the host cannot decompress gzip, use MCP; do not switch to live API lookup or the legacy release.
