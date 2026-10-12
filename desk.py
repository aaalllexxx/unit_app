"""
Рабочий стол сотрудника (/desk): всё нужное под рукой.

Наработки — заметки, чек-листы, ссылки, файлы и код. Их можно закрепить,
привязать к проекту и показать команде: тогда они появляются в проекте во
вкладке «Материалы» (команда видит, править может автор). Рядом — виджеты:
таймер, задачи на сегодня, стендап и ИИ, который знает про стол и проекты.

Правки сохраняются сами (POST /api/desk/<id> из редактора), файлы лежат в
томе данных портала: data/desk/<человек>/.
"""

import os
import re
import secrets
from datetime import datetime

from flask import abort, g, render_template, request, send_file
from sqlalchemy import func, select

import models as M
from app import app, db, is_team, need, project_or_404, visible_projects

FILES = os.path.join(M.DATA_DIR, "desk")
MAX_FILE = 25 * 1024 * 1024
QUOTA = 500 * 1024 * 1024
INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain"}
LANGS = ["python", "javascript", "typescript", "sql", "bash", "go", "java", "c", "cpp", "csharp", "html", "css",
         "json", "yaml", "markdown", "text"]
COLORS = ["", "blue", "teal", "amber", "rose", "violet"]
app.config.setdefault("MAX_CONTENT_LENGTH", MAX_FILE + 1024 * 1024)


def _item_json(item, names=None) -> dict:
    data = dict(item.data or {})
    names = names or {}
    out = {"id": item.id, "kind": item.kind, "title": item.title, "body": item.body, "data": data,
           "project_id": item.project_id or "", "project": names.get(item.project_id, ""), "shared": item.shared,
           "pinned": item.pinned, "color": item.color, "mine": item.member_id == g.me.id,
           "updated": item.updated_at.strftime("%d.%m %H:%M") if item.updated_at else ""}
    if item.kind == "file":
        out["url"] = f"/desk/file/{item.id}"
        data.pop("path", None)
    return out


def _can_read(item) -> bool:
    if item.member_id == g.me.id:
        return True
    if item.shared and item.project_id:
        project = db().get(M.Project, item.project_id)
        return bool(project) and is_team(g.me, project)
    return False


def _own(item_id: int):
    item = db().get(M.DeskItem, item_id)
    if not item or item.member_id != g.me.id:
        abort(404)
    return item


def _project_ok(pid) -> int:
    if not str(pid or "").isdigit():
        return None
    project = project_or_404(int(pid))
    if not is_team(g.me, project):
        abort(403)
    return project.id


@app.route("/desk")
@need("workshop.use")
def desk():
    items = db().scalars(select(M.DeskItem).where(M.DeskItem.member_id == g.me.id)
                         .order_by(M.DeskItem.pinned.desc(), M.DeskItem.updated_at.desc())).all()
    projects = [p for p in visible_projects(g.me) if is_team(g.me, p)]
    names = {p.id: p.title for p in visible_projects(g.me)}
    shared = db().scalars(select(M.DeskItem).where(M.DeskItem.shared.is_(True), M.DeskItem.member_id != g.me.id,
                                                   M.DeskItem.project_id.in_([p.id for p in projects] or [0]))
                          .order_by(M.DeskItem.updated_at.desc()).limit(12)).all()
    authors = {m.id: m.name for m in db().scalars(select(M.Member).where(
        M.Member.id.in_([i.member_id for i in shared] or [""])))}
    tasks = db().scalars(select(M.Task).where(M.Task.assignee_id == g.me.id, M.Task.column != "done")
                         .order_by(M.Task.due.is_(None), M.Task.due, M.Task.priority).limit(8)).all()
    used = sum(int((i.data or {}).get("size") or 0) for i in items if i.kind == "file")
    checkin = db().scalar(select(M.Checkin).where(M.Checkin.member_id == g.me.id,
                                                  M.Checkin.day == datetime.now().date()))
    return render_template("desk.html", items=[_item_json(i, names) for i in items], projects=projects, names=names,
                           shared=[dict(_item_json(i, names), author=authors.get(i.member_id, "")) for i in shared], kinds=M.DESK_KINDS,
                           langs=LANGS, colors=COLORS, tasks=tasks, used=used, quota=QUOTA, checkin=checkin)


@app.post("/api/desk")
@need("workshop.use")
def desk_create():
    data = request.get_json(silent=True) or {}
    kind = data.get("kind") if data.get("kind") in M.DESK_KINDS and data.get("kind") != "file" else "note"
    item = M.DeskItem(member_id=g.me.id, kind=kind, title=(data.get("title") or "").strip()[:200],
                      body=(data.get("body") or "")[:200000], project_id=_project_ok(data.get("project_id")))
    if kind == "checklist":
        item.data = {"items": [{"text": t.strip()[:300], "done": False}
                               for t in (data.get("items") or []) if str(t).strip()][:200]}
    elif kind == "code":
        item.data = {"lang": data.get("lang") if data.get("lang") in LANGS else "python"}
    elif kind == "link":
        url = (data.get("url") or "").strip()
        item.data = {"url": url[:1000] if url.startswith(("https://", "http://")) else ""}
    db().add(item)
    db().commit()
    return _item_json(item, {p.id: p.title for p in visible_projects(g.me)}), 201


