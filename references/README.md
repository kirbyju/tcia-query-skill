# Reference Directory Guide

This directory contains three different kinds of material. Do not load it all
for every question; start with the routing table in `../SKILL.md` and open only
the reference needed for the task.

## Agent Guidance

These files explain how to answer a particular class of question:

| Topic | Reference |
| --- | --- |
| Source and access routing | `routing.md` |
| Browser-only and web-search agents | `web-browser-llms.md` |
| Dataset DOI relationships | `datacite-doi-relationships.md` |
| Publications about TCIA data | `publications.md` |
| Public DICOM discovery, preview, and access | `idc-public-dicom.md` |
| NBIA fallback and legacy `.tcia` manifests | `nbia-public-dicom-fallback.md` |
| NIfTI and public non-DICOM | `nifti.md` |
| Pathology, PathDB, and Aspera | `pathology.md`, `pathdb-public-pathology.md`, `aspera.md` |
| Clinical and controlled access | `clinical.md`, `controlled-access.md` |
| CDA and General Commons enrichment | `cda.md`, `general-commons-graphql.md` |
| Viewer links | `visualization.md` |

## Contracts And Operations

These are implementation or maintainer references, not material to repeat in a
normal user answer:

| Purpose | Reference |
| --- | --- |
| SQLite tables and query examples | `schema.md` |
| Release installation and freshness | `snapshots.md` |
| V2 component boundaries and provenance | `artifact-model-v2.md` |
| MCP/REST migration notes | `api-upgrade-notes.md` |
| Build, validation, and raw-source investigation | `maintainer-operations.md` |
| Reviewed correction evidence and release gates | `correction-lifecycle.md` |
| Manual HPC geometry refresh | `geometry-batch-slurm.md` |

## Checked-In Build Inputs

The CSV, JSON, `.sums`, schema, geometry-manifest, and geometry-coverage files
in this directory are not general documentation. They are reviewed,
checksum-sensitive inputs consumed by V2 builders and workflows. Their paths
are part of the current producer contract; do not rename, relocate, or delete
them based only on apparent duplication or a low Markdown link count.

Examples include participant crosswalks, package inventories, image metadata,
correction assertions and semantic explanations, and the geometry seed
manifest. Each substantive change requires its normal provenance, validation,
and semantic-change review.

## Maintenance Rule

Prefer one authoritative reference per concept. When an implementation is
retired, update or remove its operational prose in the same change that updates
the code, but retain explicitly labeled migration or audit evidence when it is
still required for reproducibility. Keep raw producer-state details in
`maintainer-operations.md`; public-agent guidance should describe the supported
MCP, REST, canonical-page, and artifact surfaces.
