"""How a list field stored as one scalar string becomes a list (okengine#196, okengine#1005).

A schema-declared list field is sometimes written as a single string. What its commas mean is a
property of the FIELD, declared in the schema's ``field_shapes``, not guessed from the value:

* ``list`` -- an identifier or reference list (``aliases``, ``sources``, ``tags``, ...). The string
  is a delimited list and splits on its commas, as okengine#196 always did, so long source paths,
  URLs and aliases such as ``Adobe Inc., Adobe Systems`` keep splitting exactly as before;
* ``prose-list`` -- a list whose items are sentences (an assessment's ``assumptions``). A scalar
  is ONE item, ``[whole string]``, and is never split: its commas belong to the sentence.

Two kinds of comma are never delimiters in a ``list`` field either, because splitting at them cuts
one item in half: a comma inside brackets (``(Nissan, Oracle Health)``), and a comma inside a date
or a number (``June 29, 2026``, ``1,000``). A ``list`` field that holds prose by mistake (live:
prediction ``basis``, a source-ref list, carrying four prose values) is then kept whole instead of
shredded into fragments with unbalanced parentheses, while every real token list still splits.

Both boundaries that coerce such a scalar call ``coerce_list_scalar``: the enforced write path
(``write_services.validation._normalize_refs``) and the legacy-shape repair lane, which takes its
canonical value from the write path. Boundaries that disagree would let one of them re-shred what
the other kept whole. Empty items carry no data and are dropped.
"""
from __future__ import annotations

import re

#: The ``field_shapes`` value that declares a list of prose items.
PROSE_LIST = "prose-list"

_NUMBER_COMMA = re.compile(r"(?<=\d),(?=\d)")
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DATE_COMMA = re.compile(rf"\b{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(,)\s*\d{{4}}\b", re.I)


def _protected(text: str) -> set[int]:
    """Offsets of the commas inside a number or a date: never item delimiters."""
    offsets = {match.start() for match in _NUMBER_COMMA.finditer(text)}
    return offsets | {match.start(1) for match in _DATE_COMMA.finditer(text)}


def split_list_scalar(text: str) -> list[str]:
    """The items of a ``list`` field's scalar: split on every comma outside brackets, a date and
    a number."""
    protected = _protected(text)
    items, depth, start = [], 0, 0
    for offset, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif char == "," and not depth and offset not in protected:
            items.append(text[start:offset])
            start = offset + 1
    items.append(text[start:])
    return [item.strip() for item in items if item.strip()]


def coerce_list_scalar(text: str, prose: bool) -> list[str]:
    """The list a list field's scalar `text` stands for: one item when the field is declared a
    ``prose-list`` (`prose`), else its delimited items."""
    if prose:
        whole = text.strip()
        return [whole] if whole else []
    return split_list_scalar(text)
