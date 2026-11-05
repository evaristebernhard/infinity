"""Task adapters, imported only when a campaign selects a domain."""

from importlib import import_module


def get_domain(name: str):
    if name not in {"filament", "smoke"}:
        raise ValueError(f"Unknown research domain: {name!r}")
    return import_module(f"{__name__}.{name}")
