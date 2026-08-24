# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
