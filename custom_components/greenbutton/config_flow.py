"""Config flow for Open Green Button.

Two-step user flow:
  user    — pick a utility from the proxy server's configured list
  connect — open authorize URL, complete OAuth at utility, paste claim code

Reauth (when the proxy returns 401):
  reauth_confirm — same as `connect`, but updates the existing entry instead of creating one
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)

from .api import (
    ClaimResponse,
    OpenGbApi,
    OpenGbApiError,
    OpenGbClaimNotFoundError,
    UtilitySummary,
)
from .const import (
    CONF_API_VERSION,
    CONF_CLAIM_CODE,
    CONF_DAILY_POLL_TIME,
    CONF_DAILY_POLL_TIME_ENABLED,
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_INITIAL_HISTORY_SECONDS,
    CONF_POLL_INTERVAL_SECONDS,
    CONF_PROXY_TOKEN,
    CONF_SCOPE,
    CONF_SERVER_BASE_URL,
    CONF_SUBSCRIPTION_URI,
    CONF_UTILITY_ID,
    CONF_UTILITY_NAME,
    DEFAULT_DAILY_POLL_TIME,
    DEFAULT_SERVER_BASE_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CLAIM_CODE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLAIM_CODE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="off"),
        ),
    },
)


class GreenButtonConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Open Green Button."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GreenButtonOptionsFlow:
        """Expose the polling-schedule options flow (Configure on the entry card)."""
        return GreenButtonOptionsFlow()

    def __init__(self) -> None:
        """Initialise transient flow state."""
        self._utilities: list[UtilitySummary] = []
        self._selected_utility: UtilitySummary | None = None
        self._server_base_url: str = DEFAULT_SERVER_BASE_URL
        # An opaque nonce we tack onto the authorize URL — surfaced in proxy server logs so we
        # can correlate the user's browser session back to this flow if support is needed.
        self._ha_nonce: str = uuid.uuid4().hex

    # ------------------------------------------------------------------
    # Initial setup
    # ------------------------------------------------------------------

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 1: pick a utility from the proxy server's configured list."""
        try:
            self._utilities = await self._api().list_utilities()
        except (OpenGbApiError, ClientError) as err:
            _LOGGER.warning("Failed to fetch utility list: %s", err)
            return self.async_abort(reason="cannot_connect")

        if not self._utilities:
            return self.async_abort(reason="no_utilities")

        if user_input is not None:
            utility_id = user_input[CONF_UTILITY_ID]
            self._selected_utility = next(
                (u for u in self._utilities if u.id == utility_id),
                None,
            )
            if self._selected_utility is None:
                return self.async_abort(reason="unknown_utility")
            return await self.async_step_connect()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_UTILITY_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": u.id, "label": u.display_name} for u in self._utilities
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                },
            ),
        )

    async def async_step_connect(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 2: show the authorize link + collect the claim code."""
        assert self._selected_utility is not None  # noqa: S101
        return await self._redeem_step(
            step_id="connect",
            user_input=user_input,
            on_success=self._create_entry,
        )

    # ------------------------------------------------------------------
    # Reauth (triggered by ConfigEntryAuthFailed in the coordinator)
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Entry point for reauth — pre-populate the utility and skip the picker."""
        self._selected_utility = UtilitySummary(
            id=entry_data[CONF_UTILITY_ID],
            display_name=entry_data.get(CONF_UTILITY_NAME, entry_data[CONF_UTILITY_ID]),
        )
        self._server_base_url = entry_data.get(
            CONF_SERVER_BASE_URL,
            DEFAULT_SERVER_BASE_URL,
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Reauth UI — same as `connect`, but updates the existing entry on success."""
        assert self._selected_utility is not None  # noqa: S101
        return await self._redeem_step(
            step_id="reauth_confirm",
            user_input=user_input,
            on_success=self._update_existing_entry,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _redeem_step(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        on_success: Any,
    ) -> ConfigFlowResult:
        """Shared form-render + claim-redemption logic for connect and reauth_confirm."""
        utility = self._selected_utility
        assert utility is not None  # noqa: S101

        authorize_url = (
            f"{self._server_base_url}/connect/{utility.id}/start?ha_nonce={self._ha_nonce}"
        )

        errors: dict[str, str] = {}
        if user_input is not None:
            claim_code = user_input[CONF_CLAIM_CODE].strip()
            if not claim_code:
                errors["base"] = "claim_code_empty"
            else:
                try:
                    claim = await self._api().redeem_claim(claim_code)
                except OpenGbClaimNotFoundError:
                    errors["base"] = "invalid_or_expired_code"
                except (OpenGbApiError, ClientError) as err:
                    _LOGGER.warning("Claim redemption failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    return await on_success(claim, utility)

        return self.async_show_form(
            step_id=step_id,
            data_schema=CLAIM_CODE_SCHEMA,
            description_placeholders={
                "utility_name": utility.display_name,
                "authorize_url": authorize_url,
            },
            errors=errors,
        )

    async def _create_entry(
        self,
        claim: ClaimResponse,
        utility: UtilitySummary,
    ) -> ConfigFlowResult:
        """Persist a fresh config entry from a successful claim redemption."""
        unique_id = _unique_id_for(claim)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=utility.display_name,
            data=_entry_data(self._server_base_url, utility, claim),
        )

    async def _update_existing_entry(
        self,
        claim: ClaimResponse,
        utility: UtilitySummary,
    ) -> ConfigFlowResult:
        """Reauth-success path: replace the existing entry's data with the fresh tokens."""
        existing = self._get_reauth_entry()
        return self.async_update_reload_and_abort(
            existing,
            data=_entry_data(self._server_base_url, utility, claim),
            reason="reauth_successful",
        )

    def _api(self) -> OpenGbApi:
        """Build an API client bound to HA's shared aiohttp session."""
        return OpenGbApi(
            session=async_get_clientsession(self.hass),
            server_base_url=self._server_base_url,
        )

    def _get_reauth_entry(self) -> Any:
        """Resolve the config entry that triggered reauth."""
        # HA 2024.12+ exposes self.context["entry_id"]; older versions used self.entry_id.
        entry_id = self.context.get("entry_id") if self.source == SOURCE_REAUTH else None
        if entry_id is None:
            raise RuntimeError("reauth flow ran without an entry_id in context")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            raise RuntimeError(f"entry {entry_id} disappeared during reauth")
        return entry


class GreenButtonOptionsFlow(OptionsFlowWithReload):
    """Polling-schedule preferences for one entry.

    Subclasses OptionsFlowWithReload so saving reloads the entry — that's what re-arms the
    poll timer with the new schedule. It reloads on *save* only, which is why the integration
    can still avoid the blanket update listener described in [__init__.async_setup_entry].
    """

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show and save the polling options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_DAILY_POLL_TIME_ENABLED, default=False): BooleanSelector(),
                vol.Required(CONF_DAILY_POLL_TIME, default=DEFAULT_DAILY_POLL_TIME): TimeSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),
        )


