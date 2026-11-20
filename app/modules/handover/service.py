from collections import Counter
from datetime import datetime, time, timedelta, timezone
from app.errors import DataError
from app.evidence import finding
from app.modules.domain.values import whole, instant
from .reader import read_table, is_missing
from .rule import handover as build_handover, ARRIVAL, VALID_KINDS

OPEN_FIELDS = ('调车区', '站点', '期初车辆')
DEPART_FIELDS = ('调车区', '调车单号', '起点', '目标站', '数量', '发车时刻', '预计到达')
EVENT_FIELDS = ('调车区', '调车单号', '事件类型', '提交时间', '事件时刻', '到站')
CN_TZ = timezone(timedelta(hours=8))
DEFAULT_CUTOFF = time(7, 0)


def _bad(line, key, raw, code, detail):
    return finding({'line': line, 'key': key, 'raw': raw}, code, detail)


def parse_openings(payload):
    _, rows, bad, _ = read_table(payload, OPEN_FIELDS)
    openings, seen = {}, {}
    for row in rows:
        v = row['values']
        zone, station, key = v['调车区'], v['站点'], (v['调车区'], v['站点'])
        try:
            if is_missing(zone) or is_missing(station) or is_missing(v['期初车辆']):
                raise DataError('REQUIRED', '调车区、站点和期初车辆均须填写')
            count = whole(v['期初车辆'], 0, 100000)
            if key in seen:
                raise DataError('HANDOVER_DUP', f'站点 {station} 期初余额重复登记')
        except DataError as error:
            bad.append(_bad(row['line'], zone, row['raw'], error.code, str(error)))
            continue
        seen[key] = row['line']
        openings[key] = count
    return openings, bad


def parse_departures(payload):
    _, rows, bad, _ = read_table(payload, DEPART_FIELDS)
    departures, seen = {}, {}
    for row in rows:
        v = row['values']
        trip_id, zone = v['调车单号'], v['调车区']
        try:
            required = ('调车区', '调车单号', '起点', '目标站', '数量', '发车时刻', '预计到达')
            if any(is_missing(v[name]) for name in required):
                raise DataError('REQUIRED', '发车单字段均须填写')
            quantity = whole(v['数量'], 1, 10000)
            departed = instant(v['发车时刻'])
            eta = instant(v['预计到达'])
            if eta < departed:
                raise DataError('RANGE', '预计到达早于发车时刻')
            if v['起点'] == v['目标站']:
                raise DataError('HANDOVER_STATION', '起点与目标站不能相同')
            if trip_id in seen:
                raise DataError('HANDOVER_DUP', f'调车单号 {trip_id} 重复发车')
        except DataError as error:
            bad.append(_bad(row['line'], trip_id, row['raw'], error.code, str(error)))
            continue
        seen[trip_id] = row['line']
        departures[trip_id] = {'id': trip_id, 'zone': zone, 'origin': v['起点'],
                               'destination': v['目标站'], 'quantity': quantity,
                               'departed_at': departed, 'eta': eta,
                               'line': row['line'], 'raw': row['raw']}
    return departures, bad


def parse_events(payload):
    _, rows, bad, _ = read_table(payload, EVENT_FIELDS, allow_empty=True)
    arrivals, cancelled = [], []
    for row in rows:
        v = row['values']
        trip_id, zone = v['调车单号'], v['调车区']
        try:
            kind = v['事件类型']
            if kind not in VALID_KINDS:
                raise DataError('HANDOVER_TRIP', f'事件类型须为 {sorted(VALID_KINDS)}')
            if any(is_missing(v[name]) for name in ('调车区', '调车单号', '提交时间', '事件时刻')):
                raise DataError('REQUIRED', '事件主键与时间均须填写')
            submitted = instant(v['提交时间'])
            event_time = instant(v['事件时刻'])
            item = {'id': trip_id, 'zone': zone, 'kind': kind,
                    'submitted_at': submitted, 'event_time': event_time,
                    'station': v['到站'], 'line': row['line'], 'raw': row['raw']}
            if kind == ARRIVAL:
                if is_missing(v['到站']):
                    raise DataError('REQUIRED', '到达事件须填写到站')
                arrivals.append(item)
            else:
                cancelled.append(item)
        except DataError as error:
            bad.append(_bad(row['line'], trip_id, row['raw'], error.code, str(error)))
            continue
    return arrivals, cancelled, bad


def resolve_cutoff(cutoff_text, departures):
    # instant() 已把所有事件时刻归一到 UTC，截止时刻同样归一后再比较
    if cutoff_text:
        return instant(cutoff_text)
    if not departures:
        raise DataError('HANDOVER_CUTOFF', '无发车单时必须显式给出交接截止时刻')
    earliest = min(dep['departed_at'] for dep in departures.values())
    local_date = earliest.astimezone(CN_TZ).date()
    local_cutoff = datetime.combine(local_date, DEFAULT_CUTOFF, CN_TZ)
    return local_cutoff.astimezone(timezone.utc)


def produce(opening_bytes, departure_bytes, event_bytes, cutoff_text):
    openings, open_bad = parse_openings(opening_bytes)
    departures, depart_bad = parse_departures(departure_bytes)
    arrivals, cancelled, event_bad = parse_events(event_bytes)
    cutoff = resolve_cutoff(cutoff_text, departures)

    # 到达/取消引用的调车区与发车单不一致，隔离之且不进入状态机
    bad = [*open_bad, *depart_bad, *event_bad]
    valid_arrivals, valid_cancelled = [], []
    for item in (*arrivals, *cancelled):
        dep = departures.get(item['id'])
        if dep is not None and dep['zone'] != item['zone']:
            bad.append(_bad(item['line'], item['id'], item['raw'], 'HANDOVER_ZONE',
                            f"事件调车区 {item['zone']} 与发车单 {dep['zone']} 不一致"))
            continue
        (valid_arrivals if item['kind'] == ARRIVAL else valid_cancelled).append(item)

    result = build_handover(openings, departures, valid_arrivals,
                            valid_cancelled, cutoff)
    result['quarantine'] = sorted([*bad, *result['quarantine']],
                                  key=lambda row: row['line'])
    result['reasons'] = dict(sorted(Counter(
        row['code'] for row in result['quarantine']).items()))
    result['counts']['input_rows'] = (len(openings) + len(departures)
                                      + len(valid_arrivals) + len(valid_cancelled)
                                      + len(bad))
    result['cutoff'] = cutoff.isoformat()
    _serialize(result)
    return result


def _serialize(result):
    for trip in result['trips']:
        trip['departed_at'] = trip['departed_at'].isoformat()
        trip['eta'] = trip['eta'].isoformat()
        if trip['arrived_at'] is not None:
            trip['arrived_at'] = trip['arrived_at'].isoformat()
    for item in result['unreceived']:
        item['submitted_at'] = item['submitted_at'].isoformat()
        item['arrival_time'] = item['arrival_time'].isoformat()
    for movement in result['movements']:
        if 'eta' in movement:
            movement['eta'] = movement['eta'].isoformat()
