"""Independent closed-form checks for the fixed synthetic candidate menu.

These checks do not validate S-OIL operations or real commercial availability.
"""
from itertools import product
from pathlib import Path
import importlib.util
import json
import math

ROOT = Path(__file__).resolve().parents[2]
module_path = ROOT / 'cowork/2026-10-14-supply-decision/verify_design.py'
spec = importlib.util.spec_from_file_location('cowork_design', module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
inputs = module.load_spec()
count = 0
for d, f, b in product(range(4), range(7), range(9)):
    actual = module.compare(inputs, {'delay_days': d, 'additional_cost': f, 'b_premium': b})
    # Independent analytic conditions, only under the declared defaults:
    # floor = one day's consumption, early volume q >= 80*d; all arrivals
    # in horizon, equal material, no hold, 600 kbbl capacity, no split fee.
    feasible_costs = {'B': 500 * (72 + b)}
    if d == 0:
        feasible_costs['A'] = 500 * (72 + f)
    for q in (80, 160):
        if q >= 80 * d:
            feasible_costs[f'C{q}'] = q * 85 + (500 - q) * (72 + f)
    expected = {n for n, cost in feasible_costs.items() if cost == min(feasible_costs.values())}
    assert set(actual['winners']) == expected, (d, f, b)
    assert actual['best_cost_kusd'] == min(feasible_costs.values())
    count += 1
threshold_checks = []
for d, q, threshold in [(1, 80, 2.08), (2, 160, 4.16)]:
    row = module.compare(inputs, {'delay_days': d, 'b_premium': threshold})
    assert set(row['winners']) == {'B', f'C{q}'}
    below = module.compare(inputs, {'delay_days': d, 'b_premium': threshold - 0.01})
    above = module.compare(inputs, {'delay_days': d, 'b_premium': threshold + 0.01})
    assert below['winners'] == ['B'] and above['winners'] == [f'C{q}']
    threshold_checks.append({'delay_days': d, 'early_kbbl': q, 'b_premium_tie_usd_per_bbl': threshold})
print(json.dumps({'status': 'PASS', 'data_status': 'SYNTHETIC_NOT_COMPANY_DATA',
                  'independent_formula_grid_points': count, 'tie_boundaries': threshold_checks,
                  'scope': 'fixed seven-day equal-material menu; not a real market or refinery model'}, indent=2))
