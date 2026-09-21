# Web And Browser LLM Access

Use this reference when an assistant can browse public web pages but cannot
install this skill, attach an arbitrary MCP server, run local code, or query
downloaded SQLite artifacts.

MCP implementation, protocol compatibility, REST schemas, and deployment belong
in `mcp_server/README.md`, `mcp_server/DEPLOYMENT.md`, and
`api-upgrade-notes.md`.

## Choose A Reachable Surface

Choose by capability rather than product name because browser products change
their connector and HTTP support independently.

1. If the client already supports a remote MCP connection, use
   `https://tcia.duckdns.org/mcp` and follow `mcp_server/README.md`.
2. If it can make arbitrary HTTP requests, use the V2 REST service at
   `https://tcia.duckdns.org/v2`. Its OpenAPI document is
   `https://tcia.duckdns.org/v2/openapi.json`.
3. If it has web search or page retrieval only, use canonical TCIA Collection
   and Analysis Result pages on `https://www.cancerimagingarchive.net/`.
4. Use GitHub Release artifacts only for offline, bulk, custom-SQL, or pinned
   reproducibility work. Ordinary browser questions should not require artifact
   downloads.

Do not tell a browser-only user to install MCP, Python, `idc-index`, or the
release bundle merely to answer a routine metadata question.

## Browser-Only Workflow

1. Search within `cancerimagingarchive.net` for the dataset title, short title,
   or DOI. Prefer the canonical `/collection/` or `/analysis-result/` page.
2. Use the page's title, abstract, DOI, access table, license, download rows,
   version history, and related datasets as the TCIA evidence.
3. Check related Analysis Results before concluding that a Collection lacks
   annotations, segmentations, labels, or ground truth.
4. Use DataCite for DOI metadata and external DOI relationships. An external
   repository record derived from a TCIA DOI remains externally published unless
   TCIA also lists it as a Collection or Analysis Result.
5. Use IDC for public DICOM series detail only after confirming TCIA provenance
   and access/license status. Do not construct public viewer or download links
   for controlled-access data.
6. Cite the canonical TCIA page and DOI. State uncertainty if a page, table, or
   linked source could not be retrieved.

Do not use search-result snippets alone when the canonical page is reachable.
Do not treat GitHub, IDC, DataCite, Zenodo, PathDB, or another downstream record
as proof that TCIA publishes the dataset.

## HTTP-Capable Browser Agents

Use MCP when the host already supports it. Otherwise use REST; do not attempt to
simulate MCP with ad hoc HTTP requests.

For REST freshness, call `/v2/ready` and `/v2/bundle`, then use compact search
routes followed by detail routes. Record the installed release fingerprint and
timestamp. If latest-published status matters, compare that fingerprint with
the small current bundle manifest without downloading payload artifacts.

Some browse-only tools support GET retrieval but not POST requests. They can use
GET detail routes for known short titles, but broad REST dataset search requires
POST. In that environment, use the canonical TCIA pages or ordinary site search
instead of claiming that the REST service was queried.

## Static Fallback

If web pages, MCP, and REST are unavailable but the host can retrieve and
decompress files, resolve the current GitHub release and read
`tcia_metadata_v2_bundle_manifest.json` before selecting an asset from that same
release. Use `snapshots.md` and `artifact-model-v2.md` for the artifact contract.

The compressed JSONL exports are fallbacks for environments that can process
gzip and line-delimited JSON but cannot query SQLite. They are not precomputed
answers and should not be downloaded for a routine browser question.

## Public Website Guidance

Browser assistants work best when TCIA pages provide:

- canonical, stable URLs and correct HTTP status codes;
- meaningful server-rendered headings and text without requiring client-side
  JavaScript;
- `Dataset` and `DataDownload` JSON-LD with DOI, license, version, distribution,
  and provenance fields that agree with visible page content;
- XML sitemaps containing canonical Collection and Analysis Result pages with
  accurate `lastmod` values;
- crawler and firewall rules that distinguish search, user-triggered retrieval,
  and model-training traffic according to TCIA policy;
- a concise `/llms.txt` index linking the dataset catalog, access/license
  policy, DOI guidance, MCP endpoint, REST/OpenAPI documents, and publications;
- an official-domain landing page that explains the browser, REST, MCP, and
  offline routes without requiring repository discovery.

`llms.txt` is supplementary. It does not replace crawlable HTML, structured
data, sitemaps, canonical links, or vendor crawler configuration.
