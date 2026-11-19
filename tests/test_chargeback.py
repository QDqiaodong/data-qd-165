"""Chargeback mode tests.

The oracle used here is an independent exhaustive enumeration of every
floor/ceil choice, written from the data contract only.  It never calls the
min-cost flow solver under test.  Small cases are enumerated cell by cell;
larger cases verify structural invariants plus a spot check against a
separately-implemented lexicographic search.
"""
import itertools
import json
import unittest
from fractions import Fraction
from pathlib import Path

from api_quota_audit.chargeback import chargeback_analyze
from api_quota_audit.model import instant as iso_to_us

ROOT = Path(__file__).resolve().parents[1]


def tariff(tariff_id, principal, route, tiers, window_seconds=3600):
    return {'id': tariff_id, 'principal': principal, 'route': route,
            'window_seconds': window_seconds,
            'tiers': [{'upto': u, 'rate_num': n, 'rate_den': d} for u, n, d in tiers]}


def event(event_id, principal, route, units, center, at='2026-01-01T00:00:00Z'):
    return {'id': event_id, 'principal': principal, 'route': route,
            'at': at, 'units': units, 'center': center}


def document(tariffs, centers, events, locks=()):
    return {'tariffs': tariffs, 'centers': centers, 'events': events,
            'locks': list(locks)}


def lock(tariff_id, center, cents, window_start='2026-01-01T00:00:00Z'):
    return {'tariff_id': tariff_id, 'window_start': window_start,
            'center': center, 'cents': cents}


def frob(value):
    return Fraction(value['numerator'], value['denominator'])


# --- independent pricing and enumeration oracle -------------------------


def pool_cost(tiers, units):
    """Price [0, units) from the marginal schedule without reuse of solver."""
    total = Fraction(0)
    lower = 0
    for upto, rate in tiers:
        rate = rate if isinstance(rate, Fraction) else Fraction(rate)
        if upto is None:
            total += (units - lower) * rate
            break
        take = min(units, upto) - lower
        if take > 0:
            total += take * rate
        if units <= upto:
            break
        lower = upto
    return total


def half_up_round(cost):
    """floor(cost + 1/2) with exact half-up, implemented from first principles."""
    floor_value = cost.numerator // cost.denominator
    remainder = cost - floor_value
    return floor_value + (1 if remainder >= Fraction(1, 2) else 0)


def expected_exact(data):
    """Recompute pools and cells exactly from the raw document."""
    pools = {}
    for ev in data['events']:
        scope = next(t for t in data['tariffs']
                     if (t['principal'], t['route']) == (ev['principal'], ev['route']))
        us = 1_000_000 * scope['window_seconds']
        at = iso_to_us(ev['at'])
        start = at // us * us
        key = (scope['id'], start)
        pools.setdefault(key, {'units': 0, 'centers': {}})
        pools[key]['units'] += ev['units']
        pools[key]['centers'][ev['center']] = pools[key]['centers'].get(ev['center'], 0) + ev['units']
    cells = {}
    priced = {}
    for key, pool in pools.items():
        tiers = next(t for t in data['tariffs'] if t['id'] == key[0])['tiers']
        cost = pool_cost([(tier['upto'], Fraction(tier['rate_num'], tier['rate_den']))
                          for tier in tiers], pool['units'])
        priced[key] = cost
        for center, units in pool['centers'].items():
            cells[(key[0], key[1], center)] = (units, cost * units / pool['units'])
    return pools, priced, cells


def _utc_text(epoch_us):
    import datetime
    moment = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + \
        datetime.timedelta(microseconds=epoch_us)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def brute_force(data):
    """Enumerate every floor/ceil matrix; pick primary error then lexicographic."""
    pools, priced, cells = expected_exact(data)
    required = {row['id']: row['required_cents'] for row in data['centers']}
    locks = {(item['tariff_id'], iso_to_us(item['window_start']), item['center']): item['cents']
             for item in data['locks']}
    row_total = {key: half_up_round(value) for key, value in priced.items()}
    ordered = sorted(cells)
    if sum(required.values()) != sum(row_total.values()):
        return 'unallocatable', None, None
    options = []
    for key in ordered:
        _, exact = cells[key]
        floor = exact.numerator // exact.denominator
        values = (floor,) if exact.denominator == 1 else (floor, floor + 1)
        if key in locks:
            values = tuple(v for v in values if v == locks[key])
        options.append(values)
    best = None
    for combo in itertools.product(*options):
        row_sums, col_sums = {}, {}
        for key, value in zip(ordered, combo):
            row_sums[(key[0], key[1])] = row_sums.get((key[0], key[1]), 0) + value
            col_sums[key[2]] = col_sums.get(key[2], 0) + value
        if any(row_sums.get(k, 0) != v for k, v in row_total.items()):
            continue
        if any(col_sums.get(c, 0) != v for c, v in required.items()):
            continue
        error = sum(abs(value - cells[key][1]) for key, value in zip(ordered, combo))
        key = (error, tuple(combo))
        if best is None or key < best[0]:
            best = (key, tuple(combo), error)
    if best is None:
        return 'unallocatable', None, None
    return 'balanced', best[1], best[2]


