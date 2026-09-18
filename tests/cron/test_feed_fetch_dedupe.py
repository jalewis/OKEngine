"""Regression: a rotating feed GUID must not re-capture the same article forever.

One publisher re-emits the same article with a fresh `?p=<n>` guid on every fetch. Keyed on the
guid alone, `first_seen` was true every run: the article was re-captured on every pass, into every
category directory it matched. One URL reached 555 captures and the raw tree held 10,429 redundant
files — which inflated every count derived from it and made an ingest backlog look ~8x larger than
it was.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "feed_fetch.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="feed_fetch absent")


def _load():
    sys.path.insert(0, str(MOD.parent))
    spec = importlib.util.spec_from_file_location("feed_fetch", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["feed_fetch"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_a_rotating_cache_buster_normalises_to_one_identity():
    mod = _load()
    a = mod._dedupe_link("https://www.example.com/blog/an-article/?p=753404")
    b = mod._dedupe_link("http://example.com/blog/an-article?p=520860")
    assert a == b == "example.com/blog/an-article"


def test_scheme_www_fragment_and_trailing_slash_are_all_noise():
    mod = _load()
    assert (mod._dedupe_link("HTTPS://WWW.Example.com/a/b/#section")
            == mod._dedupe_link("http://example.com/a/b"))


def test_distinct_articles_keep_distinct_identities():
    """Normalisation must not collapse two real articles into one, which would DROP coverage."""
    mod = _load()
    assert mod._dedupe_link("https://example.com/a") != mod._dedupe_link("https://example.com/b")


def test_a_query_string_that_is_not_a_cache_buster_is_preserved():
    """Only the `p=<digits>` feed cache-buster is stripped; real query params identify content."""
    mod = _load()
    assert "id=42" in mod._dedupe_link("https://example.com/view?id=42")


def test_the_link_key_is_recorded_so_a_rotated_guid_cannot_recapture():
    """Recording only the guid key would leave the next rotation looking brand new."""
    src = MOD.read_text(encoding="utf-8")
    assert 'seen[it["_seen_link_key"]] = now' in src
    assert 'and not (link_key and link_key in seen)' in src
