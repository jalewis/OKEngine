"""okengine.vault_lookup_cache: revalidated hits, expiring misses, a bounded memo (okengine#1024)."""
from __future__ import annotations

import os
import threading

import pytest

from okengine import vault_lookup_cache as vlc


class _Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


class _Calls:
    """Counts resolve/derive calls so a test can tell a memo hit from a recompute."""

    def __init__(self, path):
        self.path = path
        self.resolves = 0
        self.derives = 0

    def resolve(self):
        self.resolves += 1
        return self.path() if callable(self.path) else self.path

    def derive(self, path):
        self.derives += 1
        return path.read_text(encoding="utf-8")


def _settled(path):
    """Push the file's timestamps out of the fingerprint settle window: a stat-only key."""
    old = 1_000_000_000_000_000_000
    os.utime(path, ns=(old, old))


def test_defaults():
    cache = vlc.VaultLookupCache()
    assert (cache.maxsize, cache.miss_ttl) == (vlc.MAXSIZE, vlc.MISS_TTL) == (4096, 60.0)
    assert cache._clock is vlc.time.monotonic


def test_a_hit_is_served_from_the_memo_while_its_file_is_unchanged(tmp_path):
    page = tmp_path / "p.md"
    page.write_text("one", encoding="utf-8")
    _settled(page)
    cache, calls = vlc.VaultLookupCache(), _Calls(page)
    assert cache.lookup("k", calls.resolve, calls.derive) == "one"
    assert cache.lookup("k", calls.resolve, calls.derive) == "one"
    assert (calls.resolves, calls.derives) == (1, 1)


def test_a_rewritten_file_is_read_again(tmp_path):
    page = tmp_path / "p.md"
    page.write_text("one", encoding="utf-8")
    cache, calls = vlc.VaultLookupCache(), _Calls(page)
    assert cache.lookup("k", calls.resolve, calls.derive) == "one"
    page.write_text("two!", encoding="utf-8")
    assert cache.lookup("k", calls.resolve, calls.derive) == "two!"
    assert calls.derives == 2


def test_a_moved_file_is_resolved_again(tmp_path):
    old, new = tmp_path / "a.md", tmp_path / "b.md"
    old.write_text("body", encoding="utf-8")
    where = {"p": old}
    cache, calls = vlc.VaultLookupCache(), _Calls(lambda: where["p"])
    assert cache.lookup("k", calls.resolve, lambda p: p) == old
    old.rename(new)
    where["p"] = new
    assert cache.lookup("k", calls.resolve, lambda p: p) == new
    assert calls.resolves == 2


def test_a_miss_is_remembered_until_its_ttl_then_resolved_again(tmp_path):
    clock = _Clock()
    page = tmp_path / "late.md"
    cache = vlc.VaultLookupCache(miss_ttl=10.0, clock=clock)
    calls = _Calls(lambda: page if page.exists() else None)
    assert cache.lookup("k", calls.resolve, calls.derive, missing="-") == "-"
    page.write_text("here", encoding="utf-8")
    clock.now += 9.999
    assert cache.lookup("k", calls.resolve, calls.derive, missing="-") == "-"
    assert calls.resolves == 1, "a miss inside its TTL does not walk the vault again"
    clock.now += 0.001
    assert cache.lookup("k", calls.resolve, calls.derive, missing="-") == "here"
    assert calls.resolves == 2


def test_a_miss_defaults_to_none():
    cache = vlc.VaultLookupCache()
    assert cache.lookup("k", lambda: None, pytest.fail) is None
    assert len(cache) == 1


def test_a_file_that_vanishes_before_it_is_stamped_is_not_memoized(tmp_path):
    ghost = tmp_path / "ghost.md"
    cache = vlc.VaultLookupCache()
    assert cache.lookup("k", lambda: ghost, lambda p: "derived") == "derived"
    assert len(cache) == 0


def test_the_file_is_stamped_before_it_is_read(tmp_path, monkeypatch):
    """A write between the stamp and the read must not be memoized under the newer stamp."""
    page = tmp_path / "p.md"
    page.write_text("old", encoding="utf-8")
    cache = vlc.VaultLookupCache()

    def derive_racing_a_write(path):
        path.write_text("newer", encoding="utf-8")
        return "old"

    assert cache.lookup("k", lambda: page, derive_racing_a_write) == "old"
    assert cache.lookup("k", lambda: page, lambda p: p.read_text(encoding="utf-8")) == "newer"


