"""Channel registry — the single place that says "which channels are live right now".

Runtime state derives from env vars. Missing credentials → channel is
disabled → the Send-form dropdown hides it AND the route-level send call
still defends in depth (raises ``ChannelDisabledError`` from the channel
itself, not from the registry).

9.B adds email via Resend; 9.C adds fax via Documo; 9.D (deferred) adds direct.
"""

from __future__ import annotations

import os
from typing import Callable, Final

from docstats.delivery.base import Channel, ChannelDisabledError

# All channels the system knows about. Entries here define the UI's
# channel dropdown vocabulary. Actual availability is determined at
# call time by env vars — see ``_channel_factories``.
CHANNEL_NAMES: Final[tuple[str, ...]] = ("email", "fax", "direct")


def _email_channel() -> Channel:
    """Factory for the email channel (Resend — Phase 9.B).

    Raises ``ChannelDisabledError`` unless ``RESEND_API_KEY`` is set.
    ``ResendEmailChannel.__init__`` performs the env-var check so the
    error message is authoritative.
    """
    from docstats.delivery.channels.email import ResendEmailChannel

    return ResendEmailChannel()


def _fax_channel() -> Channel:
    """Factory for the fax channel (Documo — Phase 9.C).

    Raises ``ChannelDisabledError`` unless ``DOCUMO_API_KEY`` is set.
    Live sends additionally require a signed Documo BAA at the
    Professional tier — see ``docs/fax-delivery.md``.  The env-var
    gate is enough for dev/test environments.
    """
    from docstats.delivery.channels.fax import DocumoFaxChannel

    return DocumoFaxChannel()


def _direct_channel() -> Channel:
    """Factory for the Direct Trust channel. Deferred past Phase 9.

    Requires a HISP relationship (DataMotion et al.). HISP onboarding
    takes weeks, so code stays stubbed until the user's HISP contract
    activates and the real integration lands in a follow-up phase.
    """
    raise ChannelDisabledError("direct", reason="Direct Trust deferred — HISP onboarding required")


_CHANNEL_FACTORIES: dict[str, Callable[[], Channel]] = {
    "email": _email_channel,
    "fax": _fax_channel,
    "direct": _direct_channel,
}


def get_channel(name: str) -> Channel:
    """Return the Channel impl for ``name`` or raise ChannelDisabledError.

    ``ChannelDisabledError`` is the single contract between the registry
    and the dispatcher: it tells the dispatcher to flip the row to
    ``failed`` with ``error_code = "channel_disabled"`` without retrying.
    """
    factory = _CHANNEL_FACTORIES.get(name)
    if factory is None:
        raise ChannelDisabledError(name, reason=f"unknown channel {name!r}")
    return factory()


def enabled_channels() -> list[str]:
    """Return the list of channel names whose factories succeed right now.

    Used by the Send form template to only render channels the user
    can actually pick. Does not short-circuit on missing env vars —
    each factory is responsible for raising ``ChannelDisabledError``
    on configuration gaps.
    """
    enabled: list[str] = []
    for name in CHANNEL_NAMES:
        try:
            get_channel(name)
        except ChannelDisabledError:
            continue
        enabled.append(name)
    return enabled


# Env-var conventions documented here for reference. Channel impls read
# these at factory-call time, not at import time, so flipping a var
# enables the channel on the next request without a process restart
# (useful for Railway env-var changes that trigger a rolling deploy).
_ENV_VARS_BY_CHANNEL: Final[dict[str, tuple[str, ...]]] = {
    "email": ("RESEND_API_KEY",),
    "fax": ("DOCUMO_API_KEY",),
    "direct": ("DIRECT_HISP_USERNAME", "DIRECT_HISP_PASSWORD", "DIRECT_HISP_ENDPOINT"),
}


def channel_is_configured(name: str) -> bool:
    """Cheap env-var presence check (no factory call / no HTTP)."""
    required = _ENV_VARS_BY_CHANNEL.get(name, ())
    return all(os.environ.get(var) for var in required)
