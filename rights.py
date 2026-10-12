"""
Раздел «Доступы» (/access): теги людей, правила тегов, личные исключения и лимиты.

Теги — общие теги аккаунтов UNIT: добавили или сняли здесь — изменение уходит в
UNIT ID (unitid.push_tags), и наоборот. Правила тегов и личные исключения живут
только в лаборатории.
"""

import re
from datetime import date, timedelta

from flask import abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, select

import access
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


def _set_tags(member, add=(), remove=()) -> bool:
    """Сменить теги человека: сначала в UNIT ID, потом здесь. → удалось ли."""
    add = [t for t in add if t and t not in (member.tags or [])]
    remove = [t for t in remove if t in (member.tags or [])]
    if not add and not remove:
        return True
    if unitid.enabled() and not unitid.push_tags(member.id, add, remove):
        flash("UNIT ID не ответил — теги не изменены. Попробуйте через минуту.", "err")
        return False
    member.tags = [t for t in (member.tags or []) if t not in remove] + add
    return True


@app.route("/access")
@need("page.admin")
def access_page():
    tab = request.args.get("tab") or "people"
    all_rules = access.rules(db())
    members = db().scalars(select(M.Member).order_by(M.Member.name)).all()
    rows = []
    for m in members:
        r = access.resolve(db(), m, all_rules=all_rules)
        rows.append({"m": m, "pending": not r["perms"], "views": r["views"], "spend": spend(m.id),
                     "limits": r["limits"]})
    rows.sort(key=lambda x: (not x["pending"], x["m"].name))
    used = {}
    for m in members:
        for t in (m.tags or []):
            used[t] = used.get(t, 0) + 1
    orphan = sorted(t for t in used if t not in all_rules)
    return render_template("access.html", tab=tab, rows=rows, rules=list(all_rules.values()), used=used, orphan=orphan,
                           catalog=access.CATALOG, names=access.NAMES, limits=access.LIMITS)


@app.route("/access/person/<mid>", methods=["GET", "POST"])
@need("page.admin")
def access_person(mid):
    member = db().get(M.Member, mid)
    if not member:
        abort(404)
    if request.method == "POST":
        if not access.can(g.me, "admin.access"):
            abort(403)
        f = request.form
        action = f.get("action")
        if action == "tag_add":
            tag = clean_tag(f.get("tag"))
            if tag and _set_tags(member, add=[tag]):
                flash(f"Тег #{tag} добавлен", "ok")
        elif action == "tag_remove":
            tag = f.get("tag", "")
            if member.id == g.me.id and tag in access.rules(db()) and "admin.access" in (access.rules(db())[tag].privileges or []):
                still = [t for t in member.tags or [] if t != tag]
                probe = M.Member(id=member.id, tags=still)
                if "admin.access" not in access.resolve(db(), probe)["perms"]:
                    flash("Этот тег снимать себе нельзя — иначе «Доступы» закроются", "err")
                    return redirect(url_for("access_person", mid=mid))
            if _set_tags(member, remove=[tag]):
                flash(f"Тег #{tag} снят", "ok")
        elif action == "personal":
            allow, deny = [], []
            for key in access.ALL:
                mode = f.get("p:" + key)
                if mode == "allow":
                    allow.append(key)
                elif mode == "deny":
                    deny.append(key)
            if member.id == g.me.id and "admin.access" in deny:
                deny.remove("admin.access")
                flash("Себе запретить «Доступы» нельзя", "err")
            limits = {}
            for key in access.LIMITS:
                raw = (f.get("l:" + key) or "").strip().replace(",", ".")
                if raw:
                    try:
                        limits[key] = max(0.0, min(10000.0, float(raw)))
                    except ValueError:
                        pass
            row = db().get(M.MemberAccess, member.id) or M.MemberAccess(member_id=member.id)
            row.allow, row.deny, row.limits = allow, deny, limits
            db().add(row)
            flash("Личные права сохранены", "ok")
        db().commit()
        return redirect(url_for("access_person", mid=mid))
    all_rules = access.rules(db())
    personal = db().get(M.MemberAccess, member.id)
    by_tags = M.Member(id="probe", tags=member.tags or [])
    tag_perms = access.resolve(db(), by_tags, all_rules=all_rules)
    effective = access.resolve(db(), member, all_rules=all_rules)
    return render_template("access_person.html", m=member, rules=all_rules, personal=personal, tag_perms=tag_perms,
                           effective=effective, catalog=access.CATALOG, limits=access.LIMITS, spend=spend(member.id),
                           suggest=[t for t in all_rules if t not in (member.tags or [])])


