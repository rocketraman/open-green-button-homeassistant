# Open Green Button — Home Assistant integration

Bridges your utility's [Green Button](https://www.greenbuttondata.org/) (NAESB ESPI) energy data into the Home Assistant Energy dashboard via a stateless OAuth proxy server.

🚧 **Pre-alpha.** See the [current utility status](https://github.com/rocketraman/open-green-button#status) for which utilities are supported or in progress; the proxy server itself is hosted on Fly.io.

## How it works

This integration talks to a hosted proxy server at `https://api.opengreenbutton.org`. The proxy exists only because utilities require a stable public HTTPS callback URL for OAuth — your data never lives on it. Refresh tokens are stored encrypted in your Home Assistant config entry; every API call carries the token through the proxy and the server discards it immediately after the round-trip.

Server source code: [rocketraman/open-green-button](https://github.com/rocketraman/open-green-button).

## Installation

### HACS (custom repository, until accepted into HACS default)

1. In HACS, open **Integrations**.
2. Click the **⋮** menu → **Custom repositories**.
3. Add `https://github.com/rocketraman/open-green-button-homeassistant` with category **Integration**.
4. Install **Open Green Button**.
5. Restart Home Assistant.

### Manual

Copy `custom_components/greenbutton/` into your Home Assistant config directory at `<config>/custom_components/greenbutton/` and restart.

## Configuration

1. **Settings → Devices & Services → Add Integration → Open Green Button**.
2. Pick your utility from the dropdown.
3. Click the authorization link, complete the Green Button consent flow with your utility.
4. Paste the claim code (starts with `gb_live_`) back into Home Assistant.

The integration writes hourly consumption data into the HA Energy dashboard's long-term statistics.

## Recomputing statistics after an update

Statistics are written once, as they're fetched — so if an update changes how usage or **cost** is calculated, rows already in the database keep their old values.

When an update fixes a calculation bug, the integration repairs affected accounts by itself: on the first poll after the update it checks whether your utility's feed has the shape the bug applied to, and if it does, rebuilds that account's statistics automatically (once — you'll see it in the log). Accounts the bug never touched are left alone rather than made to re-download their whole history.

You can also rebuild on demand — after changing something yourself, or if an automatic repair failed. Run the **Rebuild statistics** action:

**Developer Tools → Actions → “Open Green Button: Rebuild statistics” → Perform action.**

Or in YAML:

```yaml
action: greenbutton.rebuild_statistics
data:
  config_entry_id: <your entry>   # optional — omit to rebuild every configured account
```

It deletes that account's imported energy and cost statistics, then re-downloads and recomputes the full history from scratch. Because it pulls the entire initial-history window, it puts the same load on your utility as a fresh setup — run it when you need it, not on a schedule. If the re-fetch fails partway (network/utility hiccup), the statistics simply repopulate on the next successful poll or the next rebuild.

### Polling schedule

How often each utility may be polled is decided by that utility and passed to the integration by the proxy server — you can't make it poll more often. Most utilities publish once a day.

When the cadence works out to exactly once a day, you can choose *when* that poll runs, under **Settings → Devices & services → Open Green Button → Configure**. Enable **Poll daily at a specific local time** and pick a time. It uses Home Assistant's timezone and follows daylight-saving changes. Utilities on a shorter or multi-day cadence ignore the setting and keep their own interval.

If Home Assistant is down when a poll was due — either the interval elapsed or the daily time went by — the poll runs when it next starts. Restarting inside a window that has already been polled doesn't re-fetch: that data is already in the recorder, so the restart just waits for the next scheduled poll.

## Supported utilities

See the [current utility status](https://github.com/rocketraman/open-green-button#status) on the proxy server for the up-to-date list of supported and in-progress utilities.

Want your utility added? [Request a new utility](https://github.com/rocketraman/open-green-button/issues/new?template=new-utility-request.md).

## Privacy

- Your refresh token, usage data, and account identifiers live **only on your Home Assistant instance**.
- The hosted proxy server holds **zero per-user durable state** — no accounts, no databases, no usage history.
- Open source under MIT — read the code, run your own proxy, or fork it.
- **Clean removal:** deleting the integration via Devices & Services purges every long-term statistic it created. Multiple config entries on the same utility (e.g. a sandbox account beside a real one, or several meters at one address) get distinct, per-entry statistic IDs so they never bleed together in the Energy dashboard.

## Development

Toolchain pinned via [mise](https://mise.jdx.dev/). Python 3.13.

```sh
mise trust            # one-time
mise install          # installs Python 3.13, auto-creates .venv
pip install -r requirements_test.txt

ruff check .
ruff format --check .
pytest
```

The venv at `.venv/` is auto-activated when you `cd` into the repo.

## Roadmap

**Working today**

- OAuth authorization against the proxy server, with refresh-token rotation handled automatically
- Polls the proxy at the utility's permitted cadence, optionally anchored to a local time of day, and writes hourly consumption into the Energy dashboard's long-term statistics via [`async_add_external_statistics`](https://developers.home-assistant.io/docs/core/entity/sensor#statistics-imported-from-external-sources)
- Reauth flow surfaces as an HA notification when the utility revokes our refresh token
- Imports per-billing-period cost from ESPI `UsageSummary` blocks into the Energy dashboard's Cost column, with Ontario time-of-use distribution

**Pending**

- First-utility production credentials — currently in test-lab certification with the Green Button Alliance (see [utility status](https://github.com/rocketraman/open-green-button#status))
- Push-based delivery (ESPI FB_39 NotificationURI) instead of polling, once a real utility supports it
- Additional utilities — [request a new utility](https://github.com/rocketraman/open-green-button/issues/new?template=new-utility-request.md) with your provider's name

## Debugging

If something looks off (cost showing zero, readings missing, parser surprised by a utility's quirk), the integration ships a diagnostics export that surfaces the relevant signals in one click.

**Settings → Devices & Services → Open Green Button → ⋮ → Download Diagnostics**

You'll get a JSON file containing:

- Redacted config-entry data (no refresh tokens or proxy tokens leak)
- Coordinator state (last refresh result, scan interval)
- Parsed last-response summary — usage points, series counts, **billing summaries with full cost-detail breakdown**
- The raw ESPI XML from the most recent upstream fetch, **if** debug logging is enabled

To capture the raw XML for offline analysis:

1. **Enable debug logging** — the integration's ⋮ menu has an "Enable debug logging" toggle. Or add to `configuration.yaml`:
   ```yaml
   logger:
     logs:
       custom_components.greenbutton: debug
   ```
2. Wait for the next refresh (or reload the integration to force one).
3. **Download Diagnostics** — the resulting JSON has an `raw_xml` field with the full upstream feed.
4. Extract it with `jq`:
   ```sh
   jq -r '.raw_xml' diagnostics-greenbutton-*.json > usage.xml
   xmllint --format usage.xml | less
   ```

XML is cached on disk (in HA's `.storage` directory) rather than held in memory — so even multi-MB feeds don't bloat the running integration. The cache is overwritten on every debug-enabled refresh and removed when the config entry is removed.

## Contributing

Issues and PRs welcome. For substantial features, open an issue first so we can talk through the approach.

Questions or feedback? [Discuss this add-on on the Home Assistant Community forum](https://community.home-assistant.io/t/utility-energy-data-integration-via-green-button-connect/1016031).

If this integration is useful to you and you want to help keep it maintained and the proxy server hosted:

- [GitHub Sponsors](https://github.com/sponsors/rocketraman)
- [Buy Me a Coffee](https://www.buymeacoffee.com/rocketraman)

Suggested $5/month — covers proxy hosting plus time spent adding utilities and keeping up with Home Assistant changes.

## Legal

Open Green Button is an open community project. It is not a legal entity, and it is not affiliated with or endorsed by any utility. This integration is free to use and is provided under the [MIT license](LICENSE), without warranty of any kind and with no liability to the authors or copyright holders.

The hosted proxy at `https://api.opengreenbutton.org` is run by volunteers on a best-effort basis, with no uptime or support commitment, under those same terms. If you would rather not depend on it, the [server](https://github.com/rocketraman/open-green-button) is open source and can be [deployed by anyone](https://github.com/rocketraman/open-green-button/blob/master/docs/deployment.md).

Registering as a third party with a utility is a separate matter, covered under [Legal](https://github.com/rocketraman/open-green-button#legal) in the main project README.

"Green Button" is a trademark of the Green Button Alliance; this project uses the name in reference to the open data standard.