def test_the_memo_is_bounded_and_evicts_the_least_recently_used(tmp_path):
    pages = {}
    for name in "abcd":
        pages[name] = tmp_path / f"{name}.md"
        pages[name].write_text(name, encoding="utf-8")
        _settled(pages[name])
    cache = vlc.VaultLookupCache(maxsize=3)
    resolved = []

    def get(name):
        def resolve():
            resolved.append(name)
            return pages[name]
        return cache.lookup(name, resolve, lambda p: p.read_text(encoding="utf-8"))

    get("a"), get("b"), get("c")
    get("a")                      # a hit refreshes `a`, so `b` is now the oldest
    get("d")                      # evicts `b`
    assert len(cache) == 3
    resolved.clear()
    get("a"), get("c"), get("d")
    assert resolved == [], "a, c and d are still memoized"
    get("b")
    assert resolved == ["b"], "b was evicted"


def test_a_stored_miss_counts_toward_the_bound():
    cache = vlc.VaultLookupCache(maxsize=2)
    for n in range(5):
        cache.lookup(n, lambda: None, pytest.fail)
    assert len(cache) == 2


def test_clear_empties_the_memo(tmp_path):
    cache = vlc.VaultLookupCache()
    cache.lookup("k", lambda: None, pytest.fail)
    cache.clear()
    assert len(cache) == 0


def test_an_entry_evicted_while_it_is_being_validated_is_not_resurrected(tmp_path, monkeypatch):
    """`_fresh` drops the lock while it stamps the file; a concurrent clear must win."""
    page = tmp_path / "p.md"
    page.write_text("x", encoding="utf-8")
    cache = vlc.VaultLookupCache()
    cache.lookup("k", lambda: page, lambda p: "x")
    real = vlc.fingerprint

    def clearing_fingerprint(path):
        cache.clear()
        return real(path)

    monkeypatch.setattr(vlc, "fingerprint", clearing_fingerprint)
    assert cache._fresh("k") == (True, "x")
    assert len(cache) == 0


def test_concurrent_lookups_stay_consistent(tmp_path):
    page = tmp_path / "p.md"
    page.write_text("v", encoding="utf-8")
    cache = vlc.VaultLookupCache(maxsize=8)
    errors = []

    def work(n):
        try:
            for i in range(200):
                assert cache.lookup(i % 16, lambda: page, lambda p: "v") == "v"
        except Exception as exc:  # pragma: no cover - only on a failure
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(cache) <= 8


def test_a_miss_long_past_its_ttl_is_resolved_again():
    clock = _Clock()
    cache = vlc.VaultLookupCache(miss_ttl=10.0, clock=clock)
    assert cache.lookup("k", lambda: None, pytest.fail, missing="-") == "-"
    clock.now += 3600.0
    assert cache.lookup("k", lambda: None, pytest.fail, missing="again") == "again"


# --- okengine#1042 [30]/[38]: a hit revalidates the RESOLUTION, not only the file it chose ------

def test_a_hit_is_re_resolved_after_its_ttl_and_follows_a_better_answer(tmp_path):
    """[30]: `apt29` first resolved to an observation copy; an entity page created later is the
    better answer (the resolver prefers entities/). The hit's own file never changed, so the old
    memo served the observation copy until the process restarted."""
    clock = _Clock()
    obs, ent = tmp_path / "obs.md", tmp_path / "ent.md"
    obs.write_text("Observation copy", encoding="utf-8")
    _settled(obs)
    cache = vlc.VaultLookupCache(clock=clock)
    calls = _Calls(lambda: ent if ent.exists() else obs)
    assert cache.lookup("apt29", calls.resolve, calls.derive) == "Observation copy"
    ent.write_text("APT29 entity", encoding="utf-8")
    clock.now += vlc.RESOLVE_TTL - 0.001
    assert cache.lookup("apt29", calls.resolve, calls.derive) == "Observation copy"
    assert calls.resolves == 1, "inside the TTL a settled hit costs one stat, no tree walk"
    clock.now += 0.001
    assert cache.lookup("apt29", calls.resolve, calls.derive) == "APT29 entity"


def test_a_hit_that_became_ambiguous_turns_into_a_miss(tmp_path):
    """[38]: a second page with the same slug makes the resolver answer None (ambiguous). A fresh
    process renders the link unresolved; the cached hit must stop resolving it too."""
    clock = _Clock()
    page = tmp_path / "p.md"
    page.write_text("one", encoding="utf-8")
    _settled(page)
    state = {"ambiguous": False}
    cache = vlc.VaultLookupCache(clock=clock)
    calls = _Calls(lambda: None if state["ambiguous"] else page)
    assert cache.lookup("k", calls.resolve, calls.derive, missing="-") == "one"
    state["ambiguous"] = True
    clock.now += vlc.RESOLVE_TTL
    assert cache.lookup("k", calls.resolve, calls.derive, missing="-") == "-"


