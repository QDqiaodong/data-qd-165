import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from api_quota_audit import warehouse
from api_quota_audit.model import analyze, instant

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'api_quota_audit.warehouse'


def us(value):
    return instant(value)


def event(event_id, revision, principal, route, at, units):
    return {'event_id': event_id, 'revision': revision, 'deleted': False,
            'principal': principal, 'route': route, 'at': at, 'units': units}


def retire(event_id, revision):
    return {'event_id': event_id, 'revision': revision, 'deleted': True}


def policy(policy_id, revision, principal, route, valid_from, valid_to,
           window_seconds, limit):
    return {'policy_id': policy_id, 'revision': revision, 'deleted': False,
            'principal': principal, 'route': route, 'valid_from': valid_from,
            'valid_to': valid_to, 'window_seconds': window_seconds,
            'limit': limit}


def retire_policy(policy_id, revision):
    return {'policy_id': policy_id, 'revision': revision, 'deleted': True}


class WarehouseCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)
        self.db = self.directory / 'warehouse.db'
        warehouse.create_database(self.db)
        self._source_index = 0

    def source(self, data, name=None):
        self._source_index += 1
        path = self.directory / (name or ('batch-%d.json' % self._source_index))
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def ingest(self, batch_id, events=(), policies=(), db=None):
        return warehouse.ingest_batch(
            db or self.db,
            self.source({'batch_id': batch_id,
                         'event_revisions': list(events),
                         'policy_revisions': list(policies)}))

    def raw_ingest(self, data, db=None):
        return warehouse.ingest_batch(db or self.db, self.source(data))

    def query(self, max_seq, before, db=None):
        output = self.directory / ('report-%d-%s.json' % (max_seq, self._source_index))
        return warehouse.query_warehouse(db or self.db, max_seq, before, output)

    def state(self, db=None):
        """Raw durable state used to prove a failed batch left no trace."""
        path = db or self.db
        connection = sqlite3.connect(os.fspath(path))
        try:
            return {
                'batches': connection.execute(
                    'SELECT seq, batch_id, canonical, receipt FROM batches '
                    'ORDER BY seq').fetchall(),
                'events': connection.execute(
                    'SELECT event_id, revision, seq, deleted, principal, route, '
                    'at_us, units FROM event_versions ORDER BY event_id, revision'
                ).fetchall(),
                'policies': connection.execute(
                    'SELECT policy_id, revision, seq, deleted, principal, route, '
                    'valid_from_us, valid_to_us, window_seconds, limit_units '
                    'FROM policy_versions ORDER BY policy_id, revision').fetchall(),
            }
        finally:
            connection.close()


