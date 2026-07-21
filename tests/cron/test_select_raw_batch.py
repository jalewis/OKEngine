"""Regression: select_raw_batch's companion-skip.

A raw binary/markup file (.pdf/.html/.htm/.docx/.pptx) is skipped from the ingest
digest when its `<name>.txt` companion exists (the host extractors wrote it, so
the agent should ingest the clean text, not the binary). Without a companion it
stays queued. Driven black-box via the digest the script prints to stdout.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "cron" / "select_raw_batch.py"

COMPANION_EXTS = (".pdf", ".html", ".htm", ".docx", ".pptx", ".xlsx", ".rtf", ".doc")


def _run(vault: Path, batch: str = "100") -> str:
    env = {"WIKI_PATH": str(vault), "BATCH_SIZE": batch, "MIN_YEAR": "2025",
           "PATH": __import__("os").environ.get("PATH", "")}
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.returncode == 0, f"select_raw_batch failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout


def test_batch_is_bounded_with_drain_guidance(tmp_path):
    """BATCH_SIZE bounds the digest and the output makes the bound + drain model +
    sources-vs-other-lanes expectation explicit (#23)."""
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    for i in range(5):
        (raw / f"f{i}.txt").write_text("content")
    out = _run(tmp_path, batch="2")
    assert "2 of 5 ingestable" in out and "BATCH_SIZE=2" in out
    assert "Remaining after this batch:** 3" in out
    assert "source** pages only" in out and "self-draining" in out
    assert out.count("derived_year=") == 2          # exactly BATCH_SIZE files listed


def _build_vault(tmp_path: Path) -> Path:
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    for ext in COMPANION_EXTS:
        tag = ext.lstrip(".")
        # with companion -> the binary must be skipped, the .txt kept
        (raw / f"withcomp_{tag}{ext}").write_text("binary-ish")
        (raw / f"withcomp_{tag}{ext}.txt").write_text("extracted text companion body")
        # without companion -> the binary stays queued
        (raw / f"bare_{tag}{ext}").write_text("binary-ish")
    return tmp_path


def test_companion_present_skips_binary_keeps_txt(tmp_path):
    out = _run(_build_vault(tmp_path))
    for ext in COMPANION_EXTS:
        tag = ext.lstrip(".")
        # The raw binary with a companion is skipped — its exact backtick-quoted
        # path must be absent (guard against the `.txt` line matching as a substring).
        assert f"`raw/2026/withcomp_{tag}{ext}`" not in out, f"{ext}: companioned binary not skipped"
        # ...but its extracted .txt companion is a normal ingestable leaf.
        assert f"`raw/2026/withcomp_{tag}{ext}.txt`" in out, f"{ext}: companion .txt missing from digest"
        # The un-companioned binary stays queued.
        assert f"`raw/2026/bare_{tag}{ext}`" in out, f"{ext}: un-companioned binary should be queued"


def test_all_companion_exts_are_covered():
    """The skip-list in select_raw_batch.py must cover every ext this test
    exercises, and each must be an ingestable leaf — a guard so adding a format to
    the extractor without the selector (or vice-versa) is caught."""
    src = SCRIPT.read_text()
    for ext in COMPANION_EXTS:
        assert f'"{ext}"' in src, f"{ext} missing from select_raw_batch"


def test_provenance_carry_contract_prompt_and_base_schema_agree(tmp_path):
    """okengine#194: the compile agent silently dropped ingest-provenance frontmatter
    (source_feed & co.) because nothing told it to carry them AND the base schema didn't
    know them (unknown-field flags discourage extras). Multi-surface contract: the wake
    prompt must instruct the carry using PROVENANCE_KEYS, and every one of those keys must
    be schema-legal in the base's common_optional — a key added to one surface but not the
    other fails HERE, not on a live vault."""
    import importlib.util
    import yaml

    spec = importlib.util.spec_from_file_location("select_raw_batch", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    keys = m.PROVENANCE_KEYS
    assert keys, "PROVENANCE_KEYS must be non-empty"

    # surface 1: the emitted prompt carries the instruction + every key by name
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    (raw / "a.txt").write_text("content")
    out = _run(tmp_path)
    assert "CARRY the raw page's ingest-provenance" in out
    for k in keys:
        assert f"`{k}`" in out, f"prompt does not name provenance key {k!r}"

    # Source semantics are not interchangeable: repositories and discovery
    # mechanisms must not be promoted into the article publisher (#278).
    assert "`publisher` is the organization/site" in out
    assert "`source_feed` is the repository or feed" in out
    assert "Never put a retrieval repository/feed" in out
    assert "never write placeholder strings such as `undefined`" in out
    assert "do not substitute" in out

    # surface 2: the base schema lists the SAME keys (schema-legal on every type)
    base = yaml.safe_load((REPO / "config" / "base-schema.yaml").read_text())
    missing = [k for k in keys if k not in (base.get("common_optional") or [])]
    assert not missing, f"base-schema common_optional is missing provenance key(s): {missing}"
