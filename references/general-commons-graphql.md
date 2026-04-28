# General Commons GraphQL

Use this reference for controlled-access TCIA face datasets in General Commons.

## Constants

- Endpoint: `https://general.datacommons.cancer.gov/v1/graphql/`
- TCIA phs accession: `phs004225`
- Match field: General Commons `study_acronym` should match TCIA WordPress `collection_short_title` or `result_short_title`.

## Pagination

General Commons data type queries require pagination:

- `first`: number of records to return, maximum 10000, default 10.
- `offset`: number of records to skip, default 0.

When retrieving many records, loop until a page returns fewer than `first` records.

## Study Discovery

Use `studies` to identify TCIA child study acronyms:

```graphql
query TCIAStudies($phs: [String], $first: Int, $offset: Int) {
  studies(phs_accessions: $phs, first: $first, offset: $offset) {
    phs_accession
    study_acronym
    study_name
  }
}
```

Variables:

```json
{
  "phs": ["phs004225"],
  "first": 10000,
  "offset": 0
}
```

If a field is not accepted by the current schema, introspect the `Study` type and select only available fields.

## Count Queries

Most count queries require `phs_accession: "phs004225"`.

Useful query names from the current GC API guide:

- `participantsCount`
- `samplesCount`
- `filesCount`
- `diagnosesCount`
- `treatmentsCount`
- `imagesCount`
- `genomicInfoCount`
- `proteomicsCount`
- `pdxCount`
- `multiplexMicroscopiesCount`
- `nonDICOMCTimagesCount`
- `nonDICOMMRimagesCount`
- `nonDICOMPETimagesCount`
- `nonDICOMpathologyImagesCount`
- `nonDICOMradiologyAllModalitiesCount`

Example:

```graphql
query TCIACounts($phs: String!) {
  participants: participantsCount(phs_accession: $phs)
  files: filesCount(phs_accession: $phs)
  images: imagesCount(phs_accession: $phs)
  nonDICOMRadiology: nonDICOMradiologyAllModalitiesCount(phs_accession: $phs)
}
```

Variables:

```json
{"phs": "phs004225"}
```

## Data Type Queries

The GC API exposes data type queries including:

- `participants`
- `diagnoses`
- `treatments`
- `samples`
- `files`
- `genomic_info`
- `images`
- `multiplex_microscopies`
- `non_dicomct_images`
- `non_dicommr_images`
- `non_dicompet_images`
- `non_dicom_pathology_images`
- `non_dicom_radiology_all_modalities`
- `proteomics`
- `pdx`

Most require `phs_accession: "phs004225"`. Some support additional filters, such as file IDs or participant IDs.

## Access Guidance

General Commons hosts both open and controlled-access data. For TCIA face datasets, focus on metadata discovery and direct users to dbGaP/DAC authorization and SB-CGC access where controlled access applies. Do not claim that controlled files can be downloaded without authorization.
