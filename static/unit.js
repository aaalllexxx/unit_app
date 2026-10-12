/* Общий скрипт UNIT ID и портала лаборатории.
   — вибрация (UNIT.haptic): лёгкий отклик на нажатия, выбор, успех и ошибку;
     Android — navigator.vibrate, iPhone (Safari 18+) — системный отклик переключателя;
   — нижние листы (<dialog class="sheet">): открыть [data-sheet-open], закрыть
     [data-sheet-close], фоном или свайпом вниз;
   — вкладки [data-tabs] с панелями [data-pane], помнят выбор в адресе (#…);
   — уведомления: флеши превращаются во всплывающие и сами исчезают;
   — подтверждение опасных действий (data-confirm), копирование (data-copy),
     растущие поля (textarea[data-autosize]) и service worker. */
(function () {
    'use strict';

    var root = document.documentElement;
    var UNIT = window.UNIT = window.UNIT || {};

    /* ── Вибрация ─────────────────────────────────────────────────────────── */
    var PATTERNS = { tap: 8, select: 5, lift: 14, drop: [8, 30, 12], success: [10, 40, 18],
                     warn: [18, 60, 18], error: [30, 50, 30, 50, 30], swipe: [6, 20, 10, 20, 24] };
    var iosSwitch = null;
    function enabled() {
        try { if (localStorage.getItem('unit.haptics') === '0') return false; } catch (e) { /* без хранилища — включено */ }
        return document.body ? document.body.getAttribute('data-haptics') !== '0' : true;
    }
    function iosTick() {
        // Safari на iPhone не умеет vibrate, но щелчок по переключателю даёт системный отклик
        if (!iosSwitch) {
            var label = document.createElement('label');
            label.setAttribute('aria-hidden', 'true');
            label.style.cssText = 'position:fixed;left:-99px;top:0;width:1px;height:1px;overflow:hidden;opacity:0;pointer-events:none';
            var input = document.createElement('input');
            input.type = 'checkbox';
            input.setAttribute('switch', '');
            input.tabIndex = -1;
            label.appendChild(input);
            document.body.appendChild(label);
            iosSwitch = label;
        }
        iosSwitch.click();
    }
    UNIT.haptic = function (kind) {
        if (!enabled()) return;
        var pattern = PATTERNS[kind] || PATTERNS.tap;
        try {
            if (navigator.vibrate) { navigator.vibrate(pattern); return; }
            if (/iP(hone|ad|od)/.test(navigator.userAgent)) {
                iosTick();
                if (Array.isArray(pattern) && pattern.length > 2) setTimeout(iosTick, 90);
            }
        } catch (e) { /* вибрация — украшение, без неё всё работает */ }
    };
    UNIT.setHaptics = function (on) {
        try { localStorage.setItem('unit.haptics', on ? '1' : '0'); } catch (e) { /* нет хранилища */ }
        if (document.body) document.body.setAttribute('data-haptics', on ? '1' : '0');
        if (on) UNIT.haptic('success');
    };

    document.addEventListener('click', function (event) {
        var el = event.target.closest && event.target.closest('[data-haptic], .btn, .side-link, .tag-chip, .tab-btn, .chip-btn, summary');
        if (!el || el.disabled) return;
        UNIT.haptic(el.getAttribute('data-haptic') || (el.classList.contains('btn-danger') ? 'warn' : 'tap'));
    }, true);
    document.addEventListener('change', function (event) {
        var t = event.target;
        if (!t || (iosSwitch && iosSwitch.contains(t))) return;     // свой переключатель для iPhone
        if (t.type === 'checkbox' || t.type === 'radio' || t.tagName === 'SELECT') UNIT.haptic('select');
    });

    /* ── Подтверждение и копирование ──────────────────────────────────────── */
    document.addEventListener('submit', function (event) {
        var button = event.submitter;
        var text = button && button.getAttribute('data-confirm');
        if (text && !window.confirm(text)) {
            event.preventDefault();
            return;
        }
        if (text) UNIT.haptic('warn');
    });

    document.addEventListener('click', function (event) {
        var button = event.target.closest && event.target.closest('[data-copy]');
        if (!button) return;
        var source = button.parentNode.querySelector('[data-copy-src]');
        if (!source) return;
        var text = source.textContent.trim();
        var done = function () {
            UNIT.haptic('success');
            UNIT.toast('Скопировано');
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done, function () { window.getSelection().selectAllChildren(source); });
        } else {
            window.getSelection().selectAllChildren(source);
        }
    });

    /* ── Всплывающие уведомления ──────────────────────────────────────────── */
    function stack() {
        var box = document.querySelector('.toasts');
        if (!box) {
            box = document.createElement('div');
            box.className = 'toasts';
            box.setAttribute('role', 'status');
            box.setAttribute('aria-live', 'polite');
            document.body.appendChild(box);
        }
        return box;
    }
    UNIT.toast = function (text, kind) {
        var item = document.createElement('div');
        item.className = 'toast' + (kind === 'err' ? ' err' : '');
        item.textContent = text;
        stack().appendChild(item);
        requestAnimationFrame(function () { item.classList.add('on'); });
        setTimeout(function () {
            item.classList.remove('on');
            setTimeout(function () { item.remove(); }, 300);
        }, kind === 'err' ? 6000 : 3200);
    };
    function flashesToToasts() {
        document.querySelectorAll('.flashes').forEach(function (box) {
            if (box.hasAttribute('data-keep')) return;
            var bad = false;
            box.querySelectorAll('.flash').forEach(function (f) {
                var err = f.classList.contains('err');
                bad = bad || err;
                UNIT.toast(f.textContent.trim(), err ? 'err' : 'ok');
            });
            box.remove();
            UNIT.haptic(bad ? 'error' : 'success');
        });
    }

    /* ── Нижние листы ─────────────────────────────────────────────────────── */
    UNIT.openSheet = function (sheet) {
        if (typeof sheet === 'string') sheet = document.getElementById(sheet);
        if (!sheet) return null;
        if (sheet.showModal && !sheet.open) sheet.showModal(); else sheet.setAttribute('open', '');
        root.classList.add('sheet-lock');
        UNIT.haptic('lift');
        var focus = sheet.querySelector('[autofocus]');
        if (focus && window.matchMedia('(pointer: fine)').matches) setTimeout(function () { focus.focus(); }, 60);
        else if (document.activeElement && sheet.contains(document.activeElement)) document.activeElement.blur();   // без рамки фокуса на крестике
        return sheet;
    };
    UNIT.closeSheet = function (sheet) {
        if (!sheet || !sheet.open) return;
        sheet.classList.add('closing');
        setTimeout(function () {
            sheet.classList.remove('closing');
            sheet.style.transform = '';
            if (sheet.close) sheet.close(); else sheet.removeAttribute('open');
            if (!document.querySelector('dialog.sheet[open]')) root.classList.remove('sheet-lock');
        }, 180);
    };
    document.addEventListener('click', function (event) {
        var opener = event.target.closest && event.target.closest('[data-sheet-open]');
        if (opener) {
            event.preventDefault();
            UNIT.openSheet(opener.getAttribute('data-sheet-open'));
            return;
        }
        var closer = event.target.closest && event.target.closest('[data-sheet-close]');
        if (closer) {
            UNIT.closeSheet(closer.closest('dialog.sheet'));
            return;
        }
        if (event.target.matches && event.target.matches('dialog.sheet[open]')) {
            UNIT.closeSheet(event.target);   // щелчок по фону вокруг листа
        }
    });
    document.addEventListener('cancel', function (event) {
        if (event.target.matches && event.target.matches('dialog.sheet')) {
            event.preventDefault();
            UNIT.closeSheet(event.target);
        }
    }, true);
    // Свайп вниз за «ручку» или шапку листа — закрыть
    (function () {
        var sheet = null, startY = 0, dy = 0;
        document.addEventListener('pointerdown', function (event) {
            var grip = event.target.closest && event.target.closest('.sheet-grip, .sheet-head');
            if (!grip || event.target.closest('button, a, input, select, textarea')) return;
            sheet = grip.closest('dialog.sheet');
            if (!sheet || window.innerWidth > 900 && !sheet.classList.contains('sheet-bottom')) { sheet = null; return; }
            startY = event.clientY;
            dy = 0;
            sheet.style.transition = 'none';
        });
        document.addEventListener('pointermove', function (event) {
            if (!sheet) return;
            dy = Math.max(0, event.clientY - startY);
            sheet.style.transform = 'translateY(' + dy + 'px)';
        });
        var end = function () {
            if (!sheet) return;
            sheet.style.transition = '';
            if (dy > 110) UNIT.closeSheet(sheet); else sheet.style.transform = '';
            sheet = null;
        };
        document.addEventListener('pointerup', end);
        document.addEventListener('pointercancel', end);
    })();

    /* ── Вкладки ──────────────────────────────────────────────────────────── */
    function setupTabs(box) {
        var buttons = box.querySelectorAll('[data-tab]');
        var scope = document.querySelector(box.getAttribute('data-tabs') || 'body') || document;
        var show = function (name, quiet) {
            var found = false;
            buttons.forEach(function (b) {
                var on = b.getAttribute('data-tab') === name;
                found = found || on;
                b.classList.toggle('on', on);
                b.setAttribute('aria-selected', on ? 'true' : 'false');
            });
            if (!found) return false;
            scope.querySelectorAll('[data-pane]').forEach(function (pane) {
                pane.hidden = pane.getAttribute('data-pane') !== name;
            });
            if (!quiet) {
                history.replaceState(null, '', location.pathname + location.search + '#' + name);
                UNIT.haptic('select');
            }
            document.dispatchEvent(new CustomEvent('unit:tab', { detail: name }));
            return true;
        };
        buttons.forEach(function (b) {
            b.addEventListener('click', function (event) {
                event.preventDefault();
                show(b.getAttribute('data-tab'));
            });
        });
        var wanted = (location.hash || '').slice(1);
        if (!wanted || !show(wanted, true)) show(buttons[0] && buttons[0].getAttribute('data-tab'), true);
        window.addEventListener('hashchange', function () { show((location.hash || '').slice(1), true); });
    }

    /* ── Растущие поля ────────────────────────────────────────────────────── */
    function autosize(area) {
        area.style.height = 'auto';
        area.style.height = Math.min(area.scrollHeight + 2, 420) + 'px';
    }
    document.addEventListener('input', function (event) {
        if (event.target.matches && event.target.matches('textarea[data-autosize]')) autosize(event.target);
    });
    UNIT.autosize = autosize;

    /* ── Блокировка приложения 6-значным кодом ────────────────────────────────
       Решает сервер (applock.py): код хранится там хешем PBKDF2, а заблокированная
       сессия не отдаёт ни страниц, ни API. Здесь — экран ввода кода, «придумайте код»
       при первом запуске приложения, смена и отключение кода, и сигнал «человек
       работает», чтобы бездействие считалось по делу, а не по запросам. */
    (function () {
        var AppLock = UNIT.AppLock = {};
        var standalone = (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) ||
            window.navigator.standalone === true;
        var before = (document.cookie.match(/(?:^|; )unit_app=([01])/) || [])[1];
        document.cookie = 'unit_app=' + (standalone ? '1' : '0') + '; path=/; max-age=31536000; samesite=lax' +
            (location.protocol === 'https:' ? '; secure' : '');
        var mode = '';                 // '' — без кода, 'setup' — придумать, 'on' — открыто, 'locked' — экран кода
        var input = '';
        var step = '';                 // шаги окна кода: new, confirm, current, off
        var first = '';
        var current = '';
        var busy = false;
        var lastPing = 0;

        function post(url, body) {
            return fetch(url, {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-Requested-With': 'fetch' },
                body: JSON.stringify(body || {})
            }).then(function (r) {
                return r.json().catch(function () { return {}; }).then(function (d) { d.status = r.status; return d; });
            });
        }
        function el(id) { return document.getElementById(id); }
        function show(id, on) {
            var o = el(id);
            if (!o) return;
            o.hidden = !on;
            o.style.display = on ? 'flex' : 'none';
        }
        function dots(box, count, state) {
            var b = el(box);
            if (!b) return;
            b.classList.remove('error', 'success', 'shake');
            if (state) { void b.offsetWidth; b.classList.add(state); if (state === 'error') b.classList.add('shake'); }
            b.querySelectorAll('.unit-pin-dot').forEach(function (d, i) { d.classList.toggle('filled', i < count); });
        }
        function say(id, text, kind) {
            var m = el(id);
            if (!m) return;
            m.textContent = text;
            m.classList.toggle('error-msg', kind === 'error');
            m.classList.toggle('success-msg', kind === 'ok');
        }

        /* экран кода (сессия заблокирована) */
        function unlock() {
            busy = true;
            post('/lock/unlock', { pin: input }).then(function (d) {
                if (d.ok) {
                    dots('unit-lock-dots', 6, 'success');
                    say('unit-lock-msg', 'Открываю…', 'ok');
                    UNIT.haptic('success');
                    setTimeout(function () { location.reload(); }, 180);
                    return;
                }
                UNIT.haptic('error');
                dots('unit-lock-dots', 6, 'error');
                say('unit-lock-msg', d.error || 'Неверный код', 'error');
                if (d.logout) { setTimeout(function () { location.href = '/'; }, 1600); return; }
                setTimeout(function () { input = ''; busy = false; dots('unit-lock-dots', 0); }, 650);
            }).catch(function () {
                say('unit-lock-msg', 'Нет связи с сервером — попробуйте ещё раз', 'error');
                input = ''; busy = false; dots('unit-lock-dots', 0);
            });
        }

        /* окно «придумайте / смените / отключите код» */
        var TEXT = {
            'new': ['Придумайте код', 'Шесть цифр — ими приложение откроется, если вы надолго отойдёте'],
            confirm: ['Повторите код', 'Введите тот же код ещё раз'],
            current: ['Текущий код', 'Сначала введите код, который есть сейчас'],
            off: ['Отключить код', 'Введите текущий код — на этом устройстве он больше не понадобится']
        };
        function setStep(s, error) {
            step = s;
            input = '';
            var t = TEXT[s];
            el('unit-setup-title').textContent = t[0];
            say('unit-setup-msg', error || t[1], error ? 'error' : '');
            var p1 = el('unit-pill-1'), p2 = el('unit-pill-2');
            if (p1 && p2) {
                p1.textContent = s === 'current' || s === 'off' ? '1. Текущий' : '1. Придумайте';
                p2.textContent = s === 'current' || s === 'off' ? '2. Новый' : '2. Повторите';
                p1.classList.toggle('on', s !== 'confirm');
                p2.classList.toggle('on', s === 'confirm');
            }
            dots('unit-setup-dots', 0, error ? 'error' : '');
        }
        AppLock.openSetup = function (kind) {
            first = ''; current = '';
            var cancel = el('unit-setup-cancel');
            if (cancel) cancel.style.display = kind === 'setup' ? 'none' : 'inline-block';
            show('unit-pin-setup', true);
            document.documentElement.classList.add('app-locked');
            setStep(kind === 'change' ? 'current' : kind === 'off' ? 'off' : 'new');
        };
        AppLock.closeSetup = function () {
            show('unit-pin-setup', false);
            if (mode !== 'locked') document.documentElement.classList.remove('app-locked');
        };
        function setupDone() {
            busy = true;
            if (step === 'off') {
                post('/lock/off', { current: input }).then(function (d) {
                    busy = false;
                    if (!d.ok) { UNIT.haptic('error'); setStep('off', d.error || 'Неверный код'); return; }
                    AppLock.closeSetup();
                    UNIT.toast('Код на этом устройстве отключён');
                    setTimeout(function () { location.reload(); }, 600);
                });
                return;
            }
            if (step === 'current') { current = input; busy = false; setStep('new'); return; }
            if (step === 'new') { first = input; busy = false; setStep('confirm'); UNIT.haptic('tap'); return; }
            if (input !== first) { busy = false; UNIT.haptic('error'); setStep('new', 'Коды не совпали — придумайте заново'); return; }
            post('/lock/pin', current ? { pin: input, current: current } : { pin: input }).then(function (d) {
                busy = false;
                if (!d.ok) {
                    UNIT.haptic('error');
                    setStep(current ? 'current' : 'new', d.error || 'Не получилось сохранить код');
                    current = '';
                    return;
                }
                UNIT.haptic('success');
                dots('unit-setup-dots', 6, 'success');
                say('unit-setup-msg', 'Код сохранён', 'ok');
                mode = 'on';
                document.body.setAttribute('data-lock', 'on');
                setTimeout(function () { AppLock.closeSetup(); UNIT.toast('Код приложения сохранён'); }, 450);
            }).catch(function () { busy = false; setStep(step, 'Нет связи с сервером'); });
        }

        function digit(d, pad) {
            if (busy) return;
            if (input.length >= 6) return;
            input += d;
            UNIT.haptic('tap');
            if (pad === 'lock') {
                dots('unit-lock-dots', input.length);
                if (input.length === 6) unlock();
            } else {
                dots('unit-setup-dots', input.length);
                if (input.length === 6) setTimeout(setupDone, 120);
            }
        }
        function back(pad) {
            if (busy || !input) return;
            input = input.slice(0, -1);
            dots(pad === 'lock' ? 'unit-lock-dots' : 'unit-setup-dots', input.length);
        }

        /* человек работает в приложении — бездействие считается заново; давно не был — экран кода */
        function ping(force) {
            if (mode !== 'on') return;
            var now = Date.now();
            if (!force && now - lastPing < 60000) return;
            lastPing = now;
            post('/lock/ping').then(function (d) { if (d.locked) location.reload(); }).catch(function () {});
        }

        AppLock.init = function () {
            mode = (document.body && document.body.getAttribute('data-lock')) || '';
            // Первый запуск с экрана «Домой»: сервер ещё не знал, что это приложение
            if (standalone && before !== '1' && !mode && document.body.getAttribute('data-user-id')) {
                location.reload();
                return;
            }
            if (mode === 'locked') {
                document.documentElement.classList.add('app-locked');
                show('unit-app-lock', true);
                dots('unit-lock-dots', 0);
            } else if (mode === 'setup') {
                AppLock.openSetup('setup');
            }
            ['pointerdown', 'keydown', 'scroll'].forEach(function (ev) {
                window.addEventListener(ev, function () { ping(false); }, { passive: true });
            });
            document.addEventListener('visibilitychange', function () {
                if (document.visibilityState === 'visible') ping(true);
            });
            document.addEventListener('keydown', function (e) {
                var lockOpen = el('unit-app-lock') && !el('unit-app-lock').hidden;
                var setupOpen = el('unit-pin-setup') && !el('unit-pin-setup').hidden;
                if (!lockOpen && !setupOpen) return;
                if (e.key >= '0' && e.key <= '9') { e.preventDefault(); digit(e.key, lockOpen ? 'lock' : 'setup'); }
                else if (e.key === 'Backspace') { e.preventDefault(); back(lockOpen ? 'lock' : 'setup'); }
            });
            document.addEventListener('click', function (e) {
                var key = e.target.closest && e.target.closest('.unit-dial-key');
                if (key) {
                    var pad = key.closest('[data-pad]').getAttribute('data-pad');
                    if (key.hasAttribute('data-num')) digit(key.getAttribute('data-num'), pad);
                    else if (key.getAttribute('data-act') === 'del') back(pad);
                    return;
                }
                var t = e.target.closest && e.target.closest('[data-lock-app], [data-change-pin], [data-pin-off], #unit-setup-cancel');
                if (!t) return;
                e.preventDefault();
                if (t.id === 'unit-setup-cancel') { AppLock.closeSetup(); return; }
                if (mode !== 'on') { UNIT.toast('Код приложения работает, когда портал открыт с экрана «Домой»'); return; }
                if (t.hasAttribute('data-lock-app')) post('/lock/now').then(function () { location.reload(); });
                else if (t.hasAttribute('data-change-pin')) AppLock.openSetup('change');
                else AppLock.openSetup('off');
            });
        };
        AppLock.mode = function () { return mode; };
    })();

    function ready() {
        flashesToToasts();
        document.querySelectorAll('[data-tabs]').forEach(setupTabs);
        document.querySelectorAll('textarea[data-autosize]').forEach(autosize);
        document.querySelectorAll('dialog.sheet[data-open]').forEach(function (s) { UNIT.openSheet(s); });
        if (UNIT.AppLock) UNIT.AppLock.init();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready); else ready();

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/sw.js').catch(function () {});
        });
    }
})();
