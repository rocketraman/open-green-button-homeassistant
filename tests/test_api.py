"""Tests for the proxy-server HTTP client — fetch_usage paths.

After the pass-through refactor: the proxy returns raw ESPI XML rather than normalized JSON,
and rotated credentials arrive in response **headers**. These tests cover the XML→dataclass
parse round-trip plus the auth-error mapping that distinguishes "reauth required" from
"transient upstream failure".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.greenbutton.api import (
    OpenGbApi,
    OpenGbApiError,
    OpenGbAuthExpiredError,
    OpenGbDataPendingError,
    OpenGbPermanentError,
)

from .const import (
    MOCK_USAGE_XML,
    PROXY_CUSTOMER_URL,
    PROXY_USAGE_URL,
    ROTATED_HEADERS,
    SERVER_BASE_URL,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )


def _api(hass: HomeAssistant) -> OpenGbApi:
    return OpenGbApi(session=async_get_clientsession(hass), server_base_url=SERVER_BASE_URL)


async def test_fetch_usage_parses_proxied_atom_xml_into_dataclasses(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Happy path: ESPI Atom XML parses into the same domain tree the coordinator consumes."""
    aioclient_mock.post(
        PROXY_USAGE_URL,
        content=MOCK_USAGE_XML,
        headers={"Content-Type": "application/atom+xml"},
    )

    response = await _api(hass).fetch_usage("blob_value", "token_value")  # noqa: S106

    assert response.updated == datetime(2026, 6, 3, 14, 0, 0, tzinfo=UTC)
    assert response.new_credentials is None
    assert len(response.usage_points) == 1

    up = response.usage_points[0]
    assert up.usage_point_id == "e082e9a9-390b-58fb-8ca5-4ee707c95652"
    assert up.service_kind == "ELECTRICITY"
    assert len(up.series) == 1

    series = up.series[0]
    assert series.meter_reading_id == "022e1a41-a279-5af7-889e-3b46e67d9a01"
    assert series.reading_type.commodity == "ELECTRICITY_SECONDARY_METERED"
    assert series.reading_type.flow_direction == "FORWARD"
    assert series.reading_type.accumulation_behaviour == "DELTA_DATA"
    assert series.reading_type.unit_of_measure == "WATT_HOURS"
    assert series.reading_type.unit_of_measure_symbol == "Wh"
    assert series.reading_type.interval_length_seconds == 3600
    assert series.reading_type.currency_numeric_code == 124

    assert len(series.readings) == 2
    assert series.readings[0].start == datetime(2025, 2, 24, 5, 0, 0, tzinfo=UTC)
    assert series.readings[0].duration_seconds == 3600
    assert series.readings[0].value == 1000.0
    assert series.readings[1].start == datetime(2025, 2, 24, 6, 0, 0, tzinfo=UTC)
    assert series.readings[1].value == 1500.0

    # The fixture also carries a UsageSummary block attached to the UsagePoint via the
    # `related` link. After parsing it should appear on `up.summaries` with the right
    # billing-period start, total cost (raw → currency units), and line-item breakdown.
    assert len(up.summaries) == 1
    summary = up.summaries[0]
    assert summary.billing_period_start == datetime(2024, 5, 19, 14, 40, 8, tzinfo=UTC)
    assert summary.billing_period_duration_seconds == 2_592_000
    assert summary.bill_last_period_raw == 10_250_000
    assert summary.total_cost == 102.50
    assert summary.currency_numeric_code == 124
    assert len(summary.cost_details) == 2
    assert summary.cost_details[0].note == "Regulatory Charges"
    assert summary.cost_details[0].amount == 0.027
    assert summary.cost_details[1].note == "Delivery"
    assert summary.cost_details[1].amount == 0.6178


