import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import context_pack

RAW_FAILURE = "[Errno 13] Permission denied: '/data/decisions/T1/g1.json'"
TRACEBACK_TEXT = 'Traceback (most recent call last):\n  File "/opt/dashboard/server.py", line 1, in context_for'
LEAKS = ('Traceback', 'Errno', 'Permission denied', '/data/', '/opt/dashboard')


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = context_pack.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ContextPackCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'mailbox'
        self.put('projects.json', {'projects': [{'id': 'p', 'name': 'P'}]})
        self.put('tasks/p/T1.json', {'id': 'T1', 'project': 'p', 'status': 'in_progress'})
        self.put('decisions/T1/g1.json', {'verdict': 'approved', 'rationale': 'Scope accepted'})
        path_guard = patch.object(sys, 'path', list(sys.path))
        path_guard.start()
        self.addCleanup(path_guard.stop)

    def put(self, relative, record):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))

    def tree_snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in sorted(self.root.rglob('*')) if path.is_file()}

    def assertFixedFailure(self, result, line, code):
        exit_code, out, err = result
        self.assertEqual((code, '', line + '\n'), (exit_code, out, err))
        for leak in LEAKS:
            self.assertNotIn(leak, err)


class PackOutputTests(ContextPackCase):
    def test_a_named_root_prints_the_pack_as_the_only_stdout(self):
        code, out, err = run_main('--root', str(self.root), 'task:T1')
        pack = json.loads(out)
        self.assertEqual((0, '', 'task:T1', ['decision:T1/g1']), (code, err, pack['node']['id'], [d['id'] for d in pack['decisions']]))

    def test_the_pack_has_generated_at_and_no_mode(self):
        pack = json.loads(run_main('--root', str(self.root), 'task:T1')[1])
        self.assertIn('generated_at', pack)
        self.assertNotIn('mode', pack)

    def test_the_root_may_be_given_as_an_equals_option(self):
        code, out, _ = run_main(f'--root={self.root}', 'task:T1')
        self.assertEqual((0, 'task:T1'), (code, json.loads(out)['node']['id']))

    def test_the_option_may_follow_the_node(self):
        code, out, _ = run_main('task:T1', '--root', str(self.root))
        self.assertEqual((0, 'task:T1'), (code, json.loads(out)['node']['id']))

    def test_a_default_root_holding_a_registry_is_read(self):
        with patch.object(context_pack, 'MAILBOX', self.root):
            code, out, _ = run_main('task:T1')
        self.assertEqual((0, 'task:T1'), (code, json.loads(out)['node']['id']))

    def test_a_named_root_needs_no_registry(self):
        (self.root / 'projects.json').unlink()
        self.assertEqual(0, run_main('--root', str(self.root), 'task:T1')[0])

    def test_reading_leaves_the_mailbox_tree_byte_identical(self):
        before = self.tree_snapshot()
        run_main('--root', str(self.root), 'task:T1')
        with patch.object(context_pack, 'MAILBOX', self.root):
            run_main('task:T1')
        self.assertEqual(before, self.tree_snapshot())

    def test_pack_text_holds_no_absolute_mailbox_path(self):
        self.assertNotIn(str(self.root), run_main('--root', str(self.root), 'task:T1')[1])


