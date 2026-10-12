"""Рабочий стол, ИИ, контроль, роли, таймер, стендап, уведомления, задачи."""

import io
import json
import os
import sys
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
        s.add_all([M.Member(id="m1", name="Мария Сотрудник", role="member", direction_id=2),
                   M.Member(id="l1", name="Лев Лид", role="lead", roles=["lead", "customer"], direction_id=2)])
        s.add(M.ProjectMember(project_id=T.PID, member_id="m1", role="Бэкенд"))
        s.add(M.Task(project_id=T.PID, title="Просроченная", column="doing", assignee_id="m1",
                     due=date.today() - timedelta(days=2)))
        s.commit()
    s.close()


_seed()


class RolesTest(unittest.TestCase):
    def test_switch_role(self):
        c = T.client("l1")
        self.assertEqual(c.get("/people").status_code, 200)              # как руководитель направления
        r = c.post("/role", data={"role": "customer", "next": "/people"}, headers=H)
        self.assertEqual(r.headers["Location"], "/home")
        self.assertEqual(c.get("/people").status_code, 403)              # заказчику люди не видны
        with M.Session() as s:
            self.assertEqual(s.get(M.Member, "l1").role, "lead")         # в базе роль не поменялась
        c.post("/role", data={"role": "lead"}, headers=H)
        self.assertEqual(c.get("/people").status_code, 200)
        self.assertEqual(c.post("/role", data={"role": "head"}, headers=H).status_code, 302)
        self.assertEqual(c.get("/admin").status_code, 403)               # чужую роль не взять

    def test_head_grants_roles(self):
        c = T.client("h")
        c.post("/admin", data={"member_id": "m1", "roles": ["member", "customer"], "company": ""}, headers=H)
        with M.Session() as s:
            self.assertEqual(s.get(M.Member, "m1").all_roles(), ["member", "customer"])
        c.post("/admin", data={"member_id": "m1", "roles": ["member"]}, headers=H)
        for tab in ("overview", "people", "ai"):
            self.assertEqual(c.get(f"/admin?tab={tab}").status_code, 200, tab)


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
            from app import db
            g.me = db().get(M.Member, "m1")
            kinds = [s["kind"] for s in control.signals_for(g.me)]
        self.assertIn("my_late", kinds)
        self.assertGreaterEqual(control.remind_once(), 1)
        self.assertEqual(control.remind_once(), 0)                      # одно напоминание на задачу и дату
        for who, url in (("m1", "/home"), ("m1", "/pulse"), ("h", "/home"), ("c", "/home"), ("h", "/projects"),
                         ("h", f"/people/m1"), ("m1", "/ai")):
            self.assertEqual(T.client(who).get(url).status_code, 200, (who, url))


class AiTest(unittest.TestCase):
    def setUp(self):
        with M.Session() as s:
            ai.save(s, "https://example.invalid/v1", "test-model", "", ["head", "lead", "member"], 50, "Будь краток")
        self._stream, self._ask = ai.stream, ai.ask

    def tearDown(self):
        ai.stream, ai.ask = self._stream, self._ask

    def test_chat_with_actions(self):
        reply = ('План готов.\n```actions\n[{"type": "task", "project_id": %d, "title": "Написать тесты", '
                 '"assignee_id": "m1", "due": "2026-12-01"}, {"type": "note", "title": "Итоги", "body": "…"}]\n```' % T.PID)
        seen = {}

        def fake_stream(s, messages, **kw):
            seen["system"] = messages[0]["content"]
            for piece in (reply[:20], reply[20:]):
                yield {"delta": piece}
            yield {"done": True, "tokens": 42}

        ai.stream = fake_stream
        c = T.client("h")
        r = c.post("/api/ai/chat", json={"text": "Что делать?", "thread": f"p{T.PID}"}, headers=H)
        events = [json.loads(line[6:]) for line in r.get_data(as_text=True).split("\n\n") if line.startswith("data: ")]
        done = events[-1]["message"]
        self.assertEqual(done["text"], "План готов.")
        self.assertEqual([a["label"] for a in done["actions"]], ["Задача", "Заметка на стол"])
        self.assertIn("Будь краток", seen["system"])
        self.assertIn("Портал", seen["system"])
        res = c.post("/api/ai/apply", json={"message_id": done["id"], "index": 0}, headers=H).get_json()
        self.assertIn("/p/", res["link"])
        self.assertEqual(c.post("/api/ai/apply", json={"message_id": done["id"], "index": 0}, headers=H).status_code, 409)
        with M.Session() as s:
            task = s.query(M.Task).filter_by(title="Написать тесты").one()
            self.assertEqual((task.assignee_id, str(task.due)), ("m1", "2026-12-01"))
        history = c.get(f"/api/ai/history?thread=p{T.PID}").get_json()["messages"]
        self.assertTrue(history[-1]["actions"][0]["applied"])
        # Заказчику ИИ не положен; чужие сообщения не применить
        self.assertEqual(T.client("c").post("/api/ai/chat", json={"text": "?"}, headers=H).status_code, 403)
        self.assertEqual(T.client("m1").post("/api/ai/apply", json={"message_id": done["id"], "index": 1},
                                             headers=H).status_code, 404)

    def test_brief_and_limit(self):
        ai.ask = lambda s, messages, **kw: ("- Начните с просроченной задачи", 10)
        c = T.client("m1")
        self.assertIn("просроченной", c.post("/api/ai/brief", json={"scope": "day"}, headers=H).get_json()["text"])
        self.assertIn("просроченной", c.get("/home").get_data(as_text=True))
        with M.Session() as s:
            ai.save(s, "https://example.invalid/v1", "test-model", "", ["head"], 50, "")
        self.assertEqual(c.post("/api/ai/brief", json={"scope": "day"}, headers=H).status_code, 403)

    def test_admin_saves_settings_without_leaking_key(self):
        c = T.client("h")
        c.post("/admin/ai", data={"base_url": "https://x.invalid/v1", "model": "m", "api_key": "sk-secret-123456",
                                  "roles": ["head", "member"], "daily": "10"}, headers=H)
        page = c.get("/admin?tab=ai").get_data(as_text=True)
        self.assertNotIn("sk-secret-123456", page)
        self.assertIn("sk-…3456", page)


class ProfileTest(unittest.TestCase):
    def test_compact_profile_form(self):
        c = T.client("m1")
        c.post("/people/m1", data={"position": "Бэкенд", "skill": ["Python|5", "SQL|3", "python|1"], "telegram": "@m"},
               headers=H)
        with M.Session() as s:
            self.assertEqual(s.get(M.Member, "m1").skills, [{"name": "Python", "level": 5}, {"name": "SQL", "level": 3}])
        self.assertIn('id="profile-sheet"', c.get("/people/m1").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
