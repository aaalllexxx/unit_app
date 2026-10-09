/* Service worker: приложение ставится на телефон и компьютер, оформление грузится из кеша,
   а без связи вместо ошибки браузера открывается страница «Нет связи».
   Страницы с данными не кешируются: они личные и должны быть свежими. */
const CACHE = 'unit-shell-v1';
const SHELL = ['/offline', '/static/unit.css?v=3', '/static/unit.js?v=2', '/static/logo.png', '/static/icon-192.png'];

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
