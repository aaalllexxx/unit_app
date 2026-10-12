"""
ИИ-помощник лаборатории: чат, сводки и действия.

Помощник знает то же, что видит человек: его задачи, проекты, части, сигналы,
мастерскую и итоги дня. Отвечает потоком. Когда предлагает что-то сделать —
завести задачу, часть проекта, заметку на стол, запись в журнал или
передвинуть задачу, — добавляет в конец блок ```actions [...]```. Портал
показывает каждое предложение карточкой с кнопкой «Применить»: ничего не
меняется без человека, а права проверяются заново при применении.

Разговоры: «me» — личный, «p<id>» — о проекте (виден только автору).
"""

import json
import re
from datetime import date, datetime, timedelta

from flask import Response, abort, flash, g, redirect, render_template, request, stream_with_context, url_for
from sqlalchemy import select

import ai
import models as M
import access
from app import (PHASE_INDEX, PHASE_NAME, app, can_edit, db, is_team, need, notify, project_percent,
                 set_column, team_ids, visible_projects)
from control import project_health, scope_projects, signals_for

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_ACTIONS = re.compile(r"```actions\s*(\[.*?\])\s*```", re.S)
HISTORY = 16

SYSTEM = """Ты — ИИ-помощник IT-лаборатории UNIT (проекты по безопасности, разработке ПО и нейросетям).
Помогаешь сотрудникам и руководителям доводить проекты до результата: планировать, разбивать работу,
находить риски, готовить отчёты и тексты, разбирать код и идеи.

Правила:
- Отвечай по-русски, по делу и коротко. Разметка: **жирный**, списки, `код`, блоки ```код```, заголовки ###.
- Опирайся на данные ниже. Не выдумывай задачи, сроки и людей, которых нет.
- Когда уместно конкретное действие в портале, предложи его блоком в самом конце ответа:
```actions
[{"type": "task", "project_id": 3, "title": "…", "assignee_id": "…", "due": "2026-10-20", "part_id": 5, "about": "…", "priority": 2}]
```
Типы действий:
  task — новая задача (project_id и title обязательны; остальное по желанию; priority 1 срочно, 2 обычно, 3 потом);
  part — новая часть проекта {"type":"part","project_id":…,"title":"…"};
  move — сменить колонку задачи {"type":"move","task_id":…,"column":"todo|doing|review|done"};
  journal — запись в журнал проекта {"type":"journal","project_id":…,"text":"…"};
  note — заметка на рабочий стол {"type":"note","title":"…","body":"…"};
  checklist — чек-лист на стол {"type":"checklist","title":"…","items":["…","…"]}.
- Используй только id из данных. Не больше 12 действий за раз. Если действий не нужно — блок не добавляй.
- Блок actions пиши только в конце, после текста ответа, и не упоминай его в тексте."""


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
    lines.append(f"Человек: {person.name} (id {person.id}), кабинет — {access.view_title(db(), person.view) if person.view else 'без кабинета'}"
                 + (f", {person.position}" if person.position else "") + ".")
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


def split_actions(text: str) -> tuple:
    """Текст ответа и предложенные действия (блок ```actions```)."""
    actions = []
    match = _ACTIONS.search(text or "")
    if match:
        try:
            raw = json.loads(match.group(1))
            actions = [a for a in raw if isinstance(a, dict) and a.get("type") in ACTION_TYPES][:12]
        except ValueError:
            actions = []
        text = (text[:match.start()] + text[match.end():]).strip()
    return text, actions


ACTION_TYPES = {"task": "Задача", "part": "Часть проекта", "move": "Перенести задачу", "journal": "Запись в журнал",
                "note": "Заметка на стол", "checklist": "Чек-лист на стол"}