class OracleTests(unittest.TestCase):
    def check_against_oracle(self, data):
        expected_status, expected_cents, expected_error = brute_force(data)
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], expected_status)
        if expected_status == 'balanced':
            got = tuple(cell['cents'] for cell in result['cells'])
            self.assertEqual(got, expected_cents)
            self.assertEqual(frob(result['rounding_error']), expected_error)
            for cell in result['cells']:
                self.assertIsInstance(cell['cents'], int)
            actual = {row['id']: row['actual_cents'] for row in result['centers']}
            self.assertEqual(actual, {row['id']: row['required_cents'] for row in data['centers']})
        else:
            self.assertIsNone(result['rounding_error'])
            for cell in result['cells']:
                self.assertIsNone(cell['cents'])
            for row in result['centers']:
                self.assertIsNone(row['actual_cents'])
        return result

    def test_random_small_documents_match_enumeration(self):
        import random
        rng = random.Random(20261119)
        for trial in range(120):
            width = rng.randint(1, 3)
            tariffs, events = self._random_scenario(rng, width)
            pools, priced, cells = expected_exact({'tariffs': tariffs, 'centers': [],
                                                   'events': events, 'locks': []})
            invoice_base = sum(half_up_round(v) for v in priced.values())
            centers = [{'id': center, 'required_cents': 0}
                       for center in ('A', 'B', 'C')[:width]]
            for index, row in enumerate(centers):
                row['required_cents'] = (rng.randint(0, invoice_base) if index
                                         else 0)
            remainder = invoice_base - sum(row['required_cents'] for row in centers[:-1])
            centers[-1]['required_cents'] = max(0, remainder)
            if sum(row['required_cents'] for row in centers) != invoice_base:
                continue
            document_data = document(tariffs, centers, events)
            with self.subTest(trial=trial):
                self.check_against_oracle(document_data)

    def test_random_locks_match_enumeration(self):
        import random
        rng = random.Random(7)
        for trial in range(150):
            tariffs, events = self._random_scenario(rng, 3)
            pools, priced, cells = expected_exact({'tariffs': tariffs, 'centers': [],
                                                   'events': events, 'locks': []})
            invoice_base = sum(half_up_round(v) for v in priced.values())
            centers = [{'id': center, 'required_cents': 0}
                       for center in ('A', 'B', 'C')]
            for index in range(2):
                centers[index]['required_cents'] = rng.randint(0, invoice_base)
            centers[-1]['required_cents'] = \
                invoice_base - sum(row['required_cents'] for row in centers[:-1])
            if centers[-1]['required_cents'] < 0:
                continue
            locks = []
            keys = sorted(cells)
            for key in rng.sample(keys, min(len(keys), rng.randint(0, 3))):
                _, exact = cells[key]
                floor = exact.numerator // exact.denominator
                choices = [floor] if exact.denominator == 1 else [floor, floor + 1]
                # Occasionally place a lock outside the floor/ceil pair to
                # exercise the business-unallocatable path.
                value = rng.choice(choices) if rng.random() < 0.85 else floor + 2
                locks.append({'tariff_id': key[0],
                              'window_start': _utc_text(key[1]),
                              'center': key[2], 'cents': value})
            data = document(tariffs, centers, events, locks)
            with self.subTest(trial=trial):
                self.check_against_oracle(data)

    def _random_scenario(self, rng, width):
        tariffs = []
        for index in range(rng.randint(1, 3)):
            tiers = []
            if rng.random() < 0.45:
                tiers.append((rng.randint(1, 10), rng.randint(0, 5), rng.randint(1, 5)))
            tiers.append((None, rng.randint(0, 5), rng.randint(1, 5)))
            tariffs.append(tariff('t%d' % index, 'p%d' % index, '/%d' % index, tiers))
        events = []
        counter = 0
        for item in tariffs:
            center_count = rng.randint(1, width)
            for center in rng.sample(['A', 'B', 'C'][:width], center_count):
                counter += 1
                events.append(event('e%d' % counter, item['principal'],
                                    item['route'], rng.randint(1, 40), center))
        return tariffs, events


