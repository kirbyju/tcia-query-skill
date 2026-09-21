---
name: tcia-release-verification
description: Verify TCIA query-skill release and service changes when bundle schemas, manifests, staging, correction gates, MCP/REST contracts, or release workflows are modified.
license: Apache-2.0
---

# TCIA Release Verification

Use this workflow for repository maintenance. It does not authorize publishing, workflow dispatch, deployment, service restart, or data transfer.

## Scope the verification

Inspect the working tree and identify the changed contract boundary. Read only the relevant sections of the repository-root `references/maintainer-operations.md` and the affected workflow, schema, or server documentation.

- Bundle or artifact changes require manifest, component, profile, provenance, SQLite integrity, and cross-component coherence checks.
- Correction changes require exact evidence binding, semantic-change reporting, consumption or waiver validation, and negative tests for malformed or unmatched approvals.
- MCP/REST changes require typed contract, structured-output, error-envelope, cursor, annotation, and documented-tool-surface checks.
- Workflow changes require syntax validation, immutable action pins, failure-path review, and confirmation that failed validation cannot publish a release artifact.

## Execute proportionate checks

Start with the smallest deterministic tests that exercise the changed boundary. Broaden to the repository's required suite when the change affects a shared contract or release path. Use the hash-locked dependency environment for MCP/REST validation rather than the macOS system Python.

For an exact release candidate, validate the candidate artifacts themselves. Confirm declared hashes, read-only SQLite open, `PRAGMA quick_check`, foreign keys, required objects and keys, profile membership, source provenance, and negative incompatible or wrong-component probes. A test suite passing against fixtures does not validate a candidate artifact.

## Report the result

State the commit or dirty working-tree basis, artifact fingerprint when applicable, commands run, counts or hashes that form acceptance evidence, failures or unverified checks, and the highest state actually established: local validation, publication, installation, or deployment.

Stop after local verification unless the user explicitly authorizes the next external state change.
