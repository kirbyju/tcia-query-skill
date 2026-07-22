# TCIA Query MCP Deployment

This guide is written for Linux operators who want to host the TCIA query MCP
server, with or without the companion REST API. It uses generic paths; choose a
runtime root that fits your server. If the root partition is small, put the
runtime root on a larger mounted volume.

## 1. Choose A Runtime Root

Pick a directory for the Git checkout, virtual environment, pip cache, logs, and
SQLite snapshots:

```bash
export TCIA_MCP_ROOT=/srv/tcia-query-mcp
mkdir -p "$TCIA_MCP_ROOT"/{cache,pip-cache,logs}
```

`/srv/tcia-query-mcp` is only an example. On storage-constrained hosts, use a
path on a larger data partition.

## 2. Check Out The Repo

```bash
cd "$TCIA_MCP_ROOT"
git clone https://github.com/kirbyju/tcia-query-skill.git
cd "$TCIA_MCP_ROOT/tcia-query-skill"
```

If your organization mirrors or forks this repo, clone that URL instead.

## 3. Create A Virtual Environment

Keep the virtual environment and pip cache under the runtime root:

```bash
python3 -m venv "$TCIA_MCP_ROOT/.venv"
export PIP_CACHE_DIR="$TCIA_MCP_ROOT/pip-cache"
"$TCIA_MCP_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$TCIA_MCP_ROOT/.venv/bin/python" -m pip install \
  -r "$TCIA_MCP_ROOT/tcia-query-skill/mcp_server/requirements.txt"
```

## 4. Download Release Snapshots

Set explicit paths for the base snapshot and all optional sidecars:

```bash
export TCIA_SNAPSHOT_DB="$TCIA_MCP_ROOT/cache/tcia_snapshot.sqlite"
export TCIA_CONTROLLED_ACCESS_METADATA_DB="$TCIA_MCP_ROOT/cache/controlled_access_metadata.sqlite"
export TCIA_NIFTI_METADATA_DB="$TCIA_MCP_ROOT/cache/nifti_metadata.sqlite"
export TCIA_PATHOLOGY_METADATA_DB="$TCIA_MCP_ROOT/cache/pathology_metadata.sqlite"
```

Fetch and verify the current GitHub release artifacts:

```bash
cd "$TCIA_MCP_ROOT/tcia-query-skill"
"$TCIA_MCP_ROOT/.venv/bin/python" scripts/tcia_snapshot.py ensure
"$TCIA_MCP_ROOT/.venv/bin/python" scripts/tcia_controlled_access_metadata.py ensure
"$TCIA_MCP_ROOT/.venv/bin/python" scripts/tcia_nifti_metadata.py ensure
"$TCIA_MCP_ROOT/.venv/bin/python" scripts/tcia_pathology_metadata.py ensure
```

The base snapshot is required. The controlled-access, NIfTI, and pathology
sidecars are optional, but the full public MCP interface expects all three.

## 5. Smoke Test

Run the MCP HTTP endpoint locally:

```bash
cd "$TCIA_MCP_ROOT/tcia-query-skill"
"$TCIA_MCP_ROOT/.venv/bin/python" -m mcp_server \
  --transport http --host 127.0.0.1 --port 8765
```

In another terminal, run the REST API if you want ordinary HTTP endpoints too:

```bash
cd "$TCIA_MCP_ROOT/tcia-query-skill"
"$TCIA_MCP_ROOT/.venv/bin/python" -m mcp_server.tcia_query_mcp.rest \
  --host 127.0.0.1 --port 8766
```

Check REST health and loaded snapshot metadata:

```bash
curl -fsS http://127.0.0.1:8766/v1/health
curl -fsS http://127.0.0.1:8766/v1/snapshot
```

For MCP, connect a local MCP client to:

```text
http://127.0.0.1:8765/mcp
```

The MCP server also supports stdio for local desktop/client use:

```bash
"$TCIA_MCP_ROOT/.venv/bin/python" -m mcp_server --transport stdio
```

## 6. Install Systemd Services

Copy the example unit and edit every path, user, group, port, and environment
value for your server:

```bash
sudo cp mcp_server/systemd/tcia-query-mcp.service.example \
  /etc/systemd/system/tcia-query-mcp.service
sudo editor /etc/systemd/system/tcia-query-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now tcia-query-mcp
```

If you also want REST as a persistent service, copy and edit
`mcp_server/systemd/tcia-query-rest.service.example` the same way.

## 7. Put A Gateway In Front

For shared or public use, keep MCP and REST bound to `127.0.0.1` and put a
gateway in front:

- HTTPS termination
- authentication or IP allowlisting
- request size/time limits
- access logs
- firewall rules that block direct external access to the Uvicorn ports

The MCP transport disables FastMCP DNS-rebinding protection by default because
deployments commonly sit behind a reverse proxy. To enable it, set:

```text
TCIA_MCP_DNS_REBINDING_PROTECTION=true
TCIA_MCP_ALLOWED_HOSTS=mcp.example.org
TCIA_MCP_ALLOWED_ORIGINS=https://mcp.example.org
```

## 8. Refresh Snapshots

Refresh snapshots with the same environment variables used by the service:

```bash
cd "$TCIA_MCP_ROOT/tcia-query-skill"
"$TCIA_MCP_ROOT/.venv/bin/python" scripts/tcia_snapshot.py ensure
"$TCIA_MCP_ROOT/.venv/bin/python" scripts/tcia_controlled_access_metadata.py ensure
"$TCIA_MCP_ROOT/.venv/bin/python" scripts/tcia_nifti_metadata.py ensure
"$TCIA_MCP_ROOT/.venv/bin/python" scripts/tcia_pathology_metadata.py ensure
sudo systemctl restart tcia-query-mcp
```

Use a systemd timer, cron, or your configuration-management system for scheduled
refreshes. Avoid committing snapshots or copying stale local caches between
servers; use the release `ensure` commands so checksums are verified.

## Repository Vs Server State

Keep these in GitHub:

- MCP/REST source code
- tests
- reusable docs
- example service files

Keep these server-local:

- virtual environments
- pip caches
- downloaded SQLite files and manifests
- logs
- actual `/etc/systemd/system/*.service` files
- TLS certificates, auth config, firewall rules, domains, and secrets
