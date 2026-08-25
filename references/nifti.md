# TCIA NIfTI Metadata

TCIA's WordPress snapshot is the authority for whether a NIfTI package is a current, visible, TCIA-published download. The moving V2 release exposes file-grain NIfTI discovery through the unified public non-DICOM research artifact and retains specialized source tables inside its audit companion.

Use this reference when a user asks about TCIA NIfTI file counts, NIfTI modalities, NIfTI filenames, package inventories, or NIfTI segmentation/source-image relationships.

## Install V2 Detail

Install the manifest-pinned V2 detail and audit profiles from the skill root:

```bash
python scripts/tcia_v2_bundle.py install --profile research_detail
python scripts/tcia_v2_bundle.py install --profile audit_support
```

The file-grain research rows are installed at:

```text
cache/tcia-metadata-v2-latest/public_non_dicom_metadata.sqlite
```

Specialized retained NIfTI source rows and QC evidence are installed in
`public_non_dicom_audit.sqlite` by the audit profile. Both databases are pinned
by `tcia_metadata_v2_bundle_manifest.json`.

## When To Use

Use the normal TCIA snapshot first to confirm TCIA provenance, visibility, access level, and user-facing download URLs. Then use the unified public non-DICOM artifact for file-level questions and the audit companion for specialized source rows and QC.

Good V2 public non-DICOM use cases:

- Count NIfTI files by dataset.
- List NIfTI package paths.
- Find which NIfTI rows have MR or CT metadata.
- Inspect best-effort IDC-like radiology fields for NIfTI files.
- Find segmentation objects and their inferred source-image references.
- Audit sidecars or package metadata files present in accepted package listings.

Do not use this SQLite to authorize controlled data, replace WordPress licensing metadata, or infer clinical truth. Hidden and controlled records are excluded by design.

## Query Examples

Run from the skill root:

```bash
python scripts/tcia_public_non_dicom_metadata.py datasets --db cache/tcia-metadata-v2-latest/public_non_dicom_metadata.sqlite --limit 20
python scripts/tcia_public_non_dicom_metadata.py files --db cache/tcia-metadata-v2-latest/public_non_dicom_metadata.sqlite --collection UCSF-PDGM --limit 10
python scripts/tcia_public_non_dicom_metadata.py files --db cache/tcia-metadata-v2-latest/public_non_dicom_metadata.sqlite --collection CT-ORG --limit 10
```

The specialized `tcia_nifti_metadata.py` commands below are for maintainers
building and validating source inputs, not for downloading end-user artifacts:

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
| `agent_nifti_derived_objects` | Probable NIfTI segmentations, annotations, and transformed images with best-effort source-image references. |
| `agent_nifti_characteristics` | Reviewed object role, associated imaging modality, preferred source NIfTI volume, total source-reference count, source dataset/access level/DICOM UIDs, alternate DICOM SEG representation, WordPress-label provenance, and canonical study grouping. It covers every current NIfTI dataset in the release. |
| `agent_nifti_characteristics_summary` | Unambiguous source-image, segmentation, transformed-image, and fiducial counts for datasets processed through the reviewed characteristics layer. |
| `agent_nifti_review_issues` | One row per unresolved dataset-level QC question, with affected-file count, severity, status, description, and evidence. |

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
| `derived_object_references` | Best-effort provenance links with separate source NIfTI volume identifiers, source dataset/DICOM identifiers, and role-specific related-DICOM identifiers. |
| `nifti_classification_rules` | One reviewed rule per dataset/download classification, including file patterns, provenance, confidence, and WordPress download linkage. |
| `nifti_file_characteristics` | Compact per-file assignments to a reviewed classification rule, with object role and associated imaging modality. |
| `nifti_dataset_review_issues` | Compact dataset-level manual-review queue; issues are not repeated in every file row. |
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

Find reviewed CT source images without mixing in CT-associated segmentations:

```sql
SELECT short_title, subject_id, file_name, associated_imaging_modality,
       study_id, study_id_source, classification_source
FROM agent_nifti_characteristics
WHERE associated_imaging_modality = 'CT'
  AND object_role = 'source_image'
ORDER BY short_title, subject_id, file_name;
```

Find non-DICOM segmentations associated with CT source images:

```sql
SELECT short_title, subject_id, file_name,
       segmentation_representation, source_nifti_volume_file_name,
       source_dicom_series_instance_uid,
       reference_confidence
FROM agent_nifti_characteristics
WHERE associated_imaging_modality = 'CT'
  AND object_role = 'segmentation'
ORDER BY short_title, subject_id, file_name;
```

Find segmentations and their source files:

