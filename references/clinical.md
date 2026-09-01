# Patient-Level Clinical Metadata

Use the optional `clinical_metadata.sqlite` for patient-level demographics,
diagnoses, outcomes, treatment-response labels, collection-specific source
tables, and sparse DICOM fallback fields. Confirm TCIA provenance and access in
the base snapshot first.

## Source precedence

Resolve each concept independently:

1. Official, visible, non-controlled TCIA clinical downloads.
2. Public external clinical sources explicitly linked by the TCIA Collection,
   after their subject identifiers are validated against current TCIA
   imaging/manifest identifiers.
3. IDC clinical tables, which normalize tabular data accompanying TCIA/IDC
   collections.
4. Cancer Data Aggregator harmonized clinical metadata.
5. DICOM metadata, only when higher-priority sources are absent.
6. A single, non-generic WordPress Collection `cancer_types` or
   `cancer_locations` label, only as an explicit dataset-scope fallback when
   that subject has no patient-level diagnosis or site fact.

Never discard lower-priority values. `clinical_facts` preserves every sourced
value and `agent_clinical_conflicts` exposes disagreements. A blank
higher-priority value does not suppress a populated lower-priority value.
Controlled-access clinical files are not harvested.

Resolved subject columns use canonical display spellings for equivalent
case-only values while `clinical_facts.value_text` and
`clinical_rows.row_json` retain the source text. For example, `M`, `m`,
`male`, and `Male` resolve to `Male`; the same principle removes case-only
filter duplicates for race, site, diagnosis, and outcome values without
discarding their original evidence.

Coded demographic values are decoded only from the applicable dataset/table
dictionary. Equivalent numeric representations such as `1` and `1.0` may be
matched as the same code, but their meaning is never assumed globally.
Dataset-specific official dictionaries take precedence; EA1141, for example,
defines sex code `1` as Female, whereas several ACRIN datasets define `1` as
Male. Unmapped codes remain visible for review.

## Quality control and manual review

Clinical QC never overwrites source evidence. `clinical_rows.row_json` and
`clinical_facts.value_text` retain the ingested values.
`clinical_facts.value_resolved` contains the value eligible for subject
resolution, while `qc_excluded` and `qc_status` explain why a fact was withheld.
`clinical_qc_findings` and `agent_clinical_qc_findings` provide the rule,
severity, disposition, source row/fact identifiers, original and resolved
values, and review status.

The reviewed QC rules:

- convert the official `Crowds-Cure-2017` TCGA-style
  `age_at_diagnosis` field from days to years;
- exclude `Not Reported`, `not available`, and `--` from resolved ages;
- exclude DICOM `PatientAge=999Y` as a placeholder;
- exclude DICOM `PatientAge=000Y` pending dataset review, because a zero can be
  valid for a neonate but is usually a missing-value placeholder in adult
  cancer collections;
- suppress all RADCURE DICOM ages when the official TCIA clinical workbook
  provides a valid subject-level age; the raw DICOM value remains preserved;
- send RADCURE DICOM-versus-official sex disagreements to manual review while
  continuing to resolve to the official clinical value;
- treat HNC-IMRT-70-33 DICOM `PatientAge=000Y` as missing after confirming
  that TCIA publishes no patient-level clinical age alternative, CDA has no
  age or birth year, and the collection's nonzero DICOM ages span 30-83 years;
- treat DICOM `PatientAge=000Y` as a missing placeholder for the seven CMB
  collections after confirming that public CTDC participant metadata also
  contains only `000Y` or blank ages;
- treat the seven GLIS-RT and fifteen Head-Neck Cetuximab zero ages as missing
  after collection-specific cohort/range review and confirmation that TCIA
  publishes no alternative patient-level age table;
- ingest ACRIN-FMISO-Brain `A0.csv` by its `cn` identifier, retain `entryage`
  as `age_at_enrollment_years`, and exclude a DICOM zero imaging age only when
  that subject has a valid official enrollment age;
- retain Colorectal-Liver-Metastases `Age` as
  `age_at_treatment_years` because its official dictionary defines the field
  as age at operation, rather than relabeling it as imaging age;
