from __future__ import annotations

"""
Krishna Jayanthi / Janmashtami standalone scanner - backfill-safe outputs
=================================================

Purpose
-------
This program is intentionally separate from the daily Panchanga scanner.
It scans a user-supplied candidate date range for every configured city,
collects the Panchanga boundaries needed for Krishna Jayanthi determination,
applies the agreed priority rules, calculates Nishita Madhya and a 48-minute
Puja Muhurtha (24 minutes before/after Nishita Madhya), and writes CSV output.

It does NOT create HTML or website files.

Inputs
------
1) The same city CSV used by the daily scanner:
       cities_panchanga_updated.csv

   Required columns:
       display_city, search_city, state_or_region, country, timezone

   Optional but strongly recommended:
       geoname_id

2) Candidate date range configured below.

Outputs
-------
1) krishna_jayanthi_audit.csv
   - one row per city per candidate date
   - raw Panchanga values, exact interval boundaries, Nishita calculations,
     rule flags, score/priority and diagnostic reason

2) krishna_jayanthi_final.csv
   - only the selected Krishna Jayanthi date for each city
   - includes a ready-to-display Final Message column for the daily scanner

Decision rules implemented in V1.2
--------------------------------
MANDATORY GATE for the candidate date:
    * Sunsign / Saura Maasa must be Simha
    * Amanta lunar month must contain Shravana or Bhadrapada
    * Paksha must be Krishna

Priority after the mandatory gate:
    1. Ashtami AND Rohini are both present at calculated Nishita Madhya.
    2. Otherwise, Ashtami and Rohini overlap at any time during the Panchanga
       day (sunrise of candidate date through next sunrise).
    3. If there is NO Ashtami-Rohini overlap in the relevant cycle, prefer a
       date where Ashtami is present at Nishita Madhya AND Ashtami begins at
       or after that date's sunrise.
    4. Otherwise, use the later date if Ashtami is present at that date's
       sunrise (the agreed next-day sunrise fallback).
    5. Budhavara is recorded as a perfection/tie-break indicator only; it is
       not mandatory and never overrides the earlier rules.

Important V1.2 safety behavior
----------------------------
If the supplied dates do not contain enough surrounding data to reconstruct
Saptami->Ashtami->Navami and Krittika->Rohini transitions, the result is left
for review rather than silently guessing.

The scanner automatically adds one lookup day before the requested start and
one lookup day after the requested end. These lookup rows are used only for
boundary reconstruction and are not candidate observance dates.
"""

from datetime import datetime, timedelta
import argparse
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo
import re

import pandas as pd
from playwright.sync_api import BrowserContext, Page, sync_playwright


# =============================================================================
# Configuration
# =============================================================================

MONTH_PANCHANG_URL = "https://www.drikpanchang.com/panchang/month-panchang.html"

INPUT_CSV = "cities_panchanga_updated.csv"
OUTPUT_ROOT = Path("festival_runs")
OUTPUT_DIR = Path("output_2o/krishna_jayanthi")
AUDIT_CSV = OUTPUT_DIR / "krishna_jayanthi_audit.csv"
FINAL_CSV = OUTPUT_DIR / "krishna_jayanthi_final.csv"
MESSAGE_CSV = OUTPUT_DIR / "krishna_jayanthi_messages.csv"
CACHE_CSV = Path("festival_runs/cache/krishna_jayanthi_scan_cache.csv")
ACTIVE_MONTH_ID = ""
EVENT_FOLDER_NAME = "sri_krishna_jayanthi"

# -----------------------------------------------------------------------------
# Runtime selection is now supplied from the command line.
# These values are populated by main() after parsing CLI arguments.
# -----------------------------------------------------------------------------
SCAN_START_DATE = ""
SCAN_END_DATE = ""
TEST_CITY_NAMES: list[str] = []
TEST_CITY_LIMIT: int | None = None

HEADLESS = False
PAGE_LOAD_WAIT_MS = 8000
AFTER_CITY_WAIT_MS = 7000
AFTER_DATE_WAIT_MS = 7000
BETWEEN_DATES_MS = 2500
BETWEEN_CITIES_MS = 3000
CAPTCHA_SAFE_WAIT_MS = 2500
PLAYWRIGHT_PROFILE_DIR = Path("playwright_profile")

# Nishita Puja Muhurtha = 24 minutes before and 24 minutes after
# the midpoint between candidate-date sunset and next-date sunrise.
NISHITA_HALF_WINDOW_MINUTES = 24


# =============================================================================
# Basic helpers
# =============================================================================

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


def normalize_timezone_name(value: str) -> str:
    cleaned = str(value).strip()
    return TIMEZONE_NAME_ALIASES.get(cleaned, cleaned)


def clean_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_value(value).lower())


def generate_date_range(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if end < start:
        raise ValueError("SCAN_END_DATE must be on or after SCAN_START_DATE")
    days = (end - start).days
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]


def get_scan_dates_with_lookup(start_date: str, end_date: str) -> tuple[list[str], list[str]]:
    candidates = generate_date_range(start_date, end_date)
    first = datetime.strptime(candidates[0], "%Y-%m-%d")
    last = datetime.strptime(candidates[-1], "%Y-%m-%d")
    scan_dates = [
        (first - timedelta(days=1)).strftime("%Y-%m-%d"),
        *candidates,
        (last + timedelta(days=1)).strftime("%Y-%m-%d"),
    ]
    return candidates, list(dict.fromkeys(scan_dates))


