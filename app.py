"""
Портал IT-лаборатории (app.labunit.ru): проекты, люди, направления, учёт времени.

Вход — только через UNIT ID (auth.labunit.ru, OpenID Connect + PKCE). Первый вход
создаёт карточку со статусом «ожидает подтверждения»; руководитель лаборатории
назначает роль. Администраторы UNIT сразу становятся руководителями лаборатории.

Кто что видит:
    head      — всё; назначает роли, правит направления
    lead      — открытые проекты и проекты своего направления; ведёт проекты
    member    — открытые проекты и закрытые, где он в команде
    customer  — только свои проекты: готовность, части, журнал «для заказчика»
"""

import base64
import hashlib
import json
import os
import secrets
import smtplib
import threading
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func, select
from werkzeug.middleware.proxy_fix import ProxyFix

import models as M

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
def _secret() -> str:
    """Ключ сессий: из окружения или из data/.secret (создаётся один раз и переживает выкладки)."""
    if os.environ.get("LAB_SECRET"):
        return os.environ["LAB_SECRET"]
    path = os.path.join(M.DATA_DIR, ".secret")
    if not os.path.exists(path):
        with open(path, "w") as handle:
            handle.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as handle:
        return handle.read().strip()


app.secret_key = _secret()
app.config.update(SESSION_COOKIE_NAME="lab_session", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("LAB_SECURE", "1") == "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))

ISSUER = os.environ.get("LAB_ISSUER", "https://auth.labunit.ru")
CLIENT_ID = os.environ.get("LAB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("LAB_CLIENT_SECRET", "")
SITE = os.environ.get("LAB_SITE", "https://app.labunit.ru")
SMTP_HOST = os.environ.get("LAB_SMTP_HOST", "")
MAIL_FROM = os.environ.get("LAB_MAIL_FROM", "Лаборатория UNIT <noreply@labunit.ru>")
app.config["LAB_CONTACT"] = os.environ.get("LAB_CONTACT", "")       # почта для заявок на лендинге
M.init()


# ─── Общее ──────────────────────────────────────────────────────────────────
def db():
    if "db" not in g:
        g.db = M.Session()
    return g.db


@app.teardown_appcontext
def _close(_exc):
    s = g.pop("db", None)
    if s is not None:
        s.close()


def me():
    mid = session.get("mid")
    return db().get(M.Member, mid) if mid else None


@app.before_request
def guard():
    # CSRF: меняющий запрос — только со своего сайта (Sec-Fetch-Site / Origin)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        site = request.headers.get("Sec-Fetch-Site")
        origin = request.headers.get("Origin") or ""
        if (site and site not in ("same-origin", "none")) or (not site and origin and
                                                              urllib.parse.urlsplit(origin).netloc != request.host):
            abort(403)
    person = me()
    g.me = person
    if person:
        now = datetime.now()
        if not person.last_seen or now - person.last_seen > timedelta(minutes=5):
            person.last_seen = now
            visit = db().scalar(select(M.Visit).where(M.Visit.member_id == person.id, M.Visit.day == now.date()))
            if visit:
                visit.hits += 1
            else:
                db().add(M.Visit(member_id=person.id, day=now.date()))
            db().commit()


@app.after_request
def headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Content-Security-Policy",
                                "object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self' " + ISSUER)
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


def need(*roles):
    """Пускает вошедших с одной из ролей (по умолчанию — всех, кроме ожидающих)."""
    def wrap(fn):
        @wraps(fn)
        def inner(*a, **k):
            if not g.me:
                return redirect(url_for("login", next=request.full_path))
            if g.me.role == "pending":
                return render_template("pending.html")
            if roles and g.me.role not in roles:
                abort(403)
            return fn(*a, **k)
        return inner
    return wrap


STAFF = ("head", "lead", "member")


# ─── Готовность ─────────────────────────────────────────────────────────────
PHASE_INDEX = {key: i for i, (key, _, _) in enumerate(M.PHASES)}
PHASE_NAME = {key: name for key, name, _ in M.PHASES}


def part_percent(part) -> int:
    i = PHASE_INDEX.get(part.phase, 0)
    done = sum(w for _, _, w in M.PHASES[:i])
    return min(100, round(done + M.PHASES[i][2] * max(0, min(100, part.phase_progress)) / 100))


def project_percent(parts) -> int:
    total = sum(max(0.1, p.weight) for p in parts)
    return round(sum(part_percent(p) * max(0.1, p.weight) for p in parts) / total) if parts else 0


def phase_summary(parts) -> list:
    """По этапам: сколько частей уже прошли этап (для полосы «Планирование → Релиз»)."""
    out = []
    for key, name, _ in M.PHASES:
        i = PHASE_INDEX[key]
        passed = sum(1 for p in parts if PHASE_INDEX.get(p.phase, 0) > i or
                     (PHASE_INDEX.get(p.phase, 0) == i and p.phase_progress >= 100))
        current = sum(1 for p in parts if p.phase == key and p.phase_progress < 100)
        out.append({"key": key, "name": name, "passed": passed, "current": current, "total": len(parts)})
    return out


# ─── Доступ к проектам ──────────────────────────────────────────────────────
def team_ids(project_id):
    return {m.member_id: m.role for m in db().scalars(select(M.ProjectMember).where(M.ProjectMember.project_id == project_id))}


def can_see(person, project) -> bool:
    if person.role == "head":
        return True
    team = team_ids(project.id)
    if person.role == "customer":
        return person.id in team
    if person.id in team or project.lead_id == person.id:
        return True
    if person.role == "lead" and person.direction_id and person.direction_id == project.direction_id:
        return True
    return not project.closed


def can_edit(person, project) -> bool:
    if person.role == "head" or project.lead_id == person.id:
        return True
    return person.role == "lead" and person.direction_id == project.direction_id


def is_team(person, project) -> bool:
    return can_edit(person, project) or (person.role in STAFF and person.id in team_ids(project.id))


def visible_projects(person):
    rows = db().scalars(select(M.Project).order_by(M.Project.status, M.Project.priority, M.Project.title)).all()
    return [p for p in rows if can_see(person, p)]


def project_or_404(pid, edit=False):
    project = db().get(M.Project, pid)
    if not project or not can_see(g.me, project):
        abort(404)
    if edit and not can_edit(g.me, project):
        abort(403)
    return project


# ─── Уведомления по почте ───────────────────────────────────────────────────
def notify(member_id: str, subject: str, text: str, link: str = "") -> None:
    """Письмо человеку (в фоне). Без настроенного SMTP — молча пропускаем."""
    person = db().get(M.Member, member_id) if member_id else None
    if not SMTP_HOST or not person or not person.email or not person.notify_email or person.id == getattr(g.me, "id", None):
        return
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = MAIL_FROM, person.email, subject
    msg.set_content(f"{person.name}, здравствуйте.\n\n{text}\n\n{SITE + link if link else SITE}\n\n"
                    "Отключить письма можно в своём профиле на портале лаборатории.")

    def send():
        try:
            with smtplib.SMTP(SMTP_HOST, 25, timeout=20) as smtp:
                smtp.send_message(msg)
        except Exception as error:  # noqa: BLE001
            print(f"[mail] не отправлено: {error}", flush=True)
    threading.Thread(target=send, daemon=True).start()


# ─── Вход через UNIT ID ─────────────────────────────────────────────────────
def _post(url, data, auth=None):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode())


