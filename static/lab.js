/* Портал лаборатории: таймер, колокольчик, ИИ-помощник, задачи и канбан,
   рабочий стол, навыки. Общее (листы, вкладки, вибрация) — в unit.js. */
(function () {
    'use strict';

    var U = window.UNIT || { haptic: function () {}, toast: function () {}, openSheet: function () {}, closeSheet: function () {} };
    var $ = function (sel, root) { return (root || document).querySelector(sel); };
    var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

    function api(url, data, method) {
        var opts = { method: method || (data === undefined ? 'GET' : 'POST'), credentials: 'same-origin',
                     headers: { 'Accept': 'application/json' } };
        if (data instanceof FormData) {
            opts.body = data;
        } else if (data !== undefined) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(data);
        }
        return fetch(url, opts).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (body) {
                if (!r.ok) throw new Error(body.error || 'Не получилось — попробуйте ещё раз');
                return body;
            });
        });
    }
    function esc(text) {
        return String(text == null ? '' : text).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }
    function icon(name, size) {
        size = size || 16;
        return '<svg class="ic" width="' + size + '" height="' + size + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
    }
    function fail(error) { U.toast(error.message || String(error), 'err'); U.haptic('error'); }

    /* ── Разметка ответов ИИ: **жирный**, `код`, ```блоки```, списки, ### ── */
    function inline(text) {
        return text.replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
            .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<i>$2</i>')
            .replace(/\[([^\]]+)\]\((\/[^)\s]*|https?:\/\/[^)\s]+)\)/g, '<a href="$2" class="strong" style="color: var(--accent)">$1</a>');
    }
    function md(source) {
        var out = [], list = null, code = null;
        var flush = function () { if (list) { out.push('</' + list + '>'); list = null; } };
        String(source || '').split('\n').forEach(function (raw) {
            if (code !== null) {
                if (/^```/.test(raw.trim())) { out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>'); code = null; }
                else code.push(raw);
                return;
            }
            if (/^```/.test(raw.trim())) { flush(); code = []; return; }
            var line = esc(raw);
            var m;
            if ((m = line.match(/^\s*#{1,4}\s+(.*)$/))) { flush(); out.push('<h4>' + inline(m[1]) + '</h4>'); return; }
            if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
                if (list !== 'ul') { flush(); out.push('<ul>'); list = 'ul'; }
                out.push('<li>' + inline(m[1]) + '</li>');
                return;
            }
            if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
                if (list !== 'ol') { flush(); out.push('<ol>'); list = 'ol'; }
                out.push('<li>' + inline(m[1]) + '</li>');
                return;
            }
            flush();
            if (line.trim()) out.push('<p>' + inline(line) + '</p>');
        });
        if (code !== null) out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
        flush();
        return out.join('');
    }
    window.LAB = { md: md, api: api };
    $$('[data-md]').forEach(function (el) { el.innerHTML = md(el.textContent); el.classList.add('md'); });

    /* ── Таймер в шапке ───────────────────────────────────────────────────── */
    $$('[data-timer]').forEach(function (pill) {
        var start = new Date(pill.getAttribute('data-timer'));
        var text = $('[data-timer-text]', pill);
        var tick = function () {
            var s = Math.max(0, Math.floor((Date.now() - start) / 1000));
            text.textContent = Math.floor(s / 3600) + ':' + ('0' + Math.floor(s % 3600 / 60)).slice(-2) + ':' + ('0' + s % 60).slice(-2);
        };
        tick();
        setInterval(tick, 1000);
    });

    /* ── Колокольчик: новые уведомления без перезагрузки ──────────────────── */
    var bell = $('[data-bell]');
    if (bell) {
        var seen = Number(($('.dot-badge', bell) || {}).textContent || 0);
        setInterval(function () {
            if (document.hidden) return;
            api('/api/notices').then(function (data) {
                var badge = $('.dot-badge', bell);
                if (data.unread && !badge) {
                    badge = document.createElement('span');
                    badge.className = 'dot-badge';
                    bell.appendChild(badge);
                }
                if (badge) {
                    if (data.unread) badge.textContent = data.unread > 99 ? '99+' : data.unread; else badge.remove();
                }
                if (data.unread > seen && data.items && data.items[0]) {
                    U.haptic('success');
                    U.toast(data.items[0].text);
                }
                seen = data.unread;
            }).catch(function () {});
        }, 60000);
    }

    /* ── Вибрация: переключатель в «Ещё» ──────────────────────────────────── */
    $$('[data-haptics-toggle]').forEach(function (box) {
        box.addEventListener('change', function () {
            U.setHaptics(box.checked);
            api('/api/prefs', { haptics: box.checked }).catch(function () {});
        });
    });

    /* ── ИИ-помощник ──────────────────────────────────────────────────────── */
    function pageThread() {
        var holder = $('[data-ai-thread]');
        return holder ? holder.getAttribute('data-ai-thread') : 'me';
    }
    function Chat(box) {
        this.box = box;
        this.log = $('[data-chat-log]', box);
        this.empty = $('[data-chat-empty]', box);
        this.form = $('[data-chat-form]', box);
        this.area = $('textarea', this.form);
        this.thread = box.getAttribute('data-thread') || pageThread();
        this.item = box.getAttribute('data-item') || '';
        this.busy = false;
        this.loaded = false;
        this.abortController = null;
        var self = this;
        this.form.addEventListener('submit', function (event) {
            event.preventDefault();
            self.send(self.area.value);
        });
        this.area.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && window.matchMedia('(pointer: fine)').matches) {
                event.preventDefault();
                self.send(self.area.value);
            }
        });
        box.addEventListener('click', function (event) {
            var chip = event.target.closest('[data-ask]');
            if (chip) { self.send(chip.getAttribute('data-ask')); return; }
            var stopBtn = event.target.closest('[data-chat-stop]');
            if (stopBtn) { self.stop(); return; }
            var apply = event.target.closest('[data-apply]');
            if (apply) self.apply(apply);
            var copyBtn = event.target.closest('[data-msg-copy]');
            if (copyBtn) {
                var textToCopy = (copyBtn.closest('.bubble').querySelector('.md') || {}).innerText || '';
                navigator.clipboard.writeText(textToCopy).then(function () {
                    U.toast('Скопировано', 'ok');
                    U.haptic('tap');
                });
                return;
            }
            var clear = event.target.closest('[data-chat-clear]');
            if (clear && window.confirm('Очистить разговор?')) {
                api('/api/ai/clear', { thread: self.thread }).then(function () {
                    $$('.msg', self.log).forEach(function (m) { m.remove(); });
                    if (self.empty) self.empty.hidden = false;
                });
            }
        });
    }
    var TOOL_NAMES = {
        task: 'Создание задачи',
        task_edit: 'Изменение задачи',
        update_task: 'Изменение задачи',
        move: 'Перемещение задачи',
        part: 'Часть проекта',
        journal: 'Запись в журнал',
        note: 'Заметка на стол',
        checklist: 'Чек-лист на стол',
        checklist_item: 'Пункт чек-листа',
        comment: 'Комментарий к задаче',
        code: 'Код на стол',
        link: 'Ссылка на стол'
    };
    Chat.prototype.setControls = function (generating) {
        var sendBtn = $('.ci-send', this.form);
        var stopBtn = $('.ci-stop', this.form) || $('[data-chat-stop]', this.form);
        if (stopBtn) stopBtn.hidden = !generating;
        if (sendBtn) {
            sendBtn.hidden = false;
            if (generating) {
                sendBtn.classList.add('is-queued-mode');
                sendBtn.title = 'Докинуть задание в процесс';
                sendBtn.setAttribute('aria-label', 'Докинуть задание в процесс');
            } else {
                sendBtn.classList.remove('is-queued-mode');
                sendBtn.title = 'Отправить';
                sendBtn.setAttribute('aria-label', 'Отправить');
            }
        }
    };
    Chat.prototype.updateQueueBadge = function () {
        var bar = $('.chat-queue-chip', this.box);
        var count = (this.queue || []).length;
        if (count > 0) {
            if (!bar) {
                bar = document.createElement('div');
                bar.className = 'chat-queue-chip';
                this.form.parentNode.insertBefore(bar, this.form);
            }
            bar.innerHTML = icon('spark', 13) + '<span>В очереди к текущей задаче: <b>' + count + '</b></span>';
            bar.hidden = false;
        } else if (bar) {
            bar.remove();
        }
    };
    Chat.prototype.renderQueued = function (text) {
        if (this.empty) this.empty.hidden = true;
        var el = document.createElement('div');
        el.className = 'msg user is-queued';
        el.innerHTML = '<div><div class="bubble"><span class="chip-queue">В очереди</span>' + esc(text) + '</div></div>';
        this.log.appendChild(el);
        return el;
    };
    Chat.prototype.stop = function () {
        if (this.abortController) {
            this.abortController.abort();
            this.abortController = null;
        }
        this.busy = false;
        this.queue = [];
        this.updateQueueBadge();
        this.setControls(false);
    };
    Chat.prototype.load = function () {
        var self = this;
        if (this.loaded) return Promise.resolve();
        this.loaded = true;
        return api('/api/ai/history?thread=' + encodeURIComponent(this.thread)).then(function (data) {
            self.thread = data.thread;
            data.messages.forEach(function (m) { self.render(m); });
            self.scroll();
        }).catch(fail);
    };
    Chat.prototype.scroll = function () { this.log.scrollTop = this.log.scrollHeight; };
    Chat.prototype.render = function (m, holder) {
        if (this.empty) this.empty.hidden = true;
        var el = holder || document.createElement('div');
        el.className = 'msg ' + (m.role === 'user' ? 'user' : 'ai');
        el.setAttribute('data-id', m.id || '');
        if (m.role === 'user') {
            el.innerHTML = '<div><div class="bubble">' + esc(m.text) + '</div></div>';
        } else {
            var acts = (m.actions || []).map(function (a, i) {
                var state = a.applied ? ' is-done' : a.error ? ' is-bad' : '';
                return '<div class="ai-act' + state + '"><span class="grow"><span>' + esc(a.label) + '</span><b>' + esc(a.title) + '</b>'
                    + (a.meta || a.error ? '<small>' + esc(a.error || a.meta) + '</small>' : '') + '</span>'
                    + (a.applied ? '<span class="chip tone s-active">' + icon('check', 13) + 'Готово</span>'
                        : '<button class="btn btn-accent btn-sm" type="button" data-apply="' + i + '" data-msg="' + m.id + '">Применить</button>')
                    + '</div>';
            }).join('');
            var many = (m.actions || []).filter(function (a) { return !a.applied; }).length > 1;
            var cleanText = (m.text || '')
                .replace(/```(?:actions|json|tool_calls|tools)?\s*\[\s*\{[\s\S]*?\]\s*```/gi, '')
                .replace(/```actions[\s\S]*?```/gi, '')
                .replace(/\[\s*\{\s*"type"[\s\S]*$/gi, '')
                .trim();
            el.innerHTML = '<span class="ai-mark" style="width: 26px; height: 26px; border-radius: 8px">' + icon('spark', 14) + '</span>'
                + '<div class="bubble"><div class="md">' + md(cleanText) + '</div>'
                + (acts ? '<div class="ai-acts"><div class="ai-acts-head"><span>' + icon('spark', 13) + 'Инструменты агента</span>'
                    + (many ? '<button class="btn btn-ghost btn-xs" type="button" data-apply="all" data-msg="' + m.id + '">Применить всё</button>' : '') + '</div>' + acts + '</div>' : '')
                + '<div class="msg-meta-row">'
                + (m.at ? '<span class="when">' + esc(m.at) + '</span>' : '')
                + '<button class="btn btn-quiet btn-xs msg-copy-btn" type="button" data-msg-copy title="Скопировать ответ">' + icon('copy', 13) + '<span>Скопировать</span></button>'
                + '</div></div>';
        }
        if (!holder) this.log.appendChild(el);
        return el;
    };
    Chat.prototype.send = function (text) {
        var self = this;
        text = (text || '').trim();
        if (!text) return;
        if (this.busy) {
            // Докинуть задание в процессе выполнения основной задачи
            this.queue = this.queue || [];
            this.queue.push(text);
            this.renderQueued(text);
            this.area.value = '';
            if (U.autosize) U.autosize(this.area);
            this.scroll();
            this.updateQueueBadge();
            U.haptic('tap');
            return;
        }
        this.busy = true;
        this.setControls(true);
        this.abortController = new AbortController();
        this.area.value = '';
        if (U.autosize) U.autosize(this.area);
        this.render({ role: 'user', text: text });
        var answer = this.render({ role: 'assistant', text: '' });
        var bubble = $('.bubble', answer);
        bubble.classList.add('typing');
        this.scroll();
        U.haptic('tap');
        var acc = '';
        fetch('/api/ai/chat', { method: 'POST', credentials: 'same-origin',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ text: text, thread: this.thread, item_id: this.item }),
                                signal: this.abortController.signal })
            .then(function (r) {
                if (!r.ok) {
                    return r.text().then(function (raw) {
                        var msg = 'ИИ временно недоступен (' + r.status + ')';
                        try {
                            var b = JSON.parse(raw);
                            if (b && b.error) msg = b.error;
                        } catch (e) {
                            if (r.status === 502 || r.status === 504) msg = 'Шлюз модели не отвечает — попробуйте через минуту';
                            else if (r.status === 403) msg = 'Доступ к ИИ ограничен';
                            else if (r.status === 429) msg = 'Лимит запросов к ИИ на сегодня исчерпан';
                        }
                        throw new Error(msg);
                    });
                }
                var reader = r.body.getReader(), decoder = new TextDecoder(), buffer = '';
                var pump = function () {
                    return reader.read().then(function (step) {
                        if (step.done) return;
                        buffer += decoder.decode(step.value, { stream: true });
                        var chunks = buffer.split('\n\n');
                        buffer = chunks.pop();
                        chunks.forEach(function (chunk) {
                            var line = chunk.replace(/^data:\s*/, '');
                            if (!line) return;
                            var event;
                            try { event = JSON.parse(line); } catch (e) { return; }
                            if (event.delta) {
                                acc += event.delta;
                                // Прячем любой хвост действий из markdown
                                var cleanText = acc.replace(/```(?:actions|json|tool_calls|tools)?[\s\S]*$/, '')
                                                   .replace(/\[\s*\{\s*"type"[\s\S]*$/, '');
                                var toolMatch = acc.match(/\{\s*"type"\s*:\s*"([a-zA-Z_]+)"(?:[^{}]*?"title"\s*:\s*"([^"]+)")?/);
                                var toolBar = '';
                                if (toolMatch) {
                                    var tLabel = TOOL_NAMES[toolMatch[1]] || toolMatch[1];
                                    toolBar = '<div class="ai-live-tool"><span class="live-dot"></span><span>Используется инструмент: <b>' + esc(tLabel) + '</b>' + (toolMatch[2] ? ' «' + esc(toolMatch[2]) + '»' : '') + '</span></div>';
                                } else if (/```(?:actions|json|tool_calls|tools)|\[\s*\{\s*"type"/i.test(acc)) {
                                    toolBar = '<div class="ai-live-tool"><span class="live-dot"></span><span>Подготовка инструментов…</span></div>';
                                }
                                $('.md', bubble).innerHTML = md(cleanText.trim()) + toolBar;
                                self.scroll();
                            }
                            if (event.error) throw new Error(event.error);
                            if (event.done) {
                                self.render(event.message, answer);
                                U.haptic((event.message.actions || []).length ? 'success' : 'tap');
                                self.scroll();
                            }
                        });
                        return pump();
                    });
                };
                return pump();
            })
            .catch(function (error) {
                if (error.name === 'AbortError') {
                    var cur = $('.md', bubble).innerHTML;
                    $('.md', bubble).innerHTML = cur + '<p class="tiny faint" style="margin-top: 6px"><em>(ответ прерван)</em></p>';
                } else {
                    $('.md', bubble).innerHTML = '<div class="ai-error-box"><p>' + esc(error.message) + '</p></div>';
                    U.haptic('error');
                }
            })
            .then(function () {
                bubble.classList.remove('typing');
                self.busy = false;
                self.setControls(false);
                if (self.queue && self.queue.length > 0) {
                    var nextTask = self.queue.shift();
                    self.updateQueueBadge();
                    setTimeout(function () {
                        self.send(nextTask);
                    }, 200);
                }
            });
    };
    Chat.prototype.apply = function (button) {
        var self = this;
        var id = Number(button.getAttribute('data-msg'));
        var which = button.getAttribute('data-apply');
        var indexes = which === 'all'
            ? $$('[data-apply]:not([data-apply="all"])', button.closest('.ai-acts')).map(function (b) { return Number(b.getAttribute('data-apply')); })
            : [Number(which)];
        button.disabled = true;
        var chain = Promise.resolve(), done = 0;
        indexes.forEach(function (index) {
            chain = chain.then(function () {
                return api('/api/ai/apply', { message_id: id, index: index }).then(function (result) {
                    done += 1;
                    var b = $('[data-apply="' + index + '"][data-msg="' + id + '"]', self.log);
                    if (b) {
                        var card = b.closest('.ai-act');
                        card.classList.add('is-done');
                        b.outerHTML = result.link ? '<a class="chip tone s-active" href="' + esc(result.link) + '">' + icon('check', 13) + esc(result.text) + '</a>'
                            : '<span class="chip tone s-active">' + icon('check', 13) + 'Готово</span>';
                    }
                }).catch(function (error) {
                    var b = $('[data-apply="' + index + '"][data-msg="' + id + '"]', self.log);
                    if (b) { b.disabled = false; b.closest('.ai-act').classList.add('is-bad'); }
                    fail(error);
                });
            });
        });
        chain.then(function () {
            if (which === 'all') button.remove();
            if (done) { U.haptic('success'); U.toast(done > 1 ? 'Применено: ' + done : 'Применено'); }
        });
    };

    // Глобальные обработчики для создания и удаления чатов
    document.addEventListener('click', function (e) {
        var newChatBtn = e.target.closest('[data-chat-new]');
        if (newChatBtn) {
            newChatBtn.disabled = true;
            api('/api/ai/chats', {}).then(function (res) {
                location.href = '/ai?chat=' + encodeURIComponent(res.id);
            }).catch(function (err) {
                newChatBtn.disabled = false;
                fail(err);
            });
            return;
        }
        var delChatBtn = e.target.closest('[data-chat-del]');
        if (delChatBtn) {
            var cid = delChatBtn.getAttribute('data-chat-del');
            if (window.confirm('Удалить этот разговор?')) {
                delChatBtn.disabled = true;
                api('/api/ai/chats/' + encodeURIComponent(cid) + '/delete', {}).then(function () {
                    U.toast('Чат удалён', 'ok');
                    var item = delChatBtn.closest('.thread-item');
                    if (item) item.remove();
                    if (new URLSearchParams(location.search).get('chat') === cid) {
                        location.href = '/ai';
                    } else {
                        location.reload();
                    }
                }).catch(function (err) {
                    delChatBtn.disabled = false;
                    fail(err);
                });
            }
            return;
        }
    });
    var chats = $$('[data-chat]').map(function (box) { return new Chat(box); });
    var pageChat = chats.filter(function (c) { return !c.box.closest('dialog'); })[0];
    if (pageChat) {
        pageChat.load().then(function () {
            var ask = pageChat.box.getAttribute('data-ask');
            if (ask) pageChat.send(ask);
        });
    }
    var sheetChat = chats.filter(function (c) { return c.box.closest('#ai-sheet'); })[0];
    document.addEventListener('click', function (event) {
        var opener = event.target.closest('[data-ai-open]');
        if (!opener || !sheetChat) return;
        event.preventDefault();
        var thread = opener.getAttribute('data-thread') || pageThread();
        if (thread !== sheetChat.thread) {
            sheetChat.thread = thread;
            sheetChat.loaded = false;
            $$('.msg', sheetChat.log).forEach(function (m) { m.remove(); });
            if (sheetChat.empty) sheetChat.empty.hidden = false;
        }
        sheetChat.item = opener.getAttribute('data-item') || '';
        U.openSheet('ai-sheet');
        sheetChat.load().then(function () {
            var ask = opener.getAttribute('data-ask');
            if (ask) sheetChat.send(ask);
            else if (window.matchMedia('(pointer: fine)').matches) sheetChat.area.focus();
        });
    });

    /* ── Сводки ИИ (дня и «Пульса») ───────────────────────────────────────── */
    $$('[data-brief]').forEach(function (button) {
        button.addEventListener('click', function () {
            var target = $(button.getAttribute('data-target'));
            button.disabled = true;
            var was = button.innerHTML;
            button.innerHTML = '<span class="live-dot"></span>Думаю…';
            api('/api/ai/brief', { scope: button.getAttribute('data-brief') }).then(function (data) {
                target.innerHTML = md(data.text);
                target.classList.add('md');
                target.hidden = false;
                U.haptic('success');
            }).catch(fail).then(function () {
                button.disabled = false;
                button.innerHTML = was;
            });
        });
    });

    /* ── Задачи: отметить сделанной одним касанием ────────────────────────── */
    document.addEventListener('click', function (event) {
        var box = event.target.closest('[data-task-done]');
        if (!box) return;
        var row = box.closest('.task-row');
        var done = !box.classList.contains('done');
        box.classList.toggle('done', done);
        if (row) row.classList.toggle('is-done', done);
        U.haptic(done ? 'success' : 'tap');
        api('/api/task/' + box.getAttribute('data-task-done') + '/move', { column: done ? 'done' : (box.getAttribute('data-was') || 'doing') })
            .then(function () { if (done) U.toast('Задача закрыта'); })
            .catch(function (error) { box.classList.toggle('done', !done); fail(error); });
    });

    /* ── Канбан: перетаскивание, быстрый перенос, лист задачи ─────────────── */
    var board = $('[data-board]');
    if (board) {
        var drag = null;
        var colAt = function (x, y) {
            var el = document.elementFromPoint(x, y);
            return el && el.closest('[data-col]');
        };
        var counts = function () {
            $$('[data-col]', board).forEach(function (col) {
                var c = $('[data-col-count]', col);
                if (c) c.textContent = $$('.task-card', col).length;
            });
        };
        board.addEventListener('pointerdown', function (event) {
            var card = event.target.closest('.task-card');
            if (!card || event.button > 0) return;
            drag = { card: card, x: event.clientX, y: event.clientY, id: event.pointerId, live: false, touch: event.pointerType !== 'mouse' };
            drag.timer = setTimeout(function () { if (drag && drag.touch) start(); }, 320);
        });
        var start = function () {
            if (!drag || drag.live) return;
            drag.live = true;
            var r = drag.card.getBoundingClientRect();
            drag.ghost = drag.card.cloneNode(true);
            drag.ghost.classList.add('task-ghost');
            drag.ghost.style.width = r.width + 'px';
            drag.dx = drag.x - r.left;
            drag.dy = drag.y - r.top;
            document.body.appendChild(drag.ghost);
            drag.card.classList.add('dragging');
            move(drag.x, drag.y);
            U.haptic('lift');
        };
        var move = function (x, y) {
            drag.ghost.style.left = (x - drag.dx) + 'px';
            drag.ghost.style.top = (y - drag.dy) + 'px';
            var col = colAt(x, y);
            $$('[data-col]', board).forEach(function (c) { c.classList.toggle('drop-on', c === col); });
            if (col !== drag.over && col) U.haptic('select');
            drag.over = col;
        };
        document.addEventListener('pointermove', function (event) {
            if (!drag) return;
            var far = Math.abs(event.clientX - drag.x) + Math.abs(event.clientY - drag.y) > 8;
            if (!drag.live) {
                if (far && drag.touch) { clearTimeout(drag.timer); drag = null; return; }   // это прокрутка
                if (far && !drag.touch) start(); else return;
            }
            event.preventDefault();
            move(event.clientX, event.clientY);
        }, { passive: false });
        var finish = function (event) {
            if (!drag) return;
            clearTimeout(drag.timer);
            var d = drag;
            drag = null;
            if (!d.live) return;                                   // простое касание — откроется лист
            d.ghost.remove();
            d.card.classList.remove('dragging');
            $$('[data-col]', board).forEach(function (c) { c.classList.remove('drop-on'); });
            d.card.setAttribute('data-dragged', '1');
            setTimeout(function () { d.card.removeAttribute('data-dragged'); }, 50);
            var col = event.type === 'pointerup' ? colAt(event.clientX, event.clientY) : null;
            if (!col || col === d.card.closest('[data-col]')) return;
            var list = $('[data-col-list]', col);
            list.insertBefore(d.card, list.firstChild);
            counts();
            U.haptic('drop');
            api('/api/task/' + d.card.getAttribute('data-task') + '/move', { column: col.getAttribute('data-col') })
                .then(function () { if (col.getAttribute('data-col') === 'done') { U.haptic('success'); U.toast('Готово!'); } })
                .catch(function (error) { fail(error); location.reload(); });
        };
        document.addEventListener('pointerup', finish);
        document.addEventListener('pointercancel', finish);
        board.addEventListener('contextmenu', function (event) { if (event.target.closest('.task-card')) event.preventDefault(); });
    }

    var taskSheet = $('#task-sheet');
    function openTask(id) {
        if (!taskSheet) return;
        api('/api/task/' + id).then(function (t) {
            var form = $('form[data-task-form]', taskSheet);
            form.reset();
            $('[name=task_id]', form).value = t.id;
            ['title', 'about', 'column', 'phase', 'part_id', 'assignee_id', 'priority', 'estimate', 'due'].forEach(function (k) {
                var f = form.elements[k];
                if (f) f.value = t[k] == null ? '' : t[k];
            });
            $('[data-task-created]', taskSheet).textContent = 'Создана ' + t.created;
            taskSheet.setAttribute('data-id', t.id);
            var timerTask = $('[data-timer-task]', taskSheet);
            if (timerTask) timerTask.value = t.id;
            var ai = $('[data-task-ai]', taskSheet);
            if (ai) ai.setAttribute('data-ask', 'Разбей задачу [задача ' + t.id + '] «' + t.title + '» на конкретные шаги и предложи их чек-листом или подзадачами.');
            renderChecklist(t.checklist);
            renderComments(t.comments);
            document.dispatchEvent(new CustomEvent('task:loaded', { detail: t }));
            U.openSheet(taskSheet);
            history.replaceState(null, '', location.pathname + '?task=' + t.id + location.hash);
        }).catch(fail);
    }
    function renderChecklist(items) {
        var box = $('[data-checklist]', taskSheet);
        var done = items.filter(function (i) { return i.done; }).length;
        $('[data-check-count]', taskSheet).textContent = items.length ? done + ' из ' + items.length : '';
        box.innerHTML = items.map(function (item, i) {
            return '<label class="check-item' + (item.done ? ' on' : '') + '"><input type="checkbox" data-check="' + i + '"' + (item.done ? ' checked' : '') + '>'
                + '<input type="text" value="' + esc(item.text) + '" readonly tabindex="-1">'
                + '<button class="icon-btn x" type="button" data-uncheck="' + i + '" aria-label="Убрать">' + icon('x', 14) + '</button></label>';
        }).join('');
    }
    function renderComments(list) {
        var box = $('[data-comments]', taskSheet);
        box.innerHTML = list.length ? list.map(function (c) {
            return '<div class="comment"><span class="avatar sm">' + esc(c.author.split(' ').map(function (p) { return p[0]; }).join('').slice(0, 2)) + '</span>'
                + '<div class="bubble"><small>' + esc(c.author) + ' · ' + esc(c.at) + '</small>' + esc(c.text) + '</div></div>';
        }).join('') : '<p class="small faint">Комментариев пока нет.</p>';
    }
    if (taskSheet) {
        var tid = function () { return taskSheet.getAttribute('data-id'); };
        taskSheet.addEventListener('change', function (event) {
            var box = event.target.closest('[data-check]');
            if (!box) return;
            api('/api/task/' + tid() + '/check', { index: Number(box.getAttribute('data-check')), done: box.checked })
                .then(function (r) { renderChecklist(r.checklist); if (box.checked) U.haptic('success'); }).catch(fail);
        });
        taskSheet.addEventListener('click', function (event) {
            var rm = event.target.closest('[data-uncheck]');
            if (rm) {
                event.preventDefault();
                api('/api/task/' + tid() + '/check', { index: Number(rm.getAttribute('data-uncheck')), remove: true })
                    .then(function (r) { renderChecklist(r.checklist); }).catch(fail);
            }
        });
        var addCheck = $('[data-check-add]', taskSheet);
        addCheck.addEventListener('keydown', function (event) {
            if (event.key !== 'Enter' || !addCheck.value.trim()) return;
            event.preventDefault();
            api('/api/task/' + tid() + '/check', { add: addCheck.value }).then(function (r) {
                addCheck.value = '';
                renderChecklist(r.checklist);
                U.haptic('select');
            }).catch(fail);
        });
        var commentForm = $('[data-comment-form]', taskSheet);
        commentForm.addEventListener('submit', function (event) {
            event.preventDefault();
            var area = $('textarea', commentForm);
            if (!area.value.trim()) return;
            api('/api/task/' + tid() + '/comment', { text: area.value }).then(function (t) {
                area.value = '';
                renderComments(t.comments);
                U.haptic('success');
            }).catch(fail);
        });
        taskSheet.addEventListener('close', function () {
            history.replaceState(null, '', location.pathname + location.hash);
        });
        document.addEventListener('click', function (event) {
            var card = event.target.closest('[data-open-task]');
            if (!card || card.getAttribute('data-dragged')) return;
            event.preventDefault();
            openTask(card.getAttribute('data-open-task'));
        });
        var wanted = new URLSearchParams(location.search).get('task');
        if (wanted) openTask(wanted);
    }
    // Новая задача прямо в колонке
    $$('[data-col-add]').forEach(function (form) {
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            var btn = form.querySelector('button[type="submit"]');
            if (btn && btn.disabled) return;
            if (btn) btn.disabled = true;
            var data = new FormData(form);
            fetch(form.action, { method: 'POST', body: data, credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
                .then(function (r) { if (!r.ok) throw new Error('Не получилось'); U.haptic('success'); location.reload(); })
                .catch(function (err) { if (btn) btn.disabled = false; fail(err); });
        });
    });
    // Вид задач: доска или список
    $$('[data-view]').forEach(function (button) {
        button.addEventListener('click', function () {
            var view = button.getAttribute('data-view');
            $$('[data-view]').forEach(function (b) { b.classList.toggle('on', b === button); });
            $$('[data-view-pane]').forEach(function (p) { p.hidden = p.getAttribute('data-view-pane') !== view; });
            api('/api/prefs', { board_view: view }).catch(function () {});
        });
    });

    /* ── Навыки: чипы с уровнем ───────────────────────────────────────────── */
    $$('[data-skills]').forEach(function (box) {
        var input = $('[data-skill-input]', box);
        var chip = function (name, level) {
            var el = document.createElement('span');
            el.className = 'skill-chip';
            el.innerHTML = '<span>' + esc(name) + '</span><span class="lv-dots">'
                + [1, 2, 3, 4, 5].map(function (n) { return '<button type="button" data-lv="' + n + '" aria-label="Уровень ' + n + '"></button>'; }).join('')
                + '</span><button class="x" type="button" aria-label="Убрать">' + icon('x', 13) + '</button>'
                + '<input type="hidden" name="skill" value="">';
            box.insertBefore(el, input);
            set(el, name, level);
            return el;
        };
        var set = function (el, name, level) {
            $$('[data-lv]', el).forEach(function (b) { b.classList.toggle('on', Number(b.getAttribute('data-lv')) <= level); });
            $('input', el).value = name + '|' + level;
            el.setAttribute('data-name', name);
        };
        $$('[data-skill]', box).forEach(function (s) {
            chip(s.getAttribute('data-skill'), Number(s.getAttribute('data-level')) || 3);
            s.remove();
        });
        box.addEventListener('click', function (event) {
            var lv = event.target.closest('[data-lv]');
            var el = event.target.closest('.skill-chip');
            if (lv && el) { set(el, el.getAttribute('data-name'), Number(lv.getAttribute('data-lv'))); U.haptic('select'); }
            if (event.target.closest('.x') && el) { el.remove(); U.haptic('tap'); }
        });
        input.addEventListener('keydown', function (event) {
            if ((event.key === 'Enter' || event.key === ',') && input.value.trim()) {
                event.preventDefault();
                chip(input.value.trim().slice(0, 60), 3);
                input.value = '';
                U.haptic('select');
            }
        });
        input.form.addEventListener('submit', function () {
            if (input.value.trim()) chip(input.value.trim().slice(0, 60), 3);
        });
    });
})();

