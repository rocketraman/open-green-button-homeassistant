"""Constants for the Open Green Button integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "greenbutton"

# Service that purges an entry's imported statistics and re-downloads + recomputes them from
# a full history re-fetch — the supported way to pick up a calculation-logic change (e.g. a
# cost fix) without removing the entry and redoing the OAuth authorization.
SERVICE_REBUILD_STATISTICS = "rebuild_statistics"
# Optional service field: which config entry to rebuild (via HA's config_entry selector).
# Omitted → rebuild every loaded Open Green Button entry.
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

# How far back to overlap the window when re-fetching. Generous to absorb clock skew between
# us and the utility, and to forgive late-arriving corrections. The statistics writer is
# idempotent on (statistic_id, hour) so duplicates are harmless.
LAST_FETCHED_OVERLAP = timedelta(days=1)

# Fallback for how far back to look on the first fetch (no recorded `last_fetched_at`). The
# authoritative, per-utility window comes from the server in the claim response and is stored
# as `CONF_INITIAL_HISTORY_SECONDS`; this constant is used ONLY when that value is absent —
# e.g. entries created before the server exposed it, or a self-hosted server that doesn't. It
# is deliberately NOT the source of truth (see the server's per-utility `initialHistory`), so
# the backfill window isn't configured in two places.
INITIAL_FETCH_LOOKBACK = timedelta(days=2 * 365)

# NOTE: there is deliberately no forward buffer on `published_max`. A one-day lookahead used to be
# added here to absorb clock skew against the utility, and it was doing nothing but harm: the proxy
# clamps a future `published-max` down to its own `now` before the request leaves (savagedata
# rejects a future bound outright with a bare 400), so the margin never reached any custodian, and
# it silently defeated the deferred-fetch window freeze by rewriting the "frozen" bound on every
# retry. Nor was the margin load-bearing — `published-min` is anchored to the data frontier (see
# CONF_USAGE_POINT_CURSORS), so a reading excluded at the tail of one window is still inside the
# next one. A tail miss costs one poll of latency, never data.

# The default cadence at which the DataUpdateCoordinator polls the proxy for new usage data.
# The authoritative, per-utility value comes from the server in the claim response and is stored
# as `CONF_POLL_INTERVAL_SECONDS`; this constant is used ONLY when that value is absent (entries
# created before the server exposed it, or a self-hosted server that doesn't). Utilities publish
# on a multi-hour-to-multi-day lag, so a daily poll captures everything without over-polling.
DEFAULT_SCAN_INTERVAL = timedelta(days=1)

# A utility whose permitted cadence is exactly this may be anchored to a local wall-clock time
# instead of drifting with whenever HA last started. Named separately from DEFAULT_SCAN_INTERVAL
# because the two mean different things — this one is "a cadence a clock time can express".
DAILY_CADENCE = timedelta(days=1)

# Optional per-entry polling options (options flow, not entry.data — the user owns these, the
# server owns the cadence). Disabled by default so existing entries keep polling on an interval
# measured from setup. Shorter or multi-day utility cadences ignore the option entirely: it only
# changes *when* an already-daily poll runs, never how often any poll runs.
CONF_DAILY_POLL_TIME_ENABLED = "daily_poll_time_enabled"
CONF_DAILY_POLL_TIME = "daily_poll_time"
DEFAULT_DAILY_POLL_TIME = "06:00:00"

# The hosted proxy server. May be overridden per-config-entry for self-hosters via the
# server_base_url in entry.data.
DEFAULT_SERVER_BASE_URL = "https://api.opengreenbutton.org"

# GitHub issue tracking "background data loads": utilities that answer a data request
# asynchronously (ESPI async batch — HTTP 202 "data is being collected, available later") rather
# than returning the feed. Rendered as the repair issue's "Learn more" link.
#
# Deliberately the general tracking issue rather than any one user's report, since this is what
# every affected user is pointed at. It should carry the current state of the investigation —
# notably that a 202 is a custodian's normal mode and not a dataset-size threshold, which is what
# the Alectra case (issues/10) established.
BACKGROUND_LOAD_ISSUE_URL = (
    "https://github.com/rocketraman/open-green-button-homeassistant/issues/1"
)

# Stripe-style API version this client was built against. Sent as OpenGB-Api-Version on every
# request. When the server bumps its API, this constant moves with the integration version.
API_VERSION = "2026-05-22"

# Config entry data keys.
CONF_UTILITY_ID = "utility_id"
CONF_UTILITY_NAME = "utility_name"
CONF_SERVER_BASE_URL = "server_base_url"
CONF_CLAIM_CODE = "claim_code"  # noqa: S105 — config key, not a secret
CONF_PROXY_TOKEN = "proxy_token"  # noqa: S105
CONF_ENCRYPTED_REFRESH_BLOB = "encrypted_refresh_blob"  # noqa: S105
CONF_SUBSCRIPTION_URI = "subscription_uri"
CONF_SCOPE = "scope"
CONF_API_VERSION = "api_version"
CONF_LAST_IMPORTED = "last_imported"

# Per-utility initial-backfill window, in seconds, as supplied by the server in the claim
# response (`initialHistorySeconds`). The coordinator uses this to compute `published-min` on
# the first fetch. Absent on entries created before the server exposed it → coordinator falls
# back to INITIAL_FETCH_LOOKBACK. (Re-authorizing an existing entry refreshes this value.)
CONF_INITIAL_HISTORY_SECONDS = "initial_history_seconds"

# Per-utility poll cadence, in seconds, from the claim response (`pollIntervalSeconds`). Drives
# the coordinator's update_interval. Absent on entries created before the server exposed it →
# coordinator falls back to DEFAULT_SCAN_INTERVAL. (Re-auth refreshes it.)
CONF_POLL_INTERVAL_SECONDS = "poll_interval_seconds"

# UTC ISO 8601 timestamp of the newest reading we've imported (the "usage frontier"). The
# coordinator scopes each poll to `published-min = this − overlap`, so every refresh asks the
# utility only for what's been published since last time. Absent on a new entry ⇒ first refresh
# fetches the full initial-history window. `published-min` filters by *publication* date, so a
# bill published late (a monthly UsageSummary, weeks after its period) is caught by an ordinary
# poll when it appears; the cost importer distributes it over the period's already-recorded usage,
# so no separate cost cursor is needed.
CONF_LAST_FETCHED_AT = "last_fetched_at"

# Per-meter incremental cursors: ``{usage_point_id: UTC ISO 8601 newest reading start}``.
#
# One UsagePoint is one physical meter, and a subscription can carry several — commonly a
# different commodity each (electricity daily, gas or water often monthly or bi-monthly, on
# separate meter-reading routes). Nothing in ESPI makes them publish on a shared schedule, so a
# single frontier for the whole entry is driven by whichever meter runs furthest ahead and says
# nothing about where the others actually are.
#
# The poll window is scoped to the OLDEST of these (see [GreenButtonCoordinator._published_min]):
# the window has to reach back far enough for the most-behind meter, or a fast meter would drag
# `published-min` past a slow one's not-yet-collected data.
#
# CONF_LAST_FETCHED_AT is still maintained alongside this as the entry-wide frontier — it answers
# "has this entry ever imported a reading?" and drives the startup poll-due check, neither of which
# is a per-meter question. An entry written before this key existed has no map; the fallback in
# [_published_min] reads the scalar until the next successful fetch seeds the map, so no migration
# step is needed.
CONF_USAGE_POINT_CURSORS = "usage_point_cursors"

# The exact `published-min`/`published-max` (UTC ISO 8601) of a fetch the utility answered with
# HTTP 202 — "I'm preparing that dataset out of band". Set when a poll hits 202, replayed verbatim
# by every retry, cleared on the first success.
#
# Load-bearing, not bookkeeping. A custodian doing ESPI asynchronous batch delivery prepares the
# dataset under the URL it was ASKED for, so it can only hand that dataset back to a request that
# asks the same way. Recomputing the window from `now` on each retry — which is what every other
# poll does — moves both bounds every time, so each retry enqueues a *fresh* job and gets a fresh
# 202, forever. That is the Alectra failure in issues/10: hours of 600-second retries, none of
# which could ever have matched. Freezing the window is what makes retry N+1 able to collect what
# retry N started. (The proxy additionally canonicalizes the values per-utility — see
# UtilityQuirks.dateFilterFormat server-side — but that only stabilizes the format, not the
# instant, so both halves are needed.)
CONF_PENDING_PUBLISHED_MIN = "pending_published_min"
CONF_PENDING_PUBLISHED_MAX = "pending_published_max"

# UTC ISO 8601 instant the current deferred (HTTP 202) fetch was first observed. Written with the
# frozen window and cleared with it. Drives how long we keep re-attempting quickly, and whether
# the repair issue reads as "in progress" or "stuck" — see PENDING_RETRY_INTERVAL and
# PENDING_ESCALATE_AFTER.
CONF_PENDING_SINCE = "pending_since"

# How often to re-attempt while a utility is preparing a deferred batch. The ordinary poll cadence
# is the utility's (a day, typically), which is the wrong scale entirely here: the one custodian
# we've observed doing this posted its "ready" notification 60-90 seconds after the 202. Waiting a
# day to collect something that landed in a minute is what makes an async batch feel broken. Five
# minutes converges within a couple of attempts without hammering the resource server.
PENDING_RETRY_INTERVAL = timedelta(minutes=5)

# How long config-entry setup waits for the first fetch before completing anyway and letting that
# fetch finish in the background.
#
# A first, full-history pull from a slow custodian is a minutes-scale operation: the proxy gives
# the utility up to five minutes before the first byte because savagedata routinely needs tens of
# seconds. Setup, though, is usually driven by an HTTP request the user's browser made — finishing
# the config flow, or hitting Reload — and anything in front of HA that gives up on that request
# (nginx's default proxy_read_timeout is 60s) cancels the setup task with it. That lands the entry
# in SETUP_ERROR, which unlike ConfigEntryNotReady is terminal: no backoff, no retry, "unable to
# create connection" until the user restarts HA. See issues/49.
#
# So bound the *waiting*, never the fetch. Short enough to stay well inside any such timeout, and
# to keep a slow utility off HA's startup critical path; long enough that the failures worth
# reporting as a failed setup (bad credentials, proxy down, DNS) still arrive in time to be
# raised from here.
FIRST_REFRESH_GRACE = timedelta(seconds=20)

# How long to keep up that fast cadence before concluding the batch is not coming. Past this the
# entry drops back to its ordinary poll interval and the repair issue escalates from "your utility
# is preparing this" to "this hasn't arrived, please report it". A day is deliberately generous:
# a custodian with `SubscriptionFrequency=Daily` may genuinely only run its export once a day.
# Shared with the empty-feed wait below, which is the same question asked a different way.
PENDING_ESCALATE_AFTER = timedelta(hours=24)

# UTC ISO 8601 instant of the first clean fetch that left this entry with no data at all. Written
# only while the entry has never carried a single reading (no CONF_LAST_FETCHED_AT), and dropped
# the moment one lands.
#
# The 202 path above is one way a utility says "not yet"; this is the other, and it used to have no
# handling at all. A custodian that collects asynchronously (UtilityAPI, and every utility behind
# it) answers the first request after authorization with a perfectly ordinary HTTP 200 carrying no
# IntervalBlocks, and starts assembling the data in the background. Nothing is imported, so no
# statistic metadata is registered, so the Energy dashboard's picker offers literally nothing —
# and the next attempt was a full poll interval (a day) away, with no repair issue and nothing
# above INFO in the log. Users reasonably read that as a broken integration and start deleting and
# re-authorizing the entry; the re-add appears to "fix" it only because setup re-fetches, by which
# time the utility has finished collecting. See issues/43.
CONF_EMPTY_SINCE = "empty_since"

# First re-attempt delay after a clean fetch that carried nothing, for an entry that has never had
# any data. Each subsequent attempt waits as long again as we've already been waiting (5, 10, 20,
# 40 … minutes), capped at the entry's ordinary poll interval and abandoned at
# PENDING_ESCALATE_AFTER. The widening is the difference from the 202 path's flat cadence: there we
# are collecting a batch the custodian has already prepared and told us about, so retrying every
# five minutes for a day is proportionate. Here we only suspect the data is coming, and every
# attempt re-asks for the entire initial-history window — a flat five minutes would mean ~288
# full-history queries a day against a utility that may simply have nothing to give us.
EMPTY_RETRY_INITIAL = timedelta(minutes=5)

# Revision of the statistics *calculation* logic that produced this entry's stored rows.
# Statistics are written once as they're fetched, so a fix that changes how usage or cost is
# computed does NOT retroactively correct rows already in the recorder — historically that
# needed the user to notice and run `greenbutton.rebuild_statistics` by hand. The coordinator
# compares this stamp against IMPORT_LOGIC_REVISION and repairs affected entries itself; see
# [coordinator.GreenButtonCoordinator._async_migrate_import].
CONF_IMPORT_LOGIC_REVISION = "import_logic_revision"

# Bump when a change means previously-imported rows are wrong and must be rebuilt, AND add a
# recognition predicate to [statistics._IMPORT_MIGRATION_CHECKS] so the coordinator can tell an
# affected entry's feed from an unaffected one (a blanket rebuild would make every user re-pull
# their full history against their utility for a bug that may not affect them). An entry stamped
# at revision N is tested against every predicate above N, so a user who skipped a release still
# gets each repair they're owed.
#   1 — cumulative meter registers (ESPI BULK_QUANTITY et al) were summed into the consumption
#       statistic, and their `cost=0` placeholder suppressed real UsageSummary billing.
#       Affects feeds that publish a cumulative register: Milton Hydro. (issues #6, #7)
#   2 — a reading longer than an hour was written entirely into the hour it started in, instead
#       of being spread across the hours it spans. Affects billing-only feeds, where one reading
#       covers a whole billing period: Consumers Energy, and any UtilityAPI utility whose account
#       has no interval data (Eversource, El Paso Electric).
#       Revision 1 compounded this: it classed those same readings as cumulative registers and
#       excluded them, so an affected entry's rebuild purged its rows and re-imported nothing.
#       Those entries are stamped 1 and sitting on an EMPTY store with a cursor advanced past
#       their data — nothing but this migration will ever re-import them.
#   3 — billing summaries that overlap at a meter-read day were taken for duplicates and dropped,
#       so the cost statistic was built from roughly every other bill. A billing period is
#       inclusive of both read dates, so this is the norm, not an edge case: all twelve bills in
#       an El Paso Electric feed overlap the previous by exactly one day, and six were discarded
#       — $27,615 of $58,335. Burlington and Elexicon have the same shape. Affects any account
#       billed through UsageSummary; feeds that itemize per-interval <cost> never ran the
#       selection and are stamped forward untouched. Usage rows are unaffected — the damage is
#       cost, and it is damage by omission, which is why nobody reported it as wrong data.
IMPORT_LOGIC_REVISION = 3

# Customer-data fields, fetched once from the ESPI RetailCustomer feed and folded into the entry
# title so two accounts at the same utility are distinguishable (see
# [coordinator.GreenButtonCoordinator._async_ensure_customer_label]). CONF_CUSTOMER_LABEL doubles
# as the "already attempted" marker — present (even as "") means we've tried and won't refetch.
CONF_CUSTOMER_LABEL = "customer_label"
CONF_CUSTOMER_ACCOUNT_ID = "customer_account_id"
CONF_CUSTOMER_ADDRESS = "customer_address"
