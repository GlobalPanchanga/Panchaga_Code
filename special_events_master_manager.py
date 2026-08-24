from __future__ import annotations

"""
Special Events Master Manager - v3.0 Monthly Auto-Discovery
============================================

Purpose
-------
Validate and publish one or more reviewed festival *_messages.csv files into
the year-specific production special-events master consumed by the daily
Panchanga scanner.

Supported inputs
----------------
Normal monthly production mode:
    --period YYYY-MM
automatically discovers canonical *_messages.csv files below:
    festival_runs/YYYY/MM/

Explicit --input files remain supported for manual/exceptional publication.

1) Common standardized festival message schema:
   Ekadashi, Krishna Jayanthi, Sankramana, Grahana, Nag Panchami,
   Varamahalakshmi, Kalki Jayanthi, and future engines that use the same fields.

2) Legacy Ekadashi messages schema:
   Older Ekadashi files with Ekadashi Name / Ekadashi Type are normalized
   automatically.

Safety
------
* Preview-only unless --approve is explicitly supplied.
* COMPLETE rows are required by default.
* Duplicate production keys are rejected.
* Existing master is backed up before approval.
* Writes are atomic.
* Two publication scopes:
    full     -> replace the complete publication unit
    selected -> replace only incoming locations within that publication unit

Production key
--------------
    Event Family + Cycle ID + Location Key + Date + Action Role

"Cycle ID" is retained as the production-scope identifier for compatibility
with the existing master schema. For non-cycle festivals it functions as a
stable Publication ID.
"""

from datetime import datetime
from pathlib import Path
from typing import Any
import argparse
import os
import re
import shutil

import pandas as pd


MASTER_DIR = Path("special_events_master")
BACKUP_DIR = MASTER_DIR / "backups"
PREVIEW_DIR = MASTER_DIR / "previews"
MASTER_FILE_TEMPLATE = "special_events_master_{year}.csv"
FESTIVAL_RUNS_ROOT = Path("festival_runs")

REQUIRE_COMPLETE = True
DEFAULT_DISPLAY_PRIORITY = 50

ACTION_ROLE_PRIORITY = {
    "VIDDHA_NO_FAST": 10,
    "UPAVASA_DAY_1": 20,
    "UPAVASA_DAY_2": 20,
    "OBSERVANCE": 20,
    "PARANA": 30,
}

FAMILY_PRIORITY = {
    "GRAHANA": 5,
    "KRISHNA_JAYANTHI": 10,
    "EKADASHI": 20,
    "SANKRAMANA": 20,
    "NAG_PANCHAMI": 20,
    "VARAMAHALAKSHMI": 20,
    "KALKI_JAYANTHI": 20,
}

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

MASTER_COLUMNS = [
    "Date",
    "Place Key",
    "Location Key",
    "City",
    "State/Region",
    "Country",
    "Timezone",
    "Geoname ID",

    "Event Family",
    "Event Name",
    "Cycle ID",
    "Event Type",
    "Condition Code",
    "Action Role",

    "Special Events",
    "Special Event Details",
    "Note",
    "Message",

    "Display Priority",
    "Cycle Anchor",
    "Source Module",
    "Rule Version",
    "Approval Status",
    "Approved At",
    "Source File",
]

UNIQUE_KEY_COLUMNS = [
    "Event Family",
    "Cycle ID",
    "Location Key",
    "Date",
    "Action Role",
]

COMMON_REQUIRED_COLUMNS = {
    "City",
    "State/Region",
    "Country",
    "Timezone",
    "Geoname ID",
    "Completeness Status",
    "Date",
    "Action Role",
    "Special Events",
    "Special Event Details",
    "Note",
    "Message",
}

LEGACY_EKADASHI_REQUIRED_COLUMNS = COMMON_REQUIRED_COLUMNS | {
    "Ekadashi Name",
    "Ekadashi Type",
    "Condition Code",
    "Cycle Anchor",
}


def clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return re.sub(r"\s+", " ", str(value)).strip()


