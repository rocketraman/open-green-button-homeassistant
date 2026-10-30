"""Usage from bills' meter reads (the server's ``enbridgeMeterReads`` quirk; Enbridge Gas).

The bill figures are the maintainer's own Enbridge bills: June 2026 estimated at 63 m³ ($50.98),
July read actually at 21 m³ ($43.27), both at an adjustment factor of 1.004. The correction bills
are the shapes two other accounts in open-green-button#39 received.
"""

from __future__ import annotations

import zoneinfo
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.greenbutton import meter_reads
from custom_components.greenbutton.api import (
    BillingSummary,
    BillMeterReads,
    CostDetail,
    MeterReadingSeries,
    NormalizedReadingType,
    UsagePoint,
    UsageReading,
    UsageResponse,
)
from custom_components.greenbutton.const import QUIRK_ENBRIDGE_METER_READS
from custom_components.greenbutton.espi import parse_usage_feed
from custom_components.greenbutton.statistics import (
    import_usage_statistics,
    response_needs_import_migration,
    statistic_id_for_cost,
    statistic_id_for_series,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from homeassistant.core import HomeAssistant

_TORONTO = zoneinfo.ZoneInfo("America/Toronto")
_USAGE_ID = statistic_id_for_series("01TESTENTRY", "up1", "FORWARD", "gas")
_COST_ID = statistic_id_for_cost("01TESTENTRY", "up1")


@pytest.fixture(autouse=True)
def _toronto() -> Generator[None]:
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(_TORONTO)
    yield
    dt_util.set_default_time_zone(original)


def _bill(
    previous: tuple[date, float],
    current: tuple[date, float],
    *,
    actual: bool,
    dollars: float,
    bill_date: date | None = None,
    corrects: float | None = None,
) -> BillingSummary:
    """A bill from [previous] read to [current] read, shaped as Enbridge's feed sends it.

    [corrects] makes it a correction bill: it then lists the estimate it corrects before the
    actual read, its previous reads are the odd pair real corrections carry, and it is marked
    estimated like the real ones.
    """
    (previous_date, previous_read), (current_date, current_read) = previous, current
    return BillingSummary(
        # The feed's own period: UTC midnight after the previous read, one day short.
        billing_period_start=datetime.combine(
            previous_date + timedelta(days=1), datetime.min.time(), UTC
        ),
        billing_period_duration_seconds=((current_date - previous_date).days - 1) * 86400,
        # As Enbridge states a total: billLastPeriod matching an "Amount Due" line at 10^-3, which
        # is what lets a correction's negative total through.
        bill_last_period_raw=round(dollars * 1000),
        cost_additional_last_period_raw=0,
        cost_details=[
            CostDetail(
                amount_raw=round(dollars * 1000),
                note="Amount Due",
                item_kind=10,
                unit_cost_raw=None,
                amount_power_of_ten=-3,
                measurement_uom=80,
            )
        ],
        currency_numeric_code=124,
        meter_reads=BillMeterReads(
            bill_date=bill_date or current_date + timedelta(days=3),
            previous_read_date=previous_date,
            current_read_date=current_date,
            previous_reads=(0.0, previous_read + 2) if corrects is not None else (previous_read,),
            current_reads=(corrects, current_read) if corrects is not None else (current_read,),
            adjustment_factor=1.004,
            quality_of_reading=19 if actual and corrects is None else 12,
        ),
    )


_MAY29 = (date(2026, 5, 29), 35500.0)
_JUN29 = (date(2026, 6, 29), 35563.0)
_JUL30 = (date(2026, 7, 30), 35584.0)
_JUNE = _bill(_MAY29, _JUN29, actual=False, dollars=50.98)
_JULY = _bill(_JUN29, _JUL30, actual=True, dollars=43.27)


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), _TORONTO).astimezone(UTC)


