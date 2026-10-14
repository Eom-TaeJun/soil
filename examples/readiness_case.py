"""Public historical reference + synthetic operations; not an S-OIL model.

Quantity kbbl, price USD/bbl, cost kUSD. Day = synthetic planning day.
Signed planned balances diagnose infeasibility, not physical negative stocks.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_inputs():
    facts = json.loads((ROOT / 'data/public_context.json').read_text(encoding='utf-8'))
    case = json.loads((ROOT / 'data/synthetic_readiness.json').read_text(encoding='utf-8'))
    return facts, case


def reference_price(facts, case):
    rows = [r for r in facts['metrics'] if r['id'] == case['benchmark_id']]
    if len(rows) != 1:
        raise ValueError('reference must identify exactly one public metric')
    row = rows[0]
    if row['kind'] != 'HISTORICAL_OBSERVATION' or row['unit'] != 'USD/bbl':
        raise ValueError('use a historical USD/bbl reference, not a forecast or wrong unit')
    if row['publication_date'] > case['as_of']:
        raise ValueError('reference was not published by the as-of date')
    if row['period_end'] > case['as_of']:
        raise ValueError('reference observation is after the as-of date')
    source_ids = {s['id'] for s in facts['sources']}
    if row['source_id'] not in source_ids:
        raise ValueError('missing public provenance')
    value = row['value']
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid reference price')
    return float(value)


def evaluate(parcels, case, benchmark):
    s = case['settings']
    if case['operations_status'] != 'SYNTHETIC_NOT_COMPANY_DATA':
        raise ValueError('synthetic operations must remain labelled')
    if type(s['days']) is not int or s['days'] <= 0:
        raise ValueError('invalid horizon')
    for key in ('opening', 'use', 'floor', 'capacity', 'purchase', 'terminal'):
        if not math.isfinite(s[key]) or s[key] < 0:
            raise ValueError('invalid settings')
    if not (s['floor'] <= s['opening'] <= s['capacity'] and
            s['floor'] <= s['terminal'] <= s['capacity']):
        raise ValueError('opening and terminal inventories must be within bounds')
    if not math.isclose(s['opening'] + s['purchase'] - s['days'] * s['use'], s['terminal']):
        raise ValueError('settings do not close the material balance')
    for p in parcels:
        for key in ('arrival', 'customs', 'quality'):
            if type(p[key]) is not int or p[key] < 1:
                raise ValueError('invalid day')
        if p['arrival'] > s['days']:
            raise ValueError('receipt outside comparison horizon')
        for key in ('quantity', 'freight', 'insurance'):
            if not math.isfinite(p[key]) or p[key] < 0:
                raise ValueError('invalid parcel')
        if not math.isfinite(p['differential']):
            raise ValueError('invalid differential')
        if p['compatible'] not in (True, False, None):
            raise ValueError('compatibility is true, false or unknown')
        if benchmark + p['differential'] + p['freight'] + p['insurance'] < 0:
            raise ValueError('negative delivered price')
    if not math.isclose(sum(p['quantity'] for p in parcels), s['purchase']):
        raise ValueError('compare equal purchase quantities')
    usable = s['opening']
    stored = s['opening']
    violations, ledger = [], []
    for day in range(1, s['days'] + 1):
        arrivals = sum(p['quantity'] for p in parcels if p['arrival'] == day)
        released = sum(p['quantity'] for p in parcels
                       if max(p['arrival'], p['customs'], p['quality']) == day)
        stored_before_use = stored + arrivals
        usable_before_use = usable + released
        stored = stored_before_use - s['use']
        usable = usable_before_use - s['use']
        if stored_before_use > s['capacity'] + 1e-8:
            violations.append({'day': day, 'type': 'receipt_capacity'})
        if usable < s['floor'] - 1e-8:
            violations.append({'day': day, 'type': 'usable_floor'})
        ledger.append({'day': day, 'arrivals_kbbl': arrivals, 'released_kbbl': released,
                       'planned_stored_before_use_kbbl': stored_before_use,
                       'planned_stored_closing_kbbl': stored,
                       'planned_usable_closing_kbbl': usable,
                       'blocked_kbbl': stored - usable})
    if not math.isclose(usable, s['terminal']):
        violations.append({'day': s['days'], 'type': 'terminal_usable'})
    if not math.isclose(stored, s['terminal']):
        violations.append({'day': s['days'], 'type': 'terminal_stored'})
    compatibility = [p['compatible'] for p in parcels]
    if False in compatibility:
        status = 'INCOMPATIBLE'
    elif violations:
        status = 'INFEASIBLE'
    elif None in compatibility:
        status = 'NEEDS_REVIEW'
    else:
        status = 'FEASIBLE_UNDER_ASSUMPTIONS'
    cost = sum(p['quantity'] * (benchmark + p['differential'] + p['freight'] + p['insurance'])
               for p in parcels)
    return {'status': status, 'cost_kusd': round(cost, 6), 'violations': violations,
            'min_planned_usable_kbbl': min(r['planned_usable_closing_kbbl'] for r in ledger),
            'ledger': ledger}


def run(facts=None, case=None):
    if facts is None or case is None:
        facts, case = load_inputs()
    benchmark = reference_price(facts, case)
    output = {'as_of': case['as_of'], 'status': 'PUBLIC_REFERENCE_SYNTHETIC_OPERATIONS',
              'benchmark_id': case['benchmark_id'], 'benchmark_usd_per_bbl': benchmark,
              'benchmark_role': 'HISTORICAL_COMMON_REFERENCE_NOT_COMPANY_PURCHASE_PRICE',
              'scenarios': {}}
    for scenario in case['scenarios']:
        plans = deepcopy(case['plans'])
        for patch in scenario['patches']:
            plans[patch['plan']][patch['parcel']].update(patch['set'])
        results = {name: evaluate(parcels, case, benchmark) for name, parcels in plans.items()}
        eligible = {name: r['cost_kusd'] for name, r in results.items()
                    if r['status'] == 'FEASIBLE_UNDER_ASSUMPTIONS'}
        best = min(eligible.values()) if eligible else None
        winners = [name for name, cost in eligible.items() if math.isclose(cost, best, abs_tol=1e-6)]
        output['scenarios'][scenario['id']] = {'lowest_cost_feasible': winners, 'plans': results}
    return output


class Checks(unittest.TestCase):
    def setUp(self):
        self.facts, self.case = load_inputs()

    def test_baseline(self):
        r = run(self.facts, self.case)['scenarios']['base']
        self.assertEqual(r['lowest_cost_feasible'], ['A'])
        self.assertEqual(r['plans']['A']['cost_kusd'], 57750)

    def test_delay_changes_choice(self):
        self.assertEqual(run()['scenarios']['arrival_delay']['lowest_cost_feasible'], ['B'])

    def test_quality_hold_is_not_available_inventory(self):
        r = run()['scenarios']['quality_hold']['plans']['A']
        self.assertEqual(r['status'], 'INFEASIBLE')
        self.assertEqual(r['ledger'][2]['blocked_kbbl'], 500)
        self.assertEqual(r['ledger'][2]['planned_stored_closing_kbbl'], 500)
        self.assertEqual(r['ledger'][2]['planned_usable_closing_kbbl'], 0)

    def test_unknown_compatibility_requires_review(self):
        r = run()['scenarios']['unknown_compatibility']['plans']['A']
        self.assertEqual(r['status'], 'NEEDS_REVIEW')

    def test_freight_changes_choice(self):
        self.assertEqual(run()['scenarios']['freight_stress']['lowest_cost_feasible'], ['B'])

    def test_bridge_when_other_choices_fail(self):
        self.assertEqual(run()['scenarios']['bridge_only']['lowest_cost_feasible'], ['C'])

    def test_no_choice_is_reported(self):
        self.assertEqual(run()['scenarios']['no_feasible']['lowest_cost_feasible'], [])

    def test_common_benchmark_does_not_change_ranking(self):
        before = run(self.facts, self.case)
        self.facts['metrics'][0]['value'] += 20
        after = run(self.facts, self.case)
        for name, r in before['scenarios'].items():
            self.assertEqual(r['lowest_cost_feasible'], after['scenarios'][name]['lowest_cost_feasible'])
            self.assertAlmostEqual(after['scenarios'][name]['plans']['A']['cost_kusd'] -
                                   r['plans']['A']['cost_kusd'], 10000)

    def test_forecast_cannot_replace_observation(self):
        self.case['benchmark_id'] = 'M02'
        with self.assertRaises(ValueError):
            run(self.facts, self.case)

    def test_capacity_includes_blocked_material(self):
        self.case['settings']['capacity'] = 550
        r = run(self.facts, self.case)['scenarios']['quality_hold']['plans']['A']
        self.assertIn({'day': 3, 'type': 'receipt_capacity'}, r['violations'])

    def test_tie_is_preserved(self):
        self.case['plans']['A'][0]['freight'] = 7
        self.assertEqual(run(self.facts, self.case)['scenarios']['base']['lowest_cost_feasible'], ['A', 'B'])

    def test_invalid_quantity_and_missing_source(self):
        self.case['plans']['A'][0]['quantity'] = 499
        with self.assertRaises(ValueError):
            run(self.facts, self.case)
        self.facts, self.case = load_inputs()
        self.facts['metrics'][0]['source_id'] = 'MISSING'
        with self.assertRaises(ValueError):
            run(self.facts, self.case)

    def test_future_publication_is_rejected(self):
        self.facts['metrics'][0]['publication_date'] = '2026-10-15'
        with self.assertRaises(ValueError):
            run(self.facts, self.case)

    def test_material_balance_and_terminal(self):
        for scenario in run()['scenarios'].values():
            for r in scenario['plans'].values():
                self.assertEqual(r['ledger'][-1]['planned_stored_closing_kbbl'], 180)
                for row in r['ledger']:
                    self.assertEqual(row['planned_stored_closing_kbbl'] -
                                     row['planned_usable_closing_kbbl'], row['blocked_kbbl'])


if __name__ == '__main__':
    if sys.argv[1:] == ['--test']:
        unittest.main(argv=[sys.argv[0]], verbosity=2)
    elif not sys.argv[1:]:
        print(json.dumps(run(), ensure_ascii=False, indent=2, allow_nan=False))
    else:
        raise SystemExit('Usage: python examples/readiness_case.py [--test]')
