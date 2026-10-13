"""
Связь лаборатории с UNIT ID как сервиса UNIT (право accounts у приложения лаборатории).

* push_tags — теги, выданные в «Доступах», уходят в UNIT ID: теги общие для всех
  сервисов UNIT. Если UNIT ID не ответил, тег остаётся в лаборатории и уйдёт при
  следующей сверке.
* pull — раз в 2 минуты забираем аккаунты: имена, почты и теги у тех, кто уже
  есть в лаборатории, и новых людей с тегами, на которые есть правило.
* services — список сервисов UNIT для меню (из /api/services, кеш 10 минут).
* sync_access — доступы (правила тегов, личные права) настраиваются только в UNIT ID;
  здесь их копия, сверка раз в 30 секунд.
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


def sync_member(member_id: str) -> bool:
    """Сверить одну карточку человека с UNIT ID по member_id."""
    if not enabled():
        return False
    import access
    import models as M

    try:
        acc = _call("GET", f"/api/service/accounts/{urllib.parse.quote(member_id)}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            with M.Session() as s:
                m = s.get(M.Member, member_id)
                if m:
                    m.tags = []
                    s.commit()
            return True
        print(f"[unitid] ошибка сверки {member_id}: {error}", flush=True)
        return False
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"[unitid] ошибка связи при сверке {member_id}: {error}", flush=True)
        return False

    with M.Session() as s:
        ruled = set(access.rules(s))
        member = s.get(M.Member, acc["id"])
        tags = list(acc.get("tags") or [])
        if member is None:
            if acc.get("disabled") or not (set(tags) & ruled):
                return False
            member = M.Member(id=acc["id"], name=acc.get("name") or acc.get("email", ""),
                              email=acc.get("email") or "", role="member", tags=tags)
            s.add(member)
            s.commit()
            return True
        new_tags = [] if acc.get("disabled") else tags
        if (member.name, member.email, member.tags or []) != (acc.get("name") or member.name,
                                                              acc.get("email") or member.email, new_tags):
            member.name = acc.get("name") or member.name
            member.email = acc.get("email") or member.email
            member.tags = new_tags
            s.commit()
            return True
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


# ─── Доступы: настраиваются только в UNIT ID ────────────────────────────────
# Лаборатория сообщает свой каталог прав и лимитов; в первый раз отдаёт и свои
# правила тегов и личные права (перенос). Дальше UNIT ID — единственное место, где их
# меняют, а лаборатория держит копию (TagRule, MemberAccess) — права проверяются
# мгновенно и без связи — и сверяет её раз в ACCESS_EVERY секунд по номеру версии.
ID_URL = os.environ.get("LAB_ID_URL", ISSUER).rstrip("/")
ACCESS_EVERY = 30
_access = {"version": None, "declared": False}


def access_url(path: str = "") -> str:
    """Адрес раздела «Доступы» лаборатории в UNIT ID."""
    return f"{ID_URL}/access/{urllib.parse.quote(CLIENT_ID)}{path}" if CLIENT_ID else f"{ID_URL}/access"


def _mirror(s, conf: dict) -> None:
    """Копия правил из UNIT ID — целиком заменяет локальные."""
    import models as M

    wanted = {r["tag"]: r for r in conf.get("rules") or []}
    for row in s.scalars(select(M.TagRule)):
        if row.tag not in wanted:
            s.delete(row)
    for tag, r in wanted.items():
        row = s.get(M.TagRule, tag) or M.TagRule(tag=tag)
        row.title, row.is_view, row.tone = r.get("title") or tag, bool(r.get("is_view")), r.get("tone") or "blue"
        row.privileges, row.limits, row.position = list(r.get("privileges") or []), dict(r.get("limits") or {}), int(r.get("position") or 0)
        s.add(row)
    personal = conf.get("personal") or {}
    for row in s.scalars(select(M.MemberAccess)):
        if row.member_id not in personal:
            s.delete(row)
    for uid, p in personal.items():
        if not s.get(M.Member, uid):
            continue                           # человека в лаборатории ещё нет — придёт со сверкой аккаунтов
        row = s.get(M.MemberAccess, uid) or M.MemberAccess(member_id=uid)
        row.allow, row.deny, row.limits = list(p.get("allow") or []), list(p.get("deny") or []), dict(p.get("limits") or {})
        s.add(row)


def sync_access(force: bool = False) -> bool:
    """Сообщить каталог (один раз за запуск) и забрать правила, если они менялись. → обновилось ли."""
    if not enabled():
        return False
    import access
    import models as M

    try:
        if not _access["declared"]:
            with M.Session() as s:
                seed_rules = [{"tag": r.tag, "title": r.title, "is_view": r.is_view, "privileges": r.privileges or [],
                               "limits": r.limits or {}, "tone": r.tone, "position": r.position}
                              for r in access.rules(s).values()]
                seed_personal = {a.member_id: {"allow": a.allow or [], "deny": a.deny or [], "limits": a.limits or {}}
                                 for a in s.scalars(select(M.MemberAccess))}
            catalog = [[group, [list(item) for item in items]] for group, items in access.CATALOG]
            limits = {k: [v[0], v[1]] for k, v in access.LIMITS.items()}
            conf = _call("POST", "/api/service/access", {"catalog": catalog, "limits": limits, "admin_key": "admin.access",
                                                         "seed_rules": seed_rules, "seed_personal": seed_personal,
                                                         "grants": dict(access.GRANTS)})
            _access["declared"] = True
        else:
            v = "" if force or _access["version"] is None else str(_access["version"])
            conf = _call("GET", "/api/service/access" + (f"?v={v}" if v else ""))
            if conf.get("same"):
                return False
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"[unitid] доступы не сверены: {error}", flush=True)
        return False
    with M.Session() as s:
        _mirror(s, conf)
        s.commit()
    _access["version"] = conf.get("version")
    return True


def access_person(member_id: str, allow, deny, limits) -> None:
    """Личные права человека — в UNIT ID (например, их меняет агент), затем копия здесь."""
    import models as M

    try:
        conf = _call("POST", f"/api/service/access/person/{urllib.parse.quote(member_id)}",
                     {"allow": list(allow), "deny": list(deny), "limits": dict(limits)})
    except urllib.error.HTTPError as error:
        raise UnitIdError(f"UNIT ID не принял права ({error.code})") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise UnitIdError("UNIT ID не ответил — попробуйте через минуту") from error
    with M.Session() as s:
        _mirror(s, conf)
        s.commit()
    _access["version"] = conf.get("version")


def _worker() -> None:
    time.sleep(10)
    tick = 0
    while True:
        try:
            if tick % max(1, PULL_EVERY // ACCESS_EVERY) == 0:
                pull()
            sync_access()
        except Exception as error:  # noqa: BLE001 — поток не должен умирать
            print(f"[unitid] {error}", flush=True)
        tick += 1
        time.sleep(ACCESS_EVERY)


def start() -> None:
    if enabled() and os.environ.get("LAB_NO_WORKERS") != "1":
        threading.Thread(target=_worker, name="unitid-sync", daemon=True).start()
