from __future__ import annotations

"""
Panchanga Daily Scanner - v3.3 CLI (Persistent Panchanga Master + Backfill)
======================================================

Purpose
-------
This is the production Panchanga scanner with a persistent yearly data store.

It has THREE responsibilities:

1. Fetch the normal daily Panchanga fields from Drik Panchang:
   - Samvatsara
   - Ayana
   - Ritu
   - Amanta Maasa
   - Paksha
   - Tithi
   - Vaara
   - Nakshatra
   - Yoga
   - Karana
   - Sooryodaya

2. UPSERT the normal Panchanga rows into a persistent year-specific master:

       panchanga_data/panchanga_master_<YEAR>.csv

   The unique row identity is:

       Date + Location Key

   This makes one-city backfills safe: an updated city/date replaces only that
   row and never removes the other cities already stored for the date.

3. Read already-approved special events from:

       special_events_master/special_events_master_<YEAR>.csv

   and render HTML/text from the COMPLETE stored Panchanga rows for the date,
   not merely from the rows scanned in the current invocation.

The scanner DOES NOT discover, classify, or calculate Ekadashi, Parana,
festivals, Sankramana, Grahana, Jayanti, or any other special observance.
Those belong to separate festival/vrata engines and are published into the
master CSV only after validation/approval.

This separation intentionally keeps the daily scanner stable and simple.
"""


from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qs, urlencode
from zoneinfo import ZoneInfo
import re
import html
import os
import shutil
import argparse

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Page, sync_playwright


# ============================================================
# Configuration
# ============================================================

MONTH_PANCHANG_URL = (
    "https://www.drikpanchang.com/panchang/month-panchang.html"
)

DAY_PANCHANG_URL = (
    "https://www.drikpanchang.com/panchang/day-panchang.html"
)

INPUT_CSV = "cities_panchanga_updated.csv"

OUTPUT_DIR = Path("output_2o/weekly_panchanga")
INTERMEDIATE_CSV = OUTPUT_DIR / "weekly_panchanga_results.csv"
DAILY_TEXT_DIR = OUTPUT_DIR / "daily_text_files"

# Static website output. Copy the contents of this folder to your
# GitHub Pages repository (or configure GitHub Pages to serve this folder).
HTML_OUTPUT_DIR = OUTPUT_DIR / "website"

CLOUDFLARE_ANALYTICS_SNIPPET = """
<!-- Cloudflare Web Analytics --><script defer src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{"token": "729f54527b434683a9be9468e7cf4a8a"}'></script><!-- End Cloudflare Web Analytics -->
""".strip()

# ------------------------------------------------------------------
# RUN MODES
# ------------------------------------------------------------------
# DAILY         : normal production scan. Usually all configured cities, one day.
# BACKFILL      : scan only TEST_CITY_NAMES over one or more historical dates and
#                 safely merge those rows into the persistent Panchanga master.
# REBUILD_ONLY  : make no Drik calls; rebuild requested dates from the two masters.
# BOOTSTRAP_ONLY: one-time migration helper. Reconstruct base Panchanga history
#                 from existing daily_text_files and write it into the Panchanga
#                 master. It intentionally does NOT rebuild old HTML automatically.
RUN_MODE = "DAILY"

SCAN_START_DATE = "2026-08-27"
SCAN_NUM_DAYS = 1

# Keep None in production.
TEST_CITY_LIMIT = None

# DAILY: [] means all cities.
# BACKFILL: list only the newly added/repaired city or cities.
# Example: TEST_CITY_NAMES = ["Buffalo"]
TEST_CITY_NAMES: list[str] = []

# Safety: BACKFILL normally requires an explicit city list so an accidental
# historical all-city rescan does not happen.
ALLOW_ALL_CITIES_IN_BACKFILL = False

# Persistent base-Panchanga store. Special-event text is NOT stored here.
PANCHANGA_MASTER_DIR = Path("panchanga_data")
PANCHANGA_MASTER_TEMPLATE = "panchanga_master_{year}.csv"
PANCHANGA_MASTER_BACKUP_DIR = PANCHANGA_MASTER_DIR / "backups"

# Current-run raw scan output is retained for troubleshooting.
LAST_SCAN_CSV = OUTPUT_DIR / "last_scan_results.csv"

# A failed/partial fresh scan must never replace a previously good master row.
PRESERVE_EXISTING_ON_SCAN_ERROR = True
REQUIRE_CORE_FIELDS_BEFORE_MASTER_WRITE = True

# BOOTSTRAP_ONLY options. Existing daily text files are parsed only for normal
# Panchanga fields. Legacy Special Events/Notes are deliberately NOT imported.
BOOTSTRAP_OVERWRITE_EXISTING = False

# Migration-only fallback. BOOTSTRAP_ONLY also snapshots the old public event
# fields found in daily_text_files. These are NOT treated as newly approved
# festival calculations; they are used only to preserve already-published old
# pages when a historical date must be regenerated. A row from the approved
# special-events master always overrides this legacy snapshot.
LEGACY_SPECIAL_EVENTS_DIR = SPECIAL_EVENTS_MASTER_DIR if "SPECIAL_EVENTS_MASTER_DIR" in globals() else Path("special_events_master")
LEGACY_SPECIAL_EVENTS_SUBDIR = Path("legacy_import")
LEGACY_SPECIAL_EVENTS_TEMPLATE = "legacy_special_events_snapshot_{year}.csv"

HEADLESS = False

PAGE_LOAD_WAIT_MS = 8000
AFTER_CITY_WAIT_MS = 7000
AFTER_DATE_WAIT_MS = 7000
BETWEEN_DATES_MS = 2500
BETWEEN_CITIES_MS = 3000

# Reuses cookies and prior CAPTCHA verification between runs.
PLAYWRIGHT_PROFILE_DIR = Path("playwright_profile")

# Extra pause after navigations to reduce rapid automated traffic.
CAPTCHA_SAFE_WAIT_MS = 2500


# ============================================================
# General helpers
# ============================================================

NAVIGATION_NOISE = {
    "list",
    "names",
    "name",
    "more",
    "details",
    "today",
    "calendar",
    "analysis report",
}


BAD_EXTRACTED_SUFFIXES = [
    "Calculation Service",
    "Calculation Services",
    "Calendar Service",
    "Calendar Services",
    "Panchang Calculation",
    "Panchang Calculations",
]

CITY_NAME_ALIASES = {
    "mysuru": ["Mysuru", "Mysore"],
    "mysore": ["Mysuru", "Mysore"],
    "udupi": ["Udupi", "Udipi"],
    "udipi": ["Udupi", "Udipi"],
    "baroda": ["Baroda", "Vadodara"],
    "vadodara": ["Baroda", "Vadodara"],
    "stonybrook": ["Stonybrook", "Stony Brook"],
    "stony brook": ["Stonybrook", "Stony Brook"],
    "new york": ["New York", "New York City"],
    "new york city": ["New York", "New York City"],
    "placentia": ["Placentia", "Placencia"],
    "placencia": ["Placentia", "Placencia"],
    "thiruvananthapuram": [
        "Thiruvananthapuram",
        "Trivandrum",
        "Tiruvananthapuram",
    ],
    "tiruvananthapuram": [
        "Thiruvananthapuram",
        "Trivandrum",
        "Tiruvananthapuram",
    ],
    "trivandrum": [
        "Thiruvananthapuram",
        "Trivandrum",
        "Tiruvananthapuram",
    ],
    "bhubaneswar": ["Bhubaneswar", "Bhuvaneshwar", "Bhubaneshwar"],
    "bhuvaneshwar": ["Bhubaneswar", "Bhuvaneshwar", "Bhubaneshwar"],
    "tirupati": ["Tirupati", "Tirupathi"],
    "tirupathi": ["Tirupati", "Tirupathi"],
    "prayagraj": ["Prayagraj", "Allahabad"],
    "allahabad": ["Prayagraj", "Allahabad"],
    "canberra": ["Canberra", "Australian Capital Territory"],
    "australian capital territory": ["Canberra", "Australian Capital Territory"],
    "christchurch": ["Christchurch", "Christ Church"],
    "christ church": ["Christchurch", "Christ Church"],
}


