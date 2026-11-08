"""kb harvest (эксперимент): итоги сессий → kb note; по умолчанию выключен."""
import json
import os
import types

import pytest

from conftest import git_log, runs, write


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch):
    """harvest.main, как kb daily, ставит KB_HINT=0 процессу — откатываем после теста (иначе ломает test_kb_hint)."""
    for k in ("KB_HINT", "CLAUDE_CODE_DISABLE_AUTO_MEMORY"):
        monkeypatch.setenv(k, "x")      # запомнить исходное (в т.ч. «нет»), чтобы откат его восстановил
        monkeypatch.delenv(k)

SECRET = "токен поиска: sk-proj-" + "Ab3dE5gH7jK9mN1pQ3sT5vX7z" + "Ab3dE5gH7jK9"


def claude_session(path, first, tools=12, sidechain=False, old=True):
    lines = [{"type": "user", "isSidechain": sidechain, "message": {"content": first}}]
    for i in range(tools):
        lines.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": f"echo {i}"}}]}})
        lines.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "Traceback: boom" if i == 3 else "ok"}]}})
    lines.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "готово"}]}})
    write(path, "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n")
    if old:
        t = os.path.getmtime(path) - 3 * 3600
        os.utime(path, (t, t))


def setup(kb, monkeypatch):
    cfg, paths = kb
    root = paths["tmp"] / "sessions"
    monkeypatch.setenv("KB_HARVEST_CLAUDE", str(root / "claude"))
    monkeypatch.setenv("KB_HARVEST_CODEX", str(root / "codex"))
    proj = root / "claude" / "-home-app"
    claude_session(str(proj / "11111111-2222-3333-4444-555555555555.jsonl"), "почини деплой бота")
    claude_session(str(proj / "22222222-2222-3333-4444-555555555555.jsonl"), "сабагент", sidechain=True)
    claude_session(str(proj / "33333333-2222-3333-4444-555555555555.jsonl"),
                   "Сделай саммери ответа агента для уведомления")
    claude_session(str(proj / "44444444-2222-3333-4444-555555555555.jsonl"), "короткий", tools=2)
    claude_session(str(proj / "55555555-2222-3333-4444-555555555555.jsonl"), "свежая", old=False)
    return cfg, paths


def fake_llm(payload, calls):
    def run(prompt, model, timeout, cwd):
        calls.append(prompt)
        return {"result": json.dumps(payload, ensure_ascii=False), "tokens_in": 10, "tokens_out": 5}
    return run


def args(dry=False):
    return types.SimpleNamespace(dry_run=dry, days=7, max_sessions=40)


def inbox_text(cfg):
    d = os.path.join(cfg.raw, "inbox", "vps")
    return "".join(open(os.path.join(d, f), encoding="utf-8").read() for f in sorted(os.listdir(d))) \
        if os.path.isdir(d) else ""


def test_harvest_disabled_by_default(kb, monkeypatch):
    cfg, _ = setup(kb, monkeypatch)
    monkeypatch.delenv("KB_HARVEST", raising=False)
    import harvest
    calls = []
    assert harvest.main(cfg, args(), out=lambda *_: None, llm_run=fake_llm({}, calls)) == 0
    assert calls == [] and inbox_text(cfg) == ""
    assert [r for r in runs(cfg) if r.get("cmd") == "harvest"][-1]["status"] == "disabled"


def test_harvest_writes_notes_once_one_commit_and_cursor(kb, monkeypatch):
    cfg, _ = setup(kb, monkeypatch)
    open(os.path.join(cfg.state, "harvest.on"), "w").close()
    import harvest
    calls = []
    payload = {"work": True, "notes": [
        {"text": "app-stable: после правки config.json нужен systemctl restart app-stable", "area": "infra"},
        {"text": "трекер: CreateIssue в PROJ закрыт — через форму", "area": "нет-такой"},
        {"text": SECRET, "area": "infra"}]}
    assert harvest.main(cfg, args(), out=lambda *_: None, llm_run=fake_llm(payload, calls)) == 0
    assert len(calls) == 1                                   # только основная законченная рабочая сессия
    assert "ERR: Traceback" in calls[0] and "почини деплой" in calls[0]
    body = inbox_text(cfg)
    assert "systemctl restart app-stable" in body and "CreateIssue" in body and "sk-proj" not in body
    assert "источник: сессия claude:11111111-2222-3333-4444-555555555555" in body
    assert "· — · n-" in body                                # кривая область → без области
    assert sum(1 for s in git_log(cfg) if s.startswith("harvest:")) == 1
    assert not any(s.startswith("note vps") for s in git_log(cfg))
    done = [r for r in runs(cfg) if r.get("cmd") == "harvest" and r.get("step") == "done"][-1]
    assert done["written"] == 2 and done["quarantined"] == 1 and done["sessions"] == 1
    assert "app-stable" not in json.dumps([r for r in runs(cfg) if r.get("cmd") == "harvest"],
                                             ensure_ascii=False)
    # повтор: сессия не менялась — к LLM не идёт
    calls2 = []
    harvest.main(cfg, args(), out=lambda *_: None, llm_run=fake_llm(payload, calls2))
    assert calls2 == []


