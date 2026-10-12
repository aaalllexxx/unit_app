"""
ИИ-агент лаборатории — чат, как у современных ассистентов (как агент наставника в школе UNIT).

Человек пишет задачу словами, агент работает шагами: смотрит проекты, задачи,
людей и мастерскую, раздаёт поиск и черновики своей команде дешёвых моделей,
сверяет их ответы с данными портала и только потом отвечает или действует.
Каждый шаг виден в чате тихой строкой; то, что меняет данные, в ручном режиме
ждёт «Разрешить». Агент действует с правами человека: видит и меняет только то,
что разрешено ему самому, а администрирует — только с правом ai.agent_admin.

Протокол с моделью — JSON на каждом шаге (работает с любой моделью по протоколу OpenAI):
    {"say": "текст человеку", "todo": [...], "calls": [{"tool": ..., "args": {...}}]}
Пустой "calls" — работа закончена, "say" — итог.

Работа идёт в фоне (поток на сообщение), лента хранится в ai_chats.items, страница
опрашивает её, пока агент работает. Расход — в ai_spend, лимиты — в «Доступах»:
исчерпан лимит — агент продолжает на дешёвой модели команды.
Инструменты — agent_tools.py.
"""

import copy
import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Dict, List, Optional

from flask import abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, select

import access
import ai
import models as M
from app import app, db, need, with_rights

MAX_STEPS = 40            # шагов у главного агента на одно сообщение
SUB_STEPS = 12            # шагов у помощника
RUN_MINUTES = 30          # одна работа — не дольше
MAX_CALLS = 12            # инструментов за шаг
MAX_TEAM = 6              # помощников за один run_team
WAIT_DECISION = 30 * 60   # столько ждём «Разрешить»
RESULT_CHARS = 9000       # столько знаков результата инструмента видит модель
HISTORY_CHARS = 60000     # прошлый разговор в контексте; длиннее — сжатие или обрезка
COMPACT_AT = 40000        # с этой длины разговор сжимается (если человек не отключил)
RUN_CHARS = 140000        # переписка внутри одной работы; длиннее — старые результаты укорачиваются
MAX_ITEMS = 3000          # предохранитель: столько записей держит один чат
WINDOW = 300              # столько последних записей страница получает за раз
STEP_TOKENS = 6000
MEMORY_MAX = 60
FILE_EXT = (".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".sql",
            ".yaml", ".yml", ".xml", ".log", ".ini", ".toml", ".sh", ".go", ".rs", ".java", ".kt", ".c", ".cpp", ".h",
            ".cs", ".php", ".rb", ".swift", ".dart", ".env.example", ".cfg", ".conf", ".tex")
FILE_BYTES = 400 * 1024
FILE_CHARS = 60000
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


class ToolError(Exception):
    """Ошибка инструмента — её видит модель и человек."""


class Stopped(Exception):
    pass


def _id() -> str:
    return secrets.token_hex(5)


def _now() -> str:
    return datetime.now().strftime("%d.%m %H:%M")


# ═══ Настройки человека и память ════════════════════════════════════════════
PREF_DEFAULTS = {"reasoning": "", "compact": True, "model": "", "mode": "manual"}


def prefs_of(member) -> dict:
    raw = (getattr(member, "prefs", None) or {}).get("ai") or {}
    out = dict(PREF_DEFAULTS)
    out.update({k: raw[k] for k in PREF_DEFAULTS if k in raw})
    if out["reasoning"] not in ("", *ai.REASONING):
        out["reasoning"] = ""
    return out


def save_prefs(member_id: str, **fields) -> dict:
    member = db().get(M.Member, member_id)
    prefs = dict(member.prefs or {})
    current = dict(prefs.get("ai") or {})
    current.update({k: v for k, v in fields.items() if k in PREF_DEFAULTS})
    prefs["ai"] = current
    member.prefs = prefs
    db().commit()
    return current


def memory_of(member_id: str) -> List[dict]:
    member = db().get(M.Member, member_id)
    return list(((member.prefs or {}) if member else {}).get("ai_memory") or [])


def remember(member_id: str, text: str, replace: str = "") -> dict:
    text = " ".join(str(text or "").split())[:500]
    if not text:
        raise ToolError("Пустая запись памяти")
    member = db().get(M.Member, member_id)
    prefs = dict(member.prefs or {})
    items = [m for m in (prefs.get("ai_memory") or []) if m.get("id") != replace]
    entry = {"id": _id(), "text": text, "at": date.today().isoformat()}
    items.append(entry)
    prefs["ai_memory"] = items[-MEMORY_MAX:]
    member.prefs = prefs
    db().commit()
    return entry


def forget(member_id: str, memory_id: str) -> None:
    member = db().get(M.Member, member_id)
    prefs = dict(member.prefs or {})
    prefs["ai_memory"] = [m for m in (prefs.get("ai_memory") or []) if m.get("id") != memory_id]
    member.prefs = prefs
    db().commit()


# ═══ Чаты ═══════════════════════════════════════════════════════════════════
def _chat(chat_id: str, member_id: str):
    chat = db().get(M.AiChat, chat_id)
    if not chat or chat.member_id != member_id:
        abort(404)
    return chat


def chats_of(member_id: str, limit: int = 80) -> List[dict]:
    rows = db().scalars(select(M.AiChat).where(M.AiChat.member_id == member_id)
                        .order_by(M.AiChat.updated_at.desc()).limit(limit)).all()
    return [{"id": c.id, "title": c.title, "live": c.id in _RUNS and _RUNS[c.id].alive()} for c in rows]


def new_chat(member_id: str, model: str = "", mode: str = "manual", context: str = "") -> "M.AiChat":
    chat = M.AiChat(id="c_" + secrets.token_hex(6), member_id=member_id, title="Новый чат", items=[],
                    model=model[:80], mode="auto" if mode == "auto" else "manual", context=context[:40])
    db().add(chat)
    db().commit()
    return chat


