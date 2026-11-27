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