class HistoryTests(WarehouseCase):
    """Fixed hand-computed expectations, seq by seq."""

    def setUp(self):
        super().setUp()
        # seq1: two adjacent policy versions for the same scope and two
        # events, one in each effective interval.
        self.first = self.ingest(
            'b1',
            policies=[policy('P1', 1, 'a', '/x', '2026-01-01T00:00:00Z',
                             '2026-01-01T01:00:00Z', 3600, 3),
                      policy('P2', 1, 'a', '/x', '2026-01-01T01:00:00Z',
                             None, 3600, 5)],
            events=[event('E1', 1, 'a', '/x', '2026-01-01T00:30:00Z', 2),
                    event('E2', 1, 'a', '/x', '2026-01-01T01:30:00Z', 4)])
        # seq2: correct only the policy threshold and window length; events
        # keep the exact same ids, revisions and timestamps.
        self.second = self.ingest(
            'b2',
            policies=[policy('P2', 2, 'a', '/x', '2026-01-01T01:00:00Z',
                             None, 7200, 3)])

    def test_receipts_are_consecutive_and_count_rows(self):
        self.assertEqual(self.first, {'batch_id': 'b1', 'seq': 1,
                                      'event_revision_count': 2,
                                      'policy_revision_count': 2})
        self.assertEqual(self.second, {'batch_id': 'b2', 'seq': 2,
                                       'event_revision_count': 0,
                                       'policy_revision_count': 1})

    def test_seq1_hand_computed_report(self):
        report = self.query(1, '2026-01-01T02:00:00Z')
        self.assertEqual(report, {
            'schema_version': 1,
            'max_seq': 1,
            'event_before_us': us('2026-01-01T02:00:00Z'),
            'windows': [
                {'principal': 'a', 'route': '/x', 'policy_id': 'P1',
                 'policy_revision': 1,
                 'window_start_utc': '2026-01-01T00:00:00Z',
                 'window_end_utc': '2026-01-01T01:00:00Z',
                 'limit': 3, 'used': 2, 'over_by': 0, 'exceeded': False,
                 'event_versions': [{'event_id': 'E1', 'revision': 1}]},
                {'principal': 'a', 'route': '/x', 'policy_id': 'P2',
                 'policy_revision': 1,
                 'window_start_utc': '2026-01-01T01:00:00Z',
                 'window_end_utc': '2026-01-01T02:00:00Z',
                 'limit': 5, 'used': 4, 'over_by': 0, 'exceeded': False,
                 'event_versions': [{'event_id': 'E2', 'revision': 1}]},
            ],
            'window_count': 2, 'exceeded_count': 0,
            'selected_event_count': 2,
        })

    def test_seq2_reattributes_and_recomputes_without_changing_seq1(self):
        before = self.query(1, '2026-01-01T02:00:00Z')
        report = self.query(2, '2026-01-01T02:00:00Z')
        # E2 still falls in P2's interval but the revised 2-hour window is
        # epoch anchored to 00:00 and the revised limit is 3.  P2 revision 2
        # is a distinct attribution key from P1 revision 1 even though both
        # cover the same epoch window start.
        self.assertEqual([item['policy_id'] for item in report['windows']],
                         ['P1', 'P2'])
        self.assertEqual([item['policy_revision'] for item in report['windows']],
                         [1, 2])
        self.assertEqual([item['window_start_utc'] for item in report['windows']],
                         ['2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'])
        e2 = report['windows'][1]
        self.assertEqual((e2['used'], e2['limit'], e2['over_by'], e2['exceeded']),
                         (4, 3, 1, True))
        self.assertEqual(report['exceeded_count'], 1)
        # The old knowledge snapshot is byte-for-byte stable.
        self.assertEqual(self.query(1, '2026-01-01T02:00:00Z'), before)

    def test_withdrawal_restore_and_old_snapshots(self):
        expected_seq2 = self.query(2, '2026-01-01T02:00:00Z')
        self.ingest('b3', events=[retire('E2', 2)],
                    policies=[retire_policy('P2', 3)])
        # seq3: E2 is withdrawn; P1 stays and E1 keeps its P1 attribution.
        report = self.query(3, '2026-01-01T02:00:00Z')
        self.assertEqual(report['selected_event_count'], 1)
        self.assertEqual([item['event_versions'] for item in report['windows']],
                         [[{'event_id': 'E1', 'revision': 1}]])
        # seq4 restores both identities with the next full versions.
        self.ingest('b4',
                    events=[event('E2', 3, 'a', '/x', '2026-01-01T00:45:00Z', 3)],
                    policies=[policy('P2', 4, 'a', '/x', '2026-01-01T01:00:00Z',
                                     None, 3600, 5)])
        report = self.query(4, '2026-01-01T02:00:00Z')
        self.assertEqual(report['selected_event_count'], 2)
        p1 = next(item for item in report['windows'] if item['policy_id'] == 'P1')
        self.assertEqual((p1['used'], p1['over_by'], p1['exceeded']), (5, 2, True))
        self.assertEqual(p1['event_versions'],
                         [{'event_id': 'E1', 'revision': 1},
                          {'event_id': 'E2', 'revision': 3}])
        # seq2 remains exactly as before the withdrawal batch.
        self.assertEqual(self.query(2, '2026-01-01T02:00:00Z'), expected_seq2)
        # Revisions were not reused and the restored events are visible only
        # from their own knowledge snapshot onward.
        self.assertEqual(self.query(2, '2026-01-01T02:00:00Z')
                         ['windows'][1]['event_versions'],
                         [{'event_id': 'E2', 'revision': 1}])

    def test_event_time_cutoff_never_falls_back_to_older_revision(self):
        # seq1 knows E1 at 00:30; seq2 moves E2 and adds no new E1 revision.
        # Build the acceptance move: E1 revision 2 goes to /y at 01:30.
        self.ingest('move',
                    events=[event('E1', 2, 'a', '/y', '2026-01-01T01:30:00Z', 2)],
                    policies=[policy('Q1', 1, 'a', '/y',
                                     '2026-01-01T01:00:00Z', None, 3600, 5)])
        old = self.query(2, '2026-01-01T02:00:00Z')
        self.assertIn({'event_id': 'E1', 'revision': 1},
                      [item for window in old['windows']
                       for item in window['event_versions']])
        # Event-time cutoff before the moved head must hide E1 entirely even
        # though the older revision 1 was before the cutoff.
        new = self.query(3, '2026-01-01T01:00:00Z')
        self.assertEqual(new['windows'], [])
        self.assertEqual(new['selected_event_count'], 0)
        late = self.query(3, '2026-01-01T02:00:00Z')
        moved = next(item for window in late['windows']
                     for item in window['event_versions']
                     if item['event_id'] == 'E1')
        self.assertEqual(moved['revision'], 2)
        # The old /x window lost E1; only E2's own window remains there.
        old_x = [window for window in late['windows'] if window['route'] == '/x']
        self.assertEqual([[item['event_id'] for item in window['event_versions']]
                          for window in old_x], [['E2']])

    def test_seq_zero_is_empty_and_max_seq_is_bounded(self):
        for cutoff in ('2026-01-01T02:00:00Z', '2000-01-01T00:00:00Z'):
            report = self.query(0, cutoff)
            self.assertEqual(report['windows'], [])
            self.assertEqual(report['window_count'], 0)
            self.assertEqual(report['selected_event_count'], 0)
        with self.assertRaises(ValueError):
            self.query(3, '2026-01-01T02:00:00Z')
        with self.assertRaises(ValueError):
            self.query(-1, '2026-01-01T02:00:00Z')

    def test_event_before_is_strict_and_utc_normalized(self):
        # Strictly before the event instant: E1 is not selected.
        boundary = self.query(1, '2026-01-01T00:30:00Z')
        self.assertEqual(boundary['windows'], [])
        boundary = self.query(1, '2026-01-01T00:30:00.000001Z')
        self.assertEqual([item['event_versions'] for item in boundary['windows']],
                         [[{'event_id': 'E1', 'revision': 1}]])
        shifted = self.query(1, '2026-01-01T01:30:00.000001+01:00')
        self.assertEqual(shifted['event_before_us'],
                         us('2026-01-01T00:30:00.000001Z'))
        self.assertEqual(shifted['windows'], boundary['windows'])


