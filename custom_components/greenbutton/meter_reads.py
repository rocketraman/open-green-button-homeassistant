"""Usage from the meter reads printed on bills, for a custodian whose IntervalReadings fall short.

Enbridge Gas reads most residential meters by hand every other month and estimates the months in
between. Its IntervalReadings are one per billing period, stop months before its bills do, and are
never revised when an actual read corrects an estimate. Every bill, though, states the meter's
register — previous and current read, their dates, and the atmospheric adjustment factor — and
those chain without gaps. Under the server's ``summaryMeterReads`` quirk the parser reads them
into [BillMeterReads]; this module turns a meter's bills into hourly usage.

**From bills to a chain of reads.** Each bill contributes its current read as a point; the
earliest bill's previous read starts the chain. Where two bills state the same read date the later
Bill Date wins, and between equal dates an actual read beats an estimate. Then:

* An estimate strictly inside a later bill's previous-to-current span is *superseded*: that is how
  a correction bill replaces the estimate it corrects.
* An estimate with an actual read on both sides is *bracketed* and dropped. Usage is spread evenly
  from actual read to actual read, which is closer to what was used than the estimate's split.
* An estimate after the newest actual read is *provisional*: kept until the next actual read
  brackets it, when the span is restated.

A correction bill (two current reads: the estimate, then the actual) is itself marked estimated,
but its final read is the actual one — the correction exists because an actual read came in.

**From the chain to hours.** Usage between consecutive points = register difference × the
adjustment factor of the bill ending the span, rounded to a whole unit as the bill rounds it. It is
spread evenly over the span: from local midnight after the earlier read through the end of the
later read's day, in Home Assistant's time zone (Enbridge's own ``tzOffset`` is unusable).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import BillingSummary, UsagePoint

# ESPI qualityOfReading for a read the custodian took off the meter. Enbridge prints 19 as
# "Actual" and 12 as "Estimated"; anything other than 19 is treated as an estimate.
QUALITY_ACTUAL = 19

STATUS_KEPT = "kept"
STATUS_PROVISIONAL = "provisional"
STATUS_SUPERSEDED = "superseded"
STATUS_BRACKETED = "bracketed"

# A reading no longer than this is finer than any bill and always wins its hours over one; a
# longer one loses them to a bill's meter reads. A fixed day rather than a fraction of the bill:
# one Enbridge bill of 34 days arrived as a 27-day reading and a 7-day reading of 0 m³, and a
# relative rule would have let the 7-day one zero out a week the bill says was used.
FINE_READING_MAX_SECONDS = 86_400

# Within this, a dropped reading and the bill's usage over the same hours agree: bills round their
# volume to a whole unit, and a reading split across two spans rounds on each side.
_AGREEMENT_TOLERANCE = 1.0


@dataclass(frozen=True, slots=True)
class MeterReadPoint:
    """One meter read as the bills state it, and what the chain made of it."""

    read_date: date
    value: float
    actual: bool
    bill_date: date | None
    adjustment_factor: float
    status: str


@dataclass(frozen=True, slots=True)
class BillSpan:
    """Usage between two consecutive reads in the chain."""

    start: datetime  # UTC: local midnight after the earlier read
    end: datetime  # UTC: local midnight after the later read
    quantity: float
    provisional: bool


def has_meter_reads(up: UsagePoint) -> bool:
    """Whether any of [up]'s bills carries its meter's register."""
    return any(s.meter_reads is not None for s in up.summaries)


def meter_read_points(summaries: list[BillingSummary]) -> list[MeterReadPoint]:
    """Every read the bills state, oldest first, each with its status in the chain."""
    bills = [s.meter_reads for s in summaries if s.meter_reads is not None]
    if not bills:
        return []

    # Current reads, one per bill: the later Bill Date wins a read date two bills share, and
    # between equal bill dates an actual read beats an estimate.
    by_date: dict[date, tuple[tuple[date, bool], MeterReadPoint]] = {}
    for bill in bills:
        actual = bill.quality_of_reading == QUALITY_ACTUAL or bill.is_correction
        point = MeterReadPoint(
            read_date=bill.current_read_date,
            value=bill.current_reads[-1],
            actual=actual,
            bill_date=bill.bill_date,
            adjustment_factor=bill.adjustment_factor,
            status=STATUS_KEPT,
        )
        rank = (bill.bill_date or date.min, actual)
        held = by_date.get(point.read_date)
        if held is None or rank > held[0]:
            by_date[point.read_date] = (rank, point)

    # A previous read only fills a date no bill ends on — the start of the chain, or the far side
    # of a bill missing from the response. Nothing says whether it was actual; it is taken as
    # given, since nothing earlier is known to restate it. A correction's previous reads don't
    # chain: one is 0, the other most likely Enbridge's re-estimate of the register on the date of
    # the estimate it replaces, an estimate between two actual reads the chain would drop anyway.
    for bill in bills:
        if bill.previous_read_date in by_date or len(bill.previous_reads) != 1:
            continue
        by_date[bill.previous_read_date] = (
            (date.min, True),
            MeterReadPoint(
                read_date=bill.previous_read_date,
                value=bill.previous_reads[0],
                actual=True,
                bill_date=bill.bill_date,
                adjustment_factor=bill.adjustment_factor,
                status=STATUS_KEPT,
            ),
        )

    points = [point for _, point in sorted(by_date.values(), key=lambda p: p[1].read_date)]

    def superseded(point: MeterReadPoint) -> bool:
        return not point.actual and any(
            bill.previous_read_date < point.read_date < bill.current_read_date
            and (bill.bill_date or date.min) > (point.bill_date or date.min)
            for bill in bills
        )

    actual_dates = [p.read_date for p in points if p.actual]
    newest_actual = max(actual_dates, default=None)
    oldest_actual = min(actual_dates, default=None)
    out: list[MeterReadPoint] = []
    for point in points:
        status = STATUS_KEPT
        if not point.actual:
            if superseded(point):
                status = STATUS_SUPERSEDED
            elif newest_actual is not None and oldest_actual is not None:
                if oldest_actual < point.read_date < newest_actual:
                    status = STATUS_BRACKETED
                elif point.read_date > newest_actual:
                    status = STATUS_PROVISIONAL
        out.append(_with_status(point, status))
    return out


def _with_status(point: MeterReadPoint, status: str) -> MeterReadPoint:
    if status == point.status:
        return point
    return MeterReadPoint(
        read_date=point.read_date,
        value=point.value,
        actual=point.actual,
        bill_date=point.bill_date,
        adjustment_factor=point.adjustment_factor,
        status=status,
    )


def bill_spans(points: list[MeterReadPoint], tz: tzinfo) -> list[BillSpan]:
    """Usage between each pair of consecutive reads the chain keeps."""
    chain = [p for p in points if p.status in (STATUS_KEPT, STATUS_PROVISIONAL)]
    spans: list[BillSpan] = []
    for earlier, later in zip(chain, chain[1:], strict=False):
        quantity = round((later.value - earlier.value) * later.adjustment_factor)
        if quantity < 0:
            # A register that went backwards between kept reads: a meter exchange or rollover,
            # which no bill seen so far has shown. Nothing sound can be spread over it.
            continue
        spans.append(
            BillSpan(
                start=day_start(earlier.read_date + timedelta(days=1), tz),
                end=day_start(later.read_date + timedelta(days=1), tz),
                quantity=float(quantity),
                provisional=later.status == STATUS_PROVISIONAL,
            )
        )
    return spans


def billing_period(summary: BillingSummary, tz: tzinfo) -> tuple[datetime, datetime] | None:
    """A bill's period from its read dates — the day after the previous read through the current
    read, inclusive — or None for a bill without meter reads.

    The feed's own ``billingPeriod`` runs a day short and starts at UTC midnight.
    """
    reads = summary.meter_reads
    if reads is None:
        return None
    return (
        day_start(reads.previous_read_date + timedelta(days=1), tz),
        day_start(reads.current_read_date + timedelta(days=1), tz),
    )


def day_start(day: date, tz: tzinfo) -> datetime:
    """Local midnight starting [day], in UTC."""
    return datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)


def newest_actual_read(points: list[MeterReadPoint]) -> datetime | None:
    """The newest actual read's date, as the feed states dates (UTC midnight).

    Everything after it is provisional, so the poll window reaches back to it: the bill carrying
    this read has to be in the response that restates what follows.
    """
    newest = max((p.read_date for p in points if p.actual), default=None)
    if newest is None:
        return None
    return datetime.combine(newest, time(), tzinfo=UTC)


def newest_read(up: UsagePoint) -> datetime | None:
    """The newest read date on [up]'s bills (UTC midnight), for the poll cursor."""
    newest = max(
        (s.meter_reads.current_read_date for s in up.summaries if s.meter_reads is not None),
        default=None,
    )
    if newest is None:
        return None
    return datetime.combine(newest, time(), tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Mismatch:
    """A reading dropped for a bill's usage, or a restatement, that disagrees with the bill."""

    kind: str  # "same_response" or "restated"
    start: datetime
    end: datetime
    reading: float  # the dropped reading, or what was stored before the restatement
    bill: float

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "reading": round(self.reading, 3),
            "bill": round(self.bill, 3),
            "difference": round(self.bill - self.reading, 3),
        }


def agrees(reading: float, bill: float) -> bool:
    """Whether [reading] and [bill] describe the same usage, within the bill's rounding."""
    return math.isclose(reading, bill, abs_tol=_AGREEMENT_TOLERANCE)


def points_as_dicts(points: list[MeterReadPoint]) -> list[dict[str, object]]:
    """The chain for the diagnostics download."""
    return [
        {
            "read_date": p.read_date.isoformat(),
            "value": p.value,
            "actual": p.actual,
            "bill_date": p.bill_date.isoformat() if p.bill_date else None,
            "status": p.status,
        }
        for p in points
    ]
