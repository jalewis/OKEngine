"""Regression: actor_risk_rank must resolve the vault from WIKI_PATH, never os.getcwd().

The lane shipped `Path(os.environ.get("VAULT_DIR") or os.getcwd())`, so when the cron ran from an
arbitrary directory it scored the wrong tree (or bailed) — the exact backlinks_refresh cwd-resolution
regression. This locks in WIKI_PATH-first resolution with a /opt/vault default and no cwd fallback.
"""
import importlib.util
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "extensions" / "okengine.actor-risk-ranking" / "actor_risk_rank.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load():
    spec = importlib.util.spec_from_file_location("actor_risk_rank", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


m = _load()


def test_resolves_from_wiki_path_not_cwd(tmp_path, monkeypatch):
    good = tmp_path / "vault"
    (good / "wiki").mkdir(parents=True)
    bad = tmp_path / "elsewhere"
    bad.mkdir()
    monkeypatch.setenv("WIKI_PATH", str(good))
    monkeypatch.delenv("VAULT_DIR", raising=False)
    monkeypatch.chdir(bad)                      # cwd has no wiki/ — the old code would land here
    assert m._resolve_vault() == good.resolve()


def test_default_is_opt_vault_never_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("WIKI_PATH", raising=False)
    monkeypatch.delenv("VAULT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert m._resolve_vault() == Path("/opt/vault").resolve()   # default, not the cwd


def test_wiki_path_wins_over_legacy_vault_dir(tmp_path, monkeypatch):
    wp = tmp_path / "wp"
    (wp / "wiki").mkdir(parents=True)
    monkeypatch.setenv("WIKI_PATH", str(wp))
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "legacy"))
    assert m._resolve_vault() == wp.resolve()