def _gas_series(*readings: UsageReading) -> MeterReadingSeries:
    return MeterReadingSeries(
        meter_reading_id="mr1",
        reading_type=NormalizedReadingType(
            commodity="NATURAL_GAS",
            flow_direction="FORWARD",
            accumulation_behaviour="DELTA_DATA",
            interval_length_seconds=86400,
            unit_of_measure="CUBIC_METERS",
            unit_of_measure_symbol="m³",
            power_of_ten_multiplier=0,
            currency_numeric_code=124,
        ),
        readings=list(readings),
    )


def _response(*bills: BillingSummary, series: list[MeterReadingSeries] | None = None):
    up = UsagePoint(
        usage_point_id="up1", service_kind="GAS", series=series or [], summaries=list(bills)
    )
    return UsageResponse(
        updated=None,
        usage_points=[up],
        new_credentials=None,
        quirks=frozenset({QUIRK_ENBRIDGE_METER_READS}),
    )


def _entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    entry.data = {}
    return entry


async def _import(hass: HomeAssistant, response: UsageResponse, *, fresh: bool = False):
    report = await import_usage_statistics(hass, _entry(), response, "Enbridge", fresh=fresh)
    await async_wait_recording_done(hass)
    return report["up1"]


async def _changes(hass: HomeAssistant, stat_id: str) -> list[tuple[datetime, float]]:
    by_id = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2025, 1, 1, tzinfo=UTC),
        None,
        {stat_id},
        "hour",
        None,
        {"sum", "change"},
    )
    return [
        (datetime.fromtimestamp(row["start"], UTC), row["change"]) for row in by_id.get(stat_id, [])
    ]


def _total(changes: list[tuple[datetime, float]], start: datetime, end: datetime) -> float:
    return sum(change for hour, change in changes if start <= hour < end)


# --- the chain ---------------------------------------------------------------------------------


def test_an_estimate_between_actual_reads_is_bracketed_and_usage_spreads_across_both() -> None:
    points = meter_reads.meter_read_points([_JUNE, _JULY])
    assert [(p.read_date.isoformat(), p.status) for p in points] == [
        ("2026-05-29", "kept"),
        ("2026-06-29", "bracketed"),
        ("2026-07-30", "kept"),
    ]
    [span] = meter_reads.bill_spans(points, _TORONTO)
    # 63 + 21 billed; (35584 − 35500) × 1.004 = 84.3, rounded as the bills round.
    assert span.quantity == 84
    assert (span.start, span.end) == (
        _local_midnight(date(2026, 5, 30)),
        _local_midnight(date(2026, 7, 31)),
    )
    assert not span.provisional


def test_an_estimate_after_the_newest_actual_read_is_provisional() -> None:
    points = meter_reads.meter_read_points([_JUNE])
    assert [p.status for p in points] == ["kept", "provisional"]
    [span] = meter_reads.bill_spans(points, _TORONTO)
    assert span.quantity == 63
    assert span.provisional
    assert meter_reads.newest_actual_read(points) == datetime(2026, 5, 29, tzinfo=UTC)


def test_a_correction_supersedes_the_estimate_and_its_actual_read_is_kept() -> None:
    """Steven's shape: an April estimate of 53,065, corrected in May to an actual 52,806."""
    mar13, apr15, may13 = (
        (date(2026, 3, 13), 52801.0),
        (date(2026, 4, 15), 53065.0),
        (date(2026, 5, 13), 52806.0),
    )
    april = _bill(mar13, apr15, actual=False, dollars=113.16, bill_date=date(2026, 4, 17))
    correction = _bill(
        mar13, may13, actual=True, dollars=-46.35, bill_date=date(2026, 5, 19), corrects=53065.0
    )
    points = meter_reads.meter_read_points([april, correction])
    assert [(p.read_date.isoformat(), p.value, p.status) for p in points] == [
        ("2026-03-13", 52801.0, "kept"),
        ("2026-04-15", 53065.0, "superseded"),
        ("2026-05-13", 52806.0, "kept"),
    ]
    [span] = meter_reads.bill_spans(points, _TORONTO)
    assert span.quantity == 5  # never the −259 m³ the correction bills


