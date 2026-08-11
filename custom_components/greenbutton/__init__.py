"""The Open Green Button integration.

Phase 3.3: config entry setup spins up a [GreenButtonCoordinator] that polls the proxy and
appends to HA long-term statistics. The integration owns no sensor entities — its only
output is statistics surfaced via the Energy dashboard.

Two hard invariants enforced here, both about cleanly supporting multiple config entries on
the same utility (sandbox / test account beside a real account, or multi-meter homes):

1. **Statistic IDs are scoped per config entry.** See [statistics.statistic_id_for_series].
   A single user can legitimately have two entries against the same utility, and unscoped
   IDs would alias them in the Energy dashboard.
2. **``async_remove_entry`` purges statistics owned by the removed entry.** HA does NOT
   auto-purge external statistics on config-entry deletion — without this hook, uninstalling
   or re-adding the integration leaves orphaned rows that can only be cleared manually via
   DevTools → Statistics. The test → real account swap relies on this working.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.core import CoreState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import OpenGbApi
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_DAILY_POLL_TIME,
    CONF_DAILY_POLL_TIME_ENABLED,
    CONF_LAST_FETCHED_AT,
    CONF_SERVER_BASE_URL,
    DAILY_CADENCE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SERVER_BASE_URL,
    DOMAIN,
    SERVICE_REBUILD_STATISTICS,
)
from .coordinator import GreenButtonCoordinator
from .diagnostics import async_remove_xml_cache
from .statistics import async_clear_statistics_for_entry

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

_REBUILD_STATISTICS_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})

_LOGGER = logging.getLogger(__name__)


def _configured_daily_poll_time(entry: ConfigEntry, poll_interval: timedelta) -> time | None:
    """The user's local poll time, if one applies to this entry's cadence.

    The server-supplied cadence stays authoritative: a wall-clock time can only change the
    *phase* of an already-daily schedule, never turn a six-hour or multi-day cadence into a
    daily one. Anything unparseable falls back to interval scheduling with a warning rather
    than failing setup.
    """
    if poll_interval != DAILY_CADENCE or not entry.options.get(CONF_DAILY_POLL_TIME_ENABLED):
        return None

    raw = entry.options.get(CONF_DAILY_POLL_TIME)
    parsed = dt_util.parse_time(raw) if isinstance(raw, str) else None
    if parsed is None:
        _LOGGER.warning(
            "Ignoring invalid daily poll time for entry %s (%r); falling back to every %s",
            entry.entry_id,
            raw,
            poll_interval,
        )
    return parsed


def _previous_daily_occurrence(now: datetime, at: time) -> datetime:
    """The most recent moment the local wall-clock time ``at`` went past, as UTC.

    Built from a naive local datetime so it follows HA's timezone through DST rather than
    doing arithmetic on a fixed offset.
    """
    local_today = dt_util.as_local(now).date()
    todays = dt_util.as_utc(datetime.combine(local_today, at))
    if todays <= now:
        return todays
    return dt_util.as_utc(datetime.combine(local_today - timedelta(days=1), at))


def _poll_is_due(entry: ConfigEntry, poll_interval: timedelta, daily_at: time | None) -> bool:
    """Whether the schedule says a fetch should already have happened.

    Only consulted while HA is starting, to decide whether the setup-time fetch is doing real
    work. An entry that has never polled, one whose interval has elapsed, and one that was
    down at its wall-clock time are all due; an entry that polled within its own cadence is
    not, and its data is already in the recorder.
    """
    raw = entry.data.get(CONF_LAST_FETCHED_AT)
    last_fetched = dt_util.parse_datetime(raw) if isinstance(raw, str) else None
    if last_fetched is None:
        return True

    now = dt_util.utcnow()
    if daily_at is None:
        return now - last_fetched >= poll_interval
    return last_fetched < _previous_daily_occurrence(now, daily_at)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an Open Green Button config entry.

    Creates a coordinator, fetches when the schedule says a fetch is owed (which will trigger
    reauth if the persisted refresh token has been revoked while HA was down), and stashes the
    coordinator in ``hass.data`` for diagnostics.
    """
    api = OpenGbApi(
        session=async_get_clientsession(hass),
        server_base_url=entry.data.get(CONF_SERVER_BASE_URL, DEFAULT_SERVER_BASE_URL),
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    # The server's per-utility cadence, not a fixed daily tick — see the poll timer below.
    poll_interval = coordinator.update_interval or DEFAULT_SCAN_INTERVAL
    daily_poll_time = _configured_daily_poll_time(entry, poll_interval)

    # First refresh raises ConfigEntryAuthFailed → HA opens a reauth notification. Any other
    # failure becomes ConfigEntryNotReady → HA retries with backoff.
    #
    # Skipped only on an HA *restart* that lands inside the current polling window: the usage
    # for that window is already in the recorder, so the fetch would ask the utility to resend
    # what we just read, on the startup critical path. A first install and a manual reload run
    # with `hass` already running and always fetch; so does a restart after a poll was missed —
    # including one missed because HA was down at the daily wall-clock time. That keeps this
    # the place a token revoked while HA was down still surfaces, and keeps the coordinator's
    # import-logic repair (CONF_IMPORT_LOGIC_REVISION) running at startup.
    if hass.state is CoreState.running or _poll_is_due(entry, poll_interval, daily_poll_time):
        await coordinator.async_config_entry_first_refresh()
    else:
        _LOGGER.info(
            "Entry %s polled at %s, within its %s cadence — skipping the startup fetch and "
            "waiting for the next scheduled poll",
            entry.entry_id,
            entry.data.get(CONF_LAST_FETCHED_AT),
            f"daily {daily_poll_time.isoformat()} local" if daily_poll_time else poll_interval,
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # This integration owns no entities, so nothing subscribes to the coordinator. HA's
    # DataUpdateCoordinator only arms its internal poll timer when it has ≥1 listener AND the
    # entry's `pref_disable_polling` is off (update_coordinator._schedule_refresh). Relying on
    # that gated scheduler is fragile for a poll-only integration — a stray "disable polling"
    # system-option silently stops all data updates. So we drive the periodic refresh
    # ourselves with a schedule HA can't turn off. Any fetch owed at setup already ran above;
    # this covers every fetch after.
    async def _async_poll(now) -> None:
        _LOGGER.debug("Periodic poll firing for entry %s (scheduled tick %s)", entry.entry_id, now)
        await coordinator.async_refresh()

    if daily_poll_time is not None:
        entry.async_on_unload(
            async_track_time_change(
                hass,
                _async_poll,
                hour=daily_poll_time.hour,
                minute=daily_poll_time.minute,
                second=daily_poll_time.second,
            )
        )
        schedule = f"daily at {daily_poll_time.isoformat()} local time"
    else:
        entry.async_on_unload(async_track_time_interval(hass, _async_poll, poll_interval))
        schedule = f"every {poll_interval}"
    # Emitted once, immediately, on every successful setup. If you do NOT see this line in the
    # log right after an HA (full) restart, the running code is stale — the deployed files or the
    # loaded module predate the timer. A config-entry *reload* is not enough; Python caches the
    # module, so only a full HA restart re-imports this file.
    _LOGGER.info("Armed periodic poll for entry %s: %s", entry.entry_id, schedule)

    # NOTE: deliberately NO `add_update_listener(...reload...)` here. The coordinator writes
    # bookkeeping (CONF_LAST_FETCHED_AT, rotated credentials) into entry.data on every poll;
    # a blanket reload-on-update listener would tear the entry down and re-set it up on each
    # of those writes. Reauth reloads itself via the config flow's
    # `async_update_reload_and_abort`, and the polling options flow subclasses
    # OptionsFlowWithReload, which reloads only when the user saves the form — so nothing
    # here needs a reload on data change.
    _async_register_services(hass)
    _LOGGER.info(
        "Set up Open Green Button entry %s for utility %s",
        entry.entry_id,
        entry.data.get("utility_id"),
    )
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration-wide services once (idempotent across multiple entries).

    Services are global, not per-entry, so the first entry to set up registers them and later
    entries no-op. We deliberately don't deregister on unload: HA services normally persist
    for the integration's lifetime, and the handler already validates targets at call time.
    """
    if hass.services.has_service(DOMAIN, SERVICE_REBUILD_STATISTICS):
        return

    async def _handle_rebuild_statistics(call: ServiceCall) -> None:
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        coordinators: dict[str, GreenButtonCoordinator] = hass.data.get(DOMAIN, {})
        if entry_id is not None:
            coordinator = coordinators.get(entry_id)
            if coordinator is None:
                raise ServiceValidationError(
                    f"No loaded Open Green Button account with config entry id {entry_id!r}"
                )
            targets = [coordinator]
        else:
            targets = list(coordinators.values())
            if not targets:
                raise ServiceValidationError("No loaded Open Green Button accounts to rebuild")
        for coordinator in targets:
            await coordinator.async_rebuild_statistics()

    hass.services.async_register(
        DOMAIN,
        SERVICE_REBUILD_STATISTICS,
        _handle_rebuild_statistics,
        schema=_REBUILD_STATISTICS_SCHEMA,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Open Green Button config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Purge every long-term statistic owned by this entry.

    Without this, uninstalling the integration (or removing one entry of several) leaves
    the rows in ``statistics`` / ``statistics_meta`` indefinitely — they're invisible to
    the integration UI but show up in DevTools → Statistics and pollute the dashboard.
    """
    # Drop any cached debug XML for this entry regardless of whether the entry has stats
    # to clear — happens before the stats purge so a stats-purge exception doesn't strand
    # the file on disk.
    await async_remove_xml_cache(hass, entry.entry_id)

    # Drop any background-load repair issue this entry raised — HA doesn't auto-clear custom
    # integration issues on entry removal, so it would otherwise linger in Settings → Repairs.
    from homeassistant.helpers import issue_registry as ir

    ir.async_delete_issue(hass, DOMAIN, f"background_load_{entry.entry_id}")

    owned = await async_clear_statistics_for_entry(hass, entry.entry_id)
    if owned:
        _LOGGER.info(
            "Purged %d statistic(s) for removed entry %s: %s",
            len(owned),
            entry.entry_id,
            owned,
        )
