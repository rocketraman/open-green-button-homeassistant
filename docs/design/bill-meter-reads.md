# Design: usage from bills' meter reads

Status: implemented ([`meter_reads.py`](../../custom_components/greenbutton/meter_reads.py)), not yet released.
Motivating issue: [open-green-button#39](https://github.com/rocketraman/open-green-button/issues/39) (Enbridge Gas).

## Problem

Enbridge Gas reads most residential meters by hand, every other month, and estimates the months in between.
Its Green Button feed carries two kinds of usage data, and the integration imports only one of them.

* **IntervalReadings** (under MeterReading → IntervalBlock): one reading per billing period, each in its own MeterReading.
  This is what the integration imports today.
* **UsageSummaries** (one per bill): the bill's period, amount and itemized lines.
  Among the itemized lines, Enbridge includes the meter's register: "Previous Meter Read", "Current Meter Read", the two read dates, and the "PEF" (pressure/atmospheric) factor.

Across four accounts in the issue, the IntervalReadings are unreliable as a usage source:

* **They stop.**
  Every account's readings end between March and June 2026, while bills continue to September.
  Periods with an *actual* meter read are missing too, so this is not only estimates waiting to be confirmed.
* **They aren't corrected.**
  When an actual read replaces an estimate, Enbridge issues a correction bill.
  The IntervalReadings are not revised to match: one account's readings say 449 m³ for a span its corrected bills put at 209 m³.
* **They can be wrong in shape.**
  One account's 34-day December bill (364 m³, one actual read) appears as a 27-day reading of 363 m³ plus a 7-day reading of 0 m³.

The bills' register lines have none of these problems.
They chain without gaps (each bill's previous read is the last bill's current read), they cover every billed month, and a correction bill restates the register at the actual read.
They were checked against six of the maintainer's paper bills: every register value, read type and amount matched.

## Facts about the bill data (verified against paper bills)

* The billing period runs from the day after the previous read to the current read date, inclusive of both.
  The feed's `billingPeriod.duration` omits the read day (a 30-day period is sent as 29 days); the read dates are authoritative.
* `qualityOfReading` 19 is printed as "Actual", 12 as "Estimated".
* Billed volume = (current read − previous read) × PEF, rounded; PEF is "Adjustment Factor (atmospheric)", e.g. 1.004.
  "Usage (unadjusted)" is billed volume ÷ PEF, not the raw register difference.
* A correction bill (seen twice, both after an estimate) spans two periods back to the start of the estimated one.
  It lists two "Current Meter Read" lines, the estimate and then the actual read, and its volume is their difference (e.g. −259 m³).
  It is itself marked `qualityOfReading` 12, even though its final read is the actual one.
  Its "Previous Meter Read" lines don't chain: one lists 0, the other a value no other bill states (52,803 against an earlier 52,801).
  That value is most likely Enbridge's re-estimate of the register on the date of the estimate being replaced, an estimate itself (see the open questions), so it is ignored.
* Each bill carries a "Bill Date" line, so bills can be ordered by when they were issued.
* A correction bill appears only when the actual read comes in *below* the estimate (53,065 estimated vs 52,806 actual; 50,220 vs 50,185), since the next bill can't charge negative usage.
  When the actual read is above the estimate, the next bill simply starts from the estimated read and absorbs the difference.
  The maintainer's account shows this: June was estimated at 63 m³ and July, read actually, came in at 21 m³; no correction bill was issued.

## Why not use the IntervalReadings always

This was the first instinct, and for smart-meter utilities it is the right answer.
The distinction that matters is not "feed vs bills": both are in the same feed, in the same response.
It is which element of the feed is the best measurement for a given hour.

* An hourly or daily IntervalReading is a measurement at a finer grain than any bill.
  A bill can only spread its total evenly across the period, so it must never replace these.
* A billing-period-long IntervalReading is no finer than the bill.
  It adds no detail, it can disappear (Enbridge since spring 2026), and it is not revised when the bill is.
  For such hours the bill's register is the better source.

So "use the IntervalReadings always" fails exactly where they are no better than the bill, and "use the bills always" would make smart-meter data weeks stale and flatten its shape.
The rule below picks per hour, by resolution, with no per-utility switch.

## Selection rule

Bill-derived usage is applied per hour after [`statistics._hourly_totals`], which already decides per hour between readings of different granularity ([`statistics._apply_bill_hours`]).

1. For each hour, take the finest-grained IntervalReading covering it.
2. If that reading is at most one day long, it wins, exactly as today.
   The bill contributes nothing to that hour.
   The threshold is a fixed day, not a fraction of the bill: one account's 34-day bill arrives as a 27-day reading of 363 m³ and a 7-day reading of 0 m³.
   A relative rule ("under half the bill") would let the 7-day reading win and zero out a week the bill says was used.
3. Otherwise (the only covering readings are billing-period-long, or there are none), the bill's register segment covering the hour wins, and those readings are dropped for that hour.
4. If no bill covers the hour, any reading covering it is used as today.

This changes how `_hourly_totals` combines readings of similar length.
Today, of the readings covering an hour, any less than twice as long as the shortest (its "peers", per `_PEER_RESOLUTION_FACTOR`) are added together, and longer ones are ignored for that hour.
Adding is right for what peers normally are: separate pieces of usage meeting in one hour, such as four 15-minute readings, or two billing periods sharing their boundary hour.
A bill segment and a billing-period IntervalReading over the same days are peers by length, but they are two measurements of the *same* gas, so adding them would count that period twice.
Bill segments are therefore tagged as their own source, which replaces IntervalReadings of the same or greater length rather than being added to them.

What each case gets:

| Utility | Hourly/daily readings | Billing-period readings | Bills with register lines | Result |
|---|---|---|---|---|
| Smart meter, no register lines | yes | no | no | Unchanged |
| Smart meter with register lines | yes | no | yes | Unchanged where readings exist; bills fill hours the readings miss |
| Enbridge | no | sparse, stale | yes | Bills everywhere |
| Billing-only, no register lines (UtilityAPI) | no | yes | no | Unchanged |

Step 3 relies on the register lines, which are Enbridge's own itemized-line notes, not part of ESPI.
They are parsed only when the server sends a new client quirk, `summaryMeterReads`, set for Enbridge in `utilities.conf`, like the existing `invertUsageIfPositiveMultiplier`.
Another utility can opt in once its notes are checked; matching note text without the quirk would make a cosmetic rename by any utility change how its usage is read.

## From bills to a register

Each bill yields register points: (read date, register value, actual/estimated, bill date).
Its previous read is a point too.

* Points on the same read date conflict when a later bill restates an earlier one.
  The point from the bill with the later Bill Date wins; between equal bill dates, actual beats estimated.
* An estimated point strictly inside the span of a later bill's previous-to-current reads is dropped.
  That span supersedes it: this is how a correction replaces the estimate it corrects, with no negative volume ever imported.
* An estimated point with an actual point on both sides of it is dropped too, whether or not a correction bill was issued.
  Usage is spread evenly from actual read to actual read, which is closer to what was used than the estimate's split.
  The maintainer's June was billed at 63 m³ (estimated) and July at 21 m³ (actual, absorbing June's over-estimate); imported, the 84 m³ is spread over the 62 days from the May read to the July read.
  A correction bill is just the case where the actual read came in below the estimate; both cases end up the same.
