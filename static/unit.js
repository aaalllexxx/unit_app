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

    /* ── Блокировка приложения и сессии (PIN-код) ─────────────────────────── */
    (function () {
        var AppLock = UNIT.AppLock = {};

        var uid = '';
        var PIN_KEY = '';
        var LOCKED_KEY = '';
        var LAST_ACT_KEY = '';
        var HIDDEN_AT_KEY = '';
        var AWAY_TIMEOUT = 60 * 1000;   // 60 сек вне приложения/вкладки
        var IDLE_TIMEOUT = 180 * 1000;  // 3 мин неактивности

        var awayTimer = null;
        var idleTimer = null;
        var lastTouch = 0;

        var lockInput = '';
        var setupMode = 'setup_1'; // 'setup_1', 'setup_2', 'change_old', 'change_new', 'change_confirm'
        var setupPin1 = '';
        var setupPin2 = '';

        async function computeHash(pin, salt) {
            var combined = (salt || '') + ':' + pin;
            if (window.crypto && window.crypto.subtle) {
                try {
                    var buf = new TextEncoder().encode(combined);
                    var hashBuf = await window.crypto.subtle.digest('SHA-256', buf);
                    var hashArr = Array.from(new Uint8Array(hashBuf));
                    return hashArr.map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
                } catch (e) {}
            }
            var h1 = 0xdeadbeef ^ 0, h2 = 0x41c6ce57 ^ 0;
            for (var i = 0; i < combined.length; i++) {
                var ch = combined.charCodeAt(i);
                h1 = Math.imul(h1 ^ ch, 2654435761);
                h2 = Math.imul(h2 ^ ch, 1597334677);
            }
            h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
            h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
            return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(16);
        }

        function generateSalt() {
            var arr = new Uint8Array(16);
            if (window.crypto && window.crypto.getRandomValues) {
                window.crypto.getRandomValues(arr);
            } else {
                for (var i = 0; i < 16; i++) arr[i] = Math.floor(Math.random() * 256);
            }
            return Array.from(arr).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
        }

        function getStoredData() {
            try {
                var raw = localStorage.getItem(PIN_KEY);
                return raw ? JSON.parse(raw) : null;
            } catch (e) {
                return null;
            }
        }

        function setStoredData(data) {
            try {
                if (data) localStorage.setItem(PIN_KEY, JSON.stringify(data));
                else localStorage.removeItem(PIN_KEY);
            } catch (e) {}
        }

        AppLock.hasPin = function () {
            var d = getStoredData();
            return !!(d && d.hash && d.salt);
        };

        AppLock.isLocked = function () {
            try {
                return localStorage.getItem(LOCKED_KEY) === '1';
            } catch (e) {
                return false;
            }
        };

        AppLock.lock = function () {
            if (!AppLock.hasPin()) return;
            try { localStorage.setItem(LOCKED_KEY, '1'); } catch (e) {}
            root.classList.add('app-locked');
            var overlay = document.getElementById('unit-app-lock');
            if (overlay) {
                overlay.hidden = false;
                overlay.removeAttribute('hidden');
                overlay.style.display = 'flex';
            }
            var setup = document.getElementById('unit-pin-setup');
            if (setup) {
                setup.hidden = true;
                setup.style.display = 'none';
            }
            lockInput = '';
            renderLockDots();
            setLockMessage('Введите 6-значный код доступа');
            UNIT.haptic('warn');
        };

        AppLock.unlock = function () {
            try {
                localStorage.removeItem(LOCKED_KEY);
                localStorage.setItem(LAST_ACT_KEY, Date.now());
            } catch (e) {}
            root.classList.remove('app-locked');
            var overlay = document.getElementById('unit-app-lock');
            if (overlay) {
                overlay.hidden = true;
                overlay.style.display = 'none';
            }
            lockInput = '';
            renderLockDots();
            resetIdleTimer();
            UNIT.haptic('success');
        };

        function setLockMessage(msg, isError, isSuccess) {
            var el = document.getElementById('unit-lock-msg');
            if (!el) return;
            el.textContent = msg;
            el.classList.toggle('error-msg', !!isError);
            el.classList.toggle('success-msg', !!isSuccess);
        }

        function renderLockDots(state) {
            var dotsBox = document.getElementById('unit-lock-dots');
            if (!dotsBox) return;
            dotsBox.classList.remove('error', 'success', 'shake');
            if (state === 'error') dotsBox.classList.add('error', 'shake');
            if (state === 'success') dotsBox.classList.add('success');
            var dots = dotsBox.querySelectorAll('.unit-pin-dot');
            dots.forEach(function (dot, idx) {
                dot.classList.toggle('filled', idx < lockInput.length);
            });
        }

        async function verifyLockPin() {
            var data = getStoredData();
            if (!data) {
                AppLock.unlock();
                return;
            }
            var h = await computeHash(lockInput, data.salt);
            if (h === data.hash) {
                renderLockDots('success');
                setLockMessage('Сессия разблокирована', false, true);
                setTimeout(function () {
                    AppLock.unlock();
                }, 220);
            } else {
                UNIT.haptic('error');
                renderLockDots('error');
                setLockMessage('Неверный код доступа', true, false);
                setTimeout(function () {
                    lockInput = '';
                    renderLockDots();
                    setLockMessage('Введите 6-значный код доступа');
                }, 600);
            }
        }

        AppLock.handleLockDigit = function (digit) {
            if (lockInput.length >= 6) return;
            lockInput += digit;
            UNIT.haptic('tap');
            renderLockDots();
            if (lockInput.length === 6) {
                verifyLockPin();
            }
        };

        AppLock.handleLockBack = function () {
            if (lockInput.length > 0) {
                lockInput = lockInput.slice(0, -1);
                UNIT.haptic('tap');
                renderLockDots();
            }
        };

        // ── Установка и смена PIN-кода ──
        AppLock.openSetup = function (mode) {
            setupMode = mode || 'setup';
            setupPin1 = '';
            setupPin2 = '';
            var overlay = document.getElementById('unit-pin-setup');
            if (!overlay) return;
            overlay.hidden = false;
            overlay.removeAttribute('hidden');
            overlay.style.display = 'flex';
            root.classList.add('app-locked');

            var cancelBtn = document.getElementById('unit-setup-cancel');
            if (cancelBtn) {
                cancelBtn.style.display = (setupMode === 'change') ? 'inline-block' : 'none';
            }

            if (setupMode === 'change') {
                setupMode = 'change_old';
                setSetupStep('Подтверждение', 'Введите текущий 6-значный код', 0);
            } else {
                setupMode = 'setup_1';
                setSetupStep('Придумайте код доступа', 'Шестизначный пароль для защиты сессии', 1);
            }
            renderSetupDots();
        };

        AppLock.closeSetup = function () {
            var overlay = document.getElementById('unit-pin-setup');
            if (overlay) {
                overlay.hidden = true;
                overlay.style.display = 'none';
            }
            if (!AppLock.isLocked()) {
                root.classList.remove('app-locked');
            }
            setupPin1 = '';
            setupPin2 = '';
        };

        function setSetupStep(title, msg, stepIdx, isError, isSuccess) {
            var titleEl = document.getElementById('unit-setup-title');
            var msgEl = document.getElementById('unit-setup-msg');
            var pill1 = document.getElementById('unit-pill-1');
            var pill2 = document.getElementById('unit-pill-2');
            if (titleEl) titleEl.textContent = title;
            if (msgEl) {
                msgEl.textContent = msg;
                msgEl.classList.toggle('error-msg', !!isError);
                msgEl.classList.toggle('success-msg', !!isSuccess);
            }
            if (pill1 && pill2) {
                if (stepIdx === 0) {
                    pill1.textContent = '1. Текущий код';
                    pill2.textContent = '2. Новый код';
                    pill1.classList.add('on');
                    pill2.classList.remove('on');
                } else if (stepIdx === 1) {
                    pill1.textContent = '1. Придумайте код';
                    pill2.textContent = '2. Повторите';
                    pill1.classList.add('on');
                    pill2.classList.remove('on');
                } else if (stepIdx === 2) {
                    pill1.classList.remove('on');
                    pill2.classList.add('on');
                }
            }
        }

        function renderSetupDots(state) {
            var dotsBox = document.getElementById('unit-setup-dots');
            if (!dotsBox) return;
            dotsBox.classList.remove('error', 'success', 'shake');
            if (state === 'error') dotsBox.classList.add('error', 'shake');
            if (state === 'success') dotsBox.classList.add('success');
            var currentStr = (setupMode === 'setup_1' || setupMode === 'change_old' || setupMode === 'change_new') ? setupPin1 : setupPin2;
            var dots = dotsBox.querySelectorAll('.unit-pin-dot');
            dots.forEach(function (dot, idx) {
                dot.classList.toggle('filled', idx < currentStr.length);
            });
        }

        async function processSetupStep() {
            if (setupMode === 'change_old') {
                var data = getStoredData();
                var h = await computeHash(setupPin1, data ? data.salt : '');
                if (data && h === data.hash) {
                    UNIT.haptic('success');
                    renderSetupDots('success');
                    setTimeout(function () {
                        setupMode = 'change_new';
                        setupPin1 = '';
                        setSetupStep('Новый код доступа', 'Придумайте новый 6-значный код', 1);
                        renderSetupDots();
                    }, 250);
                } else {
                    UNIT.haptic('error');
                    renderSetupDots('error');
                    setSetupStep('Подтверждение', 'Неверный текущий код', 0, true);
                    setTimeout(function () {
                        setupPin1 = '';
                        renderSetupDots();
                        setSetupStep('Подтверждение', 'Введите текущий 6-значный код', 0);
                    }, 600);
                }
            } else if (setupMode === 'setup_1' || setupMode === 'change_new') {
                UNIT.haptic('tap');
                renderSetupDots();
                setupMode = (setupMode === 'setup_1') ? 'setup_2' : 'change_confirm';
                setupPin2 = '';
                setSetupStep('Повторите код доступа', 'Введите код ещё раз для подтверждения', 2);
                renderSetupDots();
            } else if (setupMode === 'setup_2' || setupMode === 'change_confirm') {
                if (setupPin1 === setupPin2) {
                    UNIT.haptic('success');
                    renderSetupDots('success');
                    setSetupStep('Готово', 'Код доступа успешно установлен', 2, false, true);
                    var salt = generateSalt();
                    var hash = await computeHash(setupPin1, salt);
                    setStoredData({ hash: hash, salt: salt, updatedAt: Date.now() });
                    try {
                        localStorage.removeItem(LOCKED_KEY);
                        localStorage.setItem(LAST_ACT_KEY, Date.now());
                    } catch (e) {}
                    setTimeout(function () {
                        AppLock.closeSetup();
                        UNIT.toast('Код сессии сохранён');
                    }, 350);
                } else {
                    UNIT.haptic('error');
                    renderSetupDots('error');
                    setSetupStep('Коды не совпали', 'Попробуйте придумать код заново', 1, true);
                    setTimeout(function () {
                        setupMode = (setupMode === 'setup_2') ? 'setup_1' : 'change_new';
                        setupPin1 = '';
                        setupPin2 = '';
                        renderSetupDots();
                        setSetupStep('Придумайте код доступа', 'Шестизначный пароль для защиты сессии', 1);
                    }, 700);
                }
            }
        }

        AppLock.handleSetupDigit = function (digit) {
            var currentStr = (setupMode === 'setup_1' || setupMode === 'change_old' || setupMode === 'change_new') ? setupPin1 : setupPin2;
            if (currentStr.length >= 6) return;
            currentStr += digit;
            if (setupMode === 'setup_1' || setupMode === 'change_old' || setupMode === 'change_new') setupPin1 = currentStr;
            else setupPin2 = currentStr;
            UNIT.haptic('tap');
            renderSetupDots();
            if (currentStr.length === 6) {
                processSetupStep();
            }
        };

        AppLock.handleSetupBack = function () {
            var currentStr = (setupMode === 'setup_1' || setupMode === 'change_old' || setupMode === 'change_new') ? setupPin1 : setupPin2;
            if (currentStr.length > 0) {
                currentStr = currentStr.slice(0, -1);
                if (setupMode === 'setup_1' || setupMode === 'change_old' || setupMode === 'change_new') setupPin1 = currentStr;
                else setupPin2 = currentStr;
                UNIT.haptic('tap');
                renderSetupDots();
            }
        };

        function resetIdleTimer() {
            try { localStorage.setItem(LAST_ACT_KEY, Date.now()); } catch (e) {}
            clearTimeout(idleTimer);
            if (!AppLock.isLocked()) {
                idleTimer = setTimeout(function () {
                    if (AppLock.hasPin()) AppLock.lock();
                }, IDLE_TIMEOUT);
            }
        }

        function onUserActivity() {
            var now = Date.now();
            if (now - lastTouch < 2000) return;
            lastTouch = now;
            resetIdleTimer();
        }

        AppLock.init = function () {
            uid = (document.body && document.body.getAttribute('data-user-id')) || (window.UNIT && window.UNIT.userId) || '';
            if (!uid) return;

            PIN_KEY = 'unit_pin_' + uid;
            LOCKED_KEY = 'unit_locked_' + uid;
            LAST_ACT_KEY = 'unit_last_act_' + uid;
            HIDDEN_AT_KEY = 'unit_hidden_at_' + uid;

            var navTime = parseInt(sessionStorage.getItem('unit_nav_time') || '0', 10);
            sessionStorage.removeItem('unit_nav_time');
            var isInternal = navTime && (Date.now() - navTime < 10000);

            document.addEventListener('click', function (e) {
                var a = e.target.closest && e.target.closest('a');
                if (a && a.href && a.origin === location.origin && !a.target && !a.hasAttribute('download')) {
                    sessionStorage.setItem('unit_nav_time', Date.now());
                }
            }, true);
            window.addEventListener('beforeunload', function () {
                sessionStorage.setItem('unit_nav_time', Date.now());
            });

            ['mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll'].forEach(function (ev) {
                window.addEventListener(ev, onUserActivity, { passive: true });
            });

            document.addEventListener('visibilitychange', function () {
                if (document.visibilityState === 'hidden') {
                    var now = Date.now();
                    try { localStorage.setItem(HIDDEN_AT_KEY, now); } catch (e) {}
                    clearTimeout(awayTimer);
                    awayTimer = setTimeout(function () {
                        if (document.visibilityState === 'hidden' && AppLock.hasPin()) {
                            try { localStorage.setItem(LOCKED_KEY, '1'); } catch (e) {}
                        }
                    }, AWAY_TIMEOUT);
                } else {
                    clearTimeout(awayTimer);
                    var hiddenAt = parseInt(localStorage.getItem(HIDDEN_AT_KEY) || '0', 10);
                    var isLocked = localStorage.getItem(LOCKED_KEY) === '1';
                    if (AppLock.hasPin() && (isLocked || (hiddenAt && (Date.now() - hiddenAt >= AWAY_TIMEOUT)))) {
                        AppLock.lock();
                    } else if (!AppLock.isLocked()) {
                        resetIdleTimer();
                    }
                    try { localStorage.removeItem(HIDDEN_AT_KEY); } catch (e) {}
                }
            });

            window.addEventListener('storage', function (e) {
                if (e.key === LOCKED_KEY) {
                    if (e.newValue === '1') AppLock.lock();
                    else if (!e.newValue) AppLock.unlock();
                } else if (e.key === PIN_KEY) {
                    if (AppLock.hasPin()) AppLock.closeSetup();
                }
            });

            window.addEventListener('keydown', function (e) {
                var lockEl = document.getElementById('unit-app-lock');
                var setupEl = document.getElementById('unit-pin-setup');
                var isLockOpen = lockEl && !lockEl.hidden && lockEl.style.display !== 'none';
                var isSetupOpen = setupEl && !setupEl.hidden && setupEl.style.display !== 'none';
                if (!isLockOpen && !isSetupOpen) return;

                if (e.key >= '0' && e.key <= '9') {
                    e.preventDefault();
                    if (isLockOpen) AppLock.handleLockDigit(e.key);
                    else if (isSetupOpen) AppLock.handleSetupDigit(e.key);
                } else if (e.key === 'Backspace') {
                    e.preventDefault();
                    if (isLockOpen) AppLock.handleLockBack();
                    else if (isSetupOpen) AppLock.handleSetupBack();
                }
            });

            document.addEventListener('click', function (e) {
                var padKey = e.target.closest && e.target.closest('.unit-dial-key');
                if (padKey) {
                    var pad = padKey.closest('[data-pad]');
                    var isLock = pad && pad.getAttribute('data-pad') === 'lock';
                    var num = padKey.getAttribute('data-num');
                    var act = padKey.getAttribute('data-act');
                    if (num !== null) {
                        if (isLock) AppLock.handleLockDigit(num);
                        else AppLock.handleSetupDigit(num);
                    } else if (act === 'del') {
                        if (isLock) AppLock.handleLockBack();
                        else AppLock.handleSetupBack();
                    }
                    return;
                }

                var lockTrigger = e.target.closest && e.target.closest('[data-lock-app]');
                if (lockTrigger) {
                    e.preventDefault();
                    AppLock.lock();
                    return;
                }

                var changeTrigger = e.target.closest && e.target.closest('[data-change-pin]');
                if (changeTrigger) {
                    e.preventDefault();
                    AppLock.openSetup('change');
                    return;
                }

                var cancelBtn = e.target.closest && e.target.closest('#unit-setup-cancel');
                if (cancelBtn) {
                    e.preventDefault();
                    AppLock.closeSetup();
                    return;
                }

                var forgotLink = e.target.closest && e.target.closest('#unit-lock-logout');
                if (forgotLink) {
                    try {
                        localStorage.removeItem(PIN_KEY);
                        localStorage.removeItem(LOCKED_KEY);
                        localStorage.removeItem(LAST_ACT_KEY);
                        localStorage.removeItem(HIDDEN_AT_KEY);
                    } catch (err) {}
                }
            });

            if (!AppLock.hasPin()) {
                AppLock.openSetup('setup');
            } else {
                var lastAct = parseInt(localStorage.getItem(LAST_ACT_KEY) || '0', 10);
                var isLocked = localStorage.getItem(LOCKED_KEY) === '1';
                if (isLocked || (!isInternal && lastAct && (Date.now() - lastAct >= AWAY_TIMEOUT))) {
                    AppLock.lock();
                } else {
                    root.classList.remove('app-locked');
                    resetIdleTimer();
                }
            }
        };
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
