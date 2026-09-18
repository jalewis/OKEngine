"""adjudication_eval (okengine#551) — the harness that decides which model can be the janitor.

The contract under test: a transport failure is never scored as a model result, a warrant is only
credited when it appears VERBATIM in the source the model named, and a run against an empty key or
an unreachable provider refuses rather than reporting a vacuous zero.

These properties are why the harness's own numbers can be trusted — a bug here would silently
mis-rank the models it exists to compare, so it is tested at the same bar as a write-path lane.
"""
import importlib.util
import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "scripts" / "eval" / "adjudication_eval.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="adjudication_eval absent")


@pytest.fixture()
def m():
    spec = importlib.util.spec_from_file_location("adjudication_eval", MOD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_chat(m, monkeypatch, content=None, raises=None, capture=None):
    """Replace the sanctioned call path. Tests exercise THIS harness, not llm_lib — llm_lib has
    its own gates and vendored-copy invariants."""
    def fake(prompt, **kw):
        if capture is not None:
            capture.append({"prompt": prompt, **kw})
        if raises is not None:
            raise raises
        return content
    monkeypatch.setattr(m.llm_lib, "chat", fake)


def _vault(tmp_path: Path, pages: dict) -> Path:
    for rel, body in pages.items():
        p = tmp_path / "wiki" / f"{rel}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def _case(vault, **kw):
    base = {"id": "c1", "vault": str(vault), "evidence": ["sources/s1"],
            "claim": "a claim", "gold": "supported"}
    base.update(kw)
    return base


# --- reply parsing --------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    ('{"verdict": "supported"}', "supported"),
    ('```json\n{"verdict": "supported"}\n```', "supported"),
    ('Sure! Here is the answer:\n{"verdict": "supported"}', "supported"),
    ('<think>hmm, let me consider</think>{"verdict": "supported"}', "supported"),
    ('{"a": {"b": 1}, "verdict": "supported"}', "supported"),
])
def test_parse_recovers_the_object_from_realistic_replies(m, raw, expect):
    """Local models fence, preface and leak <think> blocks. Scoring those as unparseable would
    grade presentation rather than reading."""
    assert m._parse(raw)["verdict"] == expect


@pytest.mark.parametrize("raw", ["", None, "no json here at all", "{not valid json}", "[1, 2, 3]"])
def test_parse_returns_none_when_there_is_no_object(m, raw):
    assert m._parse(raw) is None


def test_parse_skips_a_broken_object_and_finds_a_later_valid_one(m):
    assert m._parse('{oops} then {"verdict": "contradicted"}')["verdict"] == "contradicted"


def test_parse_survives_a_reply_truncated_mid_object(m):
    """A model that hits max_tokens emits an opening brace that never closes. The brace scanner
    must run off the end and give up, not spin — distinct from a closed-but-malformed object,
    which exits by a different path."""
    assert m._parse('{"verdict": "supported", "warrant": "it was cut off here') is None


# --- transport vs model failure -------------------------------------------

def test_backoff_is_delegated_to_llm_lib_not_reimplemented(m, tmp_path, monkeypatch):
    """llm_lib already owns transient-transport retry AND the reasoning-off policy. A second
    retry ladder here would double the backoff and, worse, tempt a raw call that bypasses the
    policy — which is exactly what tests/test_llm_call_discipline.py forbids."""
    seen = []
    _stub_chat(m, monkeypatch, content='{"verdict": "supported"}', capture=seen)
    v = _vault(tmp_path, {"sources/s1": "b"})
    m.run_case(_case(v), "http://x/v1", "mdl", None, 42)
    assert seen[0]["timeout"] == 42 and seen[0]["retries"] >= 1
    assert seen[0]["temperature"] == 0.0 and seen[0]["max_tokens"] == 900