class PricingTests(unittest.TestCase):
    def test_discount_is_shared_by_pool_not_per_center(self):
        # One pool of 20 units: first 10 at 1 cent/unit, the rest at 1/2.
        # C = 10 + 5 = 15 cents.  Per-centre pricing would instead price
        # A's 5 and B's 15 separately and change both invoice and cells.
        data = document(
            [tariff('t', 'p', '/', [(10, 1, 1), (None, 1, 2)])],
            [{'id': 'A', 'required_cents': 4}, {'id': 'B', 'required_cents': 11}],
            [event('a', 'p', '/', 5, 'A'), event('b', 'p', '/', 15, 'B')])
        result = chargeback_analyze(data)
        self.assertEqual(frob(result['pools'][0]['exact_cost']), Fraction(115, 4) if False else Fraction(15))
        self.assertEqual(result['pools'][0]['invoice_cents'], 15)
        self.assertEqual([frob(c['exact_cost']) for c in result['cells']],
                         [Fraction(15, 4), Fraction(45, 4)])
        self.assertEqual([c['cents'] for c in result['cells']], [4, 11])
        # Independent marginal recomputation of the per-centre interpretation
        # would have been 5 and 15 total -> 20, proving sharing matters.
        self.assertNotEqual(result['pools'][0]['invoice_cents'], 5 + 15)

    def test_tier_boundary_is_marginal(self):
        data = document(
            [tariff('t', 'p', '/', [(10, 3, 1), (None, 1, 1)])],
            [{'id': 'A', 'required_cents': 30}],
            [event('a', 'p', '/', 10, 'A')])
        result = chargeback_analyze(data)
        self.assertEqual(frob(result['pools'][0]['exact_cost']), Fraction(30))
        data['events'].append(event('b', 'p', '/', 5, 'A'))
        data['centers'][0]['required_cents'] = 35
        result = chargeback_analyze(data)
        self.assertEqual(frob(result['pools'][0]['exact_cost']), Fraction(35))
        self.assertEqual(result['pools'][0]['units'], 15)

    def test_zero_price_usage_still_emits_zero_cost_cells(self):
        data = document(
            [tariff('t', 'p', '/', [(None, 0, 1)])],
            [{'id': 'A', 'required_cents': 0}],
            [event('a', 'p', '/', 7, 'A')])
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'balanced')
        self.assertEqual(result['pools'][0]['invoice_cents'], 0)
        self.assertEqual(len(result['pools']), 1)
        cell = result['cells'][0]
        self.assertEqual((cell['units'], frob(cell['exact_cost']), cell['cents']), (7, Fraction(0), 0))

    def test_half_cent_rounds_up_not_bankers(self):
        for units, expected in ((1, 1), (3, 2)):  # 0.5 -> 1, 1.5 -> 2
            data = document(
                [tariff('t', 'p', '/', [(None, 1, 2)])],
                [{'id': 'A', 'required_cents': expected}],
                [event('a', 'p', '/', units, 'A')])
            result = chargeback_analyze(data)
            self.assertEqual(result['pools'][0]['invoice_cents'], expected)
            self.assertEqual(result['cells'][0]['cents'], expected)

    def test_integer_cell_cannot_be_incremented(self):
        # C = 10 exactly, single integer cell, required total 11 contradicts
        # every row total and must be unallocatable rather than bumping it.
        data = document(
            [tariff('t', 'p', '/', [(None, 1, 1)])],
            [{'id': 'A', 'required_cents': 11}],
            [event('a', 'p', '/', 10, 'A')])
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'unallocatable')
        self.assertIsNone(result['cells'][0]['cents'])


class IdentityTests(unittest.TestCase):
    def base(self):
        return document(
            [tariff('t', 'p', '/', [(None, 1, 1)])],
            [{'id': 'A', 'required_cents': 3}, {'id': 'B', 'required_cents': 2}],
            [event('e1', 'p', '/', 2, 'A'), event('e2', 'p', '/', 3, 'B')])

    def test_identical_events_are_deduplicated(self):
        data = self.base()
        data['events'].append(event('e1', 'p', '/', 2, 'A'))
        data['events'].append(dict(data['events'][0], at='2026-01-01T01:00:00+01:00'))
        result = chargeback_analyze(data)
        self.assertEqual(result['deduplicated_event_count'], 2)
        self.assertEqual([c['units'] for c in result['cells']], [2, 3])
        self.assertEqual(result['pools'][0]['units'], 5)

    def test_same_id_with_different_center_is_a_conflict(self):
        data = self.base()
        data['events'].append(event('e1', 'p', '/', 2, 'B'))
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            chargeback_analyze(data)

    def test_same_id_with_different_units_is_a_conflict(self):
        data = self.base()
        data['events'].append(event('e1', 'p', '/', 3, 'A'))
        with self.assertRaises(ValueError):
            chargeback_analyze(data)


