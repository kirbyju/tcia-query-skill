# TCIA Query Skill

`tcia-query-skill` is an agent skill for helping users find, verify, cite, and access datasets published by [The Cancer Imaging Archive (TCIA)](https://www.cancerimagingarchive.net/about-the-cancer-imaging-archive-tcia/).

The skill is designed to hide the complexity of TCIA's multi-system data ecosystem. It starts with TCIA's WordPress Collection Manager as the source of truth, then routes users to the right downstream system for the data they need.

## What Is TCIA?

The Cancer Imaging Archive is an NCI-supported service that de-identifies and hosts a large archive of cancer medical imaging data. TCIA datasets are organized into collections, usually around a disease, imaging modality, data type, trial, or research focus. TCIA primarily hosts DICOM radiology imaging, but it also connects users with digital pathology, clinical data, genomics, treatment details, expert annotations, segmentations, analysis results, and other supporting data when available.

TCIA data can live across several access systems, including:

- TCIA WordPress Collection and Analysis Result pages
- IDC / `idc-index` for many public DICOM datasets
- General Commons for controlled-access TCIA face datasets
- PathDB for non-DICOM histopathology metadata
- DataCite for DOI, citation, version, and derived-data relationships
- IBM Aspera packages for some large non-DICOM downloads

This skill helps an agent decide which system to use and how to explain the result clearly.

## What Is An Agent Skill?

An agent skill is a portable bundle of instructions, references, and helper scripts that an AI agent can load when a task matches a domain. In this repository, [SKILL.md](./SKILL.md) is the main agent-facing entry point.

This skill tells an agent how to:

- Confirm whether a dataset is TCIA-published.
- Ignore hidden WordPress records unless TCIA staff explicitly request them.
- Use verbose WordPress metadata for abstracts and descriptions.
- Classify open versus controlled access from license metadata.
- Identify Creative Commons NonCommercial datasets without mistaking them for controlled access.
- Prefer IDC/idc-index over NBIA for public DICOM downloads.
- Build browser visualization guidance for open-access DICOM through IDC viewers and public non-DICOM PathDB slides through caMicroscope.
- Return viewer URLs as links instead of trying to launch browser automation.
- Ask users whether they want direct agent downloads or portable TCIA Data Retriever CSV manifests.
- Route users to IDC, General Commons, PathDB, DataCite, WordPress downloads, or Aspera.
- Point controlled-access users to TCIA's current access policy.

The `references/` directory contains focused guidance the agent can load when needed, while `scripts/` contains small standard-library Python helpers for live metadata checks.

## Repository Layout

```text
tcia-query-skill/
+-- SKILL.md
+-- agents/
|   +-- openai.yaml
+-- references/
|   +-- aspera.md
|   +-- controlled-access.md
|   +-- datacite-relationships.md
|   +-- general-commons-graphql.md
|   +-- idc-dicom-downloads.md
|   +-- pathdb.md
|   +-- routing.md
|   +-- visualization.md
+-- scripts/
    +-- datacite_related.py
    +-- general_commons_studies.py
    +-- idc_viewer_urls.py
    +-- pathdb_metadata.py
    +-- tcia_create_data_retriever_csv.py
    +-- tcia_manifest_series_uids.py
    +-- tcia_wordpress_search.py
```

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
- "Show me datasets related to this TCIA DOI, including derived Zenodo records."
- "I am TCIA staff; include hidden staged records in the output."
- "How do I request access to a controlled-access TCIA dataset?"

## Installing Or Using The Skill

Different agent tools handle skills differently. The core requirement is that the tool can read [SKILL.md](./SKILL.md) and, ideally, run local helper scripts from `scripts/`.

Examples:

- **OpenAI Codex**: Install the GitHub repository as a Codex skill, or clone it into a local Codex skills directory so Codex can discover `SKILL.md`.
- **Claude Code / Claude Desktop**: Add this repository as a project knowledge/source folder or adapt `SKILL.md` into a Claude skill-style instruction file.
- **Cursor, Cline, Roo Code, Continue, OpenHands, or similar coding agents**: Clone the repo and tell the agent to use `SKILL.md` as the task guide. These tools can usually read the references and run the Python helper scripts.
- **Custom agents**: Load `SKILL.md` as the primary system/domain instruction, then load files from `references/` on demand. Permit script execution if the agent is allowed to query live public metadata.

For non-Codex tools, this repository may not be "installed" automatically as a native skill. It can still be used as structured agent guidance.

## Helper Scripts

The helper scripts use Python's standard library and query public metadata endpoints.

```bash
python scripts/tcia_wordpress_search.py --query breast --limit 10
python scripts/tcia_wordpress_search.py --short-title EAY131 --json
python scripts/tcia_wordpress_search.py --short-title 4D-Lung --verbose --json
python scripts/tcia_wordpress_search.py --query lung --workers 6 --limit 10
python scripts/tcia_manifest_series_uids.py ./legacy_manifest.tcia --out series_uids.txt
python scripts/tcia_create_data_retriever_csv.py --uids-file series_uids.txt --out manifest.csv
python scripts/idc_viewer_urls.py ohif-v3 --study-uid <StudyInstanceUID> --series-uid <SeriesInstanceUID>
python scripts/idc_viewer_urls.py slim --study-uid <StudyInstanceUID> --series-uid <SeriesInstanceUID>
python scripts/idc_viewer_urls.py volview --crdc-series-uuid <crdc_series_uuid>
python scripts/general_commons_studies.py --study-acronym TCGA-GBM --counts
python scripts/datacite_related.py 10.7937/TCIA.HMQ8-J677
python scripts/pathdb_metadata.py --collection CPTAC-STAD --summary
python scripts/pathdb_metadata.py --collection CPTAC-STAD --limit 5
```

The WordPress search helper parallelizes v2 pagination with `--workers 4` by default. Use a modest higher value for broad metadata scans, or `--workers 1` for sequential troubleshooting.

## Recommended Python Packages

The bundled helper scripts use Python's standard library, so the skill can still run basic metadata workflows without extra packages. For best results, install the domain packages in the same Python environment used by the local agent:

```bash
python -m pip install --upgrade tcia_utils idc-index pydicom
```

Agents should check whether these packages are available before writing custom code, ask before installing missing packages, and prefer:

- `tcia_utils` for TCIA WordPress, DataCite, PathDB, and related helper APIs.
- `idc-index` for IDC lookup, public DICOM downloads, viewer URLs, cloud-storage URLs, and Series Instance UID workflows.
- `pydicom` for local DICOM header/metadata inspection.

For public DICOM downloads, use IDC/idc-index first. Existing TCIA `.tcia` manifests can be parsed into Series Instance UID allowlists with `scripts/tcia_manifest_series_uids.py`, then looked up and downloaded through IDC. Before downloading, agents should ask whether the user wants files downloaded directly in the active environment or a portable CSV manifest created for TCIA Data Retriever. New manifests should be CSV/TSV/XLSX-compatible, not legacy `.tcia`, unless the user explicitly asks for the legacy NBIA-era format. NBIA should be fallback-only for DICOM data that cannot be found in IDC/idc-index. If NBIA fallback is needed, use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.

For new TCIA Data Retriever CSV manifests, the route is selected by column header. Use one preferred route header only: `SeriesInstanceUID` for public DICOM through IDC first/NBIA fallback, `imageUrl` for PathDB/direct public files, or `drs_uri` for General Commons controlled-access files.

For public DICOM visualization before download, use IDC viewer capabilities. OHIF v3 is preferred for radiology, SliM is used for DICOM slide microscopy (`SM`), and VolView can be used when IDC metadata provides a public S3 series folder or CRDC series UUID. Agents should provide viewer URLs for users to open in their regular browser, not install browser automation just to display examples. Controlled-access data cannot be previewed in a public browser viewer before download, regardless of file format.

For public non-DICOM histopathology slides in PathDB, use caMicroscope viewer URLs built from the PathDB cohort-builder CSV `slide_id`, for example `https://pathdb.cancerimagingarchive.net/caMicroscope/apps/mini/viewer.html?mode=pathdb&slideId=314525`. The PathDB helper adds a `camicroscope_url` field to slide-level rows.

## Controlled Access

The skill classifies controlled access from WordPress license metadata, not from deprecated collection/page accessibility fields.

- Creative Commons licenses are treated as open access.
- Creative Commons NonCommercial licenses are treated as open access with a noncommercial-use restriction.
- NIH Controlled Data Access Policy or TCIA Restricted license metadata is treated as controlled access.

For controlled-access datasets, users should consult TCIA's current policy page:

https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/

That page explains how to request access, create a JSON API key after approval, and configure TCIA Data Retriever to use that key.

## Notes

This repository is meant to help agents find and explain TCIA data. It does not grant access to restricted datasets, replace TCIA's official policies, or provide medical/legal advice about dataset suitability.
