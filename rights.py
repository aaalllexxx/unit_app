"""
Раздел «Доступы» теперь только в UNIT ID: теги людей, правила тегов, личные права и
лимиты настраиваются там (unitid.access_url), лаборатория держит их копию и сверяет
её (unitid.sync_access). Старые адреса /access… ведут на те же страницы UNIT ID.
"""

import re
import urllib.parse
from datetime import date

from flask import redirect
from sqlalchemy import func, select

import models as M
import unitid
from app import app, db, need

_TAG = re.compile(r"[^0-9a-zа-яё _\-]+")


def clean_tag(raw: str) -> str:
    return " ".join(_TAG.sub("", (raw or "").strip().lower()).split())[:60]


def spend(member_id: str) -> dict:
    """Расход на ИИ за сегодня и за месяц, $."""
    today = date.today()
    day = db().scalar(select(func.coalesce(func.sum(M.AiSpend.cost), 0)).where(
        M.AiSpend.member_id == member_id, M.AiSpend.day == today)) or 0
    month = db().scalar(select(func.coalesce(func.sum(M.AiSpend.cost), 0)).where(
        M.AiSpend.member_id == member_id, M.AiSpend.day >= today.replace(day=1))) or 0
    return {"day": float(day), "month": float(month)}


@app.route("/access")
@need("page.admin")
def access_page():
    from flask import request
    return redirect(unitid.access_url("?tab=tags" if request.args.get("tab") == "tags" else ""))


@app.route("/access/person/<mid>")
@need("page.admin")
def access_person(mid):
    return redirect(unitid.access_url("/person/" + urllib.parse.quote(mid)))


@app.route("/access/tag/<path:tag>")
@need("page.admin")
def access_tag(tag):
    return redirect(unitid.access_url("/tag/" + urllib.parse.quote(tag)))
