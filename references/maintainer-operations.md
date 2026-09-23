# Maintainer Operations

This reference is for repository and snapshot maintainers, not normal public-agent queries.

## Build And Validate

```bash
python3 scripts/tcia_snapshot.py build --out cache/tcia_snapshot.sqlite --gzip-out dist/tcia_snapshot.sqlite.gz --manifest-out dist/tcia_snapshot_manifest.json --exports-dir dist
python3 scripts/tcia_snapshot.py validate --db cache/tcia_snapshot.sqlite
python3 scripts/tcia_v2_bundle.py install --profile research_core
python3 scripts/tcia_v2_bundle.py install --profile research_detail
python3 scripts/tcia_v2_bundle.py install --profile audit_support
python3 scripts/tcia_v2_bundle.py prune
```

`prune` is a dry run unless `--apply` is supplied. It removes only obsolete installer-owned files recorded by receipts; it does not remove arbitrary `outputs/`, `dist/`, or maintainer build directories.

Use `scripts/tcia_participant_inventory.py`, `scripts/tcia_public_non_dicom_metadata.py`, `scripts/tcia_controlled_access_metadata.py`, `scripts/tcia_clinical_metadata.py`, and `scripts/tcia_correction_registry.py` to build or audit their focused components. The authoritative bundle manifest carries component hashes, decompressed SQLite hashes, schemas, profiles, fingerprints, and provenance. The source workflow imports a prior correction registry only after validating it against both the prior top manifest and immutable GitHub release, then packages a deterministic gzip from the exact staged snapshot. The new registry does not link to the top manifest being built; only previously published immutable releases may be linked, avoiding a fingerprint cycle.

### Local resumable verification

The stable public release is one atomic, fingerprinted bundle. Do not publish a
clinical, public non-DICOM, or Participant Inventory replacement independently
under the moving stable tag: the component manifests and cross-component
identity checks describe one input generation.

Before starting an expensive local rebuild, exercise both CDA endpoints used by
the clinical builder:

```bash
python3 scripts/tcia_clinical_metadata.py probe-cda
```

This probe uses the same pinned client, exact-ID request shape, and bounded
retry policy as the build. A successful probe permits a normal live build. If
it fails, the clinical builder may reuse the prior CDA rows for stable
promotion only when it can carry forward all three of the following: the last
successful live-observation time, the CDA release fingerprint, and the exact
prior clinical SQLite SHA-256. That evidence is accepted for at most seven
days. Repeated failures carry the original observation time forward rather
than resetting the clock. Missing, malformed, future-dated, or expired
evidence remains degraded and blocks stable promotion.

This bounded exception applies only to `clinical.cda_clinical`, which is a
downstream enrichment source. It does not authorize stale WordPress
publication/visibility/license/download metadata, controlled-access routing,
or another source. The downstream workflow runs `preflight-source-health`
immediately after it verifies and expands the source artifact, before geometry,
public non-DICOM, and Participant Inventory construction.

For local repair work, expensive component outputs may instead be retained as
non-release checkpoints with `scripts/tcia_local_build_cache.py`. A checkpoint
is selected by SHA-256 hashes of every declared file/directory input plus
explicit settings. Stored outputs are hash-verified before atomic restoration.
The utility never uploads or publishes anything.

Use these recommended boundaries:

1. `public-non-dicom`: save the validated public and audit SQLite files, their
   gzip files/manifests, and the geometry refresh report after the complete
   **Build public non-DICOM metadata** block succeeds.
2. `participant-inventory`: save the validated Participant Inventory and audit
   SQLite files plus their gzip files/manifests after that complete block
   succeeds.
3. Never checkpoint away the exact semantic-change explanation gate, correction
   consumption/package step, V2 contract tests, bundle build/validation,
   attestation, or published-release validation. Rerun those gates after every
   restoration.

At minimum, declare `requirements-build.lock`, `scripts/`, `references/`, the
source and baseline manifests, and every SQLite consumed by the stage as
inputs. Conservatively hashing all of `scripts/` and `references/` may cause
extra cache misses, but prevents a relevant code or curation change from being
silently overlooked. Include non-file settings such as the release contract
and PathDB inclusion mode with `--value`.

For example, after a successful public non-DICOM block:

```bash
python3 scripts/tcia_local_build_cache.py save \
  --stage public-non-dicom \
  --input requirements-build.lock --input scripts --input references \
  --input cache/tcia_metadata_v2_staging.sqlite \
  --input cache/public_non_dicom_baseline.sqlite \
  --input cache/public_non_dicom_audit_baseline.sqlite \
  --input cache/public_non_dicom_geometry_seed.sqlite \
  --input dist/tcia_snapshot_manifest.json \
  --value release_contract=streamlined --value include_pathdb_files=true \
  --output cache/public_non_dicom_metadata.sqlite \
  --output cache/public_non_dicom_audit.sqlite \
  --output dist/public_non_dicom_metadata.sqlite.gz \
  --output dist/public_non_dicom_metadata_manifest.json \
  --output dist/public_non_dicom_audit.sqlite.gz \
  --output dist/public_non_dicom_audit_manifest.json \
  --output cache/reports/public_non_dicom_geometry_refresh.json
```

