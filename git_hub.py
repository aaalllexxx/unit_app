"""
Интеграция с GitHub для сотрудников лаборатории.

* Привязка/отвязка аккаунта GitHub (логин и опциональный токен/PAT).
* Чтение репозиториев сотрудника через GitHub API.
* Стягивание репозитория в мастерскую (/desk):
  создаёт на рабочем столе заметку (README), чек-лист (issues) и ссылку на репозиторий.
"""

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import models as M

GITHUB_API = "https://api.github.com"
USER_AGENT = "UNIT-Portal/1.0"


def get_git_info(member: M.Member) -> Dict[str, Any]:
    contacts = member.contacts or {}
    username = str(contacts.get("github") or "").strip().lstrip("@")
    has_token = bool(contacts.get("github_token"))
    return {
        "username": username,
        "linked": bool(username),
        "has_token": has_token,
    }


from sqlalchemy.orm.attributes import flag_modified

def link_account(s, member_id: str, username: str, token: str = "") -> bool:
    member = s.get(M.Member, member_id)
    if not member:
        return False
    contacts = dict(member.contacts or {})
    contacts["github"] = (username or "").strip().lstrip("@")[:80]
    if token:
        contacts["github_token"] = token.strip()[:150]
    elif "github_token" in contacts and not token:
        pass
    member.contacts = contacts
    flag_modified(member, "contacts")
    s.commit()
    return True


def unlink_account(s, member_id: str) -> bool:
    member = s.get(M.Member, member_id)
    if not member:
        return False
    contacts = dict(member.contacts or {})
    contacts.pop("github", None)
    contacts.pop("github_token", None)
    member.contacts = contacts
    flag_modified(member, "contacts")
    s.commit()
    return True


def _github_request(url: str, token: str = "") -> Any:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/vnd.github.v3+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def list_repos(username: str, token: str = "") -> List[Dict[str, Any]]:
    username = (username or "").strip().lstrip("@")
    if not username and not token:
        return []
    url = f"{GITHUB_API}/user/repos?sort=updated&per_page=30" if token else f"{GITHUB_API}/users/{username}/repos?sort=updated&per_page=30"
    try:
        raw = _github_request(url, token)
        if not isinstance(raw, list):
            return []
        repos = []
        for r in raw:
            repos.append({
                "name": r.get("name", ""),
                "full_name": r.get("full_name", ""),
                "description": r.get("description") or "",
                "html_url": r.get("html_url", ""),
                "language": r.get("language") or "",
                "stars": r.get("stargazers_count", 0),
                "updated_at": (r.get("updated_at") or "")[:10],
                "private": bool(r.get("private")),
            })
        return repos
    except (urllib.error.URLError, OSError, ValueError) as err:
        print(f"[git] ошибка загрузки репозиториев для {username}: {err}", flush=True)
        return []


def fetch_repo_details(full_name: str, token: str = "") -> Dict[str, Any]:
    full_name = full_name.strip()
    if "/" not in full_name:
        return {}
    owner, repo = full_name.split("/", 1)

    desc = ""
    html_url = f"https://github.com/{full_name}"
    try:
        info = _github_request(f"{GITHUB_API}/repos/{owner}/{repo}", token)
        desc = info.get("description") or ""
        html_url = info.get("html_url") or html_url
    except Exception:
        pass

    readme_text = ""
    try:
        readme_info = _github_request(f"{GITHUB_API}/repos/{owner}/{repo}/readme", token)
        content_b64 = readme_info.get("content", "")
        if content_b64:
            readme_text = base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception:
        pass

    issues = []
    try:
        raw_issues = _github_request(f"{GITHUB_API}/repos/{owner}/{repo}/issues?state=open&per_page=20", token)
        if isinstance(raw_issues, list):
            for it in raw_issues:
                if "pull_request" in it:
                    continue
                issues.append({
                    "number": it.get("number"),
                    "title": it.get("title", ""),
                    "url": it.get("html_url", "")
                })
    except Exception:
        pass

    return {
        "name": repo,
        "owner": owner,
        "full_name": full_name,
        "description": desc,
        "html_url": html_url,
        "readme": readme_text,
        "issues": issues,
    }


def import_repo_to_desk(s, member_id: str, repo_details: Dict[str, Any], project_id: Optional[int] = None) -> List[M.DeskItem]:
    created = []
    full_name = repo_details.get("full_name") or repo_details.get("name", "Репозиторий")
    repo_name = repo_details.get("name") or full_name
    html_url = repo_details.get("html_url", "")

    # 1. Ссылка на репозиторий
    link_item = M.DeskItem(
        member_id=member_id,
        kind="link",
        title=f"Репозиторий {repo_name}",
        body=repo_details.get("description") or "",
        data={"url": html_url},
        project_id=project_id,
        color="blue",
    )
    s.add(link_item)
    created.append(link_item)

    # 2. README как заметка
    readme = (repo_details.get("readme") or "").strip()
    if readme:
        if len(readme) > 50000:
            readme = readme[:50000] + "\n\n... (обрезано)"
        note_item = M.DeskItem(
            member_id=member_id,
            kind="note",
            title=f"README · {repo_name}",
            body=readme,
            data={},
            project_id=project_id,
            color="teal",
        )
        s.add(note_item)
        created.append(note_item)

    # 3. Открытые issues как чек-лист задач
    issues = repo_details.get("issues") or []
    if issues:
        checklist_items = [{"text": f"#{it['number']} {it['title']}"[:280], "done": False} for it in issues[:50]]
        check_item = M.DeskItem(
            member_id=member_id,
            kind="checklist",
            title=f"Задачи GitHub · {repo_name}",
            body="",
            data={"items": checklist_items},
            project_id=project_id,
            color="amber",
        )
        s.add(check_item)
        created.append(check_item)

    s.commit()
    return created
