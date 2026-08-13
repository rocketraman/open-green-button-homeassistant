"""Tests for the statistics helper — id format invariants + unit conversion.

The full async_add_external_statistics path is exercised by the coordinator-level tests
where a recorder is wired up; these focus on the pure-function bits that don't need HA.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.greenbutton.api import (
    BillingSummary,
    MeterReadingSeries,
    NormalizedReadingType,
    UsagePoint,
    UsageReading,
    UsageResponse,
)
from custom_components.greenbutton.const import DOMAIN
from custom_components.greenbutton.statistics import (
    _recorded_forward_hours,
    import_usage_statistics,
    statistic_id_for_series,
    statistic_id_prefix_for_entry,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_DAY = 86400


async def test_recorded_forward_hours_reconstructs_hourly_kwh(hass: HomeAssistant) -> None:
    """`_recorded_forward_hours` reads the FORWARD usage stat back and diffs it into per-hour kWh.

    Round-trips a real recorder (not mocked) to validate the statistics_during_period call shape
    and the cumulative-sum → per-hour delta reconstruction the summary-cost path relies on.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[])
    stat_id = statistic_id_for_series("01TESTENTRY", "up1", "FORWARD")
    base = datetime(2026, 4, 3, 4, 0, tzinfo=UTC)
    metadata = {
        "has_mean": False,
        "has_sum": True,
        "name": "test usage",
        "source": DOMAIN,
        "statistic_id": stat_id,
        "unit_of_measurement": "kWh",
        "unit_class": "energy",
        "mean_type": StatisticMeanType.NONE,
    }
    # Cumulative sums 0, 2, 5, 6 → per-hour deltas 2, 3, 1 for the three hours after `base`.
    async_add_external_statistics(
        hass,
        metadata,
        [
            {"start": base + timedelta(hours=i), "state": s, "sum": s}
            for i, s in enumerate((0.0, 2.0, 5.0, 6.0))
        ],
    )
    await async_wait_recording_done(hass)

    hours = await _recorded_forward_hours(
        hass, entry, up, base + timedelta(hours=1), base + timedelta(hours=4)
    )
    assert [(h.hour, round(k, 1)) for h, k in hours] == [(5, 2.0), (6, 3.0), (7, 1.0)]


def _summary(start: datetime, duration_days: int, total_dollars: float) -> BillingSummary:
    """Build a BillingSummary whose total_cost is `total_dollars` (via billLastPeriod)."""
    return BillingSummary(
        billing_period_start=start,
        billing_period_duration_seconds=duration_days * _DAY,
        bill_last_period_raw=round(total_dollars * 100_000),
        cost_additional_last_period_raw=0,
        cost_details=[],
        currency_numeric_code=124,
    )


_APR2 = datetime(2026, 4, 2, tzinfo=UTC)
_MAY4 = datetime(2026, 5, 4, tzinfo=UTC)