/* ── Мелочи интерфейса: заполнение листов, фильтры, быстрые значения ─────── */
(function () {
    'use strict';
    var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
    // Кнопка с data-fill заполняет форму листа перед открытием (data-fill — JSON)
    document.addEventListener('click', function (event) {
        var opener = event.target.closest && event.target.closest('[data-sheet-open][data-fill]');
        if (!opener) return;
        var sheet = document.getElementById(opener.getAttribute('data-sheet-open'));
        var data;
        try { data = JSON.parse(opener.getAttribute('data-fill')); } catch (e) { return; }
        if (!sheet) return;
        Object.keys(data).forEach(function (key) {
            if (key.charAt(0) === '_') {
                $$('[data-fill-text="' + key + '"]', sheet).forEach(function (el) { el.textContent = data[key]; });
                return;
            }
            $$('[name="' + key + '"]', sheet).forEach(function (field) {
                var value = data[key];
                if (field.type === 'checkbox') field.checked = Array.isArray(value) ? value.indexOf(field.value) >= 0 : !!value;
                else field.value = value == null ? '' : value;
                field.dispatchEvent(new Event('input', { bubbles: true }));
            });
        });
    }, true);
    // Ползунок, связанный с числовым полем
    $$('[data-range-for]').forEach(function (range) {
        var field = range.form ? range.form.elements[range.getAttribute('data-range-for')]
            : range.closest('form, .sheet-body').querySelector('[name="' + range.getAttribute('data-range-for') + '"]');
        if (!field) return;
        var last = -1;
        range.addEventListener('input', function () {
            field.value = range.value;
            if (Math.abs(range.value - last) >= 10 || range.value == 0 || range.value == 100) { window.UNIT.haptic('select'); last = range.value; }
        });
        field.addEventListener('input', function () { range.value = field.value || 0; });
    });
    // Поиск по карточкам на странице
    $$('[data-filter-input]').forEach(function (input) {
        input.addEventListener('input', function () {
            var q = input.value.trim().toLowerCase();
            $$(input.getAttribute('data-filter-input')).forEach(function (el) {
                el.hidden = q && (el.getAttribute('data-text') || el.textContent.toLowerCase()).indexOf(q) < 0;
            });
        });
    });
    // Быстрые значения: кнопка data-set="поле" data-value="…"
    document.addEventListener('click', function (event) {
        var b = event.target.closest && event.target.closest('[data-set]');
        if (!b) return;
        var form = b.closest('form') || b.closest('.sheet-body');
        var field = form && form.querySelector('[name="' + b.getAttribute('data-set') + '"]');
        if (field) { field.value = b.getAttribute('data-value'); window.UNIT.haptic('select'); }
    });
    // Очистить разговор с ИИ (кнопка в шапке страницы)
    document.addEventListener('click', function (event) {
        var b = event.target.closest && event.target.closest('[data-chat-clear]');
        if (!b || b.closest('[data-chat]')) return;
        var box = document.querySelector('.chat-box [data-chat]');
        if (!box || !window.confirm('Очистить разговор?')) return;
        window.LAB.api('/api/ai/clear', { thread: box.getAttribute('data-thread') || 'me' }).then(function () { location.reload(); });
    });
})();

