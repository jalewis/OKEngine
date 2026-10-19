"""The human-review vocabulary, in one place, for every surface that shows it (okengine#823).

A reviewer is asked to pick one of five words and is shown a reason code the system invented.
Both were rendered raw: the panel printed `legacy-unspecified` verbatim, and nothing anywhere
said that `approve` and `dismiss` CLEAR quarantine while the other three keep it, or that a
decision is bound to the version the reviewer opened. `docs/human-review.md` states all of it;
the screen where the decision is made did not.

This module is the single source, imported by the Cockpit (app.py globals, since only FUNCTIONS
are rebound by `_bind_service` -- a constant defined in a service module is invisible to the
rebound body) and by the generated Markdown dashboards. Adding a reason code without adding it
here leaves a reviewer reading an identifier, which `test_review_vocabulary_covers_every_emitted_reason`
refuses.
"""
from __future__ import annotations

# code -> what the reviewer is actually being told, and what it usually implies.
# `implies` names the disposition the code tends toward; it is guidance, never a default, because
# a reviewer who is told the queue's answer stops reviewing.
REASON_GLOSSARY: dict[str, dict[str, str]] = {
    "legacy-unspecified": {
        "label": "Inherited flag, no recorded reason",
        "means": (
            "This page was flagged before the review ledger existed, so no reason was recorded. "
            "Nobody knows why it was raised."
        ),
        "implies": (
            "Usually `dismiss` rather than `reject`: you are judging that the REQUEST was never "
            "valid, not vouching for content you did not read."
        ),
    },
    "manual": {
        "label": "Two writers disagreed about a field",
        "means": (
            "A second writer offered a different value and the write path KEPT the existing one. "
            "The attempted value did not land -- the page still holds the value shown as kept."
        ),
        "implies": (
            "`approve` confirms the kept value. To override it, `request-changes` with a note "
            "naming the field and the value that should replace it."
        ),
    },
    "broken-link": {
        "label": "The page links to a page that does not exist",
        "means": (
            "A write recorded an unresolvable wikilink: the page points at a target nothing in the "
            "vault answers to, so a reader following it lands nowhere."
        ),
        "implies": (
            "Fix or remove the link (`request-changes` naming it), or `dismiss` if the target has "
            "since been created. Approving leaves the dead link in place."
        ),
    },
    "id-collision": {
        "label": "Two pages claim the same identity",
        "means": (
            "Two different pages resolved to the same slug id, so any lane joining on that id "
            "cannot tell which one it means."
        ),
        "implies": (
            "Decide whether they are one subject (merge, then tombstone the duplicate onto the "
            "canonical) or two (rename one). Approving does not separate them."
        ),
    },
    "rejected-write": {
        "label": "A writer tried to change a field that never changes",
        "means": (
            "A write tried to change an immutable field (such as the page id or its creation "
            "record) and the write path reverted it. The page still holds the original value."
        ),
        "implies": (
            "Usually `approve`: nothing changed. Look at the writer if this recurs; it is "
            "authoring fields it must not touch."
        ),
    },
    "owner-conflict": {
        "label": "A writer that does not own a field tried to change it",
        "means": (
            "A writer offered a new value for a field another pack or lane owns. The owner's value "
            "was kept; the attempted value did not land."
        ),
        "implies": (
            "`approve` confirms the owner's value. If the attempted value is right, the OWNER must "
            "write it -- `request-changes` naming the field and value."
        ),
    },
    "changed-after-approval": {
        "label": "Approved, then edited",
        "means": (
            "Someone approved an earlier version and the content changed afterwards, so the "
            "approval was withdrawn and this request opened."
        ),
        "implies": "Decide whether the change preserves what was approved.",
    },
    "agent-draft": {
        "label": "An agent flagged its own output",
        "means": "The writing agent marked its own work as needing a human.",
        "implies": "Ordinary content review.",
    },
    "grounding": {
        "label": "A grounding check flagged an unsupported claim",
        "means": "The body asserts something the cited evidence does not carry.",
        "implies": "Read the body against the evidence before deciding; this is not a citation gap.",
    },
    "conflict": {
        "label": "Sources disagree on a field",
        "means": "The page carries an unresolved `conflicts:` entry naming the contested field.",
        "implies": "Resolve the disagreement, or `defer` if the evidence cannot settle it.",
    },
    "categorical-confidence": {
        "label": "An agent asserted a human-only confidence value",
        "means": (
            "The write set a categorical verdict (`confirmed`, `false-positive`, `refuted`) that "
            "only a human may assert. The write LANDED and the page was flagged -- it was not "
            "blocked."
        ),
        "implies": "Confirm the verdict, or `request-changes` naming the value it should carry.",
    },
    "import-unvetted": {
        "label": "Pack policy requires review for this type",
        "means": (
            "The page's type is listed in the pack's `review_required_types`, so it enters review "
            "on arrival regardless of its content."
        ),
        "implies": "Ordinary content review against the pack's bar for this type.",
    },
    "legacy-signoff-import": {
        "label": "Pre-ledger sign-off, evidence scope unknown",
        "means": (
            "A historical `reviewed_by`/`reviewed_on` was imported as a decision record. What the "
            "original reviewer actually examined was not recorded."
        ),
        "implies": "Treat the prior sign-off as unverified provenance, not as an approval you inherit.",
    },
}

# The five dispositions. `quarantine` is the fact reviewers most need and were never shown:
# approval and dismissal release the content, the other three hold it.
DECISION_EFFECTS: dict[str, dict[str, object]] = {
    "approve": {"quarantine": "cleared", "note_required": False,
                "judges": "the content, at this exact version"},
    "request-changes": {"quarantine": "kept", "note_required": True,
                        "judges": "the content -- salvageable, but something must change first"},
    "reject": {"quarantine": "kept", "note_required": True,
               "judges": "the content -- it is wrong and should not stand"},
    "dismiss": {"quarantine": "cleared", "note_required": True,
                "judges": "the REQUEST -- it was not valid; the page never needed review"},
    "defer": {"quarantine": "kept", "note_required": True,
              "judges": "nothing yet -- left open for someone or something else"},
}

VERSION_LOCK_NOTICE = (
    "Your decision is recorded against the exact version and content hash you are reading. "
    "If the page changes before you submit, the decision is REFUSED (409) and neither the page "
    "nor the decision is written -- reopen and decide against the current version."
)


def explain_reason(code: str) -> dict[str, str]:
    """Glossary entry for `code`, or an honest placeholder naming the unexplained code.

    An unknown code must not render as a bare identifier and must not silently vanish: the
    reviewer is told the system did not explain itself, which is a bug report, not a judgment.
    """
    entry = REASON_GLOSSARY.get(code)
    if entry:
        return {"code": code, **entry}
    return {
        "code": code,
        "label": code or "unspecified",
        "means": f"No explanation is registered for the reason code `{code}`.",
        "implies": "",
    }


def explain_reasons(reasons) -> list[dict[str, str]]:
    """Distinct glossary entries for a request's reason rows, in first-seen order."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in reasons or []:
        code = str((row or {}).get("code") or "") if isinstance(row, dict) else str(row or "")
        if code in seen:
            continue
        seen.add(code)
        out.append(explain_reason(code))
    return out
