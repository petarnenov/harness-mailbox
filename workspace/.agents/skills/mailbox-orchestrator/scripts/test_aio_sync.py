import json
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

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


if __name__ == '__main__':
    unittest.main()
