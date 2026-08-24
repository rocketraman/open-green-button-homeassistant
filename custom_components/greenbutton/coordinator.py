"""DataUpdateCoordinator that pulls usage from the proxy and writes HA statistics.

This integration has no sensor entities — its only job is to backfill / append to HA's
long-term statistics so the data shows up in the Energy dashboard. The coordinator wraps
that fetch + write into HA's standard refresh/retry/reauth lifecycle.

Behaviour summary:
  - Polls the proxy at the per-utility cadence from the claim response (``pollIntervalSeconds``,
    default daily), falling back to ``DEFAULT_SCAN_INTERVAL``. One tight `published-min` window
    per poll: since `published-min` filters by publication date, a freshly-published bill (a
    monthly UsageSummary) is caught naturally, and the cost importer distributes it over the
    period's already-recorded usage — no reach-back needed.
  - On ``OpenGbAuthExpiredError`` → raises ``ConfigEntryAuthFailed``, which HA turns into
    a reauth notification + flow.
  - On any other ``OpenGbApiError`` / network error → raises ``UpdateFailed`` (transient).
  - If the proxy returns ``new_credentials``, the new blob + proxy_token are written back
    into the config entry *before* the stats import — that way a failed import doesn't lose
    the rotated token.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    NewCredentials,
    OpenGbApiError,
    OpenGbAuthExpiredError,
    OpenGbDataPendingError,
    OpenGbPermanentError,
    UsageResponse,
)
from .const import (
    BACKGROUND_LOAD_ISSUE_URL,
    CONF_CUSTOMER_ACCOUNT_ID,
    CONF_CUSTOMER_ADDRESS,
    CONF_CUSTOMER_LABEL,
    CONF_EMPTY_SINCE,
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_IMPORT_LOGIC_REVISION,
    CONF_INITIAL_HISTORY_SECONDS,
    CONF_LAST_FETCHED_AT,
    CONF_PENDING_PUBLISHED_MAX,
    CONF_PENDING_PUBLISHED_MIN,
    CONF_PENDING_SINCE,
    CONF_POLL_INTERVAL_SECONDS,
    CONF_PROXY_TOKEN,
    CONF_USAGE_POINT_CURSORS,
    CONF_UTILITY_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EMPTY_RETRY_INITIAL,
    IMPORT_LOGIC_REVISION,
    INITIAL_FETCH_LOOKBACK,
    LAST_FETCHED_OVERLAP,
    PENDING_ESCALATE_AFTER,
    PENDING_RETRY_INTERVAL,
    SERVICE_REBUILD_STATISTICS,
)
from .statistics import (
    async_clear_statistics_for_entry,
    async_entry_has_statistics,
    import_usage_statistics,
    response_needs_import_migration,
)
from .storage import xml_cache_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import OpenGbApi

_LOGGER = logging.getLogger(__package__)


def _newest_reading_start(response: UsageResponse) -> datetime | None:
    """Return the latest reading ``start`` across every series, or None if there are none.

    Used to anchor the incremental cursor to the real data frontier (the newest interval we
    actually imported) rather than wall-clock ``now`` — see
    [GreenButtonCoordinator._advance_cursor].
    """
    newest: datetime | None = None
    for up in response.usage_points:
        for series in up.series:
            for reading in series.readings:
                if newest is None or reading.start > newest:
                    newest = reading.start
    return newest


def _newest_reading_start_by_usage_point(response: UsageResponse) -> dict[str, datetime]:
    """Return the newest reading ``start`` per UsagePoint, skipping ones that carried none.

    The per-meter counterpart of [_newest_reading_start]. A poll routinely returns data for some
    meters and not others (they publish on their own cadences), and a meter absent from this
    mapping must keep the cursor it already had rather than be reset or dropped.
    """
    newest: dict[str, datetime] = {}
    for up in response.usage_points:
        for series in up.series:
            for reading in series.readings:
                current = newest.get(up.usage_point_id)
                if current is None or reading.start > current:
                    newest[up.usage_point_id] = reading.start
    return newest


def _parse_iso_or_none(raw: object) -> datetime | None:
    """Parse a stored ISO 8601 timestamp, tolerating None / non-str / corrupt values."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _seconds_or_default(raw: object, default: timedelta) -> timedelta:
    """A stored positive-number seconds value as a timedelta, else ``default``.

    Used for the server-supplied poll cadence (`CONF_POLL_INTERVAL_SECONDS`): absent (older server
    / pre-existing entry) or malformed ⇒ the client's local default, so the server stays the single
    source of truth without a second place to configure the window.
    """
    if isinstance(raw, (int, float)) and raw > 0:
        return timedelta(seconds=raw)
    return default


