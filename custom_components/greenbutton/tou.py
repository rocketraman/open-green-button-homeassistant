"""Time-of-use (TOU) classification for utility cost distribution.

Used by [statistics._import_cost_summaries] when an ESPI UsageSummary carries per-TOU-bucket
cost details (Ontario's IESO and some other jurisdictions break out Off-Peak, Mid-Peak,
On-Peak charges as separate `costAdditionalDetailLastPeriod` line items). For each hour we
need to know which bucket it falls in so the right rate applies — which depends on the
utility's jurisdiction. This module exposes one classifier per supported jurisdiction.

Today: only the Ontario IESO schedule is implemented because every currently supported
utility is on it. When we onboard a US or other-Canadian utility on a different schedule,
add a sibling classifier here and extend the dispatch to pick the right one per utility.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo

# Canonical TOU bucket names used across the codebase. The strings match the way ESPI
# `costAdditionalDetailLastPeriod` notes are normalized (see [cost_detail_tou_bucket]) so
# we can look up rates by bucket directly.
OFF_PEAK = "off_peak"
MID_PEAK = "mid_peak"
ON_PEAK = "on_peak"

_ONTARIO_TZ = ZoneInfo("America/Toronto")

# IESO TOU schedule changes between Summer and Winter pricing periods. Summer runs
# May 1 - Oct 31 inclusive in the calendar months sense (the IESO publishes effective
# dates but they reliably line up with the month boundaries we use here).
_SUMMER_MONTHS = range(5, 11)  # May (5) through October (10)

# Which days get off-peak-all-day pricing, and the weekend roll-forward rule, come from the
# Ontario Energy Board's published holiday schedule for Time-of-Use and Ultra-Low Overnight.
# The OEB states: "If a holiday falls on a weekend, the next weekday (that is not also a
# holiday) will have the holiday prices in effect all day."
#   https://www.oeb.ca/consumer-information-and-protection/electricity-rates/
#   holiday-schedule-time-use-and-ultra-low


def _easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous Gregorian / Meeus-Jones-Butcher).

    Exists only because Good Friday has no fixed calendar date — it is Easter minus two days.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    return date(year, (h + l_ - 7 * m + 114) // 31, (h + l_ - 7 * m + 114) % 31 + 1)


def _nth_weekday(year: int, month: int, weekday: int, count: int) -> date:
    """The `count`-th `weekday` (Monday=0) of `month` in `year`."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (count - 1))


@cache
def ontario_holiday_pricing_dates(year: int) -> frozenset[date]:
    """Days of `year` that carry off-peak-all-day TOU pricing in Ontario.

    Recomputes the OEB's published holiday schedule for the requested year, then applies the
    OEB's roll-forward rule: a holiday landing on a weekend contributes its *observed*
    weekday instead, because the weekend itself is already off-peak and only the following
    weekday needs marking. A holiday already on a weekday is returned unchanged.

    These are derived from rules rather than listed outright because most of them move every
    year — Family Day, Good Friday, Victoria Day, Civic Holiday, Labour Day and Thanksgiving
    are all positional — and the observed day depends on which weekday Christmas and Boxing
    Day happen to fall on.

    Every observed date stays inside `year`: New Year's Day can only roll forward into early
    January and Boxing Day into late December. So callers may key a lookup off a single year.

    See the module-level link for the OEB source table this reproduces.
    """
    actual_dates = {
        date(year, 1, 1),  # New Year's Day
        _nth_weekday(year, 2, 0, 3),  # Family Day (3rd Monday in February)
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        # Victoria Day is the Monday preceding May 25, i.e. the Monday on or before May 24.
        date(year, 5, 24) - timedelta(days=date(year, 5, 24).weekday()),  # Victoria Day
        date(year, 7, 1),  # Canada Day
        _nth_weekday(year, 8, 0, 1),  # Civic Holiday (1st Monday in August)
        _nth_weekday(year, 9, 0, 1),  # Labour Day (1st Monday in September)
        _nth_weekday(year, 10, 0, 2),  # Thanksgiving (2nd Monday in October)
        date(year, 12, 25),  # Christmas Day
        date(year, 12, 26),  # Boxing Day
    }

    pricing_days: set[date] = set()
    for holiday in sorted(actual_dates):
        observed = holiday
        # Skip weekends and any day that is itself another holiday's calendar date.
        while observed.weekday() >= 5 or (observed != holiday and observed in actual_dates):
            observed += timedelta(days=1)
        pricing_days.add(observed)
    return frozenset(pricing_days)


def ontario_tou_bucket(dt: datetime) -> str:
    """Classify a tz-aware instant under Ontario's IESO TOU schedule.

    Returns one of [OFF_PEAK], [MID_PEAK], [ON_PEAK].

    Summer (May 1 - Oct 31) weekdays:
      - 11:00 - 17:00 → On-Peak
      -  7:00 - 11:00 and 17:00 - 19:00 → Mid-Peak
      - elsewhere → Off-Peak

    Winter (Nov 1 - Apr 30) weekdays:
      -  7:00 - 11:00 and 17:00 - 19:00 → On-Peak
      - 11:00 - 17:00 → Mid-Peak
      - elsewhere → Off-Peak

    Weekends and every day returned by [ontario_holiday_pricing_dates] are Off-Peak for all
    24 hours, in either season.
    """
    local = dt.astimezone(_ONTARIO_TZ)
    if local.weekday() >= 5:  # 5=Sat, 6=Sun
        return OFF_PEAK
    if local.date() in ontario_holiday_pricing_dates(local.year):
        return OFF_PEAK
    hour = local.hour
    if local.month in _SUMMER_MONTHS:
        if 11 <= hour < 17:
            return ON_PEAK
        if 7 <= hour < 11 or 17 <= hour < 19:
            return MID_PEAK
        return OFF_PEAK
    # Winter:
    if 7 <= hour < 11 or 17 <= hour < 19:
        return ON_PEAK
    if 11 <= hour < 17:
        return MID_PEAK
    return OFF_PEAK


def cost_detail_tou_bucket(note: str | None) -> str | None:
    """Map an ESPI `costAdditionalDetailLastPeriod` note to a TOU bucket, or None.

    Ontario utility feeds label TOU line items with notes like "Off Peak-Summer",
    "Mid Peak-Winter", "On Peak". We accept any phrasing variant containing "off/mid/on"
    plus "peak", and ignore the season suffix (we re-derive season from the hour timestamp,
    not the line-item label, so the same `bucket` lookup works year-round).

    Non-TOU detail items (Delivery, Regulatory Charges, Global Adjustment, Ontario
    Electricity Rebate, etc.) return None — the caller distributes those flat per kWh.
    """
    if note is None:
        return None
    n = note.lower().replace("-", " ")
    if "off peak" in n:
        return OFF_PEAK
    if "mid peak" in n:
        return MID_PEAK
    if "on peak" in n:
        return ON_PEAK
    return None
