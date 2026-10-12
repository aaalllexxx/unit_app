"""
ИИ лаборатории: настройки, вызов модели и учёт расхода.

Модель подключается по протоколу OpenAI (как в школе UNIT): руководитель в
«Обзоре → ИИ» указывает адрес, ключ и модель. Ключ хранится в базе портала и
наружу не отдаётся — в интерфейсе только маска. Там же — кому доступен ИИ и
сколько запросов в день на человека.

Ответ можно получать потоком (stream): чат показывает текст по мере набора.
"""

import http.client
import json
import time
import urllib.error
import urllib.request
from datetime import date
from typing import Iterator, List, Optional

from sqlalchemy import select

import models as M

KEYS = {"base_url": "ai_base_url", "api_key": "ai_api_key", "model": "ai_model", "fast_model": "ai_fast_model",
        "roles": "ai_roles", "daily": "ai_daily", "prompt": "ai_prompt"}
DEFAULT_ROLES = ["head", "lead", "member"]
DEFAULT_DAILY = 150
TIMEOUT = 120
RETRIES = 2


class AiError(Exception):
    """Понятная человеку ошибка обращения к модели."""


# ─── Настройки ──────────────────────────────────────────────────────────────
def _get(s, key: str, default: str = "") -> str:
    row = s.get(M.Setting, key)
    return row.value if row and row.value is not None else default


def _set(s, key: str, value: str) -> None:
    row = s.get(M.Setting, key) or M.Setting(key=key)
    row.value = value
    s.add(row)


def settings(s) -> dict:
    try:
        roles = json.loads(_get(s, KEYS["roles"], "")) or DEFAULT_ROLES
    except ValueError:
        roles = DEFAULT_ROLES
    key = _get(s, KEYS["api_key"])
    try:
        daily = int(_get(s, KEYS["daily"], str(DEFAULT_DAILY)) or DEFAULT_DAILY)
    except ValueError:
        daily = DEFAULT_DAILY
    return {"base_url": _get(s, KEYS["base_url"]), "model": _get(s, KEYS["model"]),
            "fast_model": _get(s, KEYS["fast_model"]), "roles": roles, "daily": daily,
            "prompt": _get(s, KEYS["prompt"]), "key_set": bool(key),
            "masked": (key[:3] + "…" + key[-4:]) if len(key) > 10 else ("…" if key else "")}


def save(s, base_url: str, model: str, fast_model: str, roles: List[str], daily: int, prompt: str,
         api_key: Optional[str] = None) -> None:
    """Пустой ключ оставляет прежний: в форме — маска, стирать ключ при каждом сохранении незачем."""
    _set(s, KEYS["base_url"], (base_url or "").strip().rstrip("/"))
    _set(s, KEYS["model"], (model or "").strip())
    _set(s, KEYS["fast_model"], (fast_model or "").strip())
    _set(s, KEYS["roles"], json.dumps([r for r in roles if r in M.ROLES and r != "pending"]))
    _set(s, KEYS["daily"], str(max(0, min(5000, int(daily or 0)))))
    _set(s, KEYS["prompt"], (prompt or "").strip()[:4000])
    if api_key and api_key.strip():
        _set(s, KEYS["api_key"], api_key.strip())
    s.commit()


def forget_key(s) -> None:
    _set(s, KEYS["api_key"], "")
    s.commit()


def configured(s) -> bool:
    return bool(_get(s, KEYS["base_url"]) and _get(s, KEYS["model"]))


def allowed(s, member) -> bool:
    """ИИ включён и человеку можно им пользоваться (право ai.use)."""
    import access
    return configured(s) and access.can(member, "ai.use")


def used_today(s, member_id: str) -> int:
    row = s.scalar(select(M.AiUsage).where(M.AiUsage.member_id == member_id, M.AiUsage.day == date.today()))
    return row.requests if row else 0


def left_today(s, member_id: str) -> int:
    daily = settings(s)["daily"]
    return max(0, daily - used_today(s, member_id)) if daily else 10 ** 6


def count(s, member_id: str, tokens: int = 0) -> None:
    row = s.scalar(select(M.AiUsage).where(M.AiUsage.member_id == member_id, M.AiUsage.day == date.today()))
    if row is None:
        row = M.AiUsage(member_id=member_id, day=date.today(), requests=0, tokens=0)
        s.add(row)
    row.requests += 1
    row.tokens += int(tokens or 0)
    s.commit()


def fast_models(s) -> List[str]:
    raw = _get(s, KEYS["fast_model"]).strip()
    if not raw:
        return []
    return [m.strip() for m in raw.split(",") if m.strip()]


def fetch_models(s, base_url: str = "", api_key: str = "") -> dict:
    """Запрашивает /models у агрегатора и отбирает модели для чата и быстрые модели для команды агента."""
    base = (base_url or _get(s, KEYS["base_url"])).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[:-len("/chat/completions")].rstrip("/")
    if not base:
        raise AiError("Не указан адрес API агрегатора")
    url = base if base.endswith("/models") else base + "/models"
    key = (api_key if api_key is not None and api_key != "" else _get(s, KEYS["api_key"])).strip()
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as err:
        raise AiError(_http_error(err)) from err
    except Exception as err:
        raise AiError(f"Не удалось связаться с API агрегатора: {err}") from err

    raw_list = data.get("data") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        raw_list = data.get("models") if isinstance(data, dict) else []
    if not isinstance(raw_list, list):
        raw_list = []

    skip_keywords = ("embed", "whisper", "tts", "audio", "dall", "flux", "veo", "sora", "moderation", "vision-only", "guard")
    chat_models = []
    for item in raw_list:
        mid = item.get("id") if isinstance(item, dict) else str(item)
        if not mid or not isinstance(mid, str):
            continue
        mid_lower = mid.lower()
        if any(sk in mid_lower for sk in skip_keywords):
            continue
        chat_models.append(mid)

    fast_keywords = ("flash", "mini", "haiku", "deepseek-chat", "lite", "small", "turbo", "8b", "7b", "fast")
    fast_team = [m for m in chat_models if any(k in m.lower() for k in fast_keywords)]
    if not fast_team:
        fast_team = chat_models[:3]

    return {
        "models": chat_models,
        "fast_team": fast_team[:4],
        "fast_str": ", ".join(fast_team[:4])
    }