def _save_items(chat_id: str, items: list, **fields) -> None:
    """Лента пишется своей сессией: работа агента идёт в фоне, рядом с запросами страницы."""
    with M.Session() as s:
        chat = s.get(M.AiChat, chat_id)
        if not chat:
            return
        chat.items = copy.deepcopy(items[-MAX_ITEMS:])
        for key, value in fields.items():
            setattr(chat, key, value)
        chat.updated_at = datetime.now()
        s.commit()


def migrate_old_messages() -> None:
    """Разговоры прошлой версии (ai_messages) — в ленты чатов. Один раз."""
    with M.Session() as s:
        if s.get(M.Setting, "ai_items_migrated"):
            return
        groups: Dict[tuple, list] = {}
        for m in s.scalars(select(M.AiMessage).order_by(M.AiMessage.id)):
            groups.setdefault((m.member_id, m.thread), []).append(m)
        for (member_id, thread), rows in groups.items():
            chat = s.get(M.AiChat, thread) if thread.startswith("c_") else None
            if chat is None:
                title = "Личный разговор"
                if thread.startswith("p") and thread[1:].isdigit():
                    project = s.get(M.Project, int(thread[1:]))
                    title = f"Проект «{project.title}»" if project else "О проекте"
                chat = M.AiChat(id="c_" + secrets.token_hex(6), member_id=member_id, title=title[:200],
                                context=thread if thread.startswith("p") else "")
                s.add(chat)
            items = list(chat.items or [])
            for m in rows:
                items.append({"id": _id(), "type": "user" if m.role == "user" else "say", "text": m.content or "",
                              "at": m.created_at.strftime("%d.%m %H:%M") if m.created_at else ""})
            chat.items = items
            chat.updated_at = rows[-1].created_at or datetime.now()
        s.execute(M.AiMessage.__table__.delete())
        s.add(M.Setting(key="ai_items_migrated", value="1"))
        s.commit()


# ═══ Работа агента ══════════════════════════════════════════════════════════
_RUNS: Dict[str, "Run"] = {}
_RUNS_LOCK = threading.Lock()


class Run:
    """Одна работа агента над сообщением: лента, живые «дорожки», решения человека."""

    def __init__(self, chat, member_id: str, view: str, model: str, reasoning: str, compact: bool):
        self.chat_id = chat.id
        self.member_id = member_id
        self.view = view
        self.model = model
        self.mode = chat.mode
        self.context = chat.context or ""
        self.reasoning = reasoning
        self.compact = compact
        self.summary = chat.summary or ""
        self.base_cost = float(chat.cost or 0)
        self.provisional = False
        self.items: list = list(copy.deepcopy(chat.items or []))
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.waits: Dict[str, threading.Event] = {}
        self.answers: Dict[str, bool] = {}
        self.queue: List[dict] = []
        self.lanes: Dict[str, dict] = {}
        self.state, self.text = "thinking", "Думаю…"
        self.cost, self.tokens, self.calls = 0.0, 0, 0
        self.started = time.time()
        self.version = 1
        self.saved_at = 0.0
        self.touched: set = set()
        self.thread: Optional[threading.Thread] = None
        self.notified_cheap = False

    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def touch(self, force: bool = False) -> None:
        with self.lock:
            self.version += 1
            if force or time.time() - self.saved_at > 1.0:
                self.saved_at = time.time()
                _save_items(self.chat_id, self.items, cost=self._chat_cost())
        return None

    def _chat_cost(self) -> float:
        return round(self.base_cost + self.cost, 6)

    def add(self, item: dict) -> dict:
        item.setdefault("id", _id())
        item.setdefault("at", _now())
        with self.lock:
            self.items.append(item)
        self.touch()
        return item

    def status(self, state: str, text: str) -> None:
        with self.lock:
            self.state, self.text = state, text
            self.version += 1

    def public(self) -> dict:
        with self.lock:
            return {"state": self.state if self.alive() else "idle", "text": self.text,
                    "lanes": copy.deepcopy(self.lanes), "started": self.started, "now": time.time(),
                    "cost_label": ai.money(self.cost) if self.cost else ""}


def live_run(chat_id: str) -> Optional[Run]:
    run = _RUNS.get(chat_id)
    return run if run and run.alive() else None


def start(chat, member, text: str, files: list) -> dict:
    """Новое сообщение: агент свободен — начинает работу; занят — сообщение встаёт в очередь."""
    user = {"id": _id(), "type": "user", "text": text, "at": _now(),
            "files": [{"name": f["name"], "chars": len(f["text"])} for f in files],
            "attach": [{"name": f["name"], "text": f["text"]} for f in files]}
    running = live_run(chat.id)
    if running:
        user["queued"] = True
        with running.lock:
            running.queue.append(user)
        running.add(user)
        return {"queued": True}
    prefs = prefs_of(member)
    model = chat.model or (prefs["model"] if access.can(member, "ai.pick_model") else "") or ai.main_model(db())
    if not access.can(member, "ai.pick_model"):
        model = ai.main_model(db())
    reasoning = prefs["reasoning"] or ai.settings(db())["reasoning"]
    if chat.mode == "auto" and not access.can(member, "ai.auto"):
        chat.mode = "manual"
    provisional = chat.title == "Новый чат" and not any(i.get("type") == "user" for i in (chat.items or []))
    if provisional:
        first = " ".join(text.split())[:60] or (files[0]["name"] if files else "Новый чат")
        chat.title = first[:57] + ("…" if len(first) > 57 else "")
    db().commit()
    run = Run(chat, member.id, getattr(member, "view", "") or "", model, reasoning, prefs["compact"])
    run.provisional = provisional
    run.add(user)
    with _RUNS_LOCK:
        _RUNS[chat.id] = run
    run.thread = threading.Thread(target=_work, args=(run, user), daemon=True, name=f"agent-{chat.id}")
    run.thread.start()
    return {"queued": False}


