from __future__ import annotations

"""
Independent Ekadashi Cycle Scanner - v3.1 Standard Festival Runs
=========================================

Purpose
-------
Process ONE Ekadashi lunar cycle at a time across all configured cities.
The supplied anchor date is only a search anchor; it never decides the
Ekadashi/Upavaasa date.

The scanner independently reconstructs:
    Dashami end   = Ekadashi start
    Ekadashi end  = Dwadashi start
    Dwadashi end  = Trayodashi start

It then classifies the cycle using local sunrise/Arunodaya/Nakshatra data:
    - Normal Ekadashi
    - Dashami Viddha Ekadashi
    - Missing Ekadashi
    - Shravanopavasa
    - Dwadashi Dwaya
    - Ekadashi Dwaya

Arunodaya = local sunrise - 96 minutes.

Efficiency design
-----------------
* Scan the anchor first.
* Cache every useful field from each city/date page.
* Never rescan a city/date already present in the persistent cache.
* Fetch another date only when a specific required transition/sunrise is missing.
* Stop only when the city is COMPLETE or a hard safety limit is reached.

Outputs
-------
Canonical full-cycle outputs follow the common festival_runs architecture:

    festival_runs/YYYY/MM/ekadashi/<cycle_slug>/
        <cycle_slug>_audit.csv
        <cycle_slug>_messages.csv

The shared raw Panchanga cache is:

    festival_runs/cache/ekadashi_scan_cache.csv

1) <cycle_slug>_audit.csv
   One row per city with evidence, classification, Upavaasa/Parana dates,
   completeness status, and missing-parameter diagnostics.

2) <cycle_slug>_messages.csv
   One row per city + relevant civil date. This is intended for the main
   Panchanga scanner to consume directly. Two-day scenarios create Day-1,
   Day-2, and Parana rows.

3) festival_runs/cache/ekadashi_scan_cache.csv
   Persistent city/date Panchanga cache reused across reruns and later cycles.

Important
---------
This script does NOT use Drik's Ekadashi classification or Drik's Parana result.
Drik Month Panchang is used only as the source of raw local Panchanga values.

v2.4-v2.9 corrections
----------------
1) Dashami Viddha is evaluated PER EKADASHI SUNRISE, not as a cycle-wide
   conclusion. If the first Ekadashi sunrise is Viddha but Ekadashi survives
   to a later sunrise where it is already present before Arunodaya, that later
   sunrise is a valid Ekadashi Upavaasa day. Only when no later valid Ekadashi
   sunrise exists does Upavaasa shift to Dwadashi.

2) For Missing Ekadashi, true Dashami Viddha, Dwadashi Dwaya and
   Shravanopavasa, where Upavaasa is observed on Dwadashi (or includes
   Dwadashi), Parana on the following morning starts at sunrise. If Dwadashi
   is still present at sunrise, the end is min(Dwadashi end, Pratah end). If
   Dwadashi ended before sunrise, Parana is sunrise to Pratah end.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import re
import argparse

import pandas as pd
from playwright.sync_api import BrowserContext, Page, sync_playwright


# =============================================================================
# USER CONFIGURATION
# =============================================================================

# Same city file used by the main scanner.
INPUT_CSV = "cities_panchanga_updated.csv"

# ONE cycle at a time. The date is only an approximate global anchor.
CYCLE_NAME = "Shravana Putrada Ekadashi"
ANCHOR_DATE = "2026-08-23"

# Initial testing. Keep 5 for the first smoke test, then set to None.
TEST_CITY_LIMIT: int | None = None

# Optional exact display_city filter. Example:
# TEST_CITY_NAMES = ["Pittsford", "Auckland", "Perth", "Bengaluru", "London"]
# IMPORTANT v2.9 behavior: whenever TEST_CITY_NAMES is non-empty or
# TEST_CITY_LIMIT is set, outputs are written to a separate subset_runs folder.
# The canonical full-cycle audit/messages files are never overwritten by a
# partial-city run. This is ideal for adding a newly requested city later.
TEST_CITY_NAMES: list[str] = []

# Optional readable label for a partial-city run. Leave blank for automatic.
SUBSET_RUN_LABEL = ""

HEADLESS = False

# Maximum distance the dynamic resolver may move from the anchor in either
# direction. It normally needs only ~3-5 dates per city. This is a safety cap,
# not a pre-scanned window.
MAX_DYNAMIC_DISTANCE_DAYS = 6
MAX_FRESH_SCANS_PER_CITY = 10

# Browser pacing.
PAGE_LOAD_WAIT_MS = 8000
AFTER_CITY_WAIT_MS = 6000
AFTER_DATE_WAIT_MS = 6000
BETWEEN_FRESH_SCANS_MS = 1800
CAPTCHA_SAFE_WAIT_MS = 2500

PLAYWRIGHT_PROFILE_DIR = Path("playwright_profile")

# Common production artifact root used by all festival engines.
OUTPUT_ROOT = Path("festival_runs")
CACHE_DIR = OUTPUT_ROOT / "cache"
CACHE_CSV = CACHE_DIR / "ekadashi_scan_cache.csv"
CACHE_SCHEMA_VERSION = "2.0"

# If True, write the cache after every fresh page scan so an interrupted run
# can resume without repeating completed city/date requests.
SAVE_CACHE_AFTER_EACH_SCAN = True

MONTH_PANCHANG_URL = "https://www.drikpanchang.com/panchang/month-panchang.html"


# =============================================================================
# CONSTANTS / BASIC HELPERS
# =============================================================================

TIMEZONE_NAME_ALIASES = {
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
    "Asia/Calcutta": "Asia/Kolkata",
}

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
    "tirupati": ["Tirupati", "Tirupathi"],
    "tirupathi": ["Tirupati", "Tirupathi"],
    "prayagraj": ["Prayagraj", "Allahabad"],
    "allahabad": ["Prayagraj", "Allahabad"],
}

TITHI_NAMES = [
    "Pratipada", "Dwitiya", "Tritiya", "Chaturthi", "Panchami", "Shashthi",
    "Saptami", "Ashtami", "Navami", "Dashami", "Ekadashi", "Dwadashi",
    "Trayodashi", "Chaturdashi", "Purnima", "Amavasya",
]

TITHI_ORDER = {
    "Navami": 9,
    "Dashami": 10,
    "Ekadashi": 11,
    "Dwadashi": 12,
    "Trayodashi": 13,
    "Chaturdashi": 14,
}

# Canonical Nakshatra names used as a validation whitelist.  Drik pages also
# contain navigation items such as "Nakshatra compatibility report"; those
# must never be mistaken for the Panchanga Nakshatra at sunrise.
NAKSHATRA_ALIASES = {
    "ashwini": "Ashwini",
    "aswini": "Ashwini",
    "bharani": "Bharani",
    "krittika": "Krittika",
    "kritika": "Krittika",
    "rohini": "Rohini",
    "mrigashirsha": "Mrigashirsha",
    "mrigashira": "Mrigashirsha",
    "ardra": "Ardra",
    "aardra": "Ardra",
    "punarvasu": "Punarvasu",
    "pushya": "Pushya",
    "ashlesha": "Ashlesha",
    "aslesha": "Ashlesha",
    "magha": "Magha",
    "purvaphalguni": "Purva Phalguni",
    "poorvaphalguni": "Purva Phalguni",
    "uttaraphalguni": "Uttara Phalguni",
    "hasta": "Hasta",
    "chitra": "Chitra",
    "swati": "Swati",
    "svati": "Swati",
    "vishakha": "Vishakha",
    "visakha": "Vishakha",
    "anuradha": "Anuradha",
    "jyeshtha": "Jyeshtha",
    "jyestha": "Jyeshtha",
    "mula": "Mula",
    "moola": "Mula",
    "purvaashadha": "Purva Ashadha",
    "poorvaashadha": "Purva Ashadha",
    "uttaraashadha": "Uttara Ashadha",
    "shravana": "Shravana",
    "sravana": "Shravana",
    "dhanishtha": "Dhanishtha",
    "dhanishta": "Dhanishtha",
    "shatabhisha": "Shatabhisha",
    "satabhisha": "Shatabhisha",
    "purvabhadrapada": "Purva Bhadrapada",
    "poorvabhadrapada": "Purva Bhadrapada",
    "uttarabhadrapada": "Uttara Bhadrapada",
    "revati": "Revati",
}

NAVIGATION_NOISE = {
    "list", "names", "name", "more", "details", "today", "calendar",
    "analysis report",
}

BAD_EXTRACTED_SUFFIXES = [
    "Calculation Service", "Calculation Services", "Calendar Service",
    "Calendar Services", "Panchang Calculation", "Panchang Calculations",
]


def clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return re.sub(r"\s+", " ", str(value)).strip()


def clean_value(value: Any) -> str:
    value = clean(value)
    for suffix in BAD_EXTRACTED_SUFFIXES:
        value = re.sub(
            rf"(;\s*)?{re.escape(suffix)}\s*$", "", value,
            flags=re.IGNORECASE,
        ).strip()
    return value


def slugify(value: str) -> str:
    value = clean(value).lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value or "ekadashi_cycle"


def normalize_timezone_name(value: str) -> str:
    value = clean(value)
    return TIMEZONE_NAME_ALIASES.get(value, value)


def normalize_lines(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in str(text).splitlines()
        if line and line.strip()
    ]


def is_noise_value(value: str) -> bool:
    return clean(value).lower() in NAVIGATION_NOISE


def yyyy_mm_dd_to_dd_mm_yyyy(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")


def date_add(value: str, days: int) -> str:
    return (
        datetime.strptime(value, "%Y-%m-%d") + timedelta(days=days)
    ).strftime("%Y-%m-%d")


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %I:%M:%S %p %Z")


def fmt_date(value: str | date | datetime | None) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    else:
        d = datetime.strptime(str(value), "%Y-%m-%d").date()
    return d.strftime("%d-%b-%y")


def round_to_minute(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.second >= 30:
        value = value + timedelta(minutes=1)
    return value.replace(second=0, microsecond=0)


def fmt_public_time(value: datetime | None) -> str:
    value = round_to_minute(value)
    if value is None:
        return ""
    return value.strftime("%I:%M %p")


def tithi_name_from_text(text: str) -> str:
    value = clean(text)
    for name in TITHI_NAMES:
        if re.search(rf"\b{re.escape(name)}\b", value, flags=re.IGNORECASE):
            return name
    return ""


def split_segments(value: str) -> list[str]:
    return [
        clean(x) for x in re.split(r"\s*;\s*|\s*\|\|\s*", clean(value))
        if clean(x)
    ]


def sunrise_tithi_name(tithi_text: str) -> str:
    segments = split_segments(tithi_text)
    return tithi_name_from_text(segments[0]) if segments else ""


def canonical_nakshatra_name(value: str) -> str:
    """Return a canonical Nakshatra name, or empty string for non-Nakshatra text."""
    cleaned = clean(value)
    if not cleaned:
        return ""
    # Remove the timing suffix first.  Then normalize spaces/punctuation so
    # variants such as "Purva Phalguni" and "Poorva Phalguni" can be matched.
    cleaned = re.split(r"\bupto\b", cleaned, maxsplit=1, flags=re.IGNORECASE)[0]
    key = re.sub(r"[^a-z]", "", cleaned.lower())
    if not key:
        return ""

    # Prefer an exact match.  A startswith fallback safely handles occasional
    # suffix text while still rejecting navigation text like
    # "compatibility report".
    if key in NAKSHATRA_ALIASES:
        return NAKSHATRA_ALIASES[key]
    for alias, canonical in sorted(
        NAKSHATRA_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if key.startswith(alias):
            return canonical
    return ""


def sunrise_nakshatra_name(nakshatra_text: str) -> str:
    # Drik can expose multiple "Nakshatra ..." lines in page text.  Select the
    # first segment that is actually a recognized Nakshatra rather than blindly
    # accepting the first "Nakshatra" navigation entry.
    for segment in split_segments(nakshatra_text):
        canonical = canonical_nakshatra_name(segment)
        if canonical:
            return canonical
    return ""


def is_valid_nakshatra(value: str) -> bool:
    return bool(canonical_nakshatra_name(value))


def is_shravana(value: str) -> bool:
    return canonical_nakshatra_name(value) == "Shravana"


def get_place_key_from_values(
    geoname_id: str,
    city: str,
    state: str,
    country: str,
    timezone: str,
) -> str:
    if clean(geoname_id):
        return f"geoname:{clean(geoname_id)}"
    raw = "|".join([
        clean(city).lower(), clean(state).lower(), clean(country).lower(),
        clean(timezone),
    ])
    return re.sub(r"[^a-z0-9:+|_/-]+", "_", raw)


def get_geoname_id(row: pd.Series) -> str:
    raw = clean(row.get("geoname_id", ""))
    if not raw:
        raw = clean(row.get("Geoname ID", ""))
    if raw.endswith(".0") and raw[:-2].isdigit():
        raw = raw[:-2]
    return raw


def get_place_key_for_row(row: pd.Series) -> str:
    return get_place_key_from_values(
        get_geoname_id(row),
        clean(row.get("display_city", "")),
        clean(row.get("state_or_region", "")),
        clean(row.get("country", "")),
        normalize_timezone_name(clean(row.get("timezone", ""))),
    )


# =============================================================================
# DRIK NAVIGATION / EXTRACTION
# =============================================================================


def build_panchang_url(base_url: str, geoname_id: str, date_str: str) -> str:
    return (
        f"{base_url}?geoname-id={clean(geoname_id)}"
        f"&date={yyyy_mm_dd_to_dd_mm_yyyy(date_str)}"
    )


def captcha_is_present(page: Page) -> bool:
    if page.is_closed():
        return False
    try:
        title = clean(page.title()).lower()
    except Exception:
        title = ""
    try:
        body = clean(page.locator("body").inner_text(timeout=5000)).lower()
    except Exception:
        body = ""
    phrases = [
        "verify you are human", "verification", "checking your browser",
        "security verification", "captcha", "cloudflare",
    ]
    return any(p in title or p in body for p in phrases)


def wait_for_manual_captcha(page: Page, reason: str = "") -> None:
    if page.is_closed() or not captcha_is_present(page):
        return
    print("\n" + "!" * 92)
    print("DRIK PANCHANG VERIFICATION DETECTED")
    if reason:
        print(f"Reason/context: {reason}")
    print("Complete the verification in the browser, then press Enter here.")
    print("!" * 92)
    while True:
        try:
            input()
        except EOFError:
            page.wait_for_timeout(10000)
        if not captcha_is_present(page):
            page.wait_for_timeout(CAPTCHA_SAFE_WAIT_MS)
            print("Verification cleared. Resuming.\n")
            return
        print("Verification still appears present. Complete it and press Enter again.")


def validate_direct_geoname_page(
    page: Page,
    expected_geoname_id: str,
    display_city: str,
    date_str: str,
) -> None:
    expected = clean(expected_geoname_id)
    if not expected:
        return
    current = page.url
    if f"geoname-id={expected}" not in current:
        raise RuntimeError(
            f"Expected geoname-id={expected} for {display_city} on {date_str}, "
            f"but loaded {current}"
        )


def open_panchang_url(
    page: Page,
    url: str,
    reason: str,
    expected_geoname_id: str = "",
    display_city: str = "",
    date_str: str = "",
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
            wait_for_manual_captcha(page, reason=reason)
            if expected_geoname_id:
                validate_direct_geoname_page(
                    page, expected_geoname_id, display_city, date_str
                )
            return
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                print(f"Navigation retry {attempt + 1}/3 for {display_city} {date_str}")
                page.wait_for_timeout(5000)
    raise RuntimeError(f"Could not open Drik Panchang URL: {url}") from last_error


def ensure_page_at_url(page: Page, url: str) -> None:
    if not page.url.startswith(url):
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
        wait_for_manual_captcha(page, reason=f"opening {url}")


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
    wait_for_manual_captcha(page, reason="waiting for input field")
    neutralize_ad_overlays(page)
    locator.wait_for(state="visible", timeout=30000)
    locator.evaluate(
        """
        element => {
            element.focus(); element.value='';
            element.dispatchEvent(new Event('input',{bubbles:true}));
            element.dispatchEvent(new Event('change',{bubbles:true}));
        }
        """
    )


def normalize_location_piece(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def get_city_aliases(search_city: str) -> list[str]:
    aliases = CITY_NAME_ALIASES.get(clean(search_city).lower(), [clean(search_city)])
    return list(dict.fromkeys(aliases))


def set_city(page: Page, search_city: str, state_or_region: str, country: str) -> str:
    """Fallback only. Direct geoname navigation is preferred."""
    aliases = get_city_aliases(search_city)
    queries: list[str] = []
    for alias in aliases:
        queries.extend([
            alias,
            f"{alias} {state_or_region}" if state_or_region else "",
            f"{alias} {state_or_region} {country}" if state_or_region and country else "",
            f"{alias} {country}" if country else "",
        ])
    queries = [q for q in dict.fromkeys(queries) if q]

    for query in queries:
        print(f"Trying city query: {query}")
        inp = page.locator("#dp-direct-city-search")
        focus_and_clear_input(page, inp)
        page.keyboard.type(query, delay=180)
        page.wait_for_timeout(3500)
        suggestions = page.locator("ul.ui-autocomplete li:visible")
        count = suggestions.count()
        candidates: list[tuple[int, int, str]] = []
        aliases_norm = {normalize_location_piece(x) for x in aliases}
        country_norm = normalize_location_piece(country)
        for i in range(count):
            try:
                txt = suggestions.nth(i).inner_text().strip()
            except Exception:
                continue
            lines = [x.strip() for x in txt.splitlines() if x.strip()]
            if not lines:
                continue
            if normalize_location_piece(lines[0]) not in aliases_norm:
                continue
            score = 100
            normalized = normalize_location_piece(txt)
            if state_or_region and normalize_location_piece(state_or_region) in normalized:
                score += 30
            if country_norm and country_norm in normalized:
                score += 20
            candidates.append((score, i, txt))
        if not candidates:
            continue
        candidates.sort(reverse=True)
        _, idx, txt = candidates[0]
        print(f"Selected city suggestion: {txt}")
        target = suggestions.nth(idx)
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
        wait_for_manual_captcha(page, reason=f"selecting {search_city}")
        try:
            selected = page.locator("#dp-direct-city-search").input_value().strip()
        except Exception:
            selected = txt
        if normalize_location_piece(selected.split(",")[0]) in aliases_norm:
            return selected
    raise RuntimeError(
        f"Could not select fallback city {search_city}, {state_or_region}, {country}"
    )


def find_date_input(page: Page):
    preferred = page.locator("#dp-date-picker")
    if preferred.count() > 0:
        return preferred.first
    inputs = page.locator("input")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        try:
            value = el.input_value()
        except Exception:
            continue
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", value or ""):
            return el
    raise RuntimeError("Could not find Drik Panchang date input")


def set_date(page: Page, date_str: str) -> None:
    inp = find_date_input(page)
    requested = yyyy_mm_dd_to_dd_mm_yyyy(date_str)
    if inp.input_value() == requested:
        return
    focus_and_clear_input(page, inp)
    page.keyboard.type(requested, delay=70)
    page.keyboard.press("Enter")
    page.wait_for_timeout(AFTER_DATE_WAIT_MS)
    wait_for_manual_captcha(page, reason=f"changing date to {date_str}")
    actual = find_date_input(page).input_value()
    if actual != requested:
        raise RuntimeError(f"Date did not update: expected {requested}, got {actual}")


def extract_line_value(text: str, labels: list[str]) -> str:
    lines = normalize_lines(text)
    for line in lines:
        for label in labels:
            m = re.match(rf"^{re.escape(label)}\s*:?\s*(.+)$", line, flags=re.IGNORECASE)
            if m:
                value = clean_value(m.group(1))
                if value and not is_noise_value(value):
                    return value
    return ""


def extract_all_line_values(text: str, labels: list[str]) -> list[str]:
    values: list[str] = []
    for line in normalize_lines(text):
        for label in labels:
            m = re.match(rf"^{re.escape(label)}\s*:?\s*(.+)$", line, flags=re.IGNORECASE)
            if m:
                value = clean_value(m.group(1))
                if value and not is_noise_value(value) and value not in values:
                    values.append(value)
                break
    return values


def extract_month_page_values(text: str) -> dict[str, str]:
    tithis = extract_all_line_values(text, ["Tithi"])
    nakshatras = extract_all_line_values(text, ["Nakshatra"])

    # Keep only actual Nakshatra values when the page also exposes navigation
    # text such as "Nakshatra compatibility report".  If no valid value is
    # found, retain the raw candidates for diagnostics; Sunrise Nakshatra will
    # remain blank and the completeness gate will reject the city.
    valid_nakshatras = [
        value for value in nakshatras if sunrise_nakshatra_name(value)
    ]
    nakshatra_value = "; ".join(valid_nakshatras or nakshatras)

    return {
        "Amanta Maasa": extract_line_value(text, ["Amanta Month", "Amanta Maasa"]),
        "Paksha": extract_line_value(text, ["Paksha"]),
        "Tithi": "; ".join(tithis),
        "Vaara": extract_line_value(text, ["Weekday", "Vaara"]),
        "Nakshatra": nakshatra_value,
        "Sunrise": extract_line_value(text, ["Sunrise"]),
        "Sunset": extract_line_value(text, ["Sunset"]),
    }


def parse_time_on_date(time_text: str, date_str: str, timezone_str: str) -> datetime | None:
    value = clean(time_text)
    if not value:
        return None
    value = re.sub(
        r"\b(EDT|EST|CDT|CST|PDT|PST|MDT|MST|IST|AEDT|AEST|ACDT|ACST|AWST|NZDT|NZST|BST|GMT)\b",
        "", value, flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", " ", value).strip(" ,")
    tz = ZoneInfo(normalize_timezone_name(timezone_str))
    for fmt in ["%I:%M:%S %p", "%I:%M %p"]:
        try:
            parsed_time = datetime.strptime(value, fmt).time()
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            return datetime.combine(d, parsed_time, tzinfo=tz)
        except ValueError:
            pass
    return None


def parse_drik_datetime(value: str, timezone_str: str) -> datetime | None:
    value = clean(value)
    if not value:
        return None
    value = re.sub(
        r"\b(EDT|EST|CDT|CST|PDT|PST|MDT|MST|IST|AEDT|AEST|ACDT|ACST|AWST|NZDT|NZST|BST|GMT)\b",
        "", value, flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", " ", value).strip(" ,")
    value = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", value, flags=re.IGNORECASE)
    tz = ZoneInfo(normalize_timezone_name(timezone_str))
    patterns = [
        "%I:%M:%S %p on %b %d, %Y", "%I:%M %p on %b %d, %Y",
        "%I:%M:%S %p on %B %d, %Y", "%I:%M %p on %B %d, %Y",
        "%I:%M:%S %p, %b %d, %Y", "%I:%M %p, %b %d, %Y",
        "%I:%M:%S %p, %B %d, %Y", "%I:%M %p, %B %d, %Y",
    ]
    for fmt in patterns:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    return None


def parse_tithi_upto_datetime(tithi_text: str, date_str: str, timezone_str: str) -> datetime | None:
    value = clean_value(tithi_text)
    if not value or re.search(r"\bupto\s+full\s+night\b", value, flags=re.IGNORECASE):
        return None
    m = re.search(r"\bupto\s+([^;|]+)", value, flags=re.IGNORECASE)
    if not m:
        return None
    upto = clean(m.group(1))
    upto = re.sub(
        r"\b(EDT|EST|CDT|CST|PDT|PST|MDT|MST|IST|AEDT|AEST|ACDT|ACST|AWST|NZDT|NZST|BST|GMT)\b",
        "", upto, flags=re.IGNORECASE,
    )
    upto = re.sub(r"\s+", " ", upto).strip(" ,")
    base_date = datetime.strptime(date_str, "%Y-%m-%d")

    dated = re.match(
        r"^(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M),\s*([A-Za-z]{3,9})\s+(\d{1,2})$",
        upto, flags=re.IGNORECASE,
    )
    if dated:
        time_part, month_part, day_part = dated.groups()
        candidate = f"{time_part} on {month_part} {day_part}, {base_date.year}"
        parsed = parse_drik_datetime(candidate, timezone_str)
        if parsed and parsed.date() < (base_date.date() - timedelta(days=30)):
            candidate = f"{time_part} on {month_part} {day_part}, {base_date.year + 1}"
            parsed = parse_drik_datetime(candidate, timezone_str)
        return parsed

    return parse_time_on_date(upto, date_str, timezone_str)


# =============================================================================
# PERSISTENT RAW-CACHE
# =============================================================================

CACHE_COLUMNS = [
    "Cache Schema Version", "Place Key", "Date", "City", "State/Region",
    "Country", "Timezone", "Geoname ID", "Selected Drik Location",
    "Tithi", "Sunrise Tithi", "Nakshatra", "Sunrise Nakshatra",
    "Sunrise", "Sunset", "Arunodaya", "Paksha", "Amanta Maasa", "Vaara",
    "Month Page URL", "Cached At",
]


class ObservationCache:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
            if "Cache Schema Version" not in df.columns:
                df = pd.DataFrame(columns=CACHE_COLUMNS)
            else:
                df = df[df["Cache Schema Version"].astype(str) == CACHE_SCHEMA_VERSION].copy()
        else:
            df = pd.DataFrame(columns=CACHE_COLUMNS)
        for col in CACHE_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        self.df = df[CACHE_COLUMNS].copy()
        self._reindex()
        self.cache_hits = 0
        self.fresh_scans = 0

    def _reindex(self) -> None:
        self.index: dict[tuple[str, str], dict[str, Any]] = {}
        for _, row in self.df.iterrows():
            self.index[(clean(row["Place Key"]), clean(row["Date"]))] = row.to_dict()

    def get(self, place_key: str, date_str: str) -> dict[str, Any] | None:
        row = self.index.get((clean(place_key), clean(date_str)))
        if not row:
            return None
        required = ["Tithi", "Sunrise", "Sunset"]
        if not all(clean(row.get(x, "")) for x in required):
            return None
        # A stale/bad Nakshatra extraction must not be reused.  Returning None
        # here refreshes only this city/date on the next run while preserving
        # every other valid cache row.
        if not is_valid_nakshatra(clean(row.get("Sunrise Nakshatra", ""))):
            return None
        self.cache_hits += 1
        return dict(row)

    def put(self, record: dict[str, Any]) -> None:
        record = {**{c: "" for c in CACHE_COLUMNS}, **record}
        record["Cache Schema Version"] = CACHE_SCHEMA_VERSION
        record["Cached At"] = datetime.now().isoformat(timespec="seconds")
        key = (clean(record["Place Key"]), clean(record["Date"]))
        self.index[key] = record
        self.fresh_scans += 1
        self.df = pd.DataFrame(list(self.index.values()))
        for col in CACHE_COLUMNS:
            if col not in self.df.columns:
                self.df[col] = ""
        self.df = self.df[CACHE_COLUMNS]
        if SAVE_CACHE_AFTER_EACH_SCAN:
            self.save()

    def save(self) -> None:
        self.df.sort_values(["Place Key", "Date"], kind="stable").to_csv(
            self.path, index=False, encoding="utf-8-sig"
        )


@dataclass
class CitySession:
    row: pd.Series
    page: Page
    cache: ObservationCache
    fallback_city_selected: bool = False
    observations: dict[str, dict[str, Any]] = field(default_factory=dict)
    fresh_scan_count: int = 0

    @property
    def city(self) -> str:
        return clean(self.row.get("display_city", ""))

    @property
    def state(self) -> str:
        return clean(self.row.get("state_or_region", ""))

    @property
    def country(self) -> str:
        return clean(self.row.get("country", ""))

    @property
    def timezone(self) -> str:
        return normalize_timezone_name(clean(self.row.get("timezone", "")))

    @property
    def geoname_id(self) -> str:
        return get_geoname_id(self.row)

    @property
    def place_key(self) -> str:
        return get_place_key_for_row(self.row)

    def get_observation(self, date_str: str) -> dict[str, Any]:
        if date_str in self.observations:
            return self.observations[date_str]

        cached = self.cache.get(self.place_key, date_str)
        if cached is not None:
            self.observations[date_str] = cached
            print(f"  CACHE {self.city}: {date_str} -> {cached.get('Sunrise Tithi','')}")
            return cached

        anchor = datetime.strptime(ANCHOR_DATE, "%Y-%m-%d").date()
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        if abs((target - anchor).days) > MAX_DYNAMIC_DISTANCE_DAYS:
            raise RuntimeError(
                f"Dynamic scan safety limit reached: requested {date_str}, "
                f"more than {MAX_DYNAMIC_DISTANCE_DAYS} days from anchor {ANCHOR_DATE}."
            )
        if self.fresh_scan_count >= MAX_FRESH_SCANS_PER_CITY:
            raise RuntimeError(
                f"Fresh-scan safety limit reached ({MAX_FRESH_SCANS_PER_CITY}) for {self.city}."
            )

        print(f"  SCAN  {self.city}: {date_str}")
        if self.geoname_id:
            url = build_panchang_url(MONTH_PANCHANG_URL, self.geoname_id, date_str)
            open_panchang_url(
                page=self.page,
                url=url,
                reason=f"Ekadashi cycle scan for {self.city} on {date_str}",
                expected_geoname_id=self.geoname_id,
                display_city=self.city,
                date_str=date_str,
            )
            selected_location = self.city
        else:
            ensure_page_at_url(self.page, MONTH_PANCHANG_URL)
            if not self.fallback_city_selected:
                selected_location = set_city(
                    self.page,
                    clean(self.row.get("search_city", self.city)),
                    self.state,
                    self.country,
                )
                self.fallback_city_selected = True
            else:
                try:
                    selected_location = self.page.locator("#dp-direct-city-search").input_value().strip()
                except Exception:
                    selected_location = self.city
            set_date(self.page, date_str)

        page_text = self.page.locator("body").inner_text(timeout=30000)
        values = extract_month_page_values(page_text)
        sunrise_dt = parse_time_on_date(values["Sunrise"], date_str, self.timezone)
        arunodaya_dt = sunrise_dt - timedelta(minutes=96) if sunrise_dt else None

        record = {
            "Cache Schema Version": CACHE_SCHEMA_VERSION,
            "Place Key": self.place_key,
            "Date": date_str,
            "City": self.city,
            "State/Region": self.state,
            "Country": self.country,
            "Timezone": self.timezone,
            "Geoname ID": self.geoname_id,
            "Selected Drik Location": selected_location,
            "Tithi": clean(values["Tithi"]),
            "Sunrise Tithi": sunrise_tithi_name(values["Tithi"]),
            "Nakshatra": clean(values["Nakshatra"]),
            "Sunrise Nakshatra": sunrise_nakshatra_name(values["Nakshatra"]),
            "Sunrise": clean(values["Sunrise"]),
            "Sunset": clean(values["Sunset"]),
            "Arunodaya": fmt_dt(arunodaya_dt),
            "Paksha": clean(values["Paksha"]),
            "Amanta Maasa": clean(values["Amanta Maasa"]),
            "Vaara": clean(values["Vaara"]),
            "Month Page URL": self.page.url,
        }
        self.cache.put(record)
        self.observations[date_str] = record
        self.fresh_scan_count += 1
        self.page.wait_for_timeout(BETWEEN_FRESH_SCANS_MS)
        return record


# =============================================================================
# TRANSITION RECONSTRUCTION / DYNAMIC RESOLUTION
# =============================================================================


def find_tithi_end(
    observations: dict[str, dict[str, Any]],
    target_tithi: str,
    timezone: str,
) -> datetime | None:
    candidates: list[datetime] = []
    for date_str, obs in sorted(observations.items()):
        for segment in split_segments(clean(obs.get("Tithi", ""))):
            if tithi_name_from_text(segment).lower() != target_tithi.lower():
                continue
            dt = parse_tithi_upto_datetime(segment, date_str, timezone)
            if dt is not None:
                candidates.append(dt)
    if not candidates:
        return None
    # Within the narrow one-cycle window there should be only one target end.
    # If duplicate rows report the same transition, choosing the median-like
    # central unique value avoids dependence on page order.
    unique = sorted({x for x in candidates})
    return unique[len(unique) // 2]


@dataclass
class TransitionSet:
    ekadashi_start: datetime | None = None
    ekadashi_end: datetime | None = None
    dwadashi_end: datetime | None = None

    @property
    def dwadashi_start(self) -> datetime | None:
        return self.ekadashi_end

    @property
    def complete(self) -> bool:
        return all([self.ekadashi_start, self.ekadashi_end, self.dwadashi_end])


def reconstruct_transitions(session: CitySession) -> TransitionSet:
    return TransitionSet(
        ekadashi_start=find_tithi_end(session.observations, "Dashami", session.timezone),
        ekadashi_end=find_tithi_end(session.observations, "Ekadashi", session.timezone),
        dwadashi_end=find_tithi_end(session.observations, "Dwadashi", session.timezone),
    )


def observed_date_bounds(session: CitySession) -> tuple[str, str]:
    values = sorted(session.observations)
    return values[0], values[-1]


def choose_next_date_for_missing_transition(
    session: CitySession,
    transitions: TransitionSet,
) -> tuple[str, str]:
    earliest, latest = observed_date_bounds(session)
    earliest_rank = TITHI_ORDER.get(clean(session.observations[earliest].get("Sunrise Tithi", "")), 0)
    latest_rank = TITHI_ORDER.get(clean(session.observations[latest].get("Sunrise Tithi", "")), 0)

    if transitions.ekadashi_start is None:
        # Need the Dashami end. If our earliest sunrise is already Ekadashi or
        # later, move backward; otherwise move forward.
        if earliest_rank >= 11:
            return date_add(earliest, -1), "need Dashami end / Ekadashi start"
        return date_add(latest, 1), "need Dashami end / Ekadashi start"

    if transitions.ekadashi_end is None:
        if latest_rank <= 11 or latest_rank == 0:
            return date_add(latest, 1), "need Ekadashi end / Dwadashi start"
        return date_add(earliest, -1), "need Ekadashi end / Dwadashi start"

    if transitions.dwadashi_end is None:
        if latest_rank <= 12 or latest_rank == 0:
            return date_add(latest, 1), "need Dwadashi end / Trayodashi start"
        return date_add(earliest, -1), "need Dwadashi end / Trayodashi start"

    raise RuntimeError("No transition is missing")


def resolve_transitions(session: CitySession) -> TransitionSet:
    session.get_observation(ANCHOR_DATE)
    for _ in range(MAX_FRESH_SCANS_PER_CITY + 8):
        transitions = reconstruct_transitions(session)
        if transitions.complete:
            assert transitions.ekadashi_start and transitions.ekadashi_end and transitions.dwadashi_end
            if not (
                transitions.ekadashi_start < transitions.ekadashi_end < transitions.dwadashi_end
            ):
                raise RuntimeError(
                    "Transition ordering is invalid: "
                    f"E-start={fmt_dt(transitions.ekadashi_start)}, "
                    f"E-end={fmt_dt(transitions.ekadashi_end)}, "
                    f"D-end={fmt_dt(transitions.dwadashi_end)}"
                )
            return transitions
        next_date, why = choose_next_date_for_missing_transition(session, transitions)
        print(f"    -> need {next_date}: {why}")
        session.get_observation(next_date)
    raise RuntimeError("Could not reconstruct all Dashami/Ekadashi/Dwadashi transitions")


def date_range(start: date, end: date) -> list[str]:
    values: list[str] = []
    current = start
    while current <= end:
        values.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return values


def ensure_classification_sunrises(session: CitySession, t: TransitionSet) -> None:
    assert t.ekadashi_start and t.ekadashi_end and t.dwadashi_end
    # These are the only local civil dates whose sunrise can materially decide
    # Missing / Ekadashi-Dwaya / Dwadashi-Dwaya and the first Trayodashi sunrise.
    start = t.ekadashi_start.date()
    # A sunrise lying inside Ekadashi or Dwadashi can only occur on civil dates
    # from E-start.date through D-end.date. Do NOT fetch D-end+1 yet; that next
    # date is requested later only if the chosen classification actually needs
    # a Trayodashi-sunrise Parana day.
    end = t.dwadashi_end.date()
    for d in date_range(start, end):
        session.get_observation(d)


def sunrise_dt(obs: dict[str, Any], timezone: str) -> datetime | None:
    return parse_time_on_date(clean(obs.get("Sunrise", "")), clean(obs.get("Date", "")), timezone)


def sunset_dt(obs: dict[str, Any], timezone: str) -> datetime | None:
    return parse_time_on_date(clean(obs.get("Sunset", "")), clean(obs.get("Date", "")), timezone)


def arunodaya_dt(obs: dict[str, Any], timezone: str) -> datetime | None:
    s = sunrise_dt(obs, timezone)
    return s - timedelta(minutes=96) if s else None


def sunrise_dates_between(
    session: CitySession,
    start: datetime,
    end: datetime,
) -> list[str]:
    result: list[str] = []
    for d, obs in sorted(session.observations.items()):
        s = sunrise_dt(obs, session.timezone)
        if s is not None and start <= s < end:
            result.append(d)
    return result


def first_sunrise_after(session: CitySession, moment: datetime) -> str | None:
    candidates: list[tuple[datetime, str]] = []
    for d, obs in session.observations.items():
        s = sunrise_dt(obs, session.timezone)
        if s and s >= moment:
            candidates.append((s, d))
    return min(candidates)[1] if candidates else None


@dataclass
class ClassificationResult:
    ekadashi_type: str
    condition_code: str
    upavasa_dates: list[str]
    parana_date: str
    first_ekadashi_sunrise_date: str = ""
    first_dwadashi_sunrise_date: str = ""
    explanation: str = ""
    flags: list[str] = field(default_factory=list)
    # Every sunrise falling inside Ekadashi is evaluated independently.
    # A sunrise is VALID when Ekadashi began before that day's Arunodaya;
    # it is VIDDHA when Ekadashi began during that day's Arunodaya.
    valid_ekadashi_sunrise_dates: list[str] = field(default_factory=list)
    viddha_ekadashi_sunrise_dates: list[str] = field(default_factory=list)
    # DWADASHI: ordinary Ekadashi Parana; Hari Vasara and preferred kala rules apply.
    # POST_DWADASHI_UPAVASA: the Upavaasa itself was observed on Dwadashi
    # (or the second day is Dwadashi). On the following morning Hari Vasara is
    # no longer the limiter: Parana begins at sunrise and ends at the earlier
    # of Dwadashi end and Pratah end. If Dwadashi ended before sunrise, the
    # full sunrise-to-Pratah window is used.
    parana_mode: str = "DWADASHI"


def evaluate_ekadashi_sunrises(
    session: CitySession,
    t: TransitionSet,
    e_sunrises: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """
    Evaluate EACH sunrise that falls inside Ekadashi Tithi.

    VALID:
        Ekadashi began strictly before that day's Arunodaya.

    VIDDHA:
        Ekadashi began at/after Arunodaya but at/before sunrise.

    This is intentionally sunrise-specific. A first Viddha sunrise does NOT
    force the entire lunar cycle to shift to Dwadashi; if Ekadashi survives to
    another sunrise, that next sunrise is evaluated independently.
    """
    assert t.ekadashi_start
    valid: list[str] = []
    viddha: list[str] = []
    evaluations: list[str] = []

    for d in e_sunrises:
        obs = session.observations[d]
        sr = sunrise_dt(obs, session.timezone)
        aru = arunodaya_dt(obs, session.timezone)
        if not sr or not aru:
            raise RuntimeError(f"Missing sunrise/Arunodaya for Ekadashi sunrise {d}")

        if t.ekadashi_start < aru:
            valid.append(d)
            evaluations.append(
                f"{d}:VALID (start {fmt_dt(t.ekadashi_start)} < "
                f"Arunodaya {fmt_dt(aru)})"
            )
        elif aru <= t.ekadashi_start <= sr:
            viddha.append(d)
            evaluations.append(
                f"{d}:VIDDHA (Arunodaya {fmt_dt(aru)} <= "
                f"start {fmt_dt(t.ekadashi_start)} <= sunrise {fmt_dt(sr)})"
            )
        else:
            raise RuntimeError(
                "Ekadashi sunrise could not be evaluated as VALID or VIDDHA: "
                f"date={d}, start={fmt_dt(t.ekadashi_start)}, "
                f"Arunodaya={fmt_dt(aru)}, sunrise={fmt_dt(sr)}"
            )

    return valid, viddha, evaluations


def classify_cycle(session: CitySession, t: TransitionSet) -> ClassificationResult:
    assert t.ekadashi_start and t.ekadashi_end and t.dwadashi_end

    e_sunrises = sunrise_dates_between(session, t.ekadashi_start, t.ekadashi_end)
    d_sunrises = sunrise_dates_between(session, t.ekadashi_end, t.dwadashi_end)

    # Missing Ekadashi: no sunrise falls inside Ekadashi Tithi.
    if len(e_sunrises) == 0:
        if not d_sunrises:
            raise RuntimeError(
                "No sunrise during Ekadashi and no Dwadashi sunrise found; "
                "this pattern is outside the supplied scenarios."
            )
        upavasa = d_sunrises[0]
        parana = date_add(upavasa, 1)
        session.get_observation(parana)
        return ClassificationResult(
            "Missing Ekadashi", "MISSING_EKADASHI", [upavasa], parana,
            first_dwadashi_sunrise_date=d_sunrises[0],
            explanation=(
                "Ekadashi Tithi begins after one sunrise and ends before the next, "
                "so no sunrise occurs during Ekadashi. Upavaasa is observed on Dwadashi."
            ),
            parana_mode="POST_DWADASHI_UPAVASA",
        )

    valid_e, viddha_e, evaluations = evaluate_ekadashi_sunrises(
        session, t, e_sunrises
    )
    first_e = e_sunrises[0]
    flags = evaluations.copy()

    # ------------------------------------------------------------------
    # No valid Ekadashi sunrise exists.
    # This is the TRUE shifted Dashami-Viddha case: only now do we move
    # Upavaasa to Dwadashi.
    # ------------------------------------------------------------------
    if not valid_e:
        if not viddha_e:
            raise RuntimeError(
                "Ekadashi occupies sunrise(s), but none was classified VALID or VIDDHA."
            )

        if d_sunrises:
            upavasa = d_sunrises[0]
        else:
            # Defensive fallback for an unusual cycle in which Dwadashi does not
            # reach a sunrise. This keeps the result visible for audit rather than
            # silently treating the first Viddha day as valid.
            upavasa = date_add(e_sunrises[-1], 1)
            session.get_observation(upavasa)
            flags.append("No Dwadashi sunrise found; shifted Upavaasa used next civil day")

        parana = date_add(upavasa, 1)
        session.get_observation(parana)
        return ClassificationResult(
            "Dashami Viddha Ekadashi", "DASHAMI_VIDDHA", [upavasa], parana,
            first_ekadashi_sunrise_date=first_e,
            first_dwadashi_sunrise_date=d_sunrises[0] if d_sunrises else "",
            explanation=(
                "Ekadashi is present at sunrise, but every Ekadashi sunrise is "
                "Dashami Viddha; no later valid Ekadashi sunrise occurs. Therefore "
                "Upavaasa shifts to Dwadashi and Parana is on the following morning."
            ),
            flags=flags,
            valid_ekadashi_sunrise_dates=valid_e,
            viddha_ekadashi_sunrise_dates=viddha_e,
            parana_mode="POST_DWADASHI_UPAVASA",
        )

    # ------------------------------------------------------------------
    # Two VALID Ekadashi sunrises -> Ekadashi Dwaya.
    # Any earlier Viddha sunrise remains an informational flag/no-fast row;
    # it does not invalidate later valid Ekadashi sunrises.
    # ------------------------------------------------------------------
    if len(valid_e) >= 2:
        day1, day2 = valid_e[0], valid_e[1]
        parana = date_add(day2, 1)
        session.get_observation(parana)
        explanation = (
            "Ekadashi Tithi is valid at two consecutive sunrises. Observe "
            "Upavaasa on both valid Ekadashi days and Parana on Dwadashi."
        )
        if viddha_e:
            explanation = (
                "An earlier Ekadashi sunrise is Dashami Viddha, but Ekadashi is "
                "then valid at two consecutive later sunrises. " + explanation
            )
        return ClassificationResult(
            "Ekadashi Dwaya", "EKADASHI_DWAYA", [day1, day2], parana,
            first_ekadashi_sunrise_date=first_e,
            first_dwadashi_sunrise_date=d_sunrises[0] if d_sunrises else "",
            explanation=explanation,
            flags=flags,
            valid_ekadashi_sunrise_dates=valid_e,
            viddha_ekadashi_sunrise_dates=viddha_e,
        )

    # From here there is exactly ONE valid Ekadashi sunrise. That valid sunrise
    # is the base Upavaasa day, even if an earlier Ekadashi sunrise was Viddha.
    valid_day = valid_e[0]

    # Dwadashi Dwaya: valid Ekadashi sunrise followed by Dwadashi at two
    # consecutive sunrises. Upavaasa on valid Ekadashi + first Dwadashi day.
    if len(d_sunrises) >= 2:
        day2 = d_sunrises[0]
        parana = date_add(day2, 1)
        session.get_observation(parana)
        explanation = (
            "A valid Ekadashi sunrise is followed by Dwadashi at two consecutive "
            "sunrises. Observe Upavaasa on the valid Ekadashi day and on the first "
            "Dwadashi day; Parana is on the following morning."
        )
        if viddha_e:
            explanation = (
                "An earlier Ekadashi sunrise was Dashami Viddha and is not a fasting "
                "day. A later Ekadashi sunrise is valid. " + explanation
            )
        return ClassificationResult(
            "Dwadashi Dwaya", "DWADASHI_DWAYA", [valid_day, day2], parana,
            first_ekadashi_sunrise_date=first_e,
            first_dwadashi_sunrise_date=day2,
            explanation=explanation,
            flags=flags,
            valid_ekadashi_sunrise_dates=valid_e,
            viddha_ekadashi_sunrise_dates=viddha_e,
            parana_mode="POST_DWADASHI_UPAVASA",
        )

    # Shravanopavasa: valid Ekadashi followed by Dwadashi sunrise with Shravana.
    if len(d_sunrises) == 1:
        d_date = d_sunrises[0]
        d_nak = clean(session.observations[d_date].get("Sunrise Nakshatra", ""))
        if is_shravana(d_nak):
            parana = date_add(d_date, 1)
            session.get_observation(parana)
            explanation = (
                "A valid Ekadashi sunrise is followed by Dwadashi sunrise with "
                "Shravana Nakshatra. Observe Upavaasa on both days."
            )
            if viddha_e:
                explanation = (
                    "An earlier Ekadashi sunrise was Dashami Viddha and is not a "
                    "fasting day. A later Ekadashi sunrise is valid. " + explanation
                )
            return ClassificationResult(
                "Shravanopavasa", "SHRAVANOPAVASA", [valid_day, d_date], parana,
                first_ekadashi_sunrise_date=first_e,
                first_dwadashi_sunrise_date=d_date,
                explanation=explanation,
                flags=flags,
                valid_ekadashi_sunrise_dates=valid_e,
                viddha_ekadashi_sunrise_dates=viddha_e,
                parana_mode="POST_DWADASHI_UPAVASA",
            )

    # One valid Ekadashi sunrise -> one-day Upavaasa. If there was an earlier
    # Viddha sunrise, retain it as part of the classification rather than
    # incorrectly shifting the fast to Dwadashi (Adelaide-type pattern).
    parana = date_add(valid_day, 1)
    session.get_observation(parana)

    if viddha_e:
        return ClassificationResult(
            "Dashami Viddha -> Valid Ekadashi",
            "DASHAMI_VIDDHA_THEN_VALID_EKADASHI",
            [valid_day], parana,
            first_ekadashi_sunrise_date=first_e,
            first_dwadashi_sunrise_date=d_sunrises[0] if d_sunrises else "",
            explanation=(
                "The first Ekadashi sunrise is Dashami Viddha and is not a fasting "
                "day. Ekadashi continues to a later sunrise where it had already "
                "begun before Arunodaya; that later Ekadashi sunrise is the valid "
                "Upavaasa day. Parana is on the following Dwadashi morning."
            ),
            flags=flags,
            valid_ekadashi_sunrise_dates=valid_e,
            viddha_ekadashi_sunrise_dates=viddha_e,
            parana_mode="DWADASHI",
        )

    return ClassificationResult(
        "Normal Ekadashi", "NORMAL", [valid_day], parana,
        first_ekadashi_sunrise_date=first_e,
        first_dwadashi_sunrise_date=d_sunrises[0] if d_sunrises else "",
        explanation=(
            "Ekadashi began before Arunodaya and has one valid Ekadashi sunrise. "
            "No two-day exception is triggered."
        ),
        flags=flags,
        valid_ekadashi_sunrise_dates=valid_e,
        viddha_ekadashi_sunrise_dates=viddha_e,
    )


# =============================================================================
# PARANA CALCULATION
# =============================================================================


@dataclass
class DayKalas:
    sunrise: datetime
    sunset: datetime
    pratah_end: datetime
    sangava_end: datetime
    madhyahna_end: datetime
    aparahna_end: datetime


@dataclass
class ParanaResult:
    start: datetime
    end: datetime
    branch: str
    hari_vasara_end: datetime
    note: str = ""


def calculate_day_kalas(obs: dict[str, Any], timezone: str) -> DayKalas:
    sr = sunrise_dt(obs, timezone)
    ss = sunset_dt(obs, timezone)
    if not sr or not ss:
        raise RuntimeError("Parana-day sunrise or sunset is missing")
    if ss <= sr:
        ss += timedelta(days=1)
    fifth = (ss - sr) / 5
    return DayKalas(
        sunrise=sr,
        sunset=ss,
        pratah_end=sr + fifth,
        sangava_end=sr + 2 * fifth,
        madhyahna_end=sr + 3 * fifth,
        aparahna_end=sr + 4 * fifth,
    )


def calculate_parana(
    session: CitySession,
    t: TransitionSet,
    classification: ClassificationResult,
) -> ParanaResult:
    assert t.ekadashi_end and t.dwadashi_end
    d_start = t.ekadashi_end
    d_end = t.dwadashi_end
    if d_end <= d_start:
        raise RuntimeError("Dwadashi end must be after Dwadashi start")
    hv_end = d_start + (d_end - d_start) / 4

    obs = session.get_observation(classification.parana_date)
    kalas = calculate_day_kalas(obs, session.timezone)
    sr = kalas.sunrise

    # Shifted/two-day cases in which the Upavaasa was observed on Dwadashi
    # (or the second fasting day was Dwadashi). On the following morning,
    # Hari Vasara is no longer the limiter. Parana begins at sunrise.
    #
    # If Dwadashi has already ended before sunrise, the whole Pratah window
    # is available. If Dwadashi is still present at sunrise, Parana must end
    # at whichever comes first: Dwadashi end or Pratah end.
    if classification.parana_mode == "POST_DWADASHI_UPAVASA":
        if d_end <= sr:
            return ParanaResult(
                sr, kalas.pratah_end,
                "POST_DWADASHI_FAST_DWADASHI_OVER_BEFORE_SUNRISE", hv_end,
                "Dwadashi ended before Parana-day sunrise; Parana is sunrise "
                "to Pratah end.",
            )

        end = min(d_end, kalas.pratah_end)
        if d_end <= kalas.pratah_end:
            note = (
                "Dwadashi is present at Parana-day sunrise and ends during "
                "Pratah; Parana is sunrise to Dwadashi end."
            )
            branch = "POST_DWADASHI_FAST_DWADASHI_ENDS_IN_PRATAH"
        else:
            note = (
                "Dwadashi is present beyond Pratah; Parana is sunrise to "
                "Pratah end."
            )
            branch = "POST_DWADASHI_FAST_PRATAH_ENDS_FIRST"

        return ParanaResult(sr, end, branch, hv_end, note)

    # User rule: if Dwadashi has already passed before sunrise, Parana is simply
    # sunrise through the end of Pratah, because Trayodashi is already present.
    if d_end <= sr:
        return ParanaResult(
            sr, kalas.pratah_end, "DWADASHI_OVER_BEFORE_SUNRISE", hv_end,
            "Dwadashi ended before sunrise; Parana is sunrise to Pratah end.",
        )

    # Dwadashi is active at sunrise. First avoid Hari Vasara.
    if hv_end <= kalas.pratah_end:
        start = max(sr, hv_end)
        end = min(kalas.pratah_end, d_end)
        if hv_end <= sr:
            note = (
                "Hari Vasara ended before Parana-day sunrise; "
                "Parana is sunrise to Pratah end."
            )
        else:
            note = (
                "Hari Vasara ends during Pratah; Parana begins after "
                "Hari Vasara and ends at Pratah end."
            )
        return ParanaResult(
            start, end, "PRATAH", hv_end, note,
        )

    # Pratah missed. Skip Madhyahna and use Aparahna if Dwadashi still permits it.
    aparahna_start = kalas.madhyahna_end
    if d_end > aparahna_start and hv_end <= kalas.aparahna_end:
        start = max(aparahna_start, hv_end)
        end = min(kalas.aparahna_end, d_end)
        if end > start:
            return ParanaResult(
                start, end, "APARAHNA", hv_end,
                "Pratah is unavailable; Madhyahna is skipped and Aparahna is used.",
            )

    # User refinement: if even the preferred later window has been crossed,
    # Dwadashi end becomes the upper limit. This also covers cases where Dwadashi
    # itself would finish before a usable Aparahna interval can be completed.
    start = max(sr, hv_end)
    end = d_end
    if end <= start:
        # Defensive only; mathematically HV end is inside Dwadashi.
        end = d_end
    return ParanaResult(
        start, end, "DWADASHI_END_LIMIT", hv_end,
        "Preferred windows are crossed/constrained; Dwadashi end is the upper limit.",
    )


# =============================================================================
# COMPLETENESS VALIDATION
# =============================================================================


def validate_complete(
    session: CitySession,
    t: TransitionSet,
    c: ClassificationResult,
    p: ParanaResult,
) -> list[str]:
    missing: list[str] = []
    if not t.ekadashi_start:
        missing.append("Dashami end / Ekadashi start")
    if not t.ekadashi_end:
        missing.append("Ekadashi end / Dwadashi start")
    if not t.dwadashi_end:
        missing.append("Dwadashi end / Trayodashi start")
    if not c.upavasa_dates:
        missing.append("Upavaasa date")
    if not c.parana_date:
        missing.append("Parana date")
    for d in c.upavasa_dates + [c.parana_date]:
        obs = session.observations.get(d)
        if not obs:
            missing.append(f"Observation for {d}")
            continue
        if not clean(obs.get("Sunrise", "")):
            missing.append(f"Sunrise for {d}")
    parana_obs = session.observations.get(c.parana_date, {})
    if not clean(parana_obs.get("Sunset", "")):
        missing.append("Parana-day sunset")
    # Whenever Dwadashi is present at sunrise, its Nakshatra is required to
    # positively rule Shravanopavasa in or out.  A non-empty garbage string is
    # not sufficient.
    d = c.first_dwadashi_sunrise_date
    if d:
        d_nak = clean(session.observations.get(d, {}).get("Sunrise Nakshatra", ""))
        if not is_valid_nakshatra(d_nak):
            missing.append("Valid Dwadashi sunrise Nakshatra")
    if p.start is None or p.end is None:
        missing.append("Parana window")
    return sorted(set(missing))


# =============================================================================
# MESSAGE GENERATION
# =============================================================================


def transition_detail_text(t: TransitionSet) -> str:
    """Detailed transition evidence retained for audit/debug use."""
    return (
        f"Ekadashi starts: {fmt_dt(t.ekadashi_start)} | "
        f"Ekadashi ends / Dwadashi starts: {fmt_dt(t.ekadashi_end)} | "
        f"Dwadashi ends / Trayodashi starts: {fmt_dt(t.dwadashi_end)}"
    )


def full_special_detail_text(
    session: CitySession,
    t: TransitionSet,
    c: ClassificationResult,
    p: ParanaResult,
) -> str:
    """Full technical detail used by the audit, not the public daily display."""
    parts = [transition_detail_text(t)]

    for d in c.viddha_ekadashi_sunrise_dates:
        obs = session.observations.get(d, {})
        parts.append(
            f"{fmt_date(d)} Ekadashi sunrise: Dashami Viddha; "
            f"Sunrise {clean(obs.get('Sunrise',''))}; "
            f"Arunodaya {fmt_public_time(arunodaya_dt(obs, session.timezone))}"
        )

    for d in c.valid_ekadashi_sunrise_dates:
        obs = session.observations.get(d, {})
        parts.append(
            f"{fmt_date(d)} Ekadashi sunrise: Valid; "
            f"Sunrise {clean(obs.get('Sunrise',''))}; "
            f"Arunodaya {fmt_public_time(arunodaya_dt(obs, session.timezone))}"
        )

    parts.append(
        "Upavaasa: " + ", ".join(fmt_date(x) for x in c.upavasa_dates)
    )
    parts.append(f"Hari Vasara end: {fmt_dt(p.hari_vasara_end)}")
    parts.append(
        f"Parana: {fmt_date(c.parana_date)}, "
        f"{fmt_public_time(p.start)} to {fmt_public_time(p.end)}"
    )
    return " | ".join(parts)


def parana_text(c: ClassificationResult, p: ParanaResult) -> str:
    return (
        f"Parana: {fmt_date(c.parana_date)}, "
        f"{fmt_public_time(p.start)} to {fmt_public_time(p.end)}"
    )


def public_upavasa_text(c: ClassificationResult) -> str:
    dates = [fmt_date(d) for d in c.upavasa_dates if d]
    if not dates:
        return "Upavaasa:"
    if len(dates) == 1:
        return f"Upavaasa: {dates[0]}"
    return f"Upavaasa: {' and '.join(dates)}"


def public_special_details(
    c: ClassificationResult,
    p: ParanaResult,
    action_role: str,
) -> str:
    """Concise public timing field consumed by the master/main scanner."""
    upavasa = public_upavasa_text(c)
    if action_role == "PARANA":
        parana = (
            f"Parana today: {fmt_public_time(p.start)} to {fmt_public_time(p.end)}"
        )
    else:
        parana = (
            f"Parana: {fmt_date(c.parana_date)}, "
            f"{fmt_public_time(p.start)} to {fmt_public_time(p.end)}"
        )
    return f"{upavasa} | {parana}"


def _two_day_case_explanation(c: ClassificationResult, day_number: int) -> str:
    prefix = ""
    if c.viddha_ekadashi_sunrise_dates:
        prefix = (
            "An earlier Ekadashi sunrise was Dashami Viddha and was not a fasting day. "
        )

    if c.condition_code == "EKADASHI_DWAYA":
        if day_number == 1:
            return (
                prefix
                + "Ekadashi is valid at two consecutive sunrises. "
                + "**Observe Upavaasa today and continue tomorrow.**"
            )
        return (
            prefix
            + "Ekadashi is valid at a second consecutive sunrise. "
            + "**Continue the Upavaasa today.** This is the second day of the two-day Upavaasa."
        )

    if c.condition_code == "DWADASHI_DWAYA":
        if day_number == 1:
            return (
                prefix
                + "A valid Ekadashi sunrise is followed by Dwadashi at two consecutive sunrises. "
                + "**Observe Upavaasa today and continue tomorrow.**"
            )
        return (
            prefix
            + "Dwadashi continues through another sunrise in this Dwadashi Dwaya cycle. "
            + "**Continue the Upavaasa today.** This is the second day of the two-day Upavaasa."
        )

    if c.condition_code == "SHRAVANOPAVASA":
        if day_number == 1:
            return (
                prefix
                + "A valid Ekadashi sunrise is followed by Dwadashi with Shravana Nakshatra at sunrise. "
                + "**Observe Upavaasa today and continue tomorrow.**"
            )
        return (
            prefix
            + "Today is Dwadashi with Shravana Nakshatra at sunrise. "
            + "**Continue the Upavaasa today.** This is the second day of the two-day Upavaasa."
        )

    if day_number == 1:
        return "**Observe Upavaasa today and continue tomorrow.**"
    return "**Continue the Upavaasa today.** This is the second day of the two-day Upavaasa."


def public_note_for_action(
    c: ClassificationResult,
    action_role: str,
    action_date: str,
    p: ParanaResult,
) -> str:
    """Short devotional/public explanation. Full evidence remains in audit.csv."""
    if action_role == "VIDDHA_NO_FAST":
        next_fast = c.upavasa_dates[0] if c.upavasa_dates else ""
        if next_fast in c.valid_ekadashi_sunrise_dates:
            return (
                "**Do not observe Upavaasa today.** Today's Ekadashi sunrise is "
                "Dashami Viddha because Ekadashi began during Arunodaya. "
                f"Ekadashi continues to a valid sunrise on {fmt_date(next_fast)}; "
                "observe Upavaasa on that date."
            )
        return (
            "**Do not observe Upavaasa today.** Today's Ekadashi sunrise is "
            "Dashami Viddha because Ekadashi began during Arunodaya. "
            "No later valid Ekadashi sunrise occurs; "
            f"observe Upavaasa on {fmt_date(next_fast)} on Dwadashi."
        )

    if action_role == "PARANA":
        if len(c.upavasa_dates) == 2:
            observed = " and ".join(fmt_date(d) for d in c.upavasa_dates)
        else:
            observed = fmt_date(c.upavasa_dates[0]) if c.upavasa_dates else ""
        return (
            f"**Perform Parana today** between {fmt_public_time(p.start)} and "
            f"{fmt_public_time(p.end)}. Upavaasa was observed on {observed}."
        )

    # Upavaasa rows.
    if len(c.upavasa_dates) == 2:
        day_number = 1 if action_date == c.upavasa_dates[0] else 2
        return _two_day_case_explanation(c, day_number)

    if c.condition_code == "NORMAL":
        return (
            "Normal Ekadashi. **Observe Upavaasa today.** Ekadashi began before "
            "Arunodaya and no two-day exception applies."
        )

    if c.condition_code == "DASHAMI_VIDDHA_THEN_VALID_EKADASHI":
        return (
            "The previous Ekadashi sunrise was Dashami Viddha and was not a fasting day. "
            "Ekadashi continues and is valid at today's sunrise. "
            "**Observe Upavaasa today.**"
        )

    if c.condition_code == "DASHAMI_VIDDHA":
        return (
            "The Ekadashi sunrise was Dashami Viddha and no later valid Ekadashi "
            "sunrise occurred. **Observe Upavaasa today on Dwadashi.**"
        )

    if c.condition_code == "MISSING_EKADASHI":
        return (
            "Ekadashi occurred completely between two consecutive sunrises, so there "
            "was no Ekadashi sunrise. **Observe Upavaasa today on Dwadashi.**"
        )

    return f"{c.ekadashi_type}. **Observe Upavaasa today.**"


def strip_bold_markers(value: str) -> str:
    return clean(value).replace("**", "")


def build_message_rows(
    session: CitySession,
    t: TransitionSet,
    c: ClassificationResult,
    p: ParanaResult,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    common = {
        "Place Key": session.place_key,
        "City": session.city,
        "State/Region": session.state,
        "Country": session.country,
        "Timezone": session.timezone,
        "Geoname ID": session.geoname_id,
        "Ekadashi Name": CYCLE_NAME,
        "Ekadashi Type": c.ekadashi_type,
        "Condition Code": c.condition_code,
        "Upavaasa Date 1": c.upavasa_dates[0] if c.upavasa_dates else "",
        "Upavaasa Date 2": c.upavasa_dates[1] if len(c.upavasa_dates) > 1 else "",
        "Parana Date": c.parana_date,
        "Parana Start": fmt_public_time(p.start),
        "Parana End": fmt_public_time(p.end),
        "Parana Branch": p.branch,
        "Parana Tithi Rule": c.parana_mode,
        "Hari Vasara End": fmt_public_time(p.hari_vasara_end),
        "Cycle Anchor": ANCHOR_DATE,
        "Completeness Status": "COMPLETE",
    }

    # Informational no-fast row for EVERY Dashami-Viddha Ekadashi sunrise.
    for d in c.viddha_ekadashi_sunrise_dates:
        role = "VIDDHA_NO_FAST"
        note = public_note_for_action(c, role, d, p)
        details = public_special_details(c, p, role)
        rows.append({
            **common,
            "Date": d,
            "Action Role": role,
            # Public event name stays clean; the scenario belongs in Note.
            "Special Events": CYCLE_NAME,
            "Special Event Details": details,
            "Note": note,
            "Message": f"{CYCLE_NAME}. {strip_bold_markers(note)} {details}",
        })

    for idx, d in enumerate(c.upavasa_dates, start=1):
        role = "UPAVASA_DAY_2" if len(c.upavasa_dates) == 2 and idx == 2 else "UPAVASA_DAY_1"
        note = public_note_for_action(c, role, d, p)
        details = public_special_details(c, p, role)
        rows.append({
            **common,
            "Date": d,
            "Action Role": role,
            "Special Events": CYCLE_NAME,
            "Special Event Details": details,
            "Note": note,
            "Message": f"{CYCLE_NAME}. {strip_bold_markers(note)} {details}",
        })

    role = "PARANA"
    note = public_note_for_action(c, role, c.parana_date, p)
    details = public_special_details(c, p, role)
    rows.append({
        **common,
        "Date": c.parana_date,
        "Action Role": role,
        # Keep the Ekadashi name itself as the public event heading.
        "Special Events": CYCLE_NAME,
        "Special Event Details": details,
        "Note": note,
        "Message": f"{CYCLE_NAME}. {strip_bold_markers(note)} {details}",
    })

    return rows



def describe_ekadashi_sunrise_day(session: CitySession, d: str, classification: str) -> str:
    obs = session.observations.get(d, {})
    sr_txt = clean(obs.get("Sunrise", ""))
    aru = fmt_public_time(arunodaya_dt(obs, session.timezone))
    if classification == "VALID":
        return (
            f"On {fmt_date(d)}, Sooryodaya is {sr_txt} and Arunodaya is {aru}. "
            f"Ekadashi is present at sunrise and had begun before Arunodaya, so "
            f"this is a valid Ekadashi sunrise."
        )
    return (
        f"On {fmt_date(d)}, Sooryodaya is {sr_txt} and Arunodaya is {aru}. "
        f"Ekadashi is present at sunrise, but it began during Arunodaya, so "
        f"this sunrise is Dashami Viddha and no Upavaasa is observed on this day."
    )




def build_parana_audit_explanation(
    session: CitySession,
    t: TransitionSet,
    c: ClassificationResult,
    p: ParanaResult,
) -> str:
    """Explain exactly how the Parana start and end were chosen."""
    assert t.ekadashi_end and t.dwadashi_end
    obs = session.observations.get(c.parana_date, {})
    kalas = calculate_day_kalas(obs, session.timezone)
    sr = kalas.sunrise
    pe = kalas.pratah_end
    de = t.dwadashi_end
    hv = p.hari_vasara_end

    pieces: list[str] = [
        f"For Parana on {fmt_date(c.parana_date)}, Sooryodaya is {fmt_public_time(sr)}, "
        f"Pratah Kala ends at {fmt_public_time(pe)}, Dwadashi ends at {fmt_dt(de)}, "
        f"and Hari Vasara ends at {fmt_dt(hv)}."
    ]

    # Cases where Upavaasa was observed on Dwadashi (or included Dwadashi).
    if c.parana_mode == "POST_DWADASHI_UPAVASA":
        pieces.append(
            "Because the Upavaasa itself was observed on Dwadashi, Hari Vasara is "
            "not used to delay the Parana start on the following morning."
        )
        if de <= sr:
            pieces.append(
                f"Dwadashi has already ended before Sooryodaya, so Parana starts at "
                f"Sooryodaya ({fmt_public_time(sr)}). Since Dwadashi is already over, "
                f"the available morning limit is the end of Pratah Kala "
                f"({fmt_public_time(pe)})."
            )
        else:
            pieces.append(
                f"Dwadashi is still present at Sooryodaya, so Parana starts at "
                f"Sooryodaya ({fmt_public_time(sr)})."
            )
            if de <= pe:
                pieces.append(
                    f"Dwadashi ends at {fmt_public_time(de)}, before Pratah Kala ends at "
                    f"{fmt_public_time(pe)}; therefore Dwadashi end is the limiting end time."
                )
            else:
                pieces.append(
                    f"Pratah Kala ends at {fmt_public_time(pe)}, before Dwadashi ends; "
                    f"therefore Pratah Kala end is the limiting end time."
                )
        pieces.append(
            f"Hence Parana is {fmt_public_time(p.start)} to {fmt_public_time(p.end)}."
        )
        return " ".join(pieces)

    # Ordinary Dwadashi Parana.
    if de <= sr:
        pieces.append(
            f"Dwadashi has already ended before Sooryodaya. Therefore Parana starts at "
            f"Sooryodaya ({fmt_public_time(sr)}) and continues to the end of Pratah Kala "
            f"({fmt_public_time(pe)})."
        )
        pieces.append(
            f"Hence Parana is {fmt_public_time(p.start)} to {fmt_public_time(p.end)}."
        )
        return " ".join(pieces)

    if p.branch == "PRATAH":
        if hv <= sr:
            pieces.append(
                f"Hari Vasara ends before Sooryodaya, so there is no Hari-Vasara delay; "
                f"Parana starts at Sooryodaya ({fmt_public_time(sr)})."
            )
        else:
            pieces.append(
                f"Hari Vasara ends after Sooryodaya, at {fmt_public_time(hv)}, but still "
                f"within Pratah Kala. Therefore Parana cannot start at sunrise and starts "
                f"at Hari Vasara end ({fmt_public_time(hv)})."
            )
        if de <= pe:
            pieces.append(
                f"Dwadashi ends at {fmt_public_time(de)}, before Pratah Kala ends at "
                f"{fmt_public_time(pe)}; therefore Dwadashi end is the limiting end time."
            )
        else:
            pieces.append(
                f"Pratah Kala ends at {fmt_public_time(pe)}, before Dwadashi ends; "
                f"therefore Pratah Kala end is the limiting end time."
            )

    elif p.branch == "APARAHNA":
        aparahna_start = kalas.madhyahna_end
        aparahna_end = kalas.aparahna_end
        pieces.append(
            f"Hari Vasara does not clear within the usable Pratah window, so Pratah is missed. "
            f"Madhyahna is skipped. Aparahna runs from {fmt_public_time(aparahna_start)} "
            f"to {fmt_public_time(aparahna_end)}."
        )
        if hv > aparahna_start:
            pieces.append(
                f"Hari Vasara continues into Aparahna until {fmt_public_time(hv)}, so Parana "
                f"starts at Hari Vasara end rather than at Aparahna start."
            )
        else:
            pieces.append(
                f"Hari Vasara has already ended before Aparahna begins, so Parana starts at "
                f"Aparahna start ({fmt_public_time(aparahna_start)})."
            )
        if de <= aparahna_end:
            pieces.append(
                f"Dwadashi ends at {fmt_public_time(de)}, before Aparahna ends at "
                f"{fmt_public_time(aparahna_end)}; therefore Dwadashi end is the limiting end time."
            )
        else:
            pieces.append(
                f"Aparahna ends at {fmt_public_time(aparahna_end)}, before Dwadashi ends; "
                f"therefore Aparahna end is the limiting end time."
            )

    elif p.branch == "DWADASHI_END_LIMIT":
        pieces.append(
            f"The preferred Pratah/Aparahna windows cannot provide the required interval. "
            f"Parana therefore starts only after Hari Vasara, at {fmt_public_time(hv)}, and "
            f"Dwadashi end ({fmt_public_time(de)}) becomes the absolute upper limit."
        )

    else:
        pieces.append(p.note)

    pieces.append(
        f"Hence Parana is {fmt_public_time(p.start)} to {fmt_public_time(p.end)}."
    )
    return " ".join(pieces)


def build_audit_flow_message(
    session: CitySession,
    t: TransitionSet | None,
    c: ClassificationResult | None,
    p: ParanaResult | None,
    status: str,
    missing: list[str] | None = None,
    error: str = "",
) -> str:
    if status != "COMPLETE" or not (t and c and p):
        parts = [f"{CYCLE_NAME} - {session.city}"]
        if missing:
            parts.append("Could not complete the Ekadashi determination.")
            parts.append("Missing: " + ", ".join(missing) + ".")
        if error:
            parts.append(f"Error: {error}.")
        return " ".join(parts)

    parts: list[str] = [f"{CYCLE_NAME} - {session.city}."]

    if t.ekadashi_start:
        parts.append(
            f"Dashami ends and Ekadashi begins on {fmt_dt(t.ekadashi_start)}."
        )

    # Explain each Ekadashi sunrise in chronological order.
    all_ekadashi_days = sorted(set(c.viddha_ekadashi_sunrise_dates + c.valid_ekadashi_sunrise_dates))
    for d in all_ekadashi_days:
        cls = "VALID" if d in c.valid_ekadashi_sunrise_dates else "VIDDHA"
        parts.append(describe_ekadashi_sunrise_day(session, d, cls))

    # Explain whether Ekadashi survives to another sunrise.
    if len(all_ekadashi_days) >= 2:
        next_day = all_ekadashi_days[1]
        parts.append(
            f"Ekadashi extends to the next sunrise also, so it is still present at "
            f"Sooryodaya on {fmt_date(next_day)}."
        )
    else:
        # Mention that Ekadashi does not survive to another sunrise when helpful.
        if c.first_ekadashi_sunrise_date and t.ekadashi_end:
            parts.append(
                f"Ekadashi ends on {fmt_dt(t.ekadashi_end)} and does not remain through another sunrise."
            )

    if t.ekadashi_end:
        parts.append(
            f"Ekadashi ends and Dwadashi begins on {fmt_dt(t.ekadashi_end)}."
        )

    # Dwadashi sunrise presence.
    dwadashi_days = [
        d for d, obs in sorted(session.observations.items())
        if clean(obs.get('Sunrise Tithi', '')) == 'Dwadashi'
    ]
    if dwadashi_days:
        first_d = dwadashi_days[0]
        obs = session.observations[first_d]
        parts.append(
            f"Dwadashi is present at Sooryodaya on {fmt_date(first_d)} "
            f"(Sooryodaya {clean(obs.get('Sunrise', ''))})."
        )
        if len(dwadashi_days) >= 2:
            second_d = dwadashi_days[1]
            obs2 = session.observations[second_d]
            parts.append(
                f"Dwadashi extends to the next sunrise also, so it is again present at "
                f"Sooryodaya on {fmt_date(second_d)} (Sooryodaya {clean(obs2.get('Sunrise', ''))})."
            )

    if t.dwadashi_end:
        parts.append(
            f"Dwadashi ends and Trayodashi begins on {fmt_dt(t.dwadashi_end)}."
        )

    # Upavaasa summary.
    if len(c.upavasa_dates) == 2:
        parts.append(
            f"Therefore the Upavaasa is observed on {fmt_date(c.upavasa_dates[0])} and {fmt_date(c.upavasa_dates[1])}."
        )
    elif len(c.upavasa_dates) == 1:
        parts.append(
            f"Therefore the Upavaasa is observed on {fmt_date(c.upavasa_dates[0])}."
        )

    # Detailed Parana derivation: show all controlling boundaries and why the
    # selected start/end win.
    parts.append(build_parana_audit_explanation(session, t, c, p))
    return " ".join(parts)


# =============================================================================
# AUDIT ROW
# =============================================================================


def build_audit_row(
    session: CitySession,
    t: TransitionSet | None,
    c: ClassificationResult | None,
    p: ParanaResult | None,
    status: str,
    missing: list[str] | None = None,
    error: str = "",
) -> dict[str, Any]:
    dates = sorted(session.observations)
    sunrise_sequence = " | ".join(
        f"{d}:{clean(session.observations[d].get('Sunrise Tithi',''))}"
        for d in dates
    )
    nakshatra_sequence = " | ".join(
        f"{d}:{clean(session.observations[d].get('Sunrise Nakshatra',''))}"
        for d in dates
    )
    row: dict[str, Any] = {
        "Place Key": session.place_key,
        "City": session.city,
        "State/Region": session.state,
        "Country": session.country,
        "Timezone": session.timezone,
        "Geoname ID": session.geoname_id,
        "Ekadashi Name": CYCLE_NAME,
        "Anchor Date": ANCHOR_DATE,
        "Scan Start": dates[0] if dates else "",
        "Scan End": dates[-1] if dates else "",
        "Dates Used": " | ".join(dates),
        "Sunrise Tithi Sequence": sunrise_sequence,
        "Sunrise Nakshatra Sequence": nakshatra_sequence,
        "Fresh Scans This City": session.fresh_scan_count,
        "Completeness Status": status,
        "Missing Parameters": " | ".join(missing or []),
        "Error": error,
        "Audit Flow Message": build_audit_flow_message(session, t, c, p, status, missing, error),
    }
    if t:
        row.update({
            "Dashami End / Ekadashi Start": fmt_dt(t.ekadashi_start),
            "Ekadashi End / Dwadashi Start": fmt_dt(t.ekadashi_end),
            "Dwadashi End / Trayodashi Start": fmt_dt(t.dwadashi_end),
        })
    if c:
        row.update({
            "Ekadashi Type": c.ekadashi_type,
            "Condition Code": c.condition_code,
            "First Ekadashi Sunrise Date": c.first_ekadashi_sunrise_date,
            "First Dwadashi Sunrise Date": c.first_dwadashi_sunrise_date,
            "Upavaasa Date 1": c.upavasa_dates[0] if c.upavasa_dates else "",
            "Upavaasa Date 2": c.upavasa_dates[1] if len(c.upavasa_dates) > 1 else "",
            "Parana Date": c.parana_date,
            "Parana Tithi Rule": c.parana_mode,
            "Classification Explanation": c.explanation,
            "Classification Flags": " | ".join(c.flags),
            "Valid Ekadashi Sunrise Dates": " | ".join(c.valid_ekadashi_sunrise_dates),
            "Dashami Viddha Sunrise Dates": " | ".join(c.viddha_ekadashi_sunrise_dates),
            "Ekadashi Sunrise Evaluation": " | ".join(c.flags),
        })
        if c.first_ekadashi_sunrise_date:
            obs = session.observations.get(c.first_ekadashi_sunrise_date, {})
            row["First Ekadashi Sunrise"] = clean(obs.get("Sunrise", ""))
            row["First Ekadashi Arunodaya"] = fmt_dt(arunodaya_dt(obs, session.timezone))
        if c.valid_ekadashi_sunrise_dates:
            d = c.valid_ekadashi_sunrise_dates[0]
            obs = session.observations.get(d, {})
            row["First Valid Ekadashi Sunrise Date"] = d
            row["First Valid Ekadashi Sunrise"] = clean(obs.get("Sunrise", ""))
            row["First Valid Ekadashi Arunodaya"] = fmt_dt(
                arunodaya_dt(obs, session.timezone)
            )
        if c.first_dwadashi_sunrise_date:
            obs = session.observations.get(c.first_dwadashi_sunrise_date, {})
            row["Dwadashi Sunrise Nakshatra"] = clean(obs.get("Sunrise Nakshatra", ""))
    if p:
        row.update({
            "Dwadashi Q1 End / Hari Vasara End": fmt_dt(p.hari_vasara_end),
            "Parana Branch": p.branch,
            "Parana Start": fmt_dt(p.start),
            "Parana End": fmt_dt(p.end),
            "Parana Note": p.note,
        })
    return row




AUDIT_EVIDENCE_FIRST_COLUMNS = [
    # Identity
    "City",
    "State/Region",
    "Country",
    "Timezone",
    "Place Key",
    "Geoname ID",
    "Ekadashi Name",
    "Anchor Date",
    "Audit Flow Message",

    # Raw / observational evidence first
    "Sunrise Tithi Sequence",
    "Sunrise Nakshatra Sequence",
    "Dashami End / Ekadashi Start",
    "Ekadashi End / Dwadashi Start",
    "Dwadashi End / Trayodashi Start",
    "First Ekadashi Sunrise Date",
    "First Ekadashi Sunrise",
    "First Ekadashi Arunodaya",
    "Valid Ekadashi Sunrise Dates",
    "Dashami Viddha Sunrise Dates",
    "Ekadashi Sunrise Evaluation",
    "First Valid Ekadashi Sunrise Date",
    "First Valid Ekadashi Sunrise",
    "First Valid Ekadashi Arunodaya",
    "First Dwadashi Sunrise Date",
    "Dwadashi Sunrise Nakshatra",
    "Dwadashi Q1 End / Hari Vasara End",

    # Scan / validation evidence
    "Scan Start",
    "Scan End",
    "Dates Used",
    "Fresh Scans This City",
    "Completeness Status",
    "Missing Parameters",
    "Error",

    # Classification / action results later
    "Ekadashi Type",
    "Condition Code",
    "Classification Explanation",
    "Classification Flags",
    "Upavaasa Date 1",
    "Upavaasa Date 2",
    "Parana Date",
    "Parana Tithi Rule",
    "Parana Branch",
    "Parana Start",
    "Parana End",
    "Parana Note",
]


def reorder_audit_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Put raw astronomical evidence first so the audit can be reviewed manually.

    Any columns added by future code versions but not listed above are preserved
    and appended at the end rather than dropped.
    """
    if df.empty:
        return df
    preferred = [c for c in AUDIT_EVIDENCE_FIRST_COLUMNS if c in df.columns]
    remaining = [c for c in df.columns if c not in preferred]
    return df[preferred + remaining]


