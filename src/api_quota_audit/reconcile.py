"""Offline interval-meter evidence reconciliation (``--mode reconcile``).

The unknown is one integer usage value per atomic slot between consecutive
timeline instants.  Every observation is a bound on a contiguous sum, so with
the prefix quantities ``P[0] = 0`` and ``P[j] = x[0] + ... + x[j-1]`` each
observation becomes a difference constraint ``P[v] - P[u] <= w``:

* an ``upper`` side becomes the edge ``u -> v`` with weight ``upper``;
* a ``lower`` side becomes the reversed edge ``v -> u`` with weight
  ``-lower`` (so ``P[u] - P[v] <= -lower``);
* slot ``i`` contributes ``i -> i+1`` with weight ``max_slot_units`` and
  ``i+1 -> i`` with weight ``0`` (``x[i] >= 0``).

The constraint system is feasible exactly when the directed graph has no
negative-weight cycle.  Meters are checked first, then invoices are added to
the very same graph: because the meter/slot graph is already acyclic in this
sense, any negative cycle that appears afterwards necessarily uses an invoice
edge.  A witness cycle is extracted from the predecessor chain of a
Bellman-Ford relaxation, so every emitted edge is an original edge (never a
closure shortcut and never a virtual super-source edge), the edges connect
head to tail, and their integer weights sum below zero.

When feasible, all-pairs shortest path distances give the exact extremum of
any query sum: the largest feasible value of ``P[end] - P[start]`` is
``dist(start, end)`` and the smallest is ``-dist(end, start)``.  The same
distance vector is itself a complete assignment: ``dist(source, v)`` obeys
``P[v] <= P[u] + w`` for every constraint edge, so the slot increments are
exactly a feasible allocation attaining the bound.  All arithmetic is exact
integer arithmetic; nothing is approximated, enumerated or delegated to an
external solver.
"""
from .common import require, ident, number, rows
from .model import instant

MAX_TIMELINE = 161
MAX_SLOT_UNITS = 10 ** 12
MAX_UNITS = 10 ** 15
MAX_METERS = 1200
MAX_INVOICES = 200
MAX_QUERIES = 40

INF = 10 ** 30


def _parse_meters(raw, n):
    parsed = []
    seen = set()
    for row in rows(raw, MAX_METERS):
        require(row, 'id start end lower upper')
        meter_id = ident(row['id'])
        start = number(row['start'], 0, n)
        end = number(row['end'], 0, n)
        lower = number(row['lower'], 0, MAX_UNITS)
        upper = number(row['upper'], 0, MAX_UNITS)
        if start >= end:
            raise ValueError('meter range must be nonempty')
        if lower > upper:
            raise ValueError('meter lower exceeds upper')
        if meter_id in seen:
            raise ValueError('duplicate meter id: ' + meter_id)
        seen.add(meter_id)
        parsed.append((meter_id, start, end, lower, upper))
    parsed.sort(key=lambda item: item[0])
    return parsed


def _parse_invoices(raw, n):
    parsed = []
    seen = set()
    for row in rows(raw, MAX_INVOICES):
        require(row, 'id start end units')
        invoice_id = ident(row['id'])
        start = number(row['start'], 0, n)
        end = number(row['end'], 0, n)
        units = number(row['units'], 0, MAX_UNITS)
        if start >= end:
            raise ValueError('invoice range must be nonempty')
        if invoice_id in seen:
            raise ValueError('duplicate invoice id: ' + invoice_id)
        seen.add(invoice_id)
        parsed.append((invoice_id, start, end, units))
    parsed.sort(key=lambda item: item[0])
    return parsed


def _parse_queries(raw, n):
    parsed = []
    seen = set()
    for row in rows(raw, MAX_QUERIES):
        require(row, 'id start end')
        query_id = ident(row['id'])
        start = number(row['start'], 0, n)
        end = number(row['end'], 0, n)
        if start >= end:
            raise ValueError('query range must be nonempty')
        if query_id in seen:
            raise ValueError('duplicate query id: ' + query_id)
        seen.add(query_id)
        parsed.append((query_id, start, end))
    parsed.sort(key=lambda item: item[0])
    return parsed


def parse_reconcile_input(data):
    """Validate the whole document before any business judgement."""
    require(data, 'timeline max_slot_units meters invoices queries')
    raw_timeline = rows(data['timeline'], MAX_TIMELINE)
    if len(raw_timeline) < 2:
        raise ValueError('timeline requires at least two instants')
    stamps = [instant(value) for value in raw_timeline]
    for earlier, later in zip(stamps, stamps[1:]):
        if not earlier < later:
            raise ValueError('timeline instants must be strictly increasing')
    n = len(stamps) - 1
    max_slot_units = number(data['max_slot_units'], 1, MAX_SLOT_UNITS)
    meters = _parse_meters(data['meters'], n)
    invoices = _parse_invoices(data['invoices'], n)
    queries = _parse_queries(data['queries'], n)
    return n, max_slot_units, meters, invoices, queries