def decide(chat_id: str, member_id: str, tool_id: str, ok: bool) -> int:
    run = live_run(chat_id)
    if not run or run.member_id != member_id:
        return 0
    with run.lock:
        ids = list(run.waits) if tool_id == "all" else [tool_id] if tool_id in run.waits else []
        for tid in ids:
            run.answers[tid] = ok
            run.waits[tid].set()
    return len(ids)


def stop(chat_id: str, member_id: str) -> bool:
    run = live_run(chat_id)
    if not run or run.member_id != member_id:
        return False
    run.stop.set()
    run.status("stopping", "Останавливаюсь…")
    with run.lock:
        for event in run.waits.values():
            event.set()
    return True


# ─── Фоновая работа ─────────────────────────────────────────────────────────
def _work(run: Run, user: dict) -> None:
    with app.app_context():
        outcome = "done"
        try:
            g.me = _person(run)
            _loop(run, user)
        except Stopped:
            outcome = "stopped"
            run.add({"type": "notice", "text": "Остановлено"})
        except ai.AiError as error:
            outcome = "error"
            run.add({"type": "error", "text": str(error)})
        except Exception as error:                         # noqa: BLE001 — работа не должна ронять портал
            outcome = "error"
            run.add({"type": "error", "text": f"Сбой агента: {error}"[:400]})
            app.logger.exception("agent run failed")
        finally:
            _finish(run, outcome)


def _person(run: Run):
    member = db().get(M.Member, run.member_id)
    if not member:
        raise Stopped()
    return with_rights(member, run.view)


def _finish(run: Run, outcome: str) -> None:
    with run.lock:
        for item in run.items:
            if item.get("type") == "tool" and item.get("status") in ("running", "awaiting", "queued"):
                item["status"] = "stopped"
            for agent in item.get("agents") or []:
                if agent.get("status") in ("running", "queued"):
                    agent["status"] = "stopped"
            if item.get("queued"):
                item.pop("queued", None)
        if run.calls:
            run.items.append({"id": _id(), "type": "usage", "at": _now(), "label": ai.money(run.cost),
                              "tokens": run.tokens, "calls": run.calls, "model": ai.pretty(run.model),
                              "touched": sorted(run.touched)})
        run.lanes = {}
    try:
        if getattr(run, "provisional", False) and outcome == "done":
            _name_chat(run)
        if run.compact:
            _compact(run)
    except Exception:                                       # noqa: BLE001 — название и сжатие не обязательны
        app.logger.exception("agent: title/compact failed")
    _save_items(run.chat_id, run.items, cost=run._chat_cost(), tokens=_chat_tokens(run), summary=run.summary)
    run.status("idle", "")


def _chat_tokens(run: Run) -> int:
    with M.Session() as s:
        chat = s.get(M.AiChat, run.chat_id)
        return int((chat.tokens if chat else 0) or 0) + run.tokens


def _charge(run: Run, result: dict) -> None:
    ai.charge(db(), run.member_id, result["model"], result["cost"], result["tokens"])
    with run.lock:
        run.cost += result["cost"]
        run.tokens += result["tokens"]
        run.calls += 1


def _name_chat(run: Run) -> None:
    first = next((i.get("text", "") for i in run.items if i.get("type") == "user"), "")
    if not first.strip():
        return
    model = ai.cheapest(db())
    result = ai.call(db(), [{"role": "system", "content": "Придумай короткое название для чата по первому сообщению: "
                                                        "2–4 слова по-русски, без кавычек и точки. Ответь только названием."},
                            {"role": "user", "content": first[:1500]}], model, reasoning="none", max_tokens=600)
    _charge(run, result)
    title = re.sub(r"[\"«»'`*#]", "", result["text"].strip().splitlines()[0] if result["text"].strip() else "")[:80]
    if title:
        with M.Session() as s:
            chat = s.get(M.AiChat, run.chat_id)
            if chat:
                chat.title = title
                s.commit()


def _compact(run: Run) -> None:
    """Длинный разговор: начало сжимается в краткое содержание, дальше модель видит его и свежие сообщения."""
    lines, size = [], 0
    for item in run.items:
        size += len(item.get("text") or "") + len(item.get("summary") or "")
    if size < COMPACT_AT:
        return
    keep_from = len(run.items)
    tail = 0
    while keep_from > 0 and tail < COMPACT_AT // 2:
        keep_from -= 1
        tail += len(run.items[keep_from].get("text") or "") + len(run.items[keep_from].get("summary") or "")
    old = run.items[:keep_from]
    if not old:
        return
    prev = _summary_text(run)
    for item in old:
        line = _item_line(item)
        if line:
            lines.append(line)
    text = "\n".join(lines)[-60000:]
    model = ai.cheapest(db())
    result = ai.call(db(), [{"role": "system", "content": "Сожми разговор человека с ИИ-агентом лаборатории в 8–15 пунктов: "
                                                        "цели, решения, что сделано (с id задач и проектов), что осталось. "
                                                        "Только факты, по-русски."},
                            {"role": "user", "content": (("Прежнее краткое содержание:\n" + prev + "\n\n") if prev else "")
                             + "Разговор:\n" + text}], model, reasoning="none", max_tokens=1500)
    _charge(run, result)
    if result["text"]:
        run.summary = json.dumps({"upto": old[-1]["id"], "text": result["text"][:6000]}, ensure_ascii=False)


def _summary_text(run: Run) -> str:
    try:
        return json.loads(run.summary or "{}").get("text", "")
    except ValueError:
        return ""


def _summary_upto(run: Run) -> str:
    try:
        return json.loads(run.summary or "{}").get("upto", "")
    except ValueError:
        return ""


# ─── Подсказка модели ───────────────────────────────────────────────────────
def _rights_lines(person) -> List[str]:
    names = [access.NAMES[p] for p in access.ALL if p in (person.perms or set())]
    return names