@app.get("/api/desk/<int:item_id>")
@need("workshop.use")
def desk_get(item_id):
    item = db().get(M.DeskItem, item_id)
    if not item or not _can_read(item):
        abort(404)
    return _item_json(item, {p.id: p.title for p in visible_projects(g.me)})


@app.post("/api/desk/<int:item_id>")
@need("workshop.use")
def desk_update(item_id):
    """Автосохранение редактора: приходят только изменённые поля."""
    item = _own(item_id)
    data = request.get_json(silent=True) or {}
    if "title" in data:
        item.title = str(data["title"] or "").strip()[:200]
    if "body" in data:
        item.body = str(data["body"] or "")[:200000]
    if "project_id" in data:
        item.project_id = _project_ok(data["project_id"])
        if not item.project_id:
            item.shared = False
    if "shared" in data:
        item.shared = bool(data["shared"]) and bool(item.project_id)
    if "pinned" in data:
        item.pinned = bool(data["pinned"])
    if data.get("color") in COLORS:
        item.color = data["color"]
    extra = dict(item.data or {})
    if item.kind == "checklist" and isinstance(data.get("items"), list):
        extra["items"] = [{"text": str(i.get("text") or "").strip()[:300], "done": bool(i.get("done"))}
                          for i in data["items"] if isinstance(i, dict) and str(i.get("text") or "").strip()][:200]
    if item.kind == "code" and data.get("lang") in LANGS:
        extra["lang"] = data["lang"]
    if item.kind == "link" and "url" in data:
        url = str(data["url"] or "").strip()
        extra["url"] = url[:1000] if url.startswith(("https://", "http://")) else ""
    item.data = extra
    item.updated_at = datetime.now()
    db().commit()
    return _item_json(item, {p.id: p.title for p in visible_projects(g.me)})


@app.post("/api/desk/<int:item_id>/delete")
@need("workshop.use")
def desk_delete(item_id):
    item = _own(item_id)
    path = (item.data or {}).get("path")
    db().delete(item)
    db().commit()
    if path:
        try:
            os.remove(os.path.join(FILES, path))
        except OSError:
            pass
    return {"ok": True}


@app.post("/api/desk/<int:item_id>/copy")
@need("workshop.use")
def desk_copy(item_id):
    """Взять наработку коллеги к себе на стол (файл — ссылкой на тот же файл не копируем)."""
    item = db().get(M.DeskItem, item_id)
    if not item or not _can_read(item) or item.kind == "file":
        abort(404)
    copy = M.DeskItem(member_id=g.me.id, kind=item.kind, title=item.title, body=item.body, data=dict(item.data or {}),
                      project_id=item.project_id)
    db().add(copy)
    db().commit()
    return _item_json(copy, {p.id: p.title for p in visible_projects(g.me)}), 201


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "file").strip() or "file"
    return re.sub(r"[^\w.\-() ]+", "_", name)[:120]


@app.post("/api/desk/upload")
@need("workshop.use")
def desk_upload():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return {"error": "Выберите файл"}, 400
    used = db().scalars(select(M.DeskItem).where(M.DeskItem.member_id == g.me.id, M.DeskItem.kind == "file")).all()
    taken = sum(int((i.data or {}).get("size") or 0) for i in used)
    folder = os.path.join(FILES, g.me.id)
    os.makedirs(folder, exist_ok=True)
    name = _safe_name(upload.filename)
    stored = f"{g.me.id}/{secrets.token_hex(8)}_{name}"
    target = os.path.join(FILES, stored)
    upload.save(target)
    size = os.path.getsize(target)
    if size > MAX_FILE or taken + size > QUOTA:
        os.remove(target)
        return {"error": "Файл больше 25 МБ или закончилось место на столе (500 МБ)"}, 413
    item = M.DeskItem(member_id=g.me.id, kind="file", title=name, project_id=_project_ok(request.form.get("project_id")),
                      data={"path": stored, "name": name, "size": size, "type": upload.mimetype or ""})
    db().add(item)
    db().commit()
    return _item_json(item, {p.id: p.title for p in visible_projects(g.me)}), 201


@app.get("/desk/file/<int:item_id>")
@need("workshop.use")
def desk_file(item_id):
    item = db().get(M.DeskItem, item_id)
    if not item or item.kind != "file" or not _can_read(item):
        abort(404)
    data = item.data or {}
    path = os.path.join(FILES, data.get("path") or "")
    if not data.get("path") or not os.path.isfile(path):
        abort(404)
    kind = data.get("type") or "application/octet-stream"
    response = send_file(path, mimetype=kind, as_attachment=kind not in INLINE,
                         download_name=data.get("name") or "file")
    response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


def materials_count(project_id: int) -> int:
    return db().scalar(select(func.count()).select_from(M.DeskItem).where(
        M.DeskItem.project_id == project_id, M.DeskItem.shared.is_(True))) or 0
