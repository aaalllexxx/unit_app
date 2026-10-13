/* ИИ-агент лаборатории — чат (сервер: agent.py, инструменты — agent_tools.py).

   Лента приходит с сервера записями: сообщения человека, реплики агента, шаги-инструменты
   (со статусом и карточкой результата), план, помощники, цена ответа. Пока агент работает,
   лента опрашивается раз в ~0.8 с; перерисовываются только изменившиеся записи.

   Один компонент на три места: страница /ai (со списком чатов), панель «Спросить агента»
   поверх любой страницы и окно «ИИ-агент» в мастерской (LabAgent.mount). */
(() => {
  'use strict';
  const U = window.UNIT || { toast() {}, haptic() {}, openSheet() {} };

  const esc = (t) => String(t == null ? '' : t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const ic = (name, size = 16) => `<svg class="ic" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const h = (html) => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  const safeUrl = (u) => (/^(https?:\/\/|\/)/.test(String(u || '')) ? String(u) : '#');
  const tokens = (n) => (!n ? '' : n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1).replace('.0', '')} тыс. токенов` : `${n} токенов`);
  const api = async (url, body, method) => {
    const opts = { method: method || (body === undefined ? 'GET' : 'POST'), credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok && !data.error) data.error = 'Сервер ответил ошибкой ' + res.status;
    return data;
  };

  /* ── markdown: заголовки, списки, код, таблицы, жирный/курсив, ссылки ── */
  const inline = (t) => esc(t)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^\w*])\*([^*\n]+)\*(?!\w)/g, '$1<i>$2</i>')
    .replace(/\[([^\]\n]+)\]\(((?:https?:\/\/|\/)[^\s)]+)\)/g, (m, a, u) =>
      `<a href="${u}"${u.startsWith('/') ? '' : ' target="_blank" rel="noopener"'}>${a}</a>`);
  const md = (text) => {
    const lines = String(text || '').replace(/\r/g, '').split('\n');
    const out = [];
    for (let i = 0; i < lines.length;) {
      const line = lines[i];
      if (/^```/.test(line.trim())) {
        const body = [];
        i += 1;
        while (i < lines.length && !/^```/.test(lines[i].trim())) { body.push(lines[i]); i += 1; }
        i += 1;
        out.push(`<pre class="ac-code"><code>${esc(body.join('\n'))}</code></pre>`);
        continue;
      }
      const head = line.match(/^(#{1,6})\s+(.*)$/);
      if (head) { const n = Math.min(6, head[1].length + 2); out.push(`<h${n}>${inline(head[2])}</h${n}>`); i += 1; continue; }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
        const headRow = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(cells(lines[i])); i += 1; }
        out.push('<div class="ac-scroll"><table class="ac-table"><thead><tr>' + headRow.map((c) => `<th>${inline(c)}</th>`).join('')
          + '</tr></thead><tbody>' + rows.map((r) => '<tr>' + r.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>').join('')
          + '</tbody></table></div>');
        continue;
      }
      if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]/.test(line);
        const items = [];
        while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, ''));
          i += 1;
        }
        const tag = ordered ? 'ol' : 'ul';
        out.push(`<${tag}>${items.map((x) => `<li>${inline(x)}</li>`).join('')}</${tag}>`);
        continue;
      }
      if (!line.trim()) { i += 1; continue; }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(```|#{1,6}\s|\s*([-*•]|\d+[.)])\s|\s*\|)/.test(lines[i])) {
        para.push(lines[i]); i += 1;
      }
      if (!para.length) { para.push(lines[i]); i += 1; }
      out.push(`<p>${inline(para.join(' '))}</p>`);
    }
    return out.join('');
  };

  /* ── карточки результатов ── */
  const bar = (value, tone) => `<i class="ac-bar-line"><b style="width:${Math.max(0, Math.min(100, value || 0))}%;${tone ? `background:${tone}` : ''}"></b></i>`;
  const toneFor = (v) => (v >= 75 ? 'var(--ok)' : v >= 40 ? 'var(--accent)' : 'var(--warn)');
  const W = {
    table: (w) => `<div class="ac-scroll"><table class="ac-table"><thead><tr>${(w.columns || []).map((c) => `<th>${esc(c[1])}</th>`).join('')}</tr></thead>
      <tbody>${(w.rows || []).map((r) => `<tr>${(w.columns || []).map((c, i) => {
        const v = r[c[0]] == null ? '—' : r[c[0]];
        if (i === 0 && r.href) return `<td><a href="${esc(safeUrl(r.href))}">${esc(v)}</a></td>`;
        if (c[0] === 'progress') return `<td class="ac-td-bar">${bar(v, toneFor(v))}<b>${esc(v)}%</b></td>`;
        return `<td>${esc(v)}</td>`;
      }).join('')}</tr>`).join('')}</tbody></table></div>`,
    project: (w) => `<div class="ac-proj"><div class="ac-tree-head"><b>${esc(w.title)}</b>
        <a href="${esc(safeUrl(w.url))}">${ic('arrow', 13)}Открыть проект</a></div>
      <div class="ac-stats"><span>Готово <b>${w.percent}%</b></span><span>Статус <b>${esc(w.status)}</b></span>
        <span>Срок <b>${esc(w.due || '—')}</b></span><span>Открыто задач <b>${w.open}</b></span>
        <span>Просрочено <b style="${w.late ? 'color:var(--danger)' : ''}">${w.late}</b></span><span>Команда <b>${w.team}</b></span></div>
      ${(w.parts || []).map((p) => `<div class="ac-chart-row"><span title="${esc(p.title)}">${esc(p.title)} <small>${esc(p.phase || '')}</small></span>${bar(p.percent, toneFor(p.percent))}<b>${p.percent}%</b></div>`).join('')}</div>`,
  };
  const widget = (w) => {
    if (!w || !W[w.type]) return '';
    try { return `<div class="ac-widget w-${w.type}">${W[w.type](w)}</div>`; } catch (err) { return ''; }
  };

  const TOOL_IC = {
    list_projects: 'folder', get_project: 'folder', list_tasks: 'list', get_task: 'checklist', list_people: 'users',
    get_person: 'user', list_teams: 'layers', signals: 'activity', time_report: 'clock', search: 'search', my_rights: 'key',
    remember: 'star', run_team: 'users', workshop: 'desk', read_item: 'note', access_overview: 'key',
    create_task: 'plus', update_task: 'pen', delete_task: 'trash', comment_task: 'chat', checklist_task: 'checklist',
    create_part: 'layers', update_project: 'pen', journal_add: 'note', desk_add: 'plus', desk_update: 'pen',
    desk_delete: 'trash', arrange_workshop: 'grid', set_person_tags: 'key', set_person_access: 'key',
  };
  const STATUS = { queued: 'в очереди', running: 'работает', awaiting: 'ждёт вас', done: 'готово', error: 'ошибка',
    denied: 'отклонено', stopped: 'остановлено' };
  const live = (st) => st === 'running' || st === 'queued';
  const argsLine = (item) => {
    const a = item.args || {};
    const pick = a.title || a.query || a.text || a.goal || a.project_id || a.task_id || a.member_id || a.item_id || '';
    return pick ? String(pick).slice(0, 140) : '';
  };
  const confirmHtml = (item) => {
    const conf = item.confirm || {};
    const rows = conf.rows || [];
    return `<div class="ac-confirm">
      <p class="ac-confirm-what">${ic('shield', 15)}<span>${esc(conf.text || ('Агент хочет: ' + (item.label || item.name)))}</span></p>
      ${rows.length ? `<dl class="ac-confirm-rows">${rows.map((r) => `<dt>${esc(r[0])}</dt><dd>${esc(r[1])}</dd>`).join('')}</dl>` : ''}
      <div class="ac-confirm-btns"><button type="button" class="ac-ok" data-decide="${esc(item.id)}" data-ok="1">${ic('check', 14)}Разрешить</button>
        <button type="button" class="ac-no" data-decide="${esc(item.id)}" data-ok="0">Отклонить</button></div>
      <details class="ac-tech"><summary>Технические детали</summary><pre>${esc(item.preview || '')}</pre></details></div>`;
  };
  const stepIc = (st, name) => (st === 'queued' ? ic('clock', 14) : st === 'running' ? '<i class="ac-spin"></i>'
    : st === 'awaiting' ? ic('shield', 14) : st === 'error' ? ic('x', 14) : ic(TOOL_IC[name] || 'spark', 14));
  const SHOW = new Set(['project']);
  const toolHtml = (item) => {
    const awaiting = item.status === 'awaiting';
    const sum = awaiting ? 'ждёт вашего решения' : (item.summary || (item.confirm && item.confirm.text) || argsLine(item));
    const link = item.link && item.status === 'done' ? `<a class="ac-step-link" href="${esc(safeUrl(item.link))}">Открыть</a>` : '';
    const body = (awaiting ? confirmHtml(item) : '') + widget(item.widget);
    const head = `<span class="ac-step-ic">${stepIc(item.status, item.name)}</span>
      <span class="ac-step-text"><span class="ac-step-name">${esc(item.label || item.name)}</span>
      ${sum ? `<span class="ac-step-sum">${esc(sum)}</span>` : ''}</span>${link}`;
    const cls = `ac-step st-${esc(item.status)}${item.kind === 'write' ? ' is-write' : ''}`;
    if (!body) return `<div class="${cls}" data-step="${esc(item.id)}"><div class="ac-step-row">${head}</div></div>`;
    const open = awaiting || (item.status === 'done' && item.widget && SHOW.has(item.widget.type));
    return `<details class="${cls}" data-step="${esc(item.id)}" ${open ? 'open' : ''}><summary class="ac-step-row">${head}
      <span class="ac-step-chev">${ic('down', 12)}</span></summary><div class="ac-step-body">${body}</div></details>`;
  };
  const RENDER = {
    user: (it) => `<div class="ac-msg is-user ${it.queued ? 'is-queued' : ''}">${it.queued ? `<span class="ac-queued">${ic('clock', 12)}В очереди — агент прочтёт перед следующим шагом</span>` : ''}<div class="ac-bubble">${esc(it.text).replace(/\n/g, '<br>')}
      ${(it.files || []).length ? `<div class="ac-att">${it.files.map((f) => `<span class="ac-file">${ic('file', 13)}${esc(f.name)}</span>`).join('')}</div>` : ''}</div></div>`,
    say: (it) => `<div class="ac-msg is-agent${it.interim ? ' is-interim' : ''}"><div class="ac-md">${md(it.text)}</div></div>`,
    todo: (it) => {
      const items = it.items || [];
      const done = items.filter((t) => t.done).length;
      return `<details class="ac-plan" ${done < items.length ? 'open' : ''}><summary>
          <span class="ac-step-ic">${ic('list', 14)}</span><span class="ac-plan-title">План</span>
          <span class="ac-plan-n">${done} из ${items.length}</span>
          <i class="ac-plan-bar"><b style="width:${items.length ? Math.round(done / items.length * 100) : 0}%"></b></i>
          <span class="ac-step-chev">${ic('down', 12)}</span></summary>
        <ul>${items.map((t) => `<li class="${t.done ? 'is-done' : ''}"><i>${t.done ? ic('check', 11) : ''}</i>${esc(t.text)}</li>`).join('')}</ul></details>`;
    },
    tool: (it) => toolHtml(it),
    agents: (it) => {
      const agents = it.agents || [];
      const done = agents.filter((a) => !live(a.status)).length;
      return `<div class="ac-agents"><div class="ac-agents-head">${ic('users', 14)}<span>Команда</span>
          <em>${done < agents.length ? `работают параллельно · готово ${done} из ${agents.length}` : `все ${agents.length} закончили — агент проверяет`}</em></div>
        ${agents.map((a) => {
          const steps = a.steps || [];
          const note = (STATUS[a.status] || a.status) + ` · ${a.model}` + (steps.length ? ` · шагов: ${steps.length}` : '');
          return `<details class="ac-agent st-${esc(a.status)}" data-agent="${esc(a.key)}"><summary class="ac-step-row">
              <span class="ac-step-ic">${a.status === 'queued' ? ic('clock', 14) : live(a.status) ? '<i class="ac-spin"></i>' : a.status === 'error' ? ic('x', 14) : ic('check', 14)}</span>
              <span class="ac-step-text"><span class="ac-step-name"><b class="ac-agent-key">${esc(a.key)}</b>${esc(a.title)}</span>
              <span class="ac-step-sum">${esc(note)}</span></span>
              <span class="ac-step-chev">${ic('down', 12)}</span></summary>
            <div class="ac-step-body">${steps.map((s) => toolHtml(s)).join('')}
              ${a.result && !live(a.status) ? `<div class="ac-agent-res"><div class="ac-md">${md(a.result)}</div></div>` : ''}</div></details>`;
        }).join('')}</div>`;
    },
    usage: (it) => `<div class="ac-usage" title="Модель: ${esc(it.model || '')}. Обращений к моделям: ${it.calls}. Токены считает сервис нейросети.">
      ≈ ${esc(it.label)}${it.tokens ? ' · ' + tokens(it.tokens) : ''}</div>`,
    error: (it) => `<div class="ac-note is-err">${ic('x', 14)}${esc(it.text)}</div>`,
    notice: (it) => `<div class="ac-note">${esc(it.text)}</div>`,
  };

  /* ═══ Один чат ═══════════════════════════════════════════════════════════ */
  function mount(app, opts = {}) {
    const $ = (sel) => app.querySelector(sel);
    const compact = app.classList.contains('ac-compact');
    const feed = $('[data-feed]');
    const hello = $('[data-hello]');
    const form = $('[data-composer]');
    const input = $('[data-text]');
    const fileInput = $('[data-file-input]');
    const filesBox = $('[data-files]');
    const sendBtn = $('[data-send]');
    const stopBtn = $('[data-stop]');
    const statusEl = $('[data-status]');
    const spentEl = $('[data-spent]');
    const titleEl = $('[data-title]');
    const modelBox = $('[data-model-box]');
    const modelBtn = $('[data-model-open]');
    const modelPop = $('[data-model-pop]');
    const modeBtn = $('[data-mode-toggle]');
    const pending = $('[data-pending]');
    const menu = $('[data-menu]');
    const menuPop = $('[data-menu-pop]');
    const chips = $('[data-chips]');
    const list = $('[data-list]');
    const full = $('[data-full]');
    const scroller = compact ? app : null;            // в панели лента прокручивается внутри, на странице — вся страница
    const boot = opts.boot || {};

    let chatId = opts.chat || app.dataset.chat || '';
    let context = opts.context || app.dataset.context || '';
    let version = 0;
    let running = false;
    let pollT = null;
    let files = [];
    let mode = boot.mode === 'auto' ? 'auto' : 'manual';
    let model = '';
    let seenUsage = null;                               // цены ответов, которые уже были, когда открыли чат
    const nodes = new Map();
    const userOpen = new Map();

    const scrollTop = () => (scroller ? scroller.scrollTop : window.scrollY);
    const scrollHeight = () => (scroller ? scroller.scrollHeight : document.documentElement.scrollHeight);
    const viewH = () => (scroller ? scroller.clientHeight : window.innerHeight);
    const nearBottom = () => viewH() + scrollTop() >= scrollHeight() - 160;
    const toBottom = (smooth) => (scroller || window).scrollTo({ top: scrollHeight(), behavior: smooth ? 'smooth' : 'auto' });

    const setEmpty = (empty) => {
      hello.hidden = !empty;
      if (chips) chips.hidden = !empty;
      app.classList.toggle('is-empty', empty);
    };
    feed.addEventListener('click', (e) => {
      const sum = e.target.closest('summary');
      const item = sum && sum.closest('.ac-item');
      if (!item || !item.dataset.id) return;
      const d = sum.parentElement;
      const idx = [...item.querySelectorAll('details')].indexOf(d);
      setTimeout(() => userOpen.set(`${item.dataset.id}:${idx}`, d.open), 0);
    });
    const itemEl = (it) => {
      const make = RENDER[it.type];
      if (!make) return null;
      const el = h(`<div class="ac-item is-${esc(it.type)}" data-id="${esc(it.id)}">${make(it)}</div>`);
      el.querySelectorAll('details').forEach((d, i) => {
        const key = `${it.id}:${i}`;
        if (userOpen.has(key) && !d.matches('.st-awaiting') && !d.querySelector('.st-awaiting')) d.open = userOpen.get(key);
      });
      return el;
    };

    /* что агент делает прямо сейчас */
    const liveEl = h('<div class="ac-live" data-live hidden></div>');
    feed.appendChild(liveEl);
    let lanes = {};
    let skew = 0;
    let liveSig = '';
    let thinkOpen = false;
    let runStarted = 0;
    const clock = (sec) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;
    const ago = (lane) => Math.max(0, Date.now() / 1000 + skew - (lane.since || 0));

    const render = (items, total = items.length) => {
      const stick = nearBottom();
      setEmpty(total === 0);
      const seen = new Set();
      let fresh = false;
      items.forEach((it) => {
        seen.add(it.id);
        const sig = JSON.stringify(it);
        const known = nodes.get(it.id);
        if (known && known.sig === sig) return;
        const el = itemEl(it);
        if (!el) return;
        if (known) known.el.replaceWith(el); else { feed.insertBefore(el, liveEl); fresh = true; }
        nodes.set(it.id, { el, sig });
        // агент поменял мастерскую — стол перечитывает её без перезагрузки
        if (it.type === 'usage' && seenUsage && !seenUsage.has(it.id) && (it.touched || []).includes('desk')) {
          window.dispatchEvent(new CustomEvent('lab:desk-changed'));
        }
      });
      for (const [id, n] of nodes) if (!seen.has(id)) { n.el.remove(); nodes.delete(id); }
      if (!seenUsage) seenUsage = new Set(items.filter((it) => it.type === 'usage').map((it) => it.id));
      else items.forEach((it) => { if (it.type === 'usage') seenUsage.add(it.id); });
      let waiting = 0;
      items.forEach((it) => {
        if (it.status === 'awaiting') waiting += 1;
        (it.agents || []).forEach((a) => (a.steps || []).forEach((s) => { if (s.status === 'awaiting') waiting += 1; }));
      });
      pending.hidden = waiting < 2;
      $('[data-pending-n]').textContent = waiting;
      if (stick || fresh) toBottom(true);
    };

    const renderLive = () => {
      const lane = running && lanes.main;
      if (!lane) { liveEl.hidden = true; liveSig = ''; return; }
      const sig = JSON.stringify([lane.since, lane.step, lane.chars, lane.say, lane.thinking]);
      if (sig === liveSig) return;
      const stick = liveEl.hidden || nearBottom();
      liveSig = sig;
      liveEl.hidden = false;
      liveEl.innerHTML = `<div class="ac-live-head"><span class="ac-live-orb${lane.chars ? ' is-writing' : ''}"><i></i></span>
          <b>${lane.chars ? 'Пишет ответ' : 'Обдумывает'}</b>${lane.step > 1 ? `<span class="ac-live-step">шаг ${esc(lane.step)}</span>` : ''}
          <span class="ac-live-time" data-live-time></span></div>
        ${lane.say ? `<div class="ac-live-say ac-md">${md(lane.say)}</div>` : ''}
        ${lane.thinking ? `<details class="ac-live-think" ${thinkOpen ? 'open' : ''}><summary>Ход мыслей</summary><p>${esc(lane.thinking)}</p></details>` : ''}`;
      const think = liveEl.querySelector('.ac-live-think');
      if (think) think.addEventListener('toggle', () => { thinkOpen = think.open; });
      if (stick) toBottom(false);
    };
    setInterval(() => {
      if (!running) return;
      const t = liveEl.querySelector('[data-live-time]');
      if (t && lanes.main) t.textContent = clock(ago(lanes.main));
      const clk = statusEl.querySelector('[data-live-clock]');
      if (clk && runStarted) clk.textContent = clock(Date.now() / 1000 + skew - runStarted);
      // помощники и их «что сейчас делают»
      app.querySelectorAll('[data-agent]').forEach((el) => {
        const lane = lanes[el.dataset.agent];
        const sum = el.querySelector(':scope > .ac-step-row .ac-step-sum');
        if (!lane || !sum) return;
        sum.textContent = (lane.chars ? 'пишет' : 'обдумывает') + ` · шаг ${lane.step || 1} · ${clock(ago(lane))}`;
        sum.classList.add('is-live');
      });
    }, 1000);

    const setRun = (run) => {
      running = !!run && run.state !== 'idle';
      lanes = (running && run.lanes) || {};
      if (run && run.now) skew = run.now - Date.now() / 1000;
      runStarted = (running && run.started) || 0;
      stopBtn.hidden = !running;
      sendBtn.hidden = running;
      form.classList.toggle('is-running', running);
      if (boot.ready) input.placeholder = running ? 'Уточните или поправьте — агент прочтёт по ходу работы…' : 'Напишите задачу агенту…';
      const texts = { thinking: 'Думаю…', tools: 'Работаю', awaiting: 'Жду вашего решения', stopping: 'Останавливаюсь…' };
      const cost = run && run.cost_label ? ` · ${run.cost_label}` : '';
      const since = runStarted ? clock(Math.max(0, Date.now() / 1000 + skew - runStarted)) : '0:00';
      statusEl.innerHTML = running ? `<i class="ac-spin"></i><span>${esc(run.text || texts[run.state] || 'Работаю…')} · <span data-live-clock>${since}</span>${esc(cost)}</span>` : '';
      statusEl.hidden = !running;
      showSpent();
      renderLive();
    };

    let spentLabel = '';
    let hintT = null;
    const showSpent = () => {
      if (hintT) return;
      spentEl.textContent = spentLabel ? `В этом чате ≈ ${spentLabel}` : '';
      spentEl.hidden = running || !spentLabel;
    };
    const hint = (text) => {
      clearTimeout(hintT);
      spentEl.textContent = text;
      spentEl.hidden = false;
      hintT = setTimeout(() => { hintT = null; showSpent(); }, 3500);
    };

    /* модель: короткое имя и цена обращения; список — свой (на телефоне — шторкой) */
    const listEl = $('[data-model-list]');
    listEl.innerHTML = (boot.models || []).map((m) => `<button type="button" class="ac-model-opt" role="option" data-model-opt="${m.default ? '' : esc(m.id)}"
        data-name="${esc(m.name)}" data-est="${esc(m.label || '')}">
        <span class="ac-model-opt-text"><b>${esc(m.name)}</b><small>${esc(m.id)}${m.default ? ' · <span class="ac-model-tag">основная</span>' : m.cheap ? ' · дешёвая' : ''}</small></span>
        ${m.label ? `<span class="ac-model-opt-price">≈ ${esc(m.label)}<small>за обращение</small></span>` : ''}
        <i class="ac-model-check">${ic('check', 14)}</i></button>`).join('');
    if (!boot.pick) modelBtn.disabled = true;
    const optsAll = () => [...modelPop.querySelectorAll('[data-model-opt]')];
    const updateModel = () => {
      const all = optsAll();
      const opt = all.find((o) => o.dataset.modelOpt === model) || all[0];
      all.forEach((o) => { o.classList.toggle('is-on', o === opt); o.setAttribute('aria-selected', String(o === opt)); });
      if (!opt) return;
      $('[data-model-name]').textContent = opt.dataset.name;
      const price = $('[data-model-price]');
      price.textContent = opt.dataset.est ? `≈ ${opt.dataset.est}` : '';
      price.hidden = !opt.dataset.est;
      modelBtn.title = boot.pick ? `Модель: ${opt.dataset.name}${opt.dataset.est ? ` — ≈ ${opt.dataset.est} за обращение` : ''}`
        : `Модель лаборатории: ${opt.dataset.name}. Выбирать модель можно с правом «Выбирать модель»`;
    };
    const asSheet = () => window.matchMedia('(max-width: 860px)').matches;
    const openModels = (on) => {
      if (on) {
        const sheet = asSheet();
        (sheet ? document.body : modelBox).appendChild(modelPop);
        modelPop.classList.toggle('is-sheet', sheet);
      }
      modelPop.hidden = !on;
      modelBtn.setAttribute('aria-expanded', String(on));
      if (on) { const cur = modelPop.querySelector('.ac-model-opt.is-on') || optsAll()[0]; if (cur) cur.focus({ preventScroll: true }); }
    };

    const setMode = (value) => {
      mode = value === 'auto' && boot.auto ? 'auto' : 'manual';
      const auto = mode === 'auto';
      modeBtn.classList.toggle('is-auto', auto);
      modeBtn.setAttribute('aria-pressed', String(auto));
      $('[data-mode-icon]').innerHTML = ic(auto ? 'flash' : 'shield', 14);
      $('[data-mode-label]').textContent = auto ? 'Авто' : 'Ручной';
      modeBtn.title = !boot.auto ? 'Ручной: всё, что меняет данные, ждёт вашего «Разрешить». Режим «Авто» включается правом «Агент без подтверждений».'
        : auto ? 'Авто: агент меняет задачи, проекты и мастерскую без подтверждений. Нажмите — вернуть ручной режим.'
          : 'Ручной: всё, что меняет данные, ждёт вашего «Разрешить». Нажмите — включить авто.';
    };

    const reveal = (el) => { el.classList.remove('is-new'); void el.offsetWidth; el.classList.add('is-new'); };
    const applyChat = (chat) => {
      const title = chat.title || 'Новый чат';
      if (titleEl.textContent !== title && titleEl.contentEditable !== 'true') {
        const was = titleEl.textContent;
        titleEl.textContent = title;
        if (was && was !== 'Новый чат') reveal(titleEl);
      }
      if (!compact) document.title = `${chat.title || 'ИИ-агент'} — Лаборатория UNIT`;
      model = chat.model || '';
      updateModel();
      spentLabel = chat.spent_label || '';
      showSpent();
      setMode(chat.mode);
      context = chat.context || context;
      menu.hidden = !chatId;
      if (full) full.href = chatId ? `/ai/${chatId}` : '/ai';
      const link = list && list.querySelector(`[data-chat-link="${chatId}"] span`);
      if (link && link.textContent !== title) { link.textContent = title; reveal(link); }
    };

    const poll = async () => {
      clearTimeout(pollT);
      if (!chatId) return;
      const id = chatId;
      const data = await api(`/api/ai/c/${id}?v=${version}`).catch(() => null);
      if (id !== chatId) return;
      if (data && !data.error) {
        if (!data.same) {
          applyChat(data);
          render(data.items || [], data.total == null ? (data.items || []).length : data.total);
        }
        version = data.v || 0;
        setRun(data.run);
      }
      if (running || !data) pollT = setTimeout(poll, data ? 800 : 2500);
      else if (!data.same && titleEl.textContent === 'Новый чат') pollT = setTimeout(poll, 2500);   // название придёт чуть позже
    };

    /* чаты */
    const addToList = (chat) => {
      if (!list) return;
      list.querySelectorAll('.ac-chat').forEach((a) => a.classList.remove('is-on'));
      const a = h(`<div class="ac-chat is-on" data-chat-link="${esc(chat.id)}">
        <a href="/ai/${esc(chat.id)}"><span>${esc(chat.title)}</span></a>
        <button type="button" class="ac-chat-del" data-del-chat="${esc(chat.id)}" title="Удалить чат" aria-label="Удалить чат">${ic('trash', 14)}</button></div>`);
      const label = list.querySelector('.ac-list-label');
      if (label) label.after(a); else list.prepend(a);
      const empty = list.querySelector('.ac-list-empty');
      if (empty) empty.hidden = true;
    };
    const ensureChat = async () => {
      if (chatId) return chatId;
      const data = await api('/api/ai/new', { model, mode, context });
      if (!data.chat) throw new Error(data.error || 'Не получилось начать чат');
      chatId = data.chat.id;
      if (!compact) history.replaceState(null, '', '/ai/' + chatId);
      addToList(data.chat);
      menu.hidden = false;
      if (full) full.href = `/ai/${chatId}`;
      return chatId;
    };
    const reset = (ctx) => {
      clearTimeout(pollT);
      chatId = '';
      version = 0;
      running = false;
      context = ctx == null ? context : ctx;
      seenUsage = new Set();
      for (const [, n] of nodes) n.el.remove();
      nodes.clear();
      titleEl.textContent = 'Новый чат';
      menu.hidden = true;
      model = '';
      spentLabel = '';
      setMode(boot.mode);
      updateModel();
      setRun(null);
      setEmpty(true);
      if (full) full.href = '/ai';
    };

    /* отправка */
    const autosize = () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 220) + 'px';
      form.classList.toggle('has-text', !!input.value.trim() || files.length > 0);
    };
    input.addEventListener('input', autosize);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !window.matchMedia('(pointer: coarse)').matches) {
        e.preventDefault();
        form.requestSubmit();
      }
    });
    const OK_FILE = /\.(txt|md|csv|json|py|js|ts|tsx|jsx|html?|css|sql|ya?ml|xml|log|ini|toml|sh|go|rs|java|kt|c|cpp|h|cs|php|rb|swift|tex)$/i;
    const showFiles = () => {
      filesBox.hidden = !files.length;
      filesBox.innerHTML = files.map((f, i) => `<span class="ac-file">${ic('file', 13)}${esc(f.name)}
        <button type="button" data-unfile="${i}" aria-label="Убрать">×</button></span>`).join('');
      autosize();
    };
    let pasteN = 0;
    const addFiles = (incoming) => {
      const skipped = [];
      [...incoming].forEach((f) => {
        if (OK_FILE.test(f.name) && f.size <= 400 * 1024) files.push(f); else skipped.push(f.name || 'файл');
      });
      files = files.slice(0, 6);
      showFiles();
      if (skipped.length) hint(`Такие файлы агент не читает (или они больше 400 КБ): ${skipped.join(', ')}`);
    };
    fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });
    input.addEventListener('paste', (e) => {
      const text = (e.clipboardData && e.clipboardData.getData('text/plain')) || '';
      if (text.length > 6000) {
        e.preventDefault();
        pasteN += 1;
        addFiles([new File([text], `Вставленный текст ${pasteN}.txt`, { type: 'text/plain' })]);
        hint(`Длинный текст (${text.length.toLocaleString('ru-RU')} знаков) приложен файлом — агент прочитает его целиком`);
      }
    });
    const box = form.querySelector('.ac-box');
    const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
    ['dragenter', 'dragover'].forEach((type) => form.addEventListener(type, (e) => { if (hasFiles(e)) { e.preventDefault(); box.classList.add('is-drop'); } }));
    form.addEventListener('dragleave', (e) => { if (!form.contains(e.relatedTarget)) box.classList.remove('is-drop'); });
    form.addEventListener('drop', (e) => {
      box.classList.remove('is-drop');
      if (!hasFiles(e)) return;
      e.preventDefault();
      addFiles(e.dataTransfer.files);
    });
    filesBox.addEventListener('click', (e) => {
      const b = e.target.closest('[data-unfile]');
      if (b) { files.splice(Number(b.dataset.unfile), 1); showFiles(); }
    });
    const flash = (text) => {
      const n = h(`<div class="ac-note is-err ac-flash">${esc(text)}</div>`);
      feed.insertBefore(n, liveEl);
      setTimeout(() => n.remove(), 6000);
    };

    const send = async (text) => {
      if (!text && !files.length) return;
      const queued = running;
      sendBtn.disabled = true;
      try {
        await ensureChat();
        const body = new FormData();
        body.append('text', text);
        files.forEach((f) => body.append('files', f));
        setEmpty(false);
        const temp = h(`<div class="ac-item is-temp">${RENDER.user({ text, files: files.map((f) => ({ name: f.name })) })}</div>`);
        feed.insertBefore(temp, liveEl);
        toBottom(true);
        input.value = '';
        autosize();
        const sent = files;
        files = [];
        showFiles();
        const data = await api(`/api/ai/c/${chatId}/send`, body);
        temp.remove();
        if (data.error) { setEmpty(!nodes.size); flash(data.error); input.value = text; files = sent; showFiles(); return; }
        if (!(queued && data.queued)) setRun({ state: 'thinking', text: 'Думаю…', started: Date.now() / 1000, now: Date.now() / 1000 });
        version = 0;
        poll();
        U.haptic('tap');
      } catch (err) {
        flash(err.message || 'Нет связи с сервером');
      } finally {
        sendBtn.disabled = false;
      }
    };
    form.addEventListener('submit', (e) => { e.preventDefault(); send(input.value.trim()); });
    stopBtn.addEventListener('click', async () => {
      if (!chatId) return;
      setRun({ state: 'stopping', text: 'Останавливаюсь…' });
      await api(`/api/ai/c/${chatId}/stop`, {}).catch(() => null);
      poll();
    });

    /* разрешить / отклонить */
    app.addEventListener('click', async (e) => {
      const one = e.target.closest('[data-decide]');
      const all = e.target.closest('[data-decide-all]');
      if (!one && !all) return;
      e.preventDefault();
      const btn = one || all;
      btn.disabled = true;
      await api(`/api/ai/c/${chatId}/decide`, one ? { tool: one.dataset.decide, ok: one.dataset.ok === '1' }
        : { tool: 'all', ok: all.dataset.decideAll === '1' });
      U.haptic(btn.dataset.ok === '0' || btn.dataset.decideAll === '0' ? 'tap' : 'success');
      version = 0;
      poll();
    });

    /* настройки чата */
    modeBtn.addEventListener('click', () => {
      if (!boot.auto) { hint('Режим «Авто» включается правом «Агент без подтверждений» в «Доступах»'); return; }
      setMode(mode === 'auto' ? 'manual' : 'auto');
      hint(mode === 'auto' ? 'Авто: агент будет менять данные без подтверждений' : 'Ручной режим: изменения — только после вашего «Разрешить»');
      if (chatId) api(`/api/ai/c/${chatId}/settings`, { mode });
    });
    modelBtn.addEventListener('click', () => openModels(modelPop.hidden));
    modelPop.addEventListener('click', (e) => {
      const opt = e.target.closest('[data-model-opt]');
      if (!opt) { if (e.target === modelPop) openModels(false); return; }
      const changed = opt.dataset.modelOpt !== model;
      model = opt.dataset.modelOpt;
      updateModel();
      openModels(false);
      modelBtn.focus({ preventScroll: true });
      if (changed && chatId) api(`/api/ai/c/${chatId}/settings`, { model });
    });
    modelPop.addEventListener('keydown', (e) => {
      const all = optsAll();
      const at = all.indexOf(document.activeElement);
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const next = all[(at + (e.key === 'ArrowDown' ? 1 : -1) + all.length) % all.length];
        if (next) next.focus();
      } else if (e.key === 'Escape') { openModels(false); modelBtn.focus(); }
    });
    document.addEventListener('click', (e) => {
      const path = e.composedPath();
      if (!modelPop.hidden && !path.includes(modelPop) && !path.includes(modelBtn)) openModels(false);
      if (!path.includes(menu)) menuPop.hidden = true;
    });
    const rename = () => {
      if (!chatId) return;
      titleEl.contentEditable = 'true';
      titleEl.focus();
      document.getSelection().selectAllChildren(titleEl);
    };
    titleEl.addEventListener('click', rename);
    titleEl.addEventListener('blur', () => {
      if (titleEl.contentEditable !== 'true') return;
      titleEl.contentEditable = 'false';
      const t = titleEl.textContent.trim().slice(0, 200);
      if (t && chatId) api(`/api/ai/c/${chatId}/settings`, { title: t }).then((d) => d.chat && applyChat({ ...d.chat, spent_label: spentLabel }));
    });
    titleEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); titleEl.blur(); } });

    const twice = (btn, ask) => {
      if (btn.classList.contains('is-armed')) return true;
      const was = btn.innerHTML;
      btn.classList.add('is-armed');
      btn.innerHTML = ask;
      setTimeout(() => { btn.classList.remove('is-armed'); btn.innerHTML = was; }, 3000);
      return false;
    };
    const removeChat = async (id) => {
      const data = await api(`/api/ai/c/${id}/delete`, {}).catch(() => ({ error: 'Нет связи с сервером' }));
      if (data.error) { flash(data.error); return; }
      const row = list && list.querySelector(`[data-chat-link="${id}"]`);
      if (row) row.remove();
      if (list) { const empty = list.querySelector('.ac-list-empty'); if (empty) empty.hidden = !!list.querySelector('.ac-chat'); }
      if (id === chatId) { if (compact) reset(); else window.location.href = '/ai'; }
    };
    if (list) list.addEventListener('click', (e) => {
      const b = e.target.closest('[data-del-chat]');
      if (!b) return;
      e.preventDefault();
      if (twice(b, `${ic('trash', 14)}<span>Удалить?</span>`)) removeChat(b.dataset.delChat);
    });
    $('[data-menu-open]').addEventListener('click', (e) => { e.stopPropagation(); menuPop.hidden = !menuPop.hidden; });
    $('[data-rename]').addEventListener('click', () => { menuPop.hidden = true; rename(); });
    $('[data-delete]').addEventListener('click', (e) => {
      if (!chatId || !twice(e.currentTarget, `${ic('trash', 14)}Точно удалить?`)) return;
      menuPop.hidden = true;
      removeChat(chatId);
    });
    app.querySelectorAll('[data-new]').forEach((b) => b.addEventListener('click', () => {
      if (compact) { reset(); input.focus(); } else window.location.href = '/ai' + (context ? `?context=${encodeURIComponent(context)}` : '');
    }));
    app.querySelectorAll('[data-suggest]').forEach((b) => b.addEventListener('click', () => {
      input.value = b.dataset.suggest;
      autosize();
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }));

    /* список чатов на телефоне — шторка */
    const side = $('[data-side]');
    const scrim = $('[data-side-scrim]');
    const openSide = (on) => { if (!side) return; side.classList.toggle('is-open', on); scrim.hidden = !on; };
    const sideOpen = $('[data-side-open]');
    if (sideOpen) sideOpen.addEventListener('click', () => openSide(true));
    const sideClose = $('[data-side-close]');
    if (sideClose) sideClose.addEventListener('click', () => openSide(false));
    if (scrim) scrim.addEventListener('click', () => openSide(false));

    /* память агента */
    const memDlg = $('[data-memory]');
    if (memDlg) {
      const memList = $('[data-memory-list]');
      const showMemory = (items) => {
        memList.innerHTML = items.length ? items.map((m) => `<li><span>${esc(m.text)}</span>
          <button type="button" class="ac-icon" data-forget="${esc(m.id)}" title="Забыть" aria-label="Забыть">${ic('trash', 14)}</button></li>`).join('')
          : '<li class="ac-muted">Пока пусто. Агент сам запоминает важное о вас — или напишите ему «запомни, что…»</li>';
      };
      const memCall = async (body) => {
        const data = await api('/api/ai/memory', body).catch(() => null);
        if (data && data.items) showMemory(data.items);
      };
      $('[data-memory-open]').addEventListener('click', () => { openSide(false); memDlg.showModal(); memCall(); });
      memDlg.addEventListener('click', (e) => {
        const f = e.target.closest('[data-forget]');
        if (f) memCall({ forget: f.dataset.forget });
        else if (e.target === memDlg || e.target.closest('[data-memory-close]')) memDlg.close();
      });
      $('[data-memory-form]').addEventListener('submit', (e) => {
        e.preventDefault();
        const field = e.currentTarget.elements.text;
        if (field.value.trim()) memCall({ text: field.value.trim() });
        field.value = '';
      });
    }

    if (!boot.ready) {
      input.placeholder = 'ИИ лаборатории не подключён';
      const nr = $('[data-not-ready]');
      if (nr) nr.hidden = false;
    }
    setMode(boot.mode);
    updateModel();
    menu.hidden = !chatId;
    if (chatId) poll(); else setEmpty(true);

    return {
      /* открыть новый разговор (с контекстом страницы) и, если есть, сразу отправить просьбу */
      ask(text, ctx) {
        if (chatId && (ctx || '') !== (context || '') || (chatId && text)) reset(ctx || '');
        else if (ctx != null) context = ctx;
        if (text) send(text);
        else if (window.matchMedia('(pointer: fine)').matches) setTimeout(() => input.focus(), 60);
      },
      focus() { input.focus(); },
    };
  }

  /* ═══ Где живёт агент ════════════════════════════════════════════════════ */
  let bootPromise = null;
  const getBoot = () => {
    if (!bootPromise) {
      const own = document.getElementById('agent-boot');
      bootPromise = own ? Promise.resolve(JSON.parse(own.textContent || '{}'))
        : api('/api/ai/boot').then((d) => (d.error ? Promise.reject(new Error(d.error)) : d));
    }
    return bootPromise;
  };
  const fromTemplate = (holder) => {
    const tpl = document.getElementById('agent-tpl');
    if (!tpl) return false;
    holder.innerHTML = '';
    holder.appendChild(tpl.content.cloneNode(true));
    holder.classList.add('ac', 'ac-compact', 'is-empty');
    return true;
  };

  window.LabAgent = {
    /* окно мастерской: собрать компактный чат внутри контейнера */
    async mountInto(holder, opts = {}) {
      if (!fromTemplate(holder)) return null;
      const boot = await getBoot().catch(() => ({ ready: false, models: [] }));
      return mount(holder, Object.assign({ boot }, opts));
    },
    md,
  };

  const pageContext = () => {
    const m = location.pathname.match(/^\/p\/(\d+)/);
    if (m) return 'p' + m[1];
    if (location.pathname === '/desk' || location.pathname.startsWith('/workshop')) return 'desk';
    return '';
  };

  document.addEventListener('DOMContentLoaded', () => {
    const page = document.querySelector('[data-agent-app]');
    if (page) {
      getBoot().then((boot) => {
        const ctrl = mount(page, { boot });
        if (page.dataset.ask && !page.dataset.chat) {
          ctrl.ask(page.dataset.ask, page.dataset.context);
          history.replaceState(null, '', location.pathname);
        }
      });
    }
    // Панель «Спросить агента» — справа на компьютере, во весь экран на телефоне
    const drawer = document.querySelector('[data-agent-drawer]');
    let drawerCtrl = null;
    let drawerReady = null;
    const ensureDrawer = () => {
      if (!drawerReady) {
        drawerReady = getBoot().then((boot) => {
          fromTemplate(drawer);
          drawerCtrl = mount(drawer, { boot, context: pageContext() });
          return drawerCtrl;
        });
      }
      return drawerReady;
    };
    document.addEventListener('click', (event) => {
      const opener = event.target.closest('[data-ai-open]');
      if (!opener) return;
      event.preventDefault();
      if (page) {                                   // уже на странице агента — просьба идёт в неё
        const text = opener.getAttribute('data-ask') || '';
        if (text) window.location.href = '/ai?q=' + encodeURIComponent(text) + '&context=' + encodeURIComponent(pageContext());
        return;
      }
      if (!drawer) { window.location.href = '/ai'; return; }
      U.openSheet('ai-sheet');
      ensureDrawer().then((ctrl) => ctrl.ask(opener.getAttribute('data-ask') || '', pageContext()))
        .catch((err) => U.toast(err.message || 'ИИ недоступен', 'err'));
    });
  });
})();