# =============================================================================
# CITY INPUT / MAIN
# =============================================================================


def load_cities() -> pd.DataFrame:
    path = Path(INPUT_CSV)
    if not path.exists():
        raise FileNotFoundError(
            f"Could not find {INPUT_CSV}. Put this scanner beside the city CSV "
            "used by your main Panchanga scanner or edit INPUT_CSV."
        )
    cities = pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig")
    cities.columns = cities.columns.str.strip()
    required = {"display_city", "search_city", "state_or_region", "country", "timezone"}
    missing = required - set(cities.columns)
    if missing:
        raise ValueError(f"City CSV is missing required columns: {sorted(missing)}")

    if TEST_CITY_NAMES:
        wanted = {clean(x).lower() for x in TEST_CITY_NAMES}
        cities = cities[
            cities["display_city"].fillna("").astype(str).map(lambda x: clean(x).lower()).isin(wanted)
        ].copy()
        if cities.empty:
            raise ValueError(f"TEST_CITY_NAMES did not match display_city values: {TEST_CITY_NAMES}")
    elif TEST_CITY_LIMIT is not None:
        cities = cities.head(TEST_CITY_LIMIT).copy()

    return cities.reset_index(drop=True)


def print_validation_summary(audit_df: pd.DataFrame) -> None:
    total = len(audit_df)
    complete = int((audit_df["Completeness Status"] == "COMPLETE").sum()) if total else 0
    incomplete = total - complete
    print("\n" + "=" * 96)
    print("VALIDATION SUMMARY")
    print("=" * 96)
    print(f"Ekadashi cycle : {CYCLE_NAME}")
    print(f"Anchor date    : {ANCHOR_DATE}")
    print(f"Cities requested: {total}")
    print(f"Cities COMPLETE : {complete}")
    print(f"Cities INCOMPLETE: {incomplete}")
    if complete and "Ekadashi Type" in audit_df.columns:
        counts = (
            audit_df[audit_df["Completeness Status"] == "COMPLETE"]["Ekadashi Type"]
            .fillna("").value_counts()
        )
        print("\nClassification counts:")
        for name, count in counts.items():
            print(f"  {name or '(blank)'}: {count}")
    if incomplete:
        print("\nIncomplete cities:")
        for _, r in audit_df[audit_df["Completeness Status"] != "COMPLETE"].iterrows():
            reason = clean(r.get("Missing Parameters", "")) or clean(r.get("Error", ""))
            print(f"  {r.get('City','')}: {reason}")


