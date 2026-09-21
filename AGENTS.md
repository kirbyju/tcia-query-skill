# TCIA Query Skill Repository Guidance

## Project boundaries

- `SKILL.md` is the public agent entrypoint for TCIA discovery and access questions.
- `references/` contains focused domain and operational guidance; read only the files relevant to the task.
- `scripts/` builds, validates, installs, and queries release artifacts.
- `mcp_server/` exposes the read-only snapshot-backed MCP and REST contracts.
- `.agents/skills/` contains repository-maintenance workflows, not public TCIA query instructions.
- `evals/` defines agent-routing and answer-quality cases. It is not a substitute for deterministic unit and contract tests.

## Invariants

- Visible TCIA WordPress Collections and Analysis Results establish TCIA publication identity. Other systems enrich or route access but do not establish publication.
- Preserve raw source values and provenance. Keep normalized, inferred, corrected, and resolved values explicit and reviewable.
- Participant identity is dataset-scoped. Do not join participants across datasets from bare identifiers or dataset-name heuristics.
- Controlled-access metadata or manifests do not grant authorization. Never expose credentials or credential-bearing URLs.
- Distinguish local validation, published releases, installed bundles, and deployed services. Evidence for one state does not prove another.
- Preserve the separation among public DICOM, public non-DICOM, controlled-access, and Aspera/Data Retriever routes.

## Working rules

- Inspect the working tree before editing and preserve unrelated user changes.
- Use the smallest relevant reference set. Do not read the entire repository before a narrow change.
- Use existing scripts for deterministic artifact operations instead of recreating their logic ad hoc.
- Unit and contract tests use local fixtures and do not publish or deploy. Run affected tests, fix failures caused by the requested change, and rerun them without requesting approval for each local step.
- Do not push, publish a release, dispatch a workflow, transfer payload data, restart a service, or deploy without explicit authorization.
- Use `$tcia-release-verification` when changes affect bundle schemas, release manifests, staging, correction gates, MCP/REST contracts, or release workflows.

## Verification

- Skill metadata or guidance changes: run the skill validator, the skill-version tests, and `python scripts/tcia_skill_version.py check` after regenerating the manifest.
- Script changes: run the focused `scripts/tests` module first, then broaden according to the affected artifact boundary.
- MCP/REST changes: run the focused server test module in the pinned server environment and confirm the documented tool surface still matches the advertised tools.
- Before handoff, inspect the diff and report exactly what was validated. Do not describe local results as published or deployed.
