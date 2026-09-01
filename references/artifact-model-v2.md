# TCIA Metadata Artifact Model V2

Use this reference for public non-DICOM inventory, managed-system provenance,
Participant Explorer integration, and migration from the May 2026 artifact line.

## Architectural boundaries

Keep physical artifacts separate when access policy, authority, refresh cost, or
installation audience differ. Compose them through the Participant Inventory.

| Surface | Authority and scope | Distribution |
| --- | --- | --- |
| Base TCIA snapshot | WordPress publication identity, downloads, licenses, and routes | Required |
| IDC | Authoritative public DICOM metadata and access | Query/project only; do not duplicate file metadata |
| Public non-DICOM metadata | Public non-DICOM imaging and imaging-derived objects | Optional detail artifact |
| Controlled-access metadata | Public metadata describing GC/CTDC controlled DICOM and non-DICOM holdings | Optional detail artifact |
| Clinical metadata | Raw, normalized, harmonized, inferred, and resolved participant facts | Optional enrichment |
| Participant Inventory | Compact dataset-scoped participant availability across the above sources | Participant Explorer integration artifact |

Do not divide public non-DICOM data by delivery system. PathDB, Aspera,
WordPress attachments, and AWS Open Data are locations or metadata systems for
logical public non-DICOM assets.

## Managed systems

Use only these values unless a schema revision explicitly adds another:

```text
tcia_wordpress
tcia_aspera
tcia_pathdb
aws_open_data
crdc_idc
crdc_gc
crdc_ctdc
```

Record system functions separately because one managed system may support more
than one application behavior:

```text
publication_catalog
discovery_index
distribution_endpoint
viewer
controlled_access_broker
```

Use functions for routing and troubleshooting. Keep them out of the normal
Participant Explorer vocabulary; translate them into user actions such as
View, Download, Open dataset page, or Request access.

## Assets and locations

Represent a logical data object in `public_non_dicom_assets`. Represent every
observed download, cloud, or viewer location in `public_non_dicom_locations`.
Do not count multiple locations as independent participant assets.

Participant cardinality is independent from asset cardinality. Use
`public_non_dicom_asset_participants` as the canonical asset-to-participant
junction. Retain `public_non_dicom_assets.subject_id` only as a convenient
projection for ordinary one-participant assets; leave it empty for shared
assets rather than storing a delimited list.

For example, HANCOCK contains three distinct pathology grains:

- a patient-specific primary-tumor or lymph-node WSI: one asset to one patient;
- a whole TMA block slide: one asset to many patients through junction rows;
- a cropped TMA core image: one derived crop to one patient, with the parent
  block relationship recorded separately when supported by source evidence.

Preserve the raw identifier attached to each junction row. A harmonized
identifier may be stored separately when the source itself provides sufficient
evidence. HANCOCK PathDB numeric IDs are normalized to the dataset's
`patientNNN` form while retaining each raw numeric value and the mapping method.
The reviewed HANCOCK contract resolves all 13,681 inventoried Aspera images:
12,235 cropped TMA-core PNG filenames carry `patientNNN`; 1,078 patient-specific
primary-tumor or lymph-node SVS filenames carry the zero-padded numeric patient
suffix, including eight `_a` slides; and 368 whole TMA-block SVS files resolve
by exact slide name to PathDB. Of the block slides, 352 link to 33--35 patients
and 16 `block23` slides link to patient653. Preserve those block memberships in
the asset-participant junction rather than duplicating the block or forcing a
single scalar participant. This reviewed metadata relationship does not assert
byte identity between the Aspera and PathDB representations. The separate
WordPress aggregate of 13,684 images remains a curator-reported count
discrepancy and is not resolved by inventing three file rows.

Use these representation provenance classes:

| Class | Meaning |
| --- | --- |
| `submitted_original` | Object distributed as the submitter provided it |
| `exact_replica` | Byte-identical copy supported by checksum evidence |
| `standardized_representation` | Same underlying content converted or reorganized for interoperability |
| `derived_asset` | New product such as a mask, thumbnail, tile pyramid, preview, or feature table |
| `metadata_only` | Catalog, manifest, or descriptive metadata rather than the data object |
| `unknown` | Relationship has not been established |

Do not infer an exact replica from matching names, identifiers, or dimensions.
Require checksums or an authoritative replica declaration. Keep PathDB/package
relationships unresolved when conversion or reformatting cannot be ruled out.

