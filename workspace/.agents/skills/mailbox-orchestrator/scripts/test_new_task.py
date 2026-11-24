import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from new_task import create_task, main

LEAK_MARKERS = ('Traceback', 'Expecting', 'Errno', 'codec', 'Unterminated')
FIXED_ID = uuid.UUID('11111111-1111-4111-8111-111111111111')


def run_main(root, project, text, *extra):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = main(['--root', str(root), '--project', project, *extra, text])
    return status, output.getvalue()


def refuse_with_os_error(*_args, **_kwargs):
    raise OSError(28, 'refused')


class MailboxFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'mailbox'
        self.repo = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.repo.mkdir()
        self.project_entry = {
            'id': 'demo', 'repository_path': str(self.repo), 'working_directory': str(self.repo)}
        self.write_registry({'schema_version': 1, 'projects': [self.project_entry]})

    def tearDown(self):
        self.temp.cleanup()

    def write_registry(self, value):
        (self.root / 'projects.json').write_text(json.dumps(value))

    def add_loop(self, name, skill_dir='.claude/skills'):
        skill = self.repo / skill_dir / name / 'SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('loop')
        return skill

    def task_files(self):
        return sorted(path for path in (self.root / 'tasks').rglob('*') if path.is_file())

    def refused(self, output):
        """Assert one code-written JSON refusal line; return the parsed reply."""
        status, printed = output
        self.assertEqual(status, 1)
        lines = printed.splitlines()
        self.assertEqual(len(lines), 1)
        reply = json.loads(lines[0])
        self.assertEqual(set(reply), {'created', 'error'})
        self.assertIs(reply['created'], False)
        self.assertIsInstance(reply['error'], str)
        self.assertTrue(reply['error'].strip())
        for marker in (*LEAK_MARKERS, self.temp.name, str(Path(self.temp.name).resolve())):
            self.assertNotIn(marker, reply['error'])
        return reply

    def refused_run(self, project='demo', text='work'):
        return self.refused(run_main(self.root, project, text))


class NewTaskTest(MailboxFixture):
    def test_creates_task_with_uuid_verbatim_text_and_discovered_loop(self):
        skill = self.add_loop('demo.loop')
        text = 'Fix the login timeout\nKeep the session for 30 minutes.'

        task, path = create_task(self.root, 'demo', text)

        self.assertEqual(str(uuid.UUID(task['id'])), task['id'])
        self.assertEqual(path, self.root / 'tasks' / 'demo' / f"{task['id']}.json")
        self.assertEqual(json.loads(path.read_text()), task)
        self.assertEqual(task['description'], text)
        self.assertEqual(task['title'], 'Fix the login timeout')
        self.assertEqual(task['source'], 'free_text')
        self.assertEqual((task['required_loop_skill'], task['required_loop_path']), ('demo.loop', str(skill.resolve())))
        self.assertEqual(task['blockers'], [])

    def test_each_task_gets_a_distinct_id(self):
        self.add_loop('demo.loop')
        first, _ = create_task(self.root, 'demo', 'one')
        second, _ = create_task(self.root, 'demo', 'two')
        self.assertNotEqual(first['id'], second['id'])

    def test_explicit_title_and_long_first_line(self):
        self.add_loop('demo.loop')
        titled, _ = create_task(self.root, 'demo', 'body', title='Custom')
        long_line, _ = create_task(self.root, 'demo', 'x' * 200)
        self.assertEqual(titled['title'], 'Custom')
        self.assertEqual(len(long_line['title']), 80)

    def test_missing_loop_skill_becomes_blocker(self):
        task, _ = create_task(self.root, 'demo', 'work')
        self.assertIsNone(task['required_loop_skill'])
        self.assertIn('none found', task['blockers'][0])

    def test_ambiguous_loop_skill_becomes_blocker(self):
        self.add_loop('a.loop')
        self.add_loop('b.loop', '.agents/skills')
        task, _ = create_task(self.root, 'demo', 'work')
        self.assertIn('ambiguous: a.loop, b.loop', task['blockers'][0])

    def test_rejects_unknown_project_and_empty_text(self):
        with self.assertRaisesRegex(ValueError, "Unknown project 'other'"):
            create_task(self.root, 'other', 'work')
        with self.assertRaisesRegex(ValueError, 'must not be empty'):
            create_task(self.root, 'demo', '   ')
        self.assertFalse((self.root / 'tasks').exists())


