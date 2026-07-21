#!/usr/bin/env python3
"""Vault-wide render lint — maintain current-page evidence through the reader's actual render path
and assert the rendered OUTPUT is clean.

The unit suite and the smoke harness assert on IDEALIZED fixtures; the bugs that reach users live in
REAL data — a page whose rendered HTML leaks builder markup, an unrendered wikilink showing as
literal `[[…]]`, a broken embed. Every recent user-visible render bug (HTML-in-the-UI, backtick
wikilinks, source-link leaks) was a render defect on stored content that a source/schema audit and
a clean-fixture test both pass. This sweeps the whole vault through the reader and flags the output.

It hits the reader's HTTP API (`/api/pages` to enumerate, `/api/page` to render), so it tests the
EXACT bytes a user sees — no local re-render that could diverge from production. Cron mode persists
evidence by path + reader `updated` revision: changed pages run first, then a bounded first-baseline
batch. A partial baseline is explicitly IN PROGRESS, never reported clean. Once complete, unchanged
pages retain their evidence and routine runs finish well inside the scheduler timeout.

Usage (on-demand):
    python scripts/render_lint.py --reader-url http://127.0.0.1:9400
    python scripts/render_lint.py --reader-url ... --limit 500        # stateless quick sample
    python scripts/render_lint.py --reader-url ... --no-state         # stateless full sweep
    python scripts/render_lint.py --reader-url ... --write-vault /opt/vault   # write the dashboard
Exit code: 0 = clean (within --max-offenders), 1 = offenders over the threshold, 2 = usage/reachability.

As a cron: point --reader-url at the in-network reader service and pass --write-vault; it writes
wiki/operational/render-lint.md and exits non-zero when the fleet regresses.
"""
import argparse
import datetime
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── the checks (pure; unit-tested) ───────────────────────────────────────────

