"""Coordinator behaviour tests.

We mock the API client at the boundary (its async methods) and `import_usage_statistics`
so these tests don't need a running recorder — that keeps them fast and the assertions
focused on the lifecycle decisions the coordinator makes (which error → which HA failure
mode, when to persist rotated credentials, etc.).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.greenbutton.api import (
    CustomerInfo,
    CustomerResponse,
    MeterReadingSeries,
    NewCredentials,
    NormalizedReadingType,
    OpenGbApi,
    OpenGbApiError,
    OpenGbAuthExpiredError,
    OpenGbPermanentError,
    UsagePoint,
    UsageReading,
    UsageResponse,
)
from custom_components.greenbutton.const import (
    CONF_CUSTOMER_LABEL,
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_IMPORT_LOGIC_REVISION,
    CONF_LAST_FETCHED_AT,
    CONF_PENDING_PUBLISHED_MAX,
    CONF_PENDING_PUBLISHED_MIN,
    CONF_POLL_INTERVAL_SECONDS,
    CONF_PROXY_TOKEN,
    CONF_USAGE_POINT_CURSORS,
    CONF_UTILITY_ID,
    CONF_UTILITY_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    IMPORT_LOGIC_REVISION,
    INITIAL_FETCH_LOOKBACK,
    LAST_FETCHED_OVERLAP,
)
from custom_components.greenbutton.coordinator import GreenButtonCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_UTILITY_ID: "example_utility",
            CONF_UTILITY_NAME: "Example Utility",
            CONF_ENCRYPTED_REFRESH_BLOB: "original_blob",
            CONF_PROXY_TOKEN: "original_token",
            # Mark customer-labeling as already resolved so the shared entry doesn't trigger a
            # (real) fetch_customer during these lifecycle tests. The dedicated customer-label
            # tests use a fresh entry without this key.
            CONF_CUSTOMER_LABEL: "",
        },
    )
    entry.add_to_hass(hass)
    return entry


def _empty_response() -> UsageResponse:
    """A valid-shape UsageResponse with no readings — keeps stats writes trivial in tests."""
    return UsageResponse(updated=None, usage_points=[], new_credentials=None)


def _reading_type() -> NormalizedReadingType:
    return NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="DELTA_DATA",
        interval_length_seconds=3600,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )


def _response_with_readings(*starts: datetime, cost: float | None = None) -> UsageResponse:
    """A UsageResponse carrying one FORWARD series with a reading at each given start."""
    readings = [
        UsageReading(start=s, duration_seconds=3600, value=1000.0, cost=cost) for s in starts
    ]
    series = MeterReadingSeries(
        meter_reading_id="mr1", reading_type=_reading_type(), readings=readings
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[series])
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


def _response_with_meters(meters: dict[str, list[datetime]]) -> UsageResponse:
    """A UsageResponse carrying one FORWARD series per named UsagePoint (one meter each)."""
    usage_points = [
        UsagePoint(
            usage_point_id=up_id,
            service_kind="electricity",
            series=[
                MeterReadingSeries(
                    meter_reading_id=f"mr-{up_id}",
                    reading_type=_reading_type(),
                    readings=[
                        UsageReading(start=s, duration_seconds=3600, value=1000.0, cost=None)
                        for s in starts
                    ],
                )
            ],
        )
        for up_id, starts in meters.items()
    ]
    return UsageResponse(updated=None, usage_points=usage_points, new_credentials=None)


async def test_first_refresh_calls_api_and_imports_stats(hass: HomeAssistant) -> None:
    """Happy path: coordinator fetches, then hands the response to the stats importer."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    response = _empty_response()
    api.fetch_usage = AsyncMock(return_value=response)  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ) as import_mock:
        await coordinator.async_refresh()

    # Both window params are always set — sending neither makes the GBA test-lab harness
    # omit IntervalBlocks. On a first refresh (no CONF_LAST_FETCHED_AT), published_min looks
    # back INITIAL_FETCH_LOOKBACK (2 years); published_max is always now + a small buffer.
    api.fetch_usage.assert_awaited_once()
    call = api.fetch_usage.await_args
    assert call.kwargs["encrypted_refresh_blob"] == "original_blob"
    assert call.kwargs["proxy_token"] == "original_token"  # noqa: S105

    from custom_components.greenbutton.const import INITIAL_FETCH_LOOKBACK

    now = datetime.now(UTC)
    expected_min = now - INITIAL_FETCH_LOOKBACK
    # `published_max` is plain `now` — no forward buffer. A future bound never survived the proxy's
    # clamp anyway, and savagedata rejects one outright.
    expected_max = now
    # Tolerate the few seconds of clock drift between the coordinator and the assertion.
    assert abs((call.kwargs["published_min"] - expected_min).total_seconds()) < 60
    assert abs((call.kwargs["published_max"] - expected_max).total_seconds()) < 60
    import_mock.assert_awaited_once()
    call_args = import_mock.await_args
    assert call_args.args[1] is entry
    assert call_args.args[2] is response
    assert call_args.kwargs == {"utility_display_name": "Example Utility"}
    assert coordinator.last_exception is None


async def test_auth_expired_becomes_config_entry_auth_failed(hass: HomeAssistant) -> None:
    """OpenGbAuthExpiredError → ConfigEntryAuthFailed (triggers HA reauth flow).

    Calls the internal update method directly: ``async_config_entry_first_refresh`` enforces
    an entry-state precondition that we don't satisfy in unit tests (we never call
    ``async_setup_entry``), and ``async_refresh`` swallows exceptions into ``last_exception``
    instead of re-raising. The mapping logic lives in ``_async_update_data`` either way.
    """
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(side_effect=OpenGbAuthExpiredError("expired"))  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ) as import_mock,
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await coordinator._async_update_data()
    import_mock.assert_not_awaited()