def _system(run: Run, person, helper_goal: str = "") -> str:
    import agent_tools as T
    s = db()
    cfg = ai.settings(s)
    today = date.today()
    view = access.view_title(s, person.view) if getattr(person, "view", "") else "без кабинета"
    lines = [
        ("Ты — помощник главного ИИ-агента IT-лаборатории UNIT. Выполни одно поручение и верни итог."
         if helper_goal else
         f"Ты — ИИ-агент IT-лаборатории UNIT ({ai.pretty(run.model)}). Помогаешь человеку доводить проекты до "
         "результата: планировать и декомпозировать, находить риски, вести задачи, журналы и мастерскую, готовить "
         "отчёты и тексты, разбирать код и архитектуру."),
        f"Сегодня {today:%d.%m.%Y}, {WEEKDAYS[today.weekday()]}.",
        f"Человек: {person.name} (id {person.id}), кабинет «{view}»" + (f", {person.position}" if person.position else "") + ".",
        "Ты действуешь от его имени и с его правами: чего нельзя ему, нельзя и тебе — инструменты сами проверяют права.",
        "Права человека: " + "; ".join(_rights_lines(person)) + ".",
    ]
    if helper_goal:
        lines += ["", "ПОРУЧЕНИЕ", helper_goal, "",
                  "Твои инструменты только читают данные портала. Не выдумывай: id, сроки, людей и цифры бери из "
                  "результатов инструментов. В итоговом say — факты с id и ссылками, коротко и по делу; "
                  "если чего-то не нашёл — так и скажи."]
    else:
        if access.can(person, "ai.agent_admin") and access.can(person, "admin.access"):
            lines.append("Человек разрешил тебе администрировать: менять теги, права и лимиты людей.")
        lines.append("Режим: " + ("авто — меняющие инструменты выполняются сразу." if run.mode == "auto" else
                                  "ручной — меняющие инструменты ждут «Разрешить» человека; читающие — сразу."))
        if run.context.startswith("p") and run.context[1:].isdigit():
            project = s.get(M.Project, int(run.context[1:]))
            if project:
                lines.append(f"Разговор начат со страницы проекта {project.id} «{project.title}».")
        elif run.context == "desk":
            lines.append("Разговор идёт из мастерской: человек видит стол с окнами инструментов.")
        memory = memory_of(person.id)
        if memory:
            lines += ["", "ПАМЯТЬ О ЧЕЛОВЕКЕ (из прошлых чатов)"] + [f"- {m['text']}" for m in memory]
        if cfg["prompt"]:
            lines += ["", "УКАЗАНИЯ РУКОВОДИТЕЛЯ ЛАБОРАТОРИИ", cfg["prompt"]]
        team = ai.team(s)
        lines += ["", "КОМАНДА",
                  "Ты — главная модель и руководитель команды. Поиск по многим проектам и людям, сбор фактов и "
                  "черновики (описания задач, отчёты, планы) поручай помощникам инструментом run_team: они дешевле "
                  "и работают параллельно, но только читают. Их ответы ПРОВЕРЯЙ: сверяй ключевые факты (id, сроки, "
                  "исполнителей, цифры) с данными портала своими инструментами, прежде чем опираться на них. "
                  "Простое (1–3 обращения к данным) делай сам."]
        if team:
            lines += ["Модели команды (от дешёвых к дорогим):"] + [
                f"- {r['id']}" + (f" — ≈{r['price']} за обращение" if r["price"] else "") for r in team]
        else:
            lines.append("Отдельных моделей у команды нет — помощники работают на твоей модели.")
    lines += ["", "ИНСТРУМЕНТЫ", T.doc(person, helper=bool(helper_goal)), "",
              "ПРАВИЛА",
              "- Отвечай по-русски, по делу. Разметка в say: **жирный**, списки, `код`, ```блоки```, заголовки ###, таблицы |.",
              "- Не выдумывай id, задачи, людей и сроки — сначала посмотри инструментами.",
              f"- За шаг можно вызвать до {MAX_CALLS} инструментов; читающие — смело и сразу пачкой.",
              "- Перед изменениями коротко скажи в say, что собираешься сделать.",
              "- Инструмент вернул ошибку — исправь вызов или объясни человеку, что мешает.",
              "- Ссылки в итоге — markdown: [Задача](/p/ID?task=TID#tasks), [Проект](/p/ID).",
              "", "ФОРМАТ ОТВЕТА — строго один JSON-объект, без текста вокруг:",
              '{"say": "текст человеку", "todo": ["шаг плана"], "calls": [{"tool": "имя", "args": {}}]}',
              "- todo — необязательно, для работы из 3+ шагов; сделанные пункты начинай с «✓ ».",
              "- calls пустой — работа закончена, say — итоговый ответ."]
    return "\n".join(lines)


def _item_line(item: dict) -> str:
    kind = item.get("type")
    if kind == "user":
        return "Человек: " + (item.get("text") or "")
    if kind == "say":
        return "Агент: " + (item.get("text") or "")
    if kind == "tool":
        return f"[{item.get('name')} → {item.get('summary') or item.get('status')}]"
    if kind == "agents":
        return "[помощники: " + "; ".join(f"{a.get('title')}: {str(a.get('result') or '')[:300]}"
                                          for a in item.get("agents") or []) + "]"
    return ""


def _history(run: Run, upto: dict) -> List[dict]:
    """Прошлый разговор до текущего сообщения: реплики и короткие итоги шагов."""
    items = run.items[: run.items.index(upto)] if upto in run.items else run.items
    cut = _summary_upto(run)
    if cut:
        ids = [i.get("id") for i in items]
        if cut in ids:
            items = items[ids.index(cut) + 1:]
    turns: List[dict] = []
    steps: List[str] = []
    for item in items:
        kind = item.get("type")
        if kind == "user":
            if steps and turns:
                turns.append({"role": "assistant", "content": json.dumps({"say": "", "done": steps}, ensure_ascii=False)})
            steps = []
            turns.append({"role": "user", "content": _user_text(item)})
        elif kind == "say":
            turns.append({"role": "assistant", "content": json.dumps(
                {"say": item.get("text") or "", "done": steps} if steps else {"say": item.get("text") or ""},
                ensure_ascii=False)})
            steps = []
        elif kind in ("tool", "agents"):
            line = _item_line(item)
            if line:
                steps.append(line[:400])
    # длинная история — свежие сообщения целиком, остальное отрезается (или уже сжато)
    total, kept = 0, []
    for message in reversed(turns):
        total += len(message["content"])
        if total > HISTORY_CHARS:
            break
        kept.append(message)
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    out = []
    summary = _summary_text(run)
    if summary:
        out.append({"role": "user", "content": "(Служебно: краткое содержание начала разговора)\n" + summary})
        out.append({"role": "assistant", "content": json.dumps({"say": "Понял, продолжаю с учётом этого."},
                                                               ensure_ascii=False)})
    return out + kept


