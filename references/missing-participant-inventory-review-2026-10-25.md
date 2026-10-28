# Preliminary review of 33 missing participant inventories

Reviewed October 25, 2026. Tracking: **WORK-0045 (high priority)**.

The live curated summary and dataset coverage endpoints identified these exact 33 datasets in release `171f50880bb668eae21dd505a8de71bb68e4bd4a840966c378528c762a70fbe2` (generated October 24). All 33 current download IDs and URLs agree with the pinned October 21 source catalog used for local testing; the supporting files were retrieved and hashed on October 25.

## Result

- Local source-bound imports add coverage for 32 datasets: 28 match their published participant aggregate and four remain partial. Matching an aggregate supports the preliminary assessment; it does not prove complete source ingestion.
- One dataset remains without imported participants: NSCLC-Cetuximab, deferred pending its CTDC re-release. MRI-DIR was resolved by user review on October 27.
- The imports establish 17,532 dataset-scoped memberships from 23,458 source evidence rows, **not 17,532 new unique people**. Published archive totals must come from the rebuilt curated summary after all normal source-linking and release checks.
- MIDI-B contributes 538 benchmark identities, with synthetic/curated copies reconciled using the explicit one-to-one mapping and curated metadata. It does not contribute 1,076 humans.
- All added holdings are explicitly `participant_metadata` / `metadata_only`: no image/file/series counts, byte sizes, geometry, local image availability, or clinical facts are inferred. Controlled download access labels remain controlled.
- Nine source Collections gain membership only for patients whose annotation report references a series in that Collection’s exact manifest. AHEP0731 uses its own public NBIA patient metadata, covering 190 source subjects; its annotations cover 80. The corresponding 3,032 annotation-to-Collection links retain finite evidence.
- Original source files stay outside the repository. The compact membership CSV and JSON manifest retain raw participant IDs, source locators, source hashes, URL bindings, extraction rules, and review date. Changed current download URLs fail closed.

## All 33 datasets

