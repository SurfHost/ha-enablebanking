# Changelog

Releases up to and including 0.6.11 are described in the
[GitHub release notes](https://github.com/SurfHost/ha-enablebanking/releases).

## [0.7.0] - 2026-11-05

### Added
- **`enablebanking.get_transactions` action** ([#2](https://github.com/SurfHost/ha-enablebanking/issues/2)). Fetches one account's transactions for a date range and returns them as an action response, with signed amounts (negative for money going out), a `total`, and the bank's raw payload per transaction. `date_to` may be in the future to ask for scheduled payments; whether the bank returns them is up to the bank. It only runs when called, and each call spends the account's PSD2 quota for transactions.

### Fixed
- **Non-euro accounts showed `€`** ([#3](https://github.com/SurfHost/ha-enablebanking/issues/3)). The unit was hard-coded to euro, so NOK and SEK balances were labelled as euros. The unit now follows the account currency. Euro accounts keep `€`, so their unit and long-term statistics do not change; other currencies use the ISO code. Home Assistant may ask once to update the unit of the long-term statistics for a NOK or SEK sensor (**Developer tools > Statistics**). The `currency` attribute also no longer falls back to EUR when the balance leaves the currency out but the account details have it.
- **Balance lagged a day behind** ([#12](https://github.com/SurfHost/ha-enablebanking/issues/12)). The integration preferred closing booked (`CLBD`), which only moves when the bank closes the day. It now prefers interim available (`ITAV`), which is updated during the day, and falls back to `CLBD` for banks that do not report it. Check the `balance_type` attribute to see which one your bank delivers. For some banks the available balance includes an overdraft or credit limit.

## [0.6.13] - 2026-10-11

### Fixed
- **Consent could not be renewed before it expired.** The 14-day expiry warning told you to click **Reconfigure**, but the integration had no reconfigure step, so Home Assistant never offered one. Renewal was only possible after the consent had lapsed and the reauth card appeared, by which time balances were already going stale. Each bank entry now has **Reconfigure** in its **⋮** menu. It reuses the stored private key and application ID, goes straight to a new bank link, and swaps in the new session once you paste the code. Reauth could not do this: its fast path revalidates the still-live session and finishes without reaching the bank.
- **The expiry warning pointed at a button that does not exist.** It now links to the Enable Banking integration page and names the **⋮** menu, and it disappears by itself once the consent is renewed instead of lingering until dismissed.
- **The reauth success message was not shown.** Since Home Assistant 2026.10, a flow that leaves the abort reason out gets Home Assistant's generic text instead of the integration's own. The reason is now passed explicitly, so "your balances refresh on the next scheduled poll" shows again after reauth, and after reconfigure too.

## [0.6.12] - 2026-10-11

### Fixed
- **CI failed on Home Assistant 2026.10.** Home Assistant replaced voluptuous with probatio in 2026.9, and since 2026.10 `async_show_form` is typed to take a `probatio.Schema`. The config flow still imported `voluptuous`, so strict `mypy` rejected every form schema. It now imports `probatio` and falls back to `voluptuous` on Home Assistant 2026.4 to 2026.8, which do not ship probatio. Behaviour is unchanged: at runtime Home Assistant already redirects `voluptuous` imports to probatio.
