"""SMART-on-FHIR client + crypto for EHR integrations (Phase 12)."""

from docstats.ehr.crypto import EHRConfigError, decrypt_token, encrypt_token

__all__ = ["EHRConfigError", "decrypt_token", "encrypt_token"]
