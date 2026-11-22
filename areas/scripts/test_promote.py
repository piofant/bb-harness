import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch
from promote import label_of, active_days, pick, make_resolver, classify_llm

DAY = 86_400_000
NOW = 1_795_300_000_000
WORK = {'key': 'work', 'label': 'работа', 'section': 'Работа'}
SERVER = {'key': 'server', 'label': 'сервер', 'section': 'Сервер'}
AREAS = {'работа': WORK, 'work': WORK, 'сервер': SERVER, 'server': SERVER}
def thread(i, **kw):
    t = {'id': i, 'title': '10.11 | задача | работа', 'updatedAt': NOW - DAY, 'createdAt': NOW - 9 * DAY}
    t.update(kw); return t
def hist(days):
    return [{'createdAt': NOW - d * DAY} for d in days]
ANY = lambda t: WORK

class PromoteTests(unittest.TestCase):
    def test_label(self):
        self.assertEqual(label_of('10.11 | задача | Работа'), 'Работа')
        self.assertIsNone(label_of('DIARY'))
    def test_active_days_counts_distinct_days(self):
        self.assertEqual(active_days(hist([1, 1, 2, 5])), 3)
    def test_promotes_returning_thread(self):
        got = pick([thread('a')], set(), NOW, lambda _: hist([1, 2, 3]), ANY)
        self.assertEqual([(t['id'], a['section']) for t, a in got], [('a', 'Работа')])
    def test_skips(self):
        cases = [thread('child', parentThreadId='p'), thread('pin', pinnedAt=1), thread('sec', sectionId='s'),
                 thread('old', updatedAt=NOW - 30 * DAY), thread('done')]
        self.assertEqual(pick(cases, {'done'}, NOW, lambda _: hist([1, 2, 3]), ANY), [])
    def test_unresolved_area_stays_in_feed(self):
        self.assertEqual(pick([thread('a')], set(), NOW, lambda _: hist([1, 2, 3]), lambda t: None), [])
    def test_two_days_is_not_enough_and_resolver_not_called(self):
        calls = []
        self.assertEqual(pick([thread('a')], set(), NOW, lambda _: hist([1, 2]), lambda t: calls.append(t)), [])
        self.assertEqual(calls, [])  # no LLM spend on threads that are not candidates

class ResolverTests(unittest.TestCase):
    def test_order_label_then_project_then_llm(self):
        llm = []
        r = make_resolver(AREAS, {'proj_app': WORK}, lambda t: llm.append(t['id']) or 'server')
        self.assertEqual(r(thread('a', title='10.11 | x | сервер'))['key'], 'server')
        self.assertEqual(r(thread('b', title='DIARY', projectId='proj_app'))['key'], 'work')
        self.assertEqual(r(thread('c', title='Мониторинг ресурсов ВМ', projectId='proj_x'))['key'], 'server')
        self.assertEqual(llm, ['c'])
    def test_llm_none_or_unknown(self):
        self.assertIsNone(make_resolver(AREAS, {}, lambda t: None)(thread('a', title='x')))
        self.assertIsNone(make_resolver(AREAS, {}, lambda t: 'nope')(thread('a', title='x')))

class ClassifyTests(unittest.TestCase):
    KEYS = ['work', 'server']
    def test_parses_structured_output(self):
        out = '{"structured_output": {"area": "server"}}'
        with patch('promote.subprocess.run', return_value=subprocess.CompletedProcess([], 0, out, '')):
            self.assertEqual(classify_llm('Мониторинг ВМ', 'посмотри нагрузку', self.KEYS, []), 'server')
    def test_failure_returns_none_and_is_logged(self):
        log = []
        with patch('promote.subprocess.run', side_effect=subprocess.TimeoutExpired('claude', 60)):
            self.assertIsNone(classify_llm('x', 'y', self.KEYS, log))
        self.assertEqual(log[0]['status'], 'error')
    def test_none_answer(self):
        out = '{"structured_output": {"area": "none"}}'
        with patch('promote.subprocess.run', return_value=subprocess.CompletedProcess([], 0, out, '')):
            self.assertIsNone(classify_llm('x', 'y', self.KEYS, []))

