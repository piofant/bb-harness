"""Гейт секретов: до записи в raw/ каждый файл проверяется шаблонами.

Находка → файл уходит в raw/.quarantine/ (вне git), в runs.jsonl пишутся
только имя шаблона и номер строки. Значение секрета никогда не возвращается.
"""
import base64
import binascii
import hashlib
import re

# Значения, которые выглядят как заглушка, а не секрет.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:\$|\{|<|%|\*)|x{4,}|\*{3,}|\.{3}|your[_-]|example|changeme|placeholder|"
    r"redacted|dummy|sample|fake|todo|os\.environ|getenv|process\.env")


_PATHLIKE = re.compile(r"^(?:/|~|\./|\.\./)|://|\.(?:md|py|json|jsonl|txt|html|sh|ya?ml|log|conf|"
                       r"toml|csv|ts|js)\b|/.*/", re.I)


def _pathlike(v):
    """Путь или URL, а не значение секрета (для общих шаблонов «ключ = …»)."""
    return bool(_PATHLIKE.search(v))


def _random_enough(v, min_len):
    """Похоже на машинный токен: длина, есть буква и цифра, не однообразно."""
    if len(v) < min_len:
        return False
    if _PLACEHOLDER.search(v):
        return False
    if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
        return False
    return len(set(v)) >= 6


def _not_placeholder(v):
    return not _PLACEHOLDER.search(v)


def _password_value(v):
    # «пароль: спросить у владельца», «password: required» — обычное слово, не секрет
    if re.fullmatch(r"[A-Za-zА-Яа-яЁё-]+", v) or _pathlike(v):
        return False
    return _not_placeholder(v) and len(v) >= 4


def _url_password(v):
    return _not_placeholder(v) and v.lower() not in ("password", "pass", "pwd", "passwd", "secret")


def _basic_value(v):
    try:
        raw = base64.b64decode(v + "=" * (-len(v) % 4), validate=True)
    except (binascii.Error, ValueError):
        return False
    return b":" in raw and len(raw) >= 5


# (имя, регулярка, проверка значения или None). Значение — группа 1, если есть, иначе всё совпадение.
PATTERNS = [
    ("pem_private_key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"), None),
    ("sk_api_key", re.compile(
        r"(?<![A-Za-z0-9_-])(sk[-_](?:proj-|ant-(?:api\d+-)?|or-v1-|live_|test_)?[A-Za-z0-9_\-]{20,})"),
     lambda v: _random_enough(v, 20)),
    ("github_token", re.compile(
        r"(?<![A-Za-z0-9_])(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"), None),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])(xox[abposr]-[A-Za-z0-9-]{10,})"),
     lambda v: _random_enough(v, 14)),
    ("oauth_y0", re.compile(r"(?<![A-Za-z0-9_])(y0_[A-Za-z0-9_\-]{30,})"),
     lambda v: _random_enough(v, 30)),
    ("iam_t1", re.compile(r"(?<![A-Za-z0-9_])(t1[._][A-Za-z0-9_\-.]{30,})"),
     lambda v: _random_enough(v, 30) and re.search(r"[A-Z]", v) is not None),
    ("oauth_aqad", re.compile(r"(?<![A-Za-z0-9_])(AQA(?:D|AAA)[A-Za-z0-9_\-]{20,})"),
     lambda v: _random_enough(v, 24)),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+([A-Za-z0-9\-._~+/]{20,}=*)"),
     lambda v: _random_enough(v, 20)),
    ("google_api_key", re.compile(r"(AIza[0-9A-Za-z_\-]{35})"), None),
    ("telegram_bot_token", re.compile(r"(?<![0-9])(\d{6,12}:AA[A-Za-z0-9_\-]{30,40})(?![A-Za-z0-9_\-])"),
     None),
    ("jwt", re.compile(r"(eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})"), None),
    ("aws_access_key", re.compile(r"(?<![A-Z0-9])((?:AKIA|ASIA)[0-9A-Z]{16})(?![A-Z0-9])"), None),
    ("session_id_cookie", re.compile(r"Session_id[\"']?\s*[=:]\s*[\"']?([^\s\"';<>,]{20,})"),
     lambda v: _random_enough(v, 20)),
    ("password_ru", re.compile(r"(?i)парол[ья]\s*[:=]\s*[\"'`]?([^\s\"'`<>]{4,})"), _password_value),
    ("password_en", re.compile(r"(?i)\bpass(?:word|wd)\s*[:=]\s*[\"'`]?([^\s\"'`<>]{6,})"),
     _password_value),
    ("token_ru", re.compile(r"(?i)(?<![А-Яа-яЁёA-Za-z])(?:токен|ключ)[\w -]{0,20}?\s*[:=]\s*[\"'`]?([^\s\"'`<>]{16,})"),
     lambda v: _random_enough(v, 16) and not _pathlike(v)),
    ("url_credentials", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:([^\s:/@]{4,})@"),
     _url_password),
    ("basic_auth", re.compile(r"\bBasic\s+([A-Za-z0-9+/]{8,}={0,2})"), _basic_value),
    ("secret_assignment", re.compile(
        r"(?i)\b[\w.-]*(?:key|token|secret|passw(?:or)?d)[\"']?\s*[=:]\s*[\"'`]?"
        r"([^\s\"'`,;<>()\[\]{}]{8,})"),
     lambda v: _random_enough(v, 16) and not _pathlike(v)),
]


# Версия гейта: меняется при правке шаблонов → кэш чистых файлов сбрасывается.
CHECKS_REV = "4"
VERSION = hashlib.sha256((CHECKS_REV + repr([(n, rx.pattern) for n, rx, _ in PATTERNS])).encode()
                         ).hexdigest()[:16]


def scan_text(text):
    """→ список (имя_шаблона, номер_строки). Без значений."""
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if len(line) > 20000:  # гигантские строки режем, чтобы не повиснуть
            line = line[:20000]
        for name, rx, check in PATTERNS:
            for m in rx.finditer(line):
                value = m.group(1) if m.groups() else m.group(0)
                if check is None or check(value):
                    hits.append((name, lineno))
                    break
    return hits


def scan_bytes(data):
    return scan_text(data.decode("utf-8", errors="replace"))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_allowlist(path):
    """state/quarantine-allow.txt: строки `<sha256>  <путь>` — человек проверил, это не секрет."""
    allowed = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    allowed.add(line.split()[0].lower())
    except OSError:
        pass
    return allowed
