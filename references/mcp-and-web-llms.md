# MCP And Web LLM Access

Use this reference when a user asks how web-based LLMs, hosted agents, or non-skill environments should access TCIA metadata.

## Problem

Web LLMs can usually read GitHub files, but many cannot install a skill, run Python, download and query SQLite, or install `idc-index`. When they only see `SKILL.md` and helper scripts, they may fall back to Google or live WordPress API searches. That is not the intended end-user path.

## Preferred Data Surfaces

Use these sources in order:

1. A hosted read-only MCP server backed by the latest TCIA SQLite snapshot.
2. The GitHub Release SQLite snapshot, if the environment can download and query SQLite.
3. The GitHub Release web exports, if the environment can fetch and inspect JSON/JSONL but cannot run SQLite.
4. Live source APIs only for maintainers building or debugging the snapshot, not for normal dataset discovery.

The release assets are:

- `tcia_snapshot.sqlite.gz`: authoritative snapshot for local SQL and MCP backends.
- `tcia_snapshot_manifest.json`: schema version, hashes, counts, and export metadata.
- `agent_datasets.jsonl.gz`: flattened dataset/access rows from `agent_dataset_access_summary`.
- `agent_current_downloads.jsonl.gz`: current WordPress download rows from `agent_current_downloads`.
- `controlled_access_datasets.json`: visible controlled or mixed-access datasets.
- `dicom_annotation_index.json`: visible DICOM annotation/result download records.

## Remote MCP Shape

A TCIA MCP server should be public or otherwise reachable by the hosted LLM product that will call it. Keep it read-only and snapshot-backed. Do not expose arbitrary shell, unrestricted SQL, or live WordPress scraping.

Recommended tools:

- `search_datasets(filters)`: query visible TCIA Collections and Analysis Results by cancer type, body site, modality, access level, DOI, program, and free text.
- `get_dataset(short_title)`: return one dataset with access/license, DOI, page link, counts, summary, and current downloads.
- `get_current_downloads(short_title, filters)`: return current download records filtered by modality, download type, file type, access level, or annotation labels.
- `find_controlled_access_datasets(modalities, requires_annotations, include_mixed)`: answer controlled/mixed discovery questions without requiring the model to write SQL.
- `summarize_access(short_title)`: split open, open-noncommercial, controlled, and mixed downloads and include the TCIA controlled-access policy link when needed.
- `find_dicom_annotations(filters)`: return DICOM annotation/result downloads, with TCIA provenance and access caveats.
- `idc_series_summary(short_title_or_series_uids)`: optional IDC/idc-index-backed lookup for public DICOM only after TCIA provenance and license checks.

For example, a web model asked to "summarize all controlled access datasets that include CT, PET, and annotation data" should call a typed tool such as:

```text
find_controlled_access_datasets(
  modalities=["CT", "PT"],
  requires_annotations=true,
  include_mixed=true
)
```

The MCP response should include short title, title, dataset type, DOI, TCIA page, resolved access level, matching download labels, controlled download titles, and access route guidance. For `mixed`, split controlled and noncontrolled downloads in the response.

## DICOM Routing

For public DICOM metadata and series-level details, route through IDC/idc-index after TCIA provenance and access/license status are confirmed by the snapshot. Do not ask hosted LLMs to query live WordPress for DICOM series/file details.

For controlled-access DICOM, do not generate public IDC/NBIA download or viewer routes. Return TCIA controlled-access policy guidance and, when useful, Data Retriever manifest guidance for later authorized use.

## Static Export Guidance

If MCP is unavailable, web LLM prompts should point to the release exports explicitly. Suggested user prompt wording:

```text
Use the latest release assets from https://github.com/kirbyju/tcia-query-skill/releases/tag/tcia-snapshot-latest. Prefer controlled_access_datasets.json and dicom_annotation_index.json for this question. Do not query the live TCIA WordPress API.
```

The static exports are intentionally redundant with SQLite views. They exist so web-only LLMs have a compact data plane even when they cannot install the skill or execute local code.
