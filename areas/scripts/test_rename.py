import unittest
from unittest.mock import patch, MagicMock
from rename import title_for, is_bb_auto_title, only_spawn_turn, Rpc

GENERATED = [{'type': 'system/thread-provisioning', 'data': {'entries': [
    {'key': 'metadata-completed', 'metadata': {'titleGenerated': True}}]}}]

class TitleTests(unittest.TestCase):
    def test_creation_time_seconds_and_milliseconds(self):
        for timestamp in (1791456880, 1791456880000):
            self.assertEqual(title_for({'createdAt': timestamp}, 'ЧАТЫ', 'ЛИЧНОЕ'), '08.10 | чаты | личное')
    def test_preserve_manual_name(self):
        self.assertIsNone(title_for({'createdAt': 1791456880, 'name': 'Мой чат'}, 'чаты', 'личное'))
    def test_keep_original_day_when_task_changes(self):
        self.assertEqual(title_for({'createdAt': 1791456880, 'title': '02.10 | старое | работа'}, 'новое', 'личное'), '02.10 | новое | личное')
    def test_explicit_rename_overrides_manual_name(self):
        self.assertEqual(title_for({'createdAt': 1791456880, 'name': 'Мой чат'}, 'ЧАТЫ', 'ЛИЧНОЕ', True), '08.10 | чаты | личное')
    def test_bb_auto_title_is_replaced(self):
        thread = {'createdAt': 1791456880000, 'title': 'Аналитика сетевого моста'}
        self.assertEqual(title_for(thread, 'мак-бридж', 'инфра', auto_title=True), '08.10 | мак-бридж | инфра')
    def test_timezone_midnight(self):
        thread = {'createdAt': 1791414000}
        self.assertTrue(title_for(thread, 'чат', 'личное').startswith('07.10'))
        self.assertTrue(title_for(thread, 'чат', 'личное', timezone='Europe/Moscow').startswith('08.10'))
    def test_init_failure_cleans_process(self):
        process = MagicMock()
        with patch('rename.subprocess.Popen', return_value=process), patch('rename.selectors.DefaultSelector'), patch.object(Rpc, 'call', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                Rpc()
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
    def test_reject_invalid_fields(self):
        for task in ('', 'a|b', 'a\nb'):
            with self.assertRaises(ValueError):
                title_for({'createdAt': 1791456880}, task, 'личное')

class AutoTitleTests(unittest.TestCase):
    def test_generated_title_on_first_prompt(self):
        self.assertTrue(is_bb_auto_title(GENERATED, []))
    def test_no_generation_event_means_manual(self):
        self.assertFalse(is_bb_auto_title([{'type': 'user'}], []))
    def test_later_turns_keep_title(self):
        # После первого сообщения пользователь мог переименовать сам — не трогаем.
        self.assertFalse(is_bb_auto_title(GENERATED, [{'id': 1}]))
    def test_missing_data_is_fail_closed(self):
        self.assertFalse(is_bb_auto_title(None, None))
        self.assertFalse(is_bb_auto_title(GENERATED, None))
    def test_malformed_data_is_fail_closed(self):
        for events in ('oops', [1, 'x', None], [{'type': 'system/thread-provisioning', 'data': 'x'}],
                       [{'type': 'system/thread-provisioning', 'data': {'entries': [1, {'metadata': 'x'}]}}]):
            self.assertFalse(is_bb_auto_title(events, []))

def request(source):
    return {'type': 'client/turn/requested', 'data': {'source': source}}

class FirstTurnTests(unittest.TestCase):
    def test_only_spawn(self):
        self.assertTrue(only_spawn_turn([request('spawn'), {'type': 'user'}]))
    def test_system_wakeup_keeps_title(self):
        # Пробуждение от дочернего треда не попадает в history — пользователь мог успеть переименовать.
        self.assertFalse(only_spawn_turn([request('spawn'), request('tell')]))
    def test_missing_or_odd_log(self):
        for events in (None, [], 'x', [request('tell')], [{'type': 'client/turn/requested', 'data': 'x'}]):
            self.assertFalse(only_spawn_turn(events))


import subprocess
import json
import tempfile
from pathlib import Path
from rename import load_areas, area_for, AREAS_PATH

class AreasTests(unittest.TestCase):
    def test_areas_file_valid_and_unambiguous(self):
        data = json.loads(Path(AREAS_PATH).read_text())
        seen = {}
        for area in data['areas']:
            for key in ('key', 'label', 'folder'):
                self.assertTrue(area[key].strip(), (area, key))
            self.assertTrue(area['section'].strip(), area)
            names = {n.strip().lower() for n in [area['key'], area['label'], *area.get('aliases', [])]}
            for name in names:  # duplicates inside one area are fine, across areas are not
                self.assertNotIn(name, seen, f'{name}: {seen.get(name)} vs {area["key"]}')
                seen[name] = area['key']
    def test_lookup_is_case_insensitive(self):
        areas = load_areas()
        self.assertEqual(area_for(areas, ' App ')['key'], 'work')
        self.assertEqual(area_for(areas, 'Инфра')['key'], 'infra')
        self.assertEqual(area_for(areas, 'Журнал')['section'], 'Дневник')
        self.assertIsNone(area_for(areas, 'xyz'))
    def test_broken_file_disables_routing(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
            f.write('{not json'); f.flush()
            self.assertEqual(load_areas(f.name), {})
        self.assertEqual(load_areas('/nonexistent/areas.json'), {})

from rename import route_decision

class RouteDecisionTests(unittest.TestCase):
    AREA = {'section': 'Дневник', 'auto': True}
    def test_moves_fresh_root_thread(self):
        self.assertEqual(route_decision({'title': 'Новый тред', 'sectionId': None}, self.AREA), 'move')
    def test_moves_untitled_thread(self):
        self.assertEqual(route_decision({}, self.AREA), 'move')
    def test_unknown_area(self):
        self.assertEqual(route_decision({}, None), 'unknown_area')
    def test_area_without_auto_stays_in_feed(self):
        self.assertEqual(route_decision({}, {'section': 'Сервер'}), 'feed')
    def test_not_first_naming(self):  # user may have taken it out of a section on purpose
        self.assertEqual(route_decision({'title': '02.10 | старое | работа'}, self.AREA), 'kept_not_first_naming')
    def test_keeps_user_section(self):
        self.assertEqual(route_decision({'sectionId': 'sec_x'}, self.AREA), 'kept_existing_section')
    def test_keeps_child(self):
        self.assertEqual(route_decision({'parentThreadId': 'thr_p'}, self.AREA), 'kept_child')
    def test_keeps_pinned_hub(self):
        self.assertEqual(route_decision({'pinnedAt': 1}, self.AREA), 'kept_pinned')

from rename import route_section

class RouteSectionTests(unittest.TestCase):
    AREA = {'section': 'Работа', 'auto': True}
    SECTIONS = json.dumps([{'id': 'sec_l', 'name': 'Работа'}]).encode()
    def test_moves_and_verifies(self):
        moved = json.dumps({'thread': {'sectionId': 'sec_l'}}).encode()
        with patch('rename.subprocess.check_output', side_effect=[self.SECTIONS, moved]) as co:
            self.assertEqual(route_section('bb', 'thr_1', {}, self.AREA), {'section': 'moved', 'sectionName': 'Работа'})
        self.assertEqual(co.call_args_list[1].args[0], ['bb', 'thread', 'update', 'thr_1', '--section', 'sec_l', '--json'])
    def test_missing_section_is_not_created(self):
        with patch('rename.subprocess.check_output', return_value=json.dumps([]).encode()) as co:
            self.assertEqual(route_section('bb', 'thr_1', {}, self.AREA)['section'], 'missing_section')
        self.assertEqual(co.call_count, 1)
    def test_no_bb_calls_when_kept(self):
        with patch('rename.subprocess.check_output') as co:
            self.assertEqual(route_section('bb', 'thr_1', {'sectionId': 's'}, self.AREA), {'section': 'kept_existing_section'})
        co.assert_not_called()
    def test_bb_error_does_not_raise(self):
        with patch('rename.subprocess.check_output', side_effect=subprocess.CalledProcessError(1, 'bb')):
            self.assertEqual(route_section('bb', 'thr_1', {}, self.AREA)['section'], 'error')
    def test_bb_hang_times_out(self):
        with patch('rename.subprocess.check_output', side_effect=subprocess.TimeoutExpired('bb', 20)) as co:
            self.assertEqual(route_section('bb', 'thr_1', {}, self.AREA)['section'], 'error')
        self.assertEqual(co.call_args.kwargs.get('timeout'), 20)


class MainRoutingTests(unittest.TestCase):
    def run_main(self, thread, project, outputs):
        import io, os, sys, rename
        reads = {'show': {'thread': thread}, 'log': [], 'history': []}
        out = io.StringIO()
        with patch.dict(os.environ, {'BB_THREAD_ID': 'thr_1'}), \
             patch.object(sys, 'argv', ['rename.py', '--task', 'тест', '--project', project]), \
             patch('rename.bb_command', return_value='bb'), \
             patch('rename.bb_read', return_value=reads), \
             patch('rename.subprocess.check_output', side_effect=outputs) as co, \
             patch('sys.stdout', out):
            rename.main()
        return json.loads(out.getvalue()), co
    def test_first_naming_moves_therapy(self):
        named = json.dumps({'thread': {'title': '21.11 | тест | дневник'}}).encode()
        sections = json.dumps([{'id': 'sec_t', 'name': 'Дневник'}]).encode()
        moved = json.dumps({'thread': {'sectionId': 'sec_t'}}).encode()
        res, co = self.run_main({'createdAt': 1795300000000, 'title': ''}, 'дневник', [named, sections, moved])
        self.assertEqual((res['status'], res['section']), ('verified', 'moved'))
    def test_first_naming_other_area_stays_in_feed(self):
        named = json.dumps({'thread': {'title': '21.11 | тест | сервер'}}).encode()
        res, co = self.run_main({'createdAt': 1795300000000, 'title': ''}, 'сервер', [named])
        self.assertEqual(res['section'], 'feed'); self.assertEqual(co.call_count, 1)
    def test_renaming_again_does_not_move(self):
        named = json.dumps({'thread': {'title': '20.11 | тест | дневник'}}).encode()
        res, co = self.run_main({'createdAt': 1795300000000, 'title': '20.11 | старое | дневник'}, 'дневник', [named])
        self.assertEqual(res['section'], 'kept_not_first_naming'); self.assertEqual(co.call_count, 1)
    def test_routing_error_keeps_naming(self):
        named = json.dumps({'thread': {'title': '21.11 | тест | дневник'}}).encode()
        res, co = self.run_main({'createdAt': 1795300000000, 'title': ''}, 'дневник',
                                [named, subprocess.CalledProcessError(1, 'bb')])
        self.assertEqual((res['status'], res['section']), ('verified', 'error'))

if __name__ == '__main__':
    unittest.main()
