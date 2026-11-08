"""kb find (BM25, стемминг, стоп-слова, «Суть», кэш) и kb show --links."""
import json
import os
import subprocess
import time

import cards
import search
from conftest import PY, TOOLS, card_text, write

KB = os.path.join(TOOLS, "kb.py")


def put(cfg, rel, text):
    return write(os.path.join(cfg.cards, rel), text)


def names(found):
    return [f.split(" — ", 1)[0].rsplit("/", 1)[-1][:-3] for f in found]


def run(p, *args):
    env = {"PATH": "/usr/bin:/bin", **p["env"]}  # без HOME, как env -i
    return subprocess.run([PY, KB, *args], capture_output=True, text=True, env=env, timeout=60)


# ---------- нормализация, стемминг, стоп-слова ----------

def test_stem_case_forms_meet():
    assert search.stem("команду") == search.stem("команда") == search.stem("команды")
    assert search.stem("деплой") == search.stem("деплоя") == search.stem("деплою")
    assert search.stem("бэкапы") == search.stem("бэкапов") == search.stem("бэкап")
    assert search.stem("skills") == search.stem("skill")
    assert search.stem("слэш") == "слэш"
    assert search.stem("v0.8.0") == "v0.8.0"        # с цифрами не трогаем


def test_compound_kept_whole_and_split():
    t = search.terms("Слэш-команду PROJ-778 app_stable app.service")
    assert search.terms("слэш-команда")[0] in t     # целиком, со стеммингом хвоста
    for x in ["proj-778", "proj", "778", "app_stable", "app", search.stem("stable"),
              "app.service", search.stem("service"), "слэш"]:
        assert x in t, x


def test_yo_and_case():
    assert search.terms("Ёлка ЗЕЛЁНАЯ") == search.terms("елка зеленая")


def test_stopwords_dropped():
    assert search.terms("как и где на по не что это the of and") == []
    assert search.query_terms("Где копать по деплою?") == [search.stem("копать"), search.stem("деплою")]


def test_sut_extraction():
    body = "**Суть.** Первое предложение.\nВторое.\n\n## Шаги\n1. не брать\n"
    assert search.sut(body) == "Первое предложение. Второе."
    long = "**Суть.** " + "слово " * 100
    s = search.sut(long)
    assert s.endswith("…") and len(s) <= search.SUT_CHARS + 1
    assert search.sut("# Заголовок\nабзац без маркера\n") == "абзац без маркера"


def test_stem_english_trailing_e():
    """Ревью #1: service/services, release/releases/released, cache/cached — одна основа."""
    for group in (["service", "services"], ["release", "releases", "released", "releasing"],
                  ["update", "updates", "updated"], ["message", "messages"],
                  ["cache", "cached", "caches"], ["issue", "issues"]):
        assert len({search.stem(w) for w in group}) == 1, group
    assert search.stem("file") == search.stem("files") == "file"


def test_stem_short_russian_bases():
    """Ревью #2: основы из 3 букв (бот, лог, код, кэш, тег, чат) склеивают формы."""
    for group in (["бот", "бота", "боту", "ботом", "ботов", "боты"], ["лог", "логи", "логов"],
                  ["код", "кода", "коде"], ["кэш", "кэша", "кэшем"], ["тег", "теги", "тегов"],
                  ["чат", "чата", "чатах"], ["мак", "маке", "мака"]):
        assert len({search.stem(w) for w in group}) == 1, group
    assert search.stem("сеть") == "сеть"             # не гласная — не трогаем


def test_compound_of_stopwords_dropped():
    assert search.terms("из-за что-то как-то") == []


def test_sut_keeps_line_boundaries():
    assert search.sut("**Суть.** Рестарт только клона\nЛегаси не запускать\n") == \
        "Рестарт только клона; Легаси не запускать"
    assert search.sut("**Суть.** Первое.\nВторое.\n") == "Первое. Второе."