async def test_generic_api_error_becomes_update_failed(hass: HomeAssistant) -> None:
    """Non-auth API errors → UpdateFailed (HA retries with backoff, no reauth)."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(side_effect=OpenGbApiError("upstream 502"))  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()


async def test_rotated_credentials_persisted_on_upstream_error(hass: HomeAssistant) -> None:
    """A post-refresh upstream failure still rotates a one-time refresh token. The coordinator
    must persist the rotated blob (carried on the error) even though it raises UpdateFailed —
    otherwise the retry reuses the burned token and cascades into a spurious reauth.
    """
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        side_effect=OpenGbApiError(
            "upstream 502",
            new_credentials=NewCredentials(
                encrypted_refresh_blob="rotated_blob",
                proxy_token="rotated_token",  # noqa: S106
            ),
        ),
    )

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()

    # The burned token was replaced despite the failed refresh, so the next poll uses the fresh one.
    assert entry.data[CONF_ENCRYPTED_REFRESH_BLOB] == "rotated_blob"
    assert entry.data[CONF_PROXY_TOKEN] == "rotated_token"


async def test_data_pending_raises_repair_issue_and_update_failed(hass: HomeAssistant) -> None:
    """A fresh 202 → UpdateFailed, plus a *warning* repair issue that asks nothing of the user.

    A custodian saying "collecting it now" is working as designed for some utilities — an error
    there is a false alarm that makes a normal mode look like a broken integration. The refresh
    still fails (there's no data to import), but the user-facing surface stays calm.
    """
    from homeassistant.helpers import issue_registry as ir

    from custom_components.greenbutton.api import OpenGbDataPendingError
    from custom_components.greenbutton.const import BACKGROUND_LOAD_ISSUE_URL

    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        side_effect=OpenGbDataPendingError("data pending (202)"),
    )

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ) as import_mock,
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()

    import_mock.assert_not_awaited()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"background_load_{entry.entry_id}")
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.translation_key == "background_load_pending"
    assert issue.learn_more_url == BACKGROUND_LOAD_ISSUE_URL
    assert issue.translation_placeholders == {"utility": "Example Utility"}
    coordinator.cancel_pending_retry()


async def test_data_pending_escalates_to_an_error_after_a_day(hass: HomeAssistant) -> None:
    """Once the wait passes PENDING_ESCALATE_AFTER the issue becomes an error worth reporting.

    Same issue_id throughout, so the entry upgrades in place rather than showing two notices.
    """
    from homeassistant.helpers import issue_registry as ir

    from custom_components.greenbutton.api import OpenGbDataPendingError
    from custom_components.greenbutton.const import CONF_PENDING_SINCE, PENDING_ESCALATE_AFTER

    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        side_effect=OpenGbDataPendingError("data pending (202)"),
    )

    entry = _entry(hass)
    stale = datetime.now(UTC) - PENDING_ESCALATE_AFTER - timedelta(minutes=1)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_PENDING_PUBLISHED_MIN: "2024-08-06T20:19:00+00:00",
            CONF_PENDING_PUBLISHED_MAX: "2026-08-06T20:19:00+00:00",
            CONF_PENDING_SINCE: stale.isoformat(),
        },
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"background_load_{entry.entry_id}")
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_key == "background_load_stuck"
    # Past the deadline we stop the five-minute cadence and let the ordinary poll timer carry it,
    # so a custodian that never delivers isn't polled forever.
    assert coordinator._pending_retry_unsub is None


async def test_data_pending_freezes_the_window_and_retries_replay_it(
    hass: HomeAssistant,
) -> None:
    """After a 202, every retry re-asks with the IDENTICAL window.

    This is the Alectra bug in issues/10. A custodian doing ESPI asynchronous batch delivery
    prepares the dataset under the URL it was asked for, so only an identical request can collect
    it. The ordinary window is computed from `now`, so without the freeze each retry moves both
    bounds, enqueues a fresh job, and gets a fresh 202 — forever.
    """
    from custom_components.greenbutton.api import OpenGbDataPendingError

    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        side_effect=OpenGbDataPendingError("data pending (202)"),
    )

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()

    first = api.fetch_usage.await_args.kwargs
    assert entry.data[CONF_PENDING_PUBLISHED_MIN] == first["published_min"].isoformat()

    # What we sent is what gets frozen, and it is never in the future — the proxy clamps a future
    # `published-max` down to its own `now`, so a forward-dated bound would be rewritten to a fresh
    # instant on every retry, pinning `published-min` while the custodian still sees a brand-new
    # URL each time. See test_a_future_published_max_is_frozen_clamped_to_now.
    frozen_max = datetime.fromisoformat(entry.data[CONF_PENDING_PUBLISHED_MAX])
    assert frozen_max == first["published_max"]
    assert frozen_max <= datetime.now(UTC)

    # Two more retries — wall-clock moves on, so an unfrozen window would differ each time.
    for _ in range(2):
        with (
            patch(
                "custom_components.greenbutton.coordinator.import_usage_statistics",
                new=AsyncMock(),
            ),
            pytest.raises(UpdateFailed),
        ):
            await coordinator._async_update_data()

        retry = api.fetch_usage.await_args.kwargs
        assert retry["published_min"] == first["published_min"]
        # Identical AND not in the future: the value the proxy forwards is the value we sent.
        assert retry["published_max"] == frozen_max


async def test_data_pending_logs_the_custodian_detail(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 202's forwarded detail reaches the log: INFO on the first, DEBUG on retries.

    Nothing downstream prints the exception — setup logs its own generic line, and HA's
    coordinator only logs UpdateFailed on a success→failure transition, which a 202 before any
    success never produces. Issues/10 went two weeks without the custodian's response headers
    because of exactly that. The affected user's HA log is the only place this detail can be
    collected from, so losing it means losing the investigation.
    """
    from custom_components.greenbutton.api import OpenGbDataPendingError

    detail = "data pending (202) (response-headers: [X-Powered-By: ASP.NET])"
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        side_effect=OpenGbDataPendingError(detail),
    )

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
        caplog.at_level(logging.DEBUG, logger="custom_components.greenbutton"),
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

        first_records = [r for r in caplog.records if detail in r.getMessage()]
        assert [r.levelno for r in first_records] == [logging.INFO]

        caplog.clear()
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    retry_records = [r for r in caplog.records if detail in r.getMessage()]
    assert [r.levelno for r in retry_records] == [logging.DEBUG]
    coordinator.cancel_pending_retry()


async def test_successful_fetch_clears_the_frozen_pending_window(hass: HomeAssistant) -> None:
    """Once the deferred batch lands, polling returns to a `now`-relative window."""
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_PENDING_PUBLISHED_MIN: "2024-08-06T20:19:00+00:00",
            CONF_PENDING_PUBLISHED_MAX: "2026-08-06T20:19:00+00:00",
        },
    )
    api = _api_returning(_empty_response())
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    # The frozen window was replayed for the fetch that succeeded...
    assert api.fetch_usage.await_args.kwargs["published_min"] == datetime(
        2024, 8, 6, 20, 19, tzinfo=UTC
    )
    # ...and is gone afterwards, so the next poll asks for whatever is new.
    assert CONF_PENDING_PUBLISHED_MIN not in entry.data
    assert CONF_PENDING_PUBLISHED_MAX not in entry.data


async def test_rebuild_ignores_a_frozen_pending_window(hass: HomeAssistant) -> None:
    """A rebuild means "re-fetch everything", not "resume the deferred slice"."""
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_PENDING_PUBLISHED_MIN: "2024-08-06T20:19:00+00:00",
            CONF_PENDING_PUBLISHED_MAX: "2026-08-06T20:19:00+00:00",
        },
    )
    api = _api_returning(_empty_response())
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.greenbutton.coordinator.async_clear_statistics_for_entry",
            new=AsyncMock(return_value=[]),
        ),
    ):
        await coordinator.async_rebuild_statistics(publish=False)

    # The full-history window, not the frozen 2024-08-06 one.
    assert api.fetch_usage.await_args.kwargs["published_min"] != datetime(
        2024, 8, 6, 20, 19, tzinfo=UTC
    )


async def test_successful_refresh_clears_background_load_issue(hass: HomeAssistant) -> None:
    """Once a fetch succeeds, any previously-raised background-load issue is cleared."""
    from homeassistant.helpers import issue_registry as ir

    entry = _entry(hass)
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    coordinator = GreenButtonCoordinator(hass, api, entry)

    # Pre-seed the issue as if a prior poll had hit a 202.
    coordinator._async_create_background_load_issue()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"background_load_{entry.entry_id}") is not None
    )

    # A fetch that actually carries data — a *clean* fetch that carries nothing keeps the issue
    # up (as `no_data_yet`), which is the whole point of the empty-feed path.
    coordinator.api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        return_value=_response_with_readings(datetime(2026, 7, 5, 5, tzinfo=UTC))
    )
    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"background_load_{entry.entry_id}") is None


