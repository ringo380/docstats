"""Virus-scanner adapters — Phase 10.B."""

from docstats.storage_files.scanners.cloudmersive import CloudmersiveVirusScanner
from docstats.storage_files.scanners.noop import NoOpVirusScanner

__all__ = ["CloudmersiveVirusScanner", "NoOpVirusScanner"]
