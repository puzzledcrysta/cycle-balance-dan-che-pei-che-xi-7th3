import csv
from io import StringIO
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app import settings
from app.main import app
from app.modules.handover.service import produce

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / 'sample_data'


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'DB', tmp_path / 'handover.sqlite3')
    with TestClient(app) as client:
        yield client


def _read(name):
    return (SAMPLE / name).read_bytes()


@pytest.fixture
def payload():
    return {
        'opening': _read('opening.csv'),
        'departure': _read('departure.csv'),
        'event': _read('events.csv'),
    }


def report(payload, cutoff=''):
    return produce(payload['opening'], payload['departure'], payload['event'], cutoff)


def test_sample_conservation_closes(payload):
    result = report(payload)
    assert result['conservation']['balanced'] is True
    opening = result['conservation']['opening']
    assert opening == result['conservation']['confirmed'] + result['conservation']['in_transit']


def test_confirmed_vs_projected_kept_apart(payload):
    # D003(1辆)与 D102(2辆)的到达记录七点半后才回传，只能进预测口径，不进确认口径
    result = report(payload)
    in_transit = {trip['id']: trip for trip in result['trips']
                  if trip['state'] == '在途预计'}
    assert set(in_transit) == {'D003', 'D102'}
    west = next(row for row in result['stations']
                if row['zone'] == 'B区' and row['station'] == '西站')
    # 确认到站只有 D101=4；D102=2 仅在预测口径占位，确认余额不重复计入
    assert west['confirmed_balance'] == 5 and west['projected_balance'] == 7
    assert west['inflow'] == 4 and west['projected_inflow'] == 2
    bing = next(row for row in result['stations']
                if row['zone'] == 'A区' and row['station'] == '丙站')
    assert bing['confirmed_balance'] == 4 and bing['projected_balance'] == 5


def test_late_arrival_listed_as_unreceived_not_confirmed(payload):
    result = report(payload)
    assert [item['id'] for item in result['unreceived']] == ['D003', 'D102']
    assert all(item['submitted_at'] > result['cutoff'] for item in result['unreceived'])


def test_cancel_after_departure_returns_to_origin(payload):
    # D005、D006 发车后取消：冲销流出并退回起点，确认余额净变化为 0
    result = report(payload)
    returned = {trip['id']: trip for trip in result['trips'] if trip['return_to_origin']}
    assert set(returned) == {'D005', 'D006'}
    jia = next(row for row in result['stations']
               if row['zone'] == 'A区' and row['station'] == '甲站')
    yi = next(row for row in result['stations']
              if row['zone'] == 'A区' and row['station'] == '乙站')
    # D005 乙->甲(1)、D006 乙->甲(1) 取消：甲流出含在 7 内并退回 1，乙流出 3 含 D006 的 1 并退回 1
    assert jia['returns'] == 1 and yi['returns'] == 1
    assert jia['confirmed_balance'] == 4 and yi['confirmed_balance'] == 3


def test_cancel_before_departure_has_no_movement(payload):
    # D103 发车前取消：车未离站，不产生任何移动或退回
    result = report(payload)
    trip = next(trip for trip in result['trips'] if trip['id'] == 'D103')
    assert trip['state'] == '已取消' and trip['return_to_origin'] is False
    east = next(row for row in result['stations']
                if row['zone'] == 'B区' and row['station'] == '东站')
    assert east['returns'] == 0


def test_duplicate_arrival_suppressed_not_double_counted(payload):
    result = report(payload)
    suppressed = [row for row in result['suppressed'] if row['code'] == 'EXACT_DUP']
    assert [row['key'] for row in suppressed] == ['D101']
    west = next(row for row in result['stations']
                if row['zone'] == 'B区' and row['station'] == '西站')
    assert west['inflow'] == 4  # 重传不产生第二次到站


