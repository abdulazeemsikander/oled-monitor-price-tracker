# OLED Monitor Price Tracker

Free, cloud-hosted hourly price tracker for four OLED monitors. It checks **Amazon.de, MediaMarkt.de and Idealo.de**, stores the latest observed prices, and sends an email only when a price changes.

## Monitors

- ASUS ROG Strix OLED XG32UCWMG
- Samsung Odyssey OLED G8 G80SD (LS32DG800SUXEN)
- LG UltraGear OLED 32GX850A-B
- LG UltraGear 32GS94UX-B

## Notifications

- Every hour, the GitHub Actions workflow checks all configured sources.
- If a price decreases **or increases**, the bot sends one email containing all changes from that run.
- If nothing changed, it sends nothing.
- The **Test OLED Price Notification** workflow sends a clearly labelled `[TEST]` email using fake changes. It does not alter the price state.

## Amazon support

Amazon product URLs are preconfigured using these ASINs:

- ASUS: `B0BSH3JKQB`
- Samsung: `B0D49Z3N9K`
- LG 32GX850A-B: `B0FT3P9Q36`
- LG 32GS94UX-B: `B0DK6Z1869`

The Amazon parser looks for the current buy-box price, structured product data, and Amazon's embedded price fields. Amazon may sometimes return a bot-check page or temporarily block automated requests; in that case the table records `AMAZON_BOT_CHECK`/`AMAZON_BLOCKED_HTTP_*` rather than inventing a price. The other sources continue to run.

## Free setup

1. Create a free GitHub repository and upload this project.
2. In **Settings → Secrets and variables → Actions**, add:
   - `ALERT_EMAIL_FROM` = your Gmail address
   - `ALERT_EMAIL_TO` = the email address where you want alerts
   - `SMTP_PASSWORD` = a Gmail **App Password** (not your normal Gmail password)
3. Run **Actions → Test OLED Price Notification → Run workflow**. You should receive a `[TEST]` email.
4. After the test succeeds, the hourly workflow runs automatically.

## Important Gmail note

For Gmail SMTP, enable 2-Step Verification and create an App Password. Put the 16-character App Password in `SMTP_PASSWORD`.

## Current price table

| Monitor | Amazon.de | MediaMarkt.de | Idealo.de | Last check |
|---|---:|---:|---:|---|
| ASUS ROG Strix OLED XG32UCWMG | `AMAZON_PRICE_NOT_FOUND` | `MEDIAMARKT_PRICE_NOT_FOUND` | `IDEALO_PRICE_NOT_FOUND` | 2026-10-20T11:20+00:00 |
| Samsung Odyssey OLED G8 G80SD | `AMAZON_PRICE_NOT_FOUND` | `MEDIAMARKT_PRICE_NOT_FOUND` | `IDEALO_PRICE_NOT_FOUND` | 2026-10-20T11:20+00:00 |
| LG UltraGear OLED 32GX850A-B | `AMAZON_PRICE_NOT_FOUND` | `MEDIAMARKT_PRICE_NOT_FOUND` | `IDEALO_PRICE_NOT_FOUND` | 2026-10-20T11:20+00:00 |
| LG UltraGear 32GS94UX-B | `AMAZON_PRICE_NOT_FOUND` | `MEDIAMARKT_PRICE_NOT_FOUND` | `IDEALO_PRICE_NOT_FOUND` | 2026-10-20T11:20+00:00 |