def test_the_later_bill_date_wins_a_read_date_two_bills_share() -> None:
    first = _bill(_MAY29, _JUN29, actual=False, dollars=50.98, bill_date=date(2026, 7, 2))
    restated = _bill(
        _MAY29, (date(2026, 6, 29), 35540.0), actual=True, dollars=40.0, bill_date=date(2026, 7, 9)
    )
    points = meter_reads.meter_read_points([restated, first])
    assert [(p.value, p.actual) for p in points] == [(35500.0, True), (35540.0, True)]


# --- parsing -----------------------------------------------------------------------------------

_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:espi="http://naesb.org/espi">
  <entry>
    <link rel="self" href="https://x/espi/1_1/resource/Subscription/s/UsagePoint/up1"/>
    <content><espi:UsagePoint><espi:ServiceCategory><espi:kind>1</espi:kind></espi:ServiceCategory>
      </espi:UsagePoint></content>
  </entry>
  <entry>
    <link rel="self" href="https://x/espi/1_1/resource/Subscription/s/UsagePoint/up1/UsageSummary/b1"/>
    <link rel="related" type="espi-entry/UsagePoint"
          href="https://x/espi/1_1/resource/Subscription/s/UsagePoint/up1"/>
    <content><espi:UsageSummary>
      <espi:billingPeriod><espi:duration>2678400</espi:duration><espi:start>1782777600</espi:start>
        </espi:billingPeriod>
      <espi:billLastPeriod>43270</espi:billLastPeriod>
      <espi:costAdditionalDetailLastPeriod><espi:amount>43270</espi:amount>
        <espi:note>Amount Due</espi:note>
        <espi:measurement><espi:powerOfTenMultiplier>3</espi:powerOfTenMultiplier>
          <espi:uom>80</espi:uom></espi:measurement></espi:costAdditionalDetailLastPeriod>
      <espi:costAdditionalDetailLastPeriod><espi:note>Bill Date</espi:note>
        <espi:measurement><espi:powerOfTenMultiplier>0</espi:powerOfTenMultiplier>
          <espi:uom>27</espi:uom><espi:value>1785801600</espi:value></espi:measurement>
          </espi:costAdditionalDetailLastPeriod>
      <espi:costAdditionalDetailLastPeriod><espi:note>Billing Period - Current Read Date</espi:note>
        <espi:measurement><espi:powerOfTenMultiplier>0</espi:powerOfTenMultiplier>
          <espi:uom>27</espi:uom><espi:value>1785369600</espi:value></espi:measurement>
          </espi:costAdditionalDetailLastPeriod>
      <espi:costAdditionalDetailLastPeriod>
        <espi:note>Billing Period - Previous Read Date</espi:note>
        <espi:measurement><espi:powerOfTenMultiplier>0</espi:powerOfTenMultiplier>
          <espi:uom>27</espi:uom><espi:value>1782691200</espi:value></espi:measurement>
          </espi:costAdditionalDetailLastPeriod>
      <espi:costAdditionalDetailLastPeriod><espi:note>Current Meter Read</espi:note>
        <espi:measurement><espi:powerOfTenMultiplier>3</espi:powerOfTenMultiplier>
          <espi:uom>114</espi:uom><espi:value>35584000</espi:value></espi:measurement>
          </espi:costAdditionalDetailLastPeriod>
      <espi:costAdditionalDetailLastPeriod><espi:note>Previous Meter Read</espi:note>
        <espi:measurement><espi:powerOfTenMultiplier>3</espi:powerOfTenMultiplier>
          <espi:uom>114</espi:uom><espi:value>35563000</espi:value></espi:measurement>
          </espi:costAdditionalDetailLastPeriod>
      <espi:costAdditionalDetailLastPeriod><espi:note>Adjustment Factor (atmospheric)</espi:note>
        <espi:measurement><espi:powerOfTenMultiplier>6</espi:powerOfTenMultiplier>
          <espi:uom>114</espi:uom><espi:value>1004000</espi:value></espi:measurement>
          </espi:costAdditionalDetailLastPeriod>
      <espi:currency>124</espi:currency>
      <espi:qualityOfReading>19</espi:qualityOfReading>
    </espi:UsageSummary></content>
  </entry>
