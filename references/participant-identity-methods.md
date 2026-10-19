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
