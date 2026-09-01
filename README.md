# TCIA Query Skill

`tcia-query-skill` helps AI agents find, verify, cite, and access datasets
published by [The Cancer Imaging Archive
(TCIA)](https://www.cancerimagingarchive.net/about-the-cancer-imaging-archive-tcia/).

It uses TCIA's WordPress Collection Manager as the publication authority,
TCIA's Publications EndNote XML as the verified bibliography of papers about
TCIA data, and a release-backed SQLite snapshot as its normal discovery layer.
It then routes users to the appropriate data system, such as IDC, CTDC,
General Commons, PathDB, DataCite, TCIA Data Retriever, or Aspera.

## What It Can Do

- Find TCIA Collections and Analysis Results by disease, body site, modality,
  data type, program, access level, license, DOI, or supporting data.
- Distinguish newly published datasets from datasets updated recently.
- Find current downloads, clinical metadata, annotations, segmentations,
  pathology slides, NIfTI files, and related Analysis Results.
- Search TCIA's verified publication library for papers about TCIA datasets.
- Route public DICOM through IDC and create viewer links or portable TCIA Data
  Retriever manifests.
- Explain controlled-access requirements without treating a public metadata
  record or manifest as authorization.

Example prompts:

- "Find TCIA datasets with breast MRI and tell me how to access them."
- "Which datasets were newly published or updated recently?"
- "Does this Collection have segmentations in a related Analysis Result?"
- "Find peer-reviewed papers using this TCIA dataset DOI."
- "Create a TCIA Data Retriever CSV for these Series Instance UIDs."
- "Is this dataset open, noncommercial, mixed, or controlled access?"

## Try The Hosted Demo

A read-only demonstration server exposes the same snapshot-backed query layer:

- **MCP endpoint:** [https://tcia.duckdns.org/mcp](https://tcia.duckdns.org/mcp)
- **REST API and Swagger UI:**
  [https://tcia.duckdns.org/v2/docs](https://tcia.duckdns.org/v2/docs)
- **OpenAPI document:**
  [https://tcia.duckdns.org/v2/openapi.json](https://tcia.duckdns.org/v2/openapi.json)

The MCP URL is a protocol endpoint, not a normal web page; configure it in an
MCP-capable client using streamable HTTP. The demo is intended for evaluation
and read-only queries. It does not expose arbitrary SQL, shell access, live
WordPress scraping, credentials, or controlled-data downloads. Availability
and capacity are not guaranteed. V2 is the supported and documented REST
interface for new clients.

See [mcp_server/README.md](./mcp_server/README.md) for the tool surface and
instructions for running your own MCP/REST server.

## Install Or Use Locally

The repository follows the agent-skills layout: [SKILL.md](./SKILL.md) contains
the main instructions, with focused material under [references/](./references/)
and deterministic helpers under [scripts/](./scripts/).

### OpenAI Codex

For local experimentation, ask Codex's `$skill-installer` to install this
GitHub repository. You can also place or symlink the skill directory in a
supported `.agents/skills` location. See the current [OpenAI skill
documentation](https://learn.chatgpt.com/docs/build-skills) for supported
locations and distribution guidance.

### Other agents

Agent products differ in how they discover skills, attach repository files,
and permit local execution. This repository does not claim a tested native
installation path for every agent product. At minimum, the host must be able
to load `SKILL.md`; full local functionality also requires access to the
repository's references, Python scripts, SQLite files, and network sources.

For environments that cannot run Python or SQLite, use the hosted demo above
or follow [references/mcp-and-web-llms.md](./references/mcp-and-web-llms.md).

## Quick Start

Python 3 is required. These examples use `python3`; substitute your
environment's Python 3 launcher if it has a different name.

From the skill root, install the manifest-pinned V2 research core. This fetches
the base WordPress snapshot and compact Participant Inventory as one validated
release contract:

```bash
python3 scripts/tcia_v2_bundle.py install --profile research_core
```

Then run a snapshot-backed search:

```bash
python3 scripts/tcia_wordpress_search.py --query breast --limit 10
python3 scripts/tcia_wordpress_search.py --short-title TCGA-BRCA --json
```

The `cache/` directory is intentionally excluded from Git. The installer
stages changed assets, checks bundle and component hashes, verifies decompressed
SQLite hashes and integrity, and only then replaces installed files under
`cache/tcia-metadata-v2-latest/`. A successful install also removes obsolete
installer-managed files that are not selected by the new receipt. It does not
remove arbitrary files or maintainer build directories.

Inspect disk use and abandoned installer staging without deleting anything,
then explicitly apply the reported cleanup if needed:

```bash
python3 scripts/tcia_v2_bundle.py prune
python3 scripts/tcia_v2_bundle.py prune --apply
```

Large `outputs/`, top-level `dist/`, and non-release directories under `cache/`
are build workspaces, not part of a normal installed skill. Diagnose them with
`du` before removing them; the installer deliberately does not delete those
operator-owned paths.

Install file-grain detail or verbose audit support only when needed:

```bash
python3 scripts/tcia_v2_bundle.py install --profile research_detail
python3 scripts/tcia_v2_bundle.py install --profile audit_support
```

Most routine snapshot and manifest helpers use the Python standard library.
Install task-specific packages such as `idc-index`, `pydicom`, or `cdapython`
only when the requested workflow needs them. Maintainer/build dependencies are
documented in the relevant reference file rather than installed as one large
default bundle.

## Network Access And Allowlisting

For local use behind an outbound firewall, allow HTTPS (`TCP 443`) to the
domains needed by your selected workflow.

Core skill update and released snapshot access:

- `github.com`
- `api.github.com`
- `release-assets.githubusercontent.com`

Live snapshot building and primary TCIA metadata sources:

- `cancerimagingarchive.net`
- `www.cancerimagingarchive.net`
- `api.datacite.org`
- `pathdb.cancerimagingarchive.net`

Optional enrichments and routes:

- `general.datacommons.cancer.gov` for General Commons metadata
- `glioblastoma.alleninstitute.org` for the Allen IvyGAP clinical source
- `raw.githubusercontent.com` for selected public GitHub-hosted source files
- `viewer.imaging.datacommons.cancer.gov` for IDC viewer links
- `volview.kitware.app` for VolView links
- `tcia.duckdns.org` when using the hosted MCP/REST demo instead of local data

IDC/idc-index, CDA, package installation, and actual data transfer may require
additional service or object-storage domains determined by those tools and the
selected dataset. WordPress download records can also point to dataset-specific
hosts, including IBM Aspera Faspex services. Consequently, the list above
covers the skill's known fixed endpoints but cannot be an exhaustive allowlist
for every possible download. In a tightly restricted environment, start with
the core domains or use the hosted demo, then approve optional destinations for
the specific workflow rather than broadly allowing all external traffic.

## Data Freshness

The base snapshot is normally rebuilt at 7:17 AM and 7:17 PM
America/New_York, followed by the V2 bundle producer. Run
`scripts/tcia_freshness.py check` to verify local operational files against the
version manifest on GitHub `main`, then run the V2 bundle installer to refresh
the checksum-verified research core. The skill check reports an update
requirement instead of silently replacing skill code.

If network verification fails, local results are offline/unverified and should
not be described as current. See [references/snapshots.md](./references/snapshots.md)
for release assets, sidecar behavior, schema/version details, and maintainer
workflows.

The default release contract is the moving `tcia-metadata-v2-latest` tag, with
immutable versioned V2 releases retained for reproducibility. It contains
hash-pinned research-core, research-detail, audit-support, and
compatibility-export profiles. The default research core contains the base
snapshot and compact Participant Inventory; file-grain metadata and verbose
audit evidence are optional. See
[references/artifact-model-v2.md](./references/artifact-model-v2.md).

Install the default V2 core or add research detail:

```bash
python3 scripts/tcia_v2_bundle.py install --profile research_core
python3 scripts/tcia_v2_bundle.py install --profile research_detail
```

Participant Explorer integrations should query `agent_participant_search` for
one row per canonical dataset-scoped, case-equivalent participant identifier,
then download detail components only for drill-down. Every original identifier
spelling remains available through `participant_identifiers`, and Collections
remain distinct from Analysis Results. Public-DICOM participant presence for
both dataset types is projected directly from IDC at build time while series
and file detail stays in IDC. Accepted clinical facts are no longer copied
into Participant Inventory by default; use the clinical detail artifact for
those values. Root source, access, linkage, inference, and coverage states stay
in the research artifacts, while verbose JSON evidence is joinable from the
optional `*_audit.sqlite.gz` companions by stable entity ID.

## Documentation Map

- [SKILL.md](./SKILL.md): agent workflow, routing rules, and guardrails
- [references/routing.md](./references/routing.md): source and access routing
- [references/schema.md](./references/schema.md): SQLite views and query patterns
- [references/snapshots.md](./references/snapshots.md): freshness and releases
- [references/artifact-model-v2.md](./references/artifact-model-v2.md): public non-DICOM and Participant Inventory contracts
- [references/clinical.md](./references/clinical.md): patient-level clinical data
- [references/controlled-access.md](./references/controlled-access.md): controlled-access policy and authorized Data Retriever use
- [references/nifti.md](./references/nifti.md): unified V2 NIfTI metadata
- [references/pathology.md](./references/pathology.md): unified V2 PathDB/Aspera pathology metadata
- [references/publications.md](./references/publications.md): verified publication searches
- [references/visualization.md](./references/visualization.md): viewer routing
- [mcp_server/README.md](./mcp_server/README.md): local MCP/REST service

## Scope And Safety

This repository helps agents find and explain TCIA data. It does not grant
access to restricted datasets, replace TCIA's official policies, or provide
medical, legal, or regulatory conclusions about dataset suitability. Always
preserve source provenance and distinguish local validation, published release
artifacts, and deployed service status.