## Public non-DICOM scope

Include visible, public imaging and imaging-derived objects outside public
DICOM. Supported examples include NIfTI, MHA/MHD, NRRD, PNG/JPG/BMP/TIFF,
capsule-endoscopy video, whole-slide formats, spectral arrays, segmentations,
and transformations. Record format independently from media kind, imaging
domain, modality, dimensionality, and object role.

Use semantic WordPress context in addition to extensions. Do not classify an
unrelated PNG in a clinical supporting package as imaging merely because its
extension is PNG.

Routine builds start from the manifest-pinned unified V2 research/audit pair,
losslessly materialize an internal assembly, refresh current WordPress and
PathDB scopes directly, and apply checksum-pinned reviewed references. The
retired standalone NIfTI and pathology artifacts are not producer inputs.

Public DICOM remains an IDC query concern and is not duplicated here. If a
TCIA-published package contains public DICOM that is absent from IDC, treat that
as a source-remediation gap: submit it to IDC rather than adding an exception to
`public_non_dicom`. Legacy public-DICOM exception assets are removed during a
baseline refresh. The BraTS 2021 companion NIfTI trees remain in this artifact;
the package's DICOM trees are excluded pending IDC submission.

BraTS 2021 participant identity is projected from the official TCIA workbook
through the hash-pinned references
`references/brats2021_tcia_crosswalk_v1.csv` and
`references/brats2021_tcia_crosswalk_v1.json`. For named public source
Collections, use the normalized original Collection PatientID as the Analysis
Result display identifier and preserve the `BraTS2021_NNNNN` value as an
alternate challenge identifier. The Analysis Result and Collection remain
separate participant scopes; `participant_identity_evidence` records the
explicit workbook relationship. Additional, private, and unnamed groups retain
their BraTS identifiers. The reviewed workbook crosswalk contains 1,479 rows,
but the public non-DICOM artifact gates the 1,470 participants represented in
the companion NIfTI trees: 1,066 named source identifiers and 404
challenge-only identifiers. The nine DICOM-only participants are intentionally
excluded pending IDC submission. The eight numeric
BraTS-TCGA test rows are normalized only to their existing BraTS aliases;
test-set data availability is deliberately outside this crosswalk.

### Geometry assessment

Keep geometry assessment separate from format, modality, media kind, and object
role. NIfTI, MHA/MHD, and NRRD identify encodings; none proves that a
particular object is a coherent volume.

For IDC DICOM, build the compact participant projection from the separately
versioned idc-index `volume_geometry_index`. A series present there is recorded
as `checked_regular`, `checked_not_regular`, or `checked_indeterminate`; a
series outside that index's documented scope is
`not_in_geometry_index_scope`, not an inferred volume.

For single-file non-DICOM volume formats, initialize eligible assets as
`not_checked`. Import cluster results only from the header-only workflow in
`references/geometry-batch-slurm.md`. Preserve every file-level row in
`public_non_dicom_geometry_assessments`; the geometry fields on
`public_non_dicom_assets` are summaries and may be `mixed`. Header assessment
does not establish pixel quality, clinical usability, or semantic correctness.
When a source package has a documented coverage exception,
`public_non_dicom_geometry_job_coverage` and
`agent_public_non_dicom_geometry_job_coverage` retain the complete/partial
state, counts, missing paths, source error, and sidecar digest. Never infer
complete package coverage from the presence of an analysis result alone.

The reusable geometry evidence is distributed as a separate immutable release
seed because regenerating it requires large source transfers and an HPC batch
run. `references/public_non_dicom_geometry_results_manifest.json` pins its
release tag, asset name, byte sizes, SHA-256 digests, and row/job counts;
documented partial-source exceptions are small committed sidecars under
`references/public_non_dicom_geometry_coverage/`. The seed is an internal
release input, not another user-facing V2 component: its non-DICOM assessment
rows are imported into the normal `public_non_dicom` research-detail artifact.

Each scheduled build regenerates the current non-DICOM job scope and imports
only seed jobs whose scope fingerprint is unchanged. New or changed scopes are
published as `not_checked` and surfaced in a refresh report for a later manual
HPC run; removed scopes are pruned. Seed corruption is a hard release failure.
Public DICOM assessments are excluded and remain the authority of IDC/idc-index.

### Non-DICOM annotations

