"""Fast contracts before the full installer integration fixtures in mutation runs."""
import pytest
import yaml

from scripts.framework_install_domain import Plan, _merge_namespace_entries, merge_namespaces


@pytest.mark.parametrize("section", ["partitioning", "permissions", "tier"])
@pytest.mark.parametrize("layout", ["absent", "null-section", "empty-section", "no-child",
                                    "null-child", "empty-child", "block", "child-flow",
                                    "root-flow"])
def test_namespace_mapping_merge_preserves_all_existing_values(section, layout):
    shapes = {
        "absent": "",
        "null-section": f"{section}: null # retained\n",
        "empty-section": f"{section}: {{}} # retained\n",
        "no-child": f"{section}:\n  default: deny # retained\n",
        "null-child": f"{section}:\n  namespaces: # retained\n",
        "empty-child": f"{section}:\n  namespaces: {{}} # retained\n",
        "block": f"{section}:\n  default: deny\n  namespaces:\n"
                 "    protected: {create: false, update: false} # retained\n",
        "child-flow": f"{section}:\n  default: deny\n"
                      "  namespaces: {protected: {create: false, update: false}} # retained\n",
        "root-flow": f"{section}: {{default: deny, namespaces: "
                     "{protected: {create: false, update: false}}} # retained\n",
    }
    text = "unrelated: {preserve: true}\n" + shapes[layout] + "last: unchanged\n"
    expected = yaml.safe_load(text)
    prior = expected.get(section) or {}
    entries = {"incoming-a": {"strategy": "flat", "create": False},
               "incoming-b": {"strategy": "by-date", "update": True}}
    expected[section] = {**prior, "namespaces": {**(prior.get("namespaces") or {}), **entries}}

    result = _merge_namespace_entries(text, section, entries, "# fixture merge")

    assert yaml.safe_load(result) == expected
    assert result.count(f"\n{section}:") == 1
    if layout != "absent":
        assert "# retained" in result
    assert result.startswith("unrelated: {preserve: true}\n")
    assert "last: unchanged\n" in result


@pytest.mark.parametrize("layout", ["absent", "block", "flow"])
@pytest.mark.parametrize("contracts", [True, False])
@pytest.mark.parametrize("apply", [True, False])
def test_namespace_install_plan_carries_contracts_without_full_cli_setup(
        tmp_path, layout, contracts, apply):
    host, pack = tmp_path / "host", tmp_path / "pack"
    host.mkdir()
    pack.mkdir()
    (pack / "pack.yaml").write_text(
        "name: okpack-fixture\nowns:\n  namespaces: [incoming-a, incoming-b]\n")
    incoming = {
        "partitioning": {"incoming-a": {"strategy": "by-date"},
                         "incoming-b": {"strategy": "flat"}},
        "permissions": {"incoming-a": {"create": True, "update": False}},
        "tier": {"incoming-a": {"date_field": "published"}},
    }
    if not contracts:
        incoming = {"partitioning": incoming["partitioning"]}
    (pack / "schema.yaml").write_text(yaml.safe_dump(
        {section: {"namespaces": values} for section, values in incoming.items()}))
    prior = {"unrelated": "preserved"}
    if layout != "absent":
        prior.update({section: {"default": "preserved", "namespaces": {
            "protected": {"create": False, "update": False}}}
            for section in ("partitioning", "permissions", "tier")})
    original = yaml.safe_dump(prior, default_flow_style=False)
    if layout == "flow":
        original = "unrelated: preserved\n" + "".join(
            section + ": " + yaml.safe_dump(value, default_flow_style=True, width=1000)
            for section, value in prior.items() if section != "unrelated")
    schema = host / "schema.yaml"
    schema.write_text(original)
    plan = Plan(apply=apply)

    merge_namespaces(host, pack, plan)
    assert plan.run() == 0

    if not apply:
        assert schema.read_text() == original
        assert not (host / "wiki").exists()
        return
    expected = yaml.safe_load(original)
    for section, values in incoming.items():
        entry = expected.setdefault(section, {})
        entry.setdefault("namespaces", {}).update(values)
    assert yaml.safe_load(schema.read_text()) == expected
    assert (host / "wiki/incoming-a").is_dir()
    assert (host / "wiki/incoming-b").is_dir()
