"""The qualification tools must ship no deployment-specific defaults (okengine#493).

Four of these scripts shipped an operator address or filesystem layout as a
fallback default: an inference-host IP on `--endpoint`, and one operator's home
directory on `--pack-root` / `$PACK_ROOT`. That is domain knowledge in a
domain-agnostic engine, and on `cleanup_backfill_qualification.py` -- which
DELETES with `--apply` -- silently inheriting the wrong root is destructive.

This is a committed detector on purpose. The pre-commit scrub gate reads
`.scrub-patterns`, which is git-IGNORED: in a fresh clone, and in public CI, that
file is absent and the scrub cannot see this class at all. A test can.

Asserts both halves: the literals are gone, AND the tools fail loudly rather
than falling back to a guess.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).parents[1]

TOOLS = (
    "scripts/backfill_gold_live.py",
    "scripts/prepare_backfill_qualification.py",
    "scripts/cleanup_backfill_qualification.py",
    "scripts/run_backfill_qualification_matrix.sh",
)

# An absolute home directory, or a private-range address.
#
# Deliberately UNANCHORED. An earlier draft required a quote, `=` or `:`
# immediately before the path, and so missed the shell default-expansion form
# (`${VAR:-` followed by the path) because the preceding character there is the
# `-`. In these files an absolute home path is wrong wherever it appears,
# including inside a comment, so match it anywhere.
#
# This module is held to the same rule it enforces -- see
# test_this_detector_contains_no_literal_it_forbids. That is why the form above
# is described rather than quoted: writing a specimen path here would make the
# detector's own source a leak, which is exactly what CI caught on the first
# attempt at this file.
_HOME_PATH = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")
_PRIVATE_IP = re.compile(r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
                         r"|\b192\.168\.\d{1,3}\.\d{1,3}\b"
                         r"|\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b")


def _load(rel: str):
    spec = importlib.util.spec_from_file_location(Path(rel).stem, REPO / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("rel", TOOLS)
def test_no_operator_path_or_address_literal(rel):
    path = REPO / rel
    assert path.is_file(), f"{rel} is missing"
    text = path.read_text(encoding="utf-8")
    assert not _HOME_PATH.search(text), (
        f"{rel} hardcodes an operator home directory; read it from "
        "OKENGINE_PACK_ROOT/PACK_ROOT and fail when unset")
    assert not _PRIVATE_IP.search(text), (
        f"{rel} hardcodes a private inference-host address; read it from "
        "OKENGINE_LLM_BASE_URL and fail when unset")


def test_this_detector_contains_no_literal_it_forbids():
    """The detector must satisfy its own rule.

    A specimen path written into this file to illustrate the pattern is still a
    leak in a tracked file, and CI's scrub -- whose pattern set is a superset of
    the git-ignored local `.scrub-patterns` -- rejects it. Describe the shape in
    prose; never quote a real one.
    """
    text = Path(__file__).read_text(encoding="utf-8")
    assert not _HOME_PATH.search(text), (
        "this file contains an absolute home-path literal; describe the shape "
        "in prose instead of quoting a specimen")
    assert not _PRIVATE_IP.search(text), (
        "this file contains a private-range address literal; describe the shape "
        "in prose instead of quoting a specimen")


@pytest.mark.parametrize("rel", TOOLS)
def test_tool_names_the_env_var_it_reads(rel):
    """A guard that only removes the default is useless if nothing says what to set."""
    text = (REPO / rel).read_text(encoding="utf-8")
    assert "OKENGINE_PACK_ROOT" in text or "OKENGINE_LLM_BASE_URL" in text, (
        f"{rel} must name the environment variable an operator is expected to set")


def test_no_tool_names_a_deployment_or_gateway(monkeypatch):
    """The pack set is fleet composition, not an engine constant (okengine#510).

    These carried literal deployment names — one operator's five packs, including a real
    company's vault — so on any other fleet nothing matched and qualification reported a
    clean pass over ZERO packs. The pilot cohort had become the tool's boundary.

    The scrub gate cannot see this class: `.scrub-patterns` covers private hostnames and
    tokens, not pack directory names. That is how it reached main.
    """
    offenders = []
    for rel in TOOLS:
        text = (REPO / rel).read_text(encoding="utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r"\bok(?:cti|pack)[-\w]*", line) or re.search(r"\S+-gateway\b", line):
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, (
        "deployment/gateway names must not appear in the engine's qualification tooling — "
        "supply them via --pack / OKENGINE_QUALIFICATION_PACKS / QUALIFICATION_GATEWAYS. "
        "Offenders:\n  " + "\n  ".join(offenders))


def test_an_empty_pack_set_is_a_loud_failure_not_a_clean_pass(monkeypatch, tmp_path):
    """Qualifying or cleaning ZERO packs must never exit 0 — that is the #510 failure mode."""
    monkeypatch.delenv("OKENGINE_QUALIFICATION_PACKS", raising=False)
    monkeypatch.setenv("OKENGINE_PACK_ROOT", str(tmp_path))

    prepare = _load("scripts/prepare_backfill_qualification.py")
    monkeypatch.setattr(sys, "argv", ["prepare", "g99"])
    with pytest.raises(SystemExit) as error:
        prepare.main()
    assert "no packs requested" in str(error.value)

    cleanup = _load("scripts/cleanup_backfill_qualification.py")
    monkeypatch.setattr(sys, "argv", ["cleanup"])
    with pytest.raises(SystemExit) as error:
        cleanup.main()
    assert "no packs requested" in str(error.value)


def test_prediction_fixture_follows_the_capability_not_a_name_list(tmp_path):
    """A hardcoded subset of pack names is replaced by asking what the pack enables."""
    prepare = _load("scripts/prepare_backfill_qualification.py")
    for name, ext in (("with", "okengine.predictions: {}"), ("without", "okengine.dedupe: {}")):
        (tmp_path / name / ".okengine").mkdir(parents=True)
        (tmp_path / name / ".okengine" / "extensions.yaml").write_text(f"enabled:\n  {ext}\n")
    assert prepare.predictions_enabled(tmp_path / "with") is True
    assert prepare.predictions_enabled(tmp_path / "without") is False
    # A pack with no extensions.yaml at all must be False, not an exception.
    (tmp_path / "bare").mkdir()
    assert prepare.predictions_enabled(tmp_path / "bare") is False


def test_matrix_requires_paired_pack_and_gateway_lists():
    text = (REPO / "scripts" / "run_backfill_qualification_matrix.sh").read_text(encoding="utf-8")
    assert "QUALIFICATION_PACKS" in text and "QUALIFICATION_GATEWAYS" in text, (
        "the matrix must take its fleet composition as input")
    assert "${#gateways[@]} != ${#packs[@]}" in text, (
        "packs and gateways are positionally paired, so a length mismatch must fail loudly "
        "rather than silently qualifying the wrong deployment")
    # Check CODE, not prose: the comment above the fix quotes the old `for i in 0 1 3` line
    # to explain it, and a whole-text match flags that explanation. Same self-reference trap
    # as a detector whose docstring contains the literal it forbids.
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"^\s*for i in [\d ]+;\s*do", code, re.MULTILINE), (
        "the prediction lane must not index a fixed pack order — that selects the wrong "
        "packs as soon as the list is supplied rather than hardcoded")
    assert "pack_has_predictions" in text, (
        "the prediction lane must ask the pack what it enables")


