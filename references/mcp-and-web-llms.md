# MCP And Web LLM Access

Use this reference when a user asks how web-based LLMs, hosted agents, or non-skill environments should access TCIA metadata.

## Problem

Web LLMs can usually read GitHub files, but many cannot install a skill, run Python, download and query SQLite, or install `idc-index`. When they only see `SKILL.md` and helper scripts, they may fall back to Google or live WordPress API searches. That is not the intended end-user path.

## Preferred Data Surfaces

Use these sources in order:

1. A hosted read-only MCP server backed by the latest validated V2 bundle.
2. The GitHub Release V2 research core, if the environment can download and query SQLite.
3. The two compressed GitHub Release JSONL exports, if the environment can decompress gzip and inspect line-delimited JSON but cannot run SQLite.
4. Live source APIs only for maintainers building or debugging the snapshot, not for normal dataset discovery.

Before using any surface, verify freshness:

1. Fetch `https://api.github.com/repos/kirbyju/tcia-query-skill/contents/skill_version.json?ref=main` and compare its `skill_version` with the installed/local `skill_version.json`. If it differs, load the current skill from GitHub or ask the user to update it before proceeding.
2. Fetch the current release record from `https://api.github.com/repos/kirbyju/tcia-query-skill/releases/tags/tcia-metadata-v2-latest`, then fetch `tcia_metadata_v2_bundle_manifest.json` from that release.
3. Select SQLite or compressed JSONL assets from that same release record. Verify every selected asset against the top-level manifest; for SQLite, also verify the decompressed hash recorded inline under `components`.

Do not treat a previously downloaded SQLite or JSON/JSONL file as current merely because it exists. Do not silently continue when the skill version or release manifest cannot be checked; identify the results as unverified and ask whether offline use is acceptable.

The stable streamlined release has nine payload assets plus the authoritative bundle manifest:

- `tcia_metadata_v2_bundle_manifest.json`: authoritative release fingerprint, profiles, component versions, hashes, sizes, and provenance.
- `tcia_snapshot.sqlite.gz`: authoritative snapshot for local SQL and MCP backends.
- `participant_inventory.sqlite.gz`: compact dataset-scoped participant identity and availability for participant-first search.
- `agent_datasets.jsonl.gz`: compressed flattened dataset/access rows from `agent_dataset_access_summary`.
- `agent_current_downloads.jsonl.gz`: compressed current WordPress download rows from `agent_current_downloads`.
- `public_non_dicom_metadata.sqlite.gz`: unified public non-DICOM file-grain research detail, including NIfTI and pathology discovery.
- `controlled_access_metadata.sqlite.gz`: optional public controlled-access manifest/spreadsheet metadata for CTDC and General Commons routed downloads.
- `clinical_metadata.sqlite.gz`: optional patient-level resolved clinical facts, provenance, and conflicts.
- `public_non_dicom_audit.sqlite.gz`: verbose non-DICOM provenance, specialized source checkpoints, crosswalk evidence, and QC.
- `participant_inventory_audit.sqlite.gz`: verbose participant provenance, linkage evidence, and retained clinical-review material.

Per-component manifests, plain JSONL files, release-timeline JSONL files, and standalone NIfTI/pathology SQLite files are not assets in the streamlined stable release. Their validation metadata is inline in the bundle manifest. Query release timelines from the base snapshot or through MCP/REST. If a web-only host cannot decompress gzip, use MCP rather than falling back to live WordPress or the legacy release.

Direct release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/tcia_metadata_v2_bundle_manifest.json`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/tcia_snapshot.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/participant_inventory.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/agent_datasets.jsonl.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/agent_current_downloads.jsonl.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/public_non_dicom_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/controlled_access_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/clinical_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/public_non_dicom_audit.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/participant_inventory_audit.sqlite.gz`

The two JSONL exports are generic compressed table exports, not prompt-specific precomputed answer files. Decompress and filter `agent_datasets.jsonl.gz` for dataset/access fields and `agent_current_downloads.jsonl.gz` for modality/file/download/annotation labels.

JSONL usage pattern:

1. Resolve the current release and manifest as described above; fetch fresh asset URLs for each task instead of reusing saved JSONL files.
2. Confirm the host can decompress gzip. Otherwise use MCP.
3. Parse one JSON object per line.
4. Treat `short_title` as the join key between dataset and current-download rows.
5. Exclude rows where `hidden` is true unless the user explicitly asks for TCIA staff hidden/staged/retired records.
6. Filter dataset rows by `access_level` or `resolved_access_level`, and filter download rows by `download_types`, `data_types`, and `file_types`.
7. For mixed-access datasets, split controlled and noncontrolled downloads in the answer.

