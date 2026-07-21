# Revision-aware web capture

`scripts/cron/web_capture.py` is the mechanical full-text capture substrate for
linked feed items. It is deliberately disabled by default. A pack opts in by
adding `--capture-full-text` to its generic `feed_fetch.py` job and may select a
store with `--capture-dir`; otherwise the store is a `captures/` sibling of the
raw feed landing directory.

## Producer contract

For each linked item the capture stage:

1. validates the requested URL and every redirect against the public-network
   policy;
2. applies bounded time, retry, redirect, response-size, and content-type rules;
3. sends saved `ETag` and `Last-Modified` validators;
4. preserves the raw response as a SHA-256-addressed immutable object;
5. extracts readable HTML or plain text and common provenance metadata;
6. writes an immutable revision observation containing requested, final, and
   canonical URLs, publisher, source-native ID, response metadata, hash, and
   retrieval time;
7. writes a deterministic dead-letter record when fetch or extraction fails.

The supported content types are HTML, XHTML, and plain text. PDF, Office, OCR,
browser-rendered pages, images, and media require the separate hard-format
extraction capability; they fail visibly as `unsupported-content` here.

## Store layout

```text
captures/
  objects/<sha-prefix>/<sha256>.html|txt
  revisions/<canonical-url-prefix>/<canonical-url-hash>/<content-hash>-<observation>.json
  dead-letter/<failure-prefix>/<failure-hash>.json
```

Objects deduplicate identical content across syndicated URLs. Revision records
remain observation-specific, so deduplication never erases publisher or native
source provenance. Existing files are opened with create-only semantics and are
never rewritten.

## Raw landing contract

The feed landing page receives:

- `canonical_url`, `retrieved_url`, and `source_native_id`;
- `content_hash`, `capture_object`, and `capture_revision`;
- author, source tags, language, license, and enclosure metadata when supplied;
- the mechanically extracted text under `## Captured content`.

When a previously seen native item has a new content hash or changed feed
metadata, the producer writes a separate `-revision-<hash>.md` landing with
`revision_of`; it never overwrites the earlier record. Correction/retraction
labels are retained, and the first observed upstream 404/410 becomes both an
`upstream-removed` dead letter and a retraction revision. Repeated identical
removals, a 304, or wholly unchanged content create no duplicate artifact.

Consumers must treat captured text as source material, not verified fact. They
must retain links to the capture revision and apply the pack's normal source,
grounding, and review policies during ingest.

## Operational controls

- `CAPTURE_TIMEOUT` — per-request timeout, default 20 seconds.
- `CAPTURE_MAX_BYTES` — maximum response size, default 5 MiB.
- `CAPTURE_ALLOW_PRIVATE_NETS=1` — permits private/link-local destinations for
  an explicitly private-network deployment. Off by default.
- `CAPTURE_USER_AGENT` — operator-selected User-Agent.

The capture state lives beside the existing feed state under `captures` and is
checkpointed atomically with feed validators and seen IDs.
