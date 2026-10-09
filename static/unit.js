/* Общий скрипт портала и UNIT ID: подтверждение опасных действий
   (кнопки с data-confirm) и регистрация service worker для установки как приложения. */
document.addEventListener('submit', function (event) {
    var button = event.submitter;
    var text = button && button.getAttribute('data-confirm');
    if (text && !window.confirm(text)) {
        event.preventDefault();
    }
});

if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
        navigator.serviceWorker.register('/sw.js').catch(function () {});
    });
}