def _user_text(item: dict) -> str:
    text = item.get("text") or ""
    for f in item.get("attach") or []:
        text += f"\n\n--- Файл «{f.get('name')}» ---\n{(f.get('text') or '')[:FILE_CHARS]}"
    return text or "(пустое сообщение)"


# ─── Разбор ответа модели ───────────────────────────────────────────────────
def _parse(raw: str) -> Optional[dict]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    body = text[start:end + 1]
    data = None
    for strict in (True, False):
        try:
            data = json.loads(body, strict=strict)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        return None
    calls = []
    for c in data.get("calls") or []:
        if isinstance(c, dict) and isinstance(c.get("tool"), str):
            args = c.get("args") if isinstance(c.get("args"), dict) else {}
            calls.append({"tool": c["tool"].strip(), "args": args})
    todo = []
    for t in data.get("todo") or []:
        if isinstance(t, str) and t.strip():
            todo.append({"text": t.strip().lstrip("✓").strip(), "done": t.strip().startswith("✓")})
        elif isinstance(t, dict) and t.get("text"):
            todo.append({"text": str(t["text"]), "done": bool(t.get("done"))})
    return {"say": str(data.get("say") or "").strip(), "calls": calls, "todo": todo}


_SAY = re.compile(r'"say"\s*:\s*"((?:[^"\\]|\\.)*)', re.S)


def _partial_say(text: str) -> str:
    """Что модель уже написала человеку, пока ответ идёт потоком."""
    m = _SAY.search(text or "")
    if not m:
        return ""
    raw = m.group(1)
    try:
        return json.loads('"' + raw.rstrip("\\") + '"')
    except ValueError:
        return raw.replace("\\n", "\n").replace('\\"', '"')


NUDGE = ("СИСТЕМА: ответ не разобрался как JSON. Повтори его строго одним JSON-объектом "
         '{"say": "...", "todo": [...], "calls": [...]}, без текста вокруг.')


# ─── Цикл ───────────────────────────────────────────────────────────────────
class Lane:
    """Кто работает: главный агент или помощник. Свой список шагов и своя модель."""

    def __init__(self, run: Run, key: str, model: str, agent: Optional[dict] = None, goal: str = ""):
        self.run, self.key, self.model, self.agent, self.goal = run, key, model, agent, goal

    @property
    def helper(self) -> bool:
        return self.agent is not None

    def add(self, item: dict) -> dict:
        item.setdefault("id", _id())
        item.setdefault("at", _now())
        if self.agent is None:
            return self.run.add(item)
        with self.run.lock:
            self.agent.setdefault("steps", []).append(item)
        self.run.touch()
        return item

    def live(self, **fields) -> None:
        with self.run.lock:
            lane = self.run.lanes.setdefault(self.key, {"since": time.time()})
            lane.update(fields)
            self.run.version += 1


def _ask(lane: Lane, messages: List[dict]) -> dict:
    """Обращение к модели с проверкой лимита: исчерпан — дальше дешёвая модель команды."""
    run = lane.run
    if run.stop.is_set():
        raise Stopped()
    s = db()
    budget = ai.budget(s, g.me)
    if budget["over"] and not ai.is_cheap(s, lane.model):
        cheap = ai.cheapest(s)
        if cheap and ai.is_cheap(s, cheap):
            lane.model = cheap
            if not run.notified_cheap:
                run.notified_cheap = True
                run.add({"type": "notice", "text": f"Лимит на ИИ исчерпан — дальше работает дешёвая модель "
                                                   f"{ai.pretty(cheap)}: такие лимитом не ограничены"})
        else:
            raise ai.AiError("Лимит на ИИ исчерпан, а дешёвой модели в команде нет — лимит меняют в «Доступах»")
    lane.live(since=time.time(), chars=0, say="", thinking="", model=ai.pretty(lane.model))

    def watch(event):
        lane.live(chars=len(event.get("text") or ""), say=_partial_say(event.get("text") or "")[-1500:],
                  thinking=(event.get("reasoning") or "")[-900:])

    result = ai.call(s, messages, lane.model, reasoning="none" if lane.helper else run.reasoning,
                     max_tokens=STEP_TOKENS, on_delta=watch, stop=run.stop)
    _charge(run, result)
    if result.get("stopped"):
        raise Stopped()
    return result


def _trim(messages: List[dict]) -> None:
    """Работа затянулась: ранние результаты инструментов укорачиваются, начало и свежее — целиком."""
    total = sum(len(m["content"]) for m in messages)
    for m in messages[2:-6]:
        if total <= RUN_CHARS:
            break
        if m["role"] == "user" and m["content"].startswith("РЕЗУЛЬТАТЫ") and len(m["content"]) > 800:
            total -= len(m["content"]) - 800
            m["content"] = m["content"][:800] + "\n…(укорочено: работа длинная; нужно — вызови инструмент снова)"