- decode sex and vital-status values only for collections with an audited
  source dictionary, including `0=Alive` and `1=Dead` for
  Colorectal-Liver-Metastases and HistologyHSI-BC-Recurrence;
- normalize explicit categorical `--`/`not available` tokens to null;
- classify identifier warnings from dictionary/reference sheets as expected
  skips, while keeping probable patient tables without a safe ID mapping and
  genuine official-download parser failures in the manual-review queue;
- hold remaining ages outside 0–120 years for manual review; and
- exclude the known `Head-Neck-PET-CT` workbook `Explanations` row from
  patient resolution while preserving its source row.

Export the open manual-review queue with:

```bash
python scripts/tcia_clinical_metadata.py export-qc \\
  --db cache/tcia-metadata-v2-latest/clinical_metadata.sqlite \\
  --out clinical_qc_manual_review.csv
```

Add `--all` to include accepted automatic normalizations and exclusions.
Use `--collection RADCURE` (or another exact Collection short title) to
produce a dataset-specific review queue. The V2 release retains detailed
clinical-review material in the Participant Inventory audit companion; export
a CSV locally only when a separate review file is useful.
Use `--all` when auditing accepted automatic findings, including
`accepted_skip` coverage classifications; the default export contains only
open `manual_review` findings.

Treat a direct TCIA artifact and the IDC table derived from it as one official
clinical-data lineage, not two independent confirmations. Prefer the direct
artifact on conflict. Use IDC as the operational parser/delivery route when the
original TCIA package is SAS, Access, or another format the direct harvester
cannot safely parse. Preserve IDC codes, labels, and value mappings.

CDA remains a secondary enrichment layer and its source commons remain
authoritative. DICOM demographics can be missing, inconsistently encoded, or
repeated across series.

WordPress fallback values are collection-associated inferences, not confirmed
individual diagnoses. Apply them only to subjects in the imaging allowlist,
only from visible Collection records, and only when the relevant label contains
one meaningful value. Reject Analysis Results, multi-label values, and generic
labels such as `Various`, `Multiple`, `Mixed`, `Other`, or `Unknown`. Do not
create a fallback fact when any patient-level source already populated that
concept.

Treat `screen*` in a Collection short title, title, summary, abstract, or
detailed description as a review signal. When such a Collection has exactly one
otherwise-eligible cancer label and no `Non-Cancer`, `No Cancer`, or
`Cancer-Free` designation:

- set `review_required = 1` with
  `review_reason = 'screening_single_diagnosis_without_non_cancer'`;
- suppress both diagnosis and site inference;
- add a `screening_dataset_review_required` build warning; and
- continue ingesting patient-level official, IDC, CDA, or DICOM facts normally.

This is deliberately conservative. Text such as “screened for study inclusion”
or laboratory screening can create false-positive review flags; resolve those
through explicit curator review rather than weakening the automatic safety
gate. A resolved exception remains auditable with
`review_required = 0`, a `screening_review_resolved_*` review reason, and
`review_evidence`.

The first curated resolution is `ACRIN-6698`. TCIA states that 406 women with
invasive breast cancer were prospectively enrolled, and its official 385-row
ancillary workbook contains no contradictory non-cancer indicator. Four
subjects have age, race, lesion type, SBR grade, and pCR all blank; those blanks
are treated as missing ancillary data rather than evidence against the cohort
diagnosis. The single Collection diagnosis/site fallback is therefore allowed
for image-linked ACRIN-6698 subjects lacking a patient-level diagnosis/site.

`HNSCC` is also a curated false-positive screening match. The Collection's
opening sentence states that it contains data from 627 head and neck squamous
cell carcinoma patients. The later word “screened” describes selection from an
already-diagnosed HNSCC source cohort, not a cancer-screening population.
Current official patient tables provide a complete cross-check:

- the Oropharyngeal-Radiomics-Outcomes CSV contains 492 unique subjects;
- the Head-Neck-CT-Atlas workbook contains 215 unique subjects;
- 80 subjects occur in both current files; and
- their union is exactly the 627-subject Collection cohort.

