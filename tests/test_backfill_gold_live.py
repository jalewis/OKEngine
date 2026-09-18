import importlib.util
import json
import runpy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "backfill_gold_live", REPO / "scripts" / "backfill_gold_live.py")
LIVE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = LIVE
SPEC.loader.exec_module(LIVE)


def test_responses_parser_extracts_output_text():
    assert LIVE._text({"output": [{"content": [{
        "type": "output_text", "text": '{"action":"abstain"}'}]}]}) == (
        '{"action":"abstain"}')


def test_json_parser_recovers_final_object_from_extra_text():
    assert LIVE._json_object(
        'analysis {"draft":true}\\n{"action":"reject","reason":"unsafe"}'
    )["action"] == "reject"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_expected_atomic_facts_can_appear_inside_grounded_phrases(monkeypatch):
    payload = {"output": [{"content": [{
        "type": "output_text",
        "text": json.dumps({
            "action": "write",
            "facts": ["CVE-2026-1000 affects Acme Edge"],
            "fields": {},
            "mutations": [],
            "reason": "grounded",
        }),
    }]}]}
    monkeypatch.setattr(
        LIVE.urllib.request, "urlopen",
        lambda *_args, **_kwargs: _Response(payload))
    case = {
        "id": "raw-positive",
        "family": "raw-source",
        "class": "positive",
        "input": "CVE-2026-1000 affects Acme Edge",
        "expected": {
            "action": "write",
            "facts": ["CVE-2026-1000", "Acme Edge"],
        },
    }
    assert LIVE._request(case, "http://local/v1", "qwen", 5)[
        "expected_action_match"] is True


def test_response_parsers_reject_missing_payloads_and_accept_plain_object():
    with pytest.raises(ValueError, match="no output_text"):
        LIVE._text({"output": [{"content": [{"type": "other"}]}]})
    with pytest.raises(ValueError, match="no JSON object"):
        LIVE._json_object("nothing")
    assert LIVE._json_object('{"draft": true}') == {"draft": True}
    assert LIVE._json_object('{bad} [] {"one": 1} {"two": 2}') == {"one": 1}
    assert LIVE._json_object('[] {"action": "write"}') == {"action": "write"}


def test_request_detects_schema_destructive_and_fabrication(monkeypatch):
    payload = {"output": [{"content": [{
        "type": "output_text",
        "text": json.dumps({
            "action": "write", "facts": ["confidence", "invented"],
            "fields": ["invalid"], "mutations": "invalid", "reason": 3,
        }),
    }]}]}
    monkeypatch.setattr(LIVE.urllib.request, "urlopen",
                        lambda *_a, **_k: _Response(payload))
    case = {
        "id": "x", "family": "entity", "class": "adversarial", "input": "x",
        "expected": {
            "action": "reject", "fields": {"category": "x"},
            "facts": ["required"], "forbidden_mutations": ["confidence"],
            "forbidden_facts": ["invented"],
        },
    }
    result = LIVE._request(case, "http://local/v1/", "qwen", 1)
    assert not result["schema_valid"] and result["destructive_violation"]
    assert result["fabrication_violation"] and not result["expected_action_match"]
    assert result["endpoint"] == "http://local/v1/responses"


def test_request_result_retries_then_returns_structured_error(monkeypatch):
    attempts = 0

    def flaky(*_a):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("once")
        return {"ok": True}

    monkeypatch.setattr(LIVE, "_request", flaky)
    monkeypatch.setattr(LIVE.time, "sleep", lambda *_a: None)
    case = {"id": "x", "family": "entity", "class": "positive"}
    assert LIVE._request_result(case, "http://x/", "m", 1) == {"ok": True}
    monkeypatch.setattr(LIVE, "_request",
                        lambda *_a: (_ for _ in ()).throw(ValueError("always")))
    result = LIVE._request_result(case, "http://x/", "m", 1)
    assert result["error"] == "ValueError: always"
    assert result["endpoint"] == "http://x/responses"


def test_main_writes_jsonl_and_entrypoint(monkeypatch, tmp_path, capsys):
    corpus = tmp_path / "corpus.yaml"
    corpus.write_text("cases:\n- {id: x, family: entity, class: positive, input: x}\n")
    output = tmp_path / "results.jsonl"
    monkeypatch.setattr(
        LIVE, "_request_result",
        lambda case, endpoint, model, timeout: {"case_id": case["id"], "model": model},
    )
    monkeypatch.setattr(sys, "argv", [
        "backfill_gold_live", "--corpus", str(corpus), "--output", str(output),
        "--workers", "0", "--endpoint", "http://x/",
    ])
    assert LIVE.main() == 0
    assert json.loads(output.read_text())["case_id"] == "x"
    assert json.loads(capsys.readouterr().out)["endpoint"] == "http://x/responses"

    monkeypatch.setattr(sys, "argv", [
        str(REPO / "scripts" / "backfill_gold_live.py"),
        "--corpus", str(corpus), "--output", str(output),
    ])
    monkeypatch.setattr(LIVE, "main", lambda: 7)
    # run_path loads a fresh module, so use a valid empty corpus and only assert
    # the real script boundary exits normally.
    corpus.write_text("cases: []\n")
    monkeypatch.setenv("OKENGINE_LLM_BASE_URL", "http://x")
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(REPO / "scripts" / "backfill_gold_live.py"),
                       run_name="__main__")
    assert exc.value.code == 0


def test_json_scanner_skips_valid_non_object_values():
    assert LIVE._json_object('[1, 2] then {"x": 1} then {"action": "abstain"}') == {
        "action": "abstain"}
