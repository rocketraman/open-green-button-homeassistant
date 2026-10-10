"""A meter the utility re-issues under a new UsagePoint id — detection, and the user's two answers.

These run against a real recorder rather than a mocked importer: the bug is entirely about which
statistic rows end up where, and the merge is arithmetic on stored sums.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.greenbutton.api import (
    BillingSummary,
    MeterReadingSeries,
    NormalizedReadingType,
    OpenGbApi,
    UsagePoint,
    UsageReading,
    UsageResponse,
)
from custom_components.greenbutton.const import (
    CONF_CUSTOMER_LABEL,
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_IMPORT_LOGIC_REVISION,
    CONF_PROXY_TOKEN,
    CONF_USAGE_POINT_ALIASES,
    CONF_USAGE_POINT_CURSORS,
    CONF_USAGE_POINT_SEPARATE,
    CONF_UTILITY_ID,
    CONF_UTILITY_NAME,
    DOMAIN,
    IMPORT_LOGIC_REVISION,
    LAST_FETCHED_OVERLAP,
)
from custom_components.greenbutton.coordinator import GreenButtonCoordinator
from custom_components.greenbutton.repairs import async_create_fix_flow
from custom_components.greenbutton.statistics import (
    statistic_id_for_cost,
    statistic_id_for_series,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_DAY0 = datetime(2026, 9, 13, 4, tzinfo=UTC)


def _hours(first: int, count: int) -> list[datetime]:
    """[count] consecutive hourly reading starts, [first] hours after _DAY0."""
    return [_DAY0 + timedelta(hours=first + i) for i in range(count)]


def _meter(
    up_id: str,
    starts: list[datetime],
    *,
    summaries: list[BillingSummary] | None = None,
    unit: str = "WATT_HOURS",
    service_kind: str = "electricity",
) -> UsagePoint:
    """One meter reporting 1000 of [unit] in every hour of [starts]."""
    reading_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED" if unit == "WATT_HOURS" else "NATURAL_GAS",
        flow_direction="FORWARD",
        accumulation_behaviour="DELTA_DATA",
        interval_length_seconds=3600,
        unit_of_measure=unit,
        unit_of_measure_symbol=None,
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    series = MeterReadingSeries(
        meter_reading_id=f"mr-{up_id}",
        reading_type=reading_type,
        readings=[UsageReading(start=s, duration_seconds=3600, value=1000.0) for s in starts],
    )
    return UsagePoint(
        usage_point_id=up_id,
        service_kind=service_kind,
        series=[series],
        summaries=summaries or [],
    )


def _response(*usage_points: UsagePoint) -> UsageResponse:
    return UsageResponse(updated=None, usage_points=list(usage_points), new_credentials=None)


def _setup(hass: HomeAssistant, *responses: UsageResponse) -> GreenButtonCoordinator:
    """A loaded-looking entry whose successive polls return [responses] in order."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_UTILITY_ID: "example_utility",
            CONF_UTILITY_NAME: "Example Utility",
            CONF_ENCRYPTED_REFRESH_BLOB: "blob",
            CONF_PROXY_TOKEN: "token",
            CONF_CUSTOMER_LABEL: "",
            CONF_IMPORT_LOGIC_REVISION: IMPORT_LOGIC_REVISION,
        },
    )
    entry.add_to_hass(hass)
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(side_effect=list(responses))  # type: ignore[method-assign]
    coordinator = GreenButtonCoordinator(hass, api, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator


async def _poll(hass: HomeAssistant, coordinator: GreenButtonCoordinator) -> None:
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    await async_wait_recording_done(hass)


async def _sums(hass: HomeAssistant, statistic_id: str) -> list[float]:
    by_id = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime.fromtimestamp(0, tz=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return [round(row["sum"], 3) for row in by_id.get(statistic_id, [])]


def _issue(hass: HomeAssistant, coordinator: GreenButtonCoordinator, up_id: str):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"usage_point_replaced_{coordinator.entry.entry_id}_{up_id}"
    )


async def _reissued(hass: HomeAssistant, *later: UsageResponse) -> GreenButtonCoordinator:
    """An entry whose meter `old` reported for a day and was then re-issued as `new`.

    `new` first appears in a poll whose window overlaps the last 12 hours `old` reported, as it
    does in practice — the window opens one overlap behind the silent meter's cursor.
    """
    coordinator = _setup(
        hass,
        _response(_meter("old", _hours(0, 24))),
        _response(_meter("new", _hours(12, 36))),
        *later,
    )
    await _poll(hass, coordinator)
    await _poll(hass, coordinator)
    return coordinator


async def test_a_reissued_meter_raises_a_fixable_repair_issue(hass: HomeAssistant) -> None:
    """The new id's readings land in new statistics, and the user is asked what it is."""
    coordinator = await _reissued(hass)
    entry_id = coordinator.entry.entry_id

    # The bug itself: the statistic the dashboard reads has stopped, and a second one started.
    assert await _sums(hass, statistic_id_for_series(entry_id, "old", "FORWARD")) == [
        float(n) for n in range(1, 25)
    ]
    assert len(await _sums(hass, statistic_id_for_series(entry_id, "new", "FORWARD"))) == 36

    issue = _issue(hass, coordinator, "new")
    assert issue is not None
    assert issue.is_fixable
    assert issue.is_persistent
    assert issue.data == {
        "entry_id": entry_id,
        "usage_point_id": "new",
        "replaced_usage_point_id": "old",
        "replaced_last_reading": "2026-09-13",
    }
    # Nothing is merged until the user says so.
    assert CONF_USAGE_POINT_ALIASES not in coordinator.entry.data


async def test_merging_continues_the_replaced_meters_statistics(hass: HomeAssistant) -> None:
    """Merge: rows move onto the old statistics, later polls follow, and the missed bill is costed.

    The bill covers hours 6-47. It was published after the id changed, so under the new id it had
    only the hours from 12 on to be spread over; once merged it is distributed over all of them.
    """
    bill = BillingSummary(
        billing_period_start=_hours(6, 1)[0],
        billing_period_duration_seconds=42 * 3600,
        bill_last_period_raw=4_200_000,  # 1/100,000 of a dollar → $42.00
        cost_additional_last_period_raw=None,
        cost_details=[],
        currency_numeric_code=124,
    )
    assert bill.total_cost == 42.0
    coordinator = await _reissued(hass, _response(_meter("new", _hours(12, 42), summaries=[bill])))
    entry = coordinator.entry
    old_usage = statistic_id_for_series(entry.entry_id, "old", "FORWARD")

    flow = await async_create_fix_flow(hass, "ignored", dict(_issue(hass, coordinator, "new").data))
    flow.hass = hass
    menu = await flow.async_step_init()
    assert menu["type"] is FlowResultType.MENU
    assert menu["menu_options"] == ["merge", "keep_separate"]
    result = await flow.async_step_merge()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # The 36 rows written under `new` are on the end of `old`: the 12 overlapping hours dropped,
    # the remaining 24 continuing its running sum with no step and no double count.
    await async_wait_recording_done(hass)
    assert entry.data[CONF_USAGE_POINT_ALIASES] == {"new": "old"}
    assert _issue(hass, coordinator, "new") is None

    # The merge kicked off a poll: six more hours arrive under the alias.
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    assert await _sums(hass, old_usage) == [float(n) for n in range(1, 55)]
    assert await _sums(hass, statistic_id_for_series(entry.entry_id, "new", "FORWARD")) == []

    # That poll's window still reached back to where `old` stopped, so it carried the bill, which
    # now has the whole period's usage to be spread over: $1 an hour for 42 hours.
    assert await _sums(hass, statistic_id_for_cost(entry.entry_id, "old")) == [
        float(n) for n in range(1, 43)
    ]
    assert await _sums(hass, statistic_id_for_cost(entry.entry_id, "new")) == []
    published_min = coordinator.api.fetch_usage.await_args.kwargs["published_min"]
    assert published_min == _hours(23, 1)[0] - LAST_FETCHED_OVERLAP

    # And only then is the replaced meter's cursor retired, so it stops pinning the window.
    assert entry.data[CONF_USAGE_POINT_CURSORS] == {"new": _hours(53, 1)[0].isoformat()}


async def test_keeping_meters_separate_is_remembered(hass: HomeAssistant) -> None:
    """Keep separate: nothing moves, and the same pair is never asked about again."""
    coordinator = await _reissued(hass, _response(_meter("new", _hours(12, 42))))
    entry = coordinator.entry

    flow = await async_create_fix_flow(hass, "ignored", dict(_issue(hass, coordinator, "new").data))
    flow.hass = hass
    result = await flow.async_step_keep_separate()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_USAGE_POINT_SEPARATE] == {"new": ["old"]}
    assert _issue(hass, coordinator, "new") is None

    await _poll(hass, coordinator)

    assert _issue(hass, coordinator, "new") is None
    assert CONF_USAGE_POINT_ALIASES not in entry.data
    assert len(await _sums(hass, statistic_id_for_series(entry.entry_id, "old", "FORWARD"))) == 24
    assert len(await _sums(hass, statistic_id_for_series(entry.entry_id, "new", "FORWARD"))) == 42
    assert set(entry.data[CONF_USAGE_POINT_CURSORS]) == {"old", "new"}


