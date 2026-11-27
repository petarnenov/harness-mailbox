import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import dispatch_task
from mailbox_layout import gate_directory, mailbox_root

SKILL_SCRIPTS = Path('.agents/skills/mailbox-orchestrator/scripts')


class MailboxLayoutTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mailbox = Path(self.temp.name).resolve() / 'mailbox'

    def script(self, skills_parent=''):
        folder = self.mailbox / skills_parent / SKILL_SCRIPTS
        folder.mkdir(parents=True)
        return folder / 'jira_sync.py'

    def put(self, relative):
        path = self.mailbox / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')

    def test_installed_skills_find_the_mailbox_holding_the_registry(self):
        self.put('projects.json')
        self.assertEqual(self.mailbox, mailbox_root(self.script()))

    def test_skills_in_the_application_checkout_find_the_mailbox_above_workspace(self):
        self.put('projects.json')
        self.assertEqual(self.mailbox, mailbox_root(self.script('workspace')))

    def test_nearest_registry_wins(self):
        self.put('projects.json')
        self.put('workspace/projects.json')
        self.assertEqual(self.mailbox / 'workspace', mailbox_root(self.script('workspace')))

    def test_without_a_registry_the_installed_layout_is_assumed(self):
        self.assertEqual(self.mailbox / 'workspace', mailbox_root(self.script('workspace')))

    def test_gate_is_found_in_the_dashboard_of_an_installed_mailbox(self):
        self.put('dashboard/dispatch_gate.py')
        self.assertEqual(self.mailbox / 'dashboard', gate_directory(self.script()))

    def test_gate_is_found_at_the_top_of_the_application_checkout(self):
        self.put('dispatch_gate.py')
        self.assertEqual(self.mailbox, gate_directory(self.script('workspace')))

    def test_missing_gate_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            gate_directory(self.script())


class DispatchTaskTest(unittest.TestCase):
    def test_gate_gets_the_mailbox_root_when_none_is_named(self):
        self.assertEqual(['--root', str(dispatch_task.MAILBOX), 'check', '--task', 't.json'],
                         dispatch_task.with_mailbox_root(['check', '--task', 't.json']))

    def test_named_root_is_kept(self):
        for named in (['--root', '/elsewhere', 'check'], ['--root=/elsewhere', 'check']):
            self.assertEqual(named, dispatch_task.with_mailbox_root(named))

    def test_script_reaches_the_gate_of_its_own_checkout(self):
        with tempfile.TemporaryDirectory() as empty_mailbox:
            command = [sys.executable, '-B', dispatch_task.__file__, '--root', empty_mailbox, 'check', '--task', 'none.json']
            refusal = subprocess.run(command, capture_output=True, text=True)
        self.assertIn('"allowed": false', refusal.stdout)
        self.assertNotIn('Traceback', refusal.stderr)


