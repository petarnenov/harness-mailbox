import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == '__main__':
    unittest.main()