_CODE_RE = re.compile(r"<(pre|code)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _visible_prose(html: str) -> str:
    """Rendered text with tags removed AND <code>/<pre> spans dropped — a literal `[[x]]` inside a
    code span is intentional (documenting a wikilink), so only PROSE residue is a real leak."""
    return _TAG_RE.sub(" ", _CODE_RE.sub(" ", html))


def lint_html(path: str, html: str) -> list[str]:
    """Return the violation codes for one rendered page (empty = clean).

    - wl-markup-leak : the builder's `<a class="wl">` anchor got HTML-escaped and shows as literal
                       text (the HTML-in-the-UI bug) — `&lt;a class="wl"` in the output.
    - literal-wikilink: a `[[…]]` survived unrendered in visible PROSE (a wikilink the renderer
                       failed to turn into a link or plain text).
    - backtick-wikilink: backtick residue around a wikilink in prose (the _uncode_wikilinks case
                       leaving `` `[[ `` / `]]` `` behind).
    - unresolved-embed: an `![[…]]` transclusion left unrendered in visible prose.
    """
    v: list[str] = []
    # Escaped anchor markup inside code/pre is intentional documentation (or a
    # Mermaid/source example), not reader-visible prose. Apply the same code
    # exclusion used by the literal-wikilink checks before classifying it.
    non_code_html = _CODE_RE.sub(" ", html)
    low = non_code_html.lower()
    if "&lt;a class=\"wl\"" in low or "&lt;a class=&quot;wl&quot;" in low:
        v.append("wl-markup-leak")
    prose = _TAG_RE.sub(" ", non_code_html)
    if "![[" in prose:
        v.append("unresolved-embed")
    if "`[[" in prose or "]]`" in prose:
        v.append("backtick-wikilink")
    # a bare [[…]] left in prose (exclude the embed/backtick cases already counted)
    if re.search(r"(?<!!)\[\[[^\]]+\]\]", prose):
        v.append("literal-wikilink")
    return v


# ── the crawl ────────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: float = 30):
    _validated_http_url(url)
    with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec B310
        return json.loads(r.read())


def _get_text(url: str, timeout: float = 30) -> str:
    _validated_http_url(url)
    with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec B310
        return r.read().decode("utf-8", "replace")


def _validated_http_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("reader URL must use http(s) and include a host")


def enumerate_page_records(reader_url: str) -> list[dict]:
    try:
        d = _get_json(f"{reader_url}/api/page-revisions", timeout=180)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        # Rolling-upgrade compatibility with an older reader container.
        d = _get_json(f"{reader_url}/api/pages", timeout=180)
    pages = d.get("pages", d) if isinstance(d, dict) else d
    return [{"path": p["path"], "revision": str(p.get("revision") or p.get("updated") or "")}
            for p in pages if isinstance(p, dict) and p.get("path")]


def enumerate_pages(reader_url: str) -> list[str]:
    """Compatibility helper used by the smoke test and on-demand callers."""
    return [p["path"] for p in enumerate_page_records(reader_url)]


def _lint_one(reader_url: str, path: str, retries: int = 2) -> tuple[str, list[str]]:
    # Retry a failed fetch before recording it: a single-worker reader under a concurrent sweep will
    # occasionally time out a request, which is a crawler artifact, NOT a page defect. Only a page
    # that fails EVERY attempt is a real fetch-error (a genuinely un-renderable page).
    url = f"{reader_url}/api/page?path={urllib.parse.quote(path)}"
    for attempt in range(retries + 1):
        try:
            d = _get_json(url, timeout=60)
            return path, lint_html(path, d.get("html", "") or "")
        except Exception:
            if attempt == retries:
                return path, ["fetch-error"]
    return path, ["fetch-error"]


def crawl(reader_url: str, paths: list[str], workers: int = 16) -> dict[str, list[str]]:
    offenders: dict[str, list[str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for path, viol in ex.map(lambda p: _lint_one(reader_url, p), paths):
            if viol:
                offenders[path] = viol
    # A single-worker reader can remain saturated for all of a pool worker's
    # inline retries. Once the pool has drained, retry only fetch failures in
    # series. This distinguishes a genuinely unrenderable page from crawler
    # contention without serializing the normal clean sweep.
    for path in [p for p, viol in offenders.items() if viol == ["fetch-error"]]:
        _path, viol = _lint_one(reader_url, path)
        if viol:
            offenders[path] = viol
        else:
            offenders.pop(path, None)
    return offenders


def default_workers(cron_mode: bool) -> int:
    """Bound production pressure on the supported single-worker reader.

    Interactive callers retain the historical 16-worker default. Stateful cron
    sweeps use four workers unless the operator explicitly tunes the deployment.
    """
    raw = (os.environ.get("RENDER_LINT_WORKERS") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 4 if cron_mode else 16


# ── report ───────────────────────────────────────────────────────────────────

def render_report(total: int, offenders: dict[str, list[str]], now: str,
                  checked: int | None = None, pending: int = 0,
                  last_full_sweep: str = "") -> str:
    by_code: dict[str, int] = {}
    for viol in offenders.values():
        for c in viol:
            by_code[c] = by_code.get(c, 0) + 1
    n = len(offenders)
    L = ["---", "type: dashboard", 'title: "Render lint"', f"updated: {now}", "---", "",
         f"# Render lint — {now}", "",
         f"Evidence covers **{checked if checked is not None else total:,}** of **{total:,}** current "
         f"pages through the reader's render path; **{pending:,}** pending this baseline cycle. "
         f"**{n:,}** page(s) with a rendered-output defect.", "",
         f"Last completed full sweep: **{last_full_sweep or 'not completed yet'}**.", ""]
    if by_code:
        L += ["| Violation | Pages |", "|---|---|"]
        L += [f"| {c} | {k} |" for c, k in sorted(by_code.items(), key=lambda x: -x[1])]
        L += ["", "## Offenders", "", "| Page | Violations |", "|---|---|"]
        for p in sorted(offenders)[:500]:
            L.append(f"| {p} | {', '.join(offenders[p])} |")
        if n > 500:
            L.append(f"| … | +{n - 500:,} more |")
    elif pending:
        L.append("_Baseline in progress — no defect has been observed in checked pages, but this is not yet a clean full-vault result._")
    else:
        L.append("_No rendered-output defects. Clean._")
    L.append("")
    return "\n".join(L)


# ── incremental evidence state ──────────────────────────────────────────────

# Bump whenever violation semantics change so cached clean/dirty evidence cannot
# survive a ruleset change. Version 2 excludes code/pre examples from markup leaks.
_STATE_VERSION = 2


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"version": _STATE_VERSION, "pages": {}}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("version") != _STATE_VERSION or not isinstance(d.get("pages"), dict):
            raise ValueError("unsupported state shape")
        return d
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"render-lint: ignoring corrupt state {path} ({e}); rebuilding baseline", file=sys.stderr)
        return {"version": _STATE_VERSION, "pages": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def plan_incremental(records: list[dict], state: dict, batch_size: int) -> tuple[list[str], dict[str, str]]:
    """Return paths requiring render and the current path→revision inventory.

    Changed previously-checked pages outrank unseen bootstrap pages so regressions
    are caught immediately even while a large first baseline is still filling.
    Deleted pages are removed by ``apply_incremental``.
    """
    current = {r["path"]: str(r.get("revision") or "") for r in records}
    cached = state.get("pages") if isinstance(state.get("pages"), dict) else {}
    # A fetch error is not durable evidence about page content; retry it on the
    # next bounded run even when the page revision is unchanged. Retries outrank
    # changed pages, which in turn outrank unseen baseline pages.
    retry = {p for p, rev in current.items()
             if isinstance(cached.get(p), dict)
             and cached[p].get("updated") == rev
             and "fetch-error" in (cached[p].get("violations") or [])}
    dirty = [p for p, rev in current.items()
             if p in retry or not isinstance(cached.get(p), dict) or cached[p].get("updated") != rev]
    dirty.sort(key=lambda p: (0 if p in retry else (1 if p in cached else 2), p))
    if batch_size > 0:
        dirty = dirty[:batch_size]
    return dirty, current


def apply_incremental(state: dict, current: dict[str, str],
                      results: dict[str, list[str]], now: str) -> tuple[dict[str, list[str]], int, int]:
    pages = state.setdefault("pages", {})
    for deleted in set(pages) - set(current):
        pages.pop(deleted, None)
    for path, violations in results.items():
        if path in current:
            pages[path] = {"updated": current[path], "violations": list(violations)}
    checked = sum(1 for p, rev in current.items()
                  if isinstance(pages.get(p), dict) and pages[p].get("updated") == rev)
    pending = len(current) - checked
    offenders = {p: list(v.get("violations") or []) for p, v in pages.items()
                 if p in current and v.get("updated") == current[p] and v.get("violations")}
    state["version"] = _STATE_VERSION
    state["updated_at"] = now
    if pending == 0:
        state["last_full_sweep"] = now
        state.pop("cycle_started_at", None)
    else:
        state.setdefault("cycle_started_at", now)
    return offenders, checked, pending


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Vault-wide render lint over the reader")
    # CRON MODE: invoked arg-less by cron-plus (no_agent). The reader defaults to the standard
    # in-network service (override with OKENGINE_READER_URL), and WIKI_PATH in the gateway env makes
    # it WRITE the dashboard automatically — like the other no_agent audit lanes. On-demand
    # (`make render-lint`, explicit --reader-url) prints only unless --write-vault.
    ap.add_argument("--reader-url", default=os.environ.get("OKENGINE_READER_URL")
                    or ("http://okengine-reader:9200" if os.environ.get("WIKI_PATH") else "http://127.0.0.1:9400"))
    ap.add_argument("--limit", type=int, default=0, help="cap pages crawled (0 = all)")
    ap.add_argument("--batch-size", type=int, default=None,
                    help="max new/changed pages per stateful run (default 10000 in cron mode; 0 = all)")
    ap.add_argument("--state", default=os.environ.get("RENDER_LINT_STATE", ""),
                    help="incremental evidence JSON (cron defaults under HERMES_HOME/state)")
    ap.add_argument("--no-state", action="store_true", help="force a stateless full/sample crawl")
    ap.add_argument("--workers", type=int, default=None,
                    help="concurrent page fetches (cron default 4; on-demand default 16)")
    ap.add_argument("--max-offenders", type=int, default=0, help="offenders tolerated before exit 1")
    ap.add_argument("--write-vault", default="", help="vault root; writes wiki/operational/render-lint.md")
    ap.add_argument("--now", default="", help="timestamp for the report (deployment stamps it)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.write_vault and os.environ.get("WIKI_PATH"):      # cron mode -> auto-write the dashboard
        wp = Path(os.environ["WIKI_PATH"])
        a.write_vault = str(wp.parent if wp.name == "wiki" else wp)

    try:
        records = enumerate_page_records(a.reader_url)
    except (urllib.error.URLError, OSError) as e:
        print(f"render-lint: reader unreachable at {a.reader_url} ({e})", file=sys.stderr)
        return 2
    now = a.now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cron_mode = bool(os.environ.get("WIKI_PATH")) and not a.limit and not a.no_state
    if a.workers is None:
        a.workers = default_workers(cron_mode)
    if not a.state and cron_mode:
        a.state = str(Path(os.environ.get("HERMES_HOME") or "/opt/data") / "state" / "render-lint.json")
    if a.batch_size is None:
        # Live measurements on the largest supported vault put 10k beyond the
        # scheduler timeout and 4k beyond it on the 67k-page deployment. One
        # 500 leaves margin even for the measured 67k-page deployment, where a
        # cold inventory plus 100 renders takes roughly three minutes.
        a.batch_size = int(os.environ.get("RENDER_LINT_BATCH_SIZE") or ("500" if cron_mode else "0"))

    checked = len(records)
    pending = 0
    last_full = ""
    if a.state and not a.no_state and not a.limit:
        state_path = Path(a.state)
        state = load_state(state_path)
        paths, current = plan_incremental(records, state, a.batch_size)
        run_offenders = crawl(a.reader_url, paths, workers=a.workers) if paths else {}
        # crawl only returns offenders; checked clean pages need explicit empty evidence.
        results = {p: run_offenders.get(p, []) for p in paths}
        offenders, checked, pending = apply_incremental(state, current, results, now)
        last_full = str(state.get("last_full_sweep") or "")
        save_state(state_path, state)
        print(f"render-lint: incremental batch {len(paths):,}; coverage {checked:,}/{len(records):,}; "
              f"pending {pending:,}; state {state_path}")
    else:
        paths = [r["path"] for r in records]
        if a.limit:
            paths = paths[:a.limit]
        offenders = crawl(a.reader_url, paths, workers=a.workers)
        checked = len(paths)
        pending = max(0, len(records) - checked) if a.limit else 0
        if not pending:
            last_full = now

    if a.write_vault:
        out = Path(a.write_vault) / "wiki" / "operational" / "render-lint.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_report(len(records), offenders, now, checked, pending, last_full), encoding="utf-8")
        print(f"render-lint: wrote {out}")

    if a.json:
        print(json.dumps({"total": len(records), "checked": checked, "pending": pending,
                          "last_full_sweep": last_full, "offenders": offenders}, indent=2))
    else:
        by_code: dict[str, int] = {}
        for viol in offenders.values():
            for c in viol:
                by_code[c] = by_code.get(c, 0) + 1
        print(f"render-lint: evidence {checked:,}/{len(records):,} pages ({pending:,} pending), "
              f"{len(offenders):,} with defects "
              f"{dict(sorted(by_code.items(), key=lambda x: -x[1]))}")
        for p in sorted(offenders)[:20]:
            print(f"  {p}: {', '.join(offenders[p])}")
        if len(offenders) > 20:
            print(f"  … +{len(offenders) - 20:,} more")
    return 1 if len(offenders) > a.max_offenders else 0


if __name__ == "__main__":
    raise SystemExit(main())
