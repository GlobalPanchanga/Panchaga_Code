from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
import re

import pandas as pd
from playwright.sync_api import sync_playwright

from festival_engine_common import (
    MESSAGE_COLUMNS, clean, selected_event_output_dir, find_event_detail_url,
    get_geoname_id, load_discovery, make_base_message_row, normalize_lines,
    open_detail_page, open_direct_month_page, write_df,
)

ENGINE_KEY = "GRAHANA"
EVENT_FAMILY = "GRAHANA"
RULE_VERSION = "GRAHANA_V2_2_CITY_CONTEXT_THEN_DISCOVERY_URL"
SOURCE_MODULE = "grahana_engine.py"

CACHE_COLUMNS = [
    "Location Key", "Date", "Observed Festival", "Canonical Festival",
    "Detail URL", "Local Visibility", "Visibility Reason",
    "Sutak Begins", "Sutak Ends", "Eclipse Start",
    "Maximum Eclipse", "Eclipse End", "Extracted Details",
    "Completeness Status", "Scan Error",
]


class DetailCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            try:
                df = pd.read_csv(
                    path, dtype=str, encoding="utf-8-sig"
                ).fillna("")
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

    def get(
        self,
        location_key: str,
        date_str: str,
        observed: str,
        canonical: str,
    ) -> dict[str, str] | None:
        matched = self.df[
            (self.df["Location Key"] == location_key)
            & (self.df["Date"] == date_str)
            & (self.df["Observed Festival"] == observed)
            & (self.df["Canonical Festival"] == canonical)
        ]

        # Important:
        # Older versions cached failed/incomplete rows. Do NOT reuse those.
        # This lets the corrected parser recover without requiring the user
        # to delete the whole cache.
        matched = matched[
            matched["Completeness Status"]
            .map(clean)
            .str.upper()
            .eq("COMPLETE")
        ]

        if matched.empty:
            return None

        self.cache_hits += 1
        return matched.iloc[-1].to_dict()

    def put(self, record: dict[str, Any]) -> None:
        # Only definitive determinations are reusable:
        #   COMPLETE + visible
        #   COMPLETE + explicitly not visible
        if clean(record.get("Completeness Status", "")).upper() != "COMPLETE":
            return

        normalized = {col: "" for col in CACHE_COLUMNS}
        for col in CACHE_COLUMNS:
            if col in record:
                normalized[col] = clean(record[col])

        same = (
            self.df["Location Key"].eq(normalized["Location Key"])
            & self.df["Date"].eq(normalized["Date"])
            & self.df["Observed Festival"].eq(
                normalized["Observed Festival"]
            )
            & self.df["Canonical Festival"].eq(
                normalized["Canonical Festival"]
            )
        )
        self.df = self.df.loc[~same].copy()
        self.df = pd.concat(
            [self.df, pd.DataFrame([normalized])],
            ignore_index=True,
        )

        self.fresh_scans += 1
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.df.to_csv(
            self.path, index=False, encoding="utf-8-sig"
        )


