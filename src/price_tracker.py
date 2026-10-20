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
# OLED MONITOR PRICE TRACKER
# ============================================================
#
# This version does NOT directly scrape Amazon, MediaMarkt,
# or Idealo product pages.
#
# Instead, it searches public search-engine results for the
# monitor model and retailer domain, then extracts a price
# from the returned result.
#
# This avoids directly requesting retailer pages that may
# return CAPTCHA / anti-bot pages to GitHub Actions.
#
# ============================================================


# ============================================================
# FILE PATHS
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
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


# ============================================================
# SEARCH ENGINES
# ============================================================

SEARCH_ENGINES = [
    {
        "name": "Google",
        "url": "https://www.google.com/search?q={query}&hl=de&gl=de",
    },
    {
        "name": "Bing",
        "url": "https://www.bing.com/search?q={query}&setlang=de-DE",
    },
    {
        "name": "DuckDuckGo",
        "url": "https://html.duckduckgo.com/html/?q={query}",
    },
]


# ============================================================
# RETAILER DOMAINS
# ============================================================

RETAILER_DOMAINS = {
    "amazon": "amazon.de",
    "mediamarkt": "mediamarkt.de",
    "idealo": "idealo.de",
}


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# GENERAL JSON FUNCTIONS
# ============================================================

def load_json(path, default):
    """
    Safely load JSON from a file.

    If the file does not exist or contains invalid JSON,
    return the supplied default value.
    """

    try:
        if not path.exists():
            print(f"JSON file does not exist: {path}")
            return default

        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    except Exception as exc:
        print(
            f"Could not read JSON file {path}: "
            f"{type(exc).__name__}: {exc}"
        )

        return default


def save_json(path, data):
    """
    Save JSON using readable formatting.
    """

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(path, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            indent=2,
            ensure_ascii=False,
        )

        file.write("\n")


# ============================================================
# CONFIGURATION
# ============================================================

def load_config():
    """
    Load monitor configuration.

    Supports both:

        [
            {...},
            {...}
        ]

    and:

        {
            "monitors": [
                {...},
                {...}
            ]
        }
    """

    config = load_json(
        CONFIG_FILE,
        [],
    )

    if isinstance(config, list):
        return config

    if isinstance(config, dict):
        monitors = config.get("monitors", [])

        if isinstance(monitors, list):
            return monitors

    print("WARNING: Invalid monitors.json format.")

    return []


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():
    """
    Load state.json.

    IMPORTANT:
    Older versions of the tracker used:

        "monitors": [
            {...},
            {...}
        ]

    The current version uses:

        "monitors": {
            "Monitor Name": {
                ...
            }
        }

    This function automatically converts the old format.
    """

    default_state = {
        "last_updated": None,
        "monitors": {},
    }

    state = load_json(
        STATE_FILE,
        default_state,
    )

    # --------------------------------------------------------
    # Top-level validation
    # --------------------------------------------------------

    if not isinstance(state, dict):

        print(
            "WARNING: state.json has an invalid top-level format."
        )

        return default_state

    # --------------------------------------------------------
    # Make sure monitors exists
    # --------------------------------------------------------

    if "monitors" not in state:

        state["monitors"] = {}

        return state

    monitors = state["monitors"]

    # --------------------------------------------------------
    # Already using the new dictionary format
    # --------------------------------------------------------

    if isinstance(monitors, dict):

        return state

    # --------------------------------------------------------
    # Convert old list format
    # --------------------------------------------------------

    if isinstance(monitors, list):

        print()
        print("=" * 70)
        print("OLD STATE FORMAT DETECTED")
        print("=" * 70)
        print("Converting state.json from list to dictionary format.")

        converted_monitors = {}

        for item in monitors:

            if not isinstance(item, dict):
                continue

            name = item.get("name")

            if not name:
                name = item.get("monitor")

            if not name:
                continue

            converted_monitors[name] = {
                "model": item.get(
                    "model",
                    name,
                ),
                "prices": item.get(
                    "prices",
                    {},
                ),
                "last_checked": item.get(
                    "last_checked"
                ),
            }

        state["monitors"] = converted_monitors

        print(
            f"Converted {len(converted_monitors)} monitor(s)."
        )

        print("=" * 70)
        print()

        return state

    # --------------------------------------------------------
    # Unknown format
    # --------------------------------------------------------

    print(
        "WARNING: Unknown state.json monitor format."
    )

    state["monitors"] = {}

    return state


# ============================================================
# PRICE PARSING
# ============================================================

