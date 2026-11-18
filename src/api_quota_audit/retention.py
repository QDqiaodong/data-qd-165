"""Offline shared-capacity retention audit (``--mode retention``).

Every unit request occupies exactly one slot in one principal window and one
slot in one route window.  Choosing the requests to retain is solved exactly
as a lexicographic minimum-cost maximum flow on this directed network::

    source -> principal window -> request in -> request out -> route window -> sink

* source -> principal window and route window -> sink carry the policy limit
  as capacity, so both sides constrain every retained request at once;
* each request is an indivisible unit represented by its capacity-one
  in -> out edge, which also carries an integer cost encoding the three
  business objectives in strictly separated weight bands;
* a unit of flow can only pass through both sides of one request, so a
  request is never counted twice and no slot is double booked.

Successive shortest augmenting paths with Dijkstra and node potentials run in
polynomial time: there is no subset enumeration and no external optimizer.
An appeal reserves its request first (one slot is removed from each of its
two windows) and the remaining network is solved independently, so appeals
can never contaminate each other.
"""
import heapq

from .common import require, ident, number, rows
from .model import instant

PRIORITY_MAX = 1_000_000_000
LIMIT_MAX = 400
WINDOW_MAX = 31_536_000
POLICY_ROWS = 100
REQUEST_ROWS = 400
APPEAL_ROWS = 20


class RequestRecord:
    __slots__ = ('id', 'principal', 'route', 'at', 'priority')

    def __init__(self, request_id, principal, route, at, priority):
        self.id = request_id
        self.principal = principal
        self.route = route
        self.at = at
        self.priority = priority


def _parse_policies(raw_policies, scope_field):
    parsed = {}
    for row in rows(raw_policies, POLICY_ROWS):
        require(row, scope_field + ' window_seconds limit')
        scope = ident(row[scope_field])
        if scope in parsed:
            raise ValueError('duplicate ' + scope_field + ' policy scope')
        window_seconds = number(row['window_seconds'], 1, WINDOW_MAX)
        limit = number(row['limit'], 1, LIMIT_MAX)
        parsed[scope] = (window_seconds, limit)
    return parsed


def parse_retention_input(data):
    """Validate a retention document and return normalized, sorted records."""
    require(data, 'principal_policies route_policies requests appeals')
    principal_policies = _parse_policies(data['principal_policies'], 'principal')
    route_policies = _parse_policies(data['route_policies'], 'route')

    request_map = {}
    for row in rows(data['requests'], REQUEST_ROWS):
        require(row, 'id principal route at priority')
        request_id = ident(row['id'])
        normalized = (
            ident(row['principal']),
            ident(row['route']),
            instant(row['at']),
            number(row['priority'], 0, PRIORITY_MAX),
        )
        if request_id in request_map and request_map[request_id] != normalized:
            raise ValueError('conflicting duplicate request id')
        request_map[request_id] = normalized

    records = []
    for request_id in sorted(request_map):
        principal, route, at, priority = request_map[request_id]
        if principal not in principal_policies:
            raise ValueError('request has no matching principal policy: ' + request_id)
        if route not in route_policies:
            raise ValueError('request has no matching route policy: ' + request_id)
        records.append(RequestRecord(request_id, principal, route, at, priority))

    appeal_ids = []
    seen_appeals = set()
    for value in rows(data['appeals'], APPEAL_ROWS):
        appeal_id = ident(value)
        if appeal_id in seen_appeals:
            raise ValueError('duplicate appeal request id')
        if appeal_id not in request_map:
            raise ValueError('appeal references unknown request id: ' + appeal_id)
        seen_appeals.add(appeal_id)
        appeal_ids.append(appeal_id)
    appeal_ids.sort()
    return records, principal_policies, route_policies, appeal_ids, len(request_map)


