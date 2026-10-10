"""Bridge from normalized proxy usage data → HA long-term external statistics.

Two hard requirements baked in here, documented in [__init__.py] and the README:

1. **Statistic IDs are scoped per config entry.** Two entries (a sandbox/test account and a
   real account on the same utility, say) MUST NOT collide in the Energy dashboard.
2. **Removal must purge.** ``async_remove_entry`` in __init__.py reads the same id format and
   calls ``recorder.async_clear_statistics`` so deleting the integration leaves no orphans.

Both requirements depend on ``statistic_id_for_series`` being the single source of truth for
the id format — never construct one ad-hoc elsewhere.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_list_statistic_ids,
    get_last_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.start import async_at_started

from .const import (
    CONF_USAGE_POINT_ALIASES,
    DOMAIN,
    LAST_FETCHED_OVERLAP,
    USAGE_POINT_REPLACED_LOOKBACK,
)
from .tou import cost_detail_tou_bucket, ontario_tou_bucket

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
    from homeassistant.config_entries import ConfigEntry

    from .api import (
        BillingSummary,
        MeterReadingSeries,
        NormalizedReadingType,
        UsagePoint,
        UsageResponse,
    )

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData

# `mean_type` replaces `has_mean` in HA core ≥ 2025.6; the legacy field becomes a hard
# requirement to omit at 2026.11. Import lazily so this module still loads on an HA core
# that predates the enum — if the import fails we'll keep emitting `has_mean=False` only.
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_TYPE_NONE = StatisticMeanType.NONE
except ImportError:  # pragma: no cover — older HA core, drop-through to has_mean only
    _MEAN_TYPE_NONE = None

_LOGGER = logging.getLogger(__name__)

# What a series measures, as far as statistic ids and labels care — see [_series_commodity].
COMMODITY_ELECTRICITY = "electricity"
COMMODITY_GAS = "gas"
COMMODITY_WATER = "water"


def statistic_id_for_series(
    entry_id: str,
    usage_point_id: str,
    flow_direction: str,
    commodity: str | None = None,
) -> str:
    """Return the canonical statistic_id for one (entry, usage_point, commodity, flow) series.

    Format: ``greenbutton:<entry_slug>_<usage_point_slug>_<flow_lower>`` for electricity, and
    ``greenbutton:<entry_slug>_<usage_point_slug>_<commodity>_<flow_lower>`` for gas and water
    ([commodity] is ``COMMODITY_GAS`` / ``COMMODITY_WATER``; see [_series_commodity]).

    Electricity keeps the bare form because that id is already in every existing user's Energy
    dashboard config — renaming it would orphan their history. Gas and water get a series of
    their own so a meter's gas can never be summed into the same statistic as its electricity,
    and so each lands under a name and unit the dashboard's gas and water pickers will offer.

    The entry_id prefix is what scopes a test entry's stats apart from a real entry's stats
    on the same utility. Each id component is slugified — HA enforces that the part of a
    statistic_id after the `:` matches a lowercase-letters/digits/underscores slug pattern,
    and our inputs (ULID entry_id with uppercase, UUID usage_point_id with hyphens) violate
    that as-is.
    """
    prefix = f"{DOMAIN}:{_slugify(entry_id)}_{_slugify(usage_point_id)}"
    if commodity in (COMMODITY_GAS, COMMODITY_WATER):
        return f"{prefix}_{commodity}_{flow_direction.lower()}"
    return f"{prefix}_{flow_direction.lower()}"


def statistic_id_for_cost(entry_id: str, usage_point_id: str) -> str:
    """Return the statistic_id for the cost series tied to a UsagePoint.

    Cost is per UsagePoint (matches one customer account's billing), not per flow direction
    — ESPI's UsageSummary is account-level. The id shares the same entry+usage-point prefix
    as the energy stats so async_remove_entry's prefix purge catches both.
    """
    return f"{DOMAIN}:{_slugify(entry_id)}_{_slugify(usage_point_id)}_cost"


def statistic_usage_point_id(entry: ConfigEntry, usage_point_id: str) -> str:
    """The usage point whose statistics [usage_point_id]'s readings are written to.

    Itself, unless the user has merged it into a meter it replaced — see
    CONF_USAGE_POINT_ALIASES. Aliases chain, so a meter the utility re-issued twice resolves
    through both to the original, whose statistic ids are the ones in the Energy dashboard.
    """
    aliases = entry.data.get(CONF_USAGE_POINT_ALIASES)
    if not isinstance(aliases, dict):
        return usage_point_id
    seen = {usage_point_id}
    while (target := aliases.get(usage_point_id)) is not None and target not in seen:
        seen.add(target)
        usage_point_id = target
    return usage_point_id


def statistic_id_prefix_for_entry(entry_id: str) -> str:
    """Return the ``startswith`` prefix that matches every statistic owned by an entry.

    Used by ``async_remove_entry`` to find all of an entry's stats for purging — pairs with
    [statistic_id_for_series] so the format (and the slugification) only live in one place.
    """
    return f"{DOMAIN}:{_slugify(entry_id)}_"


async def async_clear_statistics_for_entry(hass: HomeAssistant, entry_id: str) -> list[str]:
    """Delete every long-term statistic owned by one config entry; return the ids cleared.

    Shared by ``async_remove_entry`` (teardown) and the ``rebuild_statistics`` service
    (purge-before-reimport), so the "which ids belong to this entry" rule lives in one place
    next to [statistic_id_for_series] / [statistic_id_prefix_for_entry].

    ``Recorder.async_clear_statistics`` is a ``@callback`` that queues the delete on the
    recorder's worker thread — call it from the event loop, never wrap it in an executor job
    (that would bypass the recorder queue and run a callback off-loop).
    """
    owned = await _async_statistic_ids_for_entry(hass, entry_id)
    if owned:
        get_instance(hass).async_clear_statistics(owned)
    return owned


async def async_entry_has_statistics(hass: HomeAssistant, entry_id: str) -> bool:
    """True when this entry already owns at least one long-term statistic.

    Read-only counterpart to [async_clear_statistics_for_entry]. The coordinator uses it to
    tell "this entry has imported before, under whatever logic shipped then" from "this entry
    is importing for the first time" — which decides whether a one-time rebuild is warranted
    after an import-logic change. See [coordinator.GreenButtonCoordinator._async_migrate_import].
    """
    return bool(await _async_statistic_ids_for_entry(hass, entry_id))


async def _async_statistic_ids_for_entry(hass: HomeAssistant, entry_id: str) -> list[str]:
    """Every statistic id owned by [entry_id], per the [statistic_id_for_series] format.

    ``async_list_statistic_ids`` is ``async`` (not a ``@callback``) and takes no source filter,
    so we list everything the recorder knows and filter to our source + this entry's prefix. The
    source check is the load-bearing one; the prefix keeps us off a sibling entry's rows.
    """
    prefix = statistic_id_prefix_for_entry(entry_id)
    all_ids = await async_list_statistic_ids(hass)
    return [
        item["statistic_id"]
        for item in all_ids
        if item.get("source") == DOMAIN and item["statistic_id"].startswith(prefix)
    ]


def _usage_point_statistic_prefix(entry_id: str, usage_point_id: str) -> str:
    """The ``startswith`` prefix of every statistic written under one usage point id."""
    return f"{statistic_id_prefix_for_entry(entry_id)}{_slugify(usage_point_id)}_"


async def async_usage_point_looks_replaced(
    hass: HomeAssistant,
    entry: ConfigEntry,
    silent_usage_point_id: str,
    silent_since: datetime,
    up: UsagePoint,
) -> bool:
    """Whether [up] looks like the same meter as one that has gone silent, under a new id.

    Two things have to hold, both read from the recorder:

      - [up] measures what the silent meter measured — at least one of the usage statistics it
        writes already exists under the silent meter's id. A gas meter that publishes monthly is
        silent through most of an electricity meter's polls and is not being replaced by it.
      - [up] has no history from before the silent meter stopped. A replacement starts where the
        old id left off; a second meter that has been reporting all along has rows well before.

    Deliberately only a "looks like": the answer raises a repair issue for the user to decide,
    it never merges anything by itself.
    """
    owned = set(await _async_statistic_ids_for_entry(hass, entry.entry_id))
    silent_target = statistic_usage_point_id(entry, silent_usage_point_id)
    same_measurement = any(
        statistic_id_for_series(
            entry.entry_id,
            silent_target,
            series.reading_type.flow_direction,
            _series_commodity(up, series),
        )
        in owned
        for series in up.series
        if _is_interval_consumption_series(up, series)
        and _ha_unit_for(series.reading_type) is not None
    )
    if not same_measurement:
        return False

    own_prefix = _usage_point_statistic_prefix(entry.entry_id, up.usage_point_id)
    own_ids = {statistic_id for statistic_id in owned if statistic_id.startswith(own_prefix)}
    if not own_ids:
        return True  # Nothing recorded under the new id yet, so certainly no older history.
    # A day clear of the poll overlap: the first poll that saw the new id asked from one overlap
    # behind the silent meter's cursor, so a replacement legitimately has rows that far back.
    before = silent_since - LAST_FETCHED_OVERLAP - timedelta(days=1)
    by_id = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        before - USAGE_POINT_REPLACED_LOOKBACK,
        before,
        own_ids,
        "hour",
        None,
        {"sum"},
    )
    return not any(by_id.values())


async def async_merge_usage_point_statistics(
    hass: HomeAssistant, entry_id: str, source_usage_point_id: str, target_usage_point_id: str
) -> int:
    """Move every statistic row written under one usage point id onto another's; return the count.

    The counterpart to an alias (CONF_USAGE_POINT_ALIASES): the alias sends *future* readings to
    the target's statistics, this carries over what was already written under the source id while
    the two were being treated as different meters.

    Each source statistic is appended to its namesake on the target. Rows the target already has
    are skipped — the two overlap by a poll window — and the rest continue the target's running
    sum, rebased on the source's sum at the last skipped row so no consumption is counted twice
    or dropped. The source statistics are then cleared. Blocks until the recorder has committed
    both, so a caller may import under the alias straight afterwards and resume from the right
    point; only call it once Home Assistant is running (see the deadlock guard in
    [import_usage_statistics]).
    """
    source_prefix = _usage_point_statistic_prefix(entry_id, source_usage_point_id)
    target_prefix = _usage_point_statistic_prefix(entry_id, target_usage_point_id)
    source_ids = [
        statistic_id
        for statistic_id in await _async_statistic_ids_for_entry(hass, entry_id)
        if statistic_id.startswith(source_prefix)
    ]
    if not source_ids:
        return 0
    target_ids = {
        source_id: target_prefix + source_id[len(source_prefix) :] for source_id in source_ids
    }

    recorder = get_instance(hass)
    metadata_by_id = await recorder.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={*source_ids, *target_ids.values()})
    )
    rows_by_id = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime.fromtimestamp(0, tz=UTC),
        None,
        set(source_ids),
        "hour",
        None,
        {"sum"},
    )

    moved = 0
    for source_id in source_ids:
        target_id = target_ids[source_id]
        resume_from_sum, resume_after_epoch = await _resume_point(hass, target_id)
        baseline = 0.0
        stats: list[StatisticData] = []
        for row in rows_by_id.get(source_id, []):
            total = row.get("sum")
            if total is None:
                continue
            start = row["start"]
            hour = start if isinstance(start, datetime) else datetime.fromtimestamp(start, tz=UTC)
            if resume_after_epoch is not None and hour.timestamp() <= resume_after_epoch:
                baseline = total
                continue
            value = resume_from_sum + (total - baseline)
            stats.append(StatisticData(start=hour, state=value, sum=value))
        if not stats:
            continue
        # The target's own metadata when it has any, so its name and unit are untouched; a
        # statistic the target never had (a first bill, say) takes the source's.
        _, known = metadata_by_id.get(target_id) or metadata_by_id[source_id]
        metadata: StatisticMetaData = {**known, "statistic_id": target_id}
        _LOGGER.info(
            "Merging %d statistic rows from %s into %s (resume_from_sum=%.3f)",
            len(stats),
            source_id,
            target_id,
            resume_from_sum,
        )
        async_add_external_statistics(hass, metadata, stats)
        moved += len(stats)

    recorder.async_clear_statistics(source_ids)
    await recorder.async_block_till_done()
    return moved


def response_has_cumulative_series(response: UsageResponse) -> bool:
    """True when [response] carries a cumulative meter register we now exclude from statistics.

    The signal that an entry's *stored* statistics may be corrupt: before the fix for issue #6,
    a register series like this was summed into the consumption statistic (and its ``cost=0``
    placeholder hijacked cost selection, issue #7). An entry whose feed contains one, and which
    imported under the old logic, needs its statistics rebuilt — see
    [coordinator.GreenButtonCoordinator._async_migrate_import].
    """
    return any(
        not _is_interval_consumption_series(up, s)
        for up in response.usage_points
        for s in up.series
    )


def response_has_multi_hour_readings(response: UsageResponse) -> bool:
    """True when [response] carries an importable reading that spans more than one hour.

    The revision-2 signal (see [const.IMPORT_LOGIC_REVISION]). Such a reading used to be
    written entirely into the hour it started in — a whole billing period's consumption as one
    midnight spike — where it is now spread across the hours it covers ([_hours_spanned]).

    Scoped to series that actually import, which is what keeps Milton Hydro off this path: its
    daily ``BULK_QUANTITY`` register spans 24 hours, but it's excluded in favour of the hourly
    ``DELTA_DATA`` sibling, so its stored rows never came from a multi-hour reading and it needs
    no second rebuild. Hourly feeds (Burlington, Elexicon) are stamped forward untouched.
    """
    return any(
        len(_hours_spanned(r.start, r.duration_seconds)) > 1
        for up in response.usage_points
        for s in up.series
        if _is_interval_consumption_series(up, s)
        for r in s.readings
    )


def response_cost_may_be_missing_bills(response: UsageResponse) -> bool | None:
    """Whether [response] shows this entry's *cost* rows were built from only some of its bills.

    The revision-3 signal. A summary that overlapped one already costed used to be rejected
    outright as a duplicate, on the belief that consecutive periods only touch at an instant. They
    share a meter-read day, so roughly every other bill was discarded and the cost statistic was
    built from about half the money — where a later bill now replaces the earlier one's hours
    instead (see [_import_cost_summaries]).

    Unlike revisions 1 and 2, this can't be recognized from the overlap itself. Those were
    properties of the *series*, present in every poll that carries readings; bills appear only in
    the poll that publishes them, and a monthly utility hands over one at a time — so a pair of
    overlapping periods would essentially never be in one response, and an entry judged on that
    would be stamped "unaffected" and never repaired. The signal has to be the thing that is
    stably observable: does this account cost from ``UsageSummary`` at all?

      - **True** — some usage point bills through summaries. Its stored cost is suspect, because
        we can't see from here which bills the old rule dropped.
      - **False** — every usage point itemizes per-interval ``<cost>`` (savagedata/Milton,
        Elexicon). Summary selection never ran for it, so nothing is wrong.
      - **None** — this poll showed neither, so it tells us nothing either way. The caller waits
        rather than stamping the entry as unaffected on absent evidence. An account whose utility
        publishes no cost of any kind stays undecided indefinitely and re-checks each poll —
        cheap, and it has no cost rows to repair regardless.
    """
    verdicts: list[bool | None] = []
    for up in response.usage_points:
        if up.summaries and not _has_interval_cost(up):
            return True
        # Per-interval cost settles it without needing a summary in this particular poll, which
        # matters: it's what lets a savagedata-family account be cleared on an ordinary poll
        # instead of waiting for a billing month to turn over.
        verdicts.append(False if _has_interval_cost(up) else None)
    if verdicts and all(v is False for v in verdicts):
        return False
    return None


def response_has_gas_or_water_series(response: UsageResponse) -> bool:
    """True when [response] carries a series whose statistic changed with gas/water support.

    The revision-4 signal. Before it, only watt-hours and "cubic metres" were importable, every
    series went to the bare electricity-style statistic id, and ESPI uom 119 — cubic FEET — was
    written as m³. An entry with such a feed needs a rebuild for three reasons at once: rows
    stored under the old id have to move to the dedicated gas/water series, cubic-feet values
    recorded as m³ are wrong by a factor of 35, and units that used to be skipped (therms,
    gallons, ...) have history behind the poll cursor that only a full re-fetch will reach.

    A feed that is watt-hours on a non-gas, non-water series throughout — every electricity
    account — is untouched and stamped forward without a rebuild.
    """
    return any(
        _UNIT_MAP.get(s.reading_type.unit_of_measure) is not None
        and (
            s.reading_type.unit_of_measure != "WATT_HOURS"
            or _series_commodity(up, s) in (COMMODITY_GAS, COMMODITY_WATER)
        )
        for up in response.usage_points
        for s in up.series
    )


def response_has_series_sharing_a_statistic(response: UsageResponse) -> bool:
    """True when [response] carries two or more importable series bound for one statistic.

    The revision-5 signal. Each such series used to be accumulated on its own instead of into
    one running sum — see [_import_series_group] for what that did to the stored rows.

    A single poll understates this: an incremental window often holds just the newest
    MeterReading of a feed that splits its history into many. [async_stored_usage_shows_resets]
    is the check that doesn't depend on what this poll happened to carry; the two are used
    together.
    """
    for up in response.usage_points:
        # (flow, commodity) is what [statistic_id_for_series] keys on.
        targets = [
            (s.reading_type.flow_direction, _series_commodity(up, s))
            for s in up.series
            if s.readings
            and _is_interval_consumption_series(up, s)
            and not _is_superseded_by_unit(up, s)
        ]
        if len(targets) != len(set(targets)):
            return True
    return False


# The revision whose damage [async_stored_usage_shows_resets] recognizes.
_SUM_RESET_REVISION = 5


async def async_stored_usage_shows_resets(
    hass: HomeAssistant, entry_id: str, stamped_revision: int
) -> bool:
    """Whether [entry_id]'s stored usage rows carry the revision-5 damage: a sum that falls.

    Read from the recorder rather than recognized from the feed, because the feed shape that
    caused it ([response_has_series_sharing_a_statistic]) is rarely visible in one incremental
    poll, while the damage itself is unmistakable — a cumulative consumption sum never
    legitimately decreases. Scans every hourly row the entry owns, which is why it is gated on
    the stamp and so runs at most until the entry is next stamped.
    """
    if stamped_revision >= _SUM_RESET_REVISION:
        return False
    usage_ids = {
        statistic_id
        for statistic_id in await _async_statistic_ids_for_entry(hass, entry_id)
        if not statistic_id.endswith("_cost")
    }
    if not usage_ids:
        return False
    by_id = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime.fromtimestamp(0, tz=UTC),
        None,
        usage_ids,
        "hour",
        None,
        {"sum"},
    )
    for rows in by_id.values():
        sums = [row["sum"] for row in rows if row.get("sum") is not None]
        # Tolerance well under any real reading, so float noise can't trigger a full re-fetch.
        if any(later < earlier - 1e-6 for earlier, later in zip(sums, sums[1:], strict=False)):
            return True
    return False


# Recognition predicates keyed by the import-logic revision that fixed the bug. An entry stamped
# at revision N is tested against every predicate for a revision > N — so a user who skipped a
# release still gets each repair they're owed, in one rebuild rather than one per revision.
#
# A predicate may return None for "this poll can't tell" — see [response_cost_may_be_missing_bills].
_IMPORT_MIGRATION_CHECKS: dict[int, Callable[[UsageResponse], bool | None]] = {
    1: response_has_cumulative_series,
    2: response_has_multi_hour_readings,
    3: response_cost_may_be_missing_bills,
    4: response_has_gas_or_water_series,
    5: response_has_series_sharing_a_statistic,
}


def response_needs_import_migration(response: UsageResponse, stamped_revision: int) -> bool | None:
    """Whether [response]'s shape shows this entry's rows were produced by a since-fixed bug.

    [stamped_revision] is the entry's ``CONF_IMPORT_LOGIC_REVISION`` (0 when never stamped).

    Returns None when no predicate found an offending shape but at least one couldn't tell from
    this response — "not yet", not "no". Stamping an entry as unaffected on that would close the
    repair permanently, which is the one outcome none of this may produce.
    """
    verdicts = [
        check(response)
        for revision, check in _IMPORT_MIGRATION_CHECKS.items()
        if revision > stamped_revision
    ]
    if any(v is True for v in verdicts):
        return True
    if any(v is None for v in verdicts):
        return None
    return False


def _slugify(component: str) -> str:
    """Lowercase + replace non-alphanumeric with underscore.

    HA's ``valid_statistic_id`` rejects anything outside ``[a-z0-9_]`` after the colon. Our
    inputs are ULIDs (uppercase letters + digits) and UUIDs (hex + hyphens) — both pass
    through this cleanly into a valid slug.
    """
    return "".join(c if c.isalnum() else "_" for c in component.lower())


async def import_usage_statistics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    response: UsageResponse,
    utility_display_name: str,
    *,
    fresh: bool = False,
) -> None:
    """Push every interval-consumption series in [response] into HA long-term statistics.

    Cumulative meter registers are excluded — see [_is_interval_consumption_series].

    Idempotent on (statistic_id, hour) — re-importing a previously-imported hour with the same
    value is a no-op, so the coordinator can pull overlapping windows on every poll without
    worrying about duplicates. A re-fetched hour whose value has *changed* restates the usage
    statistic from that hour on — see [_restated_rows].

    ``fresh=True`` means "the store was just cleared; import from a zero baseline". It skips
    the per-series resume-point read entirely. That read (``get_last_statistics``) is what a
    rebuild raced against: if it observed the pre-clear cursor, every reading in the re-fetched
    full-history feed looked "already imported" and got skipped, importing nothing. On a
    rebuild there is by definition no prior data to resume from, so reading it is both
    unnecessary and the source of the race — bypass it.
    """
    for up in response.usage_points:
        imported_any = False
        # Keyed by (flow direction, commodity), which is what [statistic_id_for_series] keys on:
        # every series in one group lands in the same statistic and has to be accumulated as one.
        by_target: dict[tuple[str, str | None], list[MeterReadingSeries]] = {}
        for series in up.series:
            if not _is_interval_consumption_series(up, series):
                _warn_once(
                    f"{entry.entry_id}:{up.usage_point_id}:{series.meter_reading_id}:cumulative",
                    "Skipping meter reading %s on usage point %s: accumulation behaviour %s is a "
                    "cumulative meter register, not per-interval consumption — adding its values "
                    "to the usage statistic would report the whole meter total as one interval's "
                    "consumption",
                    series.meter_reading_id,
                    up.usage_point_id,
                    series.reading_type.accumulation_behaviour,
                )
                continue
            if _is_superseded_by_unit(up, series):
                _warn_once(
                    f"{entry.entry_id}:{up.usage_point_id}:{series.meter_reading_id}:unit-clash",
                    "Skipping meter reading %s on usage point %s: it reports the same %s "
                    "consumption as another series, in a different unit (%s). One statistic "
                    "can only hold one unit, so the other series is imported instead",
                    series.meter_reading_id,
                    up.usage_point_id,
                    _series_commodity(up, series) or "usage",
                    series.reading_type.unit_of_measure,
                )
                continue
            target = (series.reading_type.flow_direction, _series_commodity(up, series))
            by_target.setdefault(target, []).append(series)
        for group in by_target.values():
            # Call first, then OR — `or` short-circuits, and every group must be imported.
            imported = await _import_series_group(
                hass, entry, up, group, utility_display_name, fresh=fresh
            )
            imported_any = imported or imported_any
        if up.series and not imported_any:
            # Nothing on this usage point could be represented. Since a register is now only
            # excluded when a same-flow sibling supersedes it (see
            # [_is_interval_consumption_series]), reaching here means every series carried a
            # unit we have no HA mapping for. The usage point contributes no energy at all —
            # loud, because the symptom is an empty Energy dashboard.
            _LOGGER.error(
                "Usage point %s has no importable consumption series — all %d were skipped "
                "(accumulation behaviours: %s; units: %s). No energy statistics will be written "
                "for it; please report this feed at %s",
                up.usage_point_id,
                len(up.series),
                ", ".join(sorted({s.reading_type.accumulation_behaviour for s in up.series})),
                ", ".join(sorted({s.reading_type.unit_of_measure for s in up.series})),
                "https://github.com/rocketraman/open-green-button-homeassistant/issues",
            )

    # Cost is written after usage, in a second pass. A monthly UsageSummary arrives long after its
    # billing period (Burlington publishes it ~2-3 weeks later), so the period's usage is NOT in
    # this response — it's already in the recorder. [_import_cost_summaries] reads it back to
    # distribute the bill, so the usage writes above must be committed first. Block once here; on a
    # fresh rebuild the period's usage was written moments ago and would otherwise not be visible.
    needs_recorder_flush = any(
        not _has_interval_cost(up) and up.summaries for up in response.usage_points
    )

    if needs_recorder_flush and hass.state is not CoreState.running:
        # DEADLOCK GUARD — do not await the recorder before HA has started.
        #
        # `Recorder._run()` blocks in `_wait_startup_or_shutdown()` until HOMEASSISTANT_STARTED
        # and only then enters `_run_event_loop()`, which is what drains the queue. Our
        # `async_block_till_done()` queues a SynchronizeTask and awaits it, so before start it can
        # never complete. HA in turn doesn't fire STARTED until config-entry setup returns — and
        # this runs inside `async_config_entry_first_refresh()`. That's a genuine deadlock, broken
        # only by HA's SLOW_SETUP_MAX_WAIT (300s) cancelling the setup task:
        #   "Setup of config entry '<title>' for greenbutton integration cancelled"
        # which leaves the entry in SETUP_ERROR with no retry until the next restart.
        #
        # So defer the whole cost pass to just after start instead, where the block is safe.
        # `async_at_started` fires on STARTED (or immediately if we somehow race into `running`),
        # and its unsubscribe is tied to the entry so an unload cancels a still-pending pass.
        async def _deferred_cost_pass(_hass: HomeAssistant) -> None:
            _LOGGER.debug(
                "Running deferred cost import for entry %s (HA has started)", entry.entry_id
            )
            await get_instance(hass).async_block_till_done()
            await _import_costs(hass, entry, response, utility_display_name, fresh=fresh)

        _LOGGER.debug(
            "HA is %s, not running — deferring cost import for entry %s until after startup",
            hass.state,
            entry.entry_id,
        )
        entry.async_on_unload(async_at_started(hass, _deferred_cost_pass))
        return

    if needs_recorder_flush:
        await get_instance(hass).async_block_till_done()
    await _import_costs(hass, entry, response, utility_display_name, fresh=fresh)


async def _import_costs(
    hass: HomeAssistant,
    entry: ConfigEntry,
    response: UsageResponse,
    utility_display_name: str,
    *,
    fresh: bool = False,
) -> None:
    """Second-pass cost import for every usage point in [response].

    Split out of [import_usage_statistics] so it can also run deferred, after
    EVENT_HOMEASSISTANT_STARTED — see the deadlock guard there. Assumes the usage writes it
    depends on have already been flushed to the recorder by the caller.
    """
    for up in response.usage_points:
        # Prefer per-interval <cost> — utilities like savagedata/Milton itemize actual hourly cost,
        # which is more accurate and is self-contained on the reading. Fall back to distributing a
        # monthly UsageSummary total over the period's recorded usage for utilities (Burlington)
        # that only bill via a summary.
        if _has_interval_cost(up):
            await _import_cost_from_readings(hass, entry, up, utility_display_name, fresh=fresh)
        else:
            await _import_cost_summaries(hass, entry, up, utility_display_name, fresh=fresh)


# ESPI AccumulationKind values whose readings are a running meter register (a total since the
# meter was installed / last reset), NOT the quantity consumed during the interval. Named per
# [espi._accumulation], which maps the full NAESB enum so nothing cumulative hides in "OTHER".
#
# Deliberately a *blacklist*: an accumulation behaviour we don't recognize keeps importing as it
# always has. The whitelist alternative ("import only DELTA_DATA") silently drops any series whose
# behaviour is missing, unmapped, or merely unusual — an empty Energy dashboard with nothing above
# DEBUG to explain it. `INDICATING` and `LATCHING_QUANTITY` are arguably register-like too, but no
# feed in scope emits them and misclassifying them would drop real data; revisit with a real
# sample.
_CUMULATIVE_ACCUMULATION = frozenset(
    {
        "BULK_QUANTITY",  # ESPI 1 — the daily register snapshot Milton Hydro publishes
        "CONTINUOUS_CUMULATIVE",  # ESPI 2
        "CUMULATIVE",  # ESPI 3
    }
)

# Seconds of hourly coverage we forgive before calling an hour partial. ESPI durations use an
# inclusive end (a 29-day billing period is 2505599s), so the final hour of any spread reading is
# one second short of full. See [_drop_incomplete_trailing_hour].
_HOUR_COVERAGE_SLACK = 1

# How much coarser than the finest reading covering an hour another reading may be and still be
# summed with it rather than superseded by it. See [_hourly_totals].
_PEER_RESOLUTION_FACTOR = 2

# Keys already logged at WARNING by [_warn_once], so a permanent condition doesn't repeat the
# warning on every poll. Module-level (not per-entry) and never pruned: it holds a handful of
# short strings for the lifetime of the process, and a full HA restart re-arms every warning.
_WARNED_ONCE: set[str] = set()


def _warn_once(key: str, msg: str, *args: object) -> None:
    """Log at WARNING the first time [key] is seen this run, at DEBUG every time after.

    Skipping a series is a data-loss event and has to be visible — DEBUG-only was how the
    unit-mapping skip stayed invisible. But the conditions we skip on are properties of the
    utility's feed, so they recur on every poll; warning each time would be log spam for the
    rest of the entry's life. First one loud, the rest quiet.
    """
    if key in _WARNED_ONCE:
        _LOGGER.debug(msg, *args)
        return
    _WARNED_ONCE.add(key)
    _LOGGER.warning(msg, *args)


def _is_interval_consumption_series(up: UsagePoint, series: MeterReadingSeries) -> bool:
    """True when a series' readings are per-interval quantities we can sum into a statistic.

    False for cumulative meter registers. Milton Hydro publishes an hourly ``DELTA_DATA``
    consumption series *and* a daily ``BULK_QUANTITY`` register snapshot for the same meter,
    both FORWARD — so both map to one [statistic_id_for_series] and the register's
    meter-lifetime total was being added to the hourly running sum, reporting an enormous
    false spike (issue #6).

    The accumulation behaviour ALONE can't make that call, because utilities mislabel it.
    Consumers Energy (via UtilityAPI) publishes one reading per billing period — genuine
    per-period consumption, values that rise and fall month to month (446–1246 kWh) — tagged
    ``BULK_QUANTITY``. Excluding on the name alone dropped 100% of that feed, left an empty
    Energy dashboard, and (via [response_has_cumulative_series]) triggered a repair rebuild
    that purged the account's existing rows and re-imported nothing.

    So a cumulative-named series is only excluded when this UsagePoint also carries a
    non-cumulative series of the *same flow direction* — which is precisely the statistic_id
    collision that motivated #6, and a strictly better source to resolve it in favour of. With
    no such sibling the series is everything the utility publishes, and importing it is the
    only way the account gets any energy at all. Flow direction matters: a FORWARD register
    alongside a REVERSE delta series is not a collision, and excluding it would drop the only
    consumption data there is.

    Testing the values for monotonicity instead does NOT work here: each Consumers Energy
    MeterReading holds exactly one reading, and a one-element series is trivially
    non-decreasing, so every one of them would still read as a register.
    """
    if series.reading_type.accumulation_behaviour not in _CUMULATIVE_ACCUMULATION:
        return True
    return not any(
        other.reading_type.accumulation_behaviour not in _CUMULATIVE_ACCUMULATION
        and other.reading_type.flow_direction == series.reading_type.flow_direction
        for other in up.series
    )


_COMMODITY_BY_READING_TYPE = {
    "ELECTRICITY_SECONDARY_METERED": COMMODITY_ELECTRICITY,
    "ELECTRICITY_PRIMARY_METERED": COMMODITY_ELECTRICITY,
    "ELECTRICITY_TRANSMISSION_METERED": COMMODITY_ELECTRICITY,
    "NATURAL_GAS": COMMODITY_GAS,
    "PROPANE": COMMODITY_GAS,
    "WATER": COMMODITY_WATER,
    "NONPOTABLE_WATER": COMMODITY_WATER,
}
_COMMODITY_BY_SERVICE_KIND = {
    "ELECTRICITY": COMMODITY_ELECTRICITY,
    "GAS": COMMODITY_GAS,
    "WATER": COMMODITY_WATER,
}
# Units only one commodity is ever metered in. Volumes other than gallons/litres are deliberately
# absent: a cubic metre or cubic foot is as likely gas as water.
_COMMODITY_BY_UNIT = {
    "THERMS": COMMODITY_GAS,
    "BTU": COMMODITY_GAS,
    "US_GALLONS": COMMODITY_WATER,
    "IMPERIAL_GALLONS": COMMODITY_WATER,
    "LITRES": COMMODITY_WATER,
}


def _series_commodity(up: UsagePoint, series: MeterReadingSeries) -> str | None:
    """What [series] measures: electricity, gas, water — or None when the feed doesn't say.

    Most specific statement wins: the ReadingType's own ``commodity``, then the UsagePoint's
    ``ServiceCategory``, then the unit where it is unambiguous. Feeds routinely omit the first
    (and some the second), which is why one source isn't enough. None keeps the series on the
    bare, pre-gas/water statistic id rather than guessing.
    """
    return (
        _COMMODITY_BY_READING_TYPE.get(series.reading_type.commodity)
        or _COMMODITY_BY_SERVICE_KIND.get(up.service_kind.upper())
        or _COMMODITY_BY_UNIT.get(series.reading_type.unit_of_measure)
    )


def _is_superseded_by_unit(up: UsagePoint, series: MeterReadingSeries) -> bool:
    """True when [series] shares a statistic with a sibling in a different unit, and loses.

    A gas meter may publish the same consumption twice — once as a volume (ft³) and once as
    billed energy (therms). Both are FORWARD gas, so both map to one [statistic_id_for_series];
    summing them would add cubic feet to kilowatt-hours. The statistic takes one unit, chosen
    from the series' ReadingTypes alone so the choice can't flip between polls: energy before
    volume (it is what's billed, and what compares against electricity), then by unit name.

    Series with no HA unit never win or lose here — [_import_series_group] skips those by
    itself.
    """
    mapping = _UNIT_MAP.get(series.reading_type.unit_of_measure)
    if mapping is None:
        return False
    commodity = _series_commodity(up, series)
    rivals = {
        _UNIT_MAP[other.reading_type.unit_of_measure][:2]
        for other in up.series
        if other.reading_type.unit_of_measure in _UNIT_MAP
        and other.reading_type.flow_direction == series.reading_type.flow_direction
        and _series_commodity(up, other) == commodity
        and _is_interval_consumption_series(up, other)
    }
    # Sort key: "energy" < "volume", then the unit string.
    winner = min(rivals | {mapping[:2]}, key=lambda unit_and_class: unit_and_class[::-1])
    return mapping[:2] != winner


def _forward_interval_series(up: UsagePoint) -> list[MeterReadingSeries]:
    """The FORWARD per-interval consumption series on this UsagePoint — the basis for cost.

    Cost is about what was consumed, so REVERSE (solar export) is out, and so are cumulative
    registers: they aren't billed intervals, and Milton's carries a ``cost=0`` placeholder that
    used to masquerade as real per-interval pricing (issue #7).
    """
    return [
        s
        for s in up.series
        if s.reading_type.flow_direction == "FORWARD" and _is_interval_consumption_series(up, s)
    ]


def _has_interval_cost(up: UsagePoint) -> bool:
    """True when this UsagePoint's interval-consumption readings carry per-interval cost.

    Scoped to [_forward_interval_series]. Milton Hydro attaches ``cost=0`` to its daily
    ``BULK_QUANTITY`` register while billing through ``UsageSummary``; testing every FORWARD
    reading let that placeholder select the per-interval path and write an all-zero cost
    statistic, suppressing the real (non-zero) summary entirely (issue #7).

    Deliberately still ``cost is not None`` rather than ``cost != 0``: a series that genuinely
    itemizes cost may have legitimately zero hours, and because this decision is remade on every
    poll, a "must be non-zero" test would flip a trailing all-zero window onto the summary path
    and mix summary-distributed rows into a per-interval cost statistic. Restricting *which*
    series are consulted fixes #7 on its own; see the issue for the series-level persistence
    that would make the choice stable across polls.
    """
    return any(r.cost is not None for s in _forward_interval_series(up) for r in s.readings)


async def _import_series_group(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    group: list[MeterReadingSeries],
    utility_display_name: str,
    *,
    fresh: bool = False,
) -> bool:
    """Import every series of one flow direction on [up] into the single statistic they share.

    Returns False only when none of them has a unit with an HA mapping. The return value feeds
    the "this usage point imported nothing" check in [import_usage_statistics], so it reports
    *representability*, not whether rows were actually written: an empty or fully
    stale-filtered group is a normal quiet poll, not a misconfigured feed, and must not trip
    that error.

    The series are merged BEFORE accumulating, into one running sum. Importing them one at a
    time — as this used to — gave each its own: on a rebuild every series restarted from zero,
    so the stored sum fell by a whole segment's total at each boundary (a large negative bar in
    the Energy dashboard), and on an ordinary poll a series older than one already written was
    dropped whole by the stale-window guard. A utility is free to publish one meter's history
    as many MeterReadings; UtilityAPI does (issue #21).
    """
    statistic_id = statistic_id_for_series(
        entry.entry_id,
        statistic_usage_point_id(entry, up.usage_point_id),
        group[0].reading_type.flow_direction,
        _series_commodity(up, group[0]),
    )
    # No unit can clash here: [_is_superseded_by_unit] has already left one per statistic.
    representable: list[MeterReadingSeries] = []
    for series in group:
        if _ha_unit_for(series.reading_type) is None:
            _warn_once(
                f"{statistic_id}:{series.reading_type.unit_of_measure}:no-unit",
                "Skipping series %s: no HA unit mapping for %s/%s — its readings will not "
                "appear in the Energy dashboard",
                statistic_id,
                series.reading_type.commodity,
                series.reading_type.unit_of_measure,
            )
            continue
        representable.append(series)
    if not representable:
        return False
    if not any(series.readings for series in representable):
        return True  # Nothing to write; keeps logs quiet on the test-lab empty-account case.
    series = representable[0]
    unit = _ha_unit_for(series.reading_type)

    metadata: StatisticMetaData = {
        "has_mean": False,
        "has_sum": True,
        "name": _stat_display_name(utility_display_name, up, series),
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
        "unit_class": _ha_unit_class_for(series.reading_type),
    }
    # New typed field added in HA core ≥ 2025.6; mean_type replaces has_mean. We keep
    # has_mean for compatibility with HA installs older than that. StatisticMeanType.NONE
    # is the correct value for energy/volume statistics (we only carry `sum`, no mean).
    if _MEAN_TYPE_NONE is not None:
        metadata["mean_type"] = _MEAN_TYPE_NONE

    resume_from_sum, resume_after_epoch = (
        (0.0, None) if fresh else await _resume_point(hass, statistic_id)
    )

    by_hour, covered_seconds, measured = _hourly_totals(representable)
    _drop_incomplete_trailing_hour(by_hour, covered_seconds, statistic_id)

    stats: list[StatisticData] = []
    running = resume_from_sum
    if resume_after_epoch is not None:
        # Hours this poll re-fetched that are already stored. A utility may correct a block
        # after first publishing it, so these are compared with the store instead of assumed
        # identical; only hours the feed fully measured are trusted to overrule it.
        refetched = {
            hour: by_hour[hour]
            for hour in measured
            if hour in by_hour and hour.timestamp() <= resume_after_epoch
        }
        restated = await _restated_rows(hass, statistic_id, refetched)
        if restated is not None:
            stats, running = restated
    for hour in sorted(by_hour):
        # Stale-window guard — HA's statistics machinery already deduplicates on
        # (statistic_id, start), but skipping locally avoids resetting `running` from
        # readings already accounted for in the stored cumulative sum. Compare the *aligned*
        # hour, because that's the granularity the stored row (and hence `_resume_point`) is
        # at: a raw sub-hourly reading start at :15 is > the hour's stored start, so a raw
        # comparison would wave through readings whose hour is already in the sum and add
        # them on top of it, inflating that hour and every hour after it. An already-stored
        # hour whose value changed was handled above, by [_restated_rows].
        if resume_after_epoch is not None and hour.timestamp() <= resume_after_epoch:
            continue
        running += by_hour[hour]
        stats.append(StatisticData(start=hour, state=running, sum=running))

    if not stats:
        return True

    _LOGGER.info(
        "Importing %d statistic rows for %s (resume_from_sum=%.3f)",
        len(stats),
        statistic_id,
        resume_from_sum,
    )
    async_add_external_statistics(hass, metadata, stats)
    return True


async def _restated_rows(
    hass: HomeAssistant,
    statistic_id: str,
    refetched: dict[datetime, float],
) -> tuple[list[StatisticData], float] | None:
    """Rows that restate [statistic_id] where [refetched] disagrees with it, or None if it agrees.

    [refetched] is ``{hour: quantity}`` for hours at or before the statistic's last stored row.
    A utility can publish an IntervalBlock and correct it afterwards — Burlington Hydro served
    one day as 24 zero readings and later, under the same hours, the real ~40 kWh. Skipping
    every already-stored hour froze the zeros in place, a flat day in the Energy dashboard that
    no later poll could repair.

    The statistic is a cumulative sum, so one hour can't be corrected alone: every later row
    carries the old value in its sum. The result therefore runs from the first differing hour
    to the last stored row — re-fetched hours take the feed's value, the rest keep the
    consumption already stored for them — and comes back with the sum it ends on, for the
    caller to continue from. HA upserts on (statistic_id, start), so writing these rows replaces
    the stored ones. A re-fetched hour with no stored row at all (a gap the feed has since
    filled) counts as stored zero, and is inserted.

    Nothing is deleted and nothing outside the statistic is touched; a poll whose overlap
    matches the store — every ordinary poll — writes nothing here.
    """
    if not refetched:
        return None
    since = min(refetched)
    by_id = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        since,
        None,
        {statistic_id},
        "hour",
        None,
        # `change` is each row's sum less its predecessor's — for the first row, less the last
        # row stored before [since], however far back that is.
        {"sum", "change"},
    )
    stored: dict[datetime, tuple[float, float]] = {}
    for row in by_id.get(statistic_id, []):
        total, change = row.get("sum"), row.get("change")
        if total is None or change is None:
            continue
        start = row["start"]
        hour = start if isinstance(start, datetime) else datetime.fromtimestamp(start, tz=UTC)
        stored[hour] = (total, change)
    if not stored:
        return None

    differing = [
        hour
        for hour, quantity in refetched.items()
        if not math.isclose(
            quantity, stored[hour][1] if hour in stored else 0.0, rel_tol=1e-9, abs_tol=1e-6
        )
    ]
    if not differing:
        return None
    first = min(differing)

    # The sum just before [first]: the nearest stored row ahead of it, else whatever preceded
    # the oldest row read back.
    earlier = [hour for hour in stored if hour < first]
    if earlier:
        running = stored[max(earlier)][0]
    else:
        oldest_sum, oldest_change = stored[min(stored)]
        running = oldest_sum - oldest_change
    last_stored_sum = stored[max(stored)][0]

    rows: list[StatisticData] = []
    for hour in sorted({h for h in (*stored, *refetched) if h >= first}):
        running += refetched[hour] if hour in refetched else stored[hour][1]
        rows.append(StatisticData(start=hour, state=running, sum=running))
    _LOGGER.info(
        "Restating %s from %s: %d re-published hour(s) differ from what was stored, moving the "
        "total by %+.3f across %d row(s)",
        statistic_id,
        first.isoformat(),
        len(differing),
        running - last_stored_sum,
        len(rows),
    )
    return rows, running


def _hourly_totals(
    group: list[MeterReadingSeries],
) -> tuple[dict[datetime, float], dict[datetime, int], set[datetime]]:
    """Fold [group] into ``(quantity_by_hour, covered_seconds_by_hour, measured_hours)``.

    Aggregating to the hour *before* accumulating is load-bearing for any utility whose feed
    uses a sub-hourly ``intervalLength`` (15 or 30 minutes — none in scope today, but the ESPI
    schema allows it and nothing upstream rejects it). One StatisticData row per reading would
    emit four rows sharing the same hour-aligned ``start``; HA upserts on
    (statistic_id, start), so three of the four are silently discarded and only the last
    reading's cumulative total survives. That happens to land on the right number within a
    single import, but it leaves the stored row's ``start`` at the hour boundary, which is what
    [_resume_point] reads back — and the sub-hour readings inside that already-imported hour
    then sail past a raw stale-window comparison on the next poll and get added a second time.

    Summing per hour here makes each hour exactly one row, so the row we write and the cursor
    we later resume from describe the same unit of time.

    A reading LONGER than an hour is spread across the hours it spans — see [_hours_spanned].

    Where readings of different granularity cover the same hour, the finer one wins that hour
    and the coarser is ignored there: a bill-level reading alongside hourly intervals describes
    the same consumption twice, and the hourly figure is the measurement while the bill's share
    of that hour is only an even spread. Decided per hour, not per series, so a bill still fills
    whatever part of its period the intervals don't reach. Readings within a factor of
    [_PEER_RESOLUTION_FACTOR] of the finest are its peers and add up as before — sub-hourly
    readings within an hour, or two billing periods of 29 and 31 days meeting at a boundary.

    ``measured_hours`` are the hours whose quantity is a whole measurement: made up only of
    readings no longer than an hour, which between them cover all of it. Only those may overrule
    a value already stored ([_restated_rows]). An hour that is merely a multi-hour reading's even
    share must not replace hourly readings a previous poll imported, and an hour the fetch window
    cut part-way through would restate the stored hour down to the part it happened to carry.
    """
    # hour → [(resolution_seconds, value, seconds_of_this_hour_covered), ...]
    contributions: dict[datetime, list[tuple[int, float, int]]] = {}
    for series in group:
        for reading in series.readings:
            value = _to_ha_units(reading.value, series.reading_type)
            # Everything at or under an hour is the same resolution as far as an hourly
            # statistic can tell, and a degenerate duration is pinned to one hour anyway.
            resolution = max(reading.duration_seconds, 3600)
            for hour, overlap, fraction in _hours_spanned(reading.start, reading.duration_seconds):
                contributions.setdefault(hour, []).append((resolution, value * fraction, overlap))

    by_hour: dict[datetime, float] = {}
    covered_seconds: dict[datetime, int] = {}
    measured: set[datetime] = set()
    for hour, parts in contributions.items():
        ceiling = min(resolution for resolution, _, _ in parts) * _PEER_RESOLUTION_FACTOR
        kept = [part for part in parts if part[0] < ceiling]
        by_hour[hour] = sum(value for _, value, _ in kept)
        covered_seconds[hour] = sum(overlap for _, _, overlap in kept)
        if (
            all(resolution == 3600 for resolution, _, _ in kept)
            and covered_seconds[hour] >= 3600 - _HOUR_COVERAGE_SLACK
        ):
            measured.add(hour)
    return by_hour, covered_seconds, measured


def _hours_spanned(start: datetime, duration_seconds: int) -> list[tuple[datetime, int, float]]:
    """Split ``[start, start+duration)`` into ``(hour_start, seconds_in_hour, fraction)`` triples.

    ``fraction`` is that hour's share of the reading and sums to 1.0 across the result, so
    callers multiply rather than divide — a reading with a degenerate duration can't produce a
    division by zero at a call site.

    Sub-hourly and exactly-hourly readings yield a single pair, so the common path is
    unchanged. The point is readings that span MANY hours: a billing-only utility publishes
    one reading per billing period, and folding that to ``_align_to_hour(start)`` alone wrote
    a whole month's consumption into the period's first hour — an ~900 kWh spike at midnight
    on day one and nothing for the remaining ~700 hours. Consumers Energy (via UtilityAPI)
    publishes exactly this: 23 months of history arrived as 23 statistic rows.

    Distributing evenly across the period does not invent detail the feed doesn't have — the
    hourly shape is unknowable from a monthly total — but it is the only representation HA's
    hourly statistics can carry, it makes daily/monthly dashboard rollups correct, and it
    matches what [_import_cost_summaries] already does with a monthly bill.

    Degenerate durations (0 or negative — a malformed feed) fall back to the single aligned
    hour so the reading is still imported rather than silently vanishing.
    """
    if duration_seconds <= 0:
        return [(_align_to_hour(start), 0, 1.0)]
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    end = start + timedelta(seconds=duration_seconds)
    spans: list[tuple[datetime, int, float]] = []
    hour = _align_to_hour(start)
    while hour < end:
        hour_end = hour + timedelta(hours=1)
        overlap = int((min(end, hour_end) - max(start, hour)).total_seconds())
        if overlap > 0:
            spans.append((hour, overlap, overlap / duration_seconds))
        hour = hour_end
    return spans


def _drop_incomplete_trailing_hour(
    by_hour: dict[datetime, float],
    covered_seconds: dict[datetime, int],
    statistic_id: str,
) -> None:
    """Remove the newest hour from [by_hour] when the feed only covers part of it.

    With a sub-hourly feed a poll routinely lands mid-hour — writing that half-covered hour
    would freeze it at half its real consumption: the aligned stale-window guard correctly
    refuses to add its remaining intervals on the next poll, and [_restated_rows] only revises
    a stored hour from a fetch that covers the whole of it, which the next window need not.

    So hold the partial hour back instead and let a later poll import it whole. Only the
    *trailing* hour is deferred; a mid-series hour short of 3600s is a genuine gap in the feed
    and is imported as-is. An hour with no duration information at all (0s covered) is left
    alone rather than deferred forever. Hourly feeds — every utility in scope today — cover a
    full 3600s per hour and never trip this.

    [_HOUR_COVERAGE_SLACK] accounts for ESPI's inclusive-end durations: a 29-day billing period
    is published as 2505599s, not 2505600, so the last hour of a spread reading lands exactly
    one second short. Without the slack that hour is deferred on every closed billing period —
    and never imported, because a closed period is never re-published — quietly losing ~0.14%
    of each month and leaving the summed hours short of the bill the cost pass divides by.
    """
    if not by_hour:
        return
    last_hour = max(by_hour)
    covered = covered_seconds.get(last_hour, 0)
    if 0 < covered < 3600 - _HOUR_COVERAGE_SLACK:
        _LOGGER.debug(
            "Deferring partial hour %s for %s (%ds of 3600 covered) until the feed completes it",
            last_hour.isoformat(),
            statistic_id,
            covered,
        )
        del by_hour[last_hour]


def _stat_display_name(
    utility_display_name: str,
    up: UsagePoint,
    series: MeterReadingSeries,
) -> str:
    """Friendly label shown in the Energy dashboard picker. UsagePoint UUIDs are unhelpful
    raw; we truncate to 8 chars so users can disambiguate multi-meter setups by suffix."""
    short_id = up.usage_point_id[:8]
    flow = series.reading_type.flow_direction.title()
    # Name a gas or water series for what it measures even when that came from the ReadingType
    # or the unit and the UsagePoint's own ServiceCategory is missing — "Unknown Forward" is no
    # help in the dashboard's gas picker.
    commodity = _series_commodity(up, series)
    kind = commodity if commodity in (COMMODITY_GAS, COMMODITY_WATER) else up.service_kind
    return f"{utility_display_name} · {kind.title()} {flow} ({short_id})"


def _forward_statistic_id(entry: ConfigEntry, up: UsagePoint) -> str:
    """The FORWARD usage statistic this UsagePoint's bills are distributed over.

    Follows the series that [import_usage_statistics] actually writes, so a gas or water meter's
    bill is spread over its own dedicated series. A poll that carries a bill but no series at all
    (or none importable) falls back to the UsagePoint's ServiceCategory.
    """
    commodity = next(
        (
            _series_commodity(up, s)
            for s in _forward_interval_series(up)
            if _ha_unit_for(s.reading_type) is not None and not _is_superseded_by_unit(up, s)
        ),
        _COMMODITY_BY_SERVICE_KIND.get(up.service_kind.upper()),
    )
    return statistic_id_for_series(
        entry.entry_id, statistic_usage_point_id(entry, up.usage_point_id), "FORWARD", commodity
    )


async def _recorded_forward_hours(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    period_start: datetime,
    period_end: datetime,
) -> list[tuple[datetime, float]]:
    """Per-hour FORWARD consumption ``(hour, quantity)`` for ``[period_start, period_end)``.

    The quantity is in whatever unit the usage statistic is stored in — kWh for electricity,
    ft³ or gallons for a gas or water meter. It only ever serves as a weight for spreading a
    bill, so the unit doesn't matter as long as it's one unit throughout.

    Read from the recorder, not the response: a UsageSummary distributed here arrives long after
    its period, whose readings are already imported into the FORWARD usage statistic. We recover
    each hour's kWh as the delta of that statistic's cumulative ``sum`` — querying one hour before
    ``period_start`` so the first in-period hour has a predecessor to diff against.

    Hours with no forward movement (a gap, or a duplicate) are dropped; the result feeds only the
    proportional cost distribution, so approximate weights across a small gap are harmless.
    """
    stat_id = _forward_statistic_id(entry, up)
    by_id = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        period_start - timedelta(hours=1),
        period_end,
        {stat_id},
        "hour",
        None,
        {"sum"},
    )
    hours: list[tuple[datetime, float]] = []
    prev_sum: float | None = None
    for row in by_id.get(stat_id, []):
        total = row.get("sum")
        if total is None:
            continue
        start = row["start"]
        hour = start if isinstance(start, datetime) else datetime.fromtimestamp(start, tz=UTC)
        if prev_sum is not None and period_start <= hour < period_end and total > prev_sum:
            hours.append((hour, total - prev_sum))
        prev_sum = total
    return hours


async def _import_cost_summaries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    utility_display_name: str,
    *,
    fresh: bool = False,
) -> None:
    """Write a cumulative-cost statistic from this UsagePoint's BillingSummary entries.

    HA's Energy dashboard pairs an energy stat with a cost stat at config time, and reads
    them at the *same* time-bucket granularity as the energy stat (hourly). A single-value
    cost stat at billing_period_start would show non-zero for one hour and zero for every
    other hour — which is exactly the "shows as zero" symptom users see.

    Instead, we distribute each billing-period total across the hours within that period
    in proportion to that hour's consumption:

        cost_at_hour_h = period_total_cost × (kwh_h / total_kwh_in_period)

    This gives a per-hour cost that the dashboard aggregates into daily/monthly views
    correctly, and matches the way a utility actually bills (you pay for the energy you
    used, and a higher-consumption hour incurs more of the period's total cost). Real-world
    accuracy depends on whether the utility's pricing is flat or TOU; the test lab is
    flat, while production TOU pricing would want richer cost-detail handling
    (future work).

    The period's usage comes from the **recorder**, not this response: a UsageSummary is
    published weeks after its period closes, so an incremental poll that carries a freshly-
    published summary does NOT carry that period's readings (they were imported long ago). We
    read them back from the usage statistic ([_recorded_forward_hours]) to distribute over.
    This is why a plain published-min poll is enough to keep cost current — no reach-back needed.

    **Overlapping periods: the later bill replaces the earlier one's hours, in full.** Feeds
    routinely carry summaries covering hours another summary already covers. Consecutive bills
    overlap by a meter-read day, because the period is inclusive of both reads — El Paso
    Electric's twelve monthly bills each lap one day over the previous. Feeds also repeat exact
    duplicates across pagination, and some publish a coarse rollup beside the per-bill totals.
    Costing every one of them in full would charge the shared hours twice over.

    So each summary prices every hour of its *own* period, and a summary applied later simply
    overwrites what an earlier one wrote on any hour they share. Ordering is by period start, then
    longest-first, then cheapest-first, so the most recent and most specific statement of an hour's
    cost is the one that survives. Rollups lose every hour a real bill also covers and keep only
    the gaps; exact duplicates rewrite identical values and change nothing.

    The alternative — clipping a later summary's window to the hours nobody has claimed and
    spreading its full total across what's left — was tried and is worse. A bill's ``total_cost``
    is one number with no per-day breakdown, so there is no honest way to subtract the overlap
    day's share from it: the clipped remainder gets charged the whole bill, and every hour in it
    is overpriced by the ratio of what was trimmed. Replacing needs no such guess. It also matches
    what a later bill *means*: when two statements cover the same hour, the more recent one is the
    utility's correction, not a duplicate to be reconciled against the older one.

    Note the replacement applies within an import, not retroactively across polls. An hour costed
    by a poll weeks ago sits behind this statistic's resume point and is skipped rather than
    revised — a cumulative-sum statistic can't have its middle rewritten without restating every
    row after it. A full rebuild (`greenbutton.rebuild_statistics`, or the automatic repair in
    [coordinator.GreenButtonCoordinator._async_migrate_import]) re-imports from a clean slate and
    applies this to the whole history, which is how a feed imported under the old rule gets fixed.

    Skipped when the UsagePoint has no summaries (most utilities only attach UsageSummary
    to accounts they bill; meter-only test profiles often won't), or when the currency code
    isn't one we have an ISO 4217 alpha mapping for.
    """
    if not up.summaries:
        return
    currency_alpha = _iso_4217_alpha(up.summaries[0].currency_numeric_code)
    if currency_alpha is None:
        _LOGGER.debug(
            "Skipping cost stat for usage point %s: currency code %s has no ISO 4217 mapping",
            up.usage_point_id,
            up.summaries[0].currency_numeric_code,
        )
        return

    statistic_id = statistic_id_for_cost(
        entry.entry_id, statistic_usage_point_id(entry, up.usage_point_id)
    )
    metadata: StatisticMetaData = {
        "has_mean": False,
        "has_sum": True,
        "name": (
            f"{utility_display_name} · {up.service_kind.title()} Cost ({up.usage_point_id[:8]})"
        ),
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": currency_alpha,
        # HA's recorder validates `unit_class` against the registered `BaseUnitConverter`
        # families in `util.unit_conversion`. There's no MonetaryConverter (you can't
        # convert CAD ↔ USD via a fixed-ratio table), so anything besides None throws
        # `Unsupported unit_class: '<value>'` at metadata validation. None is the
        # well-formed answer for currency stats — the 2026.11 deprecation warning still
        # fires for it, but the warning isn't a hard error and HA hasn't introduced a
        # monetary class to migrate to yet. Revisit when HA adds one.
        "unit_class": None,
    }
    if _MEAN_TYPE_NONE is not None:
        metadata["mean_type"] = _MEAN_TYPE_NONE

    resume_from_sum, resume_after_epoch = (
        (0.0, None) if fresh else await _resume_point(hass, statistic_id)
    )

    # Periods we couldn't cost for want of usage to spread them over. Collected rather than logged
    # per-period so the summary below can say how many, over what span — "why is there no cost
    # statistic?" is otherwise unanswerable from the log at any level.
    uncostable: list[datetime] = []
    # Each summary prices every hour of its own period, and a later one simply replaces whatever
    # an earlier one put on an hour they share — see the docstring on why replacing beats trimming.
    cost_by_hour: dict[datetime, float] = {}
    for summary in sorted(
        up.summaries,
        key=lambda s: (
            s.billing_period_start,
            -s.billing_period_duration_seconds,
            s.total_cost,
        ),
    ):
        period_cost = summary.total_cost
        if period_cost == 0:
            # Test-lab fixtures often have $0 placeholders — skip rather than emit a
            # cumulative-flat row across an entire month, or blank out a real bill's hours.
            continue
        period_start = summary.billing_period_start
        period_end = period_start + timedelta(seconds=summary.billing_period_duration_seconds)
        # FORWARD consumption for the period, read back from the recorder (see the docstring).
        # REVERSE flow (solar export) isn't part of consumption cost, so the usage stat we read
        # here — the FORWARD series — is the right basis.
        in_period = await _recorded_forward_hours(hass, entry, up, period_start, period_end)
        total_period_kwh = sum(k for (_, k) in in_period)
        if total_period_kwh <= 0:
            # No recorded usage for this period yet (e.g. a bill whose period predates our usage
            # backfill) — nothing to distribute the cost over; skip until/if the usage exists.
            uncostable.append(period_start)
            _LOGGER.debug(
                "No recorded usage in %s → %s for usage point %s, so its %.2f bill can't be "
                "distributed — skipping this period",
                period_start.isoformat(),
                period_end.isoformat(),
                up.usage_point_id,
                period_cost,
            )
            continue

        # Per-hour cost = per-kWh TOU rate (zero if not a TOU bucket) + per-kWh non-TOU rate
        # for everything else (Delivery, Global Adjustment, rebates, etc.). Sum of all hourly
        # costs across the period equals the period's total bill — verified by construction.
        cost_by_hour.update(_cost_distribution_for_period(summary, in_period, total_period_kwh))

    # One pass, in time order, so the cumulative sum is monotonic no matter what order the feed
    # listed its summaries in.
    stats: list[StatisticData] = []
    running = resume_from_sum
    for hour_start in sorted(cost_by_hour):
        if resume_after_epoch is not None and hour_start.timestamp() <= resume_after_epoch:
            continue
        running += cost_by_hour[hour_start]
        stats.append(StatisticData(start=hour_start, state=running, sum=running))

    if not stats:
        # No cost statistic gets registered at all in this case, so it's simply absent from the
        # Energy dashboard's picker with nothing anywhere to say why. Name the reason: on a fresh
        # account it's normally that the utility publishes bills going back further than the usage
        # it will give us, which resolves itself as usage accumulates.
        if uncostable:
            _LOGGER.info(
                "No cost statistic for usage point %s: %d billing period(s) between %s and %s "
                "were published, but this entry has imported no usage inside any of them, so "
                "there is nothing to distribute the bills across. Cost will appear once usage "
                "exists for a billed period",
                up.usage_point_id,
                len(uncostable),
                min(uncostable).date().isoformat(),
                max(uncostable).date().isoformat(),
            )
        else:
            _LOGGER.debug(
                "No cost rows to write for usage point %s (every billing period was either a "
                "zero-cost placeholder or already imported)",
                up.usage_point_id,
            )
        return

    _LOGGER.info(
        "Importing %d cost rows for %s in %s (resume_from_sum=%.2f)",
        len(stats),
        statistic_id,
        currency_alpha,
        resume_from_sum,
    )
    async_add_external_statistics(hass, metadata, stats)


async def _import_cost_from_readings(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    utility_display_name: str,
    *,
    fresh: bool = False,
) -> None:
    """Write a cumulative-cost statistic from per-interval `<cost>` on the FORWARD readings.

    Utilities like savagedata itemize the actual cost on every IntervalReading — more accurate
    and finer-grained than distributing a monthly UsageSummary total (and it works when the
    summary only carries an "Amount Due" subtotal, which our summary path deliberately drops).
    Costs are summed per hour across the UsagePoint's FORWARD interval-consumption series
    (multiple meters roll up into one bill), then accumulated into the same cost stat the Energy
    dashboard reads. Cumulative registers are excluded — see [_forward_interval_series].

    A re-fetched hour whose cost differs from the stored one restates the statistic from that
    hour on, exactly as usage does — see [_restated_rows]. The cost rides on the same reading as
    the consumption, so a block the utility corrects brings its corrected cost with it.
    """
    forward_series = _forward_interval_series(up)
    currency_code = next(
        (
            s.reading_type.currency_numeric_code
            for s in forward_series
            if s.reading_type.currency_numeric_code is not None
        ),
        None,
    )
    currency_alpha = _iso_4217_alpha(currency_code)
    if currency_alpha is None:
        _LOGGER.debug(
            "Skipping per-interval cost for %s: currency %s has no ISO 4217 mapping",
            up.usage_point_id,
            currency_code,
        )
        return

    cost_by_hour: dict[datetime, float] = {}
    # What makes an hour's cost a whole measurement, fit to overrule a stored one: every costed
    # reading in it is an hour or shorter, and together they cover it. Same rule, for the same
    # reasons, as the measured hours of [_hourly_totals].
    covered_seconds: dict[datetime, int] = {}
    spread: set[datetime] = set()
    for series in forward_series:
        for reading in series.readings:
            if reading.cost is None:
                continue
            # Spread across the reading's span for the same reason usage is — see
            # [_hours_spanned]. A billing-only feed carries the whole period's cost on one
            # reading, and pinning it to the first hour puts a month's bill in a single hour
            # of the cost statistic while the usage it pairs with is spread across the period.
            for hour, overlap, fraction in _hours_spanned(reading.start, reading.duration_seconds):
                cost_by_hour[hour] = cost_by_hour.get(hour, 0.0) + reading.cost * fraction
                covered_seconds[hour] = covered_seconds.get(hour, 0) + overlap
                if reading.duration_seconds > 3600:
                    spread.add(hour)
    if not cost_by_hour:
        return

    statistic_id = statistic_id_for_cost(
        entry.entry_id, statistic_usage_point_id(entry, up.usage_point_id)
    )
    metadata: StatisticMetaData = {
        "has_mean": False,
        "has_sum": True,
        "name": (
            f"{utility_display_name} · {up.service_kind.title()} Cost ({up.usage_point_id[:8]})"
        ),
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": currency_alpha,
        "unit_class": None,  # no monetary converter in HA; see _import_cost_summaries.
    }
    if _MEAN_TYPE_NONE is not None:
        metadata["mean_type"] = _MEAN_TYPE_NONE

    resume_from_sum, resume_after_epoch = (
        (0.0, None) if fresh else await _resume_point(hass, statistic_id)
    )
    stats: list[StatisticData] = []
    running = resume_from_sum
    if resume_after_epoch is not None:
        refetched = {
            hour: cost
            for hour, cost in cost_by_hour.items()
            if hour.timestamp() <= resume_after_epoch
            and hour not in spread
            and covered_seconds[hour] >= 3600 - _HOUR_COVERAGE_SLACK
        }
        restated = await _restated_rows(hass, statistic_id, refetched)
        if restated is not None:
            stats, running = restated
    for hour in sorted(cost_by_hour):
        if resume_after_epoch is not None and hour.timestamp() <= resume_after_epoch:
            continue
        running += cost_by_hour[hour]
        stats.append(StatisticData(start=hour, state=running, sum=running))
    if not stats:
        return
    _LOGGER.info(
        "Importing %d per-interval cost rows for %s in %s",
        len(stats),
        statistic_id,
        currency_alpha,
    )
    async_add_external_statistics(hass, metadata, stats)


def _cost_distribution_for_period(
    summary: BillingSummary,
    in_period: list[tuple[datetime, float]],
    total_period_kwh: float,
) -> dict[datetime, float]:
    """Return per-hour cost for the readings in [in_period].

    When the summary's detail items include TOU line items (Off-Peak, Mid-Peak, On-Peak),
    each TOU portion is distributed across the hours of that bucket at its own rate, and
    everything else (Delivery, taxes, rebates, plus billLastPeriod headroom) is distributed
    flat per kWh. When no TOU line items are present, the whole period total goes flat
    per kWh — same result as the pre-TOU implementation.

    Conservation invariant: ``sum(returned.values()) == summary.total_cost`` (to within
    floating-point rounding), provided every reading lands in a non-empty bucket. If a
    bucket has no readings in this period (e.g. test data spans only weekdays so the
    weekend off-peak bucket is empty), that bucket's spend is absorbed into the flat
    component instead — avoids "lost" dollars in the dashboard.
    """
    tou_cost_by_bucket: dict[str, float] = {}
    for detail in summary.cost_details:
        bucket = cost_detail_tou_bucket(detail.note)
        if bucket is not None:
            tou_cost_by_bucket[bucket] = tou_cost_by_bucket.get(bucket, 0.0) + detail.amount

    # Per-bucket kWh in this period (drives the TOU-rate denominator).
    kwh_by_bucket: dict[str, float] = {}
    bucket_of: dict[datetime, str] = {}
    for hour_start, kwh in in_period:
        b = ontario_tou_bucket(hour_start)
        bucket_of[hour_start] = b
        kwh_by_bucket[b] = kwh_by_bucket.get(b, 0.0) + kwh

    # If a TOU line item is for a bucket the period has no readings in, fold its spend into
    # the flat-rate residual rather than dropping it.
    tou_distributed: float = 0.0
    bucket_rates: dict[str, float] = {}
    for bucket, cost in tou_cost_by_bucket.items():
        bucket_kwh = kwh_by_bucket.get(bucket, 0.0)
        if bucket_kwh > 0:
            bucket_rates[bucket] = cost / bucket_kwh
            tou_distributed += cost

    flat_residual = summary.total_cost - tou_distributed
    flat_rate = (flat_residual / total_period_kwh) if total_period_kwh > 0 else 0.0

    out: dict[datetime, float] = {}
    for hour_start, kwh in in_period:
        tou_rate = bucket_rates.get(bucket_of[hour_start], 0.0)
        out[hour_start] = kwh * (tou_rate + flat_rate)
    return out


# Just the codes we expect to see from utilities currently in scope. Expand as we onboard
# more — leaving an unknown code unmapped is safe (we skip the cost stat rather than emit
# one with a unit HA can't display).
_ISO_4217_ALPHA: dict[int, str] = {
    124: "CAD",  # Canada — Ontario and other Canadian utilities
    840: "USD",  # United States
    978: "EUR",  # Eurozone
    826: "GBP",  # United Kingdom
    36: "AUD",  # Australia
    554: "NZD",  # New Zealand
}


def _iso_4217_alpha(numeric_code: int | None) -> str | None:
    """Map an ISO 4217 numeric currency code to its alpha-3 string (e.g. 124 → ``CAD``)."""
    if numeric_code is None:
        return None
    return _ISO_4217_ALPHA.get(numeric_code)


# US therm, the one North American gas utilities bill in: 105,480,400 J.
_KWH_PER_THERM = 105_480_400 / 3_600_000
# International Table BTU: 1055.05585262 J.
_KWH_PER_BTU = 1055.05585262 / 3_600_000

# Normalized ESPI unit → (HA unit, HA unit_class, factor from the ESPI base unit to the HA unit).
#
# The unit class is the string on the matching `BaseUnitConverter` subclass's `UNIT_CLASS` in
# `util.unit_conversion`; the recorder uses it to know which units the statistic can be shown in,
# and omitting it is a deprecation that becomes a hard requirement in HA 2026.11.
#
# Every energy unit is stored as kWh. HA has no therm or BTU unit, and its Energy dashboard takes
# gas either as a volume or as energy, so kWh is the only way a therm-billed gas meter can appear
# there at all. Volumes keep the utility's own unit where HA has it (the UI converts for display);
# imperial gallons, which HA lacks — its `gal` is the US gallon — are stored as litres.
_UNIT_MAP: dict[str, tuple[str, str, float]] = {
    "WATT_HOURS": (UnitOfEnergy.KILO_WATT_HOUR, "energy", 1 / 1000),
    "JOULES": (UnitOfEnergy.KILO_WATT_HOUR, "energy", 1 / 3_600_000),
    "THERMS": (UnitOfEnergy.KILO_WATT_HOUR, "energy", _KWH_PER_THERM),
    "BTU": (UnitOfEnergy.KILO_WATT_HOUR, "energy", _KWH_PER_BTU),
    "CUBIC_METERS": (UnitOfVolume.CUBIC_METERS, "volume", 1.0),
    "CUBIC_FEET": (UnitOfVolume.CUBIC_FEET, "volume", 1.0),
    "US_GALLONS": (UnitOfVolume.GALLONS, "volume", 1.0),
    "IMPERIAL_GALLONS": (UnitOfVolume.LITERS, "volume", 4.54609),
    "LITRES": (UnitOfVolume.LITERS, "volume", 1.0),
}


def _ha_unit_for(reading_type: NormalizedReadingType) -> str | None:
    """Map the normalized unit name to the HA unit the Energy dashboard expects.

    Returns None for units we don't yet have a domain mapping for — the caller skips writing
    rather than guessing and confusing the dashboard.
    """
    mapping = _UNIT_MAP.get(reading_type.unit_of_measure)
    return mapping[0] if mapping else None


def _ha_unit_class_for(reading_type: NormalizedReadingType) -> str | None:
    """Return the HA `unit_class` matching the series's normalized unit — see [_UNIT_MAP]."""
    mapping = _UNIT_MAP.get(reading_type.unit_of_measure)
    return mapping[1] if mapping else None


def _to_ha_units(value: float, reading_type: NormalizedReadingType) -> float:
    """Apply the unit conversion implied by [_ha_unit_for].

    ``value`` already arrives in the ReadingType's base unit (Wh, ft³, therms) —
    [espi._assemble] scales each reading by the ESPI ``powerOfTenMultiplier`` as it builds the
    UsageReading, so this must not apply it a second time.
    """
    mapping = _UNIT_MAP.get(reading_type.unit_of_measure)
    return value * mapping[2] if mapping else value


def _align_to_hour(start: datetime) -> datetime:
    """HA external statistics require hour-aligned UTC timestamps. ESPI hourly readings
    already align in practice, but defensively zero out sub-hour fields so a buggy utility
    can't poison the statistics store with off-boundary rows."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return start.replace(minute=0, second=0, microsecond=0).astimezone(UTC)


async def _resume_point(hass: HomeAssistant, statistic_id: str) -> tuple[float, float | None]:
    """Return (last_sum, last_start_epoch) for this statistic_id, or (0.0, None) if no prior."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        statistic_id,
        True,
        {"sum", "start"},
    )
    if not last or statistic_id not in last or not last[statistic_id]:
        return 0.0, None
    row = last[statistic_id][0]
    return float(row.get("sum") or 0.0), row.get("start")