class PublicationTest(MailboxFixture):
    def snapshot_before(self, name, snapshots):
        real = getattr(os, name)

        def recording(*args, **kwargs):
            snapshots.append((name, sorted(self.root.glob('tasks/*/*.json'))))
            return real(*args, **kwargs)
        return patch.object(os, name, recording)

    def occupy_identity(self):
        existing = self.root / 'tasks' / 'demo' / f'{FIXED_ID}.json'
        existing.parent.mkdir(parents=True)
        existing.write_text('original')
        return existing

    def test_no_file_matching_the_task_glob_exists_before_publication(self):
        snapshots = []
        with (self.snapshot_before('fsync', snapshots), self.snapshot_before('link', snapshots),
              self.snapshot_before('rename', snapshots), self.snapshot_before('replace', snapshots)):
            _, path = create_task(self.root, 'demo', 'work')
        self.assertTrue(snapshots)
        for name, visible in snapshots:
            self.assertEqual(visible, [], f'{name} ran while a task-shaped file was visible')
        self.assertEqual(sorted(self.root.glob('tasks/*/*.json')), [path])

    def test_only_the_published_task_remains_in_the_project_directory(self):
        _, path = create_task(self.root, 'demo', 'work')
        self.assertEqual(self.task_files(), [path])

    def test_a_failed_publication_leaves_no_file_behind(self):
        failing = patch.multiple(os, link=refuse_with_os_error, rename=refuse_with_os_error,
                                 replace=refuse_with_os_error)
        with failing, self.assertRaises(OSError):
            create_task(self.root, 'demo', 'work')
        self.assertEqual(self.task_files(), [])

    def test_a_failed_sync_leaves_no_file_behind(self):
        with patch.object(os, 'fsync', refuse_with_os_error), self.assertRaises(OSError):
            create_task(self.root, 'demo', 'work')
        self.assertEqual(self.task_files(), [])

    def test_a_failed_sync_ends_the_command_with_a_clean_refusal(self):
        with patch.object(os, 'fsync', refuse_with_os_error):
            self.refused(run_main(self.root, 'demo', 'work'))
        self.assertEqual(self.task_files(), [])

    def test_an_existing_task_record_is_never_overwritten(self):
        existing = self.occupy_identity()
        with patch('new_task.uuid.uuid4', return_value=FIXED_ID), self.assertRaises(OSError):
            create_task(self.root, 'demo', 'work')
        self.assertEqual(existing.read_text(), 'original')
        self.assertEqual(self.task_files(), [existing])

    def test_a_colliding_identity_is_refused_without_raw_system_text(self):
        existing = self.occupy_identity()
        with patch('new_task.uuid.uuid4', return_value=FIXED_ID):
            self.refused(run_main(self.root, 'demo', 'work'))
        self.assertEqual(existing.read_text(), 'original')

    def test_published_task_keeps_its_shape_identity_and_utc_timestamp(self):
        task, path = create_task(self.root, 'demo', 'work')
        self.assertEqual(json.loads(path.read_text()), task)
        self.assertEqual(set(task), {
            'id', 'project', 'source', 'title', 'description', 'acceptance_criteria', 'status',
            'owner_session_id', 'depends_on', 'required_loop_skill', 'required_loop_path',
            'allowed_paths', 'evidence', 'blockers', 'created_at', 'updated_at'})
        self.assertEqual(task['status'], 'pending')
        self.assertEqual(str(uuid.UUID(task['id'], version=4)), task['id'])
        self.assertEqual(datetime.fromisoformat(task['created_at']).utcoffset(), timedelta(0))
        self.assertEqual(task['created_at'], task['updated_at'])


