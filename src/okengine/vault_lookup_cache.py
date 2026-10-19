"""A bounded, revalidating memo for lookups over vault files (okengine#1024).

The reader and the cockpit resolve a wikilink or an embed to a page, and a page to its title, on
every render. A tree walk per lookup is too slow on a sharded vault, so the result is memoized. The
first memos kept every answer for the process lifetime, misses included, and never looked at the
vault again:

* a link or embed written before its target existed rendered as missing until the container was
  recreated;
* a page moved by reshelve or dedup rendered as an "unreadable embed", because the memo still held
  its old path;
* a retitled page kept its old title;
* the memo grew without bound.

``VaultLookupCache`` keeps the speed and drops the staleness:

* a HIT records the file it was derived from and that file's ``file_fingerprint``. Every later hit
  re-stamps the file (one ``stat()`` once it has settled) and is served only while the fingerprint is
  unchanged. A moved, deleted or rewritten file is resolved again;
* a HIT is also resolved again once ``resolve_ttl`` seconds have passed, because the RESOLUTION can
  change while the chosen file does not: a better match appears (an entity page created beside the
  observation copy a link first resolved to), or a second page with the same slug makes the answer
  ambiguous. Revalidating only the file served the old answer until the process restarted
  (okengine#1042 [30]/[38]). When the new resolution is the same unchanged file, the value is kept
  and only the deadline moves, so a settled hit still costs one ``stat()`` inside the window;
* a MISS is remembered for ``miss_ttl`` seconds only, so a page created after the first render shows
  up within that window while a dangling link does not walk the tree on every render;
* the memo holds at most ``maxsize`` entries and evicts the least recently used.

A miss expires at a time stamped when it is stored, never from a shared initial timestamp, so a
freshly started process has no window in which an empty entry reads as fresh.

``BasenameIndex`` makes the resolver behind a basename lookup cheap. The hits one render creates
expire together, and each re-resolution used to be its own tree walk, so the first render after the
window walked the vault once per link: a 203-link page on a live 91k-file vault took 32 s, every
five minutes (okengine#1044 [24]). The index answers every basename from one walk.
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from pathlib import Path
from typing import Any

from okengine.file_fingerprint import fingerprint

MAXSIZE = 4096
# Long enough that a page full of dangling links does not walk the tree on every render, short
# enough that a page written after the link appears without a restart.
MISS_TTL = 60.0
# How long a hit's RESOLUTION is trusted before the resolver runs again. A better or ambiguous answer
# appearing after the first render is rare and is seen within five minutes instead of at the next
# restart. Every hit one render creates expires at about the same time, so the resolver behind a
# re-resolution must be cheap: a basename resolver uses BasenameIndex, never one walk per key (a
# per-key rglob over a live 91k-file vault measured 0.16 s, 32 s for a 203-link page; #1044 [24]).
RESOLVE_TTL = 300.0
# A BasenameIndex is rebuilt once it is this old, so a second page with a known slug (a better or an
# ambiguous answer) is seen within this window of a re-resolution. One walk of that vault: 0.27 s.
INDEX_MAX_AGE = 30.0
# ...and sooner, once it is at least this old, when a lookup names a file it does not list (a page
# created since the walk). The floor makes a burst of dangling links in one render share one walk
# instead of walking once each. A listed file that is gone (a page moved since the walk) rebuilds at
# once: the rebuild drops it, so it cannot trigger another.
INDEX_MIN_AGE = 2.0


class VaultLookupCache:
    """Memo of ``key -> value`` where each value is derived from one vault file, or is a miss."""

    def __init__(self, maxsize: int = MAXSIZE, miss_ttl: float = MISS_TTL,
                 clock: Callable[[], float] = time.monotonic,
                 resolve_ttl: float = RESOLVE_TTL) -> None:
        self.maxsize = maxsize
        self.miss_ttl = miss_ttl
        self.resolve_ttl = resolve_ttl
        self._clock = clock
        # key -> (path, stamp, value, deadline). A miss has path None and expires at `deadline`; a
        # hit is served while its file's fingerprint equals `stamp` and `deadline` has not passed.
        self._entries: OrderedDict[Hashable, tuple[Path | None, Any, Any, float]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _fresh(self, key: Hashable) -> tuple[bool, Any]:
        """(True, value) when ``key`` holds an entry that is still valid."""
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return False, None
        path, stamp, value, deadline = entry
        valid = self._clock() < deadline and (path is None or fingerprint(path) == stamp)
        if valid:
            with self._lock:
                if key in self._entries:
                    self._entries.move_to_end(key)
        return valid, value

    def _held(self, key: Hashable, path: Path, stamp: Any) -> tuple[bool, Any]:
        """(True, value) when `key` holds a hit on `path` derived under `stamp`: a re-resolution
        that found the same unchanged file, so the value is still the file's and is reused."""
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None and entry[0] == path and entry[1] == stamp:
            return True, entry[2]
        return False, None

    def _store(self, key: Hashable, entry: tuple[Path | None, Any, Any, float]) -> None:
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)

    def lookup(self, key: Hashable, resolve: Callable[[], Path | None],
               derive: Callable[[Path], Any], missing: Any = None) -> Any:
        """The value for ``key``: memoized while valid, else ``derive(resolve())``.

        ``resolve()`` finds the file the value comes from, or returns None for a miss, which
        yields ``missing``. The file is fingerprinted BEFORE ``derive`` reads it, so a write that
        lands in between leaves an entry whose stamp no longer matches, and the next lookup reads
        the file again instead of serving the older value forever.
        """
        valid, value = self._fresh(key)
        if valid:
            return value
        path = resolve()
        if path is None:
            self._store(key, (None, None, missing, self._clock() + self.miss_ttl))
            return missing
        stamp = fingerprint(path)
        held, value = self._held(key, path, stamp) if stamp is not None else (False, None)
        if not held:
            value = derive(path)
        if stamp is not None:
            self._store(key, (path, stamp, value, self._clock() + self.resolve_ttl))
        return value


