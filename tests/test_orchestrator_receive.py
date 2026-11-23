"""Exercise the native coordinator receive cycle with an isolated file mailbox."""
import contextlib
import io
import json
from pathlib import Path
import runpy
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


class OrchestratorReceiveTests(unittest.TestCase):
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

    def publish(self, uid, kind, **fields):
        message = dict(id=uid, type=kind, task_id=TASK, from_project='alpha',
                       created_at='2026-01-01T00:00:00Z', to_project='orchestrator',
                       to_endpoint=COORDINATOR)
        path = self.inbox / (uid + '.json')
        staged = path.with_suffix('.tmp')
        staged.write_text(json.dumps({**message, **fields}))
        staged.rename(path)
        return path

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
