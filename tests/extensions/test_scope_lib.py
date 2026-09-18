"""Pack-owned boundary parsing and reversible flag behavior."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE = Path(__file__).resolve().parents[2] / "extensions/okengine.relevance-gate/scope_lib.py"


def _load():
    spec = importlib.util.spec_from_file_location("scope_lib_contract", MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_load_scope_rejects_missing_malformed_and_non_mapping_shapes(tmp_path):
    module = _load()
    assert module.load_scope(tmp_path) is None
    schema = tmp_path / "schema.yaml"
    for content in ("[broken", "- list\n- root\n", "pack_config: string\n",
                    "pack_config: {scope: {statement: only}}\n"):
        schema.write_text(content)
        assert module.load_scope(tmp_path) is None
    schema.write_text("pack_config:\n  scope:\n    in_scope: [alpha platform]\n")
    assert module.load_scope(tmp_path)["in_scope"] == ["alpha platform"]


def test_terms_scoring_and_shared_terms_err_toward_keep():
    module = _load()
    inside, outside = module.compile_scope({
        "in_scope": ["The cyber-security platform", None],
        "out_of_scope": ["generic platform cooking", "x --baking--"],
    })
    assert inside == {"cyber-security", "platform", "none"}
    assert outside == {"generic", "cooking", "baking"}
    assert module.score("CYBER-security cooking cooking", inside, outside) == (1, 1, ["cooking"])


def test_page_blob_and_reversible_flag_edges(tmp_path):
    module = _load()
    missing = tmp_path / "missing.md"
    assert module.page_blob(missing) == ({}, "")
    plain = tmp_path / "plain.md"
    plain.write_text("Body Alpha")
    assert module.page_blob(plain) == ({}, "plain  body alpha")
    malformed = tmp_path / "malformed.md"
    malformed.write_text("---\n[bad\n---\nBody")
    fm, blob = module.page_blob(malformed)
    assert fm == {} and "body" in blob
    sequence = tmp_path / "sequence.md"
    sequence.write_text("---\n- not\n- mapping\n---\nBody")
    assert module.page_blob(sequence)[0] == {}

    page = tmp_path / "page.md"
    page.write_text("---\ntype: source\ntitle: Example\n---\nBody")
    fm, blob = module.page_blob(page, excerpt_chars=2)
    assert fm["title"] == "Example" and blob.endswith("\nb")
    assert module.flag(page, "operator decision") is True
    assert "off_scope: true\nscope_reason: operator decision" in page.read_text()
    assert module.flag(page, "again") is False
    assert module.flag(plain, "no frontmatter") is False
