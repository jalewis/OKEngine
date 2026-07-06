"""Both pack-scaffold front-ends must share the skeleton's token vocabulary (invariant-audit #4).

The skeleton (templates/pack/skeleton) is rendered by TWO front-ends — scripts/framework_init.py
(the CLI, `framework init`) and templates/pack/new-pack.sh (the bash quickstart). Both end with a
leftover-token guard that aborts if any {{UPPER_SNAKE}} survives. When cockpit added
{{COCKPIT_PORT}} to the skeleton, framework_init got the token but new-pack.sh did NOT — so
new-pack.sh aborted on EVERY invocation, silent because no test exercised it. This locks the two
vocabularies to the skeleton so a new token can't land in one front-end but not the other.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKELETON = REPO / "templates" / "pack" / "skeleton"
NEWPACK = REPO / "templates" / "pack" / "new-pack.sh"
INIT = REPO / "scripts" / "framework_init.py"

_TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


def _skeleton_tokens() -> set:
    toks = set()
    for p in SKELETON.rglob("*"):
        toks |= set(_TOKEN.findall(p.name))          # templated filenames, e.g. {{PACK_UNDERSCORE}}_*.py
        if p.is_file():
            try:
                toks |= set(_TOKEN.findall(p.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                pass
    return toks


def test_skeleton_has_tokens():
    assert _skeleton_tokens(), "no {{TOKEN}}s found under the skeleton — test wiring is broken"


def test_new_pack_sh_substitutes_every_skeleton_token():
    """#4: new-pack.sh's repl dict must map every skeleton token, or its leftover-token guard
    aborts the scaffold (as it did for {{COCKPIT_PORT}})."""
    np = NEWPACK.read_text()
    missing = [t for t in _skeleton_tokens() if ("{{" + t + "}}") not in np]
    assert not missing, f"new-pack.sh does not substitute skeleton token(s): {sorted(missing)}"


def test_framework_init_supplies_every_skeleton_token():
    fi = INIT.read_text()
    missing = [t for t in _skeleton_tokens() if ('"' + t + '"') not in fi]
    assert not missing, f"framework_init.py _tokens is missing skeleton token(s): {sorted(missing)}"
