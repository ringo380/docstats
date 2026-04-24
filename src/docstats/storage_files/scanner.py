"""Virus-scanning contract — Phase 10.B.

Every attachment upload goes through a ``VirusScanner`` before the bytes
land in the bucket.  The scanner is pluggable so we can swap vendors
without touching the route layer — Cloudmersive is the default adapter
(REST, BAA-covered on the Enterprise plan); a ``NoOpVirusScanner`` ships
for local dev so developers aren't blocked by a missing API key.

Policy
------
The upload route consults ``VIRUS_SCAN_REQUIRED``:

  - ``1`` / ``true`` / ``yes`` → FAIL-CLOSED: if the scanner raises
    ``ScannerUnavailable`` (network / vendor outage / unconfigured), the
    upload is rejected with HTTP 502.  This is the production default
    once the BAA signs.
  - unset / ``0`` / ``false`` → FAIL-OPEN: scanner errors are logged and
    the upload proceeds.  This is the dev default — turning it on in
    production without a real scanner is a footgun.

An infected-file verdict ALWAYS rejects the upload (HTTP 422) regardless
of the policy.  The policy only governs what happens when the scanner
itself is broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ScanResult:
    """Outcome of a single scan.

    ``infected`` is authoritative — if True, the upload must be rejected.
    ``threat_names`` lists vendor-assigned threat labels for audit logs.
    ``scanner_name`` identifies the adapter (for provenance in the
    ``attachment.create`` / ``attachment.scan_rejected`` audit rows).
    """

    infected: bool
    scanner_name: str
    threat_names: list[str] = field(default_factory=list)


class ScannerUnavailable(Exception):
    """The scanner couldn't render a verdict — network error, vendor 5xx,
    missing credentials, etc.  Route layer maps to 502 when
    ``VIRUS_SCAN_REQUIRED`` is on, else logs and proceeds."""


class VirusScanner(Protocol):
    """Minimal scanner surface.  Async because real adapters do network
    I/O; in-process stub scanners implement the same shape."""

    name: str

    async def scan(self, data: bytes, *, filename: str | None = None) -> ScanResult:
        """Scan ``data``.  Raises :class:`ScannerUnavailable` on transport
        errors; returns a :class:`ScanResult` on any definitive verdict
        (clean or infected)."""
