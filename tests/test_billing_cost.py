"""Tests for BillingSummary.total_cost — the per-period charge derivation.

Regression coverage for the Burlington Hydro feed, whose UsageSummary detail list carries
running-balance bookkeeping ("Balance Forward" / "Payments Received") and subtotal lines
("New Charges This Period" / "Total Amount Due") alongside the real charges. Two failure modes
seen in real data, both fixed by summing only the itemized charge lines:

  - Naively summing *every* detail triple-counts the bill (charges + New-Charges subtotal +
    Total-Amount-Due subtotal) — the first ~3× inflation we saw.
  - On every other bill the "New Charges This Period" / "Total Amount Due" subtotals, and now
    billLastPeriod too, include two months of Halton Region water with no line item for it, so
    trusting them is also wrong — the second inflation. Only the itemized lines are the
    electricity.
"""

from __future__ import annotations

from datetime import UTC, datetime

from defusedxml.ElementTree import fromstring as _xml_fromstring

from custom_components.greenbutton import espi
from custom_components.greenbutton.api import BillingSummary, CostDetail, UsagePoint, UsageResponse
from custom_components.greenbutton.statistics import response_needs_import_migration


def _detail(note: str, dollars: float) -> CostDetail:
    return CostDetail(
        amount_raw=round(dollars * 100_000),
        note=note,
        item_kind=None,
        unit_cost_raw=0,
    )


def _summary(details: list[CostDetail], bill_last_period_raw: int = 0) -> BillingSummary:
    return BillingSummary(
        billing_period_start=datetime(2026, 1, 1, 5, tzinfo=UTC),
        billing_period_duration_seconds=31 * 86400,
        bill_last_period_raw=bill_last_period_raw,
        cost_additional_last_period_raw=0,
        cost_details=details,
        currency_numeric_code=124,
    )


# A "clean" Burlington Hydro month, where the subtotal happens to match: the itemized charges
# sum to $163.40 (the H.S.T. is 13% of the charges before the rebate), the carried balance
# cancels against an equal payment, and both subtotal lines also read $163.40. Summing every
# line yields $490.20 (3×). Structure from a real feed; amounts are synthetic.
_BURLINGTON_CLEAN_MONTH = [
    _detail("Off Peak-Charge", 60.00),
    _detail("Mid Peak-Charge", 25.00),
    _detail("On Peak-Charge", 30.00),
    _detail("Delivery Charge", 60.00),
    _detail("Regulatory charge", 5.00),
    _detail("H.S.T.", 23.40),
    _detail("Ontario Electricity Rebate", -40.00),
    _detail("Balance Forward", 200.00),
    _detail("Payments Received", -200.00),
    _detail("New Charges This Period", 163.40),
    _detail("Total Amount Due", 163.40),
]

# A water month: the itemized electricity charges sum to $167.05 and the H.S.T. is 13% of them,
# but the "New Charges This Period" / "Total Amount Due" subtotals also include two months of
# Halton Region water ($300.00 here), with no line item. total_cost must resolve to the itemized
# $167.05. Structure from a real feed; amounts are synthetic.
_BURLINGTON_WATER_MONTH = [
    _detail("Off Peak-Charge", 65.00),
    _detail("Mid Peak-Charge", 20.00),
    _detail("On Peak-Charge", 35.00),
    _detail("Delivery Charge", 60.00),
    _detail("Regulatory charge", 5.00),
    _detail("H.S.T.", 24.05),
    _detail("Ontario Electricity Rebate", -42.00),
    _detail("Balance Forward", 163.40),
    _detail("Payments Received", -163.40),
    _detail("New Charges This Period", 467.05),
    _detail("Total Amount Due", 467.05),
]


def test_total_cost_sums_itemized_charges_not_the_triple_counted_sum() -> None:
    """A clean Burlington bill resolves to its itemized period charges, not the ~3× sum."""
    summary = _summary(_BURLINGTON_CLEAN_MONTH)
    assert round(summary.total_cost, 2) == 163.40
    # And crucially NOT the naive sum-of-everything the very first version produced.
    assert round(sum(d.amount for d in summary.cost_details), 2) == 490.20