The historical Collection prose says 70 subjects overlap, but the current
official files themselves contain 80; the harvester audits the current-file
counts and requires the exact 492/215/80/627 relationship before using the
union. The radiomics CSV's long `TCIA Radiomics dummy ID...` columns are
recognized as subject identifiers. `SCC` histology is decoded to
`Head and Neck Squamous Cell Carcinoma`, and patient-level subsite, stage,
vital status, age at diagnosis, and follow-up fields are mapped where present.

HNSCC imaging is controlled access and therefore absent from IDC's public
imaging index. Once the exact official clinical union check passes, those 627
TCIA PatientIDs are marked as the Collection's image-linked cohort with
`imaging_source = 'tcia_official_clinical_union'`; this linkage does not grant
access to the controlled images. The Collection diagnosis fallback is allowed
only for subjects in that validated union. In the current files, 215 subjects
have direct SCC histology and the remaining 412 receive the auditable
Collection-scope HNSCC diagnosis; all 627 have patient-level primary site,
stage, and vital status, while 215 have histologic grade.

`CT COLONOGRAPHY` remains in the screening review queue because its three
official polyp-status workbooks cover only 345 of 825 imaging subjects. The
patient-level transformation uses the ACRIN 6664 histology key:

- histology codes 1–9 are malignant carcinomas;
- codes 12–16 are adenomatous/precancerous lesions, not invasive cancer;
- codes 10, 11, and 17 are benign or normal findings;
- codes 88 and 98 remain indeterminate; and
- subjects appearing only in the official “No polyp found” workbook receive
  `primary_diagnosis = 'Non-Cancer'` and
  `screening_result = 'No polyp found'`.

For the current official files this yields 3 malignant subjects, 68 with
adenomas, 23 with benign/normal histology, 242 negative-screening subjects, and
9 indeterminate subjects. The 480 imaging subjects absent from all three
spreadsheets remain without a diagnosis. If a subject occurs in both the
no-polyp list and a coded workbook, coded histology takes precedence; an
Other/Not-applicable code prevents a negative diagnosis. Exact lesion values
remain in `lesion_histology` and `lesion_histology_code` long-form facts.
The source page is
`https://www.cancerimagingarchive.net/collection/ct-colonography/`.

`EA1141` also remains permanently in the screening review queue, even if
future Collection prose no longer contains the word `screening`. Its official clinical
ZIP supplies patient-level resolution for nearly the entire 500-subject imaging
cohort. The ZIP contains seven CSV tables and seven PDF dictionaries. The
harvester recognizes `SUBJECT_DE` as the patient identifier, decodes baseline
age/sex/race/ethnicity, and uses `YEAR0_SENSSPEC_REFSTD` as the cohort-wide
screening classifier. Numeric `SUBJECT_DE` values are joined to TCIA imaging
IDs by the documented collection prefix (`ea1141-<SUBJECT_DE>`):

- code `1` is positive and code `0` is negative;
- `R` means withdrawn and `M` means missing;
- malignant MRI/tomosynthesis lesion outcomes and their detailed pathology
  determine the positive subject's `primary_diagnosis`;
- surgical tumor grade is preferred over core-biopsy grade when available; and
- negative subjects receive `primary_diagnosis = 'Non-Cancer'`, while withdrawn
  or missing subjects remain unclassified.

For the current official files this yields 8 positive subjects, 487 negative
subjects, 4 withdrawn subjects, and 1 subject with missing outcome data. All 8
positive subjects have resolved malignant pathology; the 5 withdrawn/missing
subjects receive no diagnosis and produce a build warning. The 6- and 12-month
`FUP_*_CANCER` fields must not be interpreted alone: their N/A code also covers
subjects already diagnosed after baseline screening. The source page is
`https://www.cancerimagingarchive.net/collection/ea1141/`.

Outside that explicit review gate, Collection-level site inference is still
suppressed for any subject that lacks either a patient-level diagnosis or an
eligible single Collection diagnosis. This prevents a mixed screening
Collection with a `Non-Cancer` label from assigning a disease site to subjects
whose screening outcome is unknown.

