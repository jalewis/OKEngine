"""Regression: framework list — readable table (header, domain last/truncated) +
--json for tooling (#9)."""
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FL = REPO / "scripts" / "framework_list.py"

_CAT = {"catalog": "okpacks-library", "packs": [
    {"name": "okpack-x", "engine_version": "v0.2.0", "trust": "public", "status": "example",
     "domain": "short"},
    {"name": "okpack-ai-research", "engine_version": "v0.2.0", "trust": "public",
     "status": "community",
     "domain": "AI / LLM research watch — models, labs, techniques, benchmarks, predictions, and more text to overflow"},
]}


def _load():
    spec = importlib.util.spec_from_file_location("framework_list", FL)
    m = importlib.util.module_from_spec(spec)
    sys.modules["framework_list"] = m
    spec.loader.exec_module(m)
    return m


def test_human_table_has_header_and_truncates_long_domain(tmp_path, capsys):
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps(_CAT))
    assert _load().main(["--catalog", str(cat)]) == 0
    out = capsys.readouterr().out
    assert "NAME" in out and "ENGINE" in out and "DOMAIN" in out      # header row
    assert "…" in out                                                 # long domain truncated
    # version/trust/status stay aligned (domain is last) — no run-together
    assert "v0.2.0   public" in out


def test_json_flag_emits_valid_catalog(tmp_path, capsys):
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps(_CAT))
    assert _load().main(["--catalog", str(cat), "--json"]) == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["catalog"] == "okpacks-library" and len(parsed["packs"]) == 2
