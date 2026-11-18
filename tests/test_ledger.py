import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from api_quota_audit import ledger

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'api_quota_audit.ledger'
T0 = '2026-01-01T00:00:00Z'
T1 = '2026-01-01T01:00:00Z'
T2 = '2026-01-01T02:00:00Z'
T3 = '2026-01-01T03:00:00Z'
FAR = '2027-01-01T00:00:00Z'


def us(value):
    from api_quota_audit.model import instant
    return instant(value)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)
        self.db = self.directory / 'ledger.db'
        ledger.init_database(self.db)
        self._source_index = 0

    def apply(self, operations, db=None):
        self._source_index += 1
        source = self.directory / ('ops-%d.json' % self._source_index)
        source.write_text(json.dumps({'operations': operations}))
        return ledger.apply_batch(db or self.db, source)

    def raw_apply(self, data, db=None):
        self._source_index += 1
        source = self.directory / ('ops-%d.json' % self._source_index)
        source.write_text(data if isinstance(data, str) else json.dumps(data))
        return ledger.apply_batch(db or self.db, source)

    def report(self, db=None):
        return ledger.build_report(db or self.db)

    def lot(self, report, lot_id):
        return next(item for item in report['lots'] if item['lot_id'] == lot_id)

    def hold(self, report, hold_id):
        return next(item for item in report['holds'] if item['hold_id'] == hold_id)

    def assert_conserved(self, report):
        """Verify every lot balance and that held/consumed match the holds."""
        held = {}
        consumed = {}
        for hold in report['holds']:
            self.assertIn(hold['status'], ('active', 'committed', 'cancelled', 'invalidated'))
            self.assertTrue(hold['allocation'])
            self.assertEqual(sum(item['units'] for item in hold['allocation']), hold['units'])
            for item in hold['allocation']:
                self.assertGreater(item['units'], 0)
            if hold['status'] == 'active':
                self.assertIsNone(hold['used_units'])
                self.assertEqual(hold['consumed_allocation'], [])
                for item in hold['allocation']:
                    held[item['lot_id']] = held.get(item['lot_id'], 0) + item['units']
            elif hold['status'] == 'committed':
                self.assertIsInstance(hold['used_units'], int)
                self.assertEqual(sum(item['units'] for item in hold['consumed_allocation']),
                                 hold['used_units'])
                for item in hold['consumed_allocation']:
                    consumed[item['lot_id']] = consumed.get(item['lot_id'], 0) + item['units']
            else:
                self.assertIsNone(hold['used_units'])
                self.assertEqual(hold['consumed_allocation'], [])
        for lot in report['lots']:
            self.assertIn(lot['status'], ('active', 'expired', 'revoked'))
            for key in ('granted', 'available', 'held', 'consumed', 'retired'):
                self.assertIsInstance(lot[key], int)
                self.assertGreaterEqual(lot[key], 0)
            self.assertEqual(lot['granted'], lot['available'] + lot['held']
                             + lot['consumed'] + lot['retired'], lot)
            self.assertEqual(lot['held'], held.get(lot['lot_id'], 0), lot)
            self.assertEqual(lot['consumed'], consumed.get(lot['lot_id'], 0), lot)
        self.assertEqual([lot['lot_id'] for lot in report['lots']],
                         sorted(lot['lot_id'] for lot in report['lots']))
        self.assertEqual([hold['hold_id'] for hold in report['holds']],
                         sorted(hold['hold_id'] for hold in report['holds']))

    def grants(self, at=T0):
        return [
            {'op_id': 'gA', 'kind': 'grant', 'at': at, 'lot_id': 'A',
             'principal': 'p', 'units': 3, 'expires_at': T1},
            {'op_id': 'gB', 'kind': 'grant', 'at': at, 'lot_id': 'B',
             'principal': 'p', 'units': 5, 'expires_at': T2},
        ]


