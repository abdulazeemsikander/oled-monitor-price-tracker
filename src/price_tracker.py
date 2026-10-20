import argparse
import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "monitors.json"
STATE = ROOT / "data" / "state.json"
README = ROOT / "README.md"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_price(value):
    if value is None:
        return None
    text = str(value).replace("\xa0", " ").strip()

    # German format: 1.234,56 €
    m = re.search(r"(\d{1,5}(?:[.\s]\d{3})*,\d{2})\s*€?", text)
    if m:
        cleaned = m.group(1).replace(".", "").replace(" ", "").replace(",", ".")
        return float(cleaned)

    # International format: 1,234.56 €
    m = re.search(r"(\d{1,5}(?:,\d{3})*\.\d{2})\s*€?", text)
    if m:
        return float(m.group(1).replace(",", ""))

    # Plain decimal value, useful for structured metadata such as 799.99.
    m = re.fullmatch(r"\s*(\d{2,5}(?:[.,]\d{1,2})?)\s*", text)
    if m:
        raw = m.group(1)
        if raw.count(",") == 1 and raw.count(".") == 0:
            return float(raw.replace(",", "."))
        return float(raw.replace(",", ""))

    m = re.search(r"(?:€|EUR)\s*(\d{2,5})(?:\s|$)", text)
    if m:
        return float(m.group(1))
    return None


def _valid_price(p):
    return p is not None and 50 <= p <= 5000


def _jsonld_prices(soup):
    prices = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "offers" in item:
                    offers = item["offers"]
                    stack.extend(offers if isinstance(offers, list) else [offers])
                if isinstance(item.get("price"), (str, int, float)):
                    p = parse_price(item["price"])
                    if _valid_price(p):
                        prices.append(p)
                for value in item.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
    return prices


def amazon_price_candidates(soup):
    """Extract likely current Amazon buy-box prices, avoiding old/list prices."""
    candidates = []

    # Highest-confidence current-price selectors first.
    selectors = [
        "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#apex_desktop .a-price:not(.a-text-price) .a-offscreen",
        "#price_inside_buybox .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "[data-a-color='price'] .a-offscreen",
    ]
    for selector in selectors:
        for tag in soup.select(selector):
            p = parse_price(tag.get_text(" ", strip=True))
            if _valid_price(p):
                candidates.append(p)

    # Amazon sometimes exposes the current price as whole + fraction spans.
    for container in soup.select("#corePriceDisplay_desktop_feature_div .a-price, #corePrice_feature_div .a-price"):
        whole = container.select_one(".a-price-whole")
        frac = container.select_one(".a-price-fraction")
        if whole and frac:
            p = parse_price(f"{whole.get_text(strip=True)}.{frac.get_text(strip=True)}")
            if _valid_price(p):
                candidates.append(p)

    # Embedded Amazon JSON often contains priceAmount/priceToPay fields.
    raw = str(soup)
    for pattern in [
        r'"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        r'"priceToPay"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)',
        r'"displayPrice"\s*:\s*"([^\"]+€[^\"]*)"',
    ]:
        for match in re.findall(pattern, raw, flags=re.I):
            p = parse_price(match)
            if _valid_price(p):
                candidates.append(p)

    # JSON-LD is the final fallback because some Amazon pages expose an
    # MSRP/list price there even when the live buy-box price is elsewhere.
    candidates.extend(_jsonld_prices(soup))
    return candidates


def price_candidates(soup, source):
    if source == "amazon":
        return amazon_price_candidates(soup)

    candidates = []

    # Structured metadata first.
    for tag in soup.select('[itemprop="price"]'):
        if tag.get("content"):
            candidates.append(tag.get("content"))
        if tag.get_text(strip=True):
            candidates.append(tag.get_text(" ", strip=True))

    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in {"product:price:amount", "og:price:amount", "price"} and meta.get("content"):
            candidates.append(meta["content"])

    selectors = [
        '[class*="price"]',
        '[data-testid*="price"]',
        '[data-test*="price"]',
        '[data-qa*="price"]',
        '[aria-label*="Preis"]',
        '[class*="Price"]',
    ]
    for selector in selectors:
        for tag in soup.select(selector)[:80]:
            text = tag.get_text(" ", strip=True)
            if "€" in text or "EUR" in text:
                candidates.append(text)

    page_text = soup.get_text(" ", strip=True)
    candidates.extend(re.findall(r"\b\d{2,5}[.,]\d{2}\s*€", page_text))

    prices = []
    for c in candidates:
        p = parse_price(c)
        if _valid_price(p):
            prices.append(p)
    return prices


def fetch_amazon_price(url):
    if not url:
        return None, "NOT_CONFIGURED"

    # Amazon can occasionally serve a bot-check page. Try two common product URL forms.
    urls = [url]
    parsed = urlparse(url)
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", parsed.path, re.I)
    if match:
        asin = match.group(1)
        urls.append(f"https://www.amazon.de/dp/{asin}?th=1&psc=1")
        urls.append(f"https://www.amazon.de/gp/product/{asin}?th=1&psc=1")

    last_status = "PRICE_NOT_FOUND"
    session = requests.Session()
    session.headers.update(HEADERS)

    for candidate_url in urls:
        try:
            r = session.get(candidate_url, timeout=30, allow_redirects=True)
            if r.status_code in (429, 503):
                last_status = f"AMAZON_BLOCKED_HTTP_{r.status_code}"
                continue
            r.raise_for_status()
        except Exception as exc:
            last_status = f"HTTP_ERROR: {type(exc).__name__}"
            continue

        lower = r.text.lower()
        if "captcha" in lower or "enter the characters you see" in lower or "robot check" in lower:
            last_status = "AMAZON_BOT_CHECK"
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        prices = amazon_price_candidates(soup)
        if prices:
            # Current buy-box selectors are already ordered first. Use the first
            # valid candidate instead of accidentally choosing an unrelated price.
            return prices[0], "OK"

        last_status = "AMAZON_PRICE_NOT_FOUND"

    return None, last_status


