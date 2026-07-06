#!/usr/bin/env python3
"""bench_vault.py — measure how the full-vault no_agent audits scale before a large import (#74).

Generates a synthetic vault of N pages (entities sharded by letter, a few sources) and times the
audits that rglob + parse every page (conformance / grounding / review-queue), reporting ms/page and
the projection at a target size (default 48k). Measure first — don't guess the bottleneck.

Usage: python scripts/bench_vault.py [N] [--target 48000]
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _HERE / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def gen_vault(root: Path, n: int) -> None:
    (root / "schema.yaml").write_text("okf: {required: [type]}\nreference_fields: [mitre_id]\n")
    sdir = root / "wiki" / "sources" / "2026" / "06"
    sdir.mkdir(parents=True, exist_ok=True)
    for i in range(min(200, n // 10 + 1)):
        (sdir / f"src-{i}.md").write_text(f"---\ntype: source\npublished: 2026-06-15\n---\n# s{i}\n")
    body = ("Lorem ipsum dolor sit amet. " * 40) + "\n## Detail\n" + ("More text. " * 40) + "\n"
    edir = root / "wiki" / "entities"
    for i in range(n):
        slug = f"entity-{i:06d}"
        d = edir / slug[7]
        d.mkdir(parents=True, exist_ok=True)
        src = f"sources/2026/06/src-{i % 200}" if i % 3 else "Prose citation only"
        (d / f"{slug}.md").write_text(
            f"---\ntype: entity\nname: Entity {i}\nsources:\n- {src}\n"
            f"last_updated: 2026-06-{(i % 28) + 1:02d}\nneeds_review: {'true' if i % 50 == 0 else 'false'}\n"
            f"---\n# Entity {i}\n{body}")


def _time(label, fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def main(argv) -> int:
    n = int(argv[0]) if argv and not argv[0].startswith("-") else 5000
    target = 48000
    if "--target" in argv:
        target = int(argv[argv.index("--target") + 1])
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="bench-vault-"))
    print(f"generating {n} pages in {root} …")
    tg = _time("gen", lambda: gen_vault(root, n))
    pages = sum(1 for _ in (root / "wiki").rglob("*.md"))
    print(f"  generated {pages} pages in {tg:.1f}s\n")
    os.environ["WIKI_PATH"] = str(root)

    ops = [
        ("conformance_audit", "cron/conformance_audit.py"),
        ("grounding_audit", "cron/grounding_audit.py"),
        ("review_queue", "cron/review_queue.py"),
    ]
    print(f"{'op':22} {'sec':>8} {'ms/page':>9} {'proj@' + str(target):>12}")
    print("-" * 54)
    for name, rel in ops:
        try:
            mod = _load(name, rel)
            # silence the audit's own stdout
            import io
            import contextlib
            buf = io.StringIO()
            dt = _time(name, lambda: contextlib.redirect_stdout(buf).__enter__() or mod.main())
            mspp = dt / pages * 1000
            proj = mspp * target / 1000
            print(f"{name:22} {dt:8.2f} {mspp:9.3f} {proj:10.1f}s")
        except Exception as e:
            print(f"{name:22} ERROR: {e}")
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