def _unique_id_for(claim: ClaimResponse) -> str:
    """Stable identifier per (utility, customer subscription).

    The subscription_uri is the utility's identifier for the customer's account; combining
    with utility_id ensures different utilities never collide. The integration entry is
    unique on this so re-running setup for the same customer is a no-op (aborts), while
    different customers / utilities can coexist.
    """
    return f"{claim.utility_id}:{claim.subscription_uri or ''}"


def _entry_data(
    server_base_url: str,
    utility: UtilitySummary,
    claim: ClaimResponse,
) -> dict[str, Any]:
    """The shape persisted in ConfigEntry.data."""
    return {
        CONF_UTILITY_ID: claim.utility_id,
        CONF_UTILITY_NAME: utility.display_name,
        CONF_SERVER_BASE_URL: server_base_url,
        CONF_ENCRYPTED_REFRESH_BLOB: claim.encrypted_refresh_blob,
        CONF_PROXY_TOKEN: claim.proxy_token,
        CONF_SUBSCRIPTION_URI: claim.subscription_uri,
        CONF_SCOPE: claim.scope,
        CONF_API_VERSION: claim.current_api_version,
        CONF_INITIAL_HISTORY_SECONDS: claim.initial_history_seconds,
        CONF_POLL_INTERVAL_SECONDS: claim.poll_interval_seconds,
    }
