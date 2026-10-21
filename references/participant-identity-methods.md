# Reviewed participant identity mappings

The Participant Inventory schema 9 adds `participant_equivalences` and
`agent_participant_equivalences` for finite, evidence-backed Collection-to-
Collection participant equivalence. These are not Analysis Result source links.
Consumers may combine both edge types into connected groups. Identical bare IDs
in unrelated datasets never establish a match. Reject conflicting components
containing two distinct identities in the same dataset.

`reviewed_participant_identity_v1.json` retains finite mappings reviewed on
2026-10-19, official source URLs/hashes, and exact mapping-row or UID evidence:

- 882 SAROS source links, including 20 aliases verified by Study/Series UID.
- One corrected-label prostate annotation source link.
- 40 LUAD source links replacing prior attribution, based on the dataset
  description and the reviewed filename/RID contract. This is not a claim that
  LungCT-Diagnosis and QIN LUNG CT are disjoint or a crosswalk between them.
- Five BraTS/UCSF follow-up aliases and source mappings. Two duplicate baseline
  memberships collapse within the Analysis Result before cross-dataset linking.
- 58 vestibular and 61 DFCI/BraTS-PEDs Collection equivalences.
- 21 Pseudo-PHI input/output aliases, not 21 new original TCIA people.

Within-dataset canonicalization preserves source identifier spellings, assets,
clinical values, challenge identifiers, and evidence. Consumers persisting an
old participant key should resolve its original dataset-scoped identifier in
`participant_identifiers`; five BraTS follow-up keys and 21 Pseudo-PHI input
keys are replaced by their reviewed canonical keys. No global suffix removal
or zero-padding rule is applied. Image, file, and byte counts are not reduced
by participant overlap.

Full mapping provenance is in the `audit_support` participant audit companion;
the core equivalence rows retain their stable edge ID, method, confidence, and
reference filename. The source input hash is also recorded in
`participant_inventory_sources`.

Missing current cross-dataset endpoints are recorded as review issues, never
invented. BraTS2021_00051 remains unresolved; the four TCGA-GBM-QI annotation-
only participants remain retained despite unavailable source images. MIDI-B's
538 documented input/output pairs do not establish additional archive people;
an original-source participant crosswalk is unavailable. Negative overlap
assumptions for report presentation are not published as positive identity
edges. Species and human-only reporting remain consumer-level classifications,
with animals, phantoms, benchmarks, and unknown/mixed identities kept separate.

## Curated participant index (schema 10)

The research-core inventory publishes `participant_groups` (one row per curated
participant), `participant_group_members` (dataset-scoped memberships),
`agent_participant_group_members` (membership discovery), and
`agent_participant_summary` (archive-wide species/category totals). Group IDs are
deterministic for an unchanged membership set within a release, not permanent
person identifiers. Existing dataset-scoped keys and search contracts are retained.

Build-time consolidation accepts only the supported high-confidence source and
Collection-equivalence methods. Missing endpoints and unsupported edges are
excluded. If a connected component would merge two distinct participants in one
dataset, the entire component stays separate. Matching bare IDs never adds edges.
Species derives from WordPress labels; mixed or unknown categories remain
unclassified. Benchmark subjects are kept outside the human count.

Use `get_participant_summary` or `POST /v2/participants/summary` with
`{"short_titles": ["Collection-A", "Analysis-Result-B"], "dataset_type": "both"}`.
Omit `short_titles` for archive-wide counts. An explicit empty list, unknown title,
or invalid dataset type is an error. Selected-dataset totals count distinct group
IDs represented in the selection, including links through other datasets. Group
classification comes from the release index, not a new classification per query.
The public service restricts selection to visible WordPress dataset scopes.
Missing inventories produce a concise coverage notice; published subject counts
are never added as synthetic participants. Older inventories return summary
unavailable rather than a misleading raw total.

### Public answer guidance

Use “X participants across Y datasets” and, where useful, “Participants appearing
in multiple datasets are counted once using TCIA's curated participant index.”
Use the human count for questions about people. Routine answers need no list of
identity caveats or audit observations. Surface material coverage notices returned
by the summary. The index reflects reviewed evidence and available inventories;
it is not a certification of globally distinct people. Explain those boundaries
when methodology is requested.

### Staff diagnostics

`participant_group_link_review` records accepted/rejected edges and reasons.
`participant_issue_dispositions` preserves the source status separately from
`source_resolved`, `resolved_by_reviewed_mapping`, `explained_annotation_only`, and
`actionable`. The finite annotation-only review is bound to
`reviewed-participant-issue-dispositions.json`, whose hash is recorded in inventory
sources. These dispositions do not change participant groups or assert new people.
Raw source issues remain intact. `find_participant_link_issues` and dataset
coverage include `current_disposition` and its evidence reference when available;
status filters continue to mean the raw source status for compatibility.

For staff issue counts use:

```sql
SELECT disposition, COUNT(*) AS issue_count
FROM participant_issue_dispositions GROUP BY disposition;
```

Use the optional audit companion for full source payloads. Compact diagnostic
classifications remain queryable without that companion. Do not treat the number
of retained source issue rows as the current actionable backlog.
