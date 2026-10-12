"""
ИИ лаборатории: подключение, каталог моделей с ценами, вызов модели и расход.

Модель подключается по протоколу OpenAI (как в школе UNIT): руководитель в
«ИИ лаборатории» указывает адрес агрегатора, ключ, основную модель агента и его
команду (дешёвые модели для поиска и черновиков). Ключ хранится в базе портала и
наружу не отдаётся — в интерфейсе только маска.

Цены берутся у агрегатора (/models отдаёт цену за миллион токенов), по ним
считается расход каждого обращения в долларах (таблица ai_spend). Лимиты — в
«Доступах»: у тегов и у людей (ai_day_usd, ai_month_usd). Исчерпан лимит —
агент продолжает работу на дешёвой модели: дешёвые лимитом не ограничены.
"""

import http.client
import json
import time
import urllib.error
import urllib.request
from datetime import date
from typing import Dict, List, Optional

from sqlalchemy import func, select

import models as M

KEYS = {"base_url": "ai_base_url", "api_key": "ai_api_key", "model": "ai_model", "team": "ai_fast_model",
        "prompt": "ai_prompt", "reasoning": "ai_reasoning"}
REASONING = {"none": "Без размышлений", "low": "Коротко", "medium": "Обычно", "high": "Глубоко"}
TIMEOUT = 120
RETRIES = 2

# Типичное обращение агента: правила, данные, история и ответ — по ним «≈ цена запроса»
EST_IN, EST_OUT = 9000, 1200
# Дешёвая модель — минимум втрое дешевле основной; такие лимитом не ограничены
CHEAP_SHARE = 1 / 3
NOT_CHAT = ("embed", "whisper", "tts", "audio", "dall", "flux", "veo", "sora", "moderation", "image", "banana",
            "imagine", "guard", "rerank", "midjourney", "recraft", "ideogram", "seedream", "kandinsky", "video")
FAST_WORDS = ("flash", "mini", "haiku", "lite", "nano", "fast", "turbo", "small", "instant", "8b", "7b")


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
    key = _get(s, KEYS["api_key"])
    reasoning = _get(s, KEYS["reasoning"], "low")
    return {"base_url": _get(s, KEYS["base_url"]), "model": _get(s, KEYS["model"]),
            "team": team_ids(s), "team_raw": _get(s, KEYS["team"]), "prompt": _get(s, KEYS["prompt"]),
            "reasoning": reasoning if reasoning in REASONING else "low", "key_set": bool(key),
            "masked": (key[:3] + "…" + key[-4:]) if len(key) > 10 else ("…" if key else "")}


def save(s, base_url: str, model: str, team: str, prompt: str, reasoning: str = "low",
         api_key: Optional[str] = None) -> None:
    """Пустой ключ оставляет прежний: в форме — маска, стирать ключ при каждом сохранении незачем."""
    _set(s, KEYS["base_url"], (base_url or "").strip().rstrip("/"))
    _set(s, KEYS["model"], (model or "").strip())
    _set(s, KEYS["team"], ", ".join(m.strip() for m in (team or "").replace("\n", ",").split(",") if m.strip()))
    _set(s, KEYS["prompt"], (prompt or "").strip()[:4000])
    _set(s, KEYS["reasoning"], reasoning if reasoning in REASONING else "low")
    if api_key and api_key.strip():
        _set(s, KEYS["api_key"], api_key.strip())
    s.commit()
    _CATALOG["at"] = 0.0


def forget_key(s) -> None:
    _set(s, KEYS["api_key"], "")
    s.commit()


def configured(s) -> bool:
    return bool(_get(s, KEYS["base_url"]) and _get(s, KEYS["model"]))


def allowed(s, member) -> bool:
    """ИИ включён и человеку можно им пользоваться (право ai.use)."""
    import access
    return configured(s) and access.can(member, "ai.use")


def main_model(s) -> str:
    return _get(s, KEYS["model"])


def team_ids(s) -> List[str]:
    """Команда агента, заданная руководителем (модели через запятую)."""
    return [m.strip() for m in _get(s, KEYS["team"]).split(",") if m.strip()]


