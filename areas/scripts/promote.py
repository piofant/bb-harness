#!/usr/bin/env python3
"""Daily: move threads Vova keeps returning to into their area section."""
import argparse, datetime, fcntl, json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rename import FORMAT, area_for, bb_command, load_areas

STATE = Path(os.environ.get('CHAT_NAMES_STATE', Path.home() / '.local/state/chat-names'))
MIN_DAYS, MAX_IDLE_DAYS, DAY = 3, 21, 86_400_000
SYSTEM = ('Классифицируй тред личного агента по области работы. Ниже — оглавление областей с описанием. '
          'Ответь ключом области (имя папки без слеша). none — только если тред явно не подходит ни к одной области.')
INDEX_PATH = Path(os.environ.get('CHAT_NAMES_INDEX', str(Path.home() / 'bb-workspace' / 'AGENTS.md')))

def area_index():
    try:
        return INDEX_PATH.read_text()
    except OSError:
        return ''

def label_of(title):
    return title.rsplit('|', 1)[1].strip() if FORMAT.match(title or '') else None

def active_days(history):
    return len({datetime.datetime.fromtimestamp(e['createdAt'] / 1000, datetime.timezone.utc).date()
                for e in history if e.get('createdAt')})

def pick(threads, promoted, now_ms, history_of, resolve):
    out = []
    for t in threads:
        if t.get('parentThreadId') or t.get('pinnedAt') or t.get('sectionId') or t.get('archivedAt'):
            continue
        if t['id'] in promoted or now_ms - t['updatedAt'] > MAX_IDLE_DAYS * DAY:
            continue  # a thread Vova took out of a section is never moved back
        if active_days(history_of(t['id'])) < MIN_DAYS:
            continue
        area = resolve(t)  # may call the model: only for real candidates
        if area and area.get('section'):
            out.append((t, area))
    return out

def make_resolver(areas, by_project, classify):
    by_key = {a['key']: a for a in areas.values()}
    def resolve(t):
        label = label_of(t.get('title'))
        if label and area_for(areas, label):
            return area_for(areas, label)
        if t.get('projectId') in by_project:
            return by_project[t['projectId']]
        return by_key.get(classify(t) or '')
    return resolve

CODEX_MODEL = os.environ.get('CHAT_NAMES_CODEX_MODEL', 'gpt-6-luna')
CODEX_ENV = {'HOME', 'PATH', 'LANG', 'LC_ALL', 'CODEX_HOME', 'TMPDIR',
             'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY'}

def classify_codex(prompt, keys, timeout=120):
    """Isolated Codex call (same recipe as the knowledge base): no user config, no history, no tools."""
    import tempfile
    schema = {'type': 'object', 'additionalProperties': False,
              'properties': {'area': {'type': 'string', 'enum': keys + ['none']}}, 'required': ['area']}
    with tempfile.TemporaryDirectory(prefix='chat-names-codex-') as d:
        schema_file, out_file = Path(d) / 'schema.json', Path(d) / 'out.json'
        schema_file.write_text(json.dumps(schema))
        cmd = ['codex', 'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
               '--sandbox', 'read-only', '--model', CODEX_MODEL, '-C', d,
               '--output-schema', str(schema_file), '-o', str(out_file)]
        for setting in ('model_reasoning_effort="medium"', 'web_search="disabled"', 'project_doc_max_bytes=0',
                        'approval_policy="never"', 'history.persistence="none"', 'features.shell_tool=false'):
            cmd += ['-c', setting]
        cmd += ['-']
        env = {k: v for k, v in os.environ.items() if k in CODEX_ENV}
        r = subprocess.run(cmd, input=f'{SYSTEM}\n\n{prompt}', capture_output=True, text=True,
                           timeout=timeout, env=env, cwd=d)
        if r.returncode != 0:
            raise RuntimeError(f'codex_exit_{r.returncode}')
        area = json.loads(out_file.read_text()).get('area')
    return area if area in keys else None

def classify_llm(title, first_prompt, keys, log):
    """Claude Haiku first, Codex on any Claude failure; every attempt is logged (backend, model, status,
    duration, error); thread text is never logged. 'none' from a model is an answer, not a failure."""
    schema = {'type': 'object', 'properties': {'area': {'enum': keys + ['none']}}, 'required': ['area']}
    prompt = f'{area_index()}\n\nКлючи: {", ".join(keys)}\nНазвание: {title}\nПервое сообщение: {(first_prompt or "")[:600]}'
    started, entry = time.time(), {'backend': 'claude-cli', 'model': 'haiku'}
    try:
        r = subprocess.run(['claude', '-p', '--model', 'haiku', '--tools', '', '--strict-mcp-config',
                            '--no-session-persistence', '--setting-sources', '', '--system-prompt', SYSTEM,
                            '--output-format', 'json', '--json-schema', json.dumps(schema)],
                           input=prompt, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f'exit {r.returncode}')
        area = json.loads(r.stdout).get('structured_output', {}).get('area')
        entry.update(status='ok', area=area, duration_s=round(time.time() - started, 1))
        log.append(entry)
        return area if area in keys else None
    except Exception as e:
        entry.update(status='error', error=f'{type(e).__name__}: {e}'[:200], duration_s=round(time.time() - started, 1))
        log.append(entry)
    started, entry = time.time(), {'backend': 'codex-cli', 'model': CODEX_MODEL, 'fallback': True}
    try:
        area = classify_codex(prompt, keys)
        entry.update(status='ok', area=area or 'none')
        return area
    except Exception as e:
        entry.update(status='error', error=f'{type(e).__name__}: {e}'[:200])
        return None
    finally:
        entry['duration_s'] = round(time.time() - started, 1)
        log.append(entry)

