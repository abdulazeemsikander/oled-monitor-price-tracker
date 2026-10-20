import json
import os
import re
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup


# ============================================================
# PATHS
# ============================================================

ROOT_DIR = Path(__file__).resolve().parent.parent

CONFIG_FILE = ROOT_DIR / "config" / "monitors.json"
STATE_FILE = ROOT_DIR / "data" / "state.json"
README_FILE = ROOT_DIR / "README.md"


# ============================================================
# SETTINGS
# ============================================================

REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# Search engines used as public search/index sources.
SEARCH_ENGINES = [
    "https://www.google.com/search?q={query}&hl=de&gl=de",
    "https://www.bing.com/search?q={query}&setlang=de-DE",
    "https://html.duckduckgo.com/html/?q={query}",
]


# ============================================================
# PRICE PARSING
# ============================================================

def parse_price(text):
    """
    Extract a Euro price from arbitrary text.

    Examples:
        1.299,00 €
        1299,00€
        €1,299.00
        999 €
    """

    if not text:
        return None

    patterns = [
        r"€\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)",
        r"([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)\s*€",
        r"EUR\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)

        for value in matches:
            try:
                value = value.replace(" ", "")

                # German format: 1.299,99
                if "," in value and "." in value:
                    value = value.replace(".", "").replace(",", ".")

                # German decimal format: 1299,99
                elif "," in value:
                    value = value.replace(",", ".")

                # Avoid interpreting things like years/model numbers
                price = float(value)

                if 50 <= price <= 10000:
                    return round(price, 2)

            except ValueError:
                continue

    return None


# ============================================================
# SEARCH
# ============================================================

def build_search_query(model, retailer):
    """
    Search for the exact monitor model on a particular retailer.
    """

    retailer_domains = {
        "amazon": "amazon.de",
        "mediamarkt": "mediamarkt.de",
        "idealo": "idealo.de",
    }

    domain = retailer_domains[retailer]

    return (
        f'"{model}" '
        f'site:{domain} '
        f'(preis OR kaufen OR €)'
    )


def get_search_page(url):
    """
    Download a public search result page.
    """

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        return response.text

    except Exception as exc:
        print(f"SEARCH_ERROR: {type(exc).__name__}: {exc}")
        return None


def extract_search_results(html):
    """
    Extract readable search result blocks from a search page.
    """

    soup = BeautifulSoup(html, "html.parser")

    results = []

    # Google
    for result in soup.select("div.MjjYud"):
        text = result.get_text(" ", strip=True)

        if text:
            results.append(text)

    # Bing
    for result in soup.select("li.b_algo"):
        text = result.get_text(" ", strip=True)

        if text:
            results.append(text)

    # DuckDuckGo
    for result in soup.select(".result"):
        text = result.get_text(" ", strip=True)

        if text:
            results.append(text)

    # Generic fallback
    if not results:
        body = soup.get_text(" ", strip=True)

        if body:
            results.append(body)

    return results


def search_for_price(model, retailer):
    """
    Search public search-engine results for the monitor model
    and extract a plausible current price.

    Returns:
        {
            "price": float | None,
            "source": str,
            "snippet": str
        }
    """

    query = build_search_query(model, retailer)
    encoded_query = quote_plus(query)

    print()
    print("=" * 70)
    print(f"Searching: {model}")
    print(f"Retailer:  {retailer}")
    print(f"Query:     {query}")
    print("=" * 70)

    for engine_index, template in enumerate(SEARCH_ENGINES, start=1):

        url = template.format(query=encoded_query)

        print(f"Search engine #{engine_index}")

        html = get_search_page(url)

        if not html:
            continue

        results = extract_search_results(html)

        print(f"Found {len(results)} result blocks.")

        for result in results:

            # We want the model name to appear somewhere in the result.
            model_words = model.lower().split()

            result_lower = result.lower()

            matching_words = sum(
                1 for word in model_words
                if len(word) >= 3 and word in result_lower
            )

            # Require a reasonable amount of model-name matching.
            if matching_words < max(2, len(model_words) // 2):
                continue

            price = parse_price(result)

            if price is None:
                continue

            print(f"PRICE FOUND: €{price:.2f}")
            print(f"Snippet: {result[:500]}")

            return {
                "price": price,
                "source": retailer,
                "snippet": result[:1000],
            }

        time.sleep(2)

    print("PRICE_NOT_FOUND")

    return {
        "price": None,
        "source": retailer,
        "snippet": "",
    }


# ============================================================
# STATE
# ============================================================

def load_json(path, default):
    try:
        if not path.exists():
            return default

        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    except Exception as exc:
        print(f"Could not read {path}: {exc}")
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            indent=2,
            ensure_ascii=False,
        )


def load_state():
    return load_json(
        STATE_FILE,
        {
            "last_updated": None,
            "monitors": {},
        },
    )


# ============================================================
# PRICE COMPARISON
# ============================================================

def classify_change(old_price, new_price):
    if old_price is None or new_price is None:
        return "UNKNOWN"

    if new_price < old_price:
        return "DECREASE"

    if new_price > old_price:
        return "INCREASE"

    return "UNCHANGED"


# ============================================================
# README UPDATE
# ============================================================

def update_readme(monitors):
    """
    Replace the automatically generated price table in README.md.
    """

    if not README_FILE.exists():
        return

    try:
        readme = README_FILE.read_text(encoding="utf-8")

        start_marker = "<!-- PRICE_TABLE_START -->"
        end_marker = "<!-- PRICE_TABLE_END -->"

        if start_marker not in readme or end_marker not in readme:
            print("README price table markers not found.")
            return

        rows = []

        rows.append("| Monitor | Amazon.de | MediaMarkt.de | Idealo.de |")
        rows.append("|---|---:|---:|---:|")

        for monitor_name, data in monitors.items():

            prices = data.get("prices", {})

            amazon = prices.get("amazon")
            mediamarkt = prices.get("mediamarkt")
            idealo = prices.get("idealo")

            def fmt(value):
                if value is None:
                    return "N/A"

                return f"€{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

            rows.append(
                f"| {monitor_name} | "
                f"{fmt(amazon)} | "
                f"{fmt(mediamarkt)} | "
                f"{fmt(idealo)} |"
            )

        table = "\n".join(rows)

        before = readme.split(start_marker)[0]
        after = readme.split(end_marker)[1]

        new_readme = (
            before
            + start_marker
            + "\n"
            + table
            + "\n"
            + end_marker
            + after
        )

        README_FILE.write_text(
            new_readme,
            encoding="utf-8",
        )

        print("README updated.")

    except Exception as exc:
        print(f"README update failed: {exc}")


# ============================================================
# EMAIL
# ============================================================

def send_email(changes):
    """
    Send ONE combined email containing all price changes
    detected during this run.
    """

    sender = os.getenv("ALERT_EMAIL_FROM")
    recipient = os.getenv("ALERT_EMAIL_TO")
    password = os.getenv("SMTP_PASSWORD")

    if not sender or not recipient or not password:
        print("Email secrets are missing.")
        print("Skipping email.")
        return

    if not changes:
        print("No changes. No email will be sent.")
        return

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    subject = f"OLED Monitor Price Alert — {len(changes)} change(s)"

    body_lines = [
        "OLED Gaming Monitor Price Tracker",
        "",
        f"Checked: {timestamp}",
        "",
        "PRICE CHANGES",
        "==============",
        "",
    ]

    for change in changes:

        old_price = change["old_price"]
        new_price = change["new_price"]

        direction = change["direction"]

        if direction == "DECREASE":
            symbol = "↓"
        elif direction == "INCREASE":
            symbol = "↑"
        else:
            symbol = "→"

        body_lines.extend(
            [
                f"{symbol} {change['monitor']}",
                f"   Source: {change['source']}",
                f"   Old price: €{old_price:.2f}",
                f"   New price: €{new_price:.2f}",
                f"   Change: €{new_price - old_price:+.2f}",
                "",
            ]
        )

    body_lines.extend(
        [
            "The tracker checks prices automatically every hour.",
            "",
            "GitHub repository:",
            "https://github.com/abdulazeemsikander/oled-monitor-price-tracker",
        ]
    )

    message = EmailMessage()

    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject

    message.set_content("\n".join(body_lines))

    try:
        context = ssl.create_default_context()

        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
            context=context,
        ) as server:

            server.login(
                sender,
                password,
            )

            server.send_message(message)

        print("Email sent successfully.")

    except Exception as exc:
        print(f"EMAIL_ERROR: {type(exc).__name__}: {exc}")


