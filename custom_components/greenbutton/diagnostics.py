"""Config-entry diagnostics for Open Green Button.

Surfaced via **Settings → Devices & Services → Open Green Button → ⋮ → Download
Diagnostics**. The output is a JSON file containing entry config (with secrets and the
customer's address / account number redacted),
coordinator state, the parsed shape of the last fetched ESPI feed, and — if debug logging
has been enabled on the integration and a fetch has completed since then — the raw upstream
XML inline. The XML is loaded from disk only when diagnostics is requested; we don't keep
it in memory between fetches.

Recovering the XML for offline analysis:

    jq -r '.raw_xml' diagnostics-greenbutton-*.json > usage.xml
    xmllint --format usage.xml | less

The integration never writes XML to disk unless debug logging is on for our domain (the
same toggle behind **⋮ → Enable debug logging**), so a vanilla diagnostics download will
show ``raw_xml: null`` until debug is on *and* one refresh cycle has run after that.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data

from .const import (
    CONF_CUSTOMER_ACCOUNT_ID,
    CONF_CUSTOMER_ADDRESS,
    CONF_CUSTOMER_LABEL,
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_PROXY_TOKEN,
    DOMAIN,
)
from .storage import xml_cache_path

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import UsageResponse
    from .coordinator import GreenButtonCoordinator

_LOGGER = logging.getLogger(__package__)

# Encrypted but still sensitive — the blob is opaque to the recipient of a diagnostics
# bundle but it's still a credential against the proxy server.
#
# The customer fields identify the end user: the service address, the utility account number,
# and the label derived from one of them (see [api.CustomerInfo.label]). A diagnostics bundle
# is routinely attached to a public issue, so none of them may leave in plain text.
_CUSTOMER_KEYS = (CONF_CUSTOMER_LABEL, CONF_CUSTOMER_ADDRESS, CONF_CUSTOMER_ACCOUNT_ID)
REDACT_KEYS = {CONF_ENCRYPTED_REFRESH_BLOB, CONF_PROXY_TOKEN, *_CUSTOMER_KEYS}

# Atom `<title>` text in the raw feed. Custodians commonly put the service address in a
# UsagePoint entry's title, and the parser never reads titles, so blanking them costs nothing.
_ATOM_TITLE_RE = re.compile(r"(<(?:\w+:)?title\b[^>]*>)[^<]+(</(?:\w+:)?title>)")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return the diagnostics payload for one Open Green Button config entry."""
    coord: GreenButtonCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    debug_enabled = _LOGGER.isEnabledFor(logging.DEBUG)

    raw_xml_path = xml_cache_path(hass, entry.entry_id)
    raw_xml_bytes, raw_xml_text = await _load_raw_xml(hass, raw_xml_path)
    customer_values = _customer_values(entry)

    coord_state: dict[str, Any] = {"available": False}
    response_summary: dict[str, Any] | None = None
    if coord is not None:
        coord_state = {
            "available": True,
            "last_update_success": coord.last_update_success,
            "last_exception": _scrub(_safe_str(coord.last_exception), customer_values),
            "update_interval_seconds": (
                coord.update_interval.total_seconds() if coord.update_interval else None
            ),
        }
        response_summary = _summarize_response(coord.data)

    return {
        "entry": {
            "entry_id": entry.entry_id,
            "title": _scrub(entry.title, customer_values),
            "data": async_redact_data(dict(entry.data), REDACT_KEYS),
            "options": dict(entry.options),
        },
        "coordinator": coord_state,
        "last_response": response_summary,
        "raw_xml_cached_bytes": raw_xml_bytes,
        "raw_xml_path": raw_xml_path,
        "raw_xml_debug_logging_enabled": debug_enabled,
        "raw_xml": _redact_xml(raw_xml_text, customer_values),
    }


def _summarize_response(response: UsageResponse | None) -> dict[str, Any] | None:
    """Compact view of the last UsageResponse — drops raw readings (too noisy for diagnostics)
    but keeps the metadata + per-summary cost detail breakdown that the user is likely
    actually trying to inspect."""
    if response is None:
        return None
    return {
        "updated": response.updated.isoformat() if response.updated else None,
        "usage_points": [
            {
                "usage_point_id": up.usage_point_id,
                "service_kind": up.service_kind,
                "series": [
                    {
                        "meter_reading_id": s.meter_reading_id,
                        "reading_type": asdict(s.reading_type),
                        "reading_count": len(s.readings),
                        "first_reading_start": (
                            s.readings[0].start.isoformat() if s.readings else None
                        ),
                        "last_reading_start": (
                            s.readings[-1].start.isoformat() if s.readings else None
                        ),
                    }
                    for s in up.series
                ],
                "summaries": [
                    {
                        "billing_period_start": summary.billing_period_start.isoformat(),
                        "billing_period_duration_seconds": summary.billing_period_duration_seconds,
                        "bill_last_period_raw": summary.bill_last_period_raw,
                        "cost_additional_last_period_raw": summary.cost_additional_last_period_raw,
                        "currency_numeric_code": summary.currency_numeric_code,
                        "total_cost": summary.total_cost,
                        "cost_details": [asdict(d) for d in summary.cost_details],
                    }
                    for summary in up.summaries
                ],
            }
            for up in response.usage_points
        ],
        "new_credentials_present": response.new_credentials is not None,
    }


def _customer_values(entry: ConfigEntry) -> list[str]:
    """The stored customer strings, longest first so a value containing another goes whole."""
    values = {v for k in _CUSTOMER_KEYS if isinstance(v := entry.data.get(k), str) and v}
    return sorted(values, key=len, reverse=True)


def _scrub(text: str | None, customer_values: list[str]) -> str | None:
    """Replace any verbatim occurrence of a customer value in free text (the entry title embeds
    the label — see [coordinator.GreenButtonCoordinator._store_customer_label])."""
    if text is None:
        return None
    for value in customer_values:
        text = text.replace(value, REDACTED)
    return text


def _redact_xml(xml: str | None, customer_values: list[str]) -> str | None:
    """Blank Atom titles and any verbatim customer value in the cached usage feed."""
    if xml is None:
        return None
    return _scrub(_ATOM_TITLE_RE.sub(rf"\g<1>{REDACTED}\g<2>", xml), customer_values)


async def _load_raw_xml(
    hass: HomeAssistant,
    path: str,
) -> tuple[int | None, str | None]:
    """Read the cached XML file off the event loop. Returns (size_bytes, decoded_text).

    Returns (None, None) when the file doesn't exist (debug never enabled or never ran a
    refresh while it was enabled). UTF-8 decoded with ``errors="replace"`` so a truncated
    file from a crash mid-write doesn't break the diagnostics download — the replacement
    character makes the damage visible in the output.
    """

    def _read_sync() -> tuple[int | None, str | None]:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return None, None
        return len(data), data.decode("utf-8", errors="replace")

    return await hass.async_add_executor_job(_read_sync)


def _safe_str(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(value)
    except Exception:  # noqa: BLE001 — diagnostics should never raise on a stringification edge case
        return repr(value)


async def async_remove_xml_cache(hass: HomeAssistant, entry_id: str) -> None:
    """Best-effort delete of the per-entry XML cache file.

    Called from `async_remove_entry` so a removed entry doesn't leave stale debug XML on
    disk indefinitely. Tolerates an absent file (the common case — debug was never on).
    """
    path = xml_cache_path(hass, entry_id)

    def _delete_sync() -> None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)

    await hass.async_add_executor_job(_delete_sync)