def test_total_cost_ignores_corrupt_new_charges_subtotal() -> None:
    """When the feed's subtotal is inflated, total_cost trusts the itemized lines instead.

    This is the second inflation: "New Charges This Period" includes the water but the real,
    itemized period charges don't. Trusting the subtotal (the earlier fix) inflated the
    dashboard's daily cost several times over.
    """
    summary = _summary(_BURLINGTON_WATER_MONTH)
    assert round(summary.total_cost, 2) == 167.05
    assert summary.total_cost != 467.05


def test_itemized_charges_win_over_a_whole_bill_total() -> None:
    """Burlington now sends a billLastPeriod on a water month: the whole bill, water and
    all. The electricity is the itemized charges, and the bill is flagged for revision 9."""
    summary = _summary(_BURLINGTON_WATER_MONTH, bill_last_period_raw=round(467.05 * 100_000))
    assert round(summary.total_cost, 2) == 167.05
    assert summary.total_disagrees_with_itemization
    clean = _summary(_BURLINGTON_CLEAN_MONTH, bill_last_period_raw=round(163.40 * 100_000))
    assert not clean.total_disagrees_with_itemization


def test_a_bill_without_itemized_charges_falls_back_to_bill_last_period() -> None:
    summary = _summary([_detail("Total Amount Due", 95.00)], bill_last_period_raw=9500000)
    assert summary.total_cost == 95.00
    assert not summary.total_disagrees_with_itemization


def test_total_cost_excludes_uncancelled_running_balance() -> None:
    """Balance Forward / Payments Received / subtotals never leak into the period cost.

    Even when a carried balance does NOT cancel (a partial payment), only the itemized
    charges count — the running balance is prior-invoice money, not this period's usage.
    """
    details = [
        _detail("Off Peak-Charge", 60.00),
        _detail("Delivery Charge", 60.00),
        _detail("H.S.T.", 15.60),
        _detail("Balance Forward", 300.00),
        _detail("Payments Received", -100.00),  # deliberately does NOT cancel
        _detail("New Charges This Period", 135.60),
        _detail("Total Amount Due", 335.60),
    ]
    summary = _summary(details)
    assert round(summary.total_cost, 2) == 60.00 + 60.00 + 15.60


def test_is_period_charge_classifies_line_items() -> None:
    """Charges are summable; balance bookkeeping and subtotals are not."""
    assert _detail("Off Peak-Charge", 1).is_period_charge
    assert _detail("Delivery Charge", 1).is_period_charge
    assert _detail("Ontario Electricity Rebate", -1).is_period_charge
    assert not _detail("Balance Forward", 1).is_period_charge
    assert not _detail("Payments Received", -1).is_period_charge
    assert not _detail("New Charges This Period", 1).is_period_charge
    assert not _detail("Total Amount Due", 1).is_period_charge
    # Elexicon's grand-total subtotal is worded "Amount Due" (Burlington's is "Total Amount
    # Due"); both must be excluded or the itemized sum double-counts the bill.
    assert not _detail("Amount Due", 1).is_period_charge
    # Matching is case- and whitespace-insensitive.
    assert not _detail("  TOTAL  AMOUNT   DUE ", 1).is_period_charge


