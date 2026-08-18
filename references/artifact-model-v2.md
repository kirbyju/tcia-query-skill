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
| Public non-DICOM metadata | Public imaging and imaging-derived objects not represented by IDC; the historical artifact name also carries narrowly scoped Aspera-only public-DICOM exceptions | Optional detail artifact |
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

The initial builder imports current visible public non-DICOM download
declarations from WordPress, file-level NIfTI rows from the legacy NIfTI
artifact, original package-file inventories from the pathology artifact, and
PathDB file rows in full release mode.

Public DICOM normally remains an IDC query concern and is not duplicated here.
When a TCIA-published public DICOM representation is distributed through an
Aspera package but is absent from IDC, retain a compact exception in the same
physical detail artifact with `file_format='DICOM'`,
`source_system='tcia_aspera'`, and participant/modality-level represented-file
counts. This keeps the Participant Inventory complete without implying IDC
availability or copying hundreds of thousands of instance rows. The BraTS 2021
parallel `*Set_dcm` trees are the initial reviewed exception; their companion
NIfTI trees are marked `standardized_representation`, while the package DICOM
trees are marked `submitted_original`.

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
one-row-per-canonical-participant search contract. Present user-centered summaries such as
CT series, MHA volumes, pathology images, capsule-endoscopy videos, clinical
data, and access level. Do not make users understand internal system names.

Provide an expandable provenance/troubleshooting surface containing managed
system, location, source version, checksums, original-versus-standardized
status, and reconciliation warnings.

Query IDC for public DICOM detail. Query the public non-DICOM, controlled, or
clinical detail artifacts only when the user drills down.

## Release isolation

Keep `tcia-snapshot-latest` unchanged while V2 is evaluated. Publish an
official-shaped, internally consistent bundle to the isolated
`tcia-metadata-v2-preview` prerelease for Participant Explorer integration
testing. The preview contains these asset groups:

- Research core: `tcia_snapshot.sqlite.gz`, compact
  `participant_inventory.sqlite.gz`, their manifests, and the compressed
  current dataset/download exports. This is the default install profile.
- Research detail: public non-DICOM, NIfTI, pathology, controlled-access, and
  clinical SQLite databases and manifests. These are fetched only for relevant
  drill-down workflows.
- Audit support: `public_non_dicom_audit.sqlite.gz`,
  `participant_inventory_audit.sqlite.gz`, their manifests, and the clinical
  manual-review queue. Audit companions hold verbose JSON provenance and
  detailed crosswalk evidence keyed by stable research-artifact identifiers.
- Compatibility exports: uncompressed and historical JSONL exports retained
  for specialized consumers, but excluded from ordinary downloads.
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
python3 scripts/tcia_v2_bundle.py expected-assets --profile research_core
python3 scripts/tcia_v2_bundle.py expected-assets --profile research_detail
python3 scripts/tcia_v2_bundle.py expected-assets --profile audit_support
```

After the contracts stabilize, publish the same bundle shape as an immutable
versioned release and a separate `tcia-metadata-v2-latest` moving tag. Do not
replace or delete the May 2026-compatible artifact line during the preview
period.

The V2 build runs after each successful scheduled base-snapshot workflow. It
captures the V1 release record, verifies every copied source asset against the
captured GitHub digest, regenerates all web exports from that exact bundled
snapshot, validates every component, and publishes only when the complete
bundle fingerprint changes. The top-level manifest is uploaded last so
consumers never accept an update without a complete hash contract. Stale assets
are removed only from the V2 preview tag, and that preview tag is advanced to
the producer commit only after the published bundle passes remote digest
validation. The workflow never uploads to
`tcia-snapshot-latest` and does not deploy, restart, or reconfigure MCP/REST.

Consumers can retrieve and validate the two research databases independently:

```bash
python3 scripts/tcia_public_non_dicom_metadata.py ensure
python3 scripts/tcia_participant_inventory.py ensure
```

Verbose provenance and troubleshooting payloads are distributed separately as
`public_non_dicom_audit.sqlite.gz` and `participant_inventory_audit.sqlite.gz`.
Use the `audit_support` profile in the bundle manifest when those companions are
needed; stable entity IDs provide the join back to the research databases.

Existing helpers can test the copied compatibility artifacts from the same V2
release by overriding their tag, for example:

```bash
python3 scripts/tcia_snapshot.py ensure --tag tcia-metadata-v2-preview
python3 scripts/tcia_clinical_metadata.py ensure --tag tcia-metadata-v2-preview
python3 scripts/tcia_controlled_access_metadata.py ensure --tag tcia-metadata-v2-preview
python3 scripts/tcia_nifti_metadata.py ensure --tag tcia-metadata-v2-preview
python3 scripts/tcia_pathology_metadata.py ensure --tag tcia-metadata-v2-preview
```

The stable preview asset URLs are:

- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-preview/public_non_dicom_metadata.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-preview/participant_inventory.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-preview/public_non_dicom_audit.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-preview/participant_inventory_audit.sqlite.gz`
- `https://github.com/kirbyju/tcia-query-skill/releases/download/tcia-metadata-v2-preview/tcia_metadata_v2_bundle_manifest.json`

Use the corresponding JSON manifest from the same release to verify schema,
release fingerprint, compressed SHA-256, and SQLite SHA-256 before replacing a
local copy.
