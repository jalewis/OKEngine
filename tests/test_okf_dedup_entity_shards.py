"""okengine#48: reconcile duplicate entity canonicals (two-level -> one-level), content-safe."""
import importlib.util
from pathlib import Path

import pytest

MOD = Path(__file__).resolve().parent.parent / "scripts" / "okf_dedup_entity_shards.py"
pytest.importorskip("yaml")


def _load():
    spec = importlib.util.spec_from_file_location("dedup", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _w(vault, rel, text):
    p = vault / "wiki" / "entities" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_relocate_two_level_only(tmp_path):
    m = _load()
    _w(tmp_path, "c/a/carbon.md", "---\ntype: malware\nname: Carbon\n---\nCarbon is a backdoor.\n")
    m.main(["--vault", str(tmp_path), "--apply"])
    one = tmp_path / "wiki" / "entities" / "c" / "carbon.md"
    assert one.is_file() and "Carbon is a backdoor." in one.read_text()
    assert not (tmp_path / "wiki" / "entities" / "c" / "a" / "carbon.md").exists()


def test_merge_keeps_assembled_fm_and_takes_rich_body(tmp_path):
    """Bare one-level (assembled fm) + rich two-level body -> one page with both."""
    m = _load()
    _w(tmp_path, "c/cve-2021-44228.md",
       "---\ntype: vulnerability\nname: cve-2021-44228\nseverity: critical\ncvss_base: 10.0\n"
       "assembled_from: [cisa-kev, nvd]\n---\ncve-2021-44228.\n")
    _w(tmp_path, "c/v/cve-2021-44228.md",
       "---\ntype: vulnerability\ncve_id: CVE-2021-44228\ntitle: Log4Shell\n---\n"
       "Apache Log4j2 JNDI remote code execution.\n")
    m.main(["--vault", str(tmp_path), "--apply"])
    one = tmp_path / "wiki" / "entities" / "c" / "cve-2021-44228.md"
    txt = one.read_text()
    assert "Apache Log4j2 JNDI" in txt                       # rich body taken
    assert "severity: critical" in txt and "assembled_from" in txt   # assembled fm kept
    assert "title: Log4Shell" in txt                         # legacy fm field filled in
    assert not (tmp_path / "wiki" / "entities" / "c" / "v" / "cve-2021-44228.md").exists()


def test_merge_appends_unique_agent_sections(tmp_path):
    """One-level has agent prose + a section; two-level has a DIFFERENT section -> appended, and
    the one-level's own content is never clobbered."""
    m = _load()
    _w(tmp_path, "a/apt29.md",
       "---\ntype: intrusion-set\nname: APT29\n---\nReal analysis.\n\n## Assessment\nKept.\n")
    _w(tmp_path, "a/p/apt29.md",
       "---\ntype: intrusion-set\nname: APT29\n---\nstub.\n\n## Sightings\nFrom legacy.\n")
    m.main(["--vault", str(tmp_path), "--apply"])
    txt = (tmp_path / "wiki" / "entities" / "a" / "apt29.md").read_text()
    assert "Real analysis." in txt and "## Assessment" in txt   # one-level content preserved
    assert "## Sightings" in txt and "From legacy." in txt      # two-level unique section appended
    assert not (tmp_path / "wiki" / "entities" / "a" / "p" / "apt29.md").exists()


def test_strips_envelope_on_merge(tmp_path):
    m = _load()
    _w(tmp_path, "c/cve-2020-1.md", "---\ntype: vulnerability\nname: cve-2020-1\nseverity: high\n---\ncve.\n")
    _w(tmp_path, "c/v/cve-2020-1.md",
       "---\ntype: vulnerability\ncredibility: '1'\nsource: cisa-kev\n---\nReal desc.\n")
    m.main(["--vault", str(tmp_path), "--apply"])
    txt = (tmp_path / "wiki" / "entities" / "c" / "cve-2020-1.md").read_text()
    assert "credibility" not in txt and "source:" not in txt and "Real desc." in txt


def test_helpers_malformed_yaml_and_multiple_sections():
    m = _load()
    assert m._fm("[") == {}
    assert m._sections("plain body") == {}
    sections = m._sections("## One\na\n## Two\nb\n")
    assert sections == {"## One": "a", "## Two": "b"}


def test_dry_run_index_backup_and_pruning_paths(tmp_path, capsys):
    m = _load()
    index = _w(tmp_path, "a/b/INDEX.md", "---\ntype: dashboard\n---\nindex\n")
    dashboard = _w(tmp_path, "a/b/nav.md", "---\ntype: dashboard\n---\nnav\n")
    relocate = _w(tmp_path, "c/a/carbon.md", "---\ntype: malware\n---\nrich\n")
    # A non-empty sibling directory remains while emptied two-level directories prune.
    keep = tmp_path / "wiki/entities/keep"
    keep.mkdir(parents=True)
    (keep / "note.txt").write_text("keep")

    assert m.main(["--vault", str(tmp_path)]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert index.exists() and dashboard.exists() and relocate.exists()

    backup = tmp_path / "backup"
    assert m.main([
        "--vault", str(tmp_path), "--apply", "--backup-dir", str(backup),
    ]) == 0
    assert (backup / "entities/a/b/INDEX.md").is_file()
    assert (backup / "entities/a/b/nav.md").is_file()
    assert (backup / "entities/c/a/carbon.md").is_file()
    assert not index.exists() and not dashboard.exists()
    assert (tmp_path / "wiki/entities/c/carbon.md").is_file()
    assert keep.is_dir()
