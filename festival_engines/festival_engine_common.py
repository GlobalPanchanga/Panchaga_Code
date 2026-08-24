from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, parse_qs, urlparse
import re

import pandas as pd
from playwright.sync_api import Page

MONTH_PANCHANG_URL = "https://www.drikpanchang.com/panchang/month-panchang.html"

PAGE_LOAD_WAIT_MS = 8000
DETAIL_PAGE_WAIT_MS = 8000
CAPTCHA_SAFE_WAIT_MS = 2500

def clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return re.sub(r"\s+", " ", str(value)).strip()

def slugify(value: str) -> str:
    value = clean(value).lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value or "event"

def normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean(value).lower())

def quote_display_date(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%b-%Y")

def get_geoname_id(row: pd.Series | dict[str, Any]) -> str:
    raw = clean(row.get("Geoname ID", row.get("geoname_id", "")))
    if re.fullmatch(r"\d+\.0+", raw):
        raw = raw.split(".", 1)[0]
    return raw

def yyyy_mm_dd_to_dd_mm_yyyy(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m/%Y")

def build_month_url(geoname_id: str, date_str: str) -> str:
    return (
        f"{MONTH_PANCHANG_URL}?geoname-id={clean(geoname_id)}"
        f"&date={yyyy_mm_dd_to_dd_mm_yyyy(date_str)}"
    )

def default_discovery_path(month_id: str) -> Path:
    year, month = month_id.split("-")
    return (
        Path("festival_runs")
        / year
        / month
        / f"festival_discovery_{year}_{month}.csv"
    )

def load_discovery(
    month_id: str,
    engine_key: str,
    canonical: str,
    discovery_file: str | None,
    all_cities: bool,
    cities: list[str] | None,
) -> pd.DataFrame:
    path = Path(discovery_file) if discovery_file else default_discovery_path(month_id)
    if not path.exists():
        raise FileNotFoundError(
            f"Discovery CSV not found: {path}\n"
            "Run month_festival_discovery.py first, or pass --discovery-file."
        )

    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    required = {
        "Month", "Place Key", "Location Key", "City", "State/Region", "Country",
        "Timezone", "Geoname ID", "Displayed Date", "Observed Festival",
        "Canonical Festival", "Engine",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError("Discovery CSV missing columns: " + ", ".join(missing))

    month_mask = df["Month"].map(clean) == month_id
    engine_mask = df["Engine"].map(lambda x: clean(x).upper()) == engine_key.upper()
    canonical_mask = (
        df["Canonical Festival"].map(normalize_token) == normalize_token(canonical)
    )
    selected = df.loc[month_mask & engine_mask & canonical_mask].copy()

    if not all_cities:
        wanted = {clean(x).lower() for x in (cities or []) if clean(x)}
        if not wanted:
            raise ValueError("Specify --all-cities or --cities ...")
        selected = selected[
            selected["City"].map(lambda x: clean(x).lower()).isin(wanted)
        ].copy()

    selected = selected.drop_duplicates(
        subset=["Location Key", "Displayed Date", "Observed Festival"],
        keep="first",
    )

    if selected.empty:
        raise ValueError(
            f"No discovery rows found for month={month_id}, engine={engine_key}, "
            f"canonical={canonical!r}."
        )

    return selected

def event_output_dir(month_id: str, family_slug: str, event_name: str) -> Path:
    year, month = month_id.split("-")
    path = Path("festival_runs") / year / month / family_slug / slugify(event_name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def selected_run_label(cities: list[str] | None) -> str:
    """Stable label for a selected-city/backfill run."""
    names = [slugify(x) for x in (cities or []) if clean(x)]
    if not names:
        return "selected"
    if len(names) <= 4:
        return "_".join(names)
    return "_".join(names[:3]) + f"_plus_{len(names) - 3}"


def selected_event_output_dir(
    month_id: str,
    family_slug: str,
    event_name: str,
    cities: list[str] | None,
) -> Path:
    """
    Full runs keep the canonical event folder.
    Selected-city runs are isolated under subset_runs/<label>/ so they can
    never overwrite the reviewed all-city audit/messages outputs.
    """
    base_dir = event_output_dir(month_id, family_slug, event_name)
    if not cities:
        return base_dir

    path = base_dir / "subset_runs" / selected_run_label(cities)
    path.mkdir(parents=True, exist_ok=True)
    return path

def normalize_lines(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in str(text).splitlines()
        if line and line.strip()
    ]

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
    print("\n" + "!" * 90)
    print("HUMAN VERIFICATION REQUIRED")
    if reason:
        print("Detected while:", reason)
    print("Complete the CAPTCHA in the browser, then return here and press Enter.")
    print("!" * 90)
    while True:
        input("Press Enter after completing the CAPTCHA... ")
        page.wait_for_timeout(3000)
        if not captcha_is_present(page):
            page.wait_for_timeout(CAPTCHA_SAFE_WAIT_MS)
            print("Verification cleared. Resuming.\n")
            return
        print("Verification still appears to be present.")

def validate_direct_geoname_page(
    page: Page, expected_geoname_id: str, city: str, date_str: str
) -> None:
    expected = clean(expected_geoname_id)
    if not expected:
        return
    query = parse_qs(urlparse(page.url).query)
    actual = clean((query.get("geoname-id") or [""])[0])
    if actual != expected:
        raise RuntimeError(
            f"Wrong Drik location loaded for {city} on {date_str}. "
            f"Expected geoname-id={expected}; URL={page.url}"
        )

def open_direct_month_page(
    page: Page, geoname_id: str, city: str, date_str: str
) -> None:
    if not geoname_id:
        raise ValueError(
            f"{city}: geoname_id is required by this independent detail-page engine."
        )
    url = build_month_url(geoname_id, date_str)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            print(f"Opening {city} - {date_str}")
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
            wait_for_manual_captcha(page, f"opening {city} on {date_str}")
            validate_direct_geoname_page(page, geoname_id, city, date_str)
            return
        except Exception as exc:
            last_error = exc
            retryable = any(x in str(exc) for x in [
                "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_TIMED_OUT",
                "Timeout", "Navigation timeout",
            ])
            if not retryable or attempt >= 3:
                raise
            page.wait_for_timeout(5000)
    if last_error:
        raise last_error

def normalize_event_name_for_matching(event_name: str) -> str:
    name = clean(event_name)
    name = re.sub(r"\s+Parana$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+Vrat$", "", name, flags=re.IGNORECASE)
    return name.strip()

def find_event_detail_url(page: Page, event_name: str) -> str:
    target = normalize_event_name_for_matching(event_name).lower()
    target_words = [w for w in re.split(r"\s+", target) if len(w) > 2]

    candidates: list[dict[str, Any]] = []
    links = page.locator("a")
    for i in range(links.count()):
        link = links.nth(i)
        try:
            text = link.inner_text().strip()
            href = link.get_attribute("href")
            visible = link.is_visible()
        except Exception:
            continue
        if not text or not href or not visible:
            continue

        normalized = normalize_event_name_for_matching(text).lower()
        if normalized in {
            "ekadashi dates", "iskcon ekadashi", "festivals",
            "hindu calendar", "panchang",
        }:
            continue

        score = 0
        if normalized == target:
            score += 30
        if target in normalized or normalized in target:
            score += 15
        for word in target_words:
            if word in normalized:
                score += 3
        if score:
            candidates.append({"href": href, "score": score, "text": text})

    if not candidates:
        return ""
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return urljoin(MONTH_PANCHANG_URL, candidates[0]["href"])

def open_detail_page(page: Page, url: str) -> tuple[str, str]:
    if not url:
        return "", ""
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(DETAIL_PAGE_WAIT_MS)
    wait_for_manual_captcha(page, "opening event detail page")
    return page.url, page.locator("body").inner_text(timeout=30000)

def make_base_message_row(
    discovery_row: pd.Series,
    *,
    event_family: str,
    event_name: str,
    event_type: str,
    condition_code: str,
    special_details: str,
    note: str,
    message: str,
    completeness: str,
    source_module: str,
    rule_version: str,
) -> dict[str, str]:
    return {
        "Place Key": clean(discovery_row.get("Place Key", "")),
        "Location Key": clean(discovery_row.get("Location Key", "")),
        "City": clean(discovery_row.get("City", "")),
        "State/Region": clean(discovery_row.get("State/Region", "")),
        "Country": clean(discovery_row.get("Country", "")),
        "Timezone": clean(discovery_row.get("Timezone", "")),
        "Geoname ID": clean(discovery_row.get("Geoname ID", "")),
        "Event Family": event_family,
        "Event Name": event_name,
        "Event Type": event_type,
        "Condition Code": condition_code,
        "Cycle Anchor": clean(discovery_row.get("Displayed Date", "")),
        "Completeness Status": completeness,
        "Date": clean(discovery_row.get("Displayed Date", "")),
        "Action Role": "OBSERVANCE",
        "Special Events": event_name,
        "Special Event Details": special_details,
        "Note": note,
        "Message": message,
        "Source Module": source_module,
        "Rule Version": rule_version,
    }

MESSAGE_COLUMNS = [
    "Place Key", "Location Key", "City", "State/Region", "Country", "Timezone",
    "Geoname ID", "Event Family", "Event Name", "Event Type", "Condition Code",
    "Cycle Anchor", "Completeness Status", "Date", "Action Role",
    "Special Events", "Special Event Details", "Note", "Message",
    "Source Module", "Rule Version",
]

def write_df(path: Path, df: pd.DataFrame, columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is not None:
        for col in columns:
            if col not in df.columns:
                df[col] = ""
        df = df[columns]
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(path)
