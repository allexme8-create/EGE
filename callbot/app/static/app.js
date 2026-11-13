/* Обзвон+ — одностраничный интерфейс (без зависимостей). */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const view = () => $('#view');

const state = {
  meta: null,
  equipment: null,
  campaignId: null,
  resultsFilter: { status: 'all', search: '', lowOnly: false },
  poll: null,
  lastEventId: 0,
};

/* ------------------------------------------------------------------ утилиты */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleTimeString('ru-RU');
}
function fmtDur(seconds) {
  const s = Math.round(seconds || 0);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
function plural(n, one, few, many) {
  const abs = Math.abs(n) % 100, last = abs % 10;
  if (abs > 10 && abs < 20) return many;
  if (last > 1 && last < 5) return few;
  if (last === 1) return one;
  return many;
}
function pct(n, total) { return total ? Math.round((n / total) * 100) : 0; }

const STATUS = {
  draft: ['draft', 'Черновик', ''],
  running: ['running', 'Идёт обзвон', 'green pulse'],
  paused: ['paused', 'Пауза', 'amber'],
  completed: ['completed', 'Завершена', 'blue'],
  stopped: ['stopped', 'Остановлена', 'red'],
  pending: ['pending', 'В очереди', ''],
  calling: ['calling', 'Звоним', 'blue pulse'],
  answered: ['answered', 'Ответил', 'green'],
  done: ['done', 'Опрос завершён', 'green'],
  no_answer: ['no_answer', 'Не ответил', 'amber'],
  busy: ['busy', 'Занято', 'amber'],
  refused: ['refused', 'Отказ', 'red'],
  failed: ['failed', 'Ошибка', 'red'],
  aborted: ['aborted', 'Прервано', 'amber'],
  dnc: ['dnc', 'Не звонить', 'violet'],
};
function badge(status, extra = '') {
  const [key, label, cls] = STATUS[status] || [status, status, ''];
  return `<span class="badge ${cls} ${extra}">${cls.includes('pulse') ? '<span class="dot"></span>' : ''}${esc(label)}</span>`;
}

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = 0; el.style.transition = 'opacity .3s'; }, 3200);
  setTimeout(() => el.remove(), 3600);
}

async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch('/api' + path, opts);
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const data = await resp.json();
      detail = typeof data.detail === 'string' ? data.detail
        : Array.isArray(data.detail) ? data.detail.map(d => d.msg).join('; ')
        : JSON.stringify(data);
    } catch (e) { /* ignore */ }
    if (resp.status === 401) detail = 'Требуется авторизация';
    throw new Error(detail);
  }
  if (resp.status === 204) return null;
  const type = resp.headers.get('content-type') || '';
  return type.includes('application/json') ? resp.json() : resp.text();
}