def describe(action: dict) -> dict:
    """Как показать предложение человеку."""
    kind = action.get("type")
    project = db().get(M.Project, int(action["project_id"])) if str(action.get("project_id", "")).isdigit() else None
    title = action.get("title") or action.get("text") or ""
    meta = []
    if project:
        meta.append(project.title)
    if kind == "task":
        who = db().get(M.Member, str(action.get("assignee_id") or "")) if action.get("assignee_id") else None
        if who:
            meta.append(who.name)
        if action.get("due"):
            meta.append("срок " + str(action["due"]))
    if kind == "move":
        task = db().get(M.Task, int(action["task_id"])) if str(action.get("task_id", "")).isdigit() else None
        title = task.title if task else "задача"
        meta.append("→ " + dict(M.TASK_COLUMNS).get(action.get("column"), "?"))
    if kind == "checklist":
        meta.append(f"{len(action.get('items') or [])} пунктов")
    return {"label": ACTION_TYPES.get(kind, kind), "title": str(title)[:200], "meta": " · ".join(meta),
            "applied": bool(action.get("applied")), "error": action.get("error", "")}


def _project_for(action, edit=False):
    pid = action.get("project_id")
    project = db().get(M.Project, int(pid)) if str(pid or "").isdigit() else None
    if not project or not is_team(g.me, project) or (edit and not can_edit(g.me, project)):
        raise ValueError("Нет доступа к проекту")
    return project


def apply(action: dict) -> dict:
    """Выполнить предложение ИИ от имени человека. → {"text", "link"}."""
    kind = action.get("type")
    if kind == "task":
        project = _project_for(action)
        title = str(action.get("title") or "").strip()[:300]
        if not title:
            raise ValueError("Нет названия задачи")
        task = M.Task(project_id=project.id, title=title, about=str(action.get("about") or "")[:5000],
                      created_by=g.me.id, priority=max(1, min(3, int(action.get("priority") or 2))))
        who = str(action.get("assignee_id") or "")
        if who and (who in team_ids(project.id) or who == project.lead_id):
            task.assignee_id = who
        if str(action.get("part_id") or "").isdigit():
            part = db().get(M.Part, int(action["part_id"]))
            task.part_id = part.id if part and part.project_id == project.id else None
        if action.get("phase") in PHASE_INDEX:
            task.phase = action["phase"]
        try:
            task.due = date.fromisoformat(str(action.get("due"))) if action.get("due") else None
            task.estimate = max(0, float(action.get("estimate") or 0))
        except (ValueError, TypeError):
            task.due = None
        db().add(task)
        db().commit()
        if task.assignee_id:
            notify(task.assignee_id, f"Новая задача: {task.title}", f"Проект «{project.title}».",
                   f"/p/{project.id}?task={task.id}#tasks", kind="task")
        return {"text": "Задача создана", "link": f"/p/{project.id}?task={task.id}#tasks"}
    if kind == "part":
        project = _project_for(action, edit=True)
        position = len(db().scalars(select(M.Part).where(M.Part.project_id == project.id)).all()) + 1
        part = M.Part(project_id=project.id, title=str(action.get("title") or "Часть проекта")[:200], position=position)
        db().add(part)
        db().commit()
        return {"text": "Часть добавлена", "link": f"/p/{project.id}#parts"}
    if kind == "move":
        task = db().get(M.Task, int(action["task_id"])) if str(action.get("task_id", "")).isdigit() else None
        project = db().get(M.Project, task.project_id) if task else None
        if not task or not is_team(g.me, project) or action.get("column") not in dict(M.TASK_COLUMNS):
            raise ValueError("Задача недоступна")
        set_column(task, action["column"], project)
        db().commit()
        return {"text": "Задача перенесена", "link": f"/p/{project.id}?task={task.id}#tasks"}
    if kind == "journal":
        project = _project_for(action)
        text = str(action.get("text") or "").strip()[:4000]
        if not text:
            raise ValueError("Пустая запись")
        db().add(M.Update(project_id=project.id, author_id=g.me.id, text=text))
        db().commit()
        return {"text": "Запись в журнале", "link": f"/p/{project.id}#journal"}
    if kind in ("note", "checklist"):
        item = M.DeskItem(member_id=g.me.id, kind=kind, title=str(action.get("title") or "")[:200],
                          body=str(action.get("body") or "")[:200000])
        if kind == "checklist":
            item.data = {"items": [{"text": str(t)[:300], "done": False} for t in (action.get("items") or [])][:200]}
        if str(action.get("project_id") or "").isdigit():
            try:
                item.project_id = _project_for(action).id
            except ValueError:
                pass
        db().add(item)
        db().commit()
        return {"text": "На рабочем столе", "link": f"/desk?open={item.id}"}
    raise ValueError("Незнакомое действие")