def _one_reading_response() -> UsageResponse:
    """A response with a single FORWARD electricity reading, enough to drive an import."""
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
    series = MeterReadingSeries(
        meter_reading_id="mr1",
        reading_type=reading_type,
        readings=[
            UsageReading(
                start=datetime(2026, 7, 5, 5, tzinfo=UTC), duration_seconds=3600, value=1000.0
            )
        ],
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[series])
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def test_fresh_import_bypasses_resume_point(hass: HomeAssistant) -> None:
    """`fresh=True` must NOT read the resume point — that read is the rebuild race.

    Regression guard: a rebuild clears the store then re-imports the full-history feed. If the
    resume-point read observed the pre-clear cursor, every reading looked "already imported"
    and got skipped, importing nothing. `fresh=True` skips the read and imports from zero.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point", new=AsyncMock()
        ) as resume_mock,
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass, entry, _one_reading_response(), utility_display_name="X", fresh=True
        )

    resume_mock.assert_not_awaited()  # the racy read is skipped entirely
    add_mock.assert_called_once()  # and the reading is still written
    _metadata, stats = add_mock.call_args.args[1], add_mock.call_args.args[2]
    assert len(stats) == 1


async def test_incremental_import_reads_resume_point(hass: HomeAssistant) -> None:
    """The normal (non-fresh) poll path still resumes from stored totals."""
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ) as resume_mock,
        patch("custom_components.greenbutton.statistics.async_add_external_statistics"),
    ):
        await import_usage_statistics(
            hass, entry, _one_reading_response(), utility_display_name="X"
        )

    resume_mock.assert_awaited()  # incremental imports must still read the resume point


def _sub_hourly_response(hours: range, interval_seconds: int = 900) -> UsageResponse:
    """A FORWARD electricity response on a sub-hourly `intervalLength`.

    Each hour in [hours] is split into `3600 // interval_seconds` readings of 250 Wh, so every
    hour totals exactly 1 kWh no matter the interval length.
    """
    per_hour = 3600 // interval_seconds
    reading_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="DELTA_DATA",
        interval_length_seconds=interval_seconds,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    series = MeterReadingSeries(
        meter_reading_id="mr1",
        reading_type=reading_type,
        readings=[
            UsageReading(
                start=datetime(2026, 7, 5, h, tzinfo=UTC) + timedelta(seconds=i * interval_seconds),
                duration_seconds=interval_seconds,
                value=1000.0 / per_hour,
            )
            for h in hours
            for i in range(per_hour)
        ],
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[series])
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def _recorded_sums(hass: HomeAssistant, stat_id: str) -> list[tuple[int, float]]:
    """Read a usage statistic back out of the recorder as ``[(hour, cumulative_sum), ...]``."""
    by_id = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 7, 5, tzinfo=UTC),
        datetime(2026, 7, 6, tzinfo=UTC),
        {stat_id},
        "hour",
        None,
        {"sum"},
    )
    return [
        (datetime.fromtimestamp(row["start"], tz=UTC).hour, round(row["sum"], 3))
        for row in by_id.get(stat_id, [])
    ]


async def test_sub_hourly_series_is_not_double_counted_across_polls(hass: HomeAssistant) -> None:
    """A 15-minute-interval feed imported twice must not inflate the cumulative sum.

    Regression guard for a latent double-count: `_align_to_hour` floors all four readings in an
    hour to the same `start`, so one row per reading collides on (statistic_id, start) and only
    the last survives HA's upsert — which looks right on a single import, but leaves the stored
    row's start at the hour boundary. `_resume_point` returns that boundary, so a stale-window
    guard comparing the *raw* reading start would wave the :15/:30/:45 readings of an
    already-imported hour straight through on the next poll and add them on top of the resumed
    sum, inflating that hour and every hour after it.

    Two polls over overlapping windows, against a real recorder, must leave 1 kWh per hour.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    stat_id = statistic_id_for_series(entry.entry_id, "up1", "FORWARD")

    # Poll 1: hours 05 and 06 (1 kWh each, as four 250 Wh quarter-hour readings).
    await import_usage_statistics(
        hass, entry, _sub_hourly_response(range(5, 7)), utility_display_name="X"
    )
    await async_wait_recording_done(hass)
    assert await _recorded_sums(hass, stat_id) == [(5, 1.0), (6, 2.0)]

    # Poll 2: the same two hours again (the fetch window overlaps by design) plus hour 07.
    await import_usage_statistics(
        hass, entry, _sub_hourly_response(range(5, 8)), utility_display_name="X"
    )
    await async_wait_recording_done(hass)
    assert await _recorded_sums(hass, stat_id) == [(5, 1.0), (6, 2.0), (7, 3.0)]


async def test_sub_hourly_readings_are_summed_into_one_row_per_hour(hass: HomeAssistant) -> None:
    """Four quarter-hour readings become a single StatisticData row carrying the whole hour."""
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass, entry, _sub_hourly_response(range(5, 7)), utility_display_name="X"
        )

    stats = add_mock.call_args.args[2]
    assert [(s["start"].hour, round(s["sum"], 3)) for s in stats] == [(5, 1.0), (6, 2.0)]