* Estimated points after the newest actual read are kept, as provisional usage, until the next actual read brackets them.
  They are then dropped and the span is restated (see below).
* Usage between consecutive surviving points = register difference × PEF (the PEF of the bill whose current read ends the span).
* Each span is spread evenly over its hours, from local midnight after the earlier read to local midnight after the later one (Home Assistant's configured time zone; Enbridge's LocalTimeParameters sends `tzOffset` -18, which is unusable).
  Spreading per hour rather than per day gives a daylight-saving day its 23 or 25 hours' share.

Readings are dates without a time of day, so a read is taken to happen at the end of its day.
Either convention is wrong for the day of the read; this one matches the inclusive period printed on the bill.

Because usage is spread actual to actual, a statistic month no longer matches each bill's printed volume when that bill was estimated.
The usage over each pair of actual reads still matches the bills' total exactly.
Cost is unaffected in total: each bill's amount is still distributed over the usage recorded within its own billing period ([`statistics._import_cost_summaries`]), so every bill's dollars land in its own period.
The period comes from the read dates, since the feed's `billingPeriod` runs a day short.
The price per m³ within an estimated period shifts accordingly (June's $50.98 over less gas, July's $43.27 over more).

A correction bill is the exception.
Its amount is only the difference between what the bills it corrects charged and what the actual read says, often a credit (−$46.35 on one account).
Added on top of the corrected bill, that credit lands on the days past it and priced one month below zero (−$6.88).
So a correction is costed together with the bills it corrects: their amounts plus its own ($113.16 − $46.35), spread over its whole span.