# --- Elexicon / savagedata per-line-scaled encoding -------------------------------------------
#
# Unlike Burlington's bare 1/100,000 amounts, savagedata (Elexicon) scales each charge line by
# its own <measurement><powerOfTenMultiplier> and reports billLastPeriod in a 1/1,000 scale.
# This is the real first-summary of a user's Elexicon feed. The bug this guards: the client
# ignored the per-line multiplier and divided billLastPeriod by 100,000, yielding $1.72 for the
# whole month (~$0.05/day on the Energy dashboard) instead of the true $172.03.
_ELEXICON_USAGE_SUMMARY_XML = """
<UsageSummary xmlns="http://naesb.org/espi">
  <billingPeriod>
    <duration>2592000</duration>
    <start>1778934402</start>
  </billingPeriod>
  <billLastPeriod>172030</billLastPeriod>
  <costAdditionalLastPeriod>172030</costAdditionalLastPeriod>
  <!-- Zero-amount meter-reading metadata: carries a multiplier but must NOT flip the feed
       into per-line-scaled detection (that hinges on a non-zero amount). -->
  <costAdditionalDetailLastPeriod>
    <note>Current Meter Read</note>
    <measurement><powerOfTenMultiplier>-2</powerOfTenMultiplier><uom>72</uom><value>17579193</value></measurement>
    <itemKind>10</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>17203</amount><note>Amount Due</note>
    <measurement><powerOfTenMultiplier>-2</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>10</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>21432</amount><note>Balance Forward</note>
    <measurement><powerOfTenMultiplier>-2</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>4</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>45950000</amount><note>DELIVERY CHARGE</note>
    <measurement><powerOfTenMultiplier>-6</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>2</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>2499</amount><note>HST</note>
    <measurement><powerOfTenMultiplier>-2</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>5</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>20150</amount><note>MID-PEAK</note>
    <measurement><powerOfTenMultiplier>-3</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>3</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>58980</amount><note>OFF-PEAK</note>
    <measurement><powerOfTenMultiplier>-3</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>3</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>39520</amount><note>ON-PEAK</note>
    <measurement><powerOfTenMultiplier>-3</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>3</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>-4517</amount><note>ONTARIO ELECTRICITY REBATE</note>
    <measurement><powerOfTenMultiplier>-2</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>8</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>830000</amount><note>REGULATORY CHARGE</note>
    <measurement><powerOfTenMultiplier>-6</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>4</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>9570000</amount><note>TRANSMISSION CONNECTION CHARGE</note>
    <measurement><powerOfTenMultiplier>-6</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>2</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>12670000</amount><note>TRANSMISSION NETWORK CHARGE</note>
    <measurement><powerOfTenMultiplier>-6</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>2</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>4540000</amount><note>WHOLESALE MARKET SERVICE CHARGE</note>
    <measurement><powerOfTenMultiplier>-6</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>4</itemKind>
  </costAdditionalDetailLastPeriod>
  <currency>124</currency>
</UsageSummary>
"""


def _parse_elexicon_summary() -> BillingSummary:
    summary = espi._parse_usage_summary(_xml_fromstring(_ELEXICON_USAGE_SUMMARY_XML))
    assert summary is not None
    return summary


def test_elexicon_per_line_multiplier_scales_each_amount() -> None:
    """Each charge amount is scaled by its own powerOfTenMultiplier, not a flat 1/100,000."""
    by_note = {d.note: d for d in _parse_elexicon_summary().cost_details}
    assert round(by_note["DELIVERY CHARGE"].amount, 2) == 45.95  # 45950000 × 10^-6
    assert round(by_note["HST"].amount, 2) == 24.99  # 2499 × 10^-2
    assert round(by_note["OFF-PEAK"].amount, 2) == 58.98  # 58980 × 10^-3
    assert round(by_note["ONTARIO ELECTRICITY REBATE"].amount, 2) == -45.17


def test_elexicon_total_cost_uses_itemized_charges_not_misscaled_bill_last_period() -> None:
    """total_cost is the reconciled itemized bill ($172.03), not billLastPeriod/100,000 ($1.72).

    Reproduces the user-reported "cost shows ~$0.05/day" symptom: dividing Elexicon's
    billLastPeriod (172030, a 1/1,000 value) by 100,000 gives $1.72 for the month.
    """
    summary = _parse_elexicon_summary()
    assert round(summary.total_cost, 2) == 172.03
    # The bug's wrong answer — must not resurface.
    assert round(summary.bill_last_period_raw / 100_000, 2) == 1.72
    assert round(summary.total_cost, 2) != 1.72


