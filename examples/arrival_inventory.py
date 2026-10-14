"""Synthetic plan comparison. Not S-OIL data, an LP, or an execution system.

Quantity: kbbl (thousand barrels); unit price: USD/bbl; cost: kUSD.
Receipts are available at the start of each day, before planned consumption.
Negative planned balances diagnose infeasibility; they are NOT physical stocks.
"""
from dataclasses import dataclass, replace
import json
import math
import sys
import unittest


@dataclass(frozen=True)
class Settings:
    days: int = 7
    opening: float = 240
    daily_use: float = 80
    floor: float = 80
    capacity: float = 600
    purchase: float = 500
    terminal: float = 180


@dataclass(frozen=True)
class Parcel:
    day: int
    quantity: float
    quote: float
    freight: float


def evaluate(name: str, parcels: tuple[Parcel, ...], s: Settings = Settings()) -> dict:
    """Check a fixed plan. Do not reduce use, invent arrivals, or relax constraints."""
    if type(s.days) is not int or s.days < 1:
        raise ValueError('days must be a positive integer')
    values = (s.opening, s.daily_use, s.floor, s.capacity, s.purchase, s.terminal)
    if any(not math.isfinite(x) or x < 0 for x in values):
        raise ValueError('settings must be finite and nonnegative')
    if not s.floor <= s.opening <= s.capacity or not s.floor <= s.terminal <= s.capacity:
        raise ValueError('opening and terminal stocks must be within bounds')
    expected = s.opening + s.purchase - s.days * s.daily_use
    if not math.isclose(expected, s.terminal, abs_tol=1e-8):
        raise ValueError('settings do not close the material balance')
    for p in parcels:
        if type(p.day) is not int or not 1 <= p.day <= s.days:
            raise ValueError('receipt day outside the planning horizon')
        if any(not math.isfinite(x) or x < 0 for x in (p.quantity, p.quote, p.freight)):
            raise ValueError('parcel values must be finite and nonnegative')
    if not math.isclose(sum(p.quantity for p in parcels), s.purchase, abs_tol=1e-8):
        raise ValueError('compare the same purchase quantity and terminal inventory')
    balance = s.opening
    rows, violations = [], []
    for day in range(1, s.days + 1):
        receipt = sum(p.quantity for p in parcels if p.day == day)
        before_use = balance + receipt
        closing = before_use - s.daily_use
        if before_use > s.capacity + 1e-8:
            violations.append({'day': day, 'kind': 'receipt_capacity'})
        if closing < s.floor - 1e-8:
            violations.append({'day': day, 'kind': 'closing_floor'})
        rows.append({'day': day, 'opening': balance, 'receipt': receipt,
                     'before_use': before_use, 'use': s.daily_use,
                     'planned_closing': closing})
        balance = closing
    return {'plan': name, 'data_status': 'SYNTHETIC_NOT_COMPANY_DATA',
            'feasible': not violations, 'violations': violations,
            'cost_kusd': sum(p.quantity * (p.quote + p.freight) for p in parcels),
            'minimum_planned_closing_kbbl': min(r['planned_closing'] for r in rows),
            'terminal_kbbl': balance, 'trajectory': rows}


def cases() -> dict:
    # All prices, dates and quantities below are invented for arithmetic testing.
    return {
        'base': {
            'A': (Parcel(3, 500, 70, 2),),
            'B': (Parcel(3, 500, 72, 3),),
            'C': (Parcel(3, 160, 82, 3), Parcel(5, 340, 70, 2)),
        },
        'arrival_delay': {
            'A': (Parcel(5, 500, 70, 2),),
            'B': (Parcel(3, 500, 72, 3),),
            'C': (Parcel(3, 160, 82, 3), Parcel(5, 340, 70, 2)),
        },
        'freight_increase': {
            'A': (Parcel(3, 500, 70, 6),),
            'B': (Parcel(3, 500, 72, 3),),
            'C': (Parcel(3, 160, 82, 3), Parcel(5, 340, 70, 6)),
        },
    }


def select(results: list[dict]) -> str | None:
    feasible = [r for r in results if r['feasible']]
    return min(feasible, key=lambda r: (r['cost_kusd'], r['plan']))['plan'] if feasible else None


def run() -> dict:
    output = {'status': 'SYNTHETIC_NOT_COMPANY_DATA', 'scenarios': {}}
    for scenario, plans in cases().items():
        results = [evaluate(name, parcels) for name, parcels in plans.items()]
        output['scenarios'][scenario] = {'lowest_cost_feasible_plan': select(results),
                                          'plans': results}
    return output


class Checks(unittest.TestCase):
    def test_base(self):
        r = run()['scenarios']['base']
        self.assertEqual(r['lowest_cost_feasible_plan'], 'A')
        self.assertEqual([x['cost_kusd'] for x in r['plans']], [36000, 37500, 38080])

    def test_delay(self):
        r = run()['scenarios']['arrival_delay']
        self.assertEqual(r['lowest_cost_feasible_plan'], 'B')
        self.assertEqual(r['plans'][0]['violations'][0], {'day': 3, 'kind': 'closing_floor'})

    def test_freight(self):
        self.assertEqual(run()['scenarios']['freight_increase']['lowest_cost_feasible_plan'], 'B')

    def test_material_balance(self):
        for case in run()['scenarios'].values():
            for plan in case['plans']:
                self.assertEqual(plan['terminal_kbbl'], 180)
                for row in plan['trajectory']:
                    self.assertEqual(row['opening'] + row['receipt'] - row['use'], row['planned_closing'])

    def test_bridge(self):
        r = evaluate('C', cases()['arrival_delay']['C'])
        self.assertTrue(r['feasible'])
        self.assertEqual(r['minimum_planned_closing_kbbl'], 80)

    def test_intra_day_capacity(self):
        r = evaluate('A', cases()['base']['A'], replace(Settings(), capacity=550))
        self.assertFalse(r['feasible'])
        self.assertIn({'day': 3, 'kind': 'receipt_capacity'}, r['violations'])

    def test_no_feasible_plan(self):
        r = evaluate('A', cases()['arrival_delay']['A'])
        self.assertIsNone(select([r]))

    def test_late_receipt(self):
        with self.assertRaises(ValueError):
            evaluate('bad', (Parcel(8, 500, 70, 2),))

    def test_unequal_quantity(self):
        with self.assertRaises(ValueError):
            evaluate('bad', (Parcel(3, 600, 70, 2),))

    def test_invalid_inputs(self):
        for p in (Parcel(3, -500, 70, 2), Parcel(3, 500, float('nan'), 2)):
            with self.assertRaises(ValueError):
                evaluate('bad', (p,))
        with self.assertRaises(ValueError):
            evaluate('bad', cases()['base']['A'], replace(Settings(), terminal=100))


if __name__ == '__main__':
    if sys.argv[1:] == ['--test']:
        unittest.main(argv=[sys.argv[0]], verbosity=2)
    elif not sys.argv[1:]:
        print(json.dumps(run(), ensure_ascii=False, indent=2, allow_nan=False))
    else:
        raise SystemExit('Usage: python examples/arrival_inventory.py [--test]')
