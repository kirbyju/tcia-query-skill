# TCIA NIfTI Metadata

TCIA's WordPress snapshot is the authority for whether a NIfTI package is a current, visible, TCIA-published download. The optional NIfTI SQLite release adds file-grain metadata mined from current visible, non-controlled NIfTI download records, companion spreadsheets, root `.sums` files, and accepted Aspera package listings.

Use this reference when a user asks about TCIA NIfTI file counts, NIfTI modalities, NIfTI filenames, package inventories, or NIfTI segmentation/source-image relationships.

## Optional Release Asset

The NIfTI SQLite is intentionally not downloaded when the skill is installed or when the normal TCIA snapshot is refreshed. Only users or agents that need NIfTI file-grain metadata should download it:

```bash
python scripts/tcia_nifti_metadata.py ensure
```

Default local cache paths:

```text
cache/nifti_metadata.sqlite
cache/nifti_metadata_manifest.json
```

Override the SQLite path with:

```bash
export TCIA_NIFTI_METADATA_DB=/path/to/nifti_metadata.sqlite
```

Release URLs:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/nifti_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-snapshot-latest/nifti_metadata_manifest.json`

## When To Use

Use the normal TCIA snapshot first to confirm TCIA provenance, visibility, access level, and user-facing download URLs. Then use the NIfTI SQLite only for file-level questions.

Good NIfTI SQLite use cases:

- Count NIfTI files by dataset.
- List NIfTI package paths.
- Find which NIfTI rows have MR or CT metadata.
- Inspect best-effort IDC-like radiology fields for NIfTI files.
- Find segmentation objects and their inferred source-image references.
- Audit sidecars or package metadata files present in accepted package listings.

Do not use this SQLite to authorize controlled data, replace WordPress licensing metadata, or infer clinical truth. Hidden and controlled records are excluded by design.

## Helper Script

Run from the skill root:

```bash
python scripts/tcia_nifti_metadata.py ensure
python scripts/tcia_nifti_metadata.py info
python scripts/tcia_nifti_metadata.py datasets --limit 20
python scripts/tcia_nifti_metadata.py files --collection UCSF-PDGM --limit 10
python scripts/tcia_nifti_metadata.py files --collection CT-ORG --modality CT
python scripts/tcia_nifti_metadata.py derived --collection BCBM-RadioGenomics --with-sources
```

The helper downloads and verifies `nifti_metadata.sqlite.gz` only when `ensure` is run. Query commands expect the local SQLite to exist and will ask the user to run `ensure` if it does not.

Maintainer commands:

```bash
python scripts/tcia_nifti_metadata.py validate --db outputs/nifti_metadata/nifti_metadata.sqlite
python scripts/tcia_nifti_metadata.py manifest \
  --db outputs/nifti_metadata/nifti_metadata.sqlite \
  --gzip outputs/nifti_metadata/nifti_metadata.sqlite.gz \
  --snapshot-db cache/tcia_snapshot.sqlite \
  --out outputs/nifti_metadata/nifti_metadata_manifest.json
python scripts/tcia_nifti_metadata.py drift-check \
  --snapshot-db dist/tcia_snapshot.sqlite \
  --manifest previous/nifti_metadata_manifest.json
```

## Core Tables

Prefer the `agent_*` views for routine querying:

| View | Use |
| --- | --- |
| `agent_nifti_downloads` | WordPress NIfTI download provenance with `download_label` fallback text when a source title is missing. |
| `agent_nifti_dataset_summary` | Dataset summary across all NIfTI downloads, including datasets whose files do not collapse into `radiology_series` rows. |
| `agent_nifti_files` | Canonical one-row-per-NIfTI logical radiology file/series table with lower-snake-case UID aliases. |
| `agent_nifti_derived_objects` | Probable NIfTI segmentation objects and best-effort source-image references. |

| Table | Use |
| --- | --- |
| `nifti_downloads` | WordPress/Collection Manager NIfTI download provenance. |
| `candidate_downloads` | NIfTI package, spreadsheet, ZIP, and metadata-hint download candidates for NIfTI datasets. |
| `package_files` | Accepted Aspera package listing rows, preserving raw `ascli` CSV rows in `row_json`. |
| `aspera_root_sums_inventory` | Parsed root package `.sums` rows. |
| `normalized_series_rows` | Source spreadsheet row-level normalized IDC-like fields. Repeated rows may exist before file-level collapse. |
| `nifti_file_series` | Collapsed one-row-per-NIfTI-file table with IDC-like series columns. |
| `non_dicom_files` | Canonical file inventory, including NIfTI files, sidecars, and package metadata rows. |
| `radiology_series` | Preferred canonical one-row-per-NIfTI logical radiology file/series table. |
| `radiology_mr`, `radiology_ct`, `radiology_pet`, `radiology_contrast` | Modality-specific extension tables. |
| `derived_objects` | Probable NIfTI segmentation objects. |
| `derived_object_references` | Best-effort source-image links for derived objects. |
| `metadata_quality_flags` | Preserved source-row flags for known bad/missing rows. |

## Common Queries

List datasets and NIfTI file counts:

```sql
SELECT short_title, nifti_downloads, nifti_files,
       radiology_series_rows, mr_files, ct_files
