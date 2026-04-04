"""GitHub OAuth flow helpers."""

from __future__ import annotations

import os
import urllib.parse

import httpx

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
GITHUB_ENABLED = bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET)

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"
_EMAILS_URL = "https://api.github.com/user/emails"
_SCOPES = "read:user user:email"


def github_authorize_url(state: str) -> str:
    """Build the GitHub OAuth authorization URL."""
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "scope": _SCOPES,
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


async def github_exchange_code(code: str, client: httpx.AsyncClient) -> dict:
    """Exchange an authorization code for an access token."""
    response = await client.post(
        _TOKEN_URL,
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
        },
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


async def github_get_user(token: str, client: httpx.AsyncClient) -> dict:
    """Get the GitHub user profile."""
    response = await client.get(
        _USER_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    response.raise_for_status()
    return response.json()


async def github_get_emails(token: str, client: httpx.AsyncClient) -> list[dict]:
    """Get the GitHub user's email addresses (requires user:email scope)."""
    response = await client.get(
        _EMAILS_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    response.raise_for_status()
    return response.json()


def primary_github_email(emails: list[dict]) -> str | None:
    """Extract the primary verified email from GitHub's email list."""
    for e in emails:
        if e.get("primary") and e.get("verified"):
            return e["email"]
    for e in emails:
        if e.get("verified"):
            return e["email"]
    return None
