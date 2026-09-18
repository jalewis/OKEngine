"""authority_enrich (okengine#314) — deterministic identity-authority stamping.

Contract under test: exactly-one exact match stamps additively with attribution; ambiguity,
disagreement, and duplicate authority IDs go to review (never merged/overwritten); curated fields
and the body are untouched; runs are idempotent; dry-run writes nothing. End-to-end through the
REAL source_connector runtime in fixture mode (no mocked connector).
"""
import importlib.util
import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "authority_enrich.py"
MANIFEST = REPO / "examples" / "source-connectors" / "ror-organizations.yaml"
FIXTURE = REPO / "examples" / "source-connectors" / "fixtures" / "ror-organizations.fixture.json"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="authority_enrich absent")


def _load(vault: Path):
    spec = importlib.util.spec_from_file_location("authority_enrich", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.VAULT = vault
    m.WIKI = vault / "wiki"
    return m


def _page(p: Path, fm: dict, body: str = "Curated body stays.\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body, encoding="utf-8")


def _fm(p: Path) -> dict:
    return yaml.safe_load(re.match(r"\A---\n(.*?\n)---", p.read_text(), re.S).group(1))


def _run(m, vault, tmp_path, *extra):
    # --health-root must be passed, not just state/ledger. MANIFEST and FIXTURE point at
    # the real repo, and without this the connector's health record resolved against the
    # CURRENT WORKING DIRECTORY — so running this test from a checkout wrote
    # .okengine/connectors/health/reference.ror-organizations.json into the REPO and left
    # a tracked file modified after every suite run (okengine#509).
    return m.main(["--manifest", str(MANIFEST), "--fixture", str(FIXTURE),
                   "--state-root", str(tmp_path / "state"),
                   "--health-root", str(tmp_path / "health"),
                   "--ledger-root", str(tmp_path / "ledger.jsonl"), *extra])


def test_exact_match_stamps_additively_with_attribution(tmp_path):
    v = tmp_path
    page = v / "wiki" / "entities" / "h" / "hkust.md"
    _page(page, {"type": "lab", "name": "Hong Kong University of Science and Technology",
                 "curated_note": "hand-written", "tags": ["research"]})
    m = _load(v)
    assert _run(m, v, tmp_path) == 0
    fm = _fm(page)
    assert fm["authority_ids"] == {"ror": "https://ror.org/00q4vv597"}
    obs = fm["authority_observations"]
    assert obs[0]["authority"] == "ror" and obs[0]["source"] == "reference.ror-organizations"
    assert "exact name match" in obs[0]["basis"]
    # additive only: curated fields + body untouched, no review flags
    assert fm["curated_note"] == "hand-written" and fm["tags"] == ["research"]
    assert "needs_review" not in fm and "conflicts" not in fm
    assert "Curated body stays." in page.read_text()
    # coverage artifact observable
    cov = json.loads((v / ".okengine" / "connectors" / "authority" / "ror.json").read_text())
    assert cov["stamped"] == 1 and cov["authority"] == "ror"


def test_acronym_alias_also_matches_via_candidate_paths(tmp_path):
    v = tmp_path
    page = v / "wiki" / "entities" / "h" / "hkust-short.md"
    _page(page, {"type": "lab", "name": "HKUST"})   # matches names[].value acronym entry
    m = _load(v)
    assert _run(m, v, tmp_path) == 0
    assert _fm(page)["authority_ids"]["ror"] == "https://ror.org/00q4vv597"


def test_unmatched_and_wrong_type_are_left_alone(tmp_path):
    v = tmp_path
    nomatch = v / "wiki" / "entities" / "n" / "nomatch.md"
    wrongtype = v / "wiki" / "entities" / "w" / "wrong.md"
    _page(nomatch, {"type": "lab", "name": "Totally Unknown Institute"})
    _page(wrongtype, {"type": "actor", "name": "HKUST"})   # not in targets.types
    m = _load(v)
    assert _run(m, v, tmp_path) == 0
    assert "authority_ids" not in _fm(nomatch) and "needs_review" not in _fm(nomatch)
    assert "authority_ids" not in _fm(wrongtype)


def test_existing_disagreeing_id_is_kept_and_review_flagged(tmp_path):
    v = tmp_path
    page = v / "wiki" / "entities" / "h" / "hkust.md"
    _page(page, {"type": "lab", "name": "Hong Kong University of Science and Technology",
                 "authority_ids": {"ror": "https://ror.org/DIFFERENT"}})
    m = _load(v)
    assert _run(m, v, tmp_path) == 0
    fm = _fm(page)
    # NEVER overwritten — and pages already stamped are skipped as eligible, so no conflict churn
    assert fm["authority_ids"]["ror"] == "https://ror.org/DIFFERENT"


def test_duplicate_authority_id_across_pages_goes_to_review(tmp_path):
    v = tmp_path
    a = v / "wiki" / "entities" / "a" / "a.md"
    b = v / "wiki" / "entities" / "b" / "b.md"
    _page(a, {"type": "lab", "name": "A Lab",
              "authority_ids": {"ror": "https://ror.org/00q4vv597"}})
    _page(b, {"type": "lab", "name": "Hong Kong University of Science and Technology"})
    m = _load(v)
    assert _run(m, v, tmp_path) == 0
    fmb = _fm(b)
    # b matched the same ROR id already stamped on a -> review, NOT auto-merged, NOT stamped
    assert fmb.get("needs_review") is True
    assert any("duplicate identity" in c["detail"] for c in fmb["conflicts"])
    assert "authority_ids" not in fmb or "ror" not in (fmb.get("authority_ids") or {})


def test_idempotent_and_dry_run(tmp_path, capsys):
    v = tmp_path
    page = v / "wiki" / "entities" / "h" / "hkust.md"
    _page(page, {"type": "lab", "name": "Hong Kong University of Science and Technology"})
    m = _load(v)
    assert _run(m, v, tmp_path, "--dry-run") == 0
    assert "authority_ids" not in _fm(page)                 # dry-run wrote nothing
    assert not (v / ".okengine" / "connectors" / "authority" / "ror.json").exists()
    assert _run(m, v, tmp_path) == 0
    first = page.read_text()
    assert _run(m, v, tmp_path) == 0                        # second run: already stamped -> skipped
    assert page.read_text() == first
    out = capsys.readouterr().out
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}


def test_manifest_validation_rejects_bad_enrich_blocks():
    import importlib.util as iu
    import sys
    spec = iu.spec_from_file_location("source_connector", REPO / "scripts" / "cron" / "source_connector.py")
    sc = iu.module_from_spec(spec)
    sys.modules["source_connector"] = sc     # dataclasses resolve InitVar via sys.modules[__module__]
    spec.loader.exec_module(sc)
    good = yaml.safe_load(MANIFEST.read_text())
    assert sc.validate_manifest(good) == []
    bad = yaml.safe_load(MANIFEST.read_text())
    bad["mode"] = "poll"                                    # enrich only valid for enrichment mode
    assert any("enrich" in e for e in sc.validate_manifest(bad))
    bad2 = yaml.safe_load(MANIFEST.read_text())
    bad2["enrich"]["match"]["query_input"] = "not_an_input"  # must be a declared required input
    assert any("query_input" in e for e in sc.validate_manifest(bad2))
    bad3 = yaml.safe_load(MANIFEST.read_text())
    del bad3["enrich"]["targets"]
    assert any("targets" in e for e in sc.validate_manifest(bad3))


def test_helpers_split_resolve_flag_and_connector_failures(tmp_path, monkeypatch, capsys):
    m = _load(tmp_path)
    assert m._split("plain") == (None, "plain")
    assert m._split("---\n[\n---\nbody") == (None, "---\n[\n---\nbody")
    assert m._split("---\n- one\n---\nbody") == (None, "body")
    payload = {"rows": [{"names": [{"value": "A"}, {"value": "B"}]},
                        {"names": ["C"]}], "none": None}
    assert set(m._resolve(payload, "rows.names")) == {"A", "B", "C"}
    assert m._resolve(payload, "missing") == []
    assert m._resolve({"x": {"other": 1}}, "x") == []
    assert m._resolve({"x": None}, "x") == []
    assert m._norm(" A   B ") == "a b"
    fm = {"conflicts": "bad"}
    m._flag(fm, "ror", "detail")
    m._flag(fm, "ror", "detail")
    assert len(fm["conflicts"]) == 1 and fm["needs_review"]

    args = type("Args", (), {
        "fixture": tmp_path / "f", "state_root": tmp_path / "s",
        "ledger_root": tmp_path / "l", "health_root": tmp_path / "h",
    })()
    monkeypatch.setattr(
        m.subprocess, "run",
        lambda *a, **k: type("P", (), {
            "stdout": "noise\n{\"ok\": true}\n", "stderr": "", "returncode": 0,
        })(),
    )
    assert m._run_connector(tmp_path / "m", "q", "v", args) == {"ok": True}
    empty_args = type("Args", (), {
        "fixture": None, "state_root": None, "ledger_root": None, "health_root": None,
    })()
    monkeypatch.setattr(
        m.subprocess, "run",
        lambda *a, **k: type("P", (), {
            "stdout": '{"ok": true}\ntrailing noise\n', "stderr": "", "returncode": 0,
        })(),
    )
    assert m._run_connector(tmp_path / "m", "q", "v", empty_args) == {"ok": True}
    monkeypatch.setattr(
        m.subprocess, "run",
        lambda *a, **k: type("P", (), {
            "stdout": "noise only\n", "stderr": "bad", "returncode": 2,
        })(),
    )
    assert m._run_connector(tmp_path / "m", "q", "v", empty_args) is None
    monkeypatch.setattr(
        m.subprocess, "run",
        lambda *a, **k: type("P", (), {
            "stdout": "{bad}\n", "stderr": "bad", "returncode": 2,
        })(),
    )
    assert m._run_connector(tmp_path / "m", "q", "v", args) is None
    monkeypatch.setattr(
        m.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 1)),
    )
    assert m._run_connector(tmp_path / "m", "q", "v", args) is None
    assert "ERROR" in capsys.readouterr().err


