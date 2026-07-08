"""Cockpit input-shape regression (pre-release invariant audit).

#19: the Briefings-tab doc view (/api/doc) must render a page whose frontmatter `title` is a
     bare YAML-inferred date/int/list — `.strip()` on the non-str value used to 500 the view,
     while the same page rendered fine in the browse rail / predictions (which str-wrap).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-cockpit" / "app.py"


def _load(vault, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(vault))
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("cockpit_app", None)
    spec = importlib.util.spec_from_file_location("cockpit_app", APP)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cockpit_app"] = m
    spec.loader.exec_module(m)
    return m


def test_api_doc_survives_yaml_date_title(tmp_path, monkeypatch):
    briefings = tmp_path / "wiki" / "briefings"
    briefings.mkdir(parents=True)
    # `title: 2026-07-08` (unquoted) — yaml.safe_load infers datetime.date, not str.
    (briefings / "brief-2026-07-08.md").write_text(
        "---\ntitle: 2026-07-08\n---\n\n# Daily brief\n\nbody\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_streams",
                        lambda: {"brief": {"dir": "briefings", "label": "Daily Brief"}})

    d = m.api_doc(stream="brief", date="2026-07-08")     # must not raise on .strip()
    # non-empty fm title coerces to str; body H1 fallback isn't needed here
    assert d["title"] == "2026-07-08"
    assert "body" in d["html"]
