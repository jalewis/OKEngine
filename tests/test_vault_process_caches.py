"""Standing detector: no process-lifetime memo over vault files in the reader or cockpit (okengine#1024).

The reader's embed and link-title memos and the cockpit's embed and ref-title memos were plain dicts
or ``lru_cache`` functions. They kept every answer for the life of the container, misses included:
a link written before its page rendered as missing until a recreate, a reshelved page rendered as an
"unreadable embed", a retitled page kept its old title, and the memos grew without bound. The
okengine#1017 mtime detector could not see them, because they had no freshness key at all.

A memo over vault files in these servers uses ``okengine.vault_lookup_cache.VaultLookupCache``
(revalidated hits, expiring misses, bounded). This test inventories every other memo shape in the
served surfaces: an ``lru_cache``/``cache`` decorator, a module-level dict whose name says cache, and
a mutable default argument named as a cache. Each must be reviewed below with the reason it cannot
serve a stale vault answer. A new one fails here until it is reviewed; the list only shrinks.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOTS = ("okengine-reader", "okengine-cockpit", "src/okengine/cockpit_services")
_CACHE_DECORATORS = {"lru_cache", "cache"}

# "<path>:<name>" -> why it cannot serve a stale vault answer
REVIEWED = {
    "okengine-cockpit/app.py:_ev_direction_enum": (
        "maxsize=1 over the composed schema artifact, which only a deploy rewrites; a deploy "
        "recreates the cockpit container"),
    "okengine-cockpit/app.py:_review_required_types": (
        "maxsize=1 over the composed schema policy, which only a deploy rewrites; a deploy "
        "recreates the cockpit container"),
    "okengine-reader/directory_index.py:_declared_namespaces": (
        "maxsize=32, and the caller passes the schema's file fingerprint in the key"),
    "okengine-reader/app.py:_DIR_CACHE": (
        "every entry carries its own monotonic stamp and is rescanned after the TTL; keyed by "
        "namespace directory"),
    "src/okengine/cockpit_services/state.py:_DIR_CACHE": (
        "every entry carries its own monotonic stamp and is rescanned after the TTL; keyed by "
        "configured dataset directory"),
    "src/okengine/cockpit_services/state.py:_BROWSE_CACHE": (
        "every entry carries its own monotonic stamp and is rescanned after the TTL; keyed by "
        "namespace directory"),
    "src/okengine/cockpit_services/datasets.py:_actor_schema._cache": (
        "one entry, keyed on the schema's file fingerprint"),
}


def _is_cache_name(name: str) -> bool:
    return "cache" in name.lower()


def _is_empty_mapping(node: ast.AST | None) -> bool:
    """`{}`, `dict()` or `OrderedDict()` with no arguments."""
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.Call) and not node.args and not node.keywords:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name in {"dict", "OrderedDict"}
    return False


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def memos(source: str) -> list[str]:
    """The memo shapes in one module's source, by name."""
    tree = ast.parse(source)
    found = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if targets and _is_empty_mapping(node.value):
            found += [t for t in targets if _is_cache_name(t)]
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_decorator_name(d) in _CACHE_DECORATORS for d in node.decorator_list):
            found.append(node.name)
        args = node.args
        positional = args.posonlyargs + args.args
        pairs = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
        found += [f"{node.name}.{a.arg}" for a, d in pairs
                  if _is_cache_name(a.arg) and _is_empty_mapping(d)]
    return found


def scan(repo: Path, roots=ROOTS) -> set[str]:
    out = set()
    for root in roots:
        base = repo / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(repo).as_posix()
            out |= {f"{rel}:{name}" for name in memos(path.read_text(encoding="utf-8"))}
    return out


def unreviewed(found: set[str], reviewed=REVIEWED) -> list[str]:
    problems = [f"{k}: a process memo with no reviewed freshness story" for k in found - set(reviewed)]
    problems += [f"{k}: reviewed but no longer present (remove it)" for k in set(reviewed) - found]
    return sorted(problems)


def test_every_reader_and_cockpit_memo_is_revalidating_or_reviewed():
    problems = unreviewed(scan(REPO))
    assert not problems, (
        "A memo over vault files in the reader or cockpit must be an okengine.vault_lookup_cache."
        "VaultLookupCache (okengine#1024); any other memo goes into REVIEWED with the reason it "
        "cannot serve a stale vault answer:\n  " + "\n  ".join(problems))


def test_the_fixed_memos_are_revalidating_caches():
    """The four #1024 memos stay VaultLookupCache instances, not plain dicts or lru_cache."""
    sources = {rel: (REPO / rel).read_text(encoding="utf-8") for rel in (
        "okengine-reader/rendering.py", "src/okengine/cockpit_services/state.py")}
    assert "_LINK_TITLE_CACHE = VaultLookupCache()" in sources["okengine-reader/rendering.py"]
    assert "_EMBED_PATH_CACHE = VaultLookupCache()" in sources["okengine-reader/rendering.py"]
    assert "_EMBED_PATH_CACHE = VaultLookupCache()" in sources["src/okengine/cockpit_services/state.py"]
    assert "_REF_TITLE_CACHE = VaultLookupCache()" in sources["src/okengine/cockpit_services/state.py"]


def test_the_detector_flags_each_planted_memo_shape(tmp_path):
    """Negative fixture: each shape the #1024 memos had is reported in a served surface."""
    mod = tmp_path / "okengine-reader" / "planted.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(
        "import functools\nfrom functools import lru_cache\nfrom collections import OrderedDict\n"
        "_TITLE_CACHE: dict = {}\n_PATH_CACHE = dict()\n_lru_cache_map = OrderedDict()\n"
        "_NOT_A_MEMO = {}\n_SEEDED_CACHE = {'k': 1}\n"
        "@functools.lru_cache(maxsize=8)\ndef title(rel): return rel\n"
        "@lru_cache\ndef bare(rel): return rel\n"
        "@functools.cache\ndef forever(rel): return rel\n"
        "def lookup(key, _cache: dict = {}, *, other_cache=dict(), plain={}): return key\n",
        encoding="utf-8")
    assert scan(tmp_path) == {
        f"okengine-reader/planted.py:{n}" for n in (
            "_TITLE_CACHE", "_PATH_CACHE", "_lru_cache_map", "title", "bare", "forever",
            "lookup._cache", "lookup.other_cache")}
    assert unreviewed({"okengine-reader/planted.py:title"}, reviewed={}) == [
        "okengine-reader/planted.py:title: a process memo with no reviewed freshness story"]


def test_the_detector_flags_a_stale_reviewed_entry():
    assert unreviewed(set(), reviewed={"okengine-reader/gone.py:_X_CACHE": "why"}) == [
        "okengine-reader/gone.py:_X_CACHE: reviewed but no longer present (remove it)"]


def test_the_detector_rejects_the_pre_1024_reader_memos():
    """Negative fixture: the exact pre-#1024 lines are flagged."""
    assert memos("_LINK_TITLE_CACHE: dict = {}\n_EMBED_PATH_CACHE: dict = {}\n") == [
        "_LINK_TITLE_CACHE", "_EMBED_PATH_CACHE"]
    assert memos("import functools\n@functools.lru_cache(maxsize=4096)\n"
                 "def _ref_title(rel):\n    return rel\n") == ["_ref_title"]