def _build_edges(n, max_slot_units, meters, invoices):
    """Return (slot + meter edges, invoice edges) using original edge data."""
    slot_edges = []
    for index in range(n):
        ref = str(index)
        slot_edges.append((index, index + 1, max_slot_units, 'slot', ref, 'upper'))
        slot_edges.append((index + 1, index, 0, 'slot', ref, 'lower'))
    meter_edges = []
    for meter_id, start, end, lower, upper in meters:
        meter_edges.append((start, end, upper, 'meter', meter_id, 'upper'))
        meter_edges.append((end, start, -lower, 'meter', meter_id, 'lower'))
    invoice_edges = []
    for invoice_id, start, end, units in invoices:
        invoice_edges.append((start, end, units, 'invoice', invoice_id, 'upper'))
        invoice_edges.append((end, start, -units, 'invoice', invoice_id, 'lower'))
    return slot_edges + meter_edges, invoice_edges


def _find_negative_cycle(edges, node_count):
    """Return a cycle as an ordered list of original edges, or None.

    Distances start at zero for every node, which is equivalent to relaxing
    from a virtual super source without ever materializing its edges.  A
    relaxation on the final pass proves a negative cycle; the predecessor
    chain then yields a simple cycle of original edges.
    """
    distance = [0] * node_count
    predecessor = [-1] * node_count
    changed = -1
    for _ in range(node_count):
        changed = -1
        for edge_index, edge in enumerate(edges):
            source, target, weight = edge[0], edge[1], edge[2]
            candidate = distance[source] + weight
            if candidate < distance[target]:
                distance[target] = candidate
                predecessor[target] = edge_index
                changed = target
    if changed == -1:
        return None
    node = changed
    for _ in range(node_count):
        node = edges[predecessor[node]][0]
    first = node
    cycle = []
    current = node
    while True:
        edge_index = predecessor[current]
        cycle.append(edges[edge_index])
        current = edges[edge_index][0]
        if current == first:
            break
    cycle.reverse()
    if sum(edge[2] for edge in cycle) >= 0:
        raise RuntimeError('extracted predecessor cycle is not negative')
    return cycle


def _all_pairs_shortest_paths(node_count, edges):
    """Exact all-pairs distances; feasible inputs have no negative cycle."""
    distance = [[INF] * node_count for _ in range(node_count)]
    for node in range(node_count):
        distance[node][node] = 0
    for source, target, weight in ((edge[0], edge[1], edge[2]) for edge in edges):
        if weight < distance[source][target]:
            distance[source][target] = weight
    for middle in range(node_count):
        through = distance[middle]
        for source in range(node_count):
            base = distance[source][middle]
            if base == INF:
                continue
            row = distance[source]
            for target in range(node_count):
                step = through[target]
                if step == INF:
                    continue
                candidate = base + step
                if candidate < row[target]:
                    row[target] = candidate
    return distance


def _allocation(distance_from, n):
    """Complete slot assignment from one shortest-path potential vector."""
    return [distance_from[index + 1] - distance_from[index] for index in range(n)]


def _edge_record(edge):
    return {
        'kind': edge[3],
        'ref': edge[4],
        'side': edge[5],
        'from': edge[0],
        'to': edge[1],
        'weight': edge[2],
    }


def reconcile_analyze(data):
    """Validate, decide feasibility, bound every query and build witnesses."""
    n, max_slot_units, meters, invoices, queries = parse_reconcile_input(data)
    slot_and_meter_edges, invoice_edges = _build_edges(
        n, max_slot_units, meters, invoices)

    node_count = n + 1
    cycle = _find_negative_cycle(slot_and_meter_edges, node_count)
    if cycle is not None:
        # The certificate intentionally uses only meter and slot edges, so a
        # reader can verify it without looking at any invoice.
        return {
            'status': 'meter_conflict',
            'ranges': [],
            'certificate': [_edge_record(edge) for edge in cycle],
        }

    full_edges = slot_and_meter_edges + invoice_edges
    cycle = _find_negative_cycle(full_edges, node_count)
    if cycle is not None:
        # The meter/slot subsystem was feasible, so every newly negative
        # cycle necessarily contains at least one invoice edge.
        if not any(edge[3] == 'invoice' for edge in cycle):
            raise RuntimeError('invoice conflict certificate lacks an invoice edge')
        return {
            'status': 'invoice_conflict',
            'ranges': [],
            'certificate': [_edge_record(edge) for edge in cycle],
        }

    distance = _all_pairs_shortest_paths(node_count, full_edges)
    ranges = []
    for query_id, start, end in queries:
        maximum = distance[start][end]
        minimum = -distance[end][start]
        max_allocation = _allocation(distance[start], n)
        min_allocation = _allocation(distance[end], n)
        ranges.append({
            'id': query_id,
            'min_units': minimum,
            'max_units': maximum,
            'min_allocation': min_allocation,
            'max_allocation': max_allocation,
        })
    return {'status': 'feasible', 'ranges': ranges, 'certificate': []}
