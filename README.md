# Nitya Panchanga – Multi-City Panchanga & Festival Pipeline

A production-oriented pipeline for generating **daily Hindu Panchanga pages** and
**location-specific vrata / festival information** for multiple cities using
Drik Panchang as the source of Panchanga observations and festival discovery.

The system is designed around four principles:

1. **Daily Panchanga data is persistent.**  
   Historical data is stored in yearly Panchanga master files and is not
   re-scraped just to rebuild HTML.

2. **Festival discovery and festival calculation are separate.**  
   The Month Panchang page tells us *what event exists and approximately where
   to look*. The appropriate festival engine decides the final result.

3. **Special events are published only after review.**  
   Festival engines write audit/message files first. The
   `special_events_master_manager.py` previews changes before anything is added
   to the production Special Events Master.

4. **Backfills are safe.**  
   Selected-city festival runs are written under `subset_runs/...` so that a
   new-city or repair run cannot overwrite the already reviewed all-city
   festival outputs.

---

## Table of Contents

- [1. High-Level Architecture](#1-high-level-architecture)
- [2. Repository Layout](#2-repository-layout)
- [3. Important Data Concepts](#3-important-data-concepts)
- [4. Supported Festival Engine Types](#4-supported-festival-engine-types)
- [5. Initial Environment Setup](#5-initial-environment-setup)
- [6. Normal Daily Production Run](#6-normal-daily-production-run)
- [7. Starting a New Month](#7-starting-a-new-month)
- [8. Adding a New City](#8-adding-a-new-city)
- [9. Historical Backfill](#9-historical-backfill)
- [10. Festival Backfill for New Cities](#10-festival-backfill-for-new-cities)
- [11. Publishing Festival Results](#11-publishing-festival-results)
- [12. Rebuilding HTML Without Drik Calls](#12-rebuilding-html-without-drik-calls)
- [13. Rebuilding Only the Website Index](#13-rebuilding-only-the-website-index)
- [14. Correcting Existing Data](#14-correcting-existing-data)
- [15. Festival-Specific Notes](#15-festival-specific-notes)
- [16. Discovery Resume, Cache, CAPTCHA, and Failures](#16-discovery-resume-cache-captcha-and-failures)
- [17. Adding a New Festival Type](#17-adding-a-new-festival-type)
- [18. GitHub / GitHub Pages Workflow](#18-github--github-pages-workflow)
- [19. What Should Be Committed to Git](#19-what-should-be-committed-to-git)
- [20. Recommended `.gitignore`](#20-recommended-gitignore)
- [21. Production Safety Rules](#21-production-safety-rules)
- [22. Command Cheat Sheet](#22-command-cheat-sheet)
- [23. Validated Backfill Example](#23-validated-backfill-example)

---

# 1. High-Level Architecture

The production flow is:

```text
cities_panchanga_updated.csv
        |
        +----------------------+
        |                      |
        v                      v
Daily Panchanga           Monthly Festival
Scanner                    Discovery
        |                      |
        v                      v
panchanga_data/          festival_runs/YYYY/MM/
panchanga_master_YYYY.csv      |
        |                      v
        |                Festival Engines
        |                      |
        |                      v
        |                *_audit.csv
        |                *_messages.csv
        |                      |
        |                      v
        |            special_events_master_manager.py
        |                      |
        |                      v
        |            special_events_master_YYYY.csv
        |                      |
        +-----------+----------+
                    |
                    v
          panchanga_daily_scanner.py
                 rebuild/daily
                    |
                    v
        output_2o/weekly_panchanga/
          ├─ daily_text_files/
          ├─ weekly_panchanga_results.csv
          └─ website/
               ├─ YYYY-MM-DD.html
               └─ index.html
```

The **Panchanga Master** contains normal daily Panchanga observations.

The **Special Events Master** contains reviewed festival / vrata publication
rows.

The daily HTML renderer combines the two.

---

# 2. Repository Layout

Recommended production layout:

```text
project-root/
│
├─ panchanga_daily_scanner.py
├─ special_events_master_manager.py
├─ cities_panchanga_updated.csv
├─ festival_registry.csv
│
├─ festival_discovery/
│  └─ month_festival_discovery.py
│
├─ festival_engines/
│  ├─ festival_engine_common.py
│  ├─ standard_festival_engine.py
│  ├─ sankramana_engine.py
│  ├─ grahana_engine.py
│  ├─ ekadashi_cycle_scanner.py
│  └─ krishna_jayanthi_engine.py
│
├─ panchanga_data/
│  ├─ panchanga_master_2026.csv
│  └─ backups/
│
├─ special_events_master/
│  ├─ special_events_master_2026.csv
│  ├─ backups/
│  ├─ previews/
│  └─ legacy_import/
│
├─ festival_runs/
│  ├─ cache/
│  └─ YYYY/
│     └─ MM/
│        ├─ festival_discovery_raw_YYYY_MM.csv
│        ├─ festival_discovery_YYYY_MM.csv
│        ├─ festival_discovery_status_YYYY_MM.csv
│        ├─ festival_jobs_YYYY_MM.csv
│        ├─ run_supported_festivals_YYYY_MM.ps1
│        ├─ ekadashi/
│        ├─ grahana/
│        ├─ sankramana/
│        ├─ nag_panchami/
│        ├─ varamahalakshmi/
│        ├─ kalki_jayanthi/
│        ├─ krishna_jayanthi/
│        └─ subset_runs/
│
└─ output_2o/
   └─ weekly_panchanga/
      ├─ last_scan_results.csv
      ├─ weekly_panchanga_results.csv
      ├─ daily_text_files/
      └─ website/
         ├─ 2026-06-16.html
         ├─ ...
         ├─ 2026-08-31.html
         └─ index.html
```

`festival_runs` is an **audit/work area**.  
The two production masters are:

```text
panchanga_data/panchanga_master_YYYY.csv
special_events_master/special_events_master_YYYY.csv
```

---

# 3. Important Data Concepts

## 3.1 `display_city`

This is the publication-facing city name and the name used with:

```powershell
--cities "City Name"
```

Use the exact value from `cities_panchanga_updated.csv`.

---

## 3.2 Drik source identity vs publication identity

These are intentionally different concepts.

A configured publication city may use the same underlying Drik source location
as another city. For example, if Drik does not provide a separate location for
a configured place, it may intentionally use a nearby Drik source.

The pipeline therefore distinguishes:

```text
Source identity
    -> the Drik place / geoname used to obtain observations

Publication identity
    -> the configured city shown to the user
```

The publication identity is protected by a city-qualified **Location Key**.

Conceptually:

```text
Location Key = Place Key + City
```

Two configured cities may therefore share one Drik source while remaining
separate publication locations.

This is especially important for caches. A raw source observation may be
reused when two configured cities deliberately point at the same Drik source,
while their published rows remain separate.

Do not change this behavior merely to force every display city to have a unique
raw cache key.

---

## 3.3 Panchanga Master

Stored as:

```text
panchanga_data/panchanga_master_YYYY.csv
```

The daily scanner UPSERTs city/date rows into this file.

Historical HTML can later be regenerated from this master **without scraping
Drik again**.

---

## 3.4 Special Events Master

Stored as:

```text
special_events_master/special_events_master_YYYY.csv
```

This contains reviewed festival rows only.

Festival engines do **not** directly modify this file.

The only normal path into the production Special Events Master is:

```text
festival engine
    -> *_messages.csv
    -> preview
    -> approve
    -> special_events_master_YYYY.csv
```

---

# 4. Supported Festival Engine Types

Festival processing is not one universal algorithm.

The registry routes each festival to the correct engine.

## 4.1 Independent rule engines – `ANCHOR_SEARCH`

These use the Month Panchang date only as a **search anchor**.

The discovered date is **not automatically the final observance date**.

Current examples:

```text
EKADASHI
KRISHNA_JAYANTHI
```

The dedicated engine independently evaluates local Panchanga evidence.

---

## 4.2 City-specific exact-date engines

These use the exact date displayed for each city in monthly discovery.

Current examples:

```text
GRAHANA
SANKRAMANA
NAG_PANCHAMI
VARAMAHALAKSHMI
KALKI_JAYANTHI
```

There are two subtypes.

### A. Date-only standard festivals

Handled by:

```text
festival_engines/standard_festival_engine.py
```

Examples:

```text
Nag Panchami
Varamahalakshmi Vrata
Kalki Jayanthi
```

The discovery date is the required publication date. The event detail URL is
retained as audit evidence but does not need to be opened.

### B. Detail-scraping festivals

Examples:

```text
Sankramana
Grahana
```

For these events the required detail is location-specific.

The production architecture is:

```text
1. Monthly discovery identifies city + exact displayed date.
2. Discovery captures the event link/href while the city Month Panchang is open.
3. Detail engine opens that exact city/date Month Panchang.
4. This establishes the correct Drik city context.
5. The engine then opens the captured event detail URL in the same browser context.
6. Requested local details are scraped.
```

Do not make URL reconstruction or neighboring-date searching the normal path.

---

# 5. Initial Environment Setup

The project is normally run from PowerShell.

Create and activate a virtual environment if needed:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install project dependencies. At minimum the current scripts require Python
packages such as Pandas and Playwright:

```powershell
pip install pandas playwright
python -m playwright install chromium
```

Run from the project root so relative paths resolve correctly:

```text
C:\...\drikpanchag>
```

The Playwright browser profile is normally:

```text
playwright_profile/
```

The browser is intentionally headed by default in several scanners because
Drik may occasionally require CAPTCHA/manual interaction.

---

# 6. Normal Daily Production Run

Once the monthly Special Events Master is prepared, normal day-to-day use is
simple.

## Today

```powershell
python panchanga_daily_scanner.py daily
```

## Explicit date

```powershell
python panchanga_daily_scanner.py daily --date 2026-09-01
```

## What the daily run does

```text
1. Scrapes normal Panchanga for configured cities.
2. Safely UPSERTs new city/date rows into panchanga_master_YYYY.csv.
3. Loads the approved special_events_master_YYYY.csv.
4. Applies festival rows relevant to the requested date.
5. Regenerates publication CSV/text/HTML.
6. Regenerates index.html while preserving historical date pages already
   present in the website directory.
```

Important outputs:

```text
output_2o/weekly_panchanga/last_scan_results.csv
output_2o/weekly_panchanga/weekly_panchanga_results.csv
output_2o/weekly_panchanga/daily_text_files/
output_2o/weekly_panchanga/website/YYYY-MM-DD.html
output_2o/weekly_panchanga/website/index.html
```

After a daily run, inspect the generated date HTML before publishing it.

---

# 7. Starting a New Month

A new month should normally be prepared **before relying on daily publication**.

For example, for September 2026:

## Step 1 – Run monthly festival discovery

```powershell
python festival_discovery\month_festival_discovery.py `
    --month 2026-09 `
    --all-cities
```

Discovery produces:

```text
festival_discovery_raw_2026_09.csv
festival_discovery_2026_09.csv
festival_discovery_status_2026_09.csv
festival_jobs_2026_09.csv
run_supported_festivals_2026_09.ps1
```

Review the summary.

Do not proceed as if discovery is complete when it reports incomplete cities.

---

## Step 2 – Review the generated jobs

Each discovered supported festival should have:

```text
Job Status = READY
```

The generated `.ps1` contains the recommended engine commands.

Discovery can also report:

```text
DISCOVERY_INCOMPLETE
ENGINE_NOT_IMPLEMENTED
DATE_STRATEGY_NOT_DEFINED
```

Those conditions must be resolved before publication.

---

## Step 3 – Run the festival engines

When discovery is complete:

```powershell
& ".\festival_runs\2026\09\run_supported_festivals_2026_09.ps1"
```

You can also run commands individually when validating a new month.

---

## Step 4 – Review audit and message files

For each engine inspect:

```text
*_audit.csv
*_messages.csv
```

Audit files contain detailed evidence.

Message files contain the concise rows intended for publication.

For rule engines, every requested city should be `COMPLETE` before approving
that publication unit.

---

## Step 5 – Preview the monthly Special Events Master

```powershell
python special_events_master_manager.py `
    --period 2026-09 `
    --mode full
```

This is preview-only.

The manager auto-discovers the **canonical** monthly `*_messages.csv` files and
does not treat `subset_runs` as full production units.

Review:

```text
publication units
incoming rows
incoming locations
incoming dates
action-role counts
rows replaced
final master row count
```

---

## Step 6 – Approve

Only after the preview is correct:

```powershell
python special_events_master_manager.py `
    --period 2026-09 `
    --mode full `
    --approve
```

---

## Step 7 – Run normal daily Panchanga

Once the monthly Special Events Master is approved:

```powershell
python panchanga_daily_scanner.py daily --date 2026-09-01
```

---

# 8. Adding a New City

Adding a city has **two independent responsibilities**:

```text
A. Daily Panchanga history
B. Festival / vrata history
```

Adding a row to the city CSV alone does not backfill either one.

---

## 8.1 Add the city to the city CSV

Update:

```text
cities_panchanga_updated.csv
```

Core fields used by the current scanners include:

```text
display_city
search_city
state_or_region
country
timezone
```

Additional source-location fields such as a geoname/place identifier should be
maintained when the configuration uses them.

`display_city` must be unique enough to act as the publication name.

---

## 8.2 Test one date first

Before scanning an entire month, test a single date:

```powershell
python panchanga_daily_scanner.py backfill `
    --date 2026-08-31 `
    --cities "New City"
```

For multiple cities:

```powershell
python panchanga_daily_scanner.py backfill `
    --date 2026-08-31 `
    --cities "City A" "City B"
```

Confirm:

```text
fresh rows inserted/replaced correctly
expected city count
HTML contains the new city
existing cities are still present
index history is preserved
```

---

## 8.3 Production rule: keep monthly Panchanga and festival coverage aligned

Festival discovery works month-by-month.

Therefore, if a new city is being added historically for a month, the safest
production rule is:

> Backfill normal daily Panchanga for the same month(s) that will receive
> festival backfill.

Example:

```text
If August festivals are being added for a new city,
also provide that city's August daily Panchanga rows.
```

Otherwise the Special Events Master could contain a festival on August 17 for
a city whose daily Panchanga does not exist on August 17.

For a complete August backfill:

```powershell
python panchanga_daily_scanner.py backfill `
    --start 2026-08-01 `
    --end 2026-08-31 `
    --cities "New City"
```

If one date was already tested successfully, it is fine to backfill only the
remaining dates.

---

# 9. Historical Backfill

Use `backfill` when:

```text
a new city is added
a city was missing on historical dates
a city/date row needs to be repaired from Drik
historical coverage is being extended
```

Example:

```powershell
python panchanga_daily_scanner.py backfill `
    --start 2026-06-16 `
    --end 2026-08-31 `
    --cities "New City"
```

Backfill is intentionally safer than a normal all-city scan.

By default it requires `--cities`.

A deliberate all-city historical rescan can use:

```powershell
python panchanga_daily_scanner.py backfill `
    --start 2026-08-01 `
    --end 2026-08-31 `
    --all-cities
```

Use an all-city historical rescan only when it is truly intended.

The scanner creates a Panchanga Master backup before changing the yearly
master.

---

# 10. Festival Backfill for New Cities

After the normal daily Panchanga is present for the target month, backfill the
month's festivals.

Example for two new cities:

```powershell
python festival_discovery\month_festival_discovery.py `
    --month 2026-08 `
    --cities "City A" "City B"
```

## Safe subset behavior

Selected-city discovery:

```text
does not replace the existing all-city discovery
adds/updates those city observations in the canonical discovery data
creates selected job files separately
```

The selected job folder is:

```text
festival_runs/YYYY/MM/subset_runs/<city_label>/
```

For:

```text
City A + City B
```

a typical label is:

```text
city_a_city_b
```

The generated files include:

```text
festival_jobs_YYYY_MM.csv
run_supported_festivals_YYYY_MM.ps1
```

### Important

Use the **same city order** throughout a selected-city backfill.

For example, if discovery is run as:

```powershell
--cities "Kuwait" "Coimbatore"
```

use that same order for engine commands and the selected master-manager command.

This keeps the generated subset folder label consistent.

---

## Run the selected festival commands

You may run the generated file:

```powershell
& ".\festival_runs\2026\08\subset_runs\city_a_city_b\run_supported_festivals_2026_08.ps1"
```

For a first-time backfill it is often safer to execute one engine at a time and
review the output.

Selected engine outputs are isolated under each event's own:

```text
subset_runs/<city_label>/
```

This prevents a two-city backfill from overwriting the reviewed full-city
audit/message files.

---

# 11. Publishing Festival Results

There are two publication modes.

---

## 11.1 Full monthly publication

Use after the normal all-city monthly festival run:

```powershell
python special_events_master_manager.py `
    --period 2026-09 `
    --mode full
```

Then:

```powershell
python special_events_master_manager.py `
    --period 2026-09 `
    --mode full `
    --approve
```

`full` means each complete canonical publication unit is treated as the full
production result for that cycle/event.

---

## 11.2 Selected-place upsert

Use for:

```text
new-city backfill
selected-city repair
partial location correction
```

Preview:

```powershell
python special_events_master_manager.py `
    --period 2026-08 `
    --mode selected `
    --cities "City A" "City B"
```

Approve:

```powershell
python special_events_master_manager.py `
    --period 2026-08 `
    --mode selected `
    --cities "City A" "City B" `
    --approve
```

This auto-discovers the selected `subset_runs/<city_label>` message files.

The publication behavior is:

```text
SELECTED_PLACES_UPSERT
```

Only incoming locations within the relevant publication unit are replaced or
added.

All other already-approved cities remain untouched.

---

## 11.3 Always preview first

Do not jump directly to `--approve` for an unfamiliar run.

The preview is designed to catch:

```text
wrong event
wrong city count
missing locations
duplicate production keys
wrong action roles
unexpected row replacement
unexpected final master size
```

Production master changes are backed up.

---

# 12. Rebuilding HTML Without Drik Calls

Use `rebuild` after:

```text
festival master approval
festival correction
historical Panchanga backfill
manual restoration of production data
HTML template changes
```

Example:

```powershell
python panchanga_daily_scanner.py rebuild `
    --start 2026-08-01 `
    --end 2026-08-31
```

`rebuild` makes **zero Drik calls**.

It reads:

```text
panchanga_master_YYYY.csv
+
special_events_master_YYYY.csv
```

and regenerates publication files.

Important:

> `rebuild` cannot invent missing Panchanga data.

If the Panchanga Master has no row for a city/date, `rebuild` does not scrape
that missing row. Use `backfill` first.

---

# 13. Rebuilding Only the Website Index

The index can be rebuilt independently:

```powershell
python panchanga_daily_scanner.py index
```

This mode:

```text
makes zero Drik calls
does not modify Panchanga masters
does not modify Special Events masters
does not rewrite dated HTML pages
```

It scans the production website directory for top-level files matching:

```text
YYYY-MM-DD.html
```

and rebuilds:

```text
website/index.html
```

The index therefore preserves historical dates that are physically present in
the website folder.

Example:

```text
2026-06-16.html
...
2026-08-31.html
```

produces an index covering June 16 through August 31.

The command also reports missing dates inside the detected range.

### Important

The index cannot preserve a historical page that is absent from the website
folder.

When restoring old history, copy the historical dated HTML pages back first,
then run:

```powershell
python panchanga_daily_scanner.py index
```

Do not restore an obsolete `index.html` over the newly generated one.

---

# 14. Correcting Existing Data

Different problems require different correction paths.

---

## 14.1 Wrong/missing normal Panchanga for one city/date

Re-scan only that city/date:

```powershell
python panchanga_daily_scanner.py backfill `
    --date 2026-08-17 `
    --cities "City Name"
```

Then inspect the master/HTML.

---

## 14.2 Wrong festival discovery date

Re-run discovery for the affected city/month.

If the city is already marked `COMPLETE` and you intentionally want a fresh
Month Panchang scan:

```powershell
python festival_discovery\month_festival_discovery.py `
    --month 2026-08 `
    --cities "City Name" `
    --refresh
```

Do not use `--refresh` merely because a previous run crashed **after**
discovery completed. In that case, rerunning normally should reuse the
`COMPLETE` status.

---

## 14.3 Wrong Sankramana or Grahana cached detail

Both detail engines support an event-level refresh option.

Example:

```powershell
python festival_engines\sankramana_engine.py `
    --month 2026-08 `
    --canonical "Sankramana" `
    --cities "City Name" `
    --refresh-event
```

or:

```powershell
python festival_engines\grahana_engine.py `
    --month 2026-08 `
    --canonical "Chandra Grahana" `
    --cities "City Name" `
    --refresh-event
```

This ignores matching cached rows for that event while preserving unrelated
event cache rows.

After validating the new message:

```text
selected master preview
-> selected approve
-> rebuild affected HTML dates
```

---

## 14.4 Wrong festival result for only a few cities

Do **not** rerun and replace a full production cycle unnecessarily.

Use a selected-city engine run so outputs go to:

```text
subset_runs/<city_label>/
```

Then publish with:

```text
--mode selected
```

---

## 14.5 Full-cycle rule change

If the actual festival algorithm changes and every city needs recomputation:

```text
1. version/update the engine
2. run the full cycle for all cities
3. inspect audit evidence
4. preview master with --mode full
5. approve only after validation
6. rebuild affected dates
```

---

# 15. Festival-Specific Notes

## 15.1 Ekadashi

Ekadashi is an independent rule engine.

The monthly discovered date is only an anchor.

Example:

```powershell
python festival_engines\ekadashi_cycle_scanner.py `
    --cycle "Kamika Ekadashi" `
    --anchor 2026-08-09 `
    --cities "Kuwait" "Coimbatore"
```

For all cities:

```powershell
python festival_engines\ekadashi_cycle_scanner.py `
    --cycle "Kamika Ekadashi" `
    --anchor 2026-08-09 `
    --all-cities
```

The engine independently determines local classification, upavasa and parana.

Typical classifications include:

```text
Normal Ekadashi
Dashami Viddha
Dashami Viddha -> Valid Ekadashi
Ekadashi Dwaya
Dwadashi Dwaya
Shravanopavasa
Missing Ekadashi between sunrises
```

Typical publication roles include:

```text
UPAVASA_DAY_1
UPAVASA_DAY_2
PARANA
VIDDHA_NO_FAST
```

Selected runs are isolated under:

```text
festival_runs/YYYY/MM/ekadashi/<cycle>/subset_runs/<city_label>/
```

The standard shared cache is:

```text
festival_runs/cache/ekadashi_scan_cache.csv
```

Do not replace the production cache behavior merely to force one cache row per
display city when two configured cities intentionally use the same Drik source.

---

## 15.2 Krishna Jayanthi

Sri Krishna Jayanthi uses its own independent rule engine:

```text
festival_engines/krishna_jayanthi_engine.py
```

Monthly discovery provides a candidate anchor.

The engine independently evaluates the local conditions.

A generated command has the general form:

```powershell
python festival_engines\krishna_jayanthi_engine.py `
    --month YYYY-MM `
    --anchor YYYY-MM-DD `
    --window-days N `
    --cities "City A" "City B"
```

Selected-city runs write to:

```text
festival_runs/YYYY/MM/krishna_jayanthi/<event>/subset_runs/<city_label>/
```

Do not replace the Krishna Jayanthi algorithm with the simple standard
date-only engine.

---

## 15.3 Nag Panchami / Varamahalakshmi / Kalki Jayanthi

These are currently standard date-only events.

Example:

```powershell
python festival_engines\standard_festival_engine.py `
    --month 2026-08 `
    --engine-key NAG_PANCHAMI `
    --canonical "Nag Panchami" `
    --cities "City A" "City B"
```

The exact city-specific date comes from discovery.

---

## 15.4 Sankramana

Sankramana requires location-specific detail.

Typical public fields:

```text
Sankranti Moment
Punya Kala
Maha Punya Kala
```

The validated architecture is:

```text
exact city/date Month Panchang
-> establish city context
-> open captured discovery event URL
-> scrape local detail
```

The shared cache is:

```text
festival_runs/cache/sankramana_scan_cache.csv
```

---

## 15.5 Grahana

Grahana requires local visibility determination.

Public visible fields can include:

```text
Sutak Begins
Sutak Ends
Eclipse Start
Eclipse End
```

`Maximum Eclipse` may exist in audit evidence but is intentionally not required
in the concise public message.

If an eclipse is not locally visible, publication can explicitly contain:

```text
NOT_APPLICABLE
```

rather than silently omitting the event.

The shared cache is:

```text
festival_runs/cache/grahana_scan_cache.csv
```

---

# 16. Discovery Resume, Cache, CAPTCHA, and Failures

## 16.1 Discovery is resumable

The monthly discovery keeps a city-status CSV.

If a city is already:

```text
COMPLETE
```

a normal rerun skips it.

Example:

```text
RESUME: skipping COMPLETE city Kuwait
```

This is useful after a failure during later job-generation or browser work.

Use `--refresh` only when you really want Drik to be scanned again.

---

## 16.2 Festival caches are intentional

Caches reduce repeated Drik traffic and make reruns practical.

Typical cache folder:

```text
festival_runs/cache/
```

Do not delete caches as a routine troubleshooting step.

Delete/refresh only when you know the stored observation is wrong or the cache
schema is intentionally being replaced.

---

## 16.3 CAPTCHA

Use headed browser mode when Drik requires human verification.

Avoid designing production logic that assumes CAPTCHA pages are valid
Panchanga content.

If a CAPTCHA interrupts a scan:

```text
resolve it in the visible browser
allow the scanner to continue
or rerun and let completed cities resume from status/cache
```

---

## 16.4 Browser crash

A browser crash does not automatically mean all work from the run was lost.

Check:

```text
discovery status
audit CSV
cache rows
```

before forcing a rescan.

---

# 17. Adding a New Festival Type

Do not immediately create a one-off scraper.

First classify the event.

---

## Type A – Date only

Use when the only required publication value is the correct local festival
date.

Architecture:

```text
Month discovery
-> registry match
-> city-specific displayed date
-> standard_festival_engine.py
-> messages
```

---

## Type B – Additional detail scraping

Use when the festival needs local timings/details from the event page.

Architecture:

```text
Month discovery
-> exact city/date
-> captured event URL
-> open city/date Month Panchang
-> establish city context
-> open captured URL in same browser context
-> scrape requested values
-> audit + messages
```

This is the established pattern for Sankramana and Grahana.

---

## Type C – Independent religious/rule algorithm

Use when the Month Panchang display date is insufficient to determine the
observance.

Architecture:

```text
Month discovery
-> candidate-cycle anchor
-> dedicated rule engine
-> local Panchanga evidence
-> final observance
```

This is the established pattern for:

```text
Ekadashi
Krishna Jayanthi
```

---

## Registry first

Add the festival mapping to:

```text
festival_registry.csv
```

The registry controls observed-name matching, canonical festival name, engine
routing and whether execution is enabled.

Discovery re-applies the current registry to stored raw discovery rows, so a
registry mapping change does not always require a fresh Drik scan.

For a new engine:

```text
1. add registry entry
2. implement engine
3. test a few cities
4. test different geographic regions/date boundaries
5. inspect audit evidence
6. run all cities
7. preview master
8. approve
```

Never enable a new festival engine in production before its output has been
validated.

---

# 18. GitHub / GitHub Pages Workflow

It is useful to separate:

```text
Code/data repository
Website / GitHub Pages publication
```

They may be separate repositories, or separate folders/branches, depending on
the deployment design.

---

## Daily website update

After a successful daily scan:

```text
1. inspect YYYY-MM-DD.html locally
2. inspect index.html
3. upload/commit the new/changed website files
4. verify GitHub Pages
```

---

## Historical rebuild

When a month is regenerated:

```text
1. regenerate the affected local dated HTML files
2. overwrite only those corresponding dated files in GitHub
3. preserve older historical dated pages
4. regenerate index.html from the complete local website directory
5. upload the new index.html
```

Avoid destructive mirror operations that can delete historical files that are
not present in a temporary local output folder.

The preferred local production folder is:

```text
output_2o/weekly_panchanga/website/
```

It should contain the complete website history that you intend GitHub Pages to
serve.

---

# 19. What Should Be Committed to Git

Recommended to commit:

```text
README.md
panchanga_daily_scanner.py
special_events_master_manager.py
cities_panchanga_updated.csv
festival_registry.csv
festival_discovery/
festival_engines/
approved Panchanga masters, if the repository is intended to preserve
production state
approved Special Events masters
algorithm documentation
```

Optional, depending on repository purpose:

```text
festival_runs/YYYY/MM audit/message outputs
generated website pages
```

If reproducibility/audit history is important, monthly festival audit/message
outputs are useful to archive.

If repository size is more important, retain only approved masters and keep
run artifacts elsewhere.

---

## Do not commit

```text
.venv/
__pycache__/
playwright_profile/
browser session data
temporary screenshots
CAPTCHA/session artifacts
local backups/previews unless intentionally archived
```

Be especially careful with `playwright_profile/`. It is a local browser
profile and should not be published.

---

# 20. Recommended `.gitignore`

A practical starting point:

```gitignore
# Python
.venv/
venv/
__pycache__/
*.pyc
*.pyo

# IDE
.idea/
.vscode/

# Playwright/browser profile
playwright_profile/

# Temporary OS/editor files
.DS_Store
Thumbs.db
*.tmp
~$*

# Generated caches
festival_runs/cache/

# Automatic backups and previews
panchanga_data/backups/
special_events_master/backups/
special_events_master/previews/

# Optional: uncomment if generated runtime output should NOT be versioned
# output_2o/
# festival_runs/
```

Decide whether `output_2o/` and monthly `festival_runs/` should be committed
based on whether the repository is primarily:

```text
source code only
or
source + production/audit archive
```

---

# 21. Production Safety Rules

1. **Never treat Month Panchang discovery date as final for an anchor-search
   engine.**

2. **Never publish directly from engine output without the master-manager
   preview.**

3. **Use `--mode selected` for new-city/backfill publication.**

4. **Do not overwrite canonical all-city festival outputs with a selected-city
   test.**  
   Selected runs belong under `subset_runs`.

5. **Keep daily Panchanga coverage aligned with festival coverage for historical
   new-city backfills.**

6. **Use `rebuild` when data already exists in masters.**  
   Do not scrape Drik unnecessarily.

7. **Use `backfill` when Panchanga data is actually missing or must be repaired.**

8. **Use `index` only to regenerate navigation from existing dated HTML pages.**

9. **Do not use `--refresh` / `--refresh-event` casually.**

10. **Do not delete production masters to solve a scanner problem.**

11. **Do not delete historical GitHub date pages when publishing a new month.**

12. **Do not commit the Playwright browser profile.**

13. **A city rename is an identity change.**  
    Because publication identity contains the city name, renaming a configured
    city should be treated as a deliberate migration, not a casual CSV edit.

14. **Removing a city from the configuration affects future scans only.**  
    Historical master rows do not disappear automatically.

15. **Review counts.**  
    City counts, message counts and master row counts are important validation
    signals.

---

# 22. Command Cheat Sheet

## Daily today

```powershell
python panchanga_daily_scanner.py daily
```

## Daily explicit date

```powershell
python panchanga_daily_scanner.py daily --date 2026-09-01
```

## Backfill one date / selected cities

```powershell
python panchanga_daily_scanner.py backfill `
    --date 2026-08-31 `
    --cities "City A" "City B"
```

## Backfill date range

```powershell
python panchanga_daily_scanner.py backfill `
    --start 2026-08-01 `
    --end 2026-08-31 `
    --cities "City A" "City B"
```

## Zero-Drik rebuild

```powershell
python panchanga_daily_scanner.py rebuild `
    --start 2026-08-01 `
    --end 2026-08-31
```

## Index only

```powershell
python panchanga_daily_scanner.py index
```

## Full monthly festival discovery

```powershell
python festival_discovery\month_festival_discovery.py `
    --month 2026-09 `
    --all-cities
```

## Selected-city festival discovery

```powershell
python festival_discovery\month_festival_discovery.py `
    --month 2026-08 `
    --cities "City A" "City B"
```

## Force selected discovery rescan

```powershell
python festival_discovery\month_festival_discovery.py `
    --month 2026-08 `
    --cities "City A" "City B" `
    --refresh
```

## Run generated full-month jobs

```powershell
& ".\festival_runs\2026\09\run_supported_festivals_2026_09.ps1"
```

## Full monthly master preview

```powershell
python special_events_master_manager.py `
    --period 2026-09 `
    --mode full
```

## Full monthly master approve

```powershell
python special_events_master_manager.py `
    --period 2026-09 `
    --mode full `
    --approve
```

## Selected-city master preview

```powershell
python special_events_master_manager.py `
    --period 2026-08 `
    --mode selected `
    --cities "City A" "City B"
```

## Selected-city master approve

```powershell
python special_events_master_manager.py `
    --period 2026-08 `
    --mode selected `
    --cities "City A" "City B" `
    --approve
```

---

# 23. Validated Backfill Example

The selected-city workflow was validated in August 2026 by adding two new
locations and backfilling them into an already approved production month.

The validation sequence was:

```text
1. Add both cities to cities_panchanga_updated.csv.
2. Backfill one test date.
3. Confirm the date HTML contained both cities.
4. Backfill the rest of August daily Panchanga.
5. Run August festival discovery for only those two cities.
6. Discovery resumed correctly and produced 8 supported festival jobs.
7. Run date-only festival engines.
8. Run Sankramana detail engine.
9. Run Surya and Chandra Grahana detail engines.
10. Run both August Ekadashi cycles.
11. Preview selected-city Special Events Master upsert.
12. Confirm no existing publication rows were replaced incorrectly.
13. Approve selected-city upsert.
14. Rebuild August HTML with zero Drik calls.
15. Visually inspect the result.
```

The selected master preview added the new-location festival rows without
replacing the previously approved cities, demonstrating the intended
`SELECTED_PLACES_UPSERT` behavior.

This is the recommended template whenever a new city is added to an already
published historical month.

---

# Final Operational Rule

When uncertain, identify which layer is missing:

```text
Missing normal daily Panchanga?
    -> panchanga_daily_scanner.py backfill

Missing/incorrect monthly festival discovery?
    -> month_festival_discovery.py

Missing/incorrect festival calculation/details?
    -> relevant festival engine

Correct engine messages but not yet in production?
    -> special_events_master_manager.py preview/approve

Masters are correct but HTML is stale?
    -> panchanga_daily_scanner.py rebuild

Dated HTML is correct but navigation/index is stale?
    -> panchanga_daily_scanner.py index
```

This separation is intentional. It prevents an HTML problem from triggering
unnecessary Drik scans and prevents a new-city backfill from disturbing
already-approved production festival data.
