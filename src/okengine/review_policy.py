"""Whether a page requires human review -- ONE definition for every surface (okengine#842).

The rule had lived in three places glued together by the Cockpit's rebound globals:
`page_metadata._requires_review`, `state._GROUNDING_REVIEW`, and `app._review_required_types`.
That was tolerable while only the Cockpit asked the question. Ledger reconciliation asks it too,
from the CLI, where those rebound names do not exist -- and a reconciler with its OWN copy of the
rule would close records the worklist still considers live, or keep open records it has dropped.
That divergence is exactly the defect being repaired, so the rule is extracted here, dependencies
passed in rather than read from ambient globals, and both callers use it.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

# A body section recording a failed grounding check keeps the page in review regardless of flags.
GROUNDING_REVIEW = re.compile(
    r"##[ \t]+Grounding check.*?(unsupported|not[- ]found|not in source|contradict)", re.S | re.I
)


def legacy_review_current(fm: dict) -> bool:
    """A pre-ledger sign-off still covers the page if nothing edited it afterwards."""
    reviewed = str(fm.get("reviewed_on") or "")[:10]
    updated = str(fm.get("last_updated") or fm.get("updated") or fm.get("created") or "")[:10]
    return bool(reviewed and updated and reviewed >= updated)


def requires_review(fm: dict, body: str, required_types: frozenset) -> bool:
    """True when the page must appear in the human-review worklist."""
    if fm.get("needs_review") is True or GROUNDING_REVIEW.search(body or ""):
        return True
    if str(fm.get("type") or "") in required_types:
        return not (str(fm.get("review_state") or "") == "approved" or legacy_review_current(fm))
    return False


def review_required_types(vault: Path) -> frozenset:
    """`review_required_types` from the governing schema: the composed artifact, else the pack's."""
    for path in (Path(vault) / ".okengine" / "composed-schema.yaml", Path(vault) / "schema.yaml"):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 -- an unreadable schema means "no declared types"
            continue
        values = doc.get("review_required_types") if isinstance(doc, dict) else None
        if isinstance(values, list):
            return frozenset(str(v) for v in values)
    return frozenset()
