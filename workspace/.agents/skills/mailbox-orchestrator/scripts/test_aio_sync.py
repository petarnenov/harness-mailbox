import contextlib
import io
import json
import runpy
import sys
import tempfile
import unittest
import urllib.error
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import aio_sync
from aio_sync import main

EPIC, STORY = 'Data Insights MCP (GEO-1)', 'GEO-2 Story'
JUNIT = """<testsuite>
  <testcase classname="com.x.AdmissionFilterTest" name="denies()"/>
  <testcase classname="com.x.ApplicationTest" name="challenge"><failure/></testcase>
  <testcase classname="com.x.ApplicationTest" name="skipped"><skipped/></testcase>
  <testcase classname="com.x.RateLimitHttpTest" name="[1] MODERN"/>
  <testcase classname="com.x.RateLimitHttpTest" name="[2] LEGACY"/>
</testsuite>"""


class FakeAio:
    def __init__(self, folders=None, cases=None, cycles=None):
        self.folders, self.cases, self.cycles = folders or [], cases or [], cycles or []
        self.writes = []

    def call(self, method, path, body=None):
        if method == 'GET' and path == '/testcase/folder':
            return self.folders
        if path == '/testcase/search':
            folder = body['folderID']['list'][0]
            return {'items': [c for c in self.cases if c['folder'] == folder and body['title']['value'] in c['title']]}
        if path == '/testcycle/search':
            return {'items': [c for c in self.cycles if c['title'] == body['title']['value']]}
        self.writes.append((method, path, body))
        if path == '/testcase/folder/hierarchy':
            return {'ID': 77}
        if path == '/testcase':
            return {'key': 'GEO-TC-1'}
        if path == '/testcycle/detail':
            return {'key': 'GEO-CY-9'}
        return {}

    def upload(self, path, file_path, fields=None):
        self.writes.append(('UPLOAD', path, Path(file_path).name))


class AioSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        (self.dir / 'junit.xml').write_text(JUNIT)
        (self.dir / 'coverage.txt').write_text('AdmissionFilter 69/69')
        self.manifest = {'issue': 'GEO-3', 'epic_folder': EPIC, 'story_folder': STORY, 'revision': 'abc',
                         'criteria': [{'id': '3-AC1', 'title': 'Denied gets 403', 'type': 'Security',
                                       'steps': [{'step': 'call', 'expected': '403'}],
                                       'tests': ['AdmissionFilterTest#denies']}],
                         'junit': [str(self.dir / 'junit.xml')],
                         'coverage': {'threshold': 85, 'summary_file': str(self.dir / 'coverage.txt')}}

    def tearDown(self):
        self.temp.cleanup()

    def run_sync(self, fake, *args):
        path = self.dir / 'manifest.json'
        path.write_text(json.dumps(self.manifest))
        return main(['--root', str(self.dir), args[0], '--manifest', str(path), *args[1:]], client=fake)

    def existing_tree(self):
        return [{'ID': 10, 'name': EPIC, 'children': [{'ID': 11, 'name': STORY, 'children': []}]}]

    def test_dry_run_plans_folder_and_case_without_writing(self):
        fake = FakeAio()
        plan = self.run_sync(fake, 'cases')
        self.assertEqual([p['action'] for p in plan], ['create_folder', 'create_case'])
        self.assertEqual(fake.writes, [])

    def test_apply_creates_folder_and_linked_case(self):
        fake = FakeAio()
        self.run_sync(fake, 'cases', '--apply')
        (_, _, folder), (_, path, case) = fake.writes
        self.assertEqual(folder['folderHierarchy'], [EPIC, STORY])
        self.assertEqual(path, '/testcase')
        self.assertEqual(case['title'], '[3-AC1] Denied gets 403')
        self.assertEqual(case['jiraRequirementIDs'], ['GEO-3'])
        self.assertEqual(case['automationKey'], 'AdmissionFilterTest#denies')
        self.assertEqual(case['folder'], {'ID': 77})
        self.assertEqual(case['scriptType'], {'ID': 1})

    def test_apply_updates_an_existing_case_instead_of_duplicating(self):
        fake = FakeAio(self.existing_tree(), [{'key': 'GEO-TC-5', 'folder': 11, 'title': '[3-AC1] old'}])
        self.run_sync(fake, 'cases', '--apply')
        self.assertEqual([(m, p) for m, p, _ in fake.writes], [('PATCH', '/testcase/GEO-TC-5/detail')])

    def test_cycle_records_junit_status_and_attaches_evidence(self):
        fake = FakeAio(self.existing_tree(), [{'key': 'GEO-TC-5', 'folder': 11, 'title': '[3-AC1] x'}])
        plan = self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.assertTrue(plan[0]['title'].endswith('Sprint 21 GEO-3') and plan[0]['title'].startswith('AI '))
        run = next(w for w in fake.writes if 'testrun' in w[1])
        self.assertEqual(run[2]['testRunStatus'], 'Passed')
        self.assertEqual([w[2] for w in fake.writes if w[0] == 'UPLOAD'], ['GEO-3-abc-evidence.zip'])
        self.assertEqual(plan[-1]['contains'], ['junit.xml', 'coverage.txt'])

    def test_cycle_marks_a_criterion_failed_when_any_test_failed(self):
        self.manifest['criteria'][0]['tests'].append('ApplicationTest#challenge')
        fake = FakeAio(self.existing_tree(), [{'key': 'GEO-TC-5', 'folder': 11, 'title': '[3-AC1] x'}])
        plan = self.run_sync(fake, 'cycle', '--sprint', '21')
        self.assertEqual(next(p for p in plan if p['action'] == 'record_run')['status'], 'Failed')
        self.assertEqual(fake.writes, [])

    def test_cycle_refuses_missing_results_or_cases(self):
        self.manifest['criteria'][0]['tests'] = ['MissingTest#absent']
        with self.assertRaisesRegex(ValueError, 'no JUnit or reported result for MissingTest#absent'):
            self.run_sync(FakeAio(self.existing_tree()), 'cycle', '--sprint', '21')
        self.manifest['criteria'][0]['tests'] = ['AdmissionFilterTest#denies']
        with self.assertRaisesRegex(ValueError, 'Run `cases --apply` first'):
            self.run_sync(FakeAio(self.existing_tree()), 'cycle', '--sprint', '21')

    def test_a_parameterized_method_resolves_to_its_class_and_any_class_failure_fails_it(self):
        fake = FakeAio(self.existing_tree(), [{'key': 'GEO-TC-5', 'folder': 11, 'title': '[3-AC1] x'}])
        self.manifest['criteria'][0]['tests'] = ['RateLimitHttpTest#sixtyCallsAreAdmitted']
        plan = self.run_sync(fake, 'cycle', '--sprint', '21')
        self.assertEqual(next(p for p in plan if p['action'] == 'record_run')['status'], 'Passed')
        self.manifest['criteria'][0]['tests'] = ['ApplicationTest#parameterizedVariant']
        plan = self.run_sync(fake, 'cycle', '--sprint', '21')
        self.assertEqual(next(p for p in plan if p['action'] == 'record_run')['status'], 'Failed')

    def test_cycle_uses_reported_results_for_tests_without_junit(self):
        self.manifest['criteria'][0]['tests'] = ['verify-views.sql#check-3']
        self.manifest['reported_results'] = {'verify-views.sql#check-3': 'passed'}
        report = self.dir / 'report.json'
        report.write_text('{}')
        self.manifest['reported_evidence'] = [str(report)]
        fake = FakeAio(self.existing_tree(), [{'key': 'GEO-TC-5', 'folder': 11, 'title': '[3-AC1] x'}])
        plan = self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        run = next(w for w in fake.writes if 'testrun' in w[1])
        self.assertEqual(run[2]['testRunStatus'], 'Passed')
        self.assertIn('report.json', next(p for p in plan if p['action'] == 'attach')['contains'])

    def test_a_reported_failure_overrides_a_passing_junit_result(self):
        self.manifest['reported_results'] = {self.manifest['criteria'][0]['tests'][0]: 'failed'}
        fake = FakeAio(self.existing_tree(), [{'key': 'GEO-TC-5', 'folder': 11, 'title': '[3-AC1] x'}])
        plan = self.run_sync(fake, 'cycle', '--sprint', '21')
        self.assertEqual(next(p for p in plan if p['action'] == 'record_run')['status'], 'Failed')
        self.manifest['reported_results'] = {'x#y': 'flaky'}
        with self.assertRaisesRegex(ValueError, 'must be passed or failed'):
            self.run_sync(fake, 'cycle', '--sprint', '21')

    def test_a_rerun_resumes_from_the_ledger_without_duplicating_runs_or_attachments(self):
        fake = FakeAio(self.existing_tree(), [{'key': 'GEO-TC-5', 'folder': 11, 'title': '[3-AC1] x'}],
                       [{'key': 'GEO-CY-9', 'title': f'AI {datetime.now(timezone.utc).year} Sprint 21 GEO-3'}])
        ledger = self.dir / 'aio' / 'ledger' / 'GEO-3-sprint-21.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({'runs': {'3-AC1': {'case': 'GEO-TC-5', 'status': 'Passed'}},
                                      'attachments': []}))
        plan = self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.assertEqual([p['action'] for p in plan[1:]], ['run_already_recorded', 'attach'])
        self.assertEqual(fake.writes, [('UPLOAD', '/testcycle/GEO-CY-9/attachment', 'GEO-3-abc-evidence.zip')])
        self.assertEqual(json.loads(ledger.read_text())['attachments'], ['GEO-3-abc-evidence.zip'])
        plan = self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.assertEqual([p['action'] for p in plan[1:]], ['run_already_recorded', 'already_attached'])

    def test_the_client_waits_and_retries_when_aio_rate_limits(self):
        import io, urllib.error
        from unittest import mock
        import aio_sync

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        limited = urllib.error.HTTPError('u', 429, 'Too Many Requests', {'Retry-After': '3'}, io.BytesIO(b''))
        with mock.patch('urllib.request.urlopen', side_effect=[limited, Response(b'{"ok": 1}')]), \
                mock.patch('time.sleep') as sleep:
            self.assertEqual(aio_sync.AioClient('t').call('GET', '/x'), {'ok': 1})
        sleep.assert_called_once_with(3)
        refused = urllib.error.HTTPError('u', 400, 'Bad', {}, io.BytesIO(b'bad'))
        with mock.patch('urllib.request.urlopen', side_effect=[refused]), mock.patch('time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '400'):
                aio_sync.AioClient('t').call('GET', '/x')
        sleep.assert_not_called()

    def test_manifest_requires_tests_for_every_criterion(self):
        self.manifest['criteria'][0]['tests'] = []
        with self.assertRaisesRegex(ValueError, 'needs id, title and tests'):
            self.run_sync(FakeAio(), 'cases')


TOKEN_MARKER = 'FAKE-TOKEN-MARKER-0000'
BODY_MARKER = 'AIO-RESPONSE-BODY-MARKER'
CASE_ROW = {'key': 'GEO-TC-5', 'folder': 11, 'title': '[3-AC1] x'}


class RecordingAio(FakeAio):
    """FakeAio that opens each uploaded archive, numbers created cycles and can fail its first uploads."""

    def __init__(self, *args, failing_uploads=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.archives = []
        self.failing_uploads = failing_uploads
        self.created_cycles = 0

    def call(self, method, path, body=None):
        result = super().call(method, path, body)
        if path == '/testcycle/detail':
            self.created_cycles += 1
            return {'key': f'GEO-CY-{self.created_cycles}'}
        return result

    def upload(self, path, file_path, fields=None):
        if self.failing_uploads:
            self.failing_uploads -= 1
            raise RuntimeError('upload interrupted')
        with zipfile.ZipFile(file_path) as bundle:
            self.archives.append({name: bundle.read(name) for name in bundle.namelist()})
        super().upload(path, file_path, fields)


class FakeTransport:
    """Stands in for urllib.request.urlopen and records every request the real client sends."""

    def __init__(self, folders=None, cases=None, replies=None):
        self.folders, self.cases = folders or [], cases or []
        self.replies = replies or {}
        self.requests = []

    def __call__(self, request, context=None, timeout=None):
        path = request.full_url.split('/api/v1/project/GEO', 1)[1]
        self.requests.append((request.get_method(), path, request.data))
        reply = {}
        if path == '/testcase/folder':
            reply = self.folders
        elif path == '/testcase/search':
            reply = {'items': self.cases}
        elif path == '/testcycle/search':
            reply = {'items': []}
        elif path == '/testcycle/detail':
            reply = {'key': 'GEO-CY-9'}
        elif path == '/testcase':
            reply = {'key': 'GEO-TC-1'}
        elif path == '/testcase/folder/hierarchy':
            reply = {'ID': 77}
        if path in self.replies:
            reply = self.replies[path]
        return FakeResponse(json.dumps(reply).encode())

    def writes(self):
        return [(method, path) for method, path, _ in self.requests
                if method != 'GET' and not path.endswith('/search')]


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def frozen_datetime(year):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(year, 12, 31, 12, tzinfo=tz)
    return Frozen


class AioSyncSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(mock.patch.stopall)
        self.dir = Path(self.temp.name) / 'mailbox'
        self.outside = Path(self.temp.name) / 'outside'
        self.dir.mkdir()
        self.outside.mkdir()
        (self.dir / 'junit.xml').write_text(JUNIT)
        (self.dir / 'coverage.txt').write_text('AdmissionFilter 69/69')
        self.manifest = {'issue': 'GEO-3', 'epic_folder': EPIC, 'story_folder': STORY, 'revision': 'abc',
                         'criteria': [{'id': '3-AC1', 'title': 'Denied gets 403', 'type': 'Security',
                                       'tests': ['AdmissionFilterTest#denies']}],
                         'junit': [str(self.dir / 'junit.xml')],
                         'coverage': {'threshold': 85, 'summary_file': str(self.dir / 'coverage.txt')}}
        self.tree = [{'ID': 10, 'name': EPIC, 'children': [{'ID': 11, 'name': STORY, 'children': []}]}]

    def fake(self, **kwargs):
        return RecordingAio(self.tree, [dict(CASE_ROW)], **kwargs)

    def write_manifest(self):
        path = self.dir / 'manifest.json'
        path.write_text(json.dumps(self.manifest))
        return path

    def run_sync(self, fake, *args):
        manifest = self.write_manifest()
        return main(['--root', str(self.dir), args[0], '--manifest', str(manifest), *args[1:]], client=fake)

    def set_year(self, year):
        mock.patch.object(aio_sync, 'datetime', frozen_datetime(year)).start()

    def attempt(self, fake, *args):
        """Run the command and return the exception or SystemExit it ended with, else None."""
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                self.run_sync(fake, *args)
            except (Exception, SystemExit) as error:
                return error
        return None

    @staticmethod
    def message_of(error):
        if isinstance(error, SystemExit):
            return error.code if isinstance(error.code, str) else ''
        return str(error)

    def register_repository(self):
        repository = self.outside / 'repo'
        (repository / '.git' / 'worktrees' / 'wt1').mkdir(parents=True)
        (self.dir / 'projects.json').write_text(json.dumps(
            {'projects': [{'id': 'p', 'repository_path': str(repository), 'working_directory': str(repository)}]}))
        return repository

    def linked_worktree(self, name, gitdir):
        worktree = self.outside / name
        worktree.mkdir()
        (worktree / '.git').write_text(f'gitdir: {gitdir}\n')
        return worktree

    def refused_paths(self):
        repository = self.register_repository()
        (self.dir / '.env.local').write_text(f'AIO_API_TOKEN={TOKEN_MARKER}\n')
        (self.dir / '.env.production').write_text(TOKEN_MARKER)
        (repository / '.env').write_text(TOKEN_MARKER)
        (self.dir / 'link-to-allowed.xml').symlink_to(self.dir / 'junit.xml')
        (self.outside / 'junit.xml').write_text(JUNIT)
        (self.dir / 'link-to-outside.xml').symlink_to(self.outside / 'junit.xml')
        (self.dir / 'evidence-dir').mkdir()
        elsewhere = self.outside / 'elsewhere' / '.git' / 'worktrees' / 'x'
        stray = self.linked_worktree('stray', elsewhere)
        (stray / 'junit.xml').write_text(JUNIT)
        return {'token file': self.dir / '.env.local', 'env prefixed file': self.dir / '.env.production',
                'env file in a project repository': repository / '.env',
                'symlink to an allowed file': self.dir / 'link-to-allowed.xml',
                'symlink to an outside file': self.dir / 'link-to-outside.xml',
                'directory': self.dir / 'evidence-dir',
                'file outside every allowed root': self.outside / 'junit.xml',
                'worktree of another repository': stray / 'junit.xml'}

    def reset_evidence(self):
        self.manifest['junit'] = [str(self.dir / 'junit.xml')]
        self.manifest['coverage'] = {'threshold': 85, 'summary_file': str(self.dir / 'coverage.txt')}
        self.manifest.pop('reported_evidence', None)

    def put_evidence(self, slot, path):
        if slot == 'junit':
            self.manifest['junit'] = [str(path)]
        elif slot == 'reported_evidence':
            self.manifest['reported_evidence'] = [str(path)]
        else:
            self.manifest['coverage']['summary_file'] = str(path)

    def test_refused_evidence_paths_stop_everything_in_apply_and_dry_run(self):
        for label, path in self.refused_paths().items():
            for slot in ('junit', 'reported_evidence', 'coverage'):
                for flags in ((), ('--apply',)):
                    with self.subTest(path=label, slot=slot, flags=flags):
                        self.reset_evidence()
                        self.put_evidence(slot, path)
                        fake = self.fake()
                        error = self.attempt(fake, 'cycle', '--sprint', '21', *flags)
                        self.assertIsNotNone(error)
                        message = self.message_of(error)
                        self.assertIn(path.name, message)
                        for leaked in (str(self.dir), str(self.outside), TOKEN_MARKER):
                            self.assertNotIn(leaked, message)
                        self.assertEqual(fake.writes, [])
                        self.assertEqual(fake.archives, [])
                        self.assertFalse((self.dir / 'aio').exists())

    def register_linked_worktree(self, repository, name='wt'):
        """A worktree layout as git writes it: the admin directory and the worktree point at each other."""
        worktree = self.outside / name
        admin = repository / '.git' / 'worktrees' / name
        (worktree / 'build').mkdir(parents=True)
        admin.mkdir(parents=True)
        (admin / 'gitdir').write_text(f'{worktree}/.git\n')
        (worktree / '.git').write_text(f'gitdir: {admin}\n')
        return worktree

    def forged_worktree(self, repository, admin_gitdir=None):
        """A directory outside every allowed location whose .git file merely names `<repo>/.git/worktrees/x`."""
        admin = repository / '.git' / 'worktrees' / 'x'
        if admin_gitdir is not None:
            admin.mkdir()
            (admin / 'gitdir').write_text(f'{admin_gitdir}\n')
        forged = self.outside / 'forged'
        (forged / 'build').mkdir(parents=True)
        (forged / '.git').write_text(f'gitdir: {admin}\n')
        (forged / 'build' / 'junit.xml').write_text(JUNIT)
        return forged / 'build' / 'junit.xml'

    def assert_evidence_refused(self, path):
        for flags in ((), ('--apply',)):
            with self.subTest(path=path, flags=flags):
                self.reset_evidence()
                self.manifest['junit'] = [str(path)]
                fake = self.fake()
                error = self.attempt(fake, 'cycle', '--sprint', '21', *flags)
                self.assertEqual(self.message_of(error), f'Evidence file {path.name} is not an allowed regular file')
                self.assertEqual(fake.writes, [])
                self.assertEqual(fake.archives, [])

    def test_a_forged_git_file_naming_a_missing_worktree_directory_opens_nothing(self):
        repository = self.register_repository()
        self.assert_evidence_refused(self.forged_worktree(repository))

    def test_a_git_file_whose_worktree_directory_points_elsewhere_opens_nothing(self):
        repository = self.register_repository()
        self.assert_evidence_refused(self.forged_worktree(repository, admin_gitdir=self.outside / 'other' / '.git'))

    def test_a_real_linked_worktree_still_accepts_files_directly_and_in_subdirectories(self):
        repository = self.register_repository()
        worktree = self.register_linked_worktree(repository)
        (worktree / 'report.json').write_text('{}')
        (worktree / 'build' / 'junit.xml').write_text(JUNIT)
        self.manifest['junit'] = [str(worktree / 'build' / 'junit.xml')]
        self.manifest['reported_evidence'] = [str(worktree / 'report.json')]
        fake = self.fake()
        self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.assertEqual(list(fake.archives[0]), ['junit.xml', 'report.json', 'coverage.txt'])

    def test_env_names_are_refused_in_any_letter_case(self):
        (self.dir / '.ENV.local').write_text(TOKEN_MARKER)
        (self.dir / '.Env').write_text(TOKEN_MARKER)
        (self.dir / 'plain.xml').symlink_to(self.dir / '.ENV.local')
        for name in ('.ENV.local', '.Env', 'plain.xml'):
            self.assert_evidence_refused(self.dir / name)

    def test_evidence_in_the_root_a_project_repository_and_its_linked_worktree_is_uploaded(self):
        repository = self.register_repository()
        worktree = self.register_linked_worktree(repository)
        (repository / 'build').mkdir()
        (repository / 'build' / 'junit.xml').write_text(JUNIT)
        (worktree / 'report.json').write_text('{}')
        self.manifest['junit'] = [str(repository / 'build' / 'junit.xml')]
        self.manifest['reported_evidence'] = [str(worktree / 'report.json')]
        fake = self.fake()
        self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.assertEqual(list(fake.archives[0]), ['junit.xml', 'report.json', 'coverage.txt'])

    def test_the_archive_holds_only_the_validated_files_and_never_the_token(self):
        (self.dir / '.env.local').write_text(f'AIO_API_TOKEN={TOKEN_MARKER}\n')
        fake = self.fake()
        self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.assertEqual(list(fake.archives[0]), ['junit.xml', 'coverage.txt'])
        self.assertNotIn(TOKEN_MARKER.encode(), b''.join(fake.archives[0].values()))

    def test_invalid_issue_or_sprint_is_refused_before_any_ledger_path_exists(self):
        refusals = [('../../x', '21'), ('GEO-1/../..', '21'), ('geo-1', '21'), ('GEO-', '21'),
                    ('1-GEO', '21'), ('GEO-3', '../x'), ('GEO-3', 'x'), ('GEO-3', '2.5'),
                    ('GEO-3', '21/../..'), ('GEO-3', '١٢')]
        for issue, sprint in refusals:
            with self.subTest(issue=issue, sprint=sprint):
                self.manifest['issue'] = issue
                fake = self.fake()
                self.assertIsNotNone(self.attempt(fake, 'cycle', '--sprint', sprint, '--apply'))
                self.assertEqual(fake.writes, [])
                self.assertEqual(sorted(path.name for path in self.dir.rglob('*')),
                                 ['coverage.txt', 'junit.xml', 'manifest.json'])
                self.assertEqual(sorted(path.name for path in self.outside.rglob('*')), [])

    def test_well_formed_issue_keys_still_get_their_ledger(self):
        self.manifest['issue'] = 'AB_C1-12'
        self.run_sync(self.fake(), 'cycle', '--sprint', '21', '--apply')
        self.assertTrue((self.dir / 'aio' / 'ledger' / 'AB_C1-12-sprint-21.json').is_file())

    @staticmethod
    def recorded_runs(fake):
        return [path for _, path, _ in fake.writes if 'testrun' in path]

    def fake_with_cycle(self):
        self.set_year(2026)
        fake = self.fake()
        fake.cycles = [{'key': 'GEO-CY-9', 'title': 'AI 2026 Sprint 21 GEO-3'}]
        return fake

    def test_a_run_for_one_revision_does_not_suppress_the_run_of_another_revision(self):
        fake = self.fake_with_cycle()
        self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.manifest['revision'] = 'def4567'
        plan = self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.assertIn('record_run', [p['action'] for p in plan])
        self.assertEqual(len(self.recorded_runs(fake)), 2)

    def test_the_same_revision_is_not_recorded_twice(self):
        fake = self.fake_with_cycle()
        self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        plan = self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.assertIn('run_already_recorded', [p['action'] for p in plan])
        self.assertEqual(len(self.recorded_runs(fake)), 1)

    def test_a_ledger_written_without_a_revision_still_counts_as_recorded(self):
        fake = self.fake_with_cycle()
        ledger = self.dir / 'aio' / 'ledger' / 'GEO-3-sprint-21.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({'runs': {'3-AC1': {'case': 'GEO-TC-5', 'status': 'Passed'}},
                                      'attachments': ['GEO-3-abc-evidence.zip']}))
        plan = self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.assertEqual([p['action'] for p in plan[1:]], ['run_already_recorded', 'already_attached'])
        self.assertEqual(fake.writes, [])

    def test_a_rerun_in_a_new_calendar_year_reuses_the_recorded_cycle(self):
        self.set_year(2026)
        fake = self.fake(failing_uploads=1)
        self.assertIsNotNone(self.attempt(fake, 'cycle', '--sprint', '21', '--apply'))
        self.set_year(2027)
        plan = self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.assertEqual(plan[0]['action'], 'use_cycle')
        self.assertEqual(plan[0]['title'], 'AI 2026 Sprint 21 GEO-3')
        self.assertEqual(fake.created_cycles, 1)
        self.assertEqual([path for _, path, _ in fake.writes if path.endswith('/attachment')],
                         ['/testcycle/GEO-CY-1/attachment'])

    def test_a_rerun_after_a_failed_upload_records_each_run_and_attachment_once(self):
        self.set_year(2026)
        fake = self.fake(failing_uploads=1)
        self.assertIsNotNone(self.attempt(fake, 'cycle', '--sprint', '21', '--apply'))
        self.run_sync(fake, 'cycle', '--sprint', '21', '--apply')
        self.assertEqual(len(self.recorded_runs(fake)), 1)
        self.assertEqual(len(fake.archives), 1)

    def run_cli(self, *args, urlopen):
        """Run aio_sync.py as a script on a fake transport; return (exit code, everything it printed)."""
        output = io.StringIO()
        code = 0
        with mock.patch('urllib.request.urlopen', side_effect=urlopen), mock.patch('time.sleep'), \
                mock.patch.object(sys, 'argv', ['aio_sync.py', '--root', str(self.dir), *args]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                warnings.catch_warnings():
            warnings.simplefilter('ignore', ResourceWarning)
            try:
                runpy.run_path(aio_sync.__file__, run_name='__main__')
            except SystemExit as stop:
                code = stop.code
                if isinstance(code, str):
                    output.write(code)
        return code, output.getvalue()

    def write_token(self, text=f'AIO_API_TOKEN={TOKEN_MARKER}\n'):
        (self.dir / '.env.local').write_text(text)

    def cli(self, command='cases', urlopen=None, manifest_text=None, extra=()):
        manifest = self.write_manifest()
        if manifest_text is not None:
            manifest.write_text(manifest_text)
        return self.run_cli(command, '--manifest', str(manifest), *extra,
                            urlopen=urlopen or FakeTransport(self.tree, [dict(CASE_ROW)]))

    def assert_one_code_written_line(self, code, text, forbidden=()):
        self.assertNotIn(code, (0, None))
        self.assertEqual(len([line for line in text.splitlines() if line.strip()]), 1, text)
        for fragment in ('Traceback', 'Errno', 'Error:', 'JSONDecodeError', 'ParseError', 'urlopen', 'http',
                         str(self.dir), TOKEN_MARKER, BODY_MARKER, *forbidden):
            self.assertNotIn(fragment, text)

    def test_a_missing_manifest_ends_with_one_clean_line(self):
        self.write_token()
        code, text = self.run_cli('cases', '--manifest', str(self.dir / 'absent.json'), urlopen=FakeTransport())
        self.assert_one_code_written_line(code, text)

    def test_a_malformed_manifest_ends_with_one_clean_line(self):
        self.write_token()
        for content in ('not json{', '[]', '"text"', ''):
            with self.subTest(content=content):
                code, text = self.cli(manifest_text=content)
                self.assert_one_code_written_line(code, text, ('Expecting', 'column', 'attribute'))

    def test_a_missing_token_file_ends_with_one_clean_line(self):
        code, text = self.cli()
        self.assert_one_code_written_line(code, text)

    def test_a_token_file_without_the_token_line_ends_with_one_clean_line(self):
        self.write_token(f'OTHER={TOKEN_MARKER}\n')
        code, text = self.cli()
        self.assert_one_code_written_line(code, text)

    def test_malformed_junit_ends_with_one_clean_line(self):
        self.write_token()
        (self.dir / 'junit.xml').write_text('<testsuite><testcase')
        code, text = self.cli('cycle', extra=('--sprint', '21'))
        self.assert_one_code_written_line(code, text, ('syntax error', 'column'))

    def test_an_http_error_hides_the_response_body(self):
        self.write_token()

        def refuse(*args, **kwargs):
            raise urllib.error.HTTPError('u', 400, 'Bad', {}, io.BytesIO(BODY_MARKER.encode()))
        code, text = self.cli(urlopen=refuse)
        self.assert_one_code_written_line(code, text)

    def test_a_network_failure_hides_the_library_text(self):
        self.write_token()

        def unreachable(*args, **kwargs):
            raise urllib.error.URLError(f'{BODY_MARKER} connection refused')
        code, text = self.cli(urlopen=unreachable)
        self.assert_one_code_written_line(code, text, ('refused', 'URLError'))

    def test_exhausted_rate_limit_retries_hide_the_response_body(self):
        self.write_token()

        def limited(*args, **kwargs):
            raise urllib.error.HTTPError('u', 429, 'Too Many', {}, io.BytesIO(BODY_MARKER.encode()))
        code, text = self.cli(urlopen=limited)
        self.assert_one_code_written_line(code, text)

    def test_the_dry_run_sends_no_write_and_prints_no_token(self):
        self.write_token()
        transport = FakeTransport(self.tree, [dict(CASE_ROW)])
        for command, extra in (('cases', ()), ('cycle', ('--sprint', '21'))):
            with self.subTest(command=command):
                code, text = self.cli(command, urlopen=transport, extra=extra)
                self.assertEqual(code, 0)
                self.assertEqual(transport.writes(), [])
                self.assertNotIn(TOKEN_MARKER, text)

    def test_the_uploaded_archive_never_carries_the_token(self):
        self.write_token()
        transport = FakeTransport(self.tree, [dict(CASE_ROW)])
        code, _ = self.cli('cycle', urlopen=transport, extra=('--sprint', '21', '--apply'))
        self.assertEqual(code, 0)
        uploads = [data for _, path, data in transport.requests if path.endswith('/attachment')]
        self.assertEqual(len(uploads), 1)
        self.assertNotIn(TOKEN_MARKER.encode(), uploads[0])

    def assert_ends_with_line(self, code, text, expected):
        self.assertNotIn(code, (0, None))
        self.assertEqual(text.strip(), expected)

    def test_an_unexpected_cycle_reply_ends_with_one_fixed_line(self):
        self.write_token()
        unexpected = [('/testcycle/search', {'items': [{'title': 'no key'}]}),
                      ('/testcycle/search', [{'key': 'GEO-CY-1'}]),
                      ('/testcycle/search', 'text'),
                      ('/testcycle/detail', None),
                      ('/testcycle/detail', [{'key': 'GEO-CY-1'}]),
                      ('/testcycle/detail', {})]
        for path, reply in unexpected:
            with self.subTest(path=path, reply=reply):
                transport = FakeTransport(self.tree, [dict(CASE_ROW)], replies={path: reply})
                code, text = self.cli('cycle', urlopen=transport, extra=('--sprint', '21', '--apply'))
                self.assert_ends_with_line(code, text, f'aio_sync: AIO POST {path} returned an unexpected response')

    def assert_unexpected_cases_reply(self, method, path, reply, flags, folders=None, cases=None):
        self.write_token()
        transport = FakeTransport(self.tree if folders is None else folders,
                                  [dict(CASE_ROW)] if cases is None else cases, replies={path: reply})
        code, text = self.cli('cases', urlopen=transport, extra=flags)
        self.assert_ends_with_line(code, text, f'aio_sync: AIO {method} {path} returned an unexpected response')

    def test_an_unexpected_folder_tree_reply_ends_with_one_fixed_line(self):
        shapes = [None, 'text', ['text'], [1], [{'name': EPIC, 'children': [{'name': STORY}]}],
                  [{'ID': 1, 'name': EPIC, 'children': {}}], [{'ID': 1, 'name': EPIC, 'children': 'text'}]]
        for reply in shapes:
            for flags in ((), ('--apply',)):
                with self.subTest(reply=reply, flags=flags):
                    self.assert_unexpected_cases_reply('GET', '/testcase/folder', reply, flags)

    def test_a_folder_without_children_or_with_null_children_has_no_children(self):
        self.write_token()
        for node in ({'ID': 10, 'name': EPIC}, {'ID': 10, 'name': EPIC, 'children': None}):
            with self.subTest(node=node):
                code, text = self.cli('cases', urlopen=FakeTransport([node], []))
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(text)['plan'][0]['action'], 'create_folder')

    def test_an_unexpected_folder_hierarchy_reply_ends_with_one_fixed_line(self):
        for reply in (None, 'text', [], {}):
            with self.subTest(reply=reply):
                self.assert_unexpected_cases_reply('PUT', '/testcase/folder/hierarchy', reply, ('--apply',),
                                                   folders=[])

    def test_an_unexpected_case_search_reply_ends_with_one_fixed_line(self):
        shapes = ['text', ['text'], {'items': 'text'}, {'items': None}, {'items': {'key': 'GEO-TC-5'}},
                  {'items': ['text']}, {'items': [None]}, {'items': [{'key': 'GEO-TC-5'}]},
                  {'items': [{'title': '[3-AC1] x'}]}]
        for reply in shapes:
            for flags in ((), ('--apply',)):
                with self.subTest(reply=reply, flags=flags):
                    self.assert_unexpected_cases_reply('POST', '/testcase/search', reply, flags)

    def test_an_unexpected_case_creation_reply_ends_with_one_fixed_line(self):
        for reply in (None, 'text', [], [{'key': 'GEO-TC-1'}], {}):
            with self.subTest(reply=reply):
                self.assert_unexpected_cases_reply('POST', '/testcase', reply, ('--apply',), cases=[])

    def test_well_formed_cases_replies_still_create_and_update(self):
        self.write_token()
        creating = FakeTransport([], [])
        code, _ = self.cli('cases', urlopen=creating, extra=('--apply',))
        self.assertEqual(code, 0)
        self.assertEqual(creating.writes(), [('PUT', '/testcase/folder/hierarchy'), ('POST', '/testcase')])
        updating = FakeTransport(self.tree, [dict(CASE_ROW)])
        code, _ = self.cli('cases', urlopen=updating, extra=('--apply',))
        self.assertEqual(code, 0)
        self.assertEqual(updating.writes(), [('PATCH', '/testcase/GEO-TC-5/detail')])

    def test_a_ledger_run_that_is_not_an_object_ends_with_one_fixed_line(self):
        self.write_token()
        ledger = self.dir / 'aio' / 'ledger' / 'GEO-3-sprint-21.json'
        ledger.parent.mkdir(parents=True)
        for run in ('passed', 5, [], None):
            with self.subTest(run=run):
                ledger.write_text(json.dumps({'runs': {'3-AC1': run}, 'attachments': []}))
                code, text = self.cli('cycle', extra=('--sprint', '21'))
                self.assert_ends_with_line(code, text, 'aio_sync: The cycle ledger holds a malformed run')

    def test_a_ledger_without_a_list_of_attachment_names_ends_with_one_fixed_line(self):
        self.write_token()
        ledger = self.dir / 'aio' / 'ledger' / 'GEO-3-sprint-21.json'
        ledger.parent.mkdir(parents=True)
        malformed = [{'runs': {}}, {'runs': {}, 'attachments': {}}, {'runs': {}, 'attachments': 'a.zip'},
                     {'runs': {}, 'attachments': [5]}, {'runs': {}, 'attachments': ['a.zip', None]}]
        for state in malformed:
            with self.subTest(state=state):
                ledger.write_text(json.dumps(state))
                transport = FakeTransport(self.tree, [dict(CASE_ROW)])
                code, text = self.cli('cycle', urlopen=transport, extra=('--sprint', '21', '--apply'))
                self.assert_ends_with_line(code, text, 'aio_sync: The cycle ledger holds a malformed attachment list')
                self.assertEqual(transport.requests, [])

    def assert_file_failure_line(self, code, text):
        self.assert_ends_with_line(code, text, 'aio_sync: a file could not be read or written')
        for fragment in (str(self.dir), 'Errno', 'Permission', 'Traceback'):
            self.assertNotIn(fragment, text)

    def test_a_ledger_that_cannot_be_saved_ends_with_one_fixed_line(self):
        self.write_token()
        manifest = self.write_manifest()
        failure = OSError(13, 'Permission denied', str(self.dir / 'aio' / 'ledger'))
        with mock.patch.object(Path, 'write_text', side_effect=failure):
            code, text = self.run_cli('cycle', '--manifest', str(manifest), '--sprint', '21', '--apply',
                                      urlopen=FakeTransport(self.tree, [dict(CASE_ROW)]))
        self.assert_file_failure_line(code, text)

    def test_an_evidence_bundle_that_cannot_be_built_ends_with_one_fixed_line(self):
        self.write_token()
        failure = OSError(28, 'No space left on device', str(self.dir / 'bundle.zip'))
        with mock.patch('zipfile.ZipFile', side_effect=failure):
            code, text = self.cli('cycle', extra=('--sprint', '21', '--apply'))
        self.assert_file_failure_line(code, text)


if __name__ == '__main__':
    unittest.main()
