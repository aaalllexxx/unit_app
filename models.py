"""
Данные портала лаборатории (app.labunit.ru). SQLite в томе lab-data.

Люди приходят из UNIT ID (sub — их id в UNIT); здесь — только лабораторное:
направление, группа, грейд, роль, навыки, активность.
Проект делится на части (модули, сервисы, исследования); каждая часть проходит
этапы «Планирование → Реализация → Тестирование → Релиз», у части есть процент
готовности текущего этапа. Из этого считается готовность части и всего проекта.
"""

import os
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATA_DIR = os.environ.get("LAB_DATA", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
ENGINE = create_engine("sqlite:///" + os.path.join(DATA_DIR, "lab.db"), future=True,
                       connect_args={"check_same_thread": False, "timeout": 15})
Session = sessionmaker(bind=ENGINE, future=True, expire_on_commit=False)

GRADES = ["Стажёр", "Junior", "Middle", "Senior", "Тимлид"]
ROLES = {"head": "Руководитель лаборатории", "lead": "Руководитель направления",
         "member": "Сотрудник", "customer": "Заказчик", "pending": "Ожидает подтверждения"}
ROLE_ORDER = ("head", "lead", "member", "customer", "pending")
ROLE_SHORT = {"head": "Руководитель", "lead": "Лид направления", "member": "Сотрудник", "customer": "Заказчик",
              "pending": "Ожидает"}
DESK_KINDS = {"note": "Заметка", "checklist": "Чек-лист", "link": "Ссылка", "file": "Файл", "code": "Код"}
PHASES = [("plan", "Планирование", 15), ("build", "Реализация", 50), ("test", "Тестирование", 25),
          ("release", "Релиз", 10)]                       # (ключ, название, доля в готовности части, %)
STATUSES = {"idea": "Идея", "active": "В работе", "paused": "Приостановлен", "done": "Завершён"}
TASK_COLUMNS = [("todo", "К выполнению"), ("doing", "В работе"), ("review", "На проверке"), ("done", "Готово")]


class Base(DeclarativeBase):
    pass


class Direction(Base):
    __tablename__ = "directions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    title: Mapped[str] = mapped_column(String(120))
    about: Mapped[str] = mapped_column(Text, default="")
    tone: Mapped[str] = mapped_column(String(20), default="blue")
    lead_id: Mapped[Optional[str]] = mapped_column(String(40), default=None)
    position: Mapped[int] = mapped_column(Integer, default=0)
    icon: Mapped[str] = mapped_column(String(24), default="")             # значок; пусто — по slug


class Team(Base):
    """Группа внутри направления."""
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction_id: Mapped[int] = mapped_column(ForeignKey("directions.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(120))


class Member(Base):
    __tablename__ = "members"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)          # sub из UNIT ID
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(20), default="pending")
    position: Mapped[str] = mapped_column(String(160), default="")       # должность словами
    grade: Mapped[str] = mapped_column(String(20), default="")
    direction_id: Mapped[Optional[int]] = mapped_column(ForeignKey("directions.id", ondelete="SET NULL"), default=None)
    direction_ids: Mapped[list] = mapped_column(JSON, default=list)       # [direction_id, ...]
    team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), default=None)
    team_ids: Mapped[list] = mapped_column(JSON, default=list)            # [team_id, ...]
    skills: Mapped[list] = mapped_column(JSON, default=list)              # [{"name", "level" 1..5}]
    about: Mapped[str] = mapped_column(Text, default="")
    contacts: Mapped[dict] = mapped_column(JSON, default=dict)            # telegram, phone
    company: Mapped[str] = mapped_column(String(160), default="")         # у заказчика
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True)
    joined: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    # Все выданные роли; role — основная. Человек с несколькими ролями переключается между ними
    roles: Mapped[list] = mapped_column(JSON, default=list)
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)               # вибрация, вид канбана…
    # Теги аккаунта UNIT (из UNIT ID и из «Доступов»); по ним считаются права — см. access.py
    tags: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    def all_roles(self) -> list:
        granted = [r for r in (self.roles or []) if r in ROLES and r != "pending"]
        if self.role not in granted:
            granted.insert(0, self.role)
        return [r for r in ROLE_ORDER if r in granted]

    def all_direction_ids(self) -> list:
        res = [int(x) for x in (self.direction_ids or []) if str(x).isdigit()]
        if self.direction_id and self.direction_id not in res:
            res.insert(0, self.direction_id)
        return res

    def all_team_ids(self) -> list:
        res = [int(x) for x in (self.team_ids or []) if str(x).isdigit()]
        if self.team_id and self.team_id not in res:
            res.insert(0, self.team_id)
        return res