FROM agent_nifti_dataset_summary
ORDER BY lower(short_title);
```

Find NIfTI files for one collection:

```sql
SELECT short_title, file_name, modality, subject_id, package_path
FROM agent_nifti_files
WHERE short_title = 'UCSF-PDGM'
ORDER BY package_path
LIMIT 20;
```

Find MR NIfTI rows with acquisition metadata:

```sql
SELECT r.short_title, r.file_name, m.echo_time_ms, m.repetition_time_ms
FROM radiology_series r
JOIN radiology_mr m USING (radiology_id)
WHERE r.modality = 'MR'
  AND (m.echo_time_ms <> '' OR m.repetition_time_ms <> '')
LIMIT 20;
```

Find CT NIfTI rows:

```sql
SELECT short_title, file_name, package_path
FROM agent_nifti_files
WHERE modality = 'CT'
ORDER BY short_title, file_name
LIMIT 20;
```

Find segmentations and their source files:

```sql
SELECT
  d.short_title,
  d.file_name AS derived_file,
  d.segmentation_representation,
  dor.referenced_file_name AS source_file,
  dor.confidence,
  dor.inference_method
FROM derived_objects d
LEFT JOIN derived_object_references dor
  ON dor.derived_object_id = d.derived_object_id
WHERE d.short_title = 'BCBM-RadioGenomics'
ORDER BY d.file_name, dor.referenced_file_name
LIMIT 20;
```

Find derived objects without source-image references:

```sql
SELECT d.short_title, COUNT(*) AS unlinked_derived_objects
FROM derived_objects d
LEFT JOIN derived_object_references dor
  ON dor.derived_object_id = d.derived_object_id
WHERE dor.derived_object_id IS NULL
GROUP BY d.short_title
ORDER BY d.short_title;
```

Inspect sidecars and package metadata files:

```sql
SELECT short_title, file_name, file_role, package_path
FROM non_dicom_files
WHERE is_nifti = 0
ORDER BY short_title, package_path
LIMIT 50;
```

## Interpretation Notes

The NIfTI SQLite is a mined metadata layer, not a submitter-authored canonical submission model. Missing fields are expected.

Some NIfTI downloads contain segmentations, masks, radiomic features, or package metadata that may not produce a `radiology_series` row. Use `agent_nifti_dataset_summary.nifti_downloads` for all scoped NIfTI downloads and `radiology_series_rows`/`nifti_files` for mined file-grain coverage.

`radiology_series.study_id` and `radiology_series.series_id` use source UIDs when available. When source UIDs are missing, deterministic synthetic IDs are generated from dataset/path context; inspect `study_id_source` and `series_id_source`.

`derived_objects.derived_object_type` is normalized to `segmentation` for current NIfTI-derived rows. Use `segmentation_representation` for the representation hint:

- `binary_mask`: usually one ROI/class per file.
- `labelmap`: usually one image where integer values encode labels/classes.
- `segmentation_file`: generic segmentation object, often from filenames such as `*-seg.nii.gz`.

`derived_object_references` is heuristic unless `inference_method` indicates an explicit source identifier. The current harvest mostly uses filename/path rules and records `confidence`, `inference_method`, and `evidence_json` so downstream users can decide how much to trust each link.

For packages where TCIA published only segmentation NIfTI files and not the source images as NIfTI, source-image linkage may require DICOM/IDC/NBIA metadata outside this NIfTI SQLite.

## Refresh Strategy

Do not rerun the full Aspera harvest on every scheduled snapshot build. The first baseline harvest is expensive.

Scheduled workflow should:

1. Build the normal TCIA WordPress/PathDB/DataCite snapshot.
2. Download only `nifti_metadata_manifest.json` from the release.
3. Run `scripts/tcia_nifti_metadata.py drift-check` against the fresh snapshot.
4. Warn maintainers if current visible, non-controlled NIfTI download records no longer match the released NIfTI manifest.

When drift is detected, run a manual maintainer refresh using the existing release SQLite and harvested file caches as the baseline, then upload refreshed `nifti_metadata.sqlite.gz` and `nifti_metadata_manifest.json` release assets.
