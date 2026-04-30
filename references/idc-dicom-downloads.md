# IDC DICOM Download Guidance

Use this reference when a user asks to download TCIA-published DICOM data, including public radiology images, DICOM pathology, RTSTRUCT, SEG, SR, RTDOSE, RTPLAN, and other DICOM annotation/result objects.

## Policy

Prefer IDC and `idc-index` for open-access/public DICOM downloads. TCIA is phasing out NBIA, so do not use NBIA as the first download route for public DICOM data. Use NBIA only as a fallback when requested open-access/public DICOM series cannot be found in IDC/idc-index. If NBIA fallback is needed, use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.

Controlled-access DICOM is different: do not route it to IDC or NBIA fallback for public download. So far, controlled-access TCIA DICOM metadata lives in General Commons under `phs004225`, with WordPress license metadata as the access-status trigger and the TCIA controlled-access policy page as the user-facing access guidance.

If an IDC skill is available, use it for IDC-specific query, visualization, license, citation, and download mechanics. The TCIA skill should establish TCIA provenance through WordPress, identify the relevant WordPress download records or manifest, and provide IDC allowlist inputs such as the TCIA short title, DOI, or Series Instance UIDs.

IDC skill reference: `https://github.com/ImagingDataCommons/idc-claude-skill/blob/main/SKILL.md`

## Package Guidance

For IDC and DICOM workflows, prefer established Python packages over custom code:

- Use `idc-index` for IDC metadata lookup, download, viewer URLs, cloud-storage URLs, and Series Instance UID workflows.
- Use `pydicom` for local DICOM header/metadata inspection.

Before writing custom IDC or DICOM parsing code, check whether `idc_index` and `pydicom` are importable in the active Python environment. If they are missing and the user permits installation, install them with:

```bash
python -m pip install --upgrade idc-index pydicom
```

Do not silently install packages. If package installation is not allowed, explain the limitation and use bundled helpers only for the workflows they actually support, such as parsing existing `.tcia` manifests into Series Instance UIDs.

## Workflow

1. Confirm the dataset is a visible TCIA WordPress Collection or Analysis Result.
2. Check WordPress license metadata before giving download commands.
3. Identify DICOM downloads from WordPress `file_type = DICOM`, existing `.tcia` manifest URLs, CSV/TSV/XLSX manifest URLs, or DICOM-specific `data_type` values such as CT, MR, PT, RTSTRUCT, SEG, SR, DX, MG, CR, NM, RTDOSE, RTPLAN, RTIMAGE, REG, KO, PR, RWV, OT, US, XA, RF, and SC.
4. Before downloading, ask the user whether they want direct agent download in the current environment or a portable TCIA Data Retriever CSV manifest.
5. Prefer IDC/idc-index for direct agent downloads:
   - Match by TCIA DOI, short title, IDC `collection_id`, IDC `analysis_result_id`, or Series Instance UID.
   - For existing `.tcia` manifest records, extract Series Instance UIDs and use them as the most precise allowlist.
6. Query IDC for those Series Instance UIDs. If all requested public DICOM series are present and the user chose direct download, download through `idc-index`.
7. If the user chose a portable manifest, create or return a CSV manifest from the validated Series Instance UIDs instead of downloading files.
8. If only some series are present in IDC, clearly report the matched and missing counts, download or create a manifest for the matched subset only after the user approves that scope, and discuss fallback options for the missing series.
9. For open-access/public DICOM only, use NBIA after IDC/idc-index lookup fails or the user explicitly asks for NBIA despite the warning. If NBIA fallback is needed, tell users to use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.

## Legacy TCIA `.tcia` Manifests

The `.tcia` format is a legacy NBIA-era manifest format. Existing WordPress `.tcia` manifests remain useful as inputs because they typically contain a few configuration lines followed by one DICOM Series Instance UID per row. Use the bundled helper to extract UIDs:

```bash
python scripts/tcia_manifest_series_uids.py /path/to/download.tcia --out series_uids.txt
python scripts/tcia_manifest_series_uids.py "https://www.cancerimagingarchive.net/path/to/manifest.tcia" --json
```

Then use the IDC skill or `idc-index` to query and download those UIDs. A typical Python pattern is:

```python
from idc_index import IDCClient

client = IDCClient()
series_uids = [line.strip() for line in open("series_uids.txt") if line.strip()]

present = client.sql_query("""
    SELECT SeriesInstanceUID
    FROM index
    WHERE SeriesInstanceUID IN ({})
""".format(",".join(repr(uid) for uid in series_uids)))

matched_uids = present["SeriesInstanceUID"].tolist()
missing_uids = sorted(set(series_uids) - set(matched_uids))
print(f"Matched in IDC: {len(matched_uids)}; missing from IDC: {len(missing_uids)}")

client.download_from_selection(seriesInstanceUID=matched_uids, downloadDir="./idc_dicom")
```