def fetch_price(url, source):
    if not url:
        return None, "NOT_CONFIGURED"
    if source == "amazon":
        return fetch_amazon_price(url)

    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as exc:
        return None, f"HTTP_ERROR: {type(exc).__name__}"

    soup = BeautifulSoup(r.text, "html.parser")
    prices = price_candidates(soup, source)
    if not prices:
        return None, "PRICE_NOT_FOUND"
    return min(prices), "OK"


def pct_change(old, new):
    if old in (None, 0) or new is None:
        return None
    return ((new - old) / old) * 100


def _smtp_settings():
    return (
        os.environ.get("ALERT_EMAIL_FROM", "").strip(),
        os.environ.get("ALERT_EMAIL_TO", "").strip(),
        os.environ.get("SMTP_PASSWORD", "").strip(),
    )


def send_email(changes, demo=False):
    sender, recipient, password = _smtp_settings()
    if not sender or not recipient or not password:
        raise RuntimeError(
            "Email secrets are missing. Set ALERT_EMAIL_FROM, ALERT_EMAIL_TO and SMTP_PASSWORD in GitHub Secrets."
        )

    lines = [
        "OLED Monitor Price Tracker",
        "",
        "DEMO TEST — no real price change was used." if demo else "",
        f"Checked: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        "",
    ]

    for c in changes:
        direction = "DECREASED" if c["new"] < c["old"] else "INCREASED"
        pct = pct_change(c["old"], c["new"])
        pct_text = f" ({pct:+.2f}%)" if pct is not None else ""
        lines += [
            f"{c['monitor']} — {c['source']}",
            f"{direction}: €{c['old']:.2f} → €{c['new']:.2f}{pct_text}",
            c["url"],
            "",
        ]

    msg = EmailMessage()
    prefix = "[TEST] " if demo else ""
    msg["Subject"] = f"{prefix}OLED price change: {len(changes)} change(s)"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content("\n".join(lines))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def demo_changes(config):
    demo_rows = []
    examples = [
        ("ASUS ROG Strix OLED XG32UCWMG", "amazon", 799.99, 749.99),
        ("Samsung Odyssey OLED G8 G80SD", "mediamarkt", 798.87, 829.99),
        ("LG UltraGear OLED 32GX850A-B", "idealo", 629.00, 599.00),
    ]
    by_name = {m["name"]: m for m in config.get("monitors", [])}
    for name, source, old, new in examples:
        monitor = by_name.get(name, {})
        demo_rows.append({
            "monitor": name,
            "source": source,
            "old": old,
            "new": new,
            "url": monitor.get(source, "https://example.com/demo"),
        })
    return demo_rows


def build_readme_table(state):
    lines = [
        "## Current price table",
        "",
        "| Monitor | Amazon.de | MediaMarkt.de | Idealo.de | Last check |",
        "|---|---:|---:|---:|---|",
    ]

    for monitor in state.get("monitors", []):
        values = {x["source"]: x for x in monitor.get("sources", [])}
        cells = []
        for source in ("amazon", "mediamarkt", "idealo"):
            item = values.get(source, {})
            price = item.get("price")
            status = item.get("status", "")
            url = item.get("url", "")
            if price is not None:
                cell = f"[€{price:.2f}]({url})" if url else f"€{price:.2f}"
            else:
                cell = f"`{status or 'N/A'}`"
            cells.append(cell)

        checked = monitor.get("last_checked", "")
        lines.append(
            f"| {monitor['name']} | {cells[0]} | {cells[1]} | {cells[2]} | {checked} |"
        )

    block = "\n".join(lines)
    readme = README.read_text(encoding="utf-8") if README.exists() else ""
    marker_start = "## Current price table"
    if marker_start in readme:
        readme = readme.split(marker_start)[0].rstrip() + "\n\n" + block + "\n"
    else:
        readme = readme.rstrip() + "\n\n" + block + "\n"
    README.write_text(readme, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo-notification", action="store_true", help="Send a clearly labelled test email without checking prices.")
    args = parser.parse_args()

    config = load_json(CONFIG, {"monitors": []})

    if args.demo_notification:
        changes = demo_changes(config)
        send_email(changes, demo=True)
        print("Demo notification sent successfully.")
        return

    state = load_json(STATE, {"monitors": []})
    state_by_model = {m["model"]: m for m in state.get("monitors", [])}
    changes = []
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")
    new_state = {"updated_at": now, "monitors": []}

    for monitor in config["monitors"]:
        old_monitor = state_by_model.get(monitor["model"], {})
        old_sources = {s["source"]: s for s in old_monitor.get("sources", [])}
        source_rows = []

        for source in ("amazon", "mediamarkt", "idealo"):
            url = monitor.get(source, "")
            price, status = fetch_price(url, source)
            old = old_sources.get(source, {}).get("price")

            if price is not None and old is not None and round(price, 2) != round(old, 2):
                changes.append({
                    "monitor": monitor["name"],
                    "source": source,
                    "old": float(old),
                    "new": float(price),
                    "url": url,
                })

            source_rows.append({
                "source": source,
                "url": url,
                "price": price,
                "status": status,
            })

        new_state["monitors"].append({
            "name": monitor["name"],
            "model": monitor["model"],
            "last_checked": now,
            "sources": source_rows,
        })

    save_json(STATE, new_state)
    build_readme_table(new_state)

    if changes:
        send_email(changes)

    print(f"Checked {len(config['monitors'])} monitors.")
    print(f"Price changes: {len(changes)}")


if __name__ == "__main__":
    main()