async def test_clean_fetch_with_no_data_at_all_explains_itself_and_retries_soon(
    hass: HomeAssistant,
) -> None:
    """A brand-new account whose utility returns nothing must not go quiet for a day.

    The issues/43 report. A custodian that assembles data on demand (UtilityAPI, and everything
    behind it) answers the first request after authorization with an ordinary HTTP 200 carrying no
    readings. Nothing is imported, so no statistic metadata is registered, so the Energy dashboard
    offers the user literally nothing — and the only thing that used to happen next was the
    ordinary poll, a day later. The user reads that as a broken integration.
    """
    from homeassistant.helpers import issue_registry as ir

    from custom_components.greenbutton.const import BACKGROUND_LOAD_ISSUE_URL, CONF_EMPTY_SINCE

    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    # The fetch SUCCEEDED — this is not a failed refresh, and must not be reported as one.
    assert coordinator.last_update_success is True
    assert CONF_EMPTY_SINCE in entry.data

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"background_load_{entry.entry_id}")
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.translation_key == "no_data_yet"
    assert issue.learn_more_url == BACKGROUND_LOAD_ISSUE_URL
    assert issue.translation_placeholders == {"utility": "Example Utility"}

    assert coordinator._pending_retry_unsub is not None
    coordinator.cancel_pending_retry()


async def test_empty_wait_widens_and_is_capped_by_the_poll_interval(hass: HomeAssistant) -> None:
    """Each re-check waits as long again as we've already waited, up to the poll cadence.

    Unlike the 202 path — where we're collecting a batch the custodian has already prepared and
    told us about — every attempt here re-asks for the whole initial-history window on nothing more
    than a suspicion. A flat five minutes would mean ~288 full-history queries a day against a
    utility that may simply have nothing to give us.
    """
    from custom_components.greenbutton.const import CONF_EMPTY_SINCE, EMPTY_RETRY_INITIAL

    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    async def _delay_for(
        coord: GreenButtonCoordinator, target: MockConfigEntry, waited: timedelta | None
    ) -> timedelta:
        data = {**target.data}
        data.pop(CONF_EMPTY_SINCE, None)
        if waited is not None:
            data[CONF_EMPTY_SINCE] = (datetime.now(UTC) - waited).isoformat()
        hass.config_entries.async_update_entry(target, data=data)
        coord.cancel_pending_retry()
        with (
            patch(
                "custom_components.greenbutton.coordinator.import_usage_statistics",
                new=AsyncMock(),
            ),
            patch("custom_components.greenbutton.coordinator.async_call_later") as call_later,
        ):
            await coord._async_update_data()
        return call_later.call_args.args[1]

    # First empty poll: the floor.
    assert await _delay_for(coordinator, entry, None) == EMPTY_RETRY_INITIAL
    # Mid-backoff: doubling falls out of "wait as long again", with no attempt counter to persist
    # (so it survives a restart and can't drift out of step with the stamp driving escalation).
    # Approximate because the elapsed time is read off the wall clock inside the call.
    delay = await _delay_for(coordinator, entry, timedelta(minutes=40))
    assert delay.total_seconds() == pytest.approx(2400, abs=5)

    # Capped at the entry's own cadence: re-checking more slowly than the ordinary poll would be a
    # regression, not a backoff. Only reachable on a utility whose server-supplied cadence is
    # shorter than the wait — a daily cadence escalates out first.
    fast_entry = MockConfigEntry(
        domain=DOMAIN,
        data={**entry.data, CONF_POLL_INTERVAL_SECONDS: 6 * 3600, CONF_CUSTOMER_LABEL: ""},
    )
    fast_entry.add_to_hass(hass)
    fast = GreenButtonCoordinator(hass, api, fast_entry)
    assert await _delay_for(fast, fast_entry, timedelta(hours=8)) == timedelta(hours=6)
    fast.cancel_pending_retry()


async def test_a_day_without_data_escalates_and_drops_the_fast_cadence(
    hass: HomeAssistant,
) -> None:
    """Past PENDING_ESCALATE_AFTER: an error worth reporting, and no more extra polling."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.greenbutton.const import CONF_EMPTY_SINCE, PENDING_ESCALATE_AFTER

    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]

    entry = _entry(hass)
    stale = datetime.now(UTC) - PENDING_ESCALATE_AFTER - timedelta(minutes=1)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_EMPTY_SINCE: stale.isoformat()}
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    # The stamp must NOT restart on every empty poll — it's what decides when to stop.
    assert entry.data[CONF_EMPTY_SINCE] == stale.isoformat()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"background_load_{entry.entry_id}")
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_key == "no_data_yet_stuck"
    assert coordinator._pending_retry_unsub is None


async def test_first_data_clears_the_empty_wait(hass: HomeAssistant) -> None:
    """The moment a reading lands, the stamp, the notice and the fast retry all go away."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.greenbutton.const import CONF_EMPTY_SINCE

    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()
        assert CONF_EMPTY_SINCE in entry.data

        coordinator.api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
            return_value=_response_with_readings(datetime(2026, 7, 5, 5, tzinfo=UTC))
        )
        await coordinator._async_update_data()

    assert CONF_EMPTY_SINCE not in entry.data
    assert entry.data[CONF_LAST_FETCHED_AT] == "2026-07-05T05:00:00+00:00"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"background_load_{entry.entry_id}") is None
    assert coordinator._pending_retry_unsub is None


async def test_an_established_entry_polling_into_a_quiet_window_stays_silent(
    hass: HomeAssistant,
) -> None:
    """An empty poll is only remarkable for an account that has NEVER had data.

    Utilities publish on a lag — weekly, or in batches — so an entry that already holds history
    returns empty routinely. Warning about that, or re-polling it every few minutes, would turn
    normal operation into a permanent repair notice.
    """
    from homeassistant.helpers import issue_registry as ir

    from custom_components.greenbutton.const import CONF_EMPTY_SINCE

    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]

    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LAST_FETCHED_AT: "2026-07-04T05:00:00+00:00"}
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    assert CONF_EMPTY_SINCE not in entry.data
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"background_load_{entry.entry_id}") is None
    assert coordinator._pending_retry_unsub is None


async def test_rotated_credentials_are_persisted_before_stats_import(
    hass: HomeAssistant,
) -> None:
    """New credentials must land in entry.data BEFORE the stats import runs.

    Otherwise a stats-write failure mid-refresh leaves HA with the stale token,
    which the next poll then sends to the utility → spurious reauth flow.
    """
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    response = UsageResponse(
        updated=None,
        usage_points=[],
        new_credentials=NewCredentials(
            encrypted_refresh_blob="rotated_blob",
            proxy_token="rotated_token",  # noqa: S106
        ),
    )
    api.fetch_usage = AsyncMock(return_value=response)  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    observed: dict[str, str] = {}

    async def capture_entry_at_import_time(*args, **kwargs):
        observed["blob"] = entry.data[CONF_ENCRYPTED_REFRESH_BLOB]
        observed["token"] = entry.data[CONF_PROXY_TOKEN]

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(side_effect=capture_entry_at_import_time),
    ):
        await coordinator.async_refresh()

    # Entry sees the rotated values both at import time and after the refresh completes.
    assert observed == {"blob": "rotated_blob", "token": "rotated_token"}
    assert entry.data[CONF_ENCRYPTED_REFRESH_BLOB] == "rotated_blob"
    assert entry.data[CONF_PROXY_TOKEN] == "rotated_token"


