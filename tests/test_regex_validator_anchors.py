"""Whole-value regex validators must anchor with ``\\Z``, never a bare ``$`` (okengine#991).

In Python ``$`` also matches just before ONE trailing newline, so ``ID_RE.match("some-ext\\n")``
accepts an id that then escapes every exact-string comparison downstream (the extension
discovery duplicate-id rule keyed ``"some-ext\\n"`` apart from ``"some-ext"``). The class is any
``^...$`` pattern applied with ``re.match``/``re.search``/``.match`` to a VALUE (an id, version,
date, schedule sentinel) that came from YAML/JSON, where a ``|`` block scalar or ``"x\\n"`` can
carry the newline.

The detector below scans ``scripts/`` and every other first-party Python root (``src/``, ``tools/``,
``okengine-mcp/``, ``extensions/``, ``okengine-reader/``, ``okengine-cockpit/``; okengine#993) for
single-line (non-MULTILINE) ``^...$`` regex literals.
Every one must either end in ``\\Z`` or be listed in ``_LINE_MATCHERS`` — patterns that are only
ever applied to one already-split line, where ``$`` is the correct anchor (some callers keep the
line ending). Adding a new whole-value validator with ``$`` fails here; the fix is ``\\Z`` (or
``fullmatch``), not an allowlist entry.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOTS = tuple(REPO / name for name in (
    "scripts", "src", "tools", "okengine-mcp", "extensions", "okengine-reader", "okengine-cockpit"))

# (path relative to the repo, pattern) -> why `$` is right: the pattern is applied per line.
_LINE_MATCHERS = {
    ("scripts/cron/deepseek_policy.py", r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)$"):
        "applied to text.splitlines() entries",
    ("scripts/cron/relink_prose_sources.py", r"^(\s*)-\s+(.*\S)\s*$"): "one frontmatter line",
    ("scripts/cron/relink_prose_sources.py", r"^sources:\s*$"): "one frontmatter line",
    ("scripts/cron/repair_body_integrity.py", r"^##[ \t]+(.+?)[ \t]*$"): "one body line",
    ("scripts/cron/repair_broken_frontmatter.py", r"^[ \t]*$"): "one frontmatter line",
    ("scripts/cron/repair_broken_frontmatter.py", r"^\|+-{3,}[ \t]*$"): "one frontmatter line",
    ("scripts/cron/repair_broken_frontmatter.py",
     r"^(?P<value>[A-Za-z_][\w-]*:[ \t]+.+?)---[ \t]*(?P<newline>\n?)$"):
        "one keepends line; the newline group is explicit",
    ("scripts/cron/repair_broken_frontmatter.py", r"^-{1,4}(?P<token>[0-9a-f]{12})---4[ \t]*$"):
        "one frontmatter line",
    ("scripts/cron/repair_broken_frontmatter.py", r"^[ \t]*(\d+\|)?\|?-{2,}(\|-*)*\|?[ \t]*$"):
        "one frontmatter line",
    ("scripts/cron/repair_broken_frontmatter.py", r"^[A-Za-z_][\w-]*:[ \t]*\[[^\]]*$"):
        "one frontmatter line",
    ("scripts/cron/repair_broken_frontmatter.py", r"^\|?[ \t]*-{2,}[ \t-]*$"): "one frontmatter line",
    ("scripts/cron/repair_carried_provenance.py", r"^([A-Za-z_][A-Za-z0-9_.-]*):[ \t]+(\S.*?)[ \t]*$"):
        "one frontmatter line",
    ("scripts/cron/review_reconcile.py",
     r"^- (?P<date>\d{4}-\d{2}-\d{2}) \*\*(?P<path>[^*]+)\*\* — (?P<reason>.*)$"):
        "one queue line, rstripped",
    ("scripts/cron/source_backlink_drain.py", r"^sources:\s*\[\s*\]\s*$"): "one frontmatter line",
    ("scripts/cron/source_backlink_drain.py", r"^(sources:\s*\[)(.*)(\]\s*)$"): "one frontmatter line",
    ("scripts/normalize_publishers.py", r"^publisher:[ \t]*(?P<value>.*?)[ \t]*$"):
        "one frontmatter line, rstripped",
    ("scripts/reconcile_review_records.py", r"^- \d{4}-\d{2}-\d{2} \*\*([^*]+)\.md\*\* — (.*)$"):
        "one queue line from splitlines()",
    ("scripts/framework_backup.py", r"^\s*(HERMES_UID|HERMES_GID)\s*=\s*([0-9]+)\s*(?:#.*)?$"):
        "one .env line from splitlines()",
    ("src/okengine/cockpit_services/documents.py", r"^(#{1,6})\s+(.+?)\s*$"):
        "one body line from splitlines()",
    ("src/okengine/cockpit_services/documents.py", r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$"):
        "one body line from splitlines()",
    ("src/okengine/projection/projector.py", r"^#{1,6}\s+(.*?)\s*$"): "one body line from splitlines()",
    ("src/okengine/write_services/state.py", r"^source/[a-z0-9][a-z0-9._/-]+$"):
        "a DENY-list detector (integrity._fabricated_source_reject): `$` keeps a trailing newline "
        "matching, so a fabricated ref cannot slip past it",
    ("src/okengine/write_services/state.py", r"^(#{1,6})[ \t]+(.*?)[ \t]*$"):
        "one body line (patching's section walk)",
    ("extensions/okengine.messaging-synthesis/select_content_pegs.py", r"^(\w+):\s*(.*)$"):
        "one frontmatter line from splitlines()",
}

_MULTILINE = re.compile(r"\b(?:re\.)?(?:M|MULTILINE)\b")


def _flags_arg(call: ast.Call) -> ast.AST | None:
    index = 1 if call.func.attr == "compile" else 2
    if len(call.args) > index:
        return call.args[index]
    for keyword in call.keywords:
        if keyword.arg == "flags":
            return keyword.value
    return None


def _single_line_dollar_patterns(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id in {"re", "_re"}
                and node.func.attr in {"compile", "match", "search"} and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            continue
        pattern = node.args[0].value
        if not (pattern.startswith("^") and pattern.endswith("$") and not pattern.endswith("\\$")):
            continue
        flags = _flags_arg(node)
        if pattern.startswith("(?m)") or (flags is not None and _MULTILINE.search(ast.unparse(flags))):
            continue
        yield node.lineno, pattern


def _python_files():
    for root in SCAN_ROOTS:
        yield from sorted(root.rglob("*.py"))


def _findings():
    found = []
    for path in _python_files():
        rel = path.relative_to(REPO).as_posix()
        for lineno, pattern in _single_line_dollar_patterns(path):
            if (rel, pattern) not in _LINE_MATCHERS:
                found.append(f"{rel}:{lineno}: {pattern!r}")
    return found


def test_whole_value_validators_anchor_with_backslash_z():
    findings = _findings()
    assert findings == [], (
        "a `^...$` regex without MULTILINE accepts a trailing newline; anchor whole-value "
        "validators with \\Z (or use fullmatch). Line-only matchers go in _LINE_MATCHERS:\n  "
        + "\n  ".join(findings))


def test_line_matcher_allowlist_has_no_stale_entries():
    live = set()
    for path in _python_files():
        rel = path.relative_to(REPO).as_posix()
        live.update((rel, pattern) for _, pattern in _single_line_dollar_patterns(path))
    stale = sorted(set(_LINE_MATCHERS) - live)
    assert stale == [], f"_LINE_MATCHERS only shrinks; remove entries that no longer exist: {stale}"


def test_detector_rejects_a_bare_dollar_validator(tmp_path):
    """Negative fixture: the scan must flag the exact shape of the original defect."""
    sample = tmp_path / "sample.py"
    sample.write_text('import re\nID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,126}[a-z0-9]$")\n'
                      'OK_RE = re.compile(r"^[a-z]+\\Z")\nLINES = re.compile(r"^# (.+)$", re.M)\n'
                      'FLAGGED = re.search(r"^x$", "x", flags=re.MULTILINE)\n',
                      encoding="utf-8")
    assert [p for _, p in _single_line_dollar_patterns(sample)] == [
        r"^[a-z0-9][a-z0-9.-]{1,126}[a-z0-9]$"]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("rel, attr, valid", [
    ("scripts/extension_manifest.py", "ID_RE", "memory-sync"),
    ("scripts/extension_manifest.py", "_SEMVER_RE", "1.2.3"),
    ("scripts/cron/source_connector.py", "ID_RE", "memory-sync"),
    ("scripts/cron/source_connector.py", "ENV_RE", "API_KEY"),
    ("scripts/application_profiles.py", "ID_RE", "analyst"),
    ("scripts/application_profiles.py", "ROLE_RE", "analyst_role"),
    ("scripts/application_profiles.py", "VERSION_RE", "1.2.3"),
    ("scripts/pack_currency.py", "_VERSION", "1.2.3"),
    ("scripts/cron/lane_expect.py", "_WINDOW", "24h"),
    ("scripts/cron/lane_expect.py", "_NAME", "lane-name"),
    ("scripts/cron_jitter.py", "_MORNING_RE", "@morning:30"),
    ("scripts/cron_jitter.py", "_SENTINEL_RE", "@jitter:daily"),
    # okengine#993: the same class outside scripts/ (src/okengine/write_services/state.py is baked).
    ("src/okengine/schema_exclusions.py", "_NAMESPACE", "entities"),
    ("src/okengine/write_services/state.py", "_WIKILINK_FULL", "[[concepts/x]]"),
    ("src/okengine/write_services/state.py", "_URL_SOURCE_ID", "sources:url-0123456789abcdef0123"),
    ("src/okengine/operations/framework.py", "_NAME", "nightly-sync"),
    ("src/okengine/operations/framework.py", "_RUN_ID", "run:2026-10-05"),
    ("src/okengine/operations/framework.py", "_LOCK", "vault/entities"),
    ("src/okengine/projection/projector.py", "_PAGED_INDEX", "index-p2.md"),
    ("src/okengine/cockpit_services/state.py", "_DATED_SERIES_RE", "daily-brief-2026-10-05"),
    ("extensions/okengine.inquiry/inquiry_lib.py", "_SLUG", "edge-device-exploitation"),
    ("extensions/okengine.inquiry/inquiry_lib.py", "_KEY", "who-attacks"),
    ("extensions/okengine.inquiry/inquiry_lib.py", "_FIELD_REF", "concepts/s/supply-chain"),
    ("extensions/okengine.assessments/adversarial_evidence.py", "_UTC_SECOND",
     "2026-10-05T12:00:00Z"),
    ("extensions/okengine.lacuna/select_lacuna_field.py", "_STRUCTURAL_PAGE", "_review-queue.md"),
])
def test_validator_accepts_the_value_but_not_a_trailing_newline(rel, attr, valid):
    pattern = getattr(_load(f"anchor_{Path(rel).stem}", rel), attr)
    assert pattern.match(valid), f"{rel}:{attr} must accept {valid!r}"
    assert pattern.match(valid + "\n") is None, f"{rel}:{attr} accepted {valid + chr(10)!r}"


def test_the_singular_source_deny_list_still_matches_a_trailing_newline():
    """A DENY-list detector must not loosen: `[[source/cisa/x\n]]` keeps its newline after
    integrity's `strip("[]")`, and `\\Z` would let that fabricated ref through (QA of !1264)."""
    pattern = _load("anchor_ws_state", "src/okengine/write_services/state.py")._SINGULAR_SOURCE_REF
    assert pattern.match("source/cisa/advisory")
    assert pattern.match("source/cisa/advisory\n")