def resolve_cycle_output_paths(
    cycle_slug: str,
    cities: pd.DataFrame,
) -> tuple[Path, Path, Path, bool]:
    """Return (run_dir, audit_csv, messages_csv, is_subset_run).

    Canonical production layout:

        festival_runs/YYYY/MM/ekadashi/<cycle_slug>/

    The anchor supplies only the year/month namespace for file organization;
    it is NEVER used to decide the Ekadashi observance date.

    Partial/test city runs remain isolated under subset_runs so they cannot
    overwrite the canonical full-cycle files.
    """
    anchor_dt = datetime.strptime(ANCHOR_DATE, "%Y-%m-%d")

    cycle_dir = (
        OUTPUT_ROOT
        / f"{anchor_dt.year:04d}"
        / f"{anchor_dt.month:02d}"
        / "ekadashi"
        / cycle_slug
    )
    cycle_dir.mkdir(parents=True, exist_ok=True)

    is_subset = bool(TEST_CITY_NAMES) or TEST_CITY_LIMIT is not None

    if not is_subset:
        run_dir = cycle_dir
        audit_csv = run_dir / f"{cycle_slug}_audit.csv"
        messages_csv = run_dir / f"{cycle_slug}_messages.csv"
        return run_dir, audit_csv, messages_csv, False

    if clean(SUBSET_RUN_LABEL):
        label = slugify(SUBSET_RUN_LABEL)
    elif TEST_CITY_NAMES:
        names = [slugify(x) for x in TEST_CITY_NAMES]
        if len(names) <= 4:
            label = "_".join(names)
        else:
            label = "_".join(names[:3]) + f"_plus_{len(names)-3}"
    else:
        label = f"first_{TEST_CITY_LIMIT}_cities"

    run_dir = cycle_dir / "subset_runs" / label
    run_dir.mkdir(parents=True, exist_ok=True)
    audit_csv = run_dir / f"{cycle_slug}_{label}_audit.csv"
    messages_csv = run_dir / f"{cycle_slug}_{label}_messages.csv"
    return run_dir, audit_csv, messages_csv, True