def test_a_failure_surviving_llm_lib_retries_is_no_result_not_a_model_failure(m, tmp_path, monkeypatch):
    """The bug this was written for: a provider 503 scored as a model failure reported a capable
    model as unreliable. A busy hour is not a capability finding."""
    v = _vault(tmp_path, {"sources/s1": "body"})
    _stub_chat(m, monkeypatch, raises=m.llm_lib.LLMError("call failed after 3 attempt(s): 503"))
    r = m.run_case(_case(v), "http://x/v1", "mdl", None, 5)
    assert r["transport"] is True and r["parsed"] is False
    s = m.score([r])
    assert s["transport_failures"] == 1 and s["graded"] == 0
    assert s["accuracy"] is None and s["parse_rate"] is None
    assert s["model_failures"] == 0          # a transport failure is NOT a model failure


def test_a_reply_that_arrives_and_does_not_parse_is_a_model_failure(m, tmp_path, monkeypatch):
    v = _vault(tmp_path, {"sources/s1": "body"})
    _stub_chat(m, monkeypatch, content="I refuse to use JSON")
    r = m.run_case(_case(v), "http://x/v1", "mdl", None, 5)
    assert r["parsed"] is False and not r.get("transport")
    assert r["raw_head"].startswith("I refuse")
    assert m.score([r])["model_failures"] == 1


# --- the warrant check ----------------------------------------------------

def test_a_verbatim_warrant_is_credited(m, tmp_path, monkeypatch):
    v = _vault(tmp_path, {"sources/s1": "Beginning April 2023, the conduct started."})
    _stub_chat(m, monkeypatch, content=json.dumps(
        {"verdict": "supported", "source_id": "sources/s1", "warrant": "Beginning April 2023"}))
    assert m.run_case(_case(v), "http://x/v1", "mdl", None, 5)["quote_ok"] is True


def test_an_invented_warrant_fails_even_when_the_verdict_is_right(m, tmp_path, monkeypatch):
    """The single most important property: a fabricated quote is worse than a wrong verdict,
    because it is built to survive review."""
    v = _vault(tmp_path, {"sources/s1": "Beginning April 2023, the conduct started."})
    _stub_chat(m, monkeypatch, content=json.dumps(
        {"verdict": "supported", "source_id": "sources/s1", "warrant": "the court found beyond doubt"}))
    r = m.run_case(_case(v), "http://x/v1", "mdl", None, 5)
    assert r["correct"] is True and r["quote_ok"] is False


def test_warrant_matching_ignores_whitespace_shape(m, tmp_path, monkeypatch):
    v = _vault(tmp_path, {"sources/s1": "the  conduct\nstarted   here"})
    _stub_chat(m, monkeypatch, content=json.dumps(
        {"verdict": "supported", "source_id": "sources/s1", "warrant": "The conduct started here"}))
    assert m.run_case(_case(v), "http://x/v1", "mdl", None, 5)["quote_ok"] is True


def test_a_wrong_source_id_falls_back_to_every_cited_page(m, tmp_path, monkeypatch):
    """Naming the wrong source is a citation slip, not a fabrication — don't score it as one."""
    v = _vault(tmp_path, {"sources/s1": "nothing here", "sources/s2": "the decisive sentence"})
    _stub_chat(m, monkeypatch, content=json.dumps(
        {"verdict": "supported", "source_id": "sources/NOPE", "warrant": "the decisive sentence"}))
    r = m.run_case(_case(v, evidence=["sources/s1", "sources/s2"]), "http://x/v1", "mdl", None, 5)
    assert r["quote_ok"] is True


@pytest.mark.parametrize("warrant", [None, "", "   ", 42])
def test_no_warrant_scores_none_not_false(m, tmp_path, monkeypatch, warrant):
    """A null warrant on an `unsupported` verdict is honest. Scoring it False would punish the
    correct behaviour and make quote-exists unreadable."""
    v = _vault(tmp_path, {"sources/s1": "body"})
    _stub_chat(m, monkeypatch, content=json.dumps(
        {"verdict": "unsupported", "source_id": None, "warrant": warrant}))
    r = m.run_case(_case(v, gold="unsupported"), "http://x/v1", "mdl", None, 5)
    assert r["quote_ok"] is None and r["correct"] is True
    assert m.score([r])["quote_exists"] is None


