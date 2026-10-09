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
    team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), default=None)
    skills: Mapped[list] = mapped_column(JSON, default=list)              # [{"name", "level" 1..5}]
    about: Mapped[str] = mapped_column(Text, default="")
    contacts: Mapped[dict] = mapped_column(JSON, default=dict)            # telegram, phone
    company: Mapped[str] = mapped_column(String(160), default="")         # у заказчика
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True)
    joined: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)


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


def init() -> None:
    Base.metadata.create_all(ENGINE)
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