def test_conflicting_arrival_and_ghost_trip_quarantined(payload):
    result = report(payload)
    codes = {(row['key'], row['code']) for row in result['quarantine']}
    assert ('D001', 'HANDOVER_STATION') in codes    # 已到站又报不同到站
    assert ('D999', 'HANDOVER_TRIP') in codes        # 引用不存在的调车单


def test_handover_sheet_zone_surplus_shortage(payload):
    result = report(payload)
    zones = {row['zone']: row for row in result['zones']}
    # 确认口径余缺：A区 11 辆、B区 7 辆（在途车不在库，故不计确认）
    assert zones['A区']['confirmed_surplus_shortage'] == 11
    assert zones['B区']['confirmed_surplus_shortage'] == 7
    # 预测口径把在途预计到达加回
    assert zones['A区']['projected_surplus_shortage'] == 12
    assert zones['B区']['projected_surplus_shortage'] == 9


def test_explicit_cutoff_changes_classification(payload):
    # 把截止时刻推后到 08:00，七点半回传的 D003/D102 转为确认到站
    result = report(payload, '2026-11-20T08:00:00+08:00')
    states = {trip['id']: trip['state'] for trip in result['trips']}
    assert states['D003'] == '已确认到站' and states['D102'] == '已确认到站'
    assert result['unreceived'] == []
    assert result['conservation']['in_transit'] == 0


def _csv(header, rows):
    out = StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode()


def test_bad_rows_are_quarantined_not_fatal():
    opening = _csv(['调车区', '站点', '期初车辆'], [['Z区', '甲站', '5'], ['Z区', '甲站', '3']])
    departure = _csv(
        ['调车区', '调车单号', '起点', '目标站', '数量', '发车时刻', '预计到达'],
        [['Z区', 'T1', '甲站', '乙站', '2', '2026-11-20T06:00:00+08:00',
          '2026-11-20T06:30:00+08:00']])
    events = _csv(['调车区', '调车单号', '事件类型', '提交时间', '事件时刻', '到站'], [])
    result = produce(opening, departure, events, '2026-11-20T07:00:00+08:00')
    dup = [row for row in result['quarantine'] if row['code'] == 'HANDOVER_DUP']
    assert len(dup) == 1
    assert result['conservation']['balanced'] is True


def test_api_idempotent_same_snapshot(client, payload):
    body = {f'{name}': blob.decode() for name, blob in payload.items()}
    first = client.post('/api/handover', json=body).json()
    second = client.post('/api/handover', json=body).json()
    assert first['manifest']['handover_id'] == second['manifest']['handover_id']
    assert first == second
    # 同一班次重跑只保留一条交接快照，结转不重复
    assert len(client.get('/api/handover').json()) == 1


def test_api_replay_does_not_duplicate_carry_forward(client, payload):
    body = {f'{name}': blob.decode() for name, blob in payload.items()}
    created = client.post('/api/handover', json=body).json()
    for _ in range(3):
        again = client.post('/api/handover', json=body).json()
        assert again['manifest']['handover_id'] == created['manifest']['handover_id']
    assert len(client.get('/api/handover').json()) == 1


def test_concurrent_runs_one_snapshot(client, payload):
    from concurrent.futures import ThreadPoolExecutor
    from app.modules.handover.ledger import run
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda _: run(payload['opening'], payload['departure'], payload['event'], ''),
            range(4)))
    ids = {item['manifest']['handover_id'] for item in results}
    assert len(ids) == 1
    assert len(client.get('/api/handover').json()) == 1


def test_api_different_cutoff_makes_new_snapshot(client, payload):
    body = {f'{name}': blob.decode() for name, blob in payload.items()}
    first = client.post('/api/handover', json=body).json()
    body['cutoff'] = '2026-11-20T08:00:00+08:00'
    second = client.post('/api/handover', json=body).json()
    assert first['manifest']['handover_id'] != second['manifest']['handover_id']
    assert len(client.get('/api/handover').json()) == 2


