"""Cloudmersive REST adapter for ``VirusScanner`` — Phase 10.B.

Wire contract (confirmed with Cloudmersive docs):
  - ``POST https://api.cloudmersive.com/virus/scan/file``
  - Header: ``Apikey: <CLOUDMERSIVE_API_KEY>``
  - Multipart with field ``inputFile`` (camelCase in the REST layer —
    NOT ``input_file`` which is the Python SDK symbol)
  - Response body JSON:

        {
          "CleanResult": bool,
          "FoundViruses": [
            {"FileName": "...", "VirusName": "..."},
            ...
          ]
        }

    The ``FoundViruses`` list may be absent on a clean result; we treat
    missing as empty.

The Enterprise tier offers a signed BAA — required before PHI flows
through production.  Sandbox / dev can use the free tier for testing.

Env vars
--------
``CLOUDMERSIVE_API_KEY``   — required; absence raises at adapter
                              instantiation so the factory can fall back
                              cleanly.
``CLOUDMERSIVE_BASE_URL``  — optional override (default prod); useful
                              for BAA-gated sandbox environments.
``CLOUDMERSIVE_TIMEOUT``   — optional seconds; default 60.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from docstats.storage_files.scanner import ScannerUnavailable, ScanResult

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.cloudmersive.com"
_SCAN_PATH = "/virus/scan/file"
_DEFAULT_TIMEOUT = 60.0


class CloudmersiveVirusScanner:
    name = "cloudmersive"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        key = api_key or os.environ.get("CLOUDMERSIVE_API_KEY", "")
        if not key:
            raise ScannerUnavailable("CLOUDMERSIVE_API_KEY not set")
        self._api_key = key
        self._base_url = (
            base_url or os.environ.get("CLOUDMERSIVE_BASE_URL") or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._timeout = timeout_seconds or _parse_timeout()

    async def scan(self, data: bytes, *, filename: str | None = None) -> ScanResult:
        if not data:
            # Empty files were already rejected by the route; defend anyway.
            return ScanResult(infected=False, scanner_name=self.name, threat_names=[])

        url = f"{self._base_url}{_SCAN_PATH}"
        # Cloudmersive expects the multipart field ``inputFile``.  httpx
        # needs a (filename, bytes, mime) tuple; the filename is
        # informational — we use ``upload`` when the caller didn't pass one
        # so server logs don't get PHI-ish strings.
        files = {"inputFile": (filename or "upload", data, "application/octet-stream")}
        headers = {"Apikey": self._api_key, "Accept": "application/json"}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(url, files=files, headers=headers)
            except httpx.TimeoutException as exc:
                raise ScannerUnavailable(f"Cloudmersive timeout: {exc}") from exc
            except httpx.RequestError as exc:
                raise ScannerUnavailable(f"Cloudmersive network error: {exc}") from exc

        if resp.status_code == 401:
            raise ScannerUnavailable("Cloudmersive 401 — check CLOUDMERSIVE_API_KEY")
        if resp.status_code == 429:
            raise ScannerUnavailable("Cloudmersive 429 — rate limited")
        if resp.status_code >= 500:
            raise ScannerUnavailable(f"Cloudmersive {resp.status_code}: {resp.text[:200]}")
        if resp.status_code not in (200, 201):
            # 4xx other than auth/rate are almost always malformed input on
            # our side — fail closed (treat as unavailable) so a broken
            # client doesn't silently skip scans.
            raise ScannerUnavailable(f"Cloudmersive {resp.status_code}: {resp.text[:200]}")

        try:
            body: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise ScannerUnavailable(f"Cloudmersive returned non-JSON: {resp.text[:200]}") from exc

        clean = body.get("CleanResult")
        if not isinstance(clean, bool):
            raise ScannerUnavailable(f"Cloudmersive response missing CleanResult: {body!r}")

        raw_viruses = body.get("FoundViruses") or []
        threat_names: list[str] = []
        if isinstance(raw_viruses, list):
            for v in raw_viruses:
                if isinstance(v, dict):
                    name = v.get("VirusName")
                    if isinstance(name, str) and name:
                        threat_names.append(name)

        return ScanResult(
            infected=not clean,
            scanner_name=self.name,
            threat_names=threat_names,
        )


def _parse_timeout() -> float:
    raw = os.environ.get("CLOUDMERSIVE_TIMEOUT", "")
    if not raw:
        return _DEFAULT_TIMEOUT
    try:
        return max(1.0, min(300.0, float(raw)))
    except ValueError:
        return _DEFAULT_TIMEOUT
