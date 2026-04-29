"""Vendor module registry for EHR/SMART-on-FHIR dispatch.

Each vendor (epic, cerner, ...) is a plain Python module that exposes the
same function signatures. The registry maps vendor name → module so routes
can dispatch without importing vendor modules directly.

Vendors register themselves at module-import time by calling ``register()``
at the bottom of their module. Import the vendor module once (e.g. in
``routes/ehr.py``) to trigger registration.
"""

from __future__ import annotations

from types import ModuleType

_REGISTRY: dict[str, ModuleType] = {}


class EHRError(RuntimeError):
    """Base exception for all EHR vendor errors.

    Individual vendor modules subclass this (EpicError, CernerError) so
    callers can catch the common base without importing vendor-specific names.
    """


def register(vendor: str, module: ModuleType) -> None:
    """Register *module* under *vendor* name. Called by each vendor module."""
    _REGISTRY[vendor] = module


def get(vendor: str) -> ModuleType:
    """Return the registered module for *vendor*, or raise ValueError."""
    try:
        return _REGISTRY[vendor]
    except KeyError:
        raise ValueError(f"Unknown EHR vendor: {vendor!r}") from None


def list_vendors() -> list[str]:
    """Return list of currently registered vendor names."""
    return list(_REGISTRY.keys())
