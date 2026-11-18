"""Immutable, sequence-numbered API usage and quota-policy fact warehouse.

The warehouse answers two independent historical questions about the same
facts:

* **knowledge cutoff** — "what did operations know after batch N?"  Every
  successful ``ingest`` batch gets one consecutive knowledge sequence number
  (``seq``).  Versions with a larger ``seq`` are invisible to a query whose
  ``max_seq`` is smaller.
* **event-time cutoff** — "which events had already happened before T?"
  Events are compared by their business timestamp only after the knowledge
  snapshot has selected the highest revision of every event identity.

Both cutoffs are applied in that order.  An event whose newest known revision
moved past (or before) the event-time cutoff is *absent*; an older revision is
never resurrected as a fallback.  Policies are likewise resolved per identity
inside ``seq <= max_seq`` and only then used to attribute each surviving event
to exactly one historical policy version.

Storage is a single SQLite file holding immutable version rows plus frozen
batch receipts.  Query time rebuilds a snapshot from those rows inside one
read-only transaction; no per-seq projection is materialised, so a historical
snapshot can never be contaminated by a later policy or usage correction.
The fixed-window arithmetic itself is reused from :mod:`api_quota_audit.model`
(UTC epoch anchoring, floor division for negative epochs and the exact
``over_by``/``exceeded`` definition).

All revisions of one batch, its sequence number and its receipt commit in a
single SQLite transaction.  A rejected batch therefore leaves no visible
trace and the previous knowledge snapshot stays unchanged.
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .common import ident, number, read, require, rows, save
from .model import breach, instant, utc_text, window_start

SCHEMA_VERSION = 1
MAX_REVISIONS = 2000
MAX_EVENT_UNITS = 10 ** 9
MAX_WINDOW_SECONDS = 31_536_000
MAX_LIMIT = 10 ** 12

_REQUIRED_TABLES = {'warehouse_meta', 'batches', 'event_versions', 'policy_versions'}

# Version rows are append-only.  ``seq`` is the knowledge sequence of the
# batch that introduced the version; ``(id, revision)`` is the immutable
# identity of a version.  Deleted rows carry no business payload, which keeps
# "a withdrawal version" strictly distinguishable from a zero payload.
_SCHEMA = """
CREATE TABLE warehouse_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE batches (
    seq INTEGER PRIMARY KEY CHECK (seq >= 1),
    batch_id TEXT NOT NULL UNIQUE,
    canonical TEXT NOT NULL,
    receipt TEXT NOT NULL,
    event_revision_count INTEGER NOT NULL CHECK (event_revision_count >= 0),
    policy_revision_count INTEGER NOT NULL CHECK (policy_revision_count >= 0)
);
CREATE TABLE event_versions (
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    seq INTEGER NOT NULL REFERENCES batches(seq),
    deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
    principal TEXT,
    route TEXT,
    at_us INTEGER,
    units INTEGER,
    PRIMARY KEY (event_id, revision),
    CHECK ((deleted = 1 AND principal IS NULL AND route IS NULL
            AND at_us IS NULL AND units IS NULL)
        OR (deleted = 0 AND principal IS NOT NULL AND route IS NOT NULL
            AND at_us IS NOT NULL AND units IS NOT NULL))
);
CREATE TABLE policy_versions (
    policy_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    seq INTEGER NOT NULL REFERENCES batches(seq),
    deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
    principal TEXT,
    route TEXT,
    valid_from_us INTEGER,
    valid_to_us INTEGER,
    window_seconds INTEGER,
    limit_units INTEGER,
    PRIMARY KEY (policy_id, revision),
    CHECK ((deleted = 1 AND principal IS NULL AND route IS NULL
            AND valid_from_us IS NULL AND valid_to_us IS NULL
            AND window_seconds IS NULL AND limit_units IS NULL)
        OR (deleted = 0 AND principal IS NOT NULL AND route IS NOT NULL
            AND valid_from_us IS NOT NULL AND window_seconds IS NOT NULL
            AND limit_units IS NOT NULL
            AND (valid_to_us IS NULL OR valid_to_us > valid_from_us)))
);
CREATE INDEX event_versions_seq ON event_versions(seq);
CREATE INDEX policy_versions_seq ON policy_versions(seq);
"""

_EVENT_FIELDS = {
    True: 'event_id revision deleted',
    False: 'event_id revision deleted principal route at units',
}
_POLICY_FIELDS = {
    True: 'policy_id revision deleted',
    False: 'policy_id revision deleted principal route valid_from valid_to '
           'window_seconds limit',
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
    return os.path.realpath(os.fspath(left)) == os.path.realpath(os.fspath(second))


def _dump(value, **options):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, **options)


def _revision(value):
    # Revisions are positive integers; bool and float are not integers here.
    if type(value) is not int or not 1 <= value <= 2 ** 63 - 1:
        raise ValueError('revision must be a positive integer')
    return value


def _deleted(value):
    if type(value) is not bool:
        raise ValueError('deleted must be a boolean')
    return value


def _parse_event(row):
    if not isinstance(row, dict):
        raise ValueError('event revision must be an object')
    deleted = _deleted(row.get('deleted'))
    require(row, _EVENT_FIELDS[deleted])
    record = {'event_id': ident(row['event_id']),
              'revision': _revision(row['revision']),
              'deleted': deleted}
    if not deleted:
        record['principal'] = ident(row['principal'])
        record['route'] = ident(row['route'])
        record['at_us'] = instant(row['at'])
        record['units'] = number(row['units'], 1, MAX_EVENT_UNITS)
    return record


def _parse_policy(row):
    if not isinstance(row, dict):
        raise ValueError('policy revision must be an object')
    deleted = _deleted(row.get('deleted'))
    require(row, _POLICY_FIELDS[deleted])
    record = {'policy_id': ident(row['policy_id']),
              'revision': _revision(row['revision']),
              'deleted': deleted}
    if not deleted:
        record['principal'] = ident(row['principal'])
        record['route'] = ident(row['route'])
        valid_from = instant(row['valid_from'])
        if row['valid_to'] is None:
            valid_to = None
        else:
            valid_to = instant(row['valid_to'])
            if not valid_from < valid_to:
                raise ValueError('valid_to must be after valid_from')
        record['valid_from_us'] = valid_from
        record['valid_to_us'] = valid_to
        record['window_seconds'] = number(
            row['window_seconds'], 1, MAX_WINDOW_SECONDS)
        record['limit'] = number(row['limit'], 1, MAX_LIMIT)
    return record


def _sort_unique(records, id_key):
    records = sorted(records, key=lambda item: (item[id_key], item['revision']))
    previous = None
    for record in records:
        key = (record[id_key], record['revision'])
        if key == previous:
            raise ValueError('duplicate %s/revision in one batch' % id_key[:-3])
        previous = key
    return records


def _canonical(batch_id, events, policies):
    """Frozen retry identity: sorted versions with UTC-microsecond times."""
    return _dump({'batch_id': batch_id,
                  'event_revisions': events,
                  'policy_revisions': policies},
                 separators=(',', ':'))


def _parse_batch(data):
    require(data, 'batch_id event_revisions policy_revisions')
    batch_id = ident(data['batch_id'])
    events = [_parse_event(row) for row in rows(data['event_revisions'], MAX_REVISIONS)]
    policies = [_parse_policy(row) for row in rows(data['policy_revisions'], MAX_REVISIONS)]
    events = _sort_unique(events, 'event_id')
    policies = _sort_unique(policies, 'policy_id')
    return batch_id, events, policies, _canonical(batch_id, events, policies)


def _verify_schema(connection):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if {name for name in tables if not name.startswith('sqlite_')} != _REQUIRED_TABLES:
        raise ValueError('not a supported usage warehouse schema')
    row = connection.execute(
        "SELECT value FROM warehouse_meta WHERE key = 'schema_version'").fetchone()
    if row is None or row[0] != str(SCHEMA_VERSION):
        raise ValueError('unsupported usage warehouse schema')


def _existing_heads(connection, table, id_column, ids):
    """Return ``id -> (top_revision, top_is_deleted)`` for each existing id."""
    result = {value: (0, False) for value in ids}
    ordered = sorted(ids)
    for start in range(0, len(ordered), 400):
        chunk = ordered[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        query = (
            'SELECT v.{id}, v.revision, v.deleted FROM {table} v JOIN '
            '(SELECT {id} AS key, MAX(revision) AS top FROM {table} '
            'WHERE {id} IN ({marks}) GROUP BY {id}) m '
            'ON m.key = v.{id} AND m.top = v.revision'
        ).format(id=id_column, table=table, marks=marks)
        for value, revision, deleted in connection.execute(query, chunk):
            result[value] = (revision, bool(deleted))
    return result


def _check_continuity(records, existing, id_key):
    grouped = {}
    for record in records:
        grouped.setdefault(record[id_key], []).append(record)
    for value in sorted(grouped):
        top, head_deleted = existing[value]
        expected = top + 1
        for record in grouped[value]:
            if record['revision'] != expected:
                raise ValueError(
                    'revision must continue contiguously for ' + value)
            if record['deleted'] and expected == 1:
                raise ValueError('the first revision of an identity cannot be a deletion')
            if record['deleted'] and head_deleted:
                raise ValueError('identity is already withdrawn: ' + value)
            head_deleted = record['deleted']
            expected += 1


def _insert_versions(connection, seq, events, policies):
    for record in events:
        connection.execute(
            'INSERT INTO event_versions (event_id, revision, seq, deleted, '
            'principal, route, at_us, units) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (record['event_id'], record['revision'], seq, int(record['deleted']),
             record.get('principal'), record.get('route'),
             record.get('at_us'), record.get('units')))
    for record in policies:
        connection.execute(
            'INSERT INTO policy_versions (policy_id, revision, seq, deleted, '
            'principal, route, valid_from_us, valid_to_us, window_seconds, '
            'limit_units) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (record['policy_id'], record['revision'], seq, int(record['deleted']),
             record.get('principal'), record.get('route'),
             record.get('valid_from_us'), record.get('valid_to_us'),
             record.get('window_seconds'), record.get('limit')))


def _event_heads(connection):
    rows_ = connection.execute(
        'SELECT e.event_id, e.revision, e.principal, e.route, e.at_us, e.units '
        'FROM event_versions e WHERE e.deleted = 0 AND e.revision = '
        '(SELECT MAX(x.revision) FROM event_versions x '
        ' WHERE x.event_id = e.event_id)')
    return {row[0]: {'event_id': row[0], 'revision': row[1], 'principal': row[2],
                     'route': row[3], 'at_us': row[4], 'units': row[5]}
            for row in rows_}


def _policy_heads(connection):
    rows_ = connection.execute(
        'SELECT p.policy_id, p.revision, p.principal, p.route, p.valid_from_us, '
        'p.valid_to_us, p.window_seconds, p.limit_units '
        'FROM policy_versions p WHERE p.deleted = 0 AND p.revision = '
        '(SELECT MAX(x.revision) FROM policy_versions x '
        ' WHERE x.policy_id = p.policy_id)')
    return {row[0]: {'policy_id': row[0], 'revision': row[1], 'principal': row[2],
                     'route': row[3], 'valid_from_us': row[4], 'valid_to_us': row[5],
                     'window_seconds': row[6], 'limit': row[7]}
            for row in rows_}


def _policy_scopes(policies):
    scopes = {}
    for policy in policies.values():
        scopes.setdefault((policy['principal'], policy['route']), []).append(policy)
    for scope, items in scopes.items():
        items.sort(key=lambda item: (item['valid_from_us'],
                                     item['valid_to_us'] is None,
                                     item['valid_to_us'] or 0,
                                     item['policy_id'], item['revision']))
    return scopes


def _validate_final_state(connection):
    """Validate overlap and event coverage over the whole committed head state.

    Only the highest revision of each identity takes part.  ``valid_to=None``
    is a half-open interval ending at infinity; touching endpoints do not
    overlap.
    """
    events = _event_heads(connection)
    policies = _policy_heads(connection)
    scopes = _policy_scopes(policies)
    for scope, items in scopes.items():
        previous_end = None
        for index, policy in enumerate(items):
            if index and (previous_end is None
                          or policy['valid_from_us'] < previous_end):
                raise ValueError(
                    'overlapping policy effective intervals for scope: %s/%s' % scope)
            previous_end = policy['valid_to_us']
    for event in events.values():
        at_us = event['at_us']
        matches = [policy for policy in scopes.get(
            (event['principal'], event['route']), ())
            if policy['valid_from_us'] <= at_us
            and (policy['valid_to_us'] is None or at_us < policy['valid_to_us'])]
        if len(matches) != 1:
            raise ValueError(
                'event has no unique matching policy: ' + event['event_id'])


def create_database(db):
    """Create a brand-new warehouse; never overwrite an existing path."""
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
            connection.executescript(_SCHEMA)
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                "INSERT INTO warehouse_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),))
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
    return {'schema_version': SCHEMA_VERSION}


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


def ingest_batch(db, source):
    """Validate and commit one batch, returning its frozen receipt.

    A retry with the same ``batch_id`` and the same canonical content returns
    the stored receipt without allocating a sequence number and without
    re-validating revisions against newer heads.
    """
    data, _ = read(source)
    batch_id, events, policies, canonical = _parse_batch(data)
    connection = _write_connection(db)
    try:
        stored = connection.execute(
            'SELECT canonical, receipt FROM batches WHERE batch_id = ?',
            (batch_id,)).fetchone()
        if stored is not None:
            if stored[0] != canonical:
                raise ValueError('batch_id already exists with different content')
            connection.execute('ROLLBACK')
            return json.loads(stored[1])
        seq = connection.execute(
            'SELECT COALESCE(MAX(seq), 0) FROM batches').fetchone()[0] + 1
        _check_continuity(
            events, _existing_heads(connection, 'event_versions', 'event_id',
                                    {record['event_id'] for record in events}),
            'event_id')
        _check_continuity(
            policies, _existing_heads(connection, 'policy_versions', 'policy_id',
                                      {record['policy_id'] for record in policies}),
            'policy_id')
        receipt = {'batch_id': batch_id, 'seq': seq,
                   'event_revision_count': len(events),
                   'policy_revision_count': len(policies)}
        connection.execute(
            'INSERT INTO batches (seq, batch_id, canonical, receipt, '
            'event_revision_count, policy_revision_count) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (seq, batch_id, canonical, _dump(receipt), receipt['event_revision_count'],
             receipt['policy_revision_count']))
        _insert_versions(connection, seq, events, policies)
        # Validation happens only after the whole batch is visible: linked
        # event moves plus policy replacements and intermediate states of the
        # same batch are never judged.
        _validate_final_state(connection)
        connection.execute('COMMIT')
    except BaseException:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise
    finally:
        connection.close()
    return receipt


def _parse_cutoff(max_seq, event_before):
    if type(max_seq) is not int or max_seq < 0:
        raise ValueError('max_seq must be a non-negative integer')
    return instant(event_before)


def _select_heads(connection, max_seq):
    """Select every identity's newest version with ``seq <= max_seq`` only.

    The newest revision is chosen before anything is said about deletion: a
    withdrawn head means the identity does not exist in that snapshot, and an
    older revision must never be used instead.
    """
    events = connection.execute(
        'SELECT e.event_id, e.revision, e.deleted, e.principal, e.route, '
        'e.at_us, e.units FROM event_versions e WHERE e.seq <= ? AND '
        'e.revision = (SELECT MAX(x.revision) FROM event_versions x '
        ' WHERE x.event_id = e.event_id AND x.seq <= ?)',
        (max_seq, max_seq))
    policies = connection.execute(
        'SELECT p.policy_id, p.revision, p.deleted, p.principal, p.route, '
        'p.valid_from_us, p.valid_to_us, p.window_seconds, p.limit_units '
        'FROM policy_versions p WHERE p.seq <= ? AND p.revision = '
        '(SELECT MAX(x.revision) FROM policy_versions x '
        ' WHERE x.policy_id = p.policy_id AND x.seq <= ?)',
        (max_seq, max_seq))
    return events, policies


def _match_policy(scopes, event):
    matches = [policy for policy in scopes.get(
        (event['principal'], event['route']), ())
        if policy['valid_from_us'] <= event['at_us']
        and (policy['valid_to_us'] is None
             or event['at_us'] < policy['valid_to_us'])]
    if len(matches) != 1:
        raise ValueError(
            'historical event has no unique matching policy: ' + event['event_id'])
    return matches[0]


def build_report(db, max_seq, event_before):
    """Build the double-cutoff report from one read-only consistent snapshot."""
    before_us = _parse_cutoff(max_seq, event_before)
    connection = _read_connection(db)
    try:
        # Sequence bound and both version sets are read in the same
        # transaction, so a concurrent append can never split the snapshot.
        current = connection.execute(
            'SELECT COALESCE(MAX(seq), 0) FROM batches').fetchone()[0]
        if max_seq > current:
            raise ValueError('max_seq exceeds the current knowledge sequence')
        if max_seq == 0:
            report = {'schema_version': SCHEMA_VERSION, 'max_seq': max_seq,
                      'event_before_us': before_us, 'windows': [],
                      'window_count': 0, 'exceeded_count': 0,
                      'selected_event_count': 0}
        else:
            events, policies = _select_heads(connection, max_seq)
            heads = {}
            for event_id, revision, deleted, principal, route, at_us, units in events:
                # The knowledge cutoff chose this revision first; the event
                # time cutoff is applied to that head only, so a newer head
                # that moved past the cutoff can never fall back to an older
                # revision.
                if deleted or at_us >= before_us:
                    continue
                heads[event_id] = {'event_id': event_id, 'revision': revision,
                                   'principal': principal, 'route': route,
                                   'at_us': at_us, 'units': units}
            known = {}
            for (policy_id, revision, deleted, principal, route, vfrom, vto,
                 window, limit) in policies:
                if deleted:
                    continue
                known[policy_id] = {'policy_id': policy_id, 'revision': revision,
                                    'principal': principal, 'route': route,
                                    'valid_from_us': vfrom, 'valid_to_us': vto,
                                    'window_seconds': window, 'limit': limit}
            scopes = _policy_scopes(known)
            windows = {}
            for event_id in sorted(heads):
                event = heads[event_id]
                policy = _match_policy(scopes, event)
                start = window_start(event['at_us'], policy['window_seconds'])
                key = (event['principal'], event['route'], policy['policy_id'],
                       policy['revision'], start)
                entry = windows.setdefault(
                    key, {'policy': policy, 'used': 0, 'event_versions': []})
                entry['used'] += event['units']
                entry['event_versions'].append(
                    {'event_id': event_id, 'revision': event['revision']})
            output = []
            for principal, route, policy_id, policy_revision, start in sorted(windows):
                entry = windows[(principal, route, policy_id, policy_revision, start)]
                policy = entry['policy']
                # The aggregation numbers and UTC text formats come from the
                # same helpers as ``model.analyze``; only the version identity
                # and the event-version list are warehouse specific.
                over_by, exceeded = breach(entry['used'], policy['limit'])
                output.append({
                    'principal': principal,
                    'route': route,
                    'policy_id': policy_id,
                    'policy_revision': policy_revision,
                    'window_start_utc': utc_text(start),
                    'window_end_utc': utc_text(start + policy['window_seconds']),
                    'limit': policy['limit'],
                    'used': entry['used'],
                    'over_by': over_by,
                    'exceeded': exceeded,
                    'event_versions': sorted(
                        entry['event_versions'],
                        key=lambda item: item['event_id']),
                })
            report = {'schema_version': SCHEMA_VERSION, 'max_seq': max_seq,
                      'event_before_us': before_us, 'windows': output,
                      'window_count': len(output),
                      'exceeded_count': sum(1 for item in output if item['exceeded']),
                      'selected_event_count': len(heads)}
        connection.execute('COMMIT')
    except BaseException:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise
    finally:
        connection.close()
    return report


def query_warehouse(db, max_seq, event_before, output):
    """Publish the double-cutoff report atomically and return it."""
    path = _db_path(db)
    if _same_file(path, output):
        raise ValueError('query output must differ from the database')
    report = build_report(path, max_seq, event_before)
    save(output, report)
    return report


def _write_stdout(text):
    stream = getattr(sys.stdout, 'buffer', None)
    if stream is None:
        sys.stdout.write(text)
        sys.stdout.flush()
    else:
        stream.write(text.encode('utf-8'))
        stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Immutable API usage and quota-policy fact warehouse')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='create a new warehouse database')
    create.add_argument('--db', type=Path, required=True)
    ingest = commands.add_parser('ingest', help='commit one revision batch')
    ingest.add_argument('--db', type=Path, required=True)
    ingest.add_argument('--source', type=Path, required=True)
    query = commands.add_parser('query', help='publish one double-cutoff report')
    query.add_argument('--db', type=Path, required=True)
    query.add_argument('--max-seq', type=int, required=True)
    query.add_argument('--event-before', required=True)
    query.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'create':
            report = create_database(args.db)
            _write_stdout(_dump(report, separators=(',', ':')) + '\n')
        elif args.command == 'ingest':
            receipt = ingest_batch(args.db, args.source)
            _write_stdout(_dump(receipt, separators=(',', ':')) + '\n')
        else:
            report = query_warehouse(args.db, args.max_seq, args.event_before,
                                     args.output)
            _write_stdout(_dump(report, indent=2) + '\n')
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, RuntimeError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