@app.route("/login")
def login():
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(16)
    session["oidc"] = {"verifier": verifier, "state": state, "next": request.args.get("next") or "/home"}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urllib.parse.urlencode({"client_id": CLIENT_ID, "response_type": "code", "scope": "openid profile email",
                                    "redirect_uri": SITE + "/auth/callback", "state": state,
                                    "code_challenge": challenge, "code_challenge_method": "S256"})
    return redirect(f"{ISSUER}/oauth/authorize?{query}")


@app.route("/auth/callback")
def callback():
    saved = session.pop("oidc", None) or {}
    if not saved or request.args.get("state") != saved.get("state"):
        return render_template("message.html", title="Вход не удался", text="Попробуйте войти ещё раз."), 400
    if request.args.get("error"):
        return render_template("message.html", title="Вход отменён",
                               text="Вы не разрешили доступ к аккаунту UNIT."), 400
    try:
        tokens = _post(f"{ISSUER}/oauth/token", {"grant_type": "authorization_code", "code": request.args.get("code"),
                                                 "redirect_uri": SITE + "/auth/callback",
                                                 "code_verifier": saved["verifier"]}, (CLIENT_ID, CLIENT_SECRET))
        req = urllib.request.Request(f"{ISSUER}/oauth/userinfo",
                                     headers={"Authorization": "Bearer " + tokens["access_token"]})
        with urllib.request.urlopen(req, timeout=20) as response:
            info = json.loads(response.read().decode())
    except Exception as error:  # noqa: BLE001
        print(f"[auth] {error}", flush=True)
        return render_template("message.html", title="Вход не удался",
                               text="UNIT ID не ответил. Попробуйте через минуту."), 502
    person = db().get(M.Member, info["sub"])
    if person is None:
        first = not db().scalar(select(func.count()).select_from(M.Member))
        admin = "admin" in (info.get("unit_roles") or [])
        person = M.Member(id=info["sub"], name=info.get("name") or "Без имени", email=info.get("email") or "",
                          role="head" if (first or admin) else "pending")
        db().add(person)
    else:
        person.name = info.get("name") or person.name
        person.email = info.get("email") or person.email
    db().commit()
    session.clear()
    session.permanent = True
    session["mid"] = person.id
    nxt = saved.get("next") or "/home"
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/home")