async def test_rebuild_refetches_full_history_then_purges(hass: HomeAssistant) -> None:
    """Rebuild re-fetches the FULL window (ignoring the cursor), then clears + re-imports.

    A stored `last_fetched_at` would normally scope the next fetch to a small incremental
    slice — the rebuild must override that and pull the whole initial-history window so the
    recomputed statistics cover all history, not just since the last poll. The fetch happens
    before the purge (see test_rebuild_leaves_stats_intact_when_refetch_fails).
    """
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]

    entry = _entry(hass)
    # Simulate an established entry mid-way through incremental polling.
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_LAST_FETCHED_AT: "2026-06-01T00:00:00+00:00"},
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.async_clear_statistics_for_entry",
            new=AsyncMock(return_value=[f"{DOMAIN}:x_cost", f"{DOMAIN}:x_forward"]),
        ) as clear_mock,
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ) as import_mock,
    ):
        await coordinator.async_rebuild_statistics()

    clear_mock.assert_awaited_once_with(hass, entry.entry_id)
    api.fetch_usage.assert_awaited_once()
    import_mock.assert_awaited_once()
    # Rebuild imports from a zero baseline — fresh=True bypasses the resume-point read that a
    # rebuild raced against (stale cursor → every reading skipped → empty store).
    assert import_mock.await_args.kwargs["fresh"] is True

    # published_min looks back the full initial window, NOT to the 2026-06-01 cursor.
    now = datetime.now(UTC)
    expected_min = now - INITIAL_FETCH_LOOKBACK
    published_min = api.fetch_usage.await_args.kwargs["published_min"]
    assert abs((published_min - expected_min).total_seconds()) < 120

    # The one-shot flag is cleared after success → the next scheduled poll is incremental again.
    assert coordinator._force_full_history is False


async def test_rebuild_leaves_stats_intact_when_refetch_fails(hass: HomeAssistant) -> None:
    """A failed re-fetch must NOT purge — the existing statistics stay put.

    Regression guard for the destructive-rebuild bug: the utility's resource server is
    intermittently flaky, and clearing before a fetch that then fails wiped the user's
    history with no way to recover (the incremental window sits ahead of the utility's
    lagged data). Fetching first makes a failed rebuild a no-op, and the caller still gets a
    clear error.
    """
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(side_effect=OpenGbApiError("upstream 502"))  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.async_clear_statistics_for_entry",
            new=AsyncMock(return_value=[f"{DOMAIN}:x_cost"]),
        ) as clear_mock,
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ) as import_mock,
        pytest.raises(HomeAssistantError),
    ):
        await coordinator.async_rebuild_statistics()

    clear_mock.assert_not_awaited()  # fetch failed first → nothing purged
    import_mock.assert_not_awaited()
    # The one-shot flag is cleared even on the failure path.
    assert coordinator._force_full_history is False


async def test_cursor_advances_to_newest_reading_not_wall_clock(hass: HomeAssistant) -> None:
    """The incremental cursor is anchored to the newest reading, never to wall-clock `now`.

    Regression guard for the window-outruns-data bug: if the cursor advanced to `now`, then
    `published-min` (= cursor − overlap) would march past a utility that publishes on a lag,
    and every later poll would return nothing.
    """
    newest = datetime(2026, 7, 4, 5, 0, tzinfo=UTC)
    older = datetime(2026, 7, 3, 5, 0, tzinfo=UTC)
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        return_value=_response_with_readings(older, newest)
    )

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()

    # Cursor is the newest reading start — well behind wall-clock now (2026-07-06).
    assert entry.data[CONF_LAST_FETCHED_AT] == newest.isoformat()


async def test_cursor_not_advanced_on_empty_response(hass: HomeAssistant) -> None:
    """An empty (0-reading) response must leave the cursor pinned to the last real data.

    Otherwise the window would creep forward on every empty poll and permanently outrun a
    lagging utility's not-yet-published data.
    """
    prior = "2026-06-01T00:00:00+00:00"
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]

    entry = _entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_LAST_FETCHED_AT: prior})
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()

    assert entry.data[CONF_LAST_FETCHED_AT] == prior  # unchanged


async def test_cursor_never_regresses_on_late_partial_window(hass: HomeAssistant) -> None:
    """A window that only returns older readings must not pull the cursor backwards."""
    prior_dt = datetime(2026, 7, 4, 5, 0, tzinfo=UTC)
    older = datetime(2026, 7, 2, 5, 0, tzinfo=UTC)
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        return_value=_response_with_readings(older)
    )

    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LAST_FETCHED_AT: prior_dt.isoformat()}
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()

    assert entry.data[CONF_LAST_FETCHED_AT] == prior_dt.isoformat()  # held, not regressed


# ---------------------------------------------------------------------------------------
# Customer-labeling: give two otherwise-identical entries a distinguishable title.
# These use a fresh entry WITHOUT CONF_CUSTOMER_LABEL so the one-time fetch actually runs.
# ---------------------------------------------------------------------------------------


def _fresh_entry(hass: HomeAssistant) -> MockConfigEntry:
    """An entry with no customer label yet — triggers the one-time customer fetch."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Milton Hydro (SANDBOX for testing only)",
        data={
            CONF_UTILITY_ID: "milton_hydro",
            CONF_UTILITY_NAME: "Milton Hydro (SANDBOX for testing only)",
            CONF_ENCRYPTED_REFRESH_BLOB: "original_blob",
            CONF_PROXY_TOKEN: "original_token",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_customer_label_retitles_entry_and_persists_details(hass: HomeAssistant) -> None:
    """First successful refresh fetches customer data and folds a distinguisher into the title."""
    from custom_components.greenbutton.const import (
        CONF_CUSTOMER_ACCOUNT_ID,
        CONF_CUSTOMER_ADDRESS,
    )

    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]
    api.fetch_customer = AsyncMock(  # type: ignore[method-assign]
        return_value=CustomerResponse(
            customer=CustomerInfo(
                account_id="100001-0000001",
                service_address="123 EXAMPLE ST, MILTON ON, L0L 0L0",
                customer_name=None,
            ),
            new_credentials=None,
        )
    )

    entry = _fresh_entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()

    api.fetch_customer.assert_awaited_once()
    # Title gains the service address (the label's first preference).
    assert entry.title == (
        "Milton Hydro (SANDBOX for testing only) — 123 EXAMPLE ST, MILTON ON, L0L 0L0"
    )
    assert entry.data[CONF_CUSTOMER_LABEL] == "123 EXAMPLE ST, MILTON ON, L0L 0L0"
    assert entry.data[CONF_CUSTOMER_ACCOUNT_ID] == "100001-0000001"
    assert entry.data[CONF_CUSTOMER_ADDRESS] == "123 EXAMPLE ST, MILTON ON, L0L 0L0"


async def test_customer_label_fetched_only_once(hass: HomeAssistant) -> None:
    """Once a label is stored, later refreshes don't refetch customer data."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]
    api.fetch_customer = AsyncMock(  # type: ignore[method-assign]
        return_value=CustomerResponse(
            customer=CustomerInfo(account_id="ACC-1", service_address=None, customer_name=None),
            new_credentials=None,
        )
    )

    entry = _fresh_entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()
        await coordinator.async_refresh()

    api.fetch_customer.assert_awaited_once()
    assert entry.title == "Milton Hydro (SANDBOX for testing only) — ACC-1"