# ============================================================
# DEMO EMAIL
# ============================================================

def send_demo_email():
    """
    Sends a clearly labelled test email.

    This is separate from real price changes.
    """

    sender = os.getenv("ALERT_EMAIL_FROM")
    recipient = os.getenv("ALERT_EMAIL_TO")
    password = os.getenv("SMTP_PASSWORD")

    if not sender or not recipient or not password:
        print("Email secrets missing. Demo email skipped.")
        return

    message = EmailMessage()

    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = "OLED Monitor Tracker — TEST EMAIL"

    message.set_content(
        """
OLED Gaming Monitor Price Tracker

This is a TEST notification.

Your GitHub Actions workflow is able to send email successfully.

Example changes:

↓ ASUS ROG Strix OLED XG32UCWMG
   Amazon.de
   €1,499.00 → €1,399.00

↑ Samsung Odyssey OLED G8 G80SD
   MediaMarkt.de
   €899.00 → €949.00

↓ LG UltraGear OLED 32GX850A-B
   Idealo.de
   €999.00 → €949.00

This email does NOT represent real prices.
        """.strip()
    )

    try:
        context = ssl.create_default_context()

        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
            context=context,
        ) as server:

            server.login(
                sender,
                password,
            )

            server.send_message(message)

        print("Demo email sent successfully.")

    except Exception as exc:
        print(f"DEMO_EMAIL_ERROR: {type(exc).__name__}: {exc}")


