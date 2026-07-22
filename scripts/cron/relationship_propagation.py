"""Domain-neutral deterministic inverse-relationship propagation."""
from __future__ import annotations


def reconcile(pages: dict[str, dict], rules: list[dict]) -> dict[str, dict]:
    """Return field-only updates implied by pack-declared bidirectional rules.

    A rule has ``left_field`` and ``right_field``. Values are canonical page keys.
    If either side declares the relationship, the missing inverse is added.
    """
    updates: dict[str, dict] = {}
    for rule in rules:
        left, right = rule["left_field"], rule["right_field"]
        for source_key, page in pages.items():
            for target_key in _values(page.get(left)):
                if target_key in pages and source_key not in _values(pages[target_key].get(right)):
                    updates.setdefault(target_key, {}).setdefault(right, []).append(source_key)
            for target_key in _values(page.get(right)):
                if target_key in pages and source_key not in _values(pages[target_key].get(left)):
                    updates.setdefault(target_key, {}).setdefault(left, []).append(source_key)
    return updates


def _values(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []
