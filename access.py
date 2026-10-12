"""
Права в лаборатории: теги, правила тегов, личные исключения и лимиты.

Теги приходят из UNIT ID (у каждого аккаунта UNIT свои теги) и меняются здесь же
в разделе «Доступы» — изменения уходят обратно в UNIT ID. На тег вешается
правило: какие разделы открыты, какие действия можно и какие лимиты (например,
сколько можно потратить на ИИ). Правило бывает «кабинетом» — как в школе
кабинеты администратора, наставника и ученика: если у человека несколько
кабинетов, он переключается между ними, и портал показывает то, что открыто
выбранному кабинету. Теги-не-кабинеты (например, «лаборатория») добавляют
свои права к любому кабинету.

Итог для человека = права не-кабинетов ∪ права выбранного кабинета,
затем личные «разрешить» и «запретить». Лимиты — максимум по его тегам,
личный лимит заменяет. Нет ни одного права — человек ждёт подтверждения.
"""

from typing import Dict, List, Optional

from sqlalchemy import select

import models as M

# ─── Каталог прав ───────────────────────────────────────────────────────────
# (ключ, название, пояснение). Группы — для экрана настройки.
CATALOG = [
    ("Разделы", [
        ("page.home", "Сегодня", "Главная: свои задачи, проекты, итоги дня"),
        ("page.projects", "Проекты", "Список проектов и страницы проектов"),
        ("page.people", "Люди", "Сотрудники лаборатории и их профили"),
        ("page.teams", "Команды", "Команды лаборатории и их проекты"),
        ("page.time", "Учёт времени", "Свои часы и таймер"),
        ("page.workshop", "Мастерская", "Рабочие пространства проектов и наработки"),
        ("page.ai", "ИИ-агент", "Чат с агентом и его настройки"),
        ("page.pulse", "Пульс", "Сигналы, риски и загрузка своей зоны"),
        ("page.admin", "Доступы", "Теги, права и лимиты людей"),
    ]),
    ("Проекты", [
        ("project.create", "Создавать проекты", ""),
        ("project.see_team", "Видеть все проекты своей команды", "Включая закрытые"),
        ("project.see_all", "Видеть все проекты", "Включая закрытые"),
        ("project.edit_team", "Править проекты своей команды", "Настройки, части, команда проекта"),
        ("project.edit_all", "Править любые проекты", ""),
        ("project.delete", "Удалять проекты", ""),
        ("project.publish", "Показывать проекты на лендинге", ""),
        ("customer.view", "Режим заказчика", "Видны только свои проекты: готовность и новости для заказчика"),
    ]),
    ("Задачи и работа", [
        ("task.work", "Работать с задачами", "Создавать, двигать, комментировать задачи в своих проектах"),
        ("task.delete_any", "Удалять любые задачи", "Свои созданные задачи удалять можно всегда"),
        ("journal.moderate", "Удалять чужие записи и комментарии",
         "Журналы проектов и обсуждения задач; свои удалять можно всегда"),
        ("time.track", "Записывать время", ""),
        ("workshop.use", "Пользоваться мастерской", ""),
        ("git.link", "Привязывать git-аккаунт", "Репозитории в мастерской"),
    ]),
    ("Люди и команды", [
        ("people.progress", "Видеть прогресс людей", "Итоги дня, загрузку, часы и активность других"),
        ("people.manage", "Управлять людьми", "Грейды, должности, команды людей"),
        ("teams.manage", "Управлять командами", "Создавать, переименовывать и удалять команды"),
        ("time.see_all", "Видеть время всех", ""),
    ]),
    ("ИИ", [
        ("ai.use", "Пользоваться ИИ-агентом", ""),
        ("ai.pick_model", "Выбирать модель", "Иначе — модель, заданная лабораторией"),
        ("ai.auto", "Агент без подтверждений", "Режим «Авто»: агент меняет задачи и проекты, не спрашивая «Разрешить»"),
        ("ai.agent_admin", "Агент администрирует", "Агент может делать то же, что человек в «Доступах»"),
        ("ai.settings", "Настраивать ИИ лаборатории", "Ключ, модели, команда агента"),
    ]),
    ("Администрирование", [
        ("admin.access", "Менять теги, права и лимиты", ""),
        ("admin.overview", "Обзор лаборатории", "Состояние всех проектов и загрузка людей"),
    ]),
]
ALL = [key for _, items in CATALOG for key, _, _ in items]
NAMES = {key: name for _, items in CATALOG for key, name, _ in items}

# Лимиты: ключ → (название, единица, значение по умолчанию «без правила»)
LIMITS = {
    "ai_day_usd": ("ИИ в день", "$", 0.0),
    "ai_month_usd": ("ИИ в месяц", "$", 0.0),
}

# Правила «из коробки» — заводятся один раз; дальше их меняет руководитель
STAFF_BASE = ["page.home", "page.projects", "page.people", "page.teams", "page.time", "page.workshop", "page.ai",
              "page.pulse", "task.work", "time.track", "workshop.use", "git.link", "ai.use"]