The unified artifact represents annotation-like files as ordinary logical
assets with `object_role` values `segmentation`, `annotation`, or
`annotation_snapshot`. MCP callers can use
`find_public_non_dicom_assets(requires_annotations=true)`; REST callers can use
`GET /v2/public-non-dicom/assets?requires_annotations=true`. Exact
`object_roles` filters remain available when one class is required.

Discovery does not establish what image an annotation targets. Do not infer a
source-to-annotation relationship from filenames, directory proximity, or a
shared participant. Return a relationship only when the artifact contains
source-supported relationship evidence; otherwise report the annotation asset
without a target linkage.

Retain `participant_link_status='dataset_only'` when a download is known but no
source-supported participant-to-file crosswalk exists. Never spread a
dataset-level file count across every participant.

Reviewed participant-to-file mappings are additive. Preserve the original
filename/folder identifier as `raw_subject_id`, store the resolved dataset-
scoped identifier separately, and record the mapping method, confidence,
reviewer note, evidence URL, review date, and source-artifact provenance in
`public_non_dicom_crosswalk_evidence`. Use
`participant_link_status='reviewed_source_crosswalk'` on mapped file rows and
`crosswalk_available_at_file_grain` on the corresponding download declaration.
An exact full package-path match to an existing PathDB source URL may support a
reviewed mapping when a UUID-only filename does not encode the participant.
Record that relationship as path evidence rather than claiming the Aspera and
PathDB representations are byte-identical. If a shorter UUID token is also
exposed as a PathDB participant identifier, the exact full-path assignment
takes precedence only through an explicit reviewed decision.
Published filename contracts can also support partial-dataset resolution. For
`C-NMC 2019`, the README-defined training UID, fold, and class path resolves
10,661 BMP cells to 73 existing dataset-scoped PathDB participants. The 1,867
numbered preliminary-test BMP files resolve to 28 participants through exact
relative-path matches to PathDB's converted TIFF representations; this path
relationship does not assert byte identity. The 2,586 numbered final-test BMP
files remain `source_confirmed_unavailable` because their 17 subject mappings
and ground-truth labels were not published. Do not create a synthetic
`NA_final` participant or infer those withheld associations.
Reviewed WordPress directory contracts may be expressed as anchored path rules
in `references/public-non-dicom-crosswalk-curation-v1.json`. Extracted values
must resolve uniquely against an official dataset-scoped clinical participant
list; unmatched paths remain explicit review issues. For
`DLBCL-Morphology`, this resolves WSI filenames and patch patient directories,
while TMA slide identifiers remain unassigned until `core.csv` establishes
their multi-participant membership.

Very high-cardinality derived objects covered by the same reviewed contract may
be projected as one `participant_file_group` asset per participant, retaining
the represented-file count and a pointer to the complete specialized package
inventory. For `DLBCL-Morphology`, the
`Cells/{patient_id}/{patch_id}/*.npy` contract projects 1,036,974 cell-shape
arrays into 170 participant groups instead of duplicating more than one million
file rows in the consumer-facing artifact. This compact projection does not
replace or alter the source paths, sizes, and checksums retained in the public
audit companion checkpoint.

The full builder also performs a conservative metadata-only Aspera-to-PathDB
crosswalk pass. It accepts a download only when every imaging file matches
exactly one PathDB dataset-scoped participant identifier, no path is ambiguous,
and numeric identifiers appear as a complete folder name or filename stem.
Accepted original Aspera rows use
`participant_link_status='automated_source_crosswalk'`; the decision and every
file mapping are recorded with `human_reviewed=false`. Partial matches remain
unmodified and stay in `agent_public_non_dicom_review_issues`. WordPress naming
text is retained as supporting context but is not sufficient by itself for
automatic acceptance.

When a reviewer confirms that the source contains no participant-level mapping,
retain the assets and decision provenance but use
`participant_link_status='source_confirmed_unavailable'`. This acknowledges the
limitation without inventing participant assignments or repeatedly returning it
as unresolved work. Curator decisions remain queryable through
`agent_public_non_dicom_crosswalk_decisions`.

The initial reviewed crosswalk snapshot is generated from
`references/public_non_dicom_crosswalks_v1.csv`; its source hashes and per-
dataset counts are recorded in
`references/public_non_dicom_crosswalks_v1_provenance.json`. Rebuild it with
`scripts/tcia_public_non_dicom_crosswalks.py` only from retained source
inventories and reviewed evidence.

