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

Use `scripts/tcia_participant_inventory.py`, `scripts/tcia_public_non_dicom_metadata.py`, `scripts/tcia_controlled_access_metadata.py`, `scripts/tcia_clinical_metadata.py`, and `scripts/tcia_correction_registry.py` to build or audit their focused V2 components. The authoritative bundle manifest carries component hashes, decompressed SQLite hashes, schemas, profiles, fingerprints, and provenance. The source workflow imports a prior correction registry only after validating it against both the prior top manifest and immutable GitHub release, then packages a deterministic gzip from the exact staged snapshot. The new registry does not link to the top manifest being built; only previously published immutable releases may be linked, avoiding a fingerprint cycle.

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
- `scripts/idc_viewer_urls.py`: create viewer URLs after provenance/access/IDC validation.
- `scripts/datacite_tcia_dois.py` and `scripts/tcia_publications.py`: DOI and verified-publication maintenance/query utilities.

See `references/snapshots.md`, `references/schema.md`, and `references/artifact-model-v2.md` for the detailed release and database contracts.
