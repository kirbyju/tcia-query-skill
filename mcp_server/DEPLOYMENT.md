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
"$TCIA_MCP_ROOT/.venv/bin/python" -m pip install --require-hashes \
  -r "$TCIA_MCP_ROOT/tcia-query-skill/requirements-server.lock"
```

## 4. Install the V2 Release Contract

Set one server-local V2 installation directory:

```bash
export TCIA_V2_INSTALL_DIR="$TCIA_MCP_ROOT/cache/tcia-metadata-v2-latest"
export TCIA_SNAPSHOT_DB="$TCIA_V2_INSTALL_DIR/tcia_snapshot.sqlite"
export TCIA_PARTICIPANT_INVENTORY_DB="$TCIA_V2_INSTALL_DIR/participant_inventory.sqlite"
export TCIA_PUBLIC_NON_DICOM_METADATA_DB="$TCIA_V2_INSTALL_DIR/public_non_dicom_metadata.sqlite"
export TCIA_CONTROLLED_ACCESS_METADATA_DB="$TCIA_V2_INSTALL_DIR/controlled_access_metadata.sqlite"
export TCIA_CLINICAL_METADATA_DB="$TCIA_V2_INSTALL_DIR/clinical_metadata.sqlite"
export TCIA_V2_BUNDLE_MANIFEST="$TCIA_V2_INSTALL_DIR/tcia_metadata_v2_bundle_manifest.json"
```

Install and verify the research core plus file-grain detail:

```bash
cd "$TCIA_MCP_ROOT/tcia-query-skill"
"$TCIA_MCP_ROOT/.venv/bin/python" scripts/tcia_v2_bundle.py install \
  --profile research_detail --install-dir "$TCIA_V2_INSTALL_DIR"
```

The installer builds a fingerprinted generation, validates all selected
payloads, and atomically switches the `current` pointer. Compatibility symlinks
keep every path shown above working. The current generation and at least one
verified prior generation are retained; inspect them with `prune` and roll back
without downloading with:

```bash
"$TCIA_MCP_ROOT/.venv/bin/python" scripts/tcia_v2_bundle.py rollback \
  --install-dir "$TCIA_V2_INSTALL_DIR"
```

The first install after this feature automatically migrates a valid legacy
flat directory. It does not change `TCIA_V2_INSTALL_DIR` or any individual
database environment variable.

`research_detail` includes its required `research_core` dependency. It adds the
unified public non-DICOM, controlled-access, and clinical detail artifacts.
NIfTI and pathology discovery are provided by the unified public non-DICOM
artifact; the streamlined release does not publish standalone NIfTI or pathology
SQLite files. Do not export `TCIA_NIFTI_METADATA_DB` or
`TCIA_PATHOLOGY_METADATA_DB` for this V2 installation.

Install `audit_support` only when the host needs verbose audit databases or the
full correction registry. That profile includes `research_detail`; the compact
correction decision-set digest, counts, and source-health status remain visible
in the top bundle manifest without installing the registry SQLite.

The MCP server advertises the 20 supported V2 tools. Do not configure standalone
NIfTI or pathology databases for the streamlined public service; those domains
are provided by the unified public non-DICOM detail artifact.

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

Check REST health and the installed bundle contract:

```bash
curl -fsS http://127.0.0.1:8766/v2/live
curl -fsS http://127.0.0.1:8766/v2/ready
curl -fsS http://127.0.0.1:8766/v2/bundle
```

Confirm the returned bundle fingerprint, component schema versions, installed
profile, and detail capabilities before switching traffic. The endpoint reads
only the manifest and installer state; it does not recount large SQLite views.
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

## 8. Refresh the V2 Bundle

Refresh snapshots with the same environment variables used by the service:

```bash
cd "$TCIA_MCP_ROOT/tcia-query-skill"
"$TCIA_MCP_ROOT/.venv/bin/python" scripts/tcia_v2_bundle.py install \
  --profile research_detail --install-dir "$TCIA_V2_INSTALL_DIR"
sudo systemctl restart tcia-query-mcp
sudo systemctl restart tcia-query-rest
```

### Existing operator upgrade answer

Existing MCP/REST operators do **not** need an extra migration command or new
path configuration. Keep the normal sequence: pull the reviewed code, install
or update dependencies from `requirements-server.lock` with `--require-hashes`,
run the same `tcia_v2_bundle.py install --profile ...`
command, restart the services, then reconnect clients that cache MCP sessions.
The installer performs the one-time flat-layout migration automatically.
Operators should additionally confirm `/v2/bundle` reports the intended
fingerprint and source-health summary after restart. Rollback is optional and
uses the command above; restart and reconnect after selecting the prior
generation.

Compatibility aliases are restored for installer-managed paths recorded by a
valid receipt. This is not a promise to preserve arbitrary operator-created
files or symlinks in the install directory.

Set the repository Actions variable `TCIA_V2_DEPLOYED_INFO_URL` to the deployed
snapshot/bundle-info JSON endpoint if release jobs should compare the published
fingerprint with the deployed fingerprint. This check reports lag but does not
perform deployment.

Use a systemd timer, cron, or your configuration-management system for scheduled
refreshes. Avoid committing snapshots or copying stale local caches between
servers; use the bundle installer so the top-level fingerprint, component
hashes, decompressed SQLite hashes, and integrity checks are verified together.

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
