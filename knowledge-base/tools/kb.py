#!/usr/bin/env python3
"""kb — база знаний агентов (корень — $KB_ROOT).

  kb find <запрос> [--raw] [--limit N] [--private] [--brief]
                                                      поиск по карточкам (BM25) с «Сутью»;
                                                      --brief — одна строка на карточку
  kb show <name> [--links]                            показать карточку (+ «Суть» её [[ссылок]])
  kb note "<факт 1–3 строки>" [--area A] [--source S] записать устойчивое знание во входящие (raw/inbox/vps):
                                                      гейт секретов, дедуп, дальше — сводчик → карточка
  kb harvest [--dry-run] [--days N] [--max-sessions N]
                                                      эксперимент, выключен: итоги сессий → kb note
                                                      (включить: touch state/harvest.on)
  kb lint                                             проверить карточки и лимиты
  kb index                                            сгенерировать INDEX*.md, INDEX.tsv, _index.md
  kb ingest                                           забрать сырьё (гейт секретов, коммит при диффе)
  kb health                                           state/health.json + HEALTH.md
  kb consolidate [--areas a,b] [--max-batches N] [--dry-run] [--min-units N]   сводчик: raw/ → карточки
  kb weekly [--dry-run] [--since D] [--until D] [--skip-consolidate]
                                                      недельный конвейер: сводчик → предложения по
                                                      карточкам проектов → обзор → доставка (опц.)
  kb daily [--dry-run] [--skip-consolidate]           ежедневный тихий прогон: сводчик → предложения за сутки;
                                                      без обзора и без отправки
"""
import argparse
import os
import sys
import time

os.umask(0o077)
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import cards  # noqa: E402
import search  # noqa: E402
from kbcore import (Cfg, LockTimeout, commit_paths, current_branch, log_run, new_run_id,  # noqa: E402
                    repo_lock)


def cmd_find(cfg, a):
    q = " ".join(a.query)
    found, raw = cards.find(cfg, q, raw=a.raw, limit=a.limit, private=a.private, brief=a.brief)
    for line in found:
        print(line)
    if not found and not search.query_terms(q):
        print("Запрос состоит из стоп-слов — добавь предметные слова (имя, команду, ошибку).")
    elif not found:
        print("В карточках ничего не найдено." + ("" if a.raw else " Сырьё: kb find --raw " + q))
    if a.raw:
        if raw:
            print()
        for line in raw:
            print(line)
    return 0 if (found or raw) else 1


def cmd_show(cfg, a):
    text, sugg = (cards.show_links if a.links else cards.show)(cfg, a.name)
    if text is None:
        print(f"Карточка `{a.name}` не найдена." + (f" Похожие: {', '.join(sugg)}" if sugg else ""))
        return 1
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_lint(cfg, a):
    errors, warnings = cards.lint(cfg)
    for w in warnings:
        print(f"предупреждение: {w}")
    for e in errors:
        print(f"ошибка: {e}")
    n = len(cards.iter_cards(cfg, include_private=True))
    print(f"lint: {n} карточек, {len(errors)} ошибок, {len(warnings)} предупреждений")
    return 1 if errors else 0


def cmd_index(cfg, a):
    t = time.time()
    run_id = new_run_id()
    br = current_branch(cfg)
    if br != "main":
        print(f"index генерируется только на main (сейчас: {br or '?'})")
        log_run(cfg, cmd="index", run_id=run_id, status="error", error=f"branch {br}",
                duration_s=0)
        return 2
    try:
        with repo_lock(cfg):
            changed = cards.build_index(cfg)
            sha = commit_paths(cfg, ["INDEX.md", "INDEX-core.md", "INDEX.tsv",
                                     ":(glob)cards/**/_index.md"], "index: пересборка индексов")
    except LockTimeout:
        log_run(cfg, cmd="index", run_id=run_id, status="lock_timeout", duration_s=0)
        print("СТОП: state/repo.lock занят")
        return 4
    except RuntimeError as e:
        log_run(cfg, cmd="index", run_id=run_id, status="error", error=str(e)[:300],
                duration_s=round(time.time() - t, 2))
        print(f"ошибка: {e}")
        return 1
    log_run(cfg, cmd="index", run_id=run_id, status="ok", changed=len(changed), commit=sha,
            duration_s=round(time.time() - t, 2))
    print(f"index: изменено {len(changed)} файлов" + (f", коммит {sha}" if sha else ""))
    for c in changed:
        print(f"  {c}")
    return 0


def cmd_note(cfg, a):
    import note  # лениво
    return note.note_vps(cfg, " ".join(a.text), area=a.area, source=a.source)


def cmd_harvest(cfg, a):
    import harvest  # лениво
    return harvest.main(cfg, a)


def cmd_ingest(cfg, a):
    import ingest  # лениво: find/show не платят за импорт
    return ingest.run_ingest(cfg, a.target)