# ============================================================
# MAIN TRACKER
# ============================================================

def main():

    print("=" * 70)
    print("OLED MONITOR PRICE TRACKER")
    print("=" * 70)

    config = load_json(
        CONFIG_FILE,
        [],
    )

    state = load_state()

    if isinstance(config, dict):
        monitors_config = config.get("monitors", [])
    else:
        monitors_config = config

    changes = []

    if "monitors" not in state:
        state["monitors"] = {}

    for monitor in monitors_config:

        name = monitor.get("name")
        model = monitor.get("model")

        if not name or not model:
            print("Skipping invalid monitor configuration.")
            continue

        print()
        print("#" * 70)
        print(f"MONITOR: {name}")
        print(f"MODEL:   {model}")
        print("#" * 70)

        previous_data = state["monitors"].get(
            name,
            {
                "prices": {},
            },
        )

        previous_prices = previous_data.get(
            "prices",
            {},
        )

        current_prices = {}

        retailers = [
            "amazon",
            "mediamarkt",
            "idealo",
        ]

        for retailer in retailers:

            result = search_for_price(
                model,
                retailer,
            )

            current_prices[retailer] = result["price"]

            old_price = previous_prices.get(retailer)
            new_price = result["price"]

            direction = classify_change(
                old_price,
                new_price,
            )

            print(
                f"{retailer}: "
                f"old={old_price} "
                f"new={new_price} "
                f"change={direction}"
            )

            # Only compare prices when both values are valid.
            if (
                old_price is not None
                and new_price is not None
                and old_price != new_price
            ):

                changes.append(
                    {
                        "monitor": name,
                        "source": retailer,
                        "old_price": old_price,
                        "new_price": new_price,
                        "direction": direction,
                    }
                )

        state["monitors"][name] = {
            "model": model,
            "prices": current_prices,
            "last_checked": datetime.now(
                timezone.utc
            ).isoformat(),
        }

    state["last_updated"] = datetime.now(
        timezone.utc
    ).isoformat()

    save_json(
        STATE_FILE,
        state,
    )

    update_readme(
        state["monitors"]
    )

    print()
    print("=" * 70)
    print(f"PRICE CHANGES DETECTED: {len(changes)}")
    print("=" * 70)

    for change in changes:
        print(
            f"{change['monitor']} | "
            f"{change['source']} | "
            f"€{change['old_price']:.2f} → "
            f"€{change['new_price']:.2f}"
        )

    # One combined email per run.
    send_email(changes)

    print()
    print("Tracker finished.")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
