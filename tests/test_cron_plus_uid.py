"""Regression guard for okengine#136: the cron-plus host wrapper must auto-detect the
gateway's uid (the /opt/data owner) instead of defaulting to 10000, else a pack that
overrides HERMES_UID gets EACCES on /opt/data when running cron-plus.sh."""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SH = REPO / "scripts" / "cron-plus.sh"


def test_cron_plus_does_not_hardcode_default_uid():
    txt = SH.read_text(encoding="utf-8")
    # an explicit value still wins; the UP-FRONT default is empty (the bug was :-10000 here)
    assert 'HERMES_UID="${HERMES_UID:-}"' in txt
    # it auto-detects from the running gateway's /opt/data owner...
    detect = txt.index("stat -c %u /opt/data")
    # ...BEFORE applying the 10000 fallback (detection precedes the fallback, not vice-versa)
    fallback = txt.index('HERMES_UID="${HERMES_UID:-10000}"')
    assert detect < fallback, "the 10000 fallback must come AFTER container detection"
    # and the exec runs as the resolved uid
    assert 'docker exec -i -u "$HERMES_UID"' in txt