```sql
SELECT
  d.short_title,
  d.file_name AS derived_file,
  d.segmentation_representation,
  dor.source_nifti_volume_file_name,
  dor.source_dicom_series_instance_uid,
  dor.confidence,
  dor.inference_method
FROM derived_objects d
LEFT JOIN derived_object_references dor
  ON dor.derived_object_id = d.derived_object_id
WHERE d.short_title = 'BCBM-RadioGenomics'
ORDER BY d.file_name, dor.source_nifti_volume_file_name
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

`agent_nifti_files.subject_id` / `radiology_series.subject_id` prefer a submitter-provided `PatientID` from parsed companion metadata. When no submitter `PatientID` exists, refreshed releases may fill a conservative path-derived identifier from common NIfTI package layouts. These inferred IDs are intended as file grouping keys, not clinical truth; inspect `metadata_sources` for `path_patient_id:*` provenance before relying on them.

`derived_objects.derived_object_type` distinguishes segmentations, annotations, and transformed images when the package supplies enough evidence. Use `segmentation_representation` for the current representation hint:

- `binary_mask`: usually one ROI/class per file.
- `labelmap`: usually one image where integer values encode labels/classes.
- `segmentation_file`: generic segmentation object, often from filenames such as `*-seg.nii.gz`.

`derived_object_references` is heuristic unless `inference_method` indicates an explicit source identifier. The current harvest mostly uses filename/path rules and records `confidence`, `inference_method`, and `evidence_json` so downstream users can decide how much to trust each link.

One derived file can reference multiple source volumes. `derived_object_references` preserves every supported link. `agent_nifti_characteristics` remains one row per reviewed file: it exposes a preferred source only when the highest-confidence candidate is unique and reports the full number in `source_reference_count`. A null preferred source with a count greater than one means the relationship is genuinely multi-source, not missing.

WordPress `file_type`, `data_type`, and `download_type` labels remain distinct. `NIfTI` stays at download grain in `nifti_downloads`; it is implicit for reviewed NIfTI files and is not repeated in every file or classification-rule row. `CT`/`MR`/`PT` populate `associated_imaging_modality`, while generic `Segmentation` is a format-independent content type. DICOM-specific values such as `SEG` or `RTSTRUCT` must not be assigned to NIfTI objects.

For a derived NIfTI object, `source_nifti_volume_id` and `source_nifti_volume_file_name` identify the related NIfTI image volume when that volume is represented in this SQLite. Explicit provenance to original DICOM is stored separately in `source_dicom_series_instance_uid` and `source_dicom_study_instance_uid`; synthetic NIfTI identifiers must never be placed in those UID fields. `associated_imaging_modality` applies regardless of whether the source representation is NIfTI or DICOM, so no second DICOM-modality field is used.

When source imaging is external to the NIfTI package, `source_dataset_short_title` records the TCIA source dataset, `source_access_level` records whether that source is open or controlled, and the source DICOM UID fields identify the exact source image when available. Source access is independent of the NIfTI download's access: an open derived file can legitimately reference a controlled source image. A DICOM SEG version of the same segmentation is not a source image: it is stored as a separate `alternate_dicom_segmentation_representation` relationship and exposed as `alternate_dicom_seg_series_instance_uid` / `alternate_dicom_seg_study_instance_uid`. This prevents a DICOM SEG UID from being mistaken for the CT or MR series analyzed to create the segmentation.

CT-ORG is the first dataset processed through `nifti_classification_rules` and `nifti_file_characteristics`. Its WordPress NIfTI download declares `CT` and `Segmentation`; `volume-N.nii.gz` rows are reviewed as CT `source_image` objects, while `labels-N.nii.gz` rows are NIfTI `segmentation` objects associated with their paired CT source images. CT-ORG uses one canonical synthetic `study_id` per subject pair, with `study_id_source = 'synthetic_from_subject_id'`. The earlier parent-directory ID was a generated modeling error rather than source metadata and is not retained.

BCBM-RadioGenomics is the second reviewed dataset. Its WordPress download declares `MR`, `Segmentation`, and `NIfTI`; `*_image_ss_n4.nii.gz` rows are reviewed as MR `source_image` objects and `*_mask_*.nii.gz` rows as MR-associated binary-mask segmentations. Every reviewed mask must have exactly one high-confidence `mask_prefix_same_folder` source link. The package's 268 parent-directory identifiers are scan IDs, not patients: the reviewed rule removes exactly the final numeric scan suffix to produce 165 patient IDs and preserves the full raw scan ID as `procedure_id` plus provenance. The scan-specific `synthetic_from_parent_path` study IDs remain intact.

Vestibular-Schwannoma-MC-RC2 is the third reviewed dataset and introduces longitudinal study grouping. Its filenames encode subject, scan date, and acquisition type (`T1`, `T1C`, or `T2`); `T1C_seg` files reference the same subject/date `T1C` source NIfTI volume. The dataset has 190 subjects but 621 subject/date imaging sessions, so canonical synthetic study IDs use both subject and scan date. T1, T1C, T2, and segmentation files from one visit share a study; different dates for the same subject do not.

NLST-New-lesion-LongCT is the fourth reviewed dataset and introduces two additional distinctions. Its WordPress download declares `CT`, `Fiducial`, and `NIfTI`, so `point_N.nii.gz` files are `fiducial_annotation` objects rather than segmentations. The current package inventory contains 750 NIfTI files: 242 source CT volumes, 241 resampled CT volumes, 122 longitudinal registered CT volumes, and 145 point-fiducial volumes. A transfer-list spreadsheet also names 998 historical/intermediate paths, including 242 lung masks, but those paths are retained only as metadata and crosswalk evidence when they are absent from the current package inventory. The reviewed counts therefore do not double-count them or mislabel the lung masks as the advertised point annotations. Source CT rows are crosswalked to the spreadsheet's DICOM Study/Series Instance UIDs; resampled and point volumes link to one source CT, while registered volumes preserve both longitudinal source links.

Radiomic-Feature-Standards is the fifth reviewed dataset and is segmentation-only at NIfTI grain. Its 10 LIDC-IDRI and 3 DRO phantom NIfTI segmentations have no source NIfTI volumes in the package. The Analysis Result's two “Collections used” manifests provide 13 CT Series Instance UIDs and 13 DICOM SEG Series Instance UIDs. IDC DICOMweb metadata verifies an exact PatientID and StudyInstanceUID match for every pair. The reviewed layer therefore associates each NIfTI segmentation with its source CT dataset/series and separately records the DICOM SEG as an alternate representation. Phantom PatientIDs are recovered from the `SEG-Phantom-*` filenames, producing 13 subjects and 13 source studies rather than collapsing the three root-level phantom files into one synthetic study.

Healthy-Total-Body-CTs is the sixth reviewed dataset and introduces mixed-access provenance. The open download contains 30 automatic MOOSE whole-body NIfTI segmentations derived from the controlled collection's 90-minute CT timepoint. Twenty-six filename subject IDs match controlled metadata exactly and receive explicit source CT Series/Study Instance UIDs. Four NIfTI IDs (`014`, `016`, `019`, and `029`) do not match a controlled subject ID; they retain a controlled dataset-level source relationship and a manual-review flag, but no UID or renumbering is guessed. The result has 30 segmentation rows and 30 studies: 26 use explicit source Study Instance UIDs and four use subject-based synthetic study IDs. `source_access_level = 'controlled'` describes the source CT relationship and does not change the open status of the NIfTI download.

For longitudinal queries, `agent_nifti_characteristics` exposes `study_date`, `series_date`, `series_description`, and `file_metadata_sources`. In this dataset the dates and acquisition descriptions are parsed from filenames, so inspect `file_metadata_sources` rather than treating them as DICOM-header metadata.

The reviewed characteristics layer covers all current NIfTI datasets. The remaining dataset rules add multimodal MR/CT/radiotherapy packages, segmentation-only packages, quantitative and processed imaging volumes, challenge identifiers, filename-encoded longitudinal visits, and exact source relationships recovered from preserved support tables. Use `agent_nifti_review_issues` for the intentionally unresolved remainder rather than inferring missing links.

List open manual-review issues:

```sql
SELECT short_title, issue_code, affected_files, description
FROM agent_nifti_review_issues
WHERE status = 'manual_review'
ORDER BY lower(short_title), issue_code;
```

For packages where TCIA published only segmentation NIfTI files and not the source images as NIfTI, source-image linkage may require DICOM/IDC/NBIA metadata outside this NIfTI SQLite.

## Refresh Strategy

Do not rerun the full Aspera harvest on every scheduled snapshot build. The first baseline harvest is expensive.

Scheduled workflow should:

1. Build the normal TCIA WordPress/PathDB/DataCite snapshot.
2. Download only `nifti_metadata_manifest.json` from the release.
3. Run `scripts/tcia_nifti_metadata.py drift-check` against the fresh snapshot.
4. Warn maintainers if current visible, non-controlled NIfTI download records no longer match the released NIfTI manifest.

When drift is detected, rebuild the unified public non-DICOM artifact and its checkpoint-backed audit companion; do not add the retired standalone NIfTI assets back to the moving V2 contract.

For patient-coverage refreshes, include Aspera metadata files as well as NIfTI package listings when the runtime can safely browse/download the packages:

```bash
python scripts/tcia_nifti_metadata_harvest.py \
  --source-db cache/tcia_snapshot.sqlite \
  --out-db outputs/nifti_metadata/nifti_metadata.sqlite \
  --files-dir outputs/nifti_metadata/files \
  --include-aspera \
  --no-aspera-nifti-only \
  --download-aspera-metadata-files \
  --reuse-cache \
  --replace
```

This is a manual maintainer workflow, not a routine scheduled job, because Aspera package browsing can be slow and may download large companion spreadsheets.
