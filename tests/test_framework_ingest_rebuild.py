import importlib.util, io, json, sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("framework_ingest_rebuild", REPO / "scripts/framework_ingest_rebuild.py")
m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)

def dep(tmp_path):
    d = tmp_path / "dep"; (d / "wiki/entities").mkdir(parents=True); (d / "connectors").mkdir()
    (d / "wiki/entities/a.md").write_text("canonical\n")
    (d / "connectors/acme.yaml").write_text("connector_version: 1\nid: acme.feed\n")
    return d

def call(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err): code = fn(argv)
    return code, out.getvalue(), err.getvalue()

def result(code=0):
    return type("Result", (), {"returncode": code, "stdout": "ok", "stderr": ""})()

def test_ingest_status_and_named_run(tmp_path, monkeypatch):
    d = dep(tmp_path); calls = []
    monkeypatch.setattr(m, "_run", lambda script, deployment, args=None: calls.append((script,args)) or result())
    code, out, _ = call(m.ingest, ["status", str(d), "--json"])
    assert code == 0 and json.loads(out)["connectors"] == ["acme.feed"]
    code, out, _ = call(m.ingest, ["run", str(d), "--connector", "acme.feed", "--json"])
    assert code == 0 and json.loads(out)["status"] == "succeeded"
    assert calls[0][0] == "source_connector.py" and "--summary-only" in calls[0][1]

def test_run_builds_the_deployment_environment(tmp_path, monkeypatch):
    d = dep(tmp_path); captured = {}
    monkeypatch.setattr(
        m.subprocess, "run",
        lambda command, **kwargs: captured.update(command=command, **kwargs) or result())
    assert m._run("worker.py", d, ["--one"]).returncode == 0
    assert captured["cwd"] == d and captured["env"]["WIKI_PATH"] == str(d)
    assert Path(captured["command"][-2]).name == "worker.py"
    assert captured["command"][-1] == "--one"
    captured.clear()
    assert m._run("@postgres-projection", d).returncode == 0
    assert captured["command"] == ["docker", "compose", "run", "--rm",
                                    "okengine-projection", "python", "/app/service.py",
                                    "--reconcile"]
    assert captured["cwd"] == d
    assert captured["text"] is True and captured["capture_output"] is True
    assert captured["check"] is False

def test_duplicate_connector_ids_fail_closed(tmp_path):
    d = dep(tmp_path); (d / "connectors/other.yaml").write_text("id: acme.feed\n")
    code, _, err = call(m.ingest, ["status", str(d)])
    assert code == 1 and "duplicate connector id" in err

def test_rebuild_registry_contains_only_derived_generators():
    assert m.REBUILDS == {"indexes": ["rebuild_index.py", "build_index_tree.py"],
                          "dashboards": ["refresh_kb_dashboards.py"],
                          "backlinks": ["backlinks_refresh.py"],
                          "projection": ["@postgres-projection"]}

def test_rebuild_detects_canonical_mutation(tmp_path, monkeypatch):
    d = dep(tmp_path)
    def mutate(*_args, **_kwargs):
        (d / "wiki/entities/a.md").write_text("mutated\n"); return result()
    monkeypatch.setattr(m, "_run", mutate)
    code, _, err = call(m.rebuild, [str(d), "--backlinks"])
    assert code == 1 and "modified canonical" in err

def test_rebuild_runs_selected_derived_family(tmp_path, monkeypatch):
    d = dep(tmp_path); calls = []
    monkeypatch.setattr(m, "_run", lambda script, deployment, args=None: calls.append(script) or result())
    code, out, _ = call(m.rebuild, [str(d), "--indexes", "--json"])
    assert code == 0 and json.loads(out)["status"] == "succeeded"
    assert calls == ["rebuild_index.py", "build_index_tree.py"]
    (d / "wiki/INDEX.md").write_text("derived")
    (d / "wiki/index.md").write_text("derived legacy index")
    (d / "wiki/AGENTS.md").write_text("derived contract pointer")
    assert "wiki/INDEX.md" not in m._canonical_snapshot(d)
    assert "wiki/index.md" not in m._canonical_snapshot(d)
    assert "wiki/AGENTS.md" not in m._canonical_snapshot(d)
    code, out, _ = call(m.rebuild, [str(d), "--projection", "--json"])
    assert code == 0 and json.loads(out)["families"] == ["projection"]
    assert calls[-1] == "@postgres-projection"


