"""班次交接状态机。

以交接截止时刻为界，把调车单归并为每辆车（调车单号）的最终状态：
- 已确认到站：截止前回传的到达记录，进入确认余额；
- 在途预计：截止前已发车、无有效到达记录，按发车单 ETA 仅进入预测口径；
- 已取消：发车前取消不产生移动，发车后取消冲销原移动并退回起点。

确认口径与预测口径分开累计，避免目标站对同一批车重复占位。
"""
from collections import defaultdict
from app.evidence import finding

DEPARTED = '已发车'
ARRIVED = '已确认到站'
IN_TRANSIT = '在途预计'
CANCELLED = '已取消'

ARRIVAL = '到达'
CANCEL = '取消'
DEPARTURE_KIND = '发车'
VALID_KINDS = {DEPARTURE_KIND, ARRIVAL, CANCEL}

# 状态机允许的事件转移：当前状态 -> 可接受事件
TRANSITIONS = {
    DEPARTED: {ARRIVAL, CANCEL},
    ARRIVED: {CANCEL},
    CANCELLED: set(),
}


def _quarantine(line, key, raw, code, detail):
    return finding({'line': line, 'key': key, 'raw': raw}, code, detail)


def build_trips(openings, departures, arrivals, cancelled, cutoff):
    """归并逐车事件，产出车辆明细、隔离记录、重传抑制与截止后才回传的到达。"""
    trips = {}
    quarantine, suppressed, unreceived = [], [], []

    def invalid(line, key, raw, code, detail):
        quarantine.append(_quarantine(line, key, raw, code, detail))

    for trip_id, dep in sorted(departures.items()):
        trips[dep['id']] = {
            'id': dep['id'], 'zone': dep['zone'], 'origin': dep['origin'],
            'destination': dep['destination'], 'quantity': dep['quantity'],
            'eta': dep['eta'], 'departed_at': dep['departed_at'],
            'state': DEPARTED, 'arrived_at': None, 'return_to_origin': False,
            'line': dep['line'],
        }

    events = []
    for row in arrivals:
        events.append((row['event_time'], row['line'], ARRIVAL, row))
    for row in cancelled:
        events.append((row['event_time'], row['line'], CANCEL, row))
    # 同一时刻先到后取消，保证事件顺序确定
    events.sort(key=lambda item: (item[0], item[1], item[2]))

    for _, line, kind, row in events:
        trip_id, raw = row['id'], row['raw']
        trip = trips.get(trip_id)
        if trip is None:
            invalid(line, trip_id, raw, 'HANDOVER_TRIP', '到达/取消引用了不存在的调车单')
            continue
        state = trip['state']
        # 同事件重传：已在该状态且内容一致，记抑制而非隔离，重跑不产生新影响
        duplicate = (kind == ARRIVAL and state == ARRIVED
                     and trip['arrived_at'] == row['event_time']
                     and row['station'] == trip['destination']) or \
                    (kind == CANCEL and state == CANCELLED)
        if duplicate:
            suppressed.append(_quarantine(line, trip_id, raw, 'EXACT_DUP',
                                          f'{kind}重传，沿用首次登记'))
            continue
        if kind not in TRANSITIONS[state]:
            if kind == ARRIVAL and state == ARRIVED:
                invalid(line, trip_id, raw, 'HANDOVER_STATION',
                        f"调车单已确认到站 {trip['destination']}，不能再次登记到达")
            else:
                invalid(line, trip_id, raw, 'HANDOVER_TRANSITION',
                        f'{state} 的调车单不能再登记{kind}')
            continue
        if kind == ARRIVAL:
            if row['station'] != trip['destination']:
                invalid(line, trip_id, raw, 'HANDOVER_STATION',
                        f"实际到站 {row['station']} 与目标站 {trip['destination']} 不一致")
                continue
            if row['submitted_at'] > cutoff:
                # 到达记录在截止后才回传：车仍按在途处理，到达不进入确认口径
                unreceived.append({'id': trip_id, 'zone': trip['zone'], 'line': line,
                                   'submitted_at': row['submitted_at'],
                                   'arrival_time': row['event_time'], 'raw': raw})
                continue
            trip['state'] = ARRIVED
            trip['arrived_at'] = row['event_time']
        else:  # 取消
            trip['state'] = CANCELLED
            # 取消事件早于发车时刻 = 发车前取消，车未离站，不产生任何移动；
            # 否则为发车后取消，需冲销发车移动并把车退回起点
            trip['return_to_origin'] = row['event_time'] >= trip['departed_at']

    # 截止时刻仍停留在「已发车」、且无有效到达记录的车，交接口径记为在途预计
    for trip in trips.values():
        if trip['state'] == DEPARTED:
            trip['state'] = IN_TRANSIT

    return trips, quarantine, suppressed, unreceived


def _station_block(openings, zone, station):
    return {'zone': zone, 'station': station, 'opening': openings.get((zone, station), 0),
            'inflow': 0, 'outflow': 0, 'projected_inflow': 0, 'returns': 0}


