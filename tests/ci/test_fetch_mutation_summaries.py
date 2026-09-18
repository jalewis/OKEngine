import importlib.util
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "ci" / "fetch_mutation_summaries.py"


def load():
    spec = importlib.util.spec_from_file_location("fetch_mutation_summaries_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("returncode,stdout,expected", [
    (1, "{}", None),
    (0, "diagnostic only", None),
    (0, "warning\n{bad", None),
    (0, 'warning\n{"ok": 1}', {"ok": 1}),
    (0, 'prefix\n[1, 2]', [1, 2]),
])
def test_glab_json_parses_payload_after_diagnostics(
        monkeypatch, returncode, stdout, expected):
    m = load()

    class Result:
        pass

    result = Result()
    result.returncode = returncode
    result.stdout = stdout
    monkeypatch.setattr(m.subprocess, "run", lambda *args, **kwargs: result)
    assert m.glab_json("projects/24") == expected


def test_main_downloads_terminal_job_and_accounts_for_holes(
        tmp_path, monkeypatch, capsys):
    m = load()
    out = tmp_path / "summaries"
    out.mkdir()
    (out / "summary-2.json").write_text("existing")
    pipelines = [{"id": value} for value in range(1, 6)]

    def api(path):
        if "pipelines?" in path:
            return pipelines
        if "/jobs/40/" in path:
            return None
        if "/jobs/50/" in path:
            return {"overall": {"score": 90}}
        pipeline = int(path.split("/pipelines/", 1)[1].split("/", 1)[0])
        if path.endswith("/jobs?per_page=100"):
            return {
                1: [],
                2: [{"id": 20, "name": "mutation-full", "status": "success"}],
                3: [{"id": 30, "name": "lint", "status": "success"}],
                4: [{"id": 40, "name": "mutation-critical", "status": "failed"}],
                5: [{"id": 50, "name": "mutation-full", "status": "success"}],
            }[pipeline]
        raise AssertionError(path)

    monkeypatch.setattr(m, "glab_json", api)
    assert m.main(["--out", str(out)]) == 0
    assert json.loads((out / "summary-5.json").read_text())["overall"]["score"] == 90
    captured = capsys.readouterr()
    assert "1 new, 1 already present" in captured.out
    assert "no summary artifact for pipeline 4" in captured.err


def test_main_fails_closed_when_pipeline_window_or_artifacts_are_empty(
        tmp_path, monkeypatch):
    m = load()
    monkeypatch.setattr(m, "glab_json", lambda path: [])
    with pytest.raises(SystemExit, match="window is UNDETECTABLE"):
        m.main(["--out", str(tmp_path / "one")])

    def no_terminal_jobs(path):
        if "pipelines?" in path:
            return [{"id": 1}]
        return [{"id": 10, "name": "lint", "status": "success"}]

    monkeypatch.setattr(m, "glab_json", no_terminal_jobs)
    with pytest.raises(SystemExit, match="nothing fetched"):
        m.main(["--out", str(tmp_path / "two")])
