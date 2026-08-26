#!/usr/bin/env python3
"""Build, download, and query an optional patient-level TCIA clinical SQLite.

The database preserves source rows and resolves common patient-level concepts
with this precedence:

    official TCIA clinical downloads > TCIA-linked external clinical sources
    > IDC clinical tables > CDA > DICOM > single-label WordPress Collection
    inference

IDC clinical tables are treated as a normalized delivery of official
collection tabular data, not as an independent corroborating source. CDA
supplies harmonized enrichment, and DICOM is used only as a sparse fallback.
Conflicts are kept in ``clinical_facts`` and surfaced by
``agent_clinical_conflicts``.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 17
CLINICAL_ASSET = "clinical_metadata.sqlite.gz"
CLINICAL_MANIFEST_ASSET = "clinical_metadata_manifest.json"
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_DB = SKILL_ROOT / "cache" / "tcia_snapshot.sqlite"
DEFAULT_DB_PATH = SKILL_ROOT / "cache" / "clinical_metadata.sqlite"
DEFAULT_MANIFEST_PATH = SKILL_ROOT / "cache" / CLINICAL_MANIFEST_ASSET
USER_AGENT = "tcia-clinical-metadata/0.1"
IVYGAP_TUMOR_DETAILS_URL = (
    "https://glioblastoma.alleninstitute.org/api/v2/gbm/tumor_details.csv"
)
VICTRE_REPOSITORY_URL = "https://github.com/DIDSR/VICTRE"
VICTRE_LOCATION_README_URL = (
    "https://raw.githubusercontent.com/DIDSR/VICTRE/master/Locations/readme.txt"
)
VICTRE_LOCATION_ARCHIVE_URLS = {
    density: (
        "https://raw.githubusercontent.com/DIDSR/VICTRE/master/Locations/"
        f"{density}_SP_phantom_vox.tar.gz"
    )
    for density in ("dense", "fatty", "hetero", "scattered")
}

SOURCE_PRIORITIES = {
    "tcia_clinical_download": 400,
    "tcia_linked_external_clinical": 350,
    "idc_clinical": 300,
    "cda": 200,
    "dicom": 100,
    # Exact PatientID inheritance is a fallback. Any direct target-dataset
    # fact, including DICOM, outranks it; it still outranks dataset-level
    # WordPress label inference.
    "tcia_collection_subject_inheritance": 75,
    "wordpress_dataset_inference": 50,
}

# These mappings are intentionally scoped to datasets whose official TCIA
# clinical package or dictionary defines the codes. Numeric demographic codes
# are not portable across studies (for example, EA1141 defines 1 as Female).
DATASET_SPECIFIC_CONCEPT_CODES = {
    "acrincontralateralbreastmr": {
        "sex_at_birth": {"1": "Male", "2": "Female"},
    },
    "acrinfltbreast": {
        "sex_at_birth": {"1": "Male", "2": "Female"},
    },
    "acrinnsclcfdgpet": {
        "sex_at_birth": {"1": "Male", "2": "Female"},
    },
    "colorectallivermetastases": {
        "sex_at_birth": {"1": "Male", "2": "Female"},
        "vital_status": {"0": "Alive", "1": "Dead"},
    },
    "hcctaceseg": {
        "sex_at_birth": {"1": "Male", "2": "Female"},
    },
    "histologyhsibcrecurrence": {
        "vital_status": {"0": "Alive", "1": "Dead"},
    },
    "ea1141": {
        "sex_at_birth": {"1": "Female"},
    },
    "ucsfpdgm": {
        "vital_status": {"0": "Alive", "1": "Dead"},
    },
}

CANONICAL_DISPLAY_VALUES = {
    "sex_at_birth": {
        "male": "Male",
        "female": "Female",
        "other": "Other",
        "unknown": "Unknown",
    },
    "vital_status": {
        "alive": "Alive",
        "dead": "Dead",
    },
    "recurrence": {
        "yes": "Yes",
        "no": "No",
        "unknown": "Unknown",
    },
    "progression": {
        "yes": "Yes",
        "no": "No",
        "unknown": "Unknown",
    },
}

GENERIC_DATASET_LABELS = {
    "mixed",
    "multiple",
    "not applicable",
    "not specified",
    "other",
    "unknown",
    "various",
}

SUBJECT_COLUMN_ALIASES = {
    "subjectid",
    "subject",
    "patientid",
    "patient",
    "participantid",
    "participant",
    "caseid",
    "case",
    "tciasubjectid",
    "tciapatientid",
    "tciaid",
    "tcianumber",
    "submitterid",
    "patientidentifier",
    "subjectidentifier",
    "participantidentifier",
    "anonpatientid",
    "anonymizedpatientid",
    "collectionsubjectid",
    "subjectde",
    "tciaradiomicsdummyidoftosubmitfinal",
    "tciaradiomicsdummyidofmergedupdatedasrmv2",
}

CONCEPT_ALIASES = {
    "sex_at_birth": {
        "sex",
        "gender",
        "sexatbirth",
        "patientsex",
        "biologicalsex",
    },
    "race": {"race", "patientrace"},
    "ethnicity": {"ethnicity", "ethnicgroup", "patientethnicity"},
    "age_at_diagnosis": {
        "ageatdiagnosis",
        "diagnosisage",
        "ageofdiagnosis",
    },
    "age_at_enrollment_years": {
        "ageatenrollment",
        "ageatenrollmentyears",
        "ageinyearsatregistration",
        "ageinyearsenrollment",
        "ageinyearsatenrollment",
    },
    "age_at_imaging_years": {
        "age",
        "patientage",
        "ageatimaging",
        "ageatscan",
        "scanage",
    },
    "primary_diagnosis": {
        "diagnosis",
        "primarydiagnosis",
        "disease",
        "histology",
        "pathology",
        "detype",
        "lcmorph",
    },
    "primary_site": {
        "primarysite",
        "tumorsite",
        "diseasesite",
        "anatomicsite",
        "cancersite",
        "lctopog",
    },
    "stage": {
        "stage",
        "clinicalstage",
        "pathologicstage",
        "tumorstage",
        "ajccstage",
        "destag",
        "pathstag",
        "stagesum",
        "stageonly",
        "destag7thed",
    },
    "grade": {
        "grade",
        "tumorgrade",
        "histologicgrade",
        "degrade",
        "lcgrade",
        "sbrgrade",
    },
    "vital_status": {
        "vitalstatus",
        "survivalstatus",
        "patientstatus",
        "status",
        "deceased",
    },
    "days_to_death": {"daystodeath", "deathdays", "timetodeathdays"},
    "days_to_last_followup": {
        "daystolastfollowup",
        "lastfollowupdays",
        "followupdays",
    },
    "overall_survival_days": {
        "overallsurvivaldays",
        "osdays",
        "survivaldays",
        "overallsurvival",
    },
    "progression_free_survival_days": {
        "progressionfreesurvivaldays",
        "pfsdays",
        "progressionfreesurvival",
    },
    "recurrence": {"recurrence", "recurrencestatus", "recurred"},
    "progression": {"progression", "progressionstatus", "progressed"},
    "response": {
        "response",
        "treatmentresponse",
        "bestresponse",
        "responsecategory",
        "pcr",
        "pcrstatus",
        "pathologiccompleteresponse",
        "pathologicalcompleteresponse",
    },
    "screening_result": {
        "screeningresult",
        "screeningstatus",
        "polypstatus",
    },
}

SOURCE_COLUMN_CONCEPT_OVERRIDES = {
    # The ACRIN-6698 workbook calls this column simply "age", while its
    # official data dictionary defines it as age in years at enrollment.
    ("acrin6698", "age"): "age_at_enrollment_years",
    # The EA1141 dictionary defines AGE as age at study enrollment.
    ("ea1141", "age"): "age_at_enrollment_years",
    # A0.csv defines entryage as age at protocol enrollment.
    ("acrinfmisobrain", "entryage"): "age_at_enrollment_years",
    # The official data dictionary defines Age as age at operation.
    ("colorectallivermetastases", "age"): "age_at_treatment_years",
    ("hnscc", "ageatdiag"): "age_at_diagnosis",
    ("hnscc", "cancersubsiteoforigin"): "primary_site",
    ("hnscc", "ajccstage7thedition"): "stage",
    ("hnscc", "daystolastfu"): "days_to_last_followup",
    ("hnscc", "aliveordead"): "vital_status",
    ("hnscc", "site"): "primary_site",
    # Reviewed bare-ID clinical tables. Preserve every original column/value;
    # these aliases expose the most useful patient-level concepts consistently.
    ("headneckradiomicshn1", "performancestatusecog"): "performance_status",
    ("headneckradiomicshn1", "overallsurvivalindays"): "overall_survival_days",
    ("tompeicmmd", "classification"): "screening_result",
    ("tompeicmmd", "breastdensity"): "breast_density",
    ("tompeicmmd", "biradscategory"): "birads_category",
    ("ucsdptgbm", "who2021diagnosis"): "primary_diagnosis",
    ("ucsdptgbm", "nonwho2021diagnosis"): "primary_diagnosis",
    ("ucsdptgbm", "mgmt"): "mgmt_status",
    ("ucsdptgbm", "idh"): "idh_status",
    ("ucsdptgbm", "1p19q"): "chromosomal_1p19q_status",
    ("ucsdptgbm", "atrx"): "atrx_status",
    ("ucsfpdgm", "ageatmri"): "age_at_imaging_years",
    ("ucsfpdgm", "whocnsgrade"): "grade",
    ("ucsfpdgm", "finalpathologicdiagnosiswho2021"): "primary_diagnosis",
    ("ucsfpdgm", "mgmtstatus"): "mgmt_status",
    ("ucsfpdgm", "mgmtindex"): "mgmt_index",
    ("ucsfpdgm", "1p19q"): "chromosomal_1p19q_status",
    ("ucsfpdgm", "idh"): "idh_status",
    ("ucsfpdgm", "1dead0alive"): "vital_status",
    ("ucsfpdgm", "os"): "overall_survival_days",
    ("ucsfpdgm", "eor"): "extent_of_resection",
    ("ucsfpdgm", "biopsypriortoimaging"): "biopsy_prior_to_imaging",
    ("upenngbm", "ageatscanyears"): "age_at_imaging_years",
    ("upenngbm", "survivalfromsurgerydaysupdated"): "overall_survival_days",
    ("upenngbm", "idh1"): "idh_status",
    ("upenngbm", "mgmt"): "mgmt_status",
    ("upenngbm", "kps"): "performance_status",
    ("upenngbm", "gtrover90percent"): "extent_of_resection",
    ("upenngbm", "timesincebaselinepreop"): "days_from_baseline_preop",
    ("upenngbm", "psptpscore"): "pseudoprogression_true_progression_score",
    # The Yale workbook defines this study-level column as age at imaging.
    # Scanner and file-grain fields use the longitudinal observation surface
    # below so repeated visits are not misclassified as patient conflicts.
    ("yalebrainmetslongitudinal", "ageatimagingyears"): "age_at_imaging_years",
    ("bcbmradiogenomics", "er"): "estrogen_receptor_status",
    ("bcbmradiogenomics", "pr"): "progesterone_receptor_status",
    ("bcbmradiogenomics", "her2"): "her2_status",
    ("braintrgammaknife", "diagnosisonlywantmets"): "metastatic_diagnosis",
    ("braintrgammaknife", "primarydiagnosis"): "primary_diagnosis",
    # ReMIND's dictionary defines Age relative to surgery, not imaging.
    ("remind", "age"): "age_at_treatment_years",
    ("remind", "whograde"): "grade",
    ("remind", "histopathology"): "primary_diagnosis",
    ("remind", "idhmutant"): "idh_status",
    ("remind", "1p19qcodeleted"): "chromosomal_1p19q_status",
    ("remind", "mgmtpromotermethylation"): "mgmt_status",
    ("remind", "laterality"): "tumor_laterality",
    ("remind", "previouscraniotomy"): "previous_craniotomy",
    ("remind", "postoperativegtr"): "postoperative_extent_of_resection",
    ("remind", "intraoperativegtr"): "intraoperative_extent_of_resection",
    ("remind", "tumorlocation"): "tumor_location",
    ("remind", "preoperativetumorvolume"): "preoperative_tumor_volume_ml",
    ("remind", "awakecraniotomy"): "awake_craniotomy",
    ("remind", "preoperativemriscanner"): "preoperative_mri_scanner",
    ("remind", "tesla"): "magnetic_field_strength_t",
    ("remind", "ultrasoundmissing"): "ultrasound_missing_reason",
}

SOURCE_COLUMN_UNIT_OVERRIDES = {
    ("yalebrainmetslongitudinal", "ageatimagingyears"): "years",
    ("remind", "age"): "years",
    ("remind", "preoperativetumorvolume"): "mL",
    ("remind", "tesla"): "T",
}

CURATED_SCREENING_DIAGNOSIS_RESOLUTIONS = {
    "ACRIN-6698": {
        "review_reason": "screening_review_resolved_confirmed_diagnosis",
        "review_evidence": (
            "TCIA Collection description states that 406 women with invasive "
            "breast cancer were prospectively enrolled. The official 385-row "
            "ancillary workbook contains four subjects with age, race, lesion "
            "type, tumor grade, and pCR all blank; blanks are treated as "
            "missing ancillary data, not evidence of a non-cancer subject."
        ),
    },
    "HNSCC": {
        "review_reason": "screening_review_resolved_confirmed_diagnosis",
        "review_evidence": (
            "The TCIA Collection description states that the Collection "
            "contains data from 627 head and neck squamous cell carcinoma "
            "patients. Its use of 'screened' describes selection of an "
            "already-diagnosed HNSCC source cohort, not cancer screening. "
            "The two official patient tables contain 492 and 215 unique "
            "subjects with 80 overlapping, yielding exactly the 627-subject "
            "Collection cohort; the CT-atlas table reports SCC histology for "
            "all 215 of its subjects."
        ),
    },
    "IvyGAP": {
        "review_reason": "screening_review_resolved_confirmed_diagnosis",
        "review_evidence": (
            "TCIA describes IvyGAP as the MRI/CT cohort for brain tumor "
            "patients in the Ivy Glioblastoma Atlas Project and labels the "
            "Collection Glioblastoma/Brain. The screen* matches in the "
            "description refer to gene-expression experimental screens, not "
            "cancer screening. Allen tumor_details.csv contains a matched "
            "glioblastoma tumor record for every one of the 39 current TCIA "
            "General Commons Participant IDs."
        ),
    },
    "VICTRE": {
        "review_reason": "screening_review_resolved_patient_level_mixed_cohort",
        "review_evidence": (
            "VICTRE is a synthetic mixed cohort. FDA VICTRE filenames in the "
            "TCIA DICOM metadata distinguish signal-absent pc phantoms from "
            "lesion-present pcl phantoms, and the linked FDA location archives "
            "provide patient-level lesion ground truth. Collection-level "
            "Breast Cancer inference remains disabled so signal-absent "
            "phantoms are not mislabeled as cancer."
        ),
        "allow_dataset_inference": False,
    },
}

# These cohorts must remain in patient-level-only review even if a future
# WordPress edit removes the word "screening" from the page text. Their
# official subject-level transformations classify mixed outcomes safely;
# Collection-level cancer/site labels must never refill unresolved subjects.
PERMANENT_SCREENING_REVIEW_DATASETS = {
    "EA1141",
    "Hungarian-Colorectal-Screening",
}

CT_COLONOGRAPHY_HISTOLOGY = {
    1: "Adenocarcinoma",
    2: "Medullary carcinoma",
    3: "Mucinous carcinoma",
    4: "Signet ring cell carcinoma",
    5: "Squamous cell carcinoma",
    6: "Adenosquamous carcinoma",
    7: "Small cell carcinoma",
    8: "Undifferentiated carcinoma",
    9: "Carcinoma, NOS",
    10: "Hyperplastic polyp",
    11: "Lipoma",
    12: "Adenomatous polyp",
    13: "Tubular adenoma",
    14: "Tubulovillous adenoma",
    15: "Villous adenoma",
    16: "Tubulovillous adenoma with dysplasia",
    17: "Normal mucosa",
    88: "Other histology",
    98: "Not applicable",
}

CT_COLONOGRAPHY_NONMALIGNANT_SEVERITY = (16, 15, 14, 13, 12, 10, 11, 17)

EA1141_RACE = {
    "1": "American Indian/Alaska Native",
    "2": "Asian",
    "3": "Black/African American",
    "4": "Native Hawaiian/Other Pacific Islander",
    "5": "White",
    "6": "Multiple races reported",
    "98": "Not reported",
    "99": "Unknown",
}

EA1141_ETHNICITY = {
    "1": "Hispanic/Latino",
    "2": "Not Hispanic/Latino",
    "98": "Not reported",
    "99": "Unknown",
}

EA1141_GRADE = {
    "1": "Grade I",
    "2": "Grade II",
    "3": "Grade III",
}

EA1141_HANDLED_COLUMNS = {
    "age",
    "sex",
    "race",
    "ethnicity",
    "year0sensspecrefstd",
    "mrilesionoutcomeyr0",
    "mrilesionoutcomedetailyr0",
    "mricorepathgradeyr0",
    "mrisurgpathgradeyr0",
    "tomolesionoutcomeyr0",
    "tomolesionoutcomedetailyr0",
    "tomocorepathgradeyr0",
    "tomosurgpathgradeyr0",
}

HNSCC_HANDLED_COLUMNS = {
    "histology",
}

SUBJECT_COLUMN_OVERRIDES = {
    # A0.csv uses the ACRIN case number column `cn`; it maps to the suffix of
    # the TCIA PatientID after zero-padding to three digits.
    "acrinfmisobrain": {"cn"},
    # The accompanying table also contains a four-character patient_id, but
    # the full TCGA barcode is the identifier shared with TCGA-BRCA.
    "tcgabreastradiogenomics": {"bcrpatientbarcode"},
    # This official file has one row for each of the 200 PathDB patients and
    # identifies them with the otherwise-too-generic column name "ID".
    "hungariancolorectalscreening": {"id"},
    # These official tables use a bare ID column. Each mapping is scoped to
    # one reviewed dataset because ID is too ambiguous to accept globally.
    "headneckradiomicshn1": {"id"},
    "tompeicmmd": {"id", "id1"},
    "ucsdptgbm": {"id"},
    "ucsdvslongitudinal": {"id"},
    "ucsfpdgm": {"id"},
    "upenngbm": {"id"},
    # Both official BCBM workbooks identify an imaging visit with the full
    # patient-plus-scan identifier. The reviewed mapping below projects it to
    # patient grain while retaining the source value in the raw row.
    "bcbmradiogenomics": {"id", "filenameprefix"},
    "braintrgammaknife": {"uniqueptid"},
    "remind": {"casenumber"},
}

# Increment only the affected official downloads when a reviewed identifier
# mapping changes. This prevents a same-schema incremental build from reusing
# an older no_patient_rows result without forcing every official source to be
# downloaded again.
OFFICIAL_SOURCE_TRANSFORM_VERSIONS = {
    "headneckradiomicshn1": 1,
    "tompeicmmd": 1,
    "ucsdptgbm": 1,
    "ucsdvslongitudinal": 1,
    "ucsfpdgm": 1,
    "upenngbm": 1,
    "yalebrainmetslongitudinal": 1,
    "bcbmradiogenomics": 1,
    "braintrgammaknife": 1,
    "remind": 1,
}

REVIEWED_OFFICIAL_COHORT_PATTERNS = {
    "HEAD-NECK-RADIOMICS-HN1": r"HN\d{4}",
    "UCSD-PTGBM": r"UCSD-PTGBM-\d{4}",
    "UCSD-VS-Longitudinal": r"VS_\d{4}",
    "UCSF-PDGM": r"UCSF-PDGM-\d{4}",
    "UPENN-GBM": r"UPENN-GBM-\d{5}",
    "BCBM-RadioGenomics": r"BCBM-RadioGenomics-\d+",
    "Brain-TR-GammaKnife": r"GK_\d{3}",
    "ReMIND": r"ReMIND-\d{3}",
}

HUNGARIAN_COLORECTAL_ICD10 = {
    "C18": {
        "diagnosis": "Malignant neoplasm of colon",
        "site": "Colon",
        "screening_result": "Malignant",
    },
    "C20": {
        "diagnosis": "Malignant neoplasm of rectum",
        "site": "Rectum",
        "screening_result": "Malignant",
    },
    "C76": {
        "diagnosis": "Malignant neoplasm of other and ill-defined sites",
        "site": "",
        "screening_result": "Malignant",
    },
    "D12": {
        "diagnosis": "Benign neoplasm of colon, rectum, anus and anal canal",
        "site": "Colon, rectum, anus and anal canal",
        "screening_result": "Non-malignant finding",
    },
    "K52": {
        "diagnosis": "Other noninfective gastroenteritis and colitis",
        "site": "",
        "screening_result": "Non-malignant finding",
    },
    "K62": {
        "diagnosis": "Other diseases of anus and rectum",
        "site": "Anus and rectum",
        "screening_result": "Non-malignant finding",
    },
    "K63": {
        "diagnosis": "Other diseases of intestine",
        "site": "Intestine",
        "screening_result": "Non-malignant finding",
    },
    "R89": {
        # WHO defines this as an abnormal specimen finding without a diagnosis.
        # Preserve and classify it, but do not invent a primary diagnosis/site.
        "diagnosis": "",
        "site": "",
        "screening_result": "Indeterminate",
    },
}

NUMERIC_CONCEPTS = {
    "age_at_diagnosis",
    "age_at_enrollment_years",
    "age_at_imaging_years",
    "age_at_treatment_years",
    "days_to_death",
    "days_to_last_followup",
    "overall_survival_days",
    "progression_free_survival_days",
    "initial_kps",
    "microcalcification_count",
    "spiculated_mass_count",
}

RESOLVED_COLUMNS = [
    "sex_at_birth",
    "race",
    "ethnicity",
    "age_at_diagnosis",
    "age_at_enrollment_years",
    "age_at_imaging_years",
    "age_at_treatment_years",
    "primary_diagnosis",
    "primary_site",
    "stage",
    "grade",
    "vital_status",
    "days_to_death",
    "days_to_last_followup",
    "overall_survival_days",
    "progression_free_survival_days",
    "recurrence",
    "progression",
    "response",
    "screening_result",
]

# Carry stable patient attributes and outcomes across a WordPress-confirmed
# Analysis Result -> Collection relationship after an exact PatientID match.
# Imaging age is intentionally excluded because it belongs to a particular
# acquisition and need not describe every derived result for that patient.
INHERITABLE_PATIENT_CONCEPTS = set(RESOLVED_COLUMNS) - {
    "age_at_imaging_years",
}

REQUIRED_TABLES = [
    "clinical_meta",
    "clinical_downloads",
    "clinical_idc_tables",
    "clinical_dictionary",
    "clinical_source_dictionary",
    "clinical_longitudinal_observations",
    "clinical_imaging_subjects",
    "clinical_dataset_inferences",
    "clinical_dataset_relationships",
    "clinical_sources",
    "clinical_rows",
    "clinical_facts",
    "clinical_subjects",
    "clinical_build_warnings",
    "clinical_qc_findings",
]
REQUIRED_VIEWS = [
    "agent_clinical_subjects",
    "agent_clinical_all_subjects",
    "agent_clinical_facts",
    "agent_clinical_conflicts",
    "agent_clinical_dataset_summary",
    "agent_clinical_source_tables",
    "agent_clinical_dictionary",
    "agent_clinical_source_dictionary",
    "agent_clinical_longitudinal_observations",
    "agent_clinical_imaging_subjects",
    "agent_clinical_dataset_inferences",
    "agent_clinical_dataset_relationships",
    "agent_clinical_qc_findings",
]
SOURCE_REUSE_TABLES = {
    "clinical_sources",
    "clinical_rows",
    "clinical_facts",
    "clinical_build_warnings",
}


class SimpleFrame:
    """Small pandas-compatible surface for standard-library delimited reads."""

    def __init__(self, columns: list[str], rows: list[dict[str, Any]]) -> None:
        self.columns = columns
        self._rows = rows

    @property
    def empty(self) -> bool:
        return not self._rows

    def iterrows(self) -> Iterable[tuple[int, dict[str, Any]]]:
        return enumerate(self._rows)


SCHEMA_SQL = """
CREATE TABLE clinical_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE clinical_downloads (
    source_id TEXT PRIMARY KEY,
    short_title TEXT NOT NULL,
    dataset_type TEXT,
    dataset_title TEXT,
    download_id TEXT,
    download_title TEXT,
    download_url TEXT,
    date_updated TEXT,
    file_types TEXT,
    download_types TEXT,
    data_types TEXT,
    access_level TEXT,
    controlled_access INTEGER NOT NULL DEFAULT 0,
    source_signature TEXT NOT NULL,
    ingest_status TEXT NOT NULL,
    rows_loaded INTEGER NOT NULL DEFAULT 0,
    subjects_loaded INTEGER NOT NULL DEFAULT 0,
    error_text TEXT
);

CREATE TABLE clinical_idc_tables (
    collection_id TEXT NOT NULL,
    short_title TEXT NOT NULL,
    table_name TEXT NOT NULL,
    idc_version TEXT NOT NULL,
    source_id TEXT,
    column_count INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0,
    subject_count INTEGER NOT NULL DEFAULT 0,
    subjects_with_imaging INTEGER NOT NULL DEFAULT 0,
    ingest_status TEXT NOT NULL,
    error_text TEXT,
    PRIMARY KEY (collection_id, table_name)
);

CREATE TABLE clinical_dictionary (
    collection_id TEXT NOT NULL,
    short_title TEXT NOT NULL,
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    column_label TEXT,
    values_json TEXT NOT NULL,
    idc_version TEXT NOT NULL,
    PRIMARY KEY (collection_id, table_name, column_name)
);

CREATE TABLE clinical_source_dictionary (
    dictionary_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    short_title TEXT NOT NULL,
    table_name TEXT NOT NULL,
    field_name TEXT NOT NULL,
    field_label TEXT,
    field_description TEXT,
    values_json TEXT NOT NULL DEFAULT '[]',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES clinical_sources(source_id)
);

CREATE TABLE clinical_longitudinal_observations (
    observation_id TEXT PRIMARY KEY,
    source_row_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    short_title TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    observation_type TEXT NOT NULL,
    study_datetime TEXT,
    file_name TEXT,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_row_id) REFERENCES clinical_rows(source_row_id),
    FOREIGN KEY (source_id) REFERENCES clinical_sources(source_id)
);

CREATE TABLE clinical_imaging_subjects (
    subject_key TEXT PRIMARY KEY,
    short_title TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    imaging_source TEXT NOT NULL
);

CREATE TABLE clinical_dataset_inferences (
    short_title TEXT NOT NULL,
    concept TEXT NOT NULL,
    source_field TEXT NOT NULL,
    raw_value TEXT,
    inferred_value TEXT,
    eligible INTEGER NOT NULL,
    eligibility_reason TEXT NOT NULL,
    review_required INTEGER NOT NULL DEFAULT 0,
    review_reason TEXT,
    review_evidence TEXT,
    screening_signal TEXT,
    candidate_subjects INTEGER NOT NULL DEFAULT 0,
    subjects_applied INTEGER NOT NULL DEFAULT 0,
    subjects_suppressed INTEGER NOT NULL DEFAULT 0,
    source_id TEXT,
    PRIMARY KEY (short_title, concept)
);

CREATE TABLE clinical_dataset_relationships (
    relationship_id TEXT PRIMARY KEY,
    target_short_title TEXT NOT NULL,
    source_short_title TEXT NOT NULL,
    relationship_source TEXT NOT NULL,
    relationship_evidence TEXT NOT NULL,
    target_subjects INTEGER NOT NULL DEFAULT 0,
    matched_subjects INTEGER NOT NULL DEFAULT 0,
    inherited_facts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    provenance_json TEXT NOT NULL
);

