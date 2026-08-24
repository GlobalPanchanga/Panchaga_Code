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

ENGINE_KEY = "SANKRAMANA"
EVENT_FAMILY = "SANKRAMANA"
RULE_VERSION = "SANKRAMANA_V2_1_CITY_CONTEXT_THEN_DISCOVERY_URL"
SOURCE_MODULE = "sankramana_engine.py"

CACHE_COLUMNS = [
    "Location Key", "Date", "Observed Festival", "Canonical Festival",
    "Detail URL", "Sankranti Moment", "Punya Kala", "Maha Punya Kala",
    "Extracted Details", "Completeness Status", "Scan Error",
]

class DetailCache:
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

    def get(self, location_key: str, date_str: str, observed: str, canonical: str) -> dict[str, str] | None:
        matched = self.df[
            (self.df["Location Key"] == location_key) &
            (self.df["Date"] == date_str) &
            (self.df["Observed Festival"] == observed) &
            (self.df["Canonical Festival"] == canonical)
        ]
        matched = matched[
            matched["Completeness Status"].map(clean).str.upper().eq("COMPLETE")
        ]
        if matched.empty:
            return None
        self.cache_hits += 1
        return matched.iloc[-1].to_dict()

    def put(self, record: dict[str, Any]) -> None:
        if clean(record.get("Completeness Status", "")).upper() != "COMPLETE":
            return

        normalized = {c: "" for c in CACHE_COLUMNS}
        for c in CACHE_COLUMNS:
            if c in record:
                normalized[c] = clean(record[c])

        same = (
            self.df["Location Key"].eq(normalized["Location Key"])
            & self.df["Date"].eq(normalized["Date"])
            & self.df["Observed Festival"].eq(normalized["Observed Festival"])
            & self.df["Canonical Festival"].eq(normalized["Canonical Festival"])
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
        self.df.to_csv(self.path, index=False, encoding="utf-8-sig")

def extract_sankranti_details(detail_text: str) -> tuple[str, dict[str, str]]:
    found: list[str] = []
    patterns = [
        r"((?:\w+\s+)?Sankranti\s+Moment\s*-\s*\d{1,2}:\d{2}\s*[AP]M)",
        r"(Sankranti\s+Moment\s*:\s*\d{1,2}:\d{2}\s*[AP]M(?:,\s*\w+\s+\d{1,2})?)",
        r"((?:\w+\s+)?Sankranti\s+Punya\s+Kala\s*-\s*\d{1,2}:\d{2}\s*[AP]M\s+to\s+\d{1,2}:\d{2}\s*[AP]M)",
        r"((?:\w+\s+)?Sankranti\s+Maha\s+Punya\s+Kala\s*-\s*\d{1,2}:\d{2}\s*[AP]M\s+to\s+\d{1,2}:\d{2}\s*[AP]M)",
    ]

    for line in normalize_lines(detail_text):
        for pattern in patterns:
            m = re.search(pattern, line, flags=re.IGNORECASE)
            if m:
                value = re.sub(r"\s+", " ", m.group(1)).strip()
                if value not in found:
                    found.append(value)

    values = {"Sankranti Moment": "", "Punya Kala": "", "Maha Punya Kala": ""}
    final: list[str] = []
    for value in found:
        low = value.lower()
        if "sankranti moment" in low and not values["Sankranti Moment"]:
            values["Sankranti Moment"] = value
        elif "maha punya kala" in low and not values["Maha Punya Kala"]:
            values["Maha Punya Kala"] = value
        elif "punya kala" in low and not values["Punya Kala"]:
            values["Punya Kala"] = value

    for key in ["Sankranti Moment", "Punya Kala", "Maha Punya Kala"]:
        if values[key]:
            final.append(values[key])

    return " | ".join(final), values

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Process a discovered Sankramana/Sankranti event for each city."
    )
    p.add_argument("--month", required=True, help="YYYY-MM")
    p.add_argument("--canonical", default="Sankramana")
    p.add_argument("--discovery-file")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--all-cities", action="store_true")
    group.add_argument("--cities", nargs="+")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--profile-dir", default="playwright_profile")
    p.add_argument(
        "--cache",
        default="",
        help="Optional exact cache CSV path. Default: festival_runs/cache/sankramana_scan_cache.csv",
    )
    p.add_argument(
        "--refresh-event",
        action="store_true",
        help=(
            "Ignore cached rows for this requested Sankramana event and rescan "
            "all matching city/date detail pages. Other cached Sankramana events "
            "are preserved; fresh COMPLETE rows replace matching cache rows."
        ),
    )
    return p.parse_args()