def test_harvest_personal_session_gives_no_notes(kb, monkeypatch):
    cfg, _ = setup(kb, monkeypatch)
    monkeypatch.setenv("KB_HARVEST", "1")
    import harvest
    harvest.main(cfg, args(), out=lambda *_: None,
                 llm_run=fake_llm({"work": False, "notes": [{"text": "личное", "area": "agents"}]}, []))
    assert inbox_text(cfg) == ""


def test_harvest_dry_run_writes_nothing(kb, monkeypatch):
    cfg, _ = setup(kb, monkeypatch)
    monkeypatch.delenv("KB_HARVEST", raising=False)
    import harvest
    out = []
    harvest.main(cfg, args(dry=True), out=out.append,
                 llm_run=fake_llm({"work": True, "notes": [{"text": "факт про bb", "area": "infra"}]}, []))
    assert any("факт про bb" in o for o in out)
    assert inbox_text(cfg) == "" and not os.path.exists(os.path.join(cfg.state, "harvest.json"))


def test_harvest_bad_json_is_logged_not_fatal(kb, monkeypatch):
    cfg, _ = setup(kb, monkeypatch)
    monkeypatch.setenv("KB_HARVEST", "1")
    import harvest

    def bad(prompt, model, timeout, cwd):
        return {"result": "не json", "tokens_in": 1, "tokens_out": 1}
    assert harvest.main(cfg, args(), out=lambda *_: None, llm_run=bad) == 0
    assert any(r.get("cmd") == "harvest" and r.get("status") == "error" for r in runs(cfg))


def test_review_cursor_saved_per_session(kb, monkeypatch):
    """#3: процесс убили после первой сессии — она не уйдёт в LLM повторно."""
    cfg, paths = setup(kb, monkeypatch)
    claude_session(str(paths["tmp"] / "sessions/claude/-home-app/66666666-2222-3333-4444-555555555555.jsonl"),
                   "вторая рабочая")
    monkeypatch.setenv("KB_HARVEST", "1")
    import harvest
    n = []

    def run(prompt, model, timeout, cwd):
        n.append(1)
        if len(n) == 2:
            raise SystemExit("killed")
        return {"result": json.dumps({"work": True, "notes": []}), "tokens_in": 1, "tokens_out": 1}
    try:
        harvest.main(cfg, args(), out=lambda *_: None, llm_run=run)
    except SystemExit:
        pass
    assert len(harvest.load_cursor(cfg)) >= 1


def test_review_sensitive_sessions_and_notes_dropped(kb, monkeypatch):
    """#11: сессии с признаками личного/терапии не идут в LLM; такие заметки не пишутся."""
    cfg, paths = setup(kb, monkeypatch)
    claude_session(str(paths["tmp"] / "sessions/claude/-home-app/77777777-2222-3333-4444-555555555555.jsonl"),
                   "занеси в дневник психотерапии")
    monkeypatch.setenv("KB_HARVEST", "1")
    import harvest
    calls = []
    payload = {"work": True, "notes": [{"text": "после психотерапии лучше спать", "area": "agents"},
                                       {"text": "bb server: порт 2222", "area": "infra"}]}
    harvest.main(cfg, args(), out=lambda *_: None, llm_run=fake_llm(payload, calls))
    assert len(calls) == 1 and "психотерап" not in calls[0]
    body = inbox_text(cfg)
    assert "порт 2222" in body and "психотерап" not in body


def test_review_bad_shapes_from_model(kb, monkeypatch):
    """#7: список вместо объекта и text не строкой — не роняют прогон."""
    cfg, paths = setup(kb, monkeypatch)
    monkeypatch.setenv("KB_HARVEST", "1")
    import harvest
    for res in (json.dumps([1, 2]), json.dumps({"work": True, "notes": [{"text": 5}, "x", {"text": "ok факт"}]})):
        def run(prompt, model, timeout, cwd, res=res):
            return {"result": res, "tokens_in": 1, "tokens_out": 1}
        os.path.exists(os.path.join(cfg.state, "harvest.json")) and os.remove(os.path.join(cfg.state, "harvest.json"))
        assert harvest.main(cfg, args(), out=lambda *_: None, llm_run=run) == 0
    assert "ok факт" in inbox_text(cfg)


def test_rereview_cursor_keeps_existing_files(kb, monkeypatch):
    """Обработанная сессия не берётся повторно даже при --days больше прежнего окна."""
    cfg, paths = setup(kb, monkeypatch)
    monkeypatch.setenv("KB_HARVEST", "1")
    import harvest
    p = str(paths["tmp"] / "sessions/claude/-home-app/11111111-2222-3333-4444-555555555555.jsonl")
    harvest.main(cfg, args(), out=lambda *_: None, llm_run=fake_llm({"work": True, "notes": []}, []))
    t = os.path.getmtime(p) - 20 * 86400
    os.utime(p, (t, t))
    a = args()
    a.days = 1
    harvest.main(cfg, a, out=lambda *_: None, llm_run=fake_llm({"work": True, "notes": []}, []))
    assert p in harvest.load_cursor(cfg)
    a.days = 60
    calls = []
    harvest.main(cfg, a, out=lambda *_: None, llm_run=fake_llm({"work": True, "notes": []}, calls))
    assert calls == []
