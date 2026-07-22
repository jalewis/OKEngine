"""Direct-LLM-call discipline: scripts/extensions must use llm_lib, never raw HTTP.

The policy (reasoning off by default for direct model calls) is only real if it's enforced —
a decision that lives in one client plus a convention is a blind spot (the gateway lanes were
protected by the Hermes provider profiles while direct scripts truncated on qwen thinking).
This gate fails the build when engine/pack automation code makes a raw chat-completions call
outside `llm_lib.py` (vendored copies keep that filename — the allowlist is by name)."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Endpoint-path signatures of a raw model call. Embeddings endpoints are exempt (no thinking).
_RAW_CALL = re.compile(r"chat/completions|/api/chat\b|/api/generate\b")
# Automation code the gate governs. UIs (reader/cockpit) relay to the Hermes gateway, which
# applies provider profiles server-side — different layer, not governed here.
_SCOPES = ("scripts", "extensions", "tools")


def test_no_raw_model_calls_outside_llm_lib():
    offenders = []
    for scope in _SCOPES:
        root = REPO / scope
        if not root.is_dir():
            continue
        for p in root.rglob("*.py"):
            if p.name == "llm_lib.py" or "__pycache__" in p.parts:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if _RAW_CALL.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:80]}")
    assert not offenders, (
        "Raw model-endpoint call(s) outside llm_lib — use scripts/cron/llm_lib.py "
        "(reasoning-off policy baked in; see docs/common-issues.md):\n  "
        + "\n  ".join(offenders))


def test_vendored_llm_lib_copies_are_byte_identical():  # invariant-audit #52, #351
    """The reasoning-off/truncation policy lives in every vendored llm_lib.py copy, and the sibling
    gate (test_no_raw_model_calls_outside_llm_lib) exempts them by FILENAME under _SCOPES. A hardcoded
    copy list meant a NEW extension vendoring llm_lib.py at a fresh path was auto-exempt from the
    raw-call gate yet NOT pinned here — free to diverge (e.g. re-enable qwen thinking) and pass both
    gates (invariant-audit #351). Discover copies the SAME way the exemption scopes them (rglob under
    _SCOPES) so the two gates govern the identical file set, and pin all to the canonical policy."""
    canonical = REPO / "scripts" / "cron" / "llm_lib.py"
    copies = sorted({p for scope in _SCOPES for p in (REPO / scope).rglob("llm_lib.py")
                     if "__pycache__" not in p.parts})
    assert canonical in copies and len(copies) >= 2, (
        f"expected the canonical scripts/cron/llm_lib.py + vendored copies, found {copies}")
    ref = canonical.read_bytes()
    diverged = [p for p in copies if p.read_bytes() != ref]
    assert not diverged, (
        "vendored llm_lib.py copies have DIVERGED from the canonical scripts/cron/llm_lib.py "
        "(a fix landed in one but not the others, or a NEW copy was added unpinned):\n  "
        + "\n  ".join(f"{p.relative_to(REPO)} ({len(p.read_bytes())} bytes)" for p in diverged))
