"""Shared tiered pricing and cost-centre chargeback (``--mode chargeback``).

A supplier prices one API scope per tariff.  All usage known for the same
``(tariff_id, UTC-epoch-anchored window)`` pool is summed **before** the
marginal tier schedule is applied, so the discount is shared by every cost
centre in that pool rather than recomputed per centre.  The exact pool cost
``C`` is a non-negative rational; each non-empty cell ``(tariff, window,
centre)`` has exact cost ``a = C * u / U``.

Rounding to whole cents is a single global problem.  Every pool must pay
``R = floor(C + 1/2)`` (half up) and every centre must receive exactly its
``required_cents``.  A cell may only take ``floor(a)`` or ``ceil(a)`` and a
manual lock pins one cell.  Writing ``z = floor(a) + x`` with ``x`` in
``{0, 1}`` turns the row/column constraints into a bipartite 0-1 feasibility
problem, while

    E = sum |z - a| = constant + sum x * (1 - 2*frac(a))

so minimising the absolute rounding error is a minimum-cost flow with exact
rational costs.  The lexicographic tie-break on the complete cents vector is
a second cost component (one bit per free cell) compared lexicographically
after the primary rational error, which keeps the production path polynomial:
no enumeration of floor/ceil combinations and no per-cent expansion.

Locks and row/column sums are decided jointly.  If any exact-arithmetic
feasibility test fails the whole matrix is ``unallocatable``; no pool is
rounded independently and no column total is silently adjusted.
"""
from fractions import Fraction

from .common import require, ident, number, rows
from .model import instant, window_start

MAX_TARIFFS = 40
MAX_CENTERS = 50
MAX_EVENTS = 10_000
MAX_LOCKS = 2_000
MAX_TIERS = 8
MAX_POOLS = 80
MAX_CELLS = 2_000
WINDOW_MAX = 31_536_000
UPTO_MAX = 10 ** 15
RATE_NUM_MAX = 10 ** 9
RATE_DEN_MAX = 10 ** 6
UNITS_MAX = 10 ** 9
CENTS_MAX = 10 ** 18

class _Cell:
    __slots__ = ('tariff_id', 'window_start_us', 'center', 'units',
                 'exact', 'floor', 'remainder', 'denominator',
                 'locked_cents', 'fixed')

    def __init__(self, tariff_id, window_start_us, center, units, exact,
                 floor, remainder, denominator):
        self.tariff_id = tariff_id
        self.window_start_us = window_start_us
        self.center = center
        self.units = units
        self.exact = exact
        self.floor = floor
        self.remainder = remainder
        self.denominator = denominator
        self.locked_cents = None
        self.fixed = None  # None: free; 0/1: forced ceil indicator


