"""Source identity for pages compiled from one raw capture (okengine#862).

Two writers compile raw captures into `source` pages: the raw-backfill MCP tool (a model fills the
fields) and the deterministic raw-compile lane (structured items, no model). Both must derive the
SAME identity and seat for the same URL, or a page compiled by one is re-created by the other at a
second path: the okengine#54 loop. So the identity lives here, once, and both call it.

Installed onto the write-server facade like its siblings: bare names (`_converge`,
`_selected_raw_url`, `hashlib`, `yaml`, `Path`) resolve against the facade's globals.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml


def _converge_raw_source(
    raw_ref: str,
    fm: dict,
    requested: str,
    body: str = "",
    pack: str = "",
    expected_sha256: str = "",
) -> str:
    """Converge one source page for `raw_ref`, keyed by its URL.

    `fm` must already be normalized (`_raw_backfill_frontmatter`). The raw backlink is always the
    runner-selected capture, never a caller-supplied one. A missing `url` is recovered from the
    capture itself; with none, the page has no identity and is rejected.
    """
    fm["raw"] = raw_ref
    url = str(fm.get("url") or "").strip()
    if not url:
        url = _selected_raw_url([raw_ref])  # noqa: F821 — facade-bound sibling
        if url:
            fm["url"] = url
    if not url:
        return "rejected: source url is required"
    if not _is_service_endpoint_url(url) and not _source_identity_url(url):  # noqa: F821
        # A placeholder (`UNKNOWN`, `null`) is the ABSENCE of identity: every capture carrying one
        # would hash to one id and fuse onto one page (#515/#962). Same outcome as no url.
        return f"rejected: source url {url!r} carries no identity — use the capture's real URL"
    if _is_service_endpoint_url(url):  # noqa: F821 — facade-bound state (okengine#901)
        # Every record read from a bare API endpoint shares its URL, so hashing it would fuse
        # them all into one page. Same outcome as no url: without a record identity, reject.
        return (
            f"rejected: source url {url!r} is a bare service endpoint, not a record — "
            "use a record-specific URL (the query that returns this record)"
        )
    identity_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    fm["id"] = f"sources:url-{identity_hash[:20]}"
    fm["raw"] = _raw_backlinks_with(fm["id"], raw_ref)
    path: Any = Path(requested)
    canonical = path.with_name(f"{path.stem}-{identity_hash[:8]}{path.suffix or '.md'}")
    # A capture of an already-compiled URL records its raw backlink and adds what is new; it never
    # replaces the compiled body (the lane contract: "duplicate captures append the raw path"),
    # except a STUB source body, which a full capture upgrades (converge.stub_upgrade, #962).
    with converge.create_redirect():  # noqa: F821 — facade-bound module
        return _converge(  # noqa: F821 — facade-bound sibling
            canonical.as_posix(),
            yaml.safe_dump(fm, sort_keys=False, allow_unicode=True),
            body,
            pack,
            "",
            expected_sha256,
        )


def _raw_backlinks_with(page_id: str, raw_ref: str) -> Any:
    """The page's `raw:` backlinks plus `raw_ref` — appended, never replaced (okengine#886).

    A second capture of an already-compiled URL converges onto the same page. Replacing `raw:`
    un-marked the first capture as processed, so the selector re-queued it forever. The existing
    live page (resolved by id, exactly as `_converge` resolves it) keeps every capture it already
    records; a page with a single capture keeps the scalar form so nothing churns.
    """
    existing_rel = _registry().resolve(page_id)  # noqa: F821 — facade-bound sibling
    if not existing_rel or _registry().is_tombstoned(page_id):  # noqa: F821
        return raw_ref
    try:
        current, _body = _read_page(_wiki() / existing_rel)  # noqa: F821
    except (OSError, ValueError):
        return raw_ref
    raw = (current or {}).get("raw")
    raws = [raw] if isinstance(raw, str) else [str(r) for r in (raw or [])]
    return raws if raw_ref in raws else (raws + [raw_ref] if raws else raw_ref)
