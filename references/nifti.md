# TCIA NIfTI Metadata

TCIA WordPress metadata is authoritative for whether a NIfTI package is a
current, visible TCIA download. File-level discovery is provided only through
the unified V2 public non-DICOM artifact. The standalone NIfTI SQLite is
retired and is neither a release asset nor a routine producer input.

## Install V2 Detail

```bash
python scripts/tcia_v2_bundle.py install --profile research_detail
python scripts/tcia_v2_bundle.py install --profile audit_support
```

Use:

- `public_non_dicom_metadata.sqlite` for dataset, file, participant, format,
  modality, role, location, relationship, and normalized image metadata.
- `public_non_dicom_audit.sqlite` for verbose provenance, crosswalk evidence,
  QC, and the immutable specialized checkpoint established at retirement.

Both are pinned by `tcia_metadata_v2_bundle_manifest.json`.

## Routine Queries

```bash
python scripts/tcia_public_non_dicom_metadata.py datasets \
  --db cache/tcia-metadata-v2-latest/public_non_dicom_metadata.sqlite
python scripts/tcia_public_non_dicom_metadata.py files \
  --db cache/tcia-metadata-v2-latest/public_non_dicom_metadata.sqlite \
  --collection UCSF-PDGM --limit 20
```

```sql
SELECT short_title, subject_id, file_name, package_path, modality, object_role
FROM agent_public_non_dicom_assets
WHERE file_format = 'NIFTI'
  AND short_title = 'BCBM-RadioGenomics'
ORDER BY subject_id, package_path;
```

```sql
SELECT a.short_title, a.subject_id, a.file_name, m.metadata_json
FROM agent_public_non_dicom_assets a
LEFT JOIN agent_public_non_dicom_image_metadata m USING(asset_id)
WHERE a.file_format = 'NIFTI' AND a.object_role = 'segmentation'
ORDER BY a.short_title, a.subject_id, a.file_name;
```

Use `public_non_dicom_asset_relationships` only when relationship evidence is
present. Do not infer a source image from filename proximity alone.

## Retained Specialized Checkpoint

The audit companion retains the exact last parity-validated rows in:

- `source_nifti__derived_objects`
- `source_nifti__derived_object_references`
- `source_nifti__nifti_classification_rules`
- `source_nifti__nifti_file_characteristics`
- `source_nifti__nifti_dataset_review_issues`
- `source_nifti__metadata_quality_flags`
- `source_nifti__annotation_groups`

These tables are historical audit evidence. They are not refreshed from a
standalone NIfTI artifact. New corrections belong in reviewed V2 references or
native V2 source adapters and must preserve source values and provenance.

## Reviewed Dataset Contracts

- BraTS 2021 preserves every `BraTS2021_*` challenge alias but projects mapped
  cases to their original TCIA collection PatientID. The known missing
  BraTS-TCGA test sets remain out of scope until TCIA publishes them.
- BCBM-RadioGenomics removes only the final numeric scan suffix to resolve 268
  scan identifiers to 165 patients. The full scan identifier remains available
  as procedure metadata and provenance.
- ReMIND NRRD segmentations use a checked-in, checksum-pinned package inventory;
  356 files cover all 114 subjects, while 113 subjects have the preoperative
  whole-tumor segmentation represented by the WordPress summary count.
- Longitudinal datasets must keep visit/date information in study grouping; a
  participant identifier alone is not a study identifier.
- DICOM UIDs and alternate DICOM representations must remain separate from the
  non-DICOM file location and representation.

## Interpretation

Missing fields are expected. Path-derived identifiers and filename-derived
roles are grouping aids, not clinical truth. Inspect participant link status,
field provenance, confidence, and QC evidence before relying on inferred
metadata. WordPress access/license metadata and IDC public-DICOM metadata stay
authoritative for their respective scopes.

The legacy `tcia_nifti_metadata.py`, harvesters, and parity script remain only
for migration forensics and reproducibility. Do not add their SQLite outputs
back to a moving release or routine workflow.