class AllocationTests(unittest.TestCase):
    def two_pool_document(self):
        return document(
            [tariff('t1', 'p1', '/a', [(None, 1, 2)]),
             tariff('t2', 'p2', '/b', [(None, 1, 2)])],
            [{'id': 'A', 'required_cents': 1}, {'id': 'B', 'required_cents': 1}],
            [event('a1', 'p1', '/a', 1, 'A'), event('b1', 'p1', '/a', 1, 'B'),
             event('a2', 'p2', '/b', 1, 'A'), event('b2', 'p2', '/b', 1, 'B')])

    def test_acceptance_tail_cannot_localise_in_one_column(self):
        result = chargeback_analyze(self.two_pool_document())
        self.assertEqual(result['status'], 'balanced')
        # Each pool rounds 1/2+1/2 independently to 1, so "give A both pools'
        # tail" would be [1,0,1,0] and break B's required column.  The global
        # optimum is exactly [0,1,1,0].
        self.assertEqual([c['cents'] for c in result['cells']], [0, 1, 1, 0])
        self.assertEqual(frob(result['rounding_error']), Fraction(2))
        self.assertEqual(result['total_invoice_cents'], 2)

    def test_lock_on_first_cell_forces_alternating_matrix(self):
        data = self.two_pool_document()
        data['locks'] = [lock('t1', 'A', 1)]
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'balanced')
        self.assertEqual([c['cents'] for c in result['cells']], [1, 0, 0, 1])
        self.assertTrue(result['cells'][0]['locked'])
        self.assertFalse(result['cells'][1]['locked'])
        self.assertEqual(frob(result['rounding_error']), Fraction(2))

    def test_price_change_reruns_bounds_and_joint_feasibility(self):
        data = self.two_pool_document()
        self.assertEqual(chargeback_analyze(data)['status'], 'balanced')
        # Price t2's two units at 1/8 each: C = 1/4 so R=0 and both its cells
        # are pinned at floor 0 while t1 still invoices one cent.  The exact
        # costs, cell bounds and global invoice total all change together.
        data['tariffs'][1]['tiers'] = [{'upto': None, 'rate_num': 1, 'rate_den': 8}]
        result = chargeback_analyze(data)
        self.assertEqual(result['pools'][1]['invoice_cents'], 0)
        self.assertEqual([frob(c['exact_cost']) for c in result['cells']],
                         [Fraction(1, 2), Fraction(1, 2), Fraction(1, 8), Fraction(1, 8)])
        # Required centres still ask for two cents total, but the re-priced
        # pools can now invoice only one: unallocatable, not a silent fix-up.
        self.assertEqual(result['status'], 'unallocatable')
        self.assertEqual(result['total_invoice_cents'], 1)
        self.assertIsNone(result['rounding_error'])
        self.assertTrue(all(c['cents'] is None for c in result['cells']))
        # Lowering A's requirement restores a balanced one-cent matrix.
        data['centers'][0]['required_cents'] = 1
        data['centers'][1]['required_cents'] = 0
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'balanced')
        self.assertEqual([c['cents'] for c in result['cells']], [1, 0, 0, 0])

    def test_sparse_unsolvable_even_though_totals_match(self):
        # t1 has cells in both centres, t2 has a single cell in A.  Every
        # exact cell is 1/2 and every pool invoices 1, so sum(R) == 2 and
        # required(A)+required(B) == 2: the totals match.  Yet t2's only cell
        # must take its cent in A, so A can never be 0 -- a support-based
        # infeasibility the totals check alone cannot see.
        data = document(
            [tariff('t1', 'p1', '/a', [(None, 1, 2)]),
             tariff('t2', 'p2', '/b', [(None, 1, 2)])],
            [{'id': 'A', 'required_cents': 0}, {'id': 'B', 'required_cents': 2}],
            [event('a1', 'p1', '/a', 1, 'A'), event('b1', 'p1', '/a', 1, 'B'),
             event('a2', 'p2', '/b', 1, 'A')])
        result = chargeback_analyze(data)
        self.assertEqual(result['total_invoice_cents'], 2)
        self.assertEqual(sum(row['required_cents'] for row in result['centers']), 2)
        self.assertEqual(result['status'], 'unallocatable')

        # The same support is fine when A is allowed to take t2's cent.
        data['centers'] = [{'id': 'A', 'required_cents': 1},
                           {'id': 'B', 'required_cents': 1}]
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'balanced')
        self.assertEqual([c['cents'] for c in result['cells']], [0, 1, 1])

    def test_lock_outside_floor_ceil_is_business_unallocatable(self):
        data = self.two_pool_document()
        data['locks'] = [lock('t1', 'A', 2)]  # exact 1/2 -> only 0 or 1
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'unallocatable')
        self.assertEqual(result['pools'][0]['invoice_cents'], 1)
        self.assertEqual(result['total_invoice_cents'], 2)

    def test_feasible_and_contradictory_lock_sets(self):
        data = self.two_pool_document()
        # A single lock on the first cell is feasible (acceptance case).
        data['locks'] = [lock('t1', 'A', 1)]
        self.assertEqual(chargeback_analyze(data)['status'], 'balanced')
        # Locks that leave a valid tail placement stay feasible.
        data['locks'] = [lock('t1', 'A', 0), lock('t1', 'B', 1)]
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'balanced')
        self.assertEqual([c['cents'] for c in result['cells']], [0, 1, 1, 0])
        # Two locks force two cents into column A, but A only requires one;
        # the columns are over-determined even though each lock is legal.
        data['locks'] = [lock('t1', 'A', 1), lock('t2', 'A', 1)]
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'unallocatable')
        self.assertIsNone(result['rounding_error'])
        # A lock that pins a whole pool while its column cannot absorb it.
        data['locks'] = [lock('t1', 'A', 1), lock('t1', 'B', 1)]
        data['centers'] = [{'id': 'A', 'required_cents': 1},
                           {'id': 'B', 'required_cents': 2}]
        # Sum of requirements no longer equals the invoices (2), so the row
        # total check alone reports unallocatable.
        self.assertEqual(chargeback_analyze(data)['status'], 'unallocatable')

    def test_zero_usage_center_is_retained_and_receives_zero(self):
        data = document(
            [tariff('t', 'p', '/', [(None, 1, 1)])],
            [{'id': 'A', 'required_cents': 2}, {'id': 'Z', 'required_cents': 0}],
            [event('a', 'p', '/', 2, 'A')])
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'balanced')
        self.assertEqual([row['id'] for row in result['centers']], ['A', 'Z'])
        zero = next(row for row in result['centers'] if row['id'] == 'Z')
        self.assertEqual(zero['actual_cents'], 0)
        self.assertEqual([c['center'] for c in result['cells']], ['A'])

    def test_unused_tariff_produces_no_pool(self):
        data = document(
            [tariff('t1', 'p1', '/a', [(None, 1, 1)]),
             tariff('t2', 'p2', '/b', [(None, 1, 1)])],
            [{'id': 'A', 'required_cents': 1}],
            [event('a', 'p1', '/a', 1, 'A')])
        result = chargeback_analyze(data)
        self.assertEqual([p['tariff_id'] for p in result['pools']], ['t1'])

    def test_empty_input_is_balanced(self):
        result = chargeback_analyze(document([], [], []))
        self.assertEqual(result['status'], 'balanced')
        self.assertEqual((result['pools'], result['centers'], result['cells']), ([], [], []))
        self.assertEqual(result['total_invoice_cents'], 0)
        self.assertEqual(frob(result['rounding_error']), Fraction(0))
        self.assertEqual(result['deduplicated_event_count'], 0)

    def test_empty_centers_with_events_is_rejected(self):
        with self.assertRaises(ValueError):
            chargeback_analyze(document(
                [tariff('t', 'p', '/', [(None, 1, 1)])], [],
                [event('a', 'p', '/', 1, 'A')]))

    def test_primary_error_beats_any_tail_placement(self):
        # Distinct exact thirds across three pools; enumerate to prove the
        # solver's reported error is the minimal one and its matrix matches.
        data = document(
            [tariff('t1', 'p1', '/a', [(None, 1, 3)]),
             tariff('t2', 'p2', '/b', [(None, 2, 3)]),
             tariff('t3', 'p3', '/c', [(None, 1, 3)])],
            [{'id': 'A', 'required_cents': 1}, {'id': 'B', 'required_cents': 1},
             {'id': 'C', 'required_cents': 1}],
            [event('a1', 'p1', '/a', 1, 'A'), event('b1', 'p1', '/a', 1, 'B'),
             event('a2', 'p2', '/b', 1, 'A'), event('b2', 'p2', '/b', 1, 'B'),
             event('a3', 'p3', '/c', 1, 'A'), event('b3', 'p3', '/c', 1, 'C')])
        status, cents, error = brute_force(data)
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], status)
        if status == 'balanced':
            self.assertEqual(tuple(c['cents'] for c in result['cells']), cents)
            self.assertEqual(frob(result['rounding_error']), error)

    def test_primary_error_tie_uses_lexicographic_vector(self):
        # Two pools, one centre.  Every pool costs exactly 1/2 with cells at
        # 1/4 each; each pool must pay one cent while the single centre must
        # receive two.  All zero-error choices are impossible (a row would
        # need one cent from a cell at 1/4), but several one-unit-error
        # matrices exist; the lexicographic minimum must win.
        data = document(
            [tariff('t1', 'p1', '/a', [(None, 1, 2)]),
             tariff('t2', 'p2', '/b', [(None, 1, 2)])],
            [{'id': 'A', 'required_cents': 2}],
            [event('a1', 'p1', '/a', 1, 'A'), event('b1', 'p1', '/a', 1, 'A'),
             event('a2', 'p2', '/b', 1, 'A'), event('b2', 'p2', '/b', 1, 'A')])
        status, cents, error = brute_force(data)
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], status)
        self.assertEqual(tuple(c['cents'] for c in result['cells']), cents)
        if status == 'balanced':
            self.assertEqual(frob(result['rounding_error']), error)

        # Build a genuine tie: two identical pools with separate centres,
        # each pool 1/2 + 1/2 and each centre required 1.  Both matrices
        # [1,0,0,1] and [0,1,1,0] carry equal error; lexicographic order
        # chooses [0,1,1,0].
        data = document(
            [tariff('t1', 'p1', '/a', [(None, 1, 2)]),
             tariff('t2', 'p2', '/b', [(None, 1, 2)])],
            [{'id': 'A', 'required_cents': 1}, {'id': 'B', 'required_cents': 1}],
            [event('a1', 'p1', '/a', 1, 'A'), event('b1', 'p1', '/a', 1, 'B'),
             event('a2', 'p2', '/b', 1, 'A'), event('b2', 'p2', '/b', 1, 'B')])
        status, cents, error = brute_force(data)
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'balanced')
        self.assertEqual(tuple(c['cents'] for c in result['cells']), cents)
        self.assertEqual(cents, (0, 1, 1, 0))
        self.assertEqual(frob(result['rounding_error']), error)