def summarize(trips, openings):
    """按调车区、站点计算确认余额与预测余额，并核对车辆守恒。"""
    stations = {}
    movements = []
    zone_order = sorted({trip['zone'] for trip in trips.values()} |
                        {zone for zone, _ in openings})
    for zone, station in openings:
        stations[(zone, station)] = _station_block(openings, zone, station)

    for trip in sorted(trips.values(), key=lambda item: (item['zone'], item['id'])):
        zone = trip['zone']
        origin_key = (zone, trip['origin'])
        dest_key = (zone, trip['destination'])
        stations.setdefault(origin_key, _station_block(openings, zone, trip['origin']))
        stations.setdefault(dest_key, _station_block(openings, zone, trip['destination']))
        quantity = trip['quantity']

        if trip['state'] == CANCELLED:
            if trip['return_to_origin']:
                # 发车后取消：冲销发车移动，退回起点，确认余额净变化为 0
                stations[origin_key]['outflow'] += quantity
                stations[origin_key]['returns'] += quantity
                movements.append({'zone': zone, 'id': trip['id'], 'station': trip['origin'],
                                  'direction': '取消退回', 'quantity': quantity,
                                  'confirmed': True, 'line': trip['line']})
            continue

        # 有效发车（在途或已到站）：起点流出
        stations[origin_key]['outflow'] += quantity
        movements.append({'zone': zone, 'id': trip['id'], 'station': trip['origin'],
                          'direction': '发车流出', 'quantity': quantity,
                          'confirmed': True, 'line': trip['line']})

        if trip['state'] == ARRIVED:
            stations[dest_key]['inflow'] += quantity
            movements.append({'zone': zone, 'id': trip['id'], 'station': trip['destination'],
                              'direction': '确认到站', 'quantity': quantity,
                              'confirmed': True, 'line': trip['line']})
        else:
            # 在途：仅预测口径占位，确认口径不计入，杜绝目标站重复占位
            stations[dest_key]['projected_inflow'] += quantity
            movements.append({'zone': zone, 'id': trip['id'], 'station': trip['destination'],
                              'direction': '预计到达', 'quantity': quantity,
                              'confirmed': False, 'line': trip['line'], 'eta': trip['eta']})

    station_rows = []
    for key in sorted(stations):
        block = stations[key]
        confirmed = block['opening'] + block['inflow'] - block['outflow'] + block['returns']
        projected = confirmed + block['projected_inflow']
        # 确认余额为正即余、为负即缺；预测口径额外计入在途预计到达
        station_rows.append({**block, 'confirmed_balance': confirmed,
                             'projected_balance': projected,
                             'confirmed_surplus_shortage': confirmed,
                             'projected_surplus_shortage': projected})

    zones = []
    for zone in zone_order:
        rows = [row for row in station_rows if row['zone'] == zone]
        zone_trips = [trip for trip in trips.values() if trip['zone'] == zone]
        count = defaultdict(int)
        for trip in zone_trips:
            count[trip['state']] += 1
        opening_total = sum(row['opening'] for row in rows)
        confirmed_total = sum(row['confirmed_balance'] for row in rows)
        projected_total = sum(row['projected_balance'] for row in rows)
        zones.append({
            'zone': zone,
            'states': {ARRIVED: count[ARRIVED], IN_TRANSIT: count[IN_TRANSIT],
                       CANCELLED: count[CANCELLED]},
            'opening_total': opening_total,
            'confirmed_total': confirmed_total,
            'projected_total': projected_total,
            # 下一班可照此派车：正数为余、负数为缺
            'confirmed_surplus_shortage': confirmed_total,
            'projected_surplus_shortage': projected_total,
            'in_transit_quantity': sum(trip['quantity'] for trip in zone_trips
                                       if trip['state'] == IN_TRANSIT),
        })

    # 守恒：期初车 = 确认在库车（含取消退回起点的车）+ 仍在途的车
    opening_sum = sum(row['opening'] for row in station_rows)
    confirmed_sum = sum(row['confirmed_balance'] for row in station_rows)
    in_transit_qty = sum(trip['quantity'] for trip in trips.values()
                         if trip['state'] == IN_TRANSIT)
    cancelled_qty = sum(trip['quantity'] for trip in trips.values()
                        if trip['state'] == CANCELLED)
    balanced = opening_sum == confirmed_sum + in_transit_qty
    return {'zones': zones, 'stations': station_rows, 'movements': movements,
            'conservation': {'opening': opening_sum, 'confirmed': confirmed_sum,
                             'in_transit': in_transit_qty, 'cancelled': cancelled_qty,
                             'balanced': balanced}}


def handover(openings, departures, arrivals, cancelled, cutoff):
    trips, quarantine, suppressed, unreceived = build_trips(
        openings, departures, arrivals, cancelled, cutoff)
    summary = summarize(trips, openings)
    counts = {'trips': len(trips),
              'quarantine': len(quarantine),
              'suppressed': len(suppressed),
              'unreceived_arrivals': len(unreceived)}
    return {'cutoff': cutoff, 'trips': sorted(trips.values(),
                                              key=lambda item: (item['zone'], item['id'])),
            'unreceived': unreceived, 'quarantine': quarantine,
            'suppressed': suppressed, 'counts': counts, **summary}
