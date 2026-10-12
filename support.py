"""
Раздел техподдержки лаборатории (/support).

Позволяет сотрудникам создавать обращения с категорией и приоритетом,
вести диалог в ветке обращения, отслеживать статус («Новое», «В работе», «Решено», «Закрыто»).
Сотрудники с правом admin.overview / page.admin видят все обращения и могут отвечать и менять статус.
Также содержит базу частых вопросов (FAQ) и контакты.
"""

from datetime import datetime
from flask import abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import desc, func, select

import access
import models as M
from app import app, db, need


FAQ_ITEMS = [
    {
        "q": "Как подключить личный репозиторий GitHub?",
        "a": "На Рабочем столе (Мастерской) откройте инструмент «GitHub». Введите ваш логин на GitHub и при необходимости Personal Access Token (PAT). После привязки вы сможете стягивать README, задачи (issues) и ссылки на стол одним кликом.",
    },
    {
        "q": "Как привязать наработку к проекту?",
        "a": "В окне наработки (заметка, чек-лист, код, файл) выберите проект из списка и включите тумблер «Показать команде». Наработка появится во вкладке «Материалы» соответствующего проекта.",
    },
    {
        "q": "Как работает ИИ агент лаборатории?",
        "a": "Агент доступен в разделе «ИИ агент», а также прямо на рабочем столе и в окне задачи. Он умеет формулировать задачи, обновлять дедлайны, разбивать задачи на чек-листы и писать код.",
    },
    {
        "q": "Куда писать, если возникла срочная критическая проблема с доступом?",
        "a": "Создайте обращение с приоритетом «Срочный» в этой форме, либо обратитесь к руководителю направления напрямую.",
    },
]


def _is_staff(member) -> bool:
    return access.can(member, "admin.overview") or access.can(member, "page.admin")


@app.route("/support")
@need()
def support_home():
    is_staff = _is_staff(g.me)
    status_filter = request.args.get("status", "").strip()

    stmt = select(M.SupportTicket)
    if not is_staff:
        stmt = stmt.where(M.SupportTicket.member_id == g.me.id)
    elif status_filter and status_filter in M.SUPPORT_STATUSES:
        stmt = stmt.where(M.SupportTicket.status == status_filter)

    stmt = stmt.order_by(desc(M.SupportTicket.updated_at))
    tickets = db().scalars(stmt).all()

    # Справочник авторов
    author_ids = list({t.member_id for t in tickets})
    authors = {m.id: m.name for m in db().scalars(select(M.Member).where(M.Member.id.in_(author_ids or [""])))}

    # Счётчики для администраторов
    counts = {}
    if is_staff:
        for st_key in M.SUPPORT_STATUSES:
            counts[st_key] = db().scalar(
                select(func.count(M.SupportTicket.id)).where(M.SupportTicket.status == st_key)
            ) or 0
        counts["all"] = sum(counts.values())

    return render_template(
        "support.html",
        tickets=tickets,
        authors=authors,
        is_staff=is_staff,
        status_filter=status_filter,
        counts=counts,
        categories=M.SUPPORT_CATEGORIES,
        statuses=M.SUPPORT_STATUSES,
        priorities=M.SUPPORT_PRIORITIES,
        faq=FAQ_ITEMS,
    )


@app.post("/support/new")
@need()
def support_new():
    subject = (request.form.get("subject") or "").strip()[:200]
    category = request.form.get("category") or "tech"
    if category not in M.SUPPORT_CATEGORIES:
        category = "tech"
    priority = request.form.get("priority") or "normal"
    if priority not in M.SUPPORT_PRIORITIES:
        priority = "normal"
    body = (request.form.get("body") or "").strip()[:10000]

    if not subject or not body:
        flash("Укажите тему обращения и подробно опишите проблему.", "danger")
        return redirect(url_for("support_home"))

    now = datetime.now()
    ticket = M.SupportTicket(
        member_id=g.me.id,
        subject=subject,
        category=category,
        priority=priority,
        status="open",
        created_at=now,
        updated_at=now,
    )
    db().add(ticket)
    db().flush()

    msg = M.SupportMessage(
        ticket_id=ticket.id,
        member_id=g.me.id,
        body=body,
        is_staff=False,
        created_at=now,
    )
    db().add(msg)
    db().commit()

    flash("Обращение создано. Мы ответим вам в ближайшее время.", "ok")
    return redirect(url_for("support_ticket", ticket_id=ticket.id))


@app.route("/support/<int:ticket_id>")
@need()
def support_ticket(ticket_id: int):
    ticket = db().get(M.SupportTicket, ticket_id)
    if not ticket:
        abort(404)

    is_staff = _is_staff(g.me)
    if ticket.member_id != g.me.id and not is_staff:
        abort(403)

    messages = db().scalars(
        select(M.SupportMessage)
        .where(M.SupportMessage.ticket_id == ticket.id)
        .order_by(M.SupportMessage.created_at)
    ).all()

    member_ids = list({m.member_id for m in messages} | {ticket.member_id})
    members = {m.id: m for m in db().scalars(select(M.Member).where(M.Member.id.in_(member_ids or [""])))}

    return render_template(
        "support_ticket.html",
        ticket=ticket,
        messages=messages,
        members=members,
        is_staff=is_staff,
        categories=M.SUPPORT_CATEGORIES,
        statuses=M.SUPPORT_STATUSES,
        priorities=M.SUPPORT_PRIORITIES,
    )


@app.post("/support/<int:ticket_id>/reply")
@need()
def support_reply(ticket_id: int):
    ticket = db().get(M.SupportTicket, ticket_id)
    if not ticket:
        abort(404)

    is_staff = _is_staff(g.me)
    if ticket.member_id != g.me.id and not is_staff:
        abort(403)

    body = (request.form.get("body") or "").strip()[:10000]
    if not body:
        flash("Введите текст сообщения.", "danger")
        return redirect(url_for("support_ticket", ticket_id=ticket.id))

    now = datetime.now()
    msg = M.SupportMessage(
        ticket_id=ticket.id,
        member_id=g.me.id,
        body=body,
        is_staff=is_staff,
        created_at=now,
    )
    db().add(msg)

    # Если ответил саппорт и обращение было "open", переводим в "in_progress"
    if is_staff and ticket.status == "open":
        ticket.status = "in_progress"
    # Если ответил автор и обращение было закрыто, переоткрываем
    elif not is_staff and ticket.status in ("resolved", "closed"):
        ticket.status = "open"

    ticket.updated_at = now
    db().commit()

    flash("Сообщение отправлено.", "ok")
    return redirect(url_for("support_ticket", ticket_id=ticket.id))


@app.post("/support/<int:ticket_id>/status")
@need()
def support_status(ticket_id: int):
    ticket = db().get(M.SupportTicket, ticket_id)
    if not ticket:
        abort(404)

    is_staff = _is_staff(g.me)
    if ticket.member_id != g.me.id and not is_staff:
        abort(403)

    new_status = request.form.get("status")
    if new_status in M.SUPPORT_STATUSES:
        ticket.status = new_status
        ticket.updated_at = datetime.now()
        db().commit()
        flash(f"Статус изменён на «{M.SUPPORT_STATUSES[new_status]}».", "ok")

    return redirect(url_for("support_ticket", ticket_id=ticket.id))