# ─── Вызов ──────────────────────────────────────────────────────────────────
def _request(s, messages: List[dict], stream: bool, fast: bool, max_tokens: int, json_only: bool):
    base = _get(s, KEYS["base_url"]).rstrip("/")
    if not base or not _get(s, KEYS["model"]):
        raise AiError("ИИ не настроен — руководитель включает его в «Обзор → ИИ»")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    candidates = fast_models(s)
    fast_picked = candidates[0] if candidates else ""
    model = (fast_picked if fast else "") or _get(s, KEYS["model"])
    payload = {"model": model, "messages": messages, "temperature": 0.4, "max_tokens": max(512, max_tokens)}
    if json_only:
        payload["response_format"] = {"type": "json_object"}
    if stream:
        payload.update(stream=True, stream_options={"include_usage": True})
    headers = {"Content-Type": "application/json"}
    key = _get(s, KEYS["api_key"])
    if key:
        headers["Authorization"] = "Bearer " + key
    return urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")


def _open(req, timeout: int):
    for attempt in range(RETRIES + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise AiError(_http_error(error)) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise AiError("Не удалось связаться с моделью — попробуйте через минуту") from error
    raise AiError("Модель не ответила")


def _http_error(error) -> str:
    try:
        body = json.loads(error.read().decode("utf-8", "replace"))
        detail = (body.get("error") or {}).get("message") or body.get("message") or ""
    except Exception:  # noqa: BLE001
        detail = ""
    notes = {400: "Сервис не принял запрос — чаще всего дело в названии модели", 401: "Ключ не принят",
             403: "У ключа нет прав на эту модель", 404: "Адрес или модель не найдены",
             429: "Слишком много запросов или закончилась квота — подождите минуту",
             502: "Шлюз не дозвонился до модели", 503: "Модель перегружена — попробуйте через минуту"}
    note = notes.get(error.code, f"Сервис ответил ошибкой {error.code}")
    return note + (f". Ответ сервиса: {detail[:200]}" if detail else "")


def _text(body: dict) -> str:
    try:
        message = body["choices"][0].get("message") or {}
    except (KeyError, IndexError, TypeError) as error:
        raise AiError("Модель вернула ответ незнакомого вида") from error
    raw = message.get("content")
    if isinstance(raw, list):
        raw = "".join(p.get("text", "") for p in raw if isinstance(p, dict))
    text = (raw or "").strip() or str(message.get("reasoning_content") or "").strip()
    if not text:
        raise AiError("Модель вернула пустой ответ — проверьте модель в настройках")
    return text


def ask(s, messages: List[dict], *, fast: bool = False, max_tokens: int = 1600, json_only: bool = False) -> tuple:
    """Ответ целиком. → (текст, токены)."""
    with _open(_request(s, messages, False, fast, max_tokens, json_only), TIMEOUT) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    usage = body.get("usage") or {}
    return _text(body), int(usage.get("total_tokens") or 0)


def stream(s, messages: List[dict], *, max_tokens: int = 2400) -> Iterator[dict]:
    """Ответ потоком: {"delta": кусок} … {"done": True, "tokens": n}."""
    response = _open(_request(s, messages, True, False, max_tokens, False), TIMEOUT)
    tokens = 0
    got = False
    try:
        if "event-stream" not in (response.headers.get("Content-Type") or ""):
            body = json.loads(response.read().decode("utf-8", "replace"))
            yield {"delta": _text(body)}
            yield {"done": True, "tokens": int((body.get("usage") or {}).get("total_tokens") or 0)}
            return
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if isinstance(chunk.get("error"), dict):
                raise AiError("Модель прервала ответ: " + str(chunk["error"].get("message") or "ошибка"))
            if chunk.get("usage"):
                tokens = int(chunk["usage"].get("total_tokens") or 0)
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if not piece and choice.get("message"):
                    piece = choice["message"].get("content")
                if not piece and choice.get("text"):
                    piece = choice["text"]
                if isinstance(piece, list):
                    piece = "".join(p.get("text", "") for p in piece if isinstance(p, dict))
                if isinstance(piece, str) and piece:
                    got = True
                    yield {"delta": piece}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thought")
                if isinstance(reasoning, str) and reasoning:
                    got = True
                    yield {"reasoning": reasoning}
    finally:
        response.close()
    if not got:
        # Fallback to non-streaming ask() if stream returned no content
        try:
            full_text, t = ask(s, messages, max_tokens=max_tokens)
            if full_text:
                yield {"delta": full_text}
                yield {"done": True, "tokens": t}
                return
        except Exception:
            pass
        raise AiError("Модель вернула пустой ответ — проверьте модель в настройках")
    yield {"done": True, "tokens": tokens}


def check(s) -> str:
    text, _ = ask(s, [{"role": "user", "content": "Ответь одним словом: готов"}], max_tokens=64)
    return text[:80]
