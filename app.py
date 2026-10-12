"""
Портал IT-лаборатории (app.labunit.ru): проекты, люди, направления, учёт времени.

Вход — только через UNIT ID (auth.labunit.ru, OpenID Connect + PKCE). Первый вход
создаёт карточку со статусом «ожидает подтверждения»; руководитель лаборатории
назначает роль. Администраторы UNIT сразу становятся руководителями лаборатории.

Кто что видит:
    head      — всё; назначает роли, правит направления, настраивает ИИ
    lead      — открытые проекты и проекты своего направления; ведёт проекты
    member    — открытые проекты и закрытые, где он в команде
    customer  — только свои проекты: готовность, части, журнал «для заказчика»

У человека может быть несколько ролей (например, руководитель направления и
заказчик соседнего проекта): портал показывает данные выбранной роли, а
переключатель — в меню профиля. Модули: desk.py — рабочий стол, assist.py —
ИИ-помощник, control.py — сигналы и контроль выполнения.
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
        # Активная роль: карточку отцепляем от сессии базы, чтобы подмена роли
        # на время запроса не записалась в базу при чужом commit
        granted = person.all_roles()
        db().expunge(person)
        person.base_role, person.granted = person.role, granted
        wanted = session.get("as_role")
        if wanted in granted:
            person.role = wanted
    g.me = person


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
def notice(member_id: str, text: str, link: str = "", kind: str = "info", key: str = "") -> None:
    """Уведомление в портале (колокольчик). С key — не чаще одного на ключ."""
    if not member_id or member_id == getattr(g.get("me"), "id", None):
        return
    key = key or f"{kind}:{datetime.now().timestamp():.6f}"
    if db().scalar(select(M.Notice.id).where(M.Notice.member_id == member_id, M.Notice.key == key)):
        return
    db().add(M.Notice(member_id=member_id, key=key[:120], kind=kind, text=text[:400], link=link[:300]))
    db().commit()


def notify(member_id: str, subject: str, text: str, link: str = "", kind: str = "info", key: str = "") -> None:
    """Уведомление в портале и письмо (в фоне). Без настроенного SMTP — только в портале."""
    person = db().get(M.Member, member_id) if member_id else None
    if person and person.id != getattr(g.get("me"), "id", None):
        notice(person.id, subject, link, kind, key)
    if not SMTP_HOST or not person or not person.email or not person.notify_email or person.id == getattr(g.get("me"), "id", None):
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


LAB_TAG = "лаборатория"


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
    tags = info.get("unit_tags") or []
    person = db().get(M.Member, info["sub"])
    if person is None:
        first = not db().scalar(select(func.count()).select_from(M.Member))
        admin = "admin" in (info.get("unit_roles") or [])
        # Аккаунт UNIT с тегом «лаборатория» — сразу сотрудник, остальные ждут подтверждения
        person = M.Member(id=info["sub"], name=info.get("name") or "Без имени", email=info.get("email") or "",
                          role="head" if (first or admin) else "member" if LAB_TAG in tags else "pending")
        db().add(person)
    else:
        person.name = info.get("name") or person.name
        person.email = info.get("email") or person.email
        if person.role == "pending" and LAB_TAG in tags:
            person.role = "member"
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


# ─── «Сегодня»: главная сотрудника ──────────────────────────────────────────
def _task_bucket(task) -> str:
    today = date.today()
    if task.due and task.due < today:
        return "late"
    if task.due and task.due == today:
        return "today"
    if task.due and task.due <= today + timedelta(days=6 - today.weekday()):
        return "week"
    return "later"


@app.route("/home")
@need()
def home():
    import control

    person = g.me
    projects = visible_projects(person)
    mine = [p for p in projects if person.id in team_ids(p.id) or p.lead_id == person.id] or \
        (projects if person.role == "head" else [])
    cards = []
    for p in mine[:12]:
        parts = db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all()
        pct = project_percent(parts)
        cards.append({"p": p, "percent": pct, "phases": phase_summary(parts), "health": control.project_health(p, pct)})
    names = {p.id: p.title for p in projects}
    if person.role == "customer":
        news = db().scalars(select(M.Update).where(M.Update.project_id.in_([c["p"].id for c in cards] or [0]),
                                                   M.Update.for_customer.is_(True))
                            .order_by(M.Update.created_at.desc()).limit(10)).all()
        return render_template("home.html", cards=cards, names=names, news=news, customer=True)
    tasks = db().scalars(select(M.Task).where(M.Task.assignee_id == person.id, M.Task.column != "done")
                         .order_by(M.Task.due.is_(None), M.Task.due, M.Task.priority)).all()
    buckets = {"late": [], "today": [], "week": [], "later": []}
    for t in tasks:
        buckets[_task_bucket(t)].append(t)
    week_start = date.today() - timedelta(days=date.today().weekday())
    week = [week_start + timedelta(days=i) for i in range(7)]
    rows = db().execute(select(M.TimeEntry.day, func.sum(M.TimeEntry.hours)).where(
        M.TimeEntry.member_id == person.id, M.TimeEntry.day >= week_start).group_by(M.TimeEntry.day)).all()
    per_day = {d: 0.0 for d in week}
    per_day.update({d: float(h or 0) for d, h in rows})
    online = db().scalars(select(M.Member).where(M.Member.last_seen >= datetime.now() - timedelta(minutes=15),
                                                 M.Member.role.in_(STAFF))).all()
    pinned = db().scalars(select(M.DeskItem).where(M.DeskItem.member_id == person.id, M.DeskItem.pinned.is_(True))
                          .order_by(M.DeskItem.updated_at.desc()).limit(4)).all()
    checkin_today = db().scalar(select(M.Checkin).where(M.Checkin.member_id == person.id, M.Checkin.day == date.today()))
    brief = db().get(M.Setting, f"brief:{person.id}:{date.today()}")
    return render_template("home.html", cards=cards, names=names, customer=False, tasks=tasks, buckets=buckets,
                           per_day=per_day, hours=sum(per_day.values()), top=max(list(per_day.values()) + [8]),
                           online=online, pinned=pinned, checkin=checkin_today, columns=dict(M.TASK_COLUMNS),
                           signals=control.signals_for(person)[:6], brief=brief.value if brief else "",
                           kinds=M.DESK_KINDS)


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


def _skills_from(form) -> list:
    """Навыки из формы: чипы «Python|4» или строки «Python — 4»."""
    raw = form.getlist("skill") or form.get("skills", "").splitlines()
    out, seen = [], set()
    for line in raw:
        name, sep, level = line.partition("|") if "|" in line else line.partition("—")
        name = name.strip()[:60]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        level = level.strip()
        out.append({"name": name, "level": max(1, min(5, int(level))) if level.isdigit() else 3})
    return out[:30]


def set_roles(member, wanted) -> None:
    """Роли человека: основная — самая старшая из выданных."""
    granted = [r for r in M.ROLE_ORDER if r in set(wanted) and r != "pending"]
    if granted:
        member.roles, member.role = granted, granted[0]


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
        member.skills = _skills_from(f)
        if head:
            member.grade = f.get("grade", "") if f.get("grade", "") in M.GRADES else ""
            member.direction_id = int(f["direction_id"]) if f.get("direction_id", "").isdigit() else None
            member.team_id = int(f["team_id"]) if f.get("team_id", "").isdigit() else None
            if member.id != g.me.id and f.getlist("roles"):
                set_roles(member, f.getlist("roles"))
        db().commit()
        flash("Профиль сохранён", "ok")
        return redirect(url_for("person", mid=mid))
    links = db().scalars(select(M.ProjectMember).where(M.ProjectMember.member_id == mid)).all()
    projects = [(db().get(M.Project, l.project_id), l.role) for l in links]
    projects = [(p, r) for p, r in projects if p and can_see(g.me, p)]
    since = date.today() - timedelta(days=83)
    visits = {v.day: v.hits for v in db().scalars(select(M.Visit).where(M.Visit.member_id == mid, M.Visit.day >= since))}
    grid = [{"day": since + timedelta(days=i), "hits": visits.get(since + timedelta(days=i), 0)} for i in range(84)]
    open_tasks = db().scalars(select(M.Task).where(M.Task.assignee_id == mid, M.Task.column != "done")
                              .order_by(M.Task.due.is_(None), M.Task.due)).all()
    done30 = db().scalar(select(func.count()).select_from(M.Task).where(
        M.Task.assignee_id == mid, M.Task.done_at >= datetime.now() - timedelta(days=30)))
    week_start = date.today() - timedelta(days=date.today().weekday())
    hours = db().scalar(select(func.coalesce(func.sum(M.TimeEntry.hours), 0)).where(
        M.TimeEntry.member_id == mid, M.TimeEntry.day >= week_start))
    see_checkins = own or g.me.role in ("head", "lead")
    checkins = db().scalars(select(M.Checkin).where(M.Checkin.member_id == mid).order_by(M.Checkin.day.desc())
                            .limit(5)).all() if see_checkins else []
    return render_template("person.html", m=member, own=own, head=head, projects=projects, grid=grid,
                           act=activity(mid), open_tasks=open_tasks, done30=done30, hours=hours, checkins=checkins,
                           names={p.id: p.title for p, _ in projects},
                           dirs=db().scalars(select(M.Direction)).all(), teams=db().scalars(select(M.Team)).all(),
                           grades=M.GRADES, roles=M.ROLES, member_roles=member.all_roles(),
                           open_edit=own and not member.position and not member.skills)


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
                      starts=date.today(),
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
        import control
        pct = project_percent(parts)
        info[p.id] = {"percent": pct, "phases": phase_summary(parts), "health": control.project_health(p, pct),
                      "lead": db().get(M.Member, p.lead_id) if p.lead_id else None,
                      "open": db().scalar(select(func.count()).select_from(M.Task).where(
                          M.Task.project_id == p.id, M.Task.column != "done"))}
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
    comments = dict(db().execute(select(M.TaskComment.task_id, func.count()).where(
        M.TaskComment.task_id.in_([t.id for t in tasks] or [0])).group_by(M.TaskComment.task_id)).all())
    materials = [] if customer else db().scalars(select(M.DeskItem).where(
        M.DeskItem.project_id == pid, (M.DeskItem.shared.is_(True)) | (M.DeskItem.member_id == g.me.id))
        .order_by(M.DeskItem.updated_at.desc())).all()
    import control
    return render_template("project.html", p=p, parts=parts, percent=project_percent(parts), phase_rows=phase_summary(parts),
                           part_percent=part_percent, phase_name=PHASE_NAME, all_phases=M.PHASES, team=team,
                           people=people, tasks=tasks, columns=M.TASK_COLUMNS, updates=db().scalars(updates_q).all(),
                           hours=hours, staff=staff, statuses=M.STATUSES, customer=customer,
                           editable=can_edit(g.me, p), member=is_team(g.me, p),
                           dirs=db().scalars(select(M.Direction)).all(), comments=comments, materials=materials,
                           signals=[] if customer else control.project_signals(p, parts, tasks),
                           health=control.project_health(p, project_percent(parts)),
                           kinds=M.DESK_KINDS, total_hours=sum(h or 0 for _, h in hours))


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
    if f.get("column") in dict(M.TASK_COLUMNS) and f["column"] != task.column:
        set_column(task, f["column"], p)
    if f.get("priority", "").isdigit():
        task.priority = max(1, min(3, int(f["priority"])))
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
               f"/p/{pid}?task={task.id}#tasks", kind="task")
    if request.accept_mimetypes.best == "application/json":
        return {"ok": True, "id": task.id}
    return redirect(url_for("project", pid=pid) + "#tasks")


def set_column(task, column: str, project) -> None:
    """Задача переехала в другую колонку: время, отметка «готово», уведомление."""
    task.column = column
    task.moved_at = datetime.now()
    task.done_at = datetime.now() if column == "done" else None
    if column == "review" and project.lead_id and project.lead_id != getattr(g.me, "id", None):
        notice(project.lead_id, f"На проверке: {task.title}", f"/p/{project.id}?task={task.id}#tasks", "task",
               f"review:{task.id}:{task.moved_at:%Y%m%d%H%M}")


def task_or_404(tid):
    task = db().get(M.Task, tid)
    if not task:
        abort(404)
    p = project_or_404(task.project_id)
    if not is_team(g.me, p):
        abort(403)
    return task, p


def _task_json(task) -> dict:
    authors = {}
    comments = []
    for c in db().scalars(select(M.TaskComment).where(M.TaskComment.task_id == task.id).order_by(M.TaskComment.id)):
        if c.author_id not in authors:
            who = db().get(M.Member, c.author_id)
            authors[c.author_id] = who.name if who else "—"
        comments.append({"id": c.id, "author": authors[c.author_id], "mine": c.author_id == g.me.id,
                         "at": c.created_at.strftime("%d.%m %H:%M"), "text": c.text})
    return {"id": task.id, "title": task.title, "about": task.about, "column": task.column, "phase": task.phase,
            "part_id": task.part_id or "", "assignee_id": task.assignee_id or "", "priority": task.priority or 2,
            "estimate": ("%g" % task.estimate) if task.estimate else "", "due": task.due.isoformat() if task.due else "",
            "checklist": task.checklist or [], "comments": comments, "project_id": task.project_id,
            "created": task.created_at.strftime("%d.%m.%Y") if task.created_at else ""}


@app.get("/api/task/<int:tid>")
@need(*STAFF)
def task_api(tid):
    task, _ = task_or_404(tid)
    return _task_json(task)


@app.post("/api/task/<int:tid>/move")
@need(*STAFF)
def task_move(tid):
    task, p = task_or_404(tid)
    column = (request.get_json(silent=True) or request.form).get("column")
    if column not in dict(M.TASK_COLUMNS):
        return {"error": "Нет такой колонки"}, 400
    if column != task.column:
        set_column(task, column, p)
        db().commit()
    return {"ok": True, "column": task.column}


@app.post("/api/task/<int:tid>/check")
@need(*STAFF)
def task_check(tid):
    """Чек-лист задачи: отметить пункт, добавить, убрать."""
    task, _ = task_or_404(tid)
    data = request.get_json(silent=True) or {}
    items = [dict(i) for i in (task.checklist or [])]
    if data.get("add"):
        for line in str(data["add"]).splitlines():
            if line.strip():
                items.append({"text": line.strip()[:200], "done": False})
    elif isinstance(data.get("index"), int) and 0 <= data["index"] < len(items):
        if data.get("remove"):
            items.pop(data["index"])
        else:
            items[data["index"]]["done"] = bool(data.get("done"))
    task.checklist = items[:60]
    db().commit()
    return {"ok": True, "checklist": task.checklist}


@app.post("/api/task/<int:tid>/comment")
@need(*STAFF)
def task_comment(tid):
    task, p = task_or_404(tid)
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()[:3000]
    if not text:
        return {"error": "Пустой комментарий"}, 400
    db().add(M.TaskComment(task_id=task.id, author_id=g.me.id, text=text))
    db().commit()
    for mid in {task.assignee_id, task.created_by, p.lead_id} - {None, "", g.me.id}:
        notice(mid, f"{g.me.name} о задаче «{task.title}»: {text[:120]}", f"/p/{p.id}?task={task.id}#tasks", "task")
    return _task_json(task)


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
    per_day = {d: sum(e.hours for e in entries if e.day == d) for d in days}
    return render_template("time.html", days=days, entries=entries, projects=projects, names=names, tasks=tasks,
                           start=start, prev=start - timedelta(days=7), next=start + timedelta(days=7),
                           per_day=per_day, top=max(list(per_day.values()) + [8]),
                           this_week=date.today() - timedelta(days=date.today().weekday()),
                           timer=db().get(M.Timer, g.me.id))


@app.post("/timer")
@need(*STAFF)
def timer():
    """Таймер работы: запустил на задаче — остановил — часы записались сами."""
    f, back = request.form, _back(request.form.get("next"), "/time")
    running = db().get(M.Timer, g.me.id)
    action = f.get("action")
    if action == "start":
        task = db().get(M.Task, int(f["task_id"])) if f.get("task_id", "").isdigit() else None
        pid = task.project_id if task else int(f.get("project_id") or 0) if (f.get("project_id") or "").isdigit() else 0
        project_ = project_or_404(pid)
        if not is_team(g.me, project_):
            abort(403)
        if running:
            _stop_timer(running)
        db().add(M.Timer(member_id=g.me.id, project_id=project_.id, task_id=task.id if task else None,
                         note=(f.get("note") or (task.title if task else "")).strip()[:300]))
        if task and task.column == "todo":
            set_column(task, "doing", project_)
        db().commit()
        flash("Таймер запущен", "ok")
    elif action == "stop" and running:
        hours = _stop_timer(running)
        db().commit()
        flash(f"Записано {hours:g} ч", "ok")
    elif action == "cancel" and running:
        db().delete(running)
        db().commit()
    return redirect(back)


def _stop_timer(running) -> float:
    spent = (datetime.now() - running.started_at).total_seconds() / 3600
    hours = max(0.25, round(spent * 4) / 4)
    db().add(M.TimeEntry(member_id=running.member_id, project_id=running.project_id, task_id=running.task_id,
                         day=date.today(), hours=min(24, hours), note=running.note))
    db().delete(running)
    return hours


def _back(url: str, default: str) -> str:
    url = url or ""
    return url if url.startswith("/") and not url.startswith("//") and "\\" not in url else default


@app.post("/role")
@need()
def switch_role():
    """Переключить активную роль (если ролей несколько)."""
    wanted = request.form.get("role")
    if wanted in g.me.granted:
        session["as_role"] = wanted
        flash(f"Вы в роли «{M.ROLES[wanted]}»", "ok")
    return redirect(_back(request.form.get("next"), "/home") if wanted in ("head", "lead", "member") else "/home")


@app.post("/checkin")
@need(*STAFF)
def checkin():
    """Стендап: что сделал, что дальше, что мешает. Блокер сразу видят руководители."""
    f = request.form
    row = db().scalar(select(M.Checkin).where(M.Checkin.member_id == g.me.id, M.Checkin.day == date.today()))
    if row is None:
        row = M.Checkin(member_id=g.me.id, day=date.today())
        db().add(row)
    had = row.blockers
    row.done, row.plan = f.get("done", "").strip()[:2000], f.get("plan", "").strip()[:2000]
    row.blockers = f.get("blockers", "").strip()[:2000]
    row.mood = max(1, min(5, int(f["mood"]))) if f.get("mood", "").isdigit() else 3
    db().commit()
    if row.blockers and row.blockers != had:
        import control
        for mid in control.leads_of(g.me):
            notify(mid, f"{g.me.name}: мешает работать", row.blockers[:300], f"/pulse#checkins", kind="blocker",
                   key=f"blocker:{g.me.id}:{date.today()}")
    flash("Стендап записан", "ok")
    return redirect(_back(f.get("next"), "/home"))


@app.get("/notices")
@need()
def notices():
    rows = db().scalars(select(M.Notice).where(M.Notice.member_id == g.me.id)
                        .order_by(M.Notice.created_at.desc()).limit(80)).all()
    page = render_template("notices.html", rows=rows)
    for n in rows:
        if n.read_at is None:
            n.read_at = datetime.now()
    db().commit()
    return page


@app.get("/api/notices")
@need()
def notices_api():
    rows = db().scalars(select(M.Notice).where(M.Notice.member_id == g.me.id)
                        .order_by(M.Notice.created_at.desc()).limit(8)).all()
    unread = db().scalar(select(func.count()).select_from(M.Notice).where(
        M.Notice.member_id == g.me.id, M.Notice.read_at.is_(None)))
    return {"unread": unread, "items": [{"text": n.text, "link": n.link, "kind": n.kind, "new": n.read_at is None,
                                         "at": n.created_at.strftime("%d.%m %H:%M")} for n in rows]}


@app.post("/api/notices/read")
@need()
def notices_read():
    for n in db().scalars(select(M.Notice).where(M.Notice.member_id == g.me.id, M.Notice.read_at.is_(None))):
        n.read_at = datetime.now()
    db().commit()
    return {"ok": True}


@app.post("/api/prefs")
@need()
def prefs():
    """Настройки интерфейса человека: вибрация, вид задач."""
    member = db().get(M.Member, g.me.id)
    data = request.get_json(silent=True) or {}
    current = dict(member.prefs or {})
    for key in ("haptics", "board_view"):
        if key in data:
            current[key] = data[key]
    member.prefs = current
    db().commit()
    return {"ok": True}


# ─── Руководителю ───────────────────────────────────────────────────────────
@app.route("/admin", methods=["GET", "POST"])
@need("head")
def admin():
    """Обзор лаборатории: проекты, люди и роли, ИИ."""
    import ai

    if request.method == "POST":
        member = db().get(M.Member, request.form.get("member_id", ""))
        if member and member.id != g.me.id and request.form.getlist("roles"):
            was = member.role
            set_roles(member, request.form.getlist("roles"))
            member.company = request.form.get("company", member.company or "").strip()[:160]
            db().commit()
            if was == "pending" and member.role != "pending":
                notify(member.id, "Доступ к порталу лаборатории открыт",
                       "Ваши роли: " + ", ".join(M.ROLES[r] for r in member.all_roles()) + ".", "/home")
            flash(f"Роли «{member.name}» сохранены", "ok")
        return redirect(url_for("admin", tab="people"))
    tab = request.args.get("tab") or "overview"
    members = db().scalars(select(M.Member).order_by(M.Member.role != "pending", M.Member.name)).all()
    week_start = date.today() - timedelta(days=date.today().weekday())
    load = {}
    for m in members:
        load[m.id] = {
            "open": db().scalar(select(func.count()).select_from(M.Task).where(M.Task.assignee_id == m.id, M.Task.column != "done")),
            "hours": db().scalar(select(func.coalesce(func.sum(M.TimeEntry.hours), 0)).where(
                M.TimeEntry.member_id == m.id, M.TimeEntry.day >= week_start)),
            "days30": activity(m.id)["days30"],
            "ai": db().scalar(select(func.coalesce(func.sum(M.AiUsage.requests), 0)).where(
                M.AiUsage.member_id == m.id, M.AiUsage.day >= date.today() - timedelta(days=6)))}
    import control
    projects = db().scalars(select(M.Project).where(M.Project.status == "active")).all()
    health = []
    for p in projects:
        pct = project_percent(db().scalars(select(M.Part).where(M.Part.project_id == p.id)).all())
        health.append({"p": p, "percent": pct, "state": control.project_health(p, pct)})
    return render_template("admin.html", tab=tab, members=members, roles=M.ROLES, load=load, health=health,
                           dirs={d.id: d for d in db().scalars(select(M.Direction))}, ai_cfg=ai.settings(db()),
                           ai_ready=ai.configured(db()), pending=[m for m in members if m.role == "pending"])


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
    import ai
    me_ = getattr(g, "me", None)
    staff = bool(me_) and me_.role in STAFF
    nav_dirs = db().scalars(select(M.Direction).order_by(M.Direction.id)).all() if staff else []
    unread = db().scalar(select(func.count()).select_from(M.Notice).where(
        M.Notice.member_id == me_.id, M.Notice.read_at.is_(None))) if me_ else 0
    return {"me": me_, "roles": M.ROLES, "role_short": M.ROLE_SHORT, "statuses": M.STATUSES, "today": date.today(),
            "nav_dirs": nav_dirs, "issuer": ISSUER, "unread": unread, "now_hour": datetime.now().hour,
            "ai_on": bool(me_) and ai.allowed(db(), me_.role),
            "running": db().get(M.Timer, me_.id) if staff else None,
            "haptics": (me_.prefs or {}).get("haptics", True) if me_ else True}


# Модули портала: рабочий стол, ИИ-помощник, контроль выполнения
import assist  # noqa: E402,F401
import control  # noqa: E402,F401
import desk  # noqa: E402,F401