class CacheTests(unittest.TestCase):
    def test_cached_classification_skips_llm_and_new_result_is_stored(self):
        from promote import cached_classifier
        cache, calls = {'a': 'server'}, []
        classify = cached_classifier(cache, lambda t: calls.append(t['id']) or 'work')
        self.assertEqual(classify({'id': 'a'}), 'server')
        self.assertEqual(classify({'id': 'b'}), 'work')
        self.assertEqual(calls, ['b']); self.assertEqual(cache, {'a': 'server', 'b': 'work'})
    def test_none_is_not_cached(self):
        from promote import cached_classifier
        cache = {}
        cached_classifier(cache, lambda t: None)({'id': 'x'})
        self.assertEqual(cache, {})

class PromptTextTests(unittest.TestCase):
    def test_joins_text_parts(self):
        from promote import prompt_text
        self.assertEqual(prompt_text({'input': [{'type': 'text', 'text': 'привет'}, {'type': 'image'}]}), 'привет')
        self.assertEqual(prompt_text({}), '')

class SafetyTests(unittest.TestCase):
    def test_threads_already_in_a_section_are_remembered(self):
        from promote import remember_sectioned
        promoted = {'old'}
        remember_sectioned([{'id': 'a', 'sectionId': 's'}, {'id': 'b'}, {'id': 'c', 'sectionId': None}], promoted)
        self.assertEqual(promoted, {'old', 'a'})
    def test_history_error_skips_thread_and_is_recorded(self):
        from promote import safe_history
        errors = []
        def boom(i): raise subprocess.TimeoutExpired('bb', 30)
        h = safe_history(boom, errors)
        self.assertEqual(h('thr_x'), [])
        self.assertEqual(errors[0]['id'], 'thr_x')
    def test_atomic_write(self):
        import tempfile, os
        from promote import write_atomic
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'promoted.json'
            write_atomic(p, '["a"]')
            self.assertEqual(p.read_text(), '["a"]')
            self.assertEqual(sorted(os.listdir(d)), ['promoted.json'])

class FallbackTests(unittest.TestCase):
    KEYS = ['work', 'server']
    def test_claude_failure_falls_back_to_codex_and_logs_both(self):
        log = []
        with patch('promote.subprocess.run', side_effect=subprocess.TimeoutExpired('claude', 60)), \
             patch('promote.classify_codex', return_value='server') as cx:
            self.assertEqual(classify_llm('Мониторинг ВМ', 'нагрузка', self.KEYS, log), 'server')
        cx.assert_called_once()
        self.assertEqual([(e['backend'], e['status']) for e in log], [('claude-cli', 'error'), ('codex-cli', 'ok')])
    def test_claude_none_does_not_call_codex(self):
        out = '{"structured_output": {"area": "none"}}'
        with patch('promote.subprocess.run', return_value=subprocess.CompletedProcess([], 0, out, '')), \
             patch('promote.classify_codex') as cx:
            self.assertIsNone(classify_llm('x', 'y', self.KEYS, []))
        cx.assert_not_called()
    def test_both_fail_returns_none(self):
        log = []
        with patch('promote.subprocess.run', side_effect=OSError('no claude')), \
             patch('promote.classify_codex', side_effect=RuntimeError('codex_exit_1')):
            self.assertIsNone(classify_llm('x', 'y', self.KEYS, log))
        self.assertEqual([e['status'] for e in log], ['error', 'error'])
    def test_codex_command_is_isolated(self):
        from promote import classify_codex
        def fake_run(cmd, **kw):
            Path(cmd[cmd.index('-o') + 1]).write_text('{"area": "work"}')
            fake_run.cmd = cmd; fake_run.kw = kw
            return subprocess.CompletedProcess(cmd, 0, '', '')
        with patch('promote.subprocess.run', side_effect=fake_run):
            self.assertEqual(classify_codex('prompt', self.KEYS), 'work')
        for flag in ('--ignore-user-config', '--ephemeral', '--skip-git-repo-check', 'read-only'):
            self.assertIn(flag, fake_run.cmd)
        self.assertIn('features.shell_tool=false', fake_run.cmd)
        self.assertNotIn('BB_CLI', fake_run.kw['env'])
