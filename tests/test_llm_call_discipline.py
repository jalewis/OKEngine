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


def test_vendored_llm_lib_copies_are_byte_identical():  # invariant-audit #52
    """The reasoning-off/truncation policy lives in THREE vendored llm_lib.py copies, and this gate
    exempts them by FILENAME — so a policy fix to one and not the others (or a stale copy) passes CI
    silently. Pin them byte-identical: a divergence is caught here, at the exact seam the by-name
    allowlist leaves open."""
    copies = [
        REPO / "scripts" / "cron" / "llm_lib.py",
        REPO / "extensions" / "okengine.relevance-gate" / "llm_lib.py",
        REPO / "extensions" / "okengine.viz" / "llm_lib.py",
    ]
    present = [p for p in copies if p.is_file()]
    assert len(present) >= 2, f"expected multiple vendored llm_lib.py copies, found {present}"
    blobs = {p.read_bytes() for p in present}
    assert len(blobs) == 1, (
        "vendored llm_lib.py copies have DIVERGED — a policy fix landed in one but not the others:\n  "
        + "\n  ".join(f"{p} ({len(p.read_bytes())} bytes)" for p in present))