def slugify(value: Any) -> str:
    value = clean(value).lower()
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_") or "event"


def normalize_timezone_name(value: str) -> str:
    value = clean(value)
    return TIMEZONE_NAME_ALIASES.get(value, value)


def normalize_iso_date(value: Any, field_name: str) -> str:
    text = clean(value)
    if not text:
        raise ValueError(f"Blank {field_name}")
    try:
        return pd.to_datetime(
            text, format="%Y-%m-%d", errors="raise"
        ).strftime("%Y-%m-%d")
    except Exception as exc:
        raise ValueError(
            f"Invalid {field_name} {text!r}; expected YYYY-MM-DD"
        ) from exc


def normalize_period(value: str) -> str:
    value = clean(value)
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("--period must use YYYY-MM")
    pd.to_datetime(value + "-01", format="%Y-%m-%d", errors="raise")
    return value


def get_place_key(
    geoname_id: str,
    city: str,
    state: str,
    country: str,
    timezone: str,
) -> str:
    geoname_id = clean(geoname_id)
    if re.fullmatch(r"\d+\.0+", geoname_id):
        geoname_id = geoname_id.split(".", 1)[0]

    if geoname_id:
        return f"geoname:{geoname_id}"

    raw = "|".join([
        clean(city).lower(),
        clean(state).lower(),
        clean(country).lower(),
        normalize_timezone_name(timezone),
    ])
    return re.sub(r"[^a-z0-9:+|_/-]+", "_", raw)


def make_location_key(
    place_key: str,
    city: str,
) -> str:
    return f"{clean(place_key)}|city:{slugify(city)}"


def infer_period_from_path(path: Path) -> str:
    parts = list(path.parts)
    lower = [p.lower() for p in parts]
    for i, part in enumerate(lower):
        if part == "festival_runs" and i + 2 < len(parts):
            year = clean(parts[i + 1])
            month = clean(parts[i + 2])
            candidate = f"{year}-{month}"
            if re.fullmatch(r"\d{4}-\d{2}", candidate):
                return candidate
    return ""



AUTO_DISCOVERY_EXCLUDED_DIRS = {
    "subset_runs",
    "preview",
    "previews",
    "backup",
    "backups",
    "cache",
}



def selected_run_label(city_names: list[str]) -> str:
    names = [slugify(x) for x in city_names if clean(x)]
    if not names:
        return "selected"
    if len(names) <= 4:
        return "_".join(names)
    return "_".join(names[:3]) + f"_plus_{len(names) - 3}"


def discover_selected_message_files(
    period: str,
    festival_runs_root: Path,
    cities: list[str],
) -> list[Path]:
    """
    Auto-discover the isolated subset message files produced by a historical
    selected-city festival backfill.
    """
    period = normalize_period(period)
    year, month = period.split("-")
    month_root = Path(festival_runs_root) / year / month
    label = selected_run_label(cities)

    if not month_root.exists():
        raise FileNotFoundError(f"Festival month folder not found: {month_root}")

    found = sorted(
        {
            p
            for p in month_root.rglob("*_messages.csv")
            if p.is_file()
            and "subset_runs" in [part.lower() for part in p.parts]
            and label in [part.lower() for part in p.parts]
        },
        key=lambda p: str(p).lower(),
    )

    if not found:
        raise FileNotFoundError(
            f"No selected-city message files found for label {label!r} "
            f"under {month_root}"
        )

    return found