@app.route("/logout")
def logout():
    """Выход и из портала, и из UNIT ID — иначе «Войти» сразу пустит обратно без пароля."""
    session.clear()
    return redirect(f"{ISSUER}/logout?" + urllib.parse.urlencode({"next": SITE + "/"}))


# ─── Лендинг ────────────────────────────────────────────────────────────────
@app.route("/")
def landing():
    rows = db().scalars(select(M.Project).where(M.Project.public.is_(True)).order_by(M.Project.status, M.Project.priority)).all()
    showcase = []
    for p in rows:
        parts = db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all()
        showcase.append({"p": p, "percent": project_percent(parts), "direction": db().get(M.Direction, p.direction_id) if p.direction_id else None})
    directions = db().scalars(select(M.Direction).order_by(M.Direction.id)).all()
    people = db().scalar(select(func.count()).select_from(M.Member).where(M.Member.role.in_(STAFF)))
    return render_template("landing.html", showcase=showcase, directions=directions, people=people)


# ─── Главная сотрудника ─────────────────────────────────────────────────────
@app.route("/home")
@need()
def home():
    person = g.me
    projects = visible_projects(person)
    mine = [p for p in projects if person.id in team_ids(p.id) or p.lead_id == person.id] or \
        (projects if person.role == "head" else [])
    cards = []
    for p in mine[:12]:
        parts = db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all()
        cards.append({"p": p, "percent": project_percent(parts), "phases": phase_summary(parts)})
    tasks = db().scalars(select(M.Task).where(M.Task.assignee_id == person.id, M.Task.column != "done")
                         .order_by(M.Task.due.is_(None), M.Task.due)).all()
    week_start = date.today() - timedelta(days=date.today().weekday())
    hours = db().scalar(select(func.coalesce(func.sum(M.TimeEntry.hours), 0)).where(
        M.TimeEntry.member_id == person.id, M.TimeEntry.day >= week_start))
    names = {p.id: p.title for p in projects}
    online = db().scalars(select(M.Member).where(M.Member.last_seen >= datetime.now() - timedelta(minutes=15),
                                                 M.Member.role.in_(STAFF))).all()
    return render_template("home.html", cards=cards, tasks=tasks, hours=hours, names=names, online=online,
                           columns=dict(M.TASK_COLUMNS))


# ─── Люди ───────────────────────────────────────────────────────────────────
def activity(member_id: str) -> dict:
    since = date.today() - timedelta(days=30)
    days = db().scalar(select(func.count()).select_from(M.Visit).where(M.Visit.member_id == member_id, M.Visit.day >= since))
    return {"days30": days or 0}


