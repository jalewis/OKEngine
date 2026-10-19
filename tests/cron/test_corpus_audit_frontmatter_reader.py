"""Regression (okengine#880): the corpus audit reads only frontmatter, and still gets the same answer.

The corpus-audit cron timed out at 600s on a deployment with 226,612 raw captures (18.8 GB): the raw
pass read every file whole and parsed its frontmatter with the pure-Python YAML loader. The fix
streams each file only up to its closing `---` and parses with the C loader when it is installed,
falling back to `yaml.safe_load` whenever the C loader refuses a document.

These tests pin the two halves of that contract: the audit's answer is unchanged (compared against
the pre-#880 reader, kept verbatim below as the reference), and the reader never touches a body.
"""
from __future__ import annotations

import importlib.util
import io
import random
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "corpus_audit.py"


def _load():
    sys.path.insert(0, str(MOD.parent))
    spec = importlib.util.spec_from_file_location("corpus_audit", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["corpus_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ca():
    return _load()


def _reference_frontmatter(path: Path, fm_re) -> dict | None:
    """The pre-#880 reader, verbatim: whole-file read + pure-Python safe_load."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = fm_re.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


# A JSON-style surrogate-pair `\u` escape inside a double-quoted scalar: the pure loader accepts
# it, the C loader rejects it (a real raw capture tripped exactly this). The pure loader's verdict
# is the one the audit has always reported, so it must still be the one reported.
INVALID_ESCAPE = '---\ntype: source\nurl: https://example.com/esc\ntitle: "smile \\ud83d\\ude00"\n---\nBody.\n'

FIXTURES: dict[str, bytes] = {
    "normal.md": b"---\ntype: source\nurl: https://example.com/a\nsource_type: news\n---\n\nBody.\n",
    "dup.md": b"---\ntype: source\nurl: http://www.example.com/a/\n---\nBody.\n",
    "crlf.md": b"---\r\ntype: source\r\nurl: https://example.com/crlf\r\n---\r\nBody.\r\n",
    "old-mac.md": b"---\rtype: source\rurl: https://example.com/cr\r---\rBody.\r",
    "no-frontmatter.md": b"# Just a heading\n\nurl: https://example.com/not-frontmatter\n",
    "bom.md": "﻿---\ntype: source\nurl: https://example.com/bom\n---\n".encode(),
    "malformed.md": b"---\ntitle: 'unclosed\nurl: https://example.com/m\n---\n\nBody.\n",
    "unterminated.md": b"---\ntype: source\nurl: https://example.com/open\n",
    "scalar.md": b"---\njust a string\n---\nBody.\n",
    "empty-fm.md": b"---\n---\nBody.\n",
    "blank-lead.md": b"---\n\n---\nurl: https://example.com/lead\n---\nBody.\n",
    "huge-body.md": (b"---\ntype: source\nurl: https://example.com/huge\n---\n"
                     + b"lorem ipsum dolor sit amet\n" * 200_000),
    "invalid-escape.md": INVALID_ESCAPE.encode(),
    "empty.md": b"",
}


def _tree(root: Path) -> Path:
    for name, data in FIXTURES.items():
        p = root / "raw" / "2026" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    src = root / "wiki" / "sources" / "a.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("---\ntype: source\nurl: https://example.com/a\n---\nBody.\n", encoding="utf-8")
    return root


def test_raw_capture_health_is_identical_to_the_pre_880_reader(ca, tmp_path):
    vault = _tree(tmp_path)
    got = ca.raw_capture_health(vault)
    want = ca.corpus_audit_inputs.raw_capture_health(
        vault, None,
        frontmatter=lambda p: _reference_frontmatter(p, ca._FM_RE),
        provenance=ca.provenance_lib, novel=ca.NOVEL, max_examples=ca.MAX_EXAMPLES,
    )
    assert got == want
    # Pinned from the pre-#880 implementation, so an equal-but-both-wrong reference cannot pass.
    assert got["captures"] == 7
    assert got["distinct_urls"] == 6
    assert got["duplicate_captures"] == 1
    assert got["unpromoted_urls"] == 5
    # no-frontmatter, bom, malformed, unterminated, scalar, empty-fm, empty: each still counted.
    assert got["unparseable_captures"] == 7


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_each_fixture_parses_exactly_as_before(ca, tmp_path, name):
    p = tmp_path / name
    p.write_bytes(FIXTURES[name])
    assert ca._frontmatter(p) == _reference_frontmatter(p, ca._FM_RE), name


_FUZZ_LINES = ["---", "--- ", "---\t", "----", "---x", "", "  ", "\t", "title: x", "url: http://a/b",
               "key: 'unclosed", "- item", "a: [1, 2", "n: 3", " ", "d: 2026-01-02", "---  "]


def test_fuzzed_documents_match_the_reference_reader(ca, tmp_path):
    """Randomized line soups around every opener/closer/whitespace shape the frontmatter regex
    distinguishes, joined with each newline convention read_text translates."""
    rng = random.Random(880)
    p = tmp_path / "fuzz.md"
    for i in range(3000):
        lines = (["---"] if rng.random() < 0.85 else []) + [
            rng.choice(_FUZZ_LINES) for _ in range(rng.randint(0, 9))]
        sep = rng.choice(["\n", "\r\n", "\r"])
        text = sep.join(lines) + (sep if rng.random() < 0.7 else "")
        p.write_bytes(text.encode("utf-8"))
        assert ca._frontmatter(p) == _reference_frontmatter(p, ca._FM_RE), repr(text)


def test_a_body_that_is_not_utf8_is_never_read(ca, tmp_path):
    """The old reader decoded the whole file, so a binary body crashed the audit. The frontmatter
    is all the audit uses; bytes after the closing `---` must not matter."""
    p = tmp_path / "binary-body.md"
    p.write_bytes(b"---\ntype: source\nurl: https://example.com/bin\n---\n\xff\xfe\x80 not utf-8\n")
    assert ca._frontmatter(p) == {"type": "source", "url": "https://example.com/bin"}


class _CountingPath(type(Path())):
    """A Path that counts the bytes the streaming reader pulls from disk (`bytes_read`) and records
    each fall-back to a whole-file read (`fallbacks`) separately."""
    bytes_read = 0
    fallbacks = 0

    @classmethod
    def reset(cls):
        cls.bytes_read = 0
        cls.fallbacks = 0

    def read_text(self, *a, **k):
        type(self).fallbacks += 1
        return Path(self).read_text(*a, **k)

    def open(self, mode="r", *a, **k):  # noqa: A003
        assert "b" in mode, "the frontmatter reader must open in binary"
        counter = type(self)

        class _Reader(io.BytesIO):
            def readline(self, size=-1):
                line = super().readline(size)
                counter.bytes_read += len(line)
                return line

            def read(self, size=-1):
                chunk = super().read(size)
                counter.bytes_read += len(chunk)
                return chunk

        return _Reader(Path(self).read_bytes())


def test_reader_stops_at_the_closing_fence_of_a_large_file(ca, tmp_path):
    """Deterministic performance guard: bytes read, not wall-clock. A 5 MB capture costs its
    frontmatter (under 100 bytes here), not its body."""
    fm = b"---\ntype: source\nurl: https://example.com/big\n---\n"
    p = tmp_path / "big.md"
    p.write_bytes(fm + b"x" * (5 * 1024 * 1024) + b"\n")
    _CountingPath.reset()
    assert ca._frontmatter(_CountingPath(p)) == {"type": "source", "url": "https://example.com/big"}
    assert _CountingPath.bytes_read == len(fm)
    assert _CountingPath.fallbacks == 0


def test_a_long_first_line_without_a_fence_is_not_read_to_the_end(ca, tmp_path):
    p = tmp_path / "one-line.md"
    p.write_bytes(b"x" * (5 * 1024 * 1024))
    _CountingPath.reset()
    assert ca._frontmatter(_CountingPath(p)) is None
    assert _CountingPath.bytes_read == ca.corpus_audit_inputs.FRONTMATTER_READ_CAP + 1
    assert _CountingPath.fallbacks == 0


def test_frontmatter_larger_than_the_cap_falls_back_to_the_whole_file(ca, tmp_path, monkeypatch):
    inputs = ca.corpus_audit_inputs
    monkeypatch.setattr(inputs, "FRONTMATTER_READ_CAP", 70)
    p = tmp_path / "long-fm.md"
    p.write_bytes(b"---\n" + b"".join(b"k%d: %d\n" % (i, i) for i in range(50)) + b"---\nBody.\n")
    got = ca._frontmatter(p)
    assert got == _reference_frontmatter(p, ca._FM_RE)
    assert len(got) == 50


def test_c_loader_refusal_falls_back_to_the_pure_loader(ca, tmp_path, monkeypatch):
    """Whatever the C loader rejects, the pure loader decides -- the pre-#880 verdict."""
    inputs = ca.corpus_audit_inputs

    class _Refuses(yaml.SafeLoader):
        def __init__(self, stream):
            raise yaml.YAMLError("C loader refused")

    monkeypatch.setattr(inputs, "_FAST_LOADER", _Refuses)
    p = tmp_path / "esc.md"
    p.write_text(INVALID_ESCAPE, encoding="utf-8")
    assert ca._frontmatter(p) == _reference_frontmatter(p, ca._FM_RE)
    bad = tmp_path / "bad.md"
    bad.write_bytes(FIXTURES["malformed.md"])
    assert ca._frontmatter(bad) == {}, "a document BOTH loaders reject is still a parse error"


def test_the_fast_loader_is_used_when_it_accepts(ca, tmp_path, monkeypatch):
    inputs = ca.corpus_audit_inputs
    calls = []

    class _Accepts(yaml.SafeLoader):
        def __init__(self, stream):
            calls.append(stream)
            super().__init__(stream)

    monkeypatch.setattr(inputs, "_FAST_LOADER", _Accepts)
    monkeypatch.setattr(inputs.yaml, "safe_load", lambda *_: pytest.fail("pure loader used"))
    p = tmp_path / "ok.md"
    p.write_bytes(FIXTURES["normal.md"])
    assert ca._frontmatter(p)["url"] == "https://example.com/a"
    assert len(calls) == 1


@pytest.mark.skipif(not hasattr(yaml, "CSafeLoader"),
                    reason="libyaml not built into this PyYAML; the fallback path is covered by "
                           "test_c_loader_refusal_falls_back_to_the_pure_loader")
def test_real_c_loader_matches_on_the_invalid_escape_capture(ca, tmp_path):
    assert ca.corpus_audit_inputs._FAST_LOADER is yaml.CSafeLoader
    p = tmp_path / "esc.md"
    p.write_text(INVALID_ESCAPE, encoding="utf-8")
    assert ca._frontmatter(p) == _reference_frontmatter(p, ca._FM_RE)


# Documents the C loader ACCEPTS but the pure loader REJECTS (or reads differently). A fallback on
# C failure cannot catch these, so they must never be handed to the C loader at all. Each was
# found by differential fuzzing of the two loaders.
C_WOULD_DIVERGE = [
    "url: a\ntitle: x\t\n",               # trailing tab
    "a:\tb\n",                            # tab as a separator
    "---\t\nurl: a\n",                    # tab after a document marker
    "a: |#\n",                            # block-scalar header glued to a comment
    "a: >-#\n",
    "{url: https://x.io/a?b=1}\n",        # `?` in a flow plain scalar
    "tags: [b?]\n",
    "tags: [It's here?]\n",               # a mid-scalar quote is not a quoted scalar
    "tags: ['a', b?]\n",
    "a: !\n",                             # bare tag: C reads '', pure reads None
    "{!, a}\n",
    "a: b\u2028c\n",                      # odd line break
    "a: \ufeffb\n",                       # BOM inside the document
]

# Real-capture shapes that must stay on the fast path, or the fix buys nothing.
C_SAFE_REAL_SHAPES = [
    'type: source\nurl: "https://news.ycombinator.com/item?id=1"\ntags: ["a", "b c"]\n',
    'source_native_id: "https://www.example.com/?p=141496"\nauthors: ["x@y (Z W)"]\n',
    'links: ["https://x/a?b=1&c=2", "https://y/"]\ntitle: Breaking! News\n',
    "k: ['it''s', \"q\\\"?\"]\n",
]


@pytest.mark.parametrize("doc", C_WOULD_DIVERGE)
def test_documents_the_c_loader_would_misread_are_routed_to_the_pure_loader(ca, doc):
    assert not ca.corpus_audit_inputs._c_loader_safe(doc), repr(doc)


@pytest.mark.parametrize("doc", C_SAFE_REAL_SHAPES)
def test_real_capture_shapes_stay_on_the_fast_path(ca, doc):
    assert ca.corpus_audit_inputs._c_loader_safe(doc), repr(doc)


# A differential contract between two YAML implementations: thousands of generated inputs cost
# seconds of CPU, far over the 100ms unit budget, so it runs in the contract layer.
@pytest.mark.contract
@pytest.mark.skipif(not hasattr(yaml, "CSafeLoader"),
                    reason="libyaml not built into this PyYAML: no C loader to diverge from; "
                           "routing is still pinned by the two tests above")
def test_c_loader_path_matches_safe_load_under_differential_fuzz(ca):
    """_safe_load must give exactly yaml.safe_load's answer (value or YAMLError) on any input."""
    inputs = ca.corpus_audit_inputs
    assert inputs._FAST_LOADER is yaml.CSafeLoader
    toks = ["a", ": ", "b", "\n", "  ", "- ", "'", '"', "[", "]", "{", "}", ",", "#", " ", "?",
            "\\u00e9", "\\ud83d", "\\", "&x", "*x", "!!str ", "!", "|", "> ", "|-", "%", "`", "---",
            "...", "1", "2026-01-02", "~", "null", "yes", "<<", "\t", "\u2028", "\ufeff", "é",
            "\u00a0", '"x?y"', "'it''s?'", '["https://x/?p=1", "b"]', " #c?]\n", "\n  "]

    def outcome(fn, s):
        try:
            return ("ok", repr(fn(s)))
        except yaml.YAMLError:
            return ("err", None)

    rng = random.Random(8800)
    for _ in range(40_000):
        s = "".join(rng.choice(toks) for _ in range(rng.randint(1, 14)))
        assert outcome(inputs._safe_load, s) == outcome(yaml.safe_load, s), repr(s)


# ---------------------------------------------------------------------------------------------
# The routing scanner, clause by clause. Each case states which way a document must go; a wrong
# answer in the "safe" direction would hand a C-divergent document to the C loader.
# ---------------------------------------------------------------------------------------------

QUESTION_IN_FLOW = [
    ("a: https://x/?p=1\n", False, "a `?` outside any flow collection is harmless"),
    ("a: [b?]\n", True, "a plain `?` inside a flow sequence"),
    ("a: {b: c?}\n", True, "a plain `?` inside a flow mapping"),
    ("a: [[b], c?]\n", True, "closing an inner collection leaves the outer one open"),
    ("a: [b]\nc: d?\n", False, "a `?` after the flow collection closed"),
    ("a: ]x?\n", False, "a stray closer never drives the depth below zero"),
    ("a: ]]\nb: [c?]\n", True, "stray closers do not cancel a later opener"),
    ('a: ["b?"]\n', False, "a `?` inside a double-quoted scalar"),
    ("a: ['b?']\n", False, "a `?` inside a single-quoted scalar"),
    ('a: ["b\\"?"]\n', False, "an escaped double quote does not close the scalar"),
    ('a: ["b\\\\"?]\n', True, "an escaped backslash does not escape the closing quote"),
    ("a: ['it''s?']\n", False, "'' is an escaped quote, not a close-and-reopen"),
    ("a: ['b'?]\n", True, "after the quoted scalar closes, a `?` is back in the flow"),
    ('a: ["b?\n', True, "an unterminated quoted scalar is not trusted"),
    ('a: [b"?"]\n', True, "a quote in the middle of a plain scalar is a literal character"),
    ('a: [b, "c?"]\n', False, "a quote after `,` opens a quoted scalar"),
    ('a: {b: "c?"}\n', False, "a quote after `: ` opens a quoted scalar"),
    ('a: {b:"c?"}\n', True, "a quote glued to `:` does not"),
    ('a: [\n  "b?"]\n', False, "whitespace and newlines between the opener and the quote"),
    ("a: [b, # c?]\n  d]\n", False, "a comment inside a flow collection is skipped"),
    ("a: [b, # c?]\n  d?]\n", True, "the comment ends at the end of its line"),
    ("a: [b#c?]\n", True, "a `#` glued to a scalar is not a comment"),
    ("a: [b\n# c?]", False, "a comment running to the end of the text"),
    ("a: [b, # c\n  d?]\n", True, "a `?` on the line after a comment"),
    ("a: [[[b]], c?]\n", True, "each closer drops the depth by exactly one"),
    ("a: [b# c?]\n", True, "`#` glued BEFORE a space is still not a comment"),
    ("a: [b, x# c?]\n", True, "a comment needs whitespace immediately before the `#`"),
    ("a: ['b\\', 'c?']\n", False, "a backslash does not escape inside single quotes"),
    ('a: ["[", "?"]\n', False, "a closing quote is not re-read as an opening one"),
    ('a: {bb: "c?"}\n', False, "a quote after `: ` opens a quoted scalar (other offset)"),
    ('a: {b:\n  "c?"}\n', False, "the whitespace after `:` may be a newline"),
]


@pytest.mark.parametrize("doc,expected,why", QUESTION_IN_FLOW, ids=[c[2] for c in QUESTION_IN_FLOW])
def test_question_in_flow_scanner(ca, doc, expected, why):
    # Also shifted by one character, so no case depends on an index happening to be odd or even.
    for text in (doc, "z" + doc):
        assert ca.corpus_audit_inputs._question_in_flow(text) is expected, (why, text)


C_UNSAFE_CHARS = [
    ("\x00", True), ("\x08", True), ("\t", True), ("\x0b", True), ("\x1f", True), ("\x7f", True),
    ("\x9f", True), ("\u2028", True), ("\u2029", True), ("\ufeff", True),
    (" ", False), ("\xa0", False), ("é", False), ("\u2019", False), ("~", False),
]


@pytest.mark.parametrize("ch,unsafe", C_UNSAFE_CHARS, ids=[repr(c[0]) for c in C_UNSAFE_CHARS])
def test_character_routing_boundaries(ca, ch, unsafe):
    assert ca.corpus_audit_inputs._c_loader_safe(f"a: b{ch}c\n") is (not unsafe)


TAG_AND_HEADER = [
    ("!x\n", True), ("a: !x\n", True), ("a: [!x]\n", True), ("a: [b,!x]\n", True),
    ("a: {!x: b}\n", True), ("a: [b]!\n", True), ("a: {b}!\n", True), ("a:\n!x\n", True),
    ("a: Breaking!\n", False), ("a: b!c\n", False),
    ("a: |#\n", True), ("a: >#\n", True), ("a: |-#\n", True), ("a: >+2#\n", True),
    ("a: |9#\n", True), ("a: | #\n", False), ("a: x#y\n", False),
]


@pytest.mark.parametrize("doc,unsafe", TAG_AND_HEADER, ids=[c[0] for c in TAG_AND_HEADER])
def test_tag_and_block_header_routing(ca, doc, unsafe):
    assert ca.corpus_audit_inputs._c_loader_safe(doc) is (not unsafe)


def test_the_pure_loader_parses_what_the_c_loader_must_not_see(ca, monkeypatch):
    """A routed document never reaches the fast loader, even one it would happily accept."""
    inputs = ca.corpus_audit_inputs

    class _MustNotRun(yaml.SafeLoader):
        def __init__(self, stream):
            pytest.fail("C loader handed a document it can misread")

    monkeypatch.setattr(inputs, "_FAST_LOADER", _MustNotRun)
    with pytest.raises(yaml.YAMLError):
        inputs._safe_load("a:\tb\n")  # the pure loader's verdict: tabs are not separators
    assert inputs._safe_load("a: !\n") == {"a": None}  # pure reads a bare tag as null, C as ''


def test_without_a_c_loader_the_pure_loader_decides(ca, monkeypatch):
    inputs = ca.corpus_audit_inputs
    monkeypatch.setattr(inputs, "_FAST_LOADER", None)
    assert inputs._safe_load("a: 1\n") == {"a": 1}
    with pytest.raises(yaml.YAMLError):
        inputs._safe_load("a: 'unclosed\n")


def _fm_of_size(size: int) -> bytes:
    """A frontmatter block (opener through closer) of exactly `size` bytes."""
    head, tail = b"---\nk: ", b"\n---\n"
    return head + b"v" * (size - len(head) - len(tail)) + tail


def test_the_documented_cap_streams_256_kib_and_falls_back_one_byte_later(ca, tmp_path):
    """The cap is part of the contract: a frontmatter of exactly 256 KiB is still streamed, one byte
    more is read the old way. Both give the same answer."""
    cap = 256 * 1024
    assert ca.corpus_audit_inputs.FRONTMATTER_READ_CAP == cap
    for size, fallbacks in ((cap, 0), (cap + 1, 1)):
        p = tmp_path / f"fm-{size}.md"
        p.write_bytes(_fm_of_size(size) + b"Body.\n")
        _CountingPath.reset()
        got = ca._frontmatter(_CountingPath(p))
        assert got == {"k": "v" * (size - 12)}, size
        assert _CountingPath.fallbacks == fallbacks, size


@pytest.mark.parametrize("size,fallbacks", [(69, 0), (70, 0), (71, 1), (72, 1)])
def test_cap_boundary_is_exact(ca, tmp_path, monkeypatch, size, fallbacks):
    monkeypatch.setattr(ca.corpus_audit_inputs, "FRONTMATTER_READ_CAP", 70)
    p = tmp_path / "fm.md"
    p.write_bytes(_fm_of_size(size) + b"Body " * 100 + b"\n")
    _CountingPath.reset()
    assert ca._frontmatter(_CountingPath(p)) == {"k": "v" * (size - 12)}
    assert _CountingPath.fallbacks == fallbacks
    assert _CountingPath.bytes_read == min(size, 71)


@pytest.mark.parametrize("opener", [b"---\n", b"---    \n"])  # 4 and 8 bytes before the long line
def test_a_long_line_inside_the_frontmatter_is_read_only_up_to_the_cap(
        ca, tmp_path, monkeypatch, opener):
    """The limit on each read is what is LEFT of the cap, whatever the earlier lines consumed."""
    monkeypatch.setattr(ca.corpus_audit_inputs, "FRONTMATTER_READ_CAP", 70)
    p = tmp_path / "long-line.md"
    p.write_bytes(opener + b"k: " + b"v" * 10_000 + b"\n---\n")
    _CountingPath.reset()
    assert ca._frontmatter(_CountingPath(p)) == {"k": "v" * 10_000}
    assert _CountingPath.bytes_read == 71, "the streaming read stops at cap + 1 bytes"
    assert _CountingPath.fallbacks == 1


def test_lone_cr_frontmatter_stops_inside_the_first_physical_line(ca, tmp_path):
    """Old-Mac newlines put the whole frontmatter in one binary line; the closer inside it must end
    the read there, not after the next `\\n` somewhere in the body."""
    fm = b"---\rtype: source\rurl: https://example.com/cr\r---\rBody.\r"
    p = tmp_path / "cr.md"
    p.write_bytes(fm + b"x" * 5000 + b"\n" + b"y" * 5000 + b"\n")
    _CountingPath.reset()
    assert ca._frontmatter(_CountingPath(p)) == {"type": "source", "url": "https://example.com/cr"}
    assert _CountingPath.bytes_read == len(fm) + 5001
