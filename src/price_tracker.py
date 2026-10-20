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
from playwright.sync_api import sync_playwright


# ============================================================
# PATHS
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

CONFIG = ROOT / "config" / "monitors.json"
STATE = ROOT / "data" / "state.json"
README = ROOT / "README.md"


# ============================================================
# BROWSER SETTINGS
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


# ============================================================
# FILE HELPERS
# ============================================================

def load_json(path, default):
    """Load JSON safely."""
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    """Save JSON with readable formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ============================================================
# PRICE PARSING
# ============================================================

def parse_price(value):
    """
    Convert German/international price strings to float.

    Examples:
        799,99 €       -> 799.99
        1.299,99 €     -> 1299.99
        799.99 €       -> 799.99
        EUR 799.99     -> 799.99
        799.99         -> 799.99
    """

    if value is None:
        return None

    text = str(value)

    text = (
        text.replace("\xa0", " ")
        .replace("\u202f", " ")
        .strip()
    )

    # --------------------------------------------------------
    # German format
    # 799,99
    # 1.299,99
    # --------------------------------------------------------

    match = re.search(
        r"(\d{1,5}(?:[.\s]\d{3})*,\d{2})",
        text,
    )

    if match:
        raw = match.group(1)

        raw = (
            raw.replace(".", "")
            .replace(" ", "")
            .replace(",", ".")
        )

        try:
            return float(raw)
        except ValueError:
            pass

    # --------------------------------------------------------
    # International format
    # 799.99
    # 1,299.99
    # --------------------------------------------------------

    match = re.search(
        r"(\d{1,5}(?:,\d{3})*\.\d{2})",
        text,
    )

    if match:
        raw = match.group(1)

        raw = raw.replace(",", "")

        try:
            return float(raw)
        except ValueError:
            pass

    # --------------------------------------------------------
    # Plain decimal
    # --------------------------------------------------------

    match = re.fullmatch(
        r"\s*(\d{2,5}(?:[.,]\d{1,2})?)\s*",
        text,
    )

    if match:
        raw = match.group(1)

        try:
            if "," in raw and "." not in raw:
                return float(raw.replace(",", "."))

            return float(raw.replace(",", ""))
        except ValueError:
            pass

    # --------------------------------------------------------
    # EUR / € before number
    # --------------------------------------------------------

    match = re.search(
        r"(?:€|EUR)\s*(\d{2,5}(?:[.,]\d{1,2})?)",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        raw = match.group(1)

        try:
            return float(raw.replace(",", "."))
        except ValueError:
            pass

    return None


def valid_price(price):
    """
    Prevent obvious false positives.

    Gaming monitors in this tracker should normally fall
    somewhere between €50 and €5000.
    """

    return (
        price is not None
        and 50 <= price <= 5000
    )


# ============================================================
# PRICE EXTRACTION FROM TEXT
# ============================================================

def extract_prices_from_text(text):
    """
    Extract possible prices from page text.

    Returns a list of valid prices.
    """

    if not text:
        return []

    text = (
        text.replace("\xa0", " ")
        .replace("\u202f", " ")
    )

    candidates = []

    # German prices:
    # 799,99 €
    # 1.299,99 €
    candidates.extend(
        re.findall(
            r"\b\d{1,5}(?:[.\s]\d{3})*,\d{2}\s*€",
            text,
            flags=re.IGNORECASE,
        )
    )

    # International prices:
    # 799.99 €
    # 1,299.99 €
    candidates.extend(
        re.findall(
            r"\b\d{1,5}(?:,\d{3})*\.\d{2}\s*€",
            text,
            flags=re.IGNORECASE,
        )
    )

    # EUR format
    candidates.extend(
        re.findall(
            r"(?:€|EUR)\s*\d{2,5}(?:[.,]\d{2})?",
            text,
            flags=re.IGNORECASE,
        )
    )

    prices = []

    for candidate in candidates:
        price = parse_price(candidate)

        if valid_price(price):
            prices.append(price)

    return prices


# ============================================================
# JSON-LD PRICE EXTRACTION
# ============================================================

def extract_jsonld_prices(soup):
    """
    Extract product prices from JSON-LD structured data.
    """

    prices = []

    scripts = soup.select(
        'script[type="application/ld+json"]'
    )

    for script in scripts:
        try:
            raw = script.string or script.get_text()

            data = json.loads(raw)

        except Exception:
            continue

        stack = []

        if isinstance(data, list):
            stack.extend(data)
        else:
            stack.append(data)

        while stack:

            item = stack.pop()

            if isinstance(item, dict):

                # Direct price
                if "price" in item:

                    price = parse_price(
                        item.get("price")
                    )

                    if valid_price(price):
                        prices.append(price)

                # Offers
                if "offers" in item:

                    offers = item["offers"]

                    if isinstance(offers, list):
                        stack.extend(offers)

                    elif isinstance(offers, dict):
                        stack.append(offers)

                # Nested dictionaries/lists
                for value in item.values():

                    if isinstance(value, dict):
                        stack.append(value)

                    elif isinstance(value, list):
                        stack.extend(value)

            elif isinstance(item, list):

                stack.extend(item)

    return prices


# ============================================================
# PAGE PRICE EXTRACTION
# ============================================================

def extract_prices_from_page(page, source):
    """
    Extract possible prices from the rendered browser page.

    We use several methods because Amazon, MediaMarkt and
    Idealo do not expose their prices in exactly the same way.
    """

    prices = []

    # --------------------------------------------------------
    # 1. Rendered page text
    # --------------------------------------------------------

    try:
        body_text = page.locator("body").inner_text(
            timeout=10000
        )

        prices.extend(
            extract_prices_from_text(body_text)
        )

    except Exception:
        body_text = ""

    # --------------------------------------------------------
    # 2. HTML / JSON-LD
    # --------------------------------------------------------

    try:
        html = page.content()

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        prices.extend(
            extract_jsonld_prices(soup)
        )

        # itemprop price
        for tag in soup.select(
            '[itemprop="price"]'
        ):

            if tag.get("content"):

                price = parse_price(
                    tag.get("content")
                )

                if valid_price(price):
                    prices.append(price)

            text = tag.get_text(
                " ",
                strip=True,
            )

            price = parse_price(text)

            if valid_price(price):
                prices.append(price)

    except Exception:
        pass

    # --------------------------------------------------------
    # 3. Source-specific selectors
    # --------------------------------------------------------

    selectors = []

    if source == "amazon":

        selectors = [
            "#corePriceDisplay_desktop_feature_div "
            ".a-price:not(.a-text-price) .a-offscreen",

            "#corePrice_feature_div "
            ".a-price:not(.a-text-price) .a-offscreen",

            "#apex_desktop "
            ".a-price:not(.a-text-price) .a-offscreen",

            "#price_inside_buybox .a-offscreen",

            "#priceblock_ourprice",

            "#priceblock_dealprice",

            "[data-a-color='price'] .a-offscreen",

            ".a-price .a-offscreen",
        ]

    elif source == "mediamarkt":

        selectors = [
            '[data-test="mms-price"]',

            '[data-testid="price"]',

            '[data-test*="price"]',

            '[class*="price"]',

            '[class*="Price"]',

            '[class*="priceContainer"]',

            '[class*="PriceContainer"]',
        ]

    elif source == "idealo":

        selectors = [
            '[data-testid*="price"]',

            '[data-test*="price"]',

            '[class*="price"]',

            '[class*="Price"]',

            '[class*="offer"]',

            '[class*="Offer"]',
        ]

    # --------------------------------------------------------
    # Read source-specific selectors
    # --------------------------------------------------------

    for selector in selectors:

        try:

            elements = page.locator(selector)

            count = min(
                elements.count(),
                100,
            )

            for index in range(count):

                try:

                    text = elements.nth(
                        index
                    ).inner_text(
                        timeout=2000
                    )

                    prices.extend(
                        extract_prices_from_text(
                            text
                        )
                    )

                except Exception:
                    continue

        except Exception:
            continue

    # --------------------------------------------------------
    # Remove duplicates
    # --------------------------------------------------------

    unique_prices = []

    for price in prices:

        rounded = round(price, 2)

        if rounded not in unique_prices:
            unique_prices.append(rounded)

    return unique_prices


# ============================================================
# SOURCE-SPECIFIC PRICE SELECTION
# ============================================================

def choose_best_price(prices, source):
    """
    Choose the most likely current product price.

    Idealo:
        Usually "ab €XXX" = lowest current offer.

    MediaMarkt:
        Usually the product page has one primary price.

    Amazon:
        Prefer the lowest valid price returned by the
        current-price selectors.
    """

    if not prices:
        return None

    prices = [
        p
        for p in prices
        if valid_price(p)
    ]

    if not prices:
        return None

    # --------------------------------------------------------
    # Idealo
    # --------------------------------------------------------

    if source == "idealo":
        return min(prices)

    # --------------------------------------------------------
    # MediaMarkt
    # --------------------------------------------------------

    if source == "mediamarkt":

        # Product prices should normally be in this range.
        # Selecting the minimum also avoids accidentally
        # picking an old/list price.
        return min(prices)

    # --------------------------------------------------------
    # Amazon
    # --------------------------------------------------------

    if source == "amazon":
        return min(prices)

    return min(prices)


# ============================================================
# PAGE DIAGNOSTICS
# ============================================================

def diagnose_page(page, source):
    """
    Print useful information when price extraction fails.

    This is intentionally printed into GitHub Actions logs.
    """

    print("")
    print("  ---------- DIAGNOSTIC ----------")

    try:
        title = page.title()

        print(f"  Page title: {title}")

    except Exception as exc:

        print(
            "  Page title unavailable: "
            f"{type(exc).__name__}"
        )

    try:
        url = page.url

        print(f"  Final URL: {url}")

    except Exception:
        pass

    try:

        body_text = page.locator(
            "body"
        ).inner_text(
            timeout=5000
        )

        body_text = body_text.strip()

        print("  Page text sample:")

        if body_text:

            print(
                body_text[:2500]
            )

        else:

            print(
                "  <EMPTY PAGE BODY>"
            )

    except Exception as exc:

        print(
            "  Could not collect page text: "
            f"{type(exc).__name__}"
        )

    print(
        f"  Source: {source}"
    )

    print(
        "  --------------------------------"
    )


# ============================================================
# BOT / BLOCK DETECTION
# ============================================================

def detect_block(page, source):
    """
    Detect common block/challenge pages.
    """

    try:

        title = page.title().lower()

    except Exception:

        title = ""

    try:

        text = page.locator(
            "body"
        ).inner_text(
            timeout=5000
        ).lower()

    except Exception:

        text = ""

    combined = (
        title
        + "\n"
        + text[:10000]
    )

    # Amazon
    if source == "amazon":

        amazon_markers = [
            "robot check",
            "enter the characters you see",
            "type the characters",
            "captcha",
            "sorry, we just need to make sure",
            "automated access",
        ]

        for marker in amazon_markers:

            if marker in combined:

                return (
                    "AMAZON_BOT_CHECK"
                )

    # General blocking
    block_markers = [
        "access denied",
        "request blocked",
        "forbidden",
        "too many requests",
        "unusual traffic",
        "security check",
        "checking your browser",
    ]

    for marker in block_markers:

        if marker in combined:

            return (
                "SITE_BLOCKED"
            )

    return None


# ============================================================
# BROWSER PAGE LOADING
# ============================================================

def load_page(browser, url, source):
    """
    Open a page using Playwright.

    Returns:
        page, status
    """

    context = browser.new_context(
        user_agent=USER_AGENT,
        locale="de-DE",
        timezone_id="Europe/Berlin",
        extra_http_headers=BROWSER_HEADERS,
        viewport={
            "width": 1440,
            "height": 1000,
        },
        java_script_enabled=True,
    )

    page = context.new_page()

    try:

        print(
            f"  Opening: {url}"
        )

        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        # Give dynamic pages time to render.
        page.wait_for_timeout(5000)

        # Try network-idle, but don't fail if the site
        # continuously loads resources.
        try:

            page.wait_for_load_state(
                "networkidle",
                timeout=15000,
            )

        except Exception:
            pass

        # Scroll a little so lazy-loaded content appears.
        try:

            page.evaluate(
                """
                () => {
                    window.scrollTo(
                        0,
                        Math.min(
                            document.body.scrollHeight,
                            1800
                        )
                    );
                }
                """
            )

            page.wait_for_timeout(2500)

        except Exception:
            pass

        # HTTP status
        if response is not None:

            status_code = response.status

            print(
                f"  HTTP status: "
                f"{status_code}"
            )

            if status_code >= 400:

                return (
                    page,
                    f"HTTP_{status_code}"
                )

        block_status = detect_block(
            page,
            source,
        )

        if block_status:

            return (
                page,
                block_status
            )

        return page, "OK"

    except Exception as exc:

        print(
            "  Page loading failed: "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        return (
            page,
            f"PAGE_ERROR: {type(exc).__name__}"
        )


# ============================================================
# FETCH PRICE
# ============================================================

def fetch_price(browser, url, source):
    """
    Fetch a price from Amazon, MediaMarkt or Idealo.
    """

    if not url:

        return (
            None,
            "NOT_CONFIGURED"
        )

    page = None

    try:

        page, load_status = load_page(
            browser,
            url,
            source,
        )

        # If the page is blocked or failed, diagnose it.
        if load_status != "OK":

            diagnose_page(
                page,
                source,
            )

            return (
                None,
                load_status
            )

        # ----------------------------------------------------
        # Extract prices
        # ----------------------------------------------------

        prices = extract_prices_from_page(
            page,
            source,
        )

        print(
            f"  Candidate prices: "
            f"{prices}"
        )

        price = choose_best_price(
            prices,
            source,
        )

        if price is not None:

            print(
                f"  Selected price: "
                f"€{price:.2f}"
            )

            return (
                price,
                "OK"
            )

        # ----------------------------------------------------
        # Nothing found
        # ----------------------------------------------------

        diagnose_page(
            page,
            source,
        )

        return (
            None,
            f"{source.upper()}_PRICE_NOT_FOUND"
        )

    except Exception as exc:

        print(
            f"  Extraction error: "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        if page is not None:

            diagnose_page(
                page,
                source,
            )

        return (
            None,
            f"ERROR: {type(exc).__name__}"
        )

    finally:

        if page is not None:

            try:
                page.context.close()

            except Exception:
                pass


# ============================================================
# PERCENTAGE CHANGE
# ============================================================

def percentage_change(old, new):
    """Calculate percentage change."""

    if old in (None, 0):
        return None

    if new is None:
        return None

    return (
        (new - old)
        / old
        * 100
    )


# ============================================================
# EMAIL SETTINGS
# ============================================================

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


# ============================================================
# SEND EMAIL
# ============================================================

def send_email(changes, demo=False):
    """
    Send one combined email containing all price changes
    from the current hourly run.
    """

    sender, recipient, password = (
        smtp_settings()
    )

    if (
        not sender
        or not recipient
        or not password
    ):

        raise RuntimeError(
            "Email secrets are missing. "
            "Set ALERT_EMAIL_FROM, "
            "ALERT_EMAIL_TO and "
            "SMTP_PASSWORD in GitHub Secrets."
        )

    lines = []

    lines.append(
        "OLED Monitor Price Tracker"
    )

    lines.append("")

    if demo:

        lines.append(
            "DEMO TEST — "
            "these are simulated price changes."
        )

        lines.append("")

    lines.append(
        "Checked: "
        + datetime.now()
        .astimezone()
        .strftime(
            "%Y-%m-%d %H:%M %Z"
        )
    )

    lines.append("")

    for change in changes:

        old = change["old"]
        new = change["new"]

        if new < old:

            direction = "PRICE DECREASED"

        elif new > old:

            direction = "PRICE INCREASED"

        else:

            direction = "PRICE UNCHANGED"

        pct = percentage_change(
            old,
            new,
        )

        if pct is not None:

            pct_text = (
                f" ({pct:+.2f}%)"
            )

        else:

            pct_text = ""

        lines.append(
            f"Monitor: "
            f"{change['monitor']}"
        )

        lines.append(
            f"Source: "
            f"{change['source']}"
        )

        lines.append(
            f"{direction}: "
            f"€{old:.2f} → "
            f"€{new:.2f}"
            f"{pct_text}"
        )

        lines.append(
            f"URL: "
            f"{change['url']}"
        )

        lines.append("")

        lines.append(
            "--------------------------------"
        )

        lines.append("")

    message = EmailMessage()

    if demo:

        subject = (
            "[TEST] OLED monitor "
            f"price tracker — "
            f"{len(changes)} example changes"
        )

    else:

        subject = (
            "OLED monitor price change — "
            f"{len(changes)} change(s)"
        )

    message["Subject"] = subject

    message["From"] = sender

    message["To"] = recipient

    message.set_content(
        "\n".join(lines)
    )

    context = ssl.create_default_context()

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        context=context,
    ) as smtp:

        smtp.login(
            sender,
            password,
        )

        smtp.send_message(
            message
        )


# ============================================================
# DEMO NOTIFICATION
# ============================================================

def demo_changes(config):
    """
    Create fake changes for testing the email system.
    No real prices are used.
    """

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

    monitors = {
        monitor.get("name"): monitor
        for monitor in config.get(
            "monitors",
            []
        )
    }

    rows = []

    for (
        name,
        source,
        old,
        new,
    ) in examples:

        monitor = monitors.get(
            name,
            {}
        )

        rows.append(
            {
                "monitor": name,
                "source": source,
                "old": old,
                "new": new,
                "url": monitor.get(
                    source,
                    "https://example.com/demo",
                ),
            }
        )

    return rows


# ============================================================
# README TABLE
# ============================================================

def build_readme_table(state):
    """
    Update the Current price table inside README.md.
    """

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
            "idealo",
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

    table = "\n".join(lines)

    if README.exists():

        readme = README.read_text(
            encoding="utf-8"
        )

    else:

        readme = ""

    marker = (
        "## Current price table"
    )

    if marker in readme:

        readme = (
            readme
            .split(marker)[0]
            .rstrip()
            + "\n\n"
            + table
            + "\n"
        )

    else:

        readme = (
            readme.rstrip()
            + "\n\n"
            + table
            + "\n"
        )

    README.write_text(
        readme,
        encoding="utf-8",
    )


# ============================================================
# MAIN PRICE TRACKING
# ============================================================

def run_tracker(config):
    """
    Main tracker process.
    """

    state = load_json(
        STATE,
        {
            "monitors": []
        }
    )

    old_state_by_model = {
        monitor["model"]: monitor
        for monitor in state.get(
            "monitors",
            []
        )
        if "model" in monitor
    }

    changes = []

    now = (
        datetime.now(timezone.utc)
        .astimezone()
        .isoformat(
            timespec="minutes"
        )
    )

    new_state = {
        "updated_at": now,
        "monitors": [],
    }

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=True
        )

        try:

            for monitor in config.get(
                "monitors",
                []
            ):

                name = monitor.get(
                    "name",
                    "Unknown monitor"
                )

                model = monitor.get(
                    "model",
                    name
                )

                print("")
                print(
                    "================================================"
                )

                print(
                    f"Checking: {name}"
                )

                print(
                    "================================================"
                )

                old_monitor = (
                    old_state_by_model.get(
                        model,
                        {}
                    )
                )

                old_sources = {
                    item["source"]: item
                    for item in old_monitor.get(
                        "sources",
                        []
                    )
                    if "source" in item
                }

                source_rows = []

                for source in (
                    "amazon",
                    "mediamarkt",
                    "idealo",
                ):

                    url = monitor.get(
                        source,
                        ""
                    )

                    print("")
                    print(
                        f"Checking {name} | "
                        f"{source}"
                    )

                    price, status = fetch_price(
                        browser,
                        url,
                        source,
                    )

                    if price is not None:

                        print(
                            f"Result: "
                            f"€{price:.2f}"
                        )

                    else:

                        print(
                            f"Result: N/A "
                            f"({status})"
                        )

                    old_item = (
                        old_sources.get(
                            source,
                            {}
                        )
                    )

                    old_price = (
                        old_item.get(
                            "price"
                        )
                    )

                    # ------------------------------------------------
                    # Detect price changes
                    # ------------------------------------------------

                    if (
                        price is not None
                        and old_price is not None
                        and round(
                            price,
                            2
                        )
                        != round(
                            float(old_price),
                            2
                        )
                    ):

                        changes.append(
                            {
                                "monitor": name,
                                "source": source,
                                "old": float(
                                    old_price
                                ),
                                "new": float(
                                    price
                                ),
                                "url": url,
                            }
                        )

                        print(
                            "PRICE CHANGE DETECTED!"
                        )

                    source_rows.append(
                        {
                            "source": source,
                            "url": url,
                            "price": price,
                            "status": status,
                        }
                    )

                new_state[
                    "monitors"
                ].append(
                    {
                        "name": name,
                        "model": model,
                        "last_checked": now,
                        "sources": source_rows,
                    }
                )

        finally:

            browser.close()

    # --------------------------------------------------------
    # Save state
    # --------------------------------------------------------

    save_json(
        STATE,
        new_state
    )

    # --------------------------------------------------------
    # Update README
    # --------------------------------------------------------

    build_readme_table(
        new_state
    )

    # --------------------------------------------------------
    # Send ONE combined email
    # --------------------------------------------------------

    if changes:

        print("")
        print(
            f"Sending email for "
            f"{len(changes)} price change(s)..."
        )

        send_email(
            changes
        )

        print(
            "Price-change email sent."
        )

    else:

        print("")
        print(
            "No price changes."
        )

        print(
            "No email sent."
        )

    print("")
    print(
        f"Checked "
        f"{len(config.get('monitors', []))} "
        f"monitors."
    )

    print(
        f"Price changes: "
        f"{len(changes)}"
    )


# ============================================================
# PROGRAM ENTRY POINT
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Automatic OLED monitor "
            "price tracker"
        )
    )

    parser.add_argument(
        "--demo-notification",
        action="store_true",
        help=(
            "Send a clearly labelled "
            "test email without checking "
            "real prices."
        ),
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # Load monitor configuration
    # --------------------------------------------------------

    config = load_json(
        CONFIG,
        {
            "monitors": []
        }
    )

    monitors = config.get(
        "monitors",
        []
    )

    if not monitors:

        raise RuntimeError(
            "No monitors found in "
            "config/monitors.json"
        )

    # --------------------------------------------------------
    # Demo email
    # --------------------------------------------------------

    if args.demo_notification:

        print(
            "Sending demo notification..."
        )

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

    # --------------------------------------------------------
    # Normal tracker
    # --------------------------------------------------------

    run_tracker(
        config
    )


if __name__ == "__main__":
    main()