def parse_price(text):
    """
    Extract a plausible Euro price from text.

    Handles examples such as:

        1.299,00 €
        1.299 €
        1299,00 €
        €1,299.00
        999 €

    Returns:
        float or None
    """

    if not text:
        return None

    text = str(text)

    patterns = [
        # German:
        # 1.299,00 €
        # 1.299 €
        r"([0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]{2})?)\s*€",

        # German:
        # 1299,00 €
        r"([0-9]+,[0-9]{2})\s*€",

        # Simple:
        # 999 €
        r"([0-9]+(?:[.,][0-9]{1,2})?)\s*€",

        # Euro before number:
        # €1,299.00
        r"€\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)",

        # EUR:
        r"EUR\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)",
    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        for value in matches:

            try:

                value = value.strip()

                # --------------------------------------------
                # German format:
                #
                # 1.299,99
                # --------------------------------------------

                if "," in value and "." in value:

                    # If comma appears after the last dot,
                    # assume German format.
                    if value.rfind(",") > value.rfind("."):

                        value = (
                            value
                            .replace(".", "")
                            .replace(",", ".")
                        )

                    else:

                        # Otherwise assume English format.
                        value = value.replace(",", "")

                # --------------------------------------------
                # German decimal:
                #
                # 1299,99
                # --------------------------------------------

                elif "," in value:

                    value = value.replace(",", ".")

                # --------------------------------------------
                # Normal English decimal
                # --------------------------------------------

                else:

                    value = value.replace(",", "")

                price = float(value)

                # OLED monitors should reasonably be somewhere
                # inside this range.
                #
                # This also prevents things like:
                #   2026
                #   32
                #   240
                # from being interpreted as prices.
                if 300 <= price <= 5000:

                    return round(
                        price,
                        2,
                    )

            except (ValueError, TypeError):

                continue

    return None


# ============================================================
# SEARCH QUERY
# ============================================================

def build_search_query(model, retailer):
    """
    Build a German search query for the exact monitor model
    on a specific retailer.
    """

    domain = RETAILER_DOMAINS.get(
        retailer
    )

    if not domain:
        raise ValueError(
            f"Unknown retailer: {retailer}"
        )

    return (
        f'"{model}" '
        f'site:{domain} '
        f'("€" OR "EUR" OR "Preis" OR "kaufen")'
    )


# ============================================================
# SEARCH PAGE DOWNLOAD
# ============================================================

def fetch_search_page(url):
    """
    Download a search-engine result page.
    """

    try:

        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
        )

        print(
            f"HTTP {response.status_code}"
        )

        if response.status_code != 200:

            print(
                f"Search request failed: "
                f"HTTP {response.status_code}"
            )

            return None

        return response.text

    except requests.RequestException as exc:

        print(
            f"SEARCH_REQUEST_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        return None


# ============================================================
# SEARCH RESULT EXTRACTION
# ============================================================

def extract_search_results(html, engine_name):
    """
    Extract readable result blocks from a search page.

    Search-engine HTML changes from time to time, so several
    selectors are used.
    """

    if not html:
        return []

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results = []

    # --------------------------------------------------------
    # Google
    # --------------------------------------------------------

    if engine_name == "Google":

        selectors = [
            "div.MjjYud",
            "div.g",
            "div.tF2Cxc",
        ]

        for selector in selectors:

            for result in soup.select(selector):

                text = result.get_text(
                    " ",
                    strip=True,
                )

                if text:
                    results.append(text)

    # --------------------------------------------------------
    # Bing
    # --------------------------------------------------------

    elif engine_name == "Bing":

        selectors = [
            "li.b_algo",
            "li.b_ans",
        ]

        for selector in selectors:

            for result in soup.select(selector):

                text = result.get_text(
                    " ",
                    strip=True,
                )

                if text:
                    results.append(text)

    # --------------------------------------------------------
    # DuckDuckGo
    # --------------------------------------------------------

    elif engine_name == "DuckDuckGo":

        selectors = [
            ".result",
            ".results_links",
        ]

        for selector in selectors:

            for result in soup.select(selector):

                text = result.get_text(
                    " ",
                    strip=True,
                )

                if text:
                    results.append(text)

    # --------------------------------------------------------
    # Generic fallback
    # --------------------------------------------------------

    if not results:

        body = soup.get_text(
            " ",
            strip=True,
        )

        if body:
            results.append(body)

    # --------------------------------------------------------
    # Remove duplicates
    # --------------------------------------------------------

    unique_results = []

    seen = set()

    for result in results:

        normalized = result.strip()

        if not normalized:
            continue

        if normalized in seen:
            continue

        seen.add(normalized)

        unique_results.append(normalized)

    return unique_results


# ============================================================
# MODEL MATCHING
# ============================================================

def model_match_score(model, text):
    """
    Calculate how strongly a search result appears to match
    the requested monitor model.
    """

    if not model or not text:
        return 0

    model_lower = model.lower()
    text_lower = text.lower()

    # Exact model is the strongest signal.
    if model_lower in text_lower:
        return 100

    # Otherwise count model tokens.
    tokens = re.findall(
        r"[a-z0-9]+",
        model_lower,
    )

    important_tokens = [
        token
        for token in tokens
        if len(token) >= 3
    ]

    if not important_tokens:
        return 0

    matches = sum(
        1
        for token in important_tokens
        if token in text_lower
    )

    return int(
        (matches / len(important_tokens))
        * 100
    )


# ============================================================
# PRICE SEARCH
# ============================================================

def search_for_price(model, retailer):
    """
    Search public search-engine results for a monitor price.

    Returns:

        {
            "price": 1399.00,
            "source": "amazon",
            "engine": "Google",
            "snippet": "..."
        }

    or a result with price=None.
    """

    query = build_search_query(
        model,
        retailer,
    )

    encoded_query = quote_plus(
        query
    )

    print()
    print("=" * 70)
    print(f"SEARCHING: {model}")
    print(f"RETAILER:  {retailer}")
    print(f"QUERY:     {query}")
    print("=" * 70)

    for engine in SEARCH_ENGINES:

        engine_name = engine["name"]

        url = engine["url"].format(
            query=encoded_query
        )

        print()
        print(
            f"Search engine: {engine_name}"
        )

        html = fetch_search_page(
            url
        )

        if not html:
            continue

        results = extract_search_results(
            html,
            engine_name,
        )

        print(
            f"Result blocks found: {len(results)}"
        )

        candidates = []

        for result in results:

            score = model_match_score(
                model,
                result,
            )

            if score < 40:
                continue

            price = parse_price(
                result
            )

            if price is None:
                continue

            candidates.append(
                {
                    "price": price,
                    "score": score,
                    "snippet": result,
                }
            )

        # ----------------------------------------------------
        # Select strongest candidate.
        # ----------------------------------------------------

        if candidates:

            candidates.sort(
                key=lambda item: (
                    item["score"],
                    -item["price"],
                ),
                reverse=True,
            )

            best = candidates[0]

            print()
            print(
                f"PRICE FOUND: €{best['price']:.2f}"
            )

            print(
                f"MATCH SCORE: {best['score']}"
            )

            print(
                f"SNIPPET: {best['snippet'][:600]}"
            )

            return {
                "price": best["price"],
                "source": retailer,
                "engine": engine_name,
                "snippet": best["snippet"][:1000],
            }

        print(
            f"No suitable price found on {engine_name}."
        )

        # Avoid hammering search engines.
        time.sleep(2)

    print()
    print("PRICE_NOT_FOUND")

    return {
        "price": None,
        "source": retailer,
        "engine": None,
        "snippet": "",
    }


# ============================================================
# CHANGE CLASSIFICATION
# ============================================================

def classify_change(old_price, new_price):
    """
    Determine whether the price increased, decreased,
    remained unchanged, or cannot be compared.
    """

    if old_price is None:
        return "FIRST_VALUE"

    if new_price is None:
        return "UNKNOWN"

    if new_price < old_price:
        return "DECREASE"

    if new_price > old_price:
        return "INCREASE"

    return "UNCHANGED"


# ============================================================
# FORMAT PRICE
# ============================================================

def format_price(price):
    """
    Format a price using German-style notation.

    Example:
        1299.99 -> €1.299,99
    """

    if price is None:
        return "N/A"

    formatted = f"{price:,.2f}"

    formatted = (
        formatted
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )

    return f"€{formatted}"


# ============================================================
# README UPDATE
# ============================================================

def update_readme(monitors):
    """
    Update the automatically generated current-price table
    in README.md.
    """

    if not README_FILE.exists():

        print(
            "README.md not found. "
            "Skipping README update."
        )

        return

    start_marker = (
        "<!-- PRICE_TABLE_START -->"
    )

    end_marker = (
        "<!-- PRICE_TABLE_END -->"
    )

    try:

        readme = README_FILE.read_text(
            encoding="utf-8"
        )

        if (
            start_marker not in readme
            or end_marker not in readme
        ):

            print(
                "README price-table markers not found."
            )

            print(
                "Skipping README update."
            )

            return

        rows = []

        rows.append(
            "| Monitor | Amazon.de | MediaMarkt.de | Idealo.de |"
        )

        rows.append(
            "|---|---:|---:|---:|"
        )

        for monitor_name, data in monitors.items():

            prices = data.get(
                "prices",
                {},
            )

            amazon = prices.get(
                "amazon"
            )

            mediamarkt = prices.get(
                "mediamarkt"
            )

            idealo = prices.get(
                "idealo"
            )

            rows.append(
                f"| {monitor_name} | "
                f"{format_price(amazon)} | "
                f"{format_price(mediamarkt)} | "
                f"{format_price(idealo)} |"
            )

        table = "\n".join(
            rows
        )

        before = readme.split(
            start_marker,
            1
        )[0]

        after = readme.split(
            end_marker,
            1
        )[1]

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

        print(
            "README.md price table updated."
        )

    except Exception as exc:

        print(
            f"README_UPDATE_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )


# ============================================================
# EMAIL CONFIGURATION
# ============================================================

def get_email_config():
    """
    Read email credentials from environment variables.
    """

    return {
        "sender": os.getenv(
            "ALERT_EMAIL_FROM"
        ),
        "recipient": os.getenv(
            "ALERT_EMAIL_TO"
        ),
        "password": os.getenv(
            "SMTP_PASSWORD"
        ),
    }


# ============================================================
# SEND PRICE ALERT
# ============================================================

def send_email(changes):
    """
    Send ONE combined email containing every price change
    found during this tracker run.
    """

    if not changes:

        print(
            "No price changes detected."
        )

        print(
            "No alert email will be sent."
        )

        return

    email = get_email_config()

    sender = email["sender"]
    recipient = email["recipient"]
    password = email["password"]

    if not sender:

        print(
            "ALERT_EMAIL_FROM secret is missing."
        )

        return

    if not recipient:

        print(
            "ALERT_EMAIL_TO secret is missing."
        )

        return

    if not password:

        print(
            "SMTP_PASSWORD secret is missing."
        )

        return

    now = datetime.now(
        timezone.utc
    )

    timestamp = now.strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    subject = (
        "OLED Monitor Price Alert - "
        f"{len(changes)} change(s)"
    )

    body = []

    body.append(
        "OLED GAMING MONITOR PRICE TRACKER"
    )

    body.append(
        "================================="
    )

    body.append("")

    body.append(
        f"Checked: {timestamp}"
    )

    body.append("")

    body.append(
        "PRICE CHANGES"
    )

    body.append(
        "-------------"
    )

    body.append("")

    for change in changes:

        direction = change[
            "direction"
        ]

        if direction == "DECREASE":

            symbol = "↓"

        elif direction == "INCREASE":

            symbol = "↑"

        else:

            symbol = "→"

        old_price = change[
            "old_price"
        ]

        new_price = change[
            "new_price"
        ]

        difference = (
            new_price
            - old_price
        )

        body.append(
            f"{symbol} {change['monitor']}"
        )

        body.append(
            f"   Source: {change['source']}"
        )

        body.append(
            f"   Old price: {format_price(old_price)}"
        )

        body.append(
            f"   New price: {format_price(new_price)}"
        )

        body.append(
            f"   Change: {format_price(abs(difference))}"
            + (
                " decrease"
                if difference < 0
                else " increase"
            )
        )

        body.append("")

    body.append(
        "The tracker checks prices automatically."
    )

    body.append(
        "Only actual price changes trigger this alert."
    )

    message = EmailMessage()

    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject

    message.set_content(
        "\n".join(body)
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

            server.send_message(
                message
            )

        print(
            "PRICE ALERT EMAIL SENT SUCCESSFULLY."
        )

    except Exception as exc:

        print(
            f"EMAIL_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )


# ============================================================
# TEST EMAIL
# ============================================================

def send_demo_email():
    """
    Send a test email.

    This does not use real monitor prices.
    """

    email = get_email_config()

    sender = email["sender"]
    recipient = email["recipient"]
    password = email["password"]

    if not sender or not recipient or not password:

        print(
            "Email credentials are incomplete."
        )

        print(
            "Demo email skipped."
        )

        return

    message = EmailMessage()

    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = (
        "OLED Monitor Tracker - TEST EMAIL"
    )

    message.set_content(
        """
OLED GAMING MONITOR PRICE TRACKER
=================================

TEST NOTIFICATION

This is a test email from your
OLED monitor price tracker.

The GitHub Actions email system
is working correctly.

Example:

↓ ASUS ROG Strix OLED XG32UCWMG
  Amazon.de
  €1.499,00 → €1.399,00

↑ Samsung Odyssey OLED G8 G80SD
  MediaMarkt.de
  €899,00 → €949,00

↓ LG UltraGear OLED 32GX850A-B
  Idealo.de
  €999,00 → €949,00

These are EXAMPLE prices only.
They are NOT real prices.

This test email does not represent
a real price change.
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

            server.send_message(
                message
            )

        print(
            "DEMO EMAIL SENT SUCCESSFULLY."
        )

    except Exception as exc:

        print(
            f"DEMO_EMAIL_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )


# ============================================================
# MAIN TRACKER
# ============================================================

def main():

    print()
    print("=" * 70)
    print("OLED MONITOR PRICE TRACKER")
    print("=" * 70)
    print()

    # --------------------------------------------------------
    # Load configuration
    # --------------------------------------------------------

    monitors_config = load_config()

    if not monitors_config:

        print(
            "ERROR: No monitors found in monitors.json."
        )

        return

    print(
        f"Monitors configured: "
        f"{len(monitors_config)}"
    )

    # --------------------------------------------------------
    # Load state
    # --------------------------------------------------------

    state = load_state()

    if not isinstance(
        state.get("monitors"),
        dict,
    ):

        state["monitors"] = {}

    changes = []

    # --------------------------------------------------------
    # Process every monitor
    # --------------------------------------------------------

    for monitor in monitors_config:

        if not isinstance(
            monitor,
            dict,
        ):

            continue

        name = monitor.get(
            "name"
        )

        model = monitor.get(
            "model"
        )

        if not name:

            print(
                "WARNING: Monitor has no name."
            )

            continue

        if not model:

            print(
                f"WARNING: {name} has no model."
            )

            continue

        print()
        print("#" * 70)
        print(
            f"MONITOR: {name}"
        )
        print(
            f"MODEL:   {model}"
        )
        print("#" * 70)

        # ----------------------------------------------------
        # Previous state
        # ----------------------------------------------------

        previous_data = state[
            "monitors"
        ].get(
            name,
            {
                "model": model,
                "prices": {},
            },
        )

        if not isinstance(
            previous_data,
            dict,
        ):

            previous_data = {
                "model": model,
                "prices": {},
            }

        previous_prices = previous_data.get(
            "prices",
            {},
        )

        if not isinstance(
            previous_prices,
            dict,
        ):

            previous_prices = {}

        # ----------------------------------------------------
        # Current prices
        # ----------------------------------------------------

        current_prices = {}

        current_sources = {}

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

            new_price = result.get(
                "price"
            )

            old_price = previous_prices.get(
                retailer
            )

            current_prices[
                retailer
            ] = new_price

            current_sources[
                retailer
            ] = {
                "engine": result.get(
                    "engine"
                ),
                "snippet": result.get(
                    "snippet",
                    "",
                ),
            }

            change_type = classify_change(
                old_price,
                new_price,
            )

            print()
            print(
                f"{retailer}:"
            )

            print(
                f"  Previous: "
                f"{format_price(old_price)}"
            )

            print(
                f"  Current:  "
                f"{format_price(new_price)}"
            )

            print(
                f"  Status:   "
                f"{change_type}"
            )

            # ------------------------------------------------
            # IMPORTANT:
            #
            # Do NOT treat N/A as a price change.
            #
            # Only compare two real prices.
            # ------------------------------------------------

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
                        "direction": change_type,
                    }
                )

        # ----------------------------------------------------
        # Save current monitor state
        # ----------------------------------------------------

        state[
            "monitors"
        ][name] = {
            "model": model,
            "prices": current_prices,
            "sources": current_sources,
            "last_checked": datetime.now(
                timezone.utc
            ).isoformat(),
        }

    # --------------------------------------------------------
    # Update timestamp
    # --------------------------------------------------------

    state[
        "last_updated"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    # --------------------------------------------------------
    # Save state
    # --------------------------------------------------------

    save_json(
        STATE_FILE,
        state,
    )

    print()
    print(
        "state.json updated."
    )

    # --------------------------------------------------------
    # Update README
    # --------------------------------------------------------

    update_readme(
        state["monitors"]
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print(
        f"PRICE CHANGES DETECTED: "
        f"{len(changes)}"
    )
    print("=" * 70)

    if changes:

        for change in changes:

            print(
                f"{change['monitor']} | "
                f"{change['source']} | "
                f"{format_price(change['old_price'])} "
                f"→ "
                f"{format_price(change['new_price'])} | "
                f"{change['direction']}"
            )

    else:

        print(
            "No price changes detected."
        )

    # --------------------------------------------------------
    # ONE combined email
    # --------------------------------------------------------

    send_email(
        changes
    )

    print()
    print("=" * 70)
    print("TRACKER FINISHED")
    print("=" * 70)
    print()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