def test_elexicon_excludes_amount_due_and_balance_forward_subtotals() -> None:
    """The "Amount Due" grand total and "Balance Forward" carry-over are not period charges."""
    summary = _parse_elexicon_summary()
    charged_notes = {d.note for d in summary.cost_details if d.is_period_charge}
    assert "Amount Due" not in charged_notes
    assert "Balance Forward" not in charged_notes
    # If "Amount Due" ($172.03) leaked in, the total would double to ~$344.
    assert round(summary.total_cost, 2) == 172.03


# --- Enbridge Gas ----------------------------------------------------------------------------
#
# Every cost-line powerOfTenMultiplier has the wrong sign (the server's
# `invertCostIfPositiveMultiplier` quirk), and
# the itemized lines don't reconcile: they include $/m³ rates and the supply charge twice, under
# two names. billLastPeriod agrees with the "Amount Due" line, which settles the bill's scale.
# Structure from a real Enbridge feed; amounts are synthetic.
_ENBRIDGE_USAGE_SUMMARY_XML = """
<UsageSummary xmlns="http://naesb.org/espi">
  <billingPeriod><duration>2592000</duration><start>1788048000</start></billingPeriod>
  <billLastPeriod>41250</billLastPeriod>
  <billToDate>41250</billToDate>
  <costAdditionalLastPeriod>0</costAdditionalLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>41250</amount><note>Amount Due</note>
    <measurement><powerOfTenMultiplier>3</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>10</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>4750</amount><note>HST ($)</note>
    <measurement><powerOfTenMultiplier>3</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>5</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>6000</amount><note>Gas Supply Charge</note>
    <measurement><powerOfTenMultiplier>3</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>2</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>6000</amount><note>Gas Service Provider Supply Charge ($)</note>
    <measurement><powerOfTenMultiplier>3</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>2</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>30500</amount><note>Customer charge</note>
    <measurement><powerOfTenMultiplier>3</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>10</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>173871</amount><note>Total Gas Supply Rate $/m3 (net)</note>
    <measurement><powerOfTenMultiplier>3</powerOfTenMultiplier><uom>80</uom></measurement>
    <itemKind>10</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <note>Adjustment Factor (atmospheric)</note>
    <measurement><powerOfTenMultiplier>6</powerOfTenMultiplier><uom>114</uom><value>1004000</value></measurement>
    <itemKind>10</itemKind>
  </costAdditionalDetailLastPeriod>
  <currency>124</currency>
</UsageSummary>
"""


def _parse_enbridge_summary(*, bill: int = 41250, amount_due: int = 41250) -> BillingSummary:
    xml = _ENBRIDGE_USAGE_SUMMARY_XML.replace(
        "<billLastPeriod>41250</billLastPeriod>", f"<billLastPeriod>{bill}</billLastPeriod>"
    ).replace("<amount>41250</amount>", f"<amount>{amount_due}</amount>")
    summary = espi._parse_usage_summary(_xml_fromstring(xml), invert_cost=True)
    assert summary is not None
    return summary


def test_enbridge_inverted_multipliers_scale_amounts_down() -> None:
    """Under the quirk a line at 3 is ÷1,000: 4750 is $4.75, not $4,750,000."""
    by_note = {d.note: d for d in _parse_enbridge_summary().cost_details}
    assert round(by_note["HST ($)"].amount, 2) == 4.75
    assert by_note["HST ($)"].amount_power_of_ten == -3


def test_enbridge_total_is_the_bill_that_matches_its_amount_due() -> None:
    """$41.25 — not the itemized sum, which counts supply twice and adds a $173.87/m³ rate."""
    assert round(_parse_enbridge_summary().total_cost, 2) == 41.25


def test_enbridge_zero_bill_is_zero() -> None:
    """A $0 bill matches its $0 Amount Due, so the unreconcilable itemization is never summed."""
    assert _parse_enbridge_summary(bill=0, amount_due=0).total_cost == 0


