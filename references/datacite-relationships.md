# DataCite Relationships

Use DataCite for DOI metadata, citations, and relationships between TCIA datasets and derived records.

## TCIA DOI Metadata

TCIA mints dataset DOIs through DataCite. The `tcia_utils.datacite` helper can retrieve TCIA DOI records:

```python
from tcia_utils import datacite

doi_records = datacite.getDoi()
```

Use WordPress citation and dataset pages as the first source for user-facing citation guidance. Use DataCite to inspect DOI metadata, related identifiers, and version information.

## Derived Records

External records may declare that they are derived from a TCIA DOI. For example, a Zenodo DOI can include `Related Works` metadata with a relation such as `IsDerivedFrom` pointing to a TCIA DOI.

Interpretation:

- If the derived record is listed in WordPress as a Collection or Analysis Result, it is TCIA-published.
- If it is not listed in WordPress, it is externally published but related to TCIA.
- Mention external derived records in a separate "Related derived data" section when helpful.

## Query Pattern

DataCite supports querying works by related identifier:

```text
relatedIdentifiers.relatedIdentifierType:DOI AND
relatedIdentifiers.relatedIdentifier:<TCIA_DOI> AND
relatedIdentifiers.relationType:IsDerivedFrom
```

Use `scripts/datacite_related.py <doi>` for a standard-library helper, or use:

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
