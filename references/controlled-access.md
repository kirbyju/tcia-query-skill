# Controlled Access

Use this reference whenever WordPress license metadata indicates a TCIA dataset is controlled/restricted, or when a user asks about controlled-access data.

## Policy Link

Always point users to the latest TCIA policy page:

```text
https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/
```

That page contains current instructions for requesting access to controlled datasets. It also explains how approved users can create a JSON API key and configure the TCIA Data Retriever to use that key.

## When To Alert Users

Use license metadata as the source of truth. Do not use `collection_page_accessibility` or `result_page_accessibility` to classify controlled access; those fields are being phased out.

Alert users when license metadata indicates NIH Controlled Data Access, TCIA Restricted, controlled access, restricted access, dbGaP, DAC approval, data usage agreements, or other authorization requirements.

Do not alert as controlled access when license metadata is Creative Commons. Creative Commons licenses mean open access. Creative Commons NonCommercial licenses are open access with a noncommercial-use restriction; call out the noncommercial restriction, but do not send users to controlled-access request instructions unless another license in the same dataset/download metadata is controlled/restricted.

## Required User-Facing Guidance

Before download/API instructions, say clearly that:

- The dataset is controlled access, not open access.
- The decision came from license metadata, not the deprecated collection/page accessibility field.
- Metadata may be visible even when files cannot be downloaded without authorization.
- The user should review the TCIA NIH Controlled Data Access Policy page for current request, approval, API key, and TCIA Data Retriever configuration instructions.

Do not invent approval requirements, timelines, or eligibility rules. Link to the policy page and summarize only what has been verified from current TCIA pages or WordPress metadata.

## Routing Notes

- Controlled-access face datasets: route access questions to the policy page and, where applicable, General Commons metadata for `phs004225`.
- NCTN trials or Biobank data: use WordPress license metadata and current TCIA access statements; CTDC support is planned but should not be assumed until confirmed.
- Public subsets of a mixed collection can be described separately from controlled/restricted subsets.
