"""Parser regression tests for custodian-specific ESPI feed shapes."""

from datetime import UTC, datetime

from custom_components.greenbutton.espi import (
    _accumulation,
    _flow_direction,
    parse_customer_feed,
    parse_usage_feed,
)


def test_flow_direction_codes_match_espi_xsd() -> None:
    """NAESB ESPI FlowDirectionKind (espi.xsd): 1=forward, 4=net, 19=reverse, 20=total.

    Guards against the earlier bug where 4/net and 20/total were swapped (net showed as "Total").
    """
    assert _flow_direction(1) == "FORWARD"
    assert _flow_direction(4) == "NET"
    assert _flow_direction(19) == "REVERSE"
    assert _flow_direction(20) == "TOTAL"


def test_accumulation_codes_match_espi_xsd() -> None:
    """NAESB ESPI AccumulationKind, in full.

    Completeness is load-bearing, not cosmetic: [statistics] decides by *name* whether a series
    is per-interval consumption or a cumulative meter register, so a cumulative code falling
    through to "OTHER" would be summed into the consumption statistic as if it were a delta
    (issue #6). continuousCumulative (2) is the one that did. Note 11=instantaneous and
    12=latchingQuantity — this map previously had 12 as INSTANTANEOUS.
    """
    assert _accumulation(0) == "NONE"
    assert _accumulation(1) == "BULK_QUANTITY"
    assert _accumulation(2) == "CONTINUOUS_CUMULATIVE"
    assert _accumulation(3) == "CUMULATIVE"
    assert _accumulation(4) == "DELTA_DATA"
    assert _accumulation(6) == "INDICATING"
    assert _accumulation(9) == "SUMMATION"
    assert _accumulation(10) == "TIME_OF_USE"
    assert _accumulation(11) == "INSTANTANEOUS"
    assert _accumulation(12) == "LATCHING_QUANTITY"
    assert _accumulation(13) == "BOUNDED_QUANTITY"
    # Unknown / absent stays "OTHER" — the statistics importer treats that as importable.
    assert _accumulation(None) == "OTHER"
    assert _accumulation(99) == "OTHER"


# Milton-shaped feed: ONE UsagePoint carrying two FORWARD MeterReadings — hourly deltas
# (accumulationBehaviour=4) and a daily cumulative register snapshot (accumulationBehaviour=1,
# with a <cost>0</cost> placeholder). Synthetic; no customer data. This is the shape issues #6
# and #7 asked for a fixture of: both series normalize to the same statistic id downstream, so
# the register's meter-lifetime total was being added to hourly consumption.
#
# 1751691600 = 2025-07-05T05:00:00Z, 1751695200 = 06:00:00Z, 1751673600 = 2025-07-05T00:00:00Z.
_MIXED_ACCUMULATION_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:f</id>
  <updated>2026-07-06T00:00:00Z</updated>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://mh/UsagePoint/UP1"/>
    <content><espi:UsagePoint><espi:ServiceCategory><espi:kind>0</espi:kind></espi:ServiceCategory></espi:UsagePoint></content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://mh/UsagePoint/UP1/MeterReading/MR_HOURLY"/>
    <link rel="related" type="espi-entry/ReadingType" href="https://mh/ReadingType/RT_DELTA"/>
    <content><espi:MeterReading/></content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="up" type="espi-feed/IntervalBlock" href="https://mh/UsagePoint/UP1/MeterReading/MR_HOURLY/IntervalBlock"/>
    <link rel="self" href="https://mh/UsagePoint/UP1/MeterReading/MR_HOURLY/IntervalBlock/IB1"/>
    <content>
      <espi:IntervalBlock>
        <espi:IntervalReading>
          <espi:timePeriod><espi:duration>3600</espi:duration><espi:start>1751691600</espi:start></espi:timePeriod>
          <espi:value>1000</espi:value>
        </espi:IntervalReading>
        <espi:IntervalReading>
          <espi:timePeriod><espi:duration>3600</espi:duration><espi:start>1751695200</espi:start></espi:timePeriod>
          <espi:value>1500</espi:value>
        </espi:IntervalReading>
      </espi:IntervalBlock>
    </content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://mh/UsagePoint/UP1/MeterReading/MR_REGISTER"/>
    <link rel="related" type="espi-entry/ReadingType" href="https://mh/ReadingType/RT_BULK"/>
    <content><espi:MeterReading/></content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="up" type="espi-feed/IntervalBlock" href="https://mh/UsagePoint/UP1/MeterReading/MR_REGISTER/IntervalBlock"/>
    <link rel="self" href="https://mh/UsagePoint/UP1/MeterReading/MR_REGISTER/IntervalBlock/IB2"/>
    <content>
      <espi:IntervalBlock>
        <espi:IntervalReading>
          <espi:cost>0</espi:cost>
          <espi:timePeriod><espi:duration>86400</espi:duration><espi:start>1751673600</espi:start></espi:timePeriod>
          <espi:value>9876543</espi:value>
        </espi:IntervalReading>
      </espi:IntervalBlock>
    </content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://mh/ReadingType/RT_DELTA"/>
    <content><espi:ReadingType><espi:accumulationBehaviour>4</espi:accumulationBehaviour><espi:commodity>1</espi:commodity><espi:currency>124</espi:currency><espi:flowDirection>1</espi:flowDirection><espi:intervalLength>3600</espi:intervalLength><espi:powerOfTenMultiplier>0</espi:powerOfTenMultiplier><espi:uom>72</espi:uom></espi:ReadingType></content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://mh/ReadingType/RT_BULK"/>
    <content><espi:ReadingType><espi:accumulationBehaviour>1</espi:accumulationBehaviour><espi:commodity>1</espi:commodity><espi:currency>124</espi:currency><espi:flowDirection>1</espi:flowDirection><espi:intervalLength>86400</espi:intervalLength><espi:powerOfTenMultiplier>0</espi:powerOfTenMultiplier><espi:uom>72</espi:uom></espi:ReadingType></content>
  </entry>