class PolicyWindowTests(WarehouseCase):
    def test_adjacent_intervals_touch_without_overlap(self):
        receipt = self.ingest(
            'adjacent',
            policies=[policy('P1', 1, 'a', '/x', '2026-01-01T00:00:00Z',
                             '2026-01-01T01:00:00Z', 3600, 5),
                      policy('P2', 1, 'a', '/x', '2026-01-01T01:00:00Z',
                             None, 3600, 5)],
            events=[event('E1', 1, 'a', '/x', '2026-01-01T00:59:59.999999Z', 1),
                    event('E2', 1, 'a', '/x', '2026-01-01T01:00:00Z', 1)])
        self.assertEqual(receipt['seq'], 1)
        report = self.query(1, '2026-01-01T02:00:00Z')
        self.assertEqual([(item['policy_id'], item['window_start_utc'])
                          for item in report['windows']],
                         [('P1', '2026-01-01T00:00:00Z'),
                          ('P2', '2026-01-01T01:00:00Z')])

    def test_overlapping_intervals_rejected_without_new_state(self):
        self.ingest('base',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 3600, 5)],
                    events=[event('E1', 1, 'a', '/x',
                                  '2026-01-01T00:30:00Z', 1)])
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            self.ingest('overlap',
                        policies=[policy('P2', 1, 'a', '/x',
                                          '2026-01-01T00:15:00Z', None, 3600, 5)])
        self.assertEqual(self.state(), before)

    def test_gap_in_policy_coverage_rejected(self):
        self.ingest('base',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 3600, 5)],
                    events=[event('E1', 1, 'a', '/x',
                                  '2026-01-01T00:30:00Z', 1)])
        before = self.state()
        # A corrected interval that starts after the event's instant leaves a
        # hole; linked event/policy correction fails as one batch.
        with self.assertRaisesRegex(ValueError, 'no unique matching policy'):
            self.ingest('gap',
                        policies=[policy('P1', 2, 'a', '/x',
                                         '2026-01-01T01:00:00Z', None, 3600, 5)])
        self.assertEqual(self.state(), before)
        with self.assertRaises(ValueError):
            self.query(2, '2026-01-01T02:00:00Z')

    def test_scope_move_and_policy_replacement_are_linked(self):
        self.ingest('base',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 3600, 2)],
                    events=[event('E1', 1, 'a', '/x',
                                  '2026-01-01T00:30:00Z', 3)])
        # Moving the event and withdrawing the only /x policy in the same
        # batch is legal: only the final state is validated.
        self.ingest('move',
                    events=[event('E1', 2, 'b', '/y',
                                  '2026-01-01T00:30:00Z', 3)],
                    policies=[retire_policy('P1', 2),
                              policy('Q1', 1, 'b', '/y',
                                     '2026-01-01T00:00:00Z', None, 3600, 5)])
        old = self.query(1, '2026-01-01T02:00:00Z')
        self.assertEqual(old['windows'][0]['policy_id'], 'P1')
        new = self.query(2, '2026-01-01T02:00:00Z')
        self.assertEqual(new['windows'][0]['principal'], 'b')
        self.assertEqual(new['windows'][0]['policy_id'], 'Q1')

    def test_withdrawing_needed_policy_rejected_but_allowed_with_event(self):
        self.ingest('base',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 3600, 5)],
                    events=[event('E1', 1, 'a', '/x',
                                  '2026-01-01T00:30:00Z', 1)])
        before = self.state()
        with self.assertRaises(ValueError):
            self.ingest('bad', policies=[retire_policy('P1', 2)])
        self.assertEqual(self.state(), before)
        self.ingest('good', events=[retire('E1', 2)],
                    policies=[retire_policy('P1', 2)])
        self.assertEqual(self.query(2, '2026-01-01T02:00:00Z')['windows'], [])

    def test_policy_may_precede_events(self):
        self.ingest('policy-first',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 3600, 5)])
        self.assertEqual(self.query(1, '2026-01-01T02:00:00Z')['windows'], [])
        self.ingest('events-later',
                    events=[event('E1', 1, 'a', '/x',
                                  '2026-01-01T00:30:00Z', 1)])
        report = self.query(2, '2026-01-01T02:00:00Z')
        self.assertEqual(report['selected_event_count'], 1)
        self.assertEqual(report['windows'][0]['policy_id'], 'P1')

    def test_event_without_policy_rejected(self):
        before = self.state()
        with self.assertRaises(ValueError):
            self.ingest('orphan',
                        events=[event('E1', 1, 'a', '/missing',
                                      '2026-01-01T00:30:00Z', 1)])
        self.assertEqual(self.state(), before)

    def test_policy_revision_can_reanchor_window(self):
        self.ingest('base',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 3600, 5)],
                    events=[event('E1', 1, 'a', '/x',
                                  '2026-01-01T00:10:00Z', 1),
                            event('E2', 1, 'a', '/x',
                                  '2026-01-01T01:50:00Z', 1)])
        first = self.query(1, '2026-01-01T03:00:00Z')
        self.assertEqual(first['window_count'], 2)
        self.ingest('widen',
                    policies=[policy('P1', 2, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 7200, 5)])
        second = self.query(2, '2026-01-01T03:00:00Z')
        self.assertEqual(second['window_count'], 1)
        self.assertEqual(second['windows'][0]['window_start_utc'],
                         '2026-01-01T00:00:00Z')
        self.assertEqual(second['windows'][0]['policy_revision'], 2)
        self.assertEqual(second['windows'][0]['used'], 2)
        self.assertEqual(self.query(1, '2026-01-01T03:00:00Z'), first)