class RefusalTests(ContextPackCase):
    def test_a_malformed_node_is_refused_with_exit_2_before_anything_is_read(self):
        for node in ('T1', 'epic:E', 'task:', 'task:-x', 'task:a/b', 'task:' + 'a' * 129, 'task:T1\n', ''):
            with self.subTest(node=node), patch.object(context_pack, 'load_context_for') as loader:
                self.assertFixedFailure(run_main('--root', str(self.root), node), context_pack.INVALID_NODE, 2)
                loader.assert_not_called()

    def test_unusable_arguments_print_the_usage_line_and_exit_2(self):
        for argv in ((), ('task:T1', 'task:T2'), ('--bogus', 'task:T1'), ('--root',), ('--root', '', 'task:T1'), ('--root=', 'task:T1'),
                     ('--root', 'a', '--root', 'b', 'task:T1')):
            with self.subTest(argv=argv):
                self.assertFixedFailure(run_main(*argv), context_pack.USAGE, 2)

    def test_a_default_root_without_a_registry_is_refused_before_the_dashboard_code_is_loaded(self):
        (self.root / 'projects.json').unlink()
        with patch.object(context_pack, 'MAILBOX', self.root), patch.object(context_pack, 'load_context_for') as loader:
            self.assertFixedFailure(run_main('task:T1'), context_pack.NO_REGISTRY, 1)
        loader.assert_not_called()

    def test_an_invalid_node_wins_over_a_missing_registry(self):
        (self.root / 'projects.json').unlink()
        with patch.object(context_pack, 'MAILBOX', self.root):
            self.assertFixedFailure(run_main('bad'), context_pack.INVALID_NODE, 2)

    def test_an_unknown_node_is_reported_as_not_found(self):
        self.assertFixedFailure(run_main('--root', str(self.root), 'task:nothing'), context_pack.NODE_NOT_FOUND, 1)

    def test_a_missing_dashboard_directory_is_the_fixed_code_not_loaded_line(self):
        missing = FileNotFoundError('dispatch_gate.py was not found above /opt/dashboard/scripts/context_pack.py')
        with patch.object(context_pack, 'gate_directory', side_effect=missing):
            self.assertFixedFailure(run_main('--root', str(self.root), 'task:T1'), context_pack.CODE_NOT_LOADED, 1)

    def test_dashboard_code_that_cannot_be_imported_is_the_fixed_code_not_loaded_line(self):
        broken_server = type(sys)('server')
        with patch.dict(sys.modules, {'server': broken_server}):
            self.assertFixedFailure(run_main('--root', str(self.root), 'task:T1'), context_pack.CODE_NOT_LOADED, 1)

    def test_a_context_graph_that_cannot_be_imported_is_the_fixed_code_not_loaded_line(self):
        with patch.dict(sys.modules, {'context_graph': None}):
            self.assertFixedFailure(run_main('--root', str(self.root), 'task:T1'), context_pack.CODE_NOT_LOADED, 1)

    def test_an_exception_text_from_the_reader_never_reaches_the_output(self):
        for failure in (OSError(RAW_FAILURE), RuntimeError(TRACEBACK_TEXT)):
            with self.subTest(failure=type(failure).__name__), patch.object(context_pack, 'load_context_for', return_value=Mock(side_effect=failure)):
                self.assertFixedFailure(run_main('--root', str(self.root), 'task:T1'), context_pack.UNREADABLE, 1)

    def test_a_pack_that_cannot_be_serialised_is_the_fixed_unreadable_line(self):
        with patch.object(context_pack, 'load_context_for', return_value=Mock(return_value={'bad': float('nan')})):
            self.assertFixedFailure(run_main('--root', str(self.root), 'task:T1'), context_pack.UNREADABLE, 1)

    def test_a_failure_outside_the_known_steps_is_the_fixed_unreadable_line(self):
        with patch.object(context_pack, 'mailbox_to_read', side_effect=RuntimeError(TRACEBACK_TEXT)):
            self.assertFixedFailure(run_main('--root', str(self.root), 'task:T1'), context_pack.UNREADABLE, 1)

    def test_refusal_messages_are_the_frozen_texts(self):
        self.assertEqual(('context_pack: invalid node', 'context_pack: no mailbox registry found; name the mailbox with --root',
                          'context_pack: dashboard code could not be loaded', 'context_pack: node not found',
                          'context_pack: mailbox could not be read'),
                         (context_pack.INVALID_NODE, context_pack.NO_REGISTRY, context_pack.CODE_NOT_LOADED, context_pack.NODE_NOT_FOUND,
                          context_pack.UNREADABLE))


class ContextBlockCase(ContextPackCase):
    def setUp(self):
        super().setUp()
        self.server, self.graph = context_pack.load_dashboard_modules()

    def add_tasks(self, *task_ids):
        for task_id in task_ids:
            self.put(f'tasks/p/{task_id}.json', {'id': task_id, 'project': 'p', 'status': 'in_progress'})

    def block_for(self, task_ids):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            block = context_pack.context_block(self.root, task_ids)
        self.assertEqual(('', ''), (out.getvalue(), err.getvalue()))
        return block

    def without_time(self, pack):
        return {key: value for key, value in pack.items() if key != 'generated_at'}