async def test_partial_trailing_hour_is_deferred_then_imported_whole(hass: HomeAssistant) -> None:
    """A poll landing mid-hour holds that hour back rather than freezing it at half a total.

    The resume point is a single (sum, start) pair, so an hour can't be revised once written —
    importing a half-covered hour would permanently under-count it. Defer it instead; the next
    poll carries the full hour and imports it whole.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    stat_id = statistic_id_for_series(entry.entry_id, "up1", "FORWARD")

    # Poll 1 lands mid-hour: hour 05 complete, hour 06 only half published.
    partial = _sub_hourly_response(range(5, 7))
    series = partial.usage_points[0].series[0]
    truncated = MeterReadingSeries(
        meter_reading_id=series.meter_reading_id,
        reading_type=series.reading_type,
        readings=series.readings[:6],  # 4 readings for hour 05, 2 for hour 06
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[truncated])
    await import_usage_statistics(
        hass,
        entry,
        UsageResponse(updated=None, usage_points=[up], new_credentials=None),
        utility_display_name="X",
    )
    await async_wait_recording_done(hass)
    assert await _recorded_sums(hass, stat_id) == [(5, 1.0)]  # hour 06 held back, not halved

    # Poll 2 carries hour 06 complete — it lands at its full 1 kWh.
    await import_usage_statistics(
        hass, entry, _sub_hourly_response(range(5, 8)), utility_display_name="X"
    )
    await async_wait_recording_done(hass)
    assert await _recorded_sums(hass, stat_id) == [(5, 1.0), (6, 2.0), (7, 3.0)]


def _per_interval_cost_response() -> UsageResponse:
    """A FORWARD electricity response where each reading itemizes a per-interval cost (Milton)."""
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
    series = MeterReadingSeries(
        meter_reading_id="mr1",
        reading_type=reading_type,
        readings=[
            UsageReading(datetime(2026, 7, 5, 5, tzinfo=UTC), 3600, 1000.0, cost=0.087),
            UsageReading(datetime(2026, 7, 5, 6, tzinfo=UTC), 3600, 1500.0, cost=0.122),
            # A genuinely free hour on a series that really does itemize cost. Must stay on the
            # per-interval path — see test_per_interval_cost_keeps_legitimate_zero_cost_hours.
            UsageReading(datetime(2026, 7, 5, 7, tzinfo=UTC), 3600, 500.0, cost=0.0),
        ],
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[series])
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def test_per_interval_cost_writes_cumulative_cost_stat(hass: HomeAssistant) -> None:
    """Readings with per-interval <cost> drive a cumulative cost stat directly (no summary)."""
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass, entry, _per_interval_cost_response(), utility_display_name="X"
        )

    cost_calls = [c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")]
    assert len(cost_calls) == 1
    metadata, stats = cost_calls[0].args[1], cost_calls[0].args[2]
    assert metadata["unit_of_measurement"] == "CAD"
    assert [round(s["sum"], 3) for s in stats] == [0.087, 0.209, 0.209]  # cumulative


async def test_per_interval_cost_keeps_legitimate_zero_cost_hours(hass: HomeAssistant) -> None:
    """A $0 hour on a genuinely itemized series stays on the per-interval path.

    The fix for #7 works by restricting *which series* are consulted for interval cost, not by
    rejecting zero values: a "costs must be non-zero" test would push a trailing all-zero window
    onto the summary path and mix summary-distributed rows into a per-interval cost statistic.
    Here the third hour is free and the response also carries a summary — the summary must be
    ignored, and the free hour must still produce a row (flat cumulative, not a gap).
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    response = _per_interval_cost_response()
    up = response.usage_points[0]
    with_summary = UsageResponse(
        updated=None,
        usage_points=[
            UsagePoint(
                usage_point_id=up.usage_point_id,
                service_kind=up.service_kind,
                series=up.series,
                summaries=[_summary(datetime(2026, 7, 1, tzinfo=UTC), 31, total_dollars=99.0)],
            )
        ],
        new_credentials=None,
    )
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch(
            "custom_components.greenbutton.statistics._recorded_forward_hours",
            new=AsyncMock(return_value=[(datetime(2026, 7, 5, 5, tzinfo=UTC), 1.0)]),
        ) as recorded_mock,
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(hass, entry, with_summary, utility_display_name="X")

    recorded_mock.assert_not_awaited()  # the summary path was never taken
    cost_calls = [c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")]
    stats = cost_calls[0].args[2]
    assert [round(s["sum"], 3) for s in stats] == [0.087, 0.209, 0.209]


async def test_all_zero_cost_window_stays_on_per_interval_path(hass: HomeAssistant) -> None:
    """A poll where every itemized cost happens to be zero must NOT flip to the summary path.

    This is why the fix for #7 restricts *which series* are consulted rather than rejecting zero
    values. A "costs must be non-zero" test looks equivalent on Milton's feed but is not: the
    cost source is re-decided on every poll, so a quiet window on a utility that genuinely
    itemizes cost (savagedata/Elexicon, which also publish UsageSummary) would flip that one poll
    onto the summary path and append summary-distributed rows into a statistic already holding
    per-interval rows — double-counting every hour the bill covers past the per-interval frontier.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    response = _per_interval_cost_response()
    series = response.usage_points[0].series[0]
    all_zero = MeterReadingSeries(
        meter_reading_id=series.meter_reading_id,
        reading_type=series.reading_type,
        readings=[
            UsageReading(r.start, r.duration_seconds, r.value, cost=0.0) for r in series.readings
        ],
    )
    up = UsagePoint(
        usage_point_id="up1",
        service_kind="electricity",
        series=[all_zero],
        summaries=[_summary(datetime(2026, 7, 1, tzinfo=UTC), 31, total_dollars=99.0)],
    )
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch(
            "custom_components.greenbutton.statistics._recorded_forward_hours",
            new=AsyncMock(return_value=[(datetime(2026, 7, 5, 5, tzinfo=UTC), 1.0)]),
        ) as recorded_mock,
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass,
            entry,
            UsageResponse(updated=None, usage_points=[up], new_credentials=None),
            utility_display_name="X",
        )

    recorded_mock.assert_not_awaited()  # the summary was not distributed over recorded usage
    cost_calls = [c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")]
    assert [round(s["sum"], 3) for s in cost_calls[0].args[2]] == [0.0, 0.0, 0.0]


def _milton_mixed_series_response(*, with_summary: bool = False) -> UsageResponse:
    """Milton Hydro's shape: hourly deltas beside a daily cumulative register snapshot.

    Both series are FORWARD on one UsagePoint, so both map to the same statistic id. The register
    reading is the meter's lifetime total (9,876.543 kWh) and carries the `cost=0` placeholder
    that used to hijack cost-source selection. Synthetic — no customer data.
    """
    delta_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="DELTA_DATA",
        interval_length_seconds=3600,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    bulk_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="BULK_QUANTITY",
        interval_length_seconds=_DAY,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    delta_series = MeterReadingSeries(
        meter_reading_id="hourly",
        reading_type=delta_type,
        readings=[
            UsageReading(datetime(2026, 7, 5, 5, tzinfo=UTC), 3600, 1000.0),
            UsageReading(datetime(2026, 7, 5, 6, tzinfo=UTC), 3600, 1500.0),
        ],
    )
    bulk_series = MeterReadingSeries(
        meter_reading_id="register",
        reading_type=bulk_type,
        readings=[UsageReading(datetime(2026, 7, 5, tzinfo=UTC), _DAY, 9_876_543.0, cost=0.0)],
    )
    summaries = (
        [_summary(datetime(2026, 7, 1, tzinfo=UTC), 31, total_dollars=50.0)] if with_summary else []
    )
    up = UsagePoint(
        usage_point_id="up1",
        service_kind="electricity",
        series=[delta_series, bulk_series],
        summaries=summaries,
    )
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def test_bulk_register_series_is_not_added_to_interval_usage(hass: HomeAssistant) -> None:
    """Issue #6: the cumulative register must not inflate the hourly consumption statistic.

    Both series are FORWARD on one UsagePoint, so both resolve to the same statistic id. Summing
    the register's 9,876.543 kWh lifetime total into the running sum reported it as a single
    interval's consumption — an enormous false spike on the Energy dashboard.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass, entry, _milton_mixed_series_response(), utility_display_name="Milton Hydro"
        )

    usage_calls = [
        c for c in add_mock.call_args_list if not c.args[1]["statistic_id"].endswith("_cost")
    ]
    assert len(usage_calls) == 1  # the register series contributed no write at all
    stats = usage_calls[0].args[2]
    assert [round(s["sum"], 3) for s in stats] == [1.0, 2.5]  # hourly deltas only


