/* Service worker: приложение ставится на телефон и компьютер, оформление грузится из кеша,
   а без связи вместо ошибки браузера открывается страница «Нет связи».
   Страницы с данными не кешируются: они личные и должны быть свежими.
   Push: уведомления портала приходят, даже когда он закрыт; нажатие открывает нужную страницу.
   Кеш можно очистить в «Настройках» — сообщением clear-cache. */
const CACHE = 'unit-shell-v4';
const SHELL = ['/offline', '/static/logo.png', '/static/icon-192.png'];

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
    event.waitUntil(caches.keys()
        .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
        .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method !== 'GET') return;
    const url = new URL(request.url);
    if (url.origin !== location.origin) return;
    if (request.mode === 'navigate') {
        event.respondWith(fetch(request).catch(() => caches.match('/offline')));
        return;
    }
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(caches.match(request).then((hit) => hit || fetch(request).then((response) => {
            if (response.ok) {
                const copy = response.clone();
                caches.open(CACHE).then((cache) => cache.put(request, copy));
            }
            return response;
        })));
    }
});

self.addEventListener('message', (event) => {
    if (event.data === 'clear-cache') {
        event.waitUntil(caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
            .then(() => caches.open(CACHE).then((cache) => cache.addAll(SHELL))));
    }
});

self.addEventListener('push', (event) => {
    let data = {};
    try { data = event.data ? event.data.json() : {}; } catch (err) { data = { body: event.data ? event.data.text() : '' }; }
    event.waitUntil(self.registration.showNotification(data.title || 'Лаборатория UNIT', {
        body: data.body || '',
        icon: '/static/icon-192.png',
        badge: '/static/icon-192.png',
        tag: data.tag || undefined,
        data: { url: data.url || '/home' },
    }));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const url = (event.notification.data && event.notification.data.url) || '/home';
    event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
        const same = wins.find((w) => new URL(w.url).origin === location.origin);
        if (same) { same.focus(); return same.navigate(url); }
        return self.clients.openWindow(url);
    }));
});
