"""Regression: select_raw_batch's companion-skip.

A raw binary/markup file (.pdf/.html/.htm/.docx/.pptx) is skipped from the ingest
digest when its `<name>.txt` companion exists (the host extractors wrote it, so
the agent should ingest the clean text, not the binary). Without a companion it
stays queued. Driven black-box via the digest the script prints to stdout.
"""
import subprocess
import sys
import json
import hashlib
import re
import importlib.util
import os
import runpy
import time
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "cron" / "select_raw_batch.py"

COMPANION_EXTS = (".pdf", ".html", ".htm", ".docx", ".pptx", ".xlsx", ".rtf", ".doc")


def _run(vault: Path, batch: str = "100") -> str:
    env = {"WIKI_PATH": str(vault), "BATCH_SIZE": batch, "MIN_YEAR": "2025",
           "OKENGINE_SELECTION_MANIFEST": str(vault / "raw" / ".selection.json"),
           "OKENGINE_LANE_ID": "lane-raw", "OKENGINE_CONTRACT_DIGEST": "sha256:contract",
           "PATH": __import__("os").environ.get("PATH", "")}
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.returncode == 0, f"select_raw_batch failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout


def _mod():
    """Import the selector as a module for unit-level checks.

    The tests above drive it as a subprocess, which is right for the digest contract but cannot
    reach a pure helper. Registered in sys.modules before exec so any dataclass in the module
    can resolve its defining namespace.
    """
    import sys as _sys
    spec = importlib.util.spec_from_file_location("select_raw_batch_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
    manifest = json.loads((tmp_path / "raw" / ".selection.json").read_text())
    assert len(manifest["selected"]) == 2 and manifest["input_digest"].startswith("sha256:")
    assert manifest["lane_id"] == "lane-raw" and manifest["contract_digest"] == "sha256:contract"
    assert "Verified receipt identity" in out


def test_missing_runner_receipt_identity_fails_before_selection(tmp_path):
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "raw").mkdir()
    env = {
        "WIKI_PATH": str(tmp_path),
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    run = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60
    )
    assert run.returncode == 1
    assert "receipt contract unavailable" in run.stderr
    assert not (tmp_path / "raw" / ".selection.json").exists()


def test_missing_raw_directory_is_clean_no_work_and_clears_stale_selection(tmp_path):
    """An initialized but empty pack must not wake an agent or replay an old batch."""
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    manifest = tmp_path / "selection.json"
    manifest.write_text('{"selected":["raw/stale.txt"]}\n')
    env = {
        "WIKI_PATH": str(tmp_path),
        "OKENGINE_SELECTION_MANIFEST": str(manifest),
        "OKENGINE_LANE_ID": "lane-raw",
        "OKENGINE_CONTRACT_DIGEST": "sha256:contract",
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    run = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"wakeAgent": False}
    assert not manifest.exists()


def test_hundred_item_mixed_receipt_has_zero_undisposed_and_drains_terminals(tmp_path):
    """The 100-item operating batch gets a keyed runner-owned template, and a
    mixed valid receipt durably removes no-write terminal decisions (#423)."""
    sources = tmp_path / "wiki" / "sources"
    sources.mkdir(parents=True)
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    for index in range(100):
        (raw / f"item-{index:03}.txt").write_text(f"raw content {index}")

    out = _run(tmp_path, batch="100")
    block = re.search(r"```okengine-receipt\n(\{.*?\})\n```", out, re.S)
    assert block, "selector did not emit the canonical receipt template"
    template = json.loads(block.group(1))
    keys = [item["key"] for item in template["items"]]
    assert len(keys) == len(set(keys)) == 100
    assert all(item["disposition"] is None for item in template["items"])

    receipt_items = []
    for index, key in enumerate(keys):
        if index < 25:
            target = sources / f"compiled-{index:03}.md"
            target.write_text(
                "---\ntype: source\n"
                f"raw: {key}\npublisher: Example\npublished: 2026-01-01\n---\n\n"
                + "Grounded source body. " * 8
            )
            receipt_items.append({
                "key": key,
                "disposition": "accepted",
                "writes": [{
                    "path": f"sources/{target.name}",
                    "sha256": "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
                }],
                "reason": None,
            })
        elif index < 50:
            receipt_items.append({
                "key": key, "disposition": "duplicate", "writes": [],
                "reason": "same reporting as an existing canonical source",
            })
        elif index < 75:
            receipt_items.append({
                "key": key, "disposition": "skipped", "writes": [],
                "reason": "not cybersecurity reporting",
            })
        else:
            receipt_items.append({
                "key": key, "disposition": "deferred", "writes": [],
                "reason": "input requires another extraction pass",
            })

    receipt_value = {**template, "items": receipt_items}
    receipts_path = REPO / "patches" / "cron-plus" / "run_receipts.py"
    spec = importlib.util.spec_from_file_location("raw_receipts", receipts_path)
    receipts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(receipts)
    manifest_path = tmp_path / "raw" / ".selection.json"
    parsed, result = receipts.verify_response(
        {
            "id": "lane-raw",
            "output_contract_digest": "sha256:contract",
            "selection_manifest": str(manifest_path),
        },
        "```okengine-receipt\n" + json.dumps(receipt_value) + "\n```",
        tmp_path / "wiki",
    )
    assert parsed == receipt_value
    assert result["valid"] and result["counts"]["undisposed"] == 0
    assert result["counts"]["accepted"] == result["counts"]["duplicate"] == 25
    assert result["counts"]["skipped"] == result["counts"]["deferred"] == 25

    next_out = _run(tmp_path, batch="100")
    for key in keys[:75]:
        assert f"`{key}`" not in next_out
    for key in keys[75:]:
        assert f"`{key}`" in next_out