class ContextBlockPacksTests(ContextBlockCase):
    def test_a_named_task_has_the_pack_the_command_prints_for_it(self):
        block = self.block_for(['T1'])
        printed = json.loads(run_main('--root', str(self.root), 'task:T1')[1])
        self.assertEqual(({'available': True, 'omitted': 0, 'nodes': ['task:T1']}),
                         {'available': block['available'], 'omitted': block['omitted'], 'nodes': list(block['packs'])})
        self.assertEqual(self.without_time(printed), self.without_time(block['packs']['task:T1']))

    def test_a_task_that_is_no_graph_node_has_no_pack_and_the_block_stays_available(self):
        self.assertEqual({'available': True, 'packs': {}, 'omitted': 0}, self.block_for(['no-such-task']))

    def test_repeated_ids_are_one_task(self):
        block = self.block_for(['T1', 'T1', 'T1'])
        self.assertEqual((['task:T1'], 0), (list(block['packs']), block['omitted']))

    def test_the_lookup_limit_is_ten(self):
        self.assertEqual(10, context_pack.LOOKUP_LIMIT)

    def test_only_the_first_ten_distinct_ids_are_looked_up_and_the_rest_are_counted_as_omitted(self):
        names = [f'N{number:02}' for number in range(12)]
        self.add_tasks(*names)
        with patch.object(self.graph, 'build_context_packs', wraps=self.graph.build_context_packs) as builder:
            block = self.block_for(names + names)
        self.assertEqual(['task:' + name for name in names[:10]], builder.call_args.args[3])
        self.assertEqual((names[:10], 2), ([key.removeprefix('task:') for key in block['packs']], block['omitted']))

    def test_an_unknown_id_among_the_first_ten_uses_its_slot(self):
        names = [f'N{number:02}' for number in range(11)]
        self.add_tasks(*(name for name in names if name != 'N03'))
        block = self.block_for(names)
        self.assertEqual((9, 1, True), (len(block['packs']), block['omitted'], block['available']))
        self.assertNotIn('task:N03', block['packs'])
        self.assertNotIn('task:N10', block['packs'])

    def test_exactly_ten_distinct_ids_omit_nothing(self):
        names = [f'N{number:02}' for number in range(10)]
        self.add_tasks(*names)
        block = self.block_for(names)
        self.assertEqual((10, 0), (len(block['packs']), block['omitted']))

    def test_the_dashboard_and_the_graph_are_built_once_for_several_tasks(self):
        self.add_tasks('N01', 'N02')
        with (patch.object(self.server, 'build_dashboard', wraps=self.server.build_dashboard) as dashboard,
              patch.object(self.graph, 'build_context_graph', wraps=self.graph.build_context_graph) as graph):
            block = self.block_for(['T1', 'N01', 'N02'])
        self.assertEqual((3, 1, 1), (len(block['packs']), dashboard.call_count, graph.call_count))

    def test_a_task_that_is_no_node_builds_no_graph(self):
        with patch.object(self.graph, 'build_context_graph') as graph:
            self.block_for(['no-such-task'])
        graph.assert_not_called()

    def test_building_the_block_leaves_the_mailbox_tree_byte_identical(self):
        before = self.tree_snapshot()
        self.block_for(['T1'])
        self.assertEqual(before, self.tree_snapshot())


class ContextBlockNothingNamedTests(ContextBlockCase):
    def test_no_task_gives_the_empty_available_block_without_loading_the_dashboard_code(self):
        with patch.object(context_pack, 'load_dashboard_modules') as loader:
            block = self.block_for([])
        loader.assert_not_called()
        self.assertEqual({'available': True, 'packs': {}, 'omitted': 0}, block)