class TimeTests(WarehouseCase):
    def test_negative_epoch_floor_window_and_microseconds(self):
        self.ingest('negative',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '1969-12-31T23:00:00Z', None, 60, 5)],
                    events=[event('E1', 1, 'a', '/x',
                                  '1969-12-31T23:59:59.999999Z', 1),
                            event('E2', 1, 'a', '/x',
                                  '1970-01-01T00:00:00Z', 1)])
        report = self.query(1, '1970-01-01T01:00:00Z')
        self.assertEqual([(item['window_start_utc'], item['window_end_utc'])
                          for item in report['windows']],
                         [('1969-12-31T23:59:00Z', '1970-01-01T00:00:00Z'),
                          ('1970-01-01T00:00:00Z', '1970-01-01T00:01:00Z')])
        self.assertEqual([item['used'] for item in report['windows']], [1, 1])

    def test_microsecond_boundary_is_not_truncated(self):
        self.ingest('micro',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 3600, 5)],
                    events=[event('E1', 1, 'a', '/x',
                                  '2026-01-01T00:59:59.999999Z', 1)])
        self.assertEqual(self.query(1, '2026-01-01T00:59:59.999999Z')['windows'], [])
        report = self.query(1, '2026-01-01T01:00:00Z')
        self.assertEqual(report['windows'][0]['window_start_utc'],
                         '2026-01-01T00:00:00Z')

    def test_shared_aggregation_matches_business_model(self):
        # Same facts through the standalone audit model and the warehouse,
        # for events that fit one exact policy per scope.
        data = {
            'policies': [{'id': 'p', 'principal': 'a', 'route': '/x',
                          'window_seconds': 60, 'limit': 5}],
            'events': [{'id': 'e1', 'principal': 'a', 'route': '/x',
                        'at': '1969-12-31T23:59:59.999999Z', 'units': 3},
                       {'id': 'e2', 'principal': 'a', 'route': '/x',
                        'at': '1970-01-01T00:00:30Z', 'units': 4}],
        }
        expected = analyze(data)
        self.ingest('shared',
                    policies=[policy('p', 1, 'a', '/x',
                                     '1969-01-01T00:00:00Z', None, 60, 5)],
                    events=[event('e1', 1, 'a', '/x',
                                  '1969-12-31T23:59:59.999999Z', 3),
                            event('e2', 1, 'a', '/x',
                                  '1970-01-01T00:00:30Z', 4)])
        report = self.query(1, '1970-01-01T01:00:00Z')
        self.assertEqual(report['window_count'], expected['window_count'])
        self.assertEqual(report['exceeded_count'], expected['exceeded_count'])
        for actual, wanted in zip(report['windows'], expected['windows']):
            self.assertEqual(actual['used'], wanted['used'])
            self.assertEqual(actual['limit'], wanted['limit'])
            self.assertEqual(actual['over_by'], wanted['over_by'])
            self.assertEqual(actual['exceeded'], wanted['exceeded'])
            self.assertEqual(actual['window_start_utc'], wanted['window_start_utc'])
            self.assertEqual(actual['window_end_utc'], wanted['window_end_utc'])


class FailureTests(WarehouseCase):
    def test_failed_batch_does_not_consume_a_sequence_number(self):
        self.ingest('base',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 3600, 5)],
                    events=[event('E1', 1, 'a', '/x',
                                  '2026-01-01T00:30:00Z', 1)])
        bad = self.directory / 'bad.json'
        bad.write_text(json.dumps({
            'batch_id': 'bad',
            'event_revisions': [],
            'policy_revisions': [policy('P2', 1, 'a', '/x',
                                        '2026-01-01T00:15:00Z', None, 3600, 5)]}))
        for _ in range(2):
            with self.assertRaises(ValueError):
                warehouse.ingest_batch(self.db, bad)
        receipt = self.ingest('next',
                              events=[event('E2', 1, 'a', '/x',
                                            '2026-01-01T00:40:00Z', 1)])
        self.assertEqual(receipt['seq'], 2)
        self.assertEqual([row[0] for row in self.state()['batches']], [1, 2])

    def test_every_old_snapshot_survives_a_rejected_batch(self):
        self.ingest('b1',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 3600, 5)],
                    events=[event('E1', 1, 'a', '/x',
                                  '2026-01-01T00:30:00Z', 1)])
        self.ingest('b2',
                    events=[event('E1', 2, 'a', '/x',
                                  '2026-01-01T00:35:00Z', 2)])
        before = {seq: self.query(seq, '2026-01-01T02:00:00Z') for seq in (0, 1, 2)}
        state = self.state()
        with self.assertRaises(ValueError):
            self.ingest('bad',
                        events=[event('E1', 4, 'a', '/x',
                                      '2026-01-01T00:45:00Z', 1)])
        self.assertEqual(self.state(), state)
        after = {seq: self.query(seq, '2026-01-01T02:00:00Z') for seq in (0, 1, 2)}
        self.assertEqual(after, before)