async def test_customer_label_permanent_error_marks_unavailable(hass: HomeAssistant) -> None:
    """A permanent (4xx) customer failure records an empty label so it isn't retried every poll.

    Covers both real cases the proxy now surfaces as a propagated 4xx → OpenGbPermanentError:
    the custodian advertising no customer resource (proxy 400 `no_customer_uri`) and refusing one
    our scope can't access (Burlington's upstream 403 `access_denied`).
    """
    for err in (
        OpenGbPermanentError("POST /proxy/customer returned 400 (permanent): no_customer_uri"),
        OpenGbPermanentError("POST /proxy/customer returned 403 (permanent): access_denied"),
    ):
        api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
        api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]
        api.fetch_customer = AsyncMock(side_effect=err)  # type: ignore[method-assign]

        entry = _fresh_entry(hass)
        coordinator = GreenButtonCoordinator(hass, api, entry)

        with patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ):
            await coordinator.async_refresh()
            await coordinator.async_refresh()

        # Attempted once, recorded unavailable, never retried; title unchanged.
        api.fetch_customer.assert_awaited_once()
        assert entry.data[CONF_CUSTOMER_LABEL] == ""
        assert entry.title == "Milton Hydro (SANDBOX for testing only)"


async def test_customer_label_transient_error_is_retried(hass: HomeAssistant) -> None:
    """A transient customer-fetch failure leaves no label stored, so the next poll retries."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]
    api.fetch_customer = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            OpenGbApiError("POST /proxy/customer returned 502: utility_upstream_error"),
            CustomerResponse(
                customer=CustomerInfo(account_id="ACC-9", service_address=None, customer_name=None),
                new_credentials=None,
            ),
        ]
    )

    entry = _fresh_entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()
        assert CONF_CUSTOMER_LABEL not in entry.data  # transient → not marked, will retry
        await coordinator.async_refresh()

    assert api.fetch_customer.await_count == 2
    assert entry.title == "Milton Hydro (SANDBOX for testing only) — ACC-9"


async def test_customer_label_persists_rotated_credentials(hass: HomeAssistant) -> None:
    """The customer fetch can rotate a one-time refresh token; the coordinator must persist it."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]
    api.fetch_customer = AsyncMock(  # type: ignore[method-assign]
        return_value=CustomerResponse(
            customer=CustomerInfo(account_id="ACC-1", service_address=None, customer_name=None),
            new_credentials=NewCredentials(
                encrypted_refresh_blob="rotated_blob",
                proxy_token="rotated_token",  # noqa: S106
            ),
        )
    )

    entry = _fresh_entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()

    assert entry.data[CONF_ENCRYPTED_REFRESH_BLOB] == "rotated_blob"
    assert entry.data[CONF_PROXY_TOKEN] == "rotated_token"  # noqa: S105


# ---------------------------------------------------------------------------------------
# Single tight incremental window; cost is caught by ordinary polls (no probe/cost cursor).
# ---------------------------------------------------------------------------------------


