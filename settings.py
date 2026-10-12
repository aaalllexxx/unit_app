"""
Настройки человека (/settings): всё про себя в одном месте.

Приложение (вибрация), уведомления (push на этом устройстве, письма, какие
события присылать), кеш приложения, вход и защита (пароль UNIT — в UNIT ID,
код приложения — applock.py), ИИ (ссылка на свои настройки агента) и токены
API лаборатории. Токены выпускает UNIT ID (одна система для всех сервисов):
портал просит его служебным ключом и показывает токен один раз.
"""

from flask import flash, g, redirect, render_template, request, url_for

import access
import models as M
import unitid
from app import ISSUER, app, db, need

# Какие события можно не присылать пушем и письмом (в колокольчике они остаются всегда)
KINDS = [("task", "Задачи и комментарии", "Новая задача, задача на проверке, комментарии"),
         ("deadline", "Сроки", "Завтра срок, сегодня срок, просрочено"),
         ("info", "Проекты и новости", "Добавили в проект, новости по проекту"),
         ("blocker", "Помехи", "Сотрудник пишет в итогах дня, что ему мешает"),
         ("support", "Поддержка", "Ответы на обращения в поддержку")]
TOKEN_LEVELS = {"read": ("Только чтение", ["lab:read"]),
                "write": ("Чтение и изменения", ["lab:read", "lab:write"])}
TOKEN_DAYS = {"30": "30 дней", "90": "90 дней", "365": "год", "0": "без срока"}


def muted(member_id: str, kind: str) -> bool:
    """Человек отключил этот вид уведомлений (пуш и письмо; в колокольчике оно всё равно будет)."""
    member = db().get(M.Member, member_id)
    return bool(member) and kind in ((member.prefs or {}).get("mute") or [])


@app.route("/settings", methods=["GET", "POST"])
@need()
def settings_page():
    member = db().get(M.Member, g.me.id)
    if request.method == "POST":
        f = request.form
        prefs = dict(member.prefs or {})
        prefs["haptics"] = f.get("haptics") == "1"
        prefs["mute"] = [k for k, _, _ in KINDS if f.get("kind:" + k) != "1"]
        member.prefs = prefs
        member.notify_email = f.get("notify_email") == "1"
        db().commit()
        flash("Настройки сохранены", "ok")
        return redirect(url_for("settings_page") + "#" + (f.get("section") or "notify"))
    tokens, token_error = [], ""
    if access.can(g.me, "page.projects") or access.can(g.me, "task.work"):
        try:
            tokens = unitid.tokens(g.me.id)
        except unitid.UnitIdError as error:
            token_error = str(error)
    return render_template("settings.html", m=member, kinds=KINDS, mute=(member.prefs or {}).get("mute") or [],
                           haptics=(member.prefs or {}).get("haptics", True), tokens=tokens, token_error=token_error,
                           levels=TOKEN_LEVELS, days=TOKEN_DAYS, issuer=ISSUER, fresh=request.args.get("fresh", ""))


@app.post("/settings/token")
@need("page.projects", "task.work")
def settings_token():
    """Новый токен API лаборатории: выпускает UNIT ID, показываем один раз."""
    f = request.form
    level = TOKEN_LEVELS.get(f.get("level") or "read", TOKEN_LEVELS["read"])
    if "lab:write" in level[1] and not access.can(g.me, "task.work"):
        flash("Токен с изменениями — только тем, кто работает с задачами", "err")
        return redirect(url_for("settings_page") + "#api")
    name = " ".join((f.get("name") or "").split())[:80] or "Токен API лаборатории"
    days = int(f.get("days")) if (f.get("days") or "") in TOKEN_DAYS else 90
    try:
        made = unitid.token_create(g.me.id, name, level[1], days)
    except unitid.UnitIdError as error:
        flash(str(error), "err")
        return redirect(url_for("settings_page") + "#api")
    # Токен показывается один раз — в странице, не в адресе и не в журнале
    return render_template("settings_token.html", token=made.get("token", ""), name=name, level=level[0],
                           days=TOKEN_DAYS[str(days)])


@app.post("/settings/token/<tid>/revoke")
@need("page.projects", "task.work")
def settings_token_revoke(tid):
    try:
        unitid.token_revoke(g.me.id, tid)
        flash("Токен отозван — запросы с ним больше не пройдут", "ok")
    except unitid.UnitIdError as error:
        flash(str(error), "err")
    return redirect(url_for("settings_page") + "#api")
