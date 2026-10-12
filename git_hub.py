"""
Интеграция с GitHub для сотрудников лаборатории.

* Привязка/отвязка аккаунта GitHub (логин и опциональный токен/PAT).
* Чтение репозиториев сотрудника через GitHub API с проверкой ошибок и лимитов.
* Стягивание репозитория в мастерскую (/desk):
  создаёт на рабочем столе заметку (README), чек-лист (issues) и ссылку на репозиторий.
"""

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm.attributes import flag_modified
import models as M

GITHUB_API = "https://api.github.com"
USER_AGENT = "UNIT-Portal/1.0"


def clean_username(username: str) -> str:
    s = str(username or "").strip()
    s = re.sub(r"^https?://(?:www\.)?github\.com/", "", s, flags=re.IGNORECASE)
    s = s.lstrip("@").strip("/").split("/")[0].strip()
    return s[:80]


def clean_token(token: str) -> str:
    t = str(token or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    elif t.lower().startswith("token "):
        t = t[6:].strip()
    return t[:150]


def clean_repo_name(name: str) -> str:
    s = str(name or "").strip()
    s = re.sub(r"^https?://(?:www\.)?github\.com/", "", s, flags=re.IGNORECASE)
    parts = s.strip("/").split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return s


def get_git_info(member: M.Member) -> Dict[str, Any]:
    contacts = member.contacts or {}
    username = clean_username(contacts.get("github") or "")
    has_token = bool(contacts.get("github_token"))
    return {
        "username": username,
        "linked": bool(username),
        "has_token": has_token,
    }


def _github_request(url: str, token: str = "") -> Any:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/vnd.github.v3+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return json.loads(data or "{}")
    except urllib.error.HTTPError as err:
        body_msg = ""
        try:
            body_text = err.read().decode("utf-8", errors="replace")
            err_json = json.loads(body_text)
            body_msg = err_json.get("message", "")
        except Exception:
            body_msg = ""
        if err.code == 401:
            raise ValueError("Неверный токен GitHub (Bad credentials). Проверьте или обновите токен.")
        elif err.code == 403:
            if "rate limit" in body_msg.lower():
                raise ValueError("Превышен лимит запросов GitHub API. Укажите Personal Access Token (PAT).")
            raise ValueError(f"Доступ к GitHub ограничен ({body_msg or err.reason}).")
        elif err.code == 404:
            raise ValueError("Пользователь или репозиторий не найден на GitHub.")
        raise ValueError(f"Ошибка GitHub API ({err.code}): {body_msg or err.reason}")
    except (urllib.error.URLError, OSError) as err:
        raise ValueError(f"Не удалось связаться с сервером GitHub: {err}")


def link_account(s, member_id: str, username: str, token: str = "") -> bool:
    member = s.get(M.Member, member_id)
    if not member:
        return False
    u = clean_username(username)
    if not u:
        raise ValueError("Укажите логин GitHub")
    t = clean_token(token)

    contacts = dict(member.contacts or {})
    contacts["github"] = u
    if t:
        contacts["github_token"] = t
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


def list_repos(username: str, token: str = "") -> Tuple[List[Dict[str, Any]], Optional[str]]:
    u = clean_username(username)
    t = clean_token(token)
    if not u and not t:
        return [], "GitHub аккаунт не указан"
    url = f"{GITHUB_API}/user/repos?sort=updated&per_page=30" if t else f"{GITHUB_API}/users/{u}/repos?sort=updated&per_page=30"
    try:
        raw = _github_request(url, t)
        if not isinstance(raw, list):
            return [], None
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
        return repos, None
    except ValueError as err:
        return [], str(err)
    except Exception as err:
        return [], f"Ошибка загрузки репозиториев: {err}"


def fetch_repo_details(full_name: str, token: str = "") -> Dict[str, Any]:
    full_name = clean_repo_name(full_name)
    if "/" not in full_name:
        return {}
    owner, repo = full_name.split("/", 1)
    t = clean_token(token)

    desc = ""
    html_url = f"https://github.com/{full_name}"
    try:
        info = _github_request(f"{GITHUB_API}/repos/{owner}/{repo}", t)
        desc = info.get("description") or ""
        html_url = info.get("html_url") or html_url
    except Exception:
        pass

    readme_text = ""
    try:
        readme_info = _github_request(f"{GITHUB_API}/repos/{owner}/{repo}/readme", t)
        content_b64 = readme_info.get("content", "")
        if content_b64:
            readme_text = base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception:
        pass

    issues = []
    try:
        raw_issues = _github_request(f"{GITHUB_API}/repos/{owner}/{repo}/issues?state=open&per_page=20", t)
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
