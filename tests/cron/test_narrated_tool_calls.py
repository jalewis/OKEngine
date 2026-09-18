"""Narrated calls without positive execution evidence must never verify green — okengine#477.

When a model's tool calls are not parsed back into the API's `tool_calls` field it emits them as
prose. Nothing executes, but the run looks successful: fluent output, `finish_reason=stop`, and
often a plausible receipt that validates. Observed on qwen3-coder:30b — ~40 correct calls narrated
in a single turn, 0 executed, lane recorded complete and its selection consumed.

The danger is not the failure; it is that the failure is INDISTINGUISHABLE FROM SUCCESS downstream.
These tests also distinguish a bounded run whose terminal summary narrates calls after
the runtime has already proven that real tool turns executed.
"""
import importlib.util
import sys
from pathlib import Path

MOD = Path(__file__).resolve().parents[2] / "patches" / "cron-plus" / "run_receipts.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_receipts_probe", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


RR = _load()


def test_clean_response_is_not_flagged():
    """A normal receipt-only response must pass — no false positives."""
    resp = (
        "Reconciled 2 sources.\n\n"
        "```okengine-receipt\n"
        '{"api": 1, "items": [{"key": "a|sha256:x", "disposition": "skipped",'
        ' "reason": "no entity evidence", "writes": []}]}\n'
        "```\n"
    )
    assert RR.detect_narrated_tool_calls(resp) == []


def test_receipt_contents_are_never_treated_as_calls():
    """The canonical receipt block is excluded — its own JSON is not a tool call."""
    resp = (
        "```okengine-receipt\n"
        '{"api": 1, "items": [{"key": "k", "disposition": "accepted",'
        ' "writes": [{"path": "wiki/entities/x.md", "sha256": "sha256:1"}]}]}\n'
        "```\n"
    )
    assert RR.detect_narrated_tool_calls(resp) == []


def test_wrong_tag_shape_is_flagged():
    """The observed qwen3-coder shape: call wrapped in the INPUT tag."""
    resp = '<tools>\n{"name": "search", "arguments": {"query": "CrowdStrike"}}\n</tools>'
    errs = RR.detect_narrated_tool_calls(resp)
    assert errs and "narrates tool calls" in errs[0]


def test_orphaned_closing_tag_is_flagged():
    """Closing tag with no opening tag — the inference team's most diagnostic sample."""
    resp = '{"name": "search", "arguments": {"query": "x"}}\n</tool_call>'
    assert RR.detect_narrated_tool_calls(resp)


def test_bare_call_objects_are_flagged():
    """Untagged bare calls — the missing-`description` failure shape."""
    resp = '\n'.join('{"name": "read_file", "arguments": {"path": "/opt/vault/wiki/s%d.md"}}' % i
                     for i in range(40))
    errs = RR.detect_narrated_tool_calls(resp)
    assert errs and "40 bare call object(s)" in errs[0]


def test_qwen_xml_function_form_is_flagged():
    """Qwen3-Coder's native XML form, if it ever arrives unparsed."""
    resp = "<tool_call>\n<function=search>\n<parameter=query>\nx\n</parameter>\n</function>\n</tool_call>"
    assert RR.detect_narrated_tool_calls(resp)


def test_empty_response_is_not_flagged():
    for resp in ("", "   ", None):
        assert RR.detect_narrated_tool_calls(resp) == []


def test_narration_invalidates_an_otherwise_valid_receipt(tmp_path, monkeypatch):
    """The load-bearing case: a VALID receipt from a run that executed nothing must fail.

    Without this, a narrated run records terminal dispositions and CONSUMES its selection —
    the items are never retried and the work is silently lost.
    """
    wiki = tmp_path / "wiki"; wiki.mkdir()
    manifest = tmp_path / "sel.json"
    selected = ["src/a.md|sha256:aa"]
    sel = {"lane_id": "L1", "contract_digest": "sha256:c", "input_digest": RR.digest_items(selected),
           "selected": selected}
    manifest.write_text(__import__("json").dumps(sel))
    job = {"id": "L1", "output_contract_digest": "sha256:c", "selection_manifest": str(manifest),
           "output_contract": {"completion": "per-selected-item"}}
    receipt = {"api": 1, "lane_id": "L1", "contract_digest": "sha256:c",
               "input_digest": sel["input_digest"],
               "items": [{"key": selected[0], "disposition": "skipped",
                          "reason": "no entity evidence", "writes": []}]}
    body = "```okengine-receipt\n" + __import__("json").dumps(receipt) + "\n```\n"

    # control: receipt alone verifies
    _, clean = RR.verify_response(job, body, wiki)
    assert clean["valid"], f"control receipt should validate: {clean.get('errors')}"

    # same receipt, preceded by narrated calls -> must be rejected
    narrated = '{"name": "read_file", "arguments": {"path": "/opt/vault/wiki/src/a.md"}}\n\n' + body
    _, result = RR.verify_response(job, narrated, wiki)
    assert not result["valid"], "a run that narrated its tool calls must not verify green"
    assert result.get("narrated_tool_calls") is True
    assert any("narrates tool calls" in e for e in result["errors"])