CREATE TABLE clinical_sources (
    source_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    source_lineage TEXT NOT NULL,
    short_title TEXT NOT NULL,
    source_url TEXT,
    source_date TEXT,
    source_signature TEXT NOT NULL,
    artifact_sha256 TEXT,
    artifact_bytes INTEGER,
    provenance_json TEXT
);

CREATE TABLE clinical_rows (
    source_row_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    short_title TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    table_name TEXT,
    row_number INTEGER,
    has_imaging INTEGER NOT NULL DEFAULT 0,
    row_json TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES clinical_sources(source_id)
);

CREATE TABLE clinical_facts (
    fact_id TEXT PRIMARY KEY,
    source_row_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    short_title TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    concept TEXT NOT NULL,
    value_text TEXT NOT NULL,
    value_resolved TEXT NOT NULL,
    value_normalized TEXT NOT NULL,
    value_number REAL,
    unit TEXT,
    original_column TEXT,
    evidence_scope TEXT NOT NULL DEFAULT 'patient',
    is_inferred INTEGER NOT NULL DEFAULT 0,
    qc_excluded INTEGER NOT NULL DEFAULT 0,
    qc_status TEXT NOT NULL DEFAULT 'accepted',
    provenance_json TEXT,
    FOREIGN KEY (source_row_id) REFERENCES clinical_rows(source_row_id),
    FOREIGN KEY (source_id) REFERENCES clinical_sources(source_id)
);

CREATE TABLE clinical_subjects (
    subject_key TEXT PRIMARY KEY,
    short_title TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    source_kinds TEXT NOT NULL,
    source_count INTEGER NOT NULL,
    conflict_count INTEGER NOT NULL,
    has_imaging INTEGER NOT NULL DEFAULT 0,
    sex_at_birth TEXT,
    race TEXT,
    ethnicity TEXT,
    age_at_diagnosis TEXT,
    age_at_enrollment_years TEXT,
    age_at_imaging_years TEXT,
    age_at_treatment_years TEXT,
    primary_diagnosis TEXT,
    primary_site TEXT,
    stage TEXT,
    grade TEXT,
    vital_status TEXT,
    days_to_death TEXT,
    days_to_last_followup TEXT,
    overall_survival_days TEXT,
    progression_free_survival_days TEXT,
    recurrence TEXT,
    progression TEXT,
    response TEXT,
    screening_result TEXT,
    primary_diagnosis_is_inferred INTEGER NOT NULL DEFAULT 0,
    primary_site_is_inferred INTEGER NOT NULL DEFAULT 0,
    resolved_values_json TEXT NOT NULL,
    resolved_sources_json TEXT NOT NULL,
    conflicts_json TEXT NOT NULL
);

CREATE TABLE clinical_build_warnings (
    warning_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT,
    short_title TEXT,
    warning_type TEXT NOT NULL,
    warning_text TEXT NOT NULL
);

CREATE TABLE clinical_qc_findings (
    finding_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    disposition TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'open',
    short_title TEXT NOT NULL,
    subject_id TEXT,
    subject_key TEXT,
    source_id TEXT,
    source_row_id TEXT,
    fact_id TEXT,
    concept TEXT,
    original_value TEXT,
    resolved_value TEXT,
    unit TEXT,
    message TEXT NOT NULL,
    provenance_json TEXT NOT NULL
);

CREATE INDEX idx_clinical_rows_subject ON clinical_rows(short_title, subject_id);
CREATE INDEX idx_clinical_facts_subject ON clinical_facts(subject_key, concept);
CREATE INDEX idx_clinical_facts_concept ON clinical_facts(concept, value_normalized);
CREATE INDEX idx_clinical_facts_source ON clinical_facts(source_id);
CREATE INDEX idx_clinical_qc_rule ON clinical_qc_findings(rule_id, review_status);
CREATE INDEX idx_clinical_qc_subject ON clinical_qc_findings(short_title, subject_id);

CREATE VIEW agent_clinical_subjects AS
SELECT * FROM clinical_subjects
WHERE has_imaging = 1;

CREATE VIEW agent_clinical_all_subjects AS
SELECT * FROM clinical_subjects;

CREATE VIEW agent_clinical_facts AS
SELECT
    f.short_title,
    f.subject_id,
    f.concept,
    f.value_text,
    f.value_resolved,
    f.value_number,
    f.unit,
    f.source_kind,
    f.source_priority,
    s.source_url,
    s.source_date,
    f.original_column,
    f.evidence_scope,
    f.is_inferred,
    f.qc_excluded,
    f.qc_status,
    f.provenance_json
FROM clinical_facts f
JOIN clinical_sources s USING (source_id);

CREATE VIEW agent_clinical_conflicts AS
SELECT
    short_title,
    subject_id,
    concept,
    COUNT(DISTINCT value_normalized) AS distinct_values,
    GROUP_CONCAT(DISTINCT value_text) AS values_seen,
    GROUP_CONCAT(DISTINCT source_kind) AS source_kinds
FROM clinical_facts
WHERE qc_excluded = 0
  AND NOT (
      lower(replace(short_title, '-', '')) = 'yalebrainmetslongitudinal'
      AND concept = 'age_at_imaging_years'
  )
GROUP BY short_title, subject_key, concept
HAVING COUNT(DISTINCT value_normalized) > 1;

CREATE VIEW agent_clinical_dataset_summary AS
SELECT
    s.short_title,
    COUNT(DISTINCT CASE WHEN s.has_imaging = 1
                        THEN s.subject_key END) AS subjects,
    COUNT(DISTINCT s.subject_key) AS all_source_subjects,
    COUNT(DISTINCT CASE WHEN s.has_imaging = 0
                        THEN s.subject_key END) AS clinical_only_subjects,
    COUNT(DISTINCT CASE WHEN s.conflict_count > 0
                        THEN s.subject_key END) AS subjects_with_conflicts,
    COUNT(DISTINCT CASE WHEN s.primary_diagnosis_is_inferred = 1
                        THEN s.subject_key END) AS subjects_with_inferred_diagnosis,
    COUNT(DISTINCT CASE WHEN s.primary_site_is_inferred = 1
                        THEN s.subject_key END) AS subjects_with_inferred_site,
    GROUP_CONCAT(DISTINCT f.source_kind) AS source_kinds,
    COUNT(DISTINCT f.concept) AS concepts
FROM clinical_subjects s
LEFT JOIN clinical_facts f
       ON f.subject_key = s.subject_key AND f.qc_excluded = 0
GROUP BY s.short_title;

CREATE VIEW agent_clinical_source_tables AS
SELECT * FROM clinical_idc_tables;

CREATE VIEW agent_clinical_dictionary AS
SELECT * FROM clinical_dictionary;

CREATE VIEW agent_clinical_source_dictionary AS
SELECT * FROM clinical_source_dictionary;

CREATE VIEW agent_clinical_longitudinal_observations AS
SELECT
    observation_id,
    source_row_id,
    source_id,
    short_title,
    subject_id,
    observation_type,
    study_datetime,
    file_name,
    json_extract(attributes_json, '$.age_at_Imaging (years)') AS age_at_imaging_years,
    json_extract(attributes_json, '$.sex') AS sex,
    json_extract(attributes_json, '$.vendor') AS manufacturer,
    json_extract(attributes_json, '$.model') AS manufacturer_model_name,
    json_extract(attributes_json, '$.field_strength (T)') AS magnetic_field_strength_t,
    json_extract(attributes_json, '$.2D_3D_acquisition') AS acquisition_dimensionality,
    json_extract(attributes_json, '$.scanner_site') AS scanner_site,
    json_extract(attributes_json, '$.sequence_class') AS sequence_class,
    json_extract(attributes_json, '$.sequence_tags') AS sequence_tags,
    json_extract(attributes_json, '$.slice_thickness (mm)') AS slice_thickness_mm,
    json_extract(attributes_json, '$.spacing_between_slices (mm)') AS spacing_between_slices_mm,
    json_extract(attributes_json, '$.repetition_time (ms)') AS repetition_time_ms,
    json_extract(attributes_json, '$.echo_time (ms)') AS echo_time_ms,
    json_extract(attributes_json, '$.inversion_time (ms)') AS inversion_time_ms,
    attributes_json,
    provenance_json
FROM clinical_longitudinal_observations;

CREATE VIEW agent_clinical_imaging_subjects AS
SELECT * FROM clinical_imaging_subjects;

CREATE VIEW agent_clinical_dataset_inferences AS
SELECT * FROM clinical_dataset_inferences;

CREATE VIEW agent_clinical_dataset_relationships AS
SELECT * FROM clinical_dataset_relationships;

CREATE VIEW agent_clinical_qc_findings AS
SELECT
    finding_id,
    rule_id,
    severity,
    disposition,
    review_status,
    short_title,
    subject_id,
    concept,
    original_value,
    resolved_value,
    unit,
    message,
    source_id,
    source_row_id,
    fact_id,
    provenance_json
FROM clinical_qc_findings;
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_id(*parts: Any) -> str:
    text = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def normalize_subject(value: Any) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def clean_value(value: Any) -> str:
    if value is None:
        return ""
    try:
        if math.isnan(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null", "na", "n/a", "<na>"}:
        return ""
    return text


def normalize_value(value: str) -> str:
    return " ".join(value.strip().lower().split())


def normalize_concept_value(concept: str, value: str) -> str:
    normalized = normalize_value(value)
    if concept in NUMERIC_CONCEPTS:
        number = number_value(value)
        return f"{number:g}" if number is not None else normalized
    if concept == "sex_at_birth":
        return {
            "m": "male",
            "male": "male",
            "f": "female",
            "female": "female",
            "o": "other",
            "other": "other",
            "u": "unknown",
            "unknown": "unknown",
        }.get(normalized, normalized)
    if concept == "vital_status":
        return {
            "living": "alive",
            "alive": "alive",
            "dead": "dead",
            "deceased": "dead",
        }.get(normalized, normalized)
    if concept in {"recurrence", "progression"}:
        return {
            "y": "yes",
            "yes": "yes",
            "true": "yes",
            "1": "yes",
            "n": "no",
            "no": "no",
            "false": "no",
            "0": "no",
        }.get(normalized, normalized)
    return normalized


def normalize_dictionary_code(value: Any) -> str:
    """Match equivalent integral dictionary codes without global semantics.

    IDC dictionaries sometimes serialize an option as ``1.0`` while the
    corresponding clinical table returns ``1``. Only the representation is
    normalized here; the meaning still comes from that table's dictionary.
    """
    text = clean_value(value)
    match = re.fullmatch(r"([+-]?\d+)\.0+", text)
    return match.group(1) if match else text


def decode_dataset_concept(
    short_title: str, concept: str, value: Any
) -> str:
    """Apply only an audited dataset-specific coded-value dictionary."""
    text = clean_value(value)
    mappings = DATASET_SPECIFIC_CONCEPT_CODES.get(
        normalize_name(short_title), {}
    ).get(concept, {})
    return mappings.get(normalize_dictionary_code(text), text)


def canonical_display_map(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    """Choose one display spelling while retaining every raw clinical fact."""
    rows = conn.execute(
        """SELECT concept, value_normalized, value_resolved,
                  COUNT(*) AS frequency, MAX(source_priority) AS max_priority
           FROM clinical_facts
           WHERE qc_excluded = 0
           GROUP BY concept, value_normalized, value_resolved
           ORDER BY concept, value_normalized"""
    ).fetchall()

    def case_rank(value: str) -> int:
        letters = "".join(character for character in value if character.isalpha())
        has_lower = any(character.islower() for character in letters)
        has_upper = any(character.isupper() for character in letters)
        if not letters or (has_lower and has_upper):
            return 0
        return 1 if letters.isupper() else 2

    rows = sorted(
        rows,
        key=lambda row: (
            row["concept"],
            row["value_normalized"],
            -row["frequency"],
            -row["max_priority"],
            case_rank(row["value_resolved"]),
            row["value_resolved"].casefold(),
            row["value_resolved"],
        ),
    )
    result: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row["concept"], row["value_normalized"])
        result.setdefault(key, row["value_resolved"])
    for concept, values in CANONICAL_DISPLAY_VALUES.items():
        for normalized, display in values.items():
            result[(concept, normalized)] = display
    return result


def number_value(value: str) -> float | None:
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def concept_for_column(column: Any) -> str | None:
    normalized = normalize_name(column)
    for concept, aliases in CONCEPT_ALIASES.items():
        if normalized in aliases:
            return concept
    return None


def concept_for_source_column(
    short_title: str, column: Any, label: Any = ""
) -> str | None:
    override = SOURCE_COLUMN_CONCEPT_OVERRIDES.get(
        (normalize_name(short_title), normalize_name(column))
    )
    if override:
        return override
    return concept_for_column(column) or concept_for_column(label)


def unit_for_source_column(short_title: str, column: Any) -> str | None:
    return SOURCE_COLUMN_UNIT_OVERRIDES.get(
        (normalize_name(short_title), normalize_name(column))
    )


YALE_BRAIN_METS_DICTIONARY_FIELDS = {
    "patientid": "patient_id",
    "studydatetime": "study_datetime",
    "ageatstudy": "age_at_Imaging (years)",
    "sex": "sex",
}


def ingest_official_source_dictionary(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    short_title: str,
    table_name: str,
    frame: Any,
) -> int:
    """Preserve reviewed official workbook dictionaries as queryable rows."""
    dataset = normalize_name(short_title)
    is_yale = dataset == "yalebrainmetslongitudinal" and str(
        table_name
    ).casefold().endswith("::data dictionary")
    is_remind = dataset == "remind" and str(table_name).casefold().endswith(
        "::remind data dictionary"
    )
    if not is_yale and not is_remind:
        return 0
    columns = list(frame.columns)
    if len(columns) < 2:
        return 0
    label_column, description_column = columns[:2]
    inserted = 0
    records: list[tuple[int, str, str]] = []
    if is_remind:
        # The first ReMIND dictionary entry is stored in the worksheet header.
        records.append((1, clean_value(label_column), clean_value(description_column)))
    records.extend(
        (
            int(index) + 2 if isinstance(index, int) else offset + 2,
            clean_value(record[label_column]),
            clean_value(record[description_column]),
        )
        for offset, (index, record) in enumerate(frame.iterrows())
    )
    for row_number, field_label, description in records:
        field_name = (
            field_label
            if is_remind
            else YALE_BRAIN_METS_DICTIONARY_FIELDS.get(normalize_name(field_label))
        )
        if not field_name:
            continue
        dictionary_id = stable_id(
            source_id, table_name, row_number, field_name, description
        )
        conn.execute(
            """INSERT OR REPLACE INTO clinical_source_dictionary
               (dictionary_id, source_id, short_title, table_name, field_name,
                field_label, field_description, values_json, provenance_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?)""",
            (
                dictionary_id,
                source_id,
                short_title,
                table_name,
                field_name,
                field_label,
                description,
                json_dumps(
                    {
                        "row_number": row_number,
                        "label_column": str(label_column),
                        "description_column": str(description_column),
                    }
                ),
            ),
        )
        inserted += 1
    return inserted


def ingest_brain_tr_gammaknife_observation(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    short_title: str,
    subject_id: str,
    table_name: str,
    row_number: int,
    row: dict[str, Any],
) -> int:
    """Retain Gamma Knife treatment courses and lesion outcomes at source grain."""
    if normalize_name(short_title) != "braintrgammaknife":
        return 0
    sheet_name = str(table_name).rsplit("::", 1)[-1].casefold()
    normalized = {normalize_name(key): clean_value(value) for key, value in row.items()}
    if sheet_name == "course_level":
        observation_type = "radiotherapy_course"
        file_name = ""
    elif sheet_name == "lesion_level":
        observation_type = "lesion_followup_outcome"
        file_name = normalized.get("lesionnameinnrrdfiles", "")
    else:
        return 0
    cleaned_row = {str(key): clean_value(value) for key, value in row.items()}
    normalized_subject = clean_value(subject_id)
    subject_key = f"{normalize_name(short_title)}:{normalize_subject(normalized_subject)}"
    row_json = json_dumps(cleaned_row)
    source_row_id = stable_id(source_id, table_name, row_number, subject_key, row_json)
    observation_id = stable_id("observation", source_row_id, observation_type)
    conn.execute(
        """INSERT OR REPLACE INTO clinical_longitudinal_observations
           (observation_id, source_row_id, source_id, short_title, subject_id,
            observation_type, study_datetime, file_name, attributes_json,
            provenance_json)
           VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)""",
        (
            observation_id,
            source_row_id,
            source_id,
            short_title,
            normalized_subject,
            observation_type,
            file_name,
            row_json,
            json_dumps(
                {
                    "table_name": table_name,
                    "row_number": row_number,
                    "source_patient_id": normalized.get("uniqueptid", ""),
                    "treatment_course": normalized.get("course", "")
                    or normalized.get("treatmentcourse", ""),
                    "lesion_number": normalized.get("lesion", ""),
                }
            ),
        ),
    )
    return 1


def ingest_yale_longitudinal_observation(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    short_title: str,
    subject_id: str,
    table_name: str,
    row_number: int,
    row: dict[str, Any],
) -> int:
    """Expose Yale visit/file rows without treating expected variation as conflict."""
    if normalize_name(short_title) != "yalebrainmetslongitudinal":
        return 0
    sheet_name = str(table_name).rsplit("::", 1)[-1]
    observation_type = {
        "clinical_data": "clinical_study",
        "acquisition_data": "scanner_acquisition",
        "image_acquisition_parameters": "image_acquisition",
    }.get(sheet_name.casefold())
    if not observation_type:
        return 0
    cleaned_row = {str(key): clean_value(value) for key, value in row.items()}
    normalized_subject = clean_value(subject_id)
    subject_key = f"{normalize_name(short_title)}:{normalize_subject(normalized_subject)}"
    row_json = json_dumps(cleaned_row)
    source_row_id = stable_id(
        source_id, table_name, row_number, subject_key, row_json
    )
    observation_id = stable_id("observation", source_row_id, observation_type)
    conn.execute(
        """INSERT OR REPLACE INTO clinical_longitudinal_observations
           (observation_id, source_row_id, source_id, short_title, subject_id,
            observation_type, study_datetime, file_name, attributes_json,
            provenance_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            observation_id,
            source_row_id,
            source_id,
            short_title,
            normalized_subject,
            observation_type,
            clean_value(row.get("study_datetime")),
            clean_value(row.get("file_name")),
            row_json,
            json_dumps({"table_name": table_name, "row_number": row_number}),
        ),
    )
    return 1


def ingest_bcbm_longitudinal_observation(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    short_title: str,
    subject_id: str,
    table_name: str,
    row_number: int,
    row: dict[str, Any],
) -> int:
    """Preserve BCBM scan and radiomics rows at their native longitudinal grain."""
    if normalize_name(short_title) != "bcbmradiogenomics":
        return 0
    normalized = {normalize_name(key): clean_value(value) for key, value in row.items()}
    scan_id = normalized.get("id") or normalized.get("filenameprefix") or ""
    if not re.fullmatch(r"BCBM-RadioGenomics-\d+-\d+", scan_id, re.IGNORECASE):
        return 0
    sheet_name = str(table_name).rsplit("::", 1)[-1].casefold()
    if sheet_name == "clinical+genetics".casefold():
        observation_type = "scanner_clinical_scan"
        study_datetime = normalized.get("year", "")
        file_name = f"{scan_id}_image_ss_n4.nii.gz"
    elif sheet_name == "merged_orig".casefold():
        observation_type = "segmentation_radiomics"
        study_datetime = ""
        segmentation_name = normalized.get("segmentationname", "")
        file_name = (
            f"{scan_id}_{segmentation_name}.nii.gz" if segmentation_name else ""
        )
    else:
        return 0
    cleaned_row = {str(key): clean_value(value) for key, value in row.items()}
    normalized_subject = clean_value(subject_id)
    subject_key = f"{normalize_name(short_title)}:{normalize_subject(normalized_subject)}"
    row_json = json_dumps(cleaned_row)
    source_row_id = stable_id(source_id, table_name, row_number, subject_key, row_json)
    observation_id = stable_id("observation", source_row_id, observation_type)
    conn.execute(
        """INSERT OR REPLACE INTO clinical_longitudinal_observations
           (observation_id, source_row_id, source_id, short_title, subject_id,
            observation_type, study_datetime, file_name, attributes_json,
            provenance_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            observation_id,
            source_row_id,
            source_id,
            short_title,
            normalized_subject,
            observation_type,
            study_datetime,
            file_name,
            row_json,
            json_dumps(
                {
                    "table_name": table_name,
                    "row_number": row_number,
                    "source_scan_id": scan_id,
                }
            ),
        ),
    )
    return 1


def choose_subject_column(
    columns: Iterable[Any], short_title: str = ""
) -> Any | None:
    scored: list[tuple[int, int, Any]] = []
    overrides = SUBJECT_COLUMN_OVERRIDES.get(
        normalize_name(short_title), set()
    )
    for index, column in enumerate(columns):
        normalized = normalize_name(column)
        score = 0
        if normalized in overrides:
            score = 110
        elif normalized in SUBJECT_COLUMN_ALIASES:
            score = 100
        elif any(
            normalized.endswith(alias)
            for alias in SUBJECT_COLUMN_ALIASES
            if len(alias) >= 6
        ):
            score = 90
        if score:
            scored.append((score, -index, column))
    return max(scored)[2] if scored else None


def prepare_official_table(
    short_title: str, table_name: str, frame: Any
) -> Any:
    """Normalize a reviewed multi-row header without altering source cells."""
    if (
        normalize_name(short_title) == "tompeicmmd"
        and "lesiondetailssheet" in normalize_name(table_name)
        and len(frame.index) >= 3
    ):
        header_row = frame.iloc[1]
        headers: list[str] = []
        counts: dict[str, int] = {}
        for index, value in enumerate(header_row):
            header = clean_value(value) or f"column_{index + 1}"
            normalized = normalize_name(header) or f"column{index + 1}"
            counts[normalized] = counts.get(normalized, 0) + 1
            if counts[normalized] > 1:
                header = f"{header}_{counts[normalized]}"
            headers.append(header)
        prepared = frame.iloc[2:].copy()
        prepared.columns = headers
        return prepared.reset_index(drop=True)
    return frame


def official_subject_id_mapping(
    short_title: str, subject_id: str
) -> tuple[str, str]:
    """Map a reviewed official row identifier to TCIA patient identity.

    The returned method is stored in fact provenance. Raw source identifiers
    remain unchanged in ``clinical_rows.row_json``.
    """
    dataset = normalize_name(short_title)
    if dataset == "ea1141" and re.fullmatch(r"\d+", subject_id):
        return f"ea1141-{subject_id}", "dataset_prefix"
    if dataset == "acrinfmisobrain":
        match = re.fullmatch(r"(\d+)(?:\.0+)?", subject_id)
        if match:
            return (
                f"ACRIN-FMISO-Brain-{int(match.group(1)):03d}",
                "dataset_prefix_zero_pad_3",
            )
    if dataset == "ucsfpdgm":
        match = re.fullmatch(r"UCSF-PDGM-(\d{3,4})(?:_FU\d+d)?", subject_id)
        if match:
            return (
                f"UCSF-PDGM-{int(match.group(1)):04d}",
                "zero_pad_4_strip_followup_suffix",
            )
    if dataset == "upenngbm":
        match = re.fullmatch(r"(UPENN-GBM-\d{5})_\d+", subject_id)
        if match:
            return match.group(1), "strip_scan_suffix"
    if dataset == "ucsdptgbm":
        match = re.fullmatch(r"(UCSD-PTGBM-\d{4})_\d+", subject_id)
        if match:
            return match.group(1), "strip_scan_suffix"
    if dataset == "ucsdvslongitudinal":
        match = re.fullmatch(r"(VS_\d{4})_\d+", subject_id)
        if match:
            return match.group(1), "strip_visit_suffix"
    if dataset == "bcbmradiogenomics":
        match = re.fullmatch(r"(BCBM-RadioGenomics-\d+)-\d+", subject_id, re.IGNORECASE)
        if match:
            return match.group(1), "strip_scan_suffix"
    if dataset == "braintrgammaknife":
        match = re.fullmatch(r"(\d+)(?:\.0+)?", subject_id)
        if match:
            return f"GK_{int(match.group(1)):03d}", "dataset_prefix_zero_pad_3"
    if dataset == "remind":
        match = re.fullmatch(r"(?:ReMIND-)?(\d+)(?:\.0+)?", subject_id, re.IGNORECASE)
        if match:
            return f"ReMIND-{int(match.group(1)):03d}", "dataset_prefix_zero_pad_3"
    if dataset in SUBJECT_COLUMN_OVERRIDES:
        return subject_id, "dataset_specific_exact_id"
    return subject_id, "identity"


def normalize_official_subject_id(short_title: str, subject_id: str) -> str:
    """Map official spreadsheet IDs to the corresponding TCIA PatientID."""
    return official_subject_id_mapping(short_title, subject_id)[0]


def source_signature(row: sqlite3.Row) -> str:
    fields = [
        row["short_title"],
        row["dataset_type"],
        row["download_id"],
        row["download_title"],
        row["download_url"],
        row["date_updated"],
        row["file_types"],
        row["download_types"],
        row["data_types"],
        row["access_level"],
        row["controlled_access"],
    ]
    transform_version = OFFICIAL_SOURCE_TRANSFORM_VERSIONS.get(
        normalize_name(row["short_title"]), 0
    )
    if transform_version:
        fields.append(f"official_transform_v{transform_version}")
    return stable_id(*fields)


def is_clinical_download(row: sqlite3.Row) -> bool:
    keys = set(row.keys())
    download_types = (
        str(row["download_types"] or "").lower()
        if "download_types" in keys
        else ""
    )
    file_types = (
        str(row["file_types"] or "").lower()
        if "file_types" in keys
        else ""
    )
    if (
        normalize_name(row["short_title"] if "short_title" in keys else "")
        == "braintrgammaknife"
        and str(row["download_id"] if "download_id" in keys else "")
        in {"41531", "41533"}
    ):
        # These are explanatory DOCX documents, not patient tables. Their
        # WordPress provenance remains in the base snapshot.
        return False
    if (
        "radiology images" in download_types
        and "clinical data" not in download_types
        and "dicom" in file_types
    ):
        return False
    haystack = " ".join(
        str(row[key] or "")
        for key in ("download_title", "download_types", "data_types", "description")
    ).lower()
    if (
        normalize_name(row["short_title"] if "short_title" in keys else "")
        == "bcbmradiogenomics"
        and str(row["download_id"] if "download_id" in keys else "") in {"50323", "50399"}
    ):
        return True
    return any(
        token in haystack
        for token in (
            "clinical",
            "demographic",
            "diagnos",
            "outcome",
            "survival",
            "patient information",
            "patient data",
            "polyp",
        )
    )


def init_db(path: Path, *, replace: bool) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not replace:
            raise RuntimeError(f"Output already exists: {path}; pass --replace")
        path.unlink()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    return conn


def insert_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO clinical_meta(key, value) VALUES (?, ?)",
        (key, json_dumps(value)),
    )