@app.route("/people")
@need(*STAFF)
def people():
    direction = request.args.get("d")
    query = select(M.Member).where(M.Member.role.in_(STAFF)).order_by(M.Member.name)
    rows = db().scalars(query).all()
    if direction:
        rows = [m for m in rows if str(m.direction_id) == direction]
    dirs = {d.id: d for d in db().scalars(select(M.Direction))}
    teams = {t.id: t for t in db().scalars(select(M.Team))}
    acts = {m.id: activity(m.id) for m in rows}
    return render_template("people.html", rows=rows, dirs=dirs, teams=teams, acts=acts, active=direction)


@app.route("/people/<mid>", methods=["GET", "POST"])
@need(*STAFF)
def person(mid):
    member = db().get(M.Member, mid)
    if not member:
        abort(404)
    own, head = member.id == g.me.id, g.me.role == "head"
    if request.method == "POST":
        if not (own or head):
            abort(403)
        f = request.form
        member.position = f.get("position", "").strip()[:160]
        member.about = f.get("about", "").strip()[:2000]
        member.contacts = {"telegram": f.get("telegram", "").strip()[:60], "phone": f.get("phone", "").strip()[:30]}
        member.notify_email = f.get("notify_email") == "1"
        skills = []
        for line in f.get("skills", "").splitlines():
            name, _, level = line.partition("—")
            if name.strip():
                skills.append({"name": name.strip()[:60], "level": max(1, min(5, int(level.strip() or 3)))
                               if level.strip().isdigit() else 3})
        member.skills = skills[:30]
        if head:
            member.grade = f.get("grade", "") if f.get("grade", "") in M.GRADES else ""
            member.direction_id = int(f["direction_id"]) if f.get("direction_id", "").isdigit() else None
            member.team_id = int(f["team_id"]) if f.get("team_id", "").isdigit() else None
            if f.get("role") in M.ROLES and member.id != g.me.id:
                member.role = f["role"]
        db().commit()
        flash("Профиль сохранён", "ok")
        return redirect(url_for("person", mid=mid))
    links = db().scalars(select(M.ProjectMember).where(M.ProjectMember.member_id == mid)).all()
    projects = [(db().get(M.Project, l.project_id), l.role) for l in links]
    projects = [(p, r) for p, r in projects if p and can_see(g.me, p)]
    since = date.today() - timedelta(days=83)
    visits = {v.day: v.hits for v in db().scalars(select(M.Visit).where(M.Visit.member_id == mid, M.Visit.day >= since))}
    grid = [{"day": since + timedelta(days=i), "hits": visits.get(since + timedelta(days=i), 0)} for i in range(84)]
    open_tasks = db().scalar(select(func.count()).select_from(M.Task).where(M.Task.assignee_id == mid, M.Task.column != "done"))
    done30 = db().scalar(select(func.count()).select_from(M.Task).where(
        M.Task.assignee_id == mid, M.Task.done_at >= datetime.now() - timedelta(days=30)))
    return render_template("person.html", m=member, own=own, head=head, projects=projects, grid=grid,
                           act=activity(mid), open_tasks=open_tasks, done30=done30,
                           dirs=db().scalars(select(M.Direction)).all(), teams=db().scalars(select(M.Team)).all(),
                           grades=M.GRADES, roles=M.ROLES)


