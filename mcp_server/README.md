# TCIA Query MCP And REST Server

This directory contains a read-only server surface for the TCIA query skill. It
follows the same separation used by IDC-REST-MCP: one shared query service over
SQLite release snapshots, plus thin protocol adapters for MCP and REST.

```text
tcia_query_mcp/service.py  # TCIA snapshot and sidecar query logic
tcia_query_mcp/server.py   # MCP tools/resources for LLM clients
tcia_query_mcp/rest.py     # FastAPI routes for scripts, checks, and apps
```

The server is backed by release SQLite files:

- `tcia_snapshot.sqlite`: required TCIA WordPress/DataCite/PathDB snapshot.
- `controlled_access_metadata.sqlite`: optional controlled-access file-grain public metadata.
- `nifti_metadata.sqlite`: optional public non-DICOM NIfTI file-grain metadata.
- `pathology_metadata.sqlite`: optional pathology, PathDB, and Aspera package metadata.
- `clinical_metadata.sqlite`: optional patient-level resolved clinical facts, provenance, and conflicts.

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

Optional sidecar tools:

- `find_controlled_access_datasets`
- `get_controlled_access_files`
- `find_nifti_datasets`
- `get_nifti_files`
- `get_nifti_derived_objects`
- `get_nifti_package_files`
- `find_pathology_datasets`
- `get_pathology_downloads`
- `get_pathology_package_files`
- `get_pathology_file_objects`
- `get_pathology_disparities`
- `find_clinical_datasets`
- `get_clinical_subjects`
- `get_clinical_facts`
- `get_clinical_conflicts`

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

## Configure Snapshots

The base snapshot is required. The sidecars are optional, but install all four
for the full MCP interface.

```bash
export TCIA_SNAPSHOT_DB=/path/to/cache/tcia_snapshot.sqlite
export TCIA_CONTROLLED_ACCESS_METADATA_DB=/path/to/cache/controlled_access_metadata.sqlite
export TCIA_NIFTI_METADATA_DB=/path/to/cache/nifti_metadata.sqlite
export TCIA_PATHOLOGY_METADATA_DB=/path/to/cache/pathology_metadata.sqlite
export TCIA_CLINICAL_METADATA_DB=/path/to/cache/clinical_metadata.sqlite

python3 scripts/tcia_snapshot.py ensure
python3 scripts/tcia_controlled_access_metadata.py ensure
python3 scripts/tcia_nifti_metadata.py ensure
python3 scripts/tcia_pathology_metadata.py ensure
python3 scripts/tcia_clinical_metadata.py ensure
```

Use the `ensure` commands rather than copying scratch SQLite files. They fetch
and verify the current `tcia-snapshot-latest` GitHub release assets.

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

Keep the process bound to `127.0.0.1` unless it is running only on a private
network. Put HTTPS, authentication, and access controls in front of it with a
reverse proxy or platform gateway.

## Run REST

The REST API uses the same service layer and is useful for health checks,
scripts, dashboards, or non-MCP clients:

```bash
python3 -m mcp_server.tcia_query_mcp.rest --host 127.0.0.1 --port 8766
```

Interactive docs are available at:

```text
http://127.0.0.1:8766/v1/docs
```

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