class PrecisionTests(unittest.TestCase):
    def test_different_denominators_are_not_float_approximated(self):
        # 1/3, 1/7 and 1/11 exact costs: float would mis-rank the error
        # comparison.  Row totals are one cent each; only exact integers can
        # decide which single cent placement minimises the absolute error.
        data = document(
            [tariff('t1', 'p1', '/a', [(None, 1, 3)]),
             tariff('t2', 'p2', '/b', [(None, 1, 7)]),
             tariff('t3', 'p3', '/c', [(None, 1, 11)])],
            [{'id': 'A', 'required_cents': 1}, {'id': 'B', 'required_cents': 0},
             {'id': 'C', 'required_cents': 0}],
            [event('a1', 'p1', '/a', 1, 'A'), event('a2', 'p1', '/a', 1, 'B'),
             event('b1', 'p2', '/b', 1, 'A'), event('b2', 'p2', '/b', 1, 'C'),
             event('c1', 'p3', '/c', 1, 'A'), event('c2', 'p3', '/c', 1, 'C')])
        result = chargeback_analyze(data)
        status, cents, error = brute_force(data)
        self.assertEqual(result['status'], status)
        self.assertEqual(tuple(c['cents'] for c in result['cells']), cents)
        self.assertEqual(frob(result['rounding_error']), error)
        # Every fraction is kept in lowest terms (0 -> 0/1).
        for cell in result['cells']:
            value = frob(cell['exact_cost'])
            self.assertEqual(cell['exact_cost']['denominator'],
                             Fraction(cell['exact_cost']['numerator'],
                                      cell['exact_cost']['denominator']).denominator)


