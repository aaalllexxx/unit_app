"""
Блокировка приложения 6-значным кодом (такая же — в UNIT ID).

Код хранится на сервере: хеш PBKDF2 на человека и устройство (cookie lab_dev).
Решает сервер: приложение (PWA — cookie unit_app=1 ставит unit.js в режиме
«на экране домой») бездействует дольше IDLE — сессия помечается заблокированной,
и пока код не введён, страницы показывают только экран кода, а API отвечает 423.
Пять ошибок подряд — выход из аккаунта и код на устройстве стирается.
Первый запуск приложения — человек придумывает код. В обычной вкладке браузера
код не спрашивается: там защищает сам вход через UNIT ID.
"""

import hashlib
import hmac
import secrets
import time
from datetime import datetime

from flask import g, render_template, request, session
from sqlalchemy import select

import models as M
from app import app, db

IDLE = 5 * 60              # столько бездействия — и приложение просит код
MAX_FAILS = 5
ITERATIONS = 200_000
DEVICE_COOKIE = "lab_dev"
EXEMPT = ("/static/", "/lock/", "/sw.js", "/manifest.webmanifest", "/healthz", "/logout", "/offline", "/login",
          "/auth/", "/favicon")


def _hash(pin: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2${ITERATIONS}${salt}${digest}"


def _check(pin: str, stored: str) -> bool:
    try:
        _, rounds, salt, digest = stored.split("$")
        mine = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), int(rounds)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(mine, digest)


def _valid(pin) -> bool:
    return isinstance(pin, str) and len(pin) == 6 and pin.isdigit()


def device() -> str:
    """Своё устройство — случайный ключ в cookie (хеш — в базе)."""
    key = request.cookies.get(DEVICE_COOKIE) or g.get("new_device")
    if not key:
        key = g.new_device = secrets.token_hex(16)
    return hashlib.sha256(key.encode()).hexdigest()


@app.after_request
def _device_cookie(response):
    if g.get("new_device"):
        response.set_cookie(DEVICE_COOKIE, g.new_device, max_age=2 * 365 * 86400, httponly=True, samesite="Lax",
                            secure=app.config.get("SESSION_COOKIE_SECURE", False))
    return response


def app_mode() -> bool:
    return request.cookies.get("unit_app") == "1"


def pin_row(member_id: str):
    return db().scalar(select(M.AppPin).where(M.AppPin.member_id == member_id, M.AppPin.device == device()))


def _touch(force: bool = False) -> None:
    now = int(time.time())
    if force or now - int(session.get("seen") or 0) > 20:
        session["seen"] = now


def state() -> str:
    """Для шаблона: '' — без кода, 'setup' — придумать код, 'on' — код есть и сессия открыта."""
    return g.get("lock_mode", "")


def wants_json() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json" or \
        request.headers.get("X-Requested-With") == "fetch"


@app.before_request
def _gate():
    me = g.get("me")
    if not me or request.path.startswith(EXEMPT):
        return None
    if not app_mode():
        session.pop("lk", None)
        _touch()
        return None
    if not pin_row(me.id):
        g.lock_mode = "setup"
        _touch()
        return None
    if session.get("lk") or int(time.time()) - int(session.get("seen") or 0) > IDLE:
        session["lk"] = True
        if wants_json():
            return {"error": "Приложение заблокировано — введите код", "locked": True}, 423
        response = app.make_response(render_template("locked.html"))
        response.headers["Cache-Control"] = "no-store"
        return response
    g.lock_mode = "on"
    _touch()
    return None


def _me():
    if not g.get("me"):
        return None, ({"error": "Нужно войти"}, 401)
    return g.me, None


@app.post("/lock/unlock")
def lock_unlock():
    me, err = _me()
    if err:
        return err
    row = pin_row(me.id)
    if not row:
        session.pop("lk", None)
        _touch(True)
        return {"ok": True}
    pin = str((request.get_json(silent=True) or {}).get("pin") or "")
    if _valid(pin) and _check(pin, row.pin_hash):
        row.fails, row.used_at = 0, datetime.now()
        db().commit()
        session.pop("lk", None)
        _touch(True)
        return {"ok": True}
    row.fails = (row.fails or 0) + 1
    if row.fails >= MAX_FAILS:
        db().delete(row)
        db().commit()
        session.clear()
        return {"error": "Пять неверных попыток — вы вышли из аккаунта. Войдите заново.", "logout": True}, 401
    db().commit()
    left = MAX_FAILS - row.fails
    return {"error": f"Неверный код. Осталось попыток: {left}", "left": left}, 400


@app.post("/lock/now")
def lock_now():
    me, err = _me()
    if err:
        return err
    if pin_row(me.id):
        session["lk"] = True
    return {"ok": True}


@app.post("/lock/ping")
def lock_ping():
    """Человек работает в приложении — бездействие считается заново. Ответ — заблокировано ли уже."""
    me, err = _me()
    if err:
        return err
    if not app_mode() or not pin_row(me.id):
        return {"locked": False}
    if session.get("lk") or int(time.time()) - int(session.get("seen") or 0) > IDLE:
        session["lk"] = True
        return {"locked": True}
    _touch(True)
    return {"locked": False}


@app.post("/lock/pin")
def lock_pin():
    """Придумать код (первый запуск приложения) или сменить: для смены нужен текущий."""
    me, err = _me()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin") or "")
    if not _valid(pin):
        return {"error": "Код — ровно 6 цифр"}, 400
    if pin in ("000000", "123456", "111111", "654321", "121212"):
        return {"error": "Слишком простой код — придумайте другой"}, 400
    row = pin_row(me.id)
    if row:
        if session.get("lk"):
            return {"error": "Сначала разблокируйте приложение", "locked": True}, 423
        if not _check(str(data.get("current") or ""), row.pin_hash):
            row.fails = (row.fails or 0) + 1
            db().commit()
            return {"error": "Текущий код неверный"}, 400
        row.pin_hash, row.fails = _hash(pin), 0
    else:
        db().add(M.AppPin(member_id=me.id, device=device(), pin_hash=_hash(pin)))
    db().commit()
    session.pop("lk", None)
    _touch(True)
    return {"ok": True}


@app.post("/lock/off")
def lock_off():
    """Убрать код на этом устройстве (по текущему коду). Приложение снова попросит придумать его."""
    me, err = _me()
    if err:
        return err
    row = pin_row(me.id)
    if row:
        if not _check(str((request.get_json(silent=True) or {}).get("current") or ""), row.pin_hash):
            return {"error": "Текущий код неверный"}, 400
        db().delete(row)
        db().commit()
    return {"ok": True}


@app.context_processor
def _lock_context():
    return {"lock_mode": g.get("lock_mode", "")}
