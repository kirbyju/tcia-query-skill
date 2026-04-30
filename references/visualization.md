# Visualization Guidance

Use this reference when a user asks to preview, visualize, open, view, inspect, or launch a viewer for TCIA-published data before downloading.

Return viewer URLs as clickable links for the user to open in their regular browser. Do not install Playwright, browser drivers, or other browser automation just to show example data. Only launch or automate a browser if the user explicitly asks for that and the local agent environment supports it.

## Access Rule

Controlled-access data cannot be visualized in a browser before download, regardless of file format. If WordPress license metadata indicates controlled/restricted access, do not construct OHIF, VolView, SliM, caMicroscope, IDC, NBIA, PathDB, or other public viewer links. Explain that metadata can be inspected, but visualization requires authorized access and local/authorized-platform download. Point users to the TCIA controlled-access policy page: `https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/`.

Open-access data can sometimes be visualized before download. Choose the viewer based on format and modality.

## Open DICOM In IDC

For open-access/public DICOM, use IDC capabilities. If the IDC skill is available, use it for IDC-specific queries and viewer URL generation. Prefer `idc-index` because it validates that the study/series is present in IDC:

```python
from idc_index import IDCClient

client = IDCClient()
url = client.get_viewer_URL(
    studyInstanceUID="1.2.3",
    seriesInstanceUID="1.2.3.4",
    viewer_selector="ohif_v3",
)
```

If `viewer_selector` is omitted, idc-index selects OHIF v3 for radiology modalities and SliM for SM slide microscopy.

Useful upstream references:

- IDC skill: `https://github.com/ImagingDataCommons/idc-claude-skill/blob/main/SKILL.md`
- idc-index API: `https://idc-index.readthedocs.io/en/stable/api/idc_index.html`
- IDC visualization guide: `https://learn.canceridc.dev/portal/visualization`

## OHIF v3 For Radiology DICOM

Use OHIF v3 for open-access/public radiology DICOM and compatible DICOM annotations/results in IDC.

Manual URL pattern:

```text
https://viewer.imaging.datacommons.cancer.gov/v3/viewer/?StudyInstanceUIDs=<StudyInstanceUID>&SeriesInstanceUIDs=<SeriesInstanceUID>
```

Multiple series can be comma-separated in `SeriesInstanceUIDs` when the viewer supports loading them together. If the user wants the whole study rather than specific series, omit the `SeriesInstanceUIDs` parameter after idc-index validates that the study is in IDC.

For TCIA skill answers, prefer OHIF v3 and the `idc-index` helper when possible.

## VolView For Radiology Volumes

VolView is useful for browser-based volume rendering of open-access/public radiology series, but it uses cloud-storage URLs rather than DICOM UIDs directly.

Workflow:

1. Confirm the series is open/public and present in IDC.
2. Use idc-index to look up the series cloud-storage folder from `SeriesInstanceUID`.
3. Construct the VolView URL from the S3 series folder.

Example idc-index lookup:

```python
from idc_index import IDCClient

client = IDCClient()
rows = client.sql_query("""
    SELECT StudyInstanceUID, SeriesInstanceUID, Modality, series_aws_url, crdc_series_uuid
    FROM index
    WHERE SeriesInstanceUID = '1.2.3.4'
""")
```

Manual URL pattern using `series_aws_url`:

```text
https://volview.kitware.app/?urls=[s3://idc-open-data/<crdc_series_uuid>]
```

Preserve the bucket from `series_aws_url` when it is not `idc-open-data`, for example `idc-open-data-two` or `idc-open-data-cr`. Remove any trailing slash or wildcard (`/*`) before placing the S3 folder in the VolView URL.

## SliM For Slide Microscopy DICOM

Use SliM for open-access/public DICOM slide microscopy (`SM`) data in IDC.

Manual URL pattern:

```text
https://viewer.imaging.datacommons.cancer.gov/slim/studies/<StudyInstanceUID>/series/<SeriesInstanceUID>
```

SliM may also show DICOM slide microscopy annotations/results when present.

## Helper Script

The bundled helper can construct viewer URLs when you already have the needed identifiers:

```bash
python scripts/idc_viewer_urls.py ohif-v3 --study-uid <StudyInstanceUID> --series-uid <SeriesInstanceUID>
python scripts/idc_viewer_urls.py slim --study-uid <StudyInstanceUID> --series-uid <SeriesInstanceUID>
python scripts/idc_viewer_urls.py volview --s3-url s3://idc-open-data/<crdc_series_uuid>
python scripts/idc_viewer_urls.py volview --crdc-series-uuid <crdc_series_uuid>
```

The helper does not verify that data are open access or present in IDC. Always do WordPress license/provenance checks and IDC/idc-index validation first.

## Open Non-DICOM PathDB Slides

For open/public non-DICOM histopathology data in PathDB, use caMicroscope for browser visualization before download. Confirm TCIA provenance in WordPress first, check that license metadata is open, then use the stable PathDB cohort-builder CSV to map slide IDs to collection, patient, DOI, cancer type/location, species, data format, and related-data flags.

CSV:

```text
https://pathdb.cancerimagingarchive.net/system/files/collectionmetadata/202401/cohort_builder_v1_01-16-2024.csv
```

caMicroscope URL pattern:

```text
https://pathdb.cancerimagingarchive.net/caMicroscope/apps/mini/viewer.html?mode=pathdb&slideId=<slide_id>
```

Example:

```text
https://pathdb.cancerimagingarchive.net/caMicroscope/apps/mini/viewer.html?mode=pathdb&slideId=314525
```

The bundled PathDB helper adds `camicroscope_url` to slide-level rows:

```bash
python scripts/pathdb_metadata.py --collection <TCIA-short-title> --limit 10
python scripts/pathdb_metadata.py --query "stomach svs" --json --limit 5
```

## Other Non-DICOM Open Data

For other open non-DICOM data, do not assume browser visualization is available. Use WordPress download metadata, Aspera, or other system-specific guidance. If a dataset page exposes a public viewer URL, report it with the source context; otherwise explain that the data likely need to be downloaded or opened in a format-specific local tool.