def generate_date_range(start_date: str, num_days: int) -> list[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    return [
        (start + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(num_days)
    ]




def yyyy_mm_dd_to_dd_mm_yyyy(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m/%Y")


def get_geoname_id(row: pd.Series) -> str:
    """
    Returns geoname_id from the city CSV when present.

    Handles CSV values read by pandas as strings like '5391959.0'.
    If geoname_id is blank, the scanner falls back to the older city-search
    workflow.
    """
    raw_value = row.get("geoname_id", "")
    value = clean_value(raw_value)

    if not value:
        return ""

    # Pandas may read numeric IDs as floats and convert them to strings
    # like '5391959.0'. Drik Panchang expects the integer ID.
    if re.fullmatch(r"\d+\.0+", value):
        value = value.split(".", 1)[0]

    if re.fullmatch(r"\d+", value):
        return value

    return value


def build_panchang_url(
    base_url: str,
    geoname_id: str,
    date_str: str,
) -> str:
    """
    Builds a Drik Panchang URL directly from geoname-id and date.

    Keep the date as dd/mm/yyyy with literal slashes. Drik Panchang's page
    scripts are more reliable with the same URL format generated by the site.
    """
    return (
        f"{base_url}?geoname-id={clean_value(geoname_id)}"
        f"&date={yyyy_mm_dd_to_dd_mm_yyyy(date_str)}"
    )


def open_panchang_url(
    page: Page,
    url: str,
    reason: str,
    expected_geoname_id: str = "",
    display_city: str = "",
    date_str: str = "",
) -> None:
    """
    Opens a fully specified Drik Panchang URL.

    Retries transient browser/network failures such as net::ERR_CONNECTION_CLOSED
    before allowing the city to become a scan-warning card.
    """
    print(f"Opening direct Drik URL: {url}")

    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            if attempt > 1:
                print(
                    f"Retrying Drik URL for {display_city} on {date_str} "
                    f"({attempt}/3)."
                )
                page.wait_for_timeout(5000)

            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=90000,
            )
            page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
            wait_for_manual_captcha(
                page,
                reason=reason,
            )

            if expected_geoname_id:
                validate_direct_geoname_page(
                    page=page,
                    expected_geoname_id=expected_geoname_id,
                    display_city=display_city,
                    date_str=date_str,
                )

            return

        except Exception as exc:
            last_error = exc
            error_text = str(exc)

            retryable = any(
                marker in error_text
                for marker in [
                    "ERR_CONNECTION_CLOSED",
                    "ERR_CONNECTION_RESET",
                    "ERR_TIMED_OUT",
                    "Timeout",
                    "Navigation timeout",
                ]
            )

            if not retryable or attempt >= 3:
                raise

    if last_error:
        raise last_error


def format_date(date_value: Any) -> str:
    if date_value is None or pd.isna(date_value):
        return ""
    dt = pd.to_datetime(date_value, errors="coerce")
    if pd.isna(dt):
        return str(date_value)
    return dt.strftime("%A, %B %d, %Y")


def format_datetime(dt: datetime | None) -> str:
    if dt is None:
        return ""
    value = dt.strftime("%d %B %Y, %I:%M:%S %p")
    return value[1:] if value.startswith("0") else value


def clean_panchanga_field_value(value: object) -> str:
    """
    Removes Drik UI/helper text accidentally captured with Panchanga values.

    Examples:
        "Purnima upto 05:26 AM, Jun 30; calculation tool"
            -> "Purnima upto 05:26 AM, Jun 30"

        "calculation tool; Chaturdashi upto 09:36 AM"
            -> "Chaturdashi upto 09:36 AM"
    """
    if value is None:
        return ""

    cleaned = str(value).strip()

    # Remove the helper phrase wherever it appears.
    cleaned = re.sub(
        r"\bcalculation\s+tool\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    # Clean delimiters left behind by removing the helper phrase.
    cleaned = re.sub(r"^\s*[;,]\s*", "", cleaned).strip()
    cleaned = re.sub(r"\s*[;,]\s*$", "", cleaned).strip()
    cleaned = re.sub(r"\s*;\s*;", ";", cleaned).strip()
    cleaned = re.sub(r"\s*,\s*,", ",", cleaned).strip()
    cleaned = re.sub(r"\s*;\s*,\s*", "; ", cleaned).strip()
    cleaned = re.sub(r"\s*,\s*;\s*", "; ", cleaned).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    return cleaned


PANCHANGA_VALUE_FIELDS_TO_CLEAN = [
    "Tithi",
    "Nakshatra",
    "Yoga",
    "Karana",
    "Amanta Maasa",
    "Paksha",
    "Vaara",
    "Sooryodaya",
]


def clean_record_panchanga_values(record: dict) -> dict:
    for field in PANCHANGA_VALUE_FIELDS_TO_CLEAN:
        if field in record:
            record[field] = clean_panchanga_field_value(record.get(field))
    return record


def clean_records_panchanga_values(records: list[dict]) -> list[dict]:
    return [clean_record_panchanga_values(record) for record in records]


def clean_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""

    cleaned = re.sub(r"\s+", " ", str(value)).strip()

    for suffix in BAD_EXTRACTED_SUFFIXES:
        cleaned = re.sub(
            rf"(;\s*)?{re.escape(suffix)}\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()

    return cleaned


def strip_ordinal_suffix(text: str) -> str:
    return re.sub(
        r"(\d+)(st|nd|rd|th)",
        r"\1",
        str(text),
        flags=re.IGNORECASE,
    )


def normalize_lines(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in str(text).splitlines()
        if line and line.strip()
    ]


def is_noise_value(value: str) -> bool:
    normalized = str(value).strip().lower()
    if not normalized:
        return True
    return normalized in NAVIGATION_NOISE


def validate_direct_geoname_page(
    page: Page,
    expected_geoname_id: str,
    display_city: str,
    date_str: str,
) -> None:
    """
    Verifies that the loaded Drik page URL contains the expected geoname-id.

    This prevents a stale browser/profile location from silently producing
    Panchanga values for a previous city while the output row is labelled as
    the requested city.
    """
    expected = clean_value(expected_geoname_id)

    if not expected:
        return

    current_url = page.url

    if f"geoname-id={expected}" not in current_url:
        raise RuntimeError(
            "Drik Panchang direct geoname navigation did not load the expected "
            f"location for {display_city} on {date_str}. "
            f"Expected geoname-id={expected}, but current URL is: {current_url}"
        )


# ============================================================
# Date/time parsing
# ============================================================


def parse_time_on_date(
    time_text: str,
    date_str: str,
    timezone_str: str,
) -> datetime | None:
    if not time_text:
        return None

    normalized = re.sub(r"\s+", " ", str(time_text)).strip()
    base_date = datetime.strptime(date_str, "%Y-%m-%d")
    timezone = ZoneInfo(normalize_timezone_name(str(timezone_str)))

    for fmt in ["%I:%M:%S %p", "%I:%M %p"]:
        try:
            parsed_time = datetime.strptime(normalized, fmt).time()
            return datetime(
                base_date.year,
                base_date.month,
                base_date.day,
                parsed_time.hour,
                parsed_time.minute,
                parsed_time.second,
                tzinfo=timezone,
            )
        except ValueError:
            continue

    print(f"Could not parse time: {time_text}")
    return None


def parse_drik_datetime(
    value: str,
    timezone_str: str,
) -> datetime | None:
    if not value:
        return None

    text = strip_ordinal_suffix(str(value))
    text = re.sub(
        r"\b(EDT|EST|CDT|CST|PDT|PST|MDT|MST|IST)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip()
    timezone = ZoneInfo(normalize_timezone_name(str(timezone_str)))

    formats = [
        "%I:%M:%S %p on %B %d, %Y",
        "%I:%M %p on %B %d, %Y",
        "%I:%M:%S %p on %b %d, %Y",
        "%I:%M %p on %b %d, %Y",
        "%d %B %Y, %I:%M:%S %p",
        "%d %B %Y, %I:%M %p",
        "%d %b %Y, %I:%M:%S %p",
        "%d %b %Y, %I:%M %p",
        "%B %d, %Y, %I:%M:%S %p",
        "%B %d, %Y, %I:%M %p",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone)
        except ValueError:
            continue

    print(f"Could not parse Drik datetime: {value}")
    return None


# ============================================================
# Browser interaction
# ============================================================


def captcha_is_present(page: Page) -> bool:
    """
    Detects active CAPTCHA / human-verification pages.

    After a CAPTCHA is solved, hidden reCAPTCHA iframes/textareas can remain
    in the DOM. Those should not be treated as an active CAPTCHA.
    """
    if page.is_closed():
        return False

    try:
        city_input = page.locator("#dp-direct-city-search")

        if city_input.count() > 0 and city_input.first.is_visible():
            return False
    except Exception:
        pass

    try:
        title = page.title().strip().lower()

        if any(
            marker in title
            for marker in [
                "just a moment",
                "attention required",
                "verify",
                "captcha",
                "security check",
            ]
        ):
            return True
    except Exception:
        pass

    try:
        body_text = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        body_text = ""

    active_text_markers = [
        "i'm not a robot",
        "i am not a robot",
        "select all images",
        "verify you are human",
        "checking your browser",
        "just a moment",
        "attention required",
        "security check",
        "complete the security check",
    ]

    if any(marker in body_text for marker in active_text_markers):
        return True

    iframe_selectors = [
        'iframe[title*="reCAPTCHA"]',
        'iframe[src*="recaptcha"]',
        'iframe[src*="hcaptcha"]',
    ]

    for selector in iframe_selectors:
        try:
            frames = page.locator(selector)
            count = min(frames.count(), 5)

            for index in range(count):
                frame = frames.nth(index)

                try:
                    if not frame.is_visible():
                        continue

                    box = frame.bounding_box()

                    if not box:
                        continue

                    if box.get("width", 0) < 120 or box.get("height", 0) < 60:
                        continue

                    return True
                except Exception:
                    continue
        except Exception:
            continue

    return False


def wait_for_manual_captcha(
    page: Page,
    reason: str = "",
) -> None:
    """
    Pauses whenever an active CAPTCHA appears.

    Complete the challenge in the opened browser, return to the terminal,
    and press Enter. The function exits as soon as the normal Drik city-search
    field is visible again.
    """
    if not captcha_is_present(page):
        return

    print("\n" + "!" * 100)
    print("HUMAN VERIFICATION REQUIRED")
    if reason:
        print(f"Detected while: {reason}")
    print(
        "Complete the CAPTCHA in the browser window. "
        "Then return here and press Enter."
    )
    print("!" * 100)

    while True:
        input("Press Enter after completing the CAPTCHA... ")
        page.wait_for_timeout(3000)

        try:
            city_input = page.locator("#dp-direct-city-search")

            if city_input.count() > 0 and city_input.first.is_visible():
                print("Verification cleared. Resuming scanner.\n")
                page.wait_for_timeout(CAPTCHA_SAFE_WAIT_MS)
                return
        except Exception:
            pass

        if not captcha_is_present(page):
            print("Verification cleared. Resuming scanner.\n")
            page.wait_for_timeout(CAPTCHA_SAFE_WAIT_MS)
            return

        print(
            "The verification screen still appears to be present. "
            "If the page looks normal, wait a few seconds and press Enter again."
        )


def ensure_open_page(
    context: BrowserContext,
    page: Page | None,
) -> Page:
    if page is None or page.is_closed():
        print("Creating replacement browser tab.")
        return context.new_page()
    return page


def ensure_page_at_url(page: Page, url: str) -> None:
    if page.is_closed():
        raise RuntimeError("Cannot navigate because the browser tab is closed.")

    if not page.url.startswith(url):
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=90000,
        )
        page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
        wait_for_manual_captcha(
            page,
            reason=f"opening {url}",
        )


def neutralize_ad_overlays(page: Page) -> None:
    """
    Prevents advertisement iframes/overlays from intercepting clicks or focus.
    Safe to call repeatedly because the style element is created only once.
    """
    try:
        page.evaluate(
            """
            () => {
                const styleId = 'dp-playwright-ad-shield';
                if (!document.getElementById(styleId)) {
                    const style = document.createElement('style');
                    style.id = styleId;
                    style.textContent = `
                        iframe[title="Advertisement"],
                        iframe[id^="aswift_"],
                        ins.adsbygoogle,
                        div[id^="google_ads_"],
                        .google-auto-placed,
                        [data-vignette-loaded="true"] {
                            pointer-events: none !important;
                        }
                    `;
                    document.head.appendChild(style);
                }
            }
            """
        )
    except Exception:
        # Page may be between navigations; the caller can safely continue.
        pass


def focus_and_clear_input(page: Page, locator) -> None:
    """
    Focuses and clears an input without a mouse click.

    If a CAPTCHA appears, the scanner pauses for manual verification.
    It avoids repeated rapid reloads, which can trigger more challenges.
    """
    last_error: Exception | None = None

    for attempt in range(1, 4):
        wait_for_manual_captcha(
            page,
            reason="waiting for the city-search field",
        )

        try:
            neutralize_ad_overlays(page)
            locator.wait_for(state="visible", timeout=30000)

            locator.evaluate(
                """
                element => {
                    element.focus();
                    element.value = '';
                    element.dispatchEvent(
                        new Event('input', {bubbles: true})
                    );
                    element.dispatchEvent(
                        new Event('change', {bubbles: true})
                    );
                }
                """
            )
            return

        except Exception as exc:
            last_error = exc

            if captcha_is_present(page):
                wait_for_manual_captcha(
                    page,
                    reason="city-search field was replaced by verification",
                )
                locator = page.locator("#dp-direct-city-search")
                continue

            if attempt >= 3:
                break

            print(
                "City-search field is not ready. "
                f"Waiting before retry {attempt + 1}/3."
            )
            page.wait_for_timeout(5000)
            locator = page.locator("#dp-direct-city-search")

    raise RuntimeError(
        "The Drik Panchang city-search input did not become visible "
        "after three attempts."
    ) from last_error


def click_city_suggestion(
    page: Page,
    suggestions,
    suggestion_index: int,
) -> None:
    """
    Selects a city suggestion through DOM mouse events.
    This bypasses ad iframes that can intercept normal Playwright clicks.
    """
    neutralize_ad_overlays(page)

    target = suggestions.nth(suggestion_index)
    target.wait_for(state="attached", timeout=30000)

    target.evaluate(
        """
        element => {
            element.scrollIntoView({
                block: 'center',
                inline: 'nearest'
            });

            const options = {
                bubbles: true,
                cancelable: true,
                view: window
            };

            element.dispatchEvent(new MouseEvent('mousedown', options));
            element.dispatchEvent(new MouseEvent('mouseup', options));
            element.dispatchEvent(new MouseEvent('click', options));
        }
        """
    )

    page.wait_for_timeout(AFTER_CITY_WAIT_MS)


def get_city_aliases(search_city: str) -> list[str]:
    aliases = CITY_NAME_ALIASES.get(
        str(search_city).strip().lower(),
        [str(search_city).strip()],
    )
    return list(dict.fromkeys(aliases))


def normalize_location_piece(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def selected_location_matches(
    selected_value: str,
    aliases: list[str],
    country: str,
) -> bool:
    """
    Performs strict validation.

    The old check used substring matching, so the requested city "Greece"
    incorrectly matched "Athens, Greece". This version requires the first
    comma-separated location component to exactly match a valid city alias.
    It also verifies the country when the country is present in the input value.
    """
    selected_value = str(selected_value).strip()

    if not selected_value:
        return False

    pieces = [
        piece.strip()
        for piece in selected_value.split(",")
        if piece.strip()
    ]

    if not pieces:
        return False

    selected_city = normalize_location_piece(pieces[0])
    alias_values = {
        normalize_location_piece(alias)
        for alias in aliases
    }

    if selected_city not in alias_values:
        return False

    # Drik commonly displays "United States" instead of "USA".
    expected_country = normalize_location_piece(country)
    valid_country_terms = {expected_country}

    if expected_country in {"usa", "united states"}:
        valid_country_terms.update({"usa", "united states"})

    # Some Drik input values contain only "City, Country".
    if len(pieces) >= 2:
        displayed_country = normalize_location_piece(pieces[-1])

        if displayed_country not in valid_country_terms:
            return False

    return True


def choose_city_suggestion(
    page: Page,
    suggestions,
    suggestion_index: int,
) -> None:
    """
    Selects the exact visible autocomplete item using DOM mouse events.

    Keyboard selection was unreliable on Drik Panchang because the active
    autocomplete item was not always synchronized with ArrowDown/Enter.
    """
    neutralize_ad_overlays(page)

    target = suggestions.nth(suggestion_index)
    target.wait_for(state="visible", timeout=30000)

    old_url = page.url

    target.evaluate(
        """
        element => {
            element.scrollIntoView({
                block: 'center',
                inline: 'nearest'
            });

            const options = {
                bubbles: true,
                cancelable: true,
                view: window
            };

            element.dispatchEvent(new MouseEvent('mouseover', options));
            element.dispatchEvent(new MouseEvent('mousedown', options));
            element.dispatchEvent(new MouseEvent('mouseup', options));
            element.dispatchEvent(new MouseEvent('click', options));
        }
        """
    )

    # The site may update the same page or navigate. Allow either.
    try:
        page.wait_for_function(
            """
            oldUrl => {
                const input = document.querySelector('#dp-direct-city-search');
                const value = input ? input.value.trim() : '';
                return window.location.href !== oldUrl || value.length > 0;
            }
            """,
            old_url,
            timeout=30000,
        )
    except Exception:
        pass

    page.wait_for_timeout(AFTER_CITY_WAIT_MS)


def set_city(
    page: Page,
    search_city: str,
    state_or_region: str,
    country: str,
) -> str:
    """
    Selects and strictly verifies the requested city.

    Search attempts progress from the city name to more specific combinations
    including state/region and country.
    """
    aliases = get_city_aliases(search_city)

    query_candidates: list[str] = []

    for alias in aliases:
        query_candidates.extend(
            [
                alias,
                f"{alias} {state_or_region}" if state_or_region else "",
                f"{alias} {state_or_region} {country}"
                if state_or_region and country
                else "",
                f"{alias} {country}" if country else "",
            ]
        )

    query_candidates = [
        value
        for value in dict.fromkeys(query_candidates)
        if value
    ]

    last_error = ""

    for query_text in query_candidates:
        print(f"Trying city query: {query_text}")

        location_input = page.locator("#dp-direct-city-search")
        focus_and_clear_input(page, location_input)

        page.keyboard.type(query_text, delay=220)
        page.wait_for_timeout(4000)

        suggestions = page.locator("ul.ui-autocomplete li:visible")
        suggestion_count = suggestions.count()

        if suggestion_count == 0:
            last_error = f"No suggestions for query '{query_text}'"
            continue

        candidates: list[dict[str, Any]] = []

        for i in range(suggestion_count):
            suggestion = suggestions.nth(i)

            try:
                suggestion_text = suggestion.inner_text().strip()
            except Exception:
                continue

            lines = [
                line.strip()
                for line in suggestion_text.splitlines()
                if line.strip()
            ]

            if not lines:
                continue

            first_line = lines[0]
            first_normalized = normalize_location_piece(first_line)
            suggestion_normalized = normalize_location_piece(suggestion_text)

            alias_values = {
                normalize_location_piece(alias)
                for alias in aliases
            }

            # Never accept a suggestion whose displayed city is not an exact
            # requested alias. This blocks Athens for the city Greece.
            if first_normalized not in alias_values:
                continue

            score = 100

            if (
                state_or_region
                and normalize_location_piece(state_or_region)
                in suggestion_normalized
            ):
                score += 50

            expected_country = normalize_location_piece(country)
            country_terms = {expected_country}

            if expected_country in {"usa", "united states"}:
                country_terms.update({"usa", "united states"})

            if any(
                term and term in suggestion_normalized
                for term in country_terms
            ):
                score += 30

            candidates.append(
                {
                    "index": i,
                    "text": suggestion_text,
                    "score": score,
                }
            )

        if not candidates:
            last_error = (
                f"No exact city suggestion for '{query_text}'. "
                f"Expected one of {aliases}."
            )
            continue

        candidates.sort(
            key=lambda item: item["score"],
            reverse=True,
        )

        best = candidates[0]
        print(f"Selected city suggestion: {best['text']}")

        choose_city_suggestion(
            page=page,
            suggestions=suggestions,
            suggestion_index=best["index"],
        )

        selected_input = page.locator("#dp-direct-city-search")

        try:
            selected_input.wait_for(state="visible", timeout=30000)

        except Exception as exc:
            if captcha_is_present(page):
                wait_for_manual_captcha(
                    page,
                    reason=(
                        "city selection triggered verification for "
                        f"{search_city}"
                    ),
                )
                return set_city(
                    page=page,
                    search_city=search_city,
                    state_or_region=state_or_region,
                    country=country,
                )

            last_error = (
                f"City input was not visible after choosing suggestion "
                f"for query '{query_text}': {type(exc).__name__}: {exc}"
            )
            continue

        selected_value = selected_input.input_value().strip()

        print(f"Selected location value: {selected_value}")

        if selected_location_matches(
            selected_value=selected_value,
            aliases=aliases,
            country=country,
        ):
            return selected_value

        if captcha_is_present(page):
            wait_for_manual_captcha(
                page,
                reason=(
                    "city validation triggered verification for "
                    f"{search_city}"
                ),
            )
            return set_city(
                page=page,
                search_city=search_city,
                state_or_region=state_or_region,
                country=country,
            )

        last_error = (
            f"Query '{query_text}' produced '{selected_value}', "
            f"which failed strict city/country validation."
        )

    raise RuntimeError(
        f"Could not select city '{search_city}', "
        f"{state_or_region}, {country}. {last_error}"
    )


def find_date_input(page: Page):
    preferred = page.locator("#dp-date-picker")

    if preferred.count() > 0:
        return preferred.first

    inputs = page.locator("input")

    for i in range(inputs.count()):
        element = inputs.nth(i)

        try:
            value = element.input_value()
        except Exception:
            continue

        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", value or ""):
            return element

    raise RuntimeError("Could not find Drik Panchang date input")


def set_date(page: Page, date_str: str) -> None:
    date_input = find_date_input(page)
    requested_value = yyyy_mm_dd_to_dd_mm_yyyy(date_str)
    current_value = date_input.input_value()

    if current_value == requested_value:
        return

    # Avoid mouse clicks because ads can also cover the date field.
    focus_and_clear_input(page, date_input)
    page.keyboard.type(requested_value, delay=80)
    page.keyboard.press("Enter")

    page.wait_for_timeout(AFTER_DATE_WAIT_MS)
    wait_for_manual_captcha(
        page,
        reason=f"changing the date to {date_str}",
    )

    current_value = find_date_input(page).input_value()

    if current_value != requested_value:
        raise RuntimeError(
            f"Date did not update correctly. "
            f"Expected {requested_value}, got {current_value}"
        )


# ============================================================
# Label extraction
# ============================================================


def extract_line_value(text: str, labels: list[str]) -> str:
    for line in normalize_lines(text):
        for label in labels:
            match = re.match(
                rf"^{re.escape(label)}\s*:?\s*(.+)$",
                line,
                flags=re.IGNORECASE,
            )
            if not match:
                continue

            value = clean_value(match.group(1).strip())
            if not is_noise_value(value):
                return value

    return ""


def extract_all_line_values(
    text: str,
    labels: list[str],
) -> list[str]:
    values: list[str] = []

    for line in normalize_lines(text):
        for label in labels:
            match = re.match(
                rf"^{re.escape(label)}\s*:?\s*(.+)$",
                line,
                flags=re.IGNORECASE,
            )
            if not match:
                continue

            value = clean_value(match.group(1).strip())
            if is_noise_value(value):
                continue

            if value not in values:
                values.append(value)

    return values


# ============================================================
# Month-page extraction
# ============================================================


def extract_month_page_values(month_text: str) -> dict[str, str]:
    """
    Extracts all month-page Panchanga fields.

    Yoga and Karana can occur multiple times on the same day, so all matching
    entries are preserved in page order and joined with semicolons.
    """
    tithis = extract_all_line_values(month_text, ["Tithi"])
    nakshatras = extract_all_line_values(month_text, ["Nakshatra"])
    yogas = extract_all_line_values(month_text, ["Yoga"])
    karanas = extract_all_line_values(month_text, ["Karana"])

    return {
        "Amanta Maasa": extract_line_value(
            month_text,
            ["Amanta Month", "Amanta Maasa"],
        ),
        "Paksha": extract_line_value(month_text, ["Paksha"]),
        "Tithi": "; ".join(tithis),
        "Vaara": extract_line_value(month_text, ["Weekday", "Vaara"]),
        "Nakshatra": "; ".join(nakshatras),
        "Yoga": "; ".join(yogas),
        "Karana": "; ".join(karanas),
        "Sooryodaya": extract_line_value(month_text, ["Sunrise"]),
    }


# ============================================================
# Day-page extraction
# ============================================================


def extract_shaka_samvatsara_name(day_text: str) -> str:
    value = extract_line_value(day_text, ["Shaka Samvat"])
    if not value:
        return ""

    value = re.sub(r"^\d+\s*", "", value).strip()
    return "" if is_noise_value(value) else value


def extract_vedic_ritu(day_text: str) -> str:
    """
    Extracts Vedic Ritu rather than Drik Ritu.
    """
    value = extract_line_value(day_text, ["Vedic Ritu"])

    if not value or is_noise_value(value):
        return ""

    stop_terms = [
        "Dinamana",
        "Ratrimana",
        "Madhyahna",
        "Drik Ritu",
        "Drik Ayana",
        "Vedic Ayana",
    ]

    for term in stop_terms:
        value = re.split(
            rf"\b{re.escape(term)}\b",
            value,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

    return value


def extract_vedic_ayana(day_text: str) -> str:
    """
    Extracts Vedic Ayana rather than Drik Ayana.

    Example:
        Drik Ayana  : Dakshinayana
        Vedic Ayana : Uttarayana

    This function returns Uttarayana.
    """
    value = extract_line_value(day_text, ["Vedic Ayana"])

    if not value or is_noise_value(value):
        return ""

    match = re.search(
        r"\b(Uttarayana|Dakshinayana)\b",
        value,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1)

    stop_terms = [
        "Madhyahna",
        "Dinamana",
        "Ratrimana",
        "Drik Ayana",
        "Drik Ritu",
        "Vedic Ritu",
    ]

    for term in stop_terms:
        value = re.split(
            rf"\b{re.escape(term)}\b",
            value,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

    return value


def extract_day_page_values(day_text: str) -> dict[str, str]:
    return {
        "Samvatsara": extract_shaka_samvatsara_name(day_text),
        "Ritu": extract_vedic_ritu(day_text),
        "Ayana": extract_vedic_ayana(day_text),
    }




# ============================================================
# Month-page weekly scan
# ============================================================


def scan_month_page_for_city(
    month_page: Page,
    row: pd.Series,
    scan_dates: list[str],
    input_order: int,
) -> list[dict[str, Any]]:
    """Fetch only normal Panchanga values from the Drik month page.

    No festival/event section is parsed here. Special events are injected later
    from the approved master CSV.
    """
    display_city = str(row["display_city"])
    search_city = str(row["search_city"])
    state_or_region = str(row["state_or_region"])
    country = str(row["country"])
    timezone = str(row["timezone"])
    geoname_id = get_geoname_id(row)

    print("\n" + "=" * 100)
    print(f"MONTH PAGE SCAN: {display_city}")
    print("=" * 100)

    selected_location = ""

    if not geoname_id:
        ensure_page_at_url(month_page, MONTH_PANCHANG_URL)
        selected_location = set_city(
            month_page,
            search_city,
            state_or_region,
            country,
        )
    else:
        selected_location = search_city or display_city
        print(
            f"Using geoname-id for {display_city}: {geoname_id}. "
            "Skipping city autocomplete."
        )

    records: list[dict[str, Any]] = []

    for date_str in scan_dates:
        print(f"Month page: {display_city} - {date_str}")

        if geoname_id:
            direct_url = build_panchang_url(
                base_url=MONTH_PANCHANG_URL,
                geoname_id=geoname_id,
                date_str=date_str,
            )
            open_panchang_url(
                page=month_page,
                url=direct_url,
                reason=f"opening month Panchanga for {display_city} on {date_str}",
                expected_geoname_id=geoname_id,
                display_city=display_city,
                date_str=date_str,
            )
        else:
            set_date(month_page, date_str)

        page_url = month_page.url
        page_text = month_page.locator("body").inner_text(timeout=30000)
        month_values = extract_month_page_values(page_text)

        records.append(
            {
                "Input Order": input_order,
                "Date": date_str,
                "City": display_city,
                "State/Region": state_or_region,
                "Country": country,
                "Timezone": timezone,
                "Geoname ID": geoname_id,
                "Selected Drik Location": selected_location,
                "Amanta Maasa": month_values["Amanta Maasa"],
                "Paksha": month_values["Paksha"],
                "Tithi": month_values["Tithi"],
                "Vaara": month_values["Vaara"],
                "Nakshatra": month_values["Nakshatra"],
                "Yoga": month_values["Yoga"],
                "Karana": month_values["Karana"],
                "Sooryodaya": month_values["Sooryodaya"],
                "Month Page URL": page_url,
            }
        )

        month_page.wait_for_timeout(BETWEEN_DATES_MS)

    return records


# ============================================================
# Day-page weekly scan
# ============================================================


def scan_day_page_for_city(
    day_page: Page,
    row: pd.Series,
    scan_dates: list[str],
    input_order: int,
) -> list[dict[str, Any]]:
    display_city = str(row["display_city"])
    search_city = str(row["search_city"])
    state_or_region = str(row["state_or_region"])
    country = str(row["country"])
    geoname_id = get_geoname_id(row)

    print("\n" + "=" * 100)
    print(f"DAY PAGE SCAN: {display_city}")
    print("=" * 100)

    # If geoname_id is available, skip the city search/autocomplete.
    if not geoname_id:
        ensure_page_at_url(day_page, DAY_PANCHANG_URL)

        set_city(
            day_page,
            search_city,
            state_or_region,
            country,
        )
    else:
        print(
            f"Using geoname-id for {display_city}: {geoname_id}. "
            "Skipping city autocomplete."
        )

    records: list[dict[str, Any]] = []

    for date_str in scan_dates:
        print(f"Day page: {display_city} - {date_str}")

        if geoname_id:
            direct_url = build_panchang_url(
                base_url=DAY_PANCHANG_URL,
                geoname_id=geoname_id,
                date_str=date_str,
            )
            open_panchang_url(
                page=day_page,
                url=direct_url,
                reason=f"opening day Panchanga for {display_city} on {date_str}",
                expected_geoname_id=geoname_id,
                display_city=display_city,
                date_str=date_str,
            )
        else:
            set_date(day_page, date_str)

        page_url = day_page.url
        page_text = day_page.locator("body").inner_text(timeout=30000)

        day_values = extract_day_page_values(page_text)

        records.append(
            {
                "Input Order": input_order,
                "Date": date_str,
                "City": display_city,
                "Samvatsara": day_values["Samvatsara"],
                "Ayana": day_values["Ayana"],
                "Ritu": day_values["Ritu"],
                "Day Page URL": page_url,
            }
        )

        day_page.wait_for_timeout(BETWEEN_DATES_MS)

    return records




# ============================================================
# Merge month/day records
# ============================================================


def merge_month_and_day_records(
    month_records: list[dict[str, Any]],
    day_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge the normal month/day Panchanga records.

    Special-event fields are intentionally blank at this stage. They are filled
    only from the approved special-events master after all city scans finish.
    """
    month_df = pd.DataFrame(month_records)
    day_df = pd.DataFrame(day_records)

    merged_df = month_df.merge(
        day_df,
        on=["Input Order", "Date", "City"],
        how="left",
        validate="one_to_one",
    )

    merged_df["Special Events"] = ""
    merged_df["Special Event Details"] = ""
    merged_df["Note"] = ""
    merged_df["Message"] = ""
    merged_df["Master Event Count"] = 0
    merged_df["Error"] = ""

    final_columns = [
        "Input Order",
        "Date",
        "City",
        "State/Region",
        "Country",
        "Timezone",
        "Geoname ID",
        "Selected Drik Location",
        "Samvatsara",
        "Ayana",
        "Ritu",
        "Amanta Maasa",
        "Paksha",
        "Tithi",
        "Vaara",
        "Nakshatra",
        "Yoga",
        "Karana",
        "Sooryodaya",
        "Special Events",
        "Special Event Details",
        "Note",
        "Message",
        "Master Event Count",
        "Month Page URL",
        "Day Page URL",
        "Error",
    ]

    for column in final_columns:
        if column not in merged_df.columns:
            merged_df[column] = ""

    return merged_df[final_columns].to_dict(orient="records")



# ============================================================
# Process one city
# ============================================================


def process_city_once(
    month_page: Page,
    day_page: Page,
    row: pd.Series,
    scan_dates: list[str],
    input_order: int,
) -> list[dict[str, Any]]:
    """Fetch normal Panchanga only. No special-event enrichment occurs here."""
    month_records = scan_month_page_for_city(
        month_page=month_page,
        row=row,
        scan_dates=scan_dates,
        input_order=input_order,
    )

    day_records = scan_day_page_for_city(
        day_page=day_page,
        row=row,
        scan_dates=scan_dates,
        input_order=input_order,
    )

    merged_records = merge_month_and_day_records(
        month_records=month_records,
        day_records=day_records,
    )
    return clean_records_panchanga_values(merged_records)


def process_city(
    month_page: Page,
    day_page: Page,
    row: pd.Series,
    scan_dates: list[str],
    input_order: int,
) -> list[dict[str, Any]]:
    display_city = clean_value(row.get("display_city", ""))

    for attempt in range(1, 3):
        try:
            return process_city_once(
                month_page=month_page,
                day_page=day_page,
                row=row,
                scan_dates=scan_dates,
                input_order=input_order,
            )
        except Exception:
            month_has_captcha = captcha_is_present(month_page)
            day_has_captcha = captcha_is_present(day_page)

            if not (month_has_captcha or day_has_captcha):
                raise

            print(
                f"CAPTCHA interrupted {display_city}. "
                f"Completing verification and retrying city ({attempt}/2)."
            )
            if month_has_captcha:
                wait_for_manual_captcha(
                    month_page,
                    reason=f"retrying month Panchanga for {display_city}",
                )
            if day_has_captcha:
                wait_for_manual_captcha(
                    day_page,
                    reason=f"retrying day Panchanga for {display_city}",
                )

    raise RuntimeError(f"Could not complete Panchanga scan for {display_city}")


# ============================================================
# Plain-text output
# ============================================================

# ============================================================
# Timezone grouping for final text output
# ============================================================

TIMEZONE_DISPLAY_ORDER = {
    # Fallback order only. Main HTML/text ordering now uses UTC offset
    # from each row's IANA timezone, sorted east-to-west.
    "NZDT": 1,
    "NZST": 1,
    "AEDT": 2,
    "AEST": 2,
    "ACDT": 3,
    "ACST": 3,
    "AWDT": 4,
    "AWST": 4,
    "JST": 5,
    "KST": 5,
    "IST": 6,
    "GST": 7,
    "AST": 8,
    "EEST": 9,
    "EET": 9,
    "CEST": 10,
    "CET": 10,
    "BST": 11,
    "GMT": 11,
    "UTC": 11,
    "EDT": 12,
    "EST": 12,
    "CDT": 13,
    "CST": 13,
    "MDT": 14,
    "MST": 14,
    "PDT": 15,
    "PST": 15,
}

# Fix common non-IANA timezone values that may appear in the city CSV.
# These are city-specific assumptions for the current location list:
# Dallas/Houston -> Central Time; Seattle -> Pacific Time.
TIMEZONE_NAME_ALIASES = {
    "America/Texas": "America/Chicago",
    "America/Illinois": "America/Chicago",
    "America/Washington": "America/Los_Angeles",
    "America/Arizona": "America/Phoenix",
    "America/California": "America/Los_Angeles",
    "America/Georgia": "America/New_York",
    "America/Florida": "America/New_York",
    "America/Ohio": "America/New_York",
    "America/Tennessee": "America/Chicago",
    "America/North_Carolina": "America/New_York",
    "America/Indiana": "America/Indiana/Indianapolis",
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
    "Australia/Queensland": "Australia/Brisbane",
    "Australia/Victoria": "Australia/Melbourne",
    "Australia/South_Australia": "Australia/Adelaide",
    "Australia/Western_Australia": "Australia/Perth",
    "Australia/New_South_Wales": "Australia/Sydney",
    "Australia/Australian_Capital_Territory": "Australia/Sydney",
    "New_Zealand/Auckland": "Pacific/Auckland",
    "New_Zealand/Wellington": "Pacific/Auckland",
    "New_Zealand/Canterbury": "Pacific/Auckland",
    "Europe/Germany": "Europe/Berlin",
    "Germany/Berlin": "Europe/Berlin",
    "Asia/Doha": "Asia/Qatar",
    "Qatar/Doha": "Asia/Qatar",
    "UAE/Dubai": "Asia/Dubai",
    "Asia/UAE": "Asia/Dubai",
}


TIMEZONE_ABBREVIATION_ALIASES = {
    # Python ZoneInfo returns numeric labels for some zones.
    # These labels are clearer for website grouping.
    ("Asia/Qatar", "+03"): "AST",
    ("Asia/Dubai", "+04"): "GST",
}


def normalize_timezone_name(timezone_name: str) -> str:
    value = str(timezone_name).strip()
    return TIMEZONE_NAME_ALIASES.get(value, value)


def get_timezone_abbreviation(timezone_name: str, date_value: str) -> str:
    normalized_timezone = normalize_timezone_name(timezone_name)

    try:
        tz = ZoneInfo(normalized_timezone)
        local_dt = datetime.strptime(
            str(date_value), "%Y-%m-%d"
        ).replace(hour=12, tzinfo=tz)

        abbreviation = local_dt.tzname()

        display_abbreviation = TIMEZONE_ABBREVIATION_ALIASES.get(
            (normalized_timezone, abbreviation),
            abbreviation,
        )

        if display_abbreviation:
            return display_abbreviation

    except Exception as exc:
        print(
            f"Could not resolve timezone '{timezone_name}' "
            f"(normalized as '{normalized_timezone}'): {exc}"
        )

    return normalized_timezone or "Unknown Timezone"


def get_timezone_offset_minutes(timezone_name: str, date_value: str) -> int:
    """
    Returns UTC offset in minutes for east-to-west ordering.

    Higher values are farther east. Example:
    New Zealand > Australia > India > Middle East > Europe > Americas.
    """
    normalized_timezone = normalize_timezone_name(timezone_name)

    try:
        tz = ZoneInfo(normalized_timezone)
        local_dt = datetime.strptime(
            str(date_value), "%Y-%m-%d"
        ).replace(hour=12, tzinfo=tz)

        offset = local_dt.utcoffset()

        if offset is None:
            return -999999

        return int(offset.total_seconds() // 60)

    except Exception as exc:
        print(
            f"Could not resolve timezone offset for '{timezone_name}' "
            f"(normalized as '{normalized_timezone}'): {exc}"
        )

    return -999999


def timezone_sort_key(timezone_abbreviation: str) -> tuple[int, str]:
    abbreviation = str(timezone_abbreviation).strip()
    return (
        TIMEZONE_DISPLAY_ORDER.get(abbreviation, 99),
        abbreviation,
    )





def format_special_events_for_display(events_value: Any) -> str:
    """
    Keeps one event unnumbered. When there are multiple events, displays:
    1) Event one
    2) Event two
    """
    events = [
        clean_value(item)
        for item in clean_value(events_value).split(";")
        if clean_value(item)
    ]
    events = list(dict.fromkeys(events))

    if len(events) <= 1:
        return events[0] if events else ""

    return "\n".join(
        f"{index}) {event}"
        for index, event in enumerate(events, start=1)
    )


def build_daily_text(
    df_timezone: pd.DataFrame,
    date_value: str,
    timezone_abbreviation: str,
) -> str:
    """
    Creates one text message for one date and one timezone.
    Cities are sorted alphabetically within the timezone.
    """
    df_timezone = df_timezone.copy()

    lines = [
        f"Daily Panchanga - {format_date(date_value)}",
        f"Timezone: {timezone_abbreviation}",
        "",
    ]

    def first_non_empty(column_name: str) -> str:
        if column_name not in df_timezone.columns:
            return ""

        for value in df_timezone[column_name]:
            cleaned = clean_value(value)

            if cleaned:
                return cleaned

        return ""

    lines.append(f"Samvatsara: {first_non_empty('Samvatsara')}")
    lines.append(f"Ayana: {first_non_empty('Ayana')}")
    lines.append("")

    df_timezone["_city_sort"] = (
        df_timezone["City"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
    )

    df_timezone = df_timezone.sort_values(
        ["_city_sort", "State/Region", "Country"],
        kind="stable",
    )

    for number, (_, row) in enumerate(
        df_timezone.iterrows(),
        start=1,
    ):
        city = clean_value(row.get("City", ""))
        state = clean_value(row.get("State/Region", ""))
        country = clean_value(row.get("Country", ""))

        location_parts = [
            value
            for value in [city, state, country]
            if value
        ]

        lines.append(f"{number}) {', '.join(location_parts)}")
        lines.append(
            f"Ritu: {clean_value(row.get('Ritu', ''))}"
        )
        lines.append(
            f"Amanta Maasa: {clean_value(row.get('Amanta Maasa', ''))}"
        )
        lines.append(
            f"Paksha: {clean_value(row.get('Paksha', ''))}"
        )
        lines.append(
            f"Tithi: {clean_value(row.get('Tithi', ''))}"
        )
        lines.append(
            f"Vaara: {clean_value(row.get('Vaara', ''))}"
        )
        lines.append(
            f"Nakshatra: {clean_value(row.get('Nakshatra', ''))}"
        )
        lines.append(
            f"Yoga: {clean_value(row.get('Yoga', ''))}"
        )
        lines.append(
            f"Karana: {clean_value(row.get('Karana', ''))}"
        )
        lines.append(
            f"Sooryodaya: {clean_value(row.get('Sooryodaya', ''))}"
        )

        events = format_special_events_for_display(
            row.get("Special Events", "")
        )
        details = clean_value(row.get("Special Event Details", ""))

        if events:
            lines.append(f"Special event: {events}")

        if details:
            lines.append(f"Special timing/details: {details}")

        note = clean_value(row.get("Note", ""))

        if note:
            lines.append(f"Note: {strip_simple_bold_markers(note)}")

        month_url = clean_value(row.get("Month Page URL", ""))

        if month_url:
            lines.append(f"{city} Panchanga: {month_url}")

        error = clean_value(row.get("Error", ""))

        if error:
            lines.append(f"Scan warning: {error}")

        lines.append("")

    lines.append(
        "Note: All dates and timings are local to the listed location."
    )

    return "\n".join(lines).strip()


def generate_daily_text_files(
    results_df: pd.DataFrame,
) -> None:
    """
    Creates one text file per date per timezone.

    Example:
    2026-06-16_IST.txt
    2026-06-16_EDT.txt
    2026-06-16_CDT.txt
    2026-06-16_MST.txt
    2026-06-16_PDT.txt
    """
    DAILY_TEXT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    HTML_OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    results_df = results_df.copy()

    results_df["Timezone Abbreviation"] = results_df.apply(
        lambda row: get_timezone_abbreviation(
            clean_value(row.get("Timezone", "")),
            clean_value(row.get("Date", "")),
        ),
        axis=1,
    )

    results_df["Timezone Offset Minutes"] = results_df.apply(
        lambda row: get_timezone_offset_minutes(
            clean_value(row.get("Timezone", "")),
            clean_value(row.get("Date", "")),
        ),
        axis=1,
    )

    for date_value, df_day in results_df.groupby(
        "Date",
        sort=True,
    ):
        timezone_groups = (
            df_day.groupby("Timezone Abbreviation", dropna=True)[
                "Timezone Offset Minutes"
            ]
            .max()
            .sort_values(ascending=False)
            .index.astype(str)
            .tolist()
        )

        for timezone_abbreviation in timezone_groups:
            df_timezone = df_day[
                df_day["Timezone Abbreviation"]
                == timezone_abbreviation
            ].copy()

            if df_timezone.empty:
                continue

            text = build_daily_text(
                df_timezone=df_timezone,
                date_value=date_value,
                timezone_abbreviation=timezone_abbreviation,
            )

            safe_timezone = re.sub(
                r"[^A-Za-z0-9_-]+",
                "_",
                timezone_abbreviation,
            )

            output_path = (
                DAILY_TEXT_DIR
                / f"{date_value}_{safe_timezone}.txt"
            )

            output_path.write_text(
                text,
                encoding="utf-8",
            )

            print(f"Created: {output_path}")


# ============================================================
# HTML website generation
# ============================================================

def inject_cloudflare_analytics(html_text: str) -> str:
    """Add the Cloudflare beacon once, immediately before </body>."""
    if "static.cloudflareinsights.com/beacon.min.js" in html_text:
        return html_text

    body_end = html_text.lower().rfind("</body>")
    if body_end < 0:
        raise RuntimeError("Could not find </body> while adding analytics.")

    return (
        html_text[:body_end]
        + "\n"
        + CLOUDFLARE_ANALYTICS_SNIPPET
        + "\n"
        + html_text[body_end:]
    )



def html_escape(value: Any) -> str:
    return html.escape(clean_value(value), quote=True)


def strip_simple_bold_markers(value: Any) -> str:
    """Remove **...** markers for plain-text outputs."""
    return clean_value(value).replace("**", "")


def html_with_simple_bold(value: Any) -> str:
    """Safely render only **...** as <strong>; escape every other character."""
    escaped = html_escape(value)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def city_details_html(row: pd.Series) -> str:
    """
    Builds one expandable city section.
    """
    city = clean_value(row.get("City", ""))
    state = clean_value(row.get("State/Region", ""))
    country = clean_value(row.get("Country", ""))

    location = ", ".join(
        value
        for value in [city, state, country]
        if value
    )

    fields = [
        ("Ritu", row.get("Ritu", "")),
        ("Amanta Maasa", row.get("Amanta Maasa", "")),
        ("Paksha", row.get("Paksha", "")),
        ("Tithi", row.get("Tithi", "")),
        ("Vaara", row.get("Vaara", "")),
        ("Nakshatra", row.get("Nakshatra", "")),
        ("Yoga", row.get("Yoga", "")),
        ("Karana", row.get("Karana", "")),
        ("Sooryodaya", row.get("Sooryodaya", "")),
    ]

    rows_html = []

    for label, value in fields:
        cleaned = clean_value(value)

        if cleaned:
            rows_html.append(
                f"""
                <div class="field-row">
                    <div class="field-label" data-i18n-label data-original="{html_escape(label)}">{html_escape(label)}</div>
                    <div class="field-value" data-i18n-value data-original="{html_escape(cleaned)}">{html_escape(cleaned)}</div>
                </div>
                """
            )

    events = format_special_events_for_display(
        row.get("Special Events", "")
    )
    details = clean_value(row.get("Special Event Details", ""))

    if events:
        rows_html.append(
            f"""
            <div class="field-row special">
                <div class="field-label" data-i18n-fixed="special_event">Special event</div>
                <div class="field-value" style="white-space: pre-line;" data-i18n-value data-original="{html_escape(events)}">{html_escape(events)}</div>
            </div>
            """
        )

    if details:
        rows_html.append(
            f"""
            <div class="field-row special">
                <div class="field-label" data-i18n-fixed="special_details">Special timing/details</div>
                <div class="field-value" data-i18n-value data-original="{html_escape(details)}">{html_escape(details)}</div>
            </div>
            """
        )

    note = clean_value(row.get("Note", ""))

    if note:
        rows_html.append(
            f"""
            <div class="field-row special">
                <div class="field-label" data-i18n-fixed="note">Note</div>
                <div class="field-value" data-i18n-value data-simple-bold="true" data-original="{html_escape(note)}">{html_with_simple_bold(note)}</div>
            </div>
            """
        )

    month_url = clean_value(row.get("Month Page URL", ""))
    source_html = ""

    if month_url:
        parsed_url = urlparse(month_url)
        query_values = parse_qs(parsed_url.query)
        geoname_id = (
            query_values.get("geoname-id")
            or query_values.get("genome-id")
            or [""]
        )[0]
        date_parameter = (query_values.get("date") or [""])[0]

        planetary_html = ""
        if geoname_id and date_parameter:
            planetary_url = (
                "https://www.drikpanchang.com/planet/position/"
                "planetary-positions-sidereal.html?"
                + urlencode(
                    {
                        "geoname-id": geoname_id,
                        "date": date_parameter,
                    }
                )
            )
            planetary_html = (
                f'<p class="source-link planetary-source-link">'
                f'<a href="{html_escape(planetary_url)}" '
                f'target="_blank" rel="noopener noreferrer">'
                f'View Planetary Position and Kundali for '
                f'{html_escape(city)} on Drik Panchang'
                f'</a></p>'
            )

        source_html = (
            planetary_html
            + f'<p class="source-link">'
            f'<a href="{html_escape(month_url)}" '
            f'target="_blank" rel="noopener noreferrer" '
            f'data-city-source="{html_escape(city)}">'
            f'View {html_escape(city)} Panchanga on Drik Panchang'
            f'</a></p>'
        )

    error = clean_value(row.get("Error", ""))
    error_html = ""

    if error:
        error_html = (
            f'<p class="warning">Scan warning: {html_escape(error)}</p>'
        )

    return f"""
    <details class="city-card">
        <summary data-i18n-value data-original="{html_escape(location)}">{html_escape(location)}</summary>
        <div class="city-content">
            {''.join(rows_html)}
            {source_html}
            {error_html}
        </div>
    </details>
    """


def build_daily_html(
    df_day: pd.DataFrame,
    date_value: str,
    available_dates: list[str],
) -> str:
    '''
    Creates one date page using the Version 2.0 navigation structure:
    vertical date links -> alphabet buttons -> cities -> Panchanga details.

    The scanner and Panchanga extraction logic are unchanged. Only the static
    HTML presentation and navigation are reorganized.
    '''
    df_day = df_day.copy()

    def first_non_empty(column_name: str) -> str:
        if column_name not in df_day.columns:
            return ""

        for value in df_day[column_name]:
            cleaned = clean_value(value)
            if cleaned:
                return cleaned
        return ""

    def city_initial(value: Any) -> str:
        cleaned = clean_value(value)
        if not cleaned:
            return "#"
        first = cleaned[0].upper()
        return first if "A" <= first <= "Z" else "#"

    def safe_dom_id(value: Any) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "-", clean_value(value).lower()).strip("-")
        return cleaned or "item"

    df_day["_city_sort"] = (
        df_day["City"].fillna("").astype(str).str.strip().str.lower()
    )
    df_day["_city_initial"] = df_day["City"].apply(city_initial)
    df_day = df_day.sort_values(
        ["_city_sort", "State/Region", "Country"],
        kind="stable",
    )

    alphabet_sections: list[str] = []
    alphabet_buttons: list[str] = []

    for letter, df_letter in df_day.groupby("_city_initial", sort=True):
        letter_id = safe_dom_id(letter)
        alphabet_buttons.append(
            f'<button type="button" class="alphabet-button" '
            f'data-letter-target="letter-{html_escape(letter_id)}" '
            f'aria-controls="letter-{html_escape(letter_id)}" '
            f'aria-pressed="false">{html_escape(letter)}</button>'
        )

        city_sections = "".join(
            city_details_html(row)
            for _, row in df_letter.iterrows()
        )

        alphabet_sections.append(
            f'''
            <section
                id="letter-{html_escape(letter_id)}"
                class="letter-panel"
                data-letter-panel
                hidden
            >
                <h2 class="letter-heading">
                    <span data-i18n-fixed="cities_beginning">Cities beginning with</span>
                    {html_escape(letter)}
                </h2>
                <div class="city-list">
                    {city_sections}
                </div>
            </section>
            '''
        )

    samvatsara = first_non_empty("Samvatsara")
    ayana = first_non_empty("Ayana")

    sorted_dates = sorted(set(str(value) for value in available_dates if value))
    if date_value not in sorted_dates:
        sorted_dates.append(date_value)
        sorted_dates.sort()

    current_index = sorted_dates.index(date_value)
    window_start = max(0, current_index - 3)
    window_end = min(len(sorted_dates), window_start + 7)
    window_start = max(0, window_end - 7)
    displayed_dates = sorted_dates[window_start:window_end]

    date_content_markup = f'''
        <section class="date-content" aria-label="Panchanga for selected date">
            <div class="common-grid">
                <div class="common-item">
                    <div class="common-label" data-i18n-fixed="samvatsara">Samvatsara</div>
                    <div class="common-value" data-i18n-value data-original="{html_escape(samvatsara)}">{html_escape(samvatsara)}</div>
                </div>
                <div class="common-item">
                    <div class="common-label" data-i18n-fixed="ayana">Ayana</div>
                    <div class="common-value" data-i18n-value data-original="{html_escape(ayana)}">{html_escape(ayana)}</div>
                </div>
            </div>

            <p class="alphabet-instruction" data-i18n-fixed="select_city_letter">
                Select the first letter of your city
            </p>
            <div class="alphabet-grid" role="group" aria-label="City initials">
                {''.join(alphabet_buttons)}
            </div>
            {''.join(alphabet_sections)}
        </section>
    '''

    date_items: list[str] = []
    for listed_date in displayed_dates:
        is_active = listed_date == date_value
        active_class = " active-date" if is_active else ""
        current_marker = ' aria-current="page"' if is_active else ""
        date_link = f'''
            <a class="date-item{active_class}"
               href="{html_escape(listed_date)}.html"{current_marker}>
                <span class="date-item-label" data-date-value="{html_escape(listed_date)}">
                    {html_escape(format_date(listed_date))}
                </span>
                <span class="date-item-icon" aria-hidden="true">
                    {"−" if is_active else "+"}
                </span>
            </a>
        '''
        date_items.append(
            f'<div class="date-entry">{date_link}{date_content_markup if is_active else ""}</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Worldwide daily Hindu Panchanga by date and city">
    <title>Daily Panchanga - {html_escape(format_date(date_value))}</title>
    <style>
        :root {{
            color-scheme: light;
            --bg: #fffaf0;
            --card: #ffffff;
            --primary: #8c3b12;
            --secondary: #d97706;
            --text: #2b2118;
            --muted: #6b5b4b;
            --border: #ead8c5;
            --soft: #fff1dc;
            --shadow: 0 8px 24px rgba(85, 45, 16, 0.08);
        }}

        * {{ box-sizing: border-box; }}

        body {{
            margin: 0;
            font-family: Arial, Helvetica, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.5;
        }}

        .page {{
            width: min(980px, calc(100% - 24px));
            margin: 0 auto;
            padding: 24px 0 48px;
        }}

        .hero {{
            background: linear-gradient(135deg, #fff7e8, #ffe8bf);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 24px;
            box-shadow: var(--shadow);
            margin-bottom: 18px;
        }}

        .hero-top {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 18px;
        }}

        .language-control {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex: 0 0 auto;
        }}

        .language-control label {{
            color: var(--muted);
            font-weight: 700;
            font-size: 0.9rem;
        }}

        .language-control select {{
            border: 1px solid var(--border);
            border-radius: 10px;
            background: rgba(255,255,255,0.86);
            color: var(--text);
            padding: 8px 10px;
            font-size: 0.95rem;
            cursor: pointer;
        }}

        h1 {{
            margin: 0 0 8px;
            color: var(--primary);
            font-size: clamp(1.65rem, 4vw, 2.4rem);
        }}

        .date-navigation {{
            display: grid;
            gap: 10px;
            margin: 0 0 18px;
        }}

        .date-entry {{
            display: grid;
            gap: 10px;
        }}

        .date-item {{
            min-height: 54px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            padding: 14px 18px;
            border: 1px solid var(--border);
            border-radius: 14px;
            background: var(--card);
            color: var(--primary);
            text-decoration: none;
            font-weight: 700;
            box-shadow: var(--shadow);
        }}

        .date-item:hover {{ background: #fff7eb; }}

        .date-item.active-date {{
            background: var(--primary);
            border-color: var(--primary);
            color: #fff;
        }}

        .date-item-icon {{
            width: 30px;
            height: 30px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            background: rgba(140, 59, 18, 0.10);
            font-size: 1.2rem;
            flex: 0 0 auto;
        }}

        .active-date .date-item-icon {{ background: rgba(255,255,255,0.18); }}

        .date-content {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 18px;
            box-shadow: var(--shadow);
            padding: 20px;
            margin-bottom: 18px;
        }}

        .common-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 20px;
        }}

        .common-item {{
            background: var(--soft);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 12px;
        }}

        .common-label {{
            color: var(--muted);
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .common-value {{ font-weight: 700; margin-top: 3px; }}

        .alphabet-instruction {{
            margin: 0 0 12px;
            color: var(--muted);
            font-weight: 700;
        }}

        .alphabet-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(58px, 1fr));
            gap: 12px;
            margin-bottom: 20px;
        }}

        .alphabet-button {{
            min-width: 58px;
            min-height: 52px;
            padding: 10px 14px;
            border: 2px solid var(--border);
            border-radius: 11px;
            background: #fff;
            color: var(--primary);
            font: inherit;
            font-size: 1.1rem;
            font-weight: 800;
            cursor: pointer;
            touch-action: manipulation;
        }}

        .alphabet-button:hover {{ background: var(--soft); }}
        .alphabet-button:focus-visible {{
            outline: 3px solid rgba(217, 119, 6, 0.42);
            outline-offset: 2px;
        }}
        .alphabet-button[aria-pressed="true"] {{
            background: var(--primary);
            border-color: var(--primary);
            color: #fff;
        }}

        .letter-panel {{ margin-top: 8px; }}
        .letter-heading {{
            margin: 0 0 12px;
            color: var(--primary);
            font-size: 1.1rem;
        }}

        details {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 14px;
        }}

        summary {{ cursor: pointer; font-weight: 700; list-style: none; }}
        summary::-webkit-details-marker {{ display: none; }}

        .city-card {{ margin-bottom: 10px; box-shadow: none; overflow: hidden; }}
        .city-card > summary {{ padding: 15px 16px; }}
        .city-card > summary::after {{ content: "+"; float: right; }}
        .city-card[open] > summary::after {{ content: "−"; }}
        .city-content {{ padding: 0 16px 16px; }}

        .field-row {{
            display: grid;
            grid-template-columns: minmax(125px, 0.34fr) 1fr;
            gap: 12px;
            padding: 8px 0;
            border-top: 1px solid #f2e8dd;
        }}

        .field-label {{ color: var(--muted); font-weight: 700; }}
        .special {{ background: #fffaf2; }}
        .source-link a {{ color: var(--primary); font-weight: 700; text-decoration: none; }}
        .source-link a:hover {{ text-decoration: underline; }}
        .warning {{ color: #9b1c1c; font-weight: 700; }}
        .footer {{ color: var(--muted); text-align: center; margin-top: 24px; font-size: 0.9rem; }}

        @media (max-width: 680px) {{
            .common-grid {{ grid-template-columns: 1fr; }}
            .field-row {{ grid-template-columns: 1fr; gap: 3px; }}
            .page {{ width: min(100% - 16px, 980px); padding-top: 12px; }}
            .hero {{ padding: 18px; }}
            .hero-top {{ flex-direction: column; }}
            .language-control {{ width: 100%; justify-content: flex-end; }}
            .date-content {{ padding: 14px; }}
            .alphabet-grid {{
                grid-template-columns: repeat(4, minmax(0, 1fr));
                gap: 12px;
            }}
            .alphabet-button {{ min-width: 0; min-height: 54px; }}
        }}

        @media (max-width: 380px) {{
            .alphabet-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
        }}
    </style>
</head>
<body>
    <main class="page">
        <section class="hero">
            <div class="hero-top">
                <div>
                    <h1 data-i18n-fixed="daily_panchanga">Daily Panchanga</h1>
                    <div id="page-date" data-date-value="{html_escape(date_value)}">
                        {html_escape(format_date(date_value))}
                    </div>
                </div>
                <div class="language-control">
                    <label for="language-select" data-i18n-fixed="language">Language</label>
                    <select id="language-select" aria-label="Language">
                        <option value="en">English</option>
                        <option value="kn">ಕನ್ನಡ</option>
                    </select>
                </div>
            </div>
        </section>

        <nav id="date-navigation" class="date-navigation" aria-label="Panchanga dates">
            {''.join(date_items)}
        </nav>

        <p class="footer" data-i18n-fixed="footer_note">
            All dates and timings are local to the listed location.
            Source details are linked city-wise to Drik Panchang.
        </p>
    </main>

    <script>
        (function () {{
            const buttons = Array.from(document.querySelectorAll("[data-letter-target]"));
            const panels = Array.from(document.querySelectorAll("[data-letter-panel]"));

            function closeCityCards() {{
                document.querySelectorAll(".city-card[open]").forEach(card => {{
                    card.open = false;
                }});
            }}

            function selectLetter(button) {{
                const targetId = button.dataset.letterTarget;
                const alreadySelected = button.getAttribute("aria-pressed") === "true";

                buttons.forEach(item => item.setAttribute("aria-pressed", "false"));
                panels.forEach(panel => panel.hidden = true);
                closeCityCards();

                if (alreadySelected) return;

                button.setAttribute("aria-pressed", "true");
                const target = document.getElementById(targetId);
                if (target) target.hidden = false;
            }}

            buttons.forEach(button => {{
                button.addEventListener("click", () => selectLetter(button));
            }});

            document.querySelectorAll(".city-card").forEach(card => {{
                card.addEventListener("toggle", () => {{
                    if (!card.open) return;
                    document.querySelectorAll(".city-card[open]").forEach(other => {{
                        if (other !== card) other.open = false;
                    }});
                }});
            }});
        }})();
    </script>

    <script id="panchanga-language-switcher">
        (function () {{
            const fixedTranslations = {{
                en: {{
                    daily_panchanga: "Daily Panchanga",
                    language: "Language",
                    samvatsara: "Samvatsara",
                    ayana: "Ayana",
                    ritu: "Ritu",
                    time_zone: "Time zone",
                    select_city_letter: "Select the first letter of your city",
                    cities_beginning: "Cities beginning with",
                    special_event: "Special event",
                    special_details: "Special timing/details",
                note: "Note",
                    footer_note:
                        "All dates and timings are local to the listed location. " +
                        "Source details are linked city-wise to Drik Panchang."
                }},
                kn: {{
                    daily_panchanga: "ದೈನಂದಿನ ಪಂಚಾಂಗ",
                    language: "ಭಾಷೆ",
                    samvatsara: "ಸಂವತ್ಸರ",
                    ayana: "ಅಯನ",
                    ritu: "ಋತು",
                    time_zone: "ಸಮಯ ವಲಯ",
                    select_city_letter: "ನಿಮ್ಮ ನಗರದ ಮೊದಲ ಇಂಗ್ಲಿಷ್ ಅಕ್ಷರವನ್ನು ಆಯ್ಕೆಮಾಡಿ",
                    cities_beginning: "ಈ ಅಕ್ಷರದಿಂದ ಆರಂಭವಾಗುವ ನಗರಗಳು",
                    special_event: "ವಿಶೇಷ ಆಚರಣೆ",
                    special_details: "ವಿಶೇಷ ಸಮಯ / ವಿವರಗಳು",
                note: "ಸೂಚನೆ",
                    footer_note:
                        "ಎಲ್ಲ ದಿನಾಂಕಗಳು ಮತ್ತು ಸಮಯಗಳು ಸೂಚಿಸಿದ ಸ್ಥಳದ ಸ್ಥಳೀಯ ಸಮಯದಲ್ಲಿವೆ. " +
                        "ಪ್ರತಿ ನಗರದ ಮೂಲ ವಿವರಗಳು ದೃಕ್ ಪಂಚಾಂಗಕ್ಕೆ ಸಂಪರ್ಕಿಸಲ್ಪಟ್ಟಿವೆ."
                }}
            }};

            const terms = {{
                "Amanta Maasa": "ಅಮಾಂತ ಮಾಸ",
                "Paksha": "ಪಕ್ಷ",
                "Tithi": "ತಿಥಿ",
                "Vaara": "ವಾರ",
                "Nakshatra": "ನಕ್ಷತ್ರ",
                "Yoga": "ಯೋಗ",
                "Karana": "ಕರಣ",
                "Sooryodaya": "ಸೂರ್ಯೋದಯ",
                "Nag Panchami": "ನಾಗ ಪಂಚಮಿ",
                "Naga Panchami": "ನಾಗ ಪಂಚಮಿ",
                "Kalki Jayanti": "ಕಲ್ಕಿ ಜಯಂತಿ",
                "Kalki Jayanthi": "ಕಲ್ಕಿ ಜಯಂತಿ",
                "Varalakshmi Vrat": "ವರಮಹಾಲಕ್ಷ್ಮೀ ವ್ರತ",
                "Varamahalakshmi Vrat": "ವರಮಹಾಲಕ್ಷ್ಮೀ ವ್ರತ",
                "Raksha Bandhan": "ರಕ್ಷಾ ಬಂಧನ",
                "Rakhi": "ರಾಖಿ",
                "Surya Grahan": "ಸೂರ್ಯ ಗ್ರಹಣ",
                "Solar Eclipse": "ಸೂರ್ಯ ಗ್ರಹಣ",
                "Chandra Grahan": "ಚಂದ್ರ ಗ್ರಹಣ",
                "Lunar Eclipse": "ಚಂದ್ರ ಗ್ರಹಣ",
                "Sutak Begins": "ಸೂತಕ ಆರಂಭ",
                "Sutak Ends": "ಸೂತಕ ಅಂತ್ಯ",
                "Eclipse Start": "ಗ್ರಹಣ ಆರಂಭ",
                "Maximum Eclipse": "ಗ್ರಹಣ ಮಧ್ಯಕಾಲ",
                "Eclipse End": "ಗ್ರಹಣ ಅಂತ್ಯ",
                "Parabhava": "ಪರಾಭವ",
                "Uttarayana": "ಉತ್ತರಾಯಣ",
                "Dakshinayana": "ದಕ್ಷಿಣಾಯನ",
                "Grishma (Summer)": "ಗ್ರೀಷ್ಮ ಋತು",
                "Shukla Paksha": "ಶುಕ್ಲ ಪಕ್ಷ",
                "Krishna Paksha": "ಕೃಷ್ಣ ಪಕ್ಷ",
                "Pratipada": "ಪ್ರತಿಪದಾ",
                "Dwitiya": "ದ್ವಿತೀಯಾ",
                "Tritiya": "ತೃತೀಯಾ",
                "Chaturthi": "ಚತುರ್ಥೀ",
                "Panchami": "ಪಂಚಮಿ",
                "Shashthi": "ಷಷ್ಠೀ",
                "Saptami": "ಸಪ್ತಮಿ",
                "Ashtami": "ಅಷ್ಟಮಿ",
                "Navami": "ನವಮಿ",
                "Dashami": "ದಶಮಿ",
                "Ekadashi": "ಏಕಾದಶಿ",
                "Dwadashi": "ದ್ವಾದಶಿ",
                "Trayodashi": "ತ್ರಯೋದಶಿ",
                "Chaturdashi": "ಚತುರ್ದಶಿ",
                "Purnima": "ಪೌರ್ಣಮಿ",
                "Amavasya": "ಅಮಾವಾಸ್ಯೆ",
                "Raviwara": "ರವಿವಾರ",
                "Somawara": "ಸೋಮವಾರ",
                "Mangalawara": "ಮಂಗಳವಾರ",
                "Budhawara": "ಬುಧವಾರ",
                "Guruwara": "ಗುರುವಾರ",
                "Shukrawara": "ಶುಕ್ರವಾರ",
                "Shaniwara": "ಶನಿವಾರ",
                "Jyeshtha": "ಜ್ಯೇಷ್ಠ",
                "Jyeshtha (Adhik)": "ಅಧಿಕ ಜ್ಯೇಷ್ಠ",
                "Ashadha": "ಆಷಾಢ",
                "Shravana": "ಶ್ರಾವಣ",
                "Bhadrapada": "ಭಾದ್ರಪದ",
                "Ashwin": "ಆಶ್ವಯುಜ",
                "Kartika": "ಕಾರ್ತಿಕ",
                "Margashirsha": "ಮಾರ್ಗಶಿರ",
                "Pausha": "ಪುಷ್ಯ",
                "Magha": "ಮಾಘ",
                "Phalguna": "ಫಾಲ್ಗುಣ",
                "Chaitra": "ಚೈತ್ರ",
                "Vaishakha": "ವೈಶಾಖ",
                "Ashwini": "ಅಶ್ವಿನಿ",
                "Bharani": "ಭರಣಿ",
                "Krittika": "ಕೃತ್ತಿಕಾ",
                "Rohini": "ರೋಹಿಣಿ",
                "Mrigashira": "ಮೃಗಶಿರ",
                "Ardra": "ಆರ್ದ್ರಾ",
                "Punarvasu": "ಪುನರ್ವಸು",
                "Pushya": "ಪುಷ್ಯ",
                "Ashlesha": "ಆಶ್ಲೇಷ",
                "Magha": "ಮಘಾ",
                "Purva Phalguni": "ಪೂರ್ವ ಫಲ್ಗುಣಿ",
                "Uttara Phalguni": "ಉತ್ತರ ಫಲ್ಗುಣಿ",
                "Hasta": "ಹಸ್ತ",
                "Chitra": "ಚಿತ್ರಾ",
                "Swati": "ಸ್ವಾತಿ",
                "Vishakha": "ವಿಶಾಖಾ",
                "Anuradha": "ಅನುರಾಧಾ",
                "Jyeshtha": "ಜ್ಯೇಷ್ಠಾ",
                "Mula": "ಮೂಲ",
                "Purva Ashadha": "ಪೂರ್ವಾಷಾಢ",
                "Uttara Ashadha": "ಉತ್ತರಾಷಾಢ",
                "Shravana": "ಶ್ರವಣ",
                "Dhanishtha": "ಧನಿಷ್ಠಾ",
                "Shatabhisha": "ಶತಭಿಷ",
                "Purva Bhadrapada": "ಪೂರ್ವಭಾದ್ರಪದ",
                "Uttara Bhadrapada": "ಉತ್ತರಭಾದ್ರಪದ",
                "Revati": "ರೇವತಿ",
                "Sukarma": "ಸುಕರ್ಮ",
                "Dhriti": "ಧೃತಿ",
                "Shula": "ಶೂಲ",
                "Ganda": "ಗಂಡ",
                "Vriddhi": "ವೃದ್ಧಿ",
                "Dhruva": "ಧ್ರುವ",
                "Harshana": "ಹರ್ಷಣ",
                "Siddhi": "ಸಿದ್ಧಿ",
                "Shubha": "ಶುಭ",
                "Shiva": "ಶಿವ",
                "Bava": "ಬವ",
                "Balava": "ಬಾಲವ",
                "Kaulava": "ಕೌಲವ",
                "Taitila": "ತೈತಿಲ",
                "Garaja": "ಗರಜ",
                "Vanija": "ವಣಿಜ",
                "Vishti": "ವಿಷ್ಟಿ",
                "Shakuni": "ಶಕುನಿ",
                "Naga": "ನಾಗ",
                "Parana": "ಪಾರಣ",
                "Arunodaya": "ಅರುಣೋದಯ",
                "Upavaasa": "ಉಪವಾಸ",
                "Upavasa": "ಉಪವಾಸ",
                "Ekadashi Tithi start": "ಏಕಾದಶಿ ತಿಥಿ ಆರಂಭ",
                "Ekadashi Tithi end": "ಏಕಾದಶಿ ತಿಥಿ ಅಂತ್ಯ",
                "Upavaasa date": "ಉಪವಾಸ ದಿನಾಂಕ",
                "Parana Time": "ಪಾರಣ ಸಮಯ",
                "Sankranti Moment": "ಸಂಕ್ರಾಂತಿ ಕ್ಷಣ",
                "Punya Kala": "ಪುಣ್ಯಕಾಲ",
                "Maha Punya Kala": "ಮಹಾಪುಣ್ಯಕಾಲ",
                "upto": "ರ ವರೆಗೆ",
                "No Ekadashi Upavasa today as it is Dashami Viddha. Kindly do Upavasa tomorrow and lookup for Parana Time in tomorrow's panchanga.": "ಇಂದು ದಶಮಿ ವಿದ್ಧ ಇರುವುದರಿಂದ ಏಕಾದಶಿ ಉಪವಾಸ ಇಲ್ಲ. ದಯವಿಟ್ಟು ನಾಳೆ ಉಪವಾಸ ಮಾಡಿ ಮತ್ತು ಪಾರಣ ಸಮಯವನ್ನು ನಾಳೆಯ ಪಂಚಾಂಗದಲ್ಲಿ ನೋಡಿ.",
                "Dashami Viddha": "ದಶಮಿ ವಿದ್ಧ",
                "Ekadashi Upavasa to be observed today": "ಇಂದು ಏಕಾದಶಿ ಉಪವಾಸ ಆಚರಿಸಬೇಕು",
                "Parana should be performed after Sunrise and Pooja": "ಸೂರ್ಯೋದಯ ಮತ್ತು ಪೂಜೆಯ ನಂತರ ಪಾರಣ ಮಾಡಬೇಕು",
                "No Ekadashi Upavasa today": "ಇಂದು ಏಕಾದಶಿ ಉಪವಾಸ ಇಲ್ಲ",
                "Full Night": "ಪೂರ್ಣ ರಾತ್ರಿ",
                "Jan": "ಜನವರಿ",
                "Feb": "ಫೆಬ್ರವರಿ",
                "Mar": "ಮಾರ್ಚ್",
                "Apr": "ಏಪ್ರಿಲ್",
                "May": "ಮೇ",
                "Jun": "ಜೂನ್",
                "Jul": "ಜುಲೈ",
                "Aug": "ಆಗಸ್ಟ್",
                "Sep": "ಸೆಪ್ಟೆಂಬರ್",
                "Oct": "ಅಕ್ಟೋಬರ್",
                "Nov": "ನವೆಂಬರ್",
                "Dec": "ಡಿಸೆಂಬರ್",
                "India": "ಭಾರತ",
                "United States": "ಅಮೆರಿಕಾ",
                "Australia": "ಆಸ್ಟ್ರೇಲಿಯಾ",
                "New Zealand": "ನ್ಯೂಜಿಲ್ಯಾಂಡ್",
                "Queensland": "ಕ್ವೀನ್ಸ್‌ಲ್ಯಾಂಡ್",
                "Victoria": "ವಿಕ್ಟೋರಿಯಾ",
                "South Australia": "ದಕ್ಷಿಣ ಆಸ್ಟ್ರೇಲಿಯಾ",
                "Western Australia": "ಪಶ್ಚಿಮ ಆಸ್ಟ್ರೇಲಿಯಾ",
                "Australian Capital Territory": "ಆಸ್ಟ್ರೇಲಿಯನ್ ಕ್ಯಾಪಿಟಲ್ ಟೆರಿಟರಿ",
                "New South Wales": "ನ್ಯೂ ಸೌತ್ ವೇಲ್ಸ್",
                "Canterbury": "ಕ್ಯಾಂಟರ್ಬರಿ",
                "Brisbane": "ಬ್ರಿಸ್ಬೇನ್",
                "Melbourne": "ಮೆಲ್ಬರ್ನ್",
                "Adelaide": "ಅಡಿಲೇಡ್",
                "Perth": "ಪರ್ಥ್",
                "Canberra": "ಕ್ಯಾನ್ಬೆರಾ",
                "Sydney": "ಸಿಡ್ನಿ",
                "Auckland": "ಆಕ್ಲೆಂಡ್",
                "Wellington": "ವೆಲ್ಲಿಂಗ್ಟನ್",
                "Christchurch": "ಕ್ರೈಸ್ಟ್‌ಚರ್ಚ್",
                "Karnataka": "ಕರ್ನಾಟಕ",
                "New York": "ನ್ಯೂಯಾರ್ಕ್",
                "Florida": "ಫ್ಲೋರಿಡಾ",
                "Texas": "ಟೆಕ್ಸಾಸ್",
                "Illinois": "ಇಲಿನಾಯ್",
                "California": "ಕ್ಯಾಲಿಫೋರ್ನಿಯಾ",
                "Washington": "ವಾಷಿಂಗ್ಟನ್",
                "Arizona": "ಅರಿಜೋನಾ",
                "Telangana": "ತೆಲಂಗಾಣ",
                "Maharashtra": "ಮಹಾರಾಷ್ಟ್ರ",
                "Gujarat": "ಗುಜರಾತ್",
                "Virginia": "ವರ್ಜೀನಿಯಾ",
                "Maryland": "ಮೆರಿಲ್ಯಾಂಡ್",
                "Pennsylvania": "ಪೆನ್ಸಿಲ್ವೇನಿಯಾ",
                "Ohio": "ಒಹಾಯೊ",
                "Tennessee": "ಟೆನೆಸಿ",
                "North Carolina": "ಉತ್ತರ ಕ್ಯಾರೊಲೈನಾ",
                "Indiana": "ಇಂಡಿಯಾನಾ",
                "Bengaluru": "ಬೆಂಗಳೂರು",
                "Mysuru": "ಮೈಸೂರು",
                "Mangalore": "ಮಂಗಳೂರು",
                "Udupi": "ಉಡುಪಿ",
                "Hyderabad": "ಹೈದರಾಬಾದ್",
                "Pune": "ಪುಣೆ",
                "Surat": "ಸೂರತ್",
                "Pittsford": "ಪಿಟ್ಸ್‌ಫರ್ಡ್",
                "Belfast":"ಬೆಲ್‌ಫಾಸ್ಟ್",
                "Greece": "ಗ್ರೀಸ್",
                "Rochester": "ರೋಚೆಸ್ಟರ್",
                "Chicago": "ಶಿಕಾಗೊ",
                "Dallas": "ಡಲ್ಲಾಸ್",
                "Houston": "ಹ್ಯೂಸ್ಟನ್",
                "Phoenix": "ಫೀನಿಕ್ಸ್",
                "Los Angeles": "ಲಾಸ್ ಏಂಜಲೀಸ್",
                "San Francisco": "ಸ್ಯಾನ್ ಫ್ರಾನ್ಸಿಸ್ಕೊ",
                "Seattle": "ಸಿಯಾಟಲ್",
                "Tampa": "ಟ್ಯಾಂಪಾ",
                "Orlando": "ಒರ್ಲ್ಯಾಂಡೊ",
                "Richmond": "ರಿಚ್ಮಂಡ್",
                "Baltimore": "ಬಾಲ್ಟಿಮೋರ್",
                "Philadelphia": "ಫಿಲಡೆಲ್ಫಿಯಾ",
                "Buffalo": "ಬಫಲೋ",
                "Cleveland": "ಕ್ಲೀವ್‌ಲ್ಯಾಂಡ್",
                "Columbus": "ಕೊಲಂಬಸ್",
                "Cincinnati": "ಸಿನ್ಸಿನಾಟಿ",
                "Knoxville": "ನಾಕ್ಸ್‌ವಿಲ್",
                "Syracuse": "ಸಿರಾಕ್ಯೂಸ್",
                "Charlotte": "ಶಾರ್ಲಟ್",
                "Raleigh": "ರಾಲಿ",
                "T Narasipura": "ತಿ. ನರಸೀಪುರ",
                "Udipi": "ಉಡುಪಿ",
                "Vadodara": "ವಡೋದರಾ",
                "Stonybrook": "ಸ್ಟೋನಿಬ್ರೂಕ್",
                "Stony Brook": "ಸ್ಟೋನಿ ಬ್ರೂಕ್",
                "Lafayette": "ಲಾಫಯೆಟ್",
                "Georgia": "ಜಾರ್ಜಿಯಾ",
                "Tamil Nadu": "ತಮಿಳುನಾಡು",
                "Kerala": "ಕೇರಳ",
                "West Bengal": "ಪಶ್ಚಿಮ ಬಂಗಾಳ",
                "Odisha": "ಒಡಿಶಾ",
                "Andhra Pradesh": "ಆಂಧ್ರ ಪ್ರದೇಶ",
                "Madhya Pradesh": "ಮಧ್ಯ ಪ್ರದೇಶ",
                "Uttar Pradesh": "ಉತ್ತರ ಪ್ರದೇಶ",
                "Jammu and Kashmir": "ಜಮ್ಮು ಮತ್ತು ಕಾಶ್ಮೀರ",
                "Placentia": "ಪ್ಲಸೆನ್ಷಿಯಾ",
                "Atlanta": "ಅಟ್ಲಾಂಟಾ",
                "Chennai": "ಚೆನ್ನೈ",
                "Thiruvananthapuram": "ತಿರುವನಂತಪುರಂ",
                "Trivandrum": "ತಿರುವನಂತಪುರಂ",
                "Mumbai": "ಮುಂಬೈ",
                "Kolkata": "ಕೊಲ್ಕತ್ತಾ",
                "Bhubaneswar": "ಭುವನೇಶ್ವರ",
                "Bhuvaneshwar": "ಭುವನೇಶ್ವರ",
                "Bhubaneshwar": "ಭುವನೇಶ್ವರ",
                "Tirupati": "ತಿರುಪತಿ",
                "Tirupathi": "ತಿರುಪತಿ",
                "Bhopal": "ಭೋಪಾಲ್",
                "Varanasi": "ವಾರಾಣಸಿ",
                "Prayagraj": "ಪ್ರಯಾಗರಾಜ್",
                "Allahabad": "ಪ್ರಯಾಗರಾಜ್",
                "Ayodhya": "ಅಯೋಧ್ಯೆ",
                "Lucknow": "ಲಖನೌ",
                "Srinagar": "ಶ್ರೀನಗರ",
                "Dubai": "ದುಬೈ",
                "Doha": "ದೋಹಾ",
                "Zurich": "ಜ್ಯೂರಿಕ್",
                "Esslingen": "ಎಸ್ಲಿಂಗನ್",
                "Baladīyat ad Dawḩah": "ಬಲದಿಯತ್ ಅಡ್ ದವ್ಹಾ",
                "Baladiyat ad Dawhah": "ಬಲದಿಯತ್ ಅಡ್ ದವ್ಹಾ",
                "United Arab Emirates": "ಯುನೈಟೆಡ್ ಅರಬ್ ಎಮಿರೇಟ್ಸ್",
                "Qatar": "ಕತಾರ್",
                "Switzerland": "ಸ್ವಿಟ್ಜರ್ಲ್ಯಾಂಡ್",
                "Germany": "ಜರ್ಮನಿ",
                "Agartala": "ಅಗರ್ತಲಾ",
                "Guwahati": "ಗುವಾಹಟಿ",
                "Shiliguri": "ಶಿಲಿಗುರಿ",
                "Siliguri": "ಸಿಲಿಗುರಿ",
                "Aizawl": "ಐಜಾಲ್",
                "Gangtok": "ಗ್ಯಾಂಗ್ಟಾಕ್",
                "Silchar": "ಸಿಲ್ಚರ್",
                "Dibrugarh": "ಡಿಬ್ರುಗಢ",
                "Imphal": "ಇಂಫಾಲ್",
                "Dimapur": "ದಿಮಾಪುರ್",
                "Itanagar": "ಇಟಾನಗರ",
                "Madurai": "ಮದುರೈ",
                "Kumbakonam": "ಕುಂಭಕೋಣಂ",
                "Chidambaram": "ಚಿದಂಬರಂ",
                "Kanchipuram": "ಕಾಂಚೀಪುರಂ",
                "Srirangam": "ಶ್ರೀರಂಗಂ",
                "Tiruchirappalli": "ತಿರುಚಿರಾಪಳ್ಳಿ",
                "Guruvayur": "ಗುರುವಾಯೂರು",
                "Pandharpur": "ಪಂಡರಪುರ",
                "Kolhapur": "ಕೊಲ್ಹಾಪುರ",
                "Nagpur": "ನಾಗಪುರ",
                "Visakhapatnam": "ವಿಶಾಖಪಟ್ಟಣಂ",
                "Ajodhya": "ಅಯೋಧ್ಯೆ",
                "Baroda": "ಬರೋಡಾ",
                "Tripura": "ತ್ರಿಪುರ",
                "Assam": "ಅಸ್ಸಾಂ",
                "Mizoram": "ಮಿಜೋರಾಂ",
                "Sikkim": "ಸಿಕ್ಕಿಂ",
                "Manipur": "ಮಣಿಪುರ",
                "Nagaland": "ನಾಗಾಲ್ಯಾಂಡ್",
                "Arunachal Pradesh": "ಅರುಣಾಚಲ ಪ್ರದೇಶ",
                "Andra Pradesh": "ಆಂಧ್ರ ಪ್ರದೇಶ",
                "Nashville": "ನ್ಯಾಶ್‌ವಿಲ್"
            }};

            const originals = new WeakMap();

            function replaceTerms(text) {{
                let output = text;
                const entries = Object.entries(terms)
                    .sort((a, b) => b[0].length - a[0].length);

                for (const [english, kannada] of entries) {{
                    output = output.split(english).join(kannada);
                }}

                output = output.replace(
                    /(\\d{{1,2}}:\\d{{2}})\\s*AM\\b/g,
                    "ಬೆಳಿಗ್ಗೆ $1"
                );
                output = output.replace(
                    /(\\d{{1,2}}:\\d{{2}})\\s*PM\\b/g,
                    "ಸಂಜೆ $1"
                );

                output = output.replace(/ರವರೆಗೆ/g, "ರ ವರೆಗೆ");

                // Reorder Kannada timing phrases into the natural format:
                // ಬೆಳಿಗ್ಗೆ/ಸಂಜೆ <time>, <date if any> ರ ವರೆಗೆ
                //
                // Examples:
                // ಪಂಚಮಿ ರ ವರೆಗೆ ಬೆಳಿಗ್ಗೆ 06:16
                // -> ಪಂಚಮಿ ಬೆಳಿಗ್ಗೆ 06:16 ರ ವರೆಗೆ
                //
                // ಪೂರ್ವ ಫಲ್ಗುಣಿ ರ ವರೆಗೆ ಬೆಳಿಗ್ಗೆ 12:01, ಜೂನ್ 21
                // -> ಪೂರ್ವ ಫಲ್ಗುಣಿ ಬೆಳಿಗ್ಗೆ 12:01, ಜೂನ್ 21 ರ ವರೆಗೆ
                output = output.replace(
                    /ರ ವರೆಗೆ\\s+(ಬೆಳಿಗ್ಗೆ|ಸಂಜೆ)\\s+(\\d{{1,2}}:\\d{{2}})(\\s*,\\s*[^;|]+)?/g,
                    function (_, dayPart, time, optionalDate) {{
                        const datePart = optionalDate
                            ? optionalDate.replace(/\\s*,\\s*/, ", ")
                            : "";

                        return `${{dayPart}} ${{time}}${{datePart}} ರ ವರೆಗೆ`;
                    }}
                );

                return output;
            }}

            function renderSimpleBold(element, text) {{
                element.textContent = "";
                const parts = String(text).split("**");

                parts.forEach((part, index) => {{
                    if (!part) return;
                    if (index % 2 === 1) {{
                        const strong = document.createElement("strong");
                        strong.textContent = part;
                        element.appendChild(strong);
                    }} else {{
                        element.appendChild(document.createTextNode(part));
                    }}
                }});
            }}

            function translateElement(element, language) {{
                if (!originals.has(element)) {{
                    originals.set(
                        element,
                        element.dataset.original || element.textContent
                    );
                }}

                const original = originals.get(element);
                const translated =
                    language === "kn" ? replaceTerms(original) : original;

                if (element.dataset.simpleBold === "true") {{
                    renderSimpleBold(element, translated);
                }} else {{
                    element.textContent = translated;
                }}
            }}

            function formatDateElement(element, language) {{
                const value = element.dataset.dateValue;
                if (!value) return;

                const parts = value.split("-").map(Number);
                const date = new Date(parts[0], parts[1] - 1, parts[2], 12);

                element.textContent = date.toLocaleDateString(
                    language === "kn" ? "kn-IN" : "en-US",
                    {{
                        weekday: "long",
                        year: "numeric",
                        month: "long",
                        day: "numeric"
                    }}
                );
            }}

            function formatDateNavigation(language) {{
                document.querySelectorAll("#date-navigation .date-item-label")
                    .forEach(element => {{
                        const value = element.dataset.dateValue || "";
                        const match = value.match(
                            /(\\d{{4}})-(\\d{{2}})-(\\d{{2}})\\.html/
                        );
                        if (!match) return;

                        const date = new Date(
                            Number(match[1]),
                            Number(match[2]) - 1,
                            Number(match[3]),
                            12
                        );

                        let label = date.toLocaleDateString(
                            language === "kn" ? "kn-IN" : "en-US",
                            {{
                                weekday: "long",
                                year: "numeric",
                                month: "long",
                                day: "numeric"
                            }}
                        );

                        if (
                            element.classList.contains("unavailable-date")
                        ) {{
                            label += language === "kn"
                                ? " — ಲಭ್ಯವಿಲ್ಲ"
                                : " — Not available";
                        }}

                        element.textContent = label;
                    }});
            }}

            function applyLanguage(language) {{
                document.documentElement.lang =
                    language === "kn" ? "kn" : "en";

                document.querySelectorAll("[data-i18n-fixed]")
                    .forEach(element => {{
                        const key = element.dataset.i18nFixed;
                        element.textContent =
                            fixedTranslations[language][key]
                            || fixedTranslations.en[key]
                            || element.textContent;
                    }});

                document.querySelectorAll(
                    "[data-i18n-label], [data-i18n-value]"
                ).forEach(element => translateElement(element, language));

                document.querySelectorAll("[data-city-source]")
                    .forEach(element => {{
                        const city = element.dataset.citySource;
                        element.textContent =
                            language === "kn"
                                ? `${{replaceTerms(city)}} ಪಂಚಾಂಗವನ್ನು ದೃಕ್ ಪಂಚಾಂಗದಲ್ಲಿ ನೋಡಿ`
                                : `View ${{city}} Panchanga on Drik Panchang`;
                    }});

                formatDateElement(
                    document.getElementById("page-date"),
                    language
                );
                formatDateNavigation(language);

                localStorage.setItem(
                    "panchangaLanguage",
                    language
                );
            }}

            const selector = document.getElementById("language-select");
            const saved =
                localStorage.getItem("panchangaLanguage") || "en";

            selector.value = saved;
            selector.addEventListener(
                "change",
                event => applyLanguage(event.target.value)
            );

            applyLanguage(saved);
        }})();
    </script>
</body>
</html>
"""


def build_index_html(
    available_dates: list[str],
) -> str:
    """
    Creates the website landing page.

    The main button opens today's date based on the visitor's browser date
    instead of the first available generated date.
    """
    cards = []

    for date_value in available_dates:
        cards.append(
            f"""
            <a class="date-card" href="{html_escape(date_value)}.html">
                <span class="date-title">{html_escape(format_date(date_value))}</span>
                <span class="date-arrow">View Panchanga →</span>
            </a>
            """
        )

    fallback_link = (
        f"{html_escape(available_dates[-1])}.html"
        if available_dates
        else "#"
    )

    available_dates_js = ", ".join(
        f'"{html_escape(date_value)}"'
        for date_value in available_dates
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Worldwide Hindu Festival and Vrata Dates">
    <title>Worldwide Hindu Panchanga</title>
    <style>
        :root {{
            --bg: #fffaf0;
            --card: #ffffff;
            --primary: #8c3b12;
            --text: #2b2118;
            --muted: #6b5b4b;
            --border: #ead8c5;
            --shadow: 0 8px 24px rgba(85, 45, 16, 0.08);
        }}

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            font-family: Arial, Helvetica, sans-serif;
            background: var(--bg);
            color: var(--text);
        }}

        .page {{
            width: min(820px, calc(100% - 24px));
            margin: 0 auto;
            padding: 36px 0 56px;
        }}

        .hero {{
            background: linear-gradient(135deg, #fff7e8, #ffe8bf);
            border: 1px solid var(--border);
            border-radius: 20px;
            padding: 28px;
            box-shadow: var(--shadow);
            margin-bottom: 20px;
        }}

        h1 {{
            color: var(--primary);
            margin-top: 0;
        }}

        .latest {{
            display: inline-block;
            margin-top: 10px;
            padding: 11px 16px;
            border-radius: 999px;
            background: var(--primary);
            color: white;
            text-decoration: none;
            font-weight: 700;
        }}

        .date-list {{
            display: grid;
            gap: 12px;
        }}

        .date-card {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            padding: 17px 18px;
            border: 1px solid var(--border);
            border-radius: 14px;
            background: var(--card);
            color: var(--text);
            text-decoration: none;
            box-shadow: var(--shadow);
        }}

        .date-title {{
            font-weight: 700;
        }}

        .date-arrow {{
            color: var(--primary);
            white-space: nowrap;
        }}

        @media (max-width: 560px) {{
            .date-card {{
                align-items: flex-start;
                flex-direction: column;
            }}
        }}
    </style>
</head>
<body>
    <main class="page">
        <section class="hero">
            <h1>Worldwide Hindu Panchanga</h1>
            <p>
                Select a date, then choose the first letter of your city to view the
                local Panchanga and special observance timings.
            </p>
            <a id="today-link" class="latest" href="{fallback_link}">Open today's Panchanga</a>
        </section>

        <section class="date-list">
            {''.join(cards)}
        </section>
    </main>

    <script>
        (function () {{
            const availableDates = [{available_dates_js}];
            const dateSet = new Set(availableDates);
            const today = new Date();
            const yyyy = today.getFullYear();
            const mm = String(today.getMonth() + 1).padStart(2, "0");
            const dd = String(today.getDate()).padStart(2, "0");
            const todayKey = `${{yyyy}}-${{mm}}-${{dd}}`;
            const todayLink = document.getElementById("today-link");

            if (!todayLink) {{
                return;
            }}

            todayLink.href = `${{todayKey}}.html`;

            if (!dateSet.has(todayKey)) {{
                todayLink.title =
                    "The page for today's date has not been generated yet.";
            }}
        }})();
    </script>
</body>
</html>
"""


def collect_existing_html_dates(
    html_output_dir: Path,
) -> list[str]:
    """
    Finds previously generated date pages already present in the website folder.

    Only filenames in YYYY-MM-DD.html format are included.
    index.html and unrelated HTML files are ignored.
    """
    existing_dates: list[str] = []

    if not html_output_dir.exists():
        return existing_dates

    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})\.html$")

    for file_path in html_output_dir.glob("*.html"):
        match = pattern.fullmatch(file_path.name)

        if match:
            existing_dates.append(match.group(1))

    return sorted(set(existing_dates))



def generate_index_from_existing_html() -> list[str]:
    """
    Rebuild ONLY website/index.html from the dated HTML pages already present
    in HTML_OUTPUT_DIR.

    No Drik calls.
    No Panchanga-master reads/writes.
    No daily HTML files are changed.

    This makes the production website folder itself the source of truth for
    historical index navigation. For example, if the folder contains:
        2026-06-16.html ... 2026-08-31.html
    the regenerated index will contain that entire range.
    """
    HTML_OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    available_dates = collect_existing_html_dates(
        HTML_OUTPUT_DIR
    )

    if not available_dates:
        raise RuntimeError(
            "INDEX_ONLY found no dated HTML pages in "
            f"{HTML_OUTPUT_DIR}. Expected files such as YYYY-MM-DD.html."
        )

    index_html = build_index_html(
        available_dates=available_dates,
    )
    index_html = inject_cloudflare_analytics(index_html)

    index_path = HTML_OUTPUT_DIR / "index.html"
    index_path.write_text(
        index_html,
        encoding="utf-8",
    )

    nojekyll_path = HTML_OUTPUT_DIR / ".nojekyll"
    nojekyll_path.write_text(
        "",
        encoding="utf-8",
    )

    print(f"Created HTML index: {index_path}")
    print(f"Index page count   : {len(available_dates)}")
    print(f"First index date   : {available_dates[0]}")
    print(f"Last index date    : {available_dates[-1]}")
    print("Index dates: " + ", ".join(available_dates))

    # Diagnostic only: warn about missing civil dates between the first and
    # last page. Do not fail, because historical gaps may sometimes be
    # intentional.
    first_dt = datetime.strptime(
        available_dates[0],
        "%Y-%m-%d",
    ).date()
    last_dt = datetime.strptime(
        available_dates[-1],
        "%Y-%m-%d",
    ).date()

    expected_dates = []
    cursor = first_dt
    while cursor <= last_dt:
        expected_dates.append(cursor.isoformat())
        cursor += timedelta(days=1)

    missing_dates = sorted(
        set(expected_dates) - set(available_dates)
    )

    if missing_dates:
        print(
            "WARNING: missing dated HTML page(s) inside the index range: "
            + ", ".join(missing_dates)
        )
    else:
        print("Index continuity   : COMPLETE (no missing dates)")

    return available_dates


def generate_daily_html_files(
    results_df: pd.DataFrame,
) -> None:
    """
    Creates and updates:
    website/index.html
    website/YYYY-MM-DD.html

    The index keeps both dates from the current run and older date pages
    already present in the website folder.
    """
    HTML_OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    current_run_dates = sorted(
        results_df["Date"]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )

    # First write the new/current date pages.
    for date_value, df_day in results_df.groupby(
        "Date",
        sort=True,
    ):
        page_html = build_daily_html(
            df_day=df_day,
            date_value=date_value,
            available_dates=current_run_dates,
        )
        page_html = inject_cloudflare_analytics(page_html)

        output_path = HTML_OUTPUT_DIR / f"{date_value}.html"
        output_path.write_text(
            page_html,
            encoding="utf-8",
        )

        print(f"Created HTML: {output_path}")

    # Now include all date pages already present in the folder.
    existing_dates = collect_existing_html_dates(
        HTML_OUTPUT_DIR
    )

    all_available_dates = sorted(
        set(existing_dates).union(current_run_dates)
    )

    # Rewrite current-run pages so their date navigation includes all dates.
    for date_value, df_day in results_df.groupby(
        "Date",
        sort=True,
    ):
        page_html = build_daily_html(
            df_day=df_day,
            date_value=date_value,
            available_dates=all_available_dates,
        )
        page_html = inject_cloudflare_analytics(page_html)

        output_path = HTML_OUTPUT_DIR / f"{date_value}.html"
        output_path.write_text(
            page_html,
            encoding="utf-8",
        )

    index_html = build_index_html(
        available_dates=all_available_dates,
    )
    index_html = inject_cloudflare_analytics(index_html)

    index_path = HTML_OUTPUT_DIR / "index.html"
    index_path.write_text(
        index_html,
        encoding="utf-8",
    )

    nojekyll_path = HTML_OUTPUT_DIR / ".nojekyll"
    nojekyll_path.write_text(
        "",
        encoding="utf-8",
    )

    print(f"Created HTML index: {index_path}")
    print("Index dates: " + ", ".join(all_available_dates))



# ============================================================
# Rolling seven-day HTML rebuild
# ============================================================

def _extract_selected_date_content(
    html_path: Path,
) -> tuple[str, str] | None:
    """
    Reads one existing date page and extracts the content belonging to that
    file's selected date. Works with both older single-date V2 pages and
    rolling V2 pages.
    """
    try:
        soup = BeautifulSoup(
            html_path.read_text(encoding="utf-8"),
            "html.parser",
        )
    except Exception as exc:
        print(f"Could not read HTML source {html_path}: {exc}")
        return None

    date_value = html_path.stem
    active_item = soup.select_one(
        f'.date-entry [data-date-value="{date_value}"]'
    )

    if active_item:
        date_entry = active_item.find_parent(
            class_="date-entry"
        )
        if date_entry:
            content = date_entry.select_one(".date-content")
            if content:
                return date_value, str(content)

    # Fallback for converted pages where the only visible content is outside
    # a date-entry wrapper.
    content = soup.select_one(".date-content")
    if content:
        return date_value, str(content)

    return None


def _prefix_date_panel_ids(
    content_html: str,
    date_value: str,
) -> str:
    """
    Prefixes letter-panel IDs so several dates can safely coexist in one page.
    """
    soup = BeautifulSoup(content_html, "html.parser")
    prefix = f"d-{date_value}-"

    id_map: dict[str, str] = {}

    for element in soup.find_all(id=True):
        old_id = clean_value(element.get("id"))
        if not old_id:
            continue
        new_id = prefix + old_id
        id_map[old_id] = new_id
        element["id"] = new_id

    for element in soup.find_all(attrs={"aria-controls": True}):
        old_value = clean_value(element.get("aria-controls"))
        if old_value in id_map:
            element["aria-controls"] = id_map[old_value]

    for element in soup.find_all(attrs={"data-letter-target": True}):
        old_value = clean_value(element.get("data-letter-target"))
        if old_value in id_map:
            element["data-letter-target"] = id_map[old_value]

    root = soup.select_one(".date-content")
    return str(root) if root else str(soup)


def _rolling_window_dates(
    all_dates: list[str],
    selected_date: str,
    window_size: int = 7,
) -> list[str]:
    """
    Returns the fixed seven-day calendar window centered on the selected date:
    selected date minus three days through selected date plus three days.

    Dates without scanned data remain in the window and are rendered as
    placeholders.
    """
    selected = datetime.strptime(selected_date, "%Y-%m-%d").date()
    return [
        (selected + timedelta(days=offset)).isoformat()
        for offset in range(-3, 4)
    ]



def _build_missing_date_placeholder(
    soup: BeautifulSoup,
    listed_date: str,
):
    """
    Creates the empty future/past date panel used when that date has not yet
    been scanned.
    """
    content = soup.new_tag("div")
    content["class"] = ["date-content"]
    content["hidden"] = ""

    placeholder = soup.new_tag("div")
    placeholder["class"] = ["missing-date-placeholder"]

    heading = soup.new_tag("div")
    heading["class"] = ["missing-date-title"]
    heading["data-i18n-fixed"] = "data_not_available"
    heading.string = "Panchanga data is not available yet."

    note = soup.new_tag("div")
    note["class"] = ["missing-date-note"]
    note.string = (
        "This date will be populated automatically after the scanner runs "
        "for it."
    )

    placeholder.append(heading)
    placeholder.append(note)
    content.append(placeholder)
    return content


def rebuild_rolling_seven_day_pages() -> None:
    """
    Rebuilds every dated page in the website folder so that it contains a
    rolling seven-date accordion. No Drik rescan is performed.

    Each file keeps its own date expanded. Other dates open directly beneath
    their date rows.
    """
    dated_files = sorted(
        path
        for path in HTML_OUTPUT_DIR.glob("*.html")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.html", path.name)
    )

    panel_by_date: dict[str, str] = {}
    shell_by_date: dict[str, BeautifulSoup] = {}

    for path in dated_files:
        extracted = _extract_selected_date_content(path)
        if extracted:
            date_value, panel_html = extracted
            panel_by_date[date_value] = _prefix_date_panel_ids(
                panel_html,
                date_value,
            )

        try:
            shell_by_date[path.stem] = BeautifulSoup(
                path.read_text(encoding="utf-8"),
                "html.parser",
            )
        except Exception as exc:
            print(f"Could not parse HTML shell {path}: {exc}")

    available_dates = sorted(panel_by_date)

    if not available_dates:
        print("No dated HTML panels found for rolling rebuild.")
        return

    rolling_script = """
    (function () {
        function activateFirstLetter(dateEntry) {
            const firstButton = dateEntry.querySelector(
                ".alphabet-button"
            );
            if (firstButton) {
                firstButton.click();
            }
        }

        document.querySelectorAll(
            "#date-navigation .date-toggle-button"
        ).forEach(button => {
            button.addEventListener("click", event => {
                // Handle expandable current/placeholder dates before any older
                // accordion listener attached by the page shell.
                event.preventDefault();
                event.stopImmediatePropagation();

                const entry = button.closest(".date-entry");
                const content = entry.querySelector(
                    ":scope > .date-content"
                );
                if (!content) return;
                const isOpening = content.hidden;

                document.querySelectorAll(
                    "#date-navigation .date-entry"
                ).forEach(otherEntry => {
                    const otherContent = otherEntry.querySelector(
                        ":scope > .date-content"
                    );
                    const otherButton = otherEntry.querySelector(
                        ":scope > .date-toggle-button"
                    );
                    if (otherContent) {
                        otherContent.hidden = true;
                    }
                    if (otherButton) {
                        otherButton.classList.remove("active-date");
                        otherButton.setAttribute(
                            "aria-expanded",
                            "false"
                        );
                        const icon = otherButton.querySelector(
                            ".date-item-icon"
                        );
                        if (icon) icon.textContent = "+";
                    }
                });

                if (isOpening) {
                    content.hidden = false;
                    button.classList.add("active-date");
                    button.setAttribute("aria-expanded", "true");
                    const icon = button.querySelector(
                        ".date-item-icon"
                    );
                    if (icon) icon.textContent = "−";
                    activateFirstLetter(entry);
                }
            }, true);
        });
    })();
    """

    rolling_style = """
    [hidden] {
        display: none !important;
    }
    .date-toggle-button,
    .date-navigation-link {
        width: 100%;
        font-family: inherit;
        text-align: left;
    }
    .date-toggle-button {
        cursor: pointer;
    }
    .date-navigation-link {
        cursor: pointer;
        text-decoration: none;
    }
    .missing-date-placeholder {
        padding: 24px;
        border: 1px dashed #d8b58b;
        border-radius: 14px;
        background: #fffaf2;
        color: #6b5b4b;
    }
    .missing-date-title {
        font-weight: 700;
        color: #963d0d;
        margin-bottom: 6px;
    }
    .missing-date-note {
        font-size: 0.95rem;
    }
    """

    for selected_date in available_dates:
        soup = shell_by_date.get(selected_date)
        if soup is None:
            continue

        navigation = soup.select_one("#date-navigation")
        if navigation is None:
            continue

        navigation.clear()
        displayed_dates = _rolling_window_dates(
            available_dates,
            selected_date,
        )

        for listed_date in displayed_dates:
            is_active = listed_date == selected_date
            has_page = listed_date in panel_by_date

            entry = soup.new_tag("div")
            entry["class"] = ["date-entry"]

            label = soup.new_tag("span")
            label["class"] = ["date-item-label"]
            label["data-date-value"] = listed_date
            label.string = format_date(listed_date)

            icon = soup.new_tag("span")
            icon["class"] = ["date-item-icon"]
            icon["aria-hidden"] = "true"
            icon.string = "−" if is_active else "+"

            if has_page and not is_active:
                # Existing neighboring dates navigate to their own HTML page.
                # That page then shows a newly centered rolling seven-day window.
                date_control = soup.new_tag(
                    "a",
                    href=f"{listed_date}.html",
                )
                date_control["class"] = [
                    "date-item",
                    "date-navigation-link",
                ]
                date_control.append(label)
                date_control.append(icon)
                entry.append(date_control)
            else:
                # The selected date remains expandable. Missing future/past dates
                # also remain expandable so their placeholder can be displayed.
                date_control = soup.new_tag("button")
                date_control["type"] = "button"
                date_control["class"] = [
                    "date-item",
                    "date-toggle-button",
                ]
                if is_active:
                    date_control["class"].append("active-date")
                date_control["aria-expanded"] = (
                    "true" if is_active else "false"
                )
                date_control.append(label)
                date_control.append(icon)
                entry.append(date_control)

            if has_page:
                panel_fragment = BeautifulSoup(
                    panel_by_date[listed_date],
                    "html.parser",
                )
                panel = panel_fragment.select_one(".date-content")
                if panel and is_active:
                    panel.attrs.pop("hidden", None)
                    entry.append(panel)
            else:
                placeholder_panel = _build_missing_date_placeholder(
                    soup,
                    listed_date,
                )
                if is_active:
                    placeholder_panel.attrs.pop("hidden", None)
                entry.append(placeholder_panel)

            navigation.append(entry)

        style_tag = soup.new_tag("style")
        style_tag.string = rolling_style
        if soup.head:
            soup.head.append(style_tag)

        script_tag = soup.new_tag("script")
        script_tag.string = rolling_script
        if soup.body:
            soup.body.append(script_tag)

        output_path = HTML_OUTPUT_DIR / f"{selected_date}.html"
        output_path.write_text(
            str(soup),
            encoding="utf-8",
        )

    # Keep index generation unchanged; it remains the date-selection landing page.
    print(
        "Rebuilt rolling seven-day HTML pages for: "
        + ", ".join(available_dates)
    )


# ============================================================
# Persistent normal-Panchanga master
# ============================================================

PANCHANGA_MASTER_COLUMNS = [
    "Date",
    "Location Key",
    "Input Order",
    "City",
    "State/Region",
    "Country",
    "Timezone",
    "Geoname ID",
    "Selected Drik Location",
    "Samvatsara",
    "Ayana",
    "Ritu",
    "Amanta Maasa",
    "Paksha",
    "Tithi",
    "Vaara",
    "Nakshatra",
    "Yoga",
    "Karana",
    "Sooryodaya",
    "Month Page URL",
    "Day Page URL",
    "Stored At",
    "Source Run Mode",
]

PANCHANGA_CORE_FIELDS = [
    "Samvatsara",
    "Ayana",
    "Ritu",
    "Amanta Maasa",
    "Paksha",
    "Tithi",
    "Vaara",
    "Nakshatra",
    "Yoga",
    "Karana",
    "Sooryodaya",
]


def _panchanga_master_path(year: str) -> Path:
    return PANCHANGA_MASTER_DIR / PANCHANGA_MASTER_TEMPLATE.format(year=year)


def _read_panchanga_master(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=PANCHANGA_MASTER_COLUMNS)
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    df.columns = df.columns.str.strip()
    for column in PANCHANGA_MASTER_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    return df[PANCHANGA_MASTER_COLUMNS].copy()


def _atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)


def _backup_panchanga_master(path: Path) -> Path | None:
    if not path.exists():
        return None
    PANCHANGA_MASTER_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = PANCHANGA_MASTER_BACKUP_DIR / f"{path.stem}_{stamp}{path.suffix}"
    shutil.copy2(path, backup)
    return backup


def _normal_panchanga_rows_for_master(scan_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (safe_rows, rejected_rows).

    Special-event columns are intentionally excluded. A failed or incomplete
    scan row is rejected so it cannot destroy a previously good master row.
    """
    if scan_df.empty:
        return pd.DataFrame(columns=PANCHANGA_MASTER_COLUMNS), scan_df.copy()

    df = scan_df.copy().fillna("")
    if "Location Key" not in df.columns:
        df["Location Key"] = df.apply(
            lambda row: get_master_location_key_from_record(row), axis=1
        )

    error_mask = df.get("Error", pd.Series("", index=df.index)).astype(str).str.strip().ne("")
    core_missing_mask = pd.Series(False, index=df.index)
    if REQUIRE_CORE_FIELDS_BEFORE_MASTER_WRITE:
        for column in PANCHANGA_CORE_FIELDS:
            if column not in df.columns:
                core_missing_mask |= True
            else:
                core_missing_mask |= df[column].map(clean_value).eq("")

    rejected_mask = error_mask | core_missing_mask
    rejected = df.loc[rejected_mask].copy()
    safe = df.loc[~rejected_mask].copy()

    now_text = datetime.now().astimezone().isoformat(timespec="seconds")
    safe["Stored At"] = now_text
    safe["Source Run Mode"] = RUN_MODE

    for column in PANCHANGA_MASTER_COLUMNS:
        if column not in safe.columns:
            safe[column] = ""

    safe = safe[PANCHANGA_MASTER_COLUMNS].copy()
    return safe, rejected


def _sort_panchanga_master(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    result = df.copy()
    result["_input_order_num"] = pd.to_numeric(result["Input Order"], errors="coerce").fillna(999999)
    result = result.sort_values(
        ["Date", "_input_order_num", "City", "Location Key"], kind="stable"
    ).drop(columns=["_input_order_num"])
    return result.reset_index(drop=True)


def upsert_panchanga_master(scan_df: pd.DataFrame) -> dict[str, int]:
    """UPSERT successful normal Panchanga rows by Date + Location Key.

    Only the exact city/date rows present in the current scan are replaced.
    Every other historical row remains untouched.
    """
    safe, rejected = _normal_panchanga_rows_for_master(scan_df)
    if not rejected.empty:
        print(
            f"Panchanga master safety gate rejected {len(rejected)} scan row(s); "
            "existing good master rows, if any, were preserved."
        )
        for _, row in rejected.head(20).iterrows():
            missing_fields = [
                c for c in PANCHANGA_CORE_FIELDS if not clean_value(row.get(c, ""))
            ]
            reason = clean_value(row.get("Error", "")) or (
                "Missing core fields: " + ", ".join(missing_fields)
            )
            print(f"  - {row.get('Date','')} | {row.get('City','')}: {reason}")

    if safe.empty:
        return {"written": 0, "rejected": len(rejected), "years": 0}

    safe["Date"] = safe["Date"].astype(str)
    safe["_Year"] = safe["Date"].str[:4]
    total_written = 0
    years_written = 0

    PANCHANGA_MASTER_DIR.mkdir(parents=True, exist_ok=True)

    for year, incoming in safe.groupby("_Year", sort=True):
        incoming = incoming.drop(columns=["_Year"]).copy()
        path = _panchanga_master_path(str(year))
        existing = _read_panchanga_master(path)

        incoming_keys = set(
            zip(incoming["Date"].astype(str), incoming["Location Key"].astype(str))
        )
        if existing.empty:
            preserved = existing
            replaced_count = 0
        else:
            existing_keys = list(
                zip(existing["Date"].astype(str), existing["Location Key"].astype(str))
            )
            replace_mask = pd.Series(
                [key in incoming_keys for key in existing_keys], index=existing.index
            )
            replaced_count = int(replace_mask.sum())
            preserved = existing.loc[~replace_mask].copy()

        merged = pd.concat([preserved, incoming], ignore_index=True, sort=False)
        dupes = merged.duplicated(["Date", "Location Key"], keep=False)
        if dupes.any():
            sample = merged.loc[dupes, ["Date", "Location Key", "City"]].head(20)
            raise ValueError(
                "Duplicate Date + Location Key rows remain in Panchanga master.\n"
                + sample.to_string(index=False)
            )

        merged = _sort_panchanga_master(merged[PANCHANGA_MASTER_COLUMNS])
        backup = _backup_panchanga_master(path)
        if backup:
            print(f"Panchanga master backup: {backup}")
        _atomic_write_csv(merged, path)
        years_written += 1
        total_written += len(incoming)
        print(
            f"Panchanga master {year}: replaced {replaced_count} row(s), "
            f"inserted {len(incoming)} fresh row(s), total {len(merged)}."
        )

    return {"written": total_written, "rejected": len(rejected), "years": years_written}


def load_panchanga_master_for_dates(output_dates: list[str]) -> pd.DataFrame:
    years = sorted({str(d)[:4] for d in output_dates if re.match(r"^\d{4}-\d{2}-\d{2}$", str(d))})
    frames: list[pd.DataFrame] = []
    for year in years:
        path = _panchanga_master_path(year)
        if not path.exists():
            print(f"Panchanga master not found for {year}: {path}")
            continue
        df = _read_panchanga_master(path)
        frames.append(df[df["Date"].astype(str).isin(output_dates)].copy())
    if not frames:
        return pd.DataFrame(columns=PANCHANGA_MASTER_COLUMNS)
    result = pd.concat(frames, ignore_index=True, sort=False)
    return _sort_panchanga_master(result)


def master_rows_to_render_df(master_df: pd.DataFrame) -> pd.DataFrame:
    """Convert stored base rows back to the render schema.

    Event fields start blank and are injected only from special_events_master.
    """
    if master_df.empty:
        return master_df.copy()
    df = master_df.copy()
    df["Special Events"] = ""
    df["Special Event Details"] = ""
    df["Note"] = ""
    df["Message"] = ""
    df["Master Event Count"] = 0
    df["Error"] = ""
    return df


# ============================================================
# One-time legacy bootstrap from existing daily text files
# ============================================================

def _normalize_location_match_piece(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_value(value).lower()).strip()


def _city_config_lookup(cities: pd.DataFrame) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for input_order, (_, row) in enumerate(cities.iterrows(), start=1):
        items.append({
            "input_order": input_order,
            "row": row,
            "city": _normalize_location_match_piece(row.get("display_city", "")),
            "state": _normalize_location_match_piece(row.get("state_or_region", "")),
            "country": _normalize_location_match_piece(row.get("country", "")),
        })
    return items


def _match_bootstrap_city(location_line: str, lookup: list[dict[str, Any]]) -> dict[str, Any] | None:
    parts = [clean_value(x) for x in location_line.split(",") if clean_value(x)]
    if not parts:
        return None
    city = _normalize_location_match_piece(parts[0])
    state = _normalize_location_match_piece(parts[1]) if len(parts) >= 3 else ""
    country = _normalize_location_match_piece(parts[-1]) if len(parts) >= 2 else ""

    candidates = [x for x in lookup if x["city"] == city]
    if state:
        narrowed = [x for x in candidates if x["state"] == state]
        if narrowed:
            candidates = narrowed
    if country:
        narrowed = [x for x in candidates if x["country"] in {country, "united states" if country == "usa" else country}]
        if narrowed:
            candidates = narrowed
    return candidates[0] if len(candidates) == 1 else None


def _parse_legacy_daily_text_file(path: Path, city_lookup: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})_", path.name)
    if not match:
        return [], []
    date_str = match.group(1)
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()

    global_values = {"Samvatsara": "", "Ayana": ""}
    for line in lines[:20]:
        if line.startswith("Samvatsara:"):
            global_values["Samvatsara"] = clean_value(line.split(":", 1)[1])
        elif line.startswith("Ayana:"):
            global_values["Ayana"] = clean_value(line.split(":", 1)[1])

    records: list[dict[str, Any]] = []
    legacy_events: list[dict[str, Any]] = []
    i = 0
    numbered = re.compile(r"^\d+\)\s+(.+)$")
    field_map = {
        "Ritu": "Ritu",
        "Amanta Maasa": "Amanta Maasa",
        "Paksha": "Paksha",
        "Tithi": "Tithi",
        "Vaara": "Vaara",
        "Nakshatra": "Nakshatra",
        "Yoga": "Yoga",
        "Karana": "Karana",
        "Sooryodaya": "Sooryodaya",
    }

    while i < len(lines):
        m = numbered.match(lines[i].strip())
        if not m:
            i += 1
            continue
        location_line = m.group(1).strip()
        matched = _match_bootstrap_city(location_line, city_lookup)
        if matched is None:
            # A numbered Special Event line can look like a city row. It is not a
            # city boundary unless it maps to a configured city.
            i += 1
            continue

        block: dict[str, str] = {}
        event_values: list[str] = []
        event_details = ""
        note = ""
        capturing_numbered_events = False
        i += 1
        while i < len(lines):
            current = lines[i].strip()
            next_numbered = numbered.match(current)
            if next_numbered and _match_bootstrap_city(next_numbered.group(1).strip(), city_lookup) is not None:
                break

            for prefix, output_col in field_map.items():
                token = prefix + ":"
                if current.startswith(token):
                    block[output_col] = clean_value(current.split(":", 1)[1])
                    break

            if current.startswith("Special event:"):
                value = clean_value(current.split(":", 1)[1])
                value = re.sub(r"^\d+\)\s*", "", value)
                if value:
                    event_values.append(value)
                capturing_numbered_events = True
            elif capturing_numbered_events and next_numbered:
                value = clean_value(next_numbered.group(1))
                if value:
                    event_values.append(value)
            elif current.startswith("Special timing/details:"):
                event_details = clean_value(current.split(":", 1)[1])
                capturing_numbered_events = False
            elif current.startswith("Note:") and "all dates and timings are local" not in current.lower():
                note = clean_value(current.split(":", 1)[1])
                capturing_numbered_events = False
            elif current and not next_numbered:
                capturing_numbered_events = False

            if " Panchanga:" in current:
                block["Month Page URL"] = clean_value(current.split("Panchanga:", 1)[1])
            i += 1

        row = matched["row"]
        record = {
            "Input Order": matched["input_order"],
            "Date": date_str,
            "City": clean_value(row.get("display_city", "")),
            "State/Region": clean_value(row.get("state_or_region", "")),
            "Country": clean_value(row.get("country", "")),
            "Timezone": clean_value(row.get("timezone", "")),
            "Geoname ID": get_geoname_id(row),
            "Selected Drik Location": clean_value(row.get("search_city", "")),
            "Samvatsara": global_values["Samvatsara"],
            "Ayana": global_values["Ayana"],
            "Ritu": block.get("Ritu", ""),
            "Amanta Maasa": block.get("Amanta Maasa", ""),
            "Paksha": block.get("Paksha", ""),
            "Tithi": block.get("Tithi", ""),
            "Vaara": block.get("Vaara", ""),
            "Nakshatra": block.get("Nakshatra", ""),
            "Yoga": block.get("Yoga", ""),
            "Karana": block.get("Karana", ""),
            "Sooryodaya": block.get("Sooryodaya", ""),
            "Month Page URL": block.get("Month Page URL", ""),
            "Day Page URL": "",
            "Error": "",
        }
        record["Location Key"] = get_master_location_key_from_record(record)
        records.append(record)

        if event_values or event_details or note:
            legacy_events.append({
                "Date": date_str,
                "Location Key": record["Location Key"],
                "City": record["City"],
                "State/Region": record["State/Region"],
                "Country": record["Country"],
                "Timezone": record["Timezone"],
                "Geoname ID": record["Geoname ID"],
                "Special Events": "; ".join(dict.fromkeys(event_values)),
                "Special Event Details": event_details,
                "Note": note,
                "Snapshot Source": path.name,
            })
    return records, legacy_events


def bootstrap_panchanga_master_from_daily_text(cities: pd.DataFrame) -> None:
    files = sorted(DAILY_TEXT_DIR.glob("*.txt"))
    if not files:
        raise FileNotFoundError(f"No daily text files found under {DAILY_TEXT_DIR}")

    lookup = _city_config_lookup(cities)
    all_rows: list[dict[str, Any]] = []
    legacy_rows: list[dict[str, Any]] = []
    for path in files:
        parsed_rows, parsed_legacy = _parse_legacy_daily_text_file(path, lookup)
        all_rows.extend(parsed_rows)
        legacy_rows.extend(parsed_legacy)

    if not all_rows:
        raise RuntimeError("Bootstrap found no usable city/date rows in daily text files.")

    df = pd.DataFrame(all_rows).drop_duplicates(["Date", "Location Key"], keep="last")
    safe, rejected = _normal_panchanga_rows_for_master(df)
    print(
        f"Bootstrap parsed {len(df)} unique city/date rows; "
        f"{len(safe)} passed the core-field gate and {len(rejected)} were rejected."
    )

    if safe.empty:
        raise RuntimeError("No bootstrap rows passed the Panchanga core-field safety gate.")

    safe["Stored At"] = datetime.now().astimezone().isoformat(timespec="seconds")
    safe["Source Run Mode"] = "BOOTSTRAP_DAILY_TEXT"
    safe["_Year"] = safe["Date"].astype(str).str[:4]

    for year, incoming in safe.groupby("_Year", sort=True):
        incoming = incoming.drop(columns=["_Year"]).copy()
        path = _panchanga_master_path(str(year))
        existing = _read_panchanga_master(path)
        if existing.empty:
            merged = incoming.copy()
            preserved_existing = 0
        else:
            existing_keys = set(zip(existing["Date"], existing["Location Key"]))
            if BOOTSTRAP_OVERWRITE_EXISTING:
                incoming_keys = set(zip(incoming["Date"], incoming["Location Key"]))
                mask = [key not in incoming_keys for key in zip(existing["Date"], existing["Location Key"])]
                preserved = existing.loc[mask].copy()
                merged = pd.concat([preserved, incoming], ignore_index=True, sort=False)
                preserved_existing = len(preserved)
            else:
                new_only_mask = [key not in existing_keys for key in zip(incoming["Date"], incoming["Location Key"])]
                new_only = incoming.loc[new_only_mask].copy()
                merged = pd.concat([existing, new_only], ignore_index=True, sort=False)
                preserved_existing = len(existing)
                incoming = new_only

        merged = _sort_panchanga_master(merged[PANCHANGA_MASTER_COLUMNS])
        backup = _backup_panchanga_master(path)
        if backup:
            print(f"Bootstrap backup: {backup}")
        _atomic_write_csv(merged, path)
        print(
            f"Bootstrap {year}: preserved {preserved_existing} existing row(s), "
            f"added/replaced {len(incoming)} row(s), total {len(merged)} -> {path}"
        )

    _write_legacy_special_event_snapshots(legacy_rows)

    print(
        "\nBOOTSTRAP COMPLETE. Existing HTML was intentionally NOT rebuilt. "
        "Legacy event text was saved only as a migration fallback; approved "
        "special-events master rows override it whenever available."
    )


# ============================================================
# Legacy published-event snapshot (migration fallback only)
# ============================================================

LEGACY_SPECIAL_EVENT_COLUMNS = [
    "Date", "Location Key", "City", "State/Region", "Country", "Timezone",
    "Geoname ID", "Special Events", "Special Event Details", "Note",
    "Snapshot Source",
]


def _legacy_special_events_path(year: str) -> Path:
    return Path("special_events_master") / LEGACY_SPECIAL_EVENTS_SUBDIR / LEGACY_SPECIAL_EVENTS_TEMPLATE.format(year=year)


def _write_legacy_special_event_snapshots(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("Bootstrap: no legacy special-event rows were found in daily text files.")
        return
    df = pd.DataFrame(rows).fillna("")
    df = df[
        df["Special Events"].map(clean_value).ne("")
        | df["Special Event Details"].map(clean_value).ne("")
        | df["Note"].map(clean_value).ne("")
    ].copy()
    if df.empty:
        print("Bootstrap: no non-empty legacy special-event rows were found.")
        return
    df = df.drop_duplicates(["Date", "Location Key"], keep="last")
    df["_Year"] = df["Date"].astype(str).str[:4]
    for year, incoming in df.groupby("_Year", sort=True):
        incoming = incoming.drop(columns=["_Year"]).copy()
        for c in LEGACY_SPECIAL_EVENT_COLUMNS:
            if c not in incoming.columns:
                incoming[c] = ""
        incoming = incoming[LEGACY_SPECIAL_EVENT_COLUMNS]
        path = _legacy_special_events_path(str(year))
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
            for c in LEGACY_SPECIAL_EVENT_COLUMNS:
                if c not in existing.columns:
                    existing[c] = ""
            incoming_keys = set(zip(incoming["Date"], incoming["Location Key"]))
            keep_mask = [key not in incoming_keys for key in zip(existing["Date"], existing["Location Key"])]
            merged = pd.concat([existing.loc[keep_mask, LEGACY_SPECIAL_EVENT_COLUMNS], incoming], ignore_index=True)
        else:
            merged = incoming
        merged = merged.sort_values(["Date", "Location Key"], kind="stable")
        _atomic_write_csv(merged, path)
        print(f"Legacy event snapshot {year}: {len(merged)} row(s) -> {path}")


def load_legacy_special_events_snapshot(output_dates: list[str]) -> pd.DataFrame:
    years = sorted({str(d)[:4] for d in output_dates if re.match(r"^\d{4}-\d{2}-\d{2}$", str(d))})
    frames: list[pd.DataFrame] = []
    for year in years:
        path = _legacy_special_events_path(year)
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
        frames.append(df[df["Date"].astype(str).isin(output_dates)].copy())
    if not frames:
        return pd.DataFrame(columns=LEGACY_SPECIAL_EVENT_COLUMNS)
    return pd.concat(frames, ignore_index=True, sort=False)


def apply_legacy_special_events_snapshot(results_df: pd.DataFrame, legacy_df: pd.DataFrame) -> pd.DataFrame:
    """Apply old published event text only as a fallback.

    The approved special-events master is applied AFTER this function and
    therefore supersedes these values whenever a validated modern event exists.
    """
    if results_df.empty or legacy_df.empty:
        return results_df
    df = results_df.copy()
    lookup = {
        (clean_value(r.get("Date", "")), clean_value(r.get("Location Key", ""))): r
        for _, r in legacy_df.iterrows()
    }
    matched = 0
    for idx, row in df.iterrows():
        key = (clean_value(row.get("Date", "")), clean_value(row.get("Location Key", "")))
        old = lookup.get(key)
        if old is None:
            continue
        matched += 1
        df.at[idx, "Special Events"] = clean_value(old.get("Special Events", ""))
        df.at[idx, "Special Event Details"] = clean_value(old.get("Special Event Details", ""))
        df.at[idx, "Note"] = clean_value(old.get("Note", ""))
    if matched:
        print(f"Applied legacy published-event fallback to {matched} stored row(s).")
    return df


# ============================================================
# Approved special-events master lookup
# ============================================================

SPECIAL_EVENTS_MASTER_DIR = Path("special_events_master")
SPECIAL_EVENTS_MASTER_TEMPLATE = "special_events_master_{year}.csv"

MASTER_REQUIRED_COLUMNS = {
    "Date",
    "Location Key",
    "Special Events",
    "Special Event Details",
    "Note",
    "Message",
}


def _master_slugify(value: Any) -> str:
    value = clean_value(value).lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value or "location"


def get_master_place_key_from_values(
    geoname_id: str,
    city: str,
    state: str,
    country: str,
    timezone: str,
) -> str:
    geoname_id = clean_value(geoname_id)
    if geoname_id:
        return f"geoname:{geoname_id}"

    raw = "|".join([
        clean_value(city).lower(),
        clean_value(state).lower(),
        clean_value(country).lower(),
        normalize_timezone_name(clean_value(timezone)),
    ])
    return re.sub(r"[^a-z0-9:+|_/-]+", "_", raw)


def get_master_location_key_from_record(record: dict[str, Any] | pd.Series) -> str:
    place_key = get_master_place_key_from_values(
        clean_value(record.get("Geoname ID", "")),
        clean_value(record.get("City", "")),
        clean_value(record.get("State/Region", "")),
        clean_value(record.get("Country", "")),
        clean_value(record.get("Timezone", "")),
    )
    return f"{place_key}|city:{_master_slugify(record.get('City', ''))}"


def load_special_events_master(output_dates: list[str]) -> pd.DataFrame:
    """Load only the year-specific approved master file(s) needed by this run."""
    years = sorted({str(d)[:4] for d in output_dates if re.match(r"^\d{4}-\d{2}-\d{2}$", str(d))})
    frames: list[pd.DataFrame] = []

    for year in years:
        path = SPECIAL_EVENTS_MASTER_DIR / SPECIAL_EVENTS_MASTER_TEMPLATE.format(year=year)
        if not path.exists():
            print(f"Special-events master not found for {year}: {path}. Continuing with no master events for that year.")
            continue

        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
        df.columns = df.columns.str.strip()
        missing = MASTER_REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise ValueError(
                f"Special-events master {path} is missing required columns: {sorted(missing)}"
            )

        if "Approval Status" in df.columns:
            df = df[
                df["Approval Status"].astype(str).str.strip().str.upper().eq("APPROVED")
            ].copy()

        if "Display Priority" not in df.columns:
            df["Display Priority"] = "50"
        df["_Display Priority Numeric"] = pd.to_numeric(
            df["Display Priority"], errors="coerce"
        ).fillna(50)
        df["_Master Source File"] = str(path)
        frames.append(df)
        print(f"Loaded approved special-events master: {path} ({len(df)} rows)")

    if not frames:
        return pd.DataFrame()

    master = pd.concat(frames, ignore_index=True, sort=False)
    master = master[
        master["Date"].astype(str).isin(output_dates)
    ].copy()

    print(f"Master rows relevant to requested date(s): {len(master)}")
    return master


def _join_unique(values: pd.Series, separator: str) -> str:
    result: list[str] = []
    for value in values:
        cleaned = clean_value(value)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return separator.join(result)


def apply_special_events_master(
    results_df: pd.DataFrame,
    master_df: pd.DataFrame,
) -> pd.DataFrame:
    """Inject approved master events into normal Panchanga rows.

    Matching is strictly Date + Location Key. Multiple approved events for the
    same city/date are kept and combined in Display Priority order.
    """
    if results_df.empty:
        return results_df

    df = results_df.copy()
    df["Location Key"] = df.apply(
        lambda row: get_master_location_key_from_record(row), axis=1
    )

    for column, default in [
        ("Special Events", ""),
        ("Special Event Details", ""),
        ("Note", ""),
        ("Message", ""),
        ("Master Event Count", 0),
    ]:
        if column not in df.columns:
            df[column] = default

    if master_df.empty:
        print("No approved master events matched this run's year/date range.")
        return df

    master = master_df.copy()
    sort_columns = ["Date", "Location Key", "_Display Priority Numeric"]
    for optional in ["Event Family", "Event Name", "Action Role"]:
        if optional in master.columns:
            sort_columns.append(optional)
    master = master.sort_values(sort_columns, kind="stable")

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for (event_date, location_key), group in master.groupby(
        ["Date", "Location Key"], sort=False, dropna=False
    ):
        grouped[(str(event_date), str(location_key))] = {
            "Special Events": _join_unique(group["Special Events"], "; "),
            "Special Event Details": _join_unique(group["Special Event Details"], " || "),
            "Note": _join_unique(group["Note"], " | "),
            "Message": _join_unique(group["Message"], " || "),
            "Master Event Count": int(len(group)),
        }

    matched_rows = 0
    matched_event_rows = 0
    for idx, row in df.iterrows():
        key = (clean_value(row.get("Date", "")), clean_value(row.get("Location Key", "")))
        payload = grouped.get(key)
        if payload is None:
            continue
        matched_rows += 1
        matched_event_rows += int(payload["Master Event Count"])
        for column, value in payload.items():
            df.at[idx, column] = value

    print(
        f"Master lookup matched {matched_rows} Panchanga city/date rows "
        f"containing {matched_event_rows} approved event rows."
    )
    return df


# ============================================================
# Error handling
# ============================================================


def create_error_records(
    row: pd.Series,
    scan_dates: list[str],
    input_order: int,
    error_message: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    geoname_id = get_geoname_id(row)

    for date_str in scan_dates:
        records.append(
            {
                "Input Order": input_order,
                "Date": date_str,
                "City": row.get("display_city", ""),
                "State/Region": row.get("state_or_region", ""),
                "Country": row.get("country", ""),
                "Timezone": row.get("timezone", ""),
                "Geoname ID": geoname_id,
                "Selected Drik Location": "",
                "Samvatsara": "",
                "Ayana": "",
                "Ritu": "",
                "Amanta Maasa": "",
                "Paksha": "",
                "Tithi": "",
                "Vaara": "",
                "Nakshatra": "",
                "Yoga": "",
                "Karana": "",
                "Sooryodaya": "",
                "Special Events": "",
                "Special Event Details": "",
                "Note": "",
                "Message": "",
                "Master Event Count": 0,
                "Month Page URL": "",
                "Day Page URL": "",
                "Error": error_message,
            }
        )

    return records


# ============================================================




# ============================================================
# Command-line interface
# ============================================================


def _add_date_range_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--date",
        help="Scan/rebuild one date (YYYY-MM-DD). Cannot be combined with --start/--end/--days.",
    )
    parser.add_argument(
        "--start",
        help="First date (YYYY-MM-DD). For DAILY, omitted dates default to today.",
    )
    end_group = parser.add_mutually_exclusive_group()
    end_group.add_argument(
        "--end",
        help="Inclusive last date (YYYY-MM-DD).",
    )
    end_group.add_argument(
        "--days",
        type=int,
        help="Number of consecutive days beginning with --start.",
    )


def _add_common_path_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input-csv",
        type=Path,
        default=Path(INPUT_CSV),
        help=f"City configuration CSV (default: {INPUT_CSV}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help=f"Scanner output directory (default: {OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--panchanga-master-dir",
        type=Path,
        default=PANCHANGA_MASTER_DIR,
        help=f"Persistent Panchanga master directory (default: {PANCHANGA_MASTER_DIR}).",
    )
    parser.add_argument(
        "--special-events-master-dir",
        type=Path,
        default=SPECIAL_EVENTS_MASTER_DIR,
        help=f"Approved special-events master directory (default: {SPECIAL_EVENTS_MASTER_DIR}).",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=PLAYWRIGHT_PROFILE_DIR,
        help=f"Playwright persistent browser profile (default: {PLAYWRIGHT_PROFILE_DIR}).",
    )
    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium headless. Not recommended when CAPTCHA interaction may be needed.",
    )
    browser_group.add_argument(
        "--headed",
        action="store_true",
        help="Force visible Chromium (default behavior).",
    )


def _add_city_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cities",
        nargs="+",
        metavar="CITY",
        help="Exact display_city names to scan, e.g. --cities Buffalo 'T Narasipura'.",
    )
    parser.add_argument(
        "--city-limit",
        type=int,
        help="Scan only the first N configured cities (primarily for testing).",
    )


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="panchanga_daily_scanner.py",
        description=(
            "Fetch normal Panchanga, safely UPSERT the persistent yearly Panchanga master, "
            "apply approved special events, and regenerate complete HTML/text pages."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    daily = sub.add_parser(
        "daily",
        help="Normal production scan. Defaults to today and all configured cities.",
    )
    _add_date_range_arguments(daily)
    _add_city_selection_arguments(daily)
    _add_common_path_arguments(daily)

    backfill = sub.add_parser(
        "backfill",
        help="Safely scan new/repaired cities for historical dates and merge them into the master.",
    )
    _add_date_range_arguments(backfill)
    _add_city_selection_arguments(backfill)
    backfill.add_argument(
        "--all-cities",
        action="store_true",
        help="Deliberately allow a historical all-city scan. Without this, --cities is required.",
    )
    _add_common_path_arguments(backfill)

    rebuild = sub.add_parser(
        "rebuild",
        help="Make zero Drik calls; rebuild HTML/text from Panchanga + special-events masters.",
    )
    _add_date_range_arguments(rebuild)
    _add_common_path_arguments(rebuild)

    index = sub.add_parser(
        "index",
        help=(
            "Make zero Drik calls; rebuild website/index.html from all "
            "existing YYYY-MM-DD.html pages already in the website folder."
        ),
    )
    _add_common_path_arguments(index)

    bootstrap = sub.add_parser(
        "bootstrap",
        help="One-time migration from existing daily_text_files into the persistent Panchanga master.",
    )
    bootstrap.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow bootstrap rows to replace existing master rows. Default is preserve-existing.",
    )
    _add_common_path_arguments(bootstrap)

    return parser.parse_args()


def _resolve_cli_date_range(args: argparse.Namespace, *, allow_default_today: bool) -> tuple[str, int]:
    date_value = clean_value(getattr(args, "date", ""))
    start_value = clean_value(getattr(args, "start", ""))
    end_value = clean_value(getattr(args, "end", ""))
    days_value = getattr(args, "days", None)

    if date_value:
        if start_value or end_value or days_value is not None:
            raise ValueError("--date cannot be combined with --start, --end, or --days.")
        datetime.strptime(date_value, "%Y-%m-%d")
        return date_value, 1

    if not start_value:
        if allow_default_today:
            start_value = datetime.now().astimezone().date().isoformat()
        else:
            raise ValueError("This command requires --date or --start.")

    start_dt = datetime.strptime(start_value, "%Y-%m-%d")

    if end_value:
        end_dt = datetime.strptime(end_value, "%Y-%m-%d")
        if end_dt < start_dt:
            raise ValueError("--end cannot be earlier than --start.")
        return start_value, (end_dt - start_dt).days + 1

    if days_value is not None:
        if days_value < 1:
            raise ValueError("--days must be at least 1.")
        return start_value, int(days_value)

    return start_value, 1


def apply_cli_args(args: argparse.Namespace) -> None:
    """Translate operational CLI arguments into the existing scanner settings.

    All religious/Panchanga logic remains unchanged. This function only sets
    run mode, date/city selection, and filesystem/runtime options.
    """
    global RUN_MODE, SCAN_START_DATE, SCAN_NUM_DAYS
    global TEST_CITY_NAMES, TEST_CITY_LIMIT, ALLOW_ALL_CITIES_IN_BACKFILL
    global INPUT_CSV, OUTPUT_DIR, INTERMEDIATE_CSV, DAILY_TEXT_DIR, HTML_OUTPUT_DIR, LAST_SCAN_CSV
    global PANCHANGA_MASTER_DIR, PANCHANGA_MASTER_BACKUP_DIR
    global SPECIAL_EVENTS_MASTER_DIR, LEGACY_SPECIAL_EVENTS_DIR
    global PLAYWRIGHT_PROFILE_DIR, HEADLESS, BOOTSTRAP_OVERWRITE_EXISTING

    command_to_mode = {
        "daily": "DAILY",
        "backfill": "BACKFILL",
        "rebuild": "REBUILD_ONLY",
        "index": "INDEX_ONLY",
        "bootstrap": "BOOTSTRAP_ONLY",
    }
    RUN_MODE = command_to_mode[args.command]

    INPUT_CSV = str(args.input_csv)
    OUTPUT_DIR = Path(args.output_dir)
    INTERMEDIATE_CSV = OUTPUT_DIR / "weekly_panchanga_results.csv"
    DAILY_TEXT_DIR = OUTPUT_DIR / "daily_text_files"
    HTML_OUTPUT_DIR = OUTPUT_DIR / "website"
    LAST_SCAN_CSV = OUTPUT_DIR / "last_scan_results.csv"

    PANCHANGA_MASTER_DIR = Path(args.panchanga_master_dir)
    PANCHANGA_MASTER_BACKUP_DIR = PANCHANGA_MASTER_DIR / "backups"

    SPECIAL_EVENTS_MASTER_DIR = Path(args.special_events_master_dir)
    LEGACY_SPECIAL_EVENTS_DIR = SPECIAL_EVENTS_MASTER_DIR

    PLAYWRIGHT_PROFILE_DIR = Path(args.profile_dir)
    if getattr(args, "headless", False):
        HEADLESS = True
    elif getattr(args, "headed", False):
        HEADLESS = False

    TEST_CITY_NAMES = list(getattr(args, "cities", None) or [])
    TEST_CITY_LIMIT = getattr(args, "city_limit", None)
    ALLOW_ALL_CITIES_IN_BACKFILL = bool(getattr(args, "all_cities", False))
    BOOTSTRAP_OVERWRITE_EXISTING = bool(getattr(args, "overwrite", False))

    if args.command == "backfill":
        if ALLOW_ALL_CITIES_IN_BACKFILL and TEST_CITY_NAMES:
            raise ValueError("Use either --cities ... or --all-cities for backfill, not both.")
        if not ALLOW_ALL_CITIES_IN_BACKFILL and not TEST_CITY_NAMES:
            raise ValueError(
                "BACKFILL requires --cities by default. Use --all-cities only when an all-city "
                "historical rescan is deliberate."
            )

    if args.command not in {"bootstrap", "index"}:
        SCAN_START_DATE, SCAN_NUM_DAYS = _resolve_cli_date_range(
            args,
            allow_default_today=(args.command == "daily"),
        )


def main() -> None:
    args = parse_cli_args()
    apply_cli_args(args)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    PANCHANGA_MASTER_DIR.mkdir(parents=True, exist_ok=True)

    mode = clean_value(RUN_MODE).upper()
    valid_modes = {
        "DAILY",
        "BACKFILL",
        "REBUILD_ONLY",
        "INDEX_ONLY",
        "BOOTSTRAP_ONLY",
    }
    if mode not in valid_modes:
        raise ValueError(f"RUN_MODE must be one of {sorted(valid_modes)}, got {RUN_MODE!r}")

    if mode == "INDEX_ONLY":
        print("RUN MODE: INDEX_ONLY")
        print("No Drik calls will be made.")
        print("No Panchanga-master or special-events-master data will be changed.")
        generate_index_from_existing_html()
        print("\nINDEX_ONLY complete.")
        print(f"HTML website files: {HTML_OUTPUT_DIR}")
        return

    cities = pd.read_csv(
        INPUT_CSV,
        sep=None,
        engine="python",
        encoding="utf-8-sig",
    )
    cities.columns = cities.columns.str.strip()
    cities["_Configured Input Order"] = range(1, len(cities) + 1)

    required_columns = {
        "display_city",
        "search_city",
        "state_or_region",
        "country",
        "timezone",
    }
    missing = required_columns - set(cities.columns)
    if missing:
        raise ValueError(
            f"Input CSV is missing required columns: {sorted(missing)}"
        )

    if mode == "BOOTSTRAP_ONLY":
        print("RUN MODE: BOOTSTRAP_ONLY")
        bootstrap_panchanga_master_from_daily_text(cities)
        return

    output_dates = generate_date_range(SCAN_START_DATE, SCAN_NUM_DAYS)
    scan_dates = output_dates
    print(f"RUN MODE: {mode}")
    print("Requested output dates: " + ", ".join(output_dates))

    if mode == "REBUILD_ONLY":
        stored = load_panchanga_master_for_dates(output_dates)
        if stored.empty:
            raise RuntimeError(
                "REBUILD_ONLY found no stored Panchanga rows for the requested dates."
            )
        legacy_events = load_legacy_special_events_snapshot(output_dates)
        special_events_master = load_special_events_master(output_dates)
        results_df = master_rows_to_render_df(stored)
        results_df = apply_legacy_special_events_snapshot(results_df, legacy_events)
        results_df = apply_special_events_master(results_df, special_events_master)
        results_df.to_csv(INTERMEDIATE_CSV, index=False, encoding="utf-8-sig")
        generate_daily_text_files(results_df)
        generate_daily_html_files(results_df)
        rebuild_rolling_seven_day_pages()
        print("\nREBUILD_ONLY complete.")
        print(f"Rows rendered: {len(results_df)}")
        return

    scan_cities = cities.copy()
    if TEST_CITY_LIMIT is not None:
        scan_cities = scan_cities.head(TEST_CITY_LIMIT).copy()

    if TEST_CITY_NAMES:
        requested_city_names = {
            clean_value(city_name).lower()
            for city_name in TEST_CITY_NAMES
        }
        scan_cities = scan_cities[
            scan_cities["display_city"]
            .fillna("")
            .astype(str)
            .map(lambda value: clean_value(value).lower())
            .isin(requested_city_names)
        ].copy()
        if scan_cities.empty:
            raise ValueError(
                "TEST_CITY_NAMES did not match any display_city values "
                f"in {INPUT_CSV}: {TEST_CITY_NAMES}"
            )

    if mode == "BACKFILL" and not TEST_CITY_NAMES and not ALLOW_ALL_CITIES_IN_BACKFILL:
        raise ValueError(
            "BACKFILL requires TEST_CITY_NAMES by default. This protects against "
            "an accidental historical all-city rescan. Set explicit city names or "
            "set ALLOW_ALL_CITIES_IN_BACKFILL=True deliberately."
        )

    print(
        f"Cities to scan: {len(scan_cities)}"
        + (f" -> {', '.join(scan_cities['display_city'].astype(str).tolist())}" if len(scan_cities) <= 10 else "")
    )

    all_records: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        PLAYWRIGHT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        context: BrowserContext = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PLAYWRIGHT_PROFILE_DIR),
            headless=HEADLESS,
            viewport={"width": 1600, "height": 950},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        month_page = context.new_page()
        month_page.goto(
            MONTH_PANCHANG_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )
        month_page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
        wait_for_manual_captcha(
            month_page,
            reason="opening the month Panchanga page",
        )

        day_page = context.new_page()
        day_page.goto(
            DAY_PANCHANG_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )
        day_page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
        wait_for_manual_captcha(
            day_page,
            reason="opening the day Panchanga page",
        )

        try:
            for _, row in scan_cities.iterrows():
                input_order = int(row.get("_Configured Input Order", 999999))
                city_name = str(row.get("display_city", ""))
                print("\n" + "#" * 100)
                print(f"STARTING CITY: {city_name}")
                print("#" * 100)

                month_page = ensure_open_page(context, month_page)
                day_page = ensure_open_page(context, day_page)

                try:
                    city_records = process_city(
                        month_page=month_page,
                        day_page=day_page,
                        row=row,
                        scan_dates=scan_dates,
                        input_order=input_order,
                    )
                    all_records.extend(clean_records_panchanga_values(city_records))
                    print(f"Completed {city_name}: {len(city_records)} daily records")
                except Exception as exc:
                    error_message = f"{type(exc).__name__}: {exc}"
                    print(f"Failed for {city_name}: {error_message}")
                    all_records.extend(
                        create_error_records(
                            row=row,
                            scan_dates=scan_dates,
                            input_order=input_order,
                            error_message=error_message,
                        )
                    )
                    month_page = ensure_open_page(context, month_page)
                    day_page = ensure_open_page(context, day_page)

                if not month_page.is_closed():
                    month_page.wait_for_timeout(BETWEEN_CITIES_MS)
        finally:
            context.close()

    all_records = clean_records_panchanga_values(all_records)
    scan_df = pd.DataFrame(all_records)
    if scan_df.empty:
        raise RuntimeError(
            "No results were collected. Review the terminal output for the first browser error."
        )

    scan_df = scan_df[scan_df["Date"].astype(str).isin(output_dates)].copy()
    scan_df["Location Key"] = scan_df.apply(
        lambda row: get_master_location_key_from_record(row), axis=1
    )
    scan_df = scan_df.sort_values(["Date", "Input Order"], kind="stable")
    scan_df.to_csv(LAST_SCAN_CSV, index=False, encoding="utf-8-sig")
    print(f"\nCreated raw current-run CSV: {LAST_SCAN_CSV}")

    stats = upsert_panchanga_master(scan_df)
    print(
        f"Master update summary: {stats['written']} safe row(s) written, "
        f"{stats['rejected']} rejected, {stats['years']} year file(s) touched."
    )

    # Critical safety behavior: HTML/text is regenerated from ALL stored rows for
    # the affected date(s), not merely from scan_df. This is what prevents a
    # one-city backfill from deleting the other cities from the page.
    stored = load_panchanga_master_for_dates(output_dates)
    if stored.empty:
        raise RuntimeError(
            "No stored Panchanga rows are available for the requested dates after the master update."
        )

    stored_counts = stored.groupby("Date")["Location Key"].nunique().to_dict()
    print("Stored city counts used for publication:")
    for date_str in output_dates:
        print(f"  {date_str}: {stored_counts.get(date_str, 0)} city/location row(s)")

    legacy_events = load_legacy_special_events_snapshot(output_dates)
    special_events_master = load_special_events_master(output_dates)
    results_df = master_rows_to_render_df(stored)
    results_df = apply_legacy_special_events_snapshot(results_df, legacy_events)
    results_df = apply_special_events_master(
        results_df=results_df,
        master_df=special_events_master,
    )

    results_df.to_csv(INTERMEDIATE_CSV, index=False, encoding="utf-8-sig")
    print(f"Created consolidated publication CSV: {INTERMEDIATE_CSV}")

    generate_daily_text_files(results_df)
    generate_daily_html_files(results_df)
    rebuild_rolling_seven_day_pages()

    print("\nDone.")
    print(f"Raw current scan      : {LAST_SCAN_CSV}")
    print(f"Publication results   : {INTERMEDIATE_CSV}")
    print(f"Panchanga master dir  : {PANCHANGA_MASTER_DIR}")
    print(f"Daily text files      : {DAILY_TEXT_DIR}")
    print(f"HTML website files    : {HTML_OUTPUT_DIR}")


if __name__ == "__main__":
    main()
