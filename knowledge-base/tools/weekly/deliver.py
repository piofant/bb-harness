"""Доставка недельного обзора в Telegram (необязательно: без настройки обзор просто лежит в reviews/).

Основной канал — «Избранное» владельца через Telegram MCP (например, fast-mcp-telegram, HTTP только во внутренней
сети) инструментом send_message с chat_id="me": бот в «Избранное» писать не может. Текст — HTML с экранированием,
чтобы Telegram показал его как есть (ссылки базы относительные, разметка их испортила бы).
Запасной канал при сбое MCP — личное сообщение от своего бота владельцу: токен читается в момент отправки из
env-файла (строка TELEGRAM_BOT_TOKEN=…), не логируется и не печатается.
Настройка: KB_TG_MCP_URL (адрес MCP; пусто — без MCP), KB_TG_ENV (путь к env-файлу бота), KB_TG_CHAT_ID (кому
пишет бот), KB_TG_API (для тестов).
"""
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

MCP_URL = ""      # по умолчанию выключено: KB_TG_MCP_URL
ENV_FILE = ""     # KB_TG_ENV
CHAT_ID = ""      # KB_TG_CHAT_ID
API = "https://api.telegram.org"
CHUNK = 3800           # предел сообщения Telegram — 4096 символов


class DeliveryError(Exception):
    """sent — сколько частей точно дошло; ambiguous — неизвестно, дошла ли текущая часть (тайм-аут или битый ответ
    после tools/call): тогда запасным каналом не дублируем."""

    def __init__(self, msg, sent=0, ambiguous=False):
        super().__init__(msg)
        self.sent, self.ambiguous = sent, ambiguous


def _token():
    path = os.environ.get("KB_TG_ENV") or ENV_FILE
    if not path:
        raise DeliveryError("запасной канал не настроен (KB_TG_ENV пуст)")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError as e:
        raise DeliveryError(f"нет файла токена канала ({type(e).__name__})")
    raise DeliveryError("в файле канала нет TELEGRAM_BOT_TOKEN")


LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
WIKI_RE = re.compile(r"\[\[([^\]|#\n]+)(?:[|#][^\]\n]*)?\]\]")


def for_telegram(md):
    """Ссылки базы в Telegram не открываются: `[текст](путь)` → «текст», `[[карточка]]` → «карточка».
    Внешние http(s)-ссылки оставляем адресом в скобках. Файл обзора в базе остаётся со ссылками."""
    md = WIKI_RE.sub(lambda m: m.group(1), md)
    return LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})"
                       if m.group(2).lower().startswith(("http://", "https://", "mailto:", "//")) else m.group(1), md)


def chunks(text, n=CHUNK):
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > n and cur:
            out.append(cur)
            cur = ""
        cur += line if len(line) <= n else line[:n]
    if cur.strip():
        out.append(cur)
    return out


