"""Factory for the active ``VirusScanner`` — Phase 10.B.

Env-driven selection.  ``VIRUS_SCANNER_BACKEND`` takes precedence over
auto-detection so tests (and sandbox envs) can force a specific adapter
without unsetting credentials.

Selection rule:

  - ``VIRUS_SCANNER_BACKEND=cloudmersive`` → :class:`CloudmersiveVirusScanner`.
  - ``VIRUS_SCANNER_BACKEND=noop``         → :class:`NoOpVirusScanner`.
  - ``VIRUS_SCANNER_BACKEND=none``         → ``None`` (route layer treats
    as "no scanner"; paired with ``VIRUS_SCAN_REQUIRED=0`` in dev).
  - unset → Cloudmersive if ``CLOUDMERSIVE_API_KEY`` is present; else
    no-op (which the upload route will reject when
    ``VIRUS_SCAN_REQUIRED=1``).

Tests override via ``app.dependency_overrides[get_virus_scanner]``.
"""

from __future__ import annotations

import os

from docstats.storage_files.scanner import VirusScanner


def get_virus_scanner() -> VirusScanner | None:
    explicit = os.environ.get("VIRUS_SCANNER_BACKEND", "").strip().lower()

    if explicit == "none":
        return None

    if explicit == "noop":
        from docstats.storage_files.scanners.noop import NoOpVirusScanner

        return NoOpVirusScanner()

    if explicit == "cloudmersive":
        from docstats.storage_files.scanners.cloudmersive import (
            CloudmersiveVirusScanner,
        )

        return CloudmersiveVirusScanner()

    # Auto — pick Cloudmersive when the key is present, else fall back
    # to no-op so dev-mode uploads still work (paired with
    # VIRUS_SCAN_REQUIRED=0, which is the dev default).
    if os.environ.get("CLOUDMERSIVE_API_KEY"):
        from docstats.storage_files.scanners.cloudmersive import (
            CloudmersiveVirusScanner,
        )

        return CloudmersiveVirusScanner()

    from docstats.storage_files.scanners.noop import NoOpVirusScanner

    return NoOpVirusScanner()


def virus_scan_is_required() -> bool:
    """Read ``VIRUS_SCAN_REQUIRED``.  Default False (dev-friendly)."""
    return os.environ.get("VIRUS_SCAN_REQUIRED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
