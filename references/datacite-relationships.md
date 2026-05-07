# DataCite Relationships

Use DataCite snapshot records first for DOI metadata, citations, and versions. Use WordPress snapshot records afterward to confirm TCIA publication status, hidden/visible status, access/license, and user-facing dataset pages.

## TCIA DOI Metadata

TCIA mints dataset DOIs through DataCite. The SQLite snapshot stores DataCite records under the TCIA DOI prefix `10.7937` in `agent_datacite_dois`.

DataCite records include DOI, title, publisher, publication year, URL, version, rights, identifiers such as `TCIA Short Name`, and related identifiers. Use WordPress snapshot records after DataCite when the answer needs TCIA page visibility, hidden-record filtering, access/license status, or download routing.

Bundled helper:

```bash
python scripts/datacite_tcia_dois.py --limit 25
python scripts/datacite_tcia_dois.py --query "breast mri" --json
python scripts/datacite_tcia_dois.py --doi 10.7937/4qad-4280 --json
```

## Derived Records

External records may declare that they are derived from a TCIA DOI. For example, a Zenodo DOI can include `Related Works` metadata with a relation such as `IsDerivedFrom` pointing to a TCIA DOI. Treat live relationship searches as explicit DOI-provenance research, not as routine end-user dataset discovery or download routing.

Interpretation:

- If the derived record is listed in WordPress as a Collection or Analysis Result, it is TCIA-published.
- If it is not listed in WordPress, it is externally published but related to TCIA.
- Mention external derived records in a separate "Related derived data" section when helpful.

## Query Pattern

DataCite supports querying works by related identifier. For normal DOI/citation questions, use the snapshot. Use `scripts/datacite_related.py <doi>` only when the user explicitly asks for current external related DOI records beyond the snapshot.

```text
relatedIdentifiers.relatedIdentifierType:DOI AND
relatedIdentifiers.relatedIdentifier:<TCIA_DOI> AND
relatedIdentifiers.relationType:IsDerivedFrom
```

## Response Guidance

When discussing related DOI records, include:

- Source TCIA DOI.
- Related DOI and title.
- Relation type, such as `IsDerivedFrom`.
- Publisher or repository, such as Zenodo, if available.
- Clear provenance wording: "external derived record" vs. "TCIA-published Analysis Result".
- Whether WordPress confirms the record is a visible TCIA Collection or Analysis Result.
