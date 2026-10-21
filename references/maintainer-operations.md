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

Use `scripts/tcia_participant_inventory.py`, `scripts/tcia_public_non_dicom_metadata.py`, `scripts/tcia_controlled_access_metadata.py`, `scripts/tcia_clinical_metadata.py`, and `scripts/tcia_correction_registry.py` to build or audit their focused components. The authoritative bundle manifest carries component hashes, decompressed SQLite hashes, schemas, profiles, fingerprints, and provenance. The source workflow imports a prior correction registry only after validating it against both the prior top manifest and immutable GitHub release. It uses `stage-prior` to validate a supported historical schema, copy it read-only into a fresh current-schema database, and strictly validate the upgraded result before linking the prior release or ingesting current inputs. It then packages a deterministic gzip from the exact staged snapshot. The new registry does not link to the top manifest being built; only previously published immutable releases may be linked, avoiding a fingerprint cycle.

Controlled-access public source downloads are streamed to a temporary file and
atomically installed only after completion. The producer allows 30 seconds of
socket inactivity, 300 seconds total across retries, and 1 GiB per artifact;
the complete build step has a 30-minute Actions timeout. Progress logs identify
the dataset, route, role, status, byte count, and elapsed time without printing
the source URL. A timeout, oversize response, or partial transfer is recorded as
a source error and remains blocked by `--require-complete-sources`.

### Local resumable verification

The stable public release is one atomic, fingerprinted bundle. Do not publish a
clinical, public non-DICOM, or Participant Inventory replacement independently
under the moving stable tag: the component manifests and cross-component
identity checks describe one input generation.

The resume workflow accepts an optional `semantic_review_commit` after the
retained full candidate fails its late semantic gate. It verifies the candidate
run's producer, workflow, and failed conclusion, and permits only a descendant
review commit whose changed paths are the exact-explanation file, its count
test, skill manifest, this runbook, and the resume workflow. Existing approvals
must remain byte-equivalent JSON declarations; only exact semantic declarations
may be appended. Producer code and all other data inputs remain unchanged.
The review commit and explanation-file SHA-256 are recorded inside the registry.
The original producer remains the bundle's producer; the supplemental review is
separately attributable. Replay still rejects unmatched, duplicate, or unused
approvals, consumes matched effects once, validates the full bundle, and reruns
the original producer's V2 contract tests before publishing. This is not a
source-health waiver or permission to change retained payload components.

For a `workflow_run` action, GitHub's run-level `head_sha` identifies the
default-branch workflow revision, not necessarily the checked-out source
producer. Recovery verifies the producer against the retained source plan's
`current_producer_commit`, which the original downstream action also checked.
The workflow revision and source producer are recorded separately.

### Selective component release

The scheduled source workflow always refreshes and validates the base snapshot.
It then runs `scripts/tcia_selective_release.py plan` against the immediately
preceding verified release. A fast release is permitted only when both of these
conditions hold:

1. the canonical schema-and-row fingerprint of every snapshot object visible to
   the expensive builders is unchanged; and
2. every producer path changed since the preceding bundle's exact producer
   commit is explicitly classified as unable to affect an expensive component.

DataCite DOI metadata and TCIA EndNote verified-use tables are snapshot-only and
are excluded from the downstream projection fingerprint. `snapshot_meta` is
also excluded because source timestamps do not drive derived content. All other
snapshot schema and data are included. Changes to WordPress, PathDB, clinical,
controlled-access, public non-DICOM, participant, dependency-lock, or non-Markdown
curation inputs therefore select the full build. An unavailable prior release,
unknown code path, missing Git history, malformed plan, unequal fingerprint, or
failed verification also selects or fails into the full path; none may silently
authorize carry-forward.

On an eligible fast release, the source workflow downloads the preceding
clinical and controlled-access artifacts and the downstream workflow downloads
the preceding public non-DICOM, Participant Inventory, and audit artifacts.
Every selected file is checked against both the preceding top manifest and the
captured GitHub Release digest and size. The complete bundle builder then
decompresses each database, verifies its component manifest, SQLite hash,
integrity, profiles, and source health, regenerates the web exports, and builds
a new top-level manifest bound to the triggering producer commit. The result is
still one complete atomic release; it is not a partial snapshot publication.

The run-scoped `tcia_selective_release_plan.json` records the current and prior
projection fingerprints, producer commits, changed paths, disqualifying paths,
mode, and reasons. It is provenance for workflow routing, not a public release
asset. The downstream workflow re-verifies it against the downloaded snapshot,
producer SHA, and preceding release fingerprint before skipping any expensive
step.

Selective reuse never replaces periodic source observation. The single 07:17
America/New_York scheduled run forces the full workflow every day. Manual
dispatches may select the fast path when the projection and producer-path gates
permit it, or set `force_full_build=true`; any explicit clinical refresh input
also forces the full path. This prevents unchanged WordPress/PathDB projections
from carrying independent controlled or clinical sources forward indefinitely.

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

