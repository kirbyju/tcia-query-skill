# Cancer Data Aggregator

Use CDA as a subject-level enrichment layer after TCIA provenance is established through WordPress and, for imaging cohorts, after subject identifiers are validated through TCIA/IDC metadata.

Primary docs:

- cdapython docs: `https://cda.readthedocs.io/en/latest/documentation/cdapython/man_pages/`
- CDA subject-list vignette: `https://cda.readthedocs.io/en/latest/documentation/cdapython/vignettes/008_multidc-from-file/`
- CDA data releases: `https://cda.readthedocs.io/en/latest/release_notes/data_updates/`
- PyPI package: `https://pypi.org/project/cdapython/`

## When To Use CDA

Use CDA for questions like:

- Do TCIA/IDC subjects also have GDC, PDC, General Commons, or other CRDC data?
- What harmonized demographics, diagnoses, observation ages, treatments, anatomic sites, or mortality fields are available for these subjects?
- What file categories, formats, data sources, and open/controlled file counts exist for a TCIA-derived subject cohort?
- Which upstream identifiers should the user take to GDC, PDC, IDC, or GC?

Do not use CDA as the authority for TCIA publication, TCIA dataset visibility, official TCIA clinical-spreadsheet completeness, TCIA DOI/citation, or TCIA license status. WordPress remains authoritative for those. CDA is harmonized discovery metadata; source commons remain authoritative for their files and access controls.

## Setup

Do not assume `cdapython` is installed:

```bash
python -c "import importlib.util as u; print(u.find_spec('cdapython') is not None)"
```

Ask before installing packages:

```bash
python -m pip install --upgrade cdapython
```

Prefer `cdapython` over direct `cda-client` imports or handwritten CDA REST calls. `cda-client` is the generated REST client dependency used by `cdapython`, and is not intended as the normal user-facing interface.

```python
from cdapython import (
    tables,
    columns,
    column_values,
    summarize_subjects,
    summarize_files,
    get_subject_data,
    get_file_data,
)
```

Useful discovery calls:

```python
tables()
columns(table="subject")
columns(table="upstream_identifiers")
columns(description="diagnosis")
column_values("anatomic_site")
```

Use `columns()` before relying on a specific column name. CDA's docs list searchable tables including `subject`, `file`, `observation`, `treatment`, `mutation`, `project`, and `upstream_identifiers`.

## TCIA Cohort Workflow

1. Confirm the dataset is a visible TCIA Collection or Analysis Result in WordPress.
2. Decide whether CDA is answering a real enrichment question. Use WordPress downloads for official TCIA clinical spreadsheets; use CDA for harmonized cross-commons discovery and summaries.
3. Obtain subject identifiers from IDC/TCIA metadata. For public DICOM, IDC patient/subject metadata is usually the best starting point after WordPress provenance is confirmed.
4. Validate identifier shape. CDA `subject_id` values may include source/project prefixes, such as `TCGA.TCGA-04-1369`; raw DICOM `PatientID` values may not match directly.
5. For many subjects, write a TSV with one identifier column and use `match_from_file`. For a few known CDA subject IDs, use `match_all` or `match_any`.
6. Summarize before fetching rows. Use `summarize_subjects()` for demographics/diagnosis-style counts and `summarize_files()` for data source, format, category, and access counts.
7. Fetch row-level enrichment with `get_subject_data()` only for the filtered cohort.

Example for a subject TSV:

```python
from cdapython import get_subject_data, summarize_files, summarize_subjects

match = {
    "input_file": "subjects.tsv",
    "input_column": "subject",
    "cda_column_to_match": "subject_id",
}

subject_summary = summarize_subjects(match_from_file=match, return_data_as="dict")
file_summary = summarize_files(match_from_file=match, return_data_as="dict")

subjects = get_subject_data(
    match_from_file=match,
    add_columns="upstream_identifiers.*",
    include_external_refs=True,
)
```

Example for a known CDA subject:

```python
subject = get_subject_data(
    match_all="subject_id = TCGA.TCGA-04-1369",
    add_columns=["upstream_identifiers.*", "observation.*"],
    collate_results=True,
    include_external_refs=True,
)
```

Use `collate_results=True` when adding whole related tables such as `observation.*` or `treatment.*`; otherwise related values may be flattened into lists of unique values.

## Identifier Guidance

Prefer exact matches against validated CDA identifiers. Avoid broad wildcard matching on subject IDs because it can silently overmatch related but distinct participants.

If a TCIA/IDC subject list does not match CDA `subject_id` directly:

1. Inspect `columns(table="upstream_identifiers")`.
2. Look for source-specific identifier columns that match the known project/source.
3. Use `match_from_file` with `cda_column_to_match` set to the relevant upstream identifier column.
4. Return both the CDA `subject_id` and source-specific upstream IDs so the user can route follow-up work.

For TCGA, CPTAC, and other major NCI programs, CDA may have multiple source systems linked to the same subject. Use `data_source` filters only when the user asks for a specific commons; otherwise keep all data sources and report the observed combinations.

Valid `data_source` filters documented by `cdapython` include `GDC`, `IDC`, `PDC`, `GC`, and `ICDC`.

## Access And Caveats

CDA file summaries can include `access` counts such as open or controlled. Treat these as discovery metadata only:

- Open IDC files route through IDC/idc-index after TCIA provenance checks.
- GDC controlled data require GDC/dbGaP authorization.
- General Commons controlled data require the appropriate GC/dbGaP/DAC authorization route.
- PDC data are generally open, but still route users to PDC for authoritative file access and attribution.

When current availability matters, check CDA data release notes before giving freshness-sensitive claims. CDA may lag upstream commons because it indexes periodic extracts from GDC, PDC, IDC, GC, and ICDC.

Avoid the `upstream_source` field until CDA release notes indicate its known linking issue has been fixed. Prefer `data_source` summaries and `upstream_identifiers.*`.

## Response Pattern

For CDA enrichment answers, report:

| Field | Notes |
| --- | --- |
| TCIA dataset | WordPress title and short title |
| Subject basis | How subject IDs were obtained and matched |
| CDA match rate | Matched subjects over submitted subjects |
| Added context | Demographics, diagnosis, treatment, observation, or file summaries |
| Cross-commons availability | IDC/GDC/PDC/GC/ICDC combinations and upstream identifiers |
| Access caveats | Open/controlled counts and source-system access guidance |
| Provenance caveat | CDA enriches; WordPress/source commons remain authoritative |