# ─── Направления ────────────────────────────────────────────────────────────
@app.route("/d/<slug>", methods=["GET", "POST"])
@need(*STAFF)
def direction(slug):
    d = db().scalar(select(M.Direction).where(M.Direction.slug == slug))
    if not d:
        abort(404)
    if request.method == "POST":
        if g.me.role != "head":
            abort(403)
        if request.form.get("team"):
            db().add(M.Team(direction_id=d.id, title=request.form["team"].strip()[:120]))
        else:
            d.about = request.form.get("about", "").strip()[:2000]
            d.lead_id = request.form.get("lead_id") or None
        db().commit()
        return redirect(url_for("direction", slug=slug))
    teams = db().scalars(select(M.Team).where(M.Team.direction_id == d.id)).all()
    members = db().scalars(select(M.Member).where(M.Member.direction_id == d.id, M.Member.role.in_(STAFF))
                           .order_by(M.Member.name)).all()
    projects = [p for p in visible_projects(g.me) if p.direction_id == d.id]
    percents = {p.id: project_percent(db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all()) for p in projects}
    staff = db().scalars(select(M.Member).where(M.Member.role.in_(STAFF)).order_by(M.Member.name)).all()
    return render_template("direction.html", d=d, teams=teams, members=members, projects=projects,
                           percents=percents, staff=staff, lead=db().get(M.Member, d.lead_id) if d.lead_id else None)


# ─── Проекты ────────────────────────────────────────────────────────────────
@app.route("/projects", methods=["GET", "POST"])
@need()
def projects():
    if request.method == "POST":
        if g.me.role not in ("head", "lead"):
            abort(403)
        f = request.form
        p = M.Project(title=f.get("title", "").strip()[:200] or "Новый проект", summary=f.get("summary", "").strip()[:400],
                      direction_id=int(f["direction_id"]) if f.get("direction_id", "").isdigit() else g.me.direction_id,
                      lead_id=g.me.id, closed=f.get("closed") == "1")
        db().add(p)
        db().flush()
        db().add(M.ProjectMember(project_id=p.id, member_id=g.me.id, role="Руководитель проекта"))
        db().commit()
        return redirect(url_for("project", pid=p.id))
    rows = visible_projects(g.me)
    status = request.args.get("status")
    if status:
        rows = [p for p in rows if p.status == status]
    info = {}
    for p in rows:
        parts = db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all()
        info[p.id] = {"percent": project_percent(parts), "phases": phase_summary(parts),
                      "lead": db().get(M.Member, p.lead_id) if p.lead_id else None}
    dirs = {d.id: d for d in db().scalars(select(M.Direction))}
    return render_template("projects.html", rows=rows, info=info, dirs=dirs, statuses=M.STATUSES, active=status)


@app.route("/p/<int:pid>")
@need()
def project(pid):
    p = project_or_404(pid)
    customer = g.me.role == "customer"
    parts = db().scalars(select(M.Part).where(M.Part.project_id == pid).order_by(M.Part.position, M.Part.id)).all()
    team = db().scalars(select(M.ProjectMember).where(M.ProjectMember.project_id == pid)).all()
    people = {m.id: m for m in db().scalars(select(M.Member).where(M.Member.id.in_([t.member_id for t in team] + [p.lead_id or ""])))}
    tasks = [] if customer else db().scalars(select(M.Task).where(M.Task.project_id == pid).order_by(M.Task.id)).all()
    updates_q = select(M.Update).where(M.Update.project_id == pid).order_by(M.Update.created_at.desc()).limit(30)
    if customer:
        updates_q = updates_q.where(M.Update.for_customer.is_(True))
    hours = [] if customer else db().execute(
        select(M.TimeEntry.member_id, func.sum(M.TimeEntry.hours)).where(M.TimeEntry.project_id == pid)
        .group_by(M.TimeEntry.member_id)).all()
    staff = db().scalars(select(M.Member).where(M.Member.role.in_(STAFF + ("customer",))).order_by(M.Member.name)).all()
    return render_template("project.html", p=p, parts=parts, percent=project_percent(parts), phase_rows=phase_summary(parts),
                           part_percent=part_percent, phase_name=PHASE_NAME, all_phases=M.PHASES, team=team,
                           people=people, tasks=tasks, columns=M.TASK_COLUMNS, updates=db().scalars(updates_q).all(),
                           hours=hours, staff=staff, statuses=M.STATUSES, customer=customer,
                           editable=can_edit(g.me, p), member=is_team(g.me, p),
                           dirs=db().scalars(select(M.Direction)).all(),
                           total_hours=sum(h or 0 for _, h in hours))