class Visit(Base):
    """День, когда человек заходил (для «как часто бывает»)."""
    __tablename__ = "visits"
    __table_args__ = (UniqueConstraint("member_id", "day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    hits: Mapped[int] = mapped_column(Integer, default=1)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(String(400), default="")
    about: Mapped[str] = mapped_column(Text, default="")
    direction_id: Mapped[Optional[int]] = mapped_column(ForeignKey("directions.id", ondelete="SET NULL"), default=None)
    lead_id: Mapped[Optional[str]] = mapped_column(String(40), default=None)
    status: Mapped[str] = mapped_column(String(20), default="active")
    priority: Mapped[int] = mapped_column(Integer, default=2)            # 1 высокий … 3 низкий
    closed: Mapped[bool] = mapped_column(Boolean, default=False)         # видно только команде
    customer: Mapped[str] = mapped_column(String(200), default="")       # организация-заказчик
    starts: Mapped[Optional[date]] = mapped_column(Date, default=None)
    due: Mapped[Optional[date]] = mapped_column(Date, default=None)
    repo_url: Mapped[str] = mapped_column(String(300), default="")
    # Публичная витрина на лендинге: показывать ли и что именно (своими словами для внешних)
    public: Mapped[bool] = mapped_column(Boolean, default=False)
    public_title: Mapped[str] = mapped_column(String(200), default="")
    public_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "member_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(80), default="")            # «Бэкенд», «Аналитик»… ; у заказчика — «Заказчик»


class Part(Base):
    """Часть проекта: модуль, сервис, исследование. Своя готовность по этапам."""
    __tablename__ = "parts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[Optional[str]] = mapped_column(String(40), default=None)
    phase: Mapped[str] = mapped_column(String(20), default="plan")
    phase_progress: Mapped[int] = mapped_column(Integer, default=0)       # % готовности текущего этапа
    weight: Mapped[float] = mapped_column(Float, default=1.0)            # вес части в проекте
    note: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    part_id: Mapped[Optional[int]] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"), default=None)
    phase: Mapped[str] = mapped_column(String(20), default="build")
    title: Mapped[str] = mapped_column(String(300))
    about: Mapped[str] = mapped_column(Text, default="")
    column: Mapped[str] = mapped_column(String(20), default="todo")
    assignee_id: Mapped[Optional[str]] = mapped_column(String(40), default=None)
    estimate: Mapped[float] = mapped_column(Float, default=0)             # часы
    due: Mapped[Optional[date]] = mapped_column(Date, default=None)
    created_by: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    checklist: Mapped[list] = mapped_column(JSON, default=list)           # [{"text", "done"}]
    moved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)   # когда сменилась колонка
    priority: Mapped[int] = mapped_column(Integer, default=2)            # 1 срочно … 3 когда-нибудь


class TaskComment(Base):
    __tablename__ = "task_comments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class TimeEntry(Base):
    """Учёт времени: сколько часов, на какой проект и задачу."""
    __tablename__ = "time_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), default=None)
    day: Mapped[date] = mapped_column(Date, default=date.today)
    hours: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(String(300), default="")


class Update(Base):
    """Запись в журнале проекта; for_customer — видна заказчику."""
    __tablename__ = "updates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    for_customer: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ─── Рабочий стол, ИИ, контроль ─────────────────────────────────────────────
class Setting(Base):
    """Настройки портала: ИИ и прочее, что задаёт руководитель."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class DeskItem(Base):
    """Наработка на рабочем столе: заметка, чек-лист, ссылка, файл или код.
    Можно привязать к проекту и показать команде — тогда она видна в «Материалах»."""
    __tablename__ = "desk_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="note")
    title: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")                   # текст заметки или код
    data: Mapped[dict] = mapped_column(JSON, default=dict)                # пункты, url, файл, язык
    project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), default=None)
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    color: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class Notice(Base):
    """Уведомление в портале (колокольчик). key — чтобы напоминание не повторялось."""
    __tablename__ = "notices"
    __table_args__ = (UniqueConstraint("member_id", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default="info")         # info | task | deadline | blocker | ai
    text: Mapped[str] = mapped_column(String(400))
    link: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)


class Checkin(Base):
    """Итоги дня: что сделано, что дальше, что мешает. Одни в день."""
    __tablename__ = "checkins"
    __table_args__ = (UniqueConstraint("member_id", "day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date, default=date.today, index=True)
    done: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[str] = mapped_column(Text, default="")
    blockers: Mapped[str] = mapped_column(Text, default="")
    mood: Mapped[int] = mapped_column(Integer, default=3)                 # 1…5
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Timer(Base):
    """Идущий таймер: остановили — превратился в запись учёта времени."""
    __tablename__ = "timers"
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), default=None)
    note: Mapped[str] = mapped_column(String(300), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AiChat(Base):
    """Чат с ИИ-агентом (agent.py). items — лента: реплики, шаги-инструменты, план, помощники, цена.
    context — откуда начат разговор: «p<id>» — о проекте, «desk» — из мастерской."""
    __tablename__ = "ai_chats"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="Новый чат")
    items: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(80), default="")
    mode: Mapped[str] = mapped_column(String(10), default="manual")      # manual | auto
    context: Mapped[str] = mapped_column(String(40), default="")
    summary: Mapped[str] = mapped_column(Text, default="")               # сжатое начало длинного разговора
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AiMessage(Base):
    """Разговор с ИИ. thread: «me» — личный, «p<id>» — о проекте."""
    __tablename__ = "ai_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    thread: Mapped[str] = mapped_column(String(40), default="me", index=True)
    role: Mapped[str] = mapped_column(String(12))                         # user | assistant
    content: Mapped[str] = mapped_column(Text)
    actions: Mapped[list] = mapped_column(JSON, default=list)             # предложения ИИ и что применено
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AiUsage(Base):
    __tablename__ = "ai_usage"
    __table_args__ = (UniqueConstraint("member_id", "day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date, default=date.today)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)


class PushSub(Base):
    """Подписка устройства на push-уведомления (Web Push)."""
    __tablename__ = "push_subs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(1000), unique=True)
    p256dh: Mapped[str] = mapped_column(String(200), default="")
    auth: Mapped[str] = mapped_column(String(100), default="")
    agent: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_ok: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)


class TagRule(Base):
    """Правило тега: какие разделы и действия открыты, какие лимиты. is_view — тег-«кабинет»."""
    __tablename__ = "tag_rules"
    tag: Mapped[str] = mapped_column(String(60), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), default="")
    is_view: Mapped[bool] = mapped_column(Boolean, default=False)
    privileges: Mapped[list] = mapped_column(JSON, default=list)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    tone: Mapped[str] = mapped_column(String(20), default="blue")
    position: Mapped[int] = mapped_column(Integer, default=0)


class MemberAccess(Base):
    """Личные исключения: разрешить или запретить сверх тегов, свои лимиты."""
    __tablename__ = "member_access"
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), primary_key=True)
    allow: Mapped[list] = mapped_column(JSON, default=list)
    deny: Mapped[list] = mapped_column(JSON, default=list)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)


class AiSpend(Base):
    """Расход на ИИ: по человеку, дню и модели — в токенах и долларах (по ценам агрегатора)."""
    __tablename__ = "ai_spend"
    __table_args__ = (UniqueConstraint("member_id", "day", "model"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date, default=date.today, index=True)
    model: Mapped[str] = mapped_column(String(80), default="")
    requests: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)


SUPPORT_CATEGORIES = {
    "tech": "Техническая проблема",
    "access": "Доступ и аккаунт",
    "ai": "ИИ агент и инструменты",
    "feature": "Предложение / идея",
    "other": "Другой вопрос",
}

SUPPORT_STATUSES = {
    "open": "Новое",
    "in_progress": "В работе",
    "resolved": "Решено",
    "closed": "Закрыто",
}

SUPPORT_PRIORITIES = {
    "normal": "Обычный",
    "high": "Высокий",
    "urgent": "Срочный",
}


class SupportTicket(Base):
    """Обращение в техподдержку лаборатории."""
    __tablename__ = "support_tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(40), default="tech")
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class SupportMessage(Base):
    """Сообщение в ветке обращения техподдержки."""
    __tablename__ = "support_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)



def _migrate() -> None:
    """Новые столбцы в таблицах прошлых версий (create_all их не добавляет)."""
    added = {
        "members": [
            ("roles", "JSON DEFAULT '[]'"),
            ("prefs", "JSON DEFAULT '{}'"),
            ("tags", "JSON"),
            ("direction_ids", "JSON DEFAULT '[]'"),
            ("team_ids", "JSON DEFAULT '[]'"),
        ],
        "directions": [("position", "INTEGER DEFAULT 0"), ("icon", "VARCHAR(24) DEFAULT ''")],
        "tasks": [("checklist", "JSON DEFAULT '[]'"), ("moved_at", "DATETIME"), ("priority", "INTEGER DEFAULT 2")],
        "ai_chats": [("items", "JSON DEFAULT '[]'"), ("model", "VARCHAR(80) DEFAULT ''"),
                     ("mode", "VARCHAR(10) DEFAULT 'manual'"), ("context", "VARCHAR(40) DEFAULT ''"),
                     ("summary", "TEXT DEFAULT ''"), ("cost", "FLOAT DEFAULT 0"), ("tokens", "INTEGER DEFAULT 0")],
    }
    with ENGINE.begin() as con:
        for table, columns in added.items():
            have = {row[1] for row in con.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in have:
                    con.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init() -> None:
    Base.metadata.create_all(ENGINE)
    _migrate()
    with Session() as s:
        if not s.query(Direction).count():
            s.add_all([
                Direction(slug="security", title="Информационная безопасность", tone="crimson",
                          about="Пентесты, анализ защищённости, расследование инцидентов и аудит."),
                Direction(slug="dev", title="Разработка ПО", tone="blue",
                          about="Веб и мобильные приложения, сервисы, внутренние инструменты."),
                Direction(slug="ml", title="Нейросети", tone="violet",
                          about="Модели, датасеты, исследования и внедрение ИИ в продукты."),
            ])
            s.commit()
        import access
        access.ensure_defaults(s)