class FlowTests(LedgerCase):
    def test_fresh_database_report(self):
        report = self.report()
        self.assertEqual(report, {'schema_version': 1, 'clock_us': None,
                                  'lots': [], 'holds': []})
        self.assert_conserved(report)

    def test_cross_lot_allocation_and_expiry_linkage(self):
        receipts = self.apply(self.grants() + [
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 6}])
        self.assertEqual(receipts[2]['allocation'],
                         [{'lot_id': 'A', 'units': 3}, {'lot_id': 'B', 'units': 3}])
        self.assert_conserved(self.report())

        receipts = self.apply([{'op_id': 'adv', 'kind': 'advance', 'at': T1}])
        self.assertEqual(receipts[0]['invalidated_hold_ids'], ['H'])
        self.assertEqual(receipts[0]['allocation'], [])
        self.assertEqual(receipts[0]['consumed_allocation'], [])
        report = self.report()
        self.assertEqual(self.lot(report, 'A')['status'], 'expired')
        self.assertEqual(self.lot(report, 'A')['retired'], 3)
        self.assertEqual(self.lot(report, 'B')['available'], 5)
        self.assertEqual(self.hold(report, 'H')['status'], 'invalidated')
        self.assertEqual(self.hold(report, 'H')['allocation'],
                         [{'lot_id': 'A', 'units': 3}, {'lot_id': 'B', 'units': 3}])
        self.assert_conserved(report)

        receipts = self.apply([{'op_id': 'rJ', 'kind': 'reserve', 'at': T1,
                                'hold_id': 'J', 'principal': 'p', 'units': 5}])
        self.assertEqual(receipts[0]['allocation'], [{'lot_id': 'B', 'units': 5}])
        self.assert_conserved(self.report())

    def test_revoke_linkage_releases_other_lot(self):
        self.apply(self.grants() + [
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 6}])
        receipts = self.apply([{'op_id': 'rev', 'kind': 'revoke', 'at': T0,
                                'lot_id': 'A'}])
        self.assertEqual(receipts[0]['invalidated_hold_ids'], ['H'])
        report = self.report()
        self.assertEqual(self.lot(report, 'A')['status'], 'revoked')
        self.assertEqual(self.lot(report, 'A')['retired'], 3)
        self.assertEqual(self.lot(report, 'B')['available'], 5)
        self.assert_conserved(report)
        receipts = self.apply([{'op_id': 'rJ', 'kind': 'reserve', 'at': T0,
                                'hold_id': 'J', 'principal': 'p', 'units': 5}])
        self.assertEqual(receipts[0]['allocation'], [{'lot_id': 'B', 'units': 5}])
        self.assert_conserved(self.report())

    def test_partial_commit_preserves_allocation_prefix(self):
        self.apply(self.grants() + [
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 6}])
        receipts = self.apply([{'op_id': 'cH', 'kind': 'commit', 'at': T0,
                                'hold_id': 'H', 'used_units': 4}])
        self.assertEqual(receipts[0]['consumed_allocation'],
                         [{'lot_id': 'A', 'units': 3}, {'lot_id': 'B', 'units': 1}])
        self.assertEqual(receipts[0]['allocation'], [])
        report = self.report()
        hold = self.hold(report, 'H')
        self.assertEqual(hold['status'], 'committed')
        self.assertEqual(hold['used_units'], 4)
        self.assertEqual(hold['allocation'],
                         [{'lot_id': 'A', 'units': 3}, {'lot_id': 'B', 'units': 3}])
        self.assertEqual(self.lot(report, 'A')['consumed'], 3)
        self.assertEqual(self.lot(report, 'A')['available'], 0)
        self.assertEqual(self.lot(report, 'B')['consumed'], 1)
        # B's freed allocation (3 - 1) plus its still-free 2 stays available.
        self.assertEqual(self.lot(report, 'B')['available'], 4)
        self.assertEqual(self.lot(report, 'B')['held'], 0)
        self.assert_conserved(report)

    def test_commit_zero_and_commit_full(self):
        self.apply(self.grants() + [
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 4},
            {'op_id': 'rI', 'kind': 'reserve', 'at': T0, 'hold_id': 'I',
             'principal': 'p', 'units': 4}])
        receipts = self.apply([
            {'op_id': 'cH', 'kind': 'commit', 'at': T0, 'hold_id': 'H', 'used_units': 0},
            {'op_id': 'cI', 'kind': 'commit', 'at': T0, 'hold_id': 'I', 'used_units': 4}])
        self.assertEqual(receipts[0]['consumed_allocation'], [])
        self.assertEqual(receipts[1]['consumed_allocation'],
                         [{'lot_id': 'B', 'units': 4}])
        report = self.report()
        self.assertEqual(self.hold(report, 'H')['used_units'], 0)
        self.assertEqual(self.hold(report, 'H')['consumed_allocation'], [])
        self.assertEqual(self.lot(report, 'A')['available'], 3)
        self.assertEqual(self.lot(report, 'B')['available'], 1)
        self.assert_conserved(report)

    def test_cancel_releases_every_source(self):
        self.apply(self.grants() + [
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 6}])
        self.apply([{'op_id': 'xH', 'kind': 'cancel', 'at': T0, 'hold_id': 'H'}])
        report = self.report()
        self.assertEqual(self.hold(report, 'H')['status'], 'cancelled')
        self.assertEqual(self.hold(report, 'H')['allocation'],
                         [{'lot_id': 'A', 'units': 3}, {'lot_id': 'B', 'units': 3}])
        self.assertEqual(self.lot(report, 'A')['available'], 3)
        self.assertEqual(self.lot(report, 'B')['available'], 5)
        self.assert_conserved(report)

    def test_multiple_holds_share_invalidated_lot(self):
        self.apply(self.grants() + [
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 2},
            {'op_id': 'rI', 'kind': 'reserve', 'at': T0, 'hold_id': 'I',
             'principal': 'p', 'units': 4}])
        receipts = self.apply([{'op_id': 'adv', 'kind': 'advance', 'at': T1}])
        self.assertEqual(receipts[0]['invalidated_hold_ids'], ['H', 'I'])
        report = self.report()
        self.assertEqual(self.hold(report, 'H')['status'], 'invalidated')
        self.assertEqual(self.hold(report, 'I')['status'], 'invalidated')
        self.assertEqual(self.lot(report, 'A')['retired'], 3)
        self.assertEqual(self.lot(report, 'B')['available'], 5)
        self.assert_conserved(report)

    def test_simultaneous_expiry_is_atomic(self):
        self.apply([
            {'op_id': 'gA', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
             'principal': 'p', 'units': 2, 'expires_at': T1},
            {'op_id': 'gB', 'kind': 'grant', 'at': T0, 'lot_id': 'B',
             'principal': 'p', 'units': 3, 'expires_at': T1},
            {'op_id': 'gC', 'kind': 'grant', 'at': T0, 'lot_id': 'C',
             'principal': 'p', 'units': 4, 'expires_at': T3},
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 5},
            {'op_id': 'rI', 'kind': 'reserve', 'at': T0, 'hold_id': 'I',
             'principal': 'p', 'units': 2}])
        receipts = self.apply([{'op_id': 'adv', 'kind': 'advance', 'at': T1}])
        # H spans A and B (A2+B3), I sits on C, so only H is invalidated.
        self.assertEqual(receipts[0]['invalidated_hold_ids'], ['H'])
        report = self.report()
        self.assertEqual(self.lot(report, 'A')['retired'], 2)
        self.assertEqual(self.lot(report, 'B')['retired'], 3)
        self.assertEqual(self.lot(report, 'C')['available'], 2)
        self.assertEqual(self.lot(report, 'C')['held'], 2)
        self.assertEqual(report['clock_us'], us(T1))
        self.assert_conserved(report)

    def test_expiry_boundary_is_inclusive(self):
        self.apply([
            {'op_id': 'gA', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
             'principal': 'p', 'units': 3, 'expires_at': T1},
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 3}])
        before = self.report()
        self.assertEqual(self.hold(before, 'H')['status'], 'active')
        receipts = self.apply([{'op_id': 'edge', 'kind': 'advance', 'at': T1}])
        self.assertEqual(receipts[0]['invalidated_hold_ids'], ['H'])
        report = self.report()
        self.assertEqual(self.lot(report, 'A')['status'], 'expired')
        # Exactly at expiry no availability may be reserved from the lot.
        with self.assertRaises(ValueError):
            self.apply([{'op_id': 'tooLate', 'kind': 'reserve', 'at': T1,
                         'hold_id': 'J', 'principal': 'p', 'units': 1}])
        self.assert_conserved(report)

    def test_revoke_partially_consumed_lot(self):
        self.apply(self.grants() + [
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 6},
            {'op_id': 'cH', 'kind': 'commit', 'at': T0, 'hold_id': 'H',
             'used_units': 4}])
        receipts = self.apply([{'op_id': 'revB', 'kind': 'revoke', 'at': T0,
                                'lot_id': 'B'}])
        self.assertEqual(receipts[0]['invalidated_hold_ids'], [])
        report = self.report()
        lot_b = self.lot(report, 'B')
        self.assertEqual(lot_b['status'], 'revoked')
        self.assertEqual(lot_b['consumed'], 1)
        self.assertEqual(lot_b['retired'], 4)
        self.assertEqual(self.hold(report, 'H')['status'], 'committed')
        self.assert_conserved(report)

    def test_revoke_retires_active_hold_but_keeps_consumed(self):
        self.apply(self.grants() + [
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 6},
            {'op_id': 'cH', 'kind': 'commit', 'at': T0, 'hold_id': 'H',
             'used_units': 4},
            {'op_id': 'rJ', 'kind': 'reserve', 'at': T0, 'hold_id': 'J',
             'principal': 'p', 'units': 2}])
        receipts = self.apply([{'op_id': 'revB', 'kind': 'revoke', 'at': T0,
                                'lot_id': 'B'}])
        self.assertEqual(receipts[0]['invalidated_hold_ids'], ['J'])
        report = self.report()
        lot_b = self.lot(report, 'B')
        self.assertEqual((lot_b['consumed'], lot_b['retired'], lot_b['available']),
                         (1, 4, 0))
        self.assertEqual(self.hold(report, 'H')['consumed_allocation'],
                         [{'lot_id': 'A', 'units': 3}, {'lot_id': 'B', 'units': 1}])
        self.assert_conserved(report)

    def test_revoke_after_same_round_expiry_fails_and_rolls_back(self):
        self.apply([
            {'op_id': 'gA', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
             'principal': 'p', 'units': 3, 'expires_at': T1},
            {'op_id': 'gB', 'kind': 'grant', 'at': T0, 'lot_id': 'B',
             'principal': 'p', 'units': 5, 'expires_at': T2},
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 3}])
        before = self.report()
        with self.assertRaises(ValueError):
            # Expiry runs first, so the target lot is no longer active.
            self.apply([{'op_id': 'revA', 'kind': 'revoke', 'at': T1,
                         'lot_id': 'A'}])
        self.assertEqual(self.report(), before)
        receipts = self.apply([{'op_id': 'adv', 'kind': 'advance', 'at': T1}])
        self.assertEqual(receipts[0]['invalidated_hold_ids'], ['H'])
        self.assert_conserved(self.report())

    def test_expiry_and_revoke_invalidations_are_combined_in_receipt(self):
        self.apply([
            {'op_id': 'gA', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
             'principal': 'p', 'units': 2, 'expires_at': T1},
            {'op_id': 'gB', 'kind': 'grant', 'at': T0, 'lot_id': 'B',
             'principal': 'p', 'units': 5, 'expires_at': T2},
            {'op_id': 'gC', 'kind': 'grant', 'at': T0, 'lot_id': 'C',
             'principal': 'p', 'units': 5, 'expires_at': T2},
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 2},
            {'op_id': 'rI', 'kind': 'reserve', 'at': T0, 'hold_id': 'I',
             'principal': 'p', 'units': 5}])
        # I sits on B.  At T1 lot A expires (invalidating H) and B is revoked
        # in the same operation (invalidating I).
        receipts = self.apply([{'op_id': 'revB', 'kind': 'revoke', 'at': T1,
                                'lot_id': 'B'}])
        self.assertEqual(receipts[0]['invalidated_hold_ids'], ['H', 'I'])
        report = self.report()
        self.assertEqual(self.lot(report, 'A')['retired'], 2)
        self.assertEqual(self.lot(report, 'B')['retired'], 5)
        self.assertEqual(self.lot(report, 'C')['available'], 5)
        self.assert_conserved(report)

    def test_reserve_priority_order_by_expiry_then_lot_id(self):
        self.apply([
            {'op_id': 'g1', 'kind': 'grant', 'at': T0, 'lot_id': 'z',
             'principal': 'p', 'units': 1, 'expires_at': T2},
            {'op_id': 'g2', 'kind': 'grant', 'at': T0, 'lot_id': 'y',
             'principal': 'p', 'units': 1, 'expires_at': T1},
            {'op_id': 'g3', 'kind': 'grant', 'at': T0, 'lot_id': 'x',
             'principal': 'p', 'units': 1, 'expires_at': T2}])
        receipts = self.apply([{'op_id': 'r', 'kind': 'reserve', 'at': T0,
                                'hold_id': 'H', 'principal': 'p', 'units': 3}])
        self.assertEqual(receipts[0]['allocation'],
                         [{'lot_id': 'y', 'units': 1}, {'lot_id': 'x', 'units': 1},
                          {'lot_id': 'z', 'units': 1}])

    def test_reserve_only_uses_same_principal(self):
        self.apply([
            {'op_id': 'g1', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
             'principal': 'p', 'units': 5, 'expires_at': FAR},
            {'op_id': 'g2', 'kind': 'grant', 'at': T0, 'lot_id': 'B',
             'principal': 'q', 'units': 5, 'expires_at': FAR}])
        receipts = self.apply([{'op_id': 'r', 'kind': 'reserve', 'at': T0,
                                'hold_id': 'H', 'principal': 'p', 'units': 5}])
        self.assertEqual(receipts[0]['allocation'], [{'lot_id': 'A', 'units': 5}])
        with self.assertRaises(ValueError):
            self.apply([{'op_id': 'r2', 'kind': 'reserve', 'at': T0,
                         'hold_id': 'I', 'principal': 'p', 'units': 1}])