`Hungarian-Colorectal-Screening` remains permanently in the screening review queue, but
its official 200-row clinical CSV provides patient-level age, sex, and
`ICD10_health_status`. Its generic `ID` column is accepted only for this
Collection. Before those rows are exposed as image-linked clinical subjects,
the harvester requires their IDs to match exactly the 200 patients and 200
slides in the PathDB snapshot.

The Collection page explains that the first four characters follow
international ICD-10, while later characters may be Hungarian or
Semmelweis-specific extensions. The harvester therefore preserves the full
source code and normalizes only its stable three-character WHO category:

- `C18`, `C20`, and `C76` are malignant categories;
- `D12`, `K52`, `K62`, and `K63` are non-malignant diagnosed findings; and
- `R89` is an abnormal specimen finding without a diagnosis, so it remains
  indeterminate and does not receive a primary diagnosis or site.

The current official file contains 39 malignant subjects, 147 subjects with
non-malignant findings, and 14 indeterminate `R89` subjects. All 200 receive
`icd10_code`, `icd10_category`, and `screening_result`; 186 receive an exact
category-level `primary_diagnosis`. The single Collection label
`Colorectal Cancer` is not used to overwrite the non-malignant findings or fill
the 14 indeterminate subjects. A build warning preserves the unresolved count,
and a changed or incomplete official/PathDB ID relationship prevents cohort
promotion and raises a separate warning.

`IvyGAP` uses a TCIA-linked external clinical source rather than a clinical
file hosted by TCIA. Each scheduled build fetches the Allen Institute
`tumor_details.csv` and validates its identities against TCIA's current
General Commons imaging manifest:

- Allen currently contains 42 tumor rows from 41 patients;
- the TCIA manifest contains 5,223 imaging-file rows for 39 participants;
- stripping the surgery/tumor suffix from Allen `tumor_name` matches every one
  of the 39 TCIA Participant IDs;
- 40 Allen tumor rows match TCIA because `W22` has separate primary and
  recurrent tumors; and
- `W27` and `W28` are Allen-only and are excluded from the clinical SQLite.

The 40 matched tumor rows are preserved at their native tumor grain. Common
patient fields are normalized according to Allen's technical white paper:
`age_in_years` becomes `age_at_diagnosis`, `survival_days` becomes
`overall_survival_days`, and `initial_kps` retains its 0–100 scale.
`molecular_subtype`, `extent_of_resection`, `surgery_status`,
`mgmt_methylation`, and `egfr_amplification` remain long-form tumor-level
facts. This yields age, initial KPS, resection extent, and surgery status for
all 39 TCIA subjects; molecular subtype for 34, MGMT methylation for 38, EGFR
amplification for 30, and overall survival for 30.

The Collection description's `screen*` matches refer to experimental
gene-expression screens, not cancer screening. IvyGAP is therefore an
evidence-backed curated resolution: all 39 manifest-linked subjects receive
the auditable Collection-scope `Glioblastoma` diagnosis and `Brain` site.
The identity audit and coverage counts are stored in
`clinical_meta.ivygap_allen_clinical_result`. Matching the manifest establishes
cohort identity only; it does not authorize access to the controlled images.

