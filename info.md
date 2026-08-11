# Open Green Button

Bridges your utility's [Green Button](https://www.greenbuttondata.org/) (NAESB ESPI) energy data into the Home Assistant Energy dashboard via a stateless OAuth proxy.

🚧 **Pre-alpha.** See the [current utility status](https://github.com/rocketraman/open-green-button#status) for which utilities are supported or in progress.

## Privacy

The hosted proxy server holds **zero per-user data**. Your OAuth refresh token lives encrypted in your Home Assistant config entry; every API call carries the token through the proxy and the server discards it immediately after the round-trip.

## Setup

Once released, setup will be:

1. Install via HACS.
2. Settings → Devices & Services → Add Integration → Open Green Button.
3. Pick your utility, click the authorization link, complete OAuth, and paste the claim code back into Home Assistant.

See the [project repository](https://github.com/rocketraman/open-green-button) for the implementation plan.

## Legal

Open Green Button is an open community project, not a legal entity, and is not affiliated with or endorsed by any utility. The integration is free to use under the MIT license, without warranty of any kind, and the hosted proxy is run by volunteers on a best-effort basis with no uptime commitment. Details under [Legal](https://github.com/rocketraman/open-green-button#legal).
