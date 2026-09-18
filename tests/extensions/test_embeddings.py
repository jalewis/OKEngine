"""okengine.embeddings (sidecar) — similarity core + manifest is a hardened sidecar."""
import importlib.util
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.embeddings"


def _run():
    spec = importlib.util.spec_from_file_location("emb_run", EXT / "image" / "run.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _comp():
    spec = importlib.util.spec_from_file_location(
        "extension_compose", REPO / "scripts" / "extension_compose.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_cosine_bounds():
    m = _run()
    a = m.vectorize("alpha beta gamma")
    assert abs(m.cosine(a, a) - 1.0) < 1e-9                                 # identical
    assert m.cosine(a, m.vectorize("delta epsilon")) == 0.0      # disjoint
    mid = m.cosine(a, m.vectorize("alpha beta delta"))
    assert 0.0 < mid < 1.0                                       # partial overlap
    assert m.cosine(m.vectorize(""), a) == 0.0


def test_find_similar_pairs_threshold_and_order():
    m = _run()
    docs = [("entities/a/x", "neural scaling laws transformer"),
            ("entities/a/x2", "neural scaling laws transformer model"),   # near-dup of x
            ("entities/b/y", "quantum error correction surface code")]    # unrelated
    pairs = m.find_similar_pairs(docs, threshold=0.5)
    assert pairs and pairs[0][:2] == ("entities/a/x", "entities/a/x2")
    assert all("entities/b/y" not in (a, b) for a, b, _ in pairs)         # unrelated excluded
    assert m.find_similar_pairs(docs, threshold=0.99) == []               # nothing that close


def test_manifest_is_digest_pinned_sidecar():
    d = yaml.safe_load((EXT / "extension.yaml").read_text())
    assert d["trust"] == "sidecar"
    img = d["operation"]["entrypoint"]["image"]
    assert img["digest"].startswith("sha256:")                  # pinned (placeholder until built)


def test_manifest_generates_hardened_service():
    """The sidecar service this manifest yields is OS-hardened (okengine#124)."""
    c = _comp()
    d = yaml.safe_load((EXT / "extension.yaml").read_text())
    spec = {"id": "okengine.embeddings", "image": c.image_ref(d["operation"]["entrypoint"]["image"]),
            "command": None, "config": {}}
    svc = c.render_sidecar_service(spec, "u", "u", "R", "W")
    assert "network_mode" not in svc and svc["cap_drop"] == ["ALL"]
    assert svc["read_only"] is True and "no-new-privileges:true" in svc["security_opt"]


def test_reference_sidecar_env_publish_and_main(monkeypatch, capsys):
    m = _run()
    monkeypatch.delenv("OKENGINE_MCP_URL", raising=False)
    assert m._require_env("OKENGINE_MCP_URL") == ""
    assert "absent" in capsys.readouterr().err
    monkeypatch.setenv("OKENGINE_MCP_URL", "http://read")
    monkeypatch.setenv("OKENGINE_READ_TOKEN", "r")
    assert m.fetch_entities() == []

    monkeypatch.setenv("OKENGINE_WRITE_MCP_URL", "http://write")
    monkeypatch.setenv("OKENGINE_WRITE_TOKEN", "w")
    m.publish([("a", "b", 0.999)] * 51)
    output = capsys.readouterr().out
    assert "51 semantic" in output and output.count("a  ~  b") == 50

    monkeypatch.setenv("OKENGINE_CONFIG_THRESHOLD", "0.75")
    monkeypatch.setenv("OKENGINE_EXTENSION_ID", "demo.embedding")
    monkeypatch.setattr(m, "fetch_entities", lambda: [("a", "same"), ("b", "same")])
    assert m.main() == 0
    output = capsys.readouterr().out
    assert "[demo.embedding]" in output and "threshold=0.75" in output
