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
