import {query, readInput, encodeInput} from './api.js';
import {el, table} from './table.js';

const byId = id => document.getElementById(id);
const grid = byId('ho-grid');
const summary = byId('ho-summary');
const notice = byId('ho-notice');
let report = null;
let view = 'zones';
const files = new Map();

function setNotice(message, error = false) {
  notice.value = message;
  notice.classList.toggle('problem', error);
}

async function collect() {
  const body = {};
  for (const [name, control] of [['opening', 'ho-opening'], ['departure', 'ho-departure'], ['event', 'ho-event']]) {
    const textarea = byId(control);
    const uploaded = files.get(name);
    if (uploaded && textarea.value === uploaded.text) body[`${name}_base64`] = encodeInput(uploaded.bytes);
    else if (textarea.value.length || uploaded) body[name] = textarea.value;
  }
  const cutoff = byId('ho-cutoff').value.trim();
  if (cutoff) body.cutoff = cutoff;
  return body;
}

function renderSummary() {
  summary.replaceChildren();
  const c = report.conservation;
  const items = [
    ['截止时刻', report.cutoff],
    ['期初车辆', `${c.opening} 辆`],
    ['确认在库', `${c.confirmed} 辆`],
    ['在途预计', `${c.in_transit} 辆`],
    ['已取消', `${c.cancelled} 辆`],
    ['车辆守恒', c.balanced ? '闭合' : '不闭合'],
    ['晚到未回传', `${report.unreceived.length} 单`],
    ['隔离记录', `${report.quarantine.length} 条`],
  ];
  const list = el('ol', undefined, 'panel-list');
  for (const [label, value] of items) {
    const li = el('li');
    li.append(el('span', label), el('strong', value, 'measure'));
    list.append(li);
  }
  summary.append(list);
}

function renderView() {
  if (!report) return;
  if (view === 'zones') {
    table(grid, [
      ['调车区', 'zone'],
      ['确认到站(单)', row => row.states['已确认到站']],
      ['在途(单)', row => row.states['在途预计']],
      ['取消(单)', row => row.states['已取消']],
      ['在途车辆', 'in_transit_quantity', 'num'],
      ['确认余缺(+余/-缺)', 'confirmed_surplus_shortage', 'num'],
      ['预测余缺(+余/-缺)', 'projected_surplus_shortage', 'num'],
    ], report.zones);
  } else if (view === 'trips') {
    table(grid, [
      ['调车区', 'zone'], ['调车单号', 'id'], ['状态', 'state'],
      ['起点', 'origin'], ['目标站', 'destination'], ['数量', 'quantity', 'num'],
      ['发车时刻', 'departed_at'], ['预计到达', 'eta'],
      ['确认到站时刻', 'arrived_at'],
      ['取消退回起点', row => (row.state === '已取消' ? (row.return_to_origin ? '是' : '否(发车前取消)') : '—')],
    ], report.trips);
  } else if (view === 'stations') {
    table(grid, [
      ['调车区', 'zone'], ['站点', 'station'], ['期初', 'opening', 'num'],
      ['确认流入', 'inflow', 'num'], ['流出', 'outflow', 'num'],
      ['取消退回', 'returns', 'num'], ['在途预计流入', 'projected_inflow', 'num'],
      ['确认余额', 'confirmed_balance', 'num'], ['预测余额', 'projected_balance', 'num'],
    ], report.stations);
  } else {
    const issues = [
      ...report.quarantine.map(row => ({kind: '隔离', id: row.key, code: row.code, detail: row.detail, line: row.line})),
      ...report.suppressed.map(row => ({kind: '重传抑制', id: row.key, code: row.code, detail: row.detail, line: row.line})),
      ...report.unreceived.map(row => ({kind: '晚到未回传', id: row.id, code: 'LATE_ARRIVAL', detail: `提交于 ${row.submitted_at}`, line: row.line})),
    ].sort((a, b) => a.line - b.line);
    table(grid, [['行号', 'line', 'num'], ['类型', 'kind'], ['调车单号', 'id'],
                 ['代码', 'code'], ['说明', 'detail']], issues);
  }
}

byId('ho-tabs').addEventListener('click', event => {
  const button = event.target.closest('.ho-view');
  if (!button) return;
  view = button.dataset.view;
  byId('ho-tabs').querySelectorAll('.ho-view').forEach(b =>
    b.setAttribute('aria-pressed', String(b === button)));
  renderView();
});

byId('handover-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    report = await query('/api/handover', await collect());
    byId('ho-id').textContent = report.manifest.handover_id.slice(0, 9);
    byId('ho-id').title = report.manifest.handover_id;
    byId('ho-cutoff-label').textContent = report.cutoff.slice(0, 16).replace('T', ' ');
    renderSummary();
    renderView();
    setNotice('交接单已生成，同输入重跑返回同一快照');
  } catch (error) {
    setNotice(error.message, true);
  }
});

for (const [name, fileId, textId] of [
  ['opening', 'ho-opening-file', 'ho-opening'],
  ['departure', 'ho-departure-file', 'ho-departure'],
  ['event', 'ho-event-file', 'ho-event'],
]) {
  byId(fileId).addEventListener('change', async event => {
    const file = event.target.files[0];
    if (!file) return;
    try {
      const [text, bytes] = await readInput(file);
      byId(textId).value = text;
      files.set(name, {text, bytes});
      setNotice(`已读取 ${file.name}`);
    } catch (error) {
      event.target.value = '';
      setNotice(error.message, true);
    }
  });
}

byId('handover-sample').addEventListener('click', async () => {
  try {
    const names = ['opening', 'departure', 'events'];
    const responses = await Promise.all(names.map(n => fetch(`/api/samples/${n}`)));
    if (responses.some(r => !r.ok)) throw Error('样例暂时不可用');
    const blobs = await Promise.all(responses.map(r => r.arrayBuffer()));
    const textIds = ['ho-opening', 'ho-departure', 'ho-event'];
    const keys = ['opening', 'departure', 'event'];
    names.forEach((_, i) => {
      const text = new TextDecoder('utf-8').decode(blobs[i]);
      byId(textIds[i]).value = text;
      files.set(keys[i], {text, bytes: blobs[i]});
    });
    setNotice('交接样例已载入');
  } catch (error) {
    setNotice(error.message, true);
  }
});
