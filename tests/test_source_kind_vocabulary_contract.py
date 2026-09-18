"""Engine lanes may only branch on `source_kind` values the base schema declares.

`repair_raw_backlinks` excludes `reference-data` by construction — "API-imported reference rows
that have no raw capture at all, so a missing `raw:` there is correct rather than a gap" — and
no engine schema declared that value. The engine was reading a vocabulary it never published.

That is two defects wearing one coat. On the engine side it is a dead field: nothing guarantees
the value the lane keys on is one any producer is allowed to write. On the corpus side it is a
conformance violation, because a pack that re-declares `source_kind` CLOSED (several do — the
base ships it `extensible: true`, and re-declaring without the flag closes it) makes every such
page a value the enforced write path would reject. One live vault held 6,481 of them, written by
`no_agent` importers that bypass the write server. okengine#595.

An exclusion set is not tuning: a value missing from it silently changes which pages a lane
processes. So this contract is strict — every branched-on value must be declared, no waivers.
"""
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
BASE_SCHEMA = REPO / "config" / "base-schema.yaml"
pytestmark = pytest.mark.skipif(not BASE_SCHEMA.is_file(), reason="base schema absent")


def declared_source_kinds() -> set[str]:
    schema = yaml.safe_load(BASE_SCHEMA.read_text(encoding="utf-8")) or {}
    values = (schema.get("enums") or {}).get("source_kind")
    assert isinstance(values, list) and values, "the base schema must declare a source_kind enum"
    return {str(v) for v in values}


def test_the_base_vocabulary_declares_reference_data():
    """The value 6,481 live pages carry and an engine lane already branches on."""
    assert "reference-data" in declared_source_kinds()


def test_the_base_vocabulary_keeps_the_core_kinds():
    """Removing one would silently invalidate every page already carrying it."""
    assert {"paper", "post", "release", "news", "report"} <= declared_source_kinds()


def test_repair_raw_backlinks_only_excludes_declared_kinds():
    """The lane's exclusion set IS the contract this file exists to enforce."""
    module = REPO / "scripts" / "cron" / "repair_raw_backlinks.py"
    if not module.is_file():
        pytest.skip("repair_raw_backlinks absent")
    namespace: dict = {}
    for line in module.read_text(encoding="utf-8").splitlines():
        if line.startswith("EXCLUDED_KINDS"):
            exec(line, namespace)                       # noqa: S102 — a literal set from our own repo
            break
    excluded = namespace.get("EXCLUDED_KINDS")
    assert isinstance(excluded, set) and excluded, "EXCLUDED_KINDS moved or stopped being a literal"
    undeclared = excluded - declared_source_kinds()
    assert not undeclared, (
        f"repair_raw_backlinks branches on {sorted(undeclared)}, which the base schema does not "
        f"declare — a pack that closes the enum makes every such page unwritable"
    )


def test_the_contract_would_catch_an_undeclared_exclusion():
    """A green check that cannot go red is not a check."""
    declared = declared_source_kinds()
    assert {"reference-data", "not-a-declared-kind"} - declared == {"not-a-declared-kind"}


