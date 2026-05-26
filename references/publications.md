# TCIA Publications

Use this reference when a user asks about peer-reviewed manuscripts, papers, research hypotheses, methods, citation lists, publication impact, or downstream scientific uses of TCIA datasets.

## Authority

TCIA's verified bibliography of manuscripts written about TCIA data is the Publications page and its EndNote XML export:

- `https://www.cancerimagingarchive.net/publications/`
- `https://cancerimagingarchive.net/endnote/Pubs_basedon_TCIA.xml`

Use the EndNote XML as the source of truth for publication-mining tasks. The local SQLite snapshot and DataCite tables are useful for dataset metadata, dataset DOI metadata, visibility, access/license, and download routing, but they are not the verified manuscript bibliography.

## When To Use This Source

Start from the EndNote XML when the user asks for:

- papers or manuscripts written about TCIA data
- publications using a specific TCIA collection, DOI, or short title
- hypotheses or methods studied in TCIA-based papers
- publication counts, years, journals, PMIDs, or manuscript DOIs
- literature mining by topic, modality, disease, biomarker, model type, or endpoint

Start from DataCite only when the user asks about TCIA dataset DOI metadata, versions, citation metadata for a dataset DOI, or DOI relationship provenance. After finding dataset DOIs in the EndNote XML, use WordPress snapshot records if you need TCIA page links, short titles, access/license status, download routes, or hidden-record filtering.

## Helper Script

Prefer the bundled parser:

```bash
python scripts/tcia_publications.py --fetch --query radiogenomics --limit 20
python scripts/tcia_publications.py --dataset-doi 10.7937/K9/TCIA.2016.RNYFUYE9 --json
python scripts/tcia_publications.py --query "proteomic CT ovarian" --from-year 2018 --json
```

The script uses Python's standard library, caches the EndNote XML at `cache/Pubs_basedon_TCIA.xml`, and extracts common EndNote fields:

- record number
- title
- authors
- journal
- year
- manuscript DOI
- PMID/accession number
- keywords
- abstract
- notes
- linked TCIA dataset DOI values from `remote-database-name`

If a user needs the freshest publication list, run with `--fetch` before searching. If network access is unavailable, use a previously cached XML file with `--xml`.

## EndNote Field Notes

The EndNote XML is dense and may be serialized onto one long line. Use an XML parser, not line-oriented text parsing.

Important fields:

| EndNote field | Meaning |
| --- | --- |
| `titles/title` | manuscript title |
| `titles/secondary-title` or `periodical/full-title` | journal or venue |
| `contributors/authors/author` | author list |
| `dates/year` | publication year |
| `keywords/keyword` | TCIA collection labels, methods, disease terms, modalities, and task labels |
| `abstract` | abstract text, when available |
| `accession-num` | usually PMID, but occasionally another identifier |
| `electronic-resource-num` | manuscript DOI |
| `remote-database-name` | linked TCIA dataset DOI or multiple TCIA dataset DOIs |

Do not assume every keyword is a TCIA short title. Some keywords are methods, diseases, modalities, or broad task labels. For dataset matching, prefer TCIA DOIs in `remote-database-name`; then map those DOIs to WordPress/DataCite metadata when needed.

## Answering Literature-Mining Questions

For broad mining questions, return a compact ranked table and explicitly separate:

- strong matches where the abstract/title makes the requested multimodal hypothesis clear
- plausible matches where keywords or linked datasets suggest relevance but the abstract needs manual review
- exclusions such as radiology-only AI papers, pathology-only papers, or dataset descriptors without an explicit hypothesis

For multimodal questions, treat radiology plus clinical outcomes as a weaker category unless the user asks for clinical-only multimodal work. For "radiology plus other data types" prioritize papers combining radiology with pathology, histopathology, genomics, transcriptomics, proteomics, molecular markers, pathology slides, lab biomarkers, or other non-radiology assays.

Include manuscript DOI/PMID when available and include linked TCIA dataset DOI values when they help connect the paper back to TCIA dataset records.