def cmd_consolidate(cfg, a):
    from consolidate import run as consolidate_run
    return consolidate_run.main(cfg, a)


def cmd_weekly(cfg, a):
    from weekly import run as weekly_run
    return weekly_run.main(cfg, a)


def cmd_daily(cfg, a):
    from weekly import daily
    return daily.main(cfg, a)





def cmd_health(cfg, a):
    import health
    health.run_health(cfg, quiet=a.quiet)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="kb", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    f = sub.add_parser("find", help="поиск")
    f.add_argument("query", nargs="+")
    f.add_argument("--raw", action="store_true", help="искать и в сырье raw/")
    f.add_argument("--limit", type=int, default=10)
    f.add_argument("--private", action="store_true", help="включить cards/private/")
    f.add_argument("--brief", action="store_true", help="одна строка на карточку, без «Сути»")
    s = sub.add_parser("show", help="показать карточку")
    s.add_argument("name")
    s.add_argument("--links", action="store_true", help="+ «Суть» карточек из её [[ссылок]]")
    nt = sub.add_parser("note", help="записать устойчивое знание (решение владельца, грабли, маршрут)")
    nt.add_argument("text", nargs="+", help="факт одной–тремя строками (в кавычках)")
    nt.add_argument("--area", help="область из KB_AREAS (по умолчанию agents | infra | tools | projects/<slug>)")
    nt.add_argument("--source", help="откуда: путь, тред, ключ задачи (не её текст)")
    hv = sub.add_parser("harvest", help="эксперимент: итоги сессий → kb note (по умолчанию выключен)")
    hv.add_argument("--dry-run", action="store_true", help="показать кандидатов, ничего не писать")
    hv.add_argument("--days", type=float, default=2, help="окно по mtime сессии, дней")
    hv.add_argument("--max-sessions", type=int, default=40)
    sub.add_parser("lint", help="проверка карточек")
    sub.add_parser("index", help="генерация индексов")
    i = sub.add_parser("ingest", help="забрать сырьё")
    i.add_argument("target", nargs="?", default="all", choices=["vps", "all"])
    h = sub.add_parser("health", help="здоровье")
    h.add_argument("--quiet", action="store_true")
    c = sub.add_parser("consolidate", help="сводчик: raw/ → карточки")
    c.add_argument("--areas", default="", help="области через запятую (по умолчанию все, кроме private)")
    c.add_argument("--max-batches", type=int, default=20, help="лимит батчей стадии 1 за прогон")
    c.add_argument("--dry-run", action="store_true", help="нарезка и план без LLM и записи")
    c.add_argument("--parallel", type=int, default=4)
    c.add_argument("--batch-size", type=int, default=18)
    c.add_argument("--no-merge", action="store_true", help="не сливать в main, оставить ветку")
    c.add_argument("--max-conflicts", type=int, default=10)
    c.add_argument("--min-units", type=int, default=0,
                   help="порог: меньше N единиц сырья к LLM — прогон отложен (deferred) без вызовов LLM; 0 — без порога")
    c.add_argument("--pilot", action="store_true", help="подсказки обязательных карточек пилота")
    c.add_argument("--stages", default="12", help="какие стадии: 1, 2 или 12")
    wk = sub.add_parser("weekly", help="недельный конвейер: сводчик → предложения → обзор")
    wk.add_argument("--dry-run", action="store_true", help="без отправки и коммита: только файлы обзора и предложений")
    wk.add_argument("--since", help="начало окна YYYY-MM-DD (по умолчанию — конец прошлого прогона или −7 дней)")
    wk.add_argument("--until", help="конец окна YYYY-MM-DD включительно (по умолчанию — сейчас)")
    wk.add_argument("--skip-consolidate", action="store_true", help="не запускать сводчик (К1)")
    wk.add_argument("--skills-check", action="store_true", help="сверить skills.md сейчас, не дожидаясь 4 недель")
    dl = sub.add_parser("daily", help="ежедневный тихий прогон: сводчик → предложения за сутки, без отправки")
    dl.add_argument("--dry-run", action="store_true", help="без коммита: файлы предложений в state/weekly/dry/")
    dl.add_argument("--skip-consolidate", action="store_true", help="не запускать сводчик (К1)")
    dl.add_argument("--min-units", type=int, default=None,
                    help="порог К1: меньше N единиц сырья к LLM — сводчик откладывается (по умолчанию 3, KB_DAILY_MIN_UNITS)")
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 2
    cfg = Cfg()
    return {"find": cmd_find, "show": cmd_show, "lint": cmd_lint, "index": cmd_index,
            "ingest": cmd_ingest, "health": cmd_health, "note": cmd_note, "harvest": cmd_harvest,
            "consolidate": cmd_consolidate, "weekly": cmd_weekly, "daily": cmd_daily}[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
