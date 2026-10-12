"""Тесты REST API лаборатории (/api/v1) и удаления тикетов поддержки."""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_pages  # noqa: E402,F401
from app import app


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.headers = {
            "Authorization": "Bearer test-token-h",
            "Content-Type": "application/json",
        }

    def test_unauthorized(self):
        res = self.client.get("/api/v1/me")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["error"], "unauthorized")

    def test_me(self):
        res = self.client.get("/api/v1/me", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["member"]["id"], "h")
        self.assertIn("permissions", data)

    def test_projects_crud(self):
        # 1. Список проектов
        res = self.client.get("/api/v1/projects", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        projects = res.get_json()["projects"]
        self.assertGreaterEqual(len(projects), 1)

        # 2. Создание проекта
        res = self.client.post(
            "/api/v1/projects",
            headers=self.headers,
            json={"title": "Новый API Проект", "summary": "Тест создания", "priority": 1},
        )
        self.assertEqual(res.status_code, 201)
        pid = res.get_json()["project"]["id"]

        # 3. Детали проекта
        res = self.client.get(f"/api/v1/projects/{pid}", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["project"]["title"], "Новый API Проект")

        # 4. Обновление проекта
        res = self.client.patch(
            f"/api/v1/projects/{pid}",
            headers=self.headers,
            json={"title": "Обновлённый API Проект"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["project"]["title"], "Обновлённый API Проект")

    def test_tasks_crud(self):
        pid = test_pages.PID
        # 1. Создать задачу
        res = self.client.post(
            "/api/v1/tasks",
            headers=self.headers,
            json={
                "project_id": pid,
                "title": "Тестовая задача через API",
                "column": "todo",
                "checklist": [{"text": "Пункт 1", "done": False}, {"text": "Пункт 2", "done": False}],
            },
        )
        self.assertEqual(res.status_code, 201)
        tid = res.get_json()["task"]["id"]

        # 2. Список задач
        res = self.client.get(f"/api/v1/tasks?project_id={pid}", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(any(t["id"] == tid for t in res.get_json()["tasks"]))

        # 3. Перенос задачи
        res = self.client.post(f"/api/v1/tasks/{tid}/move", headers=self.headers, json={"column": "doing"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["task"]["column"], "doing")

        # 4. Отметка чек-листа
        res = self.client.post(f"/api/v1/tasks/{tid}/check", headers=self.headers, json={"index": 0, "done": True})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["task"]["checklist"][0]["done"])

        # 5. Добавить комментарий
        res = self.client.post(f"/api/v1/tasks/{tid}/comments", headers=self.headers, json={"text": "Отличный прогресс!"})
        self.assertEqual(res.status_code, 201)

        # 6. Удалить задачу
        res = self.client.delete(f"/api/v1/tasks/{tid}", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["deleted"])

    def test_time_tracking(self):
        pid = test_pages.PID
        res = self.client.post(
            "/api/v1/time",
            headers=self.headers,
            json={"project_id": pid, "hours": 3.5, "note": "Работа над API"},
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["entry"]["hours"], 3.5)

        res = self.client.get("/api/v1/time", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(len(res.get_json()["entries"]), 1)

    def test_workshop_items(self):
        res = self.client.post(
            "/api/v1/workshop/items",
            headers=self.headers,
            json={"title": "Заметка API", "kind": "note", "body": "Конспект"},
        )
        self.assertEqual(res.status_code, 201)

        res = self.client.get("/api/v1/workshop/items", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(len(res.get_json()["items"]), 1)

