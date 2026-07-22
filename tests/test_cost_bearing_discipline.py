"""cost_bearing discipline: a no_agent operation whose entrypoint script spends model budget
(imports/calls llm_lib) MUST declare `cost_bearing: true`, so budget_guard can pause it when over
budget. The flag is a self-declared manifest convention (extension_manifest.py) with no validator
linking it to actual llm_lib usage — a forgotten flag makes a paid no_agent lane look free and burn
tokens unpausably (invariant-audit #36 / #351). This gate is that missing link: it derives the
required flag from the script the op actually runs, so the two can't drift.

Scope: first-party extensions (extensions/okengine.*). A no_agent op = a script entrypoint with NO
`prompt`/`prompt_file` (a prompted op is an AGENT op, inherently budgeted via the agent). Sidecar
image entrypoints run their own container, not a vault-side script, so they're out of scope.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
EXT_ROOT = REPO / "extensions"

# An llm_lib import/use inside the script = the op spends model budget. llm_lib.py itself (and its
# vendored copies) ARE the library, not a caller — never flag those.
_USES_LLM_LIB = re.compile(r"^\s*(?:from\s+llm_lib\b|import\s+llm_lib\b)|(?<![\w.])llm_lib\.", re.M)


def _operations(manifest: dict):
    """Yield (op_name, op_dict) for both the singular `operation:` and plural `operations:` forms."""
    op = manifest.get("operation")
    if isinstance(op, dict):
        yield manifest.get("id", "operation"), op
    ops = manifest.get("operations")
    if isinstance(ops, dict):
        for name, o in ops.items():
            if isinstance(o, dict):
                yield name, o


def _script_name(entrypoint):
    """The script filename from an entrypoint that is either a bare string or a {script: ...} dict.
    Returns None for an image entrypoint (sidecar) or a missing/odd shape."""
    if isinstance(entrypoint, str) and entrypoint.strip():
        return entrypoint.strip()
    if isinstance(entrypoint, dict) and isinstance(entrypoint.get("script"), str):
        return entrypoint["script"].strip()
    return None


def test_no_agent_llm_lib_ops_declare_cost_bearing():
    if not EXT_ROOT.is_dir():
        pytest.skip("no extensions/ dir")
    offenders = []
    checked = 0
    for man_path in sorted(EXT_ROOT.glob("*/extension.yaml")):
        ext_dir = man_path.parent
        try:
            manifest = yaml.safe_load(man_path.read_text(encoding="utf-8")) or {}
        except Exception as e:  # a parse fault is a different gate's job
            continue
        for op_name, op in _operations(manifest):
            # a prompted op is an AGENT op (budgeted via the agent), not the no_agent class this guards
            if op.get("prompt") or op.get("prompt_file"):
                continue
            script = _script_name(op.get("entrypoint"))
            if not script:
                continue
            spath = ext_dir / script
            if not spath.is_file():
                continue
            checked += 1
            if _USES_LLM_LIB.search(spath.read_text(encoding="utf-8", errors="replace")) \
                    and not op.get("cost_bearing"):
                offenders.append(
                    f"{ext_dir.name}:{op_name} -> {script} imports/calls llm_lib but the op does NOT "
                    f"declare cost_bearing: true — budget_guard treats it as free, so it burns paid "
                    f"tokens unpausably")
    assert checked, "no no_agent script entrypoints scanned — the gate resolved nothing (path drift?)"
    assert not offenders, (
        "no_agent op(s) that spend model budget without cost_bearing: true (invariant-audit #36):\n  "
        + "\n  ".join(offenders))