def test_a_re_resolved_hit_on_the_same_unchanged_file_is_not_derived_again(tmp_path):
    """Re-resolving keeps the speed: the same answer with an unchanged file reuses the value and
    re-arms the TTL, so the next hit inside it walks nothing."""
    clock = _Clock()
    page = tmp_path / "p.md"
    page.write_text("one", encoding="utf-8")
    _settled(page)
    cache = vlc.VaultLookupCache(clock=clock)
    calls = _Calls(page)
    cache.lookup("k", calls.resolve, calls.derive)
    clock.now += vlc.RESOLVE_TTL
    assert cache.lookup("k", calls.resolve, calls.derive) == "one"
    assert (calls.resolves, calls.derives) == (2, 1)
    clock.now += vlc.RESOLVE_TTL - 0.001
    assert cache.lookup("k", calls.resolve, calls.derive) == "one"
    assert calls.resolves == 2


def test_a_re_resolution_to_the_same_path_with_a_new_file_derives_again(tmp_path):
    clock = _Clock()
    page = tmp_path / "p.md"
    page.write_text("one", encoding="utf-8")
    cache = vlc.VaultLookupCache(clock=clock)
    calls = _Calls(page)
    cache.lookup("k", calls.resolve, calls.derive)
    page.write_text("two!", encoding="utf-8")
    clock.now += vlc.RESOLVE_TTL
    assert cache.lookup("k", calls.resolve, calls.derive) == "two!"
    assert calls.derives == 2


def test_the_resolve_ttl_is_five_minutes_by_default():
    """A hit's resolution is a tree walk per link, so it is trusted longer than a miss (60 s)."""
    cache = vlc.VaultLookupCache()
    assert cache.resolve_ttl == vlc.RESOLVE_TTL == 300.0 > vlc.MISS_TTL
    assert vlc.VaultLookupCache(resolve_ttl=5.0).resolve_ttl == 5.0


def test_a_re_resolution_to_an_equal_path_reuses_the_value(tmp_path):
    """The memo compares the re-resolved path by value: resolvers build a new Path every call."""
    clock = _Clock()
    page = tmp_path / "p.md"
    page.write_text("one", encoding="utf-8")
    _settled(page)
    cache = vlc.VaultLookupCache(clock=clock)
    calls = _Calls(lambda: type(page)(str(page)))
    cache.lookup("k", calls.resolve, calls.derive)
    clock.now += vlc.RESOLVE_TTL
    assert cache.lookup("k", calls.resolve, calls.derive) == "one"
    assert calls.derives == 1