def _api_returning(response: UsageResponse) -> OpenGbApi:
    """API whose fetch_usage returns `response`."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=response)  # type: ignore[method-assign]
    return api


async def test_update_interval_uses_server_poll_interval(hass: HomeAssistant) -> None:
    """The coordinator polls at the per-utility cadence the server sent in the claim response."""
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_POLL_INTERVAL_SECONDS: 3600}
    )
    coordinator = GreenButtonCoordinator(hass, _api_returning(_empty_response()), entry)
    assert coordinator.update_interval == timedelta(seconds=3600)


async def test_update_interval_falls_back_to_default(hass: HomeAssistant) -> None:
    """An entry without the server-supplied cadence uses the local default (daily)."""
    entry = _entry(hass)  # no CONF_POLL_INTERVAL_SECONDS
    coordinator = GreenButtonCoordinator(hass, _api_returning(_empty_response()), entry)
    assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL


async def test_incremental_poll_uses_one_tight_window_at_usage_frontier(
    hass: HomeAssistant,
) -> None:
    """A routine poll issues a single fetch anchored at the usage frontier — no probe, no widen.

    Cost rides the same window: `published-min` filters by publication date, so a bill published
    late is caught here when it appears, and the importer distributes it over recorded usage.
    """
    usage_frontier = datetime(2026, 7, 18, 5, 0, tzinfo=UTC)
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LAST_FETCHED_AT: usage_frontier.isoformat()}
    )
    api = _api_returning(_empty_response())
    coordinator = GreenButtonCoordinator(hass, api, entry)
    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics", new=AsyncMock()
    ):
        await coordinator.async_refresh()

    api.fetch_usage.assert_awaited_once()  # exactly one fetch — no probe
    assert (
        api.fetch_usage.await_args.kwargs["published_min"] == usage_frontier - LAST_FETCHED_OVERLAP
    )


# ---------------------------------------------------------------------------------------
# One-time statistics repair after an import-logic change (issues #6 / #7).
#
# Rows are written once as they're fetched, so a fix to how usage or cost is *computed* never
# reaches data already in the recorder. `rebuild_statistics` has always been the cure, but only
# for a user who notices the bad data and knows the action exists — so an affected entry repairs
# itself on the first poll after the update. These cover which entries qualify.
# ---------------------------------------------------------------------------------------


def _response_with_cumulative_register() -> UsageResponse:
    """Milton's shape: hourly deltas beside a daily cumulative meter register, both FORWARD.

    The register is the marker that this entry's stored statistics were corrupted by the old
    logic — it's the series that used to be summed into consumption (#6) and whose `cost=0`
    hijacked cost-source selection (#7).
    """
    # The hourly series itemizes per-interval <cost>, as savagedata really does. That's also what
    # settles the revision-3 check for this feed without a UsageSummary in the poll: an account on
    # the per-interval cost path never ran billing-summary selection, so it has nothing to repair.
    delta = MeterReadingSeries(
        meter_reading_id="hourly",
        reading_type=_reading_type(),
        readings=[UsageReading(datetime(2026, 7, 5, 5, tzinfo=UTC), 3600, 1000.0, cost=0.21)],
    )
    bulk_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="BULK_QUANTITY",
        interval_length_seconds=86400,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    register = MeterReadingSeries(
        meter_reading_id="register",
        reading_type=bulk_type,
        readings=[UsageReading(datetime(2026, 7, 5, tzinfo=UTC), 86400, 9_876_543.0, cost=0.0)],
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[delta, register])
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


def _migration_patches(*, had_statistics: bool):
    """Patch the recorder boundary for a migration test; the decision logic stays real."""
    return (
        patch(
            "custom_components.greenbutton.coordinator.async_entry_has_statistics",
            new=AsyncMock(return_value=had_statistics),
        ),
        patch(
            "custom_components.greenbutton.coordinator.async_clear_statistics_for_entry",
            new=AsyncMock(return_value=[f"{DOMAIN}:x_forward"]),
        ),
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
    )


async def test_import_migration_rebuilds_entry_with_cumulative_register(
    hass: HomeAssistant,
) -> None:
    """An entry whose feed carries a cumulative register repairs itself on the next poll.

    Its stored rows were computed by the old logic, which summed the register into consumption —
    a spike no incremental poll can undo, because the cumulative sum carries it forward forever.
    """
    hass.set_state(CoreState.running)  # migration runs inline once HA is up
    entry = _entry(hass)
    api = _api_returning(_response_with_cumulative_register())
    coordinator = GreenButtonCoordinator(hass, api, entry)

    has_stats, clear, _import = _migration_patches(had_statistics=True)
    with has_stats, clear as clear_mock, _import:
        await coordinator.async_refresh()

    assert coordinator.last_exception is None
    assert api.fetch_usage.await_count == 2  # the poll, then the rebuild's full-history re-fetch
    clear_mock.assert_awaited_once_with(hass, entry.entry_id)
    assert entry.data[CONF_IMPORT_LOGIC_REVISION] == IMPORT_LOGIC_REVISION


def _summary_costed_response() -> UsageResponse:
    """Burlington's shape: hourly readings with no per-interval cost, billed by UsageSummary."""
    from custom_components.greenbutton.api import BillingSummary, CostDetail

    series = MeterReadingSeries(
        meter_reading_id="hourly",
        reading_type=_reading_type(),
        readings=[UsageReading(datetime(2026, 7, 5, 5, tzinfo=UTC), 3600, 1000.0)],
    )
    summary = BillingSummary(
        billing_period_start=datetime(2026, 6, 2, tzinfo=UTC),
        billing_period_duration_seconds=30 * 86400,
        bill_last_period_raw=None,
        cost_additional_last_period_raw=0,
        cost_details=[
            CostDetail(amount_raw=18_773_000, note="Energy", item_kind=None, unit_cost_raw=0)
        ],
        currency_numeric_code=124,
    )
    up = UsagePoint(
        usage_point_id="up1",
        service_kind="electricity",
        series=[series],
        summaries=[summary],
    )
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def test_import_migration_rebuilds_a_summary_costed_entry(hass: HomeAssistant) -> None:
    """Revision 3: an account billed through UsageSummary repairs its cost without being asked.

    Selection used to drop every bill that overlapped the previous one at the meter-read day,
    which is most of them, so the cost statistic was built from about half the money. Nothing
    about the stored rows looks wrong — cost is simply too low — so nobody would think to run
    `rebuild_statistics`, which is exactly why this has to happen on its own.
    """
    hass.set_state(CoreState.running)
    entry = _entry(hass)
    _stamp(hass, entry, 2)  # current before this fix; only the revision-3 check should fire
    api = _api_returning(_summary_costed_response())
    coordinator = GreenButtonCoordinator(hass, api, entry)

    has_stats, clear, _import = _migration_patches(had_statistics=True)
    with has_stats, clear as clear_mock, _import:
        await coordinator.async_refresh()

    assert coordinator.last_exception is None
    assert api.fetch_usage.await_count == 2  # the poll, then the rebuild's full-history re-fetch
    clear_mock.assert_awaited_once_with(hass, entry.entry_id)
    assert entry.data[CONF_IMPORT_LOGIC_REVISION] == IMPORT_LOGIC_REVISION


async def test_import_migration_will_not_clear_an_entry_it_could_not_judge(
    hass: HomeAssistant,
) -> None:
    """A poll carrying no cost of any kind leaves the entry unstamped, not cleared.

    The trap this guards. Bills only appear in the poll that publishes them, and a monthly utility
    hands over one at a time — so most Burlington polls carry readings and no summary. Treating
    that as "unaffected" would stamp the entry on the first poll after the update and close the
    repair permanently, on evidence we never had. Not deciding costs one cheap re-check a day.
    """
    hass.set_state(CoreState.running)
    entry = _entry(hass)
    _stamp(hass, entry, 2)
    api = _api_returning(_response_with_readings(datetime(2026, 7, 5, 5, tzinfo=UTC)))
    coordinator = GreenButtonCoordinator(hass, api, entry)

    has_stats, clear, _import = _migration_patches(had_statistics=True)
    with has_stats, clear as clear_mock, _import:
        await coordinator.async_refresh()

    api.fetch_usage.assert_awaited_once()  # no rebuild
    clear_mock.assert_not_awaited()
    assert entry.data[CONF_IMPORT_LOGIC_REVISION] == 2  # still owed the check


async def test_import_migration_skips_entry_without_cumulative_register(
    hass: HomeAssistant,
) -> None:
    """An unaffected feed is stamped in place — no full-history re-pull from its utility.

    The whole point of gating on the feed's shape: a blanket rebuild would make every user
    re-download their entire history to fix a bug they never had. The readings carry per-interval
    cost so every check can reach a verdict from this one poll — see
    [statistics.response_cost_may_be_missing_bills] for why an entry that can't be judged is left
    unstamped rather than cleared.
    """
    hass.set_state(CoreState.running)
    entry = _entry(hass)
    api = _api_returning(
        _response_with_readings(datetime(2026, 7, 5, 5, tzinfo=UTC), cost=0.21),
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    has_stats, clear, _import = _migration_patches(had_statistics=True)
    with has_stats, clear as clear_mock, _import:
        await coordinator.async_refresh()

    api.fetch_usage.assert_awaited_once()  # no rebuild re-fetch
    clear_mock.assert_not_awaited()
    assert entry.data[CONF_IMPORT_LOGIC_REVISION] == IMPORT_LOGIC_REVISION


async def test_import_migration_skips_entry_with_no_prior_statistics(
    hass: HomeAssistant,
) -> None:
    """A newly-added entry is stamped without a rebuild — its first import is already correct.

    Nothing in the store predates the current logic, so there is nothing to repair, even though
    its feed does carry the cumulative register.
    """
    hass.set_state(CoreState.running)
    entry = _entry(hass)
    api = _api_returning(_response_with_cumulative_register())
    coordinator = GreenButtonCoordinator(hass, api, entry)

    has_stats, clear, _import = _migration_patches(had_statistics=False)
    with has_stats, clear as clear_mock, _import:
        await coordinator.async_refresh()

    api.fetch_usage.assert_awaited_once()
    clear_mock.assert_not_awaited()
    assert entry.data[CONF_IMPORT_LOGIC_REVISION] == IMPORT_LOGIC_REVISION


def _billing_period_response() -> UsageResponse:
    """Consumers Energy's shape: one month-long reading, mislabelled BULK_QUANTITY, no sibling."""
    reading_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="BULK_QUANTITY",
        interval_length_seconds=2505599,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=840,
    )
    series = MeterReadingSeries(
        meter_reading_id="B12888756_kwh_1",
        reading_type=reading_type,
        readings=[UsageReading(datetime(2026, 6, 2, tzinfo=UTC), 2505599, 914_523.0, cost=234.02)],
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[series])
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


def _stamp(hass: HomeAssistant, entry: MockConfigEntry, revision: int) -> None:
    """Mark [entry] as holding rows produced by import-logic [revision]."""
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_IMPORT_LOGIC_REVISION: revision}
    )


