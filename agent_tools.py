"""
Инструменты ИИ-агента лаборатории (agent.py).

Каждый инструмент работает от имени человека (g.me) и проверяет права теми же
функциями, что и страницы портала: can_see / is_team / can_edit, права из «Доступов».
Читающие инструменты выполняются сразу, меняющие в ручном режиме ждут «Разрешить»:
человек видит, что именно агент сделает, словами и с настоящими названиями.
Результат инструмента — {"text": для модели, "summary": строка в чате,
"widget": карточка в чате (необязательно), "link": куда ведёт}.
"""

import json
import math
import os
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional

from flask import g
from sqlalchemy import func, or_, select

import access
import agent
import ai
import models as M
from app import (PHASE_NAME, app, can_delete_task, can_edit, can_see, db, is_team, notice, notify, part_percent,
                 project_percent, set_column, team_ids, visible_projects, _delete_task)

ToolError = agent.ToolError
TOOLS: Dict[str, dict] = {}
COLUMNS = dict(M.TASK_COLUMNS)


def tool(name: str, label: str, doc: str, *, write: bool = False, perms=(), check: Optional[Callable] = None,
         main_only: bool = False, confirm: Optional[Callable] = None, touches: str = ""):
    def deco(fn):
        TOOLS[name] = {"name": name, "label": label, "doc": doc, "write": write, "perms": tuple(perms),
                       "check": check, "main_only": main_only, "confirm": confirm, "touches": touches, "fn": fn}
        return fn
    return deco


def allowed(spec: dict, person) -> bool:
    if spec["perms"] and not any(access.can(person, p) for p in spec["perms"]):
        return False
    return not spec["check"] or bool(spec["check"](person))


def doc(person, helper: bool = False) -> str:
    lines = []
    for spec in TOOLS.values():
        if helper and (spec["write"] or spec["main_only"]):
            continue
        if not allowed(spec, person):
            continue
        lines.append(f"- {spec['name']}{spec['doc']}" + (" [меняет данные]" if spec["write"] else ""))
    return "\n".join(lines)


