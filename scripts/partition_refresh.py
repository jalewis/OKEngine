"""Partitioning changes on `install-domain --refresh` (okengine#915).

A refresh is otherwise runtime-only, so an installed composed pack whose namespace partitioning
changed upstream could never be refreshed again: preflight read the drifted declaration as a
foreign namespace and the refresh corridor merged no schema that could close the gap.

This module lets a refresh carry exactly one kind of schema change -- the partitioning of a
namespace the pack is recorded as owning -- and only after proving the change moves no existing
page. Moving pages is a reshelve operation with link rewrites and collision handling; a refresh
never does it. The proof uses okf_migrate's own placement rule (`drain_target`) evaluated under
the OLD (effective, as the drain reads it today) and the NEW (pack-declared) config, so the answer
is the one the reshelve drain would give, never a hand-built path.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml
from yaml import YAMLError

_HERE = Path(__file__).resolve().parent
EXAMPLE_LIMIT = 5


def _okf_migrate():
    spec = importlib.util.spec_from_file_location("okf_migrate", _HERE / "cron" / "okf_migrate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


def _namespaces(schema: dict) -> dict:
    value = (schema.get("partitioning") or {}).get("namespaces") or {}
    return value if isinstance(value, dict) else {}


def moving_pages(host: Path, namespace: str, new_cfg: dict) -> tuple[int, list[tuple[str, str, str]]]:
    """(pages checked, [(page, seat under old, seat under new)]) for pages whose settled seat differs.

    The old seat is what the drain leaves the page at under the partitioning it enforces TODAY (the
    effective governing schema -- the composed artifact when present); the new seat is the same
    question asked of `new_cfg`. Equal seats for every page is the no-move proof."""
    okf = _okf_migrate()
    governing = okf._governing_schema(host, namespace)
    old_cfg = okf._partition_cfg(governing, namespace)
    canonical = set((governing.get("types") or {}).keys())
    # Validate the proposed strategy even for an empty namespace: an unknown strategy must never be
    # written just because no page happened to exercise it (okf_migrate raises ValueError).
    okf.drain_target(namespace, f"{namespace}/probe", "probe", {}, new_cfg, canonical)
    checked, moved = 0, []
    for cur, slug, fm in okf.placeable_pages(host, namespace):
        checked += 1
        old_seat = okf.drain_target(namespace, cur, slug, fm, old_cfg, canonical) or cur
        new_seat = okf.drain_target(namespace, cur, slug, fm, new_cfg, canonical) or cur
        if old_seat != new_seat:
            moved.append((cur, old_seat, new_seat))
    return checked, moved


def _last_scalar_end(node) -> int:
    while isinstance(node, (yaml.MappingNode, yaml.SequenceNode)) and node.value:
        node = node.value[-1][1] if isinstance(node, yaml.MappingNode) else node.value[-1]
    return node.end_mark.index


def replace_partition_entry(text: str, namespace: str, value: dict) -> str:
    """Rewrite `partitioning.namespaces.<namespace>` in place, leaving every other byte as-is.

    Live schemas are heavily commented, so a parse+dump round trip is not acceptable (see
    framework_install_domain._insert_under). The entry is re-rendered as one flow-style line at its
    original indent; the result is parsed back and must equal the prior document with only that
    entry changed, or nothing is returned but an error."""
    document = yaml.compose(text)
    section = next((v for k, v in document.value if k.value == "partitioning"), None)
    namespaces = next((v for k, v in section.value if k.value == "namespaces"), None)
    key, current = next((k, v) for k, v in namespaces.value if k.value == namespace)
    rendered = yaml.safe_dump(value, default_flow_style=True, sort_keys=False,
                              width=float("inf")).strip()
    if namespaces.flow_style:
        start, end = current.start_mark.index, current.end_mark.index
        new_text = text[:start] + rendered + text[end:]
    else:
        start = text[:key.start_mark.index].rfind("\n") + 1
        end = _last_scalar_end(current)
        if current.flow_style or not isinstance(current, yaml.MappingNode):
            end = current.end_mark.index
        new_text = (text[:start] + " " * key.start_mark.column + f"{namespace}: {rendered}"
                    + text[end:])
    before, after = yaml.safe_load(text), yaml.safe_load(new_text)
    before["partitioning"]["namespaces"][namespace] = value
    if after != before:
        raise ValueError(f"partitioning rewrite for '{namespace}' did not round-trip; "
                         "schema.yaml left unchanged")
    return new_text


def plan_owned_partitioning(host: Path, pack: Path, plan, *, owned: set[str],
                            legacy_owned: bool, pack_label: str) -> None:
    """Plan (or refuse) the partitioning change for each namespace this pack owns whose host
    declaration differs from the pack's. Ownership is the installed-domain manifest (or the legacy
    co-install marker); a namespace the pack does not own is never touched here."""
    declared = [str(n) for n in ((_load(pack / "pack.yaml").get("owns") or {}).get("namespaces") or [])]
    hschema = host / "schema.yaml"
    host_part = _namespaces(_load(hschema))
    pack_part = _namespaces(_load(pack / "schema.yaml"))
    for ns in declared:
        if ns not in host_part or ns not in pack_part or host_part[ns] == pack_part[ns]:
            continue
        if ns not in owned and not legacy_owned:
            plan.fail(f"namespace '{ns}' partitioning differs from {pack_label}'s but the namespace "
                      "is not recorded as owned by it; reconcile ownership explicitly")
            continue
        old, new = host_part[ns], pack_part[ns]
        try:
            checked, moved = moving_pages(host, ns, new)
        except ValueError as exc:
            plan.fail(f"namespace '{ns}': proposed partitioning {new} is not placeable ({exc})")
            continue
        if moved:
            examples = "; ".join(f"{cur} -> {seat}" for cur, _, seat in moved[:EXAMPLE_LIMIT])
            plan.fail(f"namespace '{ns}': partitioning change {old} -> {new} would MOVE "
                      f"{len(moved)} of {checked} page(s) (e.g. {examples}) — moving pages is a "
                      "reshelve operation, not a refresh; reshelve first or keep the old partitioning")
            continue

        def _do(ns=ns, new=new):
            hschema.write_text(replace_partition_entry(hschema.read_text(encoding="utf-8"), ns, new),
                               encoding="utf-8")
        plan.step(f"partitioning change for owned namespace '{ns}': {old} -> {new} "
                  f"(proof: {checked} page(s) checked, 0 would move)", _do)
