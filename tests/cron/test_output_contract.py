from pathlib import Path
import importlib.util
import sys


MOD = Path(__file__).parents[2] / "scripts" / "cron" / "output_contract.py"
spec = importlib.util.spec_from_file_location("output_contract", MOD)
oc = importlib.util.module_from_spec(spec)
sys.modules["output_contract"] = oc
spec.loader.exec_module(oc)


def contract(**overrides):
    value = {
        "api": 1,
        "allowed_namespaces": ["sources"],
        "allowed_types": ["source"],
        "operations": ["create", "update"],
        "required_fields": ["type", "raw"],
        "required_relationships": [],
        "body": {"required": True, "min_non_whitespace": 80},
        "unknown_fields": "review",
        "unresolved_links": "review",
        "placeholder_links": "reject",
        "completion": "per-selected-item",
    }
    value.update(overrides)
    return value


def test_valid_contract():
    assert oc.validate(contract()) == []
    assert oc.digest(contract()) == oc.digest({**contract()})
    assert oc.digest(contract()) != oc.digest(contract(allowed_types=["entity"]))


def test_unknown_version_key_and_bad_body_fail_loud():
    bad = contract(api=2, surprise=True, body={"required": False, "min_non_whitespace": 10})
    errors = oc.validate(bad)
    assert any("unknown key" in e for e in errors)
    assert any("api must be 1" in e for e in errors)
    assert any("cannot set a minimum" in e for e in errors)


def test_pack_policy_can_tighten_floor():
    floor = contract(allowed_namespaces=["sources", "entities"],
                     allowed_types=["source", "entity"], unknown_fields="review",
                     body={"required": True, "min_non_whitespace": 40})
    policy = contract(allowed_namespaces=["sources"], allowed_types=["source"],
                      operations=["create"], required_fields=["publisher"],
                      unknown_fields="reject", unresolved_links="reject",
                      body={"required": True, "min_non_whitespace": 100})
    effective = oc.compose(floor, policy)
    assert effective["allowed_namespaces"] == ["sources"]
    assert effective["operations"] == ["create"]
    assert effective["required_fields"] == ["type", "raw", "publisher"]
    assert effective["unknown_fields"] == "reject"
    assert effective["body"]["min_non_whitespace"] == 100


def test_pack_policy_cannot_weaken_floor():
    floor = contract(unknown_fields="reject")
    policy = contract(unknown_fields="allow")
    try:
        oc.compose(floor, policy)
    except ValueError as exc:
        assert "may not weaken" in str(exc)
    else:
        raise AssertionError("weaker policy accepted")


def test_pack_policy_can_narrow_domain_generic_wildcard_floor():
    floor = contract(allowed_namespaces=["*"], allowed_types=["*"])
    policy = contract(allowed_namespaces=["entities"], allowed_types=["company"])
    effective = oc.compose(floor, policy)
    assert effective["allowed_namespaces"] == ["entities"]
    assert effective["allowed_types"] == ["company"]