async def test_fetch_usage_surfaces_new_credentials_from_response_headers(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Refresh-token rotation arrives via OpenGB-New-* response headers, not the body."""
    aioclient_mock.post(
        PROXY_USAGE_URL,
        content=MOCK_USAGE_XML,
        headers={"Content-Type": "application/atom+xml", **ROTATED_HEADERS},
    )

    response = await _api(hass).fetch_usage("blob_value", "token_value")  # noqa: S106

    assert response.new_credentials is not None
    assert response.new_credentials.encrypted_refresh_blob == "rotated_blob_value"
    assert response.new_credentials.proxy_token == "rotated_proxy_token"  # noqa: S105


async def test_fetch_usage_passes_published_window(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """tz-aware datetimes → ISO 8601 with `Z` (the format the test-lab harness wants)."""
    aioclient_mock.post(
        PROXY_USAGE_URL,
        content=MOCK_USAGE_XML,
        headers={"Content-Type": "application/atom+xml"},
    )

    await _api(hass).fetch_usage(
        "blob_value",
        "token_value",  # noqa: S106
        published_min=datetime(2024, 2, 23, 5, 0, 0, tzinfo=UTC),
        published_max=datetime(2026, 2, 24, 5, 0, 0, tzinfo=UTC),
    )

    last = aioclient_mock.mock_calls[-1]
    body = last[2]
    assert body["encryptedRefreshBlob"] == "blob_value"
    assert body["publishedMin"] == "2024-02-23T05:00:00Z"
    assert body["publishedMax"] == "2026-02-24T05:00:00Z"


async def test_fetch_usage_rejects_naive_datetimes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Naive datetimes are ambiguous (no UTC offset) → ValueError before we hit the wire."""
    aioclient_mock.post(
        PROXY_USAGE_URL,
        content=MOCK_USAGE_XML,
        headers={"Content-Type": "application/atom+xml"},
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        await _api(hass).fetch_usage(
            "blob_value",
            "token_value",  # noqa: S106
            published_min=datetime(2024, 2, 23, 5, 0, 0),  # noqa: DTZ001
        )


async def test_fetch_usage_translates_utility_auth_expired_to_distinct_exception(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """401 with error=utility_auth_expired → OpenGbAuthExpiredError (reauth signal)."""
    aioclient_mock.post(
        PROXY_USAGE_URL,
        status=401,
        json={"error": "utility_auth_expired", "message": "refresh expired"},
    )

    # The utility's own reason (proxy `message`) is surfaced in the exception so HA shows why.
    with pytest.raises(OpenGbAuthExpiredError, match="refresh expired"):
        await _api(hass).fetch_usage("blob_value", "token_value")  # noqa: S106


async def test_fetch_usage_treats_other_401_as_generic_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """401 invalid_credentials is *our* bug (proxy token mismatch), not a reauth trigger."""
    aioclient_mock.post(
        PROXY_USAGE_URL,
        status=401,
        json={"error": "invalid_credentials"},
    )

    with pytest.raises(OpenGbApiError) as exc_info:
        await _api(hass).fetch_usage("blob_value", "token_value")  # noqa: S106
    assert not isinstance(exc_info.value, OpenGbAuthExpiredError)


async def test_fetch_usage_treats_5xx_as_generic_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """5xx upstream errors surface as OpenGbApiError (UpdateFailed, not reauth)."""
    aioclient_mock.post(PROXY_USAGE_URL, status=502, json={"error": "utility_upstream_error"})

    with pytest.raises(OpenGbApiError) as exc_info:
        await _api(hass).fetch_usage("blob_value", "token_value")  # noqa: S106
    assert not isinstance(exc_info.value, OpenGbAuthExpiredError)


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_fetch_customer_treats_4xx_as_permanent(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
) -> None:
    """A propagated 4xx (e.g. Burlington's 403 access_denied) is permanent — a distinct error the
    coordinator uses to stop retrying customer data every poll."""
    aioclient_mock.post(PROXY_CUSTOMER_URL, status=status, json={"error": "utility_upstream_error"})

    with pytest.raises(OpenGbPermanentError):
        await _api(hass).fetch_customer("blob_value", "token_value")  # noqa: S106


@pytest.mark.parametrize("status", [500, 502, 503, 408, 429])
async def test_fetch_customer_treats_5xx_and_retryable_4xx_as_transient(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
) -> None:
    """5xx (server error) and the retryable 4xx (408 timeout, 429 rate-limited) stay transient —
    a plain OpenGbApiError, so the label fetch is retried on the next poll rather than abandoned."""
    aioclient_mock.post(PROXY_CUSTOMER_URL, status=status, json={"error": "utility_upstream_error"})

    with pytest.raises(OpenGbApiError) as exc_info:
        await _api(hass).fetch_customer("blob_value", "token_value")  # noqa: S106
    assert not isinstance(exc_info.value, OpenGbPermanentError)


async def test_fetch_usage_carries_rotated_credentials_on_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A one-time refresh token can be rotated (via OpenGB-New-* headers) even when the resource
    fetch then fails: the proxy refreshes before fetching. The raised error must carry the new
    credentials so the coordinator can persist them and retry, instead of reusing a burned token.
    """
    aioclient_mock.post(
        PROXY_USAGE_URL,
        status=502,
        json={"error": "utility_upstream_error"},
        headers=ROTATED_HEADERS,
    )

    with pytest.raises(OpenGbApiError) as exc_info:
        await _api(hass).fetch_usage("blob_value", "token_value")  # noqa: S106
    creds = exc_info.value.new_credentials
    assert creds is not None
    assert creds.encrypted_refresh_blob == "rotated_blob_value"
    assert creds.proxy_token == "rotated_proxy_token"  # noqa: S105


async def test_fetch_usage_translates_202_to_data_pending_exception(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """202 utility_data_pending (async batch) → OpenGbDataPendingError, distinct from a 5xx.

    The coordinator keys on this to raise a background-load repair issue rather than treat it
    as a transient upstream failure.
    """
    aioclient_mock.post(PROXY_USAGE_URL, status=202, json={"error": "utility_data_pending"})

    with pytest.raises(OpenGbDataPendingError):
        await _api(hass).fetch_usage("blob_value", "token_value")  # noqa: S106


async def test_fetch_usage_202_surfaces_the_proxys_message(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The proxy's `message` (the custodian's own 202) reaches the raised exception.

    That message carries the resource server's status, headers, and body — Location names where
    the prepared batch will live, Retry-After says when. It cannot be reproduced on demand (it
    needs a live authorization at a custodian that defers), so the affected user's HA log is the
    only place to collect it from; dropping it here would strand the investigation.
    """
    aioclient_mock.post(
        PROXY_USAGE_URL,
        status=202,
        json={
            "error": "utility_data_pending",
            "message": "response-headers: [Location: https://dc.example/Batch/Bulk/000001]",
        },
    )

    with pytest.raises(OpenGbDataPendingError) as excinfo:
        await _api(hass).fetch_usage("blob_value", "token_value")  # noqa: S106

    assert "https://dc.example/Batch/Bulk/000001" in str(excinfo.value)