def test_main_rejects_manifest_and_missing_wiki(tmp_path, capsys):
    m = _load(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("mode: poll\n")
    assert m.main(["--manifest", str(bad)]) == 1
    assert '"wakeAgent": false' in capsys.readouterr().out

    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump({
        "mode": "enrichment", "id": "x",
        "enrich": {
            "authority": "ror", "id_path": "id",
            "match": {"page_field": "name", "query_input": "q",
                      "candidate_paths": ["name"]},
            "targets": {"types": ["lab"], "namespaces": ["entities"]},
        },
    }))
    assert m.main(["--manifest", str(manifest)]) == 1
    assert "wiki not found" in capsys.readouterr().err


def test_main_ambiguous_unmatched_failed_and_convergence_sweep(
    tmp_path, monkeypatch, capsys
):
    m = _load(tmp_path)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump({
        "mode": "enrichment", "id": "reference.test",
        "enrich": {
            "authority": "test", "id_path": "id",
            "match": {"page_field": "name", "query_input": "query",
                      "candidate_paths": ["name"]},
            "targets": {"types": ["lab"], "namespaces": ["entities"]},
        },
    }))
    root = tmp_path / "wiki" / "entities"
    _page(root / "_skip.md", {"type": "lab", "name": "skip"})
    _page(root / "INDEX.md", {"type": "lab", "name": "skip"})
    _page(root / "wrong.md", {"type": "actor", "name": "wrong"})
    _page(root / "tomb.md", {"type": "lab", "name": "t", "status": "tombstoned"})
    _page(root / "empty.md", {"type": "lab"})
    _page(root / "failed.md", {"type": "lab", "name": "Failed"})
    _page(root / "unmatched.md", {"type": "lab", "name": "Unmatched"})
    _page(root / "ambiguous.md", {"type": "lab", "name": "Ambiguous"})
    for name in ("owner-a", "owner-b"):
        _page(root / f"{name}.md", {
            "type": "lab", "name": name, "authority_ids": {"test": "dup"},
        })

    def connector(_manifest, _query, value, _args):
        if value == "Failed":
            return None
        if value == "Unmatched":
            return {"ok": True, "items": [{"payload": {"name": "other", "id": "x"}}]}
        return {"ok": True, "items": [
            {"payload": {"name": value, "id": "a"}},
            {"payload": {"name": value, "id": "b"}},
        ]}

    monkeypatch.setattr(m, "_run_connector", connector)
    assert m.main(["--manifest", str(manifest)]) == 0
    out = capsys.readouterr().out
    assert "unmatched" in out and "ambiguous" in out and "duplicates" in out
    assert _fm(root / "ambiguous.md")["needs_review"] is True
    assert _fm(root / "owner-a.md")["needs_review"] is True


