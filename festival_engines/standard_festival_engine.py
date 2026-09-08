from __future__ import annotations

import argparse
import re

import pandas as pd

from festival_engine_common import (
    MESSAGE_COLUMNS, clean, event_output_dir, load_discovery,
    make_base_message_row, write_df,
)

SUPPORTED = {
    "NAG_PANCHAMI": {
        "family": "NAG_PANCHAMI",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "nag_panchami",
        "rule_version": "NAG_PANCHAMI_V2_0_DISCOVERY_EVIDENCE",
    },
    "VARAMAHALAKSHMI": {
        "family": "VARAMAHALAKSHMI",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "varamahalakshmi",
        "rule_version": "VARAMAHALAKSHMI_V2_0_DISCOVERY_EVIDENCE",
    },
    "KALKI_JAYANTHI": {
        "family": "KALKI_JAYANTHI",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "kalki_jayanthi",
        "rule_version": "KALKI_JAYANTHI_V2_0_DISCOVERY_EVIDENCE",
    },
    "VARAHA_JAYANTHI": {
        "family": "VARAHA_JAYANTHI",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "varaha_jayanthi",
        "rule_version": "VARAHA_JAYANTHI_V1_0_MONTH_DISCOVERY",
    },
    "GANESHA_CHATURTHI": {
        "family": "GANESHA_CHATURTHI",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "ganesha_chaturthi",
        "rule_version": "GANESHA_CHATURTHI_V1_0_MONTH_DISCOVERY",
    },
    "RISHI_PANCHAMI": {
        "family": "RISHI_PANCHAMI",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "rishi_panchami",
        "rule_version": "RISHI_PANCHAMI_V1_0_MONTH_DISCOVERY",
    },
    "VAMANA_JAYANTHI": {
        "family": "VAMANA_JAYANTHI",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "vamana_jayanthi",
        "rule_version": "VAMANA_JAYANTHI_V1_0_MONTH_DISCOVERY",
    },
    "ANANTA_CHATURDASHI": {
        "family": "ANANTA_CHATURDASHI",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "ananta_chaturdashi",
        "rule_version": "ANANTA_CHATURDASHI_V1_0_MONTH_DISCOVERY",
    },
    "PITRUPAKSHA_BEGINS": {
        "family": "PITRUPAKSHA_BEGINS",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "pitrupaksha_begins",
        "rule_version": "PITRUPAKSHA_BEGINS_V1_0_MONTH_DISCOVERY",
    },
    "NAVRATRI_BEGINS": {
        "family": "NAVRATRI_BEGINS",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "navratri_begins",
        "rule_version": "NAVRATRI_BEGINS_V1_0_MONTH_DISCOVERY",
    },
    "SARASWATI_PUJA": {
        "family": "SARASWATI_PUJA",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "saraswati_puja",
        "rule_version": "SARASWATI_PUJA_V1_0_MONTH_DISCOVERY",
    },
    "DURGA_ASHTAMI": {
        "family": "DURGA_ASHTAMI",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "durga_ashtami",
        "rule_version": "DURGA_ASHTAMI_V1_0_MONTH_DISCOVERY",
    },
    "MAHA_NAVAMI": {
        "family": "MAHA_NAVAMI",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "maha_navami",
        "rule_version": "MAHA_NAVAMI_V1_0_MONTH_DISCOVERY",
    },
    "VIJAYADASHAMI": {
        "family": "VIJAYADASHAMI",
        "event_type": "STANDARD_MONTH_FESTIVAL",
        "folder": "vijayadashami",
        "rule_version": "VIJAYADASHAMI_V1_0_MONTH_DISCOVERY",
    },
}

MADHWA_JAYANTHI = {
    "family": "MADHWA_JAYANTHI",
    "event_name": "Madhwa Jayanthi",
    "event_type": "DERIVED_COMPANION_OBSERVANCE",
    "folder": "madhwa_jayanthi",
    "condition_code": "SAME_DATE_AS_VIJAYADASHAMI",
    "rule_version": "MADHWA_JAYANTHI_V1_0_VIJAYADASHAMI_DATE",
}


def build_public_message(engine_key: str, public_name: str) -> str:
    if engine_key == "PITRUPAKSHA_BEGINS":
        return "Pitrupaksha begins today."
    if engine_key == "NAVRATRI_BEGINS":
        return "Navratri begins today."
    return f"{public_name} is observed today."


SOURCE_MODULE = "standard_festival_engine.py"

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Convert city-specific Month-Panchang discovery rows into messages "
            "for label-only standard festivals."
        )
    )
    p.add_argument("--month", required=True, help="YYYY-MM")
    p.add_argument("--engine-key", required=True, choices=sorted(SUPPORTED))
    p.add_argument("--canonical", required=True)
    p.add_argument("--discovery-file")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--all-cities", action="store_true")
    group.add_argument("--cities", nargs="+")
    return p.parse_args()

def selected_run_label(city_names: list[str] | None) -> str:
    names = [
        re.sub(r"[^a-z0-9]+", "_", clean(name).lower()).strip("_")
        for name in (city_names or [])
        if clean(name)
    ]
    if not names:
        return "selected"
    if len(names) <= 4:
        return "_".join(names)
    return "_".join(names[:3]) + f"_plus_{len(names) - 3}"