def warning(
    conn: sqlite3.Connection,
    warning_type: str,
    warning_text: str,
    *,
    source_id: str | None = None,
    short_title: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO clinical_build_warnings
           (source_id, short_title, warning_type, warning_text)
           VALUES (?, ?, ?, ?)""",
        (source_id, short_title, warning_type, warning_text),
    )


def insert_source(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    source_kind: str,
    short_title: str,
    source_signature_value: str,
    source_lineage: str = "",
    source_url: str = "",
    source_date: str = "",
    artifact_sha256: str = "",
    artifact_bytes: int | None = None,
    provenance: Any = None,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO clinical_sources
           (source_id, source_kind, source_priority, source_lineage, short_title,
            source_url, source_date, source_signature, artifact_sha256,
            artifact_bytes, provenance_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_id,
            source_kind,
            SOURCE_PRIORITIES[source_kind],
            source_lineage or f"{source_kind}:{normalize_name(short_title)}",
            short_title,
            source_url,
            source_date,
            source_signature_value,
            artifact_sha256,
            artifact_bytes,
            json_dumps(provenance or {}),
        ),
    )


def insert_row_and_facts(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    source_kind: str,
    short_title: str,
    subject_id: str,
    table_name: str,
    row_number: int,
    row: dict[str, Any],
    facts: Iterable[tuple[str, str, str, str | None]],
    has_imaging: bool = False,
    evidence_scope: str = "patient",
    is_inferred: bool = False,
    fact_provenance: dict[str, Any] | None = None,
) -> bool:
    subject_id = clean_value(subject_id)
    if not subject_id:
        return False
    subject_key = f"{normalize_name(short_title)}:{normalize_subject(subject_id)}"
    cleaned_row = {str(key): clean_value(value) for key, value in row.items()}
    row_json = json_dumps(cleaned_row)
    source_row_id = stable_id(source_id, table_name, row_number, subject_key, row_json)
    conn.execute(
        """INSERT OR IGNORE INTO clinical_rows
           (source_row_id, source_id, short_title, subject_id, subject_key,
            table_name, row_number, has_imaging, row_json, row_sha256)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_row_id,
            source_id,
            short_title,
            subject_id,
            subject_key,
            table_name,
            row_number,
            int(has_imaging),
            row_json,
            sha256_bytes(row_json.encode("utf-8")),
        ),
    )
    for concept, value, original_column, unit in facts:
        value = clean_value(value)
        if not value:
            continue
        normalized = normalize_concept_value(concept, value)
        fact_id = stable_id(source_row_id, concept, normalized, original_column)
        conn.execute(
            """INSERT OR IGNORE INTO clinical_facts
               (fact_id, source_row_id, source_id, source_kind, source_priority,
                short_title, subject_id, subject_key, concept, value_text,
                value_resolved, value_normalized, value_number, unit,
                original_column, evidence_scope, is_inferred, qc_excluded,
                qc_status, provenance_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact_id,
                source_row_id,
                source_id,
                source_kind,
                SOURCE_PRIORITIES[source_kind],
                short_title,
                subject_id,
                subject_key,
                concept,
                value,
                value,
                normalized,
                number_value(value) if concept in NUMERIC_CONCEPTS else None,
                unit,
                original_column,
                evidence_scope,
                int(is_inferred),
                0,
                "accepted",
                json_dumps(
                    {
                        "table_name": table_name,
                        "row_number": row_number,
                        **(fact_provenance or {}),
                    }
                ),
            ),
        )
    return True


def clinical_downloads(snapshot_db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(snapshot_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT *
           FROM agent_current_downloads
           WHERE hidden = 0
           ORDER BY short_title, download_id"""
    ).fetchall()
    conn.close()
    return [row for row in rows if is_clinical_download(row)]


def visible_tcia_short_titles(snapshot_db: Path) -> set[str]:
    conn = sqlite3.connect(snapshot_db)
    try:
        rows = conn.execute(
            """SELECT DISTINCT short_title FROM agent_datasets
               WHERE hidden = 0 AND short_title IS NOT NULL"""
        ).fetchall()
    except sqlite3.Error:
        rows = conn.execute(
            """SELECT DISTINCT short_title FROM agent_current_downloads
               WHERE hidden = 0 AND short_title IS NOT NULL"""
        ).fetchall()
    conn.close()
    return {str(row[0]) for row in rows if row[0]}


def parse_dataset_labels(value: Any) -> list[str]:
    text = clean_value(value)
    if not text:
        return []
    values: list[Any]
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            values = parsed if isinstance(parsed, list) else [text]
        except json.JSONDecodeError:
            values = text.split(";")
    else:
        values = text.split(";")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        label = clean_value(item)
        normalized = normalize_value(label)
        if label and normalized not in seen:
            seen.add(normalized)
            result.append(label)
    return result


def eligible_dataset_label(value: Any) -> tuple[str, str]:
    labels = parse_dataset_labels(value)
    if not labels:
        return "", "missing"
    if len(labels) != 1:
        return "", "multiple_labels"
    label = labels[0]
    if normalize_value(label) in GENERIC_DATASET_LABELS:
        return "", "generic_label"
    return label, "eligible"


def dataset_screening_signal(dataset: sqlite3.Row) -> str:
    for field in ("short_title", "title", "summary", "abstract", "detailed_description"):
        match = re.search(r"\bscreen\w*", clean_value(dataset[field]), re.IGNORECASE)
        if match:
            return f"{field}:{match.group(0)}"
    return ""


def has_non_cancer_designation(value: Any) -> bool:
    return any(
        any(
            marker in normalize_name(label)
            for marker in ("noncancer", "nocancer", "cancerfree")
        )
        for label in parse_dataset_labels(value)
    )


def contains_dataset_short_title(text: Any, short_title: str) -> bool:
    """Return true for a delimited short-title mention in WordPress text."""
    value = clean_value(text)
    if not value or not short_title:
        return False
    pattern = rf"(?<![A-Za-z0-9]){re.escape(short_title)}(?![A-Za-z0-9])"
    return re.search(pattern, value, re.IGNORECASE) is not None


def wordpress_analysis_result_relationships(
    snapshot_db: Path,
) -> list[dict[str, Any]]:
    """Find strong WordPress Analysis Result -> Collection relationships.

    The Collection Manager's explicit source-collection prose is preferred.
    A Collection short title scoped in that Analysis Result's own download
    title/URL/search/metadata is also accepted. Program-wide related lists are
    deliberately ignored because they are not dataset-specific provenance.
    """
    snapshot = sqlite3.connect(snapshot_db)
    snapshot.row_factory = sqlite3.Row
    dataset_columns = {
        row[1] for row in snapshot.execute("PRAGMA table_info(agent_datasets)")
    }
    required = {"source", "short_title", "hidden", "source_collections"}
    if not required.issubset(dataset_columns):
        snapshot.close()
        return []
    collections = snapshot.execute(
        """SELECT short_title, title, link
           FROM agent_datasets
           WHERE source = 'collections' AND hidden = 0
             AND short_title IS NOT NULL
           ORDER BY length(short_title) DESC, short_title"""
    ).fetchall()
    results = snapshot.execute(
        """SELECT short_title, title, link, source_collections
           FROM agent_datasets
           WHERE source = 'analysis-results' AND hidden = 0
             AND short_title IS NOT NULL
           ORDER BY short_title"""
    ).fetchall()
    download_scope: dict[str, list[str]] = {}
    available = {
        row[1]
        for row in snapshot.execute("PRAGMA table_info(agent_current_downloads)")
    }
    scope_columns = [
        column
        for column in (
            "download_title",
            "download_url",
            "search_url",
            "download_metadata",
            "description",
        )
        if column in available
    ]
    if {"short_title", "parent_source"}.issubset(available) and scope_columns:
        select_scope = ", ".join(scope_columns)
        for row in snapshot.execute(
            f"""SELECT short_title, {select_scope}
                FROM agent_current_downloads
                WHERE parent_source = 'analysis-results' AND hidden = 0"""
        ):
            download_scope.setdefault(row["short_title"], []).append(
                " ".join(clean_value(row[column]) for column in scope_columns)
            )
    snapshot.close()

    relationships: list[dict[str, Any]] = []
    for result in results:
        target = clean_value(result["short_title"])
        source_text = clean_value(result["source_collections"])
        scoped_downloads = download_scope.get(target, [])
        for collection in collections:
            source = clean_value(collection["short_title"])
            evidence: list[str] = []
            if contains_dataset_short_title(source_text, source):
                evidence.append("wordpress_source_collections")
            matching_downloads = [
                text
                for text in scoped_downloads
                if contains_dataset_short_title(text, source)
            ]
            if matching_downloads:
                evidence.append("wordpress_scoped_download")
            if not evidence:
                continue
            relationships.append(
                {
                    "relationship_id": stable_id(target, source),
                    "target_short_title": target,
                    "target_title": clean_value(result["title"]),
                    "target_url": clean_value(result["link"]),
                    "source_short_title": source,
                    "source_title": clean_value(collection["title"]),
                    "source_url": clean_value(collection["link"]),
                    "relationship_source": ";".join(evidence),
                    "relationship_evidence": source_text
                    if "wordpress_source_collections" in evidence
                    else matching_downloads[0],
                }
            )
    return relationships


def apply_wordpress_dataset_inferences(
    conn: sqlite3.Connection, snapshot_db: Path
) -> dict[str, int]:
    """Backfill missing diagnosis/site from unambiguous Collection labels."""
    snapshot = sqlite3.connect(snapshot_db)
    snapshot.row_factory = sqlite3.Row
    columns = {
        row[1] for row in snapshot.execute("PRAGMA table_info(agent_datasets)")
    }
    required = {
        "short_title",
        "dataset_type",
        "hidden",
        "cancer_types",
        "cancer_locations",
        "summary",
        "abstract",
        "detailed_description",
    }
    if not required.issubset(columns):
        snapshot.close()
        return {
            "collections": 0,
            "eligible_labels": 0,
            "subjects_applied": 0,
            "screening_reviews_required": 0,
            "screening_reviews_resolved": 0,
        }
    dataset_rows = snapshot.execute(
        """SELECT short_title, title, link, date_updated,
                  cancer_types, cancer_locations, summary, abstract,
                  detailed_description
           FROM agent_datasets
           WHERE hidden = 0 AND dataset_type = 'Collection'
             AND short_title IS NOT NULL
           ORDER BY short_title"""
    ).fetchall()
    snapshot.close()

    results = {
        "collections": len(dataset_rows),
        "eligible_labels": 0,
        "subjects_applied": 0,
        "screening_reviews_required": 0,
        "screening_reviews_resolved": 0,
    }
    field_concepts = (
        ("primary_diagnosis", "cancer_types"),
        ("primary_site", "cancer_locations"),
    )
    for dataset in dataset_rows:
        short_title = clean_value(dataset["short_title"])
        subject_rows = conn.execute(
            """SELECT subject_key, subject_id
               FROM clinical_imaging_subjects
               WHERE short_title = ?
               ORDER BY subject_key""",
            (short_title,),
        ).fetchall()
        candidates = len(subject_rows)
        candidate_keys = {row["subject_key"] for row in subject_rows}
        screening_signal = dataset_screening_signal(dataset)
        permanent_review = (
            short_title in PERMANENT_SCREENING_REVIEW_DATASETS
        )
        if permanent_review and not screening_signal:
            screening_signal = "curated:permanent_screening_review"
        diagnosis_labels = parse_dataset_labels(dataset["cancer_types"])
        _, diagnosis_label_reason = eligible_dataset_label(
            dataset["cancer_types"]
        )
        screening_review_candidate = bool(
            permanent_review
            or (
                screening_signal
                and len(diagnosis_labels) == 1
                and diagnosis_label_reason == "eligible"
                and not has_non_cancer_designation(dataset["cancer_types"])
            )
        )
        curated_resolution = CURATED_SCREENING_DIAGNOSIS_RESOLUTIONS.get(
            short_title
        )
        review_required = screening_review_candidate and not curated_resolution
        patient_level_only = bool(
            curated_resolution
            and not curated_resolution.get("allow_dataset_inference", True)
        )
        if review_required:
            review_reason = "screening_single_diagnosis_without_non_cancer"
            review_evidence = ""
        elif curated_resolution:
            review_reason = clean_value(
                curated_resolution.get("review_reason")
            )
            review_evidence = clean_value(
                curated_resolution.get("review_evidence")
            )
        else:
            review_reason = ""
            review_evidence = ""
        results["screening_reviews_required"] += int(review_required)
        results["screening_reviews_resolved"] += int(
            bool(curated_resolution)
        )
        labels: dict[str, tuple[str, str, str]] = {}
        for concept, source_field in field_concepts:
            inferred_value, reason = eligible_dataset_label(dataset[source_field])
            if review_required and reason == "eligible":
                reason = "screening_review_required"
            elif patient_level_only and reason == "eligible":
                reason = "screening_patient_level_only"
            labels[concept] = (inferred_value, reason, source_field)
            results["eligible_labels"] += int(reason == "eligible")

        eligible = {
            concept: values
            for concept, values in labels.items()
            if values[1] == "eligible"
        }
        source_id = (
            f"wordpress-dataset:{normalize_name(short_title)}" if eligible else ""
        )
        if eligible:
            signature = stable_id(
                short_title,
                dataset["date_updated"],
                dataset["cancer_types"],
                dataset["cancer_locations"],
            )
            insert_source(
                conn,
                source_id=source_id,
                source_kind="wordpress_dataset_inference",
                short_title=short_title,
                source_signature_value=signature,
                source_lineage=f"wordpress-dataset:{normalize_name(short_title)}",
                source_url=clean_value(dataset["link"]),
                source_date=clean_value(dataset["date_updated"]),
                provenance={
                    "dataset_type": "Collection",
                    "dataset_title": clean_value(dataset["title"]),
                    "evidence_scope": "dataset",
                    "inference_method": "single_dataset_label",
                    "is_patient_observed": False,
                    "screening_review_resolution": (
                        review_reason if curated_resolution else None
                    ),
                    "screening_review_evidence": review_evidence or None,
                },
            )

        applied_by_concept = {concept: 0 for concept, _ in field_concepts}
        suppressed_by_concept = {concept: 0 for concept, _ in field_concepts}
        existing = {
            (row["subject_key"], row["concept"])
            for row in conn.execute(
                """SELECT DISTINCT subject_key, concept
                   FROM clinical_facts
                   WHERE short_title = ?
                     AND concept IN ('primary_diagnosis', 'primary_site')""",
                (short_title,),
            )
        }
        if review_required:
            patient_diagnosis_subjects = len(
                {
                    subject_key
                    for subject_key, concept in existing
                    if concept == "primary_diagnosis"
                    and subject_key in candidate_keys
                }
            )
            warning(
                conn,
                "screening_dataset_review_required",
                (
                    f"{screening_signal}; cancer_types="
                    f"{clean_value(dataset['cancer_types'])!r}; "
                    f"{candidates} imaging subjects, "
                    f"{patient_diagnosis_subjects} with patient-level diagnosis. "
                    "Collection-level diagnosis and site inference were suppressed "
                    "pending review."
                ),
                short_title=short_title,
            )
        for row_number, subject in enumerate(subject_rows, start=1):
            facts = []
            source_fields: dict[str, str] = {}
            for concept, (value, reason, source_field) in labels.items():
                if (subject["subject_key"], concept) in existing:
                    continue
                if reason != "eligible":
                    if reason in {
                        "screening_review_required",
                        "screening_patient_level_only",
                    }:
                        suppressed_by_concept[concept] += 1
                    continue
                if concept == "primary_site":
                    has_diagnosis_context = (
                        (subject["subject_key"], "primary_diagnosis") in existing
                        or labels["primary_diagnosis"][1] == "eligible"
                    )
                    if not has_diagnosis_context:
                        suppressed_by_concept[concept] += 1
                        continue
                if not value:
                    continue
                facts.append((concept, value, source_field, None))
                source_fields[concept] = source_field
                applied_by_concept[concept] += 1
            if not facts:
                continue
            insert_row_and_facts(
                conn,
                source_id=source_id,
                source_kind="wordpress_dataset_inference",
                short_title=short_title,
                subject_id=subject["subject_id"],
                table_name="wordpress.agent_datasets",
                row_number=row_number,
                row={
                    "subject_id": subject["subject_id"],
                    "cancer_types": dataset["cancer_types"],
                    "cancer_locations": dataset["cancer_locations"],
                },
                facts=facts,
                has_imaging=True,
                evidence_scope="dataset",
                is_inferred=True,
                fact_provenance={
                    "inference_method": "single_dataset_label",
                    "is_patient_observed": False,
                    "source_fields": source_fields,
                    "screening_review_resolution": (
                        review_reason if curated_resolution else None
                    ),
                    "screening_review_evidence": review_evidence or None,
                },
            )
            results["subjects_applied"] += 1

        if suppressed_by_concept["primary_site"] and not patient_level_only:
            warning(
                conn,
                "dataset_site_inference_suppressed_without_diagnosis",
                (
                    f"{suppressed_by_concept['primary_site']} imaging subjects "
                    "lacked patient-level or eligible dataset-level diagnosis "
                    "context; Collection-level primary site inference was "
                    "suppressed."
                ),
                short_title=short_title,
            )

        for concept, (value, reason, source_field) in labels.items():
            conn.execute(
                """INSERT OR REPLACE INTO clinical_dataset_inferences
                   (short_title, concept, source_field, raw_value,
                    inferred_value, eligible, eligibility_reason,
                    review_required, review_reason, review_evidence,
                    screening_signal,
                    candidate_subjects, subjects_applied, subjects_suppressed,
                    source_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    short_title,
                    concept,
                    source_field,
                    clean_value(dataset[source_field]),
                    value,
                    int(reason == "eligible"),
                    reason,
                    int(review_required),
                    review_reason,
                    review_evidence,
                    screening_signal,
                    candidates,
                    applied_by_concept[concept],
                    suppressed_by_concept[concept],
                    source_id or None,
                ),
            )
    return results


def copy_source_from_previous(
    conn: sqlite3.Connection, previous_db: Path, source_id: str
) -> bool:
    previous = sqlite3.connect(previous_db)
    previous.row_factory = sqlite3.Row
    tables = {
        row[0]
        for row in previous.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not SOURCE_REUSE_TABLES.issubset(tables):
        previous.close()
        return False
    source = previous.execute(
        "SELECT * FROM clinical_sources WHERE source_id = ?", (source_id,)
    ).fetchone()
    if not source:
        previous.close()
        return False
    insert_source(
        conn,
        source_id=source["source_id"],
        source_kind=source["source_kind"],
        short_title=source["short_title"],
        source_signature_value=source["source_signature"],
        source_lineage=(
            source["source_lineage"]
            if "source_lineage" in source.keys()
            else f"{source['source_kind']}:{normalize_name(source['short_title'])}"
        ),
        source_url=source["source_url"] or "",
        source_date=source["source_date"] or "",
        artifact_sha256=source["artifact_sha256"] or "",
        artifact_bytes=source["artifact_bytes"],
        provenance=json.loads(source["provenance_json"] or "{}"),
    )
    rows = previous.execute(
        "SELECT * FROM clinical_rows WHERE source_id = ?", (source_id,)
    ).fetchall()
    for row in rows:
        has_imaging = (
            row["has_imaging"]
            if "has_imaging" in row.keys()
            else int(source["source_kind"] == "dicom")
        )
        conn.execute(
            """INSERT OR IGNORE INTO clinical_rows
               (source_row_id, source_id, short_title, subject_id, subject_key,
                table_name, row_number, has_imaging, row_json, row_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["source_row_id"],
                row["source_id"],
                row["short_title"],
                row["subject_id"],
                row["subject_key"],
                row["table_name"],
                row["row_number"],
                has_imaging,
                row["row_json"],
                row["row_sha256"],
            ),
        )
    facts = previous.execute(
        "SELECT * FROM clinical_facts WHERE source_id = ?", (source_id,)
    ).fetchall()
    for fact in facts:
        conn.execute(
            """INSERT OR IGNORE INTO clinical_facts
               (fact_id, source_row_id, source_id, source_kind, source_priority,
                short_title, subject_id, subject_key, concept, value_text,
                value_resolved, value_normalized, value_number, unit,
                original_column, evidence_scope, is_inferred, qc_excluded,
                qc_status, provenance_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact["fact_id"],
                fact["source_row_id"],
                fact["source_id"],
                fact["source_kind"],
                fact["source_priority"],
                fact["short_title"],
                fact["subject_id"],
                fact["subject_key"],
                fact["concept"],
                fact["value_text"],
                (
                    fact["value_resolved"]
                    if "value_resolved" in fact.keys()
                    else fact["value_text"]
                ),
                fact["value_normalized"],
                fact["value_number"],
                fact["unit"],
                fact["original_column"],
                fact["evidence_scope"]
                if "evidence_scope" in fact.keys()
                else "patient",
                fact["is_inferred"] if "is_inferred" in fact.keys() else 0,
                fact["qc_excluded"] if "qc_excluded" in fact.keys() else 0,
                fact["qc_status"] if "qc_status" in fact.keys() else "accepted",
                fact["provenance_json"],
            ),
        )
    warnings = previous.execute(
        """SELECT source_id, short_title, warning_type, warning_text
           FROM clinical_build_warnings WHERE source_id = ?""",
        (source_id,),
    ).fetchall()
    for previous_warning in warnings:
        conn.execute(
            """INSERT INTO clinical_build_warnings
               (source_id, short_title, warning_type, warning_text)
               VALUES (?, ?, ?, ?)""",
            tuple(previous_warning),
        )
    if "clinical_source_dictionary" in tables:
        for dictionary_row in previous.execute(
            "SELECT * FROM clinical_source_dictionary WHERE source_id = ?",
            (source_id,),
        ):
            conn.execute(
                """INSERT OR REPLACE INTO clinical_source_dictionary
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(dictionary_row),
            )
    if "clinical_longitudinal_observations" in tables:
        for observation in previous.execute(
            "SELECT * FROM clinical_longitudinal_observations WHERE source_id = ?",
            (source_id,),
        ):
            conn.execute(
                """INSERT OR REPLACE INTO clinical_longitudinal_observations
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(observation),
            )
    previous.close()
    return True


def copy_nonofficial_previous(
    conn: sqlite3.Connection,
    previous_db: Path,
    allowed_short_titles: set[str],
) -> int:
    previous = sqlite3.connect(previous_db)
    previous.row_factory = sqlite3.Row
    try:
        sources = previous.execute(
            """SELECT source_id, short_title FROM clinical_sources
               WHERE source_kind NOT IN
                     ('tcia_clinical_download',
                      'tcia_linked_external_clinical', 'idc_clinical',
                      'tcia_collection_subject_inheritance',
                      'wordpress_dataset_inference')"""
        ).fetchall()
    except sqlite3.Error:
        previous.close()
        return 0
    previous.close()
    copied = 0
    for source in sources:
        if source["short_title"] not in allowed_short_titles:
            continue
        copied += int(copy_source_from_previous(conn, previous_db, source["source_id"]))
    previous = sqlite3.connect(previous_db)
    previous.row_factory = sqlite3.Row
    available = {
        row[0]
        for row in previous.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "clinical_imaging_subjects" in available:
        for row in previous.execute("SELECT * FROM clinical_imaging_subjects"):
            if row["short_title"] not in allowed_short_titles:
                continue
            conn.execute(
                """INSERT OR REPLACE INTO clinical_imaging_subjects
                   VALUES (?, ?, ?, ?)""",
                tuple(row),
            )
    previous.close()
    return copied


def previous_meta(previous_db: Path, key: str) -> Any:
    try:
        conn = sqlite3.connect(previous_db)
        row = conn.execute(
            "SELECT value FROM clinical_meta WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, json.JSONDecodeError):
        return None


def copy_idc_previous(
    conn: sqlite3.Connection,
    previous_db: Path,
    allowed_short_titles: set[str],
) -> int:
    previous = sqlite3.connect(previous_db)
    previous.row_factory = sqlite3.Row
    tables = {
        row[0]
        for row in previous.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    required = {
        "clinical_idc_tables",
        "clinical_dictionary",
        "clinical_imaging_subjects",
        "clinical_sources",
    }
    if not required.issubset(tables):
        previous.close()
        return 0
    sources = previous.execute(
        """SELECT source_id, short_title FROM clinical_sources
           WHERE source_kind = 'idc_clinical'"""
    ).fetchall()
    copied = 0
    for source in sources:
        if source["short_title"] not in allowed_short_titles:
            continue
        copied += int(copy_source_from_previous(conn, previous_db, source["source_id"]))
    for row in previous.execute("SELECT * FROM clinical_idc_tables"):
        if row["short_title"] not in allowed_short_titles:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO clinical_idc_tables
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(row),
        )
    for row in previous.execute("SELECT * FROM clinical_dictionary"):
        if row["short_title"] not in allowed_short_titles:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO clinical_dictionary
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            tuple(row),
        )
    for row in previous.execute("SELECT * FROM clinical_imaging_subjects"):
        if row["short_title"] not in allowed_short_titles:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO clinical_imaging_subjects
               VALUES (?, ?, ?, ?)""",
            tuple(row),
        )
    previous.close()
    return copied


def frame_records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return [
            {str(key): value for key, value in row.items()}
            for row in frame.to_dict(orient="records")
        ]
    return [
        {str(key): value for key, value in row.items()}
        for _, row in frame.iterrows()
    ]


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "tolist"):
        return json_safe(value.tolist())
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    try:
        if math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def idc_collection_title_map(
    collection_ids: Iterable[str], allowed_short_titles: set[str]
) -> dict[str, str]:
    normalized_titles: dict[str, list[str]] = {}
    for short_title in allowed_short_titles:
        normalized_titles.setdefault(normalize_name(short_title), []).append(short_title)
    result: dict[str, str] = {}
    for collection_id in collection_ids:
        matches = normalized_titles.get(normalize_name(collection_id), [])
        if len(matches) == 1:
            result[str(collection_id)] = matches[0]
    return result


def idc_value_mapping(value: Any, column_label: Any = "") -> dict[str, str]:
    result: dict[str, str] = {}
    for item in json_safe(value) or []:
        if not isinstance(item, dict):
            continue
        code = clean_value(item.get("option_code"))
        description = clean_value(item.get("option_description"))
        if code and description:
            result[code] = description
            result.setdefault(normalize_dictionary_code(code), description)
    label = clean_value(column_label)
    if normalize_name(label).startswith(("sex", "gender")):
        for code, description in re.findall(
            r"([+-]?\d+(?:\.0+)?)\s*=\s*(Male|Female|Other|Unknown)\b",
            label,
            flags=re.IGNORECASE,
        ):
            display = description.title()
            result.setdefault(code, display)
            result.setdefault(normalize_dictionary_code(code), display)
    return result


def is_idc_missing_value(value: str) -> bool:
    return normalize_value(value) in {
        ".m",
        ".n",
        "missing",
        "not applicable",
        "not available",
    }


def get_idc_version(client: Any) -> str:
    idc_version = str(client.get_idc_version() or "").strip()
    data_dir = clean_value(getattr(client, "indices_data_dir", ""))
    release_version = Path(data_dir).name if data_dir else ""
    if release_version and release_version != idc_version:
        return f"{idc_version}@{release_version}"
    return idc_version


def parse_victre_location_archives(
    artifacts: dict[str, bytes],
) -> dict[str, dict[str, Any]]:
    """Parse FDA signal-present lesion locations keyed by TCIA PatientID."""
    result: dict[str, dict[str, Any]] = {}
    member_pattern = re.compile(
        r"^(dense|fatty|hetero|scattered)/SP/"
        r"pcl_(-?\d+)_crop\.loc$"
    )
    for expected_density, data in sorted(artifacts.items()):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                match = member_pattern.fullmatch(member.name.lstrip("./"))
                if not match:
                    raise RuntimeError(
                        f"unexpected VICTRE location member: {member.name}"
                    )
                density, signed_seed = match.groups()
                if density != expected_density:
                    raise RuntimeError(
                        f"VICTRE archive density mismatch: {member.name}"
                    )
                patient_id = str(abs(int(signed_seed)))
                if patient_id in result:
                    raise RuntimeError(
                        f"duplicate VICTRE location PatientID: {patient_id}"
                    )
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise RuntimeError(
                        f"could not read VICTRE location member: {member.name}"
                    )
                counts = {"0": 0, "1": 0}
                for line_number, raw_line in enumerate(
                    extracted.read().decode("utf-8").splitlines(), start=1
                ):
                    values = raw_line.split()
                    if not values:
                        continue
                    if len(values) != 4 or values[3] not in counts:
                        raise RuntimeError(
                            "unexpected VICTRE lesion location row "
                            f"{member.name}:{line_number}"
                        )
                    counts[values[3]] += 1
                result[patient_id] = {
                    "density": density,
                    "location_member": member.name,
                    "microcalcification_count": counts["0"],
                    "spiculated_mass_count": counts["1"],
                }
    return result