function download(path, filename) {
  const a = document.createElement('a');
  a.href = '/api' + path;
  if (filename) a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function modal({ title, body, footer, wide = false, onMount }) {
  const host = $('#modal-host');
  host.innerHTML = `
    <div class="modal-backdrop">
      <div class="modal ${wide ? 'wide' : ''}">
        <div class="modal-head"><h2>${esc(title)}</h2><div class="spacer" style="flex:1"></div>
          <button class="btn btn-ghost btn-icon" data-close>✕</button></div>
        <div class="modal-body">${body}</div>
        ${footer ? `<div class="modal-foot">${footer}</div>` : ''}
      </div>
    </div>`;
  const close = () => { host.innerHTML = ''; };
  $$('[data-close]', host).forEach(b => b.onclick = close);
  $('.modal-backdrop', host).addEventListener('mousedown', e => { if (e.target.classList.contains('modal-backdrop')) close(); });
  if (onMount) onMount(host, close);
  return { el: host.querySelector('.modal'), close };
}

function confirmDialog(text, action, okLabel = 'Удалить') {
  modal({
    title: 'Подтверждение',
    body: `<p>${esc(text)}</p>`,
    footer: `<button class="btn" data-close>Отмена</button>
             <button class="btn btn-danger" id="confirm-ok">${esc(okLabel)}</button>`,
    onMount(host, close) {
      $('#confirm-ok', host).onclick = async () => {
        close();
        try { await action(); } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
}

/* ------------------------------------------------------------------ shell */
function setPage(title, crumb) {
  $('#page-title').textContent = title;
  $('#crumbs').textContent = crumb || '';
  document.title = `${title} — Обзвон+`;
}
function setActions(html) { $('#top-actions').innerHTML = html; }

async function loadMeta() {
  try {
    state.meta = await api('/meta');
    const m = state.meta;
    const dot = $('#mode-dot');
    dot.className = 'status-dot ' + (m.demo_mode ? 'on' : (state.equipment?.modem?.connected ? 'on' : 'off'));
    $('#mode-label').textContent = m.demo_mode ? 'Демо-режим (без модема)' : 'Режим модема';
    $('#asr-label').textContent = 'ASR: ' + ({ vosk: 'Vosk (офлайн)', whispercpp: 'whisper.cpp', http: 'HTTP-сервис', stub: 'не настроен' }[m.asr.active] || m.asr.active);
    $('#asr-label').style.color = m.asr.active === 'stub' ? '#f59e0b' : '';
    $('#tts-label').textContent = 'TTS: ' + ({ piper: 'Piper', espeak: 'espeak-ng', none: 'не настроен' }[m.tts.active] || m.tts.active);
    $('#tts-label').style.color = m.tts.active === 'none' ? '#f59e0b' : '';
    $('#version-label').textContent = m.version;
  } catch (e) {
    $('#mode-label').textContent = 'сервис недоступен';
  }
}

/* ------------------------------------------------------------------ router */
const routes = {
  dashboard: renderDashboard,
  campaigns: renderCampaigns,
  campaign: renderCampaign,
  results: renderResults,
  dnc: renderDnc,
  equipment: renderEquipment,
  settings: renderSettings,
};

async function navigate() {
  const hash = location.hash.replace(/^#\/?/, '') || 'dashboard';
  const [name, param] = hash.split('/');
  if (state.poll) { clearInterval(state.poll); state.poll = null; }
  $$('.nav a').forEach(a => a.classList.toggle('active', a.dataset.nav === name || (name === 'campaign' && a.dataset.nav === 'campaigns')));
  state.campaignId = name === 'campaign' ? Number(param) : state.campaignId;
  const handler = routes[name] || renderDashboard;
  try {
    await handler(param);
  } catch (e) {
    view().innerHTML = `<div class="card"><div class="empty"><div class="big">⚠</div>${esc(e.message)}</div></div>`;
  }
  window.scrollTo({ top: 0 });
}

window.addEventListener('hashchange', navigate);
window.addEventListener('DOMContentLoaded', async () => {
  await loadMeta();
  await navigate();
});
setInterval(loadMeta, 60000);

/* ================================================================== ОБЗОР */
async function renderDashboard() {
  setPage('Обзор', 'Главная панель');
  setActions(`<button class="btn" id="refresh-dash">↻ Обновить</button>
              <a class="btn btn-primary" href="#/campaigns">＋ Новая кампания</a>`);
  const data = await api('/dashboard');
  const t = data.totals;
  const answerRate = pct(t.answered, t.calls);
  view().innerHTML = `
    <div class="grid g4">
      ${kpi('Кампаний', t.campaigns, `${data.campaigns.filter(c => c.status === 'running').length} идут сейчас`, '☎', '#e8f0fe')}
      ${kpi('Звонков', t.calls, `сегодня: ${data.today.calls}`, '↗', '#e6f6ee')}
      ${kpi('Ответили', `${answerRate}%`, `${t.answered} из ${t.calls}`, '✓', '#fef3c7')}
      ${kpi('Ответов в таблице', t.answers, `ср. уверенность ${(t.avg_confidence * 100).toFixed(0)}%`, '▤', '#ede9fe')}
    </div>

    <div class="grid mt16" style="grid-template-columns: 1fr 360px;">
      <div class="card">
        <div class="card-head"><h2>Кампании</h2><div class="spacer"></div>
          <span class="small muted">${t.contacts} ${plural(t.contacts, 'контакт', 'контакта', 'контактов')}</span></div>
        <div id="dash-campaigns">${campaignsTable(data.campaigns)}</div>
      </div>
      <div class="grid" style="align-content:start">
        <div class="card">
          <div class="card-head"><h2>Оборудование и речь</h2></div>
          <div class="card-body" id="dash-equipment"><div class="muted small">Загрузка…</div></div>
        </div>
        <div class="card">
          <div class="card-head"><h2>Журнал</h2><div class="spacer"></div><a class="small" href="#/campaigns">все →</a></div>
          <div class="card-body"><div class="log-list" id="dash-log"><div class="muted small">Загрузка…</div></div></div>
        </div>
      </div>
    </div>

    <div class="section-title"><h2>Последние звонки</h2><div class="spacer"></div>
      <a class="small" href="#/results">Открыть таблицу ответов →</a></div>
    <div class="card table-scroll">
      <table class="tbl">
        <thead><tr><th>Звонок</th><th>Клиент</th><th>Кампания</th><th>Исход</th><th>Длительность</th><th>Время</th></tr></thead>
        <tbody>${data.last_calls.map(c => `
          <tr class="call-row ${c.outcome === 'running' ? 'live' : ''}">
            <td class="mono">#${c.id}</td>
            <td><div class="row"><div class="avatar">${esc((c.name || c.phone).slice(0, 1))}</div>
              <div><div>${esc(c.name || '—')}</div><div class="small muted mono">${esc(c.phone)}</div></div></div></td>
            <td class="small">${esc(c.campaign_name)}</td>
            <td>${badge(c.outcome)}</td>
            <td class="mono">${fmtDur(c.duration_sec)}</td>
            <td class="small muted">${fmtDate(c.started_at)}</td>
          </tr>`).join('') || `<tr><td colspan="6"><div class="empty">Звонков пока не было</div></td></tr>`}
        </tbody>
      </table>
    </div>`;

  $('#refresh-dash').onclick = () => navigate();
  loadDashboardEquipment();
  loadEvents($('#dash-log'), 12);
}

function kpi(label, value, sub, icon, color) {
  return `<div class="card kpi">
    <div class="kpi-head"><div><div class="label">${esc(label)}</div>
      <div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>
      <div class="icon" style="background:${color}">${icon}</div></div></div>`;
}

async function loadDashboardEquipment() {
  try {
    const data = await api('/equipment');
    state.equipment = data;
    const m = data.modem;
    $('#dash-equipment').innerHTML = `
      <div class="row" style="justify-content:space-between"><span class="muted small">Режим</span>
        <span>${data.modem.demo ? '<span class="badge green">Демо-эмулятор</span>' : '<span class="badge blue">Модем</span>'}</span></div>
      <div class="row mt8" style="justify-content:space-between"><span class="muted small">Модем</span>
        <span class="small">${esc(m.model || (m.port ? 'порт ' + m.port : 'не выбран'))}</span></div>
      <div class="row mt8" style="justify-content:space-between"><span class="muted small">SIM / сеть</span>
        <span class="small">${m.sim_ready ? 'готово' : 'нет'} · ${esc(m.operator || '—')} · ${m.signal ?? 0}/31</span></div>
      <div class="row mt8" style="justify-content:space-between"><span class="muted small">Распознавание</span>
        <span class="small">${esc(asrLabel(data.asr))}</span></div>
      <div class="row mt8" style="justify-content:space-between"><span class="muted small">Озвучивание</span>
        <span class="small">${esc({ piper: 'Piper', espeak: 'espeak-ng', none: 'не настроено' }[data.tts.active] || data.tts.active)}</span></div>
      <div class="row mt8" style="justify-content:space-between"><span class="muted small">Время звонков</span>
        <span class="small">${esc(data.calling_hours || 'без ограничений')}
        ${data.within_calling_hours ? '<span class="badge green" style="height:19px">можно</span>' : '<span class="badge amber" style="height:19px">пауза</span>'}</span></div>
      ${m.error ? `<div class="small" style="color:var(--red);margin-top:8px">${esc(m.error)}</div>` : ''}
      <a class="btn btn-sm mt12" href="#/equipment">Настройка оборудования →</a>`;
  } catch (e) {
    $('#dash-equipment').innerHTML = `<div class="small" style="color:var(--red)">${esc(e.message)}</div>`;
  }
}

function asrLabel(asr) {
  if (!asr) return '—';
  return { vosk: 'Vosk (офлайн)', whispercpp: 'whisper.cpp', http: 'HTTP-сервис', stub: 'не настроено' }[asr.active] || asr.active;
}

async function loadEvents(container, limit = 40, campaignId = null) {
  if (!container) return;
  try {
    const events = await api(`/events?limit=${limit}` + (campaignId ? `&campaign_id=${campaignId}` : ''));
    container.innerHTML = events.slice(-limit).reverse().map(e => `
      <div class="log-line ${e.level}">
        <span class="ts">${fmtTime(e.ts)}</span><span class="msg">${esc(e.message)}</span>
      </div>`).join('') || '<div class="muted small">Событий пока нет</div>';
  } catch (e) {
    container.innerHTML = `<div class="small" style="color:var(--red)">${esc(e.message)}</div>`;
  }
}

function campaignsTable(campaigns) {
  if (!campaigns.length) return `<div class="empty"><div class="big">☎</div>Кампаний пока нет.<br>
    <a class="btn btn-primary mt12" href="#/campaigns">Создать первую кампанию</a></div>`;
  return `<table class="tbl">
    <thead><tr><th>Кампания</th><th>Статус</th><th>Прогресс</th><th class="num">Готово</th><th class="num">Ответов</th><th></th></tr></thead>
    <tbody>${campaigns.map(c => {
      const s = c.stats || {};
      return `<tr>
        <td><a href="#/campaign/${c.id}"><b>${esc(c.name)}</b></a>
          <div class="small muted">${c.mode === 'demo' ? 'демо-режим' : 'модем'} · ${c.questions_count ?? '—'} ${plural(c.questions_count || 0, 'вопрос', 'вопроса', 'вопросов')}</div></td>
        <td>${badge(c.status)}</td>
        <td style="min-width:150px"><div class="progress"><i style="width:${s.progress || 0}%"></i></div>
          <div class="small muted mt8">${s.progress || 0}% из ${s.total || 0} контактов</div></td>
        <td class="num">${s.done || 0}</td>
        <td class="num">${s.answers || 0}</td>
        <td class="right"><a class="btn btn-sm" href="#/campaign/${c.id}">Открыть</a></td>
      </tr>`;
    }).join('')}</tbody></table>`;
}

/* ================================================================== КАМПАНИИ */
async function renderCampaigns() {
  setPage('Кампании', 'Обзвон клиентов');
  setActions(`<button class="btn btn-primary" id="new-campaign">＋ Новая кампания</button>`);
  const campaigns = await api('/campaigns');
  view().innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Все кампании</h2><div class="spacer"></div>
        <span class="muted small">${campaigns.length} ${plural(campaigns.length, 'кампания', 'кампании', 'кампаний')}</span></div>
      <div id="campaigns-list">${campaignsTable(campaigns)}</div>
    </div>
    <div class="grid g2 mt16">
      <div class="card"><div class="card-body">
        <h3>Как это работает</h3>
        <ol class="small muted" style="margin:8px 0 0;padding-left:18px;line-height:1.8">
          <li>Создайте кампанию и добавьте вопросы сценария.</li>
          <li>Загрузите контакты (CSV: имя; телефон).</li>
          <li>Запустите обзвон — модем набирает номера, задаёт вопросы и записывает ответы.</li>
          <li>Речь распознаётся, ответы превращаются в значения и складываются в таблицу.</li>
          <li>Выгрузите результат в Excel (XLSX/CSV).</li>
        </ol>
      </div></div>
      <div class="card"><div class="card-body">
        <h3>Типы вопросов</h3>
        <div class="small muted mt8" style="line-height:1.9">
          <b>Да/Нет</b> — «Вам интересно предложение?»<br>
          <b>Вариант</b> — выбор из списка (голосом или клавишей)<br>
          <b>Оценка</b> — от 1 до 5 («пять», «4»)<br>
          <b>Число</b> — «сорок два», «35»<br>
          <b>Свободный</b> — произвольная фраза целиком
        </div>
      </div></div>
    </div>`;
  $('#new-campaign').onclick = openCampaignModal;
}

function openCampaignModal(existing = null) {
  modal({
    title: existing ? 'Редактировать кампанию' : 'Новая кампания',
    body: `
      <div class="fieldset">
        <label class="field">Название
          <input class="input" id="c-name" value="${esc(existing?.name || '')}" placeholder="Например: Опрос по новому тарифу"></label>
        <label class="field">Приветствие (зачитывается после ответа)
          <textarea class="textarea" id="c-intro" placeholder="Здравствуйте! Это компания «...». Мы проводим короткий опрос, займёт около минуты.">${esc(existing?.intro_text || '')}</textarea></label>
        <label class="field">Завершение
          <textarea class="textarea" id="c-outro" placeholder="Спасибо за ответы! Всего доброго.">${esc(existing?.outro_text || '')}</textarea></label>
        <div class="grid g3">
          <label class="field">Повторных попыток
            <select class="select" id="c-retry">
              ${[0, 1, 2, 3].map(n => `<option value="${n}" ${(existing?.retry_limit ?? 1) === n ? 'selected' : ''}>${n}</option>`).join('')}
            </select><span class="hint">если не ответили</span></label>
          <label class="field">Ждать ответа, с
            <input class="input" type="number" min="1" max="60" step="1" id="c-answer" value="${existing?.answer_seconds ?? 6}">
            <span class="hint">после вопроса</span></label>
          <label class="field">Таймаут звонка, с
            <input class="input" type="number" min="10" max="300" id="c-timeout" value="${existing?.call_timeout ?? 40}"></label>
        </div>
        <div class="grid g2">
          <div><label class="check"><input type="checkbox" id="c-dtmf" ${existing?.dtmf_enabled !== false ? 'checked' : ''}>
            Разрешить ответ клавишами (DTMF)</label>
          <label class="check mt8"><input type="checkbox" id="c-record" ${existing?.record_answers !== false ? 'checked' : ''}>
            Записывать аудио ответов</label></div>
          <label class="field">Режим запуска
            <select class="select" id="c-mode">
              <option value="modem" ${existing?.mode === 'modem' ? 'selected' : ''}>GSM-модем (реальные звонки)</option>
              <option value="demo" ${existing?.mode !== 'modem' ? 'selected' : ''}>Демо-эмулятор (без оборудования)</option>
            </select><span class="hint">демо удобно для проверки сценария</span></label>
        </div>
      </div>`,
    footer: `<button class="btn" data-close>Отмена</button>
             <button class="btn btn-primary" id="c-save">${existing ? 'Сохранить' : 'Создать'}</button>`,
    onMount(host, close) {
      $('#c-save', host).onclick = async () => {
        const payload = {
          name: $('#c-name', host).value.trim(),
          intro_text: $('#c-intro', host).value,
          outro_text: $('#c-outro', host).value,
          retry_limit: Number($('#c-retry', host).value),
          answer_seconds: Number($('#c-answer', host).value),
          call_timeout: Number($('#c-timeout', host).value),
          dtmf_enabled: $('#c-dtmf', host).checked,
          record_answers: $('#c-record', host).checked,
          mode: $('#c-mode', host).value,
        };
        if (!payload.name) { toast('Укажите название кампании', 'error'); return; }
        try {
          if (existing) {
            await api(`/campaigns/${existing.id}`, { method: 'PATCH', body: payload });
            close(); toast('Сохранено', 'success'); navigate();
          } else {
            const created = await api('/campaigns', { method: 'POST', body: payload });
            close(); location.hash = `#/campaign/${created.id}`;
          }
        } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
}

/* ================================================================== КАМПАНИЯ */
async function renderCampaign(param) {
  const id = Number(param);
  if (!id) { location.hash = '#/campaigns'; return; }
  state.campaignId = id;
  const [campaign, contacts] = await Promise.all([api(`/campaigns/${id}`), api(`/campaigns/${id}/contacts?limit=500`)]);
  const stats = campaign.stats;
  setPage(campaign.name, 'Кампании / ' + campaign.name);
  setActions(`
    ${campaign.status === 'running'
      ? `<button class="btn" id="c-pause">⏸ Пауза</button><button class="btn btn-danger" id="c-stop">■ Остановить</button>`
      : campaign.status === 'paused'
        ? `<button class="btn btn-primary" id="c-resume">▶ Продолжить</button><button class="btn btn-danger" id="c-stop">■ Остановить</button>`
        : `<button class="btn btn-primary" id="c-start">▶ Запустить обзвон</button>`}
    <button class="btn" id="c-export">⤓ Excel</button>
    <a class="btn" href="#/results/${id}">▤ Таблица ответов</a>`);

  view().innerHTML = `
    <div class="grid g4">
      ${kpi('Контактов', stats.total, `в очереди: ${stats.pending}`, '👥', '#e8f0fe')}
      ${kpi('Опрос завершён', stats.done, `отказов: ${stats.refused}`, '✓', '#e6f6ee')}
      ${kpi('Звонков', stats.calls, `ответили: ${stats.answered}`, '↗', '#fef3c7')}
      ${kpi('Ответов', stats.answers, `ср. уверенность ${(stats.avg_confidence * 100).toFixed(0)}%`, '▤', '#ede9fe')}
    </div>

    <div class="split mt16">
      <div class="grid" style="align-content:start">
        <div class="card">
          <div class="card-head"><h2>Ход обзвона</h2><div class="spacer"></div>
            ${badge(campaign.status, campaign.status === 'running' ? 'pulse' : '')}</div>
          <div class="card-body">
            <div class="progress"><i id="run-progress" style="width:${stats.progress}%"></i></div>
            <div class="row mt8 small muted"><span id="run-progress-label">${stats.progress}% обработано</span>
              <span class="right" id="run-mode">${campaign.mode === 'demo' ? 'Демо-эмулятор' : 'GSM-модем · ' + (state.equipment?.modem?.port || 'порт не выбран')}</span></div>
            <div class="grid g4 mt16">
              ${['pending', 'done', 'refused', 'no_answer'].map(k => `
                <div><div class="small muted">${STATUS[k][1]}</div><div style="font-size:19px;font-weight:700" id="stat-${k}">${stats[k] || 0}</div></div>`).join('')}
            </div>
            ${campaign.status === 'running' ? '<div class="log-list mt16" id="live-log" style="max-height:260px"></div>' : ''}
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h2>Сценарий вопросов</h2><div class="spacer"></div>
            <button class="btn btn-sm" id="add-question">＋ Вопрос</button></div>
          <div class="card-body" id="questions-box"></div>
        </div>

        <div class="card">
          <div class="card-head"><h2>Контакты</h2><div class="spacer"></div>
            <button class="btn btn-sm" id="add-contact">＋ Контакт</button>
            <button class="btn btn-sm" id="import-contacts">⤒ Импорт CSV</button>
            <button class="btn btn-sm" id="sample-contacts">⤓ Шаблон</button></div>
          <div class="card-body">
            <div id="import-box" class="hidden">
              <div class="dropzone" id="dropzone">
                <div class="big">⤒</div>
                Перетащите CSV/TXT с контактами или нажмите, чтобы выбрать файл
                <div class="small mt8">Колонки: имя; телефон; [id]. Разделитель — ; , или таб. Кодировка UTF-8/CP1251.</div>
              </div>
              <div class="row mt8"><label class="check"><input type="checkbox" id="import-reset"> заменить существующий список</label></div>
              <input type="file" id="file-input" accept=".csv,.txt,.tsv" class="hidden">
              <div id="import-result" class="small mt8"></div>
            </div>
            <div class="table-scroll" style="max-height:420px">
              <table class="tbl">
                <thead><tr><th>#</th><th>Имя</th><th>Телефон</th><th>Статус</th><th class="num">Попыток</th><th></th></tr></thead>
                <tbody id="contacts-body"></tbody>
              </table>
            </div>
            <div class="small muted mt8" id="contacts-count">${contacts.total} ${plural(contacts.total, 'контакт', 'контакта', 'контактов')}</div>
          </div>
        </div>
      </div>

      <div class="grid" style="align-content:start">
        <div class="card">
          <div class="card-head"><h2>Параметры</h2><div class="spacer"></div>
            <button class="btn btn-sm btn-ghost" id="edit-campaign">✎</button></div>
          <div class="card-body small" style="line-height:2">
            <div class="row"><span class="muted">Режим</span><span class="right">${campaign.mode === 'demo' ? 'демо-эмулятор' : 'GSM-модем'}</span></div>
            <div class="row"><span class="muted">Ответ клавишами</span><span class="right">${campaign.dtmf_enabled ? 'да' : 'нет'}</span></div>
            <div class="row"><span class="muted">Запись аудио</span><span class="right">${campaign.record_answers ? 'да' : 'нет'}</span></div>
            <div class="row"><span class="muted">Повторных попыток</span><span class="right">${campaign.retry_limit}</span></div>
            <div class="row"><span class="muted">Ждать ответ</span><span class="right">${campaign.answer_seconds} с</span></div>
            <div class="row"><span class="muted">Создана</span><span class="right">${fmtDate(campaign.created_at)}</span></div>
          </div>
        </div>
        <div class="card">
          <div class="card-head"><h2>Журнал кампании</h2></div>
          <div class="card-body"><div class="log-list" id="campaign-log"></div></div>
        </div>
        <div class="card"><div class="card-body row wrap">
          <button class="btn btn-sm" id="export-csv">CSV</button>
          <button class="btn btn-sm" id="export-json">JSON</button>
          <button class="btn btn-sm" id="export-transcripts">Расшифровки</button>
          <button class="btn btn-sm" id="reset-failed">↻ Неудачные в очередь</button>
          <button class="btn btn-sm btn-danger right" id="delete-campaign">Удалить</button>
        </div></div>
      </div>
    </div>`;

  renderQuestions(campaign.questions);
  const body = $('#contacts-body');
  const drawContacts = rows => {
    body.innerHTML = contactsBody(rows);
    $$('[data-del-contact]', body).forEach(btn => btn.onclick = () => deleteContact(Number(btn.dataset.delContact)));
  };
  drawContacts(contacts.rows);
  $('#campaign-log') && loadEvents($('#campaign-log'), 60, id);
  if (campaign.status === 'running') loadEvents($('#live-log'), 30, id);

  $('#c-start').onclick = () => startCampaign(id);
  $('#c-pause') && ($('#c-pause').onclick = () => campaignAction(id, 'pause'));
  $('#c-resume') && ($('#c-resume').onclick = () => campaignAction(id, 'resume'));
  $('#c-stop') && ($('#c-stop').onclick = () => confirmDialog('Остановить обзвон? Контакты останутся в списке.',
    () => campaignAction(id, 'stop'), 'Остановить'));
  $('#c-export').onclick = () => download(`/campaigns/${id}/export?fmt=xlsx`);
  $('#export-csv').onclick = () => download(`/campaigns/${id}/export?fmt=csv`);
  $('#export-json').onclick = () => download(`/campaigns/${id}/export?fmt=json`);
  $('#export-transcripts').onclick = () => download(`/campaigns/${id}/export/transcripts`);
  $('#edit-campaign').onclick = () => openCampaignModal(campaign);
  $('#add-question').onclick = () => openQuestionModal(id, campaign.questions.length);
  $('#add-contact').onclick = () => openContactModal(id);
  $('#import-contacts').onclick = () => $('#import-box').classList.toggle('hidden');
  $('#sample-contacts').onclick = () => download(`/campaigns/${id}/contacts/sample`, 'contacts_sample.csv');
  $('#reset-failed').onclick = async () => {
    try {
      const r = await api(`/campaigns/${id}/reset-failed`, { method: 'POST' });
      toast(`Возвращено контактов: ${r.updated}`, 'success'); navigate();
    } catch (e) { toast(e.message, 'error'); }
  };
  $('#delete-campaign').onclick = () => confirmDialog('Удалить кампанию вместе с ответами и звонками?',
    async () => { await api(`/campaigns/${id}`, { method: 'DELETE' }); location.hash = '#/campaigns'; });
  setupImport(id);

  if (campaign.status === 'running' || campaign.status === 'paused') {
    if (state.poll) clearInterval(state.poll);
    state.poll = setInterval(async () => {
      try {
        const s = await api(`/campaigns/${id}/stats`);
        updateStats(s);
        if (s.active) {
          const c = await api(`/campaigns/${id}/contacts?limit=500`);
          const b = $('#contacts-body');
          if (b) { b.innerHTML = contactsBody(c.rows); $$('[data-del-contact]', b).forEach(btn => btn.onclick = () => deleteContact(Number(btn.dataset.delContact))); }
        }
        if ((!s.active && s.pending === 0) || s.status === 'completed') {
          clearInterval(state.poll); state.poll = null;
        } else {
          loadEvents($('#live-log'), 30, id);
        }
      } catch (e) { /* игнорируем сетевые сбои опроса */ }
    }, 2000);
  }
}

function contactsBody(rows) {
  if (!rows.length) return `<tr><td colspan="6"><div class="empty">Нет контактов. Загрузите CSV или добавьте вручную.</div></td></tr>`;
  return rows.map((c, i) => `<tr>
    <td class="mono small">${i + 1}</td>
    <td>${esc(c.name || '—')}</td>
    <td class="mono">${esc(c.phone)}</td>
    <td>${badge(c.status)}</td>
    <td class="num">${c.attempts}</td>
    <td class="right"><button class="btn btn-ghost btn-sm" data-del-contact="${c.id}">✕</button></td>
  </tr>`).join('');
}

async function deleteContact(contactId) {
  try {
    await api(`/contacts/${contactId}`, { method: 'DELETE' });
    toast('Контакт удалён'); navigate();
  } catch (e) { toast(e.message, 'error'); }
}

function updateStats(s) {
  const bar = $('#run-progress');
  if (!bar) return;
  bar.style.width = `${s.progress}%`;
  $('#run-progress-label').textContent = `${s.progress}% обработано`;
  ['pending', 'done', 'refused', 'no_answer'].forEach(k => {
    const el = $('#stat-' + k);
    if (el) el.textContent = s[k] || 0;
  });
}

async function startCampaign(id) {
  try {
    const camp = await api(`/campaigns/${id}`);
    const mode = camp.mode || (state.meta?.demo_mode ? 'demo' : 'modem');
    const extra = mode === 'modem'
      ? `<div class="small" style="color:var(--amber);margin-top:10px">Звонки пойдут с SIM-карты МегаФона через модем.
         Убедитесь, что порт модема и аудиоустройства указаны в «Оборудовании».</div>`
      : `<div class="small muted mt12">Демо-режим: реальные звонки не совершаются, ответы эмулируются —
         удобно проверить сценарий и посмотреть, как заполняется таблица.</div>`;
    modal({
      title: 'Запустить обзвон',
      body: `<p>Кампания <b>${esc(camp.name)}</b>. Контактов в очереди: <b>${camp.stats.pending}</b>,
             вопросов: <b>${camp.questions.length}</b>.</p>
             <label class="field mt12">Режим
               <select class="select" id="run-mode">
                 <option value="demo" ${mode === 'demo' ? 'selected' : ''}>Демо-эмулятор (без оборудования)</option>
                 <option value="modem" ${mode === 'modem' ? 'selected' : ''}>GSM-модем — реальные звонки</option>
               </select></label>${extra}`,
      footer: `<button class="btn" data-close>Отмена</button>
               <button class="btn btn-primary" id="run-go">▶ Запустить</button>`,
      onMount(host, close) {
        $('#run-go', host).onclick = async () => {
          try {
            const m = $('#run-mode', host).value;
            await api(`/campaigns/${id}/start?mode=${m}`, { method: 'POST' });
            close(); toast('Обзвон запущен', 'success'); navigate();
          } catch (e) { toast(e.message, 'error'); }
        };
      },
    });
  } catch (e) { toast(e.message, 'error'); }
}

async function campaignAction(id, action) {
  try {
    await api(`/campaigns/${id}/${action}`, { method: 'POST' });
    toast({ pause: 'Пауза', resume: 'Продолжено', stop: 'Остановлено' }[action], 'success');
    navigate();
  } catch (e) { toast(e.message, 'error'); }
}

/* ---------------------------------------------------------------- вопросы */
const QTYPE = { yes_no: 'Да / Нет', choice: 'Выбор варианта', rating: 'Оценка 1–5', number: 'Число', free: 'Свободный ответ' };

function renderQuestions(questions) {
  const box = $('#questions-box');
  if (!questions.length) {
    box.innerHTML = `<div class="empty"><div class="big">✎</div>Сценарий пуст. Добавьте вопросы, которые робот задаст клиенту.</div>`;
    return;
  }
  box.innerHTML = questions.map((q, i) => `
    <div class="question-item ${i ? 'mt12' : ''}" data-q="${q.id}">
      <div class="qhead">
        <div class="qnum">${i + 1}</div>
        <div class="qtext">${esc(q.text)}</div>
        <span class="badge blue">${QTYPE[q.type] || q.type}</span>
        <button class="btn btn-ghost btn-sm" data-edit-q="${q.id}">✎</button>
        <button class="btn btn-ghost btn-sm" data-del-q="${q.id}">✕</button>
      </div>
      ${q.options?.length ? `<div class="qopts">${q.options.map(o => `<span class="qopt">${esc(o)}</span>`).join('')}</div>` : ''}
      <div class="qaudio">
        ${q.audio_path
          ? `<span class="audio-tag">🎵 своё аудио</span><button class="audio-btn" data-play-q="${q.id}">▶ прослушать</button>
             <button class="btn btn-ghost btn-sm" data-drop-audio="${q.id}">убрать</button>`
          : `<span class="small muted">озвучка: синтез речи</span>
             <label class="small" style="cursor:pointer;color:var(--blue)">загрузить WAV
               <input type="file" class="hidden" data-upload-q="${q.id}" accept=".wav,.mp3,.ogg,.amr"></label>`}
        <span class="small muted right">макс. ответ ${q.max_seconds || '—'} с</span>
      </div>
    </div>`).join('');

  $$('[data-edit-q]', box).forEach(b => b.onclick = () => {
    const q = questions.find(x => x.id === Number(b.dataset.editQ));
    openQuestionModal(state.campaignId, q.position, q);
  });
  $$('[data-del-q]', box).forEach(b => b.onclick = () => confirmDialog('Удалить вопрос?',
    async () => { await api(`/questions/${b.dataset.delQ}`, { method: 'DELETE' }); navigate(); }));
  $$('[data-upload-q]', box).forEach(input => input.onchange = async () => {
    const file = input.files[0];
    if (!file) return;
    const fd = new FormData(); fd.append('file', file);
    try {
      await api(`/questions/${input.dataset.uploadQ}/audio`, { method: 'POST', body: fd });
      toast('Аудио вопроса загружено', 'success'); navigate();
    } catch (e) { toast(e.message, 'error'); }
  });
  $$('[data-play-q]', box).forEach(b => b.onclick = () => {
    new Audio(`/api/questions/${b.dataset.playQ}/audio-file?ts=${Date.now()}`).play()
      .catch(() => toast('Не удалось воспроизвести', 'error'));
  });
  $$('[data-drop-audio]', box).forEach(b => b.onclick = async () => {
    await api(`/questions/${b.dataset.dropAudio}/audio`, { method: 'DELETE' }); navigate();
  });
}

function openQuestionModal(campaignId, position, existing = null) {
  modal({
    title: existing ? 'Редактировать вопрос' : 'Новый вопрос',
    body: `
      <div class="fieldset">
        <label class="field">Текст вопроса (робот прочитает его вслух)
          <textarea class="textarea" id="q-text" placeholder="Вам интересно предложение подключить домашний интернет?">${esc(existing?.text || '')}</textarea></label>
        <div class="grid g2">
          <label class="field">Тип ответа
            <select class="select" id="q-type">
              ${Object.entries(QTYPE).map(([k, v]) => `<option value="${k}" ${existing?.type === k ? 'selected' : ''}>${v}</option>`).join('')}
            </select></label>
          <label class="field">Максимальная длительность ответа, с
            <input class="input" type="number" id="q-max" min="1" max="60" value="${existing?.max_seconds || ''}" placeholder="по умолчанию">
            </label>
        </div>
        <label class="field" id="q-options-wrap">Варианты ответа (по одному в строке)
          <textarea class="textarea" id="q-options" placeholder="Интернет\nСвязь\nТелевидение">${esc((existing?.options || []).join('\n'))}</textarea>
          <span class="hint">Клиент может назвать вариант или нажать его номер на клавиатуре.</span></label>
        <div class="row"><button class="btn btn-sm" id="q-preview" type="button">🔊 Проверить озвучку</button>
          <span class="small muted">синтез речи сервера (Piper/espeak) или загрузите WAV после сохранения</span></div>
      </div>`,
    footer: `<button class="btn" data-close>Отмена</button>
             <button class="btn btn-primary" id="q-save">Сохранить</button>`,
    onMount(host, close) {
      const typeSel = $('#q-type', host);
      const syncOptions = () => {
        $('#q-options-wrap', host).classList.toggle('hidden', !['choice'].includes(typeSel.value));
      };
      typeSel.onchange = syncOptions; syncOptions();
      $('#q-save', host).onclick = async () => {
        const text = $('#q-text', host).value.trim();
        if (!text) { toast('Введите текст вопроса', 'error'); return; }
        const payload = {
          text,
          type: typeSel.value,
          options: $('#q-options', host).value.split('\n').map(s => s.trim()).filter(Boolean),
          max_seconds: $('#q-max', host).value ? Number($('#q-max', host).value) : null,
        };
        try {
          if (existing) await api(`/questions/${existing.id}`, { method: 'PATCH', body: payload });
          else await api(`/campaigns/${campaignId}/questions`, { method: 'POST', body: payload });
          close(); toast('Сохранено', 'success'); navigate();
        } catch (e) { toast(e.message, 'error'); }
      };
      $('#q-preview', host).onclick = async () => {
        const text = $('#q-text', host).value.trim();
        if (!text) { toast('Введите текст', 'error'); return; }
        try {
          const resp = await fetch('/api/tts/preview', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
          });
          if (!resp.ok) throw new Error((await resp.json()).detail || 'Ошибка синтеза');
          const blob = await resp.blob();
          new Audio(URL.createObjectURL(blob)).play();
        } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
}

function openContactModal(campaignId) {
  modal({
    title: 'Добавить контакт',
    body: `<div class="fieldset">
        <label class="field">Имя<input class="input" id="ct-name" placeholder="Иван Петров"></label>
        <label class="field">Телефон<input class="input" id="ct-phone" placeholder="+7 999 123-45-67"></label>
        <span class="small muted">Можно вводить в любом формате: 8 999…, +7 (999)…, 9991234567.</span>
      </div>`,
    footer: `<button class="btn" data-close>Отмена</button><button class="btn btn-primary" id="ct-save">Добавить</button>`,
    onMount(host, close) {
      $('#ct-save', host).onclick = async () => {
        try {
          await api(`/campaigns/${campaignId}/contacts`, {
            method: 'POST',
            body: { name: $('#ct-name', host).value, phone: $('#ct-phone', host).value },
          });
          close(); toast('Контакт добавлен', 'success'); navigate();
        } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
}

function setupImport(campaignId) {
  const dz = $('#dropzone');
  const input = $('#file-input');
  if (!dz || !input) return;
  dz.onclick = () => input.click();
  dz.ondragover = e => { e.preventDefault(); dz.classList.add('over'); };
  dz.ondragleave = () => dz.classList.remove('over');
  dz.ondrop = e => { e.preventDefault(); dz.classList.remove('over'); upload(e.dataTransfer.files[0]); };
  input.onchange = () => upload(input.files[0]);
  async function upload(file) {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    fd.append('reset', $('#import-reset').checked ? 'true' : 'false');
    try {
      const res = await api(`/campaigns/${campaignId}/contacts/import`, { method: 'POST', body: fd });
      toast(`Добавлено контактов: ${res.imported}`, 'success');
      $('#import-result').innerHTML = `${res.imported} добавлено, ${res.skipped} пропущено`
        + (res.warnings?.length ? `<div class="muted mt8">${res.warnings.slice(0, 8).map(esc).join('<br>')}</div>` : '');
      setTimeout(navigate, 900);
    } catch (e) { toast(e.message, 'error'); }
  }
}

/* ================================================================== ТАБЛИЦА ОТВЕТОВ */
async function renderResults(param) {
  let campaignId = Number(param) || state.campaignId;
  const campaigns = await api('/campaigns');
  if (!campaigns.length) {
    setPage('Таблица ответов', 'Результаты');
    setActions('');
    view().innerHTML = `<div class="card"><div class="empty"><div class="big">▤</div>
      Пока нет кампаний.<br><a class="btn btn-primary mt12" href="#/campaigns">Создать кампанию</a></div></div>`;
    return;
  }
  if (!campaigns.find(c => c.id === campaignId)) campaignId = campaigns[0].id;
  state.campaignId = campaignId;
  const data = await api(`/campaigns/${campaignId}/results`);
  const camp = data.campaign;
  setPage('Таблица ответов', `Кампании / ${camp.name} / Ответы`);
  setActions(`
    <select class="select" id="results-campaign" style="width:auto;min-width:220px">
      ${campaigns.map(c => `<option value="${c.id}" ${c.id === campaignId ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}
    </select>
    <button class="btn" id="recalc">↻ Пересобрать ответы</button>
    <button class="btn btn-primary" id="export-xlsx">⤓ Скачать Excel</button>`);

  const qCount = data.questions.length;
  const rows = data.rows;
  const totals = {
    done: rows.filter(r => r.contact.status === 'done').length,
    answeredAnswers: rows.reduce((n, r) => n + r.cells.filter(Boolean).length, 0),
    low: data.low_confidence_total,
  };
  view().innerHTML = `
    <div class="grid g4">
      ${kpi('Строк в таблице', rows.length, `${qCount} ${plural(qCount, 'колонка вопросов', 'колонки вопросов', 'колонок вопросов')}`, '▤', '#e8f0fe')}
      ${kpi('Распознано ответов', totals.answeredAnswers, `из ${rows.length * qCount} возможных`, '✓', '#e6f6ee')}
      ${kpi('Требуют проверки', totals.low, 'низкая уверенность ASR', '⚠', '#fef3c7')}
      ${kpi('Ср. уверенность', `${((data.stats?.avg_confidence || 0) * 100).toFixed(0)}%`, `порог ${(data.min_confidence * 100).toFixed(0)}%`, '◔', '#ede9fe')}
    </div>

    <div class="card mt16">
      <div class="card-head wrap">
        <div class="pill-toggle" id="res-filter">
          <button data-f="all" class="active">Все</button>
          <button data-f="answered">С ответами</button>
          <button data-f="low">⚠ Проверить</button>
          <button data-f="failed">Не дозвонились</button>
        </div>
        <input class="input" id="res-search" placeholder="Поиск по имени или телефону" style="max-width:280px;margin-left:auto">
        <button class="btn btn-sm" id="columns-toggle">Колонки</button>
      </div>
      <div class="table-scroll" style="max-height:70vh">
        <table class="tbl results-table" id="results-table">
          <thead><tr>
            <th class="sticky" style="min-width:170px">Клиент</th>
            <th class="sticky" style="min-width:150px">Телефон</th>
            <th class="sticky" style="min-width:120px">Статус</th>
            ${data.questions.map((q, i) => `<th class="q" title="${esc(q.text)}">
              <div class="small muted" style="text-transform:none;letter-spacing:0;margin-bottom:4px">
                Вопрос ${i + 1} · ${{ yes_no: 'да/нет', choice: 'выбор', rating: 'оценка', number: 'число', free: 'свободный' }[q.type] || q.type}
              </div>
              ${esc(q.text)}</th>`).join('')}
            <th class="nowrap">Звонок</th>
          </tr></thead>
          <tbody id="results-body"></tbody>
        </table>
      </div>
      <div class="card-body row small muted" style="border-top:1px solid var(--line)">
        <span id="results-foot"></span>
        <span class="right">Кликните по ячейке, чтобы поправить ответ вручную</span>
      </div>
    </div>`;

  $('#results-campaign').onchange = e => { location.hash = `#/results/${e.target.value}`; };
  $('#export-xlsx').onclick = () => download(`/campaigns/${campaignId}/export?fmt=xlsx`);
  $('#recalc').onclick = async () => {
    try { const r = await api('/answers/recalc', { method: 'POST', body: {} }); toast(`Пересобрано ответов: ${r.updated}`, 'success'); navigate(); }
    catch (e) { toast(e.message, 'error'); }
  };
  $$('#res-filter button').forEach(b => b.onclick = () => {
    $$('#res-filter button').forEach(x => x.classList.toggle('active', x === b));
    state.resultsFilter.status = b.dataset.f; drawRows();
  });
  $('#res-search').oninput = e => { state.resultsFilter.search = e.target.value.toLowerCase(); drawRows(); };
  $('#columns-toggle').onclick = () => toggleColumns();

  function drawRows() {
    const f = state.resultsFilter;
    let list = rows;
    if (f.status === 'answered') list = rows.filter(r => r.cells.some(Boolean));
    else if (f.status === 'low') list = rows.filter(r => r.cells.some(c => c && c.low_confidence && !c.reviewed));
    else if (f.status === 'failed') list = rows.filter(r => ['failed', 'no_answer', 'refused', 'dnc'].includes(r.contact.status));
    if (f.search) {
      list = list.filter(r => (r.contact.name || '').toLowerCase().includes(f.search)
        || r.contact.phone.includes(f.search));
    }
    $('#results-body').innerHTML = list.map(r => {
      const c = r.contact;
      return `<tr>
        <td class="sticky"><div class="row"><div class="avatar">${esc((c.name || c.phone).slice(0, 1).toUpperCase())}</div>
          <span>${esc(c.name || '—')}</span></div></td>
        <td class="sticky mono">${esc(c.phone)}</td>
        <td class="sticky">${badge(c.status)}</td>
        ${r.cells.map((cell, idx) => cellCell(cell, r, idx)).join('')}
        <td class="small muted nowrap">${r.call ? `<a href="#" data-call="${r.call.call_id}">#${r.call.call_id}</a> · ${fmtDate(r.call.started_at)}` : '—'}</td>
      </tr>`;
    }).join('') || `<tr><td colspan="${4 + qCount}"><div class="empty">Нет строк по фильтру</div></td></tr>`;
    $('#results-foot').textContent = `Показано ${list.length} из ${rows.length} строк`;
    $$('[data-answer]').forEach(td => td.onclick = e => {
      if (e.target.closest('button,audio,a')) return;
      openAnswerModal(Number(td.dataset.answer), data);
    });
    $$('[data-call]').forEach(a => a.onclick = e => { e.preventDefault(); openCallModal(Number(a.dataset.call)); });
    $$('[data-audio-answer]').forEach(b => b.onclick = e => {
      e.stopPropagation();
      new Audio(`/api/answers/${b.dataset.audioAnswer}/audio`).play()
        .catch(() => toast('Аудио ответа недоступно', 'error'));
    });
  }
  drawRows();
}

function cellCell(cell, row, idx) {
  if (!cell) return `<td class="q empty-cell" title="нет ответа">—</td>`;
  const conf = Math.round(cell.confidence * 100);
  return `<td class="q" data-answer="${cell.answer_id}" style="cursor:pointer">
    <div class="cell-answer">
      <span class="v ${cell.low_confidence && !cell.reviewed ? 'lowconf' : ''}">${esc(cell.display || cell.value || '—')}
        ${cell.method === 'dtmf' ? '<span class="small muted">⌨</span>' : ''}
        ${cell.method === 'manual' ? '<span class="small muted">✎</span>' : ''}</span>
      ${cell.raw_text && cell.raw_text !== cell.display ? `<span class="raw" title="${esc(cell.raw_text)}">«${esc(cell.raw_text)}»</span>` : ''}
      <span class="meta">
        ${cell.low_confidence && !cell.reviewed ? '<span class="badge amber" style="height:18px;font-size:10.5px">проверить</span>'
          : `<span class="small muted" style="font-size:10.5px">${conf}%</span>`}
        ${cell.reviewed ? '<span class="badge green" style="height:18px;font-size:10.5px">✓</span>' : ''}
        ${cell.audio_url ? `<button class="audio-btn" data-audio-answer="${cell.answer_id}">▶</button>` : ''}
      </span>
    </div></td>`;
}

function toggleColumns() {
  const hide = document.body.classList.toggle('hide-q');
  document.querySelectorAll('#results-table th.q, #results-table td.q').forEach(el => {
    el.style.display = hide ? 'none' : '';
  });
  toast(hide ? 'Колонки вопросов скрыты' : 'Колонки вопросов показаны');
}

function openAnswerModal(answerId, data) {
  const cell = data.rows.flatMap(r => r.cells).find(c => c && c.answer_id === answerId);
  if (!cell) return;
  const question = (() => {
    for (const r of data.rows) {
      const idx = r.cells.findIndex(c => c && c.answer_id === answerId);
      if (idx >= 0) return data.questions[idx];
    }
    return null;
  })();
  modal({
    title: 'Правка ответа',
    body: `
      <div class="small muted">Вопрос</div>
      <p style="font-weight:600;margin:4px 0 14px">${esc(question?.text || '')}</p>
      <div class="grid g2">
        <div><div class="small muted">Распознанный текст</div>
          <div class="code" style="white-space:pre-wrap">${esc(cell.raw_text || '—')}</div>
          <div class="small muted mt8">метод: ${{ asr: 'распознавание речи', dtmf: 'клавиши', manual: 'ручная правка' }[cell.method] || cell.method},
            уверенность ${(cell.confidence * 100).toFixed(0)}%</div>
          ${cell.audio_url ? `<audio class="mt12" style="width:100%" controls src="${cell.audio_url}"></audio>` : ''}</div>
        <div>
          <label class="field">Значение для таблицы
            <input class="input" id="ans-value" value="${esc(cell.value || '')}"></label>
          <label class="field mt12">Отображение
            <input class="input" id="ans-display" value="${esc(cell.display || '')}"></label>
          ${question?.options?.length ? `<div class="small muted mt12">Варианты: ${question.options.map(esc).join(' · ')}</div>` : ''}
          ${question?.type === 'yes_no' ? `<div class="row mt12"><button class="btn btn-sm" data-quick="yes">да</button>
             <button class="btn btn-sm" data-quick="no">нет</button>
             <button class="btn btn-sm btn-danger" data-quick="refused">отказ от звонков</button></div>` : ''}
        </div>
      </div>`,
    footer: `<button class="btn" data-close>Закрыть</button>
             <button class="btn btn-primary" id="ans-save">Сохранить и отметить проверенным</button>`,
    onMount(host, close) {
      $$('[data-quick]', host).forEach(b => b.onclick = () => {
        $('#ans-value', host).value = b.dataset.quick;
        $('#ans-display', host).value = { yes: 'да', no: 'нет', refused: 'отказ от звонков' }[b.dataset.quick];
      });
      $('#ans-save', host).onclick = async () => {
        try {
          await api(`/answers/${answerId}`, { method: 'PATCH', body: {
            value: $('#ans-value', host).value,
            value_display: $('#ans-display', host).value,
            reviewed: true,
          } });
          close(); toast('Ответ обновлён', 'success'); navigate();
        } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
}

async function openCallModal(callId) {
  try {
    const call = await api(`/calls/${callId}`);
    modal({
      title: `Звонок #${call.id} — ${call.name || call.phone}`,
      wide: true,
      body: `
        <div class="row wrap small muted" style="gap:16px">
          <span>${fmtDate(call.started_at)}</span>
          <span>${badge(call.outcome)}</span>
          <span>${fmtDur(call.duration_sec)}</span>
          <span class="mono">${esc(call.phone)}</span>
          <span>движок: ${esc(call.engine || '—')}</span>
        </div>
        ${call.recording_url ? `<audio class="mt12" style="width:100%" controls src="${call.recording_url}"></audio>` : ''}
        <div class="section-title" style="margin-top:18px"><h3>Расшифровка</h3></div>
        <div class="code" style="white-space:pre-wrap;max-height:260px">${esc(call.transcript || '—')}</div>
        <div class="section-title"><h3>Ответы</h3></div>
        <table class="tbl">
          <thead><tr><th>№</th><th>Вопрос</th><th>Ответ</th><th>Текст</th><th class="num">Уверенность</th></tr></thead>
          <tbody>${call.answers.map(a => `<tr>
            <td class="mono">${a.position + 1}</td><td>${esc(a.question)}</td>
            <td><b>${esc(a.value_display || a.value || '—')}</b></td>
            <td class="small muted">${esc(a.raw_text || '—')}</td>
            <td class="num">${a.method === 'dtmf' ? 'клавиши' : (a.confidence * 100).toFixed(0) + '%'}</td></tr>`).join('') ||
            '<tr><td colspan="5"><div class="empty">Ответов нет</div></td></tr>'}</tbody>
        </table>`,
      footer: `<button class="btn" data-close>Закрыть</button>`,
    });
  } catch (e) { toast(e.message, 'error'); }
}

/* ================================================================== НЕ ЗВОНИТЬ */
async function renderDnc() {
  setPage('Не звонить', 'Стоп-лист');
  setActions('');
  const rows = await api('/do-not-call');
  view().innerHTML = `
    <div class="grid" style="grid-template-columns: 1fr 380px">
      <div class="card">
        <div class="card-head"><h2>Стоп-лист</h2><div class="spacer"></div>
          <span class="muted small">${rows.length} ${plural(rows.length, 'номер', 'номера', 'номеров')}</span></div>
        <div class="table-scroll" style="max-height:60vh">
          <table class="tbl"><thead><tr><th>Телефон</th><th>Причина</th><th>Добавлен</th><th></th></tr></thead>
          <tbody>${rows.map(r => `<tr>
            <td class="mono">${esc(r.phone)}</td><td class="small muted">${esc(r.reason || '—')}</td>
            <td class="small muted">${fmtDate(r.created_at)}</td>
            <td class="right"><button class="btn btn-ghost btn-sm" data-del-dnc="${esc(r.phone)}">✕</button></td>
          </tr>`).join('') || '<tr><td colspan="4"><div class="empty">Список пуст</div></td></tr>'}</tbody></table>
        </div>
      </div>
      <div class="grid" style="align-content:start">
        <div class="card"><div class="card-head"><h2>Добавить номер</h2></div><div class="card-body fieldset">
          <label class="field">Телефон<input class="input" id="dnc-phone" placeholder="+7 999 123-45-67"></label>
          <label class="field">Причина<input class="input" id="dnc-reason" placeholder="Просил не звонить"></label>
          <button class="btn btn-primary" id="dnc-add">Добавить в стоп-лист</button>
        </div></div>
        <div class="card"><div class="card-body small muted">
          Абоненты, попросившие не звонить, автоматически попадают в этот список во время обзвона.
          Если включена настройка «Уважать стоп-лист», такие номера пропускаются при наборе.
        </div></div>
      </div>
    </div>`;
  $('#dnc-add').onclick = async () => {
    try {
      await api('/do-not-call', { method: 'POST', body: {
        phone: $('#dnc-phone').value, reason: $('#dnc-reason').value } });
      toast('Номер добавлен', 'success'); navigate();
    } catch (e) { toast(e.message, 'error'); }
  };
  $$('[data-del-dnc]').forEach(b => b.onclick = async () => {
    await api(`/do-not-call/${encodeURIComponent(b.dataset.delDnc)}`, { method: 'DELETE' });
    navigate();
  });
}

/* ================================================================== ОБОРУДОВАНИЕ */
async function renderEquipment() {
  setPage('Оборудование', 'GSM-модем и аудио');
  setActions(`<button class="btn" id="eq-refresh">↻ Опросить модем</button>`);
  const data = await api('/equipment?probe=true');
  state.equipment = data;
  const m = data.modem;
  view().innerHTML = `
    <div class="grid" style="grid-template-columns: 1fr 1fr">
      <div class="card">
        <div class="card-head"><h2>Модем</h2><div class="spacer"></div>
          ${m.demo ? '<span class="badge green">демо-эмулятор</span>'
            : m.connected ? '<span class="badge green"><span class="dot"></span>подключён</span>'
            : '<span class="badge red">нет связи</span>'}</div>
        <div class="card-body">
          <dl class="kv">
            <dt>Порт</dt><dd class="mono">${esc(m.port || 'не выбран')}</dd>
            <dt>Модель</dt><dd>${esc(m.model || '—')}</dd>
            <dt>IMEI</dt><dd class="mono">${esc(m.imei || '—')}</dd>
            <dt>SIM</dt><dd>${m.sim_ready ? 'готова' : 'не готова'} ${m.sim_number ? '· ' + esc(m.sim_number) : ''}</dd>
            <dt>Оператор</dt><dd>${esc(m.operator || '—')}</dd>
            <dt>Уровень сигнала</dt><dd>${m.signal ?? 0} / 31 <span class="progress" style="display:inline-block;width:90px;vertical-align:middle;margin-left:6px"><i style="width:${Math.round((m.signal || 0) / 31 * 100)}%"></i></span></dd>
            <dt>Баланс</dt><dd>${esc(m.balance || '—')}</dd>
          </dl>
          ${m.error ? `<div class="small mt12" style="color:var(--red)">${esc(m.error)}</div>` : ''}
          <div class="row wrap mt16">
            <button class="btn btn-sm" id="balance-btn">Проверить баланс (*100#)</button>
            <button class="btn btn-sm" id="test-call-btn">Пробный звонок</button>
            <a class="btn btn-sm" href="#/settings">Настройки модема</a>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Аудио</h2><div class="spacer"></div>
          <span class="small muted">запись и воспроизведение</span></div>
        <div class="card-body">
          <dl class="kv">
            <dt>arecord</dt><dd>${data.audio.arecord ? '<span class="badge green">есть</span>' : '<span class="badge red">нет</span>'}</dd>
            <dt>aplay</dt><dd>${data.audio.aplay ? '<span class="badge green">есть</span>' : '<span class="badge red">нет</span>'}</dd>
            <dt>ffmpeg</dt><dd>${data.audio.ffmpeg ? '<span class="badge green">есть</span>' : '<span class="badge amber">нет</span>'}</dd>
            <dt>Устройство записи</dt><dd class="mono">${esc(data.audio.record_device || 'по умолчанию')}</dd>
            <dt>Устройство воспроизведения</dt><dd class="mono">${esc(data.audio.play_device || 'по умолчанию')}</dd>
          </dl>
          ${data.audio_devices.length ? `<div class="small muted mt12">Найденные ALSA-устройства:</div>
            <div class="small mono mt8" style="line-height:1.7">${data.audio_devices.slice(0, 8).map(d => esc(d.hw) + ' — ' + esc(d.label)).join('<br>')}</div>` : ''}
          <div class="small muted mt12">Для голосового обзвона модем соединяется с USB-звуковой картой
            (или используется гарнитура). Укажите её в настройках как устройство записи/воспроизведения.</div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Распознавание речи</h2><div class="spacer"></div>
          <span class="badge ${data.asr.active === 'stub' ? 'amber' : 'green'}">${esc(asrLabel(data.asr))}</span></div>
        <div class="card-body">
          <dl class="kv">
            <dt>Vosk (офлайн)</dt><dd>${data.asr.engines.vosk.available ? '<span class="badge green">готов</span>' : '<span class="badge amber">не найден</span>'}
              <div class="small muted mono">${esc(data.asr.engines.vosk.model || '')}</div></dd>
            <dt>whisper.cpp</dt><dd>${data.asr.engines.whispercpp.available ? '<span class="badge green">готов</span>' : '<span class="badge amber">не найден</span>'}
              <div class="small muted mono">${esc(data.asr.engines.whispercpp.model || '')}</div></dd>
            <dt>HTTP-сервис</dt><dd>${data.asr.engines.http.available ? '<span class="badge green">настроен</span>' : '<span class="badge">не задан</span>'}
              <div class="small muted mono">${esc(data.asr.engines.http.url || '')}</div></dd>
          </dl>
          <label class="field mt12">Проверить на файле
            <input class="input" type="file" id="asr-file" accept="audio/*"></label>
          <div id="asr-result" class="small mt8"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Озвучивание вопросов</h2>
          <div class="spacer"></div><span class="badge ${data.tts.active === 'none' ? 'amber' : 'green'}">
          ${esc({ piper: 'Piper', espeak: 'espeak-ng', none: 'не настроено' }[data.tts.active] || data.tts.active)}</span></div>
        <div class="card-body">
          <label class="field">Текст для проверки
            <textarea class="textarea" id="tts-text">Здравствуйте! Это короткий опрос, займёт около минуты. Вам интересно наше предложение?</textarea></label>
          <button class="btn btn-sm mt8" id="tts-play">🔊 Прослушать</button>
          <div class="small muted mt12">Вопросы озвучиваются синтезом речи при звонке или заранее
            записанным WAV-файлом, прикреплённым к вопросу. Файлы конвертируются в 8 кГц mono для телефонии.</div>
        </div>
      </div>
      ${data.ports.length ? `<div class="card" style="grid-column: 1 / -1">
        <div class="card-head"><h2>Найденные последовательные порты</h2></div>
        <div class="card-body table-scroll"><table class="tbl">
          <thead><tr><th>Устройство</th><th>Описание</th><th>HWID</th><th></th></tr></thead>
          <tbody>${data.ports.map(p => `<tr>
            <td class="mono">${esc(p.device)}</td><td class="small">${esc(p.description)}</td>
            <td class="small muted mono">${esc(p.hwid)}</td>
            <td>${p.likely_modem ? '<span class="badge green">похож на модем</span>' : ''}</td></tr>`).join('')}</tbody>
        </table></div></div>` : ''}
    </div>`;

  $('#eq-refresh').onclick = () => renderEquipment();
  $('#balance-btn') && ($('#balance-btn').onclick = async () => {
    try { const r = await api('/equipment/modem/balance', { method: 'POST' });
      if (r.ok) { toast('Баланс: ' + r.balance, 'success'); renderEquipment(); }
      else toast(r.error || 'Не удалось получить баланс', 'error');
    } catch (e) { toast(e.message, 'error'); }
  });
  $('#test-call-btn') && ($('#test-call-btn').onclick = openTestCallModal);
  $('#asr-file').onchange = async e => {
    const file = e.target.files[0]; if (!file) return;
    const fd = new FormData(); fd.append('file', file);
    $('#asr-result').textContent = 'Распознавание…';
    try {
      const r = await api('/asr/transcribe', { method: 'POST', body: fd });
      $('#asr-result').innerHTML = `<b>${esc(r.text || '(пусто)')}</b><div class="muted">движок: ${esc(r.engine)},
        уверенность ${(r.confidence * 100).toFixed(0)}%${r.error ? ' · ' + esc(r.error) : ''}</div>`;
    } catch (err) { $('#asr-result').innerHTML = `<span style="color:var(--red)">${esc(err.message)}</span>`; }
  };
  $('#tts-play').onclick = async () => {
    try {
      const resp = await fetch('/api/tts/preview', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: $('#tts-text').value }) });
      if (!resp.ok) throw new Error((await resp.json()).detail || 'Синтез недоступен');
      new Audio(URL.createObjectURL(await resp.blob())).play();
    } catch (e) { toast(e.message, 'error'); }
  };
}

function openTestCallModal() {
  modal({
    title: 'Пробный звонок',
    body: `<div class="fieldset">
        <label class="field">Номер<input class="input" id="tc-phone" placeholder="+7 999 123-45-67"></label>
        <label class="field">Произнести после ответа (необязательно)
          <input class="input" id="tc-speak" placeholder="Проверка связи"></label>
        <div class="small muted">Звонок выполняется с SIM-карты МегаФона. Убедитесь, что модем и аудио настроены,
          а проверочный номер — ваш собственный.</div>
      </div>`,
    footer: `<button class="btn" data-close>Отмена</button><button class="btn btn-primary" id="tc-go">Позвонить</button>`,
    onMount(host, close) {
      $('#tc-go', host).onclick = async () => {
        $('#tc-go', host).disabled = true; $('#tc-go', host).textContent = 'Дозвон…';
        try {
          const r = await api('/equipment/modem/test-call', { method: 'POST', body: {
            phone: $('#tc-phone', host).value, speak: $('#tc-speak', host).value, timeout: 45 } });
          toast(r.outcome === 'answered' ? 'Абонент ответил' : 'Исход: ' + r.outcome, r.ok ? 'success' : 'error');
          close();
        } catch (e) { toast(e.message, 'error'); $('#tc-go', host).disabled = false; $('#tc-go', host).textContent = 'Позвонить'; }
      };
    },
  });
}

/* ================================================================== НАСТРОЙКИ */
async function renderSettings() {
  setPage('Настройки', 'Параметры сервиса');
  setActions(`<button class="btn btn-primary" id="save-settings">Сохранить</button>`);
  const data = await api('/settings');
  const v = data.values;
  const field = (key, label, hint = '', type = 'text', options = null) => {
    const value = v[key] ?? '';
    if (options) {
      return `<label class="field">${esc(label)}
        <select class="select" data-set="${key}">${options.map(([val, text]) =>
          `<option value="${esc(val)}" ${String(value) === String(val) ? 'selected' : ''}>${esc(text)}</option>`).join('')}</select>
        ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</label>`;
    }
    return `<label class="field">${esc(label)}
      <input class="input" data-set="${key}" type="${type}" value="${esc(value)}">
      ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</label>`;
  };
  view().innerHTML = `
    <div class="grid" style="grid-template-columns: 1fr 1fr">
      <div class="card"><div class="card-head"><h2>Общие</h2></div><div class="card-body fieldset">
        ${field('default_mode', 'Режим по умолчанию', 'demo — без оборудования, modem — реальные звонки', 'text',
          [['demo', 'Демо-эмулятор'], ['modem', 'GSM-модем']])}
        ${field('calling_hours', 'Время звонков', 'Например 09:00-20:00. Вне интервала обзвон ждёт (152-ФЗ и правила вежливости)')}
        ${field('max_concurrent_calls', 'Одновременных звонков', 'Для одного модема — 1')}
        ${field('default_retry_limit', 'Повторов при недозвоне')}
        ${field('min_confidence', 'Порог уверенности ASR', 'Ниже порога ответ подсвечивается для ручной проверки')}
        <label class="check"><input type="checkbox" data-set="record_answers" ${v.record_answers === '1' ? 'checked' : ''}> Записывать аудио ответов</label>
        <label class="check"><input type="checkbox" data-set="respect_dnc" ${v.respect_dnc === '1' ? 'checked' : ''}> Уважать стоп-лист «Не звонить»</label>
      </div></div>

      <div class="card"><div class="card-head"><h2>Модем и SIM</h2></div><div class="card-body fieldset">
        ${field('modem_port', 'Порт модема', 'Linux: /dev/ttyUSB0 · Windows: COM3')}
        ${field('modem_baud', 'Скорость', 'Обычно 115200')}
        ${field('operator', 'Оператор')}
        ${field('ussd_balance', 'USSD проверки баланса', 'МегаФон: *100#')}
        ${field('sim_number', 'Номер SIM', 'для справки (можно не заполнять)')}
      </div></div>

      <div class="card"><div class="card-head"><h2>Аудио</h2></div><div class="card-body fieldset">
        ${field('audio_backend', 'Бэкенд', 'alsa — arecord/aplay', 'text', [['auto', 'Авто'], ['alsa', 'ALSA']])}
        ${field('audio_play_device', 'Устройство воспроизведения', 'например plughw:1,0')}
        ${field('audio_record_device', 'Устройство записи', 'например plughw:1,0')}
        ${field('audio_rate', 'Частота, Гц', '8000 для телефонии')}
      </div></div>

      <div class="card"><div class="card-head"><h2>Распознавание речи</h2></div><div class="card-body fieldset">
        ${field('asr_engine', 'Движок', 'auto — первый доступный', 'text',
          [['auto', 'Авто'], ['vosk', 'Vosk (офлайн)'], ['whispercpp', 'whisper.cpp (офлайн)'], ['http', 'HTTP-сервис'], ['stub', 'Отключено']])}
        ${field('vosk_model_path', 'Путь к модели Vosk', 'например /opt/vosk-model-ru')}
        ${field('whisper_bin', 'Бинарник whisper.cpp', 'whisper-cli или main')}
        ${field('whisper_model_path', 'Модель whisper.cpp', 'ggml-base.bin / ggml-small.bin')}
        ${field('http_asr_url', 'URL HTTP-ASR', 'POST {url}/transcribe, поле file')}
        ${field('http_asr_token', 'Токен HTTP-ASR', data.secrets.http_asr_token ? 'токен задан (скрыт)' : '')}
      </div></div>

      <div class="card"><div class="card-head"><h2>Озвучивание</h2></div><div class="card-body fieldset">
        ${field('tts_engine', 'Движок синтеза', 'auto — Piper, затем espeak-ng', 'text',
          [['auto', 'Авто'], ['piper', 'Piper'], ['espeak', 'espeak-ng'], ['none', 'Выключено']])}
        ${field('piper_bin', 'Бинарник Piper')}
        ${field('piper_model', 'Модель Piper (.onnx)')}
        ${field('espeak_voice', 'Голос espeak', 'ru')}
      </div></div>

      <div class="card"><div class="card-head"><h2>Служебное</h2></div><div class="card-body fieldset">
        ${field('public_url', 'Публичный адрес', 'для ссылок в уведомлениях (необязательно)')}
        <div class="small muted">Данные хранятся в SQLite (data/callbot.db), записи — в data/recordings.
          Резервная копия: остановите сервис и скопируйте папку data.</div>
        <div class="row"><button class="btn btn-sm" id="reset-settings">Сбросить все настройки</button></div>
      </div></div>
    </div>`;

  $('#save-settings').onclick = async () => {
    const values = {};
    $$('[data-set]').forEach(el => {
      const key = el.dataset.set;
      if (el.type === 'checkbox') values[key] = el.checked ? '1' : '0';
      else if (el.value !== '' && el.value !== '***') values[key] = el.value;
    });
    try {
      await api('/settings', { method: 'PUT', body: { values } });
      toast('Настройки сохранены', 'success'); await loadMeta();
    } catch (e) { toast(e.message, 'error'); }
  };
  $('#reset-settings').onclick = () => confirmDialog('Сбросить все настройки к значениям по умолчанию?',
    async () => {
      for (const key of Object.keys(v)) {
        await api('/settings/reset', { method: 'POST', body: { key } });
      }
      toast('Настройки сброшены', 'success'); navigate();
    }, 'Сбросить');
}