def main() -> None:
    args = parse_args()
    cfg = SUPPORTED[args.engine_key]
    rows = load_discovery(
        args.month, args.engine_key, args.canonical, args.discovery_file,
        args.all_cities, args.cities,
    )
    representative = rows["Observed Festival"].map(clean).value_counts().index[0]
    base_out_dir = event_output_dir(args.month, cfg["folder"], representative)
    if args.cities:
        out_dir = base_out_dir / "subset_runs" / selected_run_label(args.cities)
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = base_out_dir

    audit_rows = []
    messages = []
    madhwa_audit_rows = []
    madhwa_messages = []
    for _, row in rows.iterrows():
        observed = clean(row["Observed Festival"])
        public_name = args.canonical or observed
        date_str = clean(row["Displayed Date"])

        # This deliberately mirrors the old scanner's behavior for these
        # allowlisted festivals: the Month-Panchang listing itself supplies
        # the local festival date; there is no separate detail-page rule engine.
        status = "COMPLETE" if date_str and observed else "INCOMPLETE"
        condition = "MONTH_PANCHANG_DISCOVERY_DATE"
        message = build_public_message(args.engine_key, public_name)

        messages.append(make_base_message_row(
            row,
            event_family=cfg["family"],
            event_name=public_name,
            event_type=cfg["event_type"],
            condition_code=condition,
            special_details="",
            note="",
            message=message,
            completeness=status,
            source_module=SOURCE_MODULE,
            rule_version=cfg["rule_version"],
        ))
        audit_rows.append({
            **{k: clean(row.get(k, "")) for k in row.index},
            "Selection Basis": (
                "City-specific festival date listed on Drik Month Panchang"
            ),
            "Discovery Event Detail URL": clean(
                row.get("Event Detail URL", "")
            ),
            "Event Link Status": clean(
                row.get("Event Link Status", "")
            ),
            "Completeness Status": status,
        })

        # Madhwa Jayanthi is intentionally not a separate Drik-discovered label.
        # For this publication, it follows the same city-specific selected date
        # as Vijayadashami (Dussehra). Keep it in its own messages file/event
        # family so the special-events master can validate and publish it as an
        # independent canonical event.
        if args.engine_key == "VIJAYADASHAMI":
            madhwa_messages.append(make_base_message_row(
                row,
                event_family=MADHWA_JAYANTHI["family"],
                event_name=MADHWA_JAYANTHI["event_name"],
                event_type=MADHWA_JAYANTHI["event_type"],
                condition_code=MADHWA_JAYANTHI["condition_code"],
                special_details="",
                note="",
                message="Madhwa Jayanthi is observed today.",
                completeness=status,
                source_module=SOURCE_MODULE,
                rule_version=MADHWA_JAYANTHI["rule_version"],
            ))
            madhwa_audit_rows.append({
                **{k: clean(row.get(k, "")) for k in row.index},
                "Selection Basis": (
                    "Same city-specific date as Vijayadashami (Dussehra); "
                    "Madhwa Jayanthi is published by configured companion rule"
                ),
                "Derived Event Name": MADHWA_JAYANTHI["event_name"],
                "Companion Of": public_name,
                "Discovery Event Detail URL": clean(
                    row.get("Event Detail URL", "")
                ),
                "Event Link Status": clean(
                    row.get("Event Link Status", "")
                ),
                "Completeness Status": status,
            })

    audit_df = pd.DataFrame(audit_rows)
    messages_df = pd.DataFrame(messages)
    prefix = cfg["folder"]
    audit_path = out_dir / f"{prefix}_audit.csv"
    messages_path = out_dir / f"{prefix}_messages.csv"
    write_df(audit_path, audit_df)
    write_df(messages_path, messages_df, MESSAGE_COLUMNS)

    print(f"\n{args.engine_key} rows: {len(messages_df)}")
    print(f"Complete: {(messages_df['Completeness Status'] == 'COMPLETE').sum()}")
    print(f"Incomplete: {(messages_df['Completeness Status'] != 'COMPLETE').sum()}")
    print(f"Audit: {audit_path}")
    print(f"Messages: {messages_path}")

    if args.engine_key == "VIJAYADASHAMI":
        companion_base_out_dir = event_output_dir(
            args.month,
            MADHWA_JAYANTHI["folder"],
            MADHWA_JAYANTHI["event_name"],
        )
        if args.cities:
            companion_out_dir = (
                companion_base_out_dir
                / "subset_runs"
                / selected_run_label(args.cities)
            )
            companion_out_dir.mkdir(parents=True, exist_ok=True)
        else:
            companion_out_dir = companion_base_out_dir

        madhwa_audit_df = pd.DataFrame(madhwa_audit_rows)
        madhwa_messages_df = pd.DataFrame(madhwa_messages)
        madhwa_audit_path = companion_out_dir / "madhwa_jayanthi_audit.csv"
        madhwa_messages_path = companion_out_dir / "madhwa_jayanthi_messages.csv"
        write_df(madhwa_audit_path, madhwa_audit_df)
        write_df(madhwa_messages_path, madhwa_messages_df, MESSAGE_COLUMNS)

        print(f"\nMADHWA_JAYANTHI companion rows: {len(madhwa_messages_df)}")
        print(
            "Complete: "
            f"{(madhwa_messages_df['Completeness Status'] == 'COMPLETE').sum()}"
        )
        print(
            "Incomplete: "
            f"{(madhwa_messages_df['Completeness Status'] != 'COMPLETE').sum()}"
        )
        print(f"Audit: {madhwa_audit_path}")
        print(f"Messages: {madhwa_messages_path}")

if __name__ == "__main__":
    main()
