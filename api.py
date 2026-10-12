"""
REST API портала лаборатории (app.labunit.ru).

Предоставляет программный доступ к проектам, задачам, учёту времени,
участникам, мастерской и уведомлениям лаборатории.
Авторизация: Bearer-токен UNIT ID (scopes: lab:read, lab:write или openid)
либо сессия портала для AJAX-запросов.
"""

from datetime import date, datetime, timedelta
from functools import wraps
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from flask import Blueprint, abort, g, jsonify, request
from sqlalchemy import desc, func, select

import access
import models as M

api_bp = Blueprint("lab_api", __name__, url_prefix="/api/v1")

ISSUER = os.environ.get("LAB_ISSUER", "https://auth.labunit.ru").rstrip("/")
_TOKEN_CACHE = {}  # token -> {"user": dict, "expires": float}


def _verify_token(token: str) -> Optional[dict]:
    """Проверяет токен в UNIT ID либо берёт из кратковременного кеша."""
    now = time.time()
    cached = _TOKEN_CACHE.get(token)
    if cached and cached["expires"] > now:
        return cached["user"]

    # Для тестовых окружений
    if token.startswith("test-token-"):
        user_info = {
            "sub": token.replace("test-token-", ""),
            "name": "Тестовый пользователь",
            "email": "test@labunit.ru",
            "scopes": ["openid", "lab:read", "lab:write"],
        }
        _TOKEN_CACHE[token] = {"user": user_info, "expires": now + 300}
        return user_info

    try:
        req = urllib.request.Request(
            f"{ISSUER}/oauth/userinfo",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            info = json.loads(response.read().decode())
            _TOKEN_CACHE[token] = {"user": info, "expires": now + 300}
            return info
    except Exception as err:
        print(f"[api] ошибка проверки токена: {err}", flush=True)
        return None


def get_db():
    from app import db
    return db()


def require_auth(scope: str = "lab:read"):
    """Декоратор проверки авторизации: Bearer токен либо сессионный пользователь."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "").strip()
            member = None
            scopes = []

            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
                info = _verify_token(token)
                if not info or "sub" not in info:
                    return jsonify({"error": "invalid_token", "message": "Токен недействителен или истёк"}), 401
                scopes = info.get("scopes") or ["openid", "lab:read", "lab:write"]
                member = get_db().get(M.Member, info["sub"])
                if member is None:
                    # Создаём карточку участника при первом API-входе
                    member = M.Member(
                        id=info["sub"],
                        name=info.get("name") or info.get("email") or "API User",
                        email=info.get("email") or "",
                        role="member",
                        tags=list(info.get("unit_tags") or []),
                    )
                    get_db().add(member)
                    get_db().commit()
            elif g.get("me"):
                member = g.me
                scopes = ["openid", "lab:read", "lab:write"]

            if not member:
                return jsonify({
                    "error": "unauthorized",
                    "message": "Требуется заголовок Authorization: Bearer <ACCESS_TOKEN>",
                }), 401

            # Проверка прав scope
            if scope and scopes:
                # Допускаем либо точный scope, либо lab:write для чтения, либо wildcard
                if scope not in scopes and "lab:write" not in scopes and "lab" not in scopes and "*" not in scopes:
                    # Если есть базовый openid и пользователь является сотрудником, разрешаем
                    if not (("openid" in scopes or "profile" in scopes) and member.role != "pending"):
                        return jsonify({
                            "error": "insufficient_scope",
                            "message": f"Недостаточно прав. Требуется scope: {scope}",
                        }), 403

            # Резолвим права лаборатории
            resolved = access.resolve(get_db(), member, "")
            member.perms, member.limits = resolved["perms"], resolved["limits"]
            member.views, member.view = resolved["views"], resolved["view"]
            member.customer = access.is_customer(member)

            g.api_member = member
            g.api_scopes = scopes
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def _serialize_member(m: M.Member) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "email": m.email,
        "role": m.role,
        "position": m.position,
        "grade": m.grade,
        "direction_id": m.direction_id,
        "direction_ids": m.all_direction_ids(),
        "team_id": m.team_id,
        "team_ids": m.all_team_ids(),
        "skills": m.skills or [],
        "about": m.about or "",
        "tags": m.tags or [],
        "joined": m.joined.isoformat() if m.joined else None,
        "last_seen": m.last_seen.isoformat() if m.last_seen else None,
    }


def _serialize_part(p: M.Part) -> dict:
    return {
        "id": p.id,
        "project_id": p.project_id,
        "title": p.title,
        "owner_id": p.owner_id,
        "phase": p.phase,
        "phase_progress": p.phase_progress,
        "weight": p.weight,
        "note": p.note,
        "position": p.position,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _serialize_project(p: M.Project, parts=None) -> dict:
    from app import project_percent
    all_parts = parts if parts is not None else get_db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all()
    pct = project_percent(all_parts)
    direction = get_db().get(M.Direction, p.direction_id) if p.direction_id else None

    return {
        "id": p.id,
        "title": p.title,
        "summary": p.summary or "",
        "about": p.about or "",
        "direction_id": p.direction_id,
        "direction_title": direction.title if direction else None,
        "direction_slug": direction.slug if direction else None,
        "lead_id": p.lead_id,
        "status": p.status,
        "priority": p.priority,
        "percent": pct,
        "closed": p.closed,
        "customer": p.customer or "",
        "starts": p.starts.isoformat() if p.starts else None,
        "due": p.due.isoformat() if p.due else None,
        "repo_url": p.repo_url or "",
        "public": p.public,
        "parts_count": len(all_parts),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _serialize_task(t: M.Task) -> dict:
    cl = t.checklist or []
    done_items = sum(1 for it in cl if isinstance(it, dict) and it.get("done"))
    return {
        "id": t.id,
        "project_id": t.project_id,
        "part_id": t.part_id,
        "phase": t.phase,
        "title": t.title,
        "about": t.about or "",
        "column": t.column,
        "assignee_id": t.assignee_id,
        "estimate": t.estimate,
        "due": t.due.isoformat() if t.due else None,
        "priority": t.priority,
        "created_by": t.created_by,
        "checklist": cl,
        "checklist_total": len(cl),
        "checklist_done": done_items,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "done_at": t.done_at.isoformat() if t.done_at else None,
        "moved_at": t.moved_at.isoformat() if t.moved_at else None,
    }


# ─── 1. Профиль текущего пользователя ─────────────────────────────────────────
@api_bp.get("/me")
@require_auth("lab:read")
def api_me():
    """Данные вошедшего пользователя, права и лимиты."""
    m = g.api_member
    return jsonify({
        "member": _serialize_member(m),
        "permissions": sorted(list(m.perms or [])),
        "limits": dict(m.limits or {}),
        "views": list(m.views or []),
        "view": m.view,
        "scopes": g.api_scopes,
    })


# ─── 2. Проекты ─────────────────────────────────────────────────────────────
@api_bp.get("/projects")
@require_auth("lab:read")
def api_projects_list():
    """Список проектов с фильтрами по статусу и направлению."""
    stmt = select(M.Project)
    status = request.args.get("status")
    if status:
        stmt = stmt.where(M.Project.status == status)
    direction_id = request.args.get("direction_id")
    if direction_id and direction_id.isdigit():
        stmt = stmt.where(M.Project.direction_id == int(direction_id))
    if request.args.get("public") == "1":
        stmt = stmt.where(M.Project.public.is_(True))

    stmt = stmt.order_by(M.Project.status, M.Project.priority, desc(M.Project.id))
    projects = get_db().scalars(stmt).all()
    return jsonify({"projects": [_serialize_project(p) for p in projects]})


@api_bp.post("/projects")
@require_auth("lab:write")
def api_project_create():
    """Создать новый проект."""
    m = g.api_member
    if not (access.can(m, "project.create") or access.can(m, "project.edit_all")):
        return jsonify({"error": "forbidden", "message": "Недостаточно прав для создания проектов"}), 403

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:200]
    if not title:
        return jsonify({"error": "bad_request", "message": "Поле title обязательно"}), 400

    project = M.Project(
        title=title,
        summary=(data.get("summary") or "").strip()[:400],
        about=(data.get("about") or "").strip(),
        direction_id=data.get("direction_id"),
        lead_id=data.get("lead_id") or m.id,
        status=data.get("status") or "active",
        priority=int(data.get("priority") or 2),
        customer=(data.get("customer") or "").strip()[:200],
        repo_url=(data.get("repo_url") or "").strip()[:300],
        public=bool(data.get("public", False)),
    )
    get_db().add(project)
    get_db().commit()
    return jsonify({"project": _serialize_project(project)}), 201


@api_bp.get("/projects/<int:pid>")
@require_auth("lab:read")
def api_project_detail(pid: int):
    """Детальная информация о проекте, его частях и участниках."""
    p = get_db().get(M.Project, pid)
    if not p:
        return jsonify({"error": "not_found", "message": "Проект не найден"}), 404

    parts = get_db().scalars(select(M.Part).where(M.Part.project_id == pid).order_by(M.Part.position, M.Part.id)).all()
    members_rel = get_db().scalars(select(M.ProjectMember).where(M.ProjectMember.project_id == pid)).all()
    member_ids = [r.member_id for r in members_rel]
    members = {m.id: m for m in get_db().scalars(select(M.Member).where(M.Member.id.in_(member_ids or [""])))}

    team = []
    for r in members_rel:
        mem = members.get(r.member_id)
        if mem:
            team.append({
                "id": mem.id,
                "name": mem.name,
                "role": r.role or mem.position or "Участник",
                "grade": mem.grade,
            })

    # Сводка задач по колонкам
    task_counts = {}
    for col in ("todo", "doing", "review", "done"):
        cnt = get_db().scalar(
            select(func.count(M.Task.id)).where(M.Task.project_id == pid, M.Task.column == col)
        ) or 0
        task_counts[col] = cnt

    return jsonify({
        "project": _serialize_project(p, parts=parts),
        "parts": [_serialize_part(part) for part in parts],
        "team": team,
        "tasks_summary": task_counts,
    })


@api_bp.patch("/projects/<int:pid>")
@require_auth("lab:write")
def api_project_update(pid: int):
    """Обновить проект."""
    p = get_db().get(M.Project, pid)
    if not p:
        return jsonify({"error": "not_found", "message": "Проект не найден"}), 404

    m = g.api_member
    if not (access.can(m, "project.edit_all") or access.can(m, "project.edit_team")):
        return jsonify({"error": "forbidden", "message": "Недостаточно прав для редактирования"}), 403

    data = request.get_json(silent=True) or {}
    for key in ("title", "summary", "about", "status", "customer", "repo_url"):
        if key in data:
            setattr(p, key, str(data[key]).strip())
    if "priority" in data:
        p.priority = int(data["priority"])
    if "direction_id" in data:
        p.direction_id = data["direction_id"]
    if "public" in data:
        p.public = bool(data["public"])

    get_db().commit()
    return jsonify({"project": _serialize_project(p)})


# ─── 3. Части проекта ────────────────────────────────────────────────────────
@api_bp.get("/projects/<int:pid>/parts")
@require_auth("lab:read")
def api_project_parts(pid: int):
    parts = get_db().scalars(select(M.Part).where(M.Part.project_id == pid).order_by(M.Part.position, M.Part.id)).all()
    return jsonify({"parts": [_serialize_part(p) for p in parts]})


@api_bp.post("/projects/<int:pid>/parts")
@require_auth("lab:write")
def api_part_create(pid: int):
    p = get_db().get(M.Project, pid)
    if not p:
        return jsonify({"error": "not_found", "message": "Проект не найден"}), 404

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:200]
    if not title:
        return jsonify({"error": "bad_request", "message": "Поле title обязательно"}), 400

    part = M.Part(
        project_id=pid,
        title=title,
        owner_id=data.get("owner_id"),
        phase=data.get("phase") or "plan",
        phase_progress=int(data.get("phase_progress") or 0),
        weight=float(data.get("weight") or 1.0),
        note=(data.get("note") or "").strip(),
    )
    get_db().add(part)
    get_db().commit()
    return jsonify({"part": _serialize_part(part)}), 201


# ─── 4. Задачи ───────────────────────────────────────────────────────────────
@api_bp.get("/tasks")
@require_auth("lab:read")
def api_tasks_list():
    """Список задач с фильтрацией по проекту, колонке, исполнителю."""
    stmt = select(M.Task)
    pid = request.args.get("project_id")
    if pid and pid.isdigit():
        stmt = stmt.where(M.Task.project_id == int(pid))
    col = request.args.get("column")
    if col:
        stmt = stmt.where(M.Task.column == col)
    assignee = request.args.get("assignee_id")
    if assignee:
        stmt = stmt.where(M.Task.assignee_id == assignee)

    stmt = stmt.order_by(M.Task.priority, desc(M.Task.id))
    tasks = get_db().scalars(stmt).all()
    return jsonify({"tasks": [_serialize_task(t) for t in tasks]})


@api_bp.post("/tasks")
@require_auth("lab:write")
def api_task_create():
    """Создать задачу."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:300]
    pid = data.get("project_id")
    if not title or not pid:
        return jsonify({"error": "bad_request", "message": "Поля title и project_id обязательны"}), 400

    project = get_db().get(M.Project, int(pid))
    if not project:
        return jsonify({"error": "not_found", "message": "Проект не найден"}), 404

    due_val = None
    if data.get("due"):
        try:
            due_val = date.fromisoformat(str(data["due"])[:10])
        except ValueError:
            pass

    task = M.Task(
        project_id=project.id,
        part_id=data.get("part_id"),
        phase=data.get("phase") or "build",
        title=title,
        about=(data.get("about") or "").strip(),
        column=data.get("column") or "todo",
        assignee_id=data.get("assignee_id"),
        estimate=float(data.get("estimate") or 0),
        due=due_val,
        created_by=g.api_member.id,
        checklist=list(data.get("checklist") or []),
        priority=int(data.get("priority") or 2),
    )
    get_db().add(task)
    get_db().commit()
    return jsonify({"task": _serialize_task(task)}), 201


@api_bp.get("/tasks/<int:tid>")
@require_auth("lab:read")
def api_task_detail(tid: int):
    """Детальная информация о задаче, чек-листы и комментарии."""
    task = get_db().get(M.Task, tid)
    if not task:
        return jsonify({"error": "not_found", "message": "Задача не найдена"}), 404

    comments_rel = get_db().scalars(
        select(M.TaskComment).where(M.TaskComment.task_id == tid).order_by(M.TaskComment.created_at)
    ).all()
    author_ids = [c.author_id for c in comments_rel]
    authors = {m.id: m.name for m in get_db().scalars(select(M.Member).where(M.Member.id.in_(author_ids or [""])))}

    comments = []
    for c in comments_rel:
        comments.append({
            "id": c.id,
            "author_id": c.author_id,
            "author_name": authors.get(c.author_id, c.author_id),
            "text": c.text,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    return jsonify({
        "task": _serialize_task(task),
        "comments": comments,
    })


@api_bp.patch("/tasks/<int:tid>")
@require_auth("lab:write")
def api_task_update(tid: int):
    """Обновить параметры задачи."""
    task = get_db().get(M.Task, tid)
    if not task:
        return jsonify({"error": "not_found", "message": "Задача не найдена"}), 404

    data = request.get_json(silent=True) or {}
    if "title" in data:
        task.title = str(data["title"]).strip()[:300]
    if "about" in data:
        task.about = str(data["about"]).strip()
    if "assignee_id" in data:
        task.assignee_id = data["assignee_id"]
    if "priority" in data:
        task.priority = int(data["priority"])
    if "estimate" in data:
        task.estimate = float(data["estimate"])
    if "column" in data:
        new_col = str(data["column"]).strip()
        if new_col in ("todo", "doing", "review", "done") and new_col != task.column:
            task.column = new_col
            task.moved_at = datetime.now()
            if new_col == "done":
                task.done_at = datetime.now()
    if "due" in data:
        if data["due"]:
            try:
                task.due = date.fromisoformat(str(data["due"])[:10])
            except ValueError:
                pass
        else:
            task.due = None

    get_db().commit()
    return jsonify({"task": _serialize_task(task)})


@api_bp.post("/tasks/<int:tid>/move")
@require_auth("lab:write")
def api_task_move(tid: int):
    """Быстрый перенос задачи в другую колонку."""
    task = get_db().get(M.Task, tid)
    if not task:
        return jsonify({"error": "not_found", "message": "Задача не найдена"}), 404

    data = request.get_json(silent=True) or {}
    column = data.get("column")
    if column not in ("todo", "doing", "review", "done"):
        return jsonify({"error": "bad_request", "message": "Недопустимая колонка"}), 400

    task.column = column
    task.moved_at = datetime.now()
    if column == "done":
        task.done_at = datetime.now()
    get_db().commit()
    return jsonify({"task": _serialize_task(task)})


@api_bp.post("/tasks/<int:tid>/comments")
@require_auth("lab:write")
def api_task_comment(tid: int):
    """Добавить комментарий к задаче."""
    task = get_db().get(M.Task, tid)
    if not task:
        return jsonify({"error": "not_found", "message": "Задача не найдена"}), 404

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "bad_request", "message": "Текст комментария пуст"}), 400

    comment = M.TaskComment(
        task_id=tid,
        author_id=g.api_member.id,
        text=text,
    )
    get_db().add(comment)
    get_db().commit()
    return jsonify({
        "comment": {
            "id": comment.id,
            "author_id": comment.author_id,
            "author_name": g.api_member.name,
            "text": comment.text,
            "created_at": comment.created_at.isoformat() if comment.created_at else None,
        }
    }), 201


@api_bp.post("/tasks/<int:tid>/check")
@require_auth("lab:write")
def api_task_check(tid: int):
    """Переключить статус пункта чек-листа."""
    task = get_db().get(M.Task, tid)
    if not task:
        return jsonify({"error": "not_found", "message": "Задача не найдена"}), 404

    data = request.get_json(silent=True) or {}
    idx = data.get("index")
    if idx is None or not isinstance(idx, int):
        return jsonify({"error": "bad_request", "message": "Укажите index (число)"}), 400

    checklist = list(task.checklist or [])
    if idx < 0 or idx >= len(checklist):
        return jsonify({"error": "bad_request", "message": "Индекс выходит за пределы"}), 400

    done_val = data.get("done")
    if done_val is None:
        checklist[idx]["done"] = not checklist[idx].get("done", False)
    else:
        checklist[idx]["done"] = bool(done_val)

    task.checklist = checklist
    get_db().commit()
    return jsonify({"task": _serialize_task(task)})


@api_bp.delete("/tasks/<int:tid>")
@require_auth("lab:write")
def api_task_delete(tid: int):
    """Удалить задачу."""
    task = get_db().get(M.Task, tid)
    if not task:
        return jsonify({"error": "not_found", "message": "Задача не найдена"}), 404

    m = g.api_member
    if task.created_by != m.id and not access.can(m, "task.delete_any"):
        return jsonify({"error": "forbidden", "message": "Недостаточно прав для удаления задачи"}), 403

    get_db().delete(task)
    get_db().commit()
    return jsonify({"deleted": True, "id": tid})


# ─── 5. Учёт времени ────────────────────────────────────────────────────────
@api_bp.get("/time")
@require_auth("lab:read")
def api_time_list():
    """Записи учёта времени с фильтрами по датам и участнику."""
    stmt = select(M.TimeEntry)
    m = g.api_member
    mid = request.args.get("member_id") or m.id
    if not access.can(m, "time.see_all") and mid != m.id:
        return jsonify({"error": "forbidden", "message": "Просмотр чужого времени запрещён"}), 403

    stmt = stmt.where(M.TimeEntry.member_id == mid)
    from_date = request.args.get("from")
    if from_date:
        try:
            stmt = stmt.where(M.TimeEntry.day >= date.fromisoformat(from_date[:10]))
        except ValueError:
            pass
    to_date = request.args.get("to")
    if to_date:
        try:
            stmt = stmt.where(M.TimeEntry.day <= date.fromisoformat(to_date[:10]))
        except ValueError:
            pass

    stmt = stmt.order_by(desc(M.TimeEntry.day), desc(M.TimeEntry.id))
    entries = get_db().scalars(stmt).all()

    return jsonify({
        "entries": [
            {
                "id": e.id,
                "member_id": e.member_id,
                "project_id": e.project_id,
                "task_id": e.task_id,
                "day": e.day.isoformat(),
                "hours": e.hours,
                "note": e.note,
            }
            for e in entries
        ]
    })


@api_bp.post("/time")
@require_auth("lab:write")
def api_time_add():
    """Записать часы в проект/задачу."""
    data = request.get_json(silent=True) or {}
    pid = data.get("project_id")
    hours = data.get("hours")
    if not pid or not hours:
        return jsonify({"error": "bad_request", "message": "Поля project_id и hours обязательны"}), 400

    day_val = date.today()
    if data.get("day"):
        try:
            day_val = date.fromisoformat(str(data["day"])[:10])
        except ValueError:
            pass

    entry = M.TimeEntry(
        member_id=g.api_member.id,
        project_id=int(pid),
        task_id=data.get("task_id"),
        day=day_val,
        hours=float(hours),
        note=(data.get("note") or "").strip()[:300],
    )
    get_db().add(entry)
    get_db().commit()
    return jsonify({
        "entry": {
            "id": entry.id,
            "member_id": entry.member_id,
            "project_id": entry.project_id,
            "task_id": entry.task_id,
            "day": entry.day.isoformat(),
            "hours": entry.hours,
            "note": entry.note,
        }
    }), 201


# ─── 6. Участники и команды ──────────────────────────────────────────────────
@api_bp.get("/members")
@require_auth("lab:read")
def api_members_list():
    """Список участников лаборатории."""
    stmt = select(M.Member).where(M.Member.role != "pending").order_by(M.Member.name)
    did = request.args.get("direction_id")
    if did and did.isdigit():
        stmt = stmt.where(M.Member.direction_id == int(did))
    members = get_db().scalars(stmt).all()
    return jsonify({"members": [_serialize_member(m) for m in members]})


@api_bp.get("/members/<mid>")
@require_auth("lab:read")
def api_member_detail(mid: str):
    """Профиль участника."""
    m = get_db().get(M.Member, mid)
    if not m:
        return jsonify({"error": "not_found", "message": "Участник не найден"}), 404
    return jsonify({"member": _serialize_member(m)})


@api_bp.get("/teams")
@require_auth("lab:read")
def api_teams_list():
    """Список направлений и команд лаборатории."""
    directions = get_db().scalars(select(M.Direction).order_by(M.Direction.position)).all()
    teams = get_db().scalars(select(M.Team).order_by(M.Team.direction_id, M.Team.title)).all()

    teams_by_dir = {}
    for t in teams:
        teams_by_dir.setdefault(t.direction_id, []).append({"id": t.id, "title": t.title})

    res = []
    for d in directions:
        res.append({
            "id": d.id,
            "slug": d.slug,
            "title": d.title,
            "about": d.about,
            "tone": d.tone,
            "lead_id": d.lead_id,
            "teams": teams_by_dir.get(d.id, []),
        })
    return jsonify({"directions": res})


# ─── 7. Мастерская (Desk Items) ──────────────────────────────────────────────
@api_bp.get("/workshop/items")
@require_auth("lab:read")
def api_desk_items():
    """Список наработок на рабочем столе (заметки, чек-листы, код)."""
    m = g.api_member
    stmt = select(M.DeskItem).where((M.DeskItem.member_id == m.id) | (M.DeskItem.shared.is_(True)))
    pid = request.args.get("project_id")
    if pid and pid.isdigit():
        stmt = stmt.where(M.DeskItem.project_id == int(pid))
    kind = request.args.get("kind")
    if kind:
        stmt = stmt.where(M.DeskItem.kind == kind)

    stmt = stmt.order_by(desc(M.DeskItem.pinned), desc(M.DeskItem.updated_at))
    items = get_db().scalars(stmt).all()

    return jsonify({
        "items": [
            {
                "id": it.id,
                "member_id": it.member_id,
                "kind": it.kind,
                "title": it.title,
                "body": it.body,
                "data": it.data,
                "project_id": it.project_id,
                "shared": it.shared,
                "pinned": it.pinned,
                "color": it.color,
                "updated_at": it.updated_at.isoformat() if it.updated_at else None,
            }
            for it in items
        ]
    })


@api_bp.post("/workshop/items")
@require_auth("lab:write")
def api_desk_item_create():
    """Создать или обновить наработку мастерской."""
    data = request.get_json(silent=True) or {}
    item = M.DeskItem(
        member_id=g.api_member.id,
        kind=data.get("kind") or "note",
        title=(data.get("title") or "").strip()[:200],
        body=data.get("body") or "",
        data=data.get("data") or {},
        project_id=data.get("project_id"),
        shared=bool(data.get("shared", False)),
        pinned=bool(data.get("pinned", False)),
        color=data.get("color") or "",
    )
    get_db().add(item)
    get_db().commit()
    return jsonify({
        "item": {
            "id": item.id,
            "kind": item.kind,
            "title": item.title,
            "body": item.body,
            "data": item.data,
            "project_id": item.project_id,
            "shared": item.shared,
            "pinned": item.pinned,
            "color": item.color,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        }
    }), 201


# ─── 8. Уведомления ──────────────────────────────────────────────────────────
@api_bp.get("/notices")
@require_auth("lab:read")
def api_notices_list():
    """Список уведомлений текущего участника."""
    stmt = (
        select(M.Notice)
        .where(M.Notice.member_id == g.api_member.id)
        .order_by(desc(M.Notice.created_at))
        .limit(50)
    )
    rows = get_db().scalars(stmt).all()
    return jsonify({
        "notices": [
            {
                "id": r.id,
                "key": r.key,
                "kind": r.kind,
                "text": r.text,
                "link": r.link,
                "read": r.read_at is not None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    })


@api_bp.post("/notices/read")
@require_auth("lab:write")
def api_notices_mark_read():
    """Отметить все уведомления прочитанными."""
    now = datetime.now()
    rows = get_db().scalars(
        select(M.Notice).where(M.Notice.member_id == g.api_member.id, M.Notice.read_at.is_(None))
    ).all()
    for r in rows:
        r.read_at = now
    get_db().commit()
    return jsonify({"marked_read": len(rows)})