def _window_key(kind, scope, at_us, window_seconds):
    span_us = window_seconds * 1_000_000
    # Floor division anchors half-open windows on the UTC epoch, including
    # negative instants where the floor bucket starts before the timestamp.
    start_second = at_us // span_us * window_seconds
    return (kind, scope, start_second)


def _add_edge(graph, source, target, capacity, cost):
    forward = [target, len(graph[target]), capacity, cost]
    backward = [source, len(graph[source]), 0, -cost]
    graph[source].append(forward)
    graph[target].append(backward)
    return forward


def _min_cost_max_flow(graph, source, sink):
    """Successive shortest augmenting paths with Johnson-style potentials."""
    node_count = len(graph)
    # The initial network is acyclic and nodes are indexed in topological
    # order (source, principal windows, request in/out, route windows, sink),
    # so one ordered pass gives the shortest-path potentials; unreachable
    # nodes safely start at zero.
    potentials = [0] * node_count
    reached = [False] * node_count
    reached[source] = True
    for node in range(node_count):
        if not reached[node]:
            continue
        base = potentials[node]
        for edge in graph[node]:
            target, _, capacity, cost = edge
            if capacity > 0 and (not reached[target] or base + cost < potentials[target]):
                potentials[target] = base + cost
                reached[target] = True

    inf = float('inf')
    distances = [inf] * node_count
    parent_edge = [0] * node_count
    parent_node = [0] * node_count
    finalized = [False] * node_count
    while True:
        for index in range(node_count):
            distances[index] = inf
            finalized[index] = False
        distances[source] = 0
        heap = [(0, source)]
        while heap:
            distance, node = heapq.heappop(heap)
            if finalized[node]:
                continue
            finalized[node] = True
            if node == sink:
                break
            potential_here = potentials[node]
            for edge_index, edge in enumerate(graph[node]):
                target = edge[0]
                if edge[2] <= 0 or finalized[target]:
                    continue
                reduced = distance + edge[3] + potential_here - potentials[target]
                if reduced < distances[target]:
                    distances[target] = reduced
                    parent_node[target] = node
                    parent_edge[target] = edge_index
                    heapq.heappush(heap, (reduced, target))
        if distances[sink] == inf:
            return
        for node in range(node_count):
            if distances[node] != inf:
                potentials[node] += distances[node]
        # Every edge incident to a request has unit capacity, so every
        # augmenting path carries exactly one unit of flow.
        node = sink
        while node != source:
            edge_index = parent_edge[node]
            edge = graph[parent_node[node]][edge_index]
            edge[2] -= 1
            graph[node][edge[1]][2] += 1
            node = parent_node[node]


def _build_and_solve(records, window_cap, principal_window, route_window, cost_of, forced_id):
    """Flow path per retained request:
    source -> principal window -> request in -> request out -> route window -> sink.

    One unit therefore consumes exactly one principal slot and one route
    slot; the in->out edge (capacity one, selection cost) represents the
    indivisible request and makes sure it is never counted twice.
    """
    p_keys = sorted(key for key in window_cap if key[0] == 'p')
    r_keys = sorted(key for key in window_cap if key[0] == 'r')
    p_node = {key: index + 1 for index, key in enumerate(p_keys)}
    active = [record for record in records if record.id != forced_id]
    in_start = len(p_keys) + 1
    in_node = {record.id: in_start + index for index, record in enumerate(active)}
    out_start = in_start + len(active)
    out_node = {record.id: out_start + index for index, record in enumerate(active)}
    r_start = out_start + len(active)
    r_node = {key: r_start + index for index, key in enumerate(r_keys)}
    source = 0
    sink = r_start + len(r_keys)
    graph = [[] for _ in range(sink + 1)]

    for key in p_keys:
        capacity = window_cap[key]
        if capacity > 0:
            _add_edge(graph, source, p_node[key], capacity, 0)
    for key in r_keys:
        capacity = window_cap[key]
        if capacity > 0:
            _add_edge(graph, r_node[key], sink, capacity, 0)

    selection_edges = {}
    for record in active:
        request_in = in_node[record.id]
        request_out = out_node[record.id]
        _add_edge(graph, p_node[principal_window[record.id]], request_in, 1, 0)
        _add_edge(graph, request_in, request_out, 1, cost_of[record.id])
        _add_edge(graph, request_out, r_node[route_window[record.id]], 1, 0)
        selection_edges[record.id] = (request_in, len(graph[request_in]) - 1)

    _min_cost_max_flow(graph, source, sink)

    kept = set()
    if forced_id is not None:
        # The forced request was reserved before the network was built; its
        # two window slots are already absent from window_cap, so it counts
        # exactly once here and never competes through an edge.
        kept.add(forced_id)
    for request_id, (node, edge_index) in selection_edges.items():
        if graph[node][edge_index][2] == 0:
            kept.add(request_id)
    return kept