@app.route("/p/<int:pid>/edit", methods=["POST"])
@need(*STAFF)
def project_edit(pid):
    p = project_or_404(pid, edit=True)
    f = request.form
    p.title = f.get("title", p.title).strip()[:200] or p.title
    p.summary = f.get("summary", "").strip()[:400]
    p.about = f.get("about", "").strip()[:5000]
    p.status = f.get("status") if f.get("status") in M.STATUSES else p.status
    p.priority = int(f.get("priority") or 2) if (f.get("priority") or "2").isdigit() else 2
    p.direction_id = int(f["direction_id"]) if f.get("direction_id", "").isdigit() else p.direction_id
    p.customer = f.get("customer", "").strip()[:200]
    p.repo_url = f.get("repo_url", "").strip()[:300] if f.get("repo_url", "").startswith(("https://", "http://")) or not f.get("repo_url") else p.repo_url
    p.closed = f.get("closed") == "1"
    p.public = f.get("public") == "1"
    p.public_title = f.get("public_title", "").strip()[:200]
    p.public_text = f.get("public_text", "").strip()[:3000]
    for key in ("starts", "due"):
        try:
            setattr(p, key, date.fromisoformat(f.get(key)) if f.get(key) else None)
        except ValueError:
            pass
    db().commit()
    flash("Проект сохранён", "ok")
    return redirect(url_for("project", pid=pid) + "#about")


@app.route("/p/<int:pid>/team", methods=["POST"])
@need(*STAFF)
def project_team(pid):
    p = project_or_404(pid, edit=True)
    f = request.form
    if f.get("remove"):
        row = db().scalar(select(M.ProjectMember).where(M.ProjectMember.project_id == pid, M.ProjectMember.member_id == f["remove"]))
        if row:
            db().delete(row)
    elif f.get("member_id") and db().get(M.Member, f["member_id"]):
        row = db().scalar(select(M.ProjectMember).where(M.ProjectMember.project_id == pid, M.ProjectMember.member_id == f["member_id"]))
        if not row:
            db().add(M.ProjectMember(project_id=pid, member_id=f["member_id"], role=f.get("role", "").strip()[:80]))
            notify(f["member_id"], f"Вас добавили в проект «{p.title}»", f"Роль в проекте: {f.get('role') or 'участник'}.",
                   f"/p/{pid}")
    db().commit()
    return redirect(url_for("project", pid=pid) + "#team")


@app.route("/p/<int:pid>/part", methods=["POST"])
@need(*STAFF)
def project_part(pid):
    p = project_or_404(pid)
    if not is_team(g.me, p):
        abort(403)
    f = request.form
    part = db().get(M.Part, int(f["part_id"])) if f.get("part_id", "").isdigit() else None
    if part and part.project_id != pid:
        abort(404)
    if f.get("delete") and part and can_edit(g.me, p):
        db().delete(part)
    else:
        if part is None:
            if not can_edit(g.me, p):
                abort(403)
            part = M.Part(project_id=pid, title="", position=db().scalar(
                select(func.coalesce(func.max(M.Part.position), 0)).where(M.Part.project_id == pid)) + 1)
            db().add(part)
        part.title = f.get("title", part.title or "").strip()[:200] or part.title or "Часть проекта"
        if f.get("phase") in PHASE_INDEX:
            part.phase = f["phase"]
        try:
            part.phase_progress = max(0, min(100, int(f.get("phase_progress", part.phase_progress or 0))))
        except ValueError:
            pass
        # этап закрыт на 100% — часть сама переходит на следующий (кроме релиза)
        if part.phase_progress >= 100 and part.phase != "release" and f.get("advance") == "1":
            part.phase = M.PHASES[PHASE_INDEX[part.phase] + 1][0]
            part.phase_progress = 0
        if "weight" in f:
            try:
                part.weight = max(0.1, min(10, float(f["weight"].replace(",", "."))))
            except ValueError:
                pass
        if "owner_id" in f:
            part.owner_id = f["owner_id"] or None
        if "note" in f:
            part.note = f["note"].strip()[:1000]
        part.updated_at = datetime.now()
    db().commit()
    return redirect(url_for("project", pid=pid) + "#parts")


