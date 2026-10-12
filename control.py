"""
Контроль выполнения: сигналы, здоровье проектов, «Пульс», напоминания.

Сигнал — то, что требует внимания человека прямо сейчас: просроченный срок,
часть проекта без движения, задача без исполнителя, проверка, которая ждёт
дольше трёх дней, перегруз, блокер из стендапа. Каждый сигнал ведёт туда, где
его можно снять, а ИИ на «Пульсе» предлагает план, как это сделать.

Фоновый поток раз в полчаса напоминает о сроках: «завтра срок», «сегодня
срок», «просрочено» — по одному уведомлению на задачу и дату.
"""

import os
import threading
import time
from datetime import date, datetime, timedelta

from flask import g, render_template, request
from sqlalchemy import func, select

import models as M
from app import STAFF, app, db, need, project_percent, team_ids, visible_projects

STALE_DAYS = 7
REVIEW_DAYS = 3
OVERLOAD_TASKS = 9
OVERLOAD_HOURS = 40


def project_health(p, pct: int) -> str:
    """ok — идёт по плану, risk — отстаёт от графика, late — срок прошёл, idle — не в работе."""
    if p.status != "active":
        return "done" if p.status == "done" else "idle"
    today = date.today()
    if p.due and p.due < today and pct < 100:
        return "late"
    if p.due and p.starts and p.due > p.starts:
        elapsed = (today - p.starts).days / max(1, (p.due - p.starts).days) * 100
        if elapsed > pct + 20:
            return "risk"
    return "ok"


def _sig(level: str, text: str, link: str, icon: str = "flag", project: str = "", kind: str = "") -> dict:
    return {"level": level, "text": text, "link": link, "icon": icon, "project": project, "kind": kind}


def project_signals(p, parts=None, tasks=None) -> list:
    """Что не так с проектом — от важного к менее важному."""
    if p.status != "active":
        return []
    parts = parts if parts is not None else db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all()
    tasks = tasks if tasks is not None else db().scalars(select(M.Task).where(M.Task.project_id == p.id)).all()
    pct = project_percent(parts)
    today, now = date.today(), datetime.now()
    out = []
    health = project_health(p, pct)
    base = f"/p/{p.id}"
    if health == "late":
        out.append(_sig("red", f"Срок прошёл {p.due:%d.%m}, готово {pct}%", base, "calendar", p.title, "late"))
    elif health == "risk":
        out.append(_sig("amber", f"Отстаёт от графика: готово {pct}%, а времени прошло больше", base, "activity",
                        p.title, "risk"))
    late = [t for t in tasks if t.column != "done" and t.due and t.due < today]
    if late:
        out.append(_sig("red", f"Просрочено задач: {len(late)}", base + "#tasks", "clock", p.title, "late_tasks"))
    review = [t for t in tasks if t.column == "review" and t.moved_at and now - t.moved_at > timedelta(days=REVIEW_DAYS)]
    if review:
        out.append(_sig("amber", f"Ждут проверки дольше {REVIEW_DAYS} дней: {len(review)}", base + "#tasks", "eye",
                        p.title, "review"))
    orphan = [t for t in tasks if t.column != "done" and not t.assignee_id]
    if orphan:
        out.append(_sig("amber", f"Задачи без исполнителя: {len(orphan)}", base + "#tasks", "user", p.title, "orphan"))
    for part in parts:
        idle = (now - (part.updated_at or now)).days
        if idle >= STALE_DAYS and not (part.phase == "release" and part.phase_progress >= 100):
            out.append(_sig("amber", f"«{part.title}» без движения {idle} дн.", base + "#parts", "layers", p.title, "stale"))
    if not parts:
        out.append(_sig("amber", "Проект не разбит на части — готовность не считается", base + "#parts", "layers",
                        p.title, "no_parts"))
    if not p.due:
        out.append(_sig("amber", "Не задан срок проекта", base + "#about", "calendar", p.title, "no_due"))
    return out


def scope_projects(person) -> list:
    """Проекты, за которые человек отвечает или в которых работает."""
    rows = visible_projects(person)
    if person.role == "head":
        return rows
    if person.role == "lead":
        return [p for p in rows if p.lead_id == person.id or (person.direction_id and p.direction_id == person.direction_id)
                or person.id in team_ids(p.id)]
    return [p for p in rows if person.id in team_ids(p.id) or p.lead_id == person.id]