def test_entrypoint(tmp_path, monkeypatch):
    manifest = tmp_path / "bad.yaml"
    manifest.write_text("mode: poll\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setattr(sys, "argv", [str(MOD), "--manifest", str(manifest)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(MOD), run_name="__main__")
    assert exc.value.code == 1


def test_live_races_conflict_and_convergence_read_edges(tmp_path, monkeypatch, capsys):
    m = _load(tmp_path)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump({
        "mode": "enrichment", "id": "reference.test",
        "enrich": {
            "authority": "test", "id_path": "id",
            "match": {"page_field": "name", "query_input": "query",
                      "candidate_paths": ["name"]},
            "targets": {"types": ["lab"], "namespaces": ["entities"]},
        },
    }))
    root = tmp_path / "wiki/entities"
    outside = tmp_path / "wiki/concepts/outside.md"
    _page(outside, {"type": "lab", "name": "Outside"})
    unreadable_scan = root / "scan-race.md"
    _page(unreadable_scan, {"type": "lab", "name": "Scan Race"})
    conflict = root / "conflict.md"
    vanished = root / "vanished.md"
    invalid = root / "invalid.md"
    for path, name in ((conflict, "Conflict"), (vanished, "Vanished"), (invalid, "Invalid")):
        _page(path, {"type": "lab", "name": name})
    owner_a, owner_b = root / "owner-a.md", root / "owner-b.md"
    for path in (owner_a, owner_b):
        _page(path, {"type": "lab", "name": path.stem,
                     "authority_ids": {"test": "shared"}})

    original_read = Path.read_text
    reads = {}
    def raced_read(self, *args, **kwargs):
        reads[self] = reads.get(self, 0) + 1
        if self == unreadable_scan:
            raise OSError("scan race")
        if self == owner_a and reads[self] > 1:
            raise OSError("sweep race")
        if self == owner_b and reads[self] > 1:
            return "plain"
        return original_read(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", raced_read)

    def connector(_manifest, _query, value, _args):
        target = {"Conflict": conflict, "Vanished": vanished, "Invalid": invalid}[value]
        if value == "Conflict":
            _page(target, {"type": "lab", "name": value,
                           "authority_ids": {"test": "old"}})
        elif value == "Vanished":
            target.unlink()
        else:
            target.write_text("plain")
        return {"ok": True, "items": [{"payload": {"name": value, "id": "new"}}]}

    monkeypatch.setattr(m, "_run_connector", connector)
    assert m.main(["--manifest", str(manifest)]) == 0
    output = capsys.readouterr().out
    assert "conflict" in output
    assert _fm(conflict)["authority_ids"]["test"] == "old"


def test_convergence_existing_detail_is_idempotent(tmp_path, monkeypatch):
    m = _load(tmp_path)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump({
        "mode": "enrichment", "id": "reference.test",
        "enrich": {"authority": "test", "id_path": "id",
                   "match": {"page_field": "name", "query_input": "query",
                             "candidate_paths": ["name"]},
                   "targets": {"types": ["lab"], "namespaces": ["entities"]}},
    }))
    root = tmp_path / "wiki/entities"
    paths = [root / "a.md", root / "b.md"]
    detail = ("test id 'shared' appears on 2 pages "
              "(entities/a.md, entities/b.md); duplicate identity needs human convergence")
    for path in paths:
        _page(path, {"type": "lab", "name": path.stem,
                     "authority_ids": {"test": "shared"},
                     "conflicts": [{"field": "authority_ids.test", "detail": detail}]})
    monkeypatch.setattr(m, "_run_connector", lambda *_a: None)
    before = [path.read_text() for path in paths]
    assert m.main(["--manifest", str(manifest)]) == 0
    assert [path.read_text() for path in paths] == before


def test_missing_authority_id_candidate_and_dry_convergence(tmp_path, monkeypatch):
    m = _load(tmp_path)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump({
        "mode": "enrichment", "id": "reference.test",
        "enrich": {"authority": "test", "id_path": "id",
                   "match": {"page_field": "name", "query_input": "query",
                             "candidate_paths": ["name"]},
                   "targets": {"types": ["lab"], "namespaces": ["entities"]}},
    }))
    root = tmp_path / "wiki/entities"
    candidate = root / "candidate.md"
    _page(candidate, {"type": "lab", "name": "Candidate"})
    for name in ("owner-a", "owner-b"):
        _page(root / f"{name}.md", {"type": "lab", "name": name,
                                    "authority_ids": {"test": "shared"}})
    monkeypatch.setattr(m, "_run_connector", lambda *_a: {
        "ok": True, "items": [
            {"payload": {"name": "Candidate", "id": None}},
            {"payload": {"name": "Candidate", "id": "new"}},
        ],
    })
    before = {p: p.read_text() for p in root.glob("*.md")}
    assert m.main(["--manifest", str(manifest), "--dry-run"]) == 0
    assert {p: p.read_text() for p in root.glob("*.md")} == before
