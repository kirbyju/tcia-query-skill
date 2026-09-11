# Derived metadata correction lifecycle

Use `scripts/tcia_correction_registry.py` to record derived-metadata corrections
without changing the authoritative WordPress, clinical, workbook, or file-list
source rows. The registry is additive: current builders and their existing
reviewed inputs remain the production behavior until the bundle workflow is
explicitly wired to publish the registry.

## Evidence model

The lifecycle is:

```text
source observation -> case -> proposal -> reviewed decision revision
                   -> derived effect -> validation -> release link
```

Waivers are separate, scoped, owned, expiring records. They do not turn an
unverified observation into verified evidence.

`correction_observations` stores field-level evidence with the TCIA dataset and
source record identity, URL, field or JSON pointer, bounded excerpt, excerpt
SHA-256, raw-record SHA-256, source update date, observation timestamp, and
detector version. Source records remain untouched.

Machine-extracted WordPress clues are always `proposal_only=1`, use candidate
confidence, and cannot directly create an approved decision. Current clue
classes cover identifier/naming contracts, Collection/Analysis Result source
relationships, count statements, access restrictions, geometry wording, and
explicit unavailability. Hidden WordPress records are outside this lifecycle.

`correction_decisions` is append-only. `decision_id` identifies one logical
scope. `revision_id` includes status, reviewer/approver, review timestamps,
rationale, resolution, complete scope, negative scope, expected effects,
evidence observations, supersession, staleness, source kind, and policy
version. A change to any reviewed semantic input creates a new revision.
Supersession and staleness move `correction_cases.current_revision_id`; they do
not mutate or delete the prior revision.

The normative decision document schema is
`references/correction-registry-v1.schema.json`. The builder also validates the
same required fields, stable IDs, evidence references, current-revision links,
machine-proposal boundary, and SQLite integrity without requiring a third-party
JSON Schema package.

## First-version migration coverage

The registry deterministically projects:

- public non-DICOM reviewed decisions from
  `references/public-non-dicom-crosswalk-curation-v1.json`;
- reviewed Analysis Result-to-source-Collection relationships from
  `references/reviewed_analysis_result_source_collections_v1.csv`; and
- the clinical normalization and review policy families listed in
  `CLINICAL_DECISION_CONSTANTS` in `scripts/tcia_correction_registry.py`.

Clinical migration includes dataset-specific concepts and value labels,
canonical display values, NLST morphology/topography/screening labels, source
column concept and unit overrides, reviewed screening resolutions, permanent
screening review scopes, subject-ID overrides, reviewed cohort patterns, and
the Hungarian colorectal ICD-10 mapping. It also explicitly inventories and
projects CT colonography histology and nonmalignant-severity policy, EA1141
race/ethnicity/grade decoding and handled columns, HNSCC handled columns, and
per-source transform versions. `CLINICAL_POLICY_INVENTORY` independently
classifies each production family as registry-covered or procedural with
evidence so an unclassified transform fails policy-coverage review. These are
registry projections of the existing reviewed policy; the clinical builder
remains the source of production behavior during the additive migration.

Every imported policy decision carries negative scope that prohibits raw
clinical row/fact replacement or application outside the declared scope.
Public crosswalk decisions prohibit invented participant links and raw subject
ID replacement. Source-Collection links prohibit copying unmatched source
participants or linking equal bare identifiers across datasets.

## Build and inspect

Use a fixed observation timestamp for reproducible tests or forensic rebuilds:

```bash
python3 scripts/tcia_correction_registry.py build \
  --snapshot cache/tcia_snapshot.sqlite \
  --snapshot-manifest cache/tcia_snapshot_manifest.json \
  --out cache/tcia_correction_registry.sqlite \
  --replace

python3 scripts/tcia_correction_registry.py validate \
  --db cache/tcia_correction_registry.sqlite
```

`--replace` is a compatibility name for a safe staged refresh, not an in-place
deletion. The builder creates a fresh sibling database, transactionally imports
all prior observations, revisions, effects, validations, releases, waivers,
superseded/stale history, and case current pointers, appends deterministic
source projections, validates the staged database, and atomically replaces the
old path. Any build/import/validation failure leaves the prior database intact.