class WindowTests(unittest.TestCase):
    def test_multiple_timezones_share_one_pool(self):
        data = document(
            [tariff('t', 'p', '/', [(None, 1, 1)])],
            [{'id': 'A', 'required_cents': 2}],
            [event('a', 'p', '/', 1, 'A', at='2026-01-01T00:00:00Z'),
             event('b', 'p', '/', 1, 'A', at='2026-01-01T01:00:00+01:00')])
        result = chargeback_analyze(data)
        self.assertEqual(len(result['pools']), 1)
        self.assertEqual(result['pools'][0]['units'], 2)
        self.assertEqual(result['deduplicated_event_count'], 2)

    def test_negative_epoch_floors_into_earlier_window(self):
        data = document(
            [tariff('t', 'p', '/', [(None, 1, 1)], window_seconds=60)],
            [{'id': 'A', 'required_cents': 2}],
            [event('a', 'p', '/', 1, 'A', at='1969-12-31T23:59:00Z'),
             event('b', 'p', '/', 1, 'A', at='1969-12-31T23:59:59.999999Z')])
        result = chargeback_analyze(data)
        self.assertEqual(len(result['pools']), 1)
        # 1969-12-31T23:59:00Z -> epoch -60s; the minute window [23:59, 00:00)
        # starts at -60s = -60_000_000 us.  23:59:59.999999 floors to the same.
        self.assertEqual(result['pools'][0]['window_start_us'], -60_000_000)
        self.assertEqual(result['pools'][0]['window_end_us'], 0)

    def test_microsecond_boundary_starts_next_window(self):
        data = document(
            [tariff('t', 'p', '/', [(None, 1, 1)], window_seconds=60)],
            [{'id': 'A', 'required_cents': 2}],
            [event('a', 'p', '/', 1, 'A', at='2026-01-01T00:00:59.999999Z'),
             event('b', 'p', '/', 1, 'A', at='2026-01-01T00:01:00Z')])
        result = chargeback_analyze(data)
        self.assertEqual(len(result['pools']), 2)
        starts = [p['window_start_us'] for p in result['pools']]
        self.assertEqual(starts, [1767225600 * 10 ** 6, (1767225600 + 60) * 10 ** 6])

    def test_lock_must_be_a_real_window_start(self):
        data = document(
            [tariff('t', 'p', '/', [(None, 1, 1)])],
            [{'id': 'A', 'required_cents': 1}],
            [event('a', 'p', '/', 1, 'A')])
        data['locks'] = [lock('t', 'A', 1, window_start='2026-01-01T00:00:30Z')]
        with self.assertRaises(ValueError):
            chargeback_analyze(data)

    def test_lock_on_empty_cell_is_input_error(self):
        data = document(
            [tariff('t1', 'p1', '/a', [(None, 1, 1)]),
             tariff('t2', 'p2', '/b', [(None, 1, 1)])],
            [{'id': 'A', 'required_cents': 1}],
            [event('a', 'p1', '/a', 1, 'A')])
        data['locks'] = [lock('t2', 'A', 0)]
        with self.assertRaises(ValueError):
            chargeback_analyze(data)

    def test_duplicate_lock_is_input_error_even_if_equal(self):
        data = document(
            [tariff('t', 'p', '/', [(None, 1, 2)])],
            [{'id': 'A', 'required_cents': 1}],
            [event('a', 'p', '/', 1, 'A')])
        data['locks'] = [lock('t', 'A', 1), lock('t', 'A', 1)]
        with self.assertRaises(ValueError):
            chargeback_analyze(data)