def _loop(run: Run, user: dict) -> None:
    lane = Lane(run, "main", run.model)
    messages = [{"role": "system", "content": _system(run, g.me)}] + _history(run, user) + \
               [{"role": "user", "content": _user_text(user)}]
    todo_item = None
    deadline = time.time() + RUN_MINUTES * 60
    for step in range(1, MAX_STEPS + 1):
        if time.time() > deadline:
            run.add({"type": "notice", "text": f"Работа идёт дольше {RUN_MINUTES} минут — остановился. "
                                               "Напишите «продолжай», если нужно дальше."})
            return
        with run.lock:
            queued, run.queue = run.queue, []
        for extra in queued:
            messages.append({"role": "user", "content": "(Человек дописал по ходу работы)\n" + _user_text(extra)})
        run.status("thinking", "Думаю…")
        lane.live(step=step)
        _trim(messages)
        result = _ask(lane, messages)
        parsed = _parse(result["text"])
        if parsed is None:
            messages += [{"role": "assistant", "content": result["text"][:4000]}, {"role": "user", "content": NUDGE}]
            result = _ask(lane, messages)
            parsed = _parse(result["text"])
            if parsed is None:                                 # модель так и не ответила JSON — это и есть ответ
                text = result["text"].strip()
                if text:
                    run.add({"type": "say", "text": text, "model": ai.pretty(lane.model)})
                return
            messages = messages[:-2]
        messages.append({"role": "assistant", "content": json.dumps(
            {"say": parsed["say"], "todo": [("✓ " if t["done"] else "") + t["text"] for t in parsed["todo"]],
             "calls": parsed["calls"]}, ensure_ascii=False)})
        if parsed["todo"]:
            if todo_item is None:
                todo_item = run.add({"type": "todo", "items": parsed["todo"]})
            else:
                with run.lock:
                    todo_item["items"] = parsed["todo"]
                run.touch()
        if parsed["say"]:
            run.add({"type": "say", "text": parsed["say"], "model": ai.pretty(lane.model),
                     "interim": bool(parsed["calls"])})
        if not parsed["calls"]:
            return
        results = _run_calls(lane, parsed["calls"])
        messages.append({"role": "user", "content": "РЕЗУЛЬТАТЫ ИНСТРУМЕНТОВ\n" + "\n\n".join(results)})
    run.add({"type": "notice", "text": f"Сделано {MAX_STEPS} шагов — остановился. Напишите «продолжай», если нужно дальше."})


def _run_calls(lane: Lane, calls: List[dict]) -> List[str]:
    import agent_tools as T
    run = lane.run
    out = []
    if len(calls) > MAX_CALLS:
        out.append(f"(Вызовов больше {MAX_CALLS} — выполнены первые {MAX_CALLS}, остальные повтори следующим шагом)")
        calls = calls[:MAX_CALLS]
    run.status("tools", "Работаю")
    for call in calls:
        if run.stop.is_set():
            raise Stopped()
        out.append(T.execute(lane, call["tool"], call["args"]))
    return out


def wait_decision(lane: Lane, item: dict) -> bool:
    """Ручной режим: шаг ждёт «Разрешить» человека."""
    run = lane.run
    event = threading.Event()
    with run.lock:
        run.waits[item["id"]] = event
    run.status("awaiting", "Жду вашего решения")
    run.touch(force=True)
    got = event.wait(WAIT_DECISION)
    with run.lock:
        run.waits.pop(item["id"], None)
        ok = run.answers.pop(item["id"], False)
    if run.stop.is_set():
        raise Stopped()
    run.status("tools", "Работаю")
    return bool(got and ok)


def run_team(lane: Lane, tasks: List[dict]) -> str:
    """Помощники: каждый — свой цикл на дешёвой модели с читающими инструментами, параллельно."""
    run = lane.run
    s = db()
    team = [r["id"] for r in ai.team(s)]
    tasks = [t for t in tasks if isinstance(t, dict) and str(t.get("goal") or "").strip()][:MAX_TEAM]
    if not tasks:
        raise ToolError("Нужен список tasks: [{title, goal, model?}]")
    agents = []
    for i, t in enumerate(tasks, 1):
        model = str(t.get("model") or "").strip()
        if model not in team and model not in ai.chat_models(s):
            model = ai.cheapest(s) if team else run.model
        agents.append({"key": f"A{i}", "title": str(t.get("title") or t["goal"])[:80], "model": model,
                       "goal": str(t["goal"])[:4000], "status": "queued", "steps": [], "result": ""})
    lane.add({"type": "agents", "agents": agents})
    person_id, view = run.member_id, run.view

    def helper(agent: dict) -> str:
        with app.app_context():
            g.me = with_rights(db().get(M.Member, person_id), view)
            sub = Lane(run, agent["key"], agent["model"], agent=agent, goal=agent["goal"])
            with run.lock:
                agent["status"] = "running"
            run.touch()
            try:
                answer = _helper_loop(sub)
                with run.lock:
                    agent["status"], agent["result"] = "done", answer
                return answer
            except Stopped:
                with run.lock:
                    agent["status"] = "stopped"
                return "остановлен"
            except Exception as error:                     # noqa: BLE001
                with run.lock:
                    agent["status"], agent["result"] = "error", str(error)[:300]
                return f"ошибка: {error}"
            finally:
                with run.lock:
                    run.lanes.pop(agent["key"], None)
                run.touch()

    run.status("tools", f"Работают помощники: {len(agents)}")
    with ThreadPoolExecutor(max_workers=min(4, len(agents))) as pool:
        answers = list(pool.map(helper, agents))
    run.touch(force=True)
    return "\n\n".join(f"Помощник {a['key']} «{a['title']}» ({a['model']}):\n{ans}" for a, ans in zip(agents, answers)) + \
        "\n\nПРОВЕРЬ ключевые факты помощников своими инструментами, прежде чем опираться на них."


def _helper_loop(lane: Lane) -> str:
    messages = [{"role": "system", "content": _system(lane.run, g.me, helper_goal=lane.goal)},
                {"role": "user", "content": lane.goal}]
    for step in range(1, SUB_STEPS + 1):
        lane.live(step=step)
        result = _ask(lane, messages)
        parsed = _parse(result["text"])
        if parsed is None:
            return result["text"].strip()[:6000]
        messages.append({"role": "assistant", "content": json.dumps(
            {"say": parsed["say"], "calls": parsed["calls"]}, ensure_ascii=False)})
        if not parsed["calls"]:
            return parsed["say"][:6000]
        results = _run_calls(lane, parsed["calls"])
        messages.append({"role": "user", "content": "РЕЗУЛЬТАТЫ ИНСТРУМЕНТОВ\n" + "\n\n".join(results)})
    return "Не успел закончить за отведённые шаги."