# --- evidence assembly ----------------------------------------------------

def test_a_case_with_no_evidence_still_runs(m, tmp_path, monkeypatch):
    """The Iossifov case: zero source pages anywhere. The model must be asked, and must be able to
    answer `unsupported` — not skipped."""
    seen = []
    _stub_chat(m, monkeypatch, content='{"verdict": "unsupported", "warrant": null}', capture=seen)
    r = m.run_case(_case(_vault(tmp_path, {}), evidence=[], gold="unsupported"),
                   "http://x/v1", "mdl", None, 5)
    assert r["correct"] is True
    assert "no evidence pages are cited" in seen[0]["prompt"]


def test_a_missing_evidence_page_reads_as_empty_not_a_crash(m, tmp_path):
    assert m._read_page(_vault(tmp_path, {}), "sources/absent") == ""


def test_the_api_key_is_passed_through_to_llm_lib(m, tmp_path, monkeypatch):
    seen = []
    _stub_chat(m, monkeypatch, content='{"verdict": "supported"}', capture=seen)
    v = _vault(tmp_path, {"sources/s1": "b"})
    m.run_case(_case(v), "http://x/v1", "mdl", "sk-abc", 5)
    m.run_case(_case(v), "http://x/v1", "mdl", None, 5)
    assert [c["api_key"] for c in seen] == ["sk-abc", None]


# --- scoring --------------------------------------------------------------

def test_bias_delta_exposes_a_rubber_stamping_model(m):
    """A model that answers `supported` to a claim and to its mutated twin is agreeing, not
    reading. This is the number that catches it."""
    rs = [{"parsed": True, "correct": True, "quote_ok": None, "secs": 1, "mutated": False},
          {"parsed": True, "correct": True, "quote_ok": None, "secs": 1, "mutated": False},
          {"parsed": True, "correct": False, "quote_ok": None, "secs": 1, "mutated": True},
          {"parsed": True, "correct": False, "quote_ok": None, "secs": 1, "mutated": True}]
    s = m.score(rs)
    assert s["acc_true"] == 1.0 and s["acc_mutated"] == 0.0 and s["bias_delta"] == 1.0


def test_bias_delta_is_none_without_both_halves(m):
    rs = [{"parsed": True, "correct": True, "quote_ok": None, "secs": 1, "mutated": False}]
    s = m.score(rs)
    assert s["bias_delta"] is None and s["acc_mutated"] is None


def test_score_of_nothing_is_none_not_zero(m):
    """An empty run must not report 0.0 accuracy — that reads as a measured failure."""
    s = m.score([])
    assert s["cases"] == 0 and s["accuracy"] is None and s["parse_rate"] is None
    assert s["median_secs"] is None and s["quote_exists"] is None


def test_quote_rate_counts_only_claimed_warrants(m):
    rs = [{"parsed": True, "correct": True, "quote_ok": True, "secs": 1},
          {"parsed": True, "correct": True, "quote_ok": False, "secs": 2},
          {"parsed": True, "correct": True, "quote_ok": None, "secs": 3}]
    s = m.score(rs)
    assert s["warrants_claimed"] == 2 and s["quote_exists"] == 0.5
    assert s["median_secs"] == 2


# --- CLI ------------------------------------------------------------------

def _cases_file(tmp_path, vault, n=2):
    f = tmp_path / "cases.yaml"
    f.write_text(yaml.safe_dump({"cases": [
        dict(_case(vault), id=f"c{i}") for i in range(n)]}), encoding="utf-8")
    return f


# --- the reasoning_effort sweep -------------------------------------------

def test_reasoning_effort_is_passed_through_per_level(m, tmp_path, monkeypatch):
    """llm_lib defaults every caller to `none`, which is right for bulk classification and
    measurably wrong for adjudication (deepseek-flash lost 10 accuracy points with reasoning
    off). Benchmarking a model with its reasoning silently amputated grades the wrong thing, so
    the level is swept and reported, never assumed."""
    seen = []
    _stub_chat(m, monkeypatch, content='{"verdict": "supported"}', capture=seen)
    v = _vault(tmp_path, {"sources/s1": "b"})
    assert m.main(["--cases", str(_cases_file(tmp_path, v, n=1)), "--model", "mdl",
                   "--base-url", "http://x/v1", "--reasoning-effort", "none,high"]) == 0
    assert [c["reasoning_effort"] for c in seen] == ["none", "high"]