async def test_a_meter_that_was_always_there_is_not_a_replacement(hass: HomeAssistant) -> None:
    """Two meters reporting side by side, one of which goes quiet: no question to ask."""
    coordinator = _setup(
        hass,
        _response(_meter("a", _hours(0, 96)), _meter("b", _hours(0, 96))),
        _response(_meter("b", _hours(72, 48))),
    )
    await _poll(hass, coordinator)
    await _poll(hass, coordinator)

    assert _issue(hass, coordinator, "b") is None


async def test_a_meter_measuring_something_else_is_not_a_replacement(hass: HomeAssistant) -> None:
    """A silent gas meter is not replaced by an electricity meter that turns up."""
    coordinator = _setup(
        hass,
        _response(_meter("gas", _hours(0, 24), unit="THERMS", service_kind="gas")),
        _response(_meter("electric", _hours(12, 36))),
    )
    await _poll(hass, coordinator)
    await _poll(hass, coordinator)

    assert _issue(hass, coordinator, "electric") is None


async def test_a_meter_reissued_twice_resolves_to_the_original(hass: HomeAssistant) -> None:
    """Aliases chain: the third id still continues the statistics the dashboard started with."""
    coordinator = await _reissued(
        hass,
        _response(_meter("new", _hours(12, 36))),  # the poll the first merge kicks off
        _response(_meter("newer", _hours(36, 36))),
        _response(_meter("newer", _hours(36, 36))),  # the poll the second merge kicks off
    )
    entry = coordinator.entry
    await coordinator.async_merge_usage_point("new", "old")
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    await _poll(hass, coordinator)
    issue = _issue(hass, coordinator, "newer")
    assert issue is not None
    assert issue.data["replaced_usage_point_id"] == "new"

    await coordinator.async_merge_usage_point("newer", "new")
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    assert entry.data[CONF_USAGE_POINT_ALIASES] == {"new": "old", "newer": "new"}
    assert await _sums(hass, statistic_id_for_series(entry.entry_id, "old", "FORWARD")) == [
        float(n) for n in range(1, 73)
    ]
    assert set(entry.data[CONF_USAGE_POINT_CURSORS]) == {"newer"}


async def test_fix_flow_aborts_when_the_entry_is_not_loaded(hass: HomeAssistant) -> None:
    """The flow outlives a reload/unload of its entry; it must not act on a dead coordinator."""
    flow = await async_create_fix_flow(
        hass,
        "ignored",
        {"entry_id": "gone", "usage_point_id": "new", "replaced_usage_point_id": "old"},
    )
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"