def detect_local_visibility(
    detail_text: str,
    city: str,
    values: dict[str, str] | None = None,
) -> tuple[str, str]:
    """
    Determine LOCAL eclipse visibility.

    Priority:
      1. Parsed local Eclipse Start + End => YES.
      2. A city-specific "not visible in/from <city>" statement => NO.
      3. Sutak explicitly Not Applicable AND no local eclipse timings => NO.
      4. Otherwise => UNKNOWN.

    Generic article text such as
      "The eclipse would not be visible from India, Nepal..."
    is intentionally ignored unless it specifically names the current city.
    """
    lines = normalize_lines(detail_text)
    city_clean = clean(city)
    city_low = city_clean.lower()

    values = values or {}
    eclipse_start = clean(values.get("Eclipse Start", ""))
    eclipse_end = clean(values.get("Eclipse End", ""))

    # Parsed local timings are the strongest evidence of visibility.
    if eclipse_start and eclipse_end:
        return "YES", ""

    # Strongest Drik local wording on non-visible detail pages:
    #   "No Lunar Eclipse in Perth"
    #   "No Solar Eclipse in ..."
    if city_low:
        for line in lines:
            line_clean = clean(line)
            line_low = line_clean.lower()
            if city_low in line_low and (
                "no lunar eclipse" in line_low
                or "no solar eclipse" in line_low
                or "no eclipse" in line_low
            ):
                return "NO", line_clean

    # Strong local non-visibility: the current city must be named in the
    # same sentence/line. This prevents generic explanatory prose elsewhere
    # on the page from being mistaken for a local determination.
    if city_low:
        for line in lines:
            line_clean = clean(line)
            line_low = line_clean.lower()

            if city_low not in line_low:
                continue

            local_not_visible_phrases = [
                "eclipse would not be visible",
                "eclipse will not be visible",
                "eclipse is not visible",
                "eclipse would not be visible from",
                "eclipse would not be visible in",
                "grahan would not be visible",
                "grahan will not be visible",
                "not visible from",
                "not visible in",
            ]

            if any(phrase in line_low for phrase in local_not_visible_phrases):
                return "NO", line_clean

    # Drik commonly marks Sutak as Not Applicable on genuinely non-visible
    # local pages. Only use this when no local Start/End were extracted.
    sutak_not_applicable_lines = []
    for line in lines:
        low = line.lower()
        if (
            ("sutak begins" in low or "sutak ends" in low)
            and "not applicable" in low
        ):
            sutak_not_applicable_lines.append(clean(line))

    if sutak_not_applicable_lines and not (eclipse_start or eclipse_end):
        return (
            "NO",
            "Sutak is marked Not Applicable and no local eclipse timings are shown.",
        )

    return "UNKNOWN", ""



