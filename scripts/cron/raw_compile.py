#!/usr/bin/env python3
"""raw_compile.py — compile STRUCTURED raw captures into source pages, no model (okengine#862).

raw-backfill hands one raw capture per run to a model, which spends ~4.5 minutes copying fields a
feed item or connector record already carries in structured form. Measured on one live vault: the
model's page was the capture's own title/url/date/summary, plus a `publisher` it inferred — and on
connector records it dropped `title` and mislabeled scores. At one item per 15-minute run a 5,555
item backlog was ~58 days of runs.

This lane compiles every capture that carries a COMPLETE identity deterministically:

  feed item (`raw/**/*.md`)       url, title, published, a DECLARED publisher (the item's own,
                                  else its feed's feeds.opml `publisher=`), and a body
  connector record (`raw/<CONNECTOR_DIR>/<archive>/records/**/*.json`)
                                  payload url/title/published and snippet; publisher is the
                                  record's own, else the URL's host (the site that published it)

Anything short of that is left untouched for raw-backfill, whose model can do what code cannot.

It bypasses the SERVER, never the INVARIANTS: the queue is `select_raw_batch.scan_raw()` (the same
definition raw-backfill drains), and every write goes through the write path's own
`_raw_backfill_frontmatter` + `_converge_raw_source` — the identity/seat raw-backfill's tool uses —
under the authenticated `cron:raw-compile` actor, so this lane's `output_contract`, the policy plane
and every write-path guard apply exactly as they do to a model write.

Env: WIKI_PATH (/opt/vault) · OKENGINE_RAW_COMPILE_LIMIT (400 writes attempted per run) ·
     OKENGINE_MCP_DIR (/opt/hermes/okengine-mcp) · `--dry-run` classifies without writing.
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

import yaml
from yaml import YAMLError

sys.path.insert(0, str(Path(__file__).resolve().parent))  # staged siblings: selector, okf_migrate
import okf_migrate  # noqa: E402
import select_raw_batch as S  # noqa: E402
import source_content  # noqa: E402

ACTOR = "cron:raw-compile"
LIMIT = int(os.environ.get("OKENGINE_RAW_COMPILE_LIMIT", "400"))
# What a raw capture may NOT carry onto its page: identity, lifecycle and provenance the write
# path stamps itself, and fields other lanes own (quality scoring, review). Everything else in
# the capture's frontmatter is carried verbatim, exactly as raw-backfill's digest tells its model
# to ("CARRY the raw page's ingest-provenance frontmatter VERBATIM"). A fixed allow-list dropped
# importer provenance such as a paper id or an upvote snapshot (okengine#886 follow-up).
NEVER_CARRY = frozenset({
    "id", "type", "raw", "version", "created", "last_updated", "updated", "status",
    "producer_lane", "extension_id", "tombstone_reason", "superseded_by", "needs_review",
    "reliability", "credibility", "maintained_by", "discovered_by", "review_status",
    "reviewed_by", "reviewed_on", "review_state", "reviewed_at", "reviewed_version",
    "content_state",
})
_TAG = re.compile(r"<[^>]+>")
_HTTP = re.compile(r"https?://\S+$")
_WROTE = re.compile(r"\b(sources/\S+?\.md)\b")


def _scalar(value):
    """YAML parses an unquoted timestamp into a datetime; pages carry the ISO string."""
    if isinstance(value, str):
        return _join_surrogates(value)
    if isinstance(value, list):
        return [_scalar(v) for v in value]
    return value.isoformat() if isinstance(value, (datetime.date, datetime.datetime)) else value


def _join_surrogates(text: str) -> str:
    """Rejoin surrogate halves into the character they encode (okengine#894).

    feed_fetch once wrote an emoji into YAML as a JSON surrogate-pair escape (`"\\ud83d\\udd25"`);
    YAML decodes each half on its own, and a lone surrogate has no UTF-8 encoding, so the page it
    reached broke the projection. A half with no partner becomes U+FFFD rather than surviving."""
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:80].rstrip("-") or "untitled"


def declared_publishers(vault: Path) -> dict[str, str]:
    """feed label -> the publisher its feeds.opml outline DECLARES. Items fetched before the
    attribute existed carry only `source_feed`; this resolves them from the declaration, never
    from the label itself."""
    import feed_fetch
    out: dict[str, str] = {}
    # glob-ok: feeds/ is the pack's flat OPML config dir, not a (shardable) wiki namespace
    for opml in sorted((vault / "feeds").glob("*.opml")):
        for row in feed_fetch.load_opml_sources(opml):
            if row.get("publisher"):
                out.setdefault(row["label"], row["publisher"])
    return out


def _feed_item(text: str, publishers: dict[str, str]) -> tuple[dict, str]:
    m = S.FRONTMATTER_RE.match(text)
    fm = yaml.safe_load(m.group(1)) if m else None
    if not isinstance(fm, dict):
        return {}, ""
    # The body is the capture's own, verbatim — what raw-backfill's model writes too.
    out = {k: _scalar(v) for k, v in fm.items()
           if k not in NEVER_CARRY and v not in (None, "", [], {})}
    if not out.get("publisher") and publishers.get(str(fm.get("source_feed") or "")):
        out["publisher"] = publishers[str(fm["source_feed"])]
    body = text[m.end():].strip()
    out["content_state"] = source_content.raw_state(fm, body)
    return out, body


def _connector_record(text: str, rel: str) -> tuple[dict, str]:
    doc = json.loads(text)
    item = doc.get("payload") if isinstance(doc, dict) else None
    if not isinstance(item, dict):
        return {}, ""
    url = str(item.get("source_url") or item.get("url") or "").strip()
    host = urllib.parse.urlparse(url).hostname or ""
    fm = {
        "title": item.get("title"),
        "url": url,
        "published": item.get("published_at") or item.get("published"),
        "publisher": item.get("publisher") or host.removeprefix("www.") or None,
        "source_feed": doc.get("connector_id") or rel.split("/")[2],
        "source_channel": "api",
        "content_state": "discovery",
        "source_kind": "news",
        "source_native_id": item.get("article_id") or doc.get("source_native_id"),
    }
    snippet = str(item.get("summary") or item.get("matched_snippet") or "")
    return ({k: v for k, v in fm.items() if v not in (None, "")},
            " ".join(html.unescape(_TAG.sub("", snippet)).split()))


def structured(rel: str, text: str,
               publishers: dict[str, str] | None = None) -> tuple[dict, str] | str:
    """(frontmatter, body) for a capture with a complete identity, else the reason it has none."""
    try:
        if rel.endswith(".md"):
            fm, body = _feed_item(text, publishers or {})
        elif rel.endswith(".json") and rel.startswith(f"raw/{S.CONNECTOR_DIR}/") \
                and "/records/" in rel:
            fm, body = _connector_record(text, rel)
        else:
            return "shape-unsupported"
    except (ValueError, YAMLError):  # bare name: Cosmic Ray cannot mutate a dotted one
        return "unparseable"
    for key in ("url", "title", "published", "publisher"):
        if not str(fm.get(key) or "").strip():
            return f"missing-{key}"
    if not _HTTP.match(str(fm["url"]).strip()):
        return "url-not-http"
    if len("".join(body.split())) < S.ACCEPT_MIN_CHARS:
        return "body-too-short"
    fm["type"] = "source"
    return fm, body


def _write_server():
    """The write path, bound to this lane's identity. Env mirrors the raw-backfill MCP server's."""
    os.environ["OKENGINE_WRITE_ACTOR"] = ACTOR
    os.environ.setdefault("OKENGINE_CRON_JOBS", "/opt/data/cron-plus/jobs.json")
    os.environ.setdefault("OKENGINE_OUTPUT_CONTRACT_MODE", "enforce")
    mcp_dir = os.environ.get("OKENGINE_MCP_DIR", "/opt/hermes/okengine-mcp")
    sys.path[:0] = [mcp_dir, str(Path(mcp_dir).parent / "scripts" / "cron")]
    import write_server as W
    if not callable(getattr(W, "_converge_raw_source", None)):
        raise SystemExit(f"ERROR: {mcp_dir}/write_server.py predates okengine#862 "
                         "(_converge_raw_source missing) — rebuild the gateway image")
    return W


def compile_one(W, rel: str, fm: dict, body: str) -> str:
    fm, error = W._raw_backfill_frontmatter(fm)
    if error:
        return error
    requested = okf_migrate.write_key(S.VAULT, "sources", _slug(str(fm["title"])), fm) + ".md"
    return W._converge_raw_source(rel, fm, requested, body)


def url_index(W, vault: Path) -> dict[str, str]:
    """normalized url -> the live source page that already holds it (okengine#862).

    `_converge_raw_source` keys identity on the URL-derived id, so a page minted by another
    writer under a different id scheme (an importer's title-derived id) is invisible to it: the
    first raw-compile run on a live vault re-created 5 such papers at a second seat. Looking the
    URL up across the namespace closes that. Normalization is the write path's own `_norm_url`,
    and a URL already on two pages resolves to the lexically first — never to a new third copy.
    """
    index: dict[str, str] = {}
    for page in sorted((vault / "wiki" / "sources").rglob("*.md")):
        try:
            m = S.FRONTMATTER_RE.match(page.read_text(encoding="utf-8", errors="replace"))
            fm = yaml.safe_load(m.group(1)) if m else None
        except (OSError, YAMLError):
            continue
        if not isinstance(fm, dict) or str(fm.get("status") or "").lower() == "tombstoned":
            continue
        url = W._norm_url(fm.get("url"))
        if url:
            index.setdefault(url, page.relative_to(vault / "wiki").as_posix())
    return index


_LINK_OPEN, _LINK_CLOSE = re.compile(r"\[\["), re.compile(r"\]\]")


def literal_text(body: str) -> str:
    """Captured web text is not markup. A page that literally shows `[[ticker]]` (a template, a
    code sample) must not become a wikilink the write path rejects as unresolved — it did, every
    run, for three live captures. A zero-width break keeps the text readable and inert."""
    return _LINK_CLOSE.sub("]\u200b]", _LINK_OPEN.sub("[\u200b[", body))


def attach(W, page: str, rel: str, publisher: str = "", body: str = "") -> str:
    """Record `rel` as another capture of an existing page: append it to that page's `raw:`
    list through the write path, never a second page for the same URL.

    A page with no `publisher` also takes the capture's DECLARED one. Without it the page never
    counts as compiled, so the selector re-offers this capture every run — to raw-backfill's
    model too — and on one live vault 263 such no-ops consumed most of each run's budget
    (okengine#862). An existing publisher is never overwritten.
    """
    fm, current_body = W._read_page(S.VAULT / "wiki" / page)
    raw = fm.get("raw")
    raws = [raw] if isinstance(raw, str) else list(raw or [])
    patch: dict = {} if rel in raws else {"raw": raws + [rel]}
    if publisher and not str(fm.get("publisher") or "").strip():
        patch["publisher"] = publisher
    # The page was found by this capture's URL, so it is the same document: the FULLER capture wins,
    # the one rule the converge path applies too (converge.stub_upgrade, user decision, #962). A
    # snippet never overwrites a full extract; a fuller re-extraction corrects a bad one and fills a
    # stub. The acceptance floor still applies, because the lane's own contract refuses a shorter body.
    fuller = W.converge.stub_upgrade(current_body, body, {"type": "source"})
    new_body = body if fuller and len("".join(body.split())) >= S.ACCEPT_MIN_CHARS else None
    if new_body is not None:
        out = W._update(page, patch or None, new_body)
        if not out.startswith(("rejected", "refused", "error")):
            return out
        # A body upgrade the write path refuses never costs the capture its backlink: without
        # `raw` the capture is re-offered every run (okengine#862/#886).
    if not patch:
        return "already-attached"
    return W._update(page, patch, None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="classify the queue, write nothing")
    ap.add_argument("--limit", type=int, default=LIMIT)
    args = ap.parse_args(argv)
    if not (S.VAULT / "raw").is_dir():
        print('{"wakeAgent": false}')
        return 0
    scan = S.scan_raw()
    queue = sorted(scan.unprocessed, key=lambda t: (S.path_tier(t[0]), -t[1], -t[2]))
    publishers = declared_publishers(S.VAULT)
    W = _write_server()  # also the URL normalizer; a dry run imports it and writes nothing
    known = url_index(W, S.VAULT)
    outcomes: Counter[str] = Counter()
    attempted = 0
    for rel, _year, _mtime in queue:
        if attempted >= args.limit:
            outcomes["not-examined:run-limit"] += 1
            continue
        try:
            text = (S.VAULT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            outcomes["unreadable"] += 1
            continue
        parsed = structured(rel, text, publishers)
        if isinstance(parsed, str):
            outcomes[f"left-for-model:{parsed}"] += 1
            continue
        existing = known.get(W._norm_url(parsed[0]["url"]))
        if args.dry_run:
            outcomes["would-attach" if existing else "compilable"] += 1
            continue
        fm, body = parsed[0], literal_text(parsed[1])
        result = attach(W, existing, rel, str(fm.get("publisher") or ""), body) if existing \
            else compile_one(W, rel, fm, body)
        if result == "already-attached":  # a read, not a write: never spends the run's budget
            outcomes[result] += 1
            continue
        attempted += 1
        ok = not result.startswith(("rejected", "refused", "error"))
        seat = _WROTE.search(result.split("\n", 1)[0]) if ok and not existing else None
        if seat:  # a later capture of this URL in the same run attaches instead of re-minting
            known[W._norm_url(parsed[0]["url"])] = seat.group(1)
        outcomes[("attached" if existing else "compiled") if ok
                 else "rejected:" + result.split("\n", 1)[0][:90]] += 1
    print(f"# raw-compile — {len(queue)} uncompiled captures"
          f"{' (dry run)' if args.dry_run else ''}\n")
    for outcome, n in sorted(outcomes.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"- {outcome}: {n}")
    print(json.dumps({"wakeAgent": False, "queue": len(queue), **dict(outcomes)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