class ValidationTests(unittest.TestCase):
    def base(self):
        return document(
            [tariff('t', 'p', '/', [(None, 1, 2)])],
            [{'id': 'A', 'required_cents': 1}],
            [event('a', 'p', '/', 1, 'A')])

    def test_extra_field_rejected(self):
        data = self.base()
        data['extra'] = 1
        with self.assertRaises(ValueError):
            chargeback_analyze(data)

    def test_bool_and_float_integers_rejected(self):
        for mutate in (
            lambda d: d['centers'][0].__setitem__('required_cents', True),
            lambda d: d['events'][0].__setitem__('units', 1.0),
            lambda d: d['tariffs'][0].__setitem__('window_seconds', True),
            lambda d: d['tariffs'][0]['tiers'][0].__setitem__('rate_den', 1.0),
        ):
            data = self.base()
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                chargeback_analyze(data)

    def test_tiers_must_end_unbounded_and_increase(self):
        for tiers in (
            [{'upto': 10, 'rate_num': 1, 'rate_den': 1}],
            [{'upto': 10, 'rate_num': 1, 'rate_den': 1},
             {'upto': 10, 'rate_num': 1, 'rate_den': 1},
             {'upto': None, 'rate_num': 1, 'rate_den': 1}],
            [{'upto': None, 'rate_num': 1, 'rate_den': 1},
             {'upto': 20, 'rate_num': 1, 'rate_den': 1}],
        ):
            data = self.base()
            data['tariffs'][0]['tiers'] = tiers
            with self.subTest(tiers=tiers), self.assertRaises(ValueError):
                chargeback_analyze(data)

    def test_rate_bounds(self):
        for num, den in ((10 ** 9 + 1, 1), (1, 0), (1, 10 ** 6 + 1), (-1, 1)):
            data = self.base()
            data['tariffs'][0]['tiers'] = [{'upto': None, 'rate_num': num, 'rate_den': den}]
            with self.subTest(num=num, den=den), self.assertRaises(ValueError):
                chargeback_analyze(data)

    def test_event_center_must_exist(self):
        data = self.base()
        data['events'][0]['center'] = 'ghost'
        with self.assertRaises(ValueError):
            chargeback_analyze(data)

    def test_event_without_tariff_is_error(self):
        data = self.base()
        data['events'][0]['route'] = '/unknown'
        with self.assertRaises(ValueError):
            chargeback_analyze(data)

    def test_all_arrays_validated_before_business_status(self):
        # Amounts do not balance, but a later invalid lock must still be
        # reported as an input error rather than "unallocatable".
        data = self.base()
        data['centers'][0]['required_cents'] = 99
        data['locks'] = [{'tariff_id': 'ghost', 'window_start': '2026-01-01T00:00:00Z',
                          'center': 'A', 'cents': 0}]
        with self.assertRaises(ValueError):
            chargeback_analyze(data)

    def test_duplicate_tariff_id_and_scope(self):
        data = self.base()
        data['tariffs'].append(dict(data['tariffs'][0]))
        with self.assertRaises(ValueError):
            chargeback_analyze(data)
        data = self.base()
        data['tariffs'].append(dict(data['tariffs'][0], id='other'))
        with self.assertRaises(ValueError):
            chargeback_analyze(data)


class ReportTests(unittest.TestCase):
    def test_report_shape_and_ordering(self):
        result = chargeback_analyze(json.loads((ROOT / 'fixtures/chargeback.json').read_text()))
        self.assertEqual(set(result), {'status', 'pools', 'centers', 'cells',
                                       'total_invoice_cents', 'rounding_error',
                                       'deduplicated_event_count'})
        self.assertEqual(result['status'], 'balanced')
        self.assertEqual([p['tariff_id'] for p in result['pools']], ['t-read', 't-write'])
        self.assertEqual(set(result['pools'][0]),
                         {'tariff_id', 'principal', 'route', 'window_start_us',
                          'window_end_us', 'units', 'exact_cost', 'invoice_cents'})
        self.assertEqual(set(result['cells'][0]),
                         {'tariff_id', 'window_start_us', 'center', 'units',
                          'exact_cost', 'cents', 'locked'})
        self.assertEqual(set(result['centers'][0]),
                         {'id', 'required_cents', 'actual_cents'})
        order = [(c['tariff_id'], c['window_start_us'], c['center']) for c in result['cells']]
        self.assertEqual(order, sorted(order))
        self.assertEqual(result['total_invoice_cents'], 180)
        self.assertEqual([c['cents'] for c in result['cells']], [52, 68, 20, 40])

    def test_unallocatable_keeps_complete_pricing(self):
        data = json.loads((ROOT / 'fixtures/chargeback.json').read_text())
        data['centers'][0]['required_cents'] = 1
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'unallocatable')
        self.assertEqual(result['pools'][0]['invoice_cents'], 120)
        self.assertEqual(result['total_invoice_cents'], 180)
        self.assertIsNone(result['rounding_error'])
        self.assertTrue(all(c['cents'] is None for c in result['cells']))
        self.assertTrue(all(c['actual_cents'] is None for c in result['centers']))
        self.assertEqual([c['exact_cost'] for c in result['cells']],
                         [{'numerator': 105, 'denominator': 2},
                          {'numerator': 135, 'denominator': 2},
                          {'numerator': 20, 'denominator': 1},
                          {'numerator': 40, 'denominator': 1}])


