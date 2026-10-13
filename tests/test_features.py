"""Рабочий стол, ИИ, контроль, роли, таймер, стендап, уведомления, задачи."""

import io
import json
import os
import sys
import time
import unittest
from datetime import date, timedelta

os.environ.setdefault("LAB_WORKER", "0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_pages as T  # noqa: E402  — общая база: руководитель h, заказчик c, проект PID

import ai  # noqa: E402
import control  # noqa: E402
import models as M  # noqa: E402

H = {"Sec-Fetch-Site": "same-origin"}


def _seed():
    s = M.Session()
    if not s.get(M.Member, "m1"):
        s.add_all([M.Member(id="m1", name="Мария Сотрудник", role="member", direction_id=2, tags=["сотрудник"]),
                   M.Member(id="l1", name="Лев Лид", role="lead", direction_id=2, tags=["лид команды", "заказчик"])])
        s.add(M.ProjectMember(project_id=T.PID, member_id="m1", role="Бэкенд"))
        s.add(M.Task(project_id=T.PID, title="Просроченная", column="doing", assignee_id="m1",
                     due=date.today() - timedelta(days=2)))
        s.commit()
    s.close()


_seed()


class AccessTest(unittest.TestCase):
    def test_switch_view(self):
        c = T.client("l1")
        self.assertEqual(c.get("/people").status_code, 200)              # кабинет «Лид команды»
        r = c.post("/view", data={"view": "заказчик"}, headers=H)
        self.assertEqual(r.headers["Location"], "/home")
        self.assertEqual(c.get("/people").status_code, 403)              # заказчику люди не видны
        with M.Session() as s:
            self.assertEqual(s.get(M.Member, "l1").tags, ["лид команды", "заказчик"])   # теги не тронуты
        c.post("/view", data={"view": "лид команды"}, headers=H)
        self.assertEqual(c.get("/people").status_code, 200)
        c.post("/view", data={"view": "руководитель лаборатории"}, headers=H)          # чужой кабинет не взять
        self.assertEqual(c.get("/access").status_code, 403)

    def test_access_lives_in_unit_id(self):
        import unitid
        from unittest import mock
        h = T.client("h")
        unitid_id, unitid.CLIENT_ID = unitid.CLIENT_ID, "unit-lab"
        self.addCleanup(setattr, unitid, "CLIENT_ID", unitid_id)
        for url, tail in (("/access", "/access/unit-lab"), ("/access/person/m1", "/person/m1"), ("/access/tag/сотрудник", "/tag/")):
            r = h.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn(tail, r.headers["Location"])
        self.assertEqual(T.client("m1").get("/access").status_code, 403)
        # Первый раз лаборатория отдаёт в UNIT ID каталог и свои правила; дальше — только забирает копию
        sent = []

        def fake(method, path, data=None):
            sent.append((method, path, data))
            if method == "POST":
                return {"rules": data["seed_rules"], "personal": {"m1": {"allow": ["admin.overview"], "deny": ["page.time"],
                                                                          "limits": {"ai_day_usd": 0.5}}}, "version": 7}
            return {"same": True, "version": 7}
        saved = dict(unitid._access)
        try:
            with mock.patch.object(unitid, "enabled", return_value=True), mock.patch.object(unitid, "_call", fake):
                unitid._access.update(version=None, declared=False)
                self.assertTrue(unitid.sync_access())
                self.assertFalse(unitid.sync_access())                         # не менялось — ничего не трогаем
        finally:
            unitid._access.update(saved)
        declared = sent[0][2]
        self.assertIn("сотрудник", [r["tag"] for r in declared["seed_rules"]])
        self.assertEqual(declared["admin_key"], "admin.access")
        self.assertIn("journal.moderate", declared["grants"])
        self.assertEqual(sent[1][1], "/api/service/access?v=7")
        m1 = T.client("m1")
        self.assertEqual(m1.get("/time").status_code, 403)                   # личный запрет из UNIT ID
        self.assertEqual(m1.get("/admin").status_code, 200)
        with M.Session() as s:
            unitid._mirror(s, {"rules": declared["seed_rules"], "personal": {}})
            s.commit()
        self.assertEqual(m1.get("/time").status_code, 200)
        self.assertEqual(m1.get("/admin").status_code, 403)

    def test_teams(self):
        h = T.client("h")
        r = h.post("/teams", data={"title": "DevOps", "about": "Серверы и выкладки", "icon": "cpu", "tone": "teal"},
                   headers=H)
        self.assertIn("/d/devops", r.headers["Location"])
        self.assertIn("DevOps", h.get("/teams").get_data(as_text=True))
        with M.Session() as s:
            tid = s.query(M.Direction).filter_by(slug="devops").one().id
        h.post("/teams", data={"delete": str(tid)}, headers=H)
        self.assertNotIn('href="/d/devops"', h.get("/teams").get_data(as_text=True))
        self.assertEqual(T.client("m1").post("/teams", data={"title": "Чужая"}, headers=H).status_code, 403)


class DeskTest(unittest.TestCase):
    def test_items_files_and_sharing(self):
        c = T.client("m1")
        self.assertEqual(c.get("/desk").status_code, 200)
        note = c.post("/api/desk", json={"kind": "note"}, headers=H).get_json()
        r = c.post(f"/api/desk/{note['id']}", json={"title": "Идеи", "body": "API **быстрее**", "project_id": T.PID,
                                                    "shared": True, "pinned": True, "color": "teal"}, headers=H)
        self.assertTrue(r.get_json()["shared"])
        check = c.post("/api/desk", json={"kind": "checklist", "items": ["раз", "два"]}, headers=H).get_json()
        self.assertEqual(len(check["data"]["items"]), 2)
        up = c.post("/api/desk/upload", data={"file": (io.BytesIO(b"hello"), "notes.txt")}, headers=H,
                    content_type="multipart/form-data").get_json()
        self.assertEqual(c.get(up["url"]).data, b"hello")
        # Руководитель видит общий материал в проекте, но не личный файл
        h = T.client("h")
        self.assertIn("Идеи", h.get(f"/p/{T.PID}").get_data(as_text=True))
        self.assertEqual(h.get(up["url"]).status_code, 404)
        self.assertEqual(h.post(f"/api/desk/{note['id']}", json={"title": "чужое"}, headers=H).status_code, 404)
        copy = h.post(f"/api/desk/{note['id']}/copy", headers=H)
        self.assertEqual(copy.status_code, 201)
        self.assertEqual(c.post(f"/api/desk/{up['id']}/delete", headers=H).get_json(), {"ok": True})

    def test_layout_is_saved_and_cleaned(self):
        c = T.client("m1")
        page = c.get("/desk").get_data(as_text=True)
        for hook in ("data-wd-desk", "data-wd-dock-row", "data-wd-fs", "data-wd-launch"):
            self.assertIn(hook, page)                                   # разметка, которую ждёт desk.js
        saved = c.post("/api/desk/layout", json={"title": "Мой стол", "wins": [
            {"tool": "note", "item_id": 5, "x": 0.1, "y": 0.2, "w": 0.4, "h": 0.5, "max": True},
            {"tool": "<script>", "x": 0}, {"tool": "files", "x": 9, "w": -1}]}, headers=H).get_json()["layout"]
        self.assertEqual([w["tool"] for w in saved["wins"]], ["note", "files"])     # чужие инструменты — прочь
        self.assertEqual((saved["wins"][1]["x"], saved["wins"][1]["w"]), (0.95, 0.12))   # доли стола зажаты
        state = c.get("/api/desk/state").get_json()
        self.assertEqual(state["layout"]["title"], "Мой стол")
        self.assertIn('value="Мой стол"', c.get("/desk").get_data(as_text=True))


class TasksTest(unittest.TestCase):
    def test_board_api(self):
        c = T.client("h")
        with M.Session() as s:
            tid = s.query(M.Task).filter_by(title="Схема").one().id
        self.assertEqual(c.post(f"/api/task/{tid}/move", json={"column": "review"}, headers=H).get_json()["column"], "review")
        r = c.post(f"/api/task/{tid}/check", json={"add": "первый\nвторой"}, headers=H).get_json()
        self.assertEqual(len(r["checklist"]), 2)
        r = c.post(f"/api/task/{tid}/check", json={"index": 0, "done": True}, headers=H).get_json()
        self.assertTrue(r["checklist"][0]["done"])
        t = c.post(f"/api/task/{tid}/comment", json={"text": "Посмотрите, пожалуйста"}, headers=H).get_json()
        self.assertEqual(t["comments"][-1]["text"], "Посмотрите, пожалуйста")
        self.assertEqual(c.get(f"/api/task/{tid}").get_json()["column"], "review")
        r = c.post(f"/p/{T.PID}/task", data={"title": "Из колонки", "column": "doing"},
                   headers={**H, "Accept": "application/json"})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(T.client("c").get(f"/api/task/{tid}").status_code, 403)   # заказчику — нет

    def test_delete_own_and_moderate_journal(self):
        h, m1 = T.client("h"), T.client("m1")
        with M.Session() as s:
            tid = s.query(M.Task).filter_by(title="Просроченная").one().id
        mine = m1.post(f"/api/task/{tid}/comment", json={"text": "моё"}, headers=H).get_json()["comments"][-1]
        boss = h.post(f"/api/task/{tid}/comment", json={"text": "руководителя"}, headers=H).get_json()["comments"][-1]
        self.assertTrue(mine["can_delete"])
        # Чужой комментарий сотруднику не удалить, свой — можно; руководитель (journal.moderate) удаляет любой
        self.assertEqual(m1.post(f"/api/task/{tid}/comment/{boss['id']}/delete", headers=H).status_code, 403)
        left = m1.post(f"/api/task/{tid}/comment/{mine['id']}/delete", headers=H).get_json()["comments"]
        self.assertNotIn(mine["id"], [c["id"] for c in left])
        mine = m1.post(f"/api/task/{tid}/comment", json={"text": "ещё"}, headers=H).get_json()["comments"][-1]
        self.assertEqual(h.post(f"/api/task/{tid}/comment/{mine['id']}/delete", headers=H).status_code, 200)
        # Журнал проекта
        m1.post(f"/p/{T.PID}/update", data={"text": "Запись сотрудника"}, headers=H)
        h.post(f"/p/{T.PID}/update", data={"text": "Запись руководителя"}, headers=H)
        with M.Session() as s:
            own = s.query(M.Update).filter_by(text="Запись сотрудника").one().id
            other = s.query(M.Update).filter_by(text="Запись руководителя").one().id
        self.assertEqual(m1.post(f"/p/{T.PID}/update/{other}/delete", headers=H).status_code, 403)
        self.assertEqual(m1.post(f"/p/{T.PID}/update/{own}/delete", headers=H).status_code, 302)
        self.assertEqual(h.post(f"/p/{T.PID}/update/{other}/delete", headers=H).status_code, 302)
        with M.Session() as s:
            self.assertFalse(s.query(M.Update).filter(M.Update.id.in_([own, other])).count())


class TimerCheckinTest(unittest.TestCase):
    def test_timer_writes_time(self):
        c = T.client("m1")
        with M.Session() as s:
            tid = s.query(M.Task).filter_by(title="Просроченная").one().id
        c.post("/timer", data={"action": "start", "task_id": tid, "next": "/time"}, headers=H)
        self.assertIn("data-timer", c.get("/time").get_data(as_text=True))
        c.post("/timer", data={"action": "stop"}, headers=H)
        with M.Session() as s:
            self.assertIsNone(s.get(M.Timer, "m1"))
            self.assertTrue(s.query(M.TimeEntry).filter_by(member_id="m1", task_id=tid).count())

    def test_checkin_blocker_reaches_leads(self):
        c = T.client("m1")
        c.post("/checkin", data={"done": "API", "plan": "Тесты", "blockers": "Нет доступа к серверу", "mood": "2"}, headers=H)
        with M.Session() as s:
            self.assertTrue(s.query(M.Notice).filter_by(member_id="h", kind="blocker").count())
        h = T.client("h")
        self.assertIn("Нет доступа", h.get("/pulse").get_data(as_text=True))
        self.assertGreaterEqual(h.get("/api/notices").get_json()["unread"], 1)
        self.assertEqual(h.get("/notices").status_code, 200)
        self.assertEqual(h.get("/api/notices").get_json()["unread"], 0)


class ControlTest(unittest.TestCase):
    def test_signals_and_reminders(self):
        with T.app.test_request_context():
            from flask import g
            from app import db, with_rights
            g.me = with_rights(db().get(M.Member, "m1"))
            kinds = [s["kind"] for s in control.signals_for(g.me)]
        self.assertIn("my_late", kinds)
        self.assertGreaterEqual(control.remind_once(), 1)
        self.assertEqual(control.remind_once(), 0)                      # одно напоминание на задачу и дату
        for who, url in (("m1", "/home"), ("m1", "/pulse"), ("h", "/home"), ("c", "/home"), ("h", "/projects"),
                         ("h", f"/people/m1"), ("m1", "/ai")):
            self.assertEqual(T.client(who).get(url).status_code, 200, (who, url))


class AiTest(unittest.TestCase):
    """ИИ-агент: шаги-инструменты с правами человека, «Разрешить» в ручном режиме, авто, лимиты."""
    PRICES = {"test-model": {"price_input_per_1m_usd": 15, "price_output_per_1m_usd": 75},
              "cheap-flash": {"price_input_per_1m_usd": 0.1, "price_output_per_1m_usd": 0.4}}

    def setUp(self):
        with M.Session() as s:
            ai.save(s, "https://example.invalid/v1", "test-model", "cheap-flash", "Будь краток")
        self._call, self._ask, self._catalog = ai.call, ai.ask, ai.catalog
        ai.catalog = lambda s, fresh=False: self.PRICES
        self.script = []
        self.seen = []

        def fake_call(s, messages, model="", **kw):
            self.seen.append((model, messages))
            text = self.script.pop(0) if self.script else json.dumps({"say": "Готово.", "calls": []})
            return {"text": text, "reasoning": "", "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
                    "model": model, "cost": ai.cost_of(s, model, {"prompt_tokens": 1000, "completion_tokens": 100})[0],
                    "tokens": 1100, "stopped": False}
        ai.call = fake_call

    def tearDown(self):
        ai.call, ai.ask, ai.catalog = self._call, self._ask, self._catalog

    def _wait(self, c, chat, until, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = c.get(f"/api/ai/c/{chat}").get_json()
            if until(data):
                return data
            time.sleep(0.05)
        self.fail("агент не дошёл до нужного состояния: " + json.dumps(data, ensure_ascii=False)[:600])

    def test_agent_asks_before_changes_and_acts_with_rights(self):
        self.script = [
            json.dumps({"say": "Смотрю проекты и заведу задачу.", "calls": [
                {"tool": "list_projects", "args": {}},
                {"tool": "create_task", "args": {"project_id": T.PID, "title": "Написать тесты", "assignee_id": "m1",
                                                 "due": "2026-12-01"}}]}),
            json.dumps({"say": "Задача заведена.", "calls": []}),
        ]
        c = T.client("h")
        self.assertEqual(c.get("/ai").status_code, 200)
        chat = c.post("/api/ai/new", json={"mode": "manual", "context": f"p{T.PID}"}, headers=H).get_json()["chat"]["id"]
        self.assertEqual(c.post(f"/api/ai/c/{chat}/send", data={"text": "Заведи задачу на тесты"}, headers=H).status_code, 200)
        data = self._wait(c, chat, lambda d: any(i.get("status") == "awaiting" for i in d["items"]))
        waiting = [i for i in data["items"] if i.get("status") == "awaiting"][0]
        self.assertEqual(waiting["name"], "create_task")
        self.assertIn("Написать тесты", waiting["confirm"]["text"])                  # человек видит, что именно
        with M.Session() as s:
            self.assertFalse(s.query(M.Task).filter_by(title="Написать тесты").count())   # до «Разрешить» — ничего
        c.post(f"/api/ai/c/{chat}/decide", json={"tool": waiting["id"], "ok": True}, headers=H)
        data = self._wait(c, chat, lambda d: not d["run"] and any(i["type"] == "usage" for i in d["items"]))
        with M.Session() as s:
            task = s.query(M.Task).filter_by(title="Написать тесты").one()
            self.assertEqual((task.assignee_id, str(task.due)), ("m1", "2026-12-01"))
        kinds = [i["type"] for i in data["items"]]
        self.assertEqual(kinds[0], "user")
        self.assertIn("usage", kinds)
        self.assertEqual(data["items"][-2]["text"], "Задача заведена.")
        system = self.seen[0][1][0]["content"]
        self.assertIn("Будь краток", system)                                          # указания руководителя
        self.assertIn("create_task", system)
        self.assertIn(f"Разговор начат со страницы проекта {T.PID}", system)
        with M.Session() as s:
            self.assertGreater(s.query(M.AiSpend).filter_by(member_id="h").count(), 0)  # расход посчитан
        # Чужой чат не открыть, заказчику ИИ не положен
        self.assertEqual(T.client("m1").get(f"/api/ai/c/{chat}").status_code, 404)
        self.assertEqual(T.client("c").post("/api/ai/new", json={}, headers=H).status_code, 403)

    def test_auto_mode_and_rights_are_checked_by_tools(self):
        # Сотрудник без права «Агент без подтверждений» — авто не включить; чужой проект менять нельзя
        self.script = [json.dumps({"say": "", "calls": [{"tool": "set_person_tags", "args": {"member_id": "m1",
                                                                                           "add": ["руководитель лаборатории"]}}]}),
                       json.dumps({"say": "Не могу.", "calls": []})]
        m1 = T.client("m1")
        chat = m1.post("/api/ai/new", json={"mode": "auto"}, headers=H).get_json()["chat"]["id"]
        self.assertEqual(m1.get(f"/api/ai/c/{chat}").get_json()["mode"], "manual")
        m1.post(f"/api/ai/c/{chat}/send", data={"text": "Сделай меня руководителем"}, headers=H)
        data = self._wait(m1, chat, lambda d: not d["run"] and any(i["type"] == "usage" for i in d["items"]))
        tool = [i for i in data["items"] if i["type"] == "tool"][0]
        self.assertEqual((tool["status"], tool["summary"]), ("error", "нет права"))
        with M.Session() as s:
            self.assertNotIn("руководитель лаборатории", s.get(M.Member, "m1").tags)
        # Руководитель в авто: заметка ложится в мастерскую сразу, без «Разрешить»
        self.script = [json.dumps({"say": "", "calls": [{"tool": "desk_add", "args": {"kind": "checklist", "title": "План",
                                                                                     "items": ["раз", "два"]}},
                                                        {"tool": "arrange_workshop", "args": {"preset": "grid"}}]})]
        h = T.client("h")
        chat = h.post("/api/ai/new", json={"mode": "auto"}, headers=H).get_json()["chat"]["id"]
        h.post(f"/api/ai/c/{chat}/send", data={"text": "Наведи порядок в мастерской"}, headers=H)
        data = self._wait(h, chat, lambda d: not d["run"] and any(i["type"] == "usage" for i in d["items"]))
        self.assertEqual([i["status"] for i in data["items"] if i["type"] == "tool"], ["done", "done"])
        self.assertIn("desk", [i for i in data["items"] if i["type"] == "usage"][0]["touched"])
        with M.Session() as s:
            self.assertTrue(s.query(M.DeskItem).filter_by(member_id="h", title="План").count())

    def test_limit_switches_to_cheap_model(self):
        with M.Session() as s:
            s.merge(M.MemberAccess(member_id="m1", allow=[], deny=[], limits={"ai_day_usd": 0.01}))
            s.query(M.AiSpend).filter_by(member_id="m1").delete()
            s.add(M.AiSpend(member_id="m1", day=date.today(), model="test-model", requests=1, tokens=1, cost=0.5))
            s.commit()
        try:
            m1 = T.client("m1")
            page = m1.get("/ai/settings").get_data(as_text=True)
            self.assertIn("Лимит исчерпан", page)
            chat = m1.post("/api/ai/new", json={}, headers=H).get_json()["chat"]["id"]
            m1.post(f"/api/ai/c/{chat}/send", data={"text": "Привет"}, headers=H)
            data = self._wait(m1, chat, lambda d: not d["run"] and any(i["type"] == "usage" for i in d["items"]))
            self.assertEqual(self.seen[0][0], "cheap-flash")                         # лимит исчерпан — дешёвая модель
            self.assertTrue(any("Лимит на ИИ исчерпан" in (i.get("text") or "") for i in data["items"]))
        finally:
            with M.Session() as s:
                s.delete(s.get(M.MemberAccess, "m1"))
                s.query(M.AiSpend).filter_by(member_id="m1").delete()
                s.commit()

    def test_brief_and_access(self):
        ai.ask = lambda s, messages, **kw: ("- Начните с просроченной задачи", 0.0001, "cheap-flash")
        c = T.client("m1")
        self.assertIn("просроченной", c.post("/api/ai/brief", json={"scope": "day"}, headers=H).get_json()["text"])
        self.assertIn("просроченной", c.get("/home").get_data(as_text=True))
        with M.Session() as s:
            s.merge(M.MemberAccess(member_id="m1", allow=[], deny=["ai.use"], limits={}))
            s.commit()
        self.assertEqual(c.post("/api/ai/brief", json={"scope": "day"}, headers=H).status_code, 403)
        with M.Session() as s:
            s.delete(s.get(M.MemberAccess, "m1"))
            s.commit()

    def test_admin_saves_settings_without_leaking_key(self):
        c = T.client("h")
        c.post("/admin/ai", data={"base_url": "https://x.invalid/v1", "model": "test-model", "api_key": "sk-secret-123456",
                                  "team_pick": ["cheap-flash"], "reasoning": "medium"}, headers=H)
        page = c.get("/admin/ai").get_data(as_text=True)
        self.assertNotIn("sk-secret-123456", page)
        self.assertIn("sk-…3456", page)
        with M.Session() as s:
            cfg = ai.settings(s)
            self.assertEqual((cfg["team"], cfg["reasoning"]), (["cheap-flash"], "medium"))
        self.assertEqual(c.get("/ai/settings").status_code, 200)


class ProfileTest(unittest.TestCase):
    def test_compact_profile_form(self):
        c = T.client("m1")
        c.post("/people/m1", data={"position": "Бэкенд", "skill": ["Python|5", "SQL|3", "python|1"], "telegram": "@m"},
               headers=H)
        with M.Session() as s:
            self.assertEqual(s.get(M.Member, "m1").skills, [{"name": "Python", "level": 5}, {"name": "SQL", "level": 3}])
        self.assertIn('id="profile-sheet"', c.get("/people/m1").get_data(as_text=True))


class UnitIdSyncTest(unittest.TestCase):
    def test_first_pull_pushes_old_tags_then_syncs(self):
        import unitid
        pushed = []
        accounts = [{"id": "h", "name": "Руководитель Лаборатории", "email": "h@labunit.ru", "tags": ["школа"]},
                    {"id": "new1", "name": "Новый Сотрудник", "email": "n@labunit.ru", "tags": ["сотрудник"]},
                    {"id": "stranger", "name": "Ученик", "email": "s@labunit.ru", "tags": ["школа", "ученик"]}]
        saved = (unitid._call, unitid.push_tags, unitid.CLIENT_ID, unitid.CLIENT_SECRET)
        unitid.CLIENT_ID, unitid.CLIENT_SECRET = "lab", "secret"
        unitid._call = lambda method, path, data=None: {"accounts": [dict(a) for a in accounts]}
        unitid.push_tags = lambda mid, add=(), remove=(): pushed.append((mid, list(add))) or True
        try:
            with M.Session() as s:
                row = s.get(M.Setting, "unitid_tags_seeded")
                if row:
                    s.delete(row)
                    s.commit()
            unitid.pull()
            self.assertIn(("h", ["руководитель лаборатории"]), pushed)          # старый тег ушёл в UNIT ID
            with M.Session() as s:
                self.assertIn("руководитель лаборатории", s.get(M.Member, "h").tags)   # и не стёрся
                self.assertIsNotNone(s.get(M.Member, "new1"))                   # тег с правилом — в лабораторию
                self.assertIsNone(s.get(M.Member, "stranger"))                  # ученику школы тут делать нечего
        finally:
            unitid._call, unitid.push_tags, unitid.CLIENT_ID, unitid.CLIENT_SECRET = saved


class ProjectFlowTest(unittest.TestCase):
    def test_wizard_create_status_and_delete(self):
        h = T.client("h")
        r = h.post("/projects", data={"title": "Мастер", "summary": "Проверка", "direction_id": "2", "due": "2026-12-31",
                                      "priority": "1", "customer": "Свой", "team": ["m1"],
                                      "parts": "Бэкенд\nФронтенд\n"}, headers=H)
        pid = int(r.headers["Location"].rsplit("/", 1)[1])
        with M.Session() as s:
            p = s.get(M.Project, pid)
            self.assertEqual((str(p.due), p.priority, p.customer), ("2026-12-31", 1, "Свой"))
            self.assertEqual(s.query(M.Part).filter_by(project_id=pid).count(), 2)
            self.assertTrue(s.query(M.ProjectMember).filter_by(project_id=pid, member_id="m1").count())
        page = h.get(f"/p/{pid}").get_data(as_text=True)
        self.assertIn("data-pages-tabs", page)
        h.post(f"/p/{pid}/status", data={"status": "done"}, headers=H)
        with M.Session() as s:
            self.assertEqual(s.get(M.Project, pid).status, "done")
        # Задачу удаляет автор, чужую — нет (у сотрудника нет права task.delete_any)
        m1 = T.client("m1")
        h.post(f"/p/{pid}/task", data={"title": "Чужая"}, headers=H)
        m1.post(f"/p/{pid}/task", data={"title": "Своя"}, headers=H)
        with M.Session() as s:
            own = s.query(M.Task).filter_by(title="Своя").one().id
            other = s.query(M.Task).filter_by(title="Чужая").one().id
        self.assertEqual(m1.post(f"/api/task/{other}/delete", headers=H).status_code, 403)
        self.assertEqual(m1.post(f"/api/task/{own}/delete", headers=H).get_json(), {"ok": True})
        self.assertEqual(m1.post(f"/p/{pid}/delete", headers=H).status_code, 403)              # нет права удалять проекты
        h.post(f"/p/{pid}/delete", headers=H)
        with M.Session() as s:
            self.assertIsNone(s.get(M.Project, pid))
            self.assertEqual(s.query(M.Task).filter_by(project_id=pid).count(), 0)
            self.assertEqual(s.query(M.Part).filter_by(project_id=pid).count(), 0)


class GitIntegrationTest(unittest.TestCase):
    def test_git_link_unlink_and_import(self):
        import git_hub
        c = T.client("m1")
        # 1. Привязка аккаунта
        r = c.post("/api/git/link", json={"username": "testuser", "token": "ghp_secret"}, headers=H).get_json()
        self.assertTrue(r["ok"])
        self.assertTrue(r["info"]["linked"])
        self.assertEqual(r["info"]["username"], "testuser")

        # 2. Проверка статуса
        status = c.get("/api/git/status").get_json()
        self.assertEqual(status["username"], "testuser")
        self.assertTrue(status["linked"])

        # 3. Список репозиториев (с моком)
        saved_req = git_hub._github_request
        mock_repos = [{"name": "super-app", "full_name": "testuser/super-app", "description": "Крутое приложение",
                       "html_url": "https://github.com/testuser/super-app", "language": "Python",
                       "stargazers_count": 5, "updated_at": "2026-10-12T10:00:00Z"}]
        mock_details = {
            "name": "super-app", "owner": "testuser", "full_name": "testuser/super-app",
            "description": "Крутое приложение", "html_url": "https://github.com/testuser/super-app",
            "readme": "# Super App\n\nИнструкция и запуск.",
            "issues": [{"number": 1, "title": "Сделать API", "html_url": "https://github.com/testuser/super-app/issues/1"}]
        }
        git_hub._github_request = lambda url, token="": mock_repos if "repos" in url and "readme" not in url and "issues" not in url else {}
        git_hub.fetch_repo_details = lambda repo, token="": mock_details
        try:
            repos_res = c.get("/api/git/repos").get_json()
            self.assertEqual(len(repos_res["repos"]), 1)
            self.assertEqual(repos_res["repos"][0]["name"], "super-app")

            # 4. Импорт репозитория на стол в мастерскую
            imp_res = c.post("/api/git/import", json={"repo": "testuser/super-app", "project_id": T.PID}, headers=H).get_json()
            self.assertTrue(imp_res["ok"])
            created_kinds = [it["kind"] for it in imp_res["created"]]
            self.assertIn("link", created_kinds)
            self.assertIn("note", created_kinds)
            self.assertIn("checklist", created_kinds)

            with M.Session() as s:
                items = s.query(M.DeskItem).filter_by(member_id="m1").all()
                titles = [it.title for it in items]
                self.assertTrue(any("README" in t for t in titles))
                self.assertTrue(any("Задачи GitHub" in t for t in titles))
                self.assertTrue(any("Репозиторий" in t for t in titles))
        finally:
            git_hub._github_request = saved_req

        # 5. Отвязка аккаунта
        un = c.post("/api/git/unlink", headers=H).get_json()
        self.assertTrue(un["ok"])
        self.assertFalse(un["info"]["linked"])
        self.assertEqual(c.get("/api/git/repos").status_code, 400)


class SupportSectionTest(unittest.TestCase):
    def test_support_ticket_flow(self):
        c = T.client("m1")
        # 1. Главная поддержки
        res = c.get("/support")
        self.assertEqual(res.status_code, 200)

        # 2. Создание обращения
        r = c.post("/support/new", data={
            "subject": "Не работает таймер",
            "category": "tech",
            "priority": "high",
            "body": "После нажатия на паузу таймер сбрасывается."
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)

        with M.Session() as s:
            t = s.query(M.SupportTicket).filter_by(member_id="m1").first()
            self.assertIsNotNone(t)
            self.assertEqual(t.subject, "Не работает таймер")
            self.assertEqual(t.status, "open")
            tid = t.id

            msg = s.query(M.SupportMessage).filter_by(ticket_id=tid).first()
            self.assertIsNotNone(msg)
            self.assertIn("После нажатия", msg.body)

        # 3. Ответ в ветке
        reply_res = c.post(f"/support/{tid}/reply", data={"body": "Дополнительный комментарий"}, follow_redirects=True)
        self.assertEqual(reply_res.status_code, 200)

        # 4. Смена статуса
        status_res = c.post(f"/support/{tid}/status", data={"status": "resolved"}, follow_redirects=True)
        self.assertEqual(status_res.status_code, 200)
        with M.Session() as s:
            t = s.get(M.SupportTicket, tid)
            self.assertEqual(t.status, "resolved")

        # 5. Удаление обращения сотрудником/админом
        self.assertEqual(c.post(f"/support/{tid}/delete").status_code, 403)   # автор — не поддержка
        del_res = T.client("h").post(f"/support/{tid}/delete", follow_redirects=True)
        self.assertEqual(del_res.status_code, 200)
        with M.Session() as s:
            self.assertIsNone(s.get(M.SupportTicket, tid))


class SettingsAndLockTest(unittest.TestCase):
    """Настройки человека и код приложения (решает сервер)."""

    def test_settings_save_prefs_and_mute(self):
        c = T.client("m1")
        self.assertEqual(c.get("/settings").status_code, 200)
        c.post("/settings", data={"haptics": "1", "notify_email": "1", "kind:task": "1", "kind:info": "1"}, headers=H)
        with M.Session() as s:
            m = s.get(M.Member, "m1")
            self.assertEqual(set(m.prefs["mute"]), {"deadline", "blocker", "support"})
            self.assertTrue(m.notify_email)
        with T.app.test_request_context():
            import settings
            self.assertTrue(settings.muted("m1", "blocker"))
            self.assertFalse(settings.muted("m1", "task"))
        c.post("/settings", data={"haptics": "1", "notify_email": "1", **{f"kind:{k}": "1" for k, _, _ in
                                                                        __import__("settings").KINDS}}, headers=H)

    def test_token_section_talks_to_unit_id(self):
        import unitid
        calls = []
        saved = unitid.tokens, unitid.token_create, unitid.token_revoke
        unitid.tokens = lambda uid: [{"id": "pt-1", "name": "Скрипт", "prefix": "unit_pat_abc", "scopes": ["lab:read"],
                                      "created": "12.10.2026", "expires": "", "used": ""}]
        unitid.token_create = lambda uid, name, scopes, days: calls.append((uid, name, scopes, days)) or {"token": "unit_pat_SECRET"}
        unitid.token_revoke = lambda uid, tid: calls.append(("revoke", uid, tid))
        try:
            c = T.client("m1")
            self.assertIn("Скрипт", c.get("/settings").get_data(as_text=True))
            page = c.post("/settings/token", data={"name": "Отчёт", "level": "write", "days": "30"}, headers=H)
            self.assertIn("unit_pat_SECRET", page.get_data(as_text=True))           # показан один раз
            self.assertEqual(calls[0], ("m1", "Отчёт", ["lab:read", "lab:write"], 30))
            c.post("/settings/token/pt-1/revoke", headers=H)
            self.assertEqual(calls[1], ("revoke", "m1", "pt-1"))
        finally:
            unitid.tokens, unitid.token_create, unitid.token_revoke = saved

    def test_app_lock_is_decided_by_server(self):
        c = T.client("h")
        page = c.get("/home").get_data(as_text=True)
        self.assertNotIn('id="unit-app-lock"', page)                   # во вкладке браузера кода нет
        c.set_cookie("unit_app", "1")
        self.assertIn('data-lock="setup"', c.get("/home").get_data(as_text=True))
        self.assertTrue(c.post("/lock/pin", json={"pin": "582047"}, headers=H).get_json()["ok"])
        self.assertIn('data-lock="on"', c.get("/home").get_data(as_text=True))
        with c.session_transaction() as sess:
            sess["seen"] = 0                                            # давно не открывали
        locked = c.get("/projects").get_data(as_text=True)
        self.assertIn('data-lock="locked"', locked)
        self.assertNotIn("Портал записи", locked)                      # данных на экране кода нет
        self.assertEqual(c.get(f"/api/task/1", headers={"Accept": "application/json"}).status_code, 423)
        self.assertEqual(c.post("/lock/unlock", json={"pin": "111112"}, headers=H).status_code, 400)
        self.assertTrue(c.post("/lock/unlock", json={"pin": "582047"}, headers=H).get_json()["ok"])
        self.assertEqual(c.get("/projects").status_code, 200)
        # Смена кода — только с текущим
        self.assertEqual(c.post("/lock/pin", json={"pin": "739104", "current": "000000"}, headers=H).status_code, 400)
        self.assertTrue(c.post("/lock/pin", json={"pin": "739104", "current": "582047"}, headers=H).get_json()["ok"])
        # Пять ошибок — выход
        c.post("/lock/now", headers=H)
        for _ in range(4):
            c.post("/lock/unlock", json={"pin": "000003"}, headers=H)
        self.assertTrue(c.post("/lock/unlock", json={"pin": "000003"}, headers=H).get_json()["logout"])
        self.assertEqual(c.get("/projects").status_code, 302)

    def test_github_token_encryption(self):
        import git_hub
        raw_token = "ghp_1234567890abcdefABCDEF"
        encrypted = git_hub.encrypt_token(raw_token)
        self.assertTrue(encrypted.startswith("enc:"))
        self.assertNotEqual(encrypted, raw_token)
        decrypted = git_hub.decrypt_token(encrypted)
        self.assertEqual(decrypted, raw_token)
        # Plaintext backward compatibility
        self.assertEqual(git_hub.decrypt_token(raw_token), raw_token)
        self.assertEqual(git_hub.decrypt_token(""), "")

    def test_leads_and_notification(self):
        # 1. Отправка заявки с лендинга
        c = T.app.test_client()
        res = c.post("/lead", data={"name": "Иван", "contact": "@ivan_dev", "about": "Пишу на Python"}, headers=H)
        self.assertEqual(res.status_code, 302)
        with M.Session() as s:
            leads_row = s.get(M.Setting, "leads_archive")
            self.assertIsNotNone(leads_row)
            leads = json.loads(leads_row.value)
            self.assertTrue(len(leads) > 0)
            self.assertEqual(leads[0]["name"], "Иван")
            self.assertEqual(leads[0]["contact"], "@ivan_dev")
            self.assertEqual(leads[0]["status"], "new")
            lead_id = leads[0]["id"]

        # 2. Просмотр заявок руководителем (/leads)
        h = T.client("h")
        r = h.get("/leads")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Иван", r.get_data(as_text=True))
        self.assertIn("@ivan_dev", r.get_data(as_text=True))

        # Обычный сотрудник без прав не имеет доступа к /leads
        m1 = T.client("m1")
        self.assertEqual(m1.get("/leads").status_code, 403)

        # 3. Смена статуса заявки
        r = h.post(f"/api/lead/{lead_id}/status", data={"status": "in_progress"}, headers=H)
        self.assertEqual(r.status_code, 302)
        with M.Session() as s:
            leads = json.loads(s.get(M.Setting, "leads_archive").value)
            self.assertEqual(leads[0]["status"], "in_progress")

        # 4. Удаление заявки
        r = h.post(f"/api/lead/{lead_id}/delete", headers=H)
        self.assertEqual(r.status_code, 302)
        with M.Session() as s:
            leads = json.loads(s.get(M.Setting, "leads_archive").value)
            self.assertEqual(len(leads), 0)

    def test_team_rename_and_delete(self):
        h = T.client("h")
        with M.Session() as s:
            d = s.query(M.Direction).first()
            team = M.Team(direction_id=d.id, title="NLP")
            s.add(team)
            s.commit()
            tid = team.id

        # Переименование группы
        r = h.post(f"/api/team/{tid}/rename", data={"title": "LLM & AI"}, headers=H)
        self.assertEqual(r.status_code, 302)
        with M.Session() as s:
            team = s.get(M.Team, tid)
            self.assertEqual(team.title, "LLM & AI")

        # Удаление группы
        r = h.post(f"/api/team/{tid}/delete", headers=H)
        self.assertEqual(r.status_code, 302)
        with M.Session() as s:
            self.assertIsNone(s.get(M.Team, tid))

    def test_person_leads_notify_requires_email(self):
        h = T.client("h")
        with M.Session() as s:
            m = s.get(M.Member, "m1")
            m.email = ""
            s.commit()

        # Попытка включить leads_notify без email — не должна сохранять leads.notify в allow
        h.post("/people/m1", data={"leads_notify": "1"}, headers=H)
        with M.Session() as s:
            ma = s.get(M.MemberAccess, "m1")
            self.assertTrue(not ma or "leads.notify" not in (ma.allow or []))

        # С указанным email — включается успешно
        with M.Session() as s:
            m = s.get(M.Member, "m1")
            m.email = "m1@labunit.ru"
            s.commit()

        h.post("/people/m1", data={"leads_notify": "1"}, headers=H)
        with M.Session() as s:
            ma = s.get(M.MemberAccess, "m1")
            self.assertIn("leads.notify", ma.allow)


if __name__ == "__main__":
    unittest.main()