# ─── Каталог агрегатора и цены ──────────────────────────────────────────────
_CATALOG = {"at": 0.0, "base": "", "rows": {}}


def _base(s) -> str:
    base = _get(s, KEYS["base_url"]).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    return base


def catalog(s, fresh: bool = False) -> Dict[str, dict]:
    """Модели агрегатора с ценами: {id: pricing}. Кешируется на 10 минут."""
    base = _base(s)
    if not fresh and _CATALOG["base"] == base and time.time() - _CATALOG["at"] < 600:
        return _CATALOG["rows"]
    rows: Dict[str, dict] = {}
    if base:
        key = _get(s, KEYS["api_key"])
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        try:
            with urllib.request.urlopen(urllib.request.Request(base + "/models", headers=headers), timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            for row in (body.get("data") if isinstance(body, dict) else body) or []:
                if isinstance(row, dict) and row.get("id"):
                    rows[str(row["id"])] = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
                elif isinstance(row, str):
                    rows[row] = {}
        except Exception:                                   # noqa: BLE001 — нет каталога: цены неизвестны
            rows = dict(_CATALOG["rows"]) if _CATALOG["base"] == base else {}
    _CATALOG.update(at=time.time(), base=base, rows=rows)
    return rows


def chat_models(s) -> List[str]:
    """Текстовые модели агрегатора; основная и команда — всегда в списке."""
    found = [m for m in catalog(s) if not any(w in m.lower() for w in NOT_CHAT)]
    for extra in reversed([main_model(s)] + team_ids(s)):
        if extra and extra not in found:
            found.insert(0, extra)
    return found


def price_of(s, model: str) -> dict:
    """Цена модели, $: {"in","out"} за миллион токенов или {"per_request"}; {} — неизвестна."""
    p = catalog(s).get(model) or {}
    if p.get("billing_mode") == "per_request":
        return {"per_request": float(p.get("price_per_request_usd") or 0)}
    pin = p.get("price_input_per_1m_usd", p.get("input"))
    pout = p.get("price_output_per_1m_usd", p.get("output"))
    if pin is None and p.get("prompt") is not None:                       # цена за токен (как у OpenRouter)
        try:
            pin, pout = float(p["prompt"]) * 1e6, float(p.get("completion") or 0) * 1e6
        except (TypeError, ValueError):
            pin = pout = None
    if pin is None and pout is None:
        return {}
    try:
        return {"in": float(pin or 0), "out": float(pout or 0)}
    except (TypeError, ValueError):
        return {}


def estimate(s, model: str) -> Optional[float]:
    """Сколько примерно стоит одно обращение агента к модели, $."""
    p = price_of(s, model)
    if "per_request" in p:
        return p["per_request"]
    if p:
        return (EST_IN * p["in"] + EST_OUT * p["out"]) / 1_000_000
    return None


def cost_of(s, model: str, usage: dict, sent_chars: int = 0, got_chars: int = 0) -> tuple:
    """Стоимость обращения по токенам сервиса; нет токенов — по длине (≈3 знака на токен). → ($, токены)."""
    p = price_of(s, model)
    if "per_request" in p:
        return p["per_request"], int((usage or {}).get("total_tokens") or 0)
    pin = (usage or {}).get("prompt_tokens")
    pout = (usage or {}).get("completion_tokens")
    if pin is None:
        pin = sent_chars // 3
    if pout is None:
        pout = got_chars // 3
    return (int(pin) * p.get("in", 0) + int(pout) * p.get("out", 0)) / 1_000_000, int(pin) + int(pout)


def is_cheap(s, model: str) -> bool:
    """Дешёвая — заметно дешевле основной модели агента (сама основная дешёвой не бывает).
    Цены неизвестны — судим по названию: flash, mini, haiku…"""
    main = main_model(s)
    if not model or model == main:
        return False
    est, top = estimate(s, model), estimate(s, main)
    if est is not None and top:
        return est <= top * CHEAP_SHARE
    return any(w in model.lower() for w in FAST_WORDS)


def cheapest(s, among: Optional[List[str]] = None) -> str:
    """Самая дешёвая модель: из команды (или из списка), иначе — основная."""
    pool = [m for m in (among if among is not None else [r["id"] for r in team(s)]) if m]
    if not pool:
        return main_model(s)
    return min(pool, key=lambda m: (estimate(s, m) is None, estimate(s, m) or 0, m))


def team(s) -> List[dict]:
    """Команда агента: [{id, name, price, cheap}] от дешёвых к дорогим. Не задана — быстрые модели каталога."""
    ids = team_ids(s)
    if not ids:
        ids = [m for m in chat_models(s) if any(w in m.lower() for w in FAST_WORDS)][:4]
    ids = [m for m in ids if m != main_model(s)]
    rows = [{"id": m, "name": pretty(m), "estimate": estimate(s, m), "price": money(estimate(s, m)),
             "cheap": is_cheap(s, m)} for m in ids]
    rows.sort(key=lambda r: (r["estimate"] is None, r["estimate"] or 0, r["id"]))
    return rows


_BRANDS = {"claude": "Claude", "gpt": "GPT", "deepseek": "DeepSeek", "gemini": "Gemini", "grok": "Grok",
           "qwen": "Qwen", "llama": "Llama", "mistral": "Mistral", "kimi": "Kimi", "glm": "GLM"}


def pretty(mid: str) -> str:
    """claude-opus-4.7 → «Claude Opus 4.7», deepseek-v4-flash → «DeepSeek V4 Flash»."""
    parts = [p for p in str(mid or "").split("/")[-1].replace("_", "-").split("-") if p]
    words = []
    for p in parts:
        low = p.lower()
        if low in _BRANDS:
            words.append(_BRANDS[low])
        elif low[:1] == "v" and low[1:].replace(".", "").isdigit():
            words.append(p.upper())
        else:
            words.append(p[:1].upper() + p[1:])
    return " ".join(words) or str(mid or "")


def money(usd: Optional[float]) -> str:
    if usd is None:
        return ""
    if usd < 0.001:
        return "< $0.001" if usd > 0 else "$0"
    if usd < 0.1:
        return f"${usd:.3f}"
    return f"${usd:.2f}"


def models_menu(s, pick: bool = True) -> List[dict]:
    """Модели для выбора в чате: основная первой, дальше — от дешёвых к дорогим. Без права выбора — только основная."""
    main = main_model(s)
    ids = chat_models(s) if pick else [main]
    out = []
    for mid in ids:
        est = estimate(s, mid)
        out.append({"id": mid, "name": pretty(mid), "default": mid == main, "estimate": est, "label": money(est),
                    "cheap": is_cheap(s, mid)})
    out.sort(key=lambda m: (not m["default"], m["estimate"] is None, m["estimate"] or 0, m["id"]))
    return out


# ─── Расход и лимиты ────────────────────────────────────────────────────────
def charge(s, member_id: str, model: str, cost: float, tokens: int = 0) -> None:
    row = s.scalar(select(M.AiSpend).where(M.AiSpend.member_id == member_id, M.AiSpend.day == date.today(),
                                           M.AiSpend.model == (model or "")[:80]))
    if row is None:
        row = M.AiSpend(member_id=member_id, day=date.today(), model=(model or "")[:80], requests=0, tokens=0, cost=0.0)
        s.add(row)
    row.requests += 1
    row.tokens += int(tokens or 0)
    row.cost += float(cost or 0)
    s.commit()


def spend(s, member_id: str) -> dict:
    """Расход на ИИ за сегодня и за месяц, $."""
    today = date.today()
    day = s.scalar(select(func.coalesce(func.sum(M.AiSpend.cost), 0)).where(
        M.AiSpend.member_id == member_id, M.AiSpend.day == today)) or 0
    month = s.scalar(select(func.coalesce(func.sum(M.AiSpend.cost), 0)).where(
        M.AiSpend.member_id == member_id, M.AiSpend.day >= today.replace(day=1))) or 0
    return {"day": float(day), "month": float(month)}


def budget(s, member) -> dict:
    """Лимиты человека и сколько потрачено: кольца для настроек и проверка перед обращением."""
    used = spend(s, member.id)
    limits = getattr(member, "limits", None) or {}
    rings = []
    over = False
    for key, label, spent in (("ai_day_usd", "сегодня", used["day"]), ("ai_month_usd", "за месяц", used["month"])):
        limit = float(limits.get(key) or 0)
        part = min(100, round(spent / limit * 100)) if limit else 0
        if limit and spent >= limit:
            over = True
        rings.append({"key": key, "label": label, "spent": spent, "limit": limit, "percent": part,
                      "spent_label": money(spent) if spent else "$0", "limit_label": money(limit) if limit else ""})
    return {"rings": rings, "over": over, "day": used["day"], "month": used["month"]}


# ─── Вызов ──────────────────────────────────────────────────────────────────
def _headers(s) -> dict:
    headers = {"Content-Type": "application/json"}
    key = _get(s, KEYS["api_key"])
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def _url(s) -> str:
    base = _get(s, KEYS["base_url"]).rstrip("/")
    if not base or not main_model(s):
        raise AiError("ИИ не настроен — руководитель включает его в «ИИ лаборатории»")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


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
    return (raw or "").strip()


def call(s, messages: List[dict], model: str = "", *, reasoning: str = "", max_tokens: int = 4000,
         on_delta=None, timeout: int = 180, stop=None) -> dict:
    """Один запрос к модели потоком. on_delta({"text", "reasoning"}) — следить за ответом по ходу;
    stop (threading.Event) — оборвать ответ на полуслове («Стоп» в чате).
    → {"text", "reasoning", "usage", "model", "cost", "tokens", "stopped"}."""
    model = model or main_model(s)
    payload = {"model": model, "messages": messages, "max_tokens": max(512, max_tokens), "temperature": 0.3,
               "stream": True, "stream_options": {"include_usage": True}}
    if reasoning in ("none", "low", "medium", "high"):
        payload["reasoning_effort"] = reasoning
    req = urllib.request.Request(_url(s), data=json.dumps(payload).encode(), headers=_headers(s), method="POST")
    response = _open(req, timeout)
    text, thinking, usage = [], [], {}
    told = 0.0
    try:
        if "event-stream" not in (response.headers.get("Content-Type") or ""):
            body = json.loads(response.read().decode("utf-8", "replace"))
            text = [_text(body)]
            usage = body.get("usage") or {}
        else:
            for raw in response:
                if stop is not None and stop.is_set():
                    break
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
                if not isinstance(chunk, dict):
                    continue
                if isinstance(chunk.get("error"), dict):
                    raise AiError("Модель прервала ответ: " + str(chunk["error"].get("message") or "ошибка"))
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, list):
                        piece = "".join(p.get("text", "") for p in piece if isinstance(p, dict))
                    if isinstance(piece, str) and piece:
                        text.append(piece)
                    thought = delta.get("reasoning_content") or delta.get("reasoning")
                    if isinstance(thought, str) and thought:
                        thinking.append(thought)
                if on_delta and time.monotonic() - told > 0.3:
                    told = time.monotonic()
                    try:
                        on_delta({"text": "".join(text), "reasoning": "".join(thinking)})
                    except Exception:                       # noqa: BLE001 — наблюдатель не роняет запрос
                        pass
    finally:
        response.close()
    out = "".join(text).strip()
    sent = sum(len(m.get("content") or "") if isinstance(m.get("content"), str) else 0 for m in messages)
    cost, tokens = cost_of(s, model, usage, sent, len(out) + len("".join(thinking)))
    return {"text": out, "reasoning": "".join(thinking), "usage": usage, "model": model, "cost": cost, "tokens": tokens,
            "stopped": bool(stop is not None and stop.is_set())}


def ask(s, messages: List[dict], *, fast: bool = False, max_tokens: int = 1600) -> tuple:
    """Ответ целиком (сводки дня, проверка подключения). fast — самой дешёвой моделью команды.
    → (текст, $, модель)."""
    model = cheapest(s) if fast else main_model(s)
    result = call(s, messages, model, reasoning="none" if fast else "", max_tokens=max_tokens)
    if not result["text"]:
        raise AiError("Модель вернула пустой ответ — проверьте модель в настройках")
    return result["text"], result["cost"], model


def check(s) -> str:
    text, _, _ = ask(s, [{"role": "user", "content": "Ответь одним словом: готов"}], max_tokens=64)
    return text[:80]
