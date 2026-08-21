# TCIA Query MCP And REST Server

This directory contains a read-only server surface for the TCIA query skill. It
follows the same separation used by IDC-REST-MCP: one shared query service over
SQLite release snapshots, plus thin protocol adapters for MCP and REST.

```text
tcia_query_mcp/service.py  # TCIA snapshot and sidecar query logic
tcia_query_mcp/server.py   # MCP tools/resources for LLM clients
tcia_query_mcp/rest.py     # FastAPI routes for scripts, checks, and apps
```

The server defaults to the manifest-pinned V2 research core and optional detail
profiles:

- `tcia_snapshot.sqlite`: required TCIA WordPress/DataCite/PathDB snapshot.
- `participant_inventory.sqlite`: required V2 dataset-scoped participant availability.
- `public_non_dicom_metadata.sqlite`: optional V2 public non-DICOM file-grain detail.
- `controlled_access_metadata.sqlite`: optional controlled-access file-grain public metadata.
- `clinical_metadata.sqlite`: optional patient-level resolved clinical facts, provenance, and conflicts.

The streamlined V2 release does not contain standalone NIfTI or pathology
databases. Their research detail is unified in `public_non_dicom_metadata.sqlite`;
specialized source rows and QC evidence are retained in its audit companion.

It does not expose arbitrary SQL, shell commands, live WordPress scraping, or
direct controlled-data downloads.

## Tool Surface

Core MCP tools:

- `get_snapshot_info`
- `search_datasets`
- `get_dataset`
- `get_dataset_versions`
- `get_dataset_v1_releases`
- `get_current_downloads`
- `summarize_access`
- `find_dicom_annotations`
- `search_participants`
- `get_participant`
- `get_participant_assets`
- `get_dataset_participant_coverage`
- `find_participant_link_issues`

Optional detail tools:

- `find_public_non_dicom_assets`
- `find_controlled_access_datasets`
- `get_controlled_access_files`
- `find_clinical_datasets`
- `get_clinical_subjects`
- `get_clinical_facts`
- `get_clinical_conflicts`

The default public MCP surface contains these 20 supported V2 tools. NIfTI,
pathology, and other public non-DICOM discovery uses
`find_public_non_dicom_assets`.

MCP resources:

- `tcia://guide`
- `tcia://snapshot/info`

## Install

From the skill repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r mcp_server/requirements.txt
```

For production, create the virtual environment, pip cache, logs, and SQLite
snapshots in a server-local runtime directory rather than inside the Git
checkout. See `DEPLOYMENT.md`.

## Configure V2 Artifacts

Install the research core first. The bundle manifest pins the base snapshot and
Participant Inventory to one release fingerprint:

```bash
python3 scripts/tcia_v2_bundle.py install --profile research_core
python3 scripts/tcia_v2_bundle.py install --profile research_detail
```

`research_detail` already includes `research_core`; production hosts that need
detail can run only the second command.

By default the V2 service prefers validated files under
`cache/tcia-metadata-v2-latest/`. Production deployments can set
`TCIA_V2_INSTALL_DIR`, or
set the individual streamlined paths explicitly:

```bash
export TCIA_V2_INSTALL_DIR=/path/to/cache/tcia-metadata-v2-latest
export TCIA_SNAPSHOT_DB="$TCIA_V2_INSTALL_DIR/tcia_snapshot.sqlite"
export TCIA_PARTICIPANT_INVENTORY_DB="$TCIA_V2_INSTALL_DIR/participant_inventory.sqlite"
export TCIA_PUBLIC_NON_DICOM_METADATA_DB="$TCIA_V2_INSTALL_DIR/public_non_dicom_metadata.sqlite"
export TCIA_V2_BUNDLE_MANIFEST="$TCIA_V2_INSTALL_DIR/tcia_metadata_v2_bundle_manifest.json"
```

## Run MCP

Stdio mode is for local MCP clients:

```bash
python3 -m mcp_server --transport stdio
```

HTTP mode is for hosted/shared MCP clients:

```bash
python3 -m mcp_server --transport http --host 127.0.0.1 --port 8765
```

The HTTP MCP endpoint is:

```text
http://127.0.0.1:8765/mcp
```

A public read-only reference implementation backed by the current V2 bundle is
available at [https://tcia.duckdns.org/mcp](https://tcia.duckdns.org/mcp).
This is a streamable HTTP MCP protocol endpoint, not a normal web page.

Keep the process bound to `127.0.0.1` unless it is running only on a private
network. Put HTTPS, authentication, and access controls in front of it with a
reverse proxy or platform gateway.

## Run REST

The REST API uses the same service layer and is useful for health checks,
scripts, dashboards, or non-MCP clients:

```bash
python3 -m mcp_server.tcia_query_mcp.rest --host 127.0.0.1 --port 8766
```

V2 is the documented default. Interactive docs are available at:

```text
http://127.0.0.1:8766/v2/docs
```

The OpenAPI document is available at
`http://127.0.0.1:8766/v2/openapi.json`. New clients should use only `/v2/`
routes.

## Source Control Boundary

Commit reusable source, templates, and docs:

- `mcp_server/tcia_query_mcp/`
- `mcp_server/__main__.py`
- `mcp_server/requirements.txt`
- `mcp_server/tests/`
- `mcp_server/README.md`
- `mcp_server/DEPLOYMENT.md`
- `mcp_server/IMPLEMENTATION_PLAN.md`
- `mcp_server/systemd/*.service.example`

Do not commit runtime state:

- Python virtual environments
- pip caches
- logs
- downloaded SQLite snapshots or manifests
- actual systemd units under `/etc/systemd/system`
- reverse-proxy config, TLS material, domains, auth secrets, or firewall rules

The repository `.gitignore` excludes `cache/`, `dist/`, `*.sqlite`,
`*.sqlite.gz`, and `*_manifest.json`.