## Restating months already imported

A correction, or an actual read that brackets earlier estimates, changes usage up to about two months back.
With actual-to-actual spreading this happens on every actual read that follows an estimate, which for Enbridge is roughly every other bill.
Home Assistant statistics store a running sum, so changing one month changes every row after it.

* The importer computes the earliest hour whose usage differs from what is stored, D.
* It reads the stored sum just before D, recomputes rows from D to the newest data, and imports them.
  Home Assistant upserts statistics on (statistic id, start), so this overwrites the rows in place.
* The cost statistic is re-imported from D the same way, distributing each bill over the corrected usage.

The importer otherwise never lets a spread (non-measured) hour overwrite a stored one ([`statistics._restated_rows`]).
Bill segments are the exception, on a meter that has never published a reading of a day or shorter.
Such a meter's stored hours can only have come from bills or billing-period readings, so a bill may restate any of them.
A meter that has ever had finer readings is remembered as such (`fine_reading_usage_points` in the entry's data, since an incremental poll only shows its own window); there, bills only fill hours nothing is stored for.

The same path covers a bill that arrives after a billing-period reading was imported on its own (step 4 of the selection rule).
The later bill takes over those hours exactly as if the two had arrived together, and the stored rows from D onwards are restated.

Cost is restated from the start of the bill period containing D, not from D, so a bill whose period straddles D is spread over its corrected usage as a whole.

## Fetching late corrections

The importer needs every bill from D onwards in one response.
Enbridge filters by the dates of the data, not by publication: every entry's `published` is the request time, yet a narrow window returns only recent records.
Three ordinary polls in the issue's diagnostics settle it, each with `published-min` one day before its meter's newest reading:

* Steven's, from 2026-06-15, returned 5 bills and 1 reading; a full rebuild the same day returned 11 bills and 7 readings.
  The 5 are exactly the bills whose period ends on or after 2026-06-15, including one ending on that day; the correction bill (period March 14 to May 13) was left out.
* astro's, from 2026-03-25, returned the 8 bills from the one ending that day onwards, and a 29-day reading that started on 2026-02-25 and runs past the window's start.
* Nadim's, from 2026-03-24, returned the 8 bills from the one ending that day onwards.

A record is returned when its period reaches the window, even when it ends exactly at `published-min`.
These polls can't tell whether bills are matched on their period or on their Bill Date, which always falls just after the period; both work here, since the bill carrying the anchor read ends after `published-min` and is issued after it.

* The per-meter cursor ([`coordinator._advance_cursor`]) advances to the newest bill read date, not just the newest IntervalReading.
  Today an Enbridge cursor is stuck on the last IntervalReading, which happens to keep the window wide but for the wrong reason.
* `published-min` reaches back to the meter's newest actual read (minus the existing one-day overlap), kept per meter as `meter_read_anchors` in the entry's data.
  Everything after that read is provisional: an estimate, or a billing-period reading no bill covers yet.
  The restated span runs from that actual read, so the response has to include the bill that carries it.
  For Enbridge that is at most about two months.
  A meter whose bills carry no meter reads (every smart meter) has no anchor and is unaffected.

## Migration

Turning on the `summaryMeterReads` quirk changes Enbridge's quirk set, which already rebuilds every entry with stored statistics.
Import-logic revision 8 covers the entry that recorded the new quirk set on an integration version that ignored it, and so never rebuilt: its predicate is a poll carrying bills with meter reads under the quirk.
That matches Enbridge entries and nothing else.
Affected entries rebuild once, which re-imports two years from the bills.

## Diagnostics

A reading dropped in favour of a bill is dropped silently in the statistics, so the diagnostics download records it.
For each UsagePoint, `bill_reading_mismatches` (in the entry's data) has a row per disagreement:

* `kind`: `same_response` for a reading dropped in favour of a bill in the same poll, or `restated` for stored usage a bill's meter reads restated in a later poll;
* `start` and `end`: the dropped reading's span, or the restated stretch (from D to the last stored hour);
* `reading`: the dropped reading's value over the hours it lost, or what was stored over the restated stretch;
* `bill`: the bill-derived usage over the same hours, and `difference`.

Readings that agree (within 1 m³, the bills' rounding) are only listed by start under `agreeing`, so the rows show only real disagreements.

The diagnostics download otherwise describes only the latest poll, and a restatement happens once, in whichever poll brings the bill.
So the list is persisted in the entry's data, like `recent_polls`, keeping the newest 50 rows per UsagePoint; a rebuild clears it, and the rebuild's own import repopulates it from the full history (all `same_response` there).
This tells us how often a utility's readings are wrong and in which direction, and would catch a utility whose readings are right where its register lines are not.

The diagnostics also show the register chain itself, as `meter_read_chain` on each usage point of the last response: each read's date, value, actual/estimated, and whether it was kept, superseded, bracketed or is provisional.

## Tests

In [`tests/test_meter_reads.py`](../../tests/test_meter_reads.py) and [`tests/test_coordinator.py`](../../tests/test_coordinator.py):

* The chain: a bracketed estimate, a provisional one, a correction superseding its estimate, and the later Bill Date winning a shared read date.
* Parsing the meter-read lines, only under the quirk; the revision 8 predicate.
* Actual-to-actual spreading: the June estimate is dropped and 84 m³ spreads over 62 days; each bill's cost lands in its own period.
* A provisional estimate imported, then restated with its cost when the actual read arrives in a later poll, recorded as a `restated` mismatch.
* The split December bill: neither reading wins, the bill's 364 m³ spreads evenly over all 34 days, and both readings are reported.
* A reading that agrees with the bill is counted, not listed.
* Daily readings keep their hours; the bill fills only the days they miss.
* Bills import on a poll that carries no readings at all.
* A correction bill is costed with the bill it corrects, and no hour goes below zero.
* The cursor follows the newest bill and the window reaches back to the newest actual read; mismatches persist across polls and a rebuild starts them over; a meter with daily readings is remembered as fine.

All four accounts' feeds from the issue were also imported end to end through a real recorder.
On each, the cost statistic totals the bills to the cent.
Usage matches the meter reads, plus the parts of IntervalReadings that run outside any bill's span (0 to 2.6 m³), which no meter read covers.

## Open questions

* Whether `qualityOfReading` 12 ever means anything other than estimated in this feed (all six paper bills agree; corrections were not on paper).
* What a correction's second "Previous Meter Read" is.
  Both corrections list 0 and then a register value that sits between the two actual reads the correction spans, on the date of the estimate it replaces.
  Steven's is 52,803 between 52,801 (March 13) and 52,806 (May 13), with the replaced estimate on April 15; spreading by days puts 52,803.7 there.
  astro's is 50,097 between 49,978 (February 24) and 50,185 (April 23), with the replaced estimate on March 25: 57% of the 207 m³ in the first half, where spreading by days gives 50%, as a colder March would.
  So it is most likely Enbridge's re-estimate of the register at that date, split by weather.
  It is an estimate with an actual read on both sides, which the chain drops anyway, so ignoring it changes nothing; only a correction bill on paper could confirm what it is.
