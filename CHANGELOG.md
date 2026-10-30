# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.4] - 2026-10-30

### Changed

- Enbridge Gas: usage now comes from the meter reads printed on each bill
  instead of the per-bill readings, which stop months before the bills do
  (spring 2026 on every account we've seen) and are never corrected when a
  meter reader's visit replaces an estimate
  ([open-green-button#39](https://github.com/rocketraman/open-green-button/issues/39)).
  Usage is spread evenly from one actual meter read to the next, so an
  estimated month's figure is replaced once the next actual read arrives;
  the bills between two actual reads still add up exactly. Each bill's cost
  stays in its own billing period, and a correction bill is costed together
  with the bill it corrects, so a credit no longer prices a month below zero.
  Enbridge entries rebuild their statistics automatically, once. Requires the
  Open Green Button server deployed on or after this release.
- The diagnostics download now shows each Enbridge bill's meter reads, which
  of them the usage was built from, and every reading that disagreed with the
  bills (`bill_reading_mismatches`).

### Fixed

- Cost could go missing after a rebuild or an automatic repair, for
  utilities that bill through monthly summaries (Enbridge Gas, Burlington
  Hydro and others): every bill was skipped as having "no recorded usage",
  and no cost statistic was written at all. The integration read the usage
  back before Home Assistant had finished saving it; whether it hit depended
  on timing. It now waits for the save to finish. An entry with no cost
  recovers by running the Rebuild statistics action once.

## [0.3.3] - 2026-10-28

### Added

- The diagnostics download now shows each series' total cost, total kWh and
  implied price per kWh, plus the parsing quirks the last response was read
  under, so a cost-scale problem is visible without debug logging.
- With debug logging on, the diagnostics download now also keeps the raw data
  for utilities that send readings per meter rather than in one feed (Alectra).

### Fixed

- Alectra: interval cost was imported 100 times too small. Alectra reports
  cost in thousandths of a dollar rather than the standard hundred-thousandths;
  the server now flags this and the integration applies it. Affected entries
  rebuild their statistics automatically
  ([#33](https://github.com/rocketraman/open-green-button-homeassistant/issues/33)).
- Enbridge Gas: usage and cost came out a million times too large, because
  Enbridge sends every scale factor with the wrong sign. The server now flags
  this and the integration corrects it. Affected entries rebuild their
  statistics automatically.
- El Paso Electric (and other UtilityAPI utilities that bill through monthly
  summaries): bill costs came out about 100,000 times too high, for example
  $470,770 for a 13 kWh day. A bill line that states the kWh it charged for was
  read as if that kWh figure set the scale of the dollar amount. Affected
  entries rebuild their statistics automatically once a poll brings in a bill.
  ([open-green-button#99](https://github.com/rocketraman/open-green-button/issues/99))

## [0.3.2] - 2026-10-20

### Added

- The diagnostics download now includes `recent_polls`: when each poll of the
  last five days ran and how many readings it returned. It shows whether the
  integration is still asking and the utility has nothing new, which nothing
  else in the file could tell apart from not polling at all.

### Fixed

- Milton Hydro: a poll that carried only the meter's daily register reading,
  without the hourly data it normally comes with, recorded the meter's lifetime
  total as one day's usage (around 144,000 kWh). The poll also moved past the
  missing hourly data, so it was never fetched. The register is now recognized
  even without the hourly data, and the next poll asks for that data again.
  Accounts that were hit repair themselves once after updating; others are left
  alone
  ([#17](https://github.com/rocketraman/open-green-button-homeassistant/pull/17),
  thanks @geophilips).
- A poll with only the register reading no longer logs an error asking for the
  feed to be reported.
- Ontario time-of-use cost: statutory holidays are now priced off-peak all day,
  including the weekday a weekend holiday moves to. They were costed on the
  normal weekday schedule, which put part of the spend on on-peak and mid-peak
  hours. The bill total was not affected
  ([#16](https://github.com/rocketraman/open-green-button-homeassistant/pull/16),
  thanks @margosmark).
- Utilities listed under a parent company (e.g. Algoma Power under Fortis
  Ontario): reconnecting after the sign-in expires now shows the utility you
  picked on the consent page, rather than the parent company.

## [0.3.1] - 2026-10-10

### Added

- A repair notice when a utility appears to have re-issued a meter's ID. The
  readings were still imported, but into new statistics, so the ones in the
  Energy dashboard looked like they had stopped updating (seen with Burlington
  Hydro in September 2026). The notice asks whether the new ID is the same
  meter: **Merge** moves what was imported under it onto the end of the existing
  statistics and continues them from then on; **Keep separate** leaves both as
  they are. Nothing is merged without being asked.

### Fixed

- Statistics repairs that an update brings now run on the restart that installs
  it. They used to wait for the next scheduled poll when Home Assistant
  restarted soon after the last one, so the negative bars fixed in 0.3.0 stayed
  on screen for up to a day after updating
  ([#21](https://github.com/rocketraman/open-green-button-homeassistant/issues/21)).
- The repair for those negative bars no longer waits for a poll that returns new
  readings; it is recognized from the stored data alone.
- A first sync with a utility that prepares its data in the background (Hydro
  Ottawa, Alectra and other Savage Data utilities) no longer waits forever on
  "your utility is preparing this". These utilities reply "accepted" and
  separately tell the server where the data can be picked up; the integration
  now reads it from there instead of looking for a meter list that does not
  exist
  ([open-green-button#80](https://github.com/rocketraman/open-green-button/issues/80)).
- Readings a utility corrects after first publishing them are no longer ignored.
  An hour already imported used to be skipped even when a later poll re-fetched
  it with a different value, which could leave a day flat at zero (seen with
  Burlington Hydro). Re-fetched hours are now compared with what is stored and
  the statistics restated where they differ, for usage and for cost that the
  utility reports per reading. Cost spread from monthly bills is not restated.

## [0.3.0] - 2026-10-09

### Added

- Gas and water meters. Each gets its own statistic series
  (`…_gas_forward`, `…_water_forward`), named for the commodity and offered in
  the Energy dashboard's gas and water pickers. Electricity statistic ids are
  unchanged.
- More units: therms, BTU and joules (stored as kWh — Home Assistant has no
  therm unit, and takes gas as either volume or energy), cubic feet, US gallons,
  litres, and imperial gallons (stored as litres).

### Fixed

- Cubic feet were recorded as cubic metres. ESPI unit code 119 is ft³; m³ is 42.
- Large negative bars in the Energy dashboard for utilities that publish one
  meter's history as several separate readings, as UtilityAPI utilities such as
  Consumers Energy do
  ([#21](https://github.com/rocketraman/open-green-button-homeassistant/issues/21)).
  Each piece kept its own running total, so the stored total fell back at every
  boundary between them, and a piece older than one already imported could be
  skipped altogether. They are now combined into a single total. Affected
  accounts are detected and rebuilt automatically, once, on the next poll.
- Where a utility reports the same period both as a bill-level total and as
  hourly intervals, the hourly figures are used for the hours they cover instead
  of both being counted.
- Diagnostics downloads no longer include your service address or account
  number
  ([#20](https://github.com/rocketraman/open-green-button-homeassistant/issues/20)).

An account with a gas or water meter rebuilds its statistics once after
updating, to move them to the new series and re-read history in units that used
to be skipped. If you had already added such a meter's old statistic to the
Energy dashboard, re-select the new one. Electricity-only accounts are not
rebuilt.

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
