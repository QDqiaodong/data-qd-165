"""Retention mode tests.

The independent oracle here is an exhaustive subset enumeration written from
the data contract alone; it never calls the min-cost flow solver under test.
"""
import itertools
import random
import unittest

from api_quota_audit.retention import retention_analyze, parse_retention_input


def request(request_id, principal, route, at, priority=10):
    return {'id': request_id, 'principal': principal, 'route': route, 'at': at, 'priority': priority}


def policy_pair():
    return [
        [{'principal': 'a', 'window_seconds': 3600, 'limit': 1},
         {'principal': 'b', 'window_seconds': 3600, 'limit': 1}],
        [{'route': '1', 'window_seconds': 3600, 'limit': 1},
         {'route': '2', 'window_seconds': 3600, 'limit': 1}],
    ]


def brute_force(data, forced_id=None):
    """Independent enumeration oracle over every subset of unique requests."""
    records, p_policies, r_policies, _, _ = parse_retention_input(data)
    p_window_of, r_window_of = {}, {}
    p_cap, r_cap = {}, {}
    for record in records:
        p_window, p_limit = p_policies[record.principal]
        r_window, r_limit = r_policies[record.route]
        p_key = ('p', record.principal, record.at // (p_window * 1_000_000) * p_window)
        r_key = ('r', record.route, record.at // (r_window * 1_000_000) * r_window)
        p_window_of[record.id] = p_key
        r_window_of[record.id] = r_key
        p_cap[p_key] = p_limit
        r_cap[r_key] = r_limit
    by_id = {record.id: record for record in records}

    def feasible(members):
        p_used, r_used = {}, {}
        for request_id in members:
            p_key = p_window_of[request_id]
            r_key = r_window_of[request_id]
            p_used[p_key] = p_used.get(p_key, 0) + 1
            r_used[r_key] = r_used.get(r_key, 0) + 1
            if p_used[p_key] > p_cap[p_key] or r_used[r_key] > r_cap[r_key]:
                return False
        return True

    best = None
    for size in range(len(records), -1, -1):
        for combo in itertools.combinations(sorted(by_id), size):
            if forced_id is not None and forced_id not in combo:
                continue
            if not feasible(combo):
                continue
            priority_sum = sum(by_id[request_id].priority for request_id in combo)
            # Maximize size, then maximize priority_sum, then prefer the
            # lexicographically smallest sorted kept list: at the first
            # membership difference, the set containing the smaller id wins.
            key = (-priority_sum, tuple(0 if request_id in combo else 1
                                        for request_id in sorted(by_id)))
            if best is None or key < best[0]:
                best = (key, set(combo), priority_sum)
        if best is not None:
            return best[1]
    return set() if forced_id is None else {forced_id}


class RetentionHandcraftedTests(unittest.TestCase):
    def test_cycle_replacement_chain_of_three_or_more_edges(self):
        principal_policies, route_policies = policy_pair()
        principal_policies[0]['limit'] = 2
        data = {
            'principal_policies': principal_policies,
            'route_policies': route_policies,
            'requests': [
                request('r1', 'a', '1', '2026-01-01T00:00:00Z'),
                request('r2', 'a', '1', '2026-01-01T00:01:00Z'),
                request('r3', 'a', '2', '2026-01-01T00:02:00Z'),
                request('r4', 'a', '2', '2026-01-01T00:03:00Z'),
            ],
            'appeals': ['r2', 'r4'],
        }
        result = retention_analyze(data)
        self.assertEqual(result['baseline']['kept_ids'], ['r1', 'r3'])
        self.assertEqual(result['baseline']['dropped_ids'], ['r2', 'r4'])
        self.assertEqual(result['baseline']['kept_count'], 2)
        self.assertEqual(result['baseline']['priority_sum'], 20)
        # Forcing r2 travels source-P-r2-R1 vs r1 and leaves the r3/r4 side
        # of the shared capacity relationship intact.
        appeal_r2 = result['appeals'][0]
        self.assertEqual(appeal_r2['kept_ids'], ['r2', 'r3'])
        self.assertEqual(appeal_r2['entered_ids'], ['r2'])
        self.assertEqual(appeal_r2['removed_ids'], ['r1'])
        self.assertEqual((appeal_r2['count_loss'], appeal_r2['priority_loss']), (0, 0))
        appeal_r4 = result['appeals'][1]
        self.assertEqual(appeal_r4['kept_ids'], ['r1', 'r4'])
        self.assertEqual(appeal_r4['entered_ids'], ['r4'])
        self.assertEqual(appeal_r4['removed_ids'], ['r3'])

    def test_forced_request_outside_baseline_triggers_count_loss(self):
        principal_policies, route_policies = policy_pair()
        data = {
            'principal_policies': principal_policies,
            'route_policies': route_policies,
            'requests': [
                request('r1', 'a', '1', '2026-01-01T00:00:00Z'),
                request('r2', 'a', '2', '2026-01-01T00:01:00Z'),
                request('r3', 'b', '2', '2026-01-01T00:02:00Z'),
            ],
            'appeals': ['r1', 'r2', 'r3'],
        }
        result = retention_analyze(data)
        self.assertEqual(result['baseline']['kept_ids'], ['r1', 'r3'])
        forced = next(item for item in result['appeals'] if item['request_id'] == 'r2')
        self.assertEqual(forced['kept_ids'], ['r2'])
        self.assertEqual(forced['dropped_ids'], ['r1', 'r3'])
        self.assertEqual(forced['entered_ids'], ['r2'])
        self.assertEqual(forced['removed_ids'], ['r1', 'r3'])
        self.assertEqual(forced['kept_count'], 1)
        self.assertEqual((forced['count_loss'], forced['priority_loss']), (1, 10))
        # Appeals already in the baseline reproduce the baseline exactly.
        for appeal_id in ('r1', 'r3'):
            view = next(item for item in result['appeals'] if item['request_id'] == appeal_id)
            self.assertEqual(view['kept_ids'], ['r1', 'r3'])
            self.assertEqual((view['entered_ids'], view['removed_ids'],
                              view['count_loss'], view['priority_loss']), ([], [], 0, 0))

    def test_count_outranks_priority_anti_greedy_case(self):
        # Principal b has one slot and route 2 has one slot, both contested
        # by hi and l2. Taking the high-priority hi blocks l2 and caps the
        # total at two; dropping hi keeps the three low-priority requests,
        # so the cardinality objective beats the priority objective.
        data = {
            'principal_policies': [{'principal': 'a', 'window_seconds': 3600, 'limit': 2},
                                   {'principal': 'b', 'window_seconds': 3600, 'limit': 1}],
            'route_policies': [{'route': '1', 'window_seconds': 3600, 'limit': 2},
                               {'route': '2', 'window_seconds': 3600, 'limit': 1}],
            'requests': [
                request('hi', 'b', '1', '2026-01-01T00:00:00Z', priority=999),
                request('l1', 'a', '1', '2026-01-01T00:01:00Z', priority=1),
                request('l2', 'b', '2', '2026-01-01T00:02:00Z', priority=1),
                request('l3', 'a', '1', '2026-01-01T00:03:00Z', priority=1),
            ],
            'appeals': [],
        }
        result = retention_analyze(data)
        self.assertEqual(result['baseline']['kept_ids'], ['l1', 'l2', 'l3'])
        self.assertEqual(result['baseline']['priority_sum'], 3)

    def test_priority_outranks_id_tiebreak(self):
        # Two equally sized options; priority decides before id order.
        data = {
            'principal_policies': [{'principal': 'a', 'window_seconds': 3600, 'limit': 1}],
            'route_policies': [{'route': '1', 'window_seconds': 3600, 'limit': 1}],
            'requests': [
                request('aaa', 'a', '1', '2026-01-01T00:00:00Z', priority=1),
                request('zzz', 'a', '1', '2026-01-01T00:01:00Z', priority=2),
            ],
            'appeals': [],
        }
        self.assertEqual(retention_analyze(data)['baseline']['kept_ids'], ['zzz'])

    def test_fully_equal_priority_uses_lexicographic_id_tiebreak(self):
        data = {
            'principal_policies': [{'principal': 'a', 'window_seconds': 3600, 'limit': 1}],
            'route_policies': [{'route': '1', 'window_seconds': 3600, 'limit': 1}],
            'requests': [
                request('zzz', 'a', '1', '2026-01-01T00:00:00Z'),
                request('aaa', 'a', '1', '2026-01-01T00:01:00Z'),
                request('mmm', 'a', '1', '2026-01-01T00:02:00Z'),
            ],
            'appeals': [],
        }
        self.assertEqual(retention_analyze(data)['baseline']['kept_ids'], ['aaa'])

    def test_changing_one_priority_changes_baseline_and_appeal_explanation(self):
        data = {
            'principal_policies': [{'principal': 'a', 'window_seconds': 3600, 'limit': 2}],
            'route_policies': [{'route': '1', 'window_seconds': 3600, 'limit': 1},
                               {'route': '2', 'window_seconds': 3600, 'limit': 1}],
            'requests': [
                request('r1', 'a', '1', '2026-01-01T00:00:00Z', priority=10),
                request('r2', 'a', '1', '2026-01-01T00:01:00Z', priority=10),
                request('r3', 'a', '2', '2026-01-01T00:02:00Z', priority=10),
                request('r4', 'a', '2', '2026-01-01T00:03:00Z', priority=10),
            ],
            'appeals': ['r2', 'r4'],
        }
        first = retention_analyze(data)
        self.assertEqual(first['baseline']['kept_ids'], ['r1', 'r3'])
        # r2 becomes the preferred route-1 member, flipping the baseline and
        # the appeal explanation on both sides.
        data['requests'][1]['priority'] = 50
        second = retention_analyze(data)
        self.assertEqual(second['baseline']['kept_ids'], ['r2', 'r3'])
        self.assertEqual(second['baseline']['priority_sum'], 60)
        appeal_r2 = next(item for item in second['appeals'] if item['request_id'] == 'r2')
        self.assertEqual((appeal_r2['entered_ids'], appeal_r2['removed_ids']), ([], []))
        appeal_r4 = next(item for item in second['appeals'] if item['request_id'] == 'r4')
        self.assertEqual(appeal_r4['kept_ids'], ['r2', 'r4'])
        self.assertEqual(appeal_r4['removed_ids'], ['r3'])
        self.assertEqual(appeal_r4['priority_loss'], 0)
        # Signed loss reporting is covered separately below.

    def test_priority_loss_is_signed_and_never_clamped(self):
        # Same-cardinality forced optima can never beat the baseline sum
        # (the forced feasible region is a subset), so a negative loss only
        # appears together with a count loss. Forcing h shrinks the retained
        # set from two requests to one, but the surviving request carries
        # more priority than the whole baseline: the loss is -85, not zero.
        forced = retention_analyze({
            'principal_policies': [
                {'principal': 'a', 'window_seconds': 3600, 'limit': 1},
                {'principal': 'b', 'window_seconds': 3600, 'limit': 1}],
            'route_policies': [
                {'route': '1', 'window_seconds': 3600, 'limit': 1},
                {'route': '2', 'window_seconds': 3600, 'limit': 1}],
            'requests': [
                request('f', 'a', '1', '2026-01-01T00:00:00Z', priority=10),
                request('h', 'a', '2', '2026-01-01T00:01:00Z', priority=100),
                request('x', 'b', '2', '2026-01-01T00:02:00Z', priority=5),
            ],
            'appeals': ['h'],
        })
        self.assertEqual(forced['baseline']['kept_ids'], ['f', 'x'])
        self.assertEqual(forced['baseline']['priority_sum'], 15)
        appeal_h = forced['appeals'][0]
        self.assertEqual(appeal_h['kept_ids'], ['h'])
        self.assertEqual(appeal_h['entered_ids'], ['h'])
        self.assertEqual(appeal_h['removed_ids'], ['f', 'x'])
        self.assertEqual(appeal_h['count_loss'], 1)
        self.assertEqual(appeal_h['priority_loss'], -85)

    def test_window_boundary_negative_epoch_and_timezone_offsets(self):
        # Window 3600, one slot per side: the +01:00 timestamp normalizes to
        # 00:30Z and therefore shares the [00:00,01:00) bucket with the
        # negative-epoch-style UTC request; the 01:00Z request opens the next
        # half-open bucket and survives via the id tiebreak against 'neg'.
        data = {
            'principal_policies': [{'principal': 'a', 'window_seconds': 3600, 'limit': 1}],
            'route_policies': [{'route': '1', 'window_seconds': 3600, 'limit': 1}],
            'requests': [
                request('neg', 'a', '1', '1970-01-01T00:30:00Z'),
                request('offset', 'a', '1', '1970-01-01T01:30:00+01:00'),
                request('boundary', 'a', '1', '1970-01-01T01:00:00Z'),
            ],
            'appeals': [],
        }
        result = retention_analyze(data)
        self.assertEqual(result['baseline']['kept_count'], 2)
        self.assertEqual(result['baseline']['kept_ids'], ['boundary', 'neg'])
        self.assertEqual(result['baseline']['dropped_ids'], ['offset'])

    def test_negative_epoch_floors_into_pre_epoch_bucket(self):
        data = {
            'principal_policies': [{'principal': 'a', 'window_seconds': 3600, 'limit': 1}],
            'route_policies': [{'route': '1', 'window_seconds': 3600, 'limit': 1}],
            'requests': [
                request('neg1', 'a', '1', '1969-12-31T23:30:00.500000Z'),
                request('neg2', 'a', '1', '1969-12-31T23:45:00Z'),
            ],
            'appeals': [],
        }
        result = retention_analyze(data)
        # Both floor into the [-01:00,00:00) hour bucket, so one is dropped.
        self.assertEqual(result['baseline']['kept_ids'], ['neg1'])
        self.assertEqual(result['baseline']['dropped_ids'], ['neg2'])

    def test_principal_and_route_windows_have_different_lengths(self):
        data = {
            'principal_policies': [{'principal': 'a', 'window_seconds': 7200, 'limit': 1}],
            'route_policies': [{'route': '1', 'window_seconds': 3600, 'limit': 2}],
            'requests': [
                request('r1', 'a', '1', '2026-01-01T00:10:00Z'),
                request('r2', 'a', '1', '2026-01-01T01:10:00Z'),
                request('r3', 'a', '1', '2026-01-01T02:10:00Z'),
            ],
            'appeals': ['r3'],
        }
        result = retention_analyze(data)
        # The two-hour principal window binds at one; different hour-long
        # route windows never bind, so every principal bucket keeps one.
        self.assertEqual(result['baseline']['kept_ids'], ['r1', 'r3'])
        self.assertEqual(result['baseline']['dropped_ids'], ['r2'])
        appeal = result['appeals'][0]
        self.assertEqual(appeal['kept_ids'], ['r1', 'r3'])
        self.assertEqual((appeal['count_loss'], appeal['priority_loss']), (0, 0))

    def test_duplicate_canonical_request_is_deduplicated(self):
        data = {
            'principal_policies': [{'principal': 'a', 'window_seconds': 3600, 'limit': 1}],
            'route_policies': [{'route': '1', 'window_seconds': 3600, 'limit': 1}],
            'requests': [
                request('dup', 'a', '1', '2026-01-01T00:00:00Z'),
                request('dup', 'a', '1', '2026-01-01T01:00:00+01:00'),
            ],
            'appeals': [],
        }
        result = retention_analyze(data)
        self.assertEqual(result['deduplicated_request_count'], 1)
        self.assertEqual(result['baseline']['kept_ids'], ['dup'])
        self.assertEqual(result['baseline']['dropped_ids'], [])

    def test_empty_requests_and_empty_appeals_are_valid(self):
        data = {
            'principal_policies': [{'principal': 'a', 'window_seconds': 3600, 'limit': 1}],
            'route_policies': [{'route': '1', 'window_seconds': 3600, 'limit': 1}],
            'requests': [],
            'appeals': [],
        }
        result = retention_analyze(data)
        self.assertEqual(result['deduplicated_request_count'], 0)
        self.assertEqual(result['baseline'],
                         {'kept_ids': [], 'dropped_ids': [], 'kept_count': 0, 'priority_sum': 0})
        self.assertEqual(result['appeals'], [])


class RetentionInvarianceTests(unittest.TestCase):
    def test_input_order_and_appeal_order_do_not_change_result(self):
        principal_policies, route_policies = policy_pair()
        requests = [
            request('r1', 'a', '1', '2026-01-01T00:00:00Z', 5),
            request('r2', 'a', '2', '2026-01-01T00:01:00Z', 8),
            request('r3', 'b', '2', '2026-01-01T00:02:00Z', 3),
            request('r4', 'b', '1', '2026-01-01T00:03:00Z', 9),
        ]
        base = {
            'principal_policies': principal_policies,
            'route_policies': route_policies,
            'requests': requests,
            'appeals': ['r1', 'r2', 'r3', 'r4'],
        }
        shuffled = {
            'principal_policies': list(reversed(principal_policies)),
            'route_policies': list(reversed(route_policies)),
            'requests': list(reversed(requests)),
            'appeals': ['r4', 'r2', 'r3', 'r1'],
        }
        self.assertEqual(retention_analyze(base), retention_analyze(shuffled))

    def test_repeated_canonical_requests_anywhere_do_not_change_result(self):
        principal_policies, route_policies = policy_pair()
        requests = [
            request('r1', 'a', '1', '2026-01-01T00:00:00Z'),
            request('r2', 'a', '2', '2026-01-01T00:01:00Z'),
            request('r3', 'b', '2', '2026-01-01T00:02:00Z'),
        ]
        data = {'principal_policies': principal_policies, 'route_policies': route_policies,
                'requests': requests, 'appeals': ['r2']}
        with_duplicates = dict(data, requests=requests + [
            dict(requests[2]),
            request('r3', 'b', '2', '2026-01-01T02:02:00+02:00'),
        ])
        self.assertEqual(retention_analyze(data), retention_analyze(with_duplicates))


class RetentionValidationTests(unittest.TestCase):
    def _valid(self):
        return {
            'principal_policies': [{'principal': 'a', 'window_seconds': 3600, 'limit': 1}],
            'route_policies': [{'route': '1', 'window_seconds': 3600, 'limit': 1}],
            'requests': [request('r1', 'a', '1', '2026-01-01T00:00:00Z')],
            'appeals': [],
        }

    def test_top_level_fields_are_strict(self):
        data = self._valid()
        del data['appeals']
        data['extra'] = 1
        with self.assertRaises(ValueError):
            retention_analyze(data)

    def test_policy_rows_are_strict_and_scoped_unique(self):
        data = self._valid()
        data['principal_policies'] = [{'principal': 'a', 'window_seconds': 3600}]
        with self.assertRaises(ValueError):
            retention_analyze(data)
        data = self._valid()
        data['route_policies'].append({'route': '1', 'window_seconds': 3600, 'limit': 1})
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            retention_analyze(data)

    def test_window_and_limit_reject_bools_and_out_of_range(self):
        cases = [
            ('principal_policies', 0, 'window_seconds', 0),
            ('principal_policies', 0, 'window_seconds', True),
            ('principal_policies', 0, 'limit', 401),
            ('principal_policies', 0, 'limit', False),
            ('route_policies', 0, 'window_seconds', 31536001),
            ('route_policies', 0, 'limit', 1.5),
        ]
        for section, index, field, value in cases:
            data = self._valid()
            data[section][index][field] = value
            with self.subTest(case=(section, field, value)):
                with self.assertRaises(ValueError):
                    retention_analyze(data)

    def test_request_rows_are_strict(self):
        data = self._valid()
        data['requests'] = [{'id': 'r1', 'principal': 'a', 'route': '1',
                             'at': '2026-01-01T00:00:00Z'}]
        with self.assertRaises(ValueError):
            retention_analyze(data)

    def test_priority_bounds_and_bool_rejection(self):
        for value in (-1, 1_000_000_001, True, 2.0):
            data = self._valid()
            data['requests'][0]['priority'] = value
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    retention_analyze(data)

    def test_conflicting_duplicate_id_fails_whole_input(self):
        data = self._valid()
        data['requests'].append(request('r1', 'a', '1', '2026-01-01T00:00:01Z'))
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            retention_analyze(data)

    def test_every_request_needs_both_policies(self):
        data = self._valid()
        data['requests'][0]['route'] = '/unknown'
        with self.assertRaisesRegex(ValueError, 'route policy'):
            retention_analyze(data)
        data = self._valid()
        data['requests'][0]['principal'] = 'nobody'
        with self.assertRaisesRegex(ValueError, 'principal policy'):
            retention_analyze(data)

    def test_appeals_must_exist_and_be_unique(self):
        data = self._valid()
        data['appeals'] = ['ghost']
        with self.assertRaisesRegex(ValueError, 'unknown request'):
            retention_analyze(data)
        data = self._valid()
        data['appeals'] = ['r1', 'r1']
        with self.assertRaisesRegex(ValueError, 'duplicate appeal'):
            retention_analyze(data)

    def test_timestamp_requires_explicit_offset(self):
        data = self._valid()
        data['requests'][0]['at'] = '2026-01-01T00:00:00'
        with self.assertRaisesRegex(ValueError, 'offset'):
            retention_analyze(data)

    def test_array_limits_are_enforced(self):
        data = self._valid()
        data['principal_policies'] = [
            {'principal': 'p%d' % i, 'window_seconds': 3600, 'limit': 1} for i in range(101)]
        with self.assertRaisesRegex(ValueError, 'bounded array'):
            retention_analyze(data)
        data = self._valid()
        data['requests'] = [
            request('r%d' % i, 'a', '1', '2026-01-01T00:00:00Z') for i in range(401)]
        with self.assertRaisesRegex(ValueError, 'bounded array'):
            retention_analyze(data)
        data = self._valid()
        data['appeals'] = ['r1'] * 21
        with self.assertRaisesRegex(ValueError, 'bounded array'):
            retention_analyze(data)


class RetentionOracleTests(unittest.TestCase):
    def _random_document(self, rng):
        principals = ['p%d' % i for i in range(rng.randint(1, 3))]
        routes = ['r%d' % i for i in range(rng.randint(1, 3))]
        data = {
            'principal_policies': [
                {'principal': name, 'window_seconds': rng.choice([3600, 7200]),
                 'limit': rng.randint(1, 3)} for name in principals],
            'route_policies': [
                {'route': name, 'window_seconds': rng.choice([3600, 7200]),
                 'limit': rng.randint(1, 3)} for name in routes],
            'requests': [],
            'appeals': [],
        }
        size = rng.randint(0, 12)
        for index in range(size):
            hour = rng.randint(-2, 3)
            minute = rng.randint(0, 59)
            suffix = rng.choice(['Z', '+01:00', '-05:00', '+05:30'])
            timestamp = '1970-01-01T%02d:%02d:00%s' % (hour % 24, minute, suffix)
            data['requests'].append(request(
                'q%02d' % index, rng.choice(principals), rng.choice(routes),
                timestamp, rng.randint(0, 20)))
        appeal_count = rng.randint(0, min(3, size))
        data['appeals'] = rng.sample([row['id'] for row in data['requests']], appeal_count)
        return data

    def test_matches_independent_exhaustive_oracle(self):
        rng = random.Random(20261118)
        for trial in range(120):
            data = self._random_document(rng)
            with self.subTest(trial=trial):
                result = retention_analyze(data)
                expected_kept = brute_force(data)
                self.assertEqual(set(result['baseline']['kept_ids']), expected_kept)
                self.assertEqual(
                    result['baseline']['kept_ids'],
                    sorted(result['baseline']['kept_ids']))
                self.assertEqual(
                    result['baseline']['dropped_ids'],
                    sorted(set(r['id'] for r in data['requests']) - expected_kept))
                for appeal in result['appeals']:
                    expected_appeal = brute_force(data, appeal['request_id'])
                    self.assertEqual(set(appeal['kept_ids']), expected_appeal)
                    self.assertEqual(appeal['dropped_ids'],
                                     sorted(set(r['id'] for r in data['requests']) - expected_appeal))
                    self.assertEqual(appeal['entered_ids'],
                                     sorted(expected_appeal - expected_kept))
                    self.assertEqual(appeal['removed_ids'],
                                     sorted(expected_kept - expected_appeal))
                    self.assertEqual(appeal['count_loss'],
                                     len(expected_kept) - len(expected_appeal))
                    priority = lambda members: sum(
                        row['priority'] for row in data['requests'] if row['id'] in members)
                    self.assertEqual(appeal['priority_loss'],
                                     priority(expected_kept) - priority(expected_appeal))
                self.assertEqual([item['request_id'] for item in result['appeals']],
                                 sorted(data['appeals']))


class RetentionScaleTests(unittest.TestCase):
    def test_near_limit_fixture_runs_without_subset_enumeration(self):
        # 400 unique requests, 100 policies per side and 20 appeals: the
        # production path stays comfortably sub-second via min-cost flow.
        principals = ['p%02d' % i for i in range(20)]
        routes = ['r%02d' % i for i in range(20)]
        requests = []
        for index in range(400):
            requests.append(request(
                'req-%03d' % index,
                principals[index % len(principals)],
                routes[(index * 7) % len(routes)],
                '2026-01-01T%02d:%02d:%02dZ' % ((index // 60) % 24, index % 60, (index * 13) % 60),
                priority=index % 1_000_000_001))
        data = {
            'principal_policies': [
                {'principal': name, 'window_seconds': 3600, 'limit': 2} for name in principals],
            'route_policies': [
                {'route': name, 'window_seconds': 1800, 'limit': 3} for name in routes],
            'requests': requests,
            'appeals': ['req-%03d' % (19 * i) for i in range(20)],
        }
        result = retention_analyze(data)
        self.assertEqual(result['deduplicated_request_count'], 400)
        baseline = set(result['baseline']['kept_ids'])
        self.assertEqual(len(baseline), result['baseline']['kept_count'])
        self.assertTrue(set.isdisjoint(baseline, set(result['baseline']['dropped_ids'])))
        self.assertEqual(len(result['baseline']['kept_ids']) +
                         len(result['baseline']['dropped_ids']), 400)
        self.assertEqual(len(result['appeals']), 20)
        for appeal in result['appeals']:
            self.assertIn(appeal['request_id'], appeal['kept_ids'])
            self.assertEqual(
                appeal['kept_count'] + len(appeal['dropped_ids']), 400)
            self.assertEqual(appeal['entered_ids'],
                             sorted(set(appeal['kept_ids']) - baseline))
            self.assertEqual(appeal['removed_ids'],
                             sorted(baseline - set(appeal['kept_ids'])))
            self.assertGreaterEqual(appeal['count_loss'], 0)
            self.assertLessEqual(appeal['kept_count'], result['baseline']['kept_count'])


if __name__ == '__main__':
    unittest.main()