# ---------- ранжирование ----------

def test_slash_command_found_by_inflected_query(kb):
    cfg, _ = kb
    put(cfg, "product/slash.md", card_text(
        "slash", "gotcha", desc="Слэш-команда app молча мертва без CommandHandler",
        keys="[слэш-команда, CommandHandler]", body="**Суть.** Без хендлера команда не доходит.\n"))
    put(cfg, "product/other.md", card_text("other", desc="что-то про бота", keys="[бот]"))
    found, _ = cards.find(cfg, "Добавил боту новую слэш-команду — она молча не работает. Где копать?")
    assert names(found)[0] == "slash"


def test_keys_rank_above_body(kb):
    cfg, _ = kb
    put(cfg, "infra/in-body.md", card_text(
        "in-body", desc="разное", keys="[разное]",
        body="**Суть.** бэкапы бэкапы бэкапы бэкапы бэкапы и ещё бэкапы.\n"))
    put(cfg, "infra/in-keys.md", card_text(
        "in-keys", desc="ночные копии сервера", keys="[бэкап, restic]", body="**Суть.** текст.\n"))
    put(cfg, "infra/noise.md", card_text("noise", desc="шум", keys="[шум]"))
    assert names(cards.find(cfg, "бэкапы")[0])[:2] == ["in-keys", "in-body"]


def test_keys_beat_body_even_with_long_keys(kb):
    cfg, _ = kb
    long_keys = "[" + ", ".join(f"ключ{i}" for i in range(40)) + ", бэкап]"
    put(cfg, "infra/in-keys.md", card_text("in-keys", desc="копии", keys=long_keys))
    put(cfg, "infra/in-body.md", card_text(
        "in-body", desc="разное", keys="[разное]", body="**Суть.** бэкап бэкап бэкап бэкап.\n"))
    put(cfg, "infra/noise.md", card_text("noise", desc="шум", keys="[шум]"))
    assert names(cards.find(cfg, "бэкап")[0])[:2] == ["in-keys", "in-body"]


def test_english_inflection_found(kb):
    cfg, _ = kb
    put(cfg, "product/rel.md", card_text("skill-release-to-ticket", desc="release skill", keys="[release]"))
    put(cfg, "product/noise.md", card_text("noise", desc="шум", keys="[шум]"))
    assert names(cards.find(cfg, "releases")[0]) == ["rel"]


def test_short_russian_base_found(kb):
    cfg, _ = kb
    put(cfg, "product/bot.md", card_text("bot", desc="бот молчит", keys="[бот]"))
    put(cfg, "product/noise.md", card_text("noise", desc="шум", keys="[шум]"))
    assert names(cards.find(cfg, "почему боту не приходят")[0]) == ["bot"]


def test_superseded_do_not_displace_live(kb):
    """Ревью #3: лимит добирается живыми; устаревшие — сверх лимита, не больше двух."""
    cfg, _ = kb
    for i in range(4):
        put(cfg, f"infra/old{i}.md", card_text(
            f"old{i}", status="superseded", desc="деплой деплой рестарт", keys="[деплой, рестарт]",
            extra="superseded_by: [live0]\n", body="**Суть.** деплой рестарт деплой.\n"))
    for i in range(3):
        put(cfg, f"infra/live{i}.md", card_text(f"live{i}", desc="выкатка", keys="[выкатка]",
                                                body="**Суть.** деплой.\n"))
    found = names(cards.find(cfg, "деплой рестарт", limit=3)[0])
    assert set(found[:3]) == {"live0", "live1", "live2"}
    assert len(found) == 5 and all(n.startswith("old") for n in found[3:])
    assert names(cards.find(cfg, "деплой рестарт", limit=1)[0])[0].startswith("live")


