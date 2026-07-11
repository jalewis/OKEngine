"""Smoke e2e — rendered-DOM layer (playwright + system chrome).

Asserts on the VISUALLY rendered page in a real browser — the failure modes the HTTP/content layer
can't see: DOM/vertical ORDER of elements, and markup that only leaks once the SPA assembles the
page. Uses the system Chrome via channel="chrome" (no browser download).

Run via smoke-e2e.sh. In DEV mode this SKIPS cleanly if playwright or Chrome is unavailable — the
HTTP layer still gates. In RELEASE mode (SMOKE_REQUIRE_DOM=1) a missing playwright/Chrome or an
unreachable cockpit is a FAILURE, never a skip: the DOM layer is the whole reason smoke-e2e exists,
so a release must not report green with it silently omitted (issue okengine#204, gap 1).
"""
import os

import pytest

# Release mode: the DOM layer is MANDATORY — a missing dependency fails the run instead of skipping.
_REQUIRE_DOM = os.environ.get("SMOKE_REQUIRE_DOM") == "1"

if _REQUIRE_DOM:
    import playwright.sync_api  # noqa: F401  — hard ImportError (not a skip) if absent in release mode
else:
    pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

COCKPIT = os.environ.get("SMOKE_COCKPIT_URL", "http://127.0.0.1:9881")
_LAUNCH = {"channel": "chrome", "args": ["--no-sandbox", "--disable-gpu"]}


def _unavailable(reason: str):
    """Fail in release mode (the DOM layer must run), skip in dev mode."""
    if _REQUIRE_DOM:
        pytest.fail(f"SMOKE_REQUIRE_DOM=1 but the rendered-DOM layer could not run: {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def page():
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(**_LAUNCH)
    except Exception as e:  # chrome missing / launch failure
        _unavailable(f"cannot launch system chrome via playwright ({e})")
    pg = browser.new_page()
    try:
        pg.goto(COCKPIT + "/", wait_until="networkidle", timeout=15000)
    except Exception as e:
        browser.close(); pw.stop()
        _unavailable(f"smoke cockpit not reachable at {COCKPIT} ({e}) — run smoke-e2e.sh")
    yield pg
    browser.close()
    pw.stop()


def _open(page, path):
    page.evaluate(f"openPage({path!r})")
    page.wait_for_selector("#ov-content", state="visible", timeout=10000)


def test_actor_body_leads_fact_panel(page):
    """The 'scattered spider' fix: on a profiled actor page the prose body must render ABOVE the
    fact panel. The panel carries the same fields either way, so only DOM/vertical order catches a
    regression — a content probe sees both present and passes."""
    _open(page, "entities/a/apt-smoke")
    page.wait_for_selector("#ov-content .meta-facts", timeout=10000)
    res = page.evaluate("""() => {
      const c = document.querySelector('#ov-content');
      const facts = c.querySelector('.meta-facts');
      const walk = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
      let body=null, n;
      while (n = walk.nextNode()) { if (n.textContent.includes('SMOKE_BODY_SENTINEL')) { body = n.parentElement; break; } }
      if (!body || !facts) return {ok:false};
      return {ok:true,
              domBodyFirst: !!(body.compareDocumentPosition(facts) & Node.DOCUMENT_POSITION_FOLLOWING),
              bodyTop: body.getBoundingClientRect().top,
              factsTop: facts.getBoundingClientRect().top};
    }""")
    assert res["ok"], "body sentinel or fact panel not found in the rendered page"
    assert res["domBodyFirst"], "fact panel precedes the body in DOM order"
    assert res["bodyTop"] < res["factsTop"], "fact panel renders visually above the body"


def test_no_raw_markup_visible_on_actor_page(page):
    """Nothing the builder emits may show as visible text: no raw wl anchor markup, and NO wikilink
    (bare or backtick-wrapped) may appear as literal [[…]] once rendered. A genuine non-wikilink
    code span is the control that must survive verbatim."""
    _open(page, "entities/a/apt-smoke")
    page.wait_for_selector("#ov-content", timeout=10000)
    txt = page.eval_on_selector("#ov-content", "el => el.innerText")
    assert '<a class="wl"' not in txt, "raw wl anchor markup visible as text"
    assert "[[entities/" not in txt, "a wikilink is shown as literal [[…]] in the rendered page"
    # the genuine inline-code span is intentionally literal and MUST survive verbatim
    assert "LITERAL_CODE_KEPT_x7" in txt, "genuine inline-code span was altered/dropped"
