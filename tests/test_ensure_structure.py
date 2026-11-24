"""Exercise the mailbox structure script against temporary mailbox fixtures."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = (Path(__file__).resolve().parents[1] / 'workspace/.agents/skills'
          / 'mailbox-register-sessions/scripts/ensure_structure.py')
LEAK_MARKERS = ('Traceback', 'Expecting', 'Errno', 'codec', 'Unterminated', 'File "')
PROJECT_DIRECTORIES = ('inbox', 'outbox', 'archive', 'tasks')


class EnsureStructureTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = temporary.name
        self.root = Path(self.temp).resolve() / 'mailbox'
        self.root.mkdir()
        for name in ('AGENTS.md', 'README.md'):
            (self.root / name).write_text('x')
        self.write_registry({'schema_version': 1, 'projects': [{'id': 'alpha'}, {'id': 'beta_2'}]})

    def write_registry(self, value):
        (self.root / 'projects.json').write_text(json.dumps(value))

    def run_script(self, root=None):
        """Run the script as a command; return (exit code, stdout, stderr text incl. exit message)."""
        out, err = io.StringIO(), io.StringIO()
        argv = [str(SCRIPT), '--root', str(root or self.root)]
        code = 0
        with (patch.object(sys, 'argv', argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            try:
                runpy.run_path(str(SCRIPT), run_name='__main__')
            except SystemExit as exit_status:
                code = exit_status.code
        message = '' if code in (0, None) else (code if isinstance(code, str) else '')
        return (code or 0), out.getvalue(), err.getvalue() + str(message)

    def assert_refused(self, result):
        code, stdout, stderr = result
        self.assertNotEqual(code, 0)
        report = stdout + stderr
        self.assertEqual(len(report.strip().splitlines()), 1, report)
        for marker in (*LEAK_MARKERS, self.temp, str(self.root)):
            self.assertNotIn(marker, report)
        return report

    def test_creates_every_missing_directory_and_reports_them(self):
        code, stdout, _ = self.run_script()
        report = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(report['root'], str(self.root))
        self.assertEqual(report['projects'], ['alpha', 'beta_2'])
        expected = {'sessions', 'projects', 'epics', 'discovery', 'templates',
                    'inbox/orchestrator', 'outbox/orchestrator', 'archive/orchestrator'}
        expected |= {f'{kind}/{key}' for kind in PROJECT_DIRECTORIES for key in ('alpha', 'beta_2')}
        self.assertEqual(set(report['created_directories']), expected)
        for relative in expected:
            self.assertTrue((self.root / relative).is_dir(), relative)

    def test_second_run_creates_nothing_and_keeps_existing_files(self):
        self.run_script()
        task = self.root / 'tasks/alpha/keep.json'
        task.write_text('record')
        code, stdout, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)['created_directories'], [])
        self.assertEqual(task.read_text(), 'record')

    def test_registry_shapes_that_crash_at_base_are_refused_as_malformed(self):
        shapes = {
            'keyed object': {'projects': {'alpha': {'id': 'alpha'}}},
            'null entry': {'projects': [None, {'id': 'alpha'}]},
            'null projects': {'projects': None},
            'string projects': {'projects': 'alpha'},
            'number projects': {'projects': 5},
        }
        for name, registry in shapes.items():
            with self.subTest(name):
                self.write_registry(registry)
                self.assertIn('malformed registry', self.assert_refused(self.run_script()))
                self.assertFalse((self.root / 'sessions').exists())

    def test_other_unusable_registry_shapes_are_refused_cleanly(self):
        shapes = {
            'top level list': [{'id': 'alpha'}],
            'top level null': None,
            'missing projects': {'schema_version': 1},
            'entry without id': {'projects': [{'name': 'x'}]},
            'string entry': {'projects': ['alpha']},
            'non-string id': {'projects': [{'id': 5}]},
            'list id': {'projects': [{'id': ['a']}, {'id': ['a']}]},
        }
        for name, registry in shapes.items():
            with self.subTest(name):
                self.write_registry(registry)
                self.assert_refused(self.run_script())
                self.assertFalse((self.root / 'sessions').exists())

    def test_undecodable_registry_is_refused(self):
        for name, content in {'invalid json': b'{"projects": [', 'empty': b'', 'not utf-8': b'\xff\xfe'}.items():
            with self.subTest(name):
                (self.root / 'projects.json').write_bytes(content)
                self.assert_refused(self.run_script())

    def test_unsafe_or_duplicate_project_ids_are_refused(self):
        for name, ids in {'duplicate': ['a', 'a'], 'traversal': ['../x'], 'uppercase': ['Alpha']}.items():
            with self.subTest(name):
                self.write_registry({'projects': [{'id': value} for value in ids]})
                self.assert_refused(self.run_script())
                self.assertFalse((self.root / 'sessions').exists())

    def test_missing_mailbox_files_are_refused_without_naming_a_path(self):
        for filename in ('AGENTS.md', 'README.md', 'projects.json'):
            with self.subTest(filename):
                (self.root / filename).rename(self.root / 'moved')
                self.assert_refused(self.run_script())
                (self.root / 'moved').rename(self.root / filename)

    def test_missing_mailbox_root_is_refused(self):
        self.assert_refused(self.run_script(root=self.root / 'absent'))

    def test_a_file_in_place_of_a_directory_is_refused_without_naming_a_path(self):
        (self.root / 'epics').write_text('file')
        self.assert_refused(self.run_script())
        self.assertFalse((self.root / 'sessions').exists())

    def test_a_directory_link_leaving_the_mailbox_is_refused_without_naming_a_path(self):
        outside = Path(self.temp) / 'outside'
        outside.mkdir()
        (self.root / 'sessions').symlink_to(outside)
        self.assert_refused(self.run_script())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.root / 'epics').exists())


if __name__ == '__main__':
    unittest.main()
