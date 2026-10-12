/* Рабочий стол: карточки, фильтры, редактор с автосохранением, файлы. */
(function () {
    'use strict';
    var U = window.UNIT, LAB = window.LAB;
    var $ = function (sel, root) { return (root || document).querySelector(sel); };
    var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
    var root = $('[data-desk]');
    if (!root || !LAB) return;

    var KINDS = { note: 'Заметка', checklist: 'Чек-лист', link: 'Ссылка', file: 'Файл', code: 'Код' };
    var ICONS = { note: 'note', checklist: 'checklist', link: 'link', file: 'file', code: 'code' };
    var items = JSON.parse($('#desk-data').textContent || '[]');
    var grid = $('[data-desk-grid]');
    var filter = { kind: '', q: '' };
    var esc = function (t) { return String(t == null ? '' : t).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
    var icon = function (n, s) { return '<svg class="ic" width="' + (s || 14) + '" height="' + (s || 14) + '" aria-hidden="true"><use href="#i-' + n + '"/></svg>'; };
    var fail = function (e) { U.toast(e.message || String(e), 'err'); U.haptic('error'); };

    function size(n) {
        n = Number(n || 0);
        return n > 1048576 ? (n / 1048576).toFixed(1) + ' МБ' : Math.max(1, Math.round(n / 1024)) + ' КБ';
    }
    function card(it) {
        var body = '';
        if (it.kind === 'checklist') {
            var list = (it.data.items || []);
            var done = list.filter(function (i) { return i.done; }).length;
            body = '<div class="dc-list">' + list.slice(0, 6).map(function (i) {
                return '<span class="' + (i.done ? 'on' : '') + '">' + (i.done ? '☑ ' : '☐ ') + esc(i.text) + '</span>';
            }).join('') + (list.length > 6 ? '<span>…ещё ' + (list.length - 6) + '</span>' : '') + '</div>'
                + (list.length ? '<div class="progress" style="height: 4px"><i style="width: ' + Math.round(done / list.length * 100) + '%"></i></div>' : '');
        } else if (it.kind === 'code') {
            body = it.body ? '<pre class="dc-body">' + esc(it.body.split('\n').slice(0, 10).join('\n')) + '</pre>' : '';
        } else if (it.kind === 'link') {
            body = '<span class="dc-body" style="color: var(--accent)">' + esc((it.data.url || '').replace(/^https?:\/\//, '')) + '</span>'
                + (it.body ? '<span class="dc-body">' + esc(it.body) + '</span>' : '');
        } else if (it.kind === 'file') {
            var img = /^image\//.test(it.data.type || '');
            body = img ? '<img src="' + esc(it.url) + '" alt="" loading="lazy" style="border-radius: 10px; max-height: 180px; object-fit: cover">' : '<span class="dc-body">' + esc(it.data.name || '') + ' · ' + size(it.data.size) + '</span>';
        } else if (it.body) {
            body = '<span class="dc-body">' + esc(it.body) + '</span>';
        }
        return '<button class="desk-card' + (it.color ? ' c-' + it.color : '') + '" type="button" data-item="' + it.id + '">'
            + '<span class="dc-top">' + icon(ICONS[it.kind]) + KINDS[it.kind] + (it.pinned ? '<span class="spacer">' + icon('pin', 13) + '</span>' : '') + '</span>'
            + (it.title ? '<h4>' + esc(it.title) + '</h4>' : '') + body
            + '<span class="dc-foot">' + esc(it.updated) + (it.project ? '<span class="chip" style="height: 20px">' + esc(it.project) + '</span>' : '')
            + (it.shared ? '<span title="Видно команде">' + icon('users', 13) + '</span>' : '') + '</span></button>';
    }
    function draw() {
        var q = filter.q.toLowerCase();
        var shown = items.filter(function (it) {
            if (filter.kind && it.kind !== filter.kind) return false;
            if (!q) return true;
            var text = [it.title, it.body, it.project, (it.data.items || []).map(function (i) { return i.text; }).join(' '), it.data.url, it.data.name].join(' ').toLowerCase();
            return text.indexOf(q) >= 0;
        }).sort(function (a, b) { return (b.pinned - a.pinned); });
        grid.innerHTML = shown.map(card).join('');
        $('[data-desk-empty]').hidden = items.length > 0;
    }

    $('[data-desk-search]').addEventListener('input', function (e) { filter.q = e.target.value; draw(); });
    $('[data-desk-filter]').addEventListener('click', function (e) {
        var b = e.target.closest('[data-kind]');
        if (!b) return;
        filter.kind = b.getAttribute('data-kind');
        $$('[data-kind]', this).forEach(function (x) { x.classList.toggle('on', x === b); });
        draw();
    });

    /* ── Редактор ─────────────────────────────────────────────────────────── */
    var sheet = $('#desk-sheet');
    var current = null, timer = null, pending = {};
    var state = $('[data-ed-state]', sheet);
    function field(name) { return $('[data-ed="' + name + '"]', sheet); }
    function save(changes, now) {
        if (!current) return;
        Object.keys(changes).forEach(function (k) { pending[k] = changes[k]; });
        state.textContent = 'Сохраняю…';
        clearTimeout(timer);
        timer = setTimeout(flush, now ? 0 : 700);
    }
    function flush() {
        if (!current || !Object.keys(pending).length) return Promise.resolve();
        var body = pending, id = current.id;
        pending = {};
        return LAB.api('/api/desk/' + id, body).then(function (it) {
            replace(it);
            if (current && current.id === id) current = it;
            state.textContent = 'Сохранено';
        }).catch(function (e) { state.textContent = 'Не сохранилось'; fail(e); });
    }
    function replace(it) {
        var found = false;
        items = items.map(function (x) { if (x.id === it.id) { found = true; return it; } return x; });
        if (!found) items.unshift(it);
        draw();
    }
    function renderChecks() {
        var list = current.data.items || [];
        $('[data-ed-items]', sheet).innerHTML = list.map(function (i, n) {
            return '<label class="check-item' + (i.done ? ' on' : '') + '"><input type="checkbox" data-ci="' + n + '"' + (i.done ? ' checked' : '') + '>'
                + '<input type="text" data-ct="' + n + '" value="' + esc(i.text) + '">'
                + '<button class="icon-btn x" type="button" data-cx="' + n + '" aria-label="Убрать">' + icon('x', 14) + '</button></label>';
        }).join('');
    }
    function open(it) {
        current = JSON.parse(JSON.stringify(it));
        pending = {};
        state.textContent = '';
        $('[data-ed-icon]', sheet).innerHTML = icon(ICONS[it.kind], 16);
        field('title').value = it.title || '';
        $$('[data-ed-pane]', sheet).forEach(function (p) { p.hidden = p.getAttribute('data-ed-pane') !== it.kind; });
        if (it.kind === 'note') field('body').value = it.body || '';
        if (it.kind === 'code') { field('code').value = it.body || ''; field('lang').value = it.data.lang || 'python'; }
        if (it.kind === 'link') {
            field('url').value = it.data.url || '';
            field('linknote').value = it.body || '';
            $('[data-ed-go]', sheet).href = it.data.url || '#';
        }
        if (it.kind === 'checklist') renderChecks();
        if (it.kind === 'file') {
            $('[data-ed-fname]', sheet).textContent = it.data.name || '';
            $('[data-ed-fsize]', sheet).textContent = size(it.data.size);
            $('[data-ed-download]', sheet).href = it.url;
            var img = $('[data-ed-preview]', sheet);
            img.hidden = !/^image\//.test(it.data.type || '');
            if (!img.hidden) img.src = it.url;
        }
        field('project_id').value = it.project_id || '';
        field('shared').checked = !!it.shared;
        field('shared').disabled = !it.project_id;
        field('pinned').checked = !!it.pinned;
        $$('[data-color]', sheet).forEach(function (b) { b.classList.toggle('on', b.getAttribute('data-color') === (it.color || '')); });
        U.openSheet(sheet);
        $$('textarea[data-autosize]', sheet).forEach(function (a) { U.autosize(a); });
        history.replaceState(null, '', location.pathname + '?open=' + it.id);
    }
    sheet.addEventListener('close', function () {
        flush();
        current = null;
        history.replaceState(null, '', location.pathname);
    });
    field('title').addEventListener('input', function (e) { save({ title: e.target.value }); });
    field('body').addEventListener('input', function (e) { save({ body: e.target.value }); });
    field('code').addEventListener('input', function (e) { save({ body: e.target.value }); });
    field('code').addEventListener('keydown', function (e) {
        if (e.key !== 'Tab') return;
        e.preventDefault();
        var a = e.target, s = a.selectionStart;
        a.value = a.value.slice(0, s) + '    ' + a.value.slice(a.selectionEnd);
        a.selectionStart = a.selectionEnd = s + 4;
        save({ body: a.value });
    });
    field('lang').addEventListener('change', function (e) { save({ lang: e.target.value }, true); });
    field('url').addEventListener('input', function (e) { $('[data-ed-go]', sheet).href = e.target.value; save({ url: e.target.value }); });
    field('linknote').addEventListener('input', function (e) { save({ body: e.target.value }); });
    field('project_id').addEventListener('change', function (e) {
        field('shared').disabled = !e.target.value;
        if (!e.target.value) field('shared').checked = false;
        save({ project_id: e.target.value }, true);
    });
    field('shared').addEventListener('change', function (e) { save({ shared: e.target.checked }, true); });
    field('pinned').addEventListener('change', function (e) { save({ pinned: e.target.checked }, true); });
    $('[data-ed-colors]', sheet).addEventListener('click', function (e) {
        var b = e.target.closest('[data-color]');
        if (!b) return;
        $$('[data-color]', sheet).forEach(function (x) { x.classList.toggle('on', x === b); });
        save({ color: b.getAttribute('data-color') }, true);
    });
    // Чек-лист
    var syncChecks = function () { save({ items: current.data.items }, true); };
    $('[data-ed-items]', sheet).addEventListener('change', function (e) {
        var n = e.target.getAttribute('data-ci');
        if (n === null) return;
        current.data.items[Number(n)].done = e.target.checked;
        e.target.closest('.check-item').classList.toggle('on', e.target.checked);
        if (e.target.checked) U.haptic('success');
        syncChecks();
    });
    $('[data-ed-items]', sheet).addEventListener('input', function (e) {
        var n = e.target.getAttribute('data-ct');
        if (n === null) return;
        current.data.items[Number(n)].text = e.target.value;
        save({ items: current.data.items });
    });
    $('[data-ed-items]', sheet).addEventListener('click', function (e) {
        var x = e.target.closest('[data-cx]');
        if (!x) return;
        e.preventDefault();
        current.data.items.splice(Number(x.getAttribute('data-cx')), 1);
        renderChecks();
        syncChecks();
    });
    $('[data-ed-add]', sheet).addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' || !e.target.value.trim()) return;
        e.preventDefault();
        current.data.items = (current.data.items || []).concat([{ text: e.target.value.trim(), done: false }]);
        e.target.value = '';
        renderChecks();
        syncChecks();
        U.haptic('select');
    });
    $('[data-ed-delete]', sheet).addEventListener('click', function () {
        if (!current || !window.confirm('Удалить «' + (current.title || KINDS[current.kind]) + '»?')) return;
        var id = current.id;
        LAB.api('/api/desk/' + id + '/delete', {}).then(function () {
            items = items.filter(function (x) { return x.id !== id; });
            current = null;
            U.closeSheet(sheet);
            draw();
            U.haptic('warn');
            U.toast('Удалено');
        }).catch(fail);
    });
    $$('[data-ed-ai]', sheet).forEach(function (b) {
        b.addEventListener('click', function () {
            if (!current) return;
            var opener = document.createElement('button');
            opener.setAttribute('data-ai-open', '');
            opener.setAttribute('data-item', current.id);
            opener.setAttribute('data-thread', current.project_id ? 'p' + current.project_id : 'me');
            opener.setAttribute('data-ask', b.getAttribute('data-ed-ai') + ' («' + (current.title || KINDS[current.kind]) + '»)');
            opener.hidden = true;
            document.body.appendChild(opener);
            flush().then(function () {
                U.closeSheet(sheet);
                setTimeout(function () { opener.click(); opener.remove(); }, 220);
            });
        });
    });

    // Открыть карточку
    grid.addEventListener('click', function (e) {
        var c = e.target.closest('[data-item]');
        if (!c) return;
        var it = items.filter(function (x) { return String(x.id) === c.getAttribute('data-item'); })[0];
        if (it) open(it);
    });
    // Создать
    $$('[data-new]').forEach(function (b) {
        b.addEventListener('click', function () {
            LAB.api('/api/desk', { kind: b.getAttribute('data-new') }).then(function (it) {
                replace(it);
                open(it);
                setTimeout(function () { field('title').focus(); }, 80);
            }).catch(fail);
        });
    });
    // Файлы: кнопка и перетаскивание
    function upload(files) {
        Array.prototype.forEach.call(files, function (file) {
            var data = new FormData();
            data.append('file', file);
            U.toast('Загружаю «' + file.name + '»…');
            LAB.api('/api/desk/upload', data).then(function (it) { replace(it); U.haptic('success'); }).catch(fail);
        });
    }
    $('[data-upload]').addEventListener('change', function (e) { upload(e.target.files); e.target.value = ''; });
    var drop = $('[data-drop]');
    document.addEventListener('dragover', function (e) {
        if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') >= 0) {
            e.preventDefault();
            drop.hidden = false;
            drop.classList.add('on');
        }
    });
    document.addEventListener('dragleave', function (e) { if (!e.relatedTarget) { drop.hidden = true; drop.classList.remove('on'); } });
    document.addEventListener('drop', function (e) {
        if (!e.dataTransfer || !e.dataTransfer.files.length) return;
        e.preventDefault();
        drop.hidden = true;
        upload(e.dataTransfer.files);
    });

    // Общие материалы команды
    var shared = $('#shared-sheet');
    document.addEventListener('click', function (e) {
        var b = e.target.closest('[data-open-shared]');
        if (!b) return;
        LAB.api('/api/desk/' + b.getAttribute('data-open-shared')).then(function (it) {
            $('[data-sh-title]', shared).textContent = it.title || KINDS[it.kind];
            var body = it.kind === 'checklist' ? (it.data.items || []).map(function (i) { return (i.done ? '☑ ' : '☐ ') + esc(i.text); }).join('<br>')
                : it.kind === 'code' ? '<pre class="md" style="margin: 0"><code>' + esc(it.body) + '</code></pre>'
                : it.kind === 'link' ? '<a href="' + esc(it.data.url) + '" target="_blank" rel="noopener noreferrer" style="color: var(--accent)">' + esc(it.data.url) + '</a><div class="md">' + LAB.md(it.body) + '</div>'
                : it.kind === 'file' ? '<a class="btn btn-ghost" href="' + esc(it.url) + '">Скачать ' + esc(it.data.name) + '</a>'
                : '<div class="md">' + LAB.md(it.body) + '</div>';
            $('[data-sh-body]', shared).innerHTML = body;
            $('[data-sh-meta]', shared).textContent = (it.project ? it.project + ' · ' : '') + it.updated;
            $('[data-sh-copy]', shared).hidden = it.kind === 'file';
            shared.setAttribute('data-id', it.id);
            U.openSheet(shared);
        }).catch(fail);
    });
    $('[data-sh-copy]', shared).addEventListener('click', function () {
        LAB.api('/api/desk/' + shared.getAttribute('data-id') + '/copy', {}).then(function (it) {
            replace(it);
            U.closeSheet(shared);
            U.toast('Теперь и на вашем столе');
            U.haptic('success');
        }).catch(fail);
    });

    draw();
    var wanted = new URLSearchParams(location.search).get('open');
    if (wanted) {
        var it = items.filter(function (x) { return String(x.id) === wanted; })[0];
        if (it) open(it);
    }
})();
