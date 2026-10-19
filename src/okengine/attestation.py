"""Curated-authority attestation fields are operator-owned (okengine#980).

A source record that carries ``local_only: true``, ``export_policy: deny``, ``retrieved_via``,
``dataset``, ``record_checksum``, ``bounded_auto_accept: true`` and
``evidence_role: curated-authority``, and that an actor page lists in ``operator_evidence_refs``,
is treated by the local-evidence resolver as an attested authority record
(``extensions/okengine.assessments/local_evidence.py``). A pack authority policy may then
auto-accept a bounded claim from it with no URL or host check, because the attestation stands in
for that check.

That is only sound while the fields mean what they say: that a deterministic producer imported
the record from a curated dataset. The enforced write path used to let any model-driven lane set
them. ``page-quality-enrich`` could turn an ordinary blog post into an attested record with one
``update_entity`` call, and link it from the actor with a second. The resolver cannot tell the
difference after the fact.

The legitimate producer is a deterministic importer that writes pages directly, never through the
MCP write path. So the rule at that boundary is: an MCP write must PRESERVE these fields exactly,
whoever the caller is. It may not add, change or remove them. On a page that already carries an
attestation it may not change the record's identity (``publisher``) or its body either, because
the resolver reads the body to decide which subject the record is about.

Reading, scoring (``reliability``/``credibility``) and other enrichment of an attested record are
unaffected. The same predicates drive the corpus-audit detector, which reports attestation found
where no legitimate producer could have put it.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from okengine.field_values import same_value

#: The trust assertions themselves. Any one present makes a page an attestation carrier.
TRUST_FIELDS = ("local_only", "export_policy", "record_checksum", "bounded_auto_accept")
#: The record descriptors the resolver and the pack authority policy match on.
RECORD_FIELDS = ("retrieved_via", "dataset", "evidence_role")
#: The actor-side channel: only a reference through it can attest a record for a subject.
REFERENCE_FIELDS = ("operator_evidence_refs",)
#: Fields no MCP write may add, change or remove.
OPERATOR_FIELDS = TRUST_FIELDS + RECORD_FIELDS + REFERENCE_FIELDS
#: Fields frozen only on a page that already carries an attestation: who the record says it is
#: from (the resolver reads ``publisher``, falling back to ``vendor``), and every key the resolver
#: can look a reference up by. Writing ``authority_id: <ref>`` onto an attested record would
#: otherwise let a dangling reference somewhere else resolve to it.
RECORD_IDENTITY_FIELDS = ("publisher", "vendor", "id", "url",
                          "attack_id", "authority_id", "cve_id", "ghsa_id", "osv_id")
#: The keys a mapping-shaped reference names its target by, in the resolver's order.
_MAPPING_REF_KEYS = ("source_ref", "path", "url", "id")
#: The only page type an attestation belongs on.
SOURCE_TYPE = "source"

_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def _present(value: Any) -> bool:
    # `false` is an assertion too (bounded_auto_accept: false), so only emptiness is absence.
    return value not in (None, "", [], {})


def carries_attestation(fm: dict) -> bool:
    """True when the frontmatter makes any curated-authority trust assertion."""
    return any(_present(fm.get(field)) for field in TRUST_FIELDS)


def _value(fm: dict, field: str) -> Any:
    value = fm.get(field)
    return value if _present(value) else None


def operator_field_changes(prev: dict, new: dict) -> list[str]:
    """Operator-owned fields whose value differs between two frontmatter mappings.

    An empty value and an absent key are the same: a lane that echoes ``operator_evidence_refs: []``
    onto a page without the key has changed nothing.
    """
    return [field for field in OPERATOR_FIELDS
            if not same_value(_value(prev, field), _value(new, field))]


def mcp_write_refusal(prev: dict, new: dict, body_changed: bool) -> Optional[str]:
    """The refusal for an MCP write that would alter operator-owned attestation, or None.

    ``prev`` is the stored frontmatter (``{}`` for a create) and ``new`` the frontmatter the write
    would commit. ``body_changed`` says whether the write replaces or extends the stored body.
    """
    changed = operator_field_changes(prev, new)
    if carries_attestation(prev):
        changed += [f for f in RECORD_IDENTITY_FIELDS
                    if not same_value(_value(prev, f), _value(new, f))]
        if body_changed:
            changed.append("body")
    if not changed:
        return None
    return (
        f"refused: {', '.join(changed)} — curated-authority attestation is operator-owned "
        "(okengine#980). Only the deterministic importer of a curated dataset sets these "
        "fields; an MCP write must leave them, and the publisher and body of an attested "
        "record, exactly as stored. Omit them from the write."
    )


def misplaced(fm: dict) -> bool:
    """An attestation on a page that is not a source record."""
    return carries_attestation(fm) and str(fm.get("type") or "").strip() != SOURCE_TYPE


def _ref(value: Any) -> str:
    text = str(value).strip()
    match = _WIKILINK.fullmatch(text)
    text = (match.group(1) if match else text).strip()
    return text.removeprefix("wiki/").removesuffix(".md")


def _refs(value: Any) -> Iterable[str]:
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, dict):
            item = next((str(item[k]) for k in _MAPPING_REF_KEYS if _present(item.get(k))), "")
        if isinstance(item, str) and item.strip():
            yield _ref(item)


class AttestationAudit:
    """Accumulate, page by page, attestation that has no legitimate producer.

    Three findings:

    * ``misplaced`` — trust assertions on a page whose type is not ``source``. A merge that
      folded a record's frontmatter into an entity leaves this, and the resolver does not check
      the type of what it attests.
    * ``lane_created`` — trust assertions on a page an MCP job lane created (the write path stamps
      ``producer_lane`` on every job create). A model-driven lane is never the producer.
    * ``unattested_refs`` — an ``operator_evidence_refs`` entry naming an existing page that makes
      no trust assertion: the operator channel carrying something no importer attested.
    * ``dangling_refs`` — an ``operator_evidence_refs`` entry that names no page by exact path. The
      resolver no longer falls back to an id or URL for this channel, so the reference attests
      nothing; before that fallback was removed, it was a slot a lookup-key write could fill.
    """

    def __init__(self, max_examples: int):
        self.max_examples = max_examples
        self.attested: set[str] = set()
        self.refs: list[tuple[str, str]] = []
        self.counts = {"misplaced": 0, "lane_created": 0}
        self.examples: dict[str, list[str]] = {"misplaced": [], "lane_created": [],
                                               "unattested_refs": [], "dangling_refs": []}

    def _note(self, kind: str, example: str) -> None:
        self.examples[kind] = (self.examples[kind] + [example])[: self.max_examples]

    def observe(self, rel: str, fm: dict) -> None:
        """Record one page (``rel`` is wiki-relative without ``.md``)."""
        if carries_attestation(fm):
            self.attested.add(rel)
            if misplaced(fm):
                self.counts["misplaced"] += 1
                self._note("misplaced", f"{rel} (type {fm.get('type')!r})")
            if _present(fm.get("producer_lane")):
                self.counts["lane_created"] += 1
                self._note("lane_created", f"{rel} (producer_lane {fm.get('producer_lane')})")
        for field in REFERENCE_FIELDS:
            if _present(fm.get(field)):
                self.refs.extend((rel, target) for target in _refs(fm[field]))

    def result(self, existing: set[str]) -> dict:
        """The findings, resolving references against every page path in the vault."""
        pages: set[str] = set()
        dangling: set[str] = set()
        for rel, target in self.refs:
            if target not in existing:
                if rel not in dangling:
                    dangling.add(rel)
                    self._note("dangling_refs", f"{rel} → {target}")
            elif target not in self.attested and rel not in pages:
                pages.add(rel)
                self._note("unattested_refs", f"{rel} → {target}")
        return {
            "misplaced_attestation_pages": self.counts["misplaced"],
            "misplaced_attestation_examples": self.examples["misplaced"],
            "lane_created_attestation_pages": self.counts["lane_created"],
            "lane_created_attestation_examples": self.examples["lane_created"],
            "unattested_operator_ref_pages": len(pages),
            "unattested_operator_ref_examples": self.examples["unattested_refs"],
            "dangling_operator_ref_pages": len(dangling),
            "dangling_operator_ref_examples": self.examples["dangling_refs"],
        }
