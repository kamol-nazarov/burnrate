"""Usage ingestion adapters.

Adapter modules keep their ``ingest()`` signatures. The provider registry lives
in ``spend_app.providers`` and is re-exported here for a single import surface.
"""

from __future__ import annotations

__all__ = [
    "CAPABILITIES",
    "EXACTNESS",
    "PROVIDERS",
    "REGISTRY",
    "STABILITIES",
    "ProviderSpec",
    "capability_reports",
    "get_provider",
]


def __getattr__(name: str):
    if name in __all__:
        from spend_app import providers

        return getattr(providers, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
