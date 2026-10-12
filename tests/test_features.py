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

    def test_tags_and_personal_rights(self):
        h = T.client("h")
        for url in ("/access", "/access?tab=tags", "/access/person/m1", "/access/tag/сотрудник", "/admin", "/teams"):
            self.assertEqual(h.get(url).status_code, 200, url)
        h.post("/access/person/m1", data={"action": "tag_add", "tag": "Доступ к ИИ"}, headers=H)
        with M.Session() as s:
            self.assertIn("доступ к ии", s.get(M.Member, "m1").tags)
        # Личный запрет перекрывает теги, личное разрешение — добавляет
        h.post("/access/person/m1", data={"action": "personal", "p:page.time": "deny", "p:admin.overview": "allow",
                                          "l:ai_day_usd": "0,5"}, headers=H)
        m1 = T.client("m1")
        self.assertEqual(m1.get("/time").status_code, 403)
        self.assertEqual(m1.get("/admin").status_code, 200)
        h.post("/access/person/m1", data={"action": "personal"}, headers=H)                   # всё по тегам
        self.assertEqual(m1.get("/time").status_code, 200)
        self.assertEqual(m1.get("/admin").status_code, 403)
        # Новый тег с правами — сразу действует
        h.post("/access/tag", data={"tag": "наблюдатель", "title": "Наблюдатель"}, headers=H)
        h.post("/access/tag/наблюдатель", data={"title": "Наблюдатель", "p:admin.overview": "1"}, headers=H)
        h.post("/access/person/m1", data={"action": "tag_add", "tag": "наблюдатель"}, headers=H)
        self.assertEqual(m1.get("/admin").status_code, 200)
        # Себе «Доступы» не закрыть
        h.post("/access/person/h", data={"action": "personal", "p:admin.access": "deny"}, headers=H)
        self.assertEqual(h.get("/access").status_code, 200)

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
    def setUp(self):
        with M.Session() as s:
            ai.save(s, "https://example.invalid/v1", "test-model", "", [], 0, "Будь краток")
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
            s.merge(M.MemberAccess(member_id="m1", allow=[], deny=["ai.use"], limits={}))
            s.commit()
        self.assertEqual(c.post("/api/ai/brief", json={"scope": "day"}, headers=H).status_code, 403)
        with M.Session() as s:
            s.delete(s.get(M.MemberAccess, "m1"))
            s.commit()

    def test_admin_saves_settings_without_leaking_key(self):
        c = T.client("h")
        c.post("/admin/ai", data={"base_url": "https://x.invalid/v1", "model": "m", "api_key": "sk-secret-123456",
                                  "roles": ["head", "member"], "daily": "10"}, headers=H)
        page = c.get("/admin/ai").get_data(as_text=True)
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
        del_res = c.post(f"/support/{tid}/delete", follow_redirects=True)
        self.assertEqual(del_res.status_code, 200)
        with M.Session() as s:
            self.assertIsNone(s.get(M.SupportTicket, tid))

    def test_app_lock_elements(self):
        c = T.client("h")
        r = c.get("/home")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn('id="unit-app-lock"', page)
        self.assertIn('id="unit-pin-setup"', page)
        self.assertIn('data-lock-app', page)
        self.assertIn('data-change-pin', page)


if __name__ == "__main__":
    unittest.main()