def yyyy_mm_dd_to_dd_mm_yyyy(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m/%Y")


def get_geoname_id(row: pd.Series) -> str:
    raw = clean_value(row.get("geoname_id", ""))
    if re.fullmatch(r"\d+\.0+", raw):
        raw = raw.split(".", 1)[0]
    return raw


def build_panchang_url(geoname_id: str, date_str: str) -> str:
    return (
        f"{MONTH_PANCHANG_URL}?geoname-id={clean_value(geoname_id)}"
        f"&date={yyyy_mm_dd_to_dd_mm_yyyy(date_str)}"
    )


def format_dt(dt: datetime | None) -> str:
    if dt is None:
        return ""
    # Local timezone is implicit from the city row. Keep seconds if Drik supplied them.
    return dt.strftime("%Y-%m-%d %I:%M:%S %p")


def format_clock(dt: datetime | None) -> str:
    if dt is None:
        return ""
    value = dt.strftime("%I:%M %p")
    return value[1:] if value.startswith("0") else value


def format_display_date(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %d, %Y")


def date_from_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


# =============================================================================
# CAPTCHA / browser helpers (adapted from the existing daily scanner)
# =============================================================================


def captcha_is_present(page: Page) -> bool:
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
        if any(marker in title for marker in [
            "just a moment", "attention required", "verify", "captcha", "security check"
        ]):
            return True
    except Exception:
        pass

    try:
        body_text = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        body_text = ""

    markers = [
        "i'm not a robot",
        "i am not a robot",
        "select all images",
        "verify you are human",
        "checking your browser",
        "just a moment",
        "attention required",
        "security check",
    ]
    return any(marker in body_text for marker in markers)


def wait_for_manual_captcha(page: Page, reason: str = "") -> None:
    if not captcha_is_present(page):
        return

    print("\n" + "!" * 100)
    print("HUMAN VERIFICATION REQUIRED")
    if reason:
        print(f"Detected while: {reason}")
    print("Complete the CAPTCHA in the browser, then return here and press Enter.")
    print("!" * 100)

    while True:
        input("Press Enter after completing the CAPTCHA... ")
        page.wait_for_timeout(3000)
        if not captcha_is_present(page):
            print("Verification cleared. Resuming scanner.\n")
            page.wait_for_timeout(CAPTCHA_SAFE_WAIT_MS)
            return
        print("Verification still appears to be present.")


def validate_direct_geoname_page(page: Page, expected_geoname_id: str, city: str, date_str: str) -> None:
    if not expected_geoname_id:
        return
    current_url = page.url
    query = parse_qs(urlparse(current_url).query)
    actual = clean_value((query.get("geoname-id") or [""])[0])
    if actual != clean_value(expected_geoname_id):
        raise RuntimeError(
            f"Wrong Drik location loaded for {city} on {date_str}. "
            f"Expected geoname-id={expected_geoname_id}; URL={current_url}"
        )


def open_direct_page(page: Page, url: str, geoname_id: str, city: str, date_str: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            print(f"Opening: {city} - {date_str}")
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
            wait_for_manual_captcha(page, reason=f"opening {city} on {date_str}")
            validate_direct_geoname_page(page, geoname_id, city, date_str)
            return
        except Exception as exc:
            last_error = exc
            retryable = any(x in str(exc) for x in [
                "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_TIMED_OUT",
                "Timeout", "Navigation timeout"
            ])
            if not retryable or attempt == 3:
                raise
            print(f"Retrying ({attempt + 1}/3) after: {exc}")
            page.wait_for_timeout(5000)
    if last_error:
        raise last_error


def neutralize_ad_overlays(page: Page) -> None:
    try:
        page.evaluate(
            """
            () => {
                const styleId = 'dp-playwright-ad-shield';
                if (!document.getElementById(styleId)) {
                    const style = document.createElement('style');
                    style.id = styleId;
                    style.textContent = `
                        iframe[title="Advertisement"], iframe[id^="aswift_"],
                        ins.adsbygoogle, div[id^="google_ads_"], .google-auto-placed,
                        [data-vignette-loaded="true"] { pointer-events:none !important; }
                    `;
                    document.head.appendChild(style);
                }
            }
            """
        )
    except Exception:
        pass


def focus_and_clear_input(page: Page, locator) -> None:
    wait_for_manual_captcha(page, "waiting for city input")
    neutralize_ad_overlays(page)
    locator.wait_for(state="visible", timeout=30000)
    locator.evaluate(
        """
        element => {
            element.focus();
            element.value = '';
            element.dispatchEvent(new Event('input', {bubbles:true}));
            element.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """
    )


def normalize_location_piece(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


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
    "thiruvananthapuram": ["Thiruvananthapuram", "Trivandrum", "Tiruvananthapuram"],
    "tiruvananthapuram": ["Thiruvananthapuram", "Trivandrum", "Tiruvananthapuram"],
    "trivandrum": ["Thiruvananthapuram", "Trivandrum", "Tiruvananthapuram"],
}


def get_city_aliases(search_city: str) -> list[str]:
    return CITY_NAME_ALIASES.get(clean_value(search_city).lower(), [clean_value(search_city)])


def set_city(page: Page, search_city: str, state_or_region: str, country: str) -> str:
    aliases = get_city_aliases(search_city)
    queries: list[str] = []
    for alias in aliases:
        queries += [
            alias,
            f"{alias} {state_or_region}" if state_or_region else "",
            f"{alias} {state_or_region} {country}" if state_or_region and country else "",
            f"{alias} {country}" if country else "",
        ]
    queries = [q for q in dict.fromkeys(queries) if q]

    alias_norms = {normalize_location_piece(a) for a in aliases}
    last_error = ""

    for query in queries:
        city_input = page.locator("#dp-direct-city-search")
        focus_and_clear_input(page, city_input)
        page.keyboard.type(query, delay=180)
        page.wait_for_timeout(3500)

        suggestions = page.locator("ul.ui-autocomplete li:visible")
        candidates: list[tuple[int, int, str]] = []
        for i in range(suggestions.count()):
            try:
                text = suggestions.nth(i).inner_text().strip()
            except Exception:
                continue
            lines = [x.strip() for x in text.splitlines() if x.strip()]
            if not lines:
                continue
            if normalize_location_piece(lines[0]) not in alias_norms:
                continue
            score = 100
            ntext = normalize_location_piece(text)
            if state_or_region and normalize_location_piece(state_or_region) in ntext:
                score += 50
            country_norm = normalize_location_piece(country)
            if country_norm in ntext or (country_norm in {"usa", "united states"} and "united states" in ntext):
                score += 30
            candidates.append((score, i, text))

        if not candidates:
            last_error = f"No exact suggestion for {query}"
            continue

        candidates.sort(reverse=True)
        _, idx, text = candidates[0]
        print(f"Selected city suggestion: {text}")
        target = suggestions.nth(idx)
        neutralize_ad_overlays(page)
        target.evaluate(
            """
            element => {
                element.scrollIntoView({block:'center'});
                const o={bubbles:true,cancelable:true,view:window};
                element.dispatchEvent(new MouseEvent('mousedown',o));
                element.dispatchEvent(new MouseEvent('mouseup',o));
                element.dispatchEvent(new MouseEvent('click',o));
            }
            """
        )
        page.wait_for_timeout(AFTER_CITY_WAIT_MS)
        try:
            selected = page.locator("#dp-direct-city-search").input_value().strip()
        except Exception:
            selected = ""
        if selected:
            return selected
        last_error = f"City selection produced blank value for {query}"

    raise RuntimeError(f"Could not select city {search_city}. {last_error}")


def find_date_input(page: Page):
    preferred = page.locator("#dp-date-picker")
    if preferred.count() > 0:
        return preferred.first
    inputs = page.locator("input")
    for i in range(inputs.count()):
        try:
            value = inputs.nth(i).input_value()
        except Exception:
            continue
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", value or ""):
            return inputs.nth(i)
    raise RuntimeError("Could not find Drik date input")


def set_date(page: Page, date_str: str) -> None:
    date_input = find_date_input(page)
    requested = yyyy_mm_dd_to_dd_mm_yyyy(date_str)
    if date_input.input_value() == requested:
        return
    focus_and_clear_input(page, date_input)
    page.keyboard.type(requested, delay=80)
    page.keyboard.press("Enter")
    page.wait_for_timeout(AFTER_DATE_WAIT_MS)
    wait_for_manual_captcha(page, f"changing date to {date_str}")
    current = find_date_input(page).input_value()
    if current != requested:
        raise RuntimeError(f"Date did not update. Expected {requested}; got {current}")


# =============================================================================
# Drik Month Panchang text parsing
# =============================================================================


def normalize_lines(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in str(text).splitlines()
        if line and line.strip()
    ]


def extract_line_value(text: str, labels: list[str]) -> str:
    """
    Same simple extraction pattern used by the existing working Panchanga scanner.

    Examples:
        Amanta Month Shravana
        Amanta Month: Shravana
        Weekday Budhawara
        Weekday: Budhawara
        Sunrise 06:36
        Sunrise: 06:36
    """
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
            if value:
                return value

    return ""


def extract_all_line_values(text: str, labels: list[str]) -> list[str]:
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
            if value and value not in values:
                values.append(value)

    return values


def _first_valid_time(value: str) -> str:
    value = clean_value(value)
    match = re.search(
        r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)(?!\d)",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _valid_named_value(value: str, valid_names: list[str]) -> str:
    value = clean_value(value)
    if not value:
        return ""

    for name in sorted(valid_names, key=len, reverse=True):
        if re.match(rf"^{re.escape(name)}(?:\b|$)", value, flags=re.IGNORECASE):
            return value

    return ""


TITHI_NAMES = [
    "Pratipada", "Prathama", "Dwitiya", "Tritiya", "Chaturthi", "Panchami",
    "Shashthi", "Shashti", "Saptami", "Ashtami", "Navami", "Dashami",
    "Ekadashi", "Dwadashi", "Trayodashi", "Chaturdashi",
    "Purnima", "Pournami", "Amavasya",
]

NAKSHATRA_NAMES = [
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashirsha", "Mrigashira",
    "Ardra", "Punarvasu", "Pushya", "Ashlesha", "Magha", "Purva Phalguni",
    "Uttara Phalguni", "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha",
    "Jyeshtha", "Mula", "Purva Ashadha", "Uttara Ashadha", "Shravana",
    "Dhanishtha", "Shatabhisha", "Purva Bhadrapada", "Uttara Bhadrapada",
    "Revati",
]

MONTH_NAMES = [
    "Chaitra", "Vaishakha", "Jyeshtha", "Ashadha", "Shravana", "Bhadrapada",
    "Ashwin", "Ashwina", "Kartika", "Margashirsha", "Pausha", "Magha",
    "Phalguna",
]

SUNSIGN_NAMES = [
    "Mesha", "Vrishabha", "Mithuna", "Karka", "Simha", "Kanya", "Tula",
    "Vrishchika", "Dhanu", "Makara", "Kumbha", "Meena",
]


def extract_month_page_values(month_text: str) -> dict[str, str]:
    """
    Extract the Krishna-Jayanthi fields from Drik MONTH PANCHANG.

    Core extraction intentionally mirrors the existing working Panchanga scanner.
    """
    tithi_values = extract_all_line_values(month_text, ["Tithi"])
    nakshatra_values = extract_all_line_values(month_text, ["Nakshatra"])

    tithis = [
        value
        for value in (_valid_named_value(v, TITHI_NAMES) for v in tithi_values)
        if value
    ]
    nakshatras = [
        value
        for value in (_valid_named_value(v, NAKSHATRA_NAMES) for v in nakshatra_values)
        if value
    ]

    amanta = extract_line_value(
        month_text,
        ["Amanta Month", "Amanta Maasa", "Amanta Masa"],
    )
    weekday = extract_line_value(
        month_text,
        ["Weekday", "Vaara", "Vara"],
    )
    paksha = extract_line_value(month_text, ["Paksha"])
    sunrise_raw = extract_line_value(month_text, ["Sunrise"])
    sunset_raw = extract_line_value(month_text, ["Sunset"])
    sunsign_raw = extract_line_value(
        month_text,
        ["Sunsign", "Sun Sign"],
    )

    return {
        "Sunrise": _first_valid_time(sunrise_raw),
        "Sunset": _first_valid_time(sunset_raw),
        "Tithi": "; ".join(dict.fromkeys(tithis)),
        "Nakshatra": "; ".join(dict.fromkeys(nakshatras)),
        "Paksha": clean_value(paksha),
        "Vaara": clean_value(weekday),
        "Amanta Maasa": _valid_named_value(amanta, MONTH_NAMES) or clean_value(amanta),
        "Sunsign": _valid_named_value(sunsign_raw, SUNSIGN_NAMES),
    }


def extract_day_values(page_text: str) -> dict[str, str]:
    # Backward-compatible name used by the rest of the standalone program.
    return extract_month_page_values(page_text)


def parse_time_on_date(time_text: str, date_str: str, timezone_str: str) -> datetime | None:
    """Parse either Drik 12-hour or 24-hour clock text on a local date.

    Examples accepted:
        06:36
        19:43
        06:36 AM
        07:43 PM
        06:36:15
        06:36:15 AM
    """
    text = clean_value(time_text)
    if not text:
        return None

    tz = ZoneInfo(normalize_timezone_name(timezone_str))
    base = datetime.strptime(date_str, "%Y-%m-%d")

    # Prefer a 12-hour time if AM/PM is present.
    m12 = re.search(
        r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)(?!\w)",
        text,
        flags=re.IGNORECASE,
    )
    if m12:
        time_part = re.sub(r"\s+", " ", m12.group(1).upper()).strip()
        for fmt in ["%I:%M:%S %p", "%I:%M %p"]:
            try:
                t = datetime.strptime(time_part, fmt).time()
                return datetime(
                    base.year, base.month, base.day,
                    t.hour, t.minute, t.second, tzinfo=tz
                )
            except ValueError:
                pass

    # Drik's 24-hour display (as in Sunrise: 06:36 / Sunset: 19:43).
    m24 = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?!\d)", text)
    if m24:
        hour = int(m24.group(1))
        minute = int(m24.group(2))
        second = int(m24.group(3) or 0)
        return datetime(
            base.year, base.month, base.day,
            hour, minute, second, tzinfo=tz
        )

    return None


def parse_upto_datetime(value: str, date_str: str, timezone_str: str) -> datetime | None:
    """Parse Drik strings such as 'Ashtami upto 02:11 AM, Sep 04'."""
    text = clean_value(value)
    if not text or re.search(r"\bupto\s+full\s+night\b", text, flags=re.IGNORECASE):
        return None

    m = re.search(r"\bupto\s+([^;|]+)", text, flags=re.IGNORECASE)
    if not m:
        return None
    upto = clean_value(m.group(1))
    upto = re.sub(
        r"\b(EDT|EST|CDT|CST|PDT|PST|MDT|MST|IST|AEDT|AEST|ACDT|ACST|AWST|NZDT|NZST)\b",
        "", upto, flags=re.IGNORECASE
    )
    upto = re.sub(r"\s+", " ", upto).strip(" ,")
    base = datetime.strptime(date_str, "%Y-%m-%d")
    tz = ZoneInfo(normalize_timezone_name(timezone_str))

    dated = re.match(
        r"^(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M),\s*([A-Za-z]{3,9})\s+(\d{1,2})(?:,?\s*(\d{4}))?$",
        upto, flags=re.IGNORECASE
    )
    if dated:
        time_part, month_part, day_part, explicit_year = dated.groups()
        year = int(explicit_year) if explicit_year else base.year
        for fmt in ["%I:%M:%S %p %b %d %Y", "%I:%M %p %b %d %Y", "%I:%M:%S %p %B %d %Y", "%I:%M %p %B %d %Y"]:
            try:
                parsed = datetime.strptime(f"{time_part} {month_part} {day_part} {year}", fmt).replace(tzinfo=tz)
                # Handle Dec -> Jan style rollover if needed.
                if parsed < (base.replace(tzinfo=tz) - timedelta(days=30)) and not explicit_year:
                    parsed = parsed.replace(year=year + 1)
                return parsed
            except ValueError:
                pass

    # No explicit date: Drik means the selected civil date unless the time is
    # past midnight and explicitly supplied with the next-day date. We therefore
    # keep the selected date here rather than guessing rollover.
    return parse_time_on_date(upto, date_str, timezone_str)


def starts_with_name(value: str, name: str) -> bool:
    return normalize_token(value).startswith(normalize_token(name))


def contains_name(value: str, name: str) -> bool:
    return normalize_token(name) in normalize_token(value)


# =============================================================================
# Raw scanning
# =============================================================================


def scan_city(page: Page, row: pd.Series, scan_dates: list[str], candidate_dates: set[str], input_order: int, cache: ObservationCache) -> list[dict[str, Any]]:
    city = clean_value(row.get("display_city", ""))
    search_city = clean_value(row.get("search_city", city))
    state = clean_value(row.get("state_or_region", ""))
    country = clean_value(row.get("country", ""))
    timezone = clean_value(row.get("timezone", ""))
    geoname_id = get_geoname_id(row)

    selected_location = search_city or city
    if not geoname_id:
        page.goto(MONTH_PANCHANG_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
        wait_for_manual_captcha(page, f"opening Month Panchang for {city}")
        selected_location = set_city(page, search_city, state, country)

    records: list[dict[str, Any]] = []
    for date_str in scan_dates:
        cached = cache.get(city, state, country, timezone, geoname_id, date_str)
        if cached is not None:
            print(f"CACHE {city} {date_str}")
            records.append({
                "Input Order": input_order,
                "Date": date_str,
                "Is Candidate Date": date_str in candidate_dates,
                "City": city,
                "State/Region": state,
                "Country": country,
                "Timezone": timezone,
                "Geoname ID": geoname_id,
                "Selected Drik Location": cached.get("Selected Drik Location", selected_location),
                "Sunrise": cached.get("Sunrise", ""),
                "Sunset": cached.get("Sunset", ""),
                "Tithi": cached.get("Tithi", ""),
                "Nakshatra": cached.get("Nakshatra", ""),
                "Paksha": cached.get("Paksha", ""),
                "Vaara": cached.get("Vaara", ""),
                "Amanta Maasa": cached.get("Amanta Maasa", ""),
                "Sunsign": cached.get("Sunsign", ""),
                "Scan Error": cached.get("Scan Error", ""),
            })
            continue

        try:
            if geoname_id:
                open_direct_page(page, build_panchang_url(geoname_id, date_str), geoname_id, city, date_str)
            else:
                set_date(page, date_str)

            # IMPORTANT:
            # Krishna Jayanthi must be scanned from Drik MONTH PANCHANG.
            # The Month Panchang summary directly exposes the fields we need:
            # Sunrise, Sunset, Amanta Month, Weekday, Paksha, Tithi,
            # Nakshatra and Sunsign.
            text = page.locator("body").inner_text(timeout=30000)
            values = extract_day_values(text)

            print(
                f"{city} {date_str} | "
                f"Sunrise={values['Sunrise']!r} | "
                f"Sunset={values['Sunset']!r} | "
                f"Amanta Month={values['Amanta Maasa']!r} | "
                f"Weekday={values['Vaara']!r} | "
                f"Paksha={values['Paksha']!r} | "
                f"Tithi={values['Tithi']!r} | "
                f"Nakshatra={values['Nakshatra']!r} | "
                f"Sunsign={values['Sunsign']!r}"
            )

            records.append({
                "Input Order": input_order,
                "Date": date_str,
                "Is Candidate Date": date_str in candidate_dates,
                "City": city,
                "State/Region": state,
                "Country": country,
                "Timezone": timezone,
                "Geoname ID": geoname_id,
                "Selected Drik Location": selected_location,
                "Sunrise": values["Sunrise"],
                "Sunset": values["Sunset"],
                "Tithi": values["Tithi"],
                "Nakshatra": values["Nakshatra"],
                "Paksha": values["Paksha"],
                "Vaara": values["Vaara"],
                "Amanta Maasa": values["Amanta Maasa"],
                "Sunsign": values["Sunsign"],
                "Scan Error": "",
            })
            cache.put({
                "Key": ObservationCache.make_key(city, state, country, timezone, geoname_id),
                "Date": date_str,
                "City": city,
                "State/Region": state,
                "Country": country,
                "Timezone": timezone,
                "Geoname ID": geoname_id,
                "Selected Drik Location": selected_location,
                "Sunrise": values["Sunrise"],
                "Sunset": values["Sunset"],
                "Tithi": values["Tithi"],
                "Nakshatra": values["Nakshatra"],
                "Paksha": values["Paksha"],
                "Vaara": values["Vaara"],
                "Amanta Maasa": values["Amanta Maasa"],
                "Sunsign": values["Sunsign"],
                "Scan Error": "",
            })
        except Exception as exc:
            records.append({
                "Input Order": input_order,
                "Date": date_str,
                "Is Candidate Date": date_str in candidate_dates,
                "City": city,
                "State/Region": state,
                "Country": country,
                "Timezone": timezone,
                "Geoname ID": geoname_id,
                "Selected Drik Location": selected_location,
                "Sunrise": "", "Sunset": "", "Tithi": "", "Nakshatra": "",
                "Paksha": "", "Vaara": "", "Amanta Maasa": "", "Sunsign": "",
                "Scan Error": f"{type(exc).__name__}: {exc}",
            })
            cache.put({
                "Key": ObservationCache.make_key(city, state, country, timezone, geoname_id),
                "Date": date_str,
                "City": city,
                "State/Region": state,
                "Country": country,
                "Timezone": timezone,
                "Geoname ID": geoname_id,
                "Selected Drik Location": selected_location,
                "Sunrise": "", "Sunset": "", "Tithi": "", "Nakshatra": "",
                "Paksha": "", "Vaara": "", "Amanta Maasa": "", "Sunsign": "",
                "Scan Error": f"{type(exc).__name__}: {exc}",
            })
            print(f"WARNING: {city} {date_str}: {exc}")

        page.wait_for_timeout(BETWEEN_DATES_MS)

    return records




# =============================================================================
# Persistent raw cache
# =============================================================================

CACHE_COLUMNS = [
    "Key",
    "Date",
    "City",
    "State/Region",
    "Country",
    "Timezone",
    "Geoname ID",
    "Selected Drik Location",
    "Sunrise",
    "Sunset",
    "Tithi",
    "Nakshatra",
    "Paksha",
    "Vaara",
    "Amanta Maasa",
    "Sunsign",
    "Scan Error",
]

class ObservationCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
            except Exception:
                df = pd.DataFrame(columns=CACHE_COLUMNS)
        else:
            df = pd.DataFrame(columns=CACHE_COLUMNS)
        for col in CACHE_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        self.df = df[CACHE_COLUMNS].copy()
        self.cache_hits = 0
        self.fresh_scans = 0

    @staticmethod
    def make_key(city: str, state: str, country: str, timezone: str, geoname_id: str) -> str:
        if geoname_id:
            return f"GEONAME::{geoname_id}"
        return "CITY::" + "||".join([city, state, country, timezone]).strip()

    def get(self, city: str, state: str, country: str, timezone: str, geoname_id: str, date_str: str) -> dict[str, Any] | None:
        key = self.make_key(city, state, country, timezone, geoname_id)
        matched = self.df[(self.df["Key"] == key) & (self.df["Date"] == date_str)]
        if matched.empty:
            return None
        self.cache_hits += 1
        return matched.iloc[-1].to_dict()

    def put(self, record: dict[str, Any]) -> None:
        self.fresh_scans += 1
        normalized = {c: "" for c in CACHE_COLUMNS}
        for col in CACHE_COLUMNS:
            if col in record:
                normalized[col] = "" if pd.isna(record[col]) else str(record[col]).strip()
        self.df = pd.concat([self.df, pd.DataFrame([normalized])], ignore_index=True)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.df.to_csv(self.path, index=False, encoding="utf-8-sig")


def build_krishna_message_row(selected: dict[str, Any]) -> dict[str, str]:
    city = clean_value(selected.get("City", ""))
    event_name = "Sri Krishna Jayanthi"
    date_str = clean_value(selected.get("Festival Date", selected.get("Date", "")))
    upavaasa_date = clean_value(selected.get("Upavaasa Date", date_str))
    puja_start = clean_value(selected.get("Calculated Puja Start", ""))
    puja_end = clean_value(selected.get("Calculated Puja End", ""))
    decision_rule = clean_value(selected.get("Decision Rule", ""))
    final_message = clean_value(selected.get("Final Message", ""))

    details_parts = []
    if upavaasa_date:
        details_parts.append(f"Upavaasa: {upavaasa_date}")
    if date_str:
        details_parts.append(f"Puja Date: {date_str}")
    if puja_start or puja_end:
        details_parts.append(f"Nishita Puja: {puja_start} to {puja_end}")
    special_details = " | ".join(details_parts)

    note_parts = []
    if decision_rule:
        note_parts.append(f"Selection rule: {decision_rule}.")
    if clean_value(selected.get("Needs Review", "")) == "True":
        reason = clean_value(selected.get("Review Reason", ""))
        note_parts.append(f"Needs review. {reason}".strip())
    note = " ".join(part for part in note_parts if part).strip()

    return {
        "Place Key": "",
        "Location Key": "",
        "City": city,
        "State/Region": clean_value(selected.get("State/Region", "")),
        "Country": clean_value(selected.get("Country", "")),
        "Timezone": clean_value(selected.get("Timezone", "")),
        "Geoname ID": clean_value(selected.get("Geoname ID", "")),
        "Event Family": "KRISHNA_JAYANTHI",
        "Event Name": event_name,
        "Event Type": "KRISHNA_JAYANTHI",
        "Condition Code": clean_value(selected.get("Decision Rule", "")),
        "Cycle Anchor": "",
        "Completeness Status": "COMPLETE",
        "Date": date_str,
        "Action Role": "OBSERVANCE",
        "Special Events": event_name,
        "Special Event Details": special_details,
        "Note": note,
        "Message": final_message or event_name,
        "Source Module": "krishna_jayanthi_engine.py",
        "Rule Version": "KRISHNA_JAYANTHI_V2_1_STANDARDIZED",
    }

MESSAGE_COLUMNS = [
    "Place Key", "Location Key", "City", "State/Region", "Country", "Timezone",
    "Geoname ID", "Event Family", "Event Name", "Event Type", "Condition Code",
    "Cycle Anchor", "Completeness Status", "Date", "Action Role",
    "Special Events", "Special Event Details", "Note", "Message",
    "Source Module", "Rule Version",
]
# =============================================================================
# Boundary reconstruction
# =============================================================================


def split_panchanga_occurrences(value: str) -> list[str]:
    """
    Split a Drik Month Panchang field into each Tithi/Nakshatra occurrence.

    Drik often puts TWO transitions in the same daily field, for example:

        Shashthi upto 06:55 AM; Saptami upto 04:55 AM, Sep 04

    or:

        Krittika upto 06:59 AM; Rohini upto 05:34 AM, Sep 05

    The old code checked only whether the whole field STARTED with the requested
    Tithi/Nakshatra.  That missed Saptami/Rohini when they were the second item.
    """
    raw = clean_value(value)
    if not raw:
        return []

    parts = [
        clean_value(part)
        for part in re.split(r"\s*;\s*", raw)
        if clean_value(part)
    ]
    return parts


def find_named_occurrences(
    records: list[dict[str, Any]],
    field: str,
    name: str,
) -> list[tuple[dict[str, Any], str]]:
    """
    Return every individual semicolon-delimited occurrence whose value starts
    with the requested Tithi/Nakshatra name.
    """
    matches: list[tuple[dict[str, Any], str]] = []

    for record in sorted(records, key=lambda r: clean_value(r.get("Date", ""))):
        for occurrence in split_panchanga_occurrences(
            clean_value(record.get(field, ""))
        ):
            if starts_with_name(occurrence, name):
                matches.append((record, occurrence))

    return matches


def infer_boundary_from_named_record(
    records: list[dict[str, Any]],
    field: str,
    name: str,
    timezone: str,
) -> datetime | None:
    """
    End of the named Tithi/Nakshatra from its own Drik 'upto' value.

    IMPORTANT: each semicolon-delimited occurrence is parsed separately.  This
    allows boundaries to be reconstructed when the requested item is second in
    the field, e.g. 'Shashthi ...; Saptami ...' or
    'Krittika ...; Rohini ...'.
    """
    for record, occurrence in find_named_occurrences(records, field, name):
        dt = parse_upto_datetime(
            occurrence,
            clean_value(record.get("Date", "")),
            timezone,
        )
        if dt:
            return dt

    return None


def infer_ashtami_interval(records: list[dict[str, Any]], timezone: str) -> tuple[datetime | None, datetime | None]:
    # Ashtami begins when Saptami ends and ends at its own 'upto'.
    start = infer_boundary_from_named_record(records, "Tithi", "Saptami", timezone)
    end = infer_boundary_from_named_record(records, "Tithi", "Ashtami", timezone)
    return start, end


def infer_rohini_interval(records: list[dict[str, Any]], timezone: str) -> tuple[datetime | None, datetime | None]:
    # Rohini begins when Krittika ends and ends at its own 'upto'.
    start = infer_boundary_from_named_record(records, "Nakshatra", "Krittika", timezone)
    end = infer_boundary_from_named_record(records, "Nakshatra", "Rohini", timezone)
    return start, end


def boundary_source_text(
    records: list[dict[str, Any]],
    field: str,
    name: str,
) -> str:
    """Audit helper: show the exact Drik occurrence used for a boundary."""
    matches = find_named_occurrences(records, field, name)
    if not matches:
        return ""

    record, occurrence = matches[0]
    return f"{clean_value(record.get('Date', ''))}: {occurrence}"


def interval_contains(start: datetime | None, end: datetime | None, moment: datetime | None) -> bool:
    return bool(start and end and moment and start <= moment < end)


def intervals_overlap(a_start: datetime | None, a_end: datetime | None, b_start: datetime | None, b_end: datetime | None) -> tuple[datetime | None, datetime | None]:
    if not all([a_start, a_end, b_start, b_end]):
        return None, None
    start = max(a_start, b_start)  # type: ignore[arg-type]
    end = min(a_end, b_end)        # type: ignore[arg-type]
    if start < end:
        return start, end
    return None, None


def clamp_overlap(start: datetime | None, end: datetime | None, window_start: datetime | None, window_end: datetime | None) -> tuple[datetime | None, datetime | None]:
    if not all([start, end, window_start, window_end]):
        return None, None
    s = max(start, window_start)  # type: ignore[arg-type]
    e = min(end, window_end)      # type: ignore[arg-type]
    return (s, e) if s < e else (None, None)


# =============================================================================
# Candidate-date analysis and rule engine
# =============================================================================


def is_mandatory_gate_satisfied(record: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    sunsign_ok = contains_name(record.get("Sunsign", ""), "Simha")
    month_text = clean_value(record.get("Amanta Maasa", ""))
    month_ok = contains_name(month_text, "Shravana") or contains_name(month_text, "Bhadrapada")
    paksha_ok = contains_name(record.get("Paksha", ""), "Krishna")

    if not sunsign_ok:
        reasons.append("Saura Maasa/Sunsign is not Simha")
    if not month_ok:
        reasons.append("Amanta Maasa is neither Shravana nor Bhadrapada")
    if not paksha_ok:
        reasons.append("Paksha is not Krishna")

    return sunsign_ok and month_ok and paksha_ok, reasons


def determine_no_overlap_upavaasa(
    records: list[dict[str, Any]],
    ashtami_start: datetime | None,
    ashtami_end: datetime | None,
    timezone: str,
) -> dict[str, Any]:
    """
    Determine Upavaasa date when Ashtami and Rohini do NOT overlap.

    CASE 1:
      Ashtami starts at/before Day 1 sunrise.
      -> Upavaasa Day 1, Puja at the Nishita following Day 1.

    CASE 2:
      Ashtami starts after Day 1 sunrise and remains through Day 2 sunrise.
      -> Day 1 is Saptami-viddha Ashtami.
      -> Upavaasa Day 2, Puja at the Nishita following Day 2.

    CASE 3:
      Ashtami starts after Day 1 sunrise but ends before Day 2 sunrise.
      -> Upavaasa Day 1, Puja at the Nishita following Day 1.

    Boundary convention:
      * start exactly at Day 1 sunrise -> Day 1
      * end exactly at Day 2 sunrise -> Ashtami reaches Day 2 sunrise
    """
    result = {
        "target_date": "",
        "rule": "",
        "case": "",
        "day1_date": "",
        "day2_date": "",
        "day1_sunrise": None,
        "day2_sunrise": None,
        "starts_after_day1_sunrise": False,
        "reaches_day2_sunrise": False,
        "saptami_viddha_ashtami": False,
        "review": "",
    }

    if not ashtami_start or not ashtami_end:
        result["review"] = "Ashtami start/end is incomplete."
        return result

    by_date = {
        clean_value(record.get("Date", "")): record
        for record in records
        if clean_value(record.get("Date", ""))
    }

    day1_date = ashtami_start.strftime("%Y-%m-%d")
    day2_date = (
        datetime.strptime(day1_date, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    result["day1_date"] = day1_date
    result["day2_date"] = day2_date

    day1_record = by_date.get(day1_date)
    day2_record = by_date.get(day2_date)

    if not day1_record or not day2_record:
        result["review"] = (
            "Could not find Day 1 and Day 2 records required for the "
            "no-overlap sunrise decision."
        )
        return result

    day1_sunrise = parse_time_on_date(
        clean_value(day1_record.get("Sunrise", "")),
        day1_date,
        timezone,
    )
    day2_sunrise = parse_time_on_date(
        clean_value(day2_record.get("Sunrise", "")),
        day2_date,
        timezone,
    )

    result["day1_sunrise"] = day1_sunrise
    result["day2_sunrise"] = day2_sunrise

    if not day1_sunrise or not day2_sunrise:
        result["review"] = (
            "Could not parse Day 1 / Day 2 sunrise required for the "
            "no-overlap sunrise decision."
        )
        return result

    starts_after_day1_sunrise = ashtami_start > day1_sunrise
    reaches_day2_sunrise = ashtami_end >= day2_sunrise

    result["starts_after_day1_sunrise"] = starts_after_day1_sunrise
    result["reaches_day2_sunrise"] = reaches_day2_sunrise

    # CASE 1
    if not starts_after_day1_sunrise:
        result["target_date"] = day1_date
        result["case"] = "CASE_1_ASHTAMI_PRESENT_AT_DAY1_SUNRISE"
        result["rule"] = (
            "NO_ROHINI_OVERLAP_ASHTAMI_PRESENT_AT_DAY1_SUNRISE_UPAVAASA_DAY1"
        )
        return result

    # Day 1 sunrise was still Saptami.
    result["saptami_viddha_ashtami"] = True

    # CASE 2
    if reaches_day2_sunrise:
        result["target_date"] = day2_date
        result["case"] = "CASE_2_SAPTAMI_VIDDHA_ASHTAMI_REACHES_DAY2_SUNRISE"
        result["rule"] = (
            "NO_ROHINI_OVERLAP_SAPTAMI_VIDDHA_ASHTAMI_SHIFT_TO_DAY2"
        )
        return result

    # CASE 3
    result["target_date"] = day1_date
    result["case"] = "CASE_3_ASHTAMI_DOES_NOT_REACH_DAY2_SUNRISE"
    result["rule"] = (
        "NO_ROHINI_OVERLAP_ASHTAMI_DOES_NOT_REACH_DAY2_SUNRISE_UPAVAASA_DAY1"
    )
    return result


def analyze_city_records(records: list[dict[str, Any]], candidate_dates: list[str]) -> list[dict[str, Any]]:
    if not records:
        return []

    timezone = clean_value(records[0].get("Timezone", ""))
    by_date = {clean_value(r.get("Date", "")): r for r in records}

    ashtami_start, ashtami_end = infer_ashtami_interval(records, timezone)
    rohini_start, rohini_end = infer_rohini_interval(records, timezone)
    joint_start, joint_end = intervals_overlap(
        ashtami_start, ashtami_end, rohini_start, rohini_end
    )
    joint_overlap_exists = bool(joint_start and joint_end)

    no_overlap_selection = {
        "target_date": "",
        "rule": "",
        "case": "",
        "day1_date": "",
        "day2_date": "",
        "day1_sunrise": None,
        "day2_sunrise": None,
        "starts_after_day1_sunrise": False,
        "reaches_day2_sunrise": False,
        "saptami_viddha_ashtami": False,
        "review": "",
    }

    if (
        ashtami_start
        and ashtami_end
        and rohini_start
        and rohini_end
        and not joint_overlap_exists
    ):
        no_overlap_selection = determine_no_overlap_upavaasa(
            records=records,
            ashtami_start=ashtami_start,
            ashtami_end=ashtami_end,
            timezone=timezone,
        )

    # Preserve the exact raw Drik occurrences used to reconstruct the interval.
    # These are diagnostic only and make the audit CSV much easier to verify.
    ashtami_start_source = boundary_source_text(records, "Tithi", "Saptami")
    ashtami_end_source = boundary_source_text(records, "Tithi", "Ashtami")
    rohini_start_source = boundary_source_text(records, "Nakshatra", "Krittika")
    rohini_end_source = boundary_source_text(records, "Nakshatra", "Rohini")

    analyses: list[dict[str, Any]] = []

    for date_str in candidate_dates:
        record = by_date.get(date_str)
        if not record:
            continue

        next_date = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        next_record = by_date.get(next_date)

        sunrise = parse_time_on_date(record.get("Sunrise", ""), date_str, timezone)
        sunset = parse_time_on_date(record.get("Sunset", ""), date_str, timezone)
        next_sunrise = (
            parse_time_on_date(next_record.get("Sunrise", ""), next_date, timezone)
            if next_record else None
        )

        nishita = None
        puja_start = None
        puja_end = None
        if sunset and next_sunrise and next_sunrise > sunset:
            nishita = sunset + (next_sunrise - sunset) / 2
            puja_start = nishita - timedelta(minutes=NISHITA_HALF_WINDOW_MINUTES)
            puja_end = nishita + timedelta(minutes=NISHITA_HALF_WINDOW_MINUTES)

        gate_ok, gate_failures = is_mandatory_gate_satisfied(record)

        ashtami_at_sunrise = interval_contains(ashtami_start, ashtami_end, sunrise)
        ashtami_at_nishita = interval_contains(ashtami_start, ashtami_end, nishita)
        rohini_at_nishita = interval_contains(rohini_start, rohini_end, nishita)

        overlap_day_start, overlap_day_end = clamp_overlap(joint_start, joint_end, sunrise, next_sunrise)
        overlap_in_panchanga_day = bool(overlap_day_start and overlap_day_end)
        overlap_at_nishita = bool(ashtami_at_nishita and rohini_at_nishita)

        ashtami_starts_on_or_after_sunrise = bool(ashtami_start and sunrise and ashtami_start >= sunrise)
        ashtami_starts_before_next_sunrise = bool(ashtami_start and next_sunrise and ashtami_start < next_sunrise)

        puja_joint_start, puja_joint_end = clamp_overlap(joint_start, joint_end, puja_start, puja_end)
        joint_overlap_during_puja = bool(puja_joint_start and puja_joint_end)
        entire_puja_in_joint_overlap = bool(
            joint_start and joint_end and puja_start and puja_end
            and joint_start <= puja_start and puja_end <= joint_end
        )

        vaara = clean_value(record.get("Vaara", ""))
        budhavara = (
            contains_name(vaara, "Budha")
            or contains_name(vaara, "Budhwa")
            or contains_name(vaara, "Wednesday")
        )

        # Lower numeric priority is better.
        priority = 999
        rule = "NO_SELECTION"
        review = ""

        # IMPORTANT Krishna Jayanthi hierarchy:
        #
        # A) If Ashtami and Rohini overlap:
        #      1. Nishita inside overlap -> choose that night.
        #      2. Two Nishitas inside overlap -> city-level post-processing
        #         chooses the SECOND Nishita.
        #      3. No Nishita inside overlap -> choose the first Nishita AFTER
        #         the joint overlap ends.
        #
        # B) If Ashtami and Rohini do NOT overlap:
        #      CASE 1: Ashtami starts at/before Day 1 sunrise -> Day 1.
        #      CASE 2: Ashtami starts after Day 1 sunrise and remains through
        #              Day 2 sunrise -> Day 2 (avoid Saptami-viddha Ashtami).
        #      CASE 3: Ashtami starts after Day 1 sunrise but ends before
        #              Day 2 sunrise -> Day 1.
        #
        # In the no-overlap branch, Ashtami-at-Nishita is diagnostic only.

        first_nishita_after_overlap = bool(
            joint_overlap_exists
            and nishita
            and joint_end
            and nishita > joint_end
        )

        if not gate_ok:
            rule = "MANDATORY_GATE_FAILED"
            review = "; ".join(gate_failures)

        elif not (ashtami_start and ashtami_end and rohini_start and rohini_end):
            rule = "BOUNDARY_DATA_INCOMPLETE"
            review = (
                "Could not reconstruct complete Ashtami and Rohini intervals "
                "from supplied scan window."
            )

        elif overlap_at_nishita:
            priority = 10
            rule = "ASHTAMI_ROHINI_AT_NISHITA"

        elif joint_overlap_exists:
            if first_nishita_after_overlap:
                priority = 20
                rule = "ASHTAMI_ROHINI_OVERLAP_THEN_NEXT_NISHITA"
            else:
                priority = 999
                rule = "ASHTAMI_ROHINI_OVERLAP_WAIT_FOR_NEXT_NISHITA"
                review = (
                    "Ashtami and Rohini overlap, but this night's Nishita is "
                    "not inside the overlap and is not after the overlap has "
                    "ended. The Puja belongs to the first Nishita after the "
                    "joint overlap ends."
                )

        else:
            target_date = clean_value(
                no_overlap_selection.get("target_date", "")
            )

            if not target_date:
                priority = 999
                rule = "NO_ROHINI_OVERLAP_SUNRISE_RULE_INCOMPLETE"
                review = clean_value(
                    no_overlap_selection.get("review", "")
                ) or (
                    "Could not determine the no-overlap Upavaasa date from "
                    "Ashtami start/end and consecutive sunrises."
                )

            elif date_str == target_date:
                priority = 30
                rule = clean_value(
                    no_overlap_selection.get("rule", "")
                )

            else:
                priority = 999
                rule = "NO_ROHINI_OVERLAP_NOT_SELECTED_BY_SUNRISE_RULE"

        # Budhavara is only a tie-break among rows with exactly the same religious priority.
        tie_break = 0 if budhavara else 1

        analyses.append({
            **record,
            "Mandatory Gate": gate_ok,
            "Gate Failure Reason": "; ".join(gate_failures),
            "Ashtami Start": format_dt(ashtami_start),
            "Ashtami End": format_dt(ashtami_end),
            "Rohini Start": format_dt(rohini_start),
            "Rohini End": format_dt(rohini_end),
            "Ashtami Start Source": ashtami_start_source,
            "Ashtami End Source": ashtami_end_source,
            "Rohini Start Source": rohini_start_source,
            "Rohini End Source": rohini_end_source,
            "Ashtami-Rohini Overlap Start": format_dt(joint_start),
            "Ashtami-Rohini Overlap End": format_dt(joint_end),
            "Candidate Sunrise": format_dt(sunrise),
            "Candidate Sunset": format_dt(sunset),
            "Next Sunrise": format_dt(next_sunrise),
            "Upavaasa Date": date_str,
            "Calculated Nishita Madhya": format_dt(nishita),
            "Calculated Puja Start": format_dt(puja_start),
            "Calculated Puja End": format_dt(puja_end),
            "Ashtami At Sunrise": ashtami_at_sunrise,
            "Ashtami At Nishita": ashtami_at_nishita,
            "Rohini At Nishita": rohini_at_nishita,
            "Ashtami-Rohini Overlap In Panchanga Day": overlap_in_panchanga_day,
            "Ashtami-Rohini At Nishita": overlap_at_nishita,
            "Joint Overlap Exists": bool(joint_start and joint_end),
            "Nishita After Joint Overlap End": bool(
                joint_end and nishita and nishita > joint_end
            ),
            "Joint Overlap During Puja Window": joint_overlap_during_puja,
            "Entire Puja Window In Joint Overlap": entire_puja_in_joint_overlap,
            "Joint Puja Overlap Start": format_dt(puja_joint_start),
            "Joint Puja Overlap End": format_dt(puja_joint_end),
            "Ashtami Starts On/After Sunrise": ashtami_starts_on_or_after_sunrise,
            "Ashtami Starts Before Next Sunrise": ashtami_starts_before_next_sunrise,

            "No-Overlap Day 1": clean_value(
                no_overlap_selection.get("day1_date", "")
            ),
            "No-Overlap Day 1 Sunrise": format_dt(
                no_overlap_selection.get("day1_sunrise")
            ),
            "No-Overlap Day 2": clean_value(
                no_overlap_selection.get("day2_date", "")
            ),
            "No-Overlap Day 2 Sunrise": format_dt(
                no_overlap_selection.get("day2_sunrise")
            ),
            "No-Overlap Ashtami Starts After Day 1 Sunrise": bool(
                no_overlap_selection.get("starts_after_day1_sunrise", False)
            ),
            "No-Overlap Ashtami Reaches Day 2 Sunrise": bool(
                no_overlap_selection.get("reaches_day2_sunrise", False)
            ),
            "Saptami Viddha Ashtami": bool(
                no_overlap_selection.get("saptami_viddha_ashtami", False)
            ),
            "No-Overlap Selected Upavaasa Date": clean_value(
                no_overlap_selection.get("target_date", "")
            ),
            "No-Overlap Case": clean_value(
                no_overlap_selection.get("case", "")
            ),
            "No-Overlap Rule": clean_value(
                no_overlap_selection.get("rule", "")
            ),

            "Budhavara": budhavara,
            "Decision Priority": priority,
            "Decision Tie Break": tie_break,
            "Decision Rule": rule,
            "Needs Review": bool(review),
            "Review Reason": review,
        })

    return apply_second_nishita_preference(analyses)


def apply_second_nishita_preference(
    analyses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Apply the Krishna Jayanthi rule across ALL candidate nights for one city.

    If the Ashtami-Rohini joint overlap contains two Nishita moments, the
    SECOND Nishita is chosen.

    Example:
        Night 1 Nishita is inside Ashtami+Rohini overlap
        Night 2 Nishita is also inside Ashtami+Rohini overlap

    Result:
        Night 2 is selected.
        Night 1 is retained in the audit CSV but cannot win selection.

    If there is only one Nishita inside the overlap, the ordinary
    ASHTAMI_ROHINI_AT_NISHITA rule remains unchanged.
    """
    qualifying = [
        row
        for row in analyses
        if clean_value(row.get("Decision Rule", "")) == "ASHTAMI_ROHINI_AT_NISHITA"
        and int(row.get("Decision Priority", 999)) == 10
    ]

    qualifying.sort(key=lambda row: clean_value(row.get("Date", "")))

    count = len(qualifying)

    for row in analyses:
        row["Qualifying Nishita Count In Joint Overlap"] = count
        row["Qualifying Nishita Sequence"] = ""
        row["Nishita Selection Note"] = ""

    for sequence, row in enumerate(qualifying, start=1):
        row["Qualifying Nishita Sequence"] = sequence

    if count == 1:
        qualifying[0]["Nishita Selection Note"] = (
            "Only one Nishita falls inside the Ashtami-Rohini joint overlap."
        )
        return analyses

    if count >= 2:
        # The user's rule is specifically to choose the SECOND Nishita.
        selected_second = qualifying[1]

        for sequence, row in enumerate(qualifying, start=1):
            if row is selected_second:
                row["Decision Priority"] = 5
                row["Decision Rule"] = "ASHTAMI_ROHINI_AT_SECOND_NISHITA"
                row["Nishita Selection Note"] = (
                    f"{count} Nishita moments fall inside the Ashtami-Rohini "
                    "joint overlap; the second Nishita is selected."
                )
            else:
                row["Decision Priority"] = 999

                if sequence == 1:
                    row["Decision Rule"] = (
                        "ASHTAMI_ROHINI_AT_FIRST_NISHITA_NOT_SELECTED"
                    )
                    row["Nishita Selection Note"] = (
                        "This is the first Nishita inside the Ashtami-Rohini "
                        "joint overlap. It is not selected because the second "
                        "Nishita takes precedence."
                    )
                else:
                    row["Decision Rule"] = (
                        "ASHTAMI_ROHINI_AT_LATER_NISHITA_NOT_SELECTED"
                    )
                    row["Nishita Selection Note"] = (
                        "This Nishita is also inside the Ashtami-Rohini joint "
                        "overlap, but the prescribed selection is specifically "
                        "the second Nishita."
                    )

    return analyses


def choose_final_row(city_analysis: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [
        r for r in city_analysis
        if int(r.get("Decision Priority", 999)) < 999
    ]
    if not eligible:
        return None

    # Religious priority always comes first.
    best_priority = min(int(r.get("Decision Priority", 999)) for r in eligible)
    best = [
        r for r in eligible
        if int(r.get("Decision Priority", 999)) == best_priority
    ]

    # For "overlap then next Nishita", the EARLIEST qualifying night is
    # explicitly required.  Budhavara must not move the observance to a later
    # night.
    if best_priority == 20:
        best.sort(key=lambda r: clean_value(r.get("Date", "")))
        return best[0].copy()

    # The second-Nishita rule is already resolved before this function:
    # the chosen second Nishita gets priority 5 and all other overlap-Nishita
    # rows are made non-selectable.
    if best_priority == 5:
        best.sort(key=lambda r: clean_value(r.get("Date", "")))
        return best[0].copy()

    # For the remaining equal-priority edge/fallback cases, Budhavara remains
    # only a final tie-break because it is the least-important condition.
    best.sort(key=lambda r: (
        int(r.get("Decision Tie Break", 1)),
        clean_value(r.get("Date", "")),
    ))
    return best[0].copy()


def build_final_message(row: dict[str, Any]) -> str:
    date_str = clean_value(
        row.get("Upavaasa Date", "")
        or row.get("Festival Date", "")
        or row.get("Date", "")
    )
    nishita_text = clean_value(row.get("Calculated Nishita Madhya", ""))
    puja_start_text = clean_value(row.get("Calculated Puja Start", ""))
    puja_end_text = clean_value(row.get("Calculated Puja End", ""))

    # Parse our own normalized datetime strings for compact display.
    tz = ZoneInfo(normalize_timezone_name(clean_value(row.get("Timezone", ""))))

    def parse_our(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %I:%M:%S %p").replace(tzinfo=tz)
        except ValueError:
            return None

    nishita = parse_our(nishita_text)
    puja_start = parse_our(puja_start_text)
    puja_end = parse_our(puja_end_text)

    lines = [
        f"Sri Krishna Jayanthi Upavaasa - {format_display_date(date_str)}",
        "Upavaasa is to be observed on this day and Nishita Puja is performed during the following night.",
    ]

    if nishita:
        lines.append(f"Nishita Madhya - {format_clock(nishita)}, {nishita.strftime('%b %d')}")

    if puja_start and puja_end:
        if puja_start.date() == puja_end.date():
            lines.append(
                f"Nishita Puja Time - {format_clock(puja_start)} to {format_clock(puja_end)}, "
                f"{puja_end.strftime('%b %d')}"
            )
        else:
            lines.append(
                f"Nishita Puja Time - {format_clock(puja_start)}, {puja_start.strftime('%b %d')} "
                f"to {format_clock(puja_end)}, {puja_end.strftime('%b %d')}"
            )
        minutes = int((puja_end - puja_start).total_seconds() // 60)
        lines.append(f"Duration - {minutes // 60:02d} Hours {minutes % 60:02d} Mins")

    return " | ".join(lines)



def selected_run_label(city_names: list[str] | None) -> str:
    """Stable folder label for selected-city/backfill runs."""
    names = [
        re.sub(r"[^a-z0-9]+", "_", clean_value(name).lower()).strip("_")
        for name in (city_names or [])
        if clean_value(name)
    ]
    if not names:
        return "selected"
    if len(names) <= 4:
        return "_".join(names)
    return "_".join(names[:3]) + f"_plus_{len(names) - 3}"


# =============================================================================
# Main
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Independent Sri Krishna Jayanthi / Janmashtami scanner. "
            "Drik Month Panchang supplies raw Panchanga values; this program "
            "independently applies the configured Krishna Jayanthi rules."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  Full production run using an anchor and +/- 2 candidate days:\n"
            "    python festival_engines/krishna_jayanthi_engine.py "
            "--anchor 2026-09-04 --all-cities\n\n"
            "  Explicit candidate date range for all cities:\n"
            "    python festival_engines/krishna_jayanthi_engine.py "
            "--start 2026-09-02 --end 2026-09-06 --all-cities\n\n"
            "  Test selected cities only:\n"
            "    python festival_engines/krishna_jayanthi_engine.py "
            "--anchor 2026-09-04 --cities Pittsford Auckland Perth\n\n"
            "  Use a wider anchor window:\n"
            "    python festival_engines/krishna_jayanthi_engine.py "
            "--anchor 2026-09-04 --window-days 3 --all-cities"
        ),
    )

    date_group = parser.add_mutually_exclusive_group(required=True)
    date_group.add_argument(
        "--anchor",
        metavar="YYYY-MM-DD",
        help=(
            "Approximate Krishna Jayanthi date. Candidate dates are generated "
            "from --window-days before through --window-days after this date."
        ),
    )
    date_group.add_argument(
        "--start",
        metavar="YYYY-MM-DD",
        help="First explicit candidate observance date. Requires --end.",
    )
    parser.add_argument(
        "--end",
        metavar="YYYY-MM-DD",
        help="Last explicit candidate observance date. Used with --start.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=2,
        metavar="N",
        help="Days before/after --anchor to use as candidate dates (default: 2).",
    )

    city_group = parser.add_mutually_exclusive_group(required=True)
    city_group.add_argument(
        "--all-cities",
        action="store_true",
        help="Run every configured city in the city CSV.",
    )
    city_group.add_argument(
        "--cities",
        nargs="+",
        metavar="CITY",
        help='Run only the named display cities, e.g. --cities Pittsford "T Narasipura".',
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Optional safety/test limit applied after city selection.",
    )

    parser.add_argument(
        "--input-csv",
        default=INPUT_CSV,
        metavar="PATH",
        help=f"City configuration CSV (default: {INPUT_CSV}).",
    )
    parser.add_argument(
        "--month",
        default="",
        metavar="YYYY-MM",
        help="Optional logical run month used to standardize output folders under festival_runs/YYYY/MM/...",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        metavar="PATH",
        help="Optional exact output folder. If omitted, a standardized festival_runs/YYYY/MM/... path is used.",
    )
    parser.add_argument(
        "--cache",
        default="",
        metavar="PATH",
        help="Optional exact cache CSV path. If omitted, festival_runs/cache/krishna_jayanthi_scan_cache.csv is used.",
    )
    parser.add_argument(
        "--profile-dir",
        default=str(PLAYWRIGHT_PROFILE_DIR),
        metavar="PATH",
        help=f"Persistent Playwright profile folder (default: {PLAYWRIGHT_PROFILE_DIR}).",
    )

    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium headless.",
    )
    browser_group.add_argument(
        "--headed",
        action="store_true",
        help="Show Chromium window. This is the default and is useful for CAPTCHA handling.",
    )

    return parser.parse_args()


def resolve_candidate_range(args: argparse.Namespace) -> tuple[str, str]:
    if args.anchor:
        if args.end:
            raise ValueError("--end cannot be used together with --anchor")
        if args.window_days < 0:
            raise ValueError("--window-days must be 0 or greater")
        anchor = datetime.strptime(args.anchor, "%Y-%m-%d")
        start = anchor - timedelta(days=args.window_days)
        end = anchor + timedelta(days=args.window_days)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    if not args.start or not args.end:
        raise ValueError("Explicit range mode requires both --start and --end")

    # Validate formatting/order now so bad CLI input fails before the browser opens.
    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")
    if end < start:
        raise ValueError("--end must be on or after --start")
    return args.start, args.end


def main() -> None:
    global INPUT_CSV, OUTPUT_ROOT, OUTPUT_DIR, AUDIT_CSV, FINAL_CSV, MESSAGE_CSV, CACHE_CSV, ACTIVE_MONTH_ID
    global SCAN_START_DATE, SCAN_END_DATE, TEST_CITY_NAMES, TEST_CITY_LIMIT
    global HEADLESS, PLAYWRIGHT_PROFILE_DIR

    args = parse_args()
    SCAN_START_DATE, SCAN_END_DATE = resolve_candidate_range(args)

    INPUT_CSV = str(args.input_csv)

    if args.month:
        ACTIVE_MONTH_ID = args.month.strip()
    elif args.anchor:
        ACTIVE_MONTH_ID = args.anchor[:7]
    else:
        ACTIVE_MONTH_ID = SCAN_START_DATE[:7]

    if args.output_dir:
        OUTPUT_DIR = Path(args.output_dir)
    else:
        year, month = ACTIVE_MONTH_ID.split("-")
        base_output_dir = (
            OUTPUT_ROOT
            / year
            / month
            / "krishna_jayanthi"
            / EVENT_FOLDER_NAME
        )

        if args.cities:
            OUTPUT_DIR = (
                base_output_dir
                / "subset_runs"
                / selected_run_label(list(args.cities))
            )
        else:
            OUTPUT_DIR = base_output_dir

    AUDIT_CSV = OUTPUT_DIR / "krishna_jayanthi_audit.csv"
    FINAL_CSV = OUTPUT_DIR / "krishna_jayanthi_final.csv"
    MESSAGE_CSV = OUTPUT_DIR / "krishna_jayanthi_messages.csv"
    CACHE_CSV = Path(args.cache) if args.cache else OUTPUT_ROOT / "cache" / "krishna_jayanthi_scan_cache.csv"
    PLAYWRIGHT_PROFILE_DIR = Path(args.profile_dir)

    TEST_CITY_NAMES = list(args.cities or [])
    TEST_CITY_LIMIT = args.limit
    HEADLESS = bool(args.headless)
    if args.headed:
        HEADLESS = False

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_CSV.parent.mkdir(parents=True, exist_ok=True)

    print(
        "Run scope: "
        + ("SELECTED_CITIES_BACKFILL" if args.cities else "ALL_CITIES")
    )
    print(f"Output dir: {OUTPUT_DIR}")
    cache = ObservationCache(CACHE_CSV)

    cities = pd.read_csv(INPUT_CSV, sep=None, engine="python", encoding="utf-8-sig")
    cities.columns = cities.columns.str.strip()

    required = {"display_city", "search_city", "state_or_region", "country", "timezone"}
    missing = required - set(cities.columns)
    if missing:
        raise ValueError(f"Input CSV is missing required columns: {sorted(missing)}")

    original_city_count = len(cities)

    if TEST_CITY_NAMES:
        wanted = {clean_value(x).lower() for x in TEST_CITY_NAMES}
        available = {
            clean_value(x).lower(): clean_value(x)
            for x in cities["display_city"].fillna("").astype(str)
        }
        not_found = [name for name in TEST_CITY_NAMES if clean_value(name).lower() not in available]
        if not_found:
            raise ValueError(
                "Requested city/cities were not found in display_city: "
                + ", ".join(not_found)
            )
        cities = cities[
            cities["display_city"].fillna("").astype(str).map(
                lambda x: clean_value(x).lower()
            ).isin(wanted)
        ].copy()

    if TEST_CITY_LIMIT is not None:
        if TEST_CITY_LIMIT < 1:
            raise ValueError("--limit must be at least 1")
        cities = cities.head(TEST_CITY_LIMIT).copy()

    if cities.empty:
        raise ValueError("No cities remain after applying CLI selection")

    candidate_dates, scan_dates = get_scan_dates_with_lookup(SCAN_START_DATE, SCAN_END_DATE)
    candidate_set = set(candidate_dates)

    print("=" * 100)
    print("SRI KRISHNA JAYANTHI SCANNER")
    print("=" * 100)
    if args.anchor:
        print(f"Anchor: {args.anchor} | window: +/- {args.window_days} day(s)")
    print(f"Candidate range: {SCAN_START_DATE} through {SCAN_END_DATE}")
    print("Candidate observance dates:", ", ".join(candidate_dates))
    print("Actual scan dates (includes lookup day on each side):", ", ".join(scan_dates))
    if args.all_cities:
        print(f"Cities: ALL ({len(cities)} of {original_city_count})")
    else:
        print("Cities:", ", ".join(cities["display_city"].astype(str).tolist()))
    print(f"Output directory: {OUTPUT_DIR}")
    print(f"Browser: {'headless' if HEADLESS else 'headed'}")

    all_audit_rows: list[dict[str, Any]] = []
    final_rows: list[dict[str, Any]] = []

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(PLAYWRIGHT_PROFILE_DIR),
            headless=HEADLESS,
            viewport={"width": 1500, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            for input_order, (_, city_row) in enumerate(cities.iterrows(), start=1):
                city_name = clean_value(city_row.get("display_city", ""))
                print("\n" + "=" * 100)
                print(f"KRISHNA JAYANTHI SCAN: {city_name}")
                print("=" * 100)

                raw_records = scan_city(
                    page=page,
                    row=city_row,
                    scan_dates=scan_dates,
                    candidate_dates=candidate_set,
                    input_order=input_order,
                    cache=cache,
                )

                city_analysis = analyze_city_records(raw_records, candidate_dates)
                all_audit_rows.extend(city_analysis)

                selected = choose_final_row(city_analysis)
                if selected:
                    selected["Festival"] = "Sri Krishna Jayanthi"
                    selected["Festival Date"] = selected.get("Date", "")
                    selected["Upavaasa Date"] = selected.get("Date", "")
                    selected["Final Message"] = build_final_message(selected)
                    final_rows.append(selected)
                    print(
                        f"SELECTED: {city_name} -> {selected.get('Date')} | "
                        f"{selected.get('Decision Rule')}"
                    )
                else:
                    print(f"NO AUTOMATIC SELECTION for {city_name}. Check audit CSV.")

                if not page.is_closed():
                    page.wait_for_timeout(BETWEEN_CITIES_MS)
        finally:
            context.close()

    audit_df = pd.DataFrame(all_audit_rows)
    if not audit_df.empty:
        audit_df = audit_df.sort_values(["Input Order", "Date"], kind="stable")
    audit_df.to_csv(AUDIT_CSV, index=False, encoding="utf-8-sig")

    final_df = pd.DataFrame(final_rows)
    if not final_df.empty:
        preferred_final_columns = [
            "Festival Date", "Upavaasa Date",
            "City", "State/Region", "Country", "Timezone", "Geoname ID",
            "Festival", "Sunsign", "Amanta Maasa", "Paksha", "Vaara",
            "Ashtami Start", "Ashtami End", "Rohini Start", "Rohini End",
            "Ashtami-Rohini Overlap Start", "Ashtami-Rohini Overlap End",
            "Candidate Sunset", "Next Sunrise", "Calculated Nishita Madhya",
            "Calculated Puja Start", "Calculated Puja End",
            "Qualifying Nishita Count In Joint Overlap",
            "Qualifying Nishita Sequence",
            "Nishita Selection Note",
            "Saptami Viddha Ashtami",
            "No-Overlap Case",
            "No-Overlap Rule",
            "Decision Rule", "Budhavara", "Needs Review", "Review Reason",
            "Final Message",
        ]
        for col in preferred_final_columns:
            if col not in final_df.columns:
                final_df[col] = ""
        final_df = final_df[preferred_final_columns].sort_values(
            ["Festival Date", "Timezone", "Country", "State/Region", "City"],
            kind="stable",
        )
    final_df.to_csv(FINAL_CSV, index=False, encoding="utf-8-sig")

    message_rows = [build_krishna_message_row(row) for row in final_rows]
    messages_df = pd.DataFrame(message_rows)
    if not messages_df.empty:
        for col in MESSAGE_COLUMNS:
            if col not in messages_df.columns:
                messages_df[col] = ""
        messages_df = messages_df[MESSAGE_COLUMNS].sort_values(
            ["Date", "Timezone", "Country", "State/Region", "City"],
            kind="stable",
        )
    messages_df.to_csv(MESSAGE_CSV, index=False, encoding="utf-8-sig")
    cache.save()

    print("\nDone.")
    print(f"Audit CSV   : {AUDIT_CSV}")
    print(f"Final CSV   : {FINAL_CSV}")
    print(f"Messages CSV: {MESSAGE_CSV}")
    print(f"Cache CSV   : {CACHE_CSV}")
    print(f"Cache hits this run : {cache.cache_hits}")
    print(f"Fresh page scans    : {cache.fresh_scans}")


if __name__ == "__main__":
    main()