Before repeating that block, run the corresponding `restore` command with the
same `--stage`, `--input`, and `--value` arguments (omit `--output`). Exit status
`0` is a verified hit and `3` is a clean miss; on a miss, rebuild and save the
stage. If any declared source, code, curation, dependency, or setting changes,
the fingerprint changes and the old checkpoint is not selected.

When reproducing the Participant Inventory block locally, defer its cleanup of
`cache/clinical_metadata.sqlite` and `cache/controlled_access_metadata.sqlite`
until after the checkpoint has been saved. Those files are stage inputs and
must remain available while its fingerprint is calculated.

`scripts/tcia_metadata_change_report.py --fail-on-unexplained-high` consumes only
current approved correction effects that exactly match the asset, table,
complete ordered key, change kind, and before/after row SHA-256 values. Partial
matches, duplicate matches, malformed approvals, and unused approvals fail the
stable gate. Active validation waivers are also exact, expiring, and single-use;
expired, revoked, malformed, duplicate, or unused waivers cannot authorize
promotion.

Release reviewers record those one-build approvals in
`references/correction-semantic-explanations-v1.json` using the exact values
from the JSON change report. The workflows bind consumed effects to that
report's SHA-256 and preserve the consumed state across later registry refreshes;
they never reactivate a previously consumed declaration. These one-build gate
approvals are excluded from the durable correction decision-set digest to avoid
self-reference, but remain fully hash-pinned inside the published registry
SQLite/gzip and therefore inside the top bundle fingerprint.

When that gate fails, the source workflow uploads a non-release, run-specific
`tcia-metadata-v2-diagnostics-<run-id>-<run-attempt>` Actions artifact under
`always()`, retained for 30 days. It contains the diagnostics-only status
sentinel, the complete hash-based JSON/Markdown change report when reporting was
reached, the registry validation summary, and, when present, the
initial-registry bootstrap evidence. It deliberately excludes raw SQLite/gzip
payloads, component manifests, dependency lock inventories, credentials, and
the validated source-artifact name. Access follows the repository's Actions
artifact permissions; GitHub does not provide a separate private flag for an
artifact in a public repository. The normal source artifact and downstream
release remain blocked. Reviewers must use the JSON record's exact artifact,
table, complete ordered primary key, change kind, and before/after digests.

The one-time correction-registry migration is permitted only when the immediately
prior published bundle is a digest-verified schema-2 release that has no
correction asset, component, decision-set summary, or profile selection. The
source workflow records a critical passed `initial_registry_bootstrap`
validation plus hash-bound evidence containing the prior bundle fingerprint and
the complete initial decision-set digest. Its authorization scope explicitly
excludes metadata-row changes and all future missing/corrupt registry cases;
once a release link or bootstrap record exists, the bootstrap command refuses
to run again.

The reviewed HCC-TACE-Seg identifier migration is represented as one decision
containing 105 exact old-to-new aliases and 210 independently matched removal
and addition effects. The change report may pair equal-count key substitutions
when every non-key value is byte-identical, but pairing is explanatory only: it
does not remove either event from the semantic gate. Each exact effect must
still match and be consumed once.

This release intentionally changes 105 previously published
`clinical_facts.fact_id` values. Those hashes are derived, opaque row identities,
not a stable public identifier contract. The complete evidence-bound old-to-new
mapping is published only in the `audit_support` correction registry decision;
`research_detail` remains unchanged and does not include it. Consumers that
persisted these fact IDs must install `audit_support` for this release and apply
that exact mapping. MCP and REST do not promise an alias lookup for fact IDs.

## Raw Hidden-State Investigation

The public MCP, REST, and `TciaQueryService` surfaces deliberately exclude hidden, staged, and retired WordPress records. A maintainer who must investigate raw source state may use the local snapshot/search builder CLI's explicit `--include-hidden` option in a controlled workflow:

```bash
python3 scripts/tcia_wordpress_search.py --query retired --include-hidden
```

Do not advertise this option to public clients or use its records in ordinary discovery answers. Label findings as internal/raw source state and preserve the WordPress visibility flag.

## Supporting Utilities

- `scripts/tcia_freshness.py`: compare operational files with the published skill manifest.
- `scripts/tcia_metadata_change_report.py`: compare rebuilt artifacts and produce review summaries.
- `scripts/tcia_manifest_series_uids.py`: extract Series Instance UIDs from legacy manifests.
- `scripts/tcia_create_data_retriever_csv.py`: create supported route-column manifests.
- `scripts/datacite_tcia_dois.py` and `scripts/tcia_publications.py`: DOI and verified-publication maintenance/query utilities.

See `references/snapshots.md`, `references/schema.md`, and `references/artifact-model.md` for the detailed release and database contracts.

## Versioned Identifier Policy

Treat identifiers such as `/v2/`, `scripts/tcia_v2_bundle.py`, `TCIA_V2_*`,
`tcia_metadata_v2_bundle_manifest.json`, and `tcia-metadata-v2-latest` as
compatibility contracts. Preserve them in commands, endpoints, configuration,
release assets, and migration notes. In general public prose, describe the
REST service, release bundle, installer, and artifacts without embedding a
version number unless the version distinction is material to the task.