class RefusalTest(MailboxFixture):
    def test_registry_shapes_that_crash_at_base_are_refused_as_malformed(self):
        shapes = {
            'keyed object': {'projects': {'demo': self.project_entry}},
            'null entry': {'projects': [None, self.project_entry]},
            'null projects': {'projects': None},
            'string projects': {'projects': 'demo'},
            'number projects': {'projects': 5},
        }
        for name, registry in shapes.items():
            with self.subTest(name):
                self.write_registry(registry)
                self.assertIn('malformed registry', self.refused_run()['error'])
                self.assertEqual(self.task_files(), [])

    def test_other_unusable_registry_shapes_are_refused_cleanly(self):
        shapes = {
            'string entry': {'projects': ['demo']},
            'top level list': [self.project_entry],
            'top level null': None,
            'missing projects': {'schema_version': 1},
        }
        for name, registry in shapes.items():
            with self.subTest(name):
                self.write_registry(registry)
                self.refused_run()
                self.assertEqual(self.task_files(), [])

    def test_missing_registry_is_refused(self):
        (self.root / 'projects.json').unlink()
        self.refused_run()

    def test_undecodable_registry_is_refused(self):
        contents = {'invalid json': b'{"projects": [', 'empty': b'', 'not utf-8': b'\xff\xfe{"projects"'}
        for name, content in contents.items():
            with self.subTest(name):
                (self.root / 'projects.json').write_bytes(content)
                self.refused_run()

    def test_registry_that_is_a_directory_is_refused(self):
        (self.root / 'projects.json').unlink()
        (self.root / 'projects.json').mkdir()
        self.refused_run()

    def test_project_record_without_its_paths_is_refused_without_the_missing_key_text(self):
        self.write_registry({'projects': [{'id': 'demo'}]})
        self.assertNotEqual(self.refused_run()['error'], "'repository_path'")

    def test_unknown_project_is_refused_with_the_registered_ids(self):
        reply = self.refused_run(project='other')
        self.assertIn("Unknown project 'other'", reply['error'])
        self.assertIn('demo', reply['error'])

    def test_unknown_project_beside_an_entry_without_an_id_is_refused(self):
        self.write_registry({'projects': [{'name': 'no id'}]})
        self.assertIn("Unknown project 'demo'", self.refused_run()['error'])

    def test_entries_without_an_id_stay_accepted_beside_valid_ones(self):
        self.write_registry({'schema_version': 1, 'note': 'x', 'projects': [{'name': 'no id'}, self.project_entry]})
        status, printed = run_main(self.root, 'demo', 'work')
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(printed)['created'])

    def test_an_unwritable_task_directory_is_refused(self):
        (self.root / 'tasks').write_text('not a directory')
        self.refused_run()

    def test_empty_text_is_refused(self):
        self.assertIn('empty', self.refused_run(text='  ')['error'])
        self.assertFalse((self.root / 'tasks').exists())


class CommandLineTest(MailboxFixture):
    def test_success_prints_one_json_line_with_the_published_location(self):
        self.add_loop('demo.loop')
        status, printed = run_main(self.root, 'demo', 'work\nmore', '--title', 'Named')
        reply = json.loads(printed)
        self.assertEqual(status, 0)
        self.assertEqual(len(printed.splitlines()), 1)
        self.assertEqual(set(reply), {'created', 'id', 'project', 'path', 'blockers'})
        self.assertIs(reply['created'], True)
        self.assertEqual(reply['project'], 'demo')
        self.assertEqual(reply['blockers'], [])
        self.assertEqual(Path(reply['path']), self.root.resolve() / 'tasks' / 'demo' / f"{reply['id']}.json")
        self.assertEqual(json.loads(Path(reply['path']).read_text())['title'], 'Named')

    def test_a_dash_reads_the_task_text_from_standard_input(self):
        with patch.object(sys, 'stdin', io.StringIO('from stdin')):
            status, printed = run_main(self.root, 'demo', '-')
        self.assertEqual(status, 0)
        task = json.loads(Path(json.loads(printed)['path']).read_text())
        self.assertEqual(task['description'], 'from stdin')

    def test_an_unresolved_loop_skill_is_reported_as_a_blocker(self):
        _, printed = run_main(self.root, 'demo', 'work')
        self.assertIn('none found', json.loads(printed)['blockers'][0])


if __name__ == '__main__':
    unittest.main()