def victre_index_subjects(
    index_rows: Iterable[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Read VICTRE signal and density ground truth from DICOM series metadata."""
    pattern = re.compile(
        r"^mcgpu_image_(pc|pcl)_(-?\d+)_crop\.raw\.gz-"
        r"(dense|fatty|hetero|scattered)_0000$",
        re.IGNORECASE,
    )
    all_subjects: set[str] = set()
    subjects: dict[str, dict[str, Any]] = {}
    for row in index_rows:
        if normalize_name(row.get("collection_id")) != "victre":
            continue
        patient_id = clean_value(row.get("PatientID"))
        if not patient_id:
            continue
        all_subjects.add(patient_id)
        if clean_value(row.get("StudyDescription")) != (
            "Simulated Digital Mammography"
        ):
            continue
        series_description = clean_value(row.get("SeriesDescription"))
        match = pattern.fullmatch(series_description)
        if not match:
            raise RuntimeError(
                "unexpected VICTRE digital-mammography SeriesDescription: "
                f"{series_description!r}"
            )
        signal_code, signed_seed, density = match.groups()
        filename_patient_id = str(abs(int(signed_seed)))
        if filename_patient_id != str(abs(int(patient_id))):
            raise RuntimeError(
                "VICTRE PatientID/filename seed mismatch: "
                f"{patient_id!r} vs {signed_seed!r}"
            )
        parsed = {
            "patient_id": patient_id,
            "patient_sex": clean_value(row.get("PatientSex")),
            "series_description": series_description,
            "signal_code": signal_code.lower(),
            "density": density.lower(),
        }
        previous = subjects.get(patient_id)
        if previous and previous != parsed:
            raise RuntimeError(
                f"conflicting VICTRE metadata for PatientID {patient_id}"
            )
        subjects[patient_id] = parsed
    missing = sorted(all_subjects - set(subjects))
    if missing:
        raise RuntimeError(
            f"{len(missing)} VICTRE subjects lack a parseable DM series"
        )
    return subjects, all_subjects


def ingest_victre_external_clinical(
    conn: sqlite3.Connection,
    *,
    index_rows: Iterable[dict[str, Any]],
    idc_version: str,
    timeout: int = 60,
    max_bytes: int = 50_000_000,
    fetcher: Any | None = None,
) -> dict[str, Any]:
    """Join IDC DICOM identifiers to FDA VICTRE lesion ground truth."""
    result: dict[str, Any] = {
        "status": "not_applicable",
        "subjects": 0,
        "lesion_present_subjects": 0,
        "lesion_absent_subjects": 0,
        "cancer_subjects": 0,
        "non_cancer_subjects": 0,
        "unresolved_subjects": 0,
        "location_subjects": 0,
        "artifact_bytes": 0,
        "density_subject_counts": {},
    }
    rows = list(index_rows)
    if not any(
        normalize_name(row.get("collection_id")) == "victre" for row in rows
    ):
        return result
    source_id = "tcia-linked-external:victre:didsr-locations"
    fetcher = fetcher or fetch_url
    try:
        subjects, all_subjects = victre_index_subjects(rows)
        readme_data = fetcher(
            VICTRE_LOCATION_README_URL,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        archive_data = {
            density: fetcher(url, timeout=timeout, max_bytes=max_bytes)
            for density, url in VICTRE_LOCATION_ARCHIVE_URLS.items()
        }
        locations = parse_victre_location_archives(archive_data)
        positive_ids = {
            patient_id
            for patient_id, values in subjects.items()
            if values["signal_code"] == "pcl"
        }
        if positive_ids != set(locations):
            raise RuntimeError(
                "VICTRE signal-present/location identity mismatch "
                f"(DICOM={len(positive_ids)}, locations={len(locations)}, "
                f"overlap={len(positive_ids.intersection(locations))})"
            )
        for patient_id in positive_ids:
            if subjects[patient_id]["density"] != locations[patient_id]["density"]:
                raise RuntimeError(
                    f"VICTRE density mismatch for PatientID {patient_id}"
                )
        if any(
            values["patient_sex"] not in {"", "F"}
            for values in subjects.values()
        ):
            raise RuntimeError("VICTRE DICOM PatientSex was not uniformly F/blank")
    except Exception as exc:
        result["status"] = "failed"
        warning(
            conn,
            "victre_external_clinical_failed",
            str(exc),
            source_id=source_id,
            short_title="VICTRE",
        )
        return result

    digest = hashlib.sha256()
    digest.update(idc_version.encode("utf-8"))
    digest.update(VICTRE_LOCATION_README_URL.encode("utf-8"))
    digest.update(readme_data)
    for density, data in sorted(archive_data.items()):
        digest.update(VICTRE_LOCATION_ARCHIVE_URLS[density].encode("utf-8"))
        digest.update(data)
    artifact_bytes = len(readme_data) + sum(map(len, archive_data.values()))
    insert_source(
        conn,
        source_id=source_id,
        source_kind="tcia_linked_external_clinical",
        short_title="VICTRE",
        source_signature_value=digest.hexdigest(),
        source_lineage="tcia-dicom+fda-victre:patient-ground-truth",
        source_url=VICTRE_REPOSITORY_URL,
        source_date=idc_version,
        artifact_sha256=digest.hexdigest(),
        artifact_bytes=artifact_bytes,
        provenance={
            "provider": "FDA DIDSR",
            "idc_version": idc_version,
            "location_readme_url": VICTRE_LOCATION_README_URL,
            "location_archive_urls": VICTRE_LOCATION_ARCHIVE_URLS,
            "identity_derivation": (
                "Absolute phantom seed in the FDA pcl location filename is "
                "matched to TCIA DICOM PatientID. TCIA DM SeriesDescription "
                "encodes pc (signal absent) or pcl (lesion present) and the "
                "breast-density class."
            ),
            "diagnosis_derivation": (
                "FDA describes lesion insertion as creating cancer cases. "
                "Breast Cancer/Non-Cancer are therefore patient-level "
                "inferences from validated lesion-present/absent ground truth."
            ),
        },
    )

    density_labels = {
        "fatty": "Almost entirely fatty",
        "scattered": "Scattered fibroglandular density",
        "hetero": "Heterogeneously dense",
        "dense": "Extremely dense",
    }
    density_counts: dict[str, int] = {}
    unresolved_ids: list[str] = []
    cancer_subjects = 0
    non_cancer_subjects = 0
    for row_number, patient_id in enumerate(sorted(all_subjects), start=1):
        values = subjects[patient_id]
        density = density_labels[values["density"]]
        density_counts[density] = density_counts.get(density, 0) + 1
        location = locations.get(patient_id, {})
        lesion_present = values["signal_code"] == "pcl"
        direct_facts = [
            ("subject_type", "Synthetic breast phantom", "VICTRE", None),
            ("sex_at_birth", values["patient_sex"], "PatientSex", None),
            ("breast_density", density, "SeriesDescription", None),
            (
                "lesion_status",
                "Present" if lesion_present else "Absent",
                "SeriesDescription",
                None,
            ),
        ]
        if lesion_present:
            direct_facts.extend(
                [
                    (
                        "microcalcification_count",
                        str(location["microcalcification_count"]),
                        "lesionFlag=0",
                        "lesions",
                    ),
                    (
                        "spiculated_mass_count",
                        str(location["spiculated_mass_count"]),
                        "lesionFlag=1",
                        "lesions",
                    ),
                ]
            )
        row_payload = {
            **values,
            **location,
            "lesion_status": "Present" if lesion_present else "Absent",
        }
        insert_row_and_facts(
            conn,
            source_id=source_id,
            source_kind="tcia_linked_external_clinical",
            short_title="VICTRE",
            subject_id=patient_id,
            table_name="idc.index+VICTRE.Locations",
            row_number=row_number,
            row=row_payload,
            facts=direct_facts,
            has_imaging=True,
            fact_provenance={"is_synthetic": True},
        )

        derived_facts = [
            (
                "screening_result",
                "Positive" if lesion_present else "Negative",
                "lesion_status",
                None,
            )
        ]
        if not lesion_present:
            derived_facts.append(
                ("primary_diagnosis", "Non-Cancer", "lesion_status", None)
            )
            non_cancer_subjects += 1
        elif (
            location["microcalcification_count"]
            or location["spiculated_mass_count"]
        ):
            derived_facts.extend(
                [
                    (
                        "primary_diagnosis",
                        "Breast Cancer",
                        "lesion_status",
                        None,
                    ),
                    ("primary_site", "Breast", "lesion_status", None),
                ]
            )
            cancer_subjects += 1
        else:
            unresolved_ids.append(patient_id)
        insert_row_and_facts(
            conn,
            source_id=source_id,
            source_kind="tcia_linked_external_clinical",
            short_title="VICTRE",
            subject_id=patient_id,
            table_name="VICTRE.patient_level_derivation",
            row_number=row_number,
            row=row_payload,
            facts=derived_facts,
            has_imaging=True,
            evidence_scope="patient",
            is_inferred=True,
            fact_provenance={
                "is_synthetic": True,
                "inference_method": "validated_victre_lesion_ground_truth",
            },
        )

    if unresolved_ids:
        warning(
            conn,
            "victre_signal_location_conflict",
            (
                f"{len(unresolved_ids)} VICTRE subject(s) have a pcl "
                "signal-present filename but an empty FDA lesion-location "
                "file. Lesion status is retained, but cancer diagnosis is "
                f"withheld: {', '.join(unresolved_ids)}"
            ),
            source_id=source_id,
            short_title="VICTRE",
        )
    result.update(
        {
            "status": "loaded",
            "subjects": len(all_subjects),
            "lesion_present_subjects": len(locations),
            "lesion_absent_subjects": len(all_subjects) - len(locations),
            "cancer_subjects": cancer_subjects,
            "non_cancer_subjects": non_cancer_subjects,
            "unresolved_subjects": len(unresolved_ids),
            "unresolved_subject_ids": unresolved_ids,
            "location_subjects": len(locations),
            "artifact_bytes": artifact_bytes,
            "density_subject_counts": density_counts,
        }
    )
    return result


def ingest_idc_clinical(
    conn: sqlite3.Connection,
    *,
    allowed_short_titles: set[str],
    previous_db: Path | None,
    refresh: bool,
    no_fetch: bool,
    client: Any | None = None,
    timeout: int = 60,
    max_bytes: int = 50_000_000,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "idc_version": "",
        "tables": 0,
        "rows": 0,
        "subjects": 0,
        "subjects_with_imaging": 0,
        "sources_reused": 0,
        "unmatched_collections": [],
        "unmatched_imaging_collections": [],
        "victre": {"status": "disabled" if no_fetch else "pending"},
        "status": "disabled" if no_fetch else "pending",
    }
    if no_fetch:
        previous_version = (
            previous_meta(previous_db, "idc_version")
            if previous_db and previous_db.exists()
            else None
        )
        can_reuse_offline = bool(
            previous_db
            and previous_db.exists()
            and previous_meta(previous_db, "schema_version") == SCHEMA_VERSION
            and previous_version
            and previous_meta(previous_db, "visible_tcia_short_title_count")
            == len(allowed_short_titles)
        )
        if can_reuse_offline:
            reused = copy_idc_previous(
                conn, previous_db, allowed_short_titles  # type: ignore[arg-type]
            )
            if reused:
                previous_idc_result = previous_meta(
                    previous_db, "idc_clinical_result"  # type: ignore[arg-type]
                )
                previous_victre = (
                    previous_idc_result.get("victre", {})
                    if isinstance(previous_idc_result, dict)
                    else {}
                )
                victre_reused = 0
                if "VICTRE" in allowed_short_titles:
                    previous = sqlite3.connect(previous_db)  # type: ignore[arg-type]
                    victre_sources = previous.execute(
                        """SELECT source_id FROM clinical_sources
                           WHERE source_kind = 'tcia_linked_external_clinical'
                             AND short_title = 'VICTRE'"""
                    ).fetchall()
                    previous.close()
                    for source in victre_sources:
                        victre_reused += int(
                            copy_source_from_previous(
                                conn, previous_db, source[0]  # type: ignore[arg-type]
                            )
                        )
                if previous_victre and victre_reused:
                    previous_victre = dict(previous_victre)
                    previous_victre["reuse_status"] = "reused_offline"
                result.update(
                    {
                        "idc_version": previous_version,
                        "sources_reused": reused,
                        "tables": conn.execute(
                            "SELECT COUNT(*) FROM clinical_idc_tables"
                        ).fetchone()[0],
                        "rows": conn.execute(
                            "SELECT COALESCE(SUM(row_count), 0) FROM clinical_idc_tables"
                        ).fetchone()[0],
                        "subjects": conn.execute(
                            """SELECT COUNT(DISTINCT subject_key)
                               FROM clinical_rows r
                               JOIN clinical_sources s USING (source_id)
                               WHERE s.source_kind = 'idc_clinical'"""
                        ).fetchone()[0],
                        "subjects_with_imaging": conn.execute(
                            """SELECT COUNT(DISTINCT subject_key)
                               FROM clinical_rows r
                               JOIN clinical_sources s USING (source_id)
                               WHERE s.source_kind = 'idc_clinical'
                                 AND r.has_imaging = 1"""
                        ).fetchone()[0],
                        "status": "reused_offline",
                        "victre": previous_victre
                        if victre_reused
                        else {"status": "not_applicable"},
                    }
                )
        return result
    if client is None:
        try:
            from idc_index import IDCClient
        except ImportError as exc:
            raise RuntimeError(
                "IDC clinical ingestion requires idc-index; install the "
                "maintainer dependencies or pass --no-fetch-idc-clinical "
                "only for isolated tests"
            ) from exc
        client = IDCClient()
    idc_version = get_idc_version(client)
    result["idc_version"] = idc_version
    if (
        previous_db
        and previous_db.exists()
        and not refresh
        and previous_meta(previous_db, "schema_version") == SCHEMA_VERSION
        and previous_meta(previous_db, "idc_version") == idc_version
        and (
            previous_meta(previous_db, "visible_tcia_short_title_count")
            == len(allowed_short_titles)
        )
    ):
        reused = copy_idc_previous(conn, previous_db, allowed_short_titles)
        if reused:
            result["sources_reused"] = reused
            result["tables"] = conn.execute(
                "SELECT COUNT(*) FROM clinical_idc_tables"
            ).fetchone()[0]
            result["rows"] = conn.execute(
                "SELECT COALESCE(SUM(row_count), 0) FROM clinical_idc_tables"
            ).fetchone()[0]
            result["subjects"] = conn.execute(
                """SELECT COUNT(DISTINCT subject_key) FROM clinical_rows r
                   JOIN clinical_sources s USING (source_id)
                   WHERE s.source_kind = 'idc_clinical'"""
            ).fetchone()[0]
            result["subjects_with_imaging"] = conn.execute(
                """SELECT COUNT(DISTINCT subject_key) FROM clinical_rows r
                   JOIN clinical_sources s USING (source_id)
                   WHERE s.source_kind = 'idc_clinical'
                     AND r.has_imaging = 1"""
            ).fetchone()[0]
            index_rows = frame_records(client.index)
            if "VICTRE" in allowed_short_titles:
                result["victre"] = ingest_victre_external_clinical(
                    conn,
                    index_rows=index_rows,
                    idc_version=idc_version,
                    timeout=timeout,
                    max_bytes=max_bytes,
                )
            else:
                result["victre"] = {"status": "not_applicable"}
            result["status"] = "reused"
            return result

    client.fetch_index("clinical_index")
    dictionary_rows = frame_records(client.clinical_index)
    collection_ids = sorted(
        {
            clean_value(row.get("collection_id"))
            for row in dictionary_rows
            if clean_value(row.get("collection_id"))
        }
    )
    title_map = idc_collection_title_map(collection_ids, allowed_short_titles)
    result["unmatched_collections"] = sorted(set(collection_ids) - set(title_map))

    index_rows = frame_records(client.index)
    imaging_collection_ids = sorted(
        {
            clean_value(row.get("collection_id"))
            for row in index_rows
            if clean_value(row.get("collection_id"))
        }
    )
    imaging_title_map = idc_collection_title_map(
        imaging_collection_ids, allowed_short_titles
    )
    result["unmatched_imaging_collections"] = sorted(
        set(imaging_collection_ids) - set(imaging_title_map)
    )
    imaging_by_collection: dict[str, set[str]] = {}
    for row in index_rows:
        collection_id = clean_value(row.get("collection_id"))
        patient_id = clean_value(row.get("PatientID"))
        if collection_id in imaging_title_map and patient_id:
            imaging_by_collection.setdefault(collection_id, set()).add(
                normalize_subject(patient_id)
            )
    for collection_id, patient_ids in imaging_by_collection.items():
        short_title = imaging_title_map[collection_id]
        for patient_id in patient_ids:
            conn.execute(
                """INSERT OR REPLACE INTO clinical_imaging_subjects
                   (subject_key, short_title, subject_id, imaging_source)
                   VALUES (?, ?, ?, 'idc_index')""",
                (
                    f"{normalize_name(short_title)}:{patient_id}",
                    short_title,
                    patient_id,
                ),
            )

    if "VICTRE" in allowed_short_titles:
        result["victre"] = ingest_victre_external_clinical(
            conn,
            index_rows=index_rows,
            idc_version=idc_version,
            timeout=timeout,
            max_bytes=max_bytes,
        )
    else:
        result["victre"] = {"status": "not_applicable"}

    dictionary_by_table: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in dictionary_rows:
        collection_id = clean_value(row.get("collection_id"))
        table_name = clean_value(
            row.get("short_table_name") or row.get("table_name")
        )
        short_title = title_map.get(collection_id)
        if not short_title or not table_name:
            continue
        safe_values = json_safe(row.get("values")) or []
        conn.execute(
            """INSERT OR REPLACE INTO clinical_dictionary
               (collection_id, short_title, table_name, column_name,
                column_label, values_json, idc_version)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                collection_id,
                short_title,
                table_name,
                clean_value(row.get("column")),
                clean_value(row.get("column_label")),
                json_dumps(safe_values),
                idc_version,
            ),
        )
        dictionary_by_table.setdefault((collection_id, table_name), []).append(row)

    all_subjects: set[str] = set()
    imaging_subjects: set[str] = set()
    table_collection_counts: dict[str, int] = {}
    for _, table_name in dictionary_by_table:
        table_collection_counts[table_name] = (
            table_collection_counts.get(table_name, 0) + 1
        )
    for (collection_id, table_name), columns in sorted(dictionary_by_table.items()):
        short_title = title_map[collection_id]
        source_id = f"idc-clinical:{normalize_name(collection_id)}:{table_name}"
        lineage = f"tcia-official-clinical:{normalize_name(short_title)}"
        column_signature = stable_id(
            json_dumps(
                [
                    {
                        "column": clean_value(column.get("column")),
                        "label": clean_value(column.get("column_label")),
                        "values": json_safe(column.get("values")) or [],
                    }
                    for column in columns
                ]
            )
        )
        insert_source(
            conn,
            source_id=source_id,
            source_kind="idc_clinical",
            source_lineage=lineage,
            short_title=short_title,
            source_signature_value=stable_id(idc_version, column_signature),
            source_url="https://github.com/ImagingDataCommons/idc-index-data",
            source_date=idc_version,
            provenance={
                "idc_version": idc_version,
                "collection_id": collection_id,
                "table_name": table_name,
                "lineage_note": (
                    "IDC-normalized delivery of official collection clinical data"
                ),
            },
        )
        status = "loaded"
        error_text = ""
        row_count = 0
        table_subjects: set[str] = set()
        table_imaging_subjects: set[str] = set()
        artifact_digest = hashlib.sha256()
        artifact_bytes = 0
        try:
            frame = client.get_clinical_table(table_name)
            records = frame_records(frame)
            labels = {
                clean_value(column.get("column")): clean_value(
                    column.get("column_label")
                )
                for column in columns
            }
            mappings = {
                clean_value(column.get("column")): idc_value_mapping(
                    column.get("values"), column.get("column_label")
                )
                for column in columns
            }
            for index, record in enumerate(records, start=2):
                subject_id = clean_value(record.get("dicom_patient_id"))
                if not subject_id:
                    continue
                normalized_subject = normalize_subject(subject_id)
                has_imaging = normalized_subject in imaging_by_collection.get(
                    collection_id, set()
                )
                # Some analysis/QA tables accompany several source
                # collections. get_clinical_table() returns the shared table,
                # so partition it by the collection-specific imaging IDs
                # instead of copying every row into every collection.
                if table_collection_counts.get(table_name, 0) > 1 and not has_imaging:
                    continue
                record_bytes = (
                    json_dumps(
                        {
                            str(key): clean_value(value)
                            for key, value in record.items()
                        }
                    ).encode("utf-8")
                    + b"\n"
                )
                artifact_digest.update(record_bytes)
                artifact_bytes += len(record_bytes)
                facts: list[tuple[str, str, str, str | None]] = []
                for column, raw_value in record.items():
                    raw_text = clean_value(raw_value)
                    if not raw_text or is_idc_missing_value(raw_text):
                        continue
                    concept = concept_for_source_column(
                        short_title, column, labels.get(column, "")
                    )
                    if not concept:
                        continue
                    mapping = mappings.get(column, {})
                    decoded = mapping.get(
                        raw_text,
                        mapping.get(normalize_dictionary_code(raw_text), raw_text),
                    )
                    decoded = decode_dataset_concept(
                        short_title, concept, decoded
                    )
                    if is_idc_missing_value(decoded):
                        continue
                    facts.append((concept, decoded, column, None))
                if insert_row_and_facts(
                    conn,
                    source_id=source_id,
                    source_kind="idc_clinical",
                    short_title=short_title,
                    subject_id=subject_id,
                    table_name=table_name,
                    row_number=index,
                    row=record,
                    facts=facts,
                    has_imaging=has_imaging,
                ):
                    row_count += 1
                    subject_key = (
                        f"{normalize_name(short_title)}:{normalized_subject}"
                    )
                    table_subjects.add(subject_key)
                    all_subjects.add(subject_key)
                    if has_imaging:
                        table_imaging_subjects.add(subject_key)
                        imaging_subjects.add(subject_key)
        except Exception as exc:
            status = "failed"
            error_text = str(exc)
            warning(
                conn,
                "idc_clinical_table_failed",
                error_text,
                source_id=source_id,
                short_title=short_title,
            )
        conn.execute(
            """UPDATE clinical_sources
               SET artifact_sha256 = ?, artifact_bytes = ?
               WHERE source_id = ?""",
            (
                artifact_digest.hexdigest() if row_count else "",
                artifact_bytes,
                source_id,
            ),
        )
        conn.execute(
            """INSERT OR REPLACE INTO clinical_idc_tables
               (collection_id, short_title, table_name, idc_version, source_id,
                column_count, row_count, subject_count,
                subjects_with_imaging, ingest_status, error_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                collection_id,
                short_title,
                table_name,
                idc_version,
                source_id,
                len(columns),
                row_count,
                len(table_subjects),
                len(table_imaging_subjects),
                status,
                error_text,
            ),
        )
        result["tables"] += 1
        result["rows"] += row_count
        conn.commit()
    result["subjects"] = len(all_subjects)
    result["subjects_with_imaging"] = len(imaging_subjects)
    result["status"] = "loaded"
    return result


def fetch_url(url: str, *, timeout: int, max_bytes: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        length = response.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise RuntimeError(f"artifact is {length} bytes; limit is {max_bytes}")
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError(f"artifact exceeds {max_bytes} byte limit")
    return data


def read_delimited(data: bytes, suffix: str) -> Any:
    delimiter = "\t" if suffix in {".tsv", ".tab"} else None
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = data.decode(encoding)
            sample = text[:65536]
            if delimiter == "\t":
                dialect = csv.excel_tab
            else:
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            columns = [str(column) for column in (reader.fieldnames or []) if column]
            rows = [
                {str(key): value for key, value in row.items() if key is not None}
                for row in reader
            ]
            return SimpleFrame(columns, rows)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"could not parse delimited table: {last_error}")


def read_tables(data: bytes, name: str) -> list[tuple[str, Any]]:
    suffix = Path(name.split("?", 1)[0]).suffix.lower()
    # XLSX files are ZIP containers too, so magic-byte detection alone would
    # incorrectly treat their internal XML parts as a clinical package.
    if suffix == ".zip":
        tables: list[tuple[str, Any]] = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.namelist():
                if member.endswith("/"):
                    continue
                member_path = Path(member)
                if "__MACOSX" in member_path.parts or member_path.name.startswith(
                    "._"
                ):
                    continue
                member_suffix = Path(member).suffix.lower()
                if member_suffix not in {".csv", ".tsv", ".tab", ".xls", ".xlsx"}:
                    continue
                member_data = archive.read(member)
                tables.extend(read_tables(member_data, member))
        return tables
    if suffix in {".csv", ".tsv", ".tab", ".txt"}:
        return [(name, read_delimited(data, suffix))]
    if suffix in {".xls", ".xlsx"}:
        import pandas as pd

        engine = "xlrd" if suffix == ".xls" else "openpyxl"
        sheets = pd.read_excel(
            io.BytesIO(data), sheet_name=None, dtype=object, engine=engine
        )
        return [(f"{name}::{sheet}", frame) for sheet, frame in sheets.items()]
    return []


def ct_colonography_workbook_facts(
    short_title: str,
    download_title: str,
    record: dict[Any, Any],
) -> list[tuple[str, str, str, str | None]]:
    if normalize_name(short_title) != "ctcolonography":
        return []
    facts: list[tuple[str, str, str, str | None]] = []
    if "nopolyp" in normalize_name(download_title):
        facts.append(
            (
                "screening_result",
                "No polyp found",
                "download_title",
                None,
            )
        )
    for column, raw_value in record.items():
        if not str(column).strip().endswith(".5"):
            continue
        numeric = number_value(clean_value(raw_value))
        if numeric is None or not numeric.is_integer():
            continue
        code = int(numeric)
        label = CT_COLONOGRAPHY_HISTOLOGY.get(code)
        if not label:
            continue
        facts.extend(
            [
                ("lesion_histology", label, str(column), None),
                ("lesion_histology_code", str(code), str(column), None),
            ]
        )
    return facts


def ea1141_workbook_facts(
    short_title: str,
    record: dict[Any, Any],
) -> list[tuple[str, str, str, str | None]]:
    """Decode selected EA1141 fields using its official PDF dictionaries."""
    if normalize_name(short_title) != "ea1141":
        return []
    values = {
        normalize_name(column): (str(column), clean_value(raw_value))
        for column, raw_value in record.items()
    }
    facts: list[tuple[str, str, str, str | None]] = []

    def add(concept: str, column: str, decoded: str) -> None:
        if decoded:
            facts.append((concept, decoded, column, None))

    if "age" in values:
        column, value = values["age"]
        add("age_at_enrollment_years", column, value)
    if "sex" in values:
        column, value = values["sex"]
        add("sex_at_birth", column, {"1": "Female"}.get(value, value))
    if "race" in values:
        column, value = values["race"]
        add("race", column, EA1141_RACE.get(value, value))
    if "ethnicity" in values:
        column, value = values["ethnicity"]
        add("ethnicity", column, EA1141_ETHNICITY.get(value, value))

    if "year0sensspecrefstd" in values:
        column, value = values["year0sensspecrefstd"]
        screening_result = {
            "1": "Positive",
            "0": "Negative",
            "R": "Withdrawn",
            ".R": "Withdrawn",
            "M": "Missing",
            ".M": "Missing",
        }.get(value)
        add("screening_result", column, screening_result or value)

    for prefix in ("mri", "tomo"):
        outcome_key = f"{prefix}lesionoutcomeyr0"
        detail_key = f"{prefix}lesionoutcomedetailyr0"
        core_grade_key = f"{prefix}corepathgradeyr0"
        surgical_grade_key = f"{prefix}surgpathgradeyr0"
        if outcome_key in values:
            column, value = values[outcome_key]
            add("lesion_outcome", column, value)
        if detail_key in values:
            column, value = values[detail_key]
            add("lesion_outcome_detail", column, value)
        if core_grade_key in values:
            column, value = values[core_grade_key]
            add("tumor_grade_core_code", column, value)
        if surgical_grade_key in values:
            column, value = values[surgical_grade_key]
            add("tumor_grade_surgical_code", column, value)
    return facts


def hnscc_workbook_facts(
    short_title: str,
    record: dict[Any, Any],
) -> list[tuple[str, str, str, str | None]]:
    """Decode HNSCC histology without losing the original workbook row."""
    if normalize_name(short_title) != "hnscc":
        return []
    facts: list[tuple[str, str, str, str | None]] = []
    for column, raw_value in record.items():
        if normalize_name(column) != "histology":
            continue
        value = clean_value(raw_value)
        if not value:
            continue
        decoded = (
            "Head and Neck Squamous Cell Carcinoma"
            if normalize_value(value) == "scc"
            else value
        )
        facts.append(("primary_diagnosis", decoded, str(column), None))
    return facts


def hungarian_colorectal_workbook_facts(
    short_title: str,
    record: dict[Any, Any],
) -> list[tuple[str, str, str, str | None]]:
    """Normalize only the international ICD-10 category of each full code."""
    if normalize_name(short_title) != "hungariancolorectalscreening":
        return []
    facts: list[tuple[str, str, str, str | None]] = []
    for column, raw_value in record.items():
        if normalize_name(column) != "icd10healthstatus":
            continue
        full_code = re.sub(r"\s+", "", clean_value(raw_value)).upper()
        if not full_code:
            continue
        category = re.sub(r"[^A-Z0-9]", "", full_code)[:3]
        facts.extend(
            [
                ("icd10_code", full_code, str(column), None),
                ("icd10_category", category, str(column), None),
            ]
        )
        mapping = HUNGARIAN_COLORECTAL_ICD10.get(category)
        if not mapping:
            facts.append(
                ("screening_result", "Unmapped ICD-10 category", str(column), None)
            )
            continue
        facts.append(
            (
                "screening_result",
                mapping["screening_result"],
                str(column),
                None,
            )
        )
        if mapping["diagnosis"]:
            facts.append(
                (
                    "primary_diagnosis",
                    mapping["diagnosis"],
                    str(column),
                    None,
                )
            )
        if mapping["site"]:
            facts.append(
                ("primary_site", mapping["site"], str(column), None)
            )
    return facts


def ingest_official_bytes(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    source_id: str,
    signature: str,
    data: bytes,
) -> tuple[int, int]:
    insert_source(
        conn,
        source_id=source_id,
        source_kind="tcia_clinical_download",
        source_lineage=(
            f"tcia-official-clinical:{normalize_name(row['short_title'])}"
        ),
        short_title=row["short_title"],
        source_signature_value=signature,
        source_url=row["download_url"] or "",
        source_date=row["date_updated"] or "",
        artifact_sha256=sha256_bytes(data),
        artifact_bytes=len(data),
        provenance={
            "download_id": row["download_id"],
            "download_title": row["download_title"],
            "file_types": row["file_types"],
            "subject_column_overrides": sorted(
                SUBJECT_COLUMN_OVERRIDES.get(
                    normalize_name(row["short_title"]), set()
                )
            ),
            "transform_version": OFFICIAL_SOURCE_TRANSFORM_VERSIONS.get(
                normalize_name(row["short_title"]), 0
            ),
        },
    )
    loaded_rows = 0
    subjects: set[str] = set()
    tables = read_tables(data, row["download_url"] or row["download_title"] or "")
    if not tables:
        raise RuntimeError("no supported CSV/TSV/XLS/XLSX table found")
    for table_name, frame in tables:
        if frame is None or frame.empty:
            continue
        frame = prepare_official_table(row["short_title"], table_name, frame)
        dictionary_rows = ingest_official_source_dictionary(
            conn,
            source_id=source_id,
            short_title=row["short_title"],
            table_name=table_name,
            frame=frame,
        )
        if dictionary_rows:
            continue
        subject_column = choose_subject_column(
            frame.columns, row["short_title"]
        )
        if subject_column is None:
            warning(
                conn,
                "subject_column_not_found",
                (
                    f"No conservative subject identifier column found in "
                    f"{table_name}; columns={list(map(str, frame.columns))[:30]}"
                ),
                source_id=source_id,
                short_title=row["short_title"],
            )
            continue
        for index, record in frame.iterrows():
            values = {str(column): record[column] for column in frame.columns}
            source_subject_id = clean_value(record[subject_column])
            if not source_subject_id:
                continue
            subject_id, subject_id_mapping_method = official_subject_id_mapping(
                row["short_title"], source_subject_id
            )
            facts: list[tuple[str, str, str, str | None]] = []
            for column in frame.columns:
                if (
                    normalize_name(row["short_title"]) == "bcbmradiogenomics"
                    and normalize_name(column) == "age"
                ):
                    # Age is scan-specific in this longitudinal cohort and is
                    # retained in the observation row instead of flattened to
                    # a potentially conflicting patient-level fact.
                    continue
                if (
                    normalize_name(row["short_title"]) == "ea1141"
                    and normalize_name(column) in EA1141_HANDLED_COLUMNS
                ):
                    continue
                if (
                    normalize_name(row["short_title"]) == "hnscc"
                    and normalize_name(column) in HNSCC_HANDLED_COLUMNS
                ):
                    continue
                concept = concept_for_source_column(
                    row["short_title"], column
                )
                value = clean_value(record[column])
                if concept and value:
                    # Preserve official source codes in value_text. Audited
                    # dataset-specific decoding occurs in the QC layer and is
                    # recorded separately in value_resolved.
                    facts.append(
                        (
                            concept,
                            value,
                            str(column),
                            unit_for_source_column(row["short_title"], column),
                        )
                    )
            facts.extend(
                ct_colonography_workbook_facts(
                    row["short_title"],
                    row["download_title"],
                    values,
                )
            )
            facts.extend(ea1141_workbook_facts(row["short_title"], values))
            facts.extend(hnscc_workbook_facts(row["short_title"], values))
            facts.extend(
                hungarian_colorectal_workbook_facts(
                    row["short_title"], values
                )
            )
            subject_key = (
                f"{normalize_name(row['short_title'])}:"
                f"{normalize_subject(subject_id)}"
            )
            has_imaging = bool(
                conn.execute(
                    """SELECT 1 FROM clinical_imaging_subjects
                       WHERE subject_key = ?""",
                    (subject_key,),
                ).fetchone()
            )
            source_row_number = (
                int(index) + 2 if isinstance(index, int) else loaded_rows + 2
            )
            if insert_row_and_facts(
                conn,
                source_id=source_id,
                source_kind="tcia_clinical_download",
                short_title=row["short_title"],
                subject_id=subject_id,
                table_name=table_name,
                row_number=source_row_number,
                row=values,
                facts=facts,
                has_imaging=has_imaging,
                fact_provenance={
                    "source_subject_column": str(subject_column),
                    "source_subject_id": source_subject_id,
                    "subject_id_mapping_method": subject_id_mapping_method,
                },
            ):
                ingest_yale_longitudinal_observation(
                    conn,
                    source_id=source_id,
                    short_title=row["short_title"],
                    subject_id=subject_id,
                    table_name=table_name,
                    row_number=source_row_number,
                    row=values,
                )
                ingest_bcbm_longitudinal_observation(
                    conn,
                    source_id=source_id,
                    short_title=row["short_title"],
                    subject_id=subject_id,
                    table_name=table_name,
                    row_number=source_row_number,
                    row=values,
                )
                ingest_brain_tr_gammaknife_observation(
                    conn,
                    source_id=source_id,
                    short_title=row["short_title"],
                    subject_id=subject_id,
                    table_name=table_name,
                    row_number=source_row_number,
                    row=values,
                )
                loaded_rows += 1
                subjects.add(normalize_subject(subject_id))
    return loaded_rows, len(subjects)


def derive_ct_colonography_patient_diagnoses(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    rows = conn.execute(
        """SELECT subject_key, subject_id, concept, value_text
           FROM clinical_facts
           WHERE short_title = 'CT COLONOGRAPHY'
             AND source_kind = 'tcia_clinical_download'
             AND concept IN (
                 'lesion_histology_code', 'screening_result'
             )
           ORDER BY subject_key, concept, value_text"""
    ).fetchall()
    result = {
        "spreadsheet_subjects": 0,
        "classified_subjects": 0,
        "malignant_subjects": 0,
        "adenoma_subjects": 0,
        "benign_subjects": 0,
        "negative_screening_subjects": 0,
        "indeterminate_subjects": 0,
        "imaging_subjects": conn.execute(
            """SELECT COUNT(*) FROM clinical_imaging_subjects
               WHERE short_title = 'CT COLONOGRAPHY'"""
        ).fetchone()[0],
        "imaging_without_spreadsheet": 0,
    }
    if not rows:
        result["imaging_without_spreadsheet"] = result["imaging_subjects"]
        return result

    subjects: dict[str, dict[str, Any]] = {}
    for row in rows:
        subject = subjects.setdefault(
            row["subject_key"],
            {
                "subject_id": row["subject_id"],
                "histology_codes": set(),
                "no_polyp": False,
            },
        )
        if row["concept"] == "lesion_histology_code":
            numeric = number_value(row["value_text"])
            if numeric is not None and numeric.is_integer():
                subject["histology_codes"].add(int(numeric))
        elif normalize_value(row["value_text"]) == "no polyp found":
            subject["no_polyp"] = True

    result["spreadsheet_subjects"] = len(subjects)
    result["imaging_without_spreadsheet"] = max(
        result["imaging_subjects"] - len(subjects), 0
    )
    source_id = "tcia-derived:ctcolonography:patient-histology"
    source_signature_value = stable_id(
        json_dumps(
            {
                "histology_map": CT_COLONOGRAPHY_HISTOLOGY,
                "subjects": [
                    {
                        "subject_key": key,
                        "codes": sorted(value["histology_codes"]),
                        "no_polyp": value["no_polyp"],
                    }
                    for key, value in sorted(subjects.items())
                ],
            }
        )
    )
    insert_source(
        conn,
        source_id=source_id,
        source_kind="tcia_clinical_download",
        source_lineage="tcia-official-clinical:ctcolonography",
        short_title="CT COLONOGRAPHY",
        source_signature_value=source_signature_value,
        source_url=(
            "https://www.cancerimagingarchive.net/collection/"
            "ct-colonography/"
        ),
        provenance={
            "derivation_method": (
                "patient-level classification from official ACRIN 6664 "
                "lesion histology codes and the no-polyp list"
            ),
            "malignant_codes": list(range(1, 10)),
            "indeterminate_codes": [88, 98],
            "histology_map": CT_COLONOGRAPHY_HISTOLOGY,
        },
    )

    for row_number, (subject_key, subject) in enumerate(
        sorted(subjects.items()), start=1
    ):
        codes = set(subject["histology_codes"])
        malignant = sorted(code for code in codes if 1 <= code <= 9)
        indeterminate = bool(codes.intersection({88, 98}))
        diagnosis = ""
        category = ""
        if malignant:
            diagnosis = CT_COLONOGRAPHY_HISTOLOGY[malignant[0]]
            category = "malignant"
            result["malignant_subjects"] += 1
        elif indeterminate:
            category = "indeterminate"
            result["indeterminate_subjects"] += 1
        else:
            selected = next(
                (
                    code
                    for code in CT_COLONOGRAPHY_NONMALIGNANT_SEVERITY
                    if code in codes
                ),
                None,
            )
            if selected is not None:
                diagnosis = CT_COLONOGRAPHY_HISTOLOGY[selected]
                if 12 <= selected <= 16:
                    category = "adenoma"
                    result["adenoma_subjects"] += 1
                else:
                    category = "benign"
                    result["benign_subjects"] += 1
            elif subject["no_polyp"]:
                diagnosis = "Non-Cancer"
                category = "negative_screening"
                result["negative_screening_subjects"] += 1
            else:
                category = "indeterminate"
                result["indeterminate_subjects"] += 1

        if not diagnosis:
            continue
        facts: list[tuple[str, str, str, str | None]] = [
            (
                "primary_diagnosis",
                diagnosis,
                "derived_patient_histology",
                None,
            )
        ]
        if category != "negative_screening":
            facts.append(
                ("primary_site", "Colon", "derived_patient_histology", None)
            )
        insert_row_and_facts(
            conn,
            source_id=source_id,
            source_kind="tcia_clinical_download",
            short_title="CT COLONOGRAPHY",
            subject_id=subject["subject_id"],
            table_name="derived.ct_colonography_patient_histology",
            row_number=row_number,
            row={
                "subject_id": subject["subject_id"],
                "histology_codes": sorted(codes),
                "no_polyp_found": subject["no_polyp"],
                "classification": category,
            },
            facts=facts,
            has_imaging=bool(
                conn.execute(
                    """SELECT 1 FROM clinical_imaging_subjects
                       WHERE subject_key = ?""",
                    (subject_key,),
                ).fetchone()
            ),
            fact_provenance={
                "derivation_method": (
                    "official_acrin_6664_lesion_histology"
                ),
                "histology_codes": sorted(codes),
                "no_polyp_found": subject["no_polyp"],
                "classification": category,
                "is_patient_observed": True,
            },
        )
        result["classified_subjects"] += 1

    if result["indeterminate_subjects"]:
        warning(
            conn,
            "ct_colonography_histology_indeterminate",
            (
                f"{result['indeterminate_subjects']} spreadsheet subjects "
                "have Other/Not-applicable or otherwise unresolved histology; "
                "no primary diagnosis was derived for them."
            ),
            source_id=source_id,
            short_title="CT COLONOGRAPHY",
        )
    if result["imaging_without_spreadsheet"]:
        warning(
            conn,
            "ct_colonography_screening_coverage_incomplete",
            (
                f"{result['spreadsheet_subjects']} of "
                f"{result['imaging_subjects']} imaging subjects occur in the "
                "three polyp-status spreadsheets; Collection-level diagnosis "
                "inference remains blocked for subjects without patient-level "
                "classification."
            ),
            source_id=source_id,
            short_title="CT COLONOGRAPHY",
        )
    return result


def derive_ea1141_patient_diagnoses(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Resolve EA1141 screening status and malignant lesion pathology."""
    rows = conn.execute(
        """SELECT source_row_id, subject_key, subject_id, concept, value_text
           FROM clinical_facts
           WHERE short_title = 'EA1141'
             AND source_kind = 'tcia_clinical_download'
             AND concept IN (
                 'screening_result', 'lesion_outcome',
                 'lesion_outcome_detail', 'tumor_grade_core_code',
                 'tumor_grade_surgical_code'
             )
           ORDER BY source_row_id, concept, value_text"""
    ).fetchall()
    result = {
        "official_subjects": 0,
        "classified_subjects": 0,
        "positive_subjects": 0,
        "negative_subjects": 0,
        "withdrawn_subjects": 0,
        "missing_subjects": 0,
        "positive_with_pathology": 0,
        "positive_without_pathology": 0,
        "imaging_subjects": conn.execute(
            """SELECT COUNT(*) FROM clinical_imaging_subjects
               WHERE short_title = 'EA1141'"""
        ).fetchone()[0],
    }
    if not rows:
        return result

    source_rows: dict[str, dict[str, Any]] = {}
    subjects: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_row = source_rows.setdefault(
            row["source_row_id"],
            {
                "subject_key": row["subject_key"],
                "subject_id": row["subject_id"],
                "facts": {},
            },
        )
        source_row["facts"].setdefault(row["concept"], []).append(
            row["value_text"]
        )
        subjects.setdefault(
            row["subject_key"],
            {
                "subject_id": row["subject_id"],
                "screening_results": set(),
                "malignant_rows": [],
            },
        )

    for source_row in source_rows.values():
        subject = subjects[source_row["subject_key"]]
        facts = source_row["facts"]
        subject["screening_results"].update(
            facts.get("screening_result", [])
        )
        outcomes = {
            normalize_value(value)
            for value in facts.get("lesion_outcome", [])
        }
        if outcomes.intersection({"invasive", "dcis"}):
            subject["malignant_rows"].append(facts)

    result["official_subjects"] = len(subjects)
    source_id = "tcia-derived:ea1141:screening-pathology"
    source_signature_value = stable_id(
        json_dumps(
            {
                "subjects": [
                    {
                        "subject_key": key,
                        "screening_results": sorted(
                            value["screening_results"]
                        ),
                        "malignant_rows": value["malignant_rows"],
                    }
                    for key, value in sorted(subjects.items())
                ]
            }
        )
    )
    insert_source(
        conn,
        source_id=source_id,
        source_kind="tcia_clinical_download",
        source_lineage="tcia-official-clinical:ea1141",
        short_title="EA1141",
        source_signature_value=source_signature_value,
        source_url=(
            "https://www.cancerimagingarchive.net/wp-content/uploads/"
            "EA1141-Reviewed-Clinical-Data-and-Data-Dictionaries.zip"
        ),
        provenance={
            "derivation_method": (
                "official EA1141 year-0 sensitivity/specificity reference "
                "standard plus malignant lesion outcomes and pathology"
            ),
            "screening_codes": {
                "1": "Positive",
                "0": "Negative",
                "R": "Withdrawn",
                "M": "Missing",
            },
            "grade_codes": EA1141_GRADE,
        },
    )

    for row_number, (subject_key, subject) in enumerate(
        sorted(subjects.items()), start=1
    ):
        screening_results = {
            normalize_value(value) for value in subject["screening_results"]
        }
        if "positive" in screening_results:
            classification = "positive"
            result["positive_subjects"] += 1
        elif "negative" in screening_results:
            classification = "negative"
            result["negative_subjects"] += 1
        elif "withdrawn" in screening_results:
            classification = "withdrawn"
            result["withdrawn_subjects"] += 1
        else:
            classification = "missing"
            result["missing_subjects"] += 1

        facts: list[tuple[str, str, str, str | None]] = []
        diagnosis = ""
        grade = ""
        if classification == "positive":
            details: list[str] = []
            surgical_grades: list[str] = []
            core_grades: list[str] = []
            outcomes: set[str] = set()
            for malignant_row in subject["malignant_rows"]:
                outcomes.update(malignant_row.get("lesion_outcome", []))
                details.extend(
                    value
                    for value in malignant_row.get(
                        "lesion_outcome_detail", []
                    )
                    if normalize_value(value)
                    not in {"", "unknown", ".f", ".m"}
                )
                surgical_grades.extend(
                    malignant_row.get("tumor_grade_surgical_code", [])
                )
                core_grades.extend(
                    malignant_row.get("tumor_grade_core_code", [])
                )
            if details:
                diagnosis = sorted(
                    set(details),
                    key=lambda value: (
                        "invasive" not in normalize_value(value),
                        value,
                    ),
                )[0]
                if normalize_value(diagnosis) == "dcis":
                    diagnosis = "Ductal carcinoma in situ"
            elif any(normalize_value(value) == "dcis" for value in outcomes):
                diagnosis = "Ductal carcinoma in situ"
            elif any(
                normalize_value(value) == "invasive" for value in outcomes
            ):
                diagnosis = "Invasive breast carcinoma"
            valid_surgical = [
                value for value in surgical_grades if value in EA1141_GRADE
            ]
            valid_core = [
                value for value in core_grades if value in EA1141_GRADE
            ]
            selected_grades = valid_surgical or valid_core
            if selected_grades:
                grade = EA1141_GRADE[max(selected_grades, key=int)]
            if diagnosis:
                facts.extend(
                    [
                        (
                            "primary_diagnosis",
                            diagnosis,
                            "derived_year0_pathology",
                            None,
                        ),
                        (
                            "primary_site",
                            "Breast",
                            "derived_year0_pathology",
                            None,
                        ),
                    ]
                )
                if grade:
                    facts.append(
                        (
                            "grade",
                            grade,
                            "derived_year0_pathology",
                            None,
                        )
                    )
                result["positive_with_pathology"] += 1
            else:
                result["positive_without_pathology"] += 1
        elif classification == "negative":
            diagnosis = "Non-Cancer"
            facts.append(
                (
                    "primary_diagnosis",
                    diagnosis,
                    "derived_year0_reference_standard",
                    None,
                )
            )

        if not facts:
            continue
        insert_row_and_facts(
            conn,
            source_id=source_id,
            source_kind="tcia_clinical_download",
            short_title="EA1141",
            subject_id=subject["subject_id"],
            table_name="derived.ea1141_screening_pathology",
            row_number=row_number,
            row={
                "subject_id": subject["subject_id"],
                "screening_results": sorted(subject["screening_results"]),
                "classification": classification,
                "diagnosis": diagnosis,
                "grade": grade,
            },
            facts=facts,
            has_imaging=bool(
                conn.execute(
                    """SELECT 1 FROM clinical_imaging_subjects
                       WHERE subject_key = ?""",
                    (subject_key,),
                ).fetchone()
            ),
            fact_provenance={
                "derivation_method": (
                    "official_ea1141_year0_reference_standard_and_pathology"
                ),
                "classification": classification,
                "is_patient_observed": True,
            },
        )
        result["classified_subjects"] += 1

    unresolved = result["withdrawn_subjects"] + result["missing_subjects"]
    if unresolved:
        warning(
            conn,
            "ea1141_screening_outcome_unresolved",
            (
                f"{unresolved} EA1141 subjects lack a classifiable year-0 "
                f"screening outcome ({result['withdrawn_subjects']} withdrew; "
                f"{result['missing_subjects']} missing). No primary diagnosis "
                "was derived for them."
            ),
            source_id=source_id,
            short_title="EA1141",
        )
    if result["positive_without_pathology"]:
        warning(
            conn,
            "ea1141_positive_pathology_missing",
            (
                f"{result['positive_without_pathology']} EA1141 positive "
                "subjects lack resolved malignant lesion pathology; no "
                "primary diagnosis was derived for them."
            ),
            source_id=source_id,
            short_title="EA1141",
        )
    return result


def promote_hnscc_official_cohort(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Link the exact 627-subject official clinical union to the HNSCC cohort."""
    rows = conn.execute(
        """SELECT DISTINCT r.subject_key, r.subject_id, s.source_url
           FROM clinical_rows r
           JOIN clinical_sources s USING (source_id)
           WHERE r.short_title = 'HNSCC'
             AND s.source_kind = 'tcia_clinical_download'
             AND s.source_id NOT LIKE 'tcia-derived:%'
           ORDER BY r.subject_key, s.source_url"""
    ).fetchall()
    atlas: dict[str, str] = {}
    radiomics: dict[str, str] = {}
    all_subjects: dict[str, str] = {}
    for row in rows:
        source_url = (row["source_url"] or "").lower()
        target = None
        if "hnscc-mda-data" in source_url:
            target = atlas
        elif "radiomics_outcome_prediction_in_opc" in source_url:
            target = radiomics
        if target is None:
            continue
        target[row["subject_key"]] = row["subject_id"]
        all_subjects[row["subject_key"]] = row["subject_id"]

    overlap = set(atlas).intersection(radiomics)
    invalid_ids = sorted(
        subject_id
        for subject_id in all_subjects.values()
        if not re.fullmatch(r"HNSCC-\d{2}-\d{4}", subject_id, re.IGNORECASE)
    )
    result: dict[str, Any] = {
        "expected_collection_subjects": 627,
        "atlas_subjects": len(atlas),
        "radiomics_subjects": len(radiomics),
        "overlap_subjects": len(overlap),
        "union_subjects": len(all_subjects),
        "invalid_subject_ids": invalid_ids,
        "promoted_imaging_subjects": 0,
    }
    if not rows:
        return result

    exact_union = (
        len(atlas) == 215
        and len(radiomics) == 492
        and len(overlap) == 80
        and len(all_subjects) == 627
        and not invalid_ids
    )
    if not exact_union:
        warning(
            conn,
            "hnscc_official_cohort_mismatch",
            (
                "HNSCC official patient-table union did not match the "
                "curated 627-subject cohort "
                f"(atlas={len(atlas)}, radiomics={len(radiomics)}, "
                f"overlap={len(overlap)}, union={len(all_subjects)}, "
                f"invalid_ids={len(invalid_ids)}). Subjects were not promoted "
                "to the image-linked clinical view."
            ),
            short_title="HNSCC",
        )
        return result

    official_subject_keys = set(all_subjects)
    conn.execute(
        """DELETE FROM clinical_imaging_subjects
           WHERE short_title = 'HNSCC'
             AND subject_key NOT IN (
                 SELECT value FROM json_each(?)
             )""",
        (json.dumps(sorted(official_subject_keys)),),
    )
    conn.execute(
        """UPDATE clinical_rows
           SET has_imaging = 0
           WHERE short_title = 'HNSCC'
             AND subject_key NOT IN (
                 SELECT value FROM json_each(?)
             )""",
        (json.dumps(sorted(official_subject_keys)),),
    )
    for subject_key, subject_id in sorted(all_subjects.items()):
        conn.execute(
            """INSERT OR REPLACE INTO clinical_imaging_subjects
               (subject_key, short_title, subject_id, imaging_source)
               VALUES (?, 'HNSCC', ?, 'tcia_official_clinical_union')""",
            (subject_key, subject_id),
        )
    conn.execute(
        """UPDATE clinical_rows
           SET has_imaging = 1
           WHERE short_title = 'HNSCC'
             AND subject_key IN (
                 SELECT subject_key FROM clinical_imaging_subjects
                 WHERE short_title = 'HNSCC'
             )"""
    )
    result["promoted_imaging_subjects"] = len(all_subjects)
    return result


def promote_and_audit_hungarian_colorectal_cohort(
    conn: sqlite3.Connection,
    snapshot_db: Path,
) -> dict[str, Any]:
    """Cross-check the official CSV IDs against PathDB and audit ICD classes."""
    official_rows = conn.execute(
        """SELECT DISTINCT r.subject_key, r.subject_id
           FROM clinical_rows r
           JOIN clinical_sources s USING (source_id)
           WHERE r.short_title = 'Hungarian-Colorectal-Screening'
             AND s.source_kind = 'tcia_clinical_download'
             AND s.source_id NOT LIKE 'tcia-derived:%'
           ORDER BY r.subject_key"""
    ).fetchall()
    official = {row["subject_key"]: row["subject_id"] for row in official_rows}

    snapshot = sqlite3.connect(snapshot_db)
    try:
        tables = {
            row[0]
            for row in snapshot.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        if "agent_pathdb_slides" in tables and official:
            placeholders = ",".join("?" for _ in official)
            matched_rows = snapshot.execute(
                f"""SELECT patient_id, slide_id
                    FROM agent_pathdb_slides
                    WHERE collection = 'Hungarian-Colorectal-Screening'
                      AND patient_id IN ({placeholders})""",
                tuple(official.values()),
            ).fetchall()
            pathdb_ids = {
                clean_value(row[0])
                for row in matched_rows
                if clean_value(row[0])
            }
            pathdb_slides = len(
                {
                    (clean_value(patient_id), clean_value(slide_id))
                    for patient_id, slide_id in matched_rows
                }
            )
        else:
            pathdb_ids = set()
            pathdb_slides = 0
    finally:
        snapshot.close()

    official_ids = set(official.values())
    category_counts = {
        row[0]: row[1]
        for row in conn.execute(
            """SELECT value_text, COUNT(DISTINCT subject_key)
               FROM clinical_facts
               WHERE short_title = 'Hungarian-Colorectal-Screening'
                 AND concept = 'icd10_category'
               GROUP BY value_text ORDER BY value_text"""
        )
    }
    result: dict[str, Any] = {
        "expected_collection_subjects": 200,
        "official_subjects": len(official_ids),
        "pathdb_subjects": len(pathdb_ids),
        "pathdb_slides": pathdb_slides,
        "official_pathdb_overlap": len(official_ids.intersection(pathdb_ids)),
        "official_only_ids": sorted(official_ids - pathdb_ids),
        "pathdb_only_ids": sorted(pathdb_ids - official_ids),
        "icd10_category_counts": category_counts,
        "malignant_subjects": sum(
            category_counts.get(code, 0) for code in ("C18", "C20", "C76")
        ),
        "nonmalignant_subjects": sum(
            category_counts.get(code, 0)
            for code in ("D12", "K52", "K62", "K63")
        ),
        "indeterminate_subjects": category_counts.get("R89", 0),
        "unmapped_subjects": sum(
            count
            for code, count in category_counts.items()
            if code not in HUNGARIAN_COLORECTAL_ICD10
        ),
        "promoted_imaging_subjects": 0,
    }

    exact_cohort = (
        len(official_ids) == 200
        and len(pathdb_ids) == 200
        and pathdb_slides == 200
        and official_ids == pathdb_ids
    )
    if not exact_cohort:
        warning(
            conn,
            "hungarian_colorectal_cohort_mismatch",
            (
                "Hungarian-Colorectal-Screening official clinical IDs did not "
                "match the exact PathDB cohort "
                f"(official={len(official_ids)}, "
                f"pathdb_subjects={len(pathdb_ids)}, "
                f"pathdb_slides={pathdb_slides}, "
                f"overlap={result['official_pathdb_overlap']}). Clinical "
                "subjects were not promoted to the image-linked view."
            ),
            short_title="Hungarian-Colorectal-Screening",
        )
        return result

    for subject_key, subject_id in sorted(official.items()):
        conn.execute(
            """INSERT OR REPLACE INTO clinical_imaging_subjects
               (subject_key, short_title, subject_id, imaging_source)
               VALUES (?, 'Hungarian-Colorectal-Screening', ?,
                       'pathdb_official_clinical_match')""",
            (subject_key, subject_id),
        )
    conn.execute(
        """UPDATE clinical_rows SET has_imaging = 1
           WHERE short_title = 'Hungarian-Colorectal-Screening'
             AND subject_key IN (
                 SELECT subject_key FROM clinical_imaging_subjects
                 WHERE short_title = 'Hungarian-Colorectal-Screening'
             )"""
    )
    result["promoted_imaging_subjects"] = len(official)

    if result["indeterminate_subjects"]:
        warning(
            conn,
            "hungarian_colorectal_icd10_indeterminate",
            (
                f"{result['indeterminate_subjects']} Hungarian colorectal "
                "subjects have ICD-10 category R89 (abnormal specimen finding "
                "without a diagnosis). Their full codes are retained, but no "
                "primary diagnosis or site is derived."
            ),
            short_title="Hungarian-Colorectal-Screening",
        )
    if result["unmapped_subjects"]:
        warning(
            conn,
            "hungarian_colorectal_icd10_unmapped",
            (
                f"{result['unmapped_subjects']} Hungarian colorectal subjects "
                "have an unrecognized international three-character ICD-10 "
                "category. Their full codes are retained without diagnosis "
                "inference."
            ),
            short_title="Hungarian-Colorectal-Screening",
        )
    return result


def declared_subject_count(snapshot_db: Path, short_title: str) -> int:
    snapshot = sqlite3.connect(snapshot_db)
    try:
        try:
            row = snapshot.execute(
                """SELECT subjects FROM agent_datasets
                   WHERE hidden = 0 AND lower(short_title) = lower(?)
                   ORDER BY CASE dataset_type WHEN 'Collection' THEN 0 ELSE 1 END
                   LIMIT 1""",
                (short_title,),
            ).fetchone()
        except sqlite3.Error:
            row = None
    finally:
        snapshot.close()
    value = clean_value(row[0]) if row else ""
    return int(value) if re.fullmatch(r"\d+", value) else 0


def official_subject_ids(
    conn: sqlite3.Connection, short_title: str
) -> set[str]:
    return {
        clean_value(row[0])
        for row in conn.execute(
            """SELECT DISTINCT r.subject_id
               FROM clinical_rows r
               JOIN clinical_sources s USING (source_id)
               WHERE lower(r.short_title) = lower(?)
                 AND s.source_kind = 'tcia_clinical_download'
               ORDER BY r.subject_id""",
            (short_title,),
        )
        if clean_value(row[0])
    }


def promote_imaging_subject_ids(
    conn: sqlite3.Connection,
    short_title: str,
    subject_ids: Iterable[str],
    imaging_source: str,
) -> int:
    promoted = 0
    for subject_id in sorted(set(subject_ids)):
        subject_key = (
            f"{normalize_name(short_title)}:{normalize_subject(subject_id)}"
        )
        conn.execute(
            """INSERT OR REPLACE INTO clinical_imaging_subjects
               (subject_key, short_title, subject_id, imaging_source)
               VALUES (?, ?, ?, ?)""",
            (subject_key, short_title, subject_id, imaging_source),
        )
        promoted += 1
    conn.execute(
        """UPDATE clinical_rows SET has_imaging = 1
           WHERE subject_key IN (
               SELECT subject_key FROM clinical_imaging_subjects
               WHERE lower(short_title) = lower(?)
           )""",
        (short_title,),
    )
    return promoted


def promote_reviewed_official_id_cohorts(
    conn: sqlite3.Connection, snapshot_db: Path
) -> dict[str, Any]:
    """Promote reviewed official IDs only when they equal the published cohort.

    This is the durable equivalent of an exact clinical-to-package crosswalk
    for collections whose official table encodes visit/scan IDs or padding
    differently from the patient identifier.
    """
    results: dict[str, Any] = {}
    for short_title, pattern in REVIEWED_OFFICIAL_COHORT_PATTERNS.items():
        official_ids = official_subject_ids(conn, short_title)
        cohort_ids = {
            subject_id
            for subject_id in official_ids
            if re.fullmatch(pattern, subject_id)
        }
        expected = declared_subject_count(snapshot_db, short_title)
        exact = bool(
            expected
            and len(cohort_ids) == expected
            and cohort_ids == official_ids
        )
        promoted = 0
        if exact:
            promoted = promote_imaging_subject_ids(
                conn,
                short_title,
                cohort_ids,
                "tcia_official_clinical_exact_cohort",
            )
        else:
            warning(
                conn,
                "reviewed_official_cohort_mismatch",
                (
                    f"{short_title} official IDs did not equal the current "
                    f"published cohort: expected={expected}, "
                    f"official={len(official_ids)}, pattern={len(cohort_ids)}"
                ),
                short_title=short_title,
            )
        results[short_title] = {
            "expected_subjects": expected,
            "official_subjects": len(official_ids),
            "pattern_subjects": len(cohort_ids),
            "nonmatching_ids": sorted(official_ids - cohort_ids),
            "promoted_imaging_subjects": promoted,
            "status": "promoted" if exact else "manual_review",
        }
    return results


def promote_tompei_cmmd_package_cohort(
    conn: sqlite3.Connection,
    snapshot_db: Path,
    *,
    no_fetch: bool,
    timeout: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Crosswalk TOMPEI-CMMD clinical IDs to its annotation package."""
    result: dict[str, Any] = {
        "expected_subjects": declared_subject_count(snapshot_db, "TOMPEI-CMMD"),
        "official_subjects": len(official_subject_ids(conn, "TOMPEI-CMMD")),
        "package_subjects": 0,
        "promoted_imaging_subjects": 0,
        "status": "not_attempted",
    }
    snapshot = sqlite3.connect(snapshot_db)
    snapshot.row_factory = sqlite3.Row
    try:
        package = snapshot.execute(
            """SELECT download_id, download_url
               FROM agent_current_downloads
               WHERE hidden = 0 AND lower(short_title) = 'tompei-cmmd'
                 AND lower(file_types) LIKE '%zip%'
                 AND lower(data_types) LIKE '%segmentation%'
               ORDER BY download_id LIMIT 1"""
        ).fetchone()
    except sqlite3.Error:
        package = None
    snapshot.close()
    if not package or not clean_value(package["download_url"]):
        result["status"] = "package_not_found"
        warning(
            conn,
            "tompei_cmmd_package_not_found",
            "No visible TOMPEI-CMMD segmentation ZIP was available for patient crosswalk.",
            short_title="TOMPEI-CMMD",
        )
        return result
    result.update(
        {
            "download_id": clean_value(package["download_id"]),
            "download_url": clean_value(package["download_url"]),
        }
    )
    if no_fetch:
        result["status"] = "fetch_disabled"
        return result
    try:
        data = fetch_url(
            result["download_url"], timeout=timeout, max_bytes=max_bytes
        )
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            package_ids = {
                match.group(1)
                for name in archive.namelist()
                if not name.startswith("__MACOSX/")
                for match in [
                    re.fullmatch(
                        r"(D[12]-\d{4})_MLO_[LR]_AnnotationFile\.json",
                        Path(name).name,
                    )
                ]
                if match
            }
        official_ids = official_subject_ids(conn, "TOMPEI-CMMD")
        expected = result["expected_subjects"]
        exact = bool(
            expected
            and len(package_ids) == expected
            and package_ids.issubset(official_ids)
        )
        result.update(
            {
                "artifact_bytes": len(data),
                "artifact_sha256": sha256_bytes(data),
                "package_subjects": len(package_ids),
                "package_ids_missing_clinical": sorted(package_ids - official_ids),
                "clinical_ids_outside_package": len(official_ids - package_ids),
            }
        )
        if exact:
            result["promoted_imaging_subjects"] = promote_imaging_subject_ids(
                conn,
                "TOMPEI-CMMD",
                package_ids,
                "tcia_analysis_result_package_exact_id",
            )
            result["status"] = "promoted"
        else:
            result["status"] = "manual_review"
            warning(
                conn,
                "tompei_cmmd_package_cohort_mismatch",
                (
                    "TOMPEI-CMMD package IDs did not exactly support the "
                    f"published cohort: expected={expected}, "
                    f"package={len(package_ids)}, official={len(official_ids)}"
                ),
                short_title="TOMPEI-CMMD",
            )
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        warning(
            conn,
            "tompei_cmmd_package_crosswalk_failed",
            str(exc),
            short_title="TOMPEI-CMMD",
        )
    return result


def ingest_ivygap_external_clinical(
    conn: sqlite3.Connection,
    snapshot_db: Path,
    *,
    no_fetch: bool,
    timeout: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Ingest Allen tumor details only after matching TCIA's current manifest."""
    source_id = "tcia-linked-external:ivygap:allen-tumor-details"
    result: dict[str, Any] = {
        "status": "disabled" if no_fetch else "pending",
        "allen_tumor_rows": 0,
        "allen_subjects": 0,
        "tcia_manifest_rows": 0,
        "tcia_manifest_subjects": 0,
        "matched_tumor_rows": 0,
        "matched_subjects": 0,
        "external_only_subjects": [],
        "manifest_only_subjects": [],
        "multiple_tumor_subjects": [],
        "concept_subject_counts": {},
        "promoted_imaging_subjects": 0,
    }
    if no_fetch:
        return result
    if "IvyGAP" not in visible_tcia_short_titles(snapshot_db):
        result["status"] = "not_applicable"
        return result

    snapshot = sqlite3.connect(snapshot_db)
    snapshot.row_factory = sqlite3.Row
    try:
        dataset = snapshot.execute(
            """SELECT title, date_updated FROM agent_datasets
               WHERE short_title = 'IvyGAP' AND hidden = 0
               ORDER BY CASE WHEN dataset_type = 'Collection' THEN 0 ELSE 1 END
               LIMIT 1"""
        ).fetchone()
        manifest = snapshot.execute(
            """SELECT download_url, date_updated
               FROM agent_current_downloads
               WHERE short_title = 'IvyGAP' AND hidden = 0
                 AND download_url LIKE '%GC_manifest_IvyGAP%'
               ORDER BY date_updated DESC, download_id DESC LIMIT 1"""
        ).fetchone()
    finally:
        snapshot.close()

    if not dataset or not manifest or not clean_value(manifest["download_url"]):
        result["status"] = "failed"
        warning(
            conn,
            "ivygap_identity_manifest_missing",
            (
                "The current visible IvyGAP Collection or General Commons "
                "manifest URL was not available; Allen clinical rows were not "
                "ingested."
            ),
            source_id=source_id,
            short_title="IvyGAP",
        )
        return result

    manifest_url = clean_value(manifest["download_url"])
    try:
        tumor_data = fetch_url(
            IVYGAP_TUMOR_DETAILS_URL,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        manifest_data = fetch_url(
            manifest_url,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        tumor_frame = read_delimited(tumor_data, ".csv")
        manifest_frame = read_delimited(manifest_data, ".csv")
    except Exception as exc:
        result["status"] = "failed"
        warning(
            conn,
            "ivygap_external_clinical_failed",
            str(exc),
            source_id=source_id,
            short_title="IvyGAP",
        )
        return result

    required_tumor_columns = {
        "donor_id",
        "tumor_name",
        "molecular_subtype",
        "extent_of_resection",
        "surgery",
        "mgmt_methylation",
        "survival_days",
        "egfr_amplification",
        "initial_kps",
        "age_in_years",
    }
    tumor_columns = {str(column) for column in tumor_frame.columns}
    if not required_tumor_columns.issubset(tumor_columns):
        missing = sorted(required_tumor_columns - tumor_columns)
        result["status"] = "failed"
        warning(
            conn,
            "ivygap_external_schema_changed",
            f"Allen tumor-details CSV is missing columns: {missing}",
            source_id=source_id,
            short_title="IvyGAP",
        )
        return result
    if "Participant ID" not in manifest_frame.columns:
        result["status"] = "failed"
        warning(
            conn,
            "ivygap_manifest_schema_changed",
            "TCIA IvyGAP manifest is missing the Participant ID column.",
            source_id=source_id,
            short_title="IvyGAP",
        )
        return result

    manifest_rows = [row for _, row in manifest_frame.iterrows()]
    manifest_subjects = {
        clean_value(row.get("Participant ID"))
        for row in manifest_rows
        if clean_value(row.get("Participant ID"))
    }
    parsed_rows: list[tuple[int, str, dict[str, Any]]] = []
    allen_subjects: set[str] = set()
    subject_tumors: dict[str, set[str]] = {}
    invalid_tumor_names: list[str] = []
    for row_number, row in tumor_frame.iterrows():
        values = {str(key): value for key, value in row.items()}
        tumor_name = clean_value(values.get("tumor_name"))
        match = re.fullmatch(r"(W\d+)-\d+-\d+", tumor_name, re.IGNORECASE)
        if not match:
            invalid_tumor_names.append(tumor_name)
            continue
        subject_id = match.group(1).upper()
        allen_subjects.add(subject_id)
        subject_tumors.setdefault(subject_id, set()).add(tumor_name)
        parsed_rows.append((row_number + 1, subject_id, values))

    result.update(
        {
            "allen_tumor_rows": len(parsed_rows),
            "allen_subjects": len(allen_subjects),
            "tcia_manifest_rows": len(manifest_rows),
            "tcia_manifest_subjects": len(manifest_subjects),
            "external_only_subjects": sorted(
                allen_subjects - manifest_subjects
            ),
            "manifest_only_subjects": sorted(
                manifest_subjects - allen_subjects
            ),
            "multiple_tumor_subjects": sorted(
                subject_id
                for subject_id, tumors in subject_tumors.items()
                if len(tumors) > 1
            ),
        }
    )
    if invalid_tumor_names or not manifest_subjects:
        result["status"] = "failed"
        warning(
            conn,
            "ivygap_identity_mapping_failed",
            (
                f"Could not validate IvyGAP identity mapping "
                f"(invalid_tumor_names={invalid_tumor_names}, "
                f"manifest_subjects={len(manifest_subjects)})."
            ),
            source_id=source_id,
            short_title="IvyGAP",
        )
        return result

    combined_signature = sha256_bytes(
        (
            sha256_bytes(tumor_data)
            + sha256_bytes(manifest_data)
            + IVYGAP_TUMOR_DETAILS_URL
            + manifest_url
        ).encode("utf-8")
    )
    insert_source(
        conn,
        source_id=source_id,
        source_kind="tcia_linked_external_clinical",
        source_lineage="tcia-linked-external:ivygap:allen-institute",
        short_title="IvyGAP",
        source_signature_value=combined_signature,
        source_url=IVYGAP_TUMOR_DETAILS_URL,
        source_date=clean_value(dataset["date_updated"]),
        artifact_sha256=sha256_bytes(tumor_data),
        artifact_bytes=len(tumor_data),
        provenance={
            "provider": "Allen Institute",
            "tcia_identity_manifest_url": manifest_url,
            "tcia_identity_manifest_sha256": sha256_bytes(manifest_data),
            "identity_derivation": (
                "Allen tumor name is site/patient-surgery-tumor; the leading "
                "W-number is matched to TCIA General Commons Participant ID."
            ),
        },
    )

    for subject_id in sorted(manifest_subjects):
        subject_key = f"ivygap:{normalize_subject(subject_id)}"
        conn.execute(
            """INSERT OR REPLACE INTO clinical_imaging_subjects
               (subject_key, short_title, subject_id, imaging_source)
               VALUES (?, 'IvyGAP', ?, 'tcia_general_commons_manifest')""",
            (subject_key, subject_id),
        )
    result["promoted_imaging_subjects"] = len(manifest_subjects)

    coverage: dict[str, set[str]] = {}
    matched_subjects: set[str] = set()
    matched_rows = 0

    def add_fact(
        facts: list[tuple[str, str, str, str | None]],
        concept: str,
        value: Any,
        column: str,
        unit: str | None = None,
    ) -> None:
        cleaned = clean_value(value)
        if cleaned:
            facts.append((concept, cleaned, column, unit))

    for row_number, subject_id, values in parsed_rows:
        if subject_id not in manifest_subjects:
            continue
        facts: list[tuple[str, str, str, str | None]] = []
        age = clean_value(values.get("age_in_years"))
        age_number = number_value(age)
        add_fact(facts, "donor_id", values.get("donor_id"), "donor_id")
        add_fact(facts, "tumor_name", values.get("tumor_name"), "tumor_name")
        add_fact(
            facts,
            "molecular_subtype",
            values.get("molecular_subtype"),
            "molecular_subtype",
        )
        add_fact(
            facts,
            "extent_of_resection",
            values.get("extent_of_resection"),
            "extent_of_resection",
        )
        add_fact(
            facts,
            "surgery_status",
            values.get("surgery"),
            "surgery",
        )
        add_fact(
            facts,
            "mgmt_methylation",
            values.get("mgmt_methylation"),
            "mgmt_methylation",
        )
        add_fact(
            facts,
            "overall_survival_days",
            values.get("survival_days"),
            "survival_days",
            "days",
        )
        add_fact(
            facts,
            "egfr_amplification",
            values.get("egfr_amplification"),
            "egfr_amplification",
        )
        add_fact(
            facts,
            "initial_kps",
            values.get("initial_kps"),
            "initial_kps",
            "score (0-100)",
        )
        if age_number is not None:
            add_fact(
                facts,
                "age_at_diagnosis",
                f"{age_number:g}",
                "age_in_years",
                "years",
            )
        insert_row_and_facts(
            conn,
            source_id=source_id,
            source_kind="tcia_linked_external_clinical",
            short_title="IvyGAP",
            subject_id=subject_id,
            table_name="allen_institute.tumor_details",
            row_number=row_number,
            row=values,
            facts=facts,
            has_imaging=True,
            fact_provenance={
                "is_patient_observed": True,
                "tumor_level_record": True,
                "identity_manifest_url": manifest_url,
            },
        )
        matched_rows += 1
        matched_subjects.add(subject_id)
        for concept, value, _, _ in facts:
            if clean_value(value):
                coverage.setdefault(concept, set()).add(subject_id)

    result["matched_tumor_rows"] = matched_rows
    result["matched_subjects"] = len(matched_subjects)
    result["concept_subject_counts"] = {
        concept: len(subjects) for concept, subjects in sorted(coverage.items())
    }
    result["status"] = "loaded"

    if result["manifest_only_subjects"]:
        warning(
            conn,
            "ivygap_manifest_subject_without_allen_clinical",
            (
                f"{len(result['manifest_only_subjects'])} TCIA IvyGAP "
                "participants were absent from Allen tumor_details.csv: "
                f"{result['manifest_only_subjects']}"
            ),
            source_id=source_id,
            short_title="IvyGAP",
        )

    conn.execute(
        """INSERT OR REPLACE INTO clinical_downloads
           (source_id, short_title, dataset_type, dataset_title, download_id,
            download_title, download_url, date_updated, file_types,
            download_types, data_types, access_level, controlled_access,
            source_signature, ingest_status, rows_loaded, subjects_loaded,
            error_text)
           VALUES (?, 'IvyGAP', 'External linked clinical', ?, ?, ?, ?, ?,
                   ?, ?, ?, 'unknown', 0, ?, 'loaded', ?, ?, '')""",
        (
            source_id,
            clean_value(dataset["title"]),
            "allen-tumor-details",
            "Allen Institute IvyGAP tumor details",
            IVYGAP_TUMOR_DETAILS_URL,
            clean_value(dataset["date_updated"]),
            json_dumps(["CSV"]),
            json_dumps(["Clinical Data"]),
            json_dumps(
                [
                    "Demographic",
                    "Molecular",
                    "Treatment",
                    "Outcome",
                ]
            ),
            combined_signature,
            matched_rows,
            len(matched_subjects),
        ),
    )
    return result


def import_legacy_seed(
    conn: sqlite3.Connection,
    seed_db: Path,
    allowed_short_titles: set[str],
) -> dict[str, int]:
    """Import normalized CDA and DICOM facts from the existing prototype DB."""
    source = sqlite3.connect(seed_db)
    source.row_factory = sqlite3.Row
    tables = {
        row[0]
        for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    required = {"dataset", "subject", "diagnosis", "source_subjects"}
    if not required.issubset(tables):
        source.close()
        raise RuntimeError(
            f"Legacy seed lacks expected tables: {sorted(required - tables)}"
        )

    datasets = {
        row["dataset_id"]: row["tcia_short_title"]
        for row in source.execute("SELECT dataset_id, tcia_short_title FROM dataset")
    }
    cda_sources: set[str] = set()
    dicom_sources: set[str] = set()
    subject_lookup: dict[str, tuple[str, str]] = {}
    counts = {
        "cda_rows": 0,
        "dicom_rows": 0,
        "facts": 0,
        "unmatched_rows_skipped": 0,
        "cda_non_tcia_subjects_skipped": 0,
    }

    allowed_subject_keys = {
        row[0]
        for row in conn.execute(
            "SELECT subject_key FROM clinical_imaging_subjects"
        )
    }
    allowed_subject_keys.update(
        row[0]
        for row in conn.execute(
            """SELECT DISTINCT r.subject_key
               FROM clinical_rows r JOIN clinical_sources s USING (source_id)
               WHERE s.source_kind = 'tcia_clinical_download'"""
        )
    )
    legacy_dicom_rows = source.execute(
        """SELECT * FROM source_subjects
           WHERE source_kind = 'idc_index' AND evidence_json IS NOT NULL"""
    ).fetchall()
    for row in legacy_dicom_rows:
        evidence = json.loads(row["evidence_json"])
        short_title = clean_value(evidence.get("short_title") or row["short_title"])
        subject_id = clean_value(row["source_value"])
        if short_title in allowed_short_titles and subject_id:
            subject_key = (
                f"{normalize_name(short_title)}:{normalize_subject(subject_id)}"
            )
            allowed_subject_keys.add(subject_key)
            conn.execute(
                """INSERT OR REPLACE INTO clinical_imaging_subjects
                   (subject_key, short_title, subject_id, imaging_source)
                   VALUES (?, ?, ?, 'legacy_idc_index')""",
                (subject_key, short_title, subject_id),
            )

    for row in source.execute("SELECT * FROM subject"):
        short_title = datasets.get(row["dataset_id"], row["dataset_id"])
        if short_title not in allowed_short_titles:
            counts["unmatched_rows_skipped"] += 1
            continue
        subject_id = clean_value(row["source_subject_value"] or row["subject_id"])
        subject_key = f"{normalize_name(short_title)}:{normalize_subject(subject_id)}"
        if subject_key not in allowed_subject_keys:
            counts["cda_non_tcia_subjects_skipped"] += 1
            continue
        source_id = f"cda:{normalize_name(short_title)}"
        if source_id not in cda_sources:
            insert_source(
                conn,
                source_id=source_id,
                source_kind="cda",
                short_title=short_title,
                source_signature_value=stable_id("legacy-cda", short_title),
                source_lineage=f"cda:{normalize_name(short_title)}",
                provenance={"seed_db": str(seed_db), "legacy_table": "subject"},
            )
            cda_sources.add(source_id)
        facts = [
            ("ethnicity", row["ethnicity"], "ethnicity", None),
            ("race", row["race"], "race", None),
            ("sex_at_birth", row["sex_at_birth"], "sex_at_birth", None),
            ("vital_status", row["vital_status"], "vital_status", None),
        ]
        before = conn.total_changes
        insert_row_and_facts(
            conn,
            source_id=source_id,
            source_kind="cda",
            short_title=short_title,
            subject_id=subject_id,
            table_name="legacy.subject",
            row_number=counts["cda_rows"] + 1,
            row=dict(row),
            facts=facts,
            has_imaging=True,
        )
        counts["facts"] += max(0, conn.total_changes - before - 1)
        counts["cda_rows"] += 1
        subject_lookup[row["subject_id"]] = (short_title, subject_id)

    for row in source.execute("SELECT * FROM diagnosis"):
        match = subject_lookup.get(row["subject_id"])
        if not match:
            continue
        short_title, subject_id = match
        source_id = f"cda:{normalize_name(short_title)}"
        facts = [
            ("age_at_diagnosis", row["age_at_diagnosis"], "age_at_diagnosis", None),
            ("primary_diagnosis", row["primary_diagnosis"], "primary_diagnosis", None),
            ("primary_site", row["primary_site"], "primary_site", None),
        ]
        insert_row_and_facts(
            conn,
            source_id=source_id,
            source_kind="cda",
            short_title=short_title,
            subject_id=subject_id,
            table_name="legacy.diagnosis",
            row_number=counts["cda_rows"] + 1,
            row=dict(row),
            facts=facts,
            has_imaging=True,
        )
        counts["cda_rows"] += 1

    for row in legacy_dicom_rows:
        evidence = json.loads(row["evidence_json"])
        short_title = clean_value(evidence.get("short_title") or row["short_title"])
        context = evidence.get("context") or {}
        subject_id = clean_value(row["source_value"])
        if not short_title or not subject_id:
            continue
        if short_title not in allowed_short_titles:
            counts["unmatched_rows_skipped"] += 1
            continue
        source_id = f"dicom:{normalize_name(short_title)}"
        if source_id not in dicom_sources:
            insert_source(
                conn,
                source_id=source_id,
                source_kind="dicom",
                short_title=short_title,
                source_signature_value=stable_id("legacy-dicom", short_title),
                source_lineage=f"dicom:{normalize_name(short_title)}",
                provenance={"seed_db": str(seed_db), "legacy_table": "source_subjects"},
            )
            dicom_sources.add(source_id)
        patient_age = clean_value(context.get("PatientAge"))
        age_years = ""
        age_unit = None
        age_match = re.fullmatch(r"(\d{1,3})([DWMY])", patient_age)
        if age_match:
            number = float(age_match.group(1))
            unit_code = age_match.group(2)
            divisors = {"D": 365.25, "W": 52.1775, "M": 12.0, "Y": 1.0}
            age_years = f"{number / divisors[unit_code]:g}"
            age_unit = "years"
        facts = [
            ("sex_at_birth", context.get("PatientSex"), "PatientSex", None),
            ("age_at_imaging_years", age_years, "PatientAge", age_unit),
        ]
        insert_row_and_facts(
            conn,
            source_id=source_id,
            source_kind="dicom",
            short_title=short_title,
            subject_id=subject_id,
            table_name="legacy.idc_index",
            row_number=counts["dicom_rows"] + 1,
            row=context,
            facts=facts,
            has_imaging=True,
        )
        counts["dicom_rows"] += 1
    source.close()
    return counts


AGE_CONCEPTS = {
    "age_at_diagnosis",
    "age_at_enrollment_years",
    "age_at_imaging_years",
    "age_at_treatment_years",
}
AGE_MISSING_TOKENS = {"--", "not reported", "not available"}
CATEGORICAL_MISSING_TOKENS = {"--", "not available"}

CMB_DATASETS = {
    "cmbaml",
    "cmbcrc",
    "cmbgec",
    "cmblca",
    "cmbmel",
    "cmbmml",
    "cmbpca",
}


def insert_qc_finding(
    conn: sqlite3.Connection,
    *,
    rule_id: str,
    severity: str,
    disposition: str,
    short_title: str,
    message: str,
    subject_id: str = "",
    subject_key: str = "",
    source_id: str = "",
    source_row_id: str = "",
    fact_id: str = "",
    concept: str = "",
    original_value: str = "",
    resolved_value: str = "",
    unit: str = "",
    provenance: dict[str, Any] | None = None,
) -> None:
    review_status = "open" if disposition == "manual_review" else "accepted"
    finding_id = stable_id(
        rule_id,
        short_title,
        subject_id,
        source_row_id,
        fact_id,
        original_value,
    )
    conn.execute(
        """INSERT OR REPLACE INTO clinical_qc_findings
           (finding_id, rule_id, severity, disposition, review_status,
            short_title, subject_id, subject_key, source_id, source_row_id,
            fact_id, concept, original_value, resolved_value, unit, message,
            provenance_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            finding_id,
            rule_id,
            severity,
            disposition,
            review_status,
            short_title,
            subject_id or None,
            subject_key or None,
            source_id or None,
            source_row_id or None,
            fact_id or None,
            concept or None,
            original_value or None,
            resolved_value or None,
            unit or None,
            message,
            json_dumps(provenance or {}),
        ),
    )


def materialize_clinical_qc(conn: sqlite3.Connection) -> dict[str, int]:
    """Apply audited clinical QC without changing source text or source rows."""
    conn.execute("DELETE FROM clinical_qc_findings")
    conn.execute(
        """UPDATE clinical_facts
           SET value_resolved = value_text,
               qc_excluded = 0,
               qc_status = 'accepted'"""
    )

    # Reapply audited dictionaries during QC so incrementally reused facts
    # receive newly added mappings without rewriting their preserved source
    # value. This is intentionally dataset-specific: numeric codes do not
    # have portable meanings across clinical studies.
    coded_facts = conn.execute(
        """SELECT * FROM clinical_facts
           WHERE qc_excluded = 0
             AND concept IN ('sex_at_birth', 'vital_status')"""
    ).fetchall()
    for fact in coded_facts:
        resolved = decode_dataset_concept(
            fact["short_title"], fact["concept"], fact["value_text"]
        )
        if resolved == fact["value_text"]:
            continue
        normalized = normalize_concept_value(fact["concept"], resolved)
        conn.execute(
            """UPDATE clinical_facts
               SET value_resolved = ?, value_normalized = ?,
                   qc_status = 'normalized_dataset_code'
               WHERE fact_id = ?""",
            (resolved, normalized, fact["fact_id"]),
        )
        insert_qc_finding(
            conn,
            rule_id="dataset_specific_categorical_code",
            severity="info",
            disposition="auto_normalize",
            short_title=fact["short_title"],
            subject_id=fact["subject_id"],
            subject_key=fact["subject_key"],
            source_id=fact["source_id"],
            source_row_id=fact["source_row_id"],
            fact_id=fact["fact_id"],
            concept=fact["concept"],
            original_value=fact["value_text"],
            resolved_value=resolved,
            message=(
                "Decoded a categorical value using the audited official "
                "dictionary for this dataset."
            ),
        )

    categorical_facts = conn.execute(
        """SELECT * FROM clinical_facts
           WHERE qc_excluded = 0
             AND concept IN ('sex_at_birth', 'race', 'ethnicity',
                             'vital_status', 'recurrence', 'progression',
                             'response', 'screening_result')"""
    ).fetchall()
    for fact in categorical_facts:
        if normalize_value(fact["value_resolved"]) not in CATEGORICAL_MISSING_TOKENS:
            continue
        conn.execute(
            """UPDATE clinical_facts
               SET qc_excluded = 1, qc_status = 'excluded_missing_token'
               WHERE fact_id = ?""",
            (fact["fact_id"],),
        )
        insert_qc_finding(
            conn,
            rule_id="categorical_missing_value_token",
            severity="info",
            disposition="auto_exclude",
            short_title=fact["short_title"],
            subject_id=fact["subject_id"],
            subject_key=fact["subject_key"],
            source_id=fact["source_id"],
            source_row_id=fact["source_row_id"],
            fact_id=fact["fact_id"],
            concept=fact["concept"],
            original_value=fact["value_text"],
            message="Normalized an explicit categorical missing-value token to null.",
        )

    # This workbook sheet contains prose notes. Its header-like Patient ID
    # must never become a patient, but the raw source row remains auditable.
    explanatory_rows = conn.execute(
        """SELECT DISTINCT r.*
           FROM clinical_rows r
           WHERE lower(replace(r.short_title, '-', '')) = 'headneckpetct'
             AND lower(trim(r.subject_id)) = 'explanations'
             AND r.table_name LIKE '%::Excluded'"""
    ).fetchall()
    for row in explanatory_rows:
        conn.execute(
            """UPDATE clinical_facts
               SET qc_excluded = 1, qc_status = 'excluded_non_patient_row'
               WHERE source_row_id = ?""",
            (row["source_row_id"],),
        )
        insert_qc_finding(
            conn,
            rule_id="non_patient_explanatory_row",
            severity="error",
            disposition="auto_exclude",
            short_title=row["short_title"],
            subject_id=row["subject_id"],
            subject_key=row["subject_key"],
            source_id=row["source_id"],
            source_row_id=row["source_row_id"],
            message=(
                "Excluded the Head-Neck-PET-CT workbook Explanations row "
                "from patient-level facts; the source row remains preserved."
            ),
            provenance={"table_name": row["table_name"], "row_number": row["row_number"]},
        )

    # TCGA-style age_at_diagnosis in this official Crowds-Cure snapshot is
    # expressed in days. Keep value_text unchanged and resolve in years.
    crowds_age_facts = conn.execute(
        """SELECT * FROM clinical_facts
           WHERE lower(replace(short_title, '-', '')) = 'crowdscure2017'
             AND source_kind = 'tcia_clinical_download'
             AND concept = 'age_at_diagnosis'
             AND lower(replace(original_column, '_', '')) = 'ageatdiagnosis'
             AND qc_excluded = 0"""
    ).fetchall()
    for fact in crowds_age_facts:
        days = number_value(fact["value_text"])
        if days is None or not (3650 <= days <= 47580):
            continue
        years = days / 365.25
        resolved = f"{years:.2f}".rstrip("0").rstrip(".")
        conn.execute(
            """UPDATE clinical_facts
               SET value_resolved = ?, value_normalized = ?,
                   value_number = ?, unit = 'years',
                   qc_status = 'normalized_age_days_to_years'
               WHERE fact_id = ?""",
            (resolved, f"{years:g}", years, fact["fact_id"]),
        )
        insert_qc_finding(
            conn,
            rule_id="crowds_cure_age_days_to_years",
            severity="error",
            disposition="auto_normalize",
            short_title=fact["short_title"],
            subject_id=fact["subject_id"],
            subject_key=fact["subject_key"],
            source_id=fact["source_id"],
            source_row_id=fact["source_row_id"],
            fact_id=fact["fact_id"],
            concept=fact["concept"],
            original_value=fact["value_text"],
            resolved_value=resolved,
            unit="years",
            message=(
                "Converted official Crowds-Cure-2017 age_at_diagnosis "
                "from days to years using 365.25 days/year."
            ),
            provenance={
                "conversion": "days / 365.25",
                "original_unit": "days",
            },
        )

    age_facts = conn.execute(
        """SELECT * FROM clinical_facts
           WHERE concept IN ('age_at_diagnosis', 'age_at_enrollment_years',
                             'age_at_imaging_years')
             AND qc_excluded = 0"""
    ).fetchall()
    for fact in age_facts:
        normalized_text = normalize_value(fact["value_text"])
        if normalized_text in AGE_MISSING_TOKENS:
            conn.execute(
                """UPDATE clinical_facts
                   SET qc_excluded = 1, qc_status = 'excluded_missing_token'
                   WHERE fact_id = ?""",
                (fact["fact_id"],),
            )
            insert_qc_finding(
                conn,
                rule_id="age_missing_value_token",
                severity="info",
                disposition="auto_exclude",
                short_title=fact["short_title"],
                subject_id=fact["subject_id"],
                subject_key=fact["subject_key"],
                source_id=fact["source_id"],
                source_row_id=fact["source_row_id"],
                fact_id=fact["fact_id"],
                concept=fact["concept"],
                original_value=fact["value_text"],
                message="Normalized a documented missing-age token to null.",
            )
            continue

        numeric = fact["value_number"]
        if (
            normalize_name(fact["short_title"]) == "radcure"
            and fact["concept"] == "age_at_imaging_years"
            and fact["source_kind"] == "dicom"
        ):
            authoritative = conn.execute(
                """SELECT fact_id, source_id, source_kind, value_resolved,
                          value_number, unit
                   FROM clinical_facts
                   WHERE subject_key = ?
                     AND concept = 'age_at_imaging_years'
                     AND source_kind = 'tcia_clinical_download'
                     AND source_priority > ?
                     AND value_number > 0
                     AND value_number <= 120
                     AND qc_excluded = 0
                   ORDER BY source_priority DESC, fact_id
                   LIMIT 1""",
                (fact["subject_key"], fact["source_priority"]),
            ).fetchone()
            if authoritative:
                conn.execute(
                    """UPDATE clinical_facts
                       SET qc_excluded = 1,
                           qc_status = 'superseded_by_official_radcure_age'
                       WHERE fact_id = ?""",
                    (fact["fact_id"],),
                )
                insert_qc_finding(
                    conn,
                    rule_id="radcure_dicom_age_superseded",
                    severity="info" if numeric not in {0.0, 999.0} else "warning",
                    disposition="auto_exclude",
                    short_title=fact["short_title"],
                    subject_id=fact["subject_id"],
                    subject_key=fact["subject_key"],
                    source_id=fact["source_id"],
                    source_row_id=fact["source_row_id"],
                    fact_id=fact["fact_id"],
                    concept=fact["concept"],
                    original_value=fact["value_text"],
                    resolved_value=authoritative["value_resolved"],
                    unit=authoritative["unit"] or "years",
                    message=(
                        "Excluded RADCURE DICOM PatientAge from resolution "
                        "because the official TCIA clinical workbook provides "
                        "a valid subject-level age."
                    ),
                    provenance={
                        "authoritative_fact_id": authoritative["fact_id"],
                        "authoritative_source_id": authoritative["source_id"],
                        "authoritative_source_kind": authoritative["source_kind"],
                    },
                )
                continue
        if (
            normalize_name(fact["short_title"]) == "hncimrt7033"
            and fact["concept"] == "age_at_imaging_years"
            and fact["source_kind"] == "dicom"
            and numeric == 0.0
        ):
            conn.execute(
                """UPDATE clinical_facts
                   SET qc_excluded = 1,
                       qc_status = 'excluded_hnc_imrt_zero_age_missing'
                   WHERE fact_id = ?""",
                (fact["fact_id"],),
            )
            insert_qc_finding(
                conn,
                rule_id="hnc_imrt_dicom_zero_age_missing",
                severity="warning",
                disposition="auto_exclude",
                short_title=fact["short_title"],
                subject_id=fact["subject_id"],
                subject_key=fact["subject_key"],
                source_id=fact["source_id"],
                source_row_id=fact["source_row_id"],
                fact_id=fact["fact_id"],
                concept=fact["concept"],
                original_value=fact["value_text"],
                unit=fact["unit"] or "years",
                message=(
                    "Excluded HNC-IMRT-70-33 DICOM PatientAge=000Y as "
                    "missing. TCIA publishes no patient-level clinical age "
                    "alternative for this collection."
                ),
                provenance={
                    "review_basis": (
                        "adult head-and-neck radiotherapy cohort; nonzero "
                        "DICOM ages span 30-83 years; CDA age and birth year "
                        "are absent"
                    )
                },
            )
            continue
        dataset_name = normalize_name(fact["short_title"])
        if (
            fact["concept"] == "age_at_imaging_years"
            and fact["source_kind"] == "dicom"
            and numeric == 0.0
            and dataset_name in CMB_DATASETS | {"glisrt", "headneckcetuximab"}
        ):
            if dataset_name in CMB_DATASETS:
                rule_id = "cmb_dicom_zero_age_missing"
                basis = (
                    "CTDC participant metadata also contains only 000Y or "
                    "blank patient_age values for these CMB collections"
                )
            elif dataset_name == "glisrt":
                rule_id = "glis_rt_dicom_zero_age_missing"
                basis = (
                    "adult glioma radiotherapy cohort; 223 nonzero DICOM "
                    "ages span 18-85 years and no alternative clinical age "
                    "source is published"
                )
            else:
                rule_id = "head_neck_cetuximab_dicom_zero_age_missing"
                basis = (
                    "stage III/IV head-and-neck trial cohort; 45 nonzero "
                    "DICOM ages span 42-76 years and no patient-level "
                    "clinical age table is published"
                )
            conn.execute(
                """UPDATE clinical_facts
                   SET qc_excluded = 1,
                       qc_status = 'excluded_dataset_zero_age_missing'
                   WHERE fact_id = ?""",
                (fact["fact_id"],),
            )
            insert_qc_finding(
                conn,
                rule_id=rule_id,
                severity="warning",
                disposition="auto_exclude",
                short_title=fact["short_title"],
                subject_id=fact["subject_id"],
                subject_key=fact["subject_key"],
                source_id=fact["source_id"],
                source_row_id=fact["source_row_id"],
                fact_id=fact["fact_id"],
                concept=fact["concept"],
                original_value=fact["value_text"],
                unit=fact["unit"] or "years",
                message=(
                    "Excluded DICOM PatientAge=000Y as a source-level "
                    "missing-value placeholder; the raw fact is preserved."
                ),
                provenance={"review_basis": basis},
            )
            continue
        if (
            dataset_name == "acrinfmisobrain"
            and fact["concept"] == "age_at_imaging_years"
            and fact["source_kind"] == "dicom"
            and numeric == 0.0
        ):
            enrollment = conn.execute(
                """SELECT fact_id, source_id, value_resolved
                   FROM clinical_facts
                   WHERE subject_key = ?
                     AND concept = 'age_at_enrollment_years'
                     AND source_kind = 'tcia_clinical_download'
                     AND value_number > 0 AND value_number <= 120
                     AND qc_excluded = 0
                   ORDER BY source_priority DESC, fact_id LIMIT 1""",
                (fact["subject_key"],),
            ).fetchone()
            if enrollment:
                conn.execute(
                    """UPDATE clinical_facts
                       SET qc_excluded = 1,
                           qc_status = 'excluded_zero_age_with_official_enrollment_age'
                       WHERE fact_id = ?""",
                    (fact["fact_id"],),
                )
                insert_qc_finding(
                    conn,
                    rule_id="acrin_fmiso_dicom_zero_age_missing",
                    severity="warning",
                    disposition="auto_exclude",
                    short_title=fact["short_title"],
                    subject_id=fact["subject_id"],
                    subject_key=fact["subject_key"],
                    source_id=fact["source_id"],
                    source_row_id=fact["source_row_id"],
                    fact_id=fact["fact_id"],
                    concept=fact["concept"],
                    original_value=fact["value_text"],
                    resolved_value=enrollment["value_resolved"],
                    unit="years",
                    message=(
                        "Excluded DICOM PatientAge=000Y as missing; the "
                        "official A0.csv provides a valid enrollment age."
                    ),
                    provenance={
                        "official_enrollment_fact_id": enrollment["fact_id"],
                        "official_source_id": enrollment["source_id"],
                    },
                )
                continue
        if (
            fact["concept"] == "age_at_imaging_years"
            and fact["source_kind"] == "dicom"
            and numeric in {0.0, 999.0}
        ):
            disposition = "auto_exclude" if numeric == 999.0 else "manual_review"
            status = (
                "excluded_dicom_999_placeholder"
                if numeric == 999.0
                else "excluded_dicom_zero_pending_review"
            )
            conn.execute(
                """UPDATE clinical_facts
                   SET qc_excluded = 1, qc_status = ?
                   WHERE fact_id = ?""",
                (status, fact["fact_id"]),
            )
            insert_qc_finding(
                conn,
                rule_id=(
                    "dicom_age_999_placeholder"
                    if numeric == 999.0
                    else "dicom_age_zero_review"
                ),
                severity="error" if numeric == 999.0 else "warning",
                disposition=disposition,
                short_title=fact["short_title"],
                subject_id=fact["subject_id"],
                subject_key=fact["subject_key"],
                source_id=fact["source_id"],
                source_row_id=fact["source_row_id"],
                fact_id=fact["fact_id"],
                concept=fact["concept"],
                original_value=fact["value_text"],
                unit=fact["unit"] or "",
                message=(
                    "Excluded DICOM PatientAge=999Y as a placeholder."
                    if numeric == 999.0
                    else (
                        "Excluded DICOM PatientAge=000Y pending review; "
                        "confirm whether the collection can contain neonates."
                    )
                ),
            )
            continue

        if numeric is not None and (numeric < 0 or numeric > 120):
            conn.execute(
                """UPDATE clinical_facts
                   SET qc_excluded = 1, qc_status = 'excluded_age_outlier_pending_review'
                   WHERE fact_id = ?""",
                (fact["fact_id"],),
            )
            insert_qc_finding(
                conn,
                rule_id="age_outside_plausible_range",
                severity="error",
                disposition="manual_review",
                short_title=fact["short_title"],
                subject_id=fact["subject_id"],
                subject_key=fact["subject_key"],
                source_id=fact["source_id"],
                source_row_id=fact["source_row_id"],
                fact_id=fact["fact_id"],
                concept=fact["concept"],
                original_value=fact["value_text"],
                unit=fact["unit"] or "",
                message="Excluded age outside the provisional 0-120 year range.",
            )

    radcure_sex_conflicts = conn.execute(
        """SELECT d.*, o.fact_id AS official_fact_id,
                  o.source_id AS official_source_id,
                  o.value_resolved AS official_value
           FROM clinical_facts d
           JOIN clinical_facts o
             ON o.subject_key = d.subject_key
            AND o.concept = d.concept
            AND o.source_kind = 'tcia_clinical_download'
            AND o.source_priority > d.source_priority
            AND o.qc_excluded = 0
           WHERE lower(replace(d.short_title, '-', '')) = 'radcure'
             AND d.concept = 'sex_at_birth'
             AND d.source_kind = 'dicom'
             AND d.qc_excluded = 0
             AND d.value_normalized <> o.value_normalized
           ORDER BY d.subject_id"""
    ).fetchall()
    for fact in radcure_sex_conflicts:
        insert_qc_finding(
            conn,
            rule_id="radcure_sex_source_conflict",
            severity="warning",
            disposition="manual_review",
            short_title=fact["short_title"],
            subject_id=fact["subject_id"],
            subject_key=fact["subject_key"],
            source_id=fact["source_id"],
            source_row_id=fact["source_row_id"],
            fact_id=fact["fact_id"],
            concept=fact["concept"],
            original_value=fact["value_text"],
            resolved_value=fact["official_value"],
            message=(
                "RADCURE DICOM PatientSex disagrees with the higher-priority "
                "official TCIA clinical workbook."
            ),
            provenance={
                "official_fact_id": fact["official_fact_id"],
                "official_source_id": fact["official_source_id"],
                "official_value": fact["official_value"],
            },
        )

    # Promote ingestion coverage warnings into the same auditable QC queue.
    # Reference/dictionary sheets are expected to lack patient IDs; probable
    # patient tables and parser failures remain open for targeted follow-up.
    documentation_markers = (
        "about this excel file",
        "data dictionary",
        "_dictionary",
        "dictionary reviewed",
        "formdictionary",
        "form index",
        "table legend",
        "cde mapping",
        "column description",
        "name glossary",
        "form element number",
        "data category",
    )
    expected_non_tabular_downloads = {
        "braintrgammaknife",
        "dicomsrbreastclinical",
        "ldctandprojectiondata",
        "lungctdiagnosis",
        "midrcricord1a",
        "midrcricord1c",
        "nlst",
        "pdmrtextureanalysis",
    }
    coverage_warnings = conn.execute(
        """SELECT * FROM clinical_build_warnings
           WHERE warning_type IN ('subject_column_not_found',
                                  'official_download_failed')"""
    ).fetchall()
    for item in coverage_warnings:
        warning_text = item["warning_text"] or ""
        dataset_name = normalize_name(item["short_title"])
        if item["warning_type"] == "subject_column_not_found":
            expected = any(
                marker in warning_text.lower() for marker in documentation_markers
            )
            rule_id = (
                "expected_non_patient_reference_table"
                if expected
                else "subject_identifier_mapping_required"
            )
            disposition = "accepted_skip" if expected else "manual_review"
            severity = "info" if expected else "warning"
            message = (
                "Skipped a reference or dictionary table that is not expected "
                "to contain patient identifiers."
                if expected
                else (
                    "A probable patient-level table was skipped because no "
                    "conservative subject identifier mapping is defined."
                )
            )
        else:
            expected = (
                dataset_name in expected_non_tabular_downloads
                and "no supported csv/tsv/xls/xlsx table found"
                in warning_text.lower()
            )
            rule_id = (
                "expected_unsupported_non_tabular_download"
                if expected
                else "official_clinical_download_ingest_failure"
            )
            disposition = "accepted_skip" if expected else "manual_review"
            severity = "info" if expected else "error"
            message = (
                "Skipped an official supporting package that is not a "
                "CSV/TSV/Excel patient table."
                if expected
                else (
                    "An official clinical-looking download could not be "
                    "ingested and requires parser or source review."
                )
            )
        insert_qc_finding(
            conn,
            rule_id=rule_id,
            severity=severity,
            disposition=disposition,
            short_title=item["short_title"] or "",
            source_id=item["source_id"] or "",
            original_value=warning_text,
            message=message,
            provenance={
                "warning_id": item["warning_id"],
                "warning_type": item["warning_type"],
                "warning_text": warning_text,
            },
        )

    return {
        row["disposition"]: row["count"]
        for row in conn.execute(
            """SELECT disposition, COUNT(*) AS count
               FROM clinical_qc_findings GROUP BY disposition"""
        )
    }


def materialize_subjects(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM clinical_subjects")
    display_values = canonical_display_map(conn)
    cursor = conn.execute(
        """SELECT f.*, s.source_date, s.source_lineage, r.has_imaging
           FROM clinical_facts f
           JOIN clinical_sources s USING (source_id)
           JOIN clinical_rows r USING (source_row_id)
           WHERE f.qc_excluded = 0
           ORDER BY f.subject_key, f.concept, f.source_priority DESC,
                    COALESCE(s.source_date, '') DESC, f.source_id, f.fact_id"""
    )

    def write_subject(facts: list[sqlite3.Row]) -> None:
        if not facts:
            return
        subject_key = facts[0]["subject_key"]
        grouped: dict[str, list[sqlite3.Row]] = {}
        for fact in facts:
            grouped.setdefault(fact["concept"], []).append(fact)
        resolved: dict[str, str] = {}
        resolved_sources: dict[str, dict[str, Any]] = {}
        conflicts: dict[str, list[dict[str, Any]]] = {}
        for concept, concept_facts in grouped.items():
            yale_longitudinal_age = (
                normalize_name(facts[0]["short_title"])
                == "yalebrainmetslongitudinal"
                and concept == "age_at_imaging_years"
            )
            if yale_longitudinal_age:
                winner = min(
                    concept_facts,
                    key=lambda fact: (
                        fact["value_number"]
                        if fact["value_number"] is not None
                        else float("inf"),
                        fact["fact_id"],
                    ),
                )
            else:
                winner = concept_facts[0]
            resolved[concept] = display_values.get(
                (concept, winner["value_normalized"]), winner["value_resolved"]
            )
            resolved_sources[concept] = {
                "source_kind": winner["source_kind"],
                "source_id": winner["source_id"],
                "source_lineage": winner["source_lineage"],
                "priority": winner["source_priority"],
                "evidence_scope": winner["evidence_scope"],
                "is_inferred": bool(winner["is_inferred"]),
            }
            distinct: dict[str, dict[str, Any]] = {}
            for fact in concept_facts:
                distinct.setdefault(
                    fact["value_normalized"],
                    {
                        "value": fact["value_resolved"],
                        "source_value": fact["value_text"],
                        "source_kind": fact["source_kind"],
                        "source_id": fact["source_id"],
                        "source_lineage": fact["source_lineage"],
                        "priority": fact["source_priority"],
                    },
                )
            if len(distinct) > 1 and not yale_longitudinal_age:
                conflicts[concept] = list(distinct.values())
        source_kinds = sorted({fact["source_kind"] for fact in facts})
        source_lineages = {fact["source_lineage"] for fact in facts}
        has_imaging = max(fact["has_imaging"] for fact in facts)
        display_fact = sorted(
            facts,
            key=lambda fact: (
                -fact["source_priority"],
                fact["source_id"],
                fact["fact_id"],
            ),
        )[0]
        column_values = [resolved.get(column) for column in RESOLVED_COLUMNS]
        diagnosis_is_inferred = int(
            bool(
                resolved_sources.get("primary_diagnosis", {}).get("is_inferred")
            )
        )
        site_is_inferred = int(
            bool(resolved_sources.get("primary_site", {}).get("is_inferred"))
        )
        conn.execute(
            f"""INSERT INTO clinical_subjects
                (subject_key, short_title, subject_id, source_kinds, source_count,
                 conflict_count, has_imaging, {", ".join(RESOLVED_COLUMNS)},
                 primary_diagnosis_is_inferred, primary_site_is_inferred,
                 resolved_values_json, resolved_sources_json, conflicts_json)
                VALUES ({", ".join("?" for _ in range(7 + len(RESOLVED_COLUMNS) + 5))})""",
            (
                subject_key,
                display_fact["short_title"],
                display_fact["subject_id"],
                json_dumps(source_kinds),
                len(source_lineages),
                len(conflicts),
                has_imaging,
                *column_values,
                diagnosis_is_inferred,
                site_is_inferred,
                json_dumps(resolved),
                json_dumps(resolved_sources),
                json_dumps(conflicts),
            ),
        )

    current_key = ""
    current_facts: list[sqlite3.Row] = []
    for fact in cursor:
        if current_key and fact["subject_key"] != current_key:
            write_subject(current_facts)
            current_facts = []
        current_key = fact["subject_key"]
        current_facts.append(fact)
    write_subject(current_facts)


def inherit_analysis_result_clinical_facts(
    conn: sqlite3.Connection, snapshot_db: Path
) -> dict[str, int]:
    """Inherit source-Collection facts after relationship and ID validation."""
    conn.execute("DELETE FROM clinical_dataset_relationships")
    relationships = wordpress_analysis_result_relationships(snapshot_db)
    totals = {
        "relationships": len(relationships),
        "relationships_with_matches": 0,
        "target_subjects": 0,
        "matched_subjects": 0,
        "inherited_facts": 0,
    }
    for relationship in relationships:
        target = relationship["target_short_title"]
        source = relationship["source_short_title"]
        candidate_rows = conn.execute(
            """SELECT subject_id, MAX(has_imaging) AS has_imaging
               FROM (
                   SELECT subject_id, 1 AS has_imaging
                   FROM clinical_imaging_subjects
                   WHERE short_title = ?
                   UNION ALL
                   SELECT r.subject_id, r.has_imaging
                   FROM clinical_rows r
                   JOIN clinical_sources s USING (source_id)
                   WHERE r.short_title = ?
                     AND s.source_kind NOT IN
                         ('wordpress_dataset_inference',
                          'tcia_collection_subject_inheritance')
               )
               GROUP BY subject_id ORDER BY subject_id""",
            (target, target),
        ).fetchall()
        target_subjects = {
            normalize_subject(row["subject_id"]): (
                row["subject_id"],
                bool(row["has_imaging"]),
            )
            for row in candidate_rows
            if clean_value(row["subject_id"])
        }
        source_subjects = {
            normalize_subject(row["subject_id"]): row
            for row in conn.execute(
                """SELECT * FROM clinical_subjects
                   WHERE short_title = ? ORDER BY subject_id""",
                (source,),
            )
        }
        matching_ids = sorted(set(target_subjects) & set(source_subjects))
        source_id = (
            "tcia-inheritance:"
            f"{normalize_name(target)}:{normalize_name(source)}"
        )
        inherited_facts = 0
        if matching_ids:
            insert_source(
                conn,
                source_id=source_id,
                source_kind="tcia_collection_subject_inheritance",
                short_title=target,
                source_signature_value=stable_id(
                    relationship["relationship_id"],
                    relationship["relationship_evidence"],
                ),
                source_lineage=(
                    "wordpress-analysis-result-patient-crosswalk:"
                    f"{normalize_name(target)}:{normalize_name(source)}"
                ),
                source_url=relationship["target_url"],
                provenance=relationship,
            )
            for row_number, normalized_id in enumerate(matching_ids, start=1):
                target_subject_id, target_has_imaging = target_subjects[
                    normalized_id
                ]
                source_subject = source_subjects[normalized_id]
                values = json.loads(source_subject["resolved_values_json"])
                resolved_sources = json.loads(
                    source_subject["resolved_sources_json"]
                )
                for concept in sorted(INHERITABLE_PATIENT_CONCEPTS & values.keys()):
                    value = clean_value(values.get(concept))
                    if not value:
                        continue
                    original_source = resolved_sources.get(concept, {})
                    original_source_id = clean_value(
                        original_source.get("source_id")
                    )
                    original_facts = [
                        dict(row)
                        for row in conn.execute(
                            """SELECT fact_id, source_row_id, value_text,
                                      value_resolved, unit
                               FROM clinical_facts
                               WHERE subject_key = ? AND concept = ?
                                 AND source_id = ? AND qc_excluded = 0
                               ORDER BY fact_id""",
                            (
                                source_subject["subject_key"],
                                concept,
                                original_source_id,
                            ),
                        )
                    ]
                    unit = next(
                        (
                            clean_value(item["unit"])
                            for item in original_facts
                            if clean_value(item["unit"])
                        ),
                        "",
                    )
                    insert_row_and_facts(
                        conn,
                        source_id=source_id,
                        source_kind="tcia_collection_subject_inheritance",
                        short_title=target,
                        subject_id=target_subject_id,
                        table_name="wordpress.patient_id_crosswalk",
                        row_number=row_number,
                        row={
                            "target_short_title": target,
                            "target_subject_id": target_subject_id,
                            "source_short_title": source,
                            "source_subject_id": source_subject["subject_id"],
                            "concept": concept,
                            "value": value,
                        },
                        facts=[
                            (
                                concept,
                                value,
                                f"inherited_from:{source}:{concept}",
                                unit or None,
                            )
                        ],
                        has_imaging=target_has_imaging,
                        evidence_scope="patient_inherited",
                        is_inferred=bool(
                            original_source.get("is_inferred", False)
                        ),
                        fact_provenance={
                            "relationship_id": relationship["relationship_id"],
                            "relationship_source": relationship[
                                "relationship_source"
                            ],
                            "relationship_evidence": relationship[
                                "relationship_evidence"
                            ],
                            "inherited_from_short_title": source,
                            "inherited_from_subject_id": source_subject[
                                "subject_id"
                            ],
                            "original_source": original_source,
                            "original_facts": original_facts,
                        },
                    )
                    inherited_facts += 1
        status = (
            "matched"
            if matching_ids
            else (
                "no_target_subject_ids"
                if not target_subjects
                else "no_exact_patient_id_matches"
            )
        )
        conn.execute(
            """INSERT INTO clinical_dataset_relationships
               (relationship_id, target_short_title, source_short_title,
                relationship_source, relationship_evidence, target_subjects,
                matched_subjects, inherited_facts, status, provenance_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                relationship["relationship_id"],
                target,
                source,
                relationship["relationship_source"],
                relationship["relationship_evidence"],
                len(target_subjects),
                len(matching_ids),
                inherited_facts,
                status,
                json_dumps(relationship),
            ),
        )
        totals["target_subjects"] += len(target_subjects)
        totals["matched_subjects"] += len(matching_ids)
        totals["inherited_facts"] += inherited_facts
        totals["relationships_with_matches"] += int(bool(matching_ids))
    return totals


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in REQUIRED_TABLES:
        counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return counts


def write_artifacts(
    db_path: Path, gzip_path: Path | None, manifest_path: Path | None
) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    counts = table_counts(conn)
    meta = {
        row[0]: json.loads(row[1])
        for row in conn.execute("SELECT key, value FROM clinical_meta")
    }
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_iso(),
        "asset": CLINICAL_ASSET,
        "sqlite_bytes": db_path.stat().st_size,
        "sqlite_sha256": file_sha256(db_path),
        "integrity_check": integrity,
        "table_counts": counts,
        "clinical_meta": meta,
    }
    if gzip_path:
        gzip_path.parent.mkdir(parents=True, exist_ok=True)
        with db_path.open("rb") as source, gzip_path.open("wb") as raw_target:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_target, mtime=0
            ) as target:
                shutil.copyfileobj(source, target)
        manifest["gzip_bytes"] = gzip_path.stat().st_size
        manifest["gzip_sha256"] = file_sha256(gzip_path)
    fingerprint_fields = {
        "schema_version": SCHEMA_VERSION,
        "source_signature": meta.get("source_signature"),
        # clinical_meta contains build-path bookkeeping such as whether a seed
        # or previous DB was used. Exclude it so an identical source snapshot
        # has one release fingerprint after either a full build or reuse.
        "table_counts": {
            key: value for key, value in counts.items() if key != "clinical_meta"
        },
    }
    manifest["release_fingerprint"] = sha256_bytes(
        json_dumps(fingerprint_fields).encode("utf-8")
    )
    if manifest_path:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def build(args: argparse.Namespace) -> None:
    snapshot_db = Path(args.snapshot_db)
    out = Path(args.out)
    conn = init_db(out, replace=args.replace)
    allowed_short_titles = visible_tcia_short_titles(snapshot_db)
    previous_db = Path(args.previous_db) if args.previous_db else None
    idc_result = ingest_idc_clinical(
        conn,
        allowed_short_titles=allowed_short_titles,
        previous_db=previous_db,
        refresh=args.refresh_idc_clinical,
        no_fetch=args.no_fetch_idc_clinical,
        timeout=args.timeout,
        max_bytes=args.max_artifact_bytes,
    )

    downloads = clinical_downloads(snapshot_db)
    if args.limit is not None:
        downloads = downloads[: args.limit]
    previous_schema_compatible = bool(
        previous_db
        and previous_db.exists()
        and previous_meta(previous_db, "schema_version") == SCHEMA_VERSION
    )
    for row in downloads:
        download_key = row["download_id"] or stable_id(
            row["short_title"], row["download_url"]
        )
        source_id = (
            f"tcia-download:{normalize_name(row['short_title'])}:{download_key}"
        )
        signature = source_signature(row)
        status = "pending"
        loaded_rows = 0
        loaded_subjects = 0
        error_text = ""
        if (
            previous_db
            and previous_schema_compatible
            and not args.refresh_all_official
        ):
            previous = sqlite3.connect(previous_db)
            old = previous.execute(
                """SELECT source_signature, ingest_status, rows_loaded, subjects_loaded
                   FROM clinical_downloads WHERE source_id = ?""",
                (source_id,),
            ).fetchone()
            previous.close()
            if old and old[0] == signature and old[1] in {
                "loaded",
                "reused",
                "no_patient_rows",
            }:
                if copy_source_from_previous(conn, previous_db, source_id):
                    status = (
                        "reused"
                        if old[1] in {"loaded", "reused"}
                        else old[1]
                    )
                    loaded_rows = old[2]
                    loaded_subjects = old[3]
        if status == "pending":
            if row["controlled_access"]:
                status = "skipped"
                error_text = "controlled-access clinical downloads are not harvested"
            elif not row["download_url"]:
                status = "skipped"
                error_text = "download URL is blank"
            elif args.no_fetch_official:
                status = "skipped"
                error_text = "official fetch disabled"
            else:
                savepoint_active = False
                try:
                    data = fetch_url(
                        row["download_url"],
                        timeout=args.timeout,
                        max_bytes=args.max_artifact_bytes,
                    )
                    conn.execute("SAVEPOINT official_ingest")
                    savepoint_active = True
                    loaded_rows, loaded_subjects = ingest_official_bytes(
                        conn,
                        row,
                        source_id=source_id,
                        signature=signature,
                        data=data,
                    )
                    conn.execute("RELEASE SAVEPOINT official_ingest")
                    savepoint_active = False
                    status = "loaded" if loaded_rows else "no_patient_rows"
                except Exception as exc:
                    if savepoint_active:
                        conn.execute("ROLLBACK TO SAVEPOINT official_ingest")
                        conn.execute("RELEASE SAVEPOINT official_ingest")
                    status = "failed"
                    error_text = str(exc)
                    warning(
                        conn,
                        "official_download_failed",
                        error_text,
                        source_id=source_id,
                        short_title=row["short_title"],
                    )
        conn.execute(
            """INSERT INTO clinical_downloads
               (source_id, short_title, dataset_type, dataset_title, download_id,
                download_title, download_url, date_updated, file_types,
                download_types, data_types, access_level, controlled_access,
                source_signature, ingest_status, rows_loaded, subjects_loaded,
                error_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                row["short_title"],
                row["dataset_type"],
                row["title"],
                row["download_id"],
                row["download_title"],
                row["download_url"],
                row["date_updated"],
                row["file_types"],
                row["download_types"],
                row["data_types"],
                row["access_level"],
                row["controlled_access"],
                signature,
                status,
                loaded_rows,
                loaded_subjects,
                error_text,
            ),
        )
        conn.commit()

    ivygap_result = ingest_ivygap_external_clinical(
        conn,
        snapshot_db,
        no_fetch=args.no_fetch_official,
        timeout=args.timeout,
        max_bytes=args.max_artifact_bytes,
    )
    insert_meta(conn, "ivygap_allen_clinical_result", ivygap_result)
    conn.commit()

    # Import secondary sources only after official/IDC rows establish the
    # TCIA subject allowlist.
    if args.legacy_seed_db:
        seed_counts = import_legacy_seed(
            conn,
            Path(args.legacy_seed_db),
            allowed_short_titles,
        )
        insert_meta(conn, "legacy_seed_counts", seed_counts)
    elif previous_db and previous_db.exists():
        copied = copy_nonofficial_previous(
            conn,
            previous_db,
            allowed_short_titles,
        )
        insert_meta(conn, "previous_nonofficial_sources_reused", copied)

    conn.execute(
        """UPDATE clinical_rows
           SET has_imaging = 1
           WHERE subject_key IN (
               SELECT subject_key FROM clinical_imaging_subjects
           )"""
    )
    ct_colonography_result = derive_ct_colonography_patient_diagnoses(conn)
    insert_meta(
        conn,
        "ct_colonography_histology_result",
        ct_colonography_result,
    )
    ea1141_result = derive_ea1141_patient_diagnoses(conn)
    insert_meta(
        conn,
        "ea1141_screening_pathology_result",
        ea1141_result,
    )
    hnscc_result = promote_hnscc_official_cohort(conn)
    insert_meta(
        conn,
        "hnscc_official_cohort_result",
        hnscc_result,
    )
    hungarian_colorectal_result = (
        promote_and_audit_hungarian_colorectal_cohort(conn, snapshot_db)
    )
    insert_meta(
        conn,
        "hungarian_colorectal_icd10_result",
        hungarian_colorectal_result,
    )
    reviewed_id_cohort_result = promote_reviewed_official_id_cohorts(
        conn, snapshot_db
    )
    insert_meta(
        conn,
        "reviewed_official_id_cohort_result",
        reviewed_id_cohort_result,
    )
    tompei_cmmd_result = promote_tompei_cmmd_package_cohort(
        conn,
        snapshot_db,
        no_fetch=args.no_fetch_official,
        timeout=args.timeout,
        max_bytes=args.max_artifact_bytes,
    )
    insert_meta(conn, "tompei_cmmd_package_cohort_result", tompei_cmmd_result)
    inference_result = apply_wordpress_dataset_inferences(conn, snapshot_db)
    # Resolve source Collection subjects first, then use WordPress-confirmed
    # relationships plus exact PatientID matches to backfill Analysis Results.
    materialize_clinical_qc(conn)
    materialize_subjects(conn)
    inheritance_result = inherit_analysis_result_clinical_facts(
        conn, snapshot_db
    )
    qc_result = materialize_clinical_qc(conn)
    materialize_subjects(conn)
    source_fingerprints = [
        tuple(row)
        for row in conn.execute(
            """SELECT source_id, source_signature, artifact_sha256
               FROM clinical_sources ORDER BY source_id"""
        )
    ]
    download_fingerprints = [
        tuple(row)
        for row in conn.execute(
            """SELECT source_id, source_signature,
                      CASE WHEN ingest_status = 'reused'
                           THEN 'loaded' ELSE ingest_status END,
                      rows_loaded,
                      subjects_loaded, error_text
               FROM clinical_downloads ORDER BY source_id"""
        )
    ]
    inference_fingerprints = [
        tuple(row)
        for row in conn.execute(
            """SELECT short_title, concept, raw_value, inferred_value,
                      eligible, eligibility_reason, review_required,
                      review_reason, review_evidence, screening_signal,
                      candidate_subjects, subjects_applied,
                      subjects_suppressed
               FROM clinical_dataset_inferences
               ORDER BY short_title, concept"""
        )
    ]
    insert_meta(conn, "schema_version", SCHEMA_VERSION)
    insert_meta(conn, "created_at", now_iso())
    insert_meta(
        conn,
        "source_precedence",
        [
            "tcia_clinical_download",
            "tcia_linked_external_clinical",
            "idc_clinical",
            "cda",
            "dicom",
            "tcia_collection_subject_inheritance",
            "wordpress_dataset_inference",
        ],
    )
    insert_meta(conn, "idc_version", idc_result.get("idc_version", ""))
    insert_meta(conn, "idc_clinical_result", idc_result)
    insert_meta(conn, "wordpress_dataset_inference_result", inference_result)
    insert_meta(conn, "analysis_result_clinical_inheritance", inheritance_result)
    insert_meta(conn, "clinical_qc_result", qc_result)
    insert_meta(
        conn,
        "source_signature",
        stable_id(
            json_dumps(
                [
                    source_fingerprints,
                    download_fingerprints,
                    inference_fingerprints,
                ]
            )
        ),
    )
    insert_meta(conn, "snapshot_db", str(snapshot_db))
    insert_meta(conn, "visible_tcia_short_title_count", len(allowed_short_titles))
    insert_meta(conn, "clinical_download_candidates", len(downloads))
    insert_meta(
        conn,
        "scope_note",
        (
            "Direct official TCIA artifacts override TCIA-linked external "
            "clinical sources, followed by IDC-normalized clinical tables, "
            "CDA, and DICOM. IDC and direct TCIA artifacts share one official-"
            "data lineage. Raw values and conflicts remain available. When no "
            "direct Analysis Result fact exists, a WordPress-confirmed source "
            "Collection relationship plus exact PatientID match can supply a "
            "lower-priority inherited patient fact with crosswalk provenance. "
            "Program-wide related lists alone never authorize inheritance. "
            "When no "
            "patient-specific diagnosis or site exists, one non-generic "
            "Collection-level WordPress label is exposed as an explicit "
            "dataset-scope inference. Collections with screen* text, one "
            "diagnosis label, and no non-cancer label are held for review "
            "instead of inferred unless an evidence-backed curator resolution "
            "is recorded. "
            "agent_clinical_subjects exposes only subjects linked to imaging."
        ),
    )
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    manifest = write_artifacts(
        out,
        Path(args.gzip_out) if args.gzip_out else None,
        Path(args.manifest_out) if args.manifest_out else None,
    )
    print(json.dumps(manifest, indent=2))


def validate(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    objects = {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    missing_tables = [name for name in REQUIRED_TABLES if (name, "table") not in objects]
    missing_views = [name for name in REQUIRED_VIEWS if (name, "view") not in objects]
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    counts = table_counts(conn) if not missing_tables else {}
    precedence = conn.execute(
        """SELECT source_kind, MIN(source_priority), MAX(source_priority)
           FROM clinical_sources GROUP BY source_kind"""
    ).fetchall() if not missing_tables else []
    qc_counts = {}
    semantic_errors = 0
    relationship_errors = 0
    bcbm_counts: dict[str, int] = {}
    brain_tr_gammaknife_counts: dict[str, int] = {}
    remind_counts: dict[str, int] = {}
    if not missing_tables:
        qc_counts = {
            row[0]: row[1]
            for row in conn.execute(
                """SELECT disposition, COUNT(*)
                   FROM clinical_qc_findings GROUP BY disposition"""
            )
        }
        semantic_errors = conn.execute(
            """SELECT COUNT(*) FROM clinical_subjects
               WHERE CAST(age_at_diagnosis AS REAL) > 120
                  OR CAST(age_at_diagnosis AS REAL) < 0
                  OR CAST(age_at_enrollment_years AS REAL) > 120
                  OR CAST(age_at_enrollment_years AS REAL) < 0
                  OR CAST(age_at_imaging_years AS REAL) > 120
                  OR CAST(age_at_imaging_years AS REAL) < 0
                  OR CAST(age_at_treatment_years AS REAL) > 120
                  OR CAST(age_at_treatment_years AS REAL) < 0
                  OR (lower(replace(short_title, '-', '')) = 'headneckpetct'
                      AND lower(trim(subject_id)) = 'explanations')"""
        ).fetchone()[0]
        relationship_errors = conn.execute(
            """SELECT COUNT(*) FROM clinical_dataset_relationships
               WHERE matched_subjects > target_subjects
                  OR (matched_subjects = 0 AND inherited_facts > 0)
                  OR (status = 'matched' AND matched_subjects = 0)
                  OR (status <> 'matched' AND inherited_facts > 0)"""
        ).fetchone()[0]
        bcbm_counts = {
            "subjects": conn.execute(
                "SELECT COUNT(DISTINCT subject_id) FROM clinical_rows WHERE short_title = 'BCBM-RadioGenomics'"
            ).fetchone()[0],
            "clinical_scan_observations": conn.execute(
                """SELECT COUNT(*) FROM clinical_longitudinal_observations
                   WHERE short_title = 'BCBM-RadioGenomics'
                     AND observation_type = 'scanner_clinical_scan'"""
            ).fetchone()[0],
            "radiomics_observations": conn.execute(
                """SELECT COUNT(*) FROM clinical_longitudinal_observations
                   WHERE short_title = 'BCBM-RadioGenomics'
                     AND observation_type = 'segmentation_radiomics'"""
            ).fetchone()[0],
        }
        if any(bcbm_counts.values()) and bcbm_counts != {
            "subjects": 165,
            "clinical_scan_observations": 268,
            "radiomics_observations": 2825,
        }:
            semantic_errors += 1
        brain_tr_gammaknife_counts = {
            "subjects": conn.execute(
                "SELECT COUNT(DISTINCT subject_id) FROM clinical_rows WHERE short_title = 'Brain-TR-GammaKnife' AND source_id LIKE 'tcia-download:%'"
            ).fetchone()[0],
            "course_observations": conn.execute(
                """SELECT COUNT(*) FROM clinical_longitudinal_observations
                   WHERE short_title = 'Brain-TR-GammaKnife'
                     AND observation_type = 'radiotherapy_course'"""
            ).fetchone()[0],
            "lesion_observations": conn.execute(
                """SELECT COUNT(*) FROM clinical_longitudinal_observations
                   WHERE short_title = 'Brain-TR-GammaKnife'
                     AND observation_type = 'lesion_followup_outcome'"""
            ).fetchone()[0],
        }
        if any(brain_tr_gammaknife_counts.values()) and brain_tr_gammaknife_counts != {
            "subjects": 47,
            "course_observations": 76,
            "lesion_observations": 244,
        }:
            semantic_errors += 1
        remind_counts = {
            "official_subjects": conn.execute(
                "SELECT COUNT(DISTINCT subject_id) FROM clinical_rows WHERE short_title = 'ReMIND' AND source_id LIKE 'tcia-download:%'"
            ).fetchone()[0],
            "dictionary_fields": conn.execute(
                "SELECT COUNT(*) FROM clinical_source_dictionary WHERE short_title = 'ReMIND'"
            ).fetchone()[0],
        }
        if any(remind_counts.values()) and remind_counts != {
            "official_subjects": 114,
            "dictionary_fields": 32,
        }:
            semantic_errors += 1
    conn.close()
    result = {
        "ok": (
            not missing_tables
            and not missing_views
            and integrity == "ok"
            and semantic_errors == 0
            and relationship_errors == 0
        ),
        "integrity_check": integrity,
        "missing_tables": missing_tables,
        "missing_views": missing_views,
        "table_counts": counts,
        "source_priorities": precedence,
        "qc_finding_counts": qc_counts,
        "semantic_errors": semantic_errors,
        "relationship_errors": relationship_errors,
        "bcbm_counts": bcbm_counts,
        "brain_tr_gammaknife_counts": brain_tr_gammaknife_counts,
        "remind_counts": remind_counts,
    }
    if not result["ok"]:
        raise RuntimeError(json.dumps(result, indent=2))
    return result


def export_qc_findings(
    db_path: Path,
    out_path: Path,
    *,
    include_all: bool = False,
    short_title: str = "",
) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    clauses = []
    parameters: list[Any] = []
    if not include_all:
        clauses.append("disposition = 'manual_review'")
    if short_title:
        clauses.append("lower(short_title) = lower(?)")
        parameters.append(short_title)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""SELECT rule_id, severity, disposition, review_status, short_title,
                   subject_id, concept, original_value, resolved_value, unit,
                   message, source_id, source_row_id, fact_id, provenance_json
            FROM agent_clinical_qc_findings
            {where}
            ORDER BY severity DESC, short_title, subject_id, rule_id""",
        parameters,
    ).fetchall()
    conn.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0].keys()) if rows else [
        "rule_id",
        "severity",
        "disposition",
        "review_status",
        "short_title",
        "subject_id",
        "concept",
        "original_value",
        "resolved_value",
        "unit",
        "message",
        "source_id",
        "source_row_id",
        "fact_id",
        "provenance_json",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)
    return {
        "path": str(out_path.resolve()),
        "rows": len(rows),
        "scope": "all_findings" if include_all else "manual_review",
        "short_title": short_title or None,
    }


def release_url(repo: str, tag: str, asset: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{asset}"


def download_json(url: str) -> dict[str, Any]:
    return json.loads(fetch_url(url, timeout=60, max_bytes=10 * 1024 * 1024))


def ensure(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    manifest_path = Path(args.manifest)
    remote_manifest = download_json(
        release_url(args.repo, args.tag, CLINICAL_MANIFEST_ASSET)
    )
    if (
        db_path.exists()
        and manifest_path.exists()
        and file_sha256(db_path) == remote_manifest.get("sqlite_sha256")
    ):
        print(f"Clinical metadata is current: {db_path}")
        return
    compressed = fetch_url(
        release_url(args.repo, args.tag, CLINICAL_ASSET),
        timeout=300,
        max_bytes=args.max_download_bytes,
    )
    if sha256_bytes(compressed) != remote_manifest.get("gzip_sha256"):
        raise RuntimeError("Downloaded clinical SQLite gzip hash does not match manifest")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=db_path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(gzip.decompress(compressed))
    if file_sha256(temp_path) != remote_manifest.get("sqlite_sha256"):
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded clinical SQLite hash does not match manifest")
    os.replace(temp_path, db_path)
    manifest_path.write_text(json.dumps(remote_manifest, indent=2) + "\n")
    print(f"Installed clinical metadata: {db_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--snapshot-db", default=str(DEFAULT_SNAPSHOT_DB))
    build_parser.add_argument("--out", default=str(DEFAULT_DB_PATH))
    build_parser.add_argument("--gzip-out")
    build_parser.add_argument("--manifest-out")
    build_parser.add_argument("--previous-db")
    build_parser.add_argument(
        "--legacy-seed-db",
        help="One-time import from the experimental clinical_cda_metadata SQLite.",
    )
    build_parser.add_argument("--no-fetch-official", action="store_true")
    build_parser.add_argument("--refresh-all-official", action="store_true")
    build_parser.add_argument(
        "--no-fetch-idc-clinical",
        action="store_true",
        help="Skip IDC clinical ingestion (intended for isolated tests only).",
    )
    build_parser.add_argument(
        "--refresh-idc-clinical",
        action="store_true",
        help="Re-fetch IDC clinical tables even when the IDC version is unchanged.",
    )
    build_parser.add_argument("--limit", type=int)
    build_parser.add_argument("--timeout", type=int, default=120)
    build_parser.add_argument(
        "--max-artifact-bytes", type=int, default=256 * 1024 * 1024
    )
    build_parser.add_argument("--replace", action="store_true")

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--db", default=str(DEFAULT_DB_PATH))

    info_parser = subparsers.add_parser("info")
    info_parser.add_argument("--db", default=str(DEFAULT_DB_PATH))

    qc_parser = subparsers.add_parser(
        "export-qc", help="Export clinical QC findings to CSV."
    )
    qc_parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    qc_parser.add_argument("--out", required=True)
    qc_parser.add_argument(
        "--all",
        action="store_true",
        help="Include accepted automatic findings as well as manual-review rows.",
    )
    qc_parser.add_argument(
        "--collection",
        help="Limit the export to one exact TCIA Collection short title.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            build(args)
        elif args.command == "validate":
            print(json.dumps(validate(Path(args.db)), indent=2))
        elif args.command == "info":
            conn = sqlite3.connect(args.db)
            result = {
                "path": str(Path(args.db).resolve()),
                "bytes": Path(args.db).stat().st_size,
                "table_counts": table_counts(conn),
                "meta": {
                    row[0]: json.loads(row[1])
                    for row in conn.execute("SELECT key, value FROM clinical_meta")
                },
            }
            conn.close()
            print(json.dumps(result, indent=2))
        elif args.command == "export-qc":
            print(
                json.dumps(
                    export_qc_findings(
                        Path(args.db),
                        Path(args.out),
                        include_all=args.all,
                        short_title=args.collection or "",
                    ),
                    indent=2,
                )
            )
    except (RuntimeError, sqlite3.Error, urllib.error.URLError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