class TransactionTests(LedgerCase):
    def test_insufficient_reserve_fails_whole_batch(self):
        before = self.report()
        with self.assertRaises(ValueError):
            self.apply([
                {'op_id': 'g1', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
                 'principal': 'p', 'units': 3, 'expires_at': FAR},
                {'op_id': 'r', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
                 'principal': 'p', 'units': 4}])
        self.assertEqual(self.report(), before)

    def test_failing_last_operation_rolls_back_expiry_clock_and_receipts(self):
        self.apply(self.grants() + [
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 6}])
        before = self.report()
        with self.assertRaises(ValueError):
            self.apply([
                {'op_id': 'gNew', 'kind': 'grant', 'at': T1, 'lot_id': 'C',
                 'principal': 'p', 'units': 1, 'expires_at': FAR},
                {'op_id': 'cGhost', 'kind': 'commit', 'at': T1,
                 'hold_id': 'ghost', 'used_units': 0}])
        self.assertEqual(self.report(), before)
        # The rollback left the ledger clock at T0, so the same batch minus
        # the failing op can be applied later and expiry happens then.
        receipts = self.apply([
            {'op_id': 'gNew', 'kind': 'grant', 'at': T1, 'lot_id': 'C',
             'principal': 'p', 'units': 1, 'expires_at': FAR}])
        self.assertEqual(receipts[0]['invalidated_hold_ids'], ['H'])
        self.assertEqual(self.report()['clock_us'], us(T1))
        self.assert_conserved(self.report())

    def test_commit_invalidated_by_its_own_expiry_fails_and_rolls_back(self):
        self.apply([
            {'op_id': 'gA', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
             'principal': 'p', 'units': 3, 'expires_at': T1},
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 3}])
        before = self.report()
        with self.assertRaises(ValueError):
            self.apply([{'op_id': 'cH', 'kind': 'commit', 'at': T1,
                         'hold_id': 'H', 'used_units': 3}])
        self.assertEqual(self.report(), before)
        # A successful advance performs the expiry, then the commit is a
        # terminal-operation failure.
        self.apply([{'op_id': 'adv', 'kind': 'advance', 'at': T1}])
        with self.assertRaises(ValueError):
            self.apply([{'op_id': 'cH2', 'kind': 'commit', 'at': T1,
                         'hold_id': 'H', 'used_units': 3}])
        self.assertEqual(self.report()['clock_us'], us(T1))

    def test_retry_after_failure_and_reopen(self):
        self.apply(self.grants())
        with self.assertRaises(ValueError):
            self.apply([
                {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
                 'principal': 'p', 'units': 6},
                {'op_id': 'bad', 'kind': 'cancel', 'at': T0, 'hold_id': 'none'}])
        # Reopen the database and retry the same operations plus a new one.
        receipts = self.apply([
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 6},
            {'op_id': 'after', 'kind': 'advance', 'at': T1}])
        self.assertEqual(receipts[0]['allocation'],
                         [{'lot_id': 'A', 'units': 3}, {'lot_id': 'B', 'units': 3}])
        reopened = self.report()
        self.assert_conserved(reopened)
        self.assertEqual(reopened['clock_us'], us(T1))

    def test_unknown_ids_and_terminal_operation_failures(self):
        self.apply(self.grants() + [
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 2}])
        self.apply([{'op_id': 'cH', 'kind': 'commit', 'at': T0, 'hold_id': 'H',
                     'used_units': 1}])
        before = self.report()
        failures = [
            [{'op_id': 'a', 'kind': 'commit', 'at': T0, 'hold_id': 'missing',
              'used_units': 0}],
            [{'op_id': 'b', 'kind': 'cancel', 'at': T0, 'hold_id': 'missing'}],
            [{'op_id': 'c', 'kind': 'revoke', 'at': T0, 'lot_id': 'missing'}],
            [{'op_id': 'd', 'kind': 'commit', 'at': T0, 'hold_id': 'H',
              'used_units': 1}],
            [{'op_id': 'e', 'kind': 'cancel', 'at': T0, 'hold_id': 'H'}],
            [{'op_id': 'f', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
              'principal': 'p', 'units': 1, 'expires_at': FAR}],
            [{'op_id': 'g', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
              'principal': 'p', 'units': 1}],
            [{'op_id': 'i', 'kind': 'commit', 'at': T0, 'hold_id': 'H',
              'used_units': 3}],
        ]
        for operations in failures:
            with self.subTest(operations=operations):
                with self.assertRaises(ValueError):
                    self.apply(operations)
                self.assertEqual(self.report(), before)
        self.apply([{'op_id': 'revA', 'kind': 'revoke', 'at': T0, 'lot_id': 'A'}])
        with self.assertRaises(ValueError):
            # A revoked lot is terminal: a second revoke fails.
            self.apply([{'op_id': 'revA2', 'kind': 'revoke', 'at': T0,
                         'lot_id': 'A'}])

    def test_used_units_zero_is_valid_but_bool_is_not(self):
        self.apply(self.grants() + [
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 2}])
        with self.assertRaises(ValueError):
            self.apply([{'op_id': 'boolCommit', 'kind': 'commit', 'at': T0,
                         'hold_id': 'H', 'used_units': True}])
        with self.assertRaises(ValueError):
            self.apply([{'op_id': 'floatCommit', 'kind': 'commit', 'at': T0,
                         'hold_id': 'H', 'used_units': 1.0}])
        receipts = self.apply([{'op_id': 'zero', 'kind': 'commit', 'at': T0,
                                'hold_id': 'H', 'used_units': 0}])
        self.assertEqual(receipts[0]['consumed_allocation'], [])

    def test_clock_cannot_move_backwards(self):
        self.apply([{'op_id': 'a', 'kind': 'advance', 'at': T2}])
        with self.assertRaises(ValueError):
            self.apply([{'op_id': 'b', 'kind': 'advance', 'at': T1}])
        self.assertEqual(self.report()['clock_us'], us(T2))
        # Same instant is allowed for several new operations.
        receipts = self.apply([
            {'op_id': 'c', 'kind': 'advance', 'at': T2},
            {'op_id': 'd', 'kind': 'advance', 'at': T2}])
        self.assertEqual([item['at_us'] for item in receipts], [us(T2), us(T2)])

    def test_empty_batch_is_a_noop(self):
        receipts = self.apply([])
        self.assertEqual(receipts, [])
        self.assertEqual(self.report()['clock_us'], None)

    def test_many_operations_within_a_batch_share_the_clock(self):
        operations = []
        for index in range(1, 501):
            operations.append({'op_id': 'op%d' % index, 'kind': 'grant',
                               'at': T0, 'lot_id': 'L%d' % index,
                               'principal': 'p', 'units': 1, 'expires_at': FAR})
        receipts = self.apply(operations)
        self.assertEqual(len(receipts), 500)
        self.assert_conserved(self.report())
        with self.assertRaises(ValueError):
            self.apply(operations + [{'op_id': 'one-too-many', 'kind': 'advance',
                                      'at': T0}])


