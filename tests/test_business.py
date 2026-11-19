import json
import unittest
from pathlib import Path
from api_quota_audit.model import analyze, instant

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return json.loads((ROOT / 'fixtures/input.json').read_text())


class ApiQuotaTests(unittest.TestCase):
    def test_fixture_windows_and_excess(self):
        report = analyze(fixture())
        read = next(x for x in report['windows'] if x['route'] == '/records' and x['window_start_utc'].endswith('00:00:00Z'))
        self.assertEqual((read['used'], read['limit'], read['over_by'], read['exceeded']), (7, 5, 2, True))
        self.assertEqual((report['window_count'], report['exceeded_count']), (3, 1))

    def test_identical_event_id_is_idempotent(self):
        data = fixture()
        data['events'].append(data['events'][0])
        self.assertEqual(analyze(data)['windows'][0]['used'], 7)

    def test_conflicting_event_id_rejected(self):
        data = fixture()
        duplicate = dict(data['events'][0], units=4)
        data['events'].append(duplicate)
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            analyze(data)

    def test_boundary_starts_next_window(self):
        report = analyze(fixture())
        starts = [x['window_start_utc'] for x in report['windows'] if x['route'] == '/records']
        self.assertIn('2026-01-01T01:00:00Z', starts)

    def test_offset_normalized_to_utc(self):
        self.assertEqual(instant('2026-01-01T01:00:00+01:00'), instant('2026-01-01T00:00:00Z'))

    def test_policy_scope_is_exact(self):
        data = fixture()
        data['events'][0]['route'] = '/unknown'
        with self.assertRaisesRegex(ValueError, 'no exact'):
            analyze(data)

    def test_offset_required(self):
        with self.assertRaisesRegex(ValueError, 'offset'):
            instant('2026-01-01T00:00:00')

    def test_units_must_be_positive_integer(self):
        for value in (0, True, 1.5):
            data = fixture()
            data['events'][0]['units'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                analyze(data)

    def test_duplicate_policy_scope_rejected(self):
        data = fixture()
        data['policies'].append(dict(data['policies'][0], id='another-id'))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            analyze(data)

    def test_negative_epoch_uses_floor_window(self):
        data = {'policies': [{'id': 'p', 'principal': 'a', 'route': '/', 'window_seconds': 60, 'limit': 5}],
                'events': [{'id': 'e', 'principal': 'a', 'route': '/', 'at': '1969-12-31T23:59:59Z', 'units': 1}]}
        self.assertEqual(analyze(data)['windows'][0]['window_start_utc'], '1969-12-31T23:59:00Z')

    def test_subsecond_conflicting_duplicate_is_rejected(self):
        data = fixture()
        data['events'][0]['at'] = '2026-01-01T00:59:00.100000Z'
        data['events'].append(dict(data['events'][0], at='2026-01-01T00:59:00.200000Z'))
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            analyze(data)

    def test_subsecond_before_epoch_uses_previous_window(self):
        data = {'policies': [{'id': 'p', 'principal': 'a', 'route': '/', 'window_seconds': 60, 'limit': 5}],
                'events': [{'id': 'e', 'principal': 'a', 'route': '/', 'at': '1969-12-31T23:59:59.999999Z', 'units': 1}]}
        self.assertEqual(analyze(data)['windows'][0]['window_start_utc'], '1969-12-31T23:59:00Z')


class ChargebackBusinessTests(unittest.TestCase):
    """Regression-adjacent hand-computed chargeback checks."""

    def test_hand_computed_pool_cost_and_cells(self):
        from api_quota_audit.chargeback import chargeback_analyze
        data = {
            'tariffs': [{'id': 't', 'principal': 'p', 'route': '/', 'window_seconds': 3600,
                         'tiers': [{'upto': 10, 'rate_num': 3, 'rate_den': 1},
                                   {'upto': 20, 'rate_num': 1, 'rate_den': 2},
                                   {'upto': None, 'rate_num': 1, 'rate_den': 4}]}],
            'centers': [{'id': 'A', 'required_cents': 13},
                        {'id': 'B', 'required_cents': 25}],
            'events': [{'id': 'a', 'principal': 'p', 'route': '/',
                        'at': '2026-01-01T00:00:00Z', 'units': 10, 'center': 'A'},
                       {'id': 'b', 'principal': 'p', 'route': '/',
                        'at': '2026-01-01T00:00:00Z', 'units': 20, 'center': 'B'}],
            'locks': [],
        }
        # U = 30: 10*3 + 10*(1/2) + 10*(1/4) = 30 + 5 + 2.5 = 37.5 -> R = 38.
        result = chargeback_analyze(data)
        self.assertEqual(result['pools'][0]['units'], 30)
        self.assertEqual(result['pools'][0]['exact_cost'], {'numerator': 75, 'denominator': 2})
        self.assertEqual(result['pools'][0]['invoice_cents'], 38)
        # a_A = 37.5 * 10/30 = 12.5, a_B = 25.  Only B may round down; the
        # matrix [13, 25] hits both columns exactly.
        self.assertEqual([c['exact_cost'] for c in result['cells']],
                         [{'numerator': 25, 'denominator': 2},
                          {'numerator': 25, 'denominator': 1}])
        self.assertEqual([c['cents'] for c in result['cells']], [13, 25])
        self.assertEqual(result['rounding_error'], {'numerator': 1, 'denominator': 2})