def discover_period_message_files(
    period: str,
    festival_runs_root: Path,
) -> list[Path]:
    """
    Discover canonical monthly festival message outputs.

    Expected structure:
        festival_runs/YYYY/MM/**/<something>_messages.csv

    Test/subset outputs are deliberately excluded so a partial validation run
    cannot accidentally enter the production Special Events Master.
    """
    period = normalize_period(period)
    year, month = period.split("-")
    month_root = Path(festival_runs_root) / year / month

    if not month_root.exists():
        raise FileNotFoundError(
            f"Festival month folder not found: {month_root}"
        )

    found: list[Path] = []

    for path in month_root.rglob("*_messages.csv"):
        if not path.is_file():
            continue

        relative_parts = [
            clean(part).lower()
            for part in path.relative_to(month_root).parts[:-1]
        ]

        if any(
            part in AUTO_DISCOVERY_EXCLUDED_DIRS
            for part in relative_parts
        ):
            continue

        found.append(path)

    # Stable order makes previews and terminal output reproducible.
    found = sorted(
        set(found),
        key=lambda p: str(p).lower(),
    )

    if not found:
        raise FileNotFoundError(
            "No canonical *_messages.csv files were found under "
            f"{month_root}"
        )

    return found



def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Input messages CSV not found: {path}")
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    df.columns = [clean(c) for c in df.columns]
    if df.empty:
        raise ValueError(f"Input messages CSV contains no rows: {path}")
    return df


def detect_schema(df: pd.DataFrame) -> str:
    cols = set(df.columns)
    if {"Ekadashi Name", "Ekadashi Type"}.issubset(cols):
        missing = LEGACY_EKADASHI_REQUIRED_COLUMNS - cols
        if missing:
            raise ValueError(
                "Legacy Ekadashi input is missing columns: "
                + ", ".join(sorted(missing))
            )
        return "LEGACY_EKADASHI"

    missing = COMMON_REQUIRED_COLUMNS - cols
    if missing:
        raise ValueError(
            "Standardized messages input is missing columns: "
            + ", ".join(sorted(missing))
        )
    if "Event Family" not in cols:
        raise ValueError(
            "Standardized messages input requires Event Family. "
            "If this is an older Ekadashi file it must contain Ekadashi Name/Ekadashi Type."
        )
    return "COMMON"


def normalize_incoming(
    raw: pd.DataFrame,
    source_path: Path,
) -> tuple[pd.DataFrame, str]:
    schema = detect_schema(raw)
    df = raw.copy()

    for col in df.columns:
        df[col] = df[col].map(clean)

    if schema == "LEGACY_EKADASHI":
        df["Event Family"] = "EKADASHI"
        df["Event Name"] = df["Ekadashi Name"]
        df["Event Type"] = df["Ekadashi Type"]
        if "Source Module" not in df.columns:
            df["Source Module"] = "ekadashi_cycle_scanner.py"
        if "Rule Version" not in df.columns:
            df["Rule Version"] = "legacy_ekadashi_messages"
    else:
        for optional in [
            "Event Name", "Event Type", "Condition Code", "Cycle Anchor",
            "Source Module", "Rule Version", "Place Key", "Location Key",
            "Display Priority", "Cycle ID",
        ]:
            if optional not in df.columns:
                df[optional] = ""

    df["Date"] = df["Date"].map(lambda x: normalize_iso_date(x, "Date"))

    # Cycle Anchor is optional for exact-date festivals.
    df["Cycle Anchor"] = df["Cycle Anchor"].map(
        lambda x: normalize_iso_date(x, "Cycle Anchor") if clean(x) else ""
    )

    df["Event Family"] = df["Event Family"].map(lambda x: clean(x).upper())
    df["Action Role"] = df["Action Role"].map(lambda x: clean(x).upper())

    # Make Place Key / Location Key canonical and exactly compatible with the
    # daily scanner, even if an engine omitted them.
    place_keys = []
    location_keys = []
    for _, row in df.iterrows():
        place_key = clean(row.get("Place Key", ""))
        if not place_key:
            place_key = get_place_key(
                clean(row.get("Geoname ID", "")),
                clean(row.get("City", "")),
                clean(row.get("State/Region", "")),
                clean(row.get("Country", "")),
                clean(row.get("Timezone", "")),
            )
        place_keys.append(place_key)
        location_keys.append(
            make_location_key(place_key, clean(row.get("City", "")))
        )
    df["Place Key"] = place_keys
    df["Location Key"] = location_keys

    return df, schema


