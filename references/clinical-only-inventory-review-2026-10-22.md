# Clinical-only participant inventory review — 2026-10-22

This review covers **all 21 additional datasets / 2,441 dataset-scoped records**
identified in the October 21 bundle, beyond NLST, CBIS-DDSM, and TOMPEI-CMMD.
The baseline fingerprint is
`ca1034fa1696b8ce2d2338a44cbc1b7ad21fcf8ce0385cb6ea3ef864d6a1d37b`.
These are derived inventory records, not a count of distinct people or proof
that images do not exist. Original TCIA downloads remain the source authority.

## Findings and changes

| Dataset | Initially no direct imaging asset | Finding / action | Manual QC records |
|---|---:|---|---:|
| Lung-PET-CT-Dx | 356 | 354 explicit `newpatientid`–`dicom_patient_id` pairs from the normalized official table. Two obsolete A0266 identities resolve to Lung_Dx-A0251 under TCIA's published Version 2 correction. Raw source values retained. | 0 |
| CDD-CESM | 326 | Patient_ID and Image_name in the same official annotation row explicitly link numeric IDs to P-prefixed image owners. | 0 |
| UCSD-BMETS-Longitudinal | 300 | The public package contains 1,065 study directories for exactly 300 participants, matching all official MRI metadata Study IDs. Add study-grain availability; file counts and file-level metadata remain unavailable. | 0 |
| ISPY2 | 266 | The official ACRIN-6698 clinical crosswalk pairs each I-SPY2 research ID with its ACRIN DICOM ID. Add 266 Collection equivalences; retain separate publication memberships and original imaging ownership. | 0 |
| ACRIN 6685-Tumor-Annotations | 258 | All 258 already have explicit source-Collection imaging links. Absence of a direct annotation asset is a coverage distinction, not missing source images. No fabricated DICOM annotation inventory. | 0 |
| Breast-Lesions-USG | 256 | CaseID and Image_filename in the same official workbook row explicitly link IDs to caseNNN image owners. | 0 |
| ISPY1 | 221 | Normalized official clinical rows explicitly pair subjectid with dicom_patient_id. | 0 |
| CFB-GBM | 98 | The published unique patient-number definition and `[id_patient]_[temporality]_[modality].nii.gz` convention support 97 finite zero-padding aliases to existing image folders. No image folder 089 is present for clinical ID 89. | 1 |
| ACRIN-FLT-Breast | 90 | Correct 83 finite, unique underscore/hyphen namespace spellings. Seven clinical IDs have no current imaging counterpart. TCIA describes 90 enrolled patients and 83 with available imaging. | 7 |
| TCGA-Breast-Radiogenomics | 84 | All 84 already have explicit source-Collection imaging links. Retain the distinct Analysis Result membership. | 0 |
| Breast-MRI-NACT-Pilot | 64 | Normalized official rows explicitly pair patient_id UCSF_BR_NN with dicom_patient_id UCSF-BR-NN. | 0 |
| HistologyHSI-BC-Recurrence | 47 | Published HSI naming identifies the embedded patient ID. Correct ownership of 5,856 existing WSI/HSI/companion assets to 47 clinical patients, preserving asset IDs, paths and raw capture IDs. Do not interpret capture IDs as additional participants. One separate PathDB file-identity collision needs QC. | 0 |
| ACRIN-Contralateral-Breast-MR | 23 | Clinical tables contain 1,007 IDs; published imaging cohort has 984. No supported alternative identifier found for these 23. | 23 |
| AURORA-Metastatic-Breast-Multiomics | 18 | Seventeen IDs occur only in the Normal tissues study sheet; AFUL occurs in the AURORA study sheet without a current image owner. Do not infer a donor/specimen crosswalk. | 18 |
| Spinal-Multiple-Myeloma-SEG | 10 | Each _a/_b row's exact conventional CT SeriesInstanceUID matches a canonical Myel_NNN PatientID in IDC v25.0.5. Link the ten visit identifiers to five participants while preserving visit-specific source values. | 0 |
| ACRIN-NSCLC-FDG-PET | 9 | 251 clinical IDs versus 242 current imaging IDs; source describes imaging for 242 initially enrolled patients. Review exclusions and the additional clinical identifier. | 9 |
| ACRIN-FMISO-Brain | 5 | 50 clinical IDs versus 45 with current imaging. Source discusses enrollment/evaluability differences; do not infer missing images or merge IDs. | 5 |
| DLBCL-Morphology | 5 | 209 clinical cases, 204 currently linked image owners. TMA/ROI membership needs review. A patch called 13909.png occurs under case folder 13915: patch basenames cannot establish patient identity. | 5 |
| Head-Neck-PET-CT | 2 | Both unmatched IDs occur in the workbook's Excluded sheet. Preserve them pending an inventory-scope decision. | 2 |
| LDCT-and-Projection-data | 2 | GE and Siemens are scanner manufacturers incorrectly delivered as dicom_patient_id. Rekey 462 source rows and their facts to the explicit abdomen_case/chest_case fields (127 distinct cases); preserve original row JSON. Apply to reused sources as well as new ingestion. | 0 |
| HNSCC | 1 | HNSCC-01-0295 is in official clinical data and historical membership but lacks current controlled image membership. Verify the current manifest before changing availability. | 1 |

