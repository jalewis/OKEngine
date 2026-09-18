"""Compatibility binding for incrementally extracted write-path services."""

from __future__ import annotations

import types


def install_state(facade: dict, module) -> None:
    facade.update((name, getattr(module, name)) for name in module.__all__)


def install_services(facade: dict, modules: tuple) -> None:
    owners: dict[str, str] = {}
    collisions: list[str] = []
    for module in modules:
        for symbol, implementation in vars(module).items():
            if not (
                (isinstance(implementation, types.FunctionType)
                 or isinstance(implementation, type))
                and implementation.__module__ == module.__name__
            ):
                continue
            previous = owners.setdefault(symbol, module.__name__)
            if previous != module.__name__:
                collisions.append(f"{symbol}: {previous}, {module.__name__}")
    if collisions:
        raise ValueError(
            "write-service symbols must be pairwise unique; shared facade rebinding would "
            "silently replace sibling implementations: " + "; ".join(collisions)
        )

    def rebind(implementation):
        rebound = types.FunctionType(
            implementation.__code__,
            facade,
            implementation.__name__,
            implementation.__defaults__,
            implementation.__closure__,
        )
        rebound.__kwdefaults__ = implementation.__kwdefaults__
        rebound.__annotations__ = implementation.__annotations__
        rebound.__doc__ = implementation.__doc__
        return rebound

    for module in modules:
        for symbol, implementation in vars(module).items():
            if (
                isinstance(implementation, types.FunctionType)
                and implementation.__module__ == module.__name__
            ):
                facade[symbol] = rebind(implementation)
            elif isinstance(implementation, type) and implementation.__module__ == module.__name__:
                attrs = {
                    key: rebind(value) if isinstance(value, types.FunctionType) else value
                    for key, value in vars(implementation).items()
                    if key not in {"__dict__", "__weakref__"}
                }
                facade[symbol] = type(symbol, implementation.__bases__, attrs)