def validate_incoming(df: pd.DataFrame, source_path: Path) -> None:
    problems: list[str] = []

    if REQUIRE_COMPLETE:
        bad = df[df["Completeness Status"].str.upper() != "COMPLETE"]
        if not bad.empty:
            sample = bad[
                ["City", "Date", "Completeness Status"]
            ].head(10).to_string(index=False)
            problems.append(
                f"{len(bad)} row(s) are not COMPLETE.\n{sample}"
            )

    for col in [
        "Date", "Place Key", "Location Key", "City", "Event Family",
        "Event Name", "Action Role", "Special Events", "Message",
    ]:
        bad = df[df[col].map(clean) == ""]
        if not bad.empty:
            problems.append(f"{len(bad)} row(s) have blank {col}")

    families = sorted(set(df["Event Family"].map(clean)))
    if len(families) != 1:
        problems.append(
            f"One messages file must represent one Event Family; found {families}"
        )

    event_names = sorted(set(df["Event Name"].map(clean)))
    if len(event_names) != 1:
        problems.append(
            "One messages file must represent one publication event. "
            f"Found Event Name values: {event_names}"
        )

    years = sorted(set(df["Date"].str[:4]))
    if len(years) != 1:
        problems.append(
            f"One messages file must publish into one year; found years {years}"
        )

    if problems:
        raise ValueError(
            f"Incoming validation failed for {source_path}:\n- "
            + "\n- ".join(problems)
        )


def derive_period(
    df: pd.DataFrame,
    source_path: Path,
    explicit_period: str,
    publish_mode: str,
) -> str:
    if explicit_period:
        return normalize_period(explicit_period)

    inferred = infer_period_from_path(source_path)
    if inferred:
        return inferred

    months = sorted(set(df["Date"].str[:7]))
    if len(months) == 1:
        return months[0]

    # Legacy Ekadashi has a genuinely stable global cycle anchor.
    family = clean(df["Event Family"].iloc[0]).upper()
    anchors = sorted({clean(x) for x in df["Cycle Anchor"] if clean(x)})
    if family == "EKADASHI" and len(anchors) == 1:
        return anchors[0][:7]

    if publish_mode == "SELECTED_PLACES_UPSERT":
        raise ValueError(
            f"Could not derive a stable publication month for selected-place publication of {source_path}. "
            "Pass --period YYYY-MM so the subset uses the same Publication/Cycle ID as the original full run."
        )

    # Full first publication: choose earliest event month, but make the choice visible.
    return min(months)


def derive_publication_metadata(
    df: pd.DataFrame,
    source_path: Path,
    explicit_period: str,
    explicit_event_id: str,
    publish_mode: str,
) -> tuple[str, str, str, int]:
    family = clean(df["Event Family"].iloc[0]).upper()
    event_name = clean(df["Event Name"].iloc[0])
    year = int(clean(df["Date"].iloc[0])[:4])

    incoming_cycle_ids = sorted({
        clean(x) for x in df.get("Cycle ID", pd.Series(dtype=str)) if clean(x)
    })
    if incoming_cycle_ids:
        if len(incoming_cycle_ids) != 1:
            raise ValueError(
                f"{source_path}: multiple Cycle ID values found: {incoming_cycle_ids}"
            )
        cycle_id = incoming_cycle_ids[0]
        period = derive_period(df, source_path, explicit_period, publish_mode)
        return event_name, cycle_id, period, year

    if explicit_event_id:
        cycle_id = clean(explicit_event_id).upper()
        period = derive_period(df, source_path, explicit_period, publish_mode)
        return event_name, cycle_id, period, year

    # Preserve the historical Ekadashi ID convention when one stable anchor
    # exists, so old and new Ekadashi publications remain compatible.
    anchors = sorted({clean(x) for x in df["Cycle Anchor"] if clean(x)})
    if family == "EKADASHI" and len(anchors) == 1:
        anchor = anchors[0]
        cycle_id = (
            f"EKADASHI_{anchor.replace('-', '_')}_"
            f"{slugify(event_name).upper()}"
        )
        return event_name, cycle_id, anchor[:7], year

    period = derive_period(df, source_path, explicit_period, publish_mode)
    cycle_id = (
        f"{family}_{period.replace('-', '_')}_"
        f"{slugify(event_name).upper()}"
    )
    return event_name, cycle_id, period, year


