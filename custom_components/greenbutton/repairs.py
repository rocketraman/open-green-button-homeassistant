"""Repair flows for the Open Green Button integration.

One fixable issue so far: a meter the utility appears to have re-issued under a new UsagePoint
id (see [coordinator.GreenButtonCoordinator._async_check_replaced_usage_points]). Whether the
new id is the same meter is the user's call — a real second meter looks much the same in the
feed — so the flow offers both answers and the integration never merges without being told to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import CoreState

from .const import CONF_UTILITY_NAME, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.data_entry_flow import FlowResult

    from .coordinator import GreenButtonCoordinator


class UsagePointReplacedRepairFlow(RepairsFlow):
    """Ask whether a newly-appeared meter continues one that stopped reporting."""

    def __init__(
        self,
        entry_id: str,
        usage_point_id: str,
        replaced_usage_point_id: str,
        replaced_last_reading: str,
    ) -> None:
        """Remember which pair of meters the issue is about."""
        self._entry_id = entry_id
        self._usage_point_id = usage_point_id
        self._replaced_usage_point_id = replaced_usage_point_id
        self._replaced_last_reading = replaced_last_reading

    def _coordinator(self) -> GreenButtonCoordinator | None:
        return self.hass.data.get(DOMAIN, {}).get(self._entry_id)

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> FlowResult:  # noqa: ARG002
        """Offer the two answers."""
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="entry_not_loaded")
        return self.async_show_menu(
            step_id="init",
            menu_options=["merge", "keep_separate"],
            description_placeholders={
                "utility": coordinator.entry.data.get(CONF_UTILITY_NAME, "your utility"),
                "usage_point": self._usage_point_id[:8],
                "replaced_usage_point": self._replaced_usage_point_id[:8],
                "replaced_last_reading": self._replaced_last_reading,
            },
        )

    async def async_step_merge(self, user_input: dict[str, str] | None = None) -> FlowResult:  # noqa: ARG002
        """Same meter: continue the replaced meter's statistics with the new id's readings."""
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="entry_not_loaded")
        if self.hass.state is not CoreState.running:
            # The merge blocks on the recorder, which isn't draining its queue before startup.
            return self.async_abort(reason="not_started")
        await coordinator.async_merge_usage_point(
            self._usage_point_id, self._replaced_usage_point_id
        )
        return self.async_create_entry(data={})

    async def async_step_keep_separate(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002
    ) -> FlowResult:
        """Different meters: leave both sets of statistics alone and don't ask again."""
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="entry_not_loaded")
        coordinator.async_keep_usage_points_separate(
            self._usage_point_id, self._replaced_usage_point_id
        )
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,  # noqa: ARG001
    issue_id: str,  # noqa: ARG001
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for one of this integration's fixable issues."""
    data = data or {}
    return UsagePointReplacedRepairFlow(
        str(data.get("entry_id")),
        str(data.get("usage_point_id")),
        str(data.get("replaced_usage_point_id")),
        str(data.get("replaced_last_reading")),
    )
