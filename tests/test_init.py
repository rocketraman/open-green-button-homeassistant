"""Integration-level tests — the `rebuild_statistics` service dispatch.

The service handler resolves targets from ``hass.data[DOMAIN]`` at call time, so these tests
register the service directly and stub the coordinators, keeping the focus on target
selection and validation (the rebuild mechanics themselves live in test_coordinator.py).
"""

from __future__ import annotations

import logging
import zoneinfo
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.greenbutton import (
    _async_register_services,
    _configured_daily_poll_time,
    _previous_daily_occurrence,
)
from custom_components.greenbutton.api import (
    CustomerResponse,
    MeterReadingSeries,
    NormalizedReadingType,
    UsagePoint,
    UsageReading,
    UsageResponse,
)
from custom_components.greenbutton.const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_DAILY_POLL_TIME,
    CONF_DAILY_POLL_TIME_ENABLED,
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_LAST_FETCHED_AT,
    CONF_POLL_INTERVAL_SECONDS,
    CONF_PROXY_TOKEN,
    CONF_UTILITY_ID,
    CONF_UTILITY_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_REBUILD_STATISTICS,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant


def _stub_coordinator() -> MagicMock:
    coord = MagicMock()
    coord.async_rebuild_statistics = AsyncMock()
    return coord


def _entry(**extra_data) -> MockConfigEntry:
    """A minimal authorized entry; `extra_data` adds/overrides entry.data keys."""
    options = extra_data.pop("options", None)
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_UTILITY_ID: "example_utility",
            CONF_UTILITY_NAME: "Example Utility",
            CONF_ENCRYPTED_REFRESH_BLOB: "blob",
            CONF_PROXY_TOKEN: "token",
            **extra_data,
        },
        options=options or {},
    )


@contextmanager
def _stub_network(fetch: AsyncMock) -> Iterator[None]:
    """Patch every network/recorder touch point setup would otherwise reach."""
    with (
        patch("custom_components.greenbutton.OpenGbApi.fetch_usage", new=fetch),
        # The coordinator opportunistically fetches customer data once to label the entry;
        # stub it so setup doesn't reach the network.
        patch(
            "custom_components.greenbutton.OpenGbApi.fetch_customer",
            new=AsyncMock(return_value=CustomerResponse(customer=None, new_credentials=None)),
        ),
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
    ):
        yield


def _ok_fetch() -> AsyncMock:
    """A fetch that returns usable data.

    It has to carry at least one reading, not just parse: an account that has never received a
    single reading is now re-checked on its own backoff timer (see
    [coordinator.GreenButtonCoordinator._reconcile_data_availability]), and that extra timer would
    land inside the windows these scheduling tests advance through and be miscounted as a poll.
    Which is the right behaviour to have — it just isn't what any test in this file is measuring.
    """
    reading_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="DELTA_DATA",
        interval_length_seconds=3600,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    reading = UsageReading(
        start=datetime(2026, 7, 5, 5, tzinfo=UTC), duration_seconds=3600, value=1000.0
    )
    series = MeterReadingSeries(
        meter_reading_id="mr1", reading_type=reading_type, readings=[reading]
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[series])
    response = UsageResponse(updated=None, usage_points=[up], new_credentials=None)
    return AsyncMock(return_value=response)


def _ago(**kwargs) -> str:
    """An ISO `last_fetched_at` that far in the past."""
    return (dt_util.utcnow() - timedelta(**kwargs)).isoformat()


