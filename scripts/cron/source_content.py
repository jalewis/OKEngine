"""Acquisition state, not a quality score, for source content (okengine#923).

A discovery hit remains useful for collection. It is not a captured article, however long its
snippet or however confidently a writer labels it. Readers derive state from local raw evidence
and require the captured text to be present in the source body. Appraisal remains a separate
review decision; no acquisition state declares a claim true or a document independently verified.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from yaml import YAMLError

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
CAPTURE_HEADING = "## Captured content"
MAX_RAW_BYTES = 8_388_608  # 8 MiB, including frontmatter


def split(text: str) -> tuple[dict, str]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    value = yaml.safe_load(match.group(1))
    return (value if isinstance(value, dict) else {}), text[match.end():]


def raw_state(fm: dict, body: str) -> str:
    """Classify the producer's raw artifact; never trust a caller-supplied content_state."""
    if fm.get("capture_dead_letter"):
        return "capture-failed"
    if (fm.get("capture_object") and fm.get("capture_revision") and fm.get("content_hash")
            and CAPTURE_HEADING in body and body.partition(CAPTURE_HEADING)[2].strip()):
        return "captured"
    if fm.get("source_channel") == "feed":
        return "discovery"
    return "unknown"


def _plain(text: str) -> str:
    # raw-compile neutralizes literal wikilinks with zero-width breaks; that is not an omission.
    return " ".join(text.replace("\u200b", "").split())


def source_state(vault: Path, fm: dict, body: str) -> str:
    """Derive effective acquisition state for old and new pages without mutating the corpus.

    Missing, malformed, oversized or escaped raw references cannot attest to a capture. A newly
    attached capture also cannot upgrade a still-snippet-only source. Multiple raw observations
    are allowed, but only captured text actually present in the page makes it captured.
    """
    raw = fm.get("raw")
    refs = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    states: set[str] = set()
    for ref in refs:
        if not isinstance(ref, str) or not ref.startswith("raw/"):
            continue
        path = vault / ref
        try:
            path.resolve().relative_to((vault / "raw").resolve())
            with path.open("rb") as handle:
                data = handle.read(MAX_RAW_BYTES + 1)
            if len(data) > MAX_RAW_BYTES:
                continue
            text = data.decode("utf-8")
            if path.suffix == ".json":
                doc = json.loads(text)
                if isinstance(doc, dict) and isinstance(doc.get("payload"), dict):
                    states.add("discovery")
                continue
            raw_fm, raw_body = split(text)
            state = raw_state(raw_fm, raw_body)
            if state == "captured":
                captured = _plain(raw_body.partition(CAPTURE_HEADING)[2])
                if captured in _plain(body):
                    return "captured"
                # The artifact exists but the source page has not been hydrated from it.
                state = "unknown"
            states.add(state)
        except (OSError, ValueError, YAMLError):
            continue
    if "capture-failed" in states:
        return "capture-failed"
    if "discovery" in states:
        return "discovery"
    return "unknown"