def test_missing_pack_root_is_a_loud_failure_not_a_guess(monkeypatch):
    monkeypatch.delenv("OKENGINE_PACK_ROOT", raising=False)

    cleanup = _load("scripts/cleanup_backfill_qualification.py")
    monkeypatch.setattr(sys, "argv", ["cleanup_backfill_qualification.py"])
    with pytest.raises(SystemExit) as excinfo:
        cleanup.main()
    assert "OKENGINE_PACK_ROOT" in str(excinfo.value)

    prepare = _load("scripts/prepare_backfill_qualification.py")
    monkeypatch.setattr(sys, "argv", ["prepare_backfill_qualification.py", "g99"])
    with pytest.raises(SystemExit) as excinfo:
        prepare.main()
    assert "OKENGINE_PACK_ROOT" in str(excinfo.value)


def test_missing_endpoint_is_a_loud_failure_not_a_guess(monkeypatch, tmp_path):
    monkeypatch.delenv("OKENGINE_LLM_BASE_URL", raising=False)
    gold = _load("scripts/backfill_gold_live.py")
    monkeypatch.setattr(
        sys, "argv",
        ["backfill_gold_live.py", "--output", str(tmp_path / "out.jsonl")])
    with pytest.raises(SystemExit) as excinfo:
        gold.main()
    # Must refuse BEFORE reading the corpus, so a missing endpoint is reported
    # as a missing endpoint rather than as an unrelated file error.
    assert "OKENGINE_LLM_BASE_URL" in str(excinfo.value)


def test_pack_root_env_var_is_honoured_when_set(monkeypatch, tmp_path):
    """The guard must not fire when the operator HAS supplied the root."""
    monkeypatch.setenv("OKENGINE_PACK_ROOT", str(tmp_path))
    cleanup = _load("scripts/cleanup_backfill_qualification.py")
    monkeypatch.setattr(sys, "argv", ["cleanup_backfill_qualification.py"])
    # tmp_path holds none of the expected pack directories, so this fails on a
    # missing pack path -- proving it got PAST the pack-root guard.
    with pytest.raises(SystemExit) as excinfo:
        cleanup.main()
    assert "OKENGINE_PACK_ROOT" not in str(excinfo.value)
