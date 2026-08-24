from __future__ import annotations

"""
Month Festival Discovery
========================

Purpose
-------
Scan Drik Panchang's Month Panchang festival summary for every configured city,
match discovered festival names against festival_registry.csv, preserve each
city's displayed festival date, and generate:

1) Raw discovery CSV (all Month-Panchang festival entries)
2) Registry-matched discovery CSV (only festivals in our registry)
3) City scan-status CSV (resume/completeness support)
4) Festival job manifest CSV
5) PowerShell command file containing commands for execution-ready engines

Important date-strategy rule
----------------------------
* ANCHOR_SEARCH engines (Ekadashi, Krishna Jayanthi) use Drik's displayed date
  only to locate the candidate cycle. Their independent engine determines the
  final observance.
* CITY_SPECIFIC_EXACT_DATE engines (Grahana, Sankramana, Nag Panchami,
  Varamahalakshmi, Kalki Jayanthi) use each city's displayed discovery date
  directly.

This program DOES NOT modify special_events_master_YYYY.csv and DOES NOT run
festival engines.  It prepares the discovery evidence and recommended commands.

Typical production run
----------------------
    python festival_discovery/month_festival_discovery.py \
        --month 2026-08 --all-cities

Test selected cities
--------------------
    python festival_discovery/month_festival_discovery.py \
        --month 2026-08 --cities Pittsford Bengaluru Auckland

Force a fresh rescan
--------------------
    python festival_discovery/month_festival_discovery.py \
        --month 2026-08 --all-cities --refresh
"""

import argparse
import calendar
import csv
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urljoin

import pandas as pd
from playwright.sync_api import Page, sync_playwright


# =============================================================================
# Defaults
# =============================================================================

MONTH_PANCHANG_URL = "https://www.drikpanchang.com/panchang/month-panchang.html"
DEFAULT_INPUT_CSV = Path("cities_panchanga_updated.csv")
DEFAULT_REGISTRY_CSV = Path("festival_registry.csv")
DEFAULT_OUTPUT_ROOT = Path("festival_runs")
DEFAULT_PROFILE_DIR = Path("playwright_profile")

PAGE_LOAD_WAIT_MS = 8000
DEFAULT_BROWSER_RECYCLE_EVERY_CITIES = 10
AFTER_CITY_WAIT_MS = 7000
AFTER_DATE_WAIT_MS = 7000
BETWEEN_CITIES_MS = 2500
CAPTCHA_SAFE_WAIT_MS = 2500

DISCOVERY_SCHEMA_VERSION = "V7_EVENT_DETAIL_URL_CAPTURE"

WEEKDAYS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
}

ENGINE_SCRIPT_PATHS = {
    "EKADASHI": Path("festival_engines/ekadashi_cycle_scanner.py"),
    "KRISHNA_JAYANTHI": Path("festival_engines/krishna_jayanthi_engine.py"),
    "SANKRAMANA": Path("festival_engines/sankramana_engine.py"),
    "GRAHANA": Path("festival_engines/grahana_engine.py"),
    "NAG_PANCHAMI": Path("festival_engines/standard_festival_engine.py"),
    "VARAMAHALAKSHMI": Path("festival_engines/standard_festival_engine.py"),
    "KALKI_JAYANTHI": Path("festival_engines/standard_festival_engine.py"),
}

ANCHOR_SEARCH_ENGINES = {
    "EKADASHI",
    "KRISHNA_JAYANTHI",
}

CITY_SPECIFIC_EXACT_DATE_ENGINES = {
    "SANKRAMANA",
    "GRAHANA",
    "NAG_PANCHAMI",
    "VARAMAHALAKSHMI",
    "KALKI_JAYANTHI",
}


RAW_COLUMNS = [
    "Month",
    "Place Key",
    "Location Key",
    "City",
    "State/Region",
    "Country",
    "Timezone",
    "Geoname ID",
    "Displayed Date",
    "Displayed Day",
    "Displayed Weekday",
    "Observed Festival",
    "Normalized Festival",
    "Matched Pattern",
    "Canonical Festival",
    "Engine",
    "Discovery Enabled",
    "Execution Enabled",
    "Display Priority",
    "Registry Notes",
    "Drik URL",
    "Event Detail URL",
    "Event Link Status",
    "Discovery Schema Version",
]

STATUS_COLUMNS = [
    "Month",
    "Place Key",
    "Location Key",
    "City",
    "State/Region",
    "Country",
    "Timezone",
    "Geoname ID",
    "Scan Status",
    "Festival Count",
    "Registry Match Count",
    "Registry Detail URL Count",
    "Drik URL",
    "Discovery Schema Version",
    "Scan Error",
    "Scanned At",
]

JOB_COLUMNS = [
    "Month",
    "Job ID",
    "Event Key",
    "Representative Festival",
    "Canonical Festival",
    "Engine",
    "Execution Enabled",
    "Date Strategy",
    "Cities Discovered",
    "Expected Cities",
    "Discovery Coverage",
    "Min Displayed Date",
    "Max Displayed Date",
    "Displayed Date Spread Days",
    "Suggested Anchor",
    "Max Distance From Anchor",
    "Job Status",
    "Recommended Command",
    "Notes",
]

# =============================================================================
# Helpers
# =============================================================================


def clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", clean(value).lower()).strip("_")


def normalize_yes_no(value: Any) -> str:
    return "Yes" if clean(value).lower() in {"yes", "y", "true", "1"} else "No"


def get_geoname_id(row: pd.Series) -> str:
    raw = clean(row.get("geoname_id", ""))
    if re.fullmatch(r"\d+\.0+", raw):
        raw = raw.split(".", 1)[0]
    return raw


def place_key_for(row: pd.Series) -> str:
    geoname_id = get_geoname_id(row)
    if geoname_id:
        return f"geoname:{geoname_id}"
    city = clean(row.get("display_city", ""))
    state = clean(row.get("state_or_region", ""))
    country = clean(row.get("country", ""))
    return "location:" + slug("|".join([city, state, country]))


def location_key_for(row: pd.Series) -> str:
    return f"{place_key_for(row)}|city:{slug(row.get('display_city', ''))}"