def test_a_bill_that_disagrees_with_its_amount_due_falls_back_to_the_itemization() -> None:
    """The match is the evidence; without it the per-line-scaled rule applies as before."""
    summary = _parse_enbridge_summary(bill=99999)
    assert round(summary.total_cost, 2) != 100.0
    assert summary.total_cost != 41.25


def test_elexicon_bill_does_not_match_its_amount_due() -> None:
    """savagedata's billLastPeriod (1/1,000) never equals its Amount Due raw (at 10^-2)."""
    summary = _parse_elexicon_summary()
    amount_due = next(d for d in summary.cost_details if d.note == "Amount Due")
    assert amount_due.amount_raw != summary.bill_last_period_raw
    assert round(summary.total_cost, 2) == 172.03


def test_without_the_cost_quirk_enbridge_amounts_follow_the_spec() -> None:
    """The usage quirk alone doesn't touch cost lines: 3 stays ×1,000."""
    summary = espi._parse_usage_summary(_xml_fromstring(_ENBRIDGE_USAGE_SUMMARY_XML))
    assert summary is not None
    hst = next(d for d in summary.cost_details if d.note == "HST ($)")
    assert hst.amount_power_of_ten == 3


# --- UtilityAPI (El Paso Electric) quantity measurements --------------------------------------
#
# UtilityAPI documents every <amount> as bare 1/100,000 and uses <measurement> for the line's
# billed quantity (here kWh: uom 72 at 10^3). Reading that multiplier as the amount's scale
# flipped the bill into the per-line-scaled family and priced El Paso Electric's daily usage in
# the hundreds of thousands of dollars (open-green-button issues/99).
_UTILITYAPI_USAGE_SUMMARY_XML = """
<UsageSummary xmlns="http://naesb.org/espi">
  <billingPeriod>
    <duration>2592000</duration>
    <start>1778934402</start>
  </billingPeriod>
  <billLastPeriod>5432100</billLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>4120000</amount><note>Energy Charge</note>
    <measurement><powerOfTenMultiplier>3</powerOfTenMultiplier><uom>72</uom><value>412</value></measurement>
    <itemKind>1</itemKind>
  </costAdditionalDetailLastPeriod>
  <costAdditionalDetailLastPeriod>
    <amount>1312100</amount><note>Customer Charge</note>
    <itemKind>1</itemKind>
  </costAdditionalDetailLastPeriod>
  <currency>840</currency>
</UsageSummary>
"""


def test_a_quantity_measurement_does_not_scale_the_amount() -> None:
    """A kWh measurement describes what was billed, not the amount's scale: still 1/100,000."""
    summary = espi._parse_usage_summary(_xml_fromstring(_UTILITYAPI_USAGE_SUMMARY_XML))
    assert summary is not None
    energy = next(d for d in summary.cost_details if d.note == "Energy Charge")
    assert energy.amount_power_of_ten is None
    assert round(energy.amount, 2) == 41.20
    assert round(summary.total_cost, 2) == 54.32


def test_a_bill_total_beyond_its_itemization_is_the_revision_9_signal() -> None:
    """A Burlington water month shows the old rule priced it wrong; a clean month can't tell."""

    def response(*bills: BillingSummary) -> UsageResponse:
        up = UsagePoint(
            usage_point_id="up", service_kind="ELECTRICITY", series=[], summaries=list(bills)
        )
        return UsageResponse(updated=None, usage_points=[up], new_credentials=None)

    water = _summary(_BURLINGTON_WATER_MONTH, bill_last_period_raw=round(467.05 * 100_000))
    clean = _summary(_BURLINGTON_CLEAN_MONTH, bill_last_period_raw=round(163.40 * 100_000))
    assert response_needs_import_migration(response(clean, water), 8) is True
    assert response_needs_import_migration(response(clean), 8) is None
    assert response_needs_import_migration(response(), 8) is None
    # Burlington's older bills stated billLastPeriod = 0: the old rule priced them the same.
    assert response_needs_import_migration(response(_summary(_BURLINGTON_WATER_MONTH)), 8) is False
