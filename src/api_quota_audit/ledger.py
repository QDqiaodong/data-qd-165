"""Persistent local prepaid API-credit ledger (``api-credit-ledger``).

The ledger keeps three kinds of durable state in one SQLite file:

* **lots** hold the balance of one purchased grant.  A lot's unconsumed
  quantity is split into ``available`` (free), ``held`` (the sum of the
  frozen allocations of active holds) and ``retired`` (removed by expiry or
  revocation); ``consumed`` is the sum of the frozen consumption records of
  committed holds.  The conservation invariant

  ``granted == available + held + consumed + retired``

  holds after every operation.
* **holds** freeze the ordered, cross-lot allocation chosen at reservation
  time.  The frozen allocation never moves: partial commit consumes a prefix
  of it, cancel/invalidation releases all of it, and terminal holds keep it
  for later reconciliation.
* **ops** store the canonical identity of every successful operation together
  with the exact receipt that operation produced.  A retry with the same
  ``op_id`` and the same canonical content returns the frozen receipt without
  touching the clock or the balances.

All changes of one ``apply`` batch, including the logical clock, expiry
retirement, invalidations and the new receipts, commit in a single SQLite
transaction.  A failure therefore leaves neither partial balances nor partial
receipts behind, and a database reopened later reproduces the same state.
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .common import ident, number, read, require, rows, save
from .model import instant

SCHEMA_VERSION = 1
MAX_OPERATIONS = 500
MAX_UNITS = 10 ** 12

_LOT_STATUSES = ('active', 'expired', 'revoked')
_HOLD_STATUSES = ('active', 'committed', 'cancelled', 'invalidated')
_REQUIRED_TABLES = {
    'ledger_meta', 'lots', 'holds', 'hold_allocations', 'consumed_allocations', 'ops',
}

_SCHEMA = """
CREATE TABLE ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE lots (
    lot_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    granted INTEGER NOT NULL CHECK (granted >= 1),
    expires_at_us INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'expired', 'revoked')),
    available INTEGER NOT NULL CHECK (available >= 0),
    retired INTEGER NOT NULL CHECK (retired >= 0)
);
CREATE TABLE holds (
    hold_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units >= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'committed', 'cancelled', 'invalidated')),
    used_units INTEGER CHECK (used_units IS NULL OR (used_units >= 0 AND used_units <= units)),
    CHECK ((status = 'committed' AND used_units IS NOT NULL)
        OR (status <> 'committed' AND used_units IS NULL))
);
CREATE TABLE hold_allocations (
    hold_id TEXT NOT NULL REFERENCES holds(hold_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    lot_id TEXT NOT NULL REFERENCES lots(lot_id),
    units INTEGER NOT NULL CHECK (units > 0),
    PRIMARY KEY (hold_id, position)
);
CREATE TABLE consumed_allocations (
    hold_id TEXT NOT NULL REFERENCES holds(hold_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    lot_id TEXT NOT NULL REFERENCES lots(lot_id),
    units INTEGER NOT NULL CHECK (units > 0),
    PRIMARY KEY (hold_id, position)
);
CREATE TABLE ops (
    op_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    at_us INTEGER NOT NULL,
    canonical TEXT NOT NULL,
    receipt TEXT NOT NULL
);
CREATE INDEX lots_expiry_order ON lots(status, expires_at_us, lot_id);
CREATE INDEX hold_allocations_lot ON hold_allocations(lot_id);
CREATE INDEX consumed_allocations_lot ON consumed_allocations(lot_id);
"""

# Shared fields plus the exact extra fields allowed for each operation kind.
_FIELDS = {
    'grant': 'op_id kind at lot_id principal units expires_at',
    'reserve': 'op_id kind at hold_id principal units',
    'commit': 'op_id kind at hold_id used_units',
    'cancel': 'op_id kind at hold_id',
    'revoke': 'op_id kind at lot_id',
    'advance': 'op_id kind at',
}


def _db_path(value):
    path = Path(value)
    if path.is_symlink():
        raise ValueError('database path cannot be a symlink')
    return path


def _same_file(first, second):
    left, right = Path(first), Path(second)
    try:
        if left.exists() and right.exists() and os.path.samefile(left, right):
            return True
    except OSError:
        pass
    return os.path.realpath(os.fspath(left)) == os.path.realpath(os.fspath(right))


def _dump(value, **options):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, **options)


def _parse_units(value, minimum, maximum=MAX_UNITS):
    # ``number`` rejects bool and float because it checks ``type(v) is int``.
    return number(value, minimum, maximum)


def _parse_operation(row):
    if not isinstance(row, dict):
        raise ValueError('operation must be an object')
    kind = row.get('kind')
    if not isinstance(kind, str) or kind not in _FIELDS:
        raise ValueError('unknown operation kind')
    require(row, _FIELDS[kind])
    op = {'op_id': ident(row['op_id']), 'kind': kind, 'at_us': instant(row['at'])}
    if kind == 'grant':
        op['lot_id'] = ident(row['lot_id'])
        op['principal'] = ident(row['principal'])
        op['units'] = _parse_units(row['units'], 1)
        op['expires_at_us'] = instant(row['expires_at'])
        if not op['expires_at_us'] > op['at_us']:
            raise ValueError('grant expiry must be after its timestamp')
    elif kind == 'reserve':
        op['hold_id'] = ident(row['hold_id'])
        op['principal'] = ident(row['principal'])
        op['units'] = _parse_units(row['units'], 1)
    elif kind == 'commit':
        op['hold_id'] = ident(row['hold_id'])
        op['used_units'] = _parse_units(row['used_units'], 0)
    elif kind == 'cancel':
        op['hold_id'] = ident(row['hold_id'])
    elif kind == 'revoke':
        op['lot_id'] = ident(row['lot_id'])
    return op


def _parse_operations(data):
    require(data, 'operations')
    result = []
    for row in rows(data['operations'], MAX_OPERATIONS):
        result.append(_parse_operation(row))
    return result


def _canonical(op):
    """The retry identity: kind plus all business fields, UTC-microsecond times."""
    if op['kind'] == 'grant':
        payload = {'kind': 'grant', 'at_us': op['at_us'], 'lot_id': op['lot_id'],
                   'principal': op['principal'], 'units': op['units'],
                   'expires_at_us': op['expires_at_us']}
    elif op['kind'] == 'reserve':
        payload = {'kind': 'reserve', 'at_us': op['at_us'], 'hold_id': op['hold_id'],
                   'principal': op['principal'], 'units': op['units']}
    elif op['kind'] == 'commit':
        payload = {'kind': 'commit', 'at_us': op['at_us'], 'hold_id': op['hold_id'],
                   'used_units': op['used_units']}
    elif op['kind'] == 'cancel':
        payload = {'kind': 'cancel', 'at_us': op['at_us'], 'hold_id': op['hold_id']}
    elif op['kind'] == 'revoke':
        payload = {'kind': 'revoke', 'at_us': op['at_us'], 'lot_id': op['lot_id']}
    else:
        payload = {'kind': 'advance', 'at_us': op['at_us']}
    return _dump(payload, separators=(',', ':'))


def _verify_schema(connection):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    # SQLite may create internal ``sqlite_*`` tables; every business table is
    # known exactly, so an unrelated file is never mistaken for a ledger.
    user_tables = {name for name in tables if not name.startswith('sqlite_')}
    if user_tables != _REQUIRED_TABLES:
        raise ValueError('not a supported credit ledger schema')
    row = connection.execute(
        "SELECT value FROM ledger_meta WHERE key = 'schema_version'").fetchone()
    if row is None or row[0] != str(SCHEMA_VERSION):
        raise ValueError('unsupported credit ledger schema')
    if connection.execute(
            "SELECT value FROM ledger_meta WHERE key = 'clock_us'").fetchone() is None:
        raise ValueError('unsupported credit ledger schema')


def _load_clock(connection):
    value = connection.execute(
        "SELECT value FROM ledger_meta WHERE key = 'clock_us'").fetchone()[0]
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError('unsupported credit ledger schema') from error


def _store_clock(connection, value):
    connection.execute("UPDATE ledger_meta SET value = ? WHERE key = 'clock_us'",
                       (str(value),))


def _write_connection(path):
    path = _db_path(path)
    if not path.exists():
        raise ValueError('database does not exist')
    if not path.is_file():
        raise ValueError('database is not a regular file')
    connection = sqlite3.connect(os.fspath(path), isolation_level=None)
    try:
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('BEGIN IMMEDIATE')
        _verify_schema(connection)
    except BaseException:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        connection.close()
        raise
    return connection


def _read_connection(path):
    path = _db_path(path)
    if not path.exists():
        raise ValueError('database does not exist')
    if not path.is_file():
        raise ValueError('database is not a regular file')
    uri = Path(os.path.abspath(os.fspath(path))).as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        connection.execute('BEGIN')
        _verify_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _insert_receipt(connection, op, receipt, canonical):
    connection.execute(
        'INSERT INTO ops (op_id, kind, at_us, canonical, receipt) VALUES (?, ?, ?, ?, ?)',
        (op['op_id'], op['kind'], op['at_us'], canonical, _dump(receipt)))


def _release_allocations(connection, hold_id):
    for lot_id, units, _ in connection.execute(
            'SELECT lot_id, units, position FROM hold_allocations '
            'WHERE hold_id = ? ORDER BY position', (hold_id,)):
        changed = connection.execute(
            'UPDATE lots SET available = available + ? WHERE lot_id = ?',
            (units, lot_id)).rowcount
        if changed != 1:
            raise RuntimeError('hold allocation references a missing lot')


def _invalidate_holds(connection, hold_ids):
    """Invalidate each active hold as a whole and release every source lot."""
    invalidated = []
    for hold_id in sorted(set(hold_ids)):
        row = connection.execute(
            'SELECT status FROM holds WHERE hold_id = ?', (hold_id,)).fetchone()
        if row is None or row[0] != 'active':
            continue
        _release_allocations(connection, hold_id)
        connection.execute(
            "UPDATE holds SET status = 'invalidated' WHERE hold_id = ?", (hold_id,))
        invalidated.append(hold_id)
    return invalidated


def _expire_due(connection, at_us):
    """Retire every active lot with ``expires_at_us <= at_us`` simultaneously.

    First all active holds that reference any due lot are invalidated as a
    whole (releasing their allocations on every source lot), then each due
    lot's remaining unconsumed quantity becomes retired.  Doing the hold pass
    first is what makes a hold spanning a due lot and a still-live lot release
    the live lot's quantity instead of leaving it stuck.
    """
    due = [row[0] for row in connection.execute(
        'SELECT lot_id FROM lots WHERE status = \'active\' AND expires_at_us <= ? '
        'ORDER BY lot_id', (at_us,))]
    if not due:
        return []
    placeholders = ','.join('?' for _ in due)
    hold_ids = [row[0] for row in connection.execute(
        'SELECT DISTINCT a.hold_id FROM hold_allocations a '
        'JOIN holds h ON h.hold_id = a.hold_id '
        'WHERE h.status = \'active\' AND a.lot_id IN (' + placeholders + ')',
        due)]
    invalidated = _invalidate_holds(connection, hold_ids)
    for lot_id in due:
        changed = connection.execute(
            "UPDATE lots SET retired = retired + available, available = 0, "
            "status = 'expired' WHERE lot_id = ? AND status = 'active'",
            (lot_id,)).rowcount
        if changed != 1:
            raise RuntimeError('could not expire a due lot')
    return invalidated


def _grant(connection, op):
    if connection.execute('SELECT 1 FROM lots WHERE lot_id = ?',
                          (op['lot_id'],)).fetchone() is not None:
        raise ValueError('lot id already exists')
    connection.execute(
        "INSERT INTO lots (lot_id, principal, granted, expires_at_us, status, "
        "available, retired) VALUES (?, ?, ?, ?, 'active', ?, 0)",
        (op['lot_id'], op['principal'], op['units'], op['expires_at_us'], op['units']))


def _reserve(connection, op):
    if connection.execute('SELECT 1 FROM holds WHERE hold_id = ?',
                          (op['hold_id'],)).fetchone() is not None:
        raise ValueError('hold id already exists')
    lots = connection.execute(
        "SELECT lot_id, available FROM lots WHERE status = 'active' "
        "AND principal = ? AND available > 0 ORDER BY expires_at_us, lot_id",
        (op['principal'],)).fetchall()
    if sum(available for _, available in lots) < op['units']:
        raise ValueError('insufficient available credit for reservation')
    remaining = op['units']
    allocation = []
    for lot_id, available in lots:
        if remaining == 0:
            break
        taken = min(available, remaining)
        connection.execute(
            'UPDATE lots SET available = available - ? WHERE lot_id = ?',
            (taken, lot_id))
        allocation.append((lot_id, taken))
        remaining -= taken
    if remaining:
        raise RuntimeError('reservation allocation did not cover its units')
    connection.execute(
        "INSERT INTO holds (hold_id, principal, units, status, used_units) "
        "VALUES (?, ?, ?, 'active', NULL)",
        (op['hold_id'], op['principal'], op['units']))
    for position, (lot_id, units) in enumerate(allocation):
        connection.execute(
            'INSERT INTO hold_allocations (hold_id, position, lot_id, units) '
            'VALUES (?, ?, ?, ?)', (op['hold_id'], position, lot_id, units))
    return [{'lot_id': lot_id, 'units': units} for lot_id, units in allocation]


def _commit(connection, op):
    row = connection.execute(
        'SELECT units, status FROM holds WHERE hold_id = ?',
        (op['hold_id'],)).fetchone()
    if row is None:
        raise ValueError('unknown hold id')
    units, status = row
    if status != 'active':
        raise ValueError('hold is not active')
    if op['used_units'] > units:
        raise ValueError('used_units exceeds the hold quantity')
    remaining = op['used_units']
    consumed = []
    for lot_id, allocated, _ in connection.execute(
            'SELECT lot_id, units, position FROM hold_allocations '
            'WHERE hold_id = ? ORDER BY position', (op['hold_id'],)).fetchall():
        taken = min(remaining, allocated)
        connection.execute(
            'UPDATE lots SET available = available + ? WHERE lot_id = ?',
            (allocated - taken, lot_id))
        if taken:
            consumed.append((lot_id, taken))
            remaining -= taken
    if remaining:
        raise RuntimeError('consumption did not cover used_units')
    for position, (lot_id, taken) in enumerate(consumed):
        connection.execute(
            'INSERT INTO consumed_allocations (hold_id, position, lot_id, units) '
            'VALUES (?, ?, ?, ?)', (op['hold_id'], position, lot_id, taken))
    connection.execute(
        "UPDATE holds SET status = 'committed', used_units = ? WHERE hold_id = ?",
        (op['used_units'], op['hold_id']))
    return [{'lot_id': lot_id, 'units': taken} for lot_id, taken in consumed]


def _cancel(connection, op):
    row = connection.execute(
        'SELECT status FROM holds WHERE hold_id = ?', (op['hold_id'],)).fetchone()
    if row is None:
        raise ValueError('unknown hold id')
    if row[0] != 'active':
        raise ValueError('hold is not active')
    _release_allocations(connection, op['hold_id'])
    connection.execute(
        "UPDATE holds SET status = 'cancelled' WHERE hold_id = ?", (op['hold_id'],))


def _revoke(connection, op):
    row = connection.execute(
        'SELECT status FROM lots WHERE lot_id = ?', (op['lot_id'],)).fetchone()
    if row is None:
        raise ValueError('unknown lot id')
    if row[0] != 'active':
        raise ValueError('lot is not active')
    hold_ids = [item[0] for item in connection.execute(
        'SELECT DISTINCT a.hold_id FROM hold_allocations a '
        'JOIN holds h ON h.hold_id = a.hold_id '
        "WHERE h.status = 'active' AND a.lot_id = ?", (op['lot_id'],))]
    invalidated = _invalidate_holds(connection, hold_ids)
    changed = connection.execute(
        "UPDATE lots SET retired = retired + available, available = 0, "
        "status = 'revoked' WHERE lot_id = ? AND status = 'active'",
        (op['lot_id'],)).rowcount
    if changed != 1:
        raise RuntimeError('could not revoke the target lot')
    return invalidated


def _process_new(connection, op):
    """Advance the clock, process expiry, then apply one new operation."""
    clock = _load_clock(connection)
    if clock is not None and op['at_us'] < clock:
        raise ValueError('operation timestamp precedes the ledger clock')
    _store_clock(connection, op['at_us'])
    invalidated = _expire_due(connection, op['at_us'])
    kind = op['kind']
    allocation = []
    consumed = []
    if kind == 'grant':
        _grant(connection, op)
    elif kind == 'reserve':
        allocation = _reserve(connection, op)
    elif kind == 'commit':
        consumed = _commit(connection, op)
    elif kind == 'cancel':
        _cancel(connection, op)
    elif kind == 'revoke':
        invalidated = invalidated + _revoke(connection, op)
    # ``advance`` only advances the clock and processes expiry.
    receipt = {
        'op_id': op['op_id'],
        'kind': kind,
        'at_us': op['at_us'],
        'invalidated_hold_ids': sorted(set(invalidated)),
        'allocation': allocation,
        'consumed_allocation': consumed,
    }
    _insert_receipt(connection, op, receipt, _canonical(op))
    return receipt


def apply_batch(db, source):
    """Validate, execute and commit one batch; return its ordered receipts."""
    data, _ = read(source)
    operations = _parse_operations(data)
    connection = _write_connection(db)
    try:
        receipts = []
        for op in operations:
            canonical = _canonical(op)
            row = connection.execute(
                'SELECT canonical, receipt FROM ops WHERE op_id = ?',
                (op['op_id'],)).fetchone()
            if row is not None:
                if row[0] != canonical:
                    raise ValueError('op_id already exists with different content')
                receipts.append(json.loads(row[1]))
                continue
            receipts.append(_process_new(connection, op))
        connection.execute('COMMIT')
    except BaseException:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise
    finally:
        connection.close()
    return receipts


def _hold_allocations(connection, table, hold_id):
    return [{'lot_id': row[0], 'units': row[1]} for row in connection.execute(
        'SELECT lot_id, units FROM ' + table + ' WHERE hold_id = ? ORDER BY position',
        (hold_id,))]


def build_report(db):
    """Read one consistent committed snapshot without touching the clock."""
    connection = _read_connection(db)
    try:
        clock = _load_clock(connection)
        held = {row[0]: row[1] for row in connection.execute(
            'SELECT a.lot_id, SUM(a.units) FROM hold_allocations a '
            'JOIN holds h ON h.hold_id = a.hold_id '
            "WHERE h.status = 'active' GROUP BY a.lot_id")}
        consumed = {row[0]: row[1] for row in connection.execute(
            'SELECT c.lot_id, SUM(c.units) FROM consumed_allocations c '
            'JOIN holds h ON h.hold_id = c.hold_id '
            "WHERE h.status = 'committed' GROUP BY c.lot_id")}
        lots = []
        for lot_id, principal, granted, expires_at_us, status, available, retired in sorted(
                connection.execute(
                    'SELECT lot_id, principal, granted, expires_at_us, status, '
                    'available, retired FROM lots').fetchall()):
            entry = {
                'lot_id': lot_id,
                'principal': principal,
                'expires_at_us': expires_at_us,
                'status': status,
                'granted': granted,
                'available': available,
                'held': held.get(lot_id, 0),
                'consumed': consumed.get(lot_id, 0),
                'retired': retired,
            }
            if status not in _LOT_STATUSES:
                raise RuntimeError('invalid persisted lot status')
            if any(entry[key] < 0 for key in
                   ('granted', 'available', 'held', 'consumed', 'retired')):
                raise RuntimeError('negative persisted lot balance')
            if entry['granted'] != (entry['available'] + entry['held']
                                    + entry['consumed'] + entry['retired']):
                raise RuntimeError('lot conservation invariant violated')
            lots.append(entry)

        holds = []
        for hold_id, principal, units, status, used_units in sorted(
                connection.execute(
                    'SELECT hold_id, principal, units, status, used_units FROM holds'
                ).fetchall()):
            if status not in _HOLD_STATUSES:
                raise RuntimeError('invalid persisted hold status')
            allocation = _hold_allocations(connection, 'hold_allocations', hold_id)
            if not allocation or sum(item['units'] for item in allocation) != units:
                raise RuntimeError('hold allocation does not match its units')
            if status == 'committed':
                used = used_units
                consumed_allocation = _hold_allocations(
                    connection, 'consumed_allocations', hold_id)
                if used is None or sum(item['units'] for item in consumed_allocation) != used:
                    raise RuntimeError('committed hold consumption mismatch')
            else:
                used = None
                consumed_allocation = []
                if used_units is not None:
                    raise RuntimeError('terminal hold should not carry used_units')
            holds.append({
                'hold_id': hold_id,
                'principal': principal,
                'units': units,
                'status': status,
                'allocation': allocation,
                'used_units': used,
                'consumed_allocation': consumed_allocation,
            })
        connection.execute('COMMIT')
    except BaseException:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise
    finally:
        connection.close()
    return {
        'schema_version': SCHEMA_VERSION,
        'clock_us': clock,
        'lots': lots,
        'holds': holds,
    }


def init_database(db):
    path = _db_path(db)
    if os.path.lexists(os.fspath(path)):
        raise ValueError('database already exists')
    parent = path.parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(os.fspath(path),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        connection = sqlite3.connect(os.fspath(path), isolation_level=None)
        try:
            connection.execute('PRAGMA foreign_keys = ON')
            # ``executescript`` commits any open transaction itself, so the
            # schema is created first and only the metadata rows are wrapped
            # in an explicit transaction.  A failure deletes the just-created
            # file, so no partial ledger survives.
            connection.executescript(_SCHEMA)
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                "INSERT INTO ledger_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),))
            connection.execute(
                "INSERT INTO ledger_meta (key, value) VALUES ('clock_us', NULL)")
            connection.execute('COMMIT')
        except BaseException:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            raise
        finally:
            connection.close()
    except BaseException:
        try:
            os.unlink(os.fspath(path))
        except OSError:
            pass
        raise


def report_snapshot(db, output):
    """Publish the report atomically and return the exact text written."""
    path = _db_path(db)
    if _same_file(path, output):
        raise ValueError('report output must differ from the database')
    report = build_report(path)
    save(output, report)
    return _dump(report, indent=2) + '\n'


def _write_stdout(text):
    # Write bytes so stdout matches the report file byte for byte regardless
    # of the process locale.
    stream = getattr(sys.stdout, 'buffer', None)
    if stream is None:
        sys.stdout.write(text)
        sys.stdout.flush()
    else:
        stream.write(text.encode('utf-8'))
        stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Persistent local prepaid API credit ledger')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='create an empty ledger database')
    init.add_argument('--db', type=Path, required=True)
    apply = commands.add_parser('apply', help='apply one operations batch')
    apply.add_argument('--db', type=Path, required=True)
    apply.add_argument('--source', type=Path, required=True)
    report = commands.add_parser('report', help='export one committed snapshot')
    report.add_argument('--db', type=Path, required=True)
    report.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            init_database(args.db)
            _write_stdout(_dump({'schema_version': SCHEMA_VERSION},
                                separators=(',', ':')) + '\n')
        elif args.command == 'apply':
            receipts = apply_batch(args.db, args.source)
            # The receipt array of a successful batch is the whole success
            # contract; nothing else is printed on stdout.
            _write_stdout(_dump({'receipts': receipts},
                                separators=(',', ':')) + '\n')
        else:
            _write_stdout(report_snapshot(args.db, args.output))
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, RuntimeError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
