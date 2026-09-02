# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] - 2026-09-02

Fixes a first connection that could never complete at a slow utility, where
setup failed with "cancelled" and the entry stayed broken until Home Assistant
was restarted
([#49](https://github.com/rocketraman/open-green-button/issues/49)).

### Fixed

- Setup no longer stays open for the whole first fetch. A first, full-history
  pull is minutes-scale work at some utilities — Toronto Hydro answered the same
  2-year request after 268s, 269s, 306s and 409s on four attempts — but setup is
  usually driven by a request from your browser, and anything in front of Home
  Assistant that gives up on that request cancelled the setup with it. That left
  the entry in a terminal error state: no retry, no backoff, "unable to create
  connection" until a restart. Setup now waits 20 seconds and then completes,
  letting the fetch finish in the background and import when it lands.
- A knock-on effect of the above: because the config flow's own request was the
  one being abandoned, a retry of it re-submitted the single-use claim code and
  failed as "already used", so the real problem presented as a broken claim.
- The usage request now has a 15-minute timeout rather than inheriting the HTTP
  library's 5-minute default, which two of the four measured pulls exceeded.

## [0.2.0] - 2026-08-24

Collects usage from utilities that prepare their data asynchronously — Alectra,
Hydro Ottawa and Elexicon — which previously never imported anything at all
([#10](https://github.com/rocketraman/open-green-button-homeassistant/issues/10)).

Requires the Open Green Button proxy deployed on or after 2026-08-24.

### Added

- Collect a deferred (HTTP 202) batch from its per-UsagePoint resources. These
  custodians answer the subscription-level batch URL with 202 forever — it
  enqueues an export rather than serving one — and publish the prepared data
  beneath that subscription instead. The integration now reads it there. The
  attempt is opportunistic: if it fails, the poll behaves exactly as it did
  before, so it can only turn a failed poll into a successful one.
- An incremental cursor per meter, rather than one for the whole account. A
  subscription can carry several UsagePoints, commonly a different commodity
  each, and gas or water often publishes monthly where electricity publishes
  daily. Each meter now advances on its own, so a fast meter can no longer drag
  the poll window past a slow one's not-yet-published readings.
- Diagnostic logging for asynchronous custodians: the utility's own 202 response
  detail, and a per-meter line recording each poll's requested window against
  what came back. Enable debug logging for `custom_components.greenbutton` to
  collect it.

### Fixed

- A deferred fetch's frozen window is no longer silently un-frozen. The window
  is replayed so a custodian can answer with the batch it already prepared, but
  its upper bound was stored a day in the future and the proxy clamps a future
  bound to the current time — rewriting it on every retry, so the custodian saw
  a new request each time instead of a repeat of the previous one.
- Removed the one-day forward buffer on `published-max`. It never reached any
  utility (the proxy clamped it away), and some reject a future bound outright.

## [0.1.0]

Initial release. Everything prior to 0.2.0 shipped unversioned under this
number and is not itemized here; see the commit history.