@app.route("/p/<int:pid>/task", methods=["POST"])
@need(*STAFF)
def task_save(pid):
    p = project_or_404(pid)
    if not is_team(g.me, p):
        abort(403)
    f = request.form
    task = db().get(M.Task, int(f["task_id"])) if f.get("task_id", "").isdigit() else None
    if task and task.project_id != pid:
        abort(404)
    if task is None:
        task = M.Task(project_id=pid, title="", created_by=g.me.id)
        db().add(task)
    before = task.assignee_id
    if f.get("title"):
        task.title = f["title"].strip()[:300]
    if "about" in f:
        task.about = f["about"].strip()[:5000]
    if f.get("column") in dict(M.TASK_COLUMNS):
        task.column = f["column"]
        task.done_at = datetime.now() if task.column == "done" else None
    if f.get("phase") in PHASE_INDEX:
        task.phase = f["phase"]
    if "part_id" in f:
        task.part_id = int(f["part_id"]) if f["part_id"].isdigit() else None
    if "assignee_id" in f:
        task.assignee_id = f["assignee_id"] or None
    if f.get("estimate"):
        try:
            task.estimate = max(0, float(f["estimate"].replace(",", ".")))
        except ValueError:
            pass
    if "due" in f:
        try:
            task.due = date.fromisoformat(f["due"]) if f["due"] else None
        except ValueError:
            pass
    if not task.title:
        db().rollback()
        return redirect(url_for("project", pid=pid) + "#tasks")
    db().commit()
    if task.assignee_id and task.assignee_id != before:
        notify(task.assignee_id, f"Новая задача: {task.title}", f"Проект «{p.title}». Срок: {task.due or 'не указан'}.",
               f"/p/{pid}#tasks")
    return redirect(url_for("project", pid=pid) + "#tasks")


@app.route("/p/<int:pid>/update", methods=["POST"])
@need(*STAFF)
def project_update(pid):
    p = project_or_404(pid)
    if not is_team(g.me, p):
        abort(403)
    text = request.form.get("text", "").strip()[:4000]
    if text:
        db().add(M.Update(project_id=pid, author_id=g.me.id, text=text, for_customer=request.form.get("for_customer") == "1"))
        db().commit()
        if request.form.get("for_customer") == "1":
            for mid, role in team_ids(pid).items():
                member = db().get(M.Member, mid)
                if member and member.role == "customer":
                    notify(mid, f"Новости по проекту «{p.title}»", text, f"/p/{pid}")
    return redirect(url_for("project", pid=pid) + "#journal")


# ─── Учёт времени ───────────────────────────────────────────────────────────
@app.route("/time", methods=["GET", "POST"])
@need(*STAFF)
def timesheet():
    if request.method == "POST":
        f = request.form
        if f.get("delete", "").isdigit():
            row = db().get(M.TimeEntry, int(f["delete"]))
            if row and row.member_id == g.me.id:
                db().delete(row)
        else:
            p = project_or_404(int(f.get("project_id") or 0))
            try:
                hours = max(0.25, min(24, float(f.get("hours", "0").replace(",", "."))))
                day = date.fromisoformat(f.get("day")) if f.get("day") else date.today()
            except ValueError:
                flash("Проверьте часы и дату", "err")
                return redirect(url_for("timesheet"))
            db().add(M.TimeEntry(member_id=g.me.id, project_id=p.id, day=day, hours=hours,
                                 task_id=int(f["task_id"]) if f.get("task_id", "").isdigit() else None,
                                 note=f.get("note", "").strip()[:300]))
        db().commit()
        return redirect(url_for("timesheet", week=request.args.get("week", "")))
    try:
        start = date.fromisoformat(request.args.get("week")) if request.args.get("week") else None
    except ValueError:
        start = None
    start = start or (date.today() - timedelta(days=date.today().weekday()))
    start = start - timedelta(days=start.weekday())
    days = [start + timedelta(days=i) for i in range(7)]
    entries = db().scalars(select(M.TimeEntry).where(M.TimeEntry.member_id == g.me.id, M.TimeEntry.day >= start,
                                                     M.TimeEntry.day < start + timedelta(days=7))
                           .order_by(M.TimeEntry.day)).all()
    projects = [p for p in visible_projects(g.me) if is_team(g.me, p)]
    names = {p.id: p.title for p in visible_projects(g.me)}
    tasks = db().scalars(select(M.Task).where(M.Task.assignee_id == g.me.id, M.Task.column != "done")).all()
    return render_template("time.html", days=days, entries=entries, projects=projects, names=names, tasks=tasks,
                           start=start, prev=start - timedelta(days=7), next=start + timedelta(days=7))