class ContextBlockFailureTests(ContextBlockCase):
    UNAVAILABLE = {'available': False, 'packs': {}, 'omitted': 0}

    def assertUnavailableWithoutRawText(self, block):
        self.assertEqual(self.UNAVAILABLE, block)
        for leak in LEAKS:
            self.assertNotIn(leak, json.dumps(block))

    def test_dashboard_code_that_cannot_be_located_gives_the_unavailable_block(self):
        missing = FileNotFoundError('dispatch_gate.py was not found above /opt/dashboard/scripts/context_pack.py')
        with patch.object(context_pack, 'gate_directory', side_effect=missing):
            self.assertUnavailableWithoutRawText(self.block_for(['T1']))

    def test_a_loader_refusal_gives_the_unavailable_block(self):
        with patch.object(context_pack, 'load_dashboard_modules', side_effect=context_pack.Refusal(context_pack.CODE_NOT_LOADED, 1)):
            self.assertUnavailableWithoutRawText(self.block_for(['T1']))

    def test_a_dashboard_build_that_fails_with_a_raw_text_gives_the_unavailable_block(self):
        for failure in (OSError(RAW_FAILURE), RuntimeError(TRACEBACK_TEXT)):
            with self.subTest(failure=type(failure).__name__), patch.object(self.server, 'build_dashboard', side_effect=failure):
                self.assertUnavailableWithoutRawText(self.block_for(['T1']))

    def test_a_graph_build_that_fails_gives_the_unavailable_block(self):
        with patch.object(self.graph, 'build_context_packs', side_effect=OSError(RAW_FAILURE)):
            self.assertUnavailableWithoutRawText(self.block_for(['T1']))

    def test_a_pack_that_cannot_be_printed_gives_the_unavailable_block(self):
        for bad in (float('nan'), float('inf')):
            with self.subTest(bad=bad), patch.object(self.graph, 'build_context_packs', return_value={'task:T1': {'bad': bad}}):
                self.assertUnavailableWithoutRawText(self.block_for(['T1']))

    def test_a_block_with_unprintable_data_is_rejected_and_a_printable_one_is_not(self):
        context_pack.require_printable({'available': True, 'packs': {'task:T1': {'x': 1}}, 'omitted': 0})
        with self.assertRaises(ValueError):
            context_pack.require_printable({'packs': {'task:T1': {'x': float('nan')}}})

    def test_the_unavailable_block_is_a_fresh_object_each_time(self):
        first = context_pack.unavailable_context()
        first['packs']['task:x'] = {}
        self.assertEqual(self.UNAVAILABLE, context_pack.unavailable_context())


class LoadDashboardModulesTests(ContextPackCase):
    def test_it_returns_the_server_and_the_context_graph(self):
        server, context_graph = context_pack.load_dashboard_modules()
        self.assertEqual(('server', 'context_graph'), (server.__name__, context_graph.__name__))

    def test_load_context_for_returns_the_servers_reader(self):
        self.assertIs(context_pack.load_dashboard_modules()[0].context_for, context_pack.load_context_for())

    def test_a_failure_to_locate_or_import_is_the_fixed_refusal_without_the_raw_text(self):
        missing = FileNotFoundError('dispatch_gate.py was not found above /opt/dashboard/scripts/context_pack.py')
        for name, guard in (('locate', patch.object(context_pack, 'gate_directory', side_effect=missing)),
                            ('import', patch.dict(sys.modules, {'context_graph': None}))):
            with self.subTest(name), guard, self.assertRaises(context_pack.Refusal) as caught:
                context_pack.load_dashboard_modules()
            self.assertEqual((context_pack.CODE_NOT_LOADED, 1, None), (caught.exception.line, caught.exception.code, caught.exception.__cause__))
            self.assertTrue(caught.exception.__suppress_context__)


class MarkContextNodesTests(unittest.TestCase):
    BLOCK = {'available': True, 'packs': {'task:a': {}, 'task:c': {}}, 'omitted': 0}

    def test_the_task_node_is_the_task_prefix_and_the_id(self):
        self.assertEqual('task:abc', context_pack.task_node('abc'))

    def test_only_items_whose_task_has_a_pack_gain_a_context_node(self):
        items = [{'task': 'a'}, {'task': 'b'}, {'task': 'c', 'other': 1}]
        context_pack.mark_context_nodes(items, lambda item: item['task'], self.BLOCK)
        self.assertEqual([{'task': 'a', 'context_node': 'task:a'}, {'task': 'b'}, {'task': 'c', 'other': 1, 'context_node': 'task:c'}], items)

    def test_the_unavailable_block_marks_nothing(self):
        items = [{'task': 'a'}]
        context_pack.mark_context_nodes(items, lambda item: item['task'], context_pack.unavailable_context())
        self.assertEqual([{'task': 'a'}], items)


class ScriptSeamTests(ContextPackCase):
    def run_script(self, *arguments):
        return subprocess.run([sys.executable, '-B', context_pack.__file__, *arguments], capture_output=True, text=True)

    def test_the_script_prints_the_pack_for_a_named_root(self):
        result = self.run_script('--root', str(self.root), 'task:T1')
        self.assertEqual((0, 'task:T1', ''), (result.returncode, json.loads(result.stdout)['node']['id'], result.stderr))

    def test_the_script_refuses_a_malformed_node_without_a_traceback(self):
        result = self.run_script('--root', str(self.root), 'bad')
        self.assertEqual((2, '', context_pack.INVALID_NODE + '\n'), (result.returncode, result.stdout, result.stderr))


if __name__ == '__main__':
    unittest.main()