For `AURORA-Metastatic-Breast-Multiomics`, the reviewed layer uses the
WordPress Usage Notes filename diagram to separate the `AUR` project prefix
from the four-character patient identifier in H&E SVS filenames. The official
clinical workbook's `5.HLA sample ID linkage` sheet maps the separate
immunofluorescence TIFF slide identifiers to those same patients. The current
crosswalk covers 289 pathology images for 54 patients; the 55th four-character
clinical patient identifier has no pathology image. Existing exact Aspera
package-file rows are enriched in place rather than duplicated, and exact
PathDB source-URL suffix matches receive the same reviewed participant link
while retaining their original PathDB identifier in raw provenance.

## Public non-DICOM image metadata

Schema version 7 adds an exploratory, file-grain metadata layer without making
every source-specific spreadsheet column part of the fixed relational schema:

| Surface | Purpose |
| --- | --- |
| `agent_public_non_dicom_image_metadata` | One row per image asset, with common discovery fields projected as columns and the complete field set retained in `metadata_json` |
| `agent_public_non_dicom_metadata_field_coverage` | Per-dataset field coverage, value diversity, provenance-role counts, examples, and contributing source kinds |
| `agent_public_non_dicom_dataset_metadata_notes` | Dataset-level ambiguity, missing file mappings, conflicts, and other reviewer decisions |

Each metadata field has its own provenance entry during the full build: source
kind and source reference, extraction or inference method, confidence,
semantic role (`source_raw`, `normalized`, or `inferred`), and precedence. The
release splitter moves `field_provenance_json`, conflicting lower-priority
values, and detailed quality evidence into
`public_non_dicom_audit.sqlite.gz`; they are not discarded. The research-detail
database retains the selected metadata values and explicit conflict/coverage
summary states needed for interpretation. Its compact
`field_source_ids_json` maps each selected field to the normalized
`public_non_dicom_metadata_sources` registry, so researchers can identify the
root source without installing the audit companion.

`Yale-Brain-Mets-Longitudinal` uses its official TCIA clinical/scanner workbook
as a source-supported file metadata layer. Exact `file_name` matches attach
sequence class/tags, slice spacing and thickness, repetition/echo/inversion
times, study datetime, vendor/model, field strength, acquisition dimensionality,
scanner site, and sequence-presence flags to 33,811 NIfTI assets. The join
retains the workbook URL, artifact digest, and source-row identifier in field
provenance; unmatched workbook filenames remain an explicit dataset metadata
note rather than being silently discarded.

`BCBM-RadioGenomics` uses its official clinical and radiomics workbooks at
native scan and segmentation grain. The reviewed patient projection maps 268
raw scan IDs to 165 patients while retaining the scan ID as procedure and
provenance metadata. Per-scan age, year, pixel spacing, manufacturer, and field
strength attach to all 268 source images. The radiomics join enriches all 2,821
published segmentation assets: 2,774 exact filename matches plus 47 unique
punctuation-normalized matches. Four additional workbook rows that have no
current public mask are retained as a dataset metadata QC note.

`TCGA-LGG-Mask` is expanded from its official supporting package rather than
left at WordPress download grain. The reviewed inventory exposes 406 MATLAB
segmentation files for 108 TCGA-LGG patients. Each filename is the exact
Series Instance UID of its source MR series and is checked against both the
published manifest and corresponding-image DICOM digest. The shared VASARI CSV
is one file asset linked through the participant junction to 188 patients; its
feature-key PDF is linked with `interpreted_by`. Raw mixed-case spellings are
retained. The source value `TCGA-EZ-7264A` is retained as a curator-reviewed
alias while the canonical participant ID is `TCGA-EZ-7264`.

`TCGA-GBM-QI-Radiogenomics` exposes all 321 AIM XML files from download 45557.
Every XML directly records one TCGA PatientID and one referenced DICOM Study,
Series, and SOP Instance UID. The reviewed inventory therefore links 321 files
to 55 `TCGA-GBM` patients, 60 studies, 111 series, and 193 SOP instances without
inferring identity from filenames. XML remains a dataset-scoped annotation
format here; it is not added to the global non-DICOM image-format vocabulary.

`CPTAC-Glioblastoma-CODEX` uses a hash-pinned 52-row package inventory so the
V2 build does not depend on the retired pathology artifact. The official XLSX
maps exact WSI/cropped filenames and specimen, timepoint, or composite labels
to 12 parent patients. The projection covers all 52 submitted files and all 52
PathDB records. Corresponding Aspera and PathDB records remain separate managed
representations; filename correspondence does not assert byte identity.