async def test_import_migration_rebuilds_entry_wiped_by_revision_1(hass: HomeAssistant) -> None:
    """A billing-only entry emptied by revision 1 repairs itself — despite having no rows.

    Revision 1 classed a month-long reading as a cumulative register, so its repair rebuild
    purged the entry and re-imported nothing, leaving it stamped 1 with an empty store and a
    cursor advanced past its data. "No statistics" must NOT be read as "newly added" here, or
    the account stays permanently blank with nothing left to trigger a re-import.
    """
    hass.set_state(CoreState.running)
    entry = _entry(hass)
    _stamp(hass, entry, 1)
    api = _api_returning(_billing_period_response())
    coordinator = GreenButtonCoordinator(hass, api, entry)

    has_stats, clear, _import = _migration_patches(had_statistics=False)
    with has_stats, clear as clear_mock, _import:
        await coordinator.async_refresh()

    assert coordinator.last_exception is None
    assert api.fetch_usage.await_count == 2  # the poll, then the rebuild's full-history re-fetch
    clear_mock.assert_awaited_once_with(hass, entry.entry_id)
    assert entry.data[CONF_IMPORT_LOGIC_REVISION] == IMPORT_LOGIC_REVISION


async def test_import_migration_does_not_rebuild_repaired_entry_twice(
    hass: HomeAssistant,
) -> None:
    """An entry already at revision 1 with an hourly feed is stamped forward, not rebuilt again.

    Milton's register spans 24 hours, but it's excluded in favour of its hourly DELTA_DATA
    sibling — so its stored rows never came from a multi-hour reading and revision 2 doesn't
    apply. Re-pulling its whole history would be pure waste.
    """
    hass.set_state(CoreState.running)
    entry = _entry(hass)
    _stamp(hass, entry, 1)
    api = _api_returning(_response_with_cumulative_register())
    coordinator = GreenButtonCoordinator(hass, api, entry)

    has_stats, clear, _import = _migration_patches(had_statistics=True)
    with has_stats, clear as clear_mock, _import:
        await coordinator.async_refresh()

    api.fetch_usage.assert_awaited_once()  # no rebuild re-fetch
    clear_mock.assert_not_awaited()
    assert entry.data[CONF_IMPORT_LOGIC_REVISION] == IMPORT_LOGIC_REVISION


async def test_import_migration_defers_decision_on_empty_response(hass: HomeAssistant) -> None:
    """An empty poll can't clear an entry: "no register" may just mean "nothing published".

    Utilities publish on a multi-day lag, so empty windows are routine. Stamping on one would
    permanently write off an affected entry that simply had a quiet day.
    """
    hass.set_state(CoreState.running)
    entry = _entry(hass)
    api = _api_returning(_empty_response())
    coordinator = GreenButtonCoordinator(hass, api, entry)

    has_stats, clear, _import = _migration_patches(had_statistics=True)
    with has_stats, clear as clear_mock, _import:
        await coordinator.async_refresh()

    clear_mock.assert_not_awaited()
    assert CONF_IMPORT_LOGIC_REVISION not in entry.data  # undecided → re-checked next poll


async def test_import_migration_retries_after_a_failed_rebuild(hass: HomeAssistant) -> None:
    """A failed repair must not fail the poll, and must not mark the entry as repaired.

    The poll itself succeeded and its data is sound; taking the entry down over old rows would
    stop new ones arriving too. Leaving the stamp off is what schedules the retry.
    """
    hass.set_state(CoreState.running)
    entry = _entry(hass)
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    # First call (the poll) succeeds; the rebuild's re-fetch fails.
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        side_effect=[_response_with_cumulative_register(), OpenGbApiError("upstream 502")]
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    has_stats, clear, _import = _migration_patches(had_statistics=True)
    with has_stats, clear as clear_mock, _import:
        await coordinator.async_refresh()

    assert coordinator.last_update_success is True  # the poll stands
    assert coordinator.last_exception is None
    clear_mock.assert_not_awaited()  # fetch failed first → nothing purged
    assert CONF_IMPORT_LOGIC_REVISION not in entry.data  # still marked for repair


async def test_import_migration_not_rechecked_once_stamped(hass: HomeAssistant) -> None:
    """A stamped entry costs nothing on later polls — not even the recorder lookup."""
    hass.set_state(CoreState.running)
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_IMPORT_LOGIC_REVISION: IMPORT_LOGIC_REVISION}
    )
    api = _api_returning(_response_with_cumulative_register())
    coordinator = GreenButtonCoordinator(hass, api, entry)

    has_stats, clear, _import = _migration_patches(had_statistics=True)
    with has_stats as has_stats_mock, clear as clear_mock, _import:
        await coordinator.async_refresh()

    has_stats_mock.assert_not_awaited()  # steady state: no recorder round-trip
    clear_mock.assert_not_awaited()
    api.fetch_usage.assert_awaited_once()


async def test_import_migration_deferred_until_hass_started(hass: HomeAssistant) -> None:
    """Before HA has started, the repair waits for EVENT_HOMEASSISTANT_STARTED.

    Same deadlock as the cost pass: the rebuild awaits `Recorder.async_block_till_done()`, which
    can't complete before HA starts, while HA won't start until config-entry setup returns — and
    the first refresh runs inside that setup. Waiting for the next scheduled poll instead would
    strand the user on visibly wrong data for a full poll interval (a day on most utilities).
    """
    hass.set_state(CoreState.starting)
    entry = _entry(hass)
    api = _api_returning(_response_with_cumulative_register())
    coordinator = GreenButtonCoordinator(hass, api, entry)

    has_stats, clear, _import = _migration_patches(had_statistics=True)
    with has_stats, clear as clear_mock, _import:
        await coordinator.async_refresh()

        # Nothing recorder-blocking may run while HA is still starting.
        clear_mock.assert_not_awaited()
        api.fetch_usage.assert_awaited_once()
        assert CONF_IMPORT_LOGIC_REVISION not in entry.data

        # ...and the repair runs as soon as HA is up.
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()

        clear_mock.assert_awaited_once_with(hass, entry.entry_id)
        assert api.fetch_usage.await_count == 2
        assert entry.data[CONF_IMPORT_LOGIC_REVISION] == IMPORT_LOGIC_REVISION


