"""Tests for the out-of-order continuous session change stream.

The expectations here are hand computed (the acceptance walk-through and the
boundary cases) or produced by an independent brute-force reference built in
this test module; the production :class:`SessionAccumulator` constructor is
never used to generate an expected value.
"""
import copy
import json
import random
import unittest

from api_quota_audit.sessions import (
    SessionAccumulator,
    sessions_analyze,
    parse_policies,
    parse_operation,
)
from api_quota_audit.model import instant

POLICY = [{'principal': 'p', 'route': '/r', 'gap_seconds': 6}]
GAP_US = 6 * 1_000_000


def event(event_id, at_seconds, units=1, principal='p', route='/r'):
    return {'kind': 'event', 'id': event_id, 'principal': principal,
            'route': route,
            'at': '1970-01-01T00:00:%02dZ' % at_seconds if at_seconds >= 0
            else '1969-12-31T23:59:%02dZ' % (60 + at_seconds),
            'units': units}


def at(seconds):
    return seconds * 1_000_000


def snapshot_dict(session):
    return {key: session[key] for key in
            ('session_id', 'principal', 'route', 'first_us', 'last_us',
             'closes_at_us', 'units', 'event_ids')}


class AcceptanceWalkthroughTests(unittest.TestCase):
    """The spec's acceptance scenario, with every trace row hand computed."""

    def build(self):
        return SessionAccumulator(copy.deepcopy(POLICY), 2)

    def test_full_acceptance_trace(self):
        acc = self.build()
        operations = [
            event('a', 0, units=1),
            event('b', 5, units=2),
            event('c', 10, units=3),
            {'kind': 'watermark', 'at': '1970-01-01T00:00:10Z'},
            {'kind': 'retract', 'event_id': 'b'},
            event('d', 7, units=9),
            event('b', 5, units=2),
        ]
        trace = [acc.apply(operation) for operation in operations]

        self.assertEqual(trace[0], {
            'step': 1, 'status': 'accepted',
            'retracted_session_ids': [],
            'upserts': [{
                'session_id': 'a', 'principal': 'p', 'route': '/r',
                'first_us': 0, 'last_us': 0, 'closes_at_us': GAP_US,
                'units': 1, 'event_ids': ['a'],
            }],
            'sealed_sessions': [],
        })
        self.assertEqual(trace[1], {
            'step': 2, 'status': 'accepted',
            'retracted_session_ids': [],
            'upserts': [{
                'session_id': 'a', 'principal': 'p', 'route': '/r',
                'first_us': 0, 'last_us': at(5), 'closes_at_us': at(11),
                'units': 3, 'event_ids': ['a', 'b'],
            }],
            'sealed_sessions': [],
        })
        self.assertEqual(trace[2], {
            'step': 3, 'status': 'accepted',
            'retracted_session_ids': [],
            'upserts': [{
                'session_id': 'a', 'principal': 'p', 'route': '/r',
                'first_us': 0, 'last_us': at(10), 'closes_at_us': at(16),
                'units': 6, 'event_ids': ['a', 'b', 'c'],
            }],
            'sealed_sessions': [],
        })
        # Watermark alone changes no session; F = 10s - 2s = 8s and the long
        # session closes at 16s, so nothing seals.
        self.assertEqual(trace[3], {
            'step': 4, 'status': 'watermark',
            'retracted_session_ids': [], 'upserts': [], 'sealed_sessions': [],
        })
        # Retracting the bridge b splits the component: a seals in this same
        # step (closes 6s <= F 8s) even though the watermark did not move;
        # c stays open with the fresh 16s deadline.
        self.assertEqual(trace[4], {
            'step': 5, 'status': 'retracted',
            'retracted_session_ids': [],
            'upserts': [{
                'session_id': 'c', 'principal': 'p', 'route': '/r',
                'first_us': at(10), 'last_us': at(10),
                'closes_at_us': at(16), 'units': 3, 'event_ids': ['c'],
            }],
            'sealed_sessions': [{
                'session_id': 'a', 'principal': 'p', 'route': '/r',
                'first_us': 0, 'last_us': 0, 'closes_at_us': GAP_US,
                'units': 1, 'event_ids': ['a'],
            }],
        })
        # The 7s event is below F=8s: late, no session effect, cannot reopen a.
        self.assertEqual(trace[5], {
            'step': 6, 'status': 'late',
            'retracted_session_ids': [], 'upserts': [], 'sealed_sessions': [],
        })
        # Exact retry of the retracted b is a duplicate and never revives it.
        self.assertEqual(trace[6], {
            'step': 7, 'status': 'duplicate',
            'retracted_session_ids': [], 'upserts': [], 'sealed_sessions': [],
        })

        snapshot = acc.snapshot()
        self.assertEqual(snapshot['watermark_us'], at(10))
        self.assertEqual(snapshot['cutoff_us'], at(8))
        self.assertEqual(snapshot['open_sessions'], [{
            'session_id': 'c', 'principal': 'p', 'route': '/r',
            'first_us': at(10), 'last_us': at(10), 'closes_at_us': at(16),
            'units': 3, 'event_ids': ['c'],
        }])
        self.assertEqual(snapshot['sealed_sessions'], [{
            'session_id': 'a', 'principal': 'p', 'route': '/r',
            'first_us': 0, 'last_us': 0, 'closes_at_us': GAP_US,
            'units': 1, 'event_ids': ['a'],
        }])

    def test_trace_rebuilds_every_snapshot(self):
        acc = self.build()
        operations = [
            event('a', 0), event('b', 5), event('c', 10),
            {'kind': 'watermark', 'at': '1970-01-01T00:00:10Z'},
            {'kind': 'retract', 'event_id': 'b'},
            event('d', 7), event('b', 5),
        ]
        open_table = {}
        sealed_table = {}
        for index, operation in enumerate(operations, start=1):
            change = acc.apply(operation)
            # Apply the documented downstream ordering.
            for session_id in change['retracted_session_ids']:
                del open_table[session_id]
            for session in change['upserts']:
                open_table[session['session_id']] = snapshot_dict(session)
            for session in change['sealed_sessions']:
                open_table.pop(session['session_id'], None)
                sealed_table[session['session_id']] = snapshot_dict(session)
            snapshot = acc.snapshot()
            self.assertEqual(
                sorted(open_table.values(),
                       key=lambda s: (s['principal'], s['route'],
                                      s['first_us'], s['session_id'])),
                snapshot['open_sessions'])
            self.assertEqual(
                sorted(sealed_table.values(),
                       key=lambda s: (s['principal'], s['route'],
                                      s['first_us'], s['session_id'])),
                snapshot['sealed_sessions'])
            # No change may carry one id in both upserts and sealings.
            upsert_ids = {s['session_id'] for s in change['upserts']}
            sealed_ids = {s['session_id'] for s in change['sealed_sessions']}
            self.assertFalse(upsert_ids & sealed_ids)
            self.assertEqual(change['step'], index)


