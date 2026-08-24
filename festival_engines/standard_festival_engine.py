from __future__ import annotations

import argparse

import pandas as pd

from festival_engine_common import (
    MESSAGE_COLUMNS, clean, selected_event_output_dir, load_discovery,
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
}

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

def main() -> None:
    args = parse_args()
    cfg = SUPPORTED[args.engine_key]
    rows = load_discovery(
        args.month, args.engine_key, args.canonical, args.discovery_file,
        args.all_cities, args.cities,
    )
    representative = rows["Observed Festival"].map(clean).value_counts().index[0]
    out_dir = selected_event_output_dir(args.month, cfg["folder"], representative, args.cities)

    audit_rows = []
    messages = []
    for _, row in rows.iterrows():
        observed = clean(row["Observed Festival"])
        public_name = args.canonical or observed
        date_str = clean(row["Displayed Date"])

        # This deliberately mirrors the old scanner's behavior for these
        # allowlisted festivals: the Month-Panchang listing itself supplies
        # the local festival date; there is no separate detail-page rule engine.
        status = "COMPLETE" if date_str and observed else "INCOMPLETE"
        condition = "MONTH_PANCHANG_DISCOVERY_DATE"
        message = f"{public_name} is observed today."

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

if __name__ == "__main__":
    main()