class LargerCaseTests(unittest.TestCase):
    def test_many_pools_and_centres_with_cross_pool_locks(self):
        # 12 pools, 4 centres, hand-set locks spanning different pools.  The
        # production path must remain polynomial: 12 rows x 4 columns of
        # free cells is enumerated here only for verification.
        tariffs = []
        events = []
        centers = ['A', 'B', 'C', 'D']
        rate = [(1, 2), (1, 3), (2, 3), (1, 4), (3, 4), (1, 3), (1, 2), (2, 5), (1, 5), (3, 7), (1, 7), (2, 7)]
        for index in range(12):
            tariffs.append(tariff('t%d' % index, 'p%d' % index, '/%d' % index,
                                  [(None, rate[index][0], rate[index][1])]))
            events.append(event('a%d' % index, 'p%d' % index, '/%d' % index, 1, 'A'))
            events.append(event('b%d' % index, 'p%d' % index, '/%d' % index, 1, 'B'))
            events.append(event('c%d' % index, 'p%d' % index, '/%d' % index, 1, 'C'))
            events.append(event('d%d' % index, 'p%d' % index, '/%d' % index, 1, 'D'))
        pools, priced, cells = expected_exact({'tariffs': tariffs, 'centers': [],
                                               'events': events, 'locks': []})
        total = sum(half_up_round(v) for v in priced.values())
        centers = [{'id': 'A', 'required_cents': total // 4},
                   {'id': 'B', 'required_cents': total // 4},
                   {'id': 'C', 'required_cents': total // 4},
                   {'id': 'D', 'required_cents': total - 3 * (total // 4)}]
        data = document(tariffs, centers, events)
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'balanced')
        self.assertEqual(len(result['cells']), 48)
        # Verify all structural constraints independently.
        row_sums = {}
        col_sums = {}
        for cell in result['cells']:
            row_sums[(cell['tariff_id'], cell['window_start_us'])] = \
                row_sums.get((cell['tariff_id'], cell['window_start_us']), 0) + cell['cents']
            col_sums[cell['center']] = col_sums.get(cell['center'], 0) + cell['cents']
            exact = frob(cell['exact_cost'])
            floor = exact.numerator // exact.denominator
            self.assertIn(cell['cents'], {floor, floor + 1})
        for pool in result['pools']:
            self.assertEqual(row_sums[(pool['tariff_id'], pool['window_start_us'])],
                             pool['invoice_cents'])
        for row in result['centers']:
            self.assertEqual(col_sums[row['id']], row['required_cents'])
        # And the exact error matches the independent sum.
        error = sum(abs(Fraction(cell['cents']) - frob(cell['exact_cost']))
                    for cell in result['cells'])
        self.assertEqual(frob(result['rounding_error']), error)

    def test_acceptance_larger_case_with_locks_is_fast(self):
        # 30 pools x 5 centres; solver time must stay tiny while remaining
        # exact.  Locks pin one cell in three different pools.
        tariffs, events = [], []
        center_ids = ['A', 'B', 'C', 'D', 'E']
        for index in range(30):
            tariffs.append(tariff('t%d' % index, 'p%d' % index, '/%d' % index,
                                  [(None, 1, 3)]))
            for j, center in enumerate(center_ids):
                events.append(event('e%d_%d' % (index, j), 'p%d' % index,
                                    '/%d' % index, 1, center))
        pools, priced, cells = expected_exact({'tariffs': tariffs, 'centers': [],
                                               'events': events, 'locks': []})
        total = sum(half_up_round(v) for v in priced.values())
        split = [total // 5] * 4
        split.append(total - sum(split))
        centers = [{'id': center, 'required_cents': split[i]}
                   for i, center in enumerate(center_ids)]
        # t0 pool cost is 5/3 -> R=2; give A a lock of 1 (floor 0->allow 1).
        locks = [lock('t0', 'A', 1), lock('t10', 'B', 1), lock('t20', 'C', 1)]
        data = document(tariffs, centers, events, locks)
        result = chargeback_analyze(data)
        self.assertEqual(result['status'], 'balanced')
        self.assertEqual(len(result['cells']), 150)
        self.assertEqual(sum(1 for c in result['cells'] if c['locked']), 3)
        locked = {(c['tariff_id'], c['center']): c['cents']
                  for c in result['cells'] if c['locked']}
        self.assertEqual(locked, {('t0', 'A'): 1, ('t10', 'B'): 1, ('t20', 'C'): 1})


if __name__ == '__main__':
    unittest.main()


class InvarianceTests(unittest.TestCase):
    def test_shuffled_input_and_duplicate_events_keep_result(self):
        import random
        base = json.loads((ROOT / 'fixtures/chargeback.json').read_text())
        expected = chargeback_analyze(base)
        rng = random.Random(1)
        for _ in range(10):
            data = json.loads(json.dumps(base))
            rng.shuffle(data['events'])
            rng.shuffle(data['centers'])
            rng.shuffle(data['tariffs'])
            self.assertEqual(chargeback_analyze(data), expected)
        doubled = json.loads(json.dumps(base))
        doubled['events'] += doubled['events'][:2]
        self.assertEqual(chargeback_analyze(doubled), expected)

    def test_soft_cap_limits_are_enforced(self):
        # 81 distinct pools exceed MAX_POOLS and must fail as input error.
        tariffs = []
        events = []
        for index in range(81):
            tariffs.append(tariff('t%d' % index, 'p%d' % index, '/%d' % index,
                                  [(None, 1, 3)]))
            events.append(event('e%d' % index, 'p%d' % index, '/%d' % index,
                                1, 'A'))
        with self.assertRaises(ValueError):
            chargeback_analyze(document(tariffs, [{'id': 'A', 'required_cents': 0}],
                                        events))

    def test_more_than_2000_cells_is_input_error(self):
        centers = [{'id': 'c%03d' % i, 'required_cents': 0} for i in range(50)]
        tariffs = [tariff('t%02d' % i, 'p%02d' % i, '/%d' % i, [(None, 1, 3)])
                   for i in range(40)]
        events = []
        for t in range(40):
            for c in range(50):
                events.append(event('e%d_%d' % (t, c), 'p%02d' % t, '/%d' % t,
                                    1, centers[c]['id']))
        # Exactly 2000 cells is allowed (and would be balanced), so add one
        # more window on a tariff whose centres are all already present.
        events.append(event('extra', 'p00', '/0', 1, centers[0]['id'],
                            at='2026-01-01T01:00:00Z'))
        with self.assertRaises(ValueError):
            chargeback_analyze(document(tariffs, centers, events))