def test_rare_term_beats_common(kb):
    cfg, _ = kb
    for i in range(6):
        put(cfg, f"product/c{i}.md", card_text(f"c{i}", desc="скилл онбординга", keys="[скилл]"))
    put(cfg, "product/rare.md", card_text("rare", desc="релиз в PROJ-778", keys="[PROJ-778]"))
    assert names(cards.find(cfg, "скилл PROJ-778")[0])[0] == "rare"


def test_superseded_below_live_with_mark(kb):
    cfg, _ = kb
    put(cfg, "infra/old.md", card_text(
        "old", "process", status="superseded", desc="деплой: рестарт обоих ботов",
        keys="[деплой, рестарт]", extra="superseded_by: [new]\n",
        body="**Суть.** деплой деплой рестарт обоих.\n"))
    put(cfg, "infra/new.md", card_text("new", "process", desc="как выкатывать", keys="[выкатка]",
                                       body="**Суть.** деплой только клона.\n"))
    found, _ = cards.find(cfg, "деплой рестарт")
    assert names(found) == ["new", "old"]               # старая не спрятана, но ниже
    assert "\n  устарело → new" in found[1]
    assert "Суть:" not in found[1]
    assert "Суть: деплой только клона." in found[0]


def test_private_excluded_and_not_cached(kb):
    cfg, _ = kb
    put(cfg, "product/pub.md", card_text("pub", desc="деплой публичный"))
    put(cfg, "private/secret.md", card_text("secret", desc="деплой личный zzzprivate")
        .replace("sensitivity: internal", "sensitivity: private"))
    assert names(cards.find(cfg, "деплой")[0]) == ["pub"]
    assert "secret" in names(cards.find(cfg, "деплой", private=True)[0])
    with open(search.index_path(cfg), encoding="utf-8") as f:
        raw = f.read()
    assert "zzzprivate" not in raw and "secret" not in raw


def test_brief_is_old_format(kb):
    cfg, _ = kb
    put(cfg, "product/a.md", card_text("a", desc="деплой", body="**Суть.** важное.\n"))
    full, _ = cards.find(cfg, "деплой")
    brief, _ = cards.find(cfg, "деплой", brief=True)
    assert brief == ["cards/product/a.md — деплой [fact, проверено 2026-10-20]"]
    assert full == [brief[0] + "\n  Суть: важное."]
    assert cards.find(cfg, "zzz-нет-такого") == ([], [])


# ---------- кэш ----------

