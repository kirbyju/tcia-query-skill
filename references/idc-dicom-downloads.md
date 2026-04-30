# IDC DICOM Download Guidance

Use this reference when a user asks to download TCIA-published DICOM data, including public radiology images, DICOM pathology, RTSTRUCT, SEG, SR, RTDOSE, RTPLAN, and other DICOM annotation/result objects.

## Policy

Prefer IDC and `idc-index` for open-access/public DICOM downloads. TCIA is phasing out NBIA, so do not use NBIA as the first download route for public DICOM data. Use NBIA only as a fallback when requested open-access/public DICOM series cannot be found in IDC/idc-index. If NBIA fallback is needed, use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.

Controlled-access DICOM is different: do not route it to IDC or NBIA fallback for public download. So far, controlled-access TCIA DICOM metadata lives in General Commons under `phs004225`, with WordPress license metadata as the access-status trigger and the TCIA controlled-access policy page as the user-facing access guidance.

If an IDC skill is available, use it for IDC-specific query, visualization, license, citation, and download mechanics. The TCIA skill should establish TCIA provenance through WordPress, identify the relevant WordPress download records or manifest, and provide IDC allowlist inputs such as the TCIA short title, DOI, or Series Instance UIDs.

IDC skill reference: `https://github.com/ImagingDataCommons/idc-claude-skill/blob/main/SKILL.md`

## Workflow

1. Confirm the dataset is a visible TCIA WordPress Collection or Analysis Result.
2. Check WordPress license metadata before giving download commands.
3. Identify DICOM downloads from WordPress `file_type = DICOM`, `.tcia` manifest URLs, or DICOM-specific `data_type` values such as CT, MR, PT, RTSTRUCT, SEG, SR, DX, MG, CR, NM, RTDOSE, RTPLAN, RTIMAGE, REG, KO, PR, RWV, OT, US, XA, RF, and SC.
4. Prefer IDC/idc-index:
   - Match by TCIA DOI, short title, IDC `collection_id`, IDC `analysis_result_id`, or Series Instance UID.
   - For `.tcia` manifest records, extract Series Instance UIDs and use them as the most precise allowlist.
5. Query IDC for those Series Instance UIDs. If all requested public DICOM series are present, download through `idc-index`.
6. If only some series are present in IDC, clearly report the matched and missing counts, download the matched subset through IDC, and discuss fallback options for the missing series.
7. For open-access/public DICOM only, use NBIA after IDC/idc-index lookup fails or the user explicitly asks for NBIA despite the warning. If NBIA fallback is needed, tell users to use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.

## TCIA `.tcia` Manifests

TCIA Data Retriever manifests typically contain a few lines of configuration at the top followed by one DICOM Series Instance UID per row. Use the bundled helper to extract UIDs:

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
- `.tcia` manifests are useful as Series Instance UID allowlists, but downloading through NBIA should be fallback-only for public DICOM.
- If NBIA fallback is required, use the NBIA v4 API documented by `https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`.
- Controlled-access DICOM metadata should be handled through WordPress plus General Commons under `phs004225`; follow TCIA controlled-access guidance and do not imply public download.