def test_ingest_retry_health_and_missing_connector(tmp_path, monkeypatch):
    d = dep(tmp_path)
    health = d / ".okengine/connectors/health"
    health.mkdir(parents=True)
    (health / "bad.json").write_text('{"connector_id":"acme.feed","ok":false}\n')
    (health / "good.json").write_text('{"connector_id":"other","ok":true}\n')
    (health / "invalid.json").write_text("{bad")
    (health / "scalar.json").write_text("[]")
    calls = []
    monkeypatch.setattr(
        m, "_run", lambda script, deployment, args=None: calls.append(args) or result()
    )
    code, out, _ = call(m.ingest, ["retry", str(d), "--failed", "--json"])
    assert code == 0
    assert json.loads(out)["connectors"][0]["connector"] == "acme.feed"
    assert len(calls) == 1

    code, _, err = call(
        m.ingest, ["run", str(d), "--connector", "missing", "--json"]
    )
    assert code == 1 and "connector not found" in err
    code, _, err = call(m.ingest, ["status", str(tmp_path / "missing")])
    assert code == 1 and "not an OKEngine deployment" in err


def test_manifest_shape_and_yaml_errors(tmp_path):
    d = dep(tmp_path)
    (d / "connectors/acme.yaml").unlink()
    (d / "connectors/no-id.yaml").write_text("name: absent\n")
    code, _, err = call(m.ingest, ["status", str(d)])
    assert code == 1 and "has no id" in err
    (d / "connectors/no-id.yaml").write_text("invalid: [")
    code, _, err = call(m.ingest, ["status", str(d)])
    assert code == 1 and "invalid connector manifest" in err


def test_sources_hydrate_reconcile_and_failure(tmp_path, monkeypatch):
    d = dep(tmp_path)
    code, _, err = call(m.sources, ["hydrate", str(d), "--missing-body"])
    assert code == 1 and "feed configuration not found" in err
    feeds = d / "feeds"
    feeds.mkdir(exist_ok=True)
    (feeds / "feeds.opml").write_text("<opml/>")
    calls = []

    def run(script, deployment, args=None):
        calls.append((script, args))
        return result(0 if script == "feed_fetch.py" else 3)

    monkeypatch.setattr(m, "_run", run)
    code, out, _ = call(
        m.sources, ["hydrate", str(d), "--missing-body", "--json"]
    )
    assert code == 0 and json.loads(out)["operation"] == "hydrate"
    assert "--capture-full-text" in calls[-1][1]
    code, out, _ = call(m.sources, ["reconcile", str(d), "--json"])
    assert code == 3 and json.loads(out)["status"] == "failed"
    assert calls[-1][0] == "classify_sources.py"


def test_rebuild_all_derived_reports_child_failure(tmp_path, monkeypatch):
    d = dep(tmp_path)
    calls = []

    def run(script, deployment, args=None):
        calls.append(script)
        return result(1 if script == "backlinks_refresh.py" else 0)

    monkeypatch.setattr(m, "_run", run)
    code, out, _ = call(m.rebuild, [str(d), "--all-derived", "--json"])
    payload = json.loads(out)
    assert code == 1 and payload["status"] == "failed"
    assert payload["families"] == ["indexes", "dashboards", "backlinks", "projection"]
    assert calls == [
        "rebuild_index.py",
        "build_index_tree.py",
        "refresh_kb_dashboards.py",
        "backlinks_refresh.py",
        "@postgres-projection",
    ]
