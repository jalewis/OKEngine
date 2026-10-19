"""okengine.file_fingerprint: the one validity key for file-backed caches (okengine#1017).

A cache keyed on mtime alone served the previous content when a file was rewritten within one
filesystem timestamp tick. These tests pin the tick with os.utime so they hold on any disk, fast or
slow, instead of depending on how far apart two writes happen to land.
"""
from __future__ import annotations

import os
from unittest import mock

from okengine import file_fingerprint as ff


def _pinned_rewrite(path, text):
    """Rewrite ``path`` and restore its previous mtime: a second write in the same tick."""
    before = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _newest(path):
    st = path.stat()
    return max(st.st_mtime_ns, st.st_ctime_ns)


def test_missing_file_has_no_fingerprint(tmp_path):
    assert ff.fingerprint(tmp_path / "absent.yaml") is None


def test_same_size_rewrite_in_one_tick_changes_the_fingerprint(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text("value: aaa\n", encoding="utf-8")
    first = ff.fingerprint(path)
    _pinned_rewrite(path, "value: bbb\n")
    second = ff.fingerprint(path)
    assert path.stat().st_size == len("value: aaa\n")
    assert first != second, "a same-size, same-tick rewrite must not reuse the cached key"


def test_different_size_rewrite_in_one_tick_changes_the_fingerprint(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text("value: a\n", encoding="utf-8")
    first = ff.fingerprint(path)
    _pinned_rewrite(path, "value: a much longer value\n")
    assert ff.fingerprint(path) != first


def test_an_unchanged_unsettled_file_keeps_its_fingerprint(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text("value: a\n", encoding="utf-8")
    now = _newest(path)
    assert ff.fingerprint(path, now_ns=now) == ff.fingerprint(path, now_ns=now)


def test_a_settled_file_is_keyed_on_stat_identity_without_reading_it(tmp_path, monkeypatch):
    path = tmp_path / "schema.yaml"
    path.write_text("value: a\n", encoding="utf-8")
    st = path.stat()
    spy = mock.create_autospec(ff._digest, side_effect=ff._digest)
    monkeypatch.setattr(ff, "_digest", spy)
    key = ff.fingerprint(path, now_ns=_newest(path) + ff.SETTLE_NS + 1)
    assert key == (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    spy.assert_not_called()


def test_the_settle_window_boundary_is_still_unsettled(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text("value: a\n", encoding="utf-8")
    at_boundary = ff.fingerprint(path, now_ns=_newest(path) + ff.SETTLE_NS)
    assert len(at_boundary) == 6, "a timestamp exactly one window old is still inside the tick"
    assert len(ff.fingerprint(path, now_ns=_newest(path) + ff.SETTLE_NS + 1)) == 5


def test_a_fresh_ctime_keeps_a_file_unsettled_even_with_an_old_pinned_mtime(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text("value: a\n", encoding="utf-8")
    os.utime(path, ns=(0, 0))
    st = path.stat()
    key = ff.fingerprint(path, now_ns=st.st_ctime_ns + 1)
    assert len(key) == 6, "an mtime pinned into the past must not mark a just-written file settled"


def test_a_future_timestamp_counts_as_unsettled(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text("value: a\n", encoding="utf-8")
    future = _newest(path) + 10 * ff.SETTLE_NS
    os.utime(path, ns=(future, future))
    assert len(ff.fingerprint(path, now_ns=future - 5 * ff.SETTLE_NS)) == 6


def test_the_digest_is_of_the_full_content(tmp_path):
    path = tmp_path / "big.bin"
    head = b"x" * ((1 << 20) + 7)          # past any single read buffer
    path.write_bytes(head + b"a")
    first = ff.fingerprint(path)
    before = path.stat()
    path.write_bytes(head + b"b")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert ff.fingerprint(path) != first, "a change past the first read chunk must be seen"


def test_a_caller_supplied_stat_is_used_without_a_second_stat(tmp_path, monkeypatch):
    path = tmp_path / "schema.yaml"
    path.write_text("value: a\n", encoding="utf-8")
    st = path.stat()
    settled = _newest(path) + ff.SETTLE_NS + 1
    stat_spy = mock.create_autospec(os.stat, side_effect=os.stat)
    monkeypatch.setattr(ff.os, "stat", stat_spy)
    key = ff.fingerprint(path, st, now_ns=settled)
    assert key[:5] == (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    stat_spy.assert_not_called()


def test_a_directory_is_never_hashed(tmp_path):
    st = tmp_path.stat()
    key = ff.fingerprint(tmp_path, now_ns=max(st.st_mtime_ns, st.st_ctime_ns))
    assert key == (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def test_a_file_that_vanishes_before_it_is_read_has_no_fingerprint(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text("value: a\n", encoding="utf-8")
    st = path.stat()
    path.unlink()
    assert ff.fingerprint(path, st, now_ns=max(st.st_mtime_ns, st.st_ctime_ns)) is None


def test_token_is_stable_for_equal_keys_and_differs_otherwise():
    assert ff.token((1, 2, 3)) == ff.token((1, 2, 3))
    assert ff.token((1, 2, 3)) != ff.token((1, 2, 4))
    assert len(ff.token((1,))) == 32


def test_the_settle_window_is_two_seconds(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text("value: a\n", encoding="utf-8")
    newest = _newest(path)
    assert len(ff.fingerprint(path, now_ns=newest + 2_000_000_000)) == 6
    assert len(ff.fingerprint(path, now_ns=newest + 2_000_000_001)) == 5


def test_an_unsettled_key_differs_by_content_alone(tmp_path):
    """Same stat identity, different bytes: only the digest separates the two versions, which is
    exactly the same-size, same-tick rewrite the stat identity cannot see."""
    path = tmp_path / "schema.yaml"
    path.write_text("value: aaa\n", encoding="utf-8")
    st = path.stat()
    now = max(st.st_mtime_ns, st.st_ctime_ns)
    first = ff.fingerprint(path, st, now_ns=now)
    path.write_text("value: bbb\n", encoding="utf-8")
    second = ff.fingerprint(path, st, now_ns=now)
    assert first[:5] == second[:5] and first[5] != second[5]
