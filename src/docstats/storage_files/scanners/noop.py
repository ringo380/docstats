"""No-op virus scanner — Phase 10.B.

Accepts any input as clean.  Used as the dev default when no real scanner
credential is configured; **never use this in production** — the upload
route's ``VIRUS_SCAN_REQUIRED`` gate exists specifically to keep this
scanner out of prod.

The audit trail still records ``scanner_name='noop'`` so forensic
inspection can tell a no-op scan apart from a real one.
"""

from __future__ import annotations

from docstats.storage_files.scanner import ScanResult


class NoOpVirusScanner:
    name = "noop"

    async def scan(self, data: bytes, *, filename: str | None = None) -> ScanResult:
        return ScanResult(infected=False, scanner_name=self.name, threat_names=[])
