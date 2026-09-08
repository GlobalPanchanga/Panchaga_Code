from pathlib import Path
import importlib.util
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parent
DISC = ROOT / 'festival_discovery' / 'month_festival_discovery.py'
REG = ROOT / 'festival_registry.csv'
STD = ROOT / 'festival_engines' / 'standard_festival_engine.py'
SAN = ROOT / 'festival_engines' / 'sankramana_engine.py'

spec = importlib.util.spec_from_file_location('month_festival_discovery_check', DISC)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

keys = [
    'SANKRAMANA',
    'VARAHA_JAYANTHI',
    'GANESHA_CHATURTHI',
    'RISHI_PANCHAMI',
    'VAMANA_JAYANTHI',
    'ANANTA_CHATURDASHI',
    'PITRUPAKSHA_BEGINS',
]

print('Discovery file:', DISC)
print('Schema:', m.DISCOVERY_SCHEMA_VERSION)
print('Ekadashi normalization:', m.normalize_ekadashi_cycle_name('Gauna Aja Ekadashi'))
print('\nDate strategies / scripts:')
for k in keys:
    print(f'  {k:24s} strategy={m.get_date_strategy(k):26s} script={m.ENGINE_SCRIPT_PATHS.get(k)}')

reg = pd.read_csv(REG, dtype=str, keep_default_na=False, encoding='utf-8-sig')
print('\nRegistry execution flags:')
for k in keys:
    rows = reg[reg['Engine'].str.upper().eq(k)]
    vals = sorted(set(rows['Execution Enabled'].str.strip()))
    print(f'  {k:24s} {vals}')

errors = []
if m.normalize_ekadashi_cycle_name('Gauna Aja Ekadashi') != 'Aja Ekadashi':
    errors.append('Ekadashi normalization is missing')
for k in keys:
    if m.get_date_strategy(k) != 'CITY_SPECIFIC_EXACT_DATE':
        errors.append(f'{k}: wrong date strategy')
    if k not in m.ENGINE_SCRIPT_PATHS:
        errors.append(f'{k}: missing engine script mapping')
    rows = reg[reg['Engine'].str.upper().eq(k)]
    if rows.empty or not (rows['Execution Enabled'].str.strip().str.lower() == 'yes').all():
        errors.append(f'{k}: registry Execution Enabled is not Yes')
for p in [DISC, REG, STD, SAN]:
    if not p.exists():
        errors.append(f'Missing file: {p}')

if errors:
    print('\nFAILED:')
    for e in errors:
        print(' -', e)
    raise SystemExit(1)
print('\nPASS: September discovery/engine configuration is internally consistent.')