def _mcp_call(url, body, timeout):
    req = urllib.request.Request(url, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        if "event-stream" in (r.headers.get("Content-Type") or ""):
            datas = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
            raw = datas[-1] if datas else "{}"
    return json.loads(raw or "{}")


def _err_name(e):
    return f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__


def send_saved(text, timeout=60):
    """«Избранное» через Telegram MCP. → число сообщений. DeliveryError(sent, ambiguous) при сбое."""
    url = os.environ.get("KB_TG_MCP_URL", MCP_URL)
    if not url:
        raise DeliveryError("MCP выключен (KB_TG_MCP_URL пуст)")
    try:   # сбой до отправки — однозначный: можно запасным каналом
        _mcp_call(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "kb-weekly", "version": "1"}}},
            timeout)
    except Exception as e:  # noqa: BLE001
        raise DeliveryError(f"MCP initialize: {_err_name(e)}") from None
    sent = 0
    for i, part in enumerate(chunks(text)):
        try:
            res = _mcp_call(url, {"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {
                "name": "send_message",
                "arguments": {"chat_id": "me", "message": html.escape(part, quote=False), "parse_mode": "html"}}},
                timeout)
        except urllib.error.HTTPError as e:   # 4xx (кроме 408) — запрос отклонён до вызова инструмента
            amb = not (400 <= e.code < 500 and e.code != 408)
            raise DeliveryError(f"send_message: HTTP {e.code}" + (", статус неизвестен" if amb else "")
                                + f" (отправлено частей {sent})", sent, ambiguous=amb) from None
        except Exception as e:  # noqa: BLE001 — запрос ушёл, ответа нет: сообщение могло дойти
            raise DeliveryError(f"send_message: {_err_name(e)}, статус неизвестен (отправлено частей {sent})",
                                sent, ambiguous=True) from None
        if not isinstance(res, dict):
            raise DeliveryError(f"send_message: непонятный ответ (отправлено частей {sent})", sent, ambiguous=True)
        if res.get("error"):
            err = res["error"]
            msg = err.get("message") if isinstance(err, dict) else err
            raise DeliveryError(f"send_message: {str(msg)[:120]} (отправлено частей {sent})", sent)
        result = res.get("result")
        if not isinstance(result, dict):
            raise DeliveryError(f"send_message: непонятный ответ (отправлено частей {sent})", sent, ambiguous=True)
        payload = result.get("structuredContent")
        if payload is None:
            content = result.get("content") if isinstance(result.get("content"), list) else []
            txt = "".join(str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text")
            try:
                payload = json.loads(txt) if txt else {}
            except ValueError:
                payload = {"raw": txt[:120]}
        if not isinstance(payload, dict):
            raise DeliveryError(f"send_message: непонятный ответ (отправлено частей {sent})", sent, ambiguous=True)
        if result.get("isError") or payload.get("ok") is False:
            err = payload.get("error") or payload.get("raw") or "isError"
            raise DeliveryError(f"send_message: {str(err)[:120]} (отправлено частей {sent})", sent)
        if not payload.get("message_id"):
            raise DeliveryError(f"send_message: нет message_id (отправлено частей {sent})", sent, ambiguous=True)
        sent += 1
    return sent


def send(text, timeout=20):
    """Основной канал «Избранное»; запасной — бот владельцу, только если в «Избранное» точно ничего не ушло.
    → {"messages": n, "channel": ..., "fallback_error": ...}. DeliveryError — если не дошло или статус неизвестен."""
    text = for_telegram(text)
    try:
        return {"messages": send_saved(text), "channel": "saved_messages (telegram MCP)"}
    except DeliveryError as e:
        if e.sent or e.ambiguous:
            raise   # часть уже в «Избранном» или могла дойти — не дублировать ботом, честный сбой
        try:
            n = send_bot(text, timeout)
        except DeliveryError as b:
            raise DeliveryError(f"{e}; запасной канал: {b}") from None
        return {"messages": n, "channel": "бот → владелец (запасной)", "fallback_error": str(e)[:200]}


def send_bot(text, timeout=20):
    """Бот владельцу. → число отправленных сообщений. DeliveryError — без токена в тексте ошибки."""
    tok = _token()
    chat = os.environ.get("KB_TG_CHAT_ID") or CHAT_ID
    if not chat:
        raise DeliveryError("запасной канал не настроен (KB_TG_CHAT_ID пуст)")
    api = os.environ.get("KB_TG_API") or API
    sent = 0
    for part in chunks(text):
        data = urllib.parse.urlencode({"chat_id": chat, "text": part, "disable_web_page_preview": "true"}).encode()
        try:
            with urllib.request.urlopen(f"{api}/bot{tok}/sendMessage", data, timeout=timeout) as r:
                body = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:   # тело ответа Telegram токена не содержит: код и описание
            try:
                desc = str(json.loads(e.read().decode() or "{}").get("description"))[:120]
            except Exception:  # noqa: BLE001
                desc = ""
            raise DeliveryError(f"sendMessage: HTTP {e.code} {desc} (отправлено частей {sent})".strip()) from None
        except Exception as e:  # noqa: BLE001 — в ошибке urllib бывает URL с токеном: печатаем только тип
            raise DeliveryError(f"sendMessage: {type(e).__name__} (отправлено частей {sent})") from None
        if not body.get("ok"):
            raise DeliveryError(f"sendMessage: ok=false ({str(body.get('description'))[:120]})")
        sent += 1
    return sent
