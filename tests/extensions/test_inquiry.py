"""okengine.inquiry regressions (okengine#746).

Every gate here is paired with a NEGATIVE fixture: a gate is not proven by a passing case, only
by a case it actually rejects. The defects these reject are the ones that make an inquiry fail
SILENTLY — collecting nothing while looking healthy — which is the whole failure class the
extension exists to close.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[2] / "extensions/okengine.inquiry"


def _load(name: str):
    sys.path.insert(0, str(EXT))
    spec = importlib.util.spec_from_file_location(f"okengine_inquiry_{name}", EXT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lib = _load("inquiry_lib")


def _write(wiki: Path, slug: str, body: str) -> Path:
    page = wiki / lib.NS / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(body, encoding="utf-8")
    return page


VALID = """---
type: inquiry
question: Does the thing happen?
status: open
connector: test.search
terms: [alpha, beta]
---

Body.
"""


# ── term normalization ──────────────────────────────────────────────────────

def test_bare_string_terms_normalize_to_query_objects():
    terms, errors = lib.normalize_terms(["alpha", "beta"])
    assert not errors
    assert [t.query for t in terms] == ["alpha", "beta"]
    assert [t.key for t in terms] == ["alpha", "beta"]


def test_label_overrides_key_so_query_text_can_change_without_losing_history():
    terms, errors = lib.normalize_terms([{"query": "a very long query", "label": "short"}])
    assert not errors
    assert terms[0].key == "short"


def test_scalar_terms_value_is_accepted_as_a_single_term():
    terms, errors = lib.normalize_terms("just one")
    assert not errors and [t.query for t in terms] == ["just one"]


@pytest.mark.parametrize("raw,fragment", [
    (None, "required"),
    ([], "collects nothing"),
    ([{"label": "x"}], "`query` is required"),
    ([{"query": ""}], "`query` is required"),
    (["dup", "dup"], "duplicate term key"),
    ([123], "must be a string or a mapping"),
    ([{"query": "a", "params": ["not", "a", "map"]}], "params: must be a mapping"),
])
def test_malformed_terms_are_rejected(raw, fragment):
    """NEGATIVE: each of these would otherwise silently reduce what an inquiry collects."""
    _, errors = lib.normalize_terms(raw)
    assert any(fragment in e for e in errors), errors


def test_a_dropped_term_is_an_error_not_a_silent_skip():
    """The specific regression: two terms declared, one malformed. Accepting the good one and
    dropping the bad one quietly makes the inquiry collect less than its page claims."""
    terms, errors = lib.normalize_terms(["good", {"label": "no-query"}])
    assert [t.query for t in terms] == ["good"]
    assert errors, "a malformed term must be reported, never silently dropped"


# ── page loading + contract ─────────────────────────────────────────────────

def test_valid_inquiry_page_loads_clean(tmp_path):
    wiki = tmp_path / "wiki"
    _write(wiki, "does-it-happen", VALID)
    inquiries, errors = lib.load_inquiries(wiki)
    assert not errors
    assert len(inquiries) == 1
    inq = inquiries[0]
    assert inq.is_open and inq.connector == "test.search" and len(inq.terms) == 2


def test_non_inquiry_pages_in_the_namespace_are_ignored(tmp_path):
    wiki = tmp_path / "wiki"
    _write(wiki, "a-note", "---\ntype: dashboard\ntitle: x\n---\n")
    assert lib.load_inquiries(wiki) == ([], [])


def test_index_and_underscore_pages_are_skipped(tmp_path):
    wiki = tmp_path / "wiki"
    _write(wiki, "INDEX", VALID)
    _write(wiki, "_scratch", VALID)
    assert lib.load_inquiries(wiki)[0] == []


def test_missing_namespace_is_not_an_error(tmp_path):
    assert lib.load_inquiries(tmp_path / "wiki") == ([], [])


@pytest.mark.parametrize("field,value,fragment", [
    ("question", "", "question: required"),
    ("status", "whenever", "status: must be one of"),
])
def test_malformed_frontmatter_is_rejected(tmp_path, field, value, fragment):
    """NEGATIVE: the write path enforces presence; this catches an empty or out-of-enum value."""
    wiki = tmp_path / "wiki"
    _write(wiki, "bad", VALID.replace(f"{field}: Does the thing happen?", f"{field}: {value}")
           .replace(f"{field}: open", f"{field}: {value}"))
    _, errors = lib.load_inquiries(wiki)
    assert any(fragment in e for e in errors), errors


def test_fenced_yaml_in_the_body_is_not_read_as_frontmatter(tmp_path):
    """Regression for the unanchored-frontmatter class (okengine#349): a ```yaml block in the
    body must not leak fields into the page."""
    wiki = tmp_path / "wiki"
    _write(wiki, "fenced", VALID + "\n```yaml\nstatus: closed\nterms: []\n```\n")
    inquiries, errors = lib.load_inquiries(wiki)
    assert not errors
    assert inquiries[0].status == "open" and len(inquiries[0].terms) == 2


# ── the reachability gate ───────────────────────────────────────────────────

def test_connector_that_exists_passes(tmp_path):
    wiki = tmp_path / "wiki"
    _write(wiki, "ok", VALID)
    inquiries, _ = lib.load_inquiries(wiki)
    assert lib.connector_errors(inquiries, {"test.search"}) == []


def test_unknown_connector_is_rejected(tmp_path):
    """NEGATIVE: the defect this gate exists for. A misspelled connector makes the lane skip the
    inquiry and the dossier render empty, which reads as 'the field is quiet'."""
    wiki = tmp_path / "wiki"
    _write(wiki, "typo", VALID.replace("test.search", "test.serach"))
    inquiries, _ = lib.load_inquiries(wiki)
    errors = lib.connector_errors(inquiries, {"test.search"})
    assert errors and "test.serach" in errors[0] and "not among the discovered" in errors[0]


def test_missing_connector_field_is_rejected(tmp_path):
    """NEGATIVE: a question declared with no way to gather evidence for it."""
    wiki = tmp_path / "wiki"
    _write(wiki, "no-conn", VALID.replace("connector: test.search\n", ""))
    inquiries, _ = lib.load_inquiries(wiki)
    errors = lib.connector_errors(inquiries, {"test.search"})
    assert errors and "no `connector:` declared" in errors[0]


def test_closed_inquiry_is_exempt_from_the_reachability_gate(tmp_path):
    """Retiring a connector must not be blocked by the archived questions that once used it."""
    wiki = tmp_path / "wiki"
    _write(wiki, "done", VALID.replace("status: open", "status: closed")
           .replace("test.search", "retired.connector"))
    inquiries, _ = lib.load_inquiries(wiki)
    assert lib.connector_errors(inquiries, {"test.search"}) == []


def test_paused_inquiry_is_still_gated(tmp_path):
    """Paused is temporary, so a broken connector on a paused inquiry is still a defect."""
    wiki = tmp_path / "wiki"
    _write(wiki, "paused", VALID.replace("status: open", "status: paused")
           .replace("test.search", "gone"))
    inquiries, _ = lib.load_inquiries(wiki)
    assert lib.connector_errors(inquiries, {"test.search"})


# ── collection lane ─────────────────────────────────────────────────────────

collect = _load("inquiry_collect")

MANIFEST = {"id": "test.search", "inputs": {"required": ["q"]}}


def test_query_binds_to_the_single_required_input():
    name, error = collect.query_input(MANIFEST)
    assert (name, error) == ("q", None)


def test_connector_with_no_required_inputs_is_refused():
    """NEGATIVE: a connector a term cannot parameterize would run its bare, unfiltered request
    once per term. That is a firehose pull wearing an inquiry's name."""
    _, error = collect.query_input({"id": "x", "inputs": {"required": []}})
    assert error and "cannot parameterize" in error


def test_param_precedence_is_inquiry_then_term_then_query(tmp_path):
    inquiry = lib.Inquiry(slug="s", path=tmp_path, question="q", status="open", terms=[],
                          collection_params={"limit": "10", "sort": "relevance"})
    term = lib.Term(query="alpha", params={"sort": "recency"})
    params = collect.build_params(inquiry, term, "q")
    assert params == {"limit": "10", "sort": "recency", "q": "alpha"}


def test_a_stray_param_cannot_overwrite_its_own_query(tmp_path):
    """The query is bound last on purpose."""
    inquiry = lib.Inquiry(slug="s", path=tmp_path, question="q", status="open", terms=[],
                          collection_params={"q": "wrong"})
    params = collect.build_params(inquiry, lib.Term(query="right"), "q")
    assert params["q"] == "right"


def test_no_relevance_floor_is_injected_by_default(tmp_path):
    """THE TRAP (okengine#746). A collection service scores records for its own primary domain.
    On one deployment the same AI-extinction story scored 0.008 from a tier-2 technology outlet
    and 0.886 from a state broadcaster, so inheriting that service's security floor would keep
    the propaganda copy and drop the reputable one. This lane must add no score knob the
    inquiry did not ask for."""
    inquiry = lib.Inquiry(slug="s", path=tmp_path, question="q", status="open", terms=[])
    params = collect.build_params(inquiry, lib.Term(query="alpha"), "q")
    assert params == {"q": "alpha"}
    assert not any("relevance" in k or "risk" in k or "score" in k for k in params)


def test_declared_floor_is_passed_through_untouched(tmp_path):
    """An inquiry INSIDE a connector's home domain may still want the floor; the lane must not
    second-guess an explicit declaration either."""
    inquiry = lib.Inquiry(slug="s", path=tmp_path, question="q", status="open", terms=[],
                          collection_params={"min_security_relevance": "0.5"})
    params = collect.build_params(inquiry, lib.Term(query="alpha"), "q")
    assert params["min_security_relevance"] == "0.5"


def _fixture_vault(tmp_path, page=VALID, manifest_id="test.search"):
    vault = tmp_path / "vault"
    (vault / "wiki" / lib.NS).mkdir(parents=True)
    (vault / "wiki" / lib.NS / "does-it-happen.md").write_text(page, encoding="utf-8")
    conn = tmp_path / "connectors"
    conn.mkdir()
    (conn / "search.yaml").write_text(
        "connector_version: 1\n"
        f"id: {manifest_id}\n"
        "name: Test search\nmode: query\n"
        "trust: {permission: public, data_sensitivity: clear, source_authority: Test}\n"
        "permissions: {network: true, allowed_hosts: [query.example], "
        "allow_private_network: false, write_raw: false}\n"
        "auth: {type: none, secret_refs: {}}\n"
        "inputs: {required: [q]}\n"
        "request: {url: 'https://query.example/search', method: GET, query: {q: '${input.q}'}}\n"
        "response: {format: json, records_path: results, stable_id_path: id}\n"
        "pagination: {type: none, max_pages: 1}\n"
        "checkpoint: {path: test.json}\n"
        "conditional_requests: {enabled: false}\n"
        "rate_limit: {max_requests: 5, per_seconds: 60}\n"
        "archive: {enabled: false, raw_responses: false, path: '', retention_days: 0}\n"
        "license: {name: Test, redistribution: prohibited, max_retention_days: 0}\n"
        "health: {path: test.json}\n", encoding="utf-8")
    return vault, conn


def _patch_env(monkeypatch, vault, conn, module):
    """Point a lane at a fixture vault.

    The lane imports `inquiry_lib` under its own name, so the module object it holds is NOT the
    one this test file loaded. Patch the lane's own reference or the lane keeps reading the real
    /opt/vault — a trap worth naming, since the symptom is a test that silently exercises
    production paths instead of the fixture.
    """
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_INQUIRY_CONNECTOR_DIR", str(conn))
    for target in {lib, module.lib}:
        monkeypatch.setattr(target, "VAULT", vault)
        monkeypatch.setattr(target, "WIKI", vault / "wiki")
    monkeypatch.setattr(module, "STATE", vault / ".okengine/inquiry/collect-state.json")


def test_collect_validate_mode_passes_on_a_good_vault(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, collect)
    assert collect.main(["--validate"]) == 0
    assert "OK" in capsys.readouterr().out


def test_collect_validate_mode_fails_on_an_unknown_connector(tmp_path, monkeypatch, capsys):
    """NEGATIVE: the gate must be non-zero, not a warning. A warning here produces an empty
    dossier an operator misreads as a quiet field."""
    vault, conn = _fixture_vault(tmp_path, manifest_id="something.else")
    _patch_env(monkeypatch, vault, conn, collect)
    assert collect.main(["--validate"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_collect_runs_each_term_and_records_yield(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, collect)
    seen: list[dict] = []

    def fake_run(manifest_path, params, timeout=180):
        seen.append(dict(params))
        return {"ok": True, "records": 3, "error": ""}

    monkeypatch.setattr(collect, "run_connector", fake_run)
    assert collect.main([]) == 0
    assert [p["q"] for p in seen] == ["alpha", "beta"]
    state = __import__("json").loads(
        (vault / ".okengine/inquiry/collect-state.json").read_text())
    assert state["terms"]["does-it-happen::alpha"]["total_records"] == 3
    assert "6 record(s)" in capsys.readouterr().out


def test_collect_term_cap_defers_rather_than_drops(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, collect)
    monkeypatch.setattr(collect, "MAX_TERMS", 1)
    monkeypatch.setattr(collect, "run_connector",
                        lambda *a, **k: {"ok": True, "records": 1, "error": ""})
    assert collect.main([]) == 0
    assert "1 term(s) deferred" in capsys.readouterr().out


def test_a_failing_term_does_not_abort_the_other_terms(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, collect)
    calls = {"n": 0}

    def flaky(manifest_path, params, timeout=180):
        calls["n"] += 1
        if params["q"] == "alpha":
            return {"ok": False, "records": 0, "error": "HTTP 503"}
        return {"ok": True, "records": 2, "error": ""}

    monkeypatch.setattr(collect, "run_connector", flaky)
    assert collect.main([]) == 0
    assert calls["n"] == 2
    out = capsys.readouterr().out
    assert "FAILED: HTTP 503" in out and "1 failure(s)" in out


def test_closed_inquiry_is_not_collected(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path, page=VALID.replace("status: open", "status: closed"))
    _patch_env(monkeypatch, vault, conn, collect)
    monkeypatch.setattr(collect, "run_connector",
                        lambda *a, **k: pytest.fail("a closed inquiry must not collect"))
    assert collect.main([]) == 0
    assert "0 open inquiry(s)" in capsys.readouterr().out


# ── dossier ─────────────────────────────────────────────────────────────────

dossier = _load("inquiry_dossier")


def _utc_today():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).date()


def _render(monkeypatch, vault, conn, runs, page=VALID, evidence=None):
    _patch_env(monkeypatch, vault, conn, dossier)
    import json as _json
    state = vault / ".okengine/inquiry/collect-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(_json.dumps({"terms": runs}), encoding="utf-8")
    monkeypatch.setattr(dossier, "DASH", vault / "wiki/dashboards/inquiry")
    assert dossier.main([]) == 0
    return (vault / "wiki/dashboards/inquiry/does-it-happen.md").read_text()


def test_dossier_reports_a_healthy_term_as_ok(tmp_path, monkeypatch):
    vault, conn = _fixture_vault(tmp_path)
    today = _utc_today().isoformat()
    out = _render(monkeypatch, vault, conn, {
        "does-it-happen::alpha": {"last_run": today, "last_yield": today,
                                  "total_records": 9, "ok": True, "error": ""}})
    assert "| ok |" in out and "| 9 |" in out


def test_a_term_that_never_yielded_is_flagged_dry(tmp_path, monkeypatch):
    """THE STANDING DETECTOR. framework validate catches a term whose connector does not exist;
    only this catches a term whose connector exists, runs clean, and answers nothing. Without
    it an inquiry looks healthy while collecting zero, and the empty Evidence section reads as
    a finding about the field rather than a collection fault."""
    vault, conn = _fixture_vault(tmp_path)
    out = _render(monkeypatch, vault, conn, {
        "does-it-happen::alpha": {"last_run": "2026-09-01", "last_yield": "",
                                  "total_records": 0, "ok": True, "error": ""}})
    assert "DRY — never yielded" in out
    assert "has never yielded a record" in out
    assert "collection fault, not a finding about the field" in out


def test_a_term_gone_quiet_past_the_threshold_is_flagged_dry(tmp_path, monkeypatch):
    vault, conn = _fixture_vault(tmp_path)
    monkeypatch.setattr(dossier, "DRY_DAYS", 14)
    # UTC, matching the lane. Computing this from the HOST's local date makes the assertion
    # off-by-one whenever the two disagree — the host-vs-container time trap, in miniature.
    stale = (_utc_today() - __import__("datetime").timedelta(days=30)).isoformat()
    out = _render(monkeypatch, vault, conn, {
        "does-it-happen::alpha": {"last_run": "2026-09-10", "last_yield": stale,
                                  "total_records": 4, "ok": True, "error": ""}})
    assert "DRY — 30d since last yield" in out


def test_a_recently_yielding_term_is_not_flagged(tmp_path, monkeypatch):
    vault, conn = _fixture_vault(tmp_path)
    recent = (_utc_today() - __import__("datetime").timedelta(days=2)).isoformat()
    out = _render(monkeypatch, vault, conn, {
        "does-it-happen::alpha": {"last_run": recent, "last_yield": recent,
                                  "total_records": 4, "ok": True, "error": ""}})
    assert "DRY" not in out


def test_a_failing_term_shows_its_error_not_a_dry_flag(tmp_path, monkeypatch):
    """A connector erroring and a connector answering nothing are different faults and must not
    be reported as the same thing."""
    vault, conn = _fixture_vault(tmp_path)
    out = _render(monkeypatch, vault, conn, {
        "does-it-happen::alpha": {"last_run": "2026-09-10", "last_yield": "",
                                  "total_records": 0, "ok": False, "error": "HTTP 503"}})
    assert "ERROR: HTTP 503" in out and "DRY" not in out.split("ERROR: HTTP 503")[0]


def test_an_unrun_term_is_not_reported_as_dry(tmp_path, monkeypatch):
    """A term added today has no history; calling it DRY would cry wolf on every new term."""
    vault, conn = _fixture_vault(tmp_path)
    out = _render(monkeypatch, vault, conn, {})
    assert "not yet run" in out and "DRY" not in out


def test_evidence_is_gathered_from_declared_lists_and_back_references(tmp_path, monkeypatch):
    """Both directions occur: an author grouping known pages, and a lane attributing what it
    produced. Neither alone is complete."""
    vault, conn = _fixture_vault(
        tmp_path, page=VALID.replace("terms: [alpha, beta]",
                                     "terms: [alpha]\nassessments: [assessments/declared]"))
    back = vault / "wiki/predictions/backref.md"
    back.parent.mkdir(parents=True, exist_ok=True)
    back.write_text("---\ntype: prediction\ninquiry: does-it-happen\n---\n", encoding="utf-8")
    out = _render(monkeypatch, vault, conn, {})
    assert "[[assessments/declared]]" in out
    assert "[[predictions/backref]]" in out
    assert "Assessments (is it true?) — 1" in out
    assert "Predictions (does it resolve?) — 1" in out


def test_a_page_referencing_another_inquiry_is_not_attributed(tmp_path, monkeypatch):
    """NEGATIVE: back-reference matching must be exact, or evidence bleeds across inquiries."""
    vault, conn = _fixture_vault(tmp_path)
    other = vault / "wiki/predictions/elsewhere.md"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("---\ntype: prediction\ninquiry: some-other-question\n---\n",
                     encoding="utf-8")
    out = _render(monkeypatch, vault, conn, {})
    assert "elsewhere" not in out
    assert "Predictions (does it resolve?) — 0" in out


def test_dossier_index_lists_every_inquiry(tmp_path, monkeypatch):
    vault, conn = _fixture_vault(tmp_path)
    _render(monkeypatch, vault, conn, {})
    index = (vault / "wiki/dashboards/inquiry/INDEX.md").read_text()
    assert "[[dashboards/inquiry/does-it-happen]]" in index and "`open`" in index


def test_dossier_is_idempotent(tmp_path, monkeypatch):
    vault, conn = _fixture_vault(tmp_path)
    first = _render(monkeypatch, vault, conn, {})
    second = _render(monkeypatch, vault, conn, {})
    assert first == second


# ── framework validate wiring ───────────────────────────────────────────────

def _validate_pack(tmp_path, *, page=VALID, manifest_id="test.search", enable=True):
    """Build a minimal pack and run the real check through framework_validate_extensions."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts import framework_validate as fv

    pack = tmp_path / "pack"
    (pack / "wiki" / lib.NS).mkdir(parents=True)
    (pack / "wiki" / lib.NS / "does-it-happen.md").write_text(page, encoding="utf-8")
    (pack / ".okengine").mkdir(parents=True, exist_ok=True)
    if enable:
        (pack / ".okengine" / "extensions.yaml").write_text(
            "enabled:\n  okengine.inquiry: {}\n", encoding="utf-8")
    conn = pack / "connectors"
    conn.mkdir()
    (conn / "search.yaml").write_text(f"id: {manifest_id}\nname: Test\n", encoding="utf-8")

    report = fv.Report()
    fv.check_inquiries(pack, report)
    return report


def test_framework_validate_passes_a_consistent_pack(tmp_path):
    report = _validate_pack(tmp_path)
    assert not [x for x in report.rows if x[0] == "FAIL"], report.rows


def test_framework_validate_fails_an_unknown_connector(tmp_path):
    """NEGATIVE: the deploy-time gate. Without it this defect first shows up as an inquiry that
    silently collects nothing."""
    report = _validate_pack(tmp_path, manifest_id="something.else")
    fails = [x for x in report.rows if x[0] == "FAIL"]
    assert fails and "not among the discovered" in " ".join(str(f) for f in fails)


def test_framework_validate_fails_an_inquiry_with_no_terms(tmp_path):
    """NEGATIVE: a question with nothing collecting for it."""
    report = _validate_pack(tmp_path, page=VALID.replace("terms: [alpha, beta]", "terms: []"))
    fails = [x for x in report.rows if x[0] == "FAIL"]
    assert fails and "collects nothing" in " ".join(str(f) for f in fails)


def test_framework_validate_is_a_no_op_when_the_extension_is_not_enabled(tmp_path):
    """A pack that never enabled the extension must not be judged by its contract."""
    report = _validate_pack(tmp_path, manifest_id="something.else", enable=False)
    assert not [x for x in report.rows if x[0] == "FAIL"], report.rows


# ── connector discovery: every reject path ──────────────────────────────────
#
# These exist because the mutation campaign showed the discovery and subprocess layers were
# almost entirely unexercised: the lane tests above monkeypatch `run_connector` wholesale, so
# every branch inside it survived. A `continue` silently becoming a `break` here would stop
# discovery at the first bad manifest and make the rest of a deployment's connectors vanish.

def _conn_dir(tmp_path, files: dict[str, str]):
    d = tmp_path / "connectors"
    d.mkdir(exist_ok=True)
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
    return d


GOOD = ("connector_version: 1\nid: {id}\nname: T\nmode: query\n"
        "trust: {{permission: public, data_sensitivity: clear, source_authority: T}}\n"
        "permissions: {{network: true, allowed_hosts: [q.example], "
        "allow_private_network: false, write_raw: false}}\n"
        "auth: {{type: none, secret_refs: {{}}}}\ninputs: {{required: [q]}}\n"
        "request: {{url: 'https://q.example/s', method: GET, query: {{q: '${{input.q}}'}}}}\n"
        "response: {{format: json, records_path: results, stable_id_path: id}}\n"
        "pagination: {{type: none, max_pages: 1}}\ncheckpoint: {{path: t.json}}\n"
        "conditional_requests: {{enabled: false}}\nrate_limit: {{max_requests: 5, per_seconds: 60}}\n"
        "archive: {{enabled: false, raw_responses: false, path: '', retention_days: 0}}\n"
        "license: {{name: T, redistribution: prohibited, max_retention_days: 0}}\n"
        "health: {{path: t.json}}\n")


def test_discovery_finds_every_valid_manifest(tmp_path):
    d = _conn_dir(tmp_path, {"a.yaml": GOOD.format(id="one"), "b.yml": GOOD.format(id="two")})
    paths, manifests, problems = collect.discover_connectors(d)
    assert set(paths) == {"one", "two"} and not problems
    assert manifests["one"]["id"] == "one"


def test_discovery_reports_a_missing_directory(tmp_path):
    paths, _, problems = collect.discover_connectors(tmp_path / "nope")
    assert paths == {} and problems and "not found" in problems[0]


def test_a_malformed_manifest_does_not_stop_discovery(tmp_path):
    """The continue-vs-break distinction. One unparseable file must not hide the rest."""
    d = _conn_dir(tmp_path, {"a.yaml": "{{{not yaml", "b.yaml": GOOD.format(id="two")})
    paths, _, problems = collect.discover_connectors(d)
    assert "two" in paths, "a later valid manifest must still be discovered"
    assert problems


def test_an_invalid_manifest_does_not_stop_discovery(tmp_path):
    """Parses fine, fails the runtime contract. Same continue-vs-break requirement."""
    d = _conn_dir(tmp_path, {"a.yaml": "connector_version: 1\nid: bad\n",
                             "b.yaml": GOOD.format(id="two")})
    paths, _, problems = collect.discover_connectors(d)
    assert "two" in paths
    assert any("a.yaml" in p for p in problems)


def test_a_duplicate_connector_id_is_reported_and_the_first_wins(tmp_path):
    d = _conn_dir(tmp_path, {"a.yaml": GOOD.format(id="same"), "b.yaml": GOOD.format(id="same"),
                             "c.yaml": GOOD.format(id="other")})
    paths, _, problems = collect.discover_connectors(d)
    assert paths["same"].name == "a.yaml"
    assert "other" in paths, "discovery must continue past a duplicate"
    assert any("duplicate connector id" in p for p in problems)


def test_connector_dir_prefers_the_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OKENGINE_INQUIRY_CONNECTOR_DIR", str(tmp_path / "x"))
    assert collect.connector_dir() == tmp_path / "x"


def test_connector_dir_falls_back_to_the_vault_when_nothing_is_deployed(tmp_path, monkeypatch):
    monkeypatch.delenv("OKENGINE_INQUIRY_CONNECTOR_DIR", raising=False)
    monkeypatch.setattr(collect.lib, "VAULT", tmp_path)
    monkeypatch.setattr(collect, "Path", Path)
    result = collect.connector_dir()
    assert result in (tmp_path / "connectors", Path("/opt/data/config/connectors"))


# ── run_connector: the subprocess boundary ──────────────────────────────────

class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_run_connector_reads_the_record_count_from_the_runtime_json(tmp_path, monkeypatch):
    monkeypatch.setattr(collect.subprocess, "run",
                        lambda *a, **k: _Proc(stdout='{"ok": true, "records": 7}\n'))
    assert collect.run_connector(tmp_path / "m.yaml", {"q": "x"}) == {
        "ok": True, "records": 7, "error": ""}


def test_run_connector_ignores_non_json_chatter_before_the_result(tmp_path, monkeypatch):
    out = 'warming up\nnot json at all\n{"ok": true, "records": 2}\n'
    monkeypatch.setattr(collect.subprocess, "run", lambda *a, **k: _Proc(stdout=out))
    assert collect.run_connector(tmp_path / "m.yaml", {"q": "x"})["records"] == 2


def test_run_connector_reports_zero_when_the_runtime_says_nothing_about_records(tmp_path, monkeypatch):
    monkeypatch.setattr(collect.subprocess, "run",
                        lambda *a, **k: _Proc(stdout='{"ok": true}\n'))
    outcome = collect.run_connector(tmp_path / "m.yaml", {"q": "x"})
    assert outcome["ok"] and outcome["records"] == 0


def test_run_connector_surfaces_a_nonzero_exit_as_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(collect.subprocess, "run",
                        lambda *a, **k: _Proc(returncode=2, stderr="boom"))
    outcome = collect.run_connector(tmp_path / "m.yaml", {"q": "x"})
    assert outcome == {"ok": False, "records": 0, "error": "boom"}


def test_run_connector_falls_back_to_stdout_when_stderr_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(collect.subprocess, "run",
                        lambda *a, **k: _Proc(returncode=2, stdout="detail on stdout"))
    assert collect.run_connector(tmp_path / "m.yaml", {"q": "x"})["error"] == "detail on stdout"


def test_run_connector_names_the_exit_code_when_there_is_no_output(tmp_path, monkeypatch):
    monkeypatch.setattr(collect.subprocess, "run", lambda *a, **k: _Proc(returncode=3))
    assert "exit 3" in collect.run_connector(tmp_path / "m.yaml", {"q": "x"})["error"]


def test_run_connector_reports_a_timeout_with_its_duration(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise collect.subprocess.TimeoutExpired(cmd="x", timeout=5)
    monkeypatch.setattr(collect.subprocess, "run", boom)
    outcome = collect.run_connector(tmp_path / "m.yaml", {"q": "x"}, timeout=5)
    assert outcome["ok"] is False and outcome["error"] == "timeout after 5s"


def test_run_connector_passes_every_param_sorted(tmp_path, monkeypatch):
    seen = {}

    def capture(argv, **kwargs):
        seen["argv"] = argv
        return _Proc(stdout='{"records": 0}')
    monkeypatch.setattr(collect.subprocess, "run", capture)
    collect.run_connector(tmp_path / "m.yaml", {"z": "1", "a": "2", "q": "term"})
    argv = seen["argv"]
    pairs = [argv[i + 1] for i, v in enumerate(argv) if v == "--param"]
    assert pairs == ["a=2", "q=term", "z=1"], "params must be deterministic across runs"


# ── collect state ───────────────────────────────────────────────────────────

def test_collect_state_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "STATE", tmp_path / "deep" / "state.json")
    collect._save_state({"terms": {"a": {"records": 1}}})
    assert collect._load_state()["terms"]["a"]["records"] == 1


def test_corrupt_collect_state_is_treated_as_empty_not_fatal(tmp_path, monkeypatch):
    """A truncated state file must not wedge the lane forever."""
    state = tmp_path / "state.json"
    state.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(collect, "STATE", state)
    assert collect._load_state() == {}


def test_absent_collect_state_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "STATE", tmp_path / "missing.json")
    assert collect._load_state() == {}


def test_total_records_accumulate_across_runs(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, collect)
    monkeypatch.setattr(collect, "run_connector",
                        lambda *a, **k: {"ok": True, "records": 4, "error": ""})
    collect.main([])
    collect.main([])
    import json as _json
    state = _json.loads((vault / ".okengine/inquiry/collect-state.json").read_text())
    assert state["terms"]["does-it-happen::alpha"]["total_records"] == 8


def test_last_yield_is_retained_when_a_later_run_returns_nothing(tmp_path, monkeypatch):
    """The DRY detector reads last_yield, so a zero-record run must not erase the history."""
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, collect)
    monkeypatch.setattr(collect, "run_connector",
                        lambda *a, **k: {"ok": True, "records": 5, "error": ""})
    collect.main([])
    monkeypatch.setattr(collect, "run_connector",
                        lambda *a, **k: {"ok": True, "records": 0, "error": ""})
    collect.main([])
    import json as _json
    rec = _json.loads((vault / ".okengine/inquiry/collect-state.json").read_text())[
        "terms"]["does-it-happen::alpha"]
    assert rec["last_yield"] and rec["records"] == 0 and rec["total_records"] == 5


# ── dossier helpers: the branches the render tests walk past ────────────────

@pytest.mark.parametrize("stamp,expected", [
    ("2026-09-01", 11),
    ("2026-09-12T04:00:00+00:00", 0),   # a full ISO timestamp is truncated to its date
    ("2026-09-12", 0),
])
def test_days_since_reads_a_date_or_a_timestamp(stamp, expected):
    import datetime as dt
    assert dossier._days_since(stamp, dt.date(2026, 9, 12)) == expected


@pytest.mark.parametrize("stamp", ["", "never", "not-a-date", None, "2026-13-45"])
def test_days_since_returns_none_for_anything_unparseable(stamp):
    """None must be distinguishable from 0 days: 'unknown' is not 'today'."""
    import datetime as dt
    assert dossier._days_since(stamp, dt.date(2026, 9, 12)) is None


def test_dossier_state_is_empty_when_absent_or_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(dossier, "STATE", tmp_path / "missing.json")
    assert dossier._load_state() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(dossier, "STATE", bad)
    assert dossier._load_state() == {}


def _evidence_vault(tmp_path, pages: dict[str, str]):
    wiki = tmp_path / "wiki"
    _write(wiki, "does-it-happen", VALID)
    for rel, body in pages.items():
        p = wiki / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    inquiries, _ = lib.load_inquiries(wiki)
    return dossier.gather_evidence(wiki, inquiries)["does-it-happen"]


def test_every_evidence_type_lands_in_its_own_bucket(tmp_path):
    found = _evidence_vault(tmp_path, {
        "assessments/a.md": "---\ntype: assessment\ninquiry: does-it-happen\n---\n",
        "predictions/p.md": "---\ntype: prediction\ninquiry: does-it-happen\n---\n",
        "solutions/s.md": "---\ntype: solution\ninquiry: does-it-happen\n---\n"})
    assert found == {"assessments": ["assessments/a"], "predictions": ["predictions/p"],
                     "solutions": ["solutions/s"]}


def test_a_page_of_an_unbucketed_type_is_skipped_and_the_walk_continues(tmp_path):
    """The continue-vs-break case: an attributed page of an irrelevant type must not stop the
    scan and hide the real evidence that sorts after it."""
    found = _evidence_vault(tmp_path, {
        "entities/aaa.md": "---\ntype: entity\ninquiry: does-it-happen\n---\n",
        "predictions/zzz.md": "---\ntype: prediction\ninquiry: does-it-happen\n---\n"})
    assert found["predictions"] == ["predictions/zzz"]


def test_an_unattributed_page_is_skipped_and_the_walk_continues(tmp_path):
    found = _evidence_vault(tmp_path, {
        "predictions/aaa.md": "---\ntype: prediction\n---\n",
        "predictions/zzz.md": "---\ntype: prediction\ninquiry: does-it-happen\n---\n"})
    assert found["predictions"] == ["predictions/zzz"]


def test_underscore_and_index_pages_are_skipped_and_the_walk_continues(tmp_path):
    found = _evidence_vault(tmp_path, {
        "predictions/_draft.md": "---\ntype: prediction\ninquiry: does-it-happen\n---\n",
        "predictions/INDEX.md": "---\ntype: prediction\ninquiry: does-it-happen\n---\n",
        "predictions/zzz.md": "---\ntype: prediction\ninquiry: does-it-happen\n---\n"})
    assert found["predictions"] == ["predictions/zzz"]


def test_the_inquiry_and_dashboard_namespaces_are_not_scanned_for_evidence(tmp_path):
    """A dossier citing a page must never make that dossier its own evidence."""
    found = _evidence_vault(tmp_path, {
        "dashboards/inquiry/x.md": "---\ntype: prediction\ninquiry: does-it-happen\n---\n",
        "inquiry/other.md": "---\ntype: prediction\ninquiry: does-it-happen\n---\n",
        "predictions/zzz.md": "---\ntype: prediction\ninquiry: does-it-happen\n---\n"})
    assert found["predictions"] == ["predictions/zzz"]


def test_a_declared_ref_is_not_duplicated_by_its_own_back_reference(tmp_path):
    wiki = tmp_path / "wiki"
    _write(wiki, "does-it-happen",
           VALID.replace("terms: [alpha, beta]", "terms: [alpha]\npredictions: [predictions/p]"))
    p = wiki / "predictions/p.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: prediction\ninquiry: does-it-happen\n---\n", encoding="utf-8")
    inquiries, _ = lib.load_inquiries(wiki)
    found = dossier.gather_evidence(wiki, inquiries)["does-it-happen"]
    assert found["predictions"] == ["predictions/p"], "declared + back-reference must dedupe"


def test_gather_evidence_on_a_missing_wiki_returns_the_declared_lists_only(tmp_path):
    inquiry = lib.Inquiry(slug="s", path=tmp_path, question="q", status="open", terms=[],
                          fm={"predictions": ["predictions/declared"]})
    found = dossier.gather_evidence(tmp_path / "nope", [inquiry])
    assert found["s"]["predictions"] == ["predictions/declared"]


def test_a_long_connector_error_is_truncated_on_the_dossier(tmp_path, monkeypatch):
    """Errors are shown, but one runaway traceback must not push the table off the page."""
    vault, conn = _fixture_vault(tmp_path)
    out = _render(monkeypatch, vault, conn, {
        "does-it-happen::alpha": {"last_run": "2026-09-10", "last_yield": "",
                                  "total_records": 0, "ok": False, "error": "E" * 500}})
    row = next(l for l in out.splitlines() if "ERROR:" in l)
    assert "E" * 60 in row and "E" * 100 not in row


def test_a_closed_inquiry_still_renders_a_dossier(tmp_path, monkeypatch):
    """Closing a question must not delete its record; the dossier is the archive."""
    vault, conn = _fixture_vault(tmp_path, page=VALID.replace("status: open", "status: closed"))
    out = _render(monkeypatch, vault, conn, {})
    assert "Status: `closed`" in out
    index = (vault / "wiki/dashboards/inquiry/INDEX.md").read_text()
    assert "`closed`" in index


# ── the machine-readable summaries and the exact boundaries ─────────────────
#
# A second mutation pass showed these lines unexercised: the JSON payloads both lanes emit
# (which a cron consumer reads, so a wrong count is a wrong operational signal), the cap
# arithmetic, the DRY threshold comparison, and the paths the lanes write to. Each is asserted
# on its exact value rather than on "something was printed".

def _payload(capsys):
    """The last JSON object a lane prints — the cron-visible result."""
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    return __import__("json").loads(lines[-1])


def test_collect_payload_reports_every_counter(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, collect)
    monkeypatch.setattr(collect, "run_connector",
                        lambda *a, **k: {"ok": True, "records": 6, "error": ""})
    collect.main([])
    assert _payload(capsys) == {"wakeAgent": False, "inquiries": 1, "terms": 2,
                                "records": 12, "failures": 0}


def test_collect_payload_counts_failures_separately_from_records(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, collect)
    monkeypatch.setattr(collect, "run_connector", lambda mp, params, **k:
                        {"ok": False, "records": 0, "error": "x"} if params["q"] == "alpha"
                        else {"ok": True, "records": 5, "error": ""})
    collect.main([])
    payload = _payload(capsys)
    assert payload["records"] == 5 and payload["failures"] == 1 and payload["terms"] == 2


def test_collect_error_payload_sums_both_error_kinds(tmp_path, monkeypatch, capsys):
    """A page with a bad status AND an unknown connector must report 2, not 1."""
    vault, conn = _fixture_vault(
        tmp_path, page=VALID.replace("status: open", "status: nonsense"),
        manifest_id="something.else")
    _patch_env(monkeypatch, vault, conn, collect)
    assert collect.main([]) == 1
    assert _payload(capsys)["errors"] == 2


def test_collect_on_an_empty_namespace_names_the_path_it_looked_in(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    (vault / "wiki" / lib.NS / "does-it-happen.md").unlink()
    _patch_env(monkeypatch, vault, conn, collect)
    assert collect.main([]) == 0
    out = capsys.readouterr().out
    assert str(vault / "wiki" / lib.NS) in out
    assert "never invents one" in out


def test_inquiry_cap_defers_the_exact_surplus(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    for n in range(4):
        (vault / "wiki" / lib.NS / f"extra-{n}.md").write_text(
            VALID.replace("Does the thing happen?", f"Q{n}?"), encoding="utf-8")
    _patch_env(monkeypatch, vault, conn, collect)
    monkeypatch.setattr(collect, "MAX_INQUIRIES", 2)
    monkeypatch.setattr(collect, "run_connector",
                        lambda *a, **k: {"ok": True, "records": 1, "error": ""})
    collect.main([])
    out = capsys.readouterr().out
    assert "3 open inquiry(s) deferred" in out          # 5 declared - 2 processed
    assert "5 open inquiry(s), 4 term(s) run" in out    # only the 2 processed ran their terms


def test_no_deferral_is_reported_when_everything_fits(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, collect)
    monkeypatch.setattr(collect, "run_connector",
                        lambda *a, **k: {"ok": True, "records": 1, "error": ""})
    collect.main([])
    assert "deferred" not in capsys.readouterr().out


def test_term_count_exactly_at_the_cap_defers_nothing(tmp_path, monkeypatch, capsys):
    """Boundary: > not >=. Two terms and a cap of two must report no deferral."""
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, collect)
    monkeypatch.setattr(collect, "MAX_TERMS", 2)
    monkeypatch.setattr(collect, "run_connector",
                        lambda *a, **k: {"ok": True, "records": 1, "error": ""})
    collect.main([])
    assert "term(s) deferred" not in capsys.readouterr().out


def test_validate_mode_names_the_directory_it_searched(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, collect)
    collect.main(["--validate"])
    out = capsys.readouterr().out
    assert "1 inquiry page(s) valid against 1 connector(s)" in out and str(conn) in out


# ── dossier: threshold, ordering, and the paths written ─────────────────────

@pytest.mark.parametrize("age,expect_dry", [(13, False), (14, True), (15, True)])
def test_dry_threshold_is_inclusive_at_the_boundary(tmp_path, monkeypatch, age, expect_dry):
    """>= not >. A term that has been silent for exactly the threshold IS dry."""
    vault, conn = _fixture_vault(tmp_path)
    monkeypatch.setattr(dossier, "DRY_DAYS", 14)
    import datetime as dt
    stamp = (_utc_today() - dt.timedelta(days=age)).isoformat()
    out = _render(monkeypatch, vault, conn, {
        "does-it-happen::alpha": {"last_run": "2026-09-10", "last_yield": stamp,
                                  "total_records": 1, "ok": True, "error": ""}})
    assert (f"DRY — {age}d since last yield" in out) is expect_dry


def test_dossier_is_written_at_the_slug_path(tmp_path, monkeypatch):
    """One slug, one dossier path. A second spelling is how duplicate pages start."""
    vault, conn = _fixture_vault(tmp_path)
    _render(monkeypatch, vault, conn, {})
    written = sorted(p.name for p in (vault / "wiki/dashboards/inquiry").glob("*.md"))
    assert written == ["INDEX.md", "does-it-happen.md"]


def test_index_lists_open_inquiries_before_closed_ones(tmp_path, monkeypatch):
    """The sort key is (status != open, slug): open first, then alphabetical."""
    vault, conn = _fixture_vault(tmp_path)
    ns = vault / "wiki" / lib.NS
    (ns / "aaa-closed.md").write_text(VALID.replace("status: open", "status: closed"),
                                      encoding="utf-8")
    (ns / "zzz-open.md").write_text(VALID, encoding="utf-8")
    _render(monkeypatch, vault, conn, {})
    index = (vault / "wiki/dashboards/inquiry/INDEX.md").read_text()
    order = [l.split("[[dashboards/inquiry/")[1].split("]]")[0]
             for l in index.splitlines() if "[[dashboards/inquiry/" in l]
    assert order == ["does-it-happen", "zzz-open", "aaa-closed"]


def test_index_counts_evidence_per_inquiry(tmp_path, monkeypatch):
    vault, conn = _fixture_vault(
        tmp_path, page=VALID.replace("terms: [alpha, beta]",
                                     "terms: [alpha]\nassessments: [a/one, a/two]"))
    _render(monkeypatch, vault, conn, {})
    index = (vault / "wiki/dashboards/inquiry/INDEX.md").read_text()
    row = next(l for l in index.splitlines() if "does-it-happen" in l)
    assert "| 1 | 2 | 0 | 0 |" in row       # terms, assessments, predictions, solutions


def test_dossier_payload_counts_what_it_rendered(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    _patch_env(monkeypatch, vault, conn, dossier)
    monkeypatch.setattr(dossier, "DASH", vault / "wiki/dashboards/inquiry")
    (vault / "wiki" / lib.NS / "second.md").write_text(VALID, encoding="utf-8")
    dossier.main([])
    assert _payload(capsys) == {"wakeAgent": False, "inquiries": 2}


def test_dossier_on_an_empty_namespace_names_the_path_and_renders_nothing(tmp_path, monkeypatch, capsys):
    vault, conn = _fixture_vault(tmp_path)
    (vault / "wiki" / lib.NS / "does-it-happen.md").unlink()
    _patch_env(monkeypatch, vault, conn, dossier)
    monkeypatch.setattr(dossier, "DASH", vault / "wiki/dashboards/inquiry")
    assert dossier.main([]) == 0
    out = capsys.readouterr().out
    assert str(vault / "wiki" / lib.NS) in out
    assert not (vault / "wiki/dashboards/inquiry").exists()


def test_title_falls_back_to_the_question_then_the_slug(tmp_path, monkeypatch):
    vault, conn = _fixture_vault(tmp_path)
    out = _render(monkeypatch, vault, conn, {})
    assert 'title: "Inquiry: Does the thing happen?"' in out   # no title: -> the question
    assert "# does-it-happen" in out                            # heading -> the slug


def test_a_declared_title_wins_over_both(tmp_path, monkeypatch):
    vault, conn = _fixture_vault(tmp_path, page=VALID.replace(
        "type: inquiry", "type: inquiry\ntitle: A Named Question"))
    out = _render(monkeypatch, vault, conn, {})
    assert 'title: "Inquiry: A Named Question"' in out and "# A Named Question" in out


def test_the_opened_date_is_shown_only_when_declared(tmp_path, monkeypatch):
    vault, conn = _fixture_vault(tmp_path)
    assert "opened" not in _render(monkeypatch, vault, conn, {}).split("## Terms")[0]
    vault2, conn2 = _fixture_vault(tmp_path / "b", page=VALID.replace(
        "type: inquiry", "type: inquiry\nopened: 2026-01-05"))
    assert "· opened 2026-01-05" in _render(monkeypatch, vault2, conn2, {})


# ── the remedy convention (okengine#746) ────────────────────────────────────
#
# A solution is not its own type. It is an assessment carrying `assessment_kind: remedy`, so
# the adversarial-evidence contract applies to a proposed intervention unchanged. The cost is
# that type alone can no longer bucket a page, and getting that wrong is silent: every remedy
# would file under "is this true?" and the Solutions column would read empty forever.

@pytest.mark.parametrize("fm,expected", [
    ({"type": "assessment", "assessment_kind": "remedy"}, "solutions"),
    ({"type": "assessment", "assessment_kind": "REMEDY"}, "solutions"),
    ({"type": "assessment", "assessment_kind": "  remedy  "}, "solutions"),
    ({"type": "assessment"}, "assessments"),
    ({"type": "assessment", "assessment_kind": "actor-country-linkage"}, "assessments"),
    ({"type": "prediction"}, "predictions"),
    ({"type": "solution"}, "solutions"),
    ({"type": "entity"}, None),
    ({"type": "entity", "assessment_kind": "remedy"}, None),
    ({}, None),
])
def test_bucketing_routes_remedies_to_solutions(fm, expected):
    assert dossier._bucket(fm) == expected


def test_a_remedy_assessment_lands_under_solutions_not_assessments(tmp_path):
    """NEGATIVE for the regression this convention creates: bucketing on type alone would put
    this in Assessments and leave Solutions empty."""
    found = _evidence_vault(tmp_path, {
        "assessments/ai-control.md":
            "---\ntype: assessment\nassessment_kind: remedy\ninquiry: does-it-happen\n---\n",
        "assessments/is-it-true.md":
            "---\ntype: assessment\ninquiry: does-it-happen\n---\n"})
    assert found["solutions"] == ["assessments/ai-control"]
    assert found["assessments"] == ["assessments/is-it-true"]


def test_a_remedy_shows_in_the_dossier_solutions_section(tmp_path, monkeypatch):
    vault, conn = _fixture_vault(tmp_path)
    page = vault / "wiki/assessments/ai-control.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: assessment\nassessment_kind: remedy\n"
                    "inquiry: does-it-happen\n---\n", encoding="utf-8")
    out = _render(monkeypatch, vault, conn, {})
    assert "Solutions (what would fix it?) — 1" in out
    assert "[[assessments/ai-control]]" in out
    assert "Assessments (is it true?) — 0" in out


def test_the_assessments_fragment_declares_the_remedy_fields():
    """The convention is only real if the write path will accept the fields it needs. Pins the
    schema so a remedy's cost, prerequisites and decision cannot be silently dropped."""
    import yaml
    frag = (Path(__file__).resolve().parents[2]
            / "extensions/okengine.assessments/schema/assessments.schema.yaml")
    d = yaml.safe_load(frag.read_text())
    optional = set(d["owns"]["types"]["assessment"]["optional"])
    assert {"assessment_kind", "remedy_cost", "remedy_prerequisites",
            "remedy_decision", "inquiry"} <= optional
    assert d["field_shapes"]["remedy_prerequisites"] == "list"
    assert d["enums"]["remedy_decision"] == ["proposed", "adopted", "rejected", "deferred"]
    assert d["field_enums"]["remedy_decision"] == {"enum": "remedy_decision"}


def test_the_remedy_fields_are_all_optional_so_composition_stays_safe():
    """Every added field must be optional: a required one would invalidate every assessment
    page already in a deployed vault."""
    import yaml
    frag = (Path(__file__).resolve().parents[2]
            / "extensions/okengine.assessments/schema/assessments.schema.yaml")
    required = set(yaml.safe_load(frag.read_text())["owns"]["types"]["assessment"]["required"])
    assert not (required & {"remedy_cost", "remedy_prerequisites", "remedy_decision",
                            "inquiry", "assessment_kind"})


def test_decision_state_is_not_a_confidence_scale():
    """adopted/rejected say someone decided; they must never imply the evidence moved. Pinned
    because collapsing the two is the obvious future 'simplification'."""
    import yaml
    frag = (Path(__file__).resolve().parents[2]
            / "extensions/okengine.assessments/schema/assessments.schema.yaml")
    d = yaml.safe_load(frag.read_text())
    assert set(d["enums"]["remedy_decision"]).isdisjoint(set(d["enums"]["observation_confidence"]))
    assert "confidence" not in " ".join(d["enums"]["remedy_decision"])


# ── split_frontmatter: the four ways a page can fail to yield fields ─────────
#
# Every one of these returns ({}, ...) rather than raising, and every caller treats an empty
# frontmatter as "not an inquiry" and walks on. That leniency is deliberate, but it means a
# regression here is SILENT: the page stops being an inquiry and the vault simply collects less.

def test_an_unreadable_page_yields_no_frontmatter_instead_of_raising(tmp_path):
    """A directory where a file is expected is the cheap reproducible OSError. load_inquiries
    rglobs a live tree, so a path that cannot be read must not abort the whole scan."""
    assert lib.split_frontmatter(tmp_path) == ({}, "")


def test_a_page_without_frontmatter_returns_its_whole_text_as_body(tmp_path):
    page = tmp_path / "plain.md"
    page.write_text("no frontmatter here\njust prose\n", encoding="utf-8")
    fm, body = lib.split_frontmatter(page)
    assert fm == {}
    assert body == "no frontmatter here\njust prose\n", "body must be preserved intact"


def test_unparseable_frontmatter_yields_empty_fields_not_a_crash(tmp_path):
    page = tmp_path / "broken.md"
    page.write_text("---\nkey: [unclosed\n---\n\nBody.\n", encoding="utf-8")
    fm, body = lib.split_frontmatter(page)
    assert fm == {}, "malformed YAML must degrade to no fields"
    assert body.strip() == "Body.", "the body must still be returned"


def test_frontmatter_that_is_not_a_mapping_is_discarded(tmp_path):
    """A YAML list parses fine but is not fields. Returning it would hand every caller a list
    where it does `.get(...)`, turning a malformed page into an AttributeError mid-scan."""
    page = tmp_path / "listy.md"
    page.write_text("---\n- one\n- two\n---\n\nBody.\n", encoding="utf-8")
    fm, body = lib.split_frontmatter(page)
    assert fm == {}
    assert body.strip() == "Body."


# ── _as_list coercion, observed through exclude_domains ─────────────────────

def test_a_comma_string_of_exclude_domains_is_split_into_entries(tmp_path):
    """The write path's `list` coercion splits a comma string, so this boundary must agree or
    the same page means different things to the writer and the reader."""
    wiki = tmp_path / "wiki"
    _write(wiki, "does-it-happen", VALID.replace(
        "connector: test.search", "connector: test.search\nexclude_domains: 'a.example, b.example'"))
    inquiries, errors = lib.load_inquiries(wiki)
    assert not errors
    assert inquiries[0].exclude_domains == ["a.example", "b.example"]


def test_an_exclude_domains_value_of_the_wrong_type_yields_no_entries(tmp_path):
    """A scalar that is neither string nor list must not become a one-element list of junk: a
    bogus exclusion silently suppresses real evidence."""
    wiki = tmp_path / "wiki"
    _write(wiki, "does-it-happen", VALID.replace(
        "connector: test.search", "connector: test.search\nexclude_domains: 12345"))
    inquiries, _ = lib.load_inquiries(wiki)
    assert inquiries[0].exclude_domains == []


# ── the remaining page-contract rejections ──────────────────────────────────

def test_a_non_kebab_case_slug_is_rejected(tmp_path):
    """Slugs are the dossier's and the state file's keys. An upper-case or spaced filename
    resolves differently across the lanes that key on it."""
    wiki = tmp_path / "wiki"
    _write(wiki, "Does_It_Happen", VALID)
    _, errors = lib.load_inquiries(wiki)
    assert any("kebab-case" in e for e in errors), errors


def test_collection_params_that_is_not_a_mapping_is_rejected_and_ignored(tmp_path):
    """NEGATIVE: params feed straight into the connector invocation. A list here would either
    crash the lane or, worse, be partially applied."""
    wiki = tmp_path / "wiki"
    _write(wiki, "does-it-happen", VALID.replace(
        "connector: test.search", "connector: test.search\ncollection_params: [not, a, map]"))
    inquiries, errors = lib.load_inquiries(wiki)
    assert any("collection_params: must be a mapping" in e for e in errors), errors
    assert inquiries[0].collection_params == {}, "the bad value must not reach the connector"


# ── the deployed connector directory ────────────────────────────────────────

def test_connector_dir_uses_the_deployed_directory_when_it_exists(tmp_path, monkeypatch):
    """deploy-cron-scripts.sh stages a pack's connectors/ into /opt/data/config/connectors.
    When that exists it WINS over the vault fallback -- otherwise a deployed pack would be
    validated against a stale in-vault copy of its own manifests.

    The real path is absent in CI, so the directory probe is redirected rather than mocked away:
    the function still does a genuine is_dir() on a real directory.
    """
    monkeypatch.delenv("OKENGINE_INQUIRY_CONNECTOR_DIR", raising=False)
    deployed = tmp_path / "opt-data-config-connectors"
    deployed.mkdir()
    real_path = collect.Path

    class _Redirected(type(real_path())):
        def __new__(cls, *args):
            if args and str(args[0]) == "/opt/data/config/connectors":
                return real_path(deployed)
            return real_path(*args)

    monkeypatch.setattr(collect, "Path", _Redirected)
    monkeypatch.setattr(collect.lib, "VAULT", tmp_path / "vault")
    assert collect.connector_dir() == deployed, "a deployed connector dir must win over the vault"


# ── run_connector: a malformed JSON line is skipped, not fatal ──────────────

def test_run_connector_skips_a_line_that_looks_like_json_but_is_not(tmp_path, monkeypatch):
    """NEGATIVE: the `{`-prefix test is a cheap filter, not a parse. A truncated line -- exactly
    what a killed or buffering runtime emits -- must not take the whole term down with it."""
    out = '{"ok": true, "records":\n{"ok": true, "records": 5}\n'
    monkeypatch.setattr(collect.subprocess, "run", lambda *a, **k: _Proc(stdout=out))
    outcome = collect.run_connector(tmp_path / "m.yaml", {"q": "x"})
    assert outcome == {"ok": True, "records": 5, "error": ""}, (
        "a truncated JSON line must be skipped and the real result still read")


# ── main(): the two paths the lane tests walk past ──────────────────────────

def test_a_malformed_manifest_is_warned_about_without_failing_the_run(tmp_path, monkeypatch, capsys):
    """A connector that fails to parse is indistinguishable at the dossier from one that found
    nothing, so discovery problems must be SAID. They are warnings, not failures: one broken
    manifest must not stop every other inquiry from collecting."""
    vault, conn = _fixture_vault(tmp_path)
    (conn / "broken.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    _patch_env(monkeypatch, vault, conn, collect)
    monkeypatch.setattr(collect, "run_connector",
                        lambda *a, **k: {"ok": True, "records": 1, "error": ""})
    assert collect.main([]) == 0, "a broken manifest must not fail the whole lane"
    out = capsys.readouterr().out
    assert "WARN  connector broken.yaml" in out, out


def test_an_inquiry_whose_connector_takes_no_query_is_failed_and_skipped(tmp_path, monkeypatch, capsys):
    """NEGATIVE: a connector with no required input cannot be parameterized by a term, so running
    it would trigger an UNFILTERED pull. The inquiry is counted as a failure and skipped -- and
    crucially the loop continues, so one such inquiry does not strand the others."""
    vault, conn = _fixture_vault(tmp_path)
    # A VALID manifest that simply takes no inputs: the request is made static so nothing
    # references ${input.*}. Emptying `required` alone would make the manifest itself invalid,
    # and it would be dropped at discovery without ever reaching query_input.
    manifest = (conn / "search.yaml").read_text(encoding="utf-8")
    (conn / "search.yaml").write_text(
        manifest.replace("inputs: {required: [q]}", "inputs: {required: []}")
                .replace("query: {q: '${input.q}'}", "query: {q: everything}"), encoding="utf-8")
    _patch_env(monkeypatch, vault, conn, collect)
    called: list = []
    monkeypatch.setattr(collect, "run_connector",
                        lambda *a, **k: called.append(a) or {"ok": True, "records": 0, "error": ""})
    assert collect.main([]) == 0
    out = capsys.readouterr().out
    assert "FAIL  inquiry/does-it-happen" in out and "unfiltered pull" in out, out
    assert not called, "the connector must never be invoked without a bound query"
    import json as _json
    payload = _json.loads([ln for ln in out.splitlines() if ln.startswith("{")][-1])
    assert payload["failures"] == 1, payload


# ── dossier: contract errors are surfaced, not swallowed ───────────────────

def test_the_dossier_warns_about_a_malformed_inquiry_it_still_renders(tmp_path, monkeypatch, capsys):
    """The dossier is the operator's window. A page with a contract error still renders -- but
    silently rendering it would show an empty evidence list that reads as 'quiet field' rather
    than 'your page is broken'."""
    vault, conn = _fixture_vault(tmp_path, page=VALID.replace("status: open", "status: bogus"))
    _patch_env(monkeypatch, vault, conn, dossier)
    monkeypatch.setattr(dossier, "DASH", vault / "wiki/dashboards/inquiry")
    assert dossier.main([]) == 0
    out = capsys.readouterr().out
    assert "WARN  inquiry/does-it-happen" in out and "status: must be one of" in out, out


# ── framework validate: the early returns and the connector-id scan ─────────
#
# check_inquiries must be silent -- no OK and no FAIL -- whenever it cannot legitimately judge
# the pack. A verdict it is not entitled to is worse than none: an OK row here would be read as
# "the inquiries in this pack are reachable" by a deployment that has not been checked at all.

def _fv():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts import framework_validate as fv
    return fv


def _checks(discovery):
    """Build ExtensionChecks against a supplied discovery, wired exactly as framework_validate
    wires the real one -- so a change to that wiring breaks these tests rather than sliding past."""
    from scripts import framework_validate_extensions as fve
    fv = _fv()
    return fve.ExtensionChecks(load_yaml=fv._load_yaml, pack_meta=fv._pack_meta_mod,
                               discovery=discovery), fv.Report()


def _autospec_discovery():
    """A contract-enforcing stand-in for the real discovery module (CLAUDE.md: autospec, never a
    bare Mock -- a hand-rolled fake would not notice the real signatures changing)."""
    from unittest.mock import create_autospec
    return create_autospec(_fv()._discovery_mod(), spec_set=True)


def test_a_discovery_fault_makes_the_inquiry_check_abstain(tmp_path):
    """Discovery faults are already reported by the extension-resolution check. Reporting them
    again here as an inquiry failure would double-count one defect under two names."""
    disc = _autospec_discovery()
    disc.discover.side_effect = RuntimeError("unreadable extension.yaml")
    checks, report = _checks(lambda: disc)
    checks.check_inquiries(tmp_path / "pack", report)
    assert report.rows == [], "a discovery fault must not produce an inquiry verdict"


def test_an_extension_record_without_its_library_makes_the_check_abstain(tmp_path):
    """The check imports inquiry_lib from the DISCOVERED record, so the extension may be supplied
    at the pack or operator tier. A record whose directory has no inquiry_lib.py cannot be
    validated -- and must not be silently declared fine."""
    empty = tmp_path / "ext-without-lib"
    empty.mkdir()
    disc = _autospec_discovery()
    disc.discover.return_value = ({}, [])
    disc.load_enabled_state.return_value = (["okengine.inquiry"], [])
    disc.resolve_enabled.return_value = ({"okengine.inquiry": {"dir": str(empty)}}, [])
    checks, report = _checks(lambda: disc)
    checks.check_inquiries(tmp_path / "pack", report)
    assert report.rows == [], "a record with no inquiry_lib.py must not be judged"


def test_an_enabled_pack_with_no_inquiry_namespace_is_not_judged(tmp_path):
    """Enabling the extension is not the same as declaring a research topic. A pack that has not
    written an inquiry yet is not in violation of anything."""
    pack = tmp_path / "pack"
    (pack / ".okengine").mkdir(parents=True)
    (pack / ".okengine" / "extensions.yaml").write_text(
        "enabled:\n  okengine.inquiry: {}\n", encoding="utf-8")
    (pack / "wiki").mkdir()
    fv = _fv()
    report = fv.Report()
    fv.check_inquiries(pack, report)
    assert report.rows == [], "an inquiry-less pack must produce no verdict"


def test_an_inquiry_namespace_holding_no_inquiries_is_not_judged(tmp_path):
    """The namespace can exist holding only an INDEX or an unrelated page. Nothing to check is
    not the same as everything being fine."""
    pack = tmp_path / "pack"
    (pack / "wiki" / lib.NS).mkdir(parents=True)
    (pack / "wiki" / lib.NS / "INDEX.md").write_text("---\ntype: dashboard\n---\n", encoding="utf-8")
    (pack / ".okengine").mkdir(parents=True, exist_ok=True)
    (pack / ".okengine" / "extensions.yaml").write_text(
        "enabled:\n  okengine.inquiry: {}\n", encoding="utf-8")
    fv = _fv()
    report = fv.Report()
    fv.check_inquiries(pack, report)
    assert report.rows == [], "a namespace with no inquiry pages must produce no verdict"


def test_a_pack_with_no_connectors_directory_fails_every_open_inquiry(tmp_path):
    """NEGATIVE: no connectors at all is the strongest form of unreachable ingress. The scan must
    skip cleanly to the verdict rather than erroring on the missing directory."""
    import shutil
    _validate_pack(tmp_path)                     # builds a consistent pack, connectors and all
    pack = tmp_path / "pack"
    shutil.rmtree(pack / "connectors")           # then take the ingress away
    fv = _fv()
    report = fv.Report()
    fv.check_inquiries(pack, report)
    fails = [r for r in report.rows if r[0] == "FAIL"]
    assert fails and "(none discovered)" in " ".join(str(f) for f in fails), report.rows


def test_a_manifest_without_an_id_is_skipped_and_the_scan_continues(tmp_path):
    """NEGATIVE: an id-less manifest contributes no connector id. If that cut the scan short, a
    later valid manifest would vanish and its inquiry would be failed for no reason."""
    pack = tmp_path / "pack"
    (pack / "wiki" / lib.NS).mkdir(parents=True)
    (pack / "wiki" / lib.NS / "does-it-happen.md").write_text(VALID, encoding="utf-8")
    (pack / ".okengine").mkdir(parents=True, exist_ok=True)
    (pack / ".okengine" / "extensions.yaml").write_text(
        "enabled:\n  okengine.inquiry: {}\n", encoding="utf-8")
    conn = pack / "connectors"
    conn.mkdir()
    # sorts BEFORE the good one, so a scan that stopped here would never reach test.search
    (conn / "a-no-id.yaml").write_text("name: nameless\n", encoding="utf-8")
    (conn / "b-search.yaml").write_text("id: test.search\nname: Test\n", encoding="utf-8")
    fv = _fv()
    report = fv.Report()
    fv.check_inquiries(pack, report)
    assert not [r for r in report.rows if r[0] == "FAIL"], report.rows
    assert [r for r in report.rows if r[0] == "OK"], "the reachable inquiry must still pass"
