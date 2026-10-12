"""
Push-уведомления лаборатории (Web Push, VAPID) — как в школе UNIT.

Каждое уведомление портала (notice: новая задача, проверка, комментарий,
добавили в проект, помеха в итогах дня, сроки) уходит ещё и пушем на все
устройства, где человек разрешил уведомления — даже если портал закрыт.

Ключи VAPID создаются один раз: data/vapid_private.pem (0600). Содержимое
уведомления шифруется для конкретного устройства. Мёртвые подписки (404/410)
удаляются сами. Отправка — в фоновом потоке, чтобы не задерживать запрос.
"""

import base64
import json
import os
import threading
from datetime import datetime

from sqlalchemy import select

import models as M

SUBJECT = os.environ.get("LAB_SITE", "https://app.labunit.ru")
TTL = 6 * 60 * 60
_lock = threading.Lock()
_public = None


def _key_file() -> str:
    return os.path.join(M.DATA_DIR, "vapid_private.pem")


def available() -> bool:
    try:
        import pywebpush  # noqa: F401
        return True
    except ImportError:
        return False


def _vapid():
    from py_vapid import Vapid01

    with _lock:
        path = _key_file()
        if not os.path.exists(path):
            key = Vapid01()
            key.generate_keys()
            old = os.umask(0o077)
            try:
                key.save_key(path)
            finally:
                os.umask(old)
        return Vapid01.from_file(path)


def public_key() -> str:
    global _public
    if _public is None:
        from cryptography.hazmat.primitives import serialization

        raw = _vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        _public = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return _public


def subscribe(s, member_id: str, sub: dict, agent: str = "") -> bool:
    endpoint = str((sub or {}).get("endpoint") or "")
    keys = (sub or {}).get("keys") or {}
    p256dh, auth = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
    if not endpoint.startswith("https://") or len(endpoint) > 1000 or not p256dh or not auth:
        return False
    row = s.scalar(select(M.PushSub).where(M.PushSub.endpoint == endpoint)) or M.PushSub(endpoint=endpoint)
    row.member_id, row.p256dh, row.auth, row.agent = member_id, p256dh[:200], auth[:100], (agent or "")[:200]
    s.add(row)
    s.commit()
    return True


def unsubscribe(s, member_id: str, endpoint: str) -> None:
    row = s.scalar(select(M.PushSub).where(M.PushSub.endpoint == endpoint, M.PushSub.member_id == member_id))
    if row:
        s.delete(row)
        s.commit()


def count(s, member_id: str) -> int:
    return len(s.scalars(select(M.PushSub.id).where(M.PushSub.member_id == member_id)).all())


def send(member_id: str, title: str, body: str, url: str = "/home", tag: str = "") -> int:
    """На все устройства человека. → сколько дошло."""
    if not available():
        return 0
    from pywebpush import WebPushException, webpush

    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or url}, ensure_ascii=False)
    with M.Session() as s:
        subs = [(r.id, r.endpoint, r.p256dh, r.auth) for r in s.scalars(select(M.PushSub).where(M.PushSub.member_id == member_id))]
    if not subs:
        return 0
    delivered, dead = 0, []
    key = _vapid()
    for sub_id, endpoint, p256dh, auth in subs:
        try:
            webpush({"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}, data=payload,
                    vapid_private_key=key, vapid_claims={"sub": SUBJECT if SUBJECT.startswith("mailto:") or SUBJECT.startswith("https://") else "https://app.labunit.ru"},
                    ttl=TTL, timeout=10)
            delivered += 1
        except WebPushException as problem:
            if getattr(problem.response, "status_code", None) in (404, 410):
                dead.append(sub_id)
            else:
                print(f"[push] {endpoint[:50]}: {problem}", flush=True)
        except Exception as problem:  # noqa: BLE001 — сеть: попробуем со следующим уведомлением
            print(f"[push] {problem}", flush=True)
    with M.Session() as s:
        for sub_id in dead:
            row = s.get(M.PushSub, sub_id)
            if row:
                s.delete(row)
        if delivered:
            for sub_id, *_ in subs:
                row = s.get(M.PushSub, sub_id)
                if row:
                    row.last_ok = datetime.now()
        s.commit()
    return delivered


def send_later(member_id: str, title: str, body: str, url: str = "/home", tag: str = "") -> None:
    if os.environ.get("LAB_NO_WORKERS") == "1":
        return
    threading.Thread(target=send, args=(member_id, title, body, url, tag), daemon=True).start()
