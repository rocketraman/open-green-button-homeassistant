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

# Small forward buffer added to `published_max` to absorb clock skew between us and the
# utility — a meter reading published at `now()` on the utility's clock might be a few
# minutes in our future, and we don't want to miss it on the next sliding-window poll.
PUBLISHED_MAX_LOOKAHEAD = timedelta(days=1)

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

# How long to keep up that fast cadence before concluding the batch is not coming. Past this the
# entry drops back to its ordinary poll interval and the repair issue escalates from "your utility
# is preparing this" to "this hasn't arrived, please report it". A day is deliberately generous:
# a custodian with `SubscriptionFrequency=Daily` may genuinely only run its export once a day.
PENDING_ESCALATE_AFTER = timedelta(hours=24)

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
IMPORT_LOGIC_REVISION = 2

# Customer-data fields, fetched once from the ESPI RetailCustomer feed and folded into the entry
# title so two accounts at the same utility are distinguishable (see
# [coordinator.GreenButtonCoordinator._async_ensure_customer_label]). CONF_CUSTOMER_LABEL doubles
# as the "already attempted" marker — present (even as "") means we've tried and won't refetch.
CONF_CUSTOMER_LABEL = "customer_label"
CONF_CUSTOMER_ACCOUNT_ID = "customer_account_id"
CONF_CUSTOMER_ADDRESS = "customer_address"