WordPress download-grain non-DICOM assets retain the published `images` value
as `represented_file_count`. This is an aggregate count, not file-level
metadata and not a participant crosswalk. ReMIND demonstrates why this
distinction matters: WordPress reports 113, corresponding to subjects with a
preoperative whole-tumor segmentation, while the Aspera package contains 356
NRRD files across all 114 subjects. Reviewed subject-folder/filename matches
provide the file-level crosswalk. The package `.sums` file reports the
empty-file MD5 for every nonempty NRRD; the builder preserves that raw value as
an invalid placeholder and leaves the verified checksum field blank.

Brain-TR-GammaKnife's controlled DICOM rows remain participant/file linked in
the controlled-access artifact. Its separate controlled NRRD download is not
promoted into the public non-DICOM artifact, and historical IDC evidence does
not set current public-DICOM availability flags in Participant Inventory.

The builder applies evidence in this order:

1. file-linked values imported from the NIfTI and pathology metadata artifacts;
2. curated file-linked spreadsheet values from
   `references/public_non_dicom_image_metadata_v1.csv`;
3. reviewed crosswalk spreadsheet columns that describe the matched file;
4. uniform, explicit WordPress dataset labels or acquisition prose;
5. conservative filename tokens such as MR sequence, pathology stain, view,
   laterality, or image/mask role.

An inferred dataset-level value is propagated to files only when the source
supports one unambiguous value. For example, a CT-only MHA package may receive
file-level `modality='CT'`, while a package described as mixed CT and PET stays
unassigned unless filenames or a spreadsheet distinguish the files. Multiple
field-strength, manufacturer, model, stain, magnification, or modality
candidates are placed in the dataset review notes instead of being guessed.

The initial curated spreadsheet pass covers MHA geometry and intensity fields
for `Pedi-Cranial-CT-Healthy`, physical pixel spacing for source and paired mask
PNGs in `Breast-Lesions-USG`, and acquisition type, laterality, view, and raw
equipment codes from the reviewed `CDD-CESM` crosswalk. PathDB rows contribute
the pathology CSV fields already available at file grain, including protocol,
magnification, species, cancer type/location, and format. The legacy NIfTI
artifact remains the detailed source for its broader file metadata and is
projected into this V2 layer rather than replaced.

Use the coverage surface to decide whether an exploratory field should become
a stable projected column. Use the notes surface as the review queue. A useful
starting query is:

```sql
SELECT short_title, field_name, note_code, severity, affected_assets,
       description, evidence_json
FROM agent_public_non_dicom_dataset_metadata_notes
WHERE status = 'review_required'
ORDER BY severity DESC, short_title, field_name;
```

## Participant identity

Create participant keys from the authoritative WordPress dataset type and
short title plus the stripped, casefolded dataset-scoped participant
identifier. Source namespaces and original spellings belong to identifier
provenance, not to the canonical participant key. Preserve every spelling in
`participant_identifiers`. Select one deterministic display form using source
precedence: prefer TCIA-origin spelling, then other managed-system sources,
with stable case and lexical tie-breakers.

When TCIA/IDC, GC, or CTDC records are explicitly attached to the same
WordPress dataset and their exact or case-equivalent identifiers agree
one-to-one, resolve them to one participant. Record
`exact_identifier_same_tcia_dataset` for exact text and
`casefolded_identifier_same_tcia_dataset` for case-only spelling differences.
Both are high-confidence within-dataset associations; neither asserts that the
same bare identifier in another dataset represents the same person. Collections
and Analysis Results remain separate identity scopes.

Store every source identifier and namespace in `participant_identifiers`, even
when several identifiers resolve to one participant. Record automatic and
reviewed associations in `participant_identity_evidence`. Require an official
dataset relationship plus an exact or case-equivalent, unambiguous identifier
match for automatic resolution; use submitter crosswalks, documented shared
namespaces, or reviewed mappings for other equivalence claims. Route
cross-dataset, normalized-but-not-case-equivalent, one-to-many, and conflicting
matches to `participant_link_issues`.

Participant Inventory schema v8 adds `participant_source_links` and
`agent_participant_source_links` as the compact, queryable Analysis Result to
source Collection junction. IDC per-participant source Collection identifiers
are direct evidence. WordPress source-Collection/download relationships and the
reviewed relationship registry require an exact, unambiguous participant
identifier match. Dataset-scoped participant rows remain separate; the junction
records their verified relationship for clients that consolidate memberships
for presentation. Missing or ambiguous source participants remain explicit
coverage issues rather than inferred links.