def _short(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        text = v if isinstance(v, (int, float, bool)) or v is None else json.dumps(v, ensure_ascii=False) \
            if not isinstance(v, str) else v
        out[k] = text[:300] if isinstance(text, str) else text
    return out


def execute(lane, name: str, args: dict) -> str:
    """Выполнить вызов модели: права, подтверждение в ручном режиме, шаг в ленте. → текст для модели."""
    run = lane.run
    spec = TOOLS.get(name)
    item = {"type": "tool", "name": name, "label": spec["label"] if spec else name, "args": _short(args),
            "status": "running", "kind": "write" if spec and spec["write"] else "read"}
    if not spec:
        item.update(status="error", summary="такого инструмента нет")
        lane.add(item)
        return f"[{name}] ОШИБКА: такого инструмента нет — смотри список инструментов"
    if lane.helper and (spec["write"] or spec["main_only"]):
        item.update(status="error", summary="помощник только читает")
        lane.add(item)
        return f"[{name}] ОШИБКА: помощнику доступны только читающие инструменты"
    if not allowed(spec, g.me):
        item.update(status="error", summary="нет права")
        lane.add(item)
        return f"[{name}] ОШИБКА: у человека нет права на это — скажи ему, если это важно"
    lane.add(item)
    try:
        if spec["write"]:
            conf = spec["confirm"](args) if spec["confirm"] else {"text": spec["label"]}
            item["confirm"] = conf
            manual = run.mode != "auto" or not access.can(g.me, "ai.auto")
            if manual:
                with run.lock:
                    item["status"] = "awaiting"
                    item["preview"] = json.dumps(args, ensure_ascii=False, indent=1)[:2000]
                if not agent.wait_decision(lane, item):
                    with run.lock:
                        item["status"], item["summary"] = "denied", "отклонено"
                    return f"[{name}] ОТКЛОНЕНО человеком — не повторяй, пока он сам не попросит"
                with run.lock:
                    item["status"] = "running"
                run.touch()
        result = spec["fn"](lane, args or {})
        with run.lock:
            item.update(status="done", summary=str(result.get("summary") or "")[:300])
            if result.get("widget"):
                item["widget"] = result["widget"]
            if result.get("link"):
                item["link"] = result["link"]
        if spec["touches"]:
            run.touched.add(spec["touches"])
        text = str(result.get("text") or result.get("summary") or "готово")
        if len(text) > agent.RESULT_CHARS:
            text = text[:agent.RESULT_CHARS] + "\n…(обрезано — сузь запрос)"
        return f"[{name}] {text}"
    except ToolError as error:
        db().rollback()
        with run.lock:
            item.update(status="error", summary=str(error)[:300])
        return f"[{name}] ОШИБКА: {error}"
    except agent.Stopped:
        db().rollback()
        raise
    except Exception as error:                              # noqa: BLE001 — ошибка инструмента не роняет работу
        db().rollback()
        app.logger.exception("agent tool %s failed", name)
        with run.lock:
            item.update(status="error", summary="сбой инструмента")
        return f"[{name}] ОШИБКА: сбой инструмента ({type(error).__name__})"
    finally:
        run.touch()


# ─── Общее ──────────────────────────────────────────────────────────────────
def _int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _date(value, field: str = "срок") -> Optional[date]:
    if value in (None, "", False):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as error:
        raise ToolError(f"{field}: нужна дата ГГГГ-ММ-ДД, а не «{value}»") from error


def _names() -> dict:
    return {m.id: m.name for m in db().scalars(select(M.Member))}


def _project(pid, need: str = "see"):
    project = db().get(M.Project, _int(pid) or 0)
    if not project or not can_see(g.me, project):
        raise ToolError(f"Проект {pid} не найден или недоступен")
    if need == "team" and not is_team(g.me, project):
        raise ToolError(f"Вы не в команде проекта «{project.title}» — менять его задачи нельзя")
    if need == "edit" and not can_edit(g.me, project):
        raise ToolError(f"Нет прав менять проект «{project.title}»")
    return project


def _task(tid, need: str = "see"):
    task = db().get(M.Task, _int(tid) or 0)
    if not task:
        raise ToolError(f"Задача {tid} не найдена")
    project = _project(task.project_id, need)
    return task, project


def _member(mid):
    member = db().get(M.Member, str(mid or ""))
    if not member:
        raise ToolError(f"Человек {mid} не найден — id смотри в list_people")
    return member


def _who(mid, project) -> Optional[str]:
    """Исполнитель задачи: из команды проекта или его руководитель; «me» — сам человек."""
    if mid in (None, ""):
        return None
    mid = g.me.id if str(mid) == "me" else str(mid)
    if mid in team_ids(project.id) or mid == project.lead_id:
        return mid
    member = db().get(M.Member, mid)
    raise ToolError(f"{member.name if member else mid} не в команде проекта «{project.title}» — "
                    "сначала добавьте человека в команду")


def _task_url(task) -> str:
    return f"/p/{task.project_id}?task={task.id}#tasks"


def _fmt_task(t, names: dict, projects: Optional[dict] = None) -> str:
    bits = [f"[задача {t.id}] {t.title}", f"проект {t.project_id}" + (f" «{projects[t.project_id]}»" if projects
                                                                        and t.project_id in projects else ""),
            COLUMNS.get(t.column, t.column)]
    if t.assignee_id:
        bits.append("исполнитель " + names.get(t.assignee_id, t.assignee_id) + f" ({t.assignee_id})")
    if t.due:
        bits.append(f"срок {t.due.isoformat()}" + (" ПРОСРОЧЕНА" if t.due < date.today() and t.column != "done" else ""))
    if t.estimate:
        bits.append(f"оценка {t.estimate:g} ч")
    if t.priority == 1:
        bits.append("срочно")
    if t.checklist:
        bits.append(f"чек-лист {sum(1 for i in t.checklist if i.get('done'))}/{len(t.checklist)}")
    return " · ".join(bits)


def _table(columns, rows) -> dict:
    return {"type": "table", "columns": columns, "rows": rows[:40]}


# ═══ Читают ═════════════════════════════════════════════════════════════════
@tool("list_projects", "Смотрю проекты", "(query?, status?: idea|active|paused|done, mine?: bool) — проекты, "
      "которые видит человек: готовность, сроки, открытые и просроченные задачи", perms=("page.projects",))
def list_projects(lane, a):
    query = str(a.get("query") or "").lower().strip()
    rows = visible_projects(g.me)
    if a.get("status") in M.STATUSES:
        rows = [p for p in rows if p.status == a["status"]]
    if a.get("mine"):
        rows = [p for p in rows if is_team(g.me, p)]
    if query:
        rows = [p for p in rows if query in (p.title + " " + (p.summary or "") + " " + (p.customer or "")).lower()]
    names = _names()
    lines, table = [], []
    today = date.today()
    for p in rows[:60]:
        parts = db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all()
        pct = project_percent(parts)
        open_ = db().scalar(select(func.count()).select_from(M.Task).where(M.Task.project_id == p.id, M.Task.column != "done"))
        late = db().scalar(select(func.count()).select_from(M.Task).where(
            M.Task.project_id == p.id, M.Task.column != "done", M.Task.due < today))
        lines.append(f"[проект {p.id}] «{p.title}» — {M.STATUSES.get(p.status)}, готово {pct}%"
                     + (f", срок {p.due.isoformat()}" if p.due else ", без срока")
                     + (f", руководитель {names.get(p.lead_id, p.lead_id)}" if p.lead_id else "")
                     + f", открытых задач {open_}, просрочено {late}" + (", закрытый" if p.closed else ""))
        table.append({"title": p.title, "href": f"/p/{p.id}", "status": M.STATUSES.get(p.status), "progress": pct,
                      "due": p.due.strftime("%d.%m.%Y") if p.due else "—", "late": late})
    if not lines:
        return {"text": "Проектов не найдено", "summary": "ничего не нашлось"}
    return {"text": "\n".join(lines), "summary": f"проектов: {len(lines)}",
            "widget": _table([["title", "Проект"], ["status", "Статус"], ["progress", "Готово"], ["due", "Срок"],
                              ["late", "Просрочено"]], table)}


@tool("get_project", "Открываю проект", "(project_id) — проект целиком: описание, части и этапы, команда, задачи, "
      "свежий журнал, репозиторий", perms=("page.projects",))
def get_project(lane, a):
    p = _project(a.get("project_id"))
    names = _names()
    parts = db().scalars(select(M.Part).where(M.Part.project_id == p.id).order_by(M.Part.position)).all()
    pct = project_percent(parts)
    team = team_ids(p.id)
    lines = [f"[проект {p.id}] «{p.title}» — {M.STATUSES.get(p.status)}, готово {pct}%, приоритет {p.priority}",
             f"срок: {p.due.isoformat() if p.due else 'не задан'}; начало: {p.starts.isoformat() if p.starts else '—'}; "
             f"руководитель: {names.get(p.lead_id, p.lead_id or '—')}; заказчик: {p.customer or '—'}; "
             f"репозиторий: {p.repo_url or '—'}"]
    if p.summary or p.about:
        lines.append("Описание: " + (p.summary + "\n" + p.about).strip()[:3000])
    if parts:
        lines.append("Части:")
        lines += [f"- [часть {x.id}] {x.title} — {PHASE_NAME.get(x.phase)} {x.phase_progress}%, готово {part_percent(x)}%"
                  + (f", ответственный {names.get(x.owner_id, x.owner_id)}" if x.owner_id else "") for x in parts]
    if team:
        lines.append("Команда: " + ", ".join(f"{names.get(mid, mid)} ({mid}{', ' + r if r else ''})" for mid, r in team.items()))
    tasks = db().scalars(select(M.Task).where(M.Task.project_id == p.id)
                         .order_by(M.Task.column == "done", M.Task.due.is_(None), M.Task.due).limit(80)).all()
    if tasks:
        lines.append("Задачи:")
        lines += ["- " + _fmt_task(t, names) for t in tasks]
    ups = db().scalars(select(M.Update).where(M.Update.project_id == p.id).order_by(M.Update.created_at.desc()).limit(10)).all()
    if ups:
        lines.append("Журнал (свежие записи):")
        lines += [f"- [запись {u.id}] {u.created_at:%d.%m} {names.get(u.author_id, '')}: {u.text[:400]}" for u in ups]
    open_ = sum(1 for t in tasks if t.column != "done")
    late = sum(1 for t in tasks if t.column != "done" and t.due and t.due < date.today())
    widget = {"type": "project", "title": p.title, "url": f"/p/{p.id}", "status": M.STATUSES.get(p.status),
              "percent": pct, "due": p.due.strftime("%d.%m.%Y") if p.due else "", "open": open_, "late": late,
              "team": len(team), "parts": [{"title": x.title, "percent": part_percent(x),
                                            "phase": PHASE_NAME.get(x.phase)} for x in parts[:10]]}
    return {"text": "\n".join(lines), "summary": f"«{p.title}» · {pct}%", "widget": widget, "link": f"/p/{p.id}"}


@tool("list_tasks", "Смотрю задачи", "(project_id?, assignee_id? — id или \"me\", column?: todo|doing|review|done, "
      "query?, overdue?: bool, limit?) — задачи видимых проектов", perms=("page.projects", "task.work"))
def list_tasks(lane, a):
    projects = {p.id: p.title for p in visible_projects(g.me)}
    q = select(M.Task).where(M.Task.project_id.in_(list(projects) or [0]))
    if a.get("project_id"):
        q = q.where(M.Task.project_id == _project(a["project_id"]).id)
    if a.get("assignee_id"):
        q = q.where(M.Task.assignee_id == (g.me.id if a["assignee_id"] == "me" else str(a["assignee_id"])))
    if a.get("column") in COLUMNS:
        q = q.where(M.Task.column == a["column"])
    elif not a.get("project_id"):
        q = q.where(M.Task.column != "done")
    if a.get("overdue"):
        q = q.where(M.Task.due < date.today(), M.Task.column != "done")
    if a.get("query"):
        like = f"%{str(a['query']).strip()}%"
        q = q.where(or_(M.Task.title.ilike(like), M.Task.about.ilike(like)))
    limit = max(1, min(150, _int(a.get("limit")) or 80))
    rows = db().scalars(q.order_by(M.Task.due.is_(None), M.Task.due, M.Task.priority).limit(limit)).all()
    names = _names()
    if not rows:
        return {"text": "Задач не найдено", "summary": "ничего не нашлось"}
    table = [{"title": t.title, "href": _task_url(t), "project": projects.get(t.project_id, ""),
              "column": COLUMNS.get(t.column), "who": names.get(t.assignee_id, "—"),
              "due": t.due.strftime("%d.%m") if t.due else "—"} for t in rows]
    return {"text": "\n".join(_fmt_task(t, names, projects) for t in rows), "summary": f"задач: {len(rows)}",
            "widget": _table([["title", "Задача"], ["project", "Проект"], ["column", "Статус"], ["who", "Исполнитель"],
                              ["due", "Срок"]], table)}


@tool("get_task", "Открываю задачу", "(task_id) — задача целиком: описание, чек-лист, комментарии",
      perms=("page.projects", "task.work"))
def get_task(lane, a):
    t, p = _task(a.get("task_id"))
    names = _names()
    lines = [_fmt_task(t, names, {p.id: p.title}), f"этап: {PHASE_NAME.get(t.phase)}; приоритет {t.priority}; "
             f"создал {names.get(t.created_by, t.created_by or '—')} {t.created_at:%d.%m.%Y}"]
    if t.part_id:
        part = db().get(M.Part, t.part_id)
        if part:
            lines.append(f"часть: [часть {part.id}] {part.title}")
    if t.about:
        lines.append("Описание: " + t.about[:5000])
    if t.checklist:
        lines.append("Чек-лист: " + "; ".join(f"{i}. {'[x]' if c.get('done') else '[ ]'} {c.get('text')}"
                                             for i, c in enumerate(t.checklist)))
    comments = db().scalars(select(M.TaskComment).where(M.TaskComment.task_id == t.id)
                            .order_by(M.TaskComment.id.desc()).limit(20)).all()
    if comments:
        lines.append("Комментарии (свежие сверху):")
        lines += [f"- {c.created_at:%d.%m %H:%M} {names.get(c.author_id, '')}: {c.text[:600]}" for c in comments]
    return {"text": "\n".join(lines), "summary": t.title, "link": _task_url(t)}


@tool("list_people", "Смотрю людей", "(query?) — сотрудники: id, должность, команды, открытые задачи и загрузка",
      perms=("page.people",))
def list_people(lane, a):
    from control import load_of
    query = str(a.get("query") or "").lower().strip()
    teams = {d.id: d.title for d in db().scalars(select(M.Direction))}
    people = [m for m in access.people_with(db(), "task.work")]
    if query:
        people = [m for m in people if query in (m.name + " " + (m.position or "") + " " +
                                                 " ".join(s.get("name", "") for s in (m.skills or []))).lower()]
    people.sort(key=lambda m: m.name)
    load = load_of(people) if access.can(g.me, "people.progress") else {}
    lines, table = [], []
    for m in people[:80]:
        team_names = [teams[d] for d in m.all_direction_ids() if d in teams]
        info = load.get(m.id)
        lines.append(f"[человек {m.id}] {m.name}" + (f", {m.position}" if m.position else "")
                     + (f", команды: {', '.join(team_names)}" if team_names else "")
                     + (f", навыки: {', '.join(s.get('name', '') for s in (m.skills or [])[:6])}" if m.skills else "")
                     + (f", открытых задач {info['open']}, оценка {info['estimate']:g} ч, часов за неделю "
                        f"{info['hours']:g}, просрочено {info['late']}, состояние {info['state']}" if info else ""))
        table.append({"name": m.name, "href": f"/people/{m.id}", "position": m.position or "—",
                      "teams": ", ".join(team_names) or "—", "open": info["open"] if info else "—"})
    return {"text": "\n".join(lines) or "Никого не нашлось", "summary": f"людей: {len(lines)}",
            "widget": _table([["name", "Человек"], ["position", "Должность"], ["teams", "Команды"], ["open", "Задач"]], table)}


@tool("get_person", "Открываю профиль", "(member_id) — профиль человека; прогресс (задачи, часы, итоги дня) — "
      "если человек вправе его видеть", perms=("page.people",))
def get_person(lane, a):
    m = _member(g.me.id if a.get("member_id") == "me" else a.get("member_id"))
    teams = {d.id: d.title for d in db().scalars(select(M.Direction))}
    lines = [f"[человек {m.id}] {m.name}" + (f", {m.position}" if m.position else "") + (f", грейд {m.grade}" if m.grade else ""),
             "команды: " + (", ".join(teams[d] for d in m.all_direction_ids() if d in teams) or "—")]
    if m.skills:
        lines.append("навыки: " + ", ".join(f"{s.get('name')} {s.get('level')}/5" for s in m.skills))
    if m.about:
        lines.append("о себе: " + m.about[:1500])
    if m.id == g.me.id or access.can(g.me, "people.progress"):
        names = _names()
        tasks = db().scalars(select(M.Task).where(M.Task.assignee_id == m.id, M.Task.column != "done")
                             .order_by(M.Task.due.is_(None), M.Task.due).limit(40)).all()
        visible = {p.id: p.title for p in visible_projects(g.me)}
        tasks = [t for t in tasks if t.project_id in visible]
        lines.append(f"Открытые задачи ({len(tasks)}):")
        lines += ["- " + _fmt_task(t, names, visible) for t in tasks]
        week = date.today() - timedelta(days=6)
        hours = db().scalar(select(func.coalesce(func.sum(M.TimeEntry.hours), 0)).where(
            M.TimeEntry.member_id == m.id, M.TimeEntry.day >= week)) or 0
        lines.append(f"Часов за 7 дней: {float(hours):g}")
        checks = db().scalars(select(M.Checkin).where(M.Checkin.member_id == m.id)
                              .order_by(M.Checkin.day.desc()).limit(3)).all()
        for c in checks:
            lines.append(f"Итоги {c.day:%d.%m}: сделано — {c.done[:200]}; план — {c.plan[:200]}"
                         + (f"; мешает — {c.blockers[:200]}" if c.blockers else ""))
    return {"text": "\n".join(lines), "summary": m.name, "link": f"/people/{m.id}"}


@tool("list_teams", "Смотрю команды", "() — команды лаборатории: люди и проекты", perms=("page.teams",))
def list_teams(lane, a):
    names = _names()
    people = access.people_with(db(), "task.work")
    visible = visible_projects(g.me)
    lines = []
    for d in db().scalars(select(M.Direction).order_by(M.Direction.position, M.Direction.title)):
        members = [m for m in people if d.id in m.all_direction_ids()]
        projects = [p for p in visible if p.direction_id == d.id]
        lines.append(f"[команда {d.id}] {d.title}" + (f", лид {names.get(d.lead_id, d.lead_id)}" if d.lead_id else "")
                     + f"; людей {len(members)}: " + ", ".join(f"{m.name} ({m.id})" for m in members[:20])
                     + f"; проектов {len(projects)}: " + ", ".join(f"[{p.id}] {p.title}" for p in projects[:15]))
    return {"text": "\n".join(lines) or "Команд нет", "summary": f"команд: {len(lines)}"}


@tool("signals", "Смотрю сигналы", "() — что требует внимания: просрочки, задачи без исполнителя, застрявшие "
      "проверки, перегруз, помехи из итогов дня", perms=("task.work",))
def signals(lane, a):
    from control import signals_for
    rows = signals_for(g.me)
    lines = [f"- {s['text']}" + (f" ({s['project']})" if s.get("project") else "") + (f" → {s['link']}" if s.get("link") else "")
             for s in rows[:40]]
    return {"text": "\n".join(lines) or "Сигналов нет — всё спокойно", "summary": f"сигналов: {len(rows)}"}


@tool("time_report", "Смотрю часы", "(member_id? — по умолчанию сам человек, days?: 1–90) — учтённое время по "
      "проектам и дням", perms=("time.track", "time.see_all"))
def time_report(lane, a):
    mid = str(a.get("member_id") or g.me.id)
    if mid == "me":
        mid = g.me.id
    if mid != g.me.id and not access.can(g.me, "time.see_all"):
        raise ToolError("Чужое время видно только с правом «Видеть время всех»")
    days = max(1, min(90, _int(a.get("days")) or 7))
    since = date.today() - timedelta(days=days - 1)
    rows = db().scalars(select(M.TimeEntry).where(M.TimeEntry.member_id == mid, M.TimeEntry.day >= since)).all()
    projects = {p.id: p.title for p in db().scalars(select(M.Project))}
    by_project: Dict[str, float] = {}
    by_day: Dict[str, float] = {}
    for r in rows:
        key = projects.get(r.project_id, str(r.project_id))
        by_project[key] = by_project.get(key, 0) + r.hours
        by_day[r.day.isoformat()] = by_day.get(r.day.isoformat(), 0) + r.hours
    total = sum(r.hours for r in rows)
    lines = [f"Всего за {days} дн.: {total:g} ч", "По проектам: " + "; ".join(f"{k} — {v:g} ч" for k, v in
                                                                             sorted(by_project.items(), key=lambda x: -x[1])),
             "По дням: " + "; ".join(f"{k} — {v:g} ч" for k, v in sorted(by_day.items()))]
    return {"text": "\n".join(lines), "summary": f"{total:g} ч за {days} дн."}


@tool("search", "Ищу", "(query) — поиск по проектам, задачам, журналам и своим наработкам мастерской",
      perms=("page.projects", "task.work", "workshop.use"))
def search(lane, a):
    query = str(a.get("query") or "").strip()
    if len(query) < 2:
        raise ToolError("Слишком короткий запрос")
    like = f"%{query}%"
    projects = {p.id: p for p in visible_projects(g.me)}
    ids = list(projects) or [0]
    hits = []
    for p in projects.values():
        if query.lower() in (p.title + " " + (p.summary or "") + " " + (p.about or "")).lower():
            hits.append(f"[проект {p.id}] «{p.title}» → /p/{p.id}")
    for t in db().scalars(select(M.Task).where(M.Task.project_id.in_(ids), or_(M.Task.title.ilike(like),
                                                                             M.Task.about.ilike(like))).limit(30)):
        hits.append(f"[задача {t.id}] {t.title} (проект «{projects[t.project_id].title}») → {_task_url(t)}")
    for u in db().scalars(select(M.Update).where(M.Update.project_id.in_(ids), M.Update.text.ilike(like)).limit(15)):
        hits.append(f"[журнал проекта {u.project_id}, {u.created_at:%d.%m}] {u.text[:200]}")
    if access.can(g.me, "workshop.use"):
        for i in db().scalars(select(M.DeskItem).where(M.DeskItem.member_id == g.me.id, or_(
                M.DeskItem.title.ilike(like), M.DeskItem.body.ilike(like))).limit(15)):
            hits.append(f"[наработка {i.id}] {M.DESK_KINDS.get(i.kind)} «{i.title or 'без названия'}»")
    return {"text": "\n".join(hits) or "Ничего не нашлось", "summary": f"«{query}»: найдено {len(hits)}"}


@tool("my_rights", "Смотрю права", "() — права и лимиты человека на ИИ, расход сегодня и за месяц, режим работы")
def my_rights(lane, a):
    s = db()
    b = ai.budget(s, g.me)
    lines = ["Права: " + "; ".join(access.NAMES[p] for p in access.ALL if p in (g.me.perms or set()))]
    for ring in b["rings"]:
        lines.append(f"ИИ {ring['label']}: потрачено {ring['spent_label']}" +
                     (f" из {ring['limit_label']}" if ring["limit"] else " (без лимита)"))
    lines.append("Лимит исчерпан — работа идёт на дешёвой модели" if b["over"] else "Лимит не исчерпан")
    return {"text": "\n".join(lines), "summary": "права и лимиты"}


@tool("remember", "Запоминаю", "(text, replace_id?) — запомнить надолго важное о человеке и его работе "
      "(видно во всех чатах; он может стереть)", main_only=True)
def remember_tool(lane, a):
    entry = agent.remember(g.me.id, a.get("text"), str(a.get("replace_id") or ""))
    return {"text": f"Запомнил (id {entry['id']})", "summary": entry["text"][:120]}


@tool("run_team", "Поручаю помощникам", "(tasks: [{title, goal, model?}]) — раздать до 6 поручений помощникам "
      "(дешёвые модели, только читают, работают параллельно); вернутся их итоги — проверь их", main_only=True)
def run_team_tool(lane, a):
    text = agent.run_team(lane, a.get("tasks") if isinstance(a.get("tasks"), list) else [])
    return {"text": text, "summary": f"помощников: {len(a.get('tasks') or [])}"}


# ─── Мастерская ─────────────────────────────────────────────────────────────
def _desk_item(item_id, own: bool = True):
    import desk
    item = db().get(M.DeskItem, _int(item_id) or 0)
    if not item or (own and item.member_id != g.me.id) or (not own and not desk._can_read(item)):
        raise ToolError(f"Наработка {item_id} не найдена")
    return item


@tool("workshop", "Смотрю мастерскую", "() — мастерская человека: наработки (id, тип, название, проект, закреп, "
      "цвет, размер) и окна на столе (инструмент, наработка, x/y/w/h — доли стола 0..1)", perms=("workshop.use",))
def workshop(lane, a):
    import desk
    projects = {p.id: p.title for p in visible_projects(g.me)}
    items = db().scalars(select(M.DeskItem).where(M.DeskItem.member_id == g.me.id)
                         .order_by(M.DeskItem.pinned.desc(), M.DeskItem.updated_at.desc()).limit(150)).all()
    lines = ["Наработки:"]
    for i in items:
        size = len(i.body or "") if i.kind != "checklist" else len((i.data or {}).get("items") or [])
        lines.append(f"- [наработка {i.id}] {M.DESK_KINDS.get(i.kind)} «{i.title or 'без названия'}»"
                     + (f", проект {i.project_id} «{projects.get(i.project_id, '')}»" if i.project_id else "")
                     + (", закреплена" if i.pinned else "") + (f", цвет {i.color}" if i.color else "")
                     + (", видна команде" if i.shared else "")
                     + (f", пунктов {size}" if i.kind == "checklist" else f", знаков {size}")
                     + f", изменена {i.updated_at:%d.%m}")
    layout = desk.layout_of(g.me.id)
    lines.append(f"Название стола: «{layout['title'] or 'Мастерская'}»")
    lines.append("Окна на столе (инструменты: " + ", ".join(f"{k} — {v}" for k, v in desk.TOOLS.items()) + "):")
    for w in layout["wins"]:
        lines.append(f"- {w['tool']}" + (f" наработка {w['item_id']}" if w["item_id"] else "")
                     + f": x {w['x']:.2f}, y {w['y']:.2f}, w {w['w']:.2f}, h {w['h']:.2f}"
                     + (", свёрнуто" if w["min"] else "") + (", развёрнуто" if w["max"] else ""))
    if not layout["wins"]:
        lines.append("- окон нет")
    return {"text": "\n".join(lines), "summary": f"наработок {len(items)}, окон {len(layout['wins'])}"}


@tool("read_item", "Читаю наработку", "(item_id) — содержимое наработки: текст заметки или кода, пункты чек-листа, "
      "ссылка", perms=("workshop.use",))
def read_item(lane, a):
    i = _desk_item(a.get("item_id"), own=False)
    data = i.data or {}
    if i.kind == "checklist":
        body = "\n".join(f"{n}. {'[x]' if c.get('done') else '[ ]'} {c.get('text')}" for n, c in enumerate(data.get("items") or []))
    elif i.kind == "link":
        body = f"ссылка: {data.get('url', '')}\n{i.body or ''}"
    elif i.kind == "file":
        body = f"файл «{data.get('name', i.title)}», {int(data.get('size') or 0) // 1024} КБ — содержимое файлов агент не читает"
    else:
        body = (i.body or "")[:20000]
    return {"text": f"{M.DESK_KINDS.get(i.kind)} «{i.title}»" + (f" ({data.get('lang')})" if data.get("lang") else "")
            + f":\n{body}", "summary": i.title or M.DESK_KINDS.get(i.kind)}


# ─── Администрирование ──────────────────────────────────────────────────────
def _admin(person) -> bool:
    return access.can(person, "ai.agent_admin") and access.can(person, "admin.access")


@tool("access_overview", "Смотрю доступы", "() — теги и их права и лимиты, у кого какие теги", check=_admin)
def access_overview(lane, a):
    rules = access.rules(db())
    lines = ["Теги:"]
    for r in rules.values():
        lines.append(f"- #{r.tag} «{r.title}»" + (" (кабинет)" if r.is_view else "") + ": "
                     + ", ".join(r.privileges or []) + (f"; лимиты {r.limits}" if r.limits else ""))
    lines.append("Люди:")
    for m in db().scalars(select(M.Member).order_by(M.Member.name)):
        personal = db().get(M.MemberAccess, m.id)
        lines.append(f"- {m.name} ({m.id}): " + ", ".join("#" + t for t in (m.tags or [])) +
                     (f"; лично разрешено {personal.allow}, запрещено {personal.deny}, лимиты {personal.limits}"
                      if personal and (personal.allow or personal.deny or personal.limits) else ""))
    lines.append("Ключи прав: " + ", ".join(access.ALL))
    return {"text": "\n".join(lines), "summary": f"тегов {len(rules)}"}


# ═══ Меняют ═════════════════════════════════════════════════════════════════
def _c_create_task(a):
    p = _project(a.get("project_id"), "team")
    title = str(a.get("title") or "").strip()
    if not title:
        raise ToolError("Нет названия задачи")
    who = _who(a.get("assignee_id"), p)
    rows = [("Проект", p.title)]
    if who:
        rows.append(("Исполнитель", _member(who).name))
    if a.get("due"):
        rows.append(("Срок", _date(a["due"]).strftime("%d.%m.%Y")))
    if a.get("estimate"):
        rows.append(("Оценка", f"{a['estimate']} ч"))
    if a.get("about"):
        rows.append(("Описание", str(a["about"])[:300]))
    if isinstance(a.get("checklist"), list) and a["checklist"]:
        rows.append(("Чек-лист", f"{len(a['checklist'])} пунктов"))
    return {"text": f"Создать задачу «{title[:120]}»", "rows": rows}


@tool("create_task", "Создаю задачу", "(project_id, title, about?, assignee_id?, due? ГГГГ-ММ-ДД, estimate? ч, "
      "priority? 1 срочно|2|3, part_id?, phase?: plan|build|test|release, checklist?: [строки])",
      write=True, perms=("task.work",), confirm=_c_create_task)
def create_task(lane, a):
    p = _project(a.get("project_id"), "team")
    task = M.Task(project_id=p.id, title=str(a["title"]).strip()[:300], about=str(a.get("about") or "")[:5000],
                  created_by=g.me.id, priority=max(1, min(3, _int(a.get("priority")) or 2)))
    task.assignee_id = _who(a.get("assignee_id"), p)
    task.due = _date(a.get("due"))
    try:
        task.estimate = max(0.0, float(a.get("estimate") or 0))
    except (TypeError, ValueError):
        task.estimate = 0
    if a.get("part_id"):
        part = db().get(M.Part, _int(a["part_id"]) or 0)
        if not part or part.project_id != p.id:
            raise ToolError(f"Части {a['part_id']} нет в проекте «{p.title}»")
        task.part_id = part.id
    if a.get("phase") in PHASE_NAME:
        task.phase = a["phase"]
    if isinstance(a.get("checklist"), list):
        task.checklist = [{"text": str(i)[:200], "done": False} for i in a["checklist"] if str(i).strip()][:60]
    db().add(task)
    db().commit()
    if task.assignee_id:
        notify(task.assignee_id, f"Новая задача: {task.title}", f"Проект «{p.title}». Срок: {task.due or 'не указан'}.",
               _task_url(task), kind="task")
    return {"text": f"Задача {task.id} «{task.title}» создана → {_task_url(task)}", "summary": task.title,
            "link": _task_url(task)}


def _c_update_task(a):
    t, p = _task(a.get("task_id"), "team")
    rows = []
    if a.get("title"):
        rows.append(("Название", f"{t.title} → {a['title']}"))
    if a.get("column") in COLUMNS and a["column"] != t.column:
        rows.append(("Статус", f"{COLUMNS[t.column]} → {COLUMNS[a['column']]}"))
    if "assignee_id" in a:
        who = _who(a["assignee_id"], p)
        rows.append(("Исполнитель", (_member(who).name if who else "никого")))
    if "due" in a:
        d = _date(a["due"])
        rows.append(("Срок", d.strftime("%d.%m.%Y") if d else "без срока"))
    if "estimate" in a:
        rows.append(("Оценка", f"{a['estimate']} ч"))
    if "priority" in a:
        rows.append(("Приоритет", str(a["priority"])))
    if "about" in a:
        rows.append(("Описание", str(a["about"])[:300]))
    if not rows:
        raise ToolError("Нечего менять — укажи поля")
    return {"text": f"Изменить задачу «{t.title}» ({p.title})", "rows": rows}


@tool("update_task", "Меняю задачу", "(task_id, title?, about?, column?: todo|doing|review|done, assignee_id? "
      "(\"\" — снять), due? (\"\" — снять), estimate?, priority?, part_id?)", write=True, perms=("task.work",),
      confirm=_c_update_task)
def update_task(lane, a):
    t, p = _task(a.get("task_id"), "team")
    before = t.assignee_id
    if a.get("title"):
        t.title = str(a["title"]).strip()[:300]
    if "about" in a:
        t.about = str(a["about"] or "")[:5000]
    if "assignee_id" in a:
        t.assignee_id = _who(a["assignee_id"], p)
    if "due" in a:
        t.due = _date(a["due"])
    if "estimate" in a:
        try:
            t.estimate = max(0.0, float(a["estimate"] or 0))
        except (TypeError, ValueError):
            pass
    if "priority" in a and _int(a["priority"]):
        t.priority = max(1, min(3, _int(a["priority"])))
    if "part_id" in a:
        part = db().get(M.Part, _int(a["part_id"]) or 0) if a["part_id"] else None
        if a["part_id"] and (not part or part.project_id != p.id):
            raise ToolError(f"Части {a['part_id']} нет в проекте")
        t.part_id = part.id if part else None
    if a.get("column") in COLUMNS and a["column"] != t.column:
        set_column(t, a["column"], p)
    db().commit()
    if t.assignee_id and t.assignee_id != before:
        notify(t.assignee_id, f"Новая задача: {t.title}", f"Проект «{p.title}».", _task_url(t), kind="task")
    return {"text": f"Задача {t.id} обновлена → {_task_url(t)}", "summary": t.title, "link": _task_url(t)}


def _c_delete_task(a):
    t, p = _task(a.get("task_id"), "team")
    if not can_delete_task(t, p):
        raise ToolError("Эту задачу удалить может её автор или руководитель проекта")
    return {"text": f"Удалить задачу «{t.title}» из проекта «{p.title}»",
            "rows": [("Внимание", "комментарии к задаче тоже удалятся")]}


@tool("delete_task", "Удаляю задачу", "(task_id)", write=True, perms=("task.work",), confirm=_c_delete_task)
def delete_task(lane, a):
    t, p = _task(a.get("task_id"), "team")
    if not can_delete_task(t, p):
        raise ToolError("Эту задачу удалить может её автор или руководитель проекта")
    title = t.title
    _delete_task(t)
    db().commit()
    return {"text": f"Задача «{title}» удалена", "summary": title, "link": f"/p/{p.id}#tasks"}


def _c_comment(a):
    t, p = _task(a.get("task_id"), "team")
    text = str(a.get("text") or "").strip()
    if not text:
        raise ToolError("Пустой комментарий")
    return {"text": f"Написать комментарий к задаче «{t.title}»", "rows": [("Текст", text[:400])]}


@tool("comment_task", "Комментирую задачу", "(task_id, text) — комментарий от имени человека (исполнитель и "
      "автор получат уведомление)", write=True, perms=("task.work",), confirm=_c_comment)
def comment_task(lane, a):
    t, p = _task(a.get("task_id"), "team")
    text = str(a.get("text") or "").strip()[:3000]
    db().add(M.TaskComment(task_id=t.id, author_id=g.me.id, text=text))
    db().commit()
    for mid in {t.assignee_id, t.created_by, p.lead_id} - {None, "", g.me.id}:
        notice(mid, f"{g.me.name} о задаче «{t.title}»: {text[:120]}", _task_url(t), "task")
    return {"text": "Комментарий добавлен", "summary": t.title, "link": _task_url(t)}


def _c_checklist(a):
    t, _ = _task(a.get("task_id"), "team")
    rows = []
    if a.get("add"):
        rows.append(("Добавить", "; ".join(str(x) for x in a["add"])[:300]))
    for key, word in (("done", "Отметить"), ("undone", "Снять отметку"), ("remove", "Убрать")):
        if a.get(key):
            names = [str((t.checklist or [])[i].get("text")) for i in a[key] if isinstance(i, int) and 0 <= i < len(t.checklist or [])]
            rows.append((word, "; ".join(names)[:300]))
    if not rows:
        raise ToolError("Нечего менять в чек-листе")
    return {"text": f"Изменить чек-лист задачи «{t.title}»", "rows": rows}


@tool("checklist_task", "Меняю чек-лист", "(task_id, add?: [строки], done?: [номера], undone?: [номера], "
      "remove?: [номера]) — номера пунктов с 0", write=True, perms=("task.work",), confirm=_c_checklist)
def checklist_task(lane, a):
    t, _ = _task(a.get("task_id"), "team")
    items = [dict(i) for i in (t.checklist or [])]
    for i in a.get("done") or []:
        if isinstance(i, int) and 0 <= i < len(items):
            items[i]["done"] = True
    for i in a.get("undone") or []:
        if isinstance(i, int) and 0 <= i < len(items):
            items[i]["done"] = False
    for i in sorted([i for i in (a.get("remove") or []) if isinstance(i, int) and 0 <= i < len(items)], reverse=True):
        items.pop(i)
    for text in a.get("add") or []:
        if str(text).strip():
            items.append({"text": str(text).strip()[:200], "done": False})
    t.checklist = items[:60]
    db().commit()
    return {"text": "Чек-лист: " + "; ".join(f"{n}. {'[x]' if c['done'] else '[ ]'} {c['text']}" for n, c in enumerate(items)),
            "summary": f"{sum(1 for c in items if c['done'])}/{len(items)}", "link": _task_url(t)}


def _c_part(a):
    p = _project(a.get("project_id"), "edit")
    if not str(a.get("title") or "").strip():
        raise ToolError("Нет названия части")
    return {"text": f"Добавить часть «{a['title']}» в проект «{p.title}»"}


@tool("create_part", "Добавляю часть проекта", "(project_id, title, owner_id?) — модуль, сервис или исследование "
      "со своей готовностью", write=True, perms=("task.work", "project.edit_team", "project.edit_all"), confirm=_c_part)
def create_part(lane, a):
    p = _project(a.get("project_id"), "edit")
    position = (db().scalar(select(func.max(M.Part.position)).where(M.Part.project_id == p.id)) or 0) + 1
    part = M.Part(project_id=p.id, title=str(a["title"]).strip()[:200], position=position)
    if a.get("owner_id"):
        part.owner_id = _who(a["owner_id"], p)
    db().add(part)
    db().commit()
    return {"text": f"Часть {part.id} «{part.title}» добавлена", "summary": part.title, "link": f"/p/{p.id}#parts"}


def _c_project(a):
    p = _project(a.get("project_id"), "edit")
    rows = []
    if a.get("status") in M.STATUSES:
        rows.append(("Статус", f"{M.STATUSES[p.status]} → {M.STATUSES[a['status']]}"))
    if "due" in a:
        d = _date(a["due"])
        rows.append(("Срок", d.strftime("%d.%m.%Y") if d else "без срока"))
    if "priority" in a:
        rows.append(("Приоритет", str(a["priority"])))
    if "summary" in a:
        rows.append(("Кратко", str(a["summary"])[:300]))
    if not rows:
        raise ToolError("Нечего менять — укажи поля")
    return {"text": f"Изменить проект «{p.title}»", "rows": rows}


@tool("update_project", "Меняю проект", "(project_id, status?: idea|active|paused|done, due?, priority? 1–3, summary?)",
      write=True, perms=("task.work", "project.edit_team", "project.edit_all"), confirm=_c_project)
def update_project(lane, a):
    p = _project(a.get("project_id"), "edit")
    if a.get("status") in M.STATUSES:
        p.status = a["status"]
    if "due" in a:
        p.due = _date(a["due"])
    if "priority" in a and _int(a["priority"]):
        p.priority = max(1, min(3, _int(a["priority"])))
    if "summary" in a:
        p.summary = str(a["summary"] or "")[:400]
    db().commit()
    return {"text": f"Проект «{p.title}» обновлён", "summary": p.title, "link": f"/p/{p.id}"}


def _c_journal(a):
    p = _project(a.get("project_id"), "team")
    text = str(a.get("text") or "").strip()
    if not text:
        raise ToolError("Пустая запись")
    rows = [("Текст", text[:400])]
    if a.get("for_customer"):
        rows.append(("Заказчик", "увидит запись и получит уведомление"))
    return {"text": f"Записать в журнал проекта «{p.title}»", "rows": rows}


@tool("journal_add", "Пишу в журнал", "(project_id, text, for_customer?: bool) — запись в журнал проекта от имени "
      "человека", write=True, perms=("task.work",), confirm=_c_journal)
def journal_add(lane, a):
    p = _project(a.get("project_id"), "team")
    text = str(a.get("text") or "").strip()[:4000]
    db().add(M.Update(project_id=p.id, author_id=g.me.id, text=text, for_customer=bool(a.get("for_customer"))))
    db().commit()
    if a.get("for_customer"):
        for mid in team_ids(p.id):
            member = db().get(M.Member, mid)
            if member and "customer.view" in access.resolve(db(), member)["perms"]:
                notify(mid, f"Новости по проекту «{p.title}»", text, f"/p/{p.id}")
    return {"text": "Запись добавлена", "summary": p.title, "link": f"/p/{p.id}#journal"}


def _c_desk_add(a):
    kind = a.get("kind") if a.get("kind") in ("note", "checklist", "code", "link") else "note"
    if a.get("project_id"):
        _project(a["project_id"], "team")
    rows = [("Тип", M.DESK_KINDS[kind])]
    if kind == "checklist":
        rows.append(("Пунктов", str(len(a.get("items") or []))))
    if kind == "link":
        rows.append(("Ссылка", str(a.get("url") or "")[:200]))
    return {"text": f"Положить в мастерскую: {M.DESK_KINDS[kind].lower()} «{str(a.get('title') or 'без названия')[:100]}»",
            "rows": rows}


@tool("desk_add", "Кладу в мастерскую", "(kind: note|checklist|code|link, title, body?, items?: [строки] для "
      "checklist, url? для link, lang? для code, project_id?)", write=True, perms=("workshop.use",),
      confirm=_c_desk_add, touches="desk")
def desk_add(lane, a):
    import desk
    kind = a.get("kind") if a.get("kind") in ("note", "checklist", "code", "link") else "note"
    item = M.DeskItem(member_id=g.me.id, kind=kind, title=str(a.get("title") or "").strip()[:200],
                      body=str(a.get("body") or "")[:200000],
                      project_id=_project(a["project_id"], "team").id if a.get("project_id") else None)
    if kind == "checklist":
        item.data = {"items": [{"text": str(t).strip()[:300], "done": False} for t in (a.get("items") or []) if str(t).strip()][:200]}
    elif kind == "code":
        item.data = {"lang": a.get("lang") if a.get("lang") in desk.LANGS else "python"}
    elif kind == "link":
        url = str(a.get("url") or "").strip()
        if not url.startswith(("https://", "http://")):
            raise ToolError("Ссылка должна начинаться с https://")
        item.data = {"url": url[:1000]}
    db().add(item)
    db().commit()
    return {"text": f"Наработка {item.id} создана", "summary": item.title or M.DESK_KINDS[kind], "link": "/desk"}


def _c_desk_update(a):
    i = _desk_item(a.get("item_id"))
    rows = []
    for key, word in (("title", "Название"), ("pinned", "Закрепить"), ("color", "Цвет"), ("shared", "Показать команде"),
                      ("project_id", "Проект")):
        if key in a:
            rows.append((word, str(a[key])))
    if "body" in a:
        rows.append(("Текст", f"заменить ({len(str(a['body']))} знаков)"))
    if "items" in a:
        rows.append(("Пункты", f"заменить ({len(a.get('items') or [])})"))
    if a.get("project_id"):
        _project(a["project_id"], "team")
    if not rows:
        raise ToolError("Нечего менять")
    return {"text": f"Изменить наработку «{i.title or M.DESK_KINDS.get(i.kind)}»", "rows": rows}


@tool("desk_update", "Меняю наработку", "(item_id, title?, body?, items?: [строки], pinned?: bool, color?: "
      "\"\"|blue|teal|amber|rose|violet, shared?: bool, project_id? (\"\" — отвязать))", write=True,
      perms=("workshop.use",), confirm=_c_desk_update, touches="desk")
def desk_update(lane, a):
    import desk
    i = _desk_item(a.get("item_id"))
    if "title" in a:
        i.title = str(a["title"] or "").strip()[:200]
    if "body" in a and i.kind in ("note", "code", "link"):
        i.body = str(a["body"] or "")[:200000]
    if "items" in a and i.kind == "checklist":
        old = {c.get("text"): c.get("done") for c in (i.data or {}).get("items") or []}
        i.data = dict(i.data or {}, items=[{"text": str(t).strip()[:300], "done": bool(old.get(str(t).strip()))}
                                           for t in (a.get("items") or []) if str(t).strip()][:200])
    if "pinned" in a:
        i.pinned = bool(a["pinned"])
    if "color" in a and a["color"] in desk.COLORS:
        i.color = a["color"]
    if "project_id" in a:
        i.project_id = _project(a["project_id"], "team").id if a["project_id"] else None
    if "shared" in a:
        i.shared = bool(a["shared"]) and bool(i.project_id)
    i.updated_at = datetime.now()
    db().commit()
    return {"text": f"Наработка {i.id} обновлена", "summary": i.title or M.DESK_KINDS.get(i.kind), "link": "/desk"}


def _c_desk_delete(a):
    i = _desk_item(a.get("item_id"))
    return {"text": f"Удалить из мастерской {M.DESK_KINDS.get(i.kind, '').lower()} «{i.title or 'без названия'}»"}


@tool("desk_delete", "Удаляю наработку", "(item_id)", write=True, perms=("workshop.use",), confirm=_c_desk_delete,
      touches="desk")
def desk_delete(lane, a):
    import desk
    i = _desk_item(a.get("item_id"))
    title, path = i.title, (i.data or {}).get("path")
    db().delete(i)
    db().commit()
    if path:
        try:
            os.remove(os.path.join(desk.FILES, path))
        except OSError:
            pass
    return {"text": f"Наработка «{title}» удалена", "summary": title or "наработка"}


def _preset(wins: List[dict], preset: str) -> List[dict]:
    """Расстановка окон: grid — плитка, columns — колонки, focus — главное окно слева, cascade — каскад."""
    shown = [w for w in wins if not w.get("min")]
    hidden = [w for w in wins if w.get("min")]
    n = len(shown)
    if not n:
        return wins
    for w in shown:
        w["max"] = False
    if preset == "columns":
        cols = min(n, 4)
        for i, w in enumerate(shown):
            if i < cols:
                w.update(x=i / cols, y=0, w=1 / cols, h=1)
            else:
                w["min"] = True
    elif preset == "focus" and n > 1:
        shown[0].update(x=0, y=0, w=0.62, h=1)
        rest = shown[1:]
        for i, w in enumerate(rest):
            w.update(x=0.62, y=i / len(rest), w=0.38, h=1 / len(rest))
    elif preset == "cascade":
        for i, w in enumerate(shown):
            w.update(x=0.04 + (i % 6) * 0.04, y=0.04 + (i % 6) * 0.05, w=0.5, h=0.62)
    else:
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        for i, w in enumerate(shown):
            w.update(x=(i % cols) / cols, y=(i // cols) / rows, w=1 / cols, h=1 / rows)
    return shown + hidden


def _c_arrange(a):
    import desk
    wins = a.get("windows")
    count = len(wins) if isinstance(wins, list) else len(desk.layout_of(g.me.id)["wins"])
    words = {"grid": "плиткой", "columns": "колонками", "focus": "главное окно слева", "cascade": "каскадом"}
    rows = [("Окон", str(count))]
    if a.get("preset") in words:
        rows.append(("Как", words[a["preset"]]))
    if a.get("title"):
        rows.append(("Название стола", str(a["title"])[:120]))
    return {"text": "Переставить окна в мастерской", "rows": rows}


@tool("arrange_workshop", "Расставляю окна", "(windows?: [{tool, item_id?, x, y, w, h, min?, max?}], preset?: "
      "grid|columns|focus|cascade, title?) — раскладка стола: свои окна (доли 0..1) и/или готовая расстановка; "
      "без windows — переставляются уже открытые", write=True, perms=("workshop.use",), confirm=_c_arrange,
      touches="desk")
def arrange_workshop(lane, a):
    import desk
    current = desk.layout_of(g.me.id)
    wins = a.get("windows") if isinstance(a.get("windows"), list) else current["wins"]
    wins = desk.clean_layout({"wins": wins})["wins"]
    for i, w in enumerate(wins):
        w["z"] = i + 1
    if a.get("preset"):
        wins = _preset(wins, str(a["preset"]))
    saved = desk.save_layout(g.me.id, {"wins": wins, "title": a.get("title") or current["title"]})
    return {"text": f"Окна расставлены: {len(saved['wins'])}", "summary": f"окон: {len(saved['wins'])}", "link": "/desk"}


# ─── Администрирование (право ai.agent_admin вместе с «Доступами») ───────────
def _c_tags(a):
    m = _member(a.get("member_id"))
    add = [str(t) for t in a.get("add") or []]
    remove = [str(t) for t in a.get("remove") or []]
    if not add and not remove:
        raise ToolError("Укажи add или remove")
    rows = [("Человек", m.name)]
    if add:
        rows.append(("Добавить теги", ", ".join("#" + t for t in add)))
    if remove:
        rows.append(("Снять теги", ", ".join("#" + t for t in remove)))
    return {"text": f"Изменить теги: {m.name}", "rows": rows}


@tool("set_person_tags", "Меняю теги", "(member_id, add?: [теги], remove?: [теги]) — теги аккаунта UNIT (уходят в "
      "UNIT ID) и права по ним", write=True, check=_admin, confirm=_c_tags)
def set_person_tags(lane, a):
    import rights
    import unitid
    m = _member(a.get("member_id"))
    add = [t for t in (rights.clean_tag(x) for x in a.get("add") or []) if t and t not in (m.tags or [])]
    unknown = [t for t in add if t not in access.rules(db())]
    if unknown:   # роли UNIT ID и чужие теги выдаёт только UNIT ID
        raise ToolError("Выдавать можно только теги с правилом лаборатории: " + ", ".join("#" + t for t in unknown))
    remove = [t for t in (str(x) for x in a.get("remove") or []) if t in (m.tags or [])]
    if m.id == g.me.id and remove:
        probe = M.Member(id=m.id, tags=[t for t in m.tags or [] if t not in remove])
        if "admin.access" not in access.resolve(db(), probe)["perms"]:
            raise ToolError("Себе нельзя снять тег, на котором держатся «Доступы»")
    if (add or remove) and unitid.enabled() and not unitid.push_tags(m.id, add, remove):
        raise ToolError("UNIT ID не ответил — теги не изменены, попробуйте через минуту")
    m.tags = [t for t in (m.tags or []) if t not in remove] + add
    db().commit()
    return {"text": f"Теги {m.name}: " + ", ".join("#" + t for t in m.tags), "summary": m.name,
            "link": f"/access/person/{m.id}"}


def _c_access(a):
    m = _member(a.get("member_id"))
    rows = [("Человек", m.name)]
    for key, word in (("allow", "Разрешить"), ("deny", "Запретить")):
        if a.get(key):
            rows.append((word, ", ".join(access.NAMES.get(k, k) for k in a[key])))
    if a.get("limits"):
        rows.append(("Лимиты", ", ".join(f"{access.LIMITS.get(k, (k,))[0]}: ${v}" for k, v in a["limits"].items())))
    return {"text": f"Изменить личные права: {m.name}", "rows": rows}


@tool("set_person_access", "Меняю личные права", "(member_id, allow?: [ключи прав], deny?: [ключи], limits?: "
      "{ai_day_usd, ai_month_usd}) — личные исключения поверх тегов (заменяют прежние)", write=True, check=_admin,
      confirm=_c_access)
def set_person_access(lane, a):
    m = _member(a.get("member_id"))
    allow = [k for k in a.get("allow") or [] if k in access.ALL]
    deny = [k for k in a.get("deny") or [] if k in access.ALL]
    if m.id == g.me.id and "admin.access" in deny:
        raise ToolError("Себе запретить «Доступы» нельзя")
    limits = {}
    for key, value in (a.get("limits") or {}).items():
        if key in access.LIMITS:
            try:
                limits[key] = max(0.0, min(10000.0, float(value)))
            except (TypeError, ValueError):
                pass
    import unitid
    if unitid.enabled():
        try:
            unitid.access_person(m.id, allow, deny, limits)       # права живут в UNIT ID, здесь — копия
        except unitid.UnitIdError as error:
            raise ToolError(str(error)) from error
        db().expire_all()
    else:
        row = db().get(M.MemberAccess, m.id) or M.MemberAccess(member_id=m.id)
        row.allow, row.deny, row.limits = allow, deny, limits
        db().add(row)
        db().commit()
    return {"text": f"Личные права {m.name} сохранены", "summary": m.name, "link": f"/access/person/{m.id}"}
