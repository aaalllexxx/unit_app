"""Все страницы портала открываются: у руководителя, заказчика и гостя; PWA на месте."""

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

os.environ.update(LAB_DATA=tempfile.mkdtemp(), LAB_SECURE="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models as M  # noqa: E402
from app import app  # noqa: E402


def _seed():
    s = M.Session()
    s.add_all([M.Member(id="h", name="Руководитель Лаборатории", role="head", direction_id=2),
               M.Member(id="c", name="ООО Заказчик", role="customer")])
    s.flush()
    p = M.Project(title="Портал", summary="Тест", direction_id=2, lead_id="h", public=True, due=date.today() + timedelta(days=9))
    s.add(p)
    s.flush()
    s.add_all([M.ProjectMember(project_id=p.id, member_id="h", role=""), M.ProjectMember(project_id=p.id, member_id="c", role=""),
               M.Part(project_id=p.id, title="API", phase="build", phase_progress=40),
               M.Task(project_id=p.id, title="Схема", column="doing", assignee_id="h"),
               M.TimeEntry(member_id="h", project_id=p.id, hours=2),
               M.Update(project_id=p.id, author_id="h", text="Готово", for_customer=True)])
    s.commit()
    return p.id


PID = _seed()


def client(mid=None):
    c = app.test_client()
    if mid:
        with c.session_transaction() as sess:
            sess["mid"] = mid
    return c


class PagesTest(unittest.TestCase):
    def test_head_sees_everything(self):
        c = client("h")
        for url in ["/home", "/projects", f"/p/{PID}", "/people", "/people/h", "/d/dev", "/time", "/admin"]:
            self.assertEqual(c.get(url).status_code, 200, url)

    def test_customer_and_guest(self):
        c = client("c")
        self.assertEqual(c.get(f"/p/{PID}").status_code, 200)
        self.assertEqual(c.get("/people").status_code, 403)
        g = client()
        self.assertEqual(g.get("/").status_code, 200)
        self.assertEqual(g.get("/home").status_code, 302)

    def test_pwa(self):
        g = client()
        manifest = g.get("/manifest.webmanifest")
        self.assertEqual(manifest.mimetype, "application/manifest+json")
        self.assertEqual(manifest.get_json()["display"], "standalone")
        self.assertEqual(g.get("/sw.js").status_code, 200)
        self.assertEqual(g.get("/offline").status_code, 200)
        for icon in ("icon-192.png", "icon-512.png", "icon-maskable.png", "apple-touch-icon.png"):
            self.assertEqual(g.get("/static/" + icon).status_code, 200, icon)


if __name__ == "__main__":
    unittest.main()