def _parse_tiers(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TIERS:
        raise ValueError('tiers must be an array of 1..8 entries')
    tiers = []
    previous = None
    for index, item in enumerate(raw):
        require(item, 'upto rate_num rate_den')
        is_last = index == len(raw) - 1
        upto = item['upto']
        if upto is None:
            if not is_last:
                raise ValueError('only the last tier may be unbounded')
            tiers.append((None, number(item['rate_num'], 0, RATE_NUM_MAX),
                          number(item['rate_den'], 1, RATE_DEN_MAX)))
            continue
        if type(upto) is not int or not 1 <= upto <= UPTO_MAX:
            raise ValueError('tier upto must be a positive bounded integer')
        if previous is not None and upto <= previous:
            raise ValueError('tier upto values must strictly increase')
        previous = upto
        tiers.append((upto, number(item['rate_num'], 0, RATE_NUM_MAX),
                      number(item['rate_den'], 1, RATE_DEN_MAX)))
    if tiers[-1][0] is not None:
        # The contract denotes the infinite final band with null; without it
        # usage beyond the last finite bound would be silently unpriced.
        raise ValueError('the final tier must be unbounded (null)')
    return tiers


def _parse_tariffs(raw):
    tariffs = {}
    ids = set()
    scopes = set()
    for row in rows(raw, MAX_TARIFFS):
        require(row, 'id principal route window_seconds tiers')
        tariff_id = ident(row['id'])
        principal = ident(row['principal'])
        route = ident(row['route'])
        window_seconds = number(row['window_seconds'], 1, WINDOW_MAX)
        if tariff_id in ids:
            raise ValueError('duplicate tariff id: ' + tariff_id)
        scope = (principal, route)
        if scope in scopes:
            raise ValueError('duplicate tariff scope')
        ids.add(tariff_id)
        scopes.add(scope)
        tariffs[tariff_id] = {
            'id': tariff_id,
            'principal': principal,
            'route': route,
            'window_seconds': window_seconds,
            'tiers': _parse_tiers(row['tiers']),
        }
    return tariffs


def _parse_centers(raw):
    centers = {}
    for row in rows(raw, MAX_CENTERS):
        require(row, 'id required_cents')
        center_id = ident(row['id'])
        if center_id in centers:
            raise ValueError('duplicate center id: ' + center_id)
        centers[center_id] = number(row['required_cents'], 0, CENTS_MAX)
    return centers


def _parse_events(raw):
    event_map = {}
    for row in rows(raw, MAX_EVENTS):
        require(row, 'id principal route at units center')
        event_id = ident(row['id'])
        normalized = (
            ident(row['principal']),
            ident(row['route']),
            instant(row['at']),
            number(row['units'], 1, UNITS_MAX),
            ident(row['center']),
        )
        if event_id in event_map and event_map[event_id] != normalized:
            raise ValueError('conflicting duplicate event id: ' + event_id)
        event_map[event_id] = normalized
    return event_map


def _window_start_us(at_us, window_seconds):
    # model.window_start already floors negative epochs towards the previous
    # UTC-epoch anchored window; convert its second value back to microseconds.
    return window_start(at_us, window_seconds) * 1_000_000


def _aggregate(event_map, tariffs, centers):
    scope = {(tariff['principal'], tariff['route']): tariff
             for tariff in tariffs.values()}
    pools = {}
    cells = {}
    for event_id in sorted(event_map):
        principal, route, at_us, units, center = event_map[event_id]
        if center not in centers:
            raise ValueError('event references unknown center: ' + event_id)
        tariff = scope.get((principal, route))
        if tariff is None:
            raise ValueError('event has no exact scope tariff: ' + event_id)
        start_us = _window_start_us(at_us, tariff['window_seconds'])
        pool_key = (tariff['id'], start_us)
        cell_key = (tariff['id'], start_us, center)
        pool = pools.setdefault(pool_key, {'units': 0, 'centers': {}})
        pool['units'] += units
        pool['centers'][center] = pool['centers'].get(center, 0) + units
        cells[cell_key] = cells.get(cell_key, 0) + units
    if len(pools) > MAX_POOLS:
        raise ValueError('too many pricing pools')
    if len(cells) > MAX_CELLS:
        raise ValueError('too many non-empty cost cells')
    return pools, cells


def _parse_locks(raw, tariffs, centers, pools, cells):
    locks = {}
    for row in rows(raw, MAX_LOCKS):
        require(row, 'tariff_id window_start center cents')
        tariff_id = ident(row['tariff_id'])
        center = ident(row['center'])
        cents = number(row['cents'], 0, CENTS_MAX)
        start_us = instant(row['window_start'])
        if tariff_id not in tariffs:
            raise ValueError('lock references unknown tariff')
        if center not in centers:
            raise ValueError('lock references unknown center')
        span_us = tariffs[tariff_id]['window_seconds'] * 1_000_000
        if start_us % span_us != 0:
            raise ValueError('lock window_start is not a window boundary')
        pool_key = (tariff_id, start_us)
        if pool_key not in pools:
            raise ValueError('lock references unknown pool')
        cell_key = (tariff_id, start_us, center)
        if cell_key not in cells:
            raise ValueError('lock references unknown non-empty cell')
        if cell_key in locks:
            raise ValueError('duplicate lock for cost cell')
        locks[cell_key] = cents
    return locks


def parse_chargeback_input(data):
    """Validate the complete document before any business judgement."""
    require(data, 'tariffs centers events locks')
    raw_centers = rows(data['centers'], MAX_CENTERS)
    raw_events = rows(data['events'], MAX_EVENTS)
    raw_locks = rows(data['locks'], MAX_LOCKS)
    tariffs = _parse_tariffs(data['tariffs'])
    centers = _parse_centers(raw_centers)
    event_map = _parse_events(raw_events)
    if not centers and (event_map or raw_locks):
        raise ValueError('empty centers are only allowed without events or locks')
    pools, cells = _aggregate(event_map, tariffs, centers)
    locks = _parse_locks(raw_locks, tariffs, centers, pools, cells)
    return tariffs, centers, event_map, pools, cells, locks


def _pool_cost(tiers, units):
    """Exact marginal-tier cost of the whole pool ``[0, units)``."""
    total = Fraction(0)
    lower = 0
    for upto, rate_num, rate_den in tiers:
        # The tier covers [lower, upto); only its overlap with [0, units)
        # is charged.  The final null tier reaches the full pool.
        overlap = units - lower if upto is None else min(units, upto) - lower
        if overlap > 0:
            total += Fraction(overlap * rate_num, rate_den)
        if upto is None or upto >= units:
            break
        lower = upto
    return total


def _invoice(cost):
    """floor(cost + 1/2), i.e. exact half rounds up (not banker's rounding)."""
    return (2 * cost.numerator + cost.denominator) // (2 * cost.denominator)


def _fraction(value):
    return {'numerator': value.numerator, 'denominator': value.denominator}


def _solve(cells, pools, centers):
    """Return (status, cents_by_cell, rounding_error) for the global matrix.

    ``cells`` is the ordered list of cells; every cell already carries its
    exact value, floor/ceil pair and optional lock.  The row deficit totals
    ``sum(R)`` minus the shared floor/fixed base, and the column deficit
    totals ``sum(required_cents)`` minus the same base, so comparing them
    checks the total-invoice equality exactly.
    """
    row_keys = sorted(pools)  # (tariff_id, window_start_us)
    row_index = {key: index for index, key in enumerate(row_keys)}
    center_ids = sorted(centers)
    col_index = {center: index for index, center in enumerate(center_ids)}

    row_floor = [0] * len(row_keys)
    col_floor = [0] * len(center_ids)
    row_fixed = [0] * len(row_keys)
    col_fixed = [0] * len(center_ids)
    ok = True
    for cell in cells:
        row = row_index[(cell.tariff_id, cell.window_start_us)]
        col = col_index[cell.center]
        row_floor[row] += cell.floor
        col_floor[col] += cell.floor
        if cell.locked_cents is None:
            if cell.remainder == 0:
                cell.fixed = 0
            else:
                cell.fixed = None
        else:
            if cell.remainder == 0:
                if cell.locked_cents != cell.floor:
                    ok = False
                cell.fixed = 0
            elif cell.locked_cents == cell.floor:
                cell.fixed = 0
            elif cell.locked_cents == cell.floor + 1:
                cell.fixed = 1
            else:
                ok = False
                cell.fixed = None
        if cell.fixed == 1:
            row_fixed[row] += 1
            col_fixed[col] += 1
    if not ok:
        return 'unallocatable', None, None

    row_need = []
    col_need = [0] * len(center_ids)
    for index, key in enumerate(row_keys):
        need = pools[key]['invoice_cents'] - row_floor[index] - row_fixed[index]
        if need < 0:
            return 'unallocatable', None, None
        row_need.append(need)
    for index, center_id in enumerate(center_ids):
        need = centers[center_id] - col_floor[index] - col_fixed[index]
        if need < 0:
            return 'unallocatable', None, None
        col_need[index] = need

    free = [cell for cell in cells if cell.fixed is None]
    total = sum(row_need)
    if total != sum(col_need):
        return 'unallocatable', None, None

    chosen = {id(cell) for cell in cells if cell.fixed == 1}
    if total > 0:
        assignment = _min_cost_assignment(free, row_index, col_index,
                                          row_need, col_need, len(row_keys),
                                          len(center_ids))
        if assignment is None:
            return 'unallocatable', None, None
        chosen |= assignment
    elif any(row_need) or any(col_need):
        return 'unallocatable', None, None

    cents = []
    error = Fraction(0)
    for cell in cells:
        extra = 1 if id(cell) in chosen else 0
        value = cell.floor + extra
        cents.append(value)
        if cell.remainder:
            if extra:
                error += Fraction(cell.denominator - cell.remainder,
                                  cell.denominator)
            else:
                error += Fraction(cell.remainder, cell.denominator)
    return 'balanced', cents, error


def _min_cost_assignment(free, row_index, col_index, row_need, col_need,
                         row_count, col_count):
    """Global minimum-error assignment with a lexicographic tie-break.

    Each free cell is a capacity-one bipartite edge; choosing it means
    ``x = 1`` (take ``ceil``).  The exact primary weight of that choice is
    the rational ``1 - 2*frac(a)``.  A second cost component carries one bit
    per free cell.  Costs are compared as ``(primary, bit_tiebreak)``
    lexicographically, so the primary error is always optimised first and the
    bit component only decides among equal-error matrices -- no common
    denominator has to be materialised and no floor/ceil combination is
    enumerated.
    """
    node_count = row_count + col_count + 2
    source = 0
    sink = node_count - 1
    row_base = 1
    col_base = 1 + row_count
    graph = [[] for _ in range(node_count)]
    zero = (Fraction(0), 0)

    def add_edge(start, finish, capacity, cost):
        graph[start].append([finish, len(graph[finish]), capacity, cost])
        graph[finish].append([start, len(graph[start]) - 1, 0,
                              (-cost[0], -cost[1])])

    for row in range(row_count):
        if row_need[row] > 0:
            add_edge(source, row_base + row, row_need[row], zero)
    for col in range(col_count):
        if col_need[col] > 0:
            add_edge(col_base + col, sink, col_need[col], zero)

    cell_edges = []
    bit = 1 << (len(free) - 1) if free else 0
    for cell in free:
        row = row_index[(cell.tariff_id, cell.window_start_us)]
        col = col_index[cell.center]
        primary = Fraction(cell.denominator - 2 * cell.remainder,
                           cell.denominator)
        add_edge(row_base + row, col_base + col, 1, (primary, bit))
        bit >>= 1
        cell_edges.append((row_base + row, len(graph[row_base + row]) - 1))

    flow = _successive_shortest_paths(graph, source, sink, node_count)
    if flow != sum(row_need):
        return None
    chosen = set()
    for cell, (node, edge_index) in zip(free, cell_edges):
        if graph[node][edge_index][2] == 0:
            chosen.add(id(cell))
    return chosen


def _initial_potentials(graph, node_count):
    """Johnson potentials via Bellman-Ford from a virtual zero-cost source.

    Cell primary costs ``1 - 2*frac(a)`` may be negative, and rows whose
    deficit is zero are unreachable from the real source but still carry
    outgoing cell edges.  A one-pass topological scan would therefore leave
    some reduced costs negative and break Dijkstra.  Relaxing every node from
    an initial distance of zero (the virtual super source, never
    materialised) yields potentials valid for *all* residual edges.
    """
    potential = [(Fraction(0), 0)] * node_count
    for _ in range(node_count - 1):
        changed = False
        for node in range(node_count):
            base = potential[node]
            for edge in graph[node]:
                if edge[2] <= 0:
                    continue
                candidate = (base[0] + edge[3][0], base[1] + edge[3][1])
                if candidate < potential[edge[0]]:
                    potential[edge[0]] = candidate
                    changed = True
        if not changed:
            break
    return potential


def _successive_shortest_paths(graph, source, sink, node_count):
    """Polynomial min-cost max-flow with lexicographically ordered costs."""
    import heapq

    potential = _initial_potentials(graph, node_count)
    flow = 0
    while True:
        distance = [None] * node_count
        parent_node = [-1] * node_count
        parent_edge = [-1] * node_count
        distance[source] = (Fraction(0), 0)
        heap = [((Fraction(0), 0), source)]
        while heap:
            current, node = heapq.heappop(heap)
            if distance[node] != current:
                continue
            base = potential[node]
            for edge_index, edge in enumerate(graph[node]):
                target, _, capacity, cost = edge
                if capacity <= 0:
                    continue
                reduced = (current[0] + cost[0] + base[0] - potential[target][0],
                           current[1] + cost[1] + base[1] - potential[target][1])
                if distance[target] is None or reduced < distance[target]:
                    distance[target] = reduced
                    parent_node[target] = node
                    parent_edge[target] = edge_index
                    heapq.heappush(heap, (reduced, target))
        if distance[sink] is None:
            return flow
        for node in range(node_count):
            if distance[node] is not None:
                potential[node] = (potential[node][0] + distance[node][0],
                                   potential[node][1] + distance[node][1])
        push = None
        node = sink
        while node != source:
            edge = graph[parent_node[node]][parent_edge[node]]
            push = edge[2] if push is None else min(push, edge[2])
            node = parent_node[node]
        node = sink
        while node != source:
            edge = graph[parent_node[node]][parent_edge[node]]
            edge[2] -= push
            graph[node][edge[1]][2] += push
            node = parent_node[node]
        flow += push


def chargeback_analyze(data):
    """Validate, price every pool and solve the whole cents matrix globally."""
    tariffs, centers, event_map, pools, cells, locks = parse_chargeback_input(data)

    priced_pools = {}
    for key, pool in pools.items():
        cost = _pool_cost(tariffs[key[0]]['tiers'], pool['units'])
        priced_pools[key] = {
            'tariff': tariffs[key[0]],
            'window_start_us': key[1],
            'units': pool['units'],
            'exact_cost': cost,
            'invoice_cents': _invoice(cost),
        }
    cell_records = []
    for key in sorted(cells):
        tariff_id, start_us, center = key
        pool = priced_pools[(tariff_id, start_us)]
        units = cells[key]
        cost = pool['exact_cost']
        denominator = cost.denominator * pool['units']
        numerator = cost.numerator * units
        floor = numerator // denominator
        remainder = numerator % denominator
        cell = _Cell(tariff_id, start_us, center, units,
                     Fraction(numerator, denominator), floor, remainder,
                     denominator)
        cell.locked_cents = locks.get(key)
        cell_records.append(cell)

    status, cents, error = _solve(cell_records, priced_pools, centers)

    actual = {center_id: 0 for center_id in centers}
    if status == 'balanced':
        for cell, value in zip(cell_records, cents):
            actual[cell.center] += value

    output_pools = []
    for key in sorted(priced_pools):
        pool = priced_pools[key]
        output_pools.append({
            'tariff_id': key[0],
            'principal': pool['tariff']['principal'],
            'route': pool['tariff']['route'],
            'window_start_us': key[1],
            'window_end_us': key[1] + pool['tariff']['window_seconds'] * 1_000_000,
            'units': pool['units'],
            'exact_cost': _fraction(pool['exact_cost']),
            'invoice_cents': pool['invoice_cents'],
        })

    output_centers = []
    for center_id in sorted(centers):
        output_centers.append({
            'id': center_id,
            'required_cents': centers[center_id],
            'actual_cents': actual[center_id] if status == 'balanced' else None,
        })

    output_cells = []
    for index, cell in enumerate(cell_records):
        output_cells.append({
            'tariff_id': cell.tariff_id,
            'window_start_us': cell.window_start_us,
            'center': cell.center,
            'units': cell.units,
            'exact_cost': _fraction(cell.exact),
            'cents': cents[index] if status == 'balanced' else None,
            'locked': cell.locked_cents is not None,
        })

    return {
        'status': status,
        'pools': output_pools,
        'centers': output_centers,
        'cells': output_cells,
        'total_invoice_cents': sum(pool['invoice_cents']
                                   for pool in priced_pools.values()),
        'rounding_error': _fraction(error) if status == 'balanced' else None,
        'deduplicated_event_count': len(event_map),
    }
