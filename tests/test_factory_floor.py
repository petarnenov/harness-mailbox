import json
import shutil
import tempfile
import unittest
from pathlib import Path

import server
from factory_floor import build_factory

try:
    import factory_fixture as fixture
except ModuleNotFoundError:  # run as tests.test_factory_floor from the repository root
    from tests import factory_fixture as fixture

ALPHA, BETA, GHOST_SESSION = fixture.ALPHA, fixture.BETA, fixture.GHOST_SESSION
stamp = fixture.stamp
MALFORMED = 'malformed factory event'
EVERY_FIXTURE_TASK = sorted([*fixture.STATION_TASKS.values(), fixture.REFUSED_G1, fixture.REFUSED_G2, fixture.RETURNED, fixture.COMPLETE])


class FactoryCase(unittest.TestCase):
    """A temporary mailbox holding the two worker lanes; `full_mailbox` adds the fixture's tasks and reports."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture.put_sessions(self.root)

    def full_mailbox(self):
        fixture.put_tasks(self.root)
        fixture.put_status_reports(self.root)

    def put(self, relative, record):
        return fixture.put(self.root, relative, record)

    def task(self, task_id, stages_at, decisions=None, owner=ALPHA, project='proj-alpha'):
        fixture.put_task(self.root, project, task_id, owner, stages_at, decisions)

    def owned_task_record(self, task_id, owner=ALPHA):
        self.put(f'tasks/proj-alpha/{task_id}.json', {'id': task_id, 'project': 'proj-alpha', 'owner_session_id': owner})

    def payload(self):
        return build_factory(server.build_dashboard(self.root), self.root, server.read_json, 'live')

    def events_of(self, task_id):
        return [event for event in self.payload()['events'] if event['task_id'] == task_id]

    def steps_of(self, task_id):
        return [(event['kind'], event['stage']) for event in self.events_of(task_id)]

    def lane(self, session_id):
        return next(lane for lane in self.payload()['lanes'] if lane['session_id'] == session_id)


class LaneTests(FactoryCase):
    def test_one_lane_per_worker_session_record(self):
        lanes = self.payload()['lanes']
        self.assertEqual([(ALPHA, 'proj-alpha'), (BETA, 'proj-beta')], [(lane['session_id'], lane['project']) for lane in lanes])
        self.assertEqual({'session_id': ALPHA, 'project': 'proj-alpha', 'session_name': 'alpha-worker', 'registered_status': 'active',
                          'last_seen_at': stamp(200), 'last_status': None}, lanes[0])

    def test_orchestrator_records_and_records_without_session_id_or_project_give_no_lane(self):
        records = {'no-id': {'role': 'worker', 'project': 'proj-alpha'},
                   'empty-id': {'role': 'worker', 'session_id': '', 'project': 'proj-alpha'},
                   'number-id': {'role': 'worker', 'session_id': 7, 'project': 'proj-alpha'},
                   'no-project': {'role': 'worker', 'session_id': 'sess-x'},
                   'empty-project': {'role': 'worker', 'session_id': 'sess-x', 'project': ''},
                   'no-role': {'session_id': 'sess-x', 'project': 'proj-alpha'},
                   'other-role': {'role': 'orchestrator', 'session_id': 'sess-x', 'project': 'proj-alpha'}}
        for name, record in records.items():
            with self.subTest(name):
                path = self.put(f'sessions/{name}.json', record)
                self.assertEqual([ALPHA, BETA], [lane['session_id'] for lane in self.payload()['lanes']])
                path.unlink()

    def test_two_records_of_one_session_give_one_lane(self):
        self.put('sessions/a-first.json', {'session_id': ALPHA, 'project': 'proj-zeta', 'role': 'worker', 'session_name': 'first'})
        lanes = self.payload()['lanes']
        self.assertEqual([BETA, ALPHA], [lane['session_id'] for lane in lanes])
        self.assertEqual(('proj-zeta', 'first'), (lanes[1]['project'], lanes[1]['session_name']))

    def test_lanes_are_sorted_by_project_then_session_id(self):
        for session_id, project in (('sess-zzz', 'proj-alpha'), ('sess-aaa', 'proj-beta')):
            self.put(f'sessions/{session_id}.json', {'session_id': session_id, 'project': project, 'role': 'worker'})
        self.assertEqual([ALPHA, 'sess-zzz', 'sess-aaa', BETA], [lane['session_id'] for lane in self.payload()['lanes']])

    def test_session_values_that_are_not_text_are_null(self):
        self.put('sessions/odd.json', {'session_id': 'sess-odd', 'project': 'proj-alpha', 'role': 'worker', 'session_name': 5,
                                       'status': ['active'], 'last_seen_at': 1.5})
        self.assertEqual({'session_id': 'sess-odd', 'project': 'proj-alpha', 'session_name': None, 'registered_status': None,
                          'last_seen_at': None, 'last_status': None}, self.lane('sess-odd'))

    def test_stations_are_the_four_delivery_stages_in_order(self):
        self.assertEqual(['investigating', 'implementation', 'review', 'verification'], self.payload()['stations'])

    def test_a_removed_session_record_removes_its_lane_and_its_boxes(self):
        self.full_mailbox()
        before = self.payload()
        self.assertEqual({ALPHA, BETA}, {box['session_id'] for box in before['boxes']})
        (self.root / 'sessions' / 'beta-worker.json').unlink()
        after = self.payload()
        box_ids = {box['task_id'] for box in after['boxes']}
        self.assertEqual([ALPHA], [lane['session_id'] for lane in after['lanes']])
        self.assertEqual({ALPHA}, {box['session_id'] for box in after['boxes']})
        self.assertTrue({event['task_id'] for event in after['events']} <= box_ids)
        self.assertNotIn(fixture.COMPLETE, box_ids)

    def test_a_task_owned_by_a_session_without_a_record_has_no_box(self):
        self.full_mailbox()
        payload = self.payload()
        self.assertNotIn(GHOST_SESSION, [lane['session_id'] for lane in payload['lanes']])
        self.assertNotIn(fixture.GHOST, [box['task_id'] for box in payload['boxes']])
        self.assertNotIn(fixture.GHOST, [event['task_id'] for event in payload['events']])

    def test_every_station_has_a_box_and_boxes_are_sorted_by_task_id(self):
        self.full_mailbox()
        boxes = self.payload()['boxes']
        self.assertEqual(EVERY_FIXTURE_TASK, [box['task_id'] for box in boxes])
        review_box = next(box for box in boxes if box['task_id'] == fixture.STATION_TASKS['review'])
        self.assertEqual({'task_id': 'T-STATION-3', 'title': 'Title of T-STATION-3', 'project': 'proj-beta', 'session_id': BETA,
                          'stage': 'review'}, review_box)

    def test_a_box_rides_the_lane_of_its_owner_whatever_the_task_project(self):
        self.task('T-1', [('investigating', 1)], project='proj-beta')
        self.assertEqual([{'task_id': 'T-1', 'title': 'Title of T-1', 'project': 'proj-beta', 'session_id': ALPHA,
                           'stage': 'investigating'}], self.payload()['boxes'])

    def test_box_stage_is_the_delivery_stage_of_the_dashboard(self):
        self.task('T-1', [('investigating', 1)])
        self.put('deliveries/T-1.json', {'delivery_stage': 'verification', 'history': fixture.history(('investigating', 1))})
        self.assertEqual(['verification'], [box['stage'] for box in self.payload()['boxes']])

    def test_task_with_an_unsafe_identifier_has_no_box(self):
        for task_id in ('bad id', '.hidden'):
            with self.subTest(task_id):
                self.put('tasks/proj-alpha/unsafe.json', {'id': task_id, 'project': 'proj-alpha', 'owner_session_id': ALPHA})
                self.put(f'deliveries/{task_id}.json', {'delivery_stage': 'investigating', 'history': fixture.history(('investigating', 1))})
                payload = self.payload()
                self.assertEqual(([], []), (payload['boxes'], payload['warnings']))


class EventTests(FactoryCase):
    def test_first_station_entry_is_an_enter_event(self):
        self.task('T-1', [('investigating', 1)])
        self.assertEqual([{'id': 'T-1/stage/investigating', 'at': stamp(1), 'task_id': 'T-1', 'kind': 'enter',
                           'stage': 'investigating', 'gate': None}], self.events_of('T-1'))

    def test_a_first_entry_at_a_later_station_is_still_the_enter_event(self):
        self.task('T-1', [('review', 1), ('verification', 2)])
        self.assertEqual([('enter', 'review'), ('move', 'verification')], self.steps_of('T-1'))

    def test_later_station_entries_are_move_events_in_order(self):
        self.task('T-1', [('investigating', 1), ('implementation', 2), ('review', 3), ('verification', 4)])
        self.task('T-2', [('investigating', 1), ('verification', 5)])
        self.assertEqual([('enter', 'investigating'), ('move', 'implementation'), ('move', 'review'), ('move', 'verification')],
                         self.steps_of('T-1'))
        self.assertEqual([('enter', 'investigating'), ('move', 'verification')], self.steps_of('T-2'))
        self.assertEqual(['T-1/stage/implementation', 'T-1/stage/review'], [event['id'] for event in self.events_of('T-1')[1:3]])

    def test_history_is_read_in_time_order_whatever_its_array_order(self):
        self.task('T-1', [('implementation', 5), ('investigating', 1)])
        self.assertEqual([('enter', 'investigating'), ('move', 'implementation')], self.steps_of('T-1'))

    def test_repeated_and_earlier_stages_move_nothing(self):
        self.task('T-1', [('investigating', 1), ('implementation', 2), ('implementation', 3), ('investigating', 4),
                          ('review', 5), ('implementation', 6), ('ready', 7), ('unknown', 8)])
        self.assertEqual([('enter', 'investigating'), ('move', 'implementation'), ('move', 'review')], self.steps_of('T-1'))

    def test_task_without_station_stage_has_no_box(self):
        for stage in ('uninvestigated', 'ready', 'deferred', 'unknown'):
            self.task(f'T-{stage}', [(stage, 1)])
        payload = self.payload()
        self.assertEqual(([], []), (payload['boxes'], payload['events']))

    def test_complete_after_enter_is_an_exit_event(self):
        self.task('T-1', [('investigating', 1), ('implementation', 2), ('review', 3), ('verification', 4), ('complete', 5)])
        self.task('T-2', [('investigating', 1), ('complete', 2)])
        self.assertEqual({'id': 'T-1/exit', 'at': stamp(5), 'task_id': 'T-1', 'kind': 'exit', 'stage': 'verification', 'gate': None},
                         self.events_of('T-1')[-1])
        self.assertEqual([('enter', 'investigating'), ('exit', 'investigating')], self.steps_of('T-2'))

    def test_complete_without_enter_gives_no_event(self):
        self.task('T-1', [('complete', 1)])
        self.task('T-2', [('ready', 1), ('complete', 2)])
        payload = self.payload()
        self.assertEqual(([], []), (payload['boxes'], payload['events']))

    def test_nothing_is_emitted_after_exit(self):
        self.task('T-1', [('investigating', 1), ('complete', 2), ('implementation', 3), ('complete', 4)])
        self.assertEqual([('enter', 'investigating'), ('exit', 'investigating')], self.steps_of('T-1'))

    def test_gate_is_set_on_refuse_events_only(self):
        self.full_mailbox()
        gates = {event['kind']: set() for event in self.payload()['events']}
        for event in self.payload()['events']:
            gates[event['kind']].add(event['gate'])
        self.assertEqual({'enter': {None}, 'move': {None}, 'exit': {None}, 'refuse': {'g1', 'g2', 'report'}}, gates)

    def test_events_are_ordered_by_time_then_task_then_source(self):
        self.task('T-B', [('investigating', 1)])
        self.task('T-A', [('investigating', 1)])
        self.task('T-C', [('investigating', 0)])
        self.task('T-D', [('investigating', 1), ('implementation', 1)])
        self.task('T-E', [('investigating', 2), ('implementation', 6)],
                  {'b-late': {'verdict': 'rejected', 'decided_at': stamp(6)}, 'a-late': {'verdict': 'returned', 'decided_at': stamp(6)}})
        order = [(event['task_id'], event['kind'], event['id'].rsplit('/', 1)[-1]) for event in self.payload()['events']]
        self.assertEqual([('T-C', 'enter', 'investigating'), ('T-A', 'enter', 'investigating'), ('T-B', 'enter', 'investigating'),
                          ('T-D', 'enter', 'investigating'), ('T-D', 'move', 'implementation'), ('T-E', 'enter', 'investigating'),
                          ('T-E', 'move', 'implementation'), ('T-E', 'refuse', 'a-late'), ('T-E', 'refuse', 'b-late')], order)

    def test_events_of_the_fixture_are_in_time_order_with_unique_stable_ids(self):
        self.full_mailbox()
        events = self.payload()['events']
        self.assertEqual(sorted(event['at'] for event in events), [event['at'] for event in events])
        ids = [event['id'] for event in events]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [event['id'] for event in self.payload()['events']])


class RefusalTests(FactoryCase):
    def refusals(self, task_id):
        return {event['id']: (event['stage'], event['gate']) for event in self.events_of(task_id) if event['kind'] == 'refuse'}

    def test_refusal_verdicts_on_g1_g2_and_other_types_give_refuse_events_with_their_gate(self):
        self.task('T-R', [('investigating', 10), ('implementation', 20), ('review', 30), ('verification', 40)], {
            'g1-rejected': {'decision_type': 'g1', 'verdict': 'rejected', 'decided_at': stamp(11)},
            'g1-review-blocked': {'decision_type': 'g1_review', 'verdict': 'blocked', 'decided_at': stamp(12)},
            'g2-changes': {'decision_type': 'g2', 'verdict': 'changes_requested', 'decided_at': stamp(21)},
            'g2-final-failed': {'type': 'g2_final', 'result': 'evidence_accepted_criteria_failed', 'decided_at': stamp(31)},
            'report-returned': {'decision_type': 'report_review', 'verdict': 'returned', 'decided_at': stamp(41)},
            'untyped': {'decision': 'rejected', 'decided_at': stamp(42)}})
        self.assertEqual({'T-R/refuse/g1-rejected': ('investigating', 'g1'), 'T-R/refuse/g1-review-blocked': ('investigating', 'g1'),
                          'T-R/refuse/g2-changes': ('implementation', 'g2'), 'T-R/refuse/g2-final-failed': ('review', 'g2'),
                          'T-R/refuse/report-returned': ('verification', 'report'), 'T-R/refuse/untyped': ('verification', 'report')},
                         self.refusals('T-R'))

    def test_the_fixture_refusals_name_their_gate_and_station(self):
        self.full_mailbox()
        self.assertEqual({'T-REFUSED-G1/refuse/g1': ('investigating', 'g1')}, self.refusals(fixture.REFUSED_G1))
        self.assertEqual({'T-REFUSED-G2/refuse/g2': ('review', 'g2')}, self.refusals(fixture.REFUSED_G2))
        self.assertEqual({'T-RETURNED/refuse/report-review': ('implementation', 'report')}, self.refusals(fixture.RETURNED))

    def test_verdict_is_read_before_result_and_decision_and_the_type_field_before_type(self):
        self.task('T-R', [('investigating', 10)], {
            'verdict-wins': {'decision_type': 'g2', 'verdict': 'approved', 'result': 'rejected', 'decision': 'rejected', 'decided_at': stamp(11)},
            'result-before-decision': {'result': 'approved', 'decision': 'rejected', 'decided_at': stamp(12)},
            'decision-type-wins': {'decision_type': 'g1', 'type': 'g2', 'verdict': 'rejected', 'decided_at': stamp(13)}})
        self.assertEqual({'T-R/refuse/decision-type-wins': ('investigating', 'g1')}, self.refusals('T-R'))

    def test_accepted_merged_and_free_text_verdicts_are_not_refusals(self):
        self.task('T-R', [('investigating', 10)])
        verdicts = ('approved', 'accepted', 'merged', 'requested', 'REJECTED', 'rejected ', 'changes requested', 'please send it back', 5, None)
        for index, verdict in enumerate(verdicts):
            with self.subTest(verdict=verdict):
                path = self.put(f'decisions/T-R/case-{index}.json', {'decision_type': 'g2', 'verdict': verdict, 'decided_at': 'not a time'})
                payload = self.payload()
                self.assertEqual(({}, []), (self.refusals('T-R'), payload['warnings']))
                path.unlink()

    def test_supersedes_and_repeated_requests_are_not_refusals(self):
        self.task('T-R', [('investigating', 10), ('implementation', 20)], {
            'request-1': {'decision_type': 'g2_request', 'supersedes': 'request-0', 'decided_at': stamp(21)},
            'request-1-again': {'decision_type': 'g2_request', 'supersedes': 'request-0', 'decided_at': stamp(22)},
            'names-a-refusal': {'decision_type': 'g2_request', 'supersedes': 'changes_requested', 'decided_at': stamp(23)},
            'approval': {'decision_type': 'g2', 'verdict': 'approved', 'rationale': 'rejected earlier, changes_requested', 'decided_at': stamp(24)}})
        self.assertEqual({}, self.refusals('T-R'))

    def test_refusal_before_enter_or_after_exit_is_left_out(self):
        decided = {'before-enter': stamp(9), 'at-enter': stamp(10), 'inside': stamp(15), 'last-second': '2026-11-20T09:29:59Z',
                   'at-exit': stamp(30), 'after-exit': stamp(31)}
        self.task('T-R', [('investigating', 10), ('implementation', 20), ('complete', 30)],
                  {name: {'verdict': 'rejected', 'decided_at': moment} for name, moment in decided.items()})
        self.assertEqual({'T-R/refuse/at-enter': ('investigating', 'report'), 'T-R/refuse/inside': ('investigating', 'report'),
                          'T-R/refuse/last-second': ('implementation', 'report')}, self.refusals('T-R'))

    def test_a_refusal_after_the_latest_entry_of_a_task_still_in_flight_is_kept(self):
        self.task('T-R', [('investigating', 10)], {'late': {'verdict': 'rejected', 'decided_at': stamp(300)}})
        self.assertEqual({'T-R/refuse/late': ('investigating', 'report')}, self.refusals('T-R'))

    def test_refusal_time_is_compared_as_a_moment_not_as_text(self):
        self.task('T-R', [('investigating', 10)], {
            'before-in-text-after': {'verdict': 'rejected', 'decided_at': '2026-11-20T11:09:30+02:00'},
            'after-in-text-before': {'verdict': 'rejected', 'decided_at': '2026-11-20T11:11:00+02:00'}})
        self.assertEqual({'T-R/refuse/after-in-text-before': ('investigating', 'report')}, self.refusals('T-R'))

    def test_refuse_keeps_the_station_and_has_a_stable_id(self):
        self.task('T-R', [('investigating', 10), ('implementation', 20), ('review', 30)], {
            'g2.final': {'decision_type': 'g2', 'verdict': 'rejected', 'decided_at': stamp(25)},
            'g2-last': {'decision_type': 'g2', 'verdict': 'rejected', 'decided_at': stamp(35)}})
        steps = [(event['kind'], event['stage']) for event in self.events_of('T-R')]
        self.assertEqual([('enter', 'investigating'), ('move', 'implementation'), ('refuse', 'implementation'),
                          ('move', 'review'), ('refuse', 'review')], steps)
        refuse = self.events_of('T-R')[2]
        self.assertEqual(('T-R/refuse/g2.final', stamp(25)), (refuse['id'], refuse['at']))
        self.assertEqual([event['id'] for event in self.events_of('T-R')], [event['id'] for event in self.events_of('T-R')])


class StatusTests(FactoryCase):
    STATUS_BOXES = ('inbox/orchestrator', 'archive/orchestrator', 'outbox/proj-alpha')

    def report(self, box, name, status, created_at, **fields):
        self.put(f'{box}/{name}.json', {'id': name, 'type': 'status_report', 'from_session_id': ALPHA, 'status': status,
                                         'created_at': created_at, **fields})

    def test_the_fixture_gives_the_newest_report_to_its_own_lane_only(self):
        self.full_mailbox()
        self.assertEqual({'status': 'working', 'at': stamp(190)}, self.lane(ALPHA)['last_status'])
        self.assertIsNone(self.lane(BETA)['last_status'])

    def test_last_status_is_the_newest_status_report_of_the_session_across_inbox_archive_and_outbox(self):
        for newest_box in self.STATUS_BOXES:
            with self.subTest(newest_box=newest_box):
                for box in ('inbox', 'archive', 'outbox'):
                    shutil.rmtree(self.root / box, ignore_errors=True)
                older = [box for box in self.STATUS_BOXES if box != newest_box]
                self.report(newest_box, 'report-newest', 'waiting', stamp(300))
                self.report(older[0], 'report-older', 'idle', stamp(10))
                self.report(older[1], 'report-oldest', 'working', stamp(20))
                self.assertEqual({'status': 'waiting', 'at': stamp(300)}, self.lane(ALPHA)['last_status'])

    def test_newest_is_decided_by_the_moment_not_by_the_text(self):
        self.report('inbox/orchestrator', 'earlier-in-text-later', 'idle', '2026-11-20T11:00:00+02:00')
        self.report('inbox/orchestrator', 'later-in-text-earlier', 'working', '2026-11-20T09:30:00Z')
        self.assertEqual({'status': 'working', 'at': '2026-11-20T09:30:00Z'}, self.lane(ALPHA)['last_status'])

    def test_status_outside_the_protocol_values_is_null(self):
        self.report('archive/orchestrator', 'older-valid', 'idle', stamp(10))
        for status in ('on_fire', '', 'WORKING', 5, None):
            with self.subTest(status=status):
                path = self.put('inbox/orchestrator/newest.json', {'type': 'status_report', 'from_session_id': ALPHA,
                                                                    'created_at': stamp(20), 'status': status})
                self.assertIsNone(self.lane(ALPHA)['last_status'])
                path.unlink()
        for status in ('working', 'idle', 'waiting', 'blocked'):
            with self.subTest(status=status):
                path = self.put('inbox/orchestrator/newest.json', {'type': 'status_report', 'from_session_id': ALPHA,
                                                                    'created_at': stamp(20), 'status': status})
                self.assertEqual({'status': status, 'at': stamp(20)}, self.lane(ALPHA)['last_status'])
                path.unlink()

    def test_report_of_another_session_is_not_used(self):
        self.put('inbox/orchestrator/beta.json', {'type': 'status_report', 'from_session_id': BETA, 'created_at': stamp(30), 'status': 'blocked'})
        self.assertEqual(({'status': 'blocked', 'at': stamp(30)}, None), (self.lane(BETA)['last_status'], self.lane(ALPHA)['last_status']))

    def test_messages_that_are_not_valid_status_reports_of_the_session_are_ignored(self):
        self.report('inbox/orchestrator', 'valid', 'idle', stamp(5))
        newer = {'other-type': {'type': 'status_request', 'from_session_id': ALPHA, 'status': 'working', 'created_at': stamp(50)},
                 'invalid-time': {'type': 'status_report', 'from_session_id': ALPHA, 'status': 'working', 'created_at': 'now'},
                 'naive-time': {'type': 'status_report', 'from_session_id': ALPHA, 'status': 'working', 'created_at': '2026-11-20T10:00:00'},
                 'missing-time': {'type': 'status_report', 'from_session_id': ALPHA, 'status': 'working'},
                 'missing-sender': {'type': 'status_report', 'status': 'working', 'created_at': stamp(50)},
                 'unknown-sender': {'type': 'status_report', 'from_session_id': 'sess-unknown', 'status': 'working', 'created_at': stamp(50)}}
        for name, message in newer.items():
            with self.subTest(name):
                path = self.put(f'inbox/orchestrator/{name}.json', message)
                self.assertEqual({'status': 'idle', 'at': stamp(5)}, self.lane(ALPHA)['last_status'])
                path.unlink()


class GuardedReadTests(FactoryCase):
    def setUp(self):
        super().setUp()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self.outside = Path(outside.name)

    def put_outside(self, relative, record):
        """A JSON file outside the mailbox root, and a symlink at `relative` below the root that points to it."""
        target = self.outside / relative.replace('/', '_')
        target.write_text(json.dumps(record))
        link = self.root / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)

    def test_symlinked_oversized_and_invalid_records_become_warnings_and_are_skipped(self):
        valid_history = {'delivery_stage': 'investigating', 'history': fixture.history(('investigating', 1))}
        for task_id in ('T-LINK', 'T-HUGE', 'T-BROKEN', 'T-LIST'):
            self.owned_task_record(task_id)
        self.put_outside('deliveries/T-LINK.json', valid_history)
        self.put('deliveries/T-HUGE.json', ' ' * 4_000_001)
        self.put('deliveries/T-BROKEN.json', '{broken')
        self.put('deliveries/T-LIST.json', '[1]')
        self.task('T-DEC', [('investigating', 1)], {'good': {'decision_type': 'g2', 'verdict': 'rejected', 'decided_at': stamp(2)},
                                                      'huge': ' ' * 4_000_001, 'broken': '{broken', 'list': '[1]'})
        self.put_outside('decisions/T-DEC/linked.json', {'verdict': 'rejected', 'decided_at': stamp(3)})
        self.task('T-DIRLINK', [('investigating', 1)])
        (self.root / 'decisions').mkdir(exist_ok=True)
        (self.root / 'decisions' / 'T-DIRLINK').symlink_to(self.outside, target_is_directory=True)
        self.put_outside('sessions/linked.json', {'session_id': 'sess-leak', 'project': 'proj-alpha', 'role': 'worker'})
        self.put_outside('inbox/orchestrator/linked.json', {'type': 'status_report', 'from_session_id': ALPHA,
                                                             'created_at': stamp(300), 'status': 'blocked'})
        payload = self.payload()
        self.assertCountEqual([
            'deliveries/T-LINK.json: symlink/outside data root', 'deliveries/T-HUGE.json: exceeds 4 MB limit',
            'deliveries/T-BROKEN.json: invalid JSON', 'deliveries/T-LIST.json: record is not a JSON object',
            'decisions/T-DEC/linked.json: symlink/outside data root', 'decisions/T-DEC/huge.json: exceeds 4 MB limit',
            'decisions/T-DEC/broken.json: invalid JSON', 'decisions/T-DEC/list.json: record is not a JSON object',
            'decisions/T-DIRLINK: symlinked directory not followed', 'sessions/linked.json: symlink/outside data root',
            'inbox/orchestrator/linked.json: symlink/outside data root'], payload['warnings'])
        self.assertNotIn(str(self.root), ' '.join(payload['warnings']))
        self.assertEqual(['T-DEC', 'T-DIRLINK'], [box['task_id'] for box in payload['boxes']])
        self.assertEqual(['T-DEC/refuse/good'], [event['id'] for event in payload['events'] if event['kind'] == 'refuse'])
        self.assertEqual([ALPHA, BETA], [lane['session_id'] for lane in payload['lanes']])
        self.assertIsNone(self.lane(ALPHA)['last_status'])

    def test_non_finite_number_in_a_delivery_record_is_a_warning(self):
        self.owned_task_record('T-NUM')
        for literal in ('NaN', 'Infinity', '-Infinity', '1e400'):
            with self.subTest(literal):
                self.put('deliveries/T-NUM.json', '{"delivery_stage": "investigating", "weight": %s, "history": [{"stage": "investigating", "at": "%s"}]}'
                         % (literal, stamp(1)))
                payload = self.payload()
                self.assertEqual([f'deliveries/T-NUM.json: Non-finite JSON number: {literal}'], payload['warnings'])
                self.assertEqual(([], []), (payload['boxes'], payload['events']))

    def test_non_finite_number_in_a_decision_record_is_a_warning_and_keeps_the_other_events(self):
        self.task('T-NUM', [('investigating', 1)], {'inf': '{"verdict": "rejected", "decided_at": "%s", "weight": Infinity}' % stamp(2)})
        payload = self.payload()
        self.assertEqual(['decisions/T-NUM/inf.json: Non-finite JSON number: Infinity'], payload['warnings'])
        self.assertEqual([('enter', 'investigating')], [(event['kind'], event['stage']) for event in payload['events']])

    def test_malformed_event_time_is_one_warning_per_record(self):
        self.owned_task_record('T-M')
        self.put('deliveries/T-M.json', {'delivery_stage': 'review', 'history': [
            {'stage': 'investigating', 'at': stamp(1)}, {'stage': 'implementation', 'at': 'yesterday'}, 'not an object',
            {'stage': 'complete'}, {'stage': 'ready', 'at': 5}, {'stage': 'review', 'at': '2026-11-20T09:03:00'},
            {'stage': 'review', 'at': stamp(4)}]})
        payload = self.payload()
        self.assertEqual([f'deliveries/T-M.json: {MALFORMED}'], payload['warnings'])
        self.assertEqual(['T-M/stage/investigating', 'T-M/stage/review'], [event['id'] for event in payload['events']])

    def test_malformed_refusal_time_is_one_warning_per_refusal_record_and_other_decisions_stay_silent(self):
        self.task('T-D', [('investigating', 1), ('implementation', 2)], {
            'bad-time': {'decision_type': 'g2', 'verdict': 'rejected', 'decided_at': 'tomorrow'},
            'no-time': {'verdict': 'returned'},
            'naive-time': {'verdict': 'blocked', 'decided_at': '2026-11-20T09:05:00'},
            'number-time': {'verdict': 'rejected', 'decided_at': 5},
            'approval': {'verdict': 'approved', 'decided_at': 'tomorrow'},
            'no-verdict': {'decided_at': 'tomorrow'}})
        payload = self.payload()
        self.assertCountEqual([f'decisions/T-D/{name}.json: {MALFORMED}' for name in ('bad-time', 'no-time', 'naive-time', 'number-time')],
                              payload['warnings'])
        self.assertEqual([('enter', 'investigating'), ('move', 'implementation')], [(event['kind'], event['stage']) for event in payload['events']])

    def test_history_that_is_present_but_not_a_list_gives_one_warning_and_no_box(self):
        self.owned_task_record('T-H')
        for history in ('oops', {'at': 1}, 5, 0, ''):
            with self.subTest(history=history):
                self.put('deliveries/T-H.json', {'delivery_stage': 'review', 'history': history})
                payload = self.payload()
                self.assertEqual(([], [f'deliveries/T-H.json: {MALFORMED}']), (payload['boxes'], payload['warnings']))

    def test_history_that_is_absent_or_null_gives_no_box_and_no_warning(self):
        self.owned_task_record('T-H')
        for record in ({'delivery_stage': 'review'}, {'delivery_stage': 'review', 'history': None}):
            with self.subTest(record=record):
                self.put('deliveries/T-H.json', record)
                payload = self.payload()
                self.assertEqual(([], []), (payload['boxes'], payload['warnings']))

    def test_a_history_that_is_not_a_list_leaves_the_other_tasks_projected(self):
        self.task('T-GOOD', [('investigating', 1)])
        self.owned_task_record('T-H')
        self.put('deliveries/T-H.json', {'delivery_stage': 'review', 'history': 'oops'})
        payload = self.payload()
        self.assertEqual(['T-GOOD'], [box['task_id'] for box in payload['boxes']])
        self.assertEqual([f'deliveries/T-H.json: {MALFORMED}'], payload['warnings'])

    def test_task_owners_that_are_a_list_or_an_object_give_no_box_and_the_valid_task_stays(self):
        self.task('T-VALID', [('investigating', 1), ('implementation', 2)])
        self.task('T-LIST', [('investigating', 3)], owner=[ALPHA])
        self.task('T-OBJECT', [('investigating', 4)], owner={'session_id': ALPHA})
        payload = self.payload()
        self.assertEqual(['T-VALID'], [box['task_id'] for box in payload['boxes']])
        self.assertEqual({'T-VALID'}, {event['task_id'] for event in payload['events']})
        self.assertEqual([], payload['warnings'])
        self.assertEqual(['proj-alpha', 'proj-beta'], [lane['project'] for lane in payload['lanes']])

    def test_status_reports_whose_sender_is_a_list_or_an_object_are_not_used_and_the_valid_report_stays(self):
        self.put('inbox/orchestrator/valid.json', {'type': 'status_report', 'from_session_id': ALPHA, 'status': 'idle', 'created_at': stamp(10)})
        for name, sender in (('list', [ALPHA]), ('object', {'session_id': ALPHA})):
            self.put(f'inbox/orchestrator/{name}.json', {'type': 'status_report', 'from_session_id': sender, 'status': 'blocked', 'created_at': stamp(50)})
        payload = self.payload()
        self.assertEqual({'status': 'idle', 'at': stamp(10)}, self.lane(ALPHA)['last_status'])
        self.assertIsNone(self.lane(BETA)['last_status'])
        self.assertEqual([], payload['warnings'])

    def test_warnings_are_the_factory_projections_own_and_unowned_tasks_are_not_read(self):
        self.full_mailbox()
        self.put('projects/broken.json', '{broken')
        self.put('deliveries/T-GHOST.json', '{broken')
        self.assertIn('projects/broken.json: invalid JSON', server.build_dashboard(self.root)['warnings'])
        self.assertEqual([], self.payload()['warnings'])
        self.assertNotIn(fixture.GHOST, [box['task_id'] for box in self.payload()['boxes']])


class DuplicateTaskTests(FactoryCase):
    DUPLICATE = 'duplicate task identity'

    def duplicate(self, alpha_owner, beta_owner):
        for project, owner in (('proj-alpha', alpha_owner), ('proj-beta', beta_owner)):
            self.put(f'tasks/{project}/T-DUP.json', {'id': 'T-DUP', 'project': project, 'title': 'Twice', 'owner_session_id': owner})
        self.put('deliveries/T-DUP.json', '{broken')
        self.put('decisions/T-DUP/g1.json', '{broken')
        self.task('T-OK', [('investigating', 1), ('implementation', 2)])

    def assert_only_the_other_task_is_projected(self, payload):
        self.assertEqual(['T-OK'], [box['task_id'] for box in payload['boxes']])
        ids = [event['id'] for event in payload['events']]
        self.assertEqual(['T-OK/stage/investigating', 'T-OK/stage/implementation'], ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_duplicate_owned_by_one_lane_in_both_records_warns_once_per_record_and_has_no_box(self):
        self.duplicate(ALPHA, ALPHA)
        payload = self.payload()
        self.assertCountEqual([f'tasks/proj-alpha/T-DUP.json: {self.DUPLICATE}', f'tasks/proj-beta/T-DUP.json: {self.DUPLICATE}'],
                              payload['warnings'])
        self.assert_only_the_other_task_is_projected(payload)

    def test_a_duplicate_whose_records_have_different_lane_owners_warns_for_both_and_has_no_box(self):
        self.duplicate(ALPHA, BETA)
        payload = self.payload()
        self.assertCountEqual([f'tasks/proj-alpha/T-DUP.json: {self.DUPLICATE}', f'tasks/proj-beta/T-DUP.json: {self.DUPLICATE}'],
                              payload['warnings'])
        self.assert_only_the_other_task_is_projected(payload)

    def test_a_duplicate_whose_records_are_owned_by_no_lane_gives_no_factory_warning(self):
        self.duplicate(GHOST_SESSION, 'sess-other')
        payload = self.payload()
        self.assertEqual([], payload['warnings'])
        self.assert_only_the_other_task_is_projected(payload)

    def test_a_duplicate_with_one_record_owned_by_a_lane_warns_for_that_record_only(self):
        self.duplicate(ALPHA, GHOST_SESSION)
        payload = self.payload()
        self.assertEqual([f'tasks/proj-alpha/T-DUP.json: {self.DUPLICATE}'], payload['warnings'])
        self.assert_only_the_other_task_is_projected(payload)


class UtcTimeTests(FactoryCase):
    def test_event_times_are_written_in_utc_with_the_z_suffix_and_keep_fractional_seconds(self):
        self.owned_task_record('T-U')
        self.put('deliveries/T-U.json', {'history': [
            {'stage': 'investigating', 'at': '2026-11-20T12:10:00Z'},
            {'stage': 'implementation', 'at': '2026-11-20T14:20:00.250+02:00'},
            {'stage': 'complete', 'at': '2026-11-20T15:40:00+03:00'}]})
        self.put('decisions/T-U/g1.json', {'decision_type': 'g1', 'verdict': 'rejected', 'decided_at': '2026-11-20T14:30:00+02:00'})
        self.assertEqual([('enter', '2026-11-20T12:10:00Z'), ('move', '2026-11-20T12:20:00.250000Z'),
                          ('refuse', '2026-11-20T12:30:00Z'), ('exit', '2026-11-20T12:40:00Z')],
                         [(event['kind'], event['at']) for event in self.payload()['events']])

    def test_last_status_time_is_written_in_utc_with_the_z_suffix(self):
        self.put('inbox/orchestrator/offset.json', {'type': 'status_report', 'from_session_id': ALPHA, 'status': 'waiting',
                                                     'created_at': '2026-11-20T14:50:00+02:00'})
        self.assertEqual({'status': 'waiting', 'at': '2026-11-20T12:50:00Z'}, self.lane(ALPHA)['last_status'])

    def test_last_status_time_keeps_fractional_seconds(self):
        self.put('inbox/orchestrator/fraction.json', {'type': 'status_report', 'from_session_id': ALPHA, 'status': 'working',
                                                       'created_at': '2026-11-20T14:51:00.125+02:00'})
        self.assertEqual({'status': 'working', 'at': '2026-11-20T12:51:00.125000Z'}, self.lane(ALPHA)['last_status'])

    def test_ordering_follows_the_moment_not_the_offset_text(self):
        self.owned_task_record('T-O')
        self.put('deliveries/T-O.json', {'history': [
            {'stage': 'investigating', 'at': '2026-11-20T12:00:00Z'},
            {'stage': 'implementation', 'at': '2026-11-20T13:30:00+02:00'},
            {'stage': 'review', 'at': '2026-11-20T11:45:00Z'}]})
        self.assertEqual([('enter', 'implementation'), ('move', 'review')], self.steps_of('T-O'))
        self.assertEqual(['2026-11-20T11:30:00Z', '2026-11-20T11:45:00Z'], [event['at'] for event in self.events_of('T-O')])


class UnrepresentableTimeTests(FactoryCase):
    UNREPRESENTABLE = ('0001-01-01T00:00:00+01:00', '9999-12-31T23:30:00-01:00')

    def other_task_and_lane_stay(self, payload):
        self.assertIn('T-OTHER', [box['task_id'] for box in payload['boxes']])
        self.assertEqual([('enter', 'investigating'), ('move', 'implementation')],
                         [(event['kind'], event['stage']) for event in payload['events'] if event['task_id'] == 'T-OTHER'])
        self.assertEqual([ALPHA, BETA], [lane['session_id'] for lane in payload['lanes']])

    def test_a_history_time_utc_cannot_express_leaves_the_entry_out_with_one_warning(self):
        for value in self.UNREPRESENTABLE:
            with self.subTest(value=value):
                self.task('T-OTHER', [('investigating', 1), ('implementation', 2)], owner=BETA, project='proj-beta')
                self.owned_task_record('T-H')
                self.put('deliveries/T-H.json', {'history': [
                    {'stage': 'investigating', 'at': stamp(1)}, {'stage': 'implementation', 'at': value}, {'stage': 'review', 'at': stamp(3)}]})
                payload = self.payload()
                self.assertEqual([f'deliveries/T-H.json: {MALFORMED}'], payload['warnings'])
                self.assertEqual([('enter', 'investigating'), ('move', 'review')],
                                 [(event['kind'], event['stage']) for event in payload['events'] if event['task_id'] == 'T-H'])
                self.other_task_and_lane_stay(payload)

    def test_a_refusal_time_utc_cannot_express_gives_no_refuse_event_and_one_warning(self):
        for value in self.UNREPRESENTABLE:
            with self.subTest(value=value):
                self.task('T-OTHER', [('investigating', 1), ('implementation', 2)], owner=BETA, project='proj-beta')
                self.task('T-R', [('investigating', 1), ('implementation', 2)],
                          {'bad': {'decision_type': 'g1', 'verdict': 'rejected', 'decided_at': value}})
                payload = self.payload()
                self.assertEqual([f'decisions/T-R/bad.json: {MALFORMED}'], payload['warnings'])
                self.assertEqual([('enter', 'investigating'), ('move', 'implementation')],
                                 [(event['kind'], event['stage']) for event in payload['events'] if event['task_id'] == 'T-R'])
                self.other_task_and_lane_stay(payload)

    def test_a_status_report_time_utc_cannot_express_is_not_used_and_gives_no_warning(self):
        self.put('inbox/orchestrator/older.json', {'type': 'status_report', 'from_session_id': ALPHA, 'status': 'idle', 'created_at': stamp(10)})
        for value in self.UNREPRESENTABLE:
            with self.subTest(value=value):
                path = self.put('inbox/orchestrator/broken.json', {'type': 'status_report', 'from_session_id': ALPHA, 'status': 'blocked', 'created_at': value})
                payload = self.payload()
                self.assertEqual({'status': 'idle', 'at': stamp(10)}, self.lane(ALPHA)['last_status'])
                self.assertEqual([], payload['warnings'])
                self.assertEqual([ALPHA, BETA], [lane['session_id'] for lane in payload['lanes']])
                path.unlink()

    def test_a_status_report_time_utc_cannot_express_leaves_no_last_status_when_it_is_the_only_report(self):
        for value in self.UNREPRESENTABLE:
            with self.subTest(value=value):
                path = self.put('inbox/orchestrator/broken.json', {'type': 'status_report', 'from_session_id': ALPHA, 'status': 'blocked', 'created_at': value})
                self.assertIsNone(self.lane(ALPHA)['last_status'])
                self.assertEqual([], self.payload()['warnings'])
                path.unlink()


if __name__ == '__main__':
    unittest.main()