class DispatchTaskMainTest(unittest.TestCase):
    RAW_FAILURE = "[Errno 13] Permission denied: '/data/dispatch-gates/T1/x.json'"
    LEAKS = ('Traceback', 'Errno', 'Permission denied', '/data/')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mailbox = Path(self.temp.name).resolve() / 'mailbox'
        self.mailbox.mkdir()

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dispatch_task.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def register(self):
        (self.mailbox / 'projects.json').write_text('{}')

    def assertFixedFailure(self, result, line):
        code, out, err = result
        self.assertEqual((1, '', line + '\n'), (code, out, err))
        for leak in self.LEAKS:
            self.assertNotIn(leak, err)

    def test_without_a_root_and_without_a_registry_the_gate_is_never_located_and_nothing_is_written(self):
        with patch.object(dispatch_task, 'MAILBOX', self.mailbox), patch.object(dispatch_task, 'load_gate_main') as locate:
            self.assertFixedFailure(self.run_main('check', '--task', 't.json'), dispatch_task.NO_REGISTRY)
        locate.assert_not_called()
        self.assertEqual([], list(self.mailbox.rglob('*')))

    def test_a_named_root_reaches_the_gate_unchanged_even_without_a_registry(self):
        for arguments in (['--root', '/elsewhere', 'check'], ['--root=/elsewhere', 'check']):
            gate = Mock(return_value=0)
            with self.subTest(arguments=arguments), patch.object(dispatch_task, 'MAILBOX', self.mailbox), \
                    patch.object(dispatch_task, 'load_gate_main', return_value=gate):
                self.assertEqual((0, '', ''), self.run_main(*arguments))
            gate.assert_called_once_with(arguments)

    def test_a_default_root_holding_a_registry_is_passed_to_the_gate(self):
        self.register()
        gate = Mock(return_value=0)
        with patch.object(dispatch_task, 'MAILBOX', self.mailbox), patch.object(dispatch_task, 'load_gate_main', return_value=gate):
            self.assertEqual(0, self.run_main('check', '--task', 't.json')[0])
        gate.assert_called_once_with(['--root', str(self.mailbox), 'check', '--task', 't.json'])

    def test_the_gate_exit_code_passes_through_unchanged(self):
        for exit_code in (0, 1, 2, 3):
            with self.subTest(exit_code=exit_code), patch.object(dispatch_task, 'load_gate_main', return_value=Mock(return_value=exit_code)):
                self.assertEqual(exit_code, self.run_main('--root', str(self.mailbox), 'check')[0])

    def test_a_gate_that_cannot_be_loaded_is_the_fixed_line(self):
        with patch.object(dispatch_task, 'load_gate_main', return_value=None):
            self.assertFixedFailure(self.run_main('--root', str(self.mailbox), 'check'), dispatch_task.GATE_NOT_LOADED)

    def test_an_exception_escaping_the_gate_is_the_fixed_line_without_its_text(self):
        for failure in (OSError(self.RAW_FAILURE), RuntimeError('Traceback (most recent call last): File "/data/dispatch_gate.py", line 9')):
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(dispatch_task, 'load_gate_main', return_value=Mock(side_effect=failure)):
                self.assertFixedFailure(self.run_main('--root', str(self.mailbox), 'check'), dispatch_task.GATE_FAILED)

    def test_the_refusal_texts_are_the_frozen_lines(self):
        self.assertEqual(('dispatch_task: no mailbox registry found; name the mailbox with --root',
                          'dispatch_task: dispatch gate could not be loaded', 'dispatch_task: dispatch gate failed'),
                         (dispatch_task.NO_REGISTRY, dispatch_task.GATE_NOT_LOADED, dispatch_task.GATE_FAILED))

    def test_load_gate_main_returns_the_gate_main_of_the_nearest_dashboard(self):
        with patch.object(sys, 'path', list(sys.path)):
            gate_main = dispatch_task.load_gate_main()
        self.assertEqual('dispatch_gate', gate_main.__module__)

    def test_load_gate_main_is_none_when_no_gate_is_found_or_it_cannot_be_imported(self):
        with patch.object(dispatch_task, 'gate_directory', side_effect=FileNotFoundError('/opt/dashboard/dispatch_gate.py missing')):
            self.assertIsNone(dispatch_task.load_gate_main())
        broken_gate = types.ModuleType('dispatch_gate')
        with patch.dict(sys.modules, {'dispatch_gate': broken_gate}), patch.object(sys, 'path', list(sys.path)):
            self.assertIsNone(dispatch_task.load_gate_main())

    def test_the_script_copied_outside_a_mailbox_refuses_without_creating_a_gate_directory(self):
        scripts = self.mailbox / 'workspace' / SKILL_SCRIPTS
        scripts.mkdir(parents=True)
        for name in ('dispatch_task.py', 'mailbox_layout.py'):
            shutil.copy(Path(dispatch_task.__file__).with_name(name), scripts / name)
        result = subprocess.run([sys.executable, '-B', str(scripts / 'dispatch_task.py'), 'check', '--task', 'none.json'],
                                capture_output=True, text=True)
        self.assertEqual((1, '', dispatch_task.NO_REGISTRY + '\n'), (result.returncode, result.stdout, result.stderr))
        self.assertFalse((self.mailbox / 'workspace' / 'dispatch-gates').exists())


if __name__ == '__main__':
    unittest.main()
