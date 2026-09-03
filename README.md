# Enable Banking for Home Assistant

[![Validate](https://github.com/SurfHost/ha-enablebanking/actions/workflows/validate.yml/badge.svg)](https://github.com/SurfHost/ha-enablebanking/actions/workflows/validate.yml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that shows account balances from any bank supported by **[Enable Banking](https://enablebanking.com/)** > including ASN Bank, N26, Revolut, Openbank, and hundreds more.

Each bank connection is a separate config entry, so you can add as many as you like and see all balances on one dashboard.

The integration uses **Enable Banking** as the licensed TPP (Third Party Provider). Enable Banking offers a free personal tier that covers a single PSU polling up to four times a day, which is exactly what PSD2 allows for unattended Account Information access.

## Features

- One config entry per bank > add ASN Bank, N26, Revolut, Openbank independently
- Balance sensor per discovered account under each entry
- Revolut Business supported: select ASPSP "Revolut" with account type "Business"
- EUR (and other currencies) with `state_class: total`, `device_class: monetary`
- Attributes per sensor: IBAN, account name, product, currency, balance type, reference date, bank name, `last_polled_at`, `last_error`, `stale`, `consent_expires_at`, `consent_days_remaining`
- Scheduled polling at fixed local times (10:00, 14:00, 18:00, 22:00) with per-entry minute jitter > exactly four polls/day, aligned with PSD2's cap
- **Never-unavailable sensors**: balances are cached to disk and displayed even during rate-limits, network blips, consent expiry, or the first moment after an HA restart before the first poll runs
- Graceful 180-day consent expiry: proactive 14-day warning, automatic reauth UI when the consent lapses
- **Self-renewing API token**: the integration stores your application private key and signs a fresh 23-hour JWT itself, so there is no token to paste and nothing that silently expires
- Reauth flow that revalidates the existing bank session first, so an expired token is a one-click fix with no bank round-trip

## Requirements

- Home Assistant 2026.4 or newer
- An [Enable Banking](https://enablebanking.com/) account (free personal tier)
- Your Enable Banking application's private key (.pem) and application ID
- One active Enable Banking session per bank (valid 180 days)

## One-off setup at Enable Banking

You only need to do this once. The same private key and application ID serve every bank you add.

1. Sign up at [enablebanking.com](https://enablebanking.com/) and open the **Control Panel**.
2. Go to **API applications > Register a new application**. Give it any name (e.g. *Home Assistant*) and add `https://enablebanking.com/` as a redirect URL.
3. Download the application's **private key** (.pem) and note the **application ID** (a UUID).

That is all. You do **not** generate a JWT by hand: the config flow takes the private key plus the application ID and mints the RS256 token itself, then renews it every 23 hours (Enable Banking caps token lifetime at 24 h). Nothing to re-paste, ever.

The private key is stored in the config entry, so it lives in `.storage/core.config_entries` on your HA instance in plain text, like every other credential HA holds. Treat a backup of that directory accordingly.

`scripts/generate_jwt.py` is still in the repo, but only as a debugging aid for poking the Enable Banking API with `curl` outside Home Assistant. It is not part of the setup flow.

A full walkthrough lives at [enablebanking.com/docs/api/quick-start/](https://enablebanking.com/docs/api/quick-start/).

## Installation

### HACS (custom repository)

[![Add Repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=SurfHost&repository=ha-enablebanking&category=integration)

Or manually in HACS:

1. HACS > three-dot menu > **Custom repositories**.
2. Add `https://github.com/SurfHost/ha-enablebanking` with category **Integration**.
3. Search for **Enable Banking** and install it.
4. Restart Home Assistant.

### Manual

1. Copy `custom_components/enablebanking/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

## Adding a bank

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=enablebanking)

Or: **Settings > Devices & Services > Add Integration > Enable Banking**.

The config flow has four steps:

1. **Credentials** > paste the full contents of your application's private key (.pem) into the multi-line field and enter the application ID. The flow validates them by minting a JWT and fetching the bank list. On a second or later bank both fields come pre-filled from the entry you already have.
2. **Country** > pick the country the bank is in. This just filters the (long) bank list.
3. **Bank** > pick a bank from the dropdown (populated live from Enable Banking's ASPSP list) and select *Personal* or *Business*.
4. **Authorise** > the flow shows a link to your bank's login page. Click it, log in, and you'll be redirected to `enablebanking.com?code=...`. Copy the `code` value from the URL bar and paste it back in HA. The integration exchanges it for a session and creates the balance sensors.

Repeat from the top to add more banks.

### Revolut Business

Select ASPSP **Revolut** and account type **Business**. Enable Banking uses a single "Revolut" ASPSP entry with a `psu_type` field distinguishing personal and business > not two separate entries.

## Sensors

### Balance (per account)

| Property | Value |
|----------|-------|
| State | EUR balance (closing booked preferred, falls back to interim available) |
| Unit | Account currency |
| State class | `total` |
| Device class | `monetary` |

**Attributes**

| Key | Description |
|-----|-------------|
| `iban` | Account IBAN |
| `account_name` | Account name or product |
| `product` | Product type from the bank |
| `currency` | ISO currency code |
| `balance_type` | Balance type code (CLBD, ITAV, …) |
| `reference_date` | Date of the reported balance |
| `aspsp` | Bank name (useful for templates across multiple entries) |
| `last_polled_at` | ISO timestamp of the last successful fetch for this account |
| `last_error` | Short tag if the most recent poll attempt failed: `rate_limited`, `network`, `consent_expired`, `auth`, `api`. Empty string on success. |
| `stale` | Boolean, true when `last_polled_at` is older than 16 h (twice the 8 h nominal interval; the schedule itself is fixed, see below) |
| `consent_expires_at` | ISO timestamp when the PSD2 consent expires |
| `consent_days_remaining` | Integer days until expiry |

## Polling schedule

The integration polls at four fixed local times per day:

```
10:00   14:00   18:00   22:00
```

Per-entry minute jitter (deterministic from `entry_id`) staggers banks so they don't hit at `HH:00:00` simultaneously. No interval setting > times are hard-coded, which guarantees you sit exactly at the PSD2 4/day cap regardless of HA restart frequency.

If HA is down when a scheduled time passes, the coordinator runs one catch-up poll on startup (with 0-60 s jitter). If HA is up but the cache is still within the current schedule window, no startup poll runs at all.

### Forcing a poll

The integration registers one service, `enablebanking.refresh`, which polls every configured entry immediately:

```yaml
action: enablebanking.refresh
```

It is meant for debugging a freshly added bank. Each call spends real PSD2 quota, so use it sparingly.

## Lovelace example

A multi-bank view using one tile per account, grouped by bank:

```yaml
type: vertical-stack
cards:
  - type: heading
    heading: ASN Bank
  - type: tile
    entity: sensor.asn_bank_balance_nl00asnb0123456789
    name: Betaalrekening
    icon: mdi:bank
    color: green
  - type: heading
    heading: N26
  - type: tile
    entity: sensor.n26_balance_de00n260987654321
    name: Current Account
    icon: mdi:credit-card
  - type: heading
    heading: Revolut (business)
  - type: tile
    entity: sensor.revolut_balance_lt000000000000000000
    name: Business Account
    icon: mdi:briefcase
  - type: heading
    heading: Openbank
  - type: tile
    entity: sensor.openbank_balance_es00open0000000000
    name: Cuenta Corriente
    icon: mdi:bank-outline
```

Replace entity IDs with the ones Home Assistant created for your accounts. A template card showing days until consent expiry:

```yaml
type: markdown
content: >-
  {% set s = states.sensor | selectattr('attributes.consent_expires_at', 'defined') | list %}
  {% for e in s %}
  **{{ e.attributes.aspsp }}**: {{ e.attributes.consent_days_remaining }} days remaining
  {% endfor %}
```

## PSD2 polling quota (4 per day)

PSD2 caps unattended Account Information polling at **4 times per day per consent**. Every HA restart, reload, or manual reconfigure burns one of those slots. If you exceed it the bank responds with `HTTP 429 / ASPSP_RATE_LIMIT_EXCEEDED` (`HUB046` on de Volksbank's API) and refuses further polls until the rolling 24 h window elapses.

Instead of a configurable interval, the integration polls at four fixed local times > `10:00`, `14:00`, `18:00`, `22:00` > hitting the 4/day cap exactly and predictably. No restart can burn extra quota because the cache supplies startup values.

### What the integration does about it

- **Balances persist across HA restarts.** The last successful balance per account is written to `.storage/enablebanking.<entry_id>.cache`. On startup the sensor shows the cached value immediately > no API call is made.
- **Skip the boot-time poll.** If the cache still sits within the current schedule window, the first post-restart poll is skipped > the next scheduled slot handles it. Catch-up only runs if HA was down during a scheduled slot.
- **Staggered startup.** The catch-up (when it does run) is jittered 0-60 s per entry, so four banks don't all burst at the same second.
- **Per-account back-off.** If a single account returns 429, that account's next scheduled slot is skipped entirely, then normal cadence resumes. Other accounts under the same bank keep polling.
- **Sensors never go `unavailable`.** On any failure (rate limit, network, consent expiry, API error) the sensor keeps displaying the last known balance. The `last_error` attribute tells you why the latest attempt failed; the `stale` attribute flips to `true` once the cache is older than 16 hours.
- **Self-renewing token.** The stored private key lets the coordinator mint a new JWT whenever the current one is within 30 minutes of expiry, so an expired token never costs you a poll or a manual step.
- **Smart reauth.** If reauth is triggered while the bank session is in fact still alive, the flow mints a token, revalidates that session and finishes without a bank round-trip. Only a genuinely dead session sends you back to the bank's login page.

## 180-day consent cycle

PSD2 limits unattended Account Information consent to **180 days**, after which the user must re-authorise (Strong Customer Authentication) regardless of how frequently they have polled.

### What happens when consent expires

- **14 days before expiry**: a `persistent_notification` appears in HA with the bank name and days remaining, prompting you to renew in advance.
- **On expiry** (or if the bank revokes consent early): the next poll receives a session-not-found response. The integration triggers the reauth flow (the standard *"Integration needs attention"* card appears under Notifications) and sets `last_error: "consent_expired"` on the sensors.
- **Sensors keep showing the last known balance** with `stale: true`. They do not go unavailable. The reauth card does the nagging.

### Renewing consent

Click the **Reconfigure** button on the integration card (or the notification link), or go to **Settings > Devices & Services > Enable Banking > your bank > Reconfigure**. The reauth flow pre-fills your private key and application ID and, if the bank session really is gone, asks you to complete a fresh bank authorisation (step 4 of the setup flow above).

You do not need to regenerate your application private key unless you revoked it at Enable Banking. The JWT is never your problem: the integration signs and renews it on its own.

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| "Credentials rejected" at step 1 | Private key pasted partially (the `-----BEGIN`/`-----END` lines must be included) or the application ID belongs to a different key |
| "Auth code rejected" at step 4 | Copied the wrong query parameter, use only the `code=` value |
| Entry loads but no sensors appear | The consent granted no accounts. The log says so at WARNING level; check in the Enable Banking dashboard that the account is linked and the app's Account Information service is not restricted |
| Sensor shows `unavailable` | Consent expired or bank revoked access, use Reconfigure |
| Balance stuck / not updating | Check HA log at `logger: custom_components.enablebanking: debug` |

## Development

```bash
uv sync --extra dev
uv run pytest          # the test suite
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy            # types (scoped to custom_components/enablebanking)
```

CI runs all four on every push, plus hassfest and the HACS validator. The tests
need no network and no Enable Banking account — every API call is mocked, and
the RSA key the JWT tests sign with is generated in-process rather than
committed.

## License

MIT
