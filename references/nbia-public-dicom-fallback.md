# NBIA Public DICOM Fallback

Use this reference only when requested public TCIA DICOM cannot be found in IDC, or when the user explicitly asks for NBIA after the preferred IDC route is explained.

## Boundary

NBIA is not the first route for public DICOM. Confirm TCIA publication and public access first, query IDC through the environment-appropriate surface described in `idc-public-dicom.md`, and limit NBIA work to the public series that remain missing.

Never use NBIA as a public fallback for controlled-access DICOM. Follow `controlled-access.md` instead.

## Supported API

Use the NBIA v4 API and its current Swagger contract:

`https://cbiit.github.io/NBIA-TCIA/nbia-api.yaml`

Prefer the Swagger-defined endpoint names, parameters, and response shapes over older wiki pages or examples when they conflict. State that NBIA is a fallback route and report why it was needed.

## Fallback Workflow

1. Preserve the exact Series Instance UID allowlist used for the IDC lookup.
2. Report how many requested public series were found in IDC and how many were missing.
3. Query NBIA v4 only for the missing public series, unless the user explicitly requests an NBIA-only workflow.
4. Keep IDC and NBIA results distinguishable rather than implying one complete homogeneous response.
5. Ask before transferring payload data. A request for guidance, counts, or a manifest is not authorization to start a download.

## Legacy `.tcia` Manifests

The `.tcia` format is a legacy NBIA-era manifest. Existing TCIA manifests remain useful as inputs because they commonly contain configuration lines followed by DICOM Series Instance UIDs. Extract the UIDs with the bundled helper, then use them as an allowlist for IDC coverage checks and any necessary NBIA fallback:

```bash
python scripts/tcia_manifest_series_uids.py /path/to/download.tcia --out series_uids.txt
python scripts/tcia_manifest_series_uids.py "https://www.cancerimagingarchive.net/path/to/manifest.tcia" --json
```

Do not create new `.tcia` manifests unless the user explicitly asks for the legacy format. For portable new manifests, use a CSV containing exactly one `SeriesInstanceUID` column as described in `idc-public-dicom.md`.

## Answer Guidance

Include the TCIA dataset and license evidence, the IDC coverage result, the missing public Series Instance UIDs or count, the reason NBIA was used, and the NBIA v4 reference. Do not imply that NBIA establishes TCIA publication or provides access to controlled data.
