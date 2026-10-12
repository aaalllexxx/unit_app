/* Мастерская — рабочий стол со свободными окнами и инструментами. */
(function () {
    'use strict';
    var U = window.UNIT, LAB = window.LAB;
    var $ = function (sel, root) { return (root || document).querySelector(sel); };
    var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
    var root = $('[data-wd]');
    if (!root || !LAB) return;

    var rawData = JSON.parse($('#desk-data').textContent || '{}');
    var items = rawData.items || [];
    var projects = rawData.projects || [];
    var projectNames = rawData.project_names || {};
    var sharedItems = rawData.shared || [];
    var langs = rawData.langs || ['python', 'javascript', 'typescript', 'html', 'css', 'sql', 'bash', 'json', 'markdown'];
    var colors = rawData.colors || ['', 'blue', 'teal', 'amber', 'rose', 'violet'];

    var canvas = $('[data-wd-desk]', root);
    var hint = $('[data-wd-hint]', root);
    var snapGhost = $('[data-wd-snap]', root);
    var dockRow = $('[data-wd-dock-row]', root);
    var saveEl = $('[data-wd-save]', root);
    var projectFilterEl = $('[data-wd-project]', root);
    var titleEl = $('[data-wd-title]', root);
    var fsBtn = $('[data-wd-fs]', root);
    var launchEl = $('[data-wd-launch]', root);

    var zTop = 20;
    var uid = 0;
    var wins = [];
    var tools = new Map();
    var lastWins = {};
    var focusedWin = null;
    var selectedProject = '';

    var esc = function (t) { return String(t == null ? '' : t).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
    var icon = function (n, s) { return '<svg class="ic" width="' + (s || 15) + '" height="' + (s || 15) + '" aria-hidden="true"><use href="#i-' + n + '"/></svg>'; };
    var h = function (html) { var t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
    var debounce = function (fn, ms) { var t = null; return function () { var a = arguments; clearTimeout(t); t = setTimeout(function () { fn.apply(null, a); }, ms); }; };
    var fail = function (e) { U.toast(e.message || String(e), 'err'); U.haptic('error'); };

    /* ── Сохранение состояния ── */
    var savingCount = 0;
    var setSaving = function (isSaving, isErr) {
        if (isSaving) savingCount++; else if (savingCount > 0) savingCount--;
        saveEl.textContent = isErr ? 'Ошибка сохранения' : (savingCount > 0 ? 'Сохраняю…' : 'Сохранено');
        saveEl.classList.toggle('is-busy', savingCount > 0 && !isErr);
        saveEl.classList.toggle('is-err', !!isErr);
    };

    /* ── Раскладка окон: хранится на сервере, её же может поменять ИИ-агент ── */
    var booting = true;
    var lastLayout = '';
    function snapshot() {
        return {
            title: titleEl ? titleEl.value.trim() : '',
            wins: wins.map(function (w) {
                return { tool: w.tool, item_id: w.item_id || null, x: w.x, y: w.y, w: w.w, h: w.h,
                         min: !!w.min, max: !!w.max, z: w.z || 0 };
            })
        };
    }
    var saveLayout = debounce(function () {
        var data = snapshot();
        var sig = JSON.stringify(data);
        if (sig === lastLayout) return;
        lastLayout = sig;
        LAB.api('/api/desk/layout', data).catch(function () { lastLayout = ''; });
    }, 700);
    function queueLayout() { if (!booting) saveLayout(); }

    /* ── Управление наработками ── */
    function updateItemInState(updated) {
        var found = false;
        items = items.map(function (it) {
            if (it.id === updated.id) { found = true; return updated; }
            return it;
        });
        if (!found) items.unshift(updated);
        notifyTool('files');
        queueLayout();
    }

    function removeItemFromState(id) {
        items = items.filter(function (it) { return it.id !== id; });
        notifyTool('files');
    }

    function notifyTool(toolId) {
        wins.forEach(function (w) {
            if (w.tool === toolId && w.inst && w.inst.refresh) {
                try { w.inst.refresh(); } catch (err) { console.error(err); }
            }
        });
    }

    /* ═══ Окна ═══════════════════════════════════════════════════════════════ */
    var MIN_W = 280, MIN_H = 190;
    var isPhone = function () { return window.matchMedia('(max-width: 760px)').matches; };

    function place(win) {
        var el = win.el;
        el.style.left = (win.x * 100) + '%';
        el.style.top = (win.y * 100) + '%';
        el.style.width = (win.w * 100) + '%';
        el.style.height = (win.h * 100) + '%';
        queueLayout();
    }

    function clamp(win) {
        var cw = canvas.clientWidth || 800;
        var ch = canvas.clientHeight || 600;
        win.w = Math.min(1, Math.max(MIN_W / cw, win.w));
        win.h = Math.min(0.95, Math.max(MIN_H / ch, win.h));
        win.x = Math.min(1 - Math.min(win.w, 0.2), Math.max(-win.w + 0.15, win.x));
        win.y = Math.min(1 - 44 / ch, Math.max(0, win.y));
    }

    function cascade(def) {
        var n = wins.filter(function (w) { return !w.min; }).length % 7;
        return {
            x: 0.05 + n * 0.035,
            y: 0.04 + n * 0.045,
            w: def.w || 0.45,
            h: def.h || 0.6
        };
    }

    function updateHint() {
        hint.hidden = wins.some(function (w) { return !w.min; });
    }

    function focusWin(win) {
        wins.forEach(function (w) { w.el.classList.toggle('is-focus', w === win); });
        win.el.style.zIndex = String(++zTop);
        win.z = zTop;
        queueLayout();
        lastWins[win.tool] = win;
        focusedWin = win;
        renderDock();
    }

    function snapZone(e) {
        var r = canvas.getBoundingClientRect();
        if (e.clientY - r.top < 8) return { x: 0, y: 0, w: 1, h: 1, max: true };
        if (e.clientX - r.left < 14) return { x: 0, y: 0, w: 0.5, h: 1 };
        if (r.right - e.clientX < 14) return { x: 0.5, y: 0, w: 0.5, h: 1 };
        return null;
    }

    function dragWindow(win) {
        var bar = $('.wd-win-bar', win.el);
        bar.addEventListener('pointerdown', function (e) {
            if (e.button !== 0 || e.target.closest('button, input, select, textarea, a') || isPhone()) return;
            var r = canvas.getBoundingClientRect();
            var start = { x: e.clientX, y: e.clientY, wx: win.x, wy: win.y };
            var moved = false, zone = null;
            bar.setPointerCapture(e.pointerId);

            var move = function (ev) {
                var dx = (ev.clientX - start.x) / r.width;
                var dy = (ev.clientY - start.y) / r.height;
                if (!moved && Math.abs(ev.clientX - start.x) + Math.abs(ev.clientY - start.y) < 4) return;
                if (!moved && win.max) {
                    win.max = false;
                    win.el.classList.remove('is-max');
                    start.wx = (ev.clientX - r.left) / r.width - win.w / 2;
                    start.wy = 0;
                }
                moved = true;
                win.el.classList.add('is-drag');
                win.x = start.wx + dx;
                win.y = start.wy + dy;
                clamp(win);
                place(win);
                zone = snapZone(ev);
                snapGhost.hidden = !zone;
                if (zone) {
                    Object.assign(snapGhost.style, {
                        left: (zone.x * 100) + '%',
                        top: (zone.y * 100) + '%',
                        width: (zone.w * 100) + '%',
                        height: (zone.h * 100) + '%'
                    });
                }
            };

            var up = function () {
                bar.removeEventListener('pointermove', move);
                bar.removeEventListener('pointerup', up);
                bar.removeEventListener('pointercancel', up);
                win.el.classList.remove('is-drag');
                snapGhost.hidden = true;
                if (zone) {
                    if (zone.max) {
                        win.max = true;
                        win.el.classList.add('is-max');
                    } else {
                        Object.assign(win, { x: zone.x, y: zone.y, w: zone.w, h: zone.h });
                        place(win);
                    }
                    if (win.inst && win.inst.resize) win.inst.resize();
                }
            };

            bar.addEventListener('pointermove', move);
            bar.addEventListener('pointerup', up);
            bar.addEventListener('pointercancel', up);
        });
    }

    function resizeWindow(win) {
        win.el.querySelectorAll('[data-rs]').forEach(function (grip) {
            grip.addEventListener('pointerdown', function (e) {
                if (e.button !== 0 || win.max) return;
                e.preventDefault();
                var r = canvas.getBoundingClientRect();
                var s = { x: e.clientX, y: e.clientY, wx: win.x, wy: win.y, ww: win.w, wh: win.h };
                var side = grip.getAttribute('data-rs');
                grip.setPointerCapture(e.pointerId);
                win.el.classList.add('is-drag');

                var move = function (ev) {
                    var dx = (ev.clientX - s.x) / r.width;
                    var dy = (ev.clientY - s.y) / r.height;
                    var minW = MIN_W / r.width;
                    var minH = MIN_H / r.height;
                    if (side.indexOf('r') >= 0) win.w = Math.max(minW, s.ww + dx);
                    if (side.indexOf('b') >= 0) win.h = Math.max(minH, s.wh + dy);
                    if (side.indexOf('l') >= 0) {
                        var w = Math.max(minW, s.ww - dx);
                        win.x = s.wx + (s.ww - w);
                        win.w = w;
                    }
                    place(win);
                };

                var up = function () {
                    grip.removeEventListener('pointermove', move);
                    grip.removeEventListener('pointerup', up);
                    win.el.classList.remove('is-drag');
                    if (win.inst && win.inst.resize) win.inst.resize();
                };

                grip.addEventListener('pointermove', move);
                grip.addEventListener('pointerup', up);
            });
        });
    }

    function makeWindow(def, opts) {
        opts = opts || {};
        var winId = ++uid;
        var el = h('<section class="wd-win" data-win-id="' + winId + '" role="dialog" aria-label="' + esc(opts.title || def.title) + '">'
            + '<header class="wd-win-bar">'
            + '<span class="wd-win-ic">' + icon(def.icon, 15) + '</span>'
            + '<span class="wd-win-title">' + esc(opts.title || def.title) + '</span>'
            + '<span class="wd-win-extra"></span>'
            + '<button type="button" class="wd-win-btn" data-win-min title="Свернуть" aria-label="Свернуть"><i class="wd-g-min"></i></button>'
            + '<button type="button" class="wd-win-btn" data-win-max title="Развернуть" aria-label="Развернуть"><i class="wd-g-max"></i></button>'
            + '<button type="button" class="wd-win-btn is-close" data-win-close title="Закрыть" aria-label="Закрыть">' + icon('x', 14) + '</button>'
            + '</header>'
            + '<div class="wd-win-body"></div>'
            + '<i class="wd-rs" data-rs="r"></i><i class="wd-rs" data-rs="b"></i><i class="wd-rs" data-rs="l"></i>'
            + '<i class="wd-rs" data-rs="rb"></i><i class="wd-rs" data-rs="lb"></i>'
            + '</section>');

        var win = Object.assign({
            id: winId,
            tool: def.id,
            def: def,
            el: el,
            body: $('.wd-win-body', el),
            inst: null,
            min: false,
            max: false,
            item_id: opts.item ? opts.item.id : null,
        }, cascade(def));

        if (opts.box) Object.assign(win, opts.box);

        win.setTitle = function (text) {
            $('.wd-win-title', el).textContent = text;
            win.title = text;
            renderDock();
        };
        win.setExtra = function (html) {
            var extra = $('.wd-win-extra', el);
            extra.innerHTML = html;
            return extra;
        };
        win.focus = function () { focusWin(win); };
        win.minimize = function () {
            win.min = true;
            el.hidden = true;
            updateHint();
            renderDock();
            queueLayout();
        };
        win.restore = function () {
            if (win.min) {
                win.min = false;
                el.hidden = false;
                updateHint();
                if (win.inst && win.inst.resize) win.inst.resize();
                queueLayout();
            }
        };
        win.toggleMax = function () {
            win.max = !win.max;
            el.classList.toggle('is-max', win.max);
            if (win.inst && win.inst.resize) win.inst.resize();
            queueLayout();
        };
        win.close = function () {
            try { if (win.inst && win.inst.close) win.inst.close(); } catch (err) { console.error(err); }
            el.remove();
            wins = wins.filter(function (w) { return w !== win; });
            if (lastWins[win.tool] === win) delete lastWins[win.tool];
            if (focusedWin === win) focusedWin = null;
            var next = wins.filter(function (w) { return !w.min; }).sort(function (a, b) { return b.z - a.z; })[0];
            if (next) focusWin(next); else renderDock();
            updateHint();
            queueLayout();
        };

        clamp(win);
        place(win);
        canvas.appendChild(el);
        wins.push(win);

        el.addEventListener('pointerdown', function () { if (focusedWin !== win) focusWin(win); }, true);
        $('[data-win-min]', el).addEventListener('click', function () { win.minimize(); });
        $('[data-win-max]', el).addEventListener('click', function () { win.toggleMax(); });
        $('[data-win-close]', el).addEventListener('click', function () { win.close(); });
        $('.wd-win-bar', el).addEventListener('dblclick', function (e) {
            if (!e.target.closest('button, input, select')) win.toggleMax();
        });

        dragWindow(win);
        resizeWindow(win);

        try {
            win.inst = def.mount(win.body, win, opts) || {};
        } catch (err) {
            console.error(err);
            win.body.innerHTML = '<div class="inset" style="margin: 20px; color: var(--danger)">Ошибка инструмента: ' + esc(err.message) + '</div>';
            win.inst = {};
        }

        focusWin(win);
        updateHint();
        return win;
    }

    function openTool(toolId, opts) {
        opts = opts || {};
        var def = tools.get(toolId);
        if (!def) { U.toast('Инструмент не найден', 'err'); return null; }

        if (opts.item && opts.item.id) {
            var existing = wins.find(function (w) { return w.item_id === opts.item.id; });
            if (existing) {
                existing.restore();
                focusWin(existing);
                return existing;
            }
        }

        if (def.single) {
            var found = wins.find(function (w) { return w.tool === toolId; });
            if (found) {
                found.restore();
                focusWin(found);
                if (found.inst && found.inst.load) found.inst.load(opts);
                return found;
            }
        }

        return makeWindow(def, opts);
    }

    function registerTool(def) {
        tools.set(def.id, Object.assign({ w: 0.45, h: 0.6 }, def));
    }

    /* ═══ Док ═══════════════════════════════════════════════════════════════ */
    function renderDock() {
        var html = '<div class="wd-dock-group">';
        tools.forEach(function (def) {
            var openWin = wins.find(function (w) { return w.tool === def.id && !w.min; });
            var hasAny = wins.some(function (w) { return w.tool === def.id; });
            var isFocus = focusedWin && focusedWin.tool === def.id && !focusedWin.min;
            var cls = 'wd-dock-btn' + (hasAny ? ' is-open' : '') + (isFocus ? ' is-focus' : '');
            html += '<button type="button" class="' + cls + '" data-dock-tool="' + def.id + '" title="' + esc(def.title) + '">'
                + icon(def.icon, 20)
                + '<span class="wd-dock-mark"></span>'
                + '</button>';
        });
        html += '</div><span class="wd-dock-sep"></span>'
            + '<button type="button" class="wd-dock-btn is-all' + (launchEl && !launchEl.hidden ? ' is-focus' : '') + '" data-dock-all title="Все инструменты" aria-label="Все инструменты">'
            + icon('grid', 20) + '<span class="wd-dock-mark"></span></button>';
        dockRow.innerHTML = html;
    }

    /* ── Каталог: все инструменты с пояснениями ── */
    var ABOUT = {
        files: 'Все наработки стола: поиск, фильтр по типу и проекту, файлы',
        note: 'Текст в Markdown с предпросмотром — сохраняется сам',
        code: 'Сниппет кода с подсветкой языка',
        table: 'Таблица данных: строки и столбцы, вставка из Excel',
        draw: 'Доска для схем и набросков от руки',
        checklist: 'Список дел с отметками — можно привязать к проекту',
        github: 'Репозитории привязанного GitHub: файлы и README',
        links: 'Ссылки под рукой: документация, макеты, стенды',
        timer: 'Помодоро и учёт времени по задаче',
        ai: 'ИИ-агент видит стол и проекты и может навести порядок'
    };
    function openLaunch(on) {
        if (!launchEl) return;
        if (on) {
            var cards = '';
            tools.forEach(function (def) {
                cards += '<button type="button" class="wd-launch-item" data-launch-tool="' + def.id + '">'
                    + '<span class="wd-launch-ic">' + icon(def.icon, 18) + '</span>'
                    + '<span class="wd-launch-text"><b>' + esc(def.title) + '</b><small>' + esc(ABOUT[def.id] || '') + '</small></span></button>';
            });
            launchEl.innerHTML = '<div class="wd-launch-card" role="dialog" aria-label="Инструменты мастерской">'
                + '<div class="wd-launch-head"><b>Инструменты</b><span>Нажмите, чтобы открыть окно на столе</span>'
                + '<button type="button" class="wd-icon-btn" data-launch-close aria-label="Закрыть">' + icon('x', 15) + '</button></div>'
                + '<div class="wd-launch-body"><div class="wd-launch-group"><div class="wd-launch-grid">' + cards + '</div></div></div></div>';
        }
        launchEl.hidden = !on;
        renderDock();
    }
    if (launchEl) launchEl.addEventListener('click', function (e) {
        var item = e.target.closest('[data-launch-tool]');
        if (item) { openLaunch(false); openTool(item.getAttribute('data-launch-tool')); return; }
        if (e.target === launchEl || e.target.closest('[data-launch-close]')) openLaunch(false);
    });

    dockRow.addEventListener('click', function (e) {
        if (e.target.closest('[data-dock-all]')) { openLaunch(launchEl.hidden); return; }
        var btn = e.target.closest('[data-dock-tool]');
        if (!btn) return;
        var toolId = btn.getAttribute('data-dock-tool');
        var win = wins.find(function (w) { return w.tool === toolId; });

        if (!win) {
            openTool(toolId);
        } else if (win.min) {
            win.restore();
            focusWin(win);
        } else if (focusedWin === win) {
            win.minimize();
        } else {
            focusWin(win);
        }
    });

    /* ═══ Инструменты ════════════════════════════════════════════════════════ */

    /* 1. Материалы стола (Файловый менеджер) */
    registerTool({
        id: 'files',
        title: 'Материалы',
        icon: 'folder',
        single: true,
        w: 0.46,
        h: 0.65,
        mount: function (body, win) {
            var filterKind = '', searchQ = '';
            function render() {
                var q = searchQ.trim().toLowerCase();
                var list = items.filter(function (it) {
                    if (selectedProject && String(it.project_id) !== String(selectedProject)) return false;
                    if (filterKind && it.kind !== filterKind) return false;
                    if (!q) return true;
                    var text = [it.title, it.body, it.project, (it.data.items || []).map(function (i) { return i.text; }).join(' '), it.data.name].join(' ').toLowerCase();
                    return text.indexOf(q) >= 0;
                });

                body.innerHTML = '<div class="wt-bar">'
                    + '<div class="searchbar" style="margin: 0; flex: 1 1 140px">' + icon('search', 14) + '<input class="input input-sm" type="search" placeholder="Поиск" data-f-search value="' + esc(searchQ) + '"></div>'
                    + '<select class="select input-sm" data-f-kind style="width: auto">'
                    + '<option value="">Все типы</option>'
                    + '<option value="note"' + (filterKind === 'note' ? ' selected' : '') + '>Заметки</option>'
                    + '<option value="code"' + (filterKind === 'code' ? ' selected' : '') + '>Код</option>'
                    + '<option value="checklist"' + (filterKind === 'checklist' ? ' selected' : '') + '>Чек-листы</option>'
                    + '<option value="link"' + (filterKind === 'link' ? ' selected' : '') + '>Ссылки</option>'
                    + '<option value="file"' + (filterKind === 'file' ? ' selected' : '') + '>Файлы</option>'
                    + '</select>'
                    + '<button class="wt-btn is-primary" type="button" data-f-upload>' + icon('upload', 13) + 'Загрузить</button>'
                    + '</div>'
                    + '<div class="wt-scroll">'
                    + (list.length ? '<div class="stack-xs">' + list.map(function (it) {
                        return '<div class="panel row-link" style="padding: 10px 12px; display: flex; align-items: center; gap: 10px; cursor: pointer" data-open-item="' + it.id + '">'
                            + '<span class="badge app-badge" style="width: 30px; height: 30px; flex: 0 0 auto">' + icon(it.kind === 'note' ? 'note' : it.kind === 'code' ? 'code' : it.kind === 'checklist' ? 'checklist' : it.kind === 'link' ? 'link' : 'file', 15) + '</span>'
                            + '<div class="grow stack-xs" style="gap: 2px; min-width: 0">'
                            + '<b class="small strong truncate">' + esc(it.title || 'Без названия') + '</b>'
                            + '<span class="tiny faint">' + (it.project ? esc(it.project) + ' · ' : '') + esc(it.updated) + '</span>'
                            + '</div>'
                            + '<button class="wt-btn is-icon" type="button" data-del-item="' + it.id + '" title="Удалить">' + icon('trash', 13) + '</button>'
                            + '</div>';
                    }).join('') + '</div>' : '<div class="empty small">Находок нет. Создайте заметку или загрузите файл.</div>')
                    + '</div>';

                var sInput = $('[data-f-search]', body);
                sInput.addEventListener('input', function (e) { searchQ = e.target.value; render(); });
                var kSelect = $('[data-f-kind]', body);
                kSelect.addEventListener('change', function (e) { filterKind = e.target.value; render(); });

                $('[data-f-upload]', body).addEventListener('click', function () {
                    $('#desk-file-input').click();
                });
            }

            body.addEventListener('click', function (e) {
                var delBtn = e.target.closest('[data-del-item]');
                if (delBtn) {
                    var id = Number(delBtn.getAttribute('data-del-item'));
                    if (window.confirm('Удалить эту наработку?')) {
                        LAB.api('/api/desk/' + id + '/delete', {}).then(function () {
                            removeItemFromState(id);
                            U.toast('Удалено');
                        }).catch(fail);
                    }
                    return;
                }
                var openRow = e.target.closest('[data-open-item]');
                if (openRow) {
                    var itId = Number(openRow.getAttribute('data-open-item'));
                    var item = items.find(function (x) { return x.id === itId; });
                    if (item) {
                        if (item.kind === 'note') openTool('note', { item: item, title: item.title });
                        else if (item.kind === 'code') openTool('code', { item: item, title: item.title });
                        else if (item.kind === 'checklist') openTool('checklist', { item: item, title: item.title });
                        else if (item.kind === 'link') openTool('links', { item: item });
                        else if (item.kind === 'file') window.open(item.url, '_blank');
                    }
                }
            });

            render();
            return { refresh: render };
        }
    });

    /* 2. Документ и Заметки (Markdown) */
    registerTool({
        id: 'note',
        title: 'Документ',
        icon: 'note',
        w: 0.5,
        h: 0.65,
        mount: function (body, win, opts) {
            var item = opts.item || null;
            var text = item ? (item.body || '') : '';
            var title = item ? (item.title || '') : 'Новая заметка';
            var projectId = item ? item.project_id : (selectedProject || '');
            var isPreview = false;

            body.innerHTML = '<div class="wt-bar">'
                + '<input class="wt-input wt-grow" style="font-weight: 600" data-n-title placeholder="Название документа" value="' + esc(title) + '">'
                + '<button class="wt-btn is-icon" type="button" data-md="b" title="Жирный"><b>B</b></button>'
                + '<button class="wt-btn is-icon" type="button" data-md="i" title="Курсив"><i>I</i></button>'
                + '<button class="wt-btn is-icon" type="button" data-md="h2" title="Заголовок">H</button>'
                + '<button class="wt-btn is-icon" type="button" data-md="code" title="Код">' + icon('code', 13) + '</button>'
                + '<button class="wt-btn is-icon" type="button" data-md="list" title="Список">' + icon('list', 13) + '</button>'
                + '<button class="wt-btn' + (isPreview ? ' is-on' : '') + '" type="button" data-n-preview>' + icon('eye', 13) + 'Просмотр</button>'
                + '<button class="wt-btn is-primary" type="button" data-n-save>' + icon('check', 13) + 'Сохранить</button>'
                + '</div>'
                + '<div class="wt-note-wrap">'
                + '<textarea class="wt-note-editor" data-n-body placeholder="Пишите здесь… Поддерживается Markdown: **жирный**, списки, код.">' + esc(text) + '</textarea>'
                + '<div class="wt-note-preview md" data-n-rendered hidden></div>'
                + '</div>';

            var tInput = $('[data-n-title]', body);
            var bInput = $('[data-n-body]', body);
            var prevBox = $('[data-n-rendered]', body);

            var saveDebounced = debounce(function () {
                var curTitle = tInput.value.trim() || 'Заметка';
                var curBody = bInput.value;
                win.setTitle(curTitle);
                setSaving(true);
                if (item && item.id) {
                    LAB.api('/api/desk/' + item.id, { title: curTitle, body: curBody, project_id: projectId }).then(function (res) {
                        item = res;
                        updateItemInState(res);
                        setSaving(false);
                    }).catch(function (err) { setSaving(false, true); fail(err); });
                } else {
                    LAB.api('/api/desk', { kind: 'note', title: curTitle, body: curBody, project_id: projectId }).then(function (res) {
                        item = res;
                        win.item_id = res.id;
                        updateItemInState(res);
                        setSaving(false);
                    }).catch(function (err) { setSaving(false, true); fail(err); });
                }
            }, 600);

            tInput.addEventListener('input', saveDebounced);
            bInput.addEventListener('input', saveDebounced);

            $('[data-n-save]', body).addEventListener('click', function () {
                saveDebounced();
                U.toast('Сохранено на стол', 'ok');
            });

            $('[data-n-preview]', body).addEventListener('click', function () {
                isPreview = !isPreview;
                this.classList.toggle('is-on', isPreview);
                bInput.hidden = isPreview;
                prevBox.hidden = !isPreview;
                if (isPreview) {
                    prevBox.innerHTML = LAB.md(bInput.value || '_Текст пуст_');
                }
            });

            body.addEventListener('click', function (e) {
                var btn = e.target.closest('[data-md]');
                if (!btn) return;
                var act = btn.getAttribute('data-md');
                var start = bInput.selectionStart, end = bInput.selectionEnd;
                var sel = bInput.value.substring(start, end);
                var repl = sel;
                if (act === 'b') repl = '**' + (sel || 'текст') + '**';
                else if (act === 'i') repl = '*' + (sel || 'текст') + '*';
                else if (act === 'h2') repl = '\n## ' + (sel || 'Заголовок') + '\n';
                else if (act === 'code') repl = '`' + (sel || 'код') + '`';
                else if (act === 'list') repl = '\n* ' + (sel || 'пункт') + '\n';

                bInput.setRangeText(repl, start, end, 'end');
                bInput.focus();
                saveDebounced();
            });

            return {};
        }
    });

    /* 3. Редактор кода */
    registerTool({
        id: 'code',
        title: 'Код',
        icon: 'code',
        w: 0.52,
        h: 0.65,
        mount: function (body, win, opts) {
            var item = opts.item || null;
            var text = item ? (item.body || '') : '# Напишите код здесь\nprint("Hello, UNIT!")\n';
            var title = item ? (item.title || '') : 'Скрипт';
            var curLang = item && item.data ? (item.data.lang || 'python') : 'python';

            body.innerHTML = '<div class="wt-bar">'
                + '<input class="wt-input" style="font-weight: 600; flex: 1 1 120px" data-c-title placeholder="Название" value="' + esc(title) + '">'
                + '<select class="select input-sm" data-c-lang style="width: auto">'
                + langs.map(function (l) { return '<option' + (l === curLang ? ' selected' : '') + '>' + l + '</option>'; }).join('')
                + '</select>'
                + '<button class="wt-btn" type="button" data-c-copy title="Скопировать">' + icon('copy', 13) + 'Копия</button>'
                + '<button class="wt-btn is-primary" type="button" data-c-run title="Запустить (JS/HTML)">' + icon('play', 13) + 'Запуск</button>'
                + '<button class="wt-btn" type="button" data-c-save>' + icon('check', 13) + 'Сохранить</button>'
                + '</div>'
                + '<div class="wt-code-wrap">'
                + '<textarea class="wt-code-editor" data-c-body spellcheck="false">' + esc(text) + '</textarea>'
                + '<div class="wt-code-out" data-c-out hidden><b style="display: block; margin-bottom: 4px; color: var(--ink)">Вывод:</b><pre style="margin: 0"></pre></div>'
                + '</div>';

            var tInput = $('[data-c-title]', body);
            var lSelect = $('[data-c-lang]', body);
            var editor = $('[data-c-body]', body);
            var outBox = $('[data-c-out]', body);
            var outPre = $('pre', outBox);

            var saveDebounced = debounce(function () {
                var curTitle = tInput.value.trim() || 'Код';
                var curBody = editor.value;
                curLang = lSelect.value;
                win.setTitle(curTitle);
                setSaving(true);
                if (item && item.id) {
                    LAB.api('/api/desk/' + item.id, { title: curTitle, body: curBody, lang: curLang }).then(function (res) {
                        item = res;
                        updateItemInState(res);
                        setSaving(false);
                    }).catch(function (err) { setSaving(false, true); fail(err); });
                } else {
                    LAB.api('/api/desk', { kind: 'code', title: curTitle, body: curBody, lang: curLang }).then(function (res) {
                        item = res;
                        win.item_id = res.id;
                        updateItemInState(res);
                        setSaving(false);
                    }).catch(function (err) { setSaving(false, true); fail(err); });
                }
            }, 600);

            editor.addEventListener('keydown', function (e) {
                if (e.key === 'Tab') {
                    e.preventDefault();
                    var s = editor.selectionStart, end = editor.selectionEnd;
                    editor.value = editor.value.substring(0, s) + '    ' + editor.value.substring(end);
                    editor.selectionStart = editor.selectionEnd = s + 4;
                    saveDebounced();
                }
            });

            tInput.addEventListener('input', saveDebounced);
            editor.addEventListener('input', saveDebounced);
            lSelect.addEventListener('change', saveDebounced);

            $('[data-c-copy]', body).addEventListener('click', function () {
                navigator.clipboard.writeText(editor.value).then(function () {
                    U.toast('Скопировано в буфер', 'ok');
                });
            });

            $('[data-c-save]', body).addEventListener('click', function () {
                saveDebounced();
                U.toast('Код сохранён на стол', 'ok');
            });

            $('[data-c-run]', body).addEventListener('click', function () {
                outBox.hidden = false;
                var lang = lSelect.value;
                if (lang === 'javascript') {
                    try {
                        var log = [];
                        var fakeConsole = { log: function () { log.push(Array.prototype.slice.call(arguments).join(' ')); } };
                        var fn = new Function('console', editor.value);
                        fn(fakeConsole);
                        outPre.textContent = log.join('\n') || 'Скрипт выполнен без вывода в консоль.';
                    } catch (err) {
                        outPre.textContent = 'Ошибка выполнения: ' + err.message;
                    }
                } else {
                    outPre.textContent = 'Запуск на стороне клиента поддерживается для JavaScript. Язык: ' + lang;
                }
            });

            return {};
        }
    });

    /* 4. Таблица (Электронная таблица) */
    registerTool({
        id: 'table',
        title: 'Таблица',
        icon: 'table',
        w: 0.56,
        h: 0.65,
        mount: function (body, win) {
            var cols = 5, rows = 8;
            var data = {}; // 'r:c' -> val

            function colName(i) { return String.fromCharCode(65 + i); }

            function computeFormula(raw) {
                if (!raw || raw[0] !== '=') return raw;
                var expr = raw.slice(1).toUpperCase();
                // Сумма диапазонов =СУММ(A1:A5)
                var m = expr.match(/(?:СУММ|SUM)\(([A-Z])(\d+):([A-Z])(\d+)\)/);
                if (m) {
                    var sum = 0;
                    var c1 = m[1].charCodeAt(0) - 65, r1 = parseInt(m[2], 10) - 1;
                    var c2 = m[3].charCodeAt(0) - 65, r2 = parseInt(m[4], 10) - 1;
                    for (var r = Math.min(r1, r2); r <= Math.max(r1, r2); r++) {
                        for (var c = Math.min(c1, c2); c <= Math.max(c1, c2); c++) {
                            var v = parseFloat(data[r + ':' + c] || 0);
                            if (!isNaN(v)) sum += v;
                        }
                    }
                    return String(sum);
                }
                return raw;
            }

            function render() {
                var html = '<div class="wt-bar">'
                    + '<button class="wt-btn" type="button" data-t-addrow>' + icon('plus', 12) + 'Строка</button>'
                    + '<button class="wt-btn" type="button" data-t-addcol>' + icon('plus', 12) + 'Столбец</button>'
                    + '<button class="wt-btn" type="button" data-t-csv>' + icon('down', 12) + 'CSV</button>'
                    + '<button class="wt-btn is-primary" type="button" data-t-save>' + icon('check', 12) + 'На стол</button>'
                    + '<span class="wt-grow"></span>'
                    + '<span class="tiny faint">Формулы: =СУММ(A1:A5)</span>'
                    + '</div>'
                    + '<div class="wt-tbl-wrap"><div class="wt-tbl-scroll"><table class="wt-tbl"><thead><tr><th>#</th>';

                for (var c = 0; c < cols; c++) html += '<th>' + colName(c) + '</th>';
                html += '</tr></thead><tbody>';

                for (var r = 0; r < rows; r++) {
                    html += '<tr><th>' + (r + 1) + '</th>';
                    for (var cc = 0; cc < cols; cc++) {
                        var k = r + ':' + cc;
                        var val = data[k] || '';
                        html += '<td><input data-cell="' + k + '" value="' + esc(val) + '"></td>';
                    }
                    html += '</tr>';
                }
                html += '</tbody></table></div></div>';
                body.innerHTML = html;

                body.querySelectorAll('[data-cell]').forEach(function (inp) {
                    inp.addEventListener('input', function (e) {
                        data[e.target.getAttribute('data-cell')] = e.target.value;
                    });
                    inp.addEventListener('blur', function (e) {
                        var raw = e.target.value;
                        if (raw.startsWith('=')) {
                            e.target.value = computeFormula(raw);
                        }
                    });
                });
            }

            body.addEventListener('click', function (e) {
                if (e.target.closest('[data-t-addrow]')) { rows++; render(); }
                if (e.target.closest('[data-t-addcol]')) { cols++; render(); }
                if (e.target.closest('[data-t-csv]')) {
                    var csv = [];
                    for (var r = 0; r < rows; r++) {
                        var row = [];
                        for (var c = 0; c < cols; c++) row.push('"' + (data[r + ':' + c] || '').replace(/"/g, '""') + '"');
                        csv.push(row.join(','));
                    }
                    var blob = new Blob([csv.join('\n')], { type: 'text/csv' });
                    var a = document.createElement('a');
                    a.href = URL.createObjectURL(blob);
                    a.download = 'table.csv';
                    a.click();
                }
                if (e.target.closest('[data-t-save]')) {
                    var lines = [];
                    for (var r2 = 0; r2 < rows; r2++) {
                        var rvals = [];
                        for (var c2 = 0; c2 < cols; c2++) rvals.push(data[r2 + ':' + c2] || '');
                        if (rvals.some(Boolean)) lines.push(rvals.join('\t'));
                    }
                    LAB.api('/api/desk', { kind: 'note', title: 'Таблица данных', body: '```tsv\n' + lines.join('\n') + '\n```' }).then(function (res) {
                        updateItemInState(res);
                        U.toast('Таблица сохранена как наработка', 'ok');
                    }).catch(fail);
                }
            });

            render();
            return {};
        }
    });

    /* 5. Холст и схемы (Рисование) */
    registerTool({
        id: 'draw',
        title: 'Холст',
        icon: 'palette',
        w: 0.55,
        h: 0.7,
        mount: function (body, win) {
            var curColor = '#1e293b';
            var curWidth = 3;
            var curTool = 'pen';
            var isDrawing = false;
            var lastPos = null;

            body.innerHTML = '<div class="wt-bar">'
                + '<button class="wt-btn is-on" type="button" data-d-tool="pen">' + icon('pen', 13) + 'Кисть</button>'
                + '<button class="wt-btn" type="button" data-d-tool="line">' + icon('arrow', 13) + 'Линия</button>'
                + '<button class="wt-btn" type="button" data-d-tool="eraser">' + icon('x', 13) + 'Ластик</button>'
                + '<div class="wd-sep"></div>'
                + '<button class="wt-swatch is-on" type="button" style="background: #1e293b" data-color="#1e293b"></button>'
                + '<button class="wt-swatch" type="button" style="background: #3b82f6" data-color="#3b82f6"></button>'
                + '<button class="wt-swatch" type="button" style="background: #ef4444" data-color="#ef4444"></button>'
                + '<button class="wt-swatch" type="button" style="background: #10b981" data-color="#10b981"></button>'
                + '<button class="wt-swatch" type="button" style="background: #f59e0b" data-color="#f59e0b"></button>'
                + '<div class="wd-sep"></div>'
                + '<button class="wt-btn" type="button" data-d-clear>' + icon('trash', 13) + 'Очистить</button>'
                + '<button class="wt-btn is-primary" type="button" data-d-export>' + icon('upload', 13) + 'Скачать PNG</button>'
                + '</div>'
                + '<div class="wt-draw-wrap"><div class="wt-draw-canvas-box"><canvas class="wt-draw-canvas"></canvas></div></div>';

            var canvasEl = $('.wt-draw-canvas', body);
            var ctx = canvasEl.getContext('2d');

            function fitCanvas() {
                var box = canvasEl.parentElement;
                var imgData = null;
                if (canvasEl.width > 0 && canvasEl.height > 0) {
                    try { imgData = ctx.getImageData(0, 0, canvasEl.width, canvasEl.height); } catch(e){}
                }
                canvasEl.width = box.clientWidth || 600;
                canvasEl.height = box.clientHeight || 400;
                ctx.fillStyle = '#ffffff';
                ctx.fillRect(0, 0, canvasEl.width, canvasEl.height);
                if (imgData) ctx.putImageData(imgData, 0, 0);
            }
            setTimeout(fitCanvas, 50);

            function getCanvasPos(e) {
                var r = canvasEl.getBoundingClientRect();
                return { x: e.clientX - r.left, y: e.clientY - r.top };
            }

            canvasEl.addEventListener('pointerdown', function (e) {
                isDrawing = true;
                lastPos = getCanvasPos(e);
                canvasEl.setPointerCapture(e.pointerId);
            });

            canvasEl.addEventListener('pointermove', function (e) {
                if (!isDrawing) return;
                var pos = getCanvasPos(e);
                ctx.beginPath();
                ctx.moveTo(lastPos.x, lastPos.y);
                ctx.lineTo(pos.x, pos.y);
                ctx.strokeStyle = curTool === 'eraser' ? '#ffffff' : curColor;
                ctx.lineWidth = curTool === 'eraser' ? 18 : curWidth;
                ctx.lineCap = 'round';
                ctx.stroke();
                lastPos = pos;
            });

            var stop = function () { isDrawing = false; };
            canvasEl.addEventListener('pointerup', stop);
            canvasEl.addEventListener('pointercancel', stop);

            body.addEventListener('click', function (e) {
                var tBtn = e.target.closest('[data-d-tool]');
                if (tBtn) {
                    curTool = tBtn.getAttribute('data-d-tool');
                    body.querySelectorAll('[data-d-tool]').forEach(function (b) { b.classList.toggle('is-on', b === tBtn); });
                }
                var cBtn = e.target.closest('[data-color]');
                if (cBtn) {
                    curColor = cBtn.getAttribute('data-color');
                    body.querySelectorAll('[data-color]').forEach(function (b) { b.classList.toggle('is-on', b === cBtn); });
                }
                if (e.target.closest('[data-d-clear]')) {
                    ctx.fillStyle = '#ffffff';
                    ctx.fillRect(0, 0, canvasEl.width, canvasEl.height);
                }
                if (e.target.closest('[data-d-export]')) {
                    canvasEl.toBlob(function (blob) {
                        var a = document.createElement('a');
                        a.href = URL.createObjectURL(blob);
                        a.download = 'sketch.png';
                        a.click();
                        U.toast('Рисунок скачан', 'ok');
                    });
                }
            });

            return { resize: fitCanvas };
        }
    });

    /* 6. Чек-листы */
    registerTool({
        id: 'checklist',
        title: 'Чек-лист',
        icon: 'checklist',
        w: 0.42,
        h: 0.62,
        mount: function (body, win, opts) {
            var item = opts.item || null;
            var title = item ? (item.title || '') : 'Новый список';
            var itemsList = item && item.data && Array.isArray(item.data.items) ? item.data.items : [];

            function render() {
                var doneCount = itemsList.filter(function (i) { return i.done; }).length;
                var pct = itemsList.length ? Math.round(doneCount / itemsList.length * 100) : 0;

                body.innerHTML = '<div class="wt-bar">'
                    + '<input class="wt-input wt-grow" style="font-weight: 600" data-cl-title placeholder="Название списка" value="' + esc(title) + '">'
                    + '<span class="tiny faint">' + doneCount + ' из ' + itemsList.length + '</span>'
                    + '<button class="wt-btn is-primary" type="button" data-cl-save>' + icon('check', 13) + 'Сохранить</button>'
                    + '</div>'
                    + '<div class="progress" style="height: 3px"><i style="width: ' + pct + '%"></i></div>'
                    + '<div class="wt-scroll">'
                    + '<div class="wt-check-list" data-cl-items>'
                    + itemsList.map(function (it, idx) {
                        return '<div class="wt-check-row' + (it.done ? ' is-done' : '') + '">'
                            + '<input type="checkbox" data-cl-chk="' + idx + '"' + (it.done ? ' checked' : '') + '>'
                            + '<input class="input input-sm grow" style="border: 0; background: transparent; padding: 0" data-cl-txt="' + idx + '" value="' + esc(it.text) + '">'
                            + '<button class="icon-btn" type="button" data-cl-del="' + idx + '" title="Удалить">' + icon('x', 13) + '</button>'
                            + '</div>';
                    }).join('')
                    + '</div>'
                    + '<div class="row" style="margin-top: 12px; gap: 8px">'
                    + '<input class="input input-sm grow" data-cl-add placeholder="Добавить пункт и Enter…">'
                    + '</div>'
                    + '</div>';

                var titleInput = $('[data-cl-title]', body);
                titleInput.addEventListener('input', function (e) {
                    title = e.target.value.trim() || 'Чек-лист';
                    win.setTitle(title);
                });

                var addInput = $('[data-cl-add]', body);
                addInput.addEventListener('keydown', function (e) {
                    if (e.key === 'Enter' && e.target.value.trim()) {
                        itemsList.push({ text: e.target.value.trim(), done: false });
                        render();
                        setTimeout(function () { $('[data-cl-add]', body).focus(); }, 20);
                    }
                });

                $('[data-cl-items]', body).addEventListener('change', function (e) {
                    var chk = e.target.getAttribute('data-cl-chk');
                    if (chk !== null) {
                        itemsList[Number(chk)].done = e.target.checked;
                        render();
                        U.haptic('success');
                    }
                });

                $('[data-cl-items]', body).addEventListener('input', function (e) {
                    var txt = e.target.getAttribute('data-cl-txt');
                    if (txt !== null) {
                        itemsList[Number(txt)].text = e.target.value;
                    }
                });

                $('[data-cl-items]', body).addEventListener('click', function (e) {
                    var delBtn = e.target.closest('[data-cl-del]');
                    if (delBtn) {
                        itemsList.splice(Number(delBtn.getAttribute('data-cl-del')), 1);
                        render();
                    }
                });
            }

            body.addEventListener('click', function (e) {
                if (e.target.closest('[data-cl-save]')) {
                    setSaving(true);
                    var payload = { kind: 'checklist', title: title || 'Чек-лист', items: itemsList };
                    var prom = item && item.id ? LAB.api('/api/desk/' + item.id, payload) : LAB.api('/api/desk', payload);
                    prom.then(function (res) {
                        item = res;
                        win.item_id = res.id;
                        updateItemInState(res);
                        setSaving(false);
                        U.toast('Чек-лист сохранён на стол', 'ok');
                    }).catch(function (err) { setSaving(false, true); fail(err); });
                }
            });

            render();
            return {};
        }
    });

    /* 7. GitHub Интеграция */
    registerTool({
        id: 'github',
        title: 'GitHub',
        icon: 'git',
        single: true,
        w: 0.48,
        h: 0.65,
        mount: function (body, win) {
            var gitStatus = null;
            var gitRepos = [];
            var filterQ = '';

            function loadStatus() {
                LAB.api('/api/git/status').then(function (st) {
                    gitStatus = st;
                    if (st.linked) {
                        loadRepos();
                    } else {
                        render();
                    }
                }).catch(fail);
            }

            function loadRepos() {
                render();
                LAB.api('/api/git/repos').then(function (res) {
                    gitRepos = res.repos || [];
                    if (res.error) {
                        U.toast(res.error, 'warn');
                    }
                    render();
                }).catch(function (err) {
                    U.toast('Ошибка загрузки: ' + err.message, 'err');
                    render();
                });
            }

            function render() {
                if (!gitStatus) {
                    body.innerHTML = '<div class="empty">Загрузка данных GitHub…</div>';
                    return;
                }

                if (!gitStatus.linked) {
                    body.innerHTML = '<div class="wt-scroll">'
                        + '<div class="panel inset" style="padding: 16px">'
                        + '<h3 style="margin: 0 0 8px; font-size: 15px">Подключение GitHub</h3>'
                        + '<p class="muted small" style="margin-bottom: 14px">Привяжите аккаунт, чтобы стягивать README, задачи и ссылки на рабочий стол мастерской.</p>'
                        + '<label class="field"><span class="label">Логин GitHub</span><input class="input" id="g-login" placeholder="например, octocat или ссылка на профиль"></label>'
                        + '<label class="field" style="margin-top: 10px"><span class="label">Personal Access Token (PAT, опционально)</span><input class="input" type="password" id="g-token" placeholder="ghp_…"></label>'
                        + '<button class="btn btn-primary btn-sm" type="button" id="g-link" style="margin-top: 14px">' + icon('check', 14) + 'Привязать аккаунт</button>'
                        + '</div></div>';

                    $('#g-link', body).addEventListener('click', function () {
                        var user = ($('#g-login', body).value || '').trim();
                        var tok = ($('#g-token', body).value || '').trim();
                        if (!user) { U.toast('Укажите логин GitHub', 'err'); return; }
                        LAB.api('/api/git/link', { username: user, token: tok }).then(function () {
                            U.toast('GitHub привязан', 'ok');
                            loadStatus();
                        }).catch(fail);
                    });
                    return;
                }

                var q = filterQ.trim().toLowerCase();
                var shown = gitRepos.filter(function (r) {
                    return !q || r.name.toLowerCase().indexOf(q) >= 0 || (r.description && r.description.toLowerCase().indexOf(q) >= 0);
                });

                body.innerHTML = '<div class="wt-bar">'
                    + '<span class="small faint">Аккаунт: <b style="color: var(--ink)">' + esc(gitStatus.username) + '</b></span>'
                    + '<button class="wt-btn" type="button" id="g-unlink">Отвязать</button>'
                    + '<span class="wt-grow"></span>'
                    + '<div class="searchbar" style="margin: 0; flex: 0 1 180px">' + icon('search', 13) + '<input class="input input-sm" placeholder="Поиск репозиториев" id="g-search" value="' + esc(filterQ) + '"></div>'
                    + '</div>'
                    + '<div class="wt-scroll">'
                    + '<div class="panel inset" style="padding: 10px 14px; margin-bottom: 12px">'
                    + '<span class="label" style="margin-bottom: 6px">Прямой импорт любого репозитория (по имени или URL):</span>'
                    + '<div class="row" style="gap: 8px">'
                    + '<input class="input input-sm grow" id="g-custom-repo" placeholder="owner/repo или https://github.com/…">'
                    + '<button class="btn btn-primary btn-sm" type="button" id="g-custom-import">' + icon('down', 13) + 'Стянуть</button>'
                    + '</div></div>'
                    + (shown.length ? '<div class="stack-xs">' + shown.map(function (r) {
                        return '<div class="panel row-link" style="padding: 10px 14px; display: flex; align-items: flex-start; gap: 10px">'
                            + '<span class="badge app-badge" style="width: 28px; height: 28px; margin-top: 2px">' + icon('git', 14) + '</span>'
                            + '<div class="grow stack-xs" style="gap: 2px">'
                            + '<div class="row" style="gap: 6px; align-items: center"><b class="small strong">' + esc(r.name) + '</b>'
                            + (r.language ? '<span class="chip tiny">' + esc(r.language) + '</span>' : '')
                            + (r.stars ? '<span class="chip tiny">' + r.stars + ' зв.</span>' : '') + '</div>'
                            + (r.description ? '<span class="tiny muted">' + esc(r.description) + '</span>' : '')
                            + '</div>'
                            + '<button class="wt-btn" type="button" data-g-import="' + esc(r.full_name) + '">' + icon('down', 13) + 'На стол</button>'
                            + '</div>';
                    }).join('') + '</div>' : '<div class="empty small">Репозиториев не найдено. Воспользуйтесь строкой прямого импорта выше.</div>')
                    + '</div>';

                var sInput = $('#g-search', body);
                if (sInput) sInput.addEventListener('input', function (e) { filterQ = e.target.value; render(); });

                var unlinkBtn = $('#g-unlink', body);
                if (unlinkBtn) unlinkBtn.addEventListener('click', function () {
                    LAB.api('/api/git/unlink', {}).then(function () {
                        U.toast('GitHub отвязан', 'ok');
                        loadStatus();
                    }).catch(fail);
                });

                var customBtn = $('#g-custom-import', body);
                if (customBtn) customBtn.addEventListener('click', function () {
                    var val = ($('#g-custom-repo', body).value || '').trim();
                    if (!val) { U.toast('Укажите репозиторий', 'err'); return; }
                    importRepo(val);
                });

                body.querySelectorAll('[data-g-import]').forEach(function (b) {
                    b.addEventListener('click', function () {
                        importRepo(b.getAttribute('data-g-import'));
                    });
                });
            }

            function importRepo(repoName) {
                U.toast('Стягивание ' + repoName + '…');
                LAB.api('/api/git/import', { repo: repoName, project_id: selectedProject }).then(function (res) {
                    (res.created || []).forEach(updateItemInState);
                    U.toast('Репозиторий стянут на стол', 'ok');
                    notifyTool('files');
                }).catch(fail);
            }

            loadStatus();
            return {};
        }
    });

    /* 8. Ссылки и закладки */
    registerTool({
        id: 'links',
        title: 'Ссылки',
        icon: 'link',
        w: 0.38,
        h: 0.58,
        mount: function (body, win) {
            function render() {
                var links = items.filter(function (it) { return it.kind === 'link'; });
                body.innerHTML = '<div class="wt-bar">'
                    + '<input class="wt-input grow" data-l-url placeholder="https://… новая ссылка">'
                    + '<button class="wt-btn is-primary" type="button" data-l-add>' + icon('plus', 12) + 'Добавить</button>'
                    + '</div>'
                    + '<div class="wt-scroll">'
                    + (links.length ? '<div class="stack-xs">' + links.map(function (it) {
                        return '<div class="panel row-link" style="padding: 10px 12px; display: flex; align-items: center; gap: 10px">'
                            + '<span class="badge app-badge" style="width: 28px; height: 28px">' + icon('link', 14) + '</span>'
                            + '<div class="grow stack-xs" style="gap: 2px; min-width: 0">'
                            + '<b class="small strong truncate">' + esc(it.title || (it.data.url || '').replace(/^https?:\/\//, '')) + '</b>'
                            + '<a href="' + esc(it.data.url) + '" target="_blank" rel="noopener noreferrer" class="tiny truncate" style="color: var(--accent)">' + esc(it.data.url) + '</a>'
                            + '</div>'
                            + '<a class="wt-btn is-icon" href="' + esc(it.data.url) + '" target="_blank" rel="noopener" title="Перейти">' + icon('external', 13) + '</a>'
                            + '<button class="wt-btn is-icon" type="button" data-del-link="' + it.id + '" title="Удалить">' + icon('trash', 13) + '</button>'
                            + '</div>';
                    }).join('') + '</div>' : '<div class="empty small">Закладок пока нет. Вставьте ссылку сверху.</div>')
                    + '</div>';

                $('[data-l-add]', body).addEventListener('click', function () {
                    var u = ($('[data-l-url]', body).value || '').trim();
                    if (!u) return;
                    LAB.api('/api/desk', { kind: 'link', url: u, title: u.replace(/^https?:\/\//, '').split('/')[0] }).then(function (res) {
                        updateItemInState(res);
                        render();
                    }).catch(fail);
                });

                body.querySelectorAll('[data-del-link]').forEach(function (b) {
                    b.addEventListener('click', function () {
                        var id = Number(b.getAttribute('data-del-link'));
                        LAB.api('/api/desk/' + id + '/delete', {}).then(function () {
                            removeItemFromState(id);
                            render();
                        }).catch(fail);
                    });
                });
            }

            render();
            return {};
        }
    });

    /* 9. Помодоро / Фокус-таймер */
    registerTool({
        id: 'timer',
        title: 'Таймер',
        icon: 'clock',
        single: true,
        w: 0.32,
        h: 0.52,
        mount: function (body, win) {
            var duration = 25 * 60;
            var remaining = duration;
            var timerId = null;
            var isWork = true;

            function render() {
                var m = Math.floor(remaining / 60);
                var s = remaining % 60;
                var timeStr = (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
                var pct = (duration - remaining) / duration;
                var offset = 377 * (1 - pct);

                body.innerHTML = '<div class="wt-bar">'
                    + '<button class="wt-btn' + (duration === 25 * 60 ? ' is-on' : '') + '" type="button" data-tm-set="25">25 мин</button>'
                    + '<button class="wt-btn' + (duration === 50 * 60 ? ' is-on' : '') + '" type="button" data-tm-set="50">50 мин</button>'
                    + '<button class="wt-btn' + (duration === 5 * 60 ? ' is-on' : '') + '" type="button" data-tm-set="5">5 мин отдых</button>'
                    + '</div>'
                    + '<div class="wt-tm-box">'
                    + '<div class="wt-tm-ring">'
                    + '<svg viewBox="0 0 130 130"><circle cx="65" cy="65" r="60" class="wt-tm-track"></circle>'
                    + '<circle cx="65" cy="65" r="60" class="wt-tm-progress" stroke-dasharray="377" stroke-dashoffset="' + offset + '"></circle></svg>'
                    + '<div class="wt-tm-text"><span class="wt-tm-time">' + timeStr + '</span><span class="wt-tm-phase">' + (isWork ? 'Фокус' : 'Отдых') + '</span></div>'
                    + '</div>'
                    + '<div class="row" style="gap: 10px">'
                    + '<button class="btn btn-primary btn-sm" type="button" data-tm-toggle>' + (timerId ? icon('stop', 13) + 'Пауза' : icon('play', 13) + 'Старт') + '</button>'
                    + '<button class="btn btn-ghost btn-sm" type="button" data-tm-reset>' + icon('refresh', 13) + 'Сброс</button>'
                    + '</div>'
                    + '</div>';

                body.querySelectorAll('[data-tm-set]').forEach(function (b) {
                    b.addEventListener('click', function () {
                        var mins = Number(b.getAttribute('data-tm-set'));
                        duration = mins * 60;
                        remaining = duration;
                        isWork = mins >= 25;
                        if (timerId) { clearInterval(timerId); timerId = null; }
                        render();
                    });
                });

                $('[data-tm-toggle]', body).addEventListener('click', function () {
                    if (timerId) {
                        clearInterval(timerId);
                        timerId = null;
                        render();
                    } else {
                        timerId = setInterval(function () {
                            if (remaining > 0) {
                                remaining--;
                                render();
                            } else {
                                clearInterval(timerId);
                                timerId = null;
                                U.haptic('success');
                                U.toast('Время вышло!', 'ok');
                                render();
                            }
                        }, 1000);
                        render();
                    }
                });

                $('[data-tm-reset]', body).addEventListener('click', function () {
                    if (timerId) { clearInterval(timerId); timerId = null; }
                    remaining = duration;
                    render();
                });
            }

            render();
            return {
                close: function () { if (timerId) clearInterval(timerId); }
            };
        }
    });

    /* 10. ИИ Ассистент на столе */
    registerTool({
        id: 'ai',
        title: 'ИИ Ассистент',
        icon: 'spark',
        w: 0.44,
        h: 0.65,
        mount: function (body, win) {
            var msgs = [
                { role: 'assistant', text: 'Здравствуйте! Я ассистент лаборатории. Чем помочь на рабочем столе?' }
            ];

            function render() {
                body.innerHTML = '<div class="wt-bar">'
                    + '<span class="small faint">ИИ на столе</span>'
                    + '<span class="wt-grow"></span>'
                    + '<button class="wt-btn" type="button" data-ai-pill="Посмотри на наработки и помоги составить план дня">План дня</button>'
                    + '<button class="wt-btn" type="button" data-ai-pill="Предложи чек-лист по задачам">Чек-лист</button>'
                    + '</div>'
                    + '<div class="wt-scroll stack-sm" data-ai-msgs>'
                    + msgs.map(function (m) {
                        return '<div class="msg is-' + m.role + '" style="padding: 10px 14px; border-radius: 12px; font-size: 13.5px; background: ' + (m.role === 'user' ? 'var(--accent-soft)' : 'var(--surface-2)') + '">'
                            + '<b class="tiny faint" style="display: block; margin-bottom: 4px">' + (m.role === 'user' ? 'Вы' : 'Агент UNIT') + '</b>'
                            + '<div class="md" style="line-height: 1.5">' + LAB.md(m.text) + '</div>'
                            + '</div>';
                    }).join('')
                    + '</div>'
                    + '<form class="wt-bar" data-ai-form style="border-top: 1px solid var(--line); border-bottom: 0">'
                    + '<input class="input input-sm grow" data-ai-in placeholder="Задайте вопрос агенту…" required>'
                    + '<button class="btn btn-primary btn-sm btn-icon" type="submit">' + icon('send', 14) + '</button>'
                    + '</form>';

                var scrollBox = $('[data-ai-msgs]', body);
                scrollBox.scrollTop = scrollBox.scrollHeight;

                var form = $('[data-ai-form]', body);
                form.addEventListener('submit', function (e) {
                    e.preventDefault();
                    var inp = $('[data-ai-in]', body);
                    var text = inp.value.trim();
                    if (!text) return;
                    inp.value = '';
                    sendPrompt(text);
                });

                body.querySelectorAll('[data-ai-pill]').forEach(function (b) {
                    b.addEventListener('click', function () {
                        sendPrompt(b.getAttribute('data-ai-pill'));
                    });
                });
            }

            function sendPrompt(q) {
                msgs.push({ role: 'user', text: q });
                msgs.push({ role: 'assistant', text: 'Думаю…' });
                render();

                fetch('/api/ai/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text: q, thread: 'me' })
                }).then(function (r) {
                    if (!r.ok) throw new Error('Ошибка связи');
                    var reader = r.body.getReader();
                    var decoder = new TextDecoder();
                    var buf = '';
                    var last = msgs[msgs.length - 1];
                    last.text = '';

                    function pump() {
                        return reader.read().then(function (res) {
                            if (res.done) { render(); return; }
                            buf += decoder.decode(res.value, { stream: true });
                            var lines = buf.split('\n');
                            buf = lines.pop();
                            lines.forEach(function (line) {
                                if (line.startsWith('data: ')) {
                                    try {
                                        var data = JSON.parse(line.slice(6));
                                        if (data.delta) {
                                            last.text += data.delta;
                                            var scrollBox = $('[data-ai-msgs]', body);
                                            if (scrollBox) {
                                                var lastEl = scrollBox.lastElementChild;
                                                if (lastEl) $('.md', lastEl).innerHTML = LAB.md(last.text);
                                            }
                                        }
                                    } catch (err) {}
                                }
                            });
                            return pump();
                        });
                    }
                    return pump();
                }).catch(function (err) {
                    msgs[msgs.length - 1].text = 'Не удалось получить ответ: ' + err.message;
                    render();
                });
            }

            render();
            return {};
        }
    });

    /* ═══ Верхняя панель: Быстрые действия ═══════════════════════════════════ */
    $('[data-wd-tile]').addEventListener('click', function () {
        var open = wins.filter(function (w) { return !w.min; });
        if (!open.length) return;
        var cols = open.length <= 2 ? open.length : 2;
        var rows = Math.ceil(open.length / cols);
        open.forEach(function (w, i) {
            var c = i % cols;
            var r = Math.floor(i / cols);
            w.max = false;
            w.el.classList.remove('is-max');
            w.x = c / cols;
            w.y = r / rows;
            w.w = 1 / cols;
            w.h = 1 / rows;
            place(w);
            if (w.inst && w.inst.resize) w.inst.resize();
        });
    });

    $('[data-wd-cascade]').addEventListener('click', function () {
        wins.filter(function (w) { return !w.min; }).forEach(function (w, i) {
            w.max = false;
            w.el.classList.remove('is-max');
            w.x = 0.04 + (i % 6) * 0.04;
            w.y = 0.04 + (i % 6) * 0.05;
            w.w = 0.46;
            w.h = 0.62;
            place(w);
            if (w.inst && w.inst.resize) w.inst.resize();
        });
    });

    $('[data-wd-min-all]').addEventListener('click', function () {
        wins.forEach(function (w) { w.minimize(); });
    });

    $('[data-wd-clean]').addEventListener('click', function () {
        if (!wins.length) return;
        if (window.confirm('Закрыть все открытые окна на столе?')) {
            var copy = wins.slice();
            copy.forEach(function (w) { w.close(); });
        }
    });

    projectFilterEl.addEventListener('change', function (e) {
        selectedProject = e.target.value;
        notifyTool('files');
    });

    // Загрузка файлов через <input type="file">
    $('#desk-file-input').addEventListener('change', function (e) {
        var files = e.target.files;
        if (!files || !files.length) return;
        Array.prototype.forEach.call(files, function (file) {
            var data = new FormData();
            data.append('file', file);
            if (selectedProject) data.append('project_id', selectedProject);
            U.toast('Загружаю «' + file.name + '»…');
            setSaving(true);
            LAB.api('/api/desk/upload', data).then(function (res) {
                setSaving(false);
                updateItemInState(res);
                U.toast('Файл загружен', 'ok');
            }).catch(function (err) { setSaving(false, true); fail(err); });
        });
        e.target.value = '';
    });

    // Drag and drop файлов на рабочий стол
    canvas.addEventListener('dragover', function (e) {
        if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') >= 0) {
            e.preventDefault();
            canvas.classList.add('is-dragover');
        }
    });
    canvas.addEventListener('dragleave', function (e) {
        if (!e.relatedTarget || !canvas.contains(e.relatedTarget)) {
            canvas.classList.remove('is-dragover');
        }
    });
    canvas.addEventListener('drop', function (e) {
        if (!e.dataTransfer || !e.dataTransfer.files.length) return;
        e.preventDefault();
        canvas.classList.remove('is-dragover');
        Array.prototype.forEach.call(e.dataTransfer.files, function (file) {
            var data = new FormData();
            data.append('file', file);
            if (selectedProject) data.append('project_id', selectedProject);
            U.toast('Загружаю «' + file.name + '»…');
            setSaving(true);
            LAB.api('/api/desk/upload', data).then(function (res) {
                setSaving(false);
                updateItemInState(res);
                U.toast('Файл добавлен на стол', 'ok');
            }).catch(function (err) { setSaving(false, true); fail(err); });
        });
    });

    /* ── Во весь экран: стол поверх портала (и браузера, если он позволяет) ── */
    function setFull(on) {
        root.classList.toggle('is-full', on);
        fsBtn.setAttribute('aria-pressed', String(on));
        fsBtn.title = on ? 'Вернуть мастерскую в портал' : 'Развернуть мастерскую во весь экран';
        $('[data-wd-fs-ic]', fsBtn).innerHTML = icon(on ? 'shrink' : 'expand', 14);
        $('[data-wd-fs-label]', fsBtn).textContent = on ? 'Обычный вид' : 'Во весь экран';
        wins.forEach(function (w) { if (w.inst && w.inst.resize) w.inst.resize(); });
    }
    fsBtn.addEventListener('click', function () {
        var on = !root.classList.contains('is-full');
        setFull(on);
        try {
            if (on && document.documentElement.requestFullscreen && !document.fullscreenElement) {
                document.documentElement.requestFullscreen().catch(function () {});
            } else if (!on && document.fullscreenElement && document.exitFullscreen) {
                document.exitFullscreen().catch(function () {});
            }
        } catch (err) { /* полноэкранный режим браузера недоступен — хватает развёрнутого стола */ }
    });
    document.addEventListener('fullscreenchange', function () {
        if (!document.fullscreenElement && root.classList.contains('is-full')) setFull(false);
    });
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        if (launchEl && !launchEl.hidden) { openLaunch(false); return; }
        if (root.classList.contains('is-full') && !document.fullscreenElement) setFull(false);
    });
    if (titleEl) titleEl.addEventListener('input', queueLayout);

    /* ── Раскладка: открыть окна, как они стояли (или как их расставил ИИ-агент) ── */
    function applyLayout(layout) {
        booting = true;
        wins.slice().forEach(function (w) { w.close(); });
        var list = ((layout && layout.wins) || []).slice().sort(function (a, b) { return (a.z || 0) - (b.z || 0); });
        list.forEach(function (sv) {
            var item = null;
            if (sv.item_id) {
                item = items.concat(sharedItems).find(function (it) { return it.id === sv.item_id; });
                if (!item) return;                              // наработку удалили — окно не нужно
            }
            var win = openTool(sv.tool, { item: item, title: item ? item.title : undefined,
                                          box: { x: sv.x, y: sv.y, w: sv.w, h: sv.h } });
            if (!win) return;
            if (sv.max && !win.max) win.toggleMax();
            if (sv.min) win.minimize();
        });
        if (layout && layout.title && titleEl) titleEl.value = layout.title;
        lastLayout = JSON.stringify(snapshot());
        booting = false;
        updateHint();
        renderDock();
    }
    // Агент поменял стол (наработки или раскладку) — перечитываем без перезагрузки страницы
    window.addEventListener('lab:desk-changed', function () {
        LAB.api('/api/desk/state').then(function (st) {
            items = st.items || items;
            notifyTool('files');
            applyLayout(st.layout || {});
        }).catch(fail);
    });

    renderDock();
    updateHint();
    var saved = rawData.layout || {};
    if (saved.wins && saved.wins.length) applyLayout(saved);
    else { openTool('files'); booting = false; lastLayout = JSON.stringify(snapshot()); }
})();