# ─── Страницы и API ─────────────────────────────────────────────────────────
def _thread(raw: str) -> str:
    raw = (raw or "me").strip()
    if raw.startswith("p") and raw[1:].isdigit():
        project = db().get(M.Project, int(raw[1:]))
        if project and is_team(g.me, project):
            return raw
    return "me"


def _history(thread: str, limit: int = 40) -> list:
    rows = db().scalars(select(M.AiMessage).where(M.AiMessage.member_id == g.me.id, M.AiMessage.thread == thread)
                        .order_by(M.AiMessage.id.desc()).limit(limit)).all()
    return list(reversed(rows))


def _message_json(m) -> dict:
    return {"id": m.id, "role": m.role, "text": m.content, "at": m.created_at.strftime("%d.%m %H:%M"),
            "actions": [describe(a) for a in (m.actions or [])]}


@app.route("/ai")
@need("ai.use")
def ai_page():
    thread = _thread(request.args.get("thread") or ("p" + request.args["project"] if request.args.get("project") else "me"))
    projects = [p for p in visible_projects(g.me) if is_team(g.me, p)]
    return render_template("ai.html", thread=thread, projects=projects, ready=ai.allowed(db(), g.me),
                           left=ai.left_today(db(), g.me.id), ask=request.args.get("ask", ""))


@app.get("/api/ai/history")
@need("ai.use")
def ai_history():
    thread = _thread(request.args.get("thread"))
    return {"thread": thread, "messages": [_message_json(m) for m in _history(thread)],
            "ready": ai.allowed(db(), g.me), "left": ai.left_today(db(), g.me.id)}


@app.post("/api/ai/clear")
@need("ai.use")
def ai_clear():
    thread = _thread((request.get_json(silent=True) or {}).get("thread"))
    for m in db().scalars(select(M.AiMessage).where(M.AiMessage.member_id == g.me.id, M.AiMessage.thread == thread)):
        db().delete(m)
    db().commit()
    return {"ok": True}