class RetryTests(LedgerCase):
    def test_equivalent_timezone_retry_returns_frozen_receipt(self):
        original = {'op_id': 'g1', 'kind': 'grant', 'at': '2026-01-01T01:00:00+01:00',
                    'lot_id': 'A', 'principal': 'p', 'units': 3, 'expires_at': T2}
        first = self.apply([original])[0]
        self.assertEqual(first['at_us'], us(T0))
        # Retry with an equivalent UTC spelling and a different key order.
        retried = {'expires_at': T2, 'units': 3, 'principal': 'p',
                   'lot_id': 'A', 'kind': 'grant',
                   'at': '2026-01-01T00:00:00Z', 'op_id': 'g1'}
        second = self.apply([retried])[0]
        self.assertEqual(second, first)
        self.assertEqual(self.report()['clock_us'], us(T0))
        before = self.report()
        # A genuine retry is accepted even after the clock has moved past it.
        self.apply([{'op_id': 'adv', 'kind': 'advance', 'at': T3}])
        after_clock = self.report()['clock_us']
        third = self.apply([retried])[0]
        self.assertEqual(third, first)
        report = self.report()
        self.assertEqual(report['clock_us'], after_clock)
        self.assertEqual([lot['lot_id'] for lot in report['lots']], ['A'])
        self.assertEqual(report['lots'][0]['granted'], 3)
        self.assertNotEqual(before['clock_us'], report['clock_us'])

    def test_conflicting_reuse_of_op_id_fails(self):
        self.apply([{'op_id': 'g1', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
                     'principal': 'p', 'units': 3, 'expires_at': FAR}])
        before = self.report()
        with self.assertRaises(ValueError):
            self.apply([{'op_id': 'g1', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
                         'principal': 'p', 'units': 4, 'expires_at': FAR}])
        with self.assertRaises(ValueError):
            self.apply([{'op_id': 'g1', 'kind': 'advance', 'at': T0}])
        self.assertEqual(self.report(), before)

    def test_extra_fields_rejected_even_for_existing_op_id(self):
        self.apply([{'op_id': 'g1', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
                     'principal': 'p', 'units': 3, 'expires_at': FAR}])
        with self.assertRaises(ValueError):
            self.apply([{'op_id': 'g1', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
                         'principal': 'p', 'units': 3, 'expires_at': FAR,
                         'extra': 1}])

    def test_within_batch_duplicate_receipt_and_clock(self):
        self.apply([{'op_id': 'g', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
                     'principal': 'p', 'units': 2, 'expires_at': FAR}])
        receipts = self.apply([
            {'op_id': 'r', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 2},
            {'op_id': 'r', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 2}])
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.report()['clock_us'], us(T0))

    def test_mixed_retry_and_new_operations_only_advance_for_new(self):
        first = self.apply([{'op_id': 'g1', 'kind': 'grant', 'at': T0,
                             'lot_id': 'A', 'principal': 'p', 'units': 3,
                             'expires_at': FAR}])[0]
        self.apply([{'op_id': 'adv', 'kind': 'advance', 'at': T1}])
        receipts = self.apply([
            {'op_id': 'g1', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
             'principal': 'p', 'units': 3, 'expires_at': FAR},
            {'op_id': 'g2', 'kind': 'grant', 'at': T1, 'lot_id': 'B',
             'principal': 'p', 'units': 2, 'expires_at': FAR}])
        self.assertEqual(receipts[0], first)
        self.assertEqual(receipts[1]['kind'], 'grant')
        self.assertEqual(self.report()['clock_us'], us(T1))