async def test_bulk_zero_cost_falls_back_to_billing_summary(hass: HomeAssistant) -> None:
    """Issue #7: the register's `cost=0` placeholder must not suppress the real bill.

    Milton's hourly deltas carry no cost at all; the only cost-bearing reading in the feed is the
    register's zero. Consulting every FORWARD reading let that select the per-interval path and
    write an all-zero cost statistic while the non-zero UsageSummary went unused.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    recorded = [
        (datetime(2026, 7, 5, 5, tzinfo=UTC), 1.0),
        (datetime(2026, 7, 5, 6, tzinfo=UTC), 1.5),
    ]
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch(
            "custom_components.greenbutton.statistics._recorded_forward_hours",
            new=AsyncMock(return_value=recorded),
        ),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass,
            entry,
            _milton_mixed_series_response(with_summary=True),
            utility_display_name="Milton Hydro",
        )

    cost_calls = [c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")]
    assert len(cost_calls) == 1
    # $50 distributed over 1.0 + 1.5 kWh → $20 then $30, cumulative — not the all-zero stat.
    assert [round(s["sum"], 2) for s in cost_calls[0].args[2]] == [20.0, 50.0]


def _series(
    behaviour: str,
    flow_direction: str = "FORWARD",
    *,
    meter_reading_id: str = "mr1",
    unit: str = "WATT_HOURS",
    readings: list[UsageReading] | None = None,
) -> MeterReadingSeries:
    """One hourly FORWARD-by-default series carrying an arbitrary accumulation behaviour."""
    return MeterReadingSeries(
        meter_reading_id=meter_reading_id,
        reading_type=NormalizedReadingType(
            commodity="ELECTRICITY_SECONDARY_METERED",
            flow_direction=flow_direction,
            accumulation_behaviour=behaviour,
            interval_length_seconds=3600,
            unit_of_measure=unit,
            unit_of_measure_symbol="Wh",
            power_of_ten_multiplier=0,
            currency_numeric_code=124,
        ),
        readings=(
            [
                UsageReading(datetime(2026, 7, 5, 5, tzinfo=UTC), 3600, 1000.0),
                UsageReading(datetime(2026, 7, 5, 6, tzinfo=UTC), 3600, 1500.0),
            ]
            if readings is None
            else readings
        ),
    )


def _accumulation_response(
    behaviour: str,
    flow_direction: str = "FORWARD",
    *,
    sibling: MeterReadingSeries | None = None,
) -> UsageResponse:
    """A UsagePoint carrying [behaviour], optionally alongside a second series.

    The sibling is what makes a cumulative register *excludable* — see
    [statistics._is_interval_consumption_series]. Without one, a register is all the utility
    publishes and is imported.
    """
    series = [_series(behaviour, flow_direction)]
    if sibling is not None:
        series.append(sibling)
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=series)
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def _import_and_collect_usage(hass: HomeAssistant, response: UsageResponse) -> list:
    """Import [response] with the recorder mocked out; return the usage (non-cost) stat rows."""
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(hass, entry, response, utility_display_name="X")
    return [c for c in add_mock.call_args_list if not c.args[1]["statistic_id"].endswith("_cost")]


async def test_unrecognized_accumulation_behaviour_still_imports(hass: HomeAssistant) -> None:
    """Exclusion is a blacklist: an unknown behaviour keeps importing as it always has.

    Regression guard against the whitelist ("import only DELTA_DATA") that was tried first. ESPI
    codes we don't map — and any feed omitting `accumulationBehaviour` entirely — normalize to
    "OTHER", and a whitelist drops every one of them: zero statistics written, an empty Energy
    dashboard, and nothing above DEBUG to say why.
    """
    usage_calls = await _import_and_collect_usage(hass, _accumulation_response("OTHER"))
    assert len(usage_calls) == 1
    assert [round(s["sum"], 3) for s in usage_calls[0].args[2]] == [1.0, 2.5]


async def test_reverse_non_delta_series_still_imports(hass: HomeAssistant) -> None:
    """Solar export on a non-DELTA_DATA behaviour must survive too — same whitelist trap."""
    usage_calls = await _import_and_collect_usage(
        hass, _accumulation_response("SUMMATION", flow_direction="REVERSE")
    )
    assert len(usage_calls) == 1
    assert usage_calls[0].args[1]["statistic_id"].endswith("_reverse")


async def test_every_cumulative_behaviour_is_excluded_when_superseded(
    hass: HomeAssistant,
) -> None:
    """BULK_QUANTITY isn't special — every register behaviour loses to a same-flow sibling.

    This is Milton Hydro's shape (issue #6): an hourly DELTA_DATA consumption series and a
    register snapshot on one UsagePoint, both FORWARD, both mapping to one statistic_id.
    CONTINUOUS_CUMULATIVE (ESPI 2) is the one that matters: it used to normalize to "OTHER" and
    would have slipped straight past a name-based exclusion.

    Only the sibling's readings may land — 1.0 then 2.5 kWh cumulative. If the register were
    summed in too, the sums would be doubled.
    """
    for behaviour in ("BULK_QUANTITY", "CUMULATIVE", "CONTINUOUS_CUMULATIVE"):
        calls = await _import_and_collect_usage(
            hass,
            _accumulation_response(
                behaviour, sibling=_series("DELTA_DATA", meter_reading_id="mr2")
            ),
        )
        assert len(calls) == 1, f"{behaviour} should not be summed into a consumption statistic"
        assert [round(s["sum"], 3) for s in calls[0].args[2]] == [1.0, 2.5], behaviour


async def test_lone_cumulative_series_still_imports(hass: HomeAssistant) -> None:
    """A register with no sibling is all the utility publishes — import it.

    Consumers Energy (via UtilityAPI) publishes one reading per billing period, genuine
    per-period consumption, mislabelled BULK_QUANTITY. Excluding on the name alone dropped
    100% of that feed and left an empty Energy dashboard.
    """
    for behaviour in ("BULK_QUANTITY", "CUMULATIVE", "CONTINUOUS_CUMULATIVE"):
        calls = await _import_and_collect_usage(hass, _accumulation_response(behaviour))
        assert len(calls) == 1, f"{behaviour} with no sibling must still import"
        assert [round(s["sum"], 3) for s in calls[0].args[2]] == [1.0, 2.5], behaviour


async def test_register_survives_a_sibling_of_the_other_flow_direction(
    hass: HomeAssistant,
) -> None:
    """A REVERSE delta series doesn't supersede a FORWARD register — no statistic_id collision.

    Excluding on "any non-cumulative sibling" would drop the only consumption data there is
    and leave the account showing solar export but no usage.
    """
    calls = await _import_and_collect_usage(
        hass,
        _accumulation_response(
            "BULK_QUANTITY",
            sibling=_series("DELTA_DATA", flow_direction="REVERSE", meter_reading_id="mr2"),
        ),
    )
    assert {c.args[1]["statistic_id"].rsplit("_", 1)[-1] for c in calls} == {"forward", "reverse"}


async def test_billing_period_reading_spreads_across_its_hours(hass: HomeAssistant) -> None:
    """A month-long reading becomes one row per hour, not a single spike at the period start.

    Consumers Energy's shape: one reading per billing period. ESPI publishes an inclusive end,
    so a 3-hour period is 10799s, and the total must survive the split exactly.
    """
    up = UsagePoint(
        usage_point_id="up1",
        service_kind="electricity",
        series=[
            _series(
                "BULK_QUANTITY",
                readings=[UsageReading(datetime(2026, 6, 2, tzinfo=UTC), 10799, 3000.0)],
            )
        ],
    )
    calls = await _import_and_collect_usage(
        hass, UsageResponse(updated=None, usage_points=[up], new_credentials=None)
    )
    assert len(calls) == 1
    rows = calls[0].args[2]
    assert [r["start"] for r in rows] == [datetime(2026, 6, 2, h, tzinfo=UTC) for h in (0, 1, 2)]
    # 3 kWh spread evenly, accumulated: the trailing hour is 1s short (inclusive end) and must
    # NOT be deferred — a closed billing period is never re-published.
    assert [round(r["sum"], 3) for r in rows] == [1.0, 2.0, 3.0]


async def test_unmappable_unit_on_every_series_logs_error(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A UsagePoint whose every series is unrepresentable writes nothing — and says so loudly.

    That's an empty Energy dashboard, which has to be diagnosable from the log alone. Since a
    register is now only excluded when a sibling supersedes it, the remaining way to import
    nothing is a unit we have no HA mapping for.
    """
    up = UsagePoint(
        usage_point_id="up1",
        service_kind="electricity",
        series=[_series("DELTA_DATA", unit="OTHER")],
    )
    response = UsageResponse(updated=None, usage_points=[up], new_credentials=None)
    with caplog.at_level(logging.ERROR, logger="custom_components.greenbutton.statistics"):
        assert await _import_and_collect_usage(hass, response) == []
    assert "no importable consumption series" in caplog.text


