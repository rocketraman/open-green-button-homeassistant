"""Config-flow tests for the Open Green Button HA integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.greenbutton.const import (
    CONF_API_VERSION,
    CONF_CLAIM_CODE,
    CONF_DAILY_POLL_TIME,
    CONF_DAILY_POLL_TIME_ENABLED,
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_INITIAL_HISTORY_SECONDS,
    CONF_PROXY_TOKEN,
    CONF_SCOPE,
    CONF_SERVER_BASE_URL,
    CONF_SUBSCRIPTION_URI,
    CONF_UTILITY_ID,
    CONF_UTILITY_NAME,
    DOMAIN,
)

from .const import (
    MOCK_CLAIM_RESPONSE,
    MOCK_UTILITIES,
    SERVER_BASE_URL,
    UTILITIES_URL,
    VALID_CLAIM_CODE,
    claim_url,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )


async def test_user_flow_happy_path(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """End-to-end: pick utility → paste valid claim → entry created."""
    aioclient_mock.get(UTILITIES_URL, json=MOCK_UTILITIES)
    aioclient_mock.post(claim_url(VALID_CLAIM_CODE), json=MOCK_CLAIM_RESPONSE)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UTILITY_ID: "example_utility"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connect"
    # Authorize URL is substituted into the description; the dropdown chose example_utility
    # so the link must reference it.
    placeholders = result["description_placeholders"]
    assert "example_utility" in placeholders["authorize_url"]
    assert placeholders["utility_name"] == "Example Utility"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CLAIM_CODE: VALID_CLAIM_CODE}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Example Utility"
    data = result["data"]
    assert data[CONF_UTILITY_ID] == "example_utility"
    assert data[CONF_UTILITY_NAME] == "Example Utility"
    assert data[CONF_SERVER_BASE_URL] == SERVER_BASE_URL
    assert data[CONF_ENCRYPTED_REFRESH_BLOB] == MOCK_CLAIM_RESPONSE["encryptedRefreshBlob"]
    assert data[CONF_PROXY_TOKEN] == MOCK_CLAIM_RESPONSE["proxyToken"]
    assert data[CONF_SUBSCRIPTION_URI] == MOCK_CLAIM_RESPONSE["subscriptionUri"]
    assert data[CONF_SCOPE] == MOCK_CLAIM_RESPONSE["scope"]
    assert data[CONF_API_VERSION] == MOCK_CLAIM_RESPONSE["currentApiVersion"]
    assert data[CONF_INITIAL_HISTORY_SECONDS] == MOCK_CLAIM_RESPONSE["initialHistorySeconds"]


async def test_user_flow_strips_whitespace_in_claim_code(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Pasting a claim code with surrounding whitespace should still work."""
    aioclient_mock.get(UTILITIES_URL, json=MOCK_UTILITIES)
    aioclient_mock.post(claim_url(VALID_CLAIM_CODE), json=MOCK_CLAIM_RESPONSE)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UTILITY_ID: "example_utility"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CLAIM_CODE: f"  {VALID_CLAIM_CODE}  \n"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_aborts_when_utilities_endpoint_down(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A 5xx on /utilities surfaces a cannot_connect abort, not a 500 trace."""
    aioclient_mock.get(UTILITIES_URL, status=502)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_user_flow_aborts_when_no_utilities_configured(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An empty utility list is its own abort reason — easier for the user to diagnose."""
    aioclient_mock.get(UTILITIES_URL, json=[])

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_utilities"


async def test_connect_step_shows_invalid_or_expired_on_410(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """410 from /claim is a recoverable user error — show the form again with a message."""
    aioclient_mock.get(UTILITIES_URL, json=MOCK_UTILITIES)
    aioclient_mock.post(claim_url("gb_live_expired"), status=410)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UTILITY_ID: "example_utility"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CLAIM_CODE: "gb_live_expired"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connect"
    assert result["errors"] == {"base": "invalid_or_expired_code"}


async def test_connect_step_shows_cannot_connect_on_network_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A non-410 failure during claim redemption recovers via the form with an error."""
    aioclient_mock.get(UTILITIES_URL, json=MOCK_UTILITIES)
    aioclient_mock.post(claim_url(VALID_CLAIM_CODE), status=500)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UTILITY_ID: "example_utility"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CLAIM_CODE: VALID_CLAIM_CODE}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_connect_step_rejects_empty_claim_code(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Whitespace-only claim code never reaches the server; user gets a friendly message."""
    aioclient_mock.get(UTILITIES_URL, json=MOCK_UTILITIES)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UTILITY_ID: "example_utility"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CLAIM_CODE: "   "}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "claim_code_empty"}


async def test_second_flow_for_same_account_aborts_already_configured(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """unique_id == utility_id + subscription_uri; a re-run for the same account is a no-op."""
    aioclient_mock.get(UTILITIES_URL, json=MOCK_UTILITIES)
    aioclient_mock.post(claim_url(VALID_CLAIM_CODE), json=MOCK_CLAIM_RESPONSE)

    # First run — creates the entry.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UTILITY_ID: "example_utility"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CLAIM_CODE: VALID_CLAIM_CODE}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Second run — same claim response (same subscription_uri) → already_configured abort.
    aioclient_mock.post(claim_url("gb_live_another"), json=MOCK_CLAIM_RESPONSE)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UTILITY_ID: "example_utility"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CLAIM_CODE: "gb_live_another"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.fixture(name="config_entry")
async def fixture_config_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> config_entries.ConfigEntry:
    """Convenience: run the full flow once and return the resulting entry."""
    aioclient_mock.get(UTILITIES_URL, json=MOCK_UTILITIES)
    aioclient_mock.post(claim_url(VALID_CLAIM_CODE), json=MOCK_CLAIM_RESPONSE)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UTILITY_ID: "example_utility"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CLAIM_CODE: VALID_CLAIM_CODE}
    )
    return result["result"]


async def test_reauth_flow_updates_existing_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: config_entries.ConfigEntry,
) -> None:
    """Reauth path replaces tokens on the existing entry instead of creating a duplicate."""
    new_response = {**MOCK_CLAIM_RESPONSE, "encryptedRefreshBlob": "newblob=="}
    aioclient_mock.post(claim_url("gb_live_reauth"), json=new_response)

    # Trigger reauth as the coordinator would when it sees a 401 from the proxy.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": config_entry.entry_id,
        },
        data=config_entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CLAIM_CODE: "gb_live_reauth"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    # Existing entry's blob now reflects the new one.
    assert config_entry.data[CONF_ENCRYPTED_REFRESH_BLOB] == "newblob=="


async def test_options_flow_saves_the_daily_poll_time(
    hass: HomeAssistant,
    config_entry: config_entries.ConfigEntry,
) -> None:
    """Polling preferences round-trip through the options flow into entry.options."""
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DAILY_POLL_TIME_ENABLED: True, CONF_DAILY_POLL_TIME: "06:00:00"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_DAILY_POLL_TIME_ENABLED: True,
        CONF_DAILY_POLL_TIME: "06:00:00",
    }