def extract_grahan_details(
    detail_text: str,
) -> tuple[str, dict[str, str]]:
    """
    Grahana field extraction follows the proven old-scanner field set:

        Sutak Begins
        Sutak Ends
        Eclipse Start
        Maximum Eclipse   (audit/cache only)
        Eclipse End

    Public output intentionally uses only:
        Sutak Begins -> Sutak Ends -> Eclipse Start -> Eclipse End

    The patterns additionally cover Drik's observed local-boundary wording:
        Lunar Eclipse Starts (With Moonrise)
        Lunar Eclipse Ends (With Moonset)
        Solar Eclipse Starts (With Sunrise)
        Solar Eclipse Ends (With Sunset)
        Eclipse would start with Sunrise/Moonrise
        Eclipse would end with Sunset/Moonset
    """
    lines = normalize_lines(detail_text)

    values = {
        "Sutak Begins": "",
        "Sutak Ends": "",
        "Eclipse Start": "",
        "Maximum Eclipse": "",
        "Eclipse End": "",
    }

    patterns: dict[str, list[str]] = {
        "Sutak Begins": [
            r"^Sutak Begins\s*[-:]\s*(.+)$",
            r"^Sutak Begins at\s*[-:]?\s*(.+)$",
        ],
        "Sutak Ends": [
            r"^Sutak Ends\s*[-:]\s*(.+)$",
            r"^Sutak Ends at\s*[-:]?\s*(.+)$",
        ],
        "Eclipse Start": [
            r"^(?:Eclipse|Solar Eclipse|Lunar Eclipse) Start Time\s*[-:]\s*(.+)$",
            r"^(?:Partial|Total|Annular)?\s*(?:Solar|Lunar)?\s*Eclipse Starts?"
            r"\s*(?:\((?:With\s+)?(?:Moonrise|Sunrise)\))?\s*[-:]\s*(.+)$",
            r"^(?:Partial|Total|Annular)?\s*(?:Solar|Lunar)?\s*Eclipse Begins"
            r"\s*(?:\((?:With\s+)?(?:Moonrise|Sunrise)\))?\s*[-:]\s*(.+)$",
            r"^(?:Solar|Lunar)?\s*Eclipse would start with "
            r"(?:Moonrise|Sunrise)\s*[-:]\s*(.+)$",
            r"^Eclipse would start with (?:Moonrise|Sunrise)\s*[-:]\s*(.+)$",
            r"^Grahan Starts?\s*[-:]\s*(.+)$",
        ],
        "Maximum Eclipse": [
            r"^Maximum Eclipse Time\s*[-:]\s*(.+)$",
            r"^Maximum Eclipse\s*[-:]\s*(.+)$",
            r"^Maximum of (?:Solar|Lunar) Eclipse\s*[-:]\s*(.+)$",
            r"^Maximum Grahan\s*[-:]\s*(.+)$",
        ],
        "Eclipse End": [
            r"^(?:Eclipse|Solar Eclipse|Lunar Eclipse) End Time\s*[-:]\s*(.+)$",
            r"^(?:Partial|Total|Annular)?\s*(?:Solar|Lunar)?\s*Eclipse Ends?"
            r"\s*(?:\((?:With\s+)?(?:Moonset|Sunset)\))?\s*[-:]\s*(.+)$",
            r"^(?:Solar|Lunar)?\s*Eclipse would end with "
            r"(?:Moonset|Sunset)\s*[-:]\s*(.+)$",
            r"^Eclipse would end with (?:Moonset|Sunset)\s*[-:]\s*(.+)$",
            r"^Grahan Ends?\s*[-:]\s*(.+)$",
        ],
    }

    for label, label_patterns in patterns.items():
        for line in lines:
            low = line.lower()

            # Preserve old-scanner behavior: do not pick the relaxed Sutak
            # timings for children / elderly / sick.
            if (
                "kids" in low
                or "old and sick" in low
                or "children" in low
                or "elderly" in low
            ):
                continue

            for pattern in label_patterns:
                m = re.search(pattern, line, flags=re.IGNORECASE)
                if m:
                    values[label] = clean(m.group(1))
                    break

            if values[label]:
                break

    public_labels = [
        "Sutak Begins",
        "Sutak Ends",
        "Eclipse Start",
        "Eclipse End",
    ]

    details = " | ".join(
        f"{label} - {values[label]}"
        for label in public_labels
        if values[label]
    )

    return details, values


def find_eclipse_detail_url_old_scanner_style(
    page,
    observed: str,
    canonical: str,
) -> str:
    """
    Use the same principle as the old scanner: while still on the exact
    city/date Month Panchang page, capture the real Drik href.

    This is intentionally more eclipse-specific than the generic matcher:
    href path is strong evidence and label spelling (*Anshika, Partial,
    Chandra/Lunar, Surya/Solar) is allowed to vary.
    """
    event_text = f"{clean(observed)} {clean(canonical)}".lower()

    want_lunar = "chandra" in event_text or "lunar" in event_text
    want_solar = "surya" in event_text or "solar" in event_text

    candidates = []
    links = page.locator("a")

    for i in range(links.count()):
        link = links.nth(i)
        try:
            text = clean(link.inner_text())
            href = clean(link.get_attribute("href"))
            visible = link.is_visible()
        except Exception:
            continue

        if not href or not visible:
            continue

        low_text = text.lower()
        low_href = href.lower()

        score = 0

        if want_lunar:
            if "lunar-eclipse-date-time-duration" in low_href:
                score += 100
            if "chandra" in low_text or "lunar eclipse" in low_text:
                score += 30

        if want_solar:
            if "solar-eclipse-date-time-duration" in low_href:
                score += 100
            if "surya" in low_text or "solar eclipse" in low_text:
                score += 30

        # Prefer the actual local timing page over generic guide/date pages.
        if "date-time-duration" in low_href:
            score += 25
        if "eclipse-dates" in low_href or "calendar" in low_href:
            score -= 50
        if "guide" in low_text or "dates" in low_text:
            score -= 20

        if score > 0:
            candidates.append((score, href))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: x[0], reverse=True)
    from urllib.parse import urljoin
    return urljoin("https://www.drikpanchang.com/", candidates[0][1])