Official participant-level clinical crosswalks may resolve non-equivalent ID
spellings. The ACRIN-6698/I-SPY2 source rows explicitly pair the ACRIN DICOM
PatientID, I-SPY2 research ID, and I-SPY2 DICOM PatientID. Those scoped values
resolve I-SPY2 aliases and let a BreastDCEDL_ISPY2 participant link to both
source Collections. Equal bare identifiers in unrelated Collections, such as
NLST, are not evidence and remain separate.

The reviewed BraTS workbook is the explicit exception to the default
cross-dataset non-assertion rule: it supplies both the source Collection and
source PatientID. Current-inventory matches are marked
`linked_source_collection`; workbook-named source participants absent from the
current inventory remain visible as `source_participant_not_current` issues.

Resolve `dataset_type` and canonical short title from the base WordPress
snapshot rather than trusting sidecar defaults. This prevents one source from
creating a Collection participant while another creates an Analysis Result
participant for the same published dataset.

The Participant Inventory may show detailed participant-linked assets,
dataset-level availability without a participant crosswalk, and a source
coverage note when an optional detail artifact is not installed. This
distinction prevents false completeness claims.

Shared assets appear under every linked participant in Participant Explorer,
but they remain one logical asset and must not be multiplied in dataset-level
asset counts.

## Clinical values

Preserve the submitted field and value together with any standardized value.
Use these roles:

| Role | Meaning |
| --- | --- |
| `source_raw` | Exactly what the source supplied |
| `normalized` | Syntactic cleanup without a semantic mapping |
| `harmonized` | Mapped to a common cross-dataset concept or vocabulary |
| `inferred` | Derived from indirect evidence |
| `resolved` | Selected from competing values using documented precedence or review |

Never overwrite the source value. Record the method, source artifact, source
URL/row provenance, confidence, and review status alongside the standardized
value.

## Participant Explorer presentation

Query `agent_participant_search` in the Participant Inventory first; it is the
one-row-per-canonical-participant search contract. Use these three public
facets, aligned with the WordPress label hierarchy:

- `data_category`: broad content category, such as Radiology, Pathology,
  Annotations/Segmentations, Clinical Data, or Other Imaging;
- `data_type`: modality or specific content type, such as CT, MR, SEG,
  Segmentation, Whole Slide Image, Video, or Clinical Data;
- `file_format`: physical format, such as DICOM, NIFTI, MHA, SVS, CSV, or MPG.

Access level remains its own facet. Do not combine `open` with DICOM to create
a label such as "Public DICOM."

Retain `media_kind` as an internal, user-friendly representation grouping and
`data_domain` as the normalized scientific domain. They support derivation and
drill-down but are not replacements for the three public facets. Retain
`object_role` for source-versus-derived semantics and annotation relationships;
do not put representation values such as `whole_slide_image` or `video_clip`
there because those belong in `media_kind`/`data_type`.

Provide an expandable provenance/troubleshooting surface containing managed
system, location, source version, checksums, original-versus-standardized
status, and reconciliation warnings.

Query IDC for public DICOM detail. Query the public non-DICOM, controlled, or
clinical detail artifacts only when the user drills down.

For compact public-DICOM presence, project `collection_id`,
`analysis_result_id`, and `PatientID` directly from the IDC index during the V2
build. Include a Collection membership when IDC identifies a visible TCIA
Collection and a distinct Analysis Result membership when IDC supplies a
visible TCIA `analysis_result_id`. Do not pass this projection through the
clinical artifact, and do not infer Analysis Result membership by copying every
participant from a source Collection. Previously retained
`legacy_idc_index` Collection memberships may remain as explicitly historical
compatibility evidence, but they must not be presented as confirmed current IDC
presence or used to create Analysis Result memberships.

## V2 Release Channels

Use `tcia-metadata-v2-latest` as the single supported moving release contract.
Validated source components pass between workflows as short-lived GitHub
Actions artifacts, not as a second public release. The stable channel uses the
streamlined contract with these asset groups:

- Research core: `tcia_snapshot.sqlite.gz`, compact
  `participant_inventory.sqlite.gz`, and the compressed
  current dataset/download exports. This is the default install profile.
