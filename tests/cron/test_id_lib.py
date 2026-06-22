"""P1 regression: the engine id normalizer is deterministic, ascii-folding, and
collision-resistant — the contract that lets independent packs converge on one id.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "cron" / "id_lib.py"


def _load():
    spec = importlib.util.spec_from_file_location("id_lib", LIB)
    m = importlib.util.module_from_spec(spec)
    sys.modules["id_lib"] = m
    spec.loader.exec_module(m)
    return m


idl = _load()


# (raw input, expected key) — the normalizer's pinned test vectors.
NORMALIZE_VECTORS = [
    ("Acme Corp", "acme-corp"),
    ("  ACME   Corp  ", "acme-corp"),          # trim + collapse whitespace + lowercase
    ("café", "cafe"),                          # ascii-fold (combining accent)
    ("naïve coöp", "naive-coop"),
    ("Foo / Bar : Baz", "foo-bar-baz"),        # punctuation incl. the ':' delimiter → hyphen
    ("a---b__c", "a-b-c"),                      # collapse runs of non-alnum
    ("-leading-and-trailing-", "leading-and-trailing"),
    ("CVE-2024-12345", "cve-2024-12345"),
    ("T1059.001", "t1059-001"),                # MITRE sub-technique dot → hyphen
]


def test_normalize_vectors():
    for raw, expected in NORMALIZE_VECTORS:
        assert idl.normalize_key(raw) == expected, raw


def test_normalize_is_deterministic_and_idempotent():
    for raw, expected in NORMALIZE_VECTORS:
        assert idl.normalize_key(raw) == idl.normalize_key(raw)
        assert idl.normalize_key(expected) == expected   # normalizing a key is a no-op


def test_empty_or_unicode_only_falls_back_to_hash():
    for raw in ("", "   ", "：（）", "日本語", "Ωμέγα- только"[:6]):
        k = idl.normalize_key(raw)
        if raw.strip() and not any(c.isascii() and c.isalnum() for c in raw):
            assert k.startswith("x-") and len(k) > 2   # deterministic hash fallback
        assert ":" not in k                            # never contains the delimiter
    # deterministic
    assert idl.normalize_key("日本語") == idl.normalize_key("日本語")


def test_overlong_key_is_truncated_with_hash():
    raw = "word " * 60                               # ~300 chars
    k = idl.normalize_key(raw)
    assert len(k) <= idl._MAX_KEY_LEN + 7            # cap + "-" + 6 hex
    # two distinct long inputs that share an 80-char prefix don't collide
    a = idl.normalize_key("x" + "y" * 100 + "AAA")
    b = idl.normalize_key("x" + "y" * 100 + "BBB")
    assert a != b


def test_make_id_and_authority_id():
    assert idl.make_id("mitre", "T1059") == "mitre:t1059"
    assert idl.authority_id("MITRE", "T1059.001") == "mitre:t1059-001"
    assert idl.make_id("CVE", "CVE-2024-12345") == "cve:cve-2024-12345"


def test_authority_convergence_across_raw_forms():
    # the whole point: differing raw spellings of one authority id → one id
    forms = ["T1059", "t1059", "  T1059  ", "T-1059"]
    ids = {idl.authority_id("mitre", f) for f in forms[:3]}
    assert ids == {"mitre:t1059"}                    # first three converge


def test_parse_id_roundtrip():
    assert idl.parse_id("mitre:t1059") == ("mitre", "t1059")
    assert idl.parse_id("no-delimiter") == ("", "no-delimiter")
    # split on the FIRST colon only
    assert idl.parse_id("a:b:c") == ("a", "b:c")


def test_is_id():
    assert idl.is_id("mitre:t1059")
    assert idl.is_id(idl.make_id("entities", "Acme Corp"))
    assert not idl.is_id("noscope")
    assert not idl.is_id("Mitre:T1059")              # not normalized form
    assert not idl.is_id(":k")
    assert not idl.is_id(None)


def test_norm_version_present():
    assert isinstance(idl.NORM_VERSION, int) and idl.NORM_VERSION >= 1


def test_natural_key_prefers_title_then_name_then_fallback():
    assert idl.natural_key({"title": "Acme Corp", "name": "x"}) == "Acme Corp"
    assert idl.natural_key({"name": "Acme"}) == "Acme"
    assert idl.natural_key({"title": "  "}, fallback="acme-corp") == "acme-corp"
    assert idl.natural_key({}, fallback="stem") == "stem"


def test_derive_id_authority_vs_minted_slug():
    # authority + local id -> convergent authority id
    pid, kind = idl.derive_id(authority="mitre", local_id="T1059",
                              minted_scope="entities", slug_source="ignored")
    assert (pid, kind) == ("mitre:t1059", "authority")
    # no authority -> minted slug scoped to the creation namespace
    pid, kind = idl.derive_id(authority=None, local_id=None,
                              minted_scope="entities", slug_source="Acme Corp")
    assert (pid, kind) == ("entities:acme-corp", "slug")
    # authority declared but local id missing -> falls back to a minted slug
    pid, kind = idl.derive_id(authority="mitre", local_id="",
                              minted_scope="entities", slug_source="Acme Corp")
    assert kind == "slug" and pid == "entities:acme-corp"