def test_omit_sends_no_reasoning_key_at_all(m):
    """`omit` and `none` are different requests: one disables reasoning, the other says nothing —
    which is what a provider that rejects the key needs. Collapsing them would hide a real result."""
    assert m._effort("omit") is None
    assert m._effort("none") == "none"
    assert m._effort("high") == "high"


def test_sweep_reports_every_level_and_writes_them_all(m, tmp_path, monkeypatch, capsys):
    v = _vault(tmp_path, {"sources/s1": "the decisive sentence"})
    _stub_chat(m, monkeypatch, content=json.dumps(
        {"verdict": "supported", "source_id": "sources/s1", "warrant": "the decisive sentence"}))
    out = tmp_path / "sweep.json"
    assert m.main(["--cases", str(_cases_file(tmp_path, v, n=1)), "--model", "mdl",
                   "--base-url", "http://x/v1", "--reasoning-effort", "none,high,omit",
                   "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "reasoning_effort=none" in printed and "reasoning_effort=high" in printed
    data = json.loads(out.read_text())["sweep"]
    assert set(data) == {"none", "high", "omit"}
    assert all(d["score"]["accuracy"] == 1.0 for d in data.values())


def test_cli_runs_scores_and_writes_the_result_file(m, tmp_path, monkeypatch, capsys):
    v = _vault(tmp_path, {"sources/s1": "the decisive sentence"})
    _stub_chat(m, monkeypatch, content=json.dumps(
        {"verdict": "supported", "source_id": "sources/s1", "warrant": "the decisive sentence"}))
    out = tmp_path / "r.json"
    assert m.main(["--cases", str(_cases_file(tmp_path, v)), "--model", "mdl",
                   "--base-url", "http://x/v1", "--out", str(out)]) == 0
    assert "accuracy" in capsys.readouterr().out
    assert json.loads(out.read_text())["sweep"]["none"]["score"]["accuracy"] == 1.0


def test_cli_limit_truncates_the_run(m, tmp_path, monkeypatch, capsys):
    v = _vault(tmp_path, {"sources/s1": "b"})
    _stub_chat(m, monkeypatch, content='{"verdict": "supported"}')
    assert m.main(["--cases", str(_cases_file(tmp_path, v, n=5)), "--model", "mdl",
                   "--base-url", "http://x/v1", "--limit", "2"]) == 0
    assert "[ 2/2]" in capsys.readouterr().out


def test_cli_refuses_an_empty_api_key_rather_than_reporting_a_vacuous_zero(m, tmp_path, monkeypatch, capsys):
    """Running with no key would 401 every case and report the model as unusable. That is the
    missing-key trap: an absent prerequisite must fail loudly, never grade as a result."""
    v = _vault(tmp_path, {"sources/s1": "b"})
    monkeypatch.delenv("SOME_KEY", raising=False)
    assert m.main(["--cases", str(_cases_file(tmp_path, v)), "--model", "mdl",
                   "--base-url", "http://x/v1", "--api-key-env", "SOME_KEY"]) == 1
    assert "refusing to run" in capsys.readouterr().err


def test_cli_uses_the_key_when_the_env_var_is_set(m, tmp_path, monkeypatch):
    v = _vault(tmp_path, {"sources/s1": "b"})
    monkeypatch.setenv("SOME_KEY", "sk-live")
    seen = []
    _stub_chat(m, monkeypatch, content='{"verdict": "supported"}', capture=seen)
    assert m.main(["--cases", str(_cases_file(tmp_path, v, n=1)), "--model", "mdl",
                   "--base-url", "http://x/v1", "--api-key-env", "SOME_KEY"]) == 0
    assert seen[0]["api_key"] == "sk-live"