# =============================================================================
# COMMAND-LINE INTERFACE
# =============================================================================


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ekadashi_cycle_scanner.py",
        description=(
            "Independently calculate one Ekadashi cycle. The anchor is only a search anchor; "
            "classification is derived from local Panchanga evidence."
        ),
    )
    parser.add_argument(
        "--cycle",
        required=True,
        help='Cycle name, e.g. "Shravana Putrada Ekadashi".',
    )
    parser.add_argument(
        "--anchor",
        required=True,
        help="Approximate global anchor date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--cities",
        nargs="+",
        metavar="CITY",
        help="Exact display_city names. Omit for all cities.",
    )
    parser.add_argument(
        "--all-cities",
        action="store_true",
        help="Explicitly request all configured cities (also the default when no subset option is used).",
    )
    parser.add_argument(
        "--city-limit",
        type=int,
        help="Use only the first N configured cities; intended for smoke tests.",
    )
    parser.add_argument(
        "--subset-label",
        default="",
        help="Optional folder label for a subset/backfill run.",
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        default=Path(INPUT_CSV),
        help=f"City configuration CSV (default: {INPUT_CSV}).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=OUTPUT_ROOT,
        help=(
            f"Festival-runs root (default: {OUTPUT_ROOT}). "
            "Canonical output becomes <root>/YYYY/MM/ekadashi/<cycle>/."
        ),
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help=(
            "Optional exact cache CSV path. Otherwise "
            "<output-root>/cache/ekadashi_scan_cache.csv."
        ),
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=PLAYWRIGHT_PROFILE_DIR,
        help=f"Playwright persistent browser profile (default: {PLAYWRIGHT_PROFILE_DIR}).",
    )
    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument("--headless", action="store_true", help="Run Chromium headless.")
    browser_group.add_argument("--headed", action="store_true", help="Force visible Chromium (default).")
    parser.add_argument(
        "--max-distance-days",
        type=int,
        default=MAX_DYNAMIC_DISTANCE_DAYS,
        help=f"Dynamic resolver safety radius around anchor (default: {MAX_DYNAMIC_DISTANCE_DAYS}).",
    )
    parser.add_argument(
        "--max-fresh-scans",
        type=int,
        default=MAX_FRESH_SCANS_PER_CITY,
        help=f"Maximum fresh Drik page scans per city (default: {MAX_FRESH_SCANS_PER_CITY}).",
    )
    return parser.parse_args()


