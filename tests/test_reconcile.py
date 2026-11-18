"""Offline interval reconciliation: brute-force cross-checks and certificates."""
import itertools
import json
import unittest

from api_quota_audit.reconcile import reconcile_analyze


def doc(timeline_count=4, max_slot=10, meters=(), invoices=(), queries=()):
    timeline = ['2026-01-%02dT%02d:00:00Z' % (1 + i // 24, i % 24) for i in range(timeline_count)]
    return {
        'timeline': timeline,
        'max_slot_units': max_slot,
        'meters': list(meters),
        'invoices': list(invoices),
        'queries': list(queries),
    }


def allocations(document):
    n = len(document['timeline']) - 1
    return itertools.product(range(document['max_slot_units'] + 1), repeat=n)


def sum_of(values, start, end):
    return sum(values[start:end])


def meter_ok(document, values):
    return all(m['lower'] <= sum_of(values, m['start'], m['end']) <= m['upper']
               for m in document['meters'])


def invoice_ok(document, values):
    return all(sum_of(values, i['start'], i['end']) == i['units']
               for i in document['invoices'])


def brute_status_and_ranges(document):
    """Independent exhaustive reference for small inputs (never the solver)."""
    meter_feasible = [v for v in allocations(document) if meter_ok(document, v)]
    if not meter_feasible:
        return 'meter_conflict', {}
    joint = [v for v in meter_feasible if invoice_ok(document, v)]
    if not joint:
        return 'invoice_conflict', {}
    ranges = {}
    for query in document['queries']:
        values = [sum_of(v, query['start'], query['end']) for v in joint]
        ranges[query['id']] = (min(values), max(values))
    return 'feasible', ranges


def original_edges(document):
    """Map every original constraint edge to its weight, keyed by its record."""
    n = len(document['timeline']) - 1
    edges = {}
    for i in range(n):
        edges[('slot', str(i), 'upper', i, i + 1)] = document['max_slot_units']
        edges[('slot', str(i), 'lower', i + 1, i)] = 0
    for m in document['meters']:
        edges[('meter', m['id'], 'upper', m['start'], m['end'])] = m['upper']
        edges[('meter', m['id'], 'lower', m['end'], m['start'])] = -m['lower']
    for i in document['invoices']:
        edges[('invoice', i['id'], 'upper', i['start'], i['end'])] = i['units']
        edges[('invoice', i['id'], 'lower', i['end'], i['start'])] = -i['units']
    return edges


def check_certificate(testcase, document, result):
    """Verify a cycle witness edge by edge without trusting the solver."""
    n = len(document['timeline']) - 1
    original = original_edges(document)
    cycle = result['certificate']
    testcase.assertTrue(cycle)
    testcase.assertLessEqual(len(cycle), n + 1)
    for edge in cycle:
        testcase.assertEqual(set(edge), {'kind', 'ref', 'side', 'from', 'to', 'weight'})
        key = (edge['kind'], edge['ref'], edge['side'], edge['from'], edge['to'])
        testcase.assertIn(key, original)
        testcase.assertIs(type(edge['weight']), int)
        testcase.assertEqual(edge['weight'], original[key])
    for current, following in zip(cycle, cycle[1:] + cycle[:1]):
        testcase.assertEqual(current['to'], following['from'])
    testcase.assertLess(sum(edge['weight'] for edge in cycle), 0)
    testcase.assertEqual(cycle[-1]['to'], cycle[0]['from'])
    kinds = {edge['kind'] for edge in cycle}
    if result['status'] == 'meter_conflict':
        testcase.assertLessEqual(kinds, {'meter', 'slot'})
    else:
        testcase.assertIn('invoice', kinds)


def check_allocation(testcase, document, values, query, expected):
    testcase.assertEqual(len(values), len(document['timeline']) - 1)
    for value in values:
        testcase.assertIs(type(value), int)
        testcase.assertGreaterEqual(value, 0)
        testcase.assertLessEqual(value, document['max_slot_units'])
    testcase.assertTrue(meter_ok(document, values))
    testcase.assertTrue(invoice_ok(document, values))
    testcase.assertEqual(sum_of(values, query['start'], query['end']), expected)


class ReconcileContractTests(unittest.TestCase):
    def test_jointly_infeasible_overlapping_invoices(self):
        document = doc(
            4, 10,
            meters=[{'id': 'total', 'start': 0, 'end': 3, 'lower': 10, 'upper': 10}],
            invoices=[{'id': 'left', 'start': 0, 'end': 2, 'units': 2},
                      {'id': 'right', 'start': 1, 'end': 3, 'units': 2}],
            queries=[{'id': 'q', 'start': 0, 'end': 3}])
        result = reconcile_analyze(document)
        self.assertEqual(result['status'], 'invoice_conflict')
        self.assertEqual(result['ranges'], [])
        # Each invoice is individually satisfiable; only the batch is not.
        for invoice in document['invoices']:
            alone = dict(document, invoices=[invoice])
            self.assertEqual(brute_status_and_ranges(alone)[0], 'feasible')
        self.assertEqual(brute_status_and_ranges(document), ('invoice_conflict', {}))
        check_certificate(self, document, result)

    def test_meter_upper_change_flips_invoice_verdict(self):
        invoices = [{'id': 'i', 'start': 0, 'end': 2, 'units': 7}]
        compatible = doc(
            3, 10,
            meters=[{'id': 'm', 'start': 0, 'end': 2, 'lower': 0, 'upper': 10}],
            invoices=invoices,
            queries=[{'id': 'q', 'start': 0, 'end': 2}])
        result = reconcile_analyze(compatible)
        self.assertEqual(result['status'], 'feasible')
        self.assertEqual(result['ranges'][0]['min_units'], 7)
        self.assertEqual(result['ranges'][0]['max_units'], 7)

        tightened = doc(
            3, 10,
            meters=[{'id': 'm', 'start': 0, 'end': 2, 'lower': 0, 'upper': 6}],
            invoices=invoices)
        result = reconcile_analyze(tightened)
        self.assertEqual(result['status'], 'invoice_conflict')
        self.assertEqual(brute_status_and_ranges(compatible)[0], 'feasible')
        self.assertEqual(brute_status_and_ranges(tightened)[0], 'invoice_conflict')
        check_certificate(self, tightened, result)

    def test_meter_conflict_without_invoices(self):
        document = doc(
            3, 4,
            meters=[{'id': 'a', 'start': 0, 'end': 1, 'lower': 3, 'upper': 4},
                    {'id': 'b', 'start': 1, 'end': 2, 'lower': 3, 'upper': 4},
                    {'id': 'c', 'start': 0, 'end': 2, 'lower': 0, 'upper': 5}],
            invoices=[{'id': 'unused', 'start': 0, 'end': 2, 'units': 0}])
        result = reconcile_analyze(document)
        self.assertEqual(result['status'], 'meter_conflict')
        self.assertEqual(result['ranges'], [])
        self.assertEqual(brute_status_and_ranges(document)[0], 'meter_conflict')
        check_certificate(self, document, result)

    def test_exact_equalities_and_zero_usage(self):
        document = doc(
            5, 5,
            meters=[{'id': 'm', 'start': 0, 'end': 3, 'lower': 0, 'upper': 0}],
            invoices=[{'id': 'i', 'start': 1, 'end': 3, 'units': 0},
                      {'id': 'j', 'start': 3, 'end': 4, 'units': 2}],
            queries=[{'id': 'a', 'start': 0, 'end': 3},
                     {'id': 'b', 'start': 3, 'end': 4},
                     {'id': 'c', 'start': 0, 'end': 4}])
        result = reconcile_analyze(document)
        self.assertEqual(result['status'], 'feasible')
        self.assertEqual(brute_status_and_ranges(document),
                         ('feasible', {'a': (0, 0), 'b': (2, 2), 'c': (2, 2)}))
        for query in document['queries']:
            entry = next(x for x in result['ranges'] if x['id'] == query['id'])
            self.assertEqual(entry['min_units'], entry['max_units'])
            check_allocation(self, document, entry['min_allocation'], query, entry['min_units'])
            check_allocation(self, document, entry['max_allocation'], query, entry['max_units'])

    def test_no_extra_evidence_gives_finite_bounds(self):
        document = doc(3, 7, queries=[{'id': 'q', 'start': 0, 'end': 2}])
        result = reconcile_analyze(document)
        self.assertEqual(result['status'], 'feasible')
        self.assertEqual((result['ranges'][0]['min_units'], result['ranges'][0]['max_units']), (0, 14))
        self.assertEqual((result['ranges'][0]['min_allocation'], result['ranges'][0]['max_allocation']),
                         ([0, 0], [7, 7]))
        self.assertEqual(brute_status_and_ranges(document),
                         ('feasible', {'q': (0, 14)}))

    def test_overlapping_and_nested_intervals(self):
        document = doc(
            4, 6,
            meters=[{'id': 'outer', 'start': 0, 'end': 3, 'lower': 5, 'upper': 12},
                    {'id': 'inner', 'start': 1, 'end': 2, 'lower': 1, 'upper': 3}],
            invoices=[{'id': 'i', 'start': 0, 'end': 2, 'units': 4},
                      {'id': 'j', 'start': 1, 'end': 3, 'units': 4}],
            queries=[{'id': 'all', 'start': 0, 'end': 3},
                     {'id': 'head', 'start': 0, 'end': 1},
                     {'id': 'tail', 'start': 2, 'end': 3}])
        result = reconcile_analyze(document)
        self.assertEqual(result['status'], 'feasible')
        expected = brute_status_and_ranges(document)[1]
        self.assertEqual({x['id']: (x['min_units'], x['max_units']) for x in result['ranges']}, expected)
        for query in document['queries']:
            entry = next(x for x in result['ranges'] if x['id'] == query['id'])
            check_allocation(self, document, entry['min_allocation'], query, entry['min_units'])
            check_allocation(self, document, entry['max_allocation'], query, entry['max_units'])

    def test_offsets_and_microseconds_are_exact(self):
        document = {
            'timeline': ['2026-01-01T00:00:00Z', '2026-01-01T01:00:00+00:30',
                         '2026-01-01T00:45:00-00:15', '2026-01-01T01:00:00.000001Z'],
            'max_slot_units': 5,
            'meters': [{'id': 'm', 'start': 0, 'end': 3, 'lower': 1, 'upper': 5}],
            'invoices': [{'id': 'i', 'start': 2, 'end': 3, 'units': 1}],
            'queries': [{'id': 'mid', 'start': 1, 'end': 2}],
        }
        result = reconcile_analyze(document)
        self.assertEqual(result['status'], 'feasible')
        check_allocation(self, document, result['ranges'][0]['min_allocation'],
                         document['queries'][0], result['ranges'][0]['min_units'])

    def test_repeated_utc_instant_in_timeline_rejected(self):
        document = doc(3)
        document['timeline'][1] = '2026-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'strictly increasing'):
            reconcile_analyze(document)

    def test_unordered_source_arrays(self):
        document = doc(
            4, 5,
            meters=[{'id': 'z', 'start': 0, 'end': 3, 'lower': 2, 'upper': 4},
                    {'id': 'a', 'start': 1, 'end': 2, 'lower': 0, 'upper': 2}],
            invoices=[{'id': 'v', 'start': 0, 'end': 3, 'units': 3},
                      {'id': 'b', 'start': 1, 'end': 3, 'units': 1}],
            queries=[{'id': 'y', 'start': 0, 'end': 3},
                     {'id': 'x', 'start': 1, 'end': 2}])
        first = reconcile_analyze(document)
        shuffled = dict(document, meters=list(reversed(document['meters'])),
                        invoices=list(reversed(document['invoices'])),
                        queries=list(reversed(document['queries'])))
        second = reconcile_analyze(shuffled)
        self.assertEqual(first, second)
        self.assertEqual([x['id'] for x in first['ranges']], ['x', 'y'])

    def test_same_ref_across_sources_in_certificate(self):
        document = doc(
            4, 5,
            meters=[{'id': 'same', 'start': 0, 'end': 1, 'lower': 4, 'upper': 5},
                    {'id': 'same2', 'start': 2, 'end': 3, 'lower': 4, 'upper': 5}],
            invoices=[],
            queries=[])
        # Two hard meters plus a total upper bound of 5 cannot coexist.
        document['meters'].append({'id': 'cap', 'start': 0, 'end': 3, 'lower': 0, 'upper': 5})
        result = reconcile_analyze(document)
        self.assertEqual(result['status'], 'meter_conflict')
        check_certificate(self, document, result)

        conflicting = doc(
            4, 5,
            meters=[{'id': 'shared', 'start': 0, 'end': 3, 'lower': 0, 'upper': 9}],
            invoices=[{'id': 'shared', 'start': 0, 'end': 3, 'units': 10}])
        result = reconcile_analyze(conflicting)
        self.assertEqual(result['status'], 'invoice_conflict')
        self.assertIn(('invoice', 'shared'), {(e['kind'], e['ref']) for e in result['certificate']})
        check_certificate(self, conflicting, result)

    def test_meter_and_invoice_flexibility_keeps_feasible(self):
        document = doc(
            2, 1000000000000,
            meters=[{'id': 'm', 'start': 0, 'end': 1, 'lower': 0, 'upper': 1000000000000}],
            invoices=[{'id': 'i', 'start': 0, 'end': 1, 'units': 999999999999}],
            queries=[{'id': 'q', 'start': 0, 'end': 1}])
        result = reconcile_analyze(document)
        self.assertEqual(result['status'], 'feasible')
        self.assertEqual(result['ranges'][0]['min_units'], 999999999999)
        self.assertEqual(result['ranges'][0]['max_units'], 999999999999)

    def test_all_witnesses_are_independent_and_global(self):
        document = doc(
            4, 3,
            meters=[{'id': 'm', 'start': 0, 'end': 3, 'lower': 0, 'upper': 5},
                    {'id': 'tail', 'start': 1, 'end': 3, 'lower': 2, 'upper': 2}],
            invoices=[{'id': 'i', 'start': 0, 'end': 1, 'units': 1}],
            queries=[{'id': 'a', 'start': 0, 'end': 2},
                     {'id': 'b', 'start': 1, 'end': 3},
                     {'id': 'c', 'start': 0, 'end': 3}])
        result = reconcile_analyze(document)
        self.assertEqual(result['status'], 'feasible')
        expected = brute_status_and_ranges(document)[1]
        self.assertEqual({x['id']: (x['min_units'], x['max_units']) for x in result['ranges']}, expected)
        for query in document['queries']:
            entry = next(x for x in result['ranges'] if x['id'] == query['id'])
            check_allocation(self, document, entry['min_allocation'], query, entry['min_units'])
            check_allocation(self, document, entry['max_allocation'], query, entry['max_units'])


class ReconcileValidationTests(unittest.TestCase):
    def base(self):
        return doc(
            3, 5,
            meters=[{'id': 'm', 'start': 0, 'end': 2, 'lower': 0, 'upper': 5}],
            invoices=[{'id': 'i', 'start': 0, 'end': 1, 'units': 2}],
            queries=[{'id': 'q', 'start': 0, 'end': 2}])

    def test_top_level_fields_are_strict(self):
        document = self.base()
        document['extra'] = 1
        with self.assertRaises(ValueError):
            reconcile_analyze(document)
        missing = self.base()
        missing.pop('queries')
        with self.assertRaises(ValueError):
            reconcile_analyze(missing)

    def test_timeline_bounds_and_types(self):
        for content in ([], ['2026-01-01T00:00:00Z'], ['2026-01-01T00:00:00Z'] * 2,
                        [1, 2], ['2026-01-01T00:00:00Z', '2026-01-01T00:00:00']):
            document = self.base()
            document['timeline'] = content
            with self.subTest(content=content), self.assertRaises(ValueError):
                reconcile_analyze(document)
        document = self.base()
        document['timeline'] = ['2026-01-01T00:00:00Z'] * 162
        with self.assertRaises(ValueError):
            reconcile_analyze(document)

    def test_business_integers_reject_bools_and_floats(self):
        fields = [('max_slot_units',), ('meters', 0, 'start'), ('meters', 0, 'end'),
                  ('meters', 0, 'lower'), ('meters', 0, 'upper'),
                  ('invoices', 0, 'start'), ('invoices', 0, 'end'), ('invoices', 0, 'units'),
                  ('queries', 0, 'start'), ('queries', 0, 'end')]
        for address in fields:
            for value in (True, 1.0):
                document = self.base()
                container = document
                for step in address[:-1]:
                    container = container[step] if not isinstance(step, int) else container[step]
                container[address[-1]] = value
                with self.subTest(address=address, value=value), self.assertRaises(ValueError):
                    reconcile_analyze(document)

    def test_business_integer_ranges(self):
        document = self.base()
        document['max_slot_units'] = 0
        with self.assertRaises(ValueError):
            reconcile_analyze(document)
        document = self.base()
        document['max_slot_units'] = 10 ** 12 + 1
        with self.assertRaises(ValueError):
            reconcile_analyze(document)
        document = self.base()
        document['invoices'][0]['units'] = 10 ** 15 + 1
        with self.assertRaises(ValueError):
            reconcile_analyze(document)
        document = self.base()
        document['meters'][0]['lower'] = 6
        with self.assertRaises(ValueError):
            reconcile_analyze(document)

    def test_indices_are_checked_against_slots(self):
        for name in ('meters', 'invoices', 'queries'):
            for start, end in ((-1, 1), (0, 4), (2, 1), (1, 1), (True, 2)):
                document = self.base()
                document[name][0]['start'] = start
                document[name][0]['end'] = end
                with self.subTest(name=name, start=start, end=end), self.assertRaises(ValueError):
                    reconcile_analyze(document)

    def test_duplicate_ids_are_rejected_per_array(self):
        for name, copy in (('meters', {'id': 'm', 'start': 1, 'end': 2, 'lower': 0, 'upper': 1}),
                           ('invoices', {'id': 'i', 'start': 1, 'end': 2, 'units': 0}),
                           ('queries', {'id': 'q', 'start': 1, 'end': 2})):
            document = self.base()
            document[name].append(copy)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'duplicate'):
                reconcile_analyze(document)

    def test_rows_require_exact_fields(self):
        document = self.base()
        document['meters'][0]['extra'] = 1
        with self.assertRaises(ValueError):
            reconcile_analyze(document)

    def test_full_validation_precedes_business_judgement(self):
        # Meters alone are already contradictory (3+3 units in two slots that
        # a total upper bound of 5 forbids), but validation must still examine
        # the invoices and queries before any conflict is reported.
        document = doc(
            3, 10,
            meters=[{'id': 'a', 'start': 0, 'end': 1, 'lower': 3, 'upper': 4},
                    {'id': 'b', 'start': 1, 'end': 2, 'lower': 3, 'upper': 4},
                    {'id': 'c', 'start': 0, 'end': 2, 'lower': 0, 'upper': 5}],
            invoices=[{'id': 'i', 'start': 0, 'end': 2, 'units': 1}],
            queries=[{'id': 'q', 'start': 0, 'end': 2}])
        self.assertEqual(brute_status_and_ranges(document)[0], 'meter_conflict')
        self.assertEqual(reconcile_analyze(document)['status'], 'meter_conflict')
        for name, broken in (
                ('invoices', {'id': 'i', 'start': 0, 'end': 2, 'units': 10 ** 16}),
                ('queries', {'id': 'q', 'start': 2, 'end': 9}),
                ('queries', {'id': 'q', 'start': 0, 'end': 1, 'extra': 0})):
            invalid = json.loads(json.dumps(document))
            invalid[name] = [broken]
            with self.subTest(name=name, broken=broken), self.assertRaises(ValueError):
                reconcile_analyze(invalid)

    def test_empty_optional_arrays_are_valid(self):
        document = doc(2, 3, queries=[{'id': 'q', 'start': 0, 'end': 1}])
        self.assertEqual(reconcile_analyze(document)['status'], 'feasible')
        result = reconcile_analyze(doc(2, 3))
        self.assertEqual(result, {'status': 'feasible', 'ranges': [], 'certificate': []})

    def test_input_is_not_mutated(self):
        document = self.base()
        before = json.dumps(document, sort_keys=True)
        reconcile_analyze(document)
        self.assertEqual(before, json.dumps(document, sort_keys=True))


class ReconcileLargeTests(unittest.TestCase):
    def test_long_overlapping_evidence_many_queries(self):
        slot_count = 160
        timeline = ['2026-01-01T%02d:%02d:00Z' % (i // 60, i % 60) for i in range(161)]
        meters = []
        for i in range(0, slot_count - 10, 10):
            meters.append({'id': 'meter-%03d' % i, 'start': i, 'end': min(slot_count, i + 25),
                           'lower': 0, 'upper': 10 ** 6})
        invoices = []
        for i in range(0, slot_count - 20, 20):
            invoices.append({'id': 'invoice-%03d' % i, 'start': i, 'end': i + 40,
                             'units': 5000 * (1 + i % 3)})
        queries = [{'id': 'q%02d' % i, 'start': i, 'end': min(slot_count, i + 37)}
                   for i in range(0, slot_count, 20)][:40]
        document = {'timeline': timeline, 'max_slot_units': 10 ** 9,
                    'meters': meters, 'invoices': invoices, 'queries': queries}
        result = reconcile_analyze(document)
        self.assertEqual(result['status'], 'feasible')
        self.assertEqual(len(result['ranges']), len(queries))
        for query in queries:
            entry = next(x for x in result['ranges'] if x['id'] == query['id'])
            self.assertLessEqual(entry['min_units'], entry['max_units'])
            check_allocation(self, document, entry['min_allocation'], query, entry['min_units'])
            check_allocation(self, document, entry['max_allocation'], query, entry['max_units'])

    def test_large_conflict_certificate_is_short(self):
        slot_count = 160
        timeline = ['2026-01-01T%02d:%02d:00Z' % (i // 60, i % 60) for i in range(161)]
        document = {
            'timeline': timeline,
            'max_slot_units': 100,
            'meters': [{'id': 'm%d' % i, 'start': i, 'end': i + 1, 'lower': 100, 'upper': 100}
                       for i in range(0, slot_count, 2)],
            'invoices': [{'id': 'i', 'start': 0, 'end': slot_count, 'units': 0}],
            'queries': [{'id': 'q', 'start': 0, 'end': slot_count}],
        }
        result = reconcile_analyze(document)
        self.assertEqual(result['status'], 'invoice_conflict')
        check_certificate(self, document, result)


if __name__ == '__main__':
    unittest.main()