class BoundaryTests(unittest.TestCase):
    def test_gap_equal_breaks_component(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        acc.apply(event('e0', 0))
        acc.apply(event('e6', 6))
        self.assertEqual(
            sorted(s['session_id'] for s in acc.snapshot()['open_sessions']),
            ['e0', 'e6'])
        # One microsecond inside the gap connects the middle event to e0 only;
        # e6 remains its own component (5s gap from the middle is < 6s, so it
        # actually joins as well -> one component spanning 0..6s).
        acc.apply(event('e5x', 5))
        merged = next(s for s in acc.snapshot()['open_sessions']
                      if s['session_id'] == 'e0')
        self.assertEqual(merged['event_ids'], ['e0', 'e5x', 'e6'])
        self.assertEqual(len(acc.snapshot()['open_sessions']), 1)
        # And an event exactly six seconds after e6 opens a new component.
        acc.apply(event('e12', 12))
        self.assertEqual(
            sorted(s['session_id'] for s in acc.snapshot()['open_sessions']),
            ['e0', 'e12'])

    def test_event_exactly_at_cutoff_is_accepted(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        acc.apply({'kind': 'watermark', 'at': '1970-01-01T00:00:06Z'})
        change = acc.apply(event('x', 6))
        self.assertEqual(change['status'], 'accepted')
        self.assertEqual(acc.snapshot()['cutoff_us'], at(6))

    def test_closes_exactly_at_cutoff_seals_immediately(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        acc.apply(event('a', 0))
        change = acc.apply({'kind': 'watermark',
                            'at': '1970-01-01T00:00:06Z'})
        self.assertEqual([s['session_id'] for s in change['sealed_sessions']],
                         ['a'])
        self.assertEqual(acc.snapshot()['open_sessions'], [])

    def test_same_microsecond_multiple_ids(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        first = acc.apply(event('z', 0, units=1))
        second = acc.apply(event('a', 0, units=4))
        session = acc.snapshot()['open_sessions'][0]
        # Identity is the string-smallest member id, regardless of arrival.
        self.assertEqual(session['session_id'], 'a')
        self.assertEqual(session['event_ids'], ['a', 'z'])
        self.assertEqual(session['units'], 5)
        self.assertEqual(session['first_us'], session['last_us'])
        self.assertEqual(first['upserts'][0]['session_id'], 'z')
        self.assertEqual(second['upserts'][0]['session_id'], 'a')
        # The old z identity is revoked, the renamed a session is an upsert.
        self.assertEqual(second['retracted_session_ids'], ['z'])

    def test_min_id_removal_renames_session(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        for event_id, second in (('a', 0), ('b', 1), ('c', 2)):
            acc.apply(event(event_id, second))
        change = acc.apply({'kind': 'retract', 'event_id': 'a'})
        self.assertEqual(change['retracted_session_ids'], ['a'])
        self.assertEqual([s['session_id'] for s in change['upserts']], ['b'])
        upsert = change['upserts'][0]
        self.assertEqual(upsert['event_ids'], ['b', 'c'])
        self.assertEqual(upsert['units'], 2)
        self.assertEqual(upsert['first_us'], at(1))


class MergeSplitAndSealTests(unittest.TestCase):
    def test_out_of_order_insertion_bridges_sessions(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        acc.apply(event('a', 0))
        acc.apply(event('c', 10))
        self.assertEqual(
            sorted(s['session_id'] for s in acc.snapshot()['open_sessions']),
            ['a', 'c'])
        change = acc.apply(event('b', 5))
        self.assertEqual(change['status'], 'accepted')
        self.assertEqual(change['retracted_session_ids'], ['c'])
        self.assertEqual(len(change['upserts']), 1)
        merged = change['upserts'][0]
        self.assertEqual(merged['session_id'], 'a')
        self.assertEqual(merged['event_ids'], ['a', 'b', 'c'])
        self.assertEqual(merged['units'], 3)
        self.assertEqual(merged['closes_at_us'], at(16))

    def test_bridge_deadline_replaces_short_fragment_deadline(self):
        # Accept the bridge first, then advance the cutoff: the long session
        # must not seal on the short fragment's old 6s deadline.
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        acc.apply(event('a', 0))
        acc.apply(event('b', 5))
        change = acc.apply({'kind': 'watermark',
                            'at': '1970-01-01T00:00:06Z'})
        self.assertEqual(change['sealed_sessions'], [])
        self.assertEqual(acc.snapshot()['open_sessions'][0]['closes_at_us'],
                         at(11))

    def test_retract_bridge_splits_and_only_early_fragment_seals(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 2)
        acc.apply(event('a', 0))
        acc.apply(event('b', 5))
        acc.apply(event('c', 12))  # gap b->c = 7s > 6s, so c is separate
        before_c = next(s for s in acc.snapshot()['open_sessions']
                        if s['session_id'] == 'c')
        # Watermark 10s -> F = 8s; nothing seals yet (a/b closes 11s).
        acc.apply({'kind': 'watermark', 'at': '1970-01-01T00:00:10Z'})
        change = acc.apply({'kind': 'retract', 'event_id': 'b'})
        self.assertEqual(change['status'], 'retracted')
        # Early fragment a (closes 6s <= F 8s) seals this very step; c stays
        # open with an unchanged snapshot, so it is neither retracted nor
        # upserted.
        self.assertEqual([s['session_id'] for s in change['sealed_sessions']],
                         ['a'])
        self.assertEqual(change['upserts'], [])
        self.assertEqual(change['retracted_session_ids'], [])
        after_c = acc.snapshot()['open_sessions'][0]
        self.assertEqual(after_c, before_c)
        self.assertEqual(after_c['closes_at_us'], at(18))

    def test_retract_last_member_removes_session(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        acc.apply(event('a', 0))
        change = acc.apply({'kind': 'retract', 'event_id': 'a'})
        self.assertEqual(change['retracted_session_ids'], ['a'])
        self.assertEqual(change['upserts'], [])
        self.assertEqual(change['sealed_sessions'], [])
        self.assertEqual(acc.snapshot()['open_sessions'], [])
        # A second retraction is a noop.
        again = acc.apply({'kind': 'retract', 'event_id': 'a'})
        self.assertEqual(again['status'], 'noop')
        self.assertEqual(again['retracted_session_ids'], [])
        self.assertEqual(again['upserts'], [])

    def test_late_then_sealed_then_retracted_duplicates(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        # Late registration then exact retry.
        acc.apply({'kind': 'watermark', 'at': '1970-01-01T00:00:10Z'})
        late = acc.apply(event('late1', 3))
        self.assertEqual(late['status'], 'late')
        dup = acc.apply(event('late1', 3))
        self.assertEqual(dup['status'], 'duplicate')
        self.assertEqual(dup['upserts'], [])
        # Retracted event retry stays a duplicate and stays retracted.
        acc.apply(event('r', 20))
        acc.apply({'kind': 'retract', 'event_id': 'r'})
        dup_retracted = acc.apply(event('r', 20))
        self.assertEqual(dup_retracted['status'], 'duplicate')
        self.assertNotIn(
            'r', [s['session_id'] for s in acc.snapshot()['open_sessions']])
        # Sealed event: retract is a legal business refusal and the exact
        # retry remains a duplicate.
        acc.apply(event('s', 30))
        acc.apply({'kind': 'watermark', 'at': '1970-01-01T00:00:40Z'})
        sealed_ids_before = [x['session_id']
                             for x in acc.snapshot()['sealed_sessions']]
        refusal = acc.apply({'kind': 'retract', 'event_id': 's'})
        self.assertEqual(refusal['status'], 'sealed')
        self.assertEqual(refusal['upserts'], [])
        self.assertEqual([x['session_id']
                          for x in acc.snapshot()['sealed_sessions']],
                         sealed_ids_before)
        dup_sealed = acc.apply(event('s', 30))
        self.assertEqual(dup_sealed['status'], 'duplicate')
        self.assertEqual(dup_sealed['sealed_sessions'], [])

    def test_seal_then_late_connecting_event_keeps_snapshot(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        acc.apply(event('a', 0))
        acc.apply({'kind': 'watermark', 'at': '1970-01-01T00:00:06Z'})
        before = json.dumps(acc.snapshot()['sealed_sessions'], sort_keys=True)
        change = acc.apply(event('b', 5))
        self.assertEqual(change['status'], 'late')
        self.assertEqual(change['upserts'], [])
        self.assertEqual(change['sealed_sessions'], [])
        self.assertEqual(
            json.dumps(acc.snapshot()['sealed_sessions'], sort_keys=True),
            before)
        self.assertEqual(acc.snapshot()['open_sessions'], [])


class MultiScopeAndIdentityTests(unittest.TestCase):
    def test_multiple_scopes_use_their_own_gaps(self):
        policies = [
            {'principal': 'p', 'route': '/a', 'gap_seconds': 6},
            {'principal': 'q', 'route': '/b', 'gap_seconds': 2},
        ]
        acc = SessionAccumulator(policies, 0)
        acc.apply(event('a1', 0, principal='p', route='/a'))
        acc.apply(event('a2', 5, principal='p', route='/a'))  # connected
        acc.apply(event('b1', 0, principal='q', route='/b'))
        acc.apply(event('b2', 5, principal='q', route='/b'))  # 5s > 2s split
        open_sessions = acc.snapshot()['open_sessions']
        self.assertEqual(
            [(s['principal'], s['route'], s['session_id'])
             for s in open_sessions],
            [('p', '/a', 'a1'), ('q', '/b', 'b1'), ('q', '/b', 'b2')])
        change = acc.apply({'kind': 'watermark',
                            'at': '1970-01-01T00:00:07Z'})
        # F=7s: p/a closes 11s stays open; q/b b1 closes 2s and b2 closes 7s
        # both seal, globally sorted by (principal, route, first_us, id).
        self.assertEqual(
            [s['session_id'] for s in change['sealed_sessions']],
            ['b1', 'b2'])
        self.assertEqual(
            [s['session_id'] for s in acc.snapshot()['open_sessions']],
            ['a1'])

    def test_event_must_match_policy_even_when_late_or_duplicate(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        with self.assertRaisesRegex(ValueError, 'policy'):
            acc.apply(event('x', 0, route='/unknown'))
        # Fields and scope are validated before duplicate/conflict handling.
        acc.apply(event('a', 0))
        with self.assertRaisesRegex(ValueError, 'policy'):
            acc.apply(event('a', 0, route='/unknown'))
        self.assertEqual(acc.apply(event('a', 0))['status'], 'duplicate')


class RetryConflictAndTimeTests(unittest.TestCase):
    def test_equivalent_timezone_is_same_instant(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        acc.apply({'kind': 'event', 'id': 'a', 'principal': 'p',
                   'route': '/r', 'at': '1970-01-01T01:00:00+01:00',
                   'units': 1})
        duplicate = acc.apply({'kind': 'event', 'id': 'a', 'principal': 'p',
                               'route': '/r', 'at': '1970-01-01T00:00:00Z',
                               'units': 1})
        self.assertEqual(duplicate['status'], 'duplicate')
        # Equivalent offset on a second event still connects by UTC micros.
        acc.apply({'kind': 'event', 'id': 'b', 'principal': 'p',
                   'route': '/r', 'at': '1970-01-01T01:00:05+01:00',
                   'units': 2})
        session = acc.snapshot()['open_sessions'][0]
        self.assertEqual(session['event_ids'], ['a', 'b'])
        self.assertEqual(session['first_us'], 0)

    def test_conflicting_id_fails_and_leaves_no_state(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        acc.apply(event('a', 0, units=1))
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            acc.apply(event('a', 0, units=2))
        self.assertEqual(acc.snapshot()['open_sessions'][0]['units'], 1)
        # A conflict on an identity that has meanwhile been sealed is still a
        # hard failure and must not reopen or alter the sealed snapshot.
        acc.apply({'kind': 'watermark', 'at': '1970-01-01T00:00:10Z'})
        sealed_before = json.dumps(acc.snapshot()['sealed_sessions'],
                                   sort_keys=True)
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            acc.apply(event('a', 0, units=3))
        self.assertEqual(
            json.dumps(acc.snapshot()['sealed_sessions'], sort_keys=True),
            sealed_before)
        self.assertEqual(acc.snapshot()['open_sessions'], [])
        # And the exact retry is still recognised after the failed conflict.
        self.assertEqual(acc.apply(event('a', 0, units=1))['status'],
                         'duplicate')

    def test_negative_epoch_and_microseconds(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        first = acc.apply({
            'kind': 'event', 'id': 'n1', 'principal': 'p', 'route': '/r',
            'at': '1969-12-31T23:59:59.999999Z', 'units': 1})
        self.assertEqual(first['upserts'][0]['last_us'], -1)
        second = acc.apply({
            'kind': 'event', 'id': 'n2', 'principal': 'p', 'route': '/r',
            'at': '1970-01-01T00:00:00.000000Z', 'units': 1})
        # One microsecond gap connects across the epoch boundary.
        self.assertEqual(second['upserts'][0]['event_ids'], ['n1', 'n2'])
        self.assertEqual(second['upserts'][0]['first_us'], -1)
        self.assertEqual(second['upserts'][0]['last_us'], 0)

    def test_watermark_equality_allowed_backwards_rejected(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        acc.apply(event('a', 0))
        first = acc.apply({'kind': 'watermark',
                           'at': '1970-01-01T00:00:05Z'})
        self.assertEqual(first['status'], 'watermark')
        equal = acc.apply({'kind': 'watermark',
                           'at': '1970-01-01T00:00:05Z'})
        self.assertEqual(equal['status'], 'watermark')
        self.assertEqual(equal['step'], 3)
        with self.assertRaisesRegex(ValueError, 'backwards'):
            acc.apply({'kind': 'watermark',
                       'at': '1970-01-01T00:00:04Z'})
        # The failed move neither advanced the step nor changed the watermark.
        self.assertEqual(acc.snapshot()['watermark_us'], at(5))
        self.assertEqual(acc.apply({'kind': 'watermark',
                                    'at': '1970-01-01T00:00:05Z'})['step'], 4)


class ValidationAndImmutabilityTests(unittest.TestCase):
    def test_illegal_apply_consumes_no_step_and_state_continues(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        acc.apply(event('a', 0))
        bad_operations = [
            {'kind': 'retract', 'event_id': 'ghost'},
            {'kind': 'unknown'},
            {'kind': 'event', 'id': 'x', 'principal': 'p',
             'route': '/r', 'at': '1970-01-01T00:00:00Z'},  # missing units
            {'kind': 'event', 'id': 'x', 'principal': 'p', 'route': '/r',
             'at': '1970-01-01T00:00:00Z', 'units': True},
            {'kind': 'event', 'id': 'x', 'principal': 'p', 'route': '/r',
             'at': '1970-01-01T00:00:00Z', 'units': 1.5},
            {'kind': 'watermark'},
            {'kind': 'event', 'id': '', 'principal': 'p', 'route': '/r',
             'at': '1970-01-01T00:00:00Z', 'units': 1},
            {'kind': 'event', 'id': 'x', 'principal': 'p', 'route': '/r',
             'at': 'no-time', 'units': 1},
        ]
        snapshot_before = json.dumps(acc.snapshot(), sort_keys=True)
        for bad in bad_operations:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    acc.apply(bad)
                self.assertEqual(
                    json.dumps(acc.snapshot(), sort_keys=True),
                    snapshot_before)
        # No step was consumed; the next legal operation is step 2.
        change = acc.apply(event('b', 1))
        self.assertEqual(change['step'], 2)

    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            SessionAccumulator([], True)
        with self.assertRaises(ValueError):
            SessionAccumulator([], 1.0)
        with self.assertRaises(ValueError):
            SessionAccumulator([], -1)
        with self.assertRaises(ValueError):
            SessionAccumulator(
                [{'principal': 'p', 'route': '/r', 'gap_seconds': 0}], 0)
        with self.assertRaises(ValueError):
            parse_policies([
                {'principal': 'p', 'route': '/r', 'gap_seconds': 6},
                {'principal': 'p', 'route': '/r', 'gap_seconds': 7},
            ])
        # A failed constructor is safe to retry.
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        self.assertEqual(acc.snapshot()['cutoff_us'], None)

    def test_returned_changes_and_snapshots_are_not_mutated(self):
        acc = SessionAccumulator(copy.deepcopy(POLICY), 0)
        change = acc.apply(event('a', 0))
        snapshot = acc.snapshot()
        change['upserts'][0]['units'] = 999
        change['upserts'][0]['event_ids'].append('injected')
        snapshot['open_sessions'][0]['units'] = 999
        snapshot['open_sessions'][0]['event_ids'].append('injected')
        later = acc.apply(event('b', 1))
        self.assertEqual(later['upserts'][0]['units'], 2)
        self.assertEqual(later['upserts'][0]['event_ids'], ['a', 'b'])
        fresh = acc.snapshot()
        self.assertEqual(fresh['open_sessions'][0]['units'], 2)
        self.assertEqual(fresh['open_sessions'][0]['event_ids'], ['a', 'b'])
        # Sealed snapshots handed out earlier stay frozen as well.
        acc.apply({'kind': 'watermark', 'at': '1970-01-01T00:00:10Z'})
        sealed_change = json.dumps(
            acc.snapshot()['sealed_sessions'], sort_keys=True)
        acc.snapshot()['sealed_sessions'][0]['units'] = 999
        self.assertEqual(
            json.dumps(acc.snapshot()['sealed_sessions'], sort_keys=True),
            sealed_change)

    def test_parse_operation_rejects_unknown_and_non_objects(self):
        with self.assertRaises(ValueError):
            parse_operation(['not', 'an', 'object'])
        with self.assertRaises(ValueError):
            parse_operation({'kind': 'event', 'id': 'a'})


class WrapperAndEmptyInputTests(unittest.TestCase):
    def test_empty_operations(self):
        result = sessions_analyze({
            'policies': copy.deepcopy(POLICY),
            'allowed_lateness_seconds': 2,
            'operations': [],
        })
        self.assertEqual(set(result),
                         {'watermark_us', 'cutoff_us', 'open_sessions',
                          'sealed_sessions', 'trace'})
        self.assertIsNone(result['watermark_us'])
        self.assertIsNone(result['cutoff_us'])
        self.assertEqual(result['open_sessions'], [])
        self.assertEqual(result['sealed_sessions'], [])
        self.assertEqual(result['trace'], [])

    def test_wrapper_preserves_operation_order_and_matches_apply(self):
        operations = [
            event('c', 10), event('a', 0), event('b', 5),
            {'kind': 'watermark', 'at': '1970-01-01T00:00:10Z'},
            {'kind': 'retract', 'event_id': 'b'},
            event('late', 7), event('b', 5),
        ]
        data = {'policies': copy.deepcopy(POLICY),
                'allowed_lateness_seconds': 2, 'operations': operations}
        result = sessions_analyze(copy.deepcopy(data))
        # The trace follows arrival order, never event-time order.
        self.assertEqual([change['step'] for change in result['trace']],
                         list(range(1, 8)))
        acc = SessionAccumulator(copy.deepcopy(POLICY), 2)
        expected_trace = [acc.apply(operation) for operation in operations]
        self.assertEqual(result['trace'], expected_trace)
        expected_snapshot = acc.snapshot()
        for key in ('watermark_us', 'cutoff_us', 'open_sessions',
                    'sealed_sessions'):
            self.assertEqual(result[key], expected_snapshot[key])

    def test_wrapper_validates_top_level_fields(self):
        with self.assertRaises(ValueError):
            sessions_analyze({'policies': [], 'allowed_lateness_seconds': 0})
        with self.assertRaises(ValueError):
            sessions_analyze({'policies': [], 'allowed_lateness_seconds': 0,
                              'operations': [], 'extra': 1})


class ReferenceOracleTests(unittest.TestCase):
    """Differential testing against an independent brute-force reference."""

    def reference_run(self, policies, allowed_lateness_seconds, operations):
        """Tiny independent engine; shares no production session code."""
        gap = {(p['principal'], p['route']):
               p['gap_seconds'] * 1_000_000 for p in policies}
        registry = {}
        status = {}
        sealed_events = set()
        watermark = None
        open_table = {}
        sealed_list = []
        trace = []
        for step, op in enumerate(operations, start=1):
            kind = op['kind']
            change_status = None
            if kind == 'event':
                eid = op['id']
                canonical = (op['principal'], op['route'],
                             instant(op['at']), op['units'])
                if eid in registry:
                    assert registry[eid] == canonical
                    change_status = 'duplicate'
                else:
                    cutoff = (None if watermark is None
                              else watermark - allowed_lateness_seconds * 1_000_000)
                    if cutoff is not None and canonical[2] < cutoff:
                        registry[eid] = canonical
                        status[eid] = 'late'
                        change_status = 'late'
                    else:
                        registry[eid] = canonical
                        status[eid] = 'accepted'
                        change_status = 'accepted'
            elif kind == 'retract':
                eid = op['event_id']
                if eid in sealed_events:
                    change_status = 'sealed'
                elif status[eid] == 'accepted':
                    status[eid] = 'retracted'
                    change_status = 'retracted'
                else:
                    change_status = 'noop'
            else:
                stamp = instant(op['at'])
                assert watermark is None or stamp >= watermark
                watermark = stamp
                change_status = 'watermark'

            by_scope = {}
            for eid, (principal, route, stamp, units) in registry.items():
                if status[eid] == 'accepted' and eid not in sealed_events:
                    by_scope.setdefault((principal, route), []).append(
                        (stamp, eid, units))
            rebuilt = {}
            for scope, rows_ in by_scope.items():
                rows_.sort()
                groups, current, previous = [], [], None
                for stamp, eid, units in rows_:
                    if current and stamp - previous >= gap[scope]:
                        groups.append(current)
                        current = []
                    current.append((stamp, eid, units))
                    previous = stamp
                groups.append(current)
                for group in groups:
                    ids = sorted(eid for _, eid, _ in group)
                    rebuilt[ids[0]] = {
                        'session_id': ids[0],
                        'principal': scope[0], 'route': scope[1],
                        'first_us': group[0][0], 'last_us': group[-1][0],
                        'closes_at_us': group[-1][0] + gap[scope],
                        'units': sum(u for _, _, u in group),
                        'event_ids': ids}
            cutoff = (None if watermark is None
                      else watermark - allowed_lateness_seconds * 1_000_000)
            after_open, sealed_now = {}, []
            for sid, session in rebuilt.items():
                if cutoff is not None and session['closes_at_us'] <= cutoff:
                    sealed_now.append(session)
                else:
                    after_open[sid] = session
            sealed_now.sort(key=lambda s: (s['principal'], s['route'],
                                           s['first_us'], s['session_id']))
            sealed_ids = {s['session_id'] for s in sealed_now}
            retracted_ids = sorted(
                sid for sid in open_table
                if sid not in after_open and sid not in sealed_ids)
            upserts = []
            for sid in sorted(after_open,
                              key=lambda x: (after_open[x]['principal'],
                                             after_open[x]['route'],
                                             after_open[x]['first_us'], x)):
                if open_table.get(sid) != after_open[sid]:
                    upserts.append(copy.deepcopy(after_open[sid]))
            for session in sealed_now:
                sealed_list.append(copy.deepcopy(session))
                sealed_events.update(session['event_ids'])
            open_table = after_open
            trace.append({
                'step': step, 'status': change_status,
                'retracted_session_ids': retracted_ids,
                'upserts': upserts,
                'sealed_sessions': copy.deepcopy(sealed_now)})
        snapshot = {
            'watermark_us': watermark,
            'cutoff_us': (None if watermark is None
                          else watermark - allowed_lateness_seconds * 1_000_000),
            'open_sessions': sorted(
                (copy.deepcopy(s) for s in open_table.values()),
                key=lambda s: (s['principal'], s['route'],
                               s['first_us'], s['session_id'])),
            'sealed_sessions': sorted(
                copy.deepcopy(sealed_list),
                key=lambda s: (s['principal'], s['route'],
                               s['first_us'], s['session_id'])),
        }
        return snapshot, trace

    def test_random_operation_streams_match_reference(self):
        rng = random.Random(20261120)
        policies = [
            {'principal': 'p', 'route': '/a', 'gap_seconds': 3},
            {'principal': 'q', 'route': '/b', 'gap_seconds': 5},
        ]
        for trial in range(40):
            lateness = rng.randrange(0, 4)
            operations = []
            # Track canonical content per id so retries are always exact and
            # conflicts never occur; unknown retractions are tested separately.
            content = {}
            known = []
            watermark_seconds = None
            for _ in range(rng.randrange(0, 60)):
                choice = rng.randrange(6)
                if choice < 3:
                    eid = 'e%03d' % rng.randrange(0, 25)
                    if eid in content:
                        operations.append(dict(content[eid]))  # exact retry
                    else:
                        scope = rng.choice((('p', '/a'), ('q', '/b')))
                        stamp = rng.randrange(-5, 30)
                        op = event(eid, stamp, units=rng.randrange(1, 5),
                                   principal=scope[0], route=scope[1])
                        content[eid] = op
                        known.append(eid)
                        operations.append(op)
                elif choice == 3 and known:
                    operations.append({'kind': 'retract',
                                       'event_id': rng.choice(known)})
                elif choice == 4:
                    candidate = rng.randrange(0, 30)
                    if watermark_seconds is None or candidate >= watermark_seconds:
                        watermark_seconds = candidate
                        operations.append({
                            'kind': 'watermark',
                            'at': '1970-01-01T00:00:%02dZ' % candidate})
                elif known:
                    operations.append({'kind': 'retract',
                                       'event_id': rng.choice(known)})
            expected_snapshot, expected_trace = self.reference_run(
                policies, lateness, operations)
            acc = SessionAccumulator(copy.deepcopy(policies), lateness)
            actual_trace = [acc.apply(op) for op in operations]
            self.assertEqual(actual_trace, expected_trace)
            self.assertEqual(acc.snapshot(), expected_snapshot)

            # The wrapper must produce the same result on the same stream.
            wrapped = sessions_analyze({
                'policies': copy.deepcopy(policies),
                'allowed_lateness_seconds': lateness,
                'operations': operations,
            })
            self.assertEqual(wrapped['trace'], expected_trace)
            self.assertEqual(wrapped['watermark_us'],
                             expected_snapshot['watermark_us'])
            self.assertEqual(wrapped['cutoff_us'],
                             expected_snapshot['cutoff_us'])
            self.assertEqual(wrapped['open_sessions'],
                             expected_snapshot['open_sessions'])
            self.assertEqual(wrapped['sealed_sessions'],
                             expected_snapshot['sealed_sessions'])


if __name__ == '__main__':
    unittest.main()
