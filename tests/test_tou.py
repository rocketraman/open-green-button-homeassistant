"""Tests for the Ontario IESO TOU classifier + ESPI cost-detail note mapping."""

from __future__ import annotations

from datetime import UTC, date, datetime

from custom_components.greenbutton.tou import (
    MID_PEAK,
    OFF_PEAK,
    ON_PEAK,
    cost_detail_tou_bucket,
    ontario_holiday_pricing_dates,
    ontario_tou_bucket,
)


def test_summer_weekday_buckets() -> None:
    """11:00-17:00 on-peak; 7:00-11:00 + 17:00-19:00 mid-peak; else off-peak.

    Uses 2024-07-08 (a Monday in July) for the weekday + summer combination. Hours are local
    America/Toronto so we feed instants computed by hand (UTC = local + 4h DST offset).
    """
    # 03:00 UTC = 23:00 EDT Sunday-night-into-Monday — still weekend off-peak
    # 11:00 UTC = 07:00 EDT Mon — mid-peak starts
    # 15:00 UTC = 11:00 EDT Mon — on-peak starts
    # 21:00 UTC = 17:00 EDT Mon — mid-peak resumes
    # 23:00 UTC = 19:00 EDT Mon — off-peak resumes
    assert ontario_tou_bucket(datetime(2024, 7, 8, 6, 0, tzinfo=UTC)) == OFF_PEAK  # 02:00 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 11, 0, tzinfo=UTC)) == MID_PEAK  # 07:00 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 14, 59, tzinfo=UTC)) == MID_PEAK  # 10:59 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 15, 0, tzinfo=UTC)) == ON_PEAK  # 11:00 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 20, 59, tzinfo=UTC)) == ON_PEAK  # 16:59 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 21, 0, tzinfo=UTC)) == MID_PEAK  # 17:00 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 23, 0, tzinfo=UTC)) == OFF_PEAK  # 19:00 EDT


def test_winter_weekday_buckets() -> None:
    """Winter inverts on/mid: 7-11 + 17-19 are on-peak, 11-17 is mid-peak.

    Uses 2024-01-15 (a Monday in January). UTC = local + 5h EST offset.
    """
    assert ontario_tou_bucket(datetime(2024, 1, 15, 11, 0, tzinfo=UTC)) == OFF_PEAK  # 06:00 EST
    assert ontario_tou_bucket(datetime(2024, 1, 15, 12, 0, tzinfo=UTC)) == ON_PEAK  # 07:00 EST
    assert ontario_tou_bucket(datetime(2024, 1, 15, 16, 0, tzinfo=UTC)) == MID_PEAK  # 11:00 EST
    assert ontario_tou_bucket(datetime(2024, 1, 15, 22, 0, tzinfo=UTC)) == ON_PEAK  # 17:00 EST
    assert ontario_tou_bucket(datetime(2024, 1, 16, 0, 0, tzinfo=UTC)) == OFF_PEAK  # 19:00 EST Mon


def test_weekends_always_off_peak() -> None:
    """Every hour of a Sat/Sun is off-peak regardless of season."""
    # 2024-07-13 = Saturday, 15:00 UTC = 11:00 EDT (would be on-peak on a weekday)
    assert ontario_tou_bucket(datetime(2024, 7, 13, 15, 0, tzinfo=UTC)) == OFF_PEAK
    # 2024-01-14 = Sunday, 12:00 UTC = 07:00 EST (would be on-peak in winter on a weekday)
    assert ontario_tou_bucket(datetime(2024, 1, 14, 12, 0, tzinfo=UTC)) == OFF_PEAK


def test_cost_detail_note_to_bucket() -> None:
    """ESPI line-item notes map to canonical bucket names; non-TOU items → None."""
    # Actual Ontario IESO labels seen in the test-lab feed.
    assert cost_detail_tou_bucket("Off Peak-Summer") == OFF_PEAK
    assert cost_detail_tou_bucket("Mid Peak-Summer") == MID_PEAK
    assert cost_detail_tou_bucket("On Peak-Summer") == ON_PEAK
    # Variants we might see from other utilities or different seasons.
    assert cost_detail_tou_bucket("Off-Peak Winter") == OFF_PEAK
    assert cost_detail_tou_bucket("ON PEAK") == ON_PEAK
    assert cost_detail_tou_bucket("on peak winter") == ON_PEAK
    # Non-TOU items return None — caller distributes those flat per kWh.
    assert cost_detail_tou_bucket("Delivery") is None
    assert cost_detail_tou_bucket("Regulatory Charges") is None
    assert cost_detail_tou_bucket("Global Adjustment") is None
    assert cost_detail_tou_bucket("Ontario Electricity Rebate") is None
    assert cost_detail_tou_bucket(None) is None