def month_parts(month_text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-(\d{2})", clean(month_text))
    if not m:
        raise ValueError("--month must use YYYY-MM format, e.g. 2026-08")
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        raise ValueError("Month must be from 01 through 12")
    return year, month


def month_heading(year: int, month: int) -> str:
    return f"{calendar.month_name[month]} {year} Festivals"


def target_date_for_page(year: int, month: int) -> str:
    # Mid-month is intentionally used so Drik opens the requested month directly.
    return f"15/{month:02d}/{year:04d}"


def iso_date(year: int, month: int, day: int) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}"


def build_direct_url(geoname_id: str, year: int, month: int) -> str:
    return (
        f"{MONTH_PANCHANG_URL}?geoname-id={clean(geoname_id)}"
        f"&date={target_date_for_page(year, month)}"
    )



def selected_run_label(city_names: list[str]) -> str:
    names = [re.sub(r"[^a-z0-9]+", "_", clean(x).lower()).strip("_") or "selected" for x in city_names if clean(x)]
    if not names:
        return "selected"
    if len(names) <= 4:
        return "_".join(names)
    return "_".join(names[:3]) + f"_plus_{len(names) - 3}"


def quote_ps(value: str) -> str:
    return '"' + str(value).replace('"', '`"') + '"'


def write_csv_atomic(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})
    tmp.replace(path)


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


# =============================================================================
# Registry
# =============================================================================


@dataclass(frozen=True)
class RegistryRule:
    pattern: str
    canonical: str
    engine: str
    discovery_enabled: str
    execution_enabled: str
    display_priority: str
    notes: str

    @property
    def normalized_pattern(self) -> str:
        return normalize_token(self.pattern)


