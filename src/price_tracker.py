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

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "monitors.json"
STATE = ROOT / "data" / "state.json"
README = ROOT / "README.md"


HEADERS = {
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------
# JSON / FILE HELPERS
# ---------------------------------------------------------

def load_json(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ---------------------------------------------------------
# PRICE PARSING
# ---------------------------------------------------------

def parse_price(value):
    if value is None:
        return None

    text = str(value).replace("\xa0", " ").strip()

    # German format: 1.234,56 €
    match = re.search(
        r"(\d{1,5}(?:[.\s]\d{3})*,\d{2})\s*€?",
        text
    )

    if match:
        cleaned = (
            match.group(1)
            .replace(".", "")
            .replace(" ", "")
            .replace(",", ".")
        )

        try:
            return float(cleaned)
        except ValueError:
            pass

    # International format: 1,234.56 €
    match = re.search(
        r"(\d{1,5}(?:,\d{3})*\.\d{2})\s*€?",
        text
    )

    if match:
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            pass

    # Plain decimal value
    match = re.fullmatch(
        r"\s*(\d{2,5}(?:[.,]\d{1,2})?)\s*",
        text
    )

    if match:
        raw = match.group(1)

        try:
            if "," in raw and "." not in raw:
                return float(raw.replace(",", "."))

            return float(raw.replace(",", ""))
        except ValueError:
            pass

    # € 799 / EUR 799
    match = re.search(
        r"(?:€|EUR)\s*(\d{2,5})(?:\s|$)",
        text,
        flags=re.I
    )

    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    return None


def valid_price(price):
    return price is not None and 50 <= price <= 5000


# ---------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------

def jsonld_prices(soup):
    prices = []

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(
                script.string or script.get_text()
            )
        except Exception:
            continue

        stack = data if isinstance(data, list) else [data]

        while stack:
            item = stack.pop()

            if isinstance(item, dict):

                if "offers" in item:
                    offers = item["offers"]

                    if isinstance(offers, list):
                        stack.extend(offers)
                    else:
                        stack.append(offers)

                if isinstance(item.get("price"), (str, int, float)):
                    price = parse_price(item["price"])

                    if valid_price(price):
                        prices.append(price)

                for value in item.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)

            elif isinstance(item, list):
                stack.extend(item)

    return prices


# ---------------------------------------------------------
# AMAZON
# ---------------------------------------------------------

def amazon_price(soup):
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
        for element in soup.select(selector):
            price = parse_price(
                element.get_text(" ", strip=True)
            )

            if valid_price(price):
                return price

    # Whole + fraction
    for container in soup.select(
        "#corePriceDisplay_desktop_feature_div .a-price, "
        "#corePrice_feature_div .a-price"
    ):
        whole = container.select_one(".a-price-whole")
        fraction = container.select_one(".a-price-fraction")

        if whole and fraction:
            price = parse_price(
                f"{whole.get_text(strip=True)}."
                f"{fraction.get_text(strip=True)}"
            )

            if valid_price(price):
                return price

    # JSON-LD fallback
    prices = jsonld_prices(soup)

    if prices:
        return prices[0]

    return None


def fetch_amazon(page, url):
    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45000
        )

        page.wait_for_timeout(3000)

    except Exception as exc:
        return None, f"HTTP_ERROR: {type(exc).__name__}"

    text = page.locator("body").inner_text(
        timeout=10000
    ).lower()

    bot_words = [
        "captcha",
        "robot check",
        "enter the characters you see",
        "type the characters",
        "are you a robot",
    ]

    if any(word in text for word in bot_words):
        return None, "AMAZON_BOT_CHECK"

    soup = BeautifulSoup(
        page.content(),
        "html.parser"
    )

    price = amazon_price(soup)

    if price is not None:
        return price, "OK"

    return None, "AMAZON_PRICE_NOT_FOUND"


# ---------------------------------------------------------
# MEDIAMARKT
# ---------------------------------------------------------

def mediamarkt_price(soup, page):
    candidates = []

    selectors = [
        '[data-test="product-price"]',
        '[data-testid="product-price"]',
        '[data-test*="price"]',
        '[data-testid*="price"]',
        '[class*="price"]',
        '[class*="Price"]',
    ]

    for selector in selectors:
        try:
            elements = soup.select(selector)

            for element in elements[:100]:

                # Ignore prices inside strike/del elements
                if element.find_parent(["del", "s", "strike"]):
                    continue

                text = element.get_text(
                    " ",
                    strip=True
                )

                if "€" not in text and "EUR" not in text:
                    continue

                price = parse_price(text)

                if valid_price(price):
                    candidates.append(price)

        except Exception:
            continue

    # Structured metadata
    for element in soup.select(
        '[itemprop="price"]'
    ):
        value = element.get("content")

        if value:
            price = parse_price(value)

            if valid_price(price):
                candidates.append(price)

    # JSON-LD
    candidates.extend(
        jsonld_prices(soup)
    )

    # Remove duplicates
    candidates = sorted(
        set(round(p, 2) for p in candidates)
    )

    if not candidates:
        return None

    # Prefer prices around the normal monitor price range.
    # Do not blindly use every number found on the page.
    sensible = [
        p for p in candidates
        if 100 <= p <= 3000
    ]

    if sensible:
        return sensible[0]

    return candidates[0]


def fetch_mediamarkt(page, url):
    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45000
        )

        page.wait_for_timeout(4000)

    except Exception as exc:
        return None, f"HTTP_ERROR: {type(exc).__name__}"

    body_text = page.locator("body").inner_text(
        timeout=10000
    )

    lower = body_text.lower()

    if (
        "access denied" in lower
        or "forbidden" in lower
        or "request blocked" in lower
    ):
        return None, "MEDIAMARKT_BLOCKED"

    soup = BeautifulSoup(
        page.content(),
        "html.parser"
    )

    price = mediamarkt_price(
        soup,
        page
    )

    if price is not None:
        return price, "OK"

    return None, "MEDIAMARKT_PRICE_NOT_FOUND"


# ---------------------------------------------------------
# IDEALO
# ---------------------------------------------------------

def idealo_price(soup, page):
    candidates = []

    selectors = [
        '[data-testid*="price"]',
        '[data-test*="price"]',
        '[class*="price"]',
        '[class*="Price"]',
        '[aria-label*="Preis"]',
    ]

    for selector in selectors:

        try:
            elements = soup.select(selector)

            for element in elements[:150]:

                if element.find_parent(
                    ["del", "s", "strike"]
                ):
                    continue

                text = element.get_text(
                    " ",
                    strip=True
                )

                if "€" not in text and "EUR" not in text:
                    continue

                price = parse_price(text)

                if valid_price(price):
                    candidates.append(price)

        except Exception:
            continue

    # Metadata
    for element in soup.select(
        '[itemprop="price"]'
    ):
        value = element.get("content")

        if value:
            price = parse_price(value)

            if valid_price(price):
                candidates.append(price)

    # JSON-LD
    candidates.extend(
        jsonld_prices(soup)
    )

    candidates = sorted(
        set(round(p, 2) for p in candidates)
    )

    if not candidates:
        return None

    sensible = [
        p for p in candidates
        if 100 <= p <= 3000
    ]

    if sensible:
        return sensible[0]

    return candidates[0]


def fetch_idealo(page, url):
    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45000
        )

        page.wait_for_timeout(4000)

    except Exception as exc:
        return None, f"HTTP_ERROR: {type(exc).__name__}"

    body_text = page.locator("body").inner_text(
        timeout=10000
    )

    lower = body_text.lower()

    if (
        "access denied" in lower
        or "forbidden" in lower
        or "request blocked" in lower
        or "captcha" in lower
    ):
        return None, "IDEALO_BLOCKED"

    soup = BeautifulSoup(
        page.content(),
        "html.parser"
    )

    price = idealo_price(
        soup,
        page
    )

    if price is not None:
        return price, "OK"

    return None, "IDEALO_PRICE_NOT_FOUND"


# ---------------------------------------------------------
# BROWSER
# ---------------------------------------------------------

def fetch_price(page, url, source):

    if not url:
        return None, "NOT_CONFIGURED"

    if source == "amazon":
        return fetch_amazon(page, url)

    if source == "mediamarkt":
        return fetch_mediamarkt(page, url)

    if source == "idealo":
        return fetch_idealo(page, url)

    return None, "UNKNOWN_SOURCE"


# ---------------------------------------------------------
# EMAIL
# ---------------------------------------------------------

def smtp_settings():
    return (
        os.environ.get(
            "ALERT_EMAIL_FROM",
            ""
        ).strip(),

        os.environ.get(
            "ALERT_EMAIL_TO",
            ""
        ).strip(),

        os.environ.get(
            "SMTP_PASSWORD",
            ""
        ).strip(),
    )


def send_email(changes, demo=False):

    sender, recipient, password = smtp_settings()

    if not sender or not recipient or not password:
        raise RuntimeError(
            "Email secrets are missing. "
            "Set ALERT_EMAIL_FROM, ALERT_EMAIL_TO "
            "and SMTP_PASSWORD in GitHub Secrets."
        )

    lines = [
        "OLED Monitor Price Tracker",
        "",
        (
            "DEMO TEST — no real price change was used."
            if demo
            else ""
        ),
        f"Checked: "
        f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        "",
    ]

    for change in changes:

        direction = (
            "DECREASED"
            if change["new"] < change["old"]
            else "INCREASED"
        )

        percentage = pct_change(
            change["old"],
            change["new"]
        )

        percentage_text = (
            f" ({percentage:+.2f}%)"
            if percentage is not None
            else ""
        )

        lines += [
            f"{change['monitor']} — {change['source']}",
            (
                f"{direction}: "
                f"€{change['old']:.2f} → "
                f"€{change['new']:.2f}"
                f"{percentage_text}"
            ),
            change["url"],
            "",
        ]

    message = EmailMessage()

    prefix = "[TEST] " if demo else ""

    message["Subject"] = (
        f"{prefix}OLED price change: "
        f"{len(changes)} change(s)"
    )

    message["From"] = sender
    message["To"] = recipient

    message.set_content(
        "\n".join(lines)
    )

    context = ssl.create_default_context()

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        context=context
    ) as smtp:

        smtp.login(
            sender,
            password
        )

        smtp.send_message(
            message
        )


# ---------------------------------------------------------
# PRICE CHANGE
# ---------------------------------------------------------

def pct_change(old, new):

    if old in (None, 0) or new is None:
        return None

    return (
        (new - old) / old
    ) * 100


# ---------------------------------------------------------
# DEMO EMAIL
# ---------------------------------------------------------

def demo_changes(config):

    examples = [
        (
            "ASUS ROG Strix OLED XG32UCWMG",
            "amazon",
            799.99,
            749.99,
        ),
        (
            "Samsung Odyssey OLED G8 G80SD",
            "mediamarkt",
            798.87,
            829.99,
        ),
        (
            "LG UltraGear OLED 32GX850A-B",
            "idealo",
            629.00,
            599.00,
        ),
    ]

    by_name = {
        monitor["name"]: monitor
        for monitor in config.get(
            "monitors",
            []
        )
    }

    rows = []

    for name, source, old, new in examples:

        monitor = by_name.get(
            name,
            {}
        )

        rows.append({
            "monitor": name,
            "source": source,
            "old": old,
            "new": new,
            "url": monitor.get(
                source,
                "https://example.com/demo"
            ),
        })

    return rows


# ---------------------------------------------------------
# README TABLE
# ---------------------------------------------------------

def build_readme_table(state):

    lines = [
        "## Current price table",
        "",
        "| Monitor | Amazon.de | MediaMarkt.de | Idealo.de | Last check |",
        "|---|---:|---:|---:|---|",
    ]

    for monitor in state.get(
        "monitors",
        []
    ):

        values = {
            item["source"]: item
            for item in monitor.get(
                "sources",
                []
            )
        }

        cells = []

        for source in (
            "amazon",
            "mediamarkt",
            "idealo"
        ):

            item = values.get(
                source,
                {}
            )

            price = item.get(
                "price"
            )

            status = item.get(
                "status",
                ""
            )

            url = item.get(
                "url",
                ""
            )

            if price is not None:

                if url:
                    cell = (
                        f"[€{price:.2f}]"
                        f"({url})"
                    )
                else:
                    cell = (
                        f"€{price:.2f}"
                    )

            else:

                cell = (
                    f"`{status or 'N/A'}`"
                )

            cells.append(cell)

        checked = monitor.get(
            "last_checked",
            ""
        )

        lines.append(
            f"| {monitor['name']} | "
            f"{cells[0]} | "
            f"{cells[1]} | "
            f"{cells[2]} | "
            f"{checked} |"
        )

    block = "\n".join(lines)

    readme = (
        README.read_text(
            encoding="utf-8"
        )
        if README.exists()
        else ""
    )

    marker = "## Current price table"

    if marker in readme:

        readme = (
            readme.split(marker)[0]
            .rstrip()
            + "\n\n"
            + block
            + "\n"
        )

    else:

        readme = (
            readme.rstrip()
            + "\n\n"
            + block
            + "\n"
        )

    README.write_text(
        readme,
        encoding="utf-8"
    )


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--demo-notification",
        action="store_true",
        help=(
            "Send a clearly labelled "
            "test email without checking prices."
        ),
    )

    args = parser.parse_args()

    config = load_json(
        CONFIG,
        {"monitors": []}
    )

    # -----------------------------------------------------
    # DEMO EMAIL
    # -----------------------------------------------------

    if args.demo_notification:

        changes = demo_changes(
            config
        )

        send_email(
            changes,
            demo=True
        )

        print(
            "Demo notification sent successfully."
        )

        return

    # -----------------------------------------------------
    # LOAD OLD STATE
    # -----------------------------------------------------

    state = load_json(
        STATE,
        {"monitors": []}
    )

    state_by_model = {
        monitor["model"]: monitor
        for monitor in state.get(
            "monitors",
            []
        )
    }

    changes = []

    now = datetime.now(
        timezone.utc
    ).astimezone().isoformat(
        timespec="minutes"
    )

    new_state = {
        "updated_at": now,
        "monitors": [],
    }

    # -----------------------------------------------------
    # BROWSER
    # -----------------------------------------------------

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={
                "width": 1440,
                "height": 900,
            },
            extra_http_headers=HEADERS,
        )

        page = context.new_page()

        # -------------------------------------------------
        # MONITORS
        # -------------------------------------------------

        for monitor in config.get(
            "monitors",
            []
        ):

            old_monitor = state_by_model.get(
                monitor["model"],
                {}
            )

            old_sources = {
                source["source"]: source
                for source in old_monitor.get(
                    "sources",
                    []
                )
            }

            source_rows = []

            for source in (
                "amazon",
                "mediamarkt",
                "idealo"
            ):

                url = monitor.get(
                    source,
                    ""
                )

                print(
                    f"Checking "
                    f"{monitor['name']} "
                    f"| {source}"
                )

                try:

                    price, status = fetch_price(
                        page,
                        url,
                        source
                    )

                except PlaywrightTimeoutError:

                    price = None
                    status = (
                        "BROWSER_TIMEOUT"
                    )

                except Exception as exc:

                    price = None
                    status = (
                        f"ERROR: "
                        f"{type(exc).__name__}"
                    )

                old = old_sources.get(
                    source,
                    {}
                ).get(
                    "price"
                )

                if (
                    price is not None
                    and old is not None
                    and round(price, 2)
                    != round(float(old), 2)
                ):

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

                print(
                    f"  Result: "
                    f"{price if price is not None else 'N/A'} "
                    f"({status})"
                )

            if price is None:
    try:
        title = page.title()
        body_sample = page.locator("body").inner_text(
            timeout=5000
        )[:1500]

        print(f"  Page title: {title}")
        print("  Page text sample:")
        print(body_sample)
    except Exception as exc:
        print(
            f"  Could not collect diagnostic page text: "
            f"{type(exc).__name__}"
        )

            new_state["monitors"].append({
                "name": monitor["name"],
                "model": monitor["model"],
                "last_checked": now,
                "sources": source_rows,
            })

        browser.close()

    # -----------------------------------------------------
    # SAVE
    # -----------------------------------------------------

    save_json(
        STATE,
        new_state
    )

    build_readme_table(
        new_state
    )

    # -----------------------------------------------------
    # EMAIL ONLY IF PRICE CHANGED
    # -----------------------------------------------------

    if changes:

        send_email(
            changes
        )

        print(
            f"Email sent for "
            f"{len(changes)} price change(s)."
        )

    else:

        print(
            "No price changes. "
            "No email sent."
        )

    print(
        f"Checked "
        f"{len(config.get('monitors', []))} "
        f"monitors."
    )

    print(
        f"Price changes: "
        f"{len(changes)}"
    )


if __name__ == "__main__":
    main()