</feed>"""


def test_mixed_delta_and_bulk_series_parse_as_distinct_series() -> None:
    """One UsagePoint, two FORWARD MeterReadings — the delta and register series stay separate.

    The parser has to preserve each MeterReading's own accumulationBehaviour, because that's the
    only thing distinguishing hourly consumption from a meter-lifetime register total once both
    are FORWARD on the same UsagePoint (issues #6, #7).
    """
    _updated, usage_points = parse_usage_feed(_MIXED_ACCUMULATION_FEED)
    assert len(usage_points) == 1
    by_behaviour = {s.reading_type.accumulation_behaviour: s for s in usage_points[0].series}
    assert set(by_behaviour) == {"DELTA_DATA", "BULK_QUANTITY"}

    delta = by_behaviour["DELTA_DATA"]
    assert [r.value for r in delta.readings] == [1000.0, 1500.0]
    assert delta.readings[0].start == datetime(2025, 7, 5, 5, tzinfo=UTC)
    assert [r.cost for r in delta.readings] == [None, None]  # Milton itemizes no hourly cost

    register = by_behaviour["BULK_QUANTITY"]
    assert [r.value for r in register.readings] == [9_876_543.0]  # meter-lifetime total
    assert register.readings[0].cost == 0.0  # the placeholder that used to hijack cost selection


# savagedata-style feed: resources are nested in the URL path
# (.../UsagePoint/{up}/MeterReading/{mr}/IntervalBlock/{ib}) and the MeterReading has NO flat
# rel="related" espi-entry/UsagePoint link — only its hierarchical self URL. The parser must derive
# the parent UsagePoint from that path, or interval readings never attach (the "0 readings" bug).
_SAVAGEDATA_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:f</id>
  <updated>2026-07-08T00:00:00Z</updated>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://sd/Subscription/s/UsagePoint/UP1"/>
    <content><espi:UsagePoint><espi:ServiceCategory><espi:kind>0</espi:kind></espi:ServiceCategory></espi:UsagePoint></content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://sd/Subscription/s/UsagePoint/UP1/MeterReading/MR1"/>
    <link rel="related" type="espi-entry/ReadingType" href="https://sd/ReadingType/RT1"/>
    <content><espi:MeterReading/></content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="up" type="espi-feed/IntervalBlock" href="https://sd/Subscription/s/UsagePoint/UP1/MeterReading/MR1/IntervalBlock"/>
    <link rel="self" href="https://sd/Subscription/s/UsagePoint/UP1/MeterReading/MR1/IntervalBlock/IB1"/>
    <content>
      <espi:IntervalBlock>
        <espi:IntervalReading>
          <espi:cost>8700</espi:cost>
          <espi:timePeriod><espi:duration>3600</espi:duration><espi:start>1720432800</espi:start></espi:timePeriod>
          <espi:value>795000</espi:value>
          <espi:tou>3</espi:tou>
        </espi:IntervalReading>
        <espi:IntervalReading>
          <espi:timePeriod><espi:duration>3600</espi:duration><espi:start>1720436400</espi:start></espi:timePeriod>
          <espi:value>5271000</espi:value>
        </espi:IntervalReading>
      </espi:IntervalBlock>
    </content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://sd/ReadingType/RT1"/>
    <content><espi:ReadingType><espi:powerOfTenMultiplier>0</espi:powerOfTenMultiplier><espi:uom>72</espi:uom><espi:commodity>1</espi:commodity><espi:flowDirection>1</espi:flowDirection></espi:ReadingType></content>
  </entry>
</feed>"""


def test_hierarchical_urls_without_related_usagepoint_link_still_attach_readings() -> None:
    """savagedata omits the flat rel=related UsagePoint link on MeterReadings; deriving the
    UsagePoint from the hierarchical self URL is what keeps interval readings attached."""
    _updated, usage_points = parse_usage_feed(_SAVAGEDATA_FEED)
    assert len(usage_points) == 1
    readings = [r for series in usage_points[0].series for r in series.readings]
    assert len(readings) == 2, f"expected 2 readings, got {len(readings)}"
    assert [r.value for r in readings] == [795000.0, 5271000.0]


def test_per_interval_cost_parsed_when_present_else_none() -> None:
    """<cost> on an IntervalReading → UsageReading.cost (ESPI 1/100,000 → currency units);
    absent → None (so utilities without per-interval cost, e.g. Burlington, keep cost=None)."""
    _updated, usage_points = parse_usage_feed(_SAVAGEDATA_FEED)
    readings = usage_points[0].series[0].readings
    assert readings[0].cost == 0.087  # <cost>8700</cost>
    assert readings[1].cost is None  # second reading has no <cost>


# Burlington-shaped feed: a feed-level <updated> AND an <updated> on every entry, which is what
# real custodians emit (the synthetic fixtures above carry only the feed-level one). Burlington's
# first entry is its UsagePoint, stamped 2004-06-18 — a resource-creation date that has nothing to
# do with the freshness of the feed.
_ENTRY_UPDATED_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:f</id>
  <updated>2026-08-10T16:43:47Z</updated>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://bh/UsagePoint/UP1"/>
    <updated>2004-06-18T00:00:00Z</updated>
    <content><espi:UsagePoint><espi:ServiceCategory><espi:kind>0</espi:kind></espi:ServiceCategory></espi:UsagePoint></content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://bh/UsagePoint/UP1/MeterReading/MR1"/>
    <updated>2026-08-10T08:40:36Z</updated>
    <content><espi:MeterReading/></content>
  </entry>
</feed>"""


def test_feed_updated_is_not_clobbered_by_the_first_entrys_updated() -> None:
    """The feed-level <updated> survives entries that carry their own (issues/42).

    iterparse fires a child's end event BEFORE its parent's, so a "have we reached an entry yet"
    flag set on the <entry> end is still unset while the first entry's own <updated> closes — and
    that stale per-resource timestamp overwrote the feed's. Burlington's first entry is a
    UsagePoint stamped 2004-06-18, so diagnostics reported a feed 22 years out of date on an
    account that was fetching fine, and the report it produced spent its length on a parser that
    wasn't the problem.
    """
    updated, usage_points = parse_usage_feed(_ENTRY_UPDATED_FEED)
    assert updated == datetime(2026, 8, 10, 16, 43, 47, tzinfo=UTC)
    # …and the entries still classify — the fix changes which events we iterate, so this guards
    # against fixing the timestamp by breaking the parse.
    assert [up.usage_point_id for up in usage_points] == ["UP1"]
    assert [s.meter_reading_id for s in usage_points[0].series] == ["MR1"]


def test_feed_updated_ignores_entry_updated_regardless_of_document_order() -> None:
    """Atom doesn't constrain the order of a feed's child elements.

    "Feed metadata comes first" is exactly the assumption that produced the bug above, so the test
    is which element the timestamp belongs to, not where it sits.
    """
    trailing = _ENTRY_UPDATED_FEED.replace(b"  <updated>2026-08-10T16:43:47Z</updated>\n", b"")
    trailing = trailing.replace(b"</feed>", b"  <updated>2026-08-10T16:43:47Z</updated>\n</feed>")
    updated, _usage_points = parse_usage_feed(trailing)
    assert updated == datetime(2026, 8, 10, 16, 43, 47, tzinfo=UTC)


def test_feed_without_its_own_updated_reports_none() -> None:
    """No feed-level <updated> ⇒ None, never an entry's.

    "Unknown" is a usable diagnostic; a resource-creation date presented as the feed timestamp is
    an actively misleading one.
    """
    no_feed_updated = _ENTRY_UPDATED_FEED.replace(
        b"  <updated>2026-08-10T16:43:47Z</updated>\n", b""
    )
    updated, usage_points = parse_usage_feed(no_feed_updated)
    assert updated is None
    assert len(usage_points) == 1  # the entries are still parsed


# A RetailCustomer (customer-data) feed in the ESPI customer namespace — mirrors the real shape
# (CustomerAccount/accountId + ServiceLocation/mainAddress) used to distinguish two accounts at
# the same utility. Structure taken from an anonymized Green Button "Download My Data" feed.
_CUSTOMER_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cust="http://naesb.org/espi/customer">
  <entry>
    <content>
      <cust:Customer>
        <cust:Organisation>
          <cust:organisationName>Jane Doe</cust:organisationName>
        </cust:Organisation>
      </cust:Customer>
    </content>
  </entry>
  <entry>
    <content>
      <cust:CustomerAccount>
        <cust:accountId>100001-0000001</cust:accountId>
      </cust:CustomerAccount>
    </content>
  </entry>
  <entry>
    <content>
      <cust:ServiceLocation>
        <cust:mainAddress>
          <cust:streetDetail>
            <cust:number>123</cust:number>
            <cust:name>EXAMPLE ST</cust:name>
            <cust:suiteNumber></cust:suiteNumber>
          </cust:streetDetail>
          <cust:townDetail>
            <cust:name>MILTON</cust:name>
            <cust:stateOrProvince>ON</cust:stateOrProvince>
            <cust:country>CA</cust:country>
          </cust:townDetail>
          <cust:postalCode>L0L 0L0</cust:postalCode>
        </cust:mainAddress>
      </cust:ServiceLocation>
    </content>
  </entry>
</feed>"""


def test_parse_customer_feed_extracts_account_address_and_name() -> None:
    """Account id, formatted service address, and organisation name all round-trip."""
    info = parse_customer_feed(_CUSTOMER_FEED)
    assert info is not None
    assert info.account_id == "100001-0000001"
    assert info.service_address == "123 EXAMPLE ST, MILTON ON, L0L 0L0"
    assert info.customer_name == "Jane Doe"
    # label prefers the service address (most human-recognizable).
    assert info.label == "123 EXAMPLE ST, MILTON ON, L0L 0L0"


def test_parse_customer_feed_falls_back_to_address_general() -> None:
    """A streetDetail with only <addressGeneral> (no number/name) still yields a street line."""
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cust="http://naesb.org/espi/customer">
  <entry>
    <content>
      <cust:ServiceLocation>
        <cust:mainAddress>
          <cust:streetDetail>
            <cust:addressGeneral>456 GENERAL AVE</cust:addressGeneral>
          </cust:streetDetail>
          <cust:townDetail>
            <cust:name>MILTON</cust:name>
            <cust:stateOrProvince>ON</cust:stateOrProvince>
          </cust:townDetail>
        </cust:mainAddress>
      </cust:ServiceLocation>
    </content>
  </entry>
</feed>"""
    info = parse_customer_feed(feed)
    assert info is not None
    assert info.service_address == "456 GENERAL AVE, MILTON ON"
    # No account id / name → label falls back to the address.
    assert info.label == "456 GENERAL AVE, MILTON ON"


def test_parse_customer_feed_label_prefers_account_when_no_address() -> None:
    """With no ServiceLocation, the label falls back to the account id."""
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cust="http://naesb.org/espi/customer">
  <entry>
    <content><cust:CustomerAccount><cust:accountId>ACC-42</cust:accountId></cust:CustomerAccount></content>
  </entry>
</feed>"""
    info = parse_customer_feed(feed)
    assert info is not None
    assert info.service_address is None
    assert info.label == "ACC-42"


def test_parse_customer_feed_returns_none_when_nothing_recognizable() -> None:
    """A feed with no customer payloads → None (nothing to label with)."""
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cust="http://naesb.org/espi/customer">
  <entry><content><cust:LocalTimeParameters/></content></entry>
</feed>"""
    assert parse_customer_feed(feed) is None