def test_narrated_run_does_not_consume_the_selection(tmp_path):
    """Terminal items must NOT be recorded for a narrated run — otherwise work is lost."""
    wiki = tmp_path / "wiki"; wiki.mkdir()
    manifest = tmp_path / "sel.json"
    selected = ["src/b.md|sha256:bb"]
    sel = {"lane_id": "L2", "contract_digest": "sha256:d", "input_digest": RR.digest_items(selected),
           "selected": selected}
    manifest.write_text(__import__("json").dumps(sel))
    job = {"id": "L2", "output_contract_digest": "sha256:d", "selection_manifest": str(manifest),
           "output_contract": {"completion": "per-selected-item"}}
    receipt = {"api": 1, "lane_id": "L2", "contract_digest": "sha256:d",
               "input_digest": sel["input_digest"],
               "items": [{"key": selected[0], "disposition": "skipped", "reason": "none",
                          "writes": []}]}
    body = ('<tools>{"name": "search", "arguments": {"query": "x"}}</tools>\n'
            "```okengine-receipt\n" + __import__("json").dumps(receipt) + "\n```\n")
    before = manifest.read_text()
    RR.verify_response(job, body, wiki)
    assert manifest.read_text() == before, (
        "a narrated run recorded terminal items — its selection was consumed and the work lost"
    )


# --- zero executed tool calls (okengine#477, producer = patches/13-...) --------

def _lane(tmp_path, keys=("src/a.md|sha256:aa",)):
    wiki = tmp_path / "wiki"; wiki.mkdir(exist_ok=True)
    manifest = tmp_path / "sel.json"
    sel = {"lane_id": "L9", "contract_digest": "sha256:z",
           "input_digest": RR.digest_items(list(keys)), "selected": list(keys)}
    manifest.write_text(__import__("json").dumps(sel))
    job = {"id": "L9", "output_contract_digest": "sha256:z",
           "selection_manifest": str(manifest),
           "output_contract": {"completion": "per-selected-item"}}
    receipt = {"api": 1, "lane_id": "L9", "contract_digest": "sha256:z",
               "input_digest": sel["input_digest"],
               "items": [{"key": k, "disposition": "skipped", "reason": "none", "writes": []}
                         for k in keys]}
    body = "```okengine-receipt\n" + __import__("json").dumps(receipt) + "\n```\n"
    return job, sel, body, wiki, manifest


def test_unpatched_runtime_degrades_to_silence(tmp_path):
    """No count published (older image) -> must NOT fail the lane on a guess."""
    job, sel, body, wiki, _ = _lane(tmp_path)
    assert RR.detect_no_executed_tool_calls(job, sel) == []
    _, res = RR.verify_response(job, body, wiki)
    assert res["valid"], "an unpatched runtime must not start failing every lane"


def test_zero_executed_calls_against_real_work_is_rejected(tmp_path):
    """The #476 shape: clean receipt, no narration, but nothing was executed."""
    job, sel, body, wiki, manifest = _lane(tmp_path)
    job["_okengine_executed_tool_calls"] = 0
    before = manifest.read_text()
    _, res = RR.verify_response(job, body, wiki)
    assert not res["valid"]
    assert res.get("no_executed_tool_calls") is True
    assert any("0 tool calls" in e for e in res["errors"])
    assert manifest.read_text() == before, "selection must survive a run that did nothing"


def test_executed_calls_pass(tmp_path):
    job, sel, body, wiki, _ = _lane(tmp_path)
    job["_okengine_executed_tool_calls"] = 3
    _, res = RR.verify_response(job, body, wiki)
    assert res["valid"], f"a run that used tools must pass: {res.get('errors')}"


def test_narrated_terminal_summary_passes_with_positive_execution_telemetry(tmp_path):
    """Hermes may narrate prior calls at the iteration limit after doing real work."""
    job, _, body, wiki, _ = _lane(tmp_path)
    job["_okengine_executed_tool_calls"] = 6
    narrated = (
        '<tool_call>{"name": "read_file", "arguments": '
        '{"path": "/opt/vault/wiki/src/a.md"}}</tool_call>\n' + body
    )
    _, res = RR.verify_response(job, narrated, wiki)
    assert res["valid"], (
        "positive runtime telemetry must prevent a terminal summary from "
        f"misclassifying the whole run as unexecuted: {res.get('errors')}"
    )
    assert not res.get("narrated_tool_calls")


def test_empty_selection_with_zero_calls_is_fine(tmp_path):
    """Nothing selected -> doing nothing is correct, not a failure."""
    job, sel, _, _, _ = _lane(tmp_path)
    sel["selected"] = []
    job["_okengine_executed_tool_calls"] = 0
    assert RR.detect_no_executed_tool_calls(job, sel) == []


def test_malformed_count_degrades_to_silence(tmp_path):
    job, sel, _, _, _ = _lane(tmp_path)
    for bad in ("", "many", None, [], {}):
        job["_okengine_executed_tool_calls"] = bad
        assert RR.detect_no_executed_tool_calls(job, sel) == [], f"bad value {bad!r} must not fail the lane"