def cached_classifier(cache, classify):
    """Reuse earlier model decisions (also read by the Recent page for area labels)."""
    def run(t):
        if t['id'] in cache:
            return cache[t['id']]
        key = classify(t)
        if key:
            cache[t['id']] = key
        return key
    return run

def prompt_text(entry):
    parts = entry.get('input') or []
    return ' '.join(p.get('text', '') for p in parts if isinstance(p, dict) and p.get('type') == 'text').strip()

def remember_sectioned(threads, promoted):
    """Any thread already in a section (moved by rename.py, earlier runs or by hand) is never moved again:
    if Vova later takes it out, it stays out."""
    promoted.update(t['id'] for t in threads if t.get('sectionId'))

def safe_history(history_of, errors):
    def run(i):
        try:
            return history_of(i)
        except Exception as e:
            errors.append({'id': i, 'error': f'history: {type(e).__name__}: {e}'[:200]})
            return []
    return run

def write_atomic(path, text):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)

def bb(cli, *args):
    return json.loads(subprocess.check_output([cli, *args, '--json'], timeout=30))

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    cli, started, llm_log = bb_command(), time.time(), []
    STATE.mkdir(parents=True, exist_ok=True)
    lock = open(STATE / 'promote.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({'skipped': 'another run in progress'})); return
    state_file = STATE / 'promoted.json'
    promoted = set(json.loads(state_file.read_text())) if state_file.exists() else set()
    areas = load_areas()
    by_project = {p: a for a in areas.values() for p in a.get('projects', [])}
    keys = sorted({a['key'] for a in areas.values()})
    histories, errors = {}, []
    def raw_history(i):
        if i not in histories:
            histories[i] = bb(cli, 'thread', 'history', i, '--limit', '200')
        return histories[i]
    history_of = safe_history(raw_history, errors)
    def first_prompt(i):
        h = sorted(history_of(i), key=lambda e: e.get('createdAt') or 0)
        return prompt_text(h[0]) if h else ''
    cache_file = STATE / 'area-cache.json'
    try:
        cache = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        cache = {}
    classify = cached_classifier(cache, lambda t: classify_llm(t.get('title') or '', first_prompt(t['id']), keys, llm_log))
    threads = bb(cli, 'thread', 'list')
    if not args.dry_run:
        remember_sectioned(threads, promoted)
    chosen = pick(threads, promoted, time.time() * 1000, history_of, make_resolver(areas, by_project, classify))
    moved = []
    sections = {s['name']: s['id'] for s in bb(cli, 'thread', 'section', 'list')}
    for t, area in chosen:
        if args.dry_run:
            print(f"{area['section'] if area['section'] in sections else 'pinned':14} {t['id']} {t['title']}"); continue
        try:
            if area['section'] in sections:
                got = bb(cli, 'thread', 'update', t['id'], '--section', sections[area['section']])
                if got.get('thread', got).get('sectionId') != sections[area['section']]:
                    raise RuntimeError('read-back section differs')
                moved.append({'id': t['id'], 'section': area['section']})
            else:  # no section for this area yet: pin instead of creating one
                got = bb(cli, 'thread', 'pin', t['id'])
                if not got.get('thread', got).get('pinnedAt'):
                    raise RuntimeError('read-back pin missing')
                moved.append({'id': t['id'], 'section': 'pinned'})
            promoted.add(t['id'])
        except Exception as e:
            errors.append({'id': t['id'], 'error': f'{type(e).__name__}: {e}'[:200]})
    if not args.dry_run:  # a dry run changes nothing on disk except its own log line
        write_atomic(state_file, json.dumps(sorted(promoted)))
        write_atomic(cache_file, json.dumps(cache, ensure_ascii=False))
    with open(STATE / 'promote.jsonl', 'a') as log:
        log.write(json.dumps({'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'dry_run': args.dry_run,
                              'checked': len(threads), 'moved': moved, 'errors': errors, 'llm': llm_log,
                              'duration_s': round(time.time() - started, 1)}, ensure_ascii=False) + '\n')
    print(json.dumps({'moved': len(moved), 'errors': len(errors), 'candidates': len(chosen), 'llm_calls': len(llm_log)}))
    sys.exit(1 if errors else 0)

if __name__ == '__main__':
    main()