def choose_display_priority(row: pd.Series) -> int:
    explicit = clean(row.get("Display Priority", ""))
    if explicit:
        try:
            return int(float(explicit))
        except Exception:
            pass

    role = clean(row.get("Action Role", "")).upper()
    if role in ACTION_ROLE_PRIORITY:
        return ACTION_ROLE_PRIORITY[role]

    family = clean(row.get("Event Family", "")).upper()
    return FAMILY_PRIORITY.get(family, DEFAULT_DISPLAY_PRIORITY)


def transform_to_master(
    incoming: pd.DataFrame,
    source_path: Path,
    cycle_id: str,
    source_module_override: str,
    rule_version_override: str,
) -> pd.DataFrame:
    approved_at = datetime.now().astimezone().isoformat(timespec="seconds")
    out = pd.DataFrame()

    for col in [
        "Date", "Place Key", "Location Key", "City", "State/Region",
        "Country", "Timezone", "Geoname ID",
        "Event Family", "Event Name", "Event Type", "Condition Code",
        "Action Role", "Special Events", "Special Event Details", "Note",
        "Message", "Cycle Anchor",
    ]:
        out[col] = incoming[col] if col in incoming.columns else ""

    out["Cycle ID"] = cycle_id
    out["Display Priority"] = incoming.apply(
        choose_display_priority, axis=1
    ).astype(str)

    if source_module_override:
        out["Source Module"] = source_module_override
    else:
        out["Source Module"] = incoming.get(
            "Source Module", pd.Series("", index=incoming.index)
        ).map(clean)

    if rule_version_override:
        out["Rule Version"] = rule_version_override
    else:
        out["Rule Version"] = incoming.get(
            "Rule Version", pd.Series("", index=incoming.index)
        ).map(clean)

    out["Approval Status"] = "APPROVED"
    out["Approved At"] = approved_at
    out["Source File"] = source_path.name

    for col in MASTER_COLUMNS:
        if col not in out.columns:
            out[col] = ""

    return out[MASTER_COLUMNS].copy()


def find_duplicate_keys(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    mask = df.duplicated(UNIQUE_KEY_COLUMNS, keep=False)
    return df.loc[mask].sort_values(
        UNIQUE_KEY_COLUMNS, kind="stable"
    )


def load_existing_master(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=MASTER_COLUMNS)

    df = pd.read_csv(
        path, dtype=str, encoding="utf-8-sig"
    ).fillna("")
    df.columns = [clean(c) for c in df.columns]
    for col in MASTER_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[MASTER_COLUMNS].copy()


def merge_publication(
    master: pd.DataFrame,
    incoming_rows: pd.DataFrame,
    family: str,
    cycle_id: str,
    publish_mode: str,
) -> tuple[pd.DataFrame, int]:
    mode = clean(publish_mode).upper()
    valid = {"FULL_CYCLE_REPLACE", "SELECTED_PLACES_UPSERT"}
    if mode not in valid:
        raise ValueError(f"Invalid publish mode: {publish_mode}")

    if master.empty:
        preserved = master.copy()
        replaced = 0
    else:
        same_publication = (
            master["Event Family"].map(clean).str.upper().eq(family.upper())
            & master["Cycle ID"].map(clean).eq(cycle_id)
        )

        if mode == "FULL_CYCLE_REPLACE":
            replace_mask = same_publication
        else:
            locations = set(incoming_rows["Location Key"].map(clean))
            replace_mask = (
                same_publication
                & master["Location Key"].map(clean).isin(locations)
            )

        replaced = int(replace_mask.sum())
        preserved = master.loc[~replace_mask].copy()

    combined = pd.concat(
        [preserved, incoming_rows], ignore_index=True, sort=False
    )

    for col in MASTER_COLUMNS:
        if col not in combined.columns:
            combined[col] = ""

    dupes = find_duplicate_keys(combined)
    if not dupes.empty:
        sample = dupes[
            UNIQUE_KEY_COLUMNS
        ].head(20).to_string(index=False)
        raise ValueError(
            "Duplicate production keys remain after merge.\n" + sample
        )

    combined["_priority_num"] = pd.to_numeric(
        combined["Display Priority"], errors="coerce"
    ).fillna(DEFAULT_DISPLAY_PRIORITY)

    combined = combined.sort_values(
        [
            "Date", "Location Key", "_priority_num",
            "Event Family", "Event Name", "Action Role",
        ],
        kind="stable",
    ).drop(columns=["_priority_num"])

    return combined.reset_index(drop=True), replaced


def backup_master(path: Path) -> Path | None:
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"{path.stem}_{stamp}{path.suffix}"
    shutil.copy2(path, backup)
    return backup


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)


