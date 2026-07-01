# TCIA Query Skill

`tcia-query-skill` is an agent skill for helping users find, verify, cite, and access datasets published by [The Cancer Imaging Archive (TCIA)](https://www.cancerimagingarchive.net/about-the-cancer-imaging-archive-tcia/).

The skill is designed to hide the complexity of TCIA's multi-system data ecosystem. It treats TCIA's WordPress Collection Manager as the dataset publication source of truth, treats TCIA's Publications EndNote XML as the verified bibliography of manuscripts written about TCIA data, uses a local SQLite snapshot as the normal agent-friendly query layer for datasets, then routes users to the right downstream system for the data they need.

## What Is TCIA?

The Cancer Imaging Archive is an NCI-supported service that de-identifies and hosts a large archive of cancer medical imaging data. TCIA datasets are organized into collections, usually around a disease, imaging modality, data type, trial, or research focus. TCIA primarily hosts DICOM radiology imaging, but it also connects users with digital pathology, clinical data, genomics, treatment details, expert annotations, segmentations, analysis results, and other supporting data when available.

TCIA data can live across several access systems and metadata layers, including:

- The base SQLite metadata snapshot and optional SQLite metadata assets published by this repository
- TCIA WordPress Collection and Analysis Result pages
- IDC / `idc-index` for many public DICOM datasets
- CTDC for Biobank controlled-access face datasets
- General Commons for some other controlled-access TCIA face datasets
- PathDB and the optional pathology SQLite for non-DICOM histopathology metadata
- Optional NIfTI and controlled-access SQLite assets for file-grain metadata
- DataCite for DOI, citation, version, and derived-data relationships
- TCIA Publications EndNote XML for verified manuscripts written about TCIA data
- IBM Aspera packages for some large non-DICOM downloads

This skill helps an agent decide which system to use and how to explain the result clearly.

## What Is An Agent Skill?

An agent skill is a portable bundle of instructions, references, and helper scripts that an AI agent can load when a task matches a domain. In this repository, [SKILL.md](./SKILL.md) is the main agent-facing entry point.

This skill tells an agent how to:

- Confirm whether a dataset is TCIA-published.
- Query the local SQLite snapshot for routine discovery, access/license metadata, download labels, PathDB slide metadata, and TCIA DOI records.
- Use optional SQLite assets for file-grain NIfTI metadata, public controlled-access manifest/spreadsheet metadata, and pathology Aspera package metadata when needed.
- Use TCIA's Publications EndNote XML, not DataCite, for peer-reviewed manuscripts written about TCIA data.
- Ignore hidden WordPress records unless TCIA staff explicitly request them.
- Use snapshot text fields for abstracts and descriptions.
- Classify open versus controlled access from license metadata.
- Identify Creative Commons NonCommercial datasets without mistaking them for controlled access.
- Prefer IDC/idc-index over NBIA for public DICOM downloads.
- Build browser visualization guidance for open-access DICOM through IDC viewers and public non-DICOM PathDB slides through caMicroscope.
- Return viewer URLs as links instead of trying to launch browser automation.
- Ask users whether they want direct agent downloads or portable TCIA Data Retriever CSV manifests.
- Route users to IDC, CTDC, General Commons, PathDB, DataCite, WordPress downloads, TCIA Data Retriever manifests, or Aspera.
- Point controlled-access users to TCIA's current access policy.
- Start DOI, citation, version, and related-work questions from DataCite, then confirm TCIA publication/visibility in WordPress.
- Start publication-mining questions from `https://cancerimagingarchive.net/endnote/Pubs_basedon_TCIA.xml`.

The `references/` directory contains focused guidance the agent can load when needed, while `scripts/` contains Python helpers for snapshot refresh, snapshot querying, optional metadata assets, manifests, and viewer URL construction.

## Repository Layout

```text
tcia-query-skill/
+-- SKILL.md
+-- README.md
+-- .github/
|   +-- workflows/
|       +-- update-snapshot.yml
+-- agents/
|   +-- openai.yaml
+-- references/
|   +-- aspera.md
|   +-- cda.md
|   +-- controlled-access.md
|   +-- datacite-relationships.md
|   +-- general-commons-graphql.md
|   +-- idc-dicom-downloads.md
|   +-- mcp-and-web-llms.md
|   +-- nifti.md
|   +-- pathdb.md
|   +-- pathology.md
|   +-- publications.md
|   +-- routing.md
|   +-- schema.md
|   +-- snapshots.md
|   +-- visualization.md
+-- scripts/
    +-- datacite_related.py
    +-- datacite_tcia_dois.py
    +-- general_commons_studies.py
    +-- idc_viewer_urls.py
    +-- pathdb_metadata.py
    +-- tcia_controlled_access_metadata.py
    +-- tcia_create_data_retriever_csv.py
    +-- tcia_manifest_series_uids.py
    +-- tcia_nifti_metadata.py
    +-- tcia_pathology_metadata.py
    +-- tcia_publications.py
    +-- tcia_snapshot.py
    +-- tcia_wordpress_search.py
+-- cache/
    +-- *.sqlite and manifests created or refreshed locally
```

The `scripts/` directory also contains maintainer helpers for harvesting NIfTI metadata, checking Aspera package inventories, and rebuilding release assets. Routine users usually only need the scripts shown above.

## Example Uses

Ask an agent using this skill questions like:

- "Find TCIA datasets with breast MRI and tell me how to access them."
- "Download the DICOM series from this legacy TCIA `.tcia` manifest using IDC."
- "Create a TCIA Data Retriever CSV manifest for these Series Instance UIDs."
- "Create an OHIF v3 viewer link for this public TCIA DICOM series."
- "Open this public PathDB slide in caMicroscope."
- "Can I preview this controlled-access dataset in a browser?"
- "Is this dataset open access or controlled access?"
- "Which TCIA datasets have non-DICOM pathology data in PathDB?"
- "Which peer-reviewed TCIA publications study radiology plus genomics, pathology, or proteomics?"
- "Find papers using this TCIA dataset DOI."
- "Show me datasets related to this TCIA DOI, including derived Zenodo records."
- "I am TCIA staff; include hidden staged records in the output."
- "How do I request access to a controlled-access TCIA dataset?"

## Installing Or Using The Skill

Different agent tools handle skills differently. The core requirement is that the tool can read [SKILL.md](./SKILL.md) and, ideally, run local helper scripts from `scripts/`.

Examples:

- **OpenAI Codex**: Install the GitHub repository as a Codex skill, or clone it into a local Codex skills directory so Codex can discover `SKILL.md`.
- **Claude in a browser**: Start a chat with Claude and prompt it with:

  ```text
  Let's create a skill together using your skill-creator skill. I would just like to configure Claude to use [https://github.com/kirbyju/tcia-query-skill/blob/main/SKILL.md](https://github.com/kirbyju/tcia-query-skill/blob/main/SKILL.md)
  ```

  Then follow Claude's instructions to finish the setup. If Claude asks for source files, point it to this repository so it can use `references/` and `scripts/` when the product supports them.

- **Claude Code / Claude Desktop**: Add this repository as a project knowledge/source folder or adapt `SKILL.md` into a Claude skill-style instruction file. Local script execution and SQLite refresh support depend on the Claude product and environment.
- **Cursor, Cline, Roo Code, Continue, OpenHands, or similar coding agents**: Clone the repo and tell the agent to use `SKILL.md` as the task guide. These tools can usually read the references and run the Python helper scripts.
- **Custom agents**: Load `SKILL.md` as the primary system/domain instruction, then load files from `references/` on demand. Permit script execution so the agent can refresh/query the SQLite snapshot and create manifests.
- **SQLite-aware agents**: Mount `cache/tcia_snapshot.sqlite` directly and prefer the views documented in [references/schema.md](./references/schema.md).
- **Web-based LLMs without local execution**: Use the latest GitHub Release SQLite snapshot when possible, or the generic JSONL exports `agent_datasets.jsonl` and `agent_current_downloads.jsonl`. Compressed `.jsonl.gz` copies are also published for tools that can decompress gzip. See [references/mcp-and-web-llms.md](./references/mcp-and-web-llms.md).
- **MCP-capable hosted agents**: Connect to a read-only MCP server backed by the published SQLite snapshot or release exports. The MCP server should expose typed search/download-summary tools, not live WordPress scraping.

For non-Codex tools, this repository may not be "installed" automatically as a native skill. It can still be used as structured agent guidance.

## SQLite Snapshot

Routine discovery should use the local SQLite snapshot, not live public API calls. The published snapshot is built twice daily by GitHub Actions at **7:17 AM and 7:17 PM America/New_York** and released as:

- `tcia_snapshot.sqlite.gz`
- `tcia_snapshot_manifest.json`
- `agent_datasets.jsonl`
- `agent_current_downloads.jsonl`
- `agent_datasets.jsonl.gz`
- `agent_current_downloads.jsonl.gz`

Direct release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/tcia_snapshot.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/tcia_snapshot_manifest.json`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_datasets.jsonl`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_current_downloads.jsonl`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_datasets.jsonl.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/agent_current_downloads.jsonl.gz`

Optional NIfTI file-grain metadata is published separately on the same release tag when available:

- `nifti_metadata.sqlite.gz`
- `nifti_metadata_manifest.json`

Optional pathology Aspera package/download metadata is published separately on the same release tag when available:

- `pathology_metadata.sqlite.gz`
- `pathology_metadata_manifest.json`

Optional controlled-access public manifest/spreadsheet metadata is also published separately on the same release tag when available:

- `controlled_access_metadata.sqlite.gz`
- `controlled_access_metadata_manifest.json`

Optional direct release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/nifti_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/nifti_metadata_manifest.json`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/pathology_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/pathology_metadata_manifest.json`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/controlled_access_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/controlled_access_metadata_manifest.json`

The base snapshot and controlled-access metadata are checked by the scheduled workflow. Pathology metadata can include a more expensive Aspera package inventory and is refreshed by maintainers when that workflow is dispatched with pathology inventory enabled. NIfTI metadata is maintained as an optional on-demand asset, with scheduled drift checks warning maintainers when it may need refresh.

These optional SQLite files are **not** downloaded during skill install and are **not** downloaded by `python scripts/tcia_snapshot.py ensure`. They expose `agent_*` views for routine use. Users who need NIfTI file-level metadata can fetch it on demand:

```bash
python scripts/tcia_nifti_metadata.py ensure
```

Users who need public pathology Aspera package/download scope, Aspera-derived package file inventory, and PathDB crosswalk/disparity metadata can fetch it on demand:

```bash
python scripts/tcia_pathology_metadata.py ensure
```

Users who need controlled-access file-grain public metadata, `drs_uri` manifest rows, or IDC-shaped radiology indexes can fetch it on demand:

```bash
python scripts/tcia_controlled_access_metadata.py ensure
```

See [references/nifti.md](./references/nifti.md) for the NIfTI table guide and examples.
See [references/pathology.md](./references/pathology.md) for the pathology table guide and package inventory status notes.
See [references/controlled-access.md](./references/controlled-access.md) for the controlled-access table guide and policy guidance.

After installing or cloning the skill, refresh local metadata from the latest release:

```bash
python scripts/tcia_snapshot.py ensure
```

This updates `cache/tcia_snapshot.sqlite` only when the published snapshot data or schema changed. End users do **not** need to reinstall the skill just to get newer metadata; reinstall or update the skill only when the instructions or scripts changed.

If a dataset appears to be missing, the snapshot may not include the newest TCIA metadata yet. Try again after the next scheduled snapshot run has had time to finish, then rerun `python scripts/tcia_snapshot.py ensure`.

## Helper Scripts

Most routine helper scripts use Python's standard library. Discovery scripts query the local SQLite snapshot and ask you to run `scripts/tcia_snapshot.py ensure` if the snapshot is missing. Optional metadata build commands may need the packages listed below.

```bash
python scripts/tcia_snapshot.py ensure
python scripts/tcia_snapshot.py info
python scripts/tcia_nifti_metadata.py ensure
python scripts/tcia_nifti_metadata.py datasets --limit 20
python scripts/tcia_nifti_metadata.py derived --collection BCBM-RadioGenomics --with-sources
python scripts/tcia_pathology_metadata.py ensure
python scripts/tcia_pathology_metadata.py datasets --limit 20
python scripts/tcia_pathology_metadata.py downloads --collection CPTAC-CCRCC
python scripts/tcia_pathology_metadata.py pathdb --collection CPTAC-STAD --limit 10
python scripts/tcia_pathology_metadata.py disparities
python scripts/tcia_controlled_access_metadata.py ensure
python scripts/tcia_controlled_access_metadata.py datasets --limit 20
python scripts/tcia_controlled_access_metadata.py downloads --collection CMB-MEL
python scripts/tcia_controlled_access_metadata.py files --collection CMB-MEL --limit 10
python scripts/tcia_wordpress_search.py --query breast --limit 10
python scripts/tcia_wordpress_search.py --short-title EAY131 --json
python scripts/tcia_wordpress_search.py --short-title 4D-Lung --json
python scripts/tcia_manifest_series_uids.py ./legacy_manifest.tcia --out series_uids.txt
python scripts/tcia_create_data_retriever_csv.py --uids-file series_uids.txt --out manifest.csv
python scripts/idc_viewer_urls.py ohif-v3 --study-uid <StudyInstanceUID> --series-uid <SeriesInstanceUID>
python scripts/idc_viewer_urls.py slim --study-uid <StudyInstanceUID> --series-uid <SeriesInstanceUID>
python scripts/idc_viewer_urls.py volview --crdc-series-uuid <crdc_series_uuid>
python scripts/general_commons_studies.py --study-acronym TCGA-GBM --counts
python scripts/datacite_tcia_dois.py --query breast --limit 10
python scripts/datacite_tcia_dois.py --doi 10.7937/4qad-4280 --json
python scripts/tcia_publications.py --fetch --query radiogenomics --limit 10
python scripts/tcia_publications.py --dataset-doi 10.7937/K9/TCIA.2016.RNYFUYE9 --json
python scripts/pathdb_metadata.py --collection CPTAC-STAD --summary
python scripts/pathdb_metadata.py --collection CPTAC-STAD --limit 5
```

Developers improving the snapshot builder can use `python scripts/tcia_snapshot.py build` to query the source APIs and create release assets. End-user discovery should use the release snapshot or release exports.

```bash
python scripts/tcia_snapshot.py build \
  --out dist/tcia_snapshot.sqlite \
  --gzip-out dist/tcia_snapshot.sqlite.gz \
  --manifest-out dist/tcia_snapshot_manifest.json \
  --exports-dir dist
python scripts/tcia_snapshot.py validate --db dist/tcia_snapshot.sqlite --exports-dir dist
```

The validation step checks that the SQLite file contains the documented agent-facing views and that the web export assets were generated.

The scheduled workflow also rebuilds the controlled-access metadata asset from the freshly built snapshot and public WordPress manifest/spreadsheet URLs:

```bash
python scripts/tcia_controlled_access_metadata.py build \
  --snapshot-db dist/tcia_snapshot.sqlite \
  --out dist/controlled_access_metadata.sqlite \
  --artifact-dir dist/controlled_access_source_artifacts \
  --gzip-out dist/controlled_access_metadata.sqlite.gz \
  --manifest-out dist/controlled_access_metadata_manifest.json \
  --replace
python scripts/tcia_controlled_access_metadata.py validate --db dist/controlled_access_metadata.sqlite
```

When maintainers need to refresh pathology Aspera package inventory, the scheduled workflow can be manually dispatched with `refresh_pathology_inventory=true`; it runs `scripts/tcia_pathology_aspera_inventory.py`, then rebuilds and validates `pathology_metadata.sqlite.gz`.

## Publications About TCIA Data

For peer-reviewed manuscripts written about TCIA datasets, use TCIA's verified publications source:

https://cancerimagingarchive.net/endnote/Pubs_basedon_TCIA.xml

The EndNote XML is the authority for publication-mining tasks such as finding papers about a dataset, summarizing methods or hypotheses studied using TCIA data, or counting papers by topic. DataCite records describe TCIA dataset DOIs and versions; they are not the verified bibliography of papers that used TCIA datasets.

Use the helper script to fetch and search the library:

```bash
python scripts/tcia_publications.py --fetch --query "radiology genomics" --limit 20
python scripts/tcia_publications.py --dataset-doi 10.7937/K9/TCIA.2016.RNYFUYE9 --json
```

## Recommended Python Packages

Most bundled helper scripts use Python's standard library, so the skill can run basic snapshot and manifest workflows without extra packages. The controlled-access metadata builder needs `pandas`, `openpyxl`, and `xlrd` to read public WordPress spreadsheet artifacts. Maintainer pathology package inventory refreshes also need IBM Aspera CLI. For best results on download, viewer, DICOM, and CDA enrichment tasks, install the domain packages in the same Python environment used by the local agent:

```bash
python -m pip install --upgrade pandas openpyxl xlrd tcia_utils idc-index pydicom cdapython
```

Agents should check whether these packages are available before writing custom code, ask before installing missing packages, and prefer:

- `tcia_utils` for TCIA-specific helper APIs when maintaining the snapshot or doing explicit source-system checks.
- `idc-index` for IDC lookup, public DICOM downloads, viewer URLs, cloud-storage URLs, and Series Instance UID workflows.
- `pydicom` for local DICOM header/metadata inspection.
- `cdapython` for CDA subject/file summaries and cross-commons enrichment.

For public DICOM downloads, use IDC/idc-index first. Existing TCIA `.tcia` manifests can be parsed into Series Instance UID allowlists with `scripts/tcia_manifest_series_uids.py`, then looked up and downloaded through IDC. Before downloading, agents should ask whether the user wants files downloaded directly in the active environment or a portable CSV manifest created for TCIA Data Retriever. New manifests should be CSV/TSV/XLSX-compatible, not legacy `.tcia`, unless the user explicitly asks for the legacy NBIA-era format. NBIA should be fallback-only for DICOM data that cannot be found in IDC/idc-index. If NBIA fallback is needed, use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.

For public DICOM series/file details, agents should use IDC/idc-index after TCIA provenance and access/license status have been confirmed from the snapshot. They should not query live WordPress APIs for DICOM details during normal end-user discovery.

For new TCIA Data Retriever CSV manifests, the route is selected by column header. Use one preferred route header only: `SeriesInstanceUID` for public DICOM through IDC first/NBIA fallback, `imageUrl` for PathDB/direct public files, or `drs_uri` for controlled-access files when official WordPress, CTDC, or General Commons manifests provide DRS URIs.

For public DICOM visualization before download, use IDC viewer capabilities. OHIF v3 is preferred for radiology, SliM is used for DICOM slide microscopy (`SM`), and VolView can be used when IDC metadata provides a public S3 series folder or CRDC series UUID. Agents should provide viewer URLs for users to open in their regular browser, not install browser automation just to display examples. Controlled-access data cannot be previewed in a public browser viewer before download, regardless of file format.

For public non-DICOM histopathology slides in PathDB, use caMicroscope viewer URLs built from the PathDB cohort-builder CSV `camic_id`, for example `https://pathdb.cancerimagingarchive.net/caMicroscope/apps/mini/viewer.html?mode=pathdb&slideId=314525`. The URL parameter is named `slideId`, but it expects numeric `camic_id`, not CSV `slide_id`. The PathDB helper adds a `camicroscope_url` field to slide-level rows.

When the same non-DICOM pathology data are available through both PathDB and an Aspera package, agents should explain the provenance difference. Aspera packages are the original submitter-provided data; PathDB copies may be converted or reformatted for browser-based pathology viewing. Recommend Aspera for analyses requiring the exact submitted files.

## Controlled Access

The skill classifies controlled access from WordPress license metadata, not from deprecated collection/page accessibility fields.

- Creative Commons licenses are treated as open access.
- Creative Commons NonCommercial licenses are treated as open access with a noncommercial-use restriction.
- NIH Controlled Data Access Policy or TCIA Restricted license metadata is treated as controlled access.

For controlled-access datasets, users should consult TCIA's current policy page:

https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/

That page explains how to request access, create a JSON API key after approval, and configure TCIA Data Retriever to use that key.

Agents should not directly download controlled data. For controlled data, provide the policy link and, when useful, portable TCIA Data Retriever manifest guidance for later authorized use.

Biobank controlled-access face data are now available through CTDC using the manifests and download/view links on the relevant WordPress pages. Users must request access to dbGaP study `phs002192` for these images. Use the optional controlled-access SQLite for public CTDC/General Commons manifest and metadata spreadsheet rows when file-grain metadata are needed.

## Notes

This repository is meant to help agents find and explain TCIA data. It does not grant access to restricted datasets, replace TCIA's official policies, or provide medical/legal advice about dataset suitability.