def _sse(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


@app.post("/api/ai/chat")
@need("ai.use")
def ai_chat():
    """Вопрос → ответ потоком (text/event-stream): {delta} … {done, message}."""
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()[:6000]
    if not text:
        return {"error": "Пустой вопрос"}, 400
    if not ai.allowed(db(), g.me):
        return {"error": "ИИ для вашей роли не включён — обратитесь к руководителю лаборатории"}, 403
    if ai.left_today(db(), g.me.id) <= 0:
        return {"error": "На сегодня запросы к ИИ закончились — лимит задаёт руководитель"}, 429
    thread = _thread(data.get("thread"))
    project_id = int(thread[1:]) if thread.startswith("p") else (int(data["project_id"]) if str(data.get("project_id") or "").isdigit() else None)
    item_id = int(data["item_id"]) if str(data.get("item_id") or "").isdigit() else None
    messages = [{"role": "system", "content": context(g.me, project_id, item_id)}]
    for m in _history(thread, HISTORY):
        messages.append({"role": m.role, "content": m.content + (
            "\n```actions\n" + json.dumps(m.actions, ensure_ascii=False) + "\n```" if m.actions else "")})
    messages.append({"role": "user", "content": text})
    db().add(M.AiMessage(member_id=g.me.id, thread=thread, role="user", content=text))
    db().commit()

    def generate():
        parts, tokens = [], 0
        try:
            for event in ai.stream(db(), messages):
                if "delta" in event:
                    parts.append(event["delta"])
                    yield _sse({"delta": event["delta"]})
                if event.get("done"):
                    tokens = event.get("tokens") or 0
        except ai.AiError as error:
            yield _sse({"error": str(error)})
            return
        reply, actions = split_actions("".join(parts))
        msg = M.AiMessage(member_id=g.me.id, thread=thread, role="assistant", content=reply, actions=actions)
        db().add(msg)
        db().commit()
        ai.count(db(), g.me.id, tokens)
        yield _sse({"done": True, "message": _message_json(msg)})

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.post("/api/ai/apply")
@need("ai.use")
def ai_apply():
    data = request.get_json(silent=True) or {}
    msg = db().get(M.AiMessage, int(data.get("message_id") or 0))
    index = data.get("index")
    if not msg or msg.member_id != g.me.id or not isinstance(index, int) or not 0 <= index < len(msg.actions or []):
        return {"error": "Предложение не найдено"}, 404
    actions = [dict(a) for a in msg.actions]
    if actions[index].get("applied"):
        return {"error": "Уже применено"}, 409
    try:
        result = apply(actions[index])
        actions[index]["applied"] = True
        actions[index].pop("error", None)
    except (ValueError, KeyError, TypeError) as error:
        actions[index]["error"] = str(error)
        result = {"error": str(error)}
    msg.actions = actions
    db().commit()
    return result, 200 if "error" not in result else 400


BRIEFS = {
    "day": ("brief", "Составь сводку моего дня: 3–5 пунктов — что важно сегодня, что горит, с чего начать. "
                     "Коротко, без вступления. Действия не предлагай."),
    "pulse": ("pulse", "Сделай сводку для руководителя по моей зоне: главные риски, узкие места в людях и сроках "
                       "и конкретный план на эту неделю. 5–8 пунктов, без вступления. Действия не предлагай."),
}


@app.post("/api/ai/brief")
@need("ai.use")
def ai_brief():
    """Сводка дня или «Пульса» — раз в день, кешируется."""
    scope = (request.get_json(silent=True) or {}).get("scope")
    if scope not in BRIEFS:
        abort(400)
    if not ai.allowed(db(), g.me):
        return {"error": "ИИ не включён"}, 403
    if ai.left_today(db(), g.me.id) <= 0:
        return {"error": "Запросы к ИИ на сегодня закончились"}, 429
    prefix, prompt = BRIEFS[scope]
    try:
        text, tokens = ai.ask(db(), [{"role": "system", "content": context(g.me)}, {"role": "user", "content": prompt}],
                              fast=True, max_tokens=900)
    except ai.AiError as error:
        return {"error": str(error)}, 502
    text, _ = split_actions(text)
    key = f"{prefix}:{g.me.id}:{date.today()}"
    row = db().get(M.Setting, key) or M.Setting(key=key)
    row.value = text
    db().add(row)
    db().commit()
    ai.count(db(), g.me.id, tokens)
    return {"text": text}


# ─── Настройка ИИ (руководитель) ────────────────────────────────────────────
@app.route("/admin/ai", methods=["GET", "POST"])
@need("ai.settings")
def admin_ai():
    if request.method == "GET":
        return render_template("admin_ai.html", cfg=ai.settings(db()), ready=ai.configured(db()))
    f = request.form
    if f.get("forget"):
        ai.forget_key(db())
        flash("Ключ удалён", "ok")
        return redirect(url_for("admin_ai"))
    try:
        daily = int(f.get("daily") or 0)
    except ValueError:
        daily = ai.DEFAULT_DAILY
    ai.save(db(), f.get("base_url", ""), f.get("model", ""), f.get("fast_model", ""), f.getlist("roles"), daily,
            f.get("prompt", ""), f.get("api_key"))
    if f.get("test"):
        try:
            flash("ИИ отвечает: " + ai.check(db()), "ok")
        except ai.AiError as error:
            flash("Проверка не прошла: " + str(error), "err")
    else:
        flash("Настройки ИИ сохранены", "ok")
    return redirect(url_for("admin_ai"))


def purge_old() -> None:
    """Старые разговоры с ИИ (старше 90 дней) — прочь."""
    edge = datetime.now() - timedelta(days=90)
    db().execute(M.AiMessage.__table__.delete().where(M.AiMessage.created_at < edge))
    db().commit()