def main() -> None:
    args = parse_args()
    rows = load_discovery(
        args.month, ENGINE_KEY, args.canonical, args.discovery_file,
        args.all_cities, args.cities,
    )
    representative = rows["Observed Festival"].map(clean).value_counts().index[0]
    out_dir = selected_event_output_dir(args.month, "sankramana", representative, args.cities)
    audit_rows: list[dict[str, Any]] = []
    messages: list[dict[str, str]] = []
    cache = DetailCache(
        Path(args.cache)
        if args.cache
        else Path("festival_runs/cache/sankramana_scan_cache.csv")
    )
    print(
        "Cache mode: "
        + (
            "REFRESH THIS EVENT"
            if args.refresh_event
            else "NORMAL - use reusable COMPLETE cached rows"
        )
    )

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(Path(args.profile_dir)),
            headless=args.headless,
        )
        page = context.pages[0] if context.pages else context.new_page()
        detail_page = context.new_page()

        for _, row in rows.iterrows():
            city = clean(row["City"])
            date_str = clean(row["Displayed Date"])
            observed = clean(row["Observed Festival"])
            geoname_id = get_geoname_id(row)
            location_key = clean(row.get("Location Key", ""))

            discovered_detail_url = clean(row.get("Event Detail URL", ""))
            detail_url = ""
            detail_url_source = ""
            details = ""
            values = {"Sankranti Moment": "", "Punya Kala": "", "Maha Punya Kala": ""}
            error = ""
            status = "INCOMPLETE"

            cached = (
                None
                if args.refresh_event
                else cache.get(location_key, date_str, observed, args.canonical)
            )
            try:
                if cached is not None:
                    detail_url = clean(cached.get("Detail URL", ""))
                    detail_url_source = "CACHE"
                    values = {
                        "Sankranti Moment": clean(cached.get("Sankranti Moment", "")),
                        "Punya Kala": clean(cached.get("Punya Kala", "")),
                        "Maha Punya Kala": clean(cached.get("Maha Punya Kala", "")),
                    }
                    details = clean(cached.get("Extracted Details", ""))
                    status = clean(cached.get("Completeness Status", "")) or "INCOMPLETE"
                    error = clean(cached.get("Scan Error", ""))
                    print(f"CACHE {city}: {date_str} -> {status}")
                else:
                    # IMPORTANT:
                    # Drik's saved event href is usually not city-specific.
                    # Re-establish THIS city's Drik context first, exactly as
                    # the proven old scanner did, then open the stored href in
                    # the same browser context.
                    open_direct_month_page(
                        page,
                        geoname_id,
                        city,
                        date_str,
                    )

                    if discovered_detail_url:
                        detail_url = discovered_detail_url
                        detail_url_source = "DISCOVERY_AFTER_CITY_CONTEXT"
                    else:
                        # Backward compatibility only for an older discovery CSV.
                        # The current discovery should already contain this URL.
                        detail_url = find_event_detail_url(page, observed)
                        detail_url_source = "REDISCOVERED_AFTER_CITY_CONTEXT"

                    if not detail_url:
                        raise RuntimeError(
                            f"No Event Detail URL available for {observed}. "
                            "Rerun monthly festival discovery with the current version."
                        )

                    _, detail_text = open_detail_page(
                        detail_page,
                        detail_url,
                    )
                    details, values = extract_sankranti_details(detail_text)
                    if not values["Sankranti Moment"]:
                        raise RuntimeError("Sankranti Moment was not found on detail page")
                    status = "COMPLETE"
                    cache.put({
                        "Location Key": location_key,
                        "Date": date_str,
                        "Observed Festival": observed,
                        "Canonical Festival": args.canonical,
                        "Detail URL": detail_url,
                        "Sankranti Moment": values["Sankranti Moment"],
                        "Punya Kala": values["Punya Kala"],
                        "Maha Punya Kala": values["Maha Punya Kala"],
                        "Extracted Details": details,
                        "Completeness Status": status,
                        "Scan Error": "",
                    })
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                print(f"WARNING: {city}: {error}")

            public_name = observed or args.canonical
            message = public_name
            if details:
                message += f". {details}"

            messages.append(make_base_message_row(
                row,
                event_family=EVENT_FAMILY,
                event_name=public_name,
                event_type="SANKRAMANA",
                condition_code="DRIK_SANKRAMANA_DETAIL",
                special_details=details,
                note="",
                message=message,
                completeness=status,
                source_module=SOURCE_MODULE,
                rule_version=RULE_VERSION,
            ))
            audit_rows.append({
                **{k: clean(row.get(k, "")) for k in row.index},
                "Discovery Event Detail URL": discovered_detail_url,
                "Detail URL": detail_url,
                "Detail URL Source": detail_url_source,
                "City Context Established": (
                    "CACHE"
                    if cached is not None
                    else "YES"
                ),
                **values,
                "Extracted Details": details,
                "Completeness Status": status,
                "Scan Error": error,
            })

        context.close()

    audit_df = pd.DataFrame(audit_rows)
    messages_df = pd.DataFrame(messages)
    audit_path = out_dir / "sankramana_audit.csv"
    messages_path = out_dir / "sankramana_messages.csv"
    write_df(audit_path, audit_df)
    write_df(messages_path, messages_df, MESSAGE_COLUMNS)
    cache.save()

    print(f"\nSankramana rows: {len(messages_df)}")
    print(f"Complete: {(messages_df['Completeness Status'] == 'COMPLETE').sum()}")
    print(f"Incomplete: {(messages_df['Completeness Status'] != 'COMPLETE').sum()}")
    print(f"Audit: {audit_path}")
    print(f"Messages: {messages_path}")
    print(f"Cache: {cache.path}")
    print(f"Cache hits this run : {cache.cache_hits}")
    print(f"Fresh scans this run: {cache.fresh_scans}")

if __name__ == "__main__":
    main()
