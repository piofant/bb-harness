"""kb-hint: когда молчать и что показывать (офлайн-эвал retrieval-eval, гипотезы H1–H4).

H1 — не подсказывать на системные сообщения и короткие реплики, вложения не матчить;
H2 — порог уверенности; H3 — без повторов в сессии; H4 — суть карточки в строке;
телеметрия — skip_reason и sid в журнале.
"""
import importlib.util
import io
import json

import pytest

from conftest import card_text, write
from test_kb_hint import HOOK, ctx, hook, mkkb


def logrecs(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def run_hook(root, prompt, log, sid=None):
    raw = {"prompt": prompt, "hook_event_name": "UserPromptSubmit"}
    if sid:
        raw["session_id"] = sid
    return hook(root, raw=json.dumps(raw, ensure_ascii=False), args=("--log", str(log)))


def load_mod(name="kb_hint_skip"):
    spec = importlib.util.spec_from_file_location(name, HOOK)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DEPLOY = "как сделать деплой app-stable на Stable, нужен рестарт"


@pytest.mark.parametrize("prompt", [
    "<task-notification> <task-id>b1</task-id> <status>completed</status> деплой app-stable рестарт",
    "[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated background-task event. деплой app-stable",
    "[bb message from thread:thr_abc] деплой app-stable и рестарт",
    "[bb system] @thread:thr_x Child thread updated: деплой app-stable рестарт",
    "[bb automation due:auto_1] деплой app-stable рестарт",
    "  This session is being continued from a previous conversation that ran out of context. деплой app-stable",
    "<local-command-stdout>деплой app-stable</local-command-stdout>",
    "Сделай саммери ответа агента для уведомления в Telegram: деплой app-stable, рестарт",
    "<command-name>/model</command-name> <command-message>model</command-message> <command-args></command-args>",
])
def test_system_messages_get_no_hint_and_reason_logged(tmp_path, prompt):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    r = run_hook(root, prompt, log)
    assert ctx(r) == ""
    rec = logrecs(log)[-1]
    assert rec["status"] == "skip" and rec["skip_reason"] == "system"


def test_slash_command_with_user_text_still_hinted(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    p = f"<command-name>/fresh-reader</command-name> <command-args>{DEPLOY}</command-args>"
    assert "deploy-app-stable.md" in ctx(run_hook(root, p, log))


def test_marker_in_the_middle_is_not_system(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    p = f"{DEPLOY}; в прошлый раз пришёл <task-notification> и всё упало"
    assert "deploy-app-stable.md" in ctx(run_hook(root, p, log))


@pytest.mark.parametrize("prompt", ["да", "го", "Ок!", "продолжай", "ну что?", "делай дальше", "again", " "])
def test_short_reply_skipped(tmp_path, prompt):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    write(str(root / "cards/agents/continue-loop.md"),
          card_text("continue-loop", desc="Что делать по «продолжай» и again",
                    keys="[продолжай, again, дальше, делай]"))
    (root / "INDEX.tsv").unlink()  # карточки из frontmatter, включая новую
    p = prompt + " [Attached image. It is on disk at /tmp/x.png]" if not prompt.strip() else prompt
    assert ctx(run_hook(root, p, log)) == ""
    rec = logrecs(log)[-1]
    assert rec["status"] == "skip" and rec["skip_reason"] == "short"


@pytest.mark.parametrize("prompt", ["PROJ-778?", "deploy-app-stable", "логин colleague4",
                                    "как деплоить app"])
def test_short_with_ticket_or_card_name_is_hinted(tmp_path, prompt):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    assert ctx(run_hook(root, prompt, log)) != ""


def test_attachment_path_does_not_match_cards(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    write(str(root / "cards/agents/bb-thread-storage.md"),
          card_text("bb-thread-storage", desc="Где bb хранит вложения тредов",
                    keys="[thread-storage, attachments, bb-thread-storage]"))
    (root / "INDEX.tsv").unlink()
    p = ("посмотри что на картинке и скажи, красиво ли получилось у дизайнера "
         "[Attached image. It is on disk at /home/agent/.bb/thread-storage/thr_x/Attachments/image-1.png]")
    assert "bb-thread-storage" not in ctx(run_hook(root, p, log))


def test_low_score_skip_reason(tmp_path, monkeypatch):
    root = mkkb(tmp_path)
    m = load_mod()
    m.SHOW_MIN = 1000.0
    data = json.dumps({"prompt": DEPLOY, "session_id": "s1"}, ensure_ascii=False)
    out = io.StringIO()
    monkeypatch.delenv("KB_HINT", raising=False)
    rec = m.run(["--root", str(root)], io.StringIO(data), out)
    assert out.getvalue() == ""
    assert rec["status"] == "skip" and rec["skip_reason"] == "low_score"


def test_confident_threshold_relative_and_cap():
    m = load_mod("kb_hint_conf")
    m.SHOW_MIN, m.REL_MIN, m.MAX_CARDS = 4.0, 0.6, 2
    assert m.confident([]) == []
    assert m.confident([(3.9, 1)]) == []                     # ниже абсолютного порога
    assert m.confident([(10.0, 1), (6.5, 2), (5.9, 3)]) == [1, 2]   # 5.9 < 10·0.6
    assert m.confident([(10.0, 1), (9.0, 2), (8.0, 3)]) == [1, 2]   # не больше MAX_CARDS
    # порог от лучшей карточки до исключения повторов: слабая за повтором не всплывает
    assert m.confident([(10.0, 1), (5.0, 2)], exclude={1}) == []
    assert m.confident([(10.0, 1), (7.0, 2)], exclude={1}) == []   # лучшая — повтор → молчим
    assert m.confident([(10.0, 1), (7.0, 2)], exclude={2}) == [1]


def test_repeat_in_same_session_suppressed_then_expires(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    assert "deploy-app-stable.md" in ctx(run_hook(root, DEPLOY, log, sid="sess-A"))
    assert ctx(run_hook(root, DEPLOY, log, sid="sess-A")) == ""
    rec = logrecs(log)[-1]
    assert rec["status"] == "skip" and rec["skip_reason"] == "repeat"
    # другая сессия — подсказка есть
    assert "deploy-app-stable.md" in ctx(run_hook(root, DEPLOY, log, sid="sess-B"))
    # через REPEAT_WINDOW промптов той же сессии — снова показываем
    m = load_mod("kb_hint_rw")
    for _ in range(m.REPEAT_WINDOW):  # другие промпты пользователя (служебные «да» окно не двигают)
        run_hook(root, "расскажи анекдот про котов", log, sid="sess-A")
    assert "deploy-app-stable.md" in ctx(run_hook(root, DEPLOY, log, sid="sess-A"))


def test_repeat_without_session_or_log_never_suppresses(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    assert ctx(run_hook(root, DEPLOY, log)) != ""
    assert ctx(run_hook(root, DEPLOY, log)) != ""
    raw = json.dumps({"prompt": DEPLOY, "session_id": "s"}, ensure_ascii=False)
    assert ctx(hook(root, raw=raw)) != "" and ctx(hook(root, raw=raw)) != ""  # без --log


def test_log_has_hashed_sid_not_raw_session_id(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    run_hook(root, DEPLOY, log, sid="0199-secret-session-id")
    rec = logrecs(log)[-1]
    assert rec["sid"] and len(rec["sid"]) == 8
    assert "0199-secret-session-id" not in log.read_text()


def test_line_shows_card_essence(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    write(str(root / "cards/infra/deploy-app-stable.md"),
          card_text("deploy-app-stable", "process", desc="Как выкатывать app на Stable",
                    keys="[деплой, app-stable, app.service, рестарт]",
                    body="**Суть.** Рестарт только `app-stable.service`; app.service не трогать\n\n## Шаги\n1. x\n"))
    out = ctx(run_hook(root, DEPLOY, log))
    assert "Рестарт только 'app-stable.service'; app.service не трогать" in out
    assert "## Шаги" not in out and "`app-stable.service`" not in out


def test_line_falls_back_to_description_without_essence(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    write(str(root / "cards/infra/deploy-app-stable.md"),
          card_text("deploy-app-stable", "process", desc="Как выкатывать app на Stable",
                    keys="[деплой, app-stable, app.service, рестарт]", body="Просто текст.\n"))
    assert "Как выкатывать app на Stable" in ctx(run_hook(root, DEPLOY, log))


# ---- находки ревью ----

def test_system_messages_do_not_wash_out_repeat_window(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    assert "deploy-app-stable.md" in ctx(run_hook(root, DEPLOY, log, sid="s"))
    for _ in range(5):  # пачка уведомлений фоновых задач и служебных реплик — не промпты пользователя
        run_hook(root, "<task-notification> <status>completed</status>", log, sid="s")
        run_hook(root, "ок", log, sid="s")
    assert ctx(run_hook(root, DEPLOY, log, sid="s")) == ""
    assert logrecs(log)[-1]["skip_reason"] == "repeat"


@pytest.mark.parametrize("prompt", [
    "[Image #1] [bb system] @thread:thr_x деплой app-stable рестарт",
    "[Attached image. It is on disk at /tmp/a.png]\n[bb message from thread:thr_y] деплой app-stable рестарт",
    "/compact", "  /clear ", "/model",
])
def test_system_after_attachment_and_raw_slash_command(tmp_path, prompt):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    assert ctx(run_hook(root, prompt, log)) == ""
    assert logrecs(log)[-1]["skip_reason"] == "system"


def test_raw_slash_command_with_text_is_hinted(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    assert "deploy-app-stable.md" in ctx(run_hook(root, "/fresh-reader " + DEPLOY, log))


def test_sut_lines_share_budget_instead_of_dropping_tail(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    long_sut = "Длинная суть про деплой и рестарт с подробностями маршрута, путями и граблями. " * 4
    names = []
    for i in range(5):
        n = f"deploy-part{i}"
        names.append(n)
        write(str(root / f"cards/infra/{n}.md"),
              card_text(n, desc=f"Деплой часть {i}", keys=f"[деплой, app-stable, рестарт, {n}]",
                        body=f"**Суть.** {long_sut}\n"))
    (root / "INDEX.tsv").unlink()
    c = ctx(run_hook(root, DEPLOY + " " + " ".join(names), log))
    lines = [ln for ln in c.split("\n") if ln.startswith("- ")]
    assert len(lines) == 5
    m = load_mod("kb_hint_budget")
    assert m.est_tokens(c) <= m.MAX_TOKENS


def test_repeat_dropped_counts_only_real_repeats(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    for i in range(4):
        n = f"deploy-part{i}"
        write(str(root / f"cards/infra/{n}.md"),
              card_text(n, desc=f"Деплой часть {i}", keys=f"[деплой, app-stable, рестарт, {n}]"))
    (root / "INDEX.tsv").unlink()
    run_hook(root, "релиз скилла в тикет PROJ-778 через Store", log, sid="s")  # другая тема
    q = ("- id=q1: деплой app-stable рестарт deploy-part0 deploy-part1 deploy-part2 deploy-part3\n"
         "- id=q2: какой логин у colleague4\n")
    assert ctx(run_hook(root, q, log, sid="s")) != ""
    rec = logrecs(log)[-1]
    assert rec["status"] == "hint" and rec.get("repeat_dropped", 0) == 0


def test_multi_all_repeats_reports_repeat_not_low_score(tmp_path):
    root, log = mkkb(tmp_path), tmp_path / "h.jsonl"
    q = "- id=q1: деплой app-stable и рестарт\n- id=q2: релиз скилла PROJ-778 в Store\n"
    assert ctx(run_hook(root, q, log, sid="s")) != ""
    assert ctx(run_hook(root, q, log, sid="s")) == ""
    assert logrecs(log)[-1]["skip_reason"] == "repeat"


def test_empty_essence_does_not_leak_heading(tmp_path):
    m = load_mod("kb_hint_sut")
    root = tmp_path / "kb"
    write(str(root / "cards/infra/x.md"), card_text("x", body="**Суть.**\n\n## Шаги\n1. rm -rf\n"))
    assert m.card_sut(str(root), "cards/infra/x.md") == ""


@pytest.mark.parametrize("extra", ["sensitivity: private\n", "status: superseded\n"])
def test_essence_not_read_from_card_marked_private_or_superseded_in_file(tmp_path, extra):
    m = load_mod("kb_hint_sut2")
    root = tmp_path / "kb"
    txt = card_text("x", body="**Суть.** секрет\n").replace("sensitivity: internal\n", "").replace(
        "status: active\n", "")
    write(str(root / "cards/infra/x.md"), txt.replace("---\n\n", extra + "---\n\n", 1))
    assert m.card_sut(str(root), "cards/infra/x.md") == ""


def test_essence_not_read_through_symlink(tmp_path):
    import os
    m = load_mod("kb_hint_sut3")
    root = tmp_path / "kb"
    write(str(tmp_path / "outside.md"), card_text("o", body="**Суть.** чужое\n"))
    (root / "cards/infra").mkdir(parents=True)
    os.symlink(tmp_path / "outside.md", root / "cards/infra/link.md")
    assert m.card_sut(str(root), "cards/infra/link.md") == ""


def test_essence_not_read_through_symlinked_dir_into_private(tmp_path):
    import os
    m = load_mod("kb_hint_sut4")
    root = tmp_path / "kb"
    write(str(root / "cards/private/p.md"), card_text("p", body="**Суть.** приватное\n"))
    os.symlink(root / "cards/private", root / "cards/x")
    assert m.card_sut(str(root), "cards/x/p.md") == ""


def test_repeat_of_best_card_silences_question_no_tail_cards():
    m = load_mod("kb_hint_tail")
    m.SHOW_MIN, m.REL_MIN, m.MAX_CARDS = 4.0, 0.6, 2
    # лучшая уже показана → хвост (второй–третий по силе) не всплывает вместо неё
    assert m.confident([(10.0, 1), (9.0, 2), (8.0, 3)], exclude={1}) == []
    assert m.confident([(10.0, 1), (9.0, 2), (8.0, 3)], exclude={2}) == [1, 3]
