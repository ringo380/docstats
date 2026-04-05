"""Supabase Postgres storage backend.

Used in production when SUPABASE_URL + SUPABASE_SERVICE_KEY env vars are set.
Tables are prefixed with ``docstats_`` to coexist with other apps in the same
Supabase project.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from docstats.models import NPIResult, SavedProvider, SearchHistoryEntry

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO timestamp string from Supabase into a datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value)


class PostgresStorage:
    """Supabase-backed storage using the supabase-py REST client."""

    def __init__(self) -> None:
        from supabase import create_client  # lazy import

        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        self._client = create_client(url, key)
        self._zip_loaded = False

    # --- helpers ---

    def _t(self, name: str):
        """Return a table reference with the docstats_ prefix."""
        return self._client.table(f"docstats_{name}")

    @staticmethod
    def _row_to_provider(row: dict) -> SavedProvider:
        return SavedProvider(
            npi=row["npi"],
            display_name=row["display_name"],
            entity_type=row["entity_type"],
            specialty=row.get("specialty"),
            phone=row.get("phone"),
            fax=row.get("fax"),
            address_line1=row.get("address_line1"),
            address_city=row.get("address_city"),
            address_state=row.get("address_state"),
            address_zip=row.get("address_zip"),
            raw_json=row["raw_json"],
            notes=row.get("notes"),
            appt_address=row.get("appt_address"),
            saved_at=_parse_ts(row.get("saved_at")),
            updated_at=_parse_ts(row.get("updated_at")),
        )

    # --- User CRUD ---

    def create_user(self, email: str, password_hash: str) -> int:
        result = self._t("users").insert(
            {"email": email.strip().lower(), "password_hash": password_hash}
        ).execute()
        return result.data[0]["id"]

    def get_user_by_id(self, user_id: int) -> dict | None:
        result = self._t("users").select("*").eq("id", user_id).execute()
        return result.data[0] if result.data else None

    def get_user_by_email(self, email: str) -> dict | None:
        result = self._t("users").select("*").eq("email", email.strip().lower()).execute()
        return result.data[0] if result.data else None

    def get_user_by_github_id(self, github_id: str) -> dict | None:
        result = self._t("users").select("*").eq("github_id", str(github_id)).execute()
        return result.data[0] if result.data else None

    def upsert_github_user(
        self,
        github_id: str,
        github_login: str,
        email: str | None,
        display_name: str | None,
    ) -> int:
        github_id = str(github_id)
        now = _now_iso()
        existing = self.get_user_by_github_id(github_id)
        if existing:
            updates: dict = {"github_login": github_login, "last_login_at": now}
            if display_name is not None:
                updates["display_name"] = display_name
            self._t("users").update(updates).eq("id", existing["id"]).execute()
            return existing["id"]
        if email:
            existing_email = self.get_user_by_email(email)
            if existing_email:
                self._t("users").update(
                    {"github_id": github_id, "github_login": github_login, "last_login_at": now}
                ).eq("id", existing_email["id"]).execute()
                return existing_email["id"]
        safe_email = email.strip().lower() if email else f"github_{github_id}@noemail.invalid"
        result = self._t("users").upsert(
            {
                "email": safe_email,
                "github_id": github_id,
                "github_login": github_login,
                "display_name": display_name,
            },
            on_conflict="github_id",
        ).execute()
        return result.data[0]["id"]

    def update_last_login(self, user_id: int) -> None:
        self._t("users").update({"last_login_at": _now_iso()}).eq("id", user_id).execute()

    def set_user_pcp(self, user_id: int, pcp_npi: str) -> None:
        self._t("users").update({"pcp_npi": pcp_npi}).eq("id", user_id).execute()

    def clear_user_pcp(self, user_id: int) -> None:
        self._t("users").update({"pcp_npi": None}).eq("id", user_id).execute()

    # --- Provider CRUD ---

    def save_provider(
        self, result: NPIResult, user_id: int, notes: str | None = None
    ) -> SavedProvider:
        provider = SavedProvider.from_npi_result(result, notes=notes)
        now = _now_iso()

        # Fetch existing to preserve appt_address and merge notes (matches SQLite behavior)
        existing = self.get_provider(provider.npi, user_id)
        appt_address = existing.appt_address if existing else None
        merged_notes = provider.notes if provider.notes is not None else (existing.notes if existing else None)

        self._t("saved_providers").upsert(
            {
                "user_id": user_id,
                "npi": provider.npi,
                "display_name": provider.display_name,
                "entity_type": provider.entity_type,
                "specialty": provider.specialty,
                "phone": provider.phone,
                "fax": provider.fax,
                "address_line1": provider.address_line1,
                "address_city": provider.address_city,
                "address_state": provider.address_state,
                "address_zip": provider.address_zip,
                "raw_json": provider.raw_json,
                "notes": merged_notes,
                "appt_address": appt_address,
                "saved_at": provider.saved_at.isoformat() if provider.saved_at else now,
                "updated_at": now,
            },
            on_conflict="user_id,npi",
        ).execute()
        logger.info("Saved provider %s: %s (user %s)", provider.npi, provider.display_name, user_id)
        return provider

    def get_provider(self, npi: str, user_id: int | None) -> SavedProvider | None:
        if user_id is None:
            return None
        result = (
            self._t("saved_providers")
            .select("*")
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return self._row_to_provider(result.data[0]) if result.data else None

    def list_providers(self, user_id: int) -> list[SavedProvider]:
        result = (
            self._t("saved_providers")
            .select("*")
            .eq("user_id", user_id)
            .order("saved_at", desc=True)
            .order("npi", desc=True)
            .execute()
        )
        return [self._row_to_provider(r) for r in result.data]

    def delete_provider(self, npi: str, user_id: int) -> bool:
        result = (
            self._t("saved_providers")
            .delete()
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    def set_appt_address(self, npi: str, address: str, user_id: int) -> bool:
        result = (
            self._t("saved_providers")
            .update({"appt_address": address.strip()})
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    def clear_appt_address(self, npi: str, user_id: int) -> bool:
        result = (
            self._t("saved_providers")
            .update({"appt_address": None})
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    def update_notes(self, npi: str, notes: str | None, user_id: int) -> bool:
        result = (
            self._t("saved_providers")
            .update({"notes": notes, "updated_at": _now_iso()})
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    # --- Search history ---

    def log_search(
        self, params: dict[str, str], result_count: int, user_id: int | None = None
    ) -> None:
        self._t("search_history").insert(
            {"query_params": json.dumps(params), "result_count": result_count, "user_id": user_id}
        ).execute()

    def get_history(self, limit: int = 20, user_id: int | None = None) -> list[SearchHistoryEntry]:
        if user_id is None:
            return []
        result = (
            self._t("search_history")
            .select("*")
            .eq("user_id", user_id)
            .order("searched_at", desc=True)
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        return [
            SearchHistoryEntry(
                id=r["id"],
                query_params=json.loads(r["query_params"]),
                result_count=r["result_count"],
                searched_at=_parse_ts(r.get("searched_at")),
            )
            for r in result.data
        ]

    # --- ZIP code lookup ---

    def lookup_zip(self, zip_code: str) -> dict[str, str] | None:
        self._ensure_zip_table()
        result = (
            self._t("zip_codes")
            .select("city,state")
            .eq("zip_code", zip_code.strip()[:5])
            .execute()
        )
        if not result.data:
            return None
        return {"city": result.data[0]["city"], "state": result.data[0]["state"]}

    def _ensure_zip_table(self) -> None:
        if self._zip_loaded:
            return
        # Check if table has data
        result = self._t("zip_codes").select("zip_code").limit(1).execute()
        if result.data:
            self._zip_loaded = True
            return
        # Load from JSON
        data_file = Path(__file__).parent / "data" / "zipcodes.json"
        if not data_file.exists():
            logger.warning("ZIP code data file not found at %s", data_file)
            self._zip_loaded = True
            return
        data = json.loads(data_file.read_text())
        rows = [{"zip_code": z["zip"], "city": z["city"], "state": z["state"]} for z in data]
        try:
            for i in range(0, len(rows), 500):
                self._t("zip_codes").upsert(rows[i : i + 500], on_conflict="zip_code").execute()
            logger.info("Loaded %d ZIP codes into Supabase", len(data))
            self._zip_loaded = True
        except Exception:
            logger.exception("Failed to load ZIP codes into Supabase — will retry next request")

    def close(self) -> None:
        pass  # supabase-py client has no close method