def publication_summary(
    source_path: Path,
    incoming: pd.DataFrame,
    event_name: str,
    family: str,
    cycle_id: str,
    period: str,
    replaced: int,
    publish_mode: str,
) -> None:
    print("\n" + "-" * 100)
    print(f"Source file        : {source_path}")
    print(f"Event family       : {family}")
    print(f"Event name         : {event_name}")
    print(f"Publication ID     : {cycle_id}")
    print(f"Publication period : {period}")
    print(f"Publish scope      : {publish_mode}")
    print(f"Incoming rows      : {len(incoming)}")
    print(f"Incoming locations : {incoming['Location Key'].nunique()}")
    print(f"Incoming dates     : {incoming['Date'].nunique()}")
    print(f"Rows replaced      : {replaced}")

    print("Action roles:")
    for role, count in incoming["Action Role"].value_counts().items():
        print(f"  {role}: {count}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="special_events_master_manager.py",
        description=(
            "Validate and publish reviewed festival *_messages.csv files "
            "into the production special-events master. Normal monthly use "
            "requires only --period YYYY-MM; canonical message files are "
            "auto-discovered below festival_runs/YYYY/MM."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        nargs="+",
        default=None,
        help=(
            "Optional explicit reviewed festival *_messages.csv files. "
            "If omitted, --period auto-discovers all canonical monthly "
            "message files."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["full", "selected"],
        required=True,
        help=(
            "full = replace each complete publication unit; "
            "selected = replace only incoming locations within each unit."
        ),
    )
    parser.add_argument(
        "--period",
        default="",
        help=(
            "Publication month YYYY-MM. When --input is omitted this selects "
            "festival_runs/YYYY/MM and auto-discovers canonical "
            "*_messages.csv files. With explicit --input it remains an "
            "optional publication-period override."
        ),
    )
    parser.add_argument(
        "--cities",
        nargs="+",
        default=None,
        help=(
            "For --mode selected, auto-discover subset outputs for these "
            "cities under subset_runs/<city_label>. Use the same city order "
            "used in the festival engine commands."
        ),
    )
    parser.add_argument(
        "--festival-runs-root",
        type=Path,
        default=FESTIVAL_RUNS_ROOT,
        help=(
            f"Festival output root used for monthly auto-discovery "
            f"(default: {FESTIVAL_RUNS_ROOT})."
        ),
    )
    parser.add_argument(
        "--event-id",
        default="",
        help=(
            "Optional explicit Publication/Cycle ID. Allowed only with one "
            "--input file; normally automatic."
        ),
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help=(
            "Write production master after validation. "
            "Without --approve the run is preview-only."
        ),
    )
    parser.add_argument(
        "--master-dir",
        type=Path,
        default=MASTER_DIR,
        help=f"Master directory (default: {MASTER_DIR}).",
    )
    parser.add_argument(
        "--source-module",
        default="",
        help="Optional metadata override; otherwise preserve engine Source Module.",
    )
    parser.add_argument(
        "--rule-version",
        default="",
        help="Optional metadata override; otherwise preserve engine Rule Version.",
    )
    return parser.parse_args()


def main() -> None:
    global MASTER_DIR, BACKUP_DIR, PREVIEW_DIR

    args = parse_args()

    explicit_period = normalize_period(args.period) if args.period else ""

    # Normal production workflow: --period only.
    # Explicit --input remains available for manual/exceptional runs.
    if args.input:
        input_paths = [Path(p) for p in args.input]
        input_mode = "EXPLICIT_INPUTS"
    else:
        if not explicit_period:
            raise ValueError(
                "Provide --period YYYY-MM for automatic input discovery, "
                "or one or more explicit --input files."
            )

        if args.mode == "full":
            input_paths = discover_period_message_files(
                explicit_period,
                Path(args.festival_runs_root),
            )
            input_mode = "MONTHLY_AUTO_DISCOVERY"
        else:
            if not args.cities:
                raise ValueError(
                    "--mode selected without explicit --input requires "
                    "--cities CITY [CITY ...]."
                )
            input_paths = discover_selected_message_files(
                explicit_period,
                Path(args.festival_runs_root),
                args.cities,
            )
            input_mode = "SELECTED_CITY_AUTO_DISCOVERY"

    if args.event_id and len(input_paths) != 1:
        raise ValueError("--event-id may be used only with one input file")

    if args.event_id and input_mode in {"MONTHLY_AUTO_DISCOVERY", "SELECTED_CITY_AUTO_DISCOVERY"}:
        raise ValueError(
            "--event-id is not valid for monthly auto-discovery."
        )

    publish_mode = (
        "FULL_CYCLE_REPLACE"
        if args.mode == "full"
        else "SELECTED_PLACES_UPSERT"
    )

    MASTER_DIR = Path(args.master_dir)
    BACKUP_DIR = MASTER_DIR / "backups"
    PREVIEW_DIR = MASTER_DIR / "previews"
    MASTER_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 100)
    print("SPECIAL EVENTS MASTER - INPUT SELECTION")
    print("=" * 100)
    print(f"Input mode          : {input_mode}")
    if explicit_period:
        print(f"Requested period    : {explicit_period}")
    if input_mode in {"MONTHLY_AUTO_DISCOVERY", "SELECTED_CITY_AUTO_DISCOVERY"}:
        print(f"Festival runs root  : {Path(args.festival_runs_root)}")
    if input_mode == "SELECTED_CITY_AUTO_DISCOVERY":
        print(f"Selected cities     : {', '.join(args.cities or [])}")
    print(f"Message files found : {len(input_paths)}")
    for path in input_paths:
        print(f"  - {path}")

    # Load every input and prepare publication units first. Nothing is written
    # until every file has passed validation.
    publications: list[dict[str, Any]] = []

    for input_path in input_paths:
        input_path = Path(input_path)
        print(f"Reading messages: {input_path}")
        raw = load_csv(input_path)
        incoming, schema = normalize_incoming(raw, input_path)
        validate_incoming(incoming, input_path)

        event_name, cycle_id, period, year = derive_publication_metadata(
            incoming,
            source_path=input_path,
            explicit_period=explicit_period,
            explicit_event_id=args.event_id,
            publish_mode=publish_mode,
        )

        master_rows = transform_to_master(
            incoming=incoming,
            source_path=input_path,
            cycle_id=cycle_id,
            source_module_override=clean(args.source_module),
            rule_version_override=clean(args.rule_version),
        )

        dupes = find_duplicate_keys(master_rows)
        if not dupes.empty:
            sample = dupes[
                UNIQUE_KEY_COLUMNS
            ].head(20).to_string(index=False)
            raise ValueError(
                f"{input_path}: duplicate incoming production keys.\n{sample}"
            )

        publications.append({
            "source_path": input_path,
            "schema": schema,
            "incoming": incoming,
            "master_rows": master_rows,
            "event_name": event_name,
            "family": clean(incoming["Event Family"].iloc[0]).upper(),
            "cycle_id": cycle_id,
            "period": period,
            "year": year,
        })

    # One monthly auto-discovery run should contain exactly one source file
    # for each publication unit. Reject accidental duplicate canonical outputs.
    publication_units: dict[tuple[str, str], list[Path]] = {}
    for pub in publications:
        key = (pub["family"], pub["cycle_id"])
        publication_units.setdefault(key, []).append(pub["source_path"])

    duplicated_units = {
        key: paths
        for key, paths in publication_units.items()
        if len(paths) > 1
    }
    if duplicated_units:
        lines = []
        for (family, cycle_id), paths in sorted(duplicated_units.items()):
            lines.append(f"{family} / {cycle_id}")
            for path in paths:
                lines.append(f"    {path}")
        raise ValueError(
            "Multiple input files map to the same publication unit. "
            "Remove the duplicate/stale output before publishing:\n"
            + "\n".join(lines)
        )

    # Merge all validated publications into in-memory year masters.
    years = sorted({int(pub["year"]) for pub in publications})
    working: dict[int, pd.DataFrame] = {}
    original: dict[int, pd.DataFrame] = {}

    for year in years:
        path = MASTER_DIR / MASTER_FILE_TEMPLATE.format(year=year)
        original[year] = load_existing_master(path)
        working[year] = original[year].copy()

    for pub in publications:
        year = int(pub["year"])
        merged, replaced = merge_publication(
            master=working[year],
            incoming_rows=pub["master_rows"],
            family=pub["family"],
            cycle_id=pub["cycle_id"],
            publish_mode=publish_mode,
        )
        working[year] = merged
        pub["replaced"] = replaced

    print("\n" + "=" * 100)
    print("SPECIAL EVENTS MASTER - GENERIC PUBLICATION SUMMARY")
    print("=" * 100)
    print(f"Publication units : {len(publications)}")
    print(f"Input selection   : {input_mode}")
    print(f"Mode              : {'APPROVE / WRITE' if args.approve else 'PREVIEW ONLY'}")

    for pub in publications:
        publication_summary(
            source_path=pub["source_path"],
            incoming=pub["incoming"],
            event_name=pub["event_name"],
            family=pub["family"],
            cycle_id=pub["cycle_id"],
            period=pub["period"],
            replaced=pub["replaced"],
            publish_mode=publish_mode,
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for year in years:
        preview_path = (
            PREVIEW_DIR
            / f"special_events_master_{year}_preview_{stamp}.csv"
        )
        atomic_write_csv(working[year], preview_path)
        print(
            f"\nPreview {year}: {preview_path} "
            f"({len(working[year])} total master rows)"
        )

    if not args.approve:
        print("\nPREVIEW ONLY - production master was NOT changed.")
        print(
            "Review the preview(s). If correct, rerun the same command "
            "with --approve."
        )
        if input_mode == "MONTHLY_AUTO_DISCOVERY":
            print(
                "Monthly command: "
                f"python special_events_master_manager.py "
                f"--period {explicit_period} --mode {args.mode} --approve"
            )
        elif input_mode == "SELECTED_CITY_AUTO_DISCOVERY":
            city_args = " ".join(args.cities or [])
            print(
                "Selected-city command: "
                f"python special_events_master_manager.py "
                f"--period {explicit_period} --mode selected "
                f"--cities {city_args} --approve"
            )
        return

    # Backup all affected masters before the first production write.
    for year in years:
        master_path = MASTER_DIR / MASTER_FILE_TEMPLATE.format(year=year)
        backup = backup_master(master_path)
        if backup:
            print(f"Backup {year}: {backup}")
        else:
            print(f"Backup {year}: none (first master creation)")

    # All inputs have already validated and all backups are complete.
    for year in years:
        master_path = MASTER_DIR / MASTER_FILE_TEMPLATE.format(year=year)
        atomic_write_csv(working[year], master_path)
        print(
            f"APPROVED master written: {master_path} "
            f"({len(working[year])} rows)"
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