def people_in_scope(person) -> list:
    if person.role == "head":
        return db().scalars(select(M.Member).where(M.Member.role.in_(STAFF)).order_by(M.Member.name)).all()
    if person.role == "lead":
        ids = set()
        for p in scope_projects(person):
            ids |= set(team_ids(p.id))
        q = select(M.Member).where(M.Member.role.in_(STAFF))
        rows = db().scalars(q.order_by(M.Member.name)).all()
        return [m for m in rows if m.id in ids or (person.direction_id and m.direction_id == person.direction_id)]
    return []


def load_of(members) -> dict:
    week_start = date.today() - timedelta(days=date.today().weekday())
    out = {}
    for m in members:
        open_ = db().scalars(select(M.Task).where(M.Task.assignee_id == m.id, M.Task.column != "done")).all()
        hours = db().scalar(select(func.coalesce(func.sum(M.TimeEntry.hours), 0)).where(
            M.TimeEntry.member_id == m.id, M.TimeEntry.day >= week_start)) or 0
        last = db().scalar(select(func.max(M.Checkin.day)).where(M.Checkin.member_id == m.id))
        est = sum(t.estimate or 0 for t in open_)
        late = sum(1 for t in open_ if t.due and t.due < date.today())
        state = "over" if len(open_) >= OVERLOAD_TASKS or est > OVERLOAD_HOURS else \
            "idle" if (date.today().weekday() >= 2 and not hours and not open_) else "ok"
        out[m.id] = {"open": len(open_), "estimate": est, "hours": float(hours), "late": late, "checkin": last,
                     "state": state}
    return out


def signals_for(person) -> list:
    """Сигналы для человека: свои — всем, по проектам и людям — тем, кто ими руководит."""
    out = []
    today = date.today()
    if person.role not in STAFF:
        return out
    mine = db().scalars(select(M.Task).where(M.Task.assignee_id == person.id, M.Task.column != "done")).all()
    late = [t for t in mine if t.due and t.due < today]
    if late:
        out.append(_sig("red", f"У вас просрочено задач: {len(late)}", "/home#focus", "clock", "", "my_late"))
    parts = db().scalars(select(M.Part).where(M.Part.owner_id == person.id)).all()
    for part in parts:
        idle = (datetime.now() - (part.updated_at or datetime.now())).days
        if idle >= STALE_DAYS and part.phase_progress < 100:
            p = db().get(M.Project, part.project_id)
            if p and p.status == "active":
                out.append(_sig("amber", f"Ваша часть «{part.title}» без движения {idle} дн.", f"/p/{p.id}#parts",
                                "layers", p.title, "my_stale"))
    if person.role in ("head", "lead"):
        for p in scope_projects(person):
            out.extend(project_signals(p))
        load = load_of(people_in_scope(person))
        names = {m.id: m.name for m in people_in_scope(person)}
        for mid, l in load.items():
            if l["state"] == "over":
                out.append(_sig("amber", f"{names.get(mid, '')}: перегруз — {l['open']} задач, {l['estimate']:g} ч оценки",
                                f"/people/{mid}", "users", "", "overload"))
        for c in db().scalars(select(M.Checkin).where(M.Checkin.day >= today - timedelta(days=1),
                                                      M.Checkin.blockers != "")):
            if c.member_id in names and c.member_id != person.id:
                out.append(_sig("red", f"{names[c.member_id]}: мешает — {c.blockers[:120]}", "/pulse#checkins",
                                "flag", "", "blocker"))
        if person.role == "head":
            waiting = db().scalar(select(func.count()).select_from(M.Member).where(M.Member.role == "pending"))
            if waiting:
                out.append(_sig("amber", f"Ждут доступа к порталу: {waiting}", "/admin?tab=people", "user", "", "pending"))
    rank = {"red": 0, "amber": 1}
    return sorted(out, key=lambda s: rank.get(s["level"], 2))


def leads_of(person) -> set:
    """Кому сообщить о блокере: руководители проектов человека, его направления и лаборатории."""
    ids = set()
    for p in visible_projects(person):
        if person.id in team_ids(p.id) and p.lead_id:
            ids.add(p.lead_id)
    if person.direction_id:
        d = db().get(M.Direction, person.direction_id)
        if d and d.lead_id:
            ids.add(d.lead_id)
    ids |= set(db().scalars(select(M.Member.id).where(M.Member.role == "head")).all())
    ids.discard(person.id)
    return ids


