#!/usr/bin/env python3
"""Release-mode ALLOWED-SKIP gate (okengine#204). Runs the offline unit suite and FAILS if any test
skipped for a reason that indicates a MISSING PYTHON DEPENDENCY — i.e. the release environment is
incomplete and the "green" suite silently omitted real coverage. Skips for a genuinely-environmental
reason (needs a live stack / docker / a tool the offline suite structurally can't have) are allowed.

This makes the guide's "confirm PASSED, not skipped" enforceable. Run it AFTER `make preflight`
(which ensures the required deps are installed, so a dependency-skip here means a test importorskips
something NOT on the required list — either add the dep, or add the reason to ALLOWED below).

    make test-release            # preflight-gated full suite with the skip policy enforced
    python scripts/check-test-skips.py [extra pytest args]
"""
import re
import subprocess
import sys

# A skip is FORBIDDEN iff its reason is a MISSING-PYTHON-DEPENDENCY signal — i.e. the release env is
# incomplete. Everything else (needs a live stack / docker / a conditional the offline suite can't
# satisfy — "Dockerfile does not download IWE", "generated artifact absent", …) is a legitimate
# environmental skip. This is the exact string pytest.importorskip / a `requires <pkg>` skip emit;
# invert-matching it (rather than maintaining an environmental allowlist) is robust to new
# environmental skips being added.
FORBIDDEN_RE = re.compile(r"could not import|No module named|requires .{0,40}(?:package|module|installed)",
                          re.I)

# pytest -rs summary line: "SKIPPED [1] tests/foo.py:12: <reason>"
_SKIP_RE = re.compile(r"^SKIPPED \[\d+\] (\S+?):\d+: (.+)$", re.M)


def main() -> int:
    cmd = [sys.executable, "-m", "pytest", "tests/", "--import-mode=importlib", "-p", "no:warnings",
           "-rs", "-q", "--ignore=tests/test_post_deploy_verify.py", *sys.argv[1:]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = r.stdout
    sys.stdout.write(out[-6000:])
    if r.stderr.strip():
        sys.stderr.write(r.stderr[-1500:])

    # real test failures/errors are a hard fail regardless of the skip policy
    if r.returncode not in (0, 5):   # 5 = no tests collected
        if re.search(r"\bfailed\b|\berror", out):
            print("\nRELEASE SUITE: test failures/errors above — not a skip-policy pass.", file=sys.stderr)
            if not _forbidden(out):
                return 1

    bad = _forbidden(out)
    if bad:
        print("\nRELEASE SKIP-POLICY: ✗ missing-dependency skip(s) — the release env is incomplete "
              "(run `make preflight`, install the dep, or justify it in check-test-skips.py ALLOWED):",
              file=sys.stderr)
        for b in sorted(set(bad)):
            print("  ✗ " + b, file=sys.stderr)
        return 1
    print("\nrelease skip-policy: ✓ only environmental skips (no missing-dependency skips)")
    return 0 if r.returncode == 0 else r.returncode


def _forbidden(out: str):
    return [f"{loc}: {reason.strip()}" for loc, reason in _SKIP_RE.findall(out)
            if FORBIDDEN_RE.search(reason)]


if __name__ == "__main__":
    raise SystemExit(main())
