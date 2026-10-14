"""Check the Cowork design using the existing readiness balance evaluator.

All prices and operating inputs are SYNTHETIC_NOT_COMPANY_DATA.
This checks a declared candidate menu, not real market availability or optimality.
"""
from copy import deepcopy
from itertools import product
import json
import math
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'examples'))
from readiness_case import evaluate  # noqa: E402


def load_spec():
    spec = json.loads((HERE / 'inputs/scenarios.json').read_text(encoding='utf-8'))
    if spec['data_status'] != 'SYNTHETIC_NOT_COMPANY_DATA':
        raise ValueError('synthetic status must be preserved')
    return spec


def compare(spec, overrides=None):
    cfg = {**spec['defaults'], **(overrides or {})}
    settings = deepcopy(spec['settings'])
    settings['capacity'] = cfg.get('capacity', settings['capacity'])
    case = {'settings': settings, 'operations_status': spec['data_status']}
    for key in ('delay_days', 'quality_hold_days', 'early_limit', 'b_partial_limit'):
        if type(cfg[key]) is not int or cfg[key] < 0:
            raise ValueError(f'invalid {key}')
    for key in ('early_lot', 'b_partial_lot'):
        if type(cfg[key]) is not int or cfg[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    for key in ('additional_cost', 'b_premium', 'base_price', 'early_premium', 'split_fee_kusd'):
        if not math.isfinite(cfg[key]) or cfg[key] < 0:
            raise ValueError(f'invalid {key}')
    if type(cfg['b_available']) is not bool or type(cfg['b_partial']) is not bool:
        raise ValueError('availability flags must be booleans')
    if cfg['low_compatible'] is not None and type(cfg['low_compatible']) is not bool:
        raise ValueError('compatibility must be true, false or unknown')
    low_day = 3 + cfg['delay_days']
    if low_day > settings['days']:
        raise ValueError('low arrival outside comparison horizon')

    def parcel(quantity, day, premium, low=False):
        return {'quantity': quantity, 'arrival': day, 'customs': day,
                'quality': day + (cfg['quality_hold_days'] if low else 0),
                'differential': premium, 'freight': 0, 'insurance': 0,
                'compatible': cfg['low_compatible'] if low else True}

    quantity = settings['purchase']
    menu = {'A': [parcel(quantity, low_day, cfg['additional_cost'], low=True)]}
    unavailable = []
    if cfg['b_available']:
        menu['B'] = [parcel(quantity, 3, cfg['b_premium'])]
    else:
        unavailable.append('B')
    for q in range(cfg['early_lot'], min(cfg['early_limit'], quantity - 1) + 1, cfg['early_lot']):
        tail = parcel(quantity - q, low_day, cfg['additional_cost'], low=True)
        menu[f'C{q}'] = [parcel(q, 3, cfg['early_premium']), tail]
    if cfg['b_available'] and cfg['b_partial']:
        for q in range(cfg['b_partial_lot'], min(cfg['b_partial_limit'], quantity - 1) + 1, cfg['b_partial_lot']):
            tail = parcel(quantity - q, low_day, cfg['additional_cost'], low=True)
            menu[f'D{q}'] = [parcel(q, 3, cfg['b_premium']), deepcopy(tail)]

    results = {}
    for name, parcels in menu.items():
        r = evaluate(parcels, case, cfg['base_price'])
        if name.startswith(('C', 'D')):
            r['cost_kusd'] += cfg['split_fee_kusd']
        r['purchase_kbbl'] = sum(p['quantity'] for p in parcels)
        r['peak_planned_stored_kbbl'] = max(x['planned_stored_before_use_kbbl'] for x in r['ledger'])
        r['minimum_usable_margin_kbbl'] = r['min_planned_usable_kbbl'] - settings['floor']
        r['storage_margin_kbbl'] = settings['capacity'] - r['peak_planned_stored_kbbl']
        results[name] = r
    feasible = {name: r['cost_kusd'] for name, r in results.items()
                if r['status'] == 'FEASIBLE_UNDER_ASSUMPTIONS'}
    best = min(feasible.values()) if feasible else None
    winners = [name for name, cost in feasible.items()
               if math.isclose(cost, best, rel_tol=0, abs_tol=1e-6)]
    return {'data_status': spec['data_status'], 'conditions': cfg,
            'unavailable': unavailable, 'winners': winners,
            'best_cost_kusd': best, 'plans': results}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def check(spec):
    for scenario in spec['scenarios']:
        result = compare(spec, scenario['overrides'])
        require(result['winners'] == scenario['expected_winners'], scenario['id'] + ': winners')
        require(result['best_cost_kusd'] == scenario['expected_best_cost_kusd'], scenario['id'] + ': cost')
    grid = spec['primary_grid']
    grid_count = 0
    for d, f, b in product(grid['delay_days'], grid['additional_cost'], grid['b_premium']):
        conditions = {'delay_days': d, 'additional_cost': f, 'b_premium': b}
        before = compare(spec, conditions)
        after = compare(spec, {**conditions, 'base_price': spec['defaults']['base_price'] + 20})
        require(before['winners'] == after['winners'], 'common price must cancel')
        for name, r in before['plans'].items():
            require(r['purchase_kbbl'] == 500, 'equal purchases')
            require(r['ledger'][-1]['planned_stored_closing_kbbl'] == 180, 'terminal stored')
            require(r['ledger'][-1]['planned_usable_closing_kbbl'] == 180, 'terminal usable')
            require(after['plans'][name]['cost_kusd'] - r['cost_kusd'] == 10000, 'common price cost delta')
            previous_stored = previous_usable = 240
            for row in r['ledger']:
                require(row['planned_stored_before_use_kbbl'] == previous_stored + row['arrivals_kbbl'], 'receipt balance')
                require(row['planned_stored_closing_kbbl'] == previous_stored + row['arrivals_kbbl'] - 80, 'stored balance')
                require(row['planned_usable_closing_kbbl'] == previous_usable + row['released_kbbl'] - 80, 'usable balance')
                previous_stored = row['planned_stored_closing_kbbl']
                previous_usable = row['planned_usable_closing_kbbl']
            if name in before['winners']:
                require(r['minimum_usable_margin_kbbl'] >= 0 and r['storage_margin_kbbl'] >= 0, 'winner constraints')
        grid_count += 1

    held = compare(spec, {'quality_hold_days': 2})['plans']['A']['ledger'][2]
    require((held['planned_stored_closing_kbbl'], held['planned_usable_closing_kbbl'], held['blocked_kbbl']) == (500, 0, 500), 'quality hold balance')
    held_capacity = compare(spec, {'quality_hold_days': 2, 'capacity': 550})
    require({'day': 3, 'type': 'receipt_capacity'} in held_capacity['plans']['A']['violations'], 'held material uses capacity')
    require(held_capacity['winners'] == [], 'no capacity workaround')
    unknown = compare(spec, {'low_compatible': None})
    require(unknown['plans']['A']['status'] == 'NEEDS_REVIEW', 'unknown compatibility')
    require(compare(spec, {'low_compatible': False})['plans']['A']['status'] == 'INCOMPATIBLE', 'incompatible material')
    require(compare(spec, {'delay_days': 2, 'b_partial': True})['winners'] == ['D160'], 'partial B counterexample')
    require(compare(spec, {'delay_days': 2, 'b_partial': True})['best_cost_kusd'] == 36480, 'partial B cost')
    independent_b = compare(spec, {'delay_days': 1, 'early_limit': 0, 'b_partial': True})
    require(independent_b['winners'] == ['D80'] and independent_b['best_cost_kusd'] == 36240, 'B partial supply is independent of E limit')
    require(compare(spec, {'delay_days': 1, 'split_fee_kusd': 500})['winners'] == ['B'], 'split fee changes choice')
    require(compare(spec, {'delay_days': 1, 'early_limit': 0, 'b_available': False})['winners'] == [], 'no early supply')
    require(compare(spec, {'delay_days': 1, 'additional_cost': 3})['winners'] == ['B'], 'infeasible A cannot tie')
    return {'status': 'PASS', 'data_status': spec['data_status'],
            'representative_scenarios': len(spec['scenarios']), 'primary_grid_points': grid_count,
            'secondary_full_grid': 'NOT_EXECUTED',
            'checks': ['equal quantities and terminal stocks', 'daily material balances',
                       'feasible winner constraints', 'common price cancellation',
                       'quality hold and storage', 'unknown/incompatible material',
                       'partial B and split cost counterexamples', 'independent E and B limits',
                       'no feasible plan', 'feasible ties']}


if __name__ == '__main__':
    spec = load_spec()
    if sys.argv[1:] == ['--check']:
        output = check(spec)
    elif not sys.argv[1:]:
        output = {'data_status': spec['data_status'], 'scenarios': {
            s['id']: compare(spec, s['overrides']) for s in spec['scenarios']}}
    else:
        raise SystemExit('Usage: python verify_design.py [--check]')
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
