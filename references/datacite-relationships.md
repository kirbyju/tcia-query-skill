# DataCite Relationships

Use DataCite first for DOI metadata, citations, versions, and relationships between TCIA datasets and derived records. Use WordPress afterward to confirm TCIA publication status, hidden/visible status, access/license, and user-facing dataset pages.

## TCIA DOI Metadata

TCIA mints dataset DOIs through DataCite. The `tcia_utils.datacite` helper can retrieve TCIA DOI records:

```python
from tcia_utils import datacite

doi_records = datacite.getDoi()
```

If `tcia_utils` is installed, use `tcia_utils.datacite` before custom code:

```python
from tcia_utils import datacite

doi_records = datacite.getDoi()
derived = datacite.getDerivedDois("10.7937/TCIA.HMQ8-J677", format="df")
```

If `tcia_utils` is unavailable, call the DataCite REST API as the starting point:

```text
https://api.datacite.org/dois?prefix=10.7937
https://api.datacite.org/dois/<DOI>
```

DataCite records include DOI, title, publisher, publication year, URL, version, rights, identifiers such as `TCIA Short Name`, and related identifiers. Use WordPress after DataCite when the answer needs TCIA page visibility, hidden-record filtering, access/license status, or download routing.

Bundled helper:

```bash
python scripts/datacite_tcia_dois.py --limit 25
python scripts/datacite_tcia_dois.py --query "breast mri" --json
python scripts/datacite_tcia_dois.py --doi 10.7937/4qad-4280 --json
```

## Derived Records

External records may declare that they are derived from a TCIA DOI. For example, a Zenodo DOI can include `Related Works` metadata with a relation such as `IsDerivedFrom` pointing to a TCIA DOI.

Interpretation:

- If the derived record is listed in WordPress as a Collection or Analysis Result, it is TCIA-published.
- If it is not listed in WordPress, it is externally published but related to TCIA.
- Mention external derived records in a separate "Related derived data" section when helpful.

## Query Pattern

DataCite supports querying works by related identifier. Use `tcia_utils.datacite.getDerivedDois()` when installed, or `scripts/datacite_related.py <doi>` as a standard-library fallback.

```text
relatedIdentifiers.relatedIdentifierType:DOI AND
relatedIdentifiers.relatedIdentifier:<TCIA_DOI> AND
relatedIdentifiers.relationType:IsDerivedFrom
```

Example `tcia_utils` call:

```python
from tcia_utils import datacite

derived = datacite.getDerivedDois("10.7937/TCIA.HMQ8-J677", format="df")
```

## Response Guidance

When discussing related DOI records, include:

- Source TCIA DOI.
- Related DOI and title.
- Relation type, such as `IsDerivedFrom`.
- Publisher or repository, such as Zenodo, if available.
- Clear provenance wording: "external derived record" vs. "TCIA-published Analysis Result".
- Whether WordPress confirms the record is a visible TCIA Collection or Analysis Result.
