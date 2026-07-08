"""Regression: the lacuna wake-gate must rotate the MAPPED field out of the batch.

A lacuna page records the field it analyzed in its REQUIRED ``field_mapped`` frontmatter
(a sharded path, e.g. ``concepts/s/supply-chain-compromise``). The wake-gate used to decide
"already analyzed" by scraping every ``[[concepts/…]]`` bracketed link in the page instead —
which both (a) MISSED the mapped field when it was only recorded as the bare ``field_mapped:``
path (not a bracketed link), so the densest, just-analyzed field kept clogging slot #1 of the
batch forever, and (b) retired unrelated concepts the page merely cited, starving the pool.
This locks in reading ``field_mapped`` as the authoritative rotation signal.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "extensions" / "okengine.lacuna" / "select_lacuna_field.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ["OKENGINE_MCP_WRITE_DATE"] = "2026-07-07"   # deterministic _today()/_cutoff()
    sys.modules.pop("select_lacuna_field", None)
    spec = importlib.util.spec_from_file_location("select_lacuna_field", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _dense_concept(wiki: Path, shard: str, slug: str, n: int = 9) -> None:
    """Create the concept page + ``n`` referencing source pages so density >= MIN_DENSITY."""
    (wiki / "concepts" / shard).mkdir(parents=True, exist_ok=True)
    (wiki / "concepts" / shard / f"{slug}.md").write_text(
        f"---\ntype: concept\n---\n# {slug}\n", encoding="utf-8")
    sdir = wiki / "sources"
    sdir.mkdir(exist_ok=True)
    for i in range(n):
        (sdir / f"{slug}-src-{i}.md").write_text(
            f"---\ntype: source\n---\nRefs [[concepts/{shard}/{slug}]].\n", encoding="utf-8")


def _base_vault(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    _dense_concept(wiki, "s", "supply-chain-compromise")
    _dense_concept(wiki, "z", "zero-day-exploitation")
    _dense_concept(wiki, "r", "ransomware-as-a-service")
    (wiki / "lacuna").mkdir(parents=True)
    return wiki


def test_slug_of_handles_sharded_bracketed_and_plain():
    m = _load(Path("/nonexistent"))   # module load only; WIKI unused here
    assert m._slug_of("concepts/s/supply-chain-compromise") == "supply-chain-compromise"
    assert m._slug_of("[[concepts/z/zero-day-exploitation]]") == "zero-day-exploitation"
    assert m._slug_of("[[concepts/ransomware-as-a-service|RaaS]]") == "ransomware-as-a-service"
    assert m._slug_of("plain-slug") == "plain-slug"


def test_field_mapped_excludes_mapped_field_not_secondary_links(tmp_path):
    wiki = _base_vault(tmp_path)
    # a page that MAPPED supply-chain-compromise (bare field_mapped path) but merely CITES zero-day
    (wiki / "lacuna" / "credential-lineage-gap.md").write_text(
        "---\n"
        "type: lacuna\n"
        "field_mapped: concepts/s/supply-chain-compromise\n"
        "updated: 2026-07-07\n"
        "---\n"
        "The force resembles [[concepts/z/zero-day-exploitation]] dynamics.\n",
        encoding="utf-8")
    m = _load(tmp_path)
    covered = m._recently_analyzed()
    assert "supply-chain-compromise" in covered      # the mapped field IS retired (the fix)
    assert "zero-day-exploitation" not in covered     # a secondary citation is NOT over-excluded

    refs, _ = m._clusters()
    eligible = {s for s, p in refs.items()
                if len(p) >= m.MIN_DENSITY and s not in covered and m._has_concept_page(s)}
    assert "supply-chain-compromise" not in eligible  # no longer clogs the batch
    assert {"zero-day-exploitation", "ransomware-as-a-service"} <= eligible


def test_legacy_page_without_field_mapped_falls_back_to_links(tmp_path):
    wiki = _base_vault(tmp_path)
    (wiki / "lacuna" / "old-gap.md").write_text(
        "---\ntype: lacuna\nupdated: 2026-07-07\n---\n"
        "Maps [[concepts/r/ransomware-as-a-service]].\n",
        encoding="utf-8")
    m = _load(tmp_path)
    assert "ransomware-as-a-service" in m._recently_analyzed()


def test_undated_page_uses_mtime_and_window_elapses(tmp_path):
    wiki = _base_vault(tmp_path)
    p = wiki / "lacuna" / "undated-gap.md"
    p.write_text(
        "---\ntype: lacuna\nfield_mapped: concepts/s/supply-chain-compromise\n---\nbody\n",
        encoding="utf-8")
    m = _load(tmp_path)
    # fresh file (mtime ~ now) -> within the window -> excluded
    assert "supply-chain-compromise" in m._recently_analyzed()
    # backdate the file well past REANALYZE_DAYS -> field re-opens
    old = __import__("time").time() - (m.REANALYZE_DAYS + 30) * 86400
    os.utime(p, (old, old))
    assert "supply-chain-compromise" not in m._recently_analyzed()
