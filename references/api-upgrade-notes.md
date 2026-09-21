# API Upgrade Notes

## MCP Python SDK 2 And Protocol 2026-07-28

The MCP adapter now uses MCP Python SDK 2.x. One Streamable HTTP endpoint serves
the current `2026-07-28` stateless protocol and the earlier handshake-era
revisions, so existing clients continue to connect while newer clients can use
`server/discover`.

This protocol revision is independent of the TCIA V2 metadata bundle version and
release fingerprint. MCP negotiation describes how a client talks to the server;
`get_snapshot_info` describes which TCIA metadata generation the server queries.

## Server 0.3.0

This release intentionally narrows and types the public agent/API contract.

- Public MCP and REST inputs no longer expose legacy internal-source query
  switches. Raw-source inspection remains a maintainer-only local CLI concern.
- `search_datasets` returns compact summaries. Call `get_dataset` for narrative fields, complete access/license detail, current downloads, and related Analysis Results.
- High-volume dataset, participant, download, participant-asset, public non-DICOM, and V1-release searches return `has_more`, `truncated`, and `next_cursor`. Pass `next_cursor` back with the same filters and limit. Cursors are opaque and bound to the query, declared release/component identity, and actual installed-file generation; restart pagination after any install or in-place artifact change. V1-release discovery is newest-first.
- Limits outside each operation's documented range are rejected instead of silently defaulted or clamped.
- REST failures use `application/problem+json`: 404 for missing entities, 422 for invalid input/cursors, 503 for unavailable snapshot artifacts, and 500 for unexpected failures. MCP errors include the same stable code and a corrective action.
- MCP tools advertise structured output plus read-only, non-destructive, idempotent, snapshot-local metadata.
- Public response models reject undeclared fields and incorrect types. Variable provenance and manifest metadata is confined to explicitly named typed objects.
- The V2 OpenAPI document contains only V2 routes. Explicit `/v1/...` URLs are **breaking deprecated aliases**, not wire-compatible V1 implementations: they use V2 compact fields, cursor envelopes, strict limits, and Problem Details errors. Every `/v1` response carries `Deprecation: @1798675199`, `Sunset: Fri, 31 Dec 2027 23:59:59 GMT`, and `Link` headers to the V2 docs and this migration note.
- `/v2/live` is process liveness. `/v2/ready` validates the manifest/install receipt fingerprint, installed profile/assets/components, component schema metadata, required query surfaces, and visible WordPress dataset coherence, caching the result until an installed file changes. The baseline accepts bundle manifest schemas 2 and 3; schema-3-specific operational fields may add stricter checks during integration. The old `/v2/health` URL remains as an undocumented compatibility alias.
- Public bundle/snapshot DTOs no longer disclose filesystem paths.
- Retired standalone NIfTI/pathology MCP tools have no environment-variable opt-in. Use the unified V2 public non-DICOM and participant tools.
- Retired standalone `/v1/nifti/...` and `/v1/pathology/...` REST URLs return stable `410 Gone` Problem Details plus the standard V1 deprecation/migration headers; they never query legacy sidecars. Use `/v2/public-non-dicom/assets`.

Clients should treat additive response fields as compatible, avoid decoding cursors, and use the advertised OpenAPI/MCP schemas rather than hand-maintained field lists.
