"""Every scripts/cron module must be importable BY PATH, with nothing installed.

okpacks-library#89: adding `corpus_audit -> okengine.actor_identity` turned every merge request
in that repo red. The module imports fine in a deployment, where the engine is pip-installed,
but an external consumer that loads it by path installs nothing — so the package is absent and
the module dies at import with an opaque ModuleNotFoundError, in someone else's CI, with nothing
on this side to signal it.

The compose check is a real consumer doing exactly this, and it is not the only one: any tool
pointing spec_from_file_location at one of these files has the same contract. Requiring each
consumer to discover and declare our internal layout is backwards. A module that can be loaded
by path should be importable by path.

These tests hide the package IN-PROCESS with a meta-path finder. Shelling out to a "clean"
interpreter was tried first and cannot be made reliable: the dev venv installs the engine
editable (a .pth processed at interpreter startup, immune to PYTHONPATH), and CI symlinks
/usr/bin/python3 to the same interpreter it pip-installs the wheel into. The guard test caught
that in CI rather than a reviewer catching it here.
"""
from __future__ import annotations

import contextlib
import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CRON = REPO / "scripts" / "cron"

# Modules that import the okengine PACKAGE and are therefore the ones at risk. Kept explicit
# rather than globbed: a new entrant should be a deliberate line in this list, which is the
# moment to ask whether it also needs the bootstrap.
PACKAGE_IMPORTERS = ["corpus_audit.py", "postgres_projection.py", "review_autoverify.py"]

class _HidePackage:
    """A meta-path finder that makes `okengine` unimportable for the duration of a test.

    An earlier version of this file shelled out to a "clean" interpreter instead. That cannot be
    made reliable: the dev venv installs the engine editable (a .pth processed at startup, immune
    to PYTHONPATH), and CI symlinks /usr/bin/python3 to the same interpreter it pip-installs the
    wheel into. Both were tried; the CI run proved the second. Blocking the import in-process is
    environment-independent and tests the same property.
    """

    def find_module(self, fullname, path=None):          # pragma: no cover - legacy hook
        return None

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "okengine" or fullname.startswith("okengine."):
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None


@contextlib.contextmanager
def _package_hidden():
    finder = _HidePackage()
    saved_modules = {k: v for k, v in sys.modules.items() if k == "okengine" or k.startswith("okengine.")}
    for key in saved_modules:
        del sys.modules[key]
    saved_path = list(sys.path)
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        sys.path[:] = saved_path
        sys.modules.update(saved_modules)


def _bootstrap():
    sys.path.insert(0, str(CRON))
    try:
        import engine_package
        return engine_package
    finally:
        sys.path.remove(str(CRON))


def test_the_probe_really_hides_the_package():
    """Guards the guard. If hiding stops working, every test below passes for the wrong reason —
    which is exactly what the subprocess version did in CI before this was rewritten."""
    with _package_hidden():
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("okengine")


def test_the_bootstrap_supplies_the_package_root_when_it_is_missing():
    """THE REGRESSION, at the mechanism. Without it an external consumer loading any of these
    modules by path gets an opaque ModuleNotFoundError in its own CI."""
    module = _bootstrap()
    src = str(Path(module._SRC))
    with _package_hidden():
        sys.path[:] = [p for p in sys.path if p != src]
        assert module.ensure() is True, "fallback was not applied while the package was missing"
        assert src in sys.path, "ensure() reported success without adding the package root"


def test_the_bootstrap_is_idempotent_and_does_not_duplicate_the_path_entry():
    """`ensure()` is documented as safe to call from module scope, and three modules do call it —
    so in a by-path consumer that loads all three it runs three times in one process. Appending
    the same root on every call would grow sys.path without bound."""
    module = _bootstrap()
    src = str(Path(module._SRC))
    with _package_hidden():
        sys.path[:] = [p for p in sys.path if p != src]
        assert module.ensure() is True
        assert module.ensure() is True, "second call should still report the fallback is in force"
        assert sys.path.count(src) == 1, "ensure() appended the package root a second time"


def test_the_bootstrap_declines_when_there_is_no_adjacent_source_tree():
    """A consumer with neither an installed package nor an adjacent source tree must fail on its
    OWN import, with its own traceback. The bootstrap reports that it could not help and returns;
    it must not raise, and must not put a nonexistent directory on sys.path."""
    module = _bootstrap()
    missing = Path(module._SRC).parent / "no-such-src-tree"
    with _package_hidden():
        before = list(sys.path)
        saved_src = module._SRC
        module._SRC = missing
        try:
            assert module.ensure() is False, "ensure() claimed success with no package root to add"
        finally:
            module._SRC = saved_src
        assert sys.path == before, "ensure() modified sys.path despite having nothing to add"


@pytest.mark.parametrize("name", PACKAGE_IMPORTERS)
def test_package_importers_declare_the_bootstrap(name):
    """The bootstrap must be called BEFORE the package import, or it cannot help."""
    text = (CRON / name).read_text(encoding="utf-8")
    assert "engine_package" in text, f"{name} imports okengine without calling the bootstrap"
    boot = text.index("engine_package.ensure()")
    first_pkg = min(
        (text.index(tok) for tok in ("from okengine", "import okengine") if tok in text),
        default=len(text))
    assert boot < first_pkg, f"{name} calls the bootstrap after importing okengine"


def test_the_bootstrap_prefers_an_installed_package():
    """A deployment must keep using its INSTALLED engine. Silently shadowing it with an adjacent
    source tree of a different version would be a worse failure than the one this fixes."""
    sys.path.insert(0, str(CRON))
    try:
        import engine_package
        assert engine_package.ensure() is False, (
            "ensure() modified sys.path even though okengine was already importable")
    finally:
        sys.path.remove(str(CRON))


def test_the_bootstrap_points_at_the_real_package_root():
    sys.path.insert(0, str(CRON))
    try:
        import engine_package
        assert (engine_package._SRC / "okengine" / "__init__.py").is_file(), (
            f"bootstrap resolves to {engine_package._SRC}, which is not the package root")
    finally:
        sys.path.remove(str(CRON))