def test_only_valid_compiled_source_consumes_raw_input(tmp_path):
    sources = tmp_path / "wiki" / "sources"
    sources.mkdir(parents=True)
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    for name in ("gold-feather-qilin-agenda-ransomware", "valid"):
        (raw / f"{name}.txt").write_text("captured input")
    (sources / "gold-feather-qilin-agenda-ransomware.md").write_text(
        "---\ntype: source\nraw: raw/2026/gold-feather-qilin-agenda-ransomware.txt\n"
        "publisher: Example\npublished: 2026-01-01\n---\n")
    (sources / "valid.md").write_text(
        "---\ntype: source\nraw: raw/2026/valid.txt\npublisher: Example\npublished: 2026-01-01\n---\n\n"
        "# Valid\n\n" + "Grounded extracted source content. " * 5)
    out = _run(tmp_path)
    assert "`raw/2026/gold-feather-qilin-agenda-ransomware.txt`" in out
    assert "`raw/2026/valid.txt`" not in out


def test_declared_quarantine_is_excluded_before_selection(tmp_path, capsys):
    """Run IN-PROCESS, not through `subprocess`. A subprocess executes the real script but is
    invisible to coverage, so the exclusion path reads as untested and nothing would flag it if a
    later edit dropped it."""
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    raw = tmp_path / "raw" / "operator-feed"
    raw.mkdir(parents=True)
    (raw / "ambiguous.md").write_text(
        "---\ntype: source\ningest_disposition: quarantine\n"
        "---\n\nIrrelevant candidate body.\n")
    (raw / "matched.md").write_text(
        "---\ntype: source\n---\n\nRelevant candidate body.\n")

    assert _main_direct(_load_direct(tmp_path)) == 0
    out = capsys.readouterr().out

    assert "Quarantined raw files excluded:** 1" in out
    assert "`raw/operator-feed/ambiguous.md`" not in out
    assert "`raw/operator-feed/matched.md`" in out
    manifest = json.loads((tmp_path / "raw" / ".selection.json").read_text())
    assert "raw/operator-feed/ambiguous.md" not in manifest["selected"]


