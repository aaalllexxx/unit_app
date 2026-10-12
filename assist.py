"""
Сводки ИИ: «с чего начать сегодня» на главной и план руководителя на «Пульсе».

Сводка собирается из того, что видит человек: его задачи, проекты, части,
сигналы, мастерская и итоги дня (context). Пишет её самая дешёвая модель
команды, раз в день, результат кешируется. Чат с агентом — agent.py.
"""

from datetime import date

from flask import abort, g, request
from sqlalchemy import select

import ai
import models as M
import access
from app import PHASE_NAME, app, db, is_team, need, project_percent, team_ids, visible_projects
from control import project_health, scope_projects, signals_for

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

SYSTEM = """Ты — ИИ-помощник IT-лаборатории UNIT (проекты по безопасности, разработке ПО и нейросетям).
Готовишь короткие сводки для сотрудников и руководителей: что важно, что горит, с чего начать.
Отвечай по-русски, по делу, списком. Опирайся только на данные ниже — не выдумывай задачи, сроки и людей."""


# ─── Контекст ───────────────────────────────────────────────────────────────
def _fmt_task(t, names) -> str:
    bits = [f"[задача {t.id}] {t.title}", f"проект {t.project_id}", dict(M.TASK_COLUMNS).get(t.column, t.column)]
    if t.assignee_id:
        bits.append("исполнитель " + names.get(t.assignee_id, t.assignee_id))
    if t.due:
        bits.append(f"срок {t.due:%d.%m.%Y}")
    if t.estimate:
        bits.append(f"оценка {t.estimate:g} ч")
    if t.checklist:
        bits.append(f"чек-лист {sum(1 for i in t.checklist if i.get('done'))}/{len(t.checklist)}")
    return " · ".join(bits)


def context(person, project_id=None, item_id=None) -> str:
    today = date.today()
    lines = [f"Сегодня {today:%d.%m.%Y}, {WEEKDAYS[today.weekday()]}."]
    view_title = access.view_title(db(), getattr(person, "view", None)) if getattr(person, "view", None) else "без кабинета"
    pos = f", {person.position}" if getattr(person, "position", None) else ""
    lines.append(f"Человек: {person.name} (id {person.id}), кабинет — {view_title}{pos}.")
    people = {m.id: m.name for m in db().scalars(select(M.Member))}
    mine = db().scalars(select(M.Task).where(M.Task.assignee_id == person.id, M.Task.column != "done")
                        .order_by(M.Task.due.is_(None), M.Task.due).limit(30)).all()
    if mine:
        lines.append("\nОткрытые задачи человека:")
        lines += ["- " + _fmt_task(t, people) for t in mine]
    projects = scope_projects(person) if access.can(person, "task.work") else visible_projects(person)
    if projects:
        lines.append("\nПроекты:")
    for p in projects[:15]:
        parts = db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all()
        pct = project_percent(parts)
        team = team_ids(p.id)
        lines.append(f"- [проект {p.id}] «{p.title}» — {M.STATUSES.get(p.status)}, готово {pct}%"
                     + (f", срок {p.due:%d.%m.%Y}" if p.due else ", без срока")
                     + f", состояние: {project_health(p, pct)}"
                     + (f", руководитель {people.get(p.lead_id, '')}" if p.lead_id else ""))
        if parts:
            lines.append("  части: " + "; ".join(
                f"[часть {x.id}] {x.title} — {PHASE_NAME.get(x.phase)} {x.phase_progress}%" for x in parts[:12]))
        if team:
            lines.append("  команда: " + ", ".join(f"{people.get(mid, mid)} (id {mid}{', ' + r if r else ''})"
                                                   for mid, r in list(team.items())[:15]))
    if project_id:
        p = db().get(M.Project, project_id)
        if p and p in projects or (p and is_team(person, p)):
            lines.append(f"\nПодробно о проекте {p.id} «{p.title}»:")
            if p.summary or p.about:
                lines.append((p.summary + "\n" + p.about).strip()[:2500])
            tasks = db().scalars(select(M.Task).where(M.Task.project_id == p.id).order_by(M.Task.id.desc()).limit(60)).all()
            lines += ["- " + _fmt_task(t, people) for t in tasks]
            ups = db().scalars(select(M.Update).where(M.Update.project_id == p.id)
                               .order_by(M.Update.created_at.desc()).limit(8)).all()
            if ups:
                lines.append("Журнал (свежие записи):")
                lines += [f"- {u.created_at:%d.%m} {people.get(u.author_id, '')}: {u.text[:300]}" for u in ups]
    sig = signals_for(person)
    if sig:
        lines.append("\nСигналы (что требует внимания):")
        lines += [f"- {s['text']}" + (f" ({s['project']})" if s["project"] else "") for s in sig[:12]]
    check = db().scalar(select(M.Checkin).where(M.Checkin.member_id == person.id, M.Checkin.day == today))
    if check:
        lines.append(f"\nИтоги дня сегодня: сделано — {check.done[:300]}; план — {check.plan[:300]}"
                     + (f"; мешает — {check.blockers[:300]}" if check.blockers else ""))
    items = db().scalars(select(M.DeskItem).where(M.DeskItem.member_id == person.id)
                         .order_by(M.DeskItem.updated_at.desc()).limit(10)).all()
    if items:
        lines.append("\nНа рабочем столе: " + "; ".join(f"{M.DESK_KINDS.get(i.kind)} «{i.title or 'без названия'}»"
                                                        for i in items))
    if item_id:
        item = db().get(M.DeskItem, item_id)
        if item and item.member_id == person.id:
            body = item.body
            if item.kind == "checklist":
                body = "\n".join(("[x] " if i.get("done") else "[ ] ") + i.get("text", "")
                                 for i in (item.data or {}).get("items", []))
            lines.append(f"\nНаработка, о которой идёт речь — {M.DESK_KINDS.get(item.kind)} «{item.title}»:\n{body[:8000]}")
    extra = ai.settings(db())["prompt"]
    return SYSTEM + ("\n\nУказания руководителя лаборатории:\n" + extra if extra else "") + "\n\nДАННЫЕ\n" + "\n".join(lines)


BRIEFS = {
    "day": ("brief", "Составь сводку моего дня: 3–5 пунктов — что важно сегодня, что горит, с чего начать. "
                     "Коротко, без вступления."),
    "pulse": ("pulse", "Сделай сводку для руководителя по моей зоне: главные риски, узкие места в людях и сроках "
                       "и конкретный план на эту неделю. 5–8 пунктов, без вступления."),
}


@app.post("/api/ai/brief")
@need("ai.use")
def ai_brief():
    """Сводка дня или «Пульса» — раз в день, кешируется. Пишет самая дешёвая модель команды."""
    scope = (request.get_json(silent=True) or {}).get("scope")
    if scope not in BRIEFS:
        abort(400)
    if not ai.allowed(db(), g.me):
        return {"error": "ИИ не включён"}, 403
    prefix, prompt = BRIEFS[scope]
    try:
        text, cost, model = ai.ask(db(), [{"role": "system", "content": context(g.me)},
                                          {"role": "user", "content": prompt}], fast=True, max_tokens=900)
    except ai.AiError as error:
        return {"error": str(error)}, 502
    ai.charge(db(), g.me.id, model, cost)
    key = f"{prefix}:{g.me.id}:{date.today()}"
    row = db().get(M.Setting, key) or M.Setting(key=key)
    row.value = text
    db().add(row)
    db().commit()
    return {"text": text}
