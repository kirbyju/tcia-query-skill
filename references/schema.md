# SQLite Snapshot Schema

Use this reference when querying `cache/tcia_snapshot.sqlite` or a database selected by `TCIA_SNAPSHOT_DB`.

The GitHub Release web exports mirror the most useful agent-facing views for environments that cannot run SQLite. Use `agent_datasets.jsonl` or `agent_datasets.jsonl.gz` for `agent_dataset_access_summary`, `agent_current_downloads.jsonl` or `agent_current_downloads.jsonl.gz` for `agent_current_downloads`, `agent_dataset_versions.jsonl` or `.gz` for matched version history, and `agent_dataset_v1_releases.jsonl` or `.gz` for first-release dates. Prefer plain `.jsonl` for web LLM browse tools that cannot decompress gzip. Filter these generic JSONL tables for controlled/mixed access, modalities, DICOM annotation labels, download routes, or release dates instead of relying on prompt-specific precomputed exports.

The optional NIfTI file-grain SQLite is separate from `cache/tcia_snapshot.sqlite`. It is downloaded only when needed with `python scripts/tcia_nifti_metadata.py ensure`, defaults to `cache/nifti_metadata.sqlite`, and is documented in `references/nifti.md`.

The V2 public non-DICOM and Participant Inventory contracts are documented in
`references/artifact-model-v2.md`. They use a separate preview release line,
profile-based downloads, and optional audit companions; they do not replace the
existing sidecars during migration.

During a full build, reviewed and automated public non-DICOM crosswalks are
exposed by `agent_public_non_dicom_crosswalk_evidence` at mapped-file grain and
`agent_public_non_dicom_crosswalk_decisions` at decision grain. Published
research-detail artifacts retain compact linkage and coverage states but move
the detailed rows to `public_non_dicom_audit.sqlite.gz`; query its
`public_non_dicom_crosswalk_evidence` and
`public_non_dicom_crosswalk_decisions` tables for raw and resolved participant
IDs, mapping method, confidence, evidence URL, reviewer note, and optional
DICOM Series Instance UID.
Automated decisions use `decision_status='accepted_automated'` and record
`human_reviewed=false` in provenance; they require complete, unambiguous
download coverage. Partial candidates remain in the review-issues view.

Public non-DICOM schema v6 represents participant cardinality in
`public_non_dicom_asset_participants` and exposes
`agent_public_non_dicom_asset_participants`. Query that junction for file-level
Participant Explorer drill-downs, including shared assets such as HANCOCK whole
TMA block slides. The scalar asset `subject_id` remains a convenience for
one-participant files and is intentionally empty for multi-participant assets.
`represented_file_count` and the agent-view `represented_files` totals support
compact participant/modality summaries when retaining every source file as an
asset row would be wasteful. The initial use is the reviewed BraTS 2021 public
DICOM trees that are available through Aspera but absent from IDC; these rows
remain distinguishable with `file_format='DICOM'` and
`source_system='tcia_aspera'`.

The optional pathology Aspera SQLite is also separate from `cache/tcia_snapshot.sqlite`. It is downloaded only when needed with `python scripts/tcia_pathology_metadata.py ensure`, defaults to `cache/pathology_metadata.sqlite`, and is documented in `references/pathology.md`.

The optional controlled-access SQLite is also separate from `cache/tcia_snapshot.sqlite`. It is downloaded only when needed with `python scripts/tcia_controlled_access_metadata.py ensure`, defaults to `cache/controlled_access_metadata.sqlite`, and is documented in `references/controlled-access.md`.

The optional patient-level clinical SQLite is separate from
`cache/tcia_snapshot.sqlite`. Download it with
`python scripts/tcia_clinical_metadata.py ensure`; it defaults to
`cache/clinical_metadata.sqlite` and is documented in
`references/clinical.md`.

## Agent-Facing Views

Prefer these views for normal discovery. They flatten common JSON fields and keep the base tables available as lower-level provenance.

### `agent_datasets`

One row per TCIA WordPress Collection or Analysis Result.

Key columns:

