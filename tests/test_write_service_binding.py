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
