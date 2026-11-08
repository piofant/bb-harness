"""frontmatter, lint, index, find, show."""
import os
import subprocess

import cards
import kbcore
from conftest import card_text, git_files, git_log, write


def put(cfg, rel, text):
    return write(os.path.join(cfg.cards, rel), text)


# ---------- frontmatter ----------

def test_parse_frontmatter_subset():
    fm, body = kbcore.parse_frontmatter(
        "---\nname: deploy-x        # = имя файла\nkeys: [деплой, \"a, b\", PROJ-778]\n"
        "sources:\n  - raw/a.md@2026-10-01\n  - raw/b.md\ncore: true\ndesc: 'в кавычках # не коммент'\n"
        "---\nтело\n")
    assert fm["name"] == "deploy-x"
    assert fm["keys"] == ["деплой", "a, b", "PROJ-778"]
    assert fm["sources"] == ["raw/a.md@2026-10-01", "raw/b.md"]
    assert fm["core"] is True
    assert fm["desc"] == "в кавычках # не коммент"
    assert body == "тело\n"


def test_no_frontmatter():
    fm, body = kbcore.parse_frontmatter("просто текст")
    assert fm is None and body == "просто текст"


def test_templates_parse_and_have_all_fields(kb):
    tpl = "/opt/agent-knowledge/cards/_templates"
    if not os.path.isdir(tpl):
        return
    names = sorted(os.listdir(tpl))
    assert names == sorted(f"{t}.md" for t in kbcore.TYPES)
    for fn in names:
        fm, _ = kbcore.parse_frontmatter(open(os.path.join(tpl, fn)).read())
        for k in kbcore.REQUIRED:
            assert k in fm, (fn, k)
        assert fm["type"] == fn[:-3]


# ---------- lint ----------

def test_lint_valid_card(kb):
    cfg, _ = kb
    put(cfg, "infra/deploy-app-stable.md", card_text("deploy-app-stable", "process"))
    errors, warnings = cards.lint(cfg)
    assert errors == [] and warnings == []


def test_lint_templates_and_index_files_ignored(kb):
    cfg, _ = kb
    put(cfg, "_templates/fact.md", "---\nname: bad\n---\n")
    put(cfg, "product/_index.md", "# индекс\n[[nowhere]]\n")
    assert cards.lint(cfg) == ([], [])


def test_lint_missing_fields(kb):
    cfg, _ = kb
    put(cfg, "product/a.md", "---\nname: a\ndescription: x\n---\nтело\n")
    put(cfg, "product/b.md", "без frontmatter\n")
    errors, _ = cards.lint(cfg)
    joined = "\n".join(errors)
    for k in ["type", "status", "keys", "sources", "verified", "verified_how", "review_after",
              "sensitivity"]:
        assert f"product/a.md: нет обязательного поля `{k}`" in joined
    assert "cards/product/b.md: нет frontmatter" in joined


def test_lint_bad_values(kb):
    cfg, _ = kb
    put(cfg, "product/Bad_Name.md", card_text("Bad_Name", type_="note", status="old",
                                            extra="core: true\n"))
    put(cfg, "product/n2.md", card_text("other-name"))
    put(cfg, "product/old-card.md", card_text("old-card", status="superseded"))
    put(cfg, "product/sec.md", card_text("sec").replace("sensitivity: internal", "sensitivity: private"))
    put(cfg, "nowhere.md", card_text("nowhere"))
    put(cfg, "product/a/b/deep.md", card_text("deep"))
    put(cfg, "weird/w.md", card_text("w"))
    put(cfg, "product/d.md", card_text("d").replace("verified: 2026-10-20", "verified: вчера"))
    errors, _ = cards.lint(cfg)
    j = "\n".join(errors)
    assert "только латиница" in j
    assert "type `note`" in j and "status `old`" in j
    assert "name `other-name` не совпадает" in j
    assert "superseded без superseded_by" in j
    assert "должна лежать в cards/private/" in j
    assert "cards/nowhere.md: карточка вне папки области" in j
    assert "глубже 2 уровней" in j
    assert "область `weird`" in j
    assert "verified `вчера`" in j


def test_lint_unique_names_and_links(kb):
    cfg, _ = kb
    put(cfg, "product/dup.md", card_text("dup", body="см. [[dup]] и [[missing-card]] и [[dup|алиас]]\n"))
    put(cfg, "infra/dup.md", card_text("dup"))
    put(cfg, "infra/s.md", card_text("s", extra="supersedes: [ghost]\n"))
    errors, warnings = cards.lint(cfg)
    j = "\n".join(errors)
    assert "имя `dup` не уникально" in j
    assert "битая ссылка [[missing-card]]" in j
    assert "[[dup]]" not in j
    assert any("ghost" in w for w in warnings)