# ═══ Страницы и API ═════════════════════════════════════════════════════════
def _boot(member) -> dict:
    """Что нужно странице агента: модели с ценой, права режима и выбора модели."""
    s = db()
    pick = access.can(member, "ai.pick_model")
    prefs = prefs_of(member)
    menu = ai.models_menu(s, pick=pick)
    return {"ready": ai.allowed(s, member), "models": menu, "pick": pick, "auto": access.can(member, "ai.auto"),
            "default": prefs["model"] if pick and prefs["model"] else ai.main_model(s),
            "mode": prefs["mode"] if access.can(member, "ai.auto") else "manual"}


def _chat_json(chat, window: int = WINDOW) -> dict:
    run = live_run(chat.id)
    items = list(run.items) if run else list(chat.items or [])
    total = len(items)
    shown = items[-window:] if window else items
    cleaned = []
    for it in shown:
        it = dict(it)
        it.pop("attach", None)
        cleaned.append(it)
    cost = (run.base_cost + run.cost) if run else float(chat.cost or 0)
    return {"id": chat.id, "title": chat.title, "model": chat.model, "mode": chat.mode, "context": chat.context,
            "items": cleaned, "offset": total - len(shown), "total": total,
            "v": run.version if run else int(chat.updated_at.timestamp() * 1000) if chat.updated_at else 0,
            "run": run.public() if run else None, "spent_label": ai.money(cost) if cost else ""}


@app.route("/ai")
@app.route("/ai/<chat_id>")
@need("ai.use", "page.ai")
def ai_page(chat_id=None):
    chat = _chat(chat_id, g.me.id) if chat_id else None
    context = request.args.get("context", "")
    if request.args.get("project", "").isdigit():
        context = "p" + request.args["project"]
    return render_template("ai.html", chat=chat, chats=chats_of(g.me.id), boot=_boot(g.me),
                           context=context if re.fullmatch(r"(p\d+|desk)?", context) else "",
                           ask=request.args.get("q", "")[:2000])


@app.get("/api/ai/boot")
@need("ai.use")
def ai_boot():
    """Панель «Спросить агента» и окно мастерской собирают чат сами — им нужны модели и права режима."""
    return _boot(g.me)


@app.get("/api/ai/chats")
@need("ai.use")
def ai_chats():
    return {"chats": chats_of(g.me.id)}


@app.post("/api/ai/new")
@need("ai.use")
def ai_new():
    data = request.get_json(silent=True) or {}
    context = str(data.get("context") or "")
    if not re.fullmatch(r"(p\d+|desk)?", context):
        context = ""
    model = str(data.get("model") or "") if access.can(g.me, "ai.pick_model") else ""
    mode = "auto" if data.get("mode") == "auto" and access.can(g.me, "ai.auto") else "manual"
    chat = new_chat(g.me.id, model, mode, context)
    return {"chat": {"id": chat.id, "title": chat.title}}


@app.get("/api/ai/c/<chat_id>")
@need("ai.use")
def ai_chat_get(chat_id):
    chat = _chat(chat_id, g.me.id)
    data = _chat_json(chat)
    if str(data["v"]) == request.args.get("v") and not data["run"]:
        return {"same": True, "v": data["v"], "run": None}
    if request.args.get("before", "").isdigit():
        items = list(chat.items or [])
        before = int(request.args["before"])
        older = items[max(0, before - 200):before]
        for it in older:
            it.pop("attach", None)
        return {"older": older, "offset": max(0, before - 200)}
    return data


def _files() -> List[dict]:
    out = []
    for f in request.files.getlist("files")[:6]:
        name = (f.filename or "файл")[:120]
        raw = f.read(FILE_BYTES + 1)
        if not name.lower().endswith(FILE_EXT) or len(raw) > FILE_BYTES:
            continue
        text = raw.decode("utf-8", "replace")[:FILE_CHARS]
        out.append({"name": name, "text": text})
    return out


@app.post("/api/ai/c/<chat_id>/send")
@need("ai.use")
def ai_send(chat_id):
    chat = _chat(chat_id, g.me.id)
    if not ai.allowed(db(), g.me):
        return {"error": "ИИ для вас не включён — обратитесь к руководителю лаборатории"}, 403
    text = str(request.form.get("text") or (request.get_json(silent=True) or {}).get("text") or "").strip()[:20000]
    files = _files()
    if not text and not files:
        return {"error": "Пустое сообщение"}, 400
    if len(chat.items or []) >= MAX_ITEMS:
        return {"error": "Чат слишком длинный — начните новый"}, 400
    return start(chat, g.me, text, files)


@app.post("/api/ai/c/<chat_id>/stop")
@need("ai.use")
def ai_stop(chat_id):
    _chat(chat_id, g.me.id)
    return {"ok": stop(chat_id, g.me.id)}


@app.post("/api/ai/c/<chat_id>/decide")
@need("ai.use")
def ai_decide(chat_id):
    _chat(chat_id, g.me.id)
    data = request.get_json(silent=True) or {}
    return {"decided": decide(chat_id, g.me.id, str(data.get("tool") or ""), bool(data.get("ok")))}


@app.post("/api/ai/c/<chat_id>/settings")
@need("ai.use")
def ai_chat_settings(chat_id):
    chat = _chat(chat_id, g.me.id)
    data = request.get_json(silent=True) or {}
    if "mode" in data:
        chat.mode = "auto" if data["mode"] == "auto" and access.can(g.me, "ai.auto") else "manual"
        run = live_run(chat.id)
        if run:
            run.mode = chat.mode
    if "model" in data and access.can(g.me, "ai.pick_model"):
        chat.model = str(data["model"] or "")[:80]
    if str(data.get("title") or "").strip():
        chat.title = str(data["title"]).strip()[:200]
    db().commit()
    return {"chat": {"id": chat.id, "title": chat.title, "mode": chat.mode, "model": chat.model}}