The fixes remove **1,762 direct-only classifications**, leaving **679** records
without direct imaging assets. Of those, **608** have verified imaging through
source links or the new I-SPY2/ACRIN equivalences. The remaining **71 records in
nine datasets** are retained unchanged for manual QC / inventory-scope review.
See [the exact QC IDs and questions](clinical_only_manual_qc_v1.json).

A separate HistologyHSI PathDB record (`source_record_id=raw`) collapsed 408
capture identifiers onto one asset with an empty path. Its unsupported
participant edges are removed, while the original asset, locations and raw
links remain QC evidence. The 47 patients are independently covered by Aspera
files; reconstructing the PathDB file identities requires manual review.

## Evidence and verification boundaries

- [Finite identity mappings](clinical_only_identity_repairs_v1.json) preserve
  dataset scopes, paired source fields, source-row hashes, source URLs and the
  exact DICOM series evidence used for Spinal-Multiple-Myeloma-SEG.
- [HistologyHSI ownership links](histology_hsi_participant_links_v1.csv) are
  hash-bound by their adjacent JSON provenance record. All original asset IDs
  and raw capture identifiers remain available.
- [UCSD study directories](ucsd_bmets_study_inventory_v1.csv) are hash-bound to
  public Faspex browse metadata and the official MRI metadata. A study
  directory is not counted as a single image file. Full voxel/geometry checks
  were not performed.
- The ACRIN annotations and TCGA breast Analysis Result source links were
  checked against existing source participants with imaging assets. This
  review does not claim annotation-file completeness.
- Tests include exact source-binding failures, unmatched-case preservation,
  per-alias evidence validation, source-row rekeying, raw-value preservation,
  repeat-run safety, and honest study-versus-file counts.
- Local candidates use the pinned release inputs to isolate the effect of
  these repairs. Local verification and a push to main do not establish a new
  metadata publication or deployment. The scheduled builder must still pass
  its exact semantic-change review gates; those gates have not been waived.

## Primary sources for manual review

- [CFB-GBM](https://www.cancerimagingarchive.net/collection/cfb-gbm/)
- [ACRIN-FLT-Breast](https://www.cancerimagingarchive.net/collection/acrin-flt-breast/)
- [ACRIN-Contralateral-Breast-MR](https://www.cancerimagingarchive.net/collection/acrin-contralateral-breast-mr/)
- [ACRIN-NSCLC-FDG-PET](https://www.cancerimagingarchive.net/collection/acrin-nsclc-fdg-pet/)
- [ACRIN-FMISO-Brain](https://www.cancerimagingarchive.net/collection/acrin-fmiso-brain/)
- [DLBCL-Morphology](https://www.cancerimagingarchive.net/collection/dlbcl-morphology/)
- [Head-Neck-PET-CT](https://www.cancerimagingarchive.net/collection/head-neck-pet-ct/)
- [HNSCC](https://www.cancerimagingarchive.net/collection/hnscc/)
- [AURORA-Metastatic-Breast-Multiomics](https://www.cancerimagingarchive.net/collection/aurora-metastatic-breast-multiomics/)