/* Страницы внутри окна: мастер нового проекта и настройки проекта.
   [data-pages] — контейнер; внутри [data-page] (data-title — подпись, data-optional — можно пропустить),
   кнопки [data-page-next], [data-page-prev], [data-page-skip]; [data-pages-nav] — точки или вкладки,
   [data-pages-bar] — полоса прогресса. Переход вперёд проверяет обязательные поля страницы. */
(function () {
    'use strict';
    var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    function setup(box) {
        var pages = $$('[data-page]', box), nav = box.querySelector('[data-pages-nav]'), bar = box.querySelector('[data-pages-bar]');
        var at = 0, tabs = box.hasAttribute('data-pages-tabs');
        if (!pages.length) return;
        if (nav) {
            nav.innerHTML = pages.map(function (p, i) {
                return tabs ? '<button type="button" class="tab-btn" data-go="' + i + '">' + (p.getAttribute('data-title') || i + 1) + '</button>'
                            : '<i data-go="' + i + '" title="' + (p.getAttribute('data-title') || '') + '"></i>';
            }).join('');
        }
        function show(next, dir) {
            next = Math.max(0, Math.min(pages.length - 1, next));
            var from = pages[at], to = pages[next];
            if (from !== to) {
                from.hidden = true;
                to.hidden = false;
                if (!reduce) {
                    to.classList.remove('pg-in-left', 'pg-in-right');
                    void to.offsetWidth;
                    to.classList.add(dir < 0 ? 'pg-in-left' : 'pg-in-right');
                }
            } else {
                pages.forEach(function (p, i) { p.hidden = i !== next; });
            }
            at = next;
            box.setAttribute('data-at', String(at));
            if (nav) $$('[data-go]', nav).forEach(function (el, i) { el.classList.toggle('on', i === at); el.classList.toggle('done', i < at); });
            if (bar) bar.style.width = Math.round((at + 1) / pages.length * 100) + '%';
            $$('[data-page-prev]', box).forEach(function (b) { b.hidden = at === 0; });
            $$('[data-page-next]', box).forEach(function (b) { b.hidden = at === pages.length - 1; });
            $$('[data-page-done]', box).forEach(function (b) { b.hidden = !tabs && at !== pages.length - 1; });
            $$('[data-page-skip]', box).forEach(function (b) { b.hidden = !to.hasAttribute('data-optional'); });
            var focus = to.querySelector('[autofocus], input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea');
            if (focus && !tabs && window.matchMedia('(pointer: fine)').matches) setTimeout(function () { focus.focus(); }, reduce ? 0 : 180);
            box.dispatchEvent(new CustomEvent('pages:show', { detail: { at: at, page: to } }));
        }
        function valid() {
            var bad = $$('input, select, textarea', pages[at]).filter(function (el) { return !el.checkValidity(); });
            if (bad.length) { bad[0].reportValidity(); if (window.UNIT) window.UNIT.haptic('error'); return false; }
            return true;
        }
        box.addEventListener('click', function (event) {
            var t = event.target.closest('[data-page-next], [data-page-prev], [data-page-skip], [data-go]');
            if (!t) return;
            event.preventDefault();
            if (t.hasAttribute('data-page-prev')) show(at - 1, -1);
            else if (t.hasAttribute('data-page-skip')) show(at + 1, 1);
            else if (t.hasAttribute('data-go')) { var to = Number(t.getAttribute('data-go')); if (tabs || to <= at || valid()) show(to, to < at ? -1 : 1); }
            else if (valid()) show(at + 1, 1);
            if (window.UNIT) window.UNIT.haptic('select');
        });
        box.addEventListener('keydown', function (event) {
            if (event.key !== 'Enter' || tabs || event.target.tagName === 'TEXTAREA' || event.target.type === 'submit') return;
            if (at < pages.length - 1) { event.preventDefault(); if (valid()) show(at + 1, 1); }
        });
        // Свайп по странице на телефоне — листать
        var sx = 0, sy = 0;
        box.addEventListener('touchstart', function (e) { sx = e.touches[0].clientX; sy = e.touches[0].clientY; }, { passive: true });
        box.addEventListener('touchend', function (e) {
            var dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
            if (Math.abs(dx) < 70 || Math.abs(dy) > Math.abs(dx) * 0.7 || e.target.closest('input, textarea, select, .icon-pick')) return;
            if (dx < 0 && at < pages.length - 1 && (tabs || valid())) show(at + 1, 1);
            else if (dx > 0 && at > 0) show(at - 1, -1);
        }, { passive: true });
        var sheet = box.closest('dialog');
        if (sheet) sheet.addEventListener('close', function () { if (!tabs) setTimeout(function () { show(0, -1); }, 300); });
        show(0, 1);
        box.pagesShow = show;
    }
    $$('[data-pages]').forEach(setup);
})();

