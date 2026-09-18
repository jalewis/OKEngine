"""staged_drift: prove a deploy LANDED by inspecting the target, not by reading its exit code.

Both halves of that gap were seen on one fleet in one day. `deploy-cron-scripts.sh` returned 0 and
printed `done.` on five gateways while a pack's pre-#267 fork of `nvd_import.py` overwrote the
engine's copy on every run — for three weeks, surfacing only when somebody hashed the file inside
the container. Separately, a fleet roll piped through `| tail -3` read tail's status and reported
success on five hosts having staged nothing.

The contract here is as much about what must NEVER read as clean: an empty listing, an unreadable
listing and an empty source set are all UNDETECTABLE, and each one is the exact shape that turns a
missing measurement into a green tick.
"""
import hashlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "staged_drift.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="staged_drift absent")


def _load():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("staged_drift", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["staged_drift"] = m
    spec.loader.exec_module(m)
    return m


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _listing(**files: str) -> str:
    return "".join(f"{_sha(body)}  /opt/data/scripts/{name}\n" for name, body in files.items())


def _srcdir(root: Path, **files: str) -> Path:
    d = root / "src"
    d.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
    return d


# --- the pure verdict ----------------------------------------------------------------------------

def test_identical_source_and_target_is_the_only_clean_result(tmp_path):
    m = _load()
    src = {"a.py": "h1", "b.py": "h2"}
    assert m.compare(src, dict(src)) == {"drifted": [], "missing": [], "extra": []}


def test_each_way_a_deploy_can_lie_is_reported_separately(tmp_path):
    """They are different faults with different fixes: drifted means something else writes that
    name, missing means the stage skipped it, extra means the reconcile did not run. Collapsing
    them into one "mismatch" count would send the operator looking in the wrong place."""
    m = _load()
    result = m.compare(
        {"same.py": "h1", "forked.py": "engine", "never_arrived.py": "h3"},
        {"same.py": "h1", "forked.py": "packfork", "fossil.py": "h4"})
    assert result == {"drifted": ["forked.py"], "missing": ["never_arrived.py"],
                      "extra": ["fossil.py"]}

    # Drift is INEQUALITY, in either direction. An ordering comparison would silently pass every
    # case where the source hash happens to sort after the staged one -- half of all real drift.
    assert m.compare({"a.py": "zzz"}, {"a.py": "aaa"})["drifted"] == ["a.py"]
    assert m.compare({"a.py": "aaa"}, {"a.py": "zzz"})["drifted"] == ["a.py"]


def test_the_later_source_wins_a_name_collision(tmp_path):
    """The deploy stages engine first, pack second, so on a shared basename the PACK copy is what
    lands. Reporting drift against the engine's copy would name a file the deploy never intended to
    be live and send the reader chasing the wrong one. (The collision itself is refused by
    framework_validate + the deploy guard; this only keeps the report honest if one slips through.)"""
    m = _load()
    engine = tmp_path / "engine"
    pack = tmp_path / "pack"
    for d, body in ((engine, "engine version"), (pack, "pack version")):
        d.mkdir(parents=True)
        (d / "shared.py").write_text(body, encoding="utf-8")
    hashes = m.source_hashes([engine, pack], [])
    assert hashes["shared.py"] == _sha("pack version")


def test_an_unreadable_source_file_matches_nothing(tmp_path):
    """An unreadable file is not "identical" and not "absent". Returning "" makes it report as
    drifted, which is the honest answer — it is certainly not proven to match."""
    m = _load()
    d = tmp_path / "src"
    d.mkdir()
    missing = d / "gone.py"
    assert m.file_hash(missing) == ""
    real = _srcdir(tmp_path, **{"ok.py": "body"})
    assert m.file_hash(real / "ok.py") == _sha("body")


# --- parsing the target's listing ----------------------------------------------------------------

def test_listing_parses_real_sha256sum_output(tmp_path):
    m = _load()
    parsed = m.parse_listing(
        f"{_sha('a')}  /opt/data/scripts/a.py\n"
        f"{_sha('b')} *./b.py\n")
    assert parsed == {"a.py": _sha("a"), "b.py": _sha("b")}


def test_listing_never_invents_an_entry_from_an_error_line(tmp_path):
    """A caller that merges stderr into the listing must not turn "No such file" into an entry
    claiming some file hashes to "sha256sum:". A malformed line is not data.

    Every skippable line is placed BEFORE a good one on purpose. Skipping must CONTINUE, not stop:
    a parser that bails at the first odd line returns a short listing, and a short listing makes
    every file after it report as `missing` — a silent truncation dressed up as a deploy fault,
    which is the exact class this whole detector exists to catch."""
    m = _load()
    parsed = m.parse_listing(
        "\n"                                                       # blank
        "                \n"                                       # whitespace only
        "single-token\n"                                           # one field
        "sha256sum: /opt/data/scripts/gone.py: No such file or directory\n"
        "not-a-hash  /opt/data/scripts/x.py\n"
        f"{'z' * 64}  /opt/data/scripts/nonhex.py\n"               # right length, not hex
        f"{_sha('c')[:32]}  /opt/data/scripts/tooshort.py\n"       # hex, too short
        f"{_sha('c') + 'abc'}  /opt/data/scripts/toolong.py\n"      # hex, too long
        f"{_sha('d')}  /opt/data/scripts/notpython.txt\n"          # not a .py
        f"{_sha('real')}  /opt/data/scripts/real.py\n")            # ...and still reached
    assert parsed == {"real.py": _sha("real")}, (
        "the good line after every bad one must still be parsed")


def test_an_uppercase_digest_is_the_same_digest(tmp_path):
    """`sha256sum` writes lowercase, but a caller may normalise. Case must not read as drift."""
    m = _load()
    assert m.parse_listing(f"{_sha('a').upper()}  /opt/data/scripts/a.py\n") == {"a.py": _sha("a")}


# --- the CLI, and every way it must refuse to say "clean" ----------------------------------------

def test_matching_target_exits_zero_and_says_how_much_it_compared(tmp_path, capsys):
    """"clean" over zero files reads identically to "clean" over a hundred unless the count is
    stated, and a check that silently compared nothing is the failure being guarded against."""
    m = _load()
    src = _srcdir(tmp_path, **{"a.py": "one", "b.py": "two"})
    listing = tmp_path / "listing.txt"
    listing.write_text(_listing(**{"a.py": "one", "b.py": "two"}), encoding="utf-8")
    rc = m.main(["--source-dir", str(src), "--staged-listing", str(listing)])
    assert rc == 0
    assert "2 file(s) compared" in capsys.readouterr().out


def test_a_drifted_target_fails_and_names_the_file(tmp_path, capsys):
    m = _load()
    src = _srcdir(tmp_path, **{"a.py": "source"})
    listing = tmp_path / "listing.txt"
    listing.write_text(_listing(**{"a.py": "something else wrote this"}), encoding="utf-8")
    rc = m.main(["--source-dir", str(src), "--staged-listing", str(listing), "--label", "gw"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "drifted  a.py" in err and "gw" in err
    assert "The deploy reported success; the target disagrees" in err


def test_an_empty_listing_is_undetectable_not_clean(tmp_path, capsys):
    """The whole point. A target that listed nothing has proven nothing — reading that as parity is
    how a fleet roll reports success on five hosts having staged nothing."""
    m = _load()
    src = _srcdir(tmp_path, **{"a.py": "one"})
    listing = tmp_path / "empty.txt"
    listing.write_text("", encoding="utf-8")
    rc = m.main(["--source-dir", str(src), "--staged-listing", str(listing)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "UNDETECTABLE" in err and "not proven" in err


def test_an_unreadable_listing_is_undetectable_not_clean(tmp_path, capsys):
    m = _load()
    src = _srcdir(tmp_path, **{"a.py": "one"})
    rc = m.main(["--source-dir", str(src), "--staged-listing", str(tmp_path / "nope.txt")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot read staged listing" in err and "UNDETECTABLE" in err


def test_an_empty_source_set_is_undetectable_not_clean(tmp_path, capsys):
    """Pointed at the wrong directory, the comparison has nothing to compare and every staged file
    would look "extra". Refusing is the only honest answer — a wrong path must not read as a pass."""
    m = _load()
    listing = tmp_path / "listing.txt"
    listing.write_text(_listing(**{"a.py": "one"}), encoding="utf-8")
    rc = m.main(["--source-dir", str(tmp_path / "not-a-dir"), "--staged-listing", str(listing)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no source scripts found" in err and "UNDETECTABLE" in err


def test_individually_staged_helpers_are_part_of_the_source_set(tmp_path, capsys):
    """The deploy also stages a few files from scripts/ that are not in scripts/cron/. Leaving them
    out of the source set would report each of them as an `extra` fossil on every single deploy —
    a check that cries wolf gets switched off, and then guards nothing."""
    m = _load()
    src = _srcdir(tmp_path, **{"a.py": "one"})
    helper = tmp_path / "helper.py"
    helper.write_text("helper body", encoding="utf-8")
    listing = tmp_path / "listing.txt"
    listing.write_text(_listing(**{"a.py": "one", "helper.py": "helper body"}), encoding="utf-8")
    rc = m.main(["--source-dir", str(src), "--source-file", str(helper),
                 "--staged-listing", str(listing)])
    assert rc == 0, capsys.readouterr().err
    assert "2 file(s) compared" in capsys.readouterr().out


def test_a_declared_helper_that_does_not_exist_is_not_invented(tmp_path, capsys):
    m = _load()
    src = _srcdir(tmp_path, **{"a.py": "one"})
    listing = tmp_path / "listing.txt"
    listing.write_text(_listing(**{"a.py": "one"}), encoding="utf-8")
    rc = m.main(["--source-dir", str(src), "--source-file", str(tmp_path / "absent.py"),
                 "--staged-listing", str(listing)])
    assert rc == 0, capsys.readouterr().err


def test_stdin_is_accepted_so_the_caller_can_pipe_a_docker_exec(tmp_path, capsys, monkeypatch):
    """How the deploy actually invokes it: `docker exec ... sha256sum | staged_drift --staged-listing -`."""
    m = _load()
    src = _srcdir(tmp_path, **{"a.py": "one"})
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(_listing(**{"a.py": "one"})))
    assert m.main(["--source-dir", str(src), "--staged-listing", "-"]) == 0
    assert "1 file(s) compared" in capsys.readouterr().out


def test_the_staged_listing_argument_is_required(tmp_path):
    """There is no default target. Defaulting to stdin on a tty would hang a deploy; defaulting to
    "nothing" would report parity it never measured."""
    m = _load()
    src = _srcdir(tmp_path, **{"a.py": "one"})
    with pytest.raises(SystemExit) as exc:
        m.main(["--source-dir", str(src)])
    assert exc.value.code == 2