## Remote MCP Shape

A TCIA MCP server should be public or otherwise reachable by the hosted LLM product that will call it. Keep it read-only and snapshot-backed. Do not expose arbitrary shell, unrestricted SQL, or live WordPress scraping.

The default public tool list should expose only supported V2 operations. Do not
advertise the legacy NIfTI/pathology-specific compatibility tools; route those
questions through `find_public_non_dicom_assets`. A self-hosted operator may
enable the legacy surface explicitly while migrating, but the server must not
infer legacy databases from an old cache directory.

Recommended tools:

- `search_participants(filters)`: search canonical dataset-scoped participants and compact availability from the V2 research core.
- `get_participant(participant_key)`: return one participant plus every retained source identifier spelling.
- `get_participant_assets(participant_key, filters)`: return compact holdings and drill-down pointers.
- `get_dataset_participant_coverage(short_title)`: return unlinked dataset assets, source coverage, and linkage-review states.
- `find_public_non_dicom_assets(filters)`: query optional V2 non-DICOM file-grain detail while keeping public DICOM in IDC.
- `search_datasets(filters)`: query visible TCIA Collections and Analysis Results by cancer type, body site, modality, access level, DOI, program, and free text.
- `get_dataset(short_title)`: return one dataset with access/license, DOI, page link, counts, summary, and current downloads.
- `get_current_downloads(short_title, filters)`: return current download records filtered by modality, download type, file type, access level, or annotation labels.
- `get_dataset_versions(short_title)`: return matched `/api/v2/versions` records, including version number, version date, related short title, and match method.
- `get_dataset_v1_releases(filters)`: return first-release dates from `agent_dataset_v1_releases`, including whether the date came from an exact version match, normalized version match, or the still-v1 `date_updated` fallback.
- `find_controlled_access_datasets(modalities, requires_annotations, include_mixed)`: answer controlled/mixed discovery questions without requiring the model to write SQL.
- `get_controlled_access_files(short_title, filters)`: query the optional controlled-access SQLite for public manifest/spreadsheet rows, `drs_uri` values, route system, modality, subject/participant IDs, and Series Instance UIDs.
- `find_clinical_datasets(filters)`: summarize subject, concept, source, conflict, and clinical-only coverage in the optional clinical SQLite.
- `get_clinical_subjects(short_title, filters)`: return resolved image-linked subjects by default, with optional clinical-only rows and explicit dataset-scope inference flags.
- `get_clinical_facts(short_title, filters)`: return long-form facts with source priority, provenance, evidence scope, and inference flags.
- `get_clinical_conflicts(short_title, filters)`: return retained patient/concept disagreements for review before analysis.
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

For controlled-access DICOM, do not generate public IDC/NBIA download or viewer routes. Return TCIA controlled-access policy and Data Retriever manifest guidance. A web LLM may invoke an authorized download only when its host can securely run the official TCIA Data Retriever and the user explicitly supplies a readable JSON-key path; follow `controlled-access.md`. If the host cannot execute local tools or protect credential files, stop at manifest guidance. If file-grain public metadata are needed and the host can query SQLite, use `controlled_access_metadata.sqlite.gz`.

For patient-level clinical queries, confirm the dataset in the base snapshot first, then use `clinical_metadata.sqlite.gz`. Scope identities by `(short_title, subject_id)`, preserve source provenance and conflicts, and distinguish dataset-scope inferred diagnosis/site values from patient-level evidence.

## Static Export Guidance

If MCP is unavailable, web LLM prompts should point to the release exports explicitly. Suggested user prompt wording:

```text
Use the latest manifest-pinned assets from https://github.com/kirbyju/tcia-query-skill/releases/tag/tcia-metadata-v2-latest. Read tcia_metadata_v2_bundle_manifest.json first and select only assets listed there. Prefer research-core tcia_snapshot.sqlite.gz for dataset provenance, access, and release timelines, and participant_inventory.sqlite.gz for participant availability. Fetch research-detail assets only when the question needs file-grain public non-DICOM, controlled, pathology, NIfTI, or clinical metadata. The only static JSONL assets are agent_datasets.jsonl.gz and agent_current_downloads.jsonl.gz; use them only if you can decompress gzip. Otherwise use the snapshot-backed MCP service. Do not query the live TCIA WordPress API.
```

The two static exports are intentionally redundant with SQLite views. They provide a compact data plane for hosts that can decompress gzip but cannot query SQLite. If the host cannot fetch/decompress gzip or parse JSONL, it needs a remote MCP/data connector or user-supplied downloaded files; it should not fall back to broad web search or live WordPress scraping.