def test_2026_holiday_schedule_matches_oeb_table() -> None:
    """The computed schedule must reproduce OEB's published 2026 table exactly.

    Source: OEB "Holiday schedule - Time-of-Use and Ultra-Low Overnight". Note Boxing Day
    2026-12-26 is a Saturday, so the OEB lists it observed on Monday 2026-12-28.
    """
    assert ontario_holiday_pricing_dates(2026) == frozenset(
        {
            date(2026, 1, 1),  # New Year's Day (Thu)
            date(2026, 2, 16),  # Family Day (Mon)
            date(2026, 4, 3),  # Good Friday (Fri)
            date(2026, 5, 18),  # Victoria Day (Mon)
            date(2026, 7, 1),  # Canada Day (Wed)
            date(2026, 8, 3),  # Civic Holiday (Mon)
            date(2026, 9, 7),  # Labour Day (Mon)
            date(2026, 10, 12),  # Thanksgiving (Mon)
            date(2026, 12, 25),  # Christmas Day (Fri)
            date(2026, 12, 28),  # Boxing Day observed (Mon)
        }
    )


def test_holiday_price_rolls_forward_off_a_weekend() -> None:
    """A weekend holiday marks the following weekday; the weekend day itself is already
    off-peak, so it is not what gets returned."""
    prices_2026 = ontario_holiday_pricing_dates(2026)
    assert date(2026, 12, 26) not in prices_2026  # Boxing Day, a Saturday
    assert date(2026, 12, 28) in prices_2026  # observed the following Monday

    # 2028-01-01 is a Saturday → observed Monday 2028-01-03 (skipping Sunday the 2nd).
    prices_2028 = ontario_holiday_pricing_dates(2028)
    assert date(2028, 1, 1) not in prices_2028
    assert date(2028, 1, 3) in prices_2028


def test_holiday_is_off_peak_all_day_summer() -> None:
    """Canada Day 2026 (Wednesday) is off-peak for every hour — including hours that would
    otherwise be mid-peak and on-peak."""
    # 11:00 UTC = 07:00 EDT — mid-peak on an ordinary summer weekday
    assert ontario_tou_bucket(datetime(2026, 7, 1, 11, 0, tzinfo=UTC)) == OFF_PEAK
    # 15:00 UTC = 11:00 EDT — on-peak on an ordinary summer weekday
    assert ontario_tou_bucket(datetime(2026, 7, 1, 15, 0, tzinfo=UTC)) == OFF_PEAK
    # 19:00 UTC = 15:00 EDT — still on-peak at the far end of the afternoon
    assert ontario_tou_bucket(datetime(2026, 7, 1, 19, 0, tzinfo=UTC)) == OFF_PEAK


def test_holiday_is_off_peak_all_day_winter() -> None:
    """Family Day 2026 (Monday) is off-peak all day; 07:00 EST is on-peak in winter."""
    # 12:00 UTC = 07:00 EST
    assert ontario_tou_bucket(datetime(2026, 2, 16, 12, 0, tzinfo=UTC)) == OFF_PEAK
    # The very next day returns to the normal winter schedule — the check is date-scoped,
    # not sticky across a boundary.
    assert ontario_tou_bucket(datetime(2026, 2, 17, 12, 0, tzinfo=UTC)) == ON_PEAK


def test_observed_holiday_on_a_weekday_is_off_peak() -> None:
    """Boxing Day observed Monday 2026-12-28 carries holiday pricing even though the 28th
    is not a calendar holiday in its own right."""
    # 15:00 UTC = 10:00 EST — on-peak on an ordinary winter weekday
    assert ontario_tou_bucket(datetime(2026, 12, 28, 15, 0, tzinfo=UTC)) == OFF_PEAK


def test_non_tou_holidays_keep_normal_weekday_rates() -> None:
    """Only OEB-listed days get holiday pricing — Remembrance Day is deliberately not one."""
    # 2026-11-11 is a Wednesday; 15:00 UTC = 10:00 EST → normal winter on-peak.
    assert date(2026, 11, 11) not in ontario_holiday_pricing_dates(2026)
    assert ontario_tou_bucket(datetime(2026, 11, 11, 15, 0, tzinfo=UTC)) == ON_PEAK