def test_cache_built_reused_and_invalidated(kb):
    cfg, _ = kb
    p = put(cfg, "product/a.md", card_text("a", desc="деплой"))
    docs, src = search.load_docs(cfg)
    assert src == "built" and os.path.isfile(search.index_path(cfg))
    assert search.load_docs(cfg)[1] == "cache"
    # правка карточки (mtime/размер) → пересборка, новое слово находится
    write(p, card_text("a", desc="деплой и рестарт"))
    os.utime(p, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert search.load_docs(cfg)[1] == "built"
    assert names(cards.find(cfg, "рестарт")[0]) == ["a"]
    # новая карточка → пересборка
    put(cfg, "infra/b.md", card_text("b", desc="рестарт клона"))
    assert set(names(cards.find(cfg, "рестарт")[0])) == {"a", "b"}
    # удаление → пересборка, карточки нет в выдаче
    os.remove(os.path.join(cfg.cards, "infra/b.md"))
    assert names(cards.find(cfg, "рестарт")[0]) == ["a"]


def test_cache_rebuilds_when_code_changes(kb, monkeypatch, tmp_path):
    """Ревью #4: правка search.py/kbcore.py (mtime/размер) → кэш пересобирается."""
    cfg, _ = kb
    put(cfg, "product/a.md", card_text("a", desc="деплой"))
    search.load_docs(cfg)
    assert search.load_docs(cfg)[1] == "cache"
    fake = write(str(tmp_path / "search_copy.py"), "# другая версия кода\n")
    monkeypatch.setattr(search, "CODE_FILES", (fake,) + search.CODE_FILES[1:])
    assert search.load_docs(cfg)[1] == "built"
    assert search.load_docs(cfg)[1] == "cache"


def test_cache_file_mode_0600(kb):
    cfg, _ = kb
    put(cfg, "product/a.md", card_text("a", desc="деплой"))
    old = os.umask(0o022)
    try:
        search.load_docs(cfg)
    finally:
        os.umask(old)
    assert os.stat(search.index_path(cfg)).st_mode & 0o777 == 0o600


def test_cache_rebuilds_on_index_tsv_and_corruption(kb):
    cfg, _ = kb
    put(cfg, "product/a.md", card_text("a", desc="деплой"))
    search.load_docs(cfg)
    write(os.path.join(cfg.root, "INDEX.tsv"), "name\n")
    assert search.load_docs(cfg)[1] == "built"
    write(search.index_path(cfg), "{битый json")
    assert search.load_docs(cfg)[1] == "built"
    with open(search.index_path(cfg), encoding="utf-8") as f:
        assert json.load(f)["version"] == search.INDEX_VERSION


def test_no_state_dir_still_works(kb):
    cfg, _ = kb
    put(cfg, "product/a.md", card_text("a", desc="деплой"))
    os.rmdir(cfg.state)
    assert names(cards.find(cfg, "деплой")[0]) == ["a"]
    assert not os.path.exists(cfg.state)


# ---------- show --links ----------

def test_show_links(kb):
    cfg, _ = kb
    put(cfg, "product/hub.md", card_text(
        "hub", "process", desc="релиз скилла",
        body="**Суть.** см. [[step-a]] и [[step-b|шаг б]], ещё раз [[step-a]], битая [[gone]].\n"))
    put(cfg, "tools/step-a.md", card_text("step-a", desc="шаг а", body="**Суть.** обновить клиент.\n"))
    put(cfg, "tools/step-b.md", card_text(
        "step-b", status="superseded", desc="шаг б", extra="superseded_by: [step-a]\n"))
    text, _ = cards.show_links(cfg, "hub")
    head, links = text.split("--- ссылки карточки: 3 ---\n")
    assert head.startswith("---\nname: hub")
    assert "[[step-a]] cards/tools/step-a.md — шаг а" in links
    assert "  Суть: обновить клиент." in links
    assert "  устарело → step-a" in links
    assert "[[gone]] — карточка не найдена" in links
    assert links.count("[[step-a]]") == 1
    assert cards.show_links(cfg, "nope")[0] is None


# ---------- CLI в чистом окружении ----------

def test_cli_find_brief_links_clean_env(kb):
    cfg, p = kb
    put(cfg, "product/slash.md", card_text(
        "slash", "gotcha", desc="Слэш-команда мертва без CommandHandler", keys="[слэш-команда]",
        body="**Суть.** Править канал. См. [[deploy]].\n"))
    put(cfg, "infra/deploy.md", card_text("deploy", "process", desc="деплой", body="**Суть.** только клон.\n"))
    r = run(p, "find", "новую", "слэш-команду")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("cards/product/slash.md — ") and "\n  Суть: Править канал." in r.stdout
    r = run(p, "find", "--brief", "слэш-команду")
    assert r.returncode == 0 and "Суть" not in r.stdout
    assert r.stdout.splitlines() == ["cards/product/slash.md — Слэш-команда мертва без CommandHandler"
                                     " [gotcha, проверено 2026-10-20]"]
    r = run(p, "show", "slash", "--links")
    assert r.returncode == 0 and "[[deploy]] cards/infra/deploy.md" in r.stdout
    assert "  Суть: только клон." in r.stdout
    r = run(p, "find", "несуществующее-zzz")
    assert r.returncode == 1 and "ничего не найдено" in r.stdout
    r = run(p, "find", "как", "где", "на")
    assert r.returncode == 1 and "стоп-слов" in r.stdout and "--raw" not in r.stdout