def build_direct_eclipse_detail_url(
    observed: str,
    canonical: str,
    date_str: str,
    geoname_id: str,
) -> str:
    """
    Fallback used when Drik's Month Panchang lists the Grahana as text but does
    not expose the event as a clickable <a> element.

    The Month page has already established the correct city/location in the
    persistent Drik session. Drik's own event links use the eclipse detail path
    plus the local displayed date.
    """
    event_text = f"{clean(observed)} {clean(canonical)}".lower()

    if "chandra" in event_text or "lunar" in event_text:
        path = "lunar-eclipse-date-time-duration.html"
    elif "surya" in event_text or "solar" in event_text:
        path = "solar-eclipse-date-time-duration.html"
    else:
        raise RuntimeError(
            f"Cannot determine eclipse detail-page type from {observed!r} / {canonical!r}"
        )

    dt = pd.to_datetime(date_str, format="%Y-%m-%d", errors="raise")
    drik_date = dt.strftime("%d/%m/%Y")

    params = []
    if clean(geoname_id):
        params.append(f"geoname-id={clean(geoname_id)}")
    params.append(f"date={drik_date}")

    return (
        "https://www.drikpanchang.com/eclipse/"
        f"{path}?" + "&".join(params)
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Process a discovered Surya/Chandra Grahana for each listed city."
        )
    )
    parser.add_argument(
        "--month",
        required=True,
        help="YYYY-MM",
    )
    parser.add_argument(
        "--canonical",
        required=True,
        help='e.g. "Surya Grahana" or "Chandra Grahana"',
    )
    parser.add_argument("--discovery-file")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all-cities", action="store_true")
    group.add_argument("--cities", nargs="+")

    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--profile-dir",
        default="playwright_profile",
    )
    parser.add_argument(
        "--cache",
        default="",
        help=(
            "Optional exact cache CSV path. "
            "Default: festival_runs/cache/grahana_scan_cache.csv"
        ),
    )
    parser.add_argument(
        "--refresh-event",
        action="store_true",
        help=(
            "Ignore cached rows for THIS requested Grahana event and rescan "
            "all matching city/date rows. Other Grahana events already stored "
            "in the shared cache are preserved. Fresh COMPLETE results replace "
            "the corresponding cached rows."
        ),
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    rows = load_discovery(
        args.month,
        ENGINE_KEY,
        args.canonical,
        args.discovery_file,
        args.all_cities,
        args.cities,
    )

    representative = (
        rows["Observed Festival"]
        .map(clean)
        .value_counts()
        .index[0]
    )

    out_dir = selected_event_output_dir(
        args.month,
        "grahana",
        representative,
        args.cities,
    )

    audit_rows: list[dict[str, Any]] = []
    messages: list[dict[str, str]] = []

    cache = DetailCache(
        Path(args.cache)
        if args.cache
        else Path(
            "festival_runs/cache/grahana_scan_cache.csv"
        )
    )

    if args.refresh_event:
        print(
            "CACHE MODE: REFRESH THIS EVENT - existing matching cached rows "
            "will be ignored and replaced by fresh COMPLETE determinations."
        )
    else:
        print("CACHE MODE: NORMAL - reusable COMPLETE cached rows will be used.")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(args.profile_dir)),
            headless=args.headless,
        )

        page = (
            context.pages[0]
            if context.pages
            else context.new_page()
        )
        detail_page = context.new_page()

        for _, row in rows.iterrows():
            city = clean(row["City"])
            date_str = clean(row["Displayed Date"])
            observed = clean(row["Observed Festival"])
            geoname_id = get_geoname_id(row)
            location_key = clean(
                row.get("Location Key", "")
            )

            discovered_detail_url = clean(row.get("Event Detail URL", ""))
            detail_url = ""
            detail_url_source = ""
            details = ""
            local_visibility = "UNKNOWN"
            visibility_reason = ""

            values = {
                "Sutak Begins": "",
                "Sutak Ends": "",
                "Eclipse Start": "",
                "Maximum Eclipse": "",
                "Eclipse End": "",
            }

            error = ""
            status = "INCOMPLETE"

            if args.refresh_event:
                cached = None
            else:
                cached = cache.get(
                    location_key,
                    date_str,
                    observed,
                    args.canonical,
                )

            try:
                if cached is not None:
                    detail_url = clean(
                        cached.get("Detail URL", "")
                    )
                    detail_url_source = "CACHE"
                    local_visibility = (
                        clean(
                            cached.get(
                                "Local Visibility",
                                "",
                            )
                        )
                        or "UNKNOWN"
                    )
                    visibility_reason = clean(
                        cached.get(
                            "Visibility Reason",
                            "",
                        )
                    )

                    values = {
                        "Sutak Begins": clean(
                            cached.get("Sutak Begins", "")
                        ),
                        "Sutak Ends": clean(
                            cached.get("Sutak Ends", "")
                        ),
                        "Eclipse Start": clean(
                            cached.get("Eclipse Start", "")
                        ),
                        "Maximum Eclipse": clean(
                            cached.get(
                                "Maximum Eclipse",
                                "",
                            )
                        ),
                        "Eclipse End": clean(
                            cached.get("Eclipse End", "")
                        ),
                    }

                    # Backward-compatible cache migration:
                    # v1.1 cached successful rows before Local Visibility
                    # existed. If such a COMPLETE cached row already contains
                    # both local start and end timings, visibility is
                    # unambiguously YES for our publication logic.
                    if (
                        local_visibility == "UNKNOWN"
                        and values["Eclipse Start"]
                        and values["Eclipse End"]
                    ):
                        local_visibility = "YES"
                        visibility_reason = (
                            "Migrated from older COMPLETE cache row containing "
                            "local Eclipse Start and Eclipse End."
                        )

                    details = clean(
                        cached.get(
                            "Extracted Details",
                            "",
                        )
                    )
                    status = "COMPLETE"

                    print(
                        f"CACHE {city}: {date_str} -> COMPLETE "
                        f"(visibility={local_visibility})"
                    )

                else:
                    # IMPORTANT:
                    # The captured Drik eclipse href is generally date/event
                    # specific, not location-specific. The old scanner first
                    # loaded the exact city's Month Panchang, thereby setting
                    # Drik's current location, and only then opened the event
                    # href in the same browser context.
                    open_direct_month_page(
                        page,
                        geoname_id,
                        city,
                        date_str,
                    )

                    # Primary path: use the href already captured by discovery.
                    if discovered_detail_url:
                        detail_url = discovered_detail_url
                        detail_url_source = "DISCOVERY_AFTER_CITY_CONTEXT"
                    else:
                        # Compatibility only for an older discovery CSV.
                        detail_url = find_eclipse_detail_url_old_scanner_style(
                            page,
                            observed,
                            args.canonical,
                        )
                        if detail_url:
                            detail_url_source = (
                                "REDISCOVERED_OLD_SCANNER_STYLE_AFTER_CITY_CONTEXT"
                            )

                        if not detail_url:
                            detail_url = find_event_detail_url(
                                page,
                                observed,
                            )
                            if detail_url:
                                detail_url_source = (
                                    "REDISCOVERED_GENERIC_AFTER_CITY_CONTEXT"
                                )

                        if not detail_url:
                            # Last resort only. The city context has still been
                            # established first.
                            detail_url = build_direct_eclipse_detail_url(
                                observed=observed,
                                canonical=args.canonical,
                                date_str=date_str,
                                geoname_id=geoname_id,
                            )
                            detail_url_source = (
                                "DIRECT_FALLBACK_AFTER_CITY_CONTEXT"
                            )
                            print(
                                f"DETAIL LINK FALLBACK {city}: "
                                f"discovery row had no captured URL."
                            )

                    _, detail_text = open_detail_page(
                        detail_page,
                        detail_url,
                    )

                    details, values = (
                        extract_grahan_details(
                            detail_text
                        )
                    )

                    (
                        local_visibility,
                        visibility_reason,
                    ) = detect_local_visibility(
                        detail_text,
                        city,
                        values,
                    )

                    if local_visibility == "NO":
                        # Valid determination. No local eclipse event should be
                        # published for this city.
                        status = "COMPLETE"
                        print(
                            f"NOT VISIBLE {city}: {date_str}"
                            + (
                                f" | {visibility_reason}"
                                if visibility_reason
                                else ""
                            )
                        )

                    else:
                        # If Drik did not explicitly rule out local visibility,
                        # timings must be present before we consider the result
                        # complete.
                        if (
                            not values["Eclipse Start"]
                            or not values["Eclipse End"]
                        ):
                            raise RuntimeError(
                                "Local visibility was not ruled out, "
                                "but Eclipse Start/End were not both found"
                            )

                        local_visibility = "YES"
                        status = "COMPLETE"

                    cache.put(
                        {
                            "Location Key": location_key,
                            "Date": date_str,
                            "Observed Festival": observed,
                            "Canonical Festival": args.canonical,
                            "Detail URL": detail_url,
                            "Local Visibility": local_visibility,
                            "Visibility Reason": visibility_reason,
                            "Sutak Begins": values["Sutak Begins"],
                            "Sutak Ends": values["Sutak Ends"],
                            "Eclipse Start": values["Eclipse Start"],
                            "Maximum Eclipse": values["Maximum Eclipse"],
                            "Eclipse End": values["Eclipse End"],
                            "Extracted Details": details,
                            "Completeness Status": status,
                            "Scan Error": "",
                        }
                    )

            except Exception as exc:
                error = (
                    f"{type(exc).__name__}: {exc}"
                )
                print(
                    f"WARNING: {city}: {error}"
                )

            event_type = (
                "SURYA_GRAHANA"
                if "surya"
                in (
                    observed
                    + " "
                    + args.canonical
                ).lower()
                else "CHANDRA_GRAHANA"
                if "chandra"
                in (
                    observed
                    + " "
                    + args.canonical
                ).lower()
                else "GRAHANA"
            )

            public_name = (
                observed
                or args.canonical
            )

            message = public_name
            if details:
                message += f". {details}"

            # Public output for every definitive local determination.
            #
            # Visible:
            #   Show local Sutak / eclipse timings.
            #
            # Not visible:
            #   Still show that the global Grahana occurs, but explicitly say
            #   it is not applicable locally. This prevents a city page from
            #   looking as though the Grahana does not exist at all.
            if status == "COMPLETE" and local_visibility == "YES":
                public_row = make_base_message_row(
                    row,
                    event_family=EVENT_FAMILY,
                    event_name=public_name,
                    event_type=event_type,
                    condition_code="DRIK_LOCAL_GRAHANA_VISIBLE",
                    special_details=details,
                    note="",
                    message=message,
                    completeness=status,
                    source_module=SOURCE_MODULE,
                    rule_version=RULE_VERSION,
                )
                public_row["Action Role"] = "OBSERVANCE"
                messages.append(public_row)

            elif status == "COMPLETE" and local_visibility == "NO":
                not_applicable_details = (
                    "Not Applicable - Eclipse is not locally visible; "
                    "Sutak is not applicable."
                )
                not_applicable_message = (
                    f"{public_name}. {not_applicable_details}"
                )

                public_row = make_base_message_row(
                    row,
                    event_family=EVENT_FAMILY,
                    event_name=public_name,
                    event_type=event_type,
                    condition_code="DRIK_LOCAL_GRAHANA_NOT_VISIBLE",
                    special_details=not_applicable_details,
                    note="",
                    message=not_applicable_message,
                    completeness=status,
                    source_module=SOURCE_MODULE,
                    rule_version=RULE_VERSION,
                )
                public_row["Action Role"] = "NOT_APPLICABLE"
                messages.append(public_row)

            # Audit contains every discovered location.
            audit_rows.append(
                {
                    **{
                        key: clean(row.get(key, ""))
                        for key in row.index
                    },
                    "Discovery Event Detail URL": discovered_detail_url,
                    "Detail URL": detail_url,
                    "Detail URL Source": detail_url_source,
                    "City Context Established": (
                        "CACHE"
                        if cached is not None
                        else "YES"
                    ),
                    "Local Visibility": local_visibility,
                    "Visibility Reason": visibility_reason,
                    **values,
                    "Extracted Details": details,
                    "Completeness Status": status,
                    "Scan Error": error,
                }
            )

        context.close()

    audit_df = pd.DataFrame(audit_rows)
    messages_df = pd.DataFrame(messages)

    audit_path = (
        out_dir
        / "grahana_audit.csv"
    )
    messages_path = (
        out_dir
        / "grahana_messages.csv"
    )

    write_df(
        audit_path,
        audit_df,
    )
    write_df(
        messages_path,
        messages_df,
        MESSAGE_COLUMNS,
    )

    cache.save()

    audit_complete = (
        (
            audit_df[
                "Completeness Status"
            ]
            == "COMPLETE"
        ).sum()
        if not audit_df.empty
        else 0
    )
    audit_incomplete = (
        (
            audit_df[
                "Completeness Status"
            ]
            != "COMPLETE"
        ).sum()
        if not audit_df.empty
        else 0
    )

    visible_count = (
        (
            audit_df[
                "Local Visibility"
            ]
            == "YES"
        ).sum()
        if not audit_df.empty
        else 0
    )

    not_visible_count = (
        (
            audit_df[
                "Local Visibility"
            ]
            == "NO"
        ).sum()
        if not audit_df.empty
        else 0
    )

    unknown_count = (
        (
            audit_df[
                "Local Visibility"
            ]
            == "UNKNOWN"
        ).sum()
        if not audit_df.empty
        else 0
    )

    print(
        f"\nGrahana audit rows : "
        f"{len(audit_df)}"
    )
    print(
        f"Complete           : "
        f"{audit_complete}"
    )
    print(
        f"Incomplete         : "
        f"{audit_incomplete}"
    )
    print(
        f"Locally visible    : "
        f"{visible_count}"
    )
    print(
        f"Not locally visible: "
        f"{not_visible_count}"
    )
    print(
        f"Visibility unknown : "
        f"{unknown_count}"
    )
    applicable_message_count = (
        (messages_df["Action Role"] == "OBSERVANCE").sum()
        if not messages_df.empty
        else 0
    )
    not_applicable_message_count = (
        (messages_df["Action Role"] == "NOT_APPLICABLE").sum()
        if not messages_df.empty
        else 0
    )

    print(
        f"Public message rows: "
        f"{len(messages_df)}"
    )
    print(
        f"  Applicable        : "
        f"{applicable_message_count}"
    )
    print(
        f"  Not Applicable    : "
        f"{not_applicable_message_count}"
    )
    print(
        f"Audit: {audit_path}"
    )
    print(
        f"Messages: {messages_path}"
    )
    print(
        f"Cache: {cache.path}"
    )
    print(
        f"Cache hits this run : "
        f"{cache.cache_hits}"
    )
    print(
        f"Fresh COMPLETE cache writes: "
        f"{cache.fresh_scans}"
    )


if __name__ == "__main__":
    main()
