# IDC DICOM Download Guidance

Use this reference when a user asks to download TCIA-published DICOM data, including public radiology images, DICOM pathology, RTSTRUCT, SEG, SR, RTDOSE, RTPLAN, and other DICOM annotation/result objects.

## Policy

Prefer IDC and `idc-index` for DICOM downloads. TCIA is phasing out NBIA, so do not use the NBIA v1 API as the first download route for public DICOM data. Use NBIA only as a fallback when the requested DICOM series cannot be found in IDC/idc-index, or when the dataset is controlled access and WordPress/license guidance says IDC is not the access route.

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
7. Use NBIA v1 only after IDC/idc-index lookup fails or the user explicitly asks for NBIA despite the warning.

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

## Answering Users

When giving DICOM download guidance, say explicitly:

- IDC/idc-index is the preferred route for public DICOM because TCIA is phasing out NBIA.
- WordPress remains the TCIA provenance and license source.
- `.tcia` manifests are useful as Series Instance UID allowlists, but downloading through TCIA Data Retriever/NBIA should be fallback-only for public DICOM.
- Controlled-access datasets may not be downloadable through IDC; follow WordPress license metadata and TCIA controlled-access guidance.
