#!/usr/bin/env python3
"""Bounded full-text repair of existing sources through the raw-compile write contract.

Dry-run by default. Capture is acquisition, not appraisal. Existing bodies and identities are
retained; captures are appended, with immutable raw provenance and optimistic write checks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml
from yaml import YAMLError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import raw_compile as compiler  # noqa: E402
import source_content as content  # noqa: E402
import web_capture  # noqa: E402
from web_capture import CaptureError  # noqa: E402


def read_source(vault: Path, page: str) -> tuple[dict, str, str]:
    path = vault / "wiki" / page
    path.resolve().relative_to((vault / "wiki" / "sources").resolve())
    with path.open("rb") as handle:
        data = handle.read(content.MAX_RAW_BYTES + 1)
    if len(data) > content.MAX_RAW_BYTES:
        raise ValueError("source exceeds bounded read size")
    fm, body = content.split(data.decode("utf-8"))
    if fm.get("type") != "source" or fm.get("status") == "tombstoned":
        raise ValueError("not a live source")
    raw = fm.get("raw", [])
    if not (isinstance(raw, str) or isinstance(raw, list) and all(isinstance(x, str) for x in raw)):
        raise ValueError("malformed raw references")
    return fm, body, "sha256:" + hashlib.sha256(data).hexdigest()


def landing(vault: Path, fm: dict, provenance: dict, body: str) -> str:
    """Immutable raw observation, separate from the capture store's response object/revision."""
    raw_fm = {key: fm[key] for key in ("url", "title", "published", "publisher") if key in fm}
    raw_fm.update(provenance)
    text = "---\n" + yaml.safe_dump(raw_fm, allow_unicode=True, sort_keys=True) + "---\n\n" + body
    key = hashlib.sha256(text.encode()).hexdigest()
    rel = f"raw/hydration/{key[:2]}/{key}.md"
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        pass
    return rel


def hydrate(W, vault: Path, page: str, *, apply: bool = False) -> dict:
    fm, body, expected = read_source(vault, page)
    state = content.source_state(vault, fm, body)
    result = {"page": page, "before": state}
    if state == "captured":
        return {**result, "outcome": "already-captured"}
    url = fm.get("url")
    if not isinstance(url, str) or not url.strip():
        return {**result, "outcome": "missing-url"}
    if not apply:
        return {**result, "outcome": "would-capture", "url": url}
    # No conditional 304: a repair needs actual text, not merely a remote unchanged receipt.
    try:
        captured = web_capture.capture(vault / "raw/captures", url,
                                       native_id=str(fm.get("id") or ""),
                                       publisher=str(fm.get("publisher") or ""))
        if not captured.text.strip():
            raise CaptureError("extraction", "capture yielded no readable text")
        provenance = {
            "requested_url": captured.requested_url, "retrieved_url": captured.final_url,
            "canonical_url": captured.canonical_url, "retrieved_at": captured.retrieved_at,
            "content_hash": captured.content_hash, "text_hash": captured.text_hash,
            "capture_object": captured.object_ref, "capture_revision": captured.revision_ref,
        }
        raw_body = content.CAPTURE_HEADING + "\n\n" + captured.text.strip() + "\n"
        rel = landing(vault, fm, provenance, raw_body)
        # Keep prior excerpts and curation, clearly separate from the newly acquired document.
        new_body = (body.rstrip() + "\n\n## Full-text acquisition\n\n"
                    "Captured content; appraisal remains required.\n\n"
                    + compiler.literal_text(raw_body))
        desired_state = "captured"
    except CaptureError as exc:
        dead = web_capture.dead_letter(vault / "raw/captures", url, str(fm.get("id") or ""), exc)
        provenance = {"requested_url": url, "capture_dead_letter": dead}
        rel = landing(vault, fm, provenance, "Capture failed: " + exc.category + "\n")
        new_body = None
        desired_state = "capture-failed"
        result.update(error=str(exc), category=exc.category)
    raw = fm.get("raw", [])
    refs = [raw] if isinstance(raw, str) else raw
    patch = {"raw": list(dict.fromkeys([*refs, rel])), "content_state": desired_state}
    # All capture metadata lives in the immutable raw observation; never change the source URL/id.
    if rel in refs and fm.get("content_state") == desired_state and new_body is None:
        return {**result, "outcome": desired_state, "raw": rel}
    written = W._update(page, patch, new_body, expected_sha256=expected)
    if not written.startswith("updated "):
        return {**result, "outcome": "write-rejected", "detail": written, "raw": rel}
    # Verify the persisted artifact, not just the writer's success string.
    observed_fm, observed_body, _ = read_source(vault, page)
    observed = content.source_state(vault, observed_fm, observed_body)
    return {**result, "outcome": observed if observed == desired_state else "verification-failed",
            "raw": rel}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", action="append", help="wiki-relative source path; repeatable")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    vault = compiler.S.VAULT
    pages = args.source if args.source is not None else [
        p.relative_to(vault / "wiki").as_posix()
        for p in sorted((vault / "wiki/sources").rglob("*.md")) if p.name != "INDEX.md"]
    W = compiler._write_server() if args.apply else None
    outcomes = []
    attempted = 0
    remaining = 0
    for page in dict.fromkeys(pages):
        if attempted >= args.limit:
            remaining += 1
            continue
        try:
            result = hydrate(W, vault, page, apply=args.apply)
        except (OSError, ValueError, YAMLError) as exc:
            result = {"page": page, "outcome": "invalid-source", "detail": str(exc)}
        outcomes.append(result)
        attempted += result["outcome"] not in {"already-captured", "invalid-source"}
    print(json.dumps({"apply": args.apply, "results": outcomes, "not_examined": remaining}, sort_keys=True))
    return int(any(row["outcome"] in {"capture-failed", "write-rejected", "verification-failed",
                                     "invalid-source", "missing-url"} for row in outcomes))


if __name__ == "__main__":
    raise SystemExit(main())
