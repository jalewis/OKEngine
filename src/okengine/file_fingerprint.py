"""Validity keys for file-backed caches (okengine#1017).

A cache keyed only on a file's modification time serves stale data when the file is rewritten
within one filesystem timestamp tick: the kernel stamps mtime from a coarse clock, so two writes a
few milliseconds apart can carry the same ``st_mtime_ns``. On a fast disk that is the common case,
and the enforced write path validated against an outdated schema because of it.

``fingerprint()`` is the one key every file-backed cache in the engine uses:

* the stat identity ``(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)``. The inode catches an
  atomic replace, the size catches most in-place rewrites, and ``st_ctime_ns`` catches a rewrite
  whose mtime was pinned with ``os.utime`` (ctime cannot be set from user space);
* plus a SHA-256 of the content while the file is UNSETTLED, meaning its newest timestamp is within
  ``SETTLE_NS`` of now. A rewrite in the same tick with the same size and inode has an identical stat
  identity, so only the content can tell the two versions apart. Once the timestamps are older than
  the window, any later write must stamp a newer ctime, so the stat identity alone is exact and the
  fast path costs one ``stat()``.

A key recorded while unsettled carries the digest and a settled key does not, so the first lookup
after a file settles misses once and reloads. That is deliberate: an entry recorded inside the tick
is never trusted on its stat identity alone.
"""
from __future__ import annotations

import hashlib
import os
import stat as _stat
import time

# Two seconds covers coarse filesystem clocks (ext4/tmpfs jiffies, FAT's 2 s mtime) with margin.
SETTLE_NS = 2_000_000_000


def _digest(path) -> str:
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def fingerprint(path, st: os.stat_result | None = None, now_ns: int | None = None) -> tuple | None:
    """The cache-validity key of ``path``, or None when it cannot be stat'd or read.

    Pass ``st`` when the caller already holds the file's stat result, to avoid a second ``stat()``.
    ``now_ns`` defaults to the wall clock; tests pin it. A non-regular file (a directory, a FIFO) is
    never hashed: its stat identity is returned as is.
    """
    if st is None:
        try:
            st = os.stat(path)
        except OSError:
            return None
    key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    now = time.time_ns() if now_ns is None else now_ns
    # A future timestamp (clock skew, an mtime pinned ahead) counts as unsettled.
    if now - max(st.st_mtime_ns, st.st_ctime_ns) > SETTLE_NS or not _stat.S_ISREG(st.st_mode):
        return key
    try:
        return key + (_digest(path),)
    except OSError:
        return None


def token(key: tuple) -> str:
    """A short opaque string for a fingerprint, for a cache filename or a revision a client compares
    for equality. Equal fingerprints give equal tokens; any differing field changes it."""
    return hashlib.sha256(repr(key).encode()).hexdigest()[:32]