- `source`: `collections` or `analysis-results`.
- `dataset_type`: `Collection` or `Analysis Result`.
- `short_title`: WordPress short title; use this as the cross-system key.
- `short_title_key`: normalized short-title join key used internally for version history matching across punctuation/case differences.
- `title`, `doi`, `link`, `date_updated`, `hidden`.
- `current_version_number`: current Collection Manager version number, when exposed by WordPress.
- `license_status`, `licenses`, `controlled_access`, `noncommercial_license`.
- `access_level`: `open`, `open_noncommercial`, `controlled`, `mixed`, `review_needed`, or `unknown`.
- `controlled_access_policy_url`: populated when the dataset-level license is controlled.
- `subjects`, `data_types`, `download_types`, `download_data_types`, `download_file_types`.
- `external_resources`: semicolon-delimited top-level Collection or Analysis Result external-resource labels.
- `external_resource_labels`: JSON array of those top-level external-resource labels.
- `cancer_types`, `cancer_locations`, `species`, `program`.
- `has_tcia_clinical_download`, `has_external_clinical_resource`.
- `source_collections`: populated for some Analysis Results; use it as strong relationship evidence when mapping an Analysis Result back to source Collections.

Default user-facing filter:

```sql
SELECT short_title, title, access_level, doi, link
FROM agent_datasets
WHERE hidden = 0
ORDER BY short_title;
```

### `agent_current_downloads`

One row per current nested `collection_downloads` or `result_downloads` record. This excludes historical/global WordPress download endpoint rows.

Key columns:

- `short_title`, `title`, `dataset_type`, `hidden`.
- `download_id`, `download_title`, `download_url`, `download_metadata`, `search_url`.
- `download_types`, `data_types`, `file_types`, `external_resources`: JSON arrays as text.
- `license_label`, `license_url`, `requirements_label`, `requirements_url`, `requirements_text`.
- `subjects`, `studies`, `series`, `images`, `download_size`, `download_size_unit`.
- `access_level`, `controlled_access`, `noncommercial_license`, `controlled_access_policy_url`.

Find current MR downloads:

```sql
SELECT DISTINCT d.short_title, d.download_title, d.access_level, d.download_url
FROM agent_current_downloads d
JOIN wordpress_download_labels l
  ON l.download_row_id = d.download_row_id
WHERE d.hidden = 0
  AND l.label_kind = 'data_type'
  AND l.label = 'MR';
```

### `agent_dataset_access_summary`

One row per dataset with download-level access counts. Use this view when `access_level` is `mixed` or when deciding whether a download answer must split open and controlled files.

Additional columns:

- `current_download_count`.
- `controlled_download_count`.
- `noncontrolled_download_count`.
- `open_noncommercial_download_count`.
- `controlled_download_titles`.
- `controlled_license_labels`.
- `controlled_download_ids`.
- `controlled_download_urls`.
- `resolved_access_level`: download-aware access level.
- `resolved_controlled_access_policy_url`.

Mixed access rule:

- `resolved_access_level = 'mixed'` means some current downloads are controlled and others are not.
- Do not imply the whole dataset is open.
- Separate open/noncontrolled downloads from controlled downloads in the answer.
- For controlled downloads, provide the TCIA NIH Controlled Data Access Policy link and explain dbGaP authorization. If the user explicitly requests a transfer and supplies their valid JSON-key path, use the official CRDC manifest with TCIA Data Retriever as described in `controlled-access.md`; otherwise provide manifest guidance only.

Inspect a mixed dataset:

```sql
SELECT
  short_title,
  resolved_access_level,
  controlled_download_count,
  noncontrolled_download_count,
  controlled_download_titles,
  resolved_controlled_access_policy_url
FROM agent_dataset_access_summary
WHERE hidden = 0
  AND resolved_access_level = 'mixed';
```

### `agent_dataset_versions`

One row per current TCIA dataset matched to a `/api/v2/versions` record. The match uses exact short titles when possible and a normalized fallback key that strips punctuation and case. This matters for historical records where the version endpoint may use legacy related short titles such as `CT-IMAGES-IN-COVID-19` while the current Collection page uses `CT Images in COVID-19`.

Key columns:

- Dataset columns: `source`, `dataset_type`, `short_title`, `title`, `doi`, `link`, `date_updated`, `current_version_number`, `subjects`, `hidden`.
- Version columns: `version_id`, `version_slug`, `version_post_title`, `version_number`, `version_date`, `version_related_short_title`.
- `match_method`: `exact_short_title` or `normalized_short_title`.
- `version_downloads`, `version_text`, `version_normalized_json`, `version_raw_json`.

Find the version history for a dataset:

```sql
SELECT short_title, version_number, version_date, version_post_title, match_method
FROM agent_dataset_versions
WHERE hidden = 0
  AND short_title = 'CT Images in COVID-19'
ORDER BY CAST(NULLIF(version_number, '') AS INTEGER), version_date;
```

