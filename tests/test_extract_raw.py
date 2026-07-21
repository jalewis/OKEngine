"""Smoke tests for the stage-1 host-side extraction glue (extract-raw.sh +
install-extract-cron.sh). Pure-infra, so these assert wiring and idempotency
rather than extraction quality (that lives in test_extract_html.py)."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "scripts" / "extract-raw.sh"
INSTALLER = REPO / "scripts" / "install-extract-cron.sh"


def _bash_n(script: Path):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, f"{script.name} syntax error:\n{r.stderr}"


def test_scripts_exist_and_parse():
    assert WRAPPER.is_file() and INSTALLER.is_file()
    _bash_n(WRAPPER)
    _bash_n(INSTALLER)


def test_wrapper_wires_shipped_extractors():
    txt = WRAPPER.read_text()
    # Runs the two shipped extractors...
    assert "extract-pdfs.sh" in txt
    assert "extract-html.py" in txt
    # ...and the DOCX/PPTX one only conditionally (it ships separately, #5).
    assert "extract-docs.py" in txt
    assert 'if [ -f "$REPO/scripts/extract-docs.py" ]' in txt
    # Single-flight guard so long first passes never overlap.
    assert "flock" in txt
    assert 'LOCK_KEY="$(printf \'%s\' "$RAW" | cksum' in txt
    assert "okengine-extract-raw-${LOCK_KEY}.lock" in txt
    assert "EXTRACT_LOCK_FILE" in txt


def test_installer_is_idempotent_guarded():
    txt = INSTALLER.read_text()
    # Greps the current crontab for the wrapper before appending — re-runnable.
    assert 'grep -qF "scripts/extract-raw.sh"' in txt
    assert "crontab -" in txt


def test_wrapper_exits_zero_on_empty_raw(tmp_path=None):
    """An empty (but existing) raw root: each extractor finds nothing / or its
    host tool is absent — the wrapper logs a warning and still exits 0."""
    if shutil.which("bash") is None or shutil.which("flock") is None:
        return  # environment lacks the host tools the wrapper needs; skip
    raw = Path(tempfile.mkdtemp())
    try:
        env = dict(os.environ, WIKI_PATH=str(raw.parent))
        # pass the raw root explicitly as arg 1
        r = subprocess.run(["bash", str(WRAPPER), str(raw)],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, f"wrapper failed on empty raw:\n{r.stdout}\n{r.stderr}"
        assert "extract-raw start" in r.stdout and "extract-raw done" in r.stdout
    finally:
        shutil.rmtree(raw, ignore_errors=True)


def test_wrapper_errors_on_missing_raw_root():
    r = subprocess.run(["bash", str(WRAPPER), "/nonexistent/raw/root/xyz"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert "raw root not found" in r.stderr