class RevisionTests(WarehouseCase):
    def test_same_identity_multiple_contiguous_revisions_in_one_batch(self):
        receipt = self.ingest(
            'multi',
            policies=[policy('P1', 1, 'a', '/x', '2026-01-01T00:00:00Z',
                             None, 3600, 5),
                      policy('P1', 2, 'a', '/x', '2026-01-01T00:00:00Z',
                             None, 3600, 4)],
            events=[event('E1', 1, 'a', '/x', '2026-01-01T00:30:00Z', 1),
                    event('E2', 1, 'a', '/x', '2026-01-01T00:40:00Z', 2)])
        self.assertEqual((receipt['seq'], receipt['event_revision_count'],
                          receipt['policy_revision_count']), (1, 2, 2))
        self.assertEqual(self.query(1, '2026-01-01T02:00:00Z')['windows'][0]
                         ['policy_revision'], 2)

    def test_revision_gaps_and_first_withdrawal_rejected(self):
        bad_batches = [
            {'event_revisions': [event('E1', 2, 'a', '/x',
                                       '2026-01-01T00:30:00Z', 1)]},
            {'event_revisions': [retire('E1', 1)]},
            {'policy_revisions': [policy('P1', 3, 'a', '/x',
                                         '2026-01-01T00:00:00Z', None, 3600, 5)]},
        ]
        for payload in bad_batches:
            with self.subTest(payload=payload):
                data = dict({'batch_id': 'b', 'event_revisions': [],
                             'policy_revisions': []}, **payload)
                before = self.state()
                with self.assertRaises(ValueError):
                    self.raw_ingest(data)
                self.assertEqual(self.state(), before)

    def test_used_revision_or_duplicate_identity_rejected(self):
        self.ingest('base',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 3600, 5)],
                    events=[event('E1', 1, 'a', '/x',
                                  '2026-01-01T00:30:00Z', 1)])
        before = self.state()
        # Reusing a revision is rejected even when the content is identical.
        with self.assertRaises(ValueError):
            self.ingest('reuse', events=[event('E1', 1, 'a', '/x',
                                               '2026-01-01T00:30:00Z', 1)])
        # Duplicate (id, revision) inside one batch is rejected as well.
        with self.assertRaises(ValueError):
            self.ingest('duplicate',
                        events=[event('E1', 2, 'a', '/x',
                                      '2026-01-01T00:30:00Z', 1),
                                event('E1', 2, 'a', '/x',
                                      '2026-01-01T00:30:00Z', 1)])
        self.assertEqual(self.state(), before)

    def test_double_withdrawal_rejected_and_restore_requires_full_version(self):
        self.ingest('base',
                    policies=[policy('P1', 1, 'a', '/x',
                                     '2026-01-01T00:00:00Z', None, 3600, 5)],
                    events=[event('E1', 1, 'a', '/x',
                                  '2026-01-01T00:30:00Z', 1)])
        self.ingest('withdraw', events=[retire('E1', 2)])
        before = self.state()
        with self.assertRaises(ValueError):
            self.ingest('withdraw-again', events=[retire('E1', 3)])
        self.assertEqual(self.state(), before)
        receipt = self.ingest('restore',
                              events=[event('E1', 3, 'a', '/x',
                                            '2026-01-01T00:35:00Z', 2)])
        self.assertEqual(receipt['seq'], 3)
        self.assertEqual(self.query(3, '2026-01-01T02:00:00Z')['windows'][0]
                         ['used'], 2)

    def test_event_and_policy_identity_namespaces_are_separate(self):
        receipt = self.ingest(
            'same-name',
            policies=[policy('X', 1, 'a', '/x', '2026-01-01T00:00:00Z',
                             None, 3600, 5)],
            events=[event('X', 1, 'a', '/x', '2026-01-01T00:30:00Z', 1)])
        self.assertEqual(receipt['seq'], 1)
        report = self.query(1, '2026-01-01T02:00:00Z')
        self.assertEqual(report['windows'][0]['policy_id'], 'X')
        self.assertEqual(report['windows'][0]['event_versions'],
                         [{'event_id': 'X', 'revision': 1}])


