# Visualization Guidance

Use this reference when a user asks to preview, visualize, open, inspect, or launch a viewer for TCIA-published data before downloading. TCIA access boundaries and viewer selection live here; changing IDC mechanics remain in IDC's current documentation.

Return viewer URLs as clickable links for the user to open in a regular browser. Do not install or run browser automation merely to demonstrate a viewer link.

## Access Rule

Confirm TCIA publication and license/access metadata first. Do not construct public viewer links for controlled or restricted data. Explain that public metadata can still be inspected, but visualization requires authorized download followed by local analysis. If the user requests an authorized transfer and provides the path to their own JSON key, follow `controlled-access.md`.

## Choose A Viewer

| Public data | Viewer | Identifier source |
| --- | --- | --- |
| Public DICOM in IDC | Viewer URL returned by the current IDC surface | IDC-validated Study or Series Instance UID |
| Non-DICOM PathDB pathology slide | caMicroscope | PathDB cohort-builder CSV `camic_id` |

Preview a narrow representative study or series before a large cohort download when doing so helps confirm modality, anatomy, annotation content, or selection scope. A preview does not prove completeness or replace metadata, citation, or license checks.

## Generate IDC Viewer Links

Use the environment-appropriate IDC surface described in `idc-public-dicom.md`, follow its current documentation, and request a viewer URL for an IDC-validated study or series. Use the returned URL without reconstructing it or assuming which viewer product IDC currently supports.

Useful upstream references:

- IDC REST/MCP user guide: `https://github.com/ImagingDataCommons/IDC-REST-MCP/blob/main/docs/user-guide.md`
- IDC skill: `https://github.com/ImagingDataCommons/imaging-data-commons-skill`

## caMicroscope For PathDB Slides

For public non-DICOM PathDB slides, obtain the numeric `camic_id` from the PathDB cohort-builder CSV or `scripts/pathdb_metadata.py` and use:

```text
https://pathdb.cancerimagingarchive.net/caMicroscope/apps/mini/viewer.html?mode=pathdb&slideId=<camic_id>
```

Although the URL parameter is called `slideId`, it requires numeric `camic_id`, not the CSV `slide_id`, `patient_id`, specimen label, or filename stem.

## Other Public Non-DICOM Data

Do not assume that a browser viewer exists. If current TCIA download metadata exposes a public viewer, report it with source context; otherwise explain that the data need a format-specific local tool after download.