async def test_setup_polls_on_interval(hass: HomeAssistant) -> None:
    """Regression: the entity-less integration must keep polling on its own timer.

    It owns no entities, so HA's DataUpdateCoordinator won't self-schedule (its poll timer is
    gated on having a listener AND `pref_disable_polling` being off). Setup therefore drives
    the refresh with an explicit time interval. We assert real behaviour: a first fetch at
    setup, then another fetch once the scan interval elapses.
    """
    entry = _entry()
    entry.add_to_hass(hass)

    fetch = _ok_fetch()
    with _stub_network(fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert fetch.await_count == 1  # async_config_entry_first_refresh

        # Advance past the scan interval → the explicit time-interval poll must fire.
        async_fire_time_changed(
            hass, dt_util.utcnow() + DEFAULT_SCAN_INTERVAL + timedelta(minutes=1)
        )
        await hass.async_block_till_done()
        assert fetch.await_count == 2, "periodic poll did not fire on the scan interval"


async def test_setup_completes_when_the_utility_is_still_preparing_data(
    hass: HomeAssistant,
) -> None:
    """A 202 must not block setup — and the entry must retry in minutes, not at its cadence.

    Refusing to finish setup would leave the entry showing as broken and, more damagingly, would
    leave the poll timer unarmed: HA never reaches it. The coordinator's own short retry is what
    carries a deferred fetch, so assert it actually fires well inside the (daily) poll interval.
    """
    from custom_components.greenbutton.api import OpenGbDataPendingError
    from custom_components.greenbutton.const import PENDING_RETRY_INTERVAL

    entry = _entry()
    entry.add_to_hass(hass)

    fetch = AsyncMock(side_effect=OpenGbDataPendingError("data pending (202)"))
    with _stub_network(fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # Each deferred poll is the subscription fetch plus one attempt to collect the prepared
        # batch (here: the UsagePoint listing, which this stub also defers), so count polls by
        # the calls that carry no resource_path rather than by the raw total.
        polls = [c for c in fetch.await_args_list if c.kwargs.get("resource_path") is None]
        assert len(polls) == 1

        # Far short of DEFAULT_SCAN_INTERVAL — only the pending-retry timer can fire here.
        async_fire_time_changed(
            hass, dt_util.utcnow() + PENDING_RETRY_INTERVAL + timedelta(seconds=30)
        )
        await hass.async_block_till_done()
        polls = [c for c in fetch.await_args_list if c.kwargs.get("resource_path") is None]
        assert len(polls) == 2, "deferred fetch was not re-attempted on the short timer"

    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_still_fails_for_errors_that_are_not_a_deferred_fetch(
    hass: HomeAssistant,
) -> None:
    """The 202 escape hatch must not swallow genuine "can't start" failures."""
    from custom_components.greenbutton.api import OpenGbApiError

    entry = _entry()
    entry.add_to_hass(hass)

    with _stub_network(AsyncMock(side_effect=OpenGbApiError("proxy exploded"))):
        assert not await hass.config_entries.async_setup(entry.entry_id)


async def test_poll_timer_uses_the_server_supplied_cadence(hass: HomeAssistant) -> None:
    """Regression: the poll timer must honour the utility's cadence, not a fixed daily tick.

    `poll_interval_seconds` comes from the claim response and drives the coordinator's
    update_interval — but the coordinator's own scheduler is deliberately never armed here,
    so a timer hard-coded to DEFAULT_SCAN_INTERVAL would make that server value dead config
    and under-poll every sub-daily utility.
    """
    six_hours = timedelta(hours=6)
    entry = _entry(**{CONF_POLL_INTERVAL_SECONDS: six_hours.total_seconds()})
    entry.add_to_hass(hass)

    fetch = _ok_fetch()
    with _stub_network(fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert fetch.await_count == 1

        async_fire_time_changed(hass, dt_util.utcnow() + six_hours + timedelta(minutes=1))
        await hass.async_block_till_done()
        assert fetch.await_count == 2, "poll did not fire on the six-hour server cadence"


async def test_restart_inside_a_polled_window_skips_the_startup_fetch(
    hass: HomeAssistant,
) -> None:
    """A restart that lands mid-window must not re-ask the utility for what we already have."""
    hass.set_state(CoreState.starting)
    entry = _entry(**{CONF_LAST_FETCHED_AT: _ago(hours=2)})
    entry.add_to_hass(hass)

    fetch = _ok_fetch()
    with _stub_network(fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    fetch.assert_not_awaited()


async def test_restart_after_a_missed_interval_fetches_and_surfaces_reauth(
    hass: HomeAssistant,
) -> None:
    """A restart with a poll owed still fetches — that's where a revoked token surfaces."""
    hass.set_state(CoreState.starting)
    entry = _entry(**{CONF_LAST_FETCHED_AT: _ago(days=3)})
    entry.add_to_hass(hass)

    fetch = AsyncMock(side_effect=ConfigEntryAuthFailed("revoked"))
    with _stub_network(fetch):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    fetch.assert_awaited_once()
    reauths = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == SOURCE_REAUTH
    ]
    assert len(reauths) == 1, "reauth did not surface at startup for a revoked token"


async def test_manual_reload_always_fetches(hass: HomeAssistant) -> None:
    """With HA running, setup is an explicit user action — reload means refresh now."""
    hass.set_state(CoreState.running)
    entry = _entry(**{CONF_LAST_FETCHED_AT: _ago(minutes=1)})
    entry.add_to_hass(hass)

    fetch = _ok_fetch()
    with _stub_network(fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    fetch.assert_awaited_once()


async def test_restart_after_a_missed_daily_time_catches_up(hass: HomeAssistant) -> None:
    """`async_track_time_change` has no catch-up, so a poll missed while down runs at startup."""
    hass.set_state(CoreState.starting)
    poll_at = time(6, 0)
    # Last poll was one full cycle before the most recent 06:00 — that 06:00 was missed.
    missed = _previous_daily_occurrence(dt_util.utcnow(), poll_at)
    entry = _entry(
        options={
            CONF_DAILY_POLL_TIME_ENABLED: True,
            CONF_DAILY_POLL_TIME: poll_at.isoformat(),
        },
        **{CONF_LAST_FETCHED_AT: (missed - timedelta(hours=1)).isoformat()},
    )
    entry.add_to_hass(hass)

    fetch = _ok_fetch()
    with _stub_network(fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    fetch.assert_awaited_once()


async def test_restart_after_todays_daily_poll_skips(hass: HomeAssistant) -> None:
    """Restarting later the same day, after that day's poll already ran, must not re-fetch."""
    hass.set_state(CoreState.starting)
    poll_at = time(6, 0)
    ran = _previous_daily_occurrence(dt_util.utcnow(), poll_at)
    entry = _entry(
        options={
            CONF_DAILY_POLL_TIME_ENABLED: True,
            CONF_DAILY_POLL_TIME: poll_at.isoformat(),
        },
        **{CONF_LAST_FETCHED_AT: (ran + timedelta(seconds=30)).isoformat()},
    )
    entry.add_to_hass(hass)

    fetch = _ok_fetch()
    with _stub_network(fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    fetch.assert_not_awaited()


async def test_enabled_daily_option_arms_the_wall_clock_timer(hass: HomeAssistant) -> None:
    """An enabled daily option schedules on HA's DST-aware wall-clock helper, not an interval."""
    entry = _entry(
        options={
            CONF_DAILY_POLL_TIME_ENABLED: True,
            CONF_DAILY_POLL_TIME: "06:15:30",
        },
    )
    entry.add_to_hass(hass)

    with (
        _stub_network(_ok_fetch()),
        patch("custom_components.greenbutton.async_track_time_change") as track_change,
        patch("custom_components.greenbutton.async_track_time_interval") as track_interval,
    ):
        track_change.return_value = MagicMock()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    track_change.assert_called_once()
    assert track_change.call_args.kwargs == {"hour": 6, "minute": 15, "second": 30}
    track_interval.assert_not_called()


def test_daily_option_cannot_override_a_non_daily_cadence() -> None:
    """A saved wall-clock time must never speed up or slow down a utility's own cadence."""
    entry = MagicMock()
    entry.options = {
        CONF_DAILY_POLL_TIME_ENABLED: True,
        CONF_DAILY_POLL_TIME: "06:00:00",
    }
    assert _configured_daily_poll_time(entry, timedelta(hours=6)) is None
    assert _configured_daily_poll_time(entry, timedelta(days=2)) is None
    assert _configured_daily_poll_time(entry, timedelta(days=1)) == time(6, 0)


def test_invalid_daily_poll_time_falls_back_to_the_interval() -> None:
    """A corrupt options value degrades to interval scheduling instead of failing setup."""
    entry = MagicMock()
    entry.options = {CONF_DAILY_POLL_TIME_ENABLED: True, CONF_DAILY_POLL_TIME: "not a time"}
    assert _configured_daily_poll_time(entry, timedelta(days=1)) is None

    entry.options = {CONF_DAILY_POLL_TIME_ENABLED: True, CONF_DAILY_POLL_TIME: None}
    assert _configured_daily_poll_time(entry, timedelta(days=1)) is None


def test_previous_daily_occurrence_is_local_and_dst_aware() -> None:
    """The boundary is a *local* wall-clock time; the UTC instant shifts across a DST change."""
    at = time(6, 0)
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(zoneinfo.ZoneInfo("America/Toronto"))
    try:
        # Toronto is UTC−4 in April: 06:00 local is 10:00Z.
        before = datetime(2026, 4, 2, 9, 30, tzinfo=UTC)
        after = datetime(2026, 4, 2, 10, 30, tzinfo=UTC)
        assert _previous_daily_occurrence(before, at) == datetime(2026, 4, 1, 10, tzinfo=UTC)
        assert _previous_daily_occurrence(after, at) == datetime(2026, 4, 2, 10, tzinfo=UTC)

        # UTC−5 in January: the same local time is 11:00Z, an hour later in absolute terms.
        winter = datetime(2026, 1, 15, 12, tzinfo=UTC)
        assert _previous_daily_occurrence(winter, at) == datetime(2026, 1, 15, 11, tzinfo=UTC)
    finally:
        dt_util.set_default_time_zone(original)


async def test_service_is_registered(hass: HomeAssistant) -> None:
    """_async_register_services exposes greenbutton.rebuild_statistics."""
    _async_register_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_REBUILD_STATISTICS)


async def test_service_targets_a_single_entry(hass: HomeAssistant) -> None:
    """A config_entry_id rebuilds only that account."""
    coord_a, coord_b = _stub_coordinator(), _stub_coordinator()
    hass.data[DOMAIN] = {"entry_a": coord_a, "entry_b": coord_b}
    _async_register_services(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_REBUILD_STATISTICS,
        {ATTR_CONFIG_ENTRY_ID: "entry_a"},
        blocking=True,
    )

    coord_a.async_rebuild_statistics.assert_awaited_once()
    coord_b.async_rebuild_statistics.assert_not_awaited()


async def test_service_without_target_rebuilds_all_entries(hass: HomeAssistant) -> None:
    """Omitting the target rebuilds every loaded account."""
    coord_a, coord_b = _stub_coordinator(), _stub_coordinator()
    hass.data[DOMAIN] = {"entry_a": coord_a, "entry_b": coord_b}
    _async_register_services(hass)

    await hass.services.async_call(DOMAIN, SERVICE_REBUILD_STATISTICS, {}, blocking=True)

    coord_a.async_rebuild_statistics.assert_awaited_once()
    coord_b.async_rebuild_statistics.assert_awaited_once()


async def test_service_rejects_unknown_entry(hass: HomeAssistant) -> None:
    """An unknown config_entry_id is a user error, not a silent no-op."""
    hass.data[DOMAIN] = {"entry_a": _stub_coordinator()}
    _async_register_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REBUILD_STATISTICS,
            {ATTR_CONFIG_ENTRY_ID: "does_not_exist"},
            blocking=True,
        )


async def test_service_errors_when_no_entries_loaded(hass: HomeAssistant) -> None:
    """With nothing configured, an untargeted call reports there's nothing to do."""
    hass.data[DOMAIN] = {}
    _async_register_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, SERVICE_REBUILD_STATISTICS, {}, blocking=True)


async def test_setup_logs_the_integration_version(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The setup line names the running version.

    A pasted log excerpt is the commonest thing a bug report carries, and on its own it doesn't
    say which build produced it. In issues/10 the affected client version had to be inferred from
    poll cadence in server-side logs, which cost days. Assert against manifest.json rather than a
    literal so a release bump can't silently make the log disagree with what's installed.
    """
    import json
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).parent.parent / "custom_components/greenbutton/manifest.json").read_text()
    )

    entry = _entry()
    entry.add_to_hass(hass)

    empty = UsageResponse(updated=None, usage_points=[], new_credentials=None)
    fetch = AsyncMock(return_value=empty)
    with (
        _stub_network(fetch),
        caplog.at_level(logging.INFO, logger="custom_components.greenbutton"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    setup_lines = [
        r.getMessage() for r in caplog.records if "Set up Open Green Button" in r.getMessage()
    ]
    assert setup_lines, "no setup line was logged"
    assert manifest["version"] in setup_lines[0], setup_lines[0]

    await hass.config_entries.async_unload(entry.entry_id)
