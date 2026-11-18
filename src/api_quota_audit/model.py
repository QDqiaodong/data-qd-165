from datetime import datetime, timezone
from .common import require, ident, number, rows


def instant(value):
    if not isinstance(value, str):
        raise ValueError('timestamp string required')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('invalid ISO timestamp') from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timestamp must include UTC offset')
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def window_start(timestamp_us, window_seconds):
    """UTC epoch anchored window start in epoch seconds, flooring negative epochs."""
    return timestamp_us // (window_seconds * 1_000_000) * window_seconds


def utc_text(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


def breach(used, limit):
    """Excess and breach flag, shared by every window report."""
    return max(0, used - limit), used > limit


def window_record(principal, route, policy_id, window_seconds, limit, start, used, event_ids):
    """One observed window; shared by the audit and warehouse report paths."""
    over_by, exceeded = breach(used, limit)
    return {'principal': principal, 'route': route, 'window_start_utc': utc_text(start),
            'window_end_utc': utc_text(start + window_seconds),
            'policy_id': policy_id, 'limit': limit, 'used': used,
            'over_by': over_by, 'exceeded': exceeded,
            'event_ids': sorted(event_ids)}


def analyze(data):
    require(data, 'policies events')
    policies = {}
    ids = set()
    for row in rows(data['policies'], 1000):
        require(row, 'id principal route window_seconds limit')
        pid, principal, route = ident(row['id']), ident(row['principal']), ident(row['route'])
        key = (principal, route)
        if pid in ids or key in policies:
            raise ValueError('duplicate policy id or scope')
        ids.add(pid)
        policies[key] = {'id': pid, 'window': number(row['window_seconds'], 1, 31536000), 'limit': number(row['limit'], 1, 10**12)}
    events = {}
    for row in rows(data['events'], 100000):
        require(row, 'id principal route at units')
        eid = ident(row['id'])
        normalized = (ident(row['principal']), ident(row['route']), instant(row['at']), number(row['units'], 1, 10**9))
        if eid in events and events[eid] != normalized:
            raise ValueError('conflicting duplicate event id')
        events[eid] = normalized
    windows = {}
    for eid, (principal, route, timestamp, units) in events.items():
        scope = (principal, route)
        if scope not in policies:
            raise ValueError('event has no exact principal-route policy')
        policy = policies[scope]
        start = window_start(timestamp, policy['window'])
        key = (principal, route, start)
        entry = windows.setdefault(key, {'policy_id': policy['id'], 'window_seconds': policy['window'], 'limit': policy['limit'], 'used': 0, 'event_ids': []})
        entry['used'] += units
        entry['event_ids'].append(eid)
    output = []
    for (principal, route, start), value in sorted(windows.items()):
        output.append(window_record(principal, route, value['policy_id'],
                                    value['window_seconds'], value['limit'], start,
                                    value['used'], value['event_ids']))
    return {'windows': output, 'window_count': len(output), 'exceeded_count': sum(x['exceeded'] for x in output),
            'deduplicated_event_count': len(events)}