</feed>"""


def test_meter_reads_are_parsed_only_under_the_quirk() -> None:
    _, [up] = parse_usage_feed(
        _FEED, invert_cost_if_positive_multiplier=True, enbridge_meter_reads=True
    )
    assert up.summaries[0].meter_reads == BillMeterReads(
        bill_date=date(2026, 8, 4),
        previous_read_date=date(2026, 6, 29),
        current_read_date=date(2026, 7, 30),
        previous_reads=(35563.0,),
        current_reads=(35584.0,),
        adjustment_factor=1.004,
        quality_of_reading=19,
    )
    _, [up] = parse_usage_feed(_FEED, invert_cost_if_positive_multiplier=True)
    assert up.summaries[0].meter_reads is None


def test_bills_with_meter_reads_are_the_revision_8_signal() -> None:
    assert response_needs_import_migration(_response(_JUNE), 7) is True
    unquirked = UsageResponse(
        updated=None, usage_points=_response(_JUNE).usage_points, new_credentials=None
    )
    assert response_needs_import_migration(unquirked, 7) is False


# --- importing ---------------------------------------------------------------------------------


async def test_usage_spreads_actual_to_actual_and_each_bill_costs_its_own_period(
    hass: HomeAssistant,
) -> None:
    """The June estimate is dropped: 84 m³ over 62 days. Each bill's dollars stay in its period."""
    await _import(hass, _response(_JUNE, _JULY), fresh=True)

    usage = await _changes(hass, _USAGE_ID)
    may30, jun30, jul31 = (
        _local_midnight(date(2026, m, d)) for m, d in ((5, 30), (6, 30), (7, 31))
    )
    assert usage[0][0] == may30
    assert usage[-1][0] == jul31 - timedelta(hours=1)
    assert round(_total(usage, may30, jul31), 3) == 84
    assert round(_total(usage, may30, jun30), 3) == round(84 * 31 / 62, 3)

    cost = await _changes(hass, _COST_ID)
    assert round(_total(cost, may30, jun30), 2) == 50.98
    assert round(_total(cost, jun30, jul31), 2) == 43.27


async def test_a_provisional_estimate_is_restated_when_the_actual_read_arrives(
    hass: HomeAssistant,
) -> None:
    first = await _import(hass, _response(_JUNE), fresh=True)
    assert first.restated_from is None
    may30, jun30, jul31 = (
        _local_midnight(date(2026, m, d)) for m, d in ((5, 30), (6, 30), (7, 31))
    )
    assert round(_total(await _changes(hass, _USAGE_ID), may30, jun30), 3) == 63

    later = await _import(hass, _response(_JUNE, _JULY))

    usage = await _changes(hass, _USAGE_ID)
    assert round(_total(usage, may30, jun30), 3) == 42
    assert round(_total(usage, may30, jul31), 3) == 84
    assert later.restated_from == may30
    [row] = [m.as_dict() for m in later.mismatches]
    assert (row["kind"], row["reading"], row["bill"]) == ("restated", 63.0, 42.0)

    cost = await _changes(hass, _COST_ID)
    assert round(_total(cost, may30, jun30), 2) == 50.98
    assert round(_total(cost, jun30, jul31), 2) == 43.27