class BatchIdentityTests(WarehouseCase):
    def document(self, batch_id='b1', events=None, policies=None):
        return self.source({
            'batch_id': batch_id,
            'event_revisions': events if events is not None else [
                event('E1', 1, 'a', '/x', '2026-01-01T00:30:00Z', 1)],
            'policy_revisions': policies if policies is not None else [
                policy('P1', 1, 'a', '/x', '2026-01-01T00:00:00Z',
                       None, 3600, 5)],
        })

    def test_empty_batch_takes_a_sequence_number(self):
        receipt = self.ingest('empty')
        self.assertEqual(receipt, {'batch_id': 'empty', 'seq': 1,
                                   'event_revision_count': 0,
                                   'policy_revision_count': 0})
        self.assertEqual(self.ingest('empty')['seq'], 1)
        self.assertEqual(self.ingest('empty-two')['seq'], 2)
        self.assertEqual(self.query(2, '2026-01-01T02:00:00Z')['windows'], [])

    def test_reordered_and_timezone_equivalent_retry_is_idempotent(self):
        first = warehouse.ingest_batch(self.db, self.document(
            events=[event('E1', 1, 'a', '/x', '2026-01-01T00:30:00Z', 1),
                    event('E2', 1, 'a', '/x', '2026-01-01T00:45:00Z', 1)],
            policies=[policy('P1', 1, 'a', '/x', '2026-01-01T00:00:00Z',
                             None, 3600, 5)]))
        retried = self.document(
            events=[event('E2', 1, 'a', '/x', '2026-01-01T01:45:00+01:00', 1),
                    event('E1', 1, 'a', '/x', '2026-01-01T01:30:00+01:00', 1)],
            policies=[policy('P1', 1, 'a', '/x', '2026-01-01T01:00:00+01:00',
                             None, 3600, 5)])
        self.assertEqual(warehouse.ingest_batch(self.db, retried), first)
        self.assertEqual(len(self.state()['batches']), 1)
        # The retry still succeeds after newer knowledge was committed.
        self.ingest('b2', events=[retire('E1', 2)])
        self.assertEqual(warehouse.ingest_batch(self.db, retried), first)
        self.assertEqual(len(self.state()['batches']), 2)

    def test_conflicting_batch_id_rejected(self):
        first = self.ingest('b1', events=[event('E1', 1, 'a', '/x',
                                                '2026-01-01T00:30:00Z', 1)],
                            policies=[policy('P1', 1, 'a', '/x',
                                             '2026-01-01T00:00:00Z', None,
                                             3600, 5)])
        before = self.state()
        with self.assertRaises(ValueError):
            self.ingest('b1', events=[event('E1', 1, 'a', '/x',
                                            '2026-01-01T00:30:00Z', 2)],
                        policies=[policy('P1', 1, 'a', '/x',
                                         '2026-01-01T00:00:00Z', None, 3600, 5)])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.query(1, '2026-01-01T02:00:00Z')['windows'][0]
                         ['used'], 1)
        self.assertEqual(first['seq'], 1)

    def test_strict_input_validation(self):
        good_event = event('E1', 1, 'a', '/x', '2026-01-01T00:30:00Z', 1)
        good_policy = policy('P1', 1, 'a', '/x', '2026-01-01T00:00:00Z',
                             None, 3600, 5)
        bad = [
            '[]',
            '{}',
            {'batch_id': 'b', 'event_revisions': [], 'policy_revisions': [],
             'extra': 1},
            {'batch_id': '', 'event_revisions': [], 'policy_revisions': []},
            {'batch_id': 'b', 'event_revisions': {}, 'policy_revisions': []},
            {'batch_id': 'b', 'event_revisions': [dict(good_event, units=True)],
             'policy_revisions': []},
            {'batch_id': 'b', 'event_revisions': [dict(good_event, units=1.5)],
             'policy_revisions': []},
            {'batch_id': 'b', 'event_revisions': [dict(good_event, units=0)],
             'policy_revisions': []},
            {'batch_id': 'b', 'event_revisions': [dict(good_event, units=10**9 + 1)],
             'policy_revisions': []},
            {'batch_id': 'b', 'event_revisions': [dict(good_event, at='2026-01-01T00:30:00')],
             'policy_revisions': []},
            {'batch_id': 'b', 'event_revisions': [dict(good_event, deleted='false')],
             'policy_revisions': []},
            {'batch_id': 'b', 'event_revisions': [dict(good_event, extra=1)],
             'policy_revisions': []},
            {'batch_id': 'b', 'event_revisions': [retire('E1', 1)],
             'policy_revisions': []},
            {'batch_id': 'b', 'event_revisions': [{'event_id': 'E1', 'revision': 1,
                                                   'deleted': True, 'units': 1}],
             'policy_revisions': []},
            {'batch_id': 'b', 'event_revisions': [],
             'policy_revisions': [dict(good_policy, window_seconds=0)]},
            {'batch_id': 'b', 'event_revisions': [],
             'policy_revisions': [dict(good_policy, window_seconds=31536001)]},
            {'batch_id': 'b', 'event_revisions': [],
             'policy_revisions': [dict(good_policy, limit=0)]},
            {'batch_id': 'b', 'event_revisions': [],
             'policy_revisions': [dict(good_policy, limit=10**12 + 1)]},
            {'batch_id': 'b', 'event_revisions': [],
             'policy_revisions': [dict(good_policy, limit=True)]},
            {'batch_id': 'b', 'event_revisions': [],
             'policy_revisions': [dict(good_policy,
                                       valid_from='2026-01-01T01:00:00Z',
                                       valid_to='2026-01-01T01:00:00Z')]},
            {'batch_id': 'b', 'event_revisions': [],
             'policy_revisions': [{'policy_id': 'P1', 'revision': 1,
                                   'deleted': True,
                                   'principal': 'a'}]},
            {'batch_id': 'b', 'event_revisions': [],
             'policy_revisions': [dict(good_policy, revision=0)]},
            {'batch_id': 'b', 'event_revisions': [],
             'policy_revisions': [dict(good_policy, revision=True)]},
        ]
        before = self.state()
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises((ValueError, TypeError)):
                    self.raw_ingest(payload)
                self.assertEqual(self.state(), before)
        with self.assertRaises(ValueError):
            self.raw_ingest('{')
        with self.assertRaises(ValueError):
            self.raw_ingest('{"batch_id":"b","batch_id":"c"}')
        self.assertEqual(self.state(), before)


