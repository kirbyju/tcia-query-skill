# PathDB Metadata

Use this reference for TCIA non-DICOM histopathology data after WordPress confirms the dataset is TCIA-published.

## Stable Cohort-Builder CSV

PathDB publishes a stable CSV with rich per-slide metadata:

```text
https://pathdb.cancerimagingarchive.net/system/files/collectionmetadata/202401/cohort_builder_v1_01-16-2024.csv
```

The filename includes a date, but TCIA expects this URL to remain consistent for the foreseeable future, even as new datasets are published.

## Matching Rule

Match the CSV `collection` field to WordPress `collection_short_title` or `result_short_title`.

The older PathDB collection API may expose collection names as `collectionName`; prefer the CSV when the user needs detailed non-DICOM histopathology metadata.

## CSV Columns

Observed columns:

- `collection`
- `collection_doi`
- `patient_id`
- `slide_id`
- `view`
- `camic_id`
- `wsiimage_url`
- `has_radiology`
- `has_genomics`
- `has_proteomics`
- `species`
- `cancer_type`
- `cancer_location`
- `data_format`
- `supporting_data_type`
- `modality`
- `protocol`
- `par`
- `magnification`
- `update`

## Helper Script

Use `scripts/pathdb_metadata.py` from the skill root:

```bash
python scripts/pathdb_metadata.py --collection CPTAC-STAD --summary
python scripts/pathdb_metadata.py --query stomach --limit 10
python scripts/pathdb_metadata.py --doi 10.7937/jw9a-8k71 --json
```

## Response Guidance

For PathDB results, summarize:

- Collection and DOI.
- Patient count and slide count.
- Data format and modality.
- Cancer type/location and species.
- Whether related radiology, genomics, or proteomics flags are present.
- Whether WSI URLs are available for slide-level access.

Keep WordPress as the provenance authority. If PathDB contains a collection that does not appear in WordPress, do not present it as TCIA-published without additional confirmation.