def test_api_get_and_source_roundtrip(client, payload):
    body = {f'{name}': blob.decode() for name, blob in payload.items()}
    created = client.post('/api/handover', json=body).json()
    hid = created['manifest']['handover_id']
    fetched = client.get(f'/api/handover/{hid}').json()
    assert fetched['manifest']['handover_id'] == hid
    sources = client.get(f'/api/handover/{hid}/source').json()
    from base64 import b64decode
    assert b64decode(sources['departure_base64']) == payload['departure']
    assert client.get('/api/handover/nope').status_code == 404


def test_api_missing_header_rejected(client):
    bad = _csv(['调车区', '站点'], [['Z区', '甲站']])
    response = client.post('/api/handover', json={'opening': bad.decode()})
    assert response.status_code == 422 and response.json()['code'] == 'CONTRACT'


def test_web_wiring_and_samples(client):
    page = client.get('/')
    assert page.status_code == 200 and '班次交接' in page.text and 'handover-desk' in page.text
    assert client.get('/static/handover.js').status_code == 200
    for name in ('opening', 'departure', 'events'):
        assert client.get(f'/api/samples/{name}').status_code == 200


def test_web_end_to_end_handover(client):
    body = {name: client.get(f'/api/samples/{name}').content.decode()
            for name in ('opening', 'departure', 'events')}
    res = client.post('/api/handover', json=body).json()
    zones = {z['zone']: z for z in res['zones']}
    assert zones['A区']['confirmed_surplus_shortage'] == 11
    assert zones['B区']['projected_surplus_shortage'] == 9
    assert res['conservation']['balanced'] is True


def test_cross_zone_event_is_quarantined():
    opening = _csv(['调车区', '站点', '期初车辆'],
                   [['A区', '甲站', '5'], ['A区', '乙站', '0']])
    departure = _csv(
        ['调车区', '调车单号', '起点', '目标站', '数量', '发车时刻', '预计到达'],
        [['A区', 'T1', '甲站', '乙站', '2', '2026-11-20T06:00:00+08:00',
          '2026-11-20T06:30:00+08:00']])
    # 事件把调车区写成 B区，与发车单的 A区 不符
    events = _csv(
        ['调车区', '调车单号', '事件类型', '提交时间', '事件时刻', '到站'],
        [['B区', 'T1', '到达', '2026-11-20T06:35:00+08:00',
          '2026-11-20T06:32:00+08:00', '乙站']])
    result = produce(opening, departure, events, '2026-11-20T07:00:00+08:00')
    assert any(row['code'] == 'HANDOVER_ZONE' for row in result['quarantine'])
    # 跨区到达不采信，T1 仍按在途处理
    assert next(iter(result['trips']))['state'] == '在途预计'


def test_cancel_after_arrival_is_valid_transition():
    opening = _csv(['调车区', '站点', '期初车辆'],
                   [['A区', '甲站', '5'], ['A区', '乙站', '0']])
    departure = _csv(
        ['调车区', '调车单号', '起点', '目标站', '数量', '发车时刻', '预计到达'],
        [['A区', 'T1', '甲站', '乙站', '2', '2026-11-20T06:00:00+08:00',
          '2026-11-20T06:30:00+08:00']])
    events = _csv(
        ['调车区', '调车单号', '事件类型', '提交时间', '事件时刻', '到站'],
        [['A区', 'T1', '到达', '2026-11-20T06:35:00+08:00',
          '2026-11-20T06:32:00+08:00', '乙站'],
         ['A区', 'T1', '取消', '2026-11-20T06:40:00+08:00',
          '2026-11-20T06:39:00+08:00', '']])
    result = produce(opening, departure, events, '2026-11-20T07:00:00+08:00')
    trip = result['trips'][0]
    assert trip['state'] == '已取消' and trip['return_to_origin'] is True
    assert result['conservation']['balanced'] is True