def _summary_only_response() -> UsageResponse:
    """A monthly UsageSummary with NO per-interval <cost> and NO readings in the response.

    This is the Burlington shape *as an incremental poll sees it*: the summary is published weeks
    after its period, so the period's readings aren't here — they're already in the recorder, and
    the importer reads them back to distribute the bill.
    """
    up = UsagePoint(
        usage_point_id="up1",
        service_kind="electricity",
        series=[],
        summaries=[_summary(datetime(2026, 4, 1, tzinfo=UTC), 30, total_dollars=40.0)],
    )
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def test_summary_cost_distributes_over_recorded_usage(hass: HomeAssistant) -> None:
    """Burlington: a UsageSummary total is spread across the period's *recorded* usage.

    The period's readings are not in this response (a bill publishes weeks late); the importer
    recovers them from the recorder. $40 over 1 kWh + 3 kWh → $10 then $30 → cumulative 10, 40.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    recorded = [
        (datetime(2026, 4, 3, 5, tzinfo=UTC), 1.0),
        (datetime(2026, 4, 3, 6, tzinfo=UTC), 3.0),
    ]
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch(
            "custom_components.greenbutton.statistics._recorded_forward_hours",
            new=AsyncMock(return_value=recorded),
        ),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass, entry, _summary_only_response(), utility_display_name="X"
        )

    cost_calls = [c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")]
    assert len(cost_calls) == 1  # summary path writes a cost stat from recorded usage
    stats = cost_calls[0].args[2]
    assert [round(s["sum"], 2) for s in stats] == [10.0, 40.0]


async def test_summary_cost_skipped_when_no_recorded_usage(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bill whose period has no recorded usage yet (e.g. predates the backfill) writes nothing.

    And says so. No cost statistic is registered at all in this case, so it's simply missing from
    the Energy dashboard's picker — a user who asks why has nothing in the log to go on unless the
    skip explains itself. This is the El Paso sandbox shape: a year of bills, one day of usage.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        caplog.at_level(logging.INFO, logger="custom_components.greenbutton.statistics"),
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch(
            "custom_components.greenbutton.statistics._recorded_forward_hours",
            new=AsyncMock(return_value=[]),
        ),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass, entry, _summary_only_response(), utility_display_name="X"
        )

    cost_calls = [c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")]
    assert not cost_calls
    assert "No cost statistic for usage point" in caplog.text
    assert "nothing to distribute the bills across" in caplog.text


async def test_summary_cost_deferred_until_hass_started(hass: HomeAssistant) -> None:
    """Before HA has started, the cost pass must NOT block on the recorder — it defers.

    Regression guard for the startup deadlock: the recorder thread doesn't drain its queue until
    EVENT_HOMEASSISTANT_STARTED, and HA doesn't fire STARTED until config-entry setup returns, so
    awaiting `async_block_till_done()` inside `async_config_entry_first_refresh()` hangs until HA's
    300s setup timeout cancels the entry ("Setup of config entry ... cancelled"). The usage import
    must still complete inline; only the recorder-dependent cost pass waits for start.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    recorded = [
        (datetime(2026, 4, 3, 5, tzinfo=UTC), 1.0),
        (datetime(2026, 4, 3, 6, tzinfo=UTC), 3.0),
    ]
    hass.set_state(CoreState.starting)
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch(
            "custom_components.greenbutton.statistics._recorded_forward_hours",
            new=AsyncMock(return_value=recorded),
        ),
        patch(
            "custom_components.greenbutton.statistics.get_instance",
        ) as get_instance_mock,
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        get_instance_mock.return_value.async_block_till_done = AsyncMock()

        await import_usage_statistics(
            hass, entry, _summary_only_response(), utility_display_name="X"
        )

        # Nothing recorder-blocking may happen while HA is still starting.
        get_instance_mock.return_value.async_block_till_done.assert_not_awaited()
        assert not [
            c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")
        ]

        # ...and the deferred pass runs (and blocks safely) once HA is up.
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()

        get_instance_mock.return_value.async_block_till_done.assert_awaited_once()
        cost_calls = [
            c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")
        ]

    assert len(cost_calls) == 1
    assert [round(s["sum"], 2) for s in cost_calls[0].args[2]] == [10.0, 40.0]


