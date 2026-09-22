# IDC Public DICOM

Use this reference for public DICOM associated with a TCIA-published Collection or Analysis Result. It covers discovery, cohort sizing, series relationships, preview, citations and licenses, manifests, and downloads for radiology, DICOM slide microscopy, RTSTRUCT, SEG, SR, RTDOSE, RTPLAN, and other DICOM objects.

## Authority And Access Boundary

Confirm TCIA publication, access status, and license from the TCIA snapshot-backed MCP/REST service or a validated local bundle before using IDC. IDC can enrich and route public DICOM, but it does not establish that a dataset is TCIA-published.

Do not route controlled-access DICOM through IDC or NBIA as if it were public. Follow `controlled-access.md` and the downstream route in current TCIA metadata.

## Choose The IDC Surface

Choose by the agent's execution environment and the requested outcome. Keep this decision conceptual and let the current IDC documentation define tools, endpoints, schemas, supported viewers, and download commands.

| Environment or need | Preferred surface |
| --- | --- |
| MCP-capable agent with an IDC connection | Follow the current IDC MCP tool descriptions and resources |
| HTTP-capable agent, script, application, or notebook without MCP | Follow the current official IDC REST guide |
| Local execution, downloads, offline work, or reproducible local analysis | Follow the current IDC skill and local-tooling guidance |

Do not install local IDC tooling merely to answer a routine question when an appropriate remote IDC surface is already available. Conversely, do not force a requested local download or analysis workflow through a metadata-only interaction. Follow IDC's current division of responsibilities rather than copying it here.

Current IDC references:

- REST/MCP implementation and current user guide: `https://github.com/ImagingDataCommons/IDC-REST-MCP`
- IDC skill and current local-workflow guidance: `https://github.com/ImagingDataCommons/imaging-data-commons-skill`

## Workflow

1. Confirm that the Collection or Analysis Result is TCIA-published and public.
2. Hand IDC validated TCIA identifiers such as the short title, DOI, or Study/Series Instance UID allowlist.
3. Follow the current IDC guidance for the selected surface. Confirm the IDC data version, size the selection before downloading, and preserve any completeness or truncation signals it returns.
4. Inspect a small representative result and, when useful, request a current IDC viewer link before widening the cohort. Load `visualization.md` for the TCIA access and preview boundary.
5. Obtain current license and citation information from IDC when the user's selection or publication workflow needs it.
6. Ask whether the user wants an agent-run download or a portable TCIA Data Retriever manifest before transferring payloads.
7. If an expected public Series Instance UID is absent from IDC, report matched and missing counts. Load `nbia-public-dicom-fallback.md` only for the missing public subset or when the user explicitly requests NBIA.

## Preview Before Download

Ask the selected IDC surface for a currently supported viewer URL and use the returned URL. Do not manually construct an IDC viewer URL or assume which viewer product IDC currently supports. Use a representative study or series to help the user confirm modality, anatomy, scope, and relevant annotations before committing to a larger download.

A visual preview is a selection aid. It does not prove cohort completeness, replace metadata validation, or override access and license checks.

## DICOM Annotation Relationships

TCIA `find_dicom_annotations` and REST `/v2/dicom/annotation-downloads` identify WordPress download-level annotation signals; they do not model DICOM series relationships. After confirming TCIA provenance and public access, use the current IDC documentation and query surfaces for actual DICOM relationships and object-specific metadata.

Keep DICOM relationship schemas and query mechanics in IDC rather than copying them into the TCIA release artifact. Never infer source-image relationships from participant IDs, filenames, or proximity.

## Portable Data Retriever Manifests

When the user wants a portable manifest instead of a direct download, prefer an official current TCIA CSV/TSV/XLSX manifest when one exists. Otherwise create a CSV from a validated allowlist with `scripts/tcia_create_data_retriever_csv.py`.

For public DICOM, use exactly one `SeriesInstanceUID` column. Do not create a new legacy `.tcia` manifest unless the user explicitly requests that format. Existing `.tcia` manifests can still be parsed into an exact Series Instance UID allowlist; load `nbia-public-dicom-fallback.md` for that legacy format.

```bash
python scripts/tcia_create_data_retriever_csv.py --uids-file series_uids.txt --out manifest.csv
python scripts/tcia_create_data_retriever_csv.py --series-uid 1.2.3.4 --out manifest.csv
```

Other Data Retriever routes use separate manifests: `imageUrl` for direct public files such as PathDB images and `drs_uri` for official controlled-access DRS records. Do not mix route columns because Data Retriever applies header precedence.

## Answer Guidance

State which IDC surface was selected and why, and link the current upstream guide used for its mechanics. Distinguish metadata query, visual preview, manifest generation, and payload transfer. Report the IDC data version when freshness matters, preserve TCIA provenance and license evidence, and disclose partial IDC coverage before offering the NBIA fallback.