def apply_cli_args(args: argparse.Namespace) -> None:
    global CYCLE_NAME, ANCHOR_DATE, TEST_CITY_NAMES, TEST_CITY_LIMIT, SUBSET_RUN_LABEL
    global INPUT_CSV, OUTPUT_ROOT, CACHE_DIR, CACHE_CSV, PLAYWRIGHT_PROFILE_DIR
    global HEADLESS, MAX_DYNAMIC_DISTANCE_DAYS, MAX_FRESH_SCANS_PER_CITY

    datetime.strptime(args.anchor, "%Y-%m-%d")
    CYCLE_NAME = clean(args.cycle)
    ANCHOR_DATE = clean(args.anchor)
    if not CYCLE_NAME:
        raise ValueError("--cycle cannot be blank.")

    if args.all_cities and (args.cities or args.city_limit is not None):
        raise ValueError("Use --all-cities OR --cities/--city-limit, not both.")
    if args.cities and args.city_limit is not None:
        raise ValueError("Use --cities OR --city-limit, not both.")
    if args.city_limit is not None and args.city_limit < 1:
        raise ValueError("--city-limit must be at least 1.")

    TEST_CITY_NAMES = list(args.cities or [])
    TEST_CITY_LIMIT = args.city_limit
    SUBSET_RUN_LABEL = clean(args.subset_label)

    INPUT_CSV = str(args.input_csv)
    OUTPUT_ROOT = Path(args.output_root)
    CACHE_DIR = OUTPUT_ROOT / "cache"
    CACHE_CSV = Path(args.cache) if args.cache is not None else CACHE_DIR / "ekadashi_scan_cache.csv"
    PLAYWRIGHT_PROFILE_DIR = Path(args.profile_dir)

    if args.headless:
        HEADLESS = True
    elif args.headed:
        HEADLESS = False

    if args.max_distance_days < 1:
        raise ValueError("--max-distance-days must be at least 1.")
    if args.max_fresh_scans < 1:
        raise ValueError("--max-fresh-scans must be at least 1.")
    MAX_DYNAMIC_DISTANCE_DAYS = int(args.max_distance_days)
    MAX_FRESH_SCANS_PER_CITY = int(args.max_fresh_scans)


