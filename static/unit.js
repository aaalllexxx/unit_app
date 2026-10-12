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

    function ready() {
        flashesToToasts();
        document.querySelectorAll('[data-tabs]').forEach(setupTabs);
        document.querySelectorAll('textarea[data-autosize]').forEach(autosize);
        document.querySelectorAll('dialog.sheet[data-open]').forEach(function (s) { UNIT.openSheet(s); });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready); else ready();

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/sw.js').catch(function () {});
        });
    }
})();
