# Download requirements and manifest inputs

Use the shared `get_download_plan` MCP tool or
`GET /v2/datasets/{short_title}/download-plan` REST endpoint before download
advice. The service reads the visible WordPress download inventory and returns
all selected groups with one shared software-help dictionary. It does not probe
upstream payloads, inspect a user's computer, authenticate, or transfer files.

## Compact contract

Each download has `software_requirement`:

| Code | User-facing meaning |
| --- | --- |
| `none` | No additional download software established as necessary |
| `tcia_data_retriever` | Requires TCIA Data Retriever for the published manifest workflow |
| `aspera` | Requires supported Aspera transfer software |
| `unknown` | Follow the official dataset instructions; requirements need review |

Use the corresponding `software[code].label`, `help_url`, and `instructions`
from the plan. Authorization is a separate requirement: public metadata about
controlled files does not grant access. Aspera browser and command-line clients
are alternatives; hosted notebooks need software installed in that runtime.

`requirement_status` is `declared`, `link_fallback`, `unknown`, or `conflict`.
Recognized WordPress requirements take precedence. An unrecognized nonempty
requirement is not replaced by a link guess. Missing declarations may use a
clearly marked link fallback; conflicts have no executable download method.
Report unknown/conflicting routes and retain those groups in the inventory.

`download_method` is `dicom_manifest`, `tabular_manifest`, `aspera_package`,
`direct_file`, or `unsupported`. This describes metadata routing, not successful
transfer or installed-tool readiness. TCIA Python tools can process supported
manifest formats without making users operate another tool directly. Do not
expand this into tutorials for unrelated download clients.

## Artifact and API ownership

Snapshot schema 9 stores the three compact codes in `wordpress_downloads` and
`agent_current_downloads`. `download_software_requirements` stores four help
entries once per SQLite artifact. Existing source fields remain for compatibility;
there is no added wording history or per-download audit payload. JSONL exports
carry the codes; their meanings and help definitions are documented here and in
the shared-plan response. Earlier installed snapshots can be interpreted by the
same service resolver until their next normal rebuild. Software interpretation
belongs to `scripts/tcia_download_requirements.py`, shared by the builder and service.

Collection search responses include `data_categories` from published download
categories. Keep these separate from modalities/data types, file formats, and
external-resource labels. Clients do not need per-dataset detail calls just to
obtain categories.

## Python manifest convenience

`tcia_utils.TCIAClient.download_manifest(path_or_https_url, destination, ...)`
reads local `.tcia`, CSV, or TSV inputs. `SeriesInstanceUID` inputs select public
DICOM series; `imageUrl` inputs select files; `drs_uri` inputs require approved
controlled credentials. Tabular manifests must contain exactly one route column.
Unsupported, malformed, conflicting, or unavailable selections are reported
rather than silently reduced. Controlled UID-only manifests are not translated
into public routes. A dry run reads the small manifest but transfers no payload.

Local files, credentials, installed tools, and actual transfers belong to the
Python client. They are not uploaded to the metadata service. Keep credentials
out of notebooks, reports, and the artifact bundle. Croissant support is deferred
as a separate work item.


## Participant image files

The public non-DICOM asset response exposes `imageUrl`, `download_file_name`,
`download_representation`, and `download_access_basis` for recognized public
PathDB whole-slide file records. The HTTPS route is derived only for that known
host/file path and source class; `source_url` and source file names remain intact.
Controlled/ambiguous routes, viewer pages, packages, unknown hosts, query URLs,
and DICOM conversions do not gain a public download route. This is metadata-only
routing and does not probe or transfer files. The representation is a public
pathology slide copy, with equivalence to the submitted original unestablished.
Clinical cohorts must be matched by dataset identity plus participant ID; public
DICOM selections remain anchored to the dataset's official manifest and IDC.