- Research detail: unified public non-DICOM, controlled-access, and clinical
  SQLite databases. These are fetched only for relevant drill-down workflows.
- Audit support: `public_non_dicom_audit.sqlite.gz` and
  `participant_inventory_audit.sqlite.gz`. Audit companions hold verbose JSON provenance and
  detailed crosswalk evidence keyed by stable research-artifact identifiers.
- Compatibility exports: the two compressed current JSONL exports.
- Bundle contract: `tcia_metadata_v2_bundle_manifest.json`, which pins the
  SHA-256, size, profile, category, source, and default-download status of every
  payload and records each component schema and release fingerprint.

The compact Participant Inventory preserves participant identity, availability,
access, clinical-presence, and explicit linkage/coverage states. It does not
duplicate accepted clinical fact rows by default; those remain in
`clinical_metadata.sqlite.gz`. Pass `--embed-clinical-values` only for a
deliberate compatibility build.

Research rows retain the root managed system, source URL or record pointer,
raw scientific value where applicable, and interpretation flags. Detailed
`raw_values_json`, field-level provenance, conflicting alternatives, crosswalk
evidence, reviewer notes, and extraction/QC payloads move to the audit
companions. Repeated JSON is stored once in `payloads` and referenced from
`entity_payloads`; `agent_entity_payloads` provides the resolved JSON. Join its
`entity_id` to the stable primary identifier in `entity_table`. SQLite cannot
enforce foreign keys across attached databases, so the build validates both
companions before publishing.

Inspect an additive download plan without fetching assets:

```bash
python3 scripts/tcia_v2_bundle.py expected-assets --release-contract streamlined --profile research_core
python3 scripts/tcia_v2_bundle.py expected-assets --release-contract streamlined --profile research_detail
python3 scripts/tcia_v2_bundle.py expected-assets --release-contract streamlined --profile audit_support
```

Install the stable research core with the bundle-level installer:

```bash
python3 scripts/tcia_v2_bundle.py install --profile research_core
```

The installer reads the top-level manifest first, stages every changed asset,
verifies compressed and decompressed hashes plus SQLite integrity, and only
then replaces installed components. Add `research_detail` for drill-down or
`audit_support` for verbose provenance and QC. Installed files default to
`cache/tcia-metadata-v2-latest/`. After committing the new receipt, it removes
only obsolete installer-managed files and abandoned staging directories older
than 24 hours. It never prunes arbitrary operator files or broad build
workspaces. Audit cleanup before applying it explicitly with:

```bash
python3 scripts/tcia_v2_bundle.py prune
python3 scripts/tcia_v2_bundle.py prune --apply
```

The first command is a dry run that reports active and stale bytes. Top-level
`outputs/`, `dist/`, and other `cache/` subdirectories are maintainer build
workspaces rather than installed release content and remain outside this
receipt-aware cleanup contract.

The V2 build runs after each successful scheduled base-snapshot workflow. It
captures the source release record, verifies every copied source asset against the
captured GitHub digest, regenerates all web exports from that exact bundled
snapshot, validates every component, and publishes only when the complete
bundle fingerprint changes. The top-level manifest is uploaded last so
consumers never accept an update without a complete hash contract. Stale assets
are removed only from the selected V2 moving tag, and that tag is advanced to
the producer commit only after the published bundle passes remote digest
validation. A manually supplied immutable stable tag is created only if it
does not already exist. The workflow does not deploy, restart, or reconfigure
MCP/REST.

### Build-time staging and legacy-detail retirement

Source release assets and the direct build-time IDC participant projection are
first resolved into an explicit build contract rather than passed to V2
builders as an implicit collection of unrelated inputs. The IDC projection is
restricted to visible WordPress Collections and Analysis Results, retains both
dataset identity dimensions, and contains participant/study/series summaries
rather than file-level DICOM metadata. After the source components are
downloaded and verified and the IDC projection is built, the workflow builds
the runner-local `cache/tcia_metadata_v2_staging.sqlite` ledger with
`scripts/tcia_v2_staging.py`. The ledger pins each source database and manifest
hash, component schema/fingerprint, release identity, SQLite integrity result,
and a row-count/schema inventory. Component database names are relative to the
ledger directory; machine-specific paths are never retained.