class ValidationTests(LedgerCase):
    def test_invalid_operation_shapes(self):
        bad = [
            '{}',
            {'operations': [], 'extra': 1},
            {'operations': {}},
            {'operations': [1]},
            {'operations': [{'op_id': 'x', 'kind': 'nope', 'at': T0}]},
            {'operations': [{'op_id': 'x', 'kind': 'advance', 'at': T0, 'extra': 1}]},
            {'operations': [{'op_id': 'x', 'kind': 'grant', 'at': T0,
                             'lot_id': 'A', 'principal': 'p', 'units': 1}]},
            {'operations': [{'op_id': 'x', 'kind': 'grant', 'at': '2026-01-01T00:00:00',
                             'lot_id': 'A', 'principal': 'p', 'units': 1,
                             'expires_at': FAR}]},
            {'operations': [{'op_id': 'x', 'kind': 'grant', 'at': T0,
                             'lot_id': 'A', 'principal': 'p', 'units': 0,
                             'expires_at': FAR}]},
            {'operations': [{'op_id': 'x', 'kind': 'grant', 'at': T0,
                             'lot_id': 'A', 'principal': 'p', 'units': 10 ** 12 + 1,
                             'expires_at': FAR}]},
            {'operations': [{'op_id': 'x', 'kind': 'grant', 'at': T0,
                             'lot_id': 'A', 'principal': 'p', 'units': True,
                             'expires_at': FAR}]},
            {'operations': [{'op_id': 'x', 'kind': 'grant', 'at': T0,
                             'lot_id': 'A', 'principal': 'p', 'units': 1.5,
                             'expires_at': FAR}]},
            {'operations': [{'op_id': 'x', 'kind': 'grant', 'at': T0,
                             'lot_id': 'A', 'principal': 'p', 'units': 1,
                             'expires_at': T0}]},
            {'operations': [{'op_id': 'x', 'kind': 'grant', 'at': T1,
                             'lot_id': 'A', 'principal': 'p', 'units': 1,
                             'expires_at': T0}]},
            {'operations': [{'op_id': '', 'kind': 'advance', 'at': T0}]},
            {'operations': [{'op_id': 'x', 'kind': 'reserve', 'at': T0,
                             'hold_id': 'H', 'principal': 'p', 'units': 0}]},
            {'operations': [{'op_id': 'x', 'kind': 'commit', 'at': T0,
                             'hold_id': 'H', 'used_units': -1}]},
        ]
        before = self.report()
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.raw_apply(payload)
                self.assertEqual(self.report(), before)

    def test_malformed_json_rejected_without_touching_ledger(self):
        self.apply(self.grants())
        before = self.report()
        for raw in ('{', '{"operations":[],"operations":[]}', '{"operations":[]} trailing'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    self.raw_apply(raw)
                self.assertEqual(self.report(), before)

    def test_non_object_root_and_duplicate_json_keys(self):
        with self.assertRaises(ValueError):
            self.raw_apply('[]')
        with self.assertRaises(ValueError):
            self.raw_apply('{"operations": [], "operations": []}')


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)
        self.db = self.directory / 'ledger.db'

    def invoke(self, *arguments):
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(ROOT / 'src')
        return subprocess.run([sys.executable, '-m', MODULE, *map(str, arguments)],
                              capture_output=True, text=True, timeout=20,
                              env=environment)

    def apply_file(self, operations, name='ops.json'):
        source = self.directory / name
        source.write_text(json.dumps({'operations': operations}))
        return source

    def test_init_contract_and_refusal_to_overwrite(self):
        result = self.invoke('init', '--db', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"schema_version":1}\n')
        original = self.db.read_bytes()
        result = self.invoke('init', '--db', self.db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertEqual(self.db.read_bytes(), original)

    def test_init_refuses_symlink_database(self):
        target = self.directory / 'target.db'
        target.write_bytes(b'')
        link = self.directory / 'link.db'
        link.symlink_to(target)
        result = self.invoke('init', '--db', link)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(link.read_bytes(), b'')

    def test_apply_and_report_do_not_create_missing_database(self):
        source = self.apply_file([{'op_id': 'a', 'kind': 'advance', 'at': T0}])
        result = self.invoke('apply', '--db', self.db, '--source', source)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.exists())
        result = self.invoke('report', '--db', self.db,
                             '--output', self.directory / 'report.json')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.exists())
        self.assertFalse((self.directory / 'report.json').exists())

    def test_apply_stdout_is_receipts_object(self):
        self.invoke('init', '--db', self.db)
        source = self.apply_file([
            {'op_id': 'g', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
             'principal': 'p', 'units': 3, 'expires_at': T2}])
        result = self.invoke('apply', '--db', self.db, '--source', source)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), {'receipts'})
        self.assertEqual(len(payload['receipts']), 1)
        self.assertEqual(payload['receipts'][0]['kind'], 'grant')

    def test_failure_writes_stderr_only_and_preserves_report(self):
        self.invoke('init', '--db', self.db)
        source = self.apply_file([{'op_id': 'g', 'kind': 'grant', 'at': T0,
                                   'lot_id': 'A', 'principal': 'p', 'units': 3,
                                   'expires_at': T2}])
        self.invoke('apply', '--db', self.db, '--source', source)
        output = self.directory / 'report.json'
        self.invoke('report', '--db', self.db, '--output', output)
        previous = output.read_bytes()
        bad = self.apply_file([{'op_id': 'x', 'kind': 'cancel', 'at': T0,
                                'hold_id': 'ghost'}], 'bad.json')
        result = self.invoke('apply', '--db', self.db, '--source', bad)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertEqual(output.read_bytes(), previous)

    def test_report_stdout_matches_written_file(self):
        self.invoke('init', '--db', self.db)
        output = self.directory / 'report.json'
        result = self.invoke('report', '--db', self.db, '--output', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.encode(), output.read_bytes())
        self.assertEqual(json.loads(result.stdout),
                         {'schema_version': 1, 'clock_us': None,
                          'lots': [], 'holds': []})

    def test_report_refuses_output_aliasing_the_database(self):
        self.invoke('init', '--db', self.db)
        original = self.db.read_bytes()
        self.assertEqual(self.invoke('report', '--db', self.db,
                                     '--output', self.db).returncode, 2)
        alias = self.directory / 'alias.json'
        os.link(self.db, alias)
        self.assertEqual(self.invoke('report', '--db', self.db,
                                     '--output', alias).returncode, 2)
        relative = self.directory / 'nested' / '..' / 'ledger.db'
        (self.directory / 'nested').mkdir()
        self.assertEqual(self.invoke('report', '--db', self.db,
                                     '--output', relative).returncode, 2)
        self.assertEqual(self.db.read_bytes(), original)

    def test_report_refuses_symlink_output(self):
        self.invoke('init', '--db', self.db)
        target = self.directory / 'target.json'
        target.write_text('unchanged')
        link = self.directory / 'link.json'
        link.symlink_to(target)
        self.assertEqual(self.invoke('report', '--db', self.db,
                                     '--output', link).returncode, 2)
        self.assertEqual(target.read_text(), 'unchanged')

    def test_unknown_schema_is_rejected_and_left_untouched(self):
        connection = sqlite3.connect(self.db)
        connection.execute('CREATE TABLE other (x INTEGER)')
        connection.commit()
        connection.close()
        original = self.db.read_bytes()
        output = self.directory / 'report.json'
        output.write_text('previous report')
        source = self.apply_file([{'op_id': 'a', 'kind': 'advance', 'at': T0}])
        result = self.invoke('apply', '--db', self.db, '--source', source)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        result = self.invoke('report', '--db', self.db, '--output', output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(output.read_text(), 'previous report')
        self.assertEqual(self.db.read_bytes(), original)

    def test_schema_version_mismatch_is_rejected(self):
        self.invoke('init', '--db', self.db)
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE ledger_meta SET value = '2' WHERE key = 'schema_version'")
        connection.commit()
        connection.close()
        original = self.db.read_bytes()
        source = self.apply_file([{'op_id': 'a', 'kind': 'advance', 'at': T0}])
        self.assertEqual(self.invoke('apply', '--db', self.db, '--source', source).returncode, 2)
        output = self.directory / 'report.json'
        self.assertEqual(self.invoke('report', '--db', self.db,
                                     '--output', output).returncode, 2)
        self.assertFalse(output.exists())
        self.assertEqual(self.db.read_bytes(), original)

    def test_zero_byte_database_is_not_treated_as_empty_ledger(self):
        self.db.write_bytes(b'')
        source = self.apply_file([{'op_id': 'a', 'kind': 'advance', 'at': T0}])
        self.assertEqual(self.invoke('apply', '--db', self.db, '--source', source).returncode, 2)
        output = self.directory / 'report.json'
        self.assertEqual(self.invoke('report', '--db', self.db,
                                     '--output', output).returncode, 2)
        self.assertFalse(output.exists())
        self.assertEqual(self.db.read_bytes(), b'')

    def test_non_database_file_is_rejected(self):
        self.db.write_bytes(b'not a database at all')
        source = self.apply_file([{'op_id': 'a', 'kind': 'advance', 'at': T0}])
        result = self.invoke('apply', '--db', self.db, '--source', source)
        self.assertEqual(result.returncode, 2)
        result = self.invoke('report', '--db', self.db,
                             '--output', self.directory / 'report.json')
        self.assertEqual(result.returncode, 2)

    def test_symlink_database_rejected_for_apply_and_report(self):
        self.invoke('init', '--db', self.db)
        link = self.directory / 'link.db'
        link.symlink_to(self.db)
        source = self.apply_file([{'op_id': 'a', 'kind': 'advance', 'at': T0}])
        self.assertEqual(self.invoke('apply', '--db', link, '--source', source).returncode, 2)
        self.assertEqual(self.invoke('report', '--db', link,
                                     '--output', self.directory / 'r.json').returncode, 2)
        self.assertFalse((self.directory / 'r.json').exists())

    def test_fixture_flow(self):
        self.assertEqual(self.invoke('init', '--db', self.db).returncode, 0)
        result = self.invoke('apply', '--db', self.db,
                             '--source', ROOT / 'fixtures' / 'ledger.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        receipts = json.loads(result.stdout)['receipts']
        self.assertEqual([item['op_id'] for item in receipts],
                         ['grant-a', 'grant-b', 'reserve-h', 'advance-hour',
                          'reserve-j', 'commit-j'])
        self.assertEqual(receipts[2]['allocation'],
                         [{'lot_id': 'lot-a', 'units': 3},
                          {'lot_id': 'lot-b', 'units': 3}])
        self.assertEqual(receipts[3]['invalidated_hold_ids'], ['hold-h'])
        self.assertEqual(receipts[4]['allocation'],
                         [{'lot_id': 'lot-b', 'units': 5}])
        self.assertEqual(receipts[5]['consumed_allocation'],
                         [{'lot_id': 'lot-b', 'units': 4}])
        output = self.directory / 'report.json'
        self.assertEqual(self.invoke('report', '--db', self.db,
                                     '--output', output).returncode, 0)
        report = json.loads(output.read_text())
        self.assertEqual(report['lots'][0]['status'], 'expired')
        self.assertEqual(report['lots'][0]['retired'], 3)
        self.assertEqual(report['lots'][1]['consumed'], 4)
        self.assertEqual(report['lots'][1]['available'], 1)
        self.assertEqual(report['holds'][0]['status'], 'invalidated')
        self.assertEqual(report['holds'][1]['status'], 'committed')

    def test_full_flow_survives_reopen(self):
        self.invoke('init', '--db', self.db)
        self.invoke('apply', '--db', self.db, '--source', self.apply_file([
            {'op_id': 'gA', 'kind': 'grant', 'at': T0, 'lot_id': 'A',
             'principal': 'p', 'units': 3, 'expires_at': T1},
            {'op_id': 'gB', 'kind': 'grant', 'at': T0, 'lot_id': 'B',
             'principal': 'p', 'units': 5, 'expires_at': T2},
            {'op_id': 'rH', 'kind': 'reserve', 'at': T0, 'hold_id': 'H',
             'principal': 'p', 'units': 6}], 'one.json'))
        first = self.directory / 'first.json'
        second = self.directory / 'second.json'
        self.invoke('report', '--db', self.db, '--output', first)
        self.invoke('report', '--db', self.db, '--output', second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.invoke('apply', '--db', self.db, '--source', self.apply_file([
            {'op_id': 'adv', 'kind': 'advance', 'at': T1}], 'two.json'))
        third = self.directory / 'third.json'
        self.invoke('report', '--db', self.db, '--output', third)
        third_bytes = third.read_bytes()
        # Re-running the whole earlier batch is a pure retry after reopen.
        results = self.invoke('apply', '--db', self.db, '--source',
                              self.directory / 'one.json')
        self.assertEqual(results.returncode, 0, results.stderr)
        self.assertEqual(len(json.loads(results.stdout)['receipts']), 3)
        fourth = self.directory / 'fourth.json'
        self.invoke('report', '--db', self.db, '--output', fourth)
        self.assertEqual(fourth.read_bytes(), third_bytes)
        report = json.loads(third.read_text())
        self.assertEqual(report['lots'][0]['status'], 'expired')
        self.assertEqual(report['holds'][0]['status'], 'invalidated')
        self.assertEqual(report['clock_us'], us(T1))


class ReportInvariantTests(LedgerCase):
    def test_report_is_a_read_only_snapshot(self):
        self.apply(self.grants())
        report = self.report()
        self.assertEqual(self.report(), report)
        self.assertEqual(report['clock_us'], us(T0))

    def test_no_wall_clock_is_consulted(self):
        # A timestamp far in the past or future is accepted exactly as given.
        self.apply([{'op_id': 'g', 'kind': 'grant', 'at': '1990-01-01T00:00:00Z',
                     'lot_id': 'A', 'principal': 'p', 'units': 1,
                     'expires_at': '1990-01-02T00:00:00Z'}])
        self.assertEqual(self.report()['clock_us'], us('1990-01-01T00:00:00Z'))


if __name__ == '__main__':
    unittest.main()