# ─── Руководителю ───────────────────────────────────────────────────────────
@app.route("/admin", methods=["GET", "POST"])
@need("head")
def admin():
    if request.method == "POST":
        member = db().get(M.Member, request.form.get("member_id", ""))
        if member and member.id != g.me.id and request.form.get("role") in M.ROLES:
            member.role = request.form["role"]
            if request.form.get("company"):
                member.company = request.form["company"].strip()[:160]
            db().commit()
            if member.role != "pending":
                notify(member.id, "Доступ к порталу лаборатории открыт", f"Ваша роль: {M.ROLES[member.role]}.", "/home")
        return redirect(url_for("admin"))
    members = db().scalars(select(M.Member).order_by(M.Member.role != "pending", M.Member.name)).all()
    week_start = date.today() - timedelta(days=date.today().weekday())
    load = {}
    for m in members:
        load[m.id] = {
            "open": db().scalar(select(func.count()).select_from(M.Task).where(M.Task.assignee_id == m.id, M.Task.column != "done")),
            "hours": db().scalar(select(func.coalesce(func.sum(M.TimeEntry.hours), 0)).where(
                M.TimeEntry.member_id == m.id, M.TimeEntry.day >= week_start)),
            "days30": activity(m.id)["days30"]}
    projects = db().scalars(select(M.Project).where(M.Project.status == "active")).all()
    health = []
    for p in projects:
        parts = db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all()
        pct = project_percent(parts)
        late = bool(p.due and p.due < date.today() and pct < 100)
        risk = bool(p.due and p.starts and not late and p.due > p.starts and
                    (date.today() - p.starts).days / max(1, (p.due - p.starts).days) * 100 > pct + 20)
        health.append({"p": p, "percent": pct, "state": "late" if late else "risk" if risk else "ok"})
    return render_template("admin.html", members=members, roles=M.ROLES, load=load, health=health,
                           dirs={d.id: d for d in db().scalars(select(M.Direction))})


@app.errorhandler(403)
def denied(_e):
    return render_template("message.html", title="Нет доступа", text="Этот раздел вам недоступен."), 403


@app.errorhandler(404)
def missing(_e):
    return render_template("message.html", title="Страница не найдена", text="Возможно, её удалили или у вас нет доступа."), 404


# ─── Установка как приложения (PWA) ─────────────────────────────────────────
MANIFEST = {
    "name": "Лаборатория UNIT", "short_name": "Лаборатория", "description": "Проекты, люди и учёт времени IT-лаборатории UNIT",
    "lang": "ru", "start_url": "/home", "scope": "/", "display": "standalone",
    "background_color": "#f3f4f7", "theme_color": "#f3f4f7",
    "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
              {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
              {"src": "/static/icon-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
}


@app.get("/manifest.webmanifest")
def manifest():
    response = Response(json.dumps(MANIFEST, ensure_ascii=False), mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/sw.js")
def service_worker():
    response = app.send_static_file("sw.js")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/offline")
def offline():
    return render_template("offline.html")


@app.route("/healthz")
def healthz():
    db().scalar(select(func.count()).select_from(M.Direction))
    return {"ok": True}


@app.context_processor
def common():
    me_ = getattr(g, "me", None)
    nav_dirs = db().scalars(select(M.Direction).order_by(M.Direction.id)).all() if me_ and me_.role not in ("pending", "customer") else []
    return {"me": me_, "roles": M.ROLES, "statuses": M.STATUSES, "today": date.today(), "nav_dirs": nav_dirs,
            "issuer": ISSUER}