`VICTRE` is handled as a synthetic mixed cohort, not as 2,994 breast-cancer
patients. IDC DICOM metadata supplies one image-linked row per virtual subject.
The digital-mammography `SeriesDescription` encodes `pc` (signal absent) or
`pcl` (lesion present) and the phantom density class. Each scheduled clinical
rebuild also fetches the four small FDA DIDSR
[`VICTRE/Locations`](https://github.com/DIDSR/VICTRE/tree/master/Locations)
archives. Their signed phantom seeds are matched by absolute value to the
positive TCIA PatientIDs, and lesion flag `0`/`1` counts are retained as
`microcalcification_count` and `spiculated_mass_count`.

In IDC v24 this yields 1,050 signal-absent subjects classified `Non-Cancer`,
1,943 lesion-grounded subjects classified `Breast Cancer` at the `Breast`
site, and one signal-present subject (`552148544`) whose FDA location file is
empty. That last diagnosis is intentionally withheld and emits
`victre_signal_location_conflict`. All subjects retain
`subject_type = 'Synthetic breast phantom'`, DICOM sex, breast density,
lesion status, and
screening result. The single Collection-level `Breast Cancer` and `Breast`
labels are permanently in patient-level-only mode for VICTRE; they cannot fill
the negative or unresolved phantoms, even if future WordPress prose no longer
contains a `screen*` term. Coverage and the unresolved identity are
stored in `clinical_meta.idc_clinical_result` under `victre`.

## Main views

- `agent_clinical_subjects`: one resolved row per image-linked TCIA subject.
- `agent_clinical_all_subjects`: every resolved source subject, including
  official clinical-only participants without linked imaging.
- `agent_clinical_facts`: long-form sourced facts with priority and provenance.
- `agent_clinical_conflicts`: concepts with more than one nonblank value.
- `agent_clinical_dataset_summary`: subject, concept, source, conflict, and
  clinical-only coverage by dataset.
- `agent_clinical_source_tables`: IDC table inventory, row counts, subject
  counts, image-linked subject counts, and ingest status.
- `agent_clinical_dictionary`: IDC column labels and coded-value dictionaries.
- `agent_clinical_source_dictionary`: reviewed dictionaries shipped inside
  official TCIA clinical workbooks, with source and row provenance.
- `agent_clinical_longitudinal_observations`: visit-, scanner-, and file-level
  observations that must not be collapsed into patient-level conflicts.
- `agent_clinical_imaging_subjects`: the subject allowlist derived from IDC
  imaging plus a legacy IDC/DICOM seed.
- `agent_clinical_dataset_inferences`: Collection-label eligibility, rejection
  reason, screening signal, review flag/reason, candidate-subject count, and
  applied- and suppressed-subject counts.
- `agent_clinical_dataset_relationships`: WordPress-confirmed Analysis Result
  to source Collection relationships, target-ID coverage, exact PatientID
  matches, inherited-fact counts, evidence, and status.

The resolved subject views include common concepts such as sex, race,
ethnicity, age at diagnosis, age at enrollment, age at imaging, age at
treatment/operation, diagnosis,
primary site, stage, grade, vital status, survival intervals, recurrence,
progression, response, and screening result. Dataset-specific columns that are
not mapped remain in `clinical_rows.row_json`.

For `Yale-Brain-Mets-Longitudinal`, the workbook Data Dictionary is preserved
in `agent_clinical_source_dictionary`; `age_at_Imaging (years)` is exposed as
the patient fact `age_at_imaging_years`, and all 57,580 rows from
`Clinical_data`, `Acquisition_data`, and `image_acquisition_parameters` are
available through `agent_clinical_longitudinal_observations`. Study datetime,
scanner vendor/model/site, magnetic field strength, 2D/3D acquisition, sequence
availability, file name/class/tags, slice measurements, and MR timing parameters
remain row-grain observations so expected changes between visits are not
reported as contradictory patient facts. The resolved subject summary uses the
minimum accepted imaging age as baseline and does not classify expected
longitudinal age changes as conflicts. The public non-DICOM detail artifact
also joins the file-level and study-level values to NIfTI assets by exact file
name plus patient/study datetime.

For `BCBM-RadioGenomics`, the official clinical and radiomics workbooks use
268 patient-plus-scan identifiers. A reviewed dataset-specific mapping removes
only the final numeric scan suffix to yield 165 patient IDs; every raw scan ID
and source row remains available in `clinical_rows` and
`agent_clinical_longitudinal_observations`. The clinical workbook contributes
268 `scanner_clinical_scan` observations. The radiomics workbook contributes
2,825 `segmentation_radiomics` observations with 107 feature columns. The
public non-DICOM artifact joins 2,774 rows by exact filename and 47 by a unique
reviewed punctuation-normalized filename, covering all 2,821 published masks.
Four workbook rows without a current public mask remain explicit source-side
QC exceptions rather than being discarded.

For `Brain-TR-GammaKnife`, numeric `unique_pt_id` values in the official
workbook are projected to the controlled Collection identifiers by the reviewed
`number -> GK_NNN` rule. All raw identifiers remain in row provenance. The
course sheet contributes 76 `radiotherapy_course` observations for 47 patients;
the lesion sheet contributes 244 `lesion_followup_outcome` observations and
preserves its source NRRD lesion name without asserting that the named file is
currently retrievable.

For `ReMIND`, official `Case Number` values are projected by the reviewed
`number -> ReMIND-NNN` rule. The workbook contributes 114 direct patient rows,
and all 32 source dictionary fields are retained. Age is represented as age at
treatment/surgery, not age at imaging. Molecular marker missingness remains
source-faithful; blank IDH, MGMT, and 1p/19q cells are not filled or inferred.

For `TCGA-LGG-Mask`, the official clinical CSV contributes 188 direct patient
rows keyed by `Tumor`. TCGA barcodes are normalized to uppercase. Duplicate
source headers are preserved losslessly with `__2` suffixes instead of allowing
the second value to overwrite the first; this applies to both
`neoplasm_histologic_grade` and `IDH/1p19q Subtype`. The reviewed cohort pattern
promotes all 188 rows to image-linked Analysis Result participants. The
curator-reviewed `TCGA-EZ-7264A` typo decision is used by the VASARI non-DICOM
crosswalk; the clinical CSV itself contains canonical TCGA barcodes.

For `CPTAC-Glioblastoma-CODEX`, the official workbook distinguishes parent
patients from specimen/timepoint labels such as `-TP`, `-TR1`, and `-NAT`.
`UPENN-GBM_PatientID` and `CPTAC-GBM_PatientID` provide the source-supported
parent IDs, including the two three-patient composite slides. Participant
Inventory therefore exposes 12 patients, retains all raw specimen/composite
identifiers in file links and provenance, and associates the workbook with all 12
rather than only the nine patients also returned by CDA.

For ACRIN-6698 specifically, `age` is stored as
`age_at_enrollment_years`, `SBRgrade` as `grade`, and `pcr` as `response`.
`Ltype` remains losslessly available in `clinical_rows.row_json`.
For EA1141, `AGE` is stored as `age_at_enrollment_years`; coded demographic
fields are decoded from the official dictionaries. Lesion outcome, detailed
pathology, and grade codes remain available as long-form facts in addition to
the resolved diagnosis/site/grade fields. Every original CSV column remains
losslessly available in `clinical_rows.row_json`.
For ACRIN-FMISO-Brain, official `A0.csv` case number `cn` is expanded to the
zero-padded TCIA PatientID and `entryage` is stored as
`age_at_enrollment_years`.
For Colorectal-Liver-Metastases, official `Age` is stored as
`age_at_treatment_years` because the source dictionary labels it age at
operation; sex and vital-status codes are decoded from that same dictionary.
For `Hungarian-Colorectal-Screening`, `Age` is stored as
`age_at_imaging_years`, and the original locally extended ICD-10 value remains
available in both `icd10_code` and `clinical_rows.row_json`.
For `IvyGAP`, `W22` intentionally retains two source rows. Conflicting
tumor-level molecular subtype and surgery-status values remain visible rather
than being collapsed into one falsely singular patient value.

Use `primary_diagnosis_is_inferred` and `primary_site_is_inferred` in the
resolved subject views to distinguish dataset-scope fallbacks. Long-form facts
also expose `evidence_scope = 'dataset'` and `is_inferred = 1`; patient-level
facts use `evidence_scope = 'patient'` and `is_inferred = 0`.

Subject identity remains scoped by `(short_title, subject_id)`. An Analysis
Result can inherit stable patient facts from a source Collection only when
both of these gates pass:

1. WordPress explicitly names that Collection in the Analysis Result's
   `source_collections` prose or scopes one of the result's own downloads to
   the Collection short title; and
2. the normalized PatientID matches exactly in the Analysis Result and source
   Collection.

Program-wide related-Collection lists, title similarity, and cohort-level
subject counts do not authorize inheritance. Only matching target subjects are
backfilled; unmatched source-cohort patients are never copied. Direct target
facts from official files, IDC, CDA, or DICOM outrank inherited facts.
Inherited facts use `evidence_scope = 'patient_inherited'` and retain the
relationship, source Collection/PatientID, original source, and original fact
IDs in provenance. `age_at_imaging_years` is not inherited because it belongs
to a particular acquisition. Review `agent_clinical_dataset_relationships`
and `agent_clinical_conflicts` before analysis.

`TCGA-Breast-Radiogenomics` uses the official row's full
`bcr_patient_barcode` as its subject identifier rather than the abbreviated
four-character `patient_id`; this permits exact, auditable matches to
`TCGA-BRCA` while leaving blank or unmatched rows uninherited. Its 334-row
clinical workbook is not itself a cohort manifest. Result membership is the
exact intersection of the 91 barcodes in the result-owned radiomics workbook
and the 100 barcodes in the result-owned molecular-assay workbook, which must
equal the WordPress count of 84 before those rows are promoted as image-linked
Analysis Result participants.

CDA rows are imported only when both their dataset short title matches a
visible current TCIA dataset and their subject identifier is present in the
TCIA imaging allowlist or a direct official clinical artifact. Do not retain
placeholder, unmatched, hidden, or non-TCIA CDA projects. DICOM seed rows
themselves establish imaging presence.

## IDC clinical model

`clinical_index` is a dictionary, not a patient table. Each row describes one
column in one collection-specific clinical table. Fetch the patient/source
rows with `IDCClient.get_clinical_table(short_table_name)`.

The tables are heterogeneous and retain their collection-specific grains. A
participant table may have one row per participant while cancer, screening, or
abnormality tables may have multiple rows per participant. Preserve those
source rows in `clinical_rows`; use the resolved views only for convenient
patient-level discovery.

Some IDC analysis/QA tables accompany more than one collection. Partition a
shared table by each collection's imaging `PatientID` allowlist; never copy the
entire shared table into every associated collection.

Join IDC clinical rows to imaging with:

```text
clinical.<table>.dicom_patient_id = index.PatientID
```

Do not silently discard official clinical-only rows. Preserve them in
`clinical_rows` and `agent_clinical_all_subjects`, set `has_imaging = 0`, and
exclude them from the default `agent_clinical_subjects` view.

### NLST v24 validation baseline

The first IDC validation established:

- CDA: 449 NLST subjects.
- All 449 occur in `nlst_prsn`, `nlst_canc`, and IDC imaging.
- `nlst_prsn`: 53,452 participants, 26,410 linked to IDC imaging.
- `nlst_canc`: 2,150 cancer records for 2,058 participants, including 1,217
  participants linked to IDC imaging.
- The five NLST clinical tables contain 339,273 source rows.
- CDA sex agrees with the IDC participant table for 437/449 subjects and
  conflicts for 12; apply source precedence rather than silently merging.

Treat these as regression expectations for IDC v24, not timeless constants.

### WordPress fallback validation baseline

Against the full visible-TCIA IDC-only validation build and the July 2026 base
snapshot:

- 236 visible Collection records were evaluated.
- 204 Collections had one eligible diagnosis label and 208 had one eligible
  site label.
- The image-linked resolved view contained 50,903 subjects after fallback.
- 17,074 had a diagnosis, including 15,120 dataset-scope inferences.
- 50,717 had a primary site, including 49,459 dataset-scope inferences.

These counts measure metadata coverage, not patient-level diagnostic certainty.
Treat them as a regression baseline for that snapshot, not timeless constants.

## Local use

```bash
python scripts/tcia_v2_bundle.py install --profile research_detail
python scripts/tcia_clinical_metadata.py info --db cache/tcia-metadata-v2-latest/clinical_metadata.sqlite
python scripts/tcia_clinical_metadata.py validate --db cache/tcia-metadata-v2-latest/clinical_metadata.sqlite
```

Default files:

```text
cache/tcia-metadata-v2-latest/clinical_metadata.sqlite
```

MCP/REST deployments can override the query path with:

```bash
export TCIA_CLINICAL_METADATA_DB=/path/to/clinical_metadata.sqlite
```

## Maintainer refresh model

Use version-aware IDC and CDA refreshes plus incremental reuse of larger or
less transactional sources.

- Install the pinned native client (`cdapython==2.1.0`). Each scheduled build
  calls `release_metadata()`, hashes the normalized column/source release
  rows, and compares that value with `cda_release_fingerprint` in the previous
  clinical SQLite. When it is unchanged, CDA source rows are copied locally
  and no subject harvest runs. When it changes or no compatible prior artifact
  exists, exact TCGA imaging identifiers are harvested natively in batches of
  100 with `observation.*` and `treatment.*` expansions.
- If the CDA release check or refresh fails and a schema-compatible prior
  artifact exists, reuse the prior CDA sources and record
  `cda_refresh_failed_previous_reused`. A first build without a prior artifact
  fails instead of silently publishing an empty CDA layer.
- Legacy DICOM fallback rows may still be bootstrapped with
  `--legacy-seed-db`; that option is no longer the normal CDA ingestion path.
- On every run, inspect the current IDC version. Reuse all IDC clinical tables
  when that version matches the prior snapshot. When it changes, fetch the
  complete `clinical_index` plus all collection tables and rebuild them
  atomically. Include both the IDC release (`v24`) and idc-index-data release
  (`24.2.1`) in the stored version key so patch releases also trigger refresh.
- Compare official TCIA clinical-download signatures on every run. Download
  and parse only new or changed artifacts and reuse unchanged patient rows.
- Recompute WordPress dataset-scope inference on every run from the fresh base
  snapshot. It is local and inexpensive; never carry it forward from a prior
  clinical SQLite.
- Include the complete inference/review audit in the clinical release
  fingerprint so new or changed review flags trigger a sidecar update.
- Use workflow dispatch with `refresh_all_clinical_downloads=true` for a
  periodic forced direct-artifact validation.
- Use workflow dispatch with `refresh_idc_clinical=true` to force a complete
  IDC clinical re-fetch without waiting for a new IDC version.
- Use workflow dispatch with `refresh_cda=true` to force a CDA re-harvest even
  when the release fingerprint is unchanged.

A complete IDC clinical refresh is small enough to rebuild rather than apply
row-level deltas. At the IDC v24 baseline, the complete clinical export was
about 7 MB compressed: 59 collections, 174 collection-table mappings, 399,251
source rows, and 5,756 dictionary rows. The visible TCIA snapshot allowlist
matched 57 collections, 170 collection-table mappings, 391,854 rows, and 5,696
dictionary rows; two IDC collections were correctly excluded because they did
not match a visible TCIA WordPress record. Prefer a full version-pinned rebuild
when IDC changes.

The audit-rich SQLite is much larger than the source Parquet because it stores
row JSON, normalized facts, indexes, and resolved subjects. The NLST-only v24
validation produced about 388 MB uncompressed and 78 MB gzip-compressed in
roughly 73 seconds; same-version IDC reuse plus CDA merge completed in about
15 seconds. This remains practical for a GitHub release rebuild when IDC
changes, but reinforces reusing the prior sidecar on twice-daily runs.

The full visible-TCIA IDC build produced about 491 MB uncompressed and 92 MB
gzip-compressed in roughly 88 seconds.

A complete CDA harvest is large and periodic rather than transactional. The
existing prototype contains roughly 182,000 raw CDA rows and produces a
hundreds-of-megabytes SQLite before normalization. Repeating that full query
twice daily adds service load and failure modes without a corresponding
freshness benefit.

Official TCIA clinical artifacts are roughly 130 current download records, but
ZIP/XLS variation and occasionally unchanged URLs favor signature reuse plus
periodic forced validation.

## Parsing and quality limits

The direct harvester reads CSV, TSV, XLS, XLSX, and supported tables inside ZIP
files. It uses conservative subject-column and common-concept aliases. Tables
without a recognizable subject identifier are retained as warnings rather
than being guessed into patient rows. IDC tables use their explicit
`dicom_patient_id`, `clinical_index.column_label`, and `values` mappings.
Extend concept aliases only after inspecting the official dictionary or source
table.

Use `clinical_downloads.ingest_status`,
`clinical_idc_tables.ingest_status`, `clinical_build_warnings`, source counts,
conflict counts, and `agent_clinical_dataset_inferences` as release QA. A
successful SQLite integrity check does not imply that every heterogeneous
clinical table was semantically harmonized or that a dataset-scope inference
is a confirmed patient observation.