def test_statistic_id_is_scoped_per_entry() -> None:
    """Two entries on the same utility point must produce distinct statistic IDs.

    This is the invariant that lets a tester swap a sandbox account for a real one without
    bleeding data between them in the Energy dashboard — see README Roadmap.
    """
    id_a = statistic_id_for_series("entry_a", "up_123", "FORWARD")
    id_b = statistic_id_for_series("entry_b", "up_123", "FORWARD")
    assert id_a != id_b
    assert id_a.startswith("greenbutton:entry_a_")
    assert id_b.startswith("greenbutton:entry_b_")


def test_statistic_id_differentiates_flow_directions() -> None:
    """Same usage point, opposite flow direction → different statistic IDs.

    Solar PV would emit both FORWARD (consumption) and REVERSE (export) — they must be
    separate series in HA so the Energy dashboard can graph them separately.
    """
    forward = statistic_id_for_series("entry_a", "up_123", "FORWARD")
    reverse = statistic_id_for_series("entry_a", "up_123", "REVERSE")
    assert forward != reverse


def test_statistic_id_lowercases_flow_direction() -> None:
    """Flow casing is normalized so a future server enum-rename doesn't shift the id."""
    upper = statistic_id_for_series("entry_a", "up_123", "FORWARD")
    lower = statistic_id_for_series("entry_a", "up_123", "forward")
    assert upper == lower


