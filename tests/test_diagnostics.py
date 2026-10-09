"""Tests for the config-entry diagnostics handler."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.greenbutton.api import (
    BillingSummary,
    CostDetail,
    MeterReadingSeries,
    NormalizedReadingType,
    UsagePoint,
    UsageResponse,
)
from custom_components.greenbutton.const import (
    CONF_CUSTOMER_ACCOUNT_ID,
    CONF_CUSTOMER_ADDRESS,
    CONF_CUSTOMER_LABEL,
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_PROXY_TOKEN,
    CONF_UTILITY_ID,
    CONF_UTILITY_NAME,
    DOMAIN,
)
from custom_components.greenbutton.diagnostics import (
    async_get_config_entry_diagnostics,
    async_remove_xml_cache,
)
from custom_components.greenbutton.storage import xml_cache_path

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _entry_with_data(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Example Utility",
        data={
            CONF_UTILITY_ID: "example_utility",
            CONF_UTILITY_NAME: "Example Utility",
            CONF_ENCRYPTED_REFRESH_BLOB: "secret_blob_value",  # noqa: S106
            CONF_PROXY_TOKEN: "secret_proxy_token_value",  # noqa: S106
        },
    )
    entry.add_to_hass(hass)
    return entry


def _sample_response() -> UsageResponse:
    rt = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="DELTA_DATA",
        interval_length_seconds=3600,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    summary = BillingSummary(
        billing_period_start=datetime(2024, 5, 19, 14, 40, 8, tzinfo=UTC),
        billing_period_duration_seconds=2_592_000,
        bill_last_period_raw=0,
        cost_additional_last_period_raw=0,
        cost_details=[
            CostDetail(amount_raw=2700, note="Regulatory Charges", item_kind=0, unit_cost_raw=0),
            CostDetail(amount_raw=21960, note="Off Peak-Summer", item_kind=0, unit_cost_raw=0),
        ],
        currency_numeric_code=124,
    )
    return UsageResponse(
        updated=datetime(2026, 6, 3, 14, 0, tzinfo=UTC),
        usage_points=[
            UsagePoint(
                usage_point_id="e082e9a9-390b-58fb-8ca5-4ee707c95652",
                service_kind="ELECTRICITY",
                series=[
                    MeterReadingSeries(
                        meter_reading_id="022e1a41-a279-5af7-889e-3b46e67d9a01",
                        reading_type=rt,
                        readings=[],  # readings deliberately empty — diagnostics summarises counts
                    ),
                ],
                summaries=[summary],
            ),
        ],
        new_credentials=None,
    )


def _stub_coordinator(response: UsageResponse | None) -> MagicMock:
    coord = MagicMock()
    coord.data = response
    coord.last_update_success = True
    coord.last_exception = None
    coord.update_interval = timedelta(hours=6)
    return coord


async def test_diagnostics_redacts_credentials(hass: HomeAssistant) -> None:
    """The encrypted blob + proxy token in entry.data must not appear in the diagnostics
    output verbatim — they're the credentials the proxy verifies, and a copy-paste of the
    diagnostics file into a support ticket shouldn't leak them."""
    entry = _entry_with_data(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _stub_coordinator(_sample_response())

    result = await async_get_config_entry_diagnostics(hass, entry)

    # Round-trip through json to confirm the dict is serialisable AND the redaction works
    # everywhere we walk through (HA's redact_data only mutates the dict, not strings).
    text = json.dumps(result)
    assert "secret_blob_value" not in text
    assert "secret_proxy_token_value" not in text  # noqa: S105
    assert result["entry"]["data"][CONF_ENCRYPTED_REFRESH_BLOB] != "secret_blob_value"
    assert result["entry"]["data"][CONF_PROXY_TOKEN] != "secret_proxy_token_value"  # noqa: S105
    # Non-sensitive fields stay intact.
    assert result["entry"]["data"][CONF_UTILITY_ID] == "example_utility"


async def test_diagnostics_redacts_customer_address(hass: HomeAssistant) -> None:
    """The service address (and the account number) must not leave in a diagnostics bundle —
    not from entry.data, not from the entry title the label is folded into, and not from the
    raw feed, where custodians put the address in a UsagePoint entry's Atom title."""
    address = "123 EXAMPLE ST, BURLINGTON ON, L0L 0L0"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Example Utility — {address}",
        data={
            CONF_UTILITY_ID: "example_utility",
            CONF_UTILITY_NAME: "Example Utility",
            CONF_CUSTOMER_LABEL: address,
            CONF_CUSTOMER_ADDRESS: address,
            CONF_CUSTOMER_ACCOUNT_ID: "ACCT-0012345",
        },
    )
    entry.add_to_hass(hass)
    coord = _stub_coordinator(_sample_response())
    coord.last_exception = RuntimeError(f"no data for {address}")
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    path = xml_cache_path(hass, entry.entry_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(
            b"<feed xmlns='http://www.w3.org/2005/Atom'><title>Green Button</title>"
            b"<entry><title type='text'>123 Example St Unit 4</title>"
            b"<content><UsagePoint/></content></entry></feed>"
        )
    try:
        result = await async_get_config_entry_diagnostics(hass, entry)
    finally:
        os.remove(path)

    text = json.dumps(result)
    assert address not in text
    assert "123 Example St" not in text
    assert "ACCT-0012345" not in text
    assert result["entry"]["title"] == "Example Utility — **REDACTED**"
    assert result["entry"]["data"][CONF_UTILITY_NAME] == "Example Utility"
    # Structure of the feed survives; only the title text is blanked.
    assert "<title type='text'>**REDACTED**</title><content><UsagePoint/>" in result["raw_xml"]


async def test_diagnostics_summarizes_response_with_cost_details(hass: HomeAssistant) -> None:
    """The most important debugging info is the cost detail breakdown — assert it survives
    the summarization without losing the per-line-item amounts that the cost-distribution
    test for TOU is built on."""
    entry = _entry_with_data(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _stub_coordinator(_sample_response())

    result = await async_get_config_entry_diagnostics(hass, entry)

    last = result["last_response"]
    assert last is not None
    up = last["usage_points"][0]
    assert up["usage_point_id"] == "e082e9a9-390b-58fb-8ca5-4ee707c95652"
    assert up["service_kind"] == "ELECTRICITY"
    assert len(up["series"]) == 1
    assert up["series"][0]["reading_count"] == 0

    summaries = up["summaries"]
    assert len(summaries) == 1
    s = summaries[0]
    assert s["billing_period_start"] == "2024-05-19T14:40:08+00:00"
    assert s["currency_numeric_code"] == 124
    assert len(s["cost_details"]) == 2
    notes = {d["note"] for d in s["cost_details"]}
    assert notes == {"Regulatory Charges", "Off Peak-Summer"}


async def test_diagnostics_includes_raw_xml_when_file_present(hass: HomeAssistant) -> None:
    """When debug logging dropped an XML cache on disk, diagnostics inlines its contents.

    The handler doesn't itself check the debug flag (the gate is on the *write* side, in
    the coordinator) — if a file exists, we surface it. That keeps the diagnostics output
    self-explanatory: presence of `raw_xml` means somebody had debug on at least once.
    """
    entry = _entry_with_data(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _stub_coordinator(_sample_response())

    # Pre-populate the cache file as if a debug-enabled refresh had just run.
    path = xml_cache_path(hass, entry.entry_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"<?xml version='1.0'?><feed/>")

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["raw_xml_cached_bytes"] == len(b"<?xml version='1.0'?><feed/>")
    assert result["raw_xml"] == "<?xml version='1.0'?><feed/>"
    assert result["raw_xml_path"] == path

    # Cleanup so other tests in this file don't see a leftover cache.
    os.remove(path)


async def test_diagnostics_omits_raw_xml_when_no_cache(hass: HomeAssistant) -> None:
    """Default state — no debug logging ever ran for this entry — yields raw_xml = None and
    raw_xml_cached_bytes = None, instead of an exception or empty string."""
    entry = _entry_with_data(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _stub_coordinator(_sample_response())

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["raw_xml"] is None
    assert result["raw_xml_cached_bytes"] is None


async def test_diagnostics_reports_debug_logging_flag(hass: HomeAssistant) -> None:
    """The output records whether debug logging is on so a support recipient can see at a
    glance why `raw_xml` might be missing — vs. assuming a bug."""
    entry = _entry_with_data(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _stub_coordinator(_sample_response())

    # Force the integration's logger to DEBUG for the duration of this test.
    integ_logger = logging.getLogger("custom_components.greenbutton")
    original_level = integ_logger.level
    integ_logger.setLevel(logging.DEBUG)
    try:
        result = await async_get_config_entry_diagnostics(hass, entry)
    finally:
        integ_logger.setLevel(original_level)

    assert result["raw_xml_debug_logging_enabled"] is True


async def test_remove_xml_cache_is_idempotent(hass: HomeAssistant) -> None:
    """Calling the remove helper when no file exists must not raise — async_remove_entry
    runs this unconditionally and the typical entry never had debug enabled."""
    entry = _entry_with_data(hass)
    # No cache file pre-created.
    await async_remove_xml_cache(hass, entry.entry_id)  # Must not raise.


async def test_remove_xml_cache_deletes_existing_file(hass: HomeAssistant) -> None:
    """Inverse of the previous test — when the file is there, it gets removed."""
    entry = _entry_with_data(hass)
    path = xml_cache_path(hass, entry.entry_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"placeholder")

    await async_remove_xml_cache(hass, entry.entry_id)
    assert not os.path.exists(path)