async def test_advance_cursor_writes_one_cursor_per_meter(hass: HomeAssistant) -> None:
    """Each UsagePoint gets its own cursor, anchored to that meter's own newest reading.

    A subscription can carry several meters — commonly different commodities on entirely
    different reading cadences — so one frontier for the whole entry only ever describes
    whichever meter runs furthest ahead.
    """
    fast = datetime(2026, 6, 3, 12, tzinfo=UTC)
    slow = datetime(2026, 4, 1, 0, tzinfo=UTC)
    api = _api_returning(_response_with_meters({"electric": [fast], "gas": [slow]}))

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)
    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    assert entry.data[CONF_USAGE_POINT_CURSORS] == {
        "electric": fast.isoformat(),
        "gas": slow.isoformat(),
    }
    # The entry-wide frontier still tracks the newest across meters: it answers "has this entry
    # ever imported anything", which is not a per-meter question.
    assert entry.data[CONF_LAST_FETCHED_AT] == fast.isoformat()


async def test_window_scopes_to_the_furthest_behind_meter(hass: HomeAssistant) -> None:
    """`published_min` follows the OLDEST cursor, not the newest.

    One request serves every meter on the subscription. Scoping to the newest would let a daily
    electric meter drag the window past a monthly gas meter's not-yet-published data, and the
    gas readings would never be asked for again.
    """
    fast = datetime(2026, 6, 3, 12, tzinfo=UTC)
    slow = datetime(2026, 4, 1, 0, tzinfo=UTC)
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_LAST_FETCHED_AT: fast.isoformat(),
            CONF_USAGE_POINT_CURSORS: {
                "electric": fast.isoformat(),
                "gas": slow.isoformat(),
            },
        },
    )
    api = _api_returning(_empty_response())
    coordinator = GreenButtonCoordinator(hass, api, entry)
    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    assert api.fetch_usage.await_args.kwargs["published_min"] == slow - LAST_FETCHED_OVERLAP


async def test_a_silent_meter_keeps_its_cursor(hass: HomeAssistant) -> None:
    """A meter absent from a response keeps the cursor it had — absence is not "no data ever".

    A monthly gas meter is silent through most of an electric meter's polls. Dropping or
    resetting its cursor would re-fetch its entire history on the very next poll.
    """
    electric_first = datetime(2026, 6, 1, tzinfo=UTC)
    gas = datetime(2026, 4, 1, tzinfo=UTC)
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_USAGE_POINT_CURSORS: {
                "electric": electric_first.isoformat(),
                "gas": gas.isoformat(),
            },
        },
    )

    electric_next = datetime(2026, 6, 2, tzinfo=UTC)
    api = _api_returning(_response_with_meters({"electric": [electric_next]}))
    coordinator = GreenButtonCoordinator(hass, api, entry)
    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    assert entry.data[CONF_USAGE_POINT_CURSORS] == {
        "electric": electric_next.isoformat(),
        "gas": gas.isoformat(),
    }


async def test_meter_cursors_never_retreat(hass: HomeAssistant) -> None:
    """A response carrying older readings for a meter leaves that meter's cursor alone."""
    ahead = datetime(2026, 6, 10, tzinfo=UTC)
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_USAGE_POINT_CURSORS: {"electric": ahead.isoformat()}},
    )

    api = _api_returning(_response_with_meters({"electric": [datetime(2026, 5, 1, tzinfo=UTC)]}))
    coordinator = GreenButtonCoordinator(hass, api, entry)
    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    assert entry.data[CONF_USAGE_POINT_CURSORS] == {"electric": ahead.isoformat()}


async def test_entry_written_before_per_meter_cursors_uses_the_old_frontier(
    hass: HomeAssistant,
) -> None:
    """An entry with only the entry-wide frontier keeps polling incrementally.

    The scalar stands in until the next successful fetch seeds the per-meter map, so upgrading
    costs no migration step and — importantly — no accidental full-history refetch.
    """
    frontier = datetime(2026, 6, 1, tzinfo=UTC)
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_LAST_FETCHED_AT: frontier.isoformat()},
    )
    api = _api_returning(_empty_response())
    coordinator = GreenButtonCoordinator(hass, api, entry)
    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    assert api.fetch_usage.await_args.kwargs["published_min"] == frontier - LAST_FETCHED_OVERLAP


async def test_unparseable_meter_cursor_is_skipped_not_fatal(hass: HomeAssistant) -> None:
    """One corrupt cursor widens that meter's window instead of taking the entry down.

    Re-fetched readings are harmless (the import is idempotent on (statistic_id, hour)), so
    skipping is strictly cheaper than failing the refresh.
    """
    good = datetime(2026, 6, 1, tzinfo=UTC)
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_USAGE_POINT_CURSORS: {"electric": good.isoformat(), "gas": "not-a-timestamp"},
        },
    )
    api = _api_returning(_empty_response())
    coordinator = GreenButtonCoordinator(hass, api, entry)
    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    # The surviving cursor still scopes the window — the corrupt one is simply ignored.
    assert api.fetch_usage.await_args.kwargs["published_min"] == good - LAST_FETCHED_OVERLAP


async def test_window_compliance_is_logged_per_meter(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every field needed to tell an honoured date filter from an ignored one, per meter.

    Whether an async-batch custodian's per-UsagePoint URL honours `published-min` is unsettled,
    and one poll cannot settle it: `published-min` filters by PUBLICATION date while readings
    carry their own interval start, so a single publication legitimately holds years of data. The
    signature of an ignored filter is only visible across polls — advancing windows returning
    identical counts and spans — so each poll must record the whole comparison.
    """
    api = _api_returning(
        _response_with_meters(
            {
                "electric": [datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 2, tzinfo=UTC)],
                "gas": [datetime(2024, 1, 1, tzinfo=UTC)],
            }
        )
    )
    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)
    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
        caplog.at_level(logging.INFO, logger="custom_components.greenbutton"),
    ):
        await coordinator._async_update_data()

    lines = [r.getMessage() for r in caplog.records if "Window check" in r.getMessage()]
    assert len(lines) == 2
    electric = next(line for line in lines if "usage_point=electric" in line)
    assert "readings=2" in electric
    assert "span=[2026-06-01T00:00:00+00:00, 2026-06-02T00:00:00+00:00]" in electric
    # The gas meter's lone reading predates a 2-year initial window, which is exactly the kind of
    # divergence the comparison exists to surface.
    gas = next(line for line in lines if "usage_point=gas" in line)
    assert "readings=1" in gas
    assert "before_published_min=1" in gas


async def test_a_future_published_max_is_frozen_clamped_to_now(hass: HomeAssistant) -> None:
    """A forward-dated `published_max` is stored clamped, never as given.

    The client no longer sends a future bound, so this is defence in depth — and a regression
    guard. The proxy clamps a future `published-max` down to its own `now` before the request
    reaches the custodian, so freezing one would have the clamp rewrite it to a fresh instant on
    every retry: the window looks frozen here while the custodian sees a brand-new URL each time,
    which is exactly what a deferring custodian answers with another 202. Re-introducing any
    forward buffer must not silently defeat the freeze again.
    """
    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, _api_returning(_empty_response()), entry)

    published_min = datetime(2026, 6, 1, tzinfo=UTC)
    far_future = datetime.now(UTC) + timedelta(days=1)
    coordinator._remember_pending_window(published_min, far_future)

    frozen_max = datetime.fromisoformat(entry.data[CONF_PENDING_PUBLISHED_MAX])
    assert frozen_max < far_future
    assert frozen_max <= datetime.now(UTC)
    assert entry.data[CONF_PENDING_PUBLISHED_MIN] == published_min.isoformat()