def _throughput(project_ids, days: int = 14) -> list:
    start = date.today() - timedelta(days=days - 1)
    rows = db().scalars(select(M.Task).where(M.Task.project_id.in_(project_ids or [0]))).all()
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        out.append({"day": d, "done": sum(1 for t in rows if t.done_at and t.done_at.date() == d),
                    "new": sum(1 for t in rows if t.created_at and t.created_at.date() == d)})
    return out


@app.route("/pulse")
@need(*STAFF)
def pulse():
    """Пульс: что горит, как идут проекты, кто перегружен, что мешает."""
    person = g.me
    projects = scope_projects(person)
    rows = []
    for p in projects:
        parts = db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all()
        pct = project_percent(parts)
        elapsed = None
        if p.starts and p.due and p.due > p.starts:
            elapsed = max(0, min(100, round((date.today() - p.starts).days / (p.due - p.starts).days * 100)))
        tasks = db().scalars(select(M.Task).where(M.Task.project_id == p.id)).all()
        rows.append({"p": p, "percent": pct, "elapsed": elapsed, "health": project_health(p, pct),
                     "open": sum(1 for t in tasks if t.column != "done"),
                     "late": sum(1 for t in tasks if t.column != "done" and t.due and t.due < date.today())})
    order = {"late": 0, "risk": 1, "ok": 2, "idle": 3, "done": 4}
    rows.sort(key=lambda r: (order.get(r["health"], 5), -r["late"]))
    team = people_in_scope(person)
    checkins = db().scalars(select(M.Checkin).where(M.Checkin.day == date.today(),
                                                    M.Checkin.member_id.in_([m.id for m in team] or [person.id]))).all()
    flow = _throughput([p.id for p in projects])
    brief = db().get(M.Setting, f"pulse:{person.id}:{date.today()}")
    return render_template("pulse.html", signals=signals_for(person), rows=rows, team=team, load=load_of(team),
                           checkins=checkins, names={m.id: m.name for m in team}, flow=flow,
                           flow_top=max([max(f["done"], f["new"]) for f in flow] + [4]),
                           brief=brief.value if brief else "")


# ─── Напоминания о сроках ───────────────────────────────────────────────────
def remind_once() -> int:
    """Уведомления «завтра срок», «сегодня срок», «просрочено». → сколько добавлено."""
    s = M.Session()
    added = 0
    try:
        today = date.today()
        tasks = s.scalars(select(M.Task).where(M.Task.column != "done", M.Task.due.is_not(None),
                                               M.Task.assignee_id.is_not(None),
                                               M.Task.due <= today + timedelta(days=1))).all()
        for t in tasks:
            if t.due < today:
                key, text, kind = f"late:{t.id}:{t.due}", f"Просрочено: {t.title} (срок {t.due:%d.%m})", "deadline"
            elif t.due == today:
                key, text, kind = f"today:{t.id}:{t.due}", f"Сегодня срок: {t.title}", "deadline"
            else:
                key, text, kind = f"soon:{t.id}:{t.due}", f"Завтра срок: {t.title}", "deadline"
            if not s.scalar(select(M.Notice.id).where(M.Notice.member_id == t.assignee_id, M.Notice.key == key)):
                s.add(M.Notice(member_id=t.assignee_id, key=key, kind=kind, text=text,
                               link=f"/p/{t.project_id}?task={t.id}#tasks"))
                added += 1
        for p in s.scalars(select(M.Project).where(M.Project.status == "active", M.Project.due.is_not(None),
                                                   M.Project.due < today, M.Project.lead_id.is_not(None))):
            key = f"plate:{p.id}:{p.due}"
            if not s.scalar(select(M.Notice.id).where(M.Notice.member_id == p.lead_id, M.Notice.key == key)):
                s.add(M.Notice(member_id=p.lead_id, key=key, kind="deadline",
                               text=f"Проект «{p.title}» просрочен (срок {p.due:%d.%m})", link=f"/p/{p.id}"))
                added += 1
        s.execute(M.Notice.__table__.delete().where(M.Notice.created_at < datetime.now() - timedelta(days=60)))
        s.commit()
    finally:
        s.close()
    return added


def _worker() -> None:
    time.sleep(30)
    while True:
        try:
            remind_once()
        except Exception as error:  # noqa: BLE001 — поток не должен умирать
            print(f"[remind] {error}", flush=True)
        time.sleep(1800)


if os.environ.get("LAB_WORKER", "1") == "1":
    threading.Thread(target=_worker, name="lab-remind", daemon=True).start()
