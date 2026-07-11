#!/usr/bin/env python3
"""converge — the page+field-scoped merge arbitration for converge-on-write.

When two packs write the same (authority-id) page, the engine merges instead of
duplicating (RFC docs/design/composable-okpacks.md §5a). Ownership is **page +
field scoped**, NOT declared-field scoped (the original "owner-wins declared /
last-writer extras" rule inverted its own promise):

  - the **owner** of the page's type may set/change ANY field;
  - a **non-owner** may **add new keys** (attributed) and may **change only fields
    it owns** via an explicit `field_owners` grant (e.g. a hunt pack owns
    `detection` on an `attack-pattern` owned by an attack pack);
  - a non-owner changing a field it does NOT own is a **conflict** — left
    unchanged and surfaced for review, never a silent clobber.

`merge_frontmatter` is a pure function of its inputs (no I/O), so it is trivially
test-vectored. Owner-authorized field *removal* is a separate operation (not a
merge of provided fields) and lives in the write path, not here. Tombstoned-id
handling is the caller's job (never resurrect — §5a).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Server-managed keys the merge never treats as a pack-authored conflict — the
# write path stamps/owns these regardless of caller.
_SERVER_KEYS = {"id", "version", "updated", "last_modified_by",
                "maintained_by", "discovered_by", "created", "created_by"}

# The subset of server keys that are PROVENANCE — set once at create and never re-stamped by the
# write path. A converge merge must PRESERVE these (never take an incoming value), or a caller forges
# them (invariant-audit M19). The other server keys (id/version/updated/last_modified_by) ARE
# re-stamped after the merge, so passing them through here is harmless and back-compatible.
_PROVENANCE_KEYS = {"created", "created_by", "discovered_by"}


@dataclass
class MergeDecision:
    added: list[str] = field(default_factory=list)        # new keys the caller contributed
    updated: list[str] = field(default_factory=list)      # existing keys the caller (legally) changed
    removed: list[str] = field(default_factory=list)      # existing keys the (authorized) caller dropped
    conflicts: list[tuple[str, object, object]] = field(default_factory=list)  # (key, current, attempted)
    unchanged: list[str] = field(default_factory=list)    # same key, same value

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


def _owns_field(key: str, *, owner_pack, caller_pack, field_owners: dict) -> bool:
    """True iff the caller may mutate an existing `key`. With no declared owner
    (single-pack / no composition) there is no enforcement — changes apply
    (back-compat). Otherwise the caller must own the page (== owner_pack) or hold
    an explicit per-field grant for this key."""
    if owner_pack is None:
        return True
    if caller_pack is not None and caller_pack == owner_pack:
        return True
    return caller_pack is not None and field_owners.get(key) == caller_pack


def merge_frontmatter(prev_fm: dict, incoming_fm: dict, *,
                      owner_pack=None, caller_pack=None,
                      field_owners: dict | None = None,
                      remove: "list[str] | None" = None) -> tuple[dict, MergeDecision]:
    """Merge `incoming_fm` into `prev_fm` under page+field ownership.

    Returns ``(merged_fm, decision)``. New keys are added; same-value keys are
    no-ops; an existing key with a different value is applied only when the caller
    owns it (page owner, or `field_owners[key] == caller_pack`), otherwise it is a
    conflict (current value kept). `remove` lists fields the caller wants dropped —
    permitted ONLY for a field the caller owns (a non-owner removal is a conflict),
    and never for server-managed keys. `maintained_by` unions the caller. Pure:
    never raises, never writes.
    """
    field_owners = field_owners or {}
    merged = dict(prev_fm)
    dec = MergeDecision()

    for key, new_val in incoming_fm.items():
        if key in _SERVER_KEYS:
            # Server-managed; never a pack conflict. PRESERVE provenance keys (created/created_by/
            # discovered_by) — a caller must not forge them, and the write path does NOT re-stamp them
            # (invariant-audit M19). `merged` already carries prev_fm's value, so skip. The rest
            # (id/version/updated/last_modified_by) are re-stamped after merge → pass through as before.
            if key not in _PROVENANCE_KEYS:
                merged[key] = new_val
            continue
        if key not in prev_fm:
            merged[key] = new_val
            dec.added.append(key)
        elif prev_fm[key] == new_val:
            dec.unchanged.append(key)
        elif _owns_field(key, owner_pack=owner_pack, caller_pack=caller_pack,
                         field_owners=field_owners):
            merged[key] = new_val
            dec.updated.append(key)
        else:
            dec.conflicts.append((key, prev_fm[key], new_val))  # keep current

    # Owner-authorized removal (exempt from the field-loss guard): only the owner
    # of a field may drop it; a non-owner removal is a conflict; server keys never.
    for key in (remove or []):
        if key in _SERVER_KEYS or key not in merged:
            continue
        if _owns_field(key, owner_pack=owner_pack, caller_pack=caller_pack,
                       field_owners=field_owners):
            del merged[key]
            dec.removed.append(key)
        else:
            dec.conflicts.append((key, merged[key], "<remove>"))

    if caller_pack is not None:
        prov = merged.get("maintained_by")
        prov = list(prov) if isinstance(prov, (list, tuple)) else ([prov] if prov else [])
        if caller_pack not in prov:
            prov.append(caller_pack)
        merged["maintained_by"] = prov

    return merged, dec
