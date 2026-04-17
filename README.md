# ASN Bank Balance for Home Assistant

[![Validate](https://github.com/SurfHost/ha-asnbank-balance/actions/workflows/validate.yml/badge.svg)](https://github.com/SurfHost/ha-asnbank-balance/actions/workflows/validate.yml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that shows the balance of your **ASN Bank** (de Volksbank) checking account on a dashboard. It also works for SNS and RegioBank accounts, since they share the same banking licence.

The integration does not talk to ASN's PSD2 API directly (that requires an eIDAS QWAC certificate and TPP registration with De Nederlandsche Bank — not realistic for hobbyists). Instead it uses **[Enable Banking](https://enablebanking.com/)** as a licensed TPP aggregator. Enable Banking offers a free tier for personal use, which is plenty for a single dashboard sensor polled four times a day.

## Why not dogmatic69 / GoCardless?

[dogmatic69/open-banking-homeassistant](https://github.com/dogmatic69/open-banking-homeassistant) wraps the GoCardless Bank Account Data API (formerly Nordigen). That was the obvious choice until July 2025, when GoCardless [closed new signups](https://bankaccountdata.gocardless.com/new-signups-disabled). Existing accounts still work, but nobody new can register — so that integration has become installable-but-useless for fresh users. Enable Banking is the remaining aggregator that still onboards individuals and has a free personal tier covering de Volksbank.

## Features

- Balance sensor per ASN/SNS/RegioBank account the Enable Banking session exposes
- EUR, state class `total`, device class `monetary`
- Attributes: IBAN, account name, product, currency, balance type, reference date, last updated
- DataUpdateCoordinator polling every 6h by default (= 4/day, the PSD2 ceiling)
- Options flow to tune the interval (1-24 h)
- Reauthentication flow when the Enable Banking session is revoked or expires

## Requirements

- Home Assistant 2026.4 or newer
- An Enable Banking account (free personal tier is fine)
- An active Enable Banking session with ASN Bank (valid 180 days under PSD2)

## One-off setup at Enable Banking

Enable Banking is the licensed TPP; you are a PSU (Payment Service User) authorising access to your own account. You only need to do this once per 180 days.

1. Sign up at [enablebanking.com](https://enablebanking.com/) and open the **Control Panel**.
2. Go to **API applications**, click **Register a new application**, give it a name (e.g. *Home Assistant*) and add a redirect URL you control (any valid HTTPS URL works for the manual flow — you can even use `https://enablebanking.com/`).
3. Download the application's **private key** and note the **application id**. Keep the private key somewhere safe — you'll sign JWTs with it.
4. Generate a JWT. Enable Banking accepts JWTs signed with your application's private key (RS256) with the following payload:
   ```json
   {
     "iss": "enablebanking.com",
     "aud": "api.enablebanking.com",
     "iat": <now>,
     "exp": <now + 24h>,
     "kid": "<your application id>"
   }
   ```
   The Enable Banking docs have a ready-to-use Python snippet; you can run it locally and copy the resulting JWT string.
5. Start a session with ASN Bank. Two options:
   - **API:** `POST /auth` with body `{"access": {"valid_until": "..."}, "aspsp": {"name": "ASN Bank", "country": "NL"}, "psu_type": "personal", "state": "anything", "redirect_url": "<your redirect>"}`. Follow the returned `url` in a browser, log in at ASN, and the redirect URL will contain a `code` parameter. Then `POST /sessions` with that code to obtain the `session_id`.
   - **Quick-start helper:** the Enable Banking docs ship a small Python quick-start script that performs both steps; run it and copy the `session_id` from the output.
6. You now have a **JWT** and a **session ID**. Both go into the Home Assistant config flow below.

A full walkthrough lives at [enablebanking.com/docs/api/quick-start/](https://enablebanking.com/docs/api/quick-start/).

### PSD2 consent lifetime

Every PSD2 Account Information consent is valid for **180 days**. After that the session returns 404 / 410 and this integration triggers a reauth — generate a new session (steps 5–6) and paste the fresh `session_id` into the reauthentication dialog.

## Installation

### HACS (custom repository)

[![Add Repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=SurfHost&repository=ha-asnbank-balance&category=integration)

Or manually in HACS:

1. Open HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/SurfHost/ha-asnbank-balance` with category **Integration**.
3. Search for **ASN Bank Balance** and install it.
4. Restart Home Assistant.

### Manual

1. Copy `custom_components/asnbank/` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=asnbank)

Or manually:

1. **Settings → Devices & Services → Add Integration → ASN Bank Balance.**
2. Paste the **JWT** and **session ID** from the Enable Banking setup above.
3. The integration verifies the credentials by fetching the session and creates one balance sensor per account.

### Options

| Option | Default | Description |
|--------|---------|-------------|
| Update interval (seconds) | 21600 (6 h) | How often to poll. Minimum 3600, maximum 86400. Values below 6 h may breach the PSD2 4-polls-per-day limit and cause ASN to throttle you. |

## Sensors

### Balance (per account)
- **State**: EUR balance (closing booked preferred, with fallback to interim available)
- **Name**: `Balance <IBAN>` so multi-account setups don't collide
- **Attributes**: `iban`, `account_name`, `product`, `currency`, `balance_type`, `reference_date`, `last_updated`

## Lovelace example

A minimal "big tile" view:

```yaml
type: vertical-stack
cards:
  - type: tile
    entity: sensor.asn_bank_balance_nl00asnb0123456789
    name: ASN Betaalrekening
    icon: mdi:bank
    color: blue
    features_position: bottom
  - type: markdown
    content: >-
      **IBAN:** {{ state_attr('sensor.asn_bank_balance_nl00asnb0123456789', 'iban') }}

      **Updated:** {{ state_attr('sensor.asn_bank_balance_nl00asnb0123456789', 'last_updated') }}
```

Replace the entity id with the one Home Assistant created for your account.

## Rate limits

- **PSD2 (the regulation)**: max 4 unattended Account Information polls per day per consent. Stay at 6 h or higher. The integration's default is exactly 4/day.
- **Enable Banking free tier**: generous for a single PSU. There is no per-request charge on the personal tier.
- **ASN Bank**: no published limit beyond the PSD2 one.

## Troubleshooting

- **"Invalid auth"** at setup — your JWT is likely expired (default 24 h), or signed with the wrong key.
- **"Invalid session"** — the PSD2 consent has been revoked or hit 180 days; generate a new session at Enable Banking.
- **Balance sensor shows `unavailable`** — the Enable Banking call succeeded but the account had no balance matching the preferred types. Check the Home Assistant debug log with `logger: homeassistant.components.asnbank: debug`.

## License

MIT
