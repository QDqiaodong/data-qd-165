"""Out-of-order continuous session change stream (``--mode sessions``).

A metering session is a maximal connected run of accepted events inside one
``(principal, route)`` scope: events are ordered by ``(at_us, id)`` and two
neighbours belong to the same session exactly when their gap is **strictly
smaller** than the scope's ``gap_seconds``.  This is an interval definition,
not a fixed epoch bucket, so an out-of-order insertion can bridge several
sessions and a retraction of a bridging event can split one.

The module deliberately keeps no incremental component index.  Every legal
``apply`` rebuilds the maximal components of the affected state from the
current accepted, non-retracted, non-sealed membership, then applies sealing
to that freshly built table:

* membership changes (acceptance / retraction) and the watermark move are
  staged first, so components, identities, units, boundaries and sealing
  decisions always share one post-operation state;
* a component is sealed in the same transition when
  ``closes_at_us = last_us + gap_us <= F`` with
  ``F = watermark_us - allowed_lateness_us`` (``F = None`` before the first
  watermark, in which case nothing seals);
* sealed members leave the open set forever.  New events satisfy
  ``at_us >= F`` and connectivity is strict, so a later event can never attach
  to a sealed member; late events (``at_us < F``) are registered for identity
  purposes only and never enter any component, which keeps sealed snapshots
  immutable even when a historically connecting event arrives late;
* retractions are evaluated against the current open table, so a split early
  fragment seals in the retraction step itself even when the watermark did not
  move, while a bridged long session keeps the deadline computed from its own
  last member rather than the old short fragment's deadline.

Each transition returns a self-contained change (retracted open identities,
upserted open snapshots and newly sealed snapshots) that lets a downstream
holder of only the previous open table and the sealed table reconstruct the
current state.  Emitted dicts and lists are copies, so later transitions can
never mutate a previously returned change or snapshot.
"""
from .common import ident, number, require, rows
from .model import instant

MAX_POLICIES = 100
MAX_OPERATIONS = 2000
GAP_MAX = 31_536_000
LATENESS_MAX = 31_536_000
UNITS_MAX = 1_000_000_000
MICROS_PER_SECOND = 1_000_000

_EVENT_FIELDS = 'kind id principal route at units'
_RETRACT_FIELDS = 'kind event_id'
_WATERMARK_FIELDS = 'kind at'
_KINDS = ('event', 'retract', 'watermark')


def _session_sort_key(session):
    return (session['principal'], session['route'],
            session['first_us'], session['session_id'])


def _copy_session(session):
    """A fresh JSON-only copy so callers can never mutate internal state."""
    return {
        'session_id': session['session_id'],
        'principal': session['principal'],
        'route': session['route'],
        'first_us': session['first_us'],
        'last_us': session['last_us'],
        'closes_at_us': session['closes_at_us'],
        'units': session['units'],
        'event_ids': list(session['event_ids']),
    }


def parse_policies(raw_policies):
    """Validate the policy array and return ``scope -> gap_us``."""
    policies = {}
    for row in rows(raw_policies, MAX_POLICIES):
        require(row, 'principal route gap_seconds')
        principal = ident(row['principal'])
        route = ident(row['route'])
        gap_seconds = number(row['gap_seconds'], 1, GAP_MAX)
        scope = (principal, route)
        if scope in policies:
            raise ValueError('duplicate session policy scope')
        policies[scope] = gap_seconds * MICROS_PER_SECOND
    return policies


def parse_operation(operation):
    """Validate one operation without touching any accumulator state."""
    if not isinstance(operation, dict):
        raise ValueError('operation must be an object')
    kind = operation.get('kind')
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError('unknown operation kind')
    if kind == 'event':
        require(operation, _EVENT_FIELDS)
        event_id = ident(operation['id'])
        principal = ident(operation['principal'])
        route = ident(operation['route'])
        at_us = instant(operation['at'])
        units = number(operation['units'], 1, UNITS_MAX)
        return ('event', event_id, principal, route, at_us, units)
    if kind == 'retract':
        require(operation, _RETRACT_FIELDS)
        return ('retract', ident(operation['event_id']))
    require(operation, _WATERMARK_FIELDS)
    return ('watermark', instant(operation['at']))


