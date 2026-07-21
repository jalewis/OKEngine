import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "entity_converge.py"


def _load():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("entity_converge", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["entity_converge"] = mod
    spec.loader.exec_module(mod)
    return mod


def _page(root, rel, fm, body="body\n"):
    p = root / "wiki" / (rel + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + fm + "---\n" + body, encoding="utf-8")
    return p


def test_gentlemen_fragments_converge_to_grounded_reviewed_record(tmp_path):
    m = _load()
    (tmp_path / "schema.yaml").write_text("types: {actor: {required: [type]}}\n")
    good = _page(tmp_path, "entities/g/gentlemen-ransomware-group",
                 "type: actor\ntitle: The Gentlemen ransomware group\n"
                 "aliases: [The Gentlemen, Storm-2697]\nneeds_review: false\n"
                 "sources: [sources/2026/07/unit42]\n")
    bad = _page(tmp_path, "entities/g/gentlemen-ransomware-group-storm-2698",
                "type: actor\nname: Gentlemen\naliases: [The Gentlemen, Storm-2697]\n"
                "needs_review: true\nsources: [https://example.invalid/blog]\n")
    ref = _page(tmp_path, "briefings/weekly", "type: briefing\n",
                "See [[entities/g/gentlemen-ransomware-group-storm-2698]].\n")
    approval = {"entities/g/gentlemen-ransomware-group-storm-2698":
                "entities/g/gentlemen-ransomware-group"}
    result = m.run(tmp_path, apply=True, approved=approval)
    assert result["mapping"] == {"entities/g/gentlemen-ransomware-group-storm-2698":
                                  "entities/g/gentlemen-ransomware-group"}
    assert "status: tombstoned" in bad.read_text()
    assert "entities/g/gentlemen-ransomware-group]]" in ref.read_text()
    assert "https://example.invalid/blog" in good.read_text()  # provenance retained as additive


def test_lone_shared_alias_is_not_converged(tmp_path):
    m = _load()
    _page(tmp_path, "entities/s/sandworm", "type: actor\nname: Sandworm\naliases: [IRIDIUM]\n")
    _page(tmp_path, "entities/i/iranian-iridium", "type: actor\nname: Iridium\naliases: []\n")
    assert m.run(tmp_path)["clusters"] == []


def test_apply_without_reviewed_mapping_fails_closed(tmp_path):
    m = _load()
    _page(tmp_path, "entities/a/a-one", "type: actor\nname: Same Actor\naliases: [APT9000]\n")
    _page(tmp_path, "entities/a/a-two", "type: actor\nname: Same Actor\naliases: [APT9000]\n")
    try:
        m.run(tmp_path, apply=True)
    except ValueError as exc:
        assert "reviewed" in str(exc)
    else:
        raise AssertionError("heuristic candidates must not authorize convergence")


def test_bridge_component_is_not_auto_merged(tmp_path):
    m = _load()
    records = {
        "a": {"name": "A", "aliases": ["shared-one", "shared-two"]},
        "bridge": {"name": "B", "aliases": ["shared-one", "shared-two", "other-one", "other-two"]},
        "c": {"name": "C", "aliases": ["other-one", "other-two"]},
    }
    assert m.clusters(records) == []
