"""Exercise the native coordinator receive cycle with an isolated file mailbox."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


RECEIVE = (Path(__file__).resolve().parents[1] / 'workspace/.agents/skills'
           / 'mailbox-orchestrator/scripts/receive.py')
COORDINATOR = '10000000-0000-4000-8000-000000000001'
WORKER = '10000000-0000-4000-8000-000000000002'
TASK = '20000000-0000-4000-8000-000000000001'
G2 = '30000000-0000-4000-8000-000000000001'
PR = '30000000-0000-4000-8000-000000000002'
REPLY = '40000000-0000-4000-8000-000000000001'


FAILURE_LINE = 'Cannot inspect mailbox inbox/orchestrator. Check its path and permissions.\n'
RAW_FAILURE = "[Errno 13] Permission denied: '/data/inbox/orchestrator/secret.json'"
LEAKS = ('Errno', 'Permission denied', '/data/', 'Traceback')


BROKEN_CONTEXT_PACK = "'''A context_pack whose import fails with a raw operating-system text.'''\nraise RuntimeError(\"[Errno 13] Permission denied: '/data/secret/context_pack.py'\")\n"


def scripts_copy(parent, context_pack_source=None):
    """A copy of the scripts, deep enough below the filesystem root, without context_pack or with a replacement."""
    scripts = Path(parent) / 'a/b/c/d/scripts'
    shutil.copytree(RECEIVE.parent, scripts, ignore=shutil.ignore_patterns('test_*', '__pycache__', 'context_pack.py'))
    if context_pack_source is not None:
        (scripts / 'context_pack.py').write_text(context_pack_source)
    return scripts


def install_layout(mailbox):
    """The installed layout: scripts below .agents/skills and the application's top-level modules in dashboard/."""
    scripts = mailbox / '.agents/skills/mailbox-orchestrator/scripts'
    shutil.copytree(RECEIVE.parent, scripts, ignore=shutil.ignore_patterns('test_*', '__pycache__'))
    (mailbox / 'dashboard').mkdir()
    for module in RECEIVE.parents[5].glob('*.py'):
        shutil.copy(module, mailbox / 'dashboard' / module.name)
    return scripts


