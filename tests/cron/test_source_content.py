"""Evidence acquisition must not be inferred from prose length or a writer's self-label."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("source_content_tested", ROOT / "scripts/cron/source_content.py")
content = importlib.util.module_from_spec(spec)
spec.loader.exec_module(content)

CAPTURE = """---
source_channel: feed
capture_object: objects/ab/report.html
capture_revision: revisions/ab/report.json
content_hash: abc
---
# Official notice

## Captured content
The trial has been withdrawn.
"""


def _raw(vault, text=CAPTURE, name="notice.md"):
    path = vault / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path.relative_to(vault).as_posix()


def test_short_complete_notice_is_captured_without_being_appraised(tmp_path):
    ref = _raw(tmp_path)
    assert content.source_state(tmp_path, {"raw": ref}, "The trial has been withdrawn.") == "captured"
    fm, body = content.split(CAPTURE)
    assert content.raw_state(fm, body) == "captured"
    assert content.raw_state(fm, "## Captured content\n") == "discovery"
    assert content.raw_state({}, "long " * 1000) == "unknown"


def test_snippet_cannot_self_promote_or_pass_on_length(tmp_path):
    ref = _raw(tmp_path, json.dumps({"payload": {"summary": "boilerplate " * 1000,
                                               "content_state": "captured"}}), "record.json")
    assert content.source_state(tmp_path, {"raw": ref, "content_state": "captured"},
                                "boilerplate " * 1000) == "discovery"


def test_capture_backlink_does_not_upgrade_a_page_still_containing_only_a_snippet(tmp_path):
    capture = _raw(tmp_path)
    discovery = _raw(tmp_path, '{"payload": {"summary": "The trial"}}', "hit.json")
    assert content.source_state(tmp_path, {"raw": [discovery, capture]}, "The trial") == "discovery"
    assert content.source_state(tmp_path, {"raw": capture}, "The trial") == "unknown"
    assert content.source_state(tmp_path, {"raw": [discovery, capture]},
                                "The trial has been withdrawn.") == "captured"


def test_failed_capture_does_not_fall_back_to_summary_evidence(tmp_path):
    ref = _raw(tmp_path, CAPTURE.replace("source_channel: feed", "capture_dead_letter: failed.json"))
    assert content.source_state(tmp_path, {"raw": ref}, "The trial has been withdrawn.") == "capture-failed"


def test_missing_or_unsafe_raw_never_attests_to_capture(tmp_path):
    external = tmp_path / "external.md"
    external.write_text(CAPTURE)
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/link.md").symlink_to(external)
    for raw in [None, 12, [], [12], "elsewhere.md", "raw/missing.md", "raw/../external.md", "raw/link.md"]:
        assert content.source_state(tmp_path, {"raw": raw}, "The trial has been withdrawn.") == "unknown"


@pytest.mark.parametrize("text,name", [
    ("---\nbroken: [\n---\nbody", "bad.md"),
    ("{bad", "bad.json"),
    ('{"payload": []}', "list.json"),
    ("[]", "array.json"),
    ("plain body", "plain.md"),
    ("---\n- list\n---\nbody", "list.md"),
])
def test_unrecognized_raw_is_unknown(tmp_path, text, name):
    ref = _raw(tmp_path, text, name)
    assert content.source_state(tmp_path, {"raw": ref}, text) == "unknown"


def test_oversized_raw_is_bounded_and_unknown(tmp_path, monkeypatch):
    ref = _raw(tmp_path)
    monkeypatch.setattr(content, "MAX_RAW_BYTES", 8)
    assert content.source_state(tmp_path, {"raw": ref}, CAPTURE) == "unknown"


def test_neutralized_wikilinks_and_whitespace_preserve_capture(tmp_path):
    ref = _raw(tmp_path, CAPTURE.replace("withdrawn.", "[[withdrawn]]."))
    assert content.source_state(tmp_path, {"raw": ref},
                                "The trial\n has been [\u200b[withdrawn]\u200b].") == "captured"


def test_feed_summary_and_unmarked_caller_label_are_not_captured(tmp_path):
    ref = _raw(tmp_path, "---\nsource_channel: feed\ncontent_state: captured\n---\nsummary")
    assert content.source_state(tmp_path, {"raw": ref}, "summary") == "discovery"
    assert content.source_state(tmp_path, {"content_state": "captured"}, CAPTURE) == "unknown"


def test_size_boundary_retains_complete_capture_but_rejects_one_more_byte(tmp_path, monkeypatch):
    ref = _raw(tmp_path)
    exact_bytes = len(CAPTURE.encode())
    monkeypatch.setattr(content, "MAX_RAW_BYTES", exact_bytes)
    assert content.source_state(tmp_path, {"raw": ref}, "The trial has been withdrawn.") == "captured"
    (tmp_path / ref).write_text(CAPTURE + " ")
    assert content.source_state(tmp_path, {"raw": ref}, "The trial has been withdrawn.") == "unknown"


def test_invalid_observation_does_not_hide_later_usable_capture(tmp_path, monkeypatch):
    good = _raw(tmp_path)
    large = _raw(tmp_path, CAPTURE * 2, "large.md")
    malformed = _raw(tmp_path, "{", "invalid.json")
    monkeypatch.setattr(content, "MAX_RAW_BYTES", len(CAPTURE.encode()))
    for bad in [None, "outside", large, malformed, "raw/missing.md"]:
        assert content.source_state(tmp_path, {"raw": [bad, good]},
                                    "The trial has been withdrawn.") == "captured"


def test_capture_text_companion_is_not_parsed_as_json(tmp_path):
    ref = _raw(tmp_path, CAPTURE, "notice.html.txt")
    assert content.source_state(tmp_path, {"raw": ref}, "The trial has been withdrawn.") == "captured"