Machine-extracted WordPress findings use a stable observation identity derived
from the source record, field, normalized excerpt, detector, and clue class.
Changing an unrelated field in the same WordPress record therefore does not
create another observation or proposal. Exact raw-record hashes and source
update times remain preserved in `correction_observation_versions`; this
separates finding identity from version provenance without discarding evidence.
Change reports canonicalize JSON values before comparison and record the exact
prior bundle fingerprint, so serialization order and overlapping pipeline runs
cannot be mistaken for new metadata changes.

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

The same workflow also writes `release_review_queue.json` and
`release_review_queue.csv`. These files group otherwise-unexplained public
non-DICOM and Participant Inventory changes only when every row resolves to one
exact `(dataset_type, short_title)` scope. Clinical, correction-registry,
cross-dataset, ambiguous, and unresolvable changes remain in the blocking list.
Queue membership is not an approval and does not alter a release candidate.
Each entry carries an exact change count and digest so a maintainer can review,
approve, or explicitly hold back one dataset without granting a blanket waiver.

A future dataset holdback must restore the complete verified prior dataset
scope before artifact packaging, regenerate all dependent Participant Inventory
rows and manifests, and rerun the ordinary semantic gate. Never publish the
changed rows while merely labeling them queued, and never auto-hold an
ambiguous or cross-dataset change.

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
when every semantic non-key value is identical. One-to-one fact-ID migrations
from CDA, Collection inheritance, WordPress dataset inference, or an official
TCIA clinical download are nonblocking because only opaque row/fact identity
changed. A changed concept, subject, value, source, or other semantic payload
still fails closed. Other source kinds remain gated, including the reviewed
HCC-TACE-Seg migration; each of those exact effects must still match and be
consumed once.

Linking the immediately preceding published release into the correction
registry is also nonblocking only when the change report proves one append-only
link for its exact baseline fingerprint: the release tag must end in that
fingerprint prefix, source health must be `verified_current`, the linked
revision set must exactly equal the prior registry's active non-semantic
decision set and digest, and the evidence-health view must report zero stale
revisions. Modified, removed, degraded, unverified, mismatched, or unrelated
release-history rows remain blocking.

The predecessor's `change_report_sha256` is optional, matching `link-release`
and the scheduled source producer. An absent hash does not bypass the baseline
fingerprint, exact decision-set membership/digest, tag, or health checks. A
supplied hash must be a valid SHA-256 value. Published historical bundles do
not necessarily include a semantic report asset; do not invent a report hash.

This release intentionally changes 105 previously published
`clinical_facts.fact_id` values. Those hashes are derived, opaque row identities,
not a stable public identifier contract. The complete evidence-bound old-to-new
mapping is published only in the `audit_support` correction registry decision;
`research_detail` remains unchanged and does not include it. Consumers that
persisted these fact IDs must install `audit_support` for this release and apply
that exact mapping. MCP and REST do not promise an alias lookup for fact IDs.

## DICOM source relationships during viewer-link migration

Analysis Result clinical inheritance resolves each public TCIA-hosted `.tcia`
download manifest against the pinned IDC series index. Both NBIA search links
and IDC dataset-filter/shared-cart links use the same manifest selection;
viewer URL strings and cart names do not establish patient identity. The
crosswalk records the WordPress download ID, original manifest SHA-256,
canonical series-set SHA-256, IDC data/package version, source Collection, and
the patients belonging to that Collection's selected series. Source Collections
must map uniquely to visible WordPress Collection identities. Inheritance is
restricted to each source's resolved patients, including multi-Collection
downloads; bare PatientIDs never authorize a join across Collections. If the
pinned IDC index lacks a migrated download's series, the resolver requests
public TCIA NBIA metadata for those exact UIDs in bounded batches. Its mapping
hash binds only UID, Collection, and PatientID; raw NBIA response hashes are
retained in component audit metadata. This records index lag without claiming
that the series are currently available through IDC. Conflicting IDC mappings
cannot be overwritten by this fallback.

An incomplete, ambiguous, unavailable, or unsupported manifest for an
IDC-routed DICOM download blocks the source build. A target PatientID that
would collapse multiple source-Collection namespaces also blocks the build.
`dicom_source_scope_diagnostics.json` records affected download IDs and coverage
without patient identifiers or source URLs, and is uploaded on workflow
failure. Legacy NBIA downloads lacking complete series metadata coverage retain their
existing explicit WordPress relationship path and appear in these diagnostics;
they do not acquire a partial manifest-derived relationship. Successful scope
diagnostics are included in the clinical component metadata. Clinical source
signatures bind the new manifest-backed relationship evidence, so a changed
manifest rebuilds inherited facts even when a shared-cart URL is unchanged.
Offline clinical builds cannot silently bypass unresolved IDC-routed downloads.

The selective planner also forces a full rebuild when visible Analysis Result
DICOM manifests exist. An unchanged WordPress URL does not prove unchanged
external manifest bytes. Fast carry-forward stays disabled for these snapshots
until the planner can verify the external content hashes before routing. Forced
full plans retain the verified prior release fingerprint and producer commit.

Changing provenance or cohort coverage still runs through the ordinary exact
semantic gate. A passing fixture test does not authorize changed release rows;
review the fresh candidate and its source evidence before publication.

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