def test_lint_limits(kb):
    cfg, _ = kb
    put(cfg, "product/big.md", card_text("big", body="я" * 4000))  # кириллица: 8000 Б
    put(cfg, "product/hub.md", card_text("hub", type_="topic", body="\n".join(["- x"] * 41)))
    put(cfg, "product/hub-ok.md", card_text("hub-ok", type_="topic", body="\n".join(["- x"] * 40)))
    write(os.path.join(cfg.root, "INDEX.md"), "\n".join(["x"] * 81))
    write(os.path.join(cfg.root, "INDEX-core.md"), "я" * 3100)
    errors, _ = cards.lint(cfg)
    j = "\n".join(errors)
    assert "big.md: размер" in j
    assert "hub.md: хаб 41 строк" in j
    assert "hub-ok" not in j
    assert "INDEX.md: 81 строк" in j
    assert "INDEX-core.md" in j


def test_lint_private_ok_inside_private(kb):
    cfg, _ = kb
    put(cfg, "private/p.md", card_text("p").replace("sensitivity: internal", "sensitivity: private"))
    assert cards.lint(cfg)[0] == []


# ---------- index ----------

def _many(cfg, n_hot=60, n_hub=60):
    for i in range(n_hub):
        put(cfg, f"product/hub-{i:03d}.md", card_text(f"hub-{i:03d}", "topic", desc=f"хаб {i} " + "о" * 60))
    for i in range(n_hot):
        put(cfg, f"infra/rule-{i:03d}.md", card_text(
            f"rule-{i:03d}", "gotcha", desc=f"правило {i} " + "д" * 80, extra="importance: high\n"))
    put(cfg, "agents/core-one.md", card_text("core-one", "fact", desc="ядро", extra="core: true\n"))
    put(cfg, "private/secret-thing.md", card_text("secret-thing", "preference", desc="личное",
                                                  extra="importance: high\ncore: true\n")
        .replace("sensitivity: internal", "sensitivity: private"))
    put(cfg, "projects/alpha/a-card.md", card_text("a-card"))
    put(cfg, "product/old.md", card_text("old", status="superseded", extra="superseded_by: [hub-000]\n"))


def test_build_index_limits_and_privacy(kb):
    cfg, _ = kb
    _many(cfg)
    changed = cards.build_index(cfg)
    assert "INDEX.md" in changed and "INDEX-core.md" in changed and "INDEX.tsv" in changed
    idx = open(os.path.join(cfg.root, "INDEX.md")).read()
    core = open(os.path.join(cfg.root, "INDEX-core.md")).read()
    tsv = open(os.path.join(cfg.root, "INDEX.tsv")).read()
    assert len(idx.splitlines()) <= kbcore.LIMIT_INDEX_LINES
    assert kbcore.est_tokens(core) <= kbcore.LIMIT_CORE_TOKENS
    assert "ещё" in idx  # усечение помечено
    for text in (idx, core, tsv):
        assert "secret-thing" not in text and "личное" not in text
    assert core.index("core-one") < core.index("rule-")  # core: true первым
    assert "projects/alpha" in idx
    product_idx = open(os.path.join(cfg.cards, "product/_index.md")).read()
    assert "## Устаревшие (superseded)" in product_idx and "old → hub-000" in product_idx
    assert os.path.isfile(os.path.join(cfg.cards, "projects/alpha/_index.md"))
    assert os.path.isfile(os.path.join(cfg.cards, "private/_index.md"))
    assert cards.build_index(cfg) == []  # идемпотентно: без меток времени
    assert cards.lint(cfg)[0] == []


def test_kb_index_commits_on_main_only(kb):
    cfg, p = kb
    import kb as kbcli
    put(cfg, "product/x.md", card_text("x", "topic"))
    assert kbcli.main(["index"]) == 0
    assert git_log(cfg)[0] == "index: пересборка индексов"
    files = git_files(cfg)
    assert "INDEX.md" in files and "cards/product/_index.md" in files
    assert "cards/private/_index.md" not in files
    assert "cards/product/x.md" not in files  # index коммитит только индексы
    assert kbcli.main(["index"]) == 0
    assert git_log(cfg).count("index: пересборка индексов") == 1  # без диффа нет коммита
    subprocess.run(["git", "-C", cfg.root, "checkout", "-q", "-b", "consolidate/x"], check=True)
    assert kbcli.main(["index"]) == 2
    rec = kbcore.read_runs(cfg)[-1]
    assert rec["cmd"] == "index" and rec["status"] == "error"