def test_repeated_invalid_item_stays_retryable_and_large_input_is_partial(tmp_path, capsys):
    """In-process for the same reason as the quarantine test: a subprocess run is real but
    unmeasurable, and the retryable banner is exactly the kind of operator-facing line that can be
    dropped by an edit without any test going red."""
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    target = raw / "large.txt"
    target.write_text("x" * 210000)
    (tmp_path / "raw" / ".batch-offered.json").write_text(
        json.dumps({"raw/2026/large.txt": 99}))
    assert _main_direct(_load_direct(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Retryable" in out and "`raw/2026/large.txt`" in out
    assert "extraction=partial" in out and "deferred remainder" in out


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


def test_batch_size_env_is_namespaced_with_a_legacy_fallback():
    """okengine#486: `BATCH_SIZE` is far too generic for a gateway's shared environment.

    Every cron lane in the container sees the same env, so an unqualified `BATCH_SIZE` can be
    reached by an operator tuning some other lane, or collide with a future script. Its siblings
    (RAW_STUCK_AFTER, RAW_ACCEPT_MIN_CHARS, RAW_MAX_CONTEXT_BYTES) are already prefixed.

    The legacy name must keep working: this knob bounds the context a run accumulates, and a
    deployment silently reverting to an unbounded default on upgrade would reintroduce the
    "Cannot compress further" failure it was set to avoid.
    """
    src = (Path(__file__).resolve().parents[2] / "scripts" / "cron" / "select_raw_batch.py").read_text()
    assert 'os.environ.get("RAW_BATCH_SIZE")' in src, "namespaced name not read"
    assert 'os.environ.get("BATCH_SIZE")' in src, "legacy name must still be honoured"
    assert 'or "1"' in src
    assert 'RAW_MAX_CONTEXT_BYTES", "16000"' in src
    # The operator-facing text must name the new variable, or it teaches the deprecated one.
    assert "`RAW_BATCH_SIZE=" in src and "`BATCH_SIZE=" not in src
    # A deprecation notice must never reach stdout — this script's stdout IS the model prompt.
    for line in src.splitlines():
        if "deprecated" in line and "print(" in line:
            assert "stderr" in line or "file=sys.stderr" in src, "notice must go to stderr"


def _load_direct(vault: Path, **env):
    values = {
        "WIKI_PATH": str(vault),
        "RAW_BATCH_SIZE": "20",
        "MIN_YEAR": "2025",
        "OKENGINE_SELECTION_MANIFEST": str(vault / "raw" / ".selection.json"),
        "OKENGINE_LANE_ID": "lane-raw",
        "OKENGINE_CONTRACT_DIGEST": "sha256:contract",
        **{key: str(value) for key, value in env.items()},
    }
    with patch.dict(os.environ, values, clear=False):
        spec = importlib.util.spec_from_file_location(
            f"select_raw_batch_edges_{id(vault)}_{time.time_ns()}", SCRIPT
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _main_direct(module):
    with patch.dict(os.environ, {
        "OKENGINE_LANE_ID": "lane-raw",
        "OKENGINE_CONTRACT_DIGEST": "sha256:contract",
    }):
        return module.main()


def test_helpers_cover_corrupt_state_and_frontmatter_shapes(tmp_path, monkeypatch, capsys):
    raw = tmp_path / "raw"
    raw.mkdir()
    index = raw / ".year_index.json"
    index.write_text("[]")
    m = _load_direct(tmp_path)
    assert m._load_year_index() == {}
    index.write_text('{"good":"2024","bad":[]}')
    assert m._load_year_index() == {"good": 2024}
    index.write_text("{")
    assert m._load_year_index() == {}

    m.OFFER_MANIFEST.write_text("[]")
    assert m._load_offered() == {}
    m.OFFER_MANIFEST.write_text("{")
    assert m._load_offered() == {}
    original_write = Path.write_text
    monkeypatch.setattr(
        Path, "write_text",
        lambda path, *a, **k: (
            (_ for _ in ()).throw(OSError()) if path == m.OFFER_MANIFEST
            else original_write(path, *a, **k)
        ),
    )
    m._save_offered({"x": 1})
    monkeypatch.setattr(Path, "write_text", original_write)

    m.COMPLETION_LEDGER.write_text("[]")
    assert m._load_terminal_completions() == set()
    m.COMPLETION_LEDGER.write_text("{")
    assert m._load_terminal_completions() == set()
    m.COMPLETION_LEDGER.write_text(json.dumps({
        "raw/a.txt": {"disposition": "duplicate"},
        "raw/b.txt": {"disposition": "accepted"},
        "raw/c.txt": "bad",
        "raw/d.txt": {"disposition": "skipped"},
        "raw/e.txt": {"disposition": "failed"},
    }))
    assert m._load_terminal_completions() == {"raw/a.txt", "raw/d.txt"}

    good_body = "\nBody " * 30
    assert m.extract_processed_paths("plain") == set()
    assert m.extract_processed_paths("---\n[\n---" + good_body) == set()
    assert m.extract_processed_paths("---\n- x\n---" + good_body) == set()
    assert m.extract_processed_paths("---\ntype: note\nraw: x\n---" + good_body) == set()
    assert m.extract_processed_paths("---\ntype: source\nraw: x\n---\nshort") == set()
    assert m.extract_processed_paths(
        "---\ntype: source\nraw: x\npublisher: ''\npublished: 2026\n---" + good_body
    ) == set()
    assert m.extract_processed_paths(
        "---\ntype: source\nraw: x\npublisher: p\npublished: 2026\n---" + good_body
    ) == {"x"}
    assert m.extract_processed_paths(
        "---\ntype: source\nraw: [x, 3, ' y ']\npublisher: p\npublished: 2026\n---" + good_body
    ) == {"x", "y"}
    assert m.extract_processed_paths(
        "---\ntype: source\nraw: 3\npublisher: p\npublished: 2026\n---" + good_body
    ) == set()

    stale = m.SELECTION_MANIFEST
    stale.write_text("{}")
    original_unlink = Path.unlink
    monkeypatch.setattr(
        Path, "unlink",
        lambda path, **k: (
            (_ for _ in ()).throw(OSError("locked")) if path == stale
            else original_unlink(path, **k)
        ),
    )
    m._clear_selection_manifest()
    assert "cannot clear stale selection" in capsys.readouterr().err


def test_year_and_tier_derivation_all_routes(tmp_path):
    m = _load_direct(
        tmp_path,
        BULK_IMPORT_MTIMES="123,not-a-number",
        BULK_IMPORT_SENTINEL_YEAR="1999",
        CURATED_DIR="clips",
        BULK_DIR="archive",
    )
    m._YEAR_INDEX = {"raw/indexed.txt": 2022}
    assert m.derive_year("raw/indexed.txt", 0) == 2022
    assert m.derive_year("raw/report-2024-final.txt", 0) == 2024
    assert m.derive_year("raw/plain.txt", 123) == 1999
    assert m.derive_year("raw/plain.txt", 1_700_000_000) == 2023
    assert m.path_tier("clips/a.txt") == 0
    assert m.path_tier("raw/clips/a.txt") == 0
    assert m.path_tier("raw/x/clips/a.txt") == 0
    assert m.path_tier("raw/archive/a.txt") == 2
    assert m.path_tier("raw/other/a.txt") == 1


def test_main_error_empty_hygiene_control_and_binary_routes(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing"
    m = _load_direct(missing)
    assert _main_direct(m) == 1
    assert "vault not found" in capsys.readouterr().out

    vault = tmp_path / "vault"
    (vault / "wiki" / "sources").mkdir(parents=True)
    raw = vault / "raw"
    raw.mkdir()
    # Every scan exclusion: directory, unsupported, hidden, companioned, orphan,
    # literal-backslash path, and an old item deferred by the year gate.
    (raw / "dir").mkdir()
    (raw / "unsupported.exe").write_text("x")
    (raw / ".hidden.txt").write_text("x")
    (raw / "with.pdf").write_text("binary")
    (raw / "with.pdf.txt").write_text("extract")
    orphan = raw / "orphan"
    orphan.mkdir()
    (orphan / "link.md").write_text("[Link](x)")
    (raw / "bad\\ path.txt").write_text("x")
    old = raw / "report-2020.txt"
    old.write_text("old")
    m = _load_direct(vault, MIN_YEAR="2025")
    assert _main_direct(m) == 0
    out = capsys.readouterr()
    assert "Path Hygiene" in out.out and "deferred, year<2025" in out.out

    # Curated old content bypasses the year gate; controlled target leaves only
    # the binary, exercising the no-embedded-evidence disposition guidance.
    curated = vault / "clippings"
    curated.mkdir()
    (curated / "report-2020.pdf").write_bytes(b"%PDF")
    m = _load_direct(
        vault,
        RAW_BACKFILL_TARGET="clippings/report-2020.pdf",
        RAW_MAX_CONTEXT_BYTES="1",
    )
    assert _main_direct(m) == 0
    out = capsys.readouterr().out
    assert "Embedded extraction is unavailable" in out
    assert "tier 0 (curated" in out

    # A target that does not exist produces a clean no-work result.
    m.CONTROLLED_TARGET = "raw/not-there.txt"
    assert _main_direct(m) == 0
    assert '"wakeAgent": false' in capsys.readouterr().out

    # Selection-manifest write failure is fatal and explicit.
    m.CONTROLLED_TARGET = "clippings/report-2020.pdf"
    original_mkdir = Path.mkdir
    monkeypatch.setattr(
        Path, "mkdir",
        lambda path, *a, **k: (
            (_ for _ in ()).throw(OSError("readonly"))
            if path == m.SELECTION_MANIFEST.parent else original_mkdir(path, *a, **k)
        ),
    )
    assert _main_direct(m) == 1
    assert "cannot write selection manifest" in capsys.readouterr().err


def test_direct_contract_raw_missing_no_work_and_remaining_branches(tmp_path, capsys):
    # Legacy configuration emits its deprecation warning during import.
    m = _load_direct(tmp_path, RAW_BATCH_SIZE="", BATCH_SIZE="2")
    assert "BATCH_SIZE is deprecated" in capsys.readouterr().err

    with patch.dict(os.environ, {
        "OKENGINE_LANE_ID": "",
        "OKENGINE_CONTRACT_DIGEST": "",
    }):
        assert m.main() == 1
    assert "missing OKENGINE_LANE_ID, OKENGINE_CONTRACT_DIGEST" in capsys.readouterr().err

    tmp_path.mkdir(exist_ok=True)
    assert _main_direct(m) == 0
    assert capsys.readouterr().out.strip() == '{"wakeAgent": false}'

    old_vault = tmp_path / "old-vault"
    (old_vault / "raw").mkdir(parents=True)
    (old_vault / "raw" / "report-2020.txt").write_text("old")
    m = _load_direct(old_vault, MIN_YEAR="2025")
    assert _main_direct(m) == 0
    assert "pre-2025 files are deferred" in capsys.readouterr().out

    busy = tmp_path / "busy"
    raw = busy / "raw" / "2026"
    raw.mkdir(parents=True)
    for index in range(3):
        (raw / f"{index}.txt").write_text("x")
    m = _load_direct(busy, RAW_BATCH_SIZE="1")
    assert _main_direct(m) == 0
    assert "Remaining after this batch" in capsys.readouterr().out


def test_many_bogus_paths_and_scan_stat_race(tmp_path, monkeypatch, capsys):
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    for index in range(11):
        (raw / f"bad\\ {index}.txt").write_text("x")
    vanished = raw / "vanished.txt"
    vanished.write_text("x")
    m = _load_direct(tmp_path)
    original_stat = Path.stat
    target_stats = 0

    def flaky_stat(path, *args, **kwargs):
        nonlocal target_stats
        if path == vanished:
            target_stats += 1
            if target_stats > 1:
                raise OSError("vanished")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert _main_direct(m) == 0
    out = capsys.readouterr().out
    assert "1 more not shown" in out


def test_raw_scan_ignores_item_removed_before_metadata_read(tmp_path, monkeypatch, capsys):
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    vanished = raw / "vanished.txt"
    vanished.write_text("x")
    m = _load_direct(tmp_path)
    original_stat = Path.stat
    original_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: True if path == vanished else original_is_file(path))
    monkeypatch.setattr(Path, "stat", lambda path, *a, **k: (
        (_ for _ in ()).throw(OSError("vanished")) if path == vanished
        else original_stat(path, *a, **k)))
    assert _main_direct(m) == 0
    assert "0 ingestable files remaining" in capsys.readouterr().out


def test_source_scan_read_race_and_selected_stat_race(tmp_path, monkeypatch, capsys):
    sources = tmp_path / "wiki" / "sources"
    sources.mkdir(parents=True)
    source = sources / "gone.md"
    source.write_text("x")
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    target = raw / "target.txt"
    target.write_text("evidence")
    m = _load_direct(tmp_path)
    original_read = Path.read_text
    original_stat = Path.stat

    def flaky_read(path, *args, **kwargs):
        if path == source:
            raise OSError("vanished")
        return original_read(path, *args, **kwargs)

    def flaky_stat(path, *args, **kwargs):
        # Fire on the BRANCH, not on a call count. This previously triggered on the third stat()
        # of the target; a refactor reduced the lane to two, so the race stopped happening and the
        # test asserted deferred-language against a perfectly normal run — red for weeks while the
        # behavior it guards was never exercised (okengine#565). The selection manifest is written
        # before the evidence loop, so its existence IS "after selection" and cannot rot the same
        # way: any refactor that keeps the contract keeps this trigger.
        if path == target and m.SELECTION_MANIFEST.exists():
            raise OSError("vanished after selection")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert _main_direct(m) == 0
    out = capsys.readouterr().out
    # a source the lane could not read is surfaced as UNKNOWN, never silently dropped
    assert "target.txt" in out and "evidence unavailable" in out and "disposition it as deferred" in out


def test_script_entrypoint_defer_and_run(tmp_path, monkeypatch):
    cron_dir = str(SCRIPT.parent)
    monkeypatch.syspath_prepend(cron_dir)
    import offpeak

    monkeypatch.setattr(offpeak, "offpeak_defer", lambda: True)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0

    monkeypatch.setattr(offpeak, "offpeak_defer", lambda: False)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path / "absent"))
    monkeypatch.setenv("OKENGINE_LANE_ID", "lane")
    monkeypatch.setenv("OKENGINE_CONTRACT_DIGEST", "sha256:x")
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 1


def _raw_vault(root: Path, name: str):
    """An independent vault with one ingestable raw item (each run drains its own backlog)."""
    v = root / name
    (v / "wiki" / "sources").mkdir(parents=True)
    (v / "raw" / "2026").mkdir(parents=True)
    (v / "raw" / "2026" / "target.txt").write_text("evidence")
    return v, v / "raw" / "2026" / "target.txt"


def test_the_race_fixture_actually_reaches_the_deferred_branch(tmp_path, monkeypatch, capsys):
    """A race test that stops racing is worse than no test: it stays GREEN while the behavior it
    guards goes unexercised, or RED for reasons unrelated to that behavior (okengine#565 — the
    trigger counted stat() calls, a refactor reduced them from three to two, and the assertion then
    compared deferred-language against a perfectly normal run).

    This pins the MECHANISM: armed, the lane emits the deferred disposition and the trigger records
    that it fired; disarmed, the same input yields ordinary evidence. If a refactor defuses the
    trigger, THIS fails loudly instead of the fixture going quietly inert.

    Separate vaults per run: the lane drains its backlog, so a second run over the same vault has
    nothing left to race on."""
    control_vault, _ = _raw_vault(tmp_path, "control")
    control = _load_direct(control_vault)
    assert _main_direct(control) == 0
    clean = capsys.readouterr().out
    assert "extraction=complete" in clean and "evidence unavailable" not in clean

    race_vault, target = _raw_vault(tmp_path, "race")
    m = _load_direct(race_vault)
    original_stat = Path.stat
    armed = {"fired": False}

    def flaky_stat(path, *args, **kwargs):
        if path == target and m.SELECTION_MANIFEST.exists():
            armed["fired"] = True
            raise OSError("vanished after selection")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert _main_direct(m) == 0
    out = capsys.readouterr().out
    assert armed["fired"], "the race never triggered — the fixture has defused"
    assert "evidence unavailable" in out and "disposition it as deferred" in out


def test_quarantine_reason_reads_only_well_formed_declarations(tmp_path):
    """A quarantine declaration is an operator's explicit exclusion, so it is honoured ONLY when it
    is unambiguously there. Unreadable, unparsable and non-mapping frontmatter all mean "no
    declaration" — inferring one would silently drop evidence the operator never excluded, and
    inferring the opposite on a real declaration would admit what they did."""
    m = _load_direct(tmp_path)
    raw = tmp_path / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    (raw / "plain.txt").write_text("not markdown")
    assert m.quarantine_reason(raw / "plain.txt") is None, "only .md carries frontmatter"

    (raw / "a-directory.md").mkdir()
    assert m.quarantine_reason(raw / "a-directory.md") is None, "unreadable is not a declaration"

    (raw / "nofm.md").write_text("no frontmatter at all\n")
    assert m.quarantine_reason(raw / "nofm.md") is None

    (raw / "badyaml.md").write_text("---\ningest_disposition: [unclosed\n---\n\nbody\n")
    assert m.quarantine_reason(raw / "badyaml.md") is None, "unparsable is not a declaration"

    (raw / "listfm.md").write_text("---\n- a\n- b\n---\n\nbody\n")
    assert m.quarantine_reason(raw / "listfm.md") is None, "a sequence declares no fields"

    (raw / "nokey.md").write_text("---\ntype: source\n---\n\nbody\n")
    assert m.quarantine_reason(raw / "nokey.md") is None

    (raw / "other.md").write_text("---\ningest_disposition: ingest\n---\n\nbody\n")
    assert m.quarantine_reason(raw / "other.md") is None, "a different disposition is not quarantine"

    for value in ("quarantine", "  Quarantined  "):
        (raw / "q.md").write_text(f"---\ningest_disposition: '{value}'\n---\n\nbody\n")
        assert m.quarantine_reason(raw / "q.md") == "declared-quarantine", value


def test_a_fully_quarantined_raw_tree_is_complete_and_says_why(tmp_path, capsys):
    """Nothing left to ingest — but "0 remaining" alone would read as a drained backfill when the
    inputs were in fact all held back. The exclusion count is what tells those two apart."""
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    for i in range(3):
        (raw / f"held{i}.md").write_text(
            "---\ntype: source\ningest_disposition: quarantine\n---\n\nbody\n")

    assert _main_direct(_load_direct(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "# Backfill complete" in out
    assert "3 quarantined raw files were excluded from admission." in out
    assert '{"wakeAgent": false}' in out


def test_source_kind_is_carried_and_never_left_for_the_model_to_invent(tmp_path):
    """A field the ingest already knows must not be re-derived downstream.

    `source_kind` is stamped by the ingest lanes (feed_fetch reads it off the feed item, the
    API importers stamp their own) and is common_optional, so it is schema-legal on every
    type — but it was missing from PROVENANCE_KEYS, so the compile agent had to invent a
    value nobody handed it.

    The invention was survivable only while the model guessed plausibly. When the compile
    model changed on 2026-07-26 the guess collapsed to a constant: from 2026-07-28 every
    source page on a live vault was written `source_kind: report` — across FOUR unrelated
    publishers on TWO ingest channels, ~30/day — while the raw files beside them still said
    `cyber-news`, `advisory`, `commentary`, `threat-report`. Downstream, a lane selecting the
    news firehose on `source_kind == "news"` matched nothing from that day and the actor
    board it feeds froze, reporting success throughout.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("select_raw_batch", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert "source_kind" in m.PROVENANCE_KEYS, (
        "source_kind must be CARRIED from the raw page; leaving it out makes the compile "
        "model the classifier, and a model that changes takes the whole vocabulary with it"
    )

    # and the emitted prompt must actually name it, or the carry is only theoretical
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    (raw / "a.txt").write_text("content")
    assert "`source_kind`" in _run(tmp_path)


# ── okengine#748: revision siblings collapse to the newest before selection ───────────────

def _entries(*specs):
    """(rel, mtime) -> the selector's 4-tuple shape."""
    return [(rel, rel, 2026, mtime) for rel, mtime in specs]


def test_revision_group_strips_the_revision_token():
    m = _mod()
    assert m.revision_group("raw/ai/2026-01-01-a-revision-deadbeef.md") == "raw/ai/2026-01-01-a.md"
    assert m.revision_group(
        "raw/ai/2026-01-01-a-revision-deadbeef-0badf00d.md") == "raw/ai/2026-01-01-a.md"


def test_a_path_without_a_revision_token_is_its_own_group():
    m = _mod()
    rel = "raw/ai/2026-01-01-plain.md"
    assert m.revision_group(rel) == rel


def test_a_hex_suffix_that_is_not_a_revision_is_not_stripped():
    """NEGATIVE: over-collapsing would merge genuinely distinct articles into one."""
    m = _mod()
    rel = "raw/ai/2026-01-01-report-deadbeef.md"
    assert m.revision_group(rel) == rel


def test_collapse_keeps_only_the_newest_sibling():
    m = _mod()
    kept, collapsed = m.collapse_revisions(_entries(
        ("raw/ai/a-revision-00000001.md", 100.0),
        ("raw/ai/a-revision-00000002.md", 300.0),
        ("raw/ai/a-revision-00000003.md", 200.0)))
    assert collapsed == 2
    assert [k[0] for k in kept] == ["raw/ai/a-revision-00000002.md"]


def test_distinct_articles_are_all_kept():
    m = _mod()
    kept, collapsed = m.collapse_revisions(_entries(
        ("raw/ai/a-revision-00000001.md", 100.0),
        ("raw/ai/b-revision-00000002.md", 100.0)))
    assert collapsed == 0 and len(kept) == 2


def test_collapse_is_deterministic_when_mtimes_tie():
    """The digest is a contract: identical vault state must always select identically."""
    m = _mod()
    spec = _entries(("raw/ai/a-revision-00000002.md", 5.0),
                    ("raw/ai/a-revision-00000001.md", 5.0))
    first = m.collapse_revisions(spec)[0]
    second = m.collapse_revisions(list(reversed(spec)))[0]
    assert first == second


def test_collapse_can_be_switched_off(monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "COLLAPSE_REVISIONS", False)
    kept, collapsed = m.collapse_revisions(_entries(
        ("raw/ai/a-revision-00000001.md", 100.0),
        ("raw/ai/a-revision-00000002.md", 300.0)))
    assert collapsed == 0 and len(kept) == 2


def test_the_churn_ratio_is_collapsed_away():
    """Scale check against the shape of the real incident: 639 copies of one article plus a
    handful of distinct ones must select as the distinct count, not the file count."""
    m = _mod()
    churn = [(f"raw/ai/hot-revision-{i:08x}.md", float(i)) for i in range(639)]
    distinct = [(f"raw/ai/other-{i}.md", 1.0) for i in range(5)]
    kept, collapsed = m.collapse_revisions(_entries(*churn, *distinct))
    assert len(kept) == 6 and collapsed == 638


# ── the capture store is storage, never an ingest candidate ───────────────────────────────

def _vault_with_capture_store(tmp_path):
    (tmp_path / "raw" / "ai").mkdir(parents=True)
    (tmp_path / "raw" / "ai" / "2026-01-01-article.md").write_text(
        "---\ntitle: Article\n---\nbody\n", encoding="utf-8")
    for sub, name, body in (("objects/ab", "blob.html", "<html>raw bytes</html>"),
                            ("revisions/cd", "rev.json", '{"content_hash": "x"}'),
                            ("dead-letter", "dl.json", '{"error": "timeout"}')):
        d = tmp_path / "raw" / "captures" / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_capture_store_files_are_never_selected(tmp_path):
    """THE REGRESSION. Response blobs and provenance records are the storage layer behind a
    raw item, not items themselves. Offering them spends an agent invocation per blob — 98% of
    one live vault's queue — and can only ever produce a source page about a storage
    internal."""
    out = _run(_vault_with_capture_store(tmp_path))
    assert "raw/captures/" not in out
    assert "raw/ai/2026-01-01-article.md" in out


def test_the_exclusion_is_reported_rather_than_silent(tmp_path):
    """A skip nobody can see is indistinguishable from a scan that missed the files."""
    out = _run(_vault_with_capture_store(tmp_path))
    assert "Capture-store files excluded" in out


def test_a_directory_merely_named_like_the_capture_store_elsewhere_is_still_ingested(tmp_path):
    """NEGATIVE: the exclusion is anchored at raw/<CAPTURE_DIR>/, not a substring match, so a
    legitimately-named content directory is not swallowed."""
    (tmp_path / "raw" / "ai" / "captures").mkdir(parents=True)
    (tmp_path / "raw" / "ai" / "captures" / "2026-01-01-real-article.md").write_text(
        "---\ntitle: Real\n---\nbody\n", encoding="utf-8")
    out = _run(tmp_path)
    assert "2026-01-01-real-article.md" in out


def test_a_vault_that_is_only_capture_store_selects_nothing_and_says_so(tmp_path):
    (tmp_path / "raw" / "captures" / "objects" / "ab").mkdir(parents=True)
    (tmp_path / "raw" / "captures" / "objects" / "ab" / "blob.html").write_text(
        "<html></html>", encoding="utf-8")
    out = _run(tmp_path)
    assert "raw/captures/" not in out


# ── a later revision of an already-compiled article must not be re-offered ────────────────

def _compiled(vault, raw_rel, slug="already-compiled"):
    """A source page recording the raw path it was compiled from."""
    d = vault / "wiki" / "sources" / "2026" / "04"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(
        "---\ntype: source\ntitle: Already compiled\n"
        "published: '2026-04-20T00:00:00+00:00'\n"
        # `publisher` is in ACCEPT_REQUIRED_FIELDS — a page missing it is not counted as
        # processed at all, which would make these tests pass for the wrong reason.
        "publisher: Example\n"
        f"url: https://example.test/a\nraw: {raw_rel}\n---\n\n"
        + ("Body text that is long enough to count as a real compiled page. " * 8),
        encoding="utf-8")


def _raw_item(vault, name):
    d = vault / "raw" / "ai"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("---\ntitle: Article\n---\nbody\n", encoding="utf-8")


def test_a_new_revision_of_a_compiled_article_is_not_re_offered(tmp_path):
    """THE REGRESSION. The slug is invented per run, so re-offering an already-compiled article
    produces a SECOND page under a different name. One live article reached four pages this
    way, two of them months apart — okengine#54 reached through the revision token."""
    _compiled(tmp_path, "raw/ai/2026-04-20-import-ai-454.md")
    _raw_item(tmp_path, "2026-04-20-import-ai-454-revision-deadbeef.md")
    out = _run(tmp_path)
    assert "revision-deadbeef" not in out


def test_an_absolute_raw_path_recorded_by_an_older_lane_still_matches(tmp_path):
    """Older pages recorded `/opt/vault/raw/...`. A group that does not match across both
    spellings re-admits an article that is already compiled."""
    _compiled(tmp_path, "/opt/vault/raw/ai/2026-04-20-import-ai-454.md")
    _raw_item(tmp_path, "2026-04-20-import-ai-454-revision-deadbeef.md")
    out = _run(tmp_path)
    assert "revision-deadbeef" not in out


def test_a_genuinely_new_article_is_still_offered(tmp_path):
    """NEGATIVE: the guard must not swallow unrelated work."""
    _compiled(tmp_path, "raw/ai/2026-04-20-import-ai-454.md")
    _raw_item(tmp_path, "2026-04-21-something-entirely-different.md")
    out = _run(tmp_path)
    assert "2026-04-21-something-entirely-different.md" in out


def test_revision_group_strips_an_absolute_prefix():
    m = _mod()
    assert (m.revision_group("/opt/vault/raw/ai/a-revision-deadbeef.md")
            == m.revision_group("raw/ai/a.md") == "raw/ai/a.md")


# ── the capture-store and revision-collapse counters, measured IN-PROCESS ────────────────────
#
# The behaviours below are already covered end-to-end by the `_run` tests above, which drive the
# real CLI in a subprocess. That is the better integration evidence and it stays. But coverage
# cannot see a subprocess, so those paths read as unexecuted and the 100% floor fails on code
# that is genuinely tested. These drive the same contract through `_load_direct` so the
# measurement sees what the subprocess tests already prove. Same reason the file's other
# `_load_direct` tests exist -- this is not a second assertion of the same thing for its own sake.


def test_a_capture_store_file_is_excluded_from_selection_and_counted(tmp_path, capsys):
    """The exclusion must happen on the RELATIVE path under raw/, and must increment the counter
    rather than skipping silently -- an invisible skip is indistinguishable from a scan that
    never found the files."""
    vault = tmp_path / "vault"
    article = vault / "raw" / "ai" / "2026-01-01-article.md"
    article.parent.mkdir(parents=True)
    article.write_text("---\ntitle: Article\n---\nbody\n", encoding="utf-8")
    # A .md under the capture store: same extension as a real item, so it is a genuine
    # candidate and can only be excluded by the capture-store rule itself.
    blob = vault / "raw" / "captures" / "objects" / "ab" / "2026-01-01-blob.md"
    blob.parent.mkdir(parents=True)
    blob.write_text("---\ntitle: Blob\n---\nraw bytes\n", encoding="utf-8")

    module = _load_direct(vault)
    assert _main_direct(module) == 0
    out = capsys.readouterr().out
    assert "raw/captures/" not in out, "a capture-store file was offered for ingest"
    assert "raw/ai/2026-01-01-article.md" in out, "the real article must still be selected"
    assert "Capture-store files excluded (storage, not content):** 1" in out


def test_collapsed_revision_siblings_are_reported(tmp_path, capsys):
    """Visibility, not a warning: a large count here means the capture layer is minting
    revisions for unchanged articles (okengine#748), which is worth chasing upstream even
    though the selection itself is correct."""
    vault = tmp_path / "vault"
    raw = vault / "raw" / "ai"
    raw.mkdir(parents=True)
    for token in ("00000001", "00000002"):
        page = raw / f"2026-01-01-article-revision-{token}.md"
        page.write_text(f"---\ntitle: Article\n---\n{token}\n", encoding="utf-8")

    module = _load_direct(vault)
    assert _main_direct(module) == 0
    out = capsys.readouterr().out
    assert "Revision siblings collapsed to newest:** 1" in out