def test_statistic_id_prefix_matches_all_ids_for_an_entry() -> None:
    """The remove-entry purge filter must catch every id produced by `statistic_id_for_series`.

    The prefix is the load-bearing surface for async_remove_entry — if these drift the
    purge silently leaks orphan rows.
    """
    prefix = statistic_id_prefix_for_entry("entry_a")
    assert statistic_id_for_series("entry_a", "up_1", "FORWARD").startswith(prefix)
    assert statistic_id_for_series("entry_a", "up_2", "REVERSE").startswith(prefix)
    # …and crucially, *doesn't* catch a different entry's ids
    assert not statistic_id_for_series("entry_b", "up_1", "FORWARD").startswith(prefix)


def test_statistic_id_slugifies_real_world_ulid_and_uuid_inputs() -> None:
    """Real production inputs (HA ULID entry_id, ESPI UUID usage_point_id) must produce a
    valid slug after the `:` — HA's external-statistics machinery rejects mixed case or
    hyphens with "Invalid statistic_id", which kills async_setup_entry on first refresh.
    """
    sid = statistic_id_for_series(
        "01KT5B7TVYNVZY86P0PH0EPTAB",  # ULID — uppercase + digits
        "e082e9a9-390b-58fb-8ca5-4ee707c95652",  # UUID — hex + hyphens
        "FORWARD",
    )
    # No uppercase letters, no hyphens, only [a-z0-9_:] anywhere in the id.
    after_colon = sid.split(":", 1)[1]
    assert all(c.isalnum() or c == "_" for c in after_colon), sid
    assert after_colon.islower() or not any(c.isalpha() for c in after_colon), sid
    # And the same entry id still produces the same prefix used by async_remove_entry.
    assert sid.startswith(statistic_id_prefix_for_entry("01KT5B7TVYNVZY86P0PH0EPTAB"))


