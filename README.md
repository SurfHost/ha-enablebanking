# Enable Banking for Home Assistant

[![Validate](https://github.com/SurfHost/ha-asnbank-balance/actions/workflows/validate.yml/badge.svg)](https://github.com/SurfHost/ha-asnbank-balance/actions/workflows/validate.yml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that shows account balances from any bank supported by **[Enable Banking](https://enablebanking.com/)** — including ASN Bank, N26, Revolut, Openbank, and hundreds more.

Each bank connection is a separate config entry, so you can add as many as you like and see all balances on one dashboard.

The integration uses **Enable Banking** as the licensed TPP (Third Party Provider). Enable Banking offers a free personal tier that covers a single PSU polling up to four times a day, which is exactly what PSD2 allows for unattended Account Information access.

## Features

- One config entry per bank — add ASN Bank, N26, Revolut, Openbank independently
- Balance sensor per discovered account under each entry
- Revolut Business supported: select ASPSP "Revolut" with account type "Business"
- EUR (and other currencies) with `state_class: total`, `device_class: monetary`
- Attributes per sensor: IBAN, account name, product, currency, balance type, reference date, last updated, bank name, `consent_expires_at`, `consent_days_remaining`
- DataUpdateCoordinator polling every 6 h (4/day, the PSD2 ceiling); configurable via options flow (1–24 h)
- Graceful 180-day consent expiry: proactive 14-day warning, automatic reauth UI when the consent lapses
- Reauth flow that re-uses your existing JWT (if still valid) and only requires a new bank authorisation

## Requirements

- Home Assistant 2026.4 or newer
- An [Enable Banking](https://enablebanking.com/) account (free personal tier)
- One active Enable Banking session per bank (valid 180 days)

## One-off setup at Enable Banking

You only need to do this once per bank. Each bank connection in HA maps to one Enable Banking session.

1. Sign up at [enablebanking.com](https://enablebanking.com/) and open the **Control Panel**.
2. Go to **API applications → Register a new application**. Give it any name (e.g. *Home Assistant*) and add `https://enablebanking.com/` as a redirect URL.
3. Download the application's **private key** and note the **application ID**.
4. Generate a JWT signed with your private key (RS256). Payload:
   ```json
   {
     "iss": "enablebanking.com",
     "aud": "api.enablebanking.com",
     "iat": <now>,
     "exp": <now + 24h>,
     "kid": "<your application ID>"
   }
   ```
   The Enable Banking docs include a ready-to-run Python snippet for this.

That JWT is all you need to start adding bank connections in Home Assistant — the integration handles the rest of the OAuth flow interactively.

A full walkthrough lives at [enablebanking.com/docs/api/quick-start/](https://enablebanking.com/docs/api/quick-start/).

## Installation

### HACS (custom repository)

[![Add Repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=SurfHost&repository=ha-asnbank-balance&category=integration)

Or manually in HACS:

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/SurfHost/ha-asnbank-balance` with category **Integration**.
3. Search for **Enable Banking** and install it.
4. Restart Home Assistant.

### Manual

1. Copy `custom_components/enablebanking/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

## Adding a bank

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=enablebanking)

Or: **Settings → Devices & Services → Add Integration → Enable Banking**.

The config flow has three steps:

1. **JWT** — paste your Enable Banking application JWT.
2. **Bank** — pick a bank from the dropdown (populated live from Enable Banking's ASPSP list) and select *Personal* or *Business*.
3. **Authorise** — the flow shows a link to your bank's login page. Click it, log in, and you'll be redirected to `enablebanking.com?code=…`. Copy the `code` value from the URL bar and paste it back in HA. The integration exchanges it for a session and creates the balance sensors.

Repeat from the top to add more banks.

### Revolut Business

Select ASPSP **Revolut** and account type **Business**. Enable Banking uses a single "Revolut" ASPSP entry with a `psu_type` field distinguishing personal and business — not two separate entries.

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
| `last_updated` | Timestamp of last successful coordinator poll |
| `aspsp` | Bank name (useful for templates across multiple entries) |
| `consent_expires_at` | ISO timestamp when the PSD2 consent expires |
| `consent_days_remaining` | Integer days until expiry |

## Options

| Option | Default | Range | Description |
|--------|---------|-------|-------------|
| Update interval (seconds) | 21600 (6 h) | 3600–86400 | Poll frequency. Values below 21600 may breach PSD2's 4-polls/day limit. |

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

## 180-day consent cycle

PSD2 limits unattended Account Information consent to **180 days**, after which the user must re-authorise (Strong Customer Authentication) regardless of how frequently they have polled.

### What happens when consent expires

- **14 days before expiry**: a `persistent_notification` appears in HA with the bank name and days remaining, prompting you to renew in advance.
- **On expiry** (or if the bank revokes consent early): the next poll receives a session-not-found response. The integration marks the entry as needing attention and HA shows the standard *"Integration needs attention"* card under Notifications.
- **Sensors** go to `unavailable` while reauth is pending.

### Renewing consent

Click the **Reconfigure** button on the integration card (or the notification link), or go to **Settings → Devices & Services → Enable Banking → your bank → Reconfigure**. The reauth flow pre-fills your JWT and asks you to complete a fresh bank authorisation (steps 2–3 of the setup flow above).

You do not need to regenerate your application private key or JWT unless they have also expired.

## Rate limits

- **PSD2 (the regulation)**: max 4 unattended AIS polls per day per consent. Keep the interval at 6 h (21600 s) or higher.
- **Enable Banking free tier**: no per-request charge for personal use.
- **Individual banks**: no published limits beyond the PSD2 ceiling.

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| "JWT was rejected" at step 1 | JWT expired (default TTL is 24 h) or signed with wrong key |
| "Auth code rejected" at step 3 | Copied the wrong query parameter — use only the `code=` value |
| Sensor shows `unavailable` | Consent expired or bank revoked access — use Reconfigure |
| Balance stuck / not updating | Check HA log at `logger: homeassistant.components.enablebanking: debug` |

## License

MIT