DEFAULT_RULES = [
    ("руководитель лаборатории", "Руководитель лаборатории", True, ALL[:],
     {"ai_day_usd": 5.0, "ai_month_usd": 60.0}, "crimson"),
    ("лид команды", "Лид команды", True,
     STAFF_BASE + ["project.create", "project.see_team", "project.edit_team", "task.delete_any",
                   "people.progress", "ai.pick_model", "ai.auto"],
     {"ai_day_usd": 3.0, "ai_month_usd": 40.0}, "violet"),
    ("сотрудник", "Сотрудник", True, STAFF_BASE[:], {"ai_day_usd": 1.0, "ai_month_usd": 15.0}, "blue"),
    ("заказчик", "Заказчик", True, ["page.home", "page.projects", "customer.view"], {}, "teal"),
]
# Новые права появляются в уже настроенных правилах сами — один раз, у тех, у кого
# есть «право-якорь» (руководителю — всё новое, что касается управления)
GRANTS = {"journal.moderate": "admin.access", "ai.auto": "admin.access"}
LEGACY_ROLE_TAGS = {"head": "руководитель лаборатории", "lead": "лид команды", "member": "сотрудник",
                    "customer": "заказчик"}


def ensure_defaults(s) -> None:
    """Правила по умолчанию и перенос старых ролей в теги — один раз."""
    if s.scalar(select(M.TagRule.tag).limit(1)) is None:
        for i, (tag, title, view, privs, limits, tone) in enumerate(DEFAULT_RULES):
            s.add(M.TagRule(tag=tag, title=title, is_view=view, privileges=privs, limits=limits, tone=tone, position=i))
        s.flush()
    for member in s.scalars(select(M.Member)):
        if member.tags is None:                                  # карточка из прошлой версии: роль → теги
            tags = []
            for role in (member.roles or [member.role]):
                tag = LEGACY_ROLE_TAGS.get(role)
                if tag and tag not in tags:
                    tags.append(tag)
            member.tags = tags
    for key, anchor in GRANTS.items():
        flag = "grant:" + key
        if s.get(M.Setting, flag):
            continue
        for rule in s.scalars(select(M.TagRule)):
            privs = list(rule.privileges or [])
            if anchor in privs and key not in privs:
                rule.privileges = privs + [key]
        s.add(M.Setting(key=flag, value="1"))
    s.commit()


# ─── Вычисление ─────────────────────────────────────────────────────────────
def rules(s) -> Dict[str, "M.TagRule"]:
    return {r.tag: r for r in s.scalars(select(M.TagRule).order_by(M.TagRule.position, M.TagRule.tag))}


def resolve(s, member, wanted_view: str = "", all_rules: Optional[dict] = None) -> dict:
    """Права и лимиты человека. → {perms, limits, views, view}."""
    all_rules = all_rules if all_rules is not None else rules(s)
    tags = [t for t in (member.tags or []) if t in all_rules]
    views = [t for t in tags if all_rules[t].is_view]
    views.sort(key=lambda t: (all_rules[t].position, t))
    view = wanted_view if wanted_view in views else (views[0] if views else "")
    perms: set = set()
    limits: Dict[str, float] = {}
    for tag in tags:
        rule = all_rules[tag]
        if rule.is_view and tag != view:
            continue
        perms |= set(rule.privileges or [])
        for key, value in (rule.limits or {}).items():
            try:
                limits[key] = max(limits.get(key, 0.0), float(value))
            except (TypeError, ValueError):
                continue
    personal = s.get(M.MemberAccess, member.id)
    if personal:
        perms |= set(personal.allow or [])
        perms -= set(personal.deny or [])
        for key, value in (personal.limits or {}).items():
            if value not in (None, ""):
                try:
                    limits[key] = float(value)
                except (TypeError, ValueError):
                    continue
    return {"perms": perms & set(ALL), "limits": limits, "views": views, "view": view}


def can(member, key: str) -> bool:
    return bool(member) and key in getattr(member, "perms", set())


def is_customer(member) -> bool:
    """Режим заказчика: только свои проекты, без внутренней кухни."""
    return can(member, "customer.view") and not can(member, "task.work")


def is_staff(member) -> bool:
    return can(member, "task.work")


def pending(member) -> bool:
    return bool(member) and not getattr(member, "perms", set())


def view_title(s, tag: str) -> str:
    rule = s.get(M.TagRule, tag)
    return rule.title if rule and rule.title else tag


def people_with(s, key: str, all_rules: Optional[dict] = None) -> List["M.Member"]:
    """У кого есть право (в любом из кабинетов) — например, кому слать сигналы о блокерах."""
    all_rules = all_rules if all_rules is not None else rules(s)
    out = []
    for member in s.scalars(select(M.Member)):
        tags = [t for t in (member.tags or []) if t in all_rules]
        perms = set()
        for t in tags:
            perms |= set(all_rules[t].privileges or [])
        personal = s.get(M.MemberAccess, member.id)
        if personal:
            perms |= set(personal.allow or [])
            perms -= set(personal.deny or [])
        if key in perms:
            out.append(member)
    return out
