#!/usr/bin/env python3
"""Rename only the explicitly identified current chat through supported APIs."""
import argparse
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo

class Rpc:
    def __init__(self):
        self.process = subprocess.Popen(['codex', 'app-server', '--stdio'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b''
        self.sequence = 0
        try:
            self.call('initialize', {'clientInfo': {'name': 'chat-names', 'version': '1.0'}})
            self.process.stdin.write(b'{"method":"initialized"}\n')
            self.process.stdin.flush()
        except BaseException:
            self.close()
            raise

    def call(self, method, params):
        self.sequence += 1
        ident = self.sequence
        payload = {'id': ident, 'method': method, 'params': params}
        self.process.stdin.write((json.dumps(payload) + '\n').encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if b'\n' not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError('Codex app-server closed its output')
                self.buffer += chunk
                continue
            line, self.buffer = self.buffer.split(b'\n', 1)
            message = json.loads(line)
            if message.get('id') == ident:
                if 'error' in message:
                    raise RuntimeError(json.dumps(message['error'], ensure_ascii=False))
                return message['result']
        raise TimeoutError(f'Codex RPC timeout: {method}')

    def close(self):
        self.selector.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

FORMAT = re.compile(r'^(\d{2}\.\d{2}) \| .+ \| .+$')

AREAS_PATH = Path(__file__).resolve().parent.parent / 'areas.json'

def load_areas(path=AREAS_PATH):
    """Map every lowercased key/label/alias to its area; missing or broken file -> no routing."""
    try:
        data = json.loads(Path(path).read_text())
        index = {}
        for area in data['areas']:
            for name in [area['key'], area['label'], *area.get('aliases', [])]:
                index[name.strip().lower()] = area
        return index
    except (OSError, ValueError, KeyError, TypeError):
        return {}

def area_for(areas, label):
    return areas.get(label.strip().lower())

def route_decision(thread, area):
    if area is None:
        return 'unknown_area'
    if not area.get('auto'):
        return 'feed'                   # only 'auto' areas move at creation; others are promoted later
    if FORMAT.match(thread.get('title') or ''):
        return 'kept_not_first_naming'  # only the first naming routes
    if thread.get('sectionId'):
        return 'kept_existing_section'  # never override a manual or earlier choice
    if thread.get('parentThreadId'):
        return 'kept_child'             # children stay under their parent
    if thread.get('pinnedAt'):
        return 'kept_pinned'            # topic hubs stay in Pinned
    return 'move'

def route_section(cli, ident, thread, area):
    """Put the thread into its area section; never create sections, never raise."""
    decision = route_decision(thread, area)
    if decision != 'move':
        return {'section': decision}
    try:
        sections = json.loads(subprocess.check_output(
            [cli, 'thread', 'section', 'list', '--json'], timeout=20))
        match = next((s for s in sections if s.get('name') == area['section']), None)
        if not match:
            return {'section': 'missing_section', 'wanted': area['section']}
        actual = json.loads(subprocess.check_output(
            [cli, 'thread', 'update', ident, '--section', match['id'], '--json'], timeout=20))
        actual = actual.get('thread', actual)
        if actual.get('sectionId') != match['id']:
            return {'section': 'error', 'error': 'read-back section differs'}
        return {'section': 'moved', 'sectionName': area['section']}
    except (subprocess.SubprocessError, ValueError, OSError, AttributeError, KeyError) as error:
        return {'section': 'error', 'error': f'{type(error).__name__}: {error}'[:200]}


def _dicts(items):
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

def is_bb_auto_title(events, prompts):
    """Cheap gate: bb's own title generator ran and the user sent no follow-up (thread history
    omits the spawn prompt). Missing or odd data means a manual title."""
    if not isinstance(prompts, list) or prompts:
        return False
    for event in _dicts(events):
        if event.get('type') != 'system/thread-provisioning':
            continue
        data = event.get('data') if isinstance(event.get('data'), dict) else {}
        if any(isinstance(entry.get('metadata'), dict) and entry['metadata'].get('titleGenerated')
               for entry in _dicts(data.get('entries'))):
            return True
    return False

def only_spawn_turn(events):
    """History misses system wake-ups (tells, child completions), so confirm in the full log that
    the spawn is the only turn so far: later turns let the user rename by hand first."""
    requests = [event for event in _dicts(events) if event.get('type') == 'client/turn/requested']
    data = requests[0].get('data') if len(requests) == 1 else None
    return isinstance(data, dict) and data.get('source') == 'spawn'

def bb_read(cli, ident, commands, timeout=30):
    """Run `bb thread <cmd> <id> --json` reads in parallel (~1 s each); None for a failed read."""
    procs = {}
    deadline = time.monotonic() + timeout
    try:
        for name, command in commands.items():
            procs[name] = subprocess.Popen([cli, 'thread', *command, ident, '--json'],
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        results = {}
        for name, proc in procs.items():
            out = proc.communicate(timeout=max(0.1, deadline - time.monotonic()))[0]
            try:
                results[name] = json.loads(out, strict=False) if proc.returncode == 0 else None
            except ValueError:
                results[name] = None
        return results
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait()

def events_of(log):
    return log if isinstance(log, list) else (log.get('events') if isinstance(log, dict) else None)

def title_for(thread, task, project, force=False, timezone='UTC', auto_title=False):
    current = thread.get('title') or thread.get('name')
    match = FORMAT.match(current or '')
    if current and not match and not force and not auto_title:
        return None  # Preserve a user's custom name during automatic invocation.
    timestamp = thread['createdAt']
    if timestamp > 100_000_000_000:
        timestamp /= 1000
    day = match.group(1) if match else datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).strftime('%d.%m')
    for text in (task, project):
        if not text.strip() or '|' in text or '\n' in text or '\r' in text:
            raise ValueError('Task/project must be nonempty single-line text without |')
    return f'{day} | {task.strip().lower()} | {project.strip().lower()}'

def bb_command():
    configured = os.environ.get('BB_CLI')
    if configured and Path(configured).is_file():
        return configured
    located = shutil.which('bb')
    if located:
        return located
    fallback = Path.home() / '.local/share/bb-runtime/node_modules/bb-app/host-daemon/dist/bb'
    if fallback.is_file():
        return str(fallback)
    raise RuntimeError('bb CLI not found')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('bb', 'codex'))
    parser.add_argument('--id', help='Explicit thread ID; otherwise current environment ID')
    parser.add_argument('--task', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--force', action='store_true', help='Use only for an explicit rename request')
    parser.add_argument('--timezone', default='UTC', help='IANA timezone; same default on every host')
    args = parser.parse_args()
    backend = args.backend or ('bb' if os.environ.get('BB_THREAD_ID') else 'codex')
    ident = args.id or os.environ.get('BB_THREAD_ID' if backend == 'bb' else 'CODEX_THREAD_ID')
    if not ident:
        raise RuntimeError('No current thread ID: refuse to guess from newest session')
    rpc = None
    try:
        auto_title = False
        if backend == 'bb':
            cli = bb_command()
            reads = bb_read(cli, ident, {'show': ['show'], 'log': ['log', '--limit', '12'], 'history': ['history']})
            if not isinstance(reads['show'], dict) or not isinstance(reads['show'].get('thread'), dict):
                raise RuntimeError('bb thread show failed')
            thread = reads['show']['thread']
            current = thread.get('title') or ''
            if (current and not args.force and not FORMAT.match(current)
                    and is_bb_auto_title(events_of(reads['log']), reads['history'])):
                auto_title = only_spawn_turn(events_of(bb_read(cli, ident, {'log': ['log', '--all']})['log']))
        else:
            rpc = Rpc()
            def read():
                return rpc.call('thread/read', {'threadId': ident, 'includeTurns': False})['thread']
            thread = read()
        title = title_for(thread, args.task, args.project, args.force, args.timezone, auto_title)
        if title is None:
            result = {'status': 'preserved_custom_title', 'id': ident}
        else:
            result = None
        if result is None:
            if backend == 'bb':
                # The server returns the persisted thread, so this doubles as the read-back.
                actual = json.loads(subprocess.check_output([cli, 'thread', 'update', ident, '--title', title, '--json']))
                actual = actual.get('thread', actual)
            else:
                rpc.call('thread/name/set', {'threadId': ident, 'name': title})
                actual = read()
            if (actual.get('title') if backend == 'bb' else actual.get('name')) != title:
                raise RuntimeError('Read-back title differs from requested title')
            result = {'status': 'verified', 'backend': backend, 'id': ident, 'title': title}
        if backend == 'bb':
            try:  # routing must never break naming; `thread` was read before the rename
                result.update(route_section(cli, ident, thread, area_for(load_areas(), args.project)))
            except Exception as error:
                result.update({'section': 'error', 'error': f'{type(error).__name__}: {error}'[:200]})
        print(json.dumps(result, ensure_ascii=False))
    finally:
        if rpc:
            rpc.close()

if __name__ == '__main__':
    main()