def file_contents(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


def child_environment():
    """The caller's environment with bytecode writing allowed."""
    return {key: value for key, value in os.environ.items() if key not in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')}


def script_module(name):
    with patch.object(sys, 'path', [str(RECEIVE.parent), *sys.path]):
        return importlib.import_module(name)


def tree_snapshot(root):
    entries = {}
    for path in sorted(root.rglob('*')):
        key = str(path.relative_to(root))
        entries[key] = ('dir',) if path.is_dir() else (path.read_bytes(), path.stat().st_mtime_ns)
    return entries


class ReceiveFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.inbox = self.root / 'inbox/orchestrator'
        self.archive = self.root / 'archive/orchestrator'
        self.inbox.mkdir(parents=True)
        self.archive.mkdir(parents=True)
        (self.root / 'projects.json').write_text(json.dumps({'projects': [
            {'id': 'alpha', 'working_directory': str(self.root / 'alpha'),
             'repository_path': str(self.root / 'alpha')}]}))
        self.command = [str(RECEIVE), '--root', str(self.root), '--endpoint', COORDINATOR]

    def receive(self, *options, expected_exit=0):
        output = io.StringIO()
        with (patch.object(sys, 'argv', [*self.command, *options]),
              patch.object(sys, 'path', [str(RECEIVE.parent), *sys.path]),
              contextlib.redirect_stdout(output)):
            try:
                runpy.run_path(str(RECEIVE), run_name='__main__')
            except SystemExit as exit_status:
                self.assertEqual(exit_status.code or 0, expected_exit)
                if expected_exit:
                    return
        self.assertEqual(expected_exit, 0)
        return json.loads(output.getvalue())

    def receive_captured(self, *options):
        out, err = io.StringIO(), io.StringIO()
        with (patch.object(sys, 'argv', [*self.command, *options]),
              patch.object(sys, 'path', [str(RECEIVE.parent), *sys.path]),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            try:
                runpy.run_path(str(RECEIVE), run_name='__main__')
            except SystemExit as exit_status:
                return exit_status.code or 0, out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def publish(self, uid, kind, **fields):
        message = dict(id=uid, type=kind, task_id=TASK, from_project='alpha',
                       created_at='2026-01-01T00:00:00Z', to_project='orchestrator',
                       to_endpoint=COORDINATOR)
        path = self.inbox / (uid + '.json')
        staged = path.with_suffix('.tmp')
        staged.write_text(json.dumps({**message, **fields}))
        staged.rename(path)
        return path


class OrchestratorReceiveTests(ReceiveFixture):
    def test_registration_stays_idle_and_only_local_wake_allows_yield(self):
        with patch('time.sleep', side_effect=AssertionError('idle coordinator waited')):
            self.assertEqual(self.receive('--wait', '60')['state'], 'idle')
        self.assertEqual(self.receive('--active-goal')['state'], 'wait')
        self.assertEqual(self.receive('--active-goal', '--wake-confirmed')['state'], 'yield')
        self.publish(G2, 'g2_request')
        self.assertEqual(self.receive('--active-goal', '--wake-confirmed')['state'], 'pending')

    def test_addressed_decisions_and_information_use_separate_protocols(self):
        for kind in ('g1_request', 'g2_request', 'pr_ready', 'pr_opened', 'escalation'):
            with self.subTest(kind=kind):
                self.publish(G2, kind)
                row, = self.receive('--active-goal')['requests']
                self.assertEqual((row['id'], row['step']), (G2, 'review'))
        self.publish(G2, 'pr_ready', to_endpoint=WORKER)
        self.publish(PR, 'status')
        info, = self.receive('--active-goal')['requests']
        self.assertEqual((info['id'], info['step']), (PR, 'inspect'))
        self.assertFalse((self.root / 'outbox').exists())
        self.assertEqual(len(list(self.inbox.glob('*.json'))), 2)

    def test_nested_or_legacy_routes_require_consistent_recipient_identity(self):
        self.publish(G2, 'g2_request', to={'role': 'orchestrator', 'session_id': WORKER})
        self.publish(PR, 'pr_ready', to_project='alpha')
        self.assertEqual(self.receive('--active-goal')['requests'], [])
        self.publish(G2, 'g2_request', to_endpoint=None, to={'role': 'orchestrator'})
        row, = self.receive('--active-goal')['requests']
        self.assertEqual(row['step'], 'inspect')

    def test_role_only_reply_to_foreign_request_in_worker_archive_is_ignored(self):
        self.publish(G2, 'g2_request', to_endpoint=None, in_reply_to=REPLY)
        original = self.root / 'archive/alpha' / (REPLY + '.json')
        original.parent.mkdir(parents=True)
        original.write_text(json.dumps({'id': REPLY, 'from': {'session_id': WORKER}}))
        self.assertEqual(self.receive('--active-goal')['requests'], [])
        original.write_text(json.dumps({'id': REPLY, 'from_session_id': COORDINATOR}))
        row, = self.receive('--active-goal')['requests']
        self.assertEqual(row['id'], G2)

    def test_unavailable_inbox_reports_failure_instead_of_yielding(self):
        self.inbox.rmdir()
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.receive('--active-goal', '--wake-confirmed', expected_exit=1)
        self.assertIn('inbox/orchestrator', error.getvalue())

    def test_invalid_decision_envelopes_cannot_advance_the_coordinator(self):
        for invalid in (
            {'created_at': '2026-01-01'}, {'created_at': 'invalid'}, {'in_reply_to': 'invalid'},
            {'from_project': 'unregistered'}, {'type': None}, {'executable': True},
            {'type': 'execution_request'}, {'type': 'registration_offer'},
        ):
            with self.subTest(invalid=invalid):
                fields = dict(invalid)
                kind = fields.pop('type', 'g2_request')
                self.publish(G2, kind, **fields)
                self.assertEqual(self.receive('--active-goal')['requests'], [])
        for numeric in ('NaN', 'Infinity', '1e999'):
            path = self.publish(G2, 'g2_request', cost=0)
            path.write_text(path.read_text().replace('"cost": 0', '"cost": ' + numeric))
            self.assertEqual(self.receive('--active-goal')['requests'], [])

    def test_partial_invalid_and_linked_inbox_files_stay_unprocessed(self):
        g2 = self.publish(G2, 'g2_request')
        g2.rename(self.root / 'elsewhere')
        g2.symlink_to(self.root / 'elsewhere')
        pr = self.inbox / (PR + '.json')
        pr.write_bytes(b'{')
        (self.inbox / 'partial.tmp').touch()
        self.assertEqual(self.receive('--active-goal')['requests'], [])

    def test_g2_at_status_then_pr_ready_without_another_prompt(self):
        g2 = self.publish(G2, 'g2_request')
        # Simulated host answers a status interruption, then continues the same goal.
        status = self.receive('--active-goal')
        transcript = [f"Status: {len(status['requests'])} request awaiting review"]
        self.assertEqual(transcript, ['Status: 1 request awaiting review'])
        first, = self.receive('--active-goal')['requests']
        self.assertEqual((first['id'], first['step']), (G2, 'review'))
        decisions = self.root / 'decisions' / TASK
        decisions.mkdir(parents=True)
        decision = decisions / (G2 + '.json')
        decision.write_text(json.dumps(dict(id=G2, request_id=G2, task_id=TASK,
                                            verdict='accepted', evidence=['fixture G2 report'],
                                            orchestrator=COORDINATOR, reply_id=REPLY)))
        # A new invocation between decision and reply must recover the pending effect.
        recovered, = self.receive('--active-goal')['requests']
        self.assertEqual((recovered['id'], recovered['step']), (G2, 'reconcile'))
        reply = self.root / 'outbox/orchestrator' / (REPLY + '.json')
        reply.parent.mkdir(parents=True)
        with reply.open('x') as stream:
            json.dump(dict(id=REPLY, in_reply_to=G2, decision_id=G2, task_id=TASK,
                           to_project='alpha', to_endpoint=WORKER, verdict='accepted'), stream)
        # Second interruption after publication: observe the existing effect before archiving.
        again, = self.receive('--active-goal')['requests']
        self.assertEqual(again['step'], 'reconcile')
        self.assertEqual(json.loads(reply.read_text())['in_reply_to'], again['id'])
        worker_inbox = self.root / 'inbox/alpha'
        worker_archive = self.root / 'archive/alpha'
        worker_inbox.mkdir()
        worker_archive.mkdir()
        received = worker_inbox / reply.name
        self.assertFalse(received.exists())
        # Simulate completing the missing half of the atomic publication.
        received.hardlink_to(reply)
        self.assertEqual(received.read_bytes(), reply.read_bytes())
        g2.rename(self.archive / g2.name)
        self.publish(G2, 'g2_request')
        self.assertEqual(self.receive('--active-goal')['requests'], [])
        # A simulated worker publishes only after the first empty receive scan.
        def worker_receives_gate(_):
            incoming, = worker_inbox.glob('*.json')
            record = json.loads(incoming.read_text())
            self.assertEqual((record['in_reply_to'], record['to_endpoint'], record['verdict']),
                             (G2, WORKER, 'accepted'))
            incoming.rename(worker_archive / incoming.name)
            self.publish(PR, 'pr_ready')

        with patch('time.sleep', side_effect=worker_receives_gate) as wait:
            pending, = self.receive('--active-goal', '--wait', '5')['requests']
        self.assertTrue(wait.called)
        self.assertEqual((pending['id'], pending['step']), (PR, 'review'))
        blocker = dict(id=PR, request_id=PR, task_id=TASK, verdict='blocked',
                       rationale='PR lacks exact head and base', owner=WORKER,
                       resume_when='Worker supplies both SHAs', orchestrator=COORDINATOR)
        (decisions / (PR + '.json')).write_text(json.dumps(blocker))
        reconciliation, = self.receive('--active-goal')['requests']
        self.assertEqual(reconciliation['step'], 'reconcile')
        (self.inbox / (PR + '.json')).rename(self.archive / (PR + '.json'))
        self.publish(PR, 'pr_ready')
        self.assertEqual(self.receive('--active-goal')['requests'], [])
        self.assertEqual(list(reply.parent.iterdir()), [reply])
        self.assertEqual(list(worker_inbox.iterdir()), [])
        self.assertEqual(list(worker_archive.iterdir()), [worker_archive / reply.name])

    def test_the_fixed_failure_line_and_exit_code_stay_for_an_unreadable_inbox(self):
        self.inbox.rmdir()
        self.assertEqual((1, '', FAILURE_LINE), self.receive_captured('--active-goal'))

    def test_pending_returns_only_the_request_pointer_fields(self):
        self.publish(G2, 'g2_request')
        pending = runpy.run_path(str(RECEIVE), run_name='receive_by_path')['pending']
        self.assertEqual([{'id': G2, 'path': f'inbox/orchestrator/{G2}.json', 'step': 'review'}],
                         pending(self.root, COORDINATOR))

    def test_execution_requests_and_executable_messages_are_still_not_pending(self):
        self.publish(G2, 'execution_request')
        self.publish(PR, 'status', executable=True)
        self.assertEqual([], self.receive('--active-goal')['requests'])


class ReceiveContextTests(ReceiveFixture):
    UNAVAILABLE = {'available': False, 'packs': {}, 'omitted': 0}

    def setUp(self):
        super().setUp()
        self.context_pack = script_module('context_pack')
        self.server, self.graph = self.context_pack.load_dashboard_modules()

    def add_task(self, task_id):
        path = self.root / 'tasks/alpha' / (task_id + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'id': task_id, 'project': 'alpha', 'status': 'in_progress'}))

    def task_id(self, number):
        return f'60000000-0000-4000-8000-{number:012}'

    def message_id(self, number):
        return f'50000000-0000-4000-8000-{number:012}'

    def publish_for(self, number, task_id=None):
        return self.publish(self.message_id(number), 'status', task_id=task_id or self.task_id(number))

    def expected_pack(self, task_id):
        pack = self.context_pack.read_pack(self.root, 'task:' + task_id)
        return {key: value for key, value in pack.items() if key != 'generated_at'}

    def without_context(self, result):
        return dict(requests=[{key: value for key, value in item.items() if key != 'context_node'}
                              for item in result['requests']], state=result['state'])

    def test_every_request_with_a_pack_names_it_and_the_pack_is_what_context_pack_prints(self):
        for number in (1, 2):
            self.add_task(self.task_id(number))
            self.publish_for(number)
        result = self.receive('--active-goal')
        self.assertEqual(['task:' + self.task_id(1), 'task:' + self.task_id(2)], [item['context_node'] for item in result['requests']])
        self.assertEqual((True, 0), (result['context']['available'], result['context']['omitted']))
        for task_id in (self.task_id(1), self.task_id(2)):
            pack = result['context']['packs']['task:' + task_id]
            self.assertEqual(self.expected_pack(task_id), {key: value for key, value in pack.items() if key != 'generated_at'})

    def test_state_and_request_fields_are_kept_next_to_the_context(self):
        self.add_task(self.task_id(1))
        self.publish_for(1)
        result = self.receive('--active-goal')
        self.assertEqual({'state', 'requests', 'context'}, set(result))
        self.assertEqual(('pending', self.message_id(1), 'inspect'), (result['state'], result['requests'][0]['id'], result['requests'][0]['step']))

    def test_a_task_that_is_no_graph_node_gets_no_entry_and_no_context_node(self):
        self.publish_for(1)
        result = self.receive('--active-goal')
        self.assertEqual(({'available': True, 'packs': {}, 'omitted': 0}, False),
                         (result['context'], 'context_node' in result['requests'][0]))

    def test_a_task_id_that_is_not_a_uuid_is_not_looked_up(self):
        self.add_task('plain-task')
        self.publish_for(1, task_id='plain-task')
        self.publish(self.message_id(2), 'status', task_id=None)
        with patch.object(self.context_pack, 'load_dashboard_modules') as loader:
            result = self.receive('--active-goal')
        loader.assert_not_called()
        self.assertEqual(({'available': True, 'packs': {}, 'omitted': 0}, [False, False]),
                         (result['context'], ['context_node' in item for item in result['requests']]))

    def test_only_the_first_ten_distinct_tasks_are_looked_up_and_an_unknown_one_uses_its_slot(self):
        for number in range(1, 12):
            if number != 4:
                self.add_task(self.task_id(number))
            self.publish_for(number)
        result = self.receive('--active-goal')
        marked = [item['id'] for item in result['requests'] if 'context_node' in item]
        self.assertEqual(([self.message_id(number) for number in (1, 2, 3, 5, 6, 7, 8, 9, 10)], 9, 1),
                         (marked, len(result['context']['packs']), result['context']['omitted']))
        self.assertNotIn('task:' + self.task_id(11), result['context']['packs'])

    def test_several_requests_naming_one_task_count_once(self):
        self.add_task(self.task_id(1))
        for number in (1, 2, 3):
            self.publish_for(number, task_id=self.task_id(1))
        result = self.receive('--active-goal')
        self.assertEqual((1, 0, 3), (len(result['context']['packs']), result['context']['omitted'],
                                     sum('context_node' in item for item in result['requests'])))

    def test_the_dashboard_and_the_graph_are_built_once_for_several_tasks(self):
        for number in (1, 2):
            self.add_task(self.task_id(number))
            self.publish_for(number)
        with (patch.object(self.server, 'build_dashboard', wraps=self.server.build_dashboard) as dashboard,
              patch.object(self.graph, 'build_context_graph', wraps=self.graph.build_context_graph) as graph):
            result = self.receive('--active-goal')
        self.assertEqual((2, 1, 1), (len(result['context']['packs']), dashboard.call_count, graph.call_count))

    def test_without_a_named_task_the_dashboard_code_is_not_loaded(self):
        for options in ((), ('--wait', '60'), ('--active-goal',), ('--active-goal', '--wake-confirmed')):
            with self.subTest(options=options), patch.object(self.context_pack, 'load_dashboard_modules') as loader:
                result = self.receive(*options)
            loader.assert_not_called()
            self.assertEqual({'available': True, 'packs': {}, 'omitted': 0}, result['context'])

    def test_a_run_without_an_active_goal_stays_idle_with_an_empty_context(self):
        self.add_task(self.task_id(1))
        self.publish_for(1)
        self.assertEqual({'state': 'idle', 'requests': [], 'context': {'available': True, 'packs': {}, 'omitted': 0}},
                         self.receive())

    def test_a_failing_context_leaves_exit_code_state_and_requests_as_without_the_failure(self):
        for number in (1, 2):
            self.add_task(self.task_id(number))
            self.publish_for(number)
        healthy = self.receive_captured('--active-goal')
        healthy_result = json.loads(healthy[1])
        failures = {
            'loader': patch.object(self.context_pack, 'load_dashboard_modules',
                                   side_effect=self.context_pack.Refusal(self.context_pack.CODE_NOT_LOADED, 1)),
            'dashboard': patch.object(self.server, 'build_dashboard', side_effect=OSError(RAW_FAILURE)),
            'unprintable': patch.object(self.graph, 'build_context_packs',
                                        return_value={'task:' + self.task_id(1): {'bad': float('nan')}}),
            'block': patch.object(self.context_pack, 'context_block', side_effect=OSError(RAW_FAILURE)),
        }
        for name, failure in failures.items():
            with self.subTest(name), failure:
                code, out, err = self.receive_captured('--active-goal')
            result = json.loads(out)
            self.assertEqual((0, '', self.UNAVAILABLE), (code, err, result['context']))
            self.assertEqual(self.without_context(healthy_result), self.without_context(result))
            self.assertFalse(any('context_node' in item for item in result['requests']))
            for leak in LEAKS:
                self.assertNotIn(leak, out + err)

    def run_receive_script(self, script, options=('--active-goal',)):
        return subprocess.run([sys.executable, '-B', str(script), '--root', str(self.root), '--endpoint', COORDINATOR,
                               *options], capture_output=True, text=True, timeout=60)

    def test_without_a_named_task_an_unusable_context_pack_does_not_make_the_context_unavailable(self):
        for name, source in (('missing', None), ('failing on import', BROKEN_CONTEXT_PACK)):
            with self.subTest(name), tempfile.TemporaryDirectory() as parent:
                completed = self.run_receive_script(scripts_copy(parent, source) / 'receive.py', options=())
                self.assertEqual((0, ''), (completed.returncode, completed.stderr))
                self.assertEqual({'state': 'idle', 'requests': [], 'context': {'available': True, 'packs': {}, 'omitted': 0}},
                                 json.loads(completed.stdout))

    def test_receiving_in_an_installed_layout_with_a_pack_writes_no_file_into_the_mailbox(self):
        self.add_task(self.task_id(1))
        self.publish_for(1)
        scripts = install_layout(self.root)
        before = file_contents(self.root)
        completed = subprocess.run([sys.executable, str(scripts / 'receive.py'), '--root', str(self.root), '--endpoint',
                                    COORDINATOR, '--active-goal'], capture_output=True, text=True, timeout=60,
                                   env=child_environment())
        result = json.loads(completed.stdout)
        self.assertEqual((0, '', True, ['task:' + self.task_id(1)]),
                         (completed.returncode, completed.stderr, result['context']['available'],
                          [item.get('context_node') for item in result['requests']]))
        self.assertEqual(before, file_contents(self.root))

    def test_bytecode_writing_is_off_while_the_context_is_built_and_restored_afterwards(self):
        attach_context = runpy.run_path(str(RECEIVE), run_name='receive_by_path')['attach_context']
        item = {'id': G2}
        seen = []

        def build(root, task_ids):
            seen.append(sys.dont_write_bytecode)
            if task_ids == ['failing']:
                raise OSError(RAW_FAILURE)
            return {'available': True, 'packs': {}, 'omitted': 0}

        for previous in (False, True):
            for task, expected in ((TASK, {'available': True, 'packs': {}, 'omitted': 0}),
                                   ('failing', self.UNAVAILABLE)):
                with (self.subTest(previous=previous, task=task), patch.object(sys, 'dont_write_bytecode', previous),
                      patch.object(self.context_pack, 'context_block', side_effect=build)):
                    seen.clear()
                    self.assertEqual(expected, attach_context(self.root, [item], lambda _: task))
                    self.assertEqual(([True], previous), (seen, sys.dont_write_bytecode))

    def test_the_unavailable_block_of_receive_equals_the_one_of_context_pack(self):
        unavailable_context = runpy.run_path(str(RECEIVE), run_name='receive_by_path')['unavailable_context']
        self.assertEqual(self.context_pack.unavailable_context(), unavailable_context())

    def test_an_unusable_context_pack_script_leaves_receive_as_without_the_failure(self):
        self.add_task(self.task_id(1))
        self.publish_for(1)
        healthy = self.run_receive_script(RECEIVE)
        self.assertEqual((0, ''), (healthy.returncode, healthy.stderr))
        for name, source in (('missing', None), ('failing on import', BROKEN_CONTEXT_PACK)):
            with self.subTest(name), tempfile.TemporaryDirectory() as parent:
                completed = self.run_receive_script(scripts_copy(parent, source) / 'receive.py')
                result = json.loads(completed.stdout)
                self.assertEqual((0, '', self.UNAVAILABLE), (completed.returncode, completed.stderr, result['context']))
                self.assertEqual(self.without_context(json.loads(healthy.stdout)), self.without_context(result))
                self.assertFalse(any('context_node' in item for item in result['requests']))
                for leak in (*LEAKS, 'context_pack', 'RuntimeError'):
                    self.assertNotIn(leak, completed.stdout + completed.stderr)

    def test_receiving_with_packs_attached_leaves_the_mailbox_byte_identical(self):
        self.add_task(self.task_id(1))
        self.publish_for(1)
        before = tree_snapshot(self.root)
        result = self.receive('--active-goal')
        self.receive('--active-goal', '--wake-confirmed')
        self.assertEqual(1, len(result['context']['packs']))
        self.assertEqual(before, tree_snapshot(self.root))
