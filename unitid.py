"""
Связь лаборатории с UNIT ID как сервиса UNIT (право accounts у приложения лаборатории).

* push_tags — теги, выданные в «Доступах», уходят в UNIT ID: теги общие для всех
  сервисов UNIT. Если UNIT ID не ответил, тег остаётся в лаборатории и уйдёт при
  следующей сверке.
* pull — раз в 2 минуты забираем аккаунты: имена, почты и теги у тех, кто уже
  есть в лаборатории, и новых людей с тегами, на которые есть правило.
* services — список сервисов UNIT для меню (из /api/services, кеш 10 минут).
* tokens — личные токены API лаборатории: выпускает и хранит UNIT ID (одна система
  токенов для всех сервисов), портал просит его служебным ключом от имени человека.
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List

from sqlalchemy import select

ISSUER = os.environ.get("LAB_ISSUER", "https://auth.labunit.ru").rstrip("/")
CLIENT_ID = os.environ.get("LAB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("LAB_CLIENT_SECRET", "")
PULL_EVERY = 120
_services = {"at": 0.0, "items": []}
_lock = threading.Lock()


def _call(method: str, path: str, data=None) -> dict:
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    req = urllib.request.Request(ISSUER + path, data=body, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode())
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.loads(response.read().decode() or "{}")


def enabled() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


class UnitIdError(Exception):
    """UNIT ID не ответил или отказал — понятным текстом."""


def _service(method: str, path: str, data=None) -> dict:
    if not enabled():
        raise UnitIdError("Портал не связан с UNIT ID — токены выпускать некому")
    try:
        return _call(method, path, data)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode() or "{}").get("error_description") or ""
        except ValueError:
            detail = ""
        if error.code in (401, 403):
            raise UnitIdError("UNIT ID не разрешил порталу выпускать токены — администратор UNIT ID "
                              "даёт приложению лаборатории право «tokens»") from error
        raise UnitIdError(detail or f"UNIT ID ответил ошибкой {error.code}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise UnitIdError("UNIT ID не ответил — попробуйте через минуту") from error


def tokens(user_id: str) -> list:
    """Токены человека, которые выпустила лаборатория: без самих токенов, только приметы."""
    return _service("GET", f"/api/service/tokens?user_id={urllib.parse.quote(user_id)}").get("tokens") or []


def token_create(user_id: str, name: str, scopes: List[str], days: int) -> dict:
    return _service("POST", "/api/service/tokens", {"user_id": user_id, "name": name, "scopes": list(scopes),
                                                     "days": int(days)})


def token_revoke(user_id: str, token_id: str) -> None:
    _service("POST", f"/api/service/tokens/{urllib.parse.quote(str(token_id))}/revoke", {"user_id": user_id})


def push_tags(member_id: str, add: List[str] = (), remove: List[str] = ()) -> bool:
    """Добавить/снять теги аккаунта в UNIT ID. → удалось ли."""
    if not enabled() or not (add or remove):
        return False
    try:
        _call("POST", f"/api/service/accounts/{member_id}", {"tags_add": list(add), "tags_remove": list(remove)})
        return True
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"[unitid] теги {member_id} не ушли в UNIT ID: {error}", flush=True)
        return False


def pull() -> int:
    """Сверить людей с UNIT ID. → сколько карточек изменилось."""
    if not enabled():
        return 0
    import access
    import models as M

    try:
        accounts = _call("GET", "/api/service/accounts").get("accounts") or []
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"[unitid] сверка не удалась: {error}", flush=True)
        return 0
    changed = 0
    with M.Session() as s:
        ruled = set(access.rules(s))
        # Первый раз: теги, которые лаборатория выдала до связи с UNIT ID (бывшие роли), — туда,
        # иначе сверка их бы стёрла
        if not s.get(M.Setting, "unitid_tags_seeded"):
            known = {a["id"]: set(a.get("tags") or []) for a in accounts}
            seeded = True
            for member in s.scalars(select(M.Member)):
                missing = [t for t in (member.tags or []) if t not in known.get(member.id, set())]
                if member.id in known and missing:
                    if push_tags(member.id, add=missing):
                        known[member.id] |= set(missing)
                    else:
                        seeded = False
            if not seeded:
                return 0                       # не всё ушло — сверим в следующий раз, ничего не стирая
            for acc in accounts:
                acc["tags"] = sorted(known.get(acc["id"], set(acc.get("tags") or [])))
            s.add(M.Setting(key="unitid_tags_seeded", value="1"))
        for acc in accounts:
            member = s.get(M.Member, acc["id"])
            tags = list(acc.get("tags") or [])
            if member is None:
                if acc.get("disabled") or not (set(tags) & ruled):
                    continue
                member = M.Member(id=acc["id"], name=acc.get("name") or acc["email"], email=acc.get("email") or "",
                                  role="member", tags=tags)
                s.add(member)
                changed += 1
                continue
            new_tags = [] if acc.get("disabled") else tags
            if (member.name, member.email, member.tags or []) != (acc.get("name") or member.name,
                                                                  acc.get("email") or member.email, new_tags):
                member.name = acc.get("name") or member.name
                member.email = acc.get("email") or member.email
                member.tags = new_tags
                changed += 1
        s.commit()
    return changed


def services() -> list:
    """Сервисы UNIT (название, адрес, значок) — для меню «Сервисы UNIT»."""
    if time.time() - _services["at"] < 600 and _services["items"]:
        return _services["items"]
    with _lock:
        try:
            with urllib.request.urlopen(ISSUER + "/api/services", timeout=5) as response:
                _services["items"] = json.loads(response.read().decode()).get("services") or []
        except (urllib.error.URLError, OSError, ValueError):
            pass
        _services["at"] = time.time()
    return _services["items"]


def _worker() -> None:
    time.sleep(10)
    while True:
        try:
            pull()
        except Exception as error:  # noqa: BLE001 — поток не должен умирать
            print(f"[unitid] {error}", flush=True)
        time.sleep(PULL_EVERY)


def start() -> None:
    if enabled() and os.environ.get("LAB_NO_WORKERS") != "1":
        threading.Thread(target=_worker, name="unitid-sync", daemon=True).start()
