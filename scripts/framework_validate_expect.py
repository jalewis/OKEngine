"""The `expect:` gate (okengine#890): every lane states what "working" means.

Engine lanes (config/engine-crons.json) and first-party extension operations
(extensions/*/extension.yaml + crons/*.cron.json) must declare `expect:` or sit on
config/expect-allowlist.yaml with a reason. The allowlist only shrinks: an entry for a lane that
no longer exists, or that now declares `expect:`, is an error, and its size is pinned by
`max_entries` (tests/test_lane_expect.py ratchets it against the merge base).

Pack domain crons and pack/operator extension operations are validated per pack by
`framework validate <pack>`: a structurally invalid `expect:` FAILs; a missing one WARNs
("undeclared") until pack maintainers have migrated (#891) — failing every pack on engine
upgrade would break deployments the engine does not own.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "config" / "expect-allowlist.yaml"


def lane_expect():
    """The shared validator, by a plain import from scripts/cron (the staged-lib convention):
    a spec_from_file_location load here would be caught by tests that fake importlib globally."""
    cron = str(ROOT / "scripts" / "cron")
    if cron not in sys.path:
        sys.path.insert(0, cron)
    import lane_expect as module
    return module


def _extension_lanes(ext_root: Path, source: str) -> Iterator[tuple[str, str, dict]]:
    """(job name, source, op) for every operation of every extension under ext_root."""
    import yaml
    from yaml import YAMLError
    sys.path.insert(0, str(ROOT / "scripts"))
    import extension_compose
    for manifest in sorted(ext_root.glob("*/extension.yaml")):  # glob-ok: one level of extension dirs
        try:
            m = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except (OSError, YAMLError):
            continue  # malformed manifests FAIL in the extension checks; not this gate's finding
        if m.get("kind") != "operation":
            continue
        ops, err = extension_compose._collect_ops(m, manifest.parent)
        ext_id = str(m.get("id") or manifest.parent.name)
        for op_name, op in ([] if err else ops):
            name = ext_id if op_name is None else f"{ext_id}:{op_name}"
            yield name, f"{source} {manifest.parent.name}", op


def engine_lanes(root: Path = ROOT) -> list[tuple[str, str, dict]]:
    lanes = [(str(j.get("name")), "config/engine-crons.json", j)
             for j in json.loads((root / "config" / "engine-crons.json").read_text(encoding="utf-8"))
             if isinstance(j, dict) and j.get("name")]
    lanes.extend(_extension_lanes(root / "extensions", "extension"))
    return lanes


def check_engine(root: Path = ROOT, allowlist_path: Path | None = None) -> tuple[list[str], int]:
    """(errors, allowlisted-undeclared count) for the engine's own lanes."""
    le = lane_expect()
    allowlist, cap, errors = le.load_allowlist(allowlist_path or root / "config" / "expect-allowlist.yaml")
    lanes = engine_lanes(root)
    names = {name for name, _s, _l in lanes}
    undeclared = 0
    for name, source, lane in lanes:
        errs, allowed = le.lane_findings(name, source, lane, allowlist)
        errors.extend(errs)
        undeclared += allowed
        if name in allowlist and isinstance(lane, dict) and lane.get("expect") is not None:
            errors.append(f"expect-allowlist: {name!r} now declares expect: — remove its entry "
                          "(the allowlist only shrinks)")
    errors.extend(f"expect-allowlist: {name!r} is not a lane any more — remove its entry"
                  for name in sorted(set(allowlist) - names))
    if cap is not None and cap != len(allowlist):
        errors.append(f"expect-allowlist: max_entries is {cap} but {len(allowlist)} lane(s) are "
                      "listed — lower max_entries to the count (it may never rise)")
    return errors, undeclared


_ENGINE_RESULT: tuple[tuple[str, ...], int] | None = None


def _engine_result() -> tuple[tuple[str, ...], int]:
    """check_engine() once per process: the engine's own lanes cannot change mid-run, and a
    validate that re-scanned every extension manifest per pack doubled its runtime."""
    global _ENGINE_RESULT
    if _ENGINE_RESULT is None:
        errors, undeclared = check_engine()
        _ENGINE_RESULT = (tuple(errors), undeclared)
    return _ENGINE_RESULT


def check_pack(pack: Path, r: Any, domain_defs: list) -> None:
    """framework validate <pack>: engine lanes FAIL on error; pack lanes FAIL on a malformed
    expect and WARN when undeclared."""
    le = lane_expect()
    errors, undeclared = _engine_result()
    for error in errors:
        r.fail("lane expectations (engine)", error)
    if not errors:
        r.ok("lane expectations (engine)",
             f"engine + first-party extension lanes valid ({undeclared} allowlisted, undeclared)")
    pack_lanes = [(str(d.get("name")), "crons/domain-crons.json", d)
                  for d in domain_defs if isinstance(d, dict) and d.get("name")]
    for tier in (pack / "extensions", pack / ".okengine" / "extensions"):
        if tier.is_dir():
            pack_lanes.extend(_extension_lanes(tier, "pack extension"))
    missing: list[str] = []
    bad = False
    for name, source, lane in pack_lanes:
        errs, _ = le.lane_findings(name, source, lane, {})
        for err in errs:
            if "declares no `expect:`" in err:
                missing.append(name)
            else:
                bad = True
                r.fail("lane expectations (pack)", err)
    if missing:
        r.warn("lane expectations (pack)",
               f"{len(missing)} lane(s) undeclared: {', '.join(missing)} — add `expect:` "
               f"({le.DOC}); undeclared pack lanes will FAIL in a future release")
    elif not bad:
        r.ok("lane expectations (pack)", f"{len(pack_lanes)} pack lane(s) declare expect:")