@app.route("/access/tag", methods=["POST"])
@app.route("/access/tag/<path:tag>", methods=["GET", "POST"])
@need("page.admin")
def access_tag(tag=None):
    rule = db().get(M.TagRule, tag) if tag else None
    if tag and not rule:
        abort(404)
    if request.method == "POST":
        if not access.can(g.me, "admin.access"):
            abort(403)
        f = request.form
        if f.get("action") == "delete" and rule:
            db().delete(rule)
            db().commit()
            flash(f"Правило тега #{rule.tag} удалено — у людей тег остался, но прав больше не даёт", "ok")
            return redirect(url_for("access_page", tab="tags"))
        if rule is None:
            new_tag = clean_tag(f.get("tag"))
            if not new_tag:
                flash("Нужно название тега", "err")
                return redirect(url_for("access_page", tab="tags"))
            if db().get(M.TagRule, new_tag):
                return redirect(url_for("access_tag", tag=new_tag))
            base = db().get(M.TagRule, f.get("copy_from") or "")
            rule = M.TagRule(tag=new_tag, title=f.get("title", "").strip()[:120] or new_tag,
                             is_view=bool(base.is_view) if base else False,
                             privileges=list(base.privileges or []) if base else [],
                             limits=dict(base.limits or {}) if base else {}, tone=base.tone if base else "blue",
                             position=(db().scalar(select(func.max(M.TagRule.position))) or 0) + 1)
            db().add(rule)
            db().commit()
            flash(f"Тег #{new_tag} создан — отметьте, что он открывает", "ok")
            return redirect(url_for("access_tag", tag=new_tag))
        rule.title = f.get("title", rule.title).strip()[:120] or rule.tag
        rule.is_view = f.get("is_view") == "1"
        rule.tone = f.get("tone") if f.get("tone") in ("blue", "violet", "crimson", "teal", "amber") else rule.tone
        try:
            rule.position = max(0, min(999, int(f.get("position") or rule.position or 0)))
        except ValueError:
            pass
        wanted = [k for k in access.ALL if f.get("p:" + k) == "1"]
        # Нельзя оставить лабораторию без «Доступов»: хоть у кого-то право должно остаться
        if "admin.access" in (rule.privileges or []) and "admin.access" not in wanted:
            rule.privileges = wanted
            db().flush()
            if not access.people_with(db(), "admin.access"):
                db().rollback()
                flash("Тогда «Доступы» не сможет открыть никто — право «Менять теги, права и лимиты» оставлено", "err")
                return redirect(url_for("access_tag", tag=rule.tag))
        rule.privileges = wanted
        limits = {}
        for key in access.LIMITS:
            raw = (f.get("l:" + key) or "").strip().replace(",", ".")
            if raw:
                try:
                    limits[key] = max(0.0, min(10000.0, float(raw)))
                except ValueError:
                    pass
        rule.limits = limits
        db().commit()
        flash(f"Правило тега #{rule.tag} сохранено", "ok")
        return redirect(url_for("access_tag", tag=rule.tag))
    people = [m for m in db().scalars(select(M.Member).order_by(M.Member.name)) if rule.tag in (m.tags or [])]
    return render_template("access_tag.html", rule=rule, catalog=access.CATALOG, limits=access.LIMITS, people=people)
