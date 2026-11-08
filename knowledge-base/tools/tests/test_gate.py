"""Гейт секретов: по тесту на каждый шаблон + ложные срабатывания. Фейковые токены собираются из кусков."""
import pytest

import gate

R = "aB3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zA"  # «случайный» хвост: буквы разного регистра и цифры

POSITIVE = [
    ("pem_private_key", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    ("pem_private_key", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
    ("sk_api_key", "OPENAI: " + "sk-" + "proj-" + R),
    ("sk_api_key", "ключ " + "sk-" + "or-v1-" + R),
    ("github_token", "remote " + "ghp_" + R + "1234"),
    ("github_token", "github_" + "pat_" + R + "_x1"),
    ("slack_token", "xox" + "b-1234567890-" + R[:12]),
    ("oauth_y0", "OAuth " + "y0_" + R + "Ab12"),
    ("iam_t1", "iam " + "t1." + R + "." + R),
    ("oauth_aqad", "token " + "AQAD-" + R),
    ("oauth_aqad", "token " + "AQAAAA" + R),
    ("bearer_token", "Authorization: Bearer " + R + "Zz9"),
    ("google_api_key", "key " + "AIza" + "Sy" + R + "x"),
    ("telegram_bot_token", "бот " + "1234567890:" + "AA" + R + "c"),
    ("jwt", "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiIxMjM0In0" + "." + "abcDEF123_-xyz"),
    ("aws_access_key", "aws " + "AKIA" + "IOSFODNN7EXAMPL1"),
    ("session_id_cookie", "Cookie: Session_id=" + "3:1712345678.5.0." + R),
    ("password_ru", "Пароль: " + "Qw3rty!2026"),
    ("password_ru", "пароль = " + "s3cr3tPass"),
    ("secret_assignment", "TELEGRAM_BOT_TOKEN=" + R),
    ("secret_assignment", 'api_key = "' + R + '"'),
    ("secret_assignment", '{"client_secret": "' + R + '"}'),
    ("secret_assignment", "DB_PASSWORD=" + "Xy7" + R[:14]),
    # находки ревью: `:` без кавычек, sk_, токен/ключ, URL, Basic, короткий пароль
    ("password_en", "password: " + "Xk92mLq7Zp"),
    ("secret_assignment", "API key: " + "3f9a" + R[:20]),
    ("sk_api_key", "ELEVENLABS_API_KEY: " + "sk_" + "1a2b" + R),
    ("token_ru", "токен: " + R),
    ("token_ru", "ключ API = " + R),
    ("url_credentials", "DATABASE_URL=postgres://admin:" + "S3cr3tPw" + "@db.host:5432/x"),
    ("basic_auth", "Authorization: Basic " + "YWRtaW46UzNjcjN0UHc="),
]


@pytest.mark.parametrize("name,text", POSITIVE)
def test_each_pattern_detects(name, text):
    hits = gate.scan_text("строка до\n" + text + "\nстрока после")
    assert (name, 2) in hits, hits


def test_every_pattern_has_positive_case():
    covered = {n for n, _ in POSITIVE}
    assert covered == {p[0] for p in gate.PATTERNS}


NEGATIVE = [
    "пароль: спросить у владельца",
    "пароль: нужен",
    "token=$TELEGRAM_BOT_TOKEN",
    "api_key=os.environ['OPENAI_API_KEY']",
    "max_tokens=4096",
    "Authorization: Bearer <token>",
    "Authorization: Bearer $TOKEN",
    "используем sk-learn и scikit",
    "ключ в .env, переменная OPENAI_API_KEY",
    "SECRET_KEY=your_secret_key_here_123",
    "токен вида y0_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    "время 12:30, номер 1234567890",
    "Session_id смотри в куках браузера",
    "t1_start_time_variable_name_is_long_enough_here",
    "password=********************",
    "обычный текст про деплой app-stable и PROJ-778",
    "password: required",
    "токен: смотри в .env",
    "ключ: PROJ-778",
    "https://user:password@example.com",
    "postgres://user:$PASSWORD@host/db",
    "Basic auth не используем",
    "token: см. memory_summary.md",
    "keys: [деплой, app-stable, рестарт]",
    "ключевое правило: не трогать app.service",
    # ложные срабатывания на реальном сырье (форма сохранена, содержимое выдумано)
    "токен получен через https://oauth.example.ru/authorize?response_type=token&client_id=0a1b2c3d4e5f",
    "путь токена: `/Users/user/.config/tool/token_2026_cache_file_v2.json`",
    "Ключевой транскрипт: `sessions/2026-10-01-a1.md`",
    "на подключённой ноде: `/var/lib/tailscale/node_9f8e7d6c_state.conf`",
    "PASS: tests/test_tov_basket_compare.py",
    "`tailscale status --json` при включённой ноде: `Self.Online=true9abcdefghijklmnopq`",
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_no_false_positive(text):
    assert gate.scan_text(text) == []


def test_hits_carry_no_values():
    secret = "sk-" + "proj-" + R
    hits = gate.scan_text("a\n" + secret)
    assert hits
    for h in hits:
        assert all(secret not in str(x) for x in h)
        assert isinstance(h[0], str) and isinstance(h[1], int)


def test_allowlist(tmp_path):
    p = tmp_path / "allow.txt"
    p.write_text("# комментарий\nABCDEF0123  raw/x.md\n\n")
    assert gate.load_allowlist(str(p)) == {"abcdef0123"}
    assert gate.load_allowlist(str(tmp_path / "missing")) == set()


def test_huge_line_does_not_hang():
    gate.scan_text("a" * 500000)
