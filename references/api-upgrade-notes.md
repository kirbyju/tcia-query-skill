# API Upgrade Notes

## Server 0.3.0

This release intentionally narrows and types the public agent/API contract.

- Public MCP and REST inputs no longer accept `include_hidden`. Public service methods always query visible WordPress records. Maintainer-only raw inspection remains a local CLI concern.
- `search_datasets` returns compact summaries. Call `get_dataset` for narrative fields, complete access/license detail, current downloads, and related Analysis Results.
- High-volume dataset, participant, download, participant-asset, and public non-DICOM searches return `has_more`, `truncated`, and `next_cursor`. Pass `next_cursor` back with the same filters and limit. Cursors are opaque, query-bound, and stable for the installed immutable snapshot.
- Limits outside each operation's documented range are rejected instead of silently defaulted or clamped.
- REST failures use `application/problem+json`: 404 for missing entities, 422 for invalid input/cursors, 503 for unavailable snapshot artifacts, and 500 for unexpected failures. MCP errors include the same stable code and a corrective action.
- MCP tools advertise structured output plus read-only, non-destructive, idempotent, snapshot-local metadata.
- The V2 OpenAPI document contains only V2 routes. Explicit `/v1/...` compatibility URLs remain callable but are no longer advertised.
- `/v2/live` is process liveness and `/v2/ready` cheaply checks required artifacts/query surfaces. The old `/v2/health` URL remains as an undocumented compatibility alias.
- Public bundle/snapshot DTOs no longer disclose filesystem paths.

Clients should treat additive response fields as compatible, avoid decoding cursors, and use the advertised OpenAPI/MCP schemas rather than hand-maintained field lists.
