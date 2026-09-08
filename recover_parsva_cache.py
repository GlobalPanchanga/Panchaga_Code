import pandas as pd
from pathlib import Path

audit_path = Path(
    r"festival_runs\2026\09\ekadashi\parsva_ekadashi\parsva_ekadashi_audit.csv"
)
source_cache_path = Path(
    r"output_ekadashi_cycle\cache\ekadashi_scan_cache.csv"
)
prod_cache_path = Path(
    r"festival_runs\cache\ekadashi_scan_cache.csv"
)

audit = pd.read_csv(
    audit_path,
    dtype=str,
    keep_default_na=False,
    encoding="utf-8-sig",
)

src = pd.read_csv(
    source_cache_path,
    dtype=str,
    keep_default_na=False,
    encoding="utf-8-sig",
)

prod = pd.read_csv(
    prod_cache_path,
    dtype=str,
    keep_default_na=False,
    encoding="utf-8-sig",
)

# ------------------------------------------------------------
# 1. Validate successful Parsva audit
# ------------------------------------------------------------

if len(audit) != 95:
    raise RuntimeError(
        f"Expected 95 audit rows, found {len(audit)}"
    )

bad = audit[
    audit["Completeness Status"].str.strip() != "COMPLETE"
]

if not bad.empty:
    raise RuntimeError(
        f"Audit contains {len(bad)} non-COMPLETE cities"
    )

# Sum of actual browser scans recorded in audit.
fresh_scan_total = pd.to_numeric(
    audit["Fresh Scans This City"],
    errors="coerce",
).fillna(0).astype(int).sum()

print("Fresh scans recorded by audit:", fresh_scan_total)

if fresh_scan_total != 342:
    raise RuntimeError(
        f"Expected successful Parsva run to record 342 fresh scans, "
        f"found {fresh_scan_total}"
    )

# ------------------------------------------------------------
# 2. Build every publication-city/date usage
# ------------------------------------------------------------

usage_rows = []

for _, row in audit.iterrows():

    city = row["City"].strip()
    place_key = row["Place Key"].strip()

    dates = [
        x.strip()
        for x in row["Dates Used"].split("|")
        if x.strip()
    ]

    for date in dates:
        usage_rows.append(
            {
                "City": city,
                "Place Key": place_key,
                "Date": date,
            }
        )

usage = pd.DataFrame(usage_rows)

print("Total city/date usages       :", len(usage))

# ------------------------------------------------------------
# 3. Cache identity is Place Key + Date.
#
# Multiple publication cities may intentionally share a Drik
# source Place Key. Therefore fresh scans can exceed the number
# of unique cache records.
# ------------------------------------------------------------

duplicate_usage = usage[
    usage.duplicated(
        ["Place Key", "Date"],
        keep=False,
    )
].sort_values(
    ["Place Key", "Date", "City"]
)

if not duplicate_usage.empty:
    print()
    print("Shared cache identities detected:")
    print(
        duplicate_usage.to_string(index=False)
    )

wanted = set(
    zip(
        usage["Place Key"].str.strip(),
        usage["Date"].str.strip(),
    )
)

print()
print(
    "Unique Place Key + Date pairs:",
    len(wanted),
)

# ------------------------------------------------------------
# 4. Extract exactly those refreshed observations
# ------------------------------------------------------------

src["_key"] = list(
    zip(
        src["Place Key"].str.strip(),
        src["Date"].str.strip(),
    )
)

fresh = src[
    src["_key"].isin(wanted)
].copy()

fresh_keys = set(fresh["_key"])

missing = wanted - fresh_keys

if missing:
    raise RuntimeError(
        f"Temporary refreshed cache is missing "
        f"{len(missing)} required Place Key + Date rows. "
        f"Examples: {sorted(missing)[:10]}"
    )

if fresh["_key"].duplicated().any():
    raise RuntimeError(
        "Temporary cache contains duplicate Place Key + Date rows"
    )

if len(fresh) != len(wanted):
    raise RuntimeError(
        f"Expected {len(wanted)} unique refreshed rows, "
        f"found {len(fresh)}"
    )

print(
    "Fresh unique rows recovered  :",
    len(fresh),
)

# ------------------------------------------------------------
# 5. Upsert into production cache
# ------------------------------------------------------------

prod["_key"] = list(
    zip(
        prod["Place Key"].str.strip(),
        prod["Date"].str.strip(),
    )
)

existing_keys = set(prod["_key"])

replaced = len(
    fresh_keys & existing_keys
)

added = len(
    fresh_keys - existing_keys
)

# Remove ONLY old versions of the exact refreshed cache keys.
prod_keep = prod[
    ~prod["_key"].isin(fresh_keys)
].copy()

merged = pd.concat(
    [prod_keep, fresh],
    ignore_index=True,
)

if merged["_key"].duplicated().any():
    raise RuntimeError(
        "Duplicate cache keys remain after merge"
    )

merged = merged.drop(
    columns=["_key"]
)

merged = merged.sort_values(
    ["Place Key", "Date"],
    kind="stable",
)

merged.to_csv(
    prod_cache_path,
    index=False,
    encoding="utf-8-sig",
)

print()
print("=" * 70)
print("PARSVA CACHE RECOVERY COMPLETE")
print("=" * 70)
print("Browser fresh scans     :", fresh_scan_total)
print("Unique refreshed rows   :", len(fresh))
print("Rows replaced           :", replaced)
print("Rows newly added        :", added)
print("Final production cache  :", len(merged))
print("Production cache        :", prod_cache_path)