| Dataset | Published subjects | Imported IDs | Preliminary disposition | Source evidence |
|---|---:|---:|---|---|
| [A091105](https://www.cancerimagingarchive.net/collection/a091105/) | 83 | 83 | Supported import; aggregate matches | annotation PatientID + referenced series in source manifest |
| [A091105-Tumor-Annotations](https://www.cancerimagingarchive.net/analysis-result/a091105-tumor-annotations/) | 83 | 83 | Supported import; aggregate matches | patient-level CSV |
| [ACNS0332](https://www.cancerimagingarchive.net/collection/acns0332/) | 85 | 85 | Supported import; aggregate matches | annotation PatientID + referenced series in source manifest |
| [ACNS0332-Tumor-Annotations](https://www.cancerimagingarchive.net/analysis-result/acns0332-tumor-annotations/) | 85 | 85 | Supported import; aggregate matches | patient-level CSV |
| [AHEP0731](https://www.cancerimagingarchive.net/collection/ahep0731/) | 190 | 190 | Supported import; aggregate matches | public NBIA patient metadata |
| [AHEP0731-Tumor-Annotations](https://www.cancerimagingarchive.net/analysis-result/ahep0731-tumor-annotations/) | 80 | 80 | Supported import; aggregate matches | patient-level CSV |
| [AHOD0831](https://www.cancerimagingarchive.net/collection/ahod0831/) | 165 | 165 | Supported import; aggregate matches | annotation PatientID + referenced series in source manifest |
| [AHOD0831-Tumor-Annotations](https://www.cancerimagingarchive.net/analysis-result/ahod0831-tumor-annotations/) | 165 | 165 | Supported import; aggregate matches | patient-level CSV |
| [ARAR0331](https://www.cancerimagingarchive.net/collection/arar0331/) | 108 | 108 | Supported import; aggregate matches | annotation PatientID + referenced series in source manifest |
| [ARAR0331-Tumor-Annotations](https://www.cancerimagingarchive.net/analysis-result/arar0331-tumor-annotations/) | 108 | 108 | Supported import; aggregate matches | patient-level CSV |
| [AREN0532](https://www.cancerimagingarchive.net/collection/aren0532/) | 544 | 542 | Partial; CTDC re-release dependency confirmed | annotation PatientID + referenced series in source manifest |
| [AREN0532-Tumor-Annotations](https://www.cancerimagingarchive.net/analysis-result/aren0532-tumor-annotations/) | 542 | 542 | Supported import; aggregate matches | patient-level CSV |
| [AREN0533](https://www.cancerimagingarchive.net/collection/aren0533/) | 294 | 294 | Supported import; aggregate matches | annotation PatientID + referenced series in source manifest |
| [AREN0533-Tumor-Annotations](https://www.cancerimagingarchive.net/analysis-result/aren0533-tumor-annotations/) | 294 | 294 | Supported import; aggregate matches | patient-level CSV |
| [AREN0534](https://www.cancerimagingarchive.net/collection/aren0534/) | 239 | 239 | Supported import; aggregate matches | annotation PatientID + referenced series in source manifest |
| [AREN0534-Tumor-Annotations](https://www.cancerimagingarchive.net/analysis-result/aren0534-tumor-annotations/) | 239 | 239 | Supported import; aggregate matches | patient-level CSV |
| [CALGB50303](https://www.cancerimagingarchive.net/collection/calgb50303/) | 155 | 155 | Supported import; aggregate matches | annotation PatientID + referenced series in source manifest |
| [CALGB50303-Tumor-Annotations](https://www.cancerimagingarchive.net/analysis-result/calgb50303-tumor-annotations/) | 155 | 155 | Supported import; aggregate matches | patient-level CSV |
| [CPTAC-HNSCC-Tumor-Annotations](https://www.cancerimagingarchive.net/analysis-result/cptac-hnscc-tumor-annotations/) | 101 | 101 | Supported import; aggregate matches | patient-level CSV |
| [Crowds-Cure-2017](https://www.cancerimagingarchive.net/analysis-result/crowds-cure-2017/) | 352 | 352 | Supported import; aggregate matches | patient-level CSV |
| [Crowds-Cure-2018](https://www.cancerimagingarchive.net/analysis-result/crowds-cure-2018/) | 324 | 324 | Supported import; aggregate matches | patient-level CSV |
| [DI-Cubed-Reports](https://www.cancerimagingarchive.net/analysis-result/di-cubed-reports/) | 516 | 516 | Supported import; aggregate matches | SDTM dm.csv / USUBJID |
| [ExACT](https://www.cancerimagingarchive.net/collection/exact/) | 30 | 30 | Supported import; aggregate matches | public NBIA patient metadata |
| [MIDI-B-Test-MIDI-B-Validation](https://www.cancerimagingarchive.net/collection/midi-b-test-midi-b-validation/) | 538 | 538 | Supported import; aggregate matches | patient mapping + curated NBIA metadata |
| [MRI-DIR](https://www.cancerimagingarchive.net/collection/mri-dir/) | 9 | 9 | User approved October 27; eight computational + one physical phantom | The metadata identifies eight synthetic image sets (MRI-DIR-T1_1 through T1_4 and T2_1 through T2_4) plus MRI-DIR-zzmeatphantom |
| [NRG-1308](https://www.cancerimagingarchive.net/collection/nrg-1308/) | 12 | 12 | Supported import; aggregate matches | public NBIA patient metadata |
| [NSCLC-Cetuximab](https://www.cancerimagingarchive.net/collection/nsclc-cetuximab/) | 490 | 0 | Deferred pending CTDC re-release | Legacy series-only .tcia manifest; no linked patient metadata |
| [Pan-Cancer-Nuclei-Seg](https://www.cancerimagingarchive.net/analysis-result/pan-cancer-nuclei-seg/) | 5204 | 4,635 | Partial; manual review | official slide/QC/patch case barcodes |
| [QIN-SARCOMA](https://www.cancerimagingarchive.net/collection/qin-sarcoma/) | 15 | 15 | Supported import; aggregate matches | public NBIA patient metadata |
| [S0819](https://www.cancerimagingarchive.net/collection/s0819/) | 1299 | 1,281 | Partial; CTDC re-release dependency confirmed | annotation PatientID + referenced series in source manifest |
| [S0819-Tumor-Annotations](https://www.cancerimagingarchive.net/analysis-result/s0819-tumor-annotations/) | 1281 | 1,281 | Supported import; aggregate matches | patient-level CSV |
| [TCGA-GBM-Radiogenomics](https://www.cancerimagingarchive.net/analysis-result/tcga-gbm-radiogenomics/) | 75 | 75 | Supported import; aggregate matches | CombinedRadiologyPredictors workbook sheet |
| [TIL-WSI-TCGA](https://www.cancerimagingarchive.net/analysis-result/til-wsi-tcga/) | 4759 | 4,820 | Approved IDC-matched cohort; ten excluded | All Box map folders + IDC radiology/pathology identifiers |

## Walkthrough queue

### MRI-DIR

The metadata identifies eight synthetic image sets (MRI-DIR-T1_1 through T1_4 and T2_1 through T2_4) plus MRI-DIR-zzmeatphantom. The narrative describes porcine phantom material and synthetic images derived from 28 patients. All 61 metadata rows are labeled Homo sapiens, which conflicts with interpreting the nine IDs as nine people.

**Resolved October 27:** The user approved eight computational phantom identities and one physical phantom, contributing zero human participants. The local implementation preserves the original source label and stores the explicit per-identifier classification and approval. No 28-person cohort is inferred. Publication is pending.

### NSCLC-Cetuximab

The controlled download still points to a legacy .tcia manifest containing series UIDs; no public patient-level metadata spreadsheet is linked. The current GC/CTDC importer does not ingest this legacy route.

**Clarified October 27:** The user reports that the Collection has not yet been re-released in CTDC but should be soon (no confirmed date). Defer the Collection inventory until official patient/series metadata becomes available. Related Analysis Result annotations may remain in NBIA for several more months before eventual CTDC submission. Track and verify those separately; do not assume they are available or equate them with the full source Collection. A focused pinned-catalog narrative check found no explicit named result link, which does not establish current NBIA absence. Two manual reviews remain after the subsequent AREN/S0819 clarifications: TIL-WSI-TCGA and Pan-Cancer-Nuclei-Seg.

### AREN0532

Source-reference matches establish 542 participants, while the Collection lists 544. The annotation cohort itself also lists 542.

**Clarified October 27:** The user confirms that AREN0532, AREN0533, and AREN0534 await CTDC re-release, while their open-access annotation Analysis Results remain available in NBIA. Retain the verified subsets and separate inventories with documented source links. For AREN0532, 542 versus 544 is expected partial coverage pending official source metadata, not an established mapping error. Reconcile the full Collection inventory after CTDC re-release.

### S0819

Source-reference matches establish the 1,281 annotated subjects, versus 1,299 in the Collection.

**Clarified October 27:** The user confirms S0819 is also awaiting CTDC re-release, with open-access annotations still available in NBIA. Retain the 1,281 verified subjects and documented annotation/source links. The difference from the 1,299 listed Collection subjects remains expected partial coverage until the full source metadata is released.

### TIL-WSI-TCGA

The official browser main index returns 4,520 slide rows and 4,505 distinct PatientIDs (paginate=false), compared with 5,202 slides and 4,759 subjects listed on the dataset page. Only observed IDs were imported.

**Decision / next evidence:** Resolve whether the browser index is incomplete/outdated or the published aggregate needs correction, and identify the full source index. The difference of 254 is a count discrepancy, not proof of missing images.

### Pan-Cancer-Nuclei-Seg

The union of four official QC/slide/patch lists establishes 4,635 TCGA cases. The verified-WSI list alone has 4,186 cases versus its listed 4,187; the overall dataset lists 5,204. Lists contain overlapping subsets and QC-failure rows, which remain source membership evidence.

**Decision / next evidence:** Confirm the full package participant list and intended QC subset policy, and reconcile the 5,204 overall / 4,187 verified-subset aggregates. Do not fabricate the 569-count difference or remove published subjects solely for a QC flag.

## Additional source caveats

TCGA-GBM-Radiogenomics retains all 75 subjects explicitly present in its published radiology predictor table, including a row annotated as subsequently disqualified from TCGA. Analysis Result membership and membership in the current source Collection are separate; this review does not silently drop an original analysis subject or force a source match. The page itself reports a 71-subject source-image subset.

The TIL and Pan-Cancer sources are derived from GDC pathology cohorts. The October 27 repair binds finite TCGA case/project identities to official source locators and the GDC-documented participant-barcode namespace, corroborated by IDC v25 original SM metadata. Existing TCIA Collection endpoints are linked exactly where present. Original GDC-only source anchors deduplicate confirmed cross-result overlap without creating TCIA Collection memberships or implying current source-image availability. Unrelated bare identifiers are never matched.

## Implementation and validation

`scripts/tcia_missing_inventory_repairs.py` deterministically regenerates the reviewed CSV from hash-pinned local source files. The normal Participant Inventory builder imports these rows before identity resolution and source linking, adds the finite annotation/parent links, and supplies reviewed human-species labels only when the dataset lacks one and the reviewed inventory was actually imported. Existing benchmark classification takes precedence.

Validation includes negative probes for changed source/download hashes, hidden scopes, controlled access, unsupported parent-series matches, paginated browser data, and non-bijective MIDI mappings. A full local candidate based on the previously validated October 22 inventory is checked for SQLite integrity, foreign keys, participant identity and group invariants. This local candidate is not the October 24 live release and is not a new published archive total.

No release workflow, approval waiver, deployment, or service restart was performed. The normal semantic release gate remains required.

## October 27 TIL source and identity repair

The user supplied the official external resource [Box share](https://stonybrookmedicine.app.box.com/v/cellreportspaper). The main `til_maps.zip` is 1,907,934,914 bytes; only its 1,968,702-byte central directory and end record were read using HTTP 206 byte ranges. No map/image payloads were transferred. Original file names, per-entry sizes/CRC values and directory hashes were retained outside the repository; the reviewed membership CSV preserves exact selected-variant ZIP entry locators.

| Source representation | Files/rows | Distinct TCGA cases |
|---|---:|---:|
| Browser main index | 4,520 slides | 4,505 |
| Clustering metadata ZIP | 5,146 slides | 4,704 |
| `TIL_maps_after_thres_v1` | 5,202 map files | 4,751 |
| `TIL_maps_before_thres_v1` | 5,202 map files | 4,751 |
| `TIL_maps_before_thres_v2` | 5,107 map files | 4,670 |

The browser's case list is a subset of the v1 map cases. Unioning all archive variants would produce 4,830 case IDs, an unsupported expansion of the published cohort. The local inventory uses `TIL_maps_after_thres_v1` with explicit representation provenance, not a union. The listed 4,759-patient aggregate remains unchanged in the authoritative source and differs from this exact case count by eight.

Participant IDs are the participant portion of the TCGA slide barcode, following the [GDC barcode specification](https://docs.gdc.cancer.gov/Encyclopedia/pages/TCGA_Barcode/). The source project is explicit in each archive directory, not inferred from a bare ID. IDC original SM evidence is pinned to v25 / index 25.0.5, `analysis_result_id IS NULL`, and `Modality='SM'`; current availability and identity are separate facts.

- 4,103 TIL cases link to existing participants in their exact TCIA TCGA Collection. The validated local candidate has zero matched source participants left in different identity groups.
- 639 cases belong to original GDC/IDC projects absent from the current visible TCIA Collection catalog: PAAD 170, SKCM 406 and UVM 63. No TCIA Collection records were invented for these projects.
- Nine additional historical source cases belong to TCIA-published TCGA projects but are absent from their current participant inventories. In total ten v1 cases lack current original-SM IDC matches, including one SKCM case. Their exact IDs remain in the QC ledger; absence is not interpreted as a new unrelated person or proof that all source data is absent.
- All 4,751 retain explicit original TCGA project/case lineage. Finite original-source anchors also group the 4,153 case IDs shared with Pan-Cancer-Nuclei-Seg exactly once. This is a documented TCGA namespace crosswalk, not an archive-wide bare-ID join.
- The full local candidate passes integrity, foreign keys and participant/group validation with zero rejected group links. These source changes remain unpublished, and the normal semantic release gate is still required.

## October 28 approved TIL inventory policy (supersedes the v1-only selection)

TIL-WSI-TCGA has one TCIA dataset release. The `v1` and `v2` names identify
internal map representations in the same Box archive, not TCIA release versions.
The complete browser index contains 4,505 patients (4,520 slide rows), all of
whom occur in Box and match IDC pathology PatientIDs. The union of all three
Box map folders contains 4,830 distinct TCGA participant barcodes, including
325 absent from the browser and 79 beyond the earlier v1-only inventory.

The user approved inclusion of the **4,820** Box cases that match current IDC
radiology or pathology identifiers, and exclusion of the ten cases absent from
IDC. In IDC v25 / index 25.0.5, all 4,820 included cases have original pathology;
452 also have original radiology. An independent query for the ten excluded
IDs across all IDC collections and modalities returned no rows. The exact
comparison and IDs are retained in
`til_idc_presence_review_2026_10_28.json`; the recipe pins its SHA-256 and keeps
excluded case/project IDs and the explicit user decision as provenance.

Extraction now visits all three explicitly selected folders, validates each
folder's file-entry count and every case/project pair, deduplicates participant
barcodes, and applies the finite approved exclusion list. This adds 79 TIL
memberships and removes ten relative to the October 27 inventory, for a net
increase of 69 dataset memberships. Counts for other datasets are unchanged.
Existing TCGA source identity links and original-project anchors still apply;
this does not infer additional globally unique people or image/file counts.
The TCIA page's reported 4,759 aggregate is preserved as source metadata.

The exclusions apply to TIL membership. They do not delete unrelated source
records, change upstream data, or imply that the Box maps are missing. This
resolves the TIL inventory decision; Pan-Cancer and CTDC follow-ups remain open.
Implementation is local until a new validated artifact release is published.

## October 28 approved Pan-Cancer inventory policy

The complete filename-only listing of all 14 polygon directories in the
open Faspex package establishes 5,200 distinct TCGA cases in 6,142 slide-named
entries. The manual-patch crosswalk adds 20 cases, giving 5,220 documented
memberships. In current IDC v25 / index 25.0.5, 5,212 have pathology images;
742 also have radiology images. A second query across all IDC collections
and modalities finds no records for the eight excluded cases. The user
approved **5,212 included participants** and the eight explicit exclusions,
using the same IDC-presence policy as TIL. Of these, 5,196 are documented in
the whole-slide package and 16 additionally occur in the manual-patch table.
QC status remains separate from dataset membership.

Ten exact slide names occur under both BRCA and LUAD package folders. Their
original IDC project is unambiguously TCGA-LUAD. The recipe preserves each
original directory locator and permits only those ten exact, reviewed
case/project exceptions. It never creates duplicate BRCA identities.

All 14 directory listings, the package/source binding, and the IDC comparison
are hash-pinned. Both package and original supporting-text recipes apply
finite source-complete selections, so the eight exclusions cannot return
through another supporting file. This adds 584 and removes seven Pan-Cancer
memberships relative to the published inventory, net +577; no new unique
human count is inferred from those dataset memberships. Source-reported
aggregates (5,204 overall and 4,187 verified-subset) remain unchanged as raw
source metadata. No image or segmentation payloads were transferred.

TIL and Pan-Cancer manual reviews are resolved. Combined atomic artifact
publication and the agreed user-managed server update remain separate steps;
CTDC re-release follow-ups remain deferred.