class SessionAccumulator:
    """Incremental, in-memory session maintainer.

    State lives only for the lifetime of the object: no database, background
    service or recovery is involved.  Every legal :meth:`apply` is one atomic
    state transition; an invalid operation raises ``ValueError`` before any
    field, registration, watermark or table changes.
    """

    def __init__(self, policies, allowed_lateness_seconds):
        self._gap_us = parse_policies(policies)
        self._lateness_us = number(allowed_lateness_seconds, 0, LATENESS_MAX) * MICROS_PER_SECOND
        self._watermark_us = None
        self._step = 0
        # Canonical identity registration: id -> (principal, route, at_us, units).
        self._registry = {}
        # Lifecycle of a registered id: 'accepted', 'late' or 'retracted'.
        # Sealed events keep 'accepted' and are additionally listed below.
        self._status = {}
        self._sealed_events = set()
        # Open table: session_id -> snapshot dict (internal copies).
        self._open = {}
        # All sealed snapshots, in sealing arrival order (snapshot sorts them).
        self._sealed = []

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------
    def _cutoff(self, watermark_us=None):
        watermark_us = self._watermark_us if watermark_us is None else watermark_us
        if watermark_us is None:
            return None
        return watermark_us - self._lateness_us

    def snapshot(self):
        """Return a pure, fresh JSON snapshot of the consistent current state."""
        return {
            'watermark_us': self._watermark_us,
            'cutoff_us': self._cutoff(),
            'open_sessions': [_copy_session(session) for session in
                              sorted(self._open.values(), key=_session_sort_key)],
            'sealed_sessions': [_copy_session(session) for session in
                                sorted(self._sealed, key=_session_sort_key)],
        }

    # ------------------------------------------------------------------
    # Component construction
    # ------------------------------------------------------------------
    def _open_members(self, extra=None, remove_id=None):
        """Accepted events currently in the open set as plain records.

        ``extra`` optionally stages one newly accepted event and
        ``remove_id`` stages one retraction; neither touches the registry.
        Sealed, late and retracted identities are excluded, so sealed events
        are never put back into open component construction.
        """
        members = []
        for event_id, (principal, route, at_us, units) in self._registry.items():
            if event_id == remove_id:
                continue
            if self._status.get(event_id) != 'accepted':
                continue
            if event_id in self._sealed_events:
                continue
            members.append((event_id, principal, route, at_us, units))
        if extra is not None:
            members.append(extra)
        return members

    def _build_components(self, members):
        """Rebuild every maximal connected component from plain membership.

        Per scope the members are sorted by ``(at_us, id)``; a new component
        starts exactly when the neighbour gap equals or exceeds the scope gap
        (strictly-less connectivity, no fixed buckets).  The session identity
        is the Python-string-smallest member id, and members, units and
        boundaries all come from that identity's current component.
        """
        by_scope = {}
        for event_id, principal, route, at_us, units in members:
            by_scope.setdefault((principal, route), []).append(
                (at_us, event_id, units))
        open_table = {}
        for (principal, route), rows_ in by_scope.items():
            rows_.sort(key=lambda item: (item[0], item[1]))
            gap_us = self._gap_us[(principal, route)]
            groups = []
            current = []
            previous_at = None
            for at_us, event_id, units in rows_:
                if current and at_us - previous_at >= gap_us:
                    groups.append(current)
                    current = []
                current.append((at_us, event_id, units))
                # Track the last member *inside the current component* so the
                # next comparison is always between two neighbours, never
                # across an already opened gap.
                previous_at = at_us
            if current:
                groups.append(current)
            for group in groups:
                event_ids = sorted(event_id for _, event_id, _ in group)
                first_us = group[0][0]
                last_us = group[-1][0]
                open_table[event_ids[0]] = {
                    'session_id': event_ids[0],
                    'principal': principal,
                    'route': route,
                    'first_us': first_us,
                    'last_us': last_us,
                    'closes_at_us': last_us + gap_us,
                    'units': sum(units for _, _, units in group),
                    'event_ids': event_ids,
                }
        return open_table

    # ------------------------------------------------------------------
    # The single state transition
    # ------------------------------------------------------------------
    def apply(self, operation):
        """Validate and apply one operation, returning this step's change."""
        parsed = parse_operation(operation)
        before_open = self._open
        watermark_effective = self._watermark_us
        extra = None
        remove_id = None
        status = None
        registration = None  # (event_id, canonical, lifecycle) staged for commit
        retract_id = None

        kind = parsed[0]
        if kind == 'event':
            _, event_id, principal, route, at_us, units = parsed
            # Every event, late or duplicate, must fully match a policy first.
            if (principal, route) not in self._gap_us:
                raise ValueError('event has no exact principal-route policy')
            canonical = (principal, route, at_us, units)
            if event_id in self._registry:
                # Exact retries are recognised before any cutoff re-decision,
                # so an original that is late/retracted/sealed stays as it was.
                if self._registry[event_id] != canonical:
                    raise ValueError('conflicting duplicate event id: ' + event_id)
                status = 'duplicate'
            else:
                cutoff = self._cutoff()
                if cutoff is not None and at_us < cutoff:
                    status = 'late'
                    registration = (event_id, canonical, 'late')
                else:
                    status = 'accepted'
                    registration = (event_id, canonical, 'accepted')
                    extra = (event_id, principal, route, at_us, units)
        elif kind == 'retract':
            _, event_id = parsed
            if event_id not in self._registry:
                raise ValueError('retract references unknown event id: ' + event_id)
            if event_id in self._sealed_events:
                # A legal business refusal: sealed sessions never reopen.
                status = 'sealed'
            elif self._status[event_id] == 'accepted':
                status = 'retracted'
                remove_id = event_id
                retract_id = event_id
            else:
                # Already retracted, or first registered as late: no effect.
                status = 'noop'
        else:
            at_us = parsed[1]
            if self._watermark_us is not None and at_us < self._watermark_us:
                raise ValueError('watermark must not move backwards')
            status = 'watermark'
            watermark_effective = at_us

        # Rebuild the whole open table from the staged post-operation
        # membership, then seal on the same state.
        rebuilt = self._build_components(self._open_members(extra, remove_id))
        cutoff = self._cutoff(watermark_effective)
        after_open = {}
        sealed_now = []
        for session_id, session in rebuilt.items():
            if cutoff is not None and session['closes_at_us'] <= cutoff:
                sealed_now.append(session)
            else:
                after_open[session_id] = session
        sealed_now.sort(key=_session_sort_key)
        sealed_ids = {session['session_id'] for session in sealed_now}

        # Diff against the previous open table.  Identities carried by N are
        # sealings, never unrelated retractions; an id split off and replaced
        # by its final open snapshot therefore appears only as an upsert.
        retracted_session_ids = sorted(
            session_id for session_id in before_open
            if session_id not in after_open and session_id not in sealed_ids)
        upserts = []
        for session_id in sorted(after_open, key=lambda sid: _session_sort_key(after_open[sid])):
            session = after_open[session_id]
            previous = before_open.get(session_id)
            if previous is None or previous != session:
                upserts.append(_copy_session(session))

        # Commit: every mutation below happens only after all calculations,
        # so validation failures leave no half-step state behind.
        if registration is not None:
            event_id, canonical, lifecycle = registration
            self._registry[event_id] = canonical
            self._status[event_id] = lifecycle
        if retract_id is not None:
            self._status[retract_id] = 'retracted'
        if kind == 'watermark':
            self._watermark_us = watermark_effective
        self._open = after_open
        for session in sealed_now:
            self._sealed.append(_copy_session(session))
            self._sealed_events.update(session['event_ids'])
        self._step += 1

        return {
            'step': self._step,
            'status': status,
            'retracted_session_ids': retracted_session_ids,
            'upserts': upserts,
            'sealed_sessions': [_copy_session(session) for session in sealed_now],
        }


def sessions_analyze(data):
    """Run ``operations`` in their given business order and collect the trace.

    The input is never reordered by event time and step *k* cannot see later
    operations: each operation is fed to one :class:`SessionAccumulator` in
    array order and every per-step change is appended to ``trace`` verbatim.
    """
    require(data, 'policies allowed_lateness_seconds operations')
    accumulator = SessionAccumulator(data['policies'], data['allowed_lateness_seconds'])
    trace = [accumulator.apply(operation)
             for operation in rows(data['operations'], MAX_OPERATIONS)]
    result = accumulator.snapshot()
    result['trace'] = trace
    return result
