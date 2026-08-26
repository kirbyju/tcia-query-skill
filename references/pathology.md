# TCIA Pathology Metadata

TCIA WordPress metadata is authoritative for current public pathology download
scope. PathDB provides slide metadata and browser-viewing enrichment. Unified
V2 public non-DICOM detail is the file-level query artifact; the standalone
pathology SQLite is retired from releases and routine production.

## Install V2 Detail

```bash
python scripts/tcia_v2_bundle.py install --profile research_detail
python scripts/tcia_v2_bundle.py install --profile audit_support
```

Use `public_non_dicom_metadata.sqlite` for current logical assets, participants,
formats, roles, and managed locations. Use `public_non_dicom_audit.sqlite` for
verbose provenance, reconciliation evidence, and the immutable specialized
checkpoint. Both are pinned by `tcia_metadata_v2_bundle_manifest.json`.

## Routine Queries

```bash
python scripts/tcia_public_non_dicom_metadata.py files \
  --db cache/tcia-metadata-v2-latest/public_non_dicom_metadata.sqlite \
  --collection CPTAC-CCRCC --limit 20
```

```sql
SELECT short_title, subject_id, file_name, file_format, source_system,
       source_url, representation_provenance_class
FROM agent_public_non_dicom_assets
WHERE imaging_domain = 'pathology'
ORDER BY short_title, subject_id, file_name;
```

For PathDB-specific slide metadata and caMicroscope URLs, use
`scripts/pathdb_metadata.py` and the stable cohort-builder CSV. Construct viewer
URLs from `camic_id`, not `slide_id`.

## Retained Specialized Checkpoint

The audit companion retains the exact last parity-validated rows in:

- `source_pathology__pathology_download_label_matches`
- `source_pathology__pathology_package_files`
- `source_pathology__pathdb_slide_crosswalk`
- `source_pathology__pathology_disparities`

These are historical audit evidence. Current WordPress aggregates and PathDB
file rows are refreshed directly into unified V2; the checkpoint is not rebuilt
from a standalone pathology database.

## Interpretation

- WordPress decides TCIA publication scope, visibility, access level, license,
  DOI, and the user-facing download route.
- PathDB is best-effort enrichment and may not cover every package file.
- PathDB copies may be converted or reformatted for browser viewing. Do not
  imply byte equivalence with the Aspera package.
- The Aspera package is the original submitter-provided copy and is preferred
  for analyses requiring exact submitted files.
- `collection_only` PathDB matches establish dataset scope, not file identity.
  File identity requires exact URL, filename plus collection, a reviewed slide
  pattern, or equivalent explicit evidence.
- Hidden and controlled downloads are excluded from the public artifact and
  must not be authorized through this metadata.

New package inventories or reconciliation corrections should be added as
reviewed V2 references or native adapters with source checksums, observation
dates, and additive QC fields. The legacy pathology builders remain only for
migration forensics and reproducibility; do not publish their SQLite outputs.