@app.post("/api/ai/c/<chat_id>/delete")
@need("ai.use")
def ai_chat_delete(chat_id):
    chat = _chat(chat_id, g.me.id)
    stop(chat.id, g.me.id)
    db().delete(chat)
    db().commit()
    return {"ok": True}


@app.route("/api/ai/memory", methods=["GET", "POST"])
@need("ai.use")
def ai_memory():
    data = request.get_json(silent=True) or {}
    if request.method == "POST":
        if data.get("forget"):
            forget(g.me.id, str(data["forget"]))
        elif str(data.get("text") or "").strip():
            try:
                remember(g.me.id, data["text"])
            except ToolError as error:
                return {"error": str(error)}, 400
    return {"items": memory_of(g.me.id)}


# ─── Настройки ИИ человека: лимиты кольцами, размышления, сжатие, модель, права ──
ABILITIES = [
    ("Смотрит", [("page.projects", "проекты, части, задачи и журналы"), ("page.people", "людей и их профили"),
                 ("page.teams", "команды"), ("workshop.use", "мастерскую: наработки и окна"),
                 ("page.pulse", "сигналы и риски"), ("people.progress", "прогресс и загрузку других")]),
    ("Меняет", [("task.work", "задачи, комментарии, чек-листы, журналы проектов"),
                ("project.edit_team", "проекты своей команды: части, сроки, статус"),
                ("project.edit_all", "любые проекты"), ("task.delete_any", "удаляет любые задачи"),
                ("workshop.use", "мастерскую: заметки, чек-листы, раскладку окон")]),
    ("Администрирует", [("ai.agent_admin", "теги, права и лимиты людей (вместе с правом «Доступы»)")]),
    ("Режим работы", [("ai.auto", "может работать без подтверждений (режим «Авто»)"),
                      ("ai.pick_model", "можно выбирать модель в чате")]),
]


def abilities(member) -> List[dict]:
    out = []
    for group, rows in ABILITIES:
        out.append({"group": group, "rows": [{"label": label, "on": access.can(member, key) and (
            key != "ai.agent_admin" or access.can(member, "admin.access")), "key": key, "name": access.NAMES.get(key, key)}
            for key, label in rows]})
    return out


@app.route("/ai/settings", methods=["GET", "POST"])
@need("ai.use")
def ai_settings_page():
    s = db()
    if request.method == "POST":
        f = request.form
        fields = {"reasoning": f.get("reasoning") if f.get("reasoning") in ai.REASONING else "",
                  "compact": f.get("compact") == "1"}
        if access.can(g.me, "ai.pick_model"):
            fields["model"] = f.get("model", "")[:80]
        if access.can(g.me, "ai.auto"):
            fields["mode"] = "auto" if f.get("mode") == "auto" else "manual"
        save_prefs(g.me.id, **fields)
        flash("Настройки ИИ сохранены", "ok")
        return redirect(url_for("ai_settings_page"))
    month_start = date.today().replace(day=1)
    by_model = s.execute(select(M.AiSpend.model, func.sum(M.AiSpend.cost), func.sum(M.AiSpend.requests))
                         .where(M.AiSpend.member_id == g.me.id, M.AiSpend.day >= month_start)
                         .group_by(M.AiSpend.model).order_by(func.sum(M.AiSpend.cost).desc())).all()
    member = s.get(M.Member, g.me.id)
    return render_template("ai_settings.html", budget=ai.budget(s, g.me), prefs=prefs_of(member),
                           reasoning=ai.REASONING, default_reasoning=ai.settings(s)["reasoning"],
                           models=ai.models_menu(s, pick=access.can(g.me, "ai.pick_model")),
                           main=ai.main_model(s), cheap=ai.cheapest(s), by_model=[
                               {"model": ai.pretty(m or "—"), "cost": ai.money(c or 0), "requests": int(r or 0)}
                               for m, c, r in by_model],
                           abilities=abilities(g.me), memory=memory_of(g.me.id), ready=ai.configured(s))


# ─── Настройка ИИ лаборатории (руководитель) ─────────────────────────────────
@app.route("/admin/ai", methods=["GET", "POST"])
@need("ai.settings")
def admin_ai():
    s = db()
    if request.method == "GET":
        return render_template("admin_ai.html", cfg=ai.settings(s), ready=ai.configured(s),
                               models=ai.models_menu(s) if ai.configured(s) else [], team=ai.team(s),
                               reasoning=ai.REASONING)
    f = request.form
    if f.get("forget"):
        ai.forget_key(s)
        flash("Ключ удалён", "ok")
        return redirect(url_for("admin_ai"))
    team = ", ".join(f.getlist("team_pick")) if f.getlist("team_pick") else f.get("team", "")
    ai.save(s, f.get("base_url", ""), f.get("model", ""), team, f.get("prompt", ""), f.get("reasoning", "low"),
            f.get("api_key"))
    if f.get("test"):
        try:
            flash("ИИ отвечает: " + ai.check(s), "ok")
        except ai.AiError as error:
            flash("Проверка не прошла: " + str(error), "err")
    else:
        flash("Настройки ИИ сохранены", "ok")
    return redirect(url_for("admin_ai"))


@app.get("/admin/ai/models")
@need("ai.settings")
def admin_ai_models():
    """Свежий каталог агрегатора: модели с ценами — для выбора основной модели и команды."""
    s = db()
    if not ai.configured(s):
        return {"error": "Сначала укажите адрес API и основную модель"}, 400
    ai.catalog(s, fresh=True)
    rows = ai.models_menu(s)
    return {"models": rows, "fast": [r["id"] for r in rows if r["cheap"] and not r["default"]][:4]}