For large UID lists, prefer IDC skill/idc-index patterns that avoid building very long SQL strings if available.

Do not write new `.tcia` manifests unless the user explicitly asks for the legacy format.

## Creating Portable CSV Manifests

TCIA Data Retriever accepts spreadsheets (`.csv`, `.tsv`, `.xlsx`) as manifests. When the user wants a portable TCIA Data Retriever manifest instead of direct agent download:

- Prefer an official current WordPress CSV/TSV/XLSX manifest download URL when one exists.
- If the agent has a validated Series Instance UID list, direct image URLs, or DRS URIs, create a CSV manifest with `scripts/tcia_create_data_retriever_csv.py`.
- For DICOM Series Instance UID workflows, use a single `SeriesInstanceUID` column.
- For PathDB/direct public file workflows, use an `imageUrl` column.
- For General Commons DRS/controlled-access workflows, use a `drs_uri` column and remind users that authorization may be required.
- Explain that a CSV manifest can be saved, opened with TCIA Data Retriever on another computer, or shared with a collaborator.
- For controlled-access datasets, remind users that authorization and TCIA Data Retriever API-key configuration are still required.

Example:

```bash
python scripts/tcia_create_data_retriever_csv.py --uids-file series_uids.txt --out manifest.csv
python scripts/tcia_create_data_retriever_csv.py --series-uid 1.2.3.4 --out manifest.csv
python scripts/tcia_create_data_retriever_csv.py --image-url https://example.org/file.zip --out files.csv
python scripts/tcia_create_data_retriever_csv.py --drs-uri drs://nci-crdc.datacommons.io/<file-id> --out controlled.csv
python scripts/tcia_create_data_retriever_csv.py --file-id <file-id> --out controlled.csv
```

## Data Retriever CSV Header Semantics

TCIA Data Retriever uses spreadsheet column headers as route selectors:

| Header in CSV | Meaning | Route |
| --- | --- | --- |
| `SeriesInstanceUID` | Preferred exact header for public DICOM Series Instance UIDs | IDC/S3 first, then TCIA/NBIA v4 fallback for public data missing from IDC |
| `Series UID` | Alternate exact header for public DICOM Series Instance UIDs | Same as `SeriesInstanceUID` |
| `imageUrl` | Preferred direct public file URL header | Direct download; TCIA convention is PathDB/non-DICOM pathology |
| `wsiimage_url` | Alternate direct public file URL header | Same as `imageUrl` |
| `drs_uri` | Preferred General Commons DRS URI header | Gen3/DRS controlled-access download |
| `File ID` or `file_id` | General Commons file ID header | Gen3/DRS controlled-access download; bare IDs are interpreted as `drs://nci-crdc.datacommons.io/<file-id>` |

For new manifests, use the preferred exact headers `SeriesInstanceUID`, `imageUrl`, or `drs_uri`. Create one manifest per route and do not mix these route headers. Data Retriever checks for Series UID columns before direct-file handling; if a direct-file spreadsheet contains both `drs_uri`/`File ID` and `imageUrl`, the DRS route takes precedence.

## NBIA Fallback

Use NBIA only when IDC/idc-index cannot find the requested open-access/public DICOM series. Do not use NBIA as a public fallback for controlled-access DICOM. When NBIA is needed:

- Tell users that IDC/idc-index remains the preferred public DICOM route.
- Tell users to use the NBIA v4 API, not older NBIA examples.
- Point users to the swagger YAML as the best API reference: `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.
- Use the swagger-defined endpoint names, parameters, and response shapes rather than older wiki examples when there is a conflict.

## Answering Users

When giving DICOM download guidance, say explicitly:

- IDC/idc-index is the preferred route for public DICOM because TCIA is phasing out NBIA.
- WordPress remains the TCIA provenance and license source.
- Ask whether the user wants direct agent download or a portable TCIA Data Retriever CSV manifest.
- CSV manifests are useful as Series Instance UID allowlists and as portable Data Retriever inputs, while `.tcia` is legacy. Downloading through NBIA APIs should be fallback-only for public DICOM.
- If NBIA fallback is required, use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.
- Controlled-access DICOM metadata should be handled through WordPress plus General Commons under `phs004225`; follow TCIA controlled-access guidance and do not imply public download.