def _source_decay():
    import importlib.util
    import sys
    path = REPO / "scripts" / "cron" / "source_decay.py"
    if not path.is_file():
        pytest.skip("source_decay absent")
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("source_decay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_decay_table_covers_the_declared_spellings_of_the_kinds_it_tunes():
    """Tuning keyed on a vocabulary nobody declares is tuning that never fires.

    Four of this table's six original keys were values the base schema has never declared. On a
    vault using the declared spellings, only `report` matched and everything else silently took
    the default — the tuning was inert on 1,739 pages of one live vault alone.
    """
    decay = _source_decay()
    for declared, synonym in (("news", "article"), ("post", "blog")):
        assert declared in decay.HALF_LIVES, (
            f"{declared!r} is a base-declared source_kind whose synonym {synonym!r} is tuned "
            f"here — a vault using the declared spelling gets the default instead"
        )
        assert decay.HALF_LIVES[declared] == decay.HALF_LIVES[synonym], (
            f"{declared!r} and {synonym!r} are the same kind spelled two ways; reading one "
            f"number from two places is how they drift apart"
        )


def test_an_undeclared_or_absent_kind_still_falls_back_rather_than_raising():
    decay = _source_decay()
    assert decay.half_life_for("not-a-kind") == decay.DEFAULT_HALF_LIFE
    assert decay.half_life_for(None) == decay.DEFAULT_HALF_LIFE
    assert decay.half_life_for("NEWS") == decay.HALF_LIVES["news"], "lookup is case-insensitive"


# --- okengine#595: two fields must not share the name `source_kind` -------------------------------

def _module(relpath: str):
    import importlib.util
    import sys
    path = REPO / relpath
    if not path.is_file():
        pytest.skip(f"{relpath} absent")
    sys.path.insert(0, str(path.parent))
    name = path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_the_ledger_records_origin_class_not_source_kind(tmp_path):
    """One name, two unrelated vocabularies, and nothing in either direction could complain:
    the ledger's primary/secondary/unknown against the pages' paper/post/news/report."""
    ledger = _module("scripts/cron/collection_ledger.py")
    ledger.register_sources(tmp_path, [
        {"source_id": "a", "connector_id": "test", "label": "A", "origin_class": "primary"},
    ])
    row = ledger.load_sources(tmp_path)[0]
    assert row["origin_class"] == "primary"
    assert "source_kind" not in row, "the colliding name must not survive in a written row"


def test_a_v1_ledger_row_is_read_forward_rather_than_reset(tmp_path):
    """A rename that silently resets everyone's data is not a migration."""
    ledger = _module("scripts/cron/collection_ledger.py")
    ledger.register_sources(tmp_path, [
        {"source_id": "a", "connector_id": "test", "label": "A", "source_kind": "secondary"},
    ])
    assert ledger.load_sources(tmp_path)[0]["origin_class"] == "secondary"
    assert ledger.origin_class_of({"source_kind": "primary"}) == "primary"
    assert ledger.origin_class_of({"origin_class": "primary", "source_kind": "secondary"}) \
        == "primary", "the new key wins when both are present"


@pytest.mark.parametrize("value", ["", None, "nonsense", "NEWS", 3])
def test_an_unrecognised_origin_class_falls_back_to_unknown(value):
    ledger = _module("scripts/cron/collection_ledger.py")
    assert ledger.origin_class_of({"origin_class": value}) == "unknown"


def test_the_opml_attribute_still_accepts_the_legacy_spelling(tmp_path, monkeypatch):
    """feeds.opml is operator-authored config in every pack; silently dropping a key operators
    already wrote is not a rename."""
    feeds = _module("scripts/cron/feed_fetch.py")
    opml = tmp_path / "feeds.opml"
    opml.write_text(
        "<opml><body>"
        '<outline title="Legacy" xmlUrl="https://a.test/rss" sourceKind="primary"/>'
        '<outline title="New" xmlUrl="https://b.test/rss" originClass="secondary"/>'
        "</body></opml>", encoding="utf-8")
    rows = {row["label"]: row["origin_class"] for row in feeds.load_opml_sources(opml)}
    assert rows == {"Legacy": "primary", "New": "secondary"}


def test_no_engine_lane_writes_the_ledger_vocabulary_into_the_page_field():
    """`primary`/`secondary` are origin classes, not kinds. A lane stamping one onto a page
    puts a value in `source_kind` that no schema declares and no consumer can bucket."""
    declared = declared_source_kinds()
    assert not ({"primary", "secondary"} & declared), (
        "the ledger's origin classes must never become page source_kind values"
    )
    for relpath in ("scripts/cron/feed_fetch.py", "scripts/cron/source_connector.py",
                    "scripts/cron/collection_ledger.py"):
        path = REPO / relpath
        if not path.is_file():
            continue
        body = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                         if not line.lstrip().startswith("#"))
        # A dict-literal KEY (`"source_kind":`) is a write. Reading the legacy spelling —
        # `o.get("source_kind")` from an operator's feeds.opml, or the ledger's own
        # read-forward constant — is the back-compat this rename deliberately keeps.
        assert '"source_kind":' not in body, (
            f"{relpath} still writes the colliding key — it means origin class here"
        )


def test_the_ledger_declares_the_migrated_schema_version(tmp_path):
    """The rename is a schema change, and a reader has to be able to tell which shape it holds.

    Nothing asserted the version, so the mutation gate could move it in either direction
    untouched: SCHEMA_VERSION 1 would claim a migrated ledger is pre-rename, 3 would claim a
    migration nobody wrote.
    """
    import json
    ledger = _module("scripts/cron/collection_ledger.py")
    assert ledger.SCHEMA_VERSION == 2
    ledger.register_sources(tmp_path, [
        {"source_id": "a", "connector_id": "test", "label": "A", "origin_class": "primary"},
    ])
    doc = json.loads((tmp_path / "sources.json").read_text(encoding="utf-8"))
    assert doc["schema_version"] == 2, "a written ledger must declare the shape it is in"