def _write_xml_sync(path: str, data: bytes) -> None:
    """Synchronous file write executed off the event loop via `async_add_executor_job`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Write to a temp file + atomic rename so a half-written cache from a crash mid-write
    # doesn't fool the diagnostics handler into reading garbage.
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class GreenButtonCoordinator(DataUpdateCoordinator[UsageResponse]):
    """Polls /proxy/usage and writes the result to HA statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: OpenGbApi,
        entry: ConfigEntry,
    ) -> None:
        """Build the coordinator bound to an entry."""
        super().__init__(
            hass,
            _LOGGER,
            # Required since HA 2024.10 — without it, async_config_entry_first_refresh() and
            # the reauth-flow plumbing raise ConfigEntryError. See HA dev docs on
            # "passing the ConfigEntry to your DataUpdateCoordinator".
            config_entry=entry,
            name=DOMAIN,
            # Per-utility cadence from the claim response (server is the source of truth);
            # DEFAULT_SCAN_INTERVAL only for entries created before the server exposed it.
            update_interval=_seconds_or_default(
                entry.data.get(CONF_POLL_INTERVAL_SECONDS), DEFAULT_SCAN_INTERVAL
            ),
        )
        self.api = api
        self.entry = entry
        # One-shot flag set by [async_rebuild_statistics] so the next fetch backfills the full
        # initial-history window instead of the incremental slice since `last_fetched_at`.
        # Consumed in [_published_min]; cleared after a successful import.
        self._force_full_history = False
        # Guards [_schedule_deferred_import_migration] against arming the one-time statistics
        # repair twice — two concurrent full-history rebuilds would fight over the same store.
        self._import_migration_armed = False
        # Unsubscribe for the armed fast retry of a deferred (HTTP 202) fetch, if any.
        # See [_schedule_pending_retry].
        self._pending_retry_unsub: Callable[[], None] | None = None

    @property
    def data_pending(self) -> bool:
        """True when the utility is preparing a deferred batch we haven't collected yet.

        Read by `async_setup_entry` to distinguish "this entry can't start" from "this entry is
        fine, its data just isn't ready" — a distinction HA has no built-in vocabulary for.
        """
        return CONF_PENDING_PUBLISHED_MIN in self.entry.data

    async def _async_update_data(self) -> UsageResponse:
        """Fetch, persist rotated credentials, then write statistics."""
        now = datetime.now(UTC)
        published_min, published_max = self._fetch_window(now)

        _LOGGER.info(
            "Fetching usage for entry %s with published-min=%s published-max=%s",
            self.entry.entry_id,
            published_min.isoformat(),
            published_max.isoformat(),
        )

        # Sampled BEFORE the import, while the store still reflects only the previous logic:
        # afterwards we can't tell a first-ever import from one that appended to older rows,
        # and that distinction decides whether a repair rebuild is warranted. Only read when a
        # repair is actually outstanding, so the steady state costs no recorder round-trip.
        import_migration_pending = (
            self.entry.data.get(CONF_IMPORT_LOGIC_REVISION) != IMPORT_LOGIC_REVISION
        )
        had_prior_statistics = import_migration_pending and await async_entry_has_statistics(
            self.hass, self.entry.entry_id
        )

        response = await self._fetch(published_min, published_max)

        total_readings = sum(len(s.readings) for up in response.usage_points for s in up.series)
        _LOGGER.info(
            "Fetched %d usage point(s) with %d total reading(s) for entry %s",
            len(response.usage_points),
            total_readings,
            self.entry.entry_id,
        )

        await import_usage_statistics(
            self.hass,
            self.entry,
            response,
            utility_display_name=self.entry.data.get(CONF_UTILITY_NAME, "Open Green Button"),
        )

        # One-time: give the entry a human-distinguishable title from its customer data. Cheap
        # (guarded to run once) and best-effort — never lets a customer-fetch hiccup fail the poll.
        await self._async_ensure_customer_label()

        # Advance the incremental cursor. Done LAST so a partial failure (stats write throwing)
        # doesn't move it and leave a gap.
        self._advance_cursor(response)
        # Then decide whether this account has anything at all yet — reads the cursor the line
        # above may have just written, so it must follow it.
        self._reconcile_data_availability()
        # A full-history rebuild has now landed; revert to incremental polling.
        self._force_full_history = False

        if import_migration_pending:
            if self.hass.state is CoreState.running:
                # May purge and re-import everything we just wrote, and hands back the rebuilt
                # response so this refresh publishes the repaired data, not the stale slice.
                rebuilt = await self._async_migrate_import(response, had_prior_statistics)
                if rebuilt is not None:
                    return rebuilt
            else:
                # DEADLOCK GUARD — the rebuild awaits `Recorder.async_block_till_done()`, which
                # cannot complete before HA has started (the recorder isn't draining its queue
                # yet) while HA in turn won't start until config-entry setup returns — and the
                # first refresh runs inside that setup. Same trap [statistics] documents for its
                # cost pass; same escape. Defer to just after startup, where blocking is safe.
                self._schedule_deferred_import_migration(response, had_prior_statistics)
        return response

    async def _fetch(self, published_min: datetime, published_max: datetime) -> UsageResponse:
        """Call /proxy/usage for a window and persist any rotated credentials.

        Shared by the normal poll ([_async_update_data]) and [async_rebuild_statistics] so
        the upstream-error → HA-outcome mapping and the credential-rotation write live in one
        place. Raises ``ConfigEntryAuthFailed`` (→ reauth) or ``UpdateFailed`` (→ transient)
        on failure; the stats write is the caller's responsibility.
        """
        # Persist the raw upstream XML to disk only when debug logging is enabled on our
        # domain — keeps the integration's normal memory + disk footprint negligible while
        # giving operators a one-toggle path to capture the bytes for diagnostics. The sink
        # is invoked between read and parse inside `fetch_usage`, then the bytes drop out
        # of scope: no in-memory cache.
        raw_xml_sink = self._make_raw_xml_sink_if_debug()

        try:
            response = await self.api.fetch_usage(
                encrypted_refresh_blob=self.entry.data[CONF_ENCRYPTED_REFRESH_BLOB],
                proxy_token=self.entry.data[CONF_PROXY_TOKEN],
                published_min=published_min,
                published_max=published_max,
                raw_xml_sink=raw_xml_sink,
            )
        except OpenGbAuthExpiredError as err:
            # The refresh itself was rejected, so there's normally nothing to rotate — but
            # persist defensively in case the proxy did surface a new blob. HA turns
            # ConfigEntryAuthFailed into a persistent notification + reauth flow; the user
            # re-authorizes through the existing config flow (updates blob/token).
            self._persist_rotated_credentials(err.new_credentials)
            raise ConfigEntryAuthFailed(str(err)) from err
        except OpenGbDataPendingError as err:
            # The utility is assembling the dataset out-of-band (ESPI async batch). Freeze this
            # exact window so every retry re-asks the identical question — the custodian prepared
            # its batch for the URL we sent, and a window recomputed from `now` would enqueue a
            # new job instead of collecting the finished one (see CONF_PENDING_PUBLISHED_MIN).
            # Then raise a repair issue and fail this refresh. Must be caught BEFORE
            # OpenGbApiError, of which it is a subclass.
            self._persist_rotated_credentials(err.new_credentials)
            # Nothing downstream ever prints this exception: setup logs its own generic line, and
            # HA's coordinator only logs an UpdateFailed message on a success→failure transition —
            # which a 202-before-first-success never produces. So this is the one place the
            # custodian's raw 202 response (status, Location/Retry-After headers, body — forwarded
            # verbatim in the proxy's `message`, see [api.fetch_usage]) can reach the user's log,
            # and that log is the only way to collect it (issues/10). INFO for the first 202 of a
            # window; the every-few-minutes retries repeat it at DEBUG.
            first_deferral = self._pending_window() != (published_min, published_max)
            _LOGGER.log(
                logging.INFO if first_deferral else logging.DEBUG,
                "Entry %s: utility deferred the fetch: %s",
                self.entry.entry_id,
                err,
            )
            self._remember_pending_window(published_min, published_max)
            self._async_create_background_load_issue()
            # Come back in minutes, not at the utility's daily cadence — the batch usually lands
            # within one interval. Ordered AFTER the window freeze so the retry replays it.
            self._schedule_pending_retry()
            if first_deferral:
                # Probe a DIFFERENT batch resource on the same custodian while this one is
                # deferred. The customer feed lives at .../Batch/RetailCustomer/{sub} — same
                # platform, same auth, much smaller payload — so its status answers a question the
                # usage endpoint can't: is this custodian's WHOLE Batch tree asynchronous, or only
                # the big usage export? A 200 here means small resources are served synchronously,
                # which is what deciding how to collect a prepared batch turns on. Until now this
                # was never exercised for a deferring entry: labeling runs after a successful
                # usage fetch, and for these entries there has never been one.
                #
                # First 202 of a window only — not on every five-minute retry — so this costs one
                # extra request per deferral episode, and it is already guarded to stop entirely
                # once a label (or a permanent "none available") is recorded. Nothing it does can
                # propagate: the call swallows every exception internally.
                await self._async_ensure_customer_label()
            raise UpdateFailed(str(err)) from err
        except OpenGbApiError as err:
            # Crucial for one-time refresh tokens (savagedata/OpenIddict): the proxy may have
            # refreshed — rotating and burning our stored refresh token — before the upstream
            # fetch failed. Persist the rotated blob so the next poll retries with the fresh
            # token instead of the dead one, which would otherwise force a spurious reauth.
            self._persist_rotated_credentials(err.new_credentials)
            raise UpdateFailed(str(err)) from err
        except (TimeoutError, ConnectionError) as err:
            raise UpdateFailed(f"network error talking to the proxy: {err}") from err

        self._persist_rotated_credentials(response.new_credentials)
        self._log_window_compliance(response, published_min, published_max)
        # The deferred batch (if there was one) has landed — stop replaying its window and stand
        # the fast retry down, so the next poll goes back to asking for whatever is new.
        self._clear_pending_window()
        self.cancel_pending_retry()
        return response

    def _log_window_compliance(
        self,
        response: UsageResponse,
        published_min: datetime,
        published_max: datetime,
    ) -> None:
        """Record what each meter returned against what we asked for.

        This exists to settle an open question about asynchronous-batch custodians: whether the
        per-UsagePoint batch URL honours `published-min`/`published-max` at all. Every notified
        UsagePoint URL we have observed was bare (1622 of 1622), but that is what the custodian
        chose to advertise, not what the endpoint accepts — the same custodians do parameterize
        the subscription-level URLs they notify. So we ask WITH the filter and check the answer
        here rather than assuming either way.

        A single poll cannot settle it, and the reason is worth stating: `published-min` filters
        by PUBLICATION date while readings are stamped with their own interval START. One
        publication event legitimately carries years of readings — that is what a backfill is — so
        "asked for a day, got two years" is not by itself proof the filter was ignored.

        What IS diagnostic is the pattern across polls, which is why every field needed for the
        comparison goes on one line per meter: an ignored filter shows up as successive polls with
        ADVANCING windows returning identical counts and identical reading spans. That is exactly
        the signature that caught Burlington's `updated-min` (a 4-day and a 2-year window both
        returning 22,008 readings), and it is visible in an ordinary user's debug log without
        anyone running a credentialed probe.
        """
        if not _LOGGER.isEnabledFor(logging.INFO):
            return
        for up in response.usage_points:
            starts = [r.start for series in up.series for r in series.readings]
            if not starts:
                _LOGGER.info(
                    "Window check entry=%s usage_point=%s requested=[%s, %s] readings=0",
                    self.entry.entry_id,
                    up.usage_point_id,
                    published_min.isoformat(),
                    published_max.isoformat(),
                )
                continue
            earliest, latest = min(starts), max(starts)
            before_window = sum(1 for start in starts if start < published_min)
            _LOGGER.info(
                "Window check entry=%s usage_point=%s requested=[%s, %s] readings=%d "
                "span=[%s, %s] span_days=%d before_published_min=%d full_history=%s",
                self.entry.entry_id,
                up.usage_point_id,
                published_min.isoformat(),
                published_max.isoformat(),
                len(starts),
                earliest.isoformat(),
                latest.isoformat(),
                (latest - earliest).days,
                before_window,
                self._force_full_history,
            )

    def _persist_rotated_credentials(self, new_credentials: NewCredentials | None) -> None:
        """Write rotated credentials into the config entry, if the proxy returned any.

        Persisted on BOTH the success and error paths, and BEFORE anything else that can fail:
        a one-time refresh token (e.g. savagedata's OpenIddict) is redeemed during the proxy's
        token refresh, so dropping the rotated blob leaves the stored token dead and the next
        poll cascades into a spurious reauth. No-op when nothing was rotated.
        """
        if new_credentials is None:
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_ENCRYPTED_REFRESH_BLOB: new_credentials.encrypted_refresh_blob,
                CONF_PROXY_TOKEN: new_credentials.proxy_token,
            },
        )
        _LOGGER.info("Persisted rotated credentials for entry %s", self.entry.entry_id)

    async def _async_ensure_customer_label(self) -> None:
        """One-time: fetch customer data and fold a distinguishing label into the entry title.

        Two accounts at the same utility (e.g. Milton Hydro's two sandbox test customers)
        otherwise present as identical entries — indistinguishable in the UI without downloading
        diagnostics. We fetch the ESPI RetailCustomer feed once, derive a short label (service
        address / account number) and append it to the title, and stash the raw account id +
        address for diagnostics.

        Best-effort by design:
          - Runs only until a label is stored (``CONF_CUSTOMER_LABEL`` present — even as ``""``
            when the utility exposes no customer data — so we don't refetch every poll).
          - Also invoked from the deferred-fetch (HTTP 202) path, where it doubles as a probe of
            whether the custodian defers every Batch resource or only the usage export.
          - Any fetch failure is swallowed (labeling is never what a refresh turns on); rotated
            credentials from the attempt are still persisted, and we retry on the next poll —
            EXCEPT a permanent failure (``OpenGbPermanentError``: the proxy propagated the
            utility's 4xx — no customer resource advertised, or one our scope may not access such
            as Burlington's 403), which we record as an empty label so it isn't retried forever.
        """
        if CONF_CUSTOMER_LABEL in self.entry.data:
            return  # Already resolved (or determined unavailable) — don't refetch.

        try:
            result = await self.api.fetch_customer(
                encrypted_refresh_blob=self.entry.data[CONF_ENCRYPTED_REFRESH_BLOB],
                proxy_token=self.entry.data[CONF_PROXY_TOKEN],
            )
        except OpenGbPermanentError as err:
            # Permanent (proxy propagated the utility's 4xx). Two cases, both un-retryable: the
            # custodian advertises no customer resource (proxy 400 `no_customer_uri`), or it
            # refuses one our scope doesn't cover (e.g. Burlington's 403 `access_denied`). Record
            # an empty label so we stop retrying it on every poll. Persist any rotated credentials
            # first so we don't burn a one-time refresh token.
            self._persist_rotated_credentials(err.new_credentials)
            _LOGGER.info(
                "Customer data permanently unavailable for entry %s (%s); not retrying",
                self.entry.entry_id,
                err,
            )
            self._store_customer_label(None, account_id=None, address=None)
            return
        except OpenGbDataPendingError as err:
            # The customer feed is deferred TOO — so this custodian defers its whole Batch tree,
            # not just the heavyweight usage export. Called out separately from the transient
            # branch below, and at INFO rather than DEBUG, because it is a fact about the platform
            # rather than a hiccup: it decides whether a prepared batch can be collected by asking
            # for a smaller resource, or whether nothing under /Batch/ answers synchronously and
            # collection has to work another way entirely. Left unstored so it retries.
            self._persist_rotated_credentials(err.new_credentials)
            _LOGGER.info(
                "Entry %s: the customer feed is deferred as well (HTTP 202) — this custodian "
                "defers its whole Batch resource tree, not just the usage export: %s",
                self.entry.entry_id,
                err,
            )
            return
        except OpenGbApiError as err:
            # Transient (auth-expired / 5xx / etc.) — a customer-fetch error is non-fatal: it is
            # layered on a usage poll that either already succeeded or has already failed on its
            # own terms. Persist any rotated credentials so we don't burn a one-time refresh
            # token, then retry the label on the next poll.
            self._persist_rotated_credentials(err.new_credentials)
            _LOGGER.debug(
                "Customer-data fetch failed for entry %s (will retry): %s",
                self.entry.entry_id,
                err,
            )
            return
        except Exception as err:  # noqa: BLE001
            # Deliberately broad: labeling is a non-critical enhancement layered on an
            # already-successful usage poll, so NOTHING it does (a network error, a parse
            # surprise) may propagate and fail the refresh. Swallow, log, and retry next poll.
            _LOGGER.debug(
                "Customer-data fetch error for entry %s (will retry): %s",
                self.entry.entry_id,
                err,
            )
            return

        self._persist_rotated_credentials(result.new_credentials)
        customer = result.customer
        # Once per entry. Also the positive half of the probe described at the deferred-fetch call
        # site: reaching here at all means the customer resource answered synchronously, which for
        # an entry whose usage fetch is stuck on 202 is the informative outcome.
        _LOGGER.info(
            "Entry %s: customer feed answered (label=%s)",
            self.entry.entry_id,
            customer.label if customer else "<none advertised>",
        )
        self._store_customer_label(
            customer.label if customer else None,
            account_id=customer.account_id if customer else None,
            address=customer.service_address if customer else None,
        )

    def _store_customer_label(
        self,
        label: str | None,
        account_id: str | None,
        address: str | None,
    ) -> None:
        """Persist the resolved customer label + details, and retitle the entry when we got one."""
        data = {
            **self.entry.data,
            CONF_CUSTOMER_LABEL: label or "",
            CONF_CUSTOMER_ACCOUNT_ID: account_id,
            CONF_CUSTOMER_ADDRESS: address,
        }
        if label:
            # Base off the utility name (not the current title) so we never double-append on a
            # later re-resolution.
            base = self.entry.data.get(CONF_UTILITY_NAME) or self.entry.title
            self.hass.config_entries.async_update_entry(
                self.entry, data=data, title=f"{base} — {label}"
            )
            _LOGGER.info("Labeled entry %s with customer detail: %s", self.entry.entry_id, label)
        else:
            self.hass.config_entries.async_update_entry(self.entry, data=data)

    def _advance_cursor(self, response: UsageResponse) -> None:
        """Persist the incremental cursor as the newest reading start we've imported.

        Writes BOTH the entry-wide frontier (`CONF_LAST_FETCHED_AT`) and the per-meter cursors
        (`CONF_USAGE_POINT_CURSORS`). The entry-wide one answers "has this entry ever imported a
        reading?" and drives the startup poll-due check; the per-meter map is what scopes the poll
        window, because meters publish independently.

        Cursors only move *forward*, and only when the response actually carried readings. On an
        empty response nothing is touched — critical for a utility that publishes on a multi-day
        lag: anchoring to the real data frontier (rather than wall-clock ``now``) stops
        `published-min` from marching past the not-yet-published data and starving every later
        poll. See [_published_min].
        """
        newest = _newest_reading_start(response)
        if newest is None:
            return  # Empty response — keep the window reaching back to the last real data.
        prior_raw = self.entry.data.get(CONF_LAST_FETCHED_AT)
        prior = _parse_iso_or_none(prior_raw)
        cursor = newest if prior is None else max(prior, newest)
        cursor_iso = cursor.isoformat()

        # Per-meter cursors, MERGED not replaced: a meter absent from this response keeps the
        # cursor it already had. Meters publish on their own cadences (a monthly gas meter is
        # silent through most of an electric meter's polls), so treating absence as "no data ever"
        # would reset a slow meter's frontier and re-fetch its whole history on the next poll.
        # Forward-only per meter, for the same reason the entry-wide cursor is.
        prior_cursors = self._usage_point_cursors()
        merged = dict(prior_cursors)
        for up_id, up_newest in _newest_reading_start_by_usage_point(response).items():
            known = prior_cursors.get(up_id)
            if known is None or up_newest > known:
                merged[up_id] = up_newest
        merged_iso = {up_id: value.isoformat() for up_id, value in merged.items()}

        stored_cursors = self.entry.data.get(CONF_USAGE_POINT_CURSORS)
        if cursor_iso == prior_raw and merged_iso == stored_cursors:
            return  # No forward movement — avoid a no-op entry write (and its churn).
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_LAST_FETCHED_AT: cursor_iso,
                CONF_USAGE_POINT_CURSORS: merged_iso,
            },
        )

    def _reconcile_data_availability(self) -> None:
        """Settle, after a successful fetch, whether this account has produced any data yet.

        The cursor is the proof: [_advance_cursor] writes `CONF_LAST_FETCHED_AT` the first time a
        response carries readings and never retreats, so its absence here means this entry has
        never had a single reading — not from this poll, and not from any poll before it.

        Two outcomes:

          - **We have data.** Nothing is outstanding: drop the empty stamp, clear the repair issue
            a previous poll may have raised (for either a 202 or an empty feed), and stand any fast
            retry down. This is the steady state and costs one dict lookup.
          - **We have none.** The fetch itself was clean, so this isn't a failure HA has any
            vocabulary for — the entry is authorized, its credentials work, and the utility simply
            answered with nothing. For a brand-new entry that is overwhelmingly "the custodian is
            still assembling the data" (see CONF_EMPTY_SINCE), and the *only* thing that used to
            happen next was the ordinary poll, a day later. So stamp when the wait began, tell the
            user what's going on, and come back on a widening backoff.

        Deliberately scoped to accounts with no data AT ALL. An established entry polling into a
        quiet window — a utility that publishes weekly, or a poll that lands between publications —
        returns empty all the time and must stay silent.
        """
        if CONF_LAST_FETCHED_AT in self.entry.data:
            self._clear_empty_since()
            self._async_clear_background_load_issue()
            self.cancel_pending_retry()
            return

        first_time = CONF_EMPTY_SINCE not in self.entry.data
        self._remember_empty_since()
        # WARNING the first time, then DEBUG: the condition recurs on every retry, and a wait we
        # have already explained (in the log and in a repair issue) shouldn't keep shouting. The
        # first one is loud because "the Energy dashboard offers me no statistics" is otherwise
        # indistinguishable from a broken install.
        log = _LOGGER.warning if first_time else _LOGGER.debug
        log(
            "Entry %s: %s answered normally but has published no usage data at all yet, so there "
            "is nothing to import and the Energy dashboard has no statistics to offer. Utilities "
            "that assemble data on demand commonly do this right after authorization. Re-checking "
            "on a widening backoff (first in %s); no action is needed",
            self.entry.entry_id,
            self.entry.data.get(CONF_UTILITY_NAME, "your utility"),
            EMPTY_RETRY_INITIAL,
        )
        self._async_create_background_load_issue(deferred=False)
        self._schedule_empty_retry()

    def _remember_empty_since(self) -> None:
        """Stamp when this entry's data-less wait began. No-op once stamped.

        Must NOT restart on every empty poll — it's what decides both when to stop re-checking
        quickly and how far apart the re-checks get.
        """
        if CONF_EMPTY_SINCE in self.entry.data:
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_EMPTY_SINCE: datetime.now(UTC).isoformat()},
        )

    def _clear_empty_since(self) -> None:
        """Drop the data-less stamp once data has landed (no-op when none is set)."""
        if CONF_EMPTY_SINCE not in self.entry.data:
            return
        data = {**self.entry.data}
        data.pop(CONF_EMPTY_SINCE, None)
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    def _schedule_empty_retry(self) -> None:
        """Re-check sooner than the ordinary cadence for an account that has never had data.

        The delay is "wait as long again as you've already waited", floored at
        [EMPTY_RETRY_INITIAL] and capped at the entry's own poll interval — 5, 10, 20, 40 …
        minutes, derived from `CONF_EMPTY_SINCE` rather than a persisted attempt counter, so it
        survives a restart and can't drift out of step with the stamp that drives escalation.

        Past PENDING_ESCALATE_AFTER we stop: the ordinary poll timer carries it from there, and a
        utility that has published nothing in a day is not going to be shaken loose by a tenth
        full-history query. Shares `_pending_retry_unsub` with the 202 path so one
        [cancel_pending_retry] — and the entry-unload teardown wired in `async_setup_entry` —
        tears down whichever is armed.
        """
        if self._pending_retry_unsub is not None:
            return
        elapsed = self._pending_elapsed()
        if elapsed >= PENDING_ESCALATE_AFTER:
            _LOGGER.debug(
                "Entry %s has had no data for %s — dropping back to the ordinary poll interval",
                self.entry.entry_id,
                elapsed,
            )
            return
        ceiling = self.update_interval or DEFAULT_SCAN_INTERVAL
        delay = min(max(EMPTY_RETRY_INITIAL, elapsed), ceiling)

        async def _retry(_now: datetime) -> None:
            self._pending_retry_unsub = None
            await self.async_refresh()

        _LOGGER.debug(
            "Entry %s: re-checking for first data in %s (waiting %s so far)",
            self.entry.entry_id,
            delay,
            elapsed,
        )
        self._pending_retry_unsub = async_call_later(self.hass, delay, _retry)

    def _schedule_deferred_import_migration(
        self,
        response: UsageResponse,
        had_prior_statistics: bool,
    ) -> None:
        """Run the one-time statistics repair as soon as HA finishes starting.

        The first refresh happens inside config-entry setup, before HA is running, where the
        rebuild's recorder block would deadlock (see the caller). Waiting for the next ordinary
        poll instead would strand a user on visibly wrong data for a full poll interval — a day
        on most utilities — after they installed the update that fixes it. `async_at_started`
        fires on EVENT_HOMEASSISTANT_STARTED, moments later; its unsubscribe is tied to the entry
        so an unload cancels a still-pending repair.

        Armed at most once: a second arming would run two full-history rebuilds concurrently.
        """
        if self._import_migration_armed:
            return
        self._import_migration_armed = True

        async def _deferred_migration(_hass: HomeAssistant) -> None:
            _LOGGER.debug(
                "Running deferred import-logic repair check for entry %s (HA has started)",
                self.entry.entry_id,
            )
            # publish=True: we're outside the refresh cycle now, so a rebuild has to publish its
            # own result — nothing else will.
            await self._async_migrate_import(response, had_prior_statistics, publish=True)

        _LOGGER.debug(
            "HA is %s, not running — deferring the import-logic repair check for entry %s "
            "until after startup",
            self.hass.state,
            self.entry.entry_id,
        )
        self.entry.async_on_unload(async_at_started(self.hass, _deferred_migration))

    async def _async_migrate_import(
        self,
        response: UsageResponse,
        had_prior_statistics: bool,
        *,
        publish: bool = False,
    ) -> UsageResponse | None:
        """Repair statistics this entry imported under superseded logic. Returns the rebuilt
        response when a rebuild ran, else None.

        Rows are written once, as they're fetched, so a fix to how usage or cost is *computed*
        leaves everything already in the recorder wrong. `greenbutton.rebuild_statistics` has
        always been the cure, but it only helps a user who notices the bad data and knows the
        action exists — an inflated consumption spike mostly looks like "the Energy dashboard is
        broken". So an entry carrying a stale `CONF_IMPORT_LOGIC_REVISION` repairs itself here.

        A blanket rebuild would be the simple answer and the wrong one: it makes every user
        re-pull their entire initial-history window from their utility to fix a bug most of them
        never had. So we rebuild only entries whose feed shows an offending shape, and stamp the
        rest as current on the spot. [statistics.response_needs_import_migration] owns the
        per-revision recognition, and tests every revision newer than this entry's stamp — so an
        entry that skipped a release gets each repair it's owed, in a single rebuild.

        Three ways this resolves, all at most once per entry:

          - **Never imported at all** → nothing was written by the old logic, so stamp and move
            on. Covers a newly-added entry, whose very first import is already correct. Note this
            needs BOTH an empty store and no stamp: revision 1 purged affected entries and then
            imported nothing, so "no rows" on its own can mean *damaged*, not new.
          - **The feed has no offending shape** → this entry was never affected; stamp. Held back
            on an empty response, where "no offending shape" only means the utility published
            nothing in this window — the decision waits for a poll that carries readings.
          - **Affected** → rebuild, then stamp.

        A failed rebuild leaves the stamp unset deliberately: the entry stays corrupt but marked
        for repair, and the next poll tries again. It never fails the refresh — the poll itself
        succeeded, and taking the entry down over a repair of *old* rows would also stop new ones
        from arriving.
        """
        from homeassistant.exceptions import HomeAssistantError

        stamped_revision = self.entry.data.get(CONF_IMPORT_LOGIC_REVISION, 0)

        # An empty store alone does NOT mean "never imported" — revision 1 rebuilt affected
        # entries by purging first and then importing nothing, which is precisely the damage
        # revision 2 repairs. A stamp is proof the entry has imported before, so only an
        # unstamped entry with no rows is genuinely new and safe to stamp forward untouched.
        if not had_prior_statistics and not stamped_revision:
            self._store_import_logic_revision()
            return None

        if not any(s.readings for up in response.usage_points for s in up.series):
            _LOGGER.debug(
                "Entry %s awaits an import-logic repair check, but this poll carried no "
                "readings to judge the feed by — re-checking on the next poll",
                self.entry.entry_id,
            )
            return None

        verdict = response_needs_import_migration(response, stamped_revision)
        if verdict is None:
            # Undecidable from this poll — see [statistics.response_cost_may_be_missing_bills].
            # Stamping here is the one thing we must not do: it would close the repair on evidence
            # we never had.
            _LOGGER.debug(
                "Entry %s awaits an import-logic repair check, but this poll didn't carry what "
                "the check needs to judge by — re-checking on the next poll",
                self.entry.entry_id,
            )
            return None
        if not verdict:
            _LOGGER.debug(
                "Entry %s is unaffected by the import-logic changes since revision %d; marking "
                "revision %d without a rebuild",
                self.entry.entry_id,
                stamped_revision,
                IMPORT_LOGIC_REVISION,
            )
            self._store_import_logic_revision()
            return None

        _LOGGER.warning(
            "Entry %s imported statistics under superseded logic (revision %d, current is %d) "
            "and its feed has the shape that bug applied to. Rebuilding this entry's statistics "
            "from a full history re-fetch — this runs once and may take several minutes",
            self.entry.entry_id,
            stamped_revision,
            IMPORT_LOGIC_REVISION,
        )
        try:
            rebuilt = await self.async_rebuild_statistics(publish=publish)
        except HomeAssistantError as err:
            # Non-fatal: THIS poll's data is imported and sound. Leave the revision unstamped so
            # the next poll retries, and name the manual action for anyone who wants it sooner.
            _LOGGER.warning(
                "Automatic statistics rebuild failed for entry %s: %s. Existing statistics were "
                "left untouched and the rebuild will be retried on the next poll; you can also "
                "run the %s.%s action manually",
                self.entry.entry_id,
                err,
                DOMAIN,
                SERVICE_REBUILD_STATISTICS,
            )
            return None

        # The revision stamp is written by [async_rebuild_statistics] itself, so a manual
        # rebuild counts too — it produces rows under exactly the same current logic.
        # INFO, not WARNING: this is the resolution of the warning above, and HA logs at INFO
        # by default, so it lands in the same log the user is already reading.
        _LOGGER.info(
            "Statistics for entry %s have been rebuilt and are now correct", self.entry.entry_id
        )
        return rebuilt

    def _store_import_logic_revision(self) -> None:
        """Stamp the entry as holding statistics computed by the current import logic."""
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_IMPORT_LOGIC_REVISION: IMPORT_LOGIC_REVISION},
        )

    async def async_rebuild_statistics(self, *, publish: bool = True) -> UsageResponse:
        """Rebuild this entry's statistics from a full re-fetch — non-destructively.

        The supported recovery path when a calculation change (e.g. the cost fix) means the
        already-stored rows are wrong — without making the user remove the entry and redo the
        OAuth authorization. Steps, in order:

          1. Re-fetch the full initial-history window. **This happens FIRST**, so a failed
             fetch can never leave the store emptied.
          2. Only once we hold the replacement data: clear this entry's energy + cost stats.
          3. Block until the recorder has committed the delete.
          4. Import the freshly-fetched response into the now-empty store with ``fresh=True``,
             which imports from a zero baseline and skips the per-series resume-point read.
             That read ([statistics._resume_point], on a DB executor thread) is what a rebuild
             raced against: if it observed the pre-clear cursor, every reading in the
             full-history feed looked "already imported" and got skipped — importing nothing
             and leaving the store empty. ``fresh=True`` removes the race entirely.

        The earlier implementation cleared *before* re-fetching, so a failed fetch (the
        utility's resource server is intermittently flaky) wiped the user's history — and the
        incremental poll could not repopulate it, because its `published-min` window sits
        ahead of the utility's lagged data. Fetching first makes a failed rebuild a no-op.

        Args:
            publish: Publish the re-fetched response to the coordinator on success. True for
                the `rebuild_statistics` action, which runs standalone and must leave `.data`
                and the poll timer consistent with what it just imported. False when called
                from inside [_async_update_data] (the one-time repair in
                [_async_migrate_import]): that refresh publishes the returned response itself,
                and calling `async_set_updated_data` mid-refresh would publish twice and re-arm
                the timer underneath the running poll.

        Returns:
            The full-history response that was imported.

        Raises:
            HomeAssistantError: the rebuild fetch failed (network / upstream / reauth).
                Nothing was purged; the existing statistics remain intact.
        """
        from homeassistant.components.recorder import get_instance
        from homeassistant.exceptions import HomeAssistantError

        now = datetime.now(UTC)
        self._force_full_history = True
        try:
            # `_force_full_history` also overrides any frozen pending window: a rebuild is an
            # explicit "go get everything again", not a resumption of the deferred fetch.
            published_min, published_max = self._fetch_window(now)
            _LOGGER.info(
                "Rebuild for entry %s: re-fetching full history (published-min=%s) before purge",
                self.entry.entry_id,
                published_min.isoformat(),
            )
            try:
                response = await self._fetch(published_min, published_max)
            except (UpdateFailed, ConfigEntryAuthFailed) as err:
                raise HomeAssistantError(
                    f"Rebuild fetch failed for entry {self.entry.entry_id}: {err}. "
                    "Existing statistics were left untouched."
                ) from err
        finally:
            self._force_full_history = False

        total_readings = sum(len(s.readings) for up in response.usage_points for s in up.series)
        _LOGGER.info(
            "Rebuild for entry %s: fetched %d usage point(s) with %d total reading(s)",
            self.entry.entry_id,
            len(response.usage_points),
            total_readings,
        )

        # The fetch succeeded — now it's safe to purge and re-import from a clean slate.
        cleared = await async_clear_statistics_for_entry(self.hass, self.entry.entry_id)
        await get_instance(self.hass).async_block_till_done()
        _LOGGER.info(
            "Rebuild for entry %s: cleared %d statistic(s); importing fresh",
            self.entry.entry_id,
            len(cleared),
        )
        # fresh=True: we just cleared the store, so import from a zero baseline and skip the
        # per-series resume-point read. That read is what a rebuild raced against — if it saw
        # the pre-clear cursor, every reading in the re-fetched full-history feed looked
        # "already imported" and got skipped, importing nothing.
        await import_usage_statistics(
            self.hass,
            self.entry,
            response,
            utility_display_name=self.entry.data.get(CONF_UTILITY_NAME, "Open Green Button"),
            fresh=True,
        )
        self._advance_cursor(response)
        self._reconcile_data_availability()
        # Everything in the store was just computed by the current logic, so the entry is at the
        # current revision by construction — whether we got here from the `rebuild_statistics`
        # action or from the automatic repair in [_async_migrate_import].
        self._store_import_logic_revision()
        if publish:
            # Publish the fresh response into the coordinator (updates .data, marks the update
            # successful, notifies listeners, and re-arms the poll) without a second fetch.
            self.async_set_updated_data(response)
        _LOGGER.info("Rebuild complete for entry %s", self.entry.entry_id)
        return response

    @property
    def _background_load_issue_id(self) -> str:
        """Stable repair-issue id for this entry's async background-load condition."""
        return f"background_load_{self.entry.entry_id}"

    def _async_create_background_load_issue(self, *, deferred: bool = True) -> None:
        """Raise (or update) the repair issue for an account whose data hasn't arrived.

        Two axes, four messages:

        [deferred] says how the utility told us. ``True`` is an explicit HTTP 202 — "collecting it
        now, come back for it" — where we hold a frozen window and are collecting a batch the
        custodian has already started. ``False`` is a clean HTTP 200 that simply carried nothing
        for an account that has never had any data, where all we have is a well-founded suspicion
        that it's coming. Conflating them would tell one set of users about a status code their
        utility never sent.

        Severity tracks how long we've been waiting, because the two ends need opposite things from
        the user. A utility that has only just said "not yet" is working as designed and the user
        should do *nothing* — an ERROR there is a false alarm that makes a normal mode look like a
        broken integration. One that still has nothing a day later is a real problem we want
        reported.

        One issue id across all four: an entry can only be in one of these states at a time (a 202
        raises before the empty check can run), and re-creating with the same id updates in place,
        so an entry that crosses PENDING_ESCALATE_AFTER upgrades rather than accumulating a second.
        """
        from homeassistant.helpers import issue_registry as ir

        stuck = self._pending_elapsed() >= PENDING_ESCALATE_AFTER
        if deferred:
            translation_key = "background_load_stuck" if stuck else "background_load_pending"
        else:
            translation_key = "no_data_yet_stuck" if stuck else "no_data_yet"
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._background_load_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR if stuck else ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders={
                "utility": self.entry.data.get(CONF_UTILITY_NAME, "your utility"),
            },
            learn_more_url=BACKGROUND_LOAD_ISSUE_URL,
        )

    def _pending_elapsed(self) -> timedelta:
        """How long we've been waiting for data (zero if the wait is brand new).

        Reads whichever stamp applies — the frozen 202's `CONF_PENDING_SINCE`, or the data-less
        entry's `CONF_EMPTY_SINCE`. They're mutually exclusive in practice (a 202 raises before the
        empty check runs, and a successful fetch clears the pending window), but the 202 is the
        more specific condition so it wins if both are somehow set.
        """
        raw = self.entry.data.get(CONF_PENDING_SINCE) or self.entry.data.get(CONF_EMPTY_SINCE)
        since = _parse_iso_or_none(raw)
        if since is None:
            return timedelta(0)
        return max(timedelta(0), datetime.now(UTC) - since)

    def _async_clear_background_load_issue(self) -> None:
        """Delete the background-load repair issue for this entry (no-op if absent)."""
        from homeassistant.helpers import issue_registry as ir

        ir.async_delete_issue(self.hass, DOMAIN, self._background_load_issue_id)

    def _make_raw_xml_sink_if_debug(self):
        """Return a sink that writes the raw upstream XML to disk, or None.

        Gating is `_LOGGER.isEnabledFor(DEBUG)` for `custom_components.greenbutton` — the
        same toggle that flips when the user enables debug logging on the integration in
        the UI (Settings → Devices & Services → ⋮ → Enable debug logging). When debug is
        off we return None so [api.fetch_usage] skips the IO entirely.
        """
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return None
        path = xml_cache_path(self.hass, self.entry.entry_id)

        async def sink(data: bytes) -> None:
            await self.hass.async_add_executor_job(_write_xml_sync, path, data)
            _LOGGER.debug("Persisted %d bytes of raw upstream XML to %s", len(data), path)

        return sink

    def _fetch_window(self, now: datetime) -> tuple[datetime, datetime]:
        """Return the (`published_min`, `published_max`) to send on the next /proxy/usage call.

        Normally derived from `now` — see [_published_min]. The exception is an outstanding
        asynchronous batch: when a previous fetch came back HTTP 202, the custodian prepared that
        dataset under the URL it was asked for, and only an identical request can collect it. So
        the frozen window wins over a freshly-computed one until a fetch succeeds. Freezing BOTH
        bounds matters — `published_max` is `now`-relative too, so leaving it to drift would keep
        the request unique even with `published_min` pinned.

        A rebuild (`_force_full_history`) deliberately ignores the freeze: the user asked for
        everything, not for the resumption of a deferred slice.
        """
        if not self._force_full_history:
            pending = self._pending_window()
            if pending is not None:
                _LOGGER.debug(
                    "Entry %s replaying the window of a deferred (HTTP 202) fetch: %s → %s",
                    self.entry.entry_id,
                    pending[0].isoformat(),
                    pending[1].isoformat(),
                )
                return pending
        return self._published_min(now), now

    def _pending_window(self) -> tuple[datetime, datetime] | None:
        """The frozen window of an outstanding 202, or None. Unparseable values are discarded."""
        parsed_min = _parse_iso_or_none(self.entry.data.get(CONF_PENDING_PUBLISHED_MIN))
        parsed_max = _parse_iso_or_none(self.entry.data.get(CONF_PENDING_PUBLISHED_MAX))
        if parsed_min is None or parsed_max is None:
            return None
        return parsed_min, parsed_max

    def _remember_pending_window(self, published_min: datetime, published_max: datetime) -> None:
        """Freeze the window a 202 was returned for, so every retry re-asks identically.

        `published_max` is frozen CLAMPED TO NOW. The proxy clamps any future `published-max` down
        to its own `now` before it reaches the custodian (see UsageClient in the server repo), so a
        frozen bound in the future gets rewritten to a fresh instant on every single retry — the
        window looks frozen from here while the custodian sees a brand-new URL each time, which
        for an asynchronous-batch custodian enqueues a new job instead of collecting the finished
        one. That is the whole failure this freeze exists to prevent. Observed live: entries whose
        `published-min` was pinned to one value still produced ~175 distinct `published-max`
        values over ~175 polls. A frozen value that is already in the past makes the clamp a
        no-op, so what we send is what the custodian sees.

        The very first replay still differs slightly from the original request (the custodian saw
        the proxy's clamp instant; we freeze ours, a round-trip later). Every replay after that is
        identical, which is what collecting a prepared batch needs.

        Also stamps when the wait started, the first time. All of it is a no-op once frozen: the
        stamp must NOT restart on every retry (it's what decides when to stop waiting), and
        rewriting unchanged values would churn the config entry every few minutes.
        """
        effective_max = min(published_max, datetime.now(UTC))
        if self._pending_window() == (published_min, effective_max):
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_PENDING_PUBLISHED_MIN: published_min.isoformat(),
                CONF_PENDING_PUBLISHED_MAX: effective_max.isoformat(),
                CONF_PENDING_SINCE: datetime.now(UTC).isoformat(),
            },
        )

    def _clear_pending_window(self) -> None:
        """Drop the frozen window after a successful fetch (no-op when none is set)."""
        if CONF_PENDING_PUBLISHED_MIN not in self.entry.data:
            return
        data = {**self.entry.data}
        data.pop(CONF_PENDING_PUBLISHED_MIN, None)
        data.pop(CONF_PENDING_PUBLISHED_MAX, None)
        data.pop(CONF_PENDING_SINCE, None)
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    def _schedule_pending_retry(self) -> None:
        """Re-attempt a deferred fetch soon, rather than at the utility's ordinary cadence.

        The ordinary cadence is the utility's own poll interval — a day, typically — which is the
        wrong scale for a batch that lands in about a minute. Without this, completing setup on a
        202 (instead of leaving the entry in ConfigEntryNotReady, whose HA-driven backoff retried
        every 10 minutes) would trade a bad error state for a 24-hour wait.

        Armed at most once at a time, and only while we're still inside PENDING_ESCALATE_AFTER;
        past that we stop the fast cadence and let the normal poll timer carry it, so a custodian
        that never delivers isn't polled every five minutes forever.
        """
        if self._pending_retry_unsub is not None:
            return
        if self._pending_elapsed() >= PENDING_ESCALATE_AFTER:
            _LOGGER.debug(
                "Entry %s has been waiting on a deferred fetch for %s — dropping back to the "
                "ordinary poll interval",
                self.entry.entry_id,
                self._pending_elapsed(),
            )
            return

        async def _retry(_now: datetime) -> None:
            self._pending_retry_unsub = None
            await self.async_refresh()

        self._pending_retry_unsub = async_call_later(self.hass, PENDING_RETRY_INTERVAL, _retry)

    def cancel_pending_retry(self) -> None:
        """Cancel any armed deferred-fetch retry. Called on success and on entry unload."""
        if self._pending_retry_unsub is not None:
            self._pending_retry_unsub()
            self._pending_retry_unsub = None

    def _published_min(self, now: datetime) -> datetime:
        """Return the `published_min` value to send on the next /proxy/usage call.

        Always returns a concrete instant — never None. This is a deliberate workaround for
        a quirk in the Green Button Alliance test-lab harness: when `published-min` and
        `published-max` are both absent, it returns the usage feed without any IntervalBlock
        entries (metadata only). Sending an explicit window forces the data path on.

        On the first refresh (no recorded `last_fetched_at`) — or a rebuild, which sets
        `_force_full_history` — we look back by the per-utility initial-history window the
        server supplied in the claim response: a bounded initial import that keeps the
        utility's data-collection job and our statistics write manageable. Subsequent refreshes
        ask for the slice since the usage frontier, minus a small overlap that absorbs clock skew
        and any late-arriving corrections.

        A cursor is the newest *reading* we've imported for one meter, NOT wall-clock (see
        [_advance_cursor]). Anchoring to the data frontier is load-bearing: utilities publish on a
        multi-day lag, so a wall-clock cursor would push `published-min` past not-yet-published
        data and every later poll would come back empty.

        Cursors are per UsagePoint — per physical meter — and the window is scoped to the oldest
        of them, since one request has to serve every meter on the subscription. See
        CONF_USAGE_POINT_CURSORS for why a shared frontier isn't safe.

        A single tight window suffices for cost too: `published-min` filters by *publication* date,
        and a utility publishes a bill (per-interval `<cost>` or a monthly UsageSummary) with a
        recent publication date when it settles — so a routine poll naturally catches it. The cost
        importer distributes a freshly-published summary over the period's *already-recorded* usage
        (see [statistics._import_cost_summaries]), so we never need to reach back for old readings.
        """
        if self._force_full_history:
            return now - self._initial_lookback()

        # The OLDEST per-meter cursor, not the newest: one window has to serve every meter on the
        # subscription, and a meter that publishes monthly sits far behind one that publishes
        # daily. Scoping to the newest would let the fast meter drag `published-min` past data the
        # slow one has not delivered yet.
        cursors = self._usage_point_cursors()
        if cursors:
            return min(cursors.values()) - LAST_FETCHED_OVERLAP

        # No per-meter map yet: either a brand-new entry (fall through to full history) or one
        # written before CONF_USAGE_POINT_CURSORS existed, whose entry-wide frontier still stands
        # in until the next successful fetch seeds the map.
        raw = self.entry.data.get(CONF_LAST_FETCHED_AT)
        if raw is None:
            return now - self._initial_lookback()
        try:
            usage_frontier = datetime.fromisoformat(raw)
        except ValueError:
            # Stored value is corrupt — drop back to a full refetch rather than silently
            # losing the historical window.
            _LOGGER.warning(
                "%s in entry data is unparseable (%r); fetching full history",
                CONF_LAST_FETCHED_AT,
                raw,
            )
            return now - self._initial_lookback()
        return usage_frontier - LAST_FETCHED_OVERLAP

    def _usage_point_cursors(self) -> dict[str, datetime]:
        """Per-meter cursors from entry data, dropping any entry that won't parse.

        Corrupt values are skipped rather than fatal: losing one meter's cursor costs a wider
        window for that meter (the import is idempotent, so re-fetched readings are harmless),
        whereas raising would take the whole entry down over one bad string. An empty result is
        indistinguishable from "never written", which is exactly the fallback [_published_min]
        wants for a pre-upgrade entry.
        """
        stored = self.entry.data.get(CONF_USAGE_POINT_CURSORS)
        if not isinstance(stored, dict):
            return {}
        cursors: dict[str, datetime] = {}
        for up_id, raw in stored.items():
            parsed = _parse_iso_or_none(raw)
            if parsed is None:
                _LOGGER.warning(
                    "Entry %s: cursor for usage point %s is unparseable (%r); widening its window",
                    self.entry.entry_id,
                    up_id,
                    raw,
                )
                continue
            cursors[up_id] = parsed
        return cursors

    def _initial_lookback(self) -> timedelta:
        """How far back to backfill on the first fetch.

        Prefers the per-utility window the server supplied in the claim response (stored as
        `CONF_INITIAL_HISTORY_SECONDS`); falls back to `INITIAL_FETCH_LOOKBACK` only when that
        value is missing or non-positive (entries created before the server exposed it, or a
        self-hosted server that doesn't). The server is the single source of truth.
        """
        secs = self.entry.data.get(CONF_INITIAL_HISTORY_SECONDS)
        if isinstance(secs, (int, float)) and secs > 0:
            return timedelta(seconds=secs)
        return INITIAL_FETCH_LOOKBACK