async def test_billing_period_readings_lose_their_hours_to_the_bill(hass: HomeAssistant) -> None:
    """The split December: a 34-day bill sent as a 27-day reading and a 7-day reading of 0.

    Neither reading is a day or shorter, so the bill's 364 m³ spreads over all 34 days. Both
    readings disagree with what the bill says for their days, so both are reported.
    """
    nov27, dec31 = (date(2025, 11, 27), 33433.0), (date(2025, 12, 31), 33796.0)
    december = _bill(nov27, dec31, actual=True, dollars=170.0)
    start = _local_midnight(date(2025, 11, 28))
    series = _gas_series(
        UsageReading(start, 27 * 86400, 363.0),
        UsageReading(start + timedelta(days=27), 7 * 86400, 0.0),
    )
    report = await _import(hass, _response(december, series=[series]), fresh=True)

    usage = await _changes(hass, _USAGE_ID)
    end = _local_midnight(date(2026, 1, 1))
    assert round(_total(usage, start, end), 3) == 364
    per_hour = {round(change, 6) for hour, change in usage if start <= hour < end}
    assert len(per_hour) == 1
    assert [(m.reading, round(m.bill, 1)) for m in report.mismatches] == [
        (363.0, round(364 * 27 / 34, 1)),
        (0.0, round(364 * 7 / 34, 1)),
    ]


async def test_a_reading_that_agrees_with_the_bill_is_counted_not_listed(
    hass: HomeAssistant,
) -> None:
    series = _gas_series(UsageReading(_local_midnight(date(2026, 6, 30)), 31 * 86400, 21.0))
    report = await _import(hass, _response(_JUNE, _JULY, series=[series]), fresh=True)
    # 84 m³ over 62 days gives July's 31 days 42 m³; the reading says 21, so it IS listed.
    assert len(report.mismatches) == 1
    series = _gas_series(UsageReading(_local_midnight(date(2026, 5, 30)), 62 * 86400, 84.0))
    report = await _import(hass, _response(_JUNE, _JULY, series=[series]), fresh=True)
    assert report.mismatches == []
    assert report.agreeing == [_local_midnight(date(2026, 5, 30))]


async def test_daily_readings_keep_their_hours_and_the_bill_fills_the_rest(
    hass: HomeAssistant,
) -> None:
    """A meter with readings a day long keeps them; the bill covers only the days they miss."""
    jul1 = _local_midnight(date(2026, 7, 1))
    series = _gas_series(UsageReading(jul1, 86400, 5.0))
    await _import(hass, _response(_JUNE, _JULY, series=[series]), fresh=True)
    usage = await _changes(hass, _USAGE_ID)
    assert round(_total(usage, jul1, jul1 + timedelta(days=1)), 3) == 5
    jul2 = jul1 + timedelta(days=1)
    assert round(_total(usage, jul2, jul2 + timedelta(days=1)), 3) == round(84 / 62, 3)


async def test_bills_import_on_a_poll_that_carries_no_readings(hass: HomeAssistant) -> None:
    """Enbridge's IntervalReadings stop months before its bills; the bills still need a home."""
    await _import(hass, _response(_JUNE, _JULY, series=[]), fresh=True)
    usage = await _changes(hass, _USAGE_ID)
    assert round(sum(change for _, change in usage), 3) == 84


async def test_a_correction_bill_is_costed_together_with_the_bills_it_corrects(
    hass: HomeAssistant,
) -> None:
    """Steven's April bill charged $113.16 on an estimate; the correction credits $46.35.

    The $66.81 they come to spreads over the correction's whole span, so no day in it is priced
    below zero — as May 1–13 would be under the credit alone.
    """
    mar13, apr15 = (date(2026, 3, 13), 52801.0), (date(2026, 4, 15), 53065.0)
    may13 = (date(2026, 5, 13), 52806.0)
    march = _bill((date(2026, 2, 13), 52509.0), mar13, actual=True, dollars=124.30)
    april = _bill(mar13, apr15, actual=False, dollars=113.16, bill_date=date(2026, 4, 17))
    correction = _bill(
        mar13, may13, actual=True, dollars=-46.35, bill_date=date(2026, 5, 19), corrects=53065.0
    )
    await _import(hass, _response(march, april, correction), fresh=True)
    cost = await _changes(hass, _COST_ID)
    mar14, may14 = _local_midnight(date(2026, 3, 14)), _local_midnight(date(2026, 5, 14))
    assert round(_total(cost, mar14, may14), 2) == round(113.16 - 46.35, 2)
    assert round(sum(change for _, change in cost), 2) == round(124.30 + 113.16 - 46.35, 2)
    assert all(change >= 0 for _, change in cost)