def _describe(kept, records):
    all_ids = [record.id for record in records]
    kept_ids = sorted(kept)
    dropped_ids = sorted(set(all_ids) - kept)
    priority_sum = sum(record.priority for record in records if record.id in kept)
    return {
        'kept_ids': kept_ids,
        'dropped_ids': dropped_ids,
        'kept_count': len(kept_ids),
        'priority_sum': priority_sum,
    }


def retention_analyze(data):
    """Baseline retention plus one independent re-solve per appeal."""
    records, principal_policies, route_policies, appeal_ids, dedup_count = parse_retention_input(data)
    n = len(records)

    # Strictly separated weight bands make the three lexicographic objectives
    # comparable as one integer cost without any carry between bands:
    #   bit band  : prefer keeping the smallest id at the first membership
    #               difference (lexicographically minimal sorted kept list);
    #   W2 band   : maximize retained priority;
    #   W1 band   : maximize retained request count.
    bit_band = 1 << n
    priority_band = bit_band
    count_band = priority_band * (n * PRIORITY_MAX + 1)

    principal_window = {}
    route_window = {}
    cost_of = {}
    window_cap = {}
    for index, record in enumerate(records):
        principal_window_seconds, principal_limit = principal_policies[record.principal]
        route_window_seconds, route_limit = route_policies[record.route]
        p_key = _window_key('p', record.principal, record.at, principal_window_seconds)
        r_key = _window_key('r', record.route, record.at, route_window_seconds)
        principal_window[record.id] = p_key
        route_window[record.id] = r_key
        window_cap.setdefault(p_key, principal_limit)
        window_cap.setdefault(r_key, route_limit)
        lexicographic_bit = 1 << (n - 1 - index)
        cost_of[record.id] = -count_band - priority_band * record.priority - lexicographic_bit

    baseline_kept = _build_and_solve(records, dict(window_cap), principal_window,
                                     route_window, cost_of, None)
    baseline = _describe(baseline_kept, records)

    appeal_results = []
    for appeal_id in appeal_ids:
        forced_cap = dict(window_cap)
        forced_cap[principal_window[appeal_id]] -= 1
        forced_cap[route_window[appeal_id]] -= 1
        appeal_kept = _build_and_solve(records, forced_cap, principal_window,
                                       route_window, cost_of, appeal_id)
        appeal_view = _describe(appeal_kept, records)
        entered = sorted(appeal_kept - baseline_kept)
        removed = sorted(baseline_kept - appeal_kept)
        appeal_results.append({
            'request_id': appeal_id,
            'kept_ids': appeal_view['kept_ids'],
            'dropped_ids': appeal_view['dropped_ids'],
            'kept_count': appeal_view['kept_count'],
            'priority_sum': appeal_view['priority_sum'],
            'entered_ids': entered,
            'removed_ids': removed,
            'count_loss': baseline['kept_count'] - appeal_view['kept_count'],
            'priority_loss': baseline['priority_sum'] - appeal_view['priority_sum'],
        })

    return {
        'baseline': baseline,
        'appeals': appeal_results,
        'deduplicated_request_count': dedup_count,
    }