/* Меню «⋯» у карточек (проекты): открыть/закрыть, клик мимо — закрыть */
(function () {
    'use strict';
    document.addEventListener('click', function (event) {
        var toggle = event.target.closest('[data-menu-toggle]');
        document.querySelectorAll('.card-menu[open]').forEach(function (m) { if (!toggle || m !== toggle.closest('.card-menu')) m.removeAttribute('open'); });
        if (toggle) { event.preventDefault(); event.stopPropagation(); var m = toggle.closest('.card-menu'); if (m.hasAttribute('open')) m.removeAttribute('open'); else m.setAttribute('open', ''); }
    });
})();

/* Подсказка-тег дописывает строку в поле (data-append="поле" data-value="…") */
(function () {
    'use strict';
    document.addEventListener('click', function (event) {
        var b = event.target.closest('[data-append]');
        if (!b) return;
        var form = b.closest('form');
        var field = form && form.querySelector('[name="' + b.getAttribute('data-append') + '"]');
        if (!field) return;
        var lines = field.value.split('\n').map(function (x) { return x.trim(); }).filter(Boolean);
        if (lines.indexOf(b.getAttribute('data-value')) < 0) lines.push(b.getAttribute('data-value'));
        field.value = lines.join('\n');
        field.dispatchEvent(new Event('input'));
        b.classList.add('used');
        if (window.UNIT) window.UNIT.haptic('select');
    });
})();

/* Проект: #settings в адресе открывает настройки; в окне задачи — кнопка «Удалить», если можно */
(function () {
    'use strict';
    if (location.hash === '#settings' && document.getElementById('settings-sheet') && window.UNIT) {
        setTimeout(function () { window.UNIT.openSheet('settings-sheet'); }, 80);
    }
    document.addEventListener('task:loaded', function (event) {
        var b = document.querySelector('[data-task-delete]');
        if (b) b.hidden = !(event.detail && event.detail.can_delete);
    });
})();