### `agent_dataset_v1_releases`

One row per current TCIA dataset with the best available version 1 release date. The view prefers matched `/api/v2/versions` rows with `version_number = '1'`. If no matched v1 row exists and the current dataset is still version 1, it falls back to the current record's `date_updated`.

Key columns:

- Dataset columns: `source`, `dataset_type`, `short_title`, `title`, `doi`, `link`, `date_updated`, `current_version_number`, `subjects`, `hidden`.
- `v1_release_date`: best available first-release date.
- `v1_release_date_source`: `versions_endpoint_exact_short_title`, `versions_endpoint_normalized_short_title`, `current_record_still_v1_date_updated`, or blank when no v1 date can be inferred.
- Version provenance: `version_id`, `version_slug`, `version_post_title`, `version_related_short_title`, `match_method`.

Find visible datasets first released since a given date:

```sql
SELECT dataset_type,
       short_title,
       title,
       v1_release_date,
       v1_release_date_source,
       subjects,
       link
FROM agent_dataset_v1_releases
WHERE hidden = 0
  AND v1_release_date >= '2025-01-01'
ORDER BY v1_release_date, lower(short_title);
```

### `agent_pathdb_slides`

One row per PathDB cohort-builder slide row.

Key columns:

- `collection`: TCIA WordPress short title.
- `patient_id`, `slide_id`, `camic_id`.
- `camicroscope_url`: built from `camic_id`.
- `wsiimage_url`, `species`, `cancer_type`, `cancer_location`, `data_format`, `modality`.

For caMicroscope URLs, use `camic_id`. The URL parameter is named `slideId`, but it expects the numeric `camic_id`, not the CSV `slide_id`.

```sql
SELECT collection, patient_id, slide_id, camic_id, camicroscope_url
FROM agent_pathdb_slides
WHERE collection = 'CPTAC-STAD'
LIMIT 5;
```

### `agent_datacite_dois`

One row per DataCite record under the TCIA DOI prefix `10.7937`.

Key columns:

- `doi`, `tcia_short_name`, `title`, `publisher`, `publication_year`, `version`, `state`, `url`.

## Related Analysis Result Checks

When a user asks whether a Collection has ground truth, labels, segmentations, classifications, or annotations, do not stop after checking the Collection's own current downloads. TCIA often publishes reusable annotation files as separate Analysis Result datasets. Before saying a Collection has no labels, search visible Analysis Results and report any related result separately.

Use ranked relationship evidence. Prefer explicit `source_collections`; then result short-title patterns such as `EAY131-Tumor-Annotations`; then result titles that name the Collection; then result download/search URLs or manifest filenames scoped to the Collection. Avoid broad `raw_json LIKE '%short_title%'` matching as primary evidence because program metadata may list many related datasets and cause false positives.

Find annotation-like Analysis Results related to one Collection:

```sql
WITH input(short_title) AS (VALUES ('EAY131')),
collection AS (
  SELECT short_title, title
  FROM agent_datasets
  WHERE hidden = 0
    AND source = 'collections'
    AND lower(short_title) = lower((SELECT short_title FROM input))
),
collection_norm AS (
  SELECT short_title, title,
         lower(replace(replace(replace(short_title, ' ', ''), '-', ''), '_', '')) AS norm_short_title
  FROM collection
),
annotation_results AS (
  SELECT ar.short_title, ar.title, ar.doi, ar.link,
         ar.access_level, ar.download_data_types, ar.download_types,
         ar.source_collections,
         lower(replace(replace(replace(ar.short_title, ' ', ''), '-', ''), '_', '')) AS norm_result_short_title
  FROM agent_datasets ar
  WHERE ar.hidden = 0
    AND ar.source = 'analysis-results'
    AND (
      lower(COALESCE(ar.download_data_types, '')) LIKE '%seg%'
      OR lower(COALESCE(ar.download_data_types, '')) LIKE '%rtstruct%'
      OR lower(COALESCE(ar.download_data_types, '')) LIKE '%sr%'
      OR lower(COALESCE(ar.download_data_types, '')) LIKE '%fiducial%'
      OR lower(COALESCE(ar.download_data_types, '')) LIKE '%classification%'
      OR lower(COALESCE(ar.download_data_types, '')) LIKE '%measurement%'
      OR lower(COALESCE(ar.download_types, '')) LIKE '%annotation%'
    )
),
download_scope AS (
  SELECT d.short_title,
         group_concat(DISTINCT d.download_title) AS download_titles,
         group_concat(DISTINCT d.data_types) AS download_data_type_json,
         group_concat(DISTINCT d.file_types) AS download_file_type_json,
         lower(group_concat(DISTINCT COALESCE(d.search_url, '') || ' ' ||
                            COALESCE(d.download_url, '') || ' ' ||
                            COALESCE(d.download_metadata, ''))) AS scoped_urls
  FROM agent_current_downloads d
  WHERE d.hidden = 0
    AND d.parent_source = 'analysis-results'
  GROUP BY d.short_title
)
SELECT ar.short_title AS analysis_result,
       ar.title,
       ar.download_data_types,
       ds.download_titles,
       ds.download_file_type_json,
       ar.access_level,
       ar.doi,
       ar.link,
       trim(
         CASE
           WHEN lower(COALESCE(ar.source_collections, '')) LIKE '%' || lower(c.short_title) || '%'
             OR lower(COALESCE(ar.source_collections, '')) LIKE '%' || lower(c.title) || '%'
             THEN 'source_collections; '
           ELSE ''
         END ||
         CASE
           WHEN ar.norm_result_short_title LIKE c.norm_short_title || '%'
             THEN 'result_short_title_prefix; '
           ELSE ''
         END ||
         CASE
           WHEN lower(ar.title) LIKE '%' || lower(c.short_title) || '%'
             OR lower(ar.title) LIKE '%' || lower(c.title) || '%'
             THEN 'result_title; '
           ELSE ''
         END ||
         CASE
           WHEN ds.scoped_urls LIKE '%collectioncriteria=' || lower(c.short_title) || '%'
             OR lower(replace(replace(replace(COALESCE(ds.scoped_urls, ''), ' ', ''), '-', ''), '_', '')) LIKE '%' || c.norm_short_title || '%'
             THEN 'result_download_scope; '
           ELSE ''
         END
       ) AS relationship_evidence
FROM collection_norm c
JOIN annotation_results ar
LEFT JOIN download_scope ds
  ON ds.short_title = ar.short_title
WHERE relationship_evidence <> ''
ORDER BY ar.short_title;
```

For broad or ambiguous Collection short titles, inspect the returned `relationship_evidence` and the Analysis Result page before treating the relationship as definitive. A title or scoped-download match is useful evidence; explicit source metadata is stronger.

## Base Tables

Use base tables when the views do not expose a needed detail.

- `snapshot_meta`: schema version, source hashes, source URLs, generated timestamp, and counts.
- `wordpress_records`: WordPress Collections, Analysis Results, and global Downloads endpoint rows. `raw_json` stores source JSON. `normalized_json` is populated for Collections and Analysis Results.
- `wordpress_downloads`: normalized nested current download records plus global Downloads endpoint rows. Use `is_current_version = 1` for normal user-facing downloads.
- `wordpress_download_labels`: one row per download-level `download_type`, `data_type`, `file_type`, and `external_resource` label.
- `wordpress_versions`: normalized `/api/v2/versions` rows, expanded to one row per related Collection or Analysis Result short title.
- `pathdb_rows`: trimmed PathDB cohort-builder slide metadata.
- `pathdb_collection_summary`: collection-level PathDB patient/slide summaries.
- `datacite_dois`: TCIA DOI prefix records from DataCite.

## Optional Pathology SQLite

Use `cache/pathology_metadata.sqlite` only after the base snapshot has confirmed TCIA provenance, visibility, and access/license metadata. Important tables:

- `pathology_downloads`: visible, non-controlled, current pathology Aspera download records selected from Collection Manager/WordPress metadata.
- `pathology_download_label_matches`: label/title evidence for why a download was selected as pathology-related.
- `pathology_package_files`: imported Aspera package browse or `.sums` inventory rows. This table may be empty before package inventories are imported.
- `pathology_file_objects`: normalized file rows derived from imported Aspera package inventories.
- `pathdb_slide_crosswalk`: PathDB rows matched by exact TCIA/PathDB collection short title for enrichment and discrepancy review. These rows do not define the Aspera package inventory.
- `pathology_disparities`: curator-facing rows for PathDB/download scope mismatches, multiple-download cases, and future package-file reconciliation issues.

Prefer the agent-facing views for normal use:

- `agent_pathology_downloads`: current pathology Aspera download records with `download_label` fallback text.
- `agent_pathology_dataset_summary`: dataset-level scope plus `package_inventory_status`.
- `agent_pathology_package_files`: imported package inventory rows when available.
- `agent_pathology_file_objects`: normalized file objects from imported Aspera package rows.

`package_inventory_status = 'normalized_file_rows_available'` means imported Aspera package rows have been normalized into file objects. `package_inventory_status = 'not_imported'` means Aspera package inventory rows are not available for that dataset in the release. `pathdb_file_objects_available` can appear only in locally built or legacy SQLite files that opted into PathDB file-object seeding.

Common pathology summary:

```sql
SELECT short_title, download_records, pathdb_collection_slide_count,
       package_inventory_status, open_noncommercial_downloads
FROM agent_pathology_dataset_summary
ORDER BY lower(short_title);
```

## Optional Controlled-Access SQLite

Use `cache/controlled_access_metadata.sqlite` only after the base snapshot has confirmed TCIA provenance and controlled/restricted access. The data are public metadata extracted from WordPress-controlled download records, public manifests, public spreadsheet metadata URLs, and WordPress `download_metadata` fields. The SQLite does not grant file access and must not be used to download controlled data directly.

Important tables:

- `controlled_downloads`: current visible WordPress controlled download records routed to `general_commons` or `ctdc`.
- `wordpress_download_metadata`: key/value expansion of WordPress `download_metadata`, including nested objects serialized as JSON.
- `wordpress_download_urls`: URLs extracted from download metadata and classified as manifest, metadata spreadsheet, data dictionary, supporting docs, or other.
- `wordpress_search_filters`: parsed downstream search URL filters such as study names or IDs.
- `source_artifacts`: fetched public manifest/spreadsheet artifacts, hashes, and fetch status.
- `manifest_rows`: public manifest rows, including `drs_uri`, `file_id`, file name/type/size, study, participant, sample, and series identifiers when available.
- `metadata_rows`: public spreadsheet rows with patient, study, series, modality, manufacturer, protocol, pixel spacing, release, species, phantom, and longitudinal metadata.
- `controlled_files`: normalized file-grain rows combining manifest rows and spreadsheet rows by download and Series Instance UID where possible.
- `radiology_series`: radiology-oriented rows aligned to the public NIfTI/pathology metadata model.
- `idc_index`, `idc_ct_index`, `idc_pt_index`, `idc_contrast_index`, and `idc_series_links`: IDC-parquet-shaped indexes for controlled-access metadata discovery only.
- `controlled_metadata_exceptions`: review rows for manifest rows without DRS URIs, rows without Series Instance UIDs, and spreadsheet series that did not match a manifest row.

Prefer the agent-facing views for normal use:

- `agent_controlled_downloads`: scoped controlled downloads with `download_label` fallback text and policy URL.
- `agent_controlled_files`: normalized file rows with lower-snake-case identifiers such as `series_instance_uid`, `study_instance_uid`, `drs_uri`, and `file_id`.
- `agent_controlled_dataset_summary`: route/dataset summary with policy URL.

Common controlled-access summary:

```sql
SELECT route_system, short_title, controlled_file_rows,
       participant_ids, patient_ids, series_instance_uids
FROM agent_controlled_dataset_summary
ORDER BY route_system, lower(short_title);
```

Find controlled CTDC PET rows with DRS URIs:

```sql
SELECT short_title, file_name, modality, participant_id,
       series_instance_uid, drs_uri
FROM controlled_files
WHERE route_system = 'ctdc'
  AND upper(COALESCE(modality, image_modality, '')) = 'PT'
  AND COALESCE(drs_uri, '') <> ''
LIMIT 25;
```

## Optional Patient-Level Clinical SQLite

Use `cache/clinical_metadata.sqlite` only after the base snapshot confirms
TCIA provenance, visibility, and access/license metadata. Important tables:

- `clinical_downloads`: official clinical-download candidates and ingest status.
- `clinical_idc_tables`: IDC table/version inventory and imaging-linked counts.
- `clinical_dictionary`: IDC column labels and coded-value dictionaries.
- `clinical_imaging_subjects`: TCIA/IDC imaging subject allowlist.
- `clinical_dataset_inferences`: audit of eligible/rejected WordPress
  Collection diagnosis/site labels, `screening_signal`, `review_required`,
  `review_reason`, `review_evidence`, and the numbers of subjects backfilled
  or suppressed. Curator-cleared screening matches use
  `review_required = 0` and a `screening_review_resolved_*` reason.
