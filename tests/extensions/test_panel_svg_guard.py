"""invariant-audit B6.1 — a malformed agent-authored `panel:` must SKIP, never crash the lane.

`panel_svg.render_panel_svg` is fed a `two-axis` panel that an AGENT wrote into a page's frontmatter,
so its shape is untrusted: a node's x/y can be a non-numeric string, a node can be a scalar instead
of a dict, an edge can reference a missing slug or lack coordinates, a band can be a scalar. Any of
those used to raise (float("high"), "oops".get(...), a["x"] KeyError) and abort the whole panel-svg
REFRESH lane mid-sweep — one bad page taking out every page after it. The fix coerces coordinates
safely (`_num`), filters the three collections to well-shaped entries, and wraps the lane-facing
`svg_block` in a guard so an unforeseen shape drops ONE page's panel instead of the lane.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.viz"


def _load():
    spec = importlib.util.spec_from_file_location("panel_svg", EXT / "panel_svg.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["panel_svg"] = m
    spec.loader.exec_module(m)
    return m


MALFORMED = {
    "non-numeric x/y": {"kind": "two-axis", "nodes": [{"slug": "a", "x": "high", "y": "low", "label": "A"}]},
    "scalar nodes mixed in": {"kind": "two-axis", "nodes": ["oops", 42, {"slug": "b", "x": 0.5, "y": 0.5}]},
    "node missing coords + self edge": {"kind": "two-axis", "nodes": [{"slug": "c", "label": "C"}], "edges": [["c", "c"]]},
    "scalar band": {"kind": "two-axis", "x_bands": ["bad", {"from": 0.3, "to": 0.6, "label": "mid"}], "nodes": []},
    "edge to missing slug + short edge": {"kind": "two-axis", "nodes": [{"slug": "d", "x": 0.2, "y": 0.2}], "edges": [["d", "zzz"], ["nope"]]},
    "nodes not a list": {"kind": "two-axis", "nodes": "garbage"},
    "band non-numeric from": {"kind": "two-axis", "x_bands": [{"from": "lo", "to": "hi", "label": "x"}], "nodes": []},
    "edges not a list": {"kind": "two-axis", "nodes": [{"slug": "e", "x": 0.1, "y": 0.1}], "edges": "nope"},
}


def test_malformed_panels_never_crash_the_lane():
    P = _load()
    for name, panel in MALFORMED.items():
        # svg_block is the lane entry point; it must return None or a string, never raise
        result = P.svg_block(panel)
        assert result is None or isinstance(result, str), f"{name}: got {type(result)}"
        # and render_panel_svg itself must not raise on the guarded shapes
        rendered = P.render_panel_svg(panel)
        assert rendered is None or isinstance(rendered, str), f"{name}: render raised/returned {type(rendered)}"


def test_wellformed_panel_still_renders():
    P = _load()
    good = {
        "kind": "two-axis", "x_label": "impact", "y_label": "likelihood",
        "x_bands": [{"from": 0, "to": 0.5, "label": "low"}, {"from": 0.5, "to": 1, "label": "high"}],
        "nodes": [{"slug": "a", "x": 0.2, "y": 0.8, "label": "Alpha"},
                  {"slug": "b", "x": 0.7, "y": 0.3, "label": "Beta"}],
        "edges": [["a", "b"]],
    }
    out = P.svg_block(good)
    assert out and "<svg" in out and "Alpha" in out and "Beta" in out
    # the edge between the two well-formed nodes still draws (guard didn't over-filter valid data)
    assert "<line" in out


def test_non_two_axis_returns_none():
    P = _load()
    assert P.render_panel_svg({"kind": "bar-chart"}) is None
    assert P.render_panel_svg("not even a dict") is None


def test_panel_with_date_or_set_field_hashes_and_renders():  # invariant-audit B6.1 re-verify
    """The panel comes from yaml.safe_load of frontmatter, which turns a bare `as_of: 2026-07-10`
    into datetime.date and `!!set` into set — both non-JSON-serializable. panel_hash runs AFTER
    render (outside the render-only guard) and json.dumps'd the raw panel, so such a VALID panel used
    to raise TypeError and abort the whole refresh lane. It must now hash + render, not crash."""
    import datetime
    P = _load()
    panel = {
        "kind": "two-axis", "as_of": datetime.date(2026, 7, 10),
        "tags": {"a", "b"},                                   # a yaml !!set
        "nodes": [{"slug": "a", "x": 0.5, "y": 0.4, "label": "Node A"}],
    }
    # panel_hash must not raise on the date/set …
    h = P.panel_hash(panel)
    assert isinstance(h, str) and h
    # … and svg_block (which calls panel_hash after render) returns a real block, not None-from-crash
    out = P.svg_block(panel)
    assert out and "<svg" in out and "Node A" in out


def test_panel_hash_is_deterministic_for_set_fields():  # invariant-audit B6.1 re-verify
    """A !!set field must hash DETERMINISTICALLY. default=str(set) is PYTHONHASHSEED-dependent, so it
    would produce a different hash every process run and rewrite the page's SVG block on every refresh
    (perpetual churn). Two module loads = two processes' worth of set construction; the hash must agree."""
    import subprocess, sys as _sys
    prog = (
        "import importlib.util, sys;"
        f"spec=importlib.util.spec_from_file_location('p', {str(EXT / 'panel_svg.py')!r});"
        "m=importlib.util.module_from_spec(spec); sys.modules['p']=m; spec.loader.exec_module(m);"
        "print(m.panel_hash({'kind':'two-axis','tags':{'alpha','bravo','charlie','delta'},"
        "'nodes':[{'slug':'a','x':0.1,'y':0.2}]}))"
    )
    # run in fresh processes with DIFFERENT hash seeds — a str(set) hash would differ between them
    h1 = subprocess.run([_sys.executable, "-c", prog], capture_output=True, text=True,
                        env={"PYTHONHASHSEED": "1"}).stdout.strip()
    h2 = subprocess.run([_sys.executable, "-c", prog], capture_output=True, text=True,
                        env={"PYTHONHASHSEED": "2"}).stdout.strip()
    assert h1 and h1 == h2, f"panel_hash non-deterministic across hash seeds: {h1!r} != {h2!r}"