class ReopenTests(WarehouseCase):
    def test_reopened_database_reproduces_history_and_receipts(self):
        first = self.ingest('b1',
                            policies=[policy('P1', 1, 'a', '/x',
                                             '2026-01-01T00:00:00Z', None,
                                             3600, 5)],
                            events=[event('E1', 1, 'a', '/x',
                                          '2026-01-01T00:30:00Z', 2)])
        expected = self.query(1, '2026-01-01T02:00:00Z')
        state = self.state()
        reopened = self.directory / 'copy.db'
        reopened.write_bytes(self.db.read_bytes())
        self.assertEqual(self.query(1, '2026-01-01T02:00:00Z', db=reopened),
                         expected)
        self.assertEqual(self.state(reopened)['batches'], state['batches'])
        source = self.source({'batch_id': 'b1',
                              'event_revisions': [event('E1', 1, 'a', '/x',
                                                        '2026-01-01T00:30:00Z', 2)],
                              'policy_revisions': [policy('P1', 1, 'a', '/x',
                                                          '2026-01-01T00:00:00Z',
                                                          None, 3600, 5)]})
        self.assertEqual(warehouse.ingest_batch(reopened, source), first)
        self.assertEqual(self.query(1, '2026-01-01T02:00:00Z', db=reopened),
                         expected)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)
        self.db = self.directory / 'warehouse.db'

    def invoke(self, *arguments):
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(ROOT / 'src')
        return subprocess.run([sys.executable, '-m', MODULE, *map(str, arguments)],
                              capture_output=True, text=True, timeout=20,
                              env=environment)

    def write(self, data, name='batch.json'):
        path = self.directory / name
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def base_batch(self, batch_id='b1', events=None, policies=None):
        return self.write({
            'batch_id': batch_id,
            'event_revisions': events if events is not None else [
                event('E1', 1, 'a', '/x', '2026-01-01T00:30:00Z', 3)],
            'policy_revisions': policies if policies is not None else [
                policy('P1', 1, 'a', '/x', '2026-01-01T00:00:00Z',
                       None, 3600, 5)],
        })

    def test_create_contract_and_refusal_to_overwrite(self):
        result = self.invoke('create', '--db', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"schema_version":1}\n')
        original = self.db.read_bytes()
        result = self.invoke('create', '--db', self.db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertEqual(self.db.read_bytes(), original)

    def test_create_refuses_symlink_database(self):
        target = self.directory / 'target.db'
        target.write_bytes(b'')
        link = self.directory / 'link.db'
        link.symlink_to(target)
        result = self.invoke('create', '--db', link)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(link.read_bytes(), b'')

    def test_ingest_and_query_do_not_create_database(self):
        output = self.directory / 'report.json'
        result = self.invoke('ingest', '--db', self.db,
                             '--source', self.base_batch())
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.exists())
        result = self.invoke('query', '--db', self.db, '--max-seq', 0,
                             '--event-before', '2026-01-01T02:00:00Z',
                             '--output', output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.exists())
        self.assertFalse(output.exists())

    def test_full_cli_flow_stdout_matches_output_file(self):
        self.assertEqual(self.invoke('create', '--db', self.db).returncode, 0)
        result = self.invoke('ingest', '--db', self.db,
                             '--source', self.base_batch())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'batch_id': 'b1', 'seq': 1,
                          'event_revision_count': 1,
                          'policy_revision_count': 1})
        output = self.directory / 'report.json'
        result = self.invoke('query', '--db', self.db, '--max-seq', 1,
                             '--event-before', '2026-01-01T02:00:00Z',
                             '--output', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(), result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report['windows'][0]['used'], 3)
        self.assertEqual(report['event_before_us'],
                         us('2026-01-01T02:00:00Z'))

    def test_rejected_batch_leaves_all_history_and_receipts_intact(self):
        self.invoke('create', '--db', self.db)
        self.invoke('ingest', '--db', self.db, '--source', self.base_batch())
        connection = sqlite3.connect(os.fspath(self.db))
        try:
            before_batches = connection.execute(
                'SELECT seq, batch_id, receipt FROM batches ORDER BY seq').fetchall()
            before_events = connection.execute(
                'SELECT event_id, revision FROM event_versions').fetchall()
            before_policies = connection.execute(
                'SELECT policy_id, revision FROM policy_versions').fetchall()
        finally:
            connection.close()
        old = self.directory / 'old.json'
        self.invoke('query', '--db', self.db, '--max-seq', 1,
                    '--event-before', '2026-01-01T02:00:00Z', '--output', old)
        old_bytes = old.read_bytes()
        # Overlapping policy revision is a final-state error.
        bad = self.write({'batch_id': 'b2',
                          'event_revisions': [],
                          'policy_revisions': [
                              policy('P2', 1, 'a', '/x',
                                     '2026-01-01T00:15:00Z', None, 3600, 5)]},
                         'bad.json')
        result = self.invoke('ingest', '--db', self.db, '--source', bad)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        connection = sqlite3.connect(os.fspath(self.db))
        try:
            self.assertEqual(connection.execute(
                'SELECT seq, batch_id, receipt FROM batches ORDER BY seq'
            ).fetchall(), before_batches)
            self.assertEqual(connection.execute(
                'SELECT event_id, revision FROM event_versions').fetchall(),
                before_events)
            self.assertEqual(connection.execute(
                'SELECT policy_id, revision FROM policy_versions').fetchall(),
                before_policies)
        finally:
            connection.close()
        # seq2 does not exist, and max_seq=1 still reproduces the old report.
        fresh = self.directory / 'fresh.json'
        result = self.invoke('query', '--db', self.db, '--max-seq', 1,
                             '--event-before', '2026-01-01T02:00:00Z',
                             '--output', fresh)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fresh.read_bytes(), old_bytes)
        result = self.invoke('query', '--db', self.db, '--max-seq', 2,
                             '--event-before', '2026-01-01T02:00:00Z',
                             '--output', fresh)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(fresh.read_bytes(), old_bytes)

    def test_query_failure_keeps_existing_output(self):
        self.invoke('create', '--db', self.db)
        self.invoke('ingest', '--db', self.db, '--source', self.base_batch())
        output = self.directory / 'report.json'
        output.write_text('previous report')
        for arguments in (
                ('--max-seq', 9, '--event-before', '2026-01-01T02:00:00Z'),
                ('--max-seq', -1, '--event-before', '2026-01-01T02:00:00Z'),
                ('--max-seq', 1, '--event-before', 'yesterday'),
                ('--max-seq', 1, '--event-before', '2026-01-01T02:00:00')):
            with self.subTest(arguments=arguments):
                result = self.invoke('query', '--db', self.db, *arguments,
                                     '--output', output)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertEqual(output.read_text(), 'previous report')

    def test_output_protection(self):
        self.invoke('create', '--db', self.db)
        self.invoke('ingest', '--db', self.db, '--source', self.base_batch())
        before = self.db.read_bytes()
        # Output == DB, including a symlink alias, is refused.
        result = self.invoke('query', '--db', self.db, '--max-seq', 1,
                             '--event-before', '2026-01-01T02:00:00Z',
                             '--output', self.db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.db.read_bytes(), before)
        alias = self.directory / 'alias.db'
        alias.symlink_to(self.db)
        result = self.invoke('query', '--db', self.db, '--max-seq', 1,
                             '--event-before', '2026-01-01T02:00:00Z',
                             '--output', alias)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.db.read_bytes(), before)
        # A symlinked output target is refused and its target is untouched.
        target = self.directory / 'target.json'
        target.write_text('unchanged')
        link = self.directory / 'report.json'
        link.symlink_to(target)
        result = self.invoke('query', '--db', self.db, '--max-seq', 1,
                             '--event-before', '2026-01-01T02:00:00Z',
                             '--output', link)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_text(), 'unchanged')

    def test_unknown_schema_and_symlink_database_rejected(self):
        stranger = self.directory / 'stranger.db'
        connection = sqlite3.connect(os.fspath(stranger))
        try:
            connection.execute('CREATE TABLE something (x INTEGER)')
            connection.commit()
        finally:
            connection.close()
        before = stranger.read_bytes()
        for command in (
                ('ingest', '--db', stranger, '--source', self.base_batch()),
                ('query', '--db', stranger, '--max-seq', 0,
                 '--event-before', '2026-01-01T02:00:00Z',
                 '--output', self.directory / 'report.json')):
            with self.subTest(command=command[0]):
                result = self.invoke(*command)
                self.assertEqual(result.returncode, 2)
        self.assertEqual(stranger.read_bytes(), before)
        self.assertFalse((self.directory / 'report.json').exists())

        self.invoke('create', '--db', self.db)
        link = self.directory / 'link.db'
        link.symlink_to(self.db)
        self.assertEqual(self.invoke(
            'ingest', '--db', link,
            '--source', self.base_batch()).returncode, 2)
        self.assertEqual(self.invoke(
            'query', '--db', link, '--max-seq', 0,
            '--event-before', '2026-01-01T02:00:00Z',
            '--output', self.directory / 'other.json').returncode, 2)

    def test_missing_database_is_not_created_by_query(self):
        self.assertEqual(self.invoke(
            'query', '--db', self.directory / 'absent.db', '--max-seq', 0,
            '--event-before', '2026-01-01T02:00:00Z',
            '--output', self.directory / 'report.json').returncode, 2)
        self.assertFalse((self.directory / 'absent.db').exists())
        self.assertFalse((self.directory / 'report.json').exists())


if __name__ == '__main__':
    unittest.main()
