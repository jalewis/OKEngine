"""The except-tuple lint: catch a cosmic-ray-incompetent pattern before merge (okengine#1046).

cosmic-ray's ExceptionReplacer sets ``.value`` on each element of an ``except (...)`` tuple, so an
element that is not a bare name (``yaml.YAMLError``) raises AttributeError inside the worker. The
gate reports that mutant as an incompetent-worker error. !1329 merged exactly that line, and main
pipeline 13045's ``mutation-changed`` was the first thing to notice (job 158862).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import parso
import pytest
from cosmic_ray.operators.exception_replacer import ExceptionReplacer

from ci import mutation_lint


pytestmark = pytest.mark.invariant

REPO = Path(__file__).resolve().parents[2]


def _handler(clause: str) -> str:
    return f"try:\n    pass\n{clause}:\n    pass\n"


# Hostile: every one of these makes ExceptionReplacer raise inside the worker.
REJECTED = {
    # The 13045 line exactly as !1329 merged it (assessments.py:275, pre-!1330).
    "pipeline_13045": "except (yaml.YAMLError, ValueError, RecursionError)",
    "dotted_first": "except (yaml.YAMLError, ValueError)",
    "dotted_json_first": "except (json.JSONDecodeError, KeyError)",
    "dotted_middle": "except (ValueError, json.JSONDecodeError, KeyError)",
    "dotted_last_with_alias": "except (OSError, urllib.error.URLError) as exc",
    "single_element_tuple": "except (yaml.YAMLError,)",
    "call_element": "except (errors(), OSError)",
    "subscript_element": "except (ERRORS[0], OSError)",
    "starred_element": "except (*ERRORS, OSError)",
}

# Legitimate: ExceptionReplacer mutates each of these without raising.
ACCEPTED = {
    "bare_tuple": "except (YAMLError, ValueError, RecursionError)",
    "bare_tuple_with_alias": "except (JSONDecodeError, KeyError) as exc",
    "single_dotted": "except yaml.YAMLError",
    "single_dotted_with_alias": "except json.JSONDecodeError as exc",
    "parenthesized_single_dotted": "except (yaml.YAMLError)",
    "single_bare": "except OSError",
    "bare_except": "except",
    "name_bound_tuple": "except ERRORS",
}


def _cosmic_ray_raises(source: str) -> bool:
    """Ground truth: does the real ExceptionReplacer raise on any mutation of ``source``?"""
    operator = ExceptionReplacer()

    def walk(node):
        yield node
        for child in getattr(node, "children", ()):
            yield from walk(child)

    for node in walk(parso.parse(source)):
        for index, _position in enumerate(operator.mutation_positions(node)):
            fresh = next(candidate for candidate in walk(parso.parse(source))
                         if candidate.start_pos == node.start_pos
                         and candidate.type == node.type)
            try:
                operator.mutate(fresh, index)
            except AttributeError:
                return True
    return False


@pytest.mark.parametrize("clause", REJECTED.values(), ids=REJECTED.keys())
def test_the_lint_rejects_an_except_tuple_element_that_is_not_a_bare_name(clause):
    findings = mutation_lint.incompetent_except_elements(_handler(clause))
    assert len(findings) == 1, f"expected one finding for {clause!r}, got {findings}"
    assert findings[0].line == 3


@pytest.mark.parametrize("clause", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_the_lint_accepts_handlers_cosmic_ray_can_mutate(clause):
    assert mutation_lint.incompetent_except_elements(_handler(clause)) == []


@pytest.mark.parametrize("clause", [*REJECTED.values(), *ACCEPTED.values()])
def test_the_lint_agrees_with_cosmic_rays_own_exception_replacer(clause):
    """Contract against the real dependency, so the lint cannot drift from what it models.

    If a cosmic-ray release fixes the operator, this fails on the REJECTED cases and says the lint
    can be retired, rather than the lint silently outliving its reason.
    """
    source = _handler(clause)
    flagged = bool(mutation_lint.incompetent_except_elements(source))
    assert flagged == _cosmic_ray_raises(source), (
        f"lint says {'reject' if flagged else 'accept'} for {clause!r} but cosmic-ray's "
        f"ExceptionReplacer {'does not raise' if flagged else 'raises'}")


def test_the_finding_names_the_element_and_the_bare_name_fix():
    (finding,) = mutation_lint.incompetent_except_elements(
        _handler("except (ValueError, json.JSONDecodeError)"))
    assert (finding.line, finding.column, finding.element) == (3, 20, "json.JSONDecodeError")
    assert "from json import JSONDecodeError" in finding.hint


def test_the_hint_for_an_element_that_is_not_an_attribute_says_to_bind_a_name():
    (finding,) = mutation_lint.incompetent_except_elements(_handler("except (errors(), OSError)"))
    assert finding.element == "errors()"
    assert "bare name" in finding.hint


def test_try_star_handlers_are_checked_too():
    source = "try:\n    pass\nexcept* (yaml.YAMLError, ValueError):\n    pass\n"
    assert [f.element for f in mutation_lint.incompetent_except_elements(source)] == \
        ["yaml.YAMLError"]


def _write_manifest(root: Path, paths: list[str]) -> Path:
    manifest = root / "mutation" / "targets.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"targets": [{"path": path} for path in paths]}),
                        encoding="utf-8")
    return manifest


def test_scan_reports_only_manifest_targets(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "target.py").write_text(
        _handler("except (yaml.YAMLError, ValueError)"), encoding="utf-8")
    (tmp_path / "pkg" / "unregistered.py").write_text(
        _handler("except (json.JSONDecodeError, KeyError)"), encoding="utf-8")
    manifest = _write_manifest(tmp_path, ["pkg/target.py"])
    assert mutation_lint.scan(tmp_path, manifest) == [
        "pkg/target.py:3:8: `yaml.YAMLError` in an except tuple: cosmic-ray's ExceptionReplacer "
        "cannot mutate it (incompetent worker). Use a bare name: from yaml import YAMLError"]


def test_scan_of_a_clean_manifest_is_empty(tmp_path):
    (tmp_path / "clean.py").write_text(_handler("except (YAMLError, ValueError)"),
                                       encoding="utf-8")
    assert mutation_lint.scan(tmp_path, _write_manifest(tmp_path, ["clean.py"])) == []


def test_scan_fails_closed_on_a_target_that_does_not_parse(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        mutation_lint.scan(tmp_path, _write_manifest(tmp_path, ["broken.py"]))


def test_every_mutation_target_is_free_of_incompetent_except_tuples():
    """The gate itself: no manifest target may carry the 13045 pattern."""
    findings = mutation_lint.scan(REPO, REPO / "mutation" / "targets.json")
    assert findings == [], "\n".join(findings)


def _run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    # Invoked as CI would: a subprocess from the repository root, no test import paths.
    return subprocess.run([sys.executable, str(REPO / "ci" / "mutation_lint.py"), *args],
                          cwd=cwd, capture_output=True, text=True, check=False)


def test_cli_exits_nonzero_and_names_the_line_on_a_violation(tmp_path):
    (tmp_path / "bad.py").write_text(_handler("except (yaml.YAMLError, ValueError)"),
                                     encoding="utf-8")
    _write_manifest(tmp_path, ["bad.py"])
    result = _run_cli("--repo", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "bad.py:3:8: `yaml.YAMLError`" in result.stdout


def test_cli_passes_on_the_real_tree():
    result = _run_cli(cwd=REPO)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mutation lint: 0 incompetent except-tuple element(s)" in result.stdout


def test_main_in_process_reports_and_exits_by_finding_count(tmp_path, capsys):
    (tmp_path / "bad.py").write_text(_handler("except (json.JSONDecodeError, KeyError)"),
                                     encoding="utf-8")
    manifest = _write_manifest(tmp_path, ["bad.py"])
    assert mutation_lint.main(["--repo", str(tmp_path), "--manifest", str(manifest)]) == 1
    assert "bad.py:3:8: `json.JSONDecodeError`" in capsys.readouterr().out
    (tmp_path / "bad.py").write_text(_handler("except (JSONDecodeError, KeyError)"),
                                     encoding="utf-8")
    assert mutation_lint.main(["--repo", str(tmp_path)]) == 0
    assert "mutation lint: 0 incompetent" in capsys.readouterr().out


# ── the public snapshot tree (main pipeline 13068, job 159460) ──────────────────────────────
# publish-snapshot.sh removes its EXCLUDE entries, some of which are mutation targets
# (ci/registry_canary.py), and itself. The lint must not fail there for a file the snapshot
# deliberately omits, and must still fail when the SOURCE tree lacks a target.

_SNAPSHOT_SCRIPT = 'EXCLUDE=(\n  "pkg/private.py"   # private\n  "privdir"          # private dir\n)\n'


def _tree(root: Path, *, source: bool) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "public.py").write_text(
        _handler("except (yaml.YAMLError, ValueError)"), encoding="utf-8")
    if source:
        (root / "scripts").mkdir()
        (root / "scripts" / "publish-snapshot.sh").write_text(_SNAPSHOT_SCRIPT, encoding="utf-8")
    _write_manifest(root, ["pkg/public.py", "pkg/private.py", "privdir/mod.py"])
    return root


def test_publish_excluded_reads_the_snapshot_exclude_list(tmp_path):
    root = _tree(tmp_path, source=True)
    assert mutation_lint.publish_excluded(root) == ["pkg/private.py", "privdir"]
    assert mutation_lint.publish_excluded(tmp_path / "nowhere") is None


def test_a_public_snapshot_tree_skips_its_excluded_targets_and_reports_them(tmp_path):
    root = _tree(tmp_path, source=False)
    messages, absent = mutation_lint.scan_report(root, root / "mutation" / "targets.json")
    assert [m.split(":")[0] for m in messages] == ["pkg/public.py"], "present targets still lint"
    assert absent == ["pkg/private.py", "privdir/mod.py"]


def test_the_source_tree_fails_on_any_absent_target_even_a_publish_excluded_one(tmp_path):
    root = _tree(tmp_path, source=True)
    with pytest.raises(FileNotFoundError, match="pkg/private.py"):
        mutation_lint.scan(root, root / "mutation" / "targets.json")


def test_cli_in_a_public_snapshot_tree_names_what_it_skipped(tmp_path):
    root = _tree(tmp_path, source=False)
    (root / "pkg" / "public.py").write_text(_handler("except (YAMLError, ValueError)"),
                                            encoding="utf-8")
    result = _run_cli("--repo", str(root), cwd=root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("2 target(s) absent from this public snapshot tree: pkg/private.py, privdir/mod.py"
            in result.stdout)


def test_the_targets_a_public_tree_lacks_are_exactly_the_publish_excluded_ones(tmp_path):
    """On the source tree: build the public view of the manifest's targets and check that what the
    lint skips there is precisely targets ∩ EXCLUDE. The public tree carries no snapshot script,
    so this is proven here, where it exists."""
    excluded = mutation_lint.publish_excluded(REPO)
    if excluded is None:
        pytest.skip("public snapshot tree: publish-snapshot.sh is itself publish-excluded")
    manifest = REPO / "mutation" / "targets.json"
    targets = sorted({e["path"] for e in json.loads(manifest.read_text(encoding="utf-8"))["targets"]})
    private = {t for t in targets
               if any(t == entry or t.startswith(entry.rstrip("/") + "/") for entry in excluded)}
    assert private, "expected at least one publish-excluded target (ci/registry_canary.py)"
    for target in set(targets) - private:
        (tmp_path / target).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / target).write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "mutation").mkdir(exist_ok=True)
    (tmp_path / "mutation" / "targets.json").write_text(manifest.read_text(encoding="utf-8"),
                                                        encoding="utf-8")
    messages, absent = mutation_lint.scan_report(tmp_path, tmp_path / "mutation" / "targets.json")
    assert (messages, absent) == ([], sorted(private))


def test_main_in_process_on_a_public_snapshot_tree(tmp_path, capsys):
    root = _tree(tmp_path, source=False)
    assert mutation_lint.main(["--repo", str(root)]) == 1
    out = capsys.readouterr().out
    assert "pkg/public.py:3:8: `yaml.YAMLError`" in out
    assert "2 target(s) absent from this public snapshot tree" in out


def test_a_snapshot_script_without_an_exclude_list_excludes_nothing(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "publish-snapshot.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    assert mutation_lint.publish_excluded(tmp_path) == []