The public non-DICOM builder accepts `--staging-db` and resolves its snapshot,
unified public research/audit baselines, and clinical input through that
contract. The Participant
Inventory builder resolves its snapshot, controlled, clinical, and direct IDC
participant inputs from the same ledger. Neither the ledger nor the IDC
projection is a release asset. A path-independent copy of the ledger tables is embedded in
`public_non_dicom_audit.sqlite.gz`, while a short-lived GitHub Actions artifact
retains the ledger and IDC projection for maintainer diagnostics.
Expiration of that workflow artifact does not affect a published release.

Run the staging contract directly with:

```bash
python3 scripts/tcia_v2_staging.py validate \
  --db cache/tcia_metadata_v2_staging.sqlite \
  --verify-sources
```

Legacy parity has been established and the transition is complete. Routine
production never downloads the standalone NIfTI or pathology databases.
Instead, `scripts/tcia_v2_audit.py materialize-assembly` losslessly restores a
manifest-pinned unified V2 research/audit pair, and
`scripts/tcia_v2_checkpoint.py extract-from-audit` carries the immutable
`source_nifti__*` and `source_pathology__*` checkpoint forward. The historical
parity script and legacy builders remain migration-forensics tools only.

The streamlined release uses compact public audit schema 3. Schema 3 treats
the original public non-DICOM database
as a short-lived assembly database and projects fresh research and audit
outputs rather than clearing columns and vacuuming that multi-GB file in place.
Capacity and release-size reviews must measure those projected outputs, not the
assembly: the assembly intentionally repeats entity-local field provenance that
the audit projection subsequently normalizes and deduplicates.
It uses integer entity/field links, 32-byte digest BLOBs, and `WITHOUT ROWID`
link tables. It also removes the redundant entity index that duplicated the
schema-2 composite primary key.

File-level `field_provenance_json` remains lossless at the parsed-value level
but is normalized instead of stored as one nearly unique document per asset:

- `provenance_source_payloads` stores each canonical source payload once;
- `provenance_decision_payloads` stores repeated decision attributes once;
- `entity_provenance_sources` retains each entity-local source label;
- `field_provenance` joins a field decision to its labeled source; and
- `agent_normalized_field_provenance` exposes the joined audit rows.

Use `scripts/tcia_v2_audit.py verify-reconstruction` to compare exact document,
source-link, and field-decision totals and to reconstruct a deterministic sample
against the assembly database. The production workflow requires this check to
pass before deleting the assembly database. Its diagnostic artifact also
retains projection/compression timings, output sizes, largest-table sizes, and
the reconstruction report. Research and audit gzip outputs are compressed in
parallel at level 3, using `pigz` when present and a portable Python gzip
fallback otherwise.

The materialized streamlined release contains exactly ten files: seven
compressed SQLite databases (`tcia_snapshot`, Participant Inventory, public
non-DICOM, clinical, controlled-access, and both audit companions), the two
compressed current dataset/download JSONL exports, and the authoritative bundle
manifest. NIfTI/pathology specialized rows live in the public non-DICOM audit
checkpoint, and the clinical manual-review CSV is retained losslessly as a
Participant Inventory audit table. Component hashes, decompressed SQLite
hashes, schemas, fingerprints, profiles, and provenance are inline in the
bundle manifest, so per-component manifests are unnecessary. The installer
uses this inline streamlined contract for the moving V2 release. Standalone
NIfTI/pathology components are absent from every current bundle contract.

Verbose provenance and troubleshooting payloads are distributed separately as
`public_non_dicom_audit.sqlite.gz` and `participant_inventory_audit.sqlite.gz`.
Use the `audit_support` profile in the bundle manifest when those companions are
needed; stable entity IDs provide the join back to the research databases.

Use `scripts/tcia_v2_bundle.py install` for the stable V2 release. Its
per-component validation metadata is inline in the bundle manifest rather than
distributed as separate manifests.

NIfTI and pathology discovery now comes through the unified public non-DICOM
research artifact. Their specialized source rows remain losslessly embedded in
the public non-DICOM audit companion rather than published as separate SQLite
assets.

The stable asset URLs are:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/public_non_dicom_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/participant_inventory.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/public_non_dicom_audit.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/participant_inventory_audit.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-latest/tcia_metadata_v2_bundle_manifest.json`

Use `tcia_metadata_v2_bundle_manifest.json` to verify each selected asset's
schema, release fingerprint, compressed SHA-256, and decompressed SQLite
SHA-256 before replacing a local copy. Per-component JSON manifests are not
separate streamlined-release assets.
