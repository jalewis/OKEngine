"""frontmatter_lib: the one uncapped, body-free frontmatter reader (okengine#993)."""
import importlib
import io
import sys
from pathlib import Path

import pytest

CRON = Path(__file__).resolve().parents[2] / "scripts" / "cron"
sys.path.insert(0, str(CRON))
fl = importlib.import_module("frontmatter_lib")


def _long_frontmatter(tail: str, sources: int = 400) -> str:
    lines = ["---", "type: actor", "sources:"]
    lines += [f"  - https://example.org/report/{i:05d}-a-fairly-long-report-slug" for i in range(sources)]
    return "\n".join(lines) + f"\n{tail}---\n# Body\n"


# Each case is a whole file; the streamed block must equal the regex over the whole text.
_CASES = [
    "---\ntype: actor\n---\nbody\n",
    "---\ntype: actor\n---",                       # closing delimiter at EOF without newline
    "--- \t\ntype: actor\n---  \nbody\n",           # trailing blanks on both delimiters
    "---\r\nstatus: tombstoned\r\n---\r\nbody\r\n",  # CRLF: text mode reads it as LF
    "---\n---\ntype: actor\n---\n",                # a delimiter straight after the opener is content
    "---\n\ntype: actor\n---\n",                   # a blank first envelope line
    "---\ntype: actor\n---x\nmore: 1\n---\n",       # `---x` does not close the envelope
    "---\ntype: actor\n",                          # never closed
    "---\ntype: actor",                            # never closed, no final newline
    "----\ntype: actor\n---\n",                    # not an opener
    "---",                                         # opener without a newline
    "no frontmatter\n---\ntype: actor\n---\n",
    "",
]


@pytest.mark.parametrize("text", _CASES)
def test_streamed_block_equals_the_regex_over_the_whole_text(tmp_path, text):
    page = tmp_path / "page.md"
    page.write_bytes(text.encode())
    match = fl.FRONTMATTER_RE.match(page.read_text(encoding="utf-8", errors="replace"))
    assert fl.frontmatter_block(page) == (match.group(1) if match else "")


def test_the_pattern_is_the_placement_drains_pattern():
    okf_migrate = importlib.import_module("okf_migrate")
    assert fl.FRONTMATTER_RE.pattern == okf_migrate._FM_RE.pattern
    assert fl.FRONTMATTER_RE.flags == okf_migrate._FM_RE.flags


def test_a_status_below_a_long_list_is_read(tmp_path):
    page = tmp_path / "long.md"
    page.write_text(_long_frontmatter("status: tombstoned\n"))
    assert page.read_text().index("status: tombstoned") > 16384
    fm = fl.read_frontmatter(page)
    assert fm["status"] == "tombstoned"
    assert len(fm["sources"]) == 400


def test_reading_stops_at_the_closing_delimiter(tmp_path, monkeypatch):
    page = tmp_path / "page.md"
    page.write_text("---\ntype: actor\n---\n" + "body line\n" * 1000)
    consumed = []

    class Counting(io.StringIO):
        def __next__(self):
            line = super().__next__()
            consumed.append(line)
            return line

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Counting(open(self, encoding="utf-8").read()))
    assert fl.read_frontmatter(page) == {"type": "actor"}
    assert consumed == ["type: actor\n", "---\n"], "the body was read"


@pytest.mark.parametrize("text", [
    "---\nupdated: 2026-02-30\n---\n",              # impossible date: ValueError, not YAMLError
    "---\n[unclosed: yaml\n---\n",
    "---\n- a\n- list\n---\n",
    "no envelope\n",
])
def test_unparseable_or_non_mapping_frontmatter_is_empty(tmp_path, text):
    page = tmp_path / "page.md"
    page.write_text(text)
    assert fl.read_frontmatter(page) == {}


def test_an_unreadable_path_is_empty(tmp_path):
    assert fl.frontmatter_block(tmp_path / "missing.md") == ""
    (tmp_path / "dir.md").mkdir()
    assert fl.read_frontmatter(tmp_path / "dir.md") == {}


def test_a_custom_loader_is_used(tmp_path):
    page = tmp_path / "page.md"
    page.write_text("---\ntype: actor\n---\n")
    assert fl.read_frontmatter(page, loader=lambda block: {"seen": block}) == {"seen": "type: actor\n"}
