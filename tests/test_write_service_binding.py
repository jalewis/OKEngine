"""The shared write-service facade must never silently replace a sibling symbol."""

from types import ModuleType

import pytest

from okengine.write_services.binding import install_services


def _module(name: str, source: str) -> ModuleType:
    module = ModuleType(name)
    exec(compile(source, f"<{name}>", "exec"), module.__dict__)
    return module


def test_cross_module_symbol_collision_fails_before_facade_mutation():
    first = _module("write_first", "def shared(): return 'first'\ndef caller(): return shared()\n")
    second = _module("write_second", "def shared(): return 'second'\n")
    facade = {"sentinel": object()}

    with pytest.raises(ValueError, match=r"shared: write_first, write_second"):
        install_services(facade, (first, second))

    assert set(facade) == {"sentinel"}, "collision preflight must be transactional"


def test_unique_symbols_rebind_bare_sibling_calls_to_the_shared_facade():
    module = _module("write_unique", "def helper(): return 41\ndef caller(): return helper() + 1\n")
    facade = {}

    install_services(facade, (module,))

    assert facade["caller"]() == 42
    facade["helper"] = lambda: 99
    assert facade["caller"]() == 100, "the test must exercise dynamic shared-global rebinding"


def test_function_and_class_name_collision_is_rejected():
    first = _module("write_function", "def Guard(): return True\n")
    second = _module("write_class", "class Guard:\n    pass\n")

    with pytest.raises(ValueError, match=r"Guard: write_function, write_class"):
        install_services({}, (first, second))


def _rename(module: ModuleType, name: str) -> ModuleType:
    """Give ``module`` an equal but distinct ``__name__`` string object.

    The functions and classes defined in it keep the original string as ``__module__``,
    so ownership must be decided by string equality, never identity.
    """
    fresh = "".join(list(name))
    assert fresh == module.__name__ and fresh is not module.__name__
    module.__name__ = fresh
    return module


def test_ownership_compares_module_names_by_value_for_collision_detection():
    first = _rename(_module("write_owner_a", "def shared(): return 'a'\n"), "write_owner_a")
    second = _rename(_module("write_owner_b", "def shared(): return 'b'\n"), "write_owner_b")

    with pytest.raises(ValueError, match=r"shared: write_owner_a, write_owner_b"):
        install_services({}, (first, second))


def test_functions_are_rebound_when_their_module_name_is_an_equal_string():
    module = _rename(
        _module("write_rebound", "def helper(): return 1\ndef caller(): return helper() + 1\n"),
        "write_rebound",
    )
    facade = {"helper": lambda: 40}

    install_services(facade, (module,))

    assert facade["caller"]() == 2
    facade["helper"] = lambda: 40
    assert facade["caller"]() == 41, "caller must resolve helper through the shared facade"


def test_classes_are_rebuilt_when_their_module_name_is_an_equal_string():
    module = _rename(
        _module("write_klass", "def unit(): return 3\nclass Box:\n    def get(self): return unit()\n"),
        "write_klass",
    )
    facade = {}

    install_services(facade, (module,))

    assert facade["Box"] is not module.Box
    facade["unit"] = lambda: 7
    assert facade["Box"]().get() == 7, "class methods must be rebound to the shared facade"


def test_two_module_objects_with_one_name_do_not_collide_with_themselves():
    # A re-created module object (e.g. after reload) carries the same dotted name; its symbols
    # are owned by that name, so listing both is not a cross-module collision.
    original = _module("write_same", "def only(): return 'old'\n")
    recreated = _rename(_module("write_same", "def only(): return 'new'\n"), "write_same")
    facade = {}

    install_services(facade, (original, recreated))

    assert facade["only"]() == "new"