# ---------- find / show ----------

def _corpus(cfg):
    put(cfg, "infra/deploy-app-stable.md", card_text(
        "deploy-app-stable", "process", desc="как деплоить app на Stable",
        keys="[деплой, app-stable]", body="Рестарт только app-stable.\n"))
    put(cfg, "product/skill-release.md", card_text(
        "skill-release", "process", desc="релиз скилла в PROJ-778", keys="[PROJ-778, релиз]",
        body="Сначала PR, потом коммент. Про деплой ни слова, кроме этого: деплой.\n"))
    put(cfg, "infra/restart-both.md", card_text(
        "restart-both", "process", status="superseded", desc="деплой: рестарт обоих",
        extra="superseded_by: [deploy-app-stable]\n"))
    put(cfg, "private/health-note.md", card_text("health-note", desc="деплой личного")
        .replace("sensitivity: internal", "sensitivity: private"))
    write(os.path.join(cfg.raw, "claude-vps/p/project_two_bot.md"), "деплой: рестарт app и app-stable\n")
    write(os.path.join(cfg.quarantine, "claude-vps/p/q.md"), "деплой секрет\n")


def test_find_cards_default_no_raw_no_private(kb):
    cfg, _ = kb
    _corpus(cfg)
    found, raw = cards.find(cfg, "деплой")
    assert raw == []
    assert found[0].startswith("cards/infra/deploy-app-stable.md")
    assert not any("health-note" in f for f in found)
    old = [f for f in found if "restart-both" in f][0]
    assert "superseded → deploy-app-stable" in old
    assert found.index(old) == len(found) - 1


def test_find_raw_marked_and_quarantine_excluded(kb):
    cfg, _ = kb
    _corpus(cfg)
    found, raw = cards.find(cfg, "деплой", raw=True)
    assert raw and all(r.startswith("[сырьё] raw/") for r in raw)
    assert not any(".quarantine" in r for r in raw)


def test_find_ranking_limit_private(kb):
    cfg, _ = kb
    _corpus(cfg)
    found, _ = cards.find(cfg, "PROJ-778 релиз")
    assert found[0].startswith("cards/product/skill-release.md")
    assert len(cards.find(cfg, "деплой", limit=1)[0]) == 1
    assert any("health-note" in f for f in cards.find(cfg, "деплой", private=True)[0])
    # составной токен ищется и по частям, поэтому в запросе нет настоящих слов («слово» ~ «слова»)
    assert cards.find(cfg, "несуществующее-zzz-qqq") == ([], [])


def test_find_yo_normalized(kb):
    cfg, _ = kb
    put(cfg, "product/yo.md", card_text("yo", desc="ёлка зелёная"))
    assert cards.find(cfg, "елка")[0]


def test_show(kb, capsys):
    cfg, _ = kb
    _corpus(cfg)
    text, _ = cards.show(cfg, "skill-release")
    assert "name: skill-release" in text
    assert cards.show(cfg, "cards/infra/deploy-app-stable.md")[0]
    text, sugg = cards.show(cfg, "deploy-app")
    assert text is None and "deploy-app-stable" in sugg
    import kb as kbcli
    assert kbcli.main(["show", "nope-nope"]) == 1
    assert kbcli.main(["find", "PROJ-778"]) == 0
    assert "skill-release" in capsys.readouterr().out


def test_index_removes_stale_area_index(kb):
    """#14: у области не осталось карточек — старый _index.md удаляется, а не висит."""
    cfg, _ = kb
    put(cfg, "projects/alpha/a-card.md", card_text("a-card"))
    write(os.path.join(cfg.cards, "product/sub/_index.md"), "# мусор старой раскладки\n")
    cards.build_index(cfg)
    assert os.path.isfile(os.path.join(cfg.cards, "projects/alpha/_index.md"))
    os.remove(os.path.join(cfg.cards, "projects/alpha/a-card.md"))
    changed = cards.build_index(cfg)
    assert not os.path.exists(os.path.join(cfg.cards, "projects/alpha/_index.md"))
    assert not os.path.exists(os.path.join(cfg.cards, "product/sub/_index.md"))
    assert "cards/projects/alpha/_index.md" in changed
    assert os.path.isfile(os.path.join(cfg.cards, "product/_index.md"))  # основные области остаются