- `clinical_dataset_relationships`: strong WordPress Analysis Result to
  Collection relationships plus exact PatientID crosswalk coverage and
  inherited-fact counts.
- `clinical_sources`: source kind, priority, lineage, signature, hash, and URL.
- `clinical_rows`: lossless patient rows, imaging flag, and original `row_json`.
- `clinical_facts`: long-form concepts from every source. `value_text` remains
  source-preserving; `value_resolved`, `qc_excluded`, and `qc_status` describe
  the QC-adjusted value used for subject resolution.
- `clinical_subjects`: materialized one-row-per-subject resolution.
- `clinical_build_warnings`: downloads or tables needing curator review.
- `clinical_qc_findings`: auditable automatic corrections, exclusions, and
  open manual-review findings with source row/fact identifiers. Dispositions
  include `auto_normalize`, `auto_exclude`, `accepted_skip` for reviewed
  non-patient/unsupported artifacts, and `manual_review` for unresolved
  anomalies or ingestion coverage gaps.

`clinical_meta.ct_colonography_histology_result` records spreadsheet coverage
and patient classifications for ACRIN 6664. Its patient-level long-form facts
include `lesion_histology`, `lesion_histology_code`, and `screening_result`;
the resolved subject view exposes `screening_result`. Histology-derived
diagnoses are direct patient-level facts, while subjects with code 88/98 or no
spreadsheet evidence remain unclassified.

`clinical_meta.ea1141_screening_pathology_result` records the EA1141 official
ZIP coverage and classification totals. Its direct facts include decoded
demographics, `screening_result`, `lesion_outcome`,
`lesion_outcome_detail`, and core/surgical tumor-grade codes. The derived
patient-level facts classify code `1` as a positive screen, code `0` as a
negative screen, and leave withdrawn/missing outcomes without a diagnosis.
Positive subjects receive pathology-specific diagnosis, breast site, and grade
when available; negative subjects receive `primary_diagnosis = 'Non-Cancer'`.

`clinical_meta.hnscc_official_cohort_result` records the two official HNSCC
patient-table subject counts, their overlap and union, invalid-ID count, and
the number promoted into `clinical_imaging_subjects`. Promotion occurs only
when the current files contain 215 CT-atlas subjects and 492 radiomics
subjects, with 80 overlapping and exactly 627 in the union. These controlled
imaging PatientIDs use
`imaging_source = 'tcia_official_clinical_union'`; the linkage is cohort
metadata and does not authorize controlled-image access.

`clinical_meta.hungarian_colorectal_icd10_result` records the official CSV
subject count, PathDB patient and slide counts, exact ID overlap/differences,
three-character ICD-10 category counts, malignant/non-malignant/indeterminate
totals, unknown-category count, and promoted-subject count. Promotion requires
an exact 200-subject official CSV to 200-patient/200-slide PathDB match.
Patient-level long-form facts retain the full `icd10_code`, stable
`icd10_category`, and `screening_result`. Category `R89` is retained as
indeterminate without a derived diagnosis or site.

`clinical_meta.ivygap_allen_clinical_result` records Allen tumor and patient
counts, current TCIA General Commons manifest row/participant counts, matched
tumor rows and subjects, external-only and manifest-only subjects,
multiple-tumor subjects, per-concept patient coverage, and promoted imaging
subjects. Allen `tumor_name` is linked to TCIA `Participant ID` by its leading
deidentified `W<number>` patient component. The current result contains 40
matched tumor rows for all 39 TCIA subjects; `W22` has primary and recurrent
tumor rows, while Allen-only `W27` and `W28` are excluded.

`clinical_meta.idc_clinical_result` includes a `victre` object recording FDA
VICTRE source status, patient count, signal-present/absent counts, derived
cancer/non-cancer counts, unresolved subjects, artifact bytes, and density
coverage. VICTRE direct facts retain synthetic subject type, DICOM sex, breast
density, lesion status, and FDA location-file lesion counts. Diagnosis,
screening result, and positive-case breast site are explicitly marked as
patient-level inferences. The VICTRE Collection label is audited with
`eligibility_reason = 'screening_patient_level_only'`, so it never supplies a
dataset-level cancer diagnosis or site.

Prefer these views:

- `agent_clinical_subjects`
- `agent_clinical_all_subjects`
- `agent_clinical_facts`
- `agent_clinical_conflicts`
- `agent_clinical_dataset_summary`
- `agent_clinical_source_tables`
- `agent_clinical_dictionary`
- `agent_clinical_imaging_subjects`
- `agent_clinical_dataset_inferences`
- `agent_clinical_dataset_relationships`
- `agent_clinical_qc_findings`

Resolution is concept-specific and ordered official TCIA clinical download,
TCIA-linked external clinical source with validated identities, IDC clinical,
CDA, DICOM, exact-ID Analysis Result inheritance, and finally a single-label
WordPress Collection fallback for a missing diagnosis or site. Direct TCIA
and IDC-normalized copies share one
official-data lineage. Lower-priority patient facts are retained even when
they lose resolution. WordPress fallbacks are added only when the concept has
no patient-level fact. `agent_clinical_subjects` contains only image-linked
subjects; use `agent_clinical_all_subjects` to audit clinical-only source rows.
See `references/clinical.md`.

Analysis Result inheritance requires both strong WordPress relationship
evidence and an exact normalized PatientID match. Program-wide related lists
are ignored. Direct target-dataset facts always win, unmatched source subjects
are never copied, and acquisition-specific `age_at_imaging_years` is excluded.

```sql
SELECT target_short_title, source_short_title, relationship_source,
       target_subjects, matched_subjects, inherited_facts, status
FROM agent_clinical_dataset_relationships
WHERE source_short_title = 'TCGA-GBM'
ORDER BY target_short_title;
```

Resolved subject rows expose `primary_diagnosis_is_inferred` and
`primary_site_is_inferred`. Long-form facts expose `evidence_scope` and
`is_inferred`; filter to `is_inferred = 0` when an analysis requires
patient-observed values only.

Age concepts remain context-specific: `age_at_diagnosis`,
`age_at_enrollment_years`, `age_at_imaging_years`, and
`age_at_treatment_years` are separate resolved columns. Do not combine them
without an analysis-specific rule. The raw source value and column remain in
`agent_clinical_facts` even when QC excludes or normalizes the resolved value.

For screening review:

```sql
SELECT short_title, raw_value AS cancer_label, screening_signal,
       review_reason, candidate_subjects, subjects_applied,
       subjects_suppressed
FROM agent_clinical_dataset_inferences
WHERE concept = 'primary_diagnosis'
  AND review_required = 1
ORDER BY short_title;
```

These rows have no Collection-level diagnosis or site facts applied. Any
patient-level facts from official artifacts, IDC, CDA, or DICOM remain
available through their normal source precedence.

Resolved screening reviews can be audited separately:

```sql
SELECT short_title, raw_value AS cancer_label, screening_signal,
       review_reason, review_evidence, subjects_applied
FROM agent_clinical_dataset_inferences
WHERE concept = 'primary_diagnosis'
  AND review_required = 0
  AND review_reason LIKE 'screening_review_resolved_%'
ORDER BY short_title;
```

```sql
SELECT short_title, subject_id, vital_status, overall_survival_days,
       source_kinds, conflict_count
FROM agent_clinical_subjects
WHERE short_title = 'RADCURE';

SELECT table_name, row_count, subject_count, subjects_with_imaging
FROM agent_clinical_source_tables
WHERE short_title = 'NLST';

SELECT *
FROM agent_clinical_conflicts
WHERE short_title = 'RADCURE';

SELECT short_title, concept, inferred_value, eligibility_reason,
       candidate_subjects, subjects_applied
FROM agent_clinical_dataset_inferences
WHERE eligible = 1
ORDER BY subjects_applied DESC;
```

## Optional NIfTI SQLite

Use `cache/nifti_metadata.sqlite` only after the base snapshot has confirmed TCIA provenance, visibility, and access/license metadata. Prefer these agent-facing views:

- `agent_nifti_downloads`: WordPress NIfTI download provenance with `download_label` fallback text.
- `agent_nifti_dataset_summary`: all NIfTI download scope plus file/radiology/derived-object counts.
- `agent_nifti_files`: canonical radiology file/series rows with lower-snake-case aliases such as `series_instance_uid` and `study_instance_uid` when source UIDs exist.
- `agent_nifti_derived_objects`: segmentation, annotation, and transformed-image rows with best-effort source references.
- `agent_nifti_characteristics`: dataset-by-dataset reviewed object role, associated imaging modality, preferred source NIfTI volume, `source_reference_count`, source dataset/access level/DICOM UIDs, alternate DICOM SEG representation, download-level WordPress provenance, and canonical study grouping. It stays one row per reviewed file even when a derived image has multiple source volumes. `NIfTI` remains implicit at file grain and WordPress labels are joined from `nifti_downloads`. `source_access_level` describes the related source image and can therefore be `controlled` even when the NIfTI download is open. It covers every current NIfTI dataset in the release.
- `agent_nifti_characteristics_summary`: unambiguous source-image, associated-segmentation, transformed-image, and fiducial-annotation counts for reviewed datasets.
- `agent_nifti_review_issues`: one row per unresolved dataset-level issue. Filter `status = 'manual_review'` for the active queue; affected-file counts and evidence remain compact at dataset grain.