def load_registry(path: Path) -> list[RegistryRule]:
    if not path.exists():
        raise FileNotFoundError(f"Festival registry not found: {path}")

    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    required = {
        "Festival Pattern", "Canonical Festival", "Engine",
        "Discovery Enabled", "Execution Enabled", "Display Priority", "Notes",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Registry is missing column(s): {', '.join(missing)}")

    rules: list[RegistryRule] = []
    for _, row in df.iterrows():
        pattern = clean(row["Festival Pattern"])
        if not pattern:
            continue
        rules.append(
            RegistryRule(
                pattern=pattern,
                canonical=clean(row["Canonical Festival"]),
                engine=clean(row["Engine"]).upper(),
                discovery_enabled=normalize_yes_no(row["Discovery Enabled"]),
                execution_enabled=normalize_yes_no(row["Execution Enabled"]),
                display_priority=clean(row["Display Priority"]),
                notes=clean(row["Notes"]),
            )
        )

    # Longest patterns first prevents "Janmashtami" from winning over
    # "Krishna Janmashtami", etc.
    rules.sort(key=lambda r: len(r.normalized_pattern), reverse=True)
    return rules


def match_registry(event_name: str, rules: list[RegistryRule]) -> RegistryRule | None:
    normalized_event = normalize_token(event_name)
    if not normalized_event:
        return None

    for rule in rules:
        if rule.discovery_enabled != "Yes":
            continue
        pattern = rule.normalized_pattern
        if pattern and pattern in normalized_event:
            return rule
    return None


def refresh_registry_matches(
    raw_rows: list[dict[str, Any]],
    rules: list[RegistryRule],
) -> None:
    """Re-apply the current registry to cached raw discovery rows.

    This deliberately does NOT require another Drik scan.  For example, after
    we implement Sankramana we can change Execution Enabled from No to Yes in
    festival_registry.csv, rerun discovery, and immediately regenerate the job
    manifest/PowerShell commands from the already captured raw month list.
    """
    for row in raw_rows:
        observed = clean(row.get("Observed Festival", ""))
        rule = match_registry(observed, rules)
        row["Normalized Festival"] = normalize_token(observed)
        row["Matched Pattern"] = rule.pattern if rule else ""
        row["Canonical Festival"] = rule.canonical if rule else ""
        row["Engine"] = rule.engine if rule else ""
        row["Discovery Enabled"] = rule.discovery_enabled if rule else "No"
        row["Execution Enabled"] = rule.execution_enabled if rule else "No"
        row["Display Priority"] = rule.display_priority if rule else ""
        row["Registry Notes"] = rule.notes if rule else ""


# =============================================================================
# Browser / CAPTCHA / city helpers
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
        body = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        body = ""

    return any(marker in body for marker in [
        "i'm not a robot", "i am not a robot", "select all images",
        "verify you are human", "checking your browser", "just a moment",
        "attention required", "security check",
    ])


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
            print("Verification cleared. Resuming.\n")
            page.wait_for_timeout(CAPTCHA_SAFE_WAIT_MS)
            return
        print("Verification still appears to be present.")


def neutralize_ad_overlays(page: Page) -> None:
    try:
        page.evaluate(
            """
            () => {
                const id='dp-festival-discovery-ad-shield';
                if (!document.getElementById(id)) {
                    const style=document.createElement('style');
                    style.id=id;
                    style.textContent=`
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


def validate_geoname_page(page: Page, expected_geoname_id: str, city: str) -> None:
    if not expected_geoname_id:
        return
    query = parse_qs(urlparse(page.url).query)
    actual = clean((query.get("geoname-id") or [""])[0])
    if actual != clean(expected_geoname_id):
        raise RuntimeError(
            f"Wrong Drik location loaded for {city}: expected geoname-id="
            f"{expected_geoname_id}; URL={page.url}"
        )


def open_direct_page(page: Page, url: str, geoname_id: str, city: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            print(f"Opening Month Panchang: {city}")
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
            wait_for_manual_captcha(page, f"opening Month Panchang for {city}")
            validate_geoname_page(page, geoname_id, city)
            return
        except Exception as exc:
            last_error = exc
            retryable = any(x in str(exc) for x in [
                "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_TIMED_OUT",
                "Timeout", "Navigation timeout", "Page crashed",
            ])
            if not retryable or attempt == 3:
                raise
            print(f"Retrying ({attempt + 1}/3) after: {exc}")
            page.wait_for_timeout(5000)
    if last_error:
        raise last_error


def focus_and_clear_input(page: Page, locator) -> None:
    wait_for_manual_captcha(page, "waiting for Drik input")
    neutralize_ad_overlays(page)
    locator.wait_for(state="visible", timeout=30000)
    locator.evaluate(
        """
        element => {
            element.focus();
            element.value='';
            element.dispatchEvent(new Event('input',{bubbles:true}));
            element.dispatchEvent(new Event('change',{bubbles:true}));
        }
        """
    )


def normalize_location_piece(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


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
    return CITY_NAME_ALIASES.get(clean(search_city).lower(), [clean(search_city)])


def set_city(page: Page, search_city: str, state: str, country: str) -> str:
    aliases = get_city_aliases(search_city)
    queries: list[str] = []
    for alias in aliases:
        queries += [
            alias,
            f"{alias} {state}" if state else "",
            f"{alias} {state} {country}" if state and country else "",
            f"{alias} {country}" if country else "",
        ]
    queries = [q for q in dict.fromkeys(queries) if q]
    alias_norms = {normalize_location_piece(a) for a in aliases}
    last_error = ""

    for query in queries:
        city_input = page.locator("#dp-direct-city-search")
        focus_and_clear_input(page, city_input)
        page.keyboard.type(query, delay=150)
        page.wait_for_timeout(3000)

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
            if state and normalize_location_piece(state) in ntext:
                score += 50
            c_norm = normalize_location_piece(country)
            if c_norm and c_norm in ntext:
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
        return text

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


def set_page_date(page: Page, target_ddmmyyyy: str) -> None:
    date_input = find_date_input(page)
    if date_input.input_value() == target_ddmmyyyy:
        return
    focus_and_clear_input(page, date_input)
    page.keyboard.type(target_ddmmyyyy, delay=80)
    page.keyboard.press("Enter")
    page.wait_for_timeout(AFTER_DATE_WAIT_MS)
    wait_for_manual_captcha(page, f"changing date to {target_ddmmyyyy}")
    current = find_date_input(page).input_value()
    if current != target_ddmmyyyy:
        raise RuntimeError(
            f"Date did not update. Expected {target_ddmmyyyy}; got {current}"
        )



# =============================================================================
# Event-link capture
# =============================================================================


def normalize_event_name_for_link_matching(event_name: str) -> str:
    name = clean(event_name)
    name = re.sub(r"\s+Parana$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+Vrat$", "", name, flags=re.IGNORECASE)
    return name.strip()


def is_navigation_or_guide_link(text: str, href: str) -> bool:
    low_text = clean(text).lower()
    low_href = clean(href).lower()

    helper_phrases = [
        "compatibility report",
        "compatibility",
        "guide book",
        "guidebook",
        "festival guide",
        "festival dates",
        "festival calendar",
        "dates list",
    ]

    if any(phrase in low_text for phrase in helper_phrases):
        return True

    if (
        "festival-calendar" in low_href
        or "festival-dates" in low_href
        or "eclipse-dates" in low_href
    ):
        return True

    return False


def collect_visible_anchor_inventory(page: Page) -> list[dict[str, str]]:
    """
    Preserve actual visible event hrefs while the city/month page is open.
    This mirrors the proven old scanner architecture.
    """
    inventory: list[dict[str, str]] = []
    links = page.locator("a")

    for i in range(links.count()):
        link = links.nth(i)
        try:
            text = clean(link.inner_text())
            href = clean(link.get_attribute("href"))
            visible = link.is_visible()
        except Exception:
            continue

        if not text or not href or not visible:
            continue

        if is_navigation_or_guide_link(text, href):
            continue

        inventory.append({
            "text": text,
            "href": urljoin(MONTH_PANCHANG_URL, href),
        })

    return inventory


def choose_event_detail_url(
    event_name: str,
    anchor_inventory: list[dict[str, str]],
) -> str:
    """
    Choose the event href from the anchors captured on the same Month page.

    Exact/similar visible label matching follows the old scanner. URL-family
    affinity only helps disambiguate candidates.
    """
    target = normalize_event_name_for_link_matching(event_name).lower()
    if not target:
        return ""

    target_words = [
        word for word in re.split(r"\s+", target)
        if len(word) > 2
    ]

    target_is_eclipse = any(
        token in target
        for token in [
            "surya grahan", "solar eclipse",
            "chandra grahan", "lunar eclipse",
        ]
    )
    target_is_sankranti = (
        "sankranti" in target or "sankramana" in target
    )
    target_is_ekadashi = "ekadashi" in target

    candidates: list[tuple[int, str]] = []

    for item in anchor_inventory:
        text = clean(item.get("text", ""))
        href = clean(item.get("href", ""))
        if not text or not href:
            continue

        normalized = normalize_event_name_for_link_matching(text).lower()
        low_href = href.lower()
        score = 0

        if normalized == target:
            score += 100
        if target in normalized or normalized in target:
            score += 40

        for word in target_words:
            if word in normalized:
                score += 4

        if target_is_eclipse:
            if "/eclipse/" in low_href:
                score += 30
            if "date-time-duration" in low_href:
                score += 50

        if target_is_sankranti:
            if "sankranti" in low_href or "sankramana" in low_href:
                score += 40

        if target_is_ekadashi and "ekadashi" in low_href:
            score += 25

        if score > 0:
            candidates.append((score, href))

    if not candidates:
        return ""

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


# =============================================================================
# Month-festival summary parsing
# =============================================================================


def normalize_lines(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in str(text).splitlines()
        if line and line.strip()
    ]


def is_day_line(line: str) -> bool:
    if not re.fullmatch(r"\d{1,2}", clean(line)):
        return False
    day = int(clean(line))
    return 1 <= day <= 31


def split_event_names(lines: list[str]) -> list[str]:
    """Split one summary date block into individual festival names.

    Drik usually renders comma-separated links. Depending on viewport/browser,
    individual links can also land on separate text lines. Joining with commas
    makes both representations deterministic.
    """
    blob = ", ".join(clean(x) for x in lines if clean(x))
    if not blob:
        return []

    parts = [
        clean(part).strip(" ,;|")
        for part in re.split(r"\s*,\s*", blob)
        if clean(part).strip(" ,;|")
    ]

    # Keep order while removing duplicates.
    return list(dict.fromkeys(parts))


def extract_month_festival_summary(
    page_text: str,
    year: int,
    month: int,
) -> list[dict[str, str]]:
    heading = month_heading(year, month)
    lower = page_text.lower()
    pos = lower.find(heading.lower())
    if pos < 0:
        raise RuntimeError(f"Could not find Month Festival summary heading: {heading}")

    section = page_text[pos + len(heading):]
    stop_candidates = [
        "Hindu calendar",
        "Hindu Calendar",
        "Lunar Month List",
        "Nakshatra List",
    ]
    stop_positions = [
        section.lower().find(marker.lower())
        for marker in stop_candidates
        if section.lower().find(marker.lower()) >= 0
    ]
    if stop_positions:
        section = section[:min(stop_positions)]

    lines = normalize_lines(section)
    records: list[dict[str, str]] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if not is_day_line(line):
            i += 1
            continue

        # The next line must be a weekday; this protects against stray numbers.
        if i + 1 >= len(lines) or clean(lines[i + 1]).lower() not in WEEKDAYS:
            i += 1
            continue

        day = int(line)
        weekday = clean(lines[i + 1])
        i += 2
        event_lines: list[str] = []

        while i < len(lines):
            if (
                is_day_line(lines[i])
                and i + 1 < len(lines)
                and clean(lines[i + 1]).lower() in WEEKDAYS
            ):
                break
            event_lines.append(lines[i])
            i += 1

        for event_name in split_event_names(event_lines):
            records.append({
                "Displayed Day": f"{day:02d}",
                "Displayed Date": iso_date(year, month, day),
                "Displayed Weekday": weekday,
                "Observed Festival": event_name,
            })

    if not records:
        raise RuntimeError(
            f"Found heading {heading!r}, but no festival summary rows could be parsed."
        )
    return records


# =============================================================================
# Discovery scan
# =============================================================================


def select_cities(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if args.cities:
        wanted = {clean(x).lower() for x in args.cities}
        selected = df[df["display_city"].map(lambda x: clean(x).lower() in wanted)].copy()
        found = {clean(x).lower() for x in selected["display_city"]}
        missing = sorted(wanted - found)
        if missing:
            raise ValueError(
                "Unknown display_city name(s): " + ", ".join(missing)
            )
        return selected
    return df.copy()


def scan_one_city(
    page: Page,
    row: pd.Series,
    year: int,
    month: int,
    registry: list[RegistryRule],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    city = clean(row.get("display_city", ""))
    search_city = clean(row.get("search_city", city))
    state = clean(row.get("state_or_region", ""))
    country = clean(row.get("country", ""))
    timezone = clean(row.get("timezone", ""))
    geoname_id = get_geoname_id(row)
    place_key = place_key_for(row)
    location_key = location_key_for(row)
    month_id = f"{year:04d}-{month:02d}"
    drik_url = ""

    try:
        if geoname_id:
            drik_url = build_direct_url(geoname_id, year, month)
            open_direct_page(page, drik_url, geoname_id, city)
        else:
            page.goto(MONTH_PANCHANG_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
            wait_for_manual_captcha(page, f"opening Month Panchang for {city}")
            set_city(page, search_city, state, country)
            set_page_date(page, target_date_for_page(year, month))
            drik_url = page.url

        body = page.locator("body").inner_text(timeout=30000)
        parsed = extract_month_festival_summary(body, year, month)

        # Capture links once, now, while this exact city/month is loaded.
        anchor_inventory = collect_visible_anchor_inventory(page)

        raw_rows: list[dict[str, Any]] = []
        match_count = 0
        registry_detail_url_count = 0
        for item in parsed:
            observed = clean(item["Observed Festival"])
            rule = match_registry(observed, registry)
            if rule:
                match_count += 1

            event_detail_url = choose_event_detail_url(
                observed,
                anchor_inventory,
            )
            event_link_status = (
                "CAPTURED"
                if event_detail_url
                else "NO_CLICKABLE_DETAIL_LINK"
            )
            if rule and event_detail_url:
                registry_detail_url_count += 1

            raw_rows.append({
                "Month": month_id,
                "Place Key": place_key,
                "Location Key": location_key,
                "City": city,
                "State/Region": state,
                "Country": country,
                "Timezone": timezone,
                "Geoname ID": geoname_id,
                "Displayed Date": item["Displayed Date"],
                "Displayed Day": item["Displayed Day"],
                "Displayed Weekday": item["Displayed Weekday"],
                "Observed Festival": observed,
                "Normalized Festival": normalize_token(observed),
                "Matched Pattern": rule.pattern if rule else "",
                "Canonical Festival": rule.canonical if rule else "",
                "Engine": rule.engine if rule else "",
                "Discovery Enabled": rule.discovery_enabled if rule else "No",
                "Execution Enabled": rule.execution_enabled if rule else "No",
                "Display Priority": rule.display_priority if rule else "",
                "Registry Notes": rule.notes if rule else "",
                "Drik URL": drik_url,
                "Event Detail URL": event_detail_url,
                "Event Link Status": event_link_status,
                "Discovery Schema Version": DISCOVERY_SCHEMA_VERSION,
            })

        status = {
            "Month": month_id,
            "Place Key": place_key,
            "Location Key": location_key,
            "City": city,
            "State/Region": state,
            "Country": country,
            "Timezone": timezone,
            "Geoname ID": geoname_id,
            "Scan Status": "COMPLETE",
            "Festival Count": len(parsed),
            "Registry Match Count": match_count,
            "Registry Detail URL Count": registry_detail_url_count,
            "Drik URL": drik_url,
            "Discovery Schema Version": DISCOVERY_SCHEMA_VERSION,
            "Scan Error": "",
            "Scanned At": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        return raw_rows, status

    except Exception as exc:
        status = {
            "Month": month_id,
            "Place Key": place_key,
            "Location Key": location_key,
            "City": city,
            "State/Region": state,
            "Country": country,
            "Timezone": timezone,
            "Geoname ID": geoname_id,
            "Scan Status": "INCOMPLETE",
            "Festival Count": 0,
            "Registry Match Count": 0,
            "Registry Detail URL Count": 0,
            "Drik URL": drik_url or page.url,
            "Discovery Schema Version": DISCOVERY_SCHEMA_VERSION,
            "Scan Error": f"{type(exc).__name__}: {exc}",
            "Scanned At": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        print(f"WARNING: {city}: {status['Scan Error']}")
        return [], status




# =============================================================================
# Browser stability / crash recovery
# =============================================================================

BROWSER_CRASH_MARKERS = [
    "page crashed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "context closed",
    "target closed",
    "crash",
]


def is_browser_crash_error(value: Any) -> bool:
    text = clean(value).lower()
    return any(marker in text for marker in BROWSER_CRASH_MARKERS)


def close_context_quietly(context: Any) -> None:
    if context is None:
        return
    try:
        context.close()
    except Exception:
        pass


def launch_discovery_context(playwright: Any, args: argparse.Namespace, headless: bool):
    """
    Start a fresh persistent context.

    Periodic recycling is intentional. Drik's Month Panchang loads substantial
    script/ad content and a single Chromium renderer can accumulate memory over
    dozens of city navigations.
    """
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(args.profile_dir),
        headless=headless,
        viewport={"width": 1440, "height": 1000},
    )
    page = context.pages[0] if context.pages else context.new_page()
    return context, page


# =============================================================================
# Job/command generation
# =============================================================================


def normalize_ekadashi_cycle_name(observed: str) -> str:
    """
    Collapse Drik observance labels that belong to the same underlying
    Ekadashi cycle.

    Examples:
        Kamika Ekadashi
        Gauna Kamika Ekadashi
        Vaishnava Kamika Ekadashi
            -> Kamika Ekadashi

        Shravana Putrada Ekadashi
        Vaishnava Shravana Putrada Ekadashi
            -> Shravana Putrada Ekadashi

    Our independent Ekadashi engine determines the local Normal/Gauna/
    Vaishnava/Dashami-Viddha/etc. condition, so these must NOT become
    separate engine runs.
    """
    value = clean(observed)
    value = re.sub(
        r"^(?:Gauna|Vaishnava)\s+",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    return value


def event_key_for(row: dict[str, Any]) -> str:
    engine = clean(row.get("Engine", "")).upper()
    observed = clean(row.get("Observed Festival", ""))
    canonical = clean(row.get("Canonical Festival", ""))

    if engine == "EKADASHI":
        return slug(normalize_ekadashi_cycle_name(observed))
    if engine == "KRISHNA_JAYANTHI":
        # Different Drik naming variants are one independent festival engine/job.
        return "sri_krishna_jayanthi"
    return slug(canonical or observed)


def choose_anchor(date_values: list[str]) -> tuple[str, str, str, int, int]:
    dates = sorted(datetime.strptime(x, "%Y-%m-%d").date() for x in set(date_values))
    min_d, max_d = dates[0], dates[-1]
    spread = (max_d - min_d).days

    # Prefer the most common displayed date when it is reasonably central.
    counts = Counter(date_values)
    mode_date_str, _ = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    mode_d = datetime.strptime(mode_date_str, "%Y-%m-%d").date()

    midpoint = min_d + timedelta(days=spread // 2)
    if max(abs((mode_d - min_d).days), abs((max_d - mode_d).days)) <= max(
        abs((midpoint - min_d).days), abs((max_d - midpoint).days)
    ):
        anchor = mode_d
    else:
        anchor = midpoint

    max_distance = max(abs((d - anchor).days) for d in dates)
    return (
        min_d.isoformat(), max_d.isoformat(), anchor.isoformat(), spread, max_distance
    )


def get_date_strategy(engine: str) -> str:
    engine = clean(engine).upper()
    if engine in ANCHOR_SEARCH_ENGINES:
        return "ANCHOR_SEARCH"
    if engine in CITY_SPECIFIC_EXACT_DATE_ENGINES:
        return "CITY_SPECIFIC_EXACT_DATE"
    return "UNKNOWN"


def build_command(
    engine: str,
    representative: str,
    canonical: str,
    month_id: str,
    anchor: str,
    max_distance: int,
    all_cities: bool,
    selected_city_names: list[str],
) -> tuple[str, str]:
    engine = engine.upper()
    script = ENGINE_SCRIPT_PATHS.get(engine)
    if not script:
        return "", "ENGINE_NOT_IMPLEMENTED"

    if all_cities:
        city_arg = "--all-cities"
    else:
        city_arg = "--cities " + " ".join(quote_ps(x) for x in selected_city_names)

    if engine == "EKADASHI":
        if max_distance > 6:
            return "", "ANCHOR_SPREAD_TOO_WIDE"
        command = (
            f"python {quote_ps(str(script))} "
            f"--cycle {quote_ps(representative)} "
            f"--anchor {anchor} "
            f"{city_arg}"
        )
        return command, "READY"

    if engine == "KRISHNA_JAYANTHI":
        window_days = max(2, max_distance + 1)
        command = (
            f"python {quote_ps(str(script))} "
            f"--month {month_id} "
            f"--anchor {anchor} --window-days {window_days} "
            f"{city_arg}"
        )
        return command, "READY"

    if engine == "SANKRAMANA":
        command = (
            f"python {quote_ps(str(script))} "
            f"--month {month_id} --canonical {quote_ps(canonical)} {city_arg}"
        )
        return command, "READY"

    if engine == "GRAHANA":
        command = (
            f"python {quote_ps(str(script))} "
            f"--month {month_id} --canonical {quote_ps(canonical)} {city_arg}"
        )
        return command, "READY"

    if engine in {"NAG_PANCHAMI", "VARAMAHALAKSHMI", "KALKI_JAYANTHI"}:
        command = (
            f"python {quote_ps(str(script))} "
            f"--month {month_id} --engine-key {engine} "
            f"--canonical {quote_ps(canonical)} {city_arg}"
        )
        return command, "READY"

    return "", "ENGINE_NOT_IMPLEMENTED"

def generate_jobs(
    matched_rows: list[dict[str, Any]],
    selected_city_names: list[str],
    all_cities: bool,
    discovery_complete: bool,
    month_id: str,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in matched_rows:
        engine = clean(row.get("Engine", "")).upper()
        key = event_key_for(row)
        groups.setdefault((engine, key), []).append(row)

    jobs: list[dict[str, Any]] = []
    expected_count = len(selected_city_names)

    for (engine, event_key), rows in sorted(groups.items()):
        observed_names = [clean(r.get("Observed Festival", "")) for r in rows]
        if engine == "EKADASHI":
            normalized_cycle_names = [
                normalize_ekadashi_cycle_name(name)
                for name in observed_names
            ]
            representative = Counter(normalized_cycle_names).most_common(1)[0][0]
        else:
            representative = Counter(observed_names).most_common(1)[0][0]
        canonical = clean(rows[0].get("Canonical Festival", ""))
        execution_enabled = normalize_yes_no(rows[0].get("Execution Enabled", "No"))
        date_strategy = get_date_strategy(engine)

        city_count = len({clean(r.get("Location Key", "")) for r in rows})
        dates = [
            clean(r.get("Displayed Date", ""))
            for r in rows
            if clean(r.get("Displayed Date", ""))
        ]

        min_date, max_date, computed_anchor, spread, max_distance = choose_anchor(dates)

        if date_strategy == "ANCHOR_SEARCH":
            anchor = computed_anchor
            command_max_distance = max_distance
        else:
            anchor = ""
            command_max_distance = 0

        command = ""
        status = ""
        notes: list[str] = []

        if date_strategy == "CITY_SPECIFIC_EXACT_DATE":
            notes.append(
                "Uses each city's exact Displayed Date from the monthly discovery CSV; "
                "no global anchor is used."
            )
        elif date_strategy == "ANCHOR_SEARCH":
            notes.append(
                "Month-Panchang dates are used only to locate the candidate cycle; "
                "the festival engine independently determines the final observance."
            )
        else:
            notes.append("Date strategy is not defined for this engine.")

        if not discovery_complete:
            status = "DISCOVERY_INCOMPLETE"
            notes.append(
                "At least one selected city has not completed Month-Panchang discovery."
            )
        elif execution_enabled != "Yes":
            status = "ENGINE_NOT_IMPLEMENTED"
            notes.append(
                "Festival is discovered and registered, but execution is disabled "
                "until its engine is developed."
            )
        elif date_strategy == "UNKNOWN":
            status = "DATE_STRATEGY_NOT_DEFINED"
        else:
            command, status = build_command(
                engine=engine,
                representative=representative,
                canonical=canonical,
                month_id=month_id,
                anchor=anchor,
                max_distance=command_max_distance,
                all_cities=all_cities,
                selected_city_names=selected_city_names,
            )

        if city_count < expected_count:
            notes.append(
                f"This event was listed in {city_count}/{expected_count} selected cities "
                "for the target month. This can be legitimate near civil-month "
                "boundaries; review before assuming absence in other cities."
            )

        jobs.append({
            "Month": month_id,
            "Job ID": f"{month_id}_{engine.lower()}_{event_key}",
            "Event Key": event_key,
            "Representative Festival": representative,
            "Canonical Festival": canonical,
            "Engine": engine,
            "Execution Enabled": execution_enabled,
            "Date Strategy": date_strategy,
            "Cities Discovered": city_count,
            "Expected Cities": expected_count,
            "Discovery Coverage": f"{city_count}/{expected_count}",
            "Min Displayed Date": min_date,
            "Max Displayed Date": max_date,
            "Displayed Date Spread Days": spread,
            "Suggested Anchor": anchor,
            "Max Distance From Anchor": (
                max_distance if date_strategy == "ANCHOR_SEARCH" else ""
            ),
            "Job Status": status,
            "Recommended Command": command,
            "Notes": " ".join(notes),
        })

    return jobs

def write_command_script(
    path: Path,
    jobs: list[dict[str, Any]],
    discovery_complete: bool,
) -> None:
    lines = [
        "# Generated by month_festival_discovery.py",
        "#",
        "# Date strategies:",
        "#   ANCHOR_SEARCH             -> Month-Panchang date is only a candidate-cycle anchor.",
        "#   CITY_SPECIFIC_EXACT_DATE  -> Each city's discovered date is used directly.",
        "",
    ]

    if not discovery_complete:
        lines += [
            "# WARNING: discovery is incomplete for one or more selected cities.",
            "# No commands are enabled until discovery is complete.",
            "",
        ]

    for job in jobs:
        label = f"{job['Representative Festival']} -> {job['Engine']}"
        strategy = clean(job.get("Date Strategy", ""))

        lines.append(f"# {label}")
        lines.append(
            f"# Strategy: {strategy} | "
            f"Displayed dates: {job['Min Displayed Date']} to {job['Max Displayed Date']} | "
            f"Coverage: {job['Discovery Coverage']} | "
            f"Status: {job['Job Status']}"
        )

        if strategy == "ANCHOR_SEARCH":
            lines.append(
                f"# Suggested anchor: {job['Suggested Anchor']} | "
                f"Max distance from anchor: {job['Max Distance From Anchor']} day(s)"
            )
        elif strategy == "CITY_SPECIFIC_EXACT_DATE":
            lines.append(
                "# No anchor: the engine reads each city's exact Displayed Date "
                "from festival_discovery CSV."
            )

        if job["Job Status"] == "READY" and clean(job["Recommended Command"]):
            lines.append(clean(job["Recommended Command"]))
        else:
            lines.append(f"# NOT RUNNABLE YET: {job['Job Status']}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8-sig",
    )


# =============================================================================
# CLI / main
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Discover Month-Panchang festivals city-by-city and prepare festival-engine jobs."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  Production discovery for all configured cities:\n"
            "    python festival_discovery/month_festival_discovery.py --month 2026-08 --all-cities\n\n"
            "  Test selected cities:\n"
            "    python festival_discovery/month_festival_discovery.py --month 2026-08 "
            "--cities Pittsford Bengaluru Auckland\n\n"
            "  Force rescan even if COMPLETE city-status rows already exist:\n"
            "    python festival_discovery/month_festival_discovery.py --month 2026-08 "
            "--all-cities --refresh"
        ),
    )
    parser.add_argument("--month", required=True, metavar="YYYY-MM")

    city_group = parser.add_mutually_exclusive_group(required=True)
    city_group.add_argument("--all-cities", action="store_true")
    city_group.add_argument("--cities", nargs="+", metavar="CITY")

    parser.add_argument(
        "--input-csv", type=Path, default=DEFAULT_INPUT_CSV,
        help=f"City CSV (default: {DEFAULT_INPUT_CSV}).",
    )
    parser.add_argument(
        "--registry", type=Path, default=DEFAULT_REGISTRY_CSV,
        help=f"Festival registry CSV (default: {DEFAULT_REGISTRY_CSV}).",
    )
    parser.add_argument(
        "--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT,
        help=f"Festival run root (default: {DEFAULT_OUTPUT_ROOT}).",
    )
    parser.add_argument(
        "--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR,
        help=f"Persistent Playwright profile (default: {DEFAULT_PROFILE_DIR}).",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Ignore COMPLETE resume status and rescan selected cities.",
    )

    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument("--headless", action="store_true")
    browser_group.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--recycle-every",
        type=int,
        default=DEFAULT_BROWSER_RECYCLE_EVERY_CITIES,
        help=(
            "Recycle the persistent Chromium context after this many city scans "
            f"(default: {DEFAULT_BROWSER_RECYCLE_EVERY_CITIES}). "
            "Use 0 to disable periodic recycling."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    year, month = month_parts(args.month)
    month_id = f"{year:04d}-{month:02d}"

    if not args.input_csv.exists():
        raise FileNotFoundError(f"City CSV not found: {args.input_csv}")

    city_df = pd.read_csv(
        args.input_csv, dtype=str, keep_default_na=False, encoding="utf-8-sig"
    )
    required_city_cols = {
        "display_city", "search_city", "state_or_region", "country", "timezone"
    }
    missing = sorted(required_city_cols - set(city_df.columns))
    if missing:
        raise ValueError(f"City CSV is missing column(s): {', '.join(missing)}")

    selected_df = select_cities(city_df, args)
    if selected_df.empty:
        raise ValueError("No cities selected.")

    selected_city_names = [clean(x) for x in selected_df["display_city"].tolist()]
    selected_location_keys = {
        location_key_for(row) for _, row in selected_df.iterrows()
    }
    registry = load_registry(args.registry)

    month_dir = args.output_root / str(year) / f"{month:02d}"
    month_dir.mkdir(parents=True, exist_ok=True)

    raw_csv = month_dir / f"festival_discovery_raw_{month_id.replace('-', '_')}.csv"
    matched_csv = month_dir / f"festival_discovery_{month_id.replace('-', '_')}.csv"
    status_csv = month_dir / f"festival_discovery_status_{month_id.replace('-', '_')}.csv"
    jobs_csv = month_dir / f"festival_jobs_{month_id.replace('-', '_')}.csv"
    commands_ps1 = month_dir / f"run_supported_festivals_{month_id.replace('-', '_')}.ps1"

    raw_rows: list[dict[str, Any]] = load_csv_rows(raw_csv)
    status_rows: list[dict[str, Any]] = load_csv_rows(status_csv)

    # Keep only target-month content if files were manually combined/moved.
    raw_rows = [r for r in raw_rows if clean(r.get("Month")) == month_id]
    status_rows = [r for r in status_rows if clean(r.get("Month")) == month_id]

    status_by_key = {clean(r.get("Location Key")): r for r in status_rows}

    to_scan: list[tuple[int, pd.Series]] = []
    for idx, row in selected_df.iterrows():
        key = location_key_for(row)
        old = status_by_key.get(key)
        if (
            not args.refresh
            and old
            and clean(old.get("Scan Status", "")).upper() == "COMPLETE"
            and clean(old.get("Discovery Schema Version", ""))
                == DISCOVERY_SCHEMA_VERSION
        ):
            print(
                f"RESUME: skipping COMPLETE city "
                f"{clean(row.get('display_city', ''))}"
            )
            continue

        if (
            not args.refresh
            and old
            and clean(old.get("Scan Status", "")).upper() == "COMPLETE"
            and clean(old.get("Discovery Schema Version", ""))
                != DISCOVERY_SCHEMA_VERSION
        ):
            print(
                f"SCHEMA UPGRADE: rescanning "
                f"{clean(row.get('display_city', ''))} once to capture event URLs."
            )
        to_scan.append((idx, row))

    headless = bool(args.headless and not args.headed)

    print("=" * 100)
    print("MONTH FESTIVAL DISCOVERY")
    print(f"Month: {month_id}")
    print(f"Selected cities: {len(selected_df)}")
    print(f"Cities requiring scan: {len(to_scan)}")
    print(f"Registry: {args.registry}")
    print(f"Output folder: {month_dir}")
    print("=" * 100)

    if args.recycle_every < 0:
        raise ValueError("--recycle-every must be 0 or greater")

    if to_scan:
        with sync_playwright() as p:
            context = None
            page = None
            cities_since_browser_restart = 0

            try:
                context, page = launch_discovery_context(p, args, headless)

                for n, (_, row) in enumerate(to_scan, start=1):
                    city = clean(row.get("display_city", ""))
                    key = location_key_for(row)
                    print(f"\n[{n}/{len(to_scan)}] {city}")

                    # Proactive renderer recycling prevents a long all-city run
                    # from accumulating too much memory in one Chromium process.
                    if (
                        args.recycle_every > 0
                        and cities_since_browser_restart >= args.recycle_every
                    ):
                        print(
                            f"BROWSER RECYCLE: restarting Chromium after "
                            f"{cities_since_browser_restart} city scan(s)."
                        )
                        close_context_quietly(context)
                        context, page = launch_discovery_context(p, args, headless)
                        cities_since_browser_restart = 0

                    city_raw, city_status = scan_one_city(
                        page=page,
                        row=row,
                        year=year,
                        month=month,
                        registry=registry,
                    )

                    # A renderer crash poisons the Page object. Restart the
                    # persistent context and retry THIS SAME CITY once rather
                    # than allowing every subsequent city to fail.
                    if (
                        clean(city_status.get("Scan Status", "")).upper() != "COMPLETE"
                        and is_browser_crash_error(city_status.get("Scan Error", ""))
                    ):
                        print(
                            f"BROWSER CRASH RECOVERY: {city}. "
                            "Restarting Chromium and retrying this city once."
                        )
                        close_context_quietly(context)
                        context, page = launch_discovery_context(p, args, headless)
                        cities_since_browser_restart = 0

                        city_raw, city_status = scan_one_city(
                            page=page,
                            row=row,
                            year=year,
                            month=month,
                            registry=registry,
                        )

                    # Replace this city's previous raw/status rows atomically.
                    # This means an INCOMPLETE row from an earlier crashed run
                    # is cleanly replaced when the retry succeeds.
                    raw_rows = [
                        r for r in raw_rows
                        if clean(r.get("Location Key")) != key
                    ]
                    status_rows = [
                        r for r in status_rows
                        if clean(r.get("Location Key")) != key
                    ]
                    raw_rows.extend(city_raw)
                    status_rows.append(city_status)

                    write_csv_atomic(raw_csv, raw_rows, RAW_COLUMNS)
                    write_csv_atomic(status_csv, status_rows, STATUS_COLUMNS)

                    print(
                        f"{city}: {city_status['Scan Status']} | "
                        f"festivals={city_status['Festival Count']} | "
                        f"registry matches={city_status['Registry Match Count']} | "
                        f"detail URLs={city_status['Registry Detail URL Count']}"
                    )

                    cities_since_browser_restart += 1

                    # Do not call wait_for_timeout on a crashed renderer.
                    if (
                        page is not None
                        and clean(city_status.get("Scan Status", "")).upper() == "COMPLETE"
                    ):
                        try:
                            page.wait_for_timeout(BETWEEN_CITIES_MS)
                        except Exception as exc:
                            if is_browser_crash_error(exc):
                                print(
                                    "Browser became unavailable after the city "
                                    "completed; it will be restarted before the "
                                    "next city."
                                )
                                close_context_quietly(context)
                                context, page = launch_discovery_context(
                                    p, args, headless
                                )
                                cities_since_browser_restart = 0
                            else:
                                raise
            finally:
                close_context_quietly(context)

    # Re-apply the CURRENT registry even to cached raw rows.  Registry changes
    # therefore do not require a fresh Drik scan.
    refresh_registry_matches(raw_rows, registry)
    write_csv_atomic(raw_csv, raw_rows, RAW_COLUMNS)

    # Canonical discovery remains a complete month-level dataset across every
    # city already scanned into raw_rows. A selected-city/backfill run therefore
    # EXPANDS the canonical discovery; it never replaces it with only the subset.
    all_matched_rows = [
        r for r in raw_rows
        if normalize_yes_no(r.get("Discovery Enabled", "No")) == "Yes"
        and clean(r.get("Engine", ""))
    ]
    write_csv_atomic(matched_csv, all_matched_rows, RAW_COLUMNS)

    # Jobs for this invocation are limited to the selected scope.
    selected_raw = [
        r for r in raw_rows if clean(r.get("Location Key")) in selected_location_keys
    ]
    matched_rows = [
        r for r in selected_raw
        if normalize_yes_no(r.get("Discovery Enabled", "No")) == "Yes"
        and clean(r.get("Engine", ""))
    ]

    selected_status = {
        clean(r.get("Location Key")): r
        for r in status_rows
        if clean(r.get("Location Key")) in selected_location_keys
    }
    incomplete_keys = [
        key for key in selected_location_keys
        if key not in selected_status
        or clean(selected_status[key].get("Scan Status", "")).upper() != "COMPLETE"
    ]
    discovery_complete = not incomplete_keys

    jobs = generate_jobs(
        matched_rows=matched_rows,
        selected_city_names=selected_city_names,
        all_cities=bool(args.all_cities),
        discovery_complete=discovery_complete,
        month_id=month_id,
    )

    if args.all_cities:
        jobs_target = jobs_csv
        commands_target = commands_ps1
    else:
        subset_dir = (
            month_dir
            / "subset_runs"
            / selected_run_label(selected_city_names)
        )
        subset_dir.mkdir(parents=True, exist_ok=True)
        jobs_target = subset_dir / jobs_csv.name
        commands_target = subset_dir / commands_ps1.name

    write_csv_atomic(jobs_target, jobs, JOB_COLUMNS)
    write_command_script(commands_target, jobs, discovery_complete)

    print("\n" + "=" * 100)
    print("DISCOVERY SUMMARY")
    print(f"Complete cities: {len(selected_location_keys) - len(incomplete_keys)}/{len(selected_location_keys)}")
    print(f"Raw festival rows: {len(selected_raw)}")
    print(f"Registry-matched rows: {len(matched_rows)}")
    print(f"Festival jobs: {len(jobs)}")
    print(f"Runnable jobs: {sum(1 for j in jobs if j['Job Status'] == 'READY')}")
    print(f"Raw CSV: {raw_csv}")
    print(f"Matched discovery CSV: {matched_csv}")
    print(f"Status CSV: {status_csv}")
    print(f"Jobs CSV: {jobs_target}")
    print(f"Recommended commands: {commands_target}")

    if incomplete_keys:
        print("\nINCOMPLETE DISCOVERY - commands are intentionally disabled.")
        for key in sorted(incomplete_keys):
            row = selected_status.get(key, {})
            print(f"  - {clean(row.get('City', key))}: {clean(row.get('Scan Error', 'not scanned'))}")
    else:
        print("\nDiscovery is COMPLETE for the selected city set.")
        for job in jobs:
            strategy = clean(job.get("Date Strategy", ""))
            if strategy == "ANCHOR_SEARCH":
                date_info = f"anchor={job['Suggested Anchor']}"
            elif strategy == "CITY_SPECIFIC_EXACT_DATE":
                date_info = (
                    f"city-dates={job['Min Displayed Date']}.."
                    f"{job['Max Displayed Date']}"
                )
            else:
                date_info = "date-strategy=UNKNOWN"

            print(
                f"  {job['Representative Festival']} | {job['Engine']} | "
                f"{strategy} | {date_info} | {job['Job Status']}"
            )
    print("=" * 100)


if __name__ == "__main__":
    main()