def _walk(root: Path) -> dict[str, list[str]]:
    """``{basename: [directory, ...]}`` for every file under ``root``. Directories are kept as the
    strings os.walk yields (one object per directory), not a Path per file, to keep it small."""
    names: dict[str, list[str]] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            names.setdefault(name, []).append(dirpath)
    return names


class BasenameIndex:
    """Every file under a root by exact basename, from one walk (okengine#1044 [24]).

    ``paths(root, name)`` lists the files named ``name`` under ``root``, as ``rglob(name)`` would
    but matching the name exactly (no glob syntax) and listing files only. It never returns a file
    that is gone. The walk is redone when the root changes, after ``max_age``, when a lookup lists a
    file that is gone, and, once the index is ``min_age`` old, when it names a file not listed.
    Lookups that arrive during a walk wait for it rather than walking too."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 max_age: float = INDEX_MAX_AGE, min_age: float = INDEX_MIN_AGE) -> None:
        self.max_age = max_age
        self.min_age = min_age
        self._clock = clock
        self._root: Path | None = None     # None: never walked, so the first lookup walks
        self._built: float                 # set by every walk; read only once _root is set
        self._names: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._root = None
            self._names = {}

    def _rebuild(self, root: Path) -> None:
        # Stamped when the walk ENDS: a clock read taken before it made a walk that took min_age
        # (a cold or slow disk) already old on arrival, so every dangling link in one render walked
        # the vault again under the lock, and one >= max_age rebuilt on every lookup (v0.15.14 audit).
        self._names = _walk(root)
        self._root, self._built = root, self._clock()

    def paths(self, root: Path, name: str) -> list[Path]:
        with self._lock:
            now = self._clock()
            if self._root != root or now - self._built >= self.max_age:
                self._rebuild(root)
            found = [Path(d, name) for d in self._names.get(name, ())]
            gone = not all(p.is_file() for p in found)
            if gone or (not found and now - self._built >= self.min_age):
                self._rebuild(root)
                found = [Path(d, name) for d in self._names.get(name, ())]
        return [p for p in found if p.is_file()]
