"""Bounded, replayable property tests for high-risk parser and authorization boundaries."""
from __future__ import annotations

import importlib.util
import ipaddress
import string
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

pytestmark = pytest.mark.security
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "cron"))
from scripts.cron import (  # noqa: E402
    feed_fetch, id_lib, output_contract, probe_lane_toolcalls, select_entity_candidates,
    source_connector, web_capture,
)


PROFILE = settings(
    max_examples=100,
    deadline=250,
    derandomize=True,
    suppress_health_check=(HealthCheck.too_slow,),
)


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _scope():
    return _module("property_scope", REPO / "okengine-mcp" / "scope.py")


RUN_RECEIPTS = _module(
    "property_run_receipts", REPO / "patches" / "cron-plus" / "run_receipts.py")


JSON_SCALARS = st.none() | st.booleans() | st.integers() | st.text(max_size=40)
JSON_VALUES = st.recursive(
    JSON_SCALARS,
    lambda children: st.lists(children, max_size=5)
    | st.dictionaries(st.text(max_size=20), children, max_size=5),
    max_leaves=20,
)


@PROFILE
@given(JSON_VALUES)
def test_output_contract_validation_is_total_and_bounded(value):
    errors = output_contract.validate(value)
    assert isinstance(errors, list)
    assert all(isinstance(error, str) and error for error in errors)


@PROFILE
@given(st.text(max_size=500))
def test_frontmatter_parser_never_crashes_or_returns_an_unsafe_shape(text):
    assert isinstance(select_entity_candidates.parse_frontmatter(text), dict)


@PROFILE
@given(st.text(max_size=500))
def test_identity_normalization_is_deterministic_bounded_and_unambiguous(raw):
    first = id_lib.normalize_key(raw)
    assert first == id_lib.normalize_key(raw)
    assert 1 <= len(first) <= 87
    assert set(first) <= set(string.ascii_lowercase + string.digits + "-")
    page_id = id_lib.make_id(raw, raw[::-1])
    assert page_id.count(":") == 1 and id_lib.is_id(page_id)


@PROFILE
@given(
    st.sampled_from(["..", ".", "\\..\\", "ok\0hidden"]),
    st.text(alphabet=string.ascii_letters + string.digits + "-_", min_size=1, max_size=30),
)
def test_scope_globs_never_authorize_traversal_or_control_syntax(injection, leaf):
    scope = _scope()
    candidate = f"dashboards/{injection}/{leaf}"
    assert not scope.path_in_scopes(candidate, ["**"])
    assert not scope.path_in_scopes(candidate, ["dashboards/**"])


@PROFILE
@example(ipaddress.ip_address("100.64.0.1"))
@given(st.ip_addresses(v=4).filter(lambda address: not address.is_global))
def test_capture_url_validation_rejects_every_nonpublic_address(address):
    result = [(2, 1, 6, "", (str(address), 80))]
    with patch.object(web_capture, "ALLOW_PRIVATE", False), \
            patch.object(
                web_capture.socket, "getaddrinfo", autospec=True, return_value=result), \
            pytest.raises(web_capture.CaptureError) as exc:
        web_capture._validate_url(f"http://{address}/document")
    assert exc.value.category == "ssrf"
    with patch.object(
            source_connector.socket, "getaddrinfo", autospec=True, return_value=result), \
            pytest.raises(source_connector.ConnectorError):
        source_connector._validate_network_url(
            f"http://{address}/document", [str(address)], allow_private=False)
    with patch.object(feed_fetch, "ALLOW_PRIVATE_FEEDS", False), \
            patch.object(
                feed_fetch.socket, "getaddrinfo", autospec=True, return_value=result), \
            pytest.raises(ValueError, match="non-public"):
        feed_fetch.fetch(f"http://{address}/feed")


@PROFILE
@given(st.text(min_size=1, max_size=200))
def test_token_hashes_are_fixed_width_deterministic_and_do_not_embed_secrets(token):
    scope = _scope()
    digest = scope.token_sha256(token)
    assert digest == scope.token_sha256(token)
    assert len(digest) == 64 and set(digest) <= set(string.hexdigits.lower())
    assert digest != token and digest != token.encode("utf-8").hex()


@PROFILE
@given(st.text(max_size=2000))
def test_receipt_parser_accepts_or_refuses_arbitrary_model_text_without_crashing(text):
    try:
        receipt, mode = RUN_RECEIPTS.parse_response_details(text)
    except RUN_RECEIPTS.ReceiptError:
        return
    assert isinstance(receipt, dict)
    assert mode in {"canonical", "recovered-unterminated-fence", "recovered-json"}


@PROFILE
@given(
    st.text(alphabet=string.ascii_lowercase + string.digits + "-", min_size=1, max_size=30),
    st.lists(
        st.text(alphabet=string.ascii_lowercase + string.digits + "_", min_size=1, max_size=30),
        min_size=1, max_size=10, unique=True,
    ),
)
def test_mcp_published_names_are_stable_and_match_the_wire_prefix(server, names):
    tools = [SimpleNamespace(name=name, description="", inputSchema={}) for name in names]
    published = probe_lane_toolcalls.build_array(server, tools)
    prefix = "mcp__" + server.replace("-", "_") + "__"
    assert [item["name"] for item in published] == [prefix + name for name in names]
    assert len({item["name"] for item in published}) == len(names)