async def _cost_sums(hass, summaries: list[BillingSummary]) -> list[float]:
    """Import [summaries] against a synthetic 1 kWh/hour usage history; return the cost rows' sums.

    `_recorded_forward_hours` is stubbed to answer for whatever window it's asked about, so each
    summary spreads its total evenly over its own hours and the arithmetic below is exact.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    up = UsagePoint(
        usage_point_id="up1", service_kind="electricity", series=[], summaries=summaries
    )
    response = UsageResponse(updated=None, usage_points=[up], new_credentials=None)

    async def _hours(_hass, _entry, _up, period_start, period_end):
        hours, hour = [], period_start
        while hour < period_end:
            hours.append((hour, 1.0))
            hour += timedelta(hours=1)
        return hours

    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch("custom_components.greenbutton.statistics._recorded_forward_hours", new=_hours),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(hass, entry, response, utility_display_name="X")

    cost_calls = [c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")]
    return [] if not cost_calls else [s["sum"] for s in cost_calls[0].args[2]]


async def test_a_later_bill_replaces_an_earlier_ones_overlapping_hours(
    hass: HomeAssistant,
) -> None:
    """Where two bills cover the same hour, the later one's price is what stands.

    Consecutive bills overlap by a meter-read day, because a billing period is inclusive of both
    reads — every one of El Paso Electric's twelve monthly bills laps a day over the previous, and
    Burlington and Elexicon are the same. Each bill prices all of its own hours, and the later
    statement simply overwrites the shared ones. Nothing is dropped and nothing is guessed at.

    Apr 1-3 at $48 over 48 h = $1/h. Apr 2-4 at $96 over 48 h = $2/h. The 24 hours of Apr 1 keep
    $1; the 48 hours from Apr 2 on are restated at $2 → $24 + $96 = $120 across 72 hours.
    """
    first = _summary(datetime(2026, 4, 1, tzinfo=UTC), 2, 48.0)
    later = _summary(datetime(2026, 4, 2, tzinfo=UTC), 2, 96.0)
    sums = await _cost_sums(hass, [later, first])  # feed order must not matter

    assert len(sums) == 72
    assert round(sums[-1], 2) == 120.0
    assert round(sums[23], 2) == 24.0  # Apr 1 costed at the first bill's rate throughout


async def test_an_exact_duplicate_bill_costs_the_period_once(hass: HomeAssistant) -> None:
    """A period repeated across a paginated feed rewrites identical values, changing nothing.

    Under the old any-overlap-is-a-duplicate rule this needed detecting; replacing makes it a
    no-op for free. Costing both would double the period in the Energy dashboard.
    """
    bill = _summary(datetime(2026, 4, 1, tzinfo=UTC), 2, 48.0)
    assert round((await _cost_sums(hass, [bill, bill]))[-1], 2) == 48.0


async def test_a_rollup_only_prices_hours_no_bill_covers(hass: HomeAssistant) -> None:
    """A coarse rollup published beside the per-bill totals loses every hour a bill also states.

    It keeps the gaps, priced at its own average rate — no bill covers those hours, so its figure
    is the only evidence there is for them. Apr 1-5 at $96 over 96 h = $1/h; the two days Apr 2-4
    are restated by the $96/48 h = $2/h bill. So 24 h at $1 + 48 h at $2 + 24 h at $1 = $144.
    """
    rollup = _summary(datetime(2026, 4, 1, tzinfo=UTC), 4, 96.0)
    bill = _summary(datetime(2026, 4, 2, tzinfo=UTC), 2, 96.0)
    sums = await _cost_sums(hass, [rollup, bill])

    assert len(sums) == 96
    assert round(sums[23], 2) == 24.0  # Apr 1 — rollup's rate, no bill covers it
    assert round(sums[-1], 2) == 144.0


async def test_a_zero_cost_placeholder_never_blanks_out_a_real_bill(hass: HomeAssistant) -> None:
    """Test-lab feeds emit $0 summaries beside real ones; they must not overwrite anything."""
    placeholder = _summary(datetime(2026, 4, 1, tzinfo=UTC), 2, 0.0)
    real = _summary(datetime(2026, 4, 1, tzinfo=UTC), 2, 48.0)
    assert round((await _cost_sums(hass, [real, placeholder]))[-1], 2) == 48.0