Common NIfTI summary:

```sql
SELECT short_title, nifti_downloads, nifti_files,
       radiology_series_rows, mr_files, ct_files
FROM agent_nifti_dataset_summary
ORDER BY lower(short_title);
```

Reviewed CT-ORG characteristics:

```sql
SELECT subject_id, file_name, object_role, associated_imaging_modality,
       study_id, study_id_source, source_nifti_volume_file_name,
       source_dicom_series_instance_uid, classification_confidence
FROM agent_nifti_characteristics
WHERE short_title = 'CT-ORG'
ORDER BY subject_id, object_role, file_name;
```

Reviewed Healthy-Total-Body-CTs source linkage:

```sql
SELECT subject_id, file_name, object_role, associated_imaging_modality,
       study_id, study_id_source, source_dataset_short_title,
       source_access_level, source_dicom_series_instance_uid,
       source_dicom_study_instance_uid, reference_confidence
FROM agent_nifti_characteristics
WHERE short_title = 'Healthy-Total-Body-CTs'
ORDER BY subject_id;
```

Reviewed BCBM-RadioGenomics characteristics:

```sql
SELECT subject_id, study_id, file_name, object_role, associated_imaging_modality,
       segmentation_representation, source_nifti_volume_file_name,
       source_dicom_series_instance_uid,
       classification_confidence
FROM agent_nifti_characteristics
WHERE short_title = 'BCBM-RadioGenomics'
ORDER BY subject_id, study_id, object_role, file_name;
```

Open NIfTI review issues:

```sql
SELECT short_title, issue_code, affected_files, description
FROM agent_nifti_review_issues
WHERE status = 'manual_review'
ORDER BY lower(short_title), issue_code;
```

## Common Joins

Join datasets to current downloads:

```sql
SELECT d.short_title, d.title, w.download_title, w.access_level, w.download_url
FROM agent_datasets d
JOIN agent_current_downloads w
  ON w.parent_source = d.source
 AND w.short_title = d.short_title
WHERE d.hidden = 0;
```

Join datasets to PathDB slides:

```sql
SELECT d.short_title, d.title, p.patient_id, p.slide_id, p.camicroscope_url
FROM agent_datasets d
JOIN agent_pathdb_slides p
  ON lower(p.collection) = lower(d.short_title)
WHERE d.hidden = 0;
```

Find controlled or mixed-access datasets with CT, PET, and annotation/result labels:

```sql
SELECT DISTINCT s.short_title, s.title, s.dataset_type,
       s.resolved_access_level, s.download_data_types, s.download_types, s.link
FROM agent_dataset_access_summary s
WHERE s.hidden = 0
  AND s.resolved_access_level IN ('controlled', 'mixed')
  AND EXISTS (
    SELECT 1 FROM agent_current_downloads d, json_each(d.data_types) x
    WHERE d.short_title = s.short_title AND d.hidden = 0
      AND lower(x.value) IN ('ct', 'computed tomography')
  )
  AND EXISTS (
    SELECT 1 FROM agent_current_downloads d, json_each(d.data_types) x
    WHERE d.short_title = s.short_title AND d.hidden = 0
      AND lower(x.value) IN ('pt', 'pet')
  )
  AND (
    EXISTS (
      SELECT 1 FROM agent_current_downloads d, json_each(d.data_types) x
      WHERE d.short_title = s.short_title AND d.hidden = 0
        AND lower(x.value) IN ('seg', 'segmentation', 'rtstruct', 'sr', 'annotation', 'annotations')
    )
    OR EXISTS (
      SELECT 1 FROM agent_current_downloads d, json_each(d.download_types) x
      WHERE d.short_title = s.short_title AND d.hidden = 0
        AND lower(x.value) LIKE '%annotation%'
    )
  )
ORDER BY s.short_title;
```