def main() -> None:
    args = parse_cli_args()
    apply_cli_args(args)

    datetime.strptime(ANCHOR_DATE, "%Y-%m-%d")
    cities = load_cities()
    cycle_slug = slugify(CYCLE_NAME)
    run_dir, audit_csv, messages_csv, is_subset_run = resolve_cycle_output_paths(
        cycle_slug, cities
    )

    print("\n" + "#" * 96)
    print("INDEPENDENT EKADASHI CYCLE SCANNER v3.1 STANDARD FESTIVAL RUNS")
    print("#" * 96)
    print(f"Cycle : {CYCLE_NAME}")
    print(f"Anchor: {ANCHOR_DATE} (search anchor only; never used as the decision date)")
    print(f"Cities: {len(cities)}")
    print(f"Output: {'SUBSET / BACKFILL (canonical files preserved)' if is_subset_run else 'FULL CYCLE'}")
    print(f"Run dir: {run_dir}")
    print(f"Cache : {CACHE_CSV}")
    print("Artifact layout: festival_runs/YYYY/MM/ekadashi/<cycle>/")

    cache = ObservationCache(CACHE_CSV)
    audit_rows: list[dict[str, Any]] = []
    message_rows: list[dict[str, Any]] = []

    PLAYWRIGHT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context: BrowserContext = pw.chromium.launch_persistent_context(
            user_data_dir=str(PLAYWRIGHT_PROFILE_DIR),
            headless=HEADLESS,
            viewport={"width": 1600, "height": 950},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        try:
            for idx, (_, city_row) in enumerate(cities.iterrows(), start=1):
                city = clean(city_row.get("display_city", ""))
                print("\n" + "-" * 96)
                print(f"[{idx}/{len(cities)}] {city}")
                print("-" * 96)
                page = context.new_page()
                session = CitySession(city_row, page, cache)
                t: TransitionSet | None = None
                c: ClassificationResult | None = None
                p: ParanaResult | None = None
                try:
                    t = resolve_transitions(session)
                    ensure_classification_sunrises(session, t)
                    c = classify_cycle(session, t)
                    p = calculate_parana(session, t, c)
                    missing = validate_complete(session, t, c, p)
                    if missing:
                        audit_rows.append(build_audit_row(
                            session, t, c, p, "INCOMPLETE", missing=missing
                        ))
                    else:
                        audit_rows.append(build_audit_row(
                            session, t, c, p, "COMPLETE"
                        ))
                        message_rows.extend(build_message_rows(session, t, c, p))
                        print(
                            f"  => COMPLETE: {c.ekadashi_type}; "
                            f"Upavaasa={', '.join(c.upavasa_dates)}; "
                            f"Parana={c.parana_date} {fmt_public_time(p.start)}-{fmt_public_time(p.end)}"
                        )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    print(f"  => INCOMPLETE: {error}")
                    audit_rows.append(build_audit_row(
                        session, t, c, p, "INCOMPLETE", error=error
                    ))
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass

                reorder_audit_columns(pd.DataFrame(audit_rows)).to_csv(audit_csv, index=False, encoding="utf-8-sig")
                if message_rows:
                    pd.DataFrame(message_rows).sort_values(
                        ["Date", "City", "Action Role"], kind="stable"
                    ).to_csv(messages_csv, index=False, encoding="utf-8-sig")
        finally:
            context.close()
            cache.save()

    audit_df = pd.DataFrame(audit_rows)
    if not audit_df.empty:
        audit_df = reorder_audit_columns(audit_df)
        audit_df.to_csv(audit_csv, index=False, encoding="utf-8-sig")
    messages_df = pd.DataFrame(message_rows)
    if not messages_df.empty:
        messages_df = messages_df.sort_values(
            ["Date", "City", "Action Role"], kind="stable"
        )
        messages_df.to_csv(messages_csv, index=False, encoding="utf-8-sig")

    print_validation_summary(audit_df)
    print("\nFiles created:")
    print(f"  Audit   : {audit_csv}")
    print(f"  Messages: {messages_csv}")
    print(f"  Cache   : {CACHE_CSV}")
    print(f"\nCache hits this run : {cache.cache_hits}")
    print(f"Fresh page scans    : {cache.fresh_scans}")
    if is_subset_run:
        print("\nSubset run complete. To publish these city rows without replacing the")
        print("other cities in the cycle, use special_events_master_manager.py")
        print("with PUBLISH_MODE = 'SELECTED_PLACES_UPSERT' (or --mode selected).")


if __name__ == "__main__":
    main()