The snapshot manifest is optional, but omitting it deliberately records source
health as `unverified`. A manifest whose `source_status` is entirely `live`
records `verified_current`; any fallback records `degraded` with the original
per-source statuses and warnings.

Use these compact views before opening verbose payloads:

- `agent_correction_queue`: open or stale cases and their proposal/evidence counts;
- `agent_active_corrections`: the current approved, non-stale revision per case;
- `agent_field_resolution_trace`: expected/observed field effects and governing evidence;
- `agent_release_evidence_health`: release source health, decision-set digest,
  report digest, stale revisions, high-severity queue count, and active waivers;
- `agent_correction_changes_since_release`: added, revised, stale, or withdrawn
  current decisions relative to each linked release, with prior/current revision
  IDs and effect counts.

Mark changed/unreachable evidence by creating a new immutable stale revision:

```bash
python3 scripts/tcia_correction_registry.py mark-stale \
  --db cache/tcia_correction_registry.sqlite \
  --revision-id revision_... \
  --stale-status stale_source_changed
```

Release linkage and a temporary waiver are explicit operations:

```bash
python3 scripts/tcia_correction_registry.py link-release \
  --db cache/tcia_correction_registry.sqlite \
  --release-fingerprint SHA256 \
  --release-tag tcia-metadata-v2-latest \
  --source-health verified_current \
  --observed-at 2026-09-11T12:00:00Z \
  --change-report-sha256 SHA256

python3 scripts/tcia_correction_registry.py add-waiver \
  --db cache/tcia_correction_registry.sqlite \
  --rule-id RULE \
  --owner CURATOR \
  --reason 'Bounded explanation' \
  --scope-json '{"source":"wordpress_analysis_results"}' \
  --created-at 2026-09-11T12:00:00Z \
  --expires-at 2026-09-12T12:00:00Z
```

`correction_releases` has exactly one immutable header per release fingerprint;
`correction_release_revisions` stores its exact decision membership. Repeating
an identical link is a no-op. Reusing a fingerprint with a different tag,
decision set, source health, or change-report digest fails without mutation.

## Semantic change reporting

`scripts/tcia_metadata_change_report.py` now streams complete keyed-row digests
and detects additions, removals, modifications, and equal-count substitutions
for public non-DICOM, Participant Inventory, clinical, and correction-registry
surfaces. `--json-out` writes a canonical summary with `report_sha256`. That
digest covers the semantic comparison payload and deliberately excludes
`generated_at_utc` and the digest field itself, so repeated comparisons of the
same inputs have one evidence identity while retaining invocation time as
operational metadata. Within `correction_validations`, `executed_at` (and the
forward-compatible alias `observed_at_utc`) is likewise excluded from semantic
row comparison. Validation status, expected/actual values, evidence digest,
rule/producer versions, producer commit, and message remain semantic and can
trip the high-severity gate.
`--fail-on-unexplained-high` returns status 2 when high-severity semantic
changes remain unexplained.

The strict option is intentionally not enabled in a workflow by this change.
The final bundle integrator should build the registry before reporting, pass
the old/new registry with `--correction-old/--correction-new`, persist both
Markdown and JSON reports, and enable the strict gate after correction-effect
matching is part of the release build.

## Declarative assertions

`references/correction-assertions-v1.json` moves safe reviewed crosswalk and
participant-coverage expectations out of Python literals. Every assertion
group names its evidence file. Dynamic expectations such as optional PathDB
inclusion remain in code until their parameterized contract is represented
without weakening the existing gate.

## Integration hooks deliberately left to the final bundle integrator

No workflow, MCP, REST, deployment, or service file is changed here. The final
integrator should:

1. build and validate the registry from the exact staged snapshot and manifest;
2. persist it in audit support or define it as a new hash-pinned component;
3. pass registry old/new paths into semantic change reporting;
4. match observed row effects to approved correction effects before enabling
   the unexplained-high hard gate;
5. link the final release fingerprint, source health, and change-report digest;
6. promote correction/source-health summaries into the top bundle manifest;
7. use `--full-content` audit reconstruction when release runtime permits, or
   retain the deterministic sample plus schedule the full digest as a required
   pre-promotion job.

Do not describe the registry as published or deployed until those integration
steps have completed and the released SQLite/hash contract has been verified.