def test_a_re_resolution_whose_file_now_stamps_older_derives_again(tmp_path):
    """Any stamp change re-derives, including one that orders BEFORE the cached stamp (a file
    replaced by an older copy from a restore)."""
    clock = _Clock()
    page = tmp_path / "p.md"
    page.write_text("newer", encoding="utf-8")
    os.utime(page, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    cache = vlc.VaultLookupCache(clock=clock)
    calls = _Calls(page)
    assert cache.lookup("k", calls.resolve, calls.derive) == "newer"
    page.write_text("old", encoding="utf-8")
    os.utime(page, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    clock.now += vlc.RESOLVE_TTL
    assert cache.lookup("k", calls.resolve, calls.derive) == "old"


# okengine#1044 [24]: every hit re-resolves at RESOLVE_TTL, and the hits one render creates expire
# together. Each basename re-resolution was its own tree walk (rglob), so the first render after
# the window walked the vault once per link: 203 links on a live 91k-file vault took 32 s, every
# five minutes. BasenameIndex answers every basename from one walk.
def _tree(root, *rels):
    for rel in rels:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")


class _Walks:
    """Counts the index's tree walks while delegating to the real one."""

    def __init__(self, monkeypatch):
        self.count = 0
        real = vlc._walk

        def counted(root):
            self.count += 1
            return real(root)
        monkeypatch.setattr(vlc, "_walk", counted)


def test_the_index_answers_every_basename_from_one_walk(tmp_path, monkeypatch):
    _tree(tmp_path, "a/b/one.md", "c/two.md", "c/d/e/three.md", "f/one.md")
    walks = _Walks(monkeypatch)
    index = vlc.BasenameIndex(clock=_Clock())

    def root():
        return type(tmp_path)(str(tmp_path))   # an equal root, never the same object
    assert index.paths(root(), "two.md") == [tmp_path / "c" / "two.md"]
    assert index.paths(root(), "three.md") == [tmp_path / "c" / "d" / "e" / "three.md"]
    assert sorted(index.paths(root(), "one.md")) == [tmp_path / "a/b/one.md", tmp_path / "f/one.md"]
    assert walks.count == 1


def test_the_index_matches_names_exactly_and_lists_only_files(tmp_path):
    """rglob(name) treated `[` and `*` as glob syntax and returned directories too."""
    _tree(tmp_path, "a/x[1].md", "a/x1.md", "dir.md/inner.md")
    index = vlc.BasenameIndex(clock=_Clock())
    assert index.paths(tmp_path, "x[1].md") == [tmp_path / "a" / "x[1].md"]
    assert index.paths(tmp_path, "*.md") == []
    assert index.paths(tmp_path, "dir.md") == []


def test_the_index_is_rebuilt_after_its_max_age(tmp_path, monkeypatch):
    """A second page with a known slug (a better or ambiguous answer) is seen within max_age."""
    _tree(tmp_path, "a/dup.md")
    clock = _Clock()
    walks = _Walks(monkeypatch)
    index = vlc.BasenameIndex(clock=clock)
    assert index.paths(tmp_path, "dup.md") == [tmp_path / "a" / "dup.md"]
    _tree(tmp_path, "b/dup.md")
    clock.now += index.max_age - 0.001
    assert index.paths(tmp_path, "dup.md") == [tmp_path / "a" / "dup.md"], "fresh inside max_age"
    clock.now += 0.001
    assert sorted(index.paths(tmp_path, "dup.md")) == [tmp_path / "a/dup.md", tmp_path / "b/dup.md"]
    assert walks.count == 2


def test_an_unknown_name_rebuilds_once_the_index_is_past_its_min_age(tmp_path, monkeypatch):
    """A page created after the walk is found by the next lookup of its name once min_age has
    passed; inside min_age a burst of dangling links shares one walk instead of one each."""
    _tree(tmp_path, "a/old.md")
    clock = _Clock()
    walks = _Walks(monkeypatch)
    index = vlc.BasenameIndex(clock=clock)
    assert index.paths(tmp_path, "new.md") == []
    _tree(tmp_path, "b/new.md")
    for n in range(50):
        assert index.paths(tmp_path, f"dangling-{n}.md") == []
    assert index.paths(tmp_path, "new.md") == [], "inside min_age the walk is reused"
    assert walks.count == 1
    clock.now += index.min_age
    assert index.paths(tmp_path, "new.md") == [tmp_path / "b" / "new.md"]
    assert walks.count == 2


def test_a_moved_file_rebuilds_the_index_and_is_found_at_its_new_path(tmp_path, monkeypatch):
    """A reshelved page is found at once, inside min_age: the listed path being gone is proof the
    walk is stale, and the rebuild drops it."""
    _tree(tmp_path, "flat/page.md")
    clock = _Clock()
    walks = _Walks(monkeypatch)
    index = vlc.BasenameIndex(clock=clock)
    assert index.paths(tmp_path, "page.md") == [tmp_path / "flat" / "page.md"]
    (tmp_path / "p" / "a").mkdir(parents=True)
    (tmp_path / "flat" / "page.md").rename(tmp_path / "p" / "a" / "page.md")
    assert index.paths(tmp_path, "page.md") == [tmp_path / "p" / "a" / "page.md"]
    assert walks.count == 2


def test_a_vanished_file_is_never_returned_even_inside_min_age(tmp_path):
    _tree(tmp_path, "a/gone.md")
    index = vlc.BasenameIndex(clock=_Clock())
    assert index.paths(tmp_path, "gone.md") == [tmp_path / "a" / "gone.md"]
    (tmp_path / "a" / "gone.md").unlink()
    assert index.paths(tmp_path, "gone.md") == []


def test_a_new_root_or_clear_rebuilds(tmp_path, monkeypatch):
    _tree(tmp_path, "v1/a/p.md", "v2/b/p.md")
    walks = _Walks(monkeypatch)
    index = vlc.BasenameIndex(clock=_Clock())
    assert index.paths(tmp_path / "v1", "p.md") == [tmp_path / "v1" / "a" / "p.md"]
    assert index.paths(tmp_path / "v2", "p.md") == [tmp_path / "v2" / "b" / "p.md"]
    index.clear()
    assert index.paths(tmp_path / "v2", "p.md") == [tmp_path / "v2" / "b" / "p.md"]
    assert walks.count == 3


def test_a_missing_root_has_no_paths(tmp_path):
    assert vlc.BasenameIndex(clock=_Clock()).paths(tmp_path / "nope", "p.md") == []


def test_concurrent_lookups_share_one_walk(tmp_path, monkeypatch):
    """Single flight: requests that arrive while the index is being built wait for that build."""
    _tree(tmp_path, *(f"d{n}/p{n}.md" for n in range(20)))
    walks = _Walks(monkeypatch)
    index = vlc.BasenameIndex(clock=_Clock())
    results, start = [], threading.Barrier(8)

    def worker(n):
        start.wait()
        results.append(index.paths(tmp_path, f"p{n}.md"))
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(map(str, (r[0] for r in results))) == sorted(
        str(tmp_path / f"d{n}" / f"p{n}.md") for n in range(8))
    assert walks.count == 1


def test_the_index_defaults():
    index = vlc.BasenameIndex()
    assert (index.max_age, index.min_age) == (vlc.INDEX_MAX_AGE, vlc.INDEX_MIN_AGE) == (30.0, 2.0)
    assert vlc.INDEX_MAX_AGE <= vlc.MISS_TTL


def test_an_index_long_past_its_ages_is_rebuilt(tmp_path, monkeypatch):
    """Ages are elapsed time since the walk, compared at or above the bound, at any distance."""
    _tree(tmp_path, "a/dup.md")
    clock = _Clock()
    walks = _Walks(monkeypatch)
    index = vlc.BasenameIndex(clock=clock)
    assert index.paths(tmp_path, "dup.md") == [tmp_path / "a" / "dup.md"]
    _tree(tmp_path, "b/dup.md", "c/late.md")
    clock.now += 1000.0
    assert len(index.paths(tmp_path, "dup.md")) == 2, "far past max_age"
    _tree(tmp_path, "d/later.md")
    index.max_age = 1e9                        # only the min_age rule can find it, and
    clock.now = 2 * clock.now + 1              # elapsed is far past min_age (not a remainder)
    assert index.paths(tmp_path, "later.md") == [tmp_path / "d" / "later.md"], "far past min_age"
    assert walks.count == 3


# --- v0.15.14 audit: the index's age runs from the END of its walk -------------------------------
# The build time was the clock read BEFORE the walk. A walk that took >= min_age (a cold or slow
# disk) left the index already "old" when it finished, so every dangling link in the same render
# walked the whole vault again, serialized under the lock: the 32s+ stall okengine#1044 [24] fixed
# came back. A walk >= max_age rebuilt on every lookup.

class _SlowWalks:
    """A real walk that advances the fake clock by `seconds` while it runs."""

    def __init__(self, monkeypatch, clock, seconds):
        self.count = 0
        real = vlc._walk

        def slow(root):
            self.count += 1
            clock.now += seconds
            return real(root)
        monkeypatch.setattr(vlc, "_walk", slow)


def test_a_slow_walk_is_not_redone_for_every_dangling_link_in_a_burst(tmp_path, monkeypatch):
    _tree(tmp_path, "a/real.md")
    clock = _Clock()
    index = vlc.BasenameIndex(clock=clock)
    walks = _SlowWalks(monkeypatch, clock, 3.0)          # each walk takes longer than min_age (2s)
    for n in range(200):
        assert index.paths(tmp_path, f"dangling-{n}.md") == []
    assert walks.count == 1, f"{walks.count} walks for one burst of dangling links"
    clock.now += index.min_age                           # the age counts from the walk's end
    assert index.paths(tmp_path, "dangling-x.md") == []
    assert walks.count == 2


def test_a_walk_longer_than_max_age_is_not_redone_on_every_lookup(tmp_path, monkeypatch):
    _tree(tmp_path, "a/real.md")
    clock = _Clock()
    index = vlc.BasenameIndex(clock=clock)
    walks = _SlowWalks(monkeypatch, clock, index.max_age + 5.0)
    for _ in range(20):
        assert index.paths(tmp_path, "real.md") == [tmp_path / "a" / "real.md"]
    assert walks.count == 1, f"{walks.count} walks for 20 lookups of a known page"
    clock.now += index.max_age - 0.001
    index.paths(tmp_path, "real.md")
    assert walks.count == 1, "inside max_age of the walk's end"
    clock.now += 0.001
    index.paths(tmp_path, "real.md")
    assert walks.count == 2
